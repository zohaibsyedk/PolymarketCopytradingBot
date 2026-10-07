# PolyCopy — The Strategy

This document explains how PolyCopy decides **whom to copy, what to copy, how much to
stake and when to get out**, the research behind each decision, and how to check that the
strategy works before you risk money on it.

> **Honest expectations.** Most Polymarket traders lose money. A small group is genuinely
> skilled, and the evidence says that skill persists. Copying it profitably is still hard:
> you are always a few seconds late, and you pay a slightly worse price plus fees. PolyCopy's
> whole design is about **keeping as much of the leaders' edge as possible after those
> costs**, and refusing trades where the edge is gone. That raises your odds. It does not
> guarantee profit.

---

## Contents

1. [What the research says](#1-what-the-research-says)
2. [Design principles drawn from it](#2-design-principles-drawn-from-it)
3. [The pipeline at a glance](#3-the-pipeline-at-a-glance)
4. [Scoring leaders](#4-scoring-leaders)
5. [Entry filters: what gets copied](#5-entry-filters-what-gets-copied)
6. [Position sizing](#6-position-sizing)
7. [Exits, settlement and claiming](#7-exits-settlement-and-claiming)
8. [Risk controls](#8-risk-controls)
9. [Demonstrating the strategy: walk-forward backtest](#9-demonstrating-the-strategy-walk-forward-backtest)
10. [A worked example, end to end](#10-a-worked-example-end-to-end)
11. [What can go wrong](#11-what-can-go-wrong)
12. [Recommended rollout](#12-recommended-rollout)
13. [References](#13-references)

---

## 1. What the research says

| Finding | Source | Consequence for PolyCopy |
|---|---|---|
| **Most traders lose.** Of 95M Polymarket transactions (Apr 2024 – Dec 2025), only ~0.5% of wallets made more than $1,000; another analysis finds ~84% of traders lost money. | Industry on-chain analyses (refs 4, 5) | Copying *random* or merely *active* traders is a losing strategy. Selection is everything. |
| **Skill persists.** About 44% of accounts classified as skilled in one sample stayed skilled out of sample, versus ~10% persistence for mutual funds. Skilled accounts (~3% of all) earned persistent profits. | Reichenbach & Walther, *Accuracy, Skill, and Bias on Polymarket* (ref 1) | Past performance carries real information here, so copy trading is not hopeless. |
| **Raw profit is mostly luck.** Among the biggest winners by profit, only about 12% beat a benchmark of random-outcome simulations of their own trades. | Coverage of the same research (ref 2) | Rank traders on **statistical evidence of skill**, not on leaderboard P&L. |
| **Informativeness is a persistent wallet trait.** Wallets whose trades are followed by price moves in their direction keep that ranking across 10-day windows (rank correlation 0.52), and their identity predicts short-horizon returns out of sample. | Zhai, *Public Trader Identity: Adverse Selection and Return Predictability* (ref 3) | Watching specific wallets' trades in real time is a legitimate signal. |
| **The edge is short-lived.** The predictive power is strongest over seconds to minutes. | Zhai (ref 3) | Speed matters: detect in seconds (realtime feed) and refuse to chase a price that already moved. |
| **Winners often provide liquidity; losers take it.** Successful traders frequently use limit orders; unsuccessful ones use market orders. Profits concentrate in <1% of wallets. | Reichenbach & Walther (ref 1); Solidus Labs via CoinDesk (ref 6) | Market makers' profit comes from the spread, which a copier pays rather than earns. **Exclude market makers and high-frequency bots.** |
| **Copying costs are structural.** Slippage is not fixable by speed alone: the leader's own trade moves the price, and in thin books a copier becomes the leader's exit liquidity. | Practitioner analyses (refs 7, 8) | Cap the price paid relative to the leader. Require liquidity. Size small relative to the book. |
| **In-play sports and late copies destroy the edge.** A public paper copy-trading experiment went from −$10/trade (38.7% win rate) to +$26/trade (46.9%) after skipping entries after game start. It also used a price band of 3–97¢ and a max-chase rule. | BallesJr/polymarket-copy-trader (ref 7) | Skip in-play sports by default. Use a price band and a chase cap. |
| **Ultra-short crypto markets are bot territory.** 5- and 15-minute "Up or Down" markets are dominated by bots reacting to oracle updates in milliseconds. | Market commentary (ref 9) | Require at least 20 minutes to resolution. |
| **Taker fees exist (since 2026).** Fee = `shares × rate × p × (1 − p)`. Rates: crypto 0.07, sports 0.05, politics/finance/tech 0.04, geopolitics 0. The fee peaks at 50¢ ($1.75 per 100 crypto shares). | Polymarket fee schedule (refs 10, 11) | Every simulated and paper fill includes fees, so scores and results are net of them. |

---

## 2. Design principles drawn from it

1. **Score the copy, not the trader.** A trader is only worth following if *you* would have
   made money copying them: late, at a worse price, after fees, under your own filters.
   Every score in PolyCopy comes from replaying the trader's history that way.
2. **Demand statistical evidence of skill.** Use sample-size floors, shrinkage toward zero
   and two independent significance tests. A lucky streak must not qualify.
3. **Only copy what is copyable:**
   - markets settling within your window (48h);
   - not ultra-short markets;
   - not games in progress;
   - not extreme prices;
   - not trades where the price already ran away.
4. **Size for survival.** Use small, capped stakes (a fraction of a Kelly bet) with
   diversification across markets and leaders, plus daily-loss and drawdown brakes.
5. **Follow the leader out, too.** Leaders' exits carry information as well. Mirror them
   proportionally.
6. **Keep learning.** Track every leader's *live* copy results, and bench the ones that stop
   working.

---

## 3. The pipeline at a glance

```
 Leaderboards ──► Candidates ──► Copy-replay of 30 days ──► Score ──► Follow top N
 (week/month,                    (same filters, +1¢ slippage,          (hysteresis,
  overall+sports+crypto)          taker fees, mirrored exits)           benching)
                                                                            │
 Realtime trade feed + polling ◄────────────────────────────────────────────┘
        │  (dedupe, group fills of one order into one signal)
        ▼
 Signal ──► Entry filters ──► Order book check ──► Size ──► FAK order with price cap
                │ skip (logged with reason)                        │
                ▼                                                  ▼
          Signals tab                       Position (lot per leader) ──► mirror exits
                                                                  │      reconcile
                                                                  ▼
                                               Resolution ──► settle ──► auto-claim
                                                                  │
                                         Live per-leader results ─┘──► bench poor leaders
```

---

## 4. Scoring leaders

### 4.1 Candidates

Every 6 hours PolyCopy reads the profit leaderboards for the **week** and **month**
windows, overall and for the **sports** and **crypto** categories (configurable). That gives
a pool of up to ~300 wallets with positive recent profit. Wallets you follow manually, or
already follow, are always re-scored.

### 4.2 The copy replay

For each candidate, PolyCopy downloads all of their trades (maker and taker) from the last
30 days and replays them as a copier:

- **Fills of one order are merged into one decision**, by transaction hash or by fills
  within 60 seconds. One leader order therefore produces one copied stake, however many
  pieces it filled in.
- **Each leader BUY** goes through **exactly the same entry rules** as live trading
  (section 5), evaluated at the time of the trade. If it passes, the copier buys a fixed
  **$100** at the leader's price **+1¢** (simulated slippage), pays the market's taker fee,
  and records the position. At most 3 entries are copied per outcome.
- **Each leader SELL** makes the copier sell the same *fraction* of its position (leader
  sold 25% of their shares, copier sells 25%), at the leader's price −1¢, minus fees.
- **At the end**, positions in resolved markets settle at $1 or $0 per share (or $0.50 on a
  50-50 resolution). Positions still open are marked at the current price and excluded from
  the statistics.

A fixed stake makes a $50-a-trade wallet and a $50,000-a-trade wallet directly comparable.

### 4.3 Metrics

Computed over the copier's **closed** positions (sold or resolved):

| Metric | Formula / meaning |
|---|---|
| ROI | `Σ realized P&L / Σ invested` |
| Shrunk ROI | `Σ P&L / (Σ invested + 10 × stake)`. Ten phantom break-even positions pull small samples toward zero. |
| Win rate (+ Wilson lower bound) | Share of positions with positive P&L, and its 80% lower bound. |
| t-stat | `mean(r) / sd(r) × √n` over per-position returns `r`. |
| **Skill z (calibration test)** | For positions held to resolution, a share bought at price `p` wins with probability `p` if the trader has no skill. `z = (wins − Σp) / √Σp(1−p)`. This is the "beat random outcome simulations" test from the research. |
| Profitable days | Share of active days with positive realized P&L. |
| Halves | P&L in the first and second half of the window; both should be positive. |
| Concentration | Biggest single win ÷ total winnings. |
| Max drawdown | Largest peak-to-trough drop of the cumulative copy P&L. |
| Orders/day | After merging fills. Very high means bot or market maker. |
| Short-dated share | Share of the trader's buy volume that passes your filters. |

### 4.4 Gates (all must pass)

- at least **15** closed copyable positions;
- no more than **150** orders per day (excludes bots and market makers);
- at least **25%** of the trader's volume in markets you would copy;
- **shrunk ROI > 0**: profitable *after* slippage and fees.

### 4.5 Score (0–100)

```
confidence = ½·clamp(t/3, 0, 1) + ½·clamp(z/2.5, 0, 1)
raw = shrunk_ROI
      × (0.35 + 0.65·confidence)                  # evidence of skill
      × (0.6 + 0.4·profitable_days)               # consistency
      × (1.0 if both halves > 0 else 0.65)        # stability over time
      × (1 − 0.6·clamp((concentration − 0.35)/0.65, 0, 1))   # not one lucky win
      × (1 − 0.5·clamp(max_drawdown / (10·stake), 0, 1))     # smooth equity
      × (0.5 if inactive > 7 days) × (0.85 if last 7 days negative)
score = 100 · (1 − e^(−raw / 0.04))
```

A copier edge of ~2% per position after penalties scores ~40 (the default follow bar); 4%
scores ~63; 8% scores ~86.

### 4.6 Choosing whom to follow

- Manual follows are always followed. Blocked wallets never are.
- **Hysteresis:** a followed leader stays followed while its score is at least
  `min_score − 10`, so leaders don't flip in and out between scans.
- Free slots go to the highest-scoring eligible wallets with score ≥ 40, up to 12 leaders.
- **Live feedback:** after at least 8 closed *live* copies, a leader whose copies returned
  ≤ −15% is **benched for 7 days**. Past performance earns a slot; current performance keeps
  it.

---

## 5. Entry filters: what gets copied

Every detected leader BUY passes these checks, in this order. Each skip is recorded with its
reason in the Signals tab.

| Check | Default | Why |
|---|---|---|
| Bot running and not paused | — | Pauses come from you, the daily loss limit, the drawdown brake or location restrictions. |
| Signal age | ≤ 120 s | Old information is already priced in. |
| Market open and accepting orders | — | |
| **Resolves within the window** | ≤ 48 h, ≥ 20 min | Your two-day rule. For sports, settlement is expected at kickoff + 6h when that is earlier than the market's nominal end date (sports markets often carry an end date days after the game). Markets under 20 minutes from settlement are bot territory. |
| Entry price band | 5¢ – 94¢ | Long shots have high variance and are often noise. Near-certainties leave too little upside after fees and slippage. |
| Leader trade size | ≥ $25 | Tiny trades carry little conviction. |
| Not in-play sports | on | The research's strongest copy filter. |
| Excluded keywords | — | Optional, for example `Up or Down`. |
| Market liquidity | ≥ $500 | |
| No conflicting position | — | Never hold both outcomes of a market. When two leaders disagree, the first signal wins. |
| Daily loss limit | 12% | |
| **Order book: spread** | ≤ 6¢ | Wide spreads mean thin books and expensive exits. |
| **Order book: chase cap** | best ask ≤ min(leader + 2¢, leader × 1.06) | **The single most important protection.** If the price already ran past what the leader paid, most of the edge has gone to whoever was faster. |
| Stake after risk limits | ≥ $2 | Section 6. |
| Liquidity under the cap | ≥ stake | The stake is cut to what is available below the cap. |

Orders are **FAK (fill-and-kill) market orders with a price cap**. They take whatever is
available at or below the cap and cancel the rest. PolyCopy never chases and never leaves
resting orders behind.

### Why the chase cap matters so much

If the leader's information makes the true probability `q` and the copier pays `c`, the
expected profit per $1 held to resolution is `q/c − 1 − fee`. With a leader 5 points better
than a 50¢ market (q = 0.55) and a 5% sports fee rate:

| Copier pays | Slippage vs leader | Expected return per $ |
|---|---|---|
| 50¢ | 0¢ | **+7.5%** |
| 51¢ | 1¢ | +5.4% |
| 52¢ | 2¢ | +3.4% |
| 53¢ | 3¢ | +1.4% |
| 55¢ | 5¢ | **−2.3%** |

Each cent of slippage costs about two points of return. That is why PolyCopy refuses to pay
more than 2¢ above the leader by default, and why scoring assumes 1¢ of slippage on every
copy.

---

## 6. Position sizing

```
bankroll  = account equity              (or: fixed allocation + bot's own P&L, capped at equity)
base      = bankroll × 2%               (per_trade_pct; 1% conservative, 4% aggressive)
stake     = base
          × (0.6 + 0.8 × score/100)                      # better leaders get more   (0.6–1.4×)
          × clamp(0.5 + 0.5 × leader_size/leader_median, 0.5, 1.5)   # conviction (0.5–1.5×)
          × min(1.5, 1 + 0.25 × other_leaders_in_same_outcome)       # consensus  (1–1.5×)
          × (1 if price ≥ 25¢ else 0.5 + 2 × price)                  # damp long shots
then capped by:  max order ($150) · per-market 8% · per-leader 30% · total invested 75%
                 · cash minus 5% reserve · liquidity under the price cap
skip if the result is below $2.
```

The base stake is deliberately small. For a leader with a 5-point edge at 50¢, the Kelly
fraction is roughly `(q − c)/(1 − c) ≈ 8%` of bankroll. PolyCopy's typical 1.5–3% is about a
quarter of that, a standard choice when the edge itself is estimated with error. Combined
with the per-market and per-leader caps, no single wrong call can do serious damage.

---

## 7. Exits, settlement and claiming

- **Mirrored exits.** Each position is split into **lots**, one per leader copied into it.
  When a leader sells, PolyCopy computes the fraction sold, `sold / shares held before`.
  "Shares held before" comes from PolyCopy's own tracking of that leader's trades, refreshed
  from the Data API. PolyCopy then sells the **same fraction of that leader's lot**, accepting
  up to 4¢ below the leader's price. If the leftover would be under $1, it sells the whole
  lot.
- **Merges** (a leader converting both outcomes back to USDC) are treated as exits.
- **No buyers?** The exit is marked *pending* and retried for 30 minutes. After that, the
  position is held to resolution rather than dumped at a terrible price.
- **Missed exits.** Every 2 minutes PolyCopy checks that each leader still holds what was
  copied from them. If a leader's holding has disappeared on two consecutive checks (for
  example they exited through a path the feed missed), the lot is exited.
- **Resolution.** When a market resolves, positions settle at the final payout. In live
  mode PolyCopy then **redeems** the winning shares back to USDC through Polymarket's
  gas-free relayer, using the builder key created at login.
- **Optional take-profit** (off by default) sells once the price reaches, say, 99¢. Waiting
  for resolution captures the last cent, and with markets settling within 48 hours capital is
  not tied up for long anyway.

---

## 8. Risk controls

| Control | Default | Effect |
|---|---|---|
| Per-copy cap | $150 | |
| Per-market cap | 8% of bankroll | Correlated adds can't concentrate risk. |
| Per-leader cap | 30% | A leader going cold hurts at most 30% of your book. |
| Total invested | 75% | Keeps cash for exits, fees and new signals. |
| Cash reserve | 5% | |
| Max open positions | 30 | |
| **Daily loss limit** | −12% of start-of-day equity | No new entries until the next UTC day. Exits continue. |
| **Drawdown pause** | −30% from peak bot equity | Pauses new entries until you review and resume. |
| **Location check** | always | No live orders if Polymarket reports your location as restricted. |
| **Close-only accounts** | always | No new entries. |
| **Benching** | ROI ≤ −15% after 8 live copies | Removes leaders that stopped working. |

---

## 9. Demonstrating the strategy: walk-forward backtest

A backtest that picks leaders and measures them on the same period always looks great,
because you are just picking the lucky ones. PolyCopy's backtest avoids that:

1. **Selection window** (days −60 to −30). Score every candidate *as if it were day −30*.
   Market results that settled after day −30 are hidden from the selection, so it cannot see
   the future.
2. **Evaluation window** (the last 30 days). Replay the selected leaders' trades as copies,
   with the same rules, slippage and fees.
3. **Baselines** from the same candidate pool: *copy every candidate*, and *copy the top
   wallets by raw P&L* (no scoring).

Run it on real data from your Mac:

```bash
uv run polycopy-backtest --out my-backtest.md
```

### Example output (simulated market, NOT real results)

The table below comes from PolyCopy's built-in **simulated** market. It contains 120 fake
wallets, about 12% of which have genuine skill, and runs fully offline. It demonstrates that
the machinery works: it finds the skilled wallets using only older data, and copying them
pays off in a later period it never saw. **It says nothing about real-world returns. Run the
command above to get real numbers.**

| Strategy | Leaders | Positions | P&L | ROI | Win rate | t-stat |
|---|---:|---:|---:|---:|---:|---:|
| PolyCopy selection (score) | 12 | 1,886 | +$36,789 | **+19.0%** | 61% | 6.70 |
| Top raw P&L (no scoring) | 12 | 1,937 | +$34,987 | +17.6% | 60% | 6.26 |
| Every candidate | 95 | 9,870 | −$22,037 | **−2.2%** | 50% | −1.93 |

Copying everyone on the leaderboard **loses money** once slippage and fees are counted. A
selected group, chosen using only older data, stays profitable in the period that follows.
In this simulation, skill is simple, so raw P&L is almost as good a selector as the score.
In real markets, where most top P&L is luck, the statistical tests in the score do more of
the work. That is the comparison to look for in your real-data report.

### How to read a real report

- The selection should beat both baselines in ROI and t-stat. If raw P&L does as well,
  scoring isn't adding much in the current market.
- A **t-stat above ~2** for the selection row means the result is unlikely to be luck.
- All rows share one bias: candidates come from *today's* leaderboards, which favour wallets
  that did well recently, including during the evaluation window. Compare the **rows with each
  other**. Don't read the absolute ROI as a forecast.
- Few positions in the evaluation window means your filters are strict. That's fine, but the
  numbers will be noisier.

Then confirm with **paper trading**. Paper fills come from the live order book, so they
also capture real latency and slippage, which the backtest only approximates with a flat
1¢.

---

## 10. A worked example, end to end

*Paper mode, $1,000 equity, $600 cash, $400 already invested (of which $100 is copied from
this leader), Balanced profile.*

1. **Detection.** Leader *L* (score 80, median trade $300) buys $600 of **"Knicks to beat
   the Celtics — Yes"** at **57¢**. The game starts in 3 hours. The realtime feed delivers
   the trade in about 1 second. Its three fills share one transaction hash and become one
   signal.
2. **Filters.**
   - Expected settlement is kickoff + 6h ≈ 9h away: within 48h and more than 20 minutes.
   - 57¢ is inside the 5–94¢ band, and $600 ≥ $25.
   - The game is not in progress, liquidity is fine and there is no conflicting position.
3. **Order book.** Best ask is 58¢; the spread is 2¢.
   Cap = min(57 + 2, 57 × 1.06) = **59¢**. 58¢ ≤ 59¢, so continue.
4. **Size.**
   - base = $1,000 × 2% = $20;
   - leader multiplier = 0.6 + 0.8 × 0.80 = 1.24;
   - conviction: $600 / $300 = 2× the leader's median trade, giving 1.5;
   - consensus 1.0, price factor 1.0.
   - stake = 20 × 1.24 × 1.5 = **$37.20**, well inside every cap.
5. **Order.** FAK buy of $37.20 capped at 59¢. It fills 40 shares at 58¢ and 23.7 shares at
   59¢: **63.73 shares at an average 58.4¢**. Sports taker fee:
   63.73 × 0.05 × 0.584 × 0.416 ≈ **$0.77**.
6. **Break-even.** ($37.20 + $0.77) / 63.73 = **59.6%**. The copy makes money if the Knicks
   win more than 59.6% of the time. The leader's selection-period skill z and win rate are
   what justify believing that.
7. **Outcomes.**
   - **Leader sells half at 70¢ before tip-off.** PolyCopy sells 50% of the lot, ~31.9
     shares, at ≥ 66¢.
   - **The Knicks win.** The remaining shares pay $1 each and are claimed automatically.
     Held fully to a win: +$25.75. Held fully to a loss: −$37.97.

---

## 11. What can go wrong

- **Regime change.** A leader's edge can vanish: the sport's season ends, competitors
  catch up, or their information source dries up. *Mitigation:* rescoring every 6h, live
  benching, diversification across 12 leaders.
- **Being the exit liquidity.** A leader who knows they are copied could buy, let copiers
  push the price up, then sell into them. *Mitigation:* the chase cap, small stakes relative
  to the book, liquidity minimums, mirrored exits, and the 7-day bench.
- **Latency spikes.** If the realtime feed drops, polling adds a few seconds of delay.
  *Mitigation:* the chase cap means late copies are skipped rather than overpaid.
- **Resolution risk.** Disputed or delayed UMA resolutions can hold capital past 48h.
  Positions settle when the result is final.
- **Platform changes.** Polymarket's APIs, contracts and fees change; 2026 alone brought fees,
  a new exchange version and deposit wallets. PolyCopy uses Polymarket's official
  `polymarket-client` SDK (pinned) for all trading so that signing and contract details stay
  correct. Keep PolyCopy updated.
- **Correlation.** Several leaders may pile into the same game. *Mitigation:* the per-market
  cap and conflict rules.
- **Your own setup.** The Mac sleeping, internet outages, or running out of USDC. See the
  User Guide.

---

## 12. Recommended rollout

1. **Backtest** on real data (`polycopy-backtest`). Look for the selection beating both
   baselines.
2. **Paper trade** for 3–7 days, or until you have 50+ closed positions. Check:
   - positive realized P&L after fees;
   - average slippage ≲ 1¢;
   - profit coming from several leaders, not one.
3. **Go live small**: fixed bankroll, Conservative or Balanced. Check that live fills match
   paper fills.
4. **Scale gradually**, and only while live results keep tracking paper results.
5. **Review weekly**: the leader scorecard, skip reasons and slippage. Adjust one setting at
   a time.

---

## 13. References

1. F. Reichenbach & M. Walther, *Exploring Decentralized Prediction Markets: Accuracy, Skill,
   and Bias on Polymarket*, SSRN 5910522. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5910522
2. *Study: A tiny elite sets Polymarket's prices while most users lose money*, Jerusalem
   Post. https://www.jpost.com/science/article-894967
3. D. Zhai, *Public Trader Identity: Adverse Selection and Return Predictability*,
   arXiv 2608.04373. https://arxiv.org/abs/2608.04373
4. *Are Polymarket Trading Bots Actually Profitable? The Math Behind 2026's Prediction-Market
   Arbitrage Industry.* https://1023jack.com/market/are-polymarket-trading-bots-actually-profitable-the-math-behind-2026-s-predictio/
5. *Polymarket Strategies: 2026 Guide*, Cryptonews. https://cryptonews.com/cryptocurrency/polymarket-strategies/
6. *A tiny group is winning on Polymarket as under 1% of wallets take half the profits*,
   CoinDesk, 2026-04-29. https://www.coindesk.com/markets/2026/04/29/a-tiny-group-is-winning-on-polymarket-as-under-1-of-wallets-take-half-the-profits
7. BallesJr, *polymarket-copy-trader*: paper copy-trading experiment measuring delay,
   slippage and P&L. https://github.com/BallesJr/polymarket-copy-trader
8. *Polymarket Copy Trading Bot: How Traders Find Alpha by Mirroring Profitable Wallets*,
   QuantVPS. https://www.quantvps.com/blog/polymarket-copy-trading-bot
9. *Polymarket 5-Minute Markets*, SailGP prediction-markets guide. https://sailgp.com/prediction-markets/polymarket/5-minute-markets
10. *Polymarket Fees 2026: Calculator & Complete Cost Guide*, PredictionHunt. https://www.predictionhunt.com/blog/polymarket-fees-complete-guide
11. *Polymarket Fees Explained (2026): Category Fee Schedule*, River Markets. https://www.rivermarkets.com/insights/polymarket-fees.html
12. Polymarket official Python SDK (`polymarket-client`). https://github.com/Polymarket/py-sdk
13. Polymarket real-time data client (activity/trades stream). https://github.com/Polymarket/real-time-data-client
