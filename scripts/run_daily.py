#!/usr/bin/env python3
"""
run_daily.py — the scheduled job (Thursday afternoon after the EIA storage print).

Thin orchestration only: it wires together functions from the storage_stress
package. All real logic lives in src/ where it can be tested. This script is
intentionally dumb so the scheduler stays trivial to reason about.

Flow:  load config -> pull data -> compute DSI_clean -> z-score -> regime gate
       -> season check -> size -> (paper) place order / (research) log signal.

Usage:
    python scripts/run_daily.py --mode research      # default: compute + log, no orders
    python scripts/run_daily.py --mode paper          # also route IBKR paper orders
"""
from __future__ import annotations
import argparse
import datetime as dt
from pathlib import Path

import yaml

from storage_stress.data import connectivity as C
from storage_stress.signal import build_dsi, zscore, vol_regime_gate
from storage_stress.agents import agent_dsi


ROOT = Path(__file__).resolve().parents[1]


def load_config():
    with open(ROOT / "config" / "settings.yaml") as f:
        settings = yaml.safe_load(f)
    with open(ROOT / "config" / "instruments.yaml") as f:
        instruments = yaml.safe_load(f)
    return settings, instruments


def main(mode: str):
    settings, instruments = load_config()
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] run_daily mode={mode}")

    salt = C.eia_storage("salt_south_central", start=settings["backtest"]["train_start"])
    c1 = C.eia_henryhub_futures(1)
    c2 = C.eia_henryhub_futures(2)
    waha = C.regional_basis("waha").iloc[:, 0]
    dom = C.regional_basis("domsouth").iloc[:, 0]

    spread = (c1.iloc[:, 0] - c2.iloc[:, 0]).resample("W-FRI").last()
    spread = spread.reindex(salt.index).interpolate().dropna()
    spread_ret = spread.diff()

    dsi = build_dsi(
        salt,
        settings["signal"]["salt_max_rate_bcf_wk"],
        waha, dom,
        k=settings["signal"]["convex_k"],
    )
    side = agent_dsi(dsi["dsi_clean"], spread_ret,
                     entry_z=settings["entry"]["dsi_entry_z"])

    latest = side.index[-1]
    decision = int(side.iloc[-1])
    z = zscore(dsi["dsi_clean"]).iloc[-1]
    gated = bool(vol_regime_gate(spread_ret).reindex(side.index).fillna(False).iloc[-1])

    print(f"  as of {latest.date()}: DSI z={z:+.2f}  vol_gate={'open' if gated else 'closed'}  "
          f"decision={'SHORT spread' if decision<0 else 'LONG spread' if decision>0 else 'flat'}")

    if mode == "paper" and decision != 0:
        print("  [paper] would route IBKR combo order here "
              "(wire src/storage_stress/execution/ + start IB Gateway)")
    else:
        live_dir = ROOT / settings["paths"]["data_live"]
        live_dir.mkdir(parents=True, exist_ok=True)
        log = live_dir / "signal_log.csv"
        header = not log.exists()
        with open(log, "a") as f:
            if header:
                f.write("date,dsi_z,vol_gate,decision\n")
            f.write(f"{latest.date()},{z:.4f},{int(gated)},{decision}\n")
        print(f"  logged signal -> {log}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["research", "paper"], default="research")
    args = ap.parse_args()
    main(args.mode)
