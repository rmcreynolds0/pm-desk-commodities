# SPRINT PLANNER — Summer (12 weeks) + School Year

Platform for the year is still being finalized, so this planner is written to be
**portable and shareable**: it tracks work as plain milestones with owners and
exit criteria, not tied to any one project-management tool. Copy it into Notion,
GitHub Projects, a Trello board, or a spreadsheet — the structure carries over.

## How to read this
- Each week has a **goal**, **deliverables**, and a **done-when** gate.
- 🚦 marks a hard decision gate that can pause or rescope the project.
- The first four weeks exist to get *something* trading; the full DSI is built only
  after the MVP is live.

---

### Summer — 12 weeks

| Week | Goal | Deliverables | Done when |
|------|------|--------------|-----------|
| 1 | 🚦 Regime test | Statistical test: do post-2016 NG spreads behave differently from pre-2016? | Test run + written verdict. **If it fails, pause and rescope.** |
| 2 | Data + Agent Zero live | IBKR paper account; EIA + macro ingestion; monitoring log; **Agent Zero deployed** | Agent Zero placing/exiting paper trades; fills reconcile |
| 3 | Harden ingestion | DuckDB store; scheduled EIA pulls; data-health checks | Thursday storage print auto-ingests; gaps alert |
| 4 | 🚦 Agent One live (MVP) | Salt-flow z-score signal; **Agent One deployed** | Minimum viable strategy trading paper; this is the floor deliverable |
| 5 | Pipeline scrapers I | Top-priority Gulf Coast EBB scrapers + manual-download fallback | ≥2 pipelines parsing; uptime KPI tracked |
| 6 | Pipeline scrapers II + LNG | More scrapers; LNG feedgas estimator | Parse-failure rate < 2 days/rolling-week |
| 7 | DSI v1 | Build raw DSI → convex transform → smoothing → deseasonalize | DSI series computed over full history |
| 8 | Residualization | Rolling-OLS basis residualization → DSI_clean | DSI_clean produced; no lookahead verified |
| 9 | 🚦 Ablation study | Which DSI pieces survive out-of-sample? | Stage-by-stage OOS contribution table |
| 10 | 🚦 Orthogonality test | Does DSI_clean predict spread but **not** basis? | Pass → DSI eligible; fail-small → rebuild upstream; fail-wide → DSI = research, ship Agent One |
| 11 | Cut surviving signal to paper | Promote DSI to live paper if tests passed | DSI trading paper under the locked rules |
| 12 | Harden + handoff | Monitoring, alerting, docs, handoff materials | Someone else could run it from the docs |

### School year — 7–8 months, 10–20 hrs/week

**Weekly operations:** data health, signal review, fill reconciliation, risk monitoring.

**Quarterly improvement projects (one variable at a time):**
- Q1: better spread selection (test the fixed-spread variant).
- Q2: better basis residualization (more upstream variables: linepack, LNG noms).
- Q3: better weather controls (degree-day regime weighting).
- Q4: stronger slippage / execution modeling.

---

## What I'm keeping for myself (portable, shareable)
A single **markdown changelog + KPI sheet** committed to the repo, updated weekly:
`date | scraper uptime % | parse failures | trades | live Sharpe (rolling 24m) | drawdown | notes`.
It is plain text, lives in git, and drops into any platform we pick for the year.
That way the project's status is always one file, shareable with a mentor or
reviewer without exporting from a proprietary tool.

## KPIs tracked from week 1
Scraper uptime, parse-failure rate, trades/year (target ≥30), rolling-24m Sharpe
(CI lower bound > 0.5 target), max drawdown (< 12%), hit rate (55–62%), |correlation
to outright NG| (< 0.2).
