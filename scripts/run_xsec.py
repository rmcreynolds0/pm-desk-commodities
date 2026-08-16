#!/usr/bin/env python3
"""
run_xsec.py — PIVOT A: cross-sectional commodity carry/momentum ladder.
======================================================================

Runs the four-rung ladder (random -> carry -> +momentum -> +basis-momentum)
on a ~23-commodity long/short book, and — critically — benchmarks it against a
CALIBRATED NULL DISTRIBUTION of many random agents rather than a single random
path. That distinction is what exposed the NG result as noise.

A rung is only interesting if its return falls OUTSIDE the p5-p95 band of pure
chance. Its own confidence interval is not sufficient evidence.

Usage:
    python scripts/run_xsec.py [n_null_seeds]
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

from storage_stress.agents import xsec  # noqa: E402
from storage_stress.data import commodities as CM  # noqa: E402
from storage_stress.data import connectivity as C  # noqa: E402

LADDER = ["agent_0", "agent_1", "agent_2", "agent_3"]
LABELS = {
    "agent_0": "random (null)",
    "agent_1": "carry",
    "agent_2": "carry + momentum",
    "agent_3": "carry + mom + basis-mom",
}


def main(n_null: int = 100) -> None:
    with open(ROOT / "config" / "settings.yaml") as f:
        settings = yaml.safe_load(f)
    start = "2000-01-01"          # deep history: these factors need decades

    print("=" * 78)
    print("PIVOT A — cross-sectional commodity carry / momentum ladder")
    print("=" * 78)

    print("\n[1/4] loading contract universe")
    con = C._r2_duckdb()
    try:
        chain = CM.load_chain(con)
        print(f"    {len(chain):,} contracts across "
              f"{chain['ticker'].nunique()} commodities")
        px = CM.load_settlements(con, chain, start)
    finally:
        con.close()
    print(f"    {len(px):,} daily settlements, "
          f"{px['date'].min().date()} -> {px['date'].max().date()}")

    print("\n[2/4] building panel + factors (roll-safe returns)")
    panel = CM.build_panel(px)
    panel = CM.add_factors(panel, px)
    panel = panel.dropna(subset=["front_ret"])
    print(f"    panel rows: {len(panel):,}")
    cov = panel.groupby("date")["ticker"].nunique()
    print(f"    commodities per date: median={cov.median():.0f}  "
          f"min={cov.min()}  max={cov.max()}")
    print(f"    factor coverage — carry {panel['carry'].notna().mean():.0%}, "
          f"mom {panel['mom'].notna().mean():.0%}, "
          f"basis_mom {panel['basis_mom'].notna().mean():.0%}")

    print("\n[3/4] running the ladder")
    rows = []
    curves = {}
    for agent in LADDER:
        sc = xsec.score(panel, agent, seed=0)
        w = xsec.target_weights(sc)
        res = xsec.simulate_xsec(panel, w)
        st = xsec.perf_stats(res)
        st["agent"] = agent
        st["signal"] = LABELS[agent]
        st["avg_turnover"] = res["turnover"].mean()
        st["total_cost%"] = res["cost"].sum() * 100
        rows.append(st)
        curves[agent] = res["equity"]

    stats = pd.DataFrame(rows)[
        ["agent", "signal", "ann_ret%", "ann_vol%", "sharpe", "max_dd%",
         "avg_turnover", "total_cost%", "n_days"]]
    print()
    print(stats.round(3).to_string(index=False))

    print(f"\n[4/4] null distribution — {n_null} random agents")
    null_sharpes, null_rets = [], []
    for seed in range(n_null):
        sc = xsec.score(panel, "agent_0", seed=seed)
        w = xsec.target_weights(sc)
        res = xsec.simulate_xsec(panel, w)
        st = xsec.perf_stats(res)
        null_sharpes.append(st["sharpe"])
        null_rets.append(st["ann_ret%"])
    ns, nr = np.array(null_sharpes), np.array(null_rets)
    print(f"    sharpe : mean={ns.mean():+.3f}  sd={ns.std():.3f}  "
          f"p5={np.percentile(ns,5):+.3f}  p95={np.percentile(ns,95):+.3f}")
    print(f"    ann ret: mean={nr.mean():+.2f}%  sd={nr.std():.2f}  "
          f"p5={np.percentile(nr,5):+.2f}%  p95={np.percentile(nr,95):+.2f}%")

    print("\n" + "=" * 78)
    print("VERDICT — does each rung beat CHANCE?")
    print("=" * 78)
    p95_s, p95_r = np.percentile(ns, 95), np.percentile(nr, 95)
    for _, r in stats.iterrows():
        beats = r["sharpe"] > p95_s
        pct = (ns < r["sharpe"]).mean() * 100
        print(f"  {r['agent']:<9} {r['signal']:<26} "
              f"sharpe={r['sharpe']:+.2f}  ann={r['ann_ret%']:+6.2f}%  "
              f"-> {pct:5.1f}th pctile of chance  "
              f"{'** BEATS CHANCE **' if beats else 'within noise'}")

    out_dir = ROOT / settings["paths"]["data_processed"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stats.to_csv(out_dir / "xsec_ladder.csv", index=False)
    pd.DataFrame(curves).to_csv(out_dir / "xsec_equity_curves.csv")
    print(f"\nresults -> {out_dir / 'xsec_ladder.csv'}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100)
