"""
xsec_replica.py — a backtest that trades by the LIVE engine's rules.
====================================================================

The research backtest (agents/xsec.py + scripts/run_xsec.py) measures the
FACTORS: continuous weights, rebalanced to target every day, chained front
returns, no contract sizes. That is the right tool for asking "is there a
premium?", and its published figures must not change.

This module asks a different question: "what would the book that is actually
trading have done?" So it follows execution/xsec_engine.py step by step:

  SIGNALS   computed only on rebalance dates, from what IBKR would list that day
            -- the two nearest contracts expiring more than DEFAULT_GUARD_DAYS
            (25) out -- with momentum measured on each contract's OWN trailing
            year of prices (xsec_signals_ib._period_return), not on a chained
            front series. Markets missing a required factor are dropped before
            z-scoring (xsec_signals_ib.usable).
  TIMING    the scheduler's calendar: the last weekday of each month.
  SIZING    a fixed $book_size (no compounding), capped by what the account
            can margin, converted to WHOLE contracts with Python's round().
            A month where >30% of wanted positions round to zero is skipped.
  VOL       leverage = vol_target / realised vol of the agent's own daily
            equity over the trailing window, set at each rebalance and held for
            the month (xsec_engine.agent_leverage), capped at max_leverage.
  P&L       contract counts are held between rebalances, so weights drift with
            prices. Every weekday is marked, as the live mark job does.
  COSTS     bps_per_unit_turnover charged on the dollar notional actually
            traded, including rolls.
  ACCOUNT   the agent's paper account starts at account_equity0 and moves with
            its P&L. If it reaches zero the book is liquidated and stops
            trading -- IBKR's margin call. The live code has no such guard: its
            capacity formula would go negative and flip every position.

TWO DELIBERATE DEPARTURES FROM THE LIVE CODE
--------------------------------------------
CLEAN ROLL. The live ledger keys positions by market, not by contract, and has
no roll step: when the front contract changes, the next rebalance sizes the
difference against the OLD contract's count and trades it in the NEW one. This
replica rolls cleanly instead: at a rebalance, any position held in a contract
that is no longer the front is closed in full and the target opened in the new
front, with both legs costed. If a held contract comes within
FORCED_ROLL_DAYS of its last trade date between rebalances, it is rolled into
the then-front at the same size (live has no equivalent; IBKR would
force-close it). Those forced rolls are counted and reported.

ONE CROSS-SECTION PER REBALANCE. The live panel stamps each market with the
date of its own last IBKR bar, and xsec.score / target_weights group by date,
so markets whose last bar dates differ would be ranked in separate groups.
Here every market is stamped with the rebalance date, so all of them are
ranked together -- the evident intent.

WHAT CANNOT BE REPLICATED FROM R2
---------------------------------
R2 holds daily SETTLEMENT prices. Live uses IBKR regular-hours trade closes
and fills near 10:30 ET; IBKR also returns no bar on days a thin contract did
not trade, whereas Datastream settles it every day. Multipliers are today's
IBKR values (data/processed/xsec_universe_check.csv) applied to the whole
history. Order rejections and partial fills are not modelled.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from storage_stress.agents import xsec
from storage_stress.execution import xsec_signals_ib as SIG
from storage_stress.execution.xsec_contracts import DEFAULT_GUARD_DAYS

FORCED_ROLL_DAYS = 5      # roll a held contract this close to its last trade date
STALENESS_DAYS = 10       # a market with no settlement this recent counts as "no bars"
SKIP_ZERO_FRAC = 0.30     # live: abort a rebalance if >30% of positions round to 0
MARGIN_RATE = 0.12        # live size_book(): conservative initial margin
MARGIN_BUFFER = 0.50      # live size_book(): 50% safety buffer


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------
def last_weekdays(start, end) -> list[pd.Timestamp]:
    """Last weekday of every month in [start, end] -- the live scheduler's
    is_last_business_day(), which ignores exchange holidays."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    for p in pd.period_range(start, end, freq="M"):
        d = p.to_timestamp(how="end").normalize()
        while d.weekday() > 4:
            d -= pd.Timedelta(days=1)
        if start <= d <= end:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Contract specs
# ---------------------------------------------------------------------------
def load_specs(path) -> dict[str, tuple[float, float]]:
    """{ticker: (multiplier, price_magnifier)} as IBKR reported them, from the
    committed universe check written by scripts/verify_xsec_universe.py."""
    df = pd.read_csv(path)
    df = df[df["status"] == "ok"]
    return {r["ticker"]: (float(r["mult"]), float(r["magnif"]))
            for _, r in df.iterrows()}


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
class MarketData:
    """R2 settlements indexed two ways: per market by date (to list the
    contracts printing on a day) and per contract by date (price history)."""

    def __init__(self, px: pd.DataFrame, specs: dict[str, tuple[float, float]]):
        cols = ["futcode", "date", "settlement", "ticker", "lasttrddate"]
        if "delivery" in px.columns:
            cols.append("delivery")
        px = px[cols].copy()
        px["futcode"] = px["futcode"].astype("int64")
        # Sort fully, so the result never depends on the row order DuckDB
        # returns. Delivery month breaks ties in last trade date: the mirror
        # has a few contracts stamped with a neighbour's last trade date
        # (e.g. CL/HO/NG/RB Mar-2007 carry Feb-2007's 2007-01-22).
        keys = ["ticker", "date", "lasttrddate"] + \
            (["delivery"] if "delivery" in px.columns else []) + ["futcode"]
        px = px.sort_values(keys).reset_index(drop=True)
        self.specs = specs
        self._tk = {
            t: (g["date"].to_numpy(), g["lasttrddate"].to_numpy(),
                g["futcode"].to_numpy(), g["settlement"].to_numpy())
            for t, g in px.groupby("ticker", sort=False)
        }
        by_fc = px.sort_values(["futcode", "date"])
        self._fc = {fc: (g["date"].to_numpy(), g["settlement"].to_numpy())
                    for fc, g in by_fc.groupby("futcode", sort=False)}
        self.expiry = dict(zip(px["futcode"], px["lasttrddate"]))
        self.last_date = px["date"].max()

    def term_structure(self, ticker: str, d: pd.Timestamp,
                       guard_days: int = DEFAULT_GUARD_DAYS,
                       staleness_days: int = STALENESS_DAYS):
        """(front, second) as IBKR would list them on `d`.

        Mirrors UniverseResolver.front_and_second(): contracts expiring more
        than `guard_days` after d, nearest first. Only contracts printing on the
        market's latest date <= d count as listed. Each leg is
        (futcode, last_trade_date, settlement) or None.
        """
        if ticker not in self._tk:
            return None, None
        dates, exps, fcs, setl = self._tk[ticker]
        d64 = np.datetime64(pd.Timestamp(d), "ns")
        j = np.searchsorted(dates, d64, side="right")
        if j == 0:
            return None, None
        dm = dates[j - 1]
        if d64 - dm > np.timedelta64(staleness_days, "D"):
            return None, None
        lo = np.searchsorted(dates, dm, side="left")
        cutoff = d64 + np.timedelta64(guard_days, "D")
        legs = [(int(fcs[k]), pd.Timestamp(exps[k]), float(setl[k]))
                for k in range(lo, j) if exps[k] > cutoff][:2]
        front = legs[0] if legs else None
        second = legs[1] if len(legs) > 1 else None
        if front and second and second[1] <= front[1]:
            # Bad expiry metadata: carry's dt_years would collapse to its
            # 1-day floor and explode. IBKR's dates are correct, so live never
            # sees this; treat it as no usable term structure that day.
            second = None
        return front, second

    def closes_1y(self, futcode: int, d: pd.Timestamp) -> pd.Series:
        """One contract's own prices over the year to `d` -- what
        reqHistoricalData(durationStr='1 Y') returns live."""
        dates, vals = self._fc[futcode]
        d64 = np.datetime64(pd.Timestamp(d), "ns")
        hi = np.searchsorted(dates, d64, side="right")
        lo = np.searchsorted(dates, d64 - np.timedelta64(365, "D"), side="right")
        return pd.Series(vals[lo:hi], index=pd.DatetimeIndex(dates[lo:hi]))

    def series(self, futcode: int) -> pd.Series:
        dates, vals = self._fc[futcode]
        return pd.Series(vals, index=pd.DatetimeIndex(dates))


def build_snapshots(md: MarketData, universe: list[str],
                    rebal_dates: list[pd.Timestamp],
                    momentum_months: int = 12) -> pd.DataFrame:
    """The live factor panel (xsec_signals_ib.build_live_panel), rebuilt for
    every historical rebalance date. Column names match the live panel so
    SIG.usable, xsec.score and xsec.target_weights run on it unchanged."""
    rows = []
    for d in rebal_dates:
        for t in universe:
            front, second = md.term_structure(t, d)
            if front is None or second is None:
                rows.append({"date": d, "ticker": t, "status": "no term structure"})
                continue
            f_code, f_exp, f_px = front
            s_code, s_exp, s_px = second
            dt_years = max((s_exp - f_exp).days, 1) / 365.25
            mom_f = SIG._period_return(md.closes_1y(f_code, d), momentum_months)
            mom_s = SIG._period_return(md.closes_1y(s_code, d), momentum_months)
            mult, mag = md.specs[t]
            rows.append({
                "date": d, "ticker": t, "status": "ok",
                "front_code": f_code, "second_code": s_code,
                "front_exp": f_exp, "second_exp": s_exp,
                "front_px": f_px, "second_px": s_px, "dt_years": dt_years,
                "carry": np.log(f_px / s_px) / dt_years,
                "mom": mom_f, "mom_second": mom_s,
                "basis_mom": (np.nan if (mom_f is None or mom_s is None)
                              else mom_f - mom_s),
                "multiplier": mult, "magnifier": mag,
                "notional": f_px * mult / mag,
            })
    snap = pd.DataFrame(rows)
    for c in ("mom", "mom_second"):
        snap[c] = pd.to_numeric(snap[c], errors="coerce")
    return snap


# ---------------------------------------------------------------------------
# Pricing on the weekday mark calendar
# ---------------------------------------------------------------------------
class PriceBook:
    """Each contract's settlement forward-filled onto the weekday calendar, so
    a holiday mark repeats the last price as the live mark job would."""

    def __init__(self, md: MarketData, cal: pd.DatetimeIndex):
        self.md, self.cal = md, cal
        self._cache: dict[int, tuple[int, np.ndarray]] = {}

    def _arr(self, fc: int) -> tuple[int, np.ndarray]:
        if fc not in self._cache:
            s = self.md.series(fc)
            i0 = int(self.cal.searchsorted(s.index[0]))
            i1 = int(self.cal.searchsorted(s.index[-1], side="right"))
            sub = self.cal[i0:i1]
            a = s.reindex(s.index.union(sub)).ffill().reindex(sub).to_numpy()
            self._cache[fc] = (i0, a)
        return self._cache[fc]

    def price(self, fc: int, i: int) -> float:
        i0, a = self._arr(fc)
        k = i - i0
        if k < 0 or len(a) == 0:
            return np.nan
        return float(a[min(k, len(a) - 1)])


# ---------------------------------------------------------------------------
# Live rules
# ---------------------------------------------------------------------------
def vol_leverage(marks: np.ndarray, cfg: dict) -> float:
    """xsec_engine.agent_leverage(), on an array of daily equity marks."""
    p = cfg["portfolio"]
    window = p["vol_window_days"]
    if len(marks) < window // 2:
        return 1.0
    r = pd.Series(marks).pct_change().dropna()
    if len(r) < window // 2 or r.std() == 0:
        return 1.0
    realised = r.tail(window).std() * np.sqrt(252)
    if not np.isfinite(realised) or realised <= 0:
        return 1.0
    return float(min(p["vol_target"] / realised, p["max_leverage"]))


def account_capacity(account_equity: float) -> float:
    """xsec_engine.size_book() capacity for one agent on its own account."""
    return (account_equity * MARGIN_BUFFER) / (2 * 1 * MARGIN_RATE)


@dataclass
class Holding:
    futcode: int
    contracts: int
    mult: float
    magnif: float


@dataclass
class SimResult:
    daily: pd.DataFrame        # equity, pnl, cost, traded, gross per weekday
    log: pd.DataFrame          # one row per rebalance
    n_forced_rolls: int
    exhausted_on: pd.Timestamp | None = None   # date the paper account ran out


Scorer = Callable[[pd.DataFrame, int], pd.DataFrame]


def agent_scorer(agent: str) -> Scorer:
    """The live scoring call: xsec.score(panel_today, agent, seed=0). For
    agent_0 that redraws the SAME random numbers every month, as live does."""
    def _score(ok: pd.DataFrame, k: int) -> pd.DataFrame:
        return xsec.score(ok, agent, seed=0)
    return _score


def null_scorer(seed: int) -> Scorer:
    """A random agent that draws fresh scores at every rebalance -- the fair
    null for the replica's percentile test."""
    def _score(ok: pd.DataFrame, k: int) -> pd.DataFrame:
        rng = np.random.default_rng([seed, k])
        return ok.assign(score=rng.standard_normal(len(ok)))
    return _score


def simulate(factors: list[str], scorer: Scorer, snaps: dict, md: MarketData,
             pb: PriceBook, cfg: dict,
             account_equity0: float = 1_000_000.0) -> SimResult:
    """Run one agent through the live rules on the weekday calendar `pb.cal`.

    `snaps` maps rebalance date -> that date's status-ok factor rows (in config
    universe order). Each weekday: mark held contracts, roll any contract about
    to expire, then rebalance if it is a rebalance date.
    """
    cal = pb.cal
    book_size = float(cfg["capital"]["book_size"])
    bps = float(cfg["costs"]["bps_per_unit_turnover"]) / 1e4
    quantile = cfg["portfolio"]["quantile"]
    min_markets = cfg["portfolio"]["min_markets"]

    n = len(cal)
    equity = np.empty(n)
    pnl_a, cost_a, traded_a, gross_a = (np.zeros(n) for _ in range(4))
    held: dict[str, Holding] = {}
    cum = 0.0
    n_forced = 0
    log = []
    k_rebal = 0
    exhausted_on = None

    for i, t in enumerate(cal):
        # 1. mark existing holdings from yesterday's price to today's
        pnl = 0.0
        if i > 0:
            for h in held.values():
                p0, p1 = pb.price(h.futcode, i - 1), pb.price(h.futcode, i)
                if np.isfinite(p0) and np.isfinite(p1):
                    pnl += h.contracts * (p1 - p0) * h.mult / h.magnif

        traded = 0.0

        # 1b. margin call: the paper account holds account_equity0 + P&L. Once
        # that is gone IBKR liquidates, so close everything and stop trading.
        # (Without this the capacity formula goes negative and flips the book.)
        if exhausted_on is None and account_equity0 + cum + pnl <= 0:
            for h in held.values():
                px_now = pb.price(h.futcode, i)
                if np.isfinite(px_now):
                    traded += abs(h.contracts) * px_now * h.mult / h.magnif
            held.clear()
            exhausted_on = t
        if exhausted_on is not None:
            cost = traded * bps
            cum += pnl - cost
            equity[i] = book_size + cum
            pnl_a[i], cost_a[i], traded_a[i] = pnl, cost, traded
            continue

        # 2. forced roll: a held contract about to expire moves to the front
        for ticker, h in list(held.items()):
            exp = md.expiry.get(h.futcode)
            if exp is None or (exp - t).days > FORCED_ROLL_DAYS:
                continue
            front, _ = md.term_structure(ticker, t)
            if front is None or front[0] == h.futcode:
                continue
            old_px, new_px = pb.price(h.futcode, i), pb.price(front[0], i)
            if not (np.isfinite(old_px) and np.isfinite(new_px)):
                continue
            traded += abs(h.contracts) * (old_px + new_px) * h.mult / h.magnif
            held[ticker] = Holding(front[0], h.contracts, h.mult, h.magnif)
            n_forced += 1

        # 3. monthly rebalance
        if t in snaps:
            entry, rebal_traded = _rebalance(
                t, i, k_rebal, snaps[t], factors, scorer, held, pb, equity,
                book_size, account_equity0, quantile, min_markets, cfg)
            traded += rebal_traded
            entry["cost"] = rebal_traded * bps
            log.append(entry)
            k_rebal += 1

        cost = traded * bps
        cum += pnl - cost
        equity[i] = book_size + cum
        pnl_a[i], cost_a[i], traded_a[i] = pnl, cost, traded
        gross_a[i] = sum(abs(h.contracts) * pb.price(h.futcode, i) * h.mult / h.magnif
                         for h in held.values())

    daily = pd.DataFrame({"equity": equity, "pnl": pnl_a, "cost": cost_a,
                          "traded": traded_a, "gross": gross_a}, index=cal)
    return SimResult(daily, pd.DataFrame(log), n_forced, exhausted_on)


def _rebalance(t, i, k, ok_all, factors, scorer, held, pb, equity, book_size,
               account_equity0, quantile, min_markets, cfg):
    """xsec_engine.run_rebalance() for one agent on one date, with a clean
    roll. Returns (log entry, dollar notional traded)."""
    entry = {"date": t, "status": "traded"}
    ok = SIG.usable(ok_all, factors)
    entry["n_usable"] = len(ok)
    if len(ok) < min_markets:
        entry["status"] = "skipped: below min_markets"
        return entry, 0.0

    scored = scorer(ok, k)
    weights = xsec.target_weights(scored, quantile=quantile)
    if weights.empty:
        entry["status"] = "skipped: no targets"
        return entry, 0.0

    marks = equity[:i]
    lev = vol_leverage(marks, cfg)
    acct = account_equity0 + ((marks[-1] - book_size) if len(marks) else 0.0)
    book = min(book_size, account_capacity(acct))
    entry.update({"leverage": lev, "book": book})

    market = {r["ticker"]: r for _, r in ok_all.iterrows()}
    wmap = dict(zip(weights["ticker"], weights["w"]))

    wanted = [(tk, w) for tk, w in wmap.items() if w != 0 and tk in market]
    ideal = {tk: w * book * lev / market[tk]["notional"] for tk, w in wanted}
    n_zeroed = sum(1 for v in ideal.values() if round(v) == 0)
    entry.update({"n_long": sum(1 for _, w in wanted if w > 0),
                  "n_short": sum(1 for _, w in wanted if w < 0),
                  "n_zeroed": n_zeroed})
    if wanted and n_zeroed / len(wanted) > SKIP_ZERO_FRAC:
        entry["status"] = "skipped: too many zero-contract positions"
        return entry, 0.0

    traded = 0.0
    n_rolled = 0
    for ticker in sorted(set(wmap) | set(held)):
        info = market.get(ticker)
        if info is None:
            continue                     # live: no price -> holding untouched
        w = wmap.get(ticker, 0.0)
        target = round(w * book * lev / info["notional"])
        front = int(info["front_code"])
        h = held.get(ticker)
        current = 0
        if h is not None:
            if h.futcode != front:       # CLEAN ROLL: close the old contract
                old_px = pb.price(h.futcode, i)
                if np.isfinite(old_px):
                    traded += abs(h.contracts) * old_px * h.mult / h.magnif
                del held[ticker]
                n_rolled += 1
            else:
                current = h.contracts
        traded += abs(target - current) * info["notional"]
        if target == 0:
            held.pop(ticker, None)
        else:
            held[ticker] = Holding(front, target, float(info["multiplier"]),
                                   float(info["magnifier"]))

    errs = [abs(round(v) / v - 1) for v in ideal.values() if v != 0]
    entry.update({"traded": traded, "n_rolled": n_rolled,
                  "n_positions": len(held),
                  "holdings": ";".join(f"{tk}:{h.futcode}:{h.contracts:+d}"
                                       for tk, h in sorted(held.items())),
                  "weight_error": float(np.mean(errs)) if errs else np.nan})
    return entry, traded


def perf(daily: pd.DataFrame, capital: float) -> dict:
    """Headline stats on the replica's equity, normalised to the starting book."""
    eq = daily["equity"] / capital
    ret = eq.pct_change()
    ret.iloc[0] = eq.iloc[0] - 1
    return xsec.perf_stats(pd.DataFrame({"ret_net": ret, "equity": eq}))
