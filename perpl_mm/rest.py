"""Minimal REST client for Perpl: market context and account history."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .auth import ApiCredentials

log = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    market_id: int
    symbol: str
    price_decimals: int
    size_decimals: int
    initial_margin: int | None = None
    maker_fee: int | None = None
    taker_fee: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def price_tick(self) -> float:
        return 10**-self.price_decimals

    @property
    def size_step(self) -> float:
        return 10**-self.size_decimals


def _first(d: dict, *keys: str, default=None):
    """Fetch the first present key; the context schema uses snake_case in
    docs but be tolerant of camelCase variants."""
    for k in keys:
        if k in d:
            return d[k]
    return default


def parse_markets(context: dict[str, Any]) -> dict[str, MarketInfo]:
    markets: dict[str, MarketInfo] = {}
    for m in context.get("markets", []):
        cfg = m.get("config", m)
        symbol = str(
            _first(m, "symbol", "name", "ticker", default=_first(cfg, "symbol", "name", default="?"))
        )
        info = MarketInfo(
            market_id=int(_first(m, "id", "market_id", "marketId", default=0)),
            symbol=symbol,
            price_decimals=int(_first(cfg, "price_decimals", "priceDecimals", default=0)),
            size_decimals=int(_first(cfg, "size_decimals", "sizeDecimals", default=0)),
            initial_margin=_first(cfg, "initial_margin", "initialMargin"),
            maker_fee=_first(cfg, "maker_fee", "makerFee"),
            taker_fee=_first(cfg, "taker_fee", "takerFee"),
            raw=m,
        )
        markets[symbol.upper()] = info
    return markets


class PerplRest:
    def __init__(
        self,
        base_url: str,
        chain_id: int,
        credentials: ApiCredentials | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.chain_id = chain_id
        self.credentials = credentials
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, signed: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        headers = {}
        if signed:
            if self.credentials is None:
                raise RuntimeError(f"{path} requires API credentials")
            split = urlsplit(url)
            target = split.path + (f"?{split.query}" if split.query else "")
            headers = self.credentials.rest_headers(self.chain_id, "GET", target)
        resp = await self._client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def context(self) -> dict[str, Any]:
        """GET /v1/pub/context — chain, instances, tokens, markets."""
        return await self._get("/v1/pub/context")

    async def markets(self) -> dict[str, MarketInfo]:
        return parse_markets(await self.context())

    async def fills(self, page: str | None = None, count: int = 50) -> Any:
        path = f"/v1/trading/fills?count={count}"
        if page:
            path += f"&page={page}"
        return await self._get(path, signed=True)

    async def order_history(self, page: str | None = None, count: int = 50) -> Any:
        path = f"/v1/trading/order-history?count={count}"
        if page:
            path += f"&page={page}"
        return await self._get(path, signed=True)
