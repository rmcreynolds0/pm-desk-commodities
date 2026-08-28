"""
Tests for per-agent account routing and book sizing.

These functions decide WHICH IBKR ACCOUNT AN ORDER LANDS IN and HOW BIG the
position is. A silent mistake here does not raise — it trades the right
strategy in the wrong account, or trades a book the account cannot margin.
Both produce a forward record that looks fine and means nothing, so the
routing is pinned down here rather than trusted.
"""
from __future__ import annotations

import pytest

from storage_stress.execution import xsec_engine as E


def base_cfg(**over):
    cfg = {
        "ibkr": {
            "host": "127.0.0.1", "port": 4002, "client_id": 30,
            "market_data_type": 4, "connect_timeout_s": 20,
            "connect_retries": 3,
            "account_mode": "per_agent",
            "gateways": {
                "agent_0": {"host": "ib-gateway-0", "port": 4002},
                "agent_1": {"host": "ib-gateway-1", "port": 4002},
                "agent_2": {"host": "ib-gateway-2", "port": 4002},
                "agent_3": {"host": "ib-gateway-3", "port": 4002},
            },
        },
        "signal": {"agents": {"agent_0": [], "agent_1": ["carry"],
                              "agent_2": ["carry", "mom"],
                              "agent_3": ["carry", "mom", "basis_mom"]}},
        "capital": {"book_size": 2_000_000},
    }
    cfg["ibkr"].update(over)
    return cfg


# --------------------------------------------------------------------------
# MODE DETECTION
# --------------------------------------------------------------------------
def test_per_agent_mode_detected():
    assert E.per_agent_accounts(base_cfg()) is True


def test_shared_mode_detected():
    assert E.per_agent_accounts(base_cfg(account_mode="shared")) is False


def test_mode_defaults_to_shared_when_absent():
    """An older config with no account_mode must NOT silently start routing
    orders to gateways that do not exist."""
    cfg = base_cfg()
    del cfg["ibkr"]["account_mode"]
    assert E.per_agent_accounts(cfg) is False


# --------------------------------------------------------------------------
# ENDPOINT RESOLUTION
# --------------------------------------------------------------------------
def test_each_agent_gets_its_own_gateway():
    cfg = base_cfg()
    hosts = {a: E.endpoint_for(cfg, a)[0] for a in cfg["signal"]["agents"]}
    assert hosts == {
        "agent_0": "ib-gateway-0", "agent_1": "ib-gateway-1",
        "agent_2": "ib-gateway-2", "agent_3": "ib-gateway-3",
    }
    # Distinct hosts is the whole point: two agents sharing a gateway means
    # two agents sharing an account, which is the layout we are avoiding.
    assert len(set(hosts.values())) == 4


def test_no_agent_falls_back_to_shared_endpoint():
    assert E.endpoint_for(base_cfg(), None) == ("127.0.0.1", 4002)


def test_unknown_agent_falls_back_rather_than_raising():
    """A new rung added to the ladder before its gateway exists should land on
    the shared endpoint, not crash the whole rebalance for every other agent."""
    assert E.endpoint_for(base_cfg(), "agent_9") == ("127.0.0.1", 4002)


def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv("IBKR_HOST_AGENT_2", "other-host")
    monkeypatch.setenv("IBKR_PORT_AGENT_2", "4099")
    assert E.endpoint_for(base_cfg(), "agent_2") == ("other-host", 4099)


def test_env_override_is_per_agent_only(monkeypatch):
    """Overriding one agent must not move the others."""
    monkeypatch.setenv("IBKR_HOST_AGENT_1", "moved")
    cfg = base_cfg()
    assert E.endpoint_for(cfg, "agent_1")[0] == "moved"
    assert E.endpoint_for(cfg, "agent_0")[0] == "ib-gateway-0"


# --------------------------------------------------------------------------
# BOOK SIZING
# --------------------------------------------------------------------------
def test_solo_account_gets_four_times_the_shared_book():
    """The entire reason per-agent accounts exist: the n_share divisor.

    At IBKR's $1M paper cap, sharing across four agents yields $520,833 each,
    at which gold/heating-oil/feeder-cattle/copper round to zero contracts.
    One account each yields $2,083,333.
    """
    cfg = base_cfg()
    shared = E.size_book(cfg, 1_000_000, 4, "shared")
    solo = E.size_book(cfg, 1_000_000, 1, "solo")
    assert shared == pytest.approx(520_833, rel=1e-3)
    assert solo == pytest.approx(2_000_000)      # capped by configured book
    assert solo > shared * 3


def test_configured_book_is_a_ceiling_not_a_target():
    """Ample equity must not inflate the book beyond the frozen spec."""
    cfg = base_cfg()
    assert E.size_book(cfg, 500_000_000, 1, "rich") == 2_000_000


def test_capacity_binds_when_equity_is_short():
    cfg = base_cfg()
    book = E.size_book(cfg, 500_000, 1, "poor")
    assert book == pytest.approx(1_041_666, rel=1e-3)
    assert book < cfg["capital"]["book_size"]


def test_unreadable_equity_falls_back_to_configured_book():
    """Must never return 0 — that would silently flatten every position."""
    cfg = base_cfg()
    for bad in (None, 0):
        assert E.size_book(cfg, bad, 1, "unknown") == 2_000_000


def test_sizing_formula_matches_documented_thresholds():
    """The numbers quoted in docs/DEPLOY.md must come from this same formula."""
    cfg = base_cfg()
    cfg["capital"]["book_size"] = 10_000_000
    # $19.2M shared across 4 agents is documented as exactly the $10M/agent point.
    assert E.size_book(cfg, 19_200_000, 4, "doc") == pytest.approx(10_000_000)
