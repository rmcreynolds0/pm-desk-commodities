"""
dashboard/xsec_live.py — LIVE portfolio dashboard for the Pivot A agent ladder.
==============================================================================

This is the live counterpart to dashboard/pivot_a.py:

    pivot_a.py   RESEARCH  — reads backtest CSVs in data/processed/
    xsec_live.py LIVE      — reads the paper ledger data/live/xsec_books.db

WHY A SEPARATE FILE RATHER THAN A FLAG ON pivot_a.py
    A dashboard that silently falls back from live data to backtest data is
    dangerous: you cannot tell by looking whether the equity curve on screen
    is a real forward record or a simulation. Keeping them as two pages means
    the URL you are on tells you which one you are reading.

WHAT IT SHOWS, and why each panel exists
    Ladder        Equity of all four agents on one axis. This is the whole
                  point of the project: agent_0 is a random null and each rung
                  adds exactly one factor, so the VERTICAL GAPS between the
                  curves are the live estimate of what each factor contributes.
    Positions     What each agent holds right now, with live unrealised P&L.
    Decisions     Every decision the engine made, INCLUDING the ones it chose
                  not to trade and the reason. An untraded decision (e.g.
                  "rounds to 0 contracts") is a data point about capital
                  adequacy, not a blank to be hidden.
    Trades        Completed round trips.

READ-ONLY. This page never writes to the ledger. The engine is the only
writer; a dashboard that can mutate the trading record is a liability.

Run locally:
    streamlit run dashboard/xsec_live.py --server.port 8501
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Agent display order and colour. Fixed rather than derived so the ladder
# always reads bottom-to-top in information order, and so a given agent keeps
# its colour across every chart on the page.
AGENT_ORDER = ["agent_0", "agent_1", "agent_2", "agent_3"]
AGENT_COLOR = {
    "agent_0": "#8b8b8b",   # grey — the null benchmark, deliberately muted
    "agent_1": "#4c9be8",
    "agent_2": "#f0a202",
    "agent_3": "#2ecc71",   # green — the full signal
}
AGENT_LABEL = {
    "agent_0": "agent_0 · random (null)",
    "agent_1": "agent_1 · + carry",
    "agent_2": "agent_2 · + momentum",
    "agent_3": "agent_3 · + basis-momentum",
}


# ---------------------------------------------------------------------------
# DATA ACCESS
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60)
def load_config() -> dict:
    """Read the frozen spec. Cached for 60s so an edit shows up without a
    restart, but we are not re-parsing YAML on every widget interaction."""
    with open(ROOT / "config" / "xsec.yaml") as f:
        return yaml.safe_load(f)


def db_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["books_db"]


@st.cache_data(ttl=30)
def read_table(path_str: str, query: str) -> pd.DataFrame:
    """Run a read-only query against the ledger.

    OPENED read-only VIA URI. The engine is a concurrent writer using WAL, so
    the dashboard must not take a write lock — `mode=ro` guarantees that even
    if a future edit to this file introduces a stray INSERT.

    Cached for 30s: the ledger only changes at the daily mark and the monthly
    rebalance, so hammering it on every rerun buys nothing.

    Returns an EMPTY DataFrame if the ledger does not exist yet, which is the
    normal state before the first rebalance rather than an error.
    """
    p = Path(path_str)
    if not p.exists():
        return pd.DataFrame()
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
        try:
            return pd.read_sql_query(query, con)
        finally:
            con.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        # Table missing => the engine has not created it yet. Same meaning as
        # "no data", so present it that way instead of a stack trace.
        return pd.DataFrame()


def fmt_money(x: float) -> str:
    return f"${x:,.0f}"


# ---------------------------------------------------------------------------
# PAGE
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Pivot A — Live Agent Ladder",
                   page_icon="📈", layout="wide")

cfg = load_config()
DB = db_path(cfg)
DBS = str(DB)

st.title("Pivot A — Live Agent Ladder")
st.caption(
    "Cross-sectional commodity futures · IBKR **paper** · four agents sharing "
    "one account, separated by `orderRef` tag and the local ledger."
)

# --- LIVE-OR-NOT BANNER ----------------------------------------------------
# The single most important thing this page communicates is whether there is a
# real forward record yet. Showing an empty chart without saying why is how a
# reader concludes "the strategy lost money" when in fact it never traded.
marks = read_table(DBS, "SELECT * FROM marks ORDER BY date")
positions = read_table(DBS, "SELECT * FROM positions")

if not DB.exists():
    st.error(
        f"**No ledger yet** — `{cfg['paths']['books_db']}` does not exist. "
        "The engine creates it on its first run. Nothing has traded."
    )
elif marks.empty:
    st.warning(
        "**Ledger exists but is empty** — the engine has not recorded a mark "
        "yet. Expected before the first rebalance."
    )
else:
    span = f"{marks['date'].min()} → {marks['date'].max()}"
    st.success(f"**Live record:** {marks['date'].nunique()} marked days  ·  {span}")

# --- HEADLINE NUMBERS ------------------------------------------------------
if not marks.empty:
    latest_date = marks["date"].max()
    latest = marks[marks["date"] == latest_date].set_index("agent")

    cols = st.columns(len(AGENT_ORDER))
    for col, agent in zip(cols, AGENT_ORDER):
        if agent not in latest.index:
            col.metric(AGENT_LABEL[agent], "—", "no data")
            continue
        row = latest.loc[agent]
        # Return is measured against the agent's STARTING capital, not against
        # the account's NetLiq — four books share one account, so account-level
        # equity is a blend and is useless for per-agent attribution.
        cap = read_table(DBS, f"SELECT capital FROM agents WHERE name='{agent}'")
        start = float(cap.iloc[0, 0]) if not cap.empty else float("nan")
        ret = (row["equity"] / start - 1.0) * 100 if start else float("nan")
        col.metric(
            AGENT_LABEL[agent],
            fmt_money(row["equity"]),
            f"{ret:+.2f}%" if pd.notna(ret) else "—",
        )

st.divider()

# ---------------------------------------------------------------------------
# 1. THE LADDER
# ---------------------------------------------------------------------------
st.subheader("1 · The ladder — equity by agent")
st.caption(
    "Each agent sees exactly one more factor than the one below it. The gap "
    "between adjacent curves is the live contribution of that factor. "
    "agent_0 is random: it is the null the others must beat to be interesting."
)

if marks.empty:
    st.info("No marks recorded yet — the ladder appears after the first mark.")
else:
    # Normalise each agent to 100 at its first mark. Absolute dollars are not
    # comparable across agents if their books were ever sized differently;
    # indexed curves always are, and the ladder is a relative claim.
    fig = go.Figure()
    for agent in AGENT_ORDER:
        s = marks[marks["agent"] == agent].sort_values("date")
        if s.empty:
            continue
        idx = s["equity"] / s["equity"].iloc[0] * 100.0
        fig.add_trace(go.Scatter(
            x=s["date"], y=idx, name=AGENT_LABEL[agent],
            mode="lines",
            line=dict(color=AGENT_COLOR[agent],
                      width=3 if agent == "agent_3" else 2,
                      dash="dot" if agent == "agent_0" else "solid"),
        ))
    # Reference line at 100 = flat. Without it the eye cannot tell profit from
    # loss on an indexed axis.
    fig.add_hline(y=100, line=dict(color="#888", width=1, dash="dash"))
    fig.update_layout(
        height=430, hovermode="x unified",
        yaxis_title="equity (indexed, first mark = 100)",
        xaxis_title=None, margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Why the curves may look identical early on"):
        st.markdown(
            "Agents differ only in **which markets they pick**, not in sizing "
            "or timing. Over a handful of days, four dollar-neutral commodity "
            "books rebalanced monthly will move together because they share "
            "the same market beta. The ladder needs **months**, not days, "
            "before the gaps mean anything. Treat anything under ~30 marked "
            "days as an infrastructure check, not a result."
        )

st.divider()

# ---------------------------------------------------------------------------
# 2. CURRENT POSITIONS
# ---------------------------------------------------------------------------
st.subheader("2 · Current positions")

if positions.empty:
    st.info("Flat — no open positions in the ledger.")
else:
    n_by_agent = positions.groupby("agent")["ticker"].count()
    st.caption(
        "Open positions per agent: "
        + " · ".join(f"**{a}** {int(n_by_agent.get(a, 0))}" for a in AGENT_ORDER)
    )
    tabs = st.tabs([a for a in AGENT_ORDER])
    for tab, agent in zip(tabs, AGENT_ORDER):
        with tab:
            p = positions[positions["agent"] == agent].copy()
            if p.empty:
                st.write("Flat.")
                continue
            # Notional per contract divides by the magnifier: cents-quoted
            # markets (grains, softs, cattle) would otherwise read 100x large.
            p["notional_per_lot"] = (
                p["entry_px"] * p["multiplier"] / p["magnifier"])
            p["gross_notional"] = p["notional_per_lot"] * p["contracts"].abs()
            p["side"] = p["contracts"].apply(lambda c: "LONG" if c > 0 else "SHORT")
            show = p[["ticker", "side", "contracts", "entry_date", "entry_px",
                      "local_symbol", "expiry", "gross_notional"]]
            st.dataframe(
                show.sort_values("ticker"),
                use_container_width=True, hide_index=True,
                column_config={
                    "gross_notional": st.column_config.NumberColumn(
                        "gross notional", format="$%.0f"),
                    "entry_px": st.column_config.NumberColumn(format="%.4f"),
                },
            )
            st.caption(f"Gross notional: **{fmt_money(p['gross_notional'].sum())}**")

st.divider()

# ---------------------------------------------------------------------------
# 3. DECISIONS — INCLUDING THE UNTRADED ONES
# ---------------------------------------------------------------------------
st.subheader("3 · Decision log")
st.caption(
    "Every decision, traded or not. Rows where `actual_contracts` is 0 but "
    "`target_weight` is non-zero are positions the book wanted but could not "
    "take — usually whole-lot rounding at the current account size. These are "
    "recorded rather than hidden because they measure capital adequacy."
)

decisions = read_table(
    DBS, "SELECT * FROM decisions ORDER BY rebal_date DESC, agent, rank")

if decisions.empty:
    st.info("No decisions recorded yet.")
else:
    rebals = sorted(decisions["rebal_date"].unique(), reverse=True)
    pick = st.selectbox("Rebalance date", rebals, index=0)
    d = decisions[decisions["rebal_date"] == pick]

    # Surface the dropped-position count first — it is the number that tells
    # you whether the account is big enough to run the strategy as designed.
    wanted = d[d["target_weight"].fillna(0) != 0]
    dropped = wanted[wanted["actual_contracts"] == 0]
    if len(wanted):
        pct = len(dropped) / len(wanted) * 100
        (st.error if pct > 30 else st.info)(
            f"**{len(dropped)} of {len(wanted)}** intended positions "
            f"({pct:.0f}%) rounded to zero contracts on {pick}."
            + ("  The engine aborts above 30% — the book is under-capitalised "
               "for this universe." if pct > 30 else "")
        )

    st.dataframe(
        d[["agent", "ticker", "rank", "score", "side", "target_weight",
           "target_contracts", "actual_contracts", "price", "reason"]],
        use_container_width=True, hide_index=True,
        column_config={
            "score": st.column_config.NumberColumn(format="%.3f"),
            "target_weight": st.column_config.NumberColumn(format="%.4f"),
            "target_contracts": st.column_config.NumberColumn(format="%.2f"),
            "price": st.column_config.NumberColumn(format="%.4f"),
        },
    )

st.divider()

# ---------------------------------------------------------------------------
# 4. COMPLETED TRADES
# ---------------------------------------------------------------------------
st.subheader("4 · Completed round trips")

trades = read_table(DBS, "SELECT * FROM trades ORDER BY exit_date DESC")
if trades.empty:
    st.info("No completed round trips yet — positions are held between monthly "
            "rebalances, so the first closes land at the next rebalance.")
else:
    c1, c2, c3 = st.columns(3)
    c1.metric("Round trips", f"{len(trades):,}")
    c2.metric("Realised P&L", fmt_money(trades["pnl"].sum()))
    c3.metric("Win rate", f"{(trades['pnl'] > 0).mean() * 100:.0f}%")
    st.dataframe(
        trades[["agent", "ticker", "contracts", "entry_date", "entry_px",
                "exit_date", "exit_px", "exit_reason", "pnl"]],
        use_container_width=True, hide_index=True,
        column_config={"pnl": st.column_config.NumberColumn(format="$%.0f")},
    )

# ---------------------------------------------------------------------------
# FOOTER — provenance
# ---------------------------------------------------------------------------
st.divider()
st.caption(
    f"Ledger: `{cfg['paths']['books_db']}`  ·  "
    f"universe: {len(cfg['universe'])} markets  ·  "
    f"rebalance: {cfg['schedule']['rebalance_day']} "
    f"{cfg['schedule']['rebalance_time']} {cfg['schedule']['timezone']}  ·  "
    f"mark: {cfg['schedule']['mark_time']} daily.  "
    "Read-only view; the engine is the sole writer."
)
