"""
contracts.py — seasonal spread leg selection + delivery-notice guard.
====================================================================

Implements the LOCKED strategy's spread definition (docs/strategy.md):

    Injection season  (Apr–Oct): front month vs **next January**
    Withdrawal season (Nov–Mar): front month vs **next April**

and the safety rule "never take delivery — close every position >= 5 business
days before delivery notice."

DESIGN NOTE — pure functions only
---------------------------------
Everything in this module is deliberately broker-free and side-effect-free:
inputs are dates, outputs are (year, month) tuples or booleans. The broker
wrapper (broker.py) maps these month tuples onto the actual IBKR contract
chain (which carries the true lastTradeDate for each contract). Keeping the
calendar math pure makes it trivially unit-testable — important because a
wrong deferred month silently changes the strategy, and a wrong guard date
risks physical delivery.

NG contract mechanics (why the guard works the way it does):
  * NYMEX NG for delivery month M stops trading 3 business days before the
    first calendar day of M; delivery notice follows expiry.
  * We therefore treat a leg as "too close to delivery" when TODAY is within
    `guard_bd` business days of its expiry (lastTradeDate). 5 business days
    is the locked parameter (instruments.yaml close_before_delivery_business_days).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

DEFAULT_GUARD_BD = 5


def season_of(date: dt.date | pd.Timestamp) -> str:
    """Injection = Apr..Oct, Withdrawal = Nov..Mar (proposal section 6).

    Same convention as agents._season(); duplicated at this layer so the
    execution package does not import from the signal/backtest layer.
    """
    return "injection" if 4 <= date.month <= 10 else "withdrawal"


def business_days_between(start: dt.date, end: dt.date) -> int:
    """Whole business days from `start` (exclusive) to `end` (inclusive).

    np.busday_count counts Mon–Fri days in [start, end). We shift both ends
    by one day so the semantics become "how many trading opportunities remain
    AFTER today, up to and including expiry day" — the number the delivery
    guard actually cares about. Holidays are ignored (conservative enough:
    ignoring holidays only ever makes us close EARLIER, never later).
    """
    s = np.datetime64(start) + np.timedelta64(1, "D")
    e = np.datetime64(end) + np.timedelta64(1, "D")
    return int(np.busday_count(s, e))


def front_delivery_month(today: dt.date, front_expiry: dt.date | None = None,
                         guard_bd: int = DEFAULT_GUARD_BD) -> tuple[int, int]:
    """The delivery (year, month) of the front NG contract we are WILLING to trade.

    Two modes:
      * front_expiry given (normal live path): the broker looked up the actual
        nearest contract; if its expiry is within the guard window we roll to
        the next month, otherwise we keep it.
      * front_expiry None (offline/tests): approximate — the contract for next
        month expires ~3 bd before month end, so late in the month (inside the
        guard) we skip one month ahead.
    """
    if front_expiry is not None:
        base = _next_month(today)
        if business_days_between(today, front_expiry) <= guard_bd:
            return _next_month(dt.date(base[0], base[1], 1))
        return base

    last_dom = (dt.date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
                - dt.timedelta(days=1))
    remaining_bd = business_days_between(today, last_dom)
    base = _next_month(today)
    if remaining_bd <= guard_bd + 3:
        return _next_month(dt.date(base[0], base[1], 1))
    return base


def deferred_delivery_month(front: tuple[int, int]) -> tuple[int, int]:
    """The seasonal deferred leg for a given front delivery month.

    Injection-season fronts (Apr–Oct delivery) pair with the NEXT January —
    the month the market must carry gas toward through injection season.
    Withdrawal-season fronts (Nov–Mar delivery) pair with the NEXT April —
    the first month of the following injection season.
    Both are strictly AFTER the front month by construction.
    """
    y, m = front
    if 4 <= m <= 10:
        return (y + 1, 1)
    if m in (11, 12):
        return (y + 1, 4)
    return (y, 4)


def must_exit(today: dt.date, front_expiry: dt.date,
              guard_bd: int = DEFAULT_GUARD_BD) -> bool:
    """True when an OPEN position's front leg is inside the delivery guard.

    Checked by the daily mark job; overrides every other exit rule because
    taking delivery of physical natural gas is the one unrecoverable mistake
    this system can make.
    """
    return business_days_between(today, front_expiry) <= guard_bd


def _next_month(d: dt.date) -> tuple[int, int]:
    """(year, month) of the calendar month after `d` — tiny helper, no clamping."""
    return (d.year + (d.month == 12), (d.month % 12) + 1)


def month_code(ym: tuple[int, int]) -> str:
    """(2026, 1) -> '202601' — the localSymbol-ish month string IBKR uses in
    Contract.lastTradeDateOrContractMonth for futures month selection."""
    return f"{ym[0]:04d}{ym[1]:02d}"
