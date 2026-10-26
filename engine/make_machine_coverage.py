#!/usr/bin/env python3
"""make_machine_coverage.py - every machine in the plant register, and its work.

    python make_machine_coverage.py --run e2e_v15

WHY THIS FILE EXISTS
  `make_e2e_workbook_v13.py` and `make_pivot_workbook.py` both READ
  MACHINE_COVERAGE.csv and nothing in the repository WROTE it - the v14 copy
  was produced outside the pipeline, so a fresh run could not rebuild the
  workbook. This closes that loop.

WHAT IT ANSWERS
  The plant asked for work on every CTP machine. That claim is only worth
  something if it is measured against the REGISTER - the plant's own list of
  machines - rather than against the machines the schedule happens to mention.
  A machine can only be missing from the schedule if it is in the register.

  A machine the register marks BOTH is shared plant: 2501/2502 are routed by
  TBR alone, so they are judged on the JOINT schedule, not on PCR's half.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import ctpio as io

# the register's own spellings for the six Banburys -> their wc ids
REG_ALIAS = {"F270 M": "101", "F270 F": "102", "F440 M": "103",
             "K310 F": "104", "K430-1 M": "105", "K430-2 M": "106"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="e2e_v15")
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run

    reg = pd.read_csv(io.STAGE / "PLANT_MACHINE_REGISTER.csv", encoding="utf-8-sig")
    reg["wc"] = reg["machine"].astype(str).str.strip().map(
        lambda m: REG_ALIAS.get(m, m))

    fr = []
    for ln in ("pcr", "tbr"):
        p = D / f"phase5_floor_schedule_{ln}.csv"
        if p.exists():
            d = pd.read_csv(p, encoding="utf-8-sig",
                            parse_dates=["start_time", "end_time"])
            d["line"] = ln.upper()
            fr.append(d)
    fl = pd.concat(fr, ignore_index=True)
    fl["machine"] = fl["machine"].astype(str).str.strip()
    fl["busy_h"] = (fl["end_time"] - fl["start_time"]).dt.total_seconds() / 3600.0

    span_h = ((fl["end_time"].max() - fl["start_time"].min()).total_seconds()
              / 3600.0) or 1.0
    busy = fl.groupby("machine")["busy_h"].sum()
    lots = fl.groupby("machine").size()
    where = fl.groupby("machine")["line"].apply(
        lambda s: "+".join(sorted(set(s))))

    rows = []
    for r in reg.itertuples(index=False):
        wc = str(r.wc)
        b = float(busy.get(wc, 0.0))
        rows.append({
            "machine": r.machine, "wc": wc, "name": r.name, "line": r.line,
            "lots": int(lots.get(wc, 0)), "busy_h": round(b, 1),
            "util_pct": round(100 * b / span_h, 1),
            "planned": b > 0,
            "runs_on": where.get(wc, ""),
            "issue": ("NOT PLANNED - no work assigned" if b <= 0 else
                      ("under 20% utilised" if 100 * b / span_h < 20 else ""))})
    cov = pd.DataFrame(rows).sort_values(["planned", "util_pct"],
                                         ascending=[True, True])
    cov.to_csv(D / "MACHINE_COVERAGE.csv", index=False, encoding="utf-8-sig")

    # machines the SCHEDULE uses that the register does not list - the other
    # half of the same question, and a real finding rather than a rounding.
    extra = sorted(set(fl["machine"]) - set(reg["wc"].astype(str)))

    n_idle = int((~cov["planned"]).sum())
    print("\n" + "=" * 78)
    print(f"  MACHINE COVERAGE against the plant register   [{a.run}]")
    print("=" * 78)
    print(f"  Register        : {len(cov)} machines")
    print(f"  Carrying work   : {len(cov)-n_idle}/{len(cov)}"
          f"   ({100*(len(cov)-n_idle)/max(len(cov),1):.0f}%)")
    print(f"  Horizon span    : {span_h:,.1f} h")
    if n_idle:
        print(f"  IDLE:")
        for r in cov[~cov["planned"]].itertuples(index=False):
            print(f"    {str(r.machine):12s} {str(r.name):26s} {r.line}")
    else:
        print(f"  IDLE            : none - every register machine carries work")
    if extra:
        print(f"  Used but NOT in the register ({len(extra)}) - these are "
              f"presses and builders, which the register does not enumerate:")
        print(f"    {', '.join(extra[:14])}{' ...' if len(extra) > 14 else ''}")
    print(f"\n[OK] {D / 'MACHINE_COVERAGE.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
