#!/usr/bin/env python3
"""
null_distribution.py — calibrate the benchmark by running MANY random agents.
============================================================================

WHY THIS IS NECESSARY
---------------------
`agent_zero` is the null benchmark, but a single seed produces a single random
PATH, not a distribution. Judging a signal agent against one coin-flip path is
like judging a coin as biased after one sequence of flips.

This script runs agent_zero across many seeds on each instrument and reports
the resulting distribution of outcomes. That gives the only number that makes
the other agents interpretable:

    what range of returns does PURE CHANCE produce here?

A signal agent is only interesting if it lands outside that range. If a signal
agent's return sits inside the middle of the null distribution, it has
demonstrated nothing, regardless of its own confidence interval.

Usage:
    python scripts/null_distribution.py [n_seeds]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from storage_stress.agents import agent_zero, simulate, sharpe  # noqa: E402
from storage_stress.data import connectivity as C  # noqa: E402
from storage_stress.data import futures as F  # noqa: E402
from storage_stress.signal import build_dsi  # noqa: E402

INSTRUMENTS = ["prompt", "seasonal", "mar_apr"]


def main(n_seeds: int = 200) -> None:
    with open(ROOT / "config" / "settings.yaml") as f:
        settings = yaml.safe_load(f)
    start = settings["backtest"]["train_start"]
    sizing, costs, entry = settings["sizing"], settings["costs"], settings["entry"]

    print(f"Running {n_seeds} random agents per instrument...\n")

    salt = C.eia_storage("salt_south_central", start=start, synthetic=False)
    waha = C.regional_basis("waha", start=start)
    dom = C.regional_basis("domsouth", start=start)
    dsi = build_dsi(salt, settings["signal"]["salt_max_rate_bcf_wk"],
                    waha.iloc[:, 0].resample("W-FRI").last(),
                    dom.iloc[:, 0].resample("W-FRI").last(),
                    k=settings["signal"]["convex_k"])

    con = C._r2_duckdb()
    try:
        spreads = {i: F.ng_spread(con, i, start=start) for i in INSTRUMENTS}
    finally:
        con.close()

    summary = []
    for inst in INSTRUMENTS:
        sd = spreads[inst]
        spread = sd["spread"].resample("W-FRI").last()
        spread = spread.reindex(salt.index).interpolate(
            limit_area="inside").dropna()
        roll_w = (sd["roll"].resample("W-FRI").max()
                  .reindex(spread.index).fillna(False).astype(bool))
        spread_ret = spread.diff().mask(roll_w, 0.0)
        vol = spread_ret.rolling(30).std()

        rets, sharpes = [], []
        for seed in range(n_seeds):
            sig = agent_zero(dsi.index, seed=seed, trade_every=1)
            res = simulate(spread, sig, vol.reindex(dsi.index),
                           capital=sizing["capital"],
                           risk_per_sd=sizing["risk_per_sd"],
                           max_hold_days=entry["max_hold_days"],
                           slippage_ticks_per_leg=costs["slippage_ticks_per_leg"],
                           tick_value=costs["tick_value"],
                           exit_frac=entry["exit_revert_frac"],
                           roll_flag=roll_w)
            final = res["equity"].iloc[-1]
            rets.append((final / sizing["capital"] - 1) * 100)
            sharpes.append(sharpe(res["equity"].pct_change().dropna()))

        r = np.array(rets)
        s = np.array(sharpes)
        print(f"=== {inst} — {n_seeds} random agents ===")
        print(f"  total return %: mean={r.mean():+7.2f}  sd={r.std():6.2f}")
        print(f"     percentiles : p5={np.percentile(r,5):+7.2f}  "
              f"p50={np.percentile(r,50):+7.2f}  p95={np.percentile(r,95):+7.2f}"
              f"  min={r.min():+7.2f}  max={r.max():+7.2f}")
        print(f"  sharpe        : mean={s.mean():+.3f}  sd={s.std():.3f}  "
              f"p95={np.percentile(s,95):+.3f}")
        print(f"  share of random agents PROFITABLE: {(r > 0).mean():.1%}")
        # Where does the single seed=0 path (used elsewhere) actually sit?
        pct_seed0 = (r < r[0]).mean() * 100
        print(f"  seed=0 (the one used in the comparison): {r[0]:+.2f}%  "
              f"-> {pct_seed0:.0f}th percentile of chance\n")
        summary.append({"instrument": inst, "mean": r.mean(), "sd": r.std(),
                        "p5": np.percentile(r, 5), "p95": np.percentile(r, 95),
                        "seed0": r[0], "seed0_pctile": pct_seed0,
                        "pct_profitable": (r > 0).mean() * 100})

    out = ROOT / settings["paths"]["data_processed"] / "null_distribution.csv"
    pd.DataFrame(summary).to_csv(out, index=False)
    print(f"summary -> {out}")
    print("\nHOW TO USE THIS: a signal agent is only interesting if its return "
          "falls OUTSIDE the p5-p95 band of pure chance shown above.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
