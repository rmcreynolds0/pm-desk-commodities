"""
xsec_contracts.py — resolve the 23-market universe to real IBKR contracts.
==========================================================================

This is the layer where the backtest meets reality, and where the two things
most likely to break a multi-market strategy live:

  1. RESOLUTION. Each market lives on a specific exchange (NYMEX, COMEX, CBOT,
     CME, NYBOT/ICE, MGEX) and IBKR is fussy about the pairing. A symbol that
     resolves on one exchange returns nothing on another.

  2. MULTIPLIERS. Contract sizes differ by orders of magnitude -- gold is 100 oz
     (~$200k/contract), corn is 5,000 bushels (~$20k). Equal DOLLAR weight is
     therefore nowhere near equal contracts. Multipliers are NEVER hardcoded
     here: they are read from IBKR at runtime, because a wrong multiplier
     silently mis-sizes every position in that market and the error looks like
     a strategy result rather than a bug.

The front contract is chosen with the same delivery-guard discipline the NG
engine uses: nearest expiry more than `guard_days` away, so the book never
holds into delivery.
"""
from __future__ import annotations

import datetime as dt

# Days before expiry at which a contract stops being tradeable.
#
# Raised from 5 to 25 on 2026-08-12: IBKR rejected a copper order 15 days from
# expiry with "does not comply with our order handling rules for derivatives
# subject to IBKR near-expiration and physical delivery risk policies". Their
# policy is stricter than a bare never-take-delivery rule, and for physically
# delivered commodities it starts biting weeks out. 25 days clears it while
# still trading the front contract for most of its life.
DEFAULT_GUARD_DAYS = 25

# IBKR rejects non-algorithmic orders above this size ("too large for us to
# accept for a non-algorithmic order... not exceeding 64"). Larger targets are
# split into child orders rather than dropped.
MAX_ORDER_LOTS = 60


class UniverseResolver:
    """Resolves config universe entries to live IBKR contracts.

    Takes an already-connected ib_async IB handle so the caller controls the
    connection lifecycle (and so this class stays testable without a socket).
    """

    def __init__(self, ib, universe: dict, guard_days: int = DEFAULT_GUARD_DAYS):
        self.ib = ib
        self.universe = universe
        self.guard_days = guard_days
        self._cache: dict[str, list] = {}

    # ------------------------------------------------------------------ #
    def chain(self, ticker: str) -> list:
        """All listed contracts for one market, sorted by expiry.

        Tries each configured exchange in order and returns the first that
        yields contracts -- IBKR exposes some markets under more than one
        exchange code (NYBOT vs ICEUS, MGEX vs CBOT) and which one works can
        depend on the account's permissions.
        """
        if ticker in self._cache:
            return self._cache[ticker]

        from ib_async import Future

        cfg = self.universe[ticker]
        for exchange in cfg["exchanges"]:
            try:
                details = self.ib.reqContractDetails(
                    Future(symbol=cfg["ib_symbol"], exchange=exchange,
                           currency="USD"))
            except Exception:                       # noqa: BLE001
                details = None
            if details:
                chain = sorted(details,
                               key=lambda d: d.contract.lastTradeDateOrContractMonth)
                self._cache[ticker] = chain
                return chain

        self._cache[ticker] = []
        return []

    # ------------------------------------------------------------------ #
    def front(self, ticker: str, today: dt.date | None = None):
        """The nearest contract outside the delivery guard.

        Returns (ContractDetails, expiry_date) or (None, None) if the market
        does not resolve or has nothing tradeable.
        """
        today = today or dt.date.today()
        cutoff = today + dt.timedelta(days=self.guard_days)
        for d in self.chain(ticker):
            exp = _expiry(d.contract)
            if exp is not None and exp > cutoff:
                return d, exp
        return None, None

    # ------------------------------------------------------------------ #
    def front_and_second(self, ticker: str, today: dt.date | None = None):
        """The two nearest tradeable contracts — the term structure we need.

        CARRY is the slope between these two, so both must come from the SAME
        chain on the SAME day. Returns (front_details, second_details,
        front_expiry, second_expiry); any element may be None.
        """
        today = today or dt.date.today()
        cutoff = today + dt.timedelta(days=self.guard_days)
        live = [(d, _expiry(d.contract)) for d in self.chain(ticker)]
        live = [(d, e) for d, e in live if e is not None and e > cutoff]
        if not live:
            return None, None, None, None
        if len(live) == 1:
            return live[0][0], None, live[0][1], None
        return live[0][0], live[1][0], live[0][1], live[1][1]

    def multiplier(self, details) -> float | None:
        """Contract multiplier straight from IBKR. Never hardcoded -- see the
        module docstring for why."""
        raw = details.contract.multiplier
        try:
            return float(raw) if raw else None
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    def price_magnifier(self, details) -> float:
        """How many quoted units make one currency unit.

        MANY FUTURES ARE QUOTED IN CENTS, NOT DOLLARS. Coffee, sugar, cotton,
        cattle and the grains all quote in cents; crude, gold and cocoa quote in
        dollars. IBKR reports this as `priceMagnifier` (100 for cents-quoted
        contracts, 1 otherwise).

        Ignoring it overstates those contracts' value by 100x -- e.g. coffee
        looks like $12.8M per contract instead of ~$128k -- which would make the
        sizer refuse to trade half the universe. Read from IBKR rather than
        assumed, for the same reason as the multiplier.
        """
        raw = getattr(details, "priceMagnifier", None)
        try:
            return float(raw) if raw else 1.0
        except (TypeError, ValueError):
            return 1.0

    def notional(self, details, price: float) -> float | None:
        """Dollar value of one contract = price * multiplier / priceMagnifier.

        This converts a target dollar weight into a contract count, and it is
        the number that differs by orders of magnitude across this universe
        (gold ~$350k/contract vs oats ~$18k).
        """
        mult = self.multiplier(details)
        if mult is None or price is None:
            return None
        return price * mult / self.price_magnifier(details)


def _expiry(contract) -> dt.date | None:
    """Parse IBKR's lastTradeDateOrContractMonth ('YYYYMMDD' or 'YYYYMM')."""
    raw = contract.lastTradeDateOrContractMonth
    if not raw:
        return None
    try:
        if len(raw) >= 8:
            return dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        y, m = int(raw[:4]), int(raw[4:6])
        # Month-only form: treat as end of that month (conservative).
        nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
        return nxt - dt.timedelta(days=1)
    except (ValueError, TypeError):
        return None
