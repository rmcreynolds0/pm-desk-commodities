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
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Which factor columns each rung is allowed to see. This IS the ladder.
AGENT_FACTORS: dict[str, list[str]] = {
    "agent_0": [],                                  # random — sees nothing
    "agent_1": ["carry"],
    "agent_2": ["carry", "mom"],
    "agent_3": ["carry", "mom", "basis_mom"],
    # --- single deliberate variant --------------------------------------
    # Carry decayed to NEGATIVE post-2017 (Sharpe -0.51 over the last 3
    # years) while remaining a third of agent_3's blended score. This drops
    # it. Tested ONCE against a pre-committed decision rule (see
    # scripts/carry_drop_test.py) -- iterating variants until the number
    # improves is precisely how in-sample overfitting is manufactured.
    "agent_3_nocarry": ["mom", "basis_mom"],

    # --- STRESS VARIANTS -------------------------------------------------
    # The natural-gas DSI method generalised to the cross-section (see
    # data.commodities.add_stress). These are RESEARCH VARIANTS evaluated
    # against a pre-committed decision rule in scripts/stress_test.py --
    # they are not rungs of the live ladder unless that test promotes one.
    "stress_only":  ["stress"],                           # is it a factor at all?
    "carry_mom_stress": ["carry", "mom", "stress"],       # the literal brief
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

    # Equal-weight blend of the standardised factors this rung can see.
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
        # Each side sums to 1.0 gross => book is +1 long / -1 short, net 0.
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
    # Forward returns by (date, ticker): what a position held INTO date t earns.
    ret = panel.pivot_table(index="date", columns="ticker", values="front_ret")

    # Rebalance calendar: the last available trading day of each period.
    rebal_dates = (pd.Series(ret.index, index=ret.index)
                   .resample(rebalance).last().dropna().values)
    rebal_dates = pd.DatetimeIndex(rebal_dates)

    w_wide = (weights.pivot_table(index="date", columns="ticker", values="w")
              .reindex(columns=ret.columns))
    # Keep only rebalance dates, then forward-fill to hold between them.
    w_held = w_wide.reindex(rebal_dates).reindex(ret.index).ffill()
    # Positions are set at the CLOSE of the rebalance date, so they earn from
    # the NEXT day onward: shift by one to avoid look-ahead.
    w_eff = w_held.shift(1)

    gross = (w_eff * ret).sum(axis=1, min_count=1).fillna(0.0)
    turnover = w_held.diff().abs().sum(axis=1).fillna(0.0)

    # --- optional VOLATILITY TARGETING ------------------------------------
    # The unscaled book runs at whatever vol the market gives it (~22% here),
    # which produced brutal drawdowns. Scaling exposure to a constant risk
    # budget is standard practice and usually improves risk-adjusted return by
    # cutting size in turbulent regimes.
    #
    # NO LOOK-AHEAD: leverage for day t uses volatility estimated from returns
    # up to t-1 only (rolling window, then .shift(1)).
    leverage = pd.Series(1.0, index=gross.index)
    if vol_target is not None:
        realised = gross.rolling(vol_window, min_periods=vol_window // 2).std() \
            * np.sqrt(252)
        lev = (vol_target / realised.shift(1)).replace([np.inf, -np.inf], np.nan)
        # Cap leverage: an unconstrained inverse-vol rule explodes when a
        # quiet window precedes a shock. Also fill the warm-up with 1.0.
        leverage = lev.clip(upper=max_leverage).fillna(1.0)

    gross_lev = gross * leverage
    # Turnover scales with position size, so costs scale with leverage too.
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
