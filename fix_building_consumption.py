#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_building_consumption.py -- fill the REAL consumed component lots on BUILDING rows.

WHY THIS IS NEEDED
  Phase 5 replaces the phase-2 green-tyre lots with the building-plan echo's synthetic
  blocks (lot_id "BLD00024"). Those BLD ids are NOT nodes in the phase-3 DAG, so
  _consumption_kit() finds no predecessors for them and falls through to its
  raw-material default -- every BUILDING row ends up saying "RAW_MATERIAL", which is
  wrong: building consumes Steel Belt, Ply, Bead Apex, Inner Liner, SideWall, Tread
  and Cap Strip lots, all of which ARE scheduled in the same floor file.

WHAT IT DOES  (same logic as the [build-trace] block in append_all_changeover.py)
  1. Bind each building block to a real phase-2 GT lot, in build-time order, by
     cumulative quantity within its GT code.
  2. Take that GT lot's phase-3 DAG predecessors = its true component lots.
  3. Per component ITEM, name the lot already FINISHED by the block's start (FEFO).
     Nothing finished yet -> that component came from opening WIP -> "WIP:<item>",
     so a row is never blank and never reads as consume-before-make.

SCOPE: only the `consumption_id` cell of BUILDING rows is touched. Row count, times,
quantities, machines, lot_id and every other column are left byte-identical.

USAGE
  python fix_building_consumption.py <run_folder>
  (default run folder: output/CTP_JUNE_CORRECTED_20260803)
"""
import bisect
import os
import sys
from collections import defaultdict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "output", "CTP_JUNE_CORRECTED_20260803")
if not os.path.isabs(RUN):
    RUN = os.path.join(HERE, RUN)

FLOOR = os.path.join(RUN, "phase5_floor_schedule_updated.csv")


def _find(*names):
    """Locate a phase file either at the run root or under PHASEWISE/."""
    for n in names:
        for cand in (os.path.join(RUN, n),
                     *[os.path.join(RUN, "PHASEWISE", d, n)
                       for d in os.listdir(os.path.join(RUN, "PHASEWISE"))
                       if os.path.isdir(os.path.join(RUN, "PHASEWISE", d))]):
            if os.path.exists(cand):
                return cand
    raise FileNotFoundError(names[0])


fs = pd.read_csv(FLOOR, low_memory=False)
print("floor schedule : %-6s rows   %s" % (len(fs), FLOOR))

# ── production rows, for lot finish times ────────────────────────────────────────
prod = fs[fs["process"].astype(str).str.upper() != "CHANGEOVER"].copy()
prod["s"] = pd.to_datetime(prod["start_time"], errors="coerce")
prod["e"] = pd.to_datetime(prod["end_time"], errors="coerce")

# ── 1. phase-2 GT lots, in lot order, per GT code ────────────────────────────────
p2 = _find("phase2_lots_updated.csv")
L = pd.read_csv(p2, usecols=["lot_id", "item_code", "item_type", "lot_qty"], low_memory=False)
gtL = L[L["item_type"].astype(str).str.contains("Green", case=False, na=False)].copy()
gtL["lot_id"] = gtL["lot_id"].astype(str)
code_lots = defaultdict(list)                       # GT code -> [(lot_id, qty), ...]
for r in gtL.sort_values("lot_id").itertuples():
    code_lots[str(r.item_code).strip()].append((r.lot_id, float(r.lot_qty or 0)))
print("phase2 GT lots : %-6s lots over %s GT codes" % (len(gtL), len(code_lots)))

# ── 2. phase-3 DAG: GT lot -> its component lots ─────────────────────────────────
p3 = _find("phase3_dag_edges_updated.csv.gz", "phase3_dag_edges_updated.csv",
           "phase3_dag_edges.csv.gz", "phase3_dag_edges.csv")
dag = pd.read_csv(p3, usecols=["producer_lot", "consumer_lot"], low_memory=False,
                  compression="gzip" if p3.endswith(".gz") else None)
preds = defaultdict(list)
for p, c in zip(dag["producer_lot"].astype(str), dag["consumer_lot"].astype(str)):
    preds[c].append(p)
print("phase3 DAG     : %-6s edges  %s consumer lots" % (len(dag), len(preds)))

# ── 3. per GT code, per component ITEM, the component lots sorted by FINISH ──────
cend = prod.groupby("lot_id")["e"].max()
code_item = defaultdict(lambda: defaultdict(list))  # GT code -> comp item -> [(finish_ns, lot)]
for code, gts in code_lots.items():
    for gl, _ in gts:
        for cl in preds.get(gl, []):
            f = cend.get(cl)
            if f is not None and pd.notna(f):
                code_item[code][cl.split("_L")[0]].append((f.value, cl))
for code in code_item:
    for itm in code_item[code]:
        code_item[code][itm].sort()

# ── 4. walk the building rows ────────────────────────────────────────────────────
bmask = (fs["department"].astype(str).str.upper().str.contains("BUILDING", na=False)
         & (fs["process"].astype(str).str.upper() != "CHANGEOVER"))
b = fs[bmask].copy()
b["_s"] = pd.to_datetime(b["start_time"], errors="coerce")
b["_q"] = pd.to_numeric(b["produce_qty"], errors="coerce").fillna(0.0)
b["_code"] = b["item"].astype(str).str.strip()
print("building rows  : %-6s (was 100%% RAW_MATERIAL)" % len(b))

cons, gtlot = {}, {}
n_nomap = 0
n_wip_parts = 0
for code, grp in b.groupby("_code"):
    lots = code_lots.get(code, [])
    if not lots:
        n_nomap += len(grp)
        continue
    tot_lot = sum(q for _, q in lots) or 1.0
    tot_bld = grp["_q"].sum() or 1.0
    bounds, cc = [], 0.0
    for lid, q in lots:                              # cumulative-fraction boundary per lot
        cc += q
        bounds.append((lid, cc / tot_lot))
    items = code_item.get(code, {})
    # every component ITEM this GT code needs (even if none of its lots is scheduled)
    all_items = set(items)
    for gl, _ in lots:
        for cl in preds.get(gl, []):
            all_items.add(cl.split("_L")[0])
    cum = 0.0
    for idx, row in grp.sort_values("_s").iterrows():
        cum += row["_q"]
        frac = (cum - row["_q"] / 2.0) / tot_bld     # block midpoint as a fraction of the code's build
        lid = lots[-1][0]
        for l, bf in bounds:
            if frac <= bf:
                lid = l
                break
        gtlot[idx] = lid
        T = row["_s"].value if pd.notna(row["_s"]) else 0
        parts = []
        for itm in sorted(all_items):
            ll = items.get(itm, ())
            k = bisect.bisect_right([f for f, _ in ll], T) - 1 if ll else -1
            if k >= 0:
                parts.append(ll[k][1])               # FEFO-current lot, finished by this block
            else:
                parts.append("WIP:" + itm)           # fed from opening WIP -- not a floor lot
                n_wip_parts += 1
        cons[idx] = ";".join(parts) if parts else "RAW_MATERIAL"

if not cons:
    print("\nNOTHING TO FIX -- no building row could be bound to a phase-2 GT lot.")
    sys.exit(1)

fs.loc[list(cons), "consumption_id"] = pd.Series(cons)
fs["GT_lot_id"] = ""
fs.loc[list(gtlot), "GT_lot_id"] = pd.Series(gtlot)   # traceability: which phase-2 GT lot the block is

fs.to_csv(FLOOR, index=False)
print("\nWROTE %s" % FLOOR)

# ── report ───────────────────────────────────────────────────────────────────────
chk = pd.read_csv(FLOOR, low_memory=False)
cb = chk[chk["department"].astype(str).str.upper().str.contains("BUILDING", na=False)]
still_raw = (cb["consumption_id"].astype(str) == "RAW_MATERIAL").sum()
print("bound          : %s building rows (%s had no phase-2 GT lot)" % (len(cons), n_nomap))
print("still RAW      : %s" % still_raw)
print("WIP: parts     : %s (component came from opening WIP, not a scheduled lot)" % n_wip_parts)
ncomp = cb["consumption_id"].astype(str).str.count(";") + 1
print("components/row : min %d  median %.0f  max %d" % (ncomp.min(), ncomp.median(), ncomp.max()))
print("\nSAMPLE")
for _, r in cb.head(4).iterrows():
    print("  %s  %-18s  %s  qty=%s" % (r["date"], r["item"], r["machine"], r["produce_qty"]))
    print("     lot_id         : %s   (GT lot: %s)" % (r["lot_id"], r["GT_lot_id"]))
    print("     consumption_id : %s" % r["consumption_id"])
