"""
connectivity.py
================
Working API calls for every data source the storage-stress strategy needs,
plus the trading/backtest platform (IBKR via ib_async).

Design notes
------------
* Every fetch function has a `synthetic=` fallback so the notebooks run end-to-end
  even on a machine with no API key / no internet / no IB Gateway. The synthetic
  generators are seeded, so results are reproducible.
* EIA v2 is the primary free data source. Register a free key at
  https://www.eia.gov/opendata/  (key arrives by email instantly).
* IBKR connectivity uses ib_async (NOT ib_insync). ib_insync is unmaintained
  since its author's death in early 2024; ib_async is the drop-in successor and
  is what you should put in the proposal in place of ib_insync.

Run `python src/connectivity.py` for a self-test of every endpoint.
"""
from __future__ import annotations
import os
import io
import time
import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests

EIA_BASE   = "https://api.eia.gov/v2"
EIA_KEY    = os.environ.get("EIA_API_KEY", "")
NOAA_TOKEN = os.environ.get("NOAA_TOKEN", "")
FRED_KEY   = os.environ.get("FRED_API_KEY", "")
FRED_BASE  = "https://api.stlouisfed.org/fred"

EIA_STORAGE_SERIES = {
    "salt_south_central": "NW2_EPG0_SSO_R33_BCF",
    "nonsalt_south_central": "NW2_EPG0_SNO_R33_BCF",
    "lower48_total": "NW2_EPG0_SWO_R48_BCF",
}


def eia_storage(series_key: str = "salt_south_central",
                start: str = "2017-01-01",
                synthetic: bool | None = None) -> pd.DataFrame:
    """Weekly working-gas storage level (Bcf) for one EIA series.

    Returns a DataFrame indexed by date with columns ['level_bcf', 'net_flow_bcf'].
    net_flow is the week-over-week change (injection > 0, withdrawal < 0).
    """
    series_id = EIA_STORAGE_SERIES[series_key]
    if synthetic is None:
        synthetic = not EIA_KEY
    if synthetic:
        return _synthetic_storage(series_key, start)

    url = f"{EIA_BASE}/natural-gas/stor/wkly/data/"
    params = {
        "api_key": EIA_KEY,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": series_id,
        "start": start,
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": 5000,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    rows = r.json()["response"]["data"]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["period"])
    df = df.set_index("date").sort_index()
    df["level_bcf"] = pd.to_numeric(df["value"])
    df["net_flow_bcf"] = df["level_bcf"].diff()
    return df[["level_bcf", "net_flow_bcf"]].dropna()


def eia_henryhub_futures(contract: int = 1,
                         start: str = "2017-01-01",
                         synthetic: bool | None = None) -> pd.DataFrame:
    """Daily Henry Hub futures settle ($/MMBtu) for contract N (1..4)."""
    if synthetic is None:
        synthetic = not EIA_KEY
    if synthetic:
        return _synthetic_price(contract, start)
    url = f"{EIA_BASE}/natural-gas/pri/fut/data/"
    params = {
        "api_key": EIA_KEY,
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": f"RNGC{contract}",
        "start": start,
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": 5000,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    rows = r.json()["response"]["data"]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["period"])
    df = df.set_index("date").sort_index()
    df[f"settle_c{contract}"] = pd.to_numeric(df["value"])
    return df[[f"settle_c{contract}"]].dropna()


def noaa_degree_days(start: str = "2017-01-01",
                     synthetic: bool | None = None) -> pd.DataFrame:
    """Weekly heating & cooling degree days (population-weighted, US).

    Uses FRED EMNHDD/EMNCDD (monthly, forward-filled to weekly) when
    FRED_API_KEY is set; falls back to synthetic seasonal model otherwise.
    NOAA_TOKEN is available for future regional/granular degree-day pulls.
    """
    if synthetic is None:
        synthetic = not FRED_KEY
    if synthetic:
        return _synthetic_degree_days(start)
    try:
        return _fred_degree_days(start)
    except Exception as exc:
        print(f"    [WARN] FRED degree-days failed ({exc}); using synthetic fallback")
        return _synthetic_degree_days(start)


DATASTREAM_HUB_SERIES = {
    "waha": ("NATGWTX", "Natural Gas, West Texas (Permian ~ Waha region)"),
    "domsouth": ("NATGAPP", "Natural Gas, Appalachia Average (~ Dom South region)"),
}
DATASTREAM_HENRY_HUB = "NGHHSNL"
R2_COMMODITY_SCHEMA = "tr_ds_comds"


def _r2_duckdb():
    """DuckDB connection configured to read the R2 parquet mirror over S3.

    Credentials come from the environment (.env): R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_ENDPOINT, optional R2_BUCKET. Raises a clear
    error naming any MISSING variable -- never echoing a value.
    """
    import duckdb

    needed = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "R2 credentials missing from environment/.env: " + ", ".join(missing))

    endpoint = (os.environ["R2_ENDPOINT"].replace("https://", "")
                .replace("http://", "").rstrip("/"))
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint}';")
    con.execute("SET s3_region='auto';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_use_ssl=true;")
    con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}';")
    con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}';")
    return con


def _datastream_series(con, mnemonic: str, start: str) -> pd.Series:
    """Daily close series for one Datastream commodity mnemonic.

    Two-step lookup because Datastream keys values by numeric `comcode`:
      dscminfo : mnemonic -> comcode   (the series catalogue)
      dscmval  : comcode  -> daily observations (date_, close_)
    """
    bucket = os.environ.get("R2_BUCKET", "quantt-historical-market-data")
    info = f"s3://{bucket}/wrds/{R2_COMMODITY_SCHEMA}/dscminfo.parquet"
    val = f"s3://{bucket}/wrds/{R2_COMMODITY_SCHEMA}/dscmval.parquet"

    row = con.execute(
        f"SELECT comcode FROM read_parquet('{info}') WHERE dsmnemonic = ?",
        [mnemonic]).fetchone()
    if row is None:
        raise LookupError(f"Datastream mnemonic {mnemonic} not found in the mirror")
    comcode = int(row[0])

    df = con.execute(
        f"""SELECT date_, close_ FROM read_parquet('{val}')
            WHERE comcode = {comcode} AND date_ >= DATE '{start}'
              AND close_ IS NOT NULL
            ORDER BY date_""").fetchdf()
    s = pd.Series(df["close_"].values,
                  index=pd.to_datetime(df["date_"]), name=mnemonic)
    return s


BASIS_CLIP_USD = 10.0


def regional_basis(hub: str = "waha",
                   start: str = "2017-01-01",
                   synthetic: bool | None = None,
                   clip_usd: float | None = BASIS_CLIP_USD) -> pd.DataFrame:
    """Regional gas basis (hub minus Henry Hub), in $/MMBtu.

    hub : 'waha' | 'domsouth'  (see DATASTREAM_HUB_SERIES for the mapping to
          the regional proxy series and the caveat about regional averages)
    synthetic : None (default) -> use real R2 data when credentials exist,
                otherwise fall back to the seeded synthetic generator so the
                repo still runs end-to-end on a machine with no credentials.
                True/False forces the choice.
    clip_usd : winsorize basis to +-this many $/MMBtu (see BASIS_CLIP_USD for
               the rationale). Pass None to get the raw, unclipped series --
               useful for inspecting the extremes, NOT for feeding the OLS.

    Returns a one-column DataFrame indexed by date, named '<hub>_basis'. The
    `.attrs` carry provenance: 'source', 'series', 'description', 'last_date'
    (so callers can detect a stale mirror) and 'n_clipped'.
    """
    if hub not in DATASTREAM_HUB_SERIES:
        raise ValueError(f"unknown hub {hub!r}; expected one of "
                         f"{list(DATASTREAM_HUB_SERIES)}")

    if synthetic is None:
        synthetic = not os.environ.get("R2_ACCESS_KEY_ID")

    if synthetic:
        out = _synthetic_basis(hub, start)
        out.attrs["source"] = "synthetic"
        return out

    mnemonic, description = DATASTREAM_HUB_SERIES[hub]
    con = _r2_duckdb()
    try:
        regional = _datastream_series(con, mnemonic, start)
        henry = _datastream_series(con, DATASTREAM_HENRY_HUB, start)
    finally:
        con.close()

    joined = pd.concat({"regional": regional, "henry": henry},
                       axis=1, join="inner").dropna()
    basis = (joined["regional"] - joined["henry"]).rename(f"{hub}_basis")

    n_clipped = 0
    if clip_usd is not None:
        n_clipped = int((basis.abs() > clip_usd).sum())
        basis = basis.clip(-clip_usd, clip_usd)

    out = basis.to_frame()
    out.attrs["source"] = "datastream_r2"
    out.attrs["series"] = f"{mnemonic} - {DATASTREAM_HENRY_HUB}"
    out.attrs["description"] = description
    out.attrs["last_date"] = None if out.empty else out.index[-1].date().isoformat()
    out.attrs["n_clipped"] = n_clipped
    return out


@dataclass
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 11


def ibkr_smoke_test(cfg: IBKRConfig | None = None):
    """Connect to a running IB Gateway/TWS paper session and pull one NG future.

    Requires: `pip install ib_async`, and IB Gateway running in PAPER mode with
    API enabled (Configure > API > Enable ActiveX and Socket Clients, add 127.0.0.1
    to trusted IPs, set the socket port). Market-data subscription for NYMEX
    futures is needed for live quotes; delayed data works for testing.
    """
    cfg = cfg or IBKRConfig()
    from ib_async import IB, Future, util
    ib = IB()
    ib.connect(cfg.host, cfg.port, clientId=cfg.client_id, timeout=10)
    try:
        ng = Future(symbol="NG", exchange="NYMEX", currency="USD")
        details = ib.reqContractDetails(ng)
        contracts = sorted(
            (d.contract for d in details),
            key=lambda c: c.lastTradeDateOrContractMonth,
        )
        front, second = contracts[0], contracts[1]
        bars = ib.reqHistoricalData(
            front, endDateTime="", durationStr="30 D",
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
        )
        return {
            "connected": ib.isConnected(),
            "front": front.localSymbol,
            "second": second.localSymbol,
            "bars": util.df(bars),
        }
    finally:
        ib.disconnect()


def _fred_degree_days(start: str) -> pd.DataFrame:
    """Monthly US population-weighted HDD/CDD from FRED, forward-filled to weekly."""
    def _fetch(series_id: str) -> pd.Series:
        url = f"{FRED_BASE}/series/observations"
        params = {
            "series_id": series_id,
            "observation_start": start,
            "api_key": FRED_KEY,
            "file_type": "json",
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        s = pd.Series(
            {row["date"]: pd.to_numeric(row["value"], errors="coerce")
             for row in r.json()["observations"]},
            name=series_id,
        )
        s.index = pd.to_datetime(s.index)
        return s.dropna()

    monthly = pd.DataFrame({"hdd": _fetch("EMNHDD"), "cdd": _fetch("EMNCDD")}).sort_index()
    weekly_idx = pd.date_range(start=monthly.index[0], end=dt.date.today(), freq="W-FRI")
    return monthly.reindex(weekly_idx, method="ffill").dropna()


def _weekly_index(start):
    end = dt.date.today()
    return pd.date_range(start=start, end=end, freq="W-FRI")


def _synthetic_storage(series_key, start):
    rng = np.random.default_rng(abs(hash(series_key)) % (2**32))
    idx = _weekly_index(start)
    n = len(idx)
    t = np.arange(n)
    season = 250 + 180 * np.sin(2 * np.pi * (t / 52.0) - 1.4)
    scale = {"salt_south_central": 0.35, "nonsalt_south_central": 1.0,
             "lower48_total": 8.0}.get(series_key, 1.0)
    level = scale * (season + rng.normal(0, 8, n).cumsum() * 0.2)
    level = np.clip(level, 5, None)
    df = pd.DataFrame({"level_bcf": level}, index=idx)
    df["net_flow_bcf"] = df["level_bcf"].diff()
    return df.dropna()


def _synthetic_price(contract, start):
    rng = np.random.default_rng(1000 + contract)
    idx = pd.date_range(start=start, end=dt.date.today(), freq="B")
    n = len(idx)
    base = 3.0 + 0.4 * np.sin(2 * np.pi * np.arange(n) / 252.0)
    shock = rng.normal(0, 0.03, n).cumsum() * 0.1
    settle = base + shock + 0.05 * (contract - 1)
    return pd.DataFrame({f"settle_c{contract}": np.clip(settle, 0.5, None)}, index=idx)


def _synthetic_degree_days(start):
    idx = _weekly_index(start)
    t = np.arange(len(idx))
    hdd = np.clip(120 * np.cos(2 * np.pi * t / 52.0) + 60, 0, None)
    cdd = np.clip(80 * np.cos(2 * np.pi * t / 52.0 + np.pi) + 30, 0, None)
    return pd.DataFrame({"hdd": hdd, "cdd": cdd}, index=idx)


def _synthetic_basis(hub, start):
    rng = np.random.default_rng(7 if hub == "waha" else 9)
    idx = _weekly_index(start)
    n = len(idx)
    level = rng.normal(0, 0.4, n).cumsum() * 0.1
    return pd.DataFrame({f"{hub}_basis": level}, index=idx)


def run_self_test():
    print("=" * 64)
    print("CONNECTIVITY SELF-TEST", dt.datetime.now().isoformat(timespec="seconds"))
    print("=" * 64)

    print("\n[1] EIA storage (salt south central)")
    have_key = bool(EIA_KEY)
    print(f"    EIA_API_KEY present: {have_key}  (set env EIA_API_KEY for live)")
    df = eia_storage("salt_south_central", synthetic=not have_key)
    print(f"    rows={len(df)}  last={df.index[-1].date()}  "
          f"level={df['level_bcf'].iloc[-1]:.1f} Bcf  "
          f"net_flow={df['net_flow_bcf'].iloc[-1]:+.1f} Bcf"
          f"   [{'LIVE' if have_key else 'SYNTHETIC'}]")

    print("\n[2] EIA Henry Hub futures C1 / C2")
    c1 = eia_henryhub_futures(1, synthetic=not have_key)
    c2 = eia_henryhub_futures(2, synthetic=not have_key)
    print(f"    C1 last={c1.iloc[-1, 0]:.3f}  C2 last={c2.iloc[-1, 0]:.3f}  "
          f"calendar spread={c1.iloc[-1,0]-c2.iloc[-1,0]:+.3f} $/MMBtu")

    print("\n[3] Degree days — FRED live or synthetic seasonal")
    have_fred = bool(FRED_KEY)
    print(f"    FRED_API_KEY present: {have_fred}  (set env FRED_API_KEY for live)")
    print(f"    NOAA_TOKEN present:   {bool(NOAA_TOKEN)}  (available for future regional data)")
    dd = noaa_degree_days(start="2017-01-01")
    print(f"    rows={len(dd)}  last HDD={dd['hdd'].iloc[-1]:.0f}  "
          f"CDD={dd['cdd'].iloc[-1]:.0f}  [{'LIVE (FRED)' if have_fred else 'SYNTHETIC'}]")

    print("\n[4] Regional basis (synthetic)")
    wb = regional_basis("waha")
    print(f"    rows={len(wb)}  last waha_basis={wb.iloc[-1,0]:+.3f}")

    print("\n[5] IBKR (ib_async) — paper gateway")
    try:
        import ib_async  # noqa
        print("    ib_async importable. To run the live smoke test, start IB")
        print("    Gateway in PAPER mode and call ibkr_smoke_test().")
    except ImportError:
        print("    ib_async NOT installed. `pip install ib_async` when ready to")
        print("    paper trade. (Use ib_async, not the unmaintained ib_insync.)")
    print("\nSELF-TEST COMPLETE\n")


if __name__ == "__main__":
    run_self_test()
