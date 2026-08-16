"""
dashboard/pivot_a.py — research dashboard for the Pivot A factor ladder.
========================================================================

READ-ONLY view of the backtest artefacts written by:
    scripts/run_xsec.py          -> xsec_ladder.csv, xsec_equity_curves.csv
    scripts/carry_drop_test.py   -> (results transcribed below)

This is a RESEARCH dashboard, not a live-portfolio dashboard. Pivot A is not
yet wired to the execution engine, and the page says so prominently -- a
dashboard that looks live but isn't is worse than no dashboard.

Run:
    streamlit run dashboard/pivot_a.py --server.port 8502
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"

# Fixed colour per agent so a book is the same colour in every chart.
COLORS = {
    "agent_0": "#9aa0a6",   # grey — the null benchmark
    "agent_1": "#4c8bf5",   # blue
    "agent_2": "#f5a623",   # amber
    "agent_3": "#34a853",   # green — full signal
}
LABELS = {
    "agent_0": "random (null)",
    "agent_1": "carry",
    "agent_2": "carry + momentum",
    "agent_3": "carry + mom + basis-mom",
}

st.set_page_config(page_title="Pivot A — Commodity Factors", layout="wide")
st.title("Pivot A — Cross-Sectional Commodity Factor Ladder")
st.caption("23 commodities · 8,538 contracts · 2000–2026 · dollar-neutral long/short")

st.warning(
    "**Research backtest — not live.** Pivot A is not yet wired to the "
    "execution engine. These are historical simulation results, not a traded "
    "portfolio. The natural-gas strategy that *was* live has been retired.",
    icon="⚠️")


@st.cache_data(ttl=30)
def load(name: str):
    p = PROC / name
    return pd.read_csv(p) if p.exists() else None


ladder = load("xsec_ladder.csv")
curves = load("xsec_equity_curves.csv")

if ladder is None:
    st.error("No results found. Run `python scripts/run_xsec.py` first.")
    st.stop()

# ---- 1. THE LADDER ---------------------------------------------------------
st.subheader("The information ladder")
st.caption("Each rung sees exactly one more factor than the one below it, so "
           "the gap between adjacent rungs isolates that factor's contribution.")

cols = st.columns(len(ladder))
for col, (_, r) in zip(cols, ladder.iterrows()):
    col.markdown(f"**{r['agent']}**")
    col.caption(LABELS.get(r["agent"], r.get("signal", "")))
    col.metric("Sharpe", f"{r['sharpe']:.2f}", f"{r['ann_ret%']:+.2f}%/yr")
    col.caption(f"max DD {r['max_dd%']:.1f}%")

# ---- 2. EQUITY CURVES ------------------------------------------------------
if curves is not None and len(curves):
    st.subheader("Equity curves (growth of 1.0, net of costs)")
    date_col = curves.columns[0]
    curves[date_col] = pd.to_datetime(curves[date_col])
    fig = go.Figure()
    for a in [c for c in curves.columns if c.startswith("agent")]:
        fig.add_trace(go.Scatter(
            x=curves[date_col], y=curves[a], name=f"{a} — {LABELS.get(a,'')}",
            mode="lines", line=dict(color=COLORS.get(a), width=2)))
    fig.update_layout(height=430, hovermode="x unified", yaxis_type="log",
                      yaxis_title="growth of 1.0 (log scale)",
                      legend=dict(orientation="h", y=1.12),
                      margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Log scale — equal vertical distances are equal percentage moves.")

# ---- 3. FULL STATS ---------------------------------------------------------
st.subheader("Full statistics")
st.dataframe(ladder.round(3), hide_index=True, use_container_width=True)

# ---- 4. ROBUSTNESS ---------------------------------------------------------
st.subheader("Robustness — the tests that killed the previous strategy")

left, right = st.columns(2)

with left:
    st.markdown("**Cost sensitivity** (Sharpe at multiples of 10bps/turnover)")
    st.dataframe(pd.DataFrame({
        "agent": ["agent_0", "agent_1", "agent_2", "agent_3"],
        "1x": [0.082, 0.240, 0.355, 0.612],
        "3x": [-0.163, 0.170, 0.307, 0.565],
        "5x": [-0.402, 0.100, 0.258, 0.518],
    }), hide_index=True, use_container_width=True)
    st.caption("agent_3 holds at 0.52 under 5× costs — not cost-fragile.")

with right:
    st.markdown("**Era stability** (Sharpe by period)")
    st.dataframe(pd.DataFrame({
        "agent": ["agent_0", "agent_1", "agent_2", "agent_3"],
        "pre-2019": [-0.475, 0.537, 0.317, 0.588],
        "post-2019": [0.424, -0.302, 0.446, 0.690],
        "last 3y": [-0.176, -0.507, 0.609, 0.926],
    }), hide_index=True, use_container_width=True)
    st.caption("No post-publication decay: the full signal strengthens over time.")

# ---- 5. THE CARRY PARADOX --------------------------------------------------
st.subheader("The carry paradox")
st.markdown(
    "Carry's **standalone** Sharpe has decayed to **−0.51** over the last three "
    "years — plausibly arbitraged since commodity index investing scaled up. "
    "That made dropping it look obvious. A **pre-committed, single-shot** test "
    "(drop only if better in ≥3 of 4 conditions) said otherwise:")

carry = pd.DataFrame({
    "condition": ["full sample", "post-2019", "vol-targeted",
                  "post-2019 + 3× cost + volT"],
    "with carry": [0.612, 0.690, 0.698, 0.784],
    "without carry": [0.430, 0.650, 0.442, 0.743],
})
carry["delta"] = (carry["without carry"] - carry["with carry"]).round(3)
st.dataframe(carry, hide_index=True, use_container_width=True)
st.success(
    "**0 of 4 improved → KEEP CARRY.** Standalone Sharpe was the wrong "
    "criterion: carry is weakly correlated with the other two factors, so it "
    "cuts portfolio *variance* more than it cuts return. A factor can be a poor "
    "standalone bet and still earn its place through diversification.")

# ---- 6. VOL TARGETING ------------------------------------------------------
st.subheader("Volatility targeting (10% annualised)")
st.dataframe(pd.DataFrame({
    "agent": ["agent_0", "agent_1", "agent_2", "agent_3"],
    "sharpe base": [0.082, 0.240, 0.355, 0.612],
    "sharpe volT": [0.026, 0.330, 0.442, 0.698],
    "maxDD base %": [-66.3, -75.0, -63.8, -36.4],
    "maxDD volT %": [-45.5, -35.7, -31.8, -18.6],
}), hide_index=True, use_container_width=True)
st.caption("Average leverage 0.53 — the rule mostly *de-risks*. Drawdown roughly halved.")

# ---- 7. HONEST LIMITATIONS -------------------------------------------------
st.subheader("What is NOT established")
st.markdown("""
- **No out-of-sample holdout.** Universe, momentum window, tercile cutoffs,
  rebalance frequency and vol target were all chosen by judgement. Nothing was
  tuned iteratively — but that is not the same as validated.
- **Post-2019 sample is short** (~7.5 years) with a wide null band (sd 0.33).
- **Datastream settlements unverified** against an independent source.
- **Flat cost model** — oats and orange juice are not crude oil. No liquidity
  filtering has been applied.
- **Execution unmodelled** — 23 single-contract positions is a different problem
  from one calendar spread: per-market contract sizing, whole-lot rounding,
  margin and market-data entitlements all remain open.

The only test that cannot be overfit is **forward** data. That is the next step.
""")
