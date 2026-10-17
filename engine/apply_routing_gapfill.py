#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_routing_gapfill.py -- fill routing rows whose proc_time is BLANK (2026-09-26).

WHY: phase2 silently makes ZERO lots for an item with no proc_time, so the item is never
produced (Sep run: GT T1457 STAR = 1,301 tyres of SKU 1325214617107MSXT0 never built; T7110,
XMT180, SWRS30 never made; NYDBCHF TBR chafer 19,217 m never made).

HOW (in priority order; every fill is PRINTED and written to inputs/routing_gapfill_log.csv):
  1. GREEN TYRE -> the plant's own BUILDING PLAN cycle time for that SKU (plant data).
  2. otherwise  -> median proc_time of items with the SAME operation on the SAME machine set
                   (same UOM) -- an ASSUMPTION, flagged ASSUMED_SIBLING_MEDIAN.
  3. no sibling -> left blank and REPORTED (nothing invented).
Run AFTER adapt_inputs.py and apply_biascutter_calibration.py. Replace every ASSUMED value
with the plant's measured number when it is supplied.
"""
import os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
INP = os.path.join(HERE, "inputs")


def main():
    rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
    pt = pd.to_numeric(rt["proc_time"], errors="coerce")
    bom = pd.read_csv(os.path.join(INP, "bom.csv"), dtype=str,
                      usecols=["Super_parent", "child", "child_description"])
    # SCOPE: only items THIS plan needs (children in the BOM of a plan SKU) and never CURING
    # rows (curing is scheduled by phase6 from the plan, not from routing proc_time).
    plan_skus = set(pd.read_csv(os.path.join(INP, "plan.csv"), dtype=str)["skuCode"].str.strip())
    bp_path = os.path.join(INP, "building_plan.csv")
    if os.path.exists(bp_path):
        plan_skus |= set(pd.read_csv(bp_path, dtype=str)["skuCode"].str.strip())
    needed = set(bom.loc[bom["Super_parent"].str.strip().isin(plan_skus), "child"].str.strip())
    blank = (pt.isna() & rt["routed_product"].str.strip().isin(needed)
             & ~rt["operation_name"].astype(str).str.upper().str.contains("CURING"))
    if not blank.any():
        print("  routing gap-fill: no blank proc_time rows on items this plan needs")
        return 0
    gtb = bom[bom["child_description"].isin(["Green Tyres", "GREEN_TYRE"])]
    gt2sku = gtb.groupby(gtb["child"].str.strip())["Super_parent"].agg(
        lambda s: set(s.str.strip()))
    bp = pd.read_csv(bp_path) if os.path.exists(bp_path) else pd.DataFrame()

    log = []
    for prod in sorted(rt.loc[blank, "routed_product"].str.strip().unique()):
        rows = blank & (rt["routed_product"].str.strip() == prod)
        r0 = rt[rows].iloc[0]
        val = uom = src = None
        if prod in gt2sku.index and not bp.empty:
            x = bp[bp["skuCode"].astype(str).isin(gt2sku[prod])]
            ct = pd.to_numeric(x.get("cycleTime"), errors="coerce").dropna()
            if len(ct):
                val, uom, src = round(float(ct.median()) * 60.0, 2), "SEC", "PLANT_BUILDING_PLAN"
        if val is None:
            sib = rt[(rt["operation_name"] == r0["operation_name"]) &
                     (rt["machines"] == r0["machines"]) & pt.notna()]
            if len(sib):
                u = sib["proc_time_UOM"].mode().iloc[0]
                sib = sib[sib["proc_time_UOM"] == u]
                val = round(float(pd.to_numeric(sib["proc_time"]).median()), 4)
                uom, src = u, "ASSUMED_SIBLING_MEDIAN"
                if str(r0.get("batch_size")).strip() in ("", "nan", "None"):
                    bs = pd.to_numeric(sib["batch_size"], errors="coerce").dropna()
                    if len(bs):
                        rt.loc[rows, "batch_size"] = str(float(bs.median()))
                        if sib["batch_UNIT"].notna().any():
                            rt.loc[rows, "batch_UNIT"] = sib["batch_UNIT"].dropna().mode().iloc[0]
        if val is None:
            log.append((prod, r0["operation_name"], r0["machines"], "", "", "NOT FILLED - no data"))
            print(f"  [gap] {prod:<18} {r0['operation_name']:<14} NOT FILLED (no plant/sibling data)")
            continue
        rt.loc[rows, "proc_time"] = str(val)
        rt.loc[rows, "proc_time_UOM"] = uom
        log.append((prod, r0["operation_name"], r0["machines"], val, uom, src))
        print(f"  [gap] {prod:<18} {r0['operation_name']:<14} proc_time={val} {uom}  <- {src}")
    rt.to_csv(os.path.join(INP, "routing.csv"), index=False)
    pd.DataFrame(log, columns=["routed_product", "operation", "machines", "proc_time", "uom",
                               "source"]).to_csv(os.path.join(INP, "routing_gapfill_log.csv"),
                                                 index=False)
    n_ok = sum(1 for l in log if l[3] != "")
    print(f"  routing gap-fill: {n_ok} of {len(log)} items filled -> inputs/routing_gapfill_log.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
