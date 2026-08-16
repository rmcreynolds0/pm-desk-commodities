"""
agent_zero.py - Roan McReynolds
=================================================
Agent Zero is the null benchmark, deliberately having no predictive signal: 
Each week it flips a coin to pick a trade type, sizes it
exactly like the real strategy, holds, and exits.
This file shows the full flow in one place:
    DATA  ->  SIGNAL  ->  DECISION  ->  ORDER  ->  FILL/ACCOUNTING  ->  EXIT

Two execution modes share the SAME decision/sizing code:
    mode="backtest"  -> fills against a historical spread series (no broker)
    mode="paper"     -> routes a real combo order to IBKR paper via ib_async

"""
from __future__ import annotations
from dataclasses import dataclass, field
import datetime as dt
import numpy as np
import pandas as pd


# Decision
@dataclass
class Decision:
    date: pd.Timestamp
    trade_type: str          # "A" (short spread) or "B" (long spread)
    side: int                # +1 long spread, -1 short spread
    reason: str


def season(d) -> str:
    return "injection" if 4 <= d.month <= 10 else "withdrawal"


def agent_zero_signal(d: pd.Timestamp, rng: np.random.Generator) -> Decision:
    """Coin-flip a trade type each week. We keep the season
    convention so the order matches Agents One/DSI exactly."""
    if rng.random() < 0.5:
        return Decision(d, "A", side=-1, reason="random Type A (sell front / buy deferred)")
    return Decision(d, "B", side=+1, reason="random Type B (buy front / sell deferred)")


# Sizing
def size_position(capital: float, spread_daily_sd: float,
                  risk_per_sd: float = 0.005,
                  point_value: float = 10_000.0) -> float:
    """Inverse-vol sizing, identical for every agent. Contracts such that one SD
    of daily spread move ~= risk_per_sd of the book."""
    if spread_daily_sd <= 0 or np.isnan(spread_daily_sd):
        return 0.0
    return (capital * risk_per_sd) / (spread_daily_sd * point_value)


# Paper-Trading Order
def place_combo_order_ibkr(decision: Decision, contracts: int, cfg):
    """Route a 2-leg NG calendar spread as an IBKR combo (BAG) order on paper.
    Only called in mode='paper'. Requires ib_async + running IB Gateway."""
    from ib_async import IB, Future, Contract, ComboLeg, MarketOrder
    ib = IB(); ib.connect(cfg.host, cfg.port, clientId=cfg.client_id, timeout=10)
    try:
        ng = Future(symbol="NG", exchange="NYMEX", currency="USD")
        det = sorted((d.contract for d in ib.reqContractDetails(ng)),
                     key=lambda c: c.lastTradeDateOrContractMonth)
        front, deferred = det[0], det[1]
        # Type A short spread = sell front, buy deferred ; Type B = opposite
        front_action = "SELL" if decision.trade_type == "A" else "BUY"
        defer_action = "BUY" if decision.trade_type == "A" else "SELL"
        bag = Contract(symbol="NG", secType="BAG", exchange="NYMEX", currency="USD")
        bag.comboLegs = [
            ComboLeg(conId=front.conId, ratio=1, action=front_action, exchange="NYMEX"),
            ComboLeg(conId=deferred.conId, ratio=1, action=defer_action, exchange="NYMEX"),
        ]
        order = MarketOrder("BUY", max(int(contracts), 1))  # BAG direction set by legs
        trade = ib.placeOrder(bag, order)
        ib.sleep(2)
        return {"status": trade.orderStatus.status,
                "front": front.localSymbol, "deferred": deferred.localSymbol}
    finally:
        ib.disconnect()


# Backtest Run
@dataclass
class Book:
    capital: float = 100_000.0
    equity: float = 100_000.0
    in_pos: int = 0
    contracts: float = 0.0
    entry_px: float = 0.0
    entry_vol: float = 0.0
    entry_date: pd.Timestamp | None = None
    log: list = field(default_factory=list)


def run_backtest(spread: pd.Series, spread_vol: pd.Series,
                 hold_days: int = 14, seed: int = 0,
                 slippage_ticks_per_leg: float = 1.0, tick_value: float = 10.0):
    """Agent Zero backtest. Coin-flip entry each week, 14-day hold (the proposal's
    Agent Zero spec), exits on time stop or 2x-vol adverse move. Returns equity df."""
    rng = np.random.default_rng(seed)
    spread = spread.dropna(); idx = spread.index
    bk = Book()
    eq = pd.Series(bk.capital, index=idx, dtype=float)
    point_value = 10_000.0

    for i, d in enumerate(idx):
        px = spread.iloc[i]
        # mark open position
        if bk.in_pos != 0 and i > 0:
            bk.equity += bk.in_pos * (px - spread.iloc[i-1]) * bk.contracts * point_value
            held = (d - bk.entry_date).days
            adverse = bk.in_pos * (px - bk.entry_px) < -2.0 * bk.entry_vol
            if held >= hold_days or adverse:
                bk.log.append({"date": d, "event": "EXIT",
                               "reason": "time" if held >= hold_days else "stop",
                               "equity": round(bk.equity, 2)})
                bk.in_pos = 0; bk.contracts = 0.0
        # decision + entry (flat only)
        if bk.in_pos == 0:
            dec = agent_zero_signal(d, rng)
            vol = spread_vol.get(d, np.nan)
            c = size_position(bk.capital, vol)
            if c > 0:
                bk.in_pos = dec.side; bk.contracts = c
                bk.entry_px = px; bk.entry_vol = vol; bk.entry_date = d
                bk.equity -= slippage_ticks_per_leg * 4 * tick_value * max(c, 1)
                bk.log.append({"date": d, "event": "ENTRY", "type": dec.trade_type,
                               "side": dec.side, "contracts": round(c, 2),
                               "px": round(px, 4)})
        eq.iloc[i] = bk.equity

    out = pd.DataFrame({"spread": spread, "equity": eq})
    out.attrs["log"] = pd.DataFrame(bk.log)
    return out


if __name__ == "__main__":
    from storage_stress.data import connectivity as C
    c1 = C.eia_henryhub_futures(1, synthetic=True)
    c2 = C.eia_henryhub_futures(2, synthetic=True)
    spread = (c1.iloc[:, 0] - c2.iloc[:, 0]).resample("W-FRI").last().dropna()
    vol = spread.diff().rolling(30).std()

    res = run_backtest(spread, vol, seed=7)
    log = res.attrs["log"]
    ret = res["equity"].pct_change().dropna()
    sharpe = 0.0 if ret.std() == 0 else np.sqrt(52) * ret.mean() / ret.std()
    dd = (res["equity"] / res["equity"].cummax() - 1).min()
    print("AGENT ZERO BACKTEST (synthetic data, illustrative)")
    print(f"  trades       : {(log['event']=='ENTRY').sum()}")
    print(f"  final equity : ${res['equity'].iloc[-1]:,.0f}")
    print(f"  Sharpe       : {sharpe:+.2f}  (expected ~0 — it's random by design)")
    print(f"  max drawdown : {dd:.1%}")
    print("\n  first 6 log events:")
    print(log.head(6).to_string(index=False))
