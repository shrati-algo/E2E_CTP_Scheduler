#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_shift_refresh.py -- BR-15: replan every shift (2026-10-06, v33).

    python run_shift_refresh.py                      # the shift that has just started (07:00 / 15:00 / 23:00)
    python run_shift_refresh.py --at "2026-10-07 15:00" --days 10 --plan-dir latest_plan_file

Each refresh runs the full schedule (run_v15.py, every business rule, LIVE JK_CTP_PPDS) with
  * actual inventory   : the WIP snapshot taken at this shift's start (06:30 / 14:30 / 22:30 snapshots)
  * machine status     : live downtime_reason (PM / MTC / breakdowns entered before the run)
  * latest demand      : the plan files in --plan-dir (drop the newest building / curing plans there)
  * executed quantity  : covered by the snapshot (what was produced is now stock); a separate executed-
                         production feed is not in JK_CTP_PPDS yet (add it to db_pipeline when available)
Output: latest_schedule/shift_<YYYYMMDD>_<A|B|C>/ with the schedule, all rule checks and README.
Schedule it with Windows Task Scheduler three times a day, e.g. (run once, as the user):
  schtasks /Create /SC DAILY /ST 06:45 /TN CTP_shift_A /TR "python <repo>\\run_shift_refresh.py"
  (same for 14:45 and 22:45) -- not created automatically.
"""
import argparse
import os
import subprocess
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--at", default=None, help='shift start "YYYY-MM-DD HH:MM" (default: the shift running now)')
ap.add_argument("--days", type=float, default=10, help="plan length in days from the shift start (default 10)")
ap.add_argument("--plan-dir", default=os.path.join(HERE, "latest_plan_file"))
ap.add_argument("--wip-source", default="mix")
a = ap.parse_args()

now = pd.Timestamp(a.at) if a.at else pd.Timestamp.now()
starts = [now.normalize() + pd.Timedelta(hours=h) for h in (-1, 7, 15, 23)]    # -1 = 23:00 the day before
start = max(t for t in starts if t <= now) if not a.at else now
if start.hour == 23 and start > now:
    start -= pd.Timedelta(days=1)
shift = {7: "A", 15: "B", 23: "C"}.get(start.hour, "X")
name = f"shift_{start:%Y%m%d}_{shift}"
end = start + pd.Timedelta(days=a.days)
cmd = [sys.executable, "run_v15.py", "--wip-date", f"{start:%Y-%m-%d}", "--wip-time", f"{start:%H:%M}",
       "--plan-end", f"{end:%Y-%m-%d %H:%M}", "--plan-dir", a.plan_dir, "--name", name, "--wip-source", a.wip_source]
print("BR-15 shift refresh:", " ".join(cmd))
os.makedirs(os.path.join(HERE, "latest_schedule", name), exist_ok=True)
with open(os.path.join(HERE, "latest_schedule", name, "run_log.txt"), "w", encoding="utf-8") as fh:
    r = subprocess.run(cmd, cwd=HERE, stdout=fh, stderr=subprocess.STDOUT,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
print("exit", r.returncode, "->", os.path.join("latest_schedule", name))
sys.exit(r.returncode)
