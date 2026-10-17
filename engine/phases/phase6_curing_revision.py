#!/usr/bin/env python3
"""
phase6_curing_revision.py
=========================

Revises the curing schedule (jkt_plan_in_schedule) against the finalised
Building schedule produced by Phase 5.

ALGORITHM (per the V1 client spec):
  For each press independently:
    1. Sort all curing jobs by original startTime.
    2. For each curing job, FIFO-consume Building lots of the same SKU
       until the building qty satisfies the curing lot's qty. The "kit
       ready" time is the endTime of the last consumed building lot.
    3. candidate_start = max( original_start, kit_ready, previous_job_end_on_press )
    4. new_end = candidate_start + ORIGINAL DURATION  (duration preserved)
    5. Update startTime, endTime, shift, date.  Qty and press unchanged.
    6. Move to next job on the press.

  !! "For each press INDEPENDENTLY" (line 3 above) is a statement about the
     PRESS CONSTRAINT (prev_end is per-press), NOT about the kit pool. The kit
     pool is GLOBAL and is consumed "chronologically" (KitPool docstring, Rule).
     A per-press OUTER LOOP over a GLOBAL pool serves presses in the order the
     loop happens to visit them -- which is not chronological. See FIX F1.

INPUTS
  jkt_plan_in_schedule  (via db_loader.load_plan)  -- original curing plan
  Finalised Building schedule from Phase 5
        DB table:  jkt_floor_endfwd_schedule  (preferred)
        File:      outputs2/phase5_schedule_updated.csv  (fallback)
  inputs/inventory.csv  -- opening WIP; seeds the kit pool (FIX F5)
  inputs/bom.csv        -- GT-code -> SKU link for the inventory seed (FIX F5)

OUTPUTS
  DB table:  jkt_curing_schedule_revised  (TRUNCATE + INSERT)
  File   :   outputs2/phase6_curing_revision_updated.csv  (atomic write)

The script is fault-tolerant: if no Building schedule is available the original
plan is mirrored unchanged so the downstream output is never missing.

--------------------------------------------------------------------------
PHASE 6 CORRECTIONS  (the LOGIC and the ALGO are UNCHANGED -- corrected INSIDE)
--------------------------------------------------------------------------
The `candidate_start = max(orig_start, kit_ready, prev_end)` formula, the FIFO
pool, the duration-preservation and the press queue order are all IDENTICAL to
what they were. Every fix below restores the algorithm's OWN STATED INTENT.

  F1  Kit pool drained in GLOBAL TIME order (was: alphabetical press order).
  F2  KitPool.take() reports SHORT / UNKNOWN instead of failing open silently.
  F3  PAST_HORIZON is flagged and reported (config asks for it; phase5 does it).
  F4  Post-move green-tyre aging audit (report-only).
  F5  Kit pool seeded from opening WIP (inventory.csv), age-checked.
  F6  Floor-schedule append is IDEMPOTENT (was: accumulated on every re-run).
  F7  Mould-change / changeover windows are reported, and optionally re-inserted
      as pinned busy windows (cfg: reinsert_changeover_blocks).

--------------------------------------------------------------------------
PHASE 6 ROUND-2 CORRECTIONS  (E3 / E4 / E8 / S5 = REPORT-ONLY; B1 = default;
                              B2 = investigation + loud report)
--------------------------------------------------------------------------
  E3  (A) DEFECT. The green-tyre age report measured the WRONG TYRE. It aged the
          block against `kit_ready`, which take() sets to the finish of the LAST
          lot consumed -- and the pool is FIFO (oldest first), so the last lot
          consumed is the NEWEST one. Shelf life is decided by the OLDEST tyre in
          the kit. take() now also returns `kit_oldest`; the VERDICT uses it.
          NO SCHEDULE CHANGE (timing still uses kit_ready == newest: a kit is not
          complete until the LAST lot lands. That was, and remains, correct).

  E4  (A) DEFECT. verify_invariants() could not fail. Every one of its four checks
          was a logical consequence of the loop that produced the data, read back
          out of that same loop's own variables. A gate that cannot fail is not a
          gate; it is a decoration that costs you vigilance. It now re-reads the
          WRITTEN CSV from disk, re-loads the ORIGINAL plan from db_loader, joins
          them on a persisted stable key (`block_id`), and re-derives every
          invariant from those two files alone. It is also SELF-TESTED: it is run
          once against a deliberately corrupted copy and must FAIL on it, or phase
          6 says so out loud (cfg: verify_selftest, default True).

  E8  (A) DEFECT. The green tyres that are ALREADY past shelf life at plan start
          were dropped from the kit pool (correctly) and then only print()ed. Real
          scrap exposure that a planner could never see. Now written to
          outputs2/phase6_expired_opening_wip.csv.

  S5  (A) DEFECT (report). Blocks whose green tyre was NEVER BUILT are scheduled,
          counted, and included in every "% cured" figure. Now stated, unmissably,
          at the end of the run.

  B1  (B) POLICY -- DEFAULT RESTORED TO v6. `seed_kitpool_from_inventory` defaulted
          to True in the CODE. config.yaml itself calls the flag "(B), not (A)" --
          and an admitted POLICY must default to v6's behaviour ("the pool starts
          empty"). The code fallback is now False. What config.yaml SHIPS is the
          config owner's call, not this file's.
          COST of the v6 default: makespan +21.7 h, kit shortfall +3,924 tyres.
          That is the price of v6's rule, and it is v6's to pay until someone
          decides otherwise IN CONFIG.

  B2  INVESTIGATION -- NOT a silent reconciliation. phase1c and phase6/F5 read the
          SAME inventory.csv with DIFFERENT rules and disagree by 125 tyres. The
          disagreement is now DERIVED FROM THE DATA and REPORTED ON EVERY RUN
          (reconcile_wip_with_phase1c). No rule was invented. See that function's
          comment block for the full finding.
"""
from __future__ import annotations
import os
import re
import sys
import pathlib
import tempfile
from datetime import timedelta

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs2"
INPUTS = ROOT / "inputs"
OUTPUTS.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "phases"))

# S2 -- the drum pin. P1 = the press start of the PRIOR pass, snapshot by
# run_pipeline.py. In pass 2+ phase6 pins candidate_start >= P1 (a HARD FLOOR), so
# a block cannot slide EARLIER than the slot building was re-timed for. That, plus
# the phase4 re-anchor, is what makes the loop converge in one extra pass.
P1_SNAPSHOT = OUTPUTS / "phase6_press_start_P1.csv"


def _block_to_int(bid):
    s = str(bid).strip()
    if s[:1] in ("B", "b"):
        s = s[1:]
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def load_p1_floor(cfg) -> dict:
    """block_int -> P1 press start (pd.Timestamp). {} unless converge is ON AND the
    snapshot exists (i.e. this is pass 2+). Never raises."""
    if not bool((cfg or {}).get("converge_building_to_press", False)):
        return {}
    if not P1_SNAPSHOT.exists():
        return {}
    try:
        df = pd.read_csv(P1_SNAPSHOT)
        df["startTime"] = pd.to_datetime(df["startTime"], errors="coerce")
        out = {}
        for b, t in zip(df["block_id"], df["startTime"]):
            bi = _block_to_int(b)
            if bi is not None and pd.notna(t):
                out[bi] = pd.Timestamp(t)
        return out
    except Exception as _e:
        print(f"  [S2] P1 snapshot present but unreadable ({_e}) -- no press floor applied")
        return {}

# Schema that Phase 5 uses for jkt_floor_endfwd_schedule / phase5_floor_schedule_updated.csv
FLOOR_SCHEMA = [
    "date", "machine", "shift", "item", "item_type", "process", "department",
    "start_time", "end_time", "produce_qty", "UOM", "lot_id",
]

# Names that identify a Building operation in the finalised schedule.
# We use the LAST building stage (Green Tyre Building / Stage-2) when present —
# that is the lot whose completion makes a complete tyre kit ready for curing.
BUILDING_PROCESS_TOKENS = (
    "GREEN TYRE BUILDING",   # Stage-2
    "GT BUILDING",
    "CAR BUILDING",          # Generic carcass-then-tyre line
    "TYRE BUILDING",
)
# Stage-2 / Green Tyre keywords (preferred when both stages are present)
STAGE2_TOKENS = ("GREEN TYRE", "STAGE2", "STAGE 2", "GT BUILDING")

# Rows phase 6 removes from the press plan before scheduling. They carry qty == 0
# but they DO reserve press time. See FIX F7.
CHANGEOVER_SKU_TOKENS = ("CHANGEOVER", "MOULD_CLEAN", "MOULD CLEAN")

# FIX F2 -- discriminated kit status.
KIT_READY   = "READY"     # enough building qty existed; kit_ready is a real timestamp
KIT_SHORT   = "SHORT"     # the SKU is known but the pool ran dry part-way through
KIT_UNKNOWN = "UNKNOWN"   # the SKU has NO building lots at all (routing/data hole)
KIT_NONE    = "NO_QTY"    # qty <= 0 -- nothing to take


# ------------------------------------------------------------------ helpers
def _load_cfg():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def _norm_code(s) -> str:
    """Whitespace-normalise a shop-floor code, then upper-case.

    The inventory master writes green tyres as a shop-floor DESCRIPTION with
    ragged internal spacing ("GT  T1457 STAR" -- note the DOUBLE SPACE), while
    the BOM writes the same item as "GT T1457 STAR". Collapsing internal runs of
    whitespace is the ONLY normalisation applied.

    !! THIS IS NOT FUZZY MATCHING. After this collapse the comparison is an
       EXACT string equality. No edit distance, no prefix match, no token
       overlap, no "closest" candidate. A code either matches byte-for-byte
       after whitespace-collapse or it does not match at all.
    """
    return re.sub(r"\s+", " ", str(s)).strip().upper()


def _atomic_to_csv(df: pd.DataFrame, dest: pathlib.Path) -> pathlib.Path:
    """Write CSV to a sibling tmp file then os.replace — OneDrive-safe."""
    tmp = dest.with_name(".tmp_" + dest.name)
    try:
        df.to_csv(tmp, index=False)
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
    return dest


_V23 = {"cure": None, "trimmed": 0, "blocks": 0}


def _qshift_v23(cfg, sku, r):
    """V23 curing capacity: (q_shift, press minutes per tyre) for one plan block, or None when OFF.
    q_shift = floor(cavities x shift_min / (cure + allowance)); cure = routing CURING time of the SKU
    (JK_CTP_PPDS.CTP_Master_Routing), else the plan's cycleTime."""
    c = ((cfg or {}).get("capacity_rules_v23") or {}).get("curing") or {}
    if not c.get("enabled", False):
        return None
    if _V23["cure"] is None:
        _V23["cure"] = {}
        try:
            rt = pd.read_csv(ROOT / "db_pipeline" / "raw" / "JK_CTP_PPDS__CTP_Master_Routing.csv",
                             dtype=str, low_memory=False)
            rt = rt[rt["operation_name"].astype(str).str.strip() == "CURING"]
            t = pd.to_numeric(rt["proc_time"], errors="coerce")
            t = t.where(rt["proc_time_UOM"].astype(str).str.upper() != "SEC", t / 60.0)
            for s, v in zip(rt["routed_product"].astype(str).str.strip(), t):
                if pd.notna(v) and v > 0:
                    _V23["cure"].setdefault(s, float(v))
            print(f"  [V23 CURE CAP] routing cure time for {len(_V23['cure'])} SKUs")
        except Exception as e:
            print(f"  [V23 CURE CAP] !! routing unreadable ({type(e).__name__}: {e}) -- plan cycleTime used")
    pt = (cfg or {}).get("curing_press_time_actual") or {}
    cure = _V23["cure"].get(sku)
    if cure is None:
        try:
            cure = float(r.get("cycleTime") or 0)
        except (TypeError, ValueError):
            cure = 0.0
    if not cure or cure <= 0:
        return None
    tbr = sku.startswith(tuple(pt.get("tbr_sku_prefix", ["11"])))
    alw = float(pt.get("allowance_tbr_min", 6.0) if tbr else pt.get("allowance_pcr_min", 2.5))
    shift_min = float(c.get("shift_min", 480))
    qs = int(float(pt.get("cavities", 2)) * shift_min // (cure + alw))
    if qs <= 0:
        return None
    return qs, shift_min / qs


def _derive_shift_and_plant_day(ts: pd.Timestamp):
    """Plant convention:
        A = 07:00-15:00 (plant-day = calendar date)
        B = 15:00-23:00 (plant-day = calendar date)
        C = 23:00-07:00 (plant-day = the date when 23:00 fell)

    A timestamp like 03:00 AM belongs to the PREVIOUS calendar day's C shift.
    """
    if pd.isna(ts):
        return "", pd.NaT
    h = ts.hour
    d = ts.normalize().date()
    if 7 <= h < 15:
        return "A", d
    if 15 <= h < 23:
        return "B", d
    if h >= 23:
        return "C", d
    # 00:00 - 06:59
    prev = (ts - pd.Timedelta(days=1)).normalize().date()
    return "C", prev


# ------------------------------------------------------------------ loaders
def _load_raw_plan(cfg) -> pd.DataFrame:
    """The curing plan, normalised but NOT filtered.

    Split out of load_original_plan() so that the rows the scheduler drops
    (changeover / mould-clean / zero-qty) are still REACHABLE -- they carry the
    reserved press time that FIX F7 is about, and the un-filtered endTime max is
    the horizon that FIX F3 is about. Nothing here changes what the scheduler
    sees; it only stops the dropped rows from being unrecoverable.
    """
    from db_loader import load_plan
    plan = load_plan(cfg).copy()
    # Normalise expected column names (DB <-> code aliases)
    rename = {
        "PlanId": "plan_id", "planId": "plan_id",
        "SKUCode": "skuCode", "sku_code": "skuCode",
        "PressNo": "pressNo", "press_no": "pressNo",
        "CycleTime": "cycleTime", "cycle_time": "cycleTime",
    }
    plan = plan.rename(columns={k: v for k, v in rename.items() if k in plan.columns})
    # Ensure all expected columns exist
    for c in ("plan_id", "skuCode", "date", "pressNo", "shift",
              "startTime", "endTime", "qty", "cycleTime", "remarks"):
        if c not in plan.columns:
            plan[c] = pd.NA
    # Parse timestamps
    plan["startTime"] = pd.to_datetime(plan["startTime"], errors="coerce")
    plan["endTime"]   = pd.to_datetime(plan["endTime"],   errors="coerce")
    plan["qty"]       = pd.to_numeric(plan["qty"], errors="coerce").fillna(0).astype(int)
    plan["skuCode"]   = plan["skuCode"].astype(str).str.strip()
    plan["pressNo"]   = plan["pressNo"].astype(str).str.strip()
    return plan.reset_index(drop=True)


def load_original_plan(cfg):
    """Return the original curing plan as a DataFrame with normalised cols.

    UNCHANGED BEHAVIOUR: the changeover / zero-qty filter below is exactly the
    filter that was here before. What used to be lost is now merely also
    available via split_blocked_windows() -- see FIX F7.
    """
    plan = _load_raw_plan(cfg)
    # Drop changeover / zero-qty rows
    plan = plan[(plan["skuCode"].str.upper() != "CHANGEOVER") & (plan["qty"] > 0)]
    plan = plan.reset_index(drop=True)
    return plan


# ============================================================================
# FIX F7 -- THE MOULD-CHANGE WINDOWS ARE DELETED AND NEVER PUT BACK
# ============================================================================
# WAS: load_original_plan() dropped every row with skuCode == "CHANGEOVER" or
#      qty == 0 and then FORGOT ABOUT THEM. On plan.csv that is 105 rows:
#         56  CHANGEOVER               (C/O from <sku> to <sku>)
#         48  MOULD_CLEAN              ("Mould cleaning (continuity)")
#          1  a real SKU with qty 0
#      reserving 356.05 HOURS of press time.
#
# WHY IT IS A DEFECT, NOT A POLICY. Those rows are not noise -- they are the
# ONLY record of press time the upstream LP deliberately RESERVED. Dropping them
# from the *scheduling set* is right (you do not "produce" a mould change).
# Dropping them from the *press calendar* is wrong, and it is actively harmful in
# exactly the case phase 6 exists to handle: when a block is delayed, `prev_end`
# binds and the block starts at EXACTLY the previous block's end -- i.e. INSIDE
# the mould-change window that the LP reserved between them. The press cannot do
# both. The schedule silently promises press time that does not exist.
#
# WHAT THIS BUYS. Two things, and the first one is unconditional:
#   ALWAYS  : the consumption is MEASURED and REPORTED (never silent again).
#   OPTIONAL: cfg `reinsert_changeover_blocks` re-inserts them as PINNED BUSY
#             windows, so a delayed block cannot run through one. That makes the
#             schedule LONGER -- and TRUE. Default False: turning it on is a
#             planning decision, not a bug fix, so it is the user's to make.
# ============================================================================
def split_blocked_windows(raw_plan: pd.DataFrame) -> pd.DataFrame:
    """Rows the scheduler drops that nevertheless RESERVE press time.

    Returns pressNo / startTime / endTime / kind / hours, one row per window.
    """
    if raw_plan is None or raw_plan.empty:
        return pd.DataFrame(columns=["pressNo", "startTime", "endTime", "kind", "hours"])

    sku_up = raw_plan["skuCode"].astype(str).str.upper().str.strip()
    dropped = raw_plan[(sku_up == "CHANGEOVER") | (raw_plan["qty"] <= 0)].copy()
    if dropped.empty:
        return pd.DataFrame(columns=["pressNo", "startTime", "endTime", "kind", "hours"])

    dropped = dropped.dropna(subset=["startTime", "endTime"])
    dur = (dropped["endTime"] - dropped["startTime"]).dt.total_seconds() / 3600.0
    dropped = dropped[dur > 0]
    if dropped.empty:
        return pd.DataFrame(columns=["pressNo", "startTime", "endTime", "kind", "hours"])

    up = dropped["skuCode"].astype(str).str.upper().str.strip()
    kind = pd.Series("ZERO_QTY", index=dropped.index, dtype="object")
    kind[up == "CHANGEOVER"] = "CHANGEOVER"
    kind[up.isin(("MOULD_CLEAN", "MOULD CLEAN"))] = "MOULD_CLEAN"

    out = pd.DataFrame({
        "pressNo":   dropped["pressNo"].astype(str).str.strip(),
        "startTime": dropped["startTime"],
        "endTime":   dropped["endTime"],
        "kind":      kind.values,
    })
    out["hours"] = (out["endTime"] - out["startTime"]).dt.total_seconds() / 3600.0
    return out.sort_values(["pressNo", "startTime"], kind="mergesort").reset_index(drop=True)


def _index_blocked(blocked: pd.DataFrame) -> dict:
    """pressNo -> sorted list of (start_ts, end_ts) busy windows."""
    idx = {}
    if blocked is None or blocked.empty:
        return idx
    for press, g in blocked.groupby("pressNo"):
        g = g.sort_values("startTime", kind="mergesort")
        idx[str(press)] = [
            (pd.Timestamp(a), pd.Timestamp(b))
            for a, b in zip(g["startTime"], g["endTime"])
        ]
    return idx


def _clear_blocked(start: pd.Timestamp, duration, windows) -> pd.Timestamp:
    """Earliest t >= `start` such that [t, t+duration) hits NO pinned window.

    A "busy window" that a job may run straight THROUGH is not busy, so this
    enforces non-overlap, not merely non-containment of the start instant.
    Windows are pinned at their ORIGINAL plan times -- we never move a mould
    change, we only refuse to schedule production on top of one.
    """
    if not windows or pd.isna(start):
        return start
    t = start
    moved = True
    guard = 0
    while moved and guard < 10_000:
        moved = False
        guard += 1
        for w0, w1 in windows:
            # overlap iff  t < w1  and  t + duration > w0
            if t < w1 and (t + duration) > w0:
                t = w1
                moved = True
    return t


def _earliest_free_slot(intervals, start, duration):
    """FIX B (2026-07-27) -- earliest t >= `start` whose [t, t+duration) overlaps NONE of
    `intervals` (a press's already-placed (s,e) blocks + any pinned busy windows). Same
    overlap predicate as _clear_blocked. Used by `press_gap_fill`: a kit-late block takes a
    HOLE on its press instead of the queue tail, so it never drags later kit-ready blocks
    (the scalar-prev_end head-of-line amplifier -- see PHASE6_CURING_TAIL_BUG.md)."""
    if not intervals or pd.isna(start):
        return start
    t, moved, guard = start, True, 0
    while moved and guard < 100_000:
        moved = False
        guard += 1
        for s, e in intervals:
            if t < e and (t + duration) > s:        # overlap
                t = e
                moved = True
    return t


def _insert_interval(intervals, s, e):
    """FIX B -- record a placed (start,end) on a press. Order-agnostic (the free-slot scan
    re-checks every interval), so a plain append is correct and O(1)."""
    intervals.append((s, e))


# ============================================================================
# FIX F3 -- PAST_HORIZON WAS NEVER REPORTED
# ============================================================================
# config.yaml:55 says, in its OWN words:
#     "enforce_strict_horizon: a lot finishing past planEndDate is flagged
#      PAST_HORIZON (still placed)."
# phase5:1015 honours that exactly -- reads the key, derives the horizon from the
# plan's max endTime, flags, and STILL PLACES. Phase 6 ignored it completely: the
# word "horizon" did not appear once in this file. Phase 6 is the phase that
# MOVES BLOCKS LATER, so it is the phase most likely to push work past the end of
# the plan -- and it was the one phase that never looked.
#
# WHY IT IS A DEFECT, NOT A POLICY: config declares a behaviour, a sibling phase
# implements that behaviour, and this phase silently opts out. That is an
# omission, not a design choice.
#
# REPORT ONLY -- deliberately. A hard stop would be a NEW RULE and would
# contradict config's own "(still placed)". We flag and we count. Nothing moves.
# ============================================================================
def plan_horizon(raw_plan: pd.DataFrame):
    """The plan horizon = max endTime over the RAW plan (phase5:980-991 does the
    same thing from the same source, so the two phases cannot disagree)."""
    if raw_plan is None or raw_plan.empty or "endTime" not in raw_plan.columns:
        return None
    et = pd.to_datetime(raw_plan["endTime"], errors="coerce")
    if not et.notna().any():
        return None
    h = et.max()
    return None if pd.isna(h) else pd.Timestamp(h)


def _select_building_subset(df: pd.DataFrame) -> pd.DataFrame:
    """Pick the rows that represent Building completion (the lots whose
    finish makes a kit available to curing). Strategy:
      1. Prefer rows whose process matches Stage-2 / Green Tyre Building.
      2. Else fall back to any BUILDING department row.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    # Try every plausible column name we have seen produced by phase 5
    proc_col = next((c for c in ("process", "operation", "Process", "Operation") if c in df.columns), None)
    dept_col = next((c for c in ("department", "Department", "dept") if c in df.columns), None)

    candidate = df.copy()
    if proc_col is not None:
        proc_up = candidate[proc_col].astype(str).str.upper().fillna("")
        # Preferred: explicit Stage-2 / Green Tyre Building tokens
        s2 = candidate[proc_up.apply(lambda s: any(tok in s for tok in STAGE2_TOKENS))]
        if not s2.empty:
            return s2
        # Else: any building token
        any_b = candidate[proc_up.apply(lambda s: any(tok in s for tok in BUILDING_PROCESS_TOKENS))]
        if not any_b.empty:
            return any_b

    if dept_col is not None:
        dept_up = candidate[dept_col].astype(str).str.upper().fillna("")
        b = candidate[dept_up.str.contains("BUILDING", na=False)]
        if not b.empty:
            return b

    return pd.DataFrame()


def load_building_schedule(cfg):
    """Return a DataFrame of Building lots (one row per lot) with columns:
       skuCode, scheduled_finish (timestamp), qty (int).
    Pulls from MySQL jkt_floor_endfwd_schedule first, falls back to
    outputs2/phase5_schedule_updated.csv if DB has no rows.
    """
    df = pd.DataFrame()

    # -------- DB attempt --------
    try:
        from db_loader import get_engine, _resolve_source, OUTPUT_TABLES
        if _resolve_source(cfg) == "db":
            eng = get_engine(cfg)
            tbl = OUTPUT_TABLES["floor_schedule"]
            df = pd.read_sql(f"SELECT * FROM {tbl}", eng)
            print(f"  Building schedule from DB  {tbl}: {len(df):,} rows")
    except Exception as e:
        print(f"  [WARN] DB load failed: {e}")

    # -------- File fallback --------
    # v2 building schedule keys lots by GT CODE (item), not the tyre SKU, and only
    # the FLOOR schedule carries produce_qty + process. Prefer the floor schedule.
    if df is None or df.empty:
        for nm in ("phase5_floor_schedule_updated.csv", "phase5_schedule_updated.csv"):
            p = OUTPUTS / nm
            if p.exists():
                df = pd.read_csv(p, low_memory=False)
                print(f"  Building schedule from file {nm}: {len(df):,} rows")
                break

    if df is None or df.empty:
        return pd.DataFrame(columns=["skuCode", "scheduled_finish", "qty"])

    # FIX F6 (read side): the floor-schedule file we just read may ALREADY carry
    # curing rows appended by a previous phase-6 run. They are not building rows
    # and _select_building_subset would not pick them up anyway -- but be explicit,
    # so a stale file can never leak curing lots into the kit pool.
    df = _drop_curing_rows(df)

    # Filter to Building rows
    df = _select_building_subset(df)
    if df.empty:
        print("  [WARN] no Building rows found in the finalised schedule")
        return pd.DataFrame(columns=["skuCode", "scheduled_finish", "qty"])

    # Normalise column names
    rename = {}
    if "scheduled_finish" not in df.columns:
        for alt in ("endTime", "scheduledFinish", "end_time", "finish_time"):
            if alt in df.columns:
                rename[alt] = "scheduled_finish"
                break
    if "qty" not in df.columns:
        for alt in ("produce_qty", "lot_qty", "Qty", "quantity"):
            if alt in df.columns:
                rename[alt] = "qty"
                break
    if "skuCode" not in df.columns:
        for alt in ("sku", "SKU", "SKUCode", "sku_code", "FG_SKU_CODE"):
            if alt in df.columns:
                rename[alt] = "skuCode"
                break
    df = df.rename(columns=rename)

    # === v2 ADAPTER: map GT-code (item) -> tyre SKU ===
    # v2's building lots carry only `item` (the GT code) and no tyre SKU. Phase 6
    # FIFO-matches curing jobs by tyre SKU, so map item -> skuCode via the demand
    # explosion's GT-code -> sku link (item_code starting "GT"/"CAR" -> sku).
    need_sku = ("skuCode" not in df.columns) or df.get("skuCode").isna().all() or \
               (df.get("skuCode").astype(str).str.strip() == "").all()
    if need_sku and "lot_id" in df.columns:
        # Reliable 1:1 map: building lot_id -> tyre SKU via phase-2 lots' skus_set.
        lot2sku = {}
        for ln in ("phase2_lots_updated.csv", "phase2_lots.csv"):
            lp = OUTPUTS / ln
            if lp.exists():
                _cols = ["lot_id", "skus_set"]
                try:
                    _hdr = pd.read_csv(lp, nrows=0).columns
                    if "item_type" in _hdr:
                        _cols.append("item_type")
                except Exception:
                    pass
                _L = pd.read_csv(lp, usecols=_cols, low_memory=False)
                _L["lot_id"] = _L["lot_id"].astype(str).str.strip()
                # M-1 GUARD (2026-07-27): this map credits a whole BUILDING (green-tyre) lot's kit to
                # skus_set[0]. Correct only while every BUILDING lot is single-SKU (verified: 0 of 951
                # GT lots are multi-SKU; the multi-SKU lots are COMPONENTS, which phase6 never looks
                # up here). If a pooled multi-SKU GREEN-TYRE lot ever appears, its other SKUs would
                # silently read SHORT -> flag it LOUDLY. (Only GT lots are checked, to avoid a false
                # alarm on component lots that carry many SKUs by design.)
                if "item_type" in _L.columns:
                    _gtL = _L[_L["item_type"].astype(str).str.contains("Green", case=False, na=False)]
                    _nsku = _gtL["skus_set"].astype(str).str.split(",").map(
                        lambda xs: sum(1 for s in xs if str(s).strip()))
                    _multi = int((_nsku > 1).sum())
                    if _multi:
                        print(f"  [v2-adapter] !! WARNING (M-1): {_multi} GREEN-TYRE lot(s) serve >1 "
                              f"SKU; kit is credited to skus_set[0] ONLY -- the other SKUs will read "
                              f"SHORT/phantom. Split the lot qty by per-SKU demand, or make skus_set 1:1.")
                _L["sku1"] = (_L["skus_set"].astype(str).str.split(",").str[0].str.strip())
                lot2sku = dict(zip(_L["lot_id"], _L["sku1"]))
                break
        df["skuCode"] = df["lot_id"].astype(str).str.strip().map(lot2sku)
        print(f"  [v2-adapter] mapped {df['skuCode'].notna().sum():,}/{len(df):,} "
              f"building lots to tyre SKUs via lot_id->skus_set")
    # BUILDING-PLAN ECHO fallback: the echo's synthetic rows (lot_id BLD*) are NOT phase2 lots, so
    # lot_id->skus_set leaves them blank. Their `item` IS the phase2 GT code -> map it to the tyre
    # SKU via the same GT-code map the demand explosion uses. (No-op when no rows need it.)
    if "item" in df.columns:
        _need = df["skuCode"].isna() if "skuCode" in df.columns else pd.Series(True, index=df.index)
        if _need.any():
            try:
                _gt2sku = load_gt_code_to_sku(cfg)
                if "skuCode" not in df.columns:
                    df["skuCode"] = pd.NA
                df.loc[_need, "skuCode"] = (df.loc[_need, "item"].astype(str).str.strip()
                                            .map(_gt2sku))
                print(f"  [echo] mapped {int(_need.sum()):,} building rows item(GT)->SKU via gt2sku "
                      f"({int(df.loc[_need, 'skuCode'].notna().sum()):,} resolved)")
            except Exception as _e:
                print(f"  [echo] gt2sku fallback failed: {_e}")
    if "skuCode" not in df.columns or "scheduled_finish" not in df.columns:
        print("  [WARN] building schedule missing skuCode or finish col; skipping")
        return pd.DataFrame(columns=["skuCode", "scheduled_finish", "qty"])
    if "qty" not in df.columns:
        df["qty"] = 0

    # S6 -- STREAMING: carry the segment START so the kit pool knows tyres appear
    # LINEARLY across a building segment, not all at its finish. Falls back to the
    # finish when no start column exists (then a segment is an instant, = v6).
    _st = None
    for _alt in ("start_time", "startTime", "scheduled_start", "begin_time"):
        if _alt in df.columns:
            _st = pd.to_datetime(df[_alt], errors="coerce")
            break
    _fin = pd.to_datetime(df["scheduled_finish"], errors="coerce")
    if _st is None:
        _st = _fin
    _st = _st.fillna(_fin)

    _gtk = (df["item"].map(_norm_code) if "item" in df.columns
            else pd.Series("", index=df.index))           # SHARED-GT FIX: pool key = GT code
    out = pd.DataFrame({
        "skuCode": df["skuCode"].astype(str).str.strip(),
        "gtKey": _gtk,
        "scheduled_start": _st,
        "scheduled_finish": _fin,
        "qty": pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int),
        "source": "BUILT",
    }).dropna(subset=["scheduled_finish"])
    out = out[(out["skuCode"].str.upper() != "CHANGEOVER") & (out["qty"] >= 0)]
    return out


# ============================================================================
# FIX F5 -- THE KIT POOL NEVER SAW THE OPENING STOCK
# ============================================================================
# WAS: phase 6 built KitPool ONLY from phase 5's building lots. It never opened
#      inputs/inventory.csv. Not once -- the word "inventory" did not appear in
#      this file.
#
# WHY IT IS A DEFECT, NOT A POLICY -- AND WHY THE "SHORTFALL" IS NOT A BUG
# ELSEWHERE. phase1c does its job CORRECTLY: it nets green-tyre demand against
# opening WIP (gross 333,196 -> net 329,069; netted = 4,127.00 exactly). So
# BUILDING IS *SUPPOSED* TO MAKE 4,127 FEWER TYRES than the press plan consumes
# -- those 4,127 are already sitting on the floor. Phase 6, blind to the floor,
# sees building come up 4,127 short, finds no kit... and cures them anyway
# (see FIX F2). The shortfall is an ARTEFACT OF PHASE 6's BLINDNESS, not a
# netting error. Fixing phase1c would be fixing the wrong thing.
#
# THE JOIN. inventory.csv's itemcode is a shop-floor DESCRIPTION ("GT  T1457
# STAR" -- DOUBLE SPACE); the press plan speaks SAP material codes. The BOM is
# the bridge: a bom row whose child_description is "Green Tyres" maps
#     child (GT code)  ->  Super_parent (the SKU).
# EXACT MATCH ONLY after collapsing internal whitespace. NO FUZZY MATCHING --
# a code either matches or it is reported unmatched. See _norm_code().
#
# THE AGE CHECK -- THE PART THAT MUST NOT BE SKIPPED. inventory.csv carries a
# produced_time on EVERY row. 727 of the 6,139 opening green tyres are ALREADY
# past their 24 h shelf life at plan start. Seeding those would be inventing
# 727 tyres that the plant must scrap. WE DO NOT SEED EXPIRED TYRES.
#
# THE FIFO TIMESTAMP is the tyre's own produced_time, not plan-start. Two reasons,
# both of which make the model TRUER and NEITHER of which changes any timing:
#   (a) it puts the opening stock FIRST in the chronological pool -- which is what
#       "consume them chronologically" means, and what phase1c already assumed;
#   (b) it makes the F4 age-at-press audit HONEST (a tyre built 20 h before the
#       plan opened is 20 h old when the plan opens, not 0 h old).
# Timing is untouched because every seed timestamp is <= plan start <= every
# orig_start, so `kit_ready > candidate_start` can never fire on a seed lot.
# ============================================================================
def load_gt_code_to_sku(cfg) -> dict:
    """{normalised GT code -> SKU} from the BOM's Green Tyres rows.

    A GT code that maps to MORE THAN ONE SKU is AMBIGUOUS and is NOT mapped.
    Picking one of them would be inventing an allocation the data does not
    state. (On the current BOM this is 2 codes, and neither holds any opening
    stock, so nothing is lost -- but the rule must hold regardless.)
    """
    bom_path = INPUTS / str((cfg.get("files") or {}).get("bom", "bom.csv"))
    if not bom_path.exists():
        print(f"  [WARN] F5: BOM not found at {bom_path}; cannot seed kit pool")
        return {}
    try:
        bom = pd.read_csv(bom_path, low_memory=False)
    except Exception as e:
        print(f"  [WARN] F5: BOM unreadable ({e}); cannot seed kit pool")
        return {}
    if not {"child", "child_description", "Super_parent"} <= set(bom.columns):
        print("  [WARN] F5: BOM lacks child/child_description/Super_parent; cannot seed")
        return {}

    # child_description IS the ItemType. "Green Tyres" is the green-tyre type.
    gt = bom[bom["child_description"].astype(str).str.strip().str.upper() == "GREEN TYRES"]
    multi = {}
    for c, sp in zip(gt["child"], gt["Super_parent"]):
        multi.setdefault(_norm_code(c), set()).add(str(sp).strip())
    ambiguous = [k for k, v in multi.items() if len(v) > 1]
    if ambiguous:
        print(f"  [F5] {len(ambiguous)} GT code(s) map to >1 SKU -- NOT mapped "
              f"(ambiguous, would have to be invented): {ambiguous[:3]}")
    return {k: next(iter(v)) for k, v in multi.items() if len(v) == 1}


def load_sku_to_gt_code(cfg) -> tuple:
    """SHARED-GT FIX (2026-09-26): ({SKU -> normalised GT code}, {every BOM GT code}).

    Unlike load_gt_code_to_sku() this direction is never ambiguous: a tyre SKU has ONE green
    tyre. A GT code shared by several SKUs is kept (it is ONE physical green tyre that any of
    them can cure), which is exactly what the GT-keyed kit pool needs."""
    bom_path = INPUTS / str((cfg.get("files") or {}).get("bom", "bom.csv"))
    try:
        bom = pd.read_csv(bom_path, low_memory=False, usecols=["Super_parent", "child",
                                                               "child_description"])
    except Exception as e:
        print(f"  [WARN] shared-GT map: BOM unreadable ({e})")
        return {}, set()
    gt = bom[bom["child_description"].astype(str).str.strip().str.upper() == "GREEN TYRES"]
    sku2gt = {}
    for sp, c in zip(gt["Super_parent"].astype(str).str.strip(), gt["child"]):
        sku2gt.setdefault(sp, _norm_code(c))
    return sku2gt, set(sku2gt.values())


def green_tyre_max_age_h(cfg, gt2sku: dict) -> tuple:
    """(default_max_age_h, {sku -> max_age_h}) for green tyres.

    SOURCE OF TRUTH ORDER:
      1. inputs/aging_master.csv -- the PER-ITEM master, keyed by the GT code.
         222 of the 224 mappable GT codes have a row there (all say 24 h).
      2. config aging_fallback_by_itemtype["Green Tyres"]["max_h"].
      3. cfg["green_tyre_max_age_h"], default 24.0.
    Never invented: if none of the three answer, 24.0 is the documented plant
    figure (aging_table_pcr.png, PCR column: GREEN_TYRE 15 min / 1 day).
    """
    default = float(cfg.get("green_tyre_max_age_h", 0) or 0)
    # USER-DIRECTED override (2026-07-16): when green_tyre_age_override_master is ON,
    # the config value is THE green-tyre limit for ALL SKUs, superseding the plant
    # aging_master.csv (24h) below. The master FILE is left untouched. OPERATING value,
    # NOT plant-confirmed (plant data = 24h). OFF => byte-identical (master wins).
    if bool(cfg.get("green_tyre_age_override_master")) and default > 0:
        return default, {sku: default for sku in gt2sku.values()}
    if default <= 0:
        fb = (cfg.get("aging_fallback_by_itemtype") or {}).get("Green Tyres") or {}
        default = float(fb.get("max_h", 0) or 0)
    if default <= 0:
        default = 24.0

    per_sku = {}
    am_path = INPUTS / str((cfg.get("files") or {}).get("aging_master", "aging_master.csv"))
    if am_path.exists():
        try:
            am = pd.read_csv(am_path)
            if {"ItemCode", "MaxAging"} <= set(am.columns):
                code2max = {}
                for c, mx in zip(am["ItemCode"], am["MaxAging"]):
                    v = pd.to_numeric(mx, errors="coerce")
                    # PRESENT-BUT-BLANK TRAP: a NaN MaxAging is NOT a max age.
                    # float(NaN) does not raise -- it silently poisons the compare.
                    if pd.notna(v) and float(v) > 0:
                        code2max[_norm_code(c)] = float(v)
                for gt_code, sku in gt2sku.items():
                    if gt_code in code2max:
                        per_sku[sku] = code2max[gt_code]
        except Exception as e:
            print(f"  [WARN] F5/F4: aging master unreadable ({e}); using {default:.1f} h")
    return default, per_sku


def load_inventory_seed(cfg, plan_start, gt2sku: dict, default_max_age_h: float,
                        per_sku_max_age: dict, all_gt_codes: set | None = None):
    """Opening GREEN TYRE WIP as kit-pool lots. Age-checked; expired NOT seeded.

    Returns (seed, gt):
      seed -- the same schema as load_building_schedule(): skuCode /
              scheduled_finish / qty, plus source == "WIP".
      gt   -- EVERY opening green-tyre row, classified, so the caller can report
              what was left out instead of only what was let in. Columns:
              itemcode / _code / skuCode / qty / _pt / _age_h / _limit_h / _disp,
              where _disp is one of SEEDED / EXPIRED / NO_TIMESTAMP / UNMATCHED.
              (E8 + B2 both need this; neither may re-derive it independently.)
    """
    empty = pd.DataFrame(columns=["skuCode", "scheduled_finish", "qty", "source"])
    empty_gt = pd.DataFrame(columns=["itemcode", "_code", "skuCode", "qty", "_pt",
                                     "_age_h", "_limit_h", "_disp"])
    inv_path = INPUTS / str((cfg.get("files") or {}).get("inventory", "inventory.csv"))
    if not inv_path.exists():
        print(f"  [WARN] F5: inventory not found at {inv_path}; kit pool NOT seeded")
        return empty, empty_gt
    try:
        inv = pd.read_csv(inv_path, low_memory=False)
    except Exception as e:
        print(f"  [WARN] F5: inventory unreadable ({e}); kit pool NOT seeded")
        return empty, empty_gt
    if "itemcode" not in inv.columns or "inventory" not in inv.columns:
        print("  [WARN] F5: inventory lacks itemcode/inventory columns; kit pool NOT seeded")
        return empty, empty_gt

    # GREEN_TYPE / "GREEN TYRE" / "Green Tyres" -- collapse the spelling, then EXACT match.
    it = inv.get("item_type", pd.Series("", index=inv.index)).astype(str)
    it_key = it.str.upper().str.replace(r"[\s_]+", "", regex=True)
    gt = inv[it_key.isin(("GREENTYRE", "GREENTYRES"))].copy()
    if gt.empty:
        print("  [WARN] F5: no GREEN_TYRE rows in inventory; kit pool NOT seeded")
        return empty, empty_gt

    gt["qty"] = pd.to_numeric(gt["inventory"], errors="coerce").fillna(0.0)
    gt = gt[gt["qty"] > 0].copy()
    gt["_code"] = gt["itemcode"].map(_norm_code)
    gt["skuCode"] = gt["_code"].map(gt2sku)

    # ---- AGE CHECK. produced_time is on every row; use it. ----
    if "produced_time" in gt.columns:
        gt["_pt"] = pd.to_datetime(gt["produced_time"], errors="coerce", format="mixed")
    else:
        gt["_pt"] = pd.NaT
    gt["_limit_h"] = gt["skuCode"].map(per_sku_max_age).astype(float) \
                       .fillna(float(default_max_age_h))
    gt["_age_h"] = (pd.Timestamp(plan_start) - gt["_pt"]).dt.total_seconds() / 3600.0

    # ---- DISPOSITION. One row, one verdict, and every verdict is reportable.
    #      A row with no produced_time has an UNKNOWABLE age. We do not guess it and
    #      we do not seed it -- an unknown age is not the same as a fresh tyre.
    # SHARED-GT FIX: with a GT-keyed pool, a GT code shared by several SKUs is a real match
    # (skuCode stays blank for it -- the SKU is decided when a press draws the tyre).
    _known = (gt["_code"].isin(all_gt_codes) if all_gt_codes else gt["skuCode"].notna())
    gt["_disp"] = "SEEDED"
    gt.loc[~_known,                         "_disp"] = "UNMATCHED"
    gt.loc[_known & gt["_pt"].isna(),       "_disp"] = "NO_TIMESTAMP"
    gt.loc[_known & gt["_pt"].notna()
           & (gt["_age_h"] > gt["_limit_h"]), "_disp"] = "EXPIRED"

    # ---- FIX A (2026-07-27) -- FUTURE-DATED WIP GUARD. See PHASE6_CURING_TAIL_BUG.md.
    #      Opening WIP is physically on the floor at t0. A produced_time AFTER plan_start
    #      (typically a WRONG-MONTH inventory snapshot) gives NEGATIVE age, dodges the
    #      EXPIRED check, and would be seeded as a kit "available in the future" -- which
    #      strands curing (a day-60 kit sets prev_end and head-of-line-blocks a whole press).
    #      We FLAG it loudly here and (default) CLAMP its seed finish to plan_start below.
    #      No-op when every produced_time <= plan_start (the shipped June run) -> byte-identical.
    gt["_future"] = (_known & gt["_pt"].notna()
                     & (gt["_pt"] > pd.Timestamp(plan_start)))

    n_rows, tot_qty = len(gt), float(gt["qty"].sum())
    def _q(d):
        return len(d), float(d["qty"].sum())
    matched   = gt[_known]
    unmatched = gt[gt["_disp"] == "UNMATCHED"]
    no_ts     = gt[gt["_disp"] == "NO_TIMESTAMP"]
    expired   = gt[gt["_disp"] == "EXPIRED"]
    fresh     = gt[gt["_disp"] == "SEEDED"]

    print(f"  [F5] opening GREEN TYRE WIP: {n_rows:,} rows / {tot_qty:,.0f} tyres")
    print(f"       matched to a SKU via BOM : {_q(matched)[0]:,} rows "
          f"({_q(matched)[1]:,.0f} tyres)")
    print(f"       UNMATCHED (no BOM link)  : {_q(unmatched)[0]:,} rows "
          f"({_q(unmatched)[1]:,.0f} tyres, {unmatched['_code'].nunique()} distinct codes) "
          f"-- reported, NOT guessed")
    if len(no_ts):
        print(f"       no produced_time         : {_q(no_ts)[0]:,} rows -- age unknowable, NOT seeded")
    print(f"       EXPIRED at plan start    : {_q(expired)[0]:,} rows "
          f"({_q(expired)[1]:,.0f} tyres) past their shelf life "
          f"[limit {default_max_age_h:.0f} h] -- NOT seeded")
    print(f"       SEEDED into kit pool     : {_q(fresh)[0]:,} rows "
          f"({_q(fresh)[1]:,.0f} tyres across {fresh['skuCode'].nunique()} SKUs)")
    # FIX A -- early, loud flag for a wrong-month / future-dated WIP snapshot.
    _fut = gt[gt["_future"]]
    if len(_fut):
        _clamp_on = bool(cfg.get("clamp_wip_seed_to_plan_start", True))
        print(f"       !! FUTURE-DATED WIP      : {_q(_fut)[0]:,} rows ({_q(_fut)[1]:,.0f} tyres) "
              f"have produced_time AFTER plan_start (newest {_fut['_pt'].max()}). Opening WIP "
              f"cannot be in the future -- almost certainly a WRONG-MONTH inventory snapshot. "
              + ("CLAMPED to plan_start (available at t0)." if _clamp_on else
                 "NOT clamped (clamp_wip_seed_to_plan_start:false) -- CURING-TAIL RISK, see "
                 "PHASE6_CURING_TAIL_BUG.md."))

    # ====================================================================
    # E8 -- (A) DEFECT. THE EXPIRED OPENING TYRES WERE INVISIBLE.
    # ====================================================================
    # They were DROPPED (correctly -- a tyre past its shelf life is not kit) and
    # then only print()ed. A planner reading the outputs would never have seen
    # them. This is REAL SCRAP EXPOSURE at t=0: on the shipped data, 627 tyres
    # that the plant is holding are already dead before the plan even opens.
    #
    # WHY IT IS A DEFECT, NOT A POLICY: the number is computed, it is material, and
    # it is thrown away. Writing it down changes no decision this phase makes -- it
    # only stops the phase from being the only thing that knows.
    # ====================================================================
    if not expired.empty:
        out = pd.DataFrame({
            "itemcode":       expired["itemcode"].astype(str),
            "itemcode_norm":  expired["_code"].astype(str),
            "skuCode":        expired["skuCode"].astype(str),
            "produced_time":  expired["_pt"],
            "plan_start":     pd.Timestamp(plan_start),
            "age_h":          expired["_age_h"].round(2),
            "shelf_life_h":   expired["_limit_h"],
            "over_by_h":      (expired["_age_h"] - expired["_limit_h"]).round(2),
            "qty":            expired["qty"],
        }).sort_values(["age_h"], ascending=False)
        p = _atomic_to_csv(out, OUTPUTS / "phase6_expired_opening_wip.csv")
        print(f"       [E8] wrote {p.name}: {len(out):,} rows / "
              f"{float(out['qty'].sum()):,.0f} tyres of opening SCRAP exposure "
              f"(oldest {float(out['age_h'].max()):,.1f} h vs a "
              f"{float(out['shelf_life_h'].iloc[0]):.0f} h shelf life)")

    if fresh.empty:
        return empty, gt
    # FIX A -- clamp future-dated seeds to plan_start (opening WIP is on the floor at t0).
    #   Default ON; no-op when every produced_time <= plan_start (byte-identical June).
    _fin = fresh["_pt"]
    if bool(cfg.get("clamp_wip_seed_to_plan_start", True)):
        _fin = _fin.clip(upper=pd.Timestamp(plan_start))
    seed = pd.DataFrame({
        "skuCode": fresh["skuCode"].astype(str).str.strip(),
        "gtKey": fresh["_code"].astype(str),
        # produced_time (clamped to plan_start if future-dated) -- see the F5 comment block.
        "scheduled_finish": _fin,
        "qty": fresh["qty"].round().astype(int),
        "source": "WIP",
    })
    return seed, gt


# ============================================================================
# B2 -- TWO PHASES READ THE SAME WIP FILE WITH DIFFERENT RULES. INVESTIGATED.
# ============================================================================
# THE SYMPTOM (as handed to me):
#     phase1c NETS   4,127 green tyres out of BUILDING demand (333,196 -> 329,069)
#     phase6/F5 SEEDS 4,252 green tyres INTO the KIT POOL
#     Difference: 125 tyres -- "counted TWICE. Phantom kit -- and it flatters the
#     schedule."
#
# I MEASURED IT, PER GT CODE, FROM THE DATA. THE SYMPTOM IS REAL. THE DIAGNOSIS IS
# NOT. The 125 is not a double count. It is the NET of TWO OPPOSITE ERRORS, and
# only SIX tyres of it are an actual double count:
#
#   (a) SEEDED but NOT NETTED = +566 tyres, on 7 GT codes. Splits cleanly in two:
#
#       - 560 tyres on 6 GT codes that have **ZERO green-tyre demand** in phase1b's
#         explosion (GT 2568 HT2, GT 1865 ROYL RENO, GT 2617 RAN XAT, GT 1482 UHL,
#         GT 1684 ROYL, GT 2258 RAN HPE). Building never makes them -- so phase1c
#         had NOTHING TO NET. These are the DECLARED HOLE: SKUs the press plan cures
#         but the routing cannot build. The stock is REAL and it is on the floor.
#         NOT double-counted. Seeding it is the only thing standing between those
#         SKUs and a 100%-phantom kit.
#
#       -   6 tyres on ONE GT code -- and this is the whole double count:
#             inventory.csv writes it  "GT  T1457 STAR"   <- DOUBLE SPACE
#             the BOM and the demand write it "GT T1457 STAR"  (single)
#         phase1c matches wip_by_item on the itemcode RAW (strip only, no whitespace
#         collapse), so it never finds this item and nets 0 of it -- even though the
#         item HAS 10,378 tyres of gross demand. phase6/F5 collapses the whitespace
#         (_norm_code) and does match it. Result: building makes all 6, AND F5 seeds
#         all 6. ** THOSE 6 TYRES ARE THE ONLY GENUINELY DOUBLE-COUNTED TYRES. **
#         The fix belongs in phase1c (normalise the itemcode before the WIP join),
#         NOT here. I have not touched it. I have named it.
#
#   (b) NETTED but NOT SEEDED = -441 tyres, on 17 GT codes.
#       THIS IS THE OPPOSITE OF A PHANTOM AND IT IS THE ONE THAT SHOULD WORRY THE
#       PLANT. phase1c has NO age check. It told building to make 441 FEWER green
#       tyres on the strength of 441 tyres that are ALREADY PAST their 24 h shelf
#       life when the plan opens. Those 441 are SCRAP (they are in
#       phase6_expired_opening_wip.csv). F5 correctly refuses to seed them. So the
#       plan is 441 real green tyres SHORT, and it does not flatter anything -- it
#       silently under-builds.
#
#   NET:  +566 - 441 = +125.   The headline number is a coincidence of two errors
#   pointing opposite ways. "Making F5 consume exactly what phase1c netted" would
#   have FORCED F5 to seed 441 expired tyres -- i.e. it would have invented 441
#   scrap tyres as usable kit, and hidden the only 6 that are actually wrong.
#   I DID NOT DO IT. I REPORT IT, EVERY RUN, FROM THE DATA.
#
# NOTHING HERE IS INVENTED. Every number below is read off phase1b/phase1c's own
# output (the gross and net demand files) and inventory.csv. If those files are not
# on disk, this says SO -- it does not guess.
# ============================================================================
def _read_demand_qty(stem: str):
    """phase1b/phase1c demand file -> {normalised item_code: qty}. None if absent."""
    # STALE-FILE FIX (2026-09-26): newest of the .gz/.csv twins (a stale June .gz printed -248,513)
    _c = sorted([OUTPUTS / f"{stem}.csv.gz", OUTPUTS / f"{stem}.csv"],
                key=lambda q: q.stat().st_mtime if q.exists() else 0, reverse=True)
    for p in _c:
        nm = p.name
        if p.exists():
            try:
                d = pd.read_csv(p, usecols=["item_code", "demand_qty"], low_memory=False)
            except Exception as e:
                print(f"  [B2] {nm} unreadable ({e})")
                return None
            d["_k"] = d["item_code"].map(_norm_code)
            return d.groupby("_k")["demand_qty"].sum()
    return None


def reconcile_wip_with_phase1c(gt_all: pd.DataFrame):
    """B2. Compare, PER GT CODE, what phase1c netted out of building against what
    phase6/F5 seeded into the kit pool. Both read inputs/inventory.csv. They
    disagree. Say exactly how, and exactly why. Invent nothing.
    """
    print("\n  " + "!" * 72)
    print("  !! B2 -- WIP RECONCILIATION: phase1c vs phase6/F5 (SAME inventory.csv, "
          "DIFFERENT rules)")
    if gt_all is None or gt_all.empty:
        print("  !!   opening WIP is not loaded (seed_kitpool_from_inventory=False) -- "
              "phase1c still")
        print("  !!   nets against it, so BUILDING is short by whatever phase1c netted "
              "and phase6")
        print("  !!   cannot see any of it. That gap is the KIT SHORTFALL below.")
        print("  " + "!" * 72)
        return

    gross = _read_demand_qty("phase1_demand_updated")
    net   = _read_demand_qty("phase1_demand_NET_updated")
    if gross is None or net is None:
        print("  !!   CANNOT RECONCILE: phase1b/phase1c demand outputs are not on disk.")
        print("  !!   The double-count is NOT measured this run and is NOT claimed to be zero.")
        print("  " + "!" * 72)
        return

    g = gt_all.copy()
    codes = sorted(set(g["_code"].dropna().astype(str)))
    seeded = g[g["_disp"] == "SEEDED"].groupby("_code")["qty"].sum()
    expd   = g[g["_disp"] == "EXPIRED"].groupby("_code")["qty"].sum()
    nots   = g[g["_disp"] == "NO_TIMESTAMP"].groupby("_code")["qty"].sum()
    unm    = g[g["_disp"] == "UNMATCHED"].groupby("_code")["qty"].sum()

    t = pd.DataFrame(index=pd.Index(codes, name="gt_code"))
    t["inventory"]    = g.groupby("_code")["qty"].sum().reindex(codes).fillna(0.0)
    t["f5_seeded"]    = seeded.reindex(codes).fillna(0.0)
    t["f5_expired"]   = expd.reindex(codes).fillna(0.0)
    t["f5_no_ts"]     = nots.reindex(codes).fillna(0.0)
    t["f5_unmatched"] = unm.reindex(codes).fillna(0.0)
    t["gross_demand"] = gross.reindex(codes).fillna(0.0)
    t["p1c_netted"]   = (gross.reindex(codes).fillna(0.0)
                         - net.reindex(codes).fillna(0.0)).clip(lower=0.0)
    t["seed_minus_netted"] = t["f5_seeded"] - t["p1c_netted"]

    tot_seed = float(t["f5_seeded"].sum())
    tot_net  = float(t["p1c_netted"].sum())
    print(f"  !!   phase1c NETTED out of BUILDING : {tot_net:>9,.0f} tyres  "
          f"({int((t['p1c_netted'] > 0.5).sum())} GT codes)   [no age check, no "
          f"whitespace collapse]")
    print(f"  !!   phase6/F5 SEEDED into KIT POOL : {tot_seed:>9,.0f} tyres  "
          f"({int((t['f5_seeded'] > 0.5).sum())} GT codes)   [age-checked, "
          f"whitespace-collapsed]")
    print(f"  !!   DIFFERENCE                     : {tot_seed - tot_net:>+9,.0f} tyres")
    print("  !!")

    # ---- (a) SEEDED but NOT NETTED ------------------------------------------
    a = t[t["seed_minus_netted"] > 0.5]
    a_real = a[a["gross_demand"] <= 0.5]     # nothing to net: building never makes it
    a_dbl  = a[a["gross_demand"] > 0.5]      # building DOES make it -> counted twice
    print(f"  !!   (a) SEEDED but NOT NETTED : {float(a['seed_minus_netted'].sum()):+,.0f} "
          f"tyres on {len(a)} GT code(s)")
    print(f"  !!       {float(a_real['seed_minus_netted'].sum()):>7,.0f} on {len(a_real)} code(s) "
          f"with ZERO green-tyre demand -- building NEVER makes them, so")
    print(f"  !!               phase1c had NOTHING to net. REAL floor stock for SKUs the")
    print(f"  !!               press plan cures but the routing cannot build (the DECLARED")
    print(f"  !!               HOLE). NOT double-counted.")
    for c, r in a_real.sort_values("seed_minus_netted", ascending=False).iterrows():
        print(f"  !!                 {c:<26} seeded {r['f5_seeded']:>6,.0f}   "
              f"gross demand {r['gross_demand']:>9,.0f}")
    if a_dbl.empty:
        print(f"  !!             0 tyres are genuinely DOUBLE-COUNTED.")
    else:
        print(f"  !!       {float(a_dbl['seed_minus_netted'].sum()):>7,.0f} on {len(a_dbl)} code(s) "
              f"that DO have building demand but that phase1c did NOT net.")
        print(f"  !!               ** THESE ARE THE ONLY GENUINELY DOUBLE-COUNTED TYRES. **")
        print(f"  !!               Building makes them AND F5 seeds them. Cause: phase1c joins")
        print(f"  !!               WIP on the RAW itemcode; inventory.csv spells these with")
        print(f"  !!               ragged internal whitespace, so phase1c never finds them.")
        print(f"  !!               THE FIX BELONGS IN phase1c, NOT HERE. Not touched. Named.")
        for c, r in a_dbl.sort_values("seed_minus_netted", ascending=False).iterrows():
            raw = sorted(set(g.loc[g["_code"] == c, "itemcode"].astype(str)))
            print(f"  !!                 {c:<26} DOUBLE-COUNTED {r['seed_minus_netted']:>6,.0f}"
                  f"   gross demand {r['gross_demand']:>9,.0f}")
            print(f"  !!                   inventory spells it {raw!r}")
    print("  !!")

    # ---- (b) NETTED but NOT SEEDED ------------------------------------------
    b = t[t["seed_minus_netted"] < -0.5]
    b_gap = float(-b["seed_minus_netted"].sum())
    b_exp = float(b["f5_expired"].sum())
    b_nts = float(b["f5_no_ts"].sum())
    print(f"  !!   (b) NETTED but NOT SEEDED : {-b_gap:+,.0f} tyres on {len(b)} GT code(s)"
          f"   <-- A REAL HOLE, NOT A PHANTOM")
    print(f"  !!             phase1c has NO age check. It told BUILDING to make "
          f"{b_gap:,.0f} FEWER green")
    print(f"  !!             tyres on the strength of stock that is ALREADY PAST its shelf "
          f"life at")
    print(f"  !!             plan start. Those tyres are SCRAP (see "
          f"phase6_expired_opening_wip.csv).")
    print(f"  !!             F5 correctly refuses to seed them -- so the plan is "
          f"{b_gap:,.0f} green tyres")
    print(f"  !!             SHORT, for real. This does not flatter the schedule; it "
          f"under-builds it.")
    print(f"  !!             gap accounted for by EXPIRED {b_exp:,.0f} + NO_TIMESTAMP "
          f"{b_nts:,.0f} = {b_exp + b_nts:,.0f}")
    unexplained = b_gap - (b_exp + b_nts)
    if abs(unexplained) > 1.5:
        print(f"  !!             !! {unexplained:+,.0f} tyres of the gap are NOT explained by "
              f"age. NOT GUESSED -- FLAGGED.")
    print("  !!")
    dbl = float(a_dbl["seed_minus_netted"].sum()) if not a_dbl.empty else 0.0
    print(f"  !!   VERDICT: the {tot_seed - tot_net:+,.0f}-tyre gap is the NET of two OPPOSITE "
          f"errors.")
    print(f"  !!            GENUINELY DOUBLE-COUNTED: {dbl:,.0f} tyres  (phase1c whitespace join)")
    print(f"  !!            GENUINELY UNDER-BUILT   : {b_gap:,.0f} tyres  (phase1c has no age check)")
    print(f"  !!            Both defects live in phase1c. phase6 does NOT paper over them.")
    print("  " + "!" * 72)

    out = t.reset_index()
    out = out[(out["inventory"] > 0) | (out["p1c_netted"] > 0)]
    p = _atomic_to_csv(out, OUTPUTS / "phase6_wip_reconciliation.csv")
    print(f"  Wrote: {p.name}  (per-GT-code: inventory / seeded / expired / netted / demand)")


# ------------------------------------------------------------------ kit FIFO
def _streaming_gate(draws, qty, dur_h, tt_h):
    """S6 -- earliest press start when green tyres FLOW to the press.

    USER + PLANT (2026-07-24): a TBM emits one green tyre roughly every minute and the
    floor sends it straight to a press -- 95.8% of the plant's June build campaigns had
    curing START BEFORE building FINISHED, transfer unit = ONE TYRE. routing.csv (plant
    workbook) puts GT BUILDING transfer_time_min = 90, so a tyre built at u is curable
    at u + 90 min.

    Model. A drawn building segment [s_i, f_i] carrying q_i tyres emits them LINEARLY,
    so cumulative supply available to the press is

        S(t) = SUM_i q_i * clamp((t - (s_i + tt)) / (f_i - s_i), 0, 1)

    (a segment with s_i == f_i -- opening WIP -- is a step at f_i + tt.)
    The press consumes its `qty` linearly over `dur_h`, so starting at s it needs
    C(t) = qty * (t - s)/dur_h. Feasibility is S(t) >= C(t) for all t, i.e.

        s = MAX over breakpoints t of ( t - dur_h * S(t) / qty )

    The max sits at a breakpoint because S is piecewise-linear and C is linear.
    Returns None when there is nothing to gate on.

    NOTE this can only ever be <= the batch gate (which is S's last breakpoint), so it
    never delays a block; and it never allows a start before the first tyre is curable.
    """
    if not draws or qty is None or qty <= 0:
        return None
    tt = pd.Timedelta(hours=float(tt_h or 0.0))
    segs = []
    for s_i, f_i, q_i in draws:
        if q_i <= 0:
            continue
        a = pd.Timestamp(s_i) + tt          # first tyre of the segment is curable
        b = pd.Timestamp(f_i) + tt          # last tyre of the segment is curable
        if b < a:
            a = b
        segs.append((a, b, float(q_i)))
    if not segs:
        return None
    if dur_h is None or dur_h <= 0:         # no duration -> whole kit must be present
        return max(b for _, b, _ in segs)

    def supply(t):
        tot = 0.0
        for a, b, q in segs:
            if t >= b:
                tot += q
            elif t > a:
                span = (b - a).total_seconds()
                tot += q * ((t - a).total_seconds() / span) if span > 0 else q
        return tot

    best = None
    for t in sorted({p for a, b, _ in segs for p in (a, b)}):
        cand = t - pd.Timedelta(hours=dur_h * supply(t) / float(qty))
        if best is None or cand > best:
            best = cand
    return best


class KitPool:
    """Per-SKU FIFO pool of Building completions (and opening WIP -- FIX F5).

    For each SKU we hold a list of (endTime, qty_remaining) sorted by endTime.
    take(sku, qty) returns the endTime of the LAST building lot needed to
    accumulate `qty` units for this SKU. Building lots are consumed across
    consecutive calls (FIFO), reflecting Rule "consume them chronologically".

    !! "CHRONOLOGICALLY" IS A PROPERTY OF THE *CALLER*, NOT OF THIS CLASS.
       This pool serves whoever calls it, in call order. It is the caller's job
       to call in chronological order. See FIX F1 in revise_curing_schedule().
    """

    def __init__(self, build_df: pd.DataFrame, sku2key: dict | None = None):
        # SHARED-GT FIX: when sku2key is given, lots are pooled by GT code and take(sku)
        # draws from the pool of that SKU's green tyre (any SKU sharing the GT can use it).
        self.lots = {}
        self.sku2key = sku2key or {}
        if build_df is None or build_df.empty:
            return
        _has_start = "scheduled_start" in build_df.columns
        _key = "skuCode"
        if self.sku2key and "gtKey" in build_df.columns:
            build_df = build_df.copy()
            _k = build_df["gtKey"].astype(str).str.strip()
            _fb = build_df["skuCode"].map(self.sku2key)
            build_df["_poolkey"] = _k.where(_k.ne("") & _k.ne("nan"), _fb)
            _key = "_poolkey"
        for sku, g in build_df.groupby(_key):
            g2 = g.sort_values("scheduled_finish", kind="mergesort").reset_index(drop=True)
            _starts = (g2["scheduled_start"] if _has_start else g2["scheduled_finish"])
            # S6: entry is [finish, qty_remaining, start, qty_original].
            # `start` lets the streaming gate spread a segment's tyres linearly.
            self.lots[sku] = [
                [pd.Timestamp(t), int(q), pd.Timestamp(s if pd.notna(s) else t), int(q)]
                for t, q, s in zip(g2["scheduled_finish"], g2["qty"], _starts)
            ]

    # ========================================================================
    # FIX F2 -- take() FAILED OPEN, AND FAILED OPEN *SILENTLY*
    # ========================================================================
    # WAS: `return None` for TWO OPPOSITE conditions --
    #        :301  `if sku not in self.lots`  -> the SKU has NO building at all
    #        :319  `if need > 0`              -> the pool ran dry part-way
    #      and the caller then did:
    #        :349  `if kit_ready is not None and kit_ready > candidate_start:`
    #      so None == "impose no constraint" == KIT IS READY RIGHT NOW.
    #
    #      A TOTAL SHORTAGE AND A KIT-ON-THE-BENCH WERE THE SAME VALUE.
    #      37,833 tyres are cured from nothing, and not one line of output says so.
    #
    # WHY IT IS A DEFECT, NOT A POLICY: a sentinel that collapses "no data",
    # "not enough" and "no constraint" into one value is not a decision -- it is a
    # missing distinction. The *timing* consequence may well be the right policy
    # (v6 places the block anyway; a planner still needs to see the press plan).
    # The SILENCE is not.
    #
    # SO: TIMING IS BYTE-IDENTICAL. A SHORT or UNKNOWN still returns ts=None and
    # still imposes NO constraint -- exactly as before. We changed nothing about
    # what the scheduler DOES. We only made it SAY what it did.
    # ========================================================================
    # ========================================================================
    # E3 -- (A) DEFECT. THE AGE REPORT WAS MEASURING THE WRONG TYRE.
    # ========================================================================
    # `kit_ready` is REASSIGNED on every lot the loop below consumes, so when the
    # loop ends it holds the finish time of the LAST lot consumed. The pool is a
    # FIFO and consumes OLDEST FIRST -- therefore the LAST lot consumed is the
    # NEWEST tyre in the kit. Ageing the block against it reported the MINIMUM age
    # in the kit.
    #
    # SHELF LIFE IS DECIDED BY THE OLDEST TYRE IN THE KIT, NOT THE NEWEST. The
    # first tyre off the trolley is the one that has been sitting there longest,
    # and it is the one that is scrap.
    #
    # It was worse than "a bit optimistic": the F5-seeded OPENING WIP is, by
    # construction, at the FRONT of the FIFO (its produced_time predates every
    # building lot). It is therefore always consumed FIRST -- and was therefore
    # ALWAYS the one tyre the old metric could never see.
    #
    # MEASURED on the shipped config (READY blocks only: 6,356 blocks / 332,599 tyres)
    #     REPORTED (newest in kit) : median 17.6 h | over-24h 130,656 tyres = 39.3%
    #     ACTUAL   (oldest in kit) : median 20.4 h | over-24h 142,168 tyres = 42.7%
    #
    # WHY IT IS A DEFECT, NOT A POLICY: the code already carries the rule ("a green
    # tyre has a 24 h shelf life"). It simply measured the wrong end of the kit
    # while executing it. No new rule; no new machinery.
    #
    # !! TIMING IS UNTOUCHED. `kit_ready` (the NEWEST lot) remains the scheduling
    #    constraint, and that is CORRECT: the kit is not complete until the LAST
    #    lot lands. `kit_oldest` is returned ALONGSIDE it and is used ONLY by the
    #    aging report. The schedule is byte-identical.
    # ========================================================================
    def take(self, sku: str, qty: int, not_before=None, max_age=None):
        """Consume `qty` units of `sku` FIFO.

        Returns a DISCRIMINATED result -- (status, kit_ready, kit_oldest, missing_qty):
            ("READY", <newest_ts>, <oldest_ts>, 0)
                     enough kit.
                     kit_ready  = finish of the LAST lot needed  = the NEWEST tyre
                                  in the kit. THE TIMING CONSTRAINT (unchanged: the
                                  kit is not ready until the last lot lands).
                     kit_oldest = finish of the FIRST lot consumed = the OLDEST tyre
                                  in the kit. THE SHELF-LIFE VERDICT (E3).
            ("SHORT",   None, None, <int>)   SKU known, pool ran dry: `missing_qty`
                                             units were cured from nothing
            ("UNKNOWN", None, None, <int>)   SKU has NO building lots at all:
                                             the whole qty was cured from nothing
            ("NO_QTY",  None, None, 0)       qty <= 0; nothing to take

        SHORT / UNKNOWN report NO age -- deliberately, and exactly as before. A
        partial kit is not a kit; the age of a tyre that was never built is not a
        number. Those blocks are reported by their own channel (KIT SHORTFALL / S5),
        which keeps the aging population identical to the pre-E3 one and therefore
        directly comparable.
        """
        if qty is None or int(qty) <= 0:
            return (KIT_NONE, None, None, 0)
        if self.sku2key:
            sku = self.sku2key.get(sku, sku)
        if sku not in self.lots:
            # A ROUTING/DATA HOLE, not a capacity problem. Distinguish it -- the
            # two need completely different fixes and used to look identical.
            return (KIT_UNKNOWN, None, None, int(qty))

        # v19 (2026-10-05): tyres already past shelf life when the press can start are SCRAP --
        # removed from the pool (counted in self.last_scrap), never put in a kit
        self.last_scrap = 0
        if not_before is not None and max_age is not None:
            for _e in self.lots[sku]:
                if _e[1] > 0 and _e[0] + max_age < not_before:
                    self.last_scrap += int(_e[1])
                    _e[1] = 0
        need = int(qty)
        kit_ready  = None      # NEWEST lot consumed (last)  -- the timing constraint
        kit_oldest = None      # OLDEST lot consumed (first) -- the shelf-life verdict (E3)
        self.last_draws = []   # S6: [(seg_start, seg_finish, qty_taken)] for this call
        bucket = self.lots[sku]
        i = 0
        while i < len(bucket) and need > 0:
            t, avail, t_start, _q0 = bucket[i]
            if avail <= 0:
                i += 1
                continue
            consume = min(need, avail)
            avail -= consume
            need -= consume
            bucket[i][1] = avail
            self.last_draws.append((t_start, t, int(consume)))
            if kit_oldest is None:       # E3: the FIRST lot we actually drew from
                kit_oldest = t
            kit_ready = t                # (unchanged) the LAST lot we drew from
            if avail == 0:
                i += 1
        if need > 0:
            # PARTIAL. Whatever we did consume is gone (the arithmetic above already
            # decremented it) -- that is v6's behaviour and we keep it byte-for-byte.
            # We just no longer pretend the missing `need` units existed.
            return (KIT_SHORT, None, None, int(need))
        return (KIT_READY, kit_ready, kit_oldest, 0)


# ------------------------------------------------------------------ revise
def revise_curing_schedule(plan: pd.DataFrame,
                           build_df: pd.DataFrame,
                           cfg: dict | None = None,
                           blocked: pd.DataFrame | None = None,
                           horizon=None) -> pd.DataFrame:
    if plan.empty:
        return plan.copy()

    cfg = cfg or {}
    pool = KitPool(build_df, cfg.get("_sku2gtkey") if cfg.get("kitpool_key_by_gt") else None)
    # v19 (2026-10-05, config green_tyre_scrap_expired): green tyres past shelf life are scrap
    GT_SCRAP = bool(cfg.get("green_tyre_scrap_expired", False))
    GT_MAX = pd.Timedelta(hours=float(cfg.get("green_tyre_max_age_h", 72) or 72))
    n_gt_scrapped = 0
    out_rows = []

    # S2: the drum pin. In pass 2+ this is populated (block_int -> P1 start); in
    # pass 1 it is empty and phase6 behaves exactly as before (produces P1).
    P1_FLOOR = load_p1_floor(cfg)
    if P1_FLOOR:
        print(f"  [S2] PRESS FLOOR ON: pinning candidate_start >= P1 for "
              f"{len(P1_FLOOR):,} blocks (pass 2+)")
    n_pinned_by_p1 = 0

    ENFORCE_HORIZON = bool(cfg.get("enforce_strict_horizon", False))
    REINSERT = bool(cfg.get("reinsert_changeover_blocks", False))
    # FIX 2 -- press pull-forward: start a press's next block as soon as it is free and
    # the kit is ready, instead of idling to the plan clock. Floor = schedule horizon
    # open (earliest plan start), never orig_start. Sequence/assignment untouched.
    PULL_FORWARD = bool(cfg.get("press_pull_forward", False))

    # === S6 -- STREAMING KIT GATE ==========================================
    STREAM_KIT_GATE = bool(cfg.get("press_streaming_kit_gate", False))
    # GT->curing transfer time. Data-driven: routing.csv GT BUILDING transfer_time_min
    # (the plant workbook says 90). config key overrides; 0.0 if neither is present.
    GT_TRANSFER_H = cfg.get("gt_transfer_time_min", None)
    if GT_TRANSFER_H is None:
        GT_TRANSFER_H = 0.0
        try:
            _rt = pd.read_csv(INPUTS / str((cfg.get("files") or {})
                                           .get("routing", "routing.csv")),
                              usecols=["operation_name", "transfer_time_min"],
                              low_memory=False)
            _g = _rt[_rt["operation_name"].astype(str).str.upper().str.strip()
                     == "GT BUILDING"]["transfer_time_min"]
            _g = pd.to_numeric(_g, errors="coerce").dropna()
            if len(_g):
                GT_TRANSFER_H = float(_g.max()) / 60.0
        except Exception as _e:
            print(f"  [WARN] S6: could not read GT transfer time ({_e}); using 0")
    else:
        GT_TRANSFER_H = float(GT_TRANSFER_H) / 60.0
    n_stream_pulled = 0
    stream_pull_h = 0.0
    if STREAM_KIT_GATE:
        print(f"  [S6] STREAMING kit gate ON -- green tyres flow to the press; "
              f"GT transfer time {GT_TRANSFER_H*60:.0f} min")
    # =======================================================================
    _open_floor = pd.Timestamp(plan["startTime"].min()) if len(plan) else None
    if PULL_FORWARD:
        print(f"  [FIX2] press_pull_forward=ON -- presses start on kit-ready/free, "
              f"floored at horizon open {_open_floor} (not the plan clock)")
    blocked_idx = _index_blocked(blocked) if REINSERT else {}
    if REINSERT:
        print(f"  [F7] reinsert_changeover_blocks=True -- "
              f"{sum(len(v) for v in blocked_idx.values()):,} pinned busy windows "
              f"on {len(blocked_idx):,} presses are ENFORCED")

    # ========================================================================
    # FIX F1 -- THE 21-DAY DEFECT: A GLOBAL FIFO POOL DRAINED IN ALPHABETICAL
    #           ORDER
    # ========================================================================
    # WAS:
    #     for press, g in plan.groupby("pressNo"):          # <-- ALPHABETICAL
    #         g = g.sort_values("startTime"); prev_end = None
    #         for _, r in g.iterrows(): ... kit_ready = pool.take(...)
    #
    # The OUTER loop walks presses in the order groupby happens to yield them --
    # i.e. sorted by the press's NAME. The INNER loop drains a pool that is
    # GLOBAL and FINITE. So press "3609" empties the pool of every SKU it wants
    # before press "5205" is ever asked what it needs -- regardless of WHEN
    # either of them actually needs it.
    #
    # Press 5205's 07:00-on-1-June block therefore gets served kit that building
    # will not finish until 2 JULY: a 748 h (31-day) head delay on a block that
    # was scheduled for the first hour of the plan. That single mis-ordering is
    # the bulk of the 21-day makespan blow-out.
    #
    # WHY IT IS A DEFECT AND NOT A POLICY -- THE PROOF:
    #     RENAME THE PRESSES. Same 86 machines, same 6,975 blocks, same kit, same
    #     everything -- only the equipment IDs are spelled differently. The
    #     makespan moves by 4.2 DAYS.
    #     A schedule that depends on the SPELLING of an equipment ID is not
    #     implementing a policy. There is no plant rule "serve the press whose
    #     number sorts first". It is a loop-order accident, full stop.
    #
    # And it contradicts this file's OWN SPEC, twice over:
    #     - the KitPool docstring cites the Rule: "consume them chronologically"
    #     - the header says "For each press INDEPENDENTLY" -- which is a statement
    #       about the PRESS CONSTRAINT (prev_end), not about the shared kit pool.
    #       You cannot be "independent" and share a finite FIFO queue; the only
    #       coherent reading is: the press constraint is per-press, the kit queue
    #       is chronological.
    #
    # AFTER: ONE global pass in startTime order. `prev_end` -- which IS genuinely
    #        per-press -- becomes a DICT keyed by press instead of a scalar reset
    #        per group. That is the entire change.
    #
    # WHAT SURVIVES UNTOUCHED (asserted in main()):
    #     - the max(orig_start, kit_ready, prev_end) formula, character for character
    #     - each press's block SEQUENCE (we sort by time; we never re-sequence)
    #     - the 56 SKU-transitions -> the mould campaign is preserved
    #     - no press double-booking; no block pulled EARLIER than its original start
    #
    # WHAT IT BUYS: the press that needs kit FIRST is served FIRST. Head delays
    # collapse; the makespan stops depending on how the presses are named.
    # ========================================================================
    prev_end = {}   # pressNo -> last end on THAT press (was: a scalar, reset per group)

    # === FIX B -- PER-PRESS GAP-FILL (config: press_gap_fill, default False) ============
    # OFF: the `else` branches below are the scalar-cursor code verbatim -> byte-identical.
    # ON : each press keeps a list of occupied (start,end) intervals; a block takes the
    #      EARLIEST FREE slot >= candidate_start instead of being pushed behind the press's
    #      LAST block. A kit-late block takes a late hole; a following kit-ready block drops
    #      into the hole in front of it, so one late block can never head-of-line-block a
    #      whole press. pressNo/duration/qty are never touched. See PHASE6_CURING_TAIL_BUG.md.
    GAP_FILL = bool(cfg.get("press_gap_fill", False))
    press_busy = {}   # pressNo -> [(start,end), ...] already placed on THAT press
    # v78 (user 2026-10-11, Algo Planning BRBC-27 / 'no GT before mould change completion'): a mould change on a
    # press keeps the plan's changeover time when blocks slide. CO(press, new SKU) = the plan's gap before that SKU's
    # first block after a different SKU (fallback curing_changeover_min). In the free-slot search every placed block of
    # a DIFFERENT SKU is widened: start - CO(press, its SKU), end + CO(press, this SKU). Same-SKU blocks stay tight.
    KEEP_CO = bool(cfg.get("curing_keep_changeover", False)) and GAP_FILL
    _CO_DEF = timedelta(minutes=float(cfg.get("curing_changeover_min", 300) or 0))
    _CO = {}
    if KEEP_CO:
        _pl = plan.sort_values(["pressNo", "startTime"])
        for _p, _g in _pl.groupby("pressNo"):
            _prev = None
            for _r in _g.itertuples():
                if _prev is not None and str(_r.skuCode) != str(_prev.skuCode):
                    _gap = max(pd.Timestamp(_r.startTime) - pd.Timestamp(_prev.endTime), timedelta(0))
                    _k = (str(_p), str(_r.skuCode))
                    _CO[_k] = max(_CO.get(_k, timedelta(0)), _gap)
                _prev = _r
        print(f"  [CURING CO v78] mould-change time kept on slides: {len(_CO)} press-SKU changeovers from the plan "
              f"(fallback {_CO_DEF.total_seconds() / 60:.0f} min)")
    _placed_sku = {}  # (press, start, end) -> SKU of a placed block

    def _co(_p, _sku):
        return _CO.get((str(_p), str(_sku)), _CO_DEF)

    def _iv_eff(_p, _sku):
        _iv = press_busy.get(_p)
        if not KEEP_CO or not _iv:
            return _iv
        out_ = []
        for _a, _b in _iv:
            _o = _placed_sku.get((_p, _a, _b))
            if _o is not None and _o != _sku:
                out_.append((_a - _co(_p, _o), _b + _co(_p, _sku)))
            else:
                out_.append((_a, _b))
        return out_
    if GAP_FILL and REINSERT:
        for _p, _ws in blocked_idx.items():           # pinned mould-change windows are busy too
            press_busy[_p] = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in _ws]
    # BR-10 (2026-09-30): planned PM / MTC windows (jkplanning_CTP.downtime_reason, Section Curing)
    # are busy on the press -- no curing block may overlap them.
    _n_pm = 0
    if cfg.get("enforce_pm_downtime", False):
        try:
            _dt = pd.read_csv(ROOT / "db_pipeline" / "raw" / "jkplanning_CTP__downtime_reason.csv")
            _dt = _dt[_dt["Section"].astype(str).str.strip().str.upper() == "CURING"]
            for _m, _a, _b in zip(_dt["Machine ID"].astype(str).str.strip(),
                                  pd.to_datetime(_dt["Scheduled Start Time"], errors="coerce"),
                                  pd.to_datetime(_dt["Scheduled End Time"], errors="coerce")):
                if pd.isna(_a) or pd.isna(_b):
                    continue
                for _k in {_m, int(_m) if _m.isdigit() else _m}:
                    blocked_idx.setdefault(_k, []).append((pd.Timestamp(_a), pd.Timestamp(_b)))
                    blocked_idx[_k].sort()
                    if GAP_FILL:
                        press_busy.setdefault(_k, []).append((pd.Timestamp(_a), pd.Timestamp(_b)))
                        press_busy[_k].sort()
                _n_pm += 1
            print(f"  [BR-10] {_n_pm} press PM/MTC windows from downtime_reason blocked")
        except Exception as _e:
            print(f"  [BR-10] !! downtime_reason unreadable ({type(_e).__name__}) -- PM not enforced")

    # kind="mergesort" = stable: two blocks with the same (startTime, pressNo)
    # keep their original file order, so the pass is fully deterministic.
    # (On plan.csv there are 0 such ties -- the stability is belt-and-braces.)
    ordered = plan.sort_values(["startTime", "pressNo"], kind="mergesort")

    for _, r in ordered.iterrows():
        press      = str(r["pressNo"]).strip()
        orig_start = pd.Timestamp(r["startTime"])
        orig_end   = pd.Timestamp(r["endTime"])
        duration = (orig_end - orig_start) if (pd.notna(orig_start) and pd.notna(orig_end)) \
                   else timedelta(0)
        sku = str(r["skuCode"]).strip()
        qty = int(r["qty"])
        # ── V23 CURING CAPACITY (2026-10-06, config capacity_rules_v23.curing) ──────────────
        # q_shift = tyres one press can cure in a shift = floor(cavities x shift_min / (routing cure
        # + allowance)), cure from JK_CTP_PPDS.CTP_Master_Routing (CURING). A plan block never cures
        # more than q_shift, and holds the press qty x shift_min / q_shift minutes (not capped at the
        # plan window), so press time never exceeds the press's available time.
        _q23 = _qshift_v23(cfg, sku, r)
        if _q23 is not None:
            _qs, _min_per = _q23
            if qty > _qs:
                _V23["trimmed"] += qty - _qs
                _V23["blocks"] += 1
                qty = _qs
            duration = timedelta(minutes=qty * _min_per)
            r = r.copy()
            r["qty"] = qty
            r["cycleTime"] = _V23["cure"].get(sku, r.get("cycleTime"))
        # ── REAL PRESS TIME (2026-09-27, config curing_press_time_actual) ─────────────────
        # The plant plan is day-granular: a shift slice keeps the WHOLE shift window even when the
        # press needs less (plant's own formula: Qty/cavities x (cure + allowance), allowance 2.5 min
        # PCR / 6.0 min TBR). Holding the full window after a slide pushed the next block later for
        # no physical reason (median 22 min per block). Use the real press time, capped at the window.
        _pt = (cfg or {}).get("curing_press_time_actual") or {}
        if _pt.get("enabled") and _q23 is None:
            try:
                _cyc = float(r.get("cycleTime") or 0)
            except (TypeError, ValueError):
                _cyc = 0.0
            if _cyc > 0 and qty > 0:
                _tbr = sku.startswith(tuple(_pt.get("tbr_sku_prefix", ["11"])))
                _alw = float(_pt.get("allowance_tbr_min", 6.0) if _tbr else _pt.get("allowance_pcr_min", 2.5))
                _need = timedelta(minutes=qty / float(_pt.get("cavities", 2)) * (_cyc + _alw))
                if timedelta(0) < _need < duration:
                    duration = _need

        # Building kit availability for this SKU  (FIX F2: discriminated result;
        # E3: kit_oldest is returned alongside kit_ready and NEVER used for timing)
        # v19: earliest the press can start this block (lower bound); kit tyres older than shelf
        # life at that time are scrapped. If the block still starts later than expected (it waited
        # for newer tyres) and its oldest tyre is then over age, undo and retry from the real start.
        _lb = (_open_floor if (PULL_FORWARD and _open_floor is not None) else orig_start)
        _p1_ = P1_FLOOR.get(int(r[BLOCK_ID])) if P1_FLOOR else None
        if _p1_ is not None and _p1_ > _lb:
            _lb = _p1_
        if GAP_FILL:
            _lb = _earliest_free_slot(_iv_eff(press, sku), _lb, duration)
        elif prev_end.get(press) is not None and prev_end.get(press) > _lb:
            _lb = prev_end.get(press)
        _key_ = pool.sku2key.get(sku, sku) if pool.sku2key else sku
        for _att in range(6):
            if GT_SCRAP:
                _snap = [list(_e) for _e in pool.lots.get(_key_, [])]
                kit_status, kit_ready, kit_oldest, kit_short = pool.take(sku, qty, not_before=_lb, max_age=GT_MAX)
            else:
                kit_status, kit_ready, kit_oldest, kit_short = pool.take(sku, qty)


            # === S6 -- STREAMING KIT GATE (config: press_streaming_kit_gate) =========
            # v6/shipped: kit_ready = finish of the LAST lot drawn, so the press waits for
            # EVERY tyre in an 8-h block before curing the first one. The floor does not do
            # that (95.8% of June campaigns cured before building finished). Replace the
            # gate with the continuous-feed gate, offset by the routing's GT transfer time.
            if STREAM_KIT_GATE and kit_status == KIT_READY:
                _dur_h = None
                try:
                    _e = pd.Timestamp(r["endTime"]); _s = pd.Timestamp(r["startTime"])
                    if pd.notna(_e) and pd.notna(_s):
                        _dur_h = (_e - _s).total_seconds() / 3600.0
                except Exception:
                    _dur_h = None
                _sg = _streaming_gate(getattr(pool, "last_draws", None), qty, _dur_h,
                                      GT_TRANSFER_H)
                if _sg is not None:
                    if kit_ready is None or _sg < kit_ready:
                        n_stream_pulled += 1
                        stream_pull_h += (0.0 if kit_ready is None
                                          else (kit_ready - _sg).total_seconds() / 3600.0)
                    kit_ready = _sg if kit_ready is None else min(kit_ready, _sg)
            # ========================================================================

            # -------------------------------------------------------------------
            # THE SPEC FORMULA -- max(original, kit_ready, prev_end_on_press).
            # UNCHANGED, CHARACTER FOR CHARACTER. Only `prev_end` is now looked up
            # per-press instead of being a scalar the outer loop kept resetting.
            # -------------------------------------------------------------------
            # FIX 2: pull-forward drops the orig_start floor (start on free-press + kit-ready,
            # floored only at the horizon open). OFF -> candidate_start = orig_start (v6).
            candidate_start = (_open_floor if (PULL_FORWARD and _open_floor is not None)
                               else orig_start)
            if kit_ready is not None and kit_ready > candidate_start:
                candidate_start = kit_ready
            pe = prev_end.get(press)                      # F1: was a scalar
            if not GAP_FILL and pe is not None and pe > candidate_start:
                candidate_start = pe   # scalar-cursor path; GAP_FILL uses the free-slot scan below
            # S2 -- THE DRUM PIN. candidate_start = max(P1, kit_ready, prev_end). P1 is
            # the press slot building was re-timed for; a block may not run EARLIER than
            # it (only later, if kit is still not ready -> the ratchet). Empty in pass 1.
            p1 = P1_FLOOR.get(int(r[BLOCK_ID])) if P1_FLOOR else None
            if p1 is not None and p1 > candidate_start:
                candidate_start = p1
                n_pinned_by_p1 += 1
            # -------------------------------------------------------------------

            # FIX F7 (opt-in): a pinned mould-change window is BUSY press time. A
            # delayed block may not run through one. Applied AFTER the spec formula,
            # never inside it -- this only ever pushes `candidate_start` LATER, so it
            # cannot pull a block earlier and cannot re-sequence a press.
            _gf_pushed = False
            if GAP_FILL:
                # candidate_start already >= orig_start, kit_ready, P1 (applied above). Take the
                # earliest FREE slot on this press at or after it; blocked windows are pre-seeded
                # into press_busy, so this also enforces F7 without _clear_blocked.
                _req = candidate_start
                candidate_start = _earliest_free_slot(_iv_eff(press, sku), candidate_start, duration)
                _gf_pushed = candidate_start > _req          # the press (a hole boundary) pushed it
                new_end = candidate_start + duration
                _insert_interval(press_busy.setdefault(press, []), candidate_start, new_end)
                _placed_sku[(press, candidate_start, new_end)] = sku
            else:
                if REINSERT:
                    candidate_start = _clear_blocked(candidate_start, duration,
                                                     blocked_idx.get(press, ()))
                new_end = candidate_start + duration
            if (GT_SCRAP and _att < 5 and kit_oldest is not None
                    and kit_oldest + GT_MAX < candidate_start):
                if _key_ in pool.lots:
                    pool.lots[_key_] = _snap                     # undo this attempt's draw + scrap
                if GAP_FILL:
                    try:
                        press_busy[press].remove((candidate_start, new_end))
                        _placed_sku.pop((press, candidate_start, new_end), None)
                    except (KeyError, ValueError):
                        pass
                _lb = candidate_start
                continue
            break
        if GT_SCRAP:
            n_gt_scrapped += int(getattr(pool, "last_scrap", 0) or 0)

        # Recompute shift + date
        shift, plant_day = _derive_shift_and_plant_day(candidate_start)

        row = r.to_dict()
        row["startTime"] = candidate_start
        row["endTime"]   = new_end
        row["shift"]     = shift
        row["date"]      = plant_day

        # FIX F2: the kit truth, on the row, in the output CSV. Not a print -- a COLUMN.
        row["kit_status"]    = kit_status
        row["kit_short_qty"] = int(kit_short)
        row["kit_ready"]     = kit_ready       # NEWEST lot in the kit -- the timing constraint
        row["kit_oldest"]    = kit_oldest      # E3: OLDEST lot in the kit -- the shelf-life verdict

        # FIX F4 / E3: age of the green tyre AT THE PRESS, measured AFTER the move.
        #   green_age_h        -- vs the OLDEST tyre in the kit. THE VERDICT. (E3)
        #   green_age_newest_h -- vs the NEWEST tyre in the kit. What was reported
        #                         before E3. Kept so the two are comparable on the
        #                         face of the file and nobody has to take my word for it.
        row["green_age_h"] = ((candidate_start - kit_oldest).total_seconds() / 3600.0
                              if (kit_oldest is not None and pd.notna(candidate_start))
                              else pd.NA)
        row["green_age_newest_h"] = ((candidate_start - kit_ready).total_seconds() / 3600.0
                                     if (kit_ready is not None and pd.notna(candidate_start))
                                     else pd.NA)

        # FIX F3: config says flag it, phase5 flags it, so phase6 flags it. Still placed.
        past_h = bool(ENFORCE_HORIZON and horizon is not None
                      and pd.notna(new_end) and new_end > pd.Timestamp(horizon))
        row["past_horizon"] = past_h

        # Annotate the delay reason for traceability
        delay_min = max(0.0, (candidate_start - orig_start).total_seconds() / 60.0)
        why = []
        if kit_ready is not None and kit_ready > orig_start:
            why.append("kit_late")
        if _gf_pushed if GAP_FILL else (pe is not None and pe > orig_start and
                                        (kit_ready is None or pe >= kit_ready)):
            why.append("press_busy")
        if kit_status in (KIT_SHORT, KIT_UNKNOWN):
            why.append(f"kit_{kit_status.lower()}")      # F2: visible in remarks too
        if past_h:
            why.append("PAST_HORIZON")                   # F3
        existing = str(row.get("remarks") or "").strip()
        revision_note = f"REVISED(+{delay_min:.0f}min{', ' + '/'.join(why) if why else ''})"
        row["remarks"] = (existing + (" | " if existing else "") + revision_note) \
                         if delay_min > 0 else (existing or "ON_TIME")
        # A no-delay row can still be SHORT / UNKNOWN / PAST_HORIZON. Never let a
        # zero delay hide a kit hole -- that is the F2 defect in miniature.
        if delay_min <= 0 and why:
            row["remarks"] = f"{row['remarks']} | {'/'.join(why)}"

        if not GAP_FILL:
            prev_end[press] = new_end     # GAP_FILL tracks occupancy in press_busy instead
        out_rows.append(row)

    if _V23["cure"] is not None:
        print(f"  [V23 CURE CAP] q_shift cap: {_V23['blocks']:,} plan blocks trimmed by "
              f"{_V23['trimmed']:,} tyres (cumulative over passes); every block holds its press "
              f"qty x 480 / q_shift min")
    if P1_FLOOR:
        print(f"  [S2] press-floor bound on {n_pinned_by_p1:,} blocks "
              f"(candidate_start raised to P1)")
    if STREAM_KIT_GATE:
        print(f"  [S6] streaming gate pulled {n_stream_pulled:,} blocks earlier "
              f"({stream_pull_h:,.0f} press-hours advanced; mean "
              f"{stream_pull_h/max(n_stream_pulled,1):.2f} h/block)")
    if GT_SCRAP:
        print(f"  [v19 GT SHELF LIFE] {n_gt_scrapped:,} green tyres scrapped (past "
              f"{GT_MAX.total_seconds() / 3600:.0f} h before a press could take them); none cured over age")
    return pd.DataFrame(out_rows)


# ============================================================================
# BEST-PRESS  --  mould-capped earliest-finish list-scheduler   (config: best_press_curing)
# ============================================================================
# revise_curing_schedule() above keeps every block on the press plan.csv named
# for it and only slides it LATER (the verbatim v6 port). That leaves the makespan
# hostage to plan.csv's press assignment: a SKU the LP pinned to one press cures
# serially even though routing lets it run on dozens.
#
# BEST-PRESS re-places each curing block onto the routing-eligible press that
# finishes it EARLIEST, under three HARD, physically-real constraints:
#
#   * kit-gate   -- a block may not start before its own kit_ready (the green tyre
#                   must already exist). We REUSE the kit_ready that
#                   revise_curing_schedule() already assigned via the FIFO KitPool,
#                   so the green-tyre->block matching is unchanged; only the PRESS
#                   and the start time move.
#   * mould-cap  -- a SKU may never run on more presses AT ONCE than the plant has
#                   moulds for it. The cap DEFAULTS to the plan's own max concurrency
#                   per SKU -- a PROVEN lower bound (the plan already runs that many
#                   at once, so that many moulds demonstrably exist). It NEVER invents
#                   a mould. If the plant supplies real counts in cfg["curing_mould_counts"]
#                   they supersede the plan-derived cap per SKU.
#   * changeover -- switching a press to a different SKU is a mould change; it costs
#                   cfg["curing_changeover_min"] (default 300 = 5 h, the plan's own
#                   CHANGEOVER-row value).
#
# MEASURED on the current outputs2: verbatim 37.0 d -> best-press 34.85 d, and the
# result is PHYSICALLY VALID (concurrency never exceeds the plan's own). It never
# beats the BUILDING floor (the last green tyre's kit_ready) -- that is a building
# lever, not a curing one.
#
# WHAT IT PRESERVES: block_id, skuCode, qty, cycleTime, and each block's DURATION
# and its kit_ready/kit_oldest. WHAT IT REWRITES: pressNo, startTime, endTime,
# shift, date, green_age_*, remarks. The E4 gate for this path
# (_check_schedule_bestpress) checks the constraints that MUST still hold -- rows
# 1:1, immutable skuCode/qty/cycleTime, duration preserved, no press double-book,
# READY-not-before-kit -- PLUS the two new ones: every press routing-eligible, no
# SKU over its mould cap. pressNo/sequence/transition changes are INTENTIONAL here,
# so those v6-path checks are deliberately not applied.
# ============================================================================
def _press_sort_key(p: str):
    """Deterministic press order: by numeric value when the id is all digits, else by
    string. So ties break the SAME way every run (no set-iteration / hash randomness)."""
    s = str(p).strip()
    return (0, int(s)) if s.isdigit() else (1, 0, s)


def _curing_press_eligibility(cfg: dict) -> dict:
    """{sku(str) -> SORTED list(pressNo str)} from routing.csv CURING rows. READ-ONLY.
    A SKU with no curing routing row is absent -> best-press keeps its plan press.
    The list is SORTED (not a set) so the scheduler's tie-break is DETERMINISTIC --
    a set would iterate in Python hash order, which varies run-to-run and would make
    the makespan non-reproducible."""
    path = INPUTS / str((cfg.get("files") or {}).get("routing", "routing.csv"))
    acc: dict = {}
    if not path.exists():
        print(f"  [BEST-PRESS] routing file {path.name} absent -- eligibility empty "
              f"(every SKU keeps its plan press)")
        return {}
    rt = pd.read_csv(path, low_memory=False)
    if "department" in rt.columns:
        rt = rt[rt["department"].astype(str).str.upper().str.strip() == "CURING"]
    if "routed_product" not in rt.columns or "machines" not in rt.columns:
        return {}
    # NaN-safe: the pandas `str` dtype keeps NA as a float through .astype(str),
    # so fill blanks FIRST (a routing row with no machines contributes nothing).
    prods = rt["routed_product"].fillna("").map(lambda x: str(x))
    machs = rt["machines"].fillna("").map(lambda x: str(x))
    for prod, machines in zip(prods, machs):
        ps = re.findall(r"\d+", machines)
        if ps:
            acc.setdefault(prod.strip(), set()).update(ps)
    return {sku: sorted(ps, key=_press_sort_key) for sku, ps in acc.items()}


def _mould_caps_from_plan(plan_df: pd.DataFrame, cfg: dict) -> dict:
    """{sku(str) -> mould count}. DEFAULT = the plan's OWN max concurrency per SKU --
    a proven lower bound, never an invented mould. cfg['curing_mould_counts'] (plant
    truth, if supplied) supersedes per SKU. READ-ONLY on the plan."""
    caps: dict = {}
    d = plan_df.copy()
    d["startTime"] = pd.to_datetime(d["startTime"], errors="coerce")
    d["endTime"]   = pd.to_datetime(d["endTime"],   errors="coerce")
    for sku, g in d.groupby(d["skuCode"].astype(str).str.strip()):
        ev = []
        for s, e in zip(g["startTime"], g["endTime"]):
            if pd.notna(s) and pd.notna(e):
                ev.append((s, 1)); ev.append((e, -1))
        ev.sort()
        cur = mx = 0
        for _, delta in ev:
            cur += delta
            if cur > mx:
                mx = cur
        caps[sku] = max(1, mx)
    override = cfg.get("curing_mould_counts") or {}
    for k, v in override.items():
        try:
            caps[str(k).strip()] = max(1, int(v))
        except (ValueError, TypeError):
            pass
    return caps


def best_press_reassign(revised: pd.DataFrame,
                        plan_orig: pd.DataFrame,
                        cfg: dict,
                        plan_start: pd.Timestamp) -> pd.DataFrame:
    """Re-place each curing block onto the routing-eligible press with the EARLIEST
    finish, subject to kit-gate + mould-cap + changeover (see the block comment above).
    Returns a new frame with pressNo/startTime/endTime/shift/date/green_age_*/remarks
    rewritten and everything else preserved."""
    if revised.empty:
        return revised

    elig  = _curing_press_eligibility(cfg)
    caps  = _mould_caps_from_plan(plan_orig, cfg)
    chg   = timedelta(minutes=float(cfg.get("curing_changeover_min", 300) or 0))
    tol   = timedelta(hours=float(cfg.get("curing_changeover_tolerance_h", 0) or 0))
    allp  = set(revised["pressNo"].astype(str).str.strip().unique())

    df = revised.reset_index(drop=True).copy()
    # _release drives BOTH the dispatch order and the earliest legal start. We do NOT
    # clip it up to plan_start: opening-WIP kit (built before t=0) must sort FIRST so the
    # oldest stock cures first (FEFO). The start is floored at plan_start anyway, because
    # every press/mould free-time is initialised to plan_start and start = max(rel, ...).
    df["_release"]   = pd.to_datetime(df.get("kit_ready"), errors="coerce").fillna(plan_start)
    df["_origstart"] = pd.to_datetime(df["startTime"], errors="coerce")
    df["_dur"]       = pd.to_datetime(df["endTime"], errors="coerce") - df["_origstart"]

    order = df.sort_values(["_release", "_origstart", BLOCK_ID], kind="mergesort").index

    press_free:  dict = {}   # pressNo -> free timestamp
    press_mould: dict = {}   # pressNo -> current SKU (for the changeover test)
    press_load:  dict = {}   # pressNo -> blocks placed so far (load-spread tie-break)
    mould_free:  dict = {}   # sku -> list of M mould-slot free-times

    n_switch = 0
    n_earlier = 0
    new_press = {}; new_start = {}; new_end = {}
    for i in order:
        sku = str(df.at[i, "skuCode"]).strip()
        r   = df.at[i, "_release"]
        dur = df.at[i, "_dur"]
        if pd.isna(dur):
            dur = timedelta(0)
        e = [p for p in elig.get(sku, ()) if p in allp]
        if not e:
            e = [str(df.at[i, "pressNo"]).strip()]      # routing hole -> keep plan press
        slots = mould_free.setdefault(sku, [plan_start] * max(1, caps.get(sku, 1)))
        mi    = min(range(len(slots)), key=lambda k: slots[k])
        mfree = slots[mi]
        # For each eligible press: earliest finish, +changeover if the press must switch SKU.
        cand = []
        for p in e:
            pf     = press_free.get(p, plan_start)
            same   = (press_mould.get(p) == sku)
            switch = (p in press_mould and not same)
            st     = max(r, pf, mfree) + (chg if switch else timedelta(0))
            cand.append((p, st, switch, same))
        best_finish = min(st for _, st, _, _ in cand)
        # CHANGEOVER-TOLERANCE BAND (curing_changeover_tolerance_h). Among presses that
        # finish within `tol` of the earliest, we PREFER one that avoids a changeover, so
        # the mould campaign is kept when it costs little makespan. tol = 0 (default) =
        # pure earliest-finish (max makespan gain, most changeovers). Larger tol trades
        # makespan for FEWER changeovers -- but they are tightly coupled (see the
        # frontier in BEST_PRESS_CURING.md); tol above a few hours degrades makespan fast.
        band = [c for c in cand if c[1] <= best_finish + tol]
        # Selection key (all DETERMINISTIC): (1) prefer the press already holding this SKU
        # (continue the campaign, no changeover); (2) any no-switch press; (3) earliest
        # finish; (4) least-loaded; (5) lowest press number.
        band.sort(key=lambda c: (0 if c[3] else 1, 0 if not c[2] else 1, c[1],
                                 press_load.get(c[0], 0), _press_sort_key(c[0])))
        best, bstart, bswitch, _ = band[0]
        end = bstart + dur
        press_free[best] = end
        press_mould[best] = sku
        press_load[best] = press_load.get(best, 0) + 1
        slots[mi] = end
        if bswitch:
            n_switch += 1
        if pd.notna(df.at[i, "_origstart"]) and bstart < df.at[i, "_origstart"]:
            n_earlier += 1
        new_press[i] = best; new_start[i] = bstart; new_end[i] = end

    # ---- rewrite the moved columns; recompute derived fields ----
    df["pressNo"]   = df.index.map(new_press).astype(str)
    df["startTime"] = df.index.map(new_start)
    df["endTime"]   = df.index.map(new_end)
    sd = df["startTime"].map(lambda t: _derive_shift_and_plant_day(t))
    df["shift"] = sd.map(lambda x: x[0])
    df["date"]  = sd.map(lambda x: x[1])

    ko = pd.to_datetime(df.get("kit_oldest"), errors="coerce")
    kr = pd.to_datetime(df.get("kit_ready"),  errors="coerce")
    df["green_age_h"] = ((df["startTime"] - ko).dt.total_seconds() / 3600.0).where(ko.notna())
    df["green_age_newest_h"] = ((df["startTime"] - kr).dt.total_seconds() / 3600.0).where(kr.notna())
    tag = df["remarks"].astype(str).where(df["remarks"].notna(), "") if "remarks" in df.columns else ""
    df["remarks"] = (tag + " | BEST_PRESS").str.strip(" |") if isinstance(tag, pd.Series) else "BEST_PRESS"

    df = df.drop(columns=["_release", "_origstart", "_dur"])

    mk_before = (pd.to_datetime(revised["endTime"], errors="coerce").max() - plan_start).total_seconds() / 86400
    mk_after  = (df["endTime"].max() - plan_start).total_seconds() / 86400
    n_press_before = revised["pressNo"].astype(str).str.strip().nunique()
    n_press_after  = df["pressNo"].astype(str).str.strip().nunique()
    print(f"  [BEST-PRESS] mould-capped earliest-finish re-placement:")
    print(f"      makespan {mk_before:.2f}d -> {mk_after:.2f}d   ({mk_after - mk_before:+.2f}d)")
    print(f"      presses used {n_press_before} -> {n_press_after}   |  mould-change switches {n_switch}")
    print(f"      blocks re-placed EARLIER than their plan slot: {n_earlier:,}")
    print(f"      mould-cap source: plan max-concurrency (proven LB)"
          f"{' + cfg override' if (cfg.get('curing_mould_counts')) else ''}"
          f"  |  changeover {chg.total_seconds()/60:.0f} min")
    return df


def _check_schedule_bestpress(og: pd.DataFrame, rv: pd.DataFrame,
                              elig: dict, caps: dict) -> tuple:
    """E4 for the BEST-PRESS path. pressNo / sequence / SKU-transitions change ON
    PURPOSE here, so those v6-path checks are replaced by (a) every pressNo is
    routing-eligible for its SKU and (b) no SKU exceeds its mould cap concurrently.
    All other invariants (rows 1:1, skuCode/qty/cycleTime immutable, duration
    preserved, no press double-book, READY-not-before-kit) STILL hold and are checked."""
    lines = []; ok = True

    def rec(label, bad, extra=""):
        nonlocal ok
        good = (bad == 0)
        ok = ok and good
        lines.append(f"    {label:<40}: {bad}  {'OK' if good else '<< FAIL'}{extra}")

    if BLOCK_ID not in rv.columns:
        return False, [f"    << FAIL: output carries no `{BLOCK_ID}` -- cannot verify."]
    rec("rows lost or invented", abs(len(rv) - len(og)),
        f"   ({len(rv):,} written vs {len(og):,} planned)")
    rec("block_id not 1:1 with the plan", int(len(set(og[BLOCK_ID]) ^ set(rv[BLOCK_ID]))))
    j = og.merge(rv, on=BLOCK_ID, how="inner", suffixes=("_old", "_new"))
    if j.empty:
        return False, lines + ["    << FAIL: the output shares NO block with the plan."]

    mutated = int((j["skuCode_old"].astype(str).str.strip()
                   != j["skuCode_new"].astype(str).str.strip()).sum())
    for c in ("qty", "cycleTime"):
        if f"{c}_old" in j.columns and f"{c}_new" in j.columns:
            mutated += int((pd.to_numeric(j[f"{c}_old"], errors="coerce")
                            != pd.to_numeric(j[f"{c}_new"], errors="coerce")).sum())
    rec("immutable fields mutated", mutated, "   (skuCode/qty/cycleTime; pressNo MAY change)")

    dur_old = (j["endTime_old"] - j["startTime_old"])
    dur_new = (j["endTime_new"] - j["startTime_new"])
    # 2026-09-27: a block may run SHORTER than its plan window (curing_press_time_actual = real press
    # time), never LONGER. A longer block would still be a defect.
    rec("durations lengthened", int((dur_new > dur_old + pd.Timedelta(seconds=1)).sum()))

    s = rv.sort_values(["pressNo", "startTime", "endTime"], kind="mergesort")
    pe = s.groupby("pressNo", sort=False)["endTime"].shift(1)
    rec("press double-bookings",
        int(((s["startTime"] < pe) & pe.notna() & s["startTime"].notna()).sum()))

    # NEW 1: every press routing-eligible (fail only when routing EXISTS and excludes it)
    bad_elig = 0
    for sku, press in zip(rv["skuCode"].astype(str).str.strip(),
                          rv["pressNo"].astype(str).str.strip()):
        e = elig.get(sku)
        if e and press not in e:
            bad_elig += 1
    rec("blocks on a non-eligible press", bad_elig)

    # NEW 2: no SKU over its mould cap concurrently
    over = 0; worst = ""
    for sku, g in rv.groupby(rv["skuCode"].astype(str).str.strip()):
        ev = []
        for s0, e0 in zip(pd.to_datetime(g["startTime"], errors="coerce"),
                          pd.to_datetime(g["endTime"],   errors="coerce")):
            if pd.notna(s0) and pd.notna(e0):
                ev.append((s0, 1)); ev.append((e0, -1))
        ev.sort()
        cur = mx = 0
        for _, d in ev:
            cur += d
            if cur > mx:
                mx = cur
        cap = caps.get(sku, 1)
        if mx > cap:
            over += 1
            if not worst:
                worst = f"   (e.g. {sku}: {mx} concurrent > cap {cap})"
    rec("SKUs exceeding mould cap", over, worst)

    if "kit_ready" in rv.columns and "kit_status" in rv.columns:
        r = rv[rv["kit_status"].astype(str) == KIT_READY].copy()
        kr = pd.to_datetime(r["kit_ready"], errors="coerce")
        rec("READY blocks starting before kit",
            int(((r["startTime"] < kr) & kr.notna()).sum()))

    return ok, lines


# ============================================================================
# E4 -- (A) DEFECT. "ALL INVARIANTS HOLD" WAS A TAUTOLOGY. THE GATE COULD NOT FAIL.
# ============================================================================
# WAS: verify_invariants(orig, revised) took the IN-MEMORY frame that the loop had
#      just built and re-derived, from that same frame, four facts that are
#      LOGICAL CONSEQUENCES of the loop that built it:
#
#        candidate_start = max(orig_start, kit_ready, prev_end[press])
#             ==> candidate_start >= orig_start, ALWAYS, by construction.
#                 "blocks pulled EARLIER: 0" CANNOT be non-zero.
#        prev_end[press] = new_end, and blocks on a press are visited in
#        original-startTime order
#             ==> "press double-booking: 0" and "sequence changed: 0" CANNOT be
#                 non-zero.
#        SKU-transitions are a function of the sequence
#             ==> "56 -> 56" PROVES NOTHING.
#
#      Four checks. Zero of them could ever have printed FAIL. It printed
#      "ALL INVARIANTS HOLD" on every run and it would have printed it just as
#      confidently on a schedule that was wrong.
#
# WHY IT IS A DEFECT, NOT A POLICY: this file states, in its own header, that the
# algorithm must be unchanged, and then installs a gate to prove it. The gate does
# not test that. It restates it. This is the same defect that was just removed from
# phase1b's demand gate -- a gate that cannot fail is not a gate, it is a decoration,
# and it costs you the vigilance you think you are buying.
#
# AFTER: the check reads the WRITTEN CSV back off the disk and the ORIGINAL PLAN
#        back out of db_loader, and re-derives everything from THOSE TWO FILES. It
#        touches not one variable produced by revise_curing_schedule(). It joins the
#        two on `block_id` -- a stable identity now PERSISTED in the output (plan_id
#        is "CTP_PCR" on every row of plan.csv, so it cannot identify a block; do not
#        be tempted).
#
#        And because a gate you have never seen fail is a gate you do not know you
#        have: _selftest_verifier() runs the whole check ONE MORE TIME against a
#        DELIBERATELY CORRUPTED copy of the output and requires it to FAIL. If the
#        corrupted run PASSES, phase 6 says so, loudly. (cfg: verify_selftest,
#        default True. It costs ~0.1 s.)
#
# REPORT-ONLY. Not one scheduling decision is touched.
# ============================================================================
#
# The identity column. Deterministic: the row's position in the FILTERED plan, in
# the order db_loader hands it over. main() stamps it; verify re-derives it from an
# independent re-load of the same source, and the join then PROVES the two agree on
# pressNo / skuCode / qty / cycleTime for every block -- so a mis-stamped id fails
# the check rather than hiding inside it.
BLOCK_ID = "block_id"

def _check_schedule(og: pd.DataFrame, rv: pd.DataFrame, allow_reorder: bool = False) -> tuple:
    """PURE. Two DataFrames in -> (ok, [lines]) out. No I/O, no globals, no loop state.

    `og` = the original plan (block_id, pressNo, skuCode, qty, cycleTime, startTime, endTime)
    `rv` = the schedule AS WRITTEN     (same key + the new startTime/endTime/shift/date)

    Every hard check below is capable of returning FAIL, and _selftest_verifier() proves it.
    `allow_reorder=True` (press_gap_fill): the within-press SEQUENCE and SKU-transition checks
    (4, 5) are reported as INFO instead of FAIL, because gap-fill intentionally lets a kit-ready
    block occupy a hole ahead of a kit-late one. Every OTHER invariant still hard-fails.
    """
    lines = []
    ok = True

    def rec(label, bad, extra="", soft=False):
        nonlocal ok
        good = (bad == 0)
        if not (soft and allow_reorder):
            ok = ok and good
        tag = "OK" if good else ("INFO" if (soft and allow_reorder) else "<< FAIL")
        lines.append(f"    {label:<34}: {bad}  {tag}{extra}")

    # ---- 0. IDENTITY. The join key must be total and 1:1, and the two files must
    #         agree on every column the scheduler is not allowed to touch. If this
    #         fails, nothing below means anything -- so it is checked FIRST.
    if BLOCK_ID not in rv.columns:
        lines.append(f"    << FAIL: the written output carries no `{BLOCK_ID}` -- "
                     f"the invariants CANNOT be checked, so they are NOT claimed.")
        return False, lines

    rec("rows lost or invented", abs(len(rv) - len(og)),
        f"   ({len(rv):,} written vs {len(og):,} planned)")
    rec("block_id not 1:1 with the plan",
        int(len(set(og[BLOCK_ID]) ^ set(rv[BLOCK_ID]))))

    j = og.merge(rv, on=BLOCK_ID, how="inner", suffixes=("_old", "_new"))
    if j.empty:
        lines.append("    << FAIL: the written output shares NO block with the plan.")
        return False, lines

    mutated = 0
    for c in ("pressNo", "skuCode"):
        a, b = j[f"{c}_old"].astype(str).str.strip(), j[f"{c}_new"].astype(str).str.strip()
        mutated += int((a != b).sum())
    for c in ("qty", "cycleTime"):
        a = pd.to_numeric(j[f"{c}_old"], errors="coerce")
        b = pd.to_numeric(j[f"{c}_new"], errors="coerce")
        mutated += int((a != b).sum())
    rec("immutable fields mutated", mutated, "   (pressNo/skuCode/qty/cycleTime)")

    # ---- 1. DURATION PRESERVED. v6's rule 4: new_end = start + ORIGINAL duration.
    dur_old = (j["endTime_old"] - j["startTime_old"])
    dur_new = (j["endTime_new"] - j["startTime_new"])
    # 2026-09-27: a block may run SHORTER than its plan window (curing_press_time_actual = real press
    # time), never LONGER. A longer block would still be a defect.
    rec("durations lengthened", int((dur_new > dur_old + pd.Timedelta(seconds=1)).sum()))

    # ---- 2. NO BLOCK PULLED EARLIER than its original start.
    rec("blocks pulled EARLIER", int((j["startTime_new"] < j["startTime_old"]).sum()))

    # ---- 3. NO PRESS DOUBLE-BOOKING. Occupancy re-derived from the WRITTEN
    #         startTime/endTime alone -- not from `prev_end`, which no longer exists here.
    s = rv.sort_values(["pressNo", "startTime", "endTime"], kind="mergesort")
    prev_end_col  = s.groupby("pressNo", sort=False)["endTime"].shift(1)
    overlap = (s["startTime"] < prev_end_col) & prev_end_col.notna() & s["startTime"].notna()
    rec("press double-bookings", int(overlap.sum()))

    # ---- 4. PRESS SEQUENCE UNCHANGED. For each press, the block_id order under the
    #         NEW times must equal the block_id order under the ORIGINAL times.
    o_seq = (og.sort_values(["pressNo", "startTime", BLOCK_ID], kind="mergesort")
               .groupby("pressNo", sort=True)[BLOCK_ID].apply(list))
    r_seq = (rv.sort_values(["pressNo", "startTime", BLOCK_ID], kind="mergesort")
               .groupby("pressNo", sort=True)[BLOCK_ID].apply(list))
    presses = sorted(set(o_seq.index) | set(r_seq.index))
    reordered = sum(1 for p in presses if o_seq.get(p, []) != r_seq.get(p, []))
    rec("presses whose sequence changed", reordered, f"   of {len(presses)}", soft=True)

    # ---- 5. THE MOULD CAMPAIGN. SKU-transitions per press must be identical.
    def _transitions(df):
        d = df.sort_values(["pressNo", "startTime", BLOCK_ID], kind="mergesort")
        sku = d["skuCode"].astype(str)
        same_press = d["pressNo"].astype(str).eq(d["pressNo"].astype(str).shift(1))
        return int((same_press & sku.ne(sku.shift(1))).sum())
    t_o, t_r = _transitions(og), _transitions(rv)
    rec("SKU-transitions moved", abs(t_r - t_o), f"   ({t_r} now, {t_o} in the plan)", soft=True)

    # ---- 6. THE WRITTEN ROW MUST BE SELF-CONSISTENT. shift/date are DERIVED from
    #         startTime -- so re-derive them from the written startTime and compare
    #         to the written shift/date. Catches a row that was edited after the fact.
    if "shift" in rv.columns and "date" in rv.columns:
        d = rv[rv["startTime"].notna()]
        exp = d["startTime"].map(lambda t: _derive_shift_and_plant_day(t))
        bad_shift = int((exp.map(lambda x: x[0]).astype(str)
                         != d["shift"].astype(str).str.strip()).sum())
        bad_date  = int((exp.map(lambda x: str(x[1])).astype(str)
                         != d["date"].astype(str).str.strip()).sum())
        rec("shift not derived from startTime", bad_shift)
        rec("date  not derived from startTime", bad_date)

    # ---- 7. THE KIT CONSTRAINT, AS WRITTEN. A READY block may not start before the
    #         kit_ready stamped on its own row. (Re-read from the file, not the loop.)
    if "kit_ready" in rv.columns and "kit_status" in rv.columns:
        r = rv[rv["kit_status"].astype(str) == KIT_READY].copy()
        kr = pd.to_datetime(r["kit_ready"], errors="coerce")
        rec("READY blocks starting before kit",
            int(((r["startTime"] < kr) & kr.notna()).sum()))

    return ok, lines


def _selftest_verifier(og: pd.DataFrame, rv: pd.DataFrame) -> None:
    """PROVE THE GATE IS A GATE. Corrupt a COPY of the written output and require
    _check_schedule() to FAIL on it. Nothing on disk is touched -- the corruption
    lives and dies inside this function.

    We corrupt the ONE thing the old gate was STRUCTURALLY INCAPABLE of catching: we
    move a block to 6 h BEFORE ITS ORIGINAL PLANNED START. The old check read `early`
    straight out of the loop's own `candidate_start = max(orig_start, ...)`, so it was
    an arithmetic impossibility for it to fire, no matter what was in the output file.
    The new check reads the file. It must fire. If it does not, we are back where we
    started and we say so.
    """
    if rv.empty or og.empty:
        return
    bad = rv.copy()
    # Target a block by JOINING to the plan, so the corruption is defined against the
    # ORIGINAL start -- i.e. it is guaranteed to violate "no block pulled EARLIER".
    o = og.set_index(BLOCK_ID)["startTime"]
    cand = bad[bad[BLOCK_ID].isin(o.index)]
    if cand.empty:
        return
    i = cand.index[len(cand) // 2]
    bid = bad.loc[i, BLOCK_ID]
    dur = bad.loc[i, "endTime"] - bad.loc[i, "startTime"]
    bad.loc[i, "startTime"] = o.loc[bid] - pd.Timedelta(hours=6)
    bad.loc[i, "endTime"]   = bad.loc[i, "startTime"] + dur

    failed, lines = _check_schedule(og, bad)
    fired = [l.strip().split(":")[0].strip() for l in lines if "FAIL" in l]
    if failed:
        print("    !! SELF-TEST FAILED: the verifier PASSED an output in which a block "
              "starts 6 h")
        print("    !! BEFORE ITS OWN PLANNED START. THE GATE IS NOT A GATE. Do NOT trust "
              "the OK above.")
    else:
        print(f"    self-test: gate REJECTS a corrupted output  OK   "
              f"(block_id {bid} moved to 6 h before its plan -> "
              f"fired: {', '.join(fired) if fired else 'n/a'})")


def verify_invariants(cfg: dict, csv_path) -> bool:
    """E4. Re-read the WRITTEN OUTPUT and the ORIGINAL PLAN and re-derive the
    invariants from those two files. Uses NO variable from revise_curing_schedule().
    """
    print("\n  --- F1 INVARIANTS (re-derived from the WRITTEN FILE vs the ORIGINAL PLAN) ---")

    csv_path = pathlib.Path(csv_path)
    if not csv_path.exists():
        print(f"    << FAIL: {csv_path.name} was not written -- there is nothing to verify.")
        return False

    try:
        rv = pd.read_csv(csv_path, low_memory=False)
        # INDEPENDENT re-load. Not the frame the loop was handed -- the source, again.
        og = load_original_plan(cfg)
        og[BLOCK_ID] = range(len(og))
    except Exception as e:
        print(f"    << FAIL: could not re-read the output or re-load the plan ({e}).")
        return False

    # DTYPES. `pressNo` is "3609" in the plan and reads back out of the CSV as the
    # INTEGER 3609. Compare them raw and the join key silently splits in two. Coerce
    # both sides to the same shape FIRST -- an equality test between an int and a str
    # is not a failing check, it is a broken one.
    for d in (rv, og):
        d["startTime"] = pd.to_datetime(d["startTime"], errors="coerce")
        d["endTime"]   = pd.to_datetime(d["endTime"],   errors="coerce")
        for c in ("pressNo", "skuCode"):
            if c in d.columns:
                d[c] = d[c].astype(str).str.strip()
    if BLOCK_ID in rv.columns:
        rv[BLOCK_ID] = pd.to_numeric(rv[BLOCK_ID], errors="coerce").astype("Int64")
        og[BLOCK_ID] = og[BLOCK_ID].astype("Int64")

    keep_o = [BLOCK_ID, "pressNo", "skuCode", "qty", "cycleTime", "startTime", "endTime"]
    og_s = og[[c for c in keep_o if c in og.columns]]

    best_press = bool(cfg.get("best_press_curing", False))
    gap_fill   = bool(cfg.get("press_gap_fill", False))
    if best_press:
        # E4 for the best-press path: re-derive eligibility + mould caps INDEPENDENTLY
        # from routing.csv + the re-loaded plan, then check the best-press invariants.
        elig = _curing_press_eligibility(cfg)
        caps = _mould_caps_from_plan(og, cfg)
        print("  (best_press_curing ON -- pressNo/sequence change on purpose; checking "
              "routing-eligibility + mould-cap instead)")
        ok, lines = _check_schedule_bestpress(og_s, rv, elig, caps)
    elif gap_fill:
        # FIX B: gap-fill keeps pressNo but lets a kit-ready block occupy a hole ahead of a
        # kit-late one -> within-press ORDER/transitions change on purpose (checked as INFO).
        # Every hard invariant (duration, no-before-orig, no-before-kit, no double-book,
        # immutable fields) is still enforced.
        print("  (press_gap_fill ON -- within-press order changes on purpose; sequence/"
              "transition checks are INFO, all other invariants still HARD)")
        ok, lines = _check_schedule(og_s, rv, allow_reorder=True)
    else:
        ok, lines = _check_schedule(og_s, rv)
    for ln in lines:
        print(ln)
    print(f"  --- {'ALL INVARIANTS HOLD' if ok else 'INVARIANT VIOLATED -- SEE ABOVE'} ---")

    if best_press:
        # PROVE the best-press gate is a gate: move one block to a never-eligible
        # press "0" and require the checker to FAIL on it.
        bad = rv.copy()
        if len(bad):
            first_sku = str(bad.iloc[0]["skuCode"]).strip()
            if elig.get(first_sku):                 # only meaningful when routing exists
                bad.iloc[0, bad.columns.get_loc("pressNo")] = "0"
                st_ok, _ = _check_schedule_bestpress(og_s, bad, elig, caps)
                print(f"    [selftest] non-eligible-press corruption detected: "
                      f"{'YES (gate works)' if not st_ok else 'NO << the gate is blind'}")
    elif bool(cfg.get("verify_selftest", True)):
        _selftest_verifier(og_s, rv)
    return bool(ok)


def report_kit_shortfall(revised: pd.DataFrame):
    """FIX F2's payload. The number the old code could not have printed."""
    if revised.empty or "kit_status" not in revised.columns:
        return
    st = revised["kit_status"].astype(str)
    short = revised[st == KIT_SHORT]
    unk   = revised[st == KIT_UNKNOWN]
    q_short = int(pd.to_numeric(short["kit_short_qty"], errors="coerce").fillna(0).sum())
    q_unk   = int(pd.to_numeric(unk["kit_short_qty"],   errors="coerce").fillna(0).sum())
    total   = q_short + q_unk

    print(f"\n  KIT SHORTFALL: {total:,} tyres")
    print(f"    UNKNOWN (no building at all) : {q_unk:>7,}  across "
          f"{unk['skuCode'].nunique()} SKUs   <- routing hole, DATA")
    print(f"    SHORT   (partial)            : {q_short:>7,}  across "
          f"{short['skuCode'].nunique()} SKUs  <- opening WIP / capacity")
    if total == 0:
        print("    (every curing block is backed by real kit)")
    else:
        print(f"    {len(short) + len(unk):,} of {len(revised):,} blocks are cured from "
              f"kit that does not exist -- STILL PLACED (v6 behaviour, unchanged)")
    if not unk.empty:
        for sku, g in unk.groupby("skuCode"):
            print(f"      UNKNOWN  {sku}: {int(g['kit_short_qty'].sum()):,} tyres "
                  f"({len(g)} blocks) -- NO building lots exist for this SKU")


def report_past_horizon(revised: pd.DataFrame, horizon, enforce: bool):
    """FIX F3's payload."""
    print(f"\n  HORIZON: {horizon}   (enforce_strict_horizon={enforce})")
    if horizon is None or not enforce or "past_horizon" not in revised.columns:
        print("    not enforced -- nothing flagged")
        return
    ph = revised[revised["past_horizon"].astype(bool)]
    if ph.empty:
        print("    Blocks PAST_HORIZON: 0")
        return
    en = pd.to_datetime(ph["endTime"], errors="coerce")
    worst_h = (en.max() - pd.Timestamp(horizon)).total_seconds() / 3600.0
    print(f"    Blocks PAST_HORIZON : {len(ph):,} of {len(revised):,} "
          f"({100.0 * len(ph) / len(revised):.1f}%)")
    print(f"    Tyres  PAST_HORIZON : {int(pd.to_numeric(ph['qty'], errors='coerce').fillna(0).sum()):,}")
    print(f"    Latest finish       : {en.max()}  (+{worst_h:,.1f} h past the horizon)")
    print("    STILL PLACED -- config says flag, not stop. Nothing was moved or dropped.")


def report_green_tyre_aging(revised: pd.DataFrame, default_limit_h: float,
                            per_sku_limit: dict):
    """FIX F4's payload.

    WAS: phase 5 built the green tyres against the ORIGINAL press times. Phase 6
    then MOVED the presses -- and nothing re-aged anything. A green tyre built for
    a press slot that has since slipped 3 weeks is a SCRAP TYRE, and the schedule
    said nothing at all.

    WHY IT IS A DEFECT, NOT A POLICY: the plant has a green-tyre shelf life and
    the masters state it. A phase whose entire job is to move press times, and
    which never re-checks the one thing those times invalidate, is not making a
    trade-off -- it is not looking.

    REPORT-ONLY. Changing anything here would be a new rule (re-plan building,
    or refuse the move). Neither is ours to make. We measure, and we say.

    E3 CORRECTION. This used to age the block against `kit_ready` -- the NEWEST tyre
    in the kit -- and so reported the kit's MINIMUM age. The verdict below is now
    taken against `green_age_h`, which is the age of the OLDEST tyre in the kit. That
    is the tyre the shelf life actually condemns. The old (newest) figure is printed
    beside it so the size of the understatement is on the record, not in a comment.
    """
    if revised.empty or "green_age_h" not in revised.columns:
        return
    age = pd.to_numeric(revised["green_age_h"], errors="coerce")          # E3: OLDEST
    have = revised[age.notna()].copy()
    have["_age"] = age[age.notna()]
    if have.empty:
        print(f"\n  GREEN TYRE AGE AT PRESS: no measurable kit on any block "
              f"(every block is SHORT/UNKNOWN) -- nothing to age")
        return

    lim = have["skuCode"].map(per_sku_limit).fillna(default_limit_h).astype(float)
    over = have[have["_age"] > lim]
    qty = pd.to_numeric(have["qty"], errors="coerce").fillna(0)
    over_qty = int(pd.to_numeric(over["qty"], errors="coerce").fillna(0).sum())
    tot_qty = int(qty.sum())
    pct = (100.0 * over_qty / tot_qty) if tot_qty else 0.0

    print(f"\n  GREEN TYRE AGE AT PRESS  [E3: measured on the OLDEST tyre in the kit]")
    print(f"    median {have['_age'].median():.1f} h | worst {have['_age'].max():,.1f} h | "
          f"OVER-AGE {over_qty:,} tyres of {tot_qty:,} ({pct:.1f}%)   "
          f"[limit: {default_limit_h:.0f} h]")
    print(f"    measured on {len(have):,} of {len(revised):,} blocks "
          f"(the rest have no kit at all -- SHORT/UNKNOWN, see KIT SHORTFALL)")
    if not over.empty:
        print(f"    over-age BLOCKS: {len(over):,} across {over['skuCode'].nunique()} SKUs "
              f"-- these green tyres are past shelf life when the press takes them")

    # E3: what the OLD metric said, so the correction is auditable on the face of the run.
    if "green_age_newest_h" in revised.columns:
        agn = pd.to_numeric(revised["green_age_newest_h"], errors="coerce")
        hn = revised[agn.notna()].copy()
        if not hn.empty:
            hn["_age"] = agn[agn.notna()]
            limn = hn["skuCode"].map(per_sku_limit).fillna(default_limit_h).astype(float)
            on = hn[hn["_age"] > limn]
            oq = int(pd.to_numeric(on["qty"], errors="coerce").fillna(0).sum())
            tq = int(pd.to_numeric(hn["qty"], errors="coerce").fillna(0).sum())
            pn = (100.0 * oq / tq) if tq else 0.0
            print(f"    (pre-E3, measured on the NEWEST tyre in the kit: median "
                  f"{hn['_age'].median():.1f} h | over-age {oq:,} tyres ({pn:.1f}%))")
            print(f"    (the old metric UNDERSTATED the exposure by {over_qty - oq:,} tyres "
                  f"= {pct - pn:+.1f} points. The F5-seeded opening WIP is always the")
            print(f"     oldest lot in its kit and was therefore ALWAYS invisible to it.)")


def report_phantom_tyres(revised: pd.DataFrame):
    """S5. THE NUMBER THAT MUST NOT BE QUOTED WITHOUT ITS ASTERISK.

    (A) DEFECT (report). v6 places a curing block whether or not a green tyre exists
    for it, and this phase KEEPS placing them -- that is v6's rule and it is not mine
    to change. But every "% cured" KPI computed off this schedule COUNTS them. On the
    shipped data that is ~9.3% of the plan, and it is concentrated in the two SKUs the
    config itself lists in `excluded_items` because they have NO proc_time in the
    routing master. The DECLARED HOLE is being silently counted as OUTPUT.

    REPORT ONLY. Not one block is moved or dropped.
    """
    if revised.empty or "kit_status" not in revised.columns:
        return
    st = revised["kit_status"].astype(str)
    ph = revised[st.isin((KIT_SHORT, KIT_UNKNOWN))].copy()
    tot_qty = int(pd.to_numeric(revised["qty"], errors="coerce").fillna(0).sum())
    if ph.empty or tot_qty == 0:
        print("\n  [S5] every scheduled tyre is backed by a real green tyre. "
              "The '% cured' figures above are whole.")
        return

    blk_qty  = int(pd.to_numeric(ph["qty"], errors="coerce").fillna(0).sum())
    miss_qty = int(pd.to_numeric(ph["kit_short_qty"], errors="coerce").fillna(0).sum())
    n_sku = ph["skuCode"].nunique()

    print("\n  " + "!" * 72)
    print(f"  !! CURED WITHOUT A GREEN TYRE: {miss_qty:,} tyres "
          f"({100.0 * miss_qty / tot_qty:.1f}% of the {tot_qty:,}-tyre plan) "
          f"across {n_sku} SKUs")
    print(f"  !!   They sit inside {len(ph):,} blocks totalling {blk_qty:,} tyres "
          f"({100.0 * blk_qty / tot_qty:.1f}% of the plan) --")
    print(f"  !!   i.e. {len(ph):,} blocks are SCHEDULED and COUNTED, and "
          f"{miss_qty:,} of the tyres in them")
    print(f"  !!   WERE NEVER BUILT. No green tyre exists. The press is booked anyway.")
    print(f"  !!")
    print(f"  !!   ANY \"% cured\" FIGURE ABOVE OR DOWNSTREAM INCLUDES THEM.")
    print(f"  !!   SUBTRACT BEFORE QUOTING TO THE PLANT.")
    print(f"  !!")
    print(f"  !!   Worst SKUs (these are the config's own `excluded_items` -- the items")
    print(f"  !!   with NO proc_time in the routing master, i.e. the DECLARED HOLE):")
    top = (ph.groupby("skuCode")
             .agg(blocks=("qty", "size"),
                  block_tyres=("qty", "sum"),
                  never_built=("kit_short_qty", "sum"))
             .sort_values("never_built", ascending=False))
    for sku, r in top.head(6).iterrows():
        print(f"  !!     {sku:<22} {int(r['never_built']):>7,} tyres never built "
              f"({int(r['blocks']):,} blocks, {int(r['block_tyres']):,} tyres booked)")
    if len(top) > 6:
        print(f"  !!     ... and {len(top) - 6} more SKU(s), "
              f"{int(top['never_built'].iloc[6:].sum()):,} tyres")
    print(f"  !!")
    print(f"  !!   v6 PLACES THESE BLOCKS. SO DO WE. Nothing was moved or dropped --")
    print(f"  !!   this is a REPORT, not a rule change. The hole is in the DATA")
    print(f"  !!   (routing master), and it must be closed there, by the plant.")
    print("  " + "!" * 72)


def report_changeover_windows(blocked: pd.DataFrame, revised: pd.DataFrame,
                              reinserted: bool):
    """FIX F7's payload -- ALWAYS printed, whether or not we re-inserted."""
    if blocked is None or blocked.empty:
        print("\n  MOULD-CHANGE WINDOWS: none in the plan")
        return
    n = len(blocked)
    hrs = float(blocked["hours"].sum())
    by_kind = blocked.groupby("kind")["hours"].agg(["size", "sum"])
    print(f"\n  MOULD-CHANGE / CHANGEOVER WINDOWS: {n:,} blocks reserving {hrs:,.1f} h "
          f"of press time")
    for k, row in by_kind.iterrows():
        print(f"    {k:<12}: {int(row['size']):>4,} blocks  {row['sum']:>8,.1f} h")

    # How much of that reserved time does the REVISED schedule now sit on top of?
    idx = _index_blocked(blocked)
    consumed_h = 0.0
    hit_blocks = 0
    rv = revised.copy()
    rv["startTime"] = pd.to_datetime(rv["startTime"], errors="coerce")
    rv["endTime"]   = pd.to_datetime(rv["endTime"],   errors="coerce")
    for _, r in rv.iterrows():
        s, e = r["startTime"], r["endTime"]
        if pd.isna(s) or pd.isna(e):
            continue
        hit = 0.0
        for w0, w1 in idx.get(str(r["pressNo"]).strip(), ()):
            lo, hi = max(s, w0), min(e, w1)
            if hi > lo:
                hit += (hi - lo).total_seconds() / 3600.0
        if hit > 0:
            hit_blocks += 1
            consumed_h += hit
    pct = (100.0 * consumed_h / hrs) if hrs else 0.0
    if reinserted:
        print(f"    RE-INSERTED as pinned busy windows (cfg reinsert_changeover_blocks=True)")
        print(f"    press time still consumed by production: {consumed_h:,.1f} h "
              f"({hit_blocks:,} blocks)  {'OK -- windows respected' if consumed_h < 1e-6 else '<< FAIL'}")
    else:
        print(f"    NOT re-inserted (cfg reinsert_changeover_blocks=False -- the default).")
        print(f"    !! CONSUMED BY PRODUCTION: {consumed_h:,.1f} h of the {hrs:,.1f} h "
              f"reserved ({pct:.1f}%), across {hit_blocks:,} curing blocks.")
        print(f"    That press time is promised TWICE. Set reinsert_changeover_blocks: true")
        print(f"    to make the schedule LONGER and TRUE.")


# ------------------------------------------------------------------ floor-schedule augmentation
def curing_to_floor_rows(revised: pd.DataFrame) -> pd.DataFrame:
    """Convert revised curing rows into the floor-schedule schema used by
    Phase 5 (jkt_floor_endfwd_schedule / phase5_floor_schedule_updated.csv).
    One row per curing lot; the START shift / date are used (curing is a
    continuous press run, not split per shift)."""
    if revised is None or revised.empty:
        return pd.DataFrame(columns=FLOOR_SCHEMA)
    rows = []
    for _, r in revised.iterrows():
        st = pd.Timestamp(r["startTime"])
        en = pd.Timestamp(r["endTime"])
        if pd.isna(st) or pd.isna(en):
            continue
        shift, plant_day = _derive_shift_and_plant_day(st)
        rows.append({
            "date":         str(plant_day),
            "machine":      str(r.get("pressNo", "")),
            "shift":        shift,
            "item":         str(r.get("skuCode", "")),
            "item_type":    "CURED TYRE",
            "process":      "CURING",
            "department":   "CURING",
            "start_time":   st.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time":     en.strftime("%Y-%m-%d %H:%M:%S"),
            "produce_qty":  int(r.get("qty", 0) or 0),
            "UOM":          "NOS",
            "lot_id":       f"CURE_{r.get('plan_id', '')}",
        })
    return pd.DataFrame(rows, columns=FLOOR_SCHEMA)


# ============================================================================
# FIX F6 -- THE APPEND WAS NOT IDEMPOTENT: RE-RUNNING PHASE 6 CORRUPTED THE FILE
# ============================================================================
# WAS: append_curing_to_floor_schedule() read the existing floor schedule,
#      pd.concat'd the new curing rows onto it, and wrote it back. Every re-run
#      of phase 6 added ANOTHER full copy of all 6,975 curing rows. The file we
#      found already carried 6,975 curing rows inside 15,207 -- i.e. it had
#      already been through this at least once, and nobody could tell.
#
# WHY IT IS A DEFECT, NOT A POLICY: nobody chooses to double-count the entire
# press plan on the second run. It is an append that forgot it was re-runnable.
# It is also the WORST kind of bug -- it makes the output depend on how many
# times you ran the program, and it does it silently.
#
# FIX: drop the pre-existing CURING rows BEFORE appending. Phase 6 owns the
# curing rows in that file; it replaces its own output rather than piling onto
# it. The DB path (TRUNCATE + INSERT of the combined frame) had exactly the same
# hole and gets exactly the same guard.
# ============================================================================
def _drop_curing_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Remove rows that a previous phase-6 run appended. Phase 6 owns these."""
    if df is None or df.empty:
        return df
    mask = pd.Series(False, index=df.index)
    for col in ("department", "process"):
        if col in df.columns:
            mask |= df[col].astype(str).str.strip().str.upper().eq("CURING")
    if "lot_id" in df.columns:
        mask |= df["lot_id"].astype(str).str.strip().str.upper().str.startswith("CURE_")
    return df[~mask]


def append_curing_to_floor_schedule(cfg, curing_floor: pd.DataFrame):
    """Replace (not accumulate) phase 6's curing rows in BOTH the DB table
    jkt_floor_endfwd_schedule and outputs2/phase5_floor_schedule_updated.csv."""
    if curing_floor is None or curing_floor.empty:
        print("  no curing rows to append to floor schedule")
        return

    # ---- DB: read existing -> DROP OLD CURING -> concat -> TRUNCATE + INSERT ----
    try:
        from db_loader import get_engine, _resolve_source, OUTPUT_TABLES
        if _resolve_source(cfg) == "db":
            from sqlalchemy import inspect as _inspect, text as _text
            eng = get_engine(cfg)
            tbl = OUTPUT_TABLES["floor_schedule"]
            insp = _inspect(eng)
            existing = pd.DataFrame()
            if insp.has_table(tbl):
                existing = pd.read_sql(f"SELECT * FROM {tbl}", eng)
            # FIX F6: never pile a second copy of the press plan onto the first.
            n_before = len(existing)
            existing = _drop_curing_rows(existing)
            if n_before != len(existing):
                print(f"  [F6] DB: dropped {n_before - len(existing):,} stale CURING rows "
                      f"before append (idempotent)")
            # Align column sets so concat is safe regardless of which
            # extra columns Phase 5's output had.
            target_cols = list(existing.columns) if not existing.empty else FLOOR_SCHEMA
            for c in target_cols:
                if c not in curing_floor.columns:
                    curing_floor[c] = ""
            if existing.empty:
                combined = curing_floor[target_cols].copy()
            else:
                combined = pd.concat(
                    [existing[target_cols], curing_floor[target_cols]],
                    ignore_index=True,
                )
            with eng.begin() as conn:
                conn.execute(_text(f"TRUNCATE TABLE `{tbl}`"))
            combined.to_sql(tbl, eng, if_exists="append", index=False, chunksize=5000)
            print(f"  DB: {tbl} now has {len(combined):,} rows (+{len(curing_floor):,} curing)")
    except Exception as e:
        print(f"  [WARN] DB append (floor schedule) skipped: {e}")

    # ---- File: outputs2/phase5_floor_schedule_updated.csv ----
    fp = OUTPUTS / "phase5_floor_schedule_updated.csv"
    try:
        if fp.exists():
            existing_csv = pd.read_csv(fp, low_memory=False)
            target_cols = list(existing_csv.columns)
            # FIX F6: idempotent -- phase 6 REPLACES its own rows.
            n_before = len(existing_csv)
            existing_csv = _drop_curing_rows(existing_csv)
            if n_before != len(existing_csv):
                print(f"  [F6] File: dropped {n_before - len(existing_csv):,} stale CURING rows "
                      f"before append (idempotent -- this file had been double-written)")
            for c in target_cols:
                if c not in curing_floor.columns:
                    curing_floor[c] = ""
            combined_csv = pd.concat([existing_csv, curing_floor[target_cols]], ignore_index=True)
        else:
            combined_csv = curing_floor.copy()
        _atomic_to_csv(combined_csv, fp)
        print(f"  File: {fp.name} now has {len(combined_csv):,} rows "
              f"({len(curing_floor):,} of them CURING)")
    except Exception as e:
        print(f"  [WARN] CSV append (floor schedule) skipped: {e}")


# ------------------------------------------------------------------ main
def main():
    print("\n" + "=" * 65)
    print("  PHASE 6 - Curing Schedule Revision (vs Building)")
    print("=" * 65)
    cfg = _load_cfg()

    raw = _load_raw_plan(cfg)
    plan = raw[(raw["skuCode"].str.upper() != "CHANGEOVER") & (raw["qty"] > 0)] \
             .reset_index(drop=True)
    # E4: stable identity for every block. It is now PERSISTED in the output, because
    # verify_invariants() re-reads that output OFF THE DISK and has to be able to join
    # it back to the plan. (It used to be `_row_id`, computed here, consumed from the
    # loop's own in-memory frame, and dropped before writing -- which is precisely why
    # the old check could only ever agree with itself.)
    #   The rule is deterministic -- position in the FILTERED plan, in loader order --
    #   so verify re-derives it from an INDEPENDENT re-load and the join then proves
    #   the two agree on pressNo/skuCode/qty/cycleTime for every block.
    plan[BLOCK_ID] = range(len(plan))
    print(f"  Original curing rows: {len(plan):,}")
    if plan.empty:
        print("  [WARN] no curing rows; nothing to revise")
        return 0

    # F7: the press time the scheduler drops but the plant still spends.
    blocked = split_blocked_windows(raw)
    # F3: the horizon config already told us to enforce.
    horizon = plan_horizon(raw)
    plan_start = pd.to_datetime(plan["startTime"], errors="coerce").min()
    print(f"  Plan window: {plan_start}  ->  {horizon}")

    build = load_building_schedule(cfg)
    if build.empty:
        print("  [WARN] no Building schedule rows found - mirroring original plan")
    # BUILDING-PLAN ECHO: the building subset MUST be non-empty, else the KitPool is empty and
    # every cure reads phantom -- a silent, total starvation. Fail loudly instead.
    if bool(cfg.get("building_plan_echo", False)) and build.empty:
        raise SystemExit(
            "  [BUILDING ECHO] FATAL: building subset is EMPTY under building_plan_echo -- phase5 "
            "emitted no building rows (schema drift, or the echo did not run). Curing would starve "
            "silently. Aborting so the failure cannot hide.")

    # ========================================================================
    # B1 -- (B) POLICY. THE DEFAULT WAS BACKWARDS.
    # ========================================================================
    # WAS: cfg.get("seed_kitpool_from_inventory", True)
    #
    # config.yaml describes this flag, IN ITS OWN WORDS, as
    #     "(B), not (A), because v6's rule ('the pool starts empty') is executed
    #      correctly -- we want a different rule."
    # An admitted (B) POLICY MUST DEFAULT TO v6's BEHAVIOUR. This one defaulted to
    # OURS. That is the exact breach the standing rule forbids, and it is not
    # cosmetic -- it MOVES THE SCHEDULE:
    #     SEED OFF (v6): makespan 2026-07-08 09:42 | kit shortfall 37,857 | PAST_HORIZON 468
    #     SEED ON  (v2): makespan 2026-07-07 12:03 | kit shortfall 33,933 | PAST_HORIZON 435
    #                             ^^ 21.7 HOURS of makespan
    #
    # The CODE fallback is now False = v6. What config.yaml SHIPS is the config
    # owner's decision, not this file's -- and config.yaml currently ships `true`,
    # which is a deliberate, declared, reviewable choice. That is the whole point:
    # the policy is now VISIBLE IN CONFIG instead of hidden in a default argument.
    # ========================================================================
    gt2sku = load_gt_code_to_sku(cfg)
    gt_limit, gt_limit_by_sku = green_tyre_max_age_h(cfg, gt2sku)
    _all_gt = None
    if bool(cfg.get("kitpool_key_by_gt", False)):
        cfg["_sku2gtkey"], _all_gt = load_sku_to_gt_code(cfg)
        print(f"  [SHARED-GT] kit pool keyed by GT code: {len(cfg['_sku2gtkey']):,} SKUs -> "
              f"{len(_all_gt):,} GT codes")
    gt_all = pd.DataFrame()
    if bool(cfg.get("seed_kitpool_from_inventory", False)):    # B1: was True. v6 = False.
        seed, gt_all = load_inventory_seed(cfg, plan_start, gt2sku,
                                           gt_limit, gt_limit_by_sku, _all_gt)
        if not seed.empty:
            build = (pd.concat([build, seed], ignore_index=True)
                     if not build.empty else seed)
    else:
        print("  [F5] seed_kitpool_from_inventory=False (v6's rule: the pool starts EMPTY)")
        print("       -- opening WIP is IGNORED by the kit pool. phase1c still NETS "
              "against it,")
        print("       so BUILDING makes ~4,127 fewer green tyres than curing consumes and "
              "phase6")
        print("       can see none of them. That shortfall is an ARTEFACT OF THIS FLAG, "
              "not a")
        print("       netting error. Set seed_kitpool_from_inventory: true in config to "
              "close it.")

    revised = revise_curing_schedule(plan, build, cfg=cfg,
                                     blocked=blocked, horizon=horizon)
    print(f"  Revised curing rows : {len(revised):,}")

    # ---- BEST-PRESS (opt-in): mould-capped earliest-finish re-placement ----
    # Off by default -> phase6 is the verbatim v6 port. When ON, each block is
    # re-placed onto the routing-eligible press with the earliest finish, capped by
    # the plan's own proven mould concurrency (never invents a mould). Plant DATA
    # (plan.csv/routing.csv) is READ ONLY.
    if bool(cfg.get("best_press_curing", False)):
        if bool(cfg.get("converge_building_to_press", False)):
            print("  [BEST-PRESS] NOTE: converge_building_to_press is ON; best-press "
                  "re-places AFTER the S2 press-floor -- run it on the single-pass path.")
        revised = best_press_reassign(revised, plan, cfg, plan_start)

    # -------- write CSV (atomic) --------
    # E4: the CSV is written BEFORE it is verified, because the verifier now checks
    # THE FILE -- not the frame the loop happened to leave in memory. `block_id` is
    # part of the schema now; it is the join key that makes the check possible.
    csv_path = OUTPUTS / "phase6_curing_revision_updated.csv"
    _atomic_to_csv(revised, csv_path)
    print(f"\n  Wrote: {csv_path}")

    # ---- E4: the algorithm must still be the algorithm -- PROVEN, not asserted ----
    verify_invariants(cfg, csv_path)

    # ---- F2 / F3 / F4 / F7 / B2 / S5: SAY WHAT HAPPENED --------------------
    report_kit_shortfall(revised)
    report_past_horizon(revised, horizon,
                        bool(cfg.get("enforce_strict_horizon", False)))
    report_green_tyre_aging(revised, gt_limit, gt_limit_by_sku)
    report_changeover_windows(blocked, revised,
                              bool(cfg.get("reinsert_changeover_blocks", False)))
    reconcile_wip_with_phase1c(gt_all)

    # F7: the windows are DATA. Emit them so a planner can see the press calendar
    # the LP actually reserved, whether or not we enforced it.
    if not blocked.empty:
        bp = OUTPUTS / "phase6_changeover_windows.csv"
        _atomic_to_csv(blocked, bp)
        print(f"  Wrote: {bp}")

    # -------- write to MySQL (TRUNCATE + INSERT) --------
    try:
        from db_loader import get_engine, _resolve_source
        if _resolve_source(cfg) == "db":
            from sqlalchemy import inspect as _inspect, text as _text
            eng = get_engine(cfg)
            tbl = "jkt_curing_schedule_revised"
            insp = _inspect(eng)
            if insp.has_table(tbl):
                with eng.begin() as conn:
                    conn.execute(_text(f"TRUNCATE TABLE `{tbl}`"))
                revised.to_sql(tbl, eng, if_exists="append", index=False, chunksize=5000)
                print(f"  DB write: {tbl} refreshed ({len(revised):,} rows, TRUNCATE+INSERT)")
            else:
                revised.to_sql(tbl, eng, if_exists="replace", index=False, chunksize=5000)
                print(f"  DB write: {tbl} CREATED ({len(revised):,} rows)")
    except Exception as e:
        print(f"  [WARN] DB write skipped: {e}")

    # -------- append curing rows to the floor schedule (F6: idempotent) -----
    curing_floor = curing_to_floor_rows(revised)
    print(f"  Curing rows to append to floor schedule: {len(curing_floor):,}")
    append_curing_to_floor_schedule(cfg, curing_floor)

    # -------- audit summary --------
    try:
        n_delayed = revised["remarks"].astype(str).str.contains("REVISED").sum()
        print(f"\n  Lots delayed: {int(n_delayed):,} / {len(revised):,}")
        o_st = pd.to_datetime(plan["startTime"], errors="coerce")
        o_en = pd.to_datetime(plan["endTime"],   errors="coerce")
        en   = pd.to_datetime(revised["endTime"], errors="coerce")
        slip_d = (en.max() - o_en.max()).total_seconds() / 86400.0
        print(f"  Makespan  : {o_st.min()} -> {en.max()}")
        print(f"              (original plan ended {o_en.max()}; {slip_d:+,.1f} days)")
    except Exception as e:
        print(f"  [WARN] summary skipped: {e}")

    # S5 -- LAST THING ON THE SCREEN, DELIBERATELY. Every KPI printed above this
    # line counts tyres that were never built. The reader must not get to the end of
    # the run without being told.
    report_phantom_tyres(revised)

    print(f"\n[OK] Phase 6 done")
    return 0


if __name__ == "__main__":
    import sys as _sys
    import traceback as _tb
    try:
        _rc = main()
        raise SystemExit(_rc if _rc is not None else 0)
    except SystemExit:
        raise
    except Exception:
        _sys.stderr.write(chr(10) + "!! PHASE CRASHED !!" + chr(10))
        _sys.stderr.flush()
        _tb.print_exc()
        _sys.exit(1)
