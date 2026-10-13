#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trace_build_delays.py -- why did the early builds start later than their plan slot?

    python trace_build_delays.py <run_dir> [until "2026-09-30 07:00"]

For every build block whose PLAN slot starts before `until`:
  * plan start (building_plan.csv, matched per (TBM, green tyre) in time order) vs actual start
  * each kit component lot (floor schedule consumption_id): usable at = its finish + min aging
    (aging_master) + routing transfer time; the LATEST one is the binding component
  * TBM busy: the previous build on the same TBM finishing after the plan slot
  * the binding component is then followed UP its own chain (its latest input, recursively) to the
    root: a lot that was not held by an input (it started when its machine / the schedule allowed)
Components covered by opening stock have no producer lot in the kit and are usable at t0.
Writes <run_dir>/BUILD_DELAY_TRACE.csv and prints a summary.
"""
import os
import sys
from collections import Counter

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1]
UNTIL = pd.Timestamp(sys.argv[2] if len(sys.argv) > 2 else "2026-09-30 07:00")
H = pd.Timedelta(hours=1)

fs = pd.read_csv(os.path.join(RUN, "phase5_floor_schedule_updated.csv"), low_memory=False,
                 parse_dates=["start_time", "end_time"])
fs = fs[fs["department"].astype(str).str.upper() != "CURING"]
lots = fs.groupby("lot_id").agg(item=("item", "first"), itype=("item_type", "first"),
                                machine=("machine", "first"), dept=("department", "first"),
                                start=("start_time", "min"), end=("end_time", "max"),
                                cons=("consumption_id", "first"))
lots["item"] = lots["item"].astype(str).str.strip()

ag = pd.read_csv(os.path.join(HERE, "inputs", "aging_master.csv"))
unit = {"HOURS": 1.0, "HOUR": 1.0, "MINUTES": 1 / 60, "MINUTE": 1 / 60, "DAYS": 24.0, "DAY": 24.0}
min_age_h = dict(zip(ag["ItemCode"].astype(str).str.strip(),
                     pd.to_numeric(ag["MinAging"], errors="coerce").fillna(0)
                     * ag["MinAgingUnit"].astype(str).str.upper().str.strip().map(unit).fillna(1.0)))
rt = pd.read_csv(os.path.join(HERE, "inputs", "routing.csv"), low_memory=False,
                 usecols=["routed_product", "transfer_time_min"])
xfer_min = pd.to_numeric(rt["transfer_time_min"], errors="coerce").groupby(
    rt["routed_product"].astype(str).str.strip()).max().fillna(0).to_dict()


def usable(lid):
    r = lots.loc[lid]
    return r["end"] + pd.Timedelta(hours=min_age_h.get(r["item"], 0.0)) \
        + pd.Timedelta(minutes=xfer_min.get(r["item"], 0.0))


def inputs(lid):
    c = lots.at[lid, "cons"]
    return [x for x in str(c).split(";") if x and x != "nan" and x in lots.index] if pd.notna(c) else []


TOL = pd.Timedelta(minutes=30)


def binding(lid):
    """(input lot, usable time) that held `lid`: the latest input usable BEFORE lid started.
    A linked producer usable only AFTER the start cannot have held it -- phase3 allocates the
    GROSS need to producers without knowing opening stock, so that part came from stock."""
    st = lots.at[lid, "start"]
    u = {x: usable(x) for x in inputs(lid)}
    u = {x: t for x, t in u.items() if t <= st + pd.Timedelta(minutes=5)}
    if not u:
        return None, None
    b = max(u, key=u.get)
    return (b, u[b]) if u[b] >= st - TOL else (None, None)


def chain(lid, depth=0):
    """follow the input that held each lot up the chain to its root."""
    out = []
    while depth < 15:
        b, _ = binding(lid)
        if b is None:
            break
        r = lots.loc[b]
        out.append(f"{r['item']} [{r['itype']}] on {r['machine']} {r['start']:%d-%b %H:%M}->{r['end']:%d-%b %H:%M}"
                   f" +{min_age_h.get(r['item'], 0):g}h age")
        lid, depth = b, depth + 1
    return out, lid


# plan slot per build lot: i-th BLD lot on (TBM, GT) in time order <-> i-th plan row
bp = pd.read_csv(os.path.join(HERE, "inputs", "building_plan.csv"), parse_dates=["startTime"])
bp["k"] = list(zip(bp["buildMachine"].astype(str).str.strip(), bp["gtCode"].astype(str).str.strip()))
bld = lots[lots["dept"].astype(str).str.upper() == "BUILDING"].copy()
bld["k"] = list(zip(bld["machine"].astype(str).str.strip(), bld["item"]))
plan = {}
for k, g in bp.sort_values("startTime").groupby("k"):
    plan[k] = list(g["startTime"])
bld = bld.sort_values("start")
seen = Counter()
bld["plan_start"] = pd.NaT
for lid, r in bld.iterrows():
    ps = plan.get(r["k"], [])
    i = seen[r["k"]]; seen[r["k"]] += 1
    bld.at[lid, "plan_start"] = ps[i] if i < len(ps) else pd.NaT

rows = []
prev_end = {}
for lid, r in bld.sort_values("start").iterrows():
    m = r["machine"]
    tbm_free = prev_end.get(m)
    prev_end[m] = r["end"]
    if pd.isna(r["plan_start"]) or r["plan_start"] >= UNTIL:
        continue
    comps = {x: usable(x) for x in inputs(lid)}
    bind, kit_ready = binding(lid)
    late_links = sum(1 for t in comps.values() if t > r["start"] + pd.Timedelta(minutes=5))
    delay_h = (r["start"] - r["plan_start"]) / H
    if bind is not None:
        cause = "KIT (component)"
    elif tbm_free is not None and tbm_free >= r["start"] - pd.Timedelta(minutes=5):
        cause = "TBM busy (previous build)"
    elif delay_h <= 0.5:
        cause = "on time"
    else:
        cause = "other (no producer lot at the start: stock aging / kit gate)"
    ch, root = chain(bind) if (bind is not None and cause.startswith("KIT")) else ([], None)
    br = lots.loc[bind] if bind is not None else None
    rows.append({
        "build_lot": lid, "tbm": m, "green_tyre": r["item"], "plan_start": r["plan_start"],
        "actual_start": r["start"], "delay_h": round(delay_h, 1), "cause": cause,
        "kit_ready": kit_ready,
        "binding_component": None if br is None else br["item"],
        "binding_type": None if br is None else br["itype"],
        "binding_machine": None if br is None else br["machine"],
        "binding_finish": None if br is None else br["end"],
        "binding_min_age_h": None if br is None else min_age_h.get(br["item"], 0.0),
        "chain_up": " <- ".join(ch),
        "root_lot": None if root is None else lots.at[root, "item"],
        "root_type": None if root is None else lots.at[root, "itype"],
        "root_machine": None if root is None else lots.at[root, "machine"],
        "root_start": None if root is None else lots.at[root, "start"],
        "n_components": len(comps), "links_usable_after_start": late_links})

out = pd.DataFrame(rows).sort_values("plan_start")
out.to_csv(os.path.join(RUN, "BUILD_DELAY_TRACE.csv"), index=False)
print(f"{len(out)} build blocks planned before {UNTIL}: wrote {os.path.join(RUN, 'BUILD_DELAY_TRACE.csv')}")
print("\ncause:", out["cause"].value_counts().to_dict())
k = out[out["cause"].str.startswith("KIT")]
print(f"\nKIT-bound ({len(k)}): binding component TYPE:", k["binding_type"].value_counts().to_dict())
print("binding component ITEM (top):", k["binding_component"].value_counts().head(10).to_dict())
print("ROOT of the chain (type @ machine):",
      (k["root_type"].astype(str) + " @ " + k["root_machine"].astype(str)).value_counts().head(10).to_dict())
print(f"delay h: median {out['delay_h'].median():.1f}, max {out['delay_h'].max():.1f}")
