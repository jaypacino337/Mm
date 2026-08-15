"""Session statistics: traded volume, realized PnL (average-cost), fees.

The numbers behind the classic points-farming scorecard — volume pushed
vs. PnL given up — logged periodically and dumped to stats.json.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .venue import Side

log = logging.getLogger(__name__)


@dataclass
class BookStats:
    position: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    volume_quote: float = 0.0
    fees: float = 0.0
    fills: int = 0

    def record(self, side: Side, price: float, size: float, fee: float) -> None:
        self.fills += 1
        self.volume_quote += price * size
        self.fees += fee
        signed = size if side == Side.BUY else -size

        if self.position * signed >= 0:
            # extending (or opening) — new weighted average entry
            total = self.position + signed
            if total != 0:
                self.avg_price = (
                    self.avg_price * abs(self.position) + price * abs(signed)
                ) / abs(total)
            self.position = total
            return

        # reducing / flipping
        closed = min(abs(signed), abs(self.position))
        direction = 1.0 if self.position > 0 else -1.0
        self.realized_pnl += (price - self.avg_price) * closed * direction
        self.position += signed
        if self.position * direction < 0:
            # flipped through zero: remainder opens at the fill price
            self.avg_price = price
        elif self.position == 0:
            self.avg_price = 0.0


class StatsTracker:
    def __init__(self, path: str | Path | None = "stats.json"):
        self.books: dict[tuple[str, str], BookStats] = {}
        self.started_at = time.time()
        self.path = Path(path) if path else None

    def record_fill(
        self, venue: str, symbol: str, side: Side, price: float, size: float, fee: float = 0.0
    ) -> None:
        book = self.books.setdefault((venue, symbol), BookStats())
        book.record(side, price, size, fee)

    @property
    def total_volume(self) -> float:
        return sum(b.volume_quote for b in self.books.values())

    @property
    def total_realized_pnl(self) -> float:
        return sum(b.realized_pnl for b in self.books.values())

    @property
    def total_fees(self) -> float:
        return sum(b.fees for b in self.books.values())

    @property
    def total_fills(self) -> int:
        return sum(b.fills for b in self.books.values())

    def snapshot(self) -> dict:
        return {
            "uptime_s": round(time.time() - self.started_at),
            "volume_quote": round(self.total_volume, 2),
            "realized_pnl": round(self.total_realized_pnl, 6),
            "fees": round(self.total_fees, 6),
            "fills": self.total_fills,
            "books": {
                f"{venue}:{symbol}": {
                    "volume_quote": round(b.volume_quote, 2),
                    "realized_pnl": round(b.realized_pnl, 6),
                    "fees": round(b.fees, 6),
                    "fills": b.fills,
                    "position": b.position,
                    "avg_price": b.avg_price,
                }
                for (venue, symbol), b in sorted(self.books.items())
            },
        }

    def log_summary(self) -> None:
        if not self.total_fills:
            return
        log.info(
            "session stats: volume=$%s pnl=%+.2f fees=%.2f fills=%d",
            f"{self.total_volume:,.2f}",
            self.total_realized_pnl,
            self.total_fees,
            self.total_fills,
        )
        if self.path:
            try:
                self.path.write_text(json.dumps(self.snapshot(), indent=2))
            except OSError:  # noqa: PERF203
                log.debug("stats: could not write %s", self.path)
