# PIVOT A — Cross-Sectional Commodity Factor Strategy

*Reference document: signal, agents, data endpoints, algorithms, instruments.*
*Status: research candidate, NOT deployed. Written 2026-08-08.*

Companion to `docs/strategy.md` (the original natural-gas thesis, now retired —
see §10) and `docs/FRAMEWORK.md` (the live execution architecture, which
transfers to this strategy largely unchanged).

---

## 1. What this strategy is

A **dollar-neutral, cross-sectional long/short commodity futures portfolio.**
Each month it ranks ~23 commodity markets by a composite factor score, goes
long the top third and short the bottom third, and holds to the next rebalance.

It replaces the natural-gas storage-stress thesis, which failed its own
pre-registered week-1 regime gate. The critical difference: this strategy is
built on **premia that are documented and replicated across decades of
academic literature**, rather than on a single-market hypothesis.

**Core economic claim:** commodity futures returns are predictable in the
cross-section by (a) the shape of each market's own forward curve, (b) its
recent trend, and (c) how its curve shape has been *changing*. These are
compensation for bearing hedging pressure and storage/convenience-yield risk —
not arbitrage, but risk premia that persist because someone must hold the
other side.

---

## 2. Instruments & securities

**23 commodity futures markets**, all USD-denominated, all primary listings
(Datastream `ldb='COM'`), spanning four sectors:

| Sector | Ticker : Market |
|--------|-----------------|
| **Energy** | `CL` Crude Oil (WTI) · `NG` Natural Gas · `HO` Heating Oil · `RB` Gasoline RBOB |
| **Metals** | `GC` Gold · `SI` Silver · `HG` Copper · `PL` Platinum · `PA` Palladium |
| **Grains / oilseeds** | `ZC` Corn · `ZS` Soybeans · `ZM` Soybean Meal · `ZL` Soybean Oil · `ZO` Oats · `KE` Wheat (HRW) · `MWE` Wheat (Minneapolis) |
| **Softs** | `CC` Cocoa · `KC` Coffee · `SB` Sugar · `CT` Cotton · `OJ` Orange Juice |
| **Livestock** | `LE` Live Cattle · `GF` Feeder Cattle |

**8,538 individual contracts**, 2000-01 → 2026-06.

**Deliberately excluded**, and why:
- **Mini contracts** (`QG`, `QM`, `QU`, `QH`, `QC`) — duplicate exposure to a
  market already in the universe.
- **Swaps, crack spreads, basis swaps** (`NN`, `HH`, `GZ`, `EN`, `NW`, …) —
  derivative of the underlying markets; including them would double-count.
- **Non-USD listings** — currency risk would contaminate the factor.

**What is actually traded:** the **front contract** of each market, defined as
the nearest delivery month whose expiry is more than **5 calendar days** away.
The guard exists so the book never holds into delivery — the same
never-take-delivery discipline the NG engine enforces.

---

## 3. Data endpoints

All price data comes from the **WRDS/Datastream futures schema mirrored to
Cloudflare R2** (`quantt-historical-market-data`), queried with DuckDB `httpfs`
over the S3-compatible API.

| Endpoint | Path | Fields used | Role |
|----------|------|-------------|------|
| **Contract master** | `wrds/tr_ds_fut/wrds_contract_info.parquet` | `exchtickersymb`, `futcode`, `contrdate`, `lasttrddate`, `isocurrcode`, `ldb` | Defines the universe; maps each contract to its delivery month and expiry |
| **Daily settlements** | `wrds/tr_ds_fut/dsfutcontrval.parquet` | `futcode`, `date_`, `settlement` | The price series — one row per contract per day |

**Credentials:** `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT`,
`R2_BUCKET` in `.env` (gitignored). Read by `connectivity._r2_duckdb()`.

**Endpoints NOT used by this strategy** (retained from the NG work):
EIA storage/prices, IBKR historical bars, Datastream commodity spot
(`tr_ds_comds`). IBKR remains the **execution** venue.

**Key contract-master detail:** `contrdate` is `MMYY`; years ≥ 90 decode to
19xx, else 20xx. Duplicate `(ticker, delivery)` rows are resolved by keeping
the later-expiring listing.

---

## 4. The signal — three factors

All three are computed per market per day, then **cross-sectionally
z-scored within each date** before blending. Standardisation is essential:
carry is an annualised log slope, momentum is a 12-month return — without it
one factor would dominate the blend purely through units.

### 4.1 Carry (term-structure slope)

```
carry = ln(front_price / second_price) / Δt_years
```

where `Δt_years` is the gap between the two delivery months. **Positive carry =
backwardation** (front trades above deferred), historically associated with
positive returns; negative = contango.

Annualising matters because markets list on different cycles — a 1-month gap
and a 3-month gap are not comparable raw.

*Economic content:* backwardation signals scarcity — inventories are tight and
holders of the physical earn a convenience yield. Longs are compensated for
providing the deferred supply.

### 4.2 Momentum

12-month (≈252 trading day) compounded return of the front contract,
`min_periods` = half the window.

*Economic content:* the most robust anomaly across essentially every asset
class; interpreted as slow information diffusion and/or under-reaction.

### 4.3 Basis-momentum (Boons & Prado)

```
basis_momentum = momentum(front) − momentum(second)
```

The difference between the trailing return of the front contract and that of
the *next* contract. It measures how the **curve itself** has been moving —
distinct from both the curve's current level (carry) and the market's overall
trend (momentum).

*Economic content:* captures imbalances in the intermediation of the futures
curve; empirically it prices assets that neither carry nor momentum explain.

---

## 5. The agent ladder

Same experimental design as the NG project: each rung adds **exactly one** new
piece of information, so the gap between adjacent rungs isolates that piece's
contribution. Defined in `agents/xsec.py :: AGENT_FACTORS`.

| Agent | Sees | Purpose |
|-------|------|---------|
| **`agent_0`** | *nothing* — draws a seeded random score per (date, market) | **Null benchmark.** Establishes what pure chance produces on this universe and cost structure. |
| **`agent_1`** | `carry` | The foundational, best-documented commodity premium. |
| **`agent_2`** | `carry` + `momentum` | Adds trend. |
| **`agent_3`** | `carry` + `momentum` + `basis_momentum` | **Full signal.** Adds curve dynamics. |

**Score = equal-weight mean of the standardised factors that rung can see.**
No factor weights are fitted — an explicit choice to avoid in-sample
optimisation.

**Critical methodological rule:** `agent_0` is run across **many seeds** to
build a *distribution*, not a single path. A signal agent is only interesting
if it lands outside the p5–p95 band of that distribution. A single random path
proves nothing — in the NG work, one lucky seed produced +26% and briefly
looked like signal.

---

## 6. Portfolio construction

| Element | Choice | Rationale |
|---------|--------|-----------|
| **Ranking** | Sort all markets by score each rebalance | Cross-sectional, not time-series |
| **Positions** | Long top tercile, short bottom tercile | Standard factor construction |
| **Weighting** | Equal weight within each side; each side sums to 1.0 gross | Simple, no fitted weights |
| **Neutrality** | Dollar-neutral (+1 long / −1 short, net 0) | Strips out common commodity beta so what remains is the *factor* |
| **Minimum breadth** | ≥ 6 markets on a date (≥ 2 per side) | Thinner cross-sections are noise |
| **Rebalance** | Month-end (`ME`), held between | Standard; keeps turnover realistic |
| **Look-ahead guard** | Weights set at close, `.shift(1)` before earning | Positions cannot earn the return that determined them |

### Volatility targeting

Optional, and materially beneficial. Targets **10% annualised**:

```
leverage_t = target_vol / trailing_realised_vol_{t-1}      (capped at 3×)
```

Realised vol is a 63-day rolling estimate, **shifted one day** so no
look-ahead. Turnover — and therefore cost — scales with leverage. In practice
average leverage is **0.53**, i.e. the rule mostly *de-risks*.

---

## 7. Costs

**10 bps per 1.0 of gross weight traded**, charged on turnover:

```
turnover_t = Σ |Δweight|          cost_t = turnover_t × 0.0010
```

This is deliberately conservative for liquid futures (real round-trip is
typically 2–5 bps). Robustness is reported at **1×, 3× and 5×**.

> **Why this is emphasised:** in the NG backtest, slippage was computed and then
> silently overwritten by the equity recursion, so the entire backtest ran
> **cost-free** — which let a 250-trade coin flip look profitable. Costs are now
> accumulated in a separate series and applied explicitly, with a regression
> test (`test_trading_costs_are_actually_charged`) locking the behaviour in.

---

## 8. Technical concepts that matter

### Roll-safe returns — the single most important implementation detail

A return is **only ever computed between two prices of the same contract**
(`groupby('futcode').pct_change()`), never across a contract change.

Building a continuous series by splicing successive contracts creates a level
discontinuity at each roll. On NG data those jumps measured **6–14× a normal
daily move and accounted for 27–31% of all series movement** — differencing
across them fabricates P&L no trader could capture. This strategy is immune by
construction because it never splices.

### Calibrated null distribution

Performance is judged against the distribution of many random agents on the
*same* universe, costs and constraints — not against zero and not against a
single random path. Bootstrap CIs on a strategy's own returns answer "is this
path's Sharpe distinguishable from zero," which is a **different and weaker
question** than "could chance have produced this."

### Cross-sectional standardisation

Per-date z-scoring makes heterogeneous factors comparable and makes the
portfolio depend on *relative* rankings, which is what a dollar-neutral
long/short book actually expresses.

### Sparse-agent Sharpe instability

Where an agent trades rarely, its return series is mostly zeros and Sharpe
becomes unstable. Compare such agents on total return and drawdown instead.
(Less binding here than in NG — this book holds positions continuously.)

---

## 9. Results to date

**Full sample (2000–2026, 6,676 trading days), baseline costs, unscaled:**

| Agent | Signal | Ann. ret | Sharpe | Max DD | vs chance |
|-------|--------|----------|--------|--------|-----------|
| `agent_0` | random | −0.26% | 0.08 | −66% | — |
| `agent_1` | carry | +2.91% | 0.24 | −75% | 98th pctile |
| `agent_2` | + momentum | +5.56% | 0.36 | −64% | 100th pctile |
| `agent_3` | + basis-momentum | +11.54% | **0.61** | −36% | 100th pctile |

**With 10% vol targeting:** `agent_3` → Sharpe **0.70**, max DD **−19%**.

**Hardest test — post-2019 only, 3× costs, vol-targeted:**
`agent_3` = Sharpe **+0.78**, +8.18%/yr, max DD −19.3%.

**Robustness:**
- Survives 5× costs (`agent_3`: 0.61 → 0.52).
- Null behaves correctly: random earns Sharpe **+0.02** at zero cost — properly
  zero, confirming the simulator manufactures nothing.
- Not a turnover artifact: at zero cost for both, `agent_3` 0.64 vs random p95 0.28.
- **No post-publication decay.** Basis-momentum was published 2019; the full
  signal went 0.59 (pre-2019) → 0.69 (post-2019) → 0.93 (last 3y).

### ⚠️ The finding that complicates the ladder

**Carry alone has decayed to negative** and is now a *drag*:

| | pre-2019 | post-2019 | last 3y |
|---|---|---|---|
| carry | +0.54 | **−0.30** | **−0.51** |
| full | +0.59 | +0.69 | +0.93 |

The ladder is therefore **no longer monotonic** — rung 1's *standalone* Sharpe
is negative, while momentum and basis-momentum carry the performance.
Consistent with the financialisation story (post-2005 commodity index investing
arbitraging the basis premium).

### RESOLVED — carry stays in (test run 2026-08-09)

A single pre-committed test (`scripts/carry_drop_test.py`) compared
`agent_3` against `agent_3_nocarry` (momentum + basis-momentum only) across
four conditions — full sample and post-2019, at 1× and 3× cost, all
vol-targeted. The rule, fixed before running: drop carry only if it improves
**all four**.

| Condition | with carry | no carry | Δ |
|-----------|-----------|----------|---|
| full sample, 1× cost | **0.698** | 0.442 | −0.256 |
| post-2019, 1× cost | **0.824** | 0.777 | −0.047 |
| full sample, 3× cost | **0.646** | 0.401 | −0.245 |
| post-2019, 3× cost | **0.784** | 0.743 | −0.041 |

**0/4 improved. Carry is retained.**

**Why standalone Sharpe was the wrong criterion.** Carry's own recent Sharpe is
negative, yet removing it *degrades* the blend — sharply over the full sample
(−0.25) and still negatively post-2019. The explanation is **diversification**:
carry is weakly/negatively correlated with momentum and basis-momentum, so it
reduces the blend's variance more than it reduces the blend's return. A
component's marginal contribution to portfolio risk-adjusted return — not its
standalone performance — is what determines whether it belongs.

This is also a worked example of *why* the pre-commitment protocol exists. The
era-by-era table made "carry is a drag, drop it" look obvious; the mechanical
test contradicted it. Had the variant been evaluated by hunting for a
configuration where dropping carry looked good, a real diversification benefit
would have been discarded.

---

## 10. Honest limitations

1. **No out-of-sample holdout.** Universe, 12-month momentum window, tercile
   cutoffs, monthly rebalance, 10% vol target and 63-day vol window were all
   chosen by judgement. Nothing was tuned iteratively — but that is not the
   same as validated.
2. **Post-2019 sample is short** (~7.5 yrs) and its null band is wide (sd 0.33).
   `agent_3`'s 0.69 beats p95 = 0.47, but not overwhelmingly.
3. **Datastream settlements are unverified** against an independent source.
4. **Flat cost model** — real costs vary by market and liquidity; oats and
   orange juice are not crude oil.
5. **Mild survivorship** — the universe is markets that still trade today.
6. **Execution not yet modelled.** These are single-contract positions across 23
   markets; the existing engine trades NG calendar spreads. Margin, contract
   sizing and rounding to whole contracts are unaddressed.

## 11. Why the natural-gas thesis was retired

Recorded so the pivot is auditable:

- **Week-1 regime gate failed.** The premise was that post-2016 LNG growth
  tightened deliverability and spreads hadn't repriced it. Post-2016 spreads
  became **29% less volatile** (p=0.033) with **unchanged** seasonal amplitude —
  the opposite of a tightening system.
- **No agent beat chance** on any of three instruments (prompt, seasonal,
  March/April) once roll artifacts and costs were corrected.
- Two measurement bugs (phantom roll P&L, discarded costs) had made earlier
  results — including an apparent "monotonic information ladder" — unreliable.

The infrastructure survived and is the asset: multi-commodity futures data,
roll-safe and cost-charged backtesting, calibrated null benchmarking, per-agent
live books, and 17 passing tests.

## 12. Next steps

1. **Carry-drop test** — run once, pre-commit to the result.
2. **Freeze the specification** and move to forward out-of-sample paper
   trading. Beyond this point, another week of real forward data is worth more
   than another in-sample variant.
3. **Wire into the live engine.** `execution/` (books, broker, scheduler,
   dashboard) transfers directly; only the signal layer and the
   spread→single-contract position mapping change.
4. **Retire the NG paper books** cleanly (still frozen since 2026-07-25 with a
   stale `agent_zero` position).

## 13. Code map

| File | Role |
|------|------|
| `src/storage_stress/data/commodities.py` | Universe, contract chain, settlements, panel, factors |
| `src/storage_stress/agents/xsec.py` | Agent ladder, tercile weights, portfolio simulator, vol targeting, stats |
| `scripts/run_xsec.py` | Runs the ladder + null distribution |
| `scripts/xsec_robustness.py` | Cost sensitivity, era stability, zero-cost null |
| `scripts/xsec_stage2.py` | Post-publication decay windows, vol targeting, hardest test |
| `data/processed/xsec_ladder.csv` | Ladder results |
| `data/processed/xsec_equity_curves.csv` | Daily equity per agent |
