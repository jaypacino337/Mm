"""Multi-venue configuration: YAML file + environment for secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class MarketConfig:
    """Per-market quoting parameters. All *_bps values are basis points."""

    symbol: str
    spread_bps: float = 10.0
    levels: int = 2
    level_step_bps: float = 5.0
    order_size: float = 0.001
    max_position: float = 0.005
    inventory_skew_bps: float = 5.0
    requote_bps: float = 2.5
    min_requote_interval_s: float = 2.0
    fair_source: str = "book_mid"
    leverage: float = 2.0
    post_only: bool = True
    # Order lifetime safety net (venue-dependent unit: blocks on Perpl,
    # seconds elsewhere).
    order_ttl: int = 600
    # Venue-specific settings (e.g. Phoenix market_pubkey, Variational
    # instrument, Arcus market_id, spot target inventory).
    venue: dict[str, Any] = field(default_factory=dict)


@dataclass
class VenueConfig:
    name: str
    markets: list[MarketConfig]
    # Venue-level settings (network, rpc_url, base_url, ...).
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class BotConfig:
    dry_run: bool = True
    venues: list[VenueConfig] = field(default_factory=list)


def load_config(path: str | Path) -> BotConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    venues: list[VenueConfig] = []
    for name, vraw in (data.get("venues") or {}).items():
        vraw = dict(vraw or {})
        markets_raw = vraw.pop("markets", None) or []
        markets = []
        for mraw in markets_raw:
            mraw = dict(mraw)
            venue_extra = mraw.pop("venue", {})
            known = {k: v for k, v in mraw.items() if k in MarketConfig.__dataclass_fields__}
            unknown = {k: v for k, v in mraw.items() if k not in MarketConfig.__dataclass_fields__}
            markets.append(MarketConfig(**known, venue={**unknown, **venue_extra}))
        if not markets:
            raise ValueError(f"venue {name!r} has no markets configured")
        venues.append(VenueConfig(name=name, markets=markets, settings=vraw))
    if not venues:
        raise ValueError("config has no venues; add a 'venues:' section")
    return BotConfig(dry_run=bool(data.get("dry_run", True)), venues=venues)
