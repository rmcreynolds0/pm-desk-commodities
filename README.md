# QUANTT Commodities — cross-sectional futures research

A systematic commodity futures strategy running live on IBKR paper, built
around an unusual constraint: **every claim has to beat a random agent before
it counts.**

The book ranks 22 commodity futures against each other on carry, momentum and
basis-momentum, holds the top third long against the bottom third short,
dollar-neutral, rebalanced monthly. Over 2000–2026 that earns Sharpe 0.553 at
the 99.5th percentile of a 200-seed null distribution.

## The part worth knowing first

**This project retired its own first strategy.** Half of summer 2026 went into
a natural-gas calendar-spread model driven by a Deliverability Stress Index.
Its own pre-registered regime test falsified the hypothesis behind it — the
volatility it needed to rise had fallen 29%, and mean reversion had gone to
zero. Three separate bugs had been making the backtest look profitable.

That code is still here. It is the evidence behind the decision, and deleting
it would delete the proof. See `docs/SUMMER_SUMMARY.md`.

The naming convention tells you which is which:

| Convention | Strategy | Status |
|---|---|---|
| `agent_zero`, `agent_one`, `agent_dsi` | Natural gas calendar spreads | **Retired** |
| `agent_0` … `agent_3` | Cross-sectional commodities | **Live** |

## The agent ladder

Four agents run on identical machinery, differing only in which factors they
may read. `agent_0` trades at random. Each rung above sees exactly one more
piece of information, so the gap between two adjacent rungs measures what that
one factor contributes.

| Agent | Sees | Sharpe | vs chance |
|---|---|---|---|
| `agent_0` | random — nothing | 0.08 | — |
| `agent_1` | carry | 0.24 | 98th pctile |
| `agent_2` | + momentum | 0.40 | 100th |
| **`agent_3`** | **+ basis-momentum** | **0.553** | **99.5th** |

Only `agent_3` trades live today — each agent needs its own IBKR paper
account, and IBKR caps those at $1,000,000. See `config/xsec.yaml`.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,dashboard,live]"

pytest -q                          # 75 tests
python scripts/run_xsec.py 200     # reproduces the ladder + a 200-seed null
streamlit run dashboard/pivot_a.py # research dashboard
```

Your ladder numbers must match `docs/03_SUMMER_RESULTS.md`. If they don't,
say so — a reproducibility gap is a finding, not a nuisance.

Backtests read a historical mirror from Cloudflare R2 and need credentials in
`.env`. **Live trading needs none of that** — signals come from IBKR itself,
because the mirror was measured stale by 43–243 days per market.

## Layout

```
storage-stress/
├── config/xsec.yaml            FROZEN live spec — changing it invalidates
│                               the forward test from that date
├── src/storage_stress/
│   ├── data/                   commodities.py (factors), connectivity.py (feeds)
│   ├── signal/dsi.py           the retired gas pipeline — still used by add_stress
│   ├── agents/
│   │   ├── xsec.py             LIVE: the ladder, scoring, tercile weights
│   │   └── agents.py           RETIRED: the gas agents
│   ├── execution/              xsec_* = live; the rest is the gas engine
│   └── monitoring/             publish.py (public snapshot), flex.py (dormant)
├── scripts/                    run_xsec* = live; run_agents/run_daily = retired
├── deploy/bootstrap.sh         bare VM -> running stack, one command
├── dashboard/
│   ├── xsec_live.py            LIVE ledger
│   └── pivot_a.py              RESEARCH backtest artifacts
└── docs/                       see below
```

## Documentation

| Document | What it covers |
|---|---|
| `01_TECHNICAL_INFRASTRUCTURE.md` | How the code fits together |
| `02_PROJECT_INTRO_HIRING.md` | Roles, requirements, timeline |
| `03_SUMMER_RESULTS.md` | What was found, with evidence |
| `04_ONBOARDING_PACKAGE.md` | First-fortnight path, accounts, budget |
| `DEPLOY.md` | Always-on deployment |
| `SUMMER_SUMMARY.md` | The gas strategy and why it was retired |
| `PIVOT_A_STRATEGY.md` | How the current strategy works |
| `STRESS_FACTOR.md` | A factor that was tested and **rejected** |

## Conventions worth keeping

- **Nothing is a result until it beats a null distribution.** One random path
  is an anecdote; 200 are a benchmark. A +26.3% result that looked like signal
  turned out to be the 95th percentile of chance.
- **Pre-commit the decision rule**, in the file, before running the test.
  `scripts/stress_test.py` and `scripts/carry_drop_test.py` both do this, and
  both rejected the thing they were testing.
- **`config/*.yaml` is frozen while the forward test runs.** Changing a
  parameter mid-test turns out-of-sample into in-sample with extra steps.
- **Secrets live in `.env` only.** Never hardcode a key as a default value —
  this repo once carried literal EIA and NOAA keys as fallbacks.
- **Returns are roll-safe**: computed only between two prices of the *same*
  contract. Splicing fabricated up to 31% of all measured movement.
- Logic lives in `src/` and is tested; `scripts/` are thin wrappers;
  notebooks only look at things.

---

*Paper trading throughout. Backtest figures are simulated; forward figures
come from the live IBKR paper ledger. No capital is at risk.*
