#!/usr/bin/env python3
"""
explore_r2_basis.py — probe the QUANTT R2 parquet mirror for gas basis data.
============================================================================

GOAL
----
Find out whether the WRDS R2 mirror carries the regional natural-gas hub
prices we need to build Waha / Dominion South BASIS (hub price minus Henry
Hub), which would replace the SYNTHETIC basis currently feeding agent_dsi's
residualization step (gap #2 in docs/FRAMEWORK.md).

Basis is a PRICE differential ($/MMBtu), not a flow/capacity number — so we
look in the EIA energy-PRICE schema `doe_all` (and, secondarily, the
Datastream commodity/futures schemas) rather than the storage schemas.

CREDENTIALS — never printed by this script
------------------------------------------
Reads these from the environment (put them in .env; .env is gitignored and
is never opened by anyone but the code):

    R2_ACCESS_KEY_ID       R2 access key id
    R2_SECRET_ACCESS_KEY   R2 secret access key
    R2_ENDPOINT            full endpoint URL, e.g.
                           https://<accountid>.r2.cloudflarestorage.com
    R2_BUCKET              optional; defaults to the catalog's bucket name

The script configures DuckDB's S3-compatible client for R2 and only ever
prints DATA (table names, series descriptions, sample rows) — never the keys.

WHAT IT DOES
------------
  1. Confirm connectivity by globbing the doe_all schema's parquet files.
  2. Load `doenames` (series metadata) and search every text column for
     'waha' / 'dominion' / 'henry hub' / 'natural gas'.
  3. For any matching series, pull a sample of `doe` (the value table) so we
     can see units, frequency and date coverage.
  4. Secondary: glob tr_ds_comds / tr_ds_fut so we know what else is available
     if doe_all comes up short.

Run (after keys are in .env):
    python scripts/explore_r2_basis.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_BUCKET = "quantt-historical-market-data"
SEARCH_TERMS = ["waha", "dominion", "dom south", "henry hub", "natural gas"]


def _require_env() -> dict:
    """Collect R2 settings from the environment, failing LOUDLY but WITHOUT
    ever echoing a secret. Only the NAMES of any missing vars are printed."""
    needed = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        print("[ERROR] missing R2 settings in environment/.env:", ", ".join(missing))
        print("        add them to .env (values are never read/printed by me):")
        print("            R2_ACCESS_KEY_ID=...")
        print("            R2_SECRET_ACCESS_KEY=...")
        print("            R2_ENDPOINT=https://<accountid>.r2.cloudflarestorage.com")
        print("            R2_BUCKET=%s   # optional" % DEFAULT_BUCKET)
        sys.exit(2)
    endpoint = os.environ["R2_ENDPOINT"].replace("https://", "").replace("http://", "").rstrip("/")
    return {
        "key_id": os.environ["R2_ACCESS_KEY_ID"],
        "secret": os.environ["R2_SECRET_ACCESS_KEY"],
        "endpoint": endpoint,
        "bucket": os.environ.get("R2_BUCKET", DEFAULT_BUCKET),
    }


def _connect(cfg: dict):
    """Return a DuckDB connection wired to read the R2 bucket over S3.

    R2 is S3-compatible; the crucial non-defaults are region='auto' and
    url_style='path' (R2 does not support virtual-host bucket addressing).
    Secrets are passed to DuckDB in-process; they are never logged.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # Parameterized SET is not supported for these PRAGMA-like settings, so we
    con.execute(f"SET s3_endpoint='{cfg['endpoint']}';")
    con.execute("SET s3_region='auto';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_use_ssl=true;")
    con.execute(f"SET s3_access_key_id='{cfg['key_id']}';")
    con.execute(f"SET s3_secret_access_key='{cfg['secret']}';")
    return con


def _uri(cfg: dict, schema: str, table: str) -> str:
    """s3://<bucket>/wrds/<schema>/<table>.parquet"""
    return f"s3://{cfg['bucket']}/wrds/{schema}/{table}.parquet"


def _glob(con, cfg: dict, schema: str) -> list[str]:
    """List parquet files present under a schema prefix — confirms the schema
    exists in the mirror and shows its table names."""
    pattern = f"s3://{cfg['bucket']}/wrds/{schema}/*.parquet"
    try:
        rows = con.execute(f"SELECT file FROM glob('{pattern}')").fetchall()
        return [r[0] for r in rows]
    except Exception as e:                      # noqa: BLE001
        print(f"    [glob failed for {schema}] {e}")
        return []


def _text_columns(con, uri: str) -> list[str]:
    """Names of VARCHAR columns in a parquet table (the ones worth searching)."""
    info = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{uri}')").fetchall()
    return [name for name, ctype, *_ in info if "VARCHAR" in ctype.upper()]


def probe_doe(con, cfg: dict) -> None:
    """The main event: search doe_all.doenames for the hub series."""
    print("\n=== doe_all (EIA energy prices/production) ===")
    files = _glob(con, cfg, "doe_all")
    if not files:
        print("    doe_all not found in the mirror (or no access). Skipping.")
        return
    print("    tables present:", ", ".join(Path(f).stem for f in files))

    names_uri = _uri(cfg, "doe_all", "doenames")
    try:
        text_cols = _text_columns(con, names_uri)
    except Exception as e:                      # noqa: BLE001
        print(f"    [cannot read doenames] {e}")
        return
    print("    doenames text columns:", ", ".join(text_cols) or "(none)")

    print("\n    -- doenames sample (first 8 rows) --")
    sample = con.execute(f"SELECT * FROM read_parquet('{names_uri}') LIMIT 8").fetchdf()
    print(sample.to_string(max_colwidth=48))

    if text_cols:
        clauses = []
        for col in text_cols:
            for term in SEARCH_TERMS:
                clauses.append(f'"{col}" ILIKE \'%{term}%\'')
        where = " OR ".join(clauses)
        q = f"SELECT * FROM read_parquet('{names_uri}') WHERE {where}"
        hits = con.execute(q).fetchdf()
        print(f"\n    -- doenames rows matching {SEARCH_TERMS}: {len(hits)} --")
        if len(hits):
            print(hits.to_string(max_colwidth=60))
            _sample_values(con, cfg, hits)
        else:
            print("    NO hub-specific series found in doenames.")


def _sample_values(con, cfg: dict, hits) -> None:
    """For matched series, pull a few rows of the doe value table to reveal
    units / frequency / date coverage. We don't know the exact key column, so
    we try the common ones and fall back to a plain head()."""
    doe_uri = _uri(cfg, "doe_all", "doe")
    doe_cols = [c.lower() for c in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{doe_uri}')").fetchdf()["column_name"]]
    code_col = next((c for c in ["series", "series_code", "code", "id"]
                     if c in doe_cols), None)
    print("\n    -- doe value-table columns:", ", ".join(doe_cols))
    if code_col and code_col in [c.lower() for c in hits.columns]:
        codes = hits[[c for c in hits.columns if c.lower() == code_col][0]].dropna().unique()[:5]
        codes_sql = ",".join(f"'{c}'" for c in codes)
        q = (f"SELECT * FROM read_parquet('{doe_uri}') "
             f"WHERE {code_col} IN ({codes_sql}) LIMIT 20")
        print(f"    -- doe rows for matched codes ({code_col} in first 5 hits) --")
        print(con.execute(q).fetchdf().to_string(max_colwidth=40))
    else:
        print("    (could not auto-detect the join code column; inspect columns above)")


def probe_secondary(con, cfg: dict) -> None:
    """List Datastream commodity/futures schemas as fallbacks for NG prices."""
    for schema in ["tr_ds_comds", "tr_ds_fut"]:
        print(f"\n=== {schema} (Datastream — secondary NG price candidate) ===")
        files = _glob(con, cfg, schema)
        if files:
            print("    tables present:", ", ".join(Path(f).stem for f in files))
        else:
            print("    not found / no access.")


def main() -> int:
    cfg = _require_env()
    print(f"[R2] bucket={cfg['bucket']} endpoint={cfg['endpoint']}  (keys loaded, not shown)")
    con = _connect(cfg)
    probe_doe(con, cfg)
    probe_secondary(con, cfg)
    print("\n[done] If Waha spot/price series appear above, basis = Waha - Henry Hub "
          "can be wired into connectivity.regional_basis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
