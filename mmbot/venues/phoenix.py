"""Phoenix venue adapter — spot CLOB on Solana, via the official
`phoenix-trade` SDK (https://github.com/Ellipsis-Labs/phoenix-sdk).

Install with: pip install "mmbot[phoenix]"  (or: pip install phoenix-trade)

Venue settings:
    rpc_url:      Solana RPC endpoint (default mainnet-beta public RPC)
    keypair_path: path to a solana-cli style JSON keypair file (live only)

Per-market venue settings:
    market_pubkey:    Phoenix market address (required)
    target_inventory: base-token holdings considered "flat" (default 0);
                      inventory = actual base balance - target_inventory.

Spot notes: reduce_only has no meaning on spot and is ignored. Orders are
placed post-only with a unix-timestamp TTL (order_ttl seconds).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..core.config import MarketConfig, VenueConfig
from ..core.venue import DesiredQuote, MarketSpecs, Side, Venue

log = logging.getLogger(__name__)

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


class PhoenixVenue(Venue):
    name = "phoenix"

    def __init__(self, config: VenueConfig, dry_run: bool):
        super().__init__(dry_run)
        try:
            from phoenix.client import PhoenixClient  # noqa: F401
            from solders.pubkey import Pubkey  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Phoenix support requires the official SDK: pip install phoenix-trade"
            ) from exc
        self.config = config
        self.market_cfgs: dict[str, MarketConfig] = {m.symbol: m for m in config.markets}
        self.rpc_url = config.settings.get("rpc_url", DEFAULT_RPC)
        self.keypair_path = config.settings.get("keypair_path")
        self.client: Any = None
        self.signer: Any = None
        self.pubkeys: dict[str, Any] = {}  # symbol -> market Pubkey
        self._specs: dict[str, MarketSpecs] = {}
        self._books: dict[str, Any] = {}  # symbol -> latest UI ladder

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        from phoenix.client import PhoenixClient
        from solders.pubkey import Pubkey

        self.client = PhoenixClient(custom_url=self.rpc_url)
        for cfg in self.config.markets:
            addr = cfg.venue.get("market_pubkey")
            if not addr:
                raise ValueError(f"phoenix market {cfg.symbol!r} needs venue.market_pubkey")
            pubkey = Pubkey.from_string(str(addr))
            await self.client.add_market(pubkey)
            self.pubkeys[cfg.symbol] = pubkey
            meta = self.client.markets[pubkey]
            # One tick / one base lot converted to UI units via MarketMetadata.
            tick = float(meta.ticks_to_float_price(1))
            step = float(meta.base_lots_to_raw_base_units_as_float(1))
            self._specs[cfg.symbol] = MarketSpecs(cfg.symbol, tick_size=tick, size_step=step)
            log.info("phoenix %s: tick=%g step=%g", cfg.symbol, tick, step)
        if not self.dry_run:
            if not self.keypair_path:
                raise RuntimeError("phoenix live trading needs settings.keypair_path")
            from solders.keypair import Keypair

            raw = json.loads(Path(self.keypair_path).expanduser().read_text())
            self.signer = Keypair.from_bytes(bytes(raw))
            log.info("phoenix signer: %s", self.signer.pubkey())

    async def stop(self) -> None:
        if self.signer is not None:
            for symbol, pubkey in self.pubkeys.items():
                try:
                    await self.client.cancel_all_orders(self.signer, pubkey)
                    log.info("phoenix %s: cancelled all orders", symbol)
                except Exception:  # noqa: BLE001
                    log.exception("phoenix %s: cancel-all failed", symbol)
        if self.client is not None:
            await self.client.close()

    # ----------------------------------------------------------- primitives

    def specs(self, symbol: str) -> MarketSpecs:
        return self._specs[symbol]

    async def fair_price(self, symbol: str, source: str) -> float | None:
        ladder = await self.client.get_l2_book(self.pubkeys[symbol])
        self._books[symbol] = ladder
        bids = getattr(ladder, "bids", None) or []
        asks = getattr(ladder, "asks", None) or []
        best_bid = float(bids[0].price) if bids else None
        best_ask = float(asks[0].price) if asks else None
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / 2
        return best_bid or best_ask

    async def inventory(self, symbol: str) -> float:
        """Base-token balance relative to the configured target.

        Dry-run (no signer) always reports flat.
        """
        cfg = self.market_cfgs[symbol]
        target = float(cfg.venue.get("target_inventory", 0.0))
        if self.signer is None:
            return 0.0
        meta = self.client.markets[self.pubkeys[symbol]]
        base_mint = meta.base_mint
        from solana.rpc.types import TokenAccountOpts

        resp = await self.client.client.get_token_accounts_by_owner_json_parsed(
            self.signer.pubkey(), TokenAccountOpts(mint=base_mint)
        )
        balance = 0.0
        for acct in resp.value:
            amount = acct.account.data.parsed["info"]["tokenAmount"]["uiAmount"]
            balance += float(amount or 0)
        return balance - target

    async def replace_quotes(self, symbol: str, quotes: list[DesiredQuote]) -> None:
        from phoenix.client import ExecutableOrder
        from phoenix.types.side import Ask, Bid

        cfg = self.market_cfgs[symbol]
        pubkey = self.pubkeys[symbol]
        await self.client.cancel_all_orders(self.signer, pubkey)
        if not quotes:
            return
        orders = []
        expiry = int(time.time()) + cfg.order_ttl
        for q in quotes:
            packet = self.client.get_post_only_order_packet(
                pubkey,
                Bid() if q.side == Side.BUY else Ask(),
                price_in_quote_units=q.price,
                size_in_base_units=q.size,
                last_valid_unix_timestamp=expiry,
                fail_silently_on_insufficient_funds=True,
            )
            orders.append(ExecutableOrder(packet, pubkey))
        await self.client.send_orders(self.signer, orders)
