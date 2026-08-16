"""
engine.py — the per-agent decision & mark loop. The heart of the live system.
=============================================================================

Two jobs, both idempotent and safe to re-run:

  run_decide(root)  WEEKLY (Thu after settle). For EVERY enabled agent:
      assemble data -> compute the agent's signal for the latest weekly bar
      -> record the decision in the ledger (ALWAYS, traded or not — this is
      the rolling trade-by-trade research record) -> if the agent is flat
      and the signal fires, size inverse-vol and route a tagged combo order
      -> record order + fill + open the ledger position.

  run_mark(root)    DAILY (weekdays after settle). For EVERY enabled agent:
      price the agent's open position (if any) off fresh IBKR bars ->
      check exit rules (delivery guard / 2x-vol stop / time stop) -> exit
      via a tagged combo order when triggered -> write the daily mark row
      (equity = capital + realized + unrealized), including for flat agents
      so every book has a continuous daily equity curve.

DATA FLOW AT DECISION TIME (see docs/FRAMEWORK.md for the diagram):
    EIA API  ── weekly salt storage level/net flow ──┐
    IBKR     ── daily bars for front+deferred legs ──┼─> weekly aligned frame
    synthetic── Waha / Dom South basis (gap #2)     ──┘        │
                                                    agent ladder signals
                                                               │
                                             ledger decisions + orders + fills

The engine builds each agent's signal with the SAME functions the backtest
uses (agents.py / dsi.py), on weekly-resampled series, so live behavior is
the backtest behavior by construction — no reimplementation drift.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from storage_stress.agents import agent_one, agent_two, agent_dsi
from storage_stress.data import connectivity as conn
from storage_stress.execution import books, contracts as C
from storage_stress.execution.broker import Broker
from storage_stress.monitoring import marks as M
from storage_stress.signal import build_dsi, zscore, vol_regime_gate


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_configs(root: Path) -> tuple[dict, dict]:
    """(live, settings) config dicts. Both YAML files are the only knobs —
    the engine itself has no tunable constants (auditability rule).

    IBKR_HOST / IBKR_PORT environment variables override live.yaml when set:
    inside docker-compose the gateway is reached at hostname "ib-gateway"
    rather than 127.0.0.1, and compose injects that via the environment so
    the same yaml works on the laptop and on the VM.
    """
    import os

    with open(root / "config" / "live.yaml") as f:
        live = yaml.safe_load(f)
    with open(root / "config" / "settings.yaml") as f:
        settings = yaml.safe_load(f)
    live["ibkr"]["host"] = os.environ.get("IBKR_HOST", live["ibkr"]["host"])
    live["ibkr"]["port"] = int(os.environ.get("IBKR_PORT", live["ibkr"]["port"]))
    return live, settings


def _open_ledger(root: Path, live: dict):
    """Open books.db and make sure every enabled agent is registered."""
    con = books.connect(root / live["paths"]["books_db"])
    for name, acfg in live["agents"].items():
        if acfg.get("enabled", False):
            books.ensure_agent(con, name, acfg["capital"])
    return con


# ---------------------------------------------------------------------------
# Data assembly (decision time)
# ---------------------------------------------------------------------------
def assemble_dataset(broker: Broker, live: dict, settings: dict,
                     today: dt.date) -> dict:
    """Pull every input the agent ladder needs and align to a weekly grid.

    Returns a dict rather than a DataFrame because the pieces have different
    shapes (salt is a 2-col frame, legs are IB contracts, etc.).
    """
    start = live["data"]["history_start"]

    # -- 1. EIA weekly salt storage (REAL; the core feed). Raises on outage —
    #       a decide run without fresh storage data must not silently trade.
    salt = conn.eia_storage("salt_south_central", start=start, synthetic=False)

    # -- 2. Spread legs + daily spread series from IBKR (REAL; replaces the
    #       discontinued EIA RNGC feed). Resolve the CURRENT seasonal pair.
    front, deferred, front_expiry = broker.resolve_legs(today)
    spread_daily = broker.spread_series(front, deferred,
                                        live["data"]["spread_bar_duration"])

    # -- 3. Weekly alignment: Friday-stamped weekly closes, same convention
    #       as scripts/run_backtest.py so windows/z-scores are comparable.
    spread_w = spread_daily.resample("W-FRI").last().dropna()
    spread_ret = spread_w.diff()
    vol_window = live["data"]["spread_vol_window_weeks"]
    spread_vol = spread_ret.rolling(vol_window).std()

    # -- 4. Basis series for the DSI residualization. STILL SYNTHETIC (gap
    #       #2 in FRAMEWORK.md): a seeded random walk stands in until a real
    #       Waha / Dom South feed is wired. agent_dsi therefore runs in
    #       documented "hybrid" mode — flagged in every decision row reason.
    waha = conn.regional_basis("waha", start=start).iloc[:, 0]
    dom = conn.regional_basis("domsouth", start=start).iloc[:, 0]

    return {
        "salt": salt, "front": front, "deferred": deferred,
        "front_expiry": front_expiry, "spread_daily": spread_daily,
        "spread_w": spread_w, "spread_ret": spread_ret,
        "spread_vol": spread_vol, "waha": waha, "dom": dom,
        "basis_mode": live["data"]["basis_mode"],
    }


# ---------------------------------------------------------------------------
# Signals — one latest-bar decision per agent, using the backtest functions
# ---------------------------------------------------------------------------
def compute_signals(data: dict, live: dict, settings: dict,
                    today: dt.date) -> dict:
    """{agent_name: {side, z, gate, season, reason}} for the latest week.

    Every agent's full weekly side-series is computed with the SAME code the
    backtest uses; we then read the last row. All those functions are causal
    (rolling/expanding windows only), so "compute full series, take last"
    introduces no lookahead.
    """
    season = C.season_of(today)
    out: dict[str, dict] = {}
    acfg = live["agents"]

    # ---- agent_zero: deterministic weekly coin flip. Seeding with
    # (config seed, iso-year, iso-week) makes the flip reproducible for a
    # given week — re-running the decide job cannot flip the coin again.
    if acfg["agent_zero"].get("enabled"):
        iso = today.isocalendar()
        rng = np.random.default_rng(
            [acfg["agent_zero"]["seed"], iso.year, iso.week])
        side = -1 if rng.random() < 0.5 else 1
        out["agent_zero"] = {
            "side": side, "z": None, "gate": None, "season": season,
            "reason": f"coin flip ({'Type A short' if side < 0 else 'Type B long'})",
        }

    salt_flow = data["salt"]["net_flow_bcf"]

    # ---- agent_one: 5y z of raw salt net flow (the MVP signal).
    if acfg["agent_one"].get("enabled"):
        sides = agent_one(salt_flow, thresh=acfg["agent_one"]["entry_z"])
        z = zscore(salt_flow, window=5 * 52).iloc[-1]
        out["agent_one"] = {
            "side": int(sides.iloc[-1]), "z": float(z), "gate": None,
            "season": season,
            "reason": f"salt-flow z={z:+.2f} vs ±{acfg['agent_one']['entry_z']}",
        }

    # ---- Shared DSI pipeline for agents two & dsi. build_dsi returns both
    # dsi_raw (through step 4) and dsi_clean (residualized) so one call
    # feeds both rungs.
    need_dsi = acfg["agent_two"].get("enabled") or acfg["agent_dsi"].get("enabled")
    if need_dsi:
        dsi = build_dsi(
            data["salt"], settings["signal"]["salt_max_rate_bcf_wk"],
            data["waha"], data["dom"], k=settings["signal"]["convex_k"])

    # ---- agent_two: z of dsi_raw — smoothing/deseasonalization rung,
    # deliberately NO residualization and NO vol gate.
    if acfg["agent_two"].get("enabled"):
        sides = agent_two(dsi["dsi_raw"], thresh=acfg["agent_two"]["entry_z"])
        z = zscore(dsi["dsi_raw"], window=5 * 52).iloc[-1]
        out["agent_two"] = {
            "side": int(sides.iloc[-1]), "z": float(z), "gate": None,
            "season": season,
            "reason": f"dsi_raw z={z:+.2f} vs ±{acfg['agent_two']['entry_z']}",
        }

    # ---- agent_dsi: full residualized signal + vol regime gate. The reason
    # string carries the basis_mode flag so every decision row records that
    # the residualization currently runs on synthetic basis (hybrid mode).
    if acfg["agent_dsi"].get("enabled"):
        # Align the gate to the DSI index (spread history is shorter than
        # storage history; reindex+fillna(False) fails closed — no data, no trade).
        spread_ret_w = data["spread_ret"]
        sides = agent_dsi(dsi["dsi_clean"], spread_ret_w,
                          entry_z=acfg["agent_dsi"]["entry_z"])
        z = zscore(dsi["dsi_clean"]).iloc[-1]
        gate = bool(vol_regime_gate(spread_ret_w)
                    .reindex(dsi.index).fillna(False).iloc[-1])
        out["agent_dsi"] = {
            "side": int(sides.iloc[-1]), "z": float(z), "gate": gate,
            "season": season,
            "reason": (f"dsi_clean z={z:+.2f} vs ±{acfg['agent_dsi']['entry_z']}, "
                       f"gate={'open' if gate else 'closed'}, "
                       f"basis={data['basis_mode']}"),
        }

    return out


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
def size_contracts(capital: float, spread_vol: float, settings: dict) -> int:
    """Inverse-vol sizing, floored to whole contracts.

    contracts = (capital * risk_per_sd) / (weekly vol * point value)
    A computed size below 0.5 rounds to ZERO (no trade) rather than being
    bumped to 1 — bumping would risk more than the pre-registered 0.5% per
    SD on a small book, which violates the locked risk rule.
    """
    if spread_vol is None or not np.isfinite(spread_vol) or spread_vol <= 0:
        return 0
    raw = (capital * settings["sizing"]["risk_per_sd"]) / (
        spread_vol * settings["sizing"]["point_value"])
    return int(round(raw)) if raw >= 0.5 else 0


# ---------------------------------------------------------------------------
# THE WEEKLY DECIDE JOB
# ---------------------------------------------------------------------------
def run_decide(root: Path, today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    live, settings = load_configs(root)
    con = _open_ledger(root, live)

    ib = live["ibkr"]
    with Broker(ib["host"], ib["port"], ib["engine_client_id"],
                ib["market_data_type"], ib["connect_timeout_s"],
                ib["connect_retries"]) as broker:

        data = assemble_dataset(broker, live, settings, today)
        signals = compute_signals(data, live, settings, today)

        spread_px = float(data["spread_w"].iloc[-1])
        vol = float(data["spread_vol"].iloc[-1]) if np.isfinite(
            data["spread_vol"].iloc[-1]) else None
        decision_date = data["spread_w"].index[-1].date().isoformat()

        for agent, sig in signals.items():
            acfg = live["agents"][agent]
            pos = books.get_position(con, agent)

            # Target size only matters when we would actually enter.
            qty = size_contracts(acfg["capital"], vol, settings)

            # Entry conditions: signal fired, agent is flat, size >= 1.
            # (Exits are the mark job's responsibility — the decide job only
            # opens; this mirrors simulate(), where entries and exits are
            # evaluated by separate branches.)
            will_act = sig["side"] != 0 and pos is None and qty >= 1

            reason = sig["reason"]
            if sig["side"] != 0 and pos is not None:
                reason += " | skipped: position already open"
            elif sig["side"] != 0 and qty < 1:
                reason += " | skipped: size < 1 contract"

            books.record_decision(
                con, agent, decision_date, sig["side"], sig["z"], sig["gate"],
                sig["season"], spread_px, vol, qty, will_act, reason)

            if not will_act:
                print(f"[decide] {agent:<11} side={sig['side']:+d} -> no order ({reason})")
                continue

            # ---- route the tagged combo order and record everything
            result = broker.place_spread_order(
                agent, sig["side"], qty, data["front"], data["deferred"],
                action="ENTRY")
            order_id = books.record_order(
                con, agent, result["order_ref"], "ENTRY", sig["side"], qty,
                result["front_local"], result["deferred_local"],
                result["ib_order_id"], result["status"])

            # Record the entry in the SAME convention the mark job uses:
            # spread = front_close - deferred_close. IBKR's BAG combo
            # avgFillPrice uses a DIFFERENT (opposite) sign convention, so we
            # keep it only as an audit note — using it as the P&L entry price
            # flips the sign and corrupts every subsequent mark. For a market
            # order the decision-time spread and the true fill differ only by
            # slippage, which the strategy already models as a separate cost.
            combo_fill = result["avg_fill_price"]
            entry_px = spread_px
            note = (f"ibkr_combo_fill={combo_fill}" if combo_fill is not None
                    else "no combo fill price reported")
            books.record_fill(con, order_id, agent, entry_px, qty, note)

            books.open_position(
                con, agent, sig["side"], qty, today.isoformat(), entry_px,
                vol, result["front_local"], result["deferred_local"],
                data["front_expiry"].isoformat())
            print(f"[decide] {agent:<11} ENTERED side={sig['side']:+d} "
                  f"qty={qty} px={entry_px:.4f} ({result['status']})")

    con.close()


# ---------------------------------------------------------------------------
# THE DAILY MARK JOB
# ---------------------------------------------------------------------------
def run_mark(root: Path, today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    live, settings = load_configs(root)
    con = _open_ledger(root, live)

    ib = live["ibkr"]
    with Broker(ib["host"], ib["port"], ib["engine_client_id"],
                ib["market_data_type"], ib["connect_timeout_s"],
                ib["connect_retries"]) as broker:

        chain = broker.ng_chain()

        for agent, acfg in live["agents"].items():
            if not acfg.get("enabled"):
                continue
            pos = books.get_position(con, agent)

            if pos is None:
                # Flat book still gets a daily equity row -> continuous
                # curves on the dashboard, and gaps in the series become a
                # monitoring signal (job didn't run) instead of ambiguity.
                eq = books.record_mark(con, agent, today.isoformat(), None, 0.0)
                print(f"[mark] {agent:<11} flat  equity={eq:,.0f}")
                continue

            # Price the SPECIFIC legs this agent holds (they may differ from
            # the current seasonal pair after a month rolls).
            spread_px = _position_spread_px(broker, chain, pos)
            if spread_px is None:
                print(f"[mark] {agent:<11} WARNING: no price for "
                      f"{pos['front_leg']}/{pos['deferred_leg']} — mark skipped")
                continue

            unreal = M.unrealized_pnl(pos, spread_px)
            reason = M.check_exits(
                pos, today, spread_px, acfg["max_hold_days"],
                settings["entry"]["stop_vol_multiple"])

            if reason is not None:
                # Exit: route the OPPOSITE combo (side flips), tagged EXIT.
                front_c = _by_local(chain, pos["front_leg"])
                deferred_c = _by_local(chain, pos["deferred_leg"])
                result = broker.place_spread_order(
                    agent, -pos["side"], pos["contracts"], front_c,
                    deferred_c, action="EXIT")
                order_id = books.record_order(
                    con, agent, result["order_ref"], "EXIT", -pos["side"],
                    pos["contracts"], pos["front_leg"], pos["deferred_leg"],
                    result["ib_order_id"], result["status"])
                # Exit price in the mark convention (front - deferred), the
                # same as entry; the IBKR combo fill is kept only as an audit
                # note (its sign convention differs — see the entry path).
                exit_px = spread_px
                books.record_fill(con, order_id, agent, exit_px,
                                  pos["contracts"],
                                  f"ibkr_combo_fill={result['avg_fill_price']}")
                pnl = books.close_position(con, agent, today.isoformat(),
                                           exit_px, reason)
                unreal = 0.0            # realized now; mark reflects it via equity
                print(f"[mark] {agent:<11} EXIT ({reason}) px={exit_px:.4f} "
                      f"pnl={pnl:+,.0f}")

            eq = books.record_mark(con, agent, today.isoformat(),
                                   spread_px, unreal)
            print(f"[mark] {agent:<11} px={spread_px:.4f} "
                  f"unreal={unreal:+,.0f} equity={eq:,.0f}")

    con.close()


# ---------------------------------------------------------------------------
# Mark-job helpers
# ---------------------------------------------------------------------------
def _by_local(chain, local_symbol: str):
    """Find a chain contract by its localSymbol (stored in the ledger)."""
    for c in chain:
        if c.localSymbol == local_symbol:
            return c
    raise LookupError(f"contract {local_symbol} not in current NG chain")


def _position_spread_px(broker: Broker, chain, pos: dict) -> float | None:
    """Latest spread (front - deferred) for the exact legs a position holds.

    Uses a short 5-day bar window — we only need the most recent settle.
    Returns None when either leg has no recent bars (e.g. leg expired and
    the delivery guard will fire on the next check with stale-but-usable
    prices — the guard uses dates, not prices, so safety never depends on
    this function succeeding).
    """
    try:
        f = broker.daily_closes(_by_local(chain, pos["front_leg"]), "5 D")
        d = broker.daily_closes(_by_local(chain, pos["deferred_leg"]), "5 D")
    except LookupError:
        return None
    if f.empty or d.empty:
        return None
    return float(f.iloc[-1] - d.iloc[-1])
