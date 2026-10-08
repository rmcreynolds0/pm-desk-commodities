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
TRAPS
-----
THE LAUNCH IS SELF-HEALING AND MUST STAY THAT WAY. The original fired on one
exact date and minute. The machine was asleep for that minute, the window
passed, nothing retried, and the book sat empty for days while the mark job
dutifully recorded zeros. Now ANY weekday at rebalance_time with an unlaunched
agent triggers it: missing a slot costs a day, not the experiment.

PENDING IS KEYED ON `decisions`, NOT `positions`. An agent that rebalanced but
legitimately holds nothing still has decision rows; keying on positions would
re-flatten and relaunch a flat agent every single day.

IT IS PER AGENT, NOT PER LEDGER. With one paper account per agent, agent_3 can
be live for days before another account exists. A whole-ledger "is it empty"
check sees "not empty" and silently never launches the rest.

A FAILED FLATTEN MUST ABORT THE LAUNCH. Launching onto a book we failed to
clear inherits positions the ledger does not know about, making every later
mark fiction.

JOBS RUN AS SUBPROCESSES and the loop catches everything: a bad yaml edit or a
gateway outage must not kill the scheduler.
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
        from storage_stress.execution.xsec_engine import live_agents
        return [a for a in live_agents(cfg) if a not in started]
    except Exception as e:                           # noqa: BLE001
        print(f"[xsec-scheduler] cannot read ledger ({e}); "
              f"assuming nothing needs launching", file=sys.stderr)
        return []


def is_last_business_day(d: dt.date) -> bool:
    last = calendar.monthrange(d.year, d.month)[1]
    cur = dt.date(d.year, d.month, last)
    while cur.weekday() > 4:
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
                    rc = run_job(["scripts/flatten_account.py", "--confirm",
                                  "--agents", *pending],
                                 "flatten", log_dir)
                    if rc != 0:
                        launch_ok = False
                        print("[xsec-scheduler] flatten did not fully clear "
                              f"{', '.join(pending)} — SKIPPING launch. Will "
                              "retry at the next slot.", file=sys.stderr)
                    else:
                        time.sleep(45)

                if launch_ok:
                    args = ["scripts/run_xsec_live.py", "--job", "rebalance"]
                    if needs_launch and not is_monthly:
                        args += ["--agents", *pending]
                    run_job(args, "rebalance", log_dir)

            if (now.weekday() in [_DAYS[d] for d in sched["mark_days"]]
                    and hhmm == sched["mark_time"]
                    and fired.get("mark") != minute_key):
                fired["mark"] = minute_key
                run_job(["scripts/run_xsec_live.py", "--job", "mark"],
                        "mark", log_dir)

        except Exception as e:                       # noqa: BLE001
            print(f"[xsec-scheduler] ERROR (continuing): {e}", file=sys.stderr)

        time.sleep(60)


if __name__ == "__main__":
    main()
