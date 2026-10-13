#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_schedule_workbook.py -- the plant-facing schedule workbook for one run (2026-09-30).

    python make_schedule_workbook.py <run_dir> <plan_dir> [out.xlsx]

Same layout as CTP_27-30SEP_SCHEDULE.xlsx:
  Summary            coverage by plan end, PCR / TBR / ALL
  Daily PCR TBR      day-wise build plan / built / cure plan / cured (pro-rated by time, 07:00 day)
  Curing schedule    every press block (sku, press, shift, start, end, qty, cycle, kit status, GT age)
  Building schedule  every building run, shift-split (date, shift, TBM, green tyre, start, end, qty, lot)
  Upstream daily     produce_qty per process x machine x plant day (the pivot)
  Upstream schedule  every upstream run, shift-split (date, shift, machine, item, type, process, qty, UOM, lot)
"""
import os
import subprocess
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1]
PLANS = sys.argv[2]
OUT = sys.argv[3] if len(sys.argv) > 3 else os.path.join(RUN, "CTP_SCHEDULE.xlsx")


def main():
    if not os.path.exists(os.path.join(RUN, "DAILY_REPORT.xlsx")):
        subprocess.run([sys.executable, os.path.join(HERE, "daily_report.py"), RUN, PLANS], cwd=HERE,
                       stdout=subprocess.DEVNULL)
    summ = pd.read_excel(os.path.join(RUN, "DAILY_REPORT.xlsx"), sheet_name="Coverage by plan end")
    daily = pd.read_excel(os.path.join(RUN, "DAILY_REPORT.xlsx"), sheet_name="Daily PCR TBR")

    cu = pd.read_csv(os.path.join(RUN, "phase6_curing_revision_updated.csv"),
                     parse_dates=["startTime", "endTime", "kit_ready"])
    cur = cu[["skuCode", "pressNo", "shift", "startTime", "endTime", "qty", "cycleTime",
              "kit_status", "kit_ready", "green_age_h"]].sort_values(["startTime", "pressNo"])
    cur["green_age_h"] = pd.to_numeric(cur["green_age_h"], errors="coerce").round(1)

    fs = pd.read_csv(os.path.join(RUN, "phase5_floor_schedule_updated.csv"),
                     parse_dates=["start_time", "end_time"])
    dep = fs["department"].astype(str).str.upper()
    bld = fs[dep == "BUILDING"].rename(columns={"produce_qty": "qty"})
    bld = bld[["date", "shift", "machine", "item", "start_time", "end_time", "qty", "lot_id"]] \
        .sort_values(["start_time", "machine"])
    bld["qty"] = pd.to_numeric(bld["qty"], errors="coerce").round(0)

    up = fs[~dep.isin(["BUILDING", "CURING"])].copy()
    up = up[["date", "shift", "machine", "item", "item_type", "process", "start_time", "end_time",
             "produce_qty", "UOM", "lot_id"]].sort_values(["start_time", "machine"])
    up["produce_qty"] = pd.to_numeric(up["produce_qty"], errors="coerce").round(2)
    piv = up.pivot_table(index=["process", "machine", "UOM"], columns="date", values="produce_qty",
                         aggfunc="sum", fill_value=0).round(1)
    piv["Grand Total"] = piv.sum(axis=1)
    piv = piv.reset_index()

    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        summ.to_excel(xl, sheet_name="Summary", index=False)
        daily.to_excel(xl, sheet_name="Daily PCR TBR", index=False)
        cur.to_excel(xl, sheet_name="Curing schedule", index=False)
        bld.to_excel(xl, sheet_name="Building schedule", index=False)
        piv.to_excel(xl, sheet_name="Upstream daily", index=False)
        up.to_excel(xl, sheet_name="Upstream schedule", index=False)
        for ws in xl.book.worksheets:                  # readable column widths + frozen header
            ws.freeze_panes = "A2"
            for col in ws.columns:
                w = max(len(str(c.value)) if c.value is not None else 0 for c in list(col)[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(10, w + 2), 40)
    print(f"wrote {OUT}: Summary, Daily PCR TBR, Curing {len(cur):,}, Building {len(bld):,}, "
          f"Upstream daily {len(piv):,} machines, Upstream {len(up):,}")


if __name__ == "__main__":
    main()
