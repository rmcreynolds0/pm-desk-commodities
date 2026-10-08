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
     cannot run here at all. This rules out Oracle Always-Free AMPERE shapes
     and Raspberry Pi. Use an x86-64 host instead:
       FREE  Oracle Always Free AMD VM.Standard.E2.1.Micro (1 GB, x86)
       FREE  Google Cloud e2-micro (1 GB, us-west1/us-central1/us-east1)
       FREE  any spare laptop or desktop
       PAID  Hetzner CX32 ~EUR 8/mo (8 GB, needed for all four agents)
     Note Oracle offers BOTH an ARM shape and an AMD one -- take the AMD." ;;
  *)
    warn "unrecognised architecture $ARCH — proceeding, but expect trouble" ;;
esac

# --- 2. RESOURCES ------------------------------------------------------------
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
say "resources: ${MEM_MB} MB RAM, ${DISK_GB} GB free disk"
# Each agent's IB Gateway is a JVM wanting roughly 700 MB-1 GB, so the real
# requirement depends on how many agents are configured -- which we do not
# know until step 5. Enforce the ONE-agent floor here, and re-check against
# the actual count once .env has been read.
# The floor is set for the free tiers: Oracle's AMD E2.1.Micro and Google's
# e2-micro both report ~950 MB. One gateway DOES run there with swap and
# without the dashboard container, so refusing at 2 GB would rule out the only
# free x86 hosts. Below ~700 MB the JVM will not start at all.
[ "$MEM_MB" -ge 700 ] || die "need at least ~1 GB RAM for one gateway;
     found ${MEM_MB} MB."
if [ "$MEM_MB" -lt 1800 ]; then
  warn "${MEM_MB} MB RAM — free-tier sized. One agent only, swap required,"
  warn "and the dashboard container will be skipped. Fine for agent_0; you"
  warn "will need ~8 GB before adding the other three."
fi
[ "$DISK_GB" -ge 15 ] || die "need at least 15 GB free disk; found ${DISK_GB} GB"

# Swap on ANY size of host that lacks it. Steady-state memory is not the risk
# -- the spike is all four gateway JVMs authenticating at once during startup,
# which an 8 GB box can hit. Without swap the OOM killer takes one gateway
# down, and the symptom (silent restart, nothing useful in the log) looks
# exactly like a login failure, which is the wrong thing to spend an hour
# debugging. Swap costs only disk.
if ! swapon --show | grep -q . ; then
  SWAP_GB=4
  # Explicit if, not `[ ] && VAR=`: bash exempts that form under set -e, but
  # it is a footgun worth not modelling in a script that runs unattended.
  if [ "$MEM_MB" -ge 7600 ]; then SWAP_GB=2; fi   # large host: smaller cushion
  say "adding a ${SWAP_GB} GB swapfile (headroom for simultaneous gateway logins)"
  fallocate -l "${SWAP_GB}G" /swapfile 2>/dev/null \
    || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024))
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

# --- 5. CREDENTIALS — ONE PAPER LOGIN PER AGENT ------------------------------
# Each agent trades its own $1M paper account, so four separate IBKR paper
# usernames are required. See config/xsec.yaml ibkr.account_mode for why.
if [ ! -f .env ]; then
  say "no .env found — creating one"
  echo
  echo "  The full stack uses FOUR IBKR paper logins, one per agent. IBKR"
  echo "  takes a day or two to open each, so you can start with fewer and"
  echo "  add the rest later — re-run this script when they arrive."
  echo
  echo "  Each must be a PAPER-ONLY login: a headless gateway cannot answer"
  echo "  a phone 2FA prompt."
  echo
  read -r -p "  How many paper accounts do you have right now? [1-4]: " NACC
  case "$NACC" in
    1|2|3|4) ;;
    *) die "enter a number from 1 to 4" ;;
  esac
  umask 077
  : > .env
  i=0
  while [ "$i" -lt "$NACC" ]; do
    read -r -p "  agent_$i  paper username: " U
    read -r -s -p "  agent_$i  paper password: " P
    echo
    [ -n "$U" ] && [ -n "$P" ] || die "agent_$i: both values are required"
    printf 'TWS_USERID_%s=%s\nTWS_PASSWORD_%s=%s\n' "$i" "$U" "$i" "$P" >> .env
    i=$((i + 1))
  done
  chmod 600 .env
  ok ".env written with $NACC login(s), mode 600 (owner read only)"
else
  chmod 600 .env
  grep -q '^TWS_USERID_0=' .env || die ".env exists but has no TWS_USERID_0.
     agent_0's login is the minimum this stack needs. See .env.example."
  ok ".env present"
fi

# How many agents can actually run? Gateways 1-3 are profile-gated, so the
# stack starts whatever is configured and the engine skips the rest.
CONFIGURED=0
for i in 0 1 2 3; do
  if grep -q "^TWS_USERID_$i=." .env && grep -q "^TWS_PASSWORD_$i=." .env; then
    CONFIGURED=$((CONFIGURED + 1))
  else
    break                       # agents come online in order
  fi
done
ok "$CONFIGURED of 4 agent login(s) configured"
if [ "$CONFIGURED" -lt 4 ]; then
  warn "agents $CONFIGURED-3 will not run until their logins are added."
  warn "Add them to .env and re-run this script; agent_0's book is untouched."
fi

# NOW we know how many JVMs will start, so the memory requirement is real.
# Roughly 1 GB per gateway plus ~1 GB for the engine and dashboard.
NEED_MB=$(( CONFIGURED * 1000 + 1000 ))
if [ "$MEM_MB" -lt "$NEED_MB" ]; then
  warn "${MEM_MB} MB RAM for $CONFIGURED gateway(s) is tight (~${NEED_MB} MB"
  warn "wanted). Swap will absorb it, but before adding the remaining agents"
  warn "resize this VM to 8 GB or logins will start failing."
fi

# Refuse duplicate usernames. Two gateways logging into the SAME IBKR account
# do not fail loudly -- IBKR disconnects the older session, so gateways fight
# each other and agents intermittently cannot trade. Silent and very hard to
# diagnose from the logs, so catch it here.
DUPES=$(grep '^TWS_USERID_' .env | cut -d= -f2- | grep -v '^$' | sort | uniq -d)
[ -z "$DUPES" ] || die "duplicate IBKR username(s) in .env: $DUPES
     Each agent needs its OWN paper account. Two gateways sharing a login
     will disconnect each other."

# --- 6. FIREWALL -------------------------------------------------------------
if command -v ufw >/dev/null 2>&1 ; then
  say "firewalling the API and dashboard ports"
  ufw --force default deny incoming  >/dev/null
  ufw --force default allow outgoing >/dev/null
  ufw allow OpenSSH >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null
  # DO NOT add `ufw deny` rules for 4002 / 8501.
  #
  # An earlier version did, as "defence in depth". It broke the stack: ufw
  # rules apply to the FORWARD chain, which is exactly the path that
  # container-to-container traffic takes across the Docker bridge. The engine's
  # packets to ib-gateway-0:4002 were silently DROPPED by our own firewall.
  #
  # The failure was nasty to diagnose because the gateway healthcheck still
  # passed -- it runs INSIDE the gateway container over loopback, which never
  # touches the firewall. So the stack reported healthy while the engine timed
  # out, and a timeout (dropped) rather than a refusal (nothing listening) was
  # the only clue that a firewall was involved.
  #
  # Those rules protected nothing anyway: docker-compose binds both ports to
  # 127.0.0.1, so neither is reachable from outside this host to begin with.
  # The default-deny-incoming policy above is what actually closes the box.
  ufw --force enable >/dev/null
  ok "ufw active — SSH only; 4002 and 8501 reachable through an SSH tunnel only"
else
  warn "ufw not installed; compose already binds both ports to 127.0.0.1"
fi

# --- 7. BUILD AND START ------------------------------------------------------
mkdir -p data/live/logs data/processed data/raw data/interim
# Gateways 1-3 are profile-gated so a partial rollout can run. Only ask for
# the "full" profile once every login is present; otherwise compose would
# start gateways with empty credentials that can never log in.
COMPOSE_ARGS=""
[ "$CONFIGURED" -eq 4 ] && COMPOSE_ARGS="--profile full"

# The dashboard costs ~200 MB. On a 1 GB free-tier host that is the difference
# between the gateway JVM having headroom and the OOM killer taking it out
# mid-session -- a failure whose logs look exactly like a login problem. Skip
# it there; deploy/status.sh gives the same information over SSH.
if [ "$MEM_MB" -ge 1400 ]; then
  COMPOSE_ARGS="$COMPOSE_ARGS --profile ui"
  DASH=yes
else
  warn "only ${MEM_MB} MB RAM — skipping the dashboard container to leave"
  warn "headroom for the gateway. Use 'bash deploy/status.sh' instead, or"
  warn "start it later with: docker compose --profile ui up -d"
  DASH=no
fi

say "building images (the first build takes a few minutes)"
docker compose $COMPOSE_ARGS build
say "starting the stack ($CONFIGURED gateway(s) + engine + dashboard)"
docker compose $COMPOSE_ARGS up -d

# --- 8. WAIT FOR A REAL LOGIN ------------------------------------------------
# "Container running" is not "logged in". Poll the compose healthcheck, which
# tests whether the API port actually accepts a connection.
say "waiting for $CONFIGURED IB Gateway(s) to log in (up to 8 minutes)"
LAST=$((CONFIGURED - 1))
for i in $(seq 1 96); do
  READY=0
  FAILED=""
  for n in $(seq 0 $LAST); do
    CID=$(docker compose $COMPOSE_ARGS ps -q "ib-gateway-$n" 2>/dev/null)
    if [ -z "$CID" ]; then
      FAILED="$FAILED ib-gateway-$n(missing)"
      continue
    fi
    STATE=$(docker inspect --format '{{.State.Health.Status}}' "$CID" 2>/dev/null || echo starting)
    case "$STATE" in
      healthy)   READY=$((READY + 1)) ;;
      unhealthy) FAILED="$FAILED ib-gateway-$n" ;;
    esac
  done

  if [ "$READY" -eq "$CONFIGURED" ]; then
    ok "$READY gateway(s) healthy — API port(s) accepting"
    break
  fi
  if [ -n "$FAILED" ]; then
    die "gateway(s) went unhealthy:$FAILED
     Almost always 2FA, or the same username reused across two gateways.
     Inspect with:  docker compose logs ib-gateway-0 | tail -50"
  fi
  if [ "$i" -eq 96 ]; then
    die "only $READY of $CONFIGURED gateway(s) became healthy within 8 minutes.
     Inspect each with:  docker compose logs ib-gateway-N | tail -50"
  fi
  [ $((i % 12)) -eq 0 ] && say "  still waiting — $READY/$CONFIGURED healthy"
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

# --- 9b. FLEX RECONCILIATION -------------------------------------------------
# Our ledger is written by the same code that places the orders, so if that
# code is wrong about a fill the ledger is wrong in the way hardest to catch:
# everything downstream stays internally consistent and is simply false. This
# project has already had that bug -- 54 positions recorded that the broker
# never held. Flex is IBKR's own account of the same events and the only
# independent check available.
if grep -q '^FLEX_TOKEN=.' .env 2>/dev/null && \
   grep -q '^FLEX_QUERY_ID=.' .env 2>/dev/null ; then
  if ! crontab -l 2>/dev/null | grep -q 'flex_pull.py' ; then
    say "installing nightly Flex reconciliation at 18:30 ET"
    # After the 16:30 mark, so it checks the state the marks were computed on.
    FLEX_LINE="30 18 * * 1-5 cd $REPO_DIR && docker compose exec -T engine python scripts/flex_pull.py >> data/live/logs/flex.log 2>&1"
    { crontab -l 2>/dev/null; echo "$FLEX_LINE"; } | crontab -
    ok "flex reconciliation cron installed"
  else
    ok "flex reconciliation cron already present"
  fi
else
  # Expected on a paper-only setup: IBKR issues Flex tokens against funded
  # LIVE accounts and they cannot read a paper account at all. The public
  # status snapshot below covers external tracking instead.
  ok "no Flex credentials — skipping reconciliation (expected on paper)"
fi

# --- 9c. PUBLIC STATUS SNAPSHOT -- REMOVED
# The gist publisher was cut in favour of Flex Web Service, which reads IBKR's
# OWN record rather than re-publishing ours. scripts/publish_status.py and
# monitoring/publish.py remain in the tree, tested and unwired; git history has
# the cron if it is ever wanted back.

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
if [ "$DASH" = "no" ]; then
echo "  2. No dashboard on this host (too little RAM). Check status with:"
echo ""
echo "       bash deploy/status.sh"
else
echo "  2. View the dashboard from your laptop over an SSH tunnel:"
fi
echo
echo "       ssh -L 8501:localhost:8501 $(whoami)@<this-vm-ip>"
echo "       then browse to http://localhost:8501"
echo
echo "  3. Nothing else. The scheduler launches any agent that has not yet"
echo "     traded, at the next 10:30 ET weekday slot, and marks every weekday"
echo "     at 16:30 ET. Your laptop can be off."
echo
if [ "$CONFIGURED" -lt 4 ]; then
echo "  ${BOLD}Adding the remaining agents later${OFF}"
echo "     When the other paper accounts open, append their logins to .env:"
echo "       TWS_USERID_1=... / TWS_PASSWORD_1=...   (and 2, 3)"
echo "     then re-run this script, or directly:"
echo "       docker compose --profile full up -d"
echo "     The scheduler flattens and launches ONLY the new agents; the ones"
echo "     already trading keep their positions and their record."
echo
fi
echo
echo "  Useful:"
echo "       bash deploy/status.sh          one-screen health summary"
echo "       docker compose logs -f engine  live scheduler output"
echo "       bash deploy/backup.sh          snapshot the ledger now"
echo
