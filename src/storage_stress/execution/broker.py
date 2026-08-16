"""
broker.py — the only module that talks to IBKR (via ib_async).
==============================================================

Responsibilities (and deliberately nothing more):
  1. connect/disconnect to IB Gateway with retries (the gateway restarts
     nightly, so transient connection failures are NORMAL, not fatal),
  2. resolve the NG futures chain and pick the front + seasonal deferred
     legs using the pure calendar logic in contracts.py,
  3. pull daily historical bars for both legs and build the SPREAD SERIES —
     this doubles as the live price feed, replacing the EIA RNGC1/RNGC2
     series that EIA discontinued in April 2024 (data gap #1 in
     docs/FRAMEWORK.md),
  4. place 2-leg BAG combo orders TAGGED with orderRef=<agent name> — the
     tag is what attributes every fill in the shared paper account to
     exactly one agent's book.

Everything returns plain python/pandas objects so the engine and the tests
never need to import ib_async themselves. ib_async is imported lazily inside
the class so the whole research stack keeps working on machines without the
[live] extra installed.
"""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd

from storage_stress.execution import contracts as C


class Broker:
    """Thin, stateful wrapper around one IB Gateway connection."""

    def __init__(self, host: str, port: int, client_id: int,
                 market_data_type: int = 4, timeout_s: int = 15,
                 retries: int = 3):
        # Connection parameters come straight from config/live.yaml.
        self.host = host
        self.port = port
        self.client_id = client_id
        self.market_data_type = market_data_type
        self.timeout_s = timeout_s
        self.retries = retries
        self.ib = None          # set by connect()
        self._chain = None      # cached contract chain for this session

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def connect(self):
        """Connect with retries. Each retry backs off 10s because the usual
        failure mode is 'gateway is mid-restart' which resolves itself."""
        from ib_async import IB  # lazy import — see module docstring
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                self.ib = IB()
                self.ib.connect(self.host, self.port, clientId=self.client_id,
                                timeout=self.timeout_s)
                # Delayed(-frozen) data is fine for weekly decisions & daily
                # marks, and works without a paid CME market-data subscription.
                self.ib.reqMarketDataType(self.market_data_type)
                return self
            except Exception as e:                      # noqa: BLE001
                last_err = e
                time.sleep(10 * attempt)
        raise ConnectionError(
            f"IB Gateway unreachable at {self.host}:{self.port} after "
            f"{self.retries} attempts: {last_err}")

    def disconnect(self):
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.disconnect()

    # ------------------------------------------------------------------ #
    # Contract resolution
    # ------------------------------------------------------------------ #
    def ng_chain(self):
        """All listed NG futures sorted by expiry. Cached per session because
        the chain is stable within a job run and the request is slow."""
        from ib_async import Future
        if self._chain is None:
            details = self.ib.reqContractDetails(
                Future(symbol="NG", exchange="NYMEX", currency="USD"))
            self._chain = sorted((d.contract for d in details),
                                 key=lambda c: c.lastTradeDateOrContractMonth)
        return self._chain

    def resolve_legs(self, today: dt.date, guard_bd: int = C.DEFAULT_GUARD_BD):
        """Map the strategy's (front, seasonal deferred) months onto real
        contracts from the chain.

        Returns (front_contract, deferred_contract, front_expiry_date).
        Raises if either month is missing from the chain — that is a hard
        error the engine must surface, not paper over, because trading a
        wrong month would silently change the locked strategy.
        """
        chain = self.ng_chain()

        # The nearest contract's true expiry drives the delivery guard.
        nearest_expiry = _expiry_of(chain[0])
        front_ym = C.front_delivery_month(today, front_expiry=nearest_expiry,
                                          guard_bd=guard_bd)
        deferred_ym = C.deferred_delivery_month(front_ym)

        front = _find_month(chain, front_ym)
        deferred = _find_month(chain, deferred_ym)
        if front is None or deferred is None:
            raise LookupError(
                f"NG chain is missing {front_ym if front is None else deferred_ym}"
                " — cannot build the seasonal spread.")
        return front, deferred, _expiry_of(front)

    # ------------------------------------------------------------------ #
    # Historical data -> the live spread series (replaces dead EIA feed)
    # ------------------------------------------------------------------ #
    def daily_closes(self, contract, duration: str = "2 Y") -> pd.Series:
        """Daily settle/close series for one leg. useRTH + TRADES gives the
        official session closes, which is what the backtest used from EIA."""
        bars = self.ib.reqHistoricalData(
            contract, endDateTime="", durationStr=duration,
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True)
        if not bars:
            return pd.Series(dtype=float)
        s = pd.Series({pd.Timestamp(b.date): b.close for b in bars})
        s.index = pd.to_datetime(s.index)
        return s.sort_index()

    def spread_series(self, front, deferred, duration: str = "2 Y") -> pd.Series:
        """spread = front settle - deferred settle, on their common dates.

        The inner join drops days where one leg didn't print (fresh contracts
        have shorter history) — identical alignment rule to the backtest's
        C1-C2 construction, so vol numbers remain comparable.
        """
        f = self.daily_closes(front, duration)
        d = self.daily_closes(deferred, duration)
        df = pd.concat({"f": f, "d": d}, axis=1, join="inner")
        return (df["f"] - df["d"]).rename("spread")

    # ------------------------------------------------------------------ #
    # Orders — the ONLY place orders are created, always orderRef-tagged
    # ------------------------------------------------------------------ #
    def place_spread_order(self, agent: str, side: int, quantity: int,
                           front, deferred, action: str,
                           wait_s: int = 30) -> dict:
        """Route a 2-leg NG calendar spread as a BAG combo, tagged to `agent`.

        side  : +1 = LONG the spread (buy front / sell deferred)
                -1 = SHORT the spread (sell front / buy deferred)
        action: 'ENTRY' or 'EXIT' — recorded into orderRef so account-level
                reconciliation can pair round trips without the ledger.

        The combo is bought with leg actions expressing direction (standard
        IBKR BAG idiom): the parent MarketOrder is always BUY, and the legs
        carry the BUY/SELL sides.
        """
        from ib_async import Contract, ComboLeg, MarketOrder

        front_action = "BUY" if side > 0 else "SELL"
        defer_action = "SELL" if side > 0 else "BUY"

        bag = Contract(symbol="NG", secType="BAG", exchange="NYMEX",
                       currency="USD")
        bag.comboLegs = [
            ComboLeg(conId=front.conId, ratio=1, action=front_action,
                     exchange="NYMEX"),
            ComboLeg(conId=deferred.conId, ratio=1, action=defer_action,
                     exchange="NYMEX"),
        ]

        order = MarketOrder("BUY", max(int(quantity), 1))
        # THE attribution mechanism: every fill in the shared paper account
        # carries this tag. Format: '<agent>|<ENTRY/EXIT>|<iso date>'.
        order.orderRef = f"{agent}|{action}|{dt.date.today().isoformat()}"

        trade = self.ib.placeOrder(bag, order)

        # Market orders on liquid NG spreads fill in seconds; poll briefly
        # rather than blocking forever — the ledger records whatever status
        # we reach and reconciliation can pick up stragglers later.
        deadline = time.time() + wait_s
        while time.time() < deadline and not trade.isDone():
            self.ib.sleep(1)

        fill_px = trade.orderStatus.avgFillPrice or None
        return {
            "ib_order_id": trade.order.orderId,
            "status": trade.orderStatus.status,
            "avg_fill_price": fill_px,
            "filled": trade.orderStatus.filled,
            "order_ref": order.orderRef,
            "front_local": front.localSymbol,
            "deferred_local": deferred.localSymbol,
        }


# ---------------------------------------------------------------------------
# Small chain helpers (module-level so they're unit-testable with fakes)
# ---------------------------------------------------------------------------
def _expiry_of(contract) -> dt.date:
    """Parse IBKR's lastTradeDateOrContractMonth ('YYYYMMDD' or 'YYYYMM')."""
    raw = contract.lastTradeDateOrContractMonth
    if len(raw) >= 8:
        return dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    # Month-only form: assume end-of-prior-month expiry — only used for
    # sorting/guards where a few days of slack is acceptable & conservative.
    y, m = int(raw[:4]), int(raw[4:6])
    return dt.date(y, m, 1) - dt.timedelta(days=1)


def _find_month(chain, ym: tuple[int, int]):
    """The chain contract whose DELIVERY month equals `ym`.

    NG contract for delivery month M expires in month M-1, so we match on
    contract month (the first 6 chars of lastTradeDateOrContractMonth refer
    to expiry date whose month is delivery-month minus one when a full date
    is given). To stay robust across both formats we match either the
    contract-month form directly or expiry-month + 1.
    """
    code = C.month_code(ym)
    for c in chain:
        raw = c.lastTradeDateOrContractMonth
        if raw[:6] == code:                      # 'YYYYMM' contract-month form
            return c
        if len(raw) >= 8:                        # full expiry date form
            exp = dt.date(int(raw[:4]), int(raw[4:6]), 1)
            delivery = (exp.year + (exp.month == 12), (exp.month % 12) + 1)
            if delivery == ym:
                return c
    return None
