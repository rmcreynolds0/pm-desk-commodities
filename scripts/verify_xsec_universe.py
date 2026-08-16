#!/usr/bin/env python3
"""
verify_xsec_universe.py — can we actually trade all 23 markets on this account?
==============================================================================

THIS IS THE GO/NO-GO CHECK, and it runs before anything else is built.

Three things can each independently make a market untradeable, and all three
are invisible in a backtest:

  1. RESOLUTION -- does the symbol/exchange pair return a contract at all?
  2. MARKET DATA -- is the account entitled to a price? Backtests assume prices
     are free; live accounts have per-exchange subscriptions. This is the most
     likely blocker for a 23-market universe spanning six exchanges.
  3. SIZE -- what does ONE contract cost? Gold is ~$200k/contract, corn ~$20k.
     If one contract of a market exceeds the per-position budget, that market
     cannot be held at the intended weight and the live strategy silently
     diverges from the backtested one.

Anything that fails here forces a universe decision BEFORE code is written
around it -- which is much cheaper than discovering it after going live.

Requires IB Gateway running in PAPER mode.

Usage:
    python scripts/verify_xsec_universe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from storage_stress.execution.xsec_contracts import UniverseResolver  # noqa: E402


def main() -> int:
    with open(ROOT / "config" / "xsec.yaml") as f:
        cfg = yaml.safe_load(f)
    universe, ib_cfg = cfg["universe"], cfg["ibkr"]
    book = cfg["capital"]["book_size"]
    quantile = cfg["portfolio"]["quantile"]

    # Per-position budget: a tercile book holds ~2 x (23 * 1/3) ≈ 15 positions,
    # each targeting book/positions_per_side of gross exposure.
    per_side = max(1, int(round(len(universe) * quantile)))
    budget = book / per_side
    print(f"book ${book:,.0f} · {per_side} positions per side · "
          f"~${budget:,.0f} target per position\n")

    from ib_async import IB
    ib = IB()
    try:
        ib.connect(ib_cfg["host"], ib_cfg["port"], clientId=ib_cfg["client_id"],
                   timeout=ib_cfg["connect_timeout_s"])
    except Exception as e:                              # noqa: BLE001
        print(f"[ERROR] cannot reach IB Gateway at "
              f"{ib_cfg['host']}:{ib_cfg['port']} — {e}")
        print("        Start IB Gateway in PAPER mode with the API enabled.")
        return 2
    ib.reqMarketDataType(ib_cfg["market_data_type"])    # delayed-frozen is fine

    res = UniverseResolver(ib, universe)
    rows = []
    for ticker, spec in universe.items():
        details, expiry = res.front(ticker)
        if details is None:
            rows.append({"ticker": ticker, "market": spec["name"],
                         "status": "NO CONTRACT", "exchange": "-",
                         "expiry": "-", "mult": None, "price": None,
                         "notional": None, "contracts": None})
            continue

        c = details.contract
        mult = res.multiplier(details)

        # Price: use a recent daily bar rather than a live quote — it works
        # under delayed data and does not need a streaming subscription.
        price = None
        try:
            bars = ib.reqHistoricalData(c, endDateTime="", durationStr="5 D",
                                        barSizeSetting="1 day",
                                        whatToShow="TRADES", useRTH=True)
            if bars:
                price = bars[-1].close
        except Exception:                               # noqa: BLE001
            price = None

        notional = res.notional(details, price) if price else None
        n_contracts = (budget / notional) if notional else None
        rows.append({
            "ticker": ticker, "market": spec["name"],
            "status": "ok" if price else "NO DATA",
            "exchange": c.exchange, "expiry": str(expiry),
            "mult": mult, "magnif": res.price_magnifier(details),
            "price": price, "notional": notional,
            "contracts": n_contracts,
        })

    ib.disconnect()

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    # ---- verdict ---------------------------------------------------------
    ok = df[df["status"] == "ok"]
    no_contract = df[df["status"] == "NO CONTRACT"]
    no_data = df[df["status"] == "NO DATA"]
    # A market is unusable at this book size if one contract already exceeds
    # the per-position budget (rounds to 0 contracts).
    too_big = ok[ok["contracts"] < 1.0] if len(ok) else ok

    print("\n" + "=" * 74)
    print("VERDICT")
    print("=" * 74)
    print(f"  resolved with price : {len(ok)}/{len(df)}")
    if len(no_contract):
        print(f"  NO CONTRACT         : {list(no_contract['ticker'])}")
    if len(no_data):
        print(f"  NO MARKET DATA      : {list(no_data['ticker'])}")
    if len(too_big):
        print(f"  TOO LARGE for book  : {list(too_big['ticker'])}"
              f"  (1 contract > ${budget:,.0f})")
        print(f"      -> at ${book:,.0f} these round to 0 contracts and would be"
              f" silently dropped.")

    tradeable = len(ok) - len(too_big)
    print(f"\n  TRADEABLE UNIVERSE  : {tradeable} markets")
    if tradeable < 6:
        print("  [FAIL] below the 6-market minimum — the cross-section is too "
              "thin to form terciles.")
    elif tradeable < len(df):
        print("  [PARTIAL] Some markets are unavailable. Options: raise book "
              "size, or re-run the backtest on exactly this tradeable subset "
              "so the comparison stays honest.")
    else:
        print("  [PASS] full universe is tradeable.")

    out = ROOT / "data" / "processed" / "xsec_universe_check.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nsaved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
