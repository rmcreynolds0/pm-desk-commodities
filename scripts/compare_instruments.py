#!/usr/bin/env python3
"""
compare_instruments.py — head-to-head: which TRADED INSTRUMENT suits the signal?
===============================================================================

WHY THIS EXISTS
---------------
The four-agent ladder established that the DSI signal carries information: on
the locked seasonal spread, losses shrink monotonically as each rung adds a
piece of information (zero -28.7% -> one -21.1% -> two -13.6% -> dsi -1.7%).
But every agent still LOST, with confidence intervals excluding zero. So the
signal works while the trade construction does not.

That points at the INSTRUMENT, and specifically at a timescale mismatch:

    Salt caverns are FAST-CYCLING storage -- they turn over several times a
    season (depleted reservoirs turn over ~once), which is exactly why salt
    utilisation is a *deliverability* stress indicator. Such a signal is
    inherently SHORT-HORIZON. Yet the locked strategy trades a spread whose
    deferred leg can be NINE months out.

This script tests that hypothesis by running the identical four agents, with
identical signal code, identical sizing, costs and exit rules, across three
different traded instruments:

    prompt    front vs adjacent month   (shortest horizon, cheapest, most liquid)
    seasonal  front vs next Jan / Apr   (THE LOCKED STRATEGY -- the control)
    mar_apr   fixed March/April         (the 'widow-maker'; end-of-winter
                                         storage depletion, no monthly rolling)

ONLY the instrument varies. Everything else is held constant, which is what
makes the comparison interpretable.

READING THE OUTPUT -- two cautions stated up front:
  * `agent_dsi` trades rarely, so its weekly return series is mostly zeros and
    its SHARPE IS UNSTABLE. Compare sparse agents on total return and drawdown.
  * Testing three instruments on the same nine years invites selection bias.
    A winner here is a CANDIDATE requiring out-of-sample confirmation (that is
    what the live paper books are for), not a validated result.

Usage:
    python scripts/compare_instruments.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from storage_stress.agents import (  # noqa: E402
    agent_zero, agent_one, agent_two, agent_dsi, simulate, sharpe,
    bootstrap_sharpe_ci,
)
from storage_stress.data import connectivity as C  # noqa: E402
from storage_stress.data import futures as F  # noqa: E402
from storage_stress.signal import build_dsi  # noqa: E402

INSTRUMENTS = ["prompt", "seasonal", "mar_apr"]


def load_config() -> dict:
    with open(ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def max_drawdown_pct(equity: pd.Series) -> float:
    """Peak-to-trough decline as a fraction (e.g. -0.08 = -8%)."""
    return float((equity / equity.cummax() - 1).min())


def build_shared_inputs(settings: dict):
    """Everything that does NOT depend on the traded instrument.

    Storage and basis are instrument-independent, so we pull them once and
    reuse them for all three runs -- guaranteeing the signal inputs are
    byte-identical across instruments.
    """
    start = settings["backtest"]["train_start"]
    salt = C.eia_storage("salt_south_central", start=start, synthetic=False)
    waha = C.regional_basis("waha", start=start)
    dom = C.regional_basis("domsouth", start=start)

    dsi = build_dsi(salt, settings["signal"]["salt_max_rate_bcf_wk"],
                    waha.iloc[:, 0].resample("W-FRI").last(),
                    dom.iloc[:, 0].resample("W-FRI").last(),
                    k=settings["signal"]["convex_k"])
    return salt, dsi, waha, dom


def run_instrument(name: str, spread_daily: pd.DataFrame, salt: pd.DataFrame,
                   dsi: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """Run all four agents on ONE instrument; return a stats table."""
    spread = spread_daily["spread"].resample("W-FRI").last()
    spread = spread.reindex(salt.index).interpolate(limit_area="inside").dropna()

    roll_w = (spread_daily["roll"].resample("W-FRI").max()
              .reindex(spread.index).fillna(False).astype(bool))

    spread_ret = spread.diff().mask(roll_w, 0.0)
    spread_vol = spread_ret.rolling(
        settings["signal"].get("vol_window_weeks", 30)).std()

    sizing, costs, entry = settings["sizing"], settings["costs"], settings["entry"]

    signals = {
        "agent_zero": agent_zero(dsi.index, seed=0, trade_every=1),
        "agent_one": agent_one(salt["net_flow_bcf"].reindex(dsi.index),
                               thresh=entry["agent_one_z"]),
        "agent_two": agent_two(dsi["dsi_raw"], thresh=entry["agent_one_z"]),
        "agent_dsi": agent_dsi(dsi["dsi_clean"], spread_ret,
                               entry_z=entry["dsi_entry_z"]),
    }

    rows = []
    for agent, sig in signals.items():
        res = simulate(
            spread, sig, spread_vol.reindex(dsi.index),
            capital=sizing["capital"], risk_per_sd=sizing["risk_per_sd"],
            max_hold_days=entry["max_hold_days"],
            slippage_ticks_per_leg=costs["slippage_ticks_per_leg"],
            tick_value=costs["tick_value"], exit_frac=entry["exit_revert_frac"],
            roll_flag=roll_w)
        ret = res["equity"].pct_change().dropna()
        _, (lo, hi) = bootstrap_sharpe_ci(ret, n_boot=2000)
        final = res["equity"].iloc[-1]
        rows.append({
            "instrument": name,
            "agent": agent,
            "trades": len(res.attrs["trades"]),
            "total_ret%": (final / sizing["capital"] - 1) * 100,
            "max_dd%": max_drawdown_pct(res["equity"]) * 100,
            "sharpe": sharpe(ret),
            "ci_lo": lo, "ci_hi": hi,
        })
    return pd.DataFrame(rows)


def main() -> None:
    settings = load_config()
    start = settings["backtest"]["train_start"]

    print("=" * 78)
    print("INSTRUMENT COMPARISON — same agents, same signal, same costs")
    print("=" * 78)

    print("\n[1/3] shared inputs (instrument-independent)")
    salt, dsi, waha, dom = build_shared_inputs(settings)
    print(f"    storage      {len(salt)} rows, {salt.index[0].date()} -> {salt.index[-1].date()}")
    print(f"    basis waha   [{waha.attrs.get('source')}] {len(waha)} rows")
    print(f"    basis domsth [{dom.attrs.get('source')}] {len(dom)} rows")
    print(f"    DSI          {len(dsi)} rows (dsi_raw + dsi_clean)")

    print("\n[2/3] building instruments")
    con = C._r2_duckdb()
    spreads = {}
    try:
        for inst in INSTRUMENTS:
            df = F.ng_spread(con, inst, start=start)
            spreads[inst] = df
            w = df["spread"].resample("W-FRI").last().dropna()
            print(f"    {inst:<9} {len(df):>5} daily obs  "
                  f"{df.index[0].date()} -> {df.index[-1].date()}  "
                  f"mean={df['spread'].mean():+.3f}  "
                  f"weekly_vol={w.diff().std():.4f}")
    finally:
        con.close()

    print("\n[3/3] running 4 agents x 3 instruments")
    all_stats = pd.concat(
        [run_instrument(inst, spreads[inst], salt, dsi, settings)
         for inst in INSTRUMENTS], ignore_index=True)

    print("\n" + "=" * 78)
    print("RESULTS")
    print("=" * 78)
    hdr = (f"{'instrument':<10}{'agent':<12}{'trades':>7}{'total_ret%':>12}"
           f"{'max_dd%':>10}{'sharpe':>9}{'ci_lo':>8}{'ci_hi':>8}")
    for inst in INSTRUMENTS:
        print(f"\n--- {inst}: {spreads[inst].attrs['definition']}")
        print(hdr)
        print("-" * len(hdr))
        for _, r in all_stats[all_stats["instrument"] == inst].iterrows():
            print(f"{r['instrument']:<10}{r['agent']:<12}{r['trades']:>7}"
                  f"{r['total_ret%']:>12.2f}{r['max_dd%']:>10.2f}"
                  f"{r['sharpe']:>9.2f}{r['ci_lo']:>8.2f}{r['ci_hi']:>8.2f}")

    print("\n" + "=" * 78)
    print("TOTAL RETURN % — agent (rows) x instrument (cols)")
    print("=" * 78)
    piv = all_stats.pivot(index="agent", columns="instrument",
                          values="total_ret%")[INSTRUMENTS]
    print(piv.round(2).to_string())

    print("\nTRADE COUNT — agent x instrument")
    print(all_stats.pivot(index="agent", columns="instrument",
                          values="trades")[INSTRUMENTS].to_string())

    out = ROOT / settings["paths"]["data_processed"] / "instrument_comparison.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    all_stats.to_csv(out, index=False)
    print(f"\nfull stats -> {out}")
    print("\nNOTE: sparse agents (few trades) have unstable Sharpe — compare "
          "them on total return / drawdown. Three instruments on one sample "
          "invites selection bias; treat any winner as a CANDIDATE needing "
          "out-of-sample confirmation.")


if __name__ == "__main__":
    main()
