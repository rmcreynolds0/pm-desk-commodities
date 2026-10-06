"""
publish.py — build a public status snapshot of the live book and upload it.

WHY THIS EXISTS INSTEAD OF IBKR FLEX
------------------------------------
The original plan was to hand out an IBKR Flex Web Service token so someone
could follow the book without the trading login. Flex does not work for this:
its tokens are issued against a funded LIVE account and cannot read a paper
account at all.

So the snapshot is published instead. That turns out to be better for the
purpose anyway:

  * No broker dependency. Works on paper, and survives changing brokers.
  * No inbound port on the VM, and no TLS to manage.
  * WE choose what is exposed. A Flex statement contains the account number and
    every cash movement; this contains the equity curve and the positions and
    nothing that identifies the account.

WHAT IS DELIBERATELY NOT IN THE PAYLOAD
---------------------------------------
  account numbers        never written to the ledger in the first place
  IBKR order ids         internal, useless externally, and a correlation handle
  orderRef tags          an implementation detail of per-agent attribution
  credentials            obviously

`build_payload` is a PURE FUNCTION of the ledger so it can be unit-tested
against a fixture. The tests assert what must NOT appear, because a leak here
is published to a URL and cannot be recalled.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

SCHEMA = "xsec-status/1"

# Human labels for what each rung can see. Kept here rather than derived from
# the config so the published payload reads as English to someone who has
# never seen the repository.
SEES = {
    "agent_0": "random — sees nothing (null benchmark)",
    "agent_1": "carry",
    "agent_2": "carry + momentum",
    "agent_3": "carry + momentum + basis-momentum",
}


def _df(con, sql: str) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, con)
    except Exception:                                     # noqa: BLE001
        return pd.DataFrame()


def build_payload(con, cfg: dict, max_curve_points: int = 400,
                  max_decisions: int = 300) -> dict[str, Any]:
    """Assemble the public snapshot from an open ledger connection."""
    live = cfg["signal"].get("live_agents") or list(cfg["signal"]["agents"])

    agents_tbl = _df(con, "SELECT name, capital FROM agents")
    marks = _df(con, "SELECT agent, date, equity, n_positions, gross_notional,"
                     " unrealized_pnl, realized_pnl FROM marks ORDER BY date")
    positions = _df(con, "SELECT agent, ticker, contracts, entry_date,"
                         " entry_px, local_symbol, expiry FROM positions")
    trades = _df(con, "SELECT agent, ticker, contracts, entry_date, entry_px,"
                      " exit_date, exit_px, exit_reason, pnl FROM trades"
                      " ORDER BY exit_date DESC")
    decisions = _df(con, "SELECT agent, rebal_date, ticker, score, rank, side,"
                         " target_weight, target_contracts, actual_contracts,"
                         " price, reason FROM decisions"
                         " ORDER BY rebal_date DESC, agent, rank")

    capital = (dict(zip(agents_tbl["name"], agents_tbl["capital"]))
               if not agents_tbl.empty else {})

    # --- per-agent summary -------------------------------------------------
    agent_rows = []
    for a in live:
        cap = capital.get(a)
        eq, last_mark = cap, None
        if not marks.empty:
            m = marks[marks["agent"] == a].sort_values("date")
            if not m.empty:
                eq = float(m["equity"].iloc[-1])
                last_mark = str(m["date"].iloc[-1])
        n_pos = int((positions["agent"] == a).sum()) if not positions.empty else 0
        n_tr = int((trades["agent"] == a).sum()) if not trades.empty else 0
        agent_rows.append({
            "name": a,
            "sees": SEES.get(a, "—"),
            "starting_capital": cap,
            "equity": eq,
            "return_pct": (round((eq / cap - 1) * 100, 4)
                           if cap and eq is not None else None),
            "open_positions": n_pos,
            "closed_trades": n_tr,
            "last_mark": last_mark,
        })

    # --- equity curve, one row per date ------------------------------------
    curve: list[dict[str, Any]] = []
    if not marks.empty:
        wide = (marks[marks["agent"].isin(live)]
                .pivot_table(index="date", columns="agent", values="equity")
                .sort_index())
        # Cap the payload size: a multi-year daily curve would grow without
        # bound and this is fetched by anyone holding the URL.
        if len(wide) > max_curve_points:
            step = len(wide) // max_curve_points + 1
            wide = wide.iloc[::step]
        for d, row in wide.iterrows():
            point = {"date": str(d)}
            point.update({a: (None if pd.isna(v) else round(float(v), 2))
                          for a, v in row.items()})
            curve.append(point)

    # --- positions ---------------------------------------------------------
    pos_rows = []
    if not positions.empty:
        p = positions[positions["agent"].isin(live)]
        for r in p.itertuples():
            pos_rows.append({
                "agent": r.agent,
                "ticker": r.ticker,
                "side": "LONG" if r.contracts > 0 else "SHORT",
                "contracts": int(r.contracts),
                "entry_date": str(r.entry_date),
                "entry_px": float(r.entry_px),
                "contract": r.local_symbol,      # e.g. CLZ6 — public info
                "expiry": str(r.expiry),
            })

    # --- decisions, including the ones NOT traded --------------------------
    # The untraded rows are the honest part: they show what the book wanted and
    # could not take, which is the main thing an outside reader should be able
    # to check.
    dec_rows = []
    if not decisions.empty:
        d = decisions[decisions["agent"].isin(live)].head(max_decisions)
        dec_rows = json.loads(d.to_json(orient="records"))

    trade_rows = []
    if not trades.empty:
        trade_rows = json.loads(
            trades[trades["agent"].isin(live)].to_json(orient="records"))

    sched = cfg["schedule"]
    return {
        "schema": SCHEMA,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "strategy": {
            "name": "Cross-sectional commodity futures (Pivot A)",
            "description": (
                "Ranks commodity futures against each other on carry, momentum "
                "and basis-momentum; holds the top third long against the "
                "bottom third short, dollar-neutral, rebalanced monthly."),
            "universe_size": len(cfg["universe"]),
            "book_size_per_agent": cfg["capital"]["book_size"],
            "rebalance": f"{sched['rebalance_day']} {sched['rebalance_time']} "
                         f"{sched['timezone']}",
            "mark": f"{sched['mark_time']} {sched['timezone']}, weekdays",
            "live_agents": live,
        },
        "agents": agent_rows,
        "equity_curve": curve,
        "positions": pos_rows,
        "recent_decisions": dec_rows,
        "trades": trade_rows,
        "disclaimer": (
            "IBKR PAPER TRADING. Simulated execution, no capital at risk. "
            "Not investment advice and not a solicitation."),
    }


# ---------------------------------------------------------------------------
def upload_r2(body: str, key: str, content_type: str = "application/json",
              env: dict[str, str] | None = None) -> str:
    """PUT the snapshot into Cloudflare R2 (S3-compatible). Returns the key.

    boto3 rather than hand-rolled SigV4: request signing is fiddly, and a
    subtly wrong signature fails in ways that are hard to tell apart from a
    permissions problem.
    """
    import os

    import boto3

    env = env or dict(os.environ)
    missing = [k for k in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                           "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
               if not env.get(k)]
    if missing:
        raise RuntimeError(f"missing R2 settings: {', '.join(missing)}")

    s3 = boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",                  # R2 ignores region but boto3 wants one
    )
    s3.put_object(
        Bucket=env["R2_BUCKET"], Key=key,
        Body=body.encode("utf-8"), ContentType=content_type,
        # Short cache so a watcher polling the URL sees a fresh snapshot
        # soon after each mark, without hammering the bucket.
        CacheControl="public, max-age=300",
    )
    return key


def write_local(payload: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
