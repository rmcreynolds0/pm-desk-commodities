"""
agents.py
=========
Three agents sharing one execution path (proposal section 7):

  Agent Zero : random trade each Monday. Null benchmark.
  Agent One  : 5y rolling z-score of salt net flow. Simple benchmark.
  Agent DSI  : full residualized-DSI signal (sections 4-6).

All three emit the SAME signal object {date, side, size_scalar} and run through
the SAME `simulate()` function, so plumbing bugs surface against trivial signals
before they reach the real strategy.

Trade types (section 6):
  Type A (injection season Apr-Oct): sell front, buy next Jan  -> short spread
  Type B (withdrawal season Nov-Mar): buy front, sell next Apr -> long spread
We model the calendar spread P&L directly as the change in (C1 - C2) settle.

STATUS: RETIRED. This is the natural-gas strategy, kept as the evidence behind
its own retirement. The live book is the cross-sectional one in agents/xsec.py.
Spelled-out agent names (agent_zero, agent_one, agent_dsi) are this retired
set; numerals (agent_0 .. agent_3) are the live ladder. `agent_two` exists in
BOTH modules meaning different things, which has already caused confusion.

TRAPS -- both of these made the backtest profitable when it was not
-------------------------------------------------------------------
roll_flag MUST be passed to simulate(). Without it, the price jump at every
contract roll is booked as P&L. Measured on a spliced series those jumps were
8.1x, 6.6x and 14.4x a normal daily move, accounting for 31%, 27% and 6% of
ALL price movement in the three instruments. Spread RETURNS used for the vol
gate must exclude them too, or the gate fires on phantom volatility.

Costs accumulate in a SEPARATE series, never by subtracting from equity in
place. An earlier version did `equity.iloc[i] -= slippage` and then the equity
recursion overwrote that same cell on the next line, so every backtest ran
cost-free. A 253-trade coin flip looked profitable purely because it never
paid to trade. Both failures have regression tests in tests/test_execution.py.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

from storage_stress.signal.dsi import zscore, vol_regime_gate, ROLL_3Y


@dataclass
class Signal:
    side: int
    size: float


def _season(date) -> str:
    return "injection" if 4 <= date.month <= 10 else "withdrawal"


def agent_zero(dates: pd.DatetimeIndex, seed: int = 0, trade_every: int = 1) -> pd.Series:
    """Random +/-1 each period (the proposal's 'pick a trade type each Monday').
    On a weekly index every row is one week, so we act each row by default."""
    rng = np.random.default_rng(seed)
    sides = rng.choice([-1, 1], size=len(dates))
    out = pd.Series(0, index=dates)
    out.iloc[::trade_every] = sides[::trade_every]
    return out


def agent_one(salt_flow: pd.Series, thresh: float = 1.5) -> pd.Series:
    z = zscore(salt_flow, window=5 * 52)
    side = pd.Series(0, index=salt_flow.index)
    for d in salt_flow.index:
        if np.isnan(z[d]):
            continue
        if _season(d) == "injection" and z[d] > thresh:
            side[d] = -1
        elif _season(d) == "withdrawal" and z[d] < -thresh:
            side[d] = +1
    return side


def agent_two(dsi_raw: pd.Series, thresh: float = 1.5) -> pd.Series:

    z = zscore(dsi_raw, window=5 * 52)
    side = pd.Series(0, index=dsi_raw.index)
    for d in dsi_raw.index:
        if np.isnan(z[d]):
            continue
        if _season(d) == "injection" and z[d] > thresh:
            side[d] = -1
        elif _season(d) == "withdrawal" and z[d] < -thresh:
            side[d] = +1
    return side


def agent_dsi(dsi_clean: pd.Series,
              spread_returns: pd.Series,
              entry_z: float = 2.0) -> pd.Series:
    z = zscore(dsi_clean, window=ROLL_3Y)
    gate = vol_regime_gate(spread_returns)
    side = pd.Series(0, index=dsi_clean.index)
    for d in dsi_clean.index:
        if np.isnan(z[d]) or not gate.get(d, False):
            continue
        if _season(d) == "injection" and z[d] > entry_z:
            side[d] = -1
        elif _season(d) == "withdrawal" and z[d] < -entry_z:
            side[d] = +1
    return side


def simulate(spread: pd.Series,
             side_signal: pd.Series,
             spread_vol: pd.Series,
             capital: float = 100_000,
             risk_per_sd: float = 0.005,
             max_hold_days: int = 42,
             slippage_ticks_per_leg: float = 1.0,
             tick_value: float = 10.0,
             exit_frac: float = 0.60,
             roll_flag: pd.Series | None = None) -> pd.DataFrame:
    """Vectorized-ish event loop over a weekly spread series.

    Position sizing: contracts = (capital * risk_per_sd) / (spread_vol * point_value)
    -> inverse-vol sizing (proposal section 6). P&L in $ on the spread.
    Slippage: 4 legs round trip * ticks * tick_value charged on entry.
    Returns per-period equity and trade log columns.

    roll_flag : optional boolean Series, True on periods where the underlying
        CONTRACT PAIR changed. This matters enormously and is easy to get wrong.

        A multi-year spread series is SPLICED from successive contract pairs
        (Feb/Mar, then Mar/Apr, ...). On the splice date the quoted level jumps
        because the two pairs are different instruments -- e.g. Feb/Mar at
        +0.673 one day, Mar/Apr at +0.093 the next. Differencing the spliced
        series books that -0.580 as P&L, but it is PHANTOM: in reality you
        close one spread and open another, and the switch earns nothing.

        Measured on real NG data, roll-day jumps are 6-14x a normal daily move
        and account for ~27-31% of all movement in the prompt/seasonal series --
        enough to fabricate profits for an always-invested agent.

        When roll_flag is supplied we therefore (a) book ZERO P&L on roll
        periods and (b) force the position closed, which is what the live
        engine does anyway: its delivery guard exits before expiry, so it never
        holds across a roll.
    """
    spread = spread.dropna()
    idx = spread.index
    equity = pd.Series(capital, index=idx, dtype=float)
    in_pos = 0
    entry_price = entry_z_level = 0.0
    entry_i = -1
    contracts = 0.0
    pnl_series = pd.Series(0.0, index=idx)
    cost_series = pd.Series(0.0, index=idx)
    trade_rows = []

    point_value = 10_000.0

    for i, d in enumerate(idx):
        s = side_signal.get(d, 0)
        px = spread.iloc[i]
        vol = spread_vol.get(d, np.nan)

        if in_pos != 0:
            rolled = (roll_flag is not None
                      and bool(roll_flag.get(d, False)))
            step_pnl = (0.0 if rolled
                        else in_pos * (px - spread.iloc[i - 1])
                        * contracts * point_value)
            pnl_series.iloc[i] = step_pnl
            held_days = (d - idx[entry_i]).days
            adverse = in_pos * (px - entry_price) < -2.0 * (entry_vol)
            time_stop = held_days >= max_hold_days
            if rolled or time_stop or adverse:
                reason = ("roll" if rolled
                          else "time" if time_stop else "stop")
                trade_rows.append({"exit": d, "reason": reason,
                                   "held_days": held_days})
                in_pos = 0
                contracts = 0.0

        if in_pos == 0 and s != 0 and not np.isnan(vol) and vol > 0:
            in_pos = s
            entry_price = px
            entry_vol = vol
            entry_i = i
            contracts = (capital * risk_per_sd) / (vol * point_value)
            contracts = max(contracts, 0.0)
            cost_series.iloc[i] += (slippage_ticks_per_leg * 4 * tick_value
                                    * max(contracts, 1))
            trade_rows.append({"entry": d, "side": s,
                               "contracts": round(contracts, 2),
                               "entry_px": round(px, 4)})

        equity.iloc[i] = ((equity.iloc[i - 1] if i > 0 else capital)
                          + pnl_series.iloc[i] - cost_series.iloc[i])

    out = pd.DataFrame({"spread": spread, "equity": equity,
                        "pnl": pnl_series, "cost": cost_series,
                        "side": side_signal.reindex(idx).fillna(0)})
    out.attrs["trades"] = pd.DataFrame(trade_rows)
    out.attrs["total_cost"] = float(cost_series.sum())
    return out


def sharpe(returns: pd.Series, periods_per_year: int = 52) -> float:
    r = returns.dropna()
    if r.std() == 0 or len(r) < 2:
        return 0.0
    return np.sqrt(periods_per_year) * r.mean() / r.std()


def bootstrap_sharpe_ci(returns: pd.Series, n_boot: int = 2000,
                        ci: float = 0.90, seed: int = 42):
    r = returns.dropna().values
    rng = np.random.default_rng(seed)
    stats = np.empty(n_boot)
    for b in range(n_boot):
        sample = rng.choice(r, size=len(r), replace=True)
        sd = sample.std()
        stats[b] = 0.0 if sd == 0 else np.sqrt(52) * sample.mean() / sd
    lo = np.percentile(stats, (1 - ci) / 2 * 100)
    hi = np.percentile(stats, (1 + ci) / 2 * 100)
    return sharpe(returns), (lo, hi)
