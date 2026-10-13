#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_tests.py — phasewise validation of a scheduler run.

Each validator re-derives that phase's outputs FROM SCRATCH (from the phase inputs) and
compares against what the scheduler actually produced. A PASS means the phase's output is
internally consistent (quantities balance, MPQ respected, DAG well-formed, aging windows
honoured, etc.). The validators DO NOT modify the schedule — they only check it.

WHAT THEY READ
  testing/output/PHASEWISE/...   (00_INPUTS, PHASE1C_NETTING, PHASE2_LOTS, ...)
So point them at a run first:
      python run_tests.py --refresh          # copy ../output/latest/PHASEWISE  in, then test all
      python run_tests.py --refresh --phase phase2

RUN
      python run_tests.py                     # run ALL validators on whatever is in output/PHASEWISE
      python run_tests.py --phase phase4      # run ONE phase's validator
      python run_tests.py --list              # list the phase keys

Reports (one .xlsx per phase) land in  testing/reports/ .
"""
from __future__ import annotations
import argparse, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# phase key -> validator script
VALIDATORS = {
    "phase1_5":       "validate_phase1_5.py",
    "phase1b":        "validate_phase1b.py",
    "phase1c":        "validate_phase1c.py",
    "phase2":         "validate_phase2.py",
    "phase3":         "validate_phase3.py",
    "phase3_resolve": "validate_phase3_resolve.py",
    "phase4":         "validate_phase4_cpm.py",
    "phase5":         "validate_phase5.py",
    "phase6":         "validate_phase6.py",
}
ORDER = ["phase1b", "phase1_5", "phase1c", "phase2", "phase3", "phase3_resolve",
         "phase4", "phase5", "phase6"]


def refresh():
    src = os.path.join(ROOT, "output", "latest", "PHASEWISE")
    dst = os.path.join(HERE, "output", "PHASEWISE")
    if not os.path.isdir(src):
        print(f"  [refresh] no run at {src} — run ../run_scheduler.py first."); return
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"  [refresh] testing/output/PHASEWISE <- output/latest/PHASEWISE")


def run_one(key):
    script = VALIDATORS[key]
    path = os.path.join(HERE, script)
    if not os.path.exists(path):
        print(f"  [SKIP] {key}: {script} not found"); return None
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    print("\n" + "=" * 70 + f"\n  VALIDATE {key}  ({script})\n" + "=" * 70)
    r = subprocess.run([sys.executable, path], cwd=HERE, env=env)
    # move any report the validator wrote at testing/ into reports/
    reports = os.path.join(HERE, "reports"); os.makedirs(reports, exist_ok=True)
    for f in os.listdir(HERE):
        if f.endswith("_validation_report.xlsx"):
            shutil.move(os.path.join(HERE, f), os.path.join(reports, f))
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", help="one phase key (e.g. phase4); default = all")
    ap.add_argument("--refresh", action="store_true",
                    help="copy ../output/latest/PHASEWISE into testing/output first")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        print("phase keys:", ", ".join(ORDER)); return 0
    if a.refresh:
        refresh()
    keys = [a.phase] if a.phase else ORDER
    codes = {}
    for k in keys:
        if k not in VALIDATORS:
            print(f"  unknown phase {k!r}. Keys: {', '.join(ORDER)}"); continue
        codes[k] = run_one(k)
    print("\n" + "=" * 70)
    print("  VALIDATION SUMMARY (exit 0 = validator ran clean; see each report for PASS/FAIL)")
    for k in keys:
        c = codes.get(k)
        print(f"    {k:<16} {'ran' if c == 0 else ('SKIP' if c is None else f'exit {c}')}")
    print(f"  reports -> testing/reports/")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
