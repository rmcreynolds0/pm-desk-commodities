#!/usr/bin/env python3
"""
scheduler.py — the always-on loop that fires the decide/mark jobs on time.
==========================================================================

WHY A PYTHON LOOP INSTEAD OF CRON?
----------------------------------
* identical behavior on the Windows dev box and the Linux VM/container
  (no cron on Windows; Task Scheduler configs don't travel with the repo),
* the schedule lives in config/live.yaml next to everything else,
* trivially observable: one process, one log line per minute-of-action.

The loop wakes every 60 s, converts "now" to the configured timezone
(US/Eastern — matches CME/EIA timestamps), and when a job's (day, HH:MM)
matches, runs it as a SUBPROCESS. Subprocess isolation means an engine crash
(bad data, gateway hiccup) kills the job, not the scheduler — the next
scheduled slot still fires.

Duplicate-fire protection: a job that ran in a given minute is remembered
(`last_fired`), so the 60 s polling can't double-run within the same minute;
across restarts the jobs themselves are idempotent (ledger writes are
INSERT OR REPLACE / uniqued), so even a duplicate run is harmless.

Run directly (dev):        python scripts/scheduler.py
Run in docker (prod):      the engine service's default command (compose file)
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def load_schedule() -> dict:
    """Re-read live.yaml every loop so schedule edits apply WITHOUT a restart
    — important on a headless VM you'd rather not SSH into."""
    with open(ROOT / "config" / "live.yaml") as f:
        live = yaml.safe_load(f)
    return live["schedule"], live["paths"]["log_dir"]


def run_job(job: str, log_dir: Path) -> None:
    """Run one job as a subprocess, teeing output to a dated log file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    log_path = log_dir / f"{job}-{stamp}.log"
    print(f"[scheduler] firing {job} -> {log_path}")
    with open(log_path, "a") as log:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_agents.py"),
             "--job", job],
            stdout=log, stderr=subprocess.STDOUT, check=False)


def main() -> None:
    print("[scheduler] started — polling every 60s (Ctrl-C to stop)")
    last_fired: dict[str, str] = {}

    while True:
        try:
            sched, log_dir = load_schedule()
            tz = ZoneInfo(sched["timezone"])
            now = dt.datetime.now(tz)
            day = now.weekday()
            hhmm = now.strftime("%H:%M")
            minute_key = now.strftime("%Y-%m-%d %H:%M")

            if (day == _DAYS[sched["decide_day"]]
                    and hhmm == sched["decide_time"]
                    and last_fired.get("decide") != minute_key):
                last_fired["decide"] = minute_key
                run_job("decide", ROOT / log_dir)

            if (day in [_DAYS[d] for d in sched["mark_days"]]
                    and hhmm == sched["mark_time"]
                    and last_fired.get("mark") != minute_key):
                last_fired["mark"] = minute_key
                run_job("mark", ROOT / log_dir)

        except Exception as e:                        # noqa: BLE001
            print(f"[scheduler] ERROR (loop continues): {e}", file=sys.stderr)

        time.sleep(60)


if __name__ == "__main__":
    main()
