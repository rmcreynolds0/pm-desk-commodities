# Technical Reference

Cross-sectional commodity futures strategy. 22 markets, long/short,
dollar-neutral, monthly rebalance, IBKR paper.

---

## 1. Signals

All three derive from two prices per market: front contract `F₁` and second
`F₂`. Computed in `data/commodities.py :: add_factors`.

| Factor | Definition | Measures |
|---|---|---|
| `carry` | `ln(F₁/F₂) / Δt` | Curve slope. Positive = backwardation |
| `mom` | 12-month return of `F₁` | Price trend |
| `basis_mom` | `mom(F₁) − mom(F₂)` | Curve shape change |

`Δt` is the gap between deliveries in years. Annualisation is required:
markets list on different cycles.

Momentum uses roll-safe returns — computed only between two prices of the same
contract.

### Combination

Each factor is z-scored across markets within each date
(`agents/xsec.py :: _zscore_xs`), then averaged with equal weights. Weights are
not fitted.

Top tercile long, bottom tercile short, `1/k` per side. Minimum 6 markets.
Volatility targeted to 10% annualised, leverage capped at 3×.

### Agent ladder

| Agent | Factors | Sharpe | vs null |
|---|---|---|---|
| `agent_0` | none (random) | 0.08 | — |
| `agent_1` | `carry` | 0.24 | 98th |
| `agent_2` | `+ mom` | 0.36 | 100th |
| `agent_3` | `+ basis_mom` | 0.553 | 99.5th |

2000–2026, 6,676 trading days, 200-seed null. `agent_3` trades live.

### Rejected

`add_stress` ports the retired natural-gas DSI pipeline to the cross-section.
Orthogonal to the three incumbents (−0.10, +0.03, +0.00) and adds +0.138 Sharpe
as a third factor, but costs 0.106 as a fourth. Rejected against a rule written
before the test. Code retained as the record.

---

## 2. Universe

Specifications measured from IBKR 2026-08-12.

| Ticker | Market | Exchange | Multiplier | Notional |
|---|---|---|---:|---:|
| `CL` | Crude Oil WTI | NYMEX | 1,000 bbl | $83,170 |
| `NG` | Natural Gas | NYMEX | 10,000 MMBtu | $27,940 |
| `HO` | Heating Oil | NYMEX | 42,000 gal | $180,592 |
| `RB` | Gasoline RBOB | NYMEX | 42,000 gal | $131,699 |
| `GC` | Gold | COMEX | 100 oz | $442,550 |
| `SI` | Silver | COMEX | 1,000 oz | $64,770 |
| `HG` | Copper | COMEX | 25,000 lb | $165,775 |
| `PL` | Platinum | NYMEX | 50 oz | $87,290 |
| `PA` | Palladium | NYMEX | 100 oz | $136,600 |
| `ZC` | Corn | CBOT | 5,000 bu | $22,488 |
| `ZS` | Soybeans | CBOT | 5,000 bu | $57,975 |
| `ZM` | Soybean Meal | CBOT | 100 tons | $30,700 |
| `ZL` | Soybean Oil | CBOT | 60,000 lb | $41,244 |
| `ZO` | Oats | CBOT | 5,000 bu | $16,125 |
| `KE` | Wheat HRW | CBOT | 5,000 bu | $36,138 |
| `CC` | Cocoa | NYBOT | 10 tons | $56,360 |
| `KC` | Coffee C | NYBOT | 37,500 lb | $127,369 |
| `SB` | Sugar No.11 | NYBOT | 112,000 lb | $18,614 |
| `CT` | Cotton No.2 | NYBOT | 50,000 lb | $41,575 |
| `OJ` | Orange Juice | NYBOT | 15,000 lb | $20,692 |
| `LE` | Live Cattle | CME | 40,000 lb | $92,400 |
| `GF` | Feeder Cattle | CME | 50,000 lb | $173,200 |

**Notional span 27×** (gold $442,550 to oats $16,125). Book size determines
which markets can hold one contract.

**CBOT, NYBOT and CME markets quote in cents** — `priceMagnifier` 100.
Multipliers and magnifiers are read from IBKR at runtime, never hardcoded.

`MWE` (Minneapolis wheat) removed 2026-08-12: does not resolve on IBKR.
Backtest re-run on the remaining 22.

A narrowing to 17 markets was tested and rejected: carry stopped beating
chance (0.24 → 0.12), drawdown −36% → −53%.

---

## 3. Code layout

Two strategies. `agent_0`–`agent_3` and `xsec_*` filenames are live.
`agent_zero`/`agent_one`/`agent_dsi` and un-prefixed filenames are the retired
natural-gas strategy. `agent_two` exists in both with different meanings.

### Live modules — `src/storage_stress/`

| Module | Responsibility |
|---|---|
| `data/commodities.py` | Panel construction, factor computation, roll-safe returns |
| `data/connectivity.py` | External feeds and R2 mirror (research only) |
| `agents/xsec.py` | Ladder, z-scoring, tercile weights, simulator |
| `execution/xsec_engine.py` | Rebalance and mark; sizing, capital rail, order placement |
| `execution/xsec_signals_ib.py` | Live factor panel from IBKR |
| `execution/xsec_contracts.py` | Contract resolution, multiplier/magnifier, expiry guard |
| `execution/xsec_books.py` | SQLite ledger |
| `monitoring/flex.py` | IBKR Flex client, ledger reconciliation |
| `signal/dsi.py` | Retired DSI pipeline, used by `add_stress` |

### Scripts

| Script | Purpose |
|---|---|
| `run_xsec_live.py` | Live CLI: `--job rebalance\|mark\|status`, `--dry-run`, `--agents` |
| `xsec_scheduler.py` | Always-on loop; fires rebalance and mark as subprocesses |
| `flatten_account.py` | Close all positions; paper-only guard, `--agents` filter |
| `flex_pull.py` | Pull broker statement, reconcile against ledger |
| `verify_xsec_universe.py` | Confirm every market resolves on IBKR |
| `run_xsec.py` | Backtest: ladder plus N-seed null |
| `xsec_robustness.py` | Cost sensitivity, sub-period stability |
| `stress_test.py` | Pre-committed test that rejected the stress factor |
| `carry_drop_test.py` | Pre-committed test that retained carry |
| `md_to_pdf.py` | Markdown → PDF via headless Chromium |

Retired: `run_agents.py`, `run_backtest.py`, `run_daily.py`, `scheduler.py`,
`regime_test.py`, `null_distribution.py`, `compare_instruments.py`.

### Deployment — `deploy/`

| Script | Purpose |
|---|---|
| `bootstrap.sh` | Bare VM → running stack |
| `status.sh` | Container health, gateway state, ledger summary |
| `backup.sh` | Nightly ledger snapshot, verified, 30-day retention |

---

## 4. Architecture

```
VM (Ubuntu 24.04, x86-64, NYC)
├── ib-gateway-0 ── socat :4004 ── IBKR
├── engine        rebalance 10:30 ET, mark 16:30 ET
├── dashboard     Streamlit, 127.0.0.1:8501
└── data/live/    xsec_books.db, logs, backups

ib-gateway-1/2/3 behind the "full" compose profile
```

The live path depends on IBKR only. Signals and execution use the same source.
The R2 mirror feeds backtests; it was measured stale by 43–243 days per market.

IB Gateway binds 4002 to loopback and honours the API handshake only from
`127.0.0.1`. The image runs socat on **4004** forwarding to `127.0.0.1:4002` —
the only port another container can use.

### Schedule

| Job | When |
|---|---|
| Rebalance | Last business day, 10:30 ET |
| Mark | Weekdays, 16:30 ET |
| Backup | Daily, 02:30 ET |

10:30 ET is the only window when all 22 markets are liquid simultaneously.

### Capital adequacy

| Book per agent | Markets tradeable | Weight error |
|---|---|---|
| $250k | 11 / 22 | 31% |
| $2.5M | 22 / 22 | 8.8% |
| $10M | 22 / 22 | 1.9% |

Capacity is `(equity × 0.50) / (2 × n_share × 0.12)`, assuming 12% initial
margin with a 50% buffer. If more than 30% of intended positions round to zero
contracts, the rebalance aborts before sending any order.

Each agent requires its own IBKR paper account. IBKR caps paper at $1,000,000;
four agents sharing one would get $520,833 each, at which GC, HO, GF and HG
round to zero.

---

## 5. Deployment

### Prerequisites

- x86-64 host, 8 GB RAM for four gateways (4 GB for one). IB Gateway has no ARM build.
- One IBKR **paper** login per agent, each a distinct username.
- Each login must authenticate without a phone 2FA prompt.

### Install

```bash
git clone https://github.com/rmcreynolds0/pm-desk-commodities.git storage-stress
cd storage-stress
bash deploy/bootstrap.sh
```

Prompts for the number of accounts and their credentials. Installs Docker, adds
swap, sets the clock to `America/New_York`, firewalls, builds, starts, waits for
a gateway login, installs the backup cron.

### Verify before trading

```bash
docker compose exec engine python scripts/run_xsec_live.py --job rebalance --dry-run
```

Expected:

```
[SIZING] agent_3: equity $1,000,000 — book $2,000,000/agent is within capacity
live factor panel: 22/22 markets from IBKR
```

### Operate

```bash
bash deploy/status.sh                                     # health
docker compose exec engine python scripts/run_xsec_live.py --job status
docker compose logs -f engine                             # scheduler output
ssh -L 8501:localhost:8501 root@<ip>                      # dashboard tunnel
```

Code changes require `docker compose up -d --build`. Config and `.env` changes
require only `docker compose up -d`.

### Adding agents

Append credentials to `.env`, then:

```bash
docker compose --profile full up -d
```

The scheduler launches only agents that have never traded.

---

## 6. Failure modes

| Symptom | Cause |
|---|---|
| Gateway unhealthy | 2FA on the login, or the same username on two gateways |
| `TimeoutError` on connect | Wrong port — use 4004, not 4002 |
| `Error 326: client id in use` | Two connections sharing a client id |
| `Error 10349` then Cancelled | TIF forced to DAY and the session had closed. Orders use GTC + `outsideRth` |
| Orders stay `PreSubmitted` | Market closed; they queue until the next session |
| `ABORTED — N/M positions round to 0` | Account too small. Raise the balance |
| Flex reports 0 positions | Query period excludes today. Set Period to Last 30 Days |
| `missing GITHUB_TOKEN` despite `.env` | Variable not passed into the container in `docker-compose.yml` |

---

## 7. Invariants

- Returns are computed only between two prices of the same contract.
- Factor weights are equal and not fitted.
- `config/xsec.yaml` is frozen while the forward test runs.
- The ledger records filled quantities, never intended ones.
- Feasibility is judged before any order is sent.
- Every decision is recorded, traded or not, with its reason.
- Multipliers and price magnifiers are read from IBKR at runtime.
- Credentials live in `.env`, which is gitignored.

---

Paper trading. No capital is at risk.
