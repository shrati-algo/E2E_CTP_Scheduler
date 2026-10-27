#!/usr/bin/env python3
"""phase1_building_demand.py — the frozen building plan -> component demand.

v10 phase 1, on CTP data. Differences, all from CTP_IMPLEMENTATION.md §6.2:

  * BUILD_OPS = ("GT BUILDING",). ONE anchor per tyre, both lines: PCR has no
    carcass node in the BOM and TBR's equivalent (TBR PRE ASSEMBLY, machine
    2201) is an ordinary upstream operation, not a second building row. So the
    anchor stop-set is empty and there is nothing to double-count.
  * the plan's machineId is in the StageN namespace and is normalised upstream.
  * the changeover is COLUMNS on the production row, not a separate CHANGEOVER
    row, so v10's setup-carry collapses to reading the span.
  * need_by = build_start - min_aging(item) - buffer_hours(item_type), the
    buffer taken from jkt_buffer_master -- CTP states it, BTP tunes it.

TROLLEY SPLIT IS OFF BY DEFAULT AND THAT IS A STATED LIMITATION.
  BTP splits a plan row into 16-NOS trolleys because its MPQ sheet says so, and
  that is what sharpens need_by from "some time in this 2-hour row" to "this
  minute". jkt_mpq_{line} has no Green Tyres row at all, so there is no CTP
  number to use and inventing 16 would be inventing a plant rule. --build-mpq
  turns it on the day the planner states one.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict

import pandas as pd

import blockers as BLK
import blocker_policy as BP
import ctpio as io
import rules as R


def split_trolleys(lot: dict, cap: float | None) -> list[dict]:
    """v10's split_building_lot: prorate the row's own span across trolleys."""
    qty = float(lot["qty"])
    if not cap or cap <= 0 or qty <= cap:
        return [lot]
    start, end = lot["build_start"], lot["build_end"]
    span = (end - start) if (pd.notna(end) and end > start) else None
    n = int(math.ceil(qty / cap - 1e-9))
    out, made = [], 0.0
    for i in range(n):
        take = min(cap, qty - made)
        if take <= 0:
            break
        s = start + span * (made / qty) if span is not None else start
        e = start + span * ((made + take) / qty) if span is not None else end
        out.append({**lot, "building_lot_id": f"{lot['building_lot_id']}_T{i+1:03d}",
                    "qty": take, "build_start": s, "build_end": e,
                    "trolley_index": i + 1, "trolley_count": n,
                    "setup_min": lot.get("setup_min", 0.0) if i == 0 else 0.0})
        made += take
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--build-mpq", type=float, default=0.0,
                    help="trolley size in NOS; 0 = no split (CTP states none)")
    args = ap.parse_args()
    OUT = io.outdir(args.run, args.line)

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 1 — building-anchored component demand   [{args.line.upper()}]")
    print("=" * 74)

    alias = io.machine_alias(io.load_machines())
    bom = io.load_bom(args.line)
    rt = io.load_routing(args.line)
    meta = io.apply_assumptions(io.build_routing_meta(rt, alias))
    routed = io.routed_product_index(rt)
    sched = io.schedulable_items(rt)
    itypes = io.load_itemtypes(bom)
    child_idx = io.build_child_index(bom)
    buffer_h = io.load_buffer(args.line, bom, rt)
    aging = io.load_aging(args.line)
    aging, filled = io.apply_aging_defaults(aging, set(itypes), itypes)

    plan = pd.read_csv(OUT / "phase0_plan_effective.csv",
                       parse_dates=["build_start", "build_end"])
    # v46j: a SKU blocked for a missing machine speed (B06) cannot be timed, so
    # its trolleys and their upstream work are NOT scheduled - otherwise they
    # would take shared machines (901, mixers) from demand that can be made.
    # Its tyres stay in the plan figures as DATA_BLOCKED (phase 7 / validation).
    if str(R.rule("routing_speed.exclude_blocked_from_schedule", False)).lower() in (
            "1", "true", "on", "yes"):
        _st = pd.read_csv(OUT / BP.STATUS_FILE, dtype=str).fillna("")
        _b6 = set(_st.loc[(_st["state"] == BP.BLOCKED) & (_st["blocker_code"] == "B06"), "sku"])
        if _b6:
            _n = plan["sku"].isin(_b6)
            print(f"  B06 not scheduled   : {int(_n.sum()):,} plan row(s), "
                  f"{plan.loc[_n, 'qty'].sum():,.0f} tyres on {len(_b6)} SKU(s) - "
                  f"a component has no machine speed in all_machine_spec")
            plan = plan[~_n].reset_index(drop=True)
    eq = bom["Equipment"].iloc[0]
    print(f"  Equipment group     : {eq}   (one per line — no PLAN_MG_ALIAS needed)")
    print(f"  Plan rows           : {len(plan):,}   tyres {plan['qty'].sum():,.0f}")
    # EVERY buffer hour comes from jkt_buffer_master. The resolver only decides
    # WHICH stated row a BOM name belongs to, so print the derivation: an
    # inference nobody can audit is an invention with extra steps.
    n_res = len(io.BUFFER_RESOLVED)
    print(f"  Buffer master       : {len(buffer_h)} item type(s) carry a buffer"
          f"   ({n_res} resolved from the database, {len(io.BUFFER_ALIASED)} by rule-file alias)")
    for k, (how, src, h) in sorted(io.BUFFER_RESOLVED.items()):
        print(f"      {how:<7s} '{k}'  <-  master '{src}'  = {h:g} h")
    for k, (src, h) in sorted(io.BUFFER_ALIASED.items()):
        print(f"      ALIAS   '{k}'  <-  master '{src}'  = {h:g} h   (rule file)")
    gaps = {k: v for k, v in io.BUFFER_UNRESOLVED.items()
            if not v.startswith("purchased")}
    n_buy = len(io.BUFFER_UNRESOLVED) - len(gaps)
    if gaps:
        print(f"  [GAP] buffer        : {len(gaps)} produced item type(s) take 0 h "
              f"because the master states none ({n_buy} purchased types skipped)")
        for k, why in sorted(gaps.items()):
            print(f"      0 h     '{k}'  -  {why}")
    if filled:
        print(f"  [GAP] aging default : {len(filled)} compound(s) had NO max-aging row — "
              f"master {io.DEFAULT_MASTER_AGING_H:g} h / final {io.DEFAULT_FINAL_AGING_H:g} h applied")

    # ---- resolve every plan row to the item it actually builds -------------
    lots, unresolved = [], []
    for i, r in plan.iterrows():
        # THE PLAN CARRIES GT_Code ON EVERY ROW. Prefer it; the routing is the
        # fallback. v10 had to resolve the built item from the routing alone,
        # which is how an empty `machines` column deleted 342,393 BTP tyres.
        gt = str(r.get("gt_code") or "").strip()
        if not gt or gt not in child_idx.get((r["sku"], eq), {}):
            gt = routed.get((r["sku"], eq, "GT BUILDING")) or gt
        if not gt:
            unresolved.append({**r.to_dict(), "reason": "no_building_routing"})
            continue
        lots.append({
            # v42: LINE-PREFIXED - PCR and TBR both counted BLD000000.. (279
            # building lot ids shared by the two lines up to v41).
            "building_lot_id": f"{args.line.upper()}_BLD{i:06d}", "machine": r["machine"],
            "sku": r["sku"], "mg": eq, "operation": "GT BUILDING",
            "built_item": gt, "item_type": itypes.get(gt, "Green Tyres"),
            "qty": float(r["qty"]), "build_start": r["build_start"],
            "build_end": r["build_end"], "shift": r.get("shift", ""),
            "setup_min": float(r.get("setup_min", 0.0) or 0.0),
        })
    n_rows = len(lots)
    if args.build_mpq > 0:
        lots = [x for L in lots for x in split_trolleys(L, args.build_mpq)]
        print(f"  Trolley split       : {n_rows:,} plan rows -> {len(lots):,} trolleys "
              f"(MPQ {args.build_mpq:g} NOS)")
    else:
        print(f"  Trolley split       : OFF — jkt_mpq_{args.line} states no Green Tyres "
              f"trolley. need_by is only as sharp as a whole plan row "
              f"(median {(plan['build_end']-plan['build_start']).dt.total_seconds().median()/60:.0f} min)")

    lots_df = pd.DataFrame(lots)
    print(f"  Building lots       : {len(lots_df):,}   unresolved: {len(unresolved):,}")

    # ---- explode each building lot into component demand ------------------
    cache = io.WalkCache(child_idx, sched, io.bias_items(rt))
    rows = []
    no_bom = 0
    for L in lots:
        if (L["sku"], eq) not in child_idx:
            no_bom += 1
            continue
        # ONE anchor -> nothing to stop at. (BTP stops at the other building row.)
        nodes, _, _ = cache.get(L["sku"], eq, L["built_item"], frozenset())
        for item, m in nodes.items():
            mn = float(aging.get(item, (0.0, 0.0))[0])
            itype = str(m["item_type"] or itypes.get(item, ""))
            buf = float(buffer_h.get(itype.strip().lower(), 0.0))
            rows.append({
                "item_code": item, "item_type": itype, "uom": m["uom"],
                "building_lot_id": L["building_lot_id"], "sku": L["sku"],
                "built_item": L["built_item"], "machine": L["machine"],
                "department": (meta.get(item) or {}).get("department", ""),
                "operation": (meta.get(item) or {}).get("operation", ""),
                "per_tyre_qty": m["per_tyre"],
                "demand_qty": m["per_tyre"] * L["qty"],
                "build_start": L["build_start"],
                "min_aging_h": mn, "buffer_h": buf,
                "need_by": L["build_start"] - pd.Timedelta(hours=mn + buf),
            })
    dem = pd.DataFrame(rows)
    print(f"  Component demand    : {len(dem):,} rows, {dem['item_code'].nunique():,} "
          f"items   [walk cache: {cache.stats()}]")
    if no_bom:
        print(f"  [WARN] building lots with no BOM tree: {no_bom:,}")

    print(f"\n  Demand by department:")
    g = (dem.groupby("department").agg(items=("item_code", "nunique"),
                                       rows=("item_code", "size"),
                                       qty=("demand_qty", "sum"))
         .sort_values("rows", ascending=False))
    for d, r in g.iterrows():
        print(f"    {str(d)[:30]:31s} items={int(r['items']):>4d}  rows={int(r['rows']):>8,d}"
              f"  qty={r['qty']:>16,.0f}")
    print(f"\n  Need-by window      : {dem['need_by'].min()}  ->  {dem['need_by'].max()}")
    print(f"  Build window (frozen): {lots_df['build_start'].min()}  ->  "
          f"{lots_df['build_start'].max()}")

    lots_df.to_csv(OUT / "phase1_building_lots.csv", index=False)
    dem.to_csv(OUT / "phase1_demand.csv", index=False)
    if unresolved:
        pd.DataFrame(unresolved).to_csv(OUT / "phase1_unresolved.csv", index=False)
    # Any component the BOM demands whose ITEM TYPE the buffer master does not
    # name is already registered by load_buffer. Here we price it: how many
    # demand rows, and how much quantity, are carrying a 0 h buffer because of it.
    gapped = {k for k, v in io.BUFFER_UNRESOLVED.items()
              if not v.startswith("purchased")}
    if gapped:
        g = dem[dem["item_type"].astype(str).str.strip().str.lower().isin(gapped)]
        if len(g):
            BLK.add("phase1_demand", "MISSING_IN_DB",
                    "JK_CTP_PPDS.CTP_Master_Buffer",
                    ", ".join(sorted(gapped)[:6]) + (" ..." if len(gapped) > 6 else ""),
                    f"{len(gapped)} produced item type(s) have no buffer row, so "
                    f"{len(g):,} demand row(s) took 0 h",
                    line=args.line.upper(),
                    referenced_at="phase1_building_demand.py need_by",
                    qty=float(g["demand_qty"].sum()), uom="h",
                    impact="need_by for these sits at build_start - min_aging, so "
                           "they are aimed later than the plant intends")
    # THE PLANT'S OWN PURCHASED LIST, AND WHERE IT CONTRADICTS THE ROUTING.
    # A declared purchased code that the routing also routes is a disagreement
    # between two plant sources. The declaration wins - it is the more specific
    # statement - and the contradiction is registered so it can be fixed at
    # source rather than re-decided every run.
    _dec = io.declared_purchased()
    if _dec:
        _routed = {r for r in rt["routed_product"].map(io._s) if r}
        for _c, _why in sorted(_dec.items()):
            if _c in _routed:
                BLK.add("phase1_demand", "NAME_MISMATCH",
                        "CTP Data Gaps Report (2).xlsx + jkt_routing_" + args.line,
                        _c, f"the plant states '{_why}' but jkt_routing_{args.line} "
                            f"routes it as if the plant made it",
                        line=args.line.upper(),
                        referenced_at="ctpio.schedulable_items",
                        impact="treated as PURCHASED (the declaration wins); it is "
                               "never scheduled and never gates a kit. FIX: remove "
                               "the routing row, or correct the remark")
        print(f"  Purchased list      : {len(_dec)} code(s) declared PURCHASED by the "
              f"plant; {sum(1 for c in _dec if c in _routed)} of them still carry a "
              f"routing row on this line (the declaration wins)")

    # ---- OBSOLETE / SIZE-OUT: no honest default exists --------------------
    # A discontinued code can be neither bought nor made, so it gets NO
    # assumption. The only correct behaviour is to find out whether this plan
    # actually needs one, and say so. Three ways it could:
    #   * the plan builds an obsolete SKU outright
    #   * a live SKU's BOM still lists an obsolete component
    #   * an obsolete code reached component demand
    _obs = io.declared_obsolete()
    if _obs:
        _hit_sku = sorted(set(plan["sku"].map(io._s)) & set(_obs))
        _hit_gt = sorted({io._s(x) for x in plan.get("gt_code", [])} & set(_obs))
        _hit_dem = sorted(set(dem["item_code"].map(io._s)) & set(_obs)) if len(dem) else []
        for _c in _hit_sku:
            BLK.add("phase1_demand", "MISSING_IN_DB",
                    "CTP Data Gaps Report (2).xlsx", _c,
                    f"the plan builds this SKU but the plant calls it "
                    f"'{_obs[_c]}' - it is discontinued",
                    line=args.line.upper(),
                    referenced_at="phase1_building_demand.py plan",
                    impact="no substitute is assumed; the plan asks for something "
                           "the plant does not run any more")
        for _c in _hit_dem:
            _q = float(dem.loc[dem["item_code"].map(io._s) == _c, "demand_qty"].sum())
            BLK.add("phase1_demand", "MISSING_IN_DB",
                    "CTP Data Gaps Report (2).xlsx", _c,
                    f"a live SKU's BOM demands this component, but the plant "
                    f"calls it '{_obs[_c]}'",
                    line=args.line.upper(), qty=_q,
                    referenced_at="phase1_building_demand.py BOM walk",
                    impact="it can be neither bought nor made; nothing is "
                           "substituted for it")
        _n = len(_hit_sku) + len(_hit_gt) + len(_hit_dem)
        print(f"  Obsolete list       : {len(_obs)} code(s) declared obsolete/size-out"
              + (f"   [{_n} REACHED THIS PLAN - see DATA_BLOCKERS.csv]" if _n else
                 "   — none reached this plan, so none is assumed available"))
    if io.BYPASSED:
        _bd = {}
        for _c, _d in io.BYPASSED.items():
            _bd.setdefault(_d, []).append(_c)
        print(f"  Bypassed (v33)      : {len(io.BYPASSED)} code(s) made in "
              f"{len(_bd)} cutter/slitter department(s) are not scheduled - their "
              f"inputs feed building directly: "
              + ", ".join(f"{d} {len(v)}" for d, v in sorted(_bd.items())))
    BLK.write(OUT)
    print(f"\n[OK] phase1_building_lots.csv ({len(lots_df):,})  "
          f"phase1_demand.csv ({len(dem):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
