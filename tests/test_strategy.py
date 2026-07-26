from perpl_mm.config import StrategyConfig
from perpl_mm.protocol import OrderType, Side
from perpl_mm.rest import MarketInfo
from perpl_mm.strategy import compute_quotes, quotes_match_open_orders


MARKET = MarketInfo(market_id=1, symbol="BTC", price_decimals=1, size_decimals=5)


def cfg(**overrides) -> StrategyConfig:
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
    return StrategyConfig(**base)


def test_flat_inventory_symmetric_ladder():
    quotes = compute_quotes(cfg(), MARKET, fair_price=95000.0, inventory=0.0)
    bids = [q for q in quotes if q.side == Side.BUY]
    asks = [q for q in quotes if q.side == Side.SELL]
    assert len(bids) == 2 and len(asks) == 2
    # ±5bps of 95000 = ±47.5 → bid floor 949525 (scaled by 10^1), ask ceil 950475
    assert bids[0].price == 949525
    assert asks[0].price == 950475
    # level 2 at ±10bps
    assert bids[1].price < bids[0].price
    assert asks[1].price > asks[0].price
    # flat book opens positions on both sides
    assert all(q.order_type == OrderType.OPEN_LONG for q in bids)
    assert all(q.order_type == OrderType.OPEN_SHORT for q in asks)
    # size scaled by 10^5
    assert all(q.size == 100 for q in quotes)


def test_long_inventory_skews_down_and_sells_close():
    flat = compute_quotes(cfg(), MARKET, 95000.0, inventory=0.0)
    long = compute_quotes(cfg(), MARKET, 95000.0, inventory=0.002)
    flat_bid = max(q.price for q in flat if q.side == Side.BUY)
    long_bid = max(q.price for q in long if q.side == Side.BUY)
    assert long_bid < flat_bid  # skew moves quotes down when long
    sells = [q for q in long if q.side == Side.SELL]
    # 0.002 long → first two sell levels reduce (CloseLong)
    assert [q.order_type for q in sells] == [OrderType.CLOSE_LONG, OrderType.CLOSE_LONG]


def test_short_inventory_buys_close_first():
    quotes = compute_quotes(cfg(), MARKET, 95000.0, inventory=-0.001)
    buys = [q for q in quotes if q.side == Side.BUY]
    assert buys[0].order_type == OrderType.CLOSE_SHORT
    assert buys[1].order_type == OrderType.OPEN_LONG


def test_position_cap_drops_growing_side():
    quotes = compute_quotes(cfg(), MARKET, 95000.0, inventory=0.005)
    assert all(q.side == Side.SELL for q in quotes)  # at +max: no more bids
    quotes = compute_quotes(cfg(levels=3), MARKET, 95000.0, inventory=0.004)
    bids = [q for q in quotes if q.side == Side.BUY]
    assert len(bids) == 1  # only one more 0.001 lot fits under the 0.005 cap


def test_zero_fair_or_size_yields_no_quotes():
    assert compute_quotes(cfg(), MARKET, 0.0, 0.0) == []
    assert compute_quotes(cfg(order_size=0), MARKET, 95000.0, 0.0) == []


def test_quotes_match_open_orders_tolerance():
    quotes = compute_quotes(cfg(levels=1), MARKET, 95000.0, 0.0)
    live = [(int(q.order_type), q.price, q.size) for q in quotes]
    assert quotes_match_open_orders(quotes, live, tolerance_scaled=0)
    # nudge one price within tolerance
    nudged = [(t, p + 3, s) for t, p, s in live]
    assert quotes_match_open_orders(quotes, nudged, tolerance_scaled=5)
    assert not quotes_match_open_orders(quotes, nudged, tolerance_scaled=2)
    # missing order
    assert not quotes_match_open_orders(quotes, live[:-1], tolerance_scaled=5)
