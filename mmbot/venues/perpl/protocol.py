"""Perpl wire-protocol constants and helpers.

Message types, order enums, and scaling helpers as documented in
PerplFoundation/api-docs (websocket.md, types.md).
"""

from __future__ import annotations

from enum import IntEnum


MAINNET_REST = "https://app.perpl.xyz/api"
MAINNET_WS = "wss://app.perpl.xyz"
MAINNET_CHAIN_ID = 143

TESTNET_REST = "https://testnet.perpl.xyz/api"
TESTNET_WS = "wss://testnet.perpl.xyz"
TESTNET_CHAIN_ID = 10143


class Mt(IntEnum):
    """WebSocket message types (`mt` header field)."""

    PING = 1
    PONG = 2
    SUBSCRIPTION_REQUEST = 5
    MARKET_STATE_UPDATE = 9
    CANDLES_SNAPSHOT = 11
    CANDLES_UPDATE = 12
    L2_BOOK_SNAPSHOT = 15
    L2_BOOK_UPDATE = 16
    TRADES_SNAPSHOT = 17
    TRADES_UPDATE = 18
    WALLET_SNAPSHOT = 19
    ORDER_STATUS = 20
    ACCOUNT_UPDATE = 21
    ORDER_REQUEST = 22
    ORDERS_SNAPSHOT = 23
    ORDERS_UPDATE = 24
    FILLS_UPDATE = 25
    POSITIONS_SNAPSHOT = 26
    POSITIONS_UPDATE = 27
    ACCOUNT_STATS_UPDATE = 28
    API_KEY_SIGN_IN = 29
    HEARTBEAT = 100


class OrderType(IntEnum):
    OPEN_LONG = 1
    OPEN_SHORT = 2
    CLOSE_LONG = 3
    CLOSE_SHORT = 4
    CANCEL = 5
    INCREASE_POSITION_COLLATERAL = 6
    CHANGE = 7


class OrderFlags(IntEnum):
    GOOD_TILL_CANCEL = 0
    POST_ONLY = 1
    FILL_OR_KILL = 2
    IMMEDIATE_OR_CANCEL = 4


class OrderStatus(IntEnum):
    PENDING = 1
    OPEN = 2
    PARTIALLY_FILLED = 3
    FILLED = 4
    CANCELED = 5
    EXPIRED = 6
    FAILED = 7
    UNTRIGGERED = 8
    TRIGGERED = 9
    EXECUTED = 10
    # Failure status meaning the request id (rq) was <= account.lfr;
    # retry once with a fresh rq.
    ORDER_DESC_ID_TOO_LOW = 32


class Side(IntEnum):
    BUY = 1
    SELL = 2


def scale(value: float, decimals: int) -> int:
    """Convert a human-readable number to a scaled integer."""
    return round(value * 10**decimals)


def unscale(value: int, decimals: int) -> float:
    """Convert a scaled integer to a human-readable number."""
    return value / 10**decimals
