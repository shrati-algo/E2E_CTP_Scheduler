#!/usr/bin/env python3
"""phase0_validate.py — the EFFECTIVE plan: only what the masters can actually build.

CTP counterpart of v10's `data_validation.py`, rewritten because v10's entire
test is "does the routing row carry all_machine_spec" and at CTP that column is
empty on all 46,816 rows (CTP_IMPLEMENTATION.md §6.1 / §8.1).

THE TEST, per (SKU, Equipment) in the building plan
  1. walk jkt_bom_{line} from Super_parent = SKU, stopping at items the plant
     does not make (purchased raw material is contracted out)
  2. every item found resolves to a routed_product in jkt_routing_{line}
  3. that row carries a non-empty `machines`
  4. that row carries a non-empty `proc_time` + `proc_time_UOM`
  5. every machine token resolves against MasterMachines (recorded, not fatal --
     an unregistered work centre is a data gap, not a reason to delete the work)
  BUILDING and CURING are exempt from 4: their times come from the plan.

A component failing 2, 3 or 4 disqualifies the whole (SKU, group): a green tyre
cannot be built without all of its parts. The plan is rewritten to the SKUs that
survive, and the shortfall is stated in tyres up front.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import sys

import pandas as pd

import blocker_policy as BP
import blockers as BLK
import ctpio as io


def _sku_status(line, rows, findings, plan, child_idx, routed, eq, alias) -> pd.DataFrame:
    """CLEAN / ASSUMED / DATA_BLOCKED per SKU, for the building plan AND for
    SKUs the curing plan names that the building plan does not."""
    iss = defaultdict(set)
    for f in findings:
        iss[f["sku"]].add(f["issue"])
    out = []
    for r in rows:
        sku = r["sku"]
        rec = {"line": line.upper(), "sku": sku, "source": "building_plan",
               "build_tyres": r["qty"], "state": BP.CLEAN, "blocker_code": "",
               "missing_table": "", "missing_key": "", "assumption_codes": "", "note": ""}
        if r.get("ok"):
            pass
        elif r.get("ok_assumed"):
            rec["state"] = BP.ASSUMED
            rec["assumption_codes"] = ",".join(sorted(
                {BP.ISSUE_TO_ASSUMPTION[i] for i in iss[sku] if i in BP.ISSUE_TO_ASSUMPTION}))
        else:
            hit = [i for i in ("no_bom", "no_building_routing") if i in iss[sku]]
            code = (BP.ISSUE_TO_BLOCKER[hit[0]] if hit else
                    "B06" if "no_machine_speed" in iss[sku] else "B05")
            rec.update(state=BP.BLOCKED, blocker_code=code,
                       missing_table=BP.BLOCKERS[code]["table"],
                       missing_key=(sku if hit else
                                    ";".join(map(str, r.get("unresolved", [])[:8]))),
                       note=BP.BLOCKERS[code]["what"])
        out.append(rec)
    # SKUs the curing plan cures but the building plan never builds
    try:
        cure, _ = io.load_curing_plan(line, alias)
        for sku in sorted(set(cure["sku"]) - set(plan["sku"])):
            rec = {"line": line.upper(), "sku": sku, "source": "curing_plan_only",
                   "build_tyres": 0.0, "state": BP.CLEAN, "blocker_code": "",
                   "missing_table": "", "missing_key": "", "assumption_codes": "",
                   "note": "no building-plan row: can only cure from GT stock"}
            code = ("B01" if (sku, eq) not in child_idx else
                    "B02" if not routed.get((sku, eq, "GT BUILDING")) else "")
            if code:
                rec.update(state=BP.BLOCKED, blocker_code=code,
                           missing_table=BP.BLOCKERS[code]["table"], missing_key=sku,
                           note=BP.BLOCKERS[code]["what"])
            out.append(rec)
    except FileNotFoundError:
        pass
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v1")
    args = ap.parse_args()
    OUT = io.outdir(args.run, args.line)
    BLK.start(OUT)          # a fresh run inherits no old blockers

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 0 — effective plan   [{args.line.upper()}]")
    print("=" * 74)

    mach = io.load_machines()
    alias = io.machine_alias(mach)
    bom = io.load_bom(args.line)
    rt = io.load_routing(args.line)
    meta = io.build_routing_meta(rt, alias)
    routed = io.routed_product_index(rt)
    sched = io.schedulable_items(rt)
    itypes = io.load_itemtypes(bom)
    child_idx = io.build_child_index(bom)
    plan, pstats = io.load_building_plan(args.line, alias)

    print(f"  BOM rows            : {len(bom):,}   routing rows: {len(rt):,}")
    print(f"  Machines registered : {len(mach):,}   alias spellings: {len(alias):,}")
    print(f"  Plan source         : {pstats['source']}")
    print(f"  Plan rows           : {pstats['rows_raw']:,} raw -> {len(plan):,} production"
          f"   (dropped {pstats['zero_qty']} zero-qty, {pstats['bad_time']} unparseable)")
    print(f"  GT_Code on the row  : {pstats['with_gt_code']:,} of {pstats['rows_raw']:,}"
          f"   (the built item never has to be inferred from the routing)")
    print(f"  Plan machine ids    : {pstats['unresolved_machine']} unresolved"
          f"  ({'OK' if not pstats['unresolved_machine'] else 'GAP'})")
    print(f"  Plan window         : {plan['build_start'].min()}  ->  {plan['build_end'].max()}")
    print(f"  Plan SKUs / tyres   : {plan['sku'].nunique()} / {plan['qty'].sum():,.0f}")

    eq = bom["Equipment"].iloc[0]
    cache = io.WalkCache(child_idx, sched, io.bias_items(rt))
    rows, findings = [], []
    for sku, grp in plan.groupby("sku"):
        qty = float(grp["qty"].sum())
        gt = (str(grp["gt_code"].iloc[0]).strip()
              or routed.get((sku, eq, "GT BUILDING")))
        if gt and gt not in child_idx.get((sku, eq), {}) and                 routed.get((sku, eq, "GT BUILDING")):
            gt = routed.get((sku, eq, "GT BUILDING"))
        if not gt:
            rows.append({"sku": sku, "qty": qty, "ok": False,
                         "reason": "no GT BUILDING routing row"})
            findings.append({"sku": sku, "item": "", "issue": "no_building_routing",
                             "detail": f"({sku},{eq},GT BUILDING)", "tyres": qty})
            continue
        if (sku, eq) not in child_idx:
            rows.append({"sku": sku, "qty": qty, "ok": False, "reason": "no BOM tree"})
            findings.append({"sku": sku, "item": "", "issue": "no_bom",
                             "detail": f"({sku},{eq})", "tyres": qty})
            continue
        nodes, _, _ = cache.get(sku, eq, gt, frozenset())
        bad = []
        for it in nodes:
            m = meta.get(it)
            if m is None:
                bad.append((it, "no_routing"))
                continue
            if not m["machines"]:
                bad.append((it, "no_machines"))
            elif m["proc_time"] is None or not m["proc_time_uom"]:
                if str(m["operation"]).upper() not in ("GT BUILDING", "CURING"):
                    bad.append((it, "no_machine_speed" if m.get("speed_blocked")
                                else "no_proc_time"))
        for it, why in bad:
            findings.append({"sku": sku, "item": it, "issue": why,
                             "detail": (meta.get(it) or {}).get("department", ""),
                             "tyres": qty})
        rows.append({"sku": sku, "qty": qty, "gt_item": gt, "components": len(nodes),
                     "ok": not bad, "reason": "" if not bad else
                     f"{len(bad)} component(s): " + ", ".join(f"{i}[{w}]" for i, w in bad[:4])})

    # ---- the same test again, with the gaps closed by named assumptions ----
    meta2 = io.apply_assumptions(io.build_routing_meta(rt, alias))
    rows2 = []
    for r in rows:
        if r.get("ok"):
            rows2.append({**r, "ok_assumed": True})
            continue
        if not r.get("gt_item"):
            rows2.append({**r, "ok_assumed": False})
            continue
        nodes, _, _ = cache.get(r["sku"], eq, r["gt_item"], frozenset())
        bad = [it for it in nodes
               if meta2.get(it) is None or not meta2[it]["machines"]
               or (meta2[it]["proc_time"] is None
                   and str(meta2[it]["operation"]).upper() not in ("GT BUILDING", "CURING"))]
        rows2.append({**r, "ok_assumed": not bad, "unresolved": bad})
    rows = rows2

    # ---- v39 DATA-BLOCKER POLICY: one state per SKU (blocker_policy.py) ------
    status = _sku_status(args.line, rows, findings, plan, child_idx, routed, eq, alias)
    status.to_csv(OUT / BP.STATUS_FILE, index=False)
    for st, g in status.groupby("state"):
        print(f"  POLICY {st:13s}: {len(g):>3d} SKU(s)  {g['build_tyres'].sum():>9,.0f} "
              f"building tyres" + ("   <- excluded from final figures"
                                   if st == BP.BLOCKED else ""))
    for r in status[status["state"] == BP.BLOCKED].itertuples(index=False):
        print(f"    {r.blocker_code} {r.sku:22s} {r.missing_table}  key={r.missing_key}")

    eff = pd.DataFrame(rows)
    fnd = pd.DataFrame(findings)
    asm = pd.DataFrame(io.ASSUMPTIONS)
    ok = eff[eff["ok"]]
    oka = eff[eff["ok_assumed"]]
    tot = float(eff["qty"].sum())
    print(f"\n  Walk cache          : {cache.stats()}")
    print(f"  SKUs planned        : {len(eff)}")
    print(f"  STRICT (data as-is) : {len(ok)} SKU(s), {ok['qty'].sum():,.0f} of "
          f"{tot:,.0f} tyres  ({100*ok['qty'].sum()/max(tot,1):.1f}%)   <- v10's own test")
    print(f"  ASSUMED (gaps named): {len(oka)} SKU(s), {oka['qty'].sum():,.0f} of "
          f"{tot:,.0f} tyres  ({100*oka['qty'].sum()/max(tot,1):.1f}%)"
          f"   <- {len(asm)} assumption(s)")
    if not fnd.empty:
        print(f"\n  Blocking components by issue:")
        for k, v in fnd.groupby("issue").agg(items=("item", "nunique"),
                                             skus=("sku", "nunique")).iterrows():
            print(f"    {k:18s} items={int(v['items']):>4d}  skus_blocked={int(v['skus']):>3d}")

    if io.UNREGISTERED:
        print(f"\n  [GAP] {len(io.UNREGISTERED)} machine token(s) named by the routing "
              f"are NOT in MasterMachines — kept as pseudo-machines:")
        for t, n in sorted(io.UNREGISTERED.items(), key=lambda kv: -kv[1])[:12]:
            print(f"      {t:14s} referenced {n:,} time(s)")

    if not asm.empty:
        print(f"\n  ASSUMPTIONS MADE ({len(asm)}) — every one is recorded, none is hidden:")
        for k, v in asm.groupby(["code", "gap"]).agg(
                items=("item", "nunique"), depts=("department", "nunique")).iterrows():
            print(f"    {k[0]}  {k[1]:12s} {int(v['items']):>4d} item(s) across "
                  f"{int(v['depts'])} department(s)")
        for r in asm.head(8).itertuples(index=False):
            print(f"      {r.code} {str(r.item)[:16]:17s} {str(r.department)[:20]:21s}"
                  f" {str(r.value)[:28]:29s} {str(r.source)[:44]}")

    eff.to_csv(OUT / "phase0_effective_plan.csv", index=False)
    fnd.to_csv(OUT / "phase0_findings.csv", index=False)
    asm.to_csv(OUT / "phase0_assumptions.csv", index=False)
    plan[plan["sku"].isin(set(oka["sku"]))].to_csv(OUT / "phase0_plan_effective.csv",
                                                   index=False)
    plan.to_csv(OUT / "phase0_plan_full.csv", index=False)
    io.write_json(OUT / "phase0_stats.json", {
        "line": args.line, "plan": pstats,
        "skus_planned": len(eff),
        "skus_strict": int(len(ok)), "skus_assumed": int(len(oka)),
        "tyres_planned": tot,
        "tyres_strict": float(ok["qty"].sum()),
        "tyres_assumed": float(oka["qty"].sum()),
        "n_assumptions": int(len(asm)),
        "unregistered_machines": io.UNREGISTERED,
        "plan_start": str(plan["build_start"].min()),
        "plan_end": str(plan["build_end"].max()),
    })
    print(f"\n[OK] phase0_effective_plan.csv ({len(eff)})  phase0_findings.csv ({len(fnd)})"
          f"  phase0_assumptions.csv ({len(asm)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
