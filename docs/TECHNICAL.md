# System Reference

### Markets, contracts, signals, and the code that runs them

**QUANTT Commodities Desk · October 2026**

---

## What this document is

The operational reference for the live book: which markets it trades, what
those contracts actually are, what the signals measure, and which file does
what. It assumes you want to *work on* the system rather than evaluate it.

For the theory — why the futures curve carries information, derived from first
principles — see `05_STRATEGY_PAPER.md`. This document is the companion to
that one and does not repeat it.

---

# Part I · Motivation

## 1. Why this strategy exists, and what it replaced

The desk's first strategy was a natural-gas calendar-spread model driven by a
**Deliverability Stress Index** built from EIA storage data. The thesis: the US
gas system has tightened structurally since 2016, so when storage utilisation
runs far from normal the system is under stress and calendar spreads should
respond.

Retired for two independent reasons.

**Three bugs were manufacturing the profit.** Returns were computed across a
spliced contract series, so the price gap at every roll was booked as P&L —
measured at 8.1×, 6.6× and 14.4× a normal daily move, accounting for 31%, 27%
and 6% of all price movement. Slippage was subtracted into a cell the equity
recursion then overwrote, so every backtest ran cost-free. And the random
benchmark was a single seed: a +26.3% result that looked like signal turned out
to sit at the 95th percentile of 200 random agents, with 49% of seeds
profitable.

**The hypothesis itself was false.** Written as a falsifiable claim and tested
on 1,527 weekly observations: post-2016 volatility *fell* 29% (p=0.033) where
the thesis required a rise, seasonal amplitude was unchanged at 0.99×, and
mean reversion collapsed from +0.057 to +0.001. Every measure moved against it.

**What replaced it** is a cross-sectional strategy built on published,
independently replicated premia rather than a thesis invented in-house. That
choice was deliberate: after one falsified hypothesis, the bar for the next one
was that somebody else had already found it and others had reproduced it.

## 2. The design constraint that shapes everything

Every claim must beat a **random agent** running through identical machinery.
Not a single random path — a distribution of 200.

This is why the system is built as a **ladder** of four agents rather than one
model. Each rung sees exactly one more factor than the one below, so the gap
between adjacent rungs isolates that factor's contribution, and the gap above
the random rung measures whether there is any information at all.

| Agent | Sees | Sharpe 2000–2026 |
|---|---|---|
| `agent_0` | nothing — random scores | 0.08 |
| `agent_1` | carry | 0.24 |
| `agent_2` | carry + momentum | 0.36 |
| `agent_3` | carry + momentum + basis-momentum | **0.553** |

Monotonic, and every informed rung beats the null band.

---

# Part II · The signals

All three derive from the same two prices per market: the **front** contract
and the **second** contract. Three different questions asked of the same curve.

## 3. Carry — where the curve is now

$$\text{carry} = \frac{\ln(F_1 / F_2)}{\Delta t}$$

Positive means **backwardation** (front above deferred), implying physical
tightness and a positive expected roll return. Negative means **contango** and
a structural drag.

Dividing by $\Delta t$ annualises it. Markets list on different cycles, so a
one-month and a three-month spread are not comparable until normalised —
without this, the factor partly ranks markets by their listing calendar.

**Implementation:** `data/commodities.py :: add_factors`

## 4. Momentum — where the price has been going

Trailing 12-month compounded return of the front contract, built from
**roll-safe** daily returns.

**Implementation:** `data/commodities.py :: add_factors`, rolling 252-day
product of per-contract returns.

## 5. Basis-momentum — how the curve's shape has been changing

$$\text{basis-mom} = \text{mom}(F_1) - \text{mom}(F_2)$$

If the front has risen faster than the deferred, the curve has been steepening
toward backwardation — the market has been tightening, independent of its
current slope or absolute direction.

Empirically the strongest of the three. Boons & Prado (2019).

**Implementation:** `data/commodities.py :: add_factors`

## 6. How they combine

Each factor is **z-scored across markets within each date** — grouping by date,
not ticker, is what makes this cross-sectional. The score is the equal-weighted
mean of the z-scores an agent is permitted to see.

Weights are equal and not fitted. Fitting them on the evaluation history is in-sample optimisation. A fourth factor reduces every existing weight from 1/3 to 1/4, so it must exceed the mean contribution of the incumbents to improve the blend.

**Implementation:** `agents/xsec.py :: _zscore_xs`, `score`

## 7. A signal that was tested and rejected

`data/commodities.py :: add_stress` ports the retired gas strategy's DSI
pipeline to the cross-section: curve-state extremity → convex transform →
smoothing → deseasonalisation → residualisation against carry.

It produces a genuinely orthogonal factor (cross-sectional correlations −0.10,
+0.03, +0.00 against the three incumbents) and adds +0.138 Sharpe as a *third*
factor — nearly basis-momentum's +0.152. As a *fourth* it costs 0.106.

Rejected against a rule written before the test. The code remains because it is
the evidence behind that rejection.

---

# Part III · The universe

## 8. Why 22 markets

Four sectors, chosen for breadth of economic driver rather than correlation
statistics: energy, metals, grains and oilseeds, softs, livestock. A
cross-sectional strategy needs enough names for a tercile to mean something —
the floor is six, below which each side holds fewer than two names.

A proposed narrowing to **17 markets** was tested and **rejected**: dropping
the five most capital-hungry names made carry stop beating chance (0.24 → 0.12)
and worsened drawdown from −36% to −53%. The account is sized to the universe,
not the universe to the account.

## 9. The contracts

Prices and notionals as measured from IBKR on 2026-08-12. **Notional is what
one contract controls** — the number that determines whether the book can hold
a position at all.

### Energy — NYMEX

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `CL` | Crude Oil (WTI) | 1,000 bbl | 83.17 | $83,170 |
| `NG` | Natural Gas | 10,000 MMBtu | 2.79 | $27,940 |
| `HO` | Heating Oil | 42,000 gal | 4.30 | $180,592 |
| `RB` | Gasoline RBOB | 42,000 gal | 3.14 | $131,699 |

### Metals — COMEX / NYMEX

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `GC` | Gold | 100 oz | 4,425.50 | **$442,550** |
| `SI` | Silver | 1,000 oz | 64.77 | $64,770 |
| `HG` | Copper | 25,000 lb | 6.63 | $165,775 |
| `PL` | Platinum | 50 oz | 1,745.80 | $87,290 |
| `PA` | Palladium | 100 oz | 1,366.00 | $136,600 |

### Grains and oilseeds — CBOT · *quoted in cents*

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `ZC` | Corn | 5,000 bu | 449.75¢ | $22,488 |
| `ZS` | Soybeans | 5,000 bu | 1,159.50¢ | $57,975 |
| `ZM` | Soybean Meal | 100 tons | 307.00 | $30,700 |
| `ZL` | Soybean Oil | 60,000 lb | 68.74¢ | $41,244 |
| `ZO` | Oats | 5,000 bu | 322.50¢ | **$16,125** |
| `KE` | Wheat (Hard Red Winter) | 5,000 bu | 722.75¢ | $36,138 |

### Softs — ICE US / NYBOT · *mostly quoted in cents*

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `CC` | Cocoa | 10 tons | 5,636.00 | $56,360 |
| `KC` | Coffee C | 37,500 lb | 339.65¢ | $127,369 |
| `SB` | Sugar No.11 | 112,000 lb | 16.62¢ | $18,614 |
| `CT` | Cotton No.2 | 50,000 lb | 83.15¢ | $41,575 |
| `OJ` | Orange Juice | 15,000 lb | 137.95¢ | $20,692 |

### Livestock — CME · *quoted in cents*

| Ticker | Market | Multiplier | Price | Notional |
|---|---|---:|---:|---:|
| `LE` | Live Cattle | 40,000 lb | 231.00¢ | $92,400 |
| `GF` | Feeder Cattle | 50,000 lb | 346.40¢ | $173,200 |

## 10. Sizing constraints

**The 27× notional span.** Gold at $442,550 against oats at $16,125. Book size
therefore decides *which markets can hold even one contract*, which is why
capital adequacy is a first-order design constraint rather than an
afterthought.

**Cents quoting.** Grains, softs and cattle quote in cents, carrying a
`priceMagnifier` of 100. Ignoring it overstates contract value 100× — in
testing, coffee once appeared at $12.8M per contract instead of $128k. The
multiplier and magnifier are **read from IBKR at runtime, never hardcoded**,
because a wrong value does not raise an error; it silently mis-sizes every
position in that market.

## 11. One market that was removed

`MWE` (Minneapolis wheat) was in the original 23 and removed on 2026-08-12: it
does not resolve on IBKR. MGEX was absorbed by MIAX and CBOT has no security
definition for it. The backtest was re-run on the remaining 22 so the forward
comparison is like-for-like.

---

# Part IV · Technical infrastructure

## 12. Two strategies live in this repository

The naming convention tells them apart, . `agent_two` exists in both with different meanings:

| Convention | Strategy | Status |
|---|---|---|
| `agent_zero`, `agent_one`, `agent_dsi` · `xsec_`-less filenames | Natural gas calendar spreads | **Retired** |
| `agent_0` … `agent_3` · `xsec_` prefix | Cross-sectional commodities | **Live** |

Retired code is retained as the record behind its retirement.

## 13. Live modules — `src/storage_stress/`

| Module | Responsibility |
|---|---|
| `data/commodities.py` | Builds the panel from settlements; computes carry, momentum, basis-momentum, and the rejected stress factor. Roll-safe returns live here. |
| `data/connectivity.py` | Every external feed, plus the R2 historical mirror. Research only — the live path does not use it. |
| `agents/xsec.py` | The ladder (`AGENT_FACTORS`), cross-sectional z-scoring, tercile weights, portfolio simulator. |
| `execution/xsec_engine.py` | The live engine: rebalance and mark. Sizing, the capital-adequacy rail, order placement, per-agent gateway routing. |
| `execution/xsec_signals_ib.py` | Computes the three factors **live from IBKR** rather than the mirror. |
| `execution/xsec_contracts.py` | Resolves tickers to real IBKR contracts. Reads multiplier and magnifier, enforces the expiry guard and per-order lot cap. |
| `execution/xsec_books.py` | The SQLite ledger. Source of truth for per-agent attribution. |
| `monitoring/flex.py` | IBKR Flex client — the broker's independent record, used to reconcile the ledger. |
| `monitoring/publish.py` | Builds a sanitised public status snapshot. Currently unwired. |
| `signal/dsi.py` | The retired DSI pipeline. Still used by `add_stress`. |

## 14. Live scripts — `scripts/`

| Script | What it does |
|---|---|
| `run_xsec_live.py` | **The live CLI.** `--job rebalance \| mark \| status`, `--dry-run`, `--agents`. |
| `xsec_scheduler.py` | **The always-on loop.** Polls every 60s; fires the monthly rebalance and the daily mark as subprocesses. Self-healing launch. |
| `flatten_account.py` | Closes every open position. Paper-only guard; `--agents` to flatten a subset. |
| `flex_pull.py` | Pulls the broker's statement and reconciles it against the ledger. |
| `verify_xsec_universe.py` | Checks every market resolves on IBKR and prices sensibly. Run before trusting a universe change. |

## 15. Research scripts

| Script | What it does |
|---|---|
| `run_xsec.py` | **The main backtest.** Runs the ladder and an N-seed null distribution. |
| `xsec_robustness.py` | Cost sensitivity, sub-period stability, whether the null is handicapped by turnover. |
| `stress_test.py` | The pre-committed test that rejected the stress factor. |
| `carry_drop_test.py` | The pre-committed test that kept carry despite its negative standalone Sharpe. |
| `xsec_stage2.py` | The two questions that decided whether to deploy at all. |
| `md_to_pdf.py` | Renders project markdown to PDF via Chromium. |

## 16. Retired scripts

`run_agents.py`, `run_backtest.py`, `run_daily.py`, `scheduler.py`,
`regime_test.py`, `null_distribution.py`, `compare_instruments.py`,
`explore_r2_basis.py` — all belong to the natural-gas strategy.

`regime_test.py` is the falsification test that retired that strategy.

## 17. Deployment — `deploy/`

| Script | What it does |
|---|---|
| `bootstrap.sh` | Bare Ubuntu VM → running stack, one command. Refuses ARM, adds swap, sets the clock to ET, firewalls, prompts for credentials, waits for a real gateway login, installs crons. |
| `status.sh` | One screen: container health, gateway login state, per-agent ledger, rounded-to-zero count, disk. |
| `backup.sh` | Nightly ledger snapshot via SQLite's online backup API, verified and retained 30 days. |

---

# Part V · How it runs

## 18. The deployed stack

Six containers on one x86-64 VM:

```
┌─ VM (Ubuntu 24.04, 4 GB, NYC) ──────────────────┐
│  ib-gateway-0 ──── socat :4004 ──── IBKR        │
│  engine        rebalance 10:30 ET, mark 16:30   │
│  dashboard     Streamlit, localhost only        │
│  ledger        xsec_books.db (SQLite)           │
└──────────────────────────────────────────────────┘
       ib-gateway-1/2/3 behind a "full" profile
```

**The live path depends on exactly one external service: IBKR.** Signals and
execution come from the same place, so a position is traded and marked against
the same prices that generated it. The R2 mirror feeds backtests only — it was
measured stale by 43–243 days per market.

IB Gateway binds its API port to loopback and honours the handshake only from
`127.0.0.1`, so the image runs **socat on 4004** forwarding to `127.0.0.1:4002`.
That is the only port another container can use.

## 19. The schedule

| When | Job |
|---|---|
| Last business day, 10:30 ET | **Rebalance** — rank, diff against holdings, trade the difference |
| Every weekday, 16:30 ET | **Mark** — price positions, record equity |
| Daily 02:30 ET | Ledger backup |

10:30 ET is the only window when all 22 markets are liquid simultaneously:
grains open 09:30, softs run roughly 04:00–14:00, energy and metals nearly 24h.

## 20. Capital adequacy

Book size determines which markets can hold a contract:

| Book per agent | Markets tradeable | Avg weight error |
|---|---|---|
| $250k | 11 of 22 | 31% |
| $2.5M | 22 of 22 | 8.8% |
| $10M | 22 of 22 | 1.9% |

If more than **30%** of intended positions round to zero contracts, the engine
aborts the whole rebalance **before sending any order**.

Each agent needs its **own** IBKR paper account, because IBKR caps a paper
account at $1,000,000 and four agents sharing one would get $520,833 each — at
which gold, heating oil, feeder cattle and copper drop out. Those are four of
the five markets whose removal was already tested and rejected.

## 21. Invariants

- **Returns are roll-safe** — only between two prices of the same contract.
- **The ledger records only what actually filled.** Never the target.
- **Plan, then judge, then execute.** The feasibility check completes before
  any order is sent.
- **Every decision is recorded**, traded or not, with its reason.
- **`config/xsec.yaml` is frozen** while the forward test runs.
- **Multiplier and magnifier are read from IBKR**, never assumed.

---

# Part VI · Deployment

## 0. Prerequisites — FOUR paper logins that work headlessly

### Why four

IBKR caps a paper account at **$1,000,000**. Four agents sharing one account
get, after the margin buffer:

```
(1,000,000 × 0.50) / (2 × 4 × 0.12) = $520,833 per agent
```

At that book size **gold, heating oil, feeder cattle and copper round to zero
contracts** and silently drop out — four of the five markets whose removal was
already tested and **rejected** (carry stopped
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

### Two-factor authentication

**This is the #1 thing that breaks a headless Gateway: two-factor auth.**
IBKR's headless Gateway cannot complete a phone-app 2FA prompt.

- **Best:** use **paper-only** usernames. Paper accounts generally do **not**
  enforce IB Key 2FA, so headless auto-login works.
- A paper login tied to a live account with mandatory 2FA will fail headless.

Confirm each of the four logs into IB Gateway **without** a phone prompt before
going headless.

### Username uniqueness

Two gateways using the same IBKR login cause IBKR to disconnect the older session, producing intermittent trading failures. `deploy/bootstrap.sh` rejects duplicate usernames.

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
| **Oracle Always Free — AMD E2.1.Micro** | **$0 forever** | **x86-64** | **Works, for ONE agent.** 1 GB RAM: needs swap and no dashboard container. Two instances included. |
| ~~Oracle Always Free — Ampere A1~~ | $0 | **ARM** | **Will not work** — no ARM build of IB Gateway. 4 cores / 24 GB makes this the tempting trap; take the AMD micro above instead. |
| **Google Cloud e2-micro** | $0 forever | x86-64 | Same profile as the Oracle AMD micro. us-west1 / us-central1 / us-east1 only. |
| **A spare laptop or desktop** | $0 | x86-64 | **Best free option.** Real RAM, scales to all four agents. Disable lid-close suspend. |
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

**Verify account capacity before trading.** The dry run computes and prints target positions without sending orders:
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
tested and rejected.

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
| `Error 10349 (TIF set to DAY)` then **Cancelled** | NOT benign — every order carrying it is cancelled with filled=0. It means the account preset forced TIF=DAY and the market session had ended. Orders now go GTC + outsideRth, but the real fix is to rebalance at 10:30 ET: grains close 14:20, softs ~14:00, livestock 13:05. |
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

---

*Paper trading throughout. No capital is at risk.*
