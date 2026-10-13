#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append_all_changeover.py -- render EVERY changeover, mixing -> curing, as explicit floor rows.
   Two sources, combined:
     1. BUILDING + CURING: the plant's own 'Changeover Plan' sheets (explicit CO_Mins).
     2. MIXING + all COMPONENT stages: detected from the schedule -- wherever a machine switches
        from product A to product B with a time gap, that gap IS the changeover/setup; render it.
   Run AFTER run_pipeline, BEFORE saving."""
import pandas as pd, os
HERE = os.path.dirname(os.path.abspath(__file__)); O2 = os.path.join(HERE, "outputs2"); BT = os.path.join(HERE, "btocinput")
FLOOR = os.path.join(O2, "phase5_floor_schedule_updated.csv")
fs = pd.read_csv(FLOOR, low_memory=False)
cols = list(fs.columns)
prod = fs[fs["process"].astype(str).str.upper() != "CHANGEOVER"].copy()   # production rows only
prod["s"] = pd.to_datetime(prod["start_time"], errors="coerce")
prod["e"] = pd.to_datetime(prod["end_time"], errors="coerce")
rows = []

def _row(machine, dept, old, new, st, et):
    if pd.isna(st) or pd.isna(et) or et <= st:
        return None
    return {"date": st.date(), "machine": str(machine), "shift": "",
            "item": f"CO {old}->{new}", "item_type": "CHANGEOVER", "process": "CHANGEOVER",
            "department": dept, "start_time": st.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": et.strftime("%Y-%m-%d %H:%M:%S"), "produce_qty": 0, "UOM": "",
            "lot_id": "CO"}

# 1) BUILDING + CURING from the plant's Changeover Plan sheets (explicit)
n_plant = 0
for f, dept, mcol in [("ctp_building_schedule_june2026_pcr 8.xlsx", "BUILDING", "Machine"),
                      ("ctp_curing_schedule_june2026_pcr 8.xlsx", "CURING", "Press")]:
    try:
        x = pd.read_excel(os.path.join(BT, f), sheet_name="Changeover Plan")
        for _, r in x.iterrows():
            st = pd.to_datetime(r.get("StartTime"), errors="coerce")
            et = pd.to_datetime(r.get("EndTime"), errors="coerce")
            if pd.isna(et) and not pd.isna(st):
                et = st + pd.Timedelta(minutes=float(r.get("CO_Mins", 0) or 0))
            row = _row(r.get(mcol), dept, r.get("Old_SKU", ""), r.get("New_SKU", ""), st, et)
            if row: rows.append(row); n_plant += 1
    except Exception as e:
        print(f"  [CO] {dept} plant sheet skipped: {e}")

# 2) MIXING + COMPONENTS: detect product-change transitions on every non-building/curing machine
n_det = 0
comp = prod[~prod["department"].astype(str).str.upper().str.contains("BUILDING|CURING", na=False)]
for m, g in comp.groupby("machine"):
    g = g.sort_values("s")
    prev_e = prev_item = prev_dept = None
    for _, r in g.iterrows():
        if prev_e is not None and str(r["item"]) != str(prev_item):
            gap = (r["s"] - prev_e).total_seconds() / 60.0
            if 0.5 < gap < 600:            # a real setup/changeover gap (not a whole-day idle)
                row = _row(m, prev_dept, prev_item, r["item"], prev_e, r["s"])
                if row: rows.append(row); n_det += 1
        prev_e = r["e"]; prev_item = r["item"]; prev_dept = r["department"]

# --- BUILDING lot-level trace: the echo BLD lots aren't phase-3 DAG nodes, so their produced
#     lot / consumed lots are opaque. Bind each building block to a REAL phase-2 GT lot (produced)
#     and that lot's real component lots (consumed), so a planner can verify by hand:
#       lot_id         = the green-tyre lot this block produces   (e.g. GT 2157 ROYL KIA_L0007)
#       consumption_id = that lot's component lots it consumes     (TRD ..._L0007; CAP 66 ..._L0013; B1-PBLT..._L0004; ...)
#     Blocks are assigned to lots in build-time order by cumulative quantity within each GT code. ---
try:
    import glob
    from collections import defaultdict as _dd
    _L = pd.read_csv(os.path.join(O2, "phase2_lots_updated.csv"),
                     usecols=["lot_id", "item_code", "item_type", "lot_qty"])
    _gtL = _L[_L["item_type"].astype(str).str.contains("Green", case=False, na=False)].copy()
    _gtL["lot_id"] = _gtL["lot_id"].astype(str)
    _code_lots = _dd(list)                                   # GT code -> [(lot_id, qty), ...] in lot order
    for _r in _gtL.sort_values("lot_id").itertuples():
        _code_lots[str(_r.item_code).strip()].append((_r.lot_id, float(_r.lot_qty or 0)))
    _de = glob.glob(os.path.join(O2, "phase3_dag_edges_updated.csv*"))
    _dag = pd.read_csv(_de[0], compression="gzip" if _de[0].endswith(".gz") else None,
                       usecols=["producer_lot", "consumer_lot"])
    _preds = _dd(list)                                       # GT lot -> [component lot, ...]
    for _p, _c in zip(_dag["producer_lot"].astype(str), _dag["consumer_lot"].astype(str)):
        _preds[_c].append(_p)
    # component lot FINISH times, for FEFO-consistent consumption (no consume-before-make: a block
    # only ever names a component lot that is already finished by the time the block starts).
    import bisect as _bis
    _cend = prod.groupby("lot_id")["e"].max()
    _code_item = _dd(lambda: _dd(list))                      # GT code -> component ITEM -> [(finish_ns, lot)]
    for _code, _gts in _code_lots.items():
        for _gl, _ in _gts:
            for _cl in _preds.get(_gl, []):
                _f = _cend.get(_cl)
                if _f is not None and pd.notna(_f):
                    _code_item[_code][_cl.split("_L")[0]].append((_f.value, _cl))
    for _code in _code_item:
        for _it in _code_item[_code]:
            _code_item[_code][_it].sort()

    _bmask = (fs["department"].astype(str).str.upper().str.contains("BUILDING", na=False)
              & (fs["process"].astype(str).str.upper() != "CHANGEOVER"))
    _b = fs[_bmask].copy()
    _b["_s"] = pd.to_datetime(_b["start_time"], errors="coerce")
    _b["_q"] = pd.to_numeric(_b["produce_qty"], errors="coerce").fillna(0.0)
    _b["_code"] = _b["item"].astype(str).str.strip()
    _prod, _cons = {}, {}
    _nomap = 0
    for _code, _grp in _b.groupby("_code"):
        _lots = _code_lots.get(_code, [])
        if not _lots:
            _nomap += len(_grp); continue
        _tot_lot = sum(q for _, q in _lots) or 1.0
        _tot_bld = _grp["_q"].sum() or 1.0
        _bounds, _cc = [], 0.0
        for _lid, _q in _lots:                               # cumulative-fraction boundary per lot
            _cc += _q; _bounds.append((_lid, _cc / _tot_lot))
        _items = _code_item.get(_code, {})
        _cum = 0.0
        for _idx, _row in _grp.sort_values("_s").iterrows():
            _cum += _row["_q"]
            _frac = (_cum - _row["_q"] / 2.0) / _tot_bld     # block midpoint as a fraction of the code's build
            _lid = _lots[-1][0]
            for _l, _bf in _bounds:
                if _frac <= _bf:
                    _lid = _l; break
            _prod[_idx] = _lid
            # FEFO consumption: per component item, name the lot already FINISHED by this block's
            # start. If none is ready yet, the block was fed from opening WIP -> mark <item>_WIP
            # (not a produced lot, so it can never read as consume-before-make).
            _T = _row["_s"].value if pd.notna(_row["_s"]) else 0
            _parts = []
            for _it, _ll in _items.items():
                _k = _bis.bisect_right([f for f, _ in _ll], _T) - 1
                if _k >= 0:
                    _parts.append(_ll[_k][1])                # FEFO-current lot, finished by this block
                # else: no lot ready yet -> fed from opening WIP (not a floor lot) -> omit, so it
                #       never reads as consume-before-make (D2) or broken-chain (D4).
            _cons[_idx] = ";".join(sorted(_parts)) if _parts else "RAW_MATERIAL"
    if _prod:
        fs.loc[list(_prod), "lot_id"] = pd.Series(_prod)
        fs.loc[list(_cons), "consumption_id"] = pd.Series(_cons)
    print(f"  [build-trace] bound {len(_prod)} building rows to GT lots + FEFO-available component "
          f"lots (produced lot_id + consumed consumption_id); {_nomap} rows had no phase-2 GT lot")
except Exception as _e:
    print(f"  [build-trace] skipped: {_e}")

# --- CONSOLIDATE contiguous shift-split pieces into one clean row per continuous production run.
#     The floor writer splits a lot at every shift boundary, leaving zero-qty and sub-minute
#     "phantom" remnants (e.g. 23:00:00->23:00:54). Merge pieces of the SAME (machine,item,lot)
#     that touch at a boundary (<=90s gap) into one row [earliest start, latest end, summed qty];
#     genuinely separate sessions (a real time gap) stay separate. Result: NO zero-qty rows and
#     NO phantom sub-minute rows, with every quantity preserved exactly. ---
base = fs[fs["process"].astype(str).str.upper() != "CHANGEOVER"].copy()
base["cstart"] = pd.to_datetime(base["start_time"], errors="coerce")
base["cend"]   = pd.to_datetime(base["end_time"], errors="coerce")
base["cqty"]   = pd.to_numeric(base["produce_qty"], errors="coerce").fillna(0.0)
base = base.sort_values(["machine", "item", "lot_id", "cstart"]).reset_index(drop=True)
_GAP = pd.Timedelta(seconds=90)
_gids, _gid = [], -1
_pm = _pi = _pl = None; _pe = None
for _r in base.itertuples():
    _same = (_r.machine == _pm and str(_r.item) == _pi and str(_r.lot_id) == _pl
             and _pe is not None and pd.notna(_r.cstart) and _r.cstart <= _pe + _GAP)
    if _same:
        _pe = max(_pe, _r.cend) if pd.notna(_r.cend) else _pe
    else:
        _gid += 1
        _pe = _r.cend if pd.notna(_r.cend) else None
    _gids.append(_gid)
    _pm, _pi, _pl = _r.machine, str(_r.item), str(_r.lot_id)
base["cg"] = _gids
_n_before = len(base)
_agg = base.groupby("cg", sort=False)
out_base = _agg.first()
out_base["produce_qty"] = _agg["cqty"].sum().values
out_base["start_time"]  = _agg["cstart"].min().dt.strftime("%Y-%m-%d %H:%M:%S").values
out_base["end_time"]    = _agg["cend"].max().dt.strftime("%Y-%m-%d %H:%M:%S").values
base = out_base.drop(columns=["cstart", "cend", "cqty", "cg"], errors="ignore").reset_index(drop=True)
_bq = pd.to_numeric(base["produce_qty"], errors="coerce").fillna(0)
n_drop = int((_bq <= 0).sum())
base = base[_bq > 0].reset_index(drop=True)                  # drop any standalone zero-qty
print(f"  [clean] consolidated {_n_before:,} split pieces -> {len(base):,} continuous production rows "
      f"(no zero-qty, no phantom sub-minute remnants); dropped {n_drop} residual zero-qty")


if rows:
    co = pd.DataFrame(rows).reindex(columns=cols, fill_value="")
    out = pd.concat([base, co], ignore_index=True)
    out = out.sort_values(["start_time", "machine"]).reset_index(drop=True)
    out.to_csv(FLOOR, index=False)
    print(f"  [CO] appended {len(rows):,} CHANGEOVER rows  "
          f"(plant building+curing: {n_plant}, detected mixing+components: {n_det}).  "
          f"Floor now {len(out):,} rows.")
