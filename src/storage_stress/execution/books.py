"""
books.py — the per-agent ledger (SQLite). SOURCE OF TRUTH for every book.
=========================================================================

WHY A LOCAL LEDGER AND NOT THE IBKR ACCOUNT?
--------------------------------------------
All agents share ONE paper account, so the account's NetLiq/positions mix
every agent together and are useless for per-agent attribution. Instead:

  * every order we send carries `orderRef = <agent name>` (broker.py), so
    fills are attributable at the account level for reconciliation, and
  * this SQLite file records, per agent: every signal evaluation (traded or
    not), every order, every fill, the open position, daily marks, and the
    resulting equity curve off a VIRTUAL capital base (config live.yaml).

The dashboard reads this file directly. Nothing downstream ever needs to
re-derive state from IBKR — IBKR is only consulted to EXECUTE and to PRICE.

SCHEMA (one row types summary)
------------------------------
  agents    : registry row per agent (capital, created timestamp)
  decisions : one row per agent per decide-job run — the full "why" of the
              week (z, gate, season, vol, target size, whether we acted).
              This is the trade-by-trade research record the project is for.
  orders    : one row per order sent (entry or exit), with IBKR ids/refs
  fills     : one row per fill confirmation (price, qty, commission est.)
  positions : AT MOST one open row per agent (the strategy holds a single
              spread); closed positions move to `trades`
  trades    : completed round trips — entry+exit pair, exit reason, P&L
  marks     : one row per agent per mark-job run — spread px, unrealized
              P&L, equity after marking. The equity curve = marks over time.

Concurrency: WAL mode + short transactions. The scheduler never runs two
jobs at once, and the dashboard only reads, so contention is minimal.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Schema DDL. CREATE IF NOT EXISTS everywhere -> init is idempotent; calling
# init_db() on every job start is safe and removes "did you migrate?" drift.
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,   -- e.g. 'agent_dsi'
    capital     REAL NOT NULL,      -- virtual book size ($) equity starts from
    created_utc TEXT NOT NULL       -- first time the agent was registered
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent         TEXT NOT NULL REFERENCES agents(name),
    ts_utc        TEXT NOT NULL,    -- when the decide job ran
    decision_date TEXT NOT NULL,    -- the weekly bar date the signal is for
    side          INTEGER NOT NULL, -- +1 long spread / -1 short / 0 flat
    z             REAL,             -- signal z-score (NULL for agent_zero)
    gate          INTEGER,          -- vol regime gate state (NULL if agent has none)
    season        TEXT NOT NULL,    -- 'injection' | 'withdrawal'
    spread_px     REAL,             -- spread level at decision time
    spread_vol    REAL,             -- weekly spread vol used for sizing
    contracts_tgt REAL,             -- inverse-vol size (0 if no trade)
    acted         INTEGER NOT NULL, -- 1 if an order was actually sent
    reason        TEXT              -- human-readable explanation for the log
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL REFERENCES agents(name),
    ts_utc      TEXT NOT NULL,
    order_ref   TEXT NOT NULL,      -- the orderRef tag sent to IBKR
    action      TEXT NOT NULL,      -- 'ENTRY' | 'EXIT'
    side        INTEGER NOT NULL,   -- +1 / -1 (spread direction being opened/closed)
    quantity    INTEGER NOT NULL,   -- combo lots (>=1)
    front_leg   TEXT,               -- localSymbol of the front contract
    deferred_leg TEXT,              -- localSymbol of the deferred contract
    ib_order_id INTEGER,            -- IBKR's id, for reconciliation
    status      TEXT                -- last known IBKR order status
);

CREATE TABLE IF NOT EXISTS fills (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id  INTEGER NOT NULL REFERENCES orders(id),
    agent     TEXT NOT NULL REFERENCES agents(name),
    ts_utc    TEXT NOT NULL,
    price     REAL,                 -- avg combo fill price (spread points)
    quantity  INTEGER,
    note      TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    agent       TEXT PRIMARY KEY REFERENCES agents(name),  -- <=1 open pos/agent
    side        INTEGER NOT NULL,
    contracts   INTEGER NOT NULL,
    entry_date  TEXT NOT NULL,
    entry_px    REAL NOT NULL,      -- spread px at entry (for stop + P&L)
    entry_vol   REAL NOT NULL,      -- entry-day vol (the 2x stop reference)
    front_leg   TEXT NOT NULL,
    deferred_leg TEXT NOT NULL,
    front_expiry TEXT NOT NULL      -- ISO date; the delivery guard checks this
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL REFERENCES agents(name),
    side        INTEGER NOT NULL,
    contracts   INTEGER NOT NULL,
    entry_date  TEXT NOT NULL,
    entry_px    REAL NOT NULL,
    exit_date   TEXT NOT NULL,
    exit_px     REAL NOT NULL,
    exit_reason TEXT NOT NULL,      -- 'time' | 'stop' | 'delivery_guard' | 'manual'
    pnl         REAL NOT NULL,      -- realized $ P&L incl. slippage estimate
    held_days   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent          TEXT NOT NULL REFERENCES agents(name),
    date           TEXT NOT NULL,   -- mark date (settle date)
    spread_px      REAL,            -- latest spread level used for the mark
    unrealized_pnl REAL,            -- open-position P&L at this mark
    equity         REAL NOT NULL,   -- virtual capital + realized + unrealized
    UNIQUE (agent, date)            -- one mark per agent per day (idempotent job)
);
"""

# $ value of a 1.00 move in the spread per contract (NG = 10,000 MMBtu).
# Kept here (not imported from agents.py) so the execution layer stands alone.
POINT_VALUE = 10_000.0


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and lazily create) the ledger. WAL lets the dashboard read while
    a job writes; foreign_keys enforces the agent registry relationship."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_DDL)
    return con


def _now() -> str:
    """UTC ISO timestamp — every event row carries one for auditability."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def ensure_agent(con: sqlite3.Connection, name: str, capital: float) -> None:
    """Idempotent registration. Capital is only set on FIRST registration —
    changing live.yaml later must not silently rebase an agent's equity."""
    con.execute(
        "INSERT OR IGNORE INTO agents(name, capital, created_utc) VALUES (?,?,?)",
        (name, capital, _now()),
    )
    con.commit()


# ---------------------------------------------------------------------------
# Event writers — each is a single small transaction
# ---------------------------------------------------------------------------
def record_decision(con, agent: str, decision_date: str, side: int, z, gate,
                    season: str, spread_px, spread_vol, contracts_tgt: float,
                    acted: bool, reason: str) -> int:
    cur = con.execute(
        """INSERT INTO decisions(agent, ts_utc, decision_date, side, z, gate,
                                 season, spread_px, spread_vol, contracts_tgt,
                                 acted, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (agent, _now(), decision_date, side,
         None if z is None else float(z),
         None if gate is None else int(gate),
         season,
         None if spread_px is None else float(spread_px),
         None if spread_vol is None else float(spread_vol),
         float(contracts_tgt), int(acted), reason),
    )
    con.commit()
    return cur.lastrowid


def record_order(con, agent: str, order_ref: str, action: str, side: int,
                 quantity: int, front_leg: str, deferred_leg: str,
                 ib_order_id, status: str) -> int:
    cur = con.execute(
        """INSERT INTO orders(agent, ts_utc, order_ref, action, side, quantity,
                              front_leg, deferred_leg, ib_order_id, status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (agent, _now(), order_ref, action, side, quantity,
         front_leg, deferred_leg, ib_order_id, status),
    )
    con.commit()
    return cur.lastrowid


def record_fill(con, order_id: int, agent: str, price, quantity, note: str = "") -> None:
    con.execute(
        "INSERT INTO fills(order_id, agent, ts_utc, price, quantity, note) VALUES (?,?,?,?,?,?)",
        (order_id, agent, _now(),
         None if price is None else float(price), quantity, note),
    )
    con.commit()


# ---------------------------------------------------------------------------
# Position lifecycle
# ---------------------------------------------------------------------------
def open_position(con, agent: str, side: int, contracts: int, entry_date: str,
                  entry_px: float, entry_vol: float, front_leg: str,
                  deferred_leg: str, front_expiry: str) -> None:
    """INSERT OR REPLACE: the strategy holds at most one spread per agent, so
    an existing row would be a logic error upstream — REPLACE keeps the DB
    consistent anyway (last writer wins) rather than crashing the job."""
    con.execute(
        """INSERT OR REPLACE INTO positions(agent, side, contracts, entry_date,
               entry_px, entry_vol, front_leg, deferred_leg, front_expiry)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (agent, side, contracts, entry_date, float(entry_px), float(entry_vol),
         front_leg, deferred_leg, front_expiry),
    )
    con.commit()


def get_position(con, agent: str):
    """The agent's open position as a dict, or None when flat."""
    row = con.execute(
        "SELECT side, contracts, entry_date, entry_px, entry_vol, front_leg, "
        "deferred_leg, front_expiry FROM positions WHERE agent=?", (agent,)
    ).fetchone()
    if row is None:
        return None
    keys = ["side", "contracts", "entry_date", "entry_px", "entry_vol",
            "front_leg", "deferred_leg", "front_expiry"]
    return dict(zip(keys, row))


def close_position(con, agent: str, exit_date: str, exit_px: float,
                   exit_reason: str, slippage_cost: float = 0.0) -> float:
    """Move the open position to `trades` and return the realized $ P&L.

    P&L convention matches simulate() in agents.py exactly:
        pnl = side * (exit_px - entry_px) * contracts * POINT_VALUE - costs
    so live numbers stay directly comparable to backtest numbers.
    """
    pos = get_position(con, agent)
    if pos is None:
        return 0.0
    pnl = (pos["side"] * (float(exit_px) - pos["entry_px"])
           * pos["contracts"] * POINT_VALUE) - slippage_cost
    held = (dt.date.fromisoformat(exit_date)
            - dt.date.fromisoformat(pos["entry_date"])).days
    con.execute(
        """INSERT INTO trades(agent, side, contracts, entry_date, entry_px,
                              exit_date, exit_px, exit_reason, pnl, held_days)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (agent, pos["side"], pos["contracts"], pos["entry_date"], pos["entry_px"],
         exit_date, float(exit_px), exit_reason, pnl, held),
    )
    con.execute("DELETE FROM positions WHERE agent=?", (agent,))
    con.commit()
    return pnl


# ---------------------------------------------------------------------------
# Marks & equity
# ---------------------------------------------------------------------------
def realized_pnl(con, agent: str) -> float:
    """Sum of all completed-trade P&L — the 'banked' component of equity."""
    row = con.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE agent=?",
                      (agent,)).fetchone()
    return float(row[0])


def record_mark(con, agent: str, date: str, spread_px, unrealized: float) -> float:
    """Write today's mark and return the resulting equity.

    equity = virtual capital + all realized P&L + today's unrealized P&L.
    UNIQUE(agent,date) + INSERT OR REPLACE makes re-running the mark job for
    the same day harmless (it just refreshes the row) — jobs must be safe to
    retry because IB Gateway restarts can kill a run halfway.
    """
    cap = con.execute("SELECT capital FROM agents WHERE name=?", (agent,)).fetchone()[0]
    equity = float(cap) + realized_pnl(con, agent) + float(unrealized)
    con.execute(
        "INSERT OR REPLACE INTO marks(agent, date, spread_px, unrealized_pnl, equity) "
        "VALUES (?,?,?,?,?)",
        (agent, date, None if spread_px is None else float(spread_px),
         float(unrealized), equity),
    )
    con.commit()
    return equity


# ---------------------------------------------------------------------------
# Read side — what the dashboard consumes (returns pandas for convenience)
# ---------------------------------------------------------------------------
def equity_curve(con, agent: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, spread_px, unrealized_pnl, equity FROM marks "
        "WHERE agent=? ORDER BY date", con, params=(agent,), parse_dates=["date"])


def trade_log(con, agent: str | None = None) -> pd.DataFrame:
    q = "SELECT * FROM trades" + ("" if agent is None else " WHERE agent=?") + " ORDER BY exit_date"
    return pd.read_sql_query(q, con, params=None if agent is None else (agent,))


def decision_log(con, agent: str | None = None) -> pd.DataFrame:
    q = ("SELECT * FROM decisions" + ("" if agent is None else " WHERE agent=?")
         + " ORDER BY decision_date")
    return pd.read_sql_query(q, con, params=None if agent is None else (agent,))
