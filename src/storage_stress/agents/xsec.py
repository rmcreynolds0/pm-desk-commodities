"""
xsec.py — the cross-sectional commodity agent ladder + portfolio simulator.
===========================================================================

Same experimental design as the NG agents, applied to a multi-commodity
long/short book. Each rung adds exactly ONE piece of information, so the gap
between adjacent rungs isolates that piece's contribution:

    agent_0  random signs                          null benchmark
    agent_1  carry                                 + term-structure slope
    agent_2  carry + momentum                      + trend
    agent_3  carry + momentum + basis-momentum     + curve dynamics (full)

CONSTRUCTION (standard cross-sectional factor practice):
  * rank commodities each rebalance date by the agent's score,
  * go LONG the top tercile and SHORT the bottom tercile, equally weighted
    within each side and dollar-neutral across them,
  * hold to the next rebalance, then repeat.

Dollar-neutral long/short is what strips out the common commodity beta, so
what remains is the factor itself rather than a bet on commodities going up.

COSTS ARE CHARGED ON TURNOVER. In the NG work, slippage was computed and then
silently overwritten, making the backtest cost-free and letting a 250-trade
coin flip look profitable. Here cost is applied explicitly as
    cost_t = turnover_t * cost_per_unit_turnover
and turnover is the sum of absolute weight changes.
TRAPS
-----
THE BLEND IS EQUAL-WEIGHT ON PURPOSE. No fitted factor weights. Optimising the
blend on the same history used to evaluate it is how in-sample fitting returns
after being designed out everywhere else. Note a side effect: adding a fourth
factor cuts each existing weight from 1/3 to 1/4, so a new factor must beat the
AVERAGE of the incumbents to help, not merely be informative.

Z-SCORING IS PER DATE, NOT PER TICKER. Grouping by date is what makes this
cross-sectional -- the question is "high relative to other markets today", not
"high relative to its own history".

agent_0 MUST SEE NO DATA. It draws random scores and runs through the identical
sizing, tercile and cost machinery, which is what makes it a fair null rather
than a decorative one.

MIN 6 MARKETS. Below that a tercile holds fewer than two names a side, which is
a bet rather than a factor.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

AGENT_FACTORS: dict[str, list[str]] = {
    "agent_0": [],
    "agent_1": ["carry"],
    "agent_2": ["carry", "mom"],
    "agent_3": ["carry", "mom", "basis_mom"],
    "agent_3_nocarry": ["mom", "basis_mom"],

    "stress_only":  ["stress"],
    "carry_mom_stress": ["carry", "mom", "stress"],
    "all_four":     ["carry", "mom", "basis_mom", "stress"],
}


def _zscore_xs(df: pd.DataFrame, col: str) -> pd.Series:
    """Cross-sectional z-score of `col` within each date.

    Standardising per date puts factors with different units (carry is an
    annualised log slope; momentum is a 12-month return) on a comparable scale
    so they can be averaged into one score.
    """
    g = df.groupby("date")[col]
    return (df[col] - g.transform("mean")) / g.transform("std")


def score(panel: pd.DataFrame, agent: str, seed: int = 0) -> pd.DataFrame:
    """Attach a `score` column for one agent. Higher score => want to be long.

    agent_0 draws a reproducible random score per (date, ticker): it is the
    null benchmark and must see no data at all.
    """
    if agent not in AGENT_FACTORS:
        raise ValueError(f"unknown agent {agent!r}; expected {list(AGENT_FACTORS)}")
    out = panel.copy()
    factors = AGENT_FACTORS[agent]

    if not factors:
        rng = np.random.default_rng(seed)
        out["score"] = rng.standard_normal(len(out))
        return out

    zs = [_zscore_xs(out, f) for f in factors]
    out["score"] = pd.concat(zs, axis=1).mean(axis=1)
    return out.dropna(subset=["score"])


def target_weights(scored: pd.DataFrame, quantile: float = 1 / 3) -> pd.DataFrame:
    """Dollar-neutral tercile portfolio: long top third, short bottom third.

    Requires at least 6 commodities on a date so each side holds >= 2 names;
    thinner cross-sections produce noise rather than a factor.
    """
    rows = []
    for date, g in scored.groupby("date"):
        g = g.dropna(subset=["score"])
        n = len(g)
        if n < 6:
            continue
        k = max(1, int(round(n * quantile)))
        ranked = g.sort_values("score")
        shorts, longs = ranked.head(k), ranked.tail(k)
        for t in longs["ticker"]:
            rows.append({"date": date, "ticker": t, "w": 1.0 / k})
        for t in shorts["ticker"]:
            rows.append({"date": date, "ticker": t, "w": -1.0 / k})
    return pd.DataFrame(rows)


def simulate_xsec(panel: pd.DataFrame, weights: pd.DataFrame,
                  rebalance: str = "ME",
                  cost_per_turnover: float = 0.0010,
                  vol_target: float | None = None,
                  vol_window: int = 63,
                  max_leverage: float = 3.0) -> pd.DataFrame:
    """Run a dollar-neutral cross-sectional book and return its daily equity.

    rebalance : pandas offset alias for rebalance dates ('ME' = month end).
        Weights are set on the rebalance date and HELD until the next one,
        which is standard for these factors and keeps turnover realistic.
    cost_per_turnover : cost charged per 1.0 of gross weight traded. The
        default 10 bps per unit turnover is a deliberately conservative
        round-trip estimate for liquid futures; the sensitivity of the result
        to this number is reported by the runner, because a factor that only
        works at zero cost is not a factor.

    Returns a daily frame with columns: ret (gross), turnover, cost, ret_net,
    equity.
    """
    ret = panel.pivot_table(index="date", columns="ticker", values="front_ret")

    rebal_dates = (pd.Series(ret.index, index=ret.index)
                   .resample(rebalance).last().dropna().values)
    rebal_dates = pd.DatetimeIndex(rebal_dates)

    w_wide = (weights.pivot_table(index="date", columns="ticker", values="w")
              .reindex(columns=ret.columns))
    w_held = w_wide.reindex(rebal_dates).reindex(ret.index).ffill()
    w_eff = w_held.shift(1)

    gross = (w_eff * ret).sum(axis=1, min_count=1).fillna(0.0)
    turnover = w_held.diff().abs().sum(axis=1).fillna(0.0)

    leverage = pd.Series(1.0, index=gross.index)
    if vol_target is not None:
        realised = gross.rolling(vol_window, min_periods=vol_window // 2).std() \
            * np.sqrt(252)
        lev = (vol_target / realised.shift(1)).replace([np.inf, -np.inf], np.nan)
        leverage = lev.clip(upper=max_leverage).fillna(1.0)

    gross_lev = gross * leverage
    turnover_lev = turnover * leverage
    cost = turnover_lev * cost_per_turnover
    net = gross_lev - cost

    return pd.DataFrame({
        "ret": gross_lev, "turnover": turnover_lev, "cost": cost,
        "leverage": leverage, "ret_net": net,
        "equity": (1 + net).cumprod(),
    })


def perf_stats(res: pd.DataFrame, periods_per_year: int = 252) -> dict:
    """Headline performance of a daily net-return series."""
    r = res["ret_net"].dropna()
    if len(r) < 2 or r.std() == 0:
        return {"ann_ret%": 0.0, "ann_vol%": 0.0, "sharpe": 0.0,
                "max_dd%": 0.0, "n_days": len(r)}
    eq = res["equity"]
    years = len(r) / periods_per_year
    return {
        "ann_ret%": ((eq.iloc[-1]) ** (1 / years) - 1) * 100,
        "ann_vol%": r.std() * np.sqrt(periods_per_year) * 100,
        "sharpe": r.mean() / r.std() * np.sqrt(periods_per_year),
        "max_dd%": (eq / eq.cummax() - 1).min() * 100,
        "n_days": len(r),
    }
