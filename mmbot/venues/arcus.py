"""Arcus venue adapter — perps & stock-token CLOB on Robinhood Chain
(https://arcus.xyz, by the dYdX team).

Arcus uses an Ed25519-keyed REST/WebSocket API: you generate an Ed25519
key, register it in the app, and sign requests. Public examples show
order placement with fields address, accountIndex, marketId, orderSide,
orderType, timeInForce, quantity, price plus apiKey / timestamp /
signature (128-hex, i.e. a raw 64-byte Ed25519 signature).

IMPORTANT — verify against https://docs.arcus.xyz before live use:
docs.arcus.xyz was unreachable from the environment this adapter was
written in, so the endpoint paths and the exact byte layout of the signed
message are best-effort and kept in ONE place each:
  * endpoint paths  -> the `paths` venue setting (overridable in YAML)
  * signed message  -> `_signing_message()` below
Dry-run mode works regardless (it only reads the public order book).

Venue settings:
    base_url: REST base (default https://api.arcus.xyz)
    address:  your wallet address (live only)
    account_index: sub-account index (default 0)
    paths:    optional overrides, defaults:
                orderbook: /v1/orderbook/{market_id}
                orders:    /v1/orders
                cancel_all:/v1/orders/cancel-all
                positions: /v1/positions

Per-market venue settings:
    market_id: Arcus market identifier (required)
    tick_size / size_step: price/size increments (required until fetched
                from the API automatically)

Secrets (environment): ARCUS_API_KEY, ARCUS_PRIVATE_KEY (32-byte Ed25519
seed, hex or base64).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from ..core.config import MarketConfig, VenueConfig
from ..core.venue import DesiredQuote, LiveQuote, MarketSpecs, Side, Venue
from .perpl.auth import load_private_key

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.arcus.xyz"
DEFAULT_PATHS = {
    "orderbook": "/v1/orderbook/{market_id}",
    "orders": "/v1/orders",
    "cancel_all": "/v1/orders/cancel-all",
    "positions": "/v1/positions",
}


class ArcusVenue(Venue):
    name = "arcus"

    def __init__(self, config: VenueConfig, dry_run: bool):
        super().__init__(dry_run)
        self.config = config
        self.market_cfgs: dict[str, MarketConfig] = {m.symbol: m for m in config.markets}
        self.base_url = str(config.settings.get("base_url", DEFAULT_BASE)).rstrip("/")
        self.address = config.settings.get("address", "")
        self.account_index = int(config.settings.get("account_index", 0))
        self.paths = {**DEFAULT_PATHS, **(config.settings.get("paths") or {})}
        self.api_key = ""
        self._key = None
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        import os

        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        for cfg in self.config.markets:
            if "market_id" not in cfg.venue:
                raise ValueError(f"arcus market {cfg.symbol!r} needs venue.market_id")
            if not self.dry_run and (
                "tick_size" not in cfg.venue or "size_step" not in cfg.venue
            ):
                raise ValueError(
                    f"arcus market {cfg.symbol!r} needs venue.tick_size and venue.size_step"
                )
        if not self.dry_run:
            self.api_key = os.environ.get("ARCUS_API_KEY", "")
            secret = os.environ.get("ARCUS_PRIVATE_KEY", "")
            if not self.api_key or not secret or not self.address:
                raise RuntimeError(
                    "arcus live trading needs ARCUS_API_KEY, ARCUS_PRIVATE_KEY "
                    "and the venue 'address' setting"
                )
            self._key = load_private_key(secret)

    async def stop(self) -> None:
        if not self.dry_run and self._client is not None:
            try:
                await self._signed_post(self.paths["cancel_all"], {"address": self.address})
                log.info("arcus: cancelled all orders")
            except Exception:  # noqa: BLE001
                log.exception("arcus: cancel-all on shutdown failed")
        if self._client is not None:
            await self._client.aclose()

    # -------------------------------------------------------------- signing

    def _signing_message(self, timestamp_ms: int, body: dict[str, Any]) -> bytes:
        """Message covered by the Ed25519 signature.

        VERIFY against https://docs.arcus.xyz: assumed to be the decimal
        timestamp concatenated with the canonical (sorted-key, compact)
        JSON body.
        """
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return f"{timestamp_ms}{canonical}".encode()

    async def _signed_post(self, path: str, body: dict[str, Any]) -> Any:
        timestamp = int(time.time() * 1000)
        payload = {
            **body,
            "apiKey": self.api_key,
            "timestamp": timestamp,
        }
        signature = self._key.sign(self._signing_message(timestamp, payload))
        payload["signature"] = signature.hex()  # 128 hex chars
        resp = await self._client.post(path, json=payload)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    # ----------------------------------------------------------- primitives

    def specs(self, symbol: str) -> MarketSpecs:
        cfg = self.market_cfgs[symbol]
        return MarketSpecs(
            symbol=symbol,
            tick_size=float(cfg.venue.get("tick_size", 0.0)),
            size_step=float(cfg.venue.get("size_step", 0.0)),
        )

    async def fair_price(self, symbol: str, source: str) -> float | None:
        cfg = self.market_cfgs[symbol]
        path = self.paths["orderbook"].format(market_id=cfg.venue["market_id"])
        resp = await self._client.get(path)
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def px(level: Any) -> float | None:
            if isinstance(level, dict):
                v = level.get("price") or level.get("p")
            elif isinstance(level, (list, tuple)) and level:
                v = level[0]
            else:
                return None
            return float(v) if v is not None else None

        bb = px(bids[0]) if bids else None
        ba = px(asks[0]) if asks else None
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return bb or ba

    async def inventory(self, symbol: str) -> float:
        if self.dry_run:
            return 0.0
        cfg = self.market_cfgs[symbol]
        data = await self._signed_post(self.paths["positions"], {"address": self.address})
        for pos in (data or {}).get("positions", data if isinstance(data, list) else []):
            if str(pos.get("marketId")) == str(cfg.venue["market_id"]):
                size = float(pos.get("quantity") or pos.get("size") or 0)
                side = str(pos.get("side") or pos.get("positionSide") or "").upper()
                return -size if side in ("SHORT", "SELL") else size
        return 0.0

    async def live_quotes(self, symbol: str) -> list[LiveQuote] | None:
        return None  # unknown → the engine re-quotes on drift/timer

    async def replace_quotes(self, symbol: str, quotes: list[DesiredQuote]) -> None:
        cfg = self.market_cfgs[symbol]
        market_id = cfg.venue["market_id"]
        await self._signed_post(
            self.paths["cancel_all"], {"address": self.address, "marketId": market_id}
        )
        for q in quotes:
            await self._signed_post(
                self.paths["orders"],
                {
                    "address": self.address,
                    "accountIndex": self.account_index,
                    "marketId": market_id,
                    "orderSide": "BUY" if q.side == Side.BUY else "SELL",
                    "orderType": "LIMIT",
                    "timeInForce": "POST_ONLY" if cfg.post_only else "GTT",
                    "quantity": f"{q.size:.10g}",
                    "price": f"{q.price:.10g}",
                    "reduceOnly": q.reduce_only,
                },
            )
