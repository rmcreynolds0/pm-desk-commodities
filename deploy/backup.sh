#!/usr/bin/env bash
# =============================================================================
# deploy/backup.sh — snapshot the live ledger. Run nightly by cron (02:30 ET),
# installed by deploy/bootstrap.sh. Safe to run by hand at any time.
#
# WHY NOT `cp`: the engine is a concurrent writer using WAL journalling. A
# plain copy of xsec_books.db can catch it mid-transaction and produce a file
# that opens fine and is quietly missing the newest rows — the worst kind of
# corruption, because it looks like a valid backup. sqlite3's backup API takes
# a consistent snapshot of a live database, so that is what we use.
#
# We drive it through the ENGINE CONTAINER's python rather than the host's,
# because the host may have no python and no sqlite3 CLI, whereas the engine
# image is guaranteed to have both a working python and the ledger mounted.
# =============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

STAMP=$(date +%Y-%m-%d)
BACKUP_DIR="data/live/backups"
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] backing up ledger"

docker compose exec -T engine python - <<'PY'
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

SRC = Path("data/live/xsec_books.db")
OUT = Path("data/live/backups") / f"xsec_books-{date.today():%Y-%m-%d}.db"

if not SRC.exists():
    print(f"  ledger {SRC} does not exist yet — nothing to back up")
    sys.exit(0)

OUT.parent.mkdir(parents=True, exist_ok=True)

# sqlite3's online backup API: consistent even while the engine holds the db.
src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=30)
dst = sqlite3.connect(OUT)
with dst:
    src.backup(dst)
dst.close()
src.close()

# Verify the snapshot opens and carries the rows we expect. A backup nobody
# has read is a hope, not a backup.
chk = sqlite3.connect(f"file:{OUT}?mode=ro", uri=True)
counts = {
    t: chk.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    for t in ("agents", "positions", "decisions", "trades", "marks")
}
chk.close()

print(f"  wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
print("  verified:", ", ".join(f"{k}={v}" for k, v in counts.items()))
PY

# Retention: keep 30 days. The ledger is small (tens of KB), so this is about
# tidiness rather than disk pressure.
find "$BACKUP_DIR" -name 'xsec_books-*.db' -mtime "+$KEEP_DAYS" -print -delete 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] backup complete; $(ls -1 "$BACKUP_DIR" 2>/dev/null | wc -l) snapshots retained"
