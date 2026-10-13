#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_sep_fixed.py -- ONE command for the September 2026 PCR+TBR live-WIP run with every
2026-09-26 fix applied (see SEP2026_ISSUES_FIXES.md).

    python run_sep_fixed.py [--out output/CTP_SEP_PCRTBR_FIXED]

  1. build_sep_inputs.py          discard/ plans + LIVE DB opening_wip / CTP_Master_Aging
                                  (+ building cycle-time fix, + TBR GT-code fix)
  2. adapt_inputs.py              reshape into the v6 schema (CTP_BOM_GATE=warn for PCR+TBR)
  3. apply_biascutter_calibration provisional x1.66 on machine 2001 (unchanged, FLAW C-2)
  4. apply_routing_gapfill.py     fill blank proc_time rows (plant plan / flagged assumption)
  5. run_scheduler.py --offline   the 9 phases + deliverables
  6. capacity_wall.py 5           capacity check on the real 5-day window
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def sh(*args):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", CTP_BOM_GATE="warn")
    print("\n" + "#" * 74 + "\n#  " + " ".join(args) + "\n" + "#" * 74, flush=True)
    r = subprocess.run([sys.executable, *args], cwd=HERE, env=env)
    if r.returncode != 0:
        raise SystemExit(f"{args[0]} failed (exit {r.returncode})")


def main():
    out = "output/CTP_SEP_PCRTBR_FIXED_V2"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    plan_dir = sys.argv[sys.argv.index("--plans") + 1] if "--plans" in sys.argv else "discard"
    sh("build_sep_inputs.py", plan_dir)
    sh("adapt_inputs.py")
    # the calibration restores its .bak_biascal "pristine" copy before patching; after a fresh
    # adapt that copy may be stale, so drop it and let the script back up THIS routing.csv.
    bak = os.path.join(HERE, "inputs", "routing.csv.bak_biascal")
    if os.path.exists(bak):
        os.remove(bak)
    sh("apply_biascutter_calibration.py")
    sh("apply_routing_gapfill.py")
    sh("apply_ctb_approximations.py")   # expert approximations for plant data not yet supplied
    # sh("apply_day_pool_times.py")     # 2026-09-30 TESTED + REVERTED: 97.4/93.4 -> 93.6/89.8 (components lose their head start)
    sh("run_scheduler.py", "--offline", "--out", out)
    sh("capacity_wall.py", "5")
    sh("audit_business_rules.py", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
