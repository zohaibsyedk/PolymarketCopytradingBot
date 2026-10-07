"""The PolyCopy engine: ties discovery, detection, execution and the ledger together.

States
  stopped  nothing runs (the web terminal still works)
  running  leaders are watched and copied, positions are managed
  paused   leaders are watched, exits are mirrored and positions are managed,
           but no new positions are opened

The bot pauses itself when the daily loss limit or the drawdown limit is hit,
when the wallet is geoblocked or in close-only mode, or when live trading
loses its login.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from polycopy import __version__
from polycopy.config import Settings, SettingsStore
from polycopy.db import Database
from polycopy.engine.discovery import Discovery
from polycopy.engine.executor import Executor, LiveExecutor, PaperExecutor, liquidity_under
from polycopy.engine.markets import MarketCache
from polycopy.engine.portfolio import DUST_SHARES, Portfolio
from polycopy.engine.risk import (
    StakeRequest,
    chase_cap,
    compute_stake,
    daily_loss_exceeded,
    drawdown_exceeded,
)
from polycopy.engine.rules import EntryRules, check_entry
from polycopy.engine.scoring import market_category
from polycopy.engine.watcher import LeaderWatcher, Signal
from polycopy.gateway.base import MarketDataGateway, TradingAccount
from polycopy.models import Fill, GeoStatus, MarketInfo, OrderBook
from polycopy.secrets_store import SecretStore

log = logging.getLogger(__name__)

AccountFactory = Callable[..., Awaitable[tuple[TradingAccount, dict[str, Any]]]]
DAY = 86400.0


class Bot:
    def __init__(
        self,
        *,
        store: SettingsStore,
        db: Database,
        secrets: SecretStore,
        data: MarketDataGateway,
        account_factory: AccountFactory,
        stream=None,
        demo: bool = False,
    ) -> None:
        self.store = store
        self.db = db
        self.secrets = secrets
        self.data = data
        self.account_factory = account_factory
        self.demo = demo
        self.markets = MarketCache(data, db)
        self.portfolio = Portfolio(db)
        self.portfolio.ensure_paper_account(self.settings.paper_starting_balance)
        self.account: TradingAccount | None = None
        self.status = "stopped"
        self.pause_reason: str | None = None
        self.geo: GeoStatus | None = None
        self.login_error: str | None = None
        self.approvals: str | None = None
        self.listeners: set[asyncio.Queue] = set()
        self.queue: asyncio.Queue[Signal] = asyncio.Queue()
        self.discovery = Discovery(
            data, db, self.markets, lambda: self.settings, self.emit,
            live_stats=lambda wallet: self.portfolio.leader_live_stats(self.mode, wallet),
        )
        self.stream = stream
        self.watcher = LeaderWatcher(data, db, lambda: self.settings, self._enqueue_signal,
                                     stream=stream)
        self._tasks: list[asyncio.Task] = []
        self._leader_pos: dict[tuple[str, str], tuple[float, float]] = {}
        self._leader_missing: dict[tuple[str, str], int] = {}
        self._exit_started: dict[int, float] = {}
        self._redeeming: set[str] = set()
        self._entry_lock = asyncio.Lock()
        self._last_snapshot = 0.0
        self._last_reconcile = 0.0
        self._started_at: float | None = None
        self.signals_seen = 0

    # ----------------------------------------------------------- properties
    @property
    def settings(self) -> Settings:
        return self.store.settings

    @property
    def mode(self) -> str:
        return self.settings.mode

    @property
    def logged_in(self) -> bool:
        return self.account is not None

    @property
    def executor(self) -> Executor:
        if self.mode == "live":
            if self.account is None:
                raise RuntimeError("live trading requires a login")
            return LiveExecutor(self.account)
        return PaperExecutor()

    # --------------------------------------------------------------- events
    def emit(self, kind: str, data: Any = None) -> None:
        message = {"type": kind, "data": data, "ts": time.time()}
        for queue in list(self.listeners):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def log(self, level: str, category: str, message: str, data: Any = None) -> None:
        getattr(log, level if level in ("info", "warning", "error", "debug") else "info")(message)
        entry = self.db.log(level, category, message, data)
        self.emit("log", entry)

    # ------------------------------------------------------------------ auth
    async def login(self, *, private_key: str, wallet: str | None, remember: bool) -> dict[str, Any]:
        private_key = private_key.strip()
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        stored = self.secrets.load() or {}
        same_key = stored.get("private_key") == private_key and stored.get("wallet") == (
            wallet or ""
        ).lower()
        account, cache = await self.account_factory(
            private_key=private_key,
            wallet=wallet,
            credentials=stored.get("credentials") if same_key else None,
            builder_key=stored.get("builder_key") if same_key else None,
        )
        if self.account is not None:
            with contextlib.suppress(Exception):
                await self.account.close()
        self.account = account
        self.login_error = None
        if remember:
            self.secrets.save({
                "private_key": private_key,
                "wallet": account.info.wallet,
                "credentials": cache.get("credentials"),
                "builder_key": cache.get("builder_key"),
            })
        self.geo = await self.data.geoblock()
        approvals = getattr(account, "ensure_approvals", None)
        if approvals is not None:
            self.approvals = await approvals()
        self.log("info", "auth",
                 f"Logged in as {account.info.wallet} ({account.info.wallet_type}); "
                 f"cash ${account.info.cash:,.2f}")
        if self.geo and self.geo.blocked:
            self.log("warning", "auth",
                     f"Polymarket reports your location ({self.geo.country}) as restricted; "
                     "live orders will be refused. Paper trading still works.")
        self.emit("state", self.state())
        return self.account_summary()

    async def auto_login(self) -> bool:
        stored = self.secrets.load()
        if not stored or not stored.get("private_key"):
            return False
        try:
            await self.login(private_key=stored["private_key"], wallet=stored.get("wallet"),
                             remember=True)
            return True
        except Exception as error:
            self.login_error = str(error)
            self.log("error", "auth", f"Automatic login failed: {error}")
            return False

    async def logout(self, forget: bool = True) -> None:
        if self.mode == "live" and self.status != "stopped":
            await self.stop()
        if self.account is not None:
            with contextlib.suppress(Exception):
                await self.account.close()
        self.account = None
        if forget:
            self.secrets.clear()
        self.log("info", "auth", "Logged out")
        self.emit("state", self.state())

    def account_summary(self) -> dict[str, Any] | None:
        if self.account is None:
            return None
        info = self.account.info
        return {
            "wallet": info.wallet,
            "signer": info.signer,
            "wallet_type": info.wallet_type,
            "cash": info.cash,
            "closed_only": info.closed_only,
            "has_relayer_key": info.has_relayer_key,
            "approvals": self.approvals,
        }

    # --------------------------------------------------------------- control
    async def start(self) -> None:
        if self.mode == "live" and self.account is None:
            raise RuntimeError("Log in to your Polymarket wallet before starting live trading.")
        if self.status == "running":
            return
        self.db.kv_set("desired_state", "running")
        if self.status == "paused":
            if self.pause_reason and "drawdown" in self.pause_reason:
                # Resuming acknowledges the drawdown: measure future drawdowns from here.
                snap = self.portfolio.exposure(self.mode, await self._cash(), self._base_capital())
                self.db.kv_set(f"{self.mode}_peak_pnl", snap.bot_pnl_total)
            self.status = "running"
            self.pause_reason = None
            self.log("info", "bot", "Resumed copying")
            self.emit("state", self.state())
            return
        self.status = "running"
        self.pause_reason = None
        self._started_at = time.time()
        if self.mode == "live" and self.db.kv_get("live_base_capital") is None and self.account:
            cash = await self.account.cash_balance()
            snap = self.portfolio.exposure("live", cash, 0.0)
            self.db.kv_set("live_base_capital", snap.equity)
        followed = [t["wallet"] for t in self.db.traders("followed")]
        self.watcher.set_leaders(followed)
        await self.watcher.start()
        self._tasks = [
            asyncio.create_task(self._signal_worker(), name="signals"),
            asyncio.create_task(self._discovery_loop(), name="discovery"),
            asyncio.create_task(self._portfolio_loop(), name="portfolio"),
            asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
        ]
        self.log("info", "bot",
                 f"Bot started in {self.mode.upper()} mode, following {len(followed)} leaders")
        self.emit("state", self.state())

    async def pause(self, reason: str = "paused by user") -> None:
        if self.status != "running":
            return
        self.status = "paused"
        self.pause_reason = reason
        if reason == "paused by user":
            self.db.kv_set("desired_state", "paused")
        self.log("warning" if reason != "paused by user" else "info", "bot",
                 f"New entries paused: {reason}")
        self.emit("state", self.state())

    async def stop(self) -> None:
        if self.status == "stopped":
            return
        self.db.kv_set("desired_state", "stopped")
        await self._halt()
        self.log("info", "bot", "Bot stopped")
        self.emit("state", self.state())

    async def _halt(self) -> None:
        self.status = "stopped"
        await self.watcher.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []

    async def shutdown(self) -> None:
        await self._halt()
        if self.account is not None:
            with contextlib.suppress(Exception):
                await self.account.close()
        with contextlib.suppress(Exception):
            await self.data.close()

    async def set_mode(self, mode: str) -> None:
        if mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        if mode == self.mode:
            return
        if mode == "live" and self.account is None:
            raise RuntimeError("Log in before switching to live trading.")
        was_running = self.status in ("running", "paused")
        if was_running:
            await self._halt()
        self.store.update({"mode": mode})
        self.log("info", "bot", f"Switched to {mode.upper()} mode")
        if was_running:
            await self.start()
        self.emit("state", self.state())

    async def resume_from_saved_state(self) -> None:
        """Restart the bot after a relaunch if it was running when the app closed."""
        desired = self.db.kv_get("desired_state")
        if desired not in ("running", "paused") or not self.settings.auto_start:
            return
        if self.mode == "live" and self.account is None:
            return
        await self.start()
        if desired == "paused":
            await self.pause()

    # ------------------------------------------------------------ followers
    def refresh_leaders(self) -> None:
        followed = [t["wallet"] for t in self.db.traders("followed")]
        self.watcher.set_leaders(followed)
        self.emit("traders", {"followed": followed})

    def set_trader_status(self, wallet: str, action: str) -> dict[str, Any]:
        wallet = wallet.lower()
        now = time.time()
        if action == "follow":
            self.db.upsert_trader(wallet, {"status": "followed", "manual": 1, "followed_at": now})
        elif action == "unfollow":
            self.db.upsert_trader(wallet, {"status": "candidate", "manual": 0})
        elif action == "block":
            self.db.upsert_trader(wallet, {"status": "blocked", "manual": 0})
        elif action == "unblock":
            self.db.upsert_trader(wallet, {"status": "candidate", "manual": 0})
        elif action == "auto":
            self.db.upsert_trader(wallet, {"manual": 0})
        else:
            raise ValueError(f"unknown action {action}")
        self.log("info", "traders", f"{action.capitalize()} {wallet}")
        self.refresh_leaders()
        return self.db.get_trader(wallet) or {}

    # ---------------------------------------------------------------- loops
    async def _discovery_loop(self) -> None:
        while True:
            finished = self.db.kv_get("discovery_finished_at") or 0
            interval = self.settings.discovery.interval_hours * 3600
            wait = finished + interval - time.time()
            if wait <= 0:
                await self.discovery.run()
                self.refresh_leaders()
                continue
            await asyncio.sleep(min(wait, 60))

    async def _signal_worker(self) -> None:
        while True:
            signal = await self.queue.get()
            try:
                await self.handle_signal(signal)
            except Exception as error:
                log.exception("Signal handling failed")
                self.log("error", "signals", f"Signal handling failed: {error}")

    async def _enqueue_signal(self, signal: Signal) -> None:
        self.signals_seen += 1
        await self.queue.put(signal)

    async def _portfolio_loop(self) -> None:
        while True:
            try:
                await self.refresh_portfolio()
            except Exception as error:
                log.exception("Portfolio refresh failed")
                self.log("error", "portfolio", f"Portfolio refresh failed: {error}")
            await asyncio.sleep(20)

    async def _housekeeping_loop(self) -> None:
        while True:
            try:
                if self.mode == "live":
                    self.geo = await self.data.geoblock()
                    if self.account is not None:
                        self.account.info.closed_only = await self.account.closed_only()
                self.db.prune_logs()
            except Exception as error:
                log.debug("Housekeeping failed: %s", error)
            await asyncio.sleep(3 * 3600)

    # ------------------------------------------------------- signal handling
    async def handle_signal(self, signal: Signal) -> None:
        mode = self.mode
        trader = self.db.get_trader(signal.wallet) or {}
        is_entry = signal.side == "BUY" and signal.kind == "TRADE"
        if not is_entry:
            positions = self._positions_for_exit(mode, signal)
            if not positions:
                self._track_leader_trade(signal)
                return
        signal_id = self.db.insert("signals", {
            "mode": mode,
            "wallet": signal.wallet,
            "leader_name": trader.get("name"),
            "asset_id": signal.asset_id,
            "condition_id": signal.condition_id,
            "side": signal.side,
            "kind": signal.kind,
            "leader_price": signal.price,
            "leader_size": signal.size,
            "leader_usdc": signal.usdc,
            "leader_ts": signal.ts,
            "detected_at": signal.detected_at,
            "latency_sec": round(signal.latency, 2),
            "title": signal.title,
            "outcome": signal.outcome,
            "slug": signal.event_slug or signal.slug,
            "decision": "pending",
            "source": signal.source,
        })
        if is_entry:
            decision, reason, stake, order_id = await self._enter(signal, signal_id, trader)
        else:
            decision, reason, stake, order_id = await self._exit(signal, signal_id, positions)
        self.db.update("signals", signal_id, {
            "decision": decision, "reason": reason, "stake": stake, "order_id": order_id,
        })
        row = self.db.one("SELECT * FROM signals WHERE id=?", (signal_id,))
        self.emit("signal", row)
        if decision in ("copied", "exited"):
            self.emit("state", self.state())

    def _track_leader_trade(self, signal: Signal) -> None:
        key = (signal.wallet, signal.asset_id)
        if key in self._leader_pos:
            size, _ = self._leader_pos[key]
            delta = signal.size if signal.side == "BUY" else -signal.size
            self._leader_pos[key] = (max(0.0, size + delta), signal.ts)

    def _positions_for_exit(self, mode: str, signal: Signal) -> list[dict[str, Any]]:
        if signal.kind == "MERGE":
            candidates = self.portfolio.open_in_condition(mode, signal.condition_id)
        else:
            pos = self.portfolio.open_position_for(mode, signal.asset_id)
            candidates = [pos] if pos else []
        return [p for p in candidates if self.portfolio.lot(p["id"], signal.wallet)]

    async def _enter(
        self, signal: Signal, signal_id: int, trader: dict[str, Any]
    ) -> tuple[str, str | None, float | None, int | None]:
        settings = self.settings
        filters = settings.filters
        mode = self.mode
        self._track_leader_trade(signal)

        def skip(reason: str) -> tuple[str, str, None, None]:
            return "skipped", reason, None, None

        if self.status != "running":
            return skip(self.pause_reason or "bot is not running")
        if mode == "live":
            if self.account is None:
                return skip("not logged in")
            if self.geo is not None and self.geo.blocked:
                return skip("location is geoblocked by Polymarket")
            if self.account.info.closed_only:
                return skip("account is in close-only mode")
        age = time.time() - signal.ts
        if age > filters.max_signal_age_sec:
            return skip(f"signal is {age:.0f}s old (limit {filters.max_signal_age_sec:.0f}s)")

        market = await self.markets.for_token(signal.asset_id, max_age=120)
        if market is None and signal.condition_id:
            market = await self.markets.get(signal.condition_id, max_age=120)
        if market is None:
            return skip("market details unavailable")
        if market.closed or not market.active or not market.accepting_orders:
            return skip("market is not accepting orders")
        if not market.enable_order_book:
            return skip("market has no order book")
        reason = check_entry(market, ts=time.time(), price=signal.price, leader_usdc=signal.usdc,
                             rules=EntryRules.from_settings(filters))
        if reason:
            return skip(reason)
        if (market.liquidity or 0) < filters.min_market_liquidity_usdc:
            return skip(f"market liquidity ${market.liquidity or 0:,.0f} below "
                        f"${filters.min_market_liquidity_usdc:,.0f}")

        async with self._entry_lock:
            existing = self.portfolio.open_position_for(mode, signal.asset_id)
            opposite = market.opposite_token(signal.asset_id)
            if opposite and self.portfolio.open_position_for(mode, opposite):
                if filters.skip_conflicting_signals:
                    return skip("already holding the opposite outcome")
            if existing is not None and not filters.allow_adds:
                return skip("already holding this outcome (adds disabled)")
            consensus = 0
            if existing is not None:
                consensus = sum(
                    1 for lot in self.portfolio.lots(existing["id"]) if lot["wallet"] != signal.wallet
                )

            risk_block = self._risk_block()
            if risk_block:
                return skip(risk_block)

            try:
                book = await self.data.order_book(signal.asset_id)
            except Exception as error:
                return skip(f"order book unavailable: {error}")
            if book.best_ask is None:
                return skip("no sellers in the order book")
            if book.spread is not None and book.spread > filters.max_spread:
                return skip(f"spread {book.spread:.3f} wider than {filters.max_spread:.3f}")
            cap = chase_cap(signal.price, filters.max_chase_cents, filters.max_chase_pct)
            cap = min(cap, filters.max_entry_price + filters.max_chase_cents)
            if book.best_ask > cap + 1e-9:
                return skip(f"price moved: best ask {book.best_ask:.3f} > cap {cap:.3f} "
                            f"(leader paid {signal.price:.3f})")

            snapshot = self.portfolio.exposure(mode, await self._cash(), self._base_capital())
            metrics = trader.get("metrics") or {}
            decision = compute_stake(
                StakeRequest(
                    wallet=signal.wallet,
                    condition_id=market.condition_id,
                    price=book.best_ask,
                    leader_score=float(trader.get("score") or 50.0),
                    leader_usdc=signal.usdc,
                    leader_median_usdc=float(metrics.get("median_trade_usdc") or 0.0),
                    consensus=consensus,
                    is_new_position=existing is None,
                ),
                snapshot,
                settings.risk,
            )
            if decision.stake <= 0:
                return skip(decision.reason or "stake too small")
            stake = min(decision.stake, liquidity_under(book.asks, cap))
            if stake < settings.risk.min_trade_usdc:
                return skip(f"only ${stake:.2f} available below the price cap")

            fill = await self.executor.buy(signal.asset_id, usdc=stake, max_price=cap, book=book,
                                           fees=market.fees)
            order_id = self._record_order(
                mode=mode, signal_id=signal_id, position_id=None, wallet=signal.wallet,
                asset_id=signal.asset_id, condition_id=market.condition_id, side="BUY",
                purpose="entry", requested_usdc=stake, requested_shares=None, limit=cap,
                fill=fill, leader_price=signal.price, title=market.question,
                outcome=market.outcome_label(signal.asset_id) or signal.outcome,
            )
            if not fill.ok or fill.shares <= 0:
                return "failed", fill.error or "order not filled", stake, order_id
            position_id = self.portfolio.record_buy(
                mode, wallet=signal.wallet, asset_id=signal.asset_id, market=market, fill=fill,
                title=market.question, outcome=market.outcome_label(signal.asset_id) or signal.outcome,
                slug=signal.slug, event_slug=signal.event_slug, icon=market.icon or signal.icon,
                category=market_category(market),
            )
            self.db.update("orders", order_id, {"position_id": position_id})
            if mode == "live" and self.account is not None:
                with contextlib.suppress(Exception):
                    await self.account.cash_balance()
            asyncio.create_task(self._refresh_leader_position_later(signal.wallet, market.condition_id))
        name = trader.get("name") or signal.wallet[:10]
        self.log("info", "trade",
                 f"Copied {name}: BUY {fill.shares:,.1f} {market.outcome_label(signal.asset_id) or ''} "
                 f"@ {fill.avg_price:.3f} (${fill.usdc:,.2f}) - {market.question}",
                 {"signal_id": signal_id, "order_id": order_id})
        return "copied", None, stake, order_id

    async def _exit(
        self, signal: Signal, signal_id: int, positions: list[dict[str, Any]]
    ) -> tuple[str, str | None, float | None, int | None]:
        settings = self.settings
        mode = self.mode
        if not settings.exits.mirror_leader_sells:
            self._track_leader_trade(signal)
            return "skipped", "mirroring leader exits is disabled", None, None
        if mode == "live" and self.account is None:
            return "skipped", "not logged in", None, None
        last_order = None
        outcomes = []
        for pos in positions:
            lot = self.portfolio.lot(pos["id"], signal.wallet)
            if lot is None or lot["shares"] <= 0:
                continue
            fraction = await self._leader_exit_fraction(signal, pos)
            shares = lot["shares"] * fraction
            price_now = pos["last_price"] or 0.5
            if (lot["shares"] - shares) * price_now < 1.0 or fraction > 0.97:
                shares = lot["shares"]
            if shares <= DUST_SHARES:
                continue
            reference = signal.price if signal.kind == "TRADE" else None
            result, order_id = await self._sell_position(
                pos, shares=shares, wallet=signal.wallet, purpose="mirror_exit",
                reference_price=reference, signal_id=signal_id,
            )
            last_order = order_id or last_order
            outcomes.append(result)
        if signal.kind == "TRADE":
            self._track_leader_trade(signal)
        if not outcomes:
            return "skipped", "no copied shares to sell", None, None
        if all(o == "sold" for o in outcomes):
            return "exited", None, None, last_order
        if any(o == "sold" for o in outcomes):
            return "exited", "partially exited; remainder retrying", None, last_order
        return "pending", "no buyers near the leader's price; retrying", None, last_order

    async def _leader_exit_fraction(self, signal: Signal, pos: dict[str, Any]) -> float:
        asset_id = pos["asset_id"]
        key = (signal.wallet, asset_id)
        tracked = self._leader_pos.get(key)
        before = tracked[0] if tracked and tracked[0] > 0 else None
        if before is None:
            try:
                leader_positions = await self.data.user_positions(
                    signal.wallet, condition_ids=[pos["condition_id"]]
                )
                remaining = next(
                    (p.size for p in leader_positions if p.asset_id == asset_id), 0.0
                )
                before = remaining + signal.size
            except Exception:
                return 1.0
        if signal.kind == "MERGE":
            return min(1.0, signal.size / before) if before > 0 else 1.0
        return min(1.0, signal.size / before) if before > 0 else 1.0

    async def _refresh_leader_position_later(self, wallet: str, condition_id: str) -> None:
        await asyncio.sleep(20)
        await self._refresh_leader_positions(wallet, [condition_id])

    async def _refresh_leader_positions(self, wallet: str, condition_ids: list[str]) -> set[str] | None:
        try:
            positions = await self.data.user_positions(wallet, condition_ids=condition_ids)
        except Exception:
            return None
        held = set()
        stamp = time.time() - 30
        for p in positions:
            if p.size > 0:
                held.add(p.asset_id)
            self._leader_pos[(wallet, p.asset_id)] = (p.size, stamp)
        return held

    async def _sell_position(
        self,
        pos: dict[str, Any],
        *,
        shares: float,
        wallet: str | None,
        purpose: str,
        reference_price: float | None,
        signal_id: int | None = None,
        floor: float | None = None,
    ) -> tuple[str, int | None]:
        mode = pos["mode"]
        market = await self.markets.get(pos["condition_id"], max_age=120)
        try:
            book = await self.data.order_book(pos["asset_id"])
        except Exception as error:
            self.portfolio.set_exit_pending(pos["id"], True)
            self._exit_started.setdefault(pos["id"], time.time())
            self.log("warning", "trade", f"Exit delayed, no order book: {error}")
            return "pending", None
        slip = self.settings.exits.exit_slippage_cents
        if floor is None:
            anchor = reference_price if reference_price is not None else (book.best_bid or 0.0)
            floor = max(0.01, anchor - slip)
        if book.best_bid is None or book.best_bid < floor - 1e-9:
            self.portfolio.set_exit_pending(pos["id"], True)
            self._exit_started.setdefault(pos["id"], time.time())
            return "pending", None
        fill: Fill = await self.executor.sell(
            pos["asset_id"], shares=shares, min_price=floor, book=book,
            fees=market.fees if market else None,
        )
        order_id = self._record_order(
            mode=mode, signal_id=signal_id, position_id=pos["id"], wallet=wallet,
            asset_id=pos["asset_id"], condition_id=pos["condition_id"], side="SELL",
            purpose=purpose, requested_usdc=None, requested_shares=shares, limit=floor, fill=fill,
            leader_price=reference_price, title=pos["title"], outcome=pos["outcome"],
        )
        if not fill.ok or fill.shares <= 0:
            self.portfolio.set_exit_pending(pos["id"], True)
            self._exit_started.setdefault(pos["id"], time.time())
            return "pending", order_id
        result = self.portfolio.record_sell(mode, pos["id"], fill, wallet=wallet)
        if result.closed:
            self._exit_started.pop(pos["id"], None)
        if mode == "live" and self.account is not None:
            with contextlib.suppress(Exception):
                await self.account.cash_balance()
        self.log("info", "trade",
                 f"SELL {result.shares:,.1f} {pos['outcome'] or ''} @ {fill.avg_price:.3f} "
                 f"({purpose.replace('_', ' ')}), realized ${result.realized:+,.2f} - {pos['title']}",
                 {"order_id": order_id, "position_id": pos["id"]})
        return "sold", order_id

    def _record_order(
        self, *, mode: str, signal_id: int | None, position_id: int | None, wallet: str | None,
        asset_id: str, condition_id: str | None, side: str, purpose: str,
        requested_usdc: float | None, requested_shares: float | None, limit: float, fill: Fill,
        leader_price: float | None, title: str | None, outcome: str | None,
    ) -> int:
        avg = fill.avg_price if fill.shares > 0 else None
        slippage = None
        if avg is not None and leader_price:
            slippage = (avg - leader_price) if side == "BUY" else (leader_price - avg)
        status = "filled" if fill.ok and fill.shares > 0 else "failed"
        if status == "filled" and requested_usdc and fill.usdc < requested_usdc * 0.98:
            status = "partial"
        if status == "filled" and requested_shares and fill.shares < requested_shares * 0.98:
            status = "partial"
        order_id = self.db.insert("orders", {
            "mode": mode, "signal_id": signal_id, "position_id": position_id, "wallet": wallet,
            "asset_id": asset_id, "condition_id": condition_id, "side": side, "purpose": purpose,
            "requested_usdc": requested_usdc, "requested_shares": requested_shares,
            "limit_price": limit, "status": status, "shares": fill.shares, "usdc": fill.usdc,
            "fee": fill.fee, "avg_price": avg, "leader_price": leader_price,
            "slippage": slippage, "ext_order_id": fill.order_id, "error": fill.error,
            "title": title, "outcome": outcome, "created_at": time.time(),
        })
        self.emit("order", self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)))
        return order_id

    # ----------------------------------------------------------- risk state
    async def _cash(self) -> float:
        if self.mode == "live":
            return self.account.info.cash if self.account else 0.0
        return self.portfolio.paper_cash()

    def _base_capital(self) -> float:
        if self.mode == "live":
            return float(self.db.kv_get("live_base_capital") or 0.0)
        return self.portfolio.paper_starting_balance()

    def _day_state(self, equity: float) -> dict[str, Any]:
        key = f"{self.mode}_day"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state = self.db.kv_get(key)
        if not state or state.get("date") != today:
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            state = {
                "date": today,
                "start_ts": start.timestamp(),
                "start_equity": equity,
                "start_unrealized": self.portfolio.unrealized_total(self.mode),
            }
            self.db.kv_set(key, state)
        return state

    def day_pnl(self, equity: float) -> float:
        state = self._day_state(equity)
        realized = self.portfolio.realized_since(self.mode, state["start_ts"])
        return realized + self.portfolio.unrealized_total(self.mode) - state["start_unrealized"]

    def _risk_block(self) -> str | None:
        snapshot_cash = self.account.info.cash if (self.mode == "live" and self.account) else (
            self.portfolio.paper_cash()
        )
        snap = self.portfolio.exposure(self.mode, snapshot_cash, self._base_capital())
        state = self._day_state(snap.equity)
        if daily_loss_exceeded(self.day_pnl(snap.equity), state["start_equity"], self.settings.risk):
            return "daily loss limit reached; new entries resume tomorrow (UTC)"
        return None

    async def _check_drawdown(self, equity_pnl: float) -> None:
        key = f"{self.mode}_peak_pnl"
        peak = self.db.kv_get(key)
        if peak is None or equity_pnl > peak:
            peak = equity_pnl
            self.db.kv_set(key, peak)
        if drawdown_exceeded(self._base_capital(), peak, equity_pnl, self.settings.risk):
            if self.status == "running":
                await self.pause(
                    f"drawdown limit hit ({self.settings.risk.max_drawdown_pause_pct:.0%} below peak); "
                    "review and press Resume"
                )

    # ------------------------------------------------------------ portfolio
    async def refresh_portfolio(self) -> None:
        mode = self.mode
        positions = self.portfolio.open_positions(mode)
        if mode == "live" and self.account is not None:
            with contextlib.suppress(Exception):
                await self.account.cash_balance()
        if positions:
            markets = await self.markets.get_many({p["condition_id"] for p in positions}, max_age=60)
            mids = {}
            with contextlib.suppress(Exception):
                mids = await self.data.midpoints([p["asset_id"] for p in positions])
            for pos in positions:
                market: MarketInfo | None = markets.get(pos["condition_id"])
                payout = market.resolution_payout(pos["asset_id"]) if market else None
                if payout is not None:
                    realized = self.portfolio.settle(mode, pos["id"], payout)
                    outcome = "WON" if payout > 0.75 else "LOST" if payout < 0.25 else "SPLIT"
                    self.log("info", "resolution",
                             f"{outcome}: {pos['outcome'] or ''} - {pos['title']} "
                             f"(realized ${realized:+,.2f})", {"position_id": pos["id"]})
                    continue
                price = mids.get(pos["asset_id"])
                if price is not None and price > 0:
                    self.portfolio.mark(pos["id"], price)
                    tp = self.settings.exits.take_profit_price
                    if tp and price >= tp and not pos["exit_pending"]:
                        fresh = self.portfolio.position(pos["id"])
                        if fresh and fresh["status"] == "open":
                            await self._sell_position(fresh, shares=fresh["shares"], wallet=None,
                                                      purpose="take_profit", reference_price=None)
            await self._retry_pending_exits(mode)
            if time.time() - self._last_reconcile > 120:
                self._last_reconcile = time.time()
                await self._reconcile_leaders(mode)
                if mode == "live":
                    await self._reconcile_wallet()
        if mode == "live":
            await self._redeem_resolved()
        cash = await self._cash()
        snap = self.portfolio.exposure(mode, cash, self._base_capital())
        await self._check_drawdown(snap.bot_pnl_total)
        if time.time() - self._last_snapshot > 300:
            self._last_snapshot = time.time()
            self.snapshot_equity()
        self.emit("portfolio", self.summary())

    def snapshot_equity(self) -> None:
        mode = self.mode
        cash = self.account.info.cash if (mode == "live" and self.account) else self.portfolio.paper_cash()
        snap = self.portfolio.exposure(mode, cash, self._base_capital())
        self.db.insert("equity", {
            "mode": mode, "ts": time.time(), "cash": cash,
            "positions_value": snap.equity - cash, "equity": snap.equity,
            "exposure": snap.total_exposure,
            "realized_total": self.portfolio.realized_total(mode),
            "unrealized_total": self.portfolio.unrealized_total(mode),
        })

    async def _retry_pending_exits(self, mode: str) -> None:
        window = self.settings.exits.exit_retry_minutes * 60
        for pos in self.db.query(
            "SELECT * FROM positions WHERE mode=? AND status='open' AND exit_pending=1", (mode,)
        ):
            started = self._exit_started.setdefault(pos["id"], time.time())
            if time.time() - started > window:
                self.portfolio.set_exit_pending(pos["id"], False)
                self._exit_started.pop(pos["id"], None)
                self.log("warning", "trade",
                         f"Could not exit {pos['title']} near the leader's price; holding to resolution")
                continue
            await self._sell_position(pos, shares=pos["shares"], wallet=None,
                                      purpose="mirror_exit", reference_price=None)

    async def _reconcile_leaders(self, mode: str) -> None:
        """Exit lots whose leader no longer holds the outcome (exit we did not see)."""
        lots = self.portfolio.all_open_lots(mode)
        by_wallet: dict[str, list[dict[str, Any]]] = {}
        for lot in lots:
            by_wallet.setdefault(lot["wallet"], []).append(lot)
        for wallet, wallet_lots in by_wallet.items():
            held = await self._refresh_leader_positions(
                wallet, list({lot["condition_id"] for lot in wallet_lots})
            )
            if held is None:
                continue
            for lot in wallet_lots:
                key = (wallet, lot["asset_id"])
                if lot["asset_id"] in held:
                    self._leader_missing.pop(key, None)
                    continue
                if time.time() - (lot.get("opened_at") or 0) < 180:
                    continue  # the data API may not show a brand-new position yet
                self._leader_missing[key] = self._leader_missing.get(key, 0) + 1
                if self._leader_missing[key] < 2 or not self.settings.exits.mirror_leader_sells:
                    continue
                self._leader_missing.pop(key, None)
                pos = self.portfolio.position(lot["position_id"])
                if pos is None or pos["status"] != "open":
                    continue
                market = await self.markets.get(pos["condition_id"], max_age=60)
                if market is not None and market.resolution_payout(pos["asset_id"]) is not None:
                    continue
                self.log("info", "trade",
                         f"Leader {wallet[:10]} no longer holds {pos['outcome']} - {pos['title']}; "
                         "exiting our copy")
                await self._sell_position(pos, shares=lot["shares"], wallet=wallet,
                                          purpose="reconcile_exit", reference_price=None)

    async def _reconcile_wallet(self) -> None:
        if self.account is None:
            return
        try:
            held = await self.data.user_positions(self.account.info.wallet)
        except Exception:
            return
        sizes = {p.asset_id: p.size for p in held}
        for pos in self.portfolio.open_positions("live"):
            if time.time() - (pos["opened_at"] or 0) < 300:
                continue
            wallet_size = sizes.get(pos["asset_id"])
            if wallet_size is None:
                continue
            if wallet_size + DUST_SHARES < pos["shares"]:
                self.portfolio.reduce_to_wallet_balance(pos["id"], wallet_size)
                self.log("warning", "portfolio",
                         f"Adjusted {pos['title']} to the wallet's {wallet_size:,.2f} shares")

    async def _redeem_resolved(self) -> None:
        if self.account is None or not self.settings.exits.auto_redeem:
            return
        rows = self.db.query(
            "SELECT * FROM positions WHERE mode='live' AND status='resolved' AND redeemed=0"
        )
        for pos in rows:
            condition_id = pos["condition_id"]
            if condition_id in self._redeeming:
                continue
            self._redeeming.add(condition_id)
            asyncio.create_task(self._redeem(condition_id))

    async def _redeem(self, condition_id: str) -> None:
        try:
            assert self.account is not None
            result = await self.account.redeem(condition_id)
            if result == "redeemed":
                self.db.execute(
                    "UPDATE positions SET redeemed=1, redeem_error=NULL WHERE mode='live' "
                    "AND condition_id=? AND status='resolved'", (condition_id,),
                )
                self.log("info", "resolution", f"Redeemed winnings for {condition_id[:12]}")
                with contextlib.suppress(Exception):
                    await self.account.cash_balance()
            else:
                self.db.execute(
                    "UPDATE positions SET redeem_error=? WHERE mode='live' AND condition_id=? "
                    "AND status='resolved'", (result, condition_id),
                )
                if result.startswith("skipped"):
                    self.db.execute(
                        "UPDATE positions SET redeemed=1 WHERE mode='live' AND condition_id=? "
                        "AND status='resolved'", (condition_id,),
                    )
        finally:
            await asyncio.sleep(60)
            self._redeeming.discard(condition_id)

    async def close_position(self, position_id: int) -> str:
        pos = self.portfolio.position(position_id)
        if pos is None or pos["status"] != "open":
            raise ValueError("position is not open")
        if pos["mode"] != self.mode:
            raise ValueError(f"switch to {pos['mode']} mode to close this position")
        if pos["mode"] == "live" and self.account is None:
            raise RuntimeError("log in to close live positions")
        try:
            book = await self.data.order_book(pos["asset_id"])
        except Exception as error:
            raise RuntimeError(f"order book unavailable: {error}") from error
        if book.best_bid is None:
            raise RuntimeError("no buyers in the order book right now")
        floor = max(0.01, book.best_bid - self.settings.exits.exit_slippage_cents)
        result, _ = await self._sell_position(pos, shares=pos["shares"], wallet=None,
                                              purpose="manual_close", reference_price=None,
                                              floor=floor)
        self.emit("state", self.state())
        return result

    async def close_all(self) -> dict[str, int]:
        results = {"sold": 0, "pending": 0, "failed": 0}
        for pos in self.portfolio.open_positions(self.mode):
            try:
                outcome = await self.close_position(pos["id"])
                results["sold" if outcome == "sold" else "pending"] += 1
            except Exception:
                results["failed"] += 1
        return results

    # ------------------------------------------------------------------ state
    def summary(self) -> dict[str, Any]:
        mode = self.mode
        cash = self.account.info.cash if (mode == "live" and self.account) else self.portfolio.paper_cash()
        snap = self.portfolio.exposure(mode, cash, self._base_capital())
        realized = self.portfolio.realized_total(mode)
        unrealized = self.portfolio.unrealized_total(mode)
        stats = self.db.one(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins, "
            "COALESCE(SUM(fees),0) AS fees FROM positions WHERE mode=? AND status != 'open'",
            (mode,),
        ) or {}
        closed = int(stats.get("n") or 0)
        wins = int(stats.get("wins") or 0)
        open_fees = float(self.db.scalar(
            "SELECT COALESCE(SUM(fees),0) FROM positions WHERE mode=? AND status='open'", (mode,)
        ) or 0.0)
        return {
            "mode": mode,
            "cash": cash,
            "equity": snap.equity,
            "positions_value": snap.equity - cash,
            "exposure": snap.total_exposure,
            "open_positions": snap.open_positions,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": realized + unrealized,
            "day_pnl": self.day_pnl(snap.equity),
            "closed_positions": closed,
            "win_rate": wins / closed if closed else None,
            "fees_paid": float(stats.get("fees") or 0.0) + open_fees,
            "base_capital": self._base_capital(),
            "unredeemed": self.portfolio.unredeemed_value(mode),
        }

    def state(self) -> dict[str, Any]:
        stream = self.stream
        followed = self.db.traders("followed")
        return {
            "version": __version__,
            "demo": self.demo,
            "status": self.status,
            "pause_reason": self.pause_reason,
            "mode": self.mode,
            "logged_in": self.logged_in,
            "login_error": self.login_error,
            "account": self.account_summary(),
            "secret_backend": self.secrets.backend_name,
            "geo": None if self.geo is None else {
                "blocked": self.geo.blocked, "country": self.geo.country,
                "region": self.geo.region, "checked": self.geo.checked,
            },
            "stream": None if stream is None else {
                "connected": stream.connected,
                "messages": getattr(stream, "messages", 0),
                "last_message_at": getattr(stream, "last_message_at", None),
                "error": getattr(stream, "last_error", None),
            },
            "watcher": {
                "leaders": len(self.watcher.leaders),
                "polls": self.watcher.polls,
                "poll_errors": self.watcher.poll_errors,
                "last_poll_at": self.watcher.last_poll_at,
                "signals_seen": self.signals_seen,
            },
            "discovery": self.discovery.progress,
            "followed": [
                {"wallet": t["wallet"], "name": t.get("name"), "score": t.get("score")}
                for t in followed
            ],
            "started_at": self._started_at,
            "summary": self.summary(),
        }
