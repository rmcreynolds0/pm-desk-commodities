# System Reference

### Markets, contracts, signals, and the code that runs them

**QUANTT Commodities Desk · October 2026**

---

## What this document is

The operational reference for the live book: which markets it trades, what
those contracts actually are, what the signals measure, and which file does
what. It assumes you want to *work on* the system rather than evaluate it.

For the theory — why the futures curve carries information, derived from first
principles — see `05_STRATEGY_PAPER.md`. This document is the companion to
that one and does not repeat it.

---

# Part I · Motivation

## 1. Why this strategy exists, and what it replaced

The desk's first strategy was a natural-gas calendar-spread model driven by a
**Deliverability Stress Index** built from EIA storage data. The thesis: the US
gas system has tightened structurally since 2016, so when storage utilisation
runs far from normal the system is under stress and calendar spreads should
respond.

It was retired. Two independent reasons, and the order matters.

**Three bugs were manufacturing the profit.** Returns were computed across a
spliced contract series, so the price gap at every roll was booked as P&L —
measured at 8.1×, 6.6× and 14.4× a normal daily move, accounting for 31%, 27%
and 6% of all price movement. Slippage was subtracted into a cell the equity
recursion then overwrote, so every backtest ran cost-free. And the random
benchmark was a single seed: a +26.3% result that looked like signal turned out
to sit at the 95th percentile of 200 random agents, with 49% of seeds
profitable.

**The hypothesis itself was false.** Written as a falsifiable claim and tested
on 1,527 weekly observations: post-2016 volatility *fell* 29% (p=0.033) where
the thesis required a rise, seasonal amplitude was unchanged at 0.99×, and
mean reversion collapsed from +0.057 to +0.001. Every measure moved against it.

**What replaced it** is a cross-sectional strategy built on published,
independently replicated premia rather than a thesis invented in-house. That
choice was deliberate: after one falsified hypothesis, the bar for the next one
was that somebody else had already found it and others had reproduced it.

## 2. The design constraint that shapes everything

Every claim must beat a **random agent** running through identical machinery.
Not a single random path — a distribution of 200.

This is why the system is built as a **ladder** of four agents rather than one
model. Each rung sees exactly one more factor than the one below, so the gap
between adjacent rungs isolates that factor's contribution, and the gap above
the random rung measures whether there is any information at all.

| Agent | Sees | Sharpe 2000–2026 |
|---|---|---|
| `agent_0` | nothing — random scores | 0.08 |
| `agent_1` | carry | 0.24 |
| `agent_2` | carry + momentum | 0.36 |
| `agent_3` | carry + momentum + basis-momentum | **0.553** |

Monotonic, and every informed rung beats the null band.

---

# Part II · The signals

All three derive from the same two prices per market: the **front** contract
and the **second** contract. Three different questions asked of the same curve.

## 3. Carry — where the curve is now

$$\text{carry} = \frac{\ln(F_1 / F_2)}{\Delta t}$$

Positive means **backwardation** (front above deferred), implying physical
tightness and a positive expected roll return. Negative means **contango** and
a structural drag.

Dividing by $\Delta t$ annualises it. Markets list on different cycles, so a
one-month and a three-month spread are not comparable until normalised —
without this, the factor partly ranks markets by their listing calendar.

**Implementation:** `data/commodities.py :: add_factors`

## 4. Momentum — where the price has been going

Trailing 12-month compounded return of the front contract, built from
**roll-safe** daily returns.

**Implementation:** `data/commodities.py :: add_factors`, rolling 252-day
product of per-contract returns.

## 5. Basis-momentum — how the curve's shape has been changing

$$\text{basis-mom} = \text{mom}(F_1) - \text{mom}(F_2)$$

If the front has risen faster than the deferred, the curve has been steepening
toward backwardation — the market has been tightening, independent of its
current slope or absolute direction.

Empirically the strongest of the three, and the least widely traded. Boons &
Prado (2019).

**Implementation:** `data/commodities.py :: add_factors`

## 6. How they combine

Each factor is **z-scored across markets within each date** — grouping by date,
not ticker, is what makes this cross-sectional. The score is the equal-weighted
mean of the z-scores an agent is permitted to see.

Equal weighting is deliberate. Fitting factor weights on the same history used
to evaluate the strategy is in-sample optimisation. It also has a consequence
worth knowing: a fourth factor cuts every existing weight from ⅓ to ¼, so it
must beat the **average** of the incumbents to help.

**Implementation:** `agents/xsec.py :: _zscore_xs`, `score`

## 7. A signal that was tested and rejected

`data/commodities.py :: add_stress` ports the retired gas strategy's DSI
pipeline to the cross-section: curve-state extremity → convex transform →
smoothing → deseasonalisation → residualisation against carry.

It produces a genuinely orthogonal factor (cross-sectional correlations −0.10,
+0.03, +0.00 against the three incumbents) and adds +0.138 Sharpe as a *third*
factor — nearly basis-momentum's +0.152. As a *fourth* it costs 0.106.

Rejected against a rule written before the test. The code remains because it is
the evidence behind that rejection. See `STRESS_FACTOR.md`.

---

# Part III · The universe

## 8. Why 22 markets

Four sectors, chosen for breadth of economic driver rather than correlation
statistics: energy, metals, grains and oilseeds, softs, livestock. A
cross-sectional strategy needs enough names for a tercile to mean something —
the floor is six, below which each side holds fewer than two names.

A proposed narrowing to **17 markets** was tested and **rejected**: dropping
the five most capital-hungry names made carry stop beating chance (0.24 → 0.12)
and worsened drawdown from −36% to −53%. The account is sized to the universe,
not the universe to the account.

## 9. The contracts

Prices and notionals as measured from IBKR on 2026-08-12. **Notional is what
one contract controls** — the number that determines whether the book can hold
a position at all.

### Energy — NYMEX

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `CL` | Crude Oil (WTI) | 1,000 bbl | 83.17 | $83,170 |
| `NG` | Natural Gas | 10,000 MMBtu | 2.79 | $27,940 |
| `HO` | Heating Oil | 42,000 gal | 4.30 | $180,592 |
| `RB` | Gasoline RBOB | 42,000 gal | 3.14 | $131,699 |

### Metals — COMEX / NYMEX

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `GC` | Gold | 100 oz | 4,425.50 | **$442,550** |
| `SI` | Silver | 1,000 oz | 64.77 | $64,770 |
| `HG` | Copper | 25,000 lb | 6.63 | $165,775 |
| `PL` | Platinum | 50 oz | 1,745.80 | $87,290 |
| `PA` | Palladium | 100 oz | 1,366.00 | $136,600 |

### Grains and oilseeds — CBOT · *quoted in cents*

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `ZC` | Corn | 5,000 bu | 449.75¢ | $22,488 |
| `ZS` | Soybeans | 5,000 bu | 1,159.50¢ | $57,975 |
| `ZM` | Soybean Meal | 100 tons | 307.00 | $30,700 |
| `ZL` | Soybean Oil | 60,000 lb | 68.74¢ | $41,244 |
| `ZO` | Oats | 5,000 bu | 322.50¢ | **$16,125** |
| `KE` | Wheat (Hard Red Winter) | 5,000 bu | 722.75¢ | $36,138 |

### Softs — ICE US / NYBOT · *mostly quoted in cents*

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `CC` | Cocoa | 10 tons | 5,636.00 | $56,360 |
| `KC` | Coffee C | 37,500 lb | 339.65¢ | $127,369 |
| `SB` | Sugar No.11 | 112,000 lb | 16.62¢ | $18,614 |
| `CT` | Cotton No.2 | 50,000 lb | 83.15¢ | $41,575 |
| `OJ` | Orange Juice | 15,000 lb | 137.95¢ | $20,692 |

### Livestock — CME · *quoted in cents*

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `LE` | Live Cattle | 40,000 lb | 231.00¢ | $92,400 |
| `GF` | Feeder Cattle | 50,000 lb | 346.40¢ | $173,200 |

## 10. The two facts that matter most

**The 27× notional span.** Gold at $442,550 against oats at $16,125. Book size
therefore decides *which markets can hold even one contract*, which is why
capital adequacy is a first-order design constraint rather than an
afterthought.

**Cents quoting.** Grains, softs and cattle quote in cents, carrying a
`priceMagnifier` of 100. Ignoring it overstates contract value 100× — in
testing, coffee once appeared at $12.8M per contract instead of $128k. The
multiplier and magnifier are **read from IBKR at runtime, never hardcoded**,
because a wrong value does not raise an error; it silently mis-sizes every
position in that market.

## 11. One market that was removed

`MWE` (Minneapolis wheat) was in the original 23 and removed on 2026-08-12: it
does not resolve on IBKR. MGEX was absorbed by MIAX and CBOT has no security
definition for it. The backtest was re-run on the remaining 22 so the forward
comparison is like-for-like.

---

# Part IV · Technical infrastructure

## 12. Two strategies live in this repository

The naming convention tells them apart, and this matters because `agent_two`
exists in both meaning different things:

| Convention | Strategy | Status |
|---|---|---|
| `agent_zero`, `agent_one`, `agent_dsi` · `xsec_`-less filenames | Natural gas calendar spreads | **Retired** |
| `agent_0` … `agent_3` · `xsec_` prefix | Cross-sectional commodities | **Live** |

The retired code is kept deliberately: it is the evidence behind its own
retirement, and deleting it would delete the proof.

## 13. Live modules — `src/storage_stress/`

| Module | Responsibility |
|---|---|
| `data/commodities.py` | Builds the panel from settlements; computes carry, momentum, basis-momentum, and the rejected stress factor. Roll-safe returns live here. |
| `data/connectivity.py` | Every external feed, plus the R2 historical mirror. Research only — the live path does not use it. |
| `agents/xsec.py` | The ladder (`AGENT_FACTORS`), cross-sectional z-scoring, tercile weights, portfolio simulator. |
| `execution/xsec_engine.py` | The live engine: rebalance and mark. Sizing, the capital-adequacy rail, order placement, per-agent gateway routing. |
| `execution/xsec_signals_ib.py` | Computes the three factors **live from IBKR** rather than the mirror. |
| `execution/xsec_contracts.py` | Resolves tickers to real IBKR contracts. Reads multiplier and magnifier, enforces the expiry guard and per-order lot cap. |
| `execution/xsec_books.py` | The SQLite ledger. Source of truth for per-agent attribution. |
| `monitoring/flex.py` | IBKR Flex client — the broker's independent record, used to reconcile the ledger. |
| `monitoring/publish.py` | Builds a sanitised public status snapshot. Currently unwired. |
| `signal/dsi.py` | The retired DSI pipeline. Still used by `add_stress`. |

## 14. Live scripts — `scripts/`

| Script | What it does |
|---|---|
| `run_xsec_live.py` | **The live CLI.** `--job rebalance \| mark \| status`, `--dry-run`, `--agents`. |
| `xsec_scheduler.py` | **The always-on loop.** Polls every 60s; fires the monthly rebalance and the daily mark as subprocesses. Self-healing launch. |
| `flatten_account.py` | Closes every open position. Paper-only guard; `--agents` to flatten a subset. |
| `flex_pull.py` | Pulls the broker's statement and reconciles it against the ledger. |
| `verify_xsec_universe.py` | Checks every market resolves on IBKR and prices sensibly. Run before trusting a universe change. |

## 15. Research scripts

| Script | What it does |
|---|---|
| `run_xsec.py` | **The main backtest.** Runs the ladder and an N-seed null distribution. |
| `xsec_robustness.py` | Cost sensitivity, sub-period stability, whether the null is handicapped by turnover. |
| `stress_test.py` | The pre-committed test that rejected the stress factor. |
| `carry_drop_test.py` | The pre-committed test that kept carry despite its negative standalone Sharpe. |
| `xsec_stage2.py` | The two questions that decided whether to deploy at all. |
| `md_to_pdf.py` | Renders project markdown to PDF via Chromium. |

## 16. Retired scripts

`run_agents.py`, `run_backtest.py`, `run_daily.py`, `scheduler.py`,
`regime_test.py`, `null_distribution.py`, `compare_instruments.py`,
`explore_r2_basis.py` — all belong to the natural-gas strategy.

`regime_test.py` is the one worth reading: it is the falsification test that
retired that strategy.

## 17. Deployment — `deploy/`

| Script | What it does |
|---|---|
| `bootstrap.sh` | Bare Ubuntu VM → running stack, one command. Refuses ARM, adds swap, sets the clock to ET, firewalls, prompts for credentials, waits for a real gateway login, installs crons. |
| `status.sh` | One screen: container health, gateway login state, per-agent ledger, rounded-to-zero count, disk. |
| `backup.sh` | Nightly ledger snapshot via SQLite's online backup API, verified and retained 30 days. |

---

# Part V · How it runs

## 18. The deployed stack

Six containers on one x86-64 VM:

```
┌─ VM (Ubuntu 24.04, 4 GB, NYC) ──────────────────┐
│  ib-gateway-0 ──── socat :4004 ──── IBKR        │
│  engine        rebalance 10:30 ET, mark 16:30   │
│  dashboard     Streamlit, localhost only        │
│  ledger        xsec_books.db (SQLite)           │
└──────────────────────────────────────────────────┘
       ib-gateway-1/2/3 behind a "full" profile
```

**The live path depends on exactly one external service: IBKR.** Signals and
execution come from the same place, so a position is traded and marked against
the same prices that generated it. The R2 mirror feeds backtests only — it was
measured stale by 43–243 days per market.

IB Gateway binds its API port to loopback and honours the handshake only from
`127.0.0.1`, so the image runs **socat on 4004** forwarding to `127.0.0.1:4002`.
That is the only port another container can use.

## 19. The schedule

| When | Job |
|---|---|
| Last business day, 10:30 ET | **Rebalance** — rank, diff against holdings, trade the difference |
| Every weekday, 16:30 ET | **Mark** — price positions, record equity |
| Daily 02:30 ET | Ledger backup |

10:30 ET is the only window when all 22 markets are liquid simultaneously:
grains open 09:30, softs run roughly 04:00–14:00, energy and metals nearly 24h.

## 20. Capital adequacy

Book size determines which markets can hold a contract:

| Book per agent | Markets tradeable | Avg weight error |
|---|---|---|
| $250k | 11 of 22 | 31% |
| $2.5M | 22 of 22 | 8.8% |
| $10M | 22 of 22 | 1.9% |

If more than **30%** of intended positions round to zero contracts, the engine
aborts the whole rebalance **before sending any order**.

Each agent needs its **own** IBKR paper account, because IBKR caps a paper
account at $1,000,000 and four agents sharing one would get $520,833 each — at
which gold, heating oil, feeder cattle and copper drop out. Those are four of
the five markets whose removal was already tested and rejected.

## 21. Invariants

Things that look like style and are not:

- **Returns are roll-safe** — only between two prices of the same contract.
- **The ledger records only what actually filled.** Never the target.
- **Plan, then judge, then execute.** The feasibility check completes before
  any order is sent.
- **Every decision is recorded**, traded or not, with its reason.
- **`config/xsec.yaml` is frozen** while the forward test runs.
- **Multiplier and magnifier are read from IBKR**, never assumed.

---

*Paper trading throughout. No capital is at risk.*
