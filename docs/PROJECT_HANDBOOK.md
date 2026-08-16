# QUANTT Commodities — Project Handbook

**Contents**
1. [Project intro & hiring](#part-1--project-intro--hiring) *(for candidates — ~2 pages)*
2. [Technical infrastructure](#part-2--technical-infrastructure) *(architecture, data, code)*
3. [Onboarding for new members](#part-3--onboarding) *(setup, accounts, first tasks)*
4. [References & citations](#part-4--references)

*Living document — updated throughout the year. Each section is independent so
it can be revised without touching the others.*
*Last updated: 2026-08-12*

---
---

# PART 1 — Project intro & hiring

## What we do

We build and run **systematic commodity futures trading strategies**, validated
end-to-end: raw data → signal research → backtest → live paper execution →
performance attribution.

The current strategy is a **dollar-neutral, cross-sectional long/short commodity
portfolio**. Each month it ranks ~17 commodity futures markets by a composite
factor score, buys the top third and sells the bottom third. It harvests three
risk premia documented across decades of academic literature:

| Factor | What it measures | Intuition |
|--------|------------------|-----------|
| **Carry** | Slope of each market's forward curve | Backwardation signals scarcity; holders of the physical earn a convenience yield |
| **Momentum** | 12-month trailing return | Slow diffusion of information |
| **Basis-momentum** | How the *curve itself* has moved | Imbalances in intermediation of the futures curve |

**Backtest (2000–2026):** Sharpe 0.61 unscaled, 0.70 with volatility targeting,
**0.78 in the post-2019 period at 3× assumed costs**. Every signal tier beats a
200-run random benchmark.

## What makes this project unusual

Most student trading projects backtest a strategy and stop. We do three things
that are closer to professional practice:

1. **A null benchmark that must earn zero.** Every strategy runs alongside a
   *random* agent. If the strategy can't beat a distribution of coin flips, it
   has no edge — regardless of how good its own numbers look. This has already
   killed one strategy of ours (see Part 2 §7).
2. **An information ladder.** Four agents each see one more piece of the signal
   than the last, so we can measure what each component actually contributes,
   rather than asserting it.
3. **Live paper execution.** Strategies trade a real IBKR paper account through
   our own execution engine. Backtests are hypotheses; forward performance is
   evidence.

## Roles we're hiring

### Quantitative Research Analyst (2–3 positions)
Signal research: build and test factor hypotheses, run robustness and
significance testing, contribute to the strategy pipeline.

*Requirements:* Python (pandas, numpy); statistics (regression, hypothesis
testing, confidence intervals); comfort reading academic finance papers. Prior
finance coursework helpful, not required.

### Data Engineer (1–2 positions)
Own the pipeline: database queries (SQL/DuckDB), API integrations, data quality
checks, and the parquet/warehouse layer.

*Requirements:* Python, SQL, familiarity with APIs. Interest in data
correctness — a surprising share of our findings have been data bugs.

### Execution & Infrastructure (1–2 positions)
The live trading engine: IBKR API integration, order management, position
reconciliation, monitoring and dashboards.

*Requirements:* Python; interest in systems that must be *correct*, not just
functional. Docker/deployment experience welcome.

## What you'll learn

- How a strategy is actually validated — pre-registration, null benchmarks,
  out-of-sample discipline, and honest kill criteria
- Real market microstructure: contract specifications, margin, roll mechanics,
  delivery risk, transaction costs
- A production-style stack: Python, SQL/DuckDB, IBKR API, Docker, Streamlit

## Timeline

| Period | Focus |
|--------|-------|
| **Fall term** | Onboarding; forward paper trading accumulates out-of-sample evidence; individual research projects begin |
| **Winter term** | Research projects → candidate signals; ablation and robustness testing |
| **Spring** | Consolidation; promote signals that survive; write-up |

**Commitment:** 5–10 hrs/week. No prior trading experience required — the
methodology is teachable; curiosity and rigour are not.

---
---

# PART 2 — Technical infrastructure

## 2.1 Architecture

```
   DATA                    RESEARCH                  EXECUTION
   ────                    ────────                  ─────────
 WRDS/Datastream  ──┐
 (R2 parquet)       ├──►  commodities.py  ──►  xsec.py  ──┐
 EIA API          ──┤     (panel+factors)     (ladder,    │
 IBKR historical  ──┘                          portfolio) │
                                                          ▼
                                              xsec_engine.py
                                              (decide / mark)
                                                    │
                                    ┌───────────────┼───────────────┐
                                    ▼               ▼               ▼
                              IBKR paper     xsec_books.db     dashboards
                              (execution)    (ledger, truth)   (Streamlit)
```

**Key principle:** the ledger — not the broker account — is the source of truth
for per-agent performance. Four agents share one IBKR account and frequently
hold *opposite* positions in the same market, which net to zero at the account
level while both books carry real risk. Attribution is by `orderRef` tag plus
the local ledger.

## 2.2 Data sources

| Source | Access | Used for | Status |
|--------|--------|----------|--------|
| **WRDS/Datastream** (`tr_ds_fut`) | Cloudflare R2 parquet, DuckDB `httpfs` | Backtest: 8,538 contracts, 1990–2026 | ✅ research only |
| **IBKR** | `ib_async` → IB Gateway | Live prices, contract specs, **live factors**, execution | ✅ live |
| **EIA API v2** | REST, free key | Legacy (natural-gas strategy) | ⚠️ retired strategy |

> **⚠️ Critical lesson.** The R2 mirror is a periodic **snapshot, not a feed**.
> Measured 2026-08-12: every market stale, 43 days (softs) to **243 days**
> (corn, oats); zero markets inside 30 days. It is excellent for backtesting and
> **unusable for live signals**. Live factors are therefore computed from IBKR,
> which also means positions are traded and marked against the same prices that
> generated them.

## 2.3 Repository layout

```
storage-stress/
├── config/
│   ├── xsec.yaml              # FROZEN strategy spec — changing it invalidates
│   │                          #   the forward test from that date
│   ├── settings.yaml          # legacy NG parameters
│   └── live.yaml              # legacy NG live config
├── src/storage_stress/
│   ├── data/
│   │   ├── commodities.py     # universe, panel, carry/momentum/basis-momentum
│   │   ├── futures.py         # NG seasonal spread builder (legacy)
│   │   └── connectivity.py    # EIA + R2/DuckDB connections
│   ├── agents/
│   │   ├── xsec.py            # ladder, tercile weights, simulator, vol targeting
│   │   └── agents.py          # legacy NG agents
│   └── execution/
│       ├── xsec_contracts.py  # IBKR contract resolution, multipliers, guards
│       ├── xsec_signals_ib.py # LIVE factor computation from IBKR
│       ├── xsec_books.py      # multi-position ledger (SQLite)
│       └── xsec_engine.py     # rebalance + mark jobs
├── scripts/                   # thin CLI wrappers — logic lives in src/
├── dashboard/                 # Streamlit (8501 legacy NG, 8502 Pivot A)
├── notebooks/03_strategy_review.ipynb   # full technical record
└── docs/                      # this handbook + strategy specs
```

## 2.4 Key scripts

| Script | Purpose |
|--------|---------|
| `run_xsec.py` | Backtest the ladder + null distribution |
| `xsec_robustness.py` | Cost sensitivity, era stability, zero-cost null |
| `xsec_stage2.py` | Post-publication decay windows, vol targeting |
| `carry_drop_test.py` | Pre-committed single-shot factor test |
| `verify_xsec_universe.py` | **Run first** — contract resolution, market data, sizing |
| `run_xsec_live.py` | Live: `--job rebalance \| mark \| status`, `--dry-run` |
| `flatten_account.py` | Close all positions (paper-only, requires `--confirm`) |

## 2.5 Methodological rules (non-negotiable)

These exist because violating each one produced a false result in this project.

1. **Roll-safe returns.** A return is only ever computed between two prices of
   the *same* contract. Splicing contracts and differencing fabricates P&L —
   measured at 6–14× a normal daily move, and 27–31% of all apparent movement.
2. **Costs are charged.** A bug once wrote slippage into equity and then
   overwrote the cell, making the backtest cost-free — which let a 253-trade
   coin flip look profitable. Regression-tested.
3. **The ledger records only actual fills.** An early live run wrote 54
   positions when IBKR held 3, because rejected orders were treated as filled.
4. **Null benchmarks are distributions, not single paths.** One random seed once
   produced +26% and looked like signal; it was the 95th percentile of chance.
5. **Pre-commit factor tests.** Fix the decision rule before running, and run
   once. Iterating until a number improves is how overfitting happens.

## 2.6 Live execution specifics

| Constraint | Value | Why |
|---|---|---|
| Delivery guard | **25 days** | IBKR's physical-delivery policy rejects orders closer than ~15 days; 5 was not enough |
| Max order size | **60 lots** | IBKR refuses non-algo orders above ~64; larger targets are split |
| Price magnifier | read from IBKR | Grains/softs/cattle quote in **cents**; ignoring this overstates contract value 100× |
| Multipliers | read from IBKR | Never hardcoded — a wrong multiplier mis-sizes every position in that market |
| Book sizing | account-aware | Sized against real account equity, not the configured aspiration |

## 2.7 Strategy history (why we pivoted)

**Original: natural-gas storage stress.** Thesis — post-2016 LNG exports
tightened US deliverability; salt-cavern utilisation measures it; calendar
spreads hadn't repriced it. **Retired**, on three findings:

1. **Its own week-1 regime gate failed.** Post-2016 spreads became **29% less
   volatile** (p=0.033) with *unchanged* seasonality — the opposite of a
   tightening system. The market got more efficient, not more stressed.
2. **No agent beat chance** on any of three instruments once roll artifacts and
   costs were corrected.
3. Two measurement bugs had made earlier results — including an apparent
   "monotonic information ladder" — unreliable.

**What survived is the infrastructure and the methodology**, which is what
caught the errors and is now reused wholesale by the current strategy.

---
---

# PART 3 — Onboarding

## 3.1 Accounts to create

| Account | Cost | Purpose |
|---|---|---|
| **IBKR paper trading** | free | Live execution. Request a paper-only login (avoids 2FA blocking headless Gateway) |
| **EIA API key** | free | [eia.gov/opendata](https://www.eia.gov/opendata/) — instant |
| **WRDS** (via Queen's) | institutional | Research data |
| **GitHub** | free | Repo access — ask a lead |

## 3.2 Local setup

```bash
git clone <repo-url> && cd storage-stress
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -e ".[dev,live,dashboard]"
cp .env.example .env          # then fill in keys — NEVER commit .env
pytest                        # should pass
```

`.env` keys: `EIA_API_KEY`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_ENDPOINT`, `R2_BUCKET`, `IBKR_HOST`, `IBKR_PORT`.

## 3.3 IB Gateway

Download IB Gateway (not TWS — lighter). Log in with **paper** credentials, then
**Configure → Settings → API → Settings**:
- ✅ Enable ActiveX and Socket Clients
- ❌ uncheck Read-Only API
- Socket port **4002**; allow connections from localhost
- **Configure → Lock and Exit → Auto restart** (avoids nightly re-login)

Verify: `python scripts/verify_xsec_universe.py`

## 3.4 First tasks for a new member

1. Read `notebooks/03_strategy_review.ipynb` end to end — it's the full story
   including the mistakes.
2. Run the backtest: `python scripts/run_xsec.py`
3. Run a dry-run rebalance: `python scripts/run_xsec_live.py --job rebalance --dry-run`
4. Pick a small open question from the backlog and write up what you find.

## 3.5 Working norms

- **Logic in `src/`, thin wrappers in `scripts/`.** Notebooks explore; they never
  hold logic other code imports.
- **Config in YAML, secrets in `.env`.** No parameters hardcoded in scripts.
- **Every claim gets a number.** "It seems better" is not a result.
- **Report negatives.** A null result that's real is worth more than a positive
  one that isn't. Two of this project's most valuable findings were negative.

---
---

# PART 4 — References

## Academic

- **Gorton, G. & Rouwenhorst, K.G. (2006).** "Facts and Fantasies about
  Commodity Futures." *Financial Analysts Journal* 62(2). — foundational
  evidence for commodity risk premia.
- **Erb, C. & Harvey, C. (2006).** "The Strategic and Tactical Value of
  Commodity Futures." *Financial Analysts Journal* 62(2). — term structure /
  carry as the dominant driver of returns.
- **Boons, M. & Prado, M.P. (2019).** "Basis-Momentum." *Journal of Finance*
  74(1). — the third factor in our ladder.
- **Szymanowska, M., de Roon, F., Nijman, T. & van den Goorbergh, R. (2014).**
  "An Anatomy of Commodity Futures Risk Premia." *Journal of Finance* 69(1). —
  spot vs term premia; sorts on basis, momentum, hedging pressure and liquidity
  produce spot premia of ~5–14% p.a.
- **Moskowitz, T., Ooi, Y.H. & Pedersen, L.H. (2012).** "Time Series Momentum."
  *Journal of Financial Economics* 104(2).
- **Hong, Y. & Klabjan, D. (2026).** "Hierarchical Graph Learning for Calendar
  Spread Strategies in Commodity Futures Markets." arXiv:2606.25811. — shows
  calendar spreads attain higher information ratios and lower risk than
  long-only. *Note: does not model transaction costs.*

## Technical

- [ib_async documentation](https://ib-api-reloaded.github.io/ib_async/) —
  successor to the unmaintained `ib_insync`
- [IBKR TWS API guide](https://interactivebrokers.github.io/tws-api/)
- [EIA API v2](https://www.eia.gov/opendata/documentation.php)
- [DuckDB httpfs / S3](https://duckdb.org/docs/extensions/httpfs.html)
- [WRDS documentation](https://wrds-www.wharton.upenn.edu/)
- CME contract specifications — per-market margin, multipliers, delivery rules

## Internal documents

| Document | Contents |
|---|---|
| `docs/PIVOT_A_STRATEGY.md` | Full spec of the current strategy |
| `docs/FRAMEWORK.md` | Live execution architecture, diagrams |
| `docs/strategy.md` | The retired natural-gas thesis (kept for the record) |
| `notebooks/03_strategy_review.ipynb` | Complete technical review with runnable code |
| `config/xsec.yaml` | The frozen specification |

---

## Change log

| Date | Change |
|------|--------|
| 2026-08-12 | Handbook created. Universe narrowed to 17 markets for margin capacity; live signals moved from R2 mirror to IBKR. |
