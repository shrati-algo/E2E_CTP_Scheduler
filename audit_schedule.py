#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_schedule.py -- plant-head defect audit of the final floor schedule.
   Checks the things a production head would reject a plan for:
     D1 machine overlap      : two real jobs on the same machine at the same time
     D2 consume-before-make  : a lot consumed before its producer finishes (time-travel / infeasible feed)
     D3 shelf-life aging     : a component consumed after it has aged past its shelf life
     D4 broken lot chain     : a consumed lot that is neither produced, opening WIP, nor raw material
     D5 cure-without-GT      : a cured tyre with no green-tyre build
     D6 zero/negative dur    : rows with produce_qty>0 but end<=start
   Read-only.  Run AFTER append_all_changeover.py."""
import pandas as pd, os, glob
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__)); O2 = os.path.join(HERE, "outputs2")
fs = pd.read_csv(os.path.join(O2, "phase5_floor_schedule_updated.csv"), low_memory=False)
fs["s"] = pd.to_datetime(fs["start_time"], errors="coerce")
fs["e"] = pd.to_datetime(fs["end_time"], errors="coerce")
fs["q"] = pd.to_numeric(fs["produce_qty"], errors="coerce").fillna(0.0)
fs["proc_u"] = fs["process"].astype(str).str.upper()
prod = fs[(fs["proc_u"] != "CHANGEOVER")].copy()          # real production rows
print(f"floor rows: {len(fs):,}  (production {len(prod):,}, changeover {len(fs)-len(prod):,})")
print("=" * 78)

# opening WIP lots (available at t0, no producer row expected) + aging shelf lives
inv = pd.read_csv(os.path.join(HERE, "inputs", "inventory.csv")) if os.path.exists(os.path.join(HERE, "inputs", "inventory.csv")) else pd.DataFrame()
ag = pd.read_csv(os.path.join(HERE, "inputs", "aging_master.csv"))
bom = pd.read_csv(os.path.join(HERE, "inputs", "bom.csv"))
c2t = {str(r["child"]).strip(): str(r["child_description"]).strip() for _, r in bom.iterrows()}
def _shelf_h(itemcode):
    it = c2t.get(str(itemcode).strip().split("_L")[0], "")
    row = ag[ag[("ItemCode" if "ItemCode" in ag.columns else "itemcode")].astype(str).str.strip() == str(itemcode).strip().split("_L")[0]]
    if not len(row):
        return None
    v = float(row.iloc[0]["MaxAging"]); u = str(row.iloc[0]["MaxAgingUnit"]).upper()
    return v * 24 if "DAY" in u else (v / 60 if "MIN" in u else v)

# ---- producer index: lot_id -> (start, end, machine) ----
pend = prod.groupby("lot_id").agg(ps=("s", "min"), pe=("e", "max")).to_dict("index")
produced = set(prod["lot_id"].astype(str))
open_lots = set()  # opening-WIP lot ids look like <item>_WIP or are simply absent producers of raw stages

# ============================ D1  machine overlap ============================
d1 = 0; d1_ex = []
real = prod[(fs["q"] > 0) & prod["s"].notna() & prod["e"].notna() & (prod["e"] > prod["s"])]
for m, g in real.groupby("machine"):
    g = g.sort_values("s")
    pe = None; pit = None; pend_t = None
    for _, r in g.iterrows():
        if pend_t is not None and r["s"] < pend_t - pd.Timedelta(seconds=1):
            d1 += 1
            if len(d1_ex) < 5:
                d1_ex.append(f"m/c {m}: {pit} ends {pend_t}  vs  {r['item']} starts {r['s']}")
        if pend_t is None or r["e"] > pend_t:
            pend_t = r["e"]; pit = r["item"]
print(f"D1 machine overlap (same m/c, two jobs at once) : {d1}")
for x in d1_ex: print("      -", x)

# ============================ D2  consume-before-make ========================
# for each production row, every consumed lot's producer must FINISH before this row starts
d2 = 0; d2_ex = []; d3 = 0; d3_ex = []; d4 = 0; d4_items = defaultdict(int)
for _, r in prod.iterrows():
    cons = str(r["consumption_id"])
    if cons in ("", "nan", "RAW_MATERIAL"):
        continue
    for cl in cons.split(";"):
        cl = cl.strip()
        if not cl:
            continue
        if cl in pend:                                    # produced in-plan -> check time + aging
            pe = pend[cl]["pe"]
            if pd.notna(pe) and pd.notna(r["s"]) and pe > r["s"] + pd.Timedelta(minutes=1):
                d2 += 1
                if len(d2_ex) < 6:
                    d2_ex.append(f"{r['item']} @ {r['s']} consumes {cl} but it finishes {pe}")
            sh = _shelf_h(cl)
            if sh and pd.notna(pe) and pd.notna(r["s"]):
                age_h = (r["s"] - pe).total_seconds() / 3600.0
                if age_h > sh + 0.5:
                    d3 += 1
                    if len(d3_ex) < 6:
                        d3_ex.append(f"{cl} made {pe}, used {r['s']}  age {age_h:.0f}h > shelf {sh:.0f}h  (into {r['item']})")
        else:                                             # not produced in-plan
            base = cl.split("_L")[0]
            # acceptable if it is a leaf/raw or covered by opening WIP; else a broken chain
            it = c2t.get(base, "")
            if it in ("MASTER COMPOUND", "FINAL COMPOUND", "Steel Belt Edge Strip") or base.upper().startswith(("CHEM", "RM")):
                continue
            d4 += 1; d4_items[base] += 1
print(f"D2 consume-before-make (infeasible feed)        : {d2}")
for x in d2_ex: print("      -", x)
print(f"D3 shelf-life aging violation (in-plan lots)    : {d3}")
for x in d3_ex: print("      -", x)
print(f"D4 broken lot chain (consumed, never produced)  : {d4}  distinct items {len(d4_items)}")
for k, v in sorted(d4_items.items(), key=lambda x: -x[1])[:8]:
    print(f"      - {k} ({c2t.get(k,'?')}): referenced {v}x")

# ============================ D5  cure-without-GT ============================
cur = fs[fs["department"].astype(str).str.upper() == "CURING"]
bld = fs[fs["department"].astype(str).str.upper() == "BUILDING"]
gt_built = set(bld["item"].astype(str).str.strip())
# map cured SKU -> its GT code via BOM
sku2gt = {r["Super_parent"]: str(r["child"]).strip()
          for _, r in bom[bom["child_description"].astype(str).str.strip().isin(["Green Tyres", "GREEN_TYRE"])].iterrows()}
cured_sku = set(cur["item"].astype(str).str.strip())
d5 = [s for s in cured_sku if sku2gt.get(s, s) not in gt_built and s not in gt_built]
print(f"D5 cure-without-green-tyre (SKUs)               : {len(d5)}")
for s in d5[:8]: print("      -", s, "-> GT", sku2gt.get(s, "?"))

# ============================ D6  zero/neg duration =========================
d6 = int(((fs["q"] > 0) & (fs["e"] <= fs["s"])).sum())
print(f"D6 zero/negative-duration production rows       : {d6}")
print("=" * 78)
tot = d1 + d2 + d3 + d4 + len(d5) + d6
print(f"TOTAL hard defects: D1={d1} D2={d2} D3={d3} D4={d4} D5={len(d5)} D6={d6}")
