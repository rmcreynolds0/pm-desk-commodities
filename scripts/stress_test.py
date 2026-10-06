#!/usr/bin/env python3
"""
stress_test.py — does the natural-gas DSI method earn a place in the blend?

    python scripts/stress_test.py [n_null_seeds]

WHAT IS BEING TESTED
--------------------
`data.commodities.add_stress` ports the Deliverability Stress Index pipeline
from the retired natural-gas strategy onto every market in the cross-section:
curve-state extremity -> convex transform -> smoothing -> deseasonalise ->
residualise against carry.

The gas strategy was retired because its HYPOTHESIS was falsified. Its METHOD
was never shown to be wrong, and this asks whether the method survives being
pointed at a different question.

THE DECISION RULE — PRE-COMMITTED, WRITTEN BEFORE THE FIRST RUN
---------------------------------------------------------------
This project has already produced two results that were artifacts, and one
earlier analysis was reported without being run. So the rule is fixed here, in
the file, before any number is looked at:

  PROMOTE stress into the live single-agent blend only if ALL of:

    1. It beats chance.        The blend containing stress lands above the
                               95th percentile of the null distribution.
    2. It adds something.      Sharpe of (carry+mom+basis_mom+stress) exceeds
                               Sharpe of (carry+mom+basis_mom) by >= 0.03.
                               Below that it is noise dressed as improvement.
    3. It is not cost-fragile. The improvement in (2) survives 3x costs.
    4. stress alone is not     If `stress_only` beats the full blend, the
       the whole story.        residualisation failed and stress is just
                               re-encoding something already present.

  If the rule fails, stress is REJECTED and the live agent ships as
  carry+mom+basis_mom. Reporting a negative result is the job, not a setback.

Iterating on k, the smoothing span or the residualisation window until the
rule passes would convert this from a test into a search. One run, one answer.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from storage_stress.agents import xsec                      # noqa: E402
from storage_stress.data import commodities as CM           # noqa: E402
from storage_stress.data import connectivity as C           # noqa: E402

# The incumbent the challenger has to beat.
INCUMBENT = "agent_3"
CHALLENGER = "all_four"
MIN_EDGE = 0.03

VARIANTS = {
    "agent_1":           "carry",
    "agent_2":           "carry + mom",
    "agent_3":           "carry + mom + basis_mom   (incumbent)",
    "stress_only":       "stress alone",
    "carry_mom_stress":  "carry + mom + stress      (the brief)",
    "all_four":          "carry + mom + basis_mom + stress",
}


def run(panel: pd.DataFrame, agent: str, cost: float = 0.0010,
        seed: int = 0) -> dict:
    sc = xsec.score(panel, agent, seed=seed)
    w = xsec.target_weights(sc)
    res = xsec.simulate_xsec(panel, w, cost_per_turnover=cost)
    st = xsec.perf_stats(res)
    st["agent"] = agent
    return st


def main(n_null: int = 200) -> int:
    print("=" * 78)
    print("STRESS FACTOR TEST — does the gas-strategy method generalise?")
    print("=" * 78)

    print("\n[1/5] loading settlements")
    con = C._r2_duckdb()
    try:
        chain = CM.load_chain(con)
        px = CM.load_settlements(con, chain, "2000-01-01")
    finally:
        con.close()

    print("[2/5] building factors (roll-safe) + stress")
    panel = CM.add_factors(CM.build_panel(px), px)
    panel = CM.add_stress(panel)
    panel = panel.dropna(subset=["front_ret"])
    print(f"      {len(panel):,} rows, "
          f"stress coverage {panel['stress'].notna().mean():.0%}")

    print(f"\n[3/5] variants (baseline costs)")
    rows = []
    for agent, label in VARIANTS.items():
        st = run(panel, agent)
        st["signal"] = label
        rows.append(st)
    stats = pd.DataFrame(rows)[
        ["agent", "signal", "ann_ret%", "ann_vol%", "sharpe", "max_dd%", "n_days"]]
    print(stats.round(3).to_string(index=False))

    sharpe = dict(zip(stats["agent"], stats["sharpe"]))

    print(f"\n[4/5] null distribution — {n_null} random agents")
    null = np.array([run(panel, "agent_0", seed=s)["sharpe"]
                     for s in range(n_null)])
    p95 = np.percentile(null, 95)
    print(f"      sharpe: mean={null.mean():+.3f}  sd={null.std():.3f}  "
          f"p5={np.percentile(null,5):+.3f}  p95={p95:+.3f}")
    for agent in (INCUMBENT, CHALLENGER, "carry_mom_stress"):
        pct = (null < sharpe[agent]).mean() * 100
        print(f"      {agent:<18} sharpe {sharpe[agent]:+.3f}  "
              f"-> {pct:5.1f}th pctile of chance")

    print("\n[5/5] cost robustness (3x)")
    s3 = {a: run(panel, a, cost=0.0030)["sharpe"]
          for a in (INCUMBENT, CHALLENGER)}
    for a, v in s3.items():
        print(f"      {a:<18} {sharpe[a]:+.3f} -> {v:+.3f} at 3x cost")

    # ---------------- the pre-committed rule ----------------
    edge = sharpe[CHALLENGER] - sharpe[INCUMBENT]
    edge3 = s3[CHALLENGER] - s3[INCUMBENT]
    c1 = sharpe[CHALLENGER] > p95
    c2 = edge >= MIN_EDGE
    c3 = edge3 >= MIN_EDGE
    c4 = sharpe["stress_only"] < sharpe[CHALLENGER]

    print("\n" + "=" * 78)
    print("DECISION — against the rule written before this was run")
    print("=" * 78)
    print(f"  1. beats chance (> p95 {p95:+.3f})        "
          f"{sharpe[CHALLENGER]:+.3f}   {'PASS' if c1 else 'FAIL'}")
    print(f"  2. adds >= {MIN_EDGE:.2f} Sharpe over incumbent   "
          f"{edge:+.3f}   {'PASS' if c2 else 'FAIL'}")
    print(f"  3. edge survives 3x costs                {edge3:+.3f}   "
          f"{'PASS' if c3 else 'FAIL'}")
    print(f"  4. stress alone is not the whole story    "
          f"{sharpe['stress_only']:+.3f}   {'PASS' if c4 else 'FAIL'}")

    promoted = c1 and c2 and c3 and c4
    print()
    if promoted:
        print("  VERDICT: PROMOTE — stress joins the live blend.")
    else:
        print("  VERDICT: REJECT — the live agent ships as carry+mom+basis_mom.")
        print("  The gas method does not add measurable information here.")
        print("  This is a result, not a failure: it is recorded and kept.")

    out = ROOT / "data" / "processed" / "stress_test.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    stats.assign(null_p95=p95, promoted=promoted).to_csv(out, index=False)
    print(f"\n  written: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 200))
