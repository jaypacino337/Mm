"""Engine: runs every configured venue's quote loop concurrently."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .config import BotConfig, MarketConfig, VenueConfig
from .venue import Venue

log = logging.getLogger(__name__)


@dataclass
class MarketState:
    last_quoted_fair: float = 0.0
    consecutive_errors: int = 0


@dataclass
class VenueRuntime:
    venue: Venue
    config: VenueConfig
    states: dict[str, MarketState] = field(default_factory=dict)


class Engine:
    def __init__(self, config: BotConfig, venues: list[tuple[Venue, VenueConfig]]):
        self.config = config
        self.runtimes = [
            VenueRuntime(venue=v, config=vc, states={m.symbol: MarketState() for m in vc.markets})
            for v, vc in venues
        ]
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        started: list[VenueRuntime] = []
        try:
            for rt in self.runtimes:
                await rt.venue.start()
                started.append(rt)
                log.info("venue %s started (%d markets)", rt.venue.name, len(rt.config.markets))
            await asyncio.gather(*(self._venue_loop(rt) for rt in self.runtimes))
        finally:
            for rt in started:
                try:
                    await rt.venue.stop()
                except Exception:  # noqa: BLE001
                    log.exception("venue %s: shutdown failed", rt.venue.name)

    async def _venue_loop(self, rt: VenueRuntime) -> None:
        interval = min(m.min_requote_interval_s for m in rt.config.markets)
        while not self._stop.is_set():
            for market_cfg in rt.config.markets:
                await self._cycle(rt, market_cfg)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _cycle(self, rt: VenueRuntime, market_cfg: MarketConfig) -> None:
        state = rt.states[market_cfg.symbol]
        try:
            await rt.venue.quote_cycle(market_cfg, state)
            state.consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            state.consecutive_errors += 1
            # Log every error early on, then back off to avoid log spam.
            if state.consecutive_errors <= 3 or state.consecutive_errors % 30 == 0:
                log.exception(
                    "%s/%s: quote cycle failed (%d consecutive)",
                    rt.venue.name, market_cfg.symbol, state.consecutive_errors,
                )
