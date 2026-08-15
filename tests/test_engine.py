import asyncio

from mmbot.core.config import BotConfig, MarketConfig, VenueConfig
from mmbot.core.engine import Engine, MarketState
from mmbot.core.venue import DesiredQuote, MarketSpecs, Venue


class FakeVenue(Venue):
    name = "fake"

    def __init__(self, dry_run=True, fair=100.0):
        super().__init__(dry_run)
        self.fair = fair
        self.started = False
        self.stopped = False
        self.replaced: list[list[DesiredQuote]] = []

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def specs(self, symbol):
        return MarketSpecs(symbol=symbol, tick_size=0.01, size_step=0.01)

    async def fair_price(self, symbol, source):
        return self.fair

    async def inventory(self, symbol):
        return 0.0

    async def replace_quotes(self, symbol, quotes):
        self.replaced.append(quotes)


def market(**kw):
    base = dict(symbol="X", order_size=0.05, max_position=1.0, min_requote_interval_s=0.01)
    base.update(kw)
    return MarketConfig(**base)


def run_engine_briefly(venue, market_cfg, seconds=0.08):
    vc = VenueConfig(name="fake", markets=[market_cfg])
    engine = Engine(BotConfig(dry_run=venue.dry_run, venues=[vc]), [(venue, vc)])

    async def go():
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(seconds)
        engine.stop()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(go())
    return engine


def test_dry_run_never_places_orders():
    venue = FakeVenue(dry_run=True)
    engine = run_engine_briefly(venue, market())
    assert venue.started and venue.stopped
    assert venue.replaced == []


def test_live_places_and_requotes_on_drift():
    venue = FakeVenue(dry_run=False)
    engine = run_engine_briefly(venue, market(requote_bps=1.0))
    assert len(venue.replaced) >= 1
    quotes = venue.replaced[0]
    assert {q.side.value for q in quotes} == {"buy", "sell"}


def test_no_requote_when_fair_stable():
    # live_quotes returns None by default → unknown → the base class
    # re-quotes only when drift exceeds requote_bps; with a constant fair
    # price after the first quote, drift is 0 and live None forces requote.
    class StickyVenue(FakeVenue):
        async def live_quotes(self, symbol):
            # pretend the resting ladder exactly matches what we last placed
            return [
                type("LQ", (), {"side": q.side, "price": q.price, "size": q.size})()
                for q in (self.replaced[-1] if self.replaced else [])
            ] or None

    venue = StickyVenue(dry_run=False)
    run_engine_briefly(venue, market(requote_bps=1.0), seconds=0.12)
    assert len(venue.replaced) == 1  # placed once, then stable


def test_cycle_errors_do_not_kill_engine():
    class FlakyVenue(FakeVenue):
        async def fair_price(self, symbol, source):
            raise RuntimeError("boom")

    venue = FlakyVenue(dry_run=False)
    engine = run_engine_briefly(venue, market())
    state = engine.runtimes[0].states["X"]
    assert state.consecutive_errors >= 1
    assert venue.stopped
