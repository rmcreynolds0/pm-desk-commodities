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


def ledger_is_empty(cfg: dict) -> bool:
    """True when no agent holds any position.

    Drives the self-healing launch: an empty book on a trading day means the
    launch has not happened yet (or was missed), so it should be attempted
    again rather than waiting for the next monthly window.
    """
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from storage_stress.execution import xsec_books as B
        con = B.connect(ROOT / cfg["paths"]["books_db"])
        n = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        con.close()
        return n == 0
    except Exception:                                # noqa: BLE001
        # If we cannot read the ledger, do NOT claim it is empty — that would
        # re-launch (and re-flatten) a live book on a transient error.
        return False


def is_last_business_day(d: dt.date) -> bool:
    last = calendar.monthrange(d.year, d.month)[1]
    cur = dt.date(d.year, d.month, last)
    while cur.weekday() > 4:          # walk back over the weekend
        cur -= dt.timedelta(days=1)
    return d == cur


def run_job(args: list[str], label: str, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"xsec-{label}-{dt.date.today():%Y-%m-%d}.log"
    print(f"[xsec-scheduler] firing {label} -> {log_path}")
    with open(log_path, "a") as log:
        log.write(f"\n===== {label} @ {dt.datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        log.flush()
        subprocess.run([sys.executable, *args], stdout=log,
                       stderr=subprocess.STDOUT, check=False, cwd=ROOT)


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
            # rebalance_time with an EMPTY book triggers the launch. Missing a
            # day costs a day, not the whole experiment.
            first_run = sched.get("first_run_date")
            past_first = first_run and str(today) >= str(first_run)
            weekday = today.weekday() <= 4
            book_empty = ledger_is_empty(cfg)

            needs_launch = past_first and weekday and book_empty
            is_monthly = is_last_business_day(today)

            if ((needs_launch or is_monthly)
                    and hhmm == sched["rebalance_time"]
                    and fired.get("rebalance") != minute_key):
                fired["rebalance"] = minute_key

                # Start from a clean book on a launch: leftover positions from
                # earlier testing would otherwise contaminate both the forward
                # record and the margin calculation.
                if needs_launch:
                    run_job(["scripts/flatten_account.py", "--confirm"],
                            "flatten", log_dir)
                    time.sleep(45)      # let the closes settle before sizing

                run_job(["scripts/run_xsec_live.py", "--job", "rebalance"],
                        "rebalance", log_dir)

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
