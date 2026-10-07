from __future__ import annotations

import pytest

from polycopy.config import RISK_PRESETS, RiskSettings, Settings, SettingsStore, detect_profile
from polycopy.db import Database
from polycopy.engine.executor import PaperExecutor, liquidity_under, walk_asks, walk_bids
from polycopy.engine.portfolio import Portfolio
from polycopy.engine.risk import (
    ExposureSnapshot,
    StakeRequest,
    chase_cap,
    compute_stake,
    daily_loss_exceeded,
    drawdown_exceeded,
)
from polycopy.models import BookLevel, FeeInfo, Fill, OrderBook

from conftest import make_market


def request(**kw):
    base = dict(wallet="0xa", condition_id="c1", price=0.5, leader_score=50, leader_usdc=200,
                leader_median_usdc=200)
    base.update(kw)
    return StakeRequest(**base)


def test_stake_scales_with_score_and_conviction():
    snap = ExposureSnapshot(equity=1000, cash=1000)
    risk = RiskSettings()
    low = compute_stake(request(leader_score=0), snap, risk).stake
    high = compute_stake(request(leader_score=100), snap, risk).stake
    assert high > low
    big = compute_stake(request(leader_usdc=2000), snap, risk).stake
    assert big > compute_stake(request(), snap, risk).stake
    assert compute_stake(request(), snap, risk).stake == pytest.approx(1000 * 0.02 * 1.0 * 1.0)


def test_stake_caps_and_reasons():
    risk = RiskSettings(max_trade_usdc=10)
    assert compute_stake(request(leader_score=100, leader_usdc=5000),
                         ExposureSnapshot(equity=10000, cash=10000), risk).stake == 10
    snap = ExposureSnapshot(equity=1000, cash=1000, market_exposure={"c1": 79.0})
    decision = compute_stake(request(), snap, RiskSettings())
    assert decision.stake == 0 and "market exposure" in decision.reason
    snap = ExposureSnapshot(equity=1000, cash=40)
    decision = compute_stake(request(), snap, RiskSettings())
    assert decision.stake == 0 and "cash reserve" in decision.reason
    full = ExposureSnapshot(equity=1000, cash=1000, open_positions=30)
    assert "max open positions" in compute_stake(request(), full, RiskSettings()).reason


def test_fixed_bankroll_mode():
    risk = RiskSettings(bankroll_mode="fixed", allocated_bankroll_usdc=200)
    snap = ExposureSnapshot(equity=5000, cash=5000, bot_pnl_total=50)
    assert compute_stake(request(), snap, risk).bankroll == 250


def test_chase_cap_uses_tighter_limit():
    assert chase_cap(0.50, 0.02, 0.06) == 0.52
    assert chase_cap(0.10, 0.02, 0.06) == pytest.approx(0.106)
    assert chase_cap(0.985, 0.02, 0.06) == 0.99


def test_loss_limits():
    risk = RiskSettings(daily_loss_limit_pct=0.1, max_drawdown_pause_pct=0.3)
    assert daily_loss_exceeded(-101, 1000, risk)
    assert not daily_loss_exceeded(-99, 1000, risk)
    assert drawdown_exceeded(1000, 200, -170, risk)
    assert not drawdown_exceeded(1000, 200, 0, risk)


def test_book_walking():
    asks = [BookLevel(0.50, 100), BookLevel(0.52, 100), BookLevel(0.60, 100)]
    shares, spent = walk_asks(asks, usdc=80, max_price=0.55)
    assert spent == pytest.approx(80)
    assert shares == pytest.approx(100 + 30 / 0.52)
    shares, spent = walk_asks(asks, usdc=1000, max_price=0.55)
    assert spent == pytest.approx(102)
    assert liquidity_under(asks, 0.52) == pytest.approx(102)
    bids = [BookLevel(0.49, 50), BookLevel(0.45, 50)]
    sold, got = walk_bids(bids, shares=80, min_price=0.46)
    assert sold == 50 and got == pytest.approx(24.5)


async def test_paper_executor_charges_fees():
    book = OrderBook("a", bids=[BookLevel(0.49, 1000)], asks=[BookLevel(0.5, 1000)])
    fees = FeeInfo(True, 0.05, 1.0, True)
    fill = await PaperExecutor().buy("a", usdc=50, max_price=0.52, book=book, fees=fees)
    assert fill.ok and fill.shares == pytest.approx(100)
    assert fill.fee == pytest.approx(100 * 0.05 * 0.25)
    miss = await PaperExecutor().buy("a", usdc=50, max_price=0.45, book=book, fees=fees)
    assert not miss.ok and miss.error_code == "fak_not_filled"


def test_portfolio_lifecycle(tmp_path):
    db = Database(tmp_path / "p.db")
    pf = Portfolio(db)
    pf.ensure_paper_account(1000)
    m = make_market()
    pid = pf.record_buy("paper", wallet="0xA", asset_id=m.yes_token, market=m,
                        fill=Fill(True, shares=100, usdc=50, fee=1.0))
    pf.record_buy("paper", wallet="0xB", asset_id=m.yes_token, market=m,
                  fill=Fill(True, shares=100, usdc=60, fee=1.0))
    assert pf.paper_cash() == pytest.approx(888)
    pos = pf.position(pid)
    assert pos["shares"] == 200 and pos["cost"] == pytest.approx(112)
    assert set(pos["leaders"]) == {"0xa", "0xb"}
    # Leader A exits: sell exactly A's 100 shares at 0.70.
    result = pf.record_sell("paper", pid, Fill(True, shares=100, usdc=70, fee=0.5), wallet="0xA")
    assert result.realized == pytest.approx(70 - 0.5 - 51)
    assert not result.closed
    assert pf.lot(pid, "0xA")["shares"] == 0
    assert pf.lot(pid, "0xB")["shares"] == 100
    # The rest resolves as a win.
    realized = pf.settle("paper", pid, 1.0)
    assert realized == pytest.approx(100 - 61)
    pos = pf.position(pid)
    assert pos["status"] == "resolved" and pos["result"] == "won"
    assert pf.paper_cash() == pytest.approx(888 + 69.5 + 100)
    assert pf.realized_total("paper") == pytest.approx(18.5 + 39)
    stats = pf.leader_live_stats("paper", "0xA")
    assert stats["closed"] == 1 and stats["pnl"] == pytest.approx(18.5)
    assert stats["roi"] == pytest.approx(18.5 / 51)
    db.close()


def test_portfolio_exposure_and_reset(tmp_path):
    db = Database(tmp_path / "p.db")
    pf = Portfolio(db)
    pf.ensure_paper_account(500)
    m = make_market()
    pid = pf.record_buy("paper", wallet="0xA", asset_id=m.yes_token, market=m,
                        fill=Fill(True, shares=100, usdc=40, fee=0))
    pf.mark(pid, 0.5)
    snap = pf.exposure("paper", pf.paper_cash(), 500)
    assert snap.total_exposure == 40 and snap.equity == pytest.approx(460 + 50)
    assert snap.market_exposure[m.condition_id] == 40
    assert snap.leader_exposure["0xa"] == 40
    assert snap.bot_pnl_total == pytest.approx(10)
    pf.reset_paper(2000)
    assert pf.paper_cash() == 2000 and pf.open_positions("paper") == []
    db.close()


def test_settings_profiles(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    s = store.update({"risk_profile": "aggressive"})
    assert s.risk.per_trade_pct == RISK_PRESETS["aggressive"]["per_trade_pct"]
    s = store.update({"risk": {"bankroll_mode": "fixed", "allocated_bankroll_usdc": 300}})
    assert s.risk_profile == "aggressive"  # bankroll fields do not change the profile
    s = store.update({"risk": {"per_trade_pct": 0.033}})
    assert s.risk_profile == "custom"
    assert detect_profile(RiskSettings()) == "balanced"
    with pytest.raises(Exception):
        store.update({"risk": {"per_trade_pct": 5}})
    reloaded = SettingsStore(tmp_path / "s.json").settings
    assert reloaded.risk.per_trade_pct == 0.033
    assert Settings().filters.max_hours_to_resolution == 48
