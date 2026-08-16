# Summer 2026 — Progress, Results, and Two Pivots

**QUANTT Commodities**
*Prepared 2026-08-12*

Every quantitative claim below maps to a reproducible artifact — see the
[Evidence Index](#8-evidence-index).

---

## 1. Executive summary

The summer produced three outcomes:

1. **A validated research and execution platform** — data pipelines, backtesting
   with roll-safe returns and enforced costs, null-benchmark methodology, a live
   IBKR execution engine, per-agent ledgers, and dashboards. ~4,500 lines across
   12 modules, 17 passing tests.
2. **A rigorously killed hypothesis.** The original natural-gas storage-stress
   strategy was tested against its own pre-registered criteria and **failed**.
   Reported here as a result, not buried.
3. **A replacement strategy with materially stronger evidence** — a
   cross-sectional commodity factor portfolio whose every tier beats a calibrated
   random benchmark and which survives 5× transaction costs.

**The most transferable output is the methodology**, because it is what detected
three separate false-positive results — one of which briefly looked like a
working +26% strategy.

---

## 2. Strategy 1 — natural-gas storage stress

### The hypothesis

1. Post-2016 LNG exports (Sabine Pass, Feb 2016) tightened US gas deliverability
2. Salt-cavern utilisation measures that stress — salt caverns are *fast-cycling*
   storage (several turns per season vs ~1 for depleted reservoirs), making them
   the marginal short-term balancing supply
3. NYMEX calendar spreads had **not repriced** this, leaving an edge

### The design — an information ladder

Four agents, each seeing exactly one more piece of information:

| Agent | Signal | Adds |
|---|---|---|
| `agent_zero` | weekly coin flip | nothing — **null benchmark** |
| `agent_one` | 5y z-score of salt net flow | real storage data |
| `agent_two` | convex stress → EWMA → deseasonalised | transformation + smoothing |
| `agent_dsi` | + basis residualisation + volatility gate | full signal |

This design is the project's core methodological contribution and carried
directly into the replacement strategy.

---

## 3. Why it failed — with proof

Three independent findings. **Two were defects in measurement; the third was a
defect in the hypothesis.** Order matters: the first two had to be fixed before
the third could be tested honestly.

### 3.1 Phantom P&L at contract rolls

Multi-year spread series are **spliced** from successive contract pairs. On the
splice date the level jumps, because the two pairs are different instruments.
Differencing across that jump books profit no trader could capture.

Real example:
```
2026-01-21   Feb/Mar spread = +0.673
2026-01-22   Mar/Apr spread = +0.093     backtest booked "−0.580"
```
Nothing moved — in reality you close one spread and open another.

| Instrument | Roll jump vs normal daily move | Share of ALL movement on roll days |
|---|---|---|
| prompt | **8.1×** | **31.0%** |
| seasonal | **6.6×** | **26.8%** |
| mar_apr | **14.4×** | 5.8% |

**A quarter to a third of the "movement" the strategy traded on was an artifact.**

### 3.2 The backtest was running cost-free

Slippage was computed, written into the equity cell, then **overwritten** by the
next line of the equity recursion:

```python
equity.iloc[i] -= slippage...                            # cost computed
equity.iloc[i] = equity.iloc[i-1] + pnl_series.iloc[i]   # cost DISCARDED
```

This violated the strategy's own written rule — *"a strategy that only works
pre-cost is rejected."*

**The tell:** a coin flip trading 253 times showed a profit. Random trading
cannot have edge, so the measurement had to be wrong.

### 3.3 One random seed is not a benchmark

`agent_zero` used a single seed — one sample from a distribution. Running **200
random agents** settled it:

| Instrument | Null p5 | p50 | p95 | Seed actually used | Its percentile |
|---|---|---|---|---|---|
| prompt | −41.7% | −10.5% | +17.1% | −3.3% | 69th |
| seasonal | −31.7% | −0.3% | +24.0% | **+26.3%** | **95th** |
| mar_apr | −24.9% | −5.1% | +15.4% | +5.7% | 78th |

**The +26.3% that briefly looked like signal was the 95th percentile of chance.**

### 3.4 The decisive test — no agent beat chance

With both bugs fixed and a proper null distribution:

| Instrument | Null band (p5–p95) | agent_one | agent_two | agent_dsi |
|---|---|---|---|---|
| prompt | −41.7 … +17.1 | +3.7 | −7.1 | −5.0 |
| seasonal | −31.7 … +24.0 | −1.1 | −5.7 | −2.8 |
| mar_apr | −24.9 … +15.4 | +3.8 | −6.0 | −3.8 |

**Not one signal agent, on any instrument, fell outside the null band.**

### 3.5 The hypothesis itself was wrong

The strategy document contained a pause-gate that had never been run:

> *"Post-2016 LNG export growth tightened the deliverability system; calendar
> spreads have not fully repriced this regime. (Week-1 statistical test; if it
> fails, the project pauses for rescoping.)"*

It became testable only once Datastream contract data (back to 1990) replaced
EIA's four-contracts-out feed. Split at the first Sabine Pass cargo (2016-02-24),
**1,527 weekly observations**:

| Metric | Pre-2016 (999 wks) | Post-2016 (528 wks) | Result |
|---|---|---|---|
| Weekly volatility | 0.1932 | **0.1376** | **−29%**, p = 0.033 |
| Seasonal amplitude | 0.5691 | 0.5656 | unchanged (0.99×) |
| Mean level | −0.472 | −0.330 | +0.14, p = 0.002 |
| Mean reversion (AR1) | +0.057 | +0.001 | ~vanished |

**The premise predicted the opposite of what happened.** A tightening system
produces *more* volatile spreads and *larger* seasonal swings — scarcity shows up
as violent seasonality. Instead spreads became **29% less volatile** with
unchanged seasonality and mean reversion arbitraged to zero.

That is the signature of a market becoming **more efficient**, not more stressed.
It explains the null result coherently: **no agent beat chance because there was
no mispricing to capture.**

---

## 4. The pivot

Strategy 1's structural weakness: it was **one original hypothesis about one
market**. When the hypothesis failed, nothing remained.

Strategy 2 harvests premia **documented and replicated across decades of
literature and dozens of markets**. The bet changes from *"I found something
nobody else has"* to *"these known risk premia remain compensated, and I can
capture them after costs."*

| | Strategy 1 (NG) | Strategy 2 (cross-sectional) |
|---|---|---|
| Markets | 1 | 22 |
| Instrument | calendar spread | front contract, long/short |
| Evidence base | one original hypothesis | decades of replicated literature |
| Sample | 6–48 trades | 6,676 days × 22 markets |
| Result | **no edge vs chance** | **beats chance at every tier** |

---

## 5. Strategy 2 — results

Dollar-neutral long/short across 22 markets. Three factors — **carry**
(term-structure slope), **momentum** (12-month return), **basis-momentum**
(momentum of front minus second). Same four-agent ladder.

### Ladder (2000–2026, 6,676 trading days)

| Agent | Signal | Ann. return | Sharpe | Max DD | vs chance |
|---|---|---|---|---|---|
| `agent_0` | random (null) | −0.26% | 0.08 | −66% | — |
| `agent_1` | carry | +2.91% | 0.24 | −75% | 98th pctile ✓ |
| `agent_2` | + momentum | +5.56% | 0.36 | −64% | 100th ✓ |
| `agent_3` | + basis-momentum | **+11.54%** | **0.61** | −36% | 100th ✓ |

**Monotonic — every signal tier beats the null band.**

### Robustness

| Test | Result |
|---|---|
| **5× transaction costs** | 0.61 → **0.52**. Not cost-fragile |
| **Null sanity check** | random earns Sharpe **+0.02 at zero cost** — properly zero, confirming the simulator manufactures nothing |
| **Not a turnover artifact** | at zero cost for both: agent_3 0.64 vs random p95 0.28 |
| **Volatility targeting (10%)** | Sharpe 0.61 → **0.70**; max DD −36% → **−19%** |
| **Post-publication decay** | 0.59 (pre-2019) → 0.69 (post-2019) → **0.93** (last 3y). No decay |
| **Hardest test** — post-2019 + 3× costs + vol-targeted | **Sharpe 0.78, +8.2%/yr, −19.3% DD** |

### Two decisions made by measurement, not preference

**A. Keep carry, despite a negative standalone Sharpe.**
Carry's standalone Sharpe has decayed to **−0.51** over three years. Dropping it
looked obvious. A **pre-committed, single-shot** test (rule fixed in advance:
drop only if better in ≥3 of 4 conditions) said otherwise:

| Condition | With carry | Without | Δ |
|---|---|---|---|
| full sample | 0.612 | 0.430 | −0.182 |
| post-2019 | 0.690 | 0.650 | −0.040 |
| vol-targeted | 0.698 | 0.442 | −0.256 |
| post-2019 + 3× cost | 0.784 | 0.743 | −0.041 |

**0 of 4 improved → keep carry.** Standalone Sharpe was the wrong criterion:
carry is weakly correlated with the other factors, so it cuts portfolio
*variance* more than return. A factor can be a poor standalone bet and still earn
its place through diversification.

**B. Reject narrowing the universe to 17 markets.**
Margin constraints suggested dropping the five most capital-hungry markets.
Re-running the identical ladder showed this is not free:

| Agent | 22 markets | 17 markets | Change |
|---|---|---|---|
| `agent_1` carry | 0.24 (98th, beats) | 0.12 (88th, *within noise*) | ✗ stops beating chance |
| `agent_3` full | 0.61 | 0.59 | −0.03 |
| `agent_3` max DD | **−36%** | **−53%** | 17 points worse |

**Universe size is a strategy parameter, not an operational convenience.** The
account is sized to the universe, never the reverse.

---

## 6. Live execution — evidence and honest status

### 6.1 Proof: four independently-tracked agents

Both strategies run **four agents in parallel** on one IBKR paper account.
Separation is by `orderRef` tag plus a local ledger, because agents frequently
hold opposite positions that net to zero at the account level while both books
carry real risk.

**Ledger `data/live/books.db` (natural-gas strategy) — four registered agents,
8 daily equity marks across 2 dates:**

| Agent | 2026-07-25 | 2026-08-12 |
|---|---|---|
| `agent_zero` | **199,970** | **198,710** |
| `agent_one` | 200,000 | 200,000 |
| `agent_two` | 200,000 | 200,000 |
| `agent_dsi` | 200,000 | 200,000 |

This is the attribution mechanism working: `agent_zero` took a real position and
its equity diverged from the other three, which correctly stayed flat because
their signals never fired. The **−$1,290** move is real mark-to-market P&L on a
real IBKR position.

### 6.2 Proof: real orders reaching the exchange

From the `orders` table:

| Agent | Action | Qty | Front leg | Deferred leg | Status |
|---|---|---|---|---|---|
| `agent_zero` | ENTRY | 1 | NGU26 | NGF27 | **Filled** |
| `agent_zero` | EXIT | 1 | NGU26 | NGF27 | PreSubmitted |

The ENTRY was a genuine 2-leg NYMEX combo order, filled on IBKR, tagged
`agent_zero|ENTRY|2026-07-24`, producing one completed round trip in the `trades`
table. During cross-sectional testing further orders filled (CLU6 +17,
NGU26 −52) before margin was exhausted.

### 6.3 Proof: infrastructure verified against real IBKR

`data/processed/xsec_universe_check.csv` records live verification of all 23
candidate markets — contract resolution, exchange, expiry, multiplier, price
magnifier, live price, and computed notional. **22 of 23 resolved with prices**
across NYMEX, COMEX, CBOT, CME and NYBOT.

### 6.4 What is NOT yet proven — stated plainly

**There is no meaningful live track record.**

| Metric | Actual |
|---|---|
| Daily equity marks | 8 (4 agents × 2 dates) |
| Orders placed on IBKR | 2 (NG) + partial cross-sectional run |
| Completed round trips | 1 |
| Days of continuous live equity curve | ~1 |

The natural-gas books froze on 2026-07-25 when IB Gateway went down — the
scheduler kept running but every job failed for want of a broker connection.
Since that strategy was retired days later, the gap was not backfilled.

**The forward out-of-sample record for the current strategy begins 2026-08-13**,
with the scheduler armed to rebalance all four agents at 10:30 ET and mark daily
at 16:30 ET.

That is the honest position: **extensive backtest evidence, verified execution
infrastructure, and a track record that has not yet accumulated.**

### 6.5 Constraints discovered only in live testing

Five issues no backtest would surface:

1. **Ledger/reality divergence** — rejected orders were recorded as filled (54
   phantom positions vs 3 real). Fixed: only actual fills update the books.
2. **The research data source cannot drive live trading.** The R2 mirror is a
   snapshot: every market stale, 43 days (softs) to **243 days** (grains). Live
   factors now come from IBKR.
3. **IBKR delivery policy** rejects orders ~15 days from expiry. Guard raised
   5 → 25 days.
4. **Order size cap** — IBKR refuses non-algo orders above ~64 lots; cheap
   markets need 70–100. Now split into child orders.
5. **Market hours matter.** A run at 17:50 ET fell inside the CME settlement
   break; every order sat `PreSubmitted`. Rebalances now fire at 10:30 ET when
   all 22 markets are liquid.

Plus a hard constraint: the paper account (~$722k USD) could not margin four
agents across 22 markets — three positions consumed 95% of available margin.
Resolved by raising the paper balance rather than shrinking the universe, for the
reason documented in §5B.

---

## 7. What the summer demonstrates

1. **A hypothesis was tested rigorously and rejected** on its own pre-registered
   criteria — including a gate that had gone unrun.
2. **Three false positives were caught** by methodology, not luck: phantom roll
   P&L, cost-free backtesting, and a lucky seed that looked like a +26% strategy.
3. **A replacement strategy was built on stronger evidentiary footing** and
   passes the same tests.
4. **Live execution works** — orders reach the market and fill, with per-agent
   attribution proven in the ledger.
5. **Forward validation has not happened yet.** It begins now, and it is the only
   test that cannot be overfit.

---

## 8. Evidence index

| Claim | Artifact | Regenerate with |
|---|---|---|
| Roll artifact magnitudes | measured output | `scripts/compare_instruments.py` |
| Cost bug existed | `test_trading_costs_are_actually_charged` | `pytest` |
| Roll bug existed | `test_roll_produces_no_phantom_pnl` | `pytest` |
| Null distributions (200 seeds) | `data/processed/null_distribution.csv` | `scripts/null_distribution.py` |
| No NG agent beat chance | `data/processed/instrument_comparison.csv` | `scripts/compare_instruments.py` |
| Regime gate failure (1,527 wks) | test output | `scripts/regime_test.py` |
| Cross-sectional ladder results | `data/processed/xsec_ladder.csv` | `scripts/run_xsec.py` |
| Per-agent daily equity curves | `data/processed/xsec_equity_curves.csv` (572 KB) | `scripts/run_xsec.py` |
| Robustness battery | test output | `xsec_robustness.py`, `xsec_stage2.py` |
| Carry-drop decision | test output | `scripts/carry_drop_test.py` |
| IBKR universe verification | `data/processed/xsec_universe_check.csv` | `scripts/verify_xsec_universe.py` |
| **Four agents tracked separately** | `data/live/books.db` → `marks` | `run_agents.py --job status` |
| **Real order filled on IBKR** | `data/live/books.db` → `orders`, `fills` | `run_agents.py --job status` |
| Full technical narrative | `notebooks/03_strategy_review.ipynb` | — |

---

## 9. Next steps

**Immediate**
- Forward paper trading launches 2026-08-13; all four agents, 22 markets
- Daily marks accumulate the out-of-sample record

**Fall term**
- Onboard members (`docs/01_TECHNICAL_INFRASTRUCTURE.md`)
- Individual research projects: additional factors (hedging pressure, value,
  skewness), liquidity-aware cost modelling, execution quality analysis

**Open questions, each worth a research project**
- Does carry's decay reverse, or is the premium permanently arbitraged?
- Does basis-momentum survive its own post-publication window as data accumulates?
- Can a liquidity filter improve realised (as opposed to assumed) costs?
- Does the ladder's ordering hold out-of-sample?
