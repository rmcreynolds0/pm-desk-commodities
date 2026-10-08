"""
flex.py — IBKR Flex Web Service client.

WHAT FLEX IS, AND WHY IT IS THE RIGHT TOOL HERE
-----------------------------------------------
The Flex Web Service hands out a **read-only, token-authenticated** view of an
IBKR account: positions, trades, cash, net liquidation. It is the only way to
let someone follow this book WITHOUT giving them the trading login.

That distinction is the whole point. A paper password can place orders and
reset the account; a Flex token can only read reports. So the token is what
gets shared with a teammate, a supervisor, or an external tracker — and the
trading credentials never leave the VM.

It is also an INDEPENDENT RECORD. `data/live/xsec_books.db` is our own
bookkeeping: the engine writes what it believes filled. Flex is IBKR's account
of the same events. When they disagree, our ledger is wrong, and every mark
computed from it is fiction. `reconcile()` exists to catch exactly that, and
it has a real precedent — an earlier version of this project recorded 54
positions the broker did not actually hold, because rejected orders were being
written to the ledger as fills.

THE PROTOCOL — two calls, not one
---------------------------------
Flex generates statements asynchronously:

  1. SendRequest(token, queryId)  -> a ReferenceCode and a Url
  2. GetStatement(token, refCode) -> the statement XML, ONCE it is ready

Step 2 commonly returns error 1019 ("statement generation in progress") on the
first try. That is normal, not a failure, and is retried with backoff below.

SETUP (one-time, in IBKR Client Portal)
---------------------------------------
  1. Settings -> Account Settings -> Reporting -> **Flex Web Service**
     -> Generate a token. Note the expiry; tokens lapse and the failure mode
     is a quiet 1020 error, not an alert.
  2. Performance & Reports -> **Flex Queries** -> new *Activity* Flex Query.
     Include at minimum: Open Positions, Trades, Net Asset Value.
     Set "Date Period" to something rolling, e.g. Last 30 Days.
     Save it and note the **Query ID**.
  3. Put both in .env as FLEX_TOKEN and FLEX_QUERY_ID.

The token is a credential. It is read-only, but it still exposes your full
position and trade history, so it belongs in .env and never in the repo.
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import requests

BASE = ("https://ndcdyn.interactivebrokers.com/AccountManagement"
        "/FlexWebService")
SEND = f"{BASE}/SendRequest"
GET = f"{BASE}/GetStatement"
VERSION = "3"

RETRYABLE = {"1019"}


class FlexError(RuntimeError):
    """A Flex call failed in a way retrying will not fix."""

    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"Flex error {code}: {message}")


@dataclass
class FlexSnapshot:
    """One pull: what IBKR says the account holds right now."""
    account_id: str = ""
    when: str = ""
    nav: float | None = None
    positions: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)

    def net_by_symbol(self) -> dict[str, float]:
        """{underlying: signed position} — the shape reconciliation needs.

        KEYED ON `underlyingSymbol`, NOT `symbol`. Flex reports `symbol` as the
        specific contract ("CLZ6"); our ledger is keyed by the market ("CL").
        Using `symbol` makes every single comparison a mismatch, which turns
        the reconciliation into noise that gets ignored — the worst outcome for
        a safety check.

        Positions are SUMMED per underlying, because an account mid-roll
        legitimately holds two contract months of the same market and the
        ledger records only the net.
        """
        out: dict[str, float] = {}
        for p in self.positions:
            sym = p.get("underlyingSymbol") or p.get("symbol") or ""
            if not sym:
                continue
            try:
                out[sym] = out.get(sym, 0.0) + float(p.get("position", 0) or 0)
            except (TypeError, ValueError):
                continue
        return {k: v for k, v in out.items() if v != 0}


def _parse_or_raise(xml_text: str) -> ET.Element:
    """Flex signals failure INSIDE a 200 response, so status must be read from
    the body rather than the HTTP code."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise FlexError("parse", f"response was not XML: {e}") from e

    status = (root.findtext("Status") or "").strip()
    if status.lower() == "fail":
        code = (root.findtext("ErrorCode") or "?").strip()
        msg = (root.findtext("ErrorMessage") or "unknown").strip()
        raise FlexError(code, msg)

    if root.tag not in {"FlexStatementResponse", "FlexQueryResponse"}:
        raise FlexError("unexpected",
                        f"response root was <{root.tag}>, not a Flex response "
                        f"— IBKR may be serving an error or maintenance page")
    return root


def request_statement(token: str, query_id: str,
                      timeout: int = 30) -> tuple[str, str]:
    """Step 1. Returns (reference_code, url)."""
    r = requests.get(SEND, params={"t": token, "q": query_id, "v": VERSION},
                     timeout=timeout)
    r.raise_for_status()
    root = _parse_or_raise(r.text)
    ref = (root.findtext("ReferenceCode") or "").strip()
    url = (root.findtext("Url") or GET).strip()
    if not ref:
        raise FlexError("noref", "no ReferenceCode in response")
    return ref, url


def fetch_statement(token: str, reference_code: str, url: str = GET,
                    attempts: int = 8, delay: float = 5.0,
                    timeout: int = 60) -> str:
    """Step 2, with backoff.

    Error 1019 means IBKR is still generating the statement. That is the
    normal first response, so it is retried rather than raised; anything else
    fails immediately because retrying a bad token just wastes a minute.
    """
    last: FlexError | None = None
    for i in range(1, attempts + 1):
        r = requests.get(url, params={"t": token, "q": reference_code,
                                      "v": VERSION}, timeout=timeout)
        r.raise_for_status()
        try:
            _parse_or_raise(r.text)
            return r.text
        except FlexError as e:
            if e.code not in RETRYABLE:
                raise
            last = e
            time.sleep(delay * i)
    raise FlexError("timeout",
                    f"statement not ready after {attempts} attempts "
                    f"(last: {last})")


def parse_statement(xml_text: str) -> FlexSnapshot:
    """Pull the three things we actually use out of the statement."""
    root = ET.fromstring(xml_text)
    snap = FlexSnapshot()

    stmt = root.find(".//FlexStatement")
    if stmt is not None:
        snap.account_id = stmt.get("accountId", "")
        snap.when = f"{stmt.get('fromDate','')}..{stmt.get('toDate','')}"

    eq = root.findall(".//EquitySummaryByReportDateInBase")
    if eq:
        try:
            snap.nav = float(eq[-1].get("total") or "nan")
        except ValueError:
            snap.nav = None

    snap.positions = [dict(p.attrib) for p in root.findall(".//OpenPosition")]
    snap.trades = [dict(t.attrib) for t in root.findall(".//Trade")]
    return snap


def pull(token: str | None = None, query_id: str | None = None,
         **kw) -> FlexSnapshot:
    """One-call convenience: request, wait, fetch, parse.

    Credentials come from the environment by default so no caller has to
    handle them.
    """
    token = token or os.environ.get("FLEX_TOKEN", "")
    query_id = query_id or os.environ.get("FLEX_QUERY_ID", "")
    if not token or not query_id:
        raise FlexError("config",
                        "FLEX_TOKEN and FLEX_QUERY_ID must be set in .env — "
                        "see the setup notes at the top of this module")
    ref, url = request_statement(token, query_id)
    return parse_statement(fetch_statement(token, ref, url, **kw))


def reconcile(snap: FlexSnapshot, ledger_positions: dict[str, int],
              ) -> dict[str, Any]:
    """Compare what IBKR says we hold against what our ledger claims.

    WHY THIS IS THE POINT OF THE WHOLE MODULE
        The ledger is the source of truth for per-agent attribution, and it is
        written by the same code that places the orders. If that code is wrong
        about a fill, the ledger is wrong in exactly the way that is hardest
        to notice: every number downstream stays internally consistent and is
        simply false. Flex is the only independent account of the same events.

        Returns a structured diff rather than a boolean so the caller can show
        WHICH symbol disagrees and by how much.
    """
    broker = snap.net_by_symbol()
    ours = {k: float(v) for k, v in ledger_positions.items() if v}

    symbols = sorted(set(broker) | set(ours))
    rows = []
    for s in symbols:
        b, o = broker.get(s, 0.0), ours.get(s, 0.0)
        if b != o:
            rows.append({"symbol": s, "broker": b, "ledger": o,
                         "diff": b - o})

    return {
        "account": snap.account_id,
        "nav": snap.nav,
        "n_broker": len(broker),
        "n_ledger": len(ours),
        "agree": not rows,
        "mismatches": rows,
    }
