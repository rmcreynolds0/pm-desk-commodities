#!/usr/bin/env python3
"""
xsec_robustness.py — stress the Pivot A result before believing it.
===================================================================

The ladder produced a monotonic, chance-beating result (Sharpe 0.08 -> 0.24 ->
0.36 -> 0.61). Two earlier findings in this project were artifacts, so the
result is stressed on the three axes most likely to break it:

  1. COST SENSITIVITY. A factor that only works at low cost is not a factor.
     We re-run at 1x, 3x and 5x the baseline 10bps-per-unit-turnover.
  2. SUB-PERIOD STABILITY. Published factors frequently decay after
     publication (basis-momentum: Boons & Prado, 2019). If the edge lives
     entirely pre-2019, that matters enormously for deploying it in 2026.
  3. IS THE NULL HANDICAPPED BY COSTS? The random agent turns over ~4x more
     than the signal agents, so some of its underperformance is churn rather
     than bad predictions. We re-run the null at ZERO cost to separate the two.

Usage:
    python scripts/xsec_robustness.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from storage_stress.agents import xsec  # noqa: E402
from storage_stress.data import commodities as CM  # noqa: E402
from storage_stress.data import connectivity as C  # noqa: E402

LADDER = ["agent_0", "agent_1", "agent_2", "agent_3"]
LABELS = {"agent_0": "random", "agent_1": "carry",
          "agent_2": "carry+mom", "agent_3": "carry+mom+bmom"}


def build_panel():
    con = C._r2_duckdb()
    try:
        chain = CM.load_chain(con)
        px = CM.load_settlements(con, chain, "2000-01-01")
    finally:
        con.close()
    panel = CM.build_panel(px)
    panel = CM.add_factors(panel, px)
    return panel.dropna(subset=["front_ret"])


def run(panel, agent, cost, seed=0, date_filter=None):
    p = panel if date_filter is None else panel[date_filter(panel["date"])]
    sc = xsec.score(p, agent, seed=seed)
    w = xsec.target_weights(sc)
    res = xsec.simulate_xsec(p, w, cost_per_turnover=cost)
    return xsec.perf_stats(res)


def main() -> None:
    print("building panel...")
    panel = build_panel()
    print(f"panel: {len(panel):,} rows, "
          f"{panel['date'].min().date()} -> {panel['date'].max().date()}\n")

    print("=" * 74)
    print("1. COST SENSITIVITY (Sharpe at multiples of 10bps/unit turnover)")
    print("=" * 74)
    rows = []
    for agent in LADDER:
        r = {"agent": agent, "signal": LABELS[agent]}
        for mult in [1, 3, 5]:
            r[f"{mult}x"] = run(panel, agent, 0.0010 * mult)["sharpe"]
        rows.append(r)
    print(pd.DataFrame(rows).round(3).to_string(index=False))

    print("\n" + "=" * 74)
    print("2. SUB-PERIOD STABILITY (Sharpe by era)")
    print("=" * 74)
    eras = {
        "2000-2008": lambda d: (d >= "2000-01-01") & (d < "2009-01-01"),
        "2009-2016": lambda d: (d >= "2009-01-01") & (d < "2017-01-01"),
        "2017-2026": lambda d: (d >= "2017-01-01"),
    }
    rows = []
    for agent in LADDER:
        r = {"agent": agent, "signal": LABELS[agent]}
        for name, f in eras.items():
            r[name] = run(panel, agent, 0.0010, date_filter=f)["sharpe"]
        rows.append(r)
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    print("\n  (basis-momentum was published in 2019 — check the last column)")

    print("\n" + "=" * 74)
    print("3. NULL AT ZERO COST — is random's weakness churn or bad prediction?")
    print("=" * 74)
    for cost, label in [(0.0010, "with cost"), (0.0, "ZERO cost")]:
        sharpes = [run(panel, "agent_0", cost, seed=s)["sharpe"]
                   for s in range(30)]
        a = np.array(sharpes)
        print(f"  random, {label:<10}: mean={a.mean():+.3f}  sd={a.std():.3f}  "
              f"p95={np.percentile(a,95):+.3f}")
    print(f"  agent_3, ZERO cost : sharpe="
          f"{run(panel, 'agent_3', 0.0)['sharpe']:+.3f}")


if __name__ == "__main__":
    main()
