# Technical Infrastructure & Member Onboarding

**QUANTT Commodities — systematic futures trading**

*Living document. Each section is self-contained so it can be revised without
touching the others. Update the [Change Log](#12-change-log) with every edit.*

*Version 1.0 — 2026-08-12*

---

## Table of contents

**Infrastructure**
1. [System overview](#1-system-overview)
2. [Data sources & APIs](#2-data-sources--apis)
3. [Repository structure](#3-repository-structure)
4. [The strategy — technical specification](#4-the-strategy--technical-specification)
5. [Execution engine](#5-execution-engine)
6. [Scripts reference](#6-scripts-reference)
7. [Testing & correctness rules](#7-testing--correctness-rules)

**Onboarding**
8. [Accounts to create](#8-accounts-to-create)
9. [Local environment setup](#9-local-environment-setup)
10. [IB Gateway configuration](#10-ib-gateway-configuration)
11. [First week checklist](#11-first-week-checklist)
12. [Change log](#12-change-log)
13. [References](#13-references)

---
---

# INFRASTRUCTURE

## 1. System overview

```
   DATA LAYER                RESEARCH LAYER            EXECUTION LAYER
   ──────────                ──────────────            ───────────────

 WRDS / Datastream ──┐
 (R2 parquet)        │
                     ├──► commodities.py ──► xsec.py ──┐
 IBKR historical  ───┤    (panel, factors)  (ladder,   │
 bars                │                       portfolio)│
                     │                                 ▼
 EIA API v2       ───┘                        xsec_engine.py
 (legacy)                                     rebalance / mark
                                                     │
                              ┌──────────────────────┼──────────────────────┐
                              ▼                      ▼                      ▼
                       IBKR paper acct        xsec_books.db           Streamlit
                       (order execution)      (ledger = TRUTH)        dashboards
```

### Core architectural principle

**The local ledger — not the broker account — is the source of truth for
per-agent performance.**

Four agents share one IBKR account and will frequently hold *opposite* positions
in the same market. Those net to zero at the account level while both books
carry real risk. Attribution is therefore achieved by:

1. **`orderRef` tagging** — every order carries `xsec|<agent>|<ticker>|<date>`,
   so every fill in the account is attributable to exactly one agent
2. **The SQLite ledger** — each agent has its own virtual capital, positions,
   trades and daily equity curve

---

## 2. Data sources & APIs

| Source | Access method | Used for | Credentials |
|---|---|---|---|
| **IBKR** | `ib_async` → IB Gateway (socket :4002) | Live prices, contract specs, **live factor computation**, order execution | Paper login |
| **WRDS / Datastream** (`tr_ds_fut`) | Cloudflare R2 parquet + DuckDB `httpfs` | Backtesting: 8,538 contracts, 1990–2026 | R2 access keys |
| **EIA API v2** | REST, JSON | Legacy natural-gas strategy | Free API key |

### ⚠️ Critical: the R2 mirror is not a live feed

Measured 2026-08-12, **every market in the mirror was stale**:

| Market group | Staleness |
|---|---|
| Softs (CC, KC, SB, CT, OJ) | 43 days |
| Energy / metals | 134–145 days |
| Grains (ZC, ZO) | **243 days** |

**Zero markets had data within 30 days.** The mirror is a periodic snapshot —
excellent for backtesting, unusable for live signals.

**Consequence:** live factors are computed from **IBKR** (`xsec_signals_ib.py`).
This is also better architecture — signals and execution share one price source,
so a position is traded and marked against the prices that generated it.

### Environment variables (`.env`, gitignored)

```
EIA_API_KEY=              # free: eia.gov/opendata
R2_ACCESS_KEY_ID=         # Cloudflare R2
R2_SECRET_ACCESS_KEY=
R2_ENDPOINT=              # https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET=quantt-historical-market-data
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
```

---

## 3. Repository structure

```
storage-stress/
├── config/
│   ├── xsec.yaml              ★ FROZEN strategy spec
│   ├── settings.yaml            legacy NG parameters
│   └── live.yaml                legacy NG live config
├── src/storage_stress/
│   ├── data/
│   │   ├── commodities.py     ★ universe, panel, three factors
│   │   ├── futures.py           NG seasonal spread builder (legacy)
│   │   └── connectivity.py      EIA + R2/DuckDB connections
│   ├── agents/
│   │   ├── xsec.py            ★ ladder, weights, simulator, vol targeting
│   │   └── agents.py            legacy NG agents
│   ├── execution/
│   │   ├── xsec_contracts.py  ★ IBKR resolution, multipliers, guards
│   │   ├── xsec_signals_ib.py ★ LIVE factors from IBKR
│   │   ├── xsec_books.py      ★ multi-position ledger
│   │   ├── xsec_engine.py     ★ rebalance + mark jobs
│   │   └── (books/broker/engine — legacy NG)
│   └── monitoring/marks.py      exit-rule arithmetic (legacy)
├── scripts/                     thin CLI wrappers — logic lives in src/
├── dashboard/
│   ├── app.py                   legacy NG live books (:8501)
│   └── pivot_a.py             ★ research + live results (:8502)
├── notebooks/
│   └── 03_strategy_review.ipynb ★ full technical narrative, runnable
├── tests/                       pytest — 17 tests
├── data/
│   ├── processed/               backtest artifacts (CSV)
│   └── live/                    ledgers (SQLite) + job logs
└── docs/                        this document + specs + results
```

**GitHub:** `<REPO_URL — fill in>` · Access: ask a project lead.

---

## 4. The strategy — technical specification

### 4.1 What it is

A **dollar-neutral, cross-sectional long/short commodity futures portfolio**.
Each month it ranks 22 markets by a composite factor score, buys the top third
and sells the bottom third, and holds to the next rebalance.

### 4.2 Universe (22 markets, all USD)

| Sector | Tickers |
|---|---|
| Energy | CL, NG, HO, RB |
| Metals | GC, SI, HG, PL, PA |
| Grains / oilseeds | ZC, ZS, ZM, ZL, ZO, KE |
| Softs | CC, KC, SB, CT, OJ |
| Livestock | LE, GF |

**Excluded deliberately:** mini contracts (duplicate exposure), swaps and crack
spreads (derivative of the same markets), non-USD listings (currency
contamination), MWE/Minneapolis wheat (does not resolve on IBKR — MGEX was
absorbed by MIAX).

> **Universe size is a strategy parameter, not an operational convenience.** A
> 17-market variant was tested (dropping the five most margin-hungry markets) and
> **rejected on measurement**: carry stopped beating chance (Sharpe 0.24 → 0.12)
> and drawdown worsened from −36% to −53%. The account is sized to the universe,
> never the reverse.

### 4.3 The three factors

All computed per market per day, then **cross-sectionally z-scored within each
date** before blending. Standardisation is essential — carry is an annualised log
slope, momentum is a 12-month return; without it, units alone decide the book.

**Carry** — the forward curve's slope:
```
carry = ln(front_price / second_price) / Δt_years
```
Positive = backwardation = scarcity. Holders of the physical earn a convenience
yield; longs are compensated for supplying the deferred month. Annualised because
markets list on different cycles.

**Momentum** — 12-month (≈252 trading day) compounded return of the front
contract. The most robust anomaly across essentially every asset class.

**Basis-momentum** (Boons & Prado 2019):
```
basis_momentum = momentum(front) − momentum(second)
```
Measures how the *curve itself* has moved — distinct from the curve's current
shape (carry) and the market's overall trend (momentum).

### 4.4 The agent ladder

Each tier sees **exactly one more** factor, so the gap between adjacent tiers
isolates that factor's contribution.

| Agent | Sees | Purpose |
|---|---|---|
| `agent_0` | *nothing* — seeded random score | **Null benchmark** |
| `agent_1` | carry | first real signal |
| `agent_2` | carry + momentum | adds trend |
| `agent_3` | carry + momentum + basis-momentum | full signal |

Score = **equal-weight mean of the standardised factors that tier can see**. No
factor weights are fitted — a deliberate choice to avoid in-sample optimisation.

> **`agent_0` must be run across many seeds** to form a *distribution*. One
> random path is not a benchmark. A single seed once produced +26% and looked
> like signal; it was the 95th percentile of chance.

### 4.5 Portfolio construction

| Element | Setting | Rationale |
|---|---|---|
| Positions | long top tercile, short bottom tercile | standard factor construction |
| Weighting | equal within side; each side 1.0 gross | no fitted weights |
| Neutrality | dollar-neutral (net 0) | strips common commodity beta |
| Breadth floor | ≥ 6 markets | thinner is noise |
| Rebalance | monthly, held between | realistic turnover |
| Look-ahead guard | weights `.shift(1)` before earning | a position cannot earn the return that selected it |
| Vol targeting | 10% annualised, 63-day window, 3× cap | avg leverage 0.53 — mostly de-risks |
| Costs | 10 bps per unit turnover | stress-tested at 3× and 5× |

### 4.6 Backtest results (2000–2026, 6,676 trading days)

| Agent | Ann. return | Sharpe | Max DD | vs chance |
|---|---|---|---|---|
| `agent_0` random | −0.26% | 0.08 | −66% | — |
| `agent_1` carry | +2.91% | 0.24 | −75% | 98th pctile ✓ |
| `agent_2` +momentum | +5.56% | 0.36 | −64% | 100th ✓ |
| `agent_3` **full** | **+11.54%** | **0.61** | −36% | 100th ✓ |

With 10% vol targeting: **Sharpe 0.70, max DD −19%**.
Post-2019 at 3× costs, vol-targeted: **Sharpe 0.78, +8.2%/yr**.

---

## 5. Execution engine

### 5.1 Jobs

| Job | Schedule (ET) | Action |
|---|---|---|
| `rebalance` | monthly, last business day, **10:30** | score → rank → target contracts → diff vs holdings → trade |
| `mark` | weekdays **16:30** | price open positions, compute unrealised P&L, record equity |

**Why 10:30 ET:** the only window when all 22 markets are liquid together —
grains open 09:30, softs run ~04:00–14:00, energy/metals nearly 24h. An early
attempt at 17:50 ET (inside the CME settlement break) had every order sit
`PreSubmitted` instead of filling.

### 5.2 Live constraints discovered in testing

| Constraint | Value | Why |
|---|---|---|
| Delivery guard | **25 days** | IBKR's physical-delivery policy rejects orders ~15 days out; 5 was insufficient |
| Max order size | **60 lots** | IBKR refuses non-algo orders above ~64; larger targets split into child orders |
| Price magnifier | read from IBKR | Grains/softs/cattle quote in **cents** — ignoring this overstates contract value **100×** |
| Multipliers | read from IBKR | Never hardcoded; a wrong multiplier mis-sizes every position in that market |
| Book sizing | account-aware | Sized against real account equity, not the configured aspiration |

### 5.3 Safety rails

- **Ledger records only actual fills.** Rejected orders never create positions.
- **Abort on degradation.** If >30% of intended positions round to zero
  contracts, the agent aborts rather than trading a silently different strategy.
- **Flatten before first run** so the forward record starts from a known state.

### 5.4 Contract sizing

Contract notionals span **27×** in this universe:

| Market | $ per contract |
|---|---|
| Gold (GC) | ~$443,000 |
| Crude (CL) | ~$83,000 |
| Corn (ZC) | ~$22,000 |
| Oats (ZO) | ~$16,000 |

Equal *dollar* weight is therefore nowhere near equal contracts. Book size
determines coverage:

| Book/agent | Markets tradeable | Avg weight error |
|---|---|---|
| $250k | 11/22 | 31.1% |
| $2.5M | 22/22 | 8.8% |
| **$10M** | **22/22** | **1.9%** |

---

## 6. Scripts reference

| Script | Purpose |
|---|---|
| `verify_xsec_universe.py` | **Run first.** Contract resolution, market data, sizing feasibility |
| `run_xsec.py` | Backtest the ladder + null distribution |
| `xsec_robustness.py` | Cost sensitivity, era stability, zero-cost null |
| `xsec_stage2.py` | Post-publication decay, vol targeting |
| `carry_drop_test.py` | Pre-committed single-shot factor test |
| `run_xsec_live.py` | `--job rebalance \| mark \| status`, `--dry-run` |
| `xsec_scheduler.py` | Always-on scheduler |
| `deploy/bootstrap.sh` | Bare VM -> running stack, one command |
| `flatten_account.py` | Close all positions (paper-only, `--confirm`) |
| `regime_test.py` | Legacy: the NG week-1 gate |
| `compare_instruments.py` | Legacy: 4 agents × 3 NG instruments |

**Always dry-run first:** `python scripts/run_xsec_live.py --job rebalance --dry-run`

---

## 7. Testing & correctness rules

`pytest` — **17 tests**, including regression tests for every bug found.

### The five non-negotiable rules

Each exists because violating it produced a false result in this project.

1. **Roll-safe returns.** A return is only ever computed between two prices of
   the *same* contract. Splicing and differencing fabricates P&L — measured at
   6–14× a normal daily move, 27–31% of all apparent movement.
2. **Costs are charged.** A bug wrote slippage into equity then overwrote the
   cell, making the backtest cost-free — a 253-trade coin flip looked profitable.
3. **The ledger records only actual fills.** An early live run wrote 54 positions
   when IBKR held 3.
4. **Null benchmarks are distributions, not single paths.**
5. **Pre-commit factor tests.** Fix the rule before running; run once.

---
---

# ONBOARDING

## 8. Accounts to create

| Account | Cost | Purpose | Notes |
|---|---|---|---|
| **IBKR paper trading** | free | Live execution | Request a **paper-only** login — avoids 2FA blocking headless Gateway |
| **EIA API key** | free | Legacy NG data | [eia.gov/opendata](https://www.eia.gov/opendata/) — instant |
| **WRDS** | institutional | Research data | Via Queen's |
| **GitHub** | free | Repository | Ask a lead for access |

## 9. Local environment setup

```bash
git clone <REPO_URL> && cd storage-stress

python -m venv .venv
.venv\Scripts\activate            # Windows
source .venv/bin/activate         # macOS / Linux

pip install -e ".[dev,live,dashboard]"

cp .env.example .env               # then fill in keys
pytest                             # 17 tests should pass
```

**Never commit `.env`.** It is the first line of `.gitignore`.

## 10. IB Gateway configuration

1. Download **IB Gateway** (not TWS — lighter, built for this)
2. Log in with **paper** credentials
3. **Configure → Settings → API → Settings**:
   - ✅ Enable ActiveX and Socket Clients
   - ❌ uncheck Read-Only API
   - Socket port **4002**
   - ✅ Allow connections from localhost
4. **Configure → Lock and Exit → Auto restart** — avoids nightly re-login

Verify: `python scripts/verify_xsec_universe.py`

## 11. First week checklist

- [ ] Environment set up; `pytest` passes
- [ ] Read `notebooks/03_strategy_review.ipynb` end to end — includes the mistakes
- [ ] Read `docs/03_SUMMER_RESULTS.md`
- [ ] Run the backtest: `python scripts/run_xsec.py`
- [ ] Run a dry-run rebalance and read the recorded decisions
- [ ] Open the dashboard (`:8502`)
- [ ] Pick a question from the backlog and write up what you find

### Working norms

- **Logic in `src/`, thin wrappers in `scripts/`.** Notebooks explore; they never
  hold logic other code imports.
- **Config in YAML, secrets in `.env`.** No parameters hardcoded in scripts.
- **Every claim gets a number.** "It seems better" is not a result.
- **Report negatives.** Two of this project's most valuable findings were
  negative results.
- **Changing `config/xsec.yaml` invalidates the forward test** from that date.
  Record any change here with the date and reason.

---

## 12. Change log

| Date | Change | By |
|---|---|---|
| 2026-08-12 | v1.0 created. Universe fixed at 22 markets; live signals moved from R2 mirror to IBKR; delivery guard 5→25 days; order splitting added. | — |

---

## 13. References

### Academic

- **Gorton, G. & Rouwenhorst, K.G. (2006).** "Facts and Fantasies about
  Commodity Futures." *Financial Analysts Journal* 62(2), 47–68.
- **Erb, C. & Harvey, C. (2006).** "The Strategic and Tactical Value of
  Commodity Futures." *Financial Analysts Journal* 62(2), 69–97.
- **Boons, M. & Prado, M.P. (2019).** "Basis-Momentum." *Journal of Finance*
  74(1), 239–279. — the third factor in our ladder.
- **Szymanowska, M., de Roon, F., Nijman, T. & van den Goorbergh, R. (2014).**
  "An Anatomy of Commodity Futures Risk Premia." *Journal of Finance* 69(1),
  453–482.
- **Moskowitz, T., Ooi, Y.H. & Pedersen, L.H. (2012).** "Time Series Momentum."
  *Journal of Financial Economics* 104(2), 228–250.
- **Hong, Y. & Klabjan, D. (2026).** "Hierarchical Graph Learning for Calendar
  Spread Strategies in Commodity Futures Markets." arXiv:2606.25811. — *note:
  does not model transaction costs.*

### Technical

- [ib_async documentation](https://ib-api-reloaded.github.io/ib_async/) —
  successor to the unmaintained `ib_insync`
- [IBKR TWS API guide](https://interactivebrokers.github.io/tws-api/)
- [EIA API v2](https://www.eia.gov/opendata/documentation.php)
- [DuckDB httpfs / S3](https://duckdb.org/docs/extensions/httpfs.html)
- [WRDS](https://wrds-www.wharton.upenn.edu/)
- [CME contract specifications](https://www.cmegroup.com/markets/products.html)

### Internal

| Document | Contents |
|---|---|
| `docs/02_PROJECT_INTRO_HIRING.md` | Recruiting overview |
| `docs/03_SUMMER_RESULTS.md` | Summer progress and results |
| `docs/PIVOT_A_STRATEGY.md` | Full strategy specification |
| `docs/FRAMEWORK.md` | Legacy NG execution architecture |
| `notebooks/03_strategy_review.ipynb` | Complete technical review |
| `config/xsec.yaml` | The frozen specification |
