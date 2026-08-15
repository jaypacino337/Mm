"""Venue adapter registry."""

from __future__ import annotations

from ..core.config import VenueConfig
from ..core.venue import Venue


def create_venue(config: VenueConfig, dry_run: bool) -> Venue:
    name = config.name.lower()
    if name == "perpl":
        from .perpl.adapter import PerplVenue

        return PerplVenue(config, dry_run)
    if name == "phoenix":
        from .phoenix import PhoenixVenue

        return PhoenixVenue(config, dry_run)
    if name == "variational":
        from .variational import VariationalVenue

        return VariationalVenue(config, dry_run)
    if name == "arcus":
        from .arcus import ArcusVenue

        return ArcusVenue(config, dry_run)
    raise ValueError(f"unknown venue {config.name!r} (perpl, phoenix, variational, arcus)")
