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
TRAPS
-----
build_payload IS PURE so it can be tested against a fixture. Its tests are
mostly NEGATIVE assertions, and that is deliberate: this payload goes to a URL,
and a field that should not be there cannot be recalled once published.

DELIBERATELY ABSENT: account identifiers, IBKR order ids, orderRef tags,
credentials. Add nothing to the payload without adding a test that the account
cannot be identified from it.

The equity curve is DOWNSAMPLED rather than unbounded -- anyone holding the URL
fetches the whole file.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

SCHEMA = "xsec-status/1"

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

    curve: list[dict[str, Any]] = []
    if not marks.empty:
        wide = (marks[marks["agent"].isin(live)]
                .pivot_table(index="date", columns="agent", values="equity")
                .sort_index())
        if len(wide) > max_curve_points:
            step = len(wide) // max_curve_points + 1
            wide = wide.iloc[::step]
        for d, row in wide.iterrows():
            point = {"date": str(d)}
            point.update({a: (None if pd.isna(v) else round(float(v), 2))
                          for a, v in row.items()})
            curve.append(point)

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
                "contract": r.local_symbol,
                "expiry": str(r.expiry),
            })

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
        region_name="auto",
    )
    s3.put_object(
        Bucket=env["R2_BUCKET"], Key=key,
        Body=body.encode("utf-8"), ContentType=content_type,
        CacheControl="public, max-age=300",
    )
    return key


def upload_gist(body: str, filename: str = "status.json",
                env: dict[str, str] | None = None) -> str:
    """Update a GitHub gist with the snapshot. Returns the public raw URL.

    WHY A GIST RATHER THAN R2 OR THE REPO
        R2 needs Cloudflare admin access to make a bucket readable, and the
        bucket here belongs to the club rather than to us. A gist needs
        nothing but a GitHub account we already have.

        A SECRET gist is the right shape for this. Its URL is unguessable but
        requires NO authentication to read, so it can be handed to someone who
        has no GitHub account — which is exactly "share a URL, not a
        credential". It is not listed on a profile and is not searchable.

        Note "secret" means unlisted, not private: anyone WITH the link can
        read it. That is the intent, and it is why build_payload excludes
        anything identifying the account.

        It also versions for free — gists keep full revision history, so the
        record cannot be silently rewritten.

    The token needs only the `gist` scope. Nothing else. A fine-grained token
    scoped to gists cannot touch the repository or any other resource, so the
    blast radius if it leaks is one JSON file of paper-trading results.
    """
    import os

    import requests

    env = env or dict(os.environ)
    token = env.get("GITHUB_TOKEN", "")
    gist_id = env.get("GIST_ID", "")
    if not token or not gist_id:
        raise RuntimeError(
            "missing GITHUB_TOKEN and/or GIST_ID — create a secret gist and a "
            "token with the 'gist' scope, then put both in .env")

    r = requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        json={"files": {filename: {"content": body}}},
        timeout=30,
    )
    if r.status_code == 404:
        raise RuntimeError(
            f"gist {gist_id} not found. Either the id is wrong or the token "
            f"lacks the 'gist' scope — GitHub returns 404 rather than 403 for "
            f"a scope it will not admit to.")
    r.raise_for_status()

    data = r.json()
    files = data.get("files", {})
    owner = (data.get("owner") or {}).get("login", "")
    return (f"https://gist.githubusercontent.com/{owner}/{gist_id}"
            f"/raw/{filename}")


def write_local(payload: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
