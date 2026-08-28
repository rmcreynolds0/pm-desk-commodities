# DEPLOY — Always-On Paper Stack on a Cloud VM

Goal: the four agents trade and the dashboard stays live **24/5 with your
laptop off**. Everything runs in six Docker containers on one VM: FOUR
`ib-gateway-N` (one headless IBKR login per agent), `engine` (the scheduler
firing rebalance/mark), and `dashboard` (Streamlit). State lives in a
bind-mounted `./data` volume.

You run these steps on the VM; nothing here needs your laptop after setup.

---

## 0. Prerequisites — FOUR paper logins that work headlessly

### Why four

IBKR caps a paper account at **$1,000,000**. Four agents sharing one account
get, after the margin buffer:

```
(1,000,000 × 0.50) / (2 × 4 × 0.12) = $520,833 per agent
```

At that book size **gold, heating oil, feeder cattle and copper round to zero
contracts** and silently drop out — four of the five markets whose removal was
already tested and **rejected** (§6 of `SUMMER_SUMMARY.md`: carry stopped
beating chance, 0.24 → 0.12; drawdown −36% → −53%). The shared layout would
recreate the exact universe we rejected, and the forward test could no longer
distinguish "carry doesn't work" from "we couldn't trade the markets carry
needs."

Giving each agent its **own** $1M account removes the divisor:

```
(1,000,000 × 0.50) / (2 × 1 × 0.12) = $2,083,333 per agent
```

which holds **all 22 markets at ~9% average weight error**, without relaxing
the conservative 12% margin assumption. One gateway is one login, so four
accounts means four gateway containers.

Register four separate IBKR paper usernames before going further.

### The 2FA trap

**This is the #1 thing that breaks a headless Gateway: two-factor auth.**
IBKR's headless Gateway cannot complete a phone-app 2FA prompt.

- **Best:** use **paper-only** usernames. Paper accounts generally do **not**
  enforce IB Key 2FA, so headless auto-login works.
- A paper login tied to a live account with mandatory 2FA will fail headless.

Confirm each of the four logs into IB Gateway **without** a phone prompt before
going headless.

### Do not reuse a username

Two gateways logging into the same IBKR account do not fail loudly — IBKR
disconnects the older session, so the gateways fight each other and agents
intermittently cannot trade. `deploy/bootstrap.sh` refuses duplicate usernames
for this reason.

---

## 1. Pick a VM

**The VM must be x86-64 (amd64). This is not a preference — it is a hard
requirement.** IB Gateway ships only as an amd64 binary; there is no ARM build.
An ARM host will fail at the gateway container no matter how the rest is
configured, so ARM options are listed below only to be ruled out.

| Provider | Cost | Arch | Verdict |
|----------|------|------|---------|
| **Hetzner CX32** | ~€8/mo | x86-64 | **Recommended.** 4 vCPU / 8 GB — enough for four gateways. |
| Hetzner CX22 | ~€4/mo | x86-64 | 2 vCPU / 4 GB. Too small for four gateways; only viable in shared-account mode. |
| **x86 mini PC** (N100, 16 GB) | ~$250 once | x86-64 | Works well. No recurring cost; needs mains power and a stable network. |
| ~~Oracle Cloud Always Free~~ | $0 | **ARM Ampere** | **Will not work** — no ARM build of IB Gateway. The free tier is tempting and this is the trap. |
| ~~Raspberry Pi~~ | ~$80 once | **ARM** | **Will not work** — same reason. |

Specs to target: **8 GB RAM, ~25 GB disk, Ubuntu 24.04 LTS**. FOUR IB Gateways
run here, one per agent, and each is a JVM wanting roughly 700 MB-1 GB. A 4 GB
box will OOM-kill a gateway mid-session, which looks exactly like a login
failure. `deploy/bootstrap.sh` adds swap if RAM is short, but 8 GB is the
right target.

---

## 2. Install Docker on the VM

```bash
ssh <user>@<vm-ip>
curl -fsSL https://get.docker.com | sh          # installs Docker + compose plugin
sudo usermod -aG docker $USER                    # run docker without sudo
newgrp docker                                    # apply group now (or re-login)
docker compose version                           # verify the compose plugin
```

---

## 3. Get the code + secrets onto the VM

**If the repo is in git:**
```bash
git clone <your-repo-url> storage-stress
cd storage-stress            # the inner project folder with pyproject.toml
```

**If not, copy it up from your laptop** (run on the laptop):
```bash
# from inside the outer storage-stress folder
rsync -avz --exclude data/live --exclude .venv --exclude '__pycache__' \
  "storage-stress/" <user>@<vm-ip>:~/storage-stress/
```

**Create `.env` on the VM** — or just run `bash deploy/bootstrap.sh`, which
prompts for all four logins and writes this for you:

```bash
cat > .env <<'EOF'
# One PAPER login per agent. Four separate IBKR usernames — reusing one
# across two gateways makes them disconnect each other.
TWS_USERID_0=paper_username_for_agent_0
TWS_PASSWORD_0=paper_password_for_agent_0
TWS_USERID_1=paper_username_for_agent_1
TWS_PASSWORD_1=paper_password_for_agent_1
TWS_USERID_2=paper_username_for_agent_2
TWS_PASSWORD_2=paper_password_for_agent_2
TWS_USERID_3=paper_username_for_agent_3
TWS_PASSWORD_3=paper_password_for_agent_3
EOF
chmod 600 .env               # readable only by you
```

**No data-provider keys are required.** The cross-sectional strategy takes its
signals from IBKR itself — both legs of the term structure plus a year of daily
bars — so the live stack has no external data dependency. The R2 mirror is
research-only and was measured stale by 43–243 days per market, which is why it
does not drive live decisions. `IBKR_HOST` / `IBKR_PORT` are set directly in
`docker-compose.yml` and need not be repeated here.

---

## 4. Bring the stack up

```bash
docker compose up -d --build     # first build takes a few minutes
```

Watch the gateway log in (this is the step that either works or reveals a 2FA
problem):
```bash
docker compose logs -f ib-gateway
# look for a successful login / "IBC: Login has completed"
# Ctrl-C to stop tailing (containers keep running)
```

**Check the account is big enough BEFORE trading.** This is the step that has
failed most often. Run the rebalance as a dry run — it computes and prints
target positions without sending a single order:
```bash
docker compose exec engine python scripts/run_xsec_live.py --job rebalance --dry-run
```

You should see **one `[SIZING]` line per agent**, each naming its own account:

```
  account mode: per-agent (one paper account each)
  [SIZING] agent_0: equity $1,000,000 — book $2,000,000/agent is within capacity
  [SIZING] agent_1: equity $1,000,000 — book $2,000,000/agent is within capacity
  ...
```

The engine sizes against each account's real equity, not the configured book:

```
capacity_per_agent = (equity × 0.50) / (2 × n_share × 0.12)
```

`n_share` is how many agents share that account — **1** in per-agent mode,
which is the whole point. At IBKR's $1M cap:

| Topology | Book/agent | Markets tradeable | Weight error |
|---|---|---|---|
| Shared, 4 agents | $520,833 | **18/22** — loses GC, HO, GF, HG | 23.8% |
| **Per-agent, $1M each** | **$2,000,000** | **22/22** | **~9%** |

Only four of 22 markets dropping sounds mild, but those four are exactly the
markets the rejected 17-market narrowing removed — the configuration in which
carry stopped beating chance.

If more than **30%** of intended positions round to zero, the engine aborts the
entire rebalance *before* sending anything, and prints why. That is correct
behaviour: a book that cannot hold its universe is a different, smaller
strategy than the one that was backtested.

If a `[SIZING]` line reports less than $2M, that agent's paper account has not
been topped up — reset it in Client Portal → Settings → Paper Trading Account
Reset → **Other** → `1000000`. Do **not** narrow the universe instead; that was
tested and rejected (`docs/SUMMER_SUMMARY.md` §6).

**Then launch for real** (optional — otherwise the scheduler picks it up at the
next 10:30 ET weekday slot, since the launch is self-healing):
```bash
docker compose exec engine python scripts/run_xsec_live.py --job rebalance
docker compose exec engine python scripts/run_xsec_live.py --job mark
```

---

## 5. See the dashboard

The dashboard runs on the VM's port 8501. **Don't expose it to the public
internet** — it has no auth. Use an SSH tunnel from your laptop:

```bash
ssh -L 8501:localhost:8501 <user>@<vm-ip>
# leave that session open, then browse on your laptop to:
#   http://localhost:8501
```

That tunnels your laptop's port 8501 to the VM securely. Close the SSH session
and the tunnel closes; the stack keeps running on the VM regardless.

(If you'd rather have a permanent URL, put it behind a reverse proxy with
auth + TLS — Caddy or Tailscale are the easy options — but the SSH tunnel is
the zero-config secure default.)

---

## 6. Ongoing operation

- The scheduler fires automatically (times in `config/xsec.yaml`, US/Eastern):
  **rebalance** monthly on the last business day at 10:30, **mark** every
  weekday at 16:30.
- **The launch is self-healing.** On or after `first_run_date`, *any* weekday
  at 10:30 ET with an empty book triggers the launch. An earlier one-shot
  design missed its single minute because the host was asleep and never
  retried, leaving the book empty for days — missing a slot should cost a day,
  not the experiment.
- Everything persists in `./data` on the VM: `xsec_books.db` (the ledger) and
  `logs/`. Copy the ledger off for analysis anytime:
  ```bash
  scp <user>@<vm-ip>:~/storage-stress/data/live/xsec_books.db ./xsec-$(date +%F).db
  ```
- Update after a code change: `git pull` (or rsync) then
  `docker compose up -d --build`.
- Restart everything: `docker compose restart`. Stop: `docker compose down`
  (data survives — it's on the host volume).

---

## 7. Common issues

| Symptom | Cause / fix |
|---------|-------------|
| Gateway log shows a 2FA / login loop | The login requires phone 2FA — use a paper-only credential (see §0). |
| `engine` can't connect (connection refused) | Gateway not finished starting, or `IBKR_HOST` not `ib-gateway`. The engine retries; check `docker compose logs ib-gateway`. |
| Orders warn `Error 10349 (TIF set to DAY)` | Benign — IBKR adjusts the market order's time-in-force; the order still fills. |
| **`ABORTED before trading — N/M positions round to 0`** | **The account is too small.** Working as designed. See the sizing table in §4 and raise the paper balance; do not narrow the universe. |
| Orders rejected near expiry | IBKR blocks new positions inside its delivery window. `DEFAULT_GUARD_DAYS = 25` in `xsec_contracts.py` already rolls past this; if a market still trips it, that market's guard needs raising. |
| Order rejected for size | IBKR caps non-algo futures orders at 64 lots. The engine splits large orders automatically (`MAX_ORDER_LOTS = 60`). |
| Everything sits `PreSubmitted`, nothing fills | Ran outside market hours — most likely inside the CME settlement break. The 10:30 ET rebalance exists precisely to avoid this. |
| No trades appear | Check §3 of the dashboard: decisions are recorded even when untraded, with the reason. A wall of `rounds to 0 contracts` means under-capitalisation. |
| Gateway disconnects nightly | Normal — the image restarts the session ~daily to re-auth; the engine's retries absorb it. |
| Dashboard unreachable | Use the SSH tunnel in §5; the port is not meant to be public. |

The stack is self-healing on restarts (`restart: unless-stopped` on every
service, idempotent ledger writes), so a VM reboot brings everything back with
no data loss.
