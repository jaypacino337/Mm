from mmbot.core.venue import DesiredQuote, Side
from mmbot.venues.perpl.adapter import order_type_for
from mmbot.venues.perpl.protocol import OrderType, scale, unscale


def q(side, reduce_only):
    return DesiredQuote(side=side, price=100.0, size=1.0, reduce_only=reduce_only)


def test_order_type_mapping():
    assert order_type_for(q(Side.BUY, False)) == OrderType.OPEN_LONG
    assert order_type_for(q(Side.BUY, True)) == OrderType.CLOSE_SHORT
    assert order_type_for(q(Side.SELL, False)) == OrderType.OPEN_SHORT
    assert order_type_for(q(Side.SELL, True)) == OrderType.CLOSE_LONG


def test_scaling_roundtrip():
    assert scale(94952.5, 1) == 949525
    assert unscale(949525, 1) == 94952.5
    assert scale(0.001, 5) == 100
    assert unscale(100, 5) == 0.001
