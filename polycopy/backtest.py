"""Walk-forward backtest of PolyCopy's leader selection.

    polycopy-backtest --out report.md          # real Polymarket data
    polycopy-backtest --demo --out demo.md     # simulated market (offline)

1. Selection window (days -2N..-N): score every candidate exactly as discovery
   does, *without* knowing any result that settled after day -N.
2. Evaluation window (days -N..0): replay the selected leaders' trades as copies
   (same entry rules, slippage and fees) and measure the outcome.
3. Compare against baselines drawn from the same candidate pool:
     * copy every candidate equally
     * copy the top wallets by raw simulated PnL in the selection window
   The gap between "selected" and the baselines is what the scoring adds.

Caveat printed in the report: candidates come from today's leaderboards, which
favour wallets that did well recently. All three strategies share this bias, so
compare them with each other rather than reading absolute returns as a forecast.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time
from dataclasses import replace
from datetime import datetime, timezone

from polycopy.config import Settings
from polycopy.engine.discovery import sim_params_from_settings
from polycopy.engine.scoring import SimParams, SimResult, simulate_copy
from polycopy.models import MarketInfo, TradeEvent

DAY = 86400.0


def _as_of(markets: dict[str, MarketInfo], cutoff: float) -> dict[str, MarketInfo]:
    """Hide results that settled after ``cutoff`` (no look-ahead in selection)."""
    out = {}
    for cid, m in markets.items():
        settled = m.closed_ts or m.end_ts or 0
        if m.closed and settled > cutoff:
            out[cid] = replace(m, closed=False, active=True)
        else:
            out[cid] = m
    return out


def _combine(results: list[SimResult]) -> dict:
    positions = [p for r in results for p in r.positions if p.status in ("sold", "resolved")]
    invested = sum(p.invested for p in positions)
    pnl = sum(p.realized for p in positions)
    wins = sum(1 for p in positions if p.realized > 0)
    peak = cum = dd = 0.0
    curve = []
    for p in sorted(positions, key=lambda p: p.closed_ts or 0):
        cum += p.realized
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        curve.append((p.closed_ts or 0, cum))
    returns = [p.realized / p.invested for p in positions if p.invested > 0]
    t = 0.0
    if len(returns) > 2:
        mean = sum(returns) / len(returns)
        sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
        t = mean / sd * math.sqrt(len(returns)) if sd > 0 else 0.0
    return {
        "positions": len(positions), "invested": invested, "pnl": pnl,
        "roi": pnl / invested if invested else 0.0,
        "win_rate": wins / len(positions) if positions else 0.0,
        "max_drawdown": dd, "t_stat": t, "curve": curve,
    }


def _spark(curve: list[tuple[float, float]], width: int = 48) -> str:
    if len(curve) < 2:
        return ""
    values = [v for _, v in curve]
    step = max(1, len(values) // width)
    values = values[::step][-width:]
    lo, hi = min(values), max(values)
    blocks = "▁▂▃▄▅▆▇█"
    if hi - lo < 1e-9:
        return blocks[3] * len(values)
    return "".join(blocks[int((v - lo) / (hi - lo) * (len(blocks) - 1))] for v in values)


async def run_backtest(data, settings: Settings, *, window_days: int = 30,
                       progress=print) -> str:
    now = time.time()
    cutoff = now - window_days * DAY
    d = settings.discovery
    params: SimParams = sim_params_from_settings(settings)
    params.lookback_days = window_days

    progress("Reading leaderboards...")
    rows = {}
    for window in d.leaderboard_windows or ["month"]:
        for category in [None, *d.leaderboard_categories]:
            try:
                board = await data.leaderboard(window=window, category=category,
                                               limit=d.leaderboard_depth)
            except Exception as error:  # keep going with what we have
                progress(f"  leaderboard {window}/{category or 'all'} failed: {error}")
                continue
            for row in board:
                if row.pnl > 0 and (row.wallet not in rows or row.rank < rows[row.wallet].rank):
                    rows[row.wallet] = row
    candidates = sorted(rows.values(), key=lambda r: r.rank)[: d.max_candidates]
    progress(f"{len(candidates)} candidate wallets. Downloading {2 * window_days} days of trades...")

    trades: dict[str, list[TradeEvent]] = {}
    sem = asyncio.Semaphore(4)

    async def fetch(wallet: str) -> None:
        async with sem:
            try:
                trades[wallet] = await data.user_trades(
                    wallet, since_ts=now - 2 * window_days * DAY,
                    max_items=d.max_trades_per_trader * 2,
                )
            except Exception as error:
                progress(f"  {wallet}: {error}")

    await asyncio.gather(*(fetch(c.wallet) for c in candidates))
    condition_ids = {t.condition_id for ts in trades.values() for t in ts if t.condition_id}
    progress(f"Looking up {len(condition_ids)} markets...")
    markets: dict[str, MarketInfo] = {}
    ids = list(condition_ids)
    for i in range(0, len(ids), 200):
        try:
            markets.update(await data.markets_by_condition(ids[i:i + 200], prefer_closed=True))
        except Exception as error:
            progress(f"  market lookup failed: {error}")
    selection_markets = _as_of(markets, cutoff)

    progress("Scoring the selection window and replaying the evaluation window...")
    scored = []
    for c in candidates:
        wallet_trades = trades.get(c.wallet, [])
        sel = simulate_copy(c.wallet, [t for t in wallet_trades if t.ts < cutoff],
                            selection_markets, params, now=cutoff)
        ev = simulate_copy(c.wallet, [t for t in wallet_trades if t.ts >= cutoff],
                           markets, params, now=now)
        scored.append((c, sel, ev))

    selected = sorted((x for x in scored if x[1].metrics.eligible
                       and x[1].metrics.score >= d.min_score),
                      key=lambda x: -x[1].metrics.score)[: d.max_followed]
    by_raw = sorted((x for x in scored if x[1].metrics.closed_positions > 0),
                    key=lambda x: -x[1].metrics.total_pnl)[: d.max_followed]
    everyone = [x for x in scored if x[1].metrics.closed_positions > 0]

    strategies = {
        "PolyCopy selection (score)": _combine([x[2] for x in selected]),
        "Top raw PnL (no scoring)": _combine([x[2] for x in by_raw]),
        "Every candidate": _combine([x[2] for x in everyone]),
    }
    return _report(settings, window_days, now, candidates, selected, strategies, scored)


def _fmt_usd(v: float) -> str:
    return ("-$" if v < 0 else "$") + f"{abs(v):,.0f}"


def _report(settings, window_days, now, candidates, selected, strategies, scored) -> str:
    when = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stake = settings.discovery.sim_stake_usdc
    lines = [
        "# PolyCopy walk-forward backtest",
        "",
        f"Generated {when}. Selection window: days -{2 * window_days} to -{window_days}; "
        f"evaluation window: the last {window_days} days.",
        f"Each copied entry is a ${stake:,.0f} stake filled {settings.discovery.sim_slippage_cents * 100:.1f}¢ "
        "worse than the leader, with Polymarket taker fees, mirrored exits and settlement at "
        f"resolution. Only markets resolving within {settings.filters.max_hours_to_resolution:.0f}h "
        "of the trade are copied.",
        "",
        "## Results in the evaluation window",
        "",
        "| Strategy | Leaders | Positions | Invested | P&L | ROI | Win rate | Max drawdown | t-stat | Curve |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    counts = {"PolyCopy selection (score)": len(selected),
              "Top raw PnL (no scoring)": min(len(scored), settings.discovery.max_followed),
              "Every candidate": sum(1 for x in scored if x[1].metrics.closed_positions > 0)}
    for name, s in strategies.items():
        lines.append(
            f"| {name} | {counts[name]} | {s['positions']} | {_fmt_usd(s['invested'])} | "
            f"{_fmt_usd(s['pnl'])} | {s['roi']:+.1%} | {s['win_rate']:.0%} | "
            f"{_fmt_usd(s['max_drawdown'])} | {s['t_stat']:.2f} | `{_spark(s['curve'])}` |"
        )
    lines += [
        "",
        "## Leaders PolyCopy would have followed",
        "",
        "| Leader | Selection score | Selection ROI | Skill z | Eval positions | Eval P&L | Eval ROI |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for c, sel, ev in selected:
        name = c.name or c.wallet[:10]
        lines.append(
            f"| {name} (`{c.wallet[:8]}…`) | {sel.metrics.score:.0f} | {sel.metrics.roi:+.1%} | "
            f"{sel.metrics.skill_z:.2f} | {ev.metrics.closed_positions} | "
            f"{_fmt_usd(ev.metrics.total_pnl)} | {ev.metrics.roi:+.1%} |"
        )
    if not selected:
        lines.append("| (no wallet met the minimum score) | | | | | | |")
    rejected: dict[str, int] = {}
    for _, sel, _ in scored:
        if not sel.metrics.eligible and sel.metrics.reason:
            key = sel.metrics.reason.split(" (")[0]
            for marker in ("only ", "not profitable", "orders/day"):
                if marker in key:
                    key = {"only ": "too few copyable positions or short-dated volume",
                           "not profitable": "not profitable after copy costs",
                           "orders/day": "bot / market-maker frequency"}[marker]
                    break
            rejected[key] = rejected.get(key, 0) + 1
    lines += ["", "## Why candidates were rejected", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(rejected.items(), key=lambda kv: -kv[1])] or ["- none"]
    lines += [
        "",
        "## How to read this",
        "",
        "- Positions are counted once per market outcome, at a fixed stake, so leaders with "
        "very different bankrolls are comparable.",
        "- Candidates come from *today's* leaderboards, which favour wallets that did well "
        "recently. Every strategy above shares that bias, so the useful comparison is between "
        "rows, not the absolute return.",
        "- A t-stat above ~2 means the evaluation-window result is unlikely to be luck.",
        "- Past results of copied traders do not guarantee future results.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="polycopy-backtest",
                                     description="Walk-forward backtest of PolyCopy's selection")
    parser.add_argument("--out", default="polycopy-backtest.md")
    parser.add_argument("--days", type=int, default=30, help="length of each window in days")
    parser.add_argument("--demo", action="store_true", help="use the offline simulated market")
    parser.add_argument("--candidates", type=int, default=None, help="max wallets to evaluate")
    args = parser.parse_args(argv)

    from polycopy.config import SettingsStore

    settings = SettingsStore().settings
    if args.candidates:
        settings = settings.model_copy(update={"discovery": settings.discovery.model_copy(
            update={"max_candidates": args.candidates})})

    async def go() -> str:
        if args.demo:
            from polycopy.gateway.simulated import SimData, SimWorld

            data = SimData(SimWorld(seed=21, n_wallets=120, history_days=2 * args.days))
        else:
            from polycopy.gateway.polymarket import PolymarketData

            data = PolymarketData()
        try:
            return await run_backtest(data, settings, window_days=args.days)
        finally:
            await data.close()

    report = asyncio.run(go())
    if args.demo:
        report = report.replace(
            "# PolyCopy walk-forward backtest",
            "# PolyCopy walk-forward backtest — SIMULATED MARKET (demo data, not real results)", 1)
    with open(args.out, "w") as handle:
        handle.write(report)
    print(report)
    print(f"\nSaved to {args.out}")


if __name__ == "__main__":
    main()
