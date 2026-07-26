"""Bot configuration: YAML file + environment for secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import protocol


@dataclass
class StrategyConfig:
    """Per-market quoting parameters. All *_bps values are basis points."""

    symbol: str = "BTC"
    # Half of this spread is applied on each side of the fair price.
    spread_bps: float = 10.0
    # Number of price levels quoted per side.
    levels: int = 2
    # Distance between consecutive levels on the same side.
    level_step_bps: float = 5.0
    # Order size per level, in base units (e.g. BTC).
    order_size: float = 0.001
    # Absolute inventory cap in base units; quoting shrinks/stops beyond it.
    max_position: float = 0.005
    # Quote shift per unit of inventory ratio (position/max_position).
    # Positive values move quotes down when long, up when short.
    inventory_skew_bps: float = 5.0
    # Re-quote when the fair price moved this much since the last quote,
    # or when open orders no longer match the desired ladder.
    requote_bps: float = 2.5
    # Minimum seconds between re-quotes (WS budget is ~120 msg/min).
    min_requote_interval_s: float = 2.0
    # Fair price source: "book_mid", "mark", "last", or "state_mid".
    fair_source: str = "book_mid"
    # Leverage for opening orders, in x (sent to the API in hundredths).
    leverage: float = 2.0
    # Post-only avoids crossing the spread (maker-only quoting).
    post_only: bool = True
    # Orders auto-expire after this many blocks (safety if the bot dies).
    order_ttl_blocks: int = 600


@dataclass
class BotConfig:
    network: str = "testnet"  # "mainnet" | "testnet"
    dry_run: bool = True
    strategies: list[StrategyConfig] = field(default_factory=lambda: [StrategyConfig()])

    @property
    def rest_url(self) -> str:
        return protocol.MAINNET_REST if self.network == "mainnet" else protocol.TESTNET_REST

    @property
    def ws_url(self) -> str:
        return protocol.MAINNET_WS if self.network == "mainnet" else protocol.TESTNET_WS

    @property
    def chain_id(self) -> int:
        return (
            protocol.MAINNET_CHAIN_ID
            if self.network == "mainnet"
            else protocol.TESTNET_CHAIN_ID
        )


def load_config(path: str | Path) -> BotConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    strategies = [
        StrategyConfig(**raw) for raw in data.get("markets", [])
    ] or [StrategyConfig()]
    network = data.get("network", "testnet")
    if network not in ("mainnet", "testnet"):
        raise ValueError(f"network must be 'mainnet' or 'testnet', got {network!r}")
    return BotConfig(
        network=network,
        dry_run=bool(data.get("dry_run", True)),
        strategies=strategies,
    )
