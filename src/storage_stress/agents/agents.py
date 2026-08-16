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
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

from storage_stress.signal.dsi import zscore, vol_regime_gate, ROLL_3Y


@dataclass
class Signal:
    side: int          # +1 long spread, -1 short spread, 0 flat
    size: float        # 0..1 risk scalar


def _season(date) -> str:
    return "injection" if 4 <= date.month <= 10 else "withdrawal"


# --- Agent Zero -------------------------------------------------------------
def agent_zero(dates: pd.DatetimeIndex, seed: int = 0, trade_every: int = 1) -> pd.Series:
    """Random +/-1 each period (the proposal's 'pick a trade type each Monday').
    On a weekly index every row is one week, so we act each row by default."""
    rng = np.random.default_rng(seed)
    sides = rng.choice([-1, 1], size=len(dates))
    out = pd.Series(0, index=dates)
    out.iloc[::trade_every] = sides[::trade_every]
    return out


# --- Agent One --------------------------------------------------------------
def agent_one(salt_flow: pd.Series, thresh: float = 1.5) -> pd.Series:
    z = zscore(salt_flow, window=5 * 52)
    side = pd.Series(0, index=salt_flow.index)
    for d in salt_flow.index:
        if np.isnan(z[d]):
            continue
        if _season(d) == "injection" and z[d] > thresh:
            side[d] = -1          # Type A: short the spread
        elif _season(d) == "withdrawal" and z[d] < -thresh:
            side[d] = +1          # Type B: long the spread
    return side


# --- Agent Two --------------------------------------------------------------
def agent_two(dsi_raw: pd.Series, thresh: float = 1.5) -> pd.Series:
    """The rung between Agent One and Agent DSI on the information ladder.

    Input is `dsi_raw` — the DSI pipeline THROUGH step 4 (utilization ->
    convex stress -> 3-week EWMA -> deseasonalized) but WITHOUT step 5
    (basis residualization) and WITHOUT the vol regime gate. Comparing
    agent_two vs agent_one isolates the value of the utilization/convexity/
    smoothing/deseasonalization stages; comparing agent_dsi vs agent_two
    isolates the value of residualization + gating.

    Uses the same 5y z-window and threshold as agent_one so the ONLY thing
    that differs from agent_one is the signal construction itself.
    """
    z = zscore(dsi_raw, window=5 * 52)
    side = pd.Series(0, index=dsi_raw.index)
    for d in dsi_raw.index:
        if np.isnan(z[d]):
            continue
        if _season(d) == "injection" and z[d] > thresh:
            side[d] = -1          # Type A: short the spread
        elif _season(d) == "withdrawal" and z[d] < -thresh:
            side[d] = +1          # Type B: long the spread
    return side


# --- Agent DSI --------------------------------------------------------------
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


# --- shared execution / backtest -------------------------------------------
def simulate(spread: pd.Series,
             side_signal: pd.Series,
             spread_vol: pd.Series,
             capital: float = 100_000,
             risk_per_sd: float = 0.005,
             max_hold_days: int = 42,
             slippage_ticks_per_leg: float = 1.0,
             tick_value: float = 10.0,      # $ per tick per spread (4 legs handled below)
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
    # Trading costs, accumulated SEPARATELY from equity. They must not be
    # written into `equity` directly: the equity recursion at the bottom of the
    # loop reassigns equity.iloc[i] outright, so anything written to that cell
    # earlier in the iteration is discarded. (That was a real bug -- slippage
    # was computed and then silently overwritten, making the whole backtest
    # cost-free and letting a 250-trade coin flip look profitable. The locked
    # strategy requires costs charged BEFORE judging.)
    cost_series = pd.Series(0.0, index=idx)
    trade_rows = []

    point_value = 10_000.0  # $ per 1.00 move in the spread, per contract (NG=10,000 MMBtu)

    for i, d in enumerate(idx):
        s = side_signal.get(d, 0)
        px = spread.iloc[i]
        vol = spread_vol.get(d, np.nan)

        # mark open position
        if in_pos != 0:
            # Did the underlying contract pair change this period? If so the
            # level jump is an artifact of splicing, not tradeable P&L.
            rolled = (roll_flag is not None
                      and bool(roll_flag.get(d, False)))
            step_pnl = (0.0 if rolled
                        else in_pos * (px - spread.iloc[i - 1])
                        * contracts * point_value)
            pnl_series.iloc[i] = step_pnl
            held_days = (d - idx[entry_i]).days
            # exits: contract roll, time stop, or stop loss (2x entry vol)
            adverse = in_pos * (px - entry_price) < -2.0 * (entry_vol)
            time_stop = held_days >= max_hold_days
            if rolled or time_stop or adverse:
                reason = ("roll" if rolled
                          else "time" if time_stop else "stop")
                trade_rows.append({"exit": d, "reason": reason,
                                   "held_days": held_days})
                in_pos = 0
                contracts = 0.0

        # entries (only if flat and signal fires)
        if in_pos == 0 and s != 0 and not np.isnan(vol) and vol > 0:
            in_pos = s
            entry_price = px
            entry_vol = vol
            entry_i = i
            contracts = (capital * risk_per_sd) / (vol * point_value)
            contracts = max(contracts, 0.0)
            # Round-trip slippage: 4 leg-charges (2 legs in, 2 legs out),
            # booked at entry. Recorded in cost_series so the equity recursion
            # below actually applies it.
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


# --- performance stats with bootstrap CI (proposal section 8) ---------------
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
