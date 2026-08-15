"""Venue-neutral quote-ladder math, in human units. Pure and unit-tested.

Bids round down to the tick, asks round up, sizes round down to the step,
so quantization never makes a quote more aggressive than intended.
"""

from __future__ import annotations

import math

from .config import MarketConfig
from .venue import DesiredQuote, LiveQuote, MarketSpecs, Side


def quantize_price(price: float, tick: float, side: Side) -> float:
    if tick <= 0:
        return price
    ticks = price / tick
    n = math.floor(ticks + 1e-9) if side == Side.BUY else math.ceil(ticks - 1e-9)
    return round(n * tick, 12)


def quantize_size(size: float, step: float) -> float:
    if step <= 0:
        return size
    return round(math.floor(size / step + 1e-9) * step, 12)


def compute_quotes(
    cfg: MarketConfig,
    specs: MarketSpecs,
    fair_price: float,
    inventory: float,
) -> list[DesiredQuote]:
    """Build the desired ladder around `fair_price`.

    - Quotes skew away from inventory (long → both sides shift down).
    - The side that would push inventory past max_position is dropped.
    - Quotes that reduce existing inventory are flagged reduce_only.
    """
    if fair_price <= 0 or cfg.levels <= 0:
        return []
    size = quantize_size(cfg.order_size, specs.size_step)
    if size <= 0 or size < specs.min_size:
        return []

    ratio = 0.0
    if cfg.max_position > 0:
        ratio = max(-1.0, min(1.0, inventory / cfg.max_position))
    skew = -ratio * cfg.inventory_skew_bps
    half_spread = cfg.spread_bps / 2

    quotes: list[DesiredQuote] = []
    reducible_short = max(0.0, -inventory)  # reduced by buying
    reducible_long = max(0.0, inventory)  # reduced by selling

    for k in range(cfg.levels):
        offset = half_spread + k * cfg.level_step_bps

        if inventory + (k + 1) * size <= cfg.max_position:
            bid = quantize_price(fair_price * (1 + (skew - offset) / 1e4), specs.tick_size, Side.BUY)
            if bid > 0:
                reduce = reducible_short >= size
                if reduce:
                    reducible_short -= size
                quotes.append(DesiredQuote(Side.BUY, bid, size, reduce))

        if inventory - (k + 1) * size >= -cfg.max_position:
            ask = quantize_price(fair_price * (1 + (skew + offset) / 1e4), specs.tick_size, Side.SELL)
            reduce = reducible_long >= size
            if reduce:
                reducible_long -= size
            quotes.append(DesiredQuote(Side.SELL, ask, size, reduce))

    return quotes


def quotes_match(
    desired: list[DesiredQuote],
    live: list[LiveQuote],
    price_tol: float,
    size_rel_tol: float = 0.05,
) -> bool:
    """True when the live ladder is close enough to skip a re-quote."""
    if len(desired) != len(live):
        return False
    d = sorted(desired, key=lambda q: (q.side.value, q.price))
    l = sorted(live, key=lambda q: (q.side.value, q.price))
    for dq, lq in zip(d, l):
        if dq.side != lq.side:
            return False
        if abs(dq.price - lq.price) > price_tol:
            return False
        if dq.size > 0 and abs(dq.size - lq.size) / dq.size > size_rel_tol:
            return False
    return True


def maker_bid_ask(mid: float, spread_bps: float, skew_bps: float = 0.0) -> tuple[float, float]:
    """Two-sided quote around a mid — used by RFQ venues (Variational)."""
    shift = skew_bps / 1e4
    half = spread_bps / 2e4
    return mid * (1 + shift - half), mid * (1 + shift + half)
