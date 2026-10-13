"""
validate_phase6.py
Phase 6 Curing Revision Validation for Tire Manufacturing MRP Pipeline
Outputs: phase6_curing_validation_report.xlsx

Phase 6 curing output columns:
    plan_id, skuCode, date, pressNo, shift, startTime, endTime, qty, cycleTime,
    remarks, block_id, kit_status, kit_short_qty, kit_ready, kit_oldest,
    green_age_h, green_age_newest_h, past_horizon
This is block/press level (not lot level). pressNo = curing press.
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
PH5_DIR = BASE / "output/PHASEWISE/PHASE5_PLACEMENT"
PH6_DIR = BASE / "output/PHASEWISE/PHASE6_CURING"

OUT_FILE = BASE / "phase6_curing_validation_report.xlsx"

# ── Colour constants ─────────────────────────────────────────────────────────
PASS_FILL = PatternFill("solid", fgColor="C6EFCE")
PASS_FONT = Font(color="276221")
FAIL_FILL = PatternFill("solid", fgColor="FFC7CE")
FAIL_FONT = Font(color="9C0006")
WARN_FILL = PatternFill("solid", fgColor="FFEB9C")
WARN_FONT = Font(color="9C5700")
HDR_FILL  = PatternFill("solid", fgColor="1F3864")
HDR_FONT  = Font(color="FFFFFF", bold=True)

CYCLE_TOL = 0.05        # 5% tolerance for cycle time match
CO_TOL_MIN = 1.0        # 1-minute tolerance for changeover


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
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(m).strip() for m in parsed]
    except Exception:
        pass
    s = s.strip("[]").replace('"', "").replace("'", "")
    parts = [p.strip() for p in re.split(r"[,;]+", s) if p.strip()]
    return parts


def build_changeover_lookup(changeover_matrix, changeover_lookup):
    """Build a dict: (machine, from_item, to_item) -> changeover_min."""
    lookup = {}
    if not changeover_matrix.empty:
        for _, r in changeover_matrix.iterrows():
            key = (str(r.get("machine", "")).strip(),
                   str(r.get("from_MaterialCode_O", "")).strip(),
                   str(r.get("to_MaterialCode_O", "")).strip())
            try:
                lookup[key] = float(r.get("changeover_time_min", 0))
            except Exception:
                lookup[key] = 0.0
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
    p5_schedule = pd.read_csv(PH5_DIR / "phase5_schedule_updated.csv", low_memory=False)
    routing     = pd.read_csv(INPUTS  / "routing.csv", low_memory=False)
    chng_mx     = pd.read_csv(INPUTS  / "changeover_matrix.csv", low_memory=False)
    chng_lk     = pd.read_csv(INPUTS  / "changeover_lookup.csv", low_memory=False)
    curing      = pd.read_csv(PH6_DIR / "phase6_curing_revision_updated.csv", low_memory=False)

    # Parse datetime columns
    for col in ["scheduled_start", "scheduled_finish"]:
        if col in p5_schedule.columns:
            p5_schedule[col] = pd.to_datetime(p5_schedule[col], errors="coerce")
    for col in ["startTime", "endTime", "kit_ready", "kit_oldest"]:
        if col in curing.columns:
            curing[col] = pd.to_datetime(curing[col], errors="coerce")
    # Ensure numeric columns
    for col in ["green_age_h", "green_age_newest_h", "qty", "cycleTime", "kit_short_qty"]:
        if col in curing.columns:
            curing[col] = pd.to_numeric(curing[col], errors="coerce")

    print(f"  p5_schedule: {len(p5_schedule):>6} rows")
    print(f"  routing:     {len(routing):>6} rows")
    print(f"  curing:      {len(curing):>6} rows")
    return p5_schedule, routing, chng_mx, chng_lk, curing


# ── Validation functions ─────────────────────────────────────────────────────

def v01_curing_skus_coverage(p5_schedule, curing):
    print("V01 – All curing skuCodes covered in Phase 5 schedule …")
    # Phase 5 curing lots
    p5_items = set(str(v).strip() for v in p5_schedule["item"].dropna().unique()
                   if "CURI" in str(v).upper() or "BUILD" in str(v).upper())
    # All p5 items
    p5_all   = set(str(v).strip() for v in p5_schedule["item"].dropna().unique())
    # Curing file skuCodes
    curing_skus = set(str(v).strip() for v in curing["skuCode"].dropna().unique())
    rows = []
    for sku in sorted(curing_skus):
        in_p5 = sku in p5_all
        in_p6 = True  # by definition (we're iterating the curing file)
        status = "PASS" if in_p5 else "WARN"
        rows.append({
            "skuCode":  sku,
            "In_Phase5": in_p5,
            "In_Phase6": in_p6,
            "Status":   status,
        })
    # Also check p5 curing items that are missing from phase6
    p5_curing_items = set(str(v).strip() for v in p5_schedule["item"].dropna().unique()
                          if "CURI" in str(v).upper())
    for item in sorted(p5_curing_items - curing_skus):
        rows.append({
            "skuCode":   item,
            "In_Phase5": True,
            "In_Phase6": False,
            "Status":    "WARN",
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    n_warn = (df["Status"] == "WARN").sum()
    note = (f"{n_warn} skuCodes not found in Phase 5 (may be aggregated)"
            if n_warn else "All curing SKUs cross-referenced")
    return df, n_pass, n_fail, note


def v02_valid_curing_press_per_routing(curing, routing):
    print("V02 – Every curing lot assigned to valid curing press …")
    # Get curing rows from routing
    curing_rt = routing[
        routing["operation_name"].str.upper().str.contains("CURING", na=False) |
        routing.get("Equipment", pd.Series(dtype=str)).str.upper().str.contains("CURING", na=False)
    ] if "operation_name" in routing.columns else pd.DataFrame()

    valid_presses = set()
    for _, r in routing.iterrows():
        op = str(r.get("operation_name", "")).upper()
        if "CURING" in op or "CURING" in str(r.get("Equipment", "")).upper():
            for m in parse_machines(r.get("machines", "")):
                valid_presses.add(m.strip())

    rows = []
    for _, r in curing.iterrows():
        press = str(r.get("pressNo", "")).strip()
        sku   = str(r.get("skuCode", "")).strip()
        if not valid_presses:
            # No routing found for curing — warn
            status = "WARN"
            valid  = False
        else:
            valid  = press in valid_presses
            status = "PASS" if valid else "FAIL"
        rows.append({
            "pressNo":    press,
            "skuCode":    sku,
            "Valid_Press": valid,
            "Status":     status,
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note   = (f"{n_fail} blocks on invalid presses" if n_fail
              else f"All presses valid ({len(valid_presses)} valid presses in routing)")
    return df, n_pass, n_fail, note


def v03_no_press_overlaps(curing):
    print("V03 – No overlapping jobs on same curing press …")
    rows = []
    # Sort by pressNo, then startTime
    for press, grp in curing.groupby("pressNo"):
        grp_sorted = grp.sort_values("startTime").reset_index(drop=True)
        for i in range(len(grp_sorted) - 1):
            r1 = grp_sorted.iloc[i]
            r2 = grp_sorted.iloc[i + 1]
            end1   = r1["endTime"]
            start2 = r2["startTime"]
            try:
                if pd.isna(end1) or pd.isna(start2):
                    overlap_h, status = None, "WARN"
                else:
                    overlap_h = (end1 - start2).total_seconds() / 3600
                    if overlap_h > (1 / 3600):   # > 1 second
                        status = "FAIL"
                    else:
                        status = "PASS"
                        overlap_h = max(overlap_h, 0)
            except Exception:
                overlap_h, status = None, "WARN"
            if status != "PASS":
                rows.append({
                    "pressNo":    press,
                    "block_id_1": r1.get("block_id", ""),
                    "endTime_1":  str(end1),
                    "block_id_2": r2.get("block_id", ""),
                    "startTime_2": str(start2),
                    "Overlap_h":  round(overlap_h, 4) if overlap_h is not None else None,
                    "Status":     status,
                })
    n_fail = len(rows)
    n_pass = len(curing) - n_fail
    if not rows:
        rows.append({"pressNo": "ALL", "block_id_1": "", "endTime_1": "",
                     "block_id_2": "", "startTime_2": "", "Overlap_h": 0, "Status": "PASS"})
    df = pd.DataFrame(rows)
    note = f"{n_fail} press overlap violations" if n_fail else "No press overlaps detected"
    return df, n_pass, n_fail, note


def v04_curing_after_building(curing):
    print("V04 – Curing starts only after building lot completes (green_age_h >= 0) …")
    rows = []
    for _, r in curing.iterrows():
        block_id    = r.get("block_id", "")
        sku         = r.get("skuCode", "")
        press       = r.get("pressNo", "")
        kit_status  = str(r.get("kit_status", "")).strip().upper()
        green_age_h = r.get("green_age_h", None)
        kit_ready   = r.get("kit_ready", None)
        start_time  = r.get("startTime", None)

        issues = []
        if pd.notna(green_age_h):
            if float(green_age_h) < 0:
                issues.append(f"green_age_h={green_age_h:.2f}<0")
        else:
            issues.append("green_age_h=NaN")

        if kit_status == "SHORT":
            issues.append("kit_status=SHORT")

        if issues:
            status = "FAIL"
        elif kit_status == "READY":
            status = "PASS"
        else:
            status = "WARN"   # OTHER status (not SHORT, not READY)

        rows.append({
            "block_id":      block_id,
            "skuCode":       sku,
            "pressNo":       press,
            "kit_status":    kit_status,
            "green_age_h":   green_age_h,
            "kit_ready":     str(kit_ready),
            "curing_startTime": str(start_time),
            "Issues":        "; ".join(issues) if issues else "OK",
            "Status":        status,
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = (f"{n_fail} blocks where curing starts before green tyre ready"
            if n_fail else "All green tyres ready before curing")
    return df, n_pass, n_fail, note


def v05_curing_cycle_time_per_routing(curing, routing):
    print("V05 – Correct curing processing time per routing …")
    # Build map: skuCode (routed_product) -> proc_time_sec for CURING operation
    rt_map = {}
    for _, r in routing.iterrows():
        op = str(r.get("operation_name", "")).upper()
        if "CURING" not in op:
            continue
        prod = str(r.get("routed_product", "")).strip()
        try:
            pt  = float(r.get("proc_time", 0))
            uom = str(r.get("proc_time_UOM", "SEC")).strip().upper()
            if uom == "MIN":
                pt_sec = pt * 60
            elif uom == "HR" or uom == "HOUR":
                pt_sec = pt * 3600
            else:
                pt_sec = pt  # assume seconds
            rt_map[prod] = pt_sec
        except Exception:
            pass

    rows = []
    for _, r in curing.iterrows():
        sku       = str(r.get("skuCode", "")).strip()
        cycle_min = r.get("cycleTime", None)
        rt_pt_sec = rt_map.get(sku, None)

        if rt_pt_sec is not None and pd.notna(cycle_min):
            expected_min = rt_pt_sec / 60
            try:
                diff_pct = abs(float(cycle_min) - expected_min) / (expected_min if expected_min > 0 else 1)
                status = "PASS" if diff_pct <= CYCLE_TOL else "FAIL"
            except Exception:
                diff_pct = None
                status = "WARN"
        else:
            expected_min = None
            diff_pct     = None
            status       = "WARN"

        rows.append({
            "skuCode":              sku,
            "cycleTime":            cycle_min,
            "routing_proc_time_sec": rt_pt_sec,
            "expected_cycle_min":   round(expected_min, 2) if expected_min is not None else None,
            "Status":               status,
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    note = f"{n_fail} blocks with cycle time mismatch" if n_fail else "All cycle times match routing"
    return df, n_pass, n_fail, note


def v06_changeover_between_curing_lots(curing, chng_mx, chng_lk):
    print("V06 – Correct changeover between consecutive curing lots …")
    co_lookup = build_changeover_lookup(chng_mx, chng_lk)
    rows = []
    for press, grp in curing.groupby("pressNo"):
        grp_sorted = grp.sort_values("startTime").reset_index(drop=True)
        for i in range(len(grp_sorted) - 1):
            r1 = grp_sorted.iloc[i]
            r2 = grp_sorted.iloc[i + 1]
            from_sku = str(r1.get("skuCode", "")).strip()
            to_sku   = str(r2.get("skuCode", "")).strip()
            press_s  = str(press).strip()
            # Same SKU = no changeover needed
            if from_sku == to_sku:
                continue
            try:
                end1   = r1["endTime"]
                start2 = r2["startTime"]
                if pd.isna(end1) or pd.isna(start2):
                    gap_min = None
                else:
                    gap_min = (start2 - end1).total_seconds() / 60
            except Exception:
                gap_min = None

            # Look up expected changeover
            expected = (co_lookup.get((press_s, from_sku, to_sku)) or
                        co_lookup.get(("", from_sku, to_sku)))
            if expected is None:
                status = "WARN"
                exp_str = "N/A"
            else:
                if gap_min is not None:
                    status = "PASS" if gap_min >= (expected - CO_TOL_MIN) else "FAIL"
                else:
                    status = "WARN"
                exp_str = str(expected)

            rows.append({
                "pressNo":              press_s,
                "from_sku":             from_sku,
                "to_sku":               to_sku,
                "gap_min":              round(gap_min, 2) if gap_min is not None else None,
                "expected_changeover":  exp_str,
                "Status":               status,
            })

    n_fail = sum(1 for r in rows if r["Status"] == "FAIL")
    n_pass = sum(1 for r in rows if r["Status"] == "PASS")
    if not rows:
        rows.append({"pressNo": "ALL", "from_sku": "", "to_sku": "",
                     "gap_min": 0, "expected_changeover": "N/A", "Status": "PASS"})
    df = pd.DataFrame(rows)
    note = f"{n_fail} changeover violations" if n_fail else "All changeovers respected"
    return df, n_pass, n_fail, note


def v07_start_before_end(curing):
    print("V07 – Start Time < End Time, no negative curing durations …")
    rows = []
    for _, r in curing.iterrows():
        issues = []
        try:
            st = r["startTime"]
            et = r["endTime"]
            if pd.isna(st) or pd.isna(et):
                issues.append("null_time")
            elif st >= et:
                issues.append(f"startTime({st}) >= endTime({et})")
            else:
                dur_h = (et - st).total_seconds() / 3600
                if dur_h <= 0:
                    issues.append(f"duration={dur_h:.4f}h <= 0")
        except Exception as ex:
            issues.append(f"parse_error: {ex}")
        if issues:
            rows.append({
                "plan_id":   r.get("plan_id", ""),
                "block_id":  r.get("block_id", ""),
                "skuCode":   r.get("skuCode", ""),
                "pressNo":   r.get("pressNo", ""),
                "startTime": str(r.get("startTime", "")),
                "endTime":   str(r.get("endTime", "")),
                "Issues":    "; ".join(issues),
                "Status":    "FAIL",
            })
    n_fail = len(rows)
    n_pass = len(curing) - n_fail
    if not rows:
        rows.append({"plan_id": "ALL", "block_id": "", "skuCode": "", "pressNo": "",
                     "startTime": "", "endTime": "", "Issues": "None", "Status": "PASS"})
    df = pd.DataFrame(rows)
    note = f"{n_fail} rows with invalid times" if n_fail else "All start/end times valid"
    return df, n_pass, n_fail, note


def v08_total_cured_qty_vs_building(curing, p5_schedule):
    print("V08 – Total cured qty vs building qty from Phase 5 …")
    total_cured = curing["qty"].sum() if "qty" in curing.columns else None

    # Find building lots from phase 5
    building_mask = pd.Series([False] * len(p5_schedule))
    for col in ["operation", "item", "item_type", "process"]:
        if col in p5_schedule.columns:
            building_mask = building_mask | p5_schedule[col].astype(str).str.upper().str.contains(
                r"BUILD|GREEN", na=False, regex=True
            )

    building_lots = p5_schedule[building_mask]

    # Try to get quantity from floor schedule if available
    # Otherwise use cpm/lots duration as proxy
    total_building_qty = None

    # Attempt from p5 floor schedule (might be passed or not)
    # Since only p5_schedule is available here, we sum duration_h as proxy
    # But better: count rows * batch as approximation
    # Use produce_qty if col exists
    if "produce_qty" in p5_schedule.columns:
        total_building_qty = building_lots["produce_qty"].sum()
    elif "lot_qty" in p5_schedule.columns:
        total_building_qty = building_lots["lot_qty"].sum()
    elif "duration_h" in p5_schedule.columns:
        # Proxy: total production hours
        total_building_qty = None  # Cannot directly compare

    if total_cured is not None and total_building_qty is not None and total_building_qty > 0:
        variance = abs(total_cured - total_building_qty) / total_building_qty
        status   = "PASS" if variance <= 0.01 else ("WARN" if variance <= 0.10 else "FAIL")
        var_str  = f"{variance:.2%}"
    else:
        variance = None
        status   = "WARN"
        var_str  = "N/A (could not determine building qty)"

    row = {
        "Total_Phase5_Building_Qty": round(total_building_qty, 2) if total_building_qty is not None else "N/A",
        "Total_Phase6_Cured_Qty":    round(total_cured, 2) if total_cured is not None else "N/A",
        "Variance":                  var_str,
        "Status":                    status,
    }
    df = pd.DataFrame([row])
    n_pass = 1 if status == "PASS" else 0
    n_fail = 1 if status == "FAIL" else 0
    note   = f"Variance: {var_str}" if variance is not None else "Could not compute"
    return df, n_pass, n_fail, note


def v09_no_duplicate_blocks(curing):
    print("V09 – Every curing block assigned exactly once (no duplicate pressNo+block_id) …")
    dupes = curing.duplicated(subset=["pressNo", "block_id"], keep=False)
    dup_df = curing[dupes][["pressNo", "block_id", "skuCode", "startTime", "endTime", "qty"]].copy()
    dup_df["Status"] = "FAIL"
    dup_df["startTime"] = dup_df["startTime"].astype(str)
    dup_df["endTime"]   = dup_df["endTime"].astype(str)

    n_total  = len(curing)
    n_dup    = len(dup_df)
    n_unique = n_total - n_dup

    if dup_df.empty:
        dup_df = pd.DataFrame([{
            "pressNo": "ALL", "block_id": "", "skuCode": "",
            "startTime": "", "endTime": "", "qty": "",
            "Status": "PASS",
        }])
    df = pd.DataFrame(dup_df)
    n_pass = n_unique
    n_fail = n_dup
    note   = f"{n_dup} duplicate (pressNo, block_id) combinations" if n_dup else "No duplicate blocks found"
    return df, n_pass, n_fail, note


def v10_final_makespan(curing):
    print("V10 – Final makespan and past_horizon summary …")
    try:
        makespan = curing["endTime"].max()
    except Exception:
        makespan = None

    n_total = len(curing)
    n_past  = int((curing["past_horizon"] == True).sum()) if "past_horizon" in curing.columns else 0
    n_in    = n_total - n_past

    status = "PASS"
    if n_past > 0:
        pct = n_past / n_total if n_total > 0 else 0
        status = "WARN" if pct < 0.10 else "FAIL"

    row = {
        "Makespan":           str(makespan),
        "Total_Blocks":       n_total,
        "Within_Horizon":     n_in,
        "Past_Horizon_Blocks": n_past,
        "Pct_Past_Horizon":   f"{n_past/n_total:.1%}" if n_total > 0 else "N/A",
        "Status":             status,
    }
    df = pd.DataFrame([row])
    n_pass = 1 if status == "PASS" else 0
    n_fail = 1 if status == "FAIL" else 0
    note   = (f"{n_past} blocks past planning horizon ({n_past/n_total:.1%})" if n_past
              else "All blocks within planning horizon")
    return df, n_pass, n_fail, note


def build_workbook(results):
    wb = openpyxl.Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"

    title_cell = ws_sum.cell(row=1, column=1, value="Phase 6 Curing Revision Validation Report")
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
    print("Phase 6 Curing Revision Validation")
    print("=" * 60)
    p5_schedule, routing, chng_mx, chng_lk, curing = load_data()

    validations = [
        ("V01 – Curing SKU coverage vs Phase 5",               *v01_curing_skus_coverage(p5_schedule, curing)),
        ("V02 – Valid curing press per routing",                *v02_valid_curing_press_per_routing(curing, routing)),
        ("V03 – No overlapping jobs on same press",             *v03_no_press_overlaps(curing)),
        ("V04 – Curing starts after building completes",        *v04_curing_after_building(curing)),
        ("V05 – Correct curing cycle time per routing",         *v05_curing_cycle_time_per_routing(curing, routing)),
        ("V06 – Correct changeover between curing lots",        *v06_changeover_between_curing_lots(curing, chng_mx, chng_lk)),
        ("V07 – Start Time < End Time, no negative durations",  *v07_start_before_end(curing)),
        ("V08 – Total cured qty = building qty from Phase 5",   *v08_total_cured_qty_vs_building(curing, p5_schedule)),
        ("V09 – No duplicate (pressNo, block_id) assignments",  *v09_no_duplicate_blocks(curing)),
        ("V10 – Final makespan and past-horizon summary",       *v10_final_makespan(curing)),
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
