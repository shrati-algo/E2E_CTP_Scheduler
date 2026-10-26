#!/usr/bin/env python3
"""make_wip_usage_report.py - what happened to every opening-stock (WIP) batch.

    python make_wip_usage_report.py --run e2e_v45b

Rebuilds the opening-stock pool exactly as phase 2 does (ctpio.load_wip +
phase2_wip_netting.build_pool: same dedup, same ageing, same batch ids), joins
it to phase2_wip_allocations.csv (what the schedule drew), and classifies every
batch:

  CONSUMED (full)        all of it was drawn by the schedule
  CONSUMED (part)        some drawn; the rest is classified as below
  EXPIRED UNUSED         left over and its shelf life ends inside the horizon
  LEFT IN STOCK          left over and still usable at the horizon end
  DEAD AT OPEN           already past max ageing at 22 Oct 07:00 - never usable
  NOT NEEDED BY PLAN     the item is not demanded by this plan at all

Ageing is reported two ways:
  * real expiry   = produced_time + max ageing (what the material really has)
  * engine expiry = 07:00 + max ageing (phase 2's "opening clock": stock alive
                    at the bell gets its full shelf life from the bell)

Output: outputs/<run>/WIP_USAGE_<run>.xlsx
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import ctpio as io
import phase2_wip_netting as P2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    rows, uses = [], []
    for ln in ("pcr", "tbr"):
        O = D / ln
        dem = pd.read_csv(O / "phase1_demand.csv", parse_dates=["build_start"])
        opening = pd.read_csv(O / "phase1_building_lots.csv",
                              parse_dates=["build_start"])["build_start"].min()
        horizon = pd.read_csv(O / "phase5_building_plan.csv",
                              parse_dates=["build_end"])["build_end"].max()
        bom = io.load_bom(ln)
        itypes = io.load_itemtypes(bom)
        aging = io.load_aging(ln)
        aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)
        rt = io.load_routing(ln)
        io.schedulable_items(rt)                      # fills io.BYPASSED
        dept = {}
        for p_, d_ in zip(rt["routed_product"], rt["department"]):
            if p_ and p_ not in dept and io._s(d_):
                dept[p_] = io._s(d_)
        wip = io.load_wip(ln)
        if wip is None:
            continue
        pool, st = P2.build_pool(wip, aging, opening)
        alloc = pd.read_csv(O / "phase2_wip_allocations.csv",
                            parse_dates=["needed_by"])
        drawn = alloc.groupby("batch_id")["qty_used"].sum().to_dict()
        demanded = set(dem["item_code"].astype(str))

        def hrs(v, unit):
            v = pd.to_numeric(v, errors="coerce")
            if pd.isna(v):
                return None
            return float(v) * 24.0 if str(unit or "").strip().lower().startswith("day") else float(v)

        # every snapshot row, including the ones build_pool drops as dead
        live_rows = {b["row"]: b for it in pool for b in pool[it]}
        for i, r in enumerate(wip.itertuples(index=False)):
            item, q, t = io._s(r.itemcode), float(r.inventory), r.produced_time
            if not item or q <= 0 or pd.isna(t):
                continue
            fb = aging.get(item, (0.0, 0.0))
            mn = hrs(getattr(r, "MinAging", None), getattr(r, "MinAgingUnit", "")) or fb[0] or 0.0
            mx = hrs(getattr(r, "MaxAging", None), getattr(r, "MaxAgingUnit", "")) or fb[1] or 0.0
            real_exp = t + pd.Timedelta(hours=mx) if mx else pd.NaT
            b = live_rows.get(i + 2)
            used = float(drawn.get(b["batch_id"], 0.0)) if b else 0.0
            left = max(0.0, q - used)
            eng_exp = b["expiry"] if b else pd.NaT
            if b is None:
                status = "DEAD AT OPEN" if mx else "NOT IN POOL"
            elif item not in demanded and item not in io.BYPASSED and used <= 1e-6:
                status = "NOT NEEDED BY PLAN"
            elif left <= 1e-6:
                status = "CONSUMED (full)"
            else:
                exp_used = eng_exp if (pd.notna(eng_exp) and eng_exp != pd.Timestamp.max) else None
                rest = ("EXPIRED UNUSED" if exp_used is not None and exp_used <= horizon
                        else "LEFT IN STOCK")
                status = "CONSUMED (part) + " + rest if used > 1e-6 else rest
            rows.append({
                "line": ln.upper(), "item": item, "item_type": str(r.item_type),
                "department": dept.get(item, io.BYPASSED.get(item, "(not routed)")),
                "bypassed_cut_code": item in io.BYPASSED,
                "batch_id": b["batch_id"] if b else "", "unit": str(r.unit),
                "opening_qty": q, "consumed_qty": round(used, 3), "left_qty": round(left, 3),
                "status": status, "produced_time": t,
                "age_at_open_h": round((opening - t).total_seconds() / 3600, 1),
                "min_ageing_h": mn, "max_ageing_h": mx,
                "ready_at": t + pd.Timedelta(hours=mn),
                "real_expiry": real_exp,
                "engine_expiry": eng_exp if eng_exp != pd.Timestamp.max else pd.NaT,
                "real_shelf_left_at_open_h": (round((real_exp - opening).total_seconds() / 3600, 1)
                                              if pd.notna(real_exp) else None)})
        a2 = alloc.copy()
        a2["line"] = ln.upper()
        a2["age_at_need_h"] = ((a2["needed_by"] - pd.to_datetime(a2["produced_time"]))
                               .dt.total_seconds() / 3600).round(1)
        uses.append(a2)

    B = pd.DataFrame(rows)
    U = pd.concat(uses, ignore_index=True) if uses else pd.DataFrame()
    # used after its REAL expiry (produced + max ageing)?
    if len(U):
        rx = B.set_index("batch_id")["real_expiry"].to_dict()
        U["real_expiry"] = U["batch_id"].map(rx)
        U["used_after_real_expiry"] = U["needed_by"] > U["real_expiry"]

    def grp(keys):
        g = B.groupby(keys).agg(batches=("item", "size"), opening_qty=("opening_qty", "sum"),
                                consumed_qty=("consumed_qty", "sum"), left_qty=("left_qty", "sum"),
                                median_age_at_open_h=("age_at_open_h", "median"),
                                max_age_at_open_h=("age_at_open_h", "max")).reset_index()
        g["consumed_pct"] = (100 * g["consumed_qty"] / g["opening_qty"].replace(0, pd.NA)).round(1)
        for s in ("EXPIRED UNUSED", "LEFT IN STOCK", "DEAD AT OPEN", "NOT NEEDED BY PLAN"):
            m = B["status"].str.contains(s, regex=False)
            x = B[m].groupby(keys)["left_qty" if s in ("EXPIRED UNUSED", "LEFT IN STOCK")
                                   else "opening_qty"].sum()
            g[s.lower().replace(" ", "_") + "_qty"] = g.set_index(keys).index.map(x).fillna(0).values
        return g.round(1)

    by_status = (B.groupby(["line", "status"]).agg(batches=("item", "size"),
                 opening_qty=("opening_qty", "sum"), consumed_qty=("consumed_qty", "sum"),
                 left_qty=("left_qty", "sum")).reset_index().round(1))
    by_dept = grp(["line", "department"])
    by_type = grp(["line", "item_type"])
    by_item = grp(["line", "department", "item_type", "item"])
    late = int(U["used_after_real_expiry"].sum()) if len(U) else 0
    note = pd.DataFrame({"read me": [
        f"Opening stock = PPDS CTP_Opening_WIP, 22 Oct snapshot (data-ppds-v38), rebuilt exactly as "
        f"phase 2 builds it. Run: {a.run}. Horizon ends at the plan's last build.",
        "Quantities are in the stock's own unit (column 'unit': KG, M, NOS ...) - do not add across units.",
        "Status: CONSUMED (full/part), EXPIRED UNUSED (shelf life ends inside the horizon), LEFT IN STOCK "
        "(still usable at the horizon end), DEAD AT OPEN (past max ageing at 07:00), NOT NEEDED BY PLAN.",
        "real_expiry = produced + max ageing. engine_expiry = 07:00 + max ageing: phase 2 gives stock "
        "alive at the bell its full shelf life from the bell (the 'opening clock').",
        f"Draws needed after the batch's REAL expiry: {late} (sheet 'Consumption', column "
        f"used_after_real_expiry) - a consequence of the opening clock."]})
    out = D / f"WIP_USAGE_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Read me", note), ("By status", by_status), ("By department", by_dept),
                         ("By item type", by_type), ("By item", by_item), ("By batch", B),
                         ("Consumption", U)):
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 50)
            ws.freeze_panes = "A2"
    print(by_status.to_string(index=False))
    print(f"\ndraws needed after REAL expiry: {late}")
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
