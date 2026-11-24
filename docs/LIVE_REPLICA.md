# Live-Replica Backtest: Changes Identified and Made

**Date:** 2026-11-19 · **Branch:** `experiment/momentum-signal-backtest` · **Base commit:** `2f61bbd` (uncommitted changes)

The research backtest (`scripts/run_xsec.py`) measures the factors. It does not trade the way
the live engine trades. This report lists every difference found between the two, what was
changed to build a backtest that follows the live rules (`scripts/run_xsec_replica.py`), and
what that backtest shows. Along the way, problems turned up in three places: the research
backtest (one), the live engine (seven, unverified on the VM) and the R2 data (two).

---

## 1. Summary

| agent_3 (the live rung) | Sharpe | Ann. return | Max DD |
|---|---|---|---|
| **Live rules (replica), $1M paper account** | **0.023** | **−0.54%** | **−57.1%** |
| Live rules, unlimited account (no margin calls, no size cap) | 0.051 | −0.22% | −57.2% |
| Research backtest as published, same dates | 0.494 | +9.01% | −39.0% |
| Research backtest with its exit bug fixed, same dates | 0.401 | +5.94% | −44.5% |

Window: 2000-01-31 to 2025-03-31, the last month in which all 22 markets have a usable
front and second contract in the R2 mirror.

- **Under the live rules, agent_3 earns roughly nothing over 25 years** and does not beat
  random agents run through the same rules. With the real $1M account it lands at the 98th
  percentile, but only because 169 of 200 random agents lose their whole account to roll
  costs and are liquidated, which drags the random baseline down to a mean Sharpe of −0.31.
  With an unlimited account it sits at the 84th–90th percentile: within noise.
- **Two changes cause almost all of the gap to the research result.** (1) The live engine
  computes momentum on a single contract's own price history instead of a chained
  front-month series; that alone roughly halves the Sharpe. (2) Rolling real contracts
  costs money the research backtest never charges: about 3% of the book a year at the
  configured 10bps.
- **The replica's machinery is not the cause.** Run with the research momentum definition
  and no costs, the replica scores 0.557, above the research backtest itself.
- **Carry alone holds up best under live rules** (agent_1, Sharpe 0.125). Adding momentum
  makes things worse, consistent with momentum being the factor the live definition changes.
- **The published 0.553 is overstated.** The research simulator never closes a market that
  leaves the top or bottom third. Fixed, the full-history figure is 0.489.

---

## 2. Changes identified, and what was made

These are the 16 changes proposed before building, plus the roll decision.

| # | Proposed change | Status | Notes |
|---|---|---|---|
| 1 | Momentum on the current front contract's own trailing-year prices (≥120 bars) | **Made** | Calls the live function `xsec_signals_ib._period_return` directly |
| 2 | Basis-momentum = momentum(front) − momentum(second), both as in #1 | **Made** | |
| 3 | Carry's time between contracts from last trade dates, not delivery months | **Made** | Same formula as live: `max(days, 1) / 365.25` |
| 4 | 25-day expiry guard for front/second selection, not 5 | **Made** | Imports `DEFAULT_GUARD_DAYS` from the live code |
| 5 | Drop markets missing a required factor before z-scoring | **Made** | Calls the live `xsec_signals_ib.usable` |
| 6 | Rebalance on the last weekday of each month | **Made** | Same rule as the scheduler; uses that day's settlement |
| 7 | Compute signals only on rebalance dates | **Made** | |
| 8 | Fixed $2M book, no compounding | **Made** | |
| 9 | Cap the book at account capacity (`equity × 0.5 / 0.24`), account starting at $1M | **Made** | Capped agent_3's book below $2M in 62 of its 298 rebalances |
| 10 | Whole contracts, IBKR multipliers from `xsec_universe_check.csv` | **Made** | Unit check passed: R2 and IBKR prices agree within 0.75–1.43× for all 22 markets, so no 100× unit mismatches |
| 11 | Skip a rebalance if >30% of wanted positions round to 0 contracts | **Made** | |
| 12 | Trade only the difference in contracts | **Made** | Except rolls, which close and reopen (see below) |
| 13 | Vol-target leverage set once a month from the agent's own equity marks | **Made** | Unit test confirms identical output to `xsec_engine.agent_leverage` |
| 14 | Hold contract counts fixed between rebalances, marked every weekday | **Made** | Holidays repeat the last price, as the live mark job would |
| 15 | Costs: 10bps of dollar notional actually traded; optional commissions | **Partly made** | Cost on traded notional, roll legs included. Commission option **not built**: the live ledger records none |
| 16 | agent_3 by default, option for all four | **Made** | `--agents` takes `live`, `all` or a comma list. This run used all four |
| — | Clean roll (your choice) | **Made** | At each rebalance, a position not in the current front is closed and the target opened in the new front, both legs costed |

### Changes added during the build (not in the original list)

| Change | Why |
|---|---|
| **Forced roll** within 5 days of a held contract's last trade date | The 25-day guard often picks a contract that expires before the next rebalance (§3, item L3). Counted per agent: 994 for agent_3 |
| **Margin call:** if the $1M account reaches zero, liquidate and stop | Otherwise the capacity formula goes negative and flips every position (§3, L5). agent_0 hit it on 2025-01-29, as did 169 of the 200 random agents; agents 1–3 never did |
| **One cross-section per rebalance:** every market stamped with the rebalance date | The live panel stamps markets with their own last-bar date, which can split the ranking (§3, L6) |
| **Staleness rule:** a market with no settlement in the last 10 days is skipped that month | The replica's version of live's "no bars" skip |
| **Window ends 2025-03-31** | After that the R2 contract chains thin out market by market (21 markets by Apr-2025, 5 by Feb-2026) |
| **Deterministic contract ordering and a bad-expiry check** | R2 metadata error (§5, D1) made results depend on DuckDB row order |
| **Random baseline redraws every month** | The live `agent_0` reuses the same random draw each month (§3, L4), which is not a fair baseline |
| **Holdings logged per rebalance** in `rebalance_log.csv` | So the roll logic can be audited |

---

## 3. Live-engine issues found (not changed)

These come from reading the code. None has been checked against the live ledger on the VM.

| | Issue | Where | Effect |
|---|---|---|---|
| **L1** | **No contract roll.** Positions are tracked per market, not per contract. When the front changes, the next rebalance sizes the difference against the old contract's count and trades it in the new one | `xsec_books.set_position`, `xsec_engine.run_rebalance` | IBKR can hold the old contract while the ledger records the new one; entry prices get overwritten |
| **L2** | **Marks price the current front, not the contract held** | `xsec_engine.price_and_contract` | After the front changes, unrealised P&L mixes two contracts' prices |
| **L3** | **The 25-day guard admits contracts that expire before the next rebalance** | `xsec_contracts.front` | In GC, SI, HG, NG, HO, RB (~96–99% of months), PA, PL (~74%), LE, GF (~50%), SB and CT occasionally. With no roll step, live holds these into their final days. For metals, first notice comes before the last trade date, so IBKR's delivery rules apply even earlier |
| **L4** | **agent_0 reuses the same random draw every month** (`seed=0`, same market order) | `xsec_engine.run_rebalance` → `xsec.score` | It holds a fixed random portfolio, not a random one. agent_0 is not live today |
| **L5** | **No guard for an exhausted account** | `xsec_engine.size_book` | If account equity falls below zero, capacity goes negative and every target flips sign |
| **L6** | **Markets may be ranked in separate groups** if their last IBKR bars carry different dates | `xsec_signals_ib.build_live_panel` sets `date` per market; `xsec.score` groups by date | Possible, not confirmed. Check the decisions table: each rebalance should show one long and one short third |
| **L7** | **Vol targeting measures its own levered equity.** `leverage = 10% / vol(levered returns)` feeds back on itself | `xsec_engine.agent_leverage` | The replica copies this rule; its realised volatility ran at 13–16% against the 10% target. A self-consistent form: `previous leverage × 10% / vol(levered returns)` |

---

## 4. Research-backtest issue found (not changed)

**Exits are never closed.** In `xsec.simulate_xsec`, a market that leaves the long or short
third has no weight on that rebalance date (NaN rather than 0), and `.ffill()` carries its
previous weight forward. Positions accumulate:

| agent_3 | Positions held | Gross exposure |
|---|---|---|
| As run | 21.6 on average | 3.13× |
| Intended | 13.7 | 1.97× |

| agent_3, 2000–2026 | Sharpe | Ann. return | Max DD |
|---|---|---|---|
| Published | 0.553 | +10.57% | −39.0% |
| Exits zero-filled | 0.489 | +7.87% | −44.5% |

`xsec_robustness.py`, `carry_drop_test.py`, `stress_test.py` and `xsec_stage2.py` all use the
same simulator, so the published cost-sensitivity, era-stability, carry-test and
vol-targeting figures inherit the bug. The fix is one line: zero-fill the weight table on
rebalance dates before forward-filling. It was not applied, because these figures are the
published record and changing them is a decision for the team.

---

## 5. Data issues found

- **D1. Bad expiry metadata.** CL, HO, NG and RB March-2007 contracts carry the February
  contract's last trade date (2007-01-22). Ties made contract order depend on DuckDB's row
  order, and identical expiries collapse carry's time between contracts to its 1-day floor.
  The replica breaks ties by delivery month and skips a market for that month if its two
  expiries don't increase.
- **D2. The mirror's chains thin out from April 2025.** Near each market's final date only
  one contract still prints, so carry cannot be computed. Hence the window end.

---

## 6. Results

### Strategies under live rules (2000-01-31 to 2025-03-31)

| Agent | Signal | Sharpe | Ann. return | Vol | Max DD | Costs (% of book) | Avg leverage | Forced rolls | Months book capped | Percentile vs random |
|---|---|---|---|---|---|---|---|---|---|---|
| agent_0 | random (live-style) | −0.179 | −2.65% | 11.4% | −68.0% | 56.3% | 0.96 | 1,414 | 161 | 88.5th · account ran out 2025-01-29 |
| agent_1 | carry | 0.125 | +0.77% | 10.8% | −60.3% | 108.7% | 1.02 | 1,491 | 0 | 100th |
| agent_2 | carry + momentum | 0.004 | −0.64% | 11.8% | −66.9% | 85.2% | 0.89 | 1,272 | 73 | 97.5th |
| agent_3 | carry + mom + basis-mom | 0.023 | −0.54% | 13.0% | −57.1% | 78.9% | 0.83 | 994 | 62 | 98.0th |

Random agents under the same rules (200, $1M account): Sharpe mean −0.310, 5th percentile
−0.526, 95th percentile −0.092. **169 of the 200 were margin-called.** That makes this
baseline too weak to judge skill against: an agent can clear it just by losing money more
slowly.

### The same run with an unlimited account

`--account-equity 1e12`: no margin calls and no size cap, for the agents and the random
baseline alike.

| Agent | Sharpe | Ann. return | Max DD | Percentile vs all 200 random | Percentile vs the 156 whose equity stayed positive |
|---|---|---|---|---|---|
| agent_0 | −0.130 | −2.80% | −73.5% | 61.0th | — |
| agent_1 | 0.125 | +0.77% | −60.3% | 90.0th | 94.9th |
| agent_2 | 0.008 | −0.75% | −71.2% | 81.5th | — |
| agent_3 | 0.051 | −0.22% | −57.2% | 84.0th | 90.4th |

Random baseline: mean −0.129, 95th percentile +0.194 across all 200. 44 random agents lost
more than the whole $2M book; their percentage-return Sharpe is not meaningful, so the last
column excludes them (95th percentile +0.111). **No agent clears the 95th percentile either
way.** Run folder: `data/processed/backtests/20261119_234519_replica/`.

### Where the gap comes from (agent_3, same dates)

| Variant | Sharpe | Ann. return | Vol | Max DD | Costs (% of book) |
|---|---|---|---|---|---|
| A. Full replica (live rules) | 0.023 | −0.54% | 13.0% | −57.1% | 78.9% |
| B. No costs | 0.254 | +2.27% | 11.4% | −40.8% | 0 |
| C. No vol targeting | −0.053 | −1.86% | 14.8% | −65.9% | 82.3% |
| D. No costs, no vol targeting | 0.237 | +2.27% | 13.0% | −46.1% | 0 |
| E. D + research (chained) momentum | **0.557** | +4.63% | 8.8% | −19.6% | 0 |
| F. Research momentum, costs and vol targeting on | 0.367 | +3.26% | 10.1% | −32.8% | 104.5% |
| Research backtest, as published | 0.494 | +9.01% | 22.6% | −39.0% | — |
| Research backtest, exits fixed | 0.401 | +5.94% | 18.8% | −44.5% | — |

How to read it:

- **Momentum definition, D → E:** 0.24 → 0.56. Momentum on a single contract's own year
  of prices is a weaker signal than momentum on the chained front-month series.
- **Costs, A → B:** 0.02 → 0.25. About 3% of the book per year, almost all from rolling.
  Energy and metals roll every month. Charging 10bps on each leg of a roll is conservative:
  rolling as a calendar spread usually costs far less, so B is the bound on what cheaper rolls could recover.
- **Vol targeting, C → A:** −0.05 → 0.02. A small effect.
- **Why % volatility looks low:** live sizes off a fixed $2M book, so once equity has grown,
  the same dollar swings are a smaller percentage of it. Measured on the $2M base, daily
  P&L volatility is 16–20%, in line with the corrected research backtest's 18.8%.

### Checks on the replica

- Gross exposure averages 2.01× the book at 1× leverage, as intended.
- One month of P&L (July 2015) matches an independent calculation (+5.23% against +5.19%;
  the gap is whole-contract rounding).
- Per-market daily volatility of the contracts held matches the research data.
- Shuffling the input rows gives identical results.

---

## 7. What still cannot match live

- **Prices.** Live uses IBKR regular-hours trade closes and fills near 10:30 ET. R2 has daily
  settlements only, so the replica computes signals and trades at the rebalance day's settlement.
- **Thin contracts.** IBKR returns no bar on days a contract did not trade; Datastream settles
  it every day. For thin deferred contracts, "252 bars" covers different stretches of time.
- **Order failures.** Rejections, partial fills and IBKR delivery-window refusals are not modelled.
- **Contract sizes.** Today's IBKR multipliers are applied to all of 2000–2025.

---

## 8. Files

| File | Change |
|---|---|
| `src/storage_stress/agents/xsec_replica.py` | **New.** The live-rules backtest |
| `scripts/run_xsec_replica.py` | **New.** Runner; writes `data/processed/backtests/<timestamp>_replica/` |
| `tests/test_xsec_replica.py` | **New.** 11 tests: guard, clean roll, whole-contract sizing, forced roll, skip rule, margin call, determinism, bad expiries, leverage parity with the live engine |
| `scripts/run_xsec.py` | Changed earlier this session: each run writes its own folder |
| `dashboard/pivot_a.py` | Changed earlier this session: sidebar picker for local runs |
| `docs/LIVE_REPLICA.md` | **New.** This report's source |

Not changed: `config/xsec.yaml`, the live engine (`execution/`), the research simulator
(`agents/xsec.py`), and the committed results in `data/processed/`.

**Run:** `.venv\Scripts\python scripts/run_xsec_replica.py --agents all --null 200`

**Run folders:** `data/processed/backtests/20261119_233645_replica/` (the $1M account) and
`20261119_234519_replica/` (unlimited account). Both appear in the research dashboard's
sidebar dropdown.
