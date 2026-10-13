#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_planner.py -- the planning engine end to end: requirement -> curing plan + building plan.

    production requirement (SKU x qty)
              |
              v
    [plan_curing]   press sizing -> mould cap -> campaign packing -> shift emission
              |                                          -> plan.csv          (the ANCHOR)
              v
    [plan_building] press draw curve -> hourly deadlines -> campaigns -> latest-fit on TBMs
                                                     -> building_plan.csv     (the PULL)
                                                     -> opening WIP the month must start with
              |
              v
    scheduler (run_scheduler.py) reads both as inputs/

The two outputs are INTERLOCKED by construction: the building plan is derived from the curing plan's
draw curve, so it cannot drift from it. That is the property the hand-supplied plans did not have.

Usage:
    python -m planning_engine.run_planner                      # replan June from MES requirement
    python -m planning_engine.run_planner --requirement my.csv --start 2026-07-01 --days 31
    python -m planning_engine.run_planner --install            # write into inputs/ for the scheduler
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd

from .plan_building import build_building_plan
from .plan_curing import build_curing_plan

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RULEBOOK = HERE / "_rulebook"
OUT = HERE / "_plan"


def sku_level_requirement(rulebook_dir: Path, root: Path) -> pd.DataFrame:
    """Explode the GT-level requirement to SKU level and attach every planning parameter.

    The MES only knows green tyres; the plan files are written per finished SKU, and 10 GT codes
    carry more than one SKU (same green tyre, different marking). Where a GT has several SKUs the
    requirement is split by the SKU mix in the existing plan, falling back to an even split.
    """
    req = pd.read_csv(rulebook_dir / "requirement_june.csv")
    sku_master = pd.read_csv(rulebook_dir / "sku_master.csv")

    mix = {}
    plan_csv = root / "inputs" / "plan.csv"
    if plan_csv.exists():
        p = pd.read_csv(plan_csv, usecols=["skuCode", "qty", "remarks"])
        p["remarks"] = p["remarks"].astype(str).str.strip()
        for gt, g in p.groupby("remarks"):
            s = g.groupby("skuCode")["qty"].sum()
            if s.sum() > 0:
                mix[gt] = (s / s.sum()).to_dict()

    rows = []
    for _, r in req.iterrows():
        gt = str(r["gt_code"]).strip()
        skus = [s for s in str(r.get("sku_codes", "") or "").split("|") if s and s != "nan"]
        if not skus:
            skus = [gt]                       # no SKU mapping -> plan at GT level
        w = mix.get(gt, {})
        tot = sum(w.get(s, 0.0) for s in skus)
        for s in skus:
            share = (w.get(s, 0.0) / tot) if tot > 0 else 1.0 / len(skus)
            rows.append({"sku_code": s, "gt_code": gt,
                         "qty_month": float(r["qty_month"]) * share})
    out = pd.DataFrame(rows)
    out = out[out["qty_month"] > 0]
    out = out.merge(
        sku_master[["gt_code", "cure_cycle_min_tyre", "build_cycle_min", "mould_sets_instant"]],
        on="gt_code", how="left")
    return out.reset_index(drop=True)


def gt_shelf_hours(req: pd.DataFrame, default_h: float = 48.0) -> dict[str, float]:
    """Per-SKU green-tyre shelf life, read from the plant's aging master.

    This is the bound on how early the building plan may run ahead of the presses, so it is taken
    from the master the rest of the scheduler already obeys (48 h for 347 of 352 GT codes) rather
    than assumed.
    """
    path = ROOT / "inputs" / "aging_master.csv"
    if not path.exists():
        return {}
    ag = pd.read_csv(path)
    ag["ItemCode"] = ag["ItemCode"].astype(str).str.strip()
    unit = ag.get("MaxAgingUnit", pd.Series([""] * len(ag))).astype(str).str.upper()
    hours = pd.to_numeric(ag["MaxAging"], errors="coerce")
    hours = hours * unit.map(lambda u: 24.0 if "DAY" in u else (1 / 60.0 if "MIN" in u else 1.0))
    by_gt = dict(zip(ag["ItemCode"], hours))
    return {r["sku_code"]: float(by_gt.get(str(r["gt_code"]).strip(), default_h) or default_h)
            for _, r in req.iterrows()}


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate the curing + building plan from a requirement.")
    ap.add_argument("--requirement", default="", help="SKU-level requirement CSV "
                                                      "(sku_code, gt_code, qty_month); default = June from MES")
    ap.add_argument("--start", default="2026-06-01", help="plan start date (shift A begins 07:00)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--plan-id", default="CTP_PCR")
    ap.add_argument("--opening-wip", type=float, default=4500.0,
                    help="green tyres allowed on the floor at t0 (user cap; plan is checked "
                         "against it, never silently clamped)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--install", action="store_true",
                    help="copy the generated plans over inputs/plan.csv and inputs/building_plan.csv "
                         "(originals are backed up to *.handplan.bak)")
    a = ap.parse_args()

    rulebook = json.loads((RULEBOOK / "rulebook.json").read_text(encoding="utf-8"))
    press_master = pd.read_csv(RULEBOOK / "press_master.csv", dtype={"press": str})
    tbm_master = pd.read_csv(RULEBOOK / "tbm_master.csv", dtype={"tbm": str})

    if a.requirement:
        req = pd.read_csv(a.requirement)
        sku_master = pd.read_csv(RULEBOOK / "sku_master.csv")
        need = ["cure_cycle_min_tyre", "build_cycle_min", "mould_sets_instant"]
        if any(c not in req.columns for c in need):
            req = req.merge(sku_master[["gt_code"] + need], on="gt_code", how="left")
    else:
        req = sku_level_requirement(RULEBOOK, ROOT)

    start = pd.Timestamp(a.start)
    print(f"  requirement       : {len(req)} SKUs, {req['qty_month'].sum():,.0f} tyres "
          f"over {a.days} days from {start.date()}")

    curing, sizing = build_curing_plan(req, press_master, rulebook, start, a.days, a.plan_id)
    print(f"  CURING plan       : {len(curing):,} press-shift rows, {curing['qty'].sum():,} tyres, "
          f"{curing['pressNo'].nunique()} presses, {curing['skuCode'].nunique()} SKUs")
    infeas = sizing[sizing["mould_infeasible"]]
    if len(infeas):
        print(f"  ! mould-capped    : {len(infeas)} SKUs need more presses than mould sets "
              f"-> {', '.join(infeas['sku_code'].head(5))}")

    shelf = gt_shelf_hours(req)
    building, brep = build_building_plan(curing, req, tbm_master, rulebook, a.plan_id,
                                         gt_shelf_h=shelf)
    print(f"  BUILDING plan     : {len(building):,} blocks, {building['qty'].sum():,} tyres, "
          f"{building['buildMachine'].nunique()} TBMs")
    print(f"  interlock         : coverage {brep['coverage_pct']}% of press draw, "
          f"{brep['campaigns']:,} campaigns of median {brep['campaign_min_median']:.0f} min")
    print(f"  corridor          : GT WIP peak {brep['gt_wip_peak_total']:,.0f} "
          f"(mean {brep['gt_wip_mean_total']:,.0f}) | {brep['corridor_breaches']} SKUs over their "
          f"kanban cap, worst +{brep['worst_peak_over_cap_pct']}%")
    cap = float(a.opening_wip)
    ok = "OK" if brep["opening_wip_total"] <= cap else "OVER"
    print(f"  opening WIP       : {brep['opening_wip_total']:,.0f} green tyres at t0 across "
          f"{len(brep['opening_wip_required'])} SKUs  (cap {cap:,.0f} -> {ok})")
    if brep["opening_wip_total"] > cap:
        top = sorted(brep["opening_wip_required"].items(), key=lambda kv: -kv[1])[:5]
        print(f"  ! OVER CAP by {brep['opening_wip_total'] - cap:,.0f}: "
              + ", ".join(f"{k} {v:,.0f}" for k, v in top))
    if brep["coverage_pct"] < 99.9:
        print(f"  ! COVERAGE SHORT  : building plan covers only {brep['coverage_pct']}% of the press "
              f"draw -- presses would starve; raise TBM capacity or cut the requirement")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    curing.to_csv(out / "plan.csv", index=False)
    building.to_csv(out / "building_plan.csv", index=False)
    sizing.to_csv(out / "press_sizing.csv", index=False)
    req.to_csv(out / "requirement_used.csv", index=False)
    (out / "building_report.json").write_text(json.dumps(brep, indent=2), encoding="utf-8")
    print(f"  wrote -> {out}")

    if a.install:
        for name in ("plan.csv", "building_plan.csv"):
            dst = ROOT / "inputs" / name
            if dst.exists():
                bak = dst.with_suffix(".csv.handplan.bak")
                if not bak.exists():
                    shutil.copy2(dst, bak)
                    print(f"  backed up {name} -> {bak.name}")
            shutil.copy2(out / name, dst)
            print(f"  installed {name} -> inputs/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
