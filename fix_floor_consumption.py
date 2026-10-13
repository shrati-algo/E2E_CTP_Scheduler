#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_floor_consumption.py -- CTP #44: make `consumption_id` true on BUILDING and CURING rows.

THE DEFECT
  BUILDING: the building-plan echo replaces the phase-2 green-tyre lots with its own
    synthetic blocks (lot_id "BLD00024"). Those ids are not nodes in the phase-3 DAG,
    so the floor writer's _consumption_kit() found no predecessors for them and every
    building row fell through to its "RAW_MATERIAL" default. False -- building
    consumes Steel Belt, Ply, Bead Apex, Inner Liner, SideWall, Tread and Cap Strip
    lots, all of them scheduled in the same file. Silent: BUILDING was 100% RAW.
  CURING: phase 6 builds its floor rows from the press plan and never sets
    consumption_id at all, so all curing rows were blank. Curing consumes the green
    tyre that building produced.

THE REPAIR
  BUILDING  bind each BLD block to a real phase-2 GT lot (build-time order, by
            cumulative quantity within its GT code) -> that lot's phase-3 DAG
            predecessors are its components -> per component ITEM, name the lot
            already FINISHED by the block's start (FEFO). None finished yet => that
            component came from opening WIP => "WIP:<item>".
  CURING    map the cured SKU to its GT code, then name the building lot(s) of that
            GT already finished by the cure start (FEFO). None yet => "WIP:<GT>".

Both are FEFO-guarded, so a row can never name a lot that is not finished by the time
the row starts (no consume-before-make). IDEMPOTENT: re-running is a no-op.
ONLY the `consumption_id` cell is touched -- row count, times, quantities, machines,
lot_id and every other column are left exactly as they were.

USAGE
  python fix_floor_consumption.py <run_folder> [<run_folder> ...]
  python fix_floor_consumption.py --last 10          # newest 10 runs under output/
  python fix_floor_consumption.py                    # default: outputs2/ (live pipeline)
"""
import bisect
import glob
import os
import sys
from collections import defaultdict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DB = os.path.join(HERE, "db_pipeline", "raw")


# ── helpers ─────────────────────────────────────────────────────────────────────
def _find(run, *names):
    """Locate a phase artifact at the run root or anywhere under PHASEWISE/."""
    for n in names:
        p = os.path.join(run, n)
        if os.path.exists(p):
            return p
    pw = os.path.join(run, "PHASEWISE")
    if os.path.isdir(pw):
        for n in names:
            hits = glob.glob(os.path.join(pw, "*", n))
            if hits:
                return hits[0]
    return None


def _read(path, **kw):
    return pd.read_csv(path, low_memory=False,
                       compression="gzip" if str(path).endswith(".gz") else None, **kw)


def _fefo(pairs, t_ns):
    """Latest (finish_ns, lot) at or before t_ns; None if nothing has finished."""
    if not pairs:
        return None
    k = bisect.bisect_right([f for f, _ in pairs], t_ns) - 1
    return pairs[k][1] if k >= 0 else None


# ── the repair ──────────────────────────────────────────────────────────────────
def repair(run, verbose=True):
    floor = os.path.join(run, "phase5_floor_schedule_updated.csv")
    if not os.path.exists(floor):
        return {"run": run, "status": "no floor schedule"}

    fs = pd.read_csv(floor, low_memory=False)
    for c in ("consumption_id", "department", "process", "item", "lot_id",
              "start_time", "end_time", "produce_qty"):
        if c not in fs.columns:
            return {"run": run, "status": f"missing column {c}"}

    prod = fs[fs["process"].astype(str).str.upper() != "CHANGEOVER"].copy()
    prod["_s"] = pd.to_datetime(prod["start_time"], errors="coerce")
    prod["_e"] = pd.to_datetime(prod["end_time"], errors="coerce")
    lot_end = prod.groupby("lot_id")["_e"].max()          # lot -> finish

    dept = fs["department"].astype(str).str.upper()
    not_co = fs["process"].astype(str).str.upper() != "CHANGEOVER"
    is_bld = dept.str.contains("BUILDING", na=False) & not_co
    is_cur = dept.str.contains("CURING", na=False) & not_co

    # REPAIR ONLY WHAT IS BROKEN. A run that did NOT use the building echo keeps its
    # real phase-2 GT lot ids, so the floor writer's DAG lookup already produced an
    # EXACT per-lot kit -- re-deriving it from cumulative-quantity + FEFO would be a
    # strictly worse estimate. So a row is rewritten only when it is actually wrong:
    # blank, or the "RAW_MATERIAL" fall-through. This also makes the pass idempotent.
    cur_val = fs["consumption_id"]
    broken = cur_val.isna() | cur_val.astype(str).str.strip().isin(
        ["", "nan", "None", "RAW_MATERIAL"])
    is_bld_fix = is_bld & broken
    is_cur_fix = is_cur & broken
    out_keep_bld = int((is_bld & ~broken).sum())
    out_keep_cur = int((is_cur & ~broken).sum())

    out = {"run": os.path.basename(run.rstrip("/\\")),
           "bld_rows": int(is_bld.sum()), "cur_rows": int(is_cur.sum()),
           "bld_broken": int(is_bld_fix.sum()), "cur_broken": int(is_cur_fix.sum()),
           "bld_kept": out_keep_bld, "cur_kept": out_keep_cur,
           "bld_fixed": 0, "cur_fixed": 0, "bld_nomap": 0, "cur_nomap": 0,
           "wip_lines": 0, "status": "ok"}

    # ── BUILDING ────────────────────────────────────────────────────────────────
    p2 = _find(run, "phase2_lots_updated.csv")
    p3 = _find(run, "phase3_dag_edges_updated.csv.gz", "phase3_dag_edges_updated.csv",
               "phase3_dag_edges.csv.gz", "phase3_dag_edges.csv")
    gt_of_bld = {}                                        # floor row idx -> GT code (for curing)
    if is_bld_fix.any() and p2 and p3:
        L = _read(p2, usecols=["lot_id", "item_code", "item_type", "lot_qty"])
        # Green-tyre lots. item_type is the normal signal, but some runs (the combined
        # PCR+TBR ones) leave it blank on GT rows -- fall back to the lot-id prefix so
        # those runs are not silently treated as "no GT lots at all".
        gtL = L[L["item_type"].astype(str).str.contains("Green", case=False, na=False)
                | L["lot_id"].astype(str).str.startswith("GT ")].copy()
        code_lots = defaultdict(list)                     # GT code -> [(phase2 lot, qty)]
        for r in gtL.sort_values("lot_id").itertuples():
            lid = str(r.lot_id)
            if lid.startswith("BLD"):
                continue
            code_lots[str(r.item_code).strip()].append((lid, float(r.lot_qty or 0)))

        dag = _read(p3, usecols=["producer_lot", "consumer_lot"])
        preds = defaultdict(list)
        for p, c in zip(dag["producer_lot"].astype(str), dag["consumer_lot"].astype(str)):
            preds[c].append(p)

        code_item = defaultdict(lambda: defaultdict(list))   # GT code -> comp item -> [(fin,lot)]
        for code, gts in code_lots.items():
            for gl, _ in gts:
                for cl in preds.get(gl, []):
                    f = lot_end.get(cl)
                    if f is not None and pd.notna(f):
                        code_item[code][cl.split("_L")[0]].append((f.value, cl))
        for code in code_item:
            for itm in code_item[code]:
                code_item[code][itm].sort()

        b = fs[is_bld].copy()
        b["_s"] = pd.to_datetime(b["start_time"], errors="coerce")
        b["_q"] = pd.to_numeric(b["produce_qty"], errors="coerce").fillna(0.0)
        b["_code"] = b["item"].astype(str).str.strip()
        cons = {}
        for code, grp in b.groupby("_code"):
            lots = code_lots.get(code, [])
            if not lots:
                out["bld_nomap"] += int(broken.reindex(grp.index).fillna(False).sum())
                continue
            tot_lot = sum(q for _, q in lots) or 1.0
            tot_bld = grp["_q"].sum() or 1.0
            bounds, cc = [], 0.0
            for lid, q in lots:
                cc += q
                bounds.append((lid, cc / tot_lot))
            items = code_item.get(code, {})
            all_items = set(items)
            for gl, _ in lots:
                for cl in preds.get(gl, []):
                    all_items.add(cl.split("_L")[0])
            cum = 0.0
            for idx, row in grp.sort_values("_s").iterrows():
                cum += row["_q"]
                frac = (cum - row["_q"] / 2.0) / tot_bld
                gl = lots[-1][0]
                for cand, bf in bounds:
                    if frac <= bf:
                        gl = cand
                        break
                gt_of_bld[idx] = code
                T = row["_s"].value if pd.notna(row["_s"]) else 0
                parts = []
                for itm in sorted(all_items):
                    hit = _fefo(items.get(itm, ()), T)
                    if hit:
                        parts.append(hit)
                    else:
                        parts.append("WIP:" + itm)
                        out["wip_lines"] += 1
                if broken.at[idx]:          # never overwrite an already-exact kit
                    cons[idx] = ";".join(parts) if parts else "RAW_MATERIAL"
        if cons:
            fs.loc[list(cons), "consumption_id"] = pd.Series(cons)
            out["bld_fixed"] = len(cons)
    elif is_bld_fix.any():
        out["status"] = "building skipped (phase2/phase3 artifacts not found)"

    # ── CURING ──────────────────────────────────────────────────────────────────
    # cured SKU -> GT code, from the run's own phase2 lot->SKU map, else the routing.
    if is_cur_fix.any():
        sku2gt = {}
        pls = _find(run, "phase2_lot_skus_updated.csv")
        if pls:
            try:
                ls = _read(pls, usecols=["lot_id", "skus_set"])
                ls = ls[ls["lot_id"].astype(str).str.startswith("GT ")]
                for lid, sset in zip(ls["lot_id"].astype(str), ls["skus_set"].astype(str)):
                    gt = lid.split("_L")[0]
                    for s in sset.split(","):
                        s = s.strip()
                        if s:
                            sku2gt.setdefault(s, gt)
            except Exception:
                pass
        # routing is the fallback PER SKU (not all-or-nothing): phase2_lot_skus only
        # covers SKUs that actually got a GT lot, the routing covers every SKU.
        need = set(fs.loc[is_cur_fix, "item"].astype(str).str.strip()) - set(sku2gt)
        # BOTH product routings -- a combined PCR+TBR run cures TBR SKUs that the PCR
        # routing has never heard of, and they would otherwise all read NO_GT_BUILT.
        for rf in ("jkt_routing_pcr.csv", "jkt_routing_tbr.csv"):
            if not need:
                break
            rt = os.path.join(RAW_DB, rf)
            if not os.path.exists(rt):
                continue
            r = _read(rt, usecols=["finished_product", "routed_product", "operation_name"])
            r = r[r["operation_name"].astype(str).str.upper().str.contains("BUILDING", na=False)]
            for s, g in zip(r["finished_product"].astype(str).str.strip(),
                            r["routed_product"].astype(str).str.strip()):
                if s in need:
                    sku2gt.setdefault(s, g)
            need -= set(sku2gt)

        # GT code -> [(completion_ns, building lot)].
        # MUST use the lot's MAX end across its shift-split pieces: a building lot is
        # only a usable green tyre when the WHOLE lot is finished, not when its first
        # shift slice ends. Using per-row ends let curing name a lot that was still
        # being built (2,071 consume-before-make rows in test).
        gt_code, gt_done = {}, {}
        for idx in fs.index[is_bld]:
            lid = str(fs.at[idx, "lot_id"])
            e = pd.to_datetime(fs.at[idx, "end_time"], errors="coerce")
            if pd.isna(e):
                continue
            gt_code[lid] = str(fs.at[idx, "item"]).strip()
            if lid not in gt_done or e.value > gt_done[lid]:
                gt_done[lid] = e.value
        gt_lots = defaultdict(list)
        for lid, fin in gt_done.items():
            gt_lots[gt_code[lid]].append((fin, lid))
        for code in gt_lots:
            gt_lots[code].sort()

        ccons = {}
        for idx in fs.index[is_cur_fix]:
            sku = str(fs.at[idx, "item"]).strip()
            gt = sku2gt.get(sku)
            if not gt:
                # Cured with no green tyre anywhere in the plan -- the pipeline's
                # "CURED WITHOUT A GREEN TYRE" case. Say so; never leave it blank.
                ccons[idx] = "NO_GT_BUILT"
                out["cur_nomap"] += 1
                continue
            T = pd.to_datetime(fs.at[idx, "start_time"], errors="coerce")
            hit = _fefo(gt_lots.get(gt, ()), T.value if pd.notna(T) else 0)
            if hit:
                ccons[idx] = hit
            else:
                ccons[idx] = "WIP:" + gt                    # cured from opening GT inventory
                out["wip_lines"] += 1
        if ccons:
            fs.loc[list(ccons), "consumption_id"] = pd.Series(ccons)
            out["cur_fixed"] = len(ccons)

    fs.to_csv(floor, index=False)

    # keep the PHASEWISE copy in step, if the run has one
    pw = os.path.join(run, "PHASEWISE", "PHASE5_PLACEMENT", "phase5_floor_schedule_updated.csv")
    if os.path.exists(pw):
        fs.to_csv(pw, index=False)
        out["phasewise_synced"] = True

    # push the same values into the published workbook, cell by cell, so the CSV and
    # the XLSX a planner actually opens never disagree. Keyed on lot_id|start|machine
    # (unique across the floor file); every other cell and sheet is left untouched.
    xp = os.path.join(run, "CTP_FLOOR_SCHEDULE.xlsx")
    if os.path.exists(xp):
        try:
            from openpyxl import load_workbook
            key = (fs["lot_id"].astype(str) + "|" + fs["start_time"].astype(str)
                   + "|" + fs["machine"].astype(str))
            want = dict(zip(key, fs["consumption_id"].astype(str)))
            wb = load_workbook(xp)
            n_x = 0
            for sh in wb.sheetnames:
                ws = wb[sh]
                hdr = {str(c.value): c.column for c in ws[1] if c.value is not None}
                if not {"consumption_id", "lot_id", "start_time", "machine"} <= set(hdr):
                    continue
                cC, cL, cS, cM = (hdr["consumption_id"], hdr["lot_id"],
                                  hdr["start_time"], hdr["machine"])
                for rr in range(2, ws.max_row + 1):
                    st = ws.cell(rr, cS).value
                    st = (st.strftime("%Y-%m-%d %H:%M:%S")
                          if hasattr(st, "strftime") else str(st or ""))
                    k = "%s|%s|%s" % (ws.cell(rr, cL).value, st, ws.cell(rr, cM).value)
                    v = want.get(k)
                    if v is not None and v != "nan" and ws.cell(rr, cC).value != v:
                        ws.cell(rr, cC).value = v
                        n_x += 1
            if n_x:
                wb.save(xp)
            out["xlsx_cells"] = n_x
        except Exception as e:
            out["xlsx_error"] = str(e)

    if verbose:
        extra = ""
        if out.get("xlsx_cells"):
            extra += "  xlsx %d cells" % out["xlsx_cells"]
        if out.get("xlsx_error"):
            extra += "  xlsx FAILED: %s" % out["xlsx_error"]
        if out["bld_nomap"] or out["cur_nomap"]:
            extra += "  [nomap bld %d / cur %d]" % (out["bld_nomap"], out["cur_nomap"])
        print("  %-32s building %5d/%-5d   curing %5d/%-5d%s"
              % (out["run"], out["bld_fixed"], out["bld_rows"],
                 out["cur_fixed"], out["cur_rows"], extra))
    return out


def _last_n(n):
    root = os.path.join(HERE, "output")
    runs = [d for d in glob.glob(os.path.join(root, "*"))
            if os.path.isdir(d) and os.path.basename(d) != "latest"
            and os.path.exists(os.path.join(d, "phase5_floor_schedule_updated.csv"))]
    runs.sort(key=os.path.getmtime, reverse=True)
    return runs[:n]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--last":
        targets = _last_n(int(args[1]) if len(args) > 1 else 10)
    elif args:
        targets = [a if os.path.isabs(a) else os.path.join(HERE, a) for a in args]
    else:
        targets = [os.path.join(HERE, "outputs2")]
    print("CTP #44 consumption repair -- %d run(s)" % len(targets))
    tot_b = tot_c = 0
    for t in targets:
        try:
            r = repair(t)
            tot_b += r.get("bld_fixed", 0)
            tot_c += r.get("cur_fixed", 0)
        except Exception as e:
            print("  %-34s FAILED: %s" % (os.path.basename(t.rstrip("/\\")), e))
    print("TOTAL: %d building rows, %d curing rows repaired" % (tot_b, tot_c))
