"""
marks.py — mark-to-market arithmetic + exit-rule checks (pure functions).
=========================================================================

Separated from the engine so the risk rules are unit-testable without a
broker or a database. The engine calls these every daily mark job.

Exit rules implemented (docs/strategy.md, in priority order):
  1. delivery_guard — front leg inside the 5-business-day delivery window.
     Highest priority: taking delivery of physical gas is the one
     unrecoverable failure mode, so this overrides everything.
  2. stop — the position is down more than `stop_vol_multiple` (2x) times
     the ENTRY-day spread volatility. Uses entry vol, not current vol,
     exactly like simulate() in agents.py, so live == backtest semantics.
  3. time — held longer than the agent's max_hold_days (14 for agent_zero
     per the proposal, 42 for the signal agents per the locked strategy).
"""
from __future__ import annotations

import datetime as dt

from storage_stress.execution import contracts as C
from storage_stress.execution.books import POINT_VALUE


def unrealized_pnl(pos: dict, spread_px: float) -> float:
    """$ P&L of an open position at the given spread level.

    Same convention as the backtest: side * (px - entry) * contracts * $10k.
    `pos` is the dict returned by books.get_position().
    """
    return (pos["side"] * (float(spread_px) - pos["entry_px"])
            * pos["contracts"] * POINT_VALUE)


def check_exits(pos: dict, today: dt.date, spread_px: float,
                max_hold_days: int, stop_vol_multiple: float = 2.0,
                guard_bd: int = C.DEFAULT_GUARD_BD) -> str | None:
    """Return the exit reason ('delivery_guard' | 'stop' | 'time') or None.

    Checked in strict priority order — if several rules trigger on the same
    day, the recorded reason is the highest-priority one, which keeps the
    trade log's exit-reason statistics meaningful.
    """
    # 1. Delivery guard — compare today to the stored front-leg expiry.
    front_expiry = dt.date.fromisoformat(pos["front_expiry"])
    if C.must_exit(today, front_expiry, guard_bd):
        return "delivery_guard"

    # 2. Volatility stop: adverse move beyond 2x the entry-day vol.
    #    side * (px - entry) is the SIGNED favorable move; a large negative
    #    value means the trade moved against us.
    adverse = pos["side"] * (float(spread_px) - pos["entry_px"])
    if adverse < -stop_vol_multiple * pos["entry_vol"]:
        return "stop"

    # 3. Time stop.
    held = (today - dt.date.fromisoformat(pos["entry_date"])).days
    if held >= max_hold_days:
        return "time"

    return None
