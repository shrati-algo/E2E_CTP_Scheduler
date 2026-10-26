#!/usr/bin/env python3
"""make_wip_vs_day1_report.py - is what the first plan day needs on the rack?

    python make_wip_vs_day1_report.py --run e2e_v45b [--hours 24]

Compares the opening-stock snapshot (exactly as phase 2 reads it) with what the
building plan needs for the trolleys planned in the first `--hours` (default: plant
day 1, 07:00 -> 07:00), and the day-1 curing plan with the green-tyre stock.

  Kit level   the parts a builder loads (BOM children of the green tyre, with the
              bypassed cutter/slitter codes kept as parts - the physical kit)
  Full tree   every item under those trolleys (compounds, calendered rolls, ...)

For each item: requirement (BOM unit), stock in the snapshot (stock unit, and
converted to the BOM unit where the BOM licenses it), stock usable for day 1 (alive
at 07:00 and rested before the trolley that needs it), coverage, and what the
schedule actually drew for the day-1 trolleys (phase2_wip_allocations).
Coverage is per item, ignoring competition between trolleys; the trolley sheet uses
the schedule's real allocation.

Output: outputs/<run>/WIP_vs_DAY1_PLAN_<run>.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io
import phase2_wip_netting as P2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--hours", type=float, default=24.0)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    kit_rows, tree_rows, trol_rows, gt_rows, snap = [], [], [], [], []
    for ln in ("pcr", "tbr"):
        O = D / ln
        bl = pd.read_csv(O / "phase1_building_lots.csv", parse_dates=["build_start", "build_end"])
        opening = bl["build_start"].min()
        cut = opening + pd.Timedelta(hours=a.hours)
        d1 = bl[bl["build_start"] < cut].copy()
        bom = io.load_bom(ln)
        rt = io.load_routing(ln)
        sched = io.schedulable_items(rt)                 # fills io.BYPASSED
        itypes = io.load_itemtypes(bom)
        aging = io.load_aging(ln)
        aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)
        child_idx = io.build_child_index(bom)
        eq = bom["Equipment"].iloc[0]
        dept = {}
        for p_, d_ in zip(rt["routed_product"], rt["department"]):
            if p_ and p_ not in dept and io._s(d_):
                dept[p_] = io._s(d_)
        wip = io.load_wip(ln)
        pool, st = P2.build_pool(wip, aging, opening)
        dem = pd.read_csv(O / "phase1_demand.csv", parse_dates=["build_start", "need_by"])
        bunits = {}
        for c, u in zip(bom["child"].map(io._s), bom["child_Unit"].map(io._s)):
            if c and u:
                bunits.setdefault(c, u)
        factor_of, _bad = P2.unit_bridge(wip, dem, rt, bom, bunits)
        su = dict(zip(wip["itemcode"].map(io._s), wip["unit"].map(io._s)))
        snap.append({"line": ln.upper(), "rows": len(wip), "batches_alive_at_open": sum(len(v) for v in pool.values()),
                     "dead_at_open": st["dead_at_open"], "dead_qty": round(st["dead_qty"], 1),
                     "day1_trolleys": len(d1), "day1_tyres": float(d1["qty"].sum()),
                     "day1_window": f"{opening} -> {cut}"})

        walk = io.WalkCache(child_idx, set(sched) | set(io.BYPASSED), io.bias_items(rt))
        need = defaultdict(float)         # item -> qty (BOM unit)
        need_stock = defaultdict(float)   # item -> qty (stock unit)
        first_use = {}
        per_trolley = []
        for L in d1.itertuples(index=False):
            if (L.sku, eq) not in child_idx:
                continue
            nodes, edges, roots = walk.get(L.sku, eq, L.built_item, frozenset())
            for it, per in roots.items():
                if per <= 0 or str(itypes.get(it, "")).strip().lower() in P2.ANCHOR_TYPES:
                    continue
                q = per * float(L.qty)
                need[it] += q
                f = factor_of(L.sku, it) if it in pool else 1.0
                need_stock[it] += q * (f or 1.0)
                first_use[it] = min(first_use.get(it, L.build_start), L.build_start)
                per_trolley.append((L.building_lot_id, L.sku, L.build_start, it, q))
        alloc = pd.read_csv(O / "phase2_wip_allocations.csv")
        d1_ids = set(d1["building_lot_id"])
        drawn_d1 = alloc[alloc["building_lot_id"].isin(d1_ids)].groupby("item_code")["qty_used"].sum()

        def stock_view(it, use_by):
            bs = pool.get(it, [])
            tot = sum(b["opening"] for b in bs)
            usable = sum(b["opening"] for b in bs if b["ready"] <= use_by)
            return tot, usable

        raw = wip.assign(_it=wip["itemcode"].map(io._s)).groupby("_it")["inventory"].sum()
        for it, q in sorted(need.items()):
            tot, usable = stock_view(it, first_use[it] if it in first_use else cut)
            qs = need_stock[it]
            cov = min(1.0, usable / qs) if qs > 0 else 0.0
            kit_rows.append({
                "line": ln.upper(), "item": it, "item_type": itypes.get(it, ""),
                "department": dept.get(it, io.BYPASSED.get(it, "(not routed / purchased)")),
                "bypassed_cut_code": it in io.BYPASSED, "bom_unit": bunits.get(it, ""),
                "stock_unit": su.get(it, ""), "day1_requirement_bom_unit": round(q, 2),
                "day1_requirement_stock_unit": round(qs, 2),
                "stock_in_snapshot": round(float(raw.get(it, 0.0)), 2),
                "stock_alive_at_07:00": round(tot, 2), "stock_usable_for_day1": round(usable, 2),
                "coverage_pct": round(100 * cov, 1),
                "status": ("FULLY COVERED" if cov >= 0.999 else "PARTLY COVERED" if usable > 0
                           else ("STOCK NOT YET RESTED / DEAD" if raw.get(it, 0) > 0 else "NO STOCK")),
                "shortage_stock_unit": round(max(0.0, qs - usable), 2),
                "drawn_by_schedule_for_day1": round(float(drawn_d1.get(it, 0.0)), 2),
                "first_trolley_needing_it": first_use.get(it)})

        # full tree: every item under the day-1 trolleys (gross, phase-1 demand)
        dd = dem[dem["building_lot_id"].isin(d1_ids)]
        g = dd.groupby("item_code").agg(day1_gross=("demand_qty", "sum"), uom=("uom", "first"),
                                         item_type=("item_type", "first"),
                                         first_need=("need_by", "min")).reset_index()
        for r in g.itertuples(index=False):
            tot, usable = stock_view(r.item_code, r.first_need + pd.Timedelta(
                hours=float(aging.get(r.item_code, (0.0, 0.0))[0] or 0.0)))
            tree_rows.append({"line": ln.upper(), "item": r.item_code, "item_type": r.item_type,
                              "department": dept.get(r.item_code, ""), "bom_unit": r.uom,
                              "stock_unit": su.get(r.item_code, ""),
                              "day1_gross_requirement": round(r.day1_gross, 2),
                              "stock_in_snapshot": round(float(raw.get(r.item_code, 0.0)), 2),
                              "stock_alive_at_07:00": round(tot, 2),
                              "drawn_by_schedule_for_day1": round(float(drawn_d1.get(r.item_code, 0.0)), 2),
                              "drawn_share_pct": round(100 * float(drawn_d1.get(r.item_code, 0.0)) /
                                                       r.day1_gross, 1) if r.day1_gross else None})

        # trolley kits with the schedule's real allocation
        dr = alloc[alloc["building_lot_id"].isin(d1_ids)].groupby(["building_lot_id", "item_code"])["qty_used"].sum().to_dict()
        tdf = pd.DataFrame(per_trolley, columns=["building_lot_id", "sku", "build_start", "item", "req"])
        for (bid, sku, bs), gg in tdf.groupby(["building_lot_id", "sku", "build_start"]):
            miss = []
            for r in gg.itertuples(index=False):
                f = factor_of(sku, r.item) if r.item in pool else 1.0
                if dr.get((bid, r.item), 0.0) + 1e-6 < r.req * (f or 1.0) * 0.999:
                    miss.append(r.item)
            trol_rows.append({"line": ln.upper(), "trolley": bid, "sku": sku, "plan_start": bs,
                              "kit_parts": len(gg), "parts_fully_from_stock": len(gg) - len(miss),
                              "parts_to_make": len(miss), "kit_complete_from_stock": not miss,
                              "parts_missing": ", ".join(sorted(miss))})

        # green tyres: day-1 curing plan vs GT stock
        cp = pd.read_csv(O / "phase7_curing_plan.csv", parse_dates=["planned_start"])
        gti = io._rd(f"gt_inventory_manual_{ln}")
        gstock = dict(zip(gti["SKUCode"].map(io._s), pd.to_numeric(gti["TotalQuantity"], errors="coerce")))
        cure, _ = io.load_curing_plan(ln, io.machine_alias(io.load_machines()))
        gt_of = cure.assign(gt=cure["gt_code"].map(io._s)).groupby("sku")["gt"].first().to_dict()
        gt_of.update(dict(zip(bl["sku"], bl["built_item"])))
        c1 = cp[cp["planned_start"] < cut].assign(gt=lambda x: x["sku"].map(gt_of))
        for gt, gg in c1.groupby("gt"):
            need_t = float(gg["qty_planned"].sum())
            s_ = float(gstock.get(gt, 0) or 0)
            gt_rows.append({"line": ln.upper(), "green_tyre": gt, "skus": gg["sku"].nunique(),
                            "day1_cure_plan_tyres": need_t, "gt_stock": s_,
                            "coverage_pct": round(100 * min(1.0, s_ / need_t), 1) if need_t else None,
                            "status": "FULLY COVERED" if s_ >= need_t else ("PARTLY COVERED" if s_ > 0 else "NO STOCK")})

    K = pd.DataFrame(kit_rows)
    T = pd.DataFrame(tree_rows)
    TR = pd.DataFrame(trol_rows)
    GT = pd.DataFrame(gt_rows)
    S = []
    for ln in ("PCR", "TBR"):
        k = K[K["line"] == ln]
        t = TR[TR["line"] == ln]
        g = GT[GT["line"] == ln]
        S.append({"line": ln, "kit_items_needed_day1": len(k),
                  "fully_covered": int((k["status"] == "FULLY COVERED").sum()),
                  "partly_covered": int((k["status"] == "PARTLY COVERED").sum()),
                  "stock_not_rested_or_dead": int((k["status"] == "STOCK NOT YET RESTED / DEAD").sum()),
                  "no_stock": int((k["status"] == "NO STOCK").sum()),
                  "day1_trolleys": len(t), "trolleys_kit_complete_from_stock": int(t["kit_complete_from_stock"].sum()),
                  "median_parts_to_make_per_trolley": float(t["parts_to_make"].median()) if len(t) else None,
                  "gt_codes_cured_day1": len(g), "gt_fully_covered": int((g["status"] == "FULLY COVERED").sum()),
                  "day1_cure_tyres": float(g["day1_cure_plan_tyres"].sum()), "gt_stock_for_them": float(
                      g[["day1_cure_plan_tyres", "gt_stock"]].min(axis=1).sum())})
    S = pd.DataFrame(S)
    BD = (K.groupby(["line", "department", "status"]).size().unstack(fill_value=0).reset_index())

    ds = io.DATA
    try:
        man = json.loads((ds / "_sql_manifest.json").read_text(encoding="utf-8"))
        wip_src = man["tables"]["opening_wip"]["source_table"]
    except Exception:
        wip_src = "(manifest not readable)"
    w0 = pd.read_csv(ds / "opening_wip.csv", encoding="utf-8-sig", parse_dates=["produced_time"])
    note = pd.DataFrame({"read me": [
        f"Run {a.run}. Day-1 window = first {a.hours:g} h of the plan (plant day 1, 07:00 -> 07:00).",
        f"WIP snapshot: {wip_src}; inventory_date {w0['inventory_date'].iloc[0]}, shift "
        f"{w0['shift'].iloc[0]}, updatedAt {w0['updatedAt'].iloc[0]}; material produced up to "
        f"{w0['produced_time'].max()} (the 22 Oct 06:30 shift-A strike). Data folder: {ds.name}.",
        "GT snapshot: JK_CTP_PPDS.CTP_GT_Inventory, inventory_date 22 Oct (06:36:03 sync).",
        "Kit level = parts a builder loads (bypassed cutter/slitter codes kept as parts). Coverage per "
        "item ignores competition between trolleys; 'Trolley kits' uses the schedule's real allocation.",
        "Usable for day 1 = alive at 07:00 (not past max ageing) and rested (min ageing) before the "
        "first trolley that needs it. Units: requirement in BOM unit and in stock unit (converted where "
        "the BOM gives metres per piece).",
        "Stock beyond the snapshot used by the engine (assumptions): master compound declared purchased "
        "(always available); A-WIP-1 24 h cover for an item type absent from the feed; A-WIP-2 cap strip / "
        "edge gum covered for the first 8 h. These are NOT counted as snapshot stock here."]})
    out = D / f"WIP_vs_DAY1_PLAN_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Read me", note), ("Summary", S), ("Snapshot", pd.DataFrame(snap)),
                         ("Kit items day1", K.sort_values(["line", "status", "item"])),
                         ("By department", BD), ("Trolley kits day1", TR.sort_values(["line", "plan_start"])),
                         ("Full tree day1", T.sort_values(["line", "department", "item"])),
                         ("GT vs day1 curing", GT.sort_values(["line", "status"]))):
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 50)
            ws.freeze_panes = "A2"
    pd.set_option("display.width", 250)
    print(pd.DataFrame(snap).to_string(index=False)); print()
    print(S.to_string(index=False)); print()
    print(BD.to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
