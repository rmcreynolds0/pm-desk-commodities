#!/usr/bin/env python3
"""
xsec_stage2.py — the two questions that decide whether Pivot A is deployable.
============================================================================

(1) POST-PUBLICATION DECAY. Carry has already decayed to NEGATIVE post-2017.
    Basis-momentum -- which is carrying the recent performance -- was published
    by Boons & Prado in 2019. If its edge lives only BEFORE publication, the
    strategy is a historical artifact, not a live opportunity. We therefore cut
    the sample finely around 2019 and check the most recent years on their own.

(2) VOLATILITY TARGETING. The unscaled book ran at ~22% vol with a -36%
    drawdown. Scaling to a constant risk budget is standard practice; we test
    whether it improves risk-adjusted return, and whether the improvement
    survives costs.

Both are run against the calibrated null so "better" always means better
THAN CHANCE, not merely better than before.

Usage:
    python scripts/xsec_stage2.py
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
    return CM.add_factors(panel, px).dropna(subset=["front_ret"])


def run(panel, agent, seed=0, cost=0.0010, vol_target=None, since=None,
        until=None):
    p = panel
    if since is not None:
        p = p[p["date"] >= since]
    if until is not None:
        p = p[p["date"] < until]
    sc = xsec.score(p, agent, seed=seed)
    w = xsec.target_weights(sc)
    res = xsec.simulate_xsec(p, w, cost_per_turnover=cost,
                             vol_target=vol_target)
    return xsec.perf_stats(res), res


def main() -> None:
    print("building panel...")
    panel = build_panel()
    print(f"panel: {len(panel):,} rows, {panel['date'].min().date()} -> "
          f"{panel['date'].max().date()}\n")

    print("=" * 76)
    print("1. POST-PUBLICATION DECAY  (basis-momentum published 2019)")
    print("=" * 76)
    windows = {
        "pre-2019 (2000-2018)": ("2000-01-01", "2019-01-01"),
        "post-2019 (2019-2026)": ("2019-01-01", None),
        "last 5y (2021-2026)": ("2021-01-01", None),
        "last 3y (2023-2026)": ("2023-01-01", None),
    }
    rows = []
    for agent in LADDER:
        r = {"agent": agent, "signal": LABELS[agent]}
        for name, (s, u) in windows.items():
            r[name] = run(panel, agent, since=s, until=u)[0]["sharpe"]
        rows.append(r)
    print(pd.DataFrame(rows).round(3).to_string(index=False))

    print("\n  null distribution, post-2019 (30 random agents):")
    ns = np.array([run(panel, "agent_0", seed=s, since="2019-01-01")[0]["sharpe"]
                   for s in range(30)])
    print(f"    mean={ns.mean():+.3f}  sd={ns.std():.3f}  "
          f"p95={np.percentile(ns,95):+.3f}")
    a3_post = run(panel, "agent_3", since="2019-01-01")[0]["sharpe"]
    print(f"    agent_3 post-2019 = {a3_post:+.3f}  -> "
          f"{(ns < a3_post).mean()*100:.0f}th pctile of chance")

    print("\n" + "=" * 76)
    print("2. VOLATILITY TARGETING (target 10% annualised)")
    print("=" * 76)
    rows = []
    for agent in LADDER:
        base, _ = run(panel, agent)
        vt, res_vt = run(panel, agent, vol_target=0.10)
        rows.append({
            "agent": agent, "signal": LABELS[agent],
            "sharpe_base": base["sharpe"], "sharpe_volT": vt["sharpe"],
            "dd_base%": base["max_dd%"], "dd_volT%": vt["max_dd%"],
            "vol_base%": base["ann_vol%"], "vol_volT%": vt["ann_vol%"],
            "ret_volT%": vt["ann_ret%"],
            "avg_lev": res_vt["leverage"].mean(),
        })
    print(pd.DataFrame(rows).round(3).to_string(index=False))

    print("\n" + "=" * 76)
    print("3. HARDEST TEST: vol-targeted, post-2019, 3x costs")
    print("=" * 76)
    for agent in LADDER:
        st, _ = run(panel, agent, cost=0.0030, vol_target=0.10,
                    since="2019-01-01")
        print(f"  {agent:<9} {LABELS[agent]:<16} sharpe={st['sharpe']:+.3f}  "
              f"ann={st['ann_ret%']:+6.2f}%  maxDD={st['max_dd%']:+7.2f}%")


if __name__ == "__main__":
    main()
