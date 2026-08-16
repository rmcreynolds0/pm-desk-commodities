#!/usr/bin/env python3
"""
flatten_account.py — close every open position on the paper account.

WHY THIS EXISTS
    The first live xsec attempt sized against a $10M configured book while the
    paper account held ~$1M. Some orders filled before margin was exhausted and
    the rest were rejected, leaving the account holding an arbitrary partial
    book -- mixed with a leftover leg from the retired natural-gas strategy.

    A partially-filled, half-intended book is not a starting state for an
    experiment. This flattens everything so the forward record begins clean.

SAFETY
    Paper account only -- refuses to run if the account does not look like a
    paper account (IBKR paper account ids begin with 'D'). Prints what it will
    do and requires --confirm to actually send orders.

Usage:
    python scripts/flatten_account.py              # show what would be closed
    python scripts/flatten_account.py --confirm    # actually close
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

MAX_LOTS = 60          # IBKR non-algo per-order cap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="actually send the closing orders")
    args = ap.parse_args()

    with open(ROOT / "config" / "xsec.yaml") as f:
        cfg = yaml.safe_load(f)["ibkr"]

    from ib_async import IB, MarketOrder
    ib = IB()
    ib.connect(cfg["host"], cfg["port"], clientId=51, timeout=20)
    try:
        positions = [p for p in ib.positions() if p.position != 0]
        if not positions:
            print("account is already flat.")
            return 0

        accounts = {p.account for p in positions}
        print(f"account(s): {accounts}")
        # IBKR paper accounts start with 'D'. Refuse anything else outright.
        if any(not a.startswith("D") for a in accounts):
            print("[ABORT] this does not look like a paper account.")
            return 2

        print(f"\n{len(positions)} open positions:")
        for p in positions:
            print(f"   {p.contract.localSymbol:<10} {p.contract.secType:<5} "
                  f"{p.position:>8.0f}")

        if not args.confirm:
            print("\nDry run. Re-run with --confirm to close these.")
            return 0

        print("\nclosing...")
        for p in positions:
            qty = abs(int(p.position))
            action = "SELL" if p.position > 0 else "BUY"

            # Contracts from ib.positions() have no `exchange` set, and IBKR
            # rejects orders without one ("Missing order exchange"). Qualifying
            # by conId fills in exchange and the rest of the definition.
            contract = p.contract
            try:
                qualified = ib.qualifyContracts(contract)
                if qualified:
                    contract = qualified[0]
            except Exception:                          # noqa: BLE001
                pass
            if not contract.exchange:
                print(f"   {p.contract.localSymbol:<10} SKIP — cannot resolve "
                      f"an exchange")
                continue

            remaining = qty
            while remaining > 0:
                lots = min(remaining, MAX_LOTS)
                o = MarketOrder(action, lots)
                o.orderRef = "flatten"
                t = ib.placeOrder(contract, o)
                for _ in range(40):
                    if t.isDone():
                        break
                    ib.sleep(0.5)
                print(f"   {p.contract.localSymbol:<10} {action} {lots:>4}  "
                      f"-> {t.orderStatus.status} "
                      f"(filled {t.orderStatus.filled:.0f})")
                remaining -= lots

        ib.sleep(2)
        left = [p for p in ib.positions() if p.position != 0]
        print(f"\nremaining open positions: {len(left)}")
        for p in left:
            print(f"   {p.contract.localSymbol:<10} {p.position:>8.0f}")
    finally:
        ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
