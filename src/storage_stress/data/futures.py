"""
futures.py — the TRUE seasonal NG calendar spread, from individual contracts.
=============================================================================

WHY THIS MODULE EXISTS (the instrument-mismatch fix)
----------------------------------------------------
The LOCKED strategy (docs/strategy.md) trades a SEASONAL calendar spread:

    Injection season  (Apr-Oct):  front month  vs  next JANUARY
    Withdrawal season (Nov-Mar):  front month  vs  next APRIL

The live engine does exactly that (execution/contracts.py + broker.resolve_legs).
The backtest, however, used EIA's RNGC1 - RNGC2 — the ADJACENT-month spread —
because EIA only publishes contracts 1..4, i.e. four months out. An April front
vs next January is NINE months out, so the true seasonal spread was simply not
constructible from EIA data. Backtest and live were therefore measuring
DIFFERENT INSTRUMENTS with different volatility, which invalidated any
comparison between backtest Sharpe and live performance.

This module fixes that by building the seasonal spread from INDIVIDUAL NYMEX
contract settlements held in the WRDS/Datastream R2 mirror (schema tr_ds_fut):

    wrds_contract_info : one row per contract — futcode, contrdate (delivery
                         month, 'MMYY'), lasttrddate (expiry)
    dsfutcontrval      : daily settlement per futcode

583 NG contracts span 1990-2038, which ALSO closes the second data gap: EIA
discontinued RNGC1-4 on 2024-04-05, leaving the backtest blind for two years.

CRITICAL DESIGN CHOICE — shared month logic
-------------------------------------------
Front/deferred month selection and the delivery guard are imported from
`execution.contracts`, the SAME pure functions the live engine uses. The
backtest cannot drift from live behaviour, because there is only one
implementation of the rule.
"""
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd

from storage_stress.execution import contracts as C

NG_EXCH_TICKER = "NG"
NG_CONTRACT_NAME = "NATURAL GAS"
R2_FUTURES_SCHEMA = "tr_ds_fut"


def _parse_contrdate(mmyy: str) -> tuple[int, int] | None:
    """Datastream 'MMYY' delivery code -> (year, month).

    Century rule: the NG chain runs ~1990-2038, so a 2-digit year >= 90 means
    19xx and anything below means 20xx. Returns None for malformed codes so a
    single bad row can't abort the whole build.
    """
    if not isinstance(mmyy, str) or len(mmyy) != 4 or not mmyy.isdigit():
        return None
    month, yy = int(mmyy[:2]), int(mmyy[2:])
    if not 1 <= month <= 12:
        return None
    year = 1900 + yy if yy >= 90 else 2000 + yy
    return (year, month)


def ng_contract_chain(con) -> pd.DataFrame:
    """Every NYMEX NG contract: futcode, delivery (year, month), expiry.

    `con` is a DuckDB connection already configured for R2 (see
    connectivity._r2_duckdb). Returns one row per contract, sorted by expiry.
    """
    bucket = os.environ.get("R2_BUCKET", "quantt-historical-market-data")
    info = f"s3://{bucket}/wrds/{R2_FUTURES_SCHEMA}/wrds_contract_info.parquet"

    df = con.execute(f"""
        SELECT futcode, dsmnem, contrdate, lasttrddate
        FROM read_parquet('{info}')
        WHERE exchtickersymb = '{NG_EXCH_TICKER}'
          AND contrname = '{NG_CONTRACT_NAME}'
          AND isocurrcode = 'USD'
          AND contrdate IS NOT NULL AND lasttrddate IS NOT NULL
    """).fetchdf()

    parsed = df["contrdate"].map(_parse_contrdate)
    keep = parsed.notna()
    df = df[keep].copy()
    df["delivery_year"] = [p[0] for p in parsed[keep]]
    df["delivery_month"] = [p[1] for p in parsed[keep]]
    df["lasttrddate"] = pd.to_datetime(df["lasttrddate"])

    df = (df.sort_values("lasttrddate")
            .drop_duplicates(subset=["delivery_year", "delivery_month"],
                             keep="last")
            .sort_values("lasttrddate")
            .reset_index(drop=True))
    return df


def ng_contract_settlements(con, chain: pd.DataFrame, start: str) -> pd.DataFrame:
    """Daily settlements pivoted to  date x (delivery_year, delivery_month).

    One column per delivery month makes the per-date front/deferred lookup a
    simple column selection instead of a join.
    """
    bucket = os.environ.get("R2_BUCKET", "quantt-historical-market-data")
    val = f"s3://{bucket}/wrds/{R2_FUTURES_SCHEMA}/dsfutcontrval.parquet"

    codes = ",".join(str(int(c)) for c in chain["futcode"].dropna().unique())
    if not codes:
        raise LookupError("no NG futcodes resolved from the contract chain")

    px = con.execute(f"""
        SELECT futcode, date_, settlement
        FROM read_parquet('{val}')
        WHERE futcode IN ({codes})
          AND date_ >= DATE '{start}'
          AND settlement IS NOT NULL
    """).fetchdf()

    key = chain.set_index("futcode")[["delivery_year", "delivery_month"]]
    px = px.join(key, on="futcode")
    px["ym"] = list(zip(px["delivery_year"], px["delivery_month"]))
    wide = px.pivot_table(index="date_", columns="ym", values="settlement",
                          aggfunc="last")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def _deferred_seasonal(front_ym: tuple[int, int], _d: dt.date):
    """The LOCKED strategy: next January (injection) / next April (withdrawal).
    Delegates to the live engine's own function -- no second implementation."""
    return C.deferred_delivery_month(front_ym)


def _deferred_prompt(front_ym: tuple[int, int], _d: dt.date):
    """Adjacent month: the contract immediately after the front.

    HYPOTHESIS THIS TESTS: salt caverns are FAST-CYCLING storage (they turn over
    multiple times a season, unlike depleted reservoirs), so a salt-utilisation
    stress signal is inherently short-horizon. The prompt spread matches that
    horizon; a 9-month seasonal spread may not. It is also the tightest and most
    liquid NG calendar spread, which directly attacks the cost drag.
    """
    y, m = front_ym
    return (y + (m == 12), (m % 12) + 1)


def _deferred_mar_apr(front_ym: tuple[int, int], _d: dt.date):
    """Fixed March/April -- the 'widow-maker'.

    Not used via the front month at all: see _front_mar_apr, which overrides
    front selection to always target the next March. March is the last
    withdrawal month and April the first injection month, so this spread is the
    market's direct price on end-of-winter storage depletion.
    """
    y, _ = front_ym
    return (y, 4)


def _front_mar_apr(d: dt.date, tradeable_front: tuple[int, int],
                   expiry: dict, guard_bd: int):
    """Override front selection for the fixed Mar/Apr spread.

    Returns the nearest MARCH delivery month that is still tradeable (outside
    the delivery guard). Once a March expires we roll to the following year, so
    the position is held for months rather than rolled monthly -- which is the
    entire cost argument for a fixed spread.
    """
    year = d.year
    for _ in range(3):
        cand = (year, 3)
        exp = expiry.get(cand)
        if exp is not None and C.business_days_between(d, exp.date()) > guard_bd:
            return cand
        year += 1
    return None


SPREAD_DEFINITIONS = {
    "seasonal": (None, _deferred_seasonal,
                 "front vs next January (injection Apr-Oct) / next April "
                 "(withdrawal Nov-Mar) -- THE LOCKED STRATEGY"),
    "prompt": (None, _deferred_prompt,
               "front vs the adjacent (next) month -- shortest horizon, "
               "tightest/most liquid calendar spread"),
    "mar_apr": (_front_mar_apr, _deferred_mar_apr,
                "fixed March/April 'widow-maker' -- end-of-winter storage "
                "depletion; held without monthly rolling"),
}


def ng_spread(con, definition: str = "seasonal", start: str = "2017-01-01",
              guard_bd: int = C.DEFAULT_GUARD_BD) -> pd.DataFrame:
    """Daily NG calendar spread for one of the SPREAD_DEFINITIONS.

    Every definition shares the same machinery -- same contract universe, same
    delivery guard, same settlement source -- and differs ONLY in which two
    delivery months are selected. That is what makes an instrument A/B test
    valid: nothing else varies.

    Returns a DataFrame indexed by date with columns
        spread, front_ym, deferred_ym, front_px, deferred_px
    so every observation is auditable back to the exact contracts.
    """
    if definition not in SPREAD_DEFINITIONS:
        raise ValueError(f"unknown spread definition {definition!r}; "
                         f"expected one of {list(SPREAD_DEFINITIONS)}")
    front_rule, deferred_rule, description = SPREAD_DEFINITIONS[definition]

    chain = ng_contract_chain(con)
    wide = ng_contract_settlements(con, chain, start)

    expiry = {(int(r.delivery_year), int(r.delivery_month)): r.lasttrddate
              for r in chain.itertuples()}
    by_expiry = sorted(expiry.items(), key=lambda kv: kv[1])

    rows = []
    for date in wide.index:
        d = date.date()

        nearest = next(((ym, exp) for ym, exp in by_expiry if exp.date() >= d),
                       None)
        if nearest is None:
            continue

        front_ym = C.front_delivery_month(d, front_expiry=nearest[1].date(),
                                          guard_bd=guard_bd)
        if front_rule is not None:
            front_ym = front_rule(d, front_ym, expiry, guard_bd)
            if front_ym is None:
                continue
        deferred_ym = deferred_rule(front_ym, d)

        if front_ym not in wide.columns or deferred_ym not in wide.columns:
            continue
        f_px, d_px = wide.at[date, front_ym], wide.at[date, deferred_ym]
        if not (np.isfinite(f_px) and np.isfinite(d_px)):
            continue

        rows.append({"date": date, "spread": f_px - d_px,
                     "front_ym": f"{front_ym[0]}-{front_ym[1]:02d}",
                     "deferred_ym": f"{deferred_ym[0]}-{deferred_ym[1]:02d}",
                     "front_px": f_px, "deferred_px": d_px})

    out = pd.DataFrame(rows).set_index("date").sort_index()

    pair = out["front_ym"] + "|" + out["deferred_ym"]
    out["roll"] = pair != pair.shift()
    out.iloc[0, out.columns.get_loc("roll")] = False

    out.attrs["source"] = "datastream_r2_tr_ds_fut"
    out.attrs["instrument"] = definition
    out.attrs["definition"] = f"{description}; {guard_bd}-bd delivery guard"
    out.attrs["n_contracts"] = len(chain)
    out.attrs["n_rolls"] = int(out["roll"].sum())
    return out


def ng_seasonal_spread(con, start: str = "2017-01-01",
                       guard_bd: int = C.DEFAULT_GUARD_BD) -> pd.DataFrame:
    """The locked seasonal spread. Thin wrapper kept for existing callers."""
    return ng_spread(con, "seasonal", start=start, guard_bd=guard_bd)
