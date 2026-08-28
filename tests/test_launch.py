"""
Tests for per-agent launch detection.

This decides WHETHER AN AGENT EVER STARTS TRADING. The failure it guards
against already happened once in a different form: a whole-ledger "is it
empty?" check meant a missed launch window was never retried, and the book sat
idle for days while the mark job recorded zeros.

The per-agent version guards a second, subtler version of the same bug: with
one paper account per agent, agent_0 can be live for days before agent_3's
account exists. A whole-ledger check sees "not empty" and silently never
launches the rest.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from storage_stress.execution import xsec_books as B  # noqa: E402


def load_scheduler():
    """Import the scheduler by path — it lives in scripts/, not the package."""
    spec = importlib.util.spec_from_file_location(
        "xsec_scheduler", ROOT / "scripts" / "xsec_scheduler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGENTS = ["agent_0", "agent_1", "agent_2", "agent_3"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    sched = load_scheduler()
    monkeypatch.setattr(sched, "ROOT", tmp_path)
    db = tmp_path / "books.db"
    return sched, {"paths": {"books_db": str(db)},
                   "signal": {"agents": {a: [] for a in AGENTS}}}, db


def record(db, agent):
    """Give an agent one decision row = it has launched."""
    con = B.connect(db)
    B.ensure_agent(con, agent, 2_000_000)
    B.record_decision(con, agent, "2026-08-27", "CL", 0.5, 1, 1,
                      0.14, 3.2, 3, 83.17, 83170.0, "")
    con.close()


def test_all_agents_pending_on_a_fresh_ledger(cfg):
    sched, c, db = cfg
    assert sched.agents_needing_launch(c) == AGENTS


def test_launched_agent_drops_out_others_remain(cfg):
    """The exact scenario: agent_0 goes live while the other three paper
    accounts are still being opened. The other three MUST still launch."""
    sched, c, db = cfg
    record(db, "agent_0")
    assert sched.agents_needing_launch(c) == ["agent_1", "agent_2", "agent_3"]


def test_nothing_pending_once_all_have_traded(cfg):
    sched, c, db = cfg
    for a in AGENTS:
        record(db, a)
    assert sched.agents_needing_launch(c) == []


def test_flat_agent_is_not_treated_as_unlaunched(cfg):
    """An agent that rebalanced but holds nothing still has decision rows.
    Keying off `positions` would re-flatten and relaunch it every single day."""
    sched, c, db = cfg
    record(db, "agent_1")
    con = B.connect(db)
    assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    con.close()
    assert "agent_1" not in sched.agents_needing_launch(c)


def test_unreadable_ledger_launches_nothing(cfg, monkeypatch):
    """An unknown state must never trigger a flatten-and-relaunch of a live
    book. Silence is the safe failure here.

    Note a MISSING ledger is not this case: xsec_books.connect creates it, and
    a fresh ledger legitimately means every agent is pending. This covers a
    genuine read failure -- locked file, corrupt db, permissions.
    """
    sched, c, _ = cfg
    import storage_stress.execution.xsec_books as books

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(books, "connect", boom)
    assert sched.agents_needing_launch(c) == []


def test_missing_ledger_means_everything_is_pending(cfg):
    """Distinct from a read failure: no ledger yet is the normal pre-launch
    state, and every agent should launch."""
    sched, c, db = cfg
    assert not Path(c["paths"]["books_db"]).exists()
    assert sched.agents_needing_launch(c) == AGENTS


def test_order_follows_the_spec_not_the_database(cfg):
    sched, c, db = cfg
    record(db, "agent_2")
    assert sched.agents_needing_launch(c) == ["agent_0", "agent_1", "agent_3"]
