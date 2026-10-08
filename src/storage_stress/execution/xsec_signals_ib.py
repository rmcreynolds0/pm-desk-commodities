"""
xsec_signals_ib.py — compute the three factors LIVE from IBKR.
==============================================================

WHY THIS EXISTS
---------------
The research pipeline reads the WRDS/Datastream mirror on R2. Measured on
2026-08-12, that mirror is a periodic SNAPSHOT, not a feed: every one of the 22
markets was stale, ranging from 43 days (softs) to 243 days (corn, oats). Zero
markets had data inside 30 days. It is excellent for backtesting and unusable
for trading.

IBKR already provides everything the factors need, and is always current:

    carry       = ln(front / second) / dt_years        -- both legs, today
    momentum    = 12m compounded return of the front contract
    basis_mom   = momentum(front) - momentum(second)   [Boons & Prado]

Using one source for BOTH signals and execution also removes a whole class of
bug: the prices that generate a position are the prices it is traded and marked
against.

METHODOLOGICAL CONTINUITY
-------------------------
Returns are computed from a SINGLE contract's own bar history — never spliced
across contracts. That is the same roll-safety rule as the research code, and
the same rule whose violation fabricated 27-31% of apparent movement in the
natural-gas work.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

BAR_DURATION = "1 Y"
MIN_BARS = 120


def _daily_closes(ib, contract, duration: str = BAR_DURATION) -> pd.Series:
    """Daily closes for ONE contract. Empty series if IBKR returns nothing."""
    try:
        bars = ib.reqHistoricalData(contract, endDateTime="",
                                    durationStr=duration,
                                    barSizeSetting="1 day",
                                    whatToShow="TRADES", useRTH=True)
    except Exception:                                  # noqa: BLE001
        return pd.Series(dtype=float)
    if not bars:
        return pd.Series(dtype=float)
    s = pd.Series({pd.Timestamp(b.date): b.close for b in bars})
    return s.sort_index()


def _period_return(closes: pd.Series, months: int = 12) -> float | None:
    """Compounded return over the trailing `months`, from one contract's own
    bars. Returns None when history is too short to mean anything."""
    if len(closes) < MIN_BARS:
        return None
    window = min(len(closes), int(months * 21))
    seg = closes.iloc[-window:]
    if seg.iloc[0] <= 0:
        return None
    return float(seg.iloc[-1] / seg.iloc[0] - 1)


def build_live_panel(ib, resolver, universe: dict,
                     momentum_months: int = 12) -> pd.DataFrame:
    """One row per market with carry / mom / basis_mom, computed from IBKR.

    Column names deliberately match the research panel so the SAME
    `xsec.score()` and `xsec.target_weights()` run unchanged on both — there is
    only one implementation of the ladder.
    """
    rows = []
    for ticker in universe:
        front_d, second_d, front_exp, second_exp = resolver.front_and_second(ticker)
        if front_d is None or second_d is None:
            rows.append({"ticker": ticker, "status": "no term structure"})
            continue

        f_close = _daily_closes(ib, front_d.contract)
        s_close = _daily_closes(ib, second_d.contract)
        if f_close.empty or s_close.empty:
            rows.append({"ticker": ticker, "status": "no bars"})
            continue

        f_px, s_px = float(f_close.iloc[-1]), float(s_close.iloc[-1])
        dt_years = max((second_exp - front_exp).days, 1) / 365.25

        mom_f = _period_return(f_close, momentum_months)
        mom_s = _period_return(s_close, momentum_months)

        rows.append({
            "ticker": ticker,
            "status": "ok",
            "date": pd.Timestamp(f_close.index[-1]),
            "front_px": f_px,
            "second_px": s_px,
            "dt_years": dt_years,
            "carry": np.log(f_px / s_px) / dt_years,
            "mom": mom_f,
            "mom_second": mom_s,
            "basis_mom": (None if (mom_f is None or mom_s is None)
                          else mom_f - mom_s),
            "multiplier": resolver.multiplier(front_d),
            "magnifier": resolver.price_magnifier(front_d),
            "notional": resolver.notional(front_d, f_px),
            "local_symbol": front_d.contract.localSymbol,
            "expiry": front_exp,
            "_details": front_d,
        })

    return pd.DataFrame(rows)


def usable(panel: pd.DataFrame, factors: list[str]) -> pd.DataFrame:
    """Rows with every factor this agent needs.

    A market missing one required factor is EXCLUDED rather than filled with a
    neutral value: imputing zero would place it mid-rank on a fabricated score,
    which is a silent way to trade on nothing.
    """
    ok = panel[panel["status"] == "ok"].copy()
    for f in factors:
        ok = ok[ok[f].notna()]
    return ok
