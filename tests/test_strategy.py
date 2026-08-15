from mmbot.core.config import MarketConfig
from mmbot.core.strategy import (
    compute_quotes,
    maker_bid_ask,
    quantize_price,
    quantize_size,
    quotes_match,
)
from mmbot.core.venue import LiveQuote, MarketSpecs, Side


SPECS = MarketSpecs(symbol="BTC", tick_size=0.1, size_step=0.00001)


def cfg(**overrides) -> MarketConfig:
    base = dict(
        symbol="BTC",
        spread_bps=10,
        levels=2,
        level_step_bps=5,
        order_size=0.001,
        max_position=0.005,
        inventory_skew_bps=5,
    )
    base.update(overrides)
    return MarketConfig(**base)


def bids(quotes):
    return [q for q in quotes if q.side == Side.BUY]


def asks(quotes):
    return [q for q in quotes if q.side == Side.SELL]


def test_quantize_price_never_more_aggressive():
    assert quantize_price(94952.5, 0.1, Side.BUY) == 94952.5
    assert quantize_price(94952.54, 0.1, Side.BUY) == 94952.5  # bid rounds down
    assert quantize_price(95047.51, 0.1, Side.SELL) == 95047.6  # ask rounds up


def test_quantize_size_rounds_down():
    assert quantize_size(0.00123, 0.0001) == 0.0012
    assert quantize_size(0.5, 0) == 0.5


def test_flat_inventory_symmetric_ladder():
    quotes = compute_quotes(cfg(), SPECS, fair_price=95000.0, inventory=0.0)
    b, a = bids(quotes), asks(quotes)
    assert len(b) == 2 and len(a) == 2
    # ±5bps of 95000 = ±47.5
    assert b[0].price == 94952.5
    assert a[0].price == 95047.5
    assert b[1].price < b[0].price and a[1].price > a[0].price
    assert not any(q.reduce_only for q in quotes)
    assert all(q.size == 0.001 for q in quotes)


def test_long_inventory_skews_down_and_reduces():
    flat = compute_quotes(cfg(), SPECS, 95000.0, 0.0)
    long = compute_quotes(cfg(), SPECS, 95000.0, 0.002)
    assert max(q.price for q in bids(long)) < max(q.price for q in bids(flat))
    assert [q.reduce_only for q in asks(long)] == [True, True]
    assert [q.reduce_only for q in bids(long)] == [False, False]


def test_short_inventory_reduces_on_buys():
    quotes = compute_quotes(cfg(), SPECS, 95000.0, -0.001)
    assert [q.reduce_only for q in bids(quotes)] == [True, False]


def test_position_cap_drops_growing_side():
    quotes = compute_quotes(cfg(), SPECS, 95000.0, 0.005)
    assert bids(quotes) == []
    quotes = compute_quotes(cfg(levels=3), SPECS, 95000.0, 0.004)
    assert len(bids(quotes)) == 1


def test_degenerate_inputs():
    assert compute_quotes(cfg(), SPECS, 0.0, 0.0) == []
    assert compute_quotes(cfg(order_size=0), SPECS, 95000.0, 0.0) == []
    tiny = MarketSpecs("X", tick_size=0.1, size_step=0.01, min_size=0.01)
    assert compute_quotes(cfg(order_size=0.001), tiny, 95000.0, 0.0) == []


def test_quotes_match_tolerances():
    desired = compute_quotes(cfg(levels=1), SPECS, 95000.0, 0.0)
    live = [LiveQuote(q.side, q.price, q.size) for q in desired]
    assert quotes_match(desired, live, price_tol=0.05)
    nudged = [LiveQuote(q.side, q.price + 0.3, q.size) for q in desired]
    assert quotes_match(desired, nudged, price_tol=0.5)
    assert not quotes_match(desired, nudged, price_tol=0.2)
    assert not quotes_match(desired, live[:-1], price_tol=0.5)
    resized = [LiveQuote(q.side, q.price, q.size * 2) for q in desired]
    assert not quotes_match(desired, resized, price_tol=0.5)


def test_maker_bid_ask():
    bid, ask = maker_bid_ask(100.0, spread_bps=20)
    assert bid == 100 * (1 - 0.001) and ask == 100 * (1 + 0.001)
    bid2, ask2 = maker_bid_ask(100.0, spread_bps=20, skew_bps=-10)
    assert bid2 < bid and ask2 < ask
