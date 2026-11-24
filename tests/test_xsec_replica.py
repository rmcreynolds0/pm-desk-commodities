"""
test_xsec_replica.py — the live-rules backtest on synthetic futures chains.

No network: each market is a random-walk spot with a fixed term-structure
slope, listed as monthly contracts. That is enough to exercise contract
selection, whole-contract sizing, the clean roll, the forced roll, the skip
rule and the vol-target parity with the live engine.
"""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from storage_stress.agents import xsec_replica as XR

TICKERS = ["AA", "BB", "CC", "DD", "EE", "FF", "GG"]
CFG = {
    "portfolio": {"quantile": 0.3333, "min_markets": 6, "vol_target": 0.10,
                  "vol_window_days": 63, "max_leverage": 3.0},
    "capital": {"book_size": 2_000_000},
    "costs": {"bps_per_unit_turnover": 10.0},
}
SPECS = {t: (1000.0, 1.0) for t in TICKERS}


def _synthetic_px(ltd_day: int, start="2010-01-01", end="2012-06-30", seed=0):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    rows, fc = [], 1
    for t in TICKERS:
        spot = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days))))
        slope = rng.normal(0, 0.10)
        for m in pd.period_range(start, pd.Timestamp(end) + pd.DateOffset(months=14), freq="M"):
            ltd = pd.Timestamp(year=m.year, month=m.month, day=ltd_day)
            live = (days > ltd - pd.Timedelta(days=400)) & (days <= ltd)
            for d, s in zip(days[live], spot[live]):
                tau = (ltd - d).days / 365.25
                rows.append((fc, d, s * np.exp(-slope * tau), t, ltd))
            fc += 1
    return pd.DataFrame(rows, columns=["futcode", "date", "settlement", "ticker", "lasttrddate"])


def _run(ltd_day=15, cfg=CFG, factors=("carry",)):
    md = XR.MarketData(_synthetic_px(ltd_day), SPECS)
    rebal = XR.last_weekdays("2010-01-01", "2012-05-31")
    snap = XR.build_snapshots(md, TICKERS, rebal)
    ok = snap[snap["status"] == "ok"]
    snaps = {d: g for d, g in ok.groupby("date")}
    pb = XR.PriceBook(md, pd.bdate_range(rebal[0], rebal[-1]))
    res = XR.simulate(list(factors), XR.agent_scorer("agent_1"), snaps, md, pb, cfg)
    return res, snaps


def _holdings(entry) -> dict[str, tuple[int, int]]:
    out = {}
    for item in filter(None, str(entry["holdings"]).split(";")):
        t, fc, n = item.split(":")
        out[t] = (int(fc), int(n))
    return out


def test_last_weekdays_skip_weekends():
    got = XR.last_weekdays("2026-01-01", "2026-12-31")
    assert got[0] == pd.Timestamp("2026-01-30")     # Jan 31 2026 is a Saturday
    assert got[4] == pd.Timestamp("2026-05-29")     # May 31 2026 is a Sunday
    assert got[7] == pd.Timestamp("2026-08-31")     # a Monday
    assert all(d.weekday() < 5 for d in got) and len(got) == 12


def test_front_respects_25_day_guard():
    md = XR.MarketData(_synthetic_px(15), SPECS)
    d = pd.Timestamp("2011-03-31")
    front, second = md.term_structure("AA", d)
    assert (front[1] - d).days > 25 and second[1] > front[1]
    # Apr-15 is inside the guard, so the front is the May contract
    assert front[1] == pd.Timestamp("2011-05-15")


def test_clean_roll_holds_only_current_fronts():
    res, snaps = _run()
    traded = res.log[res.log["status"] == "traded"]
    assert len(traded) > 12
    for _, e in traded.iterrows():
        fronts = snaps[e["date"]].set_index("ticker")["front_code"].astype(int)
        for t, (fc, n) in _holdings(e).items():
            assert fc == fronts[t], f"{t} held in a stale contract on {e['date']:%Y-%m-%d}"
            assert n != 0


def test_first_rebalance_sizes_whole_contracts_at_unit_leverage():
    res, snaps = _run()
    first = res.log[res.log["status"] == "traded"].iloc[0]
    assert first["leverage"] == 1.0
    notional = snaps[first["date"]].set_index("ticker")["notional"]
    k = round(len(snaps[first["date"]]) * CFG["portfolio"]["quantile"])
    for t, (_, n) in _holdings(first).items():
        assert abs(n) == round((1 / k) * CFG["capital"]["book_size"] / notional[t])


def test_equity_is_capital_plus_pnl_minus_costs():
    res, _ = _run()
    d = res.daily
    assert d["equity"].iloc[-1] == pytest.approx(
        CFG["capital"]["book_size"] + d["pnl"].sum() - d["cost"].sum())
    assert d["cost"].sum() > 0


def test_forced_roll_only_when_front_expires_before_next_rebalance():
    # last trade on the 15th: the guarded front always outlives the month
    res15, _ = _run(ltd_day=15)
    assert res15.n_forced_rolls == 0
    # last trade on the 27th: the guard admits a contract that dies mid-month
    res27, _ = _run(ltd_day=27)
    assert res27.n_forced_rolls > 0


def test_skip_rule_when_most_positions_round_to_zero():
    cfg = copy.deepcopy(CFG)
    cfg["capital"]["book_size"] = 50_000          # ~0.25 contracts per position
    res, _ = _run(cfg=cfg)
    assert (res.log["status"] == "skipped: too many zero-contract positions").all()
    assert (res.daily["equity"] == 50_000).all()


def test_vol_leverage_matches_live_engine(monkeypatch):
    from storage_stress.execution import xsec_engine as E
    rng = np.random.default_rng(1)
    for n in (5, 31, 40, 63, 200):
        marks = 2e6 * np.cumprod(1 + rng.normal(0, 0.008, n))
        monkeypatch.setattr(E.B, "equity_curve",
                            lambda con, agent, m=marks: pd.DataFrame({"equity": m}))
        assert XR.vol_leverage(marks, CFG) == pytest.approx(
            E.agent_leverage(None, "agent_3", CFG))


def test_result_does_not_depend_on_input_row_order():
    px = _synthetic_px(15)
    px["delivery"] = px["lasttrddate"].dt.to_period("M").dt.to_timestamp()
    rebal = XR.last_weekdays("2010-01-01", "2011-12-30")
    out = []
    for frame in (px, px.sample(frac=1.0, random_state=3)):
        md = XR.MarketData(frame, SPECS)
        snap = XR.build_snapshots(md, TICKERS, rebal)
        ok = snap[snap["status"] == "ok"]
        pb = XR.PriceBook(md, pd.bdate_range(rebal[0], rebal[-1]))
        res = XR.simulate(["carry"], XR.agent_scorer("agent_1"),
                          {d: g for d, g in ok.groupby("date")}, md, pb, CFG)
        out.append(res.daily["equity"].to_numpy())
    np.testing.assert_array_equal(out[0], out[1])


def test_tied_expiries_are_not_a_term_structure():
    px = _synthetic_px(15)
    px["delivery"] = px["lasttrddate"].dt.to_period("M").dt.to_timestamp()
    d = pd.Timestamp("2011-03-31")
    md = XR.MarketData(px, SPECS)
    front, second = md.term_structure("AA", d)
    # stamp the second contract with the front's last trade date, as the
    # mirror does for CL/HO/NG/RB Mar-2007
    px.loc[px["futcode"] == second[0], "lasttrddate"] = front[1]
    f2, s2 = XR.MarketData(px, SPECS).term_structure("AA", d)
    assert f2[0] == front[0] and s2 is None


def test_margin_call_liquidates_and_stops_trading(monkeypatch):
    # size the book normally, but give the account almost nothing behind it
    monkeypatch.setattr(XR, "account_capacity", lambda acct: 10 * CFG["capital"]["book_size"])
    md = XR.MarketData(_synthetic_px(15), SPECS)
    rebal = XR.last_weekdays("2010-01-01", "2012-05-31")
    snap = XR.build_snapshots(md, TICKERS, rebal)
    ok = snap[snap["status"] == "ok"]
    pb = XR.PriceBook(md, pd.bdate_range(rebal[0], rebal[-1]))
    res = XR.simulate(["carry"], XR.agent_scorer("agent_1"),
                      {d: g for d, g in ok.groupby("date")}, md, pb, CFG,
                      account_equity0=1_000.0)       # gone on the first real loss
    assert res.exhausted_on is not None
    after = res.daily.loc[res.daily.index > res.exhausted_on]
    assert (after["gross"] == 0).all() and (after["pnl"] == 0).all()
    assert after["equity"].nunique() == 1
    assert (res.log["date"] <= res.exhausted_on).all()
