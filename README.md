# PolyCopy

A self-hosted Polymarket copy-trading bot for macOS. It finds consistently profitable
traders, copies their trades in markets that settle within two days, and shows everything in
a web terminal.

![PolyCopy dashboard (demo data)](docs/images/dashboard.png)

- **Finds leaders worth copying.** It replays every candidate's last 30 days *as a copier
  would have traded them*: late, at a worse price, after Polymarket's taker fees. It then
  scores them on profit, statistical evidence of skill (t-test plus a calibration z-score)
  and consistency. Bots, market makers, lucky streaks and long-dated traders are filtered
  out.
- **Copies in seconds.** It watches the realtime trade stream plus polling, merges an
  order's fills into one signal, and places a price-capped fill-and-kill order. It never
  pays more than the leader's price + 2¢.
- **Only short-dated markets.** It only copies into markets that settle within 48 hours
  (configurable), at least 20 minutes out, and never into games already in progress.
- **Manages the whole lifecycle.** It mirrors leaders' exits proportionally, settles
  resolved markets and claims winnings automatically.
- **Risk controls.** Per-trade, per-market, per-leader and total exposure caps; a daily loss
  limit; a drawdown pause; and a location (geoblock) check before any live order.
- **Paper mode.** Simulated fills against the real order book, so you can verify results
  before risking money.
- **Web terminal.** Equity and P&L charts, open and closed positions, every order with its
  slippage, every signal with the reason it was copied or skipped, a trader leaderboard with
  drill-down, analytics, settings and logs.

## Quick start (macOS)

1. Download this repository (**Code → Download ZIP**) and unzip it.
2. Double-click **`Start PolyCopy.command`** (right-click → Open the first time).
3. The web terminal opens at `http://127.0.0.1:8765`. Log in with your Polymarket private
   key and wallet address, pick a risk profile, and start in **Paper** mode.

Want to look around first? Double-click **`Start PolyCopy Demo.command`** for a simulated
market with fake money.

## Documentation

- **[User Guide](docs/USER_GUIDE.md)**: installation, login, every screen and setting, daily
  operation, troubleshooting.
- **[Strategy](docs/STRATEGY.md)**: the research, the scoring maths, filters, sizing, exits,
  risk controls, a worked example, and the walk-forward backtest that demonstrates the
  strategy.

## For developers

```bash
uv sync --locked --python 3.12 --extra dev
uv run pytest -q                       # unit + integration tests (Polymarket API mocked)
uv run polycopy --demo                 # run against the simulated market
uv run polycopy-backtest --out r.md    # walk-forward backtest on real data
scripts/build_macos_app.sh             # standalone macOS executable via PyInstaller
```

Layout:

| Path | Contents |
|---|---|
| `polycopy/gateway/` | Polymarket access. Built on the official `polymarket-client` SDK, plus the realtime trade stream and a simulated market. |
| `polycopy/engine/` | Scoring, discovery, watcher, risk, execution, portfolio ledger and the bot orchestrator. |
| `polycopy/web/` | FastAPI server and the static web terminal (no build step). |
| `polycopy/backtest.py` | Walk-forward backtest. |
| `tests/` | Test suite, including SDK request/response round-trips and order signing against mocked Polymarket endpoints. |

> Trading prediction markets can lose money. Past performance of copied traders does not
> guarantee future results. Only use PolyCopy where Polymarket is legally available to you.
