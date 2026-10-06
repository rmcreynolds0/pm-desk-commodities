#!/usr/bin/env python3
"""
xsec_scheduler.py — always-on scheduler for the cross-sectional book.
=====================================================================

Fires two jobs on the schedule in config/xsec.yaml (US/Eastern):

  rebalance  monthly on the last business day at 10:30 ET, PLUS a one-off
             `first_run_date` so the book can go live immediately rather than
             waiting for month-end.
  mark       every weekday at 16:30 ET.

WHY 10:30 ET FOR THE REBALANCE
    It is the only window when all 22 markets are liquid at once: grains open
    09:30, softs run ~04:00-14:00, energy and metals nearly 24h. The first live
    attempt ran at 17:50 ET -- inside the CME settlement break -- and every order
    sat PreSubmitted instead of filling.

BEFORE THE FIRST REBALANCE the scheduler flattens any pre-existing positions, so
the forward record starts from a clean, known state rather than inheriting
leftovers from earlier testing.

Jobs run as SUBPROCESSES: a crash (bad data, gateway restart) kills that job,
not the scheduler, and the next slot still fires. All jobs are idempotent.

Run:  python scripts/xsec_scheduler.py
"""
from __future__ import annotations

import calendar
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def load_cfg() -> dict:
    """Re-read config every loop so schedule edits apply without a restart."""
    with open(ROOT / "config" / "xsec.yaml") as f:
        return yaml.safe_load(f)


def agents_needing_launch(cfg: dict) -> list[str]:
    """Agents that have never traded — the ones a self-healing launch targets.

    WHY PER AGENT, NOT PER LEDGER
        The original check asked "is the ledger empty?" across ALL agents. With
        one paper account per agent that is wrong: bringing agent_0 online
        first (while the other three accounts are still being opened) makes the
        ledger permanently non-empty, so the other three would never launch —
        they would wait for the next month-end rebalance.

    WHY `decisions` AND NOT `positions`
        An agent that rebalanced but legitimately holds nothing still has
        decision rows. Using positions would treat a genuinely flat agent as
        never-launched and re-flatten it every day.

    Returns [] when the ledger cannot be read: an unknown state must not
    trigger a flatten-and-relaunch of a live book.
    """
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from storage_stress.execution import xsec_books as B
        con = B.connect(ROOT / cfg["paths"]["books_db"])
        started = {r[0] for r in
                   con.execute("SELECT DISTINCT agent FROM decisions")}
        con.close()
        # live_agents, not signal.agents: a rung defined for the research
        # ladder but with no paper account yet must not be "pending launch"
        # forever, or every scheduler tick reports work it cannot do.
        from storage_stress.execution.xsec_engine import live_agents
        return [a for a in live_agents(cfg) if a not in started]
    except Exception as e:                           # noqa: BLE001
        print(f"[xsec-scheduler] cannot read ledger ({e}); "
              f"assuming nothing needs launching", file=sys.stderr)
        return []


def is_last_business_day(d: dt.date) -> bool:
    last = calendar.monthrange(d.year, d.month)[1]
    cur = dt.date(d.year, d.month, last)
    while cur.weekday() > 4:          # walk back over the weekend
        cur -= dt.timedelta(days=1)
    return d == cur


def run_job(args: list[str], label: str, log_dir: Path) -> int:
    """Run one job as a subprocess. Returns its exit code.

    The code matters for `flatten`: launching onto a book we failed to clear
    would mix leftover positions into the forward record, and every subsequent
    mark for that agent would be fiction.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"xsec-{label}-{dt.date.today():%Y-%m-%d}.log"
    print(f"[xsec-scheduler] firing {label} -> {log_path}")
    with open(log_path, "a") as log:
        log.write(f"\n===== {label} @ {dt.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        log.flush()
        proc = subprocess.run([sys.executable, *args], stdout=log,
                              stderr=subprocess.STDOUT, check=False, cwd=ROOT)
    if proc.returncode != 0:
        print(f"[xsec-scheduler] {label} exited {proc.returncode} "
              f"(see {log_path})", file=sys.stderr)
    return proc.returncode


def main() -> None:
    print("[xsec-scheduler] started — polling every 60s (Ctrl-C to stop)")
    fired: dict[str, str] = {}

    while True:
        try:
            cfg = load_cfg()
            sched = cfg["schedule"]
            log_dir = ROOT / cfg["paths"]["log_dir"]
            tz = ZoneInfo(sched["timezone"])
            now = dt.datetime.now(tz)
            today = now.date()
            hhmm = now.strftime("%H:%M")
            minute_key = now.strftime("%Y-%m-%d %H:%M")

            # ---- REBALANCE ------------------------------------------------
            # SELF-HEALING LAUNCH. The original design fired the first
            # rebalance on one exact date+minute. The machine was asleep at
            # that minute, the window passed, and it never retried -- so the
            # book sat empty for days while the mark job dutifully recorded
            # zeros. A one-shot trigger that cannot recover is a bug.
            #
            # Now: once we are on or past `first_run_date`, ANY weekday at
            # rebalance_time launches whichever agents have NOT yet traded.
            # Missing a day costs a day, not the whole experiment.
            #
            # PER AGENT, not per ledger. With one paper account per agent, the
            # rungs come online as their accounts are opened -- agent_0 can be
            # trading for days before agent_3's account exists. A whole-ledger
            # check would see "not empty" and never launch the rest.
            first_run = sched.get("first_run_date")
            past_first = first_run and str(today) >= str(first_run)
            weekday = today.weekday() <= 4
            pending = agents_needing_launch(cfg) if (past_first and weekday) else []

            needs_launch = bool(pending)
            is_monthly = is_last_business_day(today)

            if ((needs_launch or is_monthly)
                    and hhmm == sched["rebalance_time"]
                    and fired.get("rebalance") != minute_key):
                fired["rebalance"] = minute_key

                launch_ok = True
                if needs_launch:
                    print(f"[xsec-scheduler] launching: {', '.join(pending)}")
                    # Flatten ONLY the accounts being launched. A full sweep
                    # would close the positions of agents already trading.
                    # flatten exits non-zero if anything is left or a gateway
                    # was unreachable; launching anyway would inherit positions
                    # into a book the ledger believes is empty.
                    rc = run_job(["scripts/flatten_account.py", "--confirm",
                                  "--agents", *pending],
                                 "flatten", log_dir)
                    if rc != 0:
                        launch_ok = False
                        print("[xsec-scheduler] flatten did not fully clear "
                              f"{', '.join(pending)} — SKIPPING launch. Will "
                              "retry at the next slot.", file=sys.stderr)
                    else:
                        time.sleep(45)  # let the closes settle before sizing

                if launch_ok:
                    # A launch touches only the pending agents. The monthly
                    # rebalance touches everyone.
                    args = ["scripts/run_xsec_live.py", "--job", "rebalance"]
                    if needs_launch and not is_monthly:
                        args += ["--agents", *pending]
                    run_job(args, "rebalance", log_dir)

            # ---- DAILY MARK -----------------------------------------------
            if (now.weekday() in [_DAYS[d] for d in sched["mark_days"]]
                    and hhmm == sched["mark_time"]
                    and fired.get("mark") != minute_key):
                fired["mark"] = minute_key
                run_job(["scripts/run_xsec_live.py", "--job", "mark"],
                        "mark", log_dir)

        except Exception as e:                       # noqa: BLE001
            # The scheduler must survive anything — a bad yaml edit, a disk
            # hiccup. Log and keep looping.
            print(f"[xsec-scheduler] ERROR (continuing): {e}", file=sys.stderr)

        time.sleep(60)


if __name__ == "__main__":
    main()
