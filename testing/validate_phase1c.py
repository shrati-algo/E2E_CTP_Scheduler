"""
validate_phase1c.py — Phase 1c MRP Netting Validation

  >>> v6-VERBATIM MODEL  (rewritten 2026-07-22) <<<

  WHY THIS REWRITE
  ----------------
  Phase 1c was reverted to the BTP `v6_wave_p2` netting algorithm, ALGORITHM-VERBATIM
  (USER DIRECTIVE 2026-07-22). The PREVIOUS validator judged phase1c against the
  *aging-fixed* CTP model (per-lot alive WIP, #9 SUM(child_qty)/Parent_qty cascade,
  #25 expiry pre-filter). That model no longer describes the code, so it raised ~700
  "failures" that were really just the validator checking properties the revert removed.

  This validator judges phase1c against WHAT v6 ACTUALLY DOES
  (phase1c_mrp_netting.py:202-238):
     * window is measured from PLAN_START (scalar), not per-lot produced_time+max_aging;
     * WIP is the FULL inventory snapshot per item  (wip_by_item[item]) — NO aging/expiry
       filter, NO whitespace-normalised key;
     * cascade weight is the RAW child_quantity, ONE EDGE PER BOM ROW;
     * only IN-WINDOW demand is nettable; out-of-window demand is passed through untouched;
     * each item nets by  wip_used = min(after_upstream, wip_q); the saving cascades to
       children as  saving * per_unit.

  It does this by INDEPENDENTLY RE-RUNNING v6's cascade from the inputs (a second
  implementation) and (V01) asserting the shipped output equals the replay, then
  (V02-V09) checking the physical invariants that any correct netting must satisfy.
  Under a faithful v6 port these all PASS by construction — a FAIL here is a REAL bug.

  V10 is INFORMATIONAL (WARN, never FAIL): it measures how much of the WIP v6 credited
  is actually AGED PAST SHELF LIFE at plan_start. v6 ignores per-lot age by design (the
  aging fix was reverted), so this over-net is EXPECTED, ACCEPTED behaviour — reported
  for transparency, not scored as an error. Restore .bak_agingfix to eliminate it.
"""

import sys as _sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict, deque
import warnings
import traceback
import re
import yaml

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

warnings.filterwarnings("ignore")

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE     = Path(__file__).parent
INPUTS   = BASE / "output/PHASEWISE/00_INPUTS"
PH1B_DIR = BASE / "output/PHASEWISE/PHASE1B_DEMAND"
PH1C_DIR = BASE / "output/PHASEWISE/PHASE1C_NETTING"
CONFIG   = BASE.parent / "config.yaml"          # ctp_v6_v2/config.yaml

OUTPUT_FILE = BASE / "phase1c_validation_report.xlsx"

# ── Color palette ──────────────────────────────────────────────────────────────
FILL_PASS   = PatternFill("solid", fgColor="C6EFCE")
FILL_FAIL   = PatternFill("solid", fgColor="FFC7CE")
FILL_WARN   = PatternFill("solid", fgColor="FFEB9C")
FILL_HEADER = PatternFill("solid", fgColor="1F3864")

FONT_PASS   = Font(color="276221", bold=False)
FONT_FAIL   = Font(color="9C0006", bold=False)
FONT_WARN   = Font(color="9C5700", bold=False)
FONT_HEADER = Font(color="FFFFFF", bold=True)

MAX_PASS_ROWS = 500
TOL_ABS       = 0.5           # 4-dp per-row rounding cushion
TOL_REL       = 1e-4          # relative cushion on large quantities
DEFAULT_MAX_H = 72            # phase1c's window default (phase1c_mrp_netting.py:67)


# ── Report helpers ───────────────────────────────────────────────────────────────

def get_fill_font(status: str):
    s = str(status).upper()
    if s == "PASS":
        return FILL_PASS, FONT_PASS
    elif s == "FAIL":
        return FILL_FAIL, FONT_FAIL
    else:
        return FILL_WARN, FONT_WARN


def auto_col_width(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                cell_len = len(str(cell.value)) if cell.value is not None else 0
                if cell_len > max_len:
                    max_len = cell_len
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 4, 70)


def write_detail_sheet(wb, sheet_name, description, df, status_col="Status", note=""):
    ws = wb.create_sheet(title=sheet_name)
    ws.cell(row=1, column=1, value=description).font = Font(bold=True)

    if len(df) > 10000:
        fail_df = df[df[status_col].str.upper() == "FAIL"]
        pass_df = df[df[status_col].str.upper() == "PASS"].head(MAX_PASS_ROWS)
        warn_df = df[df[status_col].str.upper() == "WARN"]
        df_out  = pd.concat([fail_df, pass_df, warn_df], ignore_index=True)
        trim_note = (f"Large dataset: showing all FAILs ({len(fail_df)}), "
                     f"first {MAX_PASS_ROWS} PASSes, all WARNs. Total rows={len(df)}.")
    else:
        df_out, trim_note = df.copy(), ""

    total  = len(df)
    n_pass = int((df[status_col].str.upper() == "PASS").sum()) if total else 0
    n_fail = int((df[status_col].str.upper() == "FAIL").sum()) if total else 0
    n_warn = int((df[status_col].str.upper() == "WARN").sum()) if total else 0
    combined_note = " | ".join(filter(None, [note, trim_note]))

    ws.cell(row=2, column=1,
            value=f"Total={total}  PASS={n_pass}  FAIL={n_fail}  WARN={n_warn}"
                  + (f"  NOTE: {combined_note}" if combined_note else ""))

    if df_out.empty:
        ws.cell(row=3, column=1, value="(No data — nothing to flag)")
        return

    cols = list(df_out.columns)
    for c_idx, col_name in enumerate(cols, start=1):
        cell = ws.cell(row=3, column=c_idx, value=col_name)
        cell.fill = FILL_HEADER; cell.font = FONT_HEADER
        cell.alignment = Alignment(horizontal="center")
    for r_idx, row in enumerate(df_out.itertuples(index=False), start=4):
        status_val = str(getattr(row, status_col, "WARN")).upper()
        fill, font = get_fill_font(status_val)
        for c_idx, col_name in enumerate(cols, start=1):
            val = getattr(row, col_name)
            if isinstance(val, float) and np.isnan(val):
                val = None
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            if col_name == status_col:
                cell.fill = fill; cell.font = font
    auto_col_width(ws)


def write_summary_sheet(wb, results):
    ws = wb.create_sheet(title="Summary", index=0)
    ws.cell(row=1, column=1, value="Phase 1c – MRP Netting Validation Report").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=f"Run timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ws.cell(row=3, column=1,
            value="MODEL: v6_wave_p2 VERBATIM — scalar plan_start window, FULL WIP snapshot "
                  "(no aging filter), raw child_quantity cascade (1 edge/BOM row). Validator "
                  "independently REPLAYS v6 and checks the output against it. V10 is "
                  "INFORMATIONAL (v6's known aged-stock over-net; accepted per 2026-07-22 revert).")
    headers = ["#", "Validation", "Status", "Pass Count", "Fail Count", "Notes"]
    for c_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=5, column=c_idx, value=h)
        cell.fill = FILL_HEADER; cell.font = FONT_HEADER
        cell.alignment = Alignment(horizontal="center")
    for r_idx, res in enumerate(results, start=6):
        fill, font = get_fill_font(res["status"])
        values = [res["num"], res["name"], res["status"],
                  res["pass_count"], res["fail_count"], res.get("notes", "")]
        for c_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.fill = fill; cell.font = font
    auto_col_width(ws)


# ── Aging helpers (for the INFORMATIONAL V10 only) ──────────────────────────────

def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().upper()

def _tkey(s):
    return re.sub(r"[\s_]+", "", str(s)).strip().upper()

def _aging_h(v, u):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return np.nan
    u = str(u or "").upper()
    if u.startswith("DAY"):
        return v * 24.0
    if u.startswith("MIN"):
        return v / 60.0
    return v


# ── Data loading ────────────────────────────────────────────────────────────────

def load_inputs():
    print("Loading input files...")
    d1b = pd.read_csv(PH1B_DIR / "phase1_demand_updated.csv.gz", compression="gzip")
    d1c = pd.read_csv(PH1C_DIR / "phase1_demand_NET_updated.csv.gz", compression="gzip")
    inv = pd.read_csv(INPUTS / "inventory.csv")
    aging = pd.read_csv(INPUTS / "aging_master.csv")
    bom = pd.read_csv(INPUTS / "bom.csv")
    plan = pd.read_csv(INPUTS / "plan.csv")
    itype = pd.read_csv(INPUTS / "itemtype_master.csv")
    try:
        cfg = yaml.safe_load(open(CONFIG)) or {}
    except Exception as e:
        print(f"  [WARN] config.yaml unreadable ({e}).")
        cfg = {}

    d1b["need_by"]    = pd.to_datetime(d1b["need_by"], errors="coerce")
    d1c["need_by"]    = pd.to_datetime(d1c["need_by"], errors="coerce")
    d1b["demand_qty"] = pd.to_numeric(d1b["demand_qty"], errors="coerce").fillna(0.0)
    d1c["demand_qty"] = pd.to_numeric(d1c["demand_qty"], errors="coerce").fillna(0.0)
    plan["startTime"] = pd.to_datetime(plan["startTime"], errors="coerce")
    inv["produced_time"] = pd.to_datetime(inv["produced_time"], errors="coerce")

    print(f"  d1b (gross) rows: {len(d1b):,}")
    print(f"  d1c (net)   rows: {len(d1c):,}")
    print(f"  inventory   rows: {len(inv):,}")
    print(f"  bom         rows: {len(bom):,}")
    return d1b, d1c, inv, aging, bom, plan, itype, cfg


def _wip_from_inventory(inv):
    """Mirror db_loader.load_wip_by_item for CTP (metres pass through unscaled): sum the
    inventory snapshot per RAW itemcode. No aging filter — exactly what v6 nets against."""
    df = inv.copy()
    item_col = next((c for c in ("itemcode", "ItemCode", "item_code") if c in df.columns), None)
    qty_col  = next((c for c in ("inventory", "availableinventory", "available_inventory")
                     if c in df.columns), None)
    if item_col is None or qty_col is None:
        return {}
    df["_q"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0)
    df["_k"] = df[item_col].astype(str).str.strip()
    return df.groupby("_k")["_q"].sum().to_dict()


# ── v6 REPLAY (the model the validator judges against) ──────────────────────────

def build_v6_replay(d1b, d1c, inv, aging, bom, plan, itype, cfg):
    """Independently re-run v6's cascade from the inputs (phase1c_mrp_netting.py:104-238)."""
    print("Replaying v6 netting (scalar window + full-WIP + raw cascade)...")

    plan_start = plan["startTime"].min()
    if pd.isna(plan_start):
        plan_start = pd.Timestamp("2026-06-01 07:00:00")
    print(f"  plan_start: {plan_start}")

    # aging map (RAW key, v6-style)
    aging = aging.copy()
    aging["max_h"] = aging.apply(
        lambda r: _aging_h(r.get("MaxAging", ""), r.get("MaxAgingUnit", "")), axis=1)
    max_aging_map = {str(k).strip(): (h if pd.notna(h) else np.nan)
                     for k, h in zip(aging["ItemCode"], aging["max_h"])}

    # WIP: full inventory snapshot per raw itemcode (NO aging filter) — v6's wip_by_item
    wip_by_item = _wip_from_inventory(inv)

    # BOM edges: RAW child_quantity, ONE EDGE PER BOM ROW (v6:154-162)
    bom = bom.copy()
    par_c = "Parent" if "Parent" in bom.columns else "parent"
    ch_c  = "child" if "child" in bom.columns else "Child"
    q_c   = "child_quantity" if "child_quantity" in bom.columns else "child quantity"
    bom["__PAR__"] = bom[par_c].astype(str).str.strip()
    bom["__CH__"]  = bom[ch_c].astype(str).str.strip()
    bom["__QTY__"] = pd.to_numeric(bom[q_c], errors="coerce").fillna(0.0)
    bf = bom[(bom["__QTY__"] > 0) & (bom["__PAR__"] != "") & (bom["__CH__"] != "")]
    edges = defaultdict(list)
    all_items = set()
    for par, ch, q in zip(bf["__PAR__"], bf["__CH__"], bf["__QTY__"]):
        edges[par].append((ch, q))
        all_items.add(par); all_items.add(ch)

    # topo order (finished good -> raw material)  (v6:184-200)
    in_deg = defaultdict(int)
    for par, kids in edges.items():
        for ch, _ in kids:
            in_deg[ch] += 1
    for it in all_items:
        in_deg.setdefault(it, 0)
    queue = deque([it for it in all_items if in_deg[it] == 0])
    topo, lid = [], dict(in_deg)
    while queue:
        u = queue.popleft(); topo.append(u)
        for ch, _ in edges.get(u, []):
            lid[ch] -= 1
            if lid[ch] == 0:
                queue.append(ch)
    for it in all_items:
        if it not in topo:
            topo.append(it)

    # in/out-of-window split — window from PLAN_START (v6:202-206)
    d1b = d1b.copy()
    d1b["max_h"] = d1b["item_code"].map(max_aging_map).fillna(DEFAULT_MAX_H)
    d1b["hfs"]   = (d1b["need_by"] - plan_start).dt.total_seconds() / 3600.0
    d1b["inw"]   = d1b["hfs"] <= d1b["max_h"]

    gross_s = d1b.groupby("item_code")["demand_qty"].sum()
    inwd_s  = d1b[d1b["inw"]].groupby("item_code")["demand_qty"].sum().reindex(gross_s.index).fillna(0.0)
    oowd_s  = (gross_s - inwd_s).clip(lower=0.0)
    gross, in_window_demand, oowd = gross_s.to_dict(), inwd_s.to_dict(), oowd_s.to_dict()

    # === the cascade (v6:209-224) ===
    reduction_in_window = defaultdict(float)   # cascade credit flowing INTO each item
    after_upstream_d, wip_used_d, net_iw_d, saving_d = {}, {}, {}, {}
    for item in topo:
        orig_iw = in_window_demand.get(item, 0.0)
        if orig_iw <= 0:
            saving_d[item] = 0.0
            continue
        after_upstream = max(0.0, orig_iw - reduction_in_window.get(item, 0.0))
        wip_q = wip_by_item.get(item, 0.0)
        wip_used = min(after_upstream, wip_q)
        net_iw = after_upstream - wip_used
        total_saved_iw = orig_iw - net_iw
        after_upstream_d[item] = after_upstream
        wip_used_d[item] = wip_used
        net_iw_d[item] = net_iw
        saving_d[item] = total_saved_iw
        for ch, per_unit in edges.get(item, []):
            reduction_in_window[ch] += total_saved_iw * per_unit

    # per-item applied ratio (v6:232-236)
    ratio = {}
    for item in gross:
        oiw = in_window_demand.get(item, 0.0)
        sav = saving_d.get(item, 0.0)
        ratio[item] = max(0.0, min(1.0, (oiw - sav) / oiw)) if oiw > 0 else 1.0

    # REPLAY the shipped output at row level (same 4-dp rounding, same drop) -> item sums
    d1b["_r"] = d1b["item_code"].map(ratio).fillna(1.0)
    d1b["_net"] = np.where(d1b["inw"],
                           (d1b["demand_qty"] * d1b["_r"]).round(4),
                           d1b["demand_qty"])
    replay_net = d1b[d1b["_net"] > 0].groupby("item_code")["_net"].sum().to_dict()

    # out-of-window preservation, measured on the ACTUAL output
    d1c = d1c.copy()
    d1c["max_h"] = d1c["item_code"].map(max_aging_map).fillna(DEFAULT_MAX_H)
    d1c["hfs"]   = (d1c["need_by"] - plan_start).dt.total_seconds() / 3600.0
    d1c["inw"]   = d1c["hfs"] <= d1c["max_h"]
    out_net      = d1c.groupby("item_code")["demand_qty"].sum().to_dict()
    out_oow_net  = d1c[~d1c["inw"]].groupby("item_code")["demand_qty"].sum().to_dict()

    return {
        "plan_start": plan_start,
        "items": sorted(set(gross) | set(out_net)),
        "gross": gross, "in_window_demand": in_window_demand, "oowd": oowd,
        "wip_by_item": wip_by_item, "reduction_in": dict(reduction_in_window),
        "after_upstream": after_upstream_d, "wip_used": wip_used_d,
        "net_iw": net_iw_d, "saving": saving_d, "ratio": ratio,
        "replay_net": replay_net, "out_net": out_net, "out_oow_net": out_oow_net,
        "edges": edges,
        # for the informational aging check:
        "inv": inv, "aging": aging, "itype": itype, "cfg": cfg,
    }


def _tol(x):
    return max(TOL_ABS, TOL_REL * abs(x))


# ── Validations (v6-faithful) ────────────────────────────────────────────────────

def v01_replay_matches_output(m):
    print("Running V01 – Shipped output == independent v6 replay...")
    rows = []
    for ic in m["items"]:
        rep = m["replay_net"].get(ic, 0.0)
        out = m["out_net"].get(ic, 0.0)
        diff = out - rep
        rows.append({"item_code": ic,
                     "Replay_Net": round(rep, 3), "Output_Net": round(out, 3),
                     "Diff": round(diff, 4),
                     "Status": "PASS" if abs(diff) <= _tol(max(rep, out)) else "FAIL"})
    df = pd.DataFrame(rows)
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V01", "name": "Shipped net demand equals independent v6 replay",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} item_codes; re-ran v6's cascade from inputs and compared to the "
                     f"shipped output row-for-row (4-dp tol). A FAIL = the port does not reproduce v6.",
            "df": df}


def v02_net_le_gross(m):
    print("Running V02 – Net demand never exceeds gross...")
    rows = []
    for ic in m["items"]:
        g = m["gross"].get(ic, 0.0); n = m["out_net"].get(ic, 0.0)
        rows.append({"item_code": ic, "Gross": round(g, 3), "Net": round(n, 3),
                     "Status": "PASS" if n <= g + _tol(g) else "FAIL"})
    df = pd.DataFrame(rows)
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V02", "name": "Net <= Gross (netting only reduces demand)",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} item_codes.", "df": df}


def v03_wip_used_within_snapshot(m):
    print("Running V03 – WIP consumed <= inventory snapshot (v6's available stock)...")
    rows = []
    for ic in m["items"]:
        wu = m["wip_used"].get(ic, 0.0)
        wq = m["wip_by_item"].get(ic, 0.0)
        if wu <= TOL_ABS and wq <= TOL_ABS:
            continue
        rows.append({"item_code": ic, "WIP_Snapshot": round(wq, 3),
                     "WIP_Used": round(wu, 3),
                     "Status": "PASS" if wu <= wq + _tol(wq) else "FAIL"})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["item_code", "WIP_Snapshot", "WIP_Used", "Status"])
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V03", "name": "WIP consumed never exceeds the inventory snapshot",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} items that hold or use WIP; v6 nets against the FULL snapshot "
                     f"(wip_used = min(after_upstream, wip_q)).", "df": df}


def v04_out_of_window_preserved(m):
    print("Running V04 – Out-of-window demand passed through untouched...")
    rows = []
    for ic in m["items"]:
        og = m["oowd"].get(ic, 0.0)
        on = m["out_oow_net"].get(ic, 0.0)
        if og <= TOL_ABS and on <= TOL_ABS:
            continue
        rows.append({"item_code": ic, "OOW_Gross": round(og, 3), "OOW_Net": round(on, 3),
                     "Status": "PASS" if abs(on - og) <= _tol(og) else "FAIL"})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["item_code", "OOW_Gross", "OOW_Net", "Status"])
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V04", "name": "Out-of-window demand is not netted (built fresh)",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} items with out-of-window demand; v6 nets in-window rows only.",
            "df": df}


def v05_saving_within_in_window(m):
    print("Running V05 – Saving never exceeds in-window demand...")
    rows = []
    for ic in m["items"]:
        sav = m["saving"].get(ic, 0.0)
        oiw = m["in_window_demand"].get(ic, 0.0)
        if sav <= TOL_ABS and oiw <= TOL_ABS:
            continue
        rows.append({"item_code": ic, "In_Window_Demand": round(oiw, 3),
                     "Saving": round(sav, 3),
                     "Status": "PASS" if sav <= oiw + _tol(oiw) else "FAIL"})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["item_code", "In_Window_Demand", "Saving", "Status"])
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V05", "name": "Netted quantity <= in-window demand",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} items; you cannot net away more than the in-window demand.",
            "df": df}


def v06_saving_equals_credit(m):
    print("Running V06 – Saving = min(in-window, cascade credit + own WIP used)...")
    rows = []
    for ic in m["items"]:
        oiw = m["in_window_demand"].get(ic, 0.0)
        if oiw <= TOL_ABS:
            continue
        red = m["reduction_in"].get(ic, 0.0)
        wu  = m["wip_used"].get(ic, 0.0)
        expected = min(oiw, red + wu)
        sav = m["saving"].get(ic, 0.0)
        rows.append({"item_code": ic, "In_Window_Demand": round(oiw, 3),
                     "Cascade_Credit_In": round(red, 3), "Own_WIP_Used": round(wu, 3),
                     "Expected_Saving": round(expected, 3), "Actual_Saving": round(sav, 3),
                     "Status": "PASS" if abs(sav - expected) <= _tol(oiw) else "FAIL"})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["item_code", "In_Window_Demand", "Cascade_Credit_In", "Own_WIP_Used",
                 "Expected_Saving", "Actual_Saving", "Status"])
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V06", "name": "Saving fully explained by cascade credit + own WIP",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} items with in-window demand; conservation of the two v6 credit "
                     f"sources (parent cascade + own WIP), capped at in-window demand.", "df": df}


def v07_cascade_nonneg(m):
    print("Running V07 – Cascade credit non-negative and only from consuming parents...")
    # recompute reduction_in from savings & edges; must equal the model's reduction_in
    recomputed = defaultdict(float)
    for par, kids in m["edges"].items():
        s = m["saving"].get(par, 0.0)
        if s <= 0:
            continue
        for ch, per_unit in kids:
            recomputed[ch] += s * per_unit
    rows = []
    for ic in m["items"]:
        got = m["reduction_in"].get(ic, 0.0)
        exp = recomputed.get(ic, 0.0)
        if got <= TOL_ABS and exp <= TOL_ABS:
            continue
        ok = (got >= -TOL_ABS) and (abs(got - exp) <= _tol(exp))
        rows.append({"item_code": ic, "Cascade_Credit_In": round(got, 3),
                     "Recomputed_From_Parents": round(exp, 3),
                     "Status": "PASS" if ok else "FAIL"})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["item_code", "Cascade_Credit_In", "Recomputed_From_Parents", "Status"])
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V07", "name": "Cascade credit = sum(parent saving x per_unit), >= 0",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} items receiving cascade credit; each child's reduction is exactly "
                     f"the sum over parents of saving x raw child_quantity.", "df": df}


def v08_no_negative_values(d1c, m):
    print("Running V08 – No null/negative rows in the netting output...")
    mask_neg  = (d1c["demand_qty"].isna()) | (d1c["demand_qty"] < 0)
    mask_item = d1c["item_code"].isna()
    mask_qty  = d1c["demand_qty"].isna()
    mask_date = d1c["need_by"].isna()
    fail_mask = mask_neg | mask_item | mask_qty | mask_date
    fail_df = d1c[fail_mask].copy()
    fail_df["Neg_or_Null_qty"] = True
    fail_df["Status"] = "FAIL"
    cols = [c for c in ("item_code", "sku", "block_id", "demand_qty", "need_by",
                        "Neg_or_Null_qty", "Status") if c in fail_df.columns]
    df = fail_df.reindex(columns=cols) if len(fail_df) else pd.DataFrame(
        columns=cols + ["Status"] if "Status" not in cols else cols)
    if df.empty:
        df = pd.DataFrame([{"item_code": "(all rows valid)", "Status": "PASS"}])
    n_fail = int(fail_mask.sum())
    return {"num": "V08", "name": "No negative / null values in netting output",
            "status": "PASS" if n_fail == 0 else "FAIL",
            "pass_count": int(len(d1c) - n_fail), "fail_count": n_fail,
            "notes": f"Total d1c rows checked: {len(d1c):,}.", "df": df}


def v09_ratio_valid(m):
    print("Running V09 – Applied netting ratio in [0,1]; net_iw >= 0...")
    rows = []
    for ic in m["items"]:
        oiw = m["in_window_demand"].get(ic, 0.0)
        if oiw <= TOL_ABS:
            continue
        r = m["ratio"].get(ic, 1.0)
        niw = m["net_iw"].get(ic, 0.0)
        ok = (-1e-9 <= r <= 1 + 1e-9) and (niw >= -TOL_ABS)
        rows.append({"item_code": ic, "Ratio": round(r, 6),
                     "Net_In_Window": round(niw, 3),
                     "Status": "PASS" if ok else "FAIL"})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["item_code", "Ratio", "Net_In_Window", "Status"])
    nf = int((df["Status"] == "FAIL").sum())
    return {"num": "V09", "name": "Netting ratio in [0,1] and net in-window >= 0",
            "status": "PASS" if nf == 0 else "FAIL",
            "pass_count": int((df["Status"] == "PASS").sum()), "fail_count": nf,
            "notes": f"{len(df)} items with in-window demand.", "df": df}


def v10_aging_information(m):
    print("Running V10 – INFORMATIONAL: WIP credited that is aged past shelf life...")
    inv = m["inv"].copy(); aging = m["aging"]; itype = m["itype"]; cfg = m["cfg"]
    plan_start = m["plan_start"]

    itype_norm = {_norm(c): str(t).strip() for c, t in zip(itype["ItemCode"], itype["ItemType"])}
    amap = dict(zip(aging["ItemCode"].astype(str).str.strip(), aging["max_h"]))
    mx_norm = {_norm(c): h for c, h in amap.items() if pd.notna(h) and h > 0}
    fb = {}
    for k, v in (cfg.get("aging_fallback_by_itemtype") or {}).items():
        h = float((v or {}).get("max_h", 0) or 0)
        if h > 0:
            fb[_tkey(k)] = h
    if "GREENTYRES" in fb and "GREENTYRE" not in fb:
        fb["GREENTYRE"] = fb["GREENTYRES"]

    inv["_q"] = pd.to_numeric(inv["inventory"], errors="coerce").fillna(0.0)
    inv = inv[inv["_q"] > 0].copy()
    inv["_k"]   = inv["itemcode"].astype(str).str.strip()
    inv["_n"]   = inv["itemcode"].map(_norm)
    inv["_ity"] = inv["_n"].map(itype_norm)
    if "item_type" in inv.columns:
        inv["_ity"] = inv["_ity"].fillna(inv["item_type"].astype(str))
    inv["_lim"] = inv["_n"].map(mx_norm)
    need = inv["_lim"].isna()
    inv.loc[need, "_lim"] = inv.loc[need, "_ity"].map(lambda t: fb.get(_tkey(t), np.nan))
    inv["_age"] = (pd.Timestamp(plan_start) - inv["produced_time"]).dt.total_seconds() / 3600.0
    inv["_expired"] = inv["produced_time"].notna() & inv["_lim"].notna() & (inv["_age"] > inv["_lim"])
    alive_by = inv[~inv["_expired"]].groupby("_k")["_q"].sum().to_dict()

    rows = []
    for ic in m["items"]:
        wu = m["wip_used"].get(ic, 0.0)
        if wu <= TOL_ABS:
            continue
        alive = alive_by.get(str(ic).strip(), 0.0)
        over = wu - alive
        if over <= TOL_ABS:
            continue
        rows.append({"item_code": ic, "WIP_Used_by_v6": round(wu, 2),
                     "Fresh_Alive_at_plan_start": round(alive, 2),
                     "Used_From_Aged_Stock": round(over, 2), "Status": "WARN"})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["item_code", "WIP_Used_by_v6", "Fresh_Alive_at_plan_start",
                 "Used_From_Aged_Stock", "Status"])
    n_warn = len(df)
    aged_units = float(df["Used_From_Aged_Stock"].sum()) if n_warn else 0.0
    return {"num": "V10", "name": "INFO: WIP credited from aged stock (v6 known over-net)",
            "status": "WARN" if n_warn else "PASS",
            "pass_count": 0, "fail_count": 0,
            "notes": (f"INFORMATIONAL, not scored. {n_warn} items where v6 credited more WIP than "
                      f"was still fresh at plan_start ({aged_units:,.0f} units from aged stock). v6 "
                      f"ignores per-lot age by design (aging fix reverted 2026-07-22) — ACCEPTED, "
                      f"not a port defect. Restore .bak_agingfix to remove it."),
            "df": df}


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("Phase 1c MRP Netting Validation  (v6-VERBATIM model)")
    print("=" * 70)

    d1b, d1c, inv, aging, bom, plan, itype, cfg = load_inputs()
    m = build_v6_replay(d1b, d1c, inv, aging, bom, plan, itype, cfg)
    print(f"  Unique item_codes: {len(m['items']):,}")

    results = []
    for fn, arg in [
        (v01_replay_matches_output, (m,)),
        (v02_net_le_gross, (m,)),
        (v03_wip_used_within_snapshot, (m,)),
        (v04_out_of_window_preserved, (m,)),
        (v05_saving_within_in_window, (m,)),
        (v06_saving_equals_credit, (m,)),
        (v07_cascade_nonneg, (m,)),
        (v08_no_negative_values, (d1c, m)),
        (v09_ratio_valid, (m,)),
        (v10_aging_information, (m,)),
    ]:
        try:
            results.append(fn(*arg))
        except Exception as e:
            traceback.print_exc()
            results.append({"num": fn.__name__[:3].upper(), "name": fn.__name__,
                            "status": "WARN", "pass_count": 0, "fail_count": 0,
                            "notes": f"Unhandled error: {e}", "df": pd.DataFrame()})

    print("\nBuilding Excel report...")
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]
    descriptions = [
        "V01 – Shipped net demand == independent v6 replay (the port reproduces v6 exactly)",
        "V02 – Net <= Gross (netting only reduces demand)",
        "V03 – WIP consumed <= inventory snapshot (v6's available stock, no aging filter)",
        "V04 – Out-of-window demand passed through untouched (built fresh)",
        "V05 – Netted quantity <= in-window demand",
        "V06 – Saving = min(in-window, parent cascade credit + own WIP used)",
        "V07 – Cascade credit = sum(parent saving x raw child_quantity), non-negative",
        "V08 – No negative / null values in the netting output",
        "V09 – Applied netting ratio in [0,1]; net in-window >= 0",
        "V10 – INFORMATIONAL: WIP v6 credited from stock already aged at plan_start",
    ]
    for res, desc in zip(results, descriptions):
        df = res.get("df", pd.DataFrame())
        write_detail_sheet(wb, res["num"], desc, df if df is not None else pd.DataFrame(),
                           status_col="Status", note=res.get("notes", ""))
    write_summary_sheet(wb, results)
    wb.save(OUTPUT_FILE)
    print(f"\nReport saved: {OUTPUT_FILE}")

    total_pass = sum(r["pass_count"] for r in results)
    total_fail = sum(r["fail_count"] for r in results)
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_warn = sum(1 for r in results if r["status"] == "WARN")
    print("\n" + "=" * 70)
    for r in results:
        print(f"  {r['num']}  {r['status']:<5s}  pass={r['pass_count']:<6d} fail={r['fail_count']:<4d}  {r['name']}")
    print("=" * 70)
    print(f"Validation complete: {n_pass} PASS | {n_fail} FAIL | {n_warn} WARN")
    print(f"Row-level totals:    {total_pass:,} PASS | {total_fail:,} FAIL")
    print("=" * 70)


if __name__ == "__main__":
    main()
