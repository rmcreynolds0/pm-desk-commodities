#!/usr/bin/env python3
"""
run_agents.py — thin CLI wrapper over the live engine (repo convention:
scripts stay dumb; all logic lives in src/ where it is testable).

Jobs:
    python scripts/run_agents.py --job decide   # weekly: signals -> orders
    python scripts/run_agents.py --job mark     # daily: mark + exits + equity
    python scripts/run_agents.py --job status   # print each book's state (no broker)

`decide` and `mark` need IB Gateway running (paper). `status` only reads the
ledger, so it works anywhere — handy for a quick check from the terminal
without opening the dashboard.

Every run appends stdout to data/live/logs/<job>-<date>.log via the shell
(see scheduler.py / docker-compose) — the script itself just prints.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

# Load .env BEFORE importing the engine so EIA/IBKR settings from the file
# are visible to connectivity.py's os.environ reads.
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]


def job_status() -> None:
    """Print a compact per-agent state summary straight from the ledger."""
    import yaml

    from storage_stress.execution import books

    with open(ROOT / "config" / "live.yaml") as f:
        live = yaml.safe_load(f)
    con = books.connect(ROOT / live["paths"]["books_db"])

    print(f"{'agent':<12}{'equity':>12}{'realized':>12}{'position':>22}{'trades':>8}")
    print("-" * 66)
    for name, acfg in live["agents"].items():
        if not acfg.get("enabled"):
            continue
        curve = books.equity_curve(con, name)
        equity = curve["equity"].iloc[-1] if len(curve) else acfg["capital"]
        realized = books.realized_pnl(con, name)
        pos = books.get_position(con, name)
        pos_str = ("flat" if pos is None else
                   f"{'+' if pos['side'] > 0 else '-'}{pos['contracts']} @ "
                   f"{pos['entry_px']:.3f} ({pos['entry_date']})")
        n_trades = len(books.trade_log(con, name))
        print(f"{name:<12}{equity:>12,.0f}{realized:>+12,.0f}{pos_str:>22}{n_trades:>8}")
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job", choices=["decide", "mark", "status"], required=True)
    ap.add_argument("--date", default=None,
                    help="override 'today' (YYYY-MM-DD) — for backfills/testing")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.date) if args.date else None

    if args.job == "status":
        job_status()
        return 0

    # Imported here (not at module top) so `status` works without ib_async.
    from storage_stress.execution import engine

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] run_agents --job {args.job}")
    try:
        if args.job == "decide":
            engine.run_decide(ROOT, today)
        else:
            engine.run_mark(ROOT, today)
    except ConnectionError as e:
        # Gateway down: report clearly, exit non-zero so the scheduler's log
        # shows a failure. The jobs are idempotent — the next run catches up.
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
