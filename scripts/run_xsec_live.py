#!/usr/bin/env python3
"""
run_xsec_live.py — CLI for the live cross-sectional book.

    python scripts/run_xsec_live.py --job rebalance --dry-run   # plan only
    python scripts/run_xsec_live.py --job rebalance             # trade all
    python scripts/run_xsec_live.py --job rebalance --agents agent_0   # one rung
    python scripts/run_xsec_live.py --job mark                  # daily marks
    python scripts/run_xsec_live.py --job status                # ledger, no broker

`--dry-run` computes and RECORDS every decision but sends no orders. Always
worth running first after any change: it shows exactly which markets round to
zero contracts at the current book size before real orders go out.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def job_status() -> None:
    import yaml
    from storage_stress.execution import xsec_books as B

    with open(ROOT / "config" / "xsec.yaml") as f:
        cfg = yaml.safe_load(f)
    con = B.connect(ROOT / cfg["paths"]["books_db"])

    print(f"{'agent':<10}{'equity':>16}{'realized':>14}{'positions':>11}{'trades':>8}")
    print("-" * 59)
    for agent in cfg["signal"]["agents"]:
        curve = B.equity_curve(con, agent)
        cap = B.capital_of(con, agent) or cfg["capital"]["book_size"]
        equity = curve["equity"].iloc[-1] if len(curve) else cap
        realized = B.realized_pnl(con, agent)
        n_pos = len(B.get_positions(con, agent))
        n_trades = len(B.trade_log(con, agent))
        print(f"{agent:<10}{equity:>16,.0f}{realized:>+14,.0f}"
              f"{n_pos:>11}{n_trades:>8}")
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job", choices=["rebalance", "mark", "status"],
                    required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and record decisions, send no orders")
    ap.add_argument("--agents", nargs="+", metavar="AGENT",
                    help="restrict a rebalance to these agents, e.g. "
                         "--agents agent_0. Lets one rung go live while the "
                         "other paper accounts are still being opened, "
                         "without touching the others' books. "
                         "Default: every agent in the spec.")
    args = ap.parse_args()

    if args.job == "status":
        job_status()
        return 0

    from storage_stress.execution import xsec_engine as E
    try:
        if args.job == "rebalance":
            E.run_rebalance(ROOT, dry_run=args.dry_run, agents=args.agents)
        else:
            E.run_mark(ROOT)
    except ConnectionError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
