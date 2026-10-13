#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plan_reconciliation.py -- static build-vs-cure reconciliation (report-only, never auto-fix).

Under the BUILDING-PLAN ECHO both plans are pinned inputs, so per SKU we can check BEFORE the run
whether the plant's own two plans agree:

    built[sku]      = sum(building plan qty)               (green tyres the plant plans to BUILD)
    cured[sku]      = sum(curing plan qty, RUNNING)        (green tyres the plant plans to CURE)
    opening_gt[sku] = opening green-tyre WIP               (0 under cold start / zero_opening_wip)
    supply = built + opening_gt ;  balance = supply - cured

    balance < 0  -> BUILD-SHORT : curing wants more than is built -> phase6 will PHANTOM-cure the gap.
    balance > 0  -> BUILD-SURPLUS: green tyres built and never consumed -> they age out (48 h) = waste.
    balance == 0 -> BALANCED.

This complements phase6's runtime "CURED WITHOUT A GREEN TYRE" (two-layer defence, different timings).
Writes outputs2/plan_reconciliation.csv. Exit code is always 0 -- this reports, it never fails the run.
"""
from __future__ import annotations
import os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(HERE, "inputs")
OUTPUTS = os.path.join(HERE, "outputs2")


def _cfg():
    try:
        import yaml
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _opening_gt_by_sku(cfg) -> dict:
    """Opening green-tyre WIP per tyre SKU (0 when zero_opening_wip). Maps GT itemcode->SKU via BOM."""
    if bool(cfg.get("zero_opening_wip", False)):
        return {}
    inv_p = os.path.join(INPUTS, "inventory.csv")
    bom_p = os.path.join(INPUTS, "bom.csv")
    if not (os.path.exists(inv_p) and os.path.exists(bom_p)):
        return {}
    try:
        inv = pd.read_csv(inv_p)
        bom = pd.read_csv(bom_p)
        gt = bom[bom["child_description"].astype(str).str.strip().str.upper()
                 .isin(["GREEN TYRES", "GREEN_TYRE"])]
        code2sku = {str(c).strip(): str(s).strip()
                    for c, s in zip(gt["child"], gt["Super_parent"])}
        inv["sku"] = inv["itemcode"].astype(str).str.strip().map(code2sku)
        g = inv.dropna(subset=["sku"]).groupby("sku")["inventory"].sum()
        return {k: float(v) for k, v in g.items()}
    except Exception:
        return {}


def reconcile() -> int:
    cfg = _cfg()
    bp_p = os.path.join(INPUTS, "building_plan.csv")
    pl_p = os.path.join(INPUTS, "plan.csv")
    if not (os.path.exists(bp_p) and os.path.exists(pl_p)):
        print("  [reconcile] SKIP -- building_plan.csv or plan.csv missing")
        return 0

    bp = pd.read_csv(bp_p)
    pl = pd.read_csv(pl_p)
    bp["skuCode"] = bp["skuCode"].astype(str).str.strip()
    pl["skuCode"] = pl["skuCode"].astype(str).str.strip()
    bp["qty"] = pd.to_numeric(bp["qty"], errors="coerce").fillna(0)
    pl["qty"] = pd.to_numeric(pl["qty"], errors="coerce").fillna(0)
    pl = pl[pl["skuCode"].str.upper() != "CHANGEOVER"]

    built = bp.groupby("skuCode")["qty"].sum()
    cured = pl.groupby("skuCode")["qty"].sum()
    open_gt = pd.Series(_opening_gt_by_sku(cfg), dtype=float)

    skus = sorted(set(built.index) | set(cured.index) | set(open_gt.index))
    rows = []
    # earliest build finish per SKU (for the aging-exposure note on surplus)
    if "endTime" in bp.columns:
        bp["_end"] = pd.to_datetime(bp["endTime"], errors="coerce")
        last_build = bp.groupby("skuCode")["_end"].max()
    else:
        last_build = pd.Series(dtype="datetime64[ns]")
    for s in skus:
        b = float(built.get(s, 0.0))
        c = float(cured.get(s, 0.0))
        o = float(open_gt.get(s, 0.0))
        bal = b + o - c
        status = ("BALANCED" if abs(bal) < 0.5
                  else ("BUILD-SHORT" if bal < 0 else "BUILD-SURPLUS"))
        rows.append({
            "skuCode": s, "built": int(round(b)), "opening_gt": int(round(o)),
            "cured": int(round(c)), "balance": int(round(bal)), "status": status,
            "last_build_finish": (last_build.get(s) if s in getattr(last_build, "index", []) else ""),
        })
    rec = pd.DataFrame(rows).sort_values(["status", "balance"])
    os.makedirs(OUTPUTS, exist_ok=True)
    out = os.path.join(OUTPUTS, "plan_reconciliation.csv")
    rec.to_csv(out, index=False)

    n_short = int((rec["status"] == "BUILD-SHORT").sum())
    n_surp = int((rec["status"] == "BUILD-SURPLUS").sum())
    n_bal = int((rec["status"] == "BALANCED").sum())
    short_q = int(-rec.loc[rec["status"] == "BUILD-SHORT", "balance"].sum())
    surp_q = int(rec.loc[rec["status"] == "BUILD-SURPLUS", "balance"].sum())
    print("\n  PLAN RECONCILIATION  (building plan  vs  curing plan, per SKU)")
    print(f"    built total {int(built.sum()):,}  |  cured total {int(cured.sum()):,}  |  "
          f"opening GT {int(open_gt.sum()) if len(open_gt) else 0:,}")
    print(f"    BUILD-SHORT  : {n_short:3d} SKUs, {short_q:,} tyres  -> phase6 will PHANTOM-cure these")
    print(f"    BUILD-SURPLUS: {n_surp:3d} SKUs, {surp_q:,} tyres  -> built, never cured (age-out waste)")
    print(f"    BALANCED     : {n_bal:3d} SKUs")
    print(f"    Wrote: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(reconcile())
