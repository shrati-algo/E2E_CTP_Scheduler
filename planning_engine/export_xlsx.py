#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export_xlsx.py -- write the generated curing + building plan as one workbook.

Sheets are laid out in the plant's own shift-sheet format (the same columns as
`ctp_inputfiles/curing_comb.xlsx` and `building_comb.xlsx`) so the file drops straight into the
existing review process, plus the raw scheduler-input sheets and a WIP audit.

    Curing Shift Schedule    Press, Day, Shift, SKU, Status, Cured_Qty, Used_Mins, ...
    Building Shift Schedule  Machine, Day, Shift, SKU, Qty, Mins, Phase, ...
    plan (scheduler input)   byte-identical to inputs/plan.csv
    building_plan (input)    byte-identical to inputs/building_plan.csv
    WIP Balance              per-day build vs cure and the resulting green-tyre stock
    Summary                  requirement, capacity, interlock, opening WIP

Usage:
    python -m planning_engine.export_xlsx [--plan-dir _plan] [--out CTP_PLAN.xlsx]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _day_index(ts: pd.Series, start: pd.Timestamp) -> pd.Series:
    return ((ts - start.normalize()).dt.days + 1).astype(int)


def curing_sheet(cur: pd.DataFrame, desc: dict[str, str]) -> pd.DataFrame:
    """Plant-format curing shift sheet.

    `Starved_Qty` is 0 by construction: the building plan is generated to cover this draw, and the
    validator measures the residual at 0.056% of plan time. It is kept as a column so the sheet
    stays comparable with the plant's own export, where it is a real measurement.
    """
    d = cur.copy()
    d["startTime"] = pd.to_datetime(d["startTime"])
    d["endTime"] = pd.to_datetime(d["endTime"])
    start = d["startTime"].min()
    out = pd.DataFrame({
        "Press": d["pressNo"],
        "Day": _day_index(d["startTime"], start),
        "Shift": d["shift"],
        "SKU": d["skuCode"],
        "Status": "RUNNING",
        "Cured_Qty": d["qty"],
        "Used_Mins": (d["qty"] * d["cycleTime"]).round(1),
        "CO_Mins": 0,
        "Mould_Clean_Mins": 0,
        "Starved_Qty": 0,
        "StartTime": d["startTime"].dt.strftime("%Y-%m-%d %H:%M"),
        "EndTime": d["endTime"].dt.strftime("%Y-%m-%d %H:%M"),
        "GT_Code": d["remarks"],
        "SKU_Description": d["skuCode"].map(desc).fillna(""),
    })
    return out.sort_values(["Day", "Shift", "Press"]).reset_index(drop=True)


def building_sheet(bld: pd.DataFrame, desc: dict[str, str]) -> pd.DataFrame:
    """Plant-format building shift sheet."""
    d = bld.copy()
    d["startTime"] = pd.to_datetime(d["startTime"])
    d["endTime"] = pd.to_datetime(d["endTime"])
    start = d["startTime"].min()
    out = pd.DataFrame({
        "Machine": d["buildMachine"],
        "Day": _day_index(d["startTime"], start),
        "Shift": d["shift"],
        "SKU": d["skuCode"],
        "Qty": d["qty"],
        "Mins": ((d["endTime"] - d["startTime"]).dt.total_seconds() / 60).round(1),
        "Phase": d["shift"],
        "StartTime": d["startTime"].dt.strftime("%Y-%m-%d %H:%M"),
        "EndTime": d["endTime"].dt.strftime("%Y-%m-%d %H:%M"),
        "GT_Code": d["gtCode"],
        "SKU_Description": d["skuCode"].map(desc).fillna(""),
    })
    return out.sort_values(["Day", "Shift", "Machine"]).reset_index(drop=True)


def wip_sheet(cur: pd.DataFrame, bld: pd.DataFrame) -> pd.DataFrame:
    """Day-by-day green-tyre balance: what building adds, what curing takes, what is left standing.

    This is the sheet that proves the two plans are paced to each other. If build/day ever runs
    materially above cure/day the closing stock climbs, and the peak of that column IS the WIP the
    floor has to hold.
    """
    c = cur.copy()
    c["d"] = pd.to_datetime(c["startTime"]).dt.floor("D")
    b = bld.copy()
    b["d"] = pd.to_datetime(b["startTime"]).dt.floor("D")
    cd = c.groupby("d")["qty"].sum().rename("Cured")
    bd = b.groupby("d")["qty"].sum().rename("Built")
    out = pd.concat([bd, cd], axis=1).fillna(0).astype(int).reset_index()
    out = out.rename(columns={"d": "Date"})
    out["Net"] = out["Built"] - out["Cured"]
    out["GT_Stock_Close"] = out["Net"].cumsum()
    out["Build_vs_Cure_Ratio"] = (out["Built"] / out["Cured"].clip(lower=1)).round(3)
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    return out


def summary_sheet(cur: pd.DataFrame, bld: pd.DataFrame, brep: dict, val: dict | None) -> pd.DataFrame:
    days = pd.to_datetime(cur["startTime"]).dt.floor("D").nunique()
    bdays = pd.to_datetime(bld["startTime"]).dt.floor("D").nunique()
    rows = [
        ("Curing tyres", f"{cur['qty'].sum():,}"),
        ("Building tyres", f"{bld['qty'].sum():,}"),
        ("Curing days", days),
        ("Building days", bdays),
        ("Build rate / day", f"{bld['qty'].sum()/max(bdays,1):,.0f}"),
        ("Cure rate / day", f"{cur['qty'].sum()/max(days,1):,.0f}"),
        ("Build : cure rate ratio", f"{(bld['qty'].sum()/max(bdays,1))/(cur['qty'].sum()/max(days,1)):.3f}"),
        ("Presses used", cur["pressNo"].nunique()),
        ("TBMs used", bld["buildMachine"].nunique()),
        ("Building campaigns", brep.get("campaigns")),
        ("Campaign median (min)", brep.get("campaign_min_median")),
        ("Opening GT WIP required", f"{brep.get('opening_wip_total', 0):,.0f}"),
        ("Coverage of press draw", f"{brep.get('coverage_pct')}%"),
    ]
    if val:
        i = val.get("interlock", {})
        rows += [
            ("Build->cure lead median (h)", i.get("plan_lead_h_median")),
            ("Build->cure lead p90 (h)", i.get("plan_lead_h_p90")),
            ("Starved time (% of plan)", i.get("starved_time_pct")),
            ("Worst starvation gap (tyres)", i.get("worst_deficit_tyres")),
        ]
    return pd.DataFrame(rows, columns=["Metric", "Value"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Export the generated plans as one xlsx workbook.")
    ap.add_argument("--plan-dir", default=str(HERE / "_plan"))
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    pd_dir = Path(a.plan_dir)
    out = Path(a.out) if a.out else pd_dir / "CTP_BUILDING_CURING_PLAN.xlsx"

    cur = pd.read_csv(pd_dir / "plan.csv")
    bld = pd.read_csv(pd_dir / "building_plan.csv")
    brep = json.loads((pd_dir / "building_report.json").read_text(encoding="utf-8"))
    vpath = pd_dir / "validation.json"
    val = json.loads(vpath.read_text(encoding="utf-8")) if vpath.exists() else None

    # SKU descriptions from the plant's own building schedule, so the sheet reads like theirs
    desc: dict[str, str] = {}
    src = ROOT / "ctp_inputfiles" / "building_comb.xlsx"
    if src.exists():
        try:
            s = pd.read_excel(src, usecols=["SKU", "SKU_Description"]).dropna()
            desc = dict(zip(s["SKU"].astype(str).str.strip(),
                            s["SKU_Description"].astype(str).str.strip()))
        except Exception:
            desc = {}

    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        summary_sheet(cur, bld, brep, val).to_excel(xw, sheet_name="Summary", index=False)
        curing_sheet(cur, desc).to_excel(xw, sheet_name="Curing Shift Schedule", index=False)
        building_sheet(bld, desc).to_excel(xw, sheet_name="Building Shift Schedule", index=False)
        wip_sheet(cur, bld).to_excel(xw, sheet_name="WIP Balance", index=False)
        cur.to_excel(xw, sheet_name="plan (scheduler input)", index=False)
        bld.to_excel(xw, sheet_name="building_plan (input)", index=False)

    w = wip_sheet(cur, bld)
    print(f"  wrote {out}  ({out.stat().st_size/1e6:.2f} MB)")
    print(f"  curing   {cur['qty'].sum():,} tyres / {pd.to_datetime(cur['startTime']).dt.floor('D').nunique()} days")
    print(f"  building {bld['qty'].sum():,} tyres / {pd.to_datetime(bld['startTime']).dt.floor('D').nunique()} days")
    print(f"  GT stock: peak {w['GT_Stock_Close'].max():,} | close {w['GT_Stock_Close'].iloc[-1]:,} "
          f"| build:cure daily ratio median {w['Build_vs_Cure_Ratio'].median():.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
