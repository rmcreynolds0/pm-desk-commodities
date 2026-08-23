# DEPLOY — Always-On Paper Stack on a Cloud VM

Goal: the four agents trade and the dashboard stays live **24/5 with your
laptop off**. Everything runs in three Docker containers on a small VM:
`ib-gateway` (headless IBKR login), `engine` (the scheduler firing decide/mark),
and `dashboard` (Streamlit). State lives in a bind-mounted `./data` volume.

You run these steps on the VM; nothing here needs your laptop after setup.

---

## 0. Prerequisites — a paper login that can log in headlessly

**This is the #1 thing that breaks a headless Gateway: two-factor auth.**
IBKR's headless Gateway cannot complete a phone-app 2FA prompt. Options:

- **Best:** use a **paper-only** username/password. Paper accounts generally
  do **not** enforce IB Key 2FA, so headless auto-login works.
- If your paper login is tied to a live account with mandatory 2FA, headless
  login will fail. Create/enable a standalone paper user, or use IBKR's
  "second factor device sharing" — but the paper-only route is far simpler.

Confirm you can log into IB Gateway with the username/password **without** a
phone prompt before going headless.

---

## 1. Pick a VM

**The VM must be x86-64 (amd64). This is not a preference — it is a hard
requirement.** IB Gateway ships only as an amd64 binary; there is no ARM build.
An ARM host will fail at the gateway container no matter how the rest is
configured, so ARM options are listed below only to be ruled out.

| Provider | Cost | Arch | Verdict |
|----------|------|------|---------|
| **Hetzner CX22** | ~€4/mo | x86-64 | **Recommended.** 2 vCPU / 4 GB. Cheapest host that actually runs the gateway. |
| **DigitalOcean** | ~$6/mo | x86-64 | Works. 1 vCPU / 2 GB is the practical minimum. |
| **x86 mini PC** (N100) | ~$150 once | x86-64 | Works. No recurring cost; needs mains power and a stable home network. |
| ~~Oracle Cloud Always Free~~ | $0 | **ARM Ampere** | **Will not work** — no ARM build of IB Gateway. The free tier is tempting and this is the trap. |
| ~~Raspberry Pi~~ | ~$80 once | **ARM** | **Will not work** — same reason. |

Specs to target: **2+ GB RAM, ~20 GB disk, Ubuntu 24.04 LTS**. 4 GB is
comfortable. The stack idles low; the gateway is the heaviest part.

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

**Create `.env` on the VM** (never commit it; create it fresh here):
```bash
cat > .env <<'EOF'
# Headless IB Gateway login (PAPER credentials) — the ONLY secrets this
# stack needs.
TWS_USERID=your_ibkr_paper_username
TWS_PASSWORD=your_ibkr_paper_password
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

Read the `[SIZING]` line it prints. The engine sizes against the account's real
equity, not the configured book:

```
capacity_per_agent = (equity × 0.50) / (2 × n_agents × 0.12)
```

Inverted, that gives the paper equity each book size requires:

| Target book/agent | Paper equity needed | Result |
|---|---|---|
| $250k | $480k | only 11/22 markets tradeable, 31% avg weight error |
| **$2.5M** | **$4.8M** | 22/22 markets, 8.8% error — **minimum viable** |
| $10M | $19.2M | 22/22 markets, 1.9% error — the `xsec.yaml` target |

If more than **30%** of intended positions round to zero contracts, the engine
aborts the entire rebalance *before* sending anything, and prints why. That is
correct behaviour, not a fault: a book that can only hold half its universe is
a different, smaller strategy than the one that was backtested.

The fix is to raise the paper balance — IBKR Client Portal → Settings → Paper
Trading Account → reset with a larger starting balance — **not** to narrow the
universe. A 17-market narrowing was tested and rejected: carry stopped beating
chance and drawdown worsened by 17 points (`docs/SUMMER_SUMMARY.md` §6).

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
