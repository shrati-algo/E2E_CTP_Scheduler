#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Push the corrected BUILDING consumption_id from the CSV into CTP_FLOOR_SCHEDULE.xlsx,
in place, cell by cell (keeps every other sheet, value and format untouched).
Adds a GT_lot_id column alongside, matching the CSV."""
import os
import sys

import pandas as pd
from openpyxl import load_workbook

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "output", "CTP_JUNE_CORRECTED_20260803")
if not os.path.isabs(RUN):
    RUN = os.path.join(HERE, RUN)

XLSX = os.path.join(RUN, "CTP_FLOOR_SCHEDULE.xlsx")
CSV = os.path.join(RUN, "phase5_floor_schedule_updated.csv")

csv = pd.read_csv(CSV, low_memory=False)
csv = csv[csv["prod_id"].notna()]
cons = dict(zip(csv["prod_id"].astype(str), csv["consumption_id"].astype(str)))
gtl = dict(zip(csv["prod_id"].astype(str), csv["GT_lot_id"].astype(str)))

wb = load_workbook(XLSX)
n_tot = 0
for sheet in wb.sheetnames:
    ws = wb[sheet]
    hdr = {str(c.value): c.column for c in ws[1] if c.value is not None}
    if "consumption_id" not in hdr or "prod_id" not in hdr:
        continue
    c_cons, c_pid = hdr["consumption_id"], hdr["prod_id"]
    c_dept = hdr.get("department")
    # append GT_lot_id if not already there
    c_gt = hdr.get("GT_lot_id")
    if c_gt is None:
        c_gt = ws.max_column + 1
        ws.cell(1, c_gt, "GT_lot_id").font = ws.cell(1, c_cons).font.copy()
        ws.cell(1, c_gt).fill = ws.cell(1, c_cons).fill.copy()
        ws.cell(1, c_gt).alignment = ws.cell(1, c_cons).alignment.copy()
    n = 0
    for r in range(2, ws.max_row + 1):
        dept = str(ws.cell(r, c_dept).value or "").upper() if c_dept else ""
        if "BUILDING" not in dept:
            continue
        pid = str(ws.cell(r, c_pid).value or "")
        if pid in cons:
            ws.cell(r, c_cons).value = cons[pid]
            ws.cell(r, c_gt).value = gtl.get(pid, "")
            n += 1
    if n:
        print("  %-22s %s building rows updated" % (sheet, n))
        n_tot += n

wb.save(XLSX)
print("WROTE %s  (%s rows)" % (XLSX, n_tot))
