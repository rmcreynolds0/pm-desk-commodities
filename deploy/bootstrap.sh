#!/usr/bin/env bash
# =============================================================================
# deploy/bootstrap.sh — take a FRESH Ubuntu 24.04 VM to a running paper stack.
#
#   ssh root@<vm-ip>
#   git clone <repo-url> storage-stress && cd storage-stress
#   bash deploy/bootstrap.sh
#
# Idempotent: safe to re-run. Every step checks before it acts, so a re-run
# after a failure resumes rather than duplicating work.
#
# WHY EACH STEP IS HERE rather than left to the operator:
#   1. Refuses to run on ARM. IB Gateway ships amd64 only; discovering that
#      after provisioning a free Oracle ARM box wastes an afternoon.
#   2. Adds swap when RAM < 4 GB. The Gateway is a JVM with a real heap; on a
#      2 GB box without swap the OOM killer takes it out mid-session, and the
#      symptom (silent restarts, no log) looks exactly like a login failure.
#   3. Sets the clock to America/New_York. The scheduler compares wall-clock
#      strings against ET times; a UTC host fires the rebalance at the wrong
#      moment and lands inside the CME settlement break, where orders queue
#      as PreSubmitted and never fill.
#   4. Firewalls 4002 and 8501. Both are already bound to localhost in compose,
#      but defence in depth: the dashboard has no auth and the API port
#      accepts orders.
#   5. Installs a nightly ledger backup. The forward record is the entire point
#      of the project and it lives in one SQLite file.
# =============================================================================
set -euo pipefail

BOLD=$(printf '\033[1m'); RED=$(printf '\033[31m')
GRN=$(printf '\033[32m'); YEL=$(printf '\033[33m'); OFF=$(printf '\033[0m')
say()  { printf '%s==>%s %s\n' "$BOLD" "$OFF" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$OFF" "$*"; }
warn() { printf '%s  !!%s %s\n' "$YEL" "$OFF" "$*"; }
die()  { printf '%s ERR%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_DIR"
say "repository: $REPO_DIR"

# --- 1. ARCHITECTURE GATE ----------------------------------------------------
ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64)
    ok "architecture $ARCH (amd64) — IB Gateway supported" ;;
  aarch64|arm64)
    die "architecture $ARCH is ARM. IB Gateway has NO ARM build, so this stack
     cannot run here at all. This rules out Oracle Cloud Always-Free Ampere
     shapes and Raspberry Pi. Use an x86-64 host: Hetzner CX22 (~EUR 4/mo),
     DigitalOcean (~USD 6/mo), or an N100 mini PC (~USD 150 once)." ;;
  *)
    warn "unrecognised architecture $ARCH — proceeding, but expect trouble" ;;
esac

# --- 2. RESOURCES ------------------------------------------------------------
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
say "resources: ${MEM_MB} MB RAM, ${DISK_GB} GB free disk"
[ "$MEM_MB" -ge 1800 ] || die "need at least 2 GB RAM; found ${MEM_MB} MB"
[ "$DISK_GB" -ge 10 ]  || die "need at least 10 GB free disk; found ${DISK_GB} GB"

if [ "$MEM_MB" -lt 3800 ] && ! swapon --show | grep -q . ; then
  say "RAM under 4 GB and no swap — adding a 2 GB swapfile"
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "swap enabled and persisted across reboot"
else
  ok "memory and swap adequate"
fi

# --- 3. TIMEZONE -------------------------------------------------------------
CURRENT_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)
if [ "$CURRENT_TZ" != "America/New_York" ]; then
  say "setting host timezone to America/New_York (scheduler compares ET times)"
  timedatectl set-timezone America/New_York || warn "could not set timezone"
fi
ok "timezone now $(timedatectl show -p Timezone --value 2>/dev/null || date +%Z)"

# --- 4. DOCKER ---------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1 ; then
  say "installing Docker"
  curl -fsSL https://get.docker.com | sh
  ok "Docker installed"
else
  ok "Docker present: $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin is missing"

if [ "$(id -u)" -ne 0 ] && ! groups | grep -qw docker ; then
  say "adding $USER to the docker group"
  sudo usermod -aG docker "$USER"
  warn "log out and back in (or run: newgrp docker), then re-run this script"
  exit 0
fi

# --- 5. CREDENTIALS ----------------------------------------------------------
if [ ! -f .env ]; then
  say "no .env found — creating one"
  echo
  echo "  IBKR PAPER credentials. These must be a paper-only login:"
  echo "  a headless IB Gateway CANNOT answer a phone 2FA prompt."
  echo
  read -r -p "  TWS_USERID (paper username): " TWS_U
  read -r -s -p "  TWS_PASSWORD (paper password): " TWS_P
  echo
  [ -n "$TWS_U" ] && [ -n "$TWS_P" ] || die "both values are required"
  umask 077
  {
    printf 'TWS_USERID=%s\n' "$TWS_U"
    printf 'TWS_PASSWORD=%s\n' "$TWS_P"
  } > .env
  chmod 600 .env
  ok ".env written, mode 600 (owner read only)"
else
  chmod 600 .env
  grep -q '^TWS_USERID='   .env || die ".env exists but has no TWS_USERID"
  grep -q '^TWS_PASSWORD=' .env || die ".env exists but has no TWS_PASSWORD"
  ok ".env present and populated"
fi

# --- 6. FIREWALL -------------------------------------------------------------
if command -v ufw >/dev/null 2>&1 ; then
  say "firewalling the API and dashboard ports"
  ufw --force default deny incoming  >/dev/null
  ufw --force default allow outgoing >/dev/null
  ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
  ufw deny 4002/tcp >/dev/null
  ufw deny 8501/tcp >/dev/null
  ufw --force enable >/dev/null
  ok "ufw active — SSH only; 4002 and 8501 reachable through an SSH tunnel only"
else
  warn "ufw not installed; compose already binds both ports to 127.0.0.1"
fi

# --- 7. BUILD AND START ------------------------------------------------------
mkdir -p data/live/logs data/processed data/raw data/interim
say "building images (the first build takes a few minutes)"
docker compose build
say "starting the stack"
docker compose up -d

# --- 8. WAIT FOR A REAL LOGIN ------------------------------------------------
# "Container running" is not "logged in". Poll the compose healthcheck, which
# tests whether the API port actually accepts a connection.
say "waiting for IB Gateway to log in (up to 5 minutes)"
GW_CID=$(docker compose ps -q ib-gateway)
for i in $(seq 1 60); do
  STATE=$(docker inspect --format '{{.State.Health.Status}}' "$GW_CID" 2>/dev/null || echo starting)
  if [ "$STATE" = "healthy" ]; then
    ok "gateway healthy — API port accepting connections"
    break
  fi
  if [ "$STATE" = "unhealthy" ]; then
    die "gateway went unhealthy. This is almost always 2FA. Inspect with:
     docker compose logs ib-gateway | tail -50"
  fi
  if [ "$i" -eq 60 ]; then
    die "gateway did not become healthy within 5 minutes. Inspect with:
     docker compose logs ib-gateway | tail -50"
  fi
  sleep 5
done

# --- 9. NIGHTLY LEDGER BACKUP ------------------------------------------------
if ! crontab -l 2>/dev/null | grep -q 'deploy/backup.sh' ; then
  say "installing nightly ledger backup at 02:30 ET"
  CRON_LINE="30 2 * * * cd $REPO_DIR && bash deploy/backup.sh >> data/live/logs/backup.log 2>&1"
  { crontab -l 2>/dev/null; echo "$CRON_LINE"; } | crontab -
  ok "backup cron installed"
else
  ok "backup cron already present"
fi

# --- 10. REPORT --------------------------------------------------------------
echo
say "stack is up"
docker compose ps
echo
echo "${BOLD}Next steps${OFF}"
echo
echo "  1. Confirm the account is large enough BEFORE it trades."
echo "     This computes and records targets but sends NO orders:"
echo
echo "       docker compose exec engine \\"
echo "         python scripts/run_xsec_live.py --job rebalance --dry-run"
echo
echo "     Read the [SIZING] line. You want to see:"
echo "       \"book \$10,000,000/agent is within capacity\""
echo "     If it names a smaller number, raise the IBKR paper balance:"
echo "       Client Portal -> Settings -> Paper Trading Account Reset -> Other"
echo "     \$10M/agent needs about \$19.2M of paper equity. See docs/DEPLOY.md."
echo
echo "  2. View the dashboard from your laptop over an SSH tunnel:"
echo
echo "       ssh -L 8501:localhost:8501 $(whoami)@<this-vm-ip>"
echo "       then browse to http://localhost:8501"
echo
echo "  3. Nothing else. The scheduler launches itself at the next 10:30 ET"
echo "     weekday slot while the book is empty, and marks every weekday at"
echo "     16:30 ET. Your laptop can be off."
echo
echo "  Useful:"
echo "       bash deploy/status.sh          one-screen health summary"
echo "       docker compose logs -f engine  live scheduler output"
echo "       bash deploy/backup.sh          snapshot the ledger now"
echo
