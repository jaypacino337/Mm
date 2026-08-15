import json

from mmbot.core.config import MarketConfig, VenueConfig
from mmbot.venues.arcus import ArcusVenue
from mmbot.venues.perpl.auth import load_private_key


SEED = bytes(range(32))


def make_venue(dry_run=True):
    vc = VenueConfig(
        name="arcus",
        markets=[MarketConfig(symbol="BTC-PERP", venue={"market_id": 1})],
        settings={"address": "0xabc"},
    )
    return ArcusVenue(vc, dry_run)


def test_signing_message_is_deterministic_and_verifiable():
    venue = make_venue()
    venue._key = load_private_key(SEED.hex())
    body = {"b": 2, "a": 1}
    msg1 = venue._signing_message(1700000000000, body)
    msg2 = venue._signing_message(1700000000000, {"a": 1, "b": 2})
    assert msg1 == msg2  # key order must not matter
    assert msg1.startswith(b"1700000000000")
    canonical = json.loads(msg1[len(b"1700000000000"):])
    assert canonical == {"a": 1, "b": 2}
    sig = venue._key.sign(msg1)
    assert len(sig.hex()) == 128  # matches Arcus' 128-hex signature examples
    venue._key.public_key().verify(sig, msg1)


def test_path_defaults_and_overrides():
    venue = make_venue()
    assert venue.paths["orderbook"] == "/v1/orderbook/{market_id}"
    vc = VenueConfig(
        name="arcus",
        markets=[MarketConfig(symbol="BTC-PERP", venue={"market_id": 1})],
        settings={"paths": {"orderbook": "/v2/book/{market_id}"}},
    )
    venue2 = ArcusVenue(vc, True)
    assert venue2.paths["orderbook"] == "/v2/book/{market_id}"
    assert venue2.paths["orders"] == "/v1/orders"
