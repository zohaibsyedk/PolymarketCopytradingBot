"""Market metadata cache (memory + SQLite).

Resolved markets never change, so they are cached forever. Open markets are
refreshed after ``max_age`` seconds because prices, liquidity and status move.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from polycopy.db import Database
from polycopy.gateway.base import MarketDataGateway
from polycopy.models import MarketInfo

log = logging.getLogger(__name__)


class MarketCache:
    def __init__(self, data: MarketDataGateway, db: Database) -> None:
        self.data = data
        self.db = db
        self._mem: dict[str, MarketInfo] = {}

    def _fresh(self, info: MarketInfo, max_age: float) -> bool:
        if info.closed and info.is_resolved():
            return True
        return time.time() - info.fetched_at < max_age

    def _remember(self, info: MarketInfo) -> None:
        key = info.condition_id.lower()
        self._mem[key] = info
        self.db.cache_market(key, info.to_dict(), info.closed and info.is_resolved())

    def peek(self, condition_id: str) -> MarketInfo | None:
        key = condition_id.lower()
        info = self._mem.get(key)
        if info is None:
            row = self.db.cached_market(key)
            if row is not None:
                info = MarketInfo.from_dict(row["data"])
                self._mem[key] = info
        return info

    async def get_many(
        self, condition_ids: Iterable[str], *, max_age: float = 600.0, historical: bool = False
    ) -> dict[str, MarketInfo]:
        out: dict[str, MarketInfo] = {}
        stale: list[str] = []
        for cid in dict.fromkeys(c for c in condition_ids if c):
            info = self.peek(cid)
            if info is not None and self._fresh(info, max_age):
                out[cid] = info
            else:
                stale.append(cid)
        if stale:
            try:
                fetched = await self.data.markets_by_condition(stale, prefer_closed=historical)
            except Exception as error:
                log.warning("Market refresh failed: %s", error)
                fetched = {}
            for cid in stale:
                info = fetched.get(cid) or fetched.get(cid.lower())
                if info is not None:
                    self._remember(info)
                    out[cid] = info
                else:
                    cached = self.peek(cid)
                    if cached is not None:
                        out[cid] = cached
        return out

    async def get(self, condition_id: str, *, max_age: float = 600.0) -> MarketInfo | None:
        return (await self.get_many([condition_id], max_age=max_age)).get(condition_id)

    async def for_token(self, asset_id: str, *, max_age: float = 600.0) -> MarketInfo | None:
        condition_id = self.db.condition_for_token(asset_id)
        if condition_id:
            return await self.get(condition_id, max_age=max_age)
        try:
            info = await self.data.market_for_token(asset_id)
        except Exception as error:
            log.warning("Market lookup by token failed: %s", error)
            return None
        if info is not None and info.condition_id:
            self._remember(info)
        return info
