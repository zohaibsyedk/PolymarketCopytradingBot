"""End-to-end engine behaviour with a fake market: signal -> decision -> ledger."""

from __future__ import annotations

import asyncio
import time

import pytest

from polycopy.engine.watcher import LeaderWatcher, Signal
from polycopy.models import PositionInfo, TradeEvent

from conftest import CID, LEADER, LEADER2, NO, YES, make_book, make_market


def signal(side="BUY", asset=YES, price=0.50, size=400.0, wallet=LEADER, kind="TRADE", age=2.0):
    now = time.time()
    return Signal(wallet=wallet, asset_id=asset, condition_id=CID, side=side, kind=kind,
                  price=price, size=size, usdc=price * size, ts=now - age, tx_hash="0xtx",
                  detected_at=now, source="stream", title="Market", outcome="Yes")


def last_signal(bot):
    return bot.db.one("SELECT * FROM signals ORDER BY id DESC LIMIT 1")


async def test_copies_a_qualifying_buy(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.50))
    await bot.handle_signal(signal())
    row = last_signal(bot)
    assert row["decision"] == "copied", row["reason"]
    pos = bot.portfolio.open_position_for("paper", YES)
    assert pos is not None and pos["shares"] > 0
    order = bot.db.one("SELECT * FROM orders ORDER BY id DESC LIMIT 1")
    assert order["status"] == "filled" and order["avg_price"] == pytest.approx(0.50)
    assert order["slippage"] == pytest.approx(0.0)
    assert bot.portfolio.paper_cash() < 1000


@pytest.mark.parametrize(
    "setup, sig_kwargs, reason",
    [
        ({"hours": 100}, {}, "limit 48h"),
        ({"hours": 0.2}, {}, "too soon"),
        ({}, {"age": 500}, "old"),
        ({}, {"price": 0.98}, "outside"),
        ({}, {"size": 10}, "below"),
        ({"closed": True}, {}, "not accepting"),
        ({"liquidity": 10.0}, {}, "liquidity"),
    ],
)
async def test_skip_reasons(bot, fake_data, setup, sig_kwargs, reason):
    fake_data.add_market(make_market(**setup))
    await bot.handle_signal(signal(**sig_kwargs))
    row = last_signal(bot)
    assert row["decision"] == "skipped"
    assert reason in row["reason"]
    assert bot.portfolio.open_positions("paper") == []


async def test_does_not_chase_a_price_that_ran_away(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.50), yes_ask=0.56)
    await bot.handle_signal(signal(price=0.50))
    row = last_signal(bot)
    assert row["decision"] == "skipped" and "price moved" in row["reason"]


async def test_skips_when_paused(bot, fake_data):
    fake_data.add_market(make_market())
    bot.status = "paused"
    bot.pause_reason = "paused by user"
    await bot.handle_signal(signal())
    assert last_signal(bot)["reason"] == "paused by user"


async def test_conflicting_outcome_is_skipped(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal())
    await bot.handle_signal(signal(asset=NO, wallet=LEADER2))
    row = last_signal(bot)
    assert row["decision"] == "skipped" and "opposite" in row["reason"]


async def test_consensus_add_from_second_leader(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal())
    first = bot.portfolio.open_position_for("paper", YES)["cost"]
    await bot.handle_signal(signal(wallet=LEADER2))
    pos = bot.portfolio.open_position_for("paper", YES)
    assert pos["cost"] > first
    assert len(bot.portfolio.lots(pos["id"])) == 2


async def test_mirrors_partial_leader_exit(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal(size=400))
    pos = bot.portfolio.open_position_for("paper", YES)
    shares = pos["shares"]
    # Leader sells 100 of their 400 shares; the data API still shows 300 left.
    fake_data.positions[LEADER] = [PositionInfo(wallet=LEADER, asset_id=YES, condition_id=CID,
                                                size=300, avg_price=0.5, current_price=0.5,
                                                current_value=150)]
    await bot.handle_signal(signal(side="SELL", price=0.50, size=100))
    row = last_signal(bot)
    assert row["decision"] == "exited", row["reason"]
    pos = bot.portfolio.open_position_for("paper", YES)
    assert pos["shares"] == pytest.approx(shares * 0.75, rel=1e-3)


async def test_full_exit_and_unrelated_sell_ignored(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal(size=400))
    count = bot.db.scalar("SELECT COUNT(*) FROM signals")
    await bot.handle_signal(signal(side="SELL", wallet=LEADER2, size=50))
    assert bot.db.scalar("SELECT COUNT(*) FROM signals") == count  # no lot from LEADER2
    fake_data.positions[LEADER] = []
    await bot.handle_signal(signal(side="SELL", size=400))
    assert bot.portfolio.open_position_for("paper", YES) is None
    closed = bot.db.one("SELECT * FROM positions WHERE asset_id=?", (YES,))
    assert closed["status"] == "closed" and closed["result"] == "sold"


async def test_exit_waits_when_bids_collapsed(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal())
    fake_data.books[YES] = make_book(YES, 0.30)
    fake_data.positions[LEADER] = []
    await bot.handle_signal(signal(side="SELL", price=0.50))
    row = last_signal(bot)
    assert row["decision"] == "pending"
    assert bot.portfolio.open_position_for("paper", YES)["exit_pending"] == 1


async def test_resolution_settles_position(bot, fake_data):
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal())
    cash_before = bot.portfolio.paper_cash()
    shares = bot.portfolio.open_position_for("paper", YES)["shares"]
    fake_data.markets[CID] = make_market(closed=True, yes_price=1.0)
    bot.markets._mem.clear()
    bot.db.execute("DELETE FROM markets")
    await bot.refresh_portfolio()
    pos = bot.db.one("SELECT * FROM positions WHERE asset_id=?", (YES,))
    assert pos["status"] == "resolved" and pos["result"] == "won"
    assert bot.portfolio.paper_cash() == pytest.approx(cash_before + shares)
    assert bot.summary()["realized_pnl"] > 0


async def test_daily_loss_limit_blocks_entries(bot, fake_data):
    fake_data.add_market(make_market())
    bot.store.update({"risk": {"daily_loss_limit_pct": 0.05}})
    bot.db.insert("pnl_events", {"mode": "paper", "ts": time.time(), "kind": "sell", "amount": -80.0})
    await bot.handle_signal(signal())
    assert "daily loss limit" in last_signal(bot)["reason"]


async def test_drawdown_pauses_bot_and_resume_acknowledges_it(bot):
    bot.db.kv_set("paper_peak_pnl", 300.0)
    await bot._check_drawdown(-200.0)
    assert bot.status == "paused" and "drawdown" in bot.pause_reason
    await bot.start()  # manual resume
    assert bot.status == "running"
    await bot._check_drawdown(0.0)  # same level as when resumed: no immediate re-pause
    assert bot.status == "running"


async def test_live_mode_uses_account_and_redeems(bot, fake_data):
    await bot.login(private_key="0x" + "12" * 32, wallet="0x" + "cc" * 20, remember=True)
    assert bot.logged_in and bot.secrets.load()["private_key"].startswith("0x12")
    bot.store.update({"mode": "live"})
    fake_data.add_market(make_market(yes_price=0.5))
    await bot.handle_signal(signal())
    assert bot.fake_account.orders[0][0] == "BUY"
    fake_data.markets[CID] = make_market(closed=True, yes_price=1.0)
    bot.markets._mem.clear()
    bot.db.execute("DELETE FROM markets")
    await bot.refresh_portfolio()
    await asyncio.sleep(0.05)
    assert bot.fake_account.redeemed == [CID]
    await bot.logout()
    assert not bot.logged_in and bot.secrets.load() is None


async def test_live_mode_refuses_when_geoblocked(bot, fake_data):
    await bot.login(private_key="0x" + "12" * 32, wallet="0x" + "cc" * 20, remember=False)
    bot.store.update({"mode": "live"})
    bot.geo.blocked = True
    fake_data.add_market(make_market())
    await bot.handle_signal(signal())
    assert "geoblocked" in last_signal(bot)["reason"]
    assert bot.fake_account.orders == []


async def test_set_mode_live_requires_login(bot):
    with pytest.raises(RuntimeError):
        await bot.set_mode("live")


# ----------------------------------------------------------------- watcher
class _Settings:
    class execution:
        use_realtime_stream = True
        poll_interval_sec = 1.0
        aggregation_window_sec = 0.0


async def test_watcher_dedupes_and_groups(tmp_path):
    from polycopy.db import Database

    db = Database(tmp_path / "w.db")
    got: list[Signal] = []

    async def on_signal(s):
        got.append(s)

    watcher = LeaderWatcher(data=None, db=db, settings=lambda: _Settings, on_signal=on_signal)
    watcher.set_leaders([LEADER])
    now = time.time()
    fill1 = TradeEvent(wallet=LEADER, asset_id=YES, condition_id=CID, side="BUY", price=0.40,
                       size=100, ts=now, tx_hash="0xT", source="stream")
    fill2 = TradeEvent(wallet=LEADER, asset_id=YES, condition_id=CID, side="BUY", price=0.44,
                       size=100, ts=now, tx_hash="0xT", source="stream")
    dup = TradeEvent(wallet=LEADER, asset_id=YES, condition_id=CID, side="BUY", price=0.40,
                     size=100, ts=now, tx_hash="0xT", source="poll")
    old = TradeEvent(wallet=LEADER, asset_id=YES, condition_id=CID, side="BUY", price=0.40,
                     size=100, ts=now - 3600, tx_hash="0xOLD", source="poll")
    stranger = TradeEvent(wallet="0x" + "99" * 20, asset_id=YES, condition_id=CID, side="BUY",
                          price=0.40, size=100, ts=now, tx_hash="0xS")
    for event in (fill1, fill2, dup, old, stranger):
        await watcher.ingest(event)
    await watcher.flush(force=True)
    assert len(got) == 1
    assert got[0].size == 200 and got[0].price == pytest.approx(0.42) and got[0].fills == 2
    # A late fill of an already-emitted order does not create a second signal.
    late = TradeEvent(wallet=LEADER, asset_id=YES, condition_id=CID, side="BUY", price=0.45,
                      size=50, ts=now, tx_hash="0xT", source="poll")
    await watcher.ingest(late)
    await watcher.flush(force=True)
    assert len(got) == 1
    assert db.scalar("SELECT COUNT(*) FROM leader_trades") == 4  # fills, old trade, late fill
    db.close()
