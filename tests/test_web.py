from __future__ import annotations

import time

import httpx
import pytest

from polycopy.web.app import create_app

from conftest import LEADER, YES, make_market
from test_bot import signal


@pytest.fixture
async def client(bot):
    app = create_app(bot)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        yield c


async def test_state_and_index(client):
    r = await client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper" and body["summary"]["cash"] == 1000
    assert len(body["followed"]) == 2
    page = await client.get("/")
    assert page.status_code == 200 and "PolyCopy" in page.text
    js = await client.get("/static/app.js")
    assert js.status_code == 200


async def test_blocks_foreign_hosts_and_cross_site(bot):
    app = create_app(bot)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://evil.example") as c:
        assert (await c.get("/api/state")).status_code == 403
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
        r = await c.post("/api/bot/stop", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.post("/api/bot/stop", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        r = await c.post("/api/bot/stop", headers={"Origin": "http://127.0.0.1:8765"})
        assert r.status_code == 200


async def test_settings_roundtrip_and_validation(client):
    r = await client.get("/api/settings")
    assert r.json()["settings"]["filters"]["max_hours_to_resolution"] == 48
    r = await client.put("/api/settings", json={"filters": {"max_hours_to_resolution": 24}})
    assert r.status_code == 200
    assert r.json()["settings"]["filters"]["max_hours_to_resolution"] == 24
    bad = await client.put("/api/settings", json={"risk": {"per_trade_pct": 3}})
    assert bad.status_code == 400 and "per_trade_pct" in bad.json()["detail"]
    r = await client.post("/api/settings/profile", json={"profile": "conservative"})
    assert r.json()["settings"]["risk"]["per_trade_pct"] == 0.01


async def test_positions_orders_signals_analytics(client, bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal())
    positions = (await client.get("/api/positions?status=open")).json()
    assert positions[0]["asset_id"] == YES and positions[0]["leader_names"] == ["alice"]
    assert (await client.get("/api/orders")).json()[0]["side"] == "BUY"
    assert (await client.get("/api/signals?decision=copied")).json()[0]["decision"] == "copied"
    analytics = (await client.get("/api/analytics")).json()
    assert analytics["leaders"][0]["wallet"] == LEADER
    equity = (await client.get("/api/equity?days=1")).json()
    assert equity[-1]["equity"] == pytest.approx(bot.summary()["equity"])
    r = await client.post(f"/api/positions/{positions[0]['id']}/close")
    assert r.status_code == 200 and r.json()["result"] == "sold"
    closed = (await client.get("/api/positions?status=closed")).json()
    assert closed[0]["result"] == "sold"


async def test_trader_actions(client):
    r = await client.post(f"/api/traders/{LEADER}/block")
    assert r.json()["status"] == "blocked"
    state = (await client.get("/api/state")).json()
    assert len(state["followed"]) == 1
    r = await client.post("/api/traders", json={"wallet": "0x" + "ef" * 20})
    assert r.json()["status"] == "followed" and r.json()["manual"] == 1
    detail = (await client.get("/api/traders/0x" + "ef" * 20)).json()
    assert detail["wallet"] == "0x" + "ef" * 20
    assert (await client.post(f"/api/traders/{LEADER}/explode")).status_code == 400


async def test_login_and_mode_switch(client, bot):
    r = await client.post("/api/mode", json={"mode": "live"})
    assert r.status_code == 400 and "Log in" in r.json()["detail"]
    r = await client.post("/api/login", json={"private_key": "12" * 32, "wallet": "0x" + "cc" * 20})
    assert r.status_code == 200 and r.json()["account"]["wallet_type"] == "POLY_PROXY"
    r = await client.post("/api/mode", json={"mode": "live"})
    assert r.json()["mode"] == "live"
    await client.post("/api/logout")
    assert (await client.get("/api/state")).json()["logged_in"] is False


async def test_paper_reset_requires_stop(client, bot):
    r = await client.post("/api/paper/reset", json={"balance": 2500})
    assert r.status_code == 400
    bot.status = "stopped"
    r = await client.post("/api/paper/reset", json={"balance": 2500})
    assert r.status_code == 200 and r.json()["summary"]["cash"] == 2500


async def test_bot_start_stop_lifecycle(client, bot):
    bot.status = "stopped"
    bot.db.kv_set("discovery_finished_at", time.time())  # skip the scan in this test
    r = await client.post("/api/bot/start")
    assert r.json()["status"] == "running"
    r = await client.post("/api/bot/pause")
    assert r.json()["status"] == "paused"
    r = await client.post("/api/bot/start")
    assert r.json()["status"] == "running"
    r = await client.post("/api/bot/stop")
    assert r.json()["status"] == "stopped"
    assert bot.db.kv_get("desired_state") == "stopped"
