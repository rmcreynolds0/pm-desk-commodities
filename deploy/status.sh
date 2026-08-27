#!/usr/bin/env bash
# =============================================================================
# deploy/status.sh — one screen telling you whether the stack is actually
# working. Run it on the VM whenever you want to know where things stand:
#
#     bash deploy/status.sh
#
# It answers, in order, the questions you actually have:
#   Are the three containers up and HEALTHY (not merely running)?
#   Is the gateway logged in?
#   Has the book launched, and what is it holding?
#   When did the scheduler last do anything?
#   Is the disk about to fill?
# =============================================================================
set -uo pipefail        # NOT -e: a failing section should still print the rest

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BOLD=$(printf '\033[1m'); OFF=$(printf '\033[0m')
hdr() { printf '\n%s%s%s\n' "$BOLD" "$*" "$OFF"; }

printf '%sxsec paper stack%s   %s\n' "$BOLD" "$OFF" "$(date '+%Y-%m-%d %H:%M:%S %Z')"

# --- containers --------------------------------------------------------------
hdr "Containers"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null \
  || echo "  docker compose unavailable or stack not created"

# --- gateway -----------------------------------------------------------------
hdr "IB Gateway"
GW=$(docker compose ps -q ib-gateway 2>/dev/null)
if [ -n "$GW" ]; then
  HEALTH=$(docker inspect --format '{{.State.Health.Status}}' "$GW" 2>/dev/null || echo unknown)
  echo "  health: $HEALTH"
  if [ "$HEALTH" != "healthy" ]; then
    echo "  last log lines:"
    docker compose logs --tail 12 ib-gateway 2>/dev/null | sed 's/^/    /'
    echo "  (an unhealthy gateway is almost always a 2FA prompt the headless"
    echo "   login cannot answer — see docs/DEPLOY.md section 0)"
  fi
else
  echo "  not running"
fi

# --- ledger ------------------------------------------------------------------
hdr "Ledger"
docker compose exec -T engine python - <<'PY' 2>/dev/null || echo "  (engine not reachable)"
import sqlite3
from pathlib import Path

DB = Path("data/live/xsec_books.db")
if not DB.exists():
    print("  no ledger yet — the engine creates it on its first run")
    raise SystemExit(0)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
q = lambda s, *a: con.execute(s, a).fetchall()

agents = q("SELECT name, capital FROM agents ORDER BY name")
if not agents:
    print("  ledger exists but no agents registered — book has not launched")
else:
    print(f"  {'agent':<10}{'capital':>15}{'equity':>15}{'positions':>11}{'trades':>8}")
    print("  " + "-" * 57)
    for name, cap in agents:
        eq = q("SELECT equity FROM marks WHERE agent=? ORDER BY date DESC LIMIT 1", name)
        eq = eq[0][0] if eq else cap
        npos = q("SELECT COUNT(*) FROM positions WHERE agent=?", name)[0][0]
        ntr = q("SELECT COUNT(*) FROM trades WHERE agent=?", name)[0][0]
        print(f"  {name:<10}{cap:>15,.0f}{eq:>15,.0f}{npos:>11}{ntr:>8}")

    md = q("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM marks")[0]
    if md[0]:
        print(f"\n  marks: {md[2]} days, {md[0]} -> {md[1]}")
    else:
        print("\n  marks: none recorded yet")

    # The number that reveals under-capitalisation.
    rows = q("SELECT rebal_date, COUNT(*), "
             "SUM(CASE WHEN actual_contracts=0 THEN 1 ELSE 0 END) "
             "FROM decisions WHERE IFNULL(target_weight,0)!=0 "
             "GROUP BY rebal_date ORDER BY rebal_date DESC LIMIT 1")
    if rows and rows[0][0]:
        d, want, zero = rows[0]
        pct = zero / want * 100 if want else 0
        flag = "  <-- UNDER-CAPITALISED" if pct > 30 else ""
        print(f"  last rebalance {d}: {zero}/{want} intended positions "
              f"rounded to zero ({pct:.0f}%){flag}")
con.close()
PY

# --- scheduler ---------------------------------------------------------------
hdr "Scheduler (last 8 lines)"
docker compose logs --tail 8 engine 2>/dev/null | sed 's/^/  /' \
  || echo "  engine not running"

hdr "Job logs on disk"
ls -1t data/live/logs/*.log 2>/dev/null | head -5 | sed 's/^/  /' \
  || echo "  none yet"

# --- host --------------------------------------------------------------------
hdr "Host"
printf '  disk : %s\n' "$(df -h / | awk 'NR==2 {print $4 " free of " $2 " (" $5 " used)"}')"
printf '  mem  : %s\n' "$(free -h 2>/dev/null | awk 'NR==2 {print $7 " available of " $2}')"
printf '  time : %s\n' "$(date '+%Z %z')  (scheduler expects America/New_York)"
BK=$(ls -1 data/live/backups/*.db 2>/dev/null | wc -l)
printf '  backups: %s snapshot(s)\n' "$BK"
echo
