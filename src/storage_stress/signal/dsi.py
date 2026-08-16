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


# --- step 1 -----------------------------------------------------------------
def utilization(net_flow_bcf: pd.Series, max_rate_bcf_wk: float) -> pd.Series:
    """Fraction of the salt region's max weekly cycling rate that is being used.
    Signed: positive on injection, negative on withdrawal. Clipped to [-1.2,1.2]
    so a single noisy print can't blow up the convex transform."""
    # u_t = net_flow_t / max_rate  -- normalizes weekly Bcf flow into a
    # dimensionless ratio of "how much of the region's max cycling capacity
    # is being used this week". net_flow_bcf is signed (+injection, -withdrawal),
    # so u inherits that sign elementwise (vectorized division over the Series).
    u = net_flow_bcf / float(max_rate_bcf_wk)
    # Clip to [-1.2, 1.2]: allows a *little* overshoot past "100% of max rate"
    # (real prints can exceed the nominal max), but caps outliers so step 2's
    # exponential transform doesn't blow up on a single bad data point.
    return u.clip(-1.2, 1.2)


# --- step 2 -----------------------------------------------------------------
def convex_stress(u: pd.Series, k: float = 4.0) -> pd.Series:
    """Convex, sign-preserving transform. g(u) = sign(u) * (e^{k|u|}-1)/(e^{k}-1).
    Near u=0 it is ~linear; near |u|=1 it accelerates -- the 'binding constraint'
    behaviour the proposal describes. k is a PRE-REGISTERED shape parameter."""
    # g(u) = sign(u) * (e^(k*|u|) - 1) / (e^k - 1)
    #
    # np.expm1(x) computes e^x - 1 (more numerically stable near x=0 than
    # np.exp(x)-1). So:
    #   numerator   = np.expm1(k * u.abs())   ->  e^(k|u|) - 1
    #   denominator = np.expm1(k)             ->  e^k - 1   (a constant, since k is fixed)
    # Dividing by the constant e^k-1 normalizes so that g(1) = sign(u)*1, i.e.
    # at full utilization (|u|=1) the output is exactly +/-1.
    #
    # np.sign(u) reapplies the original sign, since u.abs() discarded it --
    # this keeps injection (+) vs withdrawal (-) meaningful in the output.
    #
    # Shape intuition: because e^x grows faster than linearly, this function
    # is ~linear for small |u| (near 0, e^(k|u|)-1 ~ k|u|) but curves upward
    # sharply as |u| -> 1 -- i.e. stress accelerates as storage nears its
    # physical cycling limit, which is the "binding constraint" behavior.
    return np.sign(u) * (np.expm1(k * u.abs()) / np.expm1(k))


# --- step 3 -----------------------------------------------------------------
def smooth(s: pd.Series, span_weeks: int = 3) -> pd.Series:
    # Exponentially-weighted moving average with span=3 weeks.
    # adjust=False -> recursive form: y_t = alpha*x_t + (1-alpha)*y_{t-1},
    # where alpha = 2/(span+1) = 0.5 here. This is a causal filter (each
    # output only depends on current + past values, no lookahead) that
    # smooths out single-week noise in the convex-stress signal.
    return s.ewm(span=span_weeks, adjust=False).mean()


# --- step 4 -----------------------------------------------------------------
def deseasonalize(s: pd.Series) -> pd.Series:
    """Subtract the week-of-year mean (the seasonal component the market already
    prices). Uses an expanding week-of-year mean to avoid lookahead."""
    # woy[t] = ISO week-of-year (1-53) for each timestamp in the index.
    # Used to group observations by "calendar position" (e.g. all the
    # 'week 14's across different years) since storage stress is highly
    # seasonal and the market already prices in the typical seasonal pattern.
    woy = s.index.isocalendar().week.astype(int)
    out = s.copy()
    seasonal = pd.Series(index=s.index, dtype=float)
    # Loop over each of the 53 possible ISO week numbers...
    for w in range(1, 54):
        mask = (woy == w).values          # boolean mask: which rows fall in week w
        vals = s[mask]                    # subsequence of s restricted to week-of-year w
        # For each occurrence of week w (typically one per year), compute the
        # *expanding* mean using only years seen so far (.expanding().mean()),
        # then .shift(1) so that this year's own value is excluded from its
        # own baseline (no lookahead: this year's seasonal estimate only uses
        # prior years). .bfill() fills the very first occurrence (which has
        # no prior years) with the next available expanding-mean value.
        seasonal[mask] = vals.expanding().mean().shift(1).bfill()
    # Subtract the seasonal baseline from the original series; any week-of-year
    # rows where mask never matched (shouldn't normally happen) default to 0
    # via fillna(0.0) rather than NaN.
    return out - seasonal.fillna(0.0)


# --- step 5 -----------------------------------------------------------------
def residualize_against_basis(dsi_raw: pd.Series,
                              waha_basis: pd.Series,
                              domsouth_basis: pd.Series,
                              window: int = ROLL_3Y) -> pd.Series:
    """Rolling-OLS residualization (proposal section 5):
        DSI_clean = DSI_raw - a1*dWaha - a2*dWaha_lag - a3*dDom - a4*dDom_lag
    Coefficients re-estimated on a rolling 3y window; output is the residual.
    Implemented as expanding/rolling refit, no lookahead (uses data up to t)."""
    # Build a design-matrix DataFrame aligned to dsi_raw's index.
    df = pd.DataFrame({"dsi": dsi_raw}).copy()
    # dWaha_t = Waha_basis_t - Waha_basis_{t-1}  (first difference = weekly change)
    df["dWaha"] = waha_basis.reindex(dsi_raw.index).diff()
    # one-week-lagged version of dWaha, as its own regressor (captures delayed
    # basis effects on the DSI signal)
    df["dWaha_l"] = df["dWaha"].shift(1)
    # same construction for the Dominion South basis series
    df["dDom"] = domsouth_basis.reindex(dsi_raw.index).diff()
    df["dDom_l"] = df["dDom"].shift(1)
    # drop rows with any NaN (from .diff()/.shift() warmup) so every row used
    # below has a complete feature vector
    df = df.dropna()
    X_cols = ["dWaha", "dWaha_l", "dDom", "dDom_l"]
    clean = pd.Series(index=df.index, dtype=float)
    # Walk forward one observation at a time (i = 0 .. len(df)-1):
    for i in range(len(df)):
        # Rolling window of at most `window` (=ROLL_3Y=156 weeks) trailing
        # observations ending at i (inclusive) -- lo is clamped at 0 so early
        # in the series the window is just "everything seen so far".
        lo = max(0, i - window + 1)
        train = df.iloc[lo:i + 1]
        if len(train) < 30:
            # Not enough history yet to fit a stable regression (need >=30
            # observations) -- fall back to the raw (unresidualized) value.
            clean.iloc[i] = df["dsi"].iloc[i]
            continue
        # Fit y = a0 + a1*dWaha + a2*dWaha_l + a3*dDom + a4*dDom_l by OLS,
        # using only data up to and including row i (no lookahead: the model
        # used to residualize today's value was estimated on data available
        # up to today).
        X = sm.add_constant(train[X_cols])   # prepend a column of 1s for the intercept a0
        model = sm.OLS(train["dsi"], X).fit()
        # x_t = [1, dWaha_i, dWaha_l_i, dDom_i, dDom_l_i] -- today's regressor
        # row, with a leading 1 to match the intercept term in model.params.
        x_t = np.r_[1.0, df[X_cols].iloc[i].values]
        # fitted = a0 + a1*dWaha_i + a2*dWaha_l_i + a3*dDom_i + a4*dDom_l_i
        # (dot product of today's regressors with the just-fitted coefficients)
        fitted = float(x_t @ model.params.values)
        # residual = actual DSI_raw_i - the portion "explained" by basis moves
        # -> this is DSI_clean_i, the part of the stress signal NOT already
        # attributable to regional basis price action.
        clean.iloc[i] = df["dsi"].iloc[i] - fitted
    return clean


def build_dsi(storage_salt: pd.DataFrame,
              max_rate_bcf_wk: float,
              waha_basis: pd.Series,
              domsouth_basis: pd.Series,
              k: float = 4.0) -> pd.DataFrame:
    """Full pipeline. Returns DataFrame with dsi_raw and dsi_clean."""
    # Chain steps 1-5 in order, each function's output feeding the next --
    # this literally is the pipeline diagram in the module docstring.
    u = utilization(storage_salt["net_flow_bcf"], max_rate_bcf_wk)   # step 1: raw flow -> utilization ratio
    g = convex_stress(u, k=k)                                        # step 2: convex transform
    g_sm = smooth(g, span_weeks=3)                                   # step 3: 3-week EWMA smoothing
    g_des = deseasonalize(g_sm)                                      # step 4: remove week-of-year seasonality -> dsi_raw
    clean = residualize_against_basis(g_des, waha_basis, domsouth_basis)  # step 5: strip out basis-explained component -> dsi_clean
    # Combine both intermediate (dsi_raw) and final (dsi_clean) series into
    # one DataFrame for downstream inspection/plotting, aligned on the index.
    out = pd.DataFrame({"dsi_raw": g_des}).join(
        clean.rename("dsi_clean"), how="left")
    # Drop any rows where either column is still NaN (e.g. warmup period
    # before enough history existed for the rolling regression in step 5).
    return out.dropna()


# --- step 6: signal -> z-score + regime gate --------------------------------
def zscore(s: pd.Series, window: int = ROLL_3Y) -> pd.Series:
    # Standard rolling z-score: z_t = (s_t - rolling_mean_t) / rolling_std_t,
    # using a trailing window of `window` observations (default 156 weeks =
    # 3 years, from ROLL_3Y = 3*52). min_periods=30 means the first 29
    # observations produce NaN (not enough history to trust mean/std yet).
    # This turns dsi_clean into a "how many standard deviations from its own
    # recent normal is stress right now" number, so the same threshold
    # (entry_z) is comparable across different regimes over time.
    mu = s.rolling(window, min_periods=30).mean()
    sd = s.rolling(window, min_periods=30).std()
    return (s - mu) / sd


def vol_regime_gate(spread_returns: pd.Series,
                    rv_window: int = 13,
                    dist_window: int = ROLL_3Y) -> pd.Series:
    """True when 13-week realized spread vol is in the top third of its 3y dist.
    The proposal only allows trades when there is movement to translate."""
    # Realized vol: rolling 13-week standard deviation of the spread's returns
    # -- a short-window measure of "how much is the spread currently moving".
    rv = spread_returns.rolling(rv_window).std()
    # For each point in time, look back over a rolling 3y window of past rv
    # values (x) and check: is *today's* rv (x[-1], the last element of that
    # window) at or above the 66.6th percentile of that trailing distribution?
    # raw=True means x arrives as a numpy array (faster) rather than a Series.
    # The result of the lambda is 1.0 (top third) or 0.0 (bottom two-thirds),
    # so `rank` ends up as a rolling boolean-as-float series.
    rank = rv.rolling(dist_window, min_periods=30).apply(
        lambda x: (x[-1] >= np.nanpercentile(x, 66.6)) * 1.0, raw=True)
    # Rationale: the DSI signal alone can be right about *direction* of stress
    # but if the spread isn't actually moving there's no P&L to capture, so
    # this gate blocks entries during low-realized-vol regimes. NaN (warmup)
    # rows default to False (0) -- i.e. don't trade until the gate can be
    # computed with enough history.
    return rank.fillna(0).astype(bool)
