#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_linespeed_template.py -- machine x material line-speed comparison template (PCR + TBR).

For every (product, machine, operation, material) it lists OUR proc_time/UOM/efficiency (from the
routing the scheduler uses) and leaves BLANK columns for the plant's ACTUAL values, to be filled
from one month of production data. Comparing the two shows where our line speed / proc time is
wrong -- the root of the over-capacity (e.g. calender 1901) issues."""
import os, re, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "db_pipeline", "raw")
OUT = os.path.join(HERE, "ctp_inputfiles", "CTP_Machine_LineSpeed_Template.xlsx")

# ── machine id -> (name, PCR/TBR) from CTP Machines.xlsx 'Stock Machines' ──────────────
mach = pd.read_excel(os.path.join(HERE, "ctp_inputfiles", "CTP Machines.xlsx"),
                     sheet_name="Stock Machines ").dropna(subset=["Machines "])
id2name = {}
for _, r in mach.iterrows():
    for mid in re.split(r"[,/]", str(r["Machine Id"])):
        mid = mid.strip()
        if mid and mid.lower() != "nan":
            id2name[mid] = str(r["Machines "]).strip()

def _name(m):
    m = str(m).strip()
    return id2name.get(m) or id2name.get(m.rstrip(" MF")) or ""

def _itemtypes(product):
    """routed_product -> item_type map from the itemType master."""
    f = os.path.join(RAW, f"jkt_itemType_master_{product}.csv")
    if not os.path.exists(f):
        return {}
    d = pd.read_csv(f, dtype=str).fillna("")
    ic = next((c for c in d.columns if c.lower() in ("itemcode", "item_code", "code")), d.columns[0])
    it = next((c for c in d.columns if "type" in c.lower()), d.columns[-1])
    return dict(zip(d[ic].astype(str).str.strip(), d[it].astype(str).str.strip()))

rows = []
for product in ("pcr", "tbr"):
    rf = os.path.join(RAW, f"jkt_routing_{product}.csv")
    if not os.path.exists(rf):
        continue
    rt = pd.read_csv(rf, low_memory=False, dtype=str).fillna("")
    t2 = _itemtypes(product)
    rt["_it"] = rt["routed_product"].astype(str).str.strip().map(lambda x: t2.get(x, ""))
    rt["_pt"] = pd.to_numeric(rt.get("proc_time"), errors="coerce")
    rt["_ef"] = pd.to_numeric(rt.get("efficiency"), errors="coerce")
    for _, r in rt.iterrows():
        for m in re.split(r"[,]", str(r.get("machines", ""))):
            m = m.strip().strip('"').strip()
            if not m:
                continue
            rows.append({
                "Product": product.upper(),
                "Section / Operation": str(r.get("operation_name", "")).strip() or str(r.get("department", "")).strip(),
                "Machine Id": m,
                "Machine Name": _name(m),
                "Material (Item Type)": r["_it"],
                "OUR proc_time": r["_pt"],
                "OUR proc_time UOM": str(r.get("proc_time_UOM", "")).strip(),
                "OUR efficiency": r["_ef"],
            })

df = pd.DataFrame(rows)
# aggregate to one row per (product, machine, operation, material): median proc_time, common UOM
g = (df.groupby(["Product", "Section / Operation", "Machine Id", "Machine Name", "Material (Item Type)"])
       .agg({"OUR proc_time": "median", "OUR proc_time UOM": lambda s: s.mode().iat[0] if len(s.mode()) else "",
             "OUR efficiency": "median"})
       .reset_index())
g["OUR proc_time"] = g["OUR proc_time"].round(2)
g["OUR efficiency"] = g["OUR efficiency"].round(3)
# blank columns for the plant to fill from 1 month of production data
for c in ["PLANT proc_time (fill)", "PLANT proc_time UOM (fill)", "PLANT efficiency (fill)",
          "PLANT qty produced 1-month (fill)", "PLANT run hours 1-month (fill)", "Notes / correction"]:
    g[c] = ""
g = g.sort_values(["Product", "Machine Id", "Section / Operation", "Material (Item Type)"]).reset_index(drop=True)

with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
    g.to_excel(xl, sheet_name="Machine Line Speeds", index=False)
    g[g["Product"] == "PCR"].to_excel(xl, sheet_name="PCR", index=False)
    g[g["Product"] == "TBR"].to_excel(xl, sheet_name="TBR", index=False)
print(f"  wrote {OUT}")
print(f"  rows: {len(g)}  (PCR {int((g['Product']=='PCR').sum())}, TBR {int((g['Product']=='TBR').sum())})")
print(f"  machines covered: {g['Machine Id'].nunique()} | materials: {g['Material (Item Type)'].nunique()}")
