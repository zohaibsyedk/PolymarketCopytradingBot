"""FastAPI app: JSON API + websocket push + the static web terminal.

The server only listens on 127.0.0.1. On top of that every API request is
checked for a local Host header (blocks DNS-rebinding) and rejected when the
browser marks it as cross-site (blocks other websites from driving the bot).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from polycopy.config import RISK_PRESETS, Settings
from polycopy.engine.bot import Bot
from polycopy.paths import static_dir

log = logging.getLogger(__name__)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class LoginBody(BaseModel):
    private_key: str = Field(min_length=10)
    wallet: str | None = None
    remember: bool = True


class DetectBody(BaseModel):
    private_key: str = Field(min_length=10)


class ModeBody(BaseModel):
    mode: str


class TraderBody(BaseModel):
    wallet: str = Field(min_length=42, max_length=42)


class ProfileBody(BaseModel):
    profile: str


class PaperResetBody(BaseModel):
    balance: float = Field(gt=0)


def _host_of(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.split(":")[0]


def create_app(bot: Bot) -> FastAPI:
    app = FastAPI(title="PolyCopy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bot = bot

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = _host_of(request.headers.get("host"))
        if host not in LOCAL_HOSTS and host != "testserver":
            return JSONResponse({"detail": "PolyCopy only accepts local connections"}, 403)
        if request.url.path.startswith("/api"):
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "cross-site request blocked"}, 403)
            origin = request.headers.get("origin")
            if origin and (urlparse(origin).hostname or "") not in LOCAL_HOSTS:
                return JSONResponse({"detail": "cross-origin request blocked"}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def fail(error: Exception, status: int = 400):
        raise HTTPException(status_code=status, detail=str(error) or type(error).__name__)

    # ------------------------------------------------------------- state/auth
    @app.get("/api/state")
    async def get_state():
        return bot.state()

    @app.post("/api/login")
    async def login(body: LoginBody):
        try:
            account = await bot.login(private_key=body.private_key, wallet=body.wallet or None,
                                      remember=body.remember)
        except Exception as error:
            log.warning("Login failed: %s", error)
            fail(error)
        return {"ok": True, "account": account, "state": bot.state()}

    @app.post("/api/wallet/detect")
    async def detect_wallet(body: DetectBody):
        finder = getattr(bot.data, "wallet_candidates", None)
        if finder is None:
            return {"candidates": []}
        key = body.private_key.strip()
        if not key.startswith("0x"):
            key = "0x" + key
        try:
            return {"candidates": await finder(key)}
        except Exception as error:
            fail(error)

    @app.post("/api/logout")
    async def logout():
        await bot.logout(forget=True)
        return {"ok": True}

    # ---------------------------------------------------------------- control
    @app.post("/api/bot/{action}")
    async def control(action: str):
        try:
            if action == "start":
                await bot.start()
            elif action == "pause":
                await bot.pause()
            elif action == "stop":
                await bot.stop()
            else:
                raise ValueError(f"unknown action {action}")
        except Exception as error:
            fail(error)
        return bot.state()

    @app.post("/api/mode")
    async def set_mode(body: ModeBody):
        try:
            await bot.set_mode(body.mode)
        except Exception as error:
            fail(error)
        return bot.state()

    # -------------------------------------------------------------- positions
    def _mode(mode: str | None) -> str:
        return mode if mode in ("paper", "live") else bot.mode

    @app.get("/api/positions")
    async def positions(status: str = "open", mode: str | None = None, limit: int = 500):
        m = _mode(mode)
        if status == "open":
            rows = bot.db.query(
                "SELECT * FROM positions WHERE mode=? AND status='open' ORDER BY opened_at DESC",
                (m,),
            )
        else:
            rows = bot.db.query(
                "SELECT * FROM positions WHERE mode=? AND status!='open' "
                "ORDER BY closed_at DESC LIMIT ?", (m, limit),
            )
        names = {t["wallet"]: t.get("name") for t in bot.db.traders()}
        for row in rows:
            row["leader_names"] = [names.get(w) or w[:10] for w in (row.get("leaders") or [])]
            shares = row["shares"] or 0
            price = row["last_price"]
            if row["status"] == "open":
                row["value"] = shares * price if price is not None else row["cost"]
                row["unrealized_pnl"] = row["value"] - row["cost"]
                row["avg_price"] = row["cost"] / shares if shares else None
            entry = bot.db.one(
                "SELECT SUM(usdc) AS usdc, SUM(shares) AS shares FROM orders WHERE position_id=? "
                "AND side='BUY' AND status IN ('filled','partial')", (row["id"],),
            ) or {}
            if entry.get("shares"):
                row["entry_price"] = entry["usdc"] / entry["shares"]
        return rows

    @app.post("/api/positions/close-all")
    async def close_all():
        return await bot.close_all()

    @app.post("/api/positions/{position_id}/close")
    async def close_position(position_id: int):
        try:
            result = await bot.close_position(position_id)
        except Exception as error:
            fail(error)
        return {"result": result}

    @app.get("/api/orders")
    async def orders(mode: str | None = None, limit: int = 300):
        return bot.db.query("SELECT * FROM orders WHERE mode=? ORDER BY created_at DESC LIMIT ?",
                            (_mode(mode), min(limit, 5000)))

    @app.get("/api/signals")
    async def signals(mode: str | None = None, decision: str | None = None, limit: int = 300):
        if decision:
            return bot.db.query(
                "SELECT * FROM signals WHERE mode=? AND decision=? ORDER BY detected_at DESC "
                "LIMIT ?", (_mode(mode), decision, min(limit, 5000)),
            )
        return bot.db.query(
            "SELECT * FROM signals WHERE mode=? ORDER BY detected_at DESC LIMIT ?",
            (_mode(mode), min(limit, 5000)),
        )

    # ---------------------------------------------------------------- traders
    @app.get("/api/traders")
    async def traders(status: str | None = None, limit: int = 300):
        rows = bot.db.traders(status) if status else bot.db.query(
            "SELECT * FROM traders WHERE last_scored_at IS NOT NULL OR status != 'candidate' "
            "ORDER BY CASE status WHEN 'followed' THEN 0 WHEN 'benched' THEN 1 ELSE 2 END, "
            "score DESC NULLS LAST LIMIT ?", (limit,),
        )
        for row in rows:
            row["live"] = bot.portfolio.leader_live_stats(bot.mode, row["wallet"])
            row.pop("curve", None)
        return rows

    @app.get("/api/traders/{wallet}")
    async def trader(wallet: str):
        row = bot.db.get_trader(wallet)
        if row is None:
            raise HTTPException(404, "unknown trader")
        row["live"] = bot.portfolio.leader_live_stats(bot.mode, wallet)
        row["recent_trades"] = bot.db.query(
            "SELECT * FROM leader_trades WHERE wallet=? ORDER BY ts DESC LIMIT 100",
            (wallet.lower(),),
        )
        row["open_lots"] = bot.portfolio.open_lots_for_leader(bot.mode, wallet)
        return row

    @app.post("/api/traders")
    async def add_trader(body: TraderBody):
        wallet = body.wallet.lower()
        if not wallet.startswith("0x"):
            raise HTTPException(400, "wallet must be a 0x address")
        bot.set_trader_status(wallet, "follow")
        asyncio.create_task(_score_one(bot, wallet))
        return bot.db.get_trader(wallet)

    @app.post("/api/traders/{wallet}/{action}")
    async def trader_action(wallet: str, action: str):
        try:
            return bot.set_trader_status(wallet, action)
        except ValueError as error:
            fail(error)

    @app.post("/api/discovery/run")
    async def run_discovery():
        if bot.discovery.progress.get("running"):
            return bot.discovery.progress

        async def go():
            await bot.discovery.run()
            bot.refresh_leaders()

        asyncio.create_task(go())
        await asyncio.sleep(0.05)
        return bot.discovery.progress

    # -------------------------------------------------------------- analytics
    @app.get("/api/equity")
    async def equity(mode: str | None = None, days: float = 30):
        m = _mode(mode)
        since = time.time() - days * 86400 if days > 0 else 0
        rows = bot.db.query(
            "SELECT ts, equity, cash, positions_value, realized_total, unrealized_total "
            "FROM equity WHERE mode=? AND ts>=? ORDER BY ts", (m, since),
        )
        if m == bot.mode:
            s = bot.summary()
            rows.append({
                "ts": time.time(), "equity": s["equity"], "cash": s["cash"],
                "positions_value": s["positions_value"], "realized_total": s["realized_pnl"],
                "unrealized_total": s["unrealized_pnl"],
            })
        return rows

    @app.get("/api/analytics")
    async def analytics(mode: str | None = None):
        return _analytics(bot, _mode(mode))

    @app.get("/api/logs")
    async def logs(limit: int = 300, level: str | None = None, category: str | None = None):
        clauses, params = [], []
        if level:
            clauses.append("level=?")
            params.append(level)
        if category:
            clauses.append("category=?")
            params.append(category)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(min(limit, 5000))
        return bot.db.query(f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ?", params)

    # --------------------------------------------------------------- settings
    @app.get("/api/settings")
    async def get_settings():
        return {"settings": bot.settings.model_dump(), "presets": RISK_PRESETS,
                "defaults": Settings().model_dump()}

    @app.put("/api/settings")
    async def put_settings(patch: dict[str, Any]):
        patch.pop("mode", None)  # mode changes go through /api/mode
        try:
            bot.store.update(patch)
        except ValidationError as error:
            fail(ValueError("; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
            )))
        bot.log("info", "settings", "Settings updated")
        bot.emit("state", bot.state())
        return {"settings": bot.settings.model_dump()}

    @app.post("/api/settings/profile")
    async def set_profile(body: ProfileBody):
        if body.profile not in RISK_PRESETS:
            raise HTTPException(400, "unknown profile")
        bot.store.update({"risk_profile": body.profile})
        bot.log("info", "settings", f"Risk profile set to {body.profile}")
        return {"settings": bot.settings.model_dump()}

    @app.post("/api/shutdown")
    async def shutdown_server():
        """Stop the bot and quit the PolyCopy server process."""
        stopper = getattr(app.state, "shutdown", None)
        if stopper is None:
            raise HTTPException(400, "shutdown is not available in this mode")
        bot.log("info", "bot", "Server shutting down (requested from the web terminal)")
        asyncio.get_running_loop().call_later(0.3, stopper)
        return {"ok": True}

    @app.post("/api/paper/reset")
    async def reset_paper(body: PaperResetBody):
        if bot.mode == "paper" and bot.status != "stopped":
            raise HTTPException(400, "stop the bot before resetting the paper account")
        bot.store.update({"paper_starting_balance": body.balance})
        bot.portfolio.reset_paper(body.balance)
        bot.log("info", "settings", f"Paper account reset to ${body.balance:,.2f}")
        return bot.state()

    # -------------------------------------------------------------- websocket
    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        host = _host_of(websocket.headers.get("host"))
        if (origin and (urlparse(origin).hostname or "") not in LOCAL_HOSTS) or (
            host not in LOCAL_HOSTS and host != "testserver"
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        bot.listeners.add(queue)
        try:
            await websocket.send_json({"type": "state", "data": bot.state(), "ts": time.time()})
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    message = {"type": "ping", "ts": time.time()}
                await websocket.send_json(_jsonable(message))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            bot.listeners.discard(queue)

    # ----------------------------------------------------------------- static
    static = static_dir()
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(static / "index.html"))

    return app


async def _score_one(bot: Bot, wallet: str) -> None:
    from polycopy.engine.discovery import sim_params_from_settings

    settings = bot.settings
    with contextlib.suppress(Exception):
        await bot.discovery.score_wallet(
            wallet,
            params=sim_params_from_settings(settings),
            since=time.time() - settings.discovery.lookback_days * 86400,
            max_trades=settings.discovery.max_trades_per_trader,
        )
        bot.emit("traders", {})


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _analytics(bot: Bot, mode: str) -> dict[str, Any]:
    db = bot.db
    daily: dict[str, float] = {}
    for row in db.query("SELECT ts, amount FROM pnl_events WHERE mode=? ORDER BY ts", (mode,)):
        day = datetime.fromtimestamp(row["ts"], timezone.utc).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + row["amount"]

    names = {t["wallet"]: t.get("name") for t in db.traders()}
    leaders = []
    for row in db.query(
        "SELECT l.wallet, COUNT(*) AS lots, "
        "SUM(CASE WHEN l.shares <= 0 THEN 1 ELSE 0 END) AS closed, "
        "SUM(CASE WHEN l.shares <= 0 AND l.realized_pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        "COALESCE(SUM(l.invested),0) AS invested, COALESCE(SUM(l.realized_pnl),0) AS realized, "
        "COALESCE(SUM(CASE WHEN l.shares > 0 THEN l.shares * COALESCE(p.last_price, 0) - l.cost "
        "ELSE 0 END),0) AS unrealized "
        "FROM lots l JOIN positions p ON p.id=l.position_id WHERE p.mode=? GROUP BY l.wallet "
        "ORDER BY realized DESC", (mode,),
    ):
        row["name"] = names.get(row["wallet"]) or row["wallet"][:10]
        row["win_rate"] = row["wins"] / row["closed"] if row["closed"] else None
        row["roi"] = row["realized"] / row["invested"] if row["invested"] else None
        leaders.append(row)

    categories = db.query(
        "SELECT COALESCE(category,'other') AS category, SUM(amount) AS pnl, COUNT(*) AS events "
        "FROM pnl_events WHERE mode=? GROUP BY COALESCE(category,'other') ORDER BY pnl DESC",
        (mode,),
    )

    buckets = []
    for lo, hi in ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)):
        rows = db.query(
            "SELECT p.realized_pnl, p.invested FROM positions p JOIN ("
            " SELECT position_id, SUM(usdc)/SUM(shares) AS px FROM orders"
            " WHERE side='BUY' AND status IN ('filled','partial') AND shares > 0"
            " GROUP BY position_id) e ON e.position_id=p.id "
            "WHERE p.mode=? AND p.status!='open' AND e.px>=? AND e.px<?", (mode, lo, hi),
        )
        n = len(rows)
        buckets.append({
            "bucket": f"{lo:.1f}-{hi:.1f}",
            "positions": n,
            "win_rate": (sum(1 for r in rows if r["realized_pnl"] > 0) / n) if n else None,
            "pnl": sum(r["realized_pnl"] for r in rows),
        })

    slip = db.query(
        "SELECT slippage FROM orders WHERE mode=? AND purpose='entry' AND slippage IS NOT NULL",
        (mode,),
    )
    slippages = [r["slippage"] for r in slip]
    lat = db.query(
        "SELECT latency_sec, source FROM signals WHERE mode=? AND decision='copied'", (mode,)
    )
    latencies = sorted(r["latency_sec"] for r in lat if r["latency_sec"] is not None)

    decisions = db.query(
        "SELECT decision, COUNT(*) AS n FROM signals WHERE mode=? GROUP BY decision", (mode,)
    )
    reasons: dict[str, int] = {}
    for row in db.query(
        "SELECT reason FROM signals WHERE mode=? AND decision='skipped' "
        "ORDER BY detected_at DESC LIMIT 5000", (mode,),
    ):
        key = _reason_bucket(row["reason"] or "other")
        reasons[key] = reasons.get(key, 0) + 1

    hold = db.one(
        "SELECT AVG(closed_at - opened_at) AS avg_hold, COUNT(*) AS n FROM positions "
        "WHERE mode=? AND status!='open' AND closed_at IS NOT NULL", (mode,),
    ) or {}
    volume = db.one(
        "SELECT COALESCE(SUM(usdc),0) AS volume, COALESCE(SUM(fee),0) AS fees, COUNT(*) AS n "
        "FROM orders WHERE mode=? AND status IN ('filled','partial')", (mode,),
    ) or {}
    results = db.query(
        "SELECT result, COUNT(*) AS n, COALESCE(SUM(realized_pnl),0) AS pnl FROM positions "
        "WHERE mode=? AND status!='open' GROUP BY result", (mode,),
    )
    return {
        "daily_pnl": [{"day": d, "pnl": round(v, 2)} for d, v in sorted(daily.items())],
        "leaders": leaders,
        "categories": categories,
        "price_buckets": buckets,
        "slippage": {
            "count": len(slippages),
            "avg": sum(slippages) / len(slippages) if slippages else None,
            "max": max(slippages) if slippages else None,
            "histogram": _histogram(slippages, [-0.02, -0.01, 0.0, 0.005, 0.01, 0.015, 0.02, 0.03]),
        },
        "latency": {
            "count": len(latencies),
            "median": latencies[len(latencies) // 2] if latencies else None,
            "p90": latencies[int(len(latencies) * 0.9)] if latencies else None,
        },
        "decisions": decisions,
        "skip_reasons": sorted(({"reason": k, "n": v} for k, v in reasons.items()),
                               key=lambda r: -r["n"])[:12],
        "avg_hold_hours": (hold.get("avg_hold") or 0) / 3600 if hold.get("avg_hold") else None,
        "volume": volume.get("volume"),
        "fees": volume.get("fees"),
        "fills": volume.get("n"),
        "results": results,
    }


def _histogram(values: list[float], edges: list[float]) -> list[dict[str, Any]]:
    bins = []
    bounds = [float("-inf")] + edges + [float("inf")]
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        label = (f"< {hi * 100:.1f}c" if lo == float("-inf") else
                 f">= {lo * 100:.1f}c" if hi == float("inf") else
                 f"{lo * 100:.1f} to {hi * 100:.1f}c")
        bins.append({"label": label, "n": sum(1 for v in values if lo <= v < hi)})
    return bins


def _reason_bucket(reason: str) -> str:
    r = reason.lower()
    for needle, label in (
        ("price moved", "price moved past cap"),
        ("resolves in", "outside resolution window"),
        ("past its end", "outside resolution window"),
        ("price ", "entry price outside band"),
        ("leader trade", "leader trade too small"),
        ("in-play", "game in progress"),
        ("spread", "spread too wide"),
        ("liquidity", "low liquidity"),
        ("opposite", "conflicting position"),
        ("old", "signal too old"),
        ("exposure", "exposure limit"),
        ("cash reserve", "cash limit"),
        ("max open positions", "position count limit"),
        ("daily loss", "daily loss limit"),
        ("not running", "bot paused"),
        ("paused", "bot paused"),
        ("drawdown", "bot paused"),
        ("not accepting", "market closed"),
        ("geoblock", "geoblocked"),
    ):
        if needle in r:
            return label
    return reason[:40]
