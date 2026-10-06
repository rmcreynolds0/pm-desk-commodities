"""
Tests for the public status snapshot.

These are mostly NEGATIVE tests, and that is deliberate. This payload is
uploaded to a URL anyone can fetch. A field that should not be there cannot be
recalled once published, so the important assertions are about what is absent,
not what is present.

`build_payload` is a pure function of the ledger precisely so this can be
checked against a fixture without touching R2.
"""
from __future__ import annotations

import json

import pytest

from storage_stress.execution import xsec_books as B
from storage_stress.monitoring import publish as P

ACCOUNT_ID = "DUP235180"          # must never appear in the payload
IB_ORDER_ID = 987654321


@pytest.fixture
def cfg():
    return {
        "universe": {t: {} for t in ("CL", "GC", "ZC", "KC", "SB", "CT")},
        "signal": {
            "agents": {"agent_0": [], "agent_1": ["carry"],
                       "agent_2": ["carry", "mom"],
                       "agent_3": ["carry", "mom", "basis_mom"]},
            "live_agents": ["agent_3"],
        },
        "capital": {"book_size": 2_000_000},
        "schedule": {"rebalance_day": "last_business_day",
                     "rebalance_time": "10:30", "mark_time": "16:30",
                     "timezone": "US/Eastern"},
        "paths": {"books_db": "ignored"},
    }


@pytest.fixture
def ledger(tmp_path):
    con = B.connect(tmp_path / "books.db")
    for a in ("agent_0", "agent_3"):
        B.ensure_agent(con, a, 2_000_000)

    # agent_3 is live; agent_0 is defined but not trading.
    B.set_position(con, "agent_3", "CL", 3, "2026-09-30", 83.17,
                   "CLZ6", "2026-12-15", 1000, 1)
    B.set_position(con, "agent_3", "GC", -1, "2026-09-30", 4425.5,
                   "GCZ6", "2026-12-29", 100, 1)
    B.set_position(con, "agent_0", "KC", 7, "2026-09-30", 388.4,
                   "KCZ6", "2026-12-18", 37500, 100)

    # A traded decision and an untraded one — the untraded row is the honest
    # part and must survive into the payload.
    B.record_decision(con, "agent_3", "2026-09-30", "CL", 0.84, 1, 1,
                      0.1429, 3.43, 3, 83.17, 83170.0, "")
    B.record_decision(con, "agent_3", "2026-09-30", "GC", -0.91, 6, -1,
                      -0.1429, -0.64, 0, 4425.5, 442550.0,
                      "rounds to 0 contracts")

    oid = B.record_order(con, "agent_3", "xsec-agent_3-CL", "CL", "BUY", 3,
                         "CLZ6", IB_ORDER_ID, "Filled")
    B.record_fill(con, oid, "agent_3", "CL", 83.17, 3, "")

    for i, d in enumerate(("2026-10-01", "2026-10-02", "2026-10-03")):
        B.record_mark(con, "agent_3", d, 2, 525_000.0, 1_000.0 * (i + 1))
        B.record_mark(con, "agent_0", d, 1, 101_000.0, -500.0 * (i + 1))
    yield con
    con.close()


# --- what must NOT leak ----------------------------------------------------
def test_no_account_identifier_anywhere(ledger, cfg):
    """The ledger never stores an account number, but assert it regardless --
    a future schema change could introduce one silently."""
    blob = json.dumps(P.build_payload(ledger, cfg))
    assert ACCOUNT_ID not in blob
    assert "DUP" not in blob and "DU1" not in blob


def test_no_ibkr_order_ids(ledger, cfg):
    """Internal broker ids are useless externally and are a correlation
    handle back to the account."""
    blob = json.dumps(P.build_payload(ledger, cfg))
    assert str(IB_ORDER_ID) not in blob


def test_no_order_ref_tags(ledger, cfg):
    """orderRef is the per-agent attribution mechanism — an implementation
    detail, not something a reader needs."""
    blob = json.dumps(P.build_payload(ledger, cfg))
    assert "xsec-agent_3-CL" not in blob


def test_no_credential_shaped_keys(ledger, cfg):
    blob = json.dumps(P.build_payload(ledger, cfg)).lower()
    for bad in ("password", "token", "secret", "tws_user", "access_key"):
        assert bad not in blob


# --- scoping to live agents ------------------------------------------------
def test_only_live_agents_are_published(ledger, cfg):
    """agent_0 exists in the ladder but is not trading. Publishing its empty
    book would imply a four-agent comparison that is not actually running."""
    p = P.build_payload(ledger, cfg)
    assert [a["name"] for a in p["agents"]] == ["agent_3"]
    assert all(r["agent"] == "agent_3" for r in p["positions"])
    assert all(r["agent"] == "agent_3" for r in p["recent_decisions"])


def test_live_agents_absent_publishes_all(ledger, cfg):
    cfg["signal"].pop("live_agents")
    p = P.build_payload(ledger, cfg)
    assert {a["name"] for a in p["agents"]} == {
        "agent_0", "agent_1", "agent_2", "agent_3"}


# --- content that must be there -------------------------------------------
def test_untraded_decisions_are_kept(ledger, cfg):
    """The row the book wanted and could not take is the main thing an
    outside reader should be able to check. Hiding it would make the
    published record flattering rather than honest."""
    p = P.build_payload(ledger, cfg)
    zeroed = [d for d in p["recent_decisions"] if d["actual_contracts"] == 0]
    assert len(zeroed) == 1
    assert "rounds to 0" in zeroed[0]["reason"]


def test_positions_carry_side_and_size(ledger, cfg):
    p = P.build_payload(ledger, cfg)
    by = {r["ticker"]: r for r in p["positions"]}
    assert by["CL"]["side"] == "LONG" and by["CL"]["contracts"] == 3
    assert by["GC"]["side"] == "SHORT" and by["GC"]["contracts"] == -1


def test_equity_curve_has_a_point_per_marked_date(ledger, cfg):
    p = P.build_payload(ledger, cfg)
    assert len(p["equity_curve"]) == 3
    assert all("agent_3" in pt for pt in p["equity_curve"])


def test_return_is_measured_against_starting_capital(ledger, cfg):
    p = P.build_payload(ledger, cfg)
    a = p["agents"][0]
    assert a["starting_capital"] == 2_000_000
    assert a["return_pct"] == pytest.approx(
        (a["equity"] / 2_000_000 - 1) * 100, rel=1e-6)


def test_payload_states_it_is_paper(ledger, cfg):
    """Anyone reading this must not mistake it for a live track record."""
    p = P.build_payload(ledger, cfg)
    assert "PAPER" in p["disclaimer"].upper()
    assert "no capital at risk" in p["disclaimer"].lower()


def test_payload_is_json_serialisable(ledger, cfg):
    """numpy/pandas scalars raise on json.dumps and would only fail at upload."""
    json.dumps(P.build_payload(ledger, cfg))


def test_empty_ledger_does_not_crash(tmp_path, cfg):
    con = B.connect(tmp_path / "empty.db")
    try:
        p = P.build_payload(con, cfg)
        assert p["equity_curve"] == [] and p["positions"] == []
        json.dumps(p)
    finally:
        con.close()


def test_curve_is_downsampled_rather_than_unbounded(ledger, cfg):
    """A multi-year daily curve would grow the payload without limit, and this
    is fetched by anyone holding the URL."""
    p = P.build_payload(ledger, cfg, max_curve_points=2)
    assert len(p["equity_curve"]) <= 3
