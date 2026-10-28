#!/usr/bin/env python3
"""make_ctp_schedule_workbook.py - the plant's own six-sheet schedule book.

    python make_ctp_schedule_workbook.py --run e2e_v15 --out CTP_27SEP-01OCT_SCHEDULE_v15.xlsx

Reproduces the shape of `plan-files/CTP_27-30SEP_SCHEDULE.xlsx` exactly, sheet
for sheet and column for column, from a v15 run:

  1  Summary           line x (build plan, built, cure plan, cured, %)
  2  Daily PCR TBR     the same, day by day, for PCR and TBR
  3  Curing schedule   skuCode, pressNo, shift, start, end, qty, cycleTime,
                       lot_id (= the run's prod_id), consumption_id,
                       kit_status, kit_ready, green_age_h
  4  Building schedule date, shift, machine, item, start, end, qty, lot_id,
                       prod_id, consumption_id
  5  Sheet1            "Sum of produce_qty" pivot - process/machine/UOM x day
  6  Upstream schedule every upstream row, the plant's 11 columns

Two sheets are ADDED because v15 has something the reference book did not, and
hiding them inside a number nobody can open would be worse than a longer book:

  7  Assumptions       every assumption the run made, with its code, the item
                       it was made for, the value used and where it came from
  8  Rule compliance   all 76 rules of the master book with their verdict

GREEN AGE, AND WHY IT IS NOT ALWAYS AVAILABLE
  The reference book carries `green_age_h` - how long a green tyre sat before
  it was cured. It is computed here as (curing start - the moment the building
  plan finished that SKU's feeding trolley). Where a press lot is fed from
  OPENING GT STOCK rather than from a trolley in this plan, there is no build
  event to measure from and the cell is left empty rather than filled with a
  plausible number.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import ctpio as io

NAVY = PatternFill("solid", fgColor="1F3864")
SUB = PatternFill("solid", fgColor="D9E2F3")
TOT = PatternFill("solid", fgColor="FFF2CC")
BAD = PatternFill("solid", fgColor="FFC7CE")
GOOD = PatternFill("solid", fgColor="C6EFCE")
WARN = PatternFill("solid", fgColor="FFEB9C")
THIN = Border(*[Side(style="thin", color="D0D0D0")] * 4)
WB = Font(bold=True, color="FFFFFF")


def _write(ws, d: pd.DataFrame, freeze="A2") -> None:
    for j, c in enumerate(d.columns, 1):
        cell = ws.cell(1, j, str(c))
        cell.font, cell.fill = WB, NAVY
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for i, row in enumerate(d.itertuples(index=False), 2):
        for j, v in enumerate(row, 1):
            if isinstance(v, pd.Timestamp):
                v = v.to_pydatetime()
            elif pd.isna(v):
                v = None
            c = ws.cell(i, j, v)
            c.border = THIN
            if isinstance(v, __import__("datetime").datetime):
                c.number_format = "yyyy-mm-dd hh:mm:ss"
    ws.freeze_panes = freeze
    # Every sheet gets an Excel autofilter on its header row, so `line` can be
    # filtered to PCR or TBR (and any other column narrowed) without touching
    # the data. The filter spans the used range, header included - a filter set
    # on the header alone silently drops the last row from the selection.
    if len(d):
        ws.auto_filter.ref = f"A1:{get_column_letter(len(d.columns))}{len(d) + 1}"
    for j, c in enumerate(d.columns, 1):
        w = max(len(str(c)), *(len(str(x))
                for x in d.iloc[:200, j - 1].astype(str))) if len(d) else len(str(c))
        ws.column_dimensions[get_column_letter(j)].width = min(max(w + 2, 10), 42)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="e2e_v15")
    ap.add_argument("--out", default="CTP_27SEP-01OCT_SCHEDULE_v15.xlsx")
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    LN = io.scheduled_lines(a.run)

    print("\n" + "=" * 84)
    print(f"  CTP SCHEDULE BOOK - plant format   [{a.run}]")
    print("=" * 84)

    # ------------------------------------------------------------ gather
    cure, build, floor, dayg, dayc = {}, {}, {}, {}, {}
    for ln in LN:
        cure[ln] = pd.read_csv(D / ln / "phase7_curing_plan.csv",
                               parse_dates=["planned_start", "planned_end",
                                            "gt_ready", "feasible_start",
                                            "feasible_end"])
        build[ln] = pd.read_csv(D / ln / "phase5_building_plan.csv",
                                parse_dates=["build_start", "build_end",
                                             "kit_ready", "feasible_start",
                                             "feasible_end"])
        floor[ln] = pd.read_csv(D / f"phase5_floor_schedule_{ln}.csv",
                                encoding="utf-8-sig",
                                parse_dates=["start_time", "end_time"])
        dayg[ln] = pd.read_csv(D / ln / "phase6_daywise_gt.csv")
        dayc[ln] = pd.read_csv(D / ln / "phase7_daywise_cured.csv")

    # ---- MATERIAL GENEALOGY, carried onto the building and curing sheets ---
    # `phase5_floor_schedule_{line}.csv` is the plant-format file and the only
    # place the run states, per row, WHICH LOTS FED IT. The building and curing
    # sheets were showing the work without the genealogy, so a row could not be
    # walked back to the material that made it. Both lookups key on the floor
    # file's own identifiers; nothing here is recomputed.
    #
    #   BUILDING  keyed on building_lot_id (BLD000005), which the floor file
    #             carries verbatim as lot_id.
    #   CURING    has no lot id anywhere in the engine - a press run is
    #             identified by its prod_id (P000563). Keyed on
    #             (press, start second), both rounded the way the floor file
    #             rounds them.
    bld_gen: dict = {}
    cur_gen: dict = {}
    for ln in LN:
        f = floor[ln]
        dep = f["department"].astype(str)
        for r in f[dep == "BUILDING"].itertuples(index=False):
            # setdefault: a trolley split across shifts keeps its FIRST piece's id
            bld_gen.setdefault((ln, str(r.lot_id)), (str(r.prod_id or ""),
                                                     str(r.consumption_id or "")))
        for r in f[dep == "CURING"].itertuples(index=False):
            k = (ln, str(r.machine).strip(),
                 pd.Timestamp(r.start_time).round("1s"))
            cur_gen.setdefault(k, (str(r.prod_id or ""), str(r.consumption_id or "")))

    wb = Workbook()
    wb.remove(wb.active)

    # ---------------------------------------------------------- 1 Summary
    srows = []
    for ln in LN:
        b, c = build[ln], cure[ln]
        bp = float(b["qty"].sum())
        bd = float(b.loc[b["within_horizon"], "qty"].sum())
        cp = float(c["qty"].sum())
        cd = float(c.loc[c["within_horizon"], "qty"].sum())
        srows.append({"line": ln.upper(), "build_plan": round(bp),
                      "built_by_plan_end": round(bd), "cure_plan": round(cp),
                      "cured_by_plan_end": round(cd),
                      "built_%": round(100 * bd / max(bp, 1), 1),
                      "cured_%": round(100 * cd / max(cp, 1), 1)})
    t = {k: sum(r[k] for r in srows) for k in
         ("build_plan", "built_by_plan_end", "cure_plan", "cured_by_plan_end")}
    srows.append({"line": "ALL", **t,
                  "built_%": round(100 * t["built_by_plan_end"] / max(t["build_plan"], 1), 1),
                  "cured_%": round(100 * t["cured_by_plan_end"] / max(t["cure_plan"], 1), 1)})
    summ = pd.DataFrame(srows)
    ws = wb.create_sheet("Summary")
    _write(ws, summ)
    for i in range(2, len(summ) + 2):
        for j in (6, 7):
            v = ws.cell(i, j).value
            ws.cell(i, j).fill = GOOD if (v or 0) >= 90 else (
                WARN if (v or 0) >= 75 else BAD)
    ws.cell(len(summ) + 3, 1, "build_plan / cure_plan are the FROZEN plan; "
            "built / cured are what this schedule achieves inside the plan "
            "horizon. A shortfall is a lot landing outside the horizon - the "
            "plan is never rewritten.").font = Font(italic=True, size=9)

    # -------------------------------------------------- 2 Daily PCR TBR
    drows = []
    for ln in LN:
        g = dayg[ln].set_index(dayg[ln].columns[0])
        c = dayc[ln].set_index(dayc[ln].columns[0])
        for day in sorted(set(g.index) | set(c.index)):
            drows.append({
                "line": ln.upper(),
                "day": pd.to_datetime(day).strftime("%d-%b"),
                "build_plan": int(g.loc[day, "plan"]) if day in g.index else 0,
                "built": int(g.loc[day, "built"]) if day in g.index else 0,
                "cure_plan": int(c.loc[day, "plan"]) if day in c.index else 0,
                "cured": int(c.loc[day, "cured"]) if day in c.index else 0})
    daily = pd.DataFrame(drows)
    daily["built_%"] = (100 * daily["built"] / daily["build_plan"].clip(lower=1)).round(1)
    daily["cured_%"] = (100 * daily["cured"] / daily["cure_plan"].clip(lower=1)).round(1)
    ws = wb.create_sheet("Daily PCR TBR")
    _write(ws, daily)

    # ---------------------------------------------------- 3 Curing schedule
    crows = []
    for ln in LN:
        c = cure[ln].copy()
        rt = io.load_routing(ln)
        ci = io.cure_index(rt)

        def _cyc(sku):
            e = ci.get(str(sku)) or {}
            v, u = e.get("proc_time"), str(e.get("uom") or "").upper()
            if not v:
                return None
            v = float(v)
            return round(v / 60.0, 2) if u == "SEC" else (
                round(v * 60.0, 2) if u == "HOURS" else round(v, 2))

        # green age: curing start minus the feeding build end, per SKU
        b = build[ln]
        bend = (b.groupby("sku")["feasible_end"].min()
                if "sku" in b.columns else pd.Series(dtype="datetime64[ns]"))
        for r in c.itertuples(index=False):
            ga = None
            be = bend.get(str(r.sku))
            if pd.notna(be) and pd.notna(r.feasible_start):
                ga = round((r.feasible_start - be).total_seconds() / 3600.0, 2)
            # the press run's own id and the green-tyre lots it consumed
            pid, cid = cur_gen.get(
                (ln, str(r.press).strip(),
                 pd.Timestamp(r.feasible_start).round("1s")
                 if pd.notna(r.feasible_start) else pd.NaT), ("", ""))
            if not pid:
                # The press lot never ran - the building plan never made a
                # green tyre for it - so it has no row in the floor file and no
                # genealogy. Say that, rather than leave two blank cells that
                # read as missing data.
                pid = cid = "NOT SCHEDULED (no green tyre)"
            crows.append({
                "line": ln.upper(), "skuCode": r.sku, "pressNo": r.press,
                "shift": io.shift_of(r.feasible_start) if pd.notna(r.feasible_start) else "",
                "startTime": r.feasible_start, "endTime": r.feasible_end,
                "qty": r.qty, "cycleTime": _cyc(r.sku),
                # A press run has no lot id in the engine; its identity in the
                # plant-format file is prod_id, and that is what is shown here.
                "lot_id": pid, "consumption_id": cid,
                "kit_status": "READY" if pd.notna(r.gt_ready) else "NO GREEN TYRE",
                "kit_ready": r.gt_ready, "green_age_h": ga,
                "within_horizon": bool(r.within_horizon),
                "push_h": round(float(r.push_h), 2)})
    cs = pd.DataFrame(crows).sort_values(["line", "startTime", "pressNo"])
    cs = io.with_machine_name(cs, "pressNo", "pressName")
    ws = wb.create_sheet("Curing schedule")
    _write(ws, cs)

    # -------------------------------------------------- 4 Building schedule
    brows = []
    for ln in LN:
        for r in build[ln].itertuples(index=False):
            brows.append({
                "line": ln.upper(),
                "date": (io.plant_day(r.feasible_start).date()
                         if pd.notna(r.feasible_start) else None),
                "shift": io.shift_of(r.feasible_start) if pd.notna(r.feasible_start) else "",
                "machine": r.machine, "item": r.built_item, "sku": r.sku,
                "start_time": r.feasible_start, "end_time": r.feasible_end,
                "qty": r.qty, "lot_id": r.building_lot_id,
                "prod_id": bld_gen.get((ln, str(r.building_lot_id)), ("", ""))[0],
                "consumption_id": bld_gen.get((ln, str(r.building_lot_id)),
                                              ("", ""))[1],
                "kit_ready": r.kit_ready,
                "kit_incomplete": bool(r.kit_incomplete),
                "within_horizon": bool(r.within_horizon)})
    bs = pd.DataFrame(brows).sort_values(["line", "start_time", "machine"])
    bs = io.with_machine_name(bs, "machine")
    ws = wb.create_sheet("Building schedule")
    _write(ws, bs)

    # ------------------------------------------------------------ 5 Sheet1
    fl = pd.concat([floor[ln].assign(line=ln.upper()) for ln in LN],
                   ignore_index=True)
    fl["day"] = fl["start_time"].map(
        lambda t: io.plant_day(t).date() if pd.notna(t) else None)
    fl["produce_qty"] = pd.to_numeric(fl["produce_qty"], errors="coerce").fillna(0.0)
    # `line` leads the pivot index so this sheet can be filtered to PCR or TBR
    # like every other sheet. The reference book's three levels follow it
    # unchanged.
    fl["machine_name"] = fl["machine"].map(
        lambda v: io.machine_names().get(str(v).split(".")[0], "")).fillna("")
    piv = pd.pivot_table(fl, index=["line", "process", "machine", "machine_name", "UOM"],
                         columns="day", values="produce_qty", aggfunc="sum",
                         fill_value=0.0)
    piv = piv.sort_index()
    ws = wb.create_sheet("Sheet1")
    ws.cell(3, 1, "Sum of produce_qty").font = Font(bold=True)
    ws.cell(3, 6, "date").font = Font(bold=True)
    hdr = ["line", "process", "machine", "machine_name", "UOM"] +         [str(c) for c in piv.columns] + ["Grand Total"]
    for j, h in enumerate(hdr, 1):
        c = ws.cell(4, j, h)
        c.font, c.fill = WB, NAVY
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    r0 = 5
    for idx, row in piv.iterrows():
        for j, v in enumerate(list(idx) + [float(x) for x in row] + [float(row.sum())], 1):
            c = ws.cell(r0, j, v)
            c.border = THIN
            if j > 5:
                c.number_format = "#,##0.0"
        ws.cell(r0, len(hdr)).fill = TOT
        r0 += 1
    for j, h in enumerate(hdr, 1):
        c = ws.cell(r0, j, "Grand Total" if j == 1 else
                    (float(piv.iloc[:, j - 6].sum()) if 5 < j < len(hdr)
                     else (float(piv.values.sum()) if j == len(hdr) else None)))
        c.font, c.fill = Font(bold=True), TOT
        if j > 5:
            c.number_format = "#,##0.0"
    ws.freeze_panes = "F5"
    # The filter covers the DATA rows only. Including the Grand Total row would
    # hide it the moment anyone filters to one line - and the grand total is a
    # whole-plant figure that does not recompute on a filter anyway, so it has
    # to stay visible and be read as what it is.
    ws.auto_filter.ref = f"A4:{get_column_letter(len(hdr))}{r0 - 1}"
    for j in range(1, len(hdr) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 18 if j < 6 else 14

    # -------------------------------------------------- 6 Upstream schedule
    up = fl[~fl["department"].isin(["BUILDING", "CURING"])].copy()
    ups = up[["line", "date", "shift", "machine", "machine_name", "item", "item_type", "process",
              "start_time", "end_time", "produce_qty", "UOM", "lot_id",
              "consumption_id", "FG_SKU_CODE"]].sort_values(
        ["line", "start_time", "machine"])
    ws = wb.create_sheet("Upstream schedule")
    _write(ws, ups)

    # ------------------------------------------------------ 7 Assumptions
    arows = []
    for ln in LN:
        for f, stage in ((f"{ln}/phase0_assumptions.csv", "phase 0 - masters"),
                         (f"{ln}/phase3_assumptions.csv", "phase 3 - lot sizing")):
            p = D / f
            if p.exists():
                x = pd.read_csv(p)
                x.insert(0, "line", ln.upper())
                x.insert(1, "stage", stage)
                arows.append(x)
    asm = pd.concat(arows, ignore_index=True) if arows else pd.DataFrame()
    # the run-level assumptions that are not per-item
    extra = pd.DataFrame([
        {"line": "BOTH", "stage": "phase 7 - BR-10", "code": "A-PM-1",
         "item": "(all machines)", "gap": "PM schedule ends 2026-09-29 23:00",
         "value": "assumed available",
         "source": "pm_mtc_downtime_schedule holds plan_month 2026-09 only; the "
                   "last 31 h of this horizon have no PM data"},
        {"line": "BOTH", "stage": "phase 5 - BR-10", "code": "A-PM-2",
         "item": "(upstream machines)", "gap": "no upstream PM rows exist",
         "value": "no upstream downtime",
         "source": "the table carries Curing (56) and Building (20) blocks only; "
                   "stock-prep, mixing, calendering and cutting have none"},
        {"line": "BOTH", "stage": "phase 0", "code": "A-PURCH-1",
         "item": "(411 purchased codes)", "gap": "not routed",
         "value": "assumed available on demand",
         "source": "anything with no routing is contracted out - "
                   "ASSUMED_PURCHASED_ITEMS.csv lists all 411"},
        {"line": "TBR", "stage": "phase 0 - BR-4", "code": "A01-REG",
         "item": "TBR bead winding items", "gap": "routing names no machine",
         "value": "2403, 2404, 2405",
         "source": "all 220 TBR BEAD WINDING routing rows have a blank machines "
                   "cell and the department has no pool; the plant register's "
                   "TBR bead machines are used instead of a pseudo work centre"},
        {"line": "BOTH", "stage": "curing anchor", "code": "A-CURE-1..4",
         "item": "(all SKUs)", "gap": "plan-files carry no curing schedule",
         "value": "derived from the building plan",
         "source": "make_curing_plan.py - cycle time and allowable press from "
                   "the routing, mould positions from Master_WC_Master_Curing"},
    ])
    asm = pd.concat([asm, extra], ignore_index=True) if len(asm) else extra
    ws = wb.create_sheet("Assumptions")
    _write(ws, asm)

    # --------------------------------------------------- 8 Rule compliance
    p = D / "BUSINESS_RULES_COMPLIANCE_v15.csv"
    if p.exists():
        rc = pd.read_csv(p, encoding="utf-8-sig")
        ws = wb.create_sheet("Rule compliance")
        _write(ws, rc)
        for i in range(2, len(rc) + 2):
            v = str(ws.cell(i, 6).value or "")
            ws.cell(i, 6).fill = (GOOD if v.startswith("APPLIED") else
                                  (WARN if v == "PARTIAL" else BAD))

    out = D / a.out
    wb.save(out)
    print(f"  Summary       : PCR built {summ.iloc[0]['built_%']}%  cured "
          f"{summ.iloc[0]['cured_%']}%   |   TBR built {summ.iloc[1]['built_%']}%"
          f"  cured {summ.iloc[1]['cured_%']}%")
    print(f"  Sheets        : {', '.join(wb.sheetnames)}")
    print(f"  Curing rows   : {len(cs):,}   Building rows: {len(bs):,}   "
          f"Upstream rows: {len(ups):,}")
    print(f"\n[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
