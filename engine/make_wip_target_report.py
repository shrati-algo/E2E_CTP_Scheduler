#!/usr/bin/env python3
"""make_wip_target_report.py - what opening stock (WIP) would let the first days run to plan?

    python make_wip_target_report.py --run e2e_v45b [--days 2]

For the trolleys planned in the first `--days` plant days it works out, part by part
(the physical kit: parts a builder loads, bypassed cutter/slitter codes kept as parts):

  required          what those trolleys need, day by day (stock unit)
  in snapshot       usable stock in the opening snapshot, given to the earliest need first
  extra WIP needed  required - usable stock, day by day  ->  the WIP TARGET
  holdable?         stock made just before 07:00 must still be inside its max ageing when
                    the trolley uses it; parts whose shelf life is shorter than that wait
                    cannot be stocked for the whole window and must be made fresh during it

and, for curing, the green tyres needed on the rack for the presses until the newly
built tyres arrive (curing plan by GT code for the window vs GT stock).

Output: outputs/<run>/WIP_TARGET_FIRST_<days>_DAYS_<run>.xlsx
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io
import phase2_wip_netting as P2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--days", type=int, default=2)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    rows, gtr, trol = [], [], []
    for ln in ("pcr", "tbr"):
        O = D / ln
        bl = pd.read_csv(O / "phase1_building_lots.csv", parse_dates=["build_start", "build_end"])
        opening = bl["build_start"].min()
        snap_time = opening - pd.Timedelta(minutes=30)          # material made up to ~06:30
        end = opening + pd.Timedelta(days=a.days)
        win = bl[bl["build_start"] < end].copy()
        win["day"] = ((win["build_start"] - opening) // pd.Timedelta(days=1)).astype(int) + 1
        bom = io.load_bom(ln)
        rt = io.load_routing(ln)
        sched = io.schedulable_items(rt)
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
        pool, _ = P2.build_pool(wip, aging, opening)
        dem = pd.read_csv(O / "phase1_demand.csv", parse_dates=["build_start", "need_by"])
        bunits = {}
        for c, u in zip(bom["child"].map(io._s), bom["child_Unit"].map(io._s)):
            if c and u:
                bunits.setdefault(c, u)
        factor_of, _ = P2.unit_bridge(wip, dem, rt, bom, bunits)
        su = dict(zip(wip["itemcode"].map(io._s), wip["unit"].map(io._s)))
        walk = io.WalkCache(child_idx, set(sched) | set(io.BYPASSED), io.bias_items(rt))

        need = []                                   # (item, day, build_start, qty stock unit)
        for L in win.sort_values("build_start").itertuples(index=False):
            if (L.sku, eq) not in child_idx:
                continue
            _, _, roots = walk.get(L.sku, eq, L.built_item, frozenset())
            for it, per in roots.items():
                if per <= 0 or str(itypes.get(it, "")).strip().lower() in P2.ANCHOR_TYPES:
                    continue
                f = factor_of(L.sku, it) if it in pool else 1.0
                need.append((it, L.day, L.build_start, per * float(L.qty) * (f or 1.0),
                             L.building_lot_id))
        N = pd.DataFrame(need, columns=["item", "day", "build_start", "qty", "trolley"])

        # give the snapshot's usable stock to the earliest needs first (per item)
        left = {it: [dict(b) for b in sorted(bs, key=lambda b: b["expiry"])] for it, bs in pool.items()}
        N = N.sort_values(["item", "build_start"]).reset_index(drop=True)
        cov = []
        for r in N.itertuples(index=False):
            got = 0.0
            for b in left.get(r.item, []):
                if b["opening"] <= 0 or b["ready"] > r.build_start or b["expiry"] < r.build_start:
                    continue
                t = min(b["opening"], r.qty - got)
                b["opening"] -= t
                got += t
                if got >= r.qty - 1e-9:
                    break
            cov.append(got)
        N["from_snapshot"] = cov
        N["extra"] = (N["qty"] - N["from_snapshot"]).clip(lower=0)

        for it, g in N.groupby("item"):
            mn, mx = aging.get(it, (0.0, 0.0))
            last_use = g.loc[g["extra"] > 1e-9, "build_start"].max()
            wait_h = (last_use - snap_time).total_seconds() / 3600 if pd.notna(last_use) else 0.0
            holdable = (not mx) or mx <= 0 or wait_h <= mx
            r = {"line": ln.upper(), "item": it, "item_type": itypes.get(it, ""),
                 "department": dept.get(it, io.BYPASSED.get(it, "(not routed / purchased)")),
                 "stock_unit": su.get(it, bunits.get(it, "")),
                 "min_ageing_h": mn, "max_ageing_h": mx}
            for d in range(1, a.days + 1):
                gd = g[g["day"] == d]
                r[f"day{d}_required"] = round(gd["qty"].sum(), 2)
                r[f"day{d}_from_snapshot"] = round(gd["from_snapshot"].sum(), 2)
                r[f"day{d}_extra_wip_needed"] = round(gd["extra"].sum(), 2)
            r["total_required"] = round(g["qty"].sum(), 2)
            r["snapshot_qty_now"] = round(float(sum(b["opening"] for b in pool.get(it, []))), 2)
            r["total_extra_wip_needed"] = round(g["extra"].sum(), 2)
            r["extra_as_pct_of_required"] = round(100 * g["extra"].sum() / g["qty"].sum(), 1) if g["qty"].sum() else 0
            r["target_wip_at_0700"] = round(r["snapshot_qty_now"] + r["total_extra_wip_needed"], 2)
            r["hours_from_snapshot_to_last_use"] = round(wait_h, 1)
            r["can_be_held_as_wip"] = holdable
            r["note"] = ("" if holdable else
                         f"max ageing {mx:g} h < {wait_h:.0f} h wait: stock only what is used within "
                         f"{mx:g} h; the rest must be made fresh during the window")
            rows.append(r)

        for tid, g in N.groupby("trolley"):
            trol.append({"line": ln.upper(), "trolley": tid, "plan_start": g["build_start"].min(),
                         "day": int(g["day"].min()), "kit_parts": len(g),
                         "parts_short_now": int((g["extra"] > 1e-6).sum())})

        # green tyres for curing in the window
        cp = pd.read_csv(O / "phase7_curing_plan.csv", parse_dates=["planned_start"])
        gti = io._rd(f"gt_inventory_manual_{ln}")
        gstock = dict(zip(gti["SKUCode"].map(io._s), pd.to_numeric(gti["TotalQuantity"], errors="coerce")))
        cure, _ = io.load_curing_plan(ln, io.machine_alias(io.load_machines()))
        gt_of = cure.assign(gt=cure["gt_code"].map(io._s)).groupby("sku")["gt"].first().to_dict()
        gt_of.update(dict(zip(bl["sku"], bl["built_item"])))
        cw = cp[cp["planned_start"] < end].assign(gt=lambda x: x["sku"].map(gt_of))
        cw["day"] = ((cw["planned_start"] - opening) // pd.Timedelta(days=1)).astype(int).clip(lower=0) + 1
        for gt, g in cw.groupby("gt"):
            s_ = float(gstock.get(gt, 0) or 0)
            d1 = float(g.loc[g["day"] == 1, "qty_planned"].sum())
            gtr.append({"line": ln.upper(), "green_tyre": gt,
                        "day1_cure_plan": d1, "day2_cure_plan": float(g.loc[g["day"] == 2, "qty_planned"].sum()),
                        "gt_stock_now": s_, "day1_extra_gt_needed": max(0.0, d1 - s_)})

    R = pd.DataFrame(rows)
    T = pd.DataFrame(trol)
    G = pd.DataFrame(gtr)
    ex_cols = [f"day{d}_extra_wip_needed" for d in range(1, a.days + 1)]
    S = []
    for ln in ("PCR", "TBR"):
        r = R[R["line"] == ln]
        t = T[T["line"] == ln]
        g = G[G["line"] == ln]
        S.append({"line": ln, "trolleys_in_window": len(t),
                  "trolleys_short_now": int((t["parts_short_now"] > 0).sum()),
                  "kit_parts": len(r), "parts_needing_extra_wip": int((r["total_extra_wip_needed"] > 1e-6).sum()),
                  "parts_fully_in_snapshot": int((r["total_extra_wip_needed"] <= 1e-6).sum()),
                  "parts_not_holdable_whole_window": int((~r["can_be_held_as_wip"]).sum()),
                  "extra_KG": round(r.loc[r["stock_unit"] == "KG", "total_extra_wip_needed"].sum(), 0),
                  "extra_M": round(r.loc[r["stock_unit"] == "M", "total_extra_wip_needed"].sum(), 0),
                  "extra_NOS": round(r.loc[r["stock_unit"] == "NOS", "total_extra_wip_needed"].sum(), 0),
                  "day1_extra_M": round(r.loc[r["stock_unit"] == "M", "day1_extra_wip_needed"].sum(), 0),
                  "day1_extra_NOS": round(r.loc[r["stock_unit"] == "NOS", "day1_extra_wip_needed"].sum(), 0),
                  "day1_extra_KG": round(r.loc[r["stock_unit"] == "KG", "day1_extra_wip_needed"].sum(), 0),
                  "day1_extra_green_tyres": round(g["day1_extra_gt_needed"].sum(), 0)})
    S = pd.DataFrame(S)
    BT = (R.groupby(["line", "item_type", "stock_unit"])[ex_cols + ["total_required", "snapshot_qty_now",
                                                                   "total_extra_wip_needed"]]
          .sum().round(0).reset_index().sort_values(["line", "total_extra_wip_needed"], ascending=[True, False]))
    BD = (R.groupby(["line", "department", "stock_unit"])[ex_cols + ["total_extra_wip_needed"]]
          .sum().round(0).reset_index().sort_values(["line", "total_extra_wip_needed"], ascending=[True, False]))
    note = pd.DataFrame({"read me": [
        f"Run {a.run}. Window = first {a.days} plant days (07:00 -> 07:00). Snapshot = PPDS "
        f"CTP_Opening_WIP 22 Oct shift A (06:30 strike).",
        "Kit level: parts a builder loads (cut belts/plies/beads etc. - bypassed cutter/slitter codes kept as parts).",
        "extra_wip_needed = required - usable snapshot stock (stock given to the earliest need first; "
        "usable = alive at 07:00 and rested before the trolley). target_wip_at_0700 = snapshot + extra.",
        "can_be_held_as_wip = the part, made just before 07:00, is still inside its max ageing at its last "
        "use in the window. If not, only part of it can be stocked; the rest has to be made during the window.",
        "Green tyres: extra GT needed so day-1 curing does not wait for newly built tyres.",
        "Assumptions the engine adds beyond the snapshot (master compound always available, A-WIP-1, A-WIP-2) "
        "are not counted as snapshot stock here."]})
    out = D / f"WIP_TARGET_FIRST_{a.days}_DAYS_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Read me", note), ("Summary", S), ("By item type", BT), ("By department", BD),
                         ("Item target", R.sort_values(["line", "total_extra_wip_needed"], ascending=[True, False])),
                         ("Green tyres", G.sort_values(["line", "day1_extra_gt_needed"], ascending=[True, False])),
                         ("Trolleys", T.sort_values(["line", "plan_start"]))):
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 50)
            ws.freeze_panes = "A2"
    pd.set_option("display.width", 250)
    print(S.to_string(index=False)); print()
    print(BT.to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
