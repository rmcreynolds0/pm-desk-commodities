"""
xsec_books.py — per-agent ledger for the multi-position cross-sectional book.
=============================================================================

The NG ledger held AT MOST ONE spread per agent. This strategy holds ~14
simultaneous single-contract positions per agent across 22 markets, so the
schema is keyed by (agent, ticker) rather than (agent).

Everything else carries over from the NG framework, including the reasons:

  * Four agents share ONE IBKR paper account. Separation is by `orderRef` tag
    on every order plus this local ledger, which is the SOURCE OF TRUTH for
    per-agent equity. The account's blended NetLiq mixes all four books and is
    useless for attribution -- worse here than in NG, because agents will often
    hold OPPOSITE positions in the same market that net to zero at the account
    level while both books carry real risk.

  * Every decision is recorded, traded or not, with the score and rank that
    produced it. That is the research record the project exists to produce.

Tables
  agents     one row per agent (virtual capital)
  decisions  one row per agent per market per rebalance: score, rank, target
             weight, target vs actual contracts, and WHY if they differ
  orders     one row per order sent
  fills      one row per fill
  positions  one row per (agent, ticker) currently held
  trades     completed round trips
  marks      one row per agent per day: gross notional, unrealised, equity
TRAPS
-----
THIS LEDGER IS THE SOURCE OF TRUTH for per-agent attribution. The broker's
account mixes every agent together and is useless for it -- worse here than in
the NG book, because agents often hold OPPOSITE positions in the same market
that net to zero at account level while both carry real risk.

P&L DIVIDES BY THE MAGNIFIER. Grains, softs and cattle quote in CENTS; omitting
it reports their P&L 100x too large.

EVERY DECISION IS RECORDED, TRADED OR NOT, with the score and rank that
produced it and the reason if it was not acted on. That record is the research
output the project exists to produce; a book that only logs its fills cannot
be audited.

capital is set on FIRST registration only. Changing the config later must not
silently rebase an agent's equity history.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd

_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,
    capital     REAL NOT NULL,
    created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent         TEXT NOT NULL,
    ts_utc        TEXT NOT NULL,
    rebal_date    TEXT NOT NULL,   -- the rebalance this belongs to
    ticker        TEXT NOT NULL,
    score         REAL,            -- blended factor score (NULL for agent_0's rng)
    rank          INTEGER,         -- 1 = most attractive
    side          INTEGER NOT NULL,-- +1 long / -1 short / 0 flat
    target_weight REAL,            -- fraction of book (signed)
    target_contracts REAL,         -- ideal, fractional
    actual_contracts INTEGER,      -- after whole-lot rounding
    price         REAL,
    notional      REAL,            -- $ per contract (price * mult / magnifier)
    reason        TEXT             -- e.g. 'rounds to 0 contracts'
);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent        TEXT NOT NULL,
    ts_utc       TEXT NOT NULL,
    order_ref    TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    action       TEXT NOT NULL,    -- BUY / SELL
    quantity     INTEGER NOT NULL,
    local_symbol TEXT,
    ib_order_id  INTEGER,
    status       TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    agent    TEXT NOT NULL,
    ts_utc   TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    price    REAL,
    quantity INTEGER,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    agent        TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    contracts    INTEGER NOT NULL,   -- signed: + long, - short
    entry_date   TEXT NOT NULL,
    entry_px     REAL NOT NULL,
    local_symbol TEXT,
    expiry       TEXT,
    multiplier   REAL,
    magnifier    REAL,               -- 100 for cents-quoted markets
    PRIMARY KEY (agent, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    contracts   INTEGER NOT NULL,
    entry_date  TEXT NOT NULL,
    entry_px    REAL NOT NULL,
    exit_date   TEXT NOT NULL,
    exit_px     REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    pnl         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent          TEXT NOT NULL,
    date           TEXT NOT NULL,
    n_positions    INTEGER,
    gross_notional REAL,
    unrealized_pnl REAL,
    realized_pnl   REAL,
    equity         REAL NOT NULL,
    UNIQUE (agent, date)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_DDL)
    return con


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ensure_agent(con, name: str, capital: float) -> None:
    """Idempotent. Capital is set on FIRST registration only — changing the
    config later must not silently rebase an agent's equity history."""
    con.execute("INSERT OR IGNORE INTO agents(name, capital, created_utc) "
                "VALUES (?,?,?)", (name, capital, _now()))
    con.commit()


def capital_of(con, agent: str) -> float:
    row = con.execute("SELECT capital FROM agents WHERE name=?", (agent,)).fetchone()
    return float(row[0]) if row else 0.0


def record_decision(con, agent, rebal_date, ticker, score, rank, side,
                    target_weight, target_contracts, actual_contracts,
                    price, notional, reason="") -> None:
    con.execute(
        """INSERT INTO decisions(agent, ts_utc, rebal_date, ticker, score, rank,
               side, target_weight, target_contracts, actual_contracts, price,
               notional, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (agent, _now(), rebal_date, ticker,
         None if score is None else float(score),
         None if rank is None else int(rank), int(side),
         None if target_weight is None else float(target_weight),
         None if target_contracts is None else float(target_contracts),
         int(actual_contracts), None if price is None else float(price),
         None if notional is None else float(notional), reason))
    con.commit()


def record_order(con, agent, order_ref, ticker, action, quantity,
                 local_symbol, ib_order_id, status) -> int:
    cur = con.execute(
        """INSERT INTO orders(agent, ts_utc, order_ref, ticker, action,
               quantity, local_symbol, ib_order_id, status)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (agent, _now(), order_ref, ticker, action, int(quantity),
         local_symbol, ib_order_id, status))
    con.commit()
    return cur.lastrowid


def record_fill(con, order_id, agent, ticker, price, quantity, note="") -> None:
    con.execute(
        "INSERT INTO fills(order_id, agent, ts_utc, ticker, price, quantity, note)"
        " VALUES (?,?,?,?,?,?,?)",
        (order_id, agent, _now(), ticker,
         None if price is None else float(price), int(quantity), note))
    con.commit()


def set_position(con, agent, ticker, contracts, entry_date, entry_px,
                 local_symbol, expiry, multiplier, magnifier) -> None:
    """Upsert a position. contracts is SIGNED; zero deletes the row."""
    if contracts == 0:
        con.execute("DELETE FROM positions WHERE agent=? AND ticker=?",
                    (agent, ticker))
    else:
        con.execute(
            """INSERT OR REPLACE INTO positions(agent, ticker, contracts,
                   entry_date, entry_px, local_symbol, expiry, multiplier,
                   magnifier)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (agent, ticker, int(contracts), entry_date, float(entry_px),
             local_symbol, expiry, float(multiplier), float(magnifier)))
    con.commit()


def get_positions(con, agent: str) -> dict:
    """{ticker: dict} of everything this agent currently holds."""
    rows = con.execute(
        "SELECT ticker, contracts, entry_date, entry_px, local_symbol, expiry,"
        " multiplier, magnifier FROM positions WHERE agent=?", (agent,)).fetchall()
    keys = ["contracts", "entry_date", "entry_px", "local_symbol", "expiry",
            "multiplier", "magnifier"]
    return {r[0]: dict(zip(keys, r[1:])) for r in rows}


def close_position(con, agent, ticker, exit_date, exit_px, reason) -> float:
    """Book a completed round trip and return realised $ P&L.

    P&L = contracts * (exit - entry) * multiplier / magnifier
    The magnifier division is essential: cents-quoted markets (grains, softs,
    cattle) would otherwise report P&L 100x too large.
    """
    pos = get_positions(con, agent).get(ticker)
    if pos is None:
        return 0.0
    pnl = (pos["contracts"] * (float(exit_px) - pos["entry_px"])
           * pos["multiplier"] / pos["magnifier"])
    con.execute(
        """INSERT INTO trades(agent, ticker, contracts, entry_date, entry_px,
               exit_date, exit_px, exit_reason, pnl)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (agent, ticker, pos["contracts"], pos["entry_date"], pos["entry_px"],
         exit_date, float(exit_px), reason, pnl))
    con.execute("DELETE FROM positions WHERE agent=? AND ticker=?",
                (agent, ticker))
    con.commit()
    return pnl


def realized_pnl(con, agent: str) -> float:
    row = con.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE agent=?",
                      (agent,)).fetchone()
    return float(row[0])


def record_mark(con, agent, date, n_positions, gross_notional,
                unrealized) -> float:
    """Write the daily mark and return equity = capital + realised + unrealised."""
    realized = realized_pnl(con, agent)
    equity = capital_of(con, agent) + realized + float(unrealized)
    con.execute(
        """INSERT OR REPLACE INTO marks(agent, date, n_positions,
               gross_notional, unrealized_pnl, realized_pnl, equity)
           VALUES (?,?,?,?,?,?,?)""",
        (agent, date, int(n_positions), float(gross_notional),
         float(unrealized), realized, equity))
    con.commit()
    return equity


def equity_curve(con, agent: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, n_positions, gross_notional, unrealized_pnl, "
        "realized_pnl, equity FROM marks WHERE agent=? ORDER BY date",
        con, params=(agent,), parse_dates=["date"])


def decision_log(con, agent: str | None = None) -> pd.DataFrame:
    q = ("SELECT * FROM decisions" + ("" if agent is None else " WHERE agent=?")
         + " ORDER BY rebal_date DESC, rank")
    return pd.read_sql_query(q, con, params=None if agent is None else (agent,))


def trade_log(con, agent: str | None = None) -> pd.DataFrame:
    q = ("SELECT * FROM trades" + ("" if agent is None else " WHERE agent=?")
         + " ORDER BY exit_date DESC")
    return pd.read_sql_query(q, con, params=None if agent is None else (agent,))
