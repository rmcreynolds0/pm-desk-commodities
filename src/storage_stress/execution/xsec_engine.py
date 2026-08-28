"""
xsec_engine.py — the live decide/mark engine for the cross-sectional book.
==========================================================================

TWO JOBS, both idempotent and safe to re-run:

  run_rebalance(root)   MONTHLY. For every agent: compute today's factor scores
      from the R2 commodity panel, rank the 22 markets, take the top/bottom
      tercile, convert target dollar weights into WHOLE contracts, diff against
      what the agent currently holds, and trade the difference.

  run_mark(root)        DAILY. Price every open position, compute unrealised
      P&L, and record each agent's equity.

WHY THE SIZING IS THE HARD PART
-------------------------------
The backtest works in weights; the market works in whole contracts of wildly
different size (gold ~$443k, oats ~$16k -- a 27x span). Three things must be
right or the live book quietly stops being the strategy that was tested:

  1. priceMagnifier. Grains, softs and cattle quote in CENTS. Ignoring it
     overstates their contract value 100x.
  2. Whole-lot rounding. A market whose target is 0.4 contracts rounds to zero.
     The engine RECORDS that omission rather than hiding it.
  3. Signed positions. A short is negative contracts throughout, so the diff
     against target handles reversals (long -> short) in one step.

VOLATILITY TARGETING
--------------------
The research spec targets 10% annualised vol, scaling by
`target / trailing_realised`. Live, the trailing estimate comes from the
agent's own daily marks. Until `vol_window_days` of history exists, leverage is
1.0 -- exactly what the backtest does during its warm-up, so live and backtest
agree from the first day.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from storage_stress.agents import xsec
from storage_stress.data import commodities as CM
from storage_stress.data import connectivity as C
from storage_stress.execution import xsec_books as B
from storage_stress.execution import xsec_signals_ib as SIG
from storage_stress.execution.xsec_contracts import UniverseResolver


# --------------------------------------------------------------------------
def load_config(root: Path) -> dict:
    """Read the FROZEN spec. Environment overrides only the IBKR endpoint, so
    the strategy parameters cannot drift via environment."""
    import os
    with open(root / "config" / "xsec.yaml") as f:
        cfg = yaml.safe_load(f)
    cfg["ibkr"]["host"] = os.environ.get("IBKR_HOST", cfg["ibkr"]["host"])
    cfg["ibkr"]["port"] = int(os.environ.get("IBKR_PORT", cfg["ibkr"]["port"]))
    return cfg


def open_ledger(root: Path, cfg: dict):
    con = B.connect(root / cfg["paths"]["books_db"])
    for agent in cfg["signal"]["agents"]:
        B.ensure_agent(con, agent, cfg["capital"]["book_size"])
    return con


def per_agent_accounts(cfg: dict) -> bool:
    """True when each agent trades its OWN IBKR paper account.

    WHY THIS MODE EXISTS
    --------------------
    IBKR caps a paper account at $1,000,000. Four agents sharing one account
    therefore get, after the margin buffer:

        (1,000,000 * 0.50) / (2 * 4 * 0.12) = $520,833 per agent

    At that book size four markets -- gold, heating oil, feeder cattle and
    copper -- round to zero contracts and drop out. Those are four of the five
    markets whose removal was already TESTED AND REJECTED: on the 17-market
    universe carry stopped beating chance (Sharpe 0.24 -> 0.12, annual return
    +2.91% -> +0.05%) and drawdown worsened from -36% to -53%.

    So the shared-account layout would silently reproduce the exact universe we
    rejected, and the forward test could not distinguish "carry does not work"
    from "we could not trade the markets carry needs".

    Giving each agent its own $1,000,000 account removes the division by
    n_agents entirely:

        (1,000,000 * 0.50) / (2 * 1 * 0.12) = $2,083,333 per agent

    which holds all 22 markets at ~9% average weight error, without relaxing
    the conservative margin assumption.

    XSEC_ACCOUNT_MODE overrides the config for TEMPORARY runs -- notably a
    shakedown on a single account before the other three are open. It is an
    environment variable rather than a config edit on purpose: a temporary
    setting written into the frozen spec is exactly the kind of thing that
    survives into production unnoticed. The env var dies with the shell.
    """
    import os
    mode = os.environ.get("XSEC_ACCOUNT_MODE",
                          cfg["ibkr"].get("account_mode", "shared"))
    return str(mode).lower() == "per_agent"


def endpoint_for(cfg: dict, agent: str | None) -> tuple[str, int]:
    """Resolve which gateway an agent connects through.

    Precedence, highest first:
      1. IBKR_HOST_<AGENT> / IBKR_PORT_<AGENT>   (e.g. IBKR_HOST_AGENT_0)
      2. cfg["ibkr"]["gateways"][agent]
      3. IBKR_HOST / IBKR_PORT                    (shared endpoint)
      4. cfg["ibkr"]["host"] / ["port"]

    Per-agent environment overrides exist so docker-compose can point each
    agent at its own gateway container without editing the frozen spec.
    """
    import os
    ib_cfg = cfg["ibkr"]
    host, port = ib_cfg["host"], int(ib_cfg["port"])

    if agent:
        gw = (ib_cfg.get("gateways") or {}).get(agent) or {}
        host = gw.get("host", host)
        port = int(gw.get("port", port))
        suffix = agent.upper()
        host = os.environ.get(f"IBKR_HOST_{suffix}", host)
        port = int(os.environ.get(f"IBKR_PORT_{suffix}", port))
    return host, port


def connect_ib(cfg: dict, agent: str | None = None):
    """Connect to an IB Gateway with retries (gateways restart nightly).

    `agent` selects that agent's own gateway when running in per-agent-account
    mode; None uses the shared endpoint.
    """
    from ib_async import IB
    import time
    ib_cfg = cfg["ibkr"]
    host, port = endpoint_for(cfg, agent)
    last = None
    for attempt in range(1, ib_cfg["connect_retries"] + 1):
        try:
            ib = IB()
            ib.connect(host, port,
                       clientId=ib_cfg["client_id"],
                       timeout=ib_cfg["connect_timeout_s"])
            ib.reqMarketDataType(ib_cfg["market_data_type"])
            return ib
        except Exception as e:                     # noqa: BLE001
            last = e
            time.sleep(5 * attempt)
    raise ConnectionError(
        f"IB Gateway unreachable at {host}:{port}"
        + (f" (for {agent})" if agent else "") + f": {last}")


# --------------------------------------------------------------------------
def latest_scores(cfg: dict, staleness_days: int = 21):
    """Current factor panel: one row per market with carry / mom / basis_mom.

    Uses exactly the same code path as the backtest (commodities.build_panel +
    add_factors), so there is one implementation of the signal and live cannot
    drift from research.

    WHY NOT SIMPLY panel['date'].max()
    ----------------------------------
    Markets do not all print on the same final day of the mirror -- on the very
    last date only a handful have data, which silently collapses the
    cross-section (observed: 5 of 22 markets, too thin to form terciles).

    Instead we take EACH market's most recent observation, provided it is within
    `staleness_days` of the panel's last date. That maximises breadth while
    refusing to rank a market on genuinely stale information. Any market dropped
    for staleness is reported rather than silently omitted.
    """
    con = C._r2_duckdb()
    try:
        chain = CM.load_chain(con)
        px = CM.load_settlements(con, chain, cfg["signal"]["history_start"])
    finally:
        con.close()
    panel = CM.add_factors(CM.build_panel(px), px)

    panel_max = panel["date"].max()
    cutoff = panel_max - pd.Timedelta(days=staleness_days)
    recent = panel[panel["date"] >= cutoff]

    # One row per market: its latest observation inside the window.
    latest = (recent.sort_values("date")
              .groupby("ticker", as_index=False).last())

    dropped = sorted(set(panel["ticker"]) - set(latest["ticker"]))
    return latest, panel_max, dropped


def price_and_contract(res: UniverseResolver, ib, ticker: str):
    """(details, price, notional, multiplier, magnifier) for one market.

    Price comes from a recent daily bar rather than a streaming quote: it works
    under delayed data and needs no live subscription, which matters across 22
    markets on six exchanges.
    """
    details, expiry = res.front(ticker)
    if details is None:
        return None
    try:
        bars = ib.reqHistoricalData(details.contract, endDateTime="",
                                    durationStr="5 D", barSizeSetting="1 day",
                                    whatToShow="TRADES", useRTH=True)
    except Exception:                              # noqa: BLE001
        bars = None
    if not bars:
        return None
    price = bars[-1].close
    return {
        "details": details, "expiry": expiry, "price": price,
        "multiplier": res.multiplier(details),
        "magnifier": res.price_magnifier(details),
        "notional": res.notional(details, price),
        "local_symbol": details.contract.localSymbol,
    }


def account_equity_usd(ib) -> float | None:
    """Real equity of the connected IBKR account, in USD.

    The book size in config is an ASPIRATION; this is the constraint. Sizing
    against a book the account cannot margin produces a wall of rejected
    orders, which is exactly what happened on the first live attempt
    (config said $10M, the paper account held ~$1M).
    """
    try:
        rows = ib.accountSummary()
    except Exception:                                   # noqa: BLE001
        return None
    vals = {r.tag: r for r in rows if r.tag in
            ("NetLiquidation", "EquityWithLoanValue")}
    row = vals.get("NetLiquidation") or vals.get("EquityWithLoanValue")
    if row is None:
        return None
    try:
        amount = float(row.value)
    except (TypeError, ValueError):
        return None
    # Paper accounts are often denominated in the user's home currency.
    if (row.currency or "USD").upper() == "CAD":
        amount *= 0.73                                  # approximate CAD->USD
    return amount


def size_book(cfg: dict, equity: float | None, n_share: int,
              label: str) -> float:
    """Book size per agent, constrained by what the account can actually margin.

        capacity = (equity * 0.50) / (2 * n_share * 0.12)

    Futures initial margin runs ~5-12% of notional; we assume 12% (the
    conservative end) and keep a 50% safety buffer, because a rejected order is
    worse than a slightly small position. The factor of 2 is because a
    dollar-neutral book carries gross exposure of twice its book size.

    `n_share` is how many agents share this one account: len(agents) in shared
    mode, 1 when each agent has its own account. That divisor is the entire
    reason per-agent accounts exist -- see per_agent_accounts().

    Returns the configured book when the account can support it, the capacity
    when it cannot, and the configured book (with a warning) when equity cannot
    be read -- never silently zero.
    """
    book = float(cfg["capital"]["book_size"])
    if not equity:
        print(f"  [WARN] {label}: could not read account equity; "
              f"using configured ${book:,.0f}")
        return book
    capacity = (equity * 0.50) / (2 * n_share * 0.12)
    if capacity < book:
        print(f"  [SIZING] {label}: equity ${equity:,.0f} supports "
              f"~${capacity:,.0f}/agent; config asks ${book:,.0f}. "
              f"Using the smaller.")
        return capacity
    print(f"  [SIZING] {label}: equity ${equity:,.0f} — "
          f"book ${book:,.0f}/agent is within capacity")
    return book


def agent_leverage(con, agent: str, cfg: dict) -> float:
    """Vol-target leverage from the agent's OWN realised daily returns.

    Returns 1.0 until `vol_window_days` of marks exist — matching the
    backtest's warm-up behaviour so the two agree from day one.
    """
    window = cfg["portfolio"]["vol_window_days"]
    curve = B.equity_curve(con, agent)
    if len(curve) < window // 2:
        return 1.0
    r = curve["equity"].pct_change().dropna()
    if len(r) < window // 2 or r.std() == 0:
        return 1.0
    realised = r.tail(window).std() * np.sqrt(252)
    if not np.isfinite(realised) or realised <= 0:
        return 1.0
    lev = cfg["portfolio"]["vol_target"] / realised
    return float(min(lev, cfg["portfolio"]["max_leverage"]))


# --------------------------------------------------------------------------
def run_rebalance(root: Path, dry_run: bool = False) -> None:
    """MONTHLY. Compute targets, diff against holdings, trade the difference."""
    cfg = load_config(root)
    con = open_ledger(root, cfg)
    today = dt.date.today()

    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] xsec rebalance"
          f"{' (DRY RUN)' if dry_run else ''}")

    solo = per_agent_accounts(cfg)
    agent_names = list(cfg["signal"]["agents"])
    print(f"  account mode: {'per-agent (one paper account each)' if solo else 'shared (all agents, one account)'}")

    # THE SIGNAL CONNECTION. Universe resolution and the factor panel are
    # identical for every agent -- agents differ only in which factor COLUMNS
    # they are allowed to read -- so both are computed once, here, and reused.
    # In per-agent mode this borrows the first agent's gateway; any gateway
    # returns the same market data.
    ib_exec = None          # per-agent connection, cleaned up in finally
    ib = connect_ib(cfg, agent_names[0] if solo else None)
    try:
        res = UniverseResolver(ib, cfg["universe"])

        # --- SIZE AGAINST THE REAL ACCOUNT, NOT THE CONFIGURED ASPIRATION ---
        shared_book = None
        if not solo:
            # All agents share one account, so each may use at most its share
            # of the margin capacity.
            equity = account_equity_usd(ib)
            shared_book = size_book(cfg, equity, len(agent_names), "all agents")

        # SIGNALS COME FROM IBKR, NOT THE R2 MIRROR. The mirror is a periodic
        # snapshot: measured 2026-08-12 every market was stale (43-243 days),
        # so it cannot drive live decisions. IBKR gives both legs of the term
        # structure and a year of bars, always current — and using one source
        # for signals AND execution means a position is traded and marked
        # against the same prices that generated it.
        panel = SIG.build_live_panel(
            ib, res, cfg["universe"],
            momentum_months=cfg["signal"]["momentum_months"])
        ok = panel[panel["status"] == "ok"]
        bad = panel[panel["status"] != "ok"]
        print(f"  live factor panel: {len(ok)}/{len(panel)} markets from IBKR")
        if len(bad):
            for _, r in bad.iterrows():
                print(f"    [skip] {r['ticker']}: {r['status']}")

        # Reusable execution info per market, keyed by ticker.
        market = {r["ticker"]: {
            "details": r["_details"], "expiry": r["expiry"],
            "price": r["front_px"], "multiplier": r["multiplier"],
            "magnifier": r["magnifier"], "notional": r["notional"],
            "local_symbol": r["local_symbol"],
        } for _, r in ok.iterrows()}

        for agent in cfg["signal"]["agents"]:
            factors = cfg["signal"]["agents"][agent]
            # Exclude markets missing any factor this rung needs, rather than
            # imputing a neutral value (which would rank them on nothing).
            panel_today = SIG.usable(panel, factors)
            if len(panel_today) < cfg["portfolio"]["min_markets"]:
                print(f"  {agent:<9} only {len(panel_today)} usable markets — "
                      f"below min_markets, skipped")
                continue
            scored = xsec.score(panel_today, agent, seed=0)
            weights = xsec.target_weights(
                scored, quantile=cfg["portfolio"]["quantile"])
            if weights.empty:
                print(f"  {agent:<9} no targets (cross-section too thin)")
                continue

            # --- THIS AGENT'S ACCOUNT -------------------------------------
            # In per-agent mode each rung trades its own paper account through
            # its own gateway, so the connection and the book size are resolved
            # here rather than once for the whole run. The contract objects in
            # `market` were resolved on the signal connection; they are plain
            # data (conId, exchange, expiry) and remain valid on any session.
            if solo:
                if ib_exec is not None:
                    ib_exec.disconnect()
                    ib_exec = None
                ib_exec = connect_ib(cfg, agent)
                book = size_book(cfg, account_equity_usd(ib_exec), 1, agent)
            else:
                book = shared_book
            # trade_ib is the session orders actually go through: the agent's
            # own gateway in per-agent mode, otherwise the shared one.
            trade_ib = ib_exec if solo else ib

            lev = agent_leverage(con, agent, cfg)
            held = B.get_positions(con, agent)
            wmap = dict(zip(weights["ticker"], weights["w"]))
            ranked = scored.sort_values("score", ascending=False)
            rank_of = {t: i + 1 for i, t in enumerate(ranked["ticker"])}
            score_of = dict(zip(scored["ticker"], scored["score"]))

            # ---------------------------------------------------------------
            # PASS 1 — PLAN ONLY. No orders are sent here.
            #
            # The feasibility check MUST complete before any order goes out.
            # An earlier version ran this check after the trading loop, so an
            # agent printed "ABORTED" having already traded and written
            # positions -- the exact silent divergence the rail exists to
            # prevent. Plan first, decide, then execute.
            # ---------------------------------------------------------------
            n_zeroed = 0
            n_wanted = sum(1 for w in wmap.values() if w != 0)
            for ticker, w in wmap.items():
                info = market.get(ticker)
                if w == 0 or info is None:
                    continue
                ideal = (w * book * lev) / info["notional"]
                if int(round(ideal)) == 0:
                    n_zeroed += 1

            if n_wanted and (n_zeroed / n_wanted) > 0.30:
                print(f"  {agent:<9} ABORTED before trading — {n_zeroed}/"
                      f"{n_wanted} positions round to 0 contracts at "
                      f"${book:,.0f}/agent. Raise account equity; the live "
                      f"book would not be the strategy that was validated.")
                continue

            # ---------------------------------------------------------------
            # PASS 2 — EXECUTE. Only reached once the plan is judged tradeable.
            # ---------------------------------------------------------------
            n_orders = 0
            n_rejected = 0
            rejects: list[str] = []
            # Union of what we want and what we hold — so exits are included.
            for ticker in set(wmap) | set(held):
                info = market.get(ticker)
                w = wmap.get(ticker, 0.0)

                if info is None:
                    # Cannot price it: leave any existing position alone rather
                    # than trade blind, and record why.
                    B.record_decision(con, agent, str(today), ticker,
                                      score_of.get(ticker), rank_of.get(ticker),
                                      int(np.sign(w)), w, None,
                                      held.get(ticker, {}).get("contracts", 0),
                                      None, None, "no IBKR price — skipped")
                    continue

                target_notional = w * book * lev
                ideal = target_notional / info["notional"]
                target_contracts = int(round(ideal))
                current = held.get(ticker, {}).get("contracts", 0)

                reason = ""
                if w != 0 and target_contracts == 0:
                    # Too small to trade at this book. Recorded, not hidden --
                    # but the >30% case was already caught in pass 1.
                    reason = (f"rounds to 0 contracts "
                              f"(ideal {ideal:.2f}, ${info['notional']:,.0f}/ct)")

                B.record_decision(
                    con, agent, str(today), ticker, score_of.get(ticker),
                    rank_of.get(ticker), int(np.sign(w)), w, ideal,
                    target_contracts, info["price"], info["notional"], reason)

                delta = target_contracts - current
                if delta == 0:
                    continue

                if dry_run:
                    n_orders += 1
                    continue

                action = "BUY" if delta > 0 else "SELL"
                result = place_order(trade_ib, agent, ticker, info, action,
                                     abs(delta))
                oid = B.record_order(con, agent, result["order_ref"], ticker,
                                     action, abs(delta), info["local_symbol"],
                                     result["ib_order_id"], result["status"])

                # THE LEDGER MUST ONLY EVER REFLECT WHAT ACTUALLY FILLED.
                # Orders get rejected for real reasons -- insufficient margin,
                # IBKR's near-expiry delivery policy, per-order size caps. If we
                # wrote the intended position regardless, the books would claim
                # holdings IBKR does not have, every subsequent mark would be
                # fiction, and the forward experiment would be silently ruined.
                filled = float(result["filled"] or 0)
                if filled <= 0:
                    n_rejected += 1
                    rejects.append(f"{ticker}({result['status']})")
                    B.record_fill(con, oid, agent, ticker, None, 0,
                                  f"NOT FILLED: {result['status']}")
                    continue

                fill_px = result["avg_fill_price"] or info["price"]
                signed_filled = int(filled) * (1 if delta > 0 else -1)
                B.record_fill(con, oid, agent, ticker, fill_px,
                              abs(int(filled)),
                              "" if result["avg_fill_price"] else
                              "fallback=daily close")

                # Position becomes what we HAD plus what actually filled --
                # never the target, which may have been only partially reached.
                new_contracts = current + signed_filled
                if current != 0 and np.sign(new_contracts) != np.sign(current) \
                        and new_contracts != 0:
                    B.close_position(con, agent, ticker, str(today), fill_px,
                                     "reversal")
                if new_contracts == 0:
                    if current != 0:
                        B.close_position(con, agent, ticker, str(today),
                                         fill_px, "exit")
                else:
                    B.set_position(con, agent, ticker, new_contracts,
                                   str(today), fill_px, info["local_symbol"],
                                   str(info["expiry"]), info["multiplier"],
                                   info["magnifier"])
                n_orders += 1

            longs = sum(1 for t, w in wmap.items() if w > 0)
            shorts = sum(1 for t, w in wmap.items() if w < 0)
            msg = (f"  {agent:<9} lev={lev:.2f}  targets {longs}L/{shorts}S  "
                   f"filled={n_orders}")
            if n_rejected:
                msg += f"  REJECTED={n_rejected} [{', '.join(rejects[:6])}]"
            print(msg)
    finally:
        # Disconnect the per-agent session too. Tracking it in a variable
        # rather than a with-block keeps the cleanup correct on an exception
        # mid-agent without indenting the whole trading body.
        if ib_exec is not None:
            try:
                ib_exec.disconnect()
            except Exception:                       # noqa: BLE001
                pass
        ib.disconnect()
    con.close()


def place_order(ib, agent: str, ticker: str, info: dict, action: str,
                quantity: int) -> dict:
    """Market order on a single future, tagged with the agent's orderRef.

    The tag is the attribution mechanism: four agents share one paper account
    and will frequently hold OPPOSITE positions in the same market, which net
    to zero at the account level while both books carry real risk.
    """
    from ib_async import MarketOrder
    import time

    from storage_stress.execution.xsec_contracts import MAX_ORDER_LOTS

    order_ref = f"xsec|{agent}|{ticker}|{dt.date.today().isoformat()}"
    remaining = int(quantity)
    total_filled = 0.0
    px_sum = 0.0
    last_status = "NotSent"
    last_id = None

    # IBKR rejects single non-algo orders above ~64 lots, and the cheap markets
    # in this universe legitimately need 70-100. Split into child orders rather
    # than silently dropping the position.
    while remaining > 0:
        lots = min(remaining, MAX_ORDER_LOTS)
        order = MarketOrder(action, lots)
        order.orderRef = order_ref
        trade = ib.placeOrder(info["details"].contract, order)

        deadline = time.time() + 20
        while time.time() < deadline and not trade.isDone():
            ib.sleep(0.5)

        last_status = trade.orderStatus.status
        last_id = trade.order.orderId
        f = float(trade.orderStatus.filled or 0)
        if f > 0:
            total_filled += f
            px_sum += (trade.orderStatus.avgFillPrice or 0) * f
        remaining -= lots
        if f <= 0:
            break        # rejected: stop trying the rest of the split

    return {"ib_order_id": last_id,
            "status": last_status,
            "filled": total_filled,                  # 0 when rejected
            "avg_fill_price": (px_sum / total_filled) if total_filled else None,
            "order_ref": order_ref}


# --------------------------------------------------------------------------
def run_mark(root: Path) -> None:
    """DAILY. Price open positions, compute unrealised P&L, record equity."""
    cfg = load_config(root)
    con = open_ledger(root, cfg)
    today = dt.date.today()
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] xsec mark")

    # Marking needs PRICES ONLY — positions come from the local ledger, which
    # is the source of truth for per-agent attribution. Any gateway returns the
    # same market data, so one connection serves all agents. In per-agent mode
    # that must still be a real gateway: the shared 127.0.0.1 endpoint in the
    # frozen spec does not resolve inside the compose network.
    mark_agent = (list(cfg["signal"]["agents"])[0]
                  if per_agent_accounts(cfg) else None)
    ib = connect_ib(cfg, mark_agent)
    try:
        res = UniverseResolver(ib, cfg["universe"])
        price_cache: dict[str, dict] = {}

        for agent in cfg["signal"]["agents"]:
            held = B.get_positions(con, agent)
            unreal = 0.0
            gross = 0.0
            for ticker, pos in held.items():
                if ticker not in price_cache:
                    info = price_and_contract(res, ib, ticker)
                    if info is None:
                        continue
                    price_cache[ticker] = info
                info = price_cache.get(ticker)
                if info is None:
                    continue
                # Same convention as the ledger's close_position: the magnifier
                # division is what keeps cents-quoted markets honest.
                unreal += (pos["contracts"] * (info["price"] - pos["entry_px"])
                           * pos["multiplier"] / pos["magnifier"])
                gross += abs(pos["contracts"]) * info["notional"]

            eq = B.record_mark(con, agent, str(today), len(held), gross, unreal)
            print(f"  {agent:<9} {len(held):>2} pos  gross=${gross:>13,.0f}  "
                  f"unreal={unreal:>+12,.0f}  equity=${eq:>13,.0f}")
    finally:
        ib.disconnect()
    con.close()
