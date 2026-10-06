#!/usr/bin/env python3
"""
publish_status.py — publish a public snapshot of the live book.

    python scripts/publish_status.py --dry-run     # print it, upload nothing
    python scripts/publish_status.py               # upload to R2
    python scripts/publish_status.py --out s.json  # also keep a local copy

WHAT THIS IS FOR
    Letting someone follow the book WITHOUT the trading login. IBKR's Flex Web
    Service cannot do this -- its tokens only read funded live accounts, never
    paper -- so the snapshot is published directly instead.

    What goes out is the equity curve, the positions, and every decision
    including the ones the engine declined to trade. What does NOT go out is
    anything identifying the account: no account number, no IBKR order ids, no
    orderRef tags, no credentials.

    ALWAYS --dry-run the first time. Once a key is public at a URL it cannot
    be recalled, and the point of reviewing it is to confirm nothing private
    slipped in.

WHERE IT LANDS
    R2 key `status/latest.json`, plus a dated copy under `status/history/` so
    the record is not silently rewritten. The bucket must be configured to
    serve objects publicly (or hand out a signed URL) for anyone to read it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from storage_stress.execution import xsec_books as B          # noqa: E402
from storage_stress.monitoring import publish as P            # noqa: E402

LATEST_KEY = "status/latest.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="build and print the payload; upload nothing")
    ap.add_argument("--out", metavar="PATH",
                    help="also write the payload to this local file")
    ap.add_argument("--no-history", action="store_true",
                    help="overwrite latest.json only, keep no dated copy")
    args = ap.parse_args()

    with open(ROOT / "config" / "xsec.yaml") as f:
        cfg = yaml.safe_load(f)

    db = ROOT / cfg["paths"]["books_db"]
    if not db.exists():
        print(f"[ERROR] no ledger at {db} — nothing to publish", file=sys.stderr)
        return 1

    con = B.connect(db)
    try:
        payload = P.build_payload(con, cfg)
    finally:
        con.close()

    body = json.dumps(payload, indent=2)

    n_agents = len(payload["agents"])
    print(f"built snapshot: {n_agents} live agent(s), "
          f"{len(payload['equity_curve'])} curve point(s), "
          f"{len(payload['positions'])} position(s), "
          f"{len(payload['recent_decisions'])} decision(s), "
          f"{len(body)/1024:.1f} KB")

    if args.out:
        P.write_local(payload, Path(args.out))
        print(f"  local copy -> {args.out}")

    if args.dry_run:
        print("\n--- payload (dry run, nothing uploaded) ---")
        print(body[:4000])
        if len(body) > 4000:
            print(f"... [{len(body) - 4000} more chars]")
        return 0

    try:
        P.upload_r2(body, LATEST_KEY)
        print(f"  uploaded -> {LATEST_KEY}")
        if not args.no_history:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            hist = f"status/history/{stamp}.json"
            P.upload_r2(body, hist)
            print(f"  archived -> {hist}")
    except Exception as e:                                   # noqa: BLE001
        print(f"[ERROR] upload failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
