#!/usr/bin/env python3
"""make_wip_presence_report.py - opening stock PRESENT vs CONSUMED, and the
cut / slit (incl. CAP 66) conversion check, for one run.

    python make_wip_presence_report.py --run e2e_v46i

Rebuilds the opening-stock pool EXACTLY as phase 2 does today, in its order:

    ctpio.load_wip          one row per trolley (latest sync)
    mhe_stock_to_item       v46h: CAP 66 FAB / STL / cap strip -> mother roll
    bypass_stock_to_children  v46a: cut / slit stock -> the child it was cut from
    build_pool              ageing, dead-at-open, FEFO batch ids

so every batch id matches phase2_wip_allocations.csv (what the schedule drew).
(make_wip_usage_report.py predates the two conversions and no longer matches.)

Sheets
  Summary            per line and unit: present, dead at open, consumed, left
  By item            presence vs consumption vs the plan's demand, per item
  By batch           every stock row, its status and what was drawn from it
  Cut-slit check     every cut / slit code in stock: converted? to what? used?
  Not converted      cut / slit / other stock that never reached the netting
  CAP 66             the MHE conversion and what the schedule drew
  Green tyres        GT rack stock vs the plan's GT codes
Writes outputs/<run>/WIP_PRESENCE_VS_CONSUMPTION_<run>.xlsx
"""
from __future__ import annotations

import argparse
import re
import sys

import pandas as pd

import ctpio as io
import phase2_wip_netting as P2
import rules as R


_ILLEGAL = re.compile("[" + "".join(chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32))) + "]")


def _hrs(v, unit):
    v = pd.to_numeric(v, errors="coerce")
    if pd.isna(v):
        return None
    return float(v) * 24.0 if str(unit or "").strip().lower().startswith("day") else float(v)


def _on(path, default=False):
    return str(R.rule(path, default)).lower() in ("1", "true", "on", "yes")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    log = (io.OUT_ROOT / f"{a.run}_console.log")
    log = log.read_text(encoding="utf-8", errors="ignore") if log.exists() else ""

    batches, items, cut, notconv, cap, gts, summ = [], [], [], [], [], [], []
    # each line's own bypassed (cutter / slitter) codes - io.BYPASSED is
    # module state, so it is rebuilt per line (phase 2 runs one line per process)
    byp_of = {}
    for _l in ("pcr", "tbr"):
        io.BYPASSED.clear()
        io.schedulable_items(io.load_routing(_l))
        byp_of[_l] = dict(io.BYPASSED)
    for ln in io.scheduled_lines(a.run):
        io.BYPASSED.clear()
        O = D / ln
        bom = io.load_bom(ln)
        rt = io.load_routing(ln)
        sched = io.schedulable_items(rt)              # also fills io.BYPASSED
        itypes = io.load_itemtypes(bom)
        aging = io.load_aging(ln)
        aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)
        lots = pd.read_csv(O / "phase1_building_lots.csv", parse_dates=["build_start"])
        opening = lots["build_start"].min()
        horizon = pd.read_csv(O / "phase5_building_plan.csv",
                              parse_dates=["build_end"])["build_end"].max()
        dem = pd.read_csv(O / "phase1_demand.csv")
        dem_q = dem.groupby("item_code")["demand_qty"].sum()
        net = pd.read_csv(O / "phase2_demand_net.csv")
        net_q = net.groupby("item_code")["net_qty"].sum()
        alloc = pd.read_csv(O / "phase2_wip_allocations.csv")
        real = alloc[~alloc["batch_id"].astype(str).str.contains("#ASSUMED|#FIRST8H")]
        drawn = real.groupby("batch_id")["qty_used"].sum().to_dict()
        _nb = pd.to_datetime(real["needed_by"], errors="coerce")
        need_max = _nb.groupby(real["batch_id"]).max().to_dict()

        # ---- rebuild the pool exactly as phase 2 ---------------------------
        w0 = io.load_wip(ln)
        w1, mrep = P2.mhe_stock_to_item(w0, R.rule("wip.mhe_conversion") or {}, ln)
        conv = pd.DataFrame()
        w2 = w1
        if _on("wip.bypass_stock_to_children"):
            cb = R.rule("wip.bypass_conversion_bom")
            cbom = (pd.read_csv(io.resolve_ref_dir(cb) / f"jkt_bom_{ln}.csv", dtype=str)
                    if cb else None)
            w2, conv = P2.bypass_stock_to_children(w1, bom, itypes, aging, io.bias_items(rt),
                                                   ln, sched, cbom)
        pool, st = P2.build_pool(w2, aging, opening)
        live = {b["row"]: b for it in pool for b in pool[it]}
        # the run's own conversion file must match this rebuild
        run_conv = pd.read_csv(O / "phase2_bypass_stock_conversion.csv")
        same = (len(run_conv) == len(conv) and
                abs(run_conv.get("child_qty", pd.Series(dtype=float)).sum()
                    - conv.get("child_qty", pd.Series(dtype=float)).sum()) < 1e-6)

        for i, r in enumerate(w2.itertuples(index=False)):
            item, q, t = io._s(r.itemcode), float(r.inventory), r.produced_time
            if not item or q <= 0 or pd.isna(t):
                continue
            fb = aging.get(item, (0.0, 0.0))
            mn = _hrs(getattr(r, "MinAging", None), getattr(r, "MinAgingUnit", "")) or fb[0] or 0.0
            mx = _hrs(getattr(r, "MaxAging", None), getattr(r, "MaxAgingUnit", "")) or fb[1] or 0.0
            src = getattr(r, "converted_from", "")
            src = src if isinstance(src, str) else ""
            b = live.get(i + 2)
            used = float(drawn.get(b["batch_id"], 0.0)) if b else 0.0
            left = max(0.0, q - used)
            if b is None:
                status = "DEAD AT OPEN"
            elif item not in dem_q.index and used <= 1e-6:
                status = "NOT NEEDED BY PLAN"
            elif left <= 1e-6:
                status = "CONSUMED (full)"
            else:
                eng = b["expiry"] if b["expiry"] != pd.Timestamp.max else None
                rest = "EXPIRED UNUSED" if (eng is not None and eng <= horizon) else "LEFT IN STOCK"
                status = ("CONSUMED (part) + " + rest) if used > 1e-6 else rest
            real_exp = t + pd.Timedelta(hours=mx) if mx else pd.NaT
            batches.append({
                "line": ln.upper(), "item": item, "item_type": str(getattr(r, "item_type", "")),
                "unit": str(r.unit), "batch_id": b["batch_id"] if b else "",
                "production_id": getattr(r, "production_id", ""),
                "converted_from": src or ("MHE " + str(getattr(r, "mhe_code", ""))
                                          if item in (R.rule("wip.mhe_conversion") or {}) else ""),
                "present_qty": round(q, 3), "consumed_qty": round(used, 3),
                "left_qty": round(left, 3), "status": status, "produced_time": t,
                "age_at_open_h": round((opening - t).total_seconds() / 3600, 1),
                "min_ageing_h": mn, "max_ageing_h": mx,
                "real_expiry": real_exp,
                "last_need": need_max.get(b["batch_id"]) if b else pd.NaT,
                "drawn_after_real_expiry": bool(b and used > 1e-6 and pd.notna(real_exp)
                                                and pd.notna(need_max.get(b["batch_id"]))
                                                and need_max[b["batch_id"]] > real_exp)})

        B = pd.DataFrame([x for x in batches if x["line"] == ln.upper()])
        # ---- by item -------------------------------------------------------
        for it, g in B.groupby("item"):
            dead = g.loc[g["status"] == "DEAD AT OPEN", "present_qty"].sum()
            pres = g["present_qty"].sum()
            usable = pres - dead
            cons = g["consumed_qty"].sum()
            dq = float(dem_q.get(it, 0.0))
            items.append({
                "line": ln.upper(), "item": it, "item_type": g["item_type"].iloc[0],
                "unit": g["unit"].iloc[0], "batches": len(g),
                "from_cut_stock_batches": int((g["converted_from"].astype(str)
                                               .str.len() > 0).sum()),
                "present_qty": round(pres, 1), "dead_at_open_qty": round(dead, 1),
                "usable_qty": round(usable, 1), "consumed_qty": round(cons, 1),
                "consumed_pct_of_usable": round(100 * cons / usable, 1) if usable > 0 else None,
                "left_unused_qty": round(usable - cons, 1),
                "expired_unused_qty": round(g.loc[g["status"].str.contains("EXPIRED UNUSED"),
                                                  "left_qty"].sum(), 1),
                "plan_demand_qty": round(dq, 1),
                "demand_covered_by_stock_pct": round(100 * cons / dq, 1) if dq > 0 else None,
                "still_made_qty": round(float(net_q.get(it, 0.0)), 1),
                "verdict": ("not demanded by this plan" if dq <= 0 else
                            "all usable stock consumed" if usable > 0 and usable - cons <= 1e-6 else
                            "stock left while item still made" if float(net_q.get(it, 0.0)) > 1e-6
                            and usable - cons > 1e-6 else
                            "demand met; surplus stock" if usable - cons > 1e-6 else
                            "no usable stock")})

        # ---- summary per unit ---------------------------------------------
        for u, g in B.groupby("unit"):
            summ.append({"line": ln.upper(), "unit": u, "rows": len(g),
                         "present": round(g["present_qty"].sum()),
                         "dead_at_open": round(g.loc[g["status"] == "DEAD AT OPEN",
                                                     "present_qty"].sum()),
                         "not_needed_by_plan": round(g.loc[g["status"] == "NOT NEEDED BY PLAN",
                                                           "present_qty"].sum()),
                         "consumed": round(g["consumed_qty"].sum()),
                         "expired_unused": round(g.loc[g["status"].str.contains("EXPIRED UNUSED"),
                                                       "left_qty"].sum()),
                         "left_in_stock": round(g.loc[g["status"].str.contains("LEFT IN STOCK"),
                                                      "left_qty"].sum())})

        # ---- cut / slit conversion check ----------------------------------
        if len(conv):
            cB = B[B["converted_from"].astype(str).str.len() > 0]
            used_by_src = {}
            for r in cB.itertuples(index=False):
                pid = str(r.production_id).split(">")[0]
                used_by_src[(r.converted_from, pid, r.item)] = r.consumed_qty
            for r in conv.itertuples(index=False):
                ok = r.status == "CONVERTED"
                cu = used_by_src.get((r.cut_code, str(r.production_id), getattr(r, "child", "")), 0.0)
                cut.append({
                    "line": ln.upper(), "cut_code": r.cut_code, "department": r.department,
                    "production_id": r.production_id, "cut_qty": r.cut_qty, "cut_unit": r.cut_unit,
                    "status": r.status, "child": getattr(r, "child", "") if ok else "",
                    "via": getattr(r, "via", "") if ok else "",
                    "ratio": getattr(r, "ratio", None) if ok else None,
                    "ratio_source": getattr(r, "ratio_source", "") if ok else "",
                    "child_qty": getattr(r, "child_qty", None) if ok else None,
                    "child_consumed_qty": round(cu, 3) if ok else None,
                    "child_demanded_by_plan": (getattr(r, "child", "") in dem_q.index) if ok else None})

        # cut / slit stock the split sent to a line that cannot convert it:
        # the code is bypassed (cut) on the OTHER line only
        other = byp_of["tbr" if ln == "pcr" else "pcr"]
        for it, g in B[B["item"].isin(set(other) - set(byp_of[ln]))].groupby("item"):
            notconv.append({"line": ln.upper(), "item": it, "qty": round(g["present_qty"].sum(), 3),
                            "unit": g["unit"].iloc[0], "where": "stage_wip_v6 split",
                            "why": f"cut code of the OTHER line ({other[it]}); this line's "
                                   f"routing has no such cutter, so it is never converted - "
                                   f"status here: {', '.join(sorted(g['status'].unique()))}"})
        # cut / slit codes still in the pool under their own code
        byp = set(io.BYPASSED)
        kept = (conv[conv["status"] != "CONVERTED"].groupby("cut_code")["status"].first()
                if len(conv) else pd.Series(dtype=str))
        for it, g in B[B["item"].isin(byp)].groupby("item"):
            notconv.append({"line": ln.upper(), "item": it, "batches": len(g),
                            "qty": round(g["present_qty"].sum(), 3),
                            "unit": g["unit"].iloc[0], "where": "phase 2 (kept on cut code)",
                            "why": kept.get(it, "bypassed code left as its own stock"),
                            "consumed_qty": round(g["consumed_qty"].sum(), 3),
                            "status": ", ".join(sorted(g["status"].unique()))})
        # ---- CAP 66 --------------------------------------------------------
        if len(mrep):
            m = mrep.copy()
            capB = B[B["item"].isin(m["itemcode"].unique())]
            byp_pid = dict(zip(capB["production_id"].astype(str), capB["consumed_qty"]))
            st_pid = dict(zip(capB["production_id"].astype(str), capB["status"]))
            m["consumed_qty"] = m["production_id"].astype(str).map(byp_pid).fillna(0.0)
            m["batch_status"] = m["production_id"].astype(str).map(st_pid).fillna("")
            m["line"] = ln.upper()
            cap.append(m)
        summ.append({"line": ln.upper(), "unit": "(check)",
                     "rows": int(st["rows"]),
                     "present": None, "dead_at_open": int(st["dead_at_open"]),
                     "not_needed_by_plan": None,
                     "consumed": None, "expired_unused": None,
                     "left_in_stock": ("rebuild matches phase2_bypass_stock_conversion.csv"
                                       if same else "REBUILD DIFFERS from the run")})

        # ---- green tyres ---------------------------------------------------
        gi = pd.read_csv(io.DATA / f"gt_inventory_manual_{ln}.csv")
        used_gt = set(lots["built_item"].astype(str)) if "built_item" in lots.columns else set()
        for r in gi.itertuples(index=False):
            gts.append({"line": ln.upper(), "gt_code": r.SKUCode, "on_rack": r.TotalQuantity,
                        "plan_builds_this_gt": str(r.SKUCode) in used_gt})
        mt = re.findall(r"GT stock by GT\s*:\s*(\d+) GT code\(s\) match the plan; ([\d,]+) of "
                        r"([\d,]+) green tyre\(s\) allocated", log)
        if mt:
            k = 0 if ln == "pcr" else min(1, len(mt) - 1)
            summ.append({"line": ln.upper(), "unit": "GREEN TYRES (phase 7)",
                         "rows": int(mt[k][0]), "present": int(mt[k][2].replace(",", "")),
                         "dead_at_open": None, "not_needed_by_plan": None,
                         "consumed": int(mt[k][1].replace(",", "")), "expired_unused": None,
                         "left_in_stock": None})

    # orphans dropped at staging (in neither BOM)
    orf = io.DATA / "opening_wip_orphans.csv"
    if orf.exists():
        for r in pd.read_csv(orf).itertuples(index=False):
            notconv.append({"line": "(staging)", "item": r.itemcode, "qty": r.qty, "unit": "",
                            "where": "stage_wip_v6 (dropped)", "why": f"{r.issue}; type {r.item_type}"})

    Bt = pd.DataFrame(batches)
    stat = (Bt.groupby(["line", "unit", "status"]).agg(rows=("item", "size"),
            present_qty=("present_qty", "sum"), consumed_qty=("consumed_qty", "sum"),
            left_qty=("left_qty", "sum")).reset_index().round(1))
    capdf = pd.concat(cap, ignore_index=True) if cap else pd.DataFrame()
    late = int(Bt["drawn_after_real_expiry"].sum())
    note = pd.DataFrame({"read me": [
        f"Run {a.run}. Opening stock rebuilt exactly as phase 2 builds it: load_wip -> CAP 66 "
        "MHE conversion -> cut/slit stock to child -> ageing (dead at open) -> FEFO batches.",
        "Quantities are in the stock's own unit (KG, M, NOS): never add across units.",
        "Status: CONSUMED (full / part), EXPIRED UNUSED (left over, engine expiry inside the horizon), "
        "LEFT IN STOCK, DEAD AT OPEN (past produced + max ageing at the plan start), NOT NEEDED BY PLAN.",
        "By item: still_made_qty = demand the stock did not cover (made fresh). 'stock left while item "
        "still made' = stock was not usable in time for those demands (ageing / FEFO timing).",
        "Cut-slit check: every bypassed cutter / slitter code found in stock, the child it became, the "
        "ratio and its source, and how much of the converted child the schedule drew.",
        f"Batches drawn for a need AFTER their REAL expiry (produced + max ageing): {late}. "
        "Phase 2 restarts max ageing at the plan start ('opening clock'), so stock alive at "
        "07:00 is usable for its full shelf life from then (column drawn_after_real_expiry)."]})
    out = D / f"WIP_PRESENCE_VS_CONSUMPTION_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Read me", note), ("Summary", pd.DataFrame(summ)),
                         ("By status", stat), ("By item", pd.DataFrame(items)),
                         ("By batch", Bt), ("Cut-slit check", pd.DataFrame(cut)),
                         ("Not converted", pd.DataFrame(notconv)), ("CAP 66", capdf),
                         ("Green tyres", pd.DataFrame(gts))):
            df = df.copy()
            for c in df.columns:
                if df[c].dtype == object:
                    df[c] = df[c].map(lambda v: _ILLEGAL.sub("", v) if isinstance(v, str) else v)
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(
                xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 50)
            ws.freeze_panes = "A2"
    print(pd.DataFrame(summ).to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
