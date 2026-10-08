"""
signal.py
=========
The Deliverability Stress Index (DSI) pipeline, faithful to sections 4-6 of the
proposal, plus the z-score / regime gate that turns DSI_clean into a trade
decision.

Pipeline (proposal section 4):
  step 1  utilization = salt net flow / max injection-or-withdrawal rate
  step 2  convex transform  g(u) grows faster as |u| -> 1
  step 3  3-week EWMA smoothing
  step 4  subtract seasonal component (week-of-year mean)
  step 5  residualize against regional basis (section 5)  -> DSI_clean
Then (section 6):
  z = (DSI_clean - rolling 3y mean) / rolling 3y std
  regime gate: only trade when 13w realized spread vol is top-third of 3y dist
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import statsmodels.api as sm

WEEKS_PER_YEAR = 52
ROLL_3Y = 3 * WEEKS_PER_YEAR


def utilization(net_flow_bcf: pd.Series, max_rate_bcf_wk: float) -> pd.Series:
    """Fraction of the salt region's max weekly cycling rate that is being used.
    Signed: positive on injection, negative on withdrawal. Clipped to [-1.2,1.2]
    so a single noisy print can't blow up the convex transform."""
    u = net_flow_bcf / float(max_rate_bcf_wk)
    return u.clip(-1.2, 1.2)


def convex_stress(u: pd.Series, k: float = 4.0) -> pd.Series:
    """Convex, sign-preserving transform. g(u) = sign(u) * (e^{k|u|}-1)/(e^{k}-1).
    Near u=0 it is ~linear; near |u|=1 it accelerates -- the 'binding constraint'
    behaviour the proposal describes. k is a PRE-REGISTERED shape parameter."""
    return np.sign(u) * (np.expm1(k * u.abs()) / np.expm1(k))


def smooth(s: pd.Series, span_weeks: int = 3) -> pd.Series:
    return s.ewm(span=span_weeks, adjust=False).mean()


def deseasonalize(s: pd.Series) -> pd.Series:
    """Subtract the week-of-year mean (the seasonal component the market already
    prices). Uses an expanding week-of-year mean to avoid lookahead."""
    woy = s.index.isocalendar().week.astype(int)
    out = s.copy()
    seasonal = pd.Series(index=s.index, dtype=float)
    for w in range(1, 54):
        mask = (woy == w).values
        vals = s[mask]
        seasonal[mask] = vals.expanding().mean().shift(1).bfill()
    return out - seasonal.fillna(0.0)


def residualize_against_basis(dsi_raw: pd.Series,
                              waha_basis: pd.Series,
                              domsouth_basis: pd.Series,
                              window: int = ROLL_3Y) -> pd.Series:
    """Rolling-OLS residualization (proposal section 5):
        DSI_clean = DSI_raw - a1*dWaha - a2*dWaha_lag - a3*dDom - a4*dDom_lag
    Coefficients re-estimated on a rolling 3y window; output is the residual.
    Implemented as expanding/rolling refit, no lookahead (uses data up to t)."""
    df = pd.DataFrame({"dsi": dsi_raw}).copy()
    df["dWaha"] = waha_basis.reindex(dsi_raw.index).diff()
    df["dWaha_l"] = df["dWaha"].shift(1)
    df["dDom"] = domsouth_basis.reindex(dsi_raw.index).diff()
    df["dDom_l"] = df["dDom"].shift(1)
    df = df.dropna()
    X_cols = ["dWaha", "dWaha_l", "dDom", "dDom_l"]
    clean = pd.Series(index=df.index, dtype=float)
    for i in range(len(df)):
        lo = max(0, i - window + 1)
        train = df.iloc[lo:i + 1]
        if len(train) < 30:
            clean.iloc[i] = df["dsi"].iloc[i]
            continue
        X = sm.add_constant(train[X_cols])
        model = sm.OLS(train["dsi"], X).fit()
        x_t = np.r_[1.0, df[X_cols].iloc[i].values]
        fitted = float(x_t @ model.params.values)
        clean.iloc[i] = df["dsi"].iloc[i] - fitted
    return clean


def build_dsi(storage_salt: pd.DataFrame,
              max_rate_bcf_wk: float,
              waha_basis: pd.Series,
              domsouth_basis: pd.Series,
              k: float = 4.0) -> pd.DataFrame:
    """Full pipeline. Returns DataFrame with dsi_raw and dsi_clean."""
    u = utilization(storage_salt["net_flow_bcf"], max_rate_bcf_wk)
    g = convex_stress(u, k=k)
    g_sm = smooth(g, span_weeks=3)
    g_des = deseasonalize(g_sm)
    clean = residualize_against_basis(g_des, waha_basis, domsouth_basis)
    out = pd.DataFrame({"dsi_raw": g_des}).join(
        clean.rename("dsi_clean"), how="left")
    return out.dropna()


def zscore(s: pd.Series, window: int = ROLL_3Y) -> pd.Series:
    mu = s.rolling(window, min_periods=30).mean()
    sd = s.rolling(window, min_periods=30).std()
    return (s - mu) / sd


def vol_regime_gate(spread_returns: pd.Series,
                    rv_window: int = 13,
                    dist_window: int = ROLL_3Y) -> pd.Series:
    """True when 13-week realized spread vol is in the top third of its 3y dist.
    The proposal only allows trades when there is movement to translate."""
    rv = spread_returns.rolling(rv_window).std()
    rank = rv.rolling(dist_window, min_periods=30).apply(
        lambda x: (x[-1] >= np.nanpercentile(x, 66.6)) * 1.0, raw=True)
    return rank.fillna(0).astype(bool)
