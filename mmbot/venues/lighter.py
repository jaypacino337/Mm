"""Lighter venue adapter — zk perps CLOB (https://lighter.xyz), via the
official `lighter-sdk` (pip install lighter-sdk).

The inspiration venue: tight post-only quoting on Lighter farms
volume-based points campaigns while staying close to PnL-flat.

Venue settings:
    network:       mainnet | testnet (default mainnet)
    account_index: your Lighter account index (or env LIGHTER_ACCOUNT_INDEX)
    api_key_index: API key slot (default 0)

Per-market: `symbol` must match Lighter's market symbol (e.g. BTC, ETH,
SOL). Market ids, price/size decimals, and min sizes are fetched from
order_book_details at startup.

Secrets (environment): LIGHTER_API_PRIVATE_KEY (create a key at
https://app.lighter.xyz — API keys menu; the SDK's system_setup example
also works).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from ..core.config import MarketConfig, VenueConfig
from ..core.venue import DesiredQuote, MarketSpecs, Side, Venue

log = logging.getLogger(__name__)


def to_scaled(value: float, decimals: int) -> int:
    return round(value * 10**decimals)


class LighterVenue(Venue):
    name = "lighter"

    def __init__(self, config: VenueConfig, dry_run: bool):
        super().__init__(dry_run)
        try:
            import lighter  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Lighter support requires the official SDK: pip install lighter-sdk"
            ) from exc
        self.config = config
        self.market_cfgs: dict[str, MarketConfig] = {m.symbol: m for m in config.markets}
        network = config.settings.get("network", "mainnet")
        if network not in ("mainnet", "testnet"):
            raise ValueError(f"lighter network must be mainnet|testnet, got {network!r}")
        self.network = network
        self.account_index = int(
            config.settings.get("account_index", os.environ.get("LIGHTER_ACCOUNT_INDEX", -1))
        )
        self.api_key_index = int(config.settings.get("api_key_index", 0))
        self.api_client: Any = None
        self.order_api: Any = None
        self.account_api: Any = None
        self.signer: Any = None
        # symbol -> PerpsOrderBookDetail (market_id, decimals, min sizes)
        self.details: dict[str, Any] = {}
        self._coi = int(time.time() * 1000) % 2**31  # client_order_index seed

    def _profile(self):
        import lighter

        return lighter.MAINNET if self.network == "mainnet" else lighter.TESTNET

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        import lighter

        profile = self._profile()
        self.api_client = lighter.ApiClient(
            configuration=lighter.Configuration(host=profile.api_url)
        )
        self.order_api = lighter.OrderApi(self.api_client)
        self.account_api = lighter.AccountApi(self.api_client)

        detail_resp = await self.order_api.order_book_details()
        by_symbol = {
            d.symbol.upper(): d for d in (detail_resp.order_book_details or [])
        }
        for cfg in self.config.markets:
            d = by_symbol.get(cfg.symbol.upper())
            if d is None:
                raise ValueError(
                    f"lighter market {cfg.symbol!r} not found; available: "
                    f"{', '.join(sorted(by_symbol)) or '(none)'}"
                )
            self.details[cfg.symbol] = d
            log.info(
                "lighter %s: id=%s price_decimals=%s size_decimals=%s maker_fee=%s",
                cfg.symbol, d.market_id, d.price_decimals, d.size_decimals, d.maker_fee,
            )

        if not self.dry_run:
            key = os.environ.get("LIGHTER_API_PRIVATE_KEY", "")
            if not key or self.account_index < 0:
                raise RuntimeError(
                    "lighter live trading needs LIGHTER_API_PRIVATE_KEY and an "
                    "account_index (setting or LIGHTER_ACCOUNT_INDEX env)"
                )
            self.signer = lighter.SignerClient(
                url=profile.api_url,
                account_index=self.account_index,
                api_private_keys={self.api_key_index: key},
                chain_id=profile.chain_id,
            )

    async def stop(self) -> None:
        import lighter

        if self.signer is not None:
            try:
                await self.signer.cancel_all_orders(
                    lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE,
                    int(time.time() * 1000),
                )
                log.info("lighter: cancelled all orders")
            except Exception:  # noqa: BLE001
                log.exception("lighter: cancel-all on shutdown failed")
            close = getattr(self.signer, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
        if self.api_client is not None:
            await self.api_client.close()

    # ----------------------------------------------------------- primitives

    def specs(self, symbol: str) -> MarketSpecs:
        d = self.details[symbol]
        return MarketSpecs(
            symbol=symbol,
            tick_size=10 ** -int(d.price_decimals),
            size_step=10 ** -int(d.size_decimals),
            min_size=float(d.min_base_amount or 0),
        )

    async def fair_price(self, symbol: str, source: str) -> float | None:
        d = self.details[symbol]
        book = await self.order_api.order_book_orders(market_id=d.market_id, limit=1)
        bb = float(book.bids[0].price) if book.bids else None
        ba = float(book.asks[0].price) if book.asks else None
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return bb or ba

    async def inventory(self, symbol: str) -> float:
        if self.dry_run or self.account_index < 0:
            return 0.0
        d = self.details[symbol]
        resp = await self.account_api.account(by="index", value=str(self.account_index))
        for acct in resp.accounts or []:
            for pos in acct.positions or []:
                if int(pos.market_id) == int(d.market_id):
                    size = float(pos.position or 0)
                    return -size if int(pos.sign or 1) < 0 else size
        return 0.0

    async def replace_quotes(self, symbol: str, quotes: list[DesiredQuote]) -> None:
        import lighter

        d = self.details[symbol]
        cfg = self.market_cfgs[symbol]
        await self.signer.cancel_all_orders(
            lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE,
            int(time.time() * 1000),
            cancel_all_market_index=int(d.market_id),
        )
        tif = (
            lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY
            if cfg.post_only
            else lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        )
        for q in quotes:
            self._coi += 1
            tx, resp, err = await self.signer.create_order(
                market_index=int(d.market_id),
                client_order_index=self._coi,
                base_amount=to_scaled(q.size, int(d.size_decimals)),
                price=to_scaled(q.price, int(d.price_decimals)),
                is_ask=q.side == Side.SELL,
                order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=tif,
                reduce_only=q.reduce_only,
            )
            if err:
                log.warning("lighter %s: order rejected: %s", symbol, err)
