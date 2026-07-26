"""Bot orchestrator: wires market data, trading session, and strategy."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .auth import ApiCredentials
from .config import BotConfig, StrategyConfig
from .market_data import MarketDataClient
from .protocol import unscale
from .rest import MarketInfo, PerplRest
from .strategy import Quote, compute_quotes, quotes_match_open_orders
from .trading import TradingClient

log = logging.getLogger(__name__)


@dataclass
class MarketRuntime:
    cfg: StrategyConfig
    market: MarketInfo
    last_quoted_fair: float = 0.0


class MarketMakerBot:
    def __init__(self, config: BotConfig, credentials: ApiCredentials | None):
        self.config = config
        self.credentials = credentials
        self.runtimes: list[MarketRuntime] = []
        self.market_data: MarketDataClient | None = None
        self.trading: TradingClient | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        if self.market_data:
            self.market_data.stop()
        if self.trading:
            self.trading.stop()

    # ----------------------------------------------------------------- setup

    async def setup(self) -> None:
        rest = PerplRest(self.config.rest_url, self.config.chain_id, self.credentials)
        try:
            markets = await rest.markets()
        finally:
            await rest.close()
        for cfg in self.config.strategies:
            market = markets.get(cfg.symbol.upper())
            if market is None:
                available = ", ".join(sorted(markets)) or "(none)"
                raise ValueError(
                    f"market {cfg.symbol!r} not found on {self.config.network}; "
                    f"available: {available}"
                )
            self.runtimes.append(MarketRuntime(cfg=cfg, market=market))
            log.info(
                "market %s: id=%d price_decimals=%d size_decimals=%d",
                market.symbol,
                market.market_id,
                market.price_decimals,
                market.size_decimals,
            )
        self.market_data = MarketDataClient(
            self.config.ws_url,
            self.config.chain_id,
            [rt.market.market_id for rt in self.runtimes],
        )
        if not self.config.dry_run:
            if self.credentials is None:
                raise RuntimeError("live trading requires API credentials")
            self.trading = TradingClient(
                self.config.ws_url, self.config.chain_id, self.credentials
            )

    # ------------------------------------------------------------------- run

    async def run(self) -> None:
        await self.setup()
        tasks = [asyncio.create_task(self.market_data.run(), name="market-data")]
        if self.trading:
            tasks.append(asyncio.create_task(self.trading.run(), name="trading"))
            await asyncio.wait_for(self.trading.ready.wait(), timeout=60)
        try:
            await self._quote_loop()
        finally:
            if self.trading and self.trading.ready.is_set():
                log.info("shutting down: cancelling all open orders")
                try:
                    await self.trading.cancel_all(None, self._expiry_block())
                except Exception as exc:  # noqa: BLE001
                    log.warning("cancel-all on shutdown failed: %s", exc)
            self.stop()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _expiry_block(self) -> int:
        ttl = max((rt.cfg.order_ttl_blocks for rt in self.runtimes), default=600)
        return self.market_data.head_block + ttl

    # ------------------------------------------------------------ quote loop

    async def _quote_loop(self) -> None:
        interval = min(rt.cfg.min_requote_interval_s for rt in self.runtimes)
        while not self._stop.is_set():
            for rt in self.runtimes:
                try:
                    await self._quote_market(rt)
                except ConnectionError as exc:
                    log.warning("%s: trading not ready (%s)", rt.market.symbol, exc)
                except Exception:  # noqa: BLE001
                    log.exception("%s: quote cycle failed", rt.market.symbol)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def fair_price(self, rt: MarketRuntime) -> float | None:
        md = self.market_data
        book = md.books.get(rt.market.market_id)
        state = md.market_state.get(rt.market.market_id) or {}
        source = rt.cfg.fair_source
        scaled: float | None = None
        if source == "book_mid" and book:
            scaled = book.mid()
        elif source == "mark":
            scaled = state.get("mrk")
        elif source == "last":
            scaled = state.get("lst")
        elif source == "state_mid":
            scaled = state.get("mid")
        if scaled is None:  # fall back through the alternatives
            scaled = (
                (book.mid() if book else None)
                or state.get("mid")
                or state.get("mrk")
                or state.get("lst")
            )
        if scaled is None:
            return None
        return unscale(scaled, rt.market.price_decimals)

    def inventory(self, rt: MarketRuntime) -> float:
        """Net position in base units for this market (0 in dry-run)."""
        if not self.trading:
            return 0.0
        net = 0.0
        for pos in self.trading.positions.values():
            if int(pos.raw.get("mkt", -1)) != rt.market.market_id:
                continue
            size = unscale(abs(pos.size), rt.market.size_decimals)
            side = pos.side
            if side == 2 or (side is None and pos.size < 0):
                net -= size
            else:
                net += size
        return net

    async def _quote_market(self, rt: MarketRuntime) -> None:
        fair = self.fair_price(rt)
        if fair is None:
            log.debug("%s: no fair price yet", rt.market.symbol)
            return

        inventory = self.inventory(rt)
        quotes = compute_quotes(rt.cfg, rt.market, fair, inventory)

        tolerance = max(
            1, round(fair * rt.cfg.requote_bps / 1e4 * 10**rt.market.price_decimals)
        )
        drift_ok = (
            rt.last_quoted_fair > 0
            and abs(fair - rt.last_quoted_fair) / rt.last_quoted_fair * 1e4
            < rt.cfg.requote_bps
        )

        if self.config.dry_run:
            if not drift_ok:
                rt.last_quoted_fair = fair
                self._log_quotes(rt, fair, inventory, quotes)
            return

        open_orders = [
            (o.order_type, o.price, o.size)
            for o in self.trading.open_orders.values()
            if o.market_id == rt.market.market_id
        ]
        if drift_ok and quotes_match_open_orders(quotes, open_orders, tolerance):
            return

        # Simple and robust re-quote: cancel this market's ladder, then post
        # the new one. (The API also supports Change (t=7) which saves
        # messages; cancel+post keeps state handling trivial and correct.)
        expiry = self.market_data.head_block + rt.cfg.order_ttl_blocks
        await self.trading.cancel_all(rt.market.market_id, expiry)
        for q in quotes:
            await self.trading.place_limit(
                market_id=rt.market.market_id,
                order_type=q.order_type,
                price_scaled=q.price,
                size_scaled=q.size,
                leverage_hundredths=round(rt.cfg.leverage * 100),
                last_block=expiry,
                post_only=rt.cfg.post_only,
            )
        rt.last_quoted_fair = fair
        self._log_quotes(rt, fair, inventory, quotes)

    def _log_quotes(
        self, rt: MarketRuntime, fair: float, inventory: float, quotes: list[Quote]
    ) -> None:
        mode = "DRY-RUN" if self.config.dry_run else "LIVE"
        lines = ", ".join(
            f"{'B' if q.side == 1 else 'S'}{unscale(q.price, rt.market.price_decimals):g}"
            f"x{unscale(q.size, rt.market.size_decimals):g}"
            for q in quotes
        )
        log.info(
            "[%s] %s fair=%.6g inv=%+g quotes: %s",
            mode,
            rt.market.symbol,
            fair,
            inventory,
            lines or "(flat — position cap reached)",
        )
