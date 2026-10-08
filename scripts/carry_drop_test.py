#!/usr/bin/env python3
"""
carry_drop_test.py — PRE-COMMITTED, SINGLE-SHOT test: should carry be dropped?
=============================================================================

MOTIVATION
    Carry's STANDALONE Sharpe has decayed to negative (-0.51 over the last 3
    years), plausibly arbitraged away since commodity index investing scaled up
    post-2005. It is one third of agent_3's blended score, so it may be a drag.

THE RULE, FIXED BEFORE RUNNING (this matters more than the result)
    Compare agent_3 (carry + momentum + basis-momentum) against agent_3_nocarry
    (momentum + basis-momentum only) across FOUR pre-specified conditions:
        1. full sample, baseline cost
        2. post-2019 only
        3. vol-targeted (10%)
        4. post-2019 + 3x costs + vol-targeted   [the deployment-realistic case]

    DECISION: drop carry only if removing it improves Sharpe in at least 3 of
    the 4 conditions. Otherwise keep it.

    This is run ONCE. Iterating variations until a number improves is how
    overfitting happens; the whole point of pre-committing is to make that
    impossible.

Usage:
    python scripts/carry_drop_test.py
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from storage_stress.agents import xsec  # noqa: E402
from storage_stress.data import commodities as CM  # noqa: E402
from storage_stress.data import connectivity as C  # noqa: E402

xsec.AGENT_FACTORS["agent_3_nocarry"] = ["mom", "basis_mom"]

CONDITIONS = {
    "full sample":            dict(cost=0.0010, vol_target=None, since=None),
    "post-2019":              dict(cost=0.0010, vol_target=None, since="2019-01-01"),
    "vol-targeted":           dict(cost=0.0010, vol_target=0.10, since=None),
    "post-2019 + 3x + volT":  dict(cost=0.0030, vol_target=0.10, since="2019-01-01"),
}
DROP_THRESHOLD = 3


def run(panel, agent, cost, vol_target, since):
    p = panel if since is None else panel[panel["date"] >= since]
    sc = xsec.score(p, agent, seed=0)
    w = xsec.target_weights(sc)
    res = xsec.simulate_xsec(p, w, cost_per_turnover=cost, vol_target=vol_target)
    return xsec.perf_stats(res)


def main() -> None:
    print("building panel...")
    con = C._r2_duckdb()
    try:
        chain = CM.load_chain(con)
        px = CM.load_settlements(con, chain, "2000-01-01")
    finally:
        con.close()
    panel = CM.add_factors(CM.build_panel(px), px).dropna(subset=["front_ret"])
    print(f"panel: {len(panel):,} rows\n")

    print("=" * 74)
    print("CARRY-DROP TEST — pre-committed rule: drop only if better in >= 3 of 4")
    print("=" * 74)

    rows = []
    for name, cfg in CONDITIONS.items():
        with_c = run(panel, "agent_3", **cfg)["sharpe"]
        without = run(panel, "agent_3_nocarry", **cfg)["sharpe"]
        rows.append({"condition": name, "with_carry": with_c,
                     "without_carry": without, "delta": without - with_c,
                     "improved": without > with_c})
    df = pd.DataFrame(rows)
    print(df.round(3).to_string(index=False))

    n_better = int(df["improved"].sum())
    print(f"\nimprovements: {n_better} of {len(df)}  (threshold to drop: {DROP_THRESHOLD})")
    print("=" * 74)
    if n_better >= DROP_THRESHOLD:
        print("DECISION: DROP CARRY. The pre-committed threshold was met.")
    else:
        print("DECISION: KEEP CARRY. The threshold was NOT met.")
        print()
        print("Interpretation: standalone Sharpe was the wrong criterion. Carry is")
        print("weakly correlated with momentum and basis-momentum, so it can reduce")
        print("portfolio VARIANCE more than it reduces return - earning its place")
        print("through diversification despite being a poor standalone bet.")
    print("=" * 74)


if __name__ == "__main__":
    main()
