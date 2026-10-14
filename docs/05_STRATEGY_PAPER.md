# A Cross-Sectional Commodity Futures Strategy

### From first principles: the market mechanics, the signal, and the implementation

**QUANTT Commodities Desk · October 2026**

---

## Abstract

This paper describes a systematic long/short commodity futures strategy trading
22 markets on IBKR paper. It ranks every commodity against every other on three
signals derived from the futures curve — carry, momentum and basis-momentum —
and holds the top third long against the bottom third short, dollar-neutral,
rebalanced monthly. Over 2000–2026 the full signal earns a Sharpe ratio of
0.553 at the 99.5th percentile of a 200-seed random null distribution.

The paper assumes no prior knowledge of futures markets. It builds the
mechanics from first principles, derives why each signal should carry
information, describes the algorithmic implementation in full, and documents
what was tested and **rejected** — including a signal ported from the project's
own retired strategy.

---

# Part I · Market mechanics from first principles

## 1. What a futures contract is

A futures contract is a binding agreement to buy or sell a fixed quantity of a
commodity at a fixed price on a fixed future date. One NYMEX crude oil contract
is 1,000 barrels; one CBOT corn contract is 5,000 bushels.

Three properties matter for everything that follows.

**They expire.** A contract has a delivery month. Hold it to expiry and you are
obliged to make or take physical delivery — 1,000 barrels of crude arriving in
Cushing, Oklahoma. No systematic strategy wants that, so positions must be
closed or rolled before delivery approaches.

**They are standardised and exchange-traded.** Every December-2026 crude
contract is identical and fungible. This is what makes a liquid market and
makes quantitative comparison across markets possible at all.

**They are leveraged.** You post margin — typically 5–12% of the contract's
notional value — rather than paying the full amount. A $93,000 crude position
might require $7,000 of margin. This magnifies both returns and risk, and it
is why position sizing is a first-order concern rather than an afterthought.

## 2. The term structure, or "the curve"

At any moment, a commodity trades at many prices simultaneously — one for each
delivery month. Crude oil has contracts for December 2026, January 2027,
February 2027, and so on. Plotting price against delivery date gives the
**term structure**, or curve.

The curve takes one of two shapes, and the distinction drives the strategy's
primary signal.

**Contango** — deferred contracts cost more than near ones:

```
price
  ^            ○ Mar
  |        ○ Feb
  |    ○ Jan
  |  ○ Dec  (front)
  +---------------------> delivery month
```

**Backwardation** — deferred contracts cost less:

```
price
  ^  ○ Dec  (front)
  |    ○ Jan
  |        ○ Feb
  |            ○ Mar
  +---------------------> delivery month
```

## 3. Why the curve has a shape: the theory of storage

The shape is not arbitrary. It reflects the real economics of holding the
physical commodity.

Consider someone deciding whether to buy crude today and store it for three
months, versus simply buying the three-month contract. Holding physical oil
costs money: storage, insurance, and the financing cost of capital tied up in a
barrel sitting in a tank. Call this the **cost of carry**.

Holding physical oil also confers a benefit. If a refinery runs short, having
barrels on hand is worth something a paper contract cannot provide. Economists
call this the **convenience yield** — the value of immediate availability.

These two forces determine the curve:

$$F_{t,T} \approx S_t \cdot e^{(r + c - y)(T-t)}$$

where $S_t$ is the spot price, $r$ the interest rate, $c$ storage costs, and
$y$ the convenience yield.

**When inventories are plentiful**, convenience yield is near zero — nobody
pays a premium for immediate access to something abundant. Storage and
financing dominate, so deferred contracts cost more: **contango**.

**When inventories are scarce**, convenience yield is high. Having the physical
commodity now is genuinely valuable, which can exceed storage and financing
costs entirely, pushing near contracts above deferred: **backwardation**.

So the curve's shape is a market-generated signal about **physical scarcity**.
That is the economic foundation of the strategy's first factor.

## 4. Roll yield: where curve shape becomes return

A futures position held over time does not simply earn the spot price change,
because the contract expires and must be rolled into the next one.

Suppose crude's December contract trades at $90 and January at $93 (contango),
and spot prices do not move at all. As December approaches expiry, its price
converges to spot — contracts must converge to the cash market, because at
expiry they *are* the cash market. You sell December near $90 and buy January
at $93. You have paid $3 for the privilege of maintaining the position.

That is **negative roll yield**, and it is a structural headwind in contangoed
markets.

In backwardation the arithmetic reverses. Buying the cheaper deferred contract
and watching it converge upward to spot produces **positive roll yield** — a
return earned from the curve's shape rather than from any price forecast.

This is the key insight: **a long position in a backwardated market earns a
return even if the spot price never moves.** That return is not a free lunch;
it is compensation for bearing the risk that a tight market loosens. But it is
systematic and measurable, which makes it tradeable.

---

# Part II · The signals

## 5. Carry

Carry measures the curve's slope, annualised:

$$\text{carry} = \frac{\ln(F_1 / F_2)}{\Delta t}$$

where $F_1$ is the front contract, $F_2$ the next delivery, and $\Delta t$ the
time between them in years.

Positive carry means backwardation, which implies physical tightness and a
positive expected roll return. Negative carry means contango and a structural
drag.

**Annualisation is essential, not cosmetic.** Markets have different listing
cycles: crude lists monthly, some agricultural contracts quarterly. A one-month
spread and a three-month spread describe different things until normalised to a
common time basis. Without dividing by $\Delta t$, the factor would rank
markets partly by their listing calendar — a measurement artifact, not a signal.

Carry is the most replicated premium in commodity futures. Sorting markets on
basis has been documented continuously since the 1970s.

## 6. Momentum

Momentum is the trailing 12-month return of the front contract.

The premise is empirical rather than theoretical: assets that have risen tend
to continue rising over intermediate horizons. Proposed explanations include
slow diffusion of information, under-reaction to news, and the behaviour of
producers hedging gradually rather than instantly. The effect has been
documented across equities, currencies, bonds and commodities.

Momentum is **orthogonal in construction** to carry — one describes where the
curve *is*, the other where the price *has been going* — which is why combining
them improves on either alone.

## 7. Basis-momentum

Basis-momentum, introduced by Boons and Prado (2019), is the difference between
the momentum of the front contract and the momentum of the second:

$$\text{basis-mom} = \text{mom}(F_1) - \text{mom}(F_2)$$

Carry measures the curve's slope **now**. Momentum measures where the price
level **has been going**. Basis-momentum measures how the curve's **shape** has
been changing.

If the front has risen faster than the deferred, the curve has been steepening
toward backwardation — the market has been tightening, independent of its
current slope or its absolute price direction. That is genuinely different
information, and empirically it is the strongest of the three.

This is also the least widely traded of the three factors, which may partly
explain why it has not decayed.

## 8. Why rank cross-sectionally rather than forecast direction

The strategy never predicts whether crude oil will rise. It asks only whether
crude looks more attractive **than copper, than corn, than coffee** on these
three measures, then holds the top third long and the bottom third short.

This matters for three reasons.

**It removes a hard problem.** Forecasting the direction of a commodity
requires predicting supply shocks, weather, geopolitics and macro demand.
Ranking requires only that the relative ordering carries information — a much
weaker claim, and one supported by decades of literature.

**It hedges the common factor.** Commodities move together. A dollar
devaluation or a global growth shock lifts or sinks nearly everything. A
dollar-neutral long/short book has those common moves largely cancel between
the legs, isolating the ranking itself.

**It is testable.** A directional strategy's performance depends heavily on
which decade you examine. A cross-sectional ranking can be compared against a
random ranking over the same period — the null benchmark described in §12.

---

# Part III · Portfolio construction

## 9. Combining the factors

Each factor has different units. Carry is an annualised log slope with typical
magnitude around 0.05. Momentum is a 12-month return, around 0.30. Averaging
them raw would let momentum dominate purely because its numbers are larger.

Each factor is therefore **z-scored across markets within each date**:

$$z_{i,t} = \frac{x_{i,t} - \mu_t}{\sigma_t}$$

where $\mu_t$ and $\sigma_t$ are the cross-sectional mean and standard
deviation on date $t$.

Grouping by **date**, not by market, is what makes this cross-sectional. The
question becomes "is copper's carry high compared to the other 21 markets
today?" rather than "is copper's carry high compared to its own history?"

The combined score is the equal-weighted mean of the available z-scores:

$$\text{score}_{i,t} = \frac{1}{|F|}\sum_{f \in F} z^f_{i,t}$$

**The weights are equal by design, not by omission.** Fitting factor weights on
the same history used to evaluate the strategy is in-sample optimisation — it
produces a backtest that flatters itself and a live book that disappoints. The
equal-weight constraint removes that degree of freedom entirely.

This has a consequence worth stating explicitly: adding a fourth factor reduces
every existing weight from ⅓ to ¼. **A new factor must beat the average of the
incumbents to help**, not merely contain information. §15 returns to this.

## 10. From scores to positions

Markets are sorted by score. The top third are held long, the bottom third
short, each side equally weighted:

$$w_i = \begin{cases} +1/k & \text{if } i \in \text{top tercile} \\ -1/k & \text{if } i \in \text{bottom tercile} \\ 0 & \text{otherwise} \end{cases}$$

where $k$ is the tercile size. Gross exposure is 2.0, net exposure 0.

A minimum of six markets is required, so each side holds at least two names. A
tercile of one is a bet on a single market, not a factor.

## 11. Volatility targeting

Position sizes scale inversely with the book's own recent realised volatility:

$$\text{leverage}_t = \min\!\left(\frac{\sigma_{\text{target}}}{\sigma_{t-1}},\; L_{\max}\right)$$

targeting 10% annualised volatility, capped at 3×. The trailing estimate uses
63 days and is lagged one day so today's leverage never depends on today's
return — a subtle but complete source of lookahead bias if omitted.

The cap matters: inverse-volatility rules explode after unusually quiet
periods, taking the largest position precisely when the market is most likely
to be mispricing risk.

---

# Part IV · Methodology

## 12. The null benchmark

**A strategy's result is meaningless without knowing what chance produces.**

This project learned that expensively. An early natural-gas result returned
+26.3% and looked like a signal. Running the same machinery with 200 random
agents showed that the distribution of pure chance on that instrument spanned
−41.7% to +24.0%, with **49% of random seeds profitable**. The +26.3% sat at
the 95th percentile — remarkable-looking, but inside the range chance produces.

A single random path is an anecdote. A distribution is a benchmark.

Every result in this paper is therefore reported against a 200-seed null. The
random agent runs through **identical machinery** — same universe, same tercile
construction, same position sizing, same transaction costs. Only the scores are
random. This makes it a fair comparison: any gap above it is attributable to
information, not to mechanics.

## 13. The agent ladder

Rather than building one model and asking whether it is good, the strategy is
built as a **ladder of agents**, each seeing exactly one more piece of
information than the one below:

| Agent | Factors visible |
|---|---|
| `agent_0` | none — random scores |
| `agent_1` | carry |
| `agent_2` | carry + momentum |
| `agent_3` | carry + momentum + basis-momentum |

All four are otherwise identical. The **gap between adjacent rungs isolates the
contribution of the single factor that separates them**, and the gap above
`agent_0` measures whether the strategy contains any information at all.

This design makes negative results legible. If adding a factor fails to improve
the rung above, that is a measurement, not a disappointment.

---

# Part V · Results

## 14. The ladder, 2000–2026

22 markets across energy, metals, grains, softs and livestock; 6,676 trading
days.

| Agent | Signal | Ann. return | Sharpe | Max DD | vs chance |
|---|---|---|---|---|---|
| `agent_0` | random (null) | −0.26% | 0.08 | −66% | — |
| `agent_1` | carry | +2.91% | 0.24 | −75% | 98th pctile |
| `agent_2` | + momentum | +5.56% | 0.36 | −64% | 100th |
| **`agent_3`** | **+ basis-momentum** | **+11.54%** | **0.553** | **−36%** | **99.5th** |

The ladder is **monotonic** — each rung improves on the one below — and every
rung carrying real information beats the null band.

## 15. Robustness

| Test | Result |
|---|---|
| Null sanity check | Random earns Sharpe **+0.02 at zero cost** — properly zero, confirming the simulator manufactures nothing |
| 5× transaction costs | 0.61 → **0.52**. Not cost-fragile |
| Volatility targeting (10%) | 0.61 → **0.70**; max drawdown −36% → **−19%** |
| Post-publication decay | 0.59 pre-2019 → 0.69 post-2019 → **0.93** last 3 years. No decay |
| Hardest combined test | Post-2019 **and** 3× costs **and** vol-targeted: **Sharpe 0.78** |

The null sanity check is the most important line. A random agent in this
simulator earns +0.02 at zero cost — indistinguishable from nothing, which is
exactly correct. When an earlier version of the backtest was broken, random
agents were *profitable*. That number is now the first thing checked after any
change to the simulation.

## 16. What was tested and rejected

Three candidate improvements were evaluated against decision rules written
**before** each test was run. All three were rejected, and all three are
documented in the repository.

**Narrowing the universe to 17 markets.** Margin constraints made dropping the
five most capital-hungry markets tempting. Re-running the identical ladder
showed carry stopped beating chance entirely (Sharpe 0.24 → 0.12, annual return
+2.91% → +0.05%) and drawdown worsened from −36% to −53%. Rejected: the account
is sized to the universe, not the universe to the account.

**Dropping carry.** Carry's standalone Sharpe had decayed to −0.51 over three
years, which looked like a clear case for removal. Tested across four
configurations, **zero improved**. Standalone performance was the wrong
criterion — carry earns its place through its contribution to the blend.

**A stress factor ported from the retired strategy.** The project's first
strategy was a natural-gas model built on a Deliverability Stress Index. The
hypothesis was falsified, but the *method* — convex transform, smoothing,
deseasonalisation, residualisation — was never shown to be wrong. Ported to the
cross-section and residualised against carry, it produced a factor essentially
orthogonal to all three incumbents (cross-sectional correlations −0.10, +0.03,
+0.00).

It was still rejected. As a *third* factor it added +0.138 Sharpe, against
basis-momentum's +0.152 — real information, slightly worse. As a *fourth* it
cost 0.106, because under equal weighting a new factor must beat the average of
the incumbents. Re-weighting the blend to accommodate it would have been
precisely the in-sample fitting the equal-weight rule exists to prevent.

---

# Part VI · Algorithmic implementation

## 17. Data and the roll-safe return

A daily return is computed **only between two prices of the same contract**.

This sounds trivial and is the single most dangerous implementation detail in
futures research. The obvious approach — splice contracts into one continuous
series and difference it — books the price gap at every roll as profit or loss.
Measured on this project's natural-gas spreads, those artificial jumps were
**8.1×, 6.6× and 14.4× a normal daily move**, accounting for **31%, 27% and 6%
of all measured price movement**.

A strategy backtested on spliced returns is measuring its own data handling.

The implementation builds returns per contract, then reads off whichever
contract is front or second on each date:

```python
px["ret"] = px.groupby("futcode")["settlement"].pct_change()
panel["front_ret"]  = ret.reindex(idx_front).values
panel["second_ret"] = ret.reindex(idx_second).values
```

The front contract is defined as the nearest whose expiry is more than a guard
period away — never holding into the delivery window.

## 18. Position sizing and whole-lot rounding

The backtest works in weights; the market trades whole contracts of wildly
different size. Gold is roughly $443,000 per contract; oats roughly $16,000 —
a **27× span**.

Target contracts are:

$$n_i = \text{round}\!\left(\frac{w_i \cdot B \cdot \ell}{\text{notional}_i}\right)$$

where $B$ is book size and $\ell$ leverage. Three things must be right:

**Price magnifier.** Grains, softs and cattle quote in **cents**. Ignoring this
overstates contract value 100× — in testing, coffee once appeared as $12.8M per
contract instead of $128k. The multiplier and magnifier are read from the
broker at runtime, never assumed.

**Whole-lot rounding.** A market whose target is 0.4 contracts rounds to zero
and silently disappears from the book. The engine **records every such
omission** with its reason rather than hiding it, because the rate of
rounding-to-zero is a direct measure of whether the account is large enough to
run the strategy as specified.

**Signed positions.** Shorts are negative throughout, so diffing current
against target handles a long-to-short reversal in a single step.

## 19. The capital adequacy rail

Book size determines which markets can hold even one contract. Measured against
real contract notionals:

| Book per agent | Markets tradeable | Avg weight error |
|---|---|---|
| $250k | 11 of 22 | 31% |
| $2.5M | 22 of 22 | 8.8% |
| $10M | 22 of 22 | 1.9% |

If more than **30%** of intended positions round to zero, the engine aborts the
entire rebalance **before sending any order**. A book that can hold only half
its universe is a different, smaller strategy than the one that was tested, and
trading it would produce a forward record that cannot be compared to the
research.

The check runs in a strict plan → judge → execute sequence. An earlier version
ran the feasibility test *after* the trading loop, so an agent printed
"ABORTED" having already traded — the exact silent divergence the rail exists
to prevent.

## 20. Execution and the ledger

Orders are market orders tagged with an `orderRef` identifying the agent,
market and date. Agents share a broker account, so the tag plus a local ledger
is the attribution mechanism — the account's blended value cannot separate
agents that hold opposite positions in the same market.

**The ledger records only what actually filled.** Orders are rejected for real
reasons: insufficient margin, near-expiry delivery policy, per-order size caps.
An earlier version wrote intended positions regardless, leaving the books
claiming 54 holdings the broker did not have — and every subsequent mark
computed from them was fiction.

Positions therefore become `held + signed_filled`, never the target.

**Every decision is recorded, traded or not**, with the score and rank that
produced it and the reason if it was not acted on. That record, not the equity
curve, is the primary research output.

## 21. Independent reconciliation

The ledger is written by the same code that sends the orders. If that code is
wrong about a fill, the ledger is wrong in the way hardest to detect:
everything downstream remains internally consistent and is simply false.

The broker's own reporting interface provides an independent record of the same
events. A daily reconciliation compares the two and reports any divergence by
market. This is the only external check available, and it exists because the
failure it guards against has already occurred once.

---

# Part VII · The forward test

## 22. What is being tested, and the pre-committed question

Every parameter is frozen in a specification file. Freezing is what makes
forward paper trading a genuine out-of-sample test: if parameters can drift
while the strategy runs, the "out-of-sample" result is in-sample fitting with
extra steps.

The forward test asks one question, committed in advance:

> **Do the rungs stay in order?**

If `agent_3` continues to beat `agent_2`, which beats `agent_1`, which beats
random, the ladder held out of sample. If the ordering scrambles, the backtest
was fitted — and that will be reported, in the same terms the first strategy's
retirement was reported.

## 23. Current deployment

The book trades on IBKR paper from a continuously-running cloud host: a
headless broker gateway, a scheduler firing a monthly rebalance and a daily
mark, a local ledger, and a nightly verified backup. Signals are computed from
the broker's own market data, so a position is traded and marked against the
same prices that generated it.

One agent currently trades live. Each agent requires its own broker account,
because the paper-account balance cap makes a shared account too small to hold
the full universe — and the measured consequence of a universe too small is
documented in §16.

---

## 24. Conclusion

The strategy rests on three claims, each independently supported.

**The curve carries information about physical scarcity.** This follows from
the theory of storage and is the most replicated premium in commodity futures.

**Relative ranking is a weaker and more testable claim than directional
forecasting.** Dollar-neutral construction hedges the common factor and leaves
the ranking itself as the thing being measured.

**The ladder makes every claim attributable.** Each rung's contribution is the
gap to the rung below, and the whole structure is measured against what chance
produces rather than against zero.

What distinguishes this work is not the Sharpe ratio. It is that the project
has retired one strategy on its own evidence, rejected three proposed
improvements against rules written before testing, and documented every one.
The forward test will be reported the same way.

---

### References

Boons, M. and Prado, M. (2019). *Basis-Momentum.* Journal of Finance, 74(1).

Erb, C. and Harvey, C. (2006). *The Strategic and Tactical Value of Commodity
Futures.* Financial Analysts Journal, 62(2).

Gorton, G. and Rouwenhorst, K. (2006). *Facts and Fantasies about Commodity
Futures.* Financial Analysts Journal, 62(2).

Kaldor, N. (1939). *Speculation and Economic Stability.* Review of Economic
Studies, 7(1).

Working, H. (1949). *The Theory of Price of Storage.* American Economic Review,
39(6).

---

*Paper trading throughout. Backtest figures are simulated; forward figures come
from the live broker ledger. No capital is at risk. This document is a
technical description of a student research project and is neither investment
advice nor a solicitation.*
