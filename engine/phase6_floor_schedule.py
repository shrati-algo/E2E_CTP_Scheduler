#!/usr/bin/env python3
"""phase6_floor_schedule.py — the publishable, datetime- and shift-wise floor schedule.

v10 phase 6 on CTP data. ONE table covering the whole chain — mixing,
calendering, extrusion, cutting, slitting, bead, pre assembly AND the building
trolleys — so a supervisor sees upstream and building together.

  * split by plant shift: A 07:00-15:00, B 15:00-23:00, C 23:00-07:00 (next day);
  * a lot crossing a boundary becomes several rows with the quantity prorated by
    time, and NOS/batch quantities use LARGEST-REMAINDER rounding so the split
    rows sum back to exactly the lot quantity — never 15 + 15 for a lot of 31;
  * `consumer_machine` names the IMMEDIATE consumer by machine name, and the
    BUILDING GROUP where a lot goes straight to the drum;
  * building rows carry the planned start beside the feasible one, so it is
    visible at a glance which trolleys moved and by how much.

REPORTING CONVENTIONS (v10, and they matter)
  * mixing is counted in BATCHES — one charge of the vessel;
  * day-wise GT is building lots with within_horizon == True, bucketed by the
    FEASIBLE_END plant day. Bucketing by build_start restates the plan and reads
    100% every day.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io

NOS_UOMS = {"NOS", "NO", "EA", "PCS"}


def shift_rows(start, end, qty, uom, whole: bool):
    """Split [start,end) at plant shift boundaries, prorating qty."""
    out = []
    cuts = []
    t = (start - pd.Timedelta(hours=io.SHIFT_START_H)).normalize() \
        + pd.Timedelta(hours=io.SHIFT_START_H)
    while t <= end:
        for h in (0, 8, 16, 24):
            b = t + pd.Timedelta(hours=h)
            if start < b < end:
                cuts.append(b)
        t += pd.Timedelta(days=1)
    bounds = [start] + sorted(set(cuts)) + [end]
    total = (end - start).total_seconds() or 1.0
    raw = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        raw.append((a, b, qty * (b - a).total_seconds() / total))
    if not raw:
        raw = [(start, end, qty)]
    if whole and len(raw) > 1:                 # largest-remainder rounding
        flo = [int(x[2]) for x in raw]
        rem = sorted(range(len(raw)), key=lambda i: -(raw[i][2] - flo[i]))
        short = int(round(qty)) - sum(flo)
        for i in rem[:max(0, short)]:
            flo[i] += 1
        raw = [(a, b, float(f)) for (a, b, _), f in zip(raw, flo)]
    for a, b, q in raw:
        out.append({"date": io.plant_day(a).date(), "shift": io.shift_of(a),
                    "start_time": a, "end_time": b, "produce_qty": round(q, 3)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v1")
    args = ap.parse_args()
    OUT = io.outdir(args.run, args.line)

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 6 — floor schedule (datetime + shift wise)   [{args.line.upper()}]")
    print("=" * 74)

    mach = io.load_machines()
    name_of = dict(zip(mach["machineDisplayId"], mach["machineDisplayName"]))
    bom = io.load_bom(args.line)
    itypes = io.load_itemtypes(bom)

    sched = pd.read_csv(OUT / "phase5_schedule.csv", parse_dates=["start", "finish"])
    sched = sched[sched["start"].notna()].copy()
    blots = pd.read_csv(OUT / "phase5_building_plan.csv",
                        parse_dates=["build_start", "build_end",
                                     "feasible_start", "feasible_end"])
    edges = pd.read_csv(OUT / "phase4_edges.csv")
    alloc = pd.read_csv(OUT / "phase3_lot_demand.csv", usecols=["lot_id", "building_lot_id"])

    # ---- who eats this lot -------------------------------------------------
    mach_of = dict(zip(sched["lot_id"], sched["machine"]))
    cons: dict = defaultdict(set)
    for p, c in zip(edges["producer_lot"], edges["consumer_lot"]):
        m = mach_of.get(c)
        if m:
            cons[p].add(str(name_of.get(str(m), m)))
    eq = bom["Equipment"].iloc[0]
    drum_lots = set(alloc["lot_id"])
    for l in drum_lots:
        if l not in cons:
            cons[l].add(eq)                    # straight to the drum

    rows = []
    for r in sched.itertuples(index=False):
        whole = str(r.uom).strip().upper() in NOS_UOMS
        batches = float(r.batches or 0)
        for p in shift_rows(r.start, r.finish, float(r.lot_qty), r.uom, whole):
            rows.append({
                **p, "machine": r.machine,
                "machine_name": name_of.get(str(r.machine), str(r.machine)),
                "department": r.department, "process": r.operation,
                "item": r.item_code, "item_type": r.item_type,
                "lot_id": r.lot_id, "UOM": r.uom,
                "batches": round(batches * p["produce_qty"] / max(float(r.lot_qty), 1e-9), 3)
                if batches else 0.0,
                "lots": 0, "changeover_min": r.changeover_min,
                "stage": "UPSTREAM",
                "consumer_machine": ";".join(sorted(cons.get(r.lot_id, ()))[:4]),
                "planned_start": pd.NaT, "late_h": r.late_h})
    # `lots` = 1 on a lot's first row, 0 on the rest, so summing gives lots
    seen = set()
    for x in rows:
        if x["lot_id"] not in seen:
            x["lots"] = 1
            seen.add(x["lot_id"])

    # ---- the building trolleys, in the same table --------------------------
    for r in blots.itertuples(index=False):
        if pd.isna(r.feasible_start):
            continue
        for p in shift_rows(r.feasible_start, r.feasible_end, float(r.qty), "NOS", True):
            rows.append({
                **p, "machine": r.machine,
                "machine_name": name_of.get(str(r.machine), str(r.machine)),
                "department": "BUILDING", "process": "GT BUILDING",
                "item": r.built_item, "item_type": "Green Tyres",
                "lot_id": r.building_lot_id, "UOM": "NOS", "batches": 0.0, "lots": 0,
                "changeover_min": r.setup_min, "stage": "BUILDING",
                "consumer_machine": "CURING",
                "planned_start": r.build_start,
                "late_h": round(float(r.shift_h or 0.0), 2)})

    fs = pd.DataFrame(rows).sort_values(["date", "shift", "machine", "start_time"])
    # The floor reads seconds, not nanoseconds.
    for c in ("start_time", "end_time", "planned_start"):
        fs[c] = pd.to_datetime(fs[c], errors="coerce").dt.round("s")
    fs.to_csv(OUT / "floor_schedule.csv", index=False)

    up = fs[fs["stage"] == "UPSTREAM"]
    print(f"  Rows            : {len(fs):,}  ({len(up):,} upstream, "
          f"{len(fs)-len(up):,} building)")
    print(f"  Window          : {fs['start_time'].min()}  ->  {fs['end_time'].max()}")
    print(f"  Machines        : {fs['machine'].nunique()}   departments: "
          f"{fs['department'].nunique()}")
    print(f"  Consumer named  : {100*(up['consumer_machine']!='').mean():.1f}% of "
          f"upstream rows")

    # ---- day-wise GT, the ONLY correct way to bucket it --------------------
    b = blots[blots["within_horizon"] == True].copy()
    b["day"] = b["feasible_end"].map(lambda t: io.plant_day(t).date())
    plan = blots.copy()
    plan["day"] = plan["build_start"].map(lambda t: io.plant_day(t).date())
    day = (plan.groupby("day")["qty"].sum().rename("plan")
           .to_frame().join(b.groupby("day")["qty"].sum().rename("built"),
                            how="outer")
           .fillna(0.0))
    # OUTER, not the default left join. Joining onto the PLAN's day index
    # silently DROPPED green tyres whose feasible_end landed on a day the plan
    # has no row for: the plan ends 30 Sep but building runs to the 1 Oct
    # horizon, so 1,695 tyres vanished from this table while still counting in
    # the headline. The day-wise sheet and the summary sheet disagreed by
    # exactly that much.
    # `.replace(0, pd.NA)` turns the column object-dtype and .round() then
    # raises. Divide numerically and mask the zero denominator instead.
    _p = pd.to_numeric(day["plan"], errors="coerce")
    _b = pd.to_numeric(day["built"], errors="coerce")
    day["plan"], day["built"] = _p.fillna(0.0), _b.fillna(0.0)
    day["pct"] = (100.0 * day["built"] / _p.where(_p > 0)).astype(float).round(1)
    day.to_csv(OUT / "phase6_daywise_gt.csv")
    print(f"\n  DAY-WISE GT  (built = within_horizon, bucketed on FEASIBLE_END)")
    print(f"    {'day':12s}{'plan':>10s}{'built':>10s}{'%':>8s}")
    for d, r in day.iterrows():
        print(f"    {str(d):12s}{r['plan']:>10,.0f}{r['built']:>10,.0f}"
              f"{(r['pct'] if pd.notna(r['pct']) else 0):>7.1f}%")
    print(f"    {'TOTAL':12s}{day['plan'].sum():>10,.0f}{day['built'].sum():>10,.0f}"
          f"{100*day['built'].sum()/max(day['plan'].sum(),1):>7.1f}%")

    dep = (up.groupby("department").agg(rows=("lot_id", "size"),
                                        lots=("lots", "sum"),
                                        qty=("produce_qty", "sum"),
                                        batches=("batches", "sum")))
    dep.to_csv(OUT / "phase6_by_department.csv")
    print(f"\n[OK] floor_schedule.csv ({len(fs):,})  phase6_daywise_gt.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
