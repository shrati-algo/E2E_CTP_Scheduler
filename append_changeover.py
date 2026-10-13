#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append_changeover.py -- render the plant's CHANGEOVER as explicit rows in the floor schedule.
   Run AFTER run_pipeline, BEFORE saving. Pulls the 'Changeover Plan' sheets from the btoc June
   building + curing plans and appends them as process=CHANGEOVER rows (produce_qty=0)."""
import pandas as pd, os
HERE = os.path.dirname(os.path.abspath(__file__))
O2 = os.path.join(HERE, "outputs2")
BT = os.path.join(HERE, "btocinput")
BLD = os.path.join(BT, "ctp_building_schedule_june2026_pcr 8.xlsx")
CUR = os.path.join(BT, "ctp_curing_schedule_june2026_pcr 8.xlsx")
FLOOR = os.path.join(O2, "phase5_floor_schedule_updated.csv")

fs = pd.read_csv(FLOOR, low_memory=False)
cols = list(fs.columns)
rows = []

def _mk(machine, dept, old, new, comins, st, et):
    st = pd.to_datetime(st, errors="coerce"); et = pd.to_datetime(et, errors="coerce")
    if pd.isna(st):
        return None
    if pd.isna(et):
        et = st + pd.Timedelta(minutes=float(comins or 0))
    return {"date": st.date() if not pd.isna(st) else "", "machine": str(machine),
            "shift": "", "item": f"CO {old}->{new}", "item_type": "CHANGEOVER",
            "process": "CHANGEOVER", "department": dept,
            "start_time": st.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": et.strftime("%Y-%m-%d %H:%M:%S"),
            "produce_qty": 0, "UOM": "", "lot_id": f"CO_{dept[:1]}"}

try:
    bx = pd.read_excel(BLD, sheet_name="Changeover Plan")
    for _, r in bx.iterrows():
        row = _mk(r.get("Machine"), "BUILDING", r.get("Old_SKU", ""), r.get("New_SKU", ""),
                  r.get("CO_Mins", 0), r.get("StartTime"), r.get("EndTime"))
        if row: rows.append(row)
    print(f"  [changeover] building CO rows: {len(bx)}")
except Exception as e:
    print(f"  [changeover] building CO skipped: {e}")

try:
    cx = pd.read_excel(CUR, sheet_name="Changeover Plan")
    for _, r in cx.iterrows():
        row = _mk(r.get("Press"), "CURING", r.get("Old_SKU", ""), r.get("New_SKU", ""),
                  r.get("CO_Mins", 0), r.get("StartTime"), r.get("EndTime"))
        if row: rows.append(row)
    print(f"  [changeover] curing CO rows: {len(cx)}")
except Exception as e:
    print(f"  [changeover] curing CO skipped: {e}")

if rows:
    co = pd.DataFrame(rows).reindex(columns=cols, fill_value="")
    out = pd.concat([fs, co], ignore_index=True)
    out = out.sort_values(["start_time", "machine"]).reset_index(drop=True)
    out.to_csv(FLOOR, index=False)
    print(f"  [changeover] appended {len(rows)} CHANGEOVER rows to the floor schedule "
          f"(now {len(out):,} rows).")
