#!/usr/bin/env python3
"""
flatten_account.py — close every open position on the paper account(s).

WHY THIS EXISTS
    The first live xsec attempt sized against a $10M configured book while the
    paper account held ~$1M. Some orders filled before margin was exhausted and
    the rest were rejected, leaving the account holding an arbitrary partial
    book -- mixed with a leftover leg from the retired natural-gas strategy.

    A partially-filled, half-intended book is not a starting state for an
    experiment. This flattens everything so the forward record begins clean.

PER-AGENT ACCOUNTS
    When config/xsec.yaml sets ibkr.account_mode: per_agent, each agent trades
    its OWN paper account through its OWN gateway. Flattening only one of them
    would leave three books holding stale positions that the ledger does not
    know about -- every subsequent mark for those agents would be fiction. So
    this sweeps EVERY configured endpoint and reports each separately.

SAFETY
    Paper accounts only -- refuses to act on any account whose id does not
    begin with 'D' (IBKR paper ids do). Prints what it will do and requires
    --confirm to send orders. A gateway that cannot be reached is reported and
    skipped rather than silently treated as "already flat".

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
sys.path.insert(0, str(ROOT / "src"))

MAX_LOTS = 60          # IBKR non-algo per-order cap


def endpoints(cfg: dict, only: list[str] | None = None
              ) -> list[tuple[str, str, int]]:
    """[(label, host, port)] — the accounts to flatten.

    Uses the engine's own resolver so the two cannot disagree about which
    gateway an agent lives on.

    `only` restricts to specific agents. THIS MATTERS: in per-agent mode each
    agent has its own account, so flattening everything in order to launch one
    new rung would destroy the books of the agents already running. The
    scheduler passes exactly the agents it is about to launch.
    """
    from storage_stress.execution import xsec_engine as E

    if not E.per_agent_accounts(cfg):
        # One shared account: a partial flatten is not possible, since all
        # agents' positions live together and are distinguished only by tag.
        if only:
            print("[note] shared-account mode — --agents cannot flatten a "
                  "subset; all positions in the account will be closed.")
        host, port = E.endpoint_for(cfg, None)
        return [("shared", host, port)]

    agents = list(cfg["signal"]["agents"])
    if only:
        unknown = [a for a in only if a not in agents]
        if unknown:
            raise SystemExit(f"unknown agent(s) {unknown}; spec defines {agents}")
        agents = [a for a in agents if a in only]
    return [(a, *E.endpoint_for(cfg, a)) for a in agents]


def flatten_one(label: str, host: str, port: int, confirm: bool,
                client_id: int) -> tuple[int, int]:
    """Flatten one account. Returns (closed_attempted, still_open)."""
    from ib_async import IB, MarketOrder

    print(f"\n{'=' * 66}\n{label}  ->  {host}:{port}\n{'=' * 66}")
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=20)
    except Exception as e:                                 # noqa: BLE001
        print(f"  [SKIP] cannot reach this gateway: {e}")
        # NOT counted as flat: an unreachable gateway may hold positions.
        return (0, -1)

    try:
        positions = [p for p in ib.positions() if p.position != 0]
        if not positions:
            print("  already flat.")
            return (0, 0)

        accounts = {p.account for p in positions}
        print(f"  account(s): {accounts}")
        if any(not a.startswith("D") for a in accounts):
            print("  [ABORT] this does not look like a paper account.")
            return (0, len(positions))

        print(f"  {len(positions)} open positions:")
        for p in positions:
            print(f"     {p.contract.localSymbol:<10} {p.contract.secType:<5} "
                  f"{p.position:>8.0f}")

        if not confirm:
            print("  dry run — re-run with --confirm to close these.")
            return (0, len(positions))

        print("  closing...")
        sent = 0
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
            except Exception:                              # noqa: BLE001
                pass
            if not contract.exchange:
                print(f"     {p.contract.localSymbol:<10} SKIP — cannot "
                      f"resolve an exchange")
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
                print(f"     {p.contract.localSymbol:<10} {action} {lots:>4}  "
                      f"-> {t.orderStatus.status} "
                      f"(filled {t.orderStatus.filled:.0f})")
                remaining -= lots
                sent += 1

        ib.sleep(2)
        left = [p for p in ib.positions() if p.position != 0]
        print(f"  remaining open positions: {len(left)}")
        for p in left:
            print(f"     {p.contract.localSymbol:<10} {p.position:>8.0f}")
        return (sent, len(left))
    finally:
        ib.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="actually send the closing orders")
    ap.add_argument("--agents", nargs="+", metavar="AGENT",
                    help="flatten only these agents' accounts. Required when "
                         "bringing a new rung online while others are already "
                         "trading — a full sweep would close their positions "
                         "too. Default: every account.")
    args = ap.parse_args()

    with open(ROOT / "config" / "xsec.yaml") as f:
        cfg = yaml.safe_load(f)

    eps = endpoints(cfg, args.agents)
    print(f"flattening {len(eps)} account(s)"
          f"{'' if args.confirm else '  (DRY RUN)'}")

    total_left, unreachable = 0, 0
    for i, (label, host, port) in enumerate(eps):
        # Distinct clientId per gateway: reusing one across connections in the
        # same run makes IBKR drop the earlier session.
        _, left = flatten_one(label, host, port, args.confirm, 51 + i)
        if left < 0:
            unreachable += 1
        else:
            total_left += left

    print(f"\n{'=' * 66}")
    print(f"summary: {total_left} position(s) still open across "
          f"{len(eps) - unreachable} reachable account(s)"
          + (f"; {unreachable} gateway(s) UNREACHABLE" if unreachable else ""))
    # Non-zero exit if anything is still open or a gateway could not be
    # checked -- the scheduler flattens before launch and must not proceed on
    # a book it failed to clear.
    return 0 if (total_left == 0 and unreachable == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
