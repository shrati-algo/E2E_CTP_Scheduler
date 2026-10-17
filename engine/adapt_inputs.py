"""
adapt_inputs.py — the ONLY file in this project that knows CTP's data is different.

The v6 phase files in `phases/` are BYTE-IDENTICAL copies of
`v6_wave_p2/phases/*.py`. Not one line of the algorithm is changed. To keep it that
way, we convert CTP's inputs into exactly the CSVs v6 expects, with exactly v6's
column names, and drop them in `inputs/`. v6 then runs unmodified.

Every conversion below is a DATA reshape, never a rule change. Each one is listed in
MEMORY.md with the reason. Nothing here is guessed: every mapping was verified against
the real files.

    CTP file                              ->  v6 file (what the phases open)
    -----------------------------------------------------------------------
    jkt_bom_pcr 13 (1).xlsx               ->  bom.csv                (columns already match)
    jkt_routing_pcr 14.xlsx               ->  routing.csv            (+ MIN/BATCH -> SEC/BATCH)
    CTP_PCR_Curing_Schedule ... .xlsx     ->  plan.csv               (Shift Schedule -> plan cols)
    jkt_itemType_master_pcr.csv           ->  itemtype_master.csv    (columns already match)
    jkt_aging_master_pcr.csv              ->  aging_master.csv       (columns already match)
    MOQ (3).xlsx                          ->  mpq.csv                (v6's 5-column schema)
    jkt_buffer_master_pcr.csv             ->  buffer_master.csv
    opening_wip.csv                       ->  inventory.csv          (v6 inventory schema)
    jkt_changeover_matrix_combined.xlsx   ->  changeover_matrix.csv  (CTP-ONLY — see MEMORY.md)

  NOT WRITTEN ANY MORE:
    planning_max_aging.csv  -- DELETED 2026-07-14. I INVENTED it; the plant never supplied
    one. It was config laundered into a CSV to look like a plant master, and phase4 read it
    as authority. Provable zero schedule delta. See the DELETED block below, and config.yaml.

Usage:  python adapt_inputs.py
"""
from __future__ import annotations
import os
import re
import sys
import math
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "inputs")
CFG = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
SRC = os.path.join(HERE, CFG["ctp_inputs_dir"])


def _p(name: str) -> str:
    return os.path.join(SRC, CFG["ctp_files"][name])


# ── DB SOURCING (2026-07-23) ─────────────────────────────────────────────────────
# `data_source: db` makes adapt_inputs read jkplanning_CTP instead of ctp_inputfiles/.
#
# THE MISTAKE THIS DESIGN AVOIDS: a first cut piped the DB tables STRAIGHT into inputs/,
# bypassing this module. It looked fine and the pipeline died in preflight -- the DB's
# jkt_itemType_master_pcr has 2,862 rows and ZERO 'Green Tyres', while itemtype() below
# GAP-FILLS it to 3,100 incl. 225 green tyres from the BOM. adapt_inputs does REAL work
# (gap fills, itemtype overrides, UOM conversion, MPQ parsing) -- not column renaming --
# so the database must flow THROUGH it, never around it.
USE_DB = str(CFG.get("data_source", "csv")).strip().lower() in ("db", "mysql", "database")


# PRODUCT: 'pcr' (default) or 'tbr'. Swaps every `_pcr` DB table to `_<product>` so the same
# adapter builds a TBR inputs/ set from the TBR tables. Set via env CTP_PRODUCT=tbr.
PRODUCT = os.environ.get("CTP_PRODUCT", "pcr").strip().lower()


def _db_table(name: str):
    """Raw DB table for a ctp_files key, or None if this key has no DB source."""
    import sys
    dbp = os.path.join(HERE, "db_pipeline")
    if dbp not in sys.path:
        sys.path.insert(0, dbp)
    import db_connect, table_map                     # noqa: E401
    spec = table_map.ALL.get(name)
    if spec is None:
        return None
    return db_connect.read_table(table_map.for_product(spec["table"], PRODUCT))


def _read(name: str) -> pd.DataFrame:
    """Read a CTP master. DB when data_source: db, else the workbook in ctp_inputfiles/.

    Returns dtype=str + blanks (never NaN) in BOTH modes, because every adapter below was
    written against that contract -- MEMORY: 'present-but-blank' NaN trap.
    """
    if USE_DB:
        try:
            df = _db_table(name)
            if df is not None:
                return df.astype(str).replace({"None": "", "nan": "", "NaT": ""}).fillna("")
            print(f"  [adapt] {name}: no DB table mapped — using the workbook.")
        except Exception as e:
            print(f"  [adapt] DB read failed for {name!r} ({type(e).__name__}: {str(e)[:100]}) "
                  f"— FALLING BACK to the workbook. This is NOT a database run.")
    p = _p(name)
    if p.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(p, sheet_name=0, dtype=str, engine="openpyxl").fillna("")
    return pd.read_csv(p, dtype=str, keep_default_na=False)


def _w(df: pd.DataFrame, name: str) -> None:
    path = os.path.join(OUT, name)
    df.to_csv(path, index=False)
    print(f"  {name:<26} {len(df):>7,} rows")


# --------------------------------------------------------------------------- BOM
def bom() -> None:
    """CTP's BOM already carries v6's column names. Straight through.

    *** CTP WORKS IN METRES. WE DO NOT CONVERT LENGTH TO MM. ***  (user instruction)

    v6/BTP denominates length in MM (its BOM is `MM`, its routing `MM/MIN`). CTP's masters
    are in `M` and `M/MIN`, and they stay that way. This is SELF-CONSISTENT and correct,
    because v6's arithmetic is unit-agnostic wherever the units agree:

        op_duration_min:  `if u in ("MM/MIN","M/MIN","MTR/MIN"): return (qty/pt)/eff`
        -> M quantity / (M/MIN) rate = minutes.  Correct.  No conversion needed.

    THE ONE PLACE THAT WAS NOT UNIT-AGNOSTIC was v6's `db_loader.load_wip_by_item`, which
    HARD-CODED the MM assumption and multiplied M-denominated inventory by 1000:

        _LEN_TO_MM = {"M": 1000.0, ...};  qty = qty * factor

    Against CTP's M-denominated demand that inflated the WIP 1000x — the plant had 1,621
    metres of stock and the scheduler believed it had 1,621 KILOMETRES, netted the demand
    away, and never produced it. Silently.

    `db_loader.py` has been REPLACED with `ctp_loader.py` — file-based, no DB, and NO length
    conversion. See that file. The BOM below is passed through untouched.
    """
    df = _read("bom")
    need = ["Super_parent", "Equipment", "grand_parent", "Parent", "Parent_qty",
            "Parent_unit", "child", "child_quantity", "child_Unit"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"BOM is missing v6 columns: {missing}")
    _w(df, "bom.csv")


# ----------------------------------------------------------------------- ROUTING
def routing() -> None:
    """CTP's routing already carries v6's column names, with ONE data difference.

    v6's `op_duration_min` (phase2:196-213) handles these proc_time_UOMs:
        MM/MIN  M/MIN  MTR/MIN  NOS/MIN  SEC/BATCH  SEC  MIN  *anything*/MIN
    It has NO `MIN/BATCH` branch, and an unmatched UOM falls through to `qty * pt`
    — which for a 690 KG batch at 15 min/batch yields 10,350 min instead of 15.6.

    CTP's routing is 72% `MIN/BATCH` (10,599 of 14,693 rows); v6's TBR routing uses
    `SEC/BATCH`. They mean the same thing in different units, so we convert the UNIT,
    not the rule:  MIN/BATCH @ pt  ==  SEC/BATCH @ pt*60.
    v6's SEC/BATCH branch then computes  ceil(qty/batch_size) * (pt*60) / 60  minutes
    = exactly the intended minutes-per-batch. The algorithm is untouched.
    """
    df = _read("routing")
    # the sheet loads as all-strings; rebuild the two columns as plain objects so the
    # converted numbers can be written back without a dtype clash
    df["proc_time"] = df["proc_time"].astype(object)
    df["proc_time_UOM"] = df["proc_time_UOM"].astype(object)
    u = df["proc_time_UOM"].astype(str).str.strip().str.upper()
    m = (u == "MIN/BATCH").to_numpy()
    n = int(m.sum())
    if n:
        pt = pd.to_numeric(df["proc_time"], errors="coerce") * 60.0
        df.loc[m, "proc_time"] = pt[m].to_numpy(dtype=object)
        df.loc[m, "proc_time_UOM"] = "SEC/BATCH"
        print(f"  [routing] MIN/BATCH -> SEC/BATCH (x60) on {n:,} rows "
              f"— v6 has no MIN/BATCH branch; same rule, converted unit")

    # LENGTH STAYS IN M/MIN. v6's op_duration_min treats M/MIN and MM/MIN identically
    # (`return (qty/pt)/eff`), so an M quantity divided by an M/MIN rate gives the correct
    # minutes. The BOM is in M, the routing is in M/MIN — self-consistent. Do NOT convert.
    #
    # USER INSTRUCTION: "use the same units as given in the data, do not consider anything".
    # Every UOM is passed through EXACTLY as the master states it. The ONLY unit conversion
    # in this whole adapter is MIN/BATCH -> SEC/BATCH above, and that exists solely because
    # v6 has no MIN/BATCH branch at all — it is the same rule in a unit v6 can read.

    # ═══ ROUTING RATE OVERRIDES — see config.yaml -> routing_overrides ════════════════
    # PLANT-CONFIRMED corrections where the routing's rate is stated on the machine's INPUT
    # (the web) while the demand is raised on its OUTPUT (the slit strip). Applied by
    # OPERATION NAME, and every change is printed with the old value and the new one.
    ro = CFG.get("routing_overrides") or {}
    if ro:
        op = df["operation_name"].astype(str).str.strip().str.upper()
        for name, fields in ro.items():
            hit = op == str(name).strip().upper()
            if not hit.any():
                print(f"  [routing] NOTE: routing_overrides names operation {name!r}, which is "
                      f"not in the routing. Dead entry — remove it from config.")
                continue
            for col, val in fields.items():
                if col not in df.columns:
                    sys.exit(f"routing_overrides[{name!r}] names column {col!r}, which the "
                             f"routing does not have. Columns: {list(df.columns)}")
                old = sorted({str(x) for x in df.loc[hit, col].dropna()})[:3]
                df[col] = df[col].astype(object)
                df.loc[hit, col] = val
                print(f"  [routing] PLANT-CONFIRMED OVERRIDE  {name}.{col}: {old} -> {val}   "
                      f"({int(hit.sum())} rows)")

    # ═══ BR-1: EFFECTIVE LINE SPEED = single-strand rate × PARALLEL rolls ═══════════════
    # (2026-08-03 user BUSINESS RULE -> BUSINESS_RULES.md). These calender products run N rolls
    # in PARALLEL, so real throughput = N × the per-strand routing rate. proc_time_UOM stays a
    # RATE (M/MIN); multiply the rate by the roll count for each listed routed_product.
    # TEG01/TEG02 are TBR (excluded from PCR); cap-strip slitter already handled by routing_overrides.
    lr = CFG.get("linespeed_rolls") or {}
    # 4RC rule 4 (2026-10-06, config fourrc_rules.routing_speed_only): on the four-roll calendar the
    # run time uses the routing proc_time of the routed_product ONLY -- no roll multiplier there.
    _fr = CFG.get("fourrc_rules") or {}
    _on901 = pd.Series(False, index=df.index)
    if _fr.get("routing_speed_only", False):
        _m = str(_fr.get("machine", "901"))
        _on901 = df["machines"].astype(str).map(
            lambda s: _m in {x.strip().strip('"').strip("'") for x in re.split(r"[,;/|]", s)})
        _blank = _on901 & pd.to_numeric(df["proc_time"], errors="coerce").isna()
        print(f"  [4RC rule 4] machine {_m}: line speed = routing proc_time per routed_product "
              f"({df.loc[_on901, 'routed_product'].nunique()} items); NO LINE SPEED IN ROUTING: "
              f"{sorted(set(df.loc[_blank, 'routed_product'].astype(str).str.strip()))}")
    if lr:
        rpc = df["routed_product"].astype(str).str.strip()
        uom = df["proc_time_UOM"].astype(str).str.strip().str.upper()
        df["proc_time"] = df["proc_time"].astype(object)
        for code, rolls in lr.items():
            hit = (rpc == str(code).strip()) & uom.isin(["M/MIN", "MM/MIN", "MTR/MIN"]) & ~_on901
            if _fr.get("routing_speed_only", False) and ((rpc == str(code).strip()) & _on901).any():
                print(f"  [4RC rule 4] linespeed_rolls x{rolls} NOT applied to {code} on machine "
                      f"{_fr.get('machine', '901')} (routing proc_time used)")
            if not hit.any():
                print(f"  [routing] BR-1 NOTE: linespeed_rolls names {code!r}, not a M/MIN "
                      f"routed_product here. Skipped.")
                continue
            old = pd.to_numeric(df.loc[hit, "proc_time"], errors="coerce")
            df.loc[hit, "proc_time"] = (old * float(rolls)).to_numpy(dtype=object)
            print(f"  [routing] BR-1 line-speed ×{rolls}  {code}: "
                  f"{sorted({round(float(x),1) for x in old.dropna()})} -> "
                  f"{sorted({round(float(x)*float(rolls),1) for x in old.dropna()})} M/MIN "
                  f"({int(hit.sum())} rows)")

    # ═══ EXCLUDED ITEMS — see config.yaml -> excluded_items ════════════════════════════
    # No proc_time, no proc_time_UOM, mostly no machine. They cannot be scheduled and NO
    # VALUE HAS BEEN INVENTED. Dropping the routing row makes phase1b's `if ch not in routed:
    # continue` skip them cleanly — and plan() has ALREADY removed the SKUs that depend on
    # them, so the schedule stays honest instead of silently short.
    ex = [str(x).strip() for x in (CFG.get("excluded_items") or [])]
    if ex:
        rp = df["routed_product"].astype(str).str.strip()
        hit = rp.isin(ex)
        if hit.any():
            print(f"  [routing] EXCLUDED {len(set(rp[hit]))} routed_product(s) with NO "
                  f"proc_time — {int(hit.sum())} rows dropped: {sorted(set(rp[hit]))}")
            df = df[~hit]
        gone = [x for x in ex if x not in set(rp)]
        if gone:
            print(f"  [routing] NOTE: config.excluded_items lists {gone}, which are not in the "
                  f"routing at all. Remove them from the config — they are dead entries.")

    # ── CANONICAL MACHINE IDS (2026-07-31) ────────────────────────────────────────────
    # PCR and TBR label the 6 shared Banbury mixers differently (PCR '270M' == TBR 'K270M',
    # '310'==K310, '440M'==F440, '430-1/2'==K430-1/2). Normalize the machines column so BOTH
    # products name the same physical machine identically -- required for a combined schedule
    # not to double-book them. No-op for TBR (already canonical); rewrites PCR's mixer ids.
    try:
        import re as _re
        import machine_normalize as _mn
        if "machines" in df.columns:
            def _canon_cell(v):
                s = str(v)
                for _old, _new in _mn.MACHINE_ALIASES.items():
                    s = _re.sub(rf'(?<![\w-]){_re.escape(_old)}(?![\w-])', _new, s)
                return s
            _before = df["machines"].astype(str)
            df["machines"] = _before.map(_canon_cell)
            _n = int((df["machines"] != _before).sum())
            if _n:
                print(f"  [routing] canonicalised machine ids on {_n} rows "
                      f"(shared-mixer aliases -> {sorted(set(_mn.MACHINE_ALIASES.values()))})")
    except Exception as _e:
        print(f"  [routing] machine-id canonicalisation skipped: {_e}")

    _w(df, "routing.csv")


# -------------------------------------------------------------------------- PLAN
def plan() -> None:
    """The curing drum -> v6's plan schema.

    v6 (`phase1_5.parse_plan_blocks`, `phase0.parse_plan_to_curing`) reads:
        skuCode, qty, startTime, endTime, pressNo, shift, date
    and drops rows whose skuCode is CHANGEOVER / MOULD_CLEANING, and qty <= 0.

    The CTP export puts a title banner + a summary line above the header, so the real
    header is not on row 0. Find it instead of hard-coding a skiprows.
    """
    p = _p("plan")
    if PRODUCT == "tbr":
        p = p.replace("_pcr", "_tbr")                # ../btocinput/..._tbr 8.xlsx (curing echo)
    # DB MODE: the plan is OPT-IN (`db_plan: true`), NOT part of data_source: db.
    # CuringSchedule_pcr covers 2026-04-01..04-29 while the shipped run uses a JUNE shift
    # sheet, so silently switching would reschedule a DIFFERENT MONTH. It also has no
    # CycleTime_min (adapt_plan derives runTime/qty; the design cure-time table is NOT the
    # same number -- measured 24/27/15 in the sheet vs 19/22/14 by design).
    if USE_DB and bool(CFG.get("db_plan", False)):
        try:
            import sys as _s
            _dbp = os.path.join(HERE, "db_pipeline")
            if _dbp not in _s.path:
                _s.path.insert(0, _dbp)
            import adapters as _ad, db_connect as _dc      # noqa: E401
            _raw = _db_table("plan")
            _cyc = None
            try:
                _cyc = _dc.read_table("Master_Curing_Design_CycleTime_tbr" if PRODUCT == "tbr"
                                      else "Master_Curing_Design_CycleTime_pcr")
            except Exception:
                pass
            if _raw is not None:
                _out = _ad.adapt_plan(_raw, cycle_df=_cyc)
                print(f"  [plan] FROM DB CuringSchedule_pcr — horizon "
                      f"{_out['date'].min()} .. {_out['date'].max()} ({len(_out):,} rows). "
                      f"Confirm this is the month you intend to schedule.")
                _w(_out, "plan.csv")
                return
        except Exception as _e:
            print(f"  [adapt] plan: DB read failed ({_e!r}) — falling back to the shift sheet.")
    probe = pd.read_excel(p, sheet_name="Shift Schedule", header=None, nrows=10,
                          engine="openpyxl")
    hdr = 0
    for i in range(len(probe)):
        cells = {str(v).strip() for v in probe.iloc[i].tolist()}
        # old shift-sheet header OR the btoc July curing header (Press/SKU/Cured_Qty)
        if ({"SKUCode", "StartTime", "Machine"} <= cells) or ({"Press", "SKU", "Cured_Qty"} <= cells):
            hdr = i
            break
    raw = pd.read_excel(p, sheet_name="Shift Schedule", header=hdr, engine="openpyxl")
    raw.columns = [str(c).strip() for c in raw.columns]

    if {"Press", "Cured_Qty"} <= set(raw.columns):
        # ── BtoC JULY CURING format (Press, SKU, Status, Cured_Qty, Used_Mins, GT_Code, ...) ──
        raw = raw[raw["Status"].astype(str).str.strip().str.upper() == "RUNNING"].copy()
        _st = pd.to_datetime(raw["StartTime"], errors="coerce")
        _q  = pd.to_numeric(raw["Cured_Qty"], errors="coerce").fillna(0)
        _um = pd.to_numeric(raw.get("Used_Mins"), errors="coerce")
        out = pd.DataFrame({
            "plan_id":   "CTP_PCR",
            "skuCode":   raw["SKU"].astype(str).str.strip(),
            "date":      _st.dt.normalize(),
            "pressNo":   raw["Press"].astype(str).str.strip(),
            "shift":     raw["Shift"].astype(str).str.strip(),
            "startTime": _st,
            "endTime":   pd.to_datetime(raw["EndTime"], errors="coerce"),
            "qty":       _q,
            "cycleTime": (_um / _q.where(_q > 0)).round(2),
            "remarks":   raw.get("GT_Code", "").astype(str).str.strip(),
        })
        print(f"  [plan] btoc CURING schedule — {len(out):,} RUNNING blocks, "
              f"{out['startTime'].min()} .. {out['startTime'].max()}, {int(out['qty'].sum()):,} cured")
    else:
        out = pd.DataFrame({
            "plan_id":   "CTP_PCR",
            "skuCode":   raw["SKUCode"].astype(str).str.strip(),
            "date":      pd.to_datetime(raw["Date"], errors="coerce"),
            "pressNo":   raw["Machine"].astype(str).str.strip(),
            "shift":     raw["Shift"].astype(str).str.strip(),
            "startTime": pd.to_datetime(raw["StartTime"], errors="coerce"),
            "endTime":   pd.to_datetime(raw["EndTime"], errors="coerce"),
            "qty":       pd.to_numeric(raw["Qty"], errors="coerce").fillna(0),
            "cycleTime": pd.to_numeric(raw.get("CycleTime_min"), errors="coerce"),
            "remarks":   raw.get("Remarks", "").astype(str).str.strip(),
        })
    # v6 filters CHANGEOVER itself (phase1_5:64-69). Keep every row so its own
    # occupancy handling sees them — do not pre-filter here.

    # ═══ EXCLUDED ITEMS — THE SKU STAYS. ONLY THE BROKEN ITEM IS SKIPPED. ══════════════
    # USER DECISION: "skip the ITEM only, not the full SKU demand."
    #
    # We do NOT drop the SKU from the plan. Its OTHER components — tread, belt, ply, bead,
    # cap strip, compound — are perfectly good and WILL be scheduled. Only the one broken
    # routed_product is dropped (in routing(), above), so phase1b's
    # `if ch not in routed: continue` skips it cleanly.
    #
    # !! THE HOLE THIS LEAVES, STATED PLAINLY:
    #    The curing plan still cures these tyres, and every other part gets made — but the
    #    GREEN TYRE itself is NEVER BUILT, because no cycle time exists for it. The plant
    #    would make all the parts and never assemble them.
    #
    #    That is a real hole. It is ACCEPTABLE only because it is DECLARED. It is printed
    #    here on every run, and again in the schedule report. A KNOWN hole you can act on
    #    beats a SILENT one you cannot.
    ex = [str(x).strip() for x in (CFG.get("excluded_items") or [])]
    if ex:
        bom_df = pd.read_csv(os.path.join(OUT, "bom.csv"), dtype=str, keep_default_na=False)
        dep = bom_df[bom_df["child"].astype(str).str.strip().isin(ex)]
        sku = out["skuCode"].astype(str).str.strip()
        rows = []
        for s in sorted({str(x).strip() for x in dep["Super_parent"]}):
            q = pd.to_numeric(out.loc[sku == s, "qty"], errors="coerce").fillna(0).sum()
            if q <= 0:
                continue
            items = sorted({c for c in ex
                            if c in set(dep[dep["Super_parent"].astype(str).str.strip() == s]
                                        ["child"].astype(str).str.strip())})
            n_bom = int((bom_df["Super_parent"].astype(str).str.strip() == s).sum())
            rows.append((s, int(q), items, n_bom))
        if rows:
            tot = pd.to_numeric(out["qty"], errors="coerce").fillna(0).sum()
            aff = sum(r[1] for r in rows)
            print(f"  [plan] !! {len(rows)} SKU(s) / {aff:,} tyres ({100*aff/tot:.1f}% of the "
                  f"plan) have ONE component that CANNOT be scheduled.")
            print(f"         THE SKU STAYS IN THE PLAN. Only the broken ITEM is skipped, so "
                  f"every OTHER component is still made:")
            for s, q, items, n_bom in rows:
                print(f"           {s}  {q:>7,} tyres   {n_bom-len(items)} of {n_bom} BOM rows "
                      f"OK   |   BROKEN: {items}")
            print(f"         !! CONSEQUENCE: the plan cures these tyres and makes every part, "
                  f"but the GREEN TYRE is never built (no cycle time in the routing).")
            print(f"            The plant would make all the parts and never assemble them.")
            print(f"            Nothing was invented. Fill proc_time/machines in the routing "
                  f"and delete the entry in config.excluded_items to close this.")

    _w(out, "plan.csv")


# ---------------------------------------------------------------- BUILDING PLAN
def building_plan() -> None:
    """The plant's BUILDING schedule (btoc) -> inputs/building_plan.csv.

    Mirrors the curing plan schema + `buildMachine` + `gtCode`. Only produced when config has
    `ctp_files.building_plan`; consumed by phase5's building echo when `building_plan_echo` is on.
    Local xlsx, sheet 'Shift Schedule (per-shift)': Machine, Day, Shift, SKU, Qty, Mins, Phase,
    StartTime, EndTime, GT_Code, SKU_Description. cycleTime = Mins/Qty (per-tyre build minutes)."""
    key = CFG.get("ctp_files", {}).get("building_plan")
    if not key:
        return
    if PRODUCT == "tbr":
        key = key.replace("_pcr", "_tbr")            # ../btocinput/..._tbr 8.xlsx
    p = os.path.join(SRC, key)
    if not os.path.exists(p):
        print(f"  [building_plan] not found: {p} -- skipped")
        return
    xl = pd.ExcelFile(p, engine="openpyxl")
    sheet = "Shift Schedule (per-shift)" if "Shift Schedule (per-shift)" in xl.sheet_names \
            else "Shift Schedule"
    probe = pd.read_excel(p, sheet_name=sheet, header=None, nrows=10, engine="openpyxl")
    hdr = 0
    for i in range(len(probe)):
        cells = {str(v).strip() for v in probe.iloc[i].tolist()}
        if {"Machine", "SKU", "Qty"} <= cells:
            hdr = i
            break
    raw = pd.read_excel(p, sheet_name=sheet, header=hdr, engine="openpyxl")
    raw.columns = [str(c).strip() for c in raw.columns]
    _st = pd.to_datetime(raw["StartTime"], errors="coerce")
    _q  = pd.to_numeric(raw["Qty"], errors="coerce").fillna(0)
    _m  = pd.to_numeric(raw.get("Mins"), errors="coerce")
    out = pd.DataFrame({
        "plan_id":      "CTP_PCR",
        "skuCode":      raw["SKU"].astype(str).str.strip(),
        "gtCode":       raw.get("GT_Code", "").astype(str).str.strip(),
        "date":         _st.dt.normalize(),
        "buildMachine": raw["Machine"].astype(str).str.strip(),
        "shift":        raw.get("Shift", "").astype(str).str.strip(),
        "startTime":    _st,
        "endTime":      pd.to_datetime(raw["EndTime"], errors="coerce"),
        "qty":          _q,
        "cycleTime":    (_m / _q.where(_q > 0)).round(3),
        "remarks":      raw.get("Phase", "").astype(str).str.strip(),
    })
    out = out[out["qty"] > 0].reset_index(drop=True)
    _w(out, "building_plan.csv")
    print(f"  [building_plan] {len(out):,} rows, {out['buildMachine'].nunique()} TBMs, "
          f"{out['gtCode'].nunique()} GTs, {int(out['qty'].sum()):,} tyres, "
          f"{out['startTime'].min()} .. {out['startTime'].max()}")


# --------------------------------------------------------------------- ITEM TYPE
def itemtype() -> None:
    """CTP's itemtype master + the ONE gap it has: it does not type the green tyre.

    v6 keys three critical behaviours on the RAW ItemType string:
        BUILDING_ITYPES  = {"GREEN TYRES", "CARCASS"}          phase5_v2:708
        AGE_OVERRIDE_H   = {"GREEN TYRES": (0.0, 72.0), ...}   phase5_v2:869
        PLANT_ITEM_TYPE_CAPS / TARGET_WIP_HOURS                phase2 / phase5
    and it builds its map from master rows ONLY (`build_itype_map`, phase2:126-128) —
    it never canonicalises, never guesses from a code prefix.

    CTP's master types 2,862 codes but **not one green tyre** (verified: 0 of the 226
    GT codes are present). Left alone, `lot_item_type` is "" for every green tyre, so:
      - it is NOT in BUILDING_ITYPES  -> it skips `place_building_campaigns` and falls
        into the ordinary JIT picker (a different algorithm),
      - AGE_OVERRIDE_H never fires    -> its aging silently becomes
        (DEFAULT_MIN_AGE_H, DEFAULT_MAX_AGE_H) = (8 h, 8760 h) instead of (0, 72),
      - the short-life resync becomes a no-op.
    The plan would still "succeed" and be nonsense.

    The green tyre is identified DETERMINISTICALLY from the routing, not guessed:
    it is a `routed_product` whose `department` is BUILDING (226 codes; they are the
    same GT-prefixed codes the BOM uses as `Parent`). We type them `Green Tyres` —
    v6's exact string. This is a DATA GAP FILL, not a rule change.
    """
    df = _read("itemtype")
    df = df.rename(columns={c: c.strip() for c in df.columns})
    df = df[["ItemCode", "ItemType"]].copy()

    rt = pd.read_csv(os.path.join(OUT, "routing.csv"), dtype=str, keep_default_na=False)
    building = sorted({str(r).strip() for r, d in
                       zip(rt["routed_product"], rt["department"])
                       if "BUILD" in str(d).upper() and str(r).strip()})
    have = set(df["ItemCode"].astype(str).str.strip())
    add = [c for c in building if c not in have]
    if add:
        df = pd.concat([df, pd.DataFrame({"ItemCode": add, "ItemType": "Green Tyres"})],
                       ignore_index=True)
        print(f"  [itemtype] typed {len(add)} green tyres as 'Green Tyres' "
              f"(routing dept=BUILDING; CTP's master does not type them)")

    # ---- USER-CONFIRMED overrides from config.yaml -> itemtype_overrides -------------
    # These are the USER'S OWN decisions and they BEAT THE MASTER — because the master is
    # KNOWN to be wrong on at least one of them: it types `CAP30` as CALANDARED ROLL, and the
    # user's verdict is "in SMART mentioned as calandered roll which is NOT correct" -> Cap Strip.
    #
    # Overriding a master is a serious thing, so it is NEVER silent: every override that
    # actually contradicts the master is printed with the old value and the new one.
    ov = CFG.get("itemtype_overrides") or {}
    if ov:
        added, changed = [], []
        for c, t in ov.items():
            c, t = str(c).strip(), str(t).strip()
            hit = df["ItemCode"].astype(str).str.strip() == c
            if hit.any():
                old = str(df.loc[hit, "ItemType"].iloc[0]).strip()
                if old != t:
                    df.loc[hit, "ItemType"] = t
                    changed.append((c, old, t))
            else:
                added.append((c, t))
        if added:
            df = pd.concat([df, pd.DataFrame([{"ItemCode": c, "ItemType": t}
                                              for c, t in added])], ignore_index=True)
            for c, t in added:
                print(f"  [itemtype] USER-CONFIRMED (was MISSING from the master): "
                      f"{c:20s} -> {t}")
        for c, old, t in changed:
            print(f"  [itemtype] !! USER OVERRIDE — THE MASTER IS WRONG: {c:14s} "
                  f"'{old}' -> '{t}'")

    # ═══ FILL THE REST FROM `bom.child_description` — THE BOM CARRIES THE ITEMTYPE ═══════
    #
    # This is the key discovery. `bom.child_description` is NOT a free-text label — it IS the
    # ItemType column, exported from the same source system. MEASURED on this data:
    #
    #     BOM children that appear in the itemtype master : 1,476
    #     child_description == ItemType                   : 1,471   (99.66%)
    #
    # The 5 disagreements are pre-existing master typos (`TBA827`, `B617`, `BW15011`, and two
    # chemicals) — and the MASTER WINS on every one of them; we never overwrite it.
    #
    # So an item that is PRODUCED but missing from the master is not a mystery: the BOM already
    # states its type, 213 rows deep. Filling it is a LOOKUP FROM THE USER'S OWN DATA, not an
    # inference from the machine, the operation, or the code prefix.
    #
    # This matters a great deal. v6 keys MPQ floors, aging, planning_max_aging, AGE_OVERRIDE_H
    # and — critically — the MASTER COMPOUND *skip* on the ItemType string, and it builds its
    # map from master rows ONLY. An untyped item escapes EVERY one of those rules, silently.
    # `MT1511`/`MT1512` are MASTER COMPOUND: while untyped they were generating REAL PRODUCTION
    # LOTS (20 h on 440M, 25 h on 430-1) on top of 49 TONNES of opening WIP, because v6's
    # `mc_skip` never fired for them.
    bom_df = pd.read_csv(os.path.join(OUT, "bom.csv"), dtype=str, keep_default_na=False)
    children = {str(c).strip() for c in bom_df["child"] if str(c).strip()}
    rp = {str(r).strip() for r in rt["routed_product"] if str(r).strip()}
    have = set(df["ItemCode"].astype(str).str.strip())

    desc = {}
    if "child_description" in bom_df.columns:
        for c, d in zip(bom_df["child"], bom_df["child_description"]):
            c, d = str(c).strip(), str(d).strip()
            if c and d and d.lower() not in ("nan", "none"):
                desc.setdefault(c, d)

    # Only a BOM *child* that is also *routed* becomes demand (phase1b:227
    # `if ch not in routed: continue`). The ~240 finished-tyre SKU codes are routed (they have a
    # CURING operation) but are never a BOM child, so they need no ItemType — warning on them
    # would drown the real signal.
    produced = sorted((rp & children) - have)
    filled = {c: desc[c] for c in produced if c in desc}
    unknown = [c for c in produced if c not in desc]

    if filled:
        df = pd.concat([df, pd.DataFrame({"ItemCode": list(filled),
                                          "ItemType": list(filled.values())})],
                       ignore_index=True)
        print(f"  [itemtype] filled {len(filled)} PRODUCED item(s) from `bom.child_description` "
              f"(which IS the ItemType column — it agrees with the master on 99.66% of the "
              f"1,476 codes they share):")
        for c, t in sorted(filled.items(), key=lambda kv: kv[1]):
            print(f"               {c:22s} -> {t}")

    if unknown:
        print(f"  [itemtype] !! {len(unknown)} PRODUCED item(s) have NO ItemType and NO "
              f"child_description: {unknown}")
        print(f"             Every ItemType-keyed rule (MPQ floor, aging, planning_max_aging, "
              f"AGE_OVERRIDE_H, the MASTER COMPOUND skip) silently SKIPS them.")
        print(f"             Add them to `itemtype_overrides` in config.yaml, or to the master.")

    _w(df, "itemtype_master.csv")


# ------------------------------------------------------------------------- AGING
def aging() -> None:
    """CTP's aging master already carries v6's columns.

    NOTE what v6 does with a green tyre: there is NO green-tyre row in this master
    (0 of 2,658). v6 does not need one — `phase5_v2:869` hard-codes
        AGE_OVERRIDE_H = {"GREEN TYRES": (0.0, 72.0), "CARCASS": (0.0, 24.0)}
    keyed on the raw ItemType string. CTP's itemtype master must therefore type its
    green tyres as literally "GREEN TYRES" for that override to fire — see itemtype()
    and MEMORY.md.

    *** NO-INFORMATION ROWS ARE DROPPED, NOT KEPT BLANK. This is load-bearing. ***

    789 of CTP's 2,658 rows carry an ItemCode and FOUR EMPTY CELLS. v6's input contract
    is: an item is either FULLY SPECIFIED, or ABSENT from the master. Never
    present-but-blank. (Proof: v6's own aging_masterV4.csv has 7,213 rows and ZERO blanks
    in Min/MaxAging — which is why the parser below is not guarded against one.)

    A present-but-blank row does NOT behave like an absent one; it detonates:
        v6:107  aging_hours -> `float(v)`  -> float(NaN) is NaN, NOT an exception,
                                              so its `except` never fires
        v6:463  mn_age, mx_age = aging_map.get(item, (0.0, 72.0))   -> gets (NaN, NaN)
        v6:480  int(mx_age // wave_dur_h)  -> ValueError: cannot convert float NaN to integer

    An ItemCode with four empty cells carries NO INFORMATION — it is indistinguishable
    from an item the master never listed. So we DROP it, and v6's OWN documented default
    for an unlisted item fires:  `aging_map.get(item, (0.0, 72.0))`.
    Dropping a data-free row is DECLINING TO INVENT DATA. Writing a number in would be
    inventing it, and we do not.

    !! THIS IS A REAL BEHAVIOURAL DEFAULT, NOT A NO-OP. It is reported loudly at the end
       of this function and recorded in MEMORY.md. To override it, populate the aging
       master — do not patch the phases.
       (v6's phase5 additionally hard-overrides CARCASS -> (0,24) and GREEN TYRES -> (0,72)
       at placement via AGE_OVERRIDE_H, regardless of the master.)
    """
    df = _read("aging")
    need = ["ItemCode", "MaxAging", "MinAging", "MaxAgingUnit", "MinAgingUnit"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"aging master is missing v6 columns: {missing}")
    df = df[need].copy()

    # NB: `_read` uses keep_default_na=False, so a blank cell arrives here as "" (an empty
    # STRING), not NaN. It only BECOMES NaN later, when v6's phases read our CSV back with
    # pandas' default settings. So test for both — `.isna()` alone silently matches nothing.
    def _blank(s: pd.Series) -> pd.Series:
        return s.isna() | (s.astype(str).str.strip() == "")

    n0 = len(df)
    no_data = _blank(df["MaxAging"]) & _blank(df["MinAging"])
    if no_data.any():
        dropped = df.loc[no_data, "ItemCode"].astype(str).str.strip()
        df = df.loc[~no_data]
        print(f"  [aging] DROPPED {n0 - len(df)} of {n0} rows that had an ItemCode but NO "
              f"aging values.\n"
              f"          These items now take v6's OWN default for an unlisted item: "
              f"(min=0.0h, max=72.0h)  [v6 phase2:463]\n"
              f"          No aging value was invented. To override, populate the aging master.")
        try:
            it = pd.read_csv(os.path.join(OUT, "itemtype_master.csv"), dtype=str)
            tm = dict(zip(it["ItemCode"].astype(str).str.strip(),
                          it["ItemType"].astype(str).str.strip()))
            counts = dropped.map(lambda c: tm.get(c, "<untyped>")).value_counts()
            for t, n in counts.items():
                print(f"            {n:5d}  {t}")
        except Exception:
            pass

    # A HALF-blank row is NOT information-free — it means the master says something we
    # cannot read. Refuse rather than guess which half to trust.
    half = _blank(df["MaxAging"]) ^ _blank(df["MinAging"])
    if half.any():
        bad = df.loc[half, "ItemCode"].head(10).tolist()
        sys.exit(f"aging master has {int(half.sum())} rows with only ONE of Min/MaxAging "
                 f"filled, e.g. {bad}. That is not a no-information row and I will not "
                 f"guess the missing half. Fill or clear them.")

    # ═══ ITEMTYPE FALLBACK from the plant's aging table ════════════════════════════════
    # Source: ctp_inputfiles/aging_table_pcr.png, transcribed into config.yaml.
    # THE PER-ITEM MASTER ALWAYS WINS. This only fills items the master does not carry —
    # instead of letting them silently take v6's generic (0 h, 72 h) default.
    #
    # This was doing real damage: `CAP 66 - CAPSTRIP` is 61% of net demand and has NO row in
    # the aging master, so phase2 sized its lots believing max_aging = 72 h. Its real shelf
    # life is 24 h — a bare tacky ribbon on an open bobbin loses building tack fast. A
    # 3x-too-long shelf life means the scheduler builds it far too early and it reaches the
    # TBM dead.
    fb = CFG.get("aging_fallback_by_itemtype") or {}
    if fb:
        it_df = pd.read_csv(os.path.join(OUT, "itemtype_master.csv"), dtype=str)
        tmap = dict(zip(it_df["ItemCode"].astype(str).str.strip(),
                        it_df["ItemType"].astype(str).str.strip()))
        bom_df = pd.read_csv(os.path.join(OUT, "bom.csv"), dtype=str, keep_default_na=False)
        rt_df = pd.read_csv(os.path.join(OUT, "routing.csv"), dtype=str, keep_default_na=False)
        produced = ({str(c).strip() for c in bom_df["child"] if str(c).strip()} &
                    {str(r).strip() for r in rt_df["routed_product"] if str(r).strip()})
        have = set(df["ItemCode"].astype(str).str.strip())

        rows, by_t = [], {}
        for code in sorted(produced - have):
            pol = fb.get(tmap.get(code, ""))
            if not pol:
                continue
            # NEVER write a blank — that is the "present-but-blank" trap (see the rule at the
            # top of MEMORY.md): float(NaN) does not raise, so a blank detonates downstream
            # (it crashed phase4's `earliest_curing - Timedelta(hours=min_age)`).
            # A BLANK MIN in the plant table means "no minimum aging required" = 0.0, which is
            # ALSO v6's own default min. A blank MAX means the table states none — and we do
            # NOT invent one, so we fall back to v6's own default max (72 h) explicitly.
            rows.append({"ItemCode": code,
                         "MaxAging": float(pol.get("max_h", 72.0)),
                         "MinAging": float(pol.get("min_h", 0.0)),
                         "MaxAgingUnit": "Hours", "MinAgingUnit": "Hours"})
            by_t[tmap[code]] = by_t.get(tmap[code], 0) + 1
        if rows:
            df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
            print(f"  [aging] filled {len(rows)} produced item(s) from the PLANT AGING TABLE "
                  f"(ctp_inputfiles/aging_table_pcr.png). The per-item master always wins; "
                  f"these had NO row and would have taken v6's generic (0h, 72h):")
            for t, n in sorted(by_t.items(), key=lambda kv: -kv[1]):
                p = fb[t]
                print(f"            {n:4d}  {t:24s} min={p.get('min_h','-')}h "
                      f"max={p.get('max_h','-')}h")

    _w(df, "aging_master.csv")


# --------------------------------------------------------------------------- MPQ
def mpq() -> None:
    """MOQ (3).xlsx -> v6's mpq schema.

    v6 (`phase2.build_mpq_map`, :131-146) expects:
        Item Type, Minimum Run Qty, Maximum Run Qty, UOM, Fraction Allowed
    CTP's file has:
        Item Type, Minimum Order Qty, Maximum Order Qty, UOM     (no Fraction Allowed)

    Two facts, both verified in the file, both user-confirmed:
      1. `Maximum Order Qty` is BLANK for every one of the 12 real item types.
         "CTP has no max MPQ."

         *** THE COLUMN IS OMITTED, NOT WRITTEN BLANK. This is load-bearing. ***

         v6 supports a missing max — but ONLY as an ABSENT COLUMN, which is how its own
         earlier master `mpq_v2.csv` is written (4 cols, no max). Its reader is built for
         exactly that:  `try: mx = float(r["Maximum Run Qty"]) except (..., KeyError): mx = None`
         The `KeyError` in that except list IS the "no max" path.

         A BLANK column does NOT take that path, and fails silently-then-loudly:
             pandas reads blank            -> NaN
             float(NaN)                    -> NaN   (no exception! mx = NaN, not None)
             v6:461 `mpq_max <= 0`         -> False (NaN comparisons are always False)
             v6:575 `if mpq_max`           -> TRUE  (NaN is truthy)
             int(ceil(qty / NaN))          -> ValueError: cannot convert float NaN to integer
         v6's own mpq_v3.csv has the max populated on all 15 rows, so v6 never meets a NaN
         here and has no guard against one.

         With the column absent: mpq_max = None -> v6's MHE split never fires ->
         n_subs = n_dur (v6:597-599), i.e. lots split by the 8h duration cap alone.
         That is v6's own designed no-max behaviour, not a CTP special case.

      2. There is no `Fraction Allowed` column. v6's reader defaults it to "NO"
         (`r.get("Fraction Allowed","NO")`), which selects v6's else-branch
         `lot_qty_full = max(raw_qty, mpq_min)` (:567-568) — a plain floor, no
         rounding up to whole MPQ multiples. We write the column explicitly as NO so
         the behaviour is visible rather than implied.

    The compound rows read "3 Batches" (text). v6 does `float(...)` -> ValueError ->
    mn = None -> no floor at all for compounds. The batch SIZE lives in the routing
    (`batch_size`), which is what v6 rounds compounds to anyway, so we leave these as
    text and let v6 drop them. We do NOT invent a KG number.
    """
    p = _p("mpq")
    # DB MODE: jkt_mpq_pcr is byte-identical to the workbook's 'mpq' sheet -- SAME 22 rows,
    # SAME item-type naming faults (Calendar Roll (spool), Edge strip x4, BRTTL, ...). Loading
    # it raw would silently undo the corrections, so db_pipeline.adapters.adapt_mpq applies
    # the user-confirmed renames + duplicate collapse and drops the unmappable rows.
    if USE_DB:
        try:
            import sys as _s
            _dbp = os.path.join(HERE, "db_pipeline")
            if _dbp not in _s.path:
                _s.path.insert(0, _dbp)
            import adapters as _ad                        # noqa: E401
            _raw = _db_table("mpq")
            if _raw is not None:
                _w(_ad.adapt_mpq(_raw), "mpq.csv")
                return
        except Exception as _e:
            print(f"  [adapt] mpq: DB read failed ({_e!r}) — falling back to the workbook.")
    raw = pd.read_excel(p, sheet_name="PCR", header=0, engine="openpyxl")
    rows = []
    for _, r in raw.iterrows():
        it = str(r.iloc[0]).strip()
        if not it or it.lower() in ("nan",):
            continue
        if it.lower().startswith(("for mixers", "machine code")):
            break                                    # the mixer table / footer
        rows.append({
            "Item Type":         it,
            "Minimum Run Qty":   r.get("Minimum Order Qty", ""),
            # NO `Maximum Run Qty` COLUMN — see docstring. It is OMITTED, not blanked.
            "UOM":               str(r.get("UOM", "")).strip(),
            "Fraction Allowed":  "NO",               # no such column in CTP's file
        })
    # MPQ floors stay in M — the lot quantities they are compared against are in M.
    # (v6's own mpq_v3 is in MM because v6's BOM is in MM. CTP is in M throughout.)
    _w(pd.DataFrame(rows), "mpq.csv")


# ------------------------------------------------------------- MIXING MPQ (mixers)
def mixing_mpq() -> None:
    """The 'mixing mpq' sheet of the MPQ workbook -> inputs/mixing_mpq.csv.

    WIRED 2026-07-23 (USER: "we have to read this also ... so this should be always used").
    Until now NOTHING read this sheet: the compound floor came from config
    (mixing_mpq_min_batches) and there was NO maximum at all, which is why phase2 printed
    "MHE MODEL INERT: no Maximum Run Qty -> mhe_total = 1" on every run.

    The sheet is PER MIXER:
        Machine code | Machine name | Min Run Qty(batches) | Min Run Qty(KG)
                                    | Max Run Qty(batches) | Max Run Qty(KG)
    `Routing_machine_name` is the join key to routing.machines (the plant file's own codes
    101-106 do NOT appear in the routing; the NAMES do, with 270F->F270 and 310F->310
    renamed - both flagged for plant confirmation).

    Written even if the sheet is missing (empty file), so phase2 can always read it.
    """
    cols = ["machine", "min_batches", "min_kg", "max_batches", "max_kg"]
    try:
        raw = pd.read_excel(_p("mpq"), sheet_name="mixing mpq", engine="openpyxl")
    except Exception as e:
        print(f"  [WARN] no 'mixing mpq' sheet in {_p('mpq')} ({e}); writing empty mixing_mpq.csv")
        _w(pd.DataFrame(columns=cols), "mixing_mpq.csv")
        return

    def _pick(*names):
        for n in names:
            for c in raw.columns:
                if str(c).strip().lower() == n.lower():
                    return c
        return None

    c_name = _pick("Routing_machine_name") or _pick("Machine name")
    c_minb = _pick("Minimum Run Qty(in batches)", "min_batches")
    c_mink = _pick("Minimum Run Qty(in KG)", "min_kg")
    c_maxb = _pick("Maximum Run Qty(in batches)", "max_batches")
    c_maxk = _pick("Maximum Run Qty(in KG)", "max_kg")

    rows = []
    for _, r in raw.iterrows():
        m = str(r.get(c_name, "")).strip() if c_name else ""
        if not m or m.lower() in ("nan", "none", ""):
            continue
        def _f(c):
            try:
                v = float(r.get(c))
                return v if v == v and v > 0 else None      # NaN/<=0 -> None, never NaN
            except (TypeError, ValueError):
                return None
        rows.append({"machine": m, "min_batches": _f(c_minb), "min_kg": _f(c_mink),
                     "max_batches": _f(c_maxb), "max_kg": _f(c_maxk)})
    df = pd.DataFrame(rows, columns=cols)
    _w(df, "mixing_mpq.csv")
    if len(df):
        print(f"    mixers: {', '.join(df['machine'])}  |  min_kg "
              f"{df['min_kg'].min():.0f}-{df['min_kg'].max():.0f}  |  max_kg "
              f"{df['max_kg'].min():.0f}-{df['max_kg'].max():.0f}")


# ------------------------------------------------------------------------ BUFFER
def buffer() -> None:
    df = _read("buffer")
    _w(df, "buffer_master.csv")


# --------------------------------------------------------------- PLANNING MAXAGE
# ============================================================================
# DELETED 2026-07-14 -- (A) DEFECT. `planning_max_aging()` USED TO BE HERE.
# ============================================================================
# It wrote inputs/planning_max_aging.csv out of `config.planning_max_aging_h`.
#
# **I INVENTED THAT FILE. THE PLANT NEVER SUPPLIED ONE.** Its own Notes column
# said "from ctp_v6/config.yaml" -- it was CONFIG LAUNDERED INTO A CSV TO LOOK
# LIKE A PLANT MASTER, and then read back by phase4 as if it carried authority.
# Every other writer in this adapter reshapes something the plant actually sent.
# This one manufactured a master out of my own opinion. That is the defect.
#
# IT WAS LIVE: phase4_cpm.py:370-377 read it into `planning_max_map`, feeding AEST
# through `get_effective_max_aging = min(chemistry_max, planning_max)`.
#
# It was harmless BY COINCIDENCE (all binding values happened to equal the
# chemistry max), NOT BY CONSTRUCTION -- a landmine, not a no-op. See the long
# note in config.yaml where `planning_max_aging_h` used to be.
#
# DELETION IS A PROVABLE ZERO SCHEDULE DELTA. Verified against the real inputs,
# over the only set that can ever produce a lot (routed AND a BOM child):
#     PRODUCIBLE items where min(chem, planning) != chem :  0
# and the one item where it DID bind (CAP30) is neither routed nor a BOM child,
# so it never becomes demand and phase4 never evaluates it.
#
# v6 degrades gracefully with the file ABSENT and says so (phase4_cpm.py:142):
#     "planning_max_aging policy empty - AEST will use chemistry max_aging only"
#
# DO NOT RE-ADD THIS. If the plant ever supplies a REAL planning-aging policy, it
# belongs in ctp_inputfiles/ as a plant master with real provenance.
#
# The stale inputs/planning_max_aging.csv from earlier runs is REMOVED below, in
# main(), so a leftover file cannot keep feeding phase4 after this change.


# --------------------------------------------------------------------- INVENTORY
def inventory() -> None:
    """opening_wip.csv -> v6's inventory schema (phase1c / phase2 WIP).

    *** `unit` BACKFILL — EXACT MATCH ONLY, FROM THE OTHER MASTERS. NO FUZZY. ***

    WHY THIS MATTERS. v6 converts length stock to MM (db_loader:286, phase5:1466):
        _f = inv["unit"].astype(str).str.upper().str.strip().map(_L2MM).fillna(1.0)
    A BLANK unit -> astype(str) -> "nan" -> .map() misses -> NaN -> **.fillna(1.0)** ->
    factor 1.0, i.e. **MM**. If the real unit is **M**, that stock is credited at
    **1/1000th**. v6 hit this exact bug once; its own comment (db_loader:280-285) says the
    1/1000th credit "drove the cap-strip / ply over-ageing".
    NOTE `fillna(1.0)` is CORRECT for NOS/KG items — only LENGTH items are at risk.

    The unit is a **LABEL, not a quantity**, and the same item states its unit in the other
    masters. So we resolve a blank by EXACT itemcode lookup, in this priority order:
        1. a sibling WIP row      — same itemcode, non-blank unit
        2. bom.child_Unit         — exact `child` match
        3. routing.batch_UNIT     — exact `routed_product` match
        4. routing.proc_time_UOM  — M/MIN -> M, MM/MIN -> MM, NOS/MIN -> NOS
           (a rate's numerator IS the item's unit: 65 M/MIN means the item is measured in M)
    A source is used ONLY if it gives ONE unambiguous unit for that itemcode. This is a
    LOOKUP, not a guess — no value is invented, and nothing is fuzzy-matched.

    Anything still unresolved is LEFT BLANK, so v6's own `fillna(1.0)` default applies.
    On the current data that is harmless: all 2,041 blank-unit LENGTH rows are on items
    that NO BOM ever raises, so the scheduler never looks them up (dead WIP). The backfill
    is what protects us the moment one of them enters a BOM.
    """
    # 2026-07-29 COLD START: write an EMPTY inventory (no opening WIP) when zero_opening_wip is on.
    if bool(CFG.get("zero_opening_wip", False)):
        _w(pd.DataFrame(columns=["production_id", "produced_time", "itemcode",
                                 "item_type", "unit", "inventory"]), "inventory.csv")
        print("  [inventory] zero_opening_wip: COLD START -- inventory.csv written EMPTY (0 WIP).")
        return
    df = _read("inventory")
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    ic = pick("itemcode", "item_code", "material_code_o")
    qc = pick("inventory", "qty", "quantity", "livequantity")
    if not ic or not qc:
        sys.exit(f"opening_wip: cannot find itemcode/qty columns in {list(df.columns)}")
    out = pd.DataFrame({
        "production_id": df.get(pick("production_id") or ic, ""),
        "produced_time": df.get(pick("produced_time", "producedtime") or ic, ""),
        "itemcode":      df[ic].astype(str).str.strip(),
        "item_type":     df.get(pick("item_type", "itemtype") or ic, ""),
        "unit":          df.get(pick("unit", "uom") or ic, ""),
        "inventory":     pd.to_numeric(df[qc], errors="coerce").fillna(0.0),
    })
    out = out[out["inventory"] > 0]

    # ---- unit backfill: EXACT itemcode match, from the other masters (see docstring) ----
    out["unit"] = out["unit"].astype(str).str.strip()
    blank = out["unit"].isin(("", "nan", "NaN", "None"))
    n_blank = int(blank.sum())
    if n_blank:
        def _one(df_, key, val, xform=lambda v: v):
            """itemcode -> unit, but ONLY where the source is unambiguous for that code."""
            if key not in df_.columns or val not in df_.columns:
                return {}
            d = df_[[key, val]].dropna()
            d = d.assign(_k=d[key].astype(str).str.strip(),
                         _v=d[val].astype(str).str.strip().str.upper().map(xform))
            d = d[d["_v"].notna() & (d["_v"] != "")]
            g = d.groupby("_k")["_v"].agg(lambda s: set(s))
            return {k: next(iter(v)) for k, v in g.items() if len(v) == 1}

        def _rate_to_unit(u):
            """A rate's NUMERATOR is the item's unit: 65 M/MIN => the item is in M."""
            return {"M/MIN": "M", "MTR/MIN": "M", "MM/MIN": "MM", "NOS/MIN": "NOS",
                    "CM/MIN": "CM"}.get(u)

        bom_df = pd.read_csv(os.path.join(OUT, "bom.csv"), dtype=str)
        rt_df  = pd.read_csv(os.path.join(OUT, "routing.csv"), dtype=str)
        sib = out.loc[~blank, ["itemcode", "unit"]]
        sib = sib.assign(_v=sib["unit"].str.upper())
        sibg = sib.groupby("itemcode")["_v"].agg(lambda s: set(s))
        sources = [
            ("WIP sibling row",      {k: next(iter(v)) for k, v in sibg.items() if len(v) == 1}),
            ("bom.child_Unit",       _one(bom_df, "child", "child_Unit")),
            ("routing.batch_UNIT",   _one(rt_df, "routed_product", "batch_UNIT")),
            ("routing.proc_time_UOM", _one(rt_df, "routed_product", "proc_time_UOM",
                                           _rate_to_unit)),
        ]
        filled, hits = out["unit"].copy(), {}
        for name, m in sources:
            if not m:
                continue
            still = filled.isin(("", "nan", "NaN", "None"))
            got = out.loc[still, "itemcode"].map(m)
            n = int(got.notna().sum())
            if n:
                filled.loc[still & got.notna().reindex(filled.index, fill_value=False)] = \
                    got[got.notna()]
                hits[name] = n
        out["unit"] = filled
        left = int(out["unit"].isin(("", "nan", "NaN", "None")).sum())
        print(f"  [inventory] unit was BLANK on {n_blank:,} rows. Backfilled by EXACT itemcode "
              f"match (no fuzzy):")
        for name, n in hits.items():
            print(f"                {n:6,} rows  <- {name}")
        print(f"              {left:6,} rows still blank -> v6's own fillna(1.0) default. "
              f"(Harmless: none are on items any BOM raises.)")

    _w(out, "inventory.csv")


# ------------------------------------------------------------- CHANGEOVER MATRIX
def changeover() -> None:
    """CTP-ONLY. v6 has NO changeover matrix — it derives setup minutes from machine
    NAMES (`compute_changeover_min`, phase5_v2:139-170: WBC/LTBC/HTBC/FISCHER/DUPLEX/
    TRIPLEX/…). NONE of CTP's 123 machine ids match any of those branches, so under
    v6's own rule every CTP machine would fall to DEFAULT_CHANGEOVER_MIN = 15 (mixing
    = 2).

    The user supplied a real plant matrix (`jkt_changeover_matrix_combined (1).xlsx`) and
    instructed it be used: "we don't have [it] in the btp v6 but we will use it in the ctp".

    *** KEYED ON (LINE, from_item, to_item). The LINE comes from the user's machine list. ***

    The matrix is (machine, from_MaterialCode_O, to_MaterialCode_O) -> minutes. Its `machine`
    column holds 22 machine *LINE NAMES* spanning BOTH plants — "4 RC Calendar",
    "Belt Cutter PCR", "Quadraplex TBR SW" — while CTP's routing holds numeric machine *IDs*
    ("901", "1103", "3409"). The two sets have **ZERO string overlap**.

    The bridge is `machine_to_changeover_line` in config.yaml, built from the user-supplied
    CTP machine list (`pcr machines.png`). It is DATA, not a guess, and it is editable.

    A machine with no mapping, or an item pair the matrix does not carry for that line, falls
    through to v6's own `compute_changeover_min` (mixing = 2 min, else 15 min) — unchanged.
    That is the honest outcome for Building (3401-3411) and the mixers, which the matrix simply
    does not cover.

    Keying on the line also RESOLVES the 23 item pairs that carry different times on different
    lines (e.g. `EG01 -> EG02` = 10 min on the Cap Ply Cutter, 17 on the Edge Gum Calendar,
    24 on the PCR Roller Head). With the line known we take the RIGHT one instead of guessing.
    """
    p = _p("changeover_matrix")
    if not os.path.exists(p):
        print("  changeover_matrix       (absent — v6's compute_changeover_min will be used)")
        return
    df = (pd.read_csv(p, low_memory=False) if p.lower().endswith(".csv")      # live DB export
          else pd.read_excel(p, sheet_name=0, engine="openpyxl"))
    df.columns = [str(c).strip() for c in df.columns]
    _w(df, "changeover_matrix.csv")

    need = ["machine", "from_MaterialCode_O", "to_MaterialCode_O", "changeover_time_min"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"changeover matrix is missing columns {missing}; got {list(df.columns)}")

    m2l = {str(k).strip(): str(v).strip()
           for k, v in (CFG.get("machine_to_changeover_line") or {}).items()}
    if not m2l:
        sys.exit("config.yaml has no `machine_to_changeover_line`. Without it the matrix's "
                 "line NAMES cannot be joined to the routing's machine IDs and the whole "
                 "matrix would be silently ignored.")

    d = df.copy()
    d["_l"] = d["machine"].astype(str).str.strip()
    d["_f"] = d["from_MaterialCode_O"].astype(str).str.strip()
    d["_t"] = d["to_MaterialCode_O"].astype(str).str.strip()
    d["_m"] = pd.to_numeric(d["changeover_time_min"], errors="coerce")
    d = d[(d["_f"] != "") & (d["_t"] != "") & d["_m"].notna()]
    d = d[~d["_f"].str.lower().isin(("nan",)) & ~d["_t"].str.lower().isin(("nan",))]

    lines_we_use = set(m2l.values())
    unknown = sorted(lines_we_use - set(d["_l"]))
    if unknown:
        sys.exit(f"config.machine_to_changeover_line points at line name(s) that do NOT exist "
                 f"in the matrix: {unknown}. Matrix has: {sorted(set(d['_l']))}")

    keep = d[d["_l"].isin(lines_we_use)]
    lut = (keep.groupby(["_l", "_f", "_t"])["_m"].max().reset_index())
    lut.columns = ["line", "from_item", "to_item", "changeover_min"]
    _w(lut, "changeover_lookup.csv")

    rt_df = pd.read_csv(os.path.join(OUT, "routing.csv"), dtype=str)
    rmach = set()
    for m in rt_df["machines"].dropna():
        for x in re.split(r"[,;/|]", str(m)):
            x = x.strip().strip('"').strip()
            if x and x.upper() not in ("NAN", "NONE"):
                rmach.add(x)
    mapped = sorted(rmach & set(m2l))
    unmapped = sorted(rmach - set(m2l))

    print(f"  [changeover] joined via config.machine_to_changeover_line (from the plant's "
          f"machine list) — the matrix's line NAMES have zero overlap with the routing's IDs.")
    print(f"               {len(lut):,} (line, from_item, to_item) rules on "
          f"{lut['line'].nunique()} lines: {', '.join(sorted(set(lut['line'])))}")
    print(f"               routing machines MAPPED   : {len(mapped)}  {mapped}")
    print(f"               routing machines UNMAPPED : {len(unmapped)} -> v6's own rule "
          f"(mixing 2 min / default 15 min). Includes Building + curing: the matrix has no "
          f"line for them.")


# --------------------------------------------------------------- MG ASSIGNMENT
# REMOVED 2026-07-22 (user instruction: "remove this mg assignment complete from every
# phase, we are not using this"). CTP's BOM is single-variant (Equipment == 'TBM PCR' on
# all 42,411 rows), so v6's MG filter kept every row: it carried ZERO information. The
# mg_assignment() synthesiser, outputs/mg_assignment.csv, and the MG filter/keys in
# phase1b/phase1c/phase3 were all torn out together. Removal is provably schedule-neutral:
# a constant column cannot change any BOM walk, groupby, sort, merge, or DAG key. Verified
# byte-identical outputs2/ (see _mg_hashes_baseline.json regression gate).
#
# RE-INTRODUCE MG (revert from *.bak_mgremoval) if the BOM is ever re-issued with more than
# one Equipment per SKU — then the filter stops being a no-op and MG becomes load-bearing.


def validate_uom() -> None:
    """UOM CONSISTENCY GUARD (2026-08-01). The scheduler computes duration = qty / (rate x eff),
    treating the QUANTITY and the machine RATE as the same unit. If an item's quantity unit
    (BOM child_unit) disagrees with its routing rate unit (proc_time_UOM) -- e.g. a KG quantity
    against an M/MIN rate, or an M quantity against a CUTS/MIN rate -- that duration is WRONG, and
    until now it was SILENT. This does NOT change any unit (units are plant-owned; per directive):
    it only SURFACES every mismatch loudly so the scheduler can never divide mismatched units in
    silence again. Batch ops (SEC/BATCH) use batch_size, not a length/weight rate -> excluded."""
    rt = pd.read_csv(os.path.join(OUT, "routing.csv"), low_memory=False, dtype=str).fillna("")
    bm = pd.read_csv(os.path.join(OUT, "bom.csv"), low_memory=False, dtype=str).fillna("")
    rpc = next((c for c in rt.columns if c.lower() == "routed_product"), None)
    puc = next((c for c in rt.columns if "proc_time_uom" in c.lower()), None)
    cc  = next((c for c in bm.columns if c.lower() == "child"), None)
    uc  = next((c for c in bm.columns if "child" in c.lower() and "unit" in c.lower()), None)
    if not (rpc and puc and cc and uc):
        return

    def _implied(u):
        u = str(u).upper()
        if "BATCH" in u:            return "BATCH"     # batch_size handles it -> cannot mismatch
        if "M/MIN" in u or u == "M": return "M"
        if "CUT" in u:              return "NOS"       # a cut is a piece
        if "NOS" in u:              return "NOS"
        if "KG" in u:               return "KG"
        return u.strip() or "?"

    bom_uom = bm.groupby(bm[cc].astype(str).str.strip())[uc].first().astype(str).str.upper()
    rt = rt.copy()
    rt["_ru"] = rt[puc].map(_implied)
    rt["_qu"] = rt[rpc].astype(str).str.strip().map(bom_uom).fillna("")
    mm = rt[(rt["_ru"].isin(["M", "KG", "NOS"])) & (rt["_qu"] != "") & (rt["_ru"] != rt["_qu"])
            & ~((rt["_ru"] == "NOS") & (rt["_qu"].isin(["NOS", "PCS"])))]
    n = mm[rpc].nunique()
    if not n:
        print("  [uom-guard] OK -- every item's quantity unit matches its machine-rate unit.")
        return
    g = (mm.groupby([puc, "_qu"])[rpc].nunique().reset_index(name="items")
           .sort_values("items", ascending=False))
    print(f"  !! [uom-guard] {n} item(s) have a QUANTITY unit that DISAGREES with the MACHINE-RATE "
          f"unit -> duration = qty/(rate x eff) is WRONG for them (was SILENT):")
    for _, r in g.iterrows():
        print(f"       rate {str(r[puc]):12} vs quantity in {str(r['_qu']):5} : {int(r['items'])} item(s)")
    print("       NOT auto-converted (units are plant-owned). Supply the conversion factor "
          "(cut-length for CUTS<->M, kg-per-m for KG<->M) to make these durations correct.")


def main() -> int:
    # --full  : do NOT apply config.excluded_items — build the FULL curing schedule.
    #           Phase 0 is a VALIDATION report; its whole job is to SEE the problems, so it
    #           must look at every SKU, including the ones we cannot schedule.
    #           (Phases 2+ will still crash on the 3 items with no proc_time — that is the
    #            point of the exclusion, and the point of phase 0 finding them first.)
    if "--full" in sys.argv:
        CFG["excluded_items"] = []
        print("*** --full : config.excluded_items IGNORED. Building the FULL curing "
              "schedule (all 34 SKUs / 366,902 tyres). ***")

    os.makedirs(OUT, exist_ok=True)
    print(f"CTP inputs  : {SRC}")
    print(f"v6 inputs   : {OUT}")
    print("-" * 62)
    bom()
    routing()
    plan()
    building_plan()
    itemtype()
    aging()
    mpq()
    mixing_mpq()
    buffer()
    inventory()
    changeover()
    validate_uom()   # 2026-08-01: surface qty-vs-rate UOM mismatches loudly (never silent again)
    # mg_assignment() REMOVED 2026-07-22 — see MG ASSIGNMENT note above.

    # ---- PURGE THE INVENTED MASTER (see the DELETED block above) --------------------
    # phase4_cpm.py:370-377 reads inputs/planning_max_aging.csv if it is THERE. Removing
    # the writer is not enough -- a file left behind by an earlier run would keep feeding
    # AEST forever, which is exactly the silent-authority failure we are removing. Delete
    # it, loudly, every run.
    stale = os.path.join(OUT, "planning_max_aging.csv")
    if os.path.exists(stale):
        os.remove(stale)
        print("  [planning_max_aging] REMOVED stale inputs/planning_max_aging.csv -- I "
              "INVENTED that file; the plant never supplied one. phase4 now uses "
              "CHEMISTRY max_aging only (its own documented fallback). Verified ZERO "
              "schedule delta: no producible item had min(chem, planning) != chem.")

    print("-" * 62)
    print("NOT PROVIDED by CTP (v6 degrades gracefully — see MEMORY.md):")
    for f, why in [
        ("mg_preference.csv",      "MG excluded by user instruction"),
        ("belt_wire_mapping.csv",  "CTP has no belt-wire campaign data"),
        ("building_cycle_times.csv", "no per-machine cycle times; routing duration is used"),
        ("building_changeover.csv", "no per-machine building setup table"),
    ]:
        print(f"  {f:<26} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
