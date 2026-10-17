#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_biascutter_calibration.py — PROVISIONAL calibration of machine 2001 (bias cutter).

STATUS: PROVISIONAL, PENDING PLANT CONFIRMATION (raised 2026-09-25).
        Reversible: restores from the .bak_biascal backup with --revert.

WHY
  Machine 2001 was the single HARD WALL in the run (886.6 h = 36.9 d of work in a 720 h
  window = 123.1% -> a 12-day late curing tail, 52,517 tyres past the plan window).
  Its CUTS/MIN rate was NEVER plant-measured: CTP_PCR_Machine_LineSpeed_Verify_CHECKED.xlsx
  leaves `PLANT line speed (measured)` and `Match` BLANK for the 2001 row, noting only
  "per-piece/cut rate not derivable from lot-level MES (observed lot cycle ~2813s)".

THE MEASUREMENT (from OUR OWN production data -- inputs/inventory.csv = DB opening_wip_pcr)
  350 real bias-cutter production events carry the machine in production_id
  (`BIAS<DDMM><YYYY><HHMMSS>`), 2026-05-17 -> 2026-06-01:
      21.9 events/day,  median inter-completion cycle 48.8 min,  17.8 run-h/day
  Projected over the 30-day window at the plan's own volume (726,044 units):
      656 events x 48.8 min = 534 h = 22.2 d   <- FITS the window
  vs modelled 886.6 h = 36.9 d.   CALIBRATION FACTOR = 886.6 / 534 = 1.66x

  Cross-checks: the independent MES note (2,813 s) agrees with the measured 2,927 s median;
  and 8 CUTS/MIN x 1.66 = 13.3 is physically coherent beside the BELT CUTTER's 15 CUTS/MIN
  (same class of machine). The routing's 7-8 CUTS/MIN is the outlier, not 13.3.

WHAT IT DOES
  Multiplies proc_time by 1.66 on every CUTS/MIN routing row for machine 2001, PRESERVING
  the per-item split (7 -> 11.6, 8 -> 13.3) instead of flattening both to one number the way
  config.yaml `routing_overrides` would. Patches inputs/routing.csv, because `--offline` runs
  skip adapt_inputs.py entirely and therefore never apply config overrides.

*** LOAD-BEARING CAVEAT ***
  This factor is calibrated to OBSERVED TOTAL THROUGHPUT while `pieces_per_cut[BIAS CUTTER]=2`
  stays in force. That =2 comes from BR-3, whose stated reason ("cuts the steel-belt roll,
  makes B1+B2") does NOT describe this machine: the plant says 2001 is Ply roll -> CHAFER, and
  the BOM confirms parents MONOCHAF80/120 / BRTTL SLT with 1.0, 2.0 and 4.0 NOS per tyre (and
  BCFT001 in METRES, on a CUTS/MIN machine). If the plant says 1 cut = 1 piece, the cut count
  DOUBLES and this 1.66x must become ~3.3x. ASK BOTH QUESTIONS TOGETHER; neither answer is
  usable alone.
"""
from __future__ import annotations
import os, shutil, sys
import pandas as pd

HERE   = os.path.dirname(os.path.abspath(__file__))
RT     = os.path.join(HERE, "inputs", "routing.csv")
BAK    = RT + ".bak_biascal"
MACHINE = "2001"
FACTOR  = 1.66


def main():
    revert = "--revert" in sys.argv[1:]
    if revert:
        if not os.path.exists(BAK):
            sys.exit(f"  nothing to revert: {BAK} not found")
        shutil.copy2(BAK, RT)
        print(f"  REVERTED {RT} from {BAK}")
        return 0

    if not os.path.exists(BAK):                 # back up the pristine file ONCE
        shutil.copy2(RT, BAK)
        print(f"  backed up -> {BAK}")
    else:
        shutil.copy2(BAK, RT)                   # idempotent: always patch from pristine
        print(f"  restored pristine from {BAK} (idempotent re-apply)")

    df = pd.read_csv(RT)
    mc  = df["machines"].astype(str).str.contains(MACHINE)
    uom = df["proc_time_UOM"].astype(str).str.strip().str.upper() == "CUTS/MIN"
    hit = mc & uom
    if not hit.any():
        print(f"  no CUTS/MIN rows for machine {MACHINE} in {RT} -- nothing patched "
              f"(no-cutter-slitter: the bias cutter is bypassed).")
        sys.exit(0)

    old = pd.to_numeric(df.loc[hit, "proc_time"], errors="coerce")
    df["proc_time"] = df["proc_time"].astype(object)
    df.loc[hit, "proc_time"] = (old * FACTOR).round(2).to_numpy(dtype=object)
    df.to_csv(RT, index=False)

    new = pd.to_numeric(df.loc[hit, "proc_time"], errors="coerce")
    print(f"  PROVISIONAL BIAS-CUTTER CALIBRATION  machine {MACHINE}  x{FACTOR}"
          f"   ({int(hit.sum())} CUTS/MIN rows)")
    for o in sorted(old.dropna().unique()):
        print(f"    {o:>5.1f} -> {round(o * FACTOR, 2):>6.2f} CUTS/MIN"
              f"   ({int((old == o).sum())} rows)")
    print(f"  wrote {RT}")
    print(f"  revert with:  python apply_biascutter_calibration.py --revert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
