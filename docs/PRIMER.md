# Signals Primer

### Futures contract behaviour and the three factors, from scratch

**QUANTT Commodities Desk**

---

## 1. What a futures contract does

A futures contract is an agreement to buy or sell a fixed quantity at a fixed
price on a fixed future date. One crude oil contract is 1,000 barrels; one corn
contract is 5,000 bushels.

Three behaviours drive everything that follows.

**They expire.** Hold one to expiry and you must make or take physical
delivery. No systematic strategy wants 1,000 barrels of crude arriving in
Oklahoma, so positions are closed or **rolled** into a later contract before
delivery approaches.

**They converge.** As a contract nears expiry its price converges to the spot
price. It has to — at expiry the contract *is* the physical market.

**They trade in parallel.** A commodity has many contracts live at once, one
per delivery month. Plotting price against delivery date gives the **curve**.

---

## 2. The curve has two shapes

### Backwardation — front above deferred

```
price
  ^  ● Jul  311.20   ← front
  |     ● Sep  296.45
  |        ● Nov
  +--------------------> delivery
```

Buyers pay a premium for delivery **now**.

**Why:** inventories are scarce. Physical supply on hand has real value — a
roaster that runs out stops production. That value, the **convenience yield**,
exceeds the cost of storing and financing the commodity.

### Contango — front below deferred

```
price
  ^           ● Dec  5078.00
  |        ● Sep
  |     ● Jul  5002.00   ← front
  +--------------------> delivery
```

Nobody pays extra for immediate delivery.

**Why:** inventories are ample, so convenience yield is near zero. The gap is
just **storage, insurance and financing** — the cost of carry. Deferred must
cost more, or you could buy spot, store it, sell forward, and pocket the
difference risk-free.

### The relationship

$$F_{t,T} \approx S_t \cdot e^{(r + c - y)(T-t)}$$

$S$ spot, $r$ interest, $c$ storage, $y$ convenience yield. When $y$ is large
(scarcity), the exponent goes negative and deferred contracts cost **less** —
backwardation. When $y \approx 0$ (glut), storage and financing dominate —
contango.

**The curve's shape is a market-generated signal about physical scarcity.**

---

## 3. Roll yield: shape becomes return

This is the step most explanations skip, and it is why curve shape is
tradeable.

Suppose a market is in **contango** — front at \$90, next month at \$93 — and
spot never moves at all.

As the front contract approaches expiry it converges to spot, near \$90. To
stay in the position you sell it at \$90 and buy the next month at \$93. You
have paid \$3 to maintain the same exposure.

That is **negative roll yield**: a structural cost paid at every roll, in a
market where the price did nothing.

In **backwardation** it reverses. You hold a deferred contract priced below
spot and watch it converge *upward*. **Positive roll yield** — a return earned
from the curve's shape, not from any price forecast.

> A long position in a backwardated market earns a return even if spot never
> moves. This is not free money — it is compensation for bearing the risk that
> a tight market loosens — but it is systematic and measurable.

---

## 4. The three signals

All three read the same two prices: front contract $F_1$ and second $F_2$.
Three different questions.

Real values from the panel, 2026-06-30:

| Market | Front | Second | Gap | carry | mom | mom(F₂) | basis_mom |
|---|---:|---:|---:|---:|---:|---:|---:|
| Coffee `KC` | 311.20 | 296.45 | 62d | **+0.286** | +0.215 | +0.137 | **+0.078** |
| Orange Juice `OJ` | 172.75 | 165.25 | 62d | **+0.261** | −0.406 | −0.282 | **−0.124** |
| Cocoa `CC` | 5002.00 | 5078.00 | 62d | **−0.089** | −0.451 | −0.395 | **−0.057** |
| Sugar `SB` | 14.82 | 15.76 | 151d | **−0.149** | −0.162 | −0.148 | **−0.014** |
| Cotton `CT` | 72.22 | 74.99 | 92d | **−0.149** | −0.078 | −0.022 | **−0.055** |

### Carry — where the curve is *now*

$$\text{carry} = \frac{\ln(F_1 / F_2)}{\Delta t}$$

Coffee: $\ln(311.20 / 296.45) \div (62/365.25) = +0.286$

Positive means backwardation, implying tightness and positive expected roll
return. Negative means contango and a structural drag.

**Why divide by $\Delta t$:** sugar's two contracts are 151 days apart,
coffee's are 62. Without annualising you would be ranking markets partly by
their listing calendar rather than by their curve.

**The most replicated premium in commodity futures**, documented continuously
since the 1970s.

### Momentum — where the price *has been going*

Trailing 12-month compounded return of the front contract.

Coffee **+0.215** (up 21.5% over the year). Cocoa **−0.451**.

No theory of storage here — the premise is empirical. Assets that have risen
tend to keep rising over intermediate horizons. Documented across equities,
bonds, currencies and commodities. Proposed causes include slow information
diffusion and gradual producer hedging.

Momentum says nothing about the curve. That independence is why it adds to
carry rather than duplicating it.

### Basis-momentum — how the curve's *shape* has been changing

$$\text{basis-mom} = \text{mom}(F_1) - \text{mom}(F_2)$$

Coffee: $0.215 - 0.137 = +0.078$. The front rose faster than the deferred —
the curve **steepened into backwardation**. The market has been tightening.

Orange juice: $-0.406 - (-0.282) = -0.124$. Both legs fell, but the front fell
harder — the curve **flattened out of backwardation**. Loosening.

Carry is the curve's *level*. Basis-momentum is its *rate of change*.

### Why all three

Orange juice makes the case. **Carry +0.261** says the market is tight today.
**Basis-momentum −0.124** says that tightness has been draining away. Carry
alone would say buy; basis-momentum contradicts it.

Coffee agrees on all three — tight, rising, and tightening further. High
combined score.

| Signal | Reads |
|---|---|
| carry | curve **level** |
| mom | price **direction** |
| basis_mom | curve **rate of change** |

---

## 5. From signals to a position

**Standardise.** Carry is an annualised log slope (~0.05); momentum is a
12-month return (~0.30). Averaging them raw would let momentum dominate on
magnitude alone. Each is z-scored **across markets within each date**:

$$z_{i,t} = \frac{x_{i,t} - \mu_t}{\sigma_t}$$

Grouping by **date** is what makes this cross-sectional. The question becomes
"is coffee's carry high compared to the other 21 markets *today*" — not
"compared to its own history".

**Combine.** Equal-weighted mean of the z-scores. Weights are not fitted:
optimising them on the same history used to evaluate the strategy is
in-sample fitting.

**Rank and hold.** Sort by score. Top third long, bottom third short, equally
weighted within each side. Gross exposure 2.0, net 0.

**Why dollar-neutral:** commodities move together. A dollar move or a global
growth shock lifts or sinks nearly everything. Holding longs against shorts
cancels most of that, leaving the *ranking* as the thing being measured.

On 2026-06-30 this book would hold coffee and orange juice long, cocoa, sugar
and cotton short.

---

## 6. Further reading

### Start here

**Gorton, G. and Rouwenhorst, K. (2006).** *Facts and Fantasies about
Commodity Futures.* Financial Analysts Journal 62(2).
The standard entry point. Establishes that commodity futures returns come
largely from roll yield and collateral, not spot appreciation.

**Erb, C. and Harvey, C. (2006).** *The Strategic and Tactical Value of
Commodity Futures.* Financial Analysts Journal 62(2).
Published alongside Gorton–Rouwenhorst and sometimes disagrees with it.
Reading both is more useful than reading either.

### The theory of storage

**Kaldor, N. (1939).** *Speculation and Economic Stability.* Review of
Economic Studies 7(1).
Where convenience yield comes from.

**Working, H. (1949).** *The Theory of Price of Storage.* American Economic
Review 39(6).
The formal treatment of why the curve has the shape it does.

### The factors

**Boons, M. and Prado, M. (2019).** *Basis-Momentum.* Journal of Finance
74(1).
The paper behind the third signal. Shows basis-momentum is distinct from both
carry and momentum, and empirically stronger.

**Koijen, R., Moskowitz, T., Pedersen, L. and Vrugt, E. (2018).** *Carry.*
Journal of Financial Economics 127(2).
Carry as a single concept applied across every asset class, not just
commodities.

**Asness, C., Moskowitz, T. and Pedersen, L. (2013).** *Value and Momentum
Everywhere.* Journal of Finance 68(3).
Momentum across asset classes. Useful for understanding why it is treated as
a factor rather than a trading rule.

### Market mechanics

**CME Group Education** — [cmegroup.com/education](https://www.cmegroup.com/education.html)
Contract specifications, delivery procedures, roll mechanics. Free, and the
authoritative source for the contracts this book trades.

**Hull, J.** *Options, Futures, and Other Derivatives.* Chapters 2 and 5.
The standard textbook treatment of futures mechanics and the cost-of-carry
relationship.

### Methodology

**Harvey, C., Liu, Y. and Zhu, H. (2016).** *…and the Cross-Section of
Expected Returns.* Review of Financial Studies 29(1).
On how many published factors fail to replicate, and what significance
threshold a new one should clear. Directly relevant to why this project tests
everything against a random null.

---

*Paper trading. No capital is at risk. Not investment advice.*
