# pm-desk-commodities

Cross-sectional commodity futures strategy. 22 markets, long/short,
dollar-neutral, monthly rebalance. Runs live on IBKR paper.

Sharpe 0.553 over 2000–2026, 99.5th percentile of a 200-seed null distribution.

---

## Strategy

Markets are ranked against each other on three factors derived from the futures
curve. The top third is held long, the bottom third short, equally weighted
within each side.

| Factor | Definition | Measures |
|---|---|---|
| Carry | `ln(F₁/F₂) / Δt` | Curve slope |
| Momentum | 12-month return of `F₁` | Price trend |
| Basis-momentum | `mom(F₁) − mom(F₂)` | Curve shape change |

Factors are z-scored cross-sectionally within each date and combined with equal
weights. Volatility targeted to 10% annualised, leverage capped at 3×.

### Agent ladder

Four agents run on identical machinery, differing only in which factors they
read.

| Agent | Factors | Sharpe | vs null |
|---|---|---|---|
| `agent_0` | none (random) | 0.08 | — |
| `agent_1` | carry | 0.24 | 98th pctile |
| `agent_2` | + momentum | 0.36 | 100th |
| `agent_3` | + basis-momentum | 0.553 | 99.5th |

`agent_3` trades live. The others require additional IBKR paper accounts.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,dashboard,live]"

pytest -q                          # 83 tests
python scripts/run_xsec.py 200     # ladder + 200-seed null
streamlit run dashboard/pivot_a.py   # research views
```

Backtests read a historical mirror from Cloudflare R2 and require credentials
in `.env`. Live trading does not: signals are computed from IBKR.

---

## Deployment

```bash
git clone https://github.com/rmcreynolds0/pm-desk-commodities.git storage-stress
cd storage-stress
bash deploy/bootstrap.sh
```

Requires an x86-64 host (IB Gateway has no ARM build) and an IBKR paper login
without phone 2FA. See `docs/TECHNICAL.md`.

---

## Layout

```
config/xsec.yaml            Frozen strategy specification
src/storage_stress/
  data/commodities.py       Panel construction, factor computation
  data/connectivity.py      External feeds, R2 mirror (research only)
  agents/xsec.py            Ladder, scoring, portfolio weights
  execution/xsec_engine.py  Live rebalance and mark
  execution/xsec_books.py   SQLite ledger
  execution/xsec_contracts.py  IBKR contract resolution
  monitoring/flex.py        Broker reconciliation
scripts/                    CLIs and research drivers
deploy/                     Provisioning, status, backup
dashboard/                  Streamlit views
tests/                      pytest
```

Two strategies exist in this repository. `agent_0`–`agent_3` and `xsec_*`
filenames are the live cross-sectional strategy. `agent_zero`, `agent_one`,
`agent_dsi` and un-prefixed filenames are the retired natural-gas strategy,
kept as the record behind its retirement.

---

## Operations

| Job | Schedule |
|---|---|
| Rebalance | Last business day, 10:30 ET |
| Mark | Weekdays, 16:30 ET |
| Ledger backup | Daily, 02:30 ET |

```bash
python scripts/run_xsec_live.py --job rebalance --dry-run
python scripts/run_xsec_live.py --job status
bash deploy/status.sh
```

---

## Documentation

| Document | Contents |
|---|---|
| `docs/TECHNICAL.md` | Markets, contracts, signals, module and script inventory, deployment |
| `docs/05_STRATEGY_PAPER.md` | Market mechanics, factor derivation, methodology |

PDFs in `docs/pdf/`, regenerated with `python scripts/md_to_pdf.py <file>`.

---

## Conventions

- Returns are computed only between two prices of the same contract.
- Factor weights are equal and not fitted.
- `config/xsec.yaml` is frozen while the forward test runs.
- The ledger records filled quantities, not intended ones.
- Contract multipliers and price magnifiers are read from IBKR at runtime.
- Credentials live in `.env`, which is gitignored.

---

Paper trading. Backtest figures are simulated; forward figures come from the
IBKR paper ledger. No capital is at risk.
