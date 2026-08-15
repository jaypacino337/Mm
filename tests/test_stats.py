from mmbot.core.stats import StatsTracker
from mmbot.core.venue import Side


def test_volume_and_fill_count():
    t = StatsTracker(path=None)
    t.record_fill("perpl", "BTC", Side.BUY, 100.0, 2.0)
    t.record_fill("perpl", "BTC", Side.SELL, 101.0, 1.0)
    assert t.total_volume == 100 * 2 + 101 * 1
    assert t.total_fills == 2


def test_avg_cost_realized_pnl_long():
    t = StatsTracker(path=None)
    t.record_fill("v", "X", Side.BUY, 100.0, 1.0)
    t.record_fill("v", "X", Side.BUY, 110.0, 1.0)  # avg 105
    t.record_fill("v", "X", Side.SELL, 108.0, 1.0)  # +3
    book = t.books[("v", "X")]
    assert book.realized_pnl == 3.0
    assert book.position == 1.0
    assert book.avg_price == 105.0


def test_realized_pnl_short_side():
    t = StatsTracker(path=None)
    t.record_fill("v", "X", Side.SELL, 100.0, 2.0)
    t.record_fill("v", "X", Side.BUY, 95.0, 2.0)  # +5 * 2
    book = t.books[("v", "X")]
    assert book.realized_pnl == 10.0
    assert book.position == 0.0
    assert book.avg_price == 0.0


def test_flip_through_zero():
    t = StatsTracker(path=None)
    t.record_fill("v", "X", Side.BUY, 100.0, 1.0)
    t.record_fill("v", "X", Side.SELL, 104.0, 3.0)  # close +4, open short 2 @ 104
    book = t.books[("v", "X")]
    assert book.realized_pnl == 4.0
    assert book.position == -2.0
    assert book.avg_price == 104.0
    t.record_fill("v", "X", Side.BUY, 100.0, 2.0)  # +4 * 2
    assert book.realized_pnl == 12.0
    assert book.position == 0.0


def test_books_are_per_venue_and_symbol():
    t = StatsTracker(path=None)
    t.record_fill("perpl", "BTC", Side.BUY, 100.0, 1.0)
    t.record_fill("lighter", "BTC", Side.BUY, 100.0, 1.0)
    assert len(t.books) == 2
    snap = t.snapshot()
    assert set(snap["books"]) == {"perpl:BTC", "lighter:BTC"}


def test_snapshot_and_json(tmp_path):
    p = tmp_path / "stats.json"
    t = StatsTracker(path=p)
    t.record_fill("v", "X", Side.BUY, 50.0, 2.0, fee=0.01)
    t.log_summary()
    import json

    data = json.loads(p.read_text())
    assert data["volume_quote"] == 100.0
    assert data["fees"] == 0.01
    assert data["fills"] == 1
