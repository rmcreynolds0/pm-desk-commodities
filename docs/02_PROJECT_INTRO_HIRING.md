# QUANTT Commodities — Systematic Futures Trading

**Now recruiting analysts for the 2026–27 academic year**

---

## What we do

We build and run **systematic commodity futures strategies**, validated end to
end: market data → signal research → backtesting → live paper execution →
performance attribution.

The current strategy is a **dollar-neutral, cross-sectional long/short portfolio**
across 22 commodity futures markets — energy, metals, grains, softs, livestock.
Each month it ranks every market by a composite score, buys the top third and
sells the bottom third, harvesting three premia documented across decades of
academic literature:

| Factor | What it measures | Economic intuition |
|---|---|---|
| **Carry** | Slope of the forward curve | Backwardation signals scarcity — holders of the physical earn a convenience yield |
| **Momentum** | 12-month trailing return | Information diffuses slowly |
| **Basis-momentum** | How the *curve itself* moved | Imbalances in who intermediates the curve |

**Backtest, 2000–2026 (6,676 trading days):** Sharpe 0.61 unscaled, 0.70
vol-targeted, **0.78 post-2019 at 3× assumed costs**. Every signal tier beats a
200-run random benchmark. Now live on an IBKR paper account with four agents
running in parallel.

---

## What makes this project unusual

Most student trading projects backtest a strategy and stop. We do three things
closer to professional practice:

1. **A null benchmark that must earn zero.** Every strategy runs alongside a
   *random* agent. If it can't beat a distribution of coin flips, it has no edge
   — however good its own numbers look.
2. **An information ladder.** Four agents each see one more piece of the signal,
   so we *measure* what each component contributes rather than assert it.
3. **Live paper execution.** Backtests are hypotheses; forward performance is
   evidence.

### We killed our own first strategy — and that's the point

Our original strategy traded natural-gas calendar spreads on a storage-stress
signal. Over the summer we found that **27–31% of the "price movement" it traded
on was a data artifact** from splicing futures contracts; that the backtest was
**charging no transaction costs**, letting a 253-trade coin flip look profitable;
that a **+26% result was the 95th percentile of pure chance**; and that the
strategy's own pre-registered regime test **failed** — the market had become
*more* efficient post-2016, not less.

We retired it and pivoted to premia with far stronger evidentiary support.
**Finding your own errors before the market does is the most valuable skill in
this field**, and it is what we actually teach.

---

## Roles

### Quantitative Research Analyst — 2–3 positions
Design and test factor hypotheses; run robustness, significance and ablation
testing; contribute new signals to the strategy pipeline.

**Requirements:** Python (pandas, numpy); statistics (regression, hypothesis
testing, confidence intervals); willingness to read academic finance papers.
Prior finance coursework helpful but not required.

### Data Engineer — 1–2 positions
Own the data pipeline: database queries (SQL/DuckDB), API integrations, data
quality checks, and the warehouse layer.

**Requirements:** Python, SQL, familiarity with APIs. Genuine interest in data
*correctness* — a surprising share of our findings turned out to be data bugs.

### Execution & Infrastructure — 1–2 positions
The live trading engine: IBKR API integration, order management, position
reconciliation, monitoring and dashboards.

**Requirements:** Python; interest in systems that must be *correct*, not merely
functional. Docker/deployment experience welcome.

---

## What you'll learn

**How a strategy is actually validated** (pre-registration, null benchmarks,
out-of-sample discipline, honest kill criteria) · **real market microstructure**
(contract specs, margin, roll mechanics, delivery risk, transaction costs) ·
**a production stack** (Python, SQL/DuckDB, IBKR API, Docker, Streamlit) ·
**how to be wrong productively** — most findings are negative, and reporting them
clearly is a skill.

---

## Goals & timeline

| Period | Focus | Deliverable |
|---|---|---|
| **Sept–Oct** | Onboarding; forward paper trading accumulates out-of-sample data | Every member has run the pipeline end to end |
| **Nov–Dec** | Individual research projects begin | One documented hypothesis test per member |
| **Jan–Feb** | Candidate signals → ablation and robustness testing | Which new factors survive the null benchmark? |
| **Mar–Apr** | Consolidation; promote what survives | Updated strategy + written results |

**Our central goal this year:** accumulate a genuine out-of-sample track record.
Backtests can be overfit; forward data cannot. Everything else supports that.

**Commitment:** 5–10 hrs/week. No prior trading experience required — the
methodology is teachable; curiosity and rigour are not.

---

## How to apply

Contact `<CONTACT — fill in>`.

Tell us about something you've built or analysed, and — more interestingly — a
time you found out you were wrong about it.

---

*Technical details: `docs/01_TECHNICAL_INFRASTRUCTURE.md`*
*Summer results: `docs/03_SUMMER_RESULTS.md`*
