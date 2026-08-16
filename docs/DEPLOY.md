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
# EIA storage feed
EIA_API_KEY=your_free_key_from_eia.gov/opendata

# Engine reaches the gateway CONTAINER, not localhost
IBKR_HOST=ib-gateway
IBKR_PORT=4002

# Headless IB Gateway login (PAPER credentials)
TWS_USERID=your_ibkr_paper_username
TWS_PASSWORD=your_ibkr_paper_password
EOF
chmod 600 .env               # readable only by you
```
(Use your own EIA key if you have one. R2 basis keys are optional and only
needed later when you replace the synthetic basis feed.)

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

Then confirm the engine can reach it and the ledger is alive:
```bash
docker compose exec engine python scripts/run_agents.py --job status
```
You should see the four books at their $200k base. If the gateway isn't ready
yet, the engine retries (configured in `config/live.yaml`).

**Force a first decision immediately** (optional — otherwise it waits for the
next scheduled Thursday):
```bash
docker compose exec engine python scripts/run_agents.py --job decide
docker compose exec engine python scripts/run_agents.py --job mark
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

- The scheduler fires automatically (times in `config/live.yaml`, US/Eastern):
  **decide** Thursdays 16:00, **mark** weekdays 17:15.
- Everything persists in `./data` on the VM: `books.db` (the ledger) and
  `logs/`. Copy the ledger off for analysis anytime:
  ```bash
  scp <user>@<vm-ip>:~/storage-stress/data/live/books.db ./books-$(date +%F).db
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
| `engine` can't connect (connection refused) | Gateway not finished starting, or `IBKR_HOST` not `ib-gateway` in `.env`. The engine retries; check `docker compose logs ib-gateway`. |
| Orders warn `Error 10349 (TIF set to DAY)` | Benign — IBKR adjusts the market order's time-in-force; the order still fills. |
| No trades appear | Expected in weeks where signals are below threshold; only `agent_zero` trades every week. Check the decisions in the dashboard's signal-history panel. |
| Gateway disconnects nightly | Normal — the image restarts the session ~daily to re-auth; the engine's retries absorb it. |
| Dashboard unreachable | Use the SSH tunnel in §5; the port is not meant to be public. |

The stack is self-healing on restarts (`restart: unless-stopped` on every
service, idempotent ledger writes), so a VM reboot brings everything back with
no data loss.
