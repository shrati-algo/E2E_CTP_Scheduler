"""JK Tyre CTP end-to-end scheduler -- v133 -- single entry point.

    python run_schedule.py --plans plans/plan-12oct

The plan folder must hold the four plant plan workbooks (one of each):
    ctp_curing_schedule_*_pcr*.xlsx     ctp_curing_schedule_*_tbr*.xlsx
    ctp_building_schedule_*_pcr*.xlsx   ctp_building_schedule_*_tbr*.xlsx
each with a "Shift Schedule" sheet (Machine, SKUCode, StartTime, EndTime, Qty ...).

Everything else is optional; by default:
    plan start  = 07:00 of the day of the first curing row        (= opening-WIP time)
    plan end    = 07:00 after the day of the last curing row
    database    = offline: engine/db_pipeline/raw (snapshot copy)  -- use --db live to read the database
    run name    = v133_<plan folder name>

Results: outputs/<run name>/ (schedule workbook, daily numbers, rule reports, run log);
full working files: engine/latest_schedule/<run name>/run_output/.
See README.md and docs/.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
from datetime import datetime

import pandas as pd

REPO = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(REPO, "engine")
RAW = os.path.join(ENGINE, "db_pipeline", "raw")
OUTPUTS = os.path.join(REPO, "outputs")
PATTERNS = {("curing", "pcr"): "ctp_curing_schedule_*_pcr*.xlsx", ("curing", "tbr"): "ctp_curing_schedule_*_tbr*.xlsx",
            ("building", "pcr"): "ctp_building_schedule_*_pcr*.xlsx",
            ("building", "tbr"): "ctp_building_schedule_*_tbr*.xlsx"}


def fail(msg):
    print(f"\nERROR: {msg}\n")
    sys.exit(2)


def check_plans(folder):
    """The four plan files, exactly one of each; returns {(kind, product): path}."""
    if not os.path.isdir(folder):
        fail(f"plan folder not found: {folder}")
    found = {}
    for key, pat in PATTERNS.items():
        hits = sorted(glob.glob(os.path.join(folder, pat)))
        if len(hits) != 1:
            fail(f"need exactly one {key[0]} plan for {key[1].upper()} ({pat}) in {folder}; found {len(hits)}: "
                 f"{[os.path.basename(h) for h in hits]}")
        found[key] = hits[0]
    return found


def shift_schedule(path):
    probe = pd.read_excel(path, sheet_name="Shift Schedule", header=None, nrows=10)
    hdr = next((i for i in range(len(probe))
                if {"Machine", "SKUCode", "StartTime"} <= {str(v).strip() for v in probe.iloc[i]}), None)
    if hdr is None:
        fail(f"{os.path.basename(path)}: sheet 'Shift Schedule' has no header row with Machine / SKUCode / StartTime")
    d = pd.read_excel(path, sheet_name="Shift Schedule", header=hdr)
    d.columns = [str(c).strip() for c in d.columns]
    for c in ("SKUCode", "StartTime", "EndTime", "Qty"):
        if c not in d.columns:
            fail(f"{os.path.basename(path)}: column '{c}' missing in sheet 'Shift Schedule'")
    d["StartTime"] = pd.to_datetime(d["StartTime"], errors="coerce")
    return d


def wip_snapshots(day):
    """CTP_Opening_WIP snapshot times available in the database copy for one day."""
    p = os.path.join(RAW, "JK_CTP_PPDS__CTP_Opening_WIP.csv")
    if not os.path.exists(p):
        return []
    s = pd.read_csv(p, usecols=["updated_at"], low_memory=False).updated_at.astype(str).str[:19]
    t = pd.to_datetime(s.drop_duplicates(), errors="coerce").dropna()
    return sorted(x for x in t if x.date() == day.date())


def main():
    ap = argparse.ArgumentParser(description="JK Tyre CTP scheduler v133 -- run on a folder of plan files",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--plans", required=True, help="folder with the 4 plan workbooks (PCR / TBR, curing / building)")
    ap.add_argument("--name", help="run name (default v133_<plan folder>)")
    ap.add_argument("--start", help='plan start = opening-WIP time, "YYYY-MM-DD HH:MM" (default 07:00 of the first '
                                    'curing day)')
    ap.add_argument("--end", help='plan end "YYYY-MM-DD HH:MM" (default 07:00 after the last curing day)')
    ap.add_argument("--db", choices=["offline", "live"], default="offline",
                    help="offline (default): database snapshot copy in engine/db_pipeline/raw; live: read "
                         "JK_CTP_PPDS / jkplanning_CTP (read-only; needs engine/db_pipeline/.env) and refresh the copy")
    ap.add_argument("--wip-source", choices=["mix", "old"], default="mix",
                    help="mix (default, as v133): JK_CTP_PPDS.CTP_Opening_WIP snapshot at / before the plan start; "
                         "old: jkplanning_CTP.opening_wip of the plan-start day")
    ap.add_argument("--wip-correction", metavar='"YYYY-MM-DD HH:MM"',
                    help="add the lots of this later WIP snapshot that were produced before the plan start "
                         '(v133 used "2026-10-12 17:26")')
    ap.add_argument("--compare", nargs="*", default=[], help="earlier run names (in engine/latest_schedule) to compare "
                                                            "the daily numbers with")
    ap.add_argument("--check-only", action="store_true", help="check the inputs and print the run plan, do not run")
    a = ap.parse_args()

    plans = os.path.abspath(a.plans)
    files = check_plans(plans)
    print("\n  JK Tyre CTP scheduler v133")
    print(f"  plan folder : {plans}")
    cur = []
    for key, p in files.items():
        d = shift_schedule(p)
        q = pd.to_numeric(d["Qty"], errors="coerce").sum()
        print(f"    {key[0]:8s} {key[1].upper()}: {os.path.basename(p)}  ({len(d):,} rows, qty {q:,.0f}, "
              f"{d.StartTime.min():%d-%b %H:%M} .. {d.StartTime.max():%d-%b %H:%M})")
        if key[0] == "curing":
            cur.append(d)
    cur = pd.concat(cur)
    first, last = cur.StartTime.min(), cur.StartTime.max()
    start = pd.Timestamp(a.start) if a.start else pd.Timestamp(first.date()) + pd.Timedelta(hours=7) - \
        (pd.Timedelta(days=1) if first.hour < 7 else pd.Timedelta(0))
    end = pd.Timestamp(a.end) if a.end else pd.Timestamp(last.date()) + pd.Timedelta(hours=7) + \
        (pd.Timedelta(days=1) if last.hour >= 7 else pd.Timedelta(0))
    if end <= start:
        fail(f"plan end {end} is not after plan start {start}")
    name = a.name or ("v133_" + os.path.basename(plans.rstrip("\\/")).replace(" ", "_"))
    print(f"  window      : {start:%d-%b-%Y %H:%M} -> {end:%d-%b-%Y %H:%M} ({(end - start).total_seconds() / 86400:g} days)"
          f" | opening WIP at {start:%d-%b %H:%M}")
    print(f"  database    : {'LIVE (read-only)' if a.db == 'live' else 'offline snapshot copy ' + RAW}")
    if a.db == "live" and not os.path.exists(os.path.join(ENGINE, "db_pipeline", ".env")):
        fail("--db live needs engine/db_pipeline/.env with the database login (copy .env.example and fill it in)")
    if a.db == "offline":
        snaps = [t for t in wip_snapshots(start) if t <= start]
        if a.wip_source == "mix" and not snaps:
            print(f"  !! WARNING: the snapshot copy has no CTP_Opening_WIP snapshot on {start:%d-%b} at / before "
                  f"{start:%H:%M}. The engine will fall back to the latest earlier snapshot (see run log). Use "
                  f"--db live, or refresh engine/db_pipeline/raw, to use that day's WIP.")
        elif snaps:
            print(f"  opening WIP : CTP_Opening_WIP snapshot {snaps[-1]:%d-%b %H:%M} (latest at / before plan start)")
    print(f"  run name    : {name}  -> outputs/{name}/")
    if a.check_only:
        print("\n  inputs OK (--check-only: not run)\n")
        return

    env = dict(os.environ, PYTHONUTF8="1")
    if a.wip_correction:
        env["CTP_WIP_CORRECTION"] = a.wip_correction
    else:
        env.pop("CTP_WIP_CORRECTION", None)
    cmd = [sys.executable, "run_v15.py", "--wip-date", f"{start:%Y-%m-%d}", "--wip-time", f"{start:%H:%M}",
           "--plan-end", f"{end:%Y-%m-%d %H:%M}", "--plan-dir", plans, "--name", name, "--wip-source", a.wip_source]
    if a.db == "offline":
        cmd.append("--offline")
    if a.compare:
        cmd += ["--compare", *a.compare]
    t0 = datetime.now()
    print(f"\n  running the engine (about 10-60 min) ... {t0:%H:%M}\n")
    rc = subprocess.call(cmd, cwd=ENGINE, env=env)
    if rc != 0:
        fail(f"engine stopped (exit {rc}); see engine/latest_schedule/{name}/run_log.txt")

    # deliverables -> outputs/<name>/  (the big working csv files stay in engine/latest_schedule/<name>/run_output)
    src = os.path.join(ENGINE, "latest_schedule", name)
    dst = os.path.join(OUTPUTS, name)
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src):
        p = os.path.join(src, f)
        if os.path.isfile(p):
            shutil.copy2(p, dst)
    with open(os.path.join(dst, "RUN_INFO.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"JK Tyre CTP scheduler v133\nrun {name}  {t0:%d-%b-%Y %H:%M} -> {datetime.now():%H:%M}\n"
                 f"plans {plans}\nwindow {start} -> {end}\ndatabase {a.db}\nwip source {a.wip_source}\n"
                 f"wip correction {a.wip_correction or '-'}\nengine command: {' '.join(cmd)}\n"
                 f"full working files: {os.path.join(src, 'run_output')}\n")
    dn = os.path.join(dst, f"DAILY_NUMBERS_{name}.md")
    print(f"\n  DONE in {(datetime.now() - t0).total_seconds() / 60:.0f} min -> {dst}")
    if os.path.exists(dn):
        txt = open(dn, encoding="utf-8").read()
        print("\n" + txt[txt.find("**Cured"):].split("\n\n")[0] + "\n")


if __name__ == "__main__":
    main()
