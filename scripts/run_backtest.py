#!/usr/bin/env python3
"""
run_backtest.py — full-history backtest using real EIA data where available.

Flow:  load config -> acquire data (raw snapshot -> cleaned/aligned dataset)
       -> compute DSI -> run Agent Zero / Agent One / Agent DSI through the
       shared simulate() engine -> report Sharpe + bootstrap CI per agent.

Data sources (see docs in src/storage_stress/data/connectivity.py):
  storage   REAL   EIA weekly storage (salt, South Central)
  futures   REAL   EIA Henry Hub futures C1/C2 -- EIA discontinued this series
                    after 2024-04-05, so the real-data window ends there even
                    though storage data is current. That's an upstream data
                    gap, not a bug in this script.
  basis     SYNTHETIC -- no free real feed for Waha / Dom South basis exists
                    yet (see connectivity.regional_basis); DSI_clean is a
                    hybrid of real and synthetic inputs until one is wired in.

Usage:
    python scripts/run_backtest.py
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

# Load .env BEFORE importing the package so credentials (EIA key, R2 keys for
# the real basis feed) are visible to connectivity.py's os.environ lookups.
# Without this the basis silently falls back to the synthetic generator.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from storage_stress.data import connectivity as C
from storage_stress.data import futures as F
from storage_stress.signal import build_dsi
from storage_stress.agents import (
    agent_zero, agent_one, agent_two, agent_dsi, simulate, sharpe,
    bootstrap_sharpe_ci,
)

ROOT = Path(__file__).resolve().parents[1]


def load_config():
    with open(ROOT / "config" / "settings.yaml") as f:
        settings = yaml.safe_load(f)
    with open(ROOT / "config" / "instruments.yaml") as f:
        instruments = yaml.safe_load(f)
    return settings, instruments


def acquire_and_clean(settings):
    """Pull raw data, snapshot it under data/raw/, then build the aligned,
    NaN-free dataset the signal/backtest stages consume, saved to
    data/processed/. raw/ is never edited downstream, per repo convention."""
    raw_dir = ROOT / settings["paths"]["data_raw"]
    processed_dir = ROOT / settings["paths"]["data_processed"]
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    start = settings["backtest"]["train_start"]

    print("[1/3] acquiring data")
    salt = C.eia_storage("salt_south_central", start=start, synthetic=False)

    # THE TRADED INSTRUMENT: the seasonal calendar spread (front vs next
    # January in injection season, front vs next April in withdrawal), built
    # from individual NYMEX contract settlements and using the SAME
    # front/deferred month logic as the live engine (execution/contracts.py).
    #
    # Previously this used EIA's RNGC1 - RNGC2 (adjacent months) because EIA
    # only publishes 4 contracts out, while the seasonal deferred leg can be
    # 9 months out. That made the backtest measure a DIFFERENT, far less
    # volatile instrument than the live books trade. See docs/FRAMEWORK.md.
    r2 = C._r2_duckdb()
    try:
        seasonal = F.ng_seasonal_spread(r2, start=start)
    finally:
        r2.close()
    # Basis: REAL Datastream regional hub quotes via the R2 mirror when R2
    # credentials are present, else the seeded synthetic fallback. See
    # connectivity.regional_basis for the series mapping and caveats.
    waha = C.regional_basis("waha", start=start)
    dom = C.regional_basis("domsouth", start=start)
    print(f"    storage (REAL)   {len(salt)} rows, {salt.index[0].date()} -> {salt.index[-1].date()}")
    print(f"    seasonal spread (REAL) {len(seasonal)} rows, "
          f"{seasonal.index[0].date()} -> {seasonal.index[-1].date()}")
    print(f"      def: {seasonal.attrs.get('definition','-')}")
    print(f"      from {seasonal.attrs.get('n_contracts','?')} NYMEX NG contracts")
    print(f"    basis waha     [{waha.attrs.get('source','?')}] {len(waha)} rows"
          f"  ({waha.attrs.get('series','-')})"
          f"  clipped={waha.attrs.get('n_clipped', 0)}")
    print(f"    basis domsouth [{dom.attrs.get('source','?')}] {len(dom)} rows"
          f"  ({dom.attrs.get('series','-')})"
          f"  clipped={dom.attrs.get('n_clipped', 0)}")

    salt.to_csv(raw_dir / "eia_storage_salt_south_central.csv")
    # Full audit trail: which contracts produced each spread observation.
    seasonal.to_csv(raw_dir / "ng_seasonal_spread_contracts.csv")
    waha.to_csv(raw_dir / "basis_waha.csv")
    dom.to_csv(raw_dir / "basis_domsouth.csv")

    print("[2/3] cleaning / aligning")
    # Weekly (Friday) sampling of the seasonal spread, then aligned onto the
    # storage index. limit_area="inside" fills only INTERIOR gaps; plain
    # .interpolate() would flat-extrapolate trailing NaNs, silently carrying
    # the last real print forward past the end of the price data.
    spread = seasonal["spread"].resample("W-FRI").last()
    spread = spread.reindex(salt.index).interpolate(limit_area="inside").dropna()
    spread_ret = spread.diff()
    spread_vol = spread_ret.rolling(30).std()

    if spread.index[-1] < salt.index[-1]:
        print(f"    [NOTE] real futures data ends {spread.index[-1].date()}; "
              f"storage data continues to {salt.index[-1].date()}. Backtest "
              f"window is truncated to where both series overlap.")

    dataset = pd.DataFrame({
        "level_bcf": salt["level_bcf"],
        "net_flow_bcf": salt["net_flow_bcf"],
        "spread": spread,
        "spread_ret": spread_ret,
        "spread_vol_30w": spread_vol,
        # Basis is daily; resample to the weekly grid the dataset uses.
        "waha_basis": waha.iloc[:, 0].resample("W-FRI").last(),
        "domsouth_basis": dom.iloc[:, 0].resample("W-FRI").last(),
    }).dropna(subset=["spread"])
    dataset.to_csv(processed_dir / "backtest_dataset.csv")
    print(f"    cleaned dataset: {len(dataset)} rows, "
          f"{dataset.index[0].date()} -> {dataset.index[-1].date()}  "
          f"-> {processed_dir / 'backtest_dataset.csv'}")

    return salt, waha.iloc[:, 0], dom.iloc[:, 0], spread, spread_ret, spread_vol


def max_drawdown_pct(equity: pd.Series) -> float:
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return dd.min()


def main():
    settings, _ = load_config()
    salt, waha, dom, spread, spread_ret, spread_vol = acquire_and_clean(settings)

    print("[3/3] signal + backtest (basic system parameters from config/settings.yaml)")
    dsi = build_dsi(
        salt,
        settings["signal"]["salt_max_rate_bcf_wk"],
        waha, dom,
        k=settings["signal"]["convex_k"],
    )

    # The four-rung information ladder (same order as the live books, so
    # backtest and live results are directly comparable):
    #   zero -> one  : adds real storage data
    #   one  -> two  : adds utilization/convexity/smoothing/deseasonalization
    #   two  -> dsi  : adds basis residualization + vol regime gate
    agents = {
        "agent_zero": agent_zero(dsi.index, seed=0, trade_every=1),
        "agent_one": agent_one(salt["net_flow_bcf"].reindex(dsi.index),
                               thresh=settings["entry"]["agent_one_z"]),
        "agent_two": agent_two(dsi["dsi_raw"],
                               thresh=settings["entry"]["agent_one_z"]),
        "agent_dsi": agent_dsi(dsi["dsi_clean"], spread_ret,
                              entry_z=settings["entry"]["dsi_entry_z"]),
    }

    sizing = settings["sizing"]
    costs = settings["costs"]
    entry = settings["entry"]

    print()
    header = f"{'agent':<12}{'trades':>8}{'final_equity':>16}{'total_ret%':>12}{'max_dd%':>10}{'sharpe':>9}{'ci_lo':>8}{'ci_hi':>8}"
    print(header)
    print("-" * len(header))

    for name, sig in agents.items():
        res = simulate(
            spread, sig, spread_vol.reindex(dsi.index),
            capital=sizing["capital"],
            risk_per_sd=sizing["risk_per_sd"],
            max_hold_days=entry["max_hold_days"],
            slippage_ticks_per_leg=costs["slippage_ticks_per_leg"],
            tick_value=costs["tick_value"],
            exit_frac=entry["exit_revert_frac"],
        )
        ret = res["equity"].pct_change().dropna()
        sr = sharpe(ret)
        _, (lo, hi) = bootstrap_sharpe_ci(ret, n_boot=2000)
        n_trades = len(res.attrs["trades"])
        final_equity = res["equity"].iloc[-1]
        total_ret = (final_equity / sizing["capital"] - 1) * 100
        mdd = max_drawdown_pct(res["equity"]) * 100

        print(f"{name:<12}{n_trades:>8}{final_equity:>16,.0f}{total_ret:>12.2f}{mdd:>10.2f}{sr:>9.2f}{lo:>8.2f}{hi:>8.2f}")

        processed_dir = ROOT / settings["paths"]["data_processed"]
        res[["spread", "equity", "pnl", "side"]].to_csv(processed_dir / f"equity_{name}.csv")

    print(f"\nequity curves + trade logs -> {ROOT / settings['paths']['data_processed']}")


if __name__ == "__main__":
    main()
