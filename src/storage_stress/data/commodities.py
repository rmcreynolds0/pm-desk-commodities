"""
commodities.py — multi-commodity futures panel + the classic factor signals.
============================================================================

PIVOT A. The natural-gas storage-stress thesis failed its own week-1 regime
gate (post-2016 spreads became 29% LESS volatile with unchanged seasonality --
the opposite of the "tightening system" premise) and no agent beat a calibrated
random benchmark. Rather than discard the infrastructure, we point it at the
best-documented premia in commodities.

WHAT THE LITERATURE SUPPORTS (and what we therefore build):
  * CARRY / BASIS -- the term-structure slope. Backwardated markets (front above
    deferred) have historically earned positive returns, contangoed ones
    negative. The single most replicated commodity premium; sorts on basis
    produce spot premia of roughly 5-14% p.a.
  * MOMENTUM -- 12-month trailing return, a standard cross-sectional sort.
  * BASIS-MOMENTUM (Boons & Prado) -- the difference between the momentum of
    the front and of the deferred contract, i.e. how the CURVE ITSELF has been
    moving. Distinct from either carry or plain momentum.

These map onto an information ladder exactly like the NG agents:
    agent_0  random                       (null benchmark)
    agent_1  carry                        (+ first real signal)
    agent_2  carry + momentum             (+ second)
    agent_3  carry + momentum + basis-mom (full signal)

TWO CORRECTNESS RULES CARRIED OVER FROM THE NG WORK -- both were real bugs:
  1. ROLL-SAFE RETURNS. A return is only ever computed between two prices of
     the SAME contract. Splicing contracts and differencing the result
     fabricates P&L (measured at 6-14x a normal daily move on NG data).
  2. COSTS ARE CHARGED. Turnover is costed explicitly; see the simulator.
TRAPS
-----
RETURNS ARE ROLL-SAFE AND MUST STAY THAT WAY. A daily return is computed only
between two prices of the SAME futcode. Differencing across a contract change
fabricates the roll gap as P&L -- on the gas spreads that accounted for up to
31% of all measured price movement. add_factors builds returns per contract
and then reads off whichever is front or second, never differencing a spliced
series.

CARRY MUST BE ANNUALISED. Dividing the log slope by dt_years is not cosmetic:
markets have different listing cycles, so a one-month and a three-month spread
are not comparable until normalised.

add_stress PORTS THE RETIRED GAS METHOD AND WAS REJECTED. It is kept because
scripts/stress_test.py uses it as the evidence behind that rejection; it is not
in the live blend. See docs/STRESS_FACTOR.md.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

R2_FUTURES_SCHEMA = "tr_ds_fut"

COMMODITY_UNIVERSE = {
    "CL": "Crude Oil (WTI)",
    "NG": "Natural Gas",
    "HO": "Heating Oil",
    "RB": "Gasoline RBOB",
    "GC": "Gold",
    "SI": "Silver",
    "HG": "Copper",
    "PL": "Platinum",
    "PA": "Palladium",
    "ZC": "Corn",
    "ZS": "Soybeans",
    "ZM": "Soybean Meal",
    "ZL": "Soybean Oil",
    "ZO": "Oats",
    "KE": "Wheat (Hard Red Winter)",
    "CC": "Cocoa",
    "KC": "Coffee",
    "SB": "Sugar",
    "CT": "Cotton",
    "OJ": "Orange Juice",
    "LE": "Live Cattle",
    "GF": "Feeder Cattle",
}


def _uri(table: str) -> str:
    bucket = os.environ.get("R2_BUCKET", "quantt-historical-market-data")
    return f"s3://{bucket}/wrds/{R2_FUTURES_SCHEMA}/{table}.parquet"


def _parse_contrdate(mmyy: str) -> tuple[int, int] | None:
    """Datastream 'MMYY' -> (year, month). Years >= 90 are 19xx, else 20xx."""
    if not isinstance(mmyy, str) or len(mmyy) != 4 or not mmyy.isdigit():
        return None
    month, yy = int(mmyy[:2]), int(mmyy[2:])
    if not 1 <= month <= 12:
        return None
    return (1900 + yy if yy >= 90 else 2000 + yy, month)


def load_chain(con, tickers: list[str] | None = None) -> pd.DataFrame:
    """All contracts for the universe: ticker, futcode, delivery month, expiry.

    Restricted to ldb='COM' (Datastream's commodity list) and USD so we get the
    primary listing of each market rather than regional or financial variants.
    """
    tickers = tickers or list(COMMODITY_UNIVERSE)
    quoted = ",".join(f"'{t}'" for t in tickers)
    df = con.execute(f"""
        SELECT exchtickersymb AS ticker, futcode, contrdate, lasttrddate
        FROM read_parquet('{_uri('wrds_contract_info')}')
        WHERE exchtickersymb IN ({quoted})
          AND isocurrcode = 'USD' AND ldb = 'COM'
          AND contrdate IS NOT NULL AND lasttrddate IS NOT NULL
    """).fetchdf()

    parsed = df["contrdate"].map(_parse_contrdate)
    keep = parsed.notna()
    df = df[keep].copy()
    df["delivery"] = [pd.Timestamp(year=p[0], month=p[1], day=1)
                      for p in parsed[keep]]
    df["lasttrddate"] = pd.to_datetime(df["lasttrddate"])
    df = (df.sort_values("lasttrddate")
            .drop_duplicates(subset=["ticker", "delivery"], keep="last")
            .reset_index(drop=True))
    return df


def load_settlements(con, chain: pd.DataFrame, start: str) -> pd.DataFrame:
    """Daily settlements for every contract in the chain (long format)."""
    codes = ",".join(str(int(c)) for c in chain["futcode"].dropna().unique())
    px = con.execute(f"""
        SELECT futcode, date_ AS date, settlement
        FROM read_parquet('{_uri('dsfutcontrval')}')
        WHERE futcode IN ({codes}) AND date_ >= DATE '{start}'
          AND settlement IS NOT NULL AND settlement > 0
    """).fetchdf()
    px["date"] = pd.to_datetime(px["date"])
    meta = chain.set_index("futcode")[["ticker", "delivery", "lasttrddate"]]
    return px.join(meta, on="futcode")


def build_panel(px: pd.DataFrame, guard_days: int = 5) -> pd.DataFrame:
    """Per (date, ticker): the front and second contract, their prices, and the
    front's own prior-day price (which is what makes returns ROLL-SAFE).

    'Front' = nearest contract whose expiry is more than `guard_days` calendar
    days away -- the same never-hold-into-delivery discipline the NG engine
    uses. 'Second' = the next delivery month after the front.
    """
    px = px.sort_values(["ticker", "date", "delivery"])
    px["dte"] = (px["lasttrddate"] - px["date"]).dt.days
    live = px[px["dte"] > guard_days]

    live = live.copy()
    live["rank"] = live.groupby(["ticker", "date"])["delivery"].rank(
        method="first").astype(int) - 1
    near = live[live["rank"] <= 1]

    wide = near.pivot_table(index=["date", "ticker"], columns="rank",
                            values=["settlement", "futcode"], aggfunc="first")
    out = pd.DataFrame({
        "front_px": wide[("settlement", 0)],
        "second_px": wide[("settlement", 1)],
        "front_code": wide[("futcode", 0)],
        "second_code": wide[("futcode", 1)],
    }).reset_index()

    deliv = near.pivot_table(index=["date", "ticker"], columns="rank",
                             values="delivery", aggfunc="first")
    out["dt_years"] = ((deliv[1] - deliv[0]).dt.days / 365.25).values
    return out.dropna(subset=["front_px", "second_px", "dt_years"])


def add_factors(panel: pd.DataFrame, px: pd.DataFrame,
                mom_months: int = 12) -> pd.DataFrame:
    """Attach carry, momentum and basis-momentum to the panel.

    ROLL-SAFE RETURNS: a daily return is computed only between two prices of
    the SAME futcode. We therefore build the return series per contract and
    then read off whichever contract is front (or second) on each date -- never
    differencing across a contract change.
    """
    px = px.sort_values(["futcode", "date"])
    px["ret"] = px.groupby("futcode")["settlement"].pct_change()

    ret_front = px.set_index(["date", "futcode"])["ret"]
    idx_front = pd.MultiIndex.from_arrays(
        [panel["date"], panel["front_code"]])
    idx_second = pd.MultiIndex.from_arrays(
        [panel["date"], panel["second_code"]])
    panel = panel.copy()
    panel["front_ret"] = ret_front.reindex(idx_front).values
    panel["second_ret"] = ret_front.reindex(idx_second).values

    panel["carry"] = (np.log(panel["front_px"] / panel["second_px"])
                      / panel["dt_years"])

    win = int(mom_months * 21)
    panel = panel.sort_values(["ticker", "date"])
    g = panel.groupby("ticker", group_keys=False)
    panel["mom"] = g["front_ret"].apply(
        lambda s: (1 + s.fillna(0)).rolling(win, min_periods=win // 2).apply(
            np.prod, raw=True) - 1)
    panel["mom_second"] = g["second_ret"].apply(
        lambda s: (1 + s.fillna(0)).rolling(win, min_periods=win // 2).apply(
            np.prod, raw=True) - 1)
    panel["basis_mom"] = panel["mom"] - panel["mom_second"]
    return panel


def add_stress(panel: pd.DataFrame, k: float = 4.0, span_days: int = 15,
               scale_window: int = 756, resid_window: int = 756,
               scale_q: float = 0.90) -> pd.DataFrame:
    """Attach a `stress` factor built with the Deliverability Stress Index
    pipeline from the retired natural-gas strategy.

    WHY THIS EXISTS
    ---------------
    The gas strategy was retired because its HYPOTHESIS was falsified, not
    because its METHOD was wrong. The method -- turn a physical-state variable
    into a signal by making it convex, smoothing it, removing the seasonality
    the market already prices, and stripping the part explained by something
    else -- is reusable. This applies those five steps to the curve state of
    every market in the cross-section.

    THE FIVE STEPS, and what each maps to

      1. UTILIZATION.  Gas used `net_flow / max_cycling_rate`: how close the
         system is to its physical limit. Here the analogue is how extreme the
         curve is relative to its own recent range:

             u = carry / rolling_quantile(|carry|, 90th, 3y)

         The 90th percentile rather than the max, because a max is set by a
         single outlier and would make `u` jump around for reasons that have
         nothing to do with the market. Clipped to [-1.2, 1.2] exactly as the
         gas version clipped utilization.

      2. CONVEX TRANSFORM.  Reuses dsi.convex_stress with the SAME
         pre-registered k=4.0. This is the actual idea being carried over:
         stress is not linear in state. A curve at 30% of its normal range
         means little; one at 95% means a constraint is binding. Near u=0 the
         transform is ~linear, near |u|=1 it accelerates.

      3. SMOOTHING.  15 trading days ~ the 3 weeks the weekly gas version used.
         Causal EWMA; no lookahead.

      4. DESEASONALIZE.  Commodity curves are enormously seasonal -- gas,
         heating oil, grains and cattle all have calendar-driven shapes the
         market already prices. Subtracting the expanding week-of-year mean
         leaves only the part of the curve state that is unusual FOR THE TIME
         OF YEAR. Expanding and shifted by one so a year never informs its own
         baseline.

      5. RESIDUALIZE.  The gas version removed the component explained by
         regional basis. The cross-sectional analogue that actually earns its
         place is removing the component explained by CARRY ITSELF.

         This step is the difference between a useful factor and a redundant
         one. Steps 1-2 make stress a convex function of carry, so without
         this it would be ~0.9 correlated with carry and add almost nothing to
         a blend that already contains it. Residualizing leaves only the part
         of curve-state extremity that the raw slope does not already say.

         Implemented as a vectorised rolling univariate OLS (beta from rolling
         covariance / variance) rather than the gas version's row-by-row
         statsmodels refit: that loop was fine for 1,527 weekly observations
         and would be ~147,000 regressions here.

    NOT cross-sectionally demeaned here -- agents.xsec z-scores every factor
    within each date already, and doing it twice would be a silent no-op that
    future readers would mistake for a safeguard.
    """
    from storage_stress.signal.dsi import convex_stress

    panel = panel.sort_values(["ticker", "date"]).copy()
    g = panel.groupby("ticker", group_keys=False)

    scale = g["carry"].apply(
        lambda s: s.abs().rolling(scale_window, min_periods=252).quantile(scale_q))
    scale = scale.where(scale > 1e-8)
    panel["_u"] = (panel["carry"] / scale).clip(-1.2, 1.2)

    panel["_g"] = convex_stress(panel["_u"], k=k)

    panel["_g_sm"] = g["_g"].apply(
        lambda s: s.ewm(span=span_days, adjust=False).mean())

    panel["_woy"] = panel["date"].dt.isocalendar().week.astype(int).values
    seasonal = panel.groupby(["ticker", "_woy"], group_keys=False)["_g_sm"].apply(
        lambda s: s.expanding().mean().shift(1))
    panel["_g_des"] = panel["_g_sm"] - seasonal.reindex(panel.index).fillna(0.0)

    def _residualize(df: pd.DataFrame) -> pd.Series:
        y, x = df["_g_des"], df["carry"]
        cov = y.rolling(resid_window, min_periods=252).cov(x)
        var = x.rolling(resid_window, min_periods=252).var()
        beta = (cov / var.where(var > 1e-12))
        alpha = (y.rolling(resid_window, min_periods=252).mean()
                 - beta * x.rolling(resid_window, min_periods=252).mean())
        fitted = alpha + beta * x
        return y - fitted.fillna(0.0)

    panel["stress"] = panel.groupby("ticker", group_keys=False).apply(_residualize)

    return panel.drop(columns=["_u", "_g", "_g_sm", "_woy", "_g_des"])
