# Onboarding & Resources — QUANTT Commodities

The practical companion to `02_PROJECT_INTRO_HIRING.md`. That document says
what the project is and who we're looking for. This one says what a new member
actually does in their first fortnight, what accounts and tools they need, and
what the whole thing costs to run.

**Last updated:** 2026-08-27 · **Maintainer:** update this whenever the stack
or the budget changes.

---

## 1. Orientation — read this first

We run a **cross-sectional commodity futures strategy** on 22 markets. It ranks
every commodity against every other on three factors (carry, momentum,
basis-momentum), holds the top third long against the bottom third short, and
rebalances monthly. It trades live on IBKR paper.

The structure that makes it a research project rather than a trading bot is the
**agent ladder**: four agents run side by side, each seeing exactly one more
factor than the one below it. `agent_0` trades at random. The gap between two
adjacent agents is the measured contribution of the single piece of information
that separates them.

The one thing to understand before anything else: **we killed our own first
strategy.** A natural-gas spread model, built over half a summer, was retired
after its own pre-registered regime test falsified the hypothesis behind it.
Three separate bugs had been making it look profitable. Finding that ourselves,
rather than discovering it in live P&L, is the methodology working.

Read in this order:

| # | Document | Why |
|---|---|---|
| 1 | `docs/03_SUMMER_RESULTS.md` | What happened and what the evidence says |
| 2 | `docs/PIVOT_A_STRATEGY.md` | How the current strategy works in detail |
| 3 | `docs/01_TECHNICAL_INFRASTRUCTURE.md` | How the code is organised |
| 4 | `docs/DEPLOY.md` | How the live stack runs |

---

## 2. Day one — get it running locally

You need **Python 3.11+**, **git**, and about twenty minutes. No IBKR account
is needed for this part; the backtest runs entirely offline.

```bash
git clone https://github.com/rmcreynolds0/pm-desk-commodities.git storage-stress
cd storage-stress

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,dashboard,live]"

pytest -q                          # regression tests must pass
python scripts/run_xsec.py         # reproduces the agent ladder
streamlit run dashboard/pivot_a.py # research dashboard on :8501
```

`scripts/run_xsec.py` writes `data/processed/xsec_ladder.csv`. **Your numbers
must match the table in `03_SUMMER_RESULTS.md`.** If they don't, stop and ask —
a reproducibility gap is a finding, not a nuisance.

### What to do next, before writing any code

`run_xsec.py` takes an optional seed count. Run it with a big one and read
step 4 of the output carefully:

```bash
python scripts/run_xsec.py 200     # ladder, then a 200-seed null distribution
```

Step 4 generates the null benchmark: 200 random agents, producing a
*distribution* of outcomes rather than a single path, and reports where each
signal agent lands as a percentile of chance.

Understanding why a single random seed is **not** a benchmark is the most
important idea in this project. A one-seed null is exactly what made a +26.3%
natural-gas result look like signal when it was the 95th percentile of pure
chance — and nearly half of random seeds were profitable on that instrument.

Then stress the result:

```bash
python scripts/xsec_robustness.py  # cost sensitivity, sub-period stability
```

> **Note on the retired code.** `scripts/null_distribution.py` does the same
> job for the *natural-gas* strategy and refers to `agent_zero`. Spelled-out
> agent names (`agent_zero`, `agent_dsi`) belong to the retired NG book;
> numerals (`agent_0` … `agent_3`) are the live cross-sectional ladder. Both
> are kept — the retired code is the evidence behind the retirement — but only
> the numeral set is running.

---

## 3. First fortnight

| Day | Task | Done when |
|---|---|---|
| 1 | Environment up, tests pass, ladder reproduces | Your `xsec_ladder.csv` matches the documented table |
| 2–3 | Read the three core docs; write down five questions | You can explain what basis-momentum measures |
| 4–5 | Trace one rebalance end to end in the code | You can name every file a single order passes through |
| 6–8 | Run the live stack read-only against paper | `deploy/status.sh` output makes sense to you |
| 9–14 | First contribution — a test, a doc fix, or a small analysis | Merged |

**Tracing a rebalance** is the exercise that teaches the system fastest. The
path is:

```
scripts/run_xsec_live.py            CLI entry
  execution/xsec_engine.py          orchestration, sizing, safety rails
    execution/xsec_signals_ib.py    live factor panel from IBKR
    data/commodities.py             carry / mom / basis_mom computation
    agents/xsec.py                  ladder, scoring, tercile weights
    execution/xsec_contracts.py     contract resolution, expiry guards
    execution/xsec_books.py         ledger writes
```

---

## 4. What each role actually does here

Roles are described in `02_PROJECT_INTRO_HIRING.md`. Concretely, in this
codebase:

**Quantitative Research Analyst** — owns `agents/xsec.py`, `data/commodities.py`
and the `scripts/*_test.py` family. A typical project: propose a fourth factor,
add it as `agent_4`, and test whether it beats `agent_3` against the null
distribution. The answer is usually no, and reporting that clearly is the job.

**Data Engineer** — owns `data/`. The live strategy currently takes signals from
IBKR because the R2 mirror was measured stale by 43–243 days per market. Making
that mirror trustworthy enough to drive live decisions is an open, high-value
project.

**Execution & Infrastructure** — owns `execution/`, `deploy/` and `dashboard/`.
The live account reconciliation, the order-splitting logic, the expiry guards,
and the always-on stack. This is where correctness matters most: a bug here
loses money rather than just producing a wrong number.

---

## 5. Technical resources

### Accounts a new member needs

| Resource | Cost | Who provides | Needed by |
|---|---|---|---|
| GitHub account | Free | Self | Everyone, day 1 |
| Python 3.11+ / git | Free | Self | Everyone, day 1 |
| IBKR **paper** account | Free | Self (standalone paper login) | Execution team |
| EIA API key | Free | Self, [eia.gov/opendata](https://www.eia.gov/opendata/) | Data team only |
| FRED API key | Free | Self | Data team only |
| NOAA CDO token | Free | Self | Data team only |
| Cloudflare R2 credentials | Shared | Project lead | Data team only |
| LSEG Datastream | University licence | Queen's | Research, via campus access |

**Credential rules — non-negotiable.** Keys live in `.env`, which is
gitignored. Never hardcode a key as a default value in source; the repo
previously carried literal EIA and NOAA keys as fallbacks, which would have
published working credentials to anyone who cloned it. Never paste a secret
into a chat, an issue, or a screenshot. If a key is exposed, rotate it — issue
a new one, update `.env`, then revoke the old one, in that order.

### The stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.11+ | pandas, numpy, scipy |
| Storage — research | DuckDB + Parquet on Cloudflare R2 | queried over `httpfs` |
| Storage — live | SQLite (`data/live/xsec_books.db`) | WAL mode; the ledger is the source of truth for per-agent attribution |
| Broker API | `ib_async` → IB Gateway | paper only |
| Scheduling | `scripts/xsec_scheduler.py` | self-healing launch |
| Dashboards | Streamlit + Plotly | `pivot_a.py` research, `xsec_live.py` live |
| Deployment | Docker Compose on an x86-64 VM | `deploy/bootstrap.sh` |
| Docs | Markdown → PDF via `scripts/md_to_pdf.py` | Playwright/Chromium |

**One hard constraint worth knowing before you buy anything:** IB Gateway ships
**amd64 only**. There is no ARM build. Oracle Cloud's Always-Free Ampere tier
and Raspberry Pi cannot run this stack, however tempting the price.

---

## 6. Financial resources

The whole thing runs on a rounding error, which is deliberate — a research
project that needs a budget to continue is a research project that stops when
the budget does.

### Recurring

| Item | Cost | Notes |
|---|---|---|
| VM — Hetzner CX22 (2 vCPU / 4 GB, x86-64) | ≈ **CAD 7/mo** | Cheapest host that actually runs IB Gateway |
| Cloudflare R2 | **CAD 0** at current volume | 10 GB storage and egress sit inside the free tier |
| IBKR paper account | **CAD 0** | Paper only; no capital at risk |
| EIA / FRED / NOAA APIs | **CAD 0** | Free public keys |
| GitHub | **CAD 0** | Private repos are free |
| LSEG Datastream | **CAD 0** to us | Queen's university licence |
| **Total** | **≈ CAD 84 / year** | |

### One-time alternative

| Item | Cost | Trade-off |
|---|---|---|
| x86 mini PC (Intel N100, 8 GB) | ≈ **CAD 200** once | No recurring fee, but needs mains power and a stable home network; pays back against the VM in ~2.5 years |

### Explicitly *not* in the budget

- **Trading capital.** Everything is paper. The IBKR paper balance is virtual
  and is set by an account reset, not funded.
- **Market data subscriptions.** Delayed-frozen data is sufficient for a
  monthly rebalance, and it's free.
- **Cloud compute for backtests.** The full 26-year, 22-market ladder runs in
  minutes on a laptop.

### If the project scales

The realistic cost drivers, in order: a second VM for redundancy (+CAD 84/yr),
R2 storage past 10 GB (about CAD 0.02/GB-month), and real-time market data if a
future strategy ever needs intraday signals (IBKR bundles run roughly CAD
15–60/month depending on exchange). None of these are needed now.

*Costs are estimates as of August 2026 and should be re-checked before anyone
relies on them for a funding request.*

---

## 7. Operating cadence

| When | What | Owner |
|---|---|---|
| Weekdays 16:30 ET | Daily mark, automatic | Engine |
| Last business day, 10:30 ET | Monthly rebalance, automatic | Engine |
| Nightly 02:30 ET | Ledger backup, automatic | `deploy/backup.sh` |
| Weekly | Check `deploy/status.sh`; confirm marks are current | Infrastructure |
| Monthly | Review the rebalance decision log, including untraded rows | Research |
| Termly | Update `03_SUMMER_RESULTS.md` with the forward record | Everyone |

**The forward test asks one pre-committed question: do the rungs stay in
order?** If `agent_3` keeps beating `agent_2`, which beats `agent_1`, which
beats random, the ladder held out of sample. If the order scrambles, the
backtest was fitted, and we report that — exactly as we did with the gas trade.

Nothing about that question is allowed to change while the test runs. Every
parameter is frozen in `config/xsec.yaml`; changing one invalidates the forward
record from that date and must be logged in `docs/PIVOT_A_STRATEGY.md`.

---

## 8. Getting help

- **Something doesn't reproduce** — say so immediately. It is a finding.
- **Stack is down** — `deploy/status.sh` first, then `docker compose logs engine`.
- **Stuck for more than an hour** — ask. Nobody is expected to know futures
  microstructure, Docker, and factor econometrics on arrival.

*Related: `01_TECHNICAL_INFRASTRUCTURE.md` · `02_PROJECT_INTRO_HIRING.md` ·
`03_SUMMER_RESULTS.md` · `DEPLOY.md`*
