"""
dashboard/app.py — Streamlit portfolio dashboard for the four paper books.
==========================================================================

A READ-ONLY consumer of data/live/books.db (the ledger written by the engine).
It never touches IBKR and never writes to the database, so it is safe to run
alongside the always-on engine (SQLite WAL allows concurrent readers).

Run:
    streamlit run dashboard/app.py
In docker-compose it is the `dashboard` service on port 8501.

Panels (top to bottom):
  1. KPI row     — per-agent equity, total return, max drawdown, open trades.
  2. Equity      — all four agents' equity curves on one axis (the headline).
  3. Ladder      — rolling Sharpe per agent + paired bootstrap Sharpe-difference
                   CIs between adjacent rungs ("does the extra info help?").
  4. Positions   — current open spread per agent with live unrealized P&L.
  5. Trades      — full completed-trade history with exit reasons and P&L.
  6. Signals     — weekly decision history per agent (z, gate, acted, reason).

Everything is derived from the ledger tables; nothing is recomputed from raw
market data, so the dashboard shows exactly what the engine recorded.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml

from storage_stress.agents import bootstrap_sharpe_ci, sharpe
from storage_stress.execution import books

# Repo root = two levels up from this file (dashboard/app.py -> repo).
ROOT = Path(__file__).resolve().parents[1]

# Fixed color per agent so a given book is the same color in every chart —
# the eye tracks one line across panels without re-reading the legend.
AGENT_COLORS = {
    "agent_zero": "#9aa0a6",   # grey — it's the null benchmark
    "agent_one": "#4c8bf5",    # blue
    "agent_two": "#f5a623",    # amber
    "agent_dsi": "#34a853",    # green — the full signal
}
# Adjacent rungs whose difference isolates one added piece of information.
LADDER_PAIRS = [("agent_one", "agent_zero"), ("agent_two", "agent_one"),
                ("agent_dsi", "agent_two")]


# ---------------------------------------------------------------------------
# Data access — cached briefly so reruns (Streamlit re-executes top-to-bottom
# on every interaction) don't hammer SQLite. 30s TTL keeps it near-live.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=30)
def load_config() -> dict:
    with open(ROOT / "config" / "live.yaml") as f:
        return yaml.safe_load(f)


@st.cache_data(ttl=30)
def load_all(db_path: str) -> dict:
    """Pull every ledger table once per refresh into plain DataFrames.

    Returns a dict keyed by agent plus the global trade/decision logs. The
    _mtime argument (passed by the caller) busts the cache when the DB file
    changes, so a fresh mark job shows up within the TTL window.
    """
    con = books.connect(db_path)
    cfg = load_config()
    agents = [a for a, c in cfg["agents"].items() if c.get("enabled")]
    data = {"agents": agents, "capital": {}, "equity": {}, "position": {}}
    for a in agents:
        data["capital"][a] = cfg["agents"][a]["capital"]
        data["equity"][a] = books.equity_curve(con, a)
        data["position"][a] = books.get_position(con, a)
    data["trades"] = books.trade_log(con)
    data["decisions"] = books.decision_log(con)
    con.close()
    return data


def max_drawdown(equity: pd.Series) -> float:
    """Peak-to-trough drawdown as a fraction (e.g. -0.08 = -8%)."""
    if len(equity) < 2:
        return 0.0
    return float((equity / equity.cummax() - 1).min())


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Storage-Stress Agents", layout="wide")
st.title("Storage-Stress — Paper Trading Books")
st.caption("Four agents on an information ladder, one shared IBKR paper account. "
           "Read-only view of data/live/books.db.")

cfg = load_config()
db_path = str(ROOT / cfg["paths"]["books_db"])

# The DB may not exist until the first job runs — guide the user instead of
# crashing with a stack trace.
if not Path(db_path).exists():
    st.warning(
        "No ledger yet at `data/live/books.db`. Run a job first:\n\n"
        "`python scripts/run_agents.py --job decide` (needs IB Gateway), or "
        "explore with `--job mark`. The dashboard populates once the engine "
        "has written decisions and marks.")
    st.stop()

# Bust the cache when the DB file is modified (new mark/decision written).
db_mtime = Path(db_path).stat().st_mtime
data = load_all(db_path)
agents = data["agents"]

# ---- 1. KPI ROW ------------------------------------------------------------
st.subheader("Books at a glance")
cols = st.columns(len(agents))
for col, a in zip(cols, agents):
    eq = data["equity"][a]
    cap = data["capital"][a]
    latest = eq["equity"].iloc[-1] if len(eq) else cap
    ret = (latest / cap - 1) * 100
    dd = max_drawdown(eq["equity"]) * 100 if len(eq) else 0.0
    open_pos = "open" if data["position"][a] is not None else "flat"
    col.markdown(f"**{a}**")
    col.metric("Equity", f"${latest:,.0f}", f"{ret:+.2f}%")
    col.caption(f"max DD {dd:.1f}%  ·  {open_pos}")

# ---- 2. EQUITY CURVES ------------------------------------------------------
st.subheader("Equity curves")
fig = go.Figure()
for a in agents:
    eq = data["equity"][a]
    if len(eq):
        fig.add_trace(go.Scatter(
            x=eq["date"], y=eq["equity"], name=a, mode="lines",
            line=dict(color=AGENT_COLORS.get(a), width=2)))
fig.update_layout(height=420, hovermode="x unified",
                  yaxis_title="Equity ($)", xaxis_title=None,
                  legend=dict(orientation="h", y=1.1),
                  margin=dict(l=10, r=10, t=10, b=10))
st.plotly_chart(fig, use_container_width=True)

# ---- 3. LADDER: rolling Sharpe + paired difference CIs ---------------------
st.subheader("Does the extra information help?")
st.caption("Each rung adds one piece of information. A positive Sharpe "
           "difference whose 90% bootstrap CI excludes zero is evidence that "
           "piece helps. (Needs a few weeks of marks before it stabilizes.)")


def periodic_returns(agent: str) -> pd.Series:
    """Weekly-ish equity returns for an agent, indexed by mark date.

    The mark job runs daily, so we resample to weekly to match the strategy's
    decision cadence before computing Sharpe (avoids overstating the annualization).
    """
    eq = data["equity"][agent]
    if len(eq) < 3:
        return pd.Series(dtype=float)
    s = eq.set_index("date")["equity"].resample("W-FRI").last()
    return s.pct_change().dropna()


left, right = st.columns([1, 1])

with left:
    st.markdown("**Annualized Sharpe (live, weekly)**")
    rows = []
    for a in agents:
        r = periodic_returns(a)
        rows.append({"agent": a,
                     "weeks": len(r),
                     "sharpe": round(sharpe(r), 2) if len(r) > 1 else np.nan})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

with right:
    st.markdown("**Paired Sharpe difference (adjacent rungs)**")
    diff_rows = []
    for hi, lo in LADDER_PAIRS:
        if hi not in agents or lo not in agents:
            continue
        rh, rl = periodic_returns(hi), periodic_returns(lo)
        # Align on common weeks so the difference is paired, not two separate
        # samples — the CI is on the DIFFERENCE of returns week by week.
        joined = pd.concat({"hi": rh, "lo": rl}, axis=1, join="inner").dropna()
        if len(joined) < 5:
            diff_rows.append({"comparison": f"{hi} − {lo}",
                              "Δ sharpe": np.nan, "90% CI": "need ≥5 wks"})
            continue
        d = joined["hi"] - joined["lo"]
        point, (clo, chi) = bootstrap_sharpe_ci(d, n_boot=2000)
        excludes_zero = clo > 0 or chi < 0
        diff_rows.append({
            "comparison": f"{hi} − {lo}",
            "Δ sharpe": round(point, 2),
            "90% CI": f"[{clo:+.2f}, {chi:+.2f}]{'  ✓' if excludes_zero else ''}"})
    st.dataframe(pd.DataFrame(diff_rows), hide_index=True, use_container_width=True)
    st.caption("✓ = CI excludes zero (statistically distinguishable at 90%).")

# ---- 4. OPEN POSITIONS -----------------------------------------------------
st.subheader("Open positions")
pos_rows = []
for a in agents:
    p = data["position"][a]
    if p is None:
        continue
    eq = data["equity"][a]
    unreal = eq["unrealized_pnl"].iloc[-1] if len(eq) else 0.0
    pos_rows.append({
        "agent": a,
        "side": "LONG spread" if p["side"] > 0 else "SHORT spread",
        "contracts": p["contracts"],
        "entry_date": p["entry_date"],
        "entry_px": round(p["entry_px"], 4),
        "front": p["front_leg"], "deferred": p["deferred_leg"],
        "unrealized $": round(unreal, 0)})
if pos_rows:
    st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
else:
    st.info("All books are flat right now.")

# ---- 5. TRADE HISTORY ------------------------------------------------------
st.subheader("Trade history")
trades = data["trades"]
if len(trades):
    show = trades.copy()
    show["side"] = show["side"].map({1: "LONG", -1: "SHORT"})
    show["pnl"] = show["pnl"].round(0)
    show = show[["agent", "side", "contracts", "entry_date", "entry_px",
                 "exit_date", "exit_px", "exit_reason", "held_days", "pnl"]]
    # Filter chip so you can drill into one book.
    pick = st.multiselect("Filter agents", agents, default=agents)
    st.dataframe(show[show["agent"].isin(pick)].iloc[::-1],
                 hide_index=True, use_container_width=True)
else:
    st.info("No completed trades yet.")

# ---- 6. SIGNAL / DECISION HISTORY -----------------------------------------
st.subheader("Weekly signal history")
st.caption("Every decision the engine recorded — including weeks it chose not "
           "to trade, and why. This is the rolling trade-by-trade research log.")
dec = data["decisions"]
if len(dec):
    agent_pick = st.selectbox("Agent", agents, index=len(agents) - 1)
    d = dec[dec["agent"] == agent_pick].copy()
    d["gate"] = d["gate"].map({1: "open", 0: "closed"})
    d["acted"] = d["acted"].map({1: "TRADED", 0: "—"})
    d = d[["decision_date", "side", "z", "gate", "season", "spread_px",
           "contracts_tgt", "acted", "reason"]]
    st.dataframe(d.iloc[::-1], hide_index=True, use_container_width=True)
else:
    st.info("No decisions recorded yet.")
