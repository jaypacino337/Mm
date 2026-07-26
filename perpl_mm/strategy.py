"""Quote ladder computation — pure functions, unit-testable.

Works in scaled integer prices/sizes (the exchange representation) so the
output can be sent directly. Fair price and skew math happen in floats,
then bids round down / asks round up to the price tick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import StrategyConfig
from .protocol import OrderType, Side
from .rest import MarketInfo


@dataclass(frozen=True)
class Quote:
    side: Side
    order_type: OrderType
    price: int  # scaled
    size: int  # scaled


def signed_inventory(long_size: float, short_size: float) -> float:
    """Net inventory in base units: positive when long, negative when short."""
    return long_size - short_size


def compute_quotes(
    cfg: StrategyConfig,
    market: MarketInfo,
    fair_price: float,
    inventory: float,
) -> list[Quote]:
    """Build the desired ladder around `fair_price` (human units).

    Inventory handling:
    - Quotes skew away from inventory: long inventory pushes both sides down
      (buy less eagerly, sell more eagerly) and vice versa.
    - The side that would grow inventory past max_position is dropped.
    - The reducing side uses Close* order types up to the open position, and
      Open* beyond it (Perpl orders are open/close-typed).
    """
    if fair_price <= 0 or cfg.levels <= 0:
        return []

    ratio = 0.0
    if cfg.max_position > 0:
        ratio = max(-1.0, min(1.0, inventory / cfg.max_position))
    skew = -ratio * cfg.inventory_skew_bps  # bps shift applied to both sides

    half_spread = cfg.spread_bps / 2
    quotes: list[Quote] = []
    size_scaled = round(cfg.order_size * 10**market.size_decimals)
    if size_scaled <= 0:
        return []

    price_factor = 10**market.price_decimals
    remaining_short = max(0.0, -inventory)  # closable by buying
    remaining_long = max(0.0, inventory)  # closable by selling

    for k in range(cfg.levels):
        offset = half_spread + k * cfg.level_step_bps

        # ----- bid (buy) -----
        would_be = inventory + (k + 1) * cfg.order_size
        if would_be <= cfg.max_position:
            bid_px = fair_price * (1 + (skew - offset) / 1e4)
            bid_scaled = math.floor(bid_px * price_factor)
            if bid_scaled > 0:
                if remaining_short >= cfg.order_size:
                    otype = OrderType.CLOSE_SHORT
                    remaining_short -= cfg.order_size
                else:
                    otype = OrderType.OPEN_LONG
                quotes.append(Quote(Side.BUY, otype, bid_scaled, size_scaled))

        # ----- ask (sell) -----
        would_be = inventory - (k + 1) * cfg.order_size
        if would_be >= -cfg.max_position:
            ask_px = fair_price * (1 + (skew + offset) / 1e4)
            ask_scaled = math.ceil(ask_px * price_factor)
            if remaining_long >= cfg.order_size:
                otype = OrderType.CLOSE_LONG
                remaining_long -= cfg.order_size
            else:
                otype = OrderType.OPEN_SHORT
            quotes.append(Quote(Side.SELL, otype, ask_scaled, size_scaled))

    return quotes


def quotes_match_open_orders(
    quotes: list[Quote],
    open_orders: list[tuple[int, int, int]],  # (order_type, price, size), scaled
    tolerance_scaled: int,
) -> bool:
    """True when the live ladder is within tolerance of the desired one, so
    no re-quote is needed."""
    if len(quotes) != len(open_orders):
        return False
    desired = sorted((q.order_type, q.price, q.size) for q in quotes)
    live = sorted(open_orders)
    for (dt, dp, ds), (lt, lp, ls) in zip(desired, live):
        if dt != lt or ds != ls or abs(dp - lp) > tolerance_scaled:
            return False
    return True
