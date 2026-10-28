"""Step 1 - the day's building and curing plans, read from the database.

Tables (JK_CTP_PPDS):
    CTP_Schedule_BuildingPlan   one row per machine x shift slice of the building plan
    CTP_Schedule_CuringPlan     one row per press x shift slice of the curing plan
    These two tables ONLY - the pipeline never reads the *_History tables.
Both carry  run_id, plan_id, tyre_type (PCR / TBR), plan_start.

WHICH PLAN
    For plan day D the plan of each line is the one whose plan_start falls on D.
    If the planner saved that day more than once (several run_id), the run with
    the newest created_at is taken and the others are listed. `--plan-run-id`
    pins a run explicitly.
    The building and curing rows of a line must come from the SAME run_id.

WHAT IS WRITTEN
    <plan_dir>/<line>/pe_format/ctp_building_schedule_plan_<line>.xlsx
    <plan_dir>/<line>/pe_format/ctp_curing_schedule_plan_<line>.xlsx
        sheet "Shift Schedule", the same columns the planner's Excel exports
        carry, so the engine reads them exactly as it read the Excel plans:
          building  Machine, Date, Shift, SKUCode, Qty, CO_Mins, StartTime,
                    EndTime, Machine_Group, CO_Type, GT_Code, Description
          curing    Date, Shift, Machine, SKUCode, StartTime, EndTime, Qty,
                    CO_Mins, Mould_Clean_Mins, CycleTime_min, GT_Inventory,
                    Remarks, GT_Code, Description
    <out>/<run>/plan_from_db/  the raw rows as read (CSV) + plan_selection.json
"""
from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd

from . import db

BUILD_T = "CTP_Schedule_BuildingPlan"
CURE_T = "CTP_Schedule_CuringPlan"


def use_history_tables_for_test() -> None:
    """TEST ONLY (run_pipeline.py --test-history-plan, which also demands explicit
    --plan-run-id for both lines). Normal runs never call this."""
    global BUILD_T, CURE_T
    BUILD_T, CURE_T = "CTP_Schedule_BuildingPlan_History", "CTP_Schedule_CuringPlan_History"
BUILD_COLS = ["Machine", "Date", "Shift", "SKUCode", "Qty", "CO_Mins", "StartTime", "EndTime",
              "Machine_Group", "CO_Type", "GT_Code", "Description"]
CURE_COLS = ["Date", "Shift", "Machine", "SKUCode", "StartTime", "EndTime", "Qty", "CO_Mins",
             "Mould_Clean_Mins", "CycleTime_min", "GT_Inventory", "Remarks", "GT_Code",
             "Description"]
LINES = ("PCR", "TBR")


def catalogue(cn) -> pd.DataFrame:
    """Every plan the two tables hold, one row per (table, run_id, line)."""
    out = []
    for t in (BUILD_T, CURE_T):
        d = db.fetch(
            f"SELECT run_id, plan_id, tyre_type, plan_start, COUNT(*) AS n_rows, "
            f"SUM(Qty) AS qty, MIN(StartTime) AS first_start, MAX(EndTime) AS last_end, "
            f"MIN(created_at) AS created_at FROM {t} "
            f"GROUP BY run_id, plan_id, tyre_type, plan_start", cn)
        d.insert(0, "table", t)
        out.append(d)
    c = pd.concat(out, ignore_index=True)
    c["tyre_type"] = c["tyre_type"].astype(str).str.strip().str.upper()
    c["plan_start"] = pd.to_datetime(c["plan_start"])
    c["created_at"] = pd.to_datetime(c["created_at"])
    return c


def select(cat: pd.DataFrame, day, pinned: dict | None = None) -> dict:
    """{line: run_id} of the plan for plant day `day`."""
    pinned = pinned or {}
    day = pd.Timestamp(day).date()
    pick, notes = {}, []
    for ln in LINES:
        if pinned.get(ln):
            rid = pinned[ln]
            if not ((cat["run_id"] == rid) & (cat["tyre_type"] == ln)).any():
                sys.exit(f"[FATAL] --plan-run-id {ln}={rid}: no such {ln} run in {BUILD_T}/{CURE_T}")
            pick[ln] = rid
            continue
        c = cat[(cat["tyre_type"] == ln) & (cat["table"] == BUILD_T)
                & (cat["plan_start"].dt.date == day)].sort_values("created_at")
        if c.empty:
            days = sorted({str(x.date()) for x in cat.loc[cat["tyre_type"] == ln, "plan_start"]})
            sys.exit(f"[FATAL] no {ln} building plan in {BUILD_T} with plan_start on {day}.\n"
                     f"  {ln} plan days in the table: {days or 'none'}\n"
                     f"  run with --date <one of those>, or wait for the planner to save {day}")
        pick[ln] = c["run_id"].iloc[-1]
        if len(c) > 1:
            notes.append(f"{ln}: {len(c)} runs saved for {day}; newest {pick[ln]} taken, "
                         f"ignored {list(c['run_id'].iloc[:-1])}")
    return {"runs": pick, "notes": notes}


def read(cn, line: str, run_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    b = db.fetch(f"SELECT * FROM {BUILD_T} WHERE tyre_type = '{line}' AND run_id = '{run_id}' "
                 f"ORDER BY id", cn)          # the planner's own row order (= its Excel export)
    c = db.fetch(f"SELECT * FROM {CURE_T} WHERE tyre_type = '{line}' AND run_id = '{run_id}' "
                 f"ORDER BY id", cn)
    if b.empty:
        sys.exit(f"[FATAL] {BUILD_T} has no {line} rows for run_id {run_id}")
    if c.empty:
        sys.exit(f"[FATAL] {CURE_T} has no {line} rows for run_id {run_id} - the curing plan "
                 f"must come from the same planner run as the building plan")
    return b, c


def to_engine_format(b: pd.DataFrame, c: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """DB rows -> the planner's Excel "Shift Schedule" columns."""
    B = pd.DataFrame({k: b[k] if k in b.columns else None for k in BUILD_COLS}, index=b.index)
    C = pd.DataFrame({k: c[k] if k in c.columns else None for k in CURE_COLS}, index=c.index)
    if "skuDescription" in c.columns:
        C["Description"] = c["skuDescription"]
    for d in (B, C):
        for k in ("StartTime", "EndTime"):
            d[k] = pd.to_datetime(d[k])
        d["Date"] = pd.to_datetime(d["Date"]).dt.date
        d["Machine"] = d["Machine"].astype(str).str.strip()
        d["SKUCode"] = d["SKUCode"].astype(str).str.strip()
        d["Qty"] = pd.to_numeric(d["Qty"], errors="coerce").fillna(0).astype(int)
    for k in ("CO_Mins", "Mould_Clean_Mins", "CycleTime_min", "GT_Inventory"):
        if k in C.columns:
            C[k] = pd.to_numeric(C[k], errors="coerce")
    B["CO_Mins"] = pd.to_numeric(B["CO_Mins"], errors="coerce")
    return B, C


def checks(line: str, B: pd.DataFrame, C: pd.DataFrame, day) -> list:
    """Plain data checks. Problems are REPORTED; nothing is repaired."""
    out = []
    t0 = pd.Timestamp(day) + pd.Timedelta(hours=7)
    for nm, d in (("building", B), ("curing", C)):
        z = int((d["Qty"] <= 0).sum())
        if z:
            out.append(f"{line} {nm}: {z} row(s) with Qty <= 0 (ignored by the engine)")
        bad = d[d["EndTime"] < d["StartTime"]]
        if len(bad):
            out.append(f"{line} {nm}: {len(bad)} row(s) end before they start")
        early = d[d["StartTime"] < t0]
        if len(early):
            out.append(f"{line} {nm}: {len(early)} row(s) start before {t0} (plan day 07:00)")
    nb = set(B.loc[B["Qty"] > 0, "SKUCode"])
    nc = set(C.loc[C["Qty"] > 0, "SKUCode"])
    if nb - nc:
        out.append(f"{line}: {len(nb - nc)} SKU(s) built but never cured in the plan: "
                   f"{sorted(nb - nc)[:6]}")
    if nc - nb:
        out.append(f"{line}: {len(nc - nb)} SKU(s) cured but not built (GT stock only): "
                   f"{sorted(nc - nb)[:6]}")
    return out


def stage(cn, day, plan_dir: pathlib.Path, raw_dir: pathlib.Path,
          pinned: dict | None = None) -> dict:
    """Read, convert and write the day's plans. Returns the selection record."""
    cat = catalogue(cn)
    sel = select(cat, day, pinned)
    raw_dir.mkdir(parents=True, exist_ok=True)
    cat.to_csv(raw_dir / "plan_catalogue.csv", index=False)
    info = {"plan_day": str(pd.Timestamp(day).date()), "tables": [BUILD_T, CURE_T],
            "notes": list(sel["notes"]), "lines": {}}
    for ln in LINES:
        rid = sel["runs"][ln]
        b, c = read(cn, ln, rid)
        b.to_csv(raw_dir / f"{BUILD_T}_{ln}_{rid}.csv", index=False)
        c.to_csv(raw_dir / f"{CURE_T}_{ln}_{rid}.csv", index=False)
        B, C = to_engine_format(b, c)
        out = plan_dir / ln.lower() / "pe_format"
        out.mkdir(parents=True, exist_ok=True)
        for f in out.glob("ctp_*_schedule_*.xlsx"):
            f.unlink()                       # never let an older plan file win
        with pd.ExcelWriter(out / f"ctp_building_schedule_plan_{ln.lower()}.xlsx",
                            engine="openpyxl") as w:
            B.to_excel(w, sheet_name="Shift Schedule", index=False)
        with pd.ExcelWriter(out / f"ctp_curing_schedule_plan_{ln.lower()}.xlsx",
                            engine="openpyxl") as w:
            C.to_excel(w, sheet_name="Shift Schedule", index=False)
        prod = B[B["Qty"] > 0]
        info["lines"][ln] = {
            "run_id": rid, "plan_id": str(b["plan_id"].iloc[0]),
            "plan_start": str(pd.Timestamp(b["plan_start"].iloc[0])),
            "created_at": str(pd.Timestamp(b["created_at"].min())),
            "building_rows": int(len(B)), "building_tyres": int(prod["Qty"].sum()),
            "building_machines": int(prod["Machine"].nunique()),
            "building_skus": int(prod["SKUCode"].nunique()),
            "curing_rows": int(len(C)), "curing_tyres": int(C.loc[C["Qty"] > 0, "Qty"].sum()),
            "curing_presses": int(C["Machine"].nunique()),
            "first_start": str(min(B["StartTime"].min(), C["StartTime"].min())),
            "last_end": str(max(B["EndTime"].max(), C["EndTime"].max())),
        }
        info["notes"] += checks(ln, B, C, day)
    (raw_dir / "plan_selection.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info
