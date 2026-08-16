"""
test_execution.py — unit tests for the live-trading layer's PURE logic.

Deliberately no broker, no network, no IB Gateway: contracts.py is pure
calendar math, books.py runs on a temp SQLite file, marks.py is arithmetic,
and agent_two runs on synthetic series. The broker/engine integration is
exercised by the smoke run against IB Gateway (docs/FRAMEWORK.md §deploy),
not by unit tests — mocking ib_async here would only test the mock.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from storage_stress.agents import agent_two, simulate
from storage_stress.execution import books, contracts as C
from storage_stress.monitoring import marks as M


# ---------------------------------------------------------------------------
# contracts.py — the calendar math that must never be wrong
# ---------------------------------------------------------------------------
def test_seasonal_deferred_months():
    # Injection-season fronts (Apr-Oct delivery) pair with NEXT January.
    assert C.deferred_delivery_month((2026, 4)) == (2027, 1)
    assert C.deferred_delivery_month((2026, 10)) == (2027, 1)
    # Withdrawal fronts: Nov/Dec -> April NEXT year; Jan-Mar -> April SAME year.
    assert C.deferred_delivery_month((2026, 11)) == (2027, 4)
    assert C.deferred_delivery_month((2026, 12)) == (2027, 4)
    assert C.deferred_delivery_month((2027, 1)) == (2027, 4)
    assert C.deferred_delivery_month((2027, 3)) == (2027, 4)


def test_deferred_is_always_after_front():
    # Property check across every month: the deferred leg must be strictly
    # later than the front leg or the spread is nonsense.
    for m in range(1, 13):
        front = (2026, m)
        dy, dm = C.deferred_delivery_month(front)
        assert (dy, dm) > front


def test_front_month_with_expiry_and_guard():
    today = dt.date(2026, 7, 16)
    # Expiry far away (>5 bd): the nearest contract (Aug delivery) is fine.
    assert C.front_delivery_month(today, dt.date(2026, 7, 29)) == (2026, 8)
    # Expiry 3 business days away (inside the 5-bd guard): roll to September.
    assert C.front_delivery_month(today, dt.date(2026, 7, 21)) == (2026, 9)


def test_must_exit_guard():
    today = dt.date(2026, 7, 16)
    assert C.must_exit(today, dt.date(2026, 7, 21))          # 3 bd left -> exit
    assert not C.must_exit(today, dt.date(2026, 7, 31))      # 11 bd -> hold


def test_season_and_month_code():
    assert C.season_of(dt.date(2026, 7, 1)) == "injection"
    assert C.season_of(dt.date(2026, 12, 1)) == "withdrawal"
    assert C.month_code((2027, 1)) == "202701"


# ---------------------------------------------------------------------------
# books.py — full position lifecycle on a throwaway DB
# ---------------------------------------------------------------------------
def test_ledger_roundtrip(tmp_path):
    con = books.connect(tmp_path / "books.db")
    books.ensure_agent(con, "agent_test", 100_000)

    # Decision -> entry order -> fill -> open position.
    books.record_decision(con, "agent_test", "2026-07-10", -1, -2.1, 1,
                          "injection", 0.25, 0.05, 2, True, "test entry")
    oid = books.record_order(con, "agent_test", "agent_test|ENTRY|2026-07-10",
                             "ENTRY", -1, 2, "NGQ6", "NGF7", 101, "Filled")
    books.record_fill(con, oid, "agent_test", 0.25, 2)
    books.open_position(con, "agent_test", -1, 2, "2026-07-10", 0.25, 0.05,
                        "NGQ6", "NGF7", "2026-08-26")

    pos = books.get_position(con, "agent_test")
    assert pos["side"] == -1 and pos["contracts"] == 2

    # Mark while open: short spread, px fell 0.25 -> 0.20 => favorable.
    # unreal = -1 * (0.20 - 0.25) * 2 * 10_000 = +$1,000. (approx: float subtraction)
    unreal = M.unrealized_pnl(pos, 0.20)
    assert unreal == pytest.approx(1000.0)
    eq = books.record_mark(con, "agent_test", "2026-07-13", 0.20, unreal)
    assert eq == pytest.approx(101_000.0)

    # Close: realized P&L must equal the same arithmetic, position row gone.
    pnl = books.close_position(con, "agent_test", "2026-07-14", 0.20, "time")
    assert pnl == pytest.approx(1000.0)
    assert books.get_position(con, "agent_test") is None
    assert books.realized_pnl(con, "agent_test") == pytest.approx(1000.0)

    # Post-close mark: equity = capital + realized, no unrealized left.
    eq = books.record_mark(con, "agent_test", "2026-07-14", None, 0.0)
    assert eq == pytest.approx(101_000.0)

    # Read side used by the dashboard.
    assert len(books.equity_curve(con, "agent_test")) == 2
    trades = books.trade_log(con, "agent_test")
    assert len(trades) == 1 and trades.iloc[0]["exit_reason"] == "time"
    con.close()


def test_mark_idempotent(tmp_path):
    # Re-running the mark job for the same day must refresh, not duplicate —
    # jobs get retried after gateway restarts.
    con = books.connect(tmp_path / "books.db")
    books.ensure_agent(con, "a", 50_000)
    books.record_mark(con, "a", "2026-07-13", None, 0.0)
    books.record_mark(con, "a", "2026-07-13", None, 0.0)
    assert len(books.equity_curve(con, "a")) == 1
    con.close()


# ---------------------------------------------------------------------------
# marks.py — exit-rule priority
# ---------------------------------------------------------------------------
def _pos(**over):
    base = dict(side=1, contracts=1, entry_date="2026-07-01", entry_px=0.30,
                entry_vol=0.05, front_leg="NGQ6", deferred_leg="NGF7",
                front_expiry="2026-08-26")
    base.update(over)
    return base


def test_exit_rules_priority_and_thresholds():
    today = dt.date(2026, 7, 16)
    # No rule triggers: held 15 < 42 days, move within 2x vol, expiry far.
    assert M.check_exits(_pos(), today, 0.28, 42) is None
    # Stop: long spread down > 2x entry vol (0.30 -> 0.19 = -0.11 < -0.10).
    assert M.check_exits(_pos(), today, 0.19, 42) == "stop"
    # Time: max_hold_days reached (15 days held >= 14, agent_zero's spec).
    assert M.check_exits(_pos(), today, 0.30, 14) == "time"
    # Delivery guard beats everything even when stop also triggers.
    assert M.check_exits(_pos(front_expiry="2026-07-20"), today, 0.19, 14) \
        == "delivery_guard"


# ---------------------------------------------------------------------------
# simulate() must NOT book phantom P&L across a contract roll
# ---------------------------------------------------------------------------
def test_roll_produces_no_phantom_pnl():
    """A spliced spread series jumps at a roll; that jump is not tradeable.

    Construct a series that is flat except for one huge jump, and mark that
    jump as a roll. With roll_flag supplied, the jump must be ignored and the
    position closed; without it, the old (wrong) behaviour books the jump.
    """
    idx = pd.date_range("2026-01-02", periods=8, freq="W-FRI")
    #                     flat ..............  JUMP  flat ......
    spread = pd.Series([1.0, 1.0, 1.0, 1.0, 5.0, 5.0, 5.0, 5.0], index=idx)
    roll = pd.Series(False, index=idx)
    roll.iloc[4] = True                       # the +4.00 move is a splice

    side = pd.Series(0, index=idx)
    side.iloc[0] = 1                          # go long on day 0, then hold
    vol = pd.Series(0.5, index=idx)           # constant vol -> stable sizing

    # WITH the roll flag: the jump is not P&L, and the trade exits at the roll.
    res = simulate(spread, side, vol, capital=100_000, max_hold_days=999,
                   roll_flag=roll)
    assert res["pnl"].iloc[4] == 0.0, "roll jump must not be booked as P&L"
    trades = res.attrs["trades"]
    assert (trades["reason"] == "roll").any(), "position must close at the roll"

    # WITHOUT it (legacy behaviour): the same jump IS booked — this asserts the
    # bug the flag exists to fix, so the fix cannot silently regress.
    res_bad = simulate(spread, side, vol, capital=100_000, max_hold_days=999)
    assert res_bad["pnl"].iloc[4] > 0.0


def test_trading_costs_are_actually_charged():
    """Costs must reduce equity. Regression test for a real bug: slippage was
    written into equity.iloc[i] and then overwritten by the equity recursion,
    so the backtest ran cost-free and a coin flip could look profitable."""
    idx = pd.date_range("2026-01-02", periods=6, freq="W-FRI")
    spread = pd.Series(1.0, index=idx)        # perfectly flat -> zero gross P&L
    side = pd.Series(0, index=idx)
    side.iloc[0] = 1                          # one entry
    vol = pd.Series(0.5, index=idx)

    res = simulate(spread, side, vol, capital=100_000, max_hold_days=999,
                   slippage_ticks_per_leg=1.0, tick_value=10.0)
    # Flat price => the ONLY thing that can move equity is cost.
    assert res.attrs["total_cost"] > 0.0
    assert res["equity"].iloc[-1] < 100_000, "costs must reduce final equity"
    assert res["equity"].iloc[-1] == pytest.approx(
        100_000 - res.attrs["total_cost"])


def test_roll_flag_absent_is_backward_compatible():
    """Omitting roll_flag must not change behaviour for callers that don't
    supply one (the live engine and existing tests)."""
    idx = pd.date_range("2026-01-02", periods=5, freq="W-FRI")
    spread = pd.Series([1.0, 1.1, 1.2, 1.3, 1.4], index=idx)
    side = pd.Series(0, index=idx)
    side.iloc[0] = 1
    vol = pd.Series(0.5, index=idx)
    a = simulate(spread, side, vol, max_hold_days=999)
    b = simulate(spread, side, vol, max_hold_days=999, roll_flag=None)
    pd.testing.assert_series_equal(a["equity"], b["equity"])


# ---------------------------------------------------------------------------
# agent_two — the new ladder rung behaves like its siblings
# ---------------------------------------------------------------------------
def test_agent_two_seasonal_gating():
    # 6 years of weekly data so the 5y z-window has history to work with.
    idx = pd.date_range("2020-01-03", periods=6 * 52, freq="W-FRI")
    rng = np.random.default_rng(0)
    s = pd.Series(rng.normal(0, 1, len(idx)), index=idx)
    # Force the last observation to a huge positive value -> big positive z.
    s.iloc[-1] = 25.0
    side = agent_two(s, thresh=1.5)
    last_date = idx[-1]
    if 4 <= last_date.month <= 10:
        # Injection season + high z -> Type A short spread.
        assert side.iloc[-1] == -1
    else:
        # Withdrawal season needs NEGATIVE z for a trade; high positive z -> flat.
        assert side.iloc[-1] == 0
    # Sides are only ever -1/0/+1.
    assert set(side.unique()) <= {-1, 0, 1}
