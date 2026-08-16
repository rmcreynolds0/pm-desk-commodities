# LOCKED STRATEGY — Summer 2026


## Universe
- **Asset class:** US energy commodities.
- **Instrument:** NYMEX Henry Hub natural gas (`NG`) futures **calendar spreads**.
- **Spread definition:** front month minus a seasonal deferred month.
  - Injection season (Apr–Oct): front vs **next January**.
  - Withdrawal season (Nov–Mar): front vs **next April**.
- **Currency:** USD. **Exchange:** NYMEX (CME). **Delivery:** never taken — every
  position closed ≥5 business days before delivery notice.

## Signal (in deploy priority order)
1. **Agent One (ships first, the MVP):** 5-year rolling z-score of EIA salt-region
   weekly net storage flow. This is the locked, minimum-viable signal.
2. **Agent DSI (gated upgrade):** the residualized Deliverability Stress Index.
   Pipeline: utilization → convex stress transform `g(u)=sign(u)(e^{4|u|}-1)/(e^{4}-1)`
   → 3-week EWMA → subtract week-of-year seasonal → **residualize against Waha and
   Dom South basis (contemporaneous + 1-week lag, rolling 3y OLS)** → z-score.
   DSI is deployed in place of Agent One **only if** it beats Agent One by ≥0.4
   Sharpe with a 90% bootstrap CI on the paired difference that excludes zero,
   over the 5y walk-forward *and* the live window (decided end of week 10).

## Trade logic
- **Regime gate:** trade only when 13-week realized spread volatility is in the
  top third of its 3-year distribution.
- **Type A (injection, z > +entry):** sell front / buy next January (short spread).
- **Type B (withdrawal, z < −entry):** buy front / sell next April (long spread).
- **Entry threshold:** z = 2.0 for DSI; ±1.5 for Agent One. (Perturbation range
  for robustness: 1.5–2.5.)
- **Scale-in:** half size on entry, add the second half if the signal strengthens
  within 1–2 weeks.

## Position sizing
Inverse-volatility: `contracts = (capital × 0.5%) / (spread_daily_sd × point_value)`,
then scaled by a demand-regime weight in [0.5, 1.0] from heating/cooling degree
days, industrial production, and the coal-to-gas substitution price. Larger size in
quiet spreads, smaller in volatile ones.

## Holding period
1–6 weeks; **hard maximum 42 days**. Exit when (a) the signal reverts below 60% of
the entry threshold, (b) 42 days elapse, (c) the trade is down >2× the spread's
entry-day daily volatility, or (d) any portfolio risk limit triggers.

## Risk limits
- Book 1-day 99% VaR ≤ 2% of equity (from the held-spread covariance matrix).
- −6% drawdown → halve all positions, pause new entries 2 weeks.
- −10% drawdown → full liquidation, 4-week cooldown.
- ≤5 spreads open; no single tenor >40% of risk budget.
- Per trade: stop at 2× trailing 30-day daily range; 42-day time stop;
  participation ≤5% of trailing 90-day ADV.

## Costs (charged before judging)
1 tick slippage per leg on entry and exit (4 leg-charges per round trip), plus
exchange/clearing/commission. Stress-tested at 2 and 3 ticks/leg. A strategy that
only works pre-cost is rejected.

## Key assumptions
1. Post-2016 LNG export growth tightened the deliverability system; calendar
   spreads have **not fully repriced** this regime. (Week-1 statistical test; if it
   fails, the project pauses for rescoping.)
2. Salt-cavern utilization is a usable proxy for binding deliverability stress.
3. Public EIA/NOAA data carries signal **beyond** what regional basis already
   prices — the residualization isolates it (week-10 orthogonality test).
4. The trade is too specialized for the largest funds to arbitrage away, but large
   enough for a small live book.


## Validation
Three agents on shared execution code: **Zero** (random, null benchmark),
**One** (simple storage-flow z-score), **DSI** (full signal). Market benchmarks:
front-month `NG`, `UNG` ETF, equal-risk passive calendar spread. All performance
reported as bootstrapped Sharpe **confidence intervals**, not point estimates.

## Deploy / kill rules
- Ship Agent One by week 4. Promote DSI only if it passes the week-10 tests.
- Shut down if: rolling 24-month Sharpe < 0.5; drawdown > 12%; DSI fails to beat
  Agent One over 36 months; or any of the 3 core feeds lost >4 weeks without backup.
