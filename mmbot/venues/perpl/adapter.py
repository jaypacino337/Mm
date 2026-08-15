"""Perpl venue adapter: maps the neutral Venue interface onto the native
Perpl WebSocket clients (see protocol.py / trading.py / market_data.py)."""

from __future__ import annotations

import asyncio
import logging

from ...core.config import MarketConfig, VenueConfig
from ...core.venue import DesiredQuote, LiveQuote, MarketSpecs, Side, Venue
from . import protocol
from .auth import ApiCredentials
from .market_data import MarketDataClient
from .protocol import OrderType, scale, unscale
from .rest import MarketInfo, PerplRest
from .trading import TradingClient

log = logging.getLogger(__name__)


def order_type_for(quote: DesiredQuote) -> OrderType:
    """Perpl orders are open/close-typed; reduce-only quotes close."""
    if quote.side == Side.BUY:
        return OrderType.CLOSE_SHORT if quote.reduce_only else OrderType.OPEN_LONG
    return OrderType.CLOSE_LONG if quote.reduce_only else OrderType.OPEN_SHORT


class PerplVenue(Venue):
    name = "perpl"

    def __init__(self, config: VenueConfig, dry_run: bool):
        super().__init__(dry_run)
        self.config = config
        network = config.settings.get("network", "mainnet")
        if network not in ("mainnet", "testnet"):
            raise ValueError(f"perpl network must be mainnet|testnet, got {network!r}")
        self.rest_url = protocol.MAINNET_REST if network == "mainnet" else protocol.TESTNET_REST
        self.ws_url = protocol.MAINNET_WS if network == "mainnet" else protocol.TESTNET_WS
        self.chain_id = (
            protocol.MAINNET_CHAIN_ID if network == "mainnet" else protocol.TESTNET_CHAIN_ID
        )
        self.markets: dict[str, MarketInfo] = {}
        self.market_cfgs: dict[str, MarketConfig] = {m.symbol: m for m in config.markets}
        self.market_data: MarketDataClient | None = None
        self.trading: TradingClient | None = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        credentials = None if self.dry_run else ApiCredentials.from_env()
        rest = PerplRest(self.rest_url, self.chain_id, credentials)
        try:
            all_markets = await rest.markets()
        finally:
            await rest.close()
        for cfg in self.config.markets:
            info = all_markets.get(cfg.symbol.upper())
            if info is None:
                raise ValueError(
                    f"perpl market {cfg.symbol!r} not found; "
                    f"available: {', '.join(sorted(all_markets)) or '(none)'}"
                )
            self.markets[cfg.symbol] = info
        self.market_data = MarketDataClient(
            self.ws_url, self.chain_id, [m.market_id for m in self.markets.values()]
        )
        self._tasks.append(asyncio.create_task(self.market_data.run(), name="perpl-md"))
        if not self.dry_run:
            self.trading = TradingClient(self.ws_url, self.chain_id, credentials)
            self._tasks.append(asyncio.create_task(self.trading.run(), name="perpl-trading"))
            await asyncio.wait_for(self.trading.ready.wait(), timeout=60)

    async def stop(self) -> None:
        if self.trading and self.trading.ready.is_set():
            log.info("perpl: cancelling all open orders on shutdown")
            expiry = self.market_data.head_block + 600
            try:
                await self.trading.cancel_all(None, expiry)
            except Exception:  # noqa: BLE001
                log.exception("perpl: cancel-all on shutdown failed")
        if self.market_data:
            self.market_data.stop()
        if self.trading:
            self.trading.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ----------------------------------------------------------- primitives

    def specs(self, symbol: str) -> MarketSpecs:
        info = self.markets[symbol]
        return MarketSpecs(
            symbol=symbol, tick_size=info.price_tick, size_step=info.size_step
        )

    async def fair_price(self, symbol: str, source: str) -> float | None:
        info = self.markets[symbol]
        book = self.market_data.books.get(info.market_id)
        state = self.market_data.market_state.get(info.market_id) or {}
        scaled: float | None = None
        if source == "book_mid" and book:
            scaled = book.mid()
        elif source == "mark":
            scaled = state.get("mrk")
        elif source == "last":
            scaled = state.get("lst")
        elif source == "state_mid":
            scaled = state.get("mid")
        if scaled is None:
            scaled = (
                (book.mid() if book else None)
                or state.get("mid")
                or state.get("mrk")
                or state.get("lst")
            )
        return unscale(scaled, info.price_decimals) if scaled is not None else None

    async def inventory(self, symbol: str) -> float:
        if not self.trading:
            return 0.0
        info = self.markets[symbol]
        net = 0.0
        for pos in self.trading.positions.values():
            if int(pos.raw.get("mkt", -1)) != info.market_id:
                continue
            size = unscale(abs(pos.size), info.size_decimals)
            side = pos.side
            net += -size if (side == 2 or (side is None and pos.size < 0)) else size
        return net

    async def live_quotes(self, symbol: str) -> list[LiveQuote] | None:
        if not self.trading:
            return []
        info = self.markets[symbol]
        out = []
        for o in self.trading.open_orders.values():
            if o.market_id != info.market_id:
                continue
            out.append(
                LiveQuote(
                    side=Side.BUY if o.is_bid else Side.SELL,
                    price=unscale(o.price, info.price_decimals),
                    size=unscale(o.size, info.size_decimals),
                    order_id=o.order_id,
                )
            )
        return out

    async def replace_quotes(self, symbol: str, quotes: list[DesiredQuote]) -> None:
        info = self.markets[symbol]
        cfg = self.market_cfgs[symbol]
        expiry = self.market_data.head_block + cfg.order_ttl
        await self.trading.cancel_all(info.market_id, expiry)
        for q in quotes:
            await self.trading.place_limit(
                market_id=info.market_id,
                order_type=order_type_for(q),
                price_scaled=scale(q.price, info.price_decimals),
                size_scaled=scale(q.size, info.size_decimals),
                leverage_hundredths=round(cfg.leverage * 100),
                last_block=expiry,
                post_only=cfg.post_only,
            )
