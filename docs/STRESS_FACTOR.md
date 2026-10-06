# The Stress Factor — tested and rejected

**Date:** 2026-10-05 · **Verdict:** REJECTED · **Reproduce:** `python scripts/stress_test.py 200`

## The question

The natural-gas strategy was retired because its *hypothesis* was falsified —
post-2016 volatility fell 29% where the thesis needed it to rise. But its
**method** was never shown to be wrong. That method was a pipeline for turning
a physical-state variable into a tradeable signal:

> curve/inventory state → convex transform → smooth → deseasonalise →
> residualise against something else → z-score

This asks whether that method survives being pointed at a different question:
all 22 markets in the cross-section, instead of one gas spread.

## What was built

`data.commodities.add_stress` ports all five DSI steps, reusing
`signal.dsi.convex_stress` with the same pre-registered `k=4.0`:

| DSI step (gas) | Cross-sectional analogue |
|---|---|
| 1. Storage utilisation vs max cycling rate | Carry vs its own trailing 90th-percentile scale, clipped to ±1.2 |
| 2. Convex transform `g(u)` | Identical, same `k=4.0` |
| 3. 3-week EWMA | 15 trading days |
| 4. Subtract week-of-year mean | Identical, per market, expanding and shifted |
| 5. Residualise against regional basis | Residualise against **carry itself** |

Step 5 is the one that makes it a real factor rather than carry in disguise.
Steps 1–2 make stress a convex function of carry, so without residualisation it
would correlate ~0.9 with a factor already in the blend. It works:

```
cross-sectional corr(stress, carry)      −0.101
cross-sectional corr(stress, mom)        +0.031
cross-sectional corr(stress, basis_mom)  +0.001
```

Essentially orthogonal to everything. 96% panel coverage.

## The pre-committed rule

Written into `scripts/stress_test.py` **before the first run**, because this
project has already produced two results that were artifacts:

1. The blend containing stress beats the 95th percentile of a 200-seed null.
2. It adds **≥ 0.03 Sharpe** over the incumbent `carry+mom+basis_mom`.
3. That edge survives 3× costs.
4. `stress` alone is not the whole story.

## The result

| Agent | Signal | Ann. return | Sharpe | Max DD |
|---|---|---|---|---|
| `agent_1` | carry | +1.24% | 0.170 | −75.7% |
| `agent_2` | carry + mom | +6.85% | 0.401 | −47.4% |
| **`agent_3`** | **carry + mom + basis_mom** | **+10.57%** | **0.553** | **−39.0%** |
| — | stress alone | +0.17% | 0.109 | −55.4% |
| — | carry + mom + stress | +10.30% | 0.539 | −55.9% |
| — | carry + mom + basis_mom + stress | +7.93% | 0.447 | −50.7% |

Null distribution (200 seeds): mean −0.135, p95 **+0.169**.

```
1. beats chance (> p95 +0.169)        +0.447   PASS
2. adds >= 0.03 Sharpe over incumbent  −0.106   FAIL
3. edge survives 3x costs              −0.114   FAIL
4. stress alone is not the whole story +0.109   PASS

VERDICT: REJECT
```

## Why it failed — the honest mechanism

Stress is **not** uninformative. Added as the *third* factor it is nearly as
good as basis-momentum:

| Added to `carry+mom` (0.401) | Δ Sharpe |
|---|---|
| `+ basis_mom` | **+0.152** |
| `+ stress` | **+0.138** |

So the gas method does produce a real signal. It just produces a slightly
*worse* one than basis-momentum, and the two are substitutes rather than
complements — both read curve shape, by different routes.

As a *fourth* factor it then costs 0.106. Part of that is mechanical: the blend
is equal-weight by design, so a fourth factor cuts each existing weight from
⅓ to ¼. A fourth factor has to beat the **average** of the first three to help,
and stress does not.

**Note this is an explanation, not a rescue.** Re-weighting the blend to make
stress fit would be fitting the blend to the answer — exactly the in-sample
optimisation `blend: equal_weight_zscore` exists to prevent. The rule was
written first, it failed, and the verdict stands.

## What ships

The live book runs **`agent_3` — carry + momentum + basis-momentum**, Sharpe
0.553, 99.5th percentile of chance.

`stress` stays in the codebase, computed and available, because the variant
agents (`stress_only`, `carry_mom_stress`, `all_four`) are the evidence behind
this rejection. Deleting them would delete the proof.

## One discrepancy worth flagging

`agent_3` measures **0.553** here against the **0.61** recorded in
`SUMMER_SUMMARY.md`. The R2 mirror has since extended to 2026-06-30, so this
run covers more history than the earlier one. Not alarming — but the published
0.61 is stale, and the figure in that document should be refreshed from a
single current run rather than left to drift.
