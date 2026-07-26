"""Market-data WebSocket client: L2 order book and market state.

Maintains a local book from L2BookSnapshot/Update frames and the latest
MarketStateUpdate. Reconnects with exponential backoff and forces a
reconnect on heartbeat sequence gaps (lost messages).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets

from .protocol import Mt

log = logging.getLogger(__name__)

BACKOFF_S = [1, 2, 4, 8, 16, 32, 60]


@dataclass
class BookLevel:
    price: int  # scaled
    size: int  # scaled
    orders: int


@dataclass
class OrderBook:
    market_id: int
    bids: dict[int, BookLevel] = field(default_factory=dict)  # price -> level
    asks: dict[int, BookLevel] = field(default_factory=dict)
    updated_at: float = 0.0

    def apply(self, msg: dict[str, Any], snapshot: bool) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        for side_key, book_side in (("bid", self.bids), ("ask", self.asks)):
            for lvl in msg.get(side_key) or []:
                p, s, o = int(lvl["p"]), int(lvl["s"]), int(lvl.get("o", 0))
                if o == 0 or s == 0:
                    book_side.pop(p, None)
                else:
                    book_side[p] = BookLevel(p, s, o)
        self.updated_at = time.monotonic()

    def best_bid(self) -> int | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> int | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2


class MarketDataClient:
    def __init__(self, ws_base: str, chain_id: int, market_ids: list[int]):
        self.url = f"{ws_base.rstrip('/')}/ws/v1/market-data"
        self.chain_id = chain_id
        self.market_ids = market_ids
        self.books: dict[int, OrderBook] = {m: OrderBook(m) for m in market_ids}
        # market_id -> latest market-state dict (mid/mrk/lst/bid/ask, scaled)
        self.market_state: dict[int, dict[str, Any]] = {}
        self.head_block: int = 0
        self.on_update: Callable[[], None] | None = None
        self._last_sn: int | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=2**24) as ws:
                    attempt = 0
                    self._last_sn = None
                    await self._subscribe(ws)
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any failure
                log.warning("market-data ws error: %s", exc)
            if self._stop.is_set():
                break
            delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            attempt += 1
            log.info("market-data reconnecting in %ss", delay)
            await asyncio.sleep(delay)

    async def _subscribe(self, ws) -> None:
        subs = [{"stream": f"heartbeat@{self.chain_id}", "subscribe": True}]
        subs.append({"stream": f"market-state@{self.chain_id}", "subscribe": True})
        for m in self.market_ids:
            subs.append({"stream": f"order-book@{m}", "subscribe": True})
        await ws.send(json.dumps({"mt": int(Mt.SUBSCRIPTION_REQUEST), "subs": subs}))

    async def _read_loop(self, ws) -> None:
        ping_task = asyncio.create_task(self._pinger(ws))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if not self._handle(msg):
                    return  # heartbeat gap → force reconnect
        finally:
            ping_task.cancel()

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(30)
            await ws.send(json.dumps({"mt": int(Mt.PING), "t": int(time.time() * 1000)}))

    def _handle(self, msg: dict[str, Any]) -> bool:
        mt = msg.get("mt")
        if mt in (Mt.L2_BOOK_SNAPSHOT, Mt.L2_BOOK_UPDATE):
            market_id = self._market_for_sid(msg)
            if market_id is not None:
                self.books[market_id].apply(msg, snapshot=(mt == Mt.L2_BOOK_SNAPSHOT))
                if self.on_update:
                    self.on_update()
        elif mt == Mt.MARKET_STATE_UPDATE:
            for mid_str, state in (msg.get("d") or {}).items():
                self.market_state[int(mid_str)] = state
            if self.on_update:
                self.on_update()
        elif mt == Mt.HEARTBEAT:
            sn = msg.get("sn")
            self.head_block = int(msg.get("h") or self.head_block)
            if self._last_sn is not None and sn != self._last_sn + 1:
                log.warning("heartbeat gap (%s -> %s); reconnecting", self._last_sn, sn)
                return False
            self._last_sn = sn
        return True

    # The sid on book frames is the subscription id; with one book stream per
    # market we track sid -> market on first snapshot, falling back to the
    # only market when a single one is subscribed.
    _sid_market: dict[int, int] = {}

    def _market_for_sid(self, msg: dict[str, Any]) -> int | None:
        sid = msg.get("sid")
        if sid in self._sid_market:
            return self._sid_market[sid]
        if len(self.market_ids) == 1:
            if sid is not None:
                self._sid_market[sid] = self.market_ids[0]
            return self.market_ids[0]
        # Multi-market: the stream name may be echoed on the frame.
        stream = msg.get("stream") or msg.get("st")
        if isinstance(stream, str) and "@" in stream:
            market_id = int(stream.split("@", 1)[1])
            if sid is not None:
                self._sid_market[sid] = market_id
            return market_id
        log.debug("book frame with unknown sid %s", sid)
        return None
