"""Venue abstraction: what the engine needs from any exchange adapter.

Everything at this layer is in human units (floats); each adapter owns the
conversion to its venue's wire representation (scaled ints, lots/ticks, ...).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class MarketSpecs:
    symbol: str
    tick_size: float  # minimum price increment
    size_step: float  # minimum size increment
    min_size: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DesiredQuote:
    side: Side
    price: float
    size: float
    # True when this quote reduces existing inventory. Perps venues map it to
    # close/reduce-only order types; spot venues ignore it.
    reduce_only: bool = False


@dataclass(frozen=True)
class LiveQuote:
    side: Side
    price: float
    size: float
    order_id: Any = None


class Venue(ABC):
    """Adapter contract.

    Ladder venues (CLOBs) implement the four primitives and inherit the
    default `quote_cycle`. Quote-driven venues (RFQ, e.g. Variational)
    override `quote_cycle` entirely.
    """

    name: str = "venue"

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run

    # ------------------------------------------------------------ lifecycle

    @abstractmethod
    async def start(self) -> None:
        """Connect, load market specs, spawn background streams."""

    async def stop(self) -> None:
        """Cancel outstanding quotes and disconnect."""

    # ----------------------------------------------------------- primitives

    @abstractmethod
    def specs(self, symbol: str) -> MarketSpecs: ...

    @abstractmethod
    async def fair_price(self, symbol: str, source: str) -> float | None: ...

    @abstractmethod
    async def inventory(self, symbol: str) -> float:
        """Net exposure in base units (perps: signed position; spot:
        base balance minus the configured target)."""

    async def live_quotes(self, symbol: str) -> list[LiveQuote] | None:
        """Currently resting quotes, or None if unknown (forces re-quote)."""
        return None

    @abstractmethod
    async def replace_quotes(self, symbol: str, quotes: list[DesiredQuote]) -> None:
        """Atomically-ish replace the resting ladder for `symbol`."""

    # ---------------------------------------------------------- quote cycle

    async def quote_cycle(self, market_cfg: "MarketConfig", state: "MarketState") -> None:  # noqa: F821
        """One iteration of quoting for one market (default ladder logic)."""
        from .strategy import compute_quotes, quotes_match

        symbol = market_cfg.symbol
        fair = await self.fair_price(symbol, market_cfg.fair_source)
        if fair is None or fair <= 0:
            log.debug("%s/%s: no fair price yet", self.name, symbol)
            return
        inv = await self.inventory(symbol)
        desired = compute_quotes(market_cfg, self.specs(symbol), fair, inv)

        drift_bps = (
            abs(fair - state.last_quoted_fair) / state.last_quoted_fair * 1e4
            if state.last_quoted_fair > 0
            else float("inf")
        )
        if drift_bps < market_cfg.requote_bps:
            live = await self.live_quotes(symbol)
            if live is not None and quotes_match(
                desired, live, price_tol=fair * market_cfg.requote_bps / 1e4
            ):
                return

        if self.dry_run:
            state.last_quoted_fair = fair
            log.info(
                "[DRY-RUN] %s/%s fair=%.6g inv=%+g quotes: %s",
                self.name, symbol, fair, inv, _fmt(desired),
            )
            return

        await self.replace_quotes(symbol, desired)
        state.last_quoted_fair = fair
        log.info(
            "[LIVE] %s/%s fair=%.6g inv=%+g quotes: %s",
            self.name, symbol, fair, inv, _fmt(desired),
        )


def _fmt(quotes: list[DesiredQuote]) -> str:
    return ", ".join(
        f"{'B' if q.side == Side.BUY else 'S'}{q.price:g}x{q.size:g}"
        f"{'r' if q.reduce_only else ''}"
        for q in quotes
    ) or "(flat)"
