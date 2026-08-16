# Storage Stress — NYMEX Henry Hub Natural Gas Calendar Spreads

A systematic strategy that trades natural gas futures calendar spreads off a
Deliverability Stress Index (DSI) built from free public storage data. Everything
runs today on seeded synthetic data with no API key and no broker; it switches to
live by setting one environment variable and starting IB Gateway.

## Layout

```
storage-stress/
├── pyproject.toml          # deps + packaging (pip install -e .)
├── .env.example            # copy to .env, add keys (.env is gitignored)
├── config/
│   ├── settings.yaml       # pre-registered strategy parameters
│   └── instruments.yaml    # contract specs + EIA series IDs
├── src/storage_stress/     # the importable package
│   ├── data/               # connectivity.py — every feed + IBKR + synthetic fallback
│   ├── signal/             # dsi.py — the DSI pipeline (sections 4-6)
│   ├── agents/             # agents.py, agent_zero.py — Zero/One/DSI on shared code
│   ├── execution/          # (scaffold) IBKR order routing — build in summer wk 2-4
│   ├── backtest/           # (scaffold) walk-forward engine — wk 5-10
│   └── monitoring/         # (scaffold) KPI logging + alerts — wk 12
├── notebooks/              # exploration only; import from src, never the reverse
│   ├── 01_connectivity.ipynb
│   ├── 02_concepts.ipynb
│   └── scratch/            # throwaway, gitignored
├── data/                   # all gitignored; raw is immutable, rest is regenerable
│   ├── raw/  interim/  processed/  live/
├── scripts/run_daily.py    # the scheduled Thursday job
├── tests/                  # pytest; mirrors src/
└── docs/                   # LOCKED_STRATEGY.md, SPRINT_PLANNER.md
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # or your env manager
pip install -e ".[dev]"                                # editable install + dev tools
pytest                                                 # 5 tests, all green on synthetic data
python scripts/run_daily.py --mode research            # compute signal, log it, no orders

cp .env.example .env                                   # then add your free EIA key
#   EIA_API_KEY from https://www.eia.gov/opendata/  -> same code returns LIVE data

pip install -e ".[live]"                               # ib_async, for paper trading
#   start IB Gateway in PAPER mode, then:
python scripts/run_daily.py --mode paper
```

## Conventions worth keeping

- `raw/` data is never edited; everything downstream rebuilds from it. EIA prints
  get revised, so keep the original download and reconcile in `interim/`.
- Logic lives in `src/` (tested, reusable); notebooks only *look* at things.
- Secrets in `.env` only — it's the first line of `.gitignore`. Tunable parameters
  in `config/*.yaml` so the strategy changes without code edits and stays auditable.
- `scripts/` are thin wrappers over `src/`; the scheduler stays dumb.
- Use `ib_async`, not the proposal's `ib_insync` (unmaintained since early 2024).

See `docs/LOCKED_STRATEGY.md` for the frozen strategy spec and `docs/SPRINT_PLANNER.md`
for the 12-week build plan.
