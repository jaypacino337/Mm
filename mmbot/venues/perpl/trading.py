"""Trading WebSocket client: authentication, order placement, state tracking.

Implements the /ws/v1/trading protocol from PerplFoundation/api-docs:
ApiKeySignIn (mt 29) as the first frame, then Wallet/Orders/Positions
snapshots, order requests (mt 22) with a strictly increasing per-account
request id (rq) seeded from the account's `lfr`, and heartbeat sequence
tracking with forced reconnect on gaps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets

from .auth import ApiCredentials
from .protocol import Mt, OrderFlags, OrderType

log = logging.getLogger(__name__)

BACKOFF_S = [1, 2, 4, 8, 16, 32, 60]
AUTH_FAILURE_CLOSE_CODE = 3401


@dataclass
class OpenOrder:
    order_id: int
    market_id: int
    order_type: int
    price: int  # scaled
    size: int  # scaled
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_bid(self) -> bool:
        return self.order_type in (OrderType.OPEN_LONG, OrderType.CLOSE_SHORT)


@dataclass
class Position:
    position_id: int
    size: int  # scaled; sign/side taken from raw `t`/`pt` when present
    entry_price: int
    leverage: int
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> int | None:
        """1 = long, 2 = short when the server includes a position type."""
        for key in ("t", "pt", "type", "side"):
            if key in self.raw:
                return int(self.raw[key])
        return None


class TradingClient:
    def __init__(self, ws_base: str, chain_id: int, credentials: ApiCredentials):
        self.url = f"{ws_base.rstrip('/')}/ws/v1/trading"
        self.chain_id = chain_id
        self.credentials = credentials

        self.account_id: int | None = None
        self.balance: str | None = None
        self.open_orders: dict[int, OpenOrder] = {}
        self.positions: dict[int, Position] = {}
        self.ready = asyncio.Event()  # set after snapshots arrive
        self.on_fill: Callable[[dict[str, Any]], None] | None = None
        self.on_order_update: Callable[[], None] | None = None

        self._rq: int = 0
        self._last_sn: int | None = None
        self._ws = None
        self._stop = asyncio.Event()
        # rq -> (order payload, first status seen) for idempotency bookkeeping
        self._pending: dict[int, dict[str, Any]] = {}

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=2**24) as ws:
                    self._ws = ws
                    self._last_sn = None
                    await ws.send(json.dumps(self.credentials.ws_sign_in_frame(self.chain_id)))
                    attempt = 0
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed as exc:
                if exc.code == AUTH_FAILURE_CLOSE_CODE:
                    log.warning("trading ws auth failure (3401); re-signing on reconnect")
                else:
                    log.warning("trading ws closed: %s", exc)
            except Exception as exc:  # noqa: BLE001 — reconnect on any failure
                log.warning("trading ws error: %s", exc)
            finally:
                self._ws = None
                self.ready.clear()
            if self._stop.is_set():
                break
            delay = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            attempt += 1
            log.info("trading ws reconnecting in %ss", delay)
            await asyncio.sleep(delay)

    async def _read_loop(self, ws) -> None:
        ping_task = asyncio.create_task(self._pinger(ws))
        snapshots_seen: set[int] = set()
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if not self._handle(msg, snapshots_seen):
                    return  # heartbeat gap → reconnect
        finally:
            ping_task.cancel()

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(30)
            await ws.send(json.dumps({"mt": int(Mt.PING), "t": int(time.time() * 1000)}))

    # -------------------------------------------------------------- handlers

    def _handle(self, msg: dict[str, Any], snapshots_seen: set[int]) -> bool:
        mt = msg.get("mt")
        if mt == Mt.WALLET_SNAPSHOT:
            self._last_sn = msg.get("sn", self._last_sn)
            for acct in self._extract_accounts(msg):
                self._apply_account(acct)
            snapshots_seen.add(int(mt))
        elif mt == Mt.ACCOUNT_UPDATE:
            self._apply_account(msg)
        elif mt == Mt.ORDERS_SNAPSHOT:
            self.open_orders.clear()
            for o in self._items(msg):
                self._apply_order(o, removed=False)
            snapshots_seen.add(int(mt))
        elif mt == Mt.ORDERS_UPDATE:
            for o in self._items(msg):
                self._apply_order(o, removed=bool(o.get("r")))
            if self.on_order_update:
                self.on_order_update()
        elif mt == Mt.POSITIONS_SNAPSHOT:
            self.positions.clear()
            for p in self._items(msg):
                self._apply_position(p)
            snapshots_seen.add(int(mt))
        elif mt == Mt.POSITIONS_UPDATE:
            for p in self._items(msg):
                self._apply_position(p)
        elif mt == Mt.FILLS_UPDATE:
            for f in self._items(msg):
                log.info("fill: %s", f)
                if self.on_fill:
                    self.on_fill(f)
        elif mt == Mt.ORDER_STATUS:
            self._apply_status(msg)
        elif mt == Mt.HEARTBEAT:
            sn = msg.get("sn")
            if self._last_sn is not None and sn != self._last_sn + 1:
                log.warning("trading heartbeat gap (%s -> %s); reconnecting", self._last_sn, sn)
                return False
            self._last_sn = sn
        if not self.ready.is_set() and {
            int(Mt.WALLET_SNAPSHOT),
            int(Mt.ORDERS_SNAPSHOT),
            int(Mt.POSITIONS_SNAPSHOT),
        } <= snapshots_seen:
            self.ready.set()
            log.info(
                "trading session ready: account=%s rq_seed=%s open_orders=%d",
                self.account_id,
                self._rq,
                len(self.open_orders),
            )
        return True

    @staticmethod
    def _items(msg: dict[str, Any]) -> list[dict[str, Any]]:
        d = msg.get("d")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return list(d.values())
        return []

    @staticmethod
    def _extract_accounts(msg: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("accounts", "acc", "d"):
            v = msg.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                return list(v.values())
        return [msg] if "lfr" in msg else []

    def _apply_account(self, acct: dict[str, Any]) -> None:
        if "id" in acct:
            self.account_id = int(acct["id"])
        if "b" in acct:
            self.balance = acct["b"]
        lfr = acct.get("lfr")
        if lfr is not None:
            # Seed/refresh the request-id counter; rq must stay > lfr.
            self._rq = max(self._rq, int(lfr))

    def _apply_order(self, o: dict[str, Any], removed: bool) -> None:
        oid = o.get("id") or o.get("oid")
        if oid is None:
            return
        oid = int(oid)
        if removed:
            self.open_orders.pop(oid, None)
            return
        existing = self.open_orders.get(oid)
        merged = {**(existing.raw if existing else {}), **o}
        self.open_orders[oid] = OpenOrder(
            order_id=oid,
            market_id=int(merged.get("mkt", existing.market_id if existing else 0)),
            order_type=int(merged.get("t", existing.order_type if existing else 0)),
            price=int(merged.get("p", existing.price if existing else 0)),
            size=int(merged.get("s", existing.size if existing else 0)),
            raw=merged,
        )

    def _apply_position(self, p: dict[str, Any]) -> None:
        pid = p.get("id")
        if pid is None:
            return
        pid = int(pid)
        size = int(p.get("s", 0))
        if size == 0:
            self.positions.pop(pid, None)
            return
        self.positions[pid] = Position(
            position_id=pid,
            size=size,
            entry_price=int(p.get("ep", 0)),
            leverage=int(p.get("lv", 0)),
            raw=p,
        )

    def _apply_status(self, msg: dict[str, Any]) -> None:
        rq = msg.get("rq")
        sr = msg.get("sr")
        if rq in self._pending:
            self._pending.pop(rq, None)
        if sr in (7, 32):  # Failed / OrderDescIdTooLow
            log.warning("order request rq=%s failed with sr=%s", rq, sr)

    # ---------------------------------------------------------------- orders

    def next_rq(self) -> int:
        self._rq += 1
        return self._rq

    async def send_order(self, payload: dict[str, Any]) -> int:
        """Send an OrderRequest (mt 22); fills in mt/rq/acc. Returns the rq."""
        if self._ws is None or not self.ready.is_set():
            raise ConnectionError("trading ws not ready")
        if self.account_id is None:
            raise ConnectionError("account id unknown (no wallet snapshot yet)")
        rq = self.next_rq()
        frame = {"mt": int(Mt.ORDER_REQUEST), "rq": rq, "acc": self.account_id, **payload}
        self._pending[rq] = frame
        await self._ws.send(json.dumps(frame))
        return rq

    async def place_limit(
        self,
        market_id: int,
        order_type: OrderType,
        price_scaled: int,
        size_scaled: int,
        leverage_hundredths: int,
        last_block: int,
        post_only: bool = True,
    ) -> int:
        return await self.send_order(
            {
                "mkt": market_id,
                "t": int(order_type),
                "p": price_scaled,
                "s": size_scaled,
                "fl": int(OrderFlags.POST_ONLY if post_only else OrderFlags.GOOD_TILL_CANCEL),
                "lv": leverage_hundredths,
                "lb": last_block,
            }
        )

    async def cancel(self, market_id: int, order_id: int, last_block: int) -> int:
        return await self.send_order(
            {
                "mkt": market_id,
                "oid": order_id,
                "t": int(OrderType.CANCEL),
                "p": 0,
                "s": 0,
                "fl": 0,
                "lv": 0,
                "lb": last_block,
            }
        )

    async def cancel_all(self, market_id: int | None, last_block: int) -> None:
        for order in list(self.open_orders.values()):
            if market_id is None or order.market_id == market_id:
                try:
                    await self.cancel(order.market_id, order.order_id, last_block)
                except ConnectionError:
                    log.warning("cancel_all: connection lost, %d orders left", len(self.open_orders))
                    return
