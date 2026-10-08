"""
dashboard/xsec_live.py — LIVE portfolio dashboard for the Pivot A agent ladder.
==============================================================================

The live counterpart to dashboard/pivot_a.py:

    pivot_a.py    RESEARCH — reads backtest CSVs in data/processed/
    xsec_live.py  LIVE     — reads the paper ledger data/live/xsec_books.db

WHY TWO FILES RATHER THAN ONE WITH A TOGGLE
    A dashboard that silently falls back from live data to backtest data is
    dangerous: you cannot tell by looking whether the equity curve on screen is
    a real forward record or a simulation. Two pages means the URL you are on
    tells you which one you are reading.

LAYOUT — ordered by the questions you actually ask, most urgent first:

    0  STATUS      Is it alive, has it launched, is anything wrong?
    1  LADDER      Are the rungs in order? This is the pre-committed research
                   question the whole project exists to answer.
    2  AGENTS      Per-agent equity, return, exposure.
    3  POSITIONS   What is held right now.
    4  DECISIONS   Every decision INCLUDING untraded ones and the reason.
    5  TRADES      Completed round trips.

READ-ONLY. The engine is the sole writer. The ledger is opened with
`mode=ro` so this page cannot take a write lock even if a future edit
introduces a stray INSERT.

Run locally:
    streamlit run dashboard/xsec_live.py --server.port 8501
TRAPS
-----
READ-ONLY, AND ENFORCED. The ledger is opened with `mode=ro` via URI so this
page cannot take a write lock from the engine even if a future edit introduces
a stray INSERT.

DO NOT MERGE THIS WITH pivot_a.py. That page renders BACKTEST csvs. A dashboard
that silently falls back from live data to simulated data is worse than no
dashboard, because nothing on screen tells you which you are looking at.

UNTRADED DECISIONS ARE SHOWN ON PURPOSE. A wall of "rounds to 0 contracts" is
the signal that the account is too small, and hiding it hides the main
deployment blocker.

RETURNS ARE MEASURED AGAINST EACH AGENT'S OWN STARTING CAPITAL, never the
account NetLiq, which is a blend of all agents.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parents[1]

AGENT_ORDER = ["agent_0", "agent_1", "agent_2", "agent_3"]
AGENT_COLOR = {
    "agent_0": "#8E9A9D",
    "agent_1": "#5FA3A0",
    "agent_2": "#2F8F7C",
    "agent_3": "#177A63",
}
AGENT_ADDS = {
    "agent_0": "random — sees nothing",
    "agent_1": "+ carry",
    "agent_2": "+ momentum",
    "agent_3": "+ basis-momentum",
}

OK_C, WARN_C, BAD_C = "#177A63", "#B7791F", "#A6462E"


@st.cache_data(ttl=60)
def load_config() -> dict:
    with open(ROOT / "config" / "xsec.yaml") as f:
        return yaml.safe_load(f)


@st.cache_data(ttl=30)
def read_table(path_str: str, query: str) -> pd.DataFrame:
    """Read-only query against the ledger.

    Cached 30s: the ledger changes at the daily mark and monthly rebalance
    only, so re-reading on every widget interaction buys nothing.

    Returns an EMPTY frame when the ledger or table does not exist — that is
    the normal pre-launch state, not an error, and is presented as such.
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
        return pd.DataFrame()


def money(x) -> str:
    return "—" if pd.isna(x) else f"${x:,.0f}"


def chip(label: str, tone: str = "neutral") -> str:
    """A small status pill. State is encoded in colour AND text, never colour
    alone — the page has to survive being read by someone colour-blind and by
    someone printing it in greyscale."""
    tones = {
        "ok":      (OK_C, "rgba(23,122,99,.13)"),
        "warn":    (WARN_C, "rgba(183,121,31,.15)"),
        "bad":     (BAD_C, "rgba(166,70,46,.14)"),
        "neutral": ("#7C8A8D", "rgba(124,138,141,.14)"),
    }
    fg, bg = tones.get(tone, tones["neutral"])
    return (f"<span style='background:{bg};color:{fg};padding:.2rem .6rem;"
            f"border-radius:3px;font-size:.78rem;font-weight:600;"
            f"letter-spacing:.02em;white-space:nowrap'>{label}</span>")


st.set_page_config(page_title="Pivot A — Live Ladder",
                   page_icon="📈", layout="wide")

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; max-width: 1500px;}
  /* Tabular figures everywhere numbers line up in columns. */
  [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;
                                 font-size: 1.55rem;}
  [data-testid="stMetricLabel"] {font-size: .78rem; letter-spacing: .04em;}
  /* Section rules that read as structure rather than decoration. */
  h3 {margin-top: .4rem !important;}
  .sect {border-top: 1px solid rgba(128,128,128,.25);
         margin: 2.2rem 0 1.1rem; padding-top: 1.1rem;}
  .sect-n {font-size: .72rem; letter-spacing: .14em; text-transform: uppercase;
           opacity: .55; margin-bottom: .15rem;}
  .sect-h {font-size: 1.32rem; font-weight: 700; line-height: 1.2;}
  .sect-s {font-size: .88rem; opacity: .7; margin-top: .3rem; max-width: 82ch;}
  /* Captions default to full container width, which at 1500px is far past a
     readable measure. Constrain them the same way body prose is constrained. */
  [data-testid="stCaptionContainer"] {max-width: 86ch;}
  .agentcard {border: 1px solid rgba(128,128,128,.25); border-radius: 4px;
              padding: .85rem .95rem; height: 100%;}
  .agentcard .nm {font-weight: 700; font-size: .95rem;}
  .agentcard .ad {font-size: .76rem; opacity: .65; margin-bottom: .5rem;}
  .agentcard .eq {font-size: 1.4rem; font-weight: 700;
                  font-variant-numeric: tabular-nums; line-height: 1.15;}
  .agentcard .sub {font-size: .76rem; opacity: .7; margin-top: .2rem;}
</style>
""", unsafe_allow_html=True)


def section(n: str, head: str, sub: str = "") -> None:
    st.markdown(
        f"<div class='sect'><div class='sect-n'>{n}</div>"
        f"<div class='sect-h'>{head}</div>"
        + (f"<div class='sect-s'>{sub}</div>" if sub else "")
        + "</div>", unsafe_allow_html=True)


cfg = load_config()
DBS = os.environ.get("XSEC_DB", str(ROOT / cfg["paths"]["books_db"]))
DB_EXISTS = Path(DBS).exists()

marks = read_table(DBS, "SELECT * FROM marks ORDER BY date")
positions = read_table(DBS, "SELECT * FROM positions")
agents_tbl = read_table(DBS, "SELECT name, capital FROM agents")
decisions = read_table(
    DBS, "SELECT * FROM decisions ORDER BY rebal_date DESC, agent, rank")
trades = read_table(DBS, "SELECT * FROM trades ORDER BY exit_date DESC")

CAPITAL = (dict(zip(agents_tbl["name"], agents_tbl["capital"]))
           if not agents_tbl.empty else {})

st.markdown("## Pivot A — Live Agent Ladder")
st.caption(
    "Cross-sectional commodity futures · IBKR **paper** · four agents sharing "
    "one account, separated by `orderRef` tag and this ledger."
)

if not DB_EXISTS:
    state_chip, state_msg = chip("NOT STARTED", "bad"), (
        f"`{cfg['paths']['books_db']}` does not exist. The engine creates it on "
        "its first run. Nothing has traded.")
elif agents_tbl.empty:
    state_chip, state_msg = chip("NOT LAUNCHED", "warn"), (
        "Ledger exists but no agents are registered — the first rebalance has "
        "not run. The scheduler retries every weekday at "
        f"{cfg['schedule']['rebalance_time']} ET while the book is empty.")
elif positions.empty and marks.empty:
    state_chip, state_msg = chip("REGISTERED, FLAT", "warn"), (
        "Agents exist but hold nothing and no marks are recorded yet.")
elif marks.empty:
    state_chip, state_msg = chip("HOLDING, UNMARKED", "warn"), (
        "Positions are open but no daily mark has been written yet. The mark "
        f"job runs weekdays at {cfg['schedule']['mark_time']} ET.")
else:
    last = marks["date"].max()
    age = None
    try:
        age = (dt.date.today() - dt.date.fromisoformat(str(last)[:10])).days
    except ValueError:
        pass
    if age is not None and age > 4:
        state_chip, state_msg = chip("STALE", "bad"), (
            f"Last mark was **{last}**, {age} days ago. The mark job may have "
            "stopped — check `docker compose logs engine`.")
    else:
        state_chip, state_msg = chip("LIVE", "ok"), (
            f"{marks['date'].nunique()} marked days · "
            f"{marks['date'].min()} → {marks['date'].max()}")

c1, c2 = st.columns([1, 5])
c1.markdown(state_chip, unsafe_allow_html=True)
c2.markdown(state_msg)

if not decisions.empty:
    latest_rb = decisions["rebal_date"].max()
    d0 = decisions[decisions["rebal_date"] == latest_rb]
    wanted = d0[d0["target_weight"].fillna(0) != 0]
    zeroed = wanted[wanted["actual_contracts"] == 0]
    if len(wanted):
        pct = len(zeroed) / len(wanted) * 100
        if pct > 30:
            st.error(
                f"**Under-capitalised.** {len(zeroed)} of {len(wanted)} intended "
                f"positions ({pct:.0f}%) rounded to zero contracts on {latest_rb}. "
                "The engine aborts above 30% — it refuses to run a smaller, "
                "different strategy than the one backtested. Raise the paper "
                "balance rather than narrowing the universe.")
        elif pct > 10:
            st.warning(
                f"{len(zeroed)} of {len(wanted)} intended positions ({pct:.0f}%) "
                f"rounded to zero on {latest_rb}. Below the 30% abort threshold, "
                "but weights are drifting from target.")

_overridden = "XSEC_DB" in os.environ
_shown = "/".join(Path(DBS).parts[-2:])
st.caption(
    f"Read {dt.datetime.now():%Y-%m-%d %H:%M:%S} · ledger `{_shown}`"
    + (" · **⚠ XSEC_DB override — this is NOT the live ledger**"
       if _overridden else "")
    + " · read-only view")

section("Section 1", "The ladder",
        "Each agent sees exactly one more factor than the one below it, so the "
        "vertical gap between two curves is the live contribution of that one "
        "factor. agent_0 is random — the null the others must beat to be "
        "interesting.")

if marks.empty:
    st.info("No marks recorded yet. The ladder appears after the first mark.")
else:
    latest_date = marks["date"].max()
    latest = marks[marks["date"] == latest_date].set_index("agent")

    idx_now = {}
    for a in AGENT_ORDER:
        s = marks[marks["agent"] == a].sort_values("date")
        if not s.empty and s["equity"].iloc[0]:
            idx_now[a] = s["equity"].iloc[-1] / s["equity"].iloc[0] * 100

    present = [a for a in AGENT_ORDER if a in idx_now]
    pairs = list(zip(present, present[1:]))
    in_order = sum(1 for lo, hi in pairs if idx_now[hi] >= idx_now[lo])
    n_days = marks["date"].nunique()

    lc, rc = st.columns([3, 1])
    with rc:
        st.markdown("**Rung order**")
        tone = "ok" if pairs and in_order == len(pairs) else (
            "warn" if in_order >= len(pairs) - 1 else "bad")
        st.markdown(
            chip(f"{in_order} of {len(pairs)} adjacent pairs in order", tone),
            unsafe_allow_html=True)
        st.caption(
            "The forward test asks one pre-committed question: do the rungs "
            "stay in order? "
            + ("**Too early to mean anything** — this needs months, not "
               f"{n_days} days." if n_days < 30 else
               "Past 30 marked days this starts carrying signal."))

    with lc:
        fig = go.Figure()
        for agent in AGENT_ORDER:
            s = marks[marks["agent"] == agent].sort_values("date")
            if s.empty:
                continue
            base = s["equity"].iloc[0]
            if not base:
                continue
            fig.add_trace(go.Scatter(
                x=s["date"], y=s["equity"] / base * 100.0,
                name=f"{agent} · {AGENT_ADDS[agent]}",
                mode="lines",
                line=dict(color=AGENT_COLOR[agent],
                          width=3 if agent == "agent_3" else 2,
                          dash="dot" if agent == "agent_0" else "solid"),
                hovertemplate="%{y:.2f}<extra>" + agent + "</extra>",
            ))
        fig.add_hline(y=100, line=dict(color="rgba(128,128,128,.55)",
                                       width=1, dash="dash"))
        fig.update_layout(
            height=400, hovermode="x unified",
            yaxis_title="equity (first mark = 100)", xaxis_title=None,
            margin=dict(l=8, r=8, t=28, b=8),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        fig.update_yaxes(gridcolor="rgba(128,128,128,.18)", zeroline=False)
        fig.update_xaxes(gridcolor="rgba(128,128,128,.10)")
        st.plotly_chart(fig, width="stretch")

    if n_days < 30:
        st.caption(
            "Agents differ only in **which markets they pick**, not in sizing "
            "or timing. Over a few days four dollar-neutral books rebalanced "
            "monthly move together because they share the same market beta. "
            "Treat anything under ~30 marked days as an infrastructure check, "
            "not a result.")

section("Section 2", "Agents",
        "Return is measured against each agent's own starting capital. The "
        "account's blended NetLiq is useless for attribution — four books "
        "share one account and often hold opposite positions in the same "
        "market that net to zero at account level while both carry real risk.")

cols = st.columns(len(AGENT_ORDER))
for col, agent in zip(cols, AGENT_ORDER):
    with col:
        start = CAPITAL.get(agent)
        eq, ret_s, pos_s = None, "—", "—"
        if not marks.empty:
            s = marks[marks["agent"] == agent].sort_values("date")
            if not s.empty:
                eq = s["equity"].iloc[-1]
        if eq is None and start is not None:
            eq = start
        if eq is not None and start:
            ret_s = f"{(eq / start - 1) * 100:+.2f}%"
        npos = int((positions["agent"] == agent).sum()) if not positions.empty else 0
        ntr = int((trades["agent"] == agent).sum()) if not trades.empty else 0
        pos_s = f"{npos} open · {ntr} closed"

        st.markdown(
            f"<div class='agentcard' style='border-left:3px solid "
            f"{AGENT_COLOR[agent]}'>"
            f"<div class='nm' style='color:{AGENT_COLOR[agent]}'>{agent}</div>"
            f"<div class='ad'>{AGENT_ADDS[agent]}</div>"
            f"<div class='eq'>{money(eq) if eq is not None else '—'}</div>"
            f"<div class='sub'>{ret_s} vs start · {pos_s}</div>"
            f"</div>", unsafe_allow_html=True)

section("Section 3", "Current positions")

if positions.empty:
    st.info("Flat — no open positions in the ledger.")
else:
    p = positions.copy()
    p["per_lot"] = p["entry_px"] * p["multiplier"] / p["magnifier"]
    p["gross"] = p["per_lot"] * p["contracts"].abs()
    p["side"] = p["contracts"].apply(lambda c: "LONG" if c > 0 else "SHORT")

    tabs = st.tabs([f"{a}  ({int((positions['agent'] == a).sum())})"
                    for a in AGENT_ORDER])
    for tab, agent in zip(tabs, AGENT_ORDER):
        with tab:
            q = p[p["agent"] == agent]
            if q.empty:
                st.write("Flat.")
                continue
            a, b, c = st.columns(3)
            a.metric("Positions", f"{len(q)}")
            b.metric("Gross notional", money(q["gross"].sum()))
            net = (q["per_lot"] * q["contracts"]).sum()
            c.metric("Net notional", money(net),
                     help="Near zero is expected — the book is dollar-neutral.")
            st.dataframe(
                q[["ticker", "side", "contracts", "entry_date", "entry_px",
                   "local_symbol", "expiry", "gross"]].sort_values("ticker"),
                width="stretch", hide_index=True,
                column_config={
                    "gross": st.column_config.NumberColumn(
                        "gross notional", format="$%.0f"),
                    "entry_px": st.column_config.NumberColumn(format="%.4f"),
                })

section("Section 4", "Decision log",
        "Every decision, traded or not. Rows with a non-zero target weight but "
        "zero actual contracts are positions the book wanted and could not "
        "take — recorded rather than hidden, because they measure whether the "
        "account is large enough to run the strategy as specified.")

if decisions.empty:
    st.info("No decisions recorded yet.")
else:
    rebals = sorted(decisions["rebal_date"].unique(), reverse=True)
    left, right = st.columns([1, 3])
    pick = left.selectbox("Rebalance", rebals, index=0)
    only_traded = right.checkbox("Traded positions only", value=False)

    d = decisions[decisions["rebal_date"] == pick]
    if only_traded:
        d = d[d["actual_contracts"] != 0]

    st.dataframe(
        d[["agent", "ticker", "rank", "score", "side", "target_weight",
           "target_contracts", "actual_contracts", "price", "reason"]],
        width="stretch", hide_index=True,
        column_config={
            "score": st.column_config.NumberColumn(format="%.3f"),
            "target_weight": st.column_config.NumberColumn(format="%.4f"),
            "target_contracts": st.column_config.NumberColumn(format="%.2f"),
            "price": st.column_config.NumberColumn(format="%.4f"),
        })

section("Section 5", "Completed round trips")

if trades.empty:
    st.info("None yet — positions are held between monthly rebalances, so the "
            "first closes land at the next rebalance.")
else:
    a, b, c, d_ = st.columns(4)
    a.metric("Round trips", f"{len(trades):,}")
    b.metric("Realised P&L", money(trades["pnl"].sum()))
    c.metric("Win rate", f"{(trades['pnl'] > 0).mean() * 100:.0f}%")
    d_.metric("Avg P&L", money(trades["pnl"].mean()))
    st.dataframe(
        trades[["agent", "ticker", "contracts", "entry_date", "entry_px",
                "exit_date", "exit_px", "exit_reason", "pnl"]],
        width="stretch", hide_index=True,
        column_config={"pnl": st.column_config.NumberColumn(format="$%.0f")})

st.markdown("<div class='sect'></div>", unsafe_allow_html=True)
st.caption(
    f"Universe {len(cfg['universe'])} markets · rebalance "
    f"{cfg['schedule']['rebalance_day']} {cfg['schedule']['rebalance_time']} "
    f"{cfg['schedule']['timezone']} · mark {cfg['schedule']['mark_time']} "
    f"weekdays · book ${cfg['capital']['book_size']:,.0f}/agent (config target; "
    "the engine sizes down if the account cannot margin it). "
    "Paper trading — no capital at risk."
)
