"""Finds and ranks the wallets worth copying.

1. Pull the PnL leaderboards (overall and per category) for several windows.
2. For every candidate download the last ``lookback_days`` of trades.
3. Replay them through :func:`simulate_copy` with copy slippage and fees.
4. Follow the best-scoring wallets, with hysteresis so the set does not churn,
   and bench leaders whose *live* copied results disappoint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from polycopy.config import Settings
from polycopy.db import Database
from polycopy.engine.markets import MarketCache
from polycopy.engine.rules import EntryRules
from polycopy.engine.scoring import SimParams, simulate_copy
from polycopy.gateway.base import MarketDataGateway
from polycopy.models import LeaderboardRow

log = logging.getLogger(__name__)

DAY = 86400.0


def sim_params_from_settings(settings: Settings) -> SimParams:
    d = settings.discovery
    return SimParams(
        stake_usdc=d.sim_stake_usdc,
        slippage=d.sim_slippage_cents,
        lookback_days=d.lookback_days,
        min_resolved_positions=d.min_resolved_positions,
        max_trades_per_day=d.max_trades_per_day,
        min_short_horizon_share=d.min_short_horizon_share,
        rules=EntryRules.from_settings(settings.filters),
    )


class Discovery:
    def __init__(
        self,
        data: MarketDataGateway,
        db: Database,
        markets: MarketCache,
        settings: Callable[[], Settings],
        emit: Callable[[str, dict[str, Any]], None],
        live_stats: Callable[[str], dict[str, float]] | None = None,
    ) -> None:
        self.data = data
        self.db = db
        self.markets = markets
        self.settings = settings
        self.emit = emit
        self.live_stats = live_stats
        self.progress: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "done": 0,
            "total": 0,
            "started_at": None,
            "finished_at": self.db.kv_get("discovery_finished_at"),
            "error": None,
        }
        self._lock = asyncio.Lock()

    def _set(self, **values: Any) -> None:
        self.progress.update(values)
        self.emit("discovery", dict(self.progress))

    async def run(self) -> dict[str, Any]:
        if self._lock.locked():
            return self.progress
        async with self._lock:
            settings = self.settings()
            self._set(running=True, phase="leaderboards", done=0, total=0,
                      started_at=time.time(), error=None)
            try:
                candidates = await self._candidates(settings)
                self._set(phase="scoring", total=len(candidates))
                await self._score_all(candidates, settings)
                self._set(phase="selecting")
                followed = self.select_followed(settings)
                finished = time.time()
                self.db.kv_set("discovery_finished_at", finished)
                self._set(running=False, phase="idle", finished_at=finished)
                self.db.log("info", "discovery",
                            f"Discovery finished: {len(candidates)} wallets scored, "
                            f"{len(followed)} followed")
            except Exception as error:  # keep the bot alive on API trouble
                log.exception("Discovery failed")
                self._set(running=False, phase="idle", error=str(error))
                self.db.log("error", "discovery", f"Discovery failed: {error}")
            return self.progress

    async def _candidates(self, settings: Settings) -> list[LeaderboardRow]:
        d = settings.discovery
        categories: list[str | None] = [None] + list(d.leaderboard_categories)
        rows: dict[str, LeaderboardRow] = {}
        for window in d.leaderboard_windows:
            for category in categories:
                try:
                    board = await self.data.leaderboard(
                        window=window, category=category, limit=d.leaderboard_depth
                    )
                except Exception as error:
                    self.db.log("warning", "discovery",
                                f"Leaderboard {window}/{category or 'all'} failed: {error}")
                    continue
                for row in board:
                    if row.pnl <= 0:
                        continue
                    best = rows.get(row.wallet)
                    if best is None or row.rank < best.rank:
                        rows[row.wallet] = row
        blocked = {t["wallet"] for t in self.db.traders("blocked")}
        # Always rescore wallets we already follow or that were added by hand.
        for trader in self.db.query(
            "SELECT * FROM traders WHERE status IN ('followed','benched') OR manual=1"
        ):
            if trader["wallet"] not in rows:
                rows[trader["wallet"]] = LeaderboardRow(
                    wallet=trader["wallet"], rank=10**6, pnl=0.0, volume=0.0,
                    name=trader.get("name"), profile_image=trader.get("profile_image"),
                )
        candidates = [r for w, r in rows.items() if w not in blocked]
        candidates.sort(key=lambda r: r.rank)
        candidates = candidates[: d.max_candidates]
        for row in candidates:
            existing = self.db.get_trader(row.wallet)
            values: dict[str, Any] = {}
            if row.name and (existing is None or not existing.get("name")):
                values["name"] = row.name
            if row.profile_image:
                values["profile_image"] = row.profile_image
            if row.window:
                board = dict((existing or {}).get("leaderboard") or {})
                board[f"{row.window}:{row.category or 'all'}"] = {
                    "rank": row.rank, "pnl": row.pnl, "volume": row.volume,
                }
                values["leaderboard"] = board
            self.db.upsert_trader(row.wallet, values)
        return candidates

    async def _score_all(self, candidates: list[LeaderboardRow], settings: Settings) -> None:
        params = sim_params_from_settings(settings)
        since = time.time() - settings.discovery.lookback_days * DAY
        semaphore = asyncio.Semaphore(4)
        done = 0

        async def score(row: LeaderboardRow) -> None:
            nonlocal done
            async with semaphore:
                try:
                    await self.score_wallet(row.wallet, params=params, since=since,
                                            max_trades=settings.discovery.max_trades_per_trader)
                except Exception as error:
                    log.info("Scoring %s failed: %s", row.wallet, error)
                    self.db.upsert_trader(row.wallet, {"note": f"scoring failed: {error}"})
                done += 1
                self._set(done=done)

        await asyncio.gather(*(score(row) for row in candidates))

    async def score_wallet(
        self, wallet: str, *, params: SimParams, since: float, max_trades: int
    ) -> dict[str, Any]:
        trades = await self.data.user_trades(wallet, since_ts=since, max_items=max_trades)
        markets = await self.markets.get_many({t.condition_id for t in trades if t.condition_id},
                                              max_age=3600, historical=True)
        result = simulate_copy(wallet, trades, markets, params)
        metrics = result.metrics.to_dict()
        name = next((t.name for t in reversed(trades) if t.name), None)
        values: dict[str, Any] = {
            "score": result.metrics.score,
            "metrics": metrics,
            "curve": result.curve,
            "last_scored_at": time.time(),
            "note": result.metrics.reason,
        }
        existing = self.db.get_trader(wallet)
        if name and (existing is None or not existing.get("name")):
            values["name"] = name
        self.db.upsert_trader(wallet, values)
        return metrics

    def select_followed(self, settings: Settings) -> list[str]:
        d = settings.discovery
        now = time.time()
        traders = self.db.traders()
        followed: list[str] = []

        # Manual follows always stay followed.
        for t in traders:
            if t["status"] == "followed" and t["manual"]:
                followed.append(t["wallet"])

        def eligible(t: dict[str, Any]) -> bool:
            metrics = t.get("metrics") or {}
            return bool(metrics.get("eligible")) and (t.get("score") or 0) > 0

        # Live feedback: bench auto-followed leaders whose copies lose money.
        if self.live_stats is not None:
            for t in traders:
                if t["status"] != "followed" or t["manual"]:
                    continue
                stats = self.live_stats(t["wallet"])
                if (
                    stats.get("closed", 0) >= d.bench_min_positions
                    and stats.get("roi", 0.0) <= d.bench_live_roi
                ):
                    self.db.upsert_trader(t["wallet"], {
                        "status": "benched",
                        "benched_until": now + 7 * DAY,
                        "note": f"benched: live copy ROI {stats['roi']:.0%} over "
                                f"{stats['closed']} positions",
                    })
                    self.db.log("warning", "discovery",
                                f"Benched {t.get('name') or t['wallet']} after poor live results")
                    t["status"] = "benched"

        ranked = sorted(
            (t for t in traders if t["status"] not in ("blocked",) and not t["manual"]),
            key=lambda t: t.get("score") or 0,
            reverse=True,
        )
        # Keep current auto-follows unless they fall well below the bar (hysteresis).
        keep_floor = max(0.0, d.min_score - 10)
        for t in ranked:
            if len(followed) >= d.max_followed:
                break
            if t["status"] == "followed" and eligible(t) and (t.get("score") or 0) >= keep_floor:
                followed.append(t["wallet"])
        for t in ranked:
            if len(followed) >= d.max_followed:
                break
            if t["wallet"] in followed or not eligible(t):
                continue
            if t["status"] == "benched" and (t.get("benched_until") or 0) > now:
                continue
            if (t.get("score") or 0) >= d.min_score:
                followed.append(t["wallet"])

        followed_set = set(followed)
        for t in traders:
            wallet = t["wallet"]
            if t["status"] == "blocked":
                continue
            if wallet in followed_set:
                if t["status"] != "followed":
                    self.db.upsert_trader(wallet, {"status": "followed", "followed_at": now})
                    self.db.log("info", "discovery",
                                f"Now following {t.get('name') or wallet} (score {t.get('score') or 0:.0f})")
            elif t["status"] == "followed":
                self.db.upsert_trader(wallet, {"status": "candidate"})
                self.db.log("info", "discovery", f"Stopped following {t.get('name') or wallet}")
            elif t["status"] == "benched" and (t.get("benched_until") or 0) <= now:
                self.db.upsert_trader(wallet, {"status": "candidate"})
        self.emit("traders", {"followed": followed})
        return followed
