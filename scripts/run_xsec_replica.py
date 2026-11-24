#!/usr/bin/env python3
"""
run_xsec_replica.py — backtest the cross-sectional book by the LIVE rules.
==========================================================================

scripts/run_xsec.py measures the factors (continuous weights, daily
rebalanced, no contract sizes) and reproduces the published research figures.
This script instead replays history through the rules the live engine trades
by -- whole contracts on a fixed $2M book, monthly vol-target leverage,
single-contract momentum, the 25-day contract guard, the 30% skip rule -- with
a clean contract roll. See src/storage_stress/agents/xsec_replica.py for every
rule and the two deliberate departures from the live code.

The window ends at the last month in which all markets still have a usable
front and second contract in the R2 mirror; after that the mirror's chains
thin out market by market and the cross-section would shrink.

Usage:
    python scripts/run_xsec_replica.py [--null N] [--agents live|all|a,b]
                                       [--account-equity USD]

Output: data/processed/backtests/<timestamp>_replica/
    xsec_ladder.csv, xsec_equity_curves.csv   (same layout as run_xsec.py, so
                                               the research dashboard reads it)
    null_distribution.csv, rebalance_log.csv, coverage.csv, run_info.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from storage_stress.agents import xsec  # noqa: E402
from storage_stress.agents import xsec_replica as XR  # noqa: E402
from storage_stress.data import commodities as CM  # noqa: E402
from storage_stress.data import connectivity as C  # noqa: E402
from storage_stress.execution.xsec_engine import live_agents, load_config  # noqa: E402

LABELS = {
    "agent_0": "random (null)",
    "agent_1": "carry",
    "agent_2": "carry + momentum",
    "agent_3": "carry + mom + basis-mom",
}
SPECS_CSV = ROOT / "data" / "processed" / "xsec_universe_check.csv"
RESEARCH_CURVES = ROOT / "data" / "processed" / "xsec_equity_curves.csv"


def _git_commit() -> str:
    """Commit the run was made from, with '-dirty' if the tree had changes."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _parse_agents(arg: str, cfg: dict) -> list[str]:
    declared = list(cfg["signal"]["agents"])
    if arg == "live":
        return live_agents(cfg)
    if arg == "all":
        return declared
    wanted = [a.strip() for a in arg.split(",") if a.strip()]
    unknown = [a for a in wanted if a not in declared]
    if unknown:
        raise SystemExit(f"unknown agent(s) {unknown}; spec defines {declared}")
    return wanted


def _summarise(agent: str, res: XR.SimResult, capital: float) -> dict:
    st = XR.perf(res.daily, capital)
    log = res.log
    traded = log[log["status"] == "traded"] if len(log) else log
    return {
        "agent": agent, "signal": LABELS.get(agent, agent), **st,
        "avg_turnover": (res.daily["traded"] / capital).mean(),
        "total_cost%": res.daily["cost"].sum() / capital * 100,
        "avg_leverage": traded["leverage"].mean() if len(traded) else np.nan,
        "avg_weight_error%": traded["weight_error"].mean() * 100 if len(traded) else np.nan,
        "n_rebalances": len(traded),
        "n_skipped": int((log["status"] != "traded").sum()) if len(log) else 0,
        "n_forced_rolls": res.n_forced_rolls,
        "n_book_capped": int((traded["book"] < capital).sum()) if len(traded) else 0,
        "account_exhausted_on": (str(res.exhausted_on.date())
                                 if res.exhausted_on is not None else ""),
        "final_equity$": float(res.daily["equity"].iloc[-1]),
    }


def _research_reference(start, end) -> pd.DataFrame | None:
    """The committed research curves cut to the replica's window, so the two
    can be compared over identical dates."""
    if not RESEARCH_CURVES.exists():
        return None
    cur = pd.read_csv(RESEARCH_CURVES, index_col=0, parse_dates=True)
    cur = cur.loc[(cur.index >= start) & (cur.index <= end)]
    rows = []
    for a in cur.columns:
        eq = cur[a] / cur[a].iloc[0]
        st = xsec.perf_stats(pd.DataFrame({"ret_net": eq.pct_change().fillna(0.0),
                                           "equity": eq}))
        rows.append({"agent": a, **st})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--null", type=int, default=200, help="random agents for the null")
    ap.add_argument("--agents", default="live",
                    help="'live' (signal.live_agents), 'all', or a comma list")
    ap.add_argument("--account-equity", type=float, default=1_000_000.0,
                    help="starting NetLiq of the agent's paper account")
    args = ap.parse_args()

    cfg = load_config(ROOT)
    agents = _parse_agents(args.agents, cfg)
    universe = list(cfg["universe"])
    capital = float(cfg["capital"]["book_size"])
    specs = XR.load_specs(SPECS_CSV)
    missing = [t for t in universe if t not in specs]
    if missing:
        raise SystemExit(f"no IBKR contract spec for {missing} in {SPECS_CSV}")

    print("=" * 78)
    print("LIVE REPLICA — cross-sectional book under the live engine's rules")
    print("=" * 78)
    t0 = time.time()

    print("\n[1/5] loading contracts and settlements from R2")
    con = C._r2_duckdb()
    try:
        chain = CM.load_chain(con, universe)
        px = CM.load_settlements(con, chain, cfg["signal"]["history_start"])
    finally:
        con.close()
    md = XR.MarketData(px, specs)
    print(f"    {len(chain):,} contracts, {len(px):,} settlements, "
          f"{px['date'].min().date()} -> {px['date'].max().date()}")

    print("\n[2/5] rebuilding the live factor panel at every month-end")
    rebal_all = XR.last_weekdays(cfg["signal"]["history_start"], md.last_date)
    snap = XR.build_snapshots(md, universe, rebal_all,
                              cfg["signal"]["momentum_months"])
    ok = snap[snap["status"] == "ok"]
    coverage = ok.groupby("date").size().reindex(rebal_all, fill_value=0)
    full = coverage[coverage == len(universe)]
    if full.empty:
        raise SystemExit("no month has every market priced; cannot set a window")
    sim_end = full.index.max()
    dropped = coverage[coverage.index > sim_end]
    print(f"    {len(rebal_all)} month-ends; markets with a usable term structure: "
          f"median {coverage.median():.0f}, min {coverage.min()}")
    print(f"    window ends {sim_end.date()} — last month with all {len(universe)} "
          f"markets; {len(dropped)} later month(s) dropped "
          f"({', '.join(f'{d:%Y-%m}:{n}' for d, n in dropped.items())})")

    rebal = [d for d in rebal_all if d <= sim_end]
    cal = pd.bdate_range(rebal[0], sim_end)
    order = {t: i for i, t in enumerate(universe)}
    snaps = {d: g.sort_values("ticker", key=lambda s: s.map(order))
             for d, g in ok[ok["date"] <= sim_end].groupby("date")}
    pb = XR.PriceBook(md, cal)

    print(f"\n[3/5] simulating {', '.join(agents)}  ({cal[0].date()} -> {cal[-1].date()}, "
          f"{len(cal):,} weekday marks)")
    rows, curves, logs = [], {}, []
    for agent in agents:
        res = XR.simulate(cfg["signal"]["agents"][agent], XR.agent_scorer(agent),
                          snaps, md, pb, cfg, args.account_equity)
        rows.append(_summarise(agent, res, capital))
        curves[agent] = res.daily["equity"] / capital
        logs.append(res.log.assign(agent=agent))
    stats = pd.DataFrame(rows)
    print()
    print(stats.round(3).to_string(index=False))

    print(f"\n[4/5] null distribution — {args.null} random agents through the same rules")
    ns, nr, nx = [], [], []
    for s in range(args.null):
        res = XR.simulate([], XR.null_scorer(s), snaps, md, pb, cfg, args.account_equity)
        st = XR.perf(res.daily, capital)
        ns.append(st["sharpe"])
        nr.append(st["ann_ret%"])
        nx.append(res.exhausted_on is not None)
        if (s + 1) % 20 == 0:
            print(f"    {s + 1}/{args.null} done  ({time.time() - t0:,.0f}s elapsed)",
                  flush=True)
    ns, nr = np.array(ns), np.array(nr)
    p95 = float(np.percentile(ns, 95)) if len(ns) else float("nan")
    if len(ns):
        print(f"    sharpe : mean={ns.mean():+.3f}  sd={ns.std():.3f}  "
              f"p5={np.percentile(ns, 5):+.3f}  p95={p95:+.3f}")
        print(f"    ann ret: mean={nr.mean():+.2f}%  p5={np.percentile(nr, 5):+.2f}%  "
              f"p95={np.percentile(nr, 95):+.2f}%")
        print(f"    accounts exhausted (margin call): {sum(nx)}/{len(nx)}")

    print("\n" + "=" * 78)
    print("[5/5] VERDICT, and the research backtest over the same window")
    print("=" * 78)
    ref = _research_reference(cal[0], cal[-1])
    for _, r in stats.iterrows():
        pct = (ns < r["sharpe"]).mean() * 100 if len(ns) else float("nan")
        verdict = ("** BEATS CHANCE **" if r["sharpe"] > p95 else "within noise") \
            if len(ns) else "no null run"
        line = (f"  {r['agent']:<8} replica sharpe={r['sharpe']:+.3f} "
                f"ann={r['ann_ret%']:+6.2f}% maxDD={r['max_dd%']:6.1f}%  "
                f"-> {pct:5.1f}th pctile  {verdict}")
        if ref is not None and r["agent"] in set(ref["agent"]):
            rr = ref.set_index("agent").loc[r["agent"]]
            line += f"\n  {'':<8} research sharpe={rr['sharpe']:+.3f} " \
                    f"ann={rr['ann_ret%']:+6.2f}% maxDD={rr['max_dd%']:6.1f}%  (same dates)"
        print(line)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_replica"
    out_dir = ROOT / "data" / "processed" / "backtests" / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    stats.to_csv(out_dir / "xsec_ladder.csv", index=False)
    pd.DataFrame(curves).to_csv(out_dir / "xsec_equity_curves.csv")
    pd.DataFrame({"seed": range(len(ns)), "sharpe": ns, "ann_ret%": nr,
                  "account_exhausted": nx}).to_csv(
        out_dir / "null_distribution.csv", index=False)
    pd.concat(logs, ignore_index=True).to_csv(out_dir / "rebalance_log.csv", index=False)
    coverage.rename("markets_usable").to_csv(out_dir / "coverage.csv")
    if ref is not None:
        ref.to_csv(out_dir / "research_same_window.csv", index=False)
    meta = {
        "run_id": run_id,
        "kind": "live-replica",
        "agents": agents,
        "n_null_seeds": args.null,
        "data_start": str(cal[0].date()),
        "data_end": str(cal[-1].date()),
        "null_sharpe_p95": p95,
        "null_accounts_exhausted": int(sum(nx)),
        "book_size": capital,
        "account_equity_start": args.account_equity,
        "git_commit": _git_commit(),
        "runtime_s": round(time.time() - t0, 1),
    }
    (out_dir / "run_info.json").write_text(json.dumps(meta, indent=2))
    print(f"\nresults -> {out_dir}")


if __name__ == "__main__":
    main()
    sys.exit(0)
