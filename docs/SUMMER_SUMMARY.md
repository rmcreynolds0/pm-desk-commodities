# Summer 2026 — Progress, Results, and Two Pivots

**A record of what was built, what was tested, what failed, and why the strategy
changed twice.** Every claim below is tied to a reproducible artifact; see the
[Evidence Index](#9-evidence-index).

*Prepared 2026-08-12*

---

## 1. Executive summary

The summer produced three things:

1. **A validated research and execution platform** — data pipelines, backtesting
   with roll-safe returns and enforced costs, a null-benchmark methodology, a
   live IBKR execution engine, per-agent ledgers and dashboards.
2. **A rigorously killed hypothesis.** The original natural-gas storage-stress
   strategy was tested against its own pre-registered criteria and **failed**.
   This is reported as a result, not hidden.
3. **A replacement strategy with materially better evidence** — a cross-sectional
   commodity factor portfolio whose every tier beats a calibrated random
   benchmark and which survives 5× transaction costs.

**The most important output is arguably the methodology**, because it is what
detected three separate false-positive results — including one that briefly
looked like a working strategy.

---

## 2. What was built

| Component | Purpose |
|---|---|
| **Data layer** | EIA API v2 (storage/prices), WRDS/Datastream via Cloudflare R2 + DuckDB (8,538 futures contracts, 1990–2026), IBKR historical bars |
| **Signal library** | Deliverability Stress Index pipeline (NG); carry / momentum / basis-momentum (commodities) |
| **Backtest engine** | Roll-safe returns, enforced transaction costs, bootstrap CIs, calibrated null distributions |
| **Execution engine** | IBKR integration via `ib_async`, contract resolution across 6 exchanges, order management, delivery guards |
| **Ledgers** | Per-agent SQLite books — source of truth for attribution |
| **Dashboards** | Streamlit: live books (8501), research results (8502) |
| **Tests** | 17 passing, including regression tests for every bug found |

**Scale:** ~4,500 lines across 12 modules and 10 scripts. Full technical record
in `notebooks/03_strategy_review.ipynb`.

---

## 3. Strategy 1 — Natural-gas storage stress

### The hypothesis

Three linked claims:
1. Post-2016 LNG exports (Sabine Pass, Feb 2016) tightened US gas deliverability.
2. Salt-cavern utilisation measures that stress — salt caverns are *fast-cycling*
   storage (multiple turns per season vs ~1 for depleted reservoirs), so they are
   the marginal short-term balancing supply.
3. NYMEX calendar spreads had **not repriced** this, leaving an edge.

### The design — an information ladder

Four agents, each seeing exactly one more piece of information, so the gap
between adjacent tiers isolates that piece's contribution:

| Agent | Signal | Adds |
|---|---|---|
| `agent_zero` | weekly coin flip | nothing — **null benchmark** |
| `agent_one` | 5y z-score of salt net flow | real storage data |
| `agent_two` | convex stress → EWMA → deseasonalised | transformation + smoothing |
| `agent_dsi` | + basis residualisation + volatility gate | full signal |

This design is the project's core methodological contribution and carried over
to the replacement strategy.

---

## 4. Why it failed — the proof

Three independent findings. **Two were defects in measurement; the third was a
defect in the hypothesis.** They are presented in the order discovered because
the sequence matters: the first two had to be fixed before the third could even
be tested honestly.

### 4.1 Measurement defect — phantom P&L at contract rolls

Multi-year spread series are **spliced** from successive contract pairs. On the
splice date the quoted level jumps, because the two pairs are different
instruments. Differencing across that jump books profit no trader could capture.

Real example: `2026-01-21 Feb/Mar = +0.673` → `2026-01-22 Mar/Apr = +0.093`. The
backtest booked **−0.580**. Nothing moved.

| Instrument | Roll jump vs normal move | Share of ALL movement on roll days |
|---|---|---|
| prompt | **8.1×** | **31.0%** |
| seasonal | **6.6×** | **26.8%** |
| mar_apr | **14.4×** | 5.8% |

**Roughly a quarter to a third of the "movement" the strategy traded on was an
artifact.** Fixed; regression-tested.

### 4.2 Measurement defect — the backtest ran cost-free

Slippage was computed, written into the equity cell, then **overwritten** by the
next line of the equity recursion. Costs were never applied — violating the
strategy's own written rule that *"a strategy that only works pre-cost is
rejected."*

**The tell:** a coin flip trading 253 times showed a profit. Random trading
cannot have edge, so the measurement had to be wrong. Fixed; regression-tested.

### 4.3 Methodological defect — one random seed is not a benchmark

`agent_zero` used a single seed — one sample from a distribution, not a
benchmark. Running **200 random agents** settled it:

| Instrument | Null p5 | p50 | p95 | The single seed used | Its percentile |
|---|---|---|---|---|---|
| prompt | −41.7% | −10.5% | +17.1% | −3.3% | 69th |
| seasonal | −31.7% | −0.3% | +24.0% | **+26.3%** | **95th** |
| mar_apr | −24.9% | −5.1% | +15.4% | +5.7% | 78th |

The **+26.3% that briefly looked like signal was the 95th percentile of pure
chance.**

### 4.4 The decisive test — no agent beat chance

With both bugs fixed and a proper null:

| Instrument | Null band (p5–p95) | agent_one | agent_two | agent_dsi |
|---|---|---|---|---|
| prompt | −41.7 … +17.1 | +3.7 | −7.1 | −5.0 |
| seasonal | −31.7 … +24.0 | −1.1 | −5.7 | −2.8 |
| mar_apr | −24.9 … +15.4 | +3.8 | −6.0 | −3.8 |

**Not one signal agent, on any instrument, fell outside the null band.** Every
result is statistically indistinguishable from random trading.

### 4.5 The hypothesis itself was wrong — the week-1 regime gate

The strategy document contained an explicit pause-gate that had never been run:

> *"Post-2016 LNG export growth tightened the deliverability system; calendar
> spreads have not fully repriced this regime. (Week-1 statistical test; if it
> fails, the project pauses for rescoping.)"*

It only became testable once Datastream contract data (back to 1990) replaced
EIA's four-contracts-out feed. Split at the first Sabine Pass cargo (2016-02-24),
**1,527 weekly observations**:

| Metric | Pre-2016 (999 wks) | Post-2016 (528 wks) | Result |
|---|---|---|---|
| Weekly volatility | 0.1932 | **0.1376** | **−29%**, p=0.033 |
| Seasonal amplitude | 0.5691 | 0.5656 | unchanged (0.99×) |
| Mean level | −0.472 | −0.330 | +0.14, p=0.002 |
| Mean reversion (AR1) | +0.057 | +0.001 | ~vanished |

**The premise predicted the opposite of what happened.** A tightening system
should produce *more* volatile spreads and *larger* seasonal swings — scarcity
shows up as violent seasonality. Instead spreads became **29% less volatile**
with unchanged seasonality and mean reversion arbitraged to zero.

That is the signature of a market becoming **more efficient**, not more stressed.

**This explains the null result coherently: no agent beat chance because there
was no mispricing to capture.** Per the project's own rules, the gate failing
triggers a pause and rescope.

---

## 5. The pivot — from one hypothesis to documented premia

The core weakness of Strategy 1 was structural: it was **one original hypothesis
about one market**. When the hypothesis failed, nothing remained.

Strategy 2 harvests premia that are **documented and replicated across decades of
literature and dozens of markets**. The bet changes from *"I have found something
nobody else has"* to *"these known risk premia continue to be compensated, and I
can capture them after costs."*

| | Strategy 1 (NG) | Strategy 2 (cross-sectional) |
|---|---|---|
| Markets | 1 | 17 |
| Instrument | calendar spread | front contract, long/short |
| Evidence base | one original hypothesis | decades of replicated literature |
| Sample | 6–48 trades | 6,676 days × 17 markets |
| Result | **no edge vs chance** | **beats chance at every tier** |

---

## 6. Strategy 2 — cross-sectional commodity factors

### Design

Dollar-neutral long/short: rank markets monthly by a composite score, long the
top third, short the bottom third. Three factors:

- **Carry** = `ln(front/second) / Δt_years` — term-structure slope; backwardation
  signals scarcity
- **Momentum** = 12-month trailing return
- **Basis-momentum** = `momentum(front) − momentum(second)` — how the *curve*
  has moved (Boons & Prado 2019)

The **same four-agent ladder** applies: `agent_0` random, then one factor added
per tier.

### Results (22-market universe, 2000–2026, 6,676 trading days)

| Agent | Signal | Ann. return | Sharpe | Max DD | vs chance |
|---|---|---|---|---|---|
| `agent_0` | random (null) | −0.26% | 0.08 | −66% | — |
| `agent_1` | carry | +2.91% | 0.24 | −75% | 98th pctile ✓ |
| `agent_2` | + momentum | +5.56% | 0.36 | −64% | 100th ✓ |
| `agent_3` | + basis-momentum | **+11.54%** | **0.61** | −36% | 100th ✓ |

**Monotonic — and every signal tier beats the null band.**

### The cost of narrowing the universe (measured, not assumed)

Margin constraints on the paper account forced a proposal to drop the five most
capital-hungry markets (gold, heating oil, feeder cattle, copper, palladium).
Re-running the identical ladder on the resulting **17-market** universe shows
this is **not** a free simplification:

| Agent | 22 markets | 17 markets | Change |
|---|---|---|---|
| `agent_0` random | 0.08 | 0.16 | — |
| `agent_1` carry | **0.24** (98th, beats) | **0.12** (88th, *within noise*) | ✗ **stops beating chance** |
| `agent_2` +momentum | 0.36 | 0.36 | ~unchanged |
| `agent_3` **full** | **0.61** | **0.59** | −0.03 |
| `agent_3` max DD | **−36%** | **−53%** | **materially worse** |

Three things degrade:
1. **Carry stops beating chance** — its annual return collapses from +2.91% to
   +0.05%. The dropped markets (metals especially) were contributing
   disproportionately to the carry signal.
2. **Drawdown worsens by 17 points** (−36% → −53%), as expected from a thinner
   cross-section: terciles fall from ~7 to ~6 names per side.
3. **The ladder breaks at the bottom** — random (0.16) now edges out carry (0.12).

`agent_3` still beats chance at the 100th percentile, so the full signal
survives. But **narrowing is a workaround for an account constraint, not a
strategy improvement**, and it should be reversed once the constraint is lifted.
Recorded here so the decision is auditable rather than silent.

### Robustness

| Test | Result |
|---|---|
| **5× transaction costs** | 0.61 → **0.52**. Not cost-fragile |
| **Null sanity check** | random earns Sharpe **+0.02 at zero cost** — properly zero, confirming the simulator manufactures nothing |
| **Not a turnover artifact** | at zero cost for both: agent_3 0.64 vs random p95 0.28 |
| **Volatility targeting (10%)** | Sharpe 0.61 → **0.70**; max DD −36% → **−19%** |
| **Post-publication decay** | 0.59 (pre-2019) → 0.69 (post-2019) → **0.93** (last 3y). No decay |
| **Hardest test** — post-2019 + 3× costs + vol-targeted | **Sharpe 0.78, +8.2%/yr, −19.3% DD** |

### A methodological result worth reporting

Carry's **standalone** Sharpe has decayed to **−0.51** over three years — likely
arbitraged since commodity index investing scaled post-2005. Dropping it looked
obvious. A **pre-committed, single-shot test** (rule fixed in advance: drop only
if better in ≥3 of 4 conditions) said otherwise:

| Condition | With carry | Without | Δ |
|---|---|---|---|
| full sample | 0.612 | 0.430 | −0.182 |
| post-2019 | 0.690 | 0.650 | −0.040 |
| vol-targeted | 0.698 | 0.442 | −0.256 |
| post-2019 + 3× cost | 0.784 | 0.743 | −0.041 |

**0 of 4 improved → keep carry.** Standalone Sharpe was the wrong criterion:
carry is weakly correlated with the other factors, so it cuts portfolio
*variance* more than it cuts return. A factor can be a poor standalone bet and
still earn its place through diversification.

---

## 7. Live execution — honest status

### What is proven to work

The pipeline runs **end to end against real IBKR infrastructure**:

- **Contract resolution:** 22 of 23 markets resolve across NYMEX, COMEX, CBOT,
  CME and NYBOT. (MWE/Minneapolis wheat does not — MGEX was absorbed by MIAX.)
- **Market data:** available for all resolved markets under delayed entitlements.
- **Orders reach the exchange and fill.** Recorded evidence in `data/live/books.db`:
  `agent_zero` ENTRY, short NGU26 / long NGF27 combo — **status `Filled`**.
  During the first cross-sectional run, further orders filled (CLU6 +17,
  NGU26 −52) before margin was exhausted.
- **Per-agent attribution** via `orderRef` tags and the local ledger.

### What is NOT yet proven — stated plainly

**There is no meaningful live track record.** Specifically:

| Metric | Actual |
|---|---|
| Daily marks recorded | 8 (across 4 NG agents) |
| Orders placed on IBKR | 2 (NG) + partial xsec run |
| Completed round trips | 1 |
| Days of live equity curve | ~1 |

The natural-gas books were frozen from 2026-07-25 (IB Gateway went down; the
scheduler survived but every job failed). Since that strategy was retired
shortly after, the gap was not worth backfilling.

**The forward out-of-sample record for the current strategy begins now.** That is
the honest position: extensive backtest evidence, functioning execution
infrastructure, and a track record that has not yet accumulated.

### Constraints discovered in live testing

Four issues that no backtest would have surfaced:

1. **Ledger/reality divergence** — rejected orders were recorded as filled (54
   phantom positions vs 3 real). Fixed: only actual fills update the books.
2. **The research data source cannot drive live trading.** The R2 mirror is a
   snapshot: every market stale, 43 days (softs) to **243 days** (corn, oats).
   Live factors now computed from IBKR.
3. **IBKR delivery policy** rejects orders ~15 days from expiry. Guard raised
   5 → 25 days.
4. **Order size cap** — IBKR refuses non-algo orders above ~64 lots; cheap
   markets need 70–100. Now split into child orders.

Plus a hard constraint: the paper account (~$722k USD) could not margin 4 agents
across 22 markets — three positions consumed 95% of available margin. Resolved by
raising the paper balance and **narrowing the universe to 17 markets**, dropping
the five most margin-hungry (gold, heating oil, feeder cattle, copper,
palladium). The backtest was re-run on exactly those 17 markets so the forward
comparison is like-for-like.

---

## 8. What the summer actually demonstrates

1. **A hypothesis was tested rigorously and rejected** on its own pre-registered
   criteria — including a gate that had gone unrun.
2. **Three false positives were caught** by methodology, not luck: phantom roll
   P&L, cost-free backtesting, and a lucky random seed that looked like a +26%
   strategy.
3. **A replacement strategy was built on stronger evidentiary footing** and
   subjected to the same tests, which it passes.
4. **Live execution infrastructure works** — orders reach the market and fill,
   with per-agent attribution.
5. **The forward validation has not happened yet.** It starts now, and it is the
   only test that cannot be overfit.

---

## 9. Evidence index

Every claim maps to a reproducible artifact.

| Claim | Artifact | Regenerate with |
|---|---|---|
| Roll artifact magnitudes | measured output | `scripts/compare_instruments.py` |
| Cost bug existed | `tests/test_execution.py::test_trading_costs_are_actually_charged` | `pytest` |
| Roll bug existed | `tests/test_execution.py::test_roll_produces_no_phantom_pnl` | `pytest` |
| Null distributions (200 seeds) | `data/processed/null_distribution.csv` | `scripts/null_distribution.py` |
| No NG agent beat chance | `data/processed/instrument_comparison.csv` | `scripts/compare_instruments.py` |
| Regime gate failure | test output, 1,527 weekly obs | `scripts/regime_test.py` |
| Cross-sectional ladder results | `data/processed/xsec_ladder.csv` | `scripts/run_xsec.py` |
| Per-agent daily equity curves | `data/processed/xsec_equity_curves.csv` (572 KB) | `scripts/run_xsec.py` |
| Robustness battery | test output | `scripts/xsec_robustness.py`, `xsec_stage2.py` |
| Carry-drop decision | test output | `scripts/carry_drop_test.py` |
| IBKR universe verification | `data/processed/xsec_universe_check.csv` | `scripts/verify_xsec_universe.py` |
| **Real orders filled on IBKR** | `data/live/books.db` → `orders`, `fills` | `run_agents.py --job status` |
| NG per-agent equity | `data/processed/equity_agent_*.csv` | `scripts/run_backtest.py` |
| Full technical narrative | `notebooks/03_strategy_review.ipynb` | — |

---

## 10. Next steps

**Immediate**
1. Flatten leftover positions and launch the 17-market book across all four agents.
2. Accumulate forward out-of-sample data — the decisive test.

**Fall term**
3. Onboard members (see `docs/PROJECT_HANDBOOK.md`).
4. Individual research projects: additional factors (hedging pressure, value,
   skewness), liquidity-aware cost modelling, execution quality.

**Open questions worth a research project each**
- Does carry's decay reverse, or is the premium permanently arbitraged?
- Does basis-momentum survive its own post-publication window as data accumulates?
- Can a liquidity filter improve realised (as opposed to assumed) costs?
