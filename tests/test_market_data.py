from mmbot.venues.perpl.market_data import OrderBook


def test_book_snapshot_and_update():
    book = OrderBook(market_id=1)
    book.apply(
        {"bid": [{"p": 949900, "s": 10, "o": 2}], "ask": [{"p": 950100, "s": 5, "o": 1}]},
        snapshot=True,
    )
    assert book.best_bid() == 949900
    assert book.best_ask() == 950100
    assert book.mid() == 950000

    # o: 0 removes a level; new level replaces
    book.apply(
        {"bid": [{"p": 949900, "s": 0, "o": 0}, {"p": 949800, "s": 7, "o": 1}]},
        snapshot=False,
    )
    assert book.best_bid() == 949800

    # snapshot resets state
    book.apply({"bid": [{"p": 940000, "s": 1, "o": 1}], "ask": []}, snapshot=True)
    assert book.best_bid() == 940000
    assert book.best_ask() is None
    assert book.mid() is None
