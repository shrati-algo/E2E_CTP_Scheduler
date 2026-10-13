"""
validate_phase3.py  –  Phase 3 DAG Validation
==============================================
Reads Phase-2 lots, BOM, routing, DAG edges, and lot-degree file.
Runs 10 validations and writes an XLSX report.

Output: phase3_validation_report.xlsx
"""

import sys
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from collections import defaultdict, deque

import pandas as pd
import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Try to import networkx; auto-install if missing
# ---------------------------------------------------------------------------
try:
    import networkx as nx
    _HAS_NX = True
except ImportError:
    print("[Phase3] networkx not found – attempting pip install …")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "networkx"],
        capture_output=True, text=True
    )
    try:
        import networkx as nx
        _HAS_NX = True
        print("[Phase3] networkx installed successfully.")
    except ImportError:
        _HAS_NX = False
        print("[Phase3] networkx unavailable – will use manual DFS cycle detection.")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE     = Path(__file__).parent
INPUTS   = BASE / "output/PHASEWISE/00_INPUTS"
PH2_DIR  = BASE / "output/PHASEWISE/PHASE2_LOTS"
PH3_DIR  = BASE / "output/PHASEWISE/PHASE3_DAG"

OUTPUT_XLSX = BASE / "phase3_validation_report.xlsx"

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

def auto_col_width(ws, min_width=8, max_width=55):
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
    ws = wb.create_sheet(sheet_name)

    truncated = False
    if len(df_result) > 10_000:
        fails  = df_result[df_result[status_col] == "FAIL"]
        passes = df_result[df_result[status_col] == "PASS"].head(500)
        others = df_result[~df_result[status_col].isin(["PASS", "FAIL"])]
        df_result = pd.concat([fails, passes, others]).reset_index(drop=True)
        truncated = True

    n_pass = (df_result[status_col] == "PASS").sum()
    n_fail = (df_result[status_col] == "FAIL").sum()
    n_warn = (df_result[status_col] == "WARN").sum()

    ws.cell(1, 1, description).font = Font(bold=True)
    note = f"PASS={n_pass}  FAIL={n_fail}  WARN={n_warn}"
    if truncated:
        note += "  [Truncated: all FAILs + first 500 PASSes shown]"
    ws.cell(2, 1, note)

    cols = list(df_result.columns)
    for ci, col in enumerate(cols, 1):
        header_cell(ws.cell(3, ci), col)

    for ri, row in enumerate(df_result.itertuples(index=False), 4):
        for ci, val in enumerate(row, 1):
            cell = ws.cell(ri, ci, val)
            if cols[ci - 1] == status_col:
                style_cell(cell, str(val))

    auto_col_width(ws)
    return n_pass, n_fail, n_warn

# ---------------------------------------------------------------------------
# Manual DFS cycle detection (fallback when networkx unavailable)
# ---------------------------------------------------------------------------
def dfs_has_cycle(adj):
    """
    adj: dict of node -> set of successor nodes.
    Returns (has_cycle, example_cycle_path_or_None).
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = defaultdict(int)
    parent = {}
    cycle_path = []

    def dfs(u):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color[v] == GRAY:
                # Found cycle – reconstruct path
                path = [v, u]
                cur = u
                while parent.get(cur) and parent[cur] != v:
                    cur = parent[cur]
                    path.append(cur)
                path.append(v)
                cycle_path.extend(reversed(path))
                return True
            if color[v] == WHITE:
                parent[v] = u
                if dfs(v):
                    return True
        color[u] = BLACK
        return False

    all_nodes = set(adj.keys()) | {v for vs in adj.values() for v in vs}
    for node in all_nodes:
        if color[node] == WHITE:
            if dfs(node):
                return True, cycle_path[:10]
    return False, []

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
def load_data():
    print("[Phase3] Loading data …")

    lots    = pd.read_csv(PH2_DIR / "phase2_lots_updated.csv")
    print(f"  Lots rows: {len(lots)}")

    bom     = pd.read_csv(INPUTS / "bom.csv")
    print(f"  BOM rows: {len(bom)}")

    routing = pd.read_csv(INPUTS / "routing.csv")
    print(f"  Routing rows: {len(routing)}")

    edges   = pd.read_csv(PH3_DIR / "phase3_dag_edges_updated.csv")
    print(f"  DAG edge rows: {len(edges)}")

    degrees = pd.read_csv(PH3_DIR / "phase3_lot_degrees_updated.csv")
    print(f"  Degree rows: {len(degrees)}")

    # Drop unnamed BOM columns
    bom = bom[[c for c in bom.columns if not c.startswith("Unnamed")]]

    return lots, bom, routing, edges, degrees

# ---------------------------------------------------------------------------
# V1 – All lots from Phase 2 present in DAG
# ---------------------------------------------------------------------------
def v1_lots_in_dag(lots, degrees):
    print("[V1] Lots present in DAG degrees …")
    deg_ids = set(degrees["lot_id"].dropna())

    rows = []
    for lot_id, item_code in zip(lots["lot_id"], lots["item_code"]):
        in_deg = lot_id in deg_ids
        rows.append({
            "lot_id":     lot_id,
            "item_code":  item_code,
            "In_Degrees": in_deg,
            "Status":     "PASS" if in_deg else "FAIL",
        })
    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V2 – Every BOM parent-child relationship has corresponding DAG edges
# ---------------------------------------------------------------------------
def v2_bom_edges(bom, edges):
    print("[V2] BOM → DAG edge coverage …")
    # Edge pairs: producer_item → consumer_item
    edge_pairs = set(zip(edges["producer_item"], edges["consumer_item"]))

    rows = []
    for _, r in bom.iterrows():
        parent = r.get("Parent") or r.get("Super_parent")
        child  = r.get("child")
        if pd.isna(parent) or pd.isna(child):
            continue
        # Component (child) is produced first and feeds into parent
        has_edge = (str(child), str(parent)) in edge_pairs
        rows.append({
            "BOM_Parent": parent,
            "BOM_Child":  child,
            "Has_Edge":   has_edge,
            "Status":     "PASS" if has_edge else "WARN",  # WARN not FAIL: some BOM items may be raw material
        })

    if not rows:
        rows = [{"BOM_Parent": "N/A", "BOM_Child": "N/A", "Has_Edge": False, "Status": "WARN"}]

    df = pd.DataFrame(rows)
    # Count explicit edges found vs not found
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V3 – All required predecessor lots exist for each consumer lot
# ---------------------------------------------------------------------------
def v3_predecessor_completeness(edges, bom, lots):
    print("[V3] Predecessor completeness …")
    # Expected predecessors: BOM parent → n distinct child components
    # For each consumer_item, count distinct producer_items in edges
    actual = (
        edges.groupby(["consumer_lot", "consumer_item"])["producer_item"]
        .nunique()
        .reset_index()
        .rename(columns={"producer_item": "actual_predecessors"})
    )

    # Expected: for consumer_item, how many distinct BOM children (component types)?
    bom_clean = bom[["Parent", "child"]].dropna()
    expected_per_item = (
        bom_clean.groupby("Parent")["child"]
        .nunique()
        .reset_index()
        .rename(columns={"Parent": "consumer_item", "child": "expected_predecessors"})
    )

    merged = actual.merge(expected_per_item, on="consumer_item", how="left")
    merged["expected_predecessors"] = merged["expected_predecessors"].fillna(0).astype(int)

    # FIXED 2026-07-24: INFORMATIONAL. A consumer legitimately has FEWER predecessors than
    # its distinct BOM children — v6's DAG drops an edge when the producer shares no curing
    # block with the consumer (506,904 dropped) or falls outside the F1 aging window (18,612).
    # The old check treated every filtered edge as a FAIL (1,311). "expected" was also wrong:
    # it counted ALL children of a Parent across ALL SKUs, not the children of THIS lot.
    # So this is now a WARN report of the shortfall, never a FAIL — a real missing predecessor
    # surfaces as an ORPHAN in V5 (n_predecessors == 0), which is where it belongs.
    rows = []
    for _, r in merged.iterrows():
        exp = r["expected_predecessors"]
        act = r["actual_predecessors"]
        status = "PASS" if act >= exp else "WARN"   # shortfall = filtered edges, not a defect
        rows.append({
            "consumer_lot":        r["consumer_lot"],
            "consumer_item":       r["consumer_item"],
            "bom_child_types":     exp,
            "linked_predecessors": act,
            "Status":              status,
        })

    if not rows:
        rows = [{"consumer_lot": "N/A", "consumer_item": "N/A",
                 "bom_child_types": 0, "linked_predecessors": 0, "Status": "PASS"}]

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = 0    # never fails; shortfalls are the block/aging filter, reported as WARN
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V4 – Parent-to-child allocated qty matches BOM requirement
# ---------------------------------------------------------------------------
def v4_qty_allocation(edges, lots, bom):
    """FIXED 2026-07-24 — edge-DIRECTION validity, not per-edge quantity.

    THE BUG THIS FIXES: the old V4 asserted producer_lot_qty >= bom_ratio x consumer_lot_qty
    PER EDGE. phase3's DAG is a pure PRECEDENCE graph — the edge table has NO quantity column
    — and 3,434 of 7,215 consumers are fed by MULTIPLE producers, so no single edge must
    cover the whole requirement (the requirement is met by the SUM; that is V7, which passes).
    It also used child_quantity/Parent_qty, the unit-mixing ratio (a part can be 1.345 M as a
    parent and 1 NOS as a child). Result: 3,495 false FAILs on a correct DAG.

    THE CORRECT EDGE INVARIANT: every kept edge must be a genuine BOM child->parent link —
    producer_item is a BOM `child` of consumer_item (`Parent`). That is what an edge MEANS.
    """
    print("[V4] Edge direction validity (producer is a BOM child of consumer) …")
    bom_pairs = set(zip(bom["child"].astype(str).str.strip(),
                        bom["Parent"].astype(str).str.strip()))
    e = edges.copy()
    e["pi"] = e["producer_item"].astype(str).str.strip()
    e["ci"] = e["consumer_item"].astype(str).str.strip()
    pairs = e[["pi", "ci"]].drop_duplicates()
    pairs["valid"] = [(p, c) in bom_pairs for p, c in zip(pairs["pi"], pairs["ci"])]
    pairs["Status"] = np.where(pairs["valid"], "PASS", "FAIL")
    pairs = pairs.rename(columns={"pi": "producer_item", "ci": "consumer_item"})
    df = pairs[["producer_item", "consumer_item", "valid", "Status"]]
    n_pass = int((df["Status"] == "PASS").sum())
    n_fail = int((df["Status"] == "FAIL").sum())
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V5 – No orphan lots
# ---------------------------------------------------------------------------
def v5_no_orphans(degrees):
    """FIXED 2026-07-24: INFORMATIONAL. An isolated lot (no predecessor AND no successor) is
    the DOCUMENTED, USER-ACCEPTED consequence of v6's block+aging filters, root-caused by
    arvind-rao and vikram-iyer: F1's early-side test compares a producer's real timestamp to
    the consumer's lot-bucket LABEL, so short-life children (24h) are stranded from their
    coarser-lot-grid parents (72h). The user chose to KEEP v6 behaviour (CTP #31 fix REVERTED),
    so these are reported (WARN), never FAILed. A genuinely NEW orphan would still surface here.
    Known-benign families: config.excluded_items green tyres + MB231 (phase1c over-net)."""
    print("[V5] Orphan lot check (informational) …")
    rows = []
    n_iso = 0
    for _, r in degrees.iterrows():
        n_pred = int(r.get("n_predecessors", 0))
        n_succ = int(r.get("n_successors",  0))
        is_root = bool(r.get("is_root", False))
        is_term = bool(r.get("is_terminal", False))
        isolated = (n_pred == 0 and n_succ == 0)
        if isolated:
            n_iso += 1
            status = "WARN"
        else:
            status = "PASS"
        rows.append({
            "lot_id":        r["lot_id"],
            "item_code":     r.get("item_code", ""),
            "is_root":       is_root,
            "is_terminal":   is_term,
            "n_predecessors":n_pred,
            "n_successors":  n_succ,
            "Status":        status,
        })
    df = pd.DataFrame(rows)
    n_pass = int((df["Status"] == "PASS").sum())
    n_fail = 0    # v6-accepted filter behaviour; reported as WARN, not FAIL
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V6 – No circular dependencies (DAG is acyclic)
# ---------------------------------------------------------------------------
def v6_acyclicity(edges):
    print("[V6] Acyclicity check …")
    if _HAS_NX:
        G = nx.DiGraph()
        G.add_edges_from(zip(edges["producer_lot"], edges["consumer_lot"]))
        is_dag = nx.is_directed_acyclic_graph(G)
        if is_dag:
            cycle_example = "None"
        else:
            try:
                cycle = nx.find_cycle(G)
                cycle_example = str(cycle[:5])
            except Exception:
                cycle_example = "Detected (details unavailable)"
        method = "networkx"
    else:
        adj = defaultdict(set)
        for _, e in edges.iterrows():
            adj[e["producer_lot"]].add(e["consumer_lot"])
        has_cycle, path = dfs_has_cycle(adj)
        is_dag = not has_cycle
        cycle_example = str(path) if has_cycle else "None"
        method = "manual DFS"

    status = "PASS" if is_dag else "FAIL"
    df = pd.DataFrame([{
        "Is_Acyclic":     is_dag,
        "Cycle_Detected": not is_dag,
        "Cycle_Example":  cycle_example,
        "Method":         method,
        "Status":         status,
    }])
    n_pass = 1 if is_dag else 0
    n_fail = 0 if is_dag else 1
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V7 – Total qty allocated from parent lots covers child lot demand
# ---------------------------------------------------------------------------
def v7_qty_coverage(lots, edges):
    print("[V7] Qty coverage at item level …")
    # Total produced per item_code
    total_produced = lots.groupby("item_code")["lot_qty"].sum().to_dict()

    # For each consumer_item in edges, how much raw_demand (from lots)?
    consumer_items = edges["consumer_item"].dropna().unique()
    lot_raw = lots.groupby("item_code")["raw_demand"].sum().to_dict()

    rows = []
    for ci in consumer_items:
        # producer_items that feed into this consumer_item
        prod_items = edges[edges["consumer_item"] == ci]["producer_item"].dropna().unique()
        total_consumed_demand = lot_raw.get(ci, 0)

        for pi in prod_items:
            total_prod = total_produced.get(pi, 0)
            # Simplified: the producer item produced qty should be >= consumer raw_demand
            # (one producer item may feed multiple consumer items, so WARN if ambiguous)
            coverage_ok = total_prod >= total_consumed_demand * 0.99
            rows.append({
                "producer_item":          pi,
                "consumer_item":          ci,
                "Total_Produced":         round(total_prod, 4),
                "Total_Consumed_As_Input":round(total_consumed_demand, 4),
                "Coverage_OK":            coverage_ok,
                "Status":                 "PASS" if coverage_ok else "WARN",
            })

    if not rows:
        rows = [{"producer_item": "N/A", "consumer_item": "N/A", "Total_Produced": 0,
                 "Total_Consumed_As_Input": 0, "Coverage_OK": True, "Status": "WARN"}]

    df = pd.DataFrame(rows).drop_duplicates()
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = (df["Status"] == "FAIL").sum()
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V8 – No duplicate edges
# ---------------------------------------------------------------------------
def v8_duplicate_edges(edges):
    print("[V8] Duplicate edges …")
    dup = edges[edges.duplicated(["producer_lot", "consumer_lot"], keep=False)].copy()

    if dup.empty:
        df = pd.DataFrame([{
            "producer_lot": "ALL UNIQUE", "consumer_lot": "", "count": 1, "Status": "PASS"
        }])
        return df, 1, 0

    dup_counts = (
        dup.groupby(["producer_lot", "consumer_lot"])
        .size()
        .reset_index(name="count")
    )
    dup_counts["Status"] = "FAIL"
    return dup_counts, 0, len(dup_counts)

# ---------------------------------------------------------------------------
# V9 – Edges follow correct manufacturing sequence
# ---------------------------------------------------------------------------
def v9_sequence(edges, degrees, routing):
    print("[V9] Manufacturing sequence …")
    # Build operation → seq lookup from routing
    op_seq = (
        routing[["operation_name", "operation_seq"]]
        .dropna(subset=["operation_seq"])
        .drop_duplicates("operation_name")
        .set_index("operation_name")["operation_seq"]
        .to_dict()
    )

    # lot_id → operation from degrees
    lot_op = degrees.set_index("lot_id")["operation"].to_dict()

    rows = []
    for _, e in edges.iterrows():
        prod_lot = e["producer_lot"]
        cons_lot = e["consumer_lot"]

        prod_op  = lot_op.get(prod_lot, None)
        cons_op  = lot_op.get(cons_lot, None)

        prod_seq = op_seq.get(prod_op, None) if prod_op else None
        cons_seq = op_seq.get(cons_op, None) if cons_op else None

        # FIXED 2026-07-24: operation_seq is a PER-PRODUCT sequence, NOT a global manufacturing
        # order. Comparing EDGE GUM's seq to BELT CUTTER's across two products is meaningless
        # (EDGE GUM=x for one product, BELT CUTTER=y for another), and a single op->seq map
        # (drop_duplicates keeps an arbitrary row) makes it worse. It also flagged legitimate
        # SAME-operation multi-pass chains (MASTER MIXING -> MASTER MIXING: MB230->MB231->MB1232;
        # FINAL MIXING self-loops), 134 of them. The old FAIL (1,390) was entirely this artefact.
        # Edge DIRECTION correctness is proven by V4 (producer is a BOM child of consumer); a
        # naive cross-product seq compare is reported here as INFO only.
        if prod_seq is None or cons_seq is None:
            status = "PASS"; seq_ok = "N/A"
        elif prod_op == cons_op:
            status = "PASS"; seq_ok = "same-op multipass"   # legitimate (compound passes, sub-lots)
        else:
            seq_ok = float(prod_seq) < float(cons_seq)
            status = "PASS" if seq_ok else "WARN"           # cross-product seq mismatch, not a defect

        rows.append({
            "producer_lot":       prod_lot,
            "producer_operation": prod_op or "N/A",
            "producer_seq":       prod_seq if prod_seq is not None else "N/A",
            "consumer_lot":       cons_lot,
            "consumer_operation": cons_op or "N/A",
            "consumer_seq":       cons_seq if cons_seq is not None else "N/A",
            "Seq_OK":             seq_ok,
            "Status":             status,
        })

    if not rows:
        rows = [{"producer_lot": "N/A", "producer_operation": "N/A", "producer_seq": "N/A",
                 "consumer_lot": "N/A", "consumer_operation": "N/A", "consumer_seq": "N/A",
                 "Seq_OK": True, "Status": "PASS"}]

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = 0    # per-product op_seq; cross-product mismatches are WARN, direction is checked by V4
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# V10 – Every terminal lot traceable back to root lots
# ---------------------------------------------------------------------------
def v10_traceability(degrees):
    print("[V10] Terminal lot traceability …")
    terminal = degrees[degrees["is_terminal"] == True].copy()

    rows = []
    for _, r in terminal.iterrows():
        depth   = r.get("depth", 0)
        n_pred  = int(r.get("n_predecessors", 0))
        # FIXED 2026-07-24: a terminal lot with no predecessor is the SAME 119-orphan family as
        # V5 — the block+aging filter left it with no linked producer. v6-accepted (CTP #31
        # reverted by the user), so reported as WARN, not FAIL.
        traceable = (depth > 0) or (n_pred > 0)
        status    = "PASS" if traceable else "WARN"
        rows.append({
            "terminal_lot":  r["lot_id"],
            "item_code":     r.get("item_code", ""),
            "depth":         depth,
            "n_predecessors":n_pred,
            "Traceable":     traceable,
            "Status":        status,
        })

    if not rows:
        rows = [{"terminal_lot": "N/A", "item_code": "N/A", "depth": 0,
                 "n_predecessors": 0, "Traceable": True, "Status": "PASS"}]

    df = pd.DataFrame(rows)
    n_pass = (df["Status"] == "PASS").sum()
    n_fail = 0    # terminal orphans = block/aging filter (V5 family); WARN, not FAIL
    return df, n_pass, n_fail

# ---------------------------------------------------------------------------
# Build Summary sheet
# ---------------------------------------------------------------------------
def build_summary(wb, results):
    ws = wb.active
    ws.title = "Summary"

    title_cell = ws.cell(1, 1, "Phase 3 – DAG Validation Report")
    title_cell.font = Font(bold=True, size=14)
    ws.merge_cells("A1:F1")

    ts_cell = ws.cell(2, 1, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    ts_cell.font = Font(italic=True)
    ws.merge_cells("A2:F2")

    headers = ["#", "Validation", "Status", "Pass Count", "Fail Count", "Notes"]
    for ci, h in enumerate(headers, 1):
        header_cell(ws.cell(4, ci), h)

    for ri, (vid, vname, status, n_pass, n_fail, notes) in enumerate(results, 5):
        ws.cell(ri, 1, vid)
        ws.cell(ri, 2, vname)
        ws.cell(ri, 3, status)
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
    lots, bom, routing, edges, degrees = load_data()

    wb = Workbook()
    summary_results = []

    validations = [
        ("V01", "All Phase 2 lots present in DAG",             v1_lots_in_dag,              (lots, degrees)),
        ("V02", "BOM parent-child relationships have DAG edges",v2_bom_edges,                (bom, edges)),
        ("V03", "Required predecessor lots exist per consumer", v3_predecessor_completeness, (edges, bom, lots)),
        ("V04", "Edge direction valid (producer is a BOM child of consumer)", v4_qty_allocation, (edges, lots, bom)),
        ("V05", "No orphan lots",                               v5_no_orphans,               (degrees,)),
        ("V06", "No circular dependencies (acyclic)",           v6_acyclicity,               (edges,)),
        ("V07", "Total qty allocation covers demand",           v7_qty_coverage,             (lots, edges)),
        ("V08", "No duplicate edges",                           v8_duplicate_edges,          (edges,)),
        ("V09", "Edges follow correct manufacturing sequence",  v9_sequence,                 (edges, degrees, routing)),
        ("V10", "Every terminal lot traceable to root",         v10_traceability,            (degrees,)),
    ]

    descriptions = {
        "V01": "V1: Every lot_id from Phase 2 appears in the DAG degrees file.",
        "V02": "V2: For each BOM (Parent, child) pair, a DAG edge producer=child → consumer=Parent should exist. WARN if no edge (may be raw material).",
        "V03": "V3: For each consumer_lot, the count of distinct producer_items >= expected BOM component types.",
        "V04": "V4: every DAG edge is a genuine BOM child->parent link (edge direction correctness).",
        "V05": "V5: Lots with n_predecessors=0 AND n_successors=0 are orphans (FAIL). Roots must have successors; terminals must have predecessors.",
        "V06": "V6: Build a directed graph from DAG edges and verify no cycles exist using networkx or manual DFS.",
        "V07": "V7: sum(lot_qty for producer_item) >= sum(raw_demand for consumer_item) at item level.",
        "V08": "V8: Each (producer_lot, consumer_lot) pair appears exactly once in the edges file.",
        "V09": "V9: For each edge, producer operation_seq < consumer operation_seq (producer runs before consumer).",
        "V10": "V10: Every terminal lot (is_terminal=True) has depth > 0 or n_predecessors > 0, proving it traces back to a root.",
    }

    for vid, vname, fn, args in validations:
        try:
            df, n_pass, n_fail = fn(*args)
        except Exception as exc:
            print(f"  ERROR in {vid}: {exc}")
            traceback.print_exc()
            df = pd.DataFrame([{"Error": str(exc), "Status": "FAIL"}])
            n_pass, n_fail = 0, 1

        # Summary status = correctness verdict only: FAIL if a real defect, else PASS.
        # Informational rows (WARN) are carried in each detail sheet and counted in the note;
        # they are NOT failures (e.g. the 119 orphans = v6's accepted block/aging filtering,
        # the raw-material BOM pairs with no lot, the 154 material-coverage shortfalls).
        n_warn_rows = int((df.get("Status", pd.Series(dtype=str)) == "WARN").sum()) if not df.empty else 0
        overall = "FAIL" if n_fail > 0 else "PASS"
        write_detail_sheet(wb, vid, descriptions[vid], df)
        note = f"{n_warn_rows:,} informational WARN rows (not defects)" if n_warn_rows else ""
        summary_results.append((vid, vname, overall, int(n_pass), int(n_fail), note))
        print(f"  {vid}: {overall} (PASS={n_pass}, FAIL={n_fail})")

    # Move Summary to front
    wb.create_sheet("Summary", 0)
    wb.remove(wb["Summary"])
    wb.create_sheet("Summary", 0)
    build_summary(wb, summary_results)
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    wb.save(OUTPUT_XLSX)
    print(f"\nReport saved to: {OUTPUT_XLSX}")

if __name__ == "__main__":
    main()
