#!/usr/bin/env python3
"""
flex_pull.py — pull the IBKR Flex statement and reconcile it against our ledger.

    python scripts/flex_pull.py                 # pull, reconcile, save
    python scripts/flex_pull.py --no-save       # just look
    python scripts/flex_pull.py --raw out.xml   # keep the raw statement too

WHY RUN THIS
    data/live/xsec_books.db is written by the same code that places the orders.
    If that code is wrong about a fill, the ledger is wrong in the way that is
    hardest to catch: everything downstream stays internally consistent and is
    simply false. This project has already had that exact bug -- 54 positions
    recorded that the broker never held, because rejected orders were being
    written as fills.

    Flex is IBKR's own account of the same events, reached with a READ-ONLY
    token. It is the only independent check available.

EXIT CODES
    0  broker and ledger agree
    1  they disagree, or the pull failed
    Non-zero is deliberate: this is meant to be run from cron and to be noisy
    when it finds something.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from storage_stress.monitoring import flex                    # noqa: E402
from storage_stress.execution import xsec_books as B          # noqa: E402


def ledger_positions(cfg: dict) -> dict[str, int]:
    """Net contracts per ticker across ALL agents.

    Summed across agents because Flex reports the ACCOUNT, and in shared-account
    mode four agents' positions net together there. In per-agent mode each
    account holds one agent, so the sum is that agent's book and the comparison
    is exact either way.
    """
    db = ROOT / cfg["paths"]["books_db"]
    if not db.exists():
        return {}
    con = B.connect(db)
    try:
        rows = con.execute(
            "SELECT ticker, SUM(contracts) FROM positions GROUP BY ticker"
        ).fetchall()
    finally:
        con.close()
    return {t: int(n) for t, n in rows if n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true",
                    help="do not write a snapshot file")
    ap.add_argument("--raw", metavar="PATH",
                    help="also write the raw statement XML here")
    args = ap.parse_args()

    with open(ROOT / "config" / "xsec.yaml") as f:
        cfg = yaml.safe_load(f)

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] pulling Flex statement")
    try:
        if args.raw:
            import os
            token = os.environ.get("FLEX_TOKEN", "")
            qid = os.environ.get("FLEX_QUERY_ID", "")
            if not token or not qid:
                raise flex.FlexError("config", "FLEX_TOKEN / FLEX_QUERY_ID unset")
            ref, url = flex.request_statement(token, qid)
            xml = flex.fetch_statement(token, ref, url)
            Path(args.raw).write_text(xml, encoding="utf-8")
            print(f"  raw statement -> {args.raw}")
            snap = flex.parse_statement(xml)
        else:
            snap = flex.pull()
    except flex.FlexError as e:
        print(f"  [FAIL] {e}", file=sys.stderr)
        if e.code == "config":
            print("\n  Set these in .env (see src/storage_stress/monitoring/"
                  "flex.py for how to generate them):", file=sys.stderr)
            print("    FLEX_TOKEN=...\n    FLEX_QUERY_ID=...", file=sys.stderr)
        return 1
    except Exception as e:                                   # noqa: BLE001
        print(f"  [FAIL] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"  account {snap.account_id or '?'}   period {snap.when or '?'}")
    print(f"  NAV {f'${snap.nav:,.0f}' if snap.nav is not None else '—'}")
    print(f"  {len(snap.positions)} open position(s), "
          f"{len(snap.trades)} trade(s) in the period")

    rec = flex.reconcile(snap, ledger_positions(cfg))
    print(f"\n  broker holds {rec['n_broker']} symbol(s); "
          f"ledger claims {rec['n_ledger']}")

    if rec["agree"]:
        print("  RECONCILED — broker and ledger agree")
    else:
        print(f"  MISMATCH on {len(rec['mismatches'])} symbol(s):")
        print(f"    {'symbol':<10}{'broker':>10}{'ledger':>10}{'diff':>10}")
        for m in rec["mismatches"]:
            print(f"    {m['symbol']:<10}{m['broker']:>10.0f}"
                  f"{m['ledger']:>10.0f}{m['diff']:>+10.0f}")
        print("\n  The ledger is the source of truth for per-agent attribution,")
        print("  so a mismatch means the marks computed from it are wrong.")
        print("  Check for rejected orders recorded as fills.")

    if not args.no_save:
        out_dir = ROOT / "data" / "live" / "flex"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        out = out_dir / f"flex-{stamp}.json"
        out.write_text(json.dumps({
            "pulled_utc": stamp,
            "account": snap.account_id,
            "period": snap.when,
            "nav": snap.nav,
            "positions": snap.positions,
            "trades": snap.trades,
            "reconciliation": rec,
        }, indent=2), encoding="utf-8")
        print(f"\n  snapshot -> {out.relative_to(ROOT)}")

    return 0 if rec["agree"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
