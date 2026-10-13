"""
validate_phase5.py
Phase 5 Forward Placement Validation for Tire Manufacturing MRP Pipeline
Outputs: phase5_validation_report.xlsx
"""

import pandas as pd
import numpy as np
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

# ── Paths ────────────────────────────────────────────────────────────────────
BASE    = Path(__file__).parent
INPUTS  = BASE / "output/PHASEWISE/00_INPUTS"
PH2_DIR = BASE / "output/PHASEWISE/PHASE2_LOTS"
PH3_DIR = BASE / "output/PHASEWISE/PHASE3_DAG"
PH4_DIR = BASE / "output/PHASEWISE/PHASE4_CPM"
PH5_DIR = BASE / "output/PHASEWISE/PHASE5_PLACEMENT"

OUT_FILE = BASE / "phase5_validation_report.xlsx"

# ── Colour constants ─────────────────────────────────────────────────────────
PASS_FILL = PatternFill("solid", fgColor="C6EFCE")
PASS_FONT = Font(color="276221")
FAIL_FILL = PatternFill("solid", fgColor="FFC7CE")
FAIL_FONT = Font(color="9C0006")
WARN_FILL = PatternFill("solid", fgColor="FFEB9C")
WARN_FONT = Font(color="9C5700")
HDR_FILL  = PatternFill("solid", fgColor="1F3864")
HDR_FONT  = Font(color="FFFFFF", bold=True)

TOLERANCE_SEC = 1 / 3600    # 1 second in hours
PROC_TOL      = 0.10        # 10% tolerance for processing time
CHNG_TOL_MIN  = 5           # 5-minute tolerance for changeover


def style_status_cell(cell, status: str):
    if status == "PASS":
        cell.fill, cell.font = PASS_FILL, PASS_FONT
    elif status == "FAIL":
        cell.fill, cell.font = FAIL_FILL, FAIL_FONT
    else:
        cell.fill, cell.font = WARN_FILL, WARN_FONT


def write_header_row(ws, headers, row=1):
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=col, value=h)
        c.fill = HDR_FILL
        c.font = HDR_FONT
        c.alignment = Alignment(horizontal="center", wrap_text=True)


def auto_col_widths(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val = str(cell.value) if cell.value is not None else ""
                max_len = max(max_len, len(val))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max(max_len + 2, 10), 60)


def write_detail_sheet(wb, sheet_name: str, description: str,
                        df: pd.DataFrame, pass_col: str = "Status"):
    ws = wb.create_sheet(title=sheet_name)
    ws.cell(row=1, column=1, value=description).font = Font(bold=True)

    if pass_col in df.columns:
        n_pass = (df[pass_col] == "PASS").sum()
        n_fail = (df[pass_col] == "FAIL").sum()
        n_warn = (df[pass_col] == "WARN").sum()
        ws.cell(row=2, column=1, value=f"PASS: {n_pass}  FAIL: {n_fail}  WARN: {n_warn}")
    else:
        ws.cell(row=2, column=1, value=f"Rows: {len(df)}")

    note = ""
    if len(df) > 10000 and pass_col in df.columns:
        fails = df[df[pass_col] == "FAIL"]
        passes = df[df[pass_col] == "PASS"].head(500)
        warns  = df[df[pass_col] == "WARN"]
        df = pd.concat([fails, passes, warns]).reset_index(drop=True)
        note = " [TRUNCATED: FAILs + first 500 PASSes shown]"
    if note:
        ws.cell(row=2, column=1).value += note

    if df.empty:
        ws.cell(row=3, column=1, value="No data")
        return

    headers = list(df.columns)
    write_header_row(ws, headers, row=3)

    for r_idx, row_data in enumerate(df.itertuples(index=False), start=4):
        for c_idx, val in enumerate(row_data, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            if headers[c_idx - 1] == pass_col:
                style_status_cell(cell, str(val))

    auto_col_widths(ws)


def parse_machines(val):
    """Parse a routing machines field into a list of machine strings."""
    if pd.isna(val) or val is None:
        return []
    s = str(val).strip()
    # Try JSON parse
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(m).strip() for m in parsed]
    except Exception:
        pass
    # Try bracket-stripped comma-separated
    s = s.strip("[]").replace('"', "").replace("'", "")
    parts = [p.strip() for p in re.split(r"[,;]+", s) if p.strip()]
    return parts


def build_changeover_lookup(changeover_matrix, changeover_lookup):
    """Build a dict: (machine, from_item, to_item) -> changeover_min."""
    lookup = {}
    # From changeover_matrix
    if not changeover_matrix.empty:
        for _, r in changeover_matrix.iterrows():
            key = (str(r.get("machine", "")).strip(),
                   str(r.get("from_MaterialCode_O", "")).strip(),
                   str(r.get("to_MaterialCode_O", "")).strip())
            try:
                lookup[key] = float(r.get("changeover_time_min", 0))
            except Exception:
                lookup[key] = 0.0
    # From changeover_lookup
    if not changeover_lookup.empty:
        for _, r in changeover_lookup.iterrows():
            key = (str(r.get("line", "")).strip(),
                   str(r.get("from_item", "")).strip(),
                   str(r.get("to_item", "")).strip())
            try:
                lookup.setdefault(key, float(r.get("changeover_min", 0)))
            except Exception:
                lookup.setdefault(key, 0.0)
    return lookup


def load_data():
    print("Loading input files …")
    cpm      = pd.read_csv(PH4_DIR / "phase4_lot_times_updated.csv", low_memory=False)
    lots     = pd.read_csv(PH2_DIR / "phase2_lots_updated.csv", low_memory=False)
    edges    = pd.read_csv(PH3_DIR / "phase3_dag_edges_updated.csv", low_memory=False)
    routing  = pd.read_csv(INPUTS  / "routing.csv", low_memory=False)
    chng_mx  = pd.read_csv(INPUTS  / "changeover_matrix.csv", low_memory=False)
    chng_lk  = pd.read_csv(INPUTS  / "changeover_lookup.csv", low_memory=False)
    schedule = pd.read_csv(PH5_DIR / "phase5_schedule_updated.csv", low_memory=False)
    floor    = pd.read_csv(PH5_DIR / "phase5_floor_schedule_updated.csv", low_memory=False)

    # Parse datetime columns
    for col in ["EST", "EFT", "LST", "LFT", "AEST"]:
        if col in cpm.columns:
            cpm[col] = pd.to_datetime(cpm[col], errors="coerce")
    for col in ["scheduled_start", "scheduled_finish"]:
        if col in schedule.columns:
            schedule[col] = pd.to_datetime(schedule[col], errors="coerce")

    print(f"  cpm:      {len(cpm):>6} rows")
    print(f"  lots:     {len(lots):>6} rows")
    print(f"  edges:    {len(edges):>6} rows")
    print(f"  routing:  {len(routing):>6} rows")
    print(f"  schedule: {len(schedule):>6} rows")
    print(f"  floor:    {len(floor):>6} rows")
    return cpm, lots, edges, routing, chng_mx, chng_lk, schedule, floor


# ── Validation functions ─────────────────────────────────────────────────────

def v01_all_cpm_lots_scheduled(cpm, schedule):
    print("V01 – All lots from Phase 4 are scheduled …")
    sched_ids = set(schedule["lot_id"].astype(str))
    cpm_ids   = set(cpm["lot_id"].astype(str))
    # Build lookup for item_code from cpm
    cpm_item  = cpm.set_index("lot_id")["item_code"].to_dict() if "item_code" in cpm.columns else {}
    rows = []
    for lid in cpm_ids:
        in_sched = lid in sched_ids
        rows.append({
            "lot_id":      lid,
            "item_code":   cpm_item.get(lid, ""),
            "In_Schedule": in_sched,
            "Status":      "PASS" if in_sched else "FAIL",
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = f"{n_fail} CPM lots missing from schedule" if n_fail else "All CPM lots scheduled"
    return df, n_pass, n_fail, note


def v02_valid_machine_per_routing(schedule, routing):
    print("V02 – Every lot assigned to valid machine per routing …")
    # Build routing machine map: routed_product -> list of valid machines
    rt_map = {}
    for _, r in routing.iterrows():
        prod = str(r.get("routed_product", "")).strip()
        machines = parse_machines(r.get("machines", ""))
        if prod not in rt_map:
            rt_map[prod] = set()
        rt_map[prod].update(machines)

    rows = []
    for _, r in schedule.iterrows():
        item    = str(r.get("item", "")).strip()
        machine = str(r.get("machine", "")).strip()
        valid   = rt_map.get(item, set())
        if not valid:
            status = "WARN"
            valid_str = "Not in routing"
        else:
            ok = machine in valid
            status = "PASS" if ok else "FAIL"
            valid_str = ", ".join(sorted(valid))
        rows.append({
            "lot_id":        r.get("lot_id", ""),
            "item":          item,
            "machine":       machine,
            "valid_machines": valid_str[:200],
            "Machine_Valid": machine in rt_map.get(item, set()),
            "Status":        status,
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = f"{n_fail} lots on invalid machines" if n_fail else "All machines valid per routing"
    return df, n_pass, n_fail, note


def v03_no_machine_overlaps(schedule):
    print("V03 – No overlapping lots on same machine …")
    rows = []
    for machine, grp in schedule.groupby("machine"):
        grp_sorted = grp.sort_values("scheduled_start").reset_index(drop=True)
        for i in range(len(grp_sorted) - 1):
            r1 = grp_sorted.iloc[i]
            r2 = grp_sorted.iloc[i + 1]
            end1   = r1["scheduled_finish"]
            start2 = r2["scheduled_start"]
            try:
                overlap_h = (end1 - start2).total_seconds() / 3600
                if overlap_h > TOLERANCE_SEC:
                    status = "FAIL"
                else:
                    status = "PASS"
                    overlap_h = max(overlap_h, 0)
            except Exception:
                overlap_h, status = None, "WARN"
            if status != "PASS":
                rows.append({
                    "machine":           machine,
                    "lot_id_1":          r1.get("lot_id", ""),
                    "scheduled_finish_1": str(end1),
                    "lot_id_2":          r2.get("lot_id", ""),
                    "scheduled_start_2": str(start2),
                    "Overlap_h":         round(overlap_h, 4) if overlap_h is not None else None,
                    "Status":            status,
                })
    n_fail = len(rows)
    n_pass_val = len(schedule) - n_fail
    if not rows:
        rows.append({"machine": "ALL", "lot_id_1": "", "scheduled_finish_1": "",
                     "lot_id_2": "", "scheduled_start_2": "", "Overlap_h": 0, "Status": "PASS"})
    df = pd.DataFrame(rows)
    note = f"{n_fail} overlap violations found" if n_fail else "No machine overlaps detected"
    return df, n_pass_val, n_fail, note


def v04_precedence_maintained(edges, schedule):
    print("V04 – Precedence constraints maintained …")
    sched_idx = schedule.set_index("lot_id")[["scheduled_start", "scheduled_finish"]].copy()
    rows = []
    for _, e in edges.iterrows():
        prod = str(e["producer_lot"])
        cons = str(e["consumer_lot"])
        try:
            prod_fin  = sched_idx.loc[prod, "scheduled_finish"] if prod in sched_idx.index else None
            cons_start= sched_idx.loc[cons, "scheduled_start"]  if cons in sched_idx.index else None
            if prod_fin is None or cons_start is None or pd.isna(prod_fin) or pd.isna(cons_start):
                status = "WARN"
                gap_h  = None
            else:
                gap_h  = (cons_start - prod_fin).total_seconds() / 3600
                status = "PASS" if gap_h >= -TOLERANCE_SEC else "FAIL"
        except Exception:
            gap_h, status = None, "WARN"
        rows.append({
            "producer_lot":    prod,
            "consumer_lot":    cons,
            "producer_finish": str(prod_fin) if prod_fin is not None else "",
            "consumer_start":  str(cons_start) if cons_start is not None else "",
            "Gap_h":           round(gap_h, 4) if gap_h is not None else None,
            "Status":          status,
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = f"{n_fail} precedence violations" if n_fail else "All precedence constraints satisfied"
    return df, n_pass, n_fail, note


def v05_changeover_time_applied(schedule, chng_mx, chng_lk):
    print("V05 – Correct changeover time between consecutive lots …")
    co_lookup = build_changeover_lookup(chng_mx, chng_lk)
    rows = []
    for machine, grp in schedule.groupby("machine"):
        grp_sorted = grp.sort_values("scheduled_start").reset_index(drop=True)
        for i in range(len(grp_sorted) - 1):
            r1 = grp_sorted.iloc[i]
            r2 = grp_sorted.iloc[i + 1]
            from_item = str(r1.get("item", "")).strip()
            to_item   = str(r2.get("item", "")).strip()
            machine_s = str(machine).strip()
            try:
                gap_min = (r2["scheduled_start"] - r1["scheduled_finish"]).total_seconds() / 60
            except Exception:
                gap_min = None
            # Look up expected changeover
            expected = (co_lookup.get((machine_s, from_item, to_item)) or
                        co_lookup.get((machine_s, from_item, to_item), None))
            # Try all combos
            if expected is None:
                for key in [(machine_s, from_item, to_item)]:
                    expected = co_lookup.get(key)
                    if expected is not None:
                        break
            if expected is None:
                status = "WARN"
                exp_str = "N/A"
            else:
                if gap_min is not None:
                    status = "PASS" if gap_min >= (expected - CHNG_TOL_MIN) else "FAIL"
                else:
                    status = "WARN"
                exp_str = str(expected)
            rows.append({
                "machine":                machine_s,
                "from_lot":               r1.get("lot_id", ""),
                "to_lot":                 r2.get("lot_id", ""),
                "from_item":              from_item,
                "to_item":                to_item,
                "gap_min":                round(gap_min, 2) if gap_min is not None else None,
                "expected_changeover_min": exp_str,
                "Status":                 status,
            })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = f"{n_fail} changeover violations" if n_fail else "All changeovers respected"
    return df, n_pass, n_fail, note


def v06_processing_time_matches_routing(schedule, lots, routing):
    print("V06 – Processing time matches routing standards …")
    # Build routing map: routed_product -> (proc_time, proc_time_UOM, batch_size, efficiency)
    rt_map = {}
    for _, r in routing.iterrows():
        prod = str(r.get("routed_product", "")).strip()
        try:
            pt  = float(r.get("proc_time", 0))
            uom = str(r.get("proc_time_UOM", "SEC")).strip().upper()
            bs  = float(r.get("batch_size", 1)) or 1
            eff = float(r.get("efficiency", 1)) if pd.notna(r.get("efficiency")) else 1.0
            rt_map[prod] = (pt, uom, bs, eff)
        except Exception:
            pass

    # Build lots qty map
    lots_qty = {}
    if "lot_id" in lots.columns and "lot_qty" in lots.columns:
        for _, r in lots.iterrows():
            lots_qty[str(r["lot_id"])] = r["lot_qty"]

    rows = []
    for _, r in schedule.iterrows():
        item   = str(r.get("item", "")).strip()
        lot_id = str(r.get("lot_id", "")).strip()
        actual_dur_h = r.get("duration_h", None)
        lot_qty = lots_qty.get(lot_id, None)
        if item in rt_map:
            pt, uom, bs, eff = rt_map[item]
            # Convert proc_time to hours
            if uom == "SEC":
                pt_h = pt / 3600
            elif uom == "MIN":
                pt_h = pt / 60
            else:
                pt_h = pt  # assume hours
            if lot_qty is not None:
                try:
                    expected_h = (float(lot_qty) / bs) * pt_h / (eff if eff > 0 else 1)
                except Exception:
                    expected_h = None
            else:
                try:
                    expected_h = pt_h
                except Exception:
                    expected_h = None
            if expected_h is not None and actual_dur_h is not None:
                try:
                    diff_pct = abs(float(actual_dur_h) - expected_h) / (expected_h if expected_h > 0 else 1)
                    status = "PASS" if diff_pct <= PROC_TOL else "WARN"
                except Exception:
                    diff_pct = None
                    status = "WARN"
            else:
                diff_pct = None
                status = "WARN"
        else:
            pt, uom, bs, eff, expected_h, diff_pct = None, None, None, None, None, None
            status = "WARN"

        rows.append({
            "lot_id":             lot_id,
            "item":               item,
            "lot_qty":            lot_qty,
            "batch_size":         bs if item in rt_map else None,
            "proc_time":          pt,
            "expected_duration_h": round(expected_h, 4) if expected_h is not None else None,
            "actual_duration_h":  actual_dur_h,
            "Status":             status,
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = f"{n_fail} lots with duration mismatch > 10%" if n_fail else "All durations within tolerance"
    return df, n_pass, n_fail, note


def v07_start_before_finish(schedule):
    print("V07 – Start Time < End Time, no negative durations …")
    rows = []
    for _, r in schedule.iterrows():
        issues = []
        try:
            if pd.notna(r["scheduled_start"]) and pd.notna(r["scheduled_finish"]):
                if r["scheduled_start"] >= r["scheduled_finish"]:
                    issues.append("start>=finish")
            else:
                issues.append("null_time")
        except Exception:
            issues.append("parse_error")
        try:
            if float(r.get("duration_h", 1)) <= 0:
                issues.append("duration<=0")
        except Exception:
            pass
        if issues:
            rows.append({
                "lot_id":           r.get("lot_id", ""),
                "item":             r.get("item", ""),
                "machine":          r.get("machine", ""),
                "scheduled_start":  str(r.get("scheduled_start", "")),
                "scheduled_finish": str(r.get("scheduled_finish", "")),
                "duration_h":       r.get("duration_h", ""),
                "Issues":           "; ".join(issues),
                "Status":           "FAIL",
            })
    n_fail = len(rows)
    n_pass = len(schedule) - n_fail
    if not rows:
        rows.append({"lot_id": "ALL", "item": "", "machine": "",
                     "scheduled_start": "", "scheduled_finish": "",
                     "duration_h": "", "Issues": "None", "Status": "PASS"})
    df = pd.DataFrame(rows)
    note = f"{n_fail} rows with invalid times" if n_fail else "All start/finish times valid"
    return df, n_pass, n_fail, note


def v08_valid_machine_and_operation(schedule, routing):
    print("V08 – Every scheduled lot has valid machine and operation …")
    valid_operations = set(str(v).strip() for v in routing["operation_name"].dropna().unique())
    valid_machines   = set()
    for _, r in routing.iterrows():
        for m in parse_machines(r.get("machines", "")):
            valid_machines.add(m.strip())

    rows = []
    for _, r in schedule.iterrows():
        machine   = str(r.get("machine", "")).strip()
        operation = str(r.get("operation", "")).strip()
        mach_ok   = machine in valid_machines
        op_ok     = operation in valid_operations
        if not mach_ok or not op_ok:
            status = "FAIL"
        else:
            status = "PASS"
        rows.append({
            "lot_id":          r.get("lot_id", ""),
            "item":            r.get("item", ""),
            "machine":         machine,
            "operation":       operation,
            "Machine_Valid":   mach_ok,
            "Operation_Valid": op_ok,
            "Status":          status,
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = f"{n_fail} lots with invalid machine/operation" if n_fail else "All machines and operations valid"
    return df, n_pass, n_fail, note


def v09_total_qty_matches(lots, floor):
    print("V09 – Total scheduled quantity = total lot quantity …")
    try:
        total_lot_qty = lots["lot_qty"].sum()
    except Exception:
        total_lot_qty = None
    try:
        total_sched_qty = floor["produce_qty"].sum()
    except Exception:
        total_sched_qty = None

    if total_lot_qty is not None and total_sched_qty is not None and total_lot_qty > 0:
        variance = abs(total_sched_qty - total_lot_qty) / total_lot_qty
        status   = "PASS" if variance <= 0.01 else ("WARN" if variance <= 0.05 else "FAIL")
    else:
        variance = None
        status   = "WARN"

    row = {
        "Total_Lot_Qty":       round(total_lot_qty, 2) if total_lot_qty is not None else "N/A",
        "Total_Scheduled_Qty": round(total_sched_qty, 2) if total_sched_qty is not None else "N/A",
        "Variance":            f"{variance:.2%}" if variance is not None else "N/A",
        "Status":              status,
    }
    df = pd.DataFrame([row])
    n_pass = 1 if status == "PASS" else 0
    n_fail = 1 if status == "FAIL" else 0
    note = f"Variance: {variance:.2%}" if variance is not None else "Could not compute"
    return df, n_pass, n_fail, note


def v10_makespan_correct(schedule, lots):
    print("V10 – Makespan matches last scheduled lot finish …")
    try:
        makespan = schedule["scheduled_finish"].max()
    except Exception:
        makespan = None
    try:
        if "last_need" in lots.columns:
            max_last_need = pd.to_datetime(lots["last_need"], errors="coerce").max()
        else:
            max_last_need = None
    except Exception:
        max_last_need = None

    status = "PASS"
    note_txt = ""
    if makespan is not None and max_last_need is not None:
        diff_h = (makespan - max_last_need).total_seconds() / 3600
        if diff_h > 24:
            status   = "WARN"
            note_txt = f"Makespan exceeds last_need by {diff_h:.1f}h"
        else:
            note_txt = f"Makespan within {diff_h:.1f}h of last demand need"
    else:
        note_txt = "Could not compare (missing data)"
        status   = "WARN"

    row = {
        "Makespan":       str(makespan),
        "Max_Last_Need":  str(max_last_need),
        "Lots_Count":     len(schedule),
        "Notes":          note_txt,
        "Status":         status,
    }
    df = pd.DataFrame([row])
    n_pass = 1 if status == "PASS" else 0
    n_fail = 1 if status == "FAIL" else 0
    return df, n_pass, n_fail, note_txt


def build_workbook(results):
    wb = openpyxl.Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"

    title_cell = ws_sum.cell(row=1, column=1, value="Phase 5 Forward Placement Validation Report")
    title_cell.font = Font(bold=True, size=16, color="FFFFFF")
    title_cell.fill = HDR_FILL
    ws_sum.merge_cells("A1:F1")

    ts_cell = ws_sum.cell(row=2, column=1,
                           value=f"Run timestamp: {datetime.now():%Y-%m-%d %H:%M:%S}")
    ts_cell.font = Font(italic=True)
    ws_sum.merge_cells("A2:F2")

    sum_headers = ["#", "Validation", "Status", "Pass Count", "Fail Count", "Notes"]
    write_header_row(ws_sum, sum_headers, row=3)

    for i, (name, df, n_pass, n_fail, note) in enumerate(results, start=1):
        status = "PASS" if n_fail == 0 else "FAIL"
        row_vals = [f"V{i:02d}", name, status, n_pass, n_fail, note]
        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws_sum.cell(row=3 + i, column=c_idx, value=val)
            if c_idx == 3:
                style_status_cell(cell, status)
    auto_col_widths(ws_sum)

    for i, (name, df, n_pass, n_fail, note) in enumerate(results, start=1):
        write_detail_sheet(wb, f"V{i:02d}", name, df)

    return wb


def main():
    print("=" * 60)
    print("Phase 5 Forward Placement Validation")
    print("=" * 60)
    cpm, lots, edges, routing, chng_mx, chng_lk, schedule, floor = load_data()

    validations = [
        ("V01 – All Phase 4 lots scheduled",                   *v01_all_cpm_lots_scheduled(cpm, schedule)),
        ("V02 – Lots on valid machines per routing",           *v02_valid_machine_per_routing(schedule, routing)),
        ("V03 – No overlapping lots on same machine",          *v03_no_machine_overlaps(schedule)),
        ("V04 – Precedence constraints maintained",            *v04_precedence_maintained(edges, schedule)),
        ("V05 – Correct changeover time between lots",         *v05_changeover_time_applied(schedule, chng_mx, chng_lk)),
        ("V06 – Processing time matches routing standards",    *v06_processing_time_matches_routing(schedule, lots, routing)),
        ("V07 – Start < Finish, no negative durations",        *v07_start_before_finish(schedule)),
        ("V08 – Valid machine and operation per routing",      *v08_valid_machine_and_operation(schedule, routing)),
        ("V09 – Total scheduled qty = total lot qty",          *v09_total_qty_matches(lots, floor)),
        ("V10 – Makespan matches last scheduled finish",       *v10_makespan_correct(schedule, lots)),
    ]

    results = [(name, df, n_pass, n_fail, note)
               for name, df, n_pass, n_fail, note in validations]

    print("\nBuilding Excel workbook …")
    wb = build_workbook(results)
    wb.save(OUT_FILE)
    print(f"\nReport saved: {OUT_FILE}")

    total_pass = sum(r[2] for r in results)
    total_fail = sum(r[3] for r in results)
    print(f"Total PASS: {total_pass}  Total FAIL: {total_fail}")
    print("=" * 60)


if __name__ == "__main__":
    main()
