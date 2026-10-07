from __future__ import annotations

import random
import time

import pytest

from polycopy.engine.fees import fee_per_dollar, taker_fee
from polycopy.engine.rules import EntryRules, check_entry
from polycopy.engine.scoring import (
    SimParams,
    aggregate_orders,
    simulate_copy,
    wilson_lower,
)
from polycopy.models import FeeInfo, MarketInfo, TradeEvent

W = "0x" + "ab" * 20
NOW = 1_800_000_000.0
DAY = 86400.0


def market(i: int, *, start: float, hours: float, yes_wins: bool | None, yes_price=0.5,
           category="sports") -> MarketInfo:
    resolved = yes_wins is not None
    return MarketInfo(
        condition_id=f"0x{i:064x}", question=f"Q{i}", yes_token=f"y{i}", no_token=f"n{i}",
        yes_price=(1.0 if yes_wins else 0.0) if resolved else yes_price,
        no_price=(0.0 if yes_wins else 1.0) if resolved else 1 - yes_price,
        end_ts=start + hours * 3600, closed=resolved, category=category,
        closed_ts=start + hours * 3600 + 600 if resolved else None,
        fees=FeeInfo(True, 0.05, 1.0, True),
    )


def buy(i, ts, price, usdc, token=None, tx=None):
    return TradeEvent(wallet=W, asset_id=token or f"y{i}", condition_id=f"0x{i:064x}", side="BUY",
                      price=price, size=usdc / price, ts=ts, tx_hash=tx or f"0x{i:064x}{int(ts)}")


def sell(i, ts, price, size, token=None):
    return TradeEvent(wallet=W, asset_id=token or f"y{i}", condition_id=f"0x{i:064x}",
                      side="SELL", price=price, size=size, ts=ts, tx_hash=f"0xs{i}{int(ts)}")


def book_of(wallet_edge: float, n: int = 60, seed: int = 1, hours: float = 10.0):
    """n resolved short-dated markets; the wallet buys YES at ~0.5 and wins with p=0.5+edge."""
    rng = random.Random(seed)
    trades, markets = [], {}
    for i in range(n):
        ts = NOW - rng.uniform(1, 29) * DAY
        wins = rng.random() < 0.5 + wallet_edge
        m = market(i, start=ts, hours=hours, yes_wins=wins)
        markets[m.condition_id] = m
        trades.append(buy(i, ts, 0.5, 200))
    return trades, markets


def test_fee_formula_peaks_at_half():
    fees = FeeInfo(True, 0.07, 1.0, True)
    assert taker_fee(100, 0.5, fees) == pytest.approx(1.75)
    assert taker_fee(100, 0.9, fees) == pytest.approx(0.63)
    assert taker_fee(100, 0.5, FeeInfo()) == 0
    assert fee_per_dollar(0.5, fees) == pytest.approx(0.035)


def test_wilson_lower_bound():
    assert wilson_lower(0, 0) == 0
    assert 0.5 < wilson_lower(70, 100) < 0.7
    assert wilson_lower(7, 10) < wilson_lower(70, 100)


def test_aggregate_orders_merges_fills_of_one_transaction():
    fills = [buy(1, NOW, 0.40, 40, tx="0xT"), buy(1, NOW + 1, 0.42, 42, tx="0xT"),
             buy(1, NOW + 2, 0.45, 45, tx="0xU")]
    merged = aggregate_orders(fills, 60)
    assert len(merged) == 2
    assert merged[0].size == pytest.approx(200)
    assert merged[0].usdc == pytest.approx(82)
    assert 0.40 < merged[0].price < 0.42


def test_skilled_wallet_scores_and_unskilled_does_not():
    good_trades, good_markets = book_of(0.20, seed=3)
    bad_trades, bad_markets = book_of(-0.10, seed=4)
    params = SimParams(min_resolved_positions=15)
    good = simulate_copy(W, good_trades, good_markets, params, now=NOW).metrics
    bad = simulate_copy(W, bad_trades, bad_markets, params, now=NOW).metrics
    assert good.eligible and good.score > 40
    assert good.skill_z > 2
    assert not bad.eligible and bad.score == 0
    assert "not profitable" in bad.reason


def test_slippage_and_fees_reduce_simulated_returns():
    trades, markets = book_of(0.10, seed=5)
    cheap = simulate_copy(W, trades, markets, SimParams(slippage=0.0), now=NOW).metrics
    costly = simulate_copy(W, trades, markets, SimParams(slippage=0.03), now=NOW).metrics
    assert costly.roi < cheap.roi


def test_long_dated_markets_are_not_copied():
    trades, markets = book_of(0.2, seed=6, hours=200)
    m = simulate_copy(W, trades, markets, SimParams(), now=NOW).metrics
    assert m.closed_positions == 0
    assert m.skip_reasons.get("resolves too late") == len(trades)
    assert not m.eligible


def test_sample_size_gate():
    trades, markets = book_of(0.3, n=8, seed=7)
    m = simulate_copy(W, trades, markets, SimParams(min_resolved_positions=15), now=NOW).metrics
    assert not m.eligible and "need 15" in m.reason


def test_mirrored_partial_sell_realizes_fraction():
    m = market(1, start=NOW - 5 * DAY, hours=20, yes_wins=True)
    trades = [buy(1, NOW - 5 * DAY, 0.40, 400), sell(1, NOW - 5 * DAY + 3600, 0.60, 500)]
    result = simulate_copy(W, trades, {m.condition_id: m}, SimParams(slippage=0.0), now=NOW)
    pos = result.positions[0]
    # half sold at 0.60, the other half resolves at 1.0
    shares = 100 / 0.40
    fee_in = taker_fee(shares, 0.40, m.fees)
    fee_out = taker_fee(shares / 2, 0.60, m.fees)
    expected = shares / 2 * 0.60 - fee_out + shares / 2 * 1.0 - (100 + fee_in)
    assert pos.status == "resolved"
    assert pos.realized == pytest.approx(expected, rel=1e-6)


def test_sell_without_known_entry_is_ignored():
    m = market(1, start=NOW - 2 * DAY, hours=20, yes_wins=False)
    trades = [sell(1, NOW - 2 * DAY, 0.6, 50)]
    result = simulate_copy(W, trades, {m.condition_id: m}, SimParams(), now=NOW)
    assert result.positions == []


def test_bot_like_frequency_is_rejected():
    trades, markets = book_of(0.2, n=60, seed=8)
    params = SimParams(max_trades_per_day=1.0)
    m = simulate_copy(W, trades, markets, params, now=NOW).metrics
    assert not m.eligible and "bot or market maker" in m.reason


def test_check_entry_rules():
    rules = EntryRules()
    now = time.time()
    m = MarketInfo(condition_id="0x1", question="Lakers vs Celtics", yes_token="a", no_token="b",
                   end_ts=now + 5 * 3600, game_start_ts=now - 60)
    assert check_entry(m, ts=now, price=0.5, leader_usdc=100, rules=rules) == "game already in progress (in-play)"
    m.game_start_ts = now + 3600
    assert check_entry(m, ts=now, price=0.5, leader_usdc=100, rules=rules) is None
    assert "outside" in check_entry(m, ts=now, price=0.97, leader_usdc=100, rules=rules)
    assert "below" in check_entry(m, ts=now, price=0.5, leader_usdc=5, rules=rules)
    m.end_ts = now + 600
    assert "too soon" in check_entry(m, ts=now, price=0.5, leader_usdc=100, rules=rules)
    m.end_ts = now + 100 * 3600
    m.game_start_ts = None
    assert "limit 48h" in check_entry(m, ts=now, price=0.5, leader_usdc=100, rules=rules)
    m.end_ts = now + 3600 * 5
    rules.excluded_keywords = ["lakers"]
    assert "excluded keyword" in check_entry(m, ts=now, price=0.5, leader_usdc=100, rules=rules)
    assert check_entry(None, ts=now, price=0.5, leader_usdc=100, rules=rules) == "market details unavailable"


def test_market_resolution_payout():
    m = MarketInfo(condition_id="0x1", question="q", yes_token="a", no_token="b",
                   yes_price=1.0, no_price=0.0, closed=True)
    assert m.resolution_payout("a") == 1.0 and m.resolution_payout("b") == 0.0
    m.yes_price, m.no_price = 0.5, 0.5
    assert m.resolution_payout("a") == 0.5
    m.yes_price, m.no_price = 0.93, 0.07
    assert m.resolution_payout("a") is None  # closed but not final
    m.closed = False
    m.yes_price, m.no_price = 1.0, 0.0
    assert m.resolution_payout("a") is None
    m.yes_alt = "alt-a"
    m.closed = True
    assert m.resolution_payout("alt-a") == 1.0
    again = MarketInfo.from_dict(m.to_dict())
    assert again.resolution_payout("alt-a") == 1.0


def test_sports_markets_use_kickoff_for_expected_resolution():
    now = time.time()
    m = MarketInfo(condition_id="0x1", question="Game", yes_token="a", no_token="b",
                   end_ts=now + 7 * 86400, game_start_ts=now + 3 * 3600)
    assert m.expected_resolution_ts() == pytest.approx(now + 9 * 3600)
    assert check_entry(m, ts=now, price=0.5, leader_usdc=100, rules=EntryRules()) is None
    plain = MarketInfo(condition_id="0x2", question="Q", yes_token="a", no_token="b",
                       end_ts=now + 7 * 86400)
    assert "limit 48h" in check_entry(plain, ts=now, price=0.5, leader_usdc=100, rules=EntryRules())
