"""Variational venue adapter — RFQ-based perps, via the official
`variational` reference SDK (pip install variational).

Variational has no resting order book for makers: market making means
responding to RFQs you receive with two-sided quotes. This adapter
therefore overrides `quote_cycle` instead of using the ladder logic:

  1. poll RFQs received (with indicative pricing),
  2. for each open RFQ leg, quote bid/ask around the venue's indicative
     price, spread and inventory skew coming from the market config,
  3. replace stale quotes when the mid drifts, cancel everything on stop.

Venue settings:
    base_url:   API base (default testnet: https://api.testnet.variational.io/v1;
                mainnet: https://api.variational.io/v1)
    pool_strategy: settlement-pool strategy dict passed through to the SDK,
                e.g. {strategy: use_existing, pool_id: "..."} — see
                https://docs.variational.io/for-developers/api
    quote_expiry_s: quote lifetime (default 30)

Secrets (environment): VARIATIONAL_API_KEY, VARIATIONAL_API_SECRET.

Per-market: `symbol` must match the RFQ leg instrument's underlying/name
(substring match, case-insensitive), so one config block per instrument
you're willing to quote. Markets with symbol "*" quote every RFQ.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.config import MarketConfig, VenueConfig
from ..core.engine import MarketState
from ..core.strategy import maker_bid_ask
from ..core.venue import DesiredQuote, MarketSpecs, Venue

log = logging.getLogger(__name__)

TESTNET_BASE = "https://api.testnet.variational.io/v1"
MAINNET_BASE = "https://api.variational.io/v1"


class VariationalVenue(Venue):
    name = "variational"

    def __init__(self, config: VenueConfig, dry_run: bool):
        super().__init__(dry_run)
        try:
            from variational import Client  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Variational support requires: pip install variational"
            ) from exc
        self.config = config
        self.market_cfgs = config.markets
        self.base_url = config.settings.get("base_url", TESTNET_BASE)
        self.pool_strategy: dict[str, Any] = config.settings.get(
            "pool_strategy", {"strategy": "create_new"}
        )
        self.quote_expiry_s = int(config.settings.get("quote_expiry_s", 30))
        self.client: Any = None
        # rfq_id -> (quote_id, mid used when quoting)
        self._quoted: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        from variational import Client

        key = os.environ.get("VARIATIONAL_API_KEY", "")
        secret = os.environ.get("VARIATIONAL_API_SECRET", "")
        if not key or not secret:
            if self.dry_run:
                log.info("variational: no credentials; dry-run will only log")
                return
            raise RuntimeError(
                "Set VARIATIONAL_API_KEY / VARIATIONAL_API_SECRET "
                "(create at https://testnet.variational.io/app/settings)"
            )
        self.client = Client(key, secret, base_url=self.base_url)

    async def stop(self) -> None:
        if self.client is not None and not self.dry_run:
            try:
                await asyncio.to_thread(self.client.cancel_all_quotes)
                log.info("variational: cancelled all quotes")
            except Exception:  # noqa: BLE001
                log.exception("variational: cancel_all_quotes failed")

    # ------------------------------------------------- unused ladder hooks

    def specs(self, symbol: str) -> MarketSpecs:
        return MarketSpecs(symbol=symbol, tick_size=0.0, size_step=0.0)

    async def fair_price(self, symbol: str, source: str) -> float | None:
        return None

    async def inventory(self, symbol: str) -> float:
        return 0.0

    async def replace_quotes(self, symbol: str, quotes: list[DesiredQuote]) -> None:
        raise NotImplementedError("variational is RFQ-driven; see quote_cycle")

    # ---------------------------------------------------------- RFQ quoting

    def _cfg_for_instrument(self, instrument: dict[str, Any]) -> MarketConfig | None:
        text = str(instrument).lower()
        for cfg in self.market_cfgs:
            if cfg.symbol == "*" or cfg.symbol.lower() in text:
                return cfg
        return None

    async def quote_cycle(self, market_cfg: MarketConfig, state: MarketState) -> None:
        # Called once per configured market per tick; the RFQ poll covers all
        # markets at once, so only run it for the first market in the list.
        if market_cfg is not self.market_cfgs[0]:
            return
        if self.client is None:
            log.debug("variational: no client (dry-run without credentials)")
            return
        rfqs = await asyncio.to_thread(self.client.get_rfqs_received, None, None, True)
        for rfq in getattr(rfqs, "result", None) or []:
            await self._quote_rfq(rfq)

    async def _quote_rfq(self, rfq: dict[str, Any]) -> None:
        rfq_id = rfq.get("id") or rfq.get("rfq_id")
        status = str(rfq.get("rfq_status", "")).lower()
        if status and status not in ("open", "active", "pending"):
            self._quoted.pop(rfq_id, None)
            return

        leg_quotes = []
        mids: list[float] = []
        for leg in rfq.get("legs") or []:
            cfg = self._cfg_for_instrument(leg.get("instrument") or {})
            if cfg is None:
                continue
            mid = self._leg_mid(leg)
            if mid is None or mid <= 0:
                continue
            skew = self._skew_bps(cfg)
            bid, ask = maker_bid_ask(mid, cfg.spread_bps, skew)
            leg_quotes.append(
                {
                    "target_rfq_leg_id": leg.get("rfq_leg_id") or leg.get("id"),
                    "bid": f"{bid:.10g}",
                    "ask": f"{ask:.10g}",
                }
            )
            mids.append(mid)
        if not leg_quotes:
            return

        mid_now = mids[0]
        prev = self._quoted.get(rfq_id)
        cfg0 = self.market_cfgs[0]
        if prev and abs(mid_now - prev[1]) / prev[1] * 1e4 < cfg0.requote_bps:
            return  # existing quote still fresh

        if self.dry_run:
            log.info("[DRY-RUN] variational rfq=%s quotes: %s", rfq_id, leg_quotes)
            self._quoted[rfq_id] = ("dry", mid_now)
            return

        expires = (
            datetime.now(timezone.utc) + timedelta(seconds=self.quote_expiry_s)
        ).isoformat().replace("+00:00", "Z")
        if prev:
            resp = await asyncio.to_thread(
                self.client.replace_quote,
                prev[0], rfq_id, expires, leg_quotes, self.pool_strategy,
            )
        else:
            resp = await asyncio.to_thread(
                self.client.create_quote,
                rfq_id, expires, leg_quotes, self.pool_strategy,
            )
        quote = getattr(resp, "result", resp) or {}
        quote_id = quote.get("id") if isinstance(quote, dict) else None
        if quote_id:
            self._quoted[rfq_id] = (quote_id, mid_now)
            log.info("[LIVE] variational rfq=%s quoted (mid=%g)", rfq_id, mid_now)

    @staticmethod
    def _leg_mid(leg: dict[str, Any]) -> float | None:
        """Indicative mid for an RFQ leg (RFQs fetched with price=True)."""
        for key in ("price", "indicative_price", "mark_price", "mid"):
            v = leg.get(key)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    def _skew_bps(self, cfg: MarketConfig) -> float:
        # Portfolio-based inventory skew for RFQ flow is venue-specific;
        # keep quotes symmetric until positions are wired in.
        return 0.0
