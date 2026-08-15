import pytest

from mmbot.core.config import load_config


def test_example_config_loads():
    cfg = load_config("config.example.yaml")
    assert cfg.dry_run is True
    names = [v.name for v in cfg.venues]
    assert names == ["perpl", "lighter", "phoenix", "variational", "arcus"]
    phoenix = next(v for v in cfg.venues if v.name == "phoenix")
    m = phoenix.markets[0]
    assert m.symbol == "SOL/USDC"
    assert m.venue["market_pubkey"]
    assert phoenix.settings["rpc_url"].startswith("https://")
    arcus = next(v for v in cfg.venues if v.name == "arcus")
    assert arcus.markets[0].venue["market_id"] == 1


def test_empty_config_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("dry_run: true\n")
    with pytest.raises(ValueError, match="no venues"):
        load_config(p)
    p.write_text("venues:\n  perpl: {network: testnet}\n")
    with pytest.raises(ValueError, match="no markets"):
        load_config(p)
