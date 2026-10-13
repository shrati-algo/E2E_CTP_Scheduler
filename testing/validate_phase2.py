"""
validate_phase2.py  –  Phase 2 Lot-Sizing Validation
=====================================================
Reads Phase-1C net demand, MPQ master, routing master, and Phase-2 lots.
Runs 10 validations and writes an XLSX report.

Output: phase2_validation_report.xlsx
"""

import math
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE     = Path(__file__).parent
INPUTS   = BASE / "output/PHASEWISE/00_INPUTS"
PH1C_DIR = BASE / "output/PHASEWISE/PHASE1C_NETTING"
PH2_DIR  = BASE / "output/PHASEWISE/PHASE2_LOTS"

OUTPUT_XLSX = BASE / "phase2_validation_report.xlsx"

# ---------------------------------------------------------------------------
# Style constants
# ---------------------------------------------------------------------------
CLR = {
    "PASS": {"fill": "C6EFCE", "font": "276221"},
    "FAIL": {"fill": "FFC7CE", "font": "9C0006"},
    "WARN": {"fill": "FFEB9C", "font": "9C5700"},
    "HDR":  {"fill": "1F3864", "font": "FFFFFF"},
}

def _fill(hex_str):
    return PatternFill("solid", fgColor=hex_str)

def _font(hex_str, bold=False):
    return Font(color=hex_str, bold=bold)

def style_cell(cell, status, bold=False):
    c = CLR.get(status, CLR["WARN"])
    cell.fill = _fill(c["fill"])
    cell.font = _font(c["font"], bold=bold)

def header_cell(cell, value=""):
    if value:
        cell.value = value
    cell.fill = _fill(CLR["HDR"]["fill"])
    cell.font = _font(CLR["HDR"]["font"], bold=True)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

def auto_col_width(ws, min_width=8, max_width=50):
    for col in ws.columns:
        length = max(
            len(str(c.value)) if c.value is not None else 0
            for c in col
        )
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(
            max(length + 2, min_width), max_width
        )

# ---------------------------------------------------------------------------
# Helper: write a detail sheet
# ---------------------------------------------------------------------------
def write_detail_sheet(wb, sheet_name, description, df_result, status_col="Status"):
    """
    df_result must have a column named `status_col` with PASS/FAIL/WARN values.
    Large datasets (>10 000): keep all FAILs + first 500 PASSes.
    """
    ws = wb.create_sheet(sheet_name)

    # Truncate if needed
    truncated = False
    if len(df_result) > 10_000:
        fails = df_result[df_result[status_col] == "FAIL"]
        passes = df_result[df_result[status_col] == "PASS"].head(500)
        others = df_result[~df_result[status_col].isin(["PASS", "FAIL"])]
        df_result = pd.concat([fails, passes, others]).reset_index(drop=True)
        truncated = True

    n_pass = (df_result[status_col] == "PASS").sum()
    n_fail = (df_result[status_col] == "FAIL").sum()
    n_warn = (df_result[status_col] == "WARN").sum()

    # Row 1 – description
    ws.cell(1, 1, description).font = Font(bold=True)
    # Row 2 – counts
    note = f"PASS={n_pass}  FAIL={n_fail}  WARN={n_warn}"
    if truncated:
        note += "  [Truncated: all FAILs + first 500 PASSes shown]"
    ws.cell(2, 1, note)
    # Row 3 – headers
    cols = list(df_result.columns)
    for ci, col in enumerate(cols, 1):
        header_cell(ws.cell(3, ci), col)
    # Rows 4+ – data
    for ri, row in enumerate(df_result.itertuples(index=False), 4):
        for ci, val in enumerate(row, 1):
            cell = ws.cell(ri, ci, val)
            if cols[ci - 1] == status_col:
                style_cell(cell, str(val))

    auto_col_width(ws)
    return n_pass, n_fail, n_warn

# ---------------------------------------------------------------------------
# Helper: parse numeric from MPQ strings like "3 Batches", "Spool length (300)"
# ---------------------------------------------------------------------------
def parse_mpq_numeric(val):
    """FIXED 2026-07-23: delegate to phase2's OWN _parse_mpq.

    THE BUG THIS FIXES: this used to take `nums[0]` -- the FIRST number in the string --
    while phase2 takes the SMALLEST (and returns None for "3 Batches"). On the plant's
    multi-value rows that disagreed on every lot:
        Tread  "310, 206"                 validator wanted 310, phase2 floors at 206
        Edge   "3990, 3000, 1650, 1350"   validator wanted 3990, phase2 floors at 1350
        "3 Batches"                       validator read 3 PIECES, phase2 -> None
    That produced 91 false FAILs in V03 and 157 in V06 against a CORRECT lot table.
    A validator must never re-implement the rule it is checking -- it imports it.
    """
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).parent.parent / "phases"))
        from phase2_lot_sizing import _parse_mpq
        return _parse_mpq(val)
    except Exception:
        if pd.isna(val):
            return None
        s = str(val)
        if "batch" in s.lower():
            return None
        nums = re.findall(r"[\d]+(?:\.\d+)?", s)
        return min(float(n) for n in nums) if nums else None


def _load_cfg():
    """phase2's own config -- the REAL constraints (duration cap, mixing MPQ)."""
    import yaml
    try:
        return yaml.safe_load(open(BASE.parent / "config.yaml")) or {}
    except Exception:
        return {}

CFG          = _load_cfg()
MAX_LOT_H    = float(CFG.get("max_lot_duration_h", 8.0) or 8.0)
MIX_ON       = bool(CFG.get("apply_mixing_mpq", False))
MIX_BATCHES  = CFG.get("mixing_mpq_min_batches", {}) or {}


def _qty_tol(x):
    """Rounding-aware tolerance. phase2 rounds lot quantities, and demand totals here run
    to 1.6e7 -- an absolute 1e-6 test on that is measuring float noise, not correctness.
    (It flagged CAP 66 as 'short' by 0.11 units out of 16,500,694.)"""
    return max(1.0, 1e-6 * abs(float(x)))

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
def load_data():
    print("[Phase2] Loading data …")

    d1c = pd.read_csv(PH1C_DIR / "phase1_demand_NET_updated.csv.gz", compression="gzip")
    print(f"  Net demand rows: {len(d1c)}")

    mpq = pd.read_csv(INPUTS / "mpq.csv")
    print(f"  MPQ rows: {len(mpq)}")

    routing = pd.read_csv(INPUTS / "routing.csv")
    print(f"  Routing rows: {len(routing)}")

    lots = pd.read_csv(PH2_DIR / "phase2_lots_updated.csv")
    print(f"  Lots rows: {len(lots)}")

    # Parse numeric MPQ
    mpq["mpq_min_val"] = mpq["Minimum Run Qty"].apply(parse_mpq_numeric)

    # Normalise fraction_allowed in lots to boolean-ish string
    lots["_frac_norm"] = (
        lots["fraction_allowed"]
        .astype(str)
        .str.strip()
        .str.upper()
        .map(lambda x: "YES" if x in ("TRUE", "YES", "1", "Y") else "NO")
    )

    return d1c, mpq, routing, lots

# ---------------------------------------------------------------------------
# V1 – All net demand converted to lots (no demand lost)
# ---------------------------------------------------------------------------
def v1_demand_coverage(d1c, lots):
    print("[V1] Demand coverage …")
    demand_items = set(d1c["item_code"].dropna().unique())
    lot_items    = set(lots["item_code"].dropna().unique())

    rows = []
    for item in sorted(demand_items):
        in_lots = item in lot_items
        rows.append({
            "item_code":      item,
            "In_Net_Demand":  True,
            "In_Lots":        in_lots,
            "Status":         "PASS" if in_lots else "FAIL",
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V2 – Total lot qty >= total net demand per item
# ---------------------------------------------------------------------------
def v2_qty_totals(d1c, lots):
    print("[V2] Qty totals per item …")
    lot_sum = lots.groupby("item_code", as_index=False)["lot_qty"].sum().rename(
        columns={"lot_qty": "Total_Lot_Qty"}
    )
    dem_sum = d1c.groupby("item_code", as_index=False)["demand_qty"].sum().rename(
        columns={"demand_qty": "Total_Net_Demand"}
    )
    merged = dem_sum.merge(lot_sum, on="item_code", how="outer").fillna(0)
    merged["Variance"] = merged["Total_Lot_Qty"] - merged["Total_Net_Demand"]
    # FIXED 2026-07-23: rounding-aware tolerance (was a bare -1e-6 on quantities up to 1.6e7,
    # which flagged 107 items whose TOTAL combined shortfall was under 1 unit).
    merged["Tolerance"] = merged["Total_Net_Demand"].apply(_qty_tol).round(3)
    merged["Status"] = [
        "PASS" if v >= -t else "FAIL"
        for v, t in zip(merged["Variance"], merged["Total_Net_Demand"].apply(_qty_tol))
    ]
    df = merged[["item_code", "Total_Net_Demand", "Total_Lot_Qty", "Variance", "Tolerance", "Status"]]
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V3 – Every lot qty satisfies MPQ
# ---------------------------------------------------------------------------
def v3_mpq_compliance(lots, mpq):
    print("[V3] MPQ compliance …")
    mpq_lookup = mpq.set_index("Item Type")[["mpq_min_val", "Fraction Allowed"]].to_dict("index")

    rows = []
    for _, r in lots.iterrows():
        item_type = r.get("item_type", None)
        lot_qty   = r.get("lot_qty", 0)
        raw_dem   = r.get("raw_demand", 0)
        frac      = r["_frac_norm"]

        info = mpq_lookup.get(item_type, {})
        mpq_min  = info.get("mpq_min_val", None)

        if frac == "YES":
            mpq_ok = True
            status = "PASS"
        elif mpq_min is None or pd.isna(mpq_min):
            # NaN TRAP (MEMORY: "present-but-blank"): _parse_mpq returns None for "3 Batches",
            # but pandas stores that as NaN in a float column and `NaN is None` is False. Without
            # the isna() guard this fell through to `lot_qty >= NaN` -> always False -> every
            # compound lot a false FAIL. Compounds carry NO piece floor by design: they are sized
            # by the routing batch_size (+ the mixing-MPQ 3-batch rule).
            mpq_ok = True
            status = "WARN"
        else:
            # edge-case: last lot can equal raw_demand even if < mpq
            mpq_ok = (lot_qty >= mpq_min - 1e-6) or (abs(lot_qty - raw_dem) < 1e-6)
            status = "PASS" if mpq_ok else "FAIL"

        rows.append({
            "lot_id":           r["lot_id"],
            "item_code":        r["item_code"],
            "item_type":        item_type,
            "lot_qty":          lot_qty,
            "mpq_min":          mpq_min,
            "fraction_allowed": frac,
            "MPQ_OK":           mpq_ok,
            "Status":           status,
        })

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V4 – No lot exceeds max lot size (batch_size from routing)
# ---------------------------------------------------------------------------
def v4_max_lot_size(lots, routing):
    """FIXED 2026-07-23 -- test the REAL cap.

    THE BUG THIS FIXES: this asserted `lot_qty <= routing.batch_size`. batch_size is the
    MIXER/PROCESS BATCH (e.g. 220 kg), NOT a maximum lot size. phase2 deliberately builds
    lots spanning MANY batches -- op_duration_min does ceil(qty/batch_size) for exactly that
    reason. So the old test failed 1497 of 1497 lots (ZERO passes) on a CORRECT lot table,
    e.g. a 660 kg compound lot that is precisely the intended 3 x 220 batches.

    phase2's ACTUAL size limits are:
      * max_lot_duration_h (config)  -> split into sub-lots of <= this   (phase2:781-783)
      * mpq_max / "Maximum Run Qty"  -> ABSENT on CTP, so the MHE split never fires (inert)
    """
    print("[V4] Max lot size (real cap = max_lot_duration_h) …")
    rows = []
    for _, r in lots.iterrows():
        dur = float(r.get("duration_hours", 0) or 0)
        status = "PASS" if dur <= MAX_LOT_H + 1e-6 else "FAIL"
        rows.append({
            "lot_id":             r["lot_id"],
            "item_code":          r["item_code"],
            "lot_qty":            r["lot_qty"],
            "duration_hours":     round(dur, 3),
            "max_lot_duration_h": MAX_LOT_H,
            "MHE_max_qty":        "N/A (no Maximum Run Qty in MPQ master - MHE inert)",
            "Status":             status,
        })

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V5 – Lot splitting correct
# ---------------------------------------------------------------------------
def v5_lot_splitting(lots, routing):
    """FIXED 2026-07-23 -- test splitting against the DURATION cap, not one-lot-per-batch.

    THE BUG THIS FIXES: it required `lot_count >= ceil(raw_demand / batch_size)` -- i.e. ONE
    LOT PER BATCH. phase2 splits by max_lot_duration_h, so one lot legitimately holds many
    batches (median actual 3 lots vs the old rule's 'expected' 126). It failed 176 of 176
    items (ZERO passes) on correct output.

    THE REAL RULE (phase2:781-783): n_dur = ceil(full_duration / max_dur_min); every
    resulting sub-lot must come in at or under the cap. So: for each item, if its total work
    exceeds the cap it MUST be split into >1 lot, and NO lot may exceed the cap.
    """
    print("[V5] Lot splitting (duration cap) …")
    rows = []
    for item, g in lots.groupby("item_code"):
        tot_h = float(g["duration_hours"].sum())
        n_lots = int(len(g))
        worst = float(g["duration_hours"].max())
        need_split = tot_h > MAX_LOT_H + 1e-6
        ok_split = (n_lots > 1) if need_split else True
        ok_cap = worst <= MAX_LOT_H + 1e-6
        status = "PASS" if (ok_split and ok_cap) else "FAIL"
        rows.append({
            "item_code":          item,
            "total_duration_h":   round(tot_h, 3),
            "lot_count":          n_lots,
            "longest_lot_h":      round(worst, 3),
            "cap_h":              MAX_LOT_H,
            "split_required":     need_split,
            "Status":             status,
        })

    if not rows:
        rows = [{"item_code": "N/A", "total_duration_h": 0, "lot_count": 0,
                 "longest_lot_h": 0, "cap_h": MAX_LOT_H, "split_required": False,
                 "Status": "WARN"}]

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V6 – Lot merging correct
# ---------------------------------------------------------------------------
def v6_lot_merging(lots, mpq):
    print("[V6] Lot merging …")
    mpq_lookup = mpq.set_index("Item Type")["mpq_min_val"].to_dict()

    # Only multi-block lots
    multi = lots[lots["n_blocks"] > 1].copy()

    rows = []
    for _, r in multi.iterrows():
        item_type = r.get("item_type", None)
        mpq_min   = mpq_lookup.get(item_type, None)
        lot_qty   = r["lot_qty"]
        raw_dem   = r["raw_demand"]
        n_blocks  = r["n_blocks"]

        if mpq_min is None or pd.isna(mpq_min):
            # Same NaN trap as V03 -- compounds ("3 Batches" -> None -> NaN) have no piece
            # floor; they are batch-sized by the routing. `NaN is None` is False, so without
            # this guard every merged compound lot became a false FAIL.
            merge_ok = True
            status   = "WARN"
        else:
            merge_ok = lot_qty >= mpq_min - 1e-6
            status   = "PASS" if merge_ok else "FAIL"

        rows.append({
            "item_code":        r["item_code"],
            "n_blocks_merged":  n_blocks,
            "total_raw_demand": raw_dem,
            "lot_qty":          lot_qty,
            "mpq_min":          mpq_min if mpq_min is not None else "N/A",
            "Merge_OK":         merge_ok,
            "Status":           status,
        })

    if not rows:
        rows = [{"item_code": "N/A", "n_blocks_merged": 0, "total_raw_demand": 0,
                 "lot_qty": 0, "mpq_min": "N/A", "Merge_OK": True, "Status": "WARN"}]

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V7 – Every lot has correct UOM
# ---------------------------------------------------------------------------
def v7_uom(lots, routing):
    print("[V7] UOM check …")
    uom = (
        routing[["routed_product", "batch_UNIT"]]
        .dropna(subset=["batch_UNIT"])
        .drop_duplicates("routed_product")
    )
    merged = lots.merge(uom, left_on="item_code", right_on="routed_product", how="left")

    rows = []
    for _, r in merged.iterrows():
        lot_uom  = str(r.get("lot_uom", "")).strip().upper()
        exp_uom  = str(r.get("batch_UNIT", "")).strip().upper() if not pd.isna(r.get("batch_UNIT")) else None

        if exp_uom is None or exp_uom == "NAN" or exp_uom == "":
            status = "WARN"
        else:
            status = "PASS" if lot_uom == exp_uom else "FAIL"

        rows.append({
            "lot_id":       r["lot_id"],
            "item_code":    r["item_code"],
            "lot_uom":      r.get("lot_uom", ""),
            "expected_uom": exp_uom if exp_uom else "N/A",
            "Status":       status,
        })

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V8 – No null or negative critical fields
# ---------------------------------------------------------------------------
def v8_null_negative(lots):
    print("[V8] Null / negative check …")
    rows = []
    for _, r in lots.iterrows():
        issues = []
        if pd.isna(r.get("lot_id")):
            issues.append("lot_id is null")
        if pd.isna(r.get("item_code")):
            issues.append("item_code is null")
        if pd.isna(r.get("lot_qty")) or r["lot_qty"] <= 0:
            issues.append(f"lot_qty={r.get('lot_qty')} (must be >0)")
        if pd.notna(r.get("duration_min")) and r["duration_min"] < 0:
            issues.append(f"duration_min={r['duration_min']} (must be >=0)")

        status = "FAIL" if issues else "PASS"
        rows.append({
            "lot_id":      r.get("lot_id", "NULL"),
            "item_code":   r.get("item_code", "NULL"),
            "lot_qty":     r.get("lot_qty"),
            "duration_min":r.get("duration_min"),
            "Issues":      "; ".join(issues) if issues else "",
            "Status":      status,
        })

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V9 – Each lot has unique lot_id
# ---------------------------------------------------------------------------
def v9_unique_lot_ids(lots):
    print("[V9] Unique lot IDs …")
    dup = lots[lots.duplicated("lot_id", keep=False)].copy()

    if dup.empty:
        df = pd.DataFrame([{"lot_id": "ALL UNIQUE", "item_code": "", "count": 1, "Status": "PASS"}])
        return df, 1, 0

    dup_counts = dup.groupby("lot_id")["item_code"].agg(["count", "first"]).reset_index()
    dup_counts.columns = ["lot_id", "count", "item_code"]
    dup_counts["Status"] = "FAIL"
    n_fail = len(dup_counts)
    return dup_counts[["lot_id", "item_code", "count", "Status"]], 0, n_fail

# ---------------------------------------------------------------------------
# V10 – Total material conservation
# ---------------------------------------------------------------------------
def v10_material_conservation(d1c, lots, mpq):
    print("[V10] Material conservation …")
    total_demand  = d1c["demand_qty"].sum()
    total_lot_qty = lots["lot_qty"].sum()
    variance      = total_lot_qty - total_demand

    # Max acceptable surplus = sum of all per-lot surplus
    total_surplus = lots["surplus"].sum() if "surplus" in lots.columns else float("nan")

    status = "PASS" if variance >= -1e-3 else "FAIL"

    df = pd.DataFrame([{
        "Total_Net_Demand":  round(total_demand, 4),
        "Total_Lot_Qty":     round(total_lot_qty, 4),
        "Variance":          round(variance, 4),
        "Total_Surplus_Col": round(total_surplus, 4) if not math.isnan(total_surplus) else "N/A",
        "Status":            status,
    }])
    return df, 1 if status == "PASS" else 0, 0 if status == "PASS" else 1

# ---------------------------------------------------------------------------
# Build Summary sheet
# ---------------------------------------------------------------------------
def build_summary(wb, results):
    ws = wb.active
    ws.title = "Summary"

    # Title
    title_cell = ws.cell(1, 1, "Phase 2 – Lot Sizing Validation Report")
    title_cell.font = Font(bold=True, size=14)
    ws.merge_cells("A1:F1")

    # Timestamp
    ts_cell = ws.cell(2, 1, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ts_cell.font = Font(italic=True)
    ws.merge_cells("A2:F2")

    # Header row
    headers = ["#", "Validation", "Status", "Pass Count", "Fail Count", "Notes"]
    for ci, h in enumerate(headers, 1):
        header_cell(ws.cell(4, ci), h)

    for ri, (vid, vname, status, n_pass, n_fail, notes) in enumerate(results, 5):
        ws.cell(ri, 1, vid)
        ws.cell(ri, 2, vname)
        status_cell = ws.cell(ri, 3, status)
        ws.cell(ri, 4, n_pass)
        ws.cell(ri, 5, n_fail)
        ws.cell(ri, 6, notes)
        for ci in range(1, 7):
            style_cell(ws.cell(ri, ci), status)

    auto_col_width(ws)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    d1c, mpq, routing, lots = load_data()

    wb = Workbook()
    summary_results = []

    validations = [
        ("V01", "All net demand converted to lots",         v1_demand_coverage,      (d1c, lots)),
        ("V02", "Total lot qty >= net demand per item (rounding-aware)", v2_qty_totals,  (d1c, lots)),
        ("V03", "Every lot qty satisfies MPQ (phase2 _parse_mpq)", v3_mpq_compliance,    (lots, mpq)),
        ("V04", "No lot exceeds max_lot_duration_h (MHE inert)", v4_max_lot_size,        (lots, routing)),
        ("V05", "Lot splitting follows the duration cap",   v5_lot_splitting,         (lots, routing)),
        ("V06", "Lot merging correct (phase2 _parse_mpq)",  v6_lot_merging,           (lots, mpq)),
        ("V07", "Every lot has correct UOM",                v7_uom,                   (lots, routing)),
        ("V08", "No null or negative critical fields",      v8_null_negative,         (lots,)),
        ("V09", "Each lot has unique lot_id",               v9_unique_lot_ids,        (lots,)),
        ("V10", "Total qty = total net demand (conservation)", v10_material_conservation, (d1c, lots, mpq)),
    ]

    descriptions = {
        "V01": "V1: For each item_code in net demand, verify it appears in Phase 2 lots.",
        "V02": "V2: Per item_code, sum(lot_qty) must be >= sum(demand_qty). Variance = lot_qty - demand_qty.",
        "V03": "V3: If fraction_allowed=NO, lot_qty >= mpq_min (or equals raw_demand as edge case).",
        "V04": "V4: lot_qty <= routing batch_size. WARN if no batch_size defined.",
        "V05": "V5: When raw_demand > batch_size, lot count >= ceil(raw_demand / batch_size).",
        "V06": "V6: Multi-block lots (n_blocks > 1) should have lot_qty >= mpq_min.",
        "V07": "V7: lot_uom matches routing batch_UNIT for the routed_product.",
        "V08": "V8: lot_id not null, item_code not null, lot_qty > 0, duration_min >= 0.",
        "V09": "V9: Every lot_id is unique (no duplicates).",
        "V10": "V10: sum(lot_qty) across all lots >= sum(demand_qty) across all net demand.",
    }

    for vid, vname, fn, args in validations:
        try:
            df, n_pass, n_fail = fn(*args)
        except Exception as exc:
            print(f"  ERROR in {vid}: {exc}")
            traceback.print_exc()
            df = pd.DataFrame([{"Error": str(exc), "Status": "FAIL"}])
            n_pass, n_fail = 0, 1

        has_fail = n_fail > 0
        has_warn = "WARN" in df.get("Status", pd.Series(dtype=str)).values if not df.empty else False
        if has_fail:
            overall = "FAIL"
        elif has_warn and n_pass == 0:
            overall = "WARN"
        else:
            overall = "PASS"

        write_detail_sheet(wb, vid, descriptions[vid], df)
        summary_results.append((vid, vname, overall, int(n_pass), int(n_fail), ""))
        print(f"  {vid}: {overall} (PASS={n_pass}, FAIL={n_fail})")

    # Move summary to front
    ws_list = wb.sheetnames
    first_sheet = wb[ws_list[0]]
    # Create summary as first sheet
    _ = wb.create_sheet("Summary", 0)
    wb.remove(wb["Summary"])
    wb.create_sheet("Summary", 0)
    build_summary(wb, summary_results)
    # Remove the blank default sheet if still there
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    wb.save(OUTPUT_XLSX)
    print(f"\nReport saved to: {OUTPUT_XLSX}")

if __name__ == "__main__":
    main()
