#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_schedule_from_template.py -- fill the plant schedule TEMPLATE workbook with one run's data.

    python make_schedule_from_template.py <run_dir> <plan_dir> <template.xlsx> <out.xlsx>

Same data as make_schedule_workbook.py, but written INTO a copy of the template through Excel
(Windows + Excel + pywin32 required), so the template's sheet order, column widths, number
formats, filters, freeze panes and the live 'Sheet1' PivotTable (process x machine,
Sum of produce_qty over 'Upstream schedule'!A:K) are kept exactly. The pivot is refreshed.
"""
import math
import os
import shutil
import subprocess
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))


def frames(run, plans):
    if not os.path.exists(os.path.join(run, "DAILY_REPORT.xlsx")):
        subprocess.run([sys.executable, os.path.join(HERE, "daily_report.py"), run, plans], cwd=HERE,
                       stdout=subprocess.DEVNULL, check=True)
    summ = pd.read_excel(os.path.join(run, "DAILY_REPORT.xlsx"), sheet_name="Coverage by plan end")
    daily = pd.read_excel(os.path.join(run, "DAILY_REPORT.xlsx"), sheet_name="Daily PCR TBR")

    cu = pd.read_csv(os.path.join(run, "phase6_curing_revision_updated.csv"),
                     parse_dates=["startTime", "endTime", "kit_ready"])
    cur = cu[["skuCode", "pressNo", "shift", "startTime", "endTime", "qty", "cycleTime",
              "kit_status", "kit_ready", "green_age_h"]].sort_values(["startTime", "pressNo"])
    cur["green_age_h"] = pd.to_numeric(cur["green_age_h"], errors="coerce").round(1)

    fs = pd.read_csv(os.path.join(run, "phase5_floor_schedule_updated.csv"),
                     parse_dates=["start_time", "end_time"], low_memory=False)
    fs["machine"] = fs["machine"].astype(str)
    fs["date"] = fs["date"].astype(str)
    dep = fs["department"].astype(str).str.upper()
    bld = fs[dep == "BUILDING"].rename(columns={"produce_qty": "qty"})
    bld = bld[["date", "shift", "machine", "item", "start_time", "end_time", "qty", "lot_id"]] \
        .sort_values(["start_time", "machine"])
    bld["qty"] = pd.to_numeric(bld["qty"], errors="coerce").round(0)

    up = fs[~dep.isin(["BUILDING", "CURING"])].copy()
    up["produce_qty"] = pd.to_numeric(up["produce_qty"], errors="coerce").round(2)
    extra = {}
    if "batches" in up.columns and up["batches"].notna().any():
        # BR-V1: mixing is planned in BATCHES -> mixing rows report batches (UOM BATCH)
        mix = up[up["batches"].notna()].copy()
        mix["batches"] = pd.to_numeric(mix["batches"], errors="coerce").astype(int)
        mix["kg"] = mix["produce_qty"]
        up.loc[mix.index, "produce_qty"] = mix["batches"]
        up.loc[mix.index, "UOM"] = "BATCH"
        alloc = (mix.groupby(["machine", "item", "item_type", "batch_size_kg"], as_index=False)
                 .agg(batches=("batches", "sum"), kg=("kg", "sum"), runs=("lot_id", "nunique"),
                      first_start=("start_time", "min"), last_end=("end_time", "max"))
                 .sort_values(["machine", "first_start"]))
        tot = (alloc.groupby("machine", as_index=False)[["batches", "kg"]].sum()
               .assign(item="MIXER TOTAL"))
        alloc = (pd.concat([alloc, tot], ignore_index=True)
                 .assign(_t=lambda d: d["item"].eq("MIXER TOTAL"))
                 .sort_values(["machine", "_t"], kind="stable").drop(columns="_t"))
        alloc["kg"] = alloc["kg"].round(2)
        plan = mix.pivot_table(index="machine", columns="date", values="batches", aggfunc="sum",
                               fill_value=0)
        plan["Total"] = plan.sum(axis=1)
        plan.loc["ALL MIXERS"] = plan.sum()
        extra = {"Mixing batches": alloc[["machine", "item", "item_type", "batch_size_kg", "batches",
                                          "kg", "runs", "first_start", "last_end"]],
                 "Mixer batch plan": plan.reset_index()}
    co = cleanouts(run)
    if co is not None:
        extra["Mixer clean-outs"] = co
    rp = revised_plan(run)
    if rp is not None:
        extra["Revised building plan"], extra["Revised daily targets"] = rp
    up = up[["date", "shift", "machine", "item", "item_type", "process", "start_time", "end_time",
             "produce_qty", "UOM", "lot_id"]].sort_values(["start_time", "machine"])
    return {"Summary": summ, "Daily PCR TBR": daily, "Curing schedule": cur,
            "Building schedule": bld, "Upstream schedule": up}, extra


def cleanouts(run):
    """BR-V2: every mixer transition that breaks the butyl-friendly sequence (clean-out charged),
    from the lot-level schedule. None when the rule is off."""
    import yaml
    sys.path.insert(0, HERE)
    import butyl_rule
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    rule = butyl_rule.load(cfg)
    if not rule.enabled:
        return None
    s = pd.read_csv(os.path.join(run, "phase5_schedule_updated.csv"), low_memory=False,
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
    s = s[s["department"].astype(str).str.upper().str.contains("MIX")].copy()
    s["machine"], s["item"] = s["machine"].astype(str).str.strip(), s["item"].astype(str).str.strip()
    rows = []
    for m, g in s.groupby("machine"):
        r = list(g.sort_values("scheduled_start").itertuples())
        for a, b in zip(r, r[1:]):
            hit, why = rule.cleanout(a.item, b.item)
            if hit:
                req, basis = rule.cleanout_minutes(a.item, b.item, m)
                rows.append({"machine": m, "from_compound": a.item, "to_compound": b.item,
                             "cleanout_start": a.scheduled_finish, "next_start": b.scheduled_start,
                             "gap_sec": round((b.scheduled_start - a.scheduled_finish).total_seconds()),
                             "cleanout_sec": round((req or rule.cleanout_min) * 60), "basis": basis,
                             "reason": why, "from_lot": a.lot_id, "to_lot": b.lot_id})
    cols = ["machine", "from_compound", "to_compound", "cleanout_start", "next_start", "gap_sec",
            "cleanout_sec", "basis", "reason", "from_lot", "to_lot"]
    return pd.DataFrame(rows, columns=cols).sort_values(["machine", "cleanout_start"])


def revised_plan(run):
    """BR-V7: the plan-feasible (re-levelled) building plan -- every plan row with its original
    slot and its feasible start, plus plan vs feasible tyres per plant day and line. Needs the
    building-echo gate log copied into the run folder; None when it is absent."""
    import yaml
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    g_path = os.path.join(run, "phase5_building_echo_gates.csv")
    if not (cfg.get("building_plan_feasible") or {}).get("enabled") or not os.path.exists(g_path):
        return None
    g = pd.read_csv(g_path, parse_dates=["plan_start", "start"])
    g["line"] = g["tbm"].astype(str).map(lambda m: "TBR" if m.startswith("TBM-") else "PCR")
    g["shift_h"] = ((g["start"] - g["plan_start"]).dt.total_seconds() / 3600).round(1)
    g["move"] = g["shift_h"].map(lambda h: "earlier" if h < -0.5 else ("later" if h > 0.5 else "on time"))
    day = lambda s: (s - pd.Timedelta(hours=7)).dt.strftime("%Y-%m-%d")
    g["plan_day"], g["feasible_day"] = day(g["plan_start"]), day(g["start"])
    rows = g[["line", "tbm", "sku", "gt", "qty", "plan_day", "plan_start", "feasible_day", "start",
              "shift_h", "move", "binding_item"]].rename(columns={"start": "feasible_start",
                                                                 "binding_item": "last_kit_item"})
    rows = rows.sort_values(["feasible_start", "tbm"])
    p = g.groupby(["plan_day", "line"])["qty"].sum().rename("plan_qty")
    f = g.groupby(["feasible_day", "line"])["qty"].sum().rename("feasible_qty")
    f.index = f.index.set_names(["plan_day", "line"])
    tgt = pd.concat([p, f], axis=1).fillna(0).astype(int).reset_index().rename(columns={"plan_day": "day"})
    tgt["difference"] = tgt["feasible_qty"] - tgt["plan_qty"]
    return rows, tgt.sort_values(["line", "day"])


def add_sheet(wb, name, df):
    """Append a plain sheet (header + values) after the template's sheets, template style."""
    ws = wb.Worksheets.Add(None, wb.Worksheets(wb.Worksheets.Count))   # (Before, After): append at end
    ws.Name = name
    ncol = len(df.columns)
    ws.Range(ws.Cells(1, 1), ws.Cells(1, ncol)).NumberFormat = "@"
    ws.Range(ws.Cells(1, 1), ws.Cells(1, ncol)).Value = [tuple(str(c) for c in df.columns)]
    if len(df):
        ws.Range(ws.Cells(2, 1), ws.Cells(len(df) + 1, ncol)).Value = \
            [tuple(_cell(v) for v in r) for r in df.itertuples(index=False, name=None)]
        for c, col in enumerate(df.columns, 1):
            if "start" in str(col) or "end" in str(col):
                ws.Range(ws.Cells(2, c), ws.Cells(len(df) + 1, c)).NumberFormat = "yyyy/mm/dd\\ hh:mm:ss"
    ws.Columns.AutoFit()
    ws.Activate()
    xl = wb.Application
    xl.ActiveWindow.SplitRow, xl.ActiveWindow.SplitColumn = 1, 0
    xl.ActiveWindow.FreezePanes = True
    return len(df)


_EXCEL_EPOCH = pd.Timestamp("1899-12-30")


def _cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        # Excel serial date, NOT a Python datetime: pywin32 treats a naive datetime as UTC and
        # Excel shows it in local time (IST: every time 5 h 30 min early). The column's
        # date number format (taken from the template) renders the serial as a date-time.
        return (v.floor("s") - _EXCEL_EPOCH) / pd.Timedelta(days=1)
    if hasattr(v, "item"):                      # numpy scalar -> python
        return v.item()
    return v


def fill(ws, df):
    """Replace the data under the template header, keeping header, widths and column formats."""
    ncol = len(df.columns)
    hdr = [ws.Cells(1, c + 1).Value for c in range(ncol)]
    if hdr != list(df.columns):
        raise SystemExit(f"{ws.Name}: template header {hdr} != data columns {list(df.columns)}")
    fmts = [ws.Cells(2, c + 1).NumberFormat for c in range(ncol)]   # template's per-column format
    # columns the template stores as TEXT (machine '1002', date '2026-09-29'): Excel would coerce
    # them to numbers / dates on entry, so enter them under '@' and restore the format after.
    text = [isinstance(ws.Cells(2, c + 1).Value, str) for c in range(ncol)]
    last = ws.UsedRange.Row + ws.UsedRange.Rows.Count - 1
    if last >= 2:
        ws.Range(ws.Rows(2), ws.Rows(last)).Delete()
    n = len(df)
    if n:
        rows = [tuple(_cell(v) for v in r) for r in df.itertuples(index=False, name=None)]
        for c in range(ncol):
            if text[c]:
                ws.Range(ws.Cells(2, c + 1), ws.Cells(n + 1, c + 1)).NumberFormat = "@"
        rng = ws.Range(ws.Cells(2, 1), ws.Cells(n + 1, ncol))
        rng.Value = rows
        for c, f in enumerate(fmts):
            ws.Range(ws.Cells(2, c + 1), ws.Cells(n + 1, c + 1)).NumberFormat = f
    return n


def main():
    run, plans, template, out = sys.argv[1:5]
    data, extra = frames(run, plans)
    out = os.path.abspath(out)
    shutil.copyfile(template, out)

    import win32com.client as win32
    xl = win32.DispatchEx("Excel.Application")
    xl.Visible = False
    xl.DisplayAlerts = False
    try:
        wb = xl.Workbooks.Open(out)
        for name, df in data.items():
            ws = wb.Worksheets(name)
            flt = ws.AutoFilterMode and ws.AutoFilter.Range.Rows.Count > 1   # filter over data rows?
            if ws.AutoFilterMode:
                ws.AutoFilterMode = False
                hdr_only = not flt
            else:
                hdr_only = None
            n = fill(ws, df)
            if hdr_only is not None:                # re-apply the template's filter shape
                ncol = len(df.columns)
                ws.Range(ws.Cells(1, 1), ws.Cells(1 if hdr_only else n + 1, ncol)).AutoFilter()
            print(f"  {name:<18} {n:>6,} rows")
        ps = wb.Worksheets("Sheet1")
        widths = [ps.Columns(c).ColumnWidth for c in range(1, 15)]     # refresh autofits; keep template's
        pt = ps.PivotTables(1)
        pt.PivotCache().Refresh()
        pt.RefreshTable()
        for c, w in enumerate(widths, 1):
            ps.Columns(c).ColumnWidth = w
        print(f"  Sheet1 pivot       {pt.TableRange1.Address} (source {pt.SourceData})")
        for name, df in extra.items():
            print(f"  {name:<18} {add_sheet(wb, name, df):>6,} rows  (added sheet)")
        wb.Worksheets(1).Activate()
        wb.Save()
        wb.Close(False)
    finally:
        xl.Quit()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
