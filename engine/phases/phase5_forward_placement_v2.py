#!/usr/bin/env python3
"""
phase5_forward_placement.py -- Forward MRP placement with JIT + FRC campaigns.

KEY CHANGES (FRC release):
  1. Dispatch sort: (first_need asc, lot_id) -> natural interleave.
  2. Strict JIT placement -- every lot scheduled at target_finish = first_need - min_aging.
     Predecessor wins if pred.finish + min_aging > target_start.
  3. FRC campaigns placed atomically as one block on FRC machine.
     Same wire consecutive -> no cooldown. Different wire -> 6 h cool-down.
  4. Same-item cap implicit via (first_need asc) dispatch.

Optimised with bisect for first_fit + insort, and precomputed item aging in ns.
"""
from __future__ import annotations
import sys, pathlib, yaml, re, bisect, os, math
import pandas as pd
import numpy as np
from collections import defaultdict, deque

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS  = ROOT/"inputs"
OUTPUTS = ROOT/"outputs2"           # NEW WRITE LOCATION
OUTPUTS.mkdir(parents=True, exist_ok=True)
# v36: outputs written only when their rule is on -- clear them so a run never ships the previous run's file
for _stale in ("phase5_spool_consumed.csv", "phase5_aging_no_fresh_supply.csv"):
    (OUTPUTS / _stale).unlink(missing_ok=True)
PRIOR_OUTPUTS = ROOT/"outputs"      # Phase 1a / 1.5 outputs (already-run, re-used)
RUN_SUFFIX = "_updated"


def _suff(name):
    """Insert _updated before the extension (handles .csv.gz, .xlsx, .csv)."""
    if name.endswith(".csv.gz"):
        return name[:-7] + RUN_SUFFIX + ".csv.gz"
    if name.endswith(".csv"):
        return name[:-4] + RUN_SUFFIX + ".csv"
    if name.endswith(".xlsx"):
        return name[:-5] + RUN_SUFFIX + ".xlsx"
    return name + RUN_SUFFIX

import re as _re

DEFAULT_CHANGEOVER_MIN = 15
MIXING_CHANGEOVER_MIN  = 2
INTRA_CAMP_CHANGEOVER_MIN = 0
PRE_CURING_BUFFER_H    = 72.0
RESPECT_LFT            = False
LFT_GRACE_HOURS        = 24
MAX_HORIZON_DAYS       = 365

# === TARGET WIP HOURS (rolling-buffer maintenance per item-type) ===
# Producer must start no LATER than (consumer_first_need - target_wip_hours).
# This keeps a N-hour buffer alive throughout the schedule — plant never lets
# WIP deplete to zero before producing replacement.
TARGET_WIP_HOURS = {
    "FINAL COMPOUND":               72.0,   # avg max_aging 108h -> 72h buffer is safe
    "SideWall":                     24.0,
    "Tread":                        24.0,
    "Inner Liner":                  24.0,
    "Cap Strip":                     8.0,   # short aging 24h, tight buffer
    "Carcass":                      16.0,   # 24h max_aging - 8h safety = 16h buffer
                                            # Stage-1 produces ahead so Stage-2 (6 mach) is fed
                                            # continuously from carcass WIP pool of 15 Stage-1 machines
    "Green Tyres":                   8.0,   # CTP #14: this comment used to say "48h max_aging".
                                            # It was WRONG. The code executes AGE_OVERRIDE_H's
                                            # 72 h (v6 default) or cfg green_tyre_max_age_h (the
                                            # plant says 24 h; phase2 already uses 24 h). There is
                                            # ONE source of truth and it is AGE_OVERRIDE_H — not
                                            # this comment. The 8 h buffer below is unchanged.
    "Bead Bundle":                  12.0,
    "Bead Apex":                    12.0,
    "Rubberized Steel Belt":        12.0,
    "Rubberized Ply":               12.0,
    "Rubberized Steel Belt-mother roll": 24.0,
    "Rubberized Ply-mother roll":   24.0,
    "Pre Cap Strip":                 8.0,
    "Chaffer":                      12.0,
    "Steel Belt Edge Strip":        12.0,
    "Steel Belt":                   12.0,
    "Apex":                          8.0,
    "Ply":                          12.0,
    "CALANDARED ROLL":              24.0,
    "PRE CUT ROLL MATERIAL":         8.0,
    "Bead Wire":                    24.0,
}

# V6_wave: ASAP for upstream / LFT for terminal building+curing.
TERMINAL_DEPT_TOKENS = ("BUILD", "TBM", "CURING")

# === PARENT-AWARE CHANGEOVER (cutter family) ===
PARENT_AWARE_CUTTERS = {"WBC", "WBC NEW", "LTBC", "LTBCNew", "LTBC NEW", "HTBC", "FISCHER"}
_PARENT_RE = _re.compile(r'^([A-Z]+\d+(?:_B\d+|_OUTER)?)')

def parent_of(item_code):
    if not item_code: return ""
    m = _PARENT_RE.match(str(item_code).strip())
    return m.group(1) if m else str(item_code).strip()

# === PER-MACHINE CHANGEOVER RULES (item-type-aware extruders + flat) ===
# Triplex/Quadraplex: same item-type = 5 min, cross-type (e.g. SW<->Tread) = 50 min
TRIPLEX_LIKE = {"TRIPLEX", "QUADRAPLEX"}
# Duplex: flat 5 min always
DUPLEX_FLAT_MIN = 5
# Dual / Quintuplex: flat 6 min always
DUAL_FLAT_MIN = 6
QUINTUPLEX_FLAT_MIN = 6

# === CUTTER MOTHER-ROLL MODEL (production-scheduling-realistic for tyre cutters) ===
# Plant reality: when a mother roll (e.g., CPJ120 = 400,000 MM lot from FRC) is
# loaded onto a cutter, the operator runs ALL the dim-variant cuts they can from
# THAT roll (different widths/angles) before swapping. 3 min between cuts (just
# re-set width/angle). 15 min to swap to a new mother roll (any new lot_id,
# even same parent, requires physical mount/unmount = different MHE).
MOTHER_ROLL_MAX_MM = 400_000.0   # mpq_max for Rubberized Steel Belt-mother roll
LFT_PRESSURE_THRESHOLD_H = 12.0  # if other-parent lot has <12h slack, force switch
# Per-cutter runtime state (initialized in main()):
#   cutter_loaded_lot_id[m]    -> str  (specific FRC mother-roll lot loaded)
#   cutter_loaded_parent[m]    -> str  (e.g., "CPJ120")
#   cutter_roll_remaining_mm[m] -> float (MM left on the loaded mother roll)


def compute_changeover_for_cutter(machine, prev_item, curr_item, curr_qty,
                                   mm_per_cut_map,
                                   cutter_loaded_parent, cutter_loaded_lot_id,
                                   cutter_roll_remaining_mm,
                                   curr_lot_id=None):
    """Cutter-specific changeover with mother-roll life-cycle model.
    Returns (changeover_min, mm_consumed, will_swap_roll).
    """
    if not prev_item:  # first lot on machine — no changeover, fresh roll
        mother_roll_parent, mm_per_unit = mm_per_cut_map.get(curr_item, (None, 0.0))
        mm_needed = curr_qty * mm_per_unit
        return 0, mm_needed, True  # treat first lot as "loading roll"
    parent_curr = parent_of(curr_item)
    parent_loaded = cutter_loaded_parent.get(machine, "")
    mother_roll_parent, mm_per_unit = mm_per_cut_map.get(curr_item, (None, 0.0))
    mm_needed = curr_qty * mm_per_unit
    roll_left = cutter_roll_remaining_mm.get(machine, 0.0)
    # Case A: same parent loaded AND roll has enough MM left -> 3 min cut re-set
    if parent_loaded == parent_curr and roll_left >= mm_needed:
        return 3, mm_needed, False  # no swap, just adjust width/angle
    # Case B/C: must swap to a new mother roll (15 min)
    return 15, mm_needed, True


# ═══ CTP DEVIATION #3 — THE PLANT CHANGEOVER MATRIX ════════════════════════════════
# v6 has NO changeover matrix: `compute_changeover_min` below derives setup minutes from
# machine NAMES (WBC / LTBC / HTBC / FISCHER / DUPLEX / TRIPLEX / …). NONE of CTP's 123
# numeric machine IDs match ANY of those branches — so under v6's own rule EVERY CTP
# machine falls to a flat DEFAULT_CHANGEOVER_MIN = 15 (or 2 for mixing). The user supplied
# a real plant matrix and instructed it be used.
#
# Keyed on (LINE, from_item, to_item). The matrix's `machine` column holds 22 LINE NAMES
# ("4 RC Calendar", "Belt Cutter PCR") while the routing holds numeric IDs ("901", "3409") —
# ZERO string overlap. The bridge is `machine_to_changeover_line` in config.yaml, built from
# the plant's own machine list. See adapt_inputs.changeover().
#
# Both populated in main(). Empty dicts = pure v6 behaviour.
CHANGEOVER_LUT:  dict[tuple[str, str, str], float] = {}   # (line, from_item, to_item) -> min
MACHINE_TO_LINE: dict[str, str] = {}                       # routing machine id -> line name
MATRIX_MISSING_MIN: dict[str, float] = {}                  # line -> minutes for a pair the matrix lacks
MATRIX_MISSING_BY_TYPE: dict[str, tuple] = {}              # line -> (minutes same item type, minutes different type)
BUTYL_RULE = None                                            # BR-V2 butyl_rule.ButylRule, set in main()


def compute_changeover_min(machine, dept, prev_item, prev_itype, curr_item, curr_itype):
    """Returns changeover minutes for THIS lot (after prev lot on the same machine).
    Production-scheduling-realistic rule set per machine.
    NOTE: For CUTTER family, this is a FALLBACK; the dispatcher uses
    compute_changeover_for_cutter() which is parent+roll-state-aware.
    """
    if not prev_item:  # first lot on machine — no changeover
        return 0
    # CTP: the real plant matrix WINS wherever it has a rule for this (line, item-pair).
    # A machine with no line, or a pair the matrix does not carry, falls through to v6's
    # own rules below, unchanged.
    if CHANGEOVER_LUT:
        line = MACHINE_TO_LINE.get(str(machine or "").strip().strip('"').strip())
        if line:
            hit = CHANGEOVER_LUT.get(
                (line, str(prev_item).strip(), str(curr_item).strip()))
            if hit is not None:
                return hit
            if line in MATRIX_MISSING_BY_TYPE:            # BR-31: missing pair -> by item type (SW <-> SP)
                _same, _cross = MATRIX_MISSING_BY_TYPE[line]
                return (_same if str(prev_itype or "").strip().upper() == str(curr_itype or "").strip().upper()
                        else _cross)
            if line in MATRIX_MISSING_MIN:                # BR-24: pair not in the matrix -> plant default
                return MATRIX_MISSING_MIN[line]
    m_up = str(machine or "").upper()
    d_up = str(dept or "").upper()
    # Cutter family — parent-aware fallback (replaced by full mother-roll model in dispatcher)
    if str(machine).strip() in PARENT_AWARE_CUTTERS:
        return 3 if parent_of(prev_item) == parent_of(curr_item) else 15
    # Duplex — flat 5
    if "DUPLEX" in m_up:
        return DUPLEX_FLAT_MIN
    # Quintuplex — flat 6
    if "QUINTUPLEX" in m_up:
        return QUINTUPLEX_FLAT_MIN
    # Triplex / Quadraplex — item-type-aware
    if any(t in m_up for t in TRIPLEX_LIKE):
        if str(prev_itype).strip() == str(curr_itype).strip():
            return 5
        return 50
    # Dual — flat 6 (be careful: must not match "DUAL" inside other names; check token)
    if m_up == "DUAL" or m_up.startswith("DUAL "):
        return DUAL_FLAT_MIN
    # Mixing / Final Mixing — 2 min (existing behavior)
    if "MIX" in d_up:
        # BR-V2 (butyl_rule.py): a broken butyl-friendly sequence costs a clean-out cycle INSTEAD
        # of the changeover
        if BUTYL_RULE is not None:
            _cmin, _ = BUTYL_RULE.cleanout_minutes(prev_item, curr_item, machine)   # BR-V10: 1 batch of prev
            if _cmin is not None:
                return _cmin
        return MIXING_CHANGEOVER_MIN
    # Default
    return DEFAULT_CHANGEOVER_MIN


def build_mm_per_cut_map(bom_df):
    """From BOM, build: cut_item_code -> (mother_roll_parent_code, mm_per_unit).
    Looks for BOM rows where Parent looks like 'CPJ120-108MM/29°' (dim-variant)
    and child is the mother roll (e.g., 'CPJ120') with child_Unit == MM.
    """
    out = {}
    if bom_df is None or bom_df.empty:
        return out
    # The BOM row format: Parent (cut), child (mother roll), child_quantity (MM per cut)
    for _, r in bom_df.iterrows():
        par = str(r.get("Parent", "")).strip()
        ch  = str(r.get("child", "")).strip()
        unit = str(r.get("child_Unit", "")).strip().upper()
        try:
            qty = float(r.get("child_quantity", 0))
        except (TypeError, ValueError):
            qty = 0
        if not par or not ch or qty <= 0 or unit != "MM":
            continue
        # Heuristic: a "cut variant" parent has a hyphen-dimension suffix like -108MM/
        if "-" not in par or "MM" not in par.upper():
            continue
        # If we already have a row for this cut item, keep first
        if par in out:
            continue
        out[par] = (ch, qty)
    return out

# === AEST CAPACITY-AWARE for compounds (replaces blanket NO_AEST) ===
# Old behavior: MASTER/FINAL COMPOUND bypass AEST → ASAP from schedule_open → 706h
# producer-consumer gaps when consumer is pulled late by downstream bottleneck.
# New behavior: target start = max(earliest, consumer_first_need - max_aging - safety)
# This keeps mixer fed but never produces > max_aging ahead of consumer.
NO_AEST_ITEM_TYPES = set()  # disabled — use capacity-aware AEST for everyone
AGING_SAFETY_BUFFER_H = 6   # 6h buffer before producer can age out

# === PRODUCTION DE-PULSE (binding feeders -> JIT-to-deadline) ===
# Deadlines are already smooth daily (phase-2 de-pulse), but AEST pulls feeder
# production EARLY, which fills the shared upstream machines with future demand
# and DEFERS the near-term feeders into bursts (e.g. tread makes nothing 05-03/04/05
# then dumps 2.5 days on 05-06) -> kit-readiness lumps -> building dips. Placing the
# binding feeders JIT (finish at their already-smooth deadline) makes their production
# track the level daily demand, so kits flow evenly. Mixing/FRC are intentionally
# excluded (over-splitting them caused changeover/util blow-ups before).
FEEDER_JIT_TYPES = {"Tread", "SideWall", "Inner Liner", "Rubberized Ply",
                    "Cap Strip", "Pre Cap Strip"}
FEEDER_JIT_BUFFER_H = 4.0   # finish a few hours before the deadline (small safety)


# ═══ CTP DEVIATION #27 — THE CASE-MISMATCH LOOKUP: TWO RULES THE CODE NEVER RAN ═══════
# (A) DEFECT in the LOOKUP. (B) POLICY in the ACTIVATION. They are NOT the same thing and
# they are handled differently below. Read this whole block before touching either.
#
# THE DEFECT. phase5:~567 builds the lot->item_type map with `.str.upper()`:
#       lot_item_type = dict(zip(lots["lot_id"], lots["item_type"]....str.upper()))
# Both constants above are keyed in MIXED CASE. So:
#       "TREAD" in FEEDER_JIT_TYPES              -> False, always
#       TARGET_WIP_HOURS.get("STEEL BELT", 0.0)  -> 0.0,   always
# MEASURED on the CTP lot table (6,715 lots, 15 distinct item_types):
#       FEEDER_JIT_TYPES : 0 of 6 keys reachable -> the PRODUCTION DE-PULSE path has
#                          NEVER EXECUTED, on a single lot, ever.
#       TARGET_WIP_HOURS : 3 of 22 keys reachable, and ONLY because they happen to be
#                          typed in caps in the source: FINAL COMPOUND, CALANDARED ROLL,
#                          PRE CUT ROLL MATERIAL. The other 19 silently return 0.0 -> JIT.
# THE SAME BUG IS IN BTP v6 (v6_wave_p2/phases/phase5_forward_placement_v2.py:53/217/492).
# So NEITHER PLANT HAS EVER RUN THESE PATHS. There is no "restoring" them: switching them
# on ACTIVATES UNTESTED CODE. A lookup that silently misses is never acceptable, so the
# lookup is repaired UNCONDITIONALLY (the *_U maps below). The RULES, however, are
# behaviour nobody has ever observed, so they go behind flags that DEFAULT OFF.
#
# WITH THE FLAG OFF WE USE THE ORIGINAL MIXED-CASE CONSTANT ON PURPOSE. That is the only
# way to reproduce today's observed schedule BIT-FOR-BIT: "off" must mean the 3 accidental
# caps keys still hit and nothing else does. It is an accident, but it is the accident that
# is in production, and preserving it is the point of a default-off flag.
#
# ══ MEASURED 2026-07-14. BOTH FLAGS STAY OFF. THE REASONS ARE DIFFERENT. ═════════════
#
# enable_feeder_jit -> ON. IT WORKS PERFECTLY ON ITS OWN 4 ITEM TYPES AND WRECKS THE PLANT.
#   It binds 796 lots: Tread 231, SideWall 209, Inner Liner 220, Cap Strip 136. ("Rubberized
#   Ply" and "Pre Cap Strip" DO NOT EXIST on CTP -- they are BTP item_types.)
#                             OFF (shipped)      ON
#     Tread   % LOTS dead        3.3%           0.0%      <- the rule DOES what it claims
#     SideWall                   6.8%           0.0%
#     Inner Liner               10.6%           0.0%
#     Cap Strip                  2.9%           0.0%
#     ---------------------------------------------------------------------------------
#     Steel Belt                18.2%          71.2%      <- and then it kills everything
#     Ply                       19.8%          72.0%         ELSE
#     SLITTED MATERIAL          15.2%          59.2%
#     FINAL COMPOUND             2.5%          29.9%
#     ALL LOTS dead             12.9%          47.1%      (645 -> 2,351 lots)
#     makespan                  31.66 d        32.75 d
#     cured in 30 d             97.67%         95.86%
#     press starvation (med/max) 24.0/48.0 h   42.0/76.1 h
#     cutter 801 booked+remake   99.1% of 720  130.7% of 720
#   WHY. Steel Belt + Ply are 3,327 lots -- 62% of the upstream lots and 87.5% of the dead
#   ones -- and THEY ARE NOT IN FEEDER_JIT_TYPES. Delaying tread/SW/IL/cap-strip to 4 h
#   before their deadline delays the GREEN TYRE that consumes them; the belt and ply that
#   were already placed early then sit longer waiting for that building slot, and rot. The
#   rule does not remove the rot -- IT MOVES IT, from 796 lots onto 3,327. This is the
#   textbook cost of JIT-ing a NON-constraint in isolation while its co-feeders stay on the
#   old early path. To be safe on CTP it would have to cover the WHOLE kit, not 4 of 6
#   feeder types -- and that is a NEW rule, not this one. NOT DONE. NOT A KNOB.
#
# enable_target_wip -> ON. IT IS INERT ON CTP. Schedule is BYTE-FOR-BYTE IDENTICAL to OFF
#   (all 7 phase5/phase6 CSVs, md5-verified, at schedule_open_h 24 AND 0). Two independent
#   reasons, EITHER of which alone kills it:
#     1. THE :1633 GUARD. `if item_max_h < target_wip_h_default + _headroom_h -> 0.0`, with
#        _headroom_h = AGING_SAFETY_BUFFER_H(6) + 8 = 14.0. Steel Belt / Ply / Steel Belt
#        Edge Strip have max_aging 24 h: 24 < 12 + 14 = 26 -> TRUE -> target forced to 0.0
#        EVEN WITH THE KEY REACHABLE. The three biggest keys are dead on arrival.
#        (NB the comment on :1622 says "16h headroom" and the code computes 14.0. The
#         comment is STALE -- v6's AGING_SAFETY_BUFFER_H was 8. NOT changed: moving 6 -> 8
#         is RE-TUNING v6's number, which is a plant question, not a knob.)
#     2. THE CLAMP NEVER FIRES. `wip_cap_ns = LFT - dur - target_wip` is a LATE bound and is
#        only applied when `aest_ns > wip_cap_ns`. AEST is by construction an EARLY start.
#        MEASURED over all 5,436 upstream lots, for EVERY item_type incl. the 6 the repair
#        newly reaches: the clamp binds on ZERO of them. 0/5,436.
#   So the repaired lookup buys exactly nothing here. It is still a repair -- a lookup that
#   silently misses is never acceptable, and on a plant whose AEST runs tighter it WOULD
#   fire. But on CTP TODAY it is a no-op, and the flag stays off because there is no
#   evidence to turn it on with.
#
# THE 22 VALUES AND FEEDER_JIT_BUFFER_H = 4.0 ARE v6's OWN NUMBERS, TUNED FOR BTP. They are
# NOT re-tuned here. If one is wrong for CTP that is a question for the plant, not a knob.
# NOTHING WAS TUNED TO MAKE EITHER FLAG LOOK GOOD.
def _upper_keys(d, name):
    out = {}
    for k, v in d.items():
        ku = str(k).strip().upper()
        if ku in out and out[ku] != v:
            raise ValueError(f"{name}: case-collision on {ku!r} with conflicting values")
        out[ku] = v
    return out

FEEDER_JIT_TYPES_U = {str(t).strip().upper() for t in FEEDER_JIT_TYPES}
TARGET_WIP_HOURS_U = _upper_keys(TARGET_WIP_HOURS, "TARGET_WIP_HOURS")


def _is_terminal_dept(dept_str: str) -> bool:
    d = str(dept_str or "").upper()
    return any(tok in d for tok in TERMINAL_DEPT_TOKENS)


def _read(p):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(p, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(p)

# ============================================================
# DB-AWARE MASTER LOADER (auto-inserted by DB-only migration)
# Routes `load master` calls to MySQL when cfg["data_source"]=="db".
# Falls back to the existing local CSV read otherwise.
# ============================================================
def _db_or_csv(_key, _cfg=None):
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        from db_loader import load_input
        return load_input(_key, _cfg)
    except Exception as _e:
        # Fall back to CSV when DB or db_loader is unavailable
        _fp = (_cfg or {}).get("files", {})
        _fname = _fp.get(_key)
        if _fname is None:
            raise
        return _read(INPUTS / _fname)


def load_cfg(): return yaml.safe_load(open(ROOT/"config.yaml"))


def aging_hours(v, u):
    try: v = float(v)
    except (TypeError, ValueError): return 0.0
    u = str(u or "").strip().upper()
    if u.startswith("MON"): return v*720.0          # BR-12 (v34): CTP_Master_Aging 'Month' = 30 days
    if u.startswith("DAY"): return v*24.0
    if u.startswith("MIN"): return v/60.0
    return v


def build_aging_map(am):
    out = {}
    for _, r in am.iterrows():
        code = str(r["ItemCode"]).strip()
        if code:
            out[code] = (aging_hours(r["MinAging"], r["MinAgingUnit"]),
                         aging_hours(r["MaxAging"], r["MaxAgingUnit"]))
    return out


def _load_csv(name_candidates):
    for n in name_candidates:
        p = OUTPUTS/_suff(n)
        if p.exists() and p.is_file():
            df = pd.read_csv(p, low_memory=False)
            print(f"  loaded {p.name}  rows={len(df):,}", flush=True)
            return df
    raise FileNotFoundError(f"no file found from {name_candidates}")


def parse_machines(s):
    if pd.isna(s) or not s: return []
    out = []
    for tok in str(s).split(","):
        m = tok.strip().strip("'\"")
        if m and m.upper() not in ("NAN","NONE",""):
            out.append(m)
    return out


def first_fit_jit(busy, earliest_ns, target_start_ns, duration_ns):
    """JIT-aware first-fit (bisect-optimised). busy = sorted [(start_ns,end_ns)]."""
    n = len(busy)
    if n == 0:
        return max(earliest_ns, target_start_ns)
    last_end = busy[-1][1]
    if target_start_ns >= last_end and target_start_ns >= earliest_ns:
        return target_start_ns
    if earliest_ns >= last_end:
        return max(earliest_ns, target_start_ns)
    idx = bisect.bisect_left(busy, (earliest_ns,))
    cur = earliest_ns
    if idx > 0:
        prev_end = busy[idx - 1][1]
        if prev_end > cur:
            cur = prev_end
    best = None
    for i in range(idx, n):
        s, e = busy[i]
        if s > cur and (s - cur) >= duration_ns:
            place_start = max(cur, min(target_start_ns, s - duration_ns))
            if place_start >= target_start_ns:
                return place_start
            if best is None or place_start > best:
                best = place_start
        if e > cur:
            cur = e
    place_start = max(cur, target_start_ns)
    if best is None or place_start > best:
        best = place_start
    return best


# ═══ CTP DEVIATION #22 — THE MISSING PRIMITIVE: AN ALAP (LATEST-FIT) PLACER ═══════════
# (A) DEFECT in the resync, and it is the reason the pass throws good moves away.
#
# `first_fit_jit` above is a FORWARD placer: it aims at a target and, when the machine is
# busy there, returns max(cursor, target) — i.e. it lands AT OR AFTER the target. That is
# correct for the initial ASAP/JIT layout, and it is EXACTLY WRONG for the resync, which is
# a BACKWARD (ALAP) ratchet: the resync's target is a DEADLINE (finish before the consumer
# starts), not an aspiration. So when the target slot is occupied, first_fit_jit overshoots
# the deadline, the safety re-check fires, and THE WHOLE MOVE IS DISCARDED — the lot is put
# back at its original, far-too-early slot and left to rot. MEASURED: 1,385 moves thrown
# away this way in chain mode, on machines that Hall's condition proves have room.
#
# A lot that cannot reach its ideal slot but CAN reach a later one is strictly better off
# there. This function finds the LATEST free slot at or before the latest legal start.
def latest_fit(busy, earliest_ns, latest_start_ns, occ_ns):
    """Latest start s with earliest_ns <= s <= latest_start_ns and [s, s+occ_ns) free.
    None if no such slot. busy = sorted [(start_ns, end_ns)] (occupancy incl. changeover)."""
    if latest_start_ns < earliest_ns:
        return None
    hi_bound = latest_start_ns + occ_ns          # nothing we place may extend past this
    cur = earliest_ns
    idx = bisect.bisect_left(busy, (earliest_ns,))
    if idx > 0 and busy[idx - 1][1] > cur:
        cur = busy[idx - 1][1]                   # the interval straddling `earliest`
    best = None
    for i in range(idx, len(busy)):
        s, e = busy[i]
        if s >= hi_bound:
            break                                # beyond our deadline; cannot collide
        if s > cur:                              # free gap [cur, s)
            cand = min(latest_start_ns, s - occ_ns)
            if cand >= cur and (best is None or cand > best):
                best = cand
        if e > cur:
            cur = e
    # the tail gap [cur, hi_bound) — free by construction (any busy start >= hi_bound
    # cannot overlap a placement that ends at or before hi_bound)
    cand = min(latest_start_ns, hi_bound - occ_ns)
    if cand >= cur and (best is None or cand > best):
        best = cand
    return best


def main():
    print("\n" + "=" * 65)
    print("  PHASE 5 - Forward Placement (JIT + FRC campaigns)")
    print("=" * 65)
    cfg = load_cfg()
    # BR-74 (2026-09-30): the plant's fixed mixing changeover (config mixer_changeover_min; plant
    # override = 1 min). Was hard-coded 2 and the config key was never read.
    global MIXING_CHANGEOVER_MIN
    MIXING_CHANGEOVER_MIN = float(cfg.get("mixer_changeover_min", MIXING_CHANGEOVER_MIN))
    # BR-V2 (2026-10-01): butyl-friendly sequencing with a clean-out cycle (butyl_rule.py)
    global BUTYL_RULE
    try:
        sys.path.insert(0, str(ROOT))
        import butyl_rule as _butyl_rule
        BUTYL_RULE = _butyl_rule.load(cfg)
        if BUTYL_RULE.enabled:
            _basis = ("ONE BATCH of the compound being cleaned out (proc_time x 60 / efficiency, "
                      f"{len(BUTYL_RULE.cycle_min)} compound-mixer rates)" if BUTYL_RULE.basis == "one_batch_cycle"
                      else f"{BUTYL_RULE.cleanout_min * 60:.0f} s fixed")
            print(f"  [BR-V2] butyl sequence rule ON: clean-out = {_basis} instead of the "
                  f"changeover; " + " | ".join(f"{g['name']}: {sorted(g['targets'])} {sorted(g['check'])} "
                                              f"{sorted(g['prep'])} family {sorted(g['family'])}" for g in BUTYL_RULE.groups))
        else:
            BUTYL_RULE = None
    except Exception as _e:
        BUTYL_RULE = None
        print(f"  [BR-V2] !! butyl sequence rule not loaded ({type(_e).__name__}: {_e})")
    pre_curing_buffer_h = float(cfg.get("pre_curing_buffer_h", PRE_CURING_BUFFER_H))
    frc_cooldown_min    = float(cfg.get("frc_cooldown_min", 360))
    print(f"  pre_curing_buffer_h: {pre_curing_buffer_h}")
    print(f"  frc_cooldown_min:    {frc_cooldown_min}")

    print("\n  Loading inputs...")
    lots = _load_csv(["phase2_lots.csv","phase2_lots_v2.csv"])
    # CURING representation lots (L11) are terminal -- phase6 schedules curing from plan.csv.
    # phase5 must NEVER place them (doing so would double-schedule curing as building). Drop.
    if lots is not None and "operation" in lots.columns:
        _n0 = len(lots)
        lots = lots[lots["operation"].astype(str).str.strip().str.upper() != "CURING"].reset_index(drop=True)
        if len(lots) < _n0:
            print(f"    dropped {_n0 - len(lots):,} CURING representation lot(s) "
                  f"(terminal; scheduled by phase6, not placed here)")
    edges = _load_csv(["phase3_dag_edges.csv.gz","phase3_dag_edges.csv"])
    blocks = _load_csv(["phase2_lot_blocks.csv.gz","phase2_lot_blocks.csv"])
    times = _load_csv(["phase4_lot_times.csv"])
    am = _db_or_csv("aging_master", cfg)
    aging_map = build_aging_map(am)

    blocks["need_by"] = pd.to_datetime(blocks["need_by"], errors="coerce")
    first_curing = blocks["need_by"].min()

    # V1 fix: anchor T0 (schedule turn-on) at plan.startTime.min().
    # All machines begin from this moment — not earlier and not later.
    plan_start = None
    try:
        _plan_df = _db_or_csv("plan", cfg)
        _st_col = pd.to_datetime(_plan_df["startTime"], errors="coerce")
        if _st_col.notna().any():
            plan_start = _st_col.min()
    except Exception as _pe:
        print(f"  [WARN] could not derive plan.startTime.min ({_pe})")
    if plan_start is not None and not pd.isna(plan_start):
        T0 = plan_start
        print(f"  T0 = plan.startTime.min() = {T0}  (all machines start here)")
    else:
        T0 = first_curing - pd.Timedelta(hours=pre_curing_buffer_h)
        print(f"  T0 (fallback) = {T0}")

    lots["duration_h"]  = pd.to_numeric(lots["duration_hours"], errors="coerce").fillna(0)
    lots["machines_l"]  = lots["machines"].apply(parse_machines)
    # --- BUILDING machine eligibility STRICTLY FROM ROUTING (operation + product) ---
    # Stage-1 carcass (15 machines) and Stage-2 GT (24 machines) are DISJOINT pools;
    # carcass feeds GT in the two-stage process, while single-stage groups (BJ /
    # Unistage / VMIMaxx) build the whole GT in one go on a stage-2 machine. Upstream
    # lot 'machines' can merge both stages, which put ~half of GT lots onto carcass
    # machines. Take each building lot's eligible machines from the ROUTING's own
    # operation-specific machine list for that routed_product, so GT only ever runs
    # on stage-2 GT machines and carcass only on stage-1 machines.
    try:
        _rt = _db_or_csv("routing", cfg)
        _route_m, _op_all = {}, {}
        for _, _rr in _rt.iterrows():
            _opn  = str(_rr.get("operation_name", "")).strip().upper()
            _prod = str(_rr.get("routed_product", "")).strip()
            _ms   = set(parse_machines(_rr.get("machines", "")))
            if not _ms:
                continue
            _route_m.setdefault((_opn, _prod), set()).update(_ms)
            _op_all.setdefault(_opn, set()).update(_ms)
        _BUILD_OPS = {"GT BUILDING", "CAR BUILDING"}
        def _route_machines(row):
            op = str(row["operation"]).strip().upper()
            if op not in _BUILD_OPS:
                return row["machines_l"]
            ms = _route_m.get((op, str(row["item_code"]).strip()))
            if ms:
                return sorted(ms)
            # product not matched by code -> restrict to the operation's machine pool
            # (never let a GT lot fall back onto carcass machines, and vice-versa)
            pool = _op_all.get(op, set())
            keep = [m for m in row["machines_l"] if m in pool]
            return keep if keep else sorted(pool)
        lots["machines_l"] = lots.apply(_route_machines, axis=1)
        print(f"  Building eligibility from routing: "
              f"GT={len(_op_all.get('GT BUILDING', []))} machines, "
              f"CAR={len(_op_all.get('CAR BUILDING', []))} machines")
    except Exception as _re:
        print(f"  [WARN] could not load routing machine pools ({_re}); "
              f"using upstream lot eligibility")
    lots["is_belt"]     = lots["is_belt"].astype(str).str.lower().isin(("true","1","yes","y"))
    lots["first_need"]  = pd.to_datetime(lots["first_need"], errors="coerce")
    lots["last_need"]   = pd.to_datetime(lots["last_need"], errors="coerce")

    # ------------------------------------------------------------------
    # V6_wave WIP INJECTION (FIFO):
    # Earliest-needed consumer lots of each item draw from starting WIP first.
    # When cumulative lot_qty would exceed the available WIP for that item,
    # we stop (don't over-shrink) and let real production take over.
    # WIP-covered lots are dropped from the working set before topo / scheduling.
    # ------------------------------------------------------------------
    wip_path = OUTPUTS / "WIP_simulation_May2026.csv"
    wip = pd.read_csv(wip_path) if wip_path.exists() else pd.DataFrame()
    wip_by_item = {}
    if not wip.empty:
        for _, r in wip.iterrows():
            item = str(r["itemcode"]).strip()
            try:
                qty = float(r["availableinventory"])
            except (TypeError, ValueError):
                qty = 0.0
            wip_by_item[item] = wip_by_item.get(item, 0.0) + qty
    print(f"  WIP loaded: {len(wip_by_item):,} items, {sum(wip_by_item.values()):,.0f} total qty")

    # Build per-item lot list sorted FIFO by first_need; consume WIP greedily.
    wip_covered_lots = set()
    if wip_by_item:
        if "lot_qty" in lots.columns:
            _lot_qty_series = pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)
        else:
            _lot_qty_series = pd.Series(0.0, index=lots.index)
        lots["_wip_lot_qty"] = _lot_qty_series
        for item, group in lots.sort_values(["item_code", "first_need"]).groupby("item_code"):
            avail = wip_by_item.get(item, 0.0)
            if avail <= 0:
                continue
            consumed = 0.0
            for _, lot in group.iterrows():
                if consumed >= avail:
                    break
                lq = float(lot["_wip_lot_qty"])
                if lq <= 0:
                    continue
                if consumed + lq <= avail:
                    wip_covered_lots.add(lot["lot_id"])
                    consumed += lq
                else:
                    # partial coverage - keep this lot for v1 (don't over-shrink)
                    break
        lots.drop(columns=["_wip_lot_qty"], inplace=True)
    print(f"  WIP-covered lots (will be skipped): {len(wip_covered_lots):,}")
    print(f"  Total Phase 2 lots: {len(lots):,}")
    print(f"  Lots that will be scheduled: {len(lots) - len(wip_covered_lots):,}")

    # Capture lot_qty / item mapping BEFORE we drop covered lots so audit can use them.
    if "lot_qty" in lots.columns:
        lot_qty_full = dict(zip(lots["lot_id"],
                                pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)))
    else:
        lot_qty_full = {}
    lot_item_full = dict(zip(lots["lot_id"], lots["item_code"]))

    # Filter lots to remove WIP-covered ones.
    if wip_covered_lots:
        before = len(lots)
        lots = lots[~lots["lot_id"].isin(wip_covered_lots)].copy()
        print(f"  Lots after WIP filter: {len(lots):,} (dropped {before - len(lots):,})")

    # Filter edges to remove any whose producer OR consumer was WIP-covered.
    if wip_covered_lots:
        before_e = len(edges)
        edges = edges[
            ~edges["producer_lot"].isin(wip_covered_lots) &
            ~edges["consumer_lot"].isin(wip_covered_lots)
        ].copy()
        print(f"  Edges after WIP filter: {len(edges):,} (dropped {before_e - len(edges):,})")
    # ------------------------------------------------------------------

    # == v47 MIXING RULE A (user 2026-10-08, config mixing_v47.one_mixer_per_day) ==========================
    # "One compound in only one mixer per day, never split across mixers": every compound gets ONE mixer (routing-
    # eligible) for the whole plan, so no plant day can have it on two mixers whatever later passes do. Butyl /
    # butyl-friendly compounds stay on their block mixer (butyl_day_block groups); single-mixer compounds keep it;
    # the rest are balanced by total run time (largest first onto the least-loaded eligible mixer).
    _MV47 = cfg.get("mixing_v47") or {}
    _MIX_FIX = {}
    _DAYMIX = {}                                             # v49: (compound, plant day) -> mixer chosen that day
    _DAYMIX_ON = bool(_MV47.get("enabled", False) and _MV47.get("one_mixer_per_day", True)
                      and str(_MV47.get("one_mixer_mode", "plan")).lower() == "day")
    if _MV47.get("enabled", False) and _MV47.get("one_mixer_per_day", True):
        try:
            _ctm = lots["item_type"].astype(str).str.strip().str.upper().isin(["MASTER COMPOUND", "FINAL COMPOUND"])
            _pin = {}
            for _g in ((cfg.get("butyl_day_block") or {}).get("groups") or {}).values():
                for _x in list(_g.get("butyl") or []) + list(_g.get("friendly") or []):
                    _pin[str(_x).strip()] = str(_g.get("mixer", "")).strip()
            _elig, _ld = {}, defaultdict(float)
            for _it, _ms, _du in zip(lots.loc[_ctm, "item_code"].astype(str).str.strip(), lots.loc[_ctm, "machines_l"],
                                     pd.to_numeric(lots.loc[_ctm, "duration_h"], errors="coerce").fillna(0)):
                _ms = [str(x).strip() for x in (_ms or []) if str(x).strip()]
                _elig[_it] = (_elig[_it] & set(_ms)) if _it in _elig else set(_ms)
                _ld[_it] += float(_du)
            _mload = defaultdict(float)
            for _it, _es in _elig.items():
                if _it in _pin and _pin[_it] in _es:
                    _MIX_FIX[_it] = _pin[_it]
                elif len(_es) == 1:
                    _MIX_FIX[_it] = next(iter(_es))
                if _it in _MIX_FIX:
                    _mload[_MIX_FIX[_it]] += _ld[_it]
            _per_day = str(_MV47.get("one_mixer_mode", "plan")).lower() == "day"
            if _per_day:                                     # v49: only the butyl-group compounds are fixed; the
                _MIX_FIX = {i: m for i, m in _MIX_FIX.items() if i in _pin}   # rest: one mixer per DAY (placement)
            for _it in ([] if _per_day else sorted([i for i in _elig if i not in _MIX_FIX and _elig[i]], key=lambda i: -_ld[i])):
                _mm = min(sorted(_elig[_it]), key=lambda m: _mload[m])
                _MIX_FIX[_it] = _mm
                _mload[_mm] += _ld[_it]
            _fx = lots["item_code"].astype(str).str.strip().map(_MIX_FIX)
            _sel = _ctm & _fx.notna()
            lots.loc[_sel, "machines_l"] = _fx[_sel].map(lambda m: [m])
            lots.loc[_sel, "machines"] = _fx[_sel]
            if _per_day:
                print(f"  [MIXING v49 RULE A] one mixer per compound per PLANT DAY (chosen at placement by load; the mixer "
                      f"may differ between days); butyl-group compounds fixed on their block mixer: {sorted(_MIX_FIX)}")
            else:
                print(f"  [MIXING v47 RULE A] {len(_MIX_FIX)} compounds -> ONE mixer each (never split across mixers); "
                      f"planned load h: " + ", ".join(f"{m} {h:.0f}" for m, h in sorted(_mload.items())))
        except Exception as _e47:
            import traceback as _tb47
            _tb47.print_exc()
            print(f"  [MIXING v47 RULE A] !! skipped ({type(_e47).__name__}: {_e47})")
    lot_ids       = lots["lot_id"].tolist()
    lot_set       = set(lot_ids)
    duration_h    = dict(zip(lots["lot_id"], lots["duration_h"]))
    lot_item      = dict(zip(lots["lot_id"], lots["item_code"]))
    # V6_wave selective AEST: item_type drives whether AEST applies (off for bottleneck
    # items like MASTER COMPOUND / FINAL COMPOUND so the bottleneck mixer never starves).
    if "item_type" in lots.columns:
        lot_item_type = dict(zip(lots["lot_id"], lots["item_type"].fillna("").astype(str).str.strip().str.upper()))
    else:
        lot_item_type = {}
    # item_code -> itemtype (UPPER). Lets aging be standardized by TYPE (GT / carcass)
    # rather than per-SKU aging_master rows. Built from the lot table.
    item_itype = {}
    for _lid, _ic in lot_item.items():
        _it = lot_item_type.get(_lid, "")
        if _ic and _it:
            item_itype[_ic] = _it
    lot_op        = dict(zip(lots["lot_id"], lots["operation"]))
    lot_dept      = dict(zip(lots["lot_id"], lots["department"]))
    lot_eq        = dict(zip(lots["lot_id"], lots["equipment"]))
    lot_machines  = dict(zip(lots["lot_id"], lots["machines_l"]))
    lot_is_belt   = dict(zip(lots["lot_id"], lots["is_belt"]))
    lot_first_need= dict(zip(lots["lot_id"], lots["first_need"]))
    # CTP #32 — THE DRUM SIGNAL. last_need is the LAST curing block that consumes this lot,
    # straight out of phase2. It is the only press-need time in phase5 that is NOT distorted
    # by the phase2 double-allocation defect: MEASURED on the 911 GT lots,
    #   median(last_need - phase4 LFT) = +41.5 h,  median(build start - last_need) = +13.2 h.
    # The phase4 LFT says all 911 lots are late (phase5 prints late-vs-curing: 911), which
    # makes it useless as a gate; last_need ranks and dates them sanely.
    lot_last_need = {k: (pd.Timestamp(v).value if (v is not None and not pd.isna(v)) else None)
                     for k, v in zip(lots["lot_id"], lots["last_need"])}

    # ══════════════════════════════════════════════════════════════════════════
    # CTP DEVIATION #37 — DRUM PRIORITY (H1 / H2).            DEFAULT OFF.
    # ══════════════════════════════════════════════════════════════════════════
    # THE PREMISE (measured on inputs/plan.csv, reproduced in _drum_exp/premise.py):
    #   63 of 86 presses are booked >= 99.5 % of the 720 h horizon. A press with ZERO
    #   slack cannot absorb starvation — every idle hour goes straight to the makespan.
    #   ALL 10 of the latest-finishing presses are in that group (10 of 10). Those 63
    #   lose 920 h of late start + 2,674 h of internal idle in the 33.315 d run; the 23
    #   presses WITH slack finish on average day 24.84, ~8 days early.
    #
    # THE SIGNAL: press slack = 720 h - (hours plan.csv books on that press, INCLUDING
    #   its CHANGEOVER shifts — a press in changeover is not available either). A lot's
    #   criticality = the MINIMUM slack over every press that consumes it.
    #
    # THE JOIN is exact, not a heuristic: phase1b_demand_explosion.py:161 numbers blocks
    #   f"B{i:04d}" over the plan rows left after dropping skuCode == 'CHANGEOVER' and
    #   qty <= 0, in file order. Rebuilding that gives block_id -> pressNo with 100.00 %
    #   coverage of phase2_lot_blocks (364,919 / 364,919 rows, verified).
    #
    # DEGENERACY CHECK (memory rule: "check nunique() of every sort field on CTP data").
    #   MEASURED, _drum_exp/degeneracy.py:
    #     press slack        38 distinct values; 20 presses at exactly 0.00
    #     lot criticality    24 distinct (all lots) / 19 (GT); largest class 53.2 %
    #     layout key         changes the GT code order on 30 of 30 due_days
    #     dispatch key       discriminates 205 of 273 multi-lot tie-groups (75.1 %)
    #   So it is a REAL key, not a tie-break that silently became THE key — but it is
    #   COARSE (half the lots sit in one class), which is exactly what keeps campaigns
    #   intact. Do not "improve" it into a per-lot continuum without re-measuring
    #   changeovers.
    #
    # THE CAMPAIGN GUARD. F2 previously blew building changeovers 370 -> 763 by
    #   destroying the item-code term. Here the LAYOUT key uses the PER-CODE minimum, so
    #   criticality is a pure function of info[l]["code"]: sorting by (due_day, crit,
    #   code, ...) is a PERMUTATION OF WHOLE CODE GROUPS inside a due_day and can never
    #   split a campaign. The DISPATCH key (which has no campaign notion) uses per-lot.
    #
    # SCOPE
    #   off  — no term is added anywhere; the key expressions below are the v6 literals.
    #   gt   — H1. Only GREEN TYRES lots carry a criticality; everything else gets the
    #          neutral 0.0, so components keep their v6 order among themselves.
    #   all  — H2. Every lot carries its own criticality, so the drum signal propagates
    #          to the components. This is the version that reaches extruder 1002 /
    #          sidewall 1001, where the head is ONE tie-group of 26 lots decided purely
    #          by item-code ASCII (TRD 1402 … TRD 2776 before TRD T1457 STAR — "T" sorts
    #          after every digit). MEASURED: crit has 11 distinct values in that group.
    DRUM_PRIORITY = str(cfg.get("drum_priority", "off") or "off").strip().lower()
    if DRUM_PRIORITY in ("false", "no", "none", ""):
        DRUM_PRIORITY = "off"
    if DRUM_PRIORITY in ("true", "yes"):
        DRUM_PRIORITY = "all"
    if DRUM_PRIORITY not in ("off", "gt", "all"):
        print(f"  [WARN] drum_priority={DRUM_PRIORITY!r} unknown -> off")
        DRUM_PRIORITY = "off"
    DRUM_BAND_H   = float(cfg.get("drum_priority_band_h", 0.0) or 0.0)
    DRUM_LAYOUT   = bool(cfg.get("drum_priority_layout", True))
    DRUM_DISPATCH = bool(cfg.get("drum_priority_dispatch", True))
    DRUM_HORIZON_H = float(cfg.get("drum_priority_horizon_h", 720.0) or 720.0)
    # RANK — how a PRESS is scored before the per-lot MIN is taken.
    #   slack     : 720 h - plan booking. Non-circular, but MEASURED DEGENERATE at the
    #               top: 20 of 86 presses are booked to exactly 720.00 h, so the
    #               most-critical class is a 20-way tie and the press that actually
    #               sets the makespan (4836, booked 719.78 h) ranks BEHIND all 20.
    #   press_end : minus the press's observed END TIME in a PREVIOUS run
    #               (drum_priority_feedback_dir). One predictive-reactive feedback pass:
    #               45 distinct values, largest class 9.9 %, and it ranks GT 2256 ROYL /
    #               GT1824 XPC1 first — the two lots independently identified as gating
    #               press 4836's start and its three internal gaps.
    DRUM_RANK = str(cfg.get("drum_priority_rank", "slack") or "slack").strip().lower()
    DRUM_FB_DIR = str(cfg.get("drum_priority_feedback_dir", "") or "").strip()
    lot_drum_crit  = {}          # lot_id   -> float (lower = feeds a tighter press)
    code_drum_crit = {}          # item_code-> float (per-code MIN; the campaign guard)
    if DRUM_PRIORITY != "off":
        try:
            _pl = _db_or_csv("plan", cfg)
            _pl = _pl.copy()
            _pl["_press"] = _pl["pressNo"].astype(str).str.strip()
            _ph = ((pd.to_datetime(_pl["endTime"], errors="coerce")
                    - pd.to_datetime(_pl["startTime"], errors="coerce"))
                   .dt.total_seconds() / 3600.0)
            _pl["_h"] = _ph.fillna(0.0)
            _slack = (DRUM_HORIZON_H - _pl.groupby("_press")["_h"].sum()).clip(lower=0.0)
            _rank_note = f"plan slack (20 presses tie at 0.00 h)"
            if DRUM_RANK == "press_end":
                # FEEDBACK PASS. Explicit snapshot dir only — never the live outputs2,
                # which would make the run depend on whatever happened to be left there
                # (the stale-file trap). Print what the file encodes so a stale one shows.
                _fb = pathlib.Path(DRUM_FB_DIR)
                if not _fb.is_absolute():
                    _fb = ROOT.parent / DRUM_FB_DIR
                _fc = pd.read_csv(_fb / "phase6_curing_revision_updated.csv", low_memory=False)
                _fe = pd.to_datetime(_fc["endTime"])
                _t0f = _fe.min()
                _feh = (_fe - _t0f).dt.total_seconds() / 3600.0
                _pe = _feh.groupby(_fc["pressNo"].astype(str).str.strip()).max()
                _slack = -_pe                       # latest-finishing press = most critical
                _rank_note = (f"press END from {_fb.name} (horizon {_pe.max()/24:.3f} d, "
                              f"{_pe.nunique()} distinct ends)")
            # block_id numbering — MUST mirror phase1b_demand_explosion.py:130-161
            _bf = _pl[(_pl["skuCode"].astype(str).str.strip() != "CHANGEOVER")
                      & (pd.to_numeric(_pl["qty"], errors="coerce").fillna(0) > 0)
                      ].reset_index(drop=True)
            _b2p = pd.Series(_bf["_press"].values,
                             index=[f"B{i:04d}" for i in range(len(_bf))])
            _bl = blocks[["lot_id", "block_id"]].copy()
            _bl["_s"] = _bl["block_id"].astype(str).str.strip().map(_b2p).map(_slack)
            _cov = float(_bl["_s"].notna().mean()) * 100.0
            lot_drum_crit = _bl.groupby("lot_id")["_s"].min().dropna().to_dict()
            if DRUM_BAND_H > 0:
                # floor-divide, NOT int(): press_end ranks are negative and int()
                # truncates toward zero, which would break monotonicity across 0.
                lot_drum_crit = {k: float(v // DRUM_BAND_H)
                                 for k, v in lot_drum_crit.items()}
            if DRUM_PRIORITY == "gt":
                lot_drum_crit = {k: v for k, v in lot_drum_crit.items()
                                 if lot_item_type.get(k, "") == "GREEN TYRES"}
            for _l, _c in lot_drum_crit.items():
                _ic = lot_item.get(_l, "")
                if _ic and (_ic not in code_drum_crit or _c < code_drum_crit[_ic]):
                    code_drum_crit[_ic] = _c
            _vals = list(lot_drum_crit.values())
            _mode = (max(_vals.count(v) for v in set(_vals)) / len(_vals) * 100) if _vals else 0
            print(f"  DRUM PRIORITY: scope={DRUM_PRIORITY} rank={DRUM_RANK} "
                  f"band={DRUM_BAND_H} layout={DRUM_LAYOUT} dispatch={DRUM_DISPATCH}")
            print(f"    rank source: {_rank_note} | {len(_slack)} presses, "
                  f"{_slack.nunique()} distinct scores")
            print(f"    block->press coverage {_cov:.2f}% | {len(lot_drum_crit):,} lots scored "
                  f"({len(set(_vals))} distinct crit values, largest class {_mode:.1f}%, "
                  f"{len(code_drum_crit)} codes)")
        except Exception as _de:
            print(f"  [WARN] drum_priority could not build the criticality map ({_de}) "
                  f"-> DISABLED for this run")
            DRUM_PRIORITY = "off"
            lot_drum_crit, code_drum_crit = {}, {}
    # Neutral for anything unscored: it ties with the most-critical class, so unscored
    # lots keep their v6 relative order (item_code decides, exactly as before).
    def _dcrit_lot(l):  return lot_drum_crit.get(l, 0.0)
    def _dcrit_code(c): return code_drum_crit.get(c, 0.0)

    # CUTTER MODEL: per-lot qty (NOS) needed for mother-roll MM consumption math
    if "lot_qty" in lots.columns:
        lot_qty = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
    else:
        lot_qty = {}
    lot_camp_id   = dict(zip(lots["lot_id"], lots["campaign_id"].fillna("").astype(str)))
    # v55 (user 2026-10-09): mixing RUN GROUPS from phase 2 -- separate lots of one compound that run back to back on
    # one mixer (no changeover between); the first member placed opens the run and RESERVES the time of the others
    # directly behind it, each later member fills its slot (at its inputs-ready time) when its turn comes.
    _RUNGRP = {}
    if "mixrun_id" in lots.columns:
        _RUNGRP = {l: g for l, g in zip(lots["lot_id"], lots["mixrun_id"].fillna("").astype(str)) if g}
    _RUN_MEM = defaultdict(list)
    for _l, _g in _RUNGRP.items():
        _RUN_MEM[_g].append(_l)
    _RUN_STATE = {}                                          # group -> {"m", "end", "res": (s, f) reservation}
    _RUN_STAT = {"groups": len(_RUN_MEM), "members": 0, "gap_h": 0.0, "broken": 0}
    lot_wire      = dict(zip(lots["lot_id"], lots["wire_type"].fillna("").astype(str)))
    lot_camp_start= dict(zip(lots["lot_id"], lots["is_campaign_start"].astype(str).str.lower().isin(("true","1","yes","y"))))
    lot_camp_end  = dict(zip(lots["lot_id"], lots["is_campaign_end"].astype(str).str.lower().isin(("true","1","yes","y"))))
    # V6_wave: wave_first for wave-priority dispatch
    if "wave_first" in lots.columns:
        lot_wave_first = dict(zip(lots["lot_id"], lots["wave_first"].fillna("Z99").astype(str)))
    else:
        lot_wave_first = {lid: "Z99" for lid in lots["lot_id"]}

    # V6_wave MHE model: per-MHE drum material flow.
    # mhe_total = drums produced sequentially during the lot's duration.
    # First drum ready at p_start + p_dur / mhe_total; nth drum at p_start + n*p_dur/mhe_total.
    # Downstream may pull as soon as drum 1 ages out - no need to wait for whole batch.
    #
    # ═══ CTP DEVIATION #20 — `lot_mhe_total` IS NOW UNREAD. IT IS INERT ON CTP. ═══════
    # MEASURED: mhe_total == 1 on 6,780 of 6,780 lots (100.0%). phase2_lot_sizing.py PRINTS
    # "MHE MODEL INERT: no Maximum Run Qty in the MPQ master -> mhe_total = 1 on ALL 6,780
    # lots." CTP's MPQ master correctly has no `Maximum Run Qty` column — CTP has no max
    # MPQ, so no lot is ever split into drums.
    # Every consumer of this map divided by it (p_start + p_dur // mhe_total), which with
    # mhe_total == 1 is EXACTLY p_finish. The whole drum apparatus was an IDENTITY dressed
    # up as a model, and the "any 1 drum supplies" MIN it justified was the E1 blocker.
    # The map is LEFT LOADED (it is free, and a future plant WITH a max MPQ would need it)
    # but NOTHING READS IT ANY MORE. Do not re-introduce a rule on top of it without first
    # checking that mhe_total > 1 actually occurs in the data.
    if "mhe_total" in lots.columns:
        lot_mhe_total = dict(zip(lots["lot_id"],
                                  pd.to_numeric(lots["mhe_total"], errors="coerce").fillna(1).astype(int)))
    else:
        lot_mhe_total = {lid: 1 for lid in lots["lot_id"]}

    # V6_wave anti-monopoly: dispatch MHE-drum 0 of every item before drum 1 etc.
    if "mhe_index" in lots.columns:
        lot_mhe_index = dict(zip(lots["lot_id"],
                                   pd.to_numeric(lots["mhe_index"], errors="coerce").fillna(0).astype(int)))
    else:
        lot_mhe_index = {lid: 0 for lid in lots["lot_id"]}
    if "batch_id" in lots.columns:
        lot_batch_id = dict(zip(lots["lot_id"], lots["batch_id"].fillna("").astype(str)))
    else:
        lot_batch_id = {lid: lid for lid in lots["lot_id"]}
    # format="mixed": Phase-4 writes timestamps in two formats — with sub-second
    # precision (…07:00:00.000000000) and without (…15:00:00). A single-format
    # parse infers ONE format from the first row and silently coerces the other
    # ~40% to NaT, which made those lots look deadline-less (due=BIG) and get
    # shoved to the end of every machine queue (the entire June tail). Parse each
    # element independently so every LFT is honoured.
    lst_map = dict(zip(times["lot_id"], pd.to_datetime(times["LST"], errors="coerce", format="mixed")))
    # C4 honest-LFT REPORT mode: phase4 makes the LFT column truthful (own last_need) for the
    # C3/C4 report but preserves the placement-basis LFT in LFT_place. Read that column for the
    # aging/wip caps so REPORT mode is schedule byte-identical. Absent (v6 output) -> use LFT.
    _lft_place_col = "LFT_place" if "LFT_place" in times.columns else "LFT"
    lft_map = dict(zip(times["lot_id"], pd.to_datetime(times[_lft_place_col], errors="coerce", format="mixed")))
    # ── LFT CAPPED BY THE REAL NEED (2026-09-30, config `lft_cap_by_need`) ──────────────────
    # The CPM LFT is propagated back from phase2's internal GT lots, not from the building plan the
    # echo actually builds. For a 05-Oct 07:00 build it gave IL056 LFT 16:50, TRD 1765 12:45,
    # B2-PBLT001 14:50 -- JIT placement then made them 6-10 h after the build needed them and
    # building sat idle (15 TBMs >4 h on 05-Oct). A lot must finish by its own first need
    # (phase1b need_by = the building-plan time) minus its min aging + transfer lag.
    if cfg.get("lft_cap_by_need", False):
        _mn_h = dict(zip(lots["lot_id"], pd.to_numeric(lots.get("min_aging_h", 0), errors="coerce").fillna(0)
                         + pd.to_numeric(lots.get("transfer_time_h", 0), errors="coerce").fillna(0)))
        _dept = dict(zip(lots["lot_id"], lots.get("department", pd.Series("", index=lots.index)).astype(str).str.upper()))
        _n_cap = 0; _cap_h = 0.0
        _skipm = {str(x).strip() for x in (cfg.get("lft_cap_skip_machines") or [])}
        lot_machines_raw = {l: [t.strip().strip('"').strip("'") for t in str(ms).split(",")]
                            for l, ms in zip(lots["lot_id"], lots.get("machines", pd.Series("", index=lots.index)))}
        for _l, _lf in list(lft_map.items()):
            _fn = lot_first_need.get(_l)
            if pd.isna(_lf) or _fn is None or pd.isna(_fn) or _dept.get(_l, "") in ("BUILDING", "CURING"):
                continue
            if _skipm and any(_m in _skipm for _m in lot_machines_raw.get(_l, ())):
                continue                          # hard-wall machine: keep its CPM order
            _cap = pd.Timestamp(_fn) - pd.Timedelta(hours=float(_mn_h.get(_l, 0.0)))
            if _cap < _lf:
                _cap_h += (_lf - _cap).total_seconds() / 3600.0
                lft_map[_l] = _cap
                _n_cap += 1
        print(f"  LFT CAP BY NEED: {_n_cap:,} component lots had a CPM LFT later than their own need "
              f"(building-plan time - min aging); pulled in by {(_cap_h / max(_n_cap, 1)):.1f} h on average")
    # == CAPACITY-AWARE DUE TIMES (2026-09-30, config `capacity_aware_lft`) ======================
    # RCA 05-Oct: the CPM LFT assumes infinite capacity. All 19 treads for 05-Oct 07:00 run on ONE
    # extruder (1002, ~18 h of work) but each got a due time just before 07:00, and their compound
    # B815 was made 20th of 21 in F270's queue (04-Oct 09:39) -> 1002 could only start at 14:24
    # and the last treads were 1-9 h late -> TBM backlog -> 2,095 cures lost. Here a single-machine
    # component lot is due by (need - min aging - the OTHER work on its machine for that same need
    # day), and that earlier due time is carried back to its inputs (compounds) through the DAG,
    # so the mixers make the compound of the longest downstream queue first. Only ever tightens.
    if cfg.get("capacity_aware_lft", False):
        _dur_h = dict(zip(lots["lot_id"], pd.to_numeric(lots.get("duration_hours", 0), errors="coerce").fillna(0)))
        _mach = dict(zip(lots["lot_id"], lots.get("machines", pd.Series("", index=lots.index)).astype(str)))
        _dep2 = dict(zip(lots["lot_id"], lots.get("department", pd.Series("", index=lots.index)).astype(str).str.upper()))
        _mnh = dict(zip(lots["lot_id"], pd.to_numeric(lots.get("min_aging_h", 0), errors="coerce").fillna(0)
                        + pd.to_numeric(lots.get("transfer_time_h", 0), errors="coerce").fillna(0)))
        _skip_dep = ("BUILDING", "CURING", "MASTER MIXING", "FINAL MIXING")
        _bucket = defaultdict(list)
        for _l in lots["lot_id"]:
            _fn = lot_first_need.get(_l)
            _ms = [t.strip().strip('"').strip("'") for t in _mach.get(_l, "").split(",") if t.strip()]
            if _fn is None or pd.isna(_fn) or len(_ms) != 1 or _dep2.get(_l, "") in _skip_dep:
                continue
            _day = (pd.Timestamp(_fn) - pd.Timedelta(hours=7)).normalize()
            _bucket[(_ms[0], _day)].append(_l)
        _n_t = 0
        _new = {}
        for (_m, _day), _ls in _bucket.items():
            _w = sum(_dur_h.get(l, 0.0) for l in _ls)
            if _w <= 1.0:
                continue
            for _l in _ls:
                _due = (pd.Timestamp(lot_first_need[_l]) - pd.Timedelta(hours=float(_mnh.get(_l, 0.0)))
                        - pd.Timedelta(hours=_w - _dur_h.get(_l, 0.0)))
                _cur = lft_map.get(_l)
                if _cur is None or pd.isna(_cur) or _due < _cur:
                    lft_map[_l] = _due
                    _new[_l] = _due
                    _n_t += 1
        # carry the tighter due time back to the inputs (compounds, rolls): 3 levels
        try:
            _ed = pd.read_csv(OUTPUTS / "phase3_dag_edges_updated.csv",
                              usecols=["producer_lot", "consumer_lot", "consume_qty"])
            _ed = _ed[pd.to_numeric(_ed["consume_qty"], errors="coerce").fillna(0) > 0]
            _preds = defaultdict(list)
            for _pp, _cc in zip(_ed["producer_lot"], _ed["consumer_lot"]):
                _preds[_cc].append(_pp)
            _front = dict(_new)
            _n_p = 0
            for _lvl in range(3):
                _nxt = {}
                for _cc, _due in _front.items():
                    for _pp in _preds.get(_cc, ()):
                        _pd = (_due - pd.Timedelta(hours=float(_dur_h.get(_cc, 0.0)))
                               - pd.Timedelta(hours=float(_mnh.get(_pp, 0.0))))
                        _cur = lft_map.get(_pp)
                        if _cur is None or pd.isna(_cur) or _pd < _cur:
                            lft_map[_pp] = _pd
                            _nxt[_pp] = _pd
                            _n_p += 1
                _front = _nxt
                if not _front:
                    break
        except Exception as _e:
            _n_p = 0
            print(f"  [CAP-LFT] !! edge propagation skipped ({type(_e).__name__}: {_e})")
        print(f"  [CAP-LFT] capacity-aware due times: {_n_t:,} single-machine component lots tightened "
              f"by their machine's same-day queue; {_n_p:,} input lots (compounds/rolls) tightened upstream")
    # == PROTECT BOTTLENECK FEED (2026-09-30, config `protect_bottleneck_feed`) =================
    # True-baseline RCA: 1802 (BEF001) ran 24 h and was 10-16k m AHEAD of need to 03-Oct, then sat
    # 45 h idle (04-Oct 18:19 -> 06-Oct 15:41) because its compound B1144 came late off saturated
    # mixers -> 20k m BEF001 short on 05-Oct -> TBR builds wait. Same for tread compounds -> 1002.
    # Only the compound lots that feed the listed bottleneck items get a due time `lead_h` earlier,
    # so the mixers make them first (BEF001 / tread / B1144 shelf life 72 h allows it). A global
    # re-prioritisation of every compound was measured WORSE -- this is deliberately targeted.
    _pb = cfg.get("protect_bottleneck_feed") or {}
    if _pb.get("enabled", False):
        import re as _re_pb
        _pat = [_re_pb.compile(str(x), _re_pb.I) for x in (_pb.get("items") or [])]
        _lead = pd.Timedelta(hours=float(_pb.get("lead_h", 24)))
        try:
            _ed2 = pd.read_csv(OUTPUTS / "phase3_dag_edges_updated.csv",
                               usecols=["producer_lot", "consumer_lot", "consume_qty"])
            _ed2 = _ed2[pd.to_numeric(_ed2["consume_qty"], errors="coerce").fillna(0) > 0]
            _prd = defaultdict(list)
            for _pp, _cc in zip(_ed2["producer_lot"], _ed2["consumer_lot"]):
                _prd[_cc].append(_pp)
            _mset = {str(x).strip() for x in (_pb.get("machines") or [])}   # v43: every lot of these machines (901)
            _front = [l for l, ms in zip(lots["lot_id"], lots["machines"].astype(str))
                      if any(r_.search(str(lot_item.get(l, ""))) for r_ in _pat)
                      or (_mset and _mset & set(_re_pb.findall(r"[A-Za-z0-9\-]+", ms)))]
            _n_pb = 0
            _seen = set()
            for _lvl in range(3):                    # the item's compound, and that compound's master
                _nxt = []
                for _cc in _front:
                    _fn = lot_first_need.get(_cc)
                    for _pp in _prd.get(_cc, ()):
                        if _pp in _seen:
                            continue
                        _seen.add(_pp)
                        _cur = lft_map.get(_pp)
                        _base = _cur if (_cur is not None and not pd.isna(_cur)) else (
                            pd.Timestamp(_fn) if _fn is not None and not pd.isna(_fn) else None)
                        if _base is None:
                            continue
                        lft_map[_pp] = pd.Timestamp(_base) - _lead
                        _n_pb += 1
                        _nxt.append(_pp)
                _front = _nxt
            print(f"  [PROTECT FEED] {_n_pb:,} compound lots feeding {_pb.get('items')} / machines {sorted(_mset)} due {_lead} earlier "
                  f"(mixers make bottleneck-feeding compounds first)")
        except Exception as _e:
            print(f"  [PROTECT FEED] !! skipped ({type(_e).__name__}: {_e})")
    # == v47 MIXING RULE B + FINAL-MIXER FEED (user 2026-10-08, config mixing_v47) ======================
    # B: the same compound is not mixed again within gap_h (15 h) of its previous run's START -- gap_h_long (24 h) for
    #    IL3 / IL5 and 4RC compounds; butyl-friendly compounds exempt. Enforced in the final repair (later-only) and
    #    in the daily butyl blocks. _GAP_W: compound -> minimum start-to-start gap (ns).
    # C: final mixers ~230 batches / shift: they idled waiting for MASTER compound. Each final lot may start as early
    #    as its shelf life allows (last need - (max aging - margin)); its master lots are due before that start
    #    (- min aging - transfer) and may start as early as the master's own shelf life allows against the LAST
    #    final that uses it -- so masters come early enough for the finals to fill the mixers.
    _GAP_W, _FILL_FLOOR = {}, {}
    _RUNS47 = defaultdict(list)                              # v53: compound -> [machine, run start, run end] placed so far
    _GAP_PLACE = bool(_MV47.get("enabled", False) and _MV47.get("placement_aware", False))
    _COMPACT = bool((cfg.get("repair_compaction") or {}).get("enabled", False))
    _FLOOR0, _BFLOOR0 = {}, {}                               # v53: placement start per lot / build row (repair floor)
    if _MV47.get("enabled", False):
        try:
            _H1g = 3600 * 10 ** 9                           # NS is defined further down this function
            _ex47 = {str(x).strip() for x in (_MV47.get("exempt") or [])}
            _long47 = {str(x).strip() for x in (_MV47.get("long_items") or [])}
            if _MV47.get("long_fourrc_compounds", True):
                _fl47 = pd.read_csv(INPUTS / "frc_lengths.csv")
                _b47 = pd.read_csv(INPUTS / "bom.csv", dtype=str, low_memory=False)
                _long47 |= set(_b47.loc[_b47["Parent"].astype(str).str.strip().isin(
                    set(_fl47["item"].astype(str).str.strip())), "child"].astype(str).str.strip())
            _ctl = {str(i).strip() for i, t in zip(lots["item_code"], lots["item_type"].astype(str).str.upper())
                    if t.strip() in ("MASTER COMPOUND", "FINAL COMPOUND")}
            if _MV47.get("repeat_gap", True):
                for _it in _ctl - _ex47:
                    _GAP_W[_it] = int(float(_MV47.get("gap_h_long" if _it in _long47 else "gap_h", 24 if _it in _long47 else 15)) * _H1g)
            print(f"  [MIXING v47 RULE B] {len(_GAP_W)} compounds: not repeated within "
                  f"{_MV47.get('gap_h', 15)} h ({_MV47.get('gap_h_long', 24)} h for {sorted(_long47 & set(_GAP_W))}); "
                  f"exempt {sorted(_ex47)}")
            _ff = _MV47.get("final_feed") or {}
            if _ff.get("enabled", False):
                _mg = float(_ff.get("shelf_margin_h", 2)) * _H1g
                _lt47 = lots.set_index("lot_id")
                _dur = lambda l: int(float(_lt47.at[l, "duration_h"] or 0) * _H1g)
                _mxh = lambda l: float(_lt47.at[l, "max_aging_h"] or 0) * _H1g
                _mnh = lambda l: (float(_lt47.at[l, "min_aging_h"] or 0) + float(_lt47.at[l, "transfer_time_h"] or 0)) * _H1g
                _ed47 = pd.read_csv(OUTPUTS / "phase3_dag_edges_updated.csv", usecols=["producer_lot", "consumer_lot", "consume_qty"])
                _ed47 = _ed47[pd.to_numeric(_ed47["consume_qty"], errors="coerce").fillna(0) > 0]
                _prd47 = defaultdict(list)
                for _pp, _cc in zip(_ed47["producer_lot"], _ed47["consumer_lot"]):
                    _prd47[_cc].append(_pp)
                _fin = [l for l, t in zip(lots["lot_id"], lots["item_type"].astype(str).str.upper())
                        if t.strip() == "FINAL COMPOUND" and l in _lt47.index]
                _due_m, _last_m = {}, {}
                for l in _fin:
                    _ln = lot_last_need.get(l) or (pd.Timestamp(lot_first_need[l]).value
                                                  if lot_first_need.get(l) is not None and not pd.isna(lot_first_need.get(l)) else None)
                    if _ln is None or _mxh(l) <= 0:
                        continue
                    P = int(_ln - (_mxh(l) - _mg) - _dur(l))
                    _FILL_FLOOR[l] = P                      # the final may start this early (shelf life at its last use)
                    for p in _prd47.get(l, ()):
                        if p not in _lt47.index or str(_lt47.at[p, "item_type"]).strip().upper() != "MASTER COMPOUND":
                            continue
                        d_ = int(P - _mnh(p))
                        _due_m[p] = min(_due_m.get(p, d_), d_)
                        _last_m[p] = max(_last_m.get(p, d_), d_)
                _n_t = 0
                for p, d_ in _due_m.items():
                    _cur = lft_map.get(p)
                    if _cur is None or pd.isna(_cur) or d_ < pd.Timestamp(_cur).value:
                        lft_map[p] = pd.Timestamp(int(d_))
                        _n_t += 1
                    if _mxh(p) > 0:                         # alive at its LAST final; never later than its due
                        _FILL_FLOOR[p] = int(min(_last_m[p] - (_mxh(p) - _mg) - _dur(p), d_ - _dur(p)))
                print(f"  [MIXING v47 FINAL FEED] {len(_fin)} final lots may start from their shelf-life floor; "
                      f"{_n_t} master lots due earlier (before the earliest final that uses them)")
        except Exception as _e47b:
            import traceback as _tb47b
            _tb47b.print_exc()
            print(f"  [MIXING v47 RULE B / FEED] !! skipped ({type(_e47b).__name__}: {_e47b})")
    # == V44 901 LEVEL PLAN FIRST (user 2026-10-07, config fourrc_rules.level_plan) =================
    # The 4-roll calender is planned BEFORE any placement: every 901 lot gets a plant day so that each day makes
    # min_km..max_km (made metres = whole Spool/Dip Length per CTP_Master_FRC wire_type). Peaks move to EARLIER days,
    # light days pull the next days' lots -- only while every metre is still used within the lot's max aging
    # (CTP_Master_Aging, minus safety_h) before its last need. Each lot gets a target start in its day; its feeding
    # compounds (skim final, master, re-mill: 3 BOM levels) get their due time back-scheduled from that start
    # (start - min aging - transfer), so the mixers make exactly those lots in time. Placement starts the 901 lot at
    # its target (never before its inputs are ready); every 4RC / mixing rule is applied afterwards as before.
    _LEVEL_TGT = {}
    _LEVEL_BAND = {}
    _LPc = ((cfg.get("fourrc_rules") or {}).get("level_plan") or {})
    if _LPc.get("enabled", False):
        try:
            import math as _mth
            _H1 = 3600 * 10 ** 9
            _H7l, _Dl = 7 * _H1, 24 * _H1
            _dayl = lambda t: (int(t) - _H7l) // _Dl
            _lo, _hi = float(_LPc.get("min_km", 30)) * 1000, float(_LPc.get("max_km", 35)) * 1000
            _saf = int(float(_LPc.get("safety_h", 4)) * _H1)
            _shift_max = int(_LPc.get("max_days", 3))
            _flv = pd.read_csv(INPUTS / "frc_lengths.csv")
            _wl = dict(zip(_flv["item"].astype(str).str.strip(), _flv["wire"].astype(str).str.strip()))
            _Dlv = dict(zip(_flv["item"].astype(str).str.strip(), pd.to_numeric(_flv["spool_dip_length_m"], errors="coerce")))
            _m901 = str((cfg.get("fourrc_rules") or {}).get("machine", "901"))
            _lt = lots.set_index("lot_id")
            _L9 = [l for l, ms in zip(lots["lot_id"], lots["machines"].astype(str))
                   if _m901 in [x.strip().strip('"') for x in ms.replace(";", ",").split(",")]]
            _t0 = pd.Timestamp(os.environ.get("CTP_PLAN_START") or min(
                pd.Timestamp(lft_map.get(l) or lot_first_need.get(l)) for l in _L9)).value
            _d0 = _dayl(_t0)
            inf = {}
            for l in _L9:
                it = str(lot_item.get(l, "")).strip()
                q = float(_lt.at[l, "lot_qty"] or 0.0)
                du = int(float(_lt.at[l, "duration_hours"] or 0.0) * _H1)
                nd = lot_first_need.get(l)                   # v44 fix: building-plan first need (phase4 LFT is far too early)
                nd = nd if (nd is not None and not pd.isna(nd)) else lft_map.get(l)
                if nd is None or pd.isna(nd):
                    continue
                nd = pd.Timestamp(nd).value
                ln = lot_last_need.get(l) or nd
                mx = float(_lt.at[l, "max_aging_h"] or 0.0)
                inf[l] = {"it": it, "q": q, "du": du, "need": nd, "last": max(ln, nd), "mx": int(mx * _H1),
                          "w": _wl.get(it, it), "D": _Dlv.get(it)}
            day = {l: max(_d0, _dayl(v["need"] - v["du"] - _H1)) for l, v in inf.items()}

            def _est(ls):
                bw = defaultdict(float)
                for x in ls:
                    bw[inf[x]["w"]] += inf[x]["q"]
                t = 0.0
                for w_, q_ in bw.items():
                    D_ = next((inf[x]["D"] for x in ls if inf[x]["w"] == w_), None)
                    t += (_mth.ceil((q_ - 1.0) / D_) * D_) if (D_ and D_ == D_ and D_ > 0) else q_
                return t

            def _ok(l, d):                          # made by the end of day d -> still alive at its last need
                if inf[l]["mx"] <= 0:
                    return d <= day[l]
                fin = (d + 1) * _Dl + _H7l
                return fin + inf[l]["mx"] - _saf >= inf[l]["last"] and d >= orig.get(l, day[l]) - _shift_max

            orig = dict(day)
            byd = lambda d: [l for l in inf if day[l] == d]
            dmax = max(day.values()) if day else _d0
            if str(_LPc.get("mode", "band")).lower() == "average":   # v44 option C: average daily need +- band_km
                _pe = os.environ.get("CTP_PLAN_END")
                _dl = _dayl(pd.Timestamp(_pe).value) - 1 if _pe else dmax
                _nd = max(1, _dl - _d0 + 1)
                _avg = sum(_est(byd(d)) for d in range(_d0, _dl + 1)) / _nd
                _bk = float(_LPc.get("band_km", 5)) * 1000
                _lo, _hi = max(_avg - _bk, 0.0), _avg + _bk
                print(f"  [4RC LEVEL PLAN v44] average daily 901 need (made metres, whole spools / dips) {_avg / 1000:.1f} km "
                      f"over {_nd} plant days -> band {_lo / 1000:.1f}-{_hi / 1000:.1f} km")
            for d in range(dmax, _d0, -1):           # peaks -> earlier days
                while _est(byd(d)) > _hi:
                    c = [l for l in byd(d) if _ok(l, d - 1)]
                    if not c:
                        break
                    c.sort(key=lambda l: (-(inf[l]["mx"] - (inf[l]["last"] - (d * _Dl + _H7l))), inf[l]["need"]))
                    day[c[0]] = d - 1
            for d in range(_d0, dmax + 1):           # light days <- next days
                guard = 0
                while _est(byd(d)) < _lo and guard < 200:
                    guard += 1
                    ws = {inf[x]["w"] for x in byd(d)}
                    c = sorted([l for l in inf if d < day[l] <= d + _shift_max and _ok(l, d)],
                               key=lambda l: (inf[l]["w"] not in ws, inf[l]["need"]))
                    c = [l for l in c if _est(byd(d) + [l]) <= _hi]
                    if not c:
                        break
                    day[c[0]] = d
            # target start inside each day (wire groups together, by need), compounds back-scheduled
            _ed = pd.read_csv(OUTPUTS / "phase3_dag_edges_updated.csv", usecols=["producer_lot", "consumer_lot", "consume_qty"])
            _ed = _ed[pd.to_numeric(_ed["consume_qty"], errors="coerce").fillna(0) > 0]
            _prd = defaultdict(list)
            for _pp, _cc in zip(_ed["producer_lot"], _ed["consumer_lot"]):
                _prd[_cc].append(_pp)
            _n_c = 0
            for d in sorted(set(day.values())):
                ls = sorted(byd(d), key=lambda l: (min(inf[x]["need"] for x in byd(d) if inf[x]["w"] == inf[l]["w"]), inf[l]["need"]))
                t = d * _Dl + _H7l + _H1
                for l in ls:
                    _LEVEL_TGT[l] = t
                    _cur = lft_map.get(l)
                    _tf = pd.Timestamp(int(t + inf[l]["du"]))
                    if _cur is None or pd.isna(_cur) or _tf < pd.Timestamp(_cur):
                        lft_map[l] = _tf
                    front = [(l, t)]
                    for _lvl in range(3):
                        nxt = []
                        for cl, cst in front:
                            for p in _prd.get(cl, ()):
                                if p not in _lt.index:
                                    continue
                                mn = float(_lt.at[p, "min_aging_h"] or 0.0) + float(_lt.at[p, "transfer_time_h"] or 0.0)
                                due = pd.Timestamp(int(cst - mn * _H1))
                                _cp = lft_map.get(p)
                                if _cp is None or pd.isna(_cp) or due < pd.Timestamp(_cp):
                                    lft_map[p] = due
                                    _n_c += 1
                                pdu = int(float(_lt.at[p, "duration_hours"] or 0.0) * _H1)
                                nxt.append((p, due.value - pdu))
                        front = nxt
                    t += inf[l]["du"] + 20 * 60 * 10 ** 9
            _LEVEL_BAND.update(lo=_lo, hi=_hi)
            _moved = sum(1 for l in day if day[l] != orig[l])
            _kmd = {d: _est(byd(d)) / 1000 for d in sorted(set(day.values()))}
            _inb = sum(1 for d, v in _kmd.items() if _lo / 1000 <= v <= _hi / 1000)
            print(f"  [4RC LEVEL PLAN v44] {len(inf)} lots on {_m901} levelled to {_lo/1000:g}-{_hi/1000:g} km/day: {_moved} moved "
                  f"to another day (earlier, within shelf life); {_inb}/{len(_kmd)} plant days in the band; {_n_c} feeding compound "
                  f"lots back-scheduled; planned km/day " + ", ".join(f"{pd.Timestamp(int(d * _Dl + _H7l)):%d-%b} {v:.1f}" for d, v in list(_kmd.items())[:12]))
        except Exception as _elv:
            import traceback as _tbl
            _tbl.print_exc()
            print(f"  [4RC LEVEL PLAN v44] !! failed ({type(_elv).__name__}: {_elv}) -- 901 not levelled")
    # V6_wave AEST: aging-aware earliest-start floor from Phase 4 planning policy.
    # If column is absent (legacy Phase 4 output), falls back to pure ASAP behaviour.
    if "AEST" in times.columns:
        aest_map = dict(zip(times["lot_id"], pd.to_datetime(times["AEST"], errors="coerce", format="mixed")))
        n_aest = sum(1 for v in aest_map.values() if pd.notna(v))
        print(f"  AEST data loaded for {n_aest:,} lots (upstream ASAP will be bounded by AEST)")
    else:
        aest_map = {}
        print(f"  AEST column missing - upstream uses pure ASAP (no aging-aware floor)")
    # OPTION C: slack-based placement. Slack <= 0 -> critical -> ASAP.
    slack_map = {}
    if "slack_h" in times.columns:
        slack_map = dict(zip(times["lot_id"], pd.to_numeric(times["slack_h"], errors="coerce").fillna(1e9)))
    elif "slack_hours" in times.columns:
        slack_map = dict(zip(times["lot_id"], pd.to_numeric(times["slack_hours"], errors="coerce").fillna(1e9)))
    else:
        # Fallback: compute slack ourselves from LFT and a rough est
        for lid in times["lot_id"]:
            slack_map[lid] = 1e9   # treat all as non-critical
    print(f"  Slack data loaded for {sum(1 for v in slack_map.values() if v <= 0):,} critical lots (slack <= 0)")

    successors   = defaultdict(list)
    predecessors = defaultdict(list)
    for u, v in zip(edges["producer_lot"], edges["consumer_lot"]):
        if u in lot_set and v in lot_set:
            successors[u].append(v); predecessors[v].append(u)

    # ═══ CTP DEVIATION #33 (F1) — PER-EDGE CONSUMPTION QUANTITY ════════════════════════
    # phase3 emits `consume_qty` when config `edge_consume_qty` is true. It is the amount
    # THIS producer lot contributes to THIS consumer lot, FEFO-allocated from the TRUE BOM
    # consumption (see annotate_edge_consume_qty in phase3_dag_construction.py). With it,
    # _material_ready_ns's `need` becomes SUM(edge_qty) instead of SUM(predecessor lot_qty),
    # which was MEASURED 28.7x too large on this plan. Column ABSENT -> EDGE_QTY_ON stays
    # False and the gate is the v6 lot-sum, bit-identical.
    EDGE_QTY_ON = bool(cfg.get("edge_consume_qty", False)) and ("consume_qty" in edges.columns)
    edge_qty = {}
    if EDGE_QTY_ON:
        _cq = pd.to_numeric(edges["consume_qty"], errors="coerce").fillna(0.0)
        for u, v, q in zip(edges["producer_lot"], edges["consumer_lot"], _cq):
            if u in lot_set and v in lot_set:
                edge_qty[(u, v)] = float(q)
        print(f"  F1 EDGE CONSUME-QTY: ON — {len(edge_qty):,} edges carry a quantity, "
              f"total {sum(edge_qty.values()):,.0f} units "
              f"(v6 lot-sum equivalent would be "
              f"{sum(float(lot_qty.get(u,0.0) or 0.0) for u,_v in edge_qty):,.0f})")
    elif bool(cfg.get("edge_consume_qty", False)):
        print("  F1 EDGE CONSUME-QTY: !! requested but phase3 emitted NO `consume_qty` "
              "column — re-run phase3. Falling back to the v6 lot-sum need.")
    else:
        print("  F1 EDGE CONSUME-QTY: OFF — need = SUM(predecessor lot_qty) (v6).")

    # ═══ CTP DEVIATION #34 (F3) — OPENING COMPONENT WIP AS SUPPLY AT t=0 ═══════════════
    # phase5's material gate counted ONLY predecessors `in schedule`, i.e. lots PRODUCED
    # this month. Opening component stock — which phase1c has ALREADY netted out of the
    # demand, so it is NOT produced — was INVISIBLE, and the gate then waited for
    # production that the netting had deliberately removed. This is the component analogue
    # of phase6's `seed_kitpool_from_inventory` (green tyres), which has been live since
    # 2026-07-16.
    #
    # !! IT REQUIRES F1 AND IS REFUSED WITHOUT IT. With the v6 lot-sum `need` (28.7x the
    #    truth) the opening stock would be credited against a fictitious demand — a real
    #    double count. With F1's true `need` there is no double count, and the proof is
    #    the material balance printed below:
    #        SUM(true need) == SUM(production) + SUM(opening stock)     per item.
    #    Any item where need > production + opening is a REAL shortage; its consumers keep
    #    the old strict-precedence behaviour and the item is listed.
    #
    # ALLOCATION: FEFO — opening stock goes to the EARLIEST consumers (by consumer
    # first_need, then lot_id), never more than that consumer actually needs and never more
    # than the item has. Deterministic; no set iteration.
    SEED_COMP_WIP = bool(cfg.get("seed_component_wip_phase5", False))
    open_alloc = {}                       # (consumer_lot, item) -> qty covered at t=0
    _OPEN_NETTED = set()                  # v83: (consumer, item) whose edges already exclude open_alloc
    if SEED_COMP_WIP and not EDGE_QTY_ON:
        print("  F3 OPENING-COMPONENT-WIP: !! REFUSED — seed_component_wip_phase5 requires "
              "edge_consume_qty (F1). Seeding opening stock against the v6 lot-sum need "
              "would DOUBLE-COUNT it. Running WITHOUT F3.")
        SEED_COMP_WIP = False
    if SEED_COMP_WIP:
        try:
            from db_loader import load_wip_by_item
            _W = load_wip_by_item(cfg, verbose=False)
        except Exception as _we:
            print(f"  F3 OPENING-COMPONENT-WIP: inventory load failed ({_we}) — DISABLED")
            _W = {}
            SEED_COMP_WIP = False
        if SEED_COMP_WIP:
            _need_ci = defaultdict(float)                 # (consumer, item) -> true need
            for (u, v), q in edge_qty.items():
                _need_ci[(v, lot_item.get(u, ""))] += q
            _need_i = defaultdict(float)
            for (c, i), q in _need_ci.items():
                _need_i[i] += q
            _prod_i = defaultdict(float)
            for l in lot_ids:
                _prod_i[lot_item.get(l, "")] += float(lot_qty.get(l, 0.0) or 0.0)
            _BIGT = pd.Timestamp("2099-12-31")
            _cons_sorted = sorted(_need_ci.keys(),
                                  key=lambda k: (lot_first_need.get(k[0], _BIGT), str(k[0])))
            _rem_w = {}
            for i in _need_i:
                _rem_w[i] = float(_W.get(i, 0.0) or 0.0)
            _alloc_i = defaultdict(float)
            for (c, i) in _cons_sorted:
                w = _rem_w.get(i, 0.0)
                if w <= 1e-9:
                    continue
                take = _need_ci[(c, i)]
                if take > w:
                    take = w
                if take > 0.0:
                    open_alloc[(c, i)] = take
                    _rem_w[i] = w - take
                    _alloc_i[i] += take
            _short = {i: (_need_i[i] - _prod_i.get(i, 0.0) - float(_W.get(i, 0.0) or 0.0))
                      for i in _need_i}
            _short = {i: v for i, v in _short.items() if v > 1e-6}
            print(f"  F3 OPENING-COMPONENT-WIP: ON — {len(_alloc_i):,} items seeded, "
                  f"{sum(_alloc_i.values()):,.0f} units allocated to "
                  f"{len(open_alloc):,} (consumer,item) pairs at t=0 (FEFO).")
            print(f"     MATERIAL BALANCE (the no-double-count proof): "
                  f"true need {sum(_need_i.values()):,.0f} | production "
                  f"{sum(_prod_i.get(i,0.0) for i in _need_i):,.0f} | opening "
                  f"{sum(float(_W.get(i,0.0) or 0.0) for i in _need_i):,.0f} | items covered "
                  f"{len(_need_i)-len(_short):,}/{len(_need_i):,} | REAL SHORTAGE "
                  f"{sum(_short.values()):,.0f} units over {len(_short):,} items "
                  f"(these keep strict precedence).")
            if _short:
                for i, v in sorted(_short.items(), key=lambda kv: -kv[1])[:8]:
                    print(f"       short: {str(i)[:28]:28s} need {_need_i[i]:>12,.0f}  "
                          f"prod {_prod_i.get(i,0.0):>12,.0f}  open "
                          f"{float(_W.get(i,0.0) or 0.0):>10,.0f}  short {v:>12,.0f}")
    else:
        print("  F3 OPENING-COMPONENT-WIP: OFF — opening component stock is invisible to "
              "the material gate (v6).")

    campaigns = {}
    for lid in lot_ids:
        cid = lot_camp_id.get(lid, "")
        if not cid: continue
        c = campaigns.setdefault(cid, {"wire": lot_wire.get(lid, ""), "lots": []})
        c["lots"].append(lid)
    for cid, c in campaigns.items():
        big_n = pd.Timestamp("2099-12-31")
        c["lots"].sort(key=lambda l: (lot_first_need.get(l, big_n), l))
        c["n_total"] = len(c["lots"])
        c["ready"] = 0
    print(f"  Belt campaigns: {len(campaigns)}")

    in_deg = {l: 0 for l in lot_ids}
    for u, succs in successors.items():
        for v in succs:
            if v in in_deg: in_deg[v] += 1
    import heapq
    big_need = pd.Timestamp("2099-12-31")
    # V6_wave ROUND-ROBIN dispatch: (wave_first, mhe_index, first_need, item_code, lot_id).
    #   wave_first    -> W01 lots dispatch before W02 etc.
    #   mhe_index     -> drum 0 of every item across all items dispatches BEFORE drum 1
    #                   of any item. This is the anti-monopoly key: prevents one item
    #                   from running consecutively on a bottleneck mixer for hours.
    #   first_need    -> within same drum index, earlier-needed lots go first.
    #   item_code     -> alphabetical tie-break (consistent interleaving across runs).
    #   lot_id        -> final tie-break.
    #
    # ═══ CTP DEVIATION #24 — THE 4th KEY IS NOT A TIE-BREAK. IT IS *THE* KEY. ═════════
    # (A) DEFECT — the THIRD alphabetical-order bug in this codebase (after phase6's
    #     alphabetical kit-pool drain and phase5's hash-ordered `elig` set).
    #
    # WHAT IT WAS: `item_code` sat in slot 4 and the comment one line above calls it an
    #   "alphabetical tie-break (consistent interleaving across runs)". Its STATED PURPOSE
    #   IS DETERMINISM. Nobody ever wrote "dispatch B-compounds before S-compounds because
    #   B is more urgent" — that is not a rule anybody chose. It is a coin-flip with a
    #   stable seed.
    #
    # WHY IT DEGENERATED INTO THE PRIMARY SORT KEY ON CTP. The three keys ABOVE it are
    #   supposed to do the deciding. MEASURED on the real lot table (6,780 lots):
    #     wave_first  — live (10 waves).
    #     mhe_index   — live but WEAK: 6,313 lots are index 0 (93.1%); only 467 lots carry
    #                   1/2/3. It is CONSTANT (all 0) inside every cold-start group.
    #                   (NB: `mhe_TOTAL` is the one that is 100% inert — ==1 on all 6,780.
    #                    mhe_INDEX is NOT inert. Do not delete it. This key is left intact.)
    #     first_need  — live ACROSS waves, but CONSTANT INSIDE each wave's main group,
    #                   because phase2 de-pulses need-dates onto a daily grid.
    #   The result: 6,714 of 6,780 lots (99.0%) sit in a tie-group of size > 1 in which the
    #   first three keys are ALL EQUAL and `item_code` ALONE decides the order. The
    #   cold-start group — (W01, mhe 0, first_need = 2026-06-01 07:00) — is 201 lots deep.
    #   A determinism device was silently promoted to the plant's dispatch policy.
    #
    # WHAT IT COST, TRACED END-TO-END (the true critical path to the first green tyre):
    #     S53_L0001  FINAL MIXING   0.03 h  (1.9 MINUTES of work)   ran at h=8.30
    #       -> BW13002_L0001  BEAD WINDING   h=15.80
    #       -> BA13001_L0001  BEAD APEXING   h=24.84 - 29.94
    #       -> GT 1503 NEO MSIL_L0001  BUILDING  h=30.06   <- the FIRST green tyre
    #   S53 is 19th ALPHABETICALLY among the 30 cold-start mixing lots (rank 151 of the
    #   201-lot group), so a 1.9-MINUTE bead compound on the critical path waited 8.3 HOURS
    #   behind B*, H*, I*, J*, P*, R* while final mixing ran at 96.3% on its 2 machines.
    #   At schedule_open_h=0 the presses then sat with NO new green tyre for 30.06 h.
    #
    # THE REPAIR: replace slot 4 with phase4's **LFT** (Latest Finish Time), ascending.
    #   NOTHING IS INVENTED. phase4_cpm.py ALREADY computes LFT by a backward CPM pass
    #   anchored at the press: terminal LFT = block.need_by - min_aging, then
    #   LFT_u = MIN over consumers v of (LST_v - lag).
    #   So LFT IS, definitionally, "the latest this lot may finish without delaying the
    #   earliest press block it ultimately serves" = the press deadline minus the
    #   critical-path distance to that press. Dispatching by LFT ascending == "the lot whose
    #   successor chain has the tightest downstream deadline goes FIRST" — which is what was
    #   asked for, and which is EDD (earliest-due-date) on the derived deadline, the
    #   classical rule for minimising maximum lateness. Read straight out of
    #   phase4_lot_times_updated.csv; 0 nulls on all 6,780 lots. `lot_id` stays the FINAL
    #   key, so the pass remains fully DETERMINISTIC.
    #
    # !! TWO THINGS I GOT WRONG ON THE WAY, RECORDED SO NOBODY REDOES THEM !!
    #   (1) I TRIED LST FIRST (the "obvious" key — latest START) AND IT MADE EVERYTHING
    #       WORSE: makespan 31.84 -> 32.01 d, first GT build 30.06 h -> 61.02 h. The reason
    #       is the whole lesson of this deviation and is written out at `dispatch_priority`
    #       below: LST = LFT - duration, so it PROMOTES A LONG JOB PURELY FOR BEING LONG,
    #       which is right in a backward pass and pathological in a capacitated queue.
    #       `slack` (= LST - EST) carries the same duration term and fails identically.
    #   (2) I THEN ASSUMED `item_code` WAS SECRETLY DOING FAMILY/GROUP SCHEDULING (keeping
    #       an item's lots contiguous so CTP #20's pooled material gate is satisfied early)
    #       AND BUILT A FAMILY-MIN-LST KEY TO PRESERVE THAT. IT IS A NO-OP AND THE PREMISE
    #       IS FALSE. MEASURED: all 6,780 lots have a UNIQUE (wave, mhe_index, first_need,
    #       item_code) — every "family" is a single lot, so there is no grouping to preserve
    #       and the family key produced a byte-identical topo order. The lots of one item are
    #       separated by `mhe_index` (key #2) and `first_need` (key #3), which sit ABOVE
    #       slot 4 and are untouched by any of this. Do not re-derive that idea.
    #
    # ── SLOT 4 IS SELECTABLE. `dispatch_priority` in config.yaml. ─────────────────────
    #   "lft"  (DEFAULT, = THE FIX) phase4 Latest Finish Time asc — EDD on the press deadline
    #   "item_code"                 v6, bit-for-bit. The alphabet. The defect.
    #   "lst"                       phase4 Latest Start Time asc  — MEASURED WORSE, see below
    #   "slack"                     phase4 slack_h asc (least-slack / STR) — MEASURED WORSE
    #
    # MEASURED, all four, same config (schedule_open_h 24, time-phased building ON):
    #     key         makespan  30d      35d     starve_max  CO   lots_dead  first GT build
    #     item_code   31.84 d   97.02%   100%    52.4 h      530  18.4%      30.06 h   <- v6
    #     lst         32.01 d   96.50%   100%    60.4 h      530  18.0%      61.02 h   <- WORSE
    #     slack       32.01 d   96.41%   100%    60.4 h      530  21.5%      61.02 h   <- WORSE
    #     lft         31.45 d   97.60%   100%    44.2 h      530  12.9%       1.92 h   <- FIX
    #   LFT is better than v6 on EVERY axis and costs ZERO extra changeovers.
    #
    # !! WHY LST AND slack FAIL AND LFT DOES NOT — THIS IS THE WHOLE POINT. !!
    #   phase4 is a pure CPM: LST/LFT/slack are INFINITE-CAPACITY quantities that know the
    #   DAG's lags but NOTHING about machine QUEUES. LST = LFT - duration. That subtraction
    #   means A LONG JOB GETS AN EARLIER LST *PURELY FOR BEING LONG*. In a backward pass that
    #   is correct (it must start earlier to finish on time). AS A DISPATCH PRIORITY IN A
    #   CAPACITATED QUEUE IT IS PATHOLOGICAL: it lets the long job seize the machine first.
    #   TRACED, on calender 901, the two lots that decide the cold start:
    #       CAP 66-MOTHERROLL  dur 1.08 h   LST 8.05 h   LFT 9.13 h
    #       ECOPOLY350         dur 5.45 h   LST 4.26 h   LFT 9.71 h
    #     LST   -> ECOPOLY350 first (4.26 < 8.05). The mother roll slips 5.5 h. That shrinks
    #             the free window before CAPSTRIP_L0004's JIT target from 8.8 h to 3.3 h, and
    #             CAP 66 - CAPSTRIP_L0003 is a 5.35-HOUR lot, so IT NO LONGER FITS —
    #             first_fit_jit drops it through every later gap to h=55.59 (it was 13.87).
    #             Every early green tyre then collapses onto h=61.02, the hour it finishes.
    #             A 5.5-hour myopic error at a calender became a 31-hour hole at the presses.
    #     LFT   -> mother roll first (9.13 < 9.71). Correct. The chain holds.
    #   LFT is EDD on the derived press deadline — the classical, robust rule for minimising
    #   maximum lateness — and it does NOT carry LST's long-job promotion. slack (= LST - EST)
    #   inherits the same duration term and fails the same way (identical 61.02 h).
    #
    # CLASSIFICATION: (A) DEFECT. Nobody CHOSE alphabetical. The code's own comment one line
    #   up calls item_code a "tie-break ... (consistent interleaving across runs)" — its
    #   stated job is DETERMINISM, not priority. It became the decider only because the three
    #   keys above it collapsed on CTP data: MEASURED, 6,714 of 6,780 lots (99.0%) sit in a
    #   tie-group where wave/mhe_index/first_need are ALL EQUAL and the alphabet ALONE orders
    #   them; the cold-start group is 201 lots deep with 201 distinct item codes. A
    #   determinism device was silently promoted to the plant's dispatch policy. So: fixed,
    #   not flagged. DEFAULT = lft.
    #   (v6's alphabet did get the calender pair right BY LUCK — "CAP 66-MOTHERROLL" <
    #    "ECOPOLY350" because C < E. LFT gets it right BY THE DEADLINE. Same answer there,
    #    but LFT also gets S53 right, which the alphabet did not: S53_L0001 — a 1.9-MINUTE
    #    bead compound on the critical path to the first green tyre — was rank 151/201 and
    #    waited 8.3 h; under LFT it is rank 1/201.)
    #
    # WHAT IT COSTS: nothing measured. Building changeovers 530 -> 530, building peak 606.1 h
    #   unchanged, press SKU-transitions 56 -> 56, press block sequence 0 of 86 reordered,
    #   negative-gap 0, incomplete-material 0, material starvation 0. `lot_id` remains the
    #   FINAL key so the pass stays DETERMINISTIC (verified: two consecutive runs identical).
    #   It is NOT capacity-aware — no CPM number is. A queue-aware priority (buffer
    #   penetration / shifting bottleneck) would be strictly better and is (C) ALGORITHM,
    #   out of bounds here. Flagged, not smuggled in.
    # ── BR-V8 MIXERS IN DUE-DATE ORDER (2026-10-01, BUSINESS_RULES_LOG.md) ─────────────────
    # v5 measured 949 of 1,740 mixer lots (55%) running while a READY lot due >= 12 h earlier
    # waited behind them. Master/final compound lots are placed FIRST, in order of their LFT (the
    # real due time from curing, via the DAG), instead of (3-day wave, first_need): the most urgent
    # compound gets first pick of mixer time. Their start target becomes "as early as inputs
    # allow", capped so the compound is still inside its shelf life (minus safety) at its LFT.
    _MEDD = cfg.get("mixer_edd") or {}
    MIX_EDD = bool(_MEDD.get("enabled", False))
    _MIX_T = {"MASTER COMPOUND", "FINAL COMPOUND"}
    _MEDD_SAFE_NS = int(float(_MEDD.get("safety_h", 4)) * 3600 * 1_000_000_000)   # NS is bound later in main()
    # ── BR-V9 TBR COMPONENTS AS EARLY AS INPUTS ALLOW (2026-10-01, BUSINESS_RULES_LOG.md) ──────
    # v5: TBR builds planned 29-Sep 23:00 slid 12-29 h because the SQ -> TIL (calender 1901) ->
    # TPA (2201) chain was timed to deadlines ~16-24 h after the build slot while 1901 sat idle
    # ~29 h. TBR-only components (item_tyre_type_live = TBR; compounds and building excluded) now
    # target "as early as inputs allow", capped so the item is still within its shelf life (minus
    # safety) at its building-plan need (phase2 first_need). CTP-GEN-01: follow the building plan.
    _TBA = cfg.get("tbr_components_asap") or {}
    TBR_ASAP = bool(_TBA.get("enabled", False))
    _TBA_SAFE_NS = int(float(_TBA.get("safety_h", 4)) * 3600 * 1_000_000_000)
    TBR_ITEMS = set()
    if TBR_ASAP:
        try:
            _tt = pd.read_csv(ROOT / "ctp_inputfiles" / "item_tyre_type_live.csv", dtype=str)
            TBR_ITEMS = set(_tt.loc[_tt["tyre_type"].str.upper().str.strip() == "TBR", "item"].astype(str).str.strip())
        except Exception as _e:
            print(f"  [BR-V9] !! TBR item list unreadable ({type(_e).__name__}: {_e}) -- rule inactive")
        _n9 = sum(1 for l in lot_ids if str(lot_item.get(l, "")).strip() in TBR_ITEMS
                  and str(lot_item_type.get(l, "")).upper() not in _MIX_T
                  and str(lot_dept.get(l, "")).upper() not in ("BUILDING", "CURING"))
        print(f"  [BR-V9] TBR components as early as inputs allow: {_n9:,} lots of {len(TBR_ITEMS):,} TBR items")
    if MIX_EDD:
        _nm = 0
        for l in lot_ids:
            if str(lot_item_type.get(l, "")).upper() in _MIX_T:
                _lf = lft_map.get(l)
                if _lf is not None and not pd.isna(_lf):
                    lot_wave_first[l] = "W00"            # ahead of every production wave
                    lot_first_need[l] = pd.Timestamp(_lf)
                    _nm += 1
        print(f"  [BR-V8] mixer EDD ON: {_nm:,} compound lots placed first, in order of their LFT")
    _DP = str(cfg.get("dispatch_priority", "lft")).strip().lower()
    if _DP not in ("item_code", "lst", "lft", "slack"):
        print(f"  [WARN] dispatch_priority={_DP!r} unknown -> falling back to v6 'item_code'")
        _DP = "item_code"
    # Pre-resolve to plain numbers ONCE. Integers/floats give a total, NaT-free ordering; a
    # raw NaT inside a heap key compares inconsistently and would silently corrupt the order
    # (the present-but-blank trap). A lot with no value sorts LAST, never first.
    _BIG_NS = pd.Timestamp("2099-12-31").value
    def _ts_ns(v):
        if v is None or pd.isna(v):
            return _BIG_NS
        return int(pd.Timestamp(v).value)
    if _DP == "lst":
        _prio = {l: _ts_ns(lst_map.get(l)) for l in lot_ids}
    elif _DP == "lft":
        _prio = {l: _ts_ns(lft_map.get(l)) for l in lot_ids}
    elif _DP == "slack":
        _prio = {l: float(slack_map.get(l, 1e9)) for l in lot_ids}
    else:
        _prio = None
    _n_missing = (0 if _prio is None
                  else sum(1 for l in lot_ids if _prio[l] in (_BIG_NS, 1e9)))

    # ═══ CTP DEVIATION #39 (T1) — `mhe_index` IN THE DISPATCH KEY.   DEFAULT v6. ═════
    # ══════════════════════════════════════════════════════════════════════════════════
    # WHAT IT IS. Slot 2 of the dispatch key, ABOVE `first_need` (slot 3). Its stated
    # purpose (comment at the key, above) is ANTI-MONOPOLY: "drum 0 of every item across
    # all items dispatches BEFORE drum 1 of any item ... prevents one item from running
    # consecutively on a bottleneck mixer for hours". That rule is only meaningful if a
    # lot HAS sub-drums, i.e. if `mhe_total > 1`.
    #
    # MEASURED on THIS lot table (`_five_exp/t1_facts.py`, 8,070 lots):
    #   * mhe_total == 1 on 8,070 of 8,070 lots (100.0%). There ARE no sub-lots. The
    #     sub-lot streaming model this key indexes into is DEAD on CTP (see CTP #20).
    #   * mhe_index still ranges 0..13 and 787 lots carry a NON-ZERO value, because
    #     phase2 assigns it as a within-item SEQUENCE NUMBER, not a drum number.
    #   * So slot 2 orders lots by "how many earlier lots of my own item exist", ABOVE
    #     the date they are actually needed.
    #   * COST: 204,845 inverted ordered pairs; 5,183 of 8,070 lots (64.2%) dispatch
    #     ahead of at least one lot needed EARLIER, inside the same wave.
    #     `CAP 66 - CAPSTRIP_L0003` (mhe 2, needed h=8) dispatches BEHIND
    #     `CAP 66 - CAPSTRIP_L0021` (mhe 0, needed h=102).
    #   * 117 of 911 GT lots (12.8%) have a LAST-FINISHING (binding) component at
    #     mhe_index >= 1.
    #
    # dispatch_mhe_index:  v6 (default, slot 2) | off (drop) | demote (below first_need)
    # `off` and `demote` both add a CONSTANT term where the other one sits, so the tuple
    # arity never changes and `v6` is provably byte-identical to the previous key.
    # NB: YAML 1.1 turns a BARE `off` into the boolean False (and `on`/`yes`/`no` likewise).
    # Normalise it back, or an unquoted `off` silently falls through to the v6 default and
    # the experiment reports "no change" for a lever that never ran. (This actually
    # happened on the first T1 run; the unknown-value guard below is what caught it.)
    _MHE_RAW = cfg.get("dispatch_mhe_index", "v6")
    if isinstance(_MHE_RAW, bool):
        _MHE_RAW = "off" if _MHE_RAW is False else "v6"
    _MHE = str(_MHE_RAW).strip().lower()
    if _MHE not in ("v6", "off", "demote"):
        print(f"  [WARN] dispatch_mhe_index={_MHE!r} unknown -> falling back to 'v6'")
        _MHE = "v6"
    if _MHE == "off":
        def _mhe_hi(l): return 0
        def _mhe_lo(l): return 0
    elif _MHE == "demote":
        def _mhe_hi(l): return 0
        def _mhe_lo(l): return lot_mhe_index.get(l, 0)
    else:
        def _mhe_hi(l): return lot_mhe_index.get(l, 0)
        def _mhe_lo(l): return 0
    if _MHE != "v6":
        _nz = sum(1 for l in lot_ids if lot_mhe_index.get(l, 0) != 0)
        print(f"  CTP #39 dispatch_mhe_index={_MHE} — mhe_index {'REMOVED FROM' if _MHE=='off' else 'DEMOTED BELOW first_need IN'} "
              f"the dispatch key ({_nz:,} of {len(lot_ids):,} lots carried a non-zero value; "
              f"mhe_total==1 on all -> the field indexes nothing)")

    # ═══ CTP DEVIATION #38 (J1/J2/J3) — TIE-BREAK ONLY.            DEFAULT OFF. ═══════
    # ══════════════════════════════════════════════════════════════════════════════════
    # SCOPE, STATED FIRST BECAUSE IT IS THE WHOLE POINT. This lever REORDERS ONLY LOTS
    # THAT ARE ALREADY TIED on (wave_first, mhe_index, first_need). It is inserted at
    # slot 4, BEHIND all three v6 keys and AHEAD of item_code. NOTHING THAT DIFFERS ON
    # first_need CAN MOVE. That is the difference from CTP #37 / F2, which replaced or
    # preceded a live key and were monotonically worse. Every lot outside the configured
    # scope gets the CONSTANT 0.0, and slot 4 is only ever compared between lots whose
    # first three keys are equal, so a constant there is a provable no-op for that group.
    #
    # WHY (measured, _tie_exp/repro.py + _tie_exp/headroom.py, shipped 33.315 d run):
    #   tread: 266 lots, only 35 distinct first_need; the largest tie group is 26 lots all
    #   at first_need = 2026-06-01 07:00. Inside it the order is the ALPHABET, and
    #   `TRD 2256 ROYL` is 19th of 26 purely because "2256" sorts after "1402" AS TEXT.
    #   Globally 7,892 of 8,070 lots (97.8%) sit in a multi-lot tie group; the largest is
    #   350. Press 4836's first kit (GT 2256 ROYL, 9 components, 8 at zero stock) is
    #   gated by TRD 2256 ROYL finishing at 34.69 h (queue 23/325 on extruder 1002) and
    #   SWRS27 at 23.55 h (18/286 on 1001), against own durations of 0.79 h and 0.42 h.
    #   headroom.py proves EVERY lot ahead of them is in the SAME tie group — 27.88 h and
    #   18.68 h of REORDERABLE work, 0.00 h frozen. So the ceiling of this lever on that
    #   kit is real: cap strip (5.48 h, already queue 1/122) becomes the binding member.
    #
    # J2 CAMPAIGN GUARD — MEASURED TO BE STRUCTURALLY AUTOMATIC HERE, NOT ASSUMED.
    #   All 8,070 lots have a UNIQUE (wave_first, mhe_index, first_need, item_code):
    #   max lots per (tie-group, item_code) = 1, pairs with >1 lot = 0. A tie group
    #   therefore contains AT MOST ONE lot of any code, so no slot-4 term can split a
    #   code group — there is nothing to split. (This is the same fact CTP #24 recorded.)
    #   F2 blew changeovers 370 -> 763 by touching the BUILDING LAYOUT key, where code
    #   groups really are multi-lot (up to 14 GT lots per (due_day, code)). This lever
    #   does not touch that key. The check is re-run and PRINTED every run below, and
    #   building changeovers are reported by the pipeline — do not take it on trust.
    #
    # THE SIGNALS (`tie_break`), and what each was measured to do to the traced lots
    # (rank inside their own 210-lot tie group, before -> after; _tie_exp/signals.py):
    #   last_need  J1 as literally specified: phase2's last_need, the CTP #32 rope signal.
    #              MEASURED A NO-OP ON THE TRACED MECHANISM: inside the cold tie group
    #              last_need has only 3 distinct values and 24 of the 26 tread lots share
    #              the latest one, so TRD 2256 ROYL 202 -> 202 and SWRS27 166 -> 166.
    #              It does split 159 of 273 groups, so it is NOT inert globally — it just
    #              does not point where the evidence points. Kept, because it is the
    #              literal ask and its result is a finding.
    #   seed       J3, the surgical one. 0.0 for a lot whose item_code is in
    #              tie_break_seed_file (the 25 components of the first kits of the 26
    #              presses finishing within 1.0 d of critical), 1.0 otherwise. With
    #              scope=cold this promotes exactly 26 lots inside one 212-lot group.
    #   press_end  the real downstream-need signal: MINUS the observed end time of the
    #              LATEST press this lot ultimately feeds, from a PREVIOUS run's snapshot
    #              (never live outputs2 — the stale-file trap). Strongest discriminator
    #              measured: splits 250 of 273 groups, residual largest sub-group 69,
    #              and moves all 7 traced lots to rank 2-14 of 210.
    #              !! WITH scope=all THIS IS ARITHMETICALLY IDENTICAL TO CTP #37's
    #              REJECTED FB_disp (phase5:777 `_slack = -_pe` then per-lot `.min()`
    #              == -max press end), which measured 34.280 d. Only scope=cold is new.
    #
    # `tie_break_scope`:
    #   cold  the signal is applied ONLY to lots in the EARLIEST first_need group (the
    #         cold start, 212 lots / 210 codes). Everything later keeps v6's alphabet.
    #         This is the narrow version: the ramp is where an idle hour is unrecoverable.
    #   all   every lot is scored. For press_end this reproduces FB_disp — see above.
    _TB       = str(cfg.get("tie_break", "off") or "off").strip().lower()
    if _TB in ("false", "no", "none", ""):
        _TB = "off"
    if _TB not in ("off", "last_need", "seed", "press_end"):
        print(f"  [WARN] tie_break={_TB!r} unknown -> off")
        _TB = "off"
    _TB_SCOPE = str(cfg.get("tie_break_scope", "cold") or "cold").strip().lower()
    if _TB_SCOPE not in ("cold", "all"):
        print(f"  [WARN] tie_break_scope={_TB_SCOPE!r} unknown -> cold")
        _TB_SCOPE = "cold"
    _tb_val = {}
    if _TB != "off":
        # MUTUAL EXCLUSION, LOUD. Three levers now want slot 4 of the same tuple. Half-
        # applying two of them would produce a schedule nobody can attribute to a rule.
        if _prio is not None:
            raise SystemExit(f"CONFIG ERROR: tie_break={_TB} and dispatch_priority={_DP} "
                             f"both ON — both write slot 4 of the dispatch key. Set "
                             f"dispatch_priority: item_code (v6) to use tie_break.")
        if DRUM_PRIORITY != "off" and DRUM_DISPATCH:
            raise SystemExit(f"CONFIG ERROR: tie_break={_TB} and drum_priority="
                             f"{DRUM_PRIORITY} (dispatch leg) both ON — both write slot 4 "
                             f"of the dispatch key. Turn one off.")
        # scope mask: which lots may carry a non-neutral value
        _cold_ns = min((lot_first_need.get(l, big_need) for l in lot_ids),
                       default=big_need)
        _in_scope = ((lambda l: True) if _TB_SCOPE == "all"
                     else (lambda l: lot_first_need.get(l, big_need) == _cold_ns))
        _raw = {}
        if _TB == "last_need":
            for l in lot_ids:
                _n = lot_last_need.get(l)
                _raw[l] = float(_BIG_NS if _n is None else _n)
        elif _TB == "seed":
            _sf = pathlib.Path(str(cfg.get("tie_break_seed_file", "")).strip())
            if not _sf.is_absolute():
                _sf = ROOT.parent / _sf
            _seed = set(pd.read_csv(_sf)["component"].astype(str).str.strip())
            for l in lot_ids:
                _raw[l] = 0.0 if str(lot_item.get(l, "")).strip() in _seed else 1.0
            print(f"  CTP #38 seed list: {_sf.name} -> {len(_seed)} codes, "
                  f"{sum(1 for v in _raw.values() if v == 0.0):,} lots matched")
        else:                                            # press_end
            _fb = pathlib.Path(str(cfg.get("tie_break_feedback_dir", "")).strip())
            if not _fb.is_absolute():
                _fb = ROOT.parent / _fb
            _fc = pd.read_csv(_fb / "phase6_curing_revision_updated.csv", low_memory=False)
            _fe = pd.to_datetime(_fc["endTime"])
            _peh = ((_fe - _fe.min()).dt.total_seconds() / 3600.0)
            _pe = _peh.groupby(_fc["pressNo"].astype(str).str.strip()).max()
            _plr = _db_or_csv("plan", cfg).copy()
            _bfr = _plr[(_plr["skuCode"].astype(str).str.strip() != "CHANGEOVER")
                        & (pd.to_numeric(_plr["qty"], errors="coerce").fillna(0) > 0)
                        ].reset_index(drop=True)
            _b2pr = pd.Series(_bfr["pressNo"].astype(str).str.strip().values,
                              index=[f"B{i:04d}" for i in range(len(_bfr))])
            _blr = blocks[["lot_id", "block_id"]].copy()
            _blr["_s"] = (_blr["block_id"].astype(str).str.strip()
                          .map(_b2pr).map(-_pe))       # -end : latest press sorts FIRST
            _cov = float(_blr["_s"].notna().mean()) * 100.0
            _m = _blr.groupby("lot_id")["_s"].min().dropna().to_dict()
            for l in lot_ids:
                _raw[l] = float(_m.get(l, 0.0))
            print(f"  CTP #38 press_end feedback: {_fb.name} "
                  f"(horizon {_pe.max()/24:.3f} d, {_pe.nunique()} distinct press ends, "
                  f"block->press coverage {_cov:.2f}%)")
        for l in lot_ids:
            _tb_val[l] = _raw[l] if _in_scope(l) else 0.0
        # DEGENERACY + CAMPAIGN-GUARD PROOF, printed so a dead key cannot hide.
        _grp = defaultdict(list)
        for l in lot_ids:
            _grp[(lot_wave_first.get(l, "Z99"), lot_mhe_index.get(l, 0),
                  lot_first_need.get(l, big_need))].append(l)
        _multi = [g for g in _grp.values() if len(g) > 1]
        _split = sum(1 for g in _multi if len({_tb_val[l] for l in g}) > 1)
        _resid = max((max(sum(1 for l in g if _tb_val[l] == v) for v in {_tb_val[x] for x in g})
                      for g in _multi), default=0)
        _dupcode = sum(1 for g in _grp.values()
                       if len({lot_item.get(l, "") for l in g}) != len(g))
        print(f"  CTP #38 TIE-BREAK: {_TB} scope={_TB_SCOPE} — splits {_split:,} of "
              f"{len(_multi):,} multi-lot tie-groups | residual largest sub-group "
              f"{_resid:,} (was {max((len(g) for g in _multi), default=0):,}) | "
              f"{sum(1 for l in lot_ids if _tb_val[l] != 0.0):,} lots carry a non-neutral "
              f"value | J2 campaign guard: {_dupcode} tie-groups contain 2+ lots of one "
              f"code (0 => contiguity is structural, nothing can be split)")

    # CTP #37 — DRUM PRIORITY on the TOPOLOGICAL DISPATCH key. Inserted AHEAD of the
    # item-code term and BEHIND (wave, mhe_index, first_need), so it only ever decides
    # what item-code ASCII was deciding before. MEASURED (_drum_exp/degeneracy.py):
    # 7,892 of 8,070 lots sit in a multi-lot tie-group on (wave, mhe_index, first_need)
    # and crit discriminates 205 of the 273 such groups. Extruder 1002's whole head is
    # ONE group of 26 lots — see the CTP #37 write-up.
    _DRUM_ON_DISPATCH = (DRUM_PRIORITY != "off" and DRUM_DISPATCH)
    if _TB != "off":
        def _key(l):                                   # CTP #38 — TIE-BREAK ONLY
            # slot 4 sits BEHIND the three v6 keys, so it is only ever consulted between
            # lots that are ALREADY TIED on (wave, mhe_index, first_need). item_code
            # stays in slot 5 (determinism), lot_id in slot 6 (total order).
            return (lot_wave_first.get(l, "Z99"),
                    _mhe_hi(l),                        # CTP #39
                    lot_first_need.get(l, big_need),
                    _mhe_lo(l),                        # CTP #39
                    _tb_val[l],
                    lot_item.get(l, ""),
                    l)
        print(f"  DISPATCH KEY: (wave, mhe_index, first_need, TIE_BREAK[{_TB}/"
              f"{_TB_SCOPE}] asc, item_code, lot_id)  [CTP #38 — reorders TIED lots only; "
              f"nothing that differs on first_need can move]")
    elif _prio is None and not _DRUM_ON_DISPATCH:
        def _key(l):                                   # v6, bit-for-bit
            return (lot_wave_first.get(l, "Z99"),
                    _mhe_hi(l),                        # CTP #39
                    lot_first_need.get(l, big_need),
                    _mhe_lo(l),                        # CTP #39
                    lot_item.get(l, ""),
                    l)
        print("  DISPATCH KEY: (wave, mhe_index, first_need, ITEM_CODE, lot_id)  "
              "[!! v6's ALPHABET — this is the CTP #24 DEFECT, restored on request. It "
              "decides 99.0% of all lots. Default is dispatch_priority: lft]")
    elif _prio is None:
        def _key(l):                                   # CTP #37 — drum priority only
            return (lot_wave_first.get(l, "Z99"),
                    _mhe_hi(l),                        # CTP #39
                    lot_first_need.get(l, big_need),
                    _mhe_lo(l),                        # CTP #39
                    _dcrit_lot(l),                     # min slack of the presses it feeds
                    lot_item.get(l, ""),
                    l)
        print(f"  DISPATCH KEY: (wave, mhe_index, first_need, DRUM_CRIT asc, ITEM_CODE, "
              f"lot_id)  [CTP #37 scope={DRUM_PRIORITY}; "
              f"{len(lot_ids) - sum(1 for l in lot_ids if l in lot_drum_crit):,} lots "
              f"unscored -> neutral 0.0 (v6 order among themselves)]")
    else:
        def _key(l):                                   # CTP #24 — THE FIX
            return (lot_wave_first.get(l, "Z99"),
                    _mhe_hi(l),                        # CTP #39
                    lot_first_need.get(l, big_need),
                    _mhe_lo(l),                        # CTP #39
                    _prio[l],                          # downstream press urgency
                    lot_item.get(l, ""),               # determinism (unique per tie-group)
                    l)
        _note = ("EDD on the press deadline — CTP #24, the fix" if _DP == "lft"
                 else "!! MEASURED WORSE THAN v6 on CTP — read CTP #24 before using")
        print(f"  DISPATCH KEY: (wave, mhe_index, first_need, {_DP.upper()} asc, item_code, "
              f"lot_id)  [{_note}. {_n_missing:,} lots have no {_DP.upper()} and sort last]")
    heap = []
    for l, d in in_deg.items():
        if d == 0:
            heapq.heappush(heap, (_key(l), l))
    topo = []
    while heap:
        _, u = heapq.heappop(heap)
        topo.append(u)
        for v in successors.get(u, ()):
            in_deg[v] -= 1
            if in_deg[v] == 0:
                heapq.heappush(heap, (_key(v), v))
    if len(topo) < len(lot_ids):
        seen = set(topo)
        for l in lot_ids:
            if l not in seen: topo.append(l)
    print(f"  Topological order computed: {len(topo):,} lots")

    # === CASCADING ORPHAN DETECTION (production-scheduling expert fix) ===
    # A lot is an orphan if it has NO downstream consumer that reaches a
    # terminal (Building / Curing). Single-level orphan check misses
    # CASCADES: cutter → orphan, then Stage-1 → orphan, then FRC → orphan.
    # We iteratively mark orphans backward from terminals until convergence.
    #
    # Algorithm (reverse-topo backward sweep):
    #   1. Mark all terminal lots (Building / Curing) as "reaches_terminal".
    #   2. For each lot in REVERSE topo order, if any of its successors
    #      reaches a terminal, this lot also reaches terminal.
    #   3. Otherwise, this lot is an orphan (no productive consumer).
    #
    # Lots marked as orphans are SKIPPED in the placement loop below.
    reaches_terminal = set()
    for lid in lot_ids:
        if _is_terminal_dept(lot_dept.get(lid, "")):
            reaches_terminal.add(lid)
    # Process in REVERSE topo so we mark consumers before producers.
    for lid in reversed(topo):
        if lid in reaches_terminal:
            continue
        for s in successors.get(lid, ()):
            if s in reaches_terminal:
                reaches_terminal.add(lid)
                break
    cascade_orphans = set()
    for lid in lot_ids:
        if lid not in reaches_terminal:
            cascade_orphans.add(lid)
    print(f"  Cascading orphans detected: {len(cascade_orphans):,} lots "
          f"(out of {len(lot_ids):,} total — {100*len(cascade_orphans)/max(len(lot_ids),1):.1f}%)")

    # === QUANTITY-AWARE OVER-PRODUCTION DETECTION (DISABLED) ===
    # Previously: counted consumer LOTS but a single Stage-2 GT lot might
    # consume MULTIPLE carcass MHEs. Counting by lot drastically under-
    # estimated demand and dropped 78% of valid lots (47K -> 10K placed,
    # 0 GT produced — completely broken schedule).
    #
    # The proper fix requires QUANTITY tracking: sum producer qtys per item
    # and compare to BOM-cascaded consumer qty demand. That's a Phase 1c/
    # Phase 2 fix, not a Phase 5 retrofit.
    #
    # For now, rely on:
    #   1. Cascading orphan detection (reaches_terminal) — handles dead-end chains
    #   2. Phase 1c MRP cascade — already removes WIP-covered demand
    # The remaining FRC > Cutter inversion is acceptable: FRC has some
    # over-production but it doesn't break the schedule horizon.

    machine_busy = defaultdict(list)
    # BR-10 (2026-09-30): planned PM / MTC downtime (jkplanning_CTP.downtime_reason) is BUSY time.
    # Building ids map to the plan's TBM names (3401..3411 -> TBMPCR<n>Stage2, 3801..3809 -> TBM-0<n>).
    DOWNTIME_WIN = defaultdict(list)
    if cfg.get("enforce_pm_downtime", False):
        try:
            _dt = pd.read_csv(ROOT / "db_pipeline" / "raw" / "jkplanning_CTP__downtime_reason.csv")
            for _m, _sec, _a, _b in zip(_dt["Machine ID"].astype(str).str.strip(), _dt["Section"].astype(str),
                                        pd.to_datetime(_dt["Scheduled Start Time"], errors="coerce"),
                                        pd.to_datetime(_dt["Scheduled End Time"], errors="coerce")):
                if pd.isna(_a) or pd.isna(_b) or "CURING" in _sec.upper():
                    continue                              # presses: phase6
                _k = _m
                if _m.isdigit() and _m.startswith("34"):
                    _k = f"TBMPCR{int(_m[2:])}Stage2"
                elif _m.isdigit() and _m.startswith("38"):
                    _k = f"TBM-{int(_m[2:]):02d}"
                DOWNTIME_WIN[_k].append((int(pd.Timestamp(_a).value), int(pd.Timestamp(_b).value)))
            for _k, _ws in DOWNTIME_WIN.items():
                for _w in _ws:
                    bisect.insort(machine_busy[_k], _w)
            print(f"  [BR-10] PM/MTC downtime blocked on {len(DOWNTIME_WIN)} building/upstream machines "
                  f"({sum(len(v) for v in DOWNTIME_WIN.values())} windows)")
        except Exception as _e:
            print(f"  [BR-10] !! downtime_reason unreadable ({type(_e).__name__}) -- PM not enforced")
    machine_last_camp_wire = defaultdict(str)
    machine_last_camp_end_ns = defaultdict(int)
    machine_busy_ns = defaultdict(int)
    machine_last_item = defaultdict(str)
    machine_last_itype = defaultdict(str)

    # === BUILDING MACHINE CYCLE TIMES (per-machine real plant data) ===
    # Override routing's per-SKU proc_time with per-machine cycle_sec (if a file is
    # provided). Empty -> _bld_dur_ns falls back to routing-based duration.
    machine_cycle_sec = {}  # machine_code -> cycle_sec for building MGs
    try:
        try:
            bct = _db_or_csv("building_cycle_times", cfg)
        except Exception:
            bct_path = INPUTS / "building_cycle_times.csv"
            bct = pd.read_csv(bct_path) if bct_path.exists() else pd.DataFrame()
        if not bct.empty:
            for _, r in bct.iterrows():
                m = str(r["Machine"]).strip()
                cs = float(r["cycle_sec"])
                if m and cs > 0:
                    machine_cycle_sec[m] = cs
            print(f"  BUILDING CYCLE TIMES: {len(machine_cycle_sec)} machines loaded")
    except Exception as _e:
        print(f"  BUILDING CYCLE TIMES: load skipped ({_e})")

    # === BUILDING CAMPAIGN INPUTS (v2) ===
    # Inch per SKU (inch-lock) + lot->SKU (primary). Building lots are placed
    # as inch-locked, per-SKU campaigns with a flat 30-min changeover — see
    # place_building_campaigns() below.
    BUILDING_ITYPES = {"GREEN TYRES", "CARCASS"}
    # Changeover from the previous building code (Master_Building_ChangeoverTime
    # fallback): same-size 40 min, different-size 60 min. Machines are inch-locked,
    # so changeovers are almost always same-size (formula change) = 40 min.
    BUILDING_CO_SAME_MIN = 40.0
    BUILDING_CO_DIFF_MIN = 60.0
    # ═══ E9 — DEAD KNOBS. COMPUTED, NEVER ENFORCED. ══════════════════════════════════
    # These three describe a "machine portfolio" limit — "max DISTINCT SKUs a building
    # machine runs", "a SKU runs on at most N machines". NO SUCH LIMIT IS ENFORCED
    # ANYWHERE. Verified by reference search over the whole file:
    #   BUILDING_SKU_MACHINE_MAX  — read by NOTHING. Zero references. Fully dead.
    #   BUILDING_PORTFOLIO_MAX / _FAST — read ONLY inside `_pcap()`, and _pcap() IS NEVER
    #   CALLED (see the deletion note at the placement loop). So they are dead too, one
    #   level down.
    # They are KEPT (not deleted) only because they are cheap literals that document the
    # plant's intent, and a future portfolio rule would want them. They are now LABELLED
    # so nobody reads them as constraints the schedule honours. THEY ARE NOT.
    # The rule that ACTUALLY runs is LPT-ordered cap-and-spill FFD packing — a machine's
    # SKU count is whatever falls out of it, and is not bounded by these numbers.
    BUILDING_PORTFOLIO_MAX      = 4     # DEAD (only read by the dead _pcap)
    BUILDING_PORTFOLIO_MAX_FAST = 6     # DEAD (only read by the dead _pcap)
    BUILDING_SKU_MACHINE_MAX    = 3     # DEAD — zero references anywhere in this file
    # Cap-and-spill: max build-WORK hours per machine before a SKU spills to an
    # eligible sibling. ~one month at ~90% (leaves room for changeovers + freshness
    # gaps so the layout still finishes inside the month).
    # ═══ CTP DEVIATION #12 — BUILDING_MONTH_CAP_H IS NOW TUNABLE (B) POLICY ═══════════
    # WAS: BUILDING_MONTH_CAP_H = 31 * 24 * 0.90  (= 669.6 h), a hard-coded literal.
    # CLASS: (B) POLICY, not a defect. The cap-and-spill rule EXECUTES CORRECTLY —
    #   it is first-fit-decreasing PACKING (see the comment at the placement loop), and
    #   FFD packs, it does not balance. We are NOT changing that rule.
    # WHAT CHANGES: only that the NUMBER is read from config instead of being frozen in
    #   the source. DEFAULT IS 669.6 = v6's exact behaviour, so an unchanged config
    #   reproduces v6 bit-for-bit.
    # WHY IT BUYS ANYTHING: measured, the cap at 669.6 h never binds (peak building load
    #   690.4 h — the :~1889 fallback used to ignore the cap; see CTP DEVIATION #13).
    #   With #13 repaired, LOWERING this cap is what actually levels the building line
    #   (cap=507 -> peak 653.9 h). That is a POLICY choice for the planner, and it now
    #   costs a config edit, not a code edit.
    # !! FLAG: lowering the cap WITHOUT CTP DEVIATION #13 makes the peak WORSE
    #    (690.4 -> 790.9 h), because the old fallback spilled straight past the cap.
    BUILDING_MONTH_CAP_H        = float(cfg.get("building_month_cap_h", 31 * 24 * 0.90))
    _bld_cap_src = ("config.yaml:building_month_cap_h"
                    if "building_month_cap_h" in cfg else "v6 default (31*24*0.90)")
    print(f"  BUILDING_MONTH_CAP_H: {BUILDING_MONTH_CAP_H:.1f} h  [from {_bld_cap_src}]"
          f"  (B) POLICY — cap-and-spill / FFD packing rule itself is UNCHANGED")
    # ═══ CTP DEVIATION #19 — BUILDING LOAD BALANCING (B) POLICY, DEFAULT OFF ══════════
    # THE PROBLEM, MEASURED (cap=480): building machine loads run 637.2 h (3403, 88.5%)
    #   down to 283.3 h (3409, 39.4%) — a 49-point spread on the ONLY unbalanced
    #   department. 3409 is legally eligible for 1,419.5 h of work and is given 283.4 h.
    #
    # WHY v6 DOES THIS. The cap-and-spill rule below is first-fit-decreasing PACKING and
    #   it executes CORRECTLY. The damage is done by the ORDER it packs in: item-groups
    #   are sorted LARGEST-FIRST (LPT). A machine choice is made ONCE PER SPILL, not per
    #   lot — after a group opens a machine, EVERY remaining lot of that group goes there
    #   until the cap. So the biggest, MOST FLEXIBLE groups grab the popular machines
    #   first, and a group with only 2 eligible machines that is placed LATER finds both
    #   of them already at the cap and has to blow through it.
    #   TRACED, cap=480: GT 1995 ROYL KIA (472.3 h, eligible on 3403/3404/3408) fills
    #   3403 to 472.3 h. GT 2476 SUP MM (587.6 h, eligible on 3408/3409/3410/3411) fills
    #   3408 to 480.7 h. THEN GT 1765 ROYL (318.7 h) arrives — it is eligible on
    #   {3403, 3408} AND NOTHING ELSE. Both are full, so all 318.7 h go through the
    #   over-cap fallback and push 3403/3408 to 637 h. Meanwhile 3409 sits at 283 h.
    #
    # THE FIX (classic most-constrained-first): order the groups by ASCENDING ELIGIBILITY
    #   (fewest machines first), not by descending size. The 2-machine group books its
    #   two machines while they are still empty; the flexible 4-machine groups then fill
    #   whatever is left — which is exactly what flexibility is FOR.
    #
    # !! THE CODE'S OWN COMMENT (:~1895) EXPLICITLY CONSIDERED AND REJECTED THIS:
    #    "Sorting ALL SKUs by ascending eligibility instead overloads machines that are
    #     the sole option for several 2-3-eligible SKUs".
    #    ON CTP DATA THAT NOTE IS WRONG, AND IT IS WRONG BY A LOT. Measured, all three
    #    knobs isolated (offline replay of this exact loop, validated against the
    #    pipeline to 0.1 h):
    #         v6 FFD cap=480 (shipping)          peak 637.2 h   530 changeovers (441.7 h)
    #         + ascending-eligibility order ONLY peak 521.9 h   511 changeovers (447.3 h)
    #         + fair-share cap ONLY              peak 653.9 h   511 changeovers  <- WORSE
    #         + load-first spill ONLY            peak 637.2 h   527 changeovers  <- inert
    #         ALL THREE (= balanced mode)        peak 515.9 h   473 changeovers (401.3 h)
    #    The LP lower bound on the peak (fractional, honouring every lot's real routing
    #    eligibility, max-flow binary search) is 506.7 h. Balanced mode lands 1.8% above
    #    the fractional optimum. Pure per-lot LPT is WORSE (533.2 h) and costs 782
    #    changeovers, because it shreds the campaigns.
    #
    # BALANCING IS NOT A TRADE-OFF HERE. It is better on BOTH axes: the peak drops 121 h
    #   AND changeovers drop by 57 (40 h of press-feeding time given back). That is the
    #   opposite of the usual "balance costs changeovers" law, and it is only true because
    #   the fix is an ORDERING change, not a splitting change: groups still run as long
    #   campaigns, they just book scarce machines before flexible ones do.
    #
    # STILL (B) POLICY, NOT (A). The shipped rule (LPT order) executes exactly as written;
    #   we are choosing a DIFFERENT rule. So it is flag-gated and DEFAULTS TO v6.
    BUILDING_BALANCE       = bool(cfg.get("balance_building_load", False))
    # Balanced mode caps each machine at its FAIR SHARE (total building work / machines)
    # times this slack, instead of at BUILDING_MONTH_CAP_H. slack 1.00 = perfect share.
    BUILDING_BALANCE_SLACK = float(cfg.get("building_balance_slack", 1.0))
    # ═══ CTP DEVIATION #28 — EQUAL LOAD-BALANCING FOR EVERY NON-BUILDING GROUP ════════
    # CLASS: (B) POLICY, DEFAULT OFF. USER-DIRECTED (2026-07-16): "no machine should sit
    #   idle if there is any little work, and material is available" + "max utilisation of
    #   ALL machines EQUALLY" across EVERY section, not just building.
    #
    # WHAT CHANGES: ONLY the machine-selection tie in place_one_lot_jit() (the per-lot
    #   dispatcher that serves mixers, cutters, calenders, tread/extruder, slitters, bead).
    #   The shipped picker scores by (expected_finish_ns, busy_ns): earliest-finish leads,
    #   load is only an exact-ns tie-break that almost never fires — so a machine with a
    #   marginally lower changeover keeps winning and saturates while a routing-eligible
    #   sibling idles. THE NEW RULE: among machines that can finish WITHIN a tolerance of
    #   the earliest achievable finish, run the LEAST-LOADED eligible machine.
    #
    # BOUNDED, NOT FORCED:
    #   * Eligibility is `machines` = the routing-legal set for THIS lot (inputs/routing.csv).
    #     A machine never receives a lot it is not routed for; "equal" is bounded by routing.
    #   * The tolerance caps how far a lot may be delayed from its best-possible finish just
    #     to balance (default 1 h). A machine whose changeover/queue pushes its finish more
    #     than TOL beyond the best is EXCLUDED — so campaigns and mother-roll runs (whose
    #     alternative costs hours, not minutes) are preserved; balancing only trades inside
    #     the tolerance band. This mirrors the load-balance rule place_campaign() already
    #     uses (see FIX 2 there) — the per-lot dispatcher simply never got it wired.
    #   * Material availability is unchanged: first_fit_jit still floors every start at
    #     _material_ready_ns (the quantity-summing kit gate), so a machine only balances
    #     work whose material is actually ready. ASAP is preserved (best-finish leads the
    #     tolerance band; a truly-idle machine's slot IS the earliest, so it gets picked).
    # DEFAULT FALSE = the shipped earliest-finish picker, byte-for-byte.
    BALANCE_ALL_MACHINES   = bool(cfg.get("balance_all_machines", False))
    # Tolerance band (hours): a machine is a balancing candidate only if its expected finish
    # is within this of the earliest achievable finish for the lot. 0 => off (earliest only).
    # (NS is defined later in main(); use the literal 1e9 ns/s so this stays in scope.)
    BALANCE_ALL_TOL_NS     = int(float(cfg.get("balance_all_tolerance_h", 1.0)) * 3600 * 1_000_000_000)
    # ═══ CTP DEVIATION #23 — TIME-PHASED BUILDING ASSIGNMENT. (C) ALGORITHM ═══════════
    # CLASS: (C) ALGORITHM — a NEW rule, not a repair of a broken one. v6's cap-and-spill
    #   FFD packer executes EXACTLY as written; it is simply the wrong instrument for this
    #   objective. Nothing here is a defect fix. DEFAULT FALSE = v6 bit-for-bit.
    #
    # WHAT IS WRONG WITH THE SHIPPED RULE (measured, not asserted):
    #   `machine_load` is a SINGLE SCALAR per machine (defaultdict(int), ~:1903). It has NO
    #   TIME DIMENSION. The only cap is a MONTH cap (MACHINE_CAP_NS). So the packer will
    #   happily give a machine 280 h of work whose green tyres are ALL needed by the press
    #   in WEEK 0 — against a 168 h/week physical capacity — as long as the machine's
    #   MONTH total stays under cap. MEASURED on the shipped config:
    #        machine  wk0    wk1    wk2    wk3   wk4  | TOTAL  month-util
    #        3407    280.1! 200.7!   0.0    0.0   0.0 | 480.8     67%
    #        3410     29.7  120.4  239.3!  88.2  29.0 | 506.7     70%
    #        3409     31.1   40.5   37.4  112.3  62.0 | 283.3     39%
    #   5 of 55 machine-weeks demand >168 h. 3407 is IMPOSSIBLE in wk0/wk1 at 67% month
    #   util, then idles. 3409 is at 39% while 3407 drowns. The month total hides all of it.
    #
    # WHY THE BUCKET IS A **DAY**, NOT A WEEK:
    #   MEASURED: every GT lot's build window (due - earliest) is AT MOST 22.5 h
    #   (median 20.6 h) — because green_tyre_max_age_h = 24 sets fresh_floor = due - 24 h.
    #   A green tyre therefore CANNOT be built a week early; it would expire waiting to
    #   cure. Work CANNOT be moved ACROSS WEEKS AT ALL. It can only be moved ACROSS
    #   MACHINES INSIDE THE SAME ~24 h WINDOW. A week bucket would be too coarse to
    #   express the only degree of freedom that actually exists.
    #
    # THE RULE: `bucket_load[(machine, day)]` — machine_load gains a TIME AXIS. A lot is
    #   charged to the DAY ITS PRESS NEEDS IT (bucket of `due`). A machine is refused if
    #   that day is already full; the lot spills to an ELIGIBLE machine with room IN THAT
    #   SAME DAY. Groups are processed MOST-CONSTRAINED-FIRST so sole-eligible items claim
    #   their machine-days BEFORE flexible work can bury them (this is the whole game: the
    #   item 8 presses starve on, GT 1543 XPC1, is SOLE-ELIGIBLE on 3410 and cannot move —
    #   what must move is the flexible work piled on top of it).
    BUILDING_TIME_PHASED   = bool(cfg.get("building_time_phased", False))
    # Bucket width in hours. 24 = one day = the GT shelf life. Do not raise above 24
    # without re-measuring the build window: a bucket wider than the aging window lets
    # the packer pretend it can time-shift work that physically cannot move.
    BUILDING_TP_BUCKET_H   = float(cfg.get("building_tp_bucket_h", 24.0))
    # Per-machine-day capacity = bucket_h * this. 1.0 = the machine's real 24 h day.
    # >1.0 deliberately over-books the bucket (the layout absorbs it by running late);
    # <1.0 reserves headroom for the layout's changeovers. TUNED BELOW.
    BUILDING_TP_SLACK      = float(cfg.get("building_tp_slack", 1.0))
    # ═══ CTP DEVIATION #31 — PER-CODE TAKT SPREAD (min TBMs by DAILY curing pull) ═════
    # The balanced cap-and-spill splits a CODE across machines only when its MONTH-TOTAL
    # build-hours exceed the fair-share cap. A high-runner at 300-470 h stays on ONE TBM
    # and becomes the makespan long pole: MEASURED, our curing tail is 8 d (90->100%) vs
    # the plant's 3 d, and 27 of 30 multi-eligible codes run on a single machine
    # (mean 1.13) while the plant runs those same codes on 2-3 TBMs. This gives each
    # long-pole code a MINIMUM machine count = ceil(daily_ns / (per-machine-day *
    # target_util)), pre-opens that many of its least-loaded eligible machines, and
    # round-robins its lots across them (even per-machine share). DEFAULT OFF => a true
    # A/B: with the flag off, NOTHING below this changes (every edit is gated on the
    # per-group cap being set). Only codes above building_takt_min_hours ever move.
    BUILDING_TAKT_SPREAD      = bool(cfg.get("building_takt_spread", False))
    BUILDING_TAKT_TARGET_UTIL = float(cfg.get("building_takt_target_util", 0.65))
    BUILDING_TAKT_MIN_HOURS   = float(cfg.get("building_takt_min_hours", 120.0))
    # per-machine effective build h/day, single-sourced to the month cap (648/30 = 21.6)
    BUILDING_TAKT_PMDH_H      = float(cfg.get("building_takt_per_machine_daily_h",
                                             BUILDING_MONTH_CAP_H / 30.0))
    # Inch-lock: when True a machine is pinned to ONE tyre size (fewer changeovers
    # but strands capacity — an idle machine locked to size A can't take size-B work).
    # When False, machines run any routing-eligible size (better balance / absorbs the
    # tail, but more different-size changeovers, which cost more time).
    BUILDING_INCH_LOCK          = True
    # === CTB (CLEAR-TO-BUILD) QUANTITY GATE ===
    # When True, a GT/carcass building lot cannot start until the CUMULATIVE
    # supply of EACH input component (opening WIP + component production finished
    # so far) covers the CUMULATIVE consumption through that lot. Consumption rates
    # come from the Phase-1 demand explosion.
    #
    # ═══ CTP DEVIATION #20 — WHY THIS IS *NOT* THE FIX FOR E1, AND STAYS OFF ══════════
    # I was asked to check whether this gate already does the right thing and could simply
    # be wired to the DAG predecessor constraint. IT CANNOT. It does the right KIND of
    # thing (cumulative supply vs cumulative demand) but it is not a substitute, for five
    # reasons, all verified in the code below:
    #   1. IT IS OFF, and not even config-gated — a hard-coded `False`.
    #   2. IT IS BUILDING-ONLY. It sweeps `blds`. It never gates a mixer, a calender, a
    #      cutter, an extruder or an FRC campaign. The E1 defect is in the PREDECESSOR
    #      rule, which every one of those paths uses.
    #   3. IT EXCLUDES ANY COMPONENT THAT IS ITSELF A BUILDING PRODUCT (`if ip in
    #      _bld_codes: continue`) — i.e. carcass. On a two-stage plant that is the single
    #      largest component of a green tyre, and it is the one the gate skips. (On CTP it
    #      happens to be moot: there are ZERO Carcass lots. But the hole is real.)
    #   4. IT IS A PRE-PASS over an ASSUMED sweep order (due_day, code, due, lot_id), not
    #      the chronology the layout actually produces. Its own comment used to admit this:
    #      "a single-pass per-lot floor can't model the parallel machine-interleaved
    #      consumption chronology, so stockouts persist."
    #   5. IT NEVER TOUCHES THE PREDECESSOR CONSTRAINT. It is an EXTRA floor bolted on top
    #      of a broken one — which is exactly why it was invented, and exactly why it could
    #      never work: it was papering over the hole instead of closing it.
    # The hole is now closed AT THE PREDECESSOR RULE (_material_ready_ns, CTP #20), which
    # is where it belonged. This flag is left at v6's default (False) and UNCHANGED — it is
    # now redundant for the quantity question and its carcass exclusion makes it actively
    # misleading. Turning it on is a (B) POLICY choice; it is not needed for correctness.
    BUILDING_CTB_GATE           = False
    # === HEIJUNKA DAILY-LEVELING BUILD LAYOUT (option b) ===
    # Instead of building each SKU as a long campaign ASAP (which front-loads and
    # collapses daily SKU diversity to ~23, dipping output on trough days), level
    # the build to the daily curing takt: each day, round-robin a broad mix of the
    # ripe SKUs across machines so ~35-40 distinct SKUs build every day and output
    # holds ~the curing rate. LEAD = how many days ahead of curing a lot may build
    # (bounded by freshness). MINI = max consecutive lots of one code per machine
    # visit (controls changeover count: ~total_lots/MINI changeovers).
    # === CTP DEVIATION #18 - BUILDING_LEVEL IS NOW TUNABLE (B) POLICY ==================
    # v6 ships this OFF with the note "naive takt-cap hurt throughput". But its LEAD is
    # 3.0 DAYS - "= GT freshness" - which is BTP's number. CTP's green tyre lives 24 h.
    # So on CTP the mode has never been tested with a lead that matches the shelf life.
    # The presses starve MID-MONTH (median 49 h, worst 169 h) because building front-loads
    # instead of tracking takt. Levelling to takt is exactly the drum-buffer-rope answer.
    # Default stays FALSE = v6's behaviour.
    BUILDING_LEVEL              = bool(cfg.get("building_level", False))
    BUILDING_LEVEL_LEAD_DAYS    = float(cfg.get("building_level_lead_days", 3.0))
    # ═══ IDEA 1 — EARLIEST-FINISH-TIME BUILDING SPREAD. (C) ALGORITHM, DEFAULT OFF ═════
    # THE DEFECT IT TARGETS (measured on the MOQ-5 state, makespan 40.12 d): building is
    # the binding stage (last green tyre finishes day 36.37) NOT because the machines are
    # short of capacity, but because the assignment CONCENTRATES. The cap-and-spill FFD
    # packer below gives a group ONE machine and keeps feeding it until that machine hits
    # the fair-share cap (~545 h) — it only ever opens a second machine when the first is
    # FULL FOR THE MONTH. MEASURED: 30 of 35 green-tyre codes run on FEWER machines than
    # their routing allows (most on 1-2, where routing permits 4-8). Capacity that the
    # routing says is eligible is simply never opened, so a code's lots queue behind each
    # other on one machine instead of running in parallel on four.
    #
    # THE RULE WHEN ON: per-lot EARLIEST-FINISH-TIME selection. For each building lot, over
    # its ELIGIBLE machines only, compute the finish it would actually get —
    #     start  = max(machine_cursor + changeover, kit_ready/fresh_floor, material_ready)
    #     finish = start + per-machine duration
    # — and take the machine with the earliest finish. A fresh parallel machine finishes a
    # lot sooner than queueing behind the same code on a busy one, so a code FANS OUT across
    # its allowable machines exactly as far as its volume needs and no further. This is
    # classic list-scheduling (LPT/ECT), not a takt cap: it never withholds work from a
    # machine, so it cannot starve the press the way BUILDING_LEVEL did (that mode capped
    # daily qty and blew the makespan to 64 d — see BUILDING_LEVEL above).
    #
    # WHAT IS PRESERVED (this is why it is safe):
    #   * ROUTING ELIGIBILITY — candidates are info[lid]["machines"] (= lot_machines), the
    #     same routing-eligible set the packer uses. A non-eligible machine is never seen.
    #   * MATERIAL-READY / FRESHNESS — `earliest` (kit_ready, fresh_floor, schedule-open)
    #     and `material_ready` are floors INSIDE the finish estimate, and the per-machine
    #     layout below re-applies them verbatim. A lot is never built early or un-kitted.
    #   * NO DOUBLE-BOOKING — this pass only decides WHICH machine (machine_lots). The
    #     per-machine layout below is still the single authority for start/finish and still
    #     walks one cursor per machine.
    #   * CAMPAIGN COHERENCE — see the sticky band below.
    BUILDING_SPREAD_EFT = bool(cfg.get("building_spread_eft", False))
    # STICKY BAND (minutes). EFT on its own would hand a lot to a machine that finishes one
    # NANOSECOND earlier even if that costs a changeover — which would shred campaigns into
    # thousands of setups. So: among machines whose finish is within this band of the best,
    # prefer the machine ALREADY RUNNING THIS CODE (no changeover), then the least loaded.
    # Default = the same-size changeover (40 min): never switch machines for a gain smaller
    # than the changeover the switch costs. That is the SMED-side rule, not a fudge.
    BUILDING_SPREAD_EFT_STICKY_MIN = float(cfg.get("building_spread_eft_sticky_min",
                                                   BUILDING_CO_SAME_MIN))
    # ═══ LEVER 1 — S2b: JIT TERMINAL GREEN-TYRE BUILDING (config flag, default OFF) ═════
    # WAS (and still is, when OFF): place_building_campaigns floors a terminal GT lot at
    #   fresh_floor = due - (gmax - gmin)  (= LFT - 24 h at gmax=24, gmin=0).
    # So a GT is built at the EARLIEST its shelf life allows and then WAITS ~24 h - dur for
    # the press -> over-age. This flag targets its LFT instead (finish = due = P1 - min_aging,
    # as-late-as-possible in the cure-by band) so it lands FRESH at the press. Terminal
    # GREEN TYRES ONLY -- carcass/feeders untouched (this is the narrow lever, NOT the
    # tested-dead building_level / feeder_jit). Machine capacity still respected: the forward
    # packer starts at max(cursor, jit_floor), so a capacity-bound machine simply packs later,
    # it never double-books. PROVE it lowers over-age without raising phantom/makespan.
    GT_JIT_BUILD               = bool(cfg.get("green_tyre_jit_build", False))
    print(f"  GT-JIT-BUILD (lever 1, terminal GT only): "
          f"{'ON -- target finish = LFT (fresh at press)' if GT_JIT_BUILD else 'OFF -- v6 fresh_floor (build early, wait)'}")
    # ═══ CTP DEVIATION #32 — LATENESS-GATED TAIL SPILL (config, DEFAULT OFF) ═══════════
    # See the full write-up at the repair itself (search "CTP DEVIATION #32"). Every knob
    # here only ever WIDENS or NARROWS the three gates; with building_tail_spill: false
    # the repair block does not execute and phase5 is bit-identical to the OFF run.
    BUILDING_TAIL_SPILL            = bool(cfg.get("building_tail_spill", False))
    # RULE A (machine-end shortening). MEASURED STRICTLY DOMINATED by rule B on this plan:
    #   A only 34.143 d / 47.49 % >24 h | A+B 33.375 d / 46.85 % | B only 33.349 d / 46.48 %
    # Kept, defaulted OFF. Turn it on only if a future mix has a machine-end tail that
    # rule B's idle-absorption cannot reach.
    BUILDING_TAIL_SPILL_RULE_A     = bool(cfg.get("building_tail_spill_tail_rule", False))
    # how many improvement passes over the candidate list (stops early at 0 moves)
    BUILDING_TAIL_SPILL_ROUNDS     = int(cfg.get("building_tail_spill_rounds", 12))
    # GATE 1 — only lots finishing within this many hours of the BUILDING makespan
    BUILDING_TAIL_SPILL_TAIL_H     = float(cfg.get("building_tail_spill_tail_h", 120.0))
    # GATE 2 — only lots kept waiting by the MACHINE (start - ready) longer than this.
    #          A lot waiting on MATERIAL has slip 0 and is never touched.
    BUILDING_TAIL_SPILL_TOL_H      = float(cfg.get("building_tail_spill_tol_h", 8.0))
    # GATE 3 — a move must cut max(end(donor), end(receiver)) by at least this
    BUILDING_TAIL_SPILL_MIN_GAIN_H = float(cfg.get("building_tail_spill_min_gain_h", 1.0))
    # AGING GUARD — non-moved lots keep their pre-repair start as a FLOOR, so the repair
    # can never pull another green tyre earlier (= older at the press). Keep ON unless
    # A/B-ing the guard itself.
    BUILDING_TAIL_SPILL_PIN        = bool(cfg.get("building_tail_spill_pin_starts", True))
    # JIT LANDING — after a move is accepted, binary-search the LATEST start the receiver
    # can give the moved lot while the pair-end stays at the value that justified the
    # move. Without this the receiver builds it as early as it can and the tyre ages
    # waiting for its press (MEASURED +0.75 pp over-24 h). Keep ON.
    BUILDING_TAIL_SPILL_JIT        = bool(cfg.get("building_tail_spill_jit_landing", True))
    # RULE B — idle-absorption pull-forward. Rule A can only shorten a machine's END, so a
    # MID-QUEUE lot that starves a press mid-horizon is invisible to it. Rule B moves such
    # a lot into GENUINE IDLE on an eligible machine: nothing else may be delayed, the
    # building makespan may not grow, and the lot lands as LATE as the gain allows.
    BUILDING_TAIL_SPILL_PULL       = bool(cfg.get("building_tail_spill_pull_forward", False))
    BUILDING_TAIL_SPILL_PULL_TOL_H = float(cfg.get("building_tail_spill_pull_tol_h", 48.0))
    BUILDING_TAIL_SPILL_PULL_GAIN_H = float(cfg.get("building_tail_spill_pull_min_gain_h",
                                                    12.0))
    BUILDING_TAIL_SPILL_PULL_HARM_H = float(cfg.get("building_tail_spill_pull_harm_h", 0.0))
    # AGING CAP (hours): the most a rule-B move may pull a green tyre forward. 0 = no cap.
    BUILDING_TAIL_SPILL_PULL_MAX_H = float(cfg.get("building_tail_spill_pull_max_h", 40.0))
    # THE ROPE. Use phase2's last_need (the LAST curing block that consumes the lot) as the
    # drum signal instead of the phase4 LFT. A moved lot may never be built earlier than
    # last_need - dur, i.e. it can be pulled up to the press's own need time and NO FURTHER.
    BUILDING_TAIL_SPILL_USE_NEED   = bool(cfg.get("building_tail_spill_use_last_need", True))
    # ═══ CTP DEVIATION #36 (G1/G2/G3) — QUEUE-WAIT TRIGGER, NEED-BOUNDED DISPLACEMENT,
    #     AND THE HEAD-LOT CONTENTION FIX.  ALL THREE DEFAULT OFF.
    # Read the full write-up at the code (search "CTP DEVIATION #36"). With
    #   building_tail_spill_pull_queue_h : 0
    #   building_tail_spill_pull_drum_h  : 0
    #   building_tail_spill_pull_harm_mode: strict
    #   building_tail_spill_head_fix     : false
    # every expression below collapses to the CTP #32 constant it replaced and the run is
    # bit-identical (VERIFIED — see _bal_exp/REVERT.txt).
    #
    # G1 — the two rule-B admission gates, previously BOTH hard-wired to _pull_tol_h (48 h).
    #      0 (or absent) => fall back to _pull_tol_h, i.e. CTP #32 behaviour exactly.
    #      queue_h : min (start - max(earliest, material_ready)) to be admitted
    #      drum_h  : min (finish - last_need)  to be admitted
    _q = float(cfg.get("building_tail_spill_pull_queue_h", 0.0) or 0.0)
    _d = float(cfg.get("building_tail_spill_pull_drum_h", 0.0) or 0.0)
    BUILDING_TAIL_SPILL_PULL_QUEUE_H = _q if _q > 0 else BUILDING_TAIL_SPILL_PULL_TOL_H
    BUILDING_TAIL_SPILL_PULL_DRUM_H  = _d if _d > 0 else BUILDING_TAIL_SPILL_PULL_TOL_H
    # G2 — how much a lot ALREADY ON the receiver may be displaced by the insert.
    #      "strict"    : not at all (CTP #32; harm tolerance = _pull_harm_h, default 0)
    #      "last_need" : it may finish later, but ONLY inside its own last_need. A lot with
    #                    no last_need is still strict. THIS IS STILL A ROPE — the displaced
    #                    tyre is never pushed past the moment its own press needs it.
    BUILDING_TAIL_SPILL_HARM_MODE  = str(cfg.get("building_tail_spill_pull_harm_mode",
                                                 "strict")).strip().lower()
    # G3 — HEAD-LOT CONTENTION FIX. Rule B's gates are sized for the tail (48 h of queue).
    #      The lot that sets the critical press's START waits only ~14 h — invisible to
    #      rule B, and it is 100 % contention (its machine is busy building another code
    #      while 5 other routing-eligible TBMs are free).
    BUILDING_TAIL_SPILL_HEAD_FIX   = bool(cfg.get("building_tail_spill_head_fix", False))
    BUILDING_TAIL_SPILL_HEAD_WIN_H = float(cfg.get("building_tail_spill_head_window_h", 168.0))
    BUILDING_TAIL_SPILL_HEAD_TOL_H = float(cfg.get("building_tail_spill_head_tol_h", 4.0))
    BUILDING_TAIL_SPILL_HEAD_GAIN_H = float(cfg.get("building_tail_spill_head_min_gain_h", 2.0))
    # RULE D (G1+G2 combined, applied WITHIN a machine) — SAME-MACHINE CAMPAIGN
    # RESEQUENCE. Rules A/B/C can only REASSIGN a lot to another TBM; measured, that is
    # the wrong move for this plan (441 of 584 head probes refused at "no gain" because
    # every TBM walks the same global due_day/item-code order in lockstep). Rule D moves a
    # whole item-code CAMPAIGN earlier IN ITS OWN QUEUE — changeover-neutral by
    # construction — when it is past its own last_need and the campaigns it displaces
    # still make theirs.
    BUILDING_TAIL_SPILL_RESEQ      = bool(cfg.get("building_tail_spill_reseq", False))
    BUILDING_TAIL_SPILL_RESEQ_TOL_H = float(cfg.get("building_tail_spill_reseq_tol_h", 8.0))
    BUILDING_TAIL_SPILL_RESEQ_GAIN_H = float(cfg.get("building_tail_spill_reseq_gain_h", 2.0))
    # ═══ CTP DEVIATION #40 (T2) — RULE E: SAME-MACHINE SINGLE-LOT PULL-FORWARD ════════
    # DEFAULT OFF. Rules A/B/C are ALL machine REASSIGNMENTS (`if _m2 == _m: continue`,
    # :4032 / :4168), so a lot can never be pulled earlier ON ITS OWN MACHINE. Rule D can,
    # but only as a WHOLE ITEM-CODE CAMPAIGN. Rule E is the missing single-lot case.
    BUILDING_TAIL_SPILL_SM         = bool(cfg.get("building_tail_spill_same_machine", False))
    BUILDING_TAIL_SPILL_SM_QUEUE_H = float(cfg.get("building_tail_spill_same_machine_queue_h", 8.0))
    BUILDING_TAIL_SPILL_SM_DRUM_H  = float(cfg.get("building_tail_spill_same_machine_drum_h", 0.0))
    BUILDING_TAIL_SPILL_SM_GAIN_H  = float(cfg.get("building_tail_spill_same_machine_gain_h", 2.0))
    BUILDING_TAIL_SPILL_SM_CO_GUARD = bool(cfg.get("building_tail_spill_same_machine_co_guard", True))
    # AUDIT — write the per-candidate / per-target rejection census. Pure instrumentation.
    BUILDING_TAIL_SPILL_DEBUG      = bool(cfg.get("building_tail_spill_debug", False))
    # ═══ CTP DEVIATION #35 (F2) — ORDER THE MACHINE QUEUE BY NEED, NOT BY NAME ═════════
    # THE DEFECT. Every per-machine building queue is laid out by
    #     (due_day, ITEM CODE, due, lot_id)
    # (the layout at the bottom of place_building_campaigns, its exact replica inside
    # _spill_sim, and the CTB pre-pass). `due_day` is a WHOLE DAY, so inside one day the
    # ITEM CODE — a string — is the priority. "GT 1402 ..." runs before "GT 1925 ..."
    # because 4 < 9, not because any press needs it first. The same class of defect is
    # visible upstream on extruder 1001, where 20 sidewall codes are made in ASCII order
    # ("SWRS100" < "SWRS43") before the code the first green tyre is waiting for.
    # ON  -> (due_day, LAST_NEED, due, lot_id): last_need is phase2's LAST curing block
    #        that consumes the lot — the real drum signal, and the same one CTP #32's rope
    #        uses. MEASURED over the 911 GT lots: median(last_need - phase4 LFT) = +41.5 h,
    #        and the phase4 LFT is inert as a gate (872 of 911 lots have earliest > due).
    #        A lot with NO last_need sorts LAST (never first) and keeps a code tie-break.
    # COST TO WATCH: `code` was also the CAMPAIGN key — consecutive lots of one code form
    # one campaign on the machine. Ordering by need can interleave codes and ADD building
    # changeovers. That number is reported on every run.
    # TWO MODES, because the literal replacement has a MEASURED cost:
    #   "lot"    — the literal fix: (due_day, last_need, due, code, lot_id). Correct on
    #              priority, but `code` was ALSO the campaign key, so codes interleave:
    #              MEASURED 371 -> 763 building changeovers (+393, ~+262 h) and makespan
    #              33.315 -> 34.261 d. GT over-24h 44.83 -> 32.12 % (much better),
    #              COMPONENT FEFO ex-cap-strip 57.18 -> 66.62 % (WORSE).
    #   "family" — family/group scheduling, which is what the alphabet was accidentally
    #              doing: order the CODES inside a due-day by that code's EARLIEST
    #              last_need, then keep every lot of a code together as one campaign and
    #              order WITHIN the code by last_need. Same drum signal, campaigns intact.
    _obn = cfg.get("building_order_by_need", False)
    if isinstance(_obn, str):
        BUILDING_ORDER_BY_NEED = _obn.strip().lower()
        if BUILDING_ORDER_BY_NEED in ("false", "off", "no", ""):
            BUILDING_ORDER_BY_NEED = ""
        elif BUILDING_ORDER_BY_NEED not in ("lot", "family"):
            print(f"  [WARN] building_order_by_need={_obn!r} unknown -> OFF (v6)")
            BUILDING_ORDER_BY_NEED = ""
    else:
        BUILDING_ORDER_BY_NEED = "lot" if bool(_obn) else ""
    print("  F2 BUILDING QUEUE ORDER: " + {
        "": "(due_day, ITEM CODE, due, lot_id) — v6, the alphabet",
        "lot": "(due_day, LAST_NEED, due, code, lot_id) — drum signal, campaigns NOT preserved",
        "family": "(due_day, code's earliest LAST_NEED, code, last_need, due, lot_id) — "
                  "drum signal at the FAMILY level, campaigns preserved",
    }[BUILDING_ORDER_BY_NEED])
    if BUILDING_TAIL_SPILL:
        print(f"  BUILDING TAIL-SPILL (CTP #32): ENABLED — rounds "
              f"{BUILDING_TAIL_SPILL_ROUNDS}, tail {BUILDING_TAIL_SPILL_TAIL_H:.0f} h, "
              f"lateness-tol {BUILDING_TAIL_SPILL_TOL_H:.0f} h, min-gain "
              f"{BUILDING_TAIL_SPILL_MIN_GAIN_H:.1f} h, pin-starts "
              f"{'ON' if BUILDING_TAIL_SPILL_PIN else 'OFF'}")
    BUILDING_LEVEL_MINI_CAMPAIGN = 8    # ~15.9k lots / 8 ≈ ~2000 changeovers
    BUILDING_LEVEL_TAKT_HEADROOM = 1.12 # daily qty cap = takt × this (level, don't front-load)
    # E9 — DEAD. Both were read ONLY by _pcap(), which was never called and is now deleted.
    # Nothing in the executed schedule treats any machine as "fast". (Do not confuse these
    # with `machine_cycle_sec`, which IS live and drives per-machine building durations.)
    BUILDING_FAST_CYCLE_SEC     = 70.0  # DEAD (was: "machines at/below this are fast")
    BUILDING_FAST_MACHINES      = {"6001", "6002", "6003", "6004"}  # DEAD
    _sku_inch = {}
    try:
        _im = pd.read_csv(INPUTS / "sku_inch_map.csv")
        _sku_inch = dict(zip(_im["sku_code"].astype(str).str.strip(),
                             _im["inch"].astype(str).str.strip()))
        print(f"  BUILDING INCH MAP: {len(_sku_inch):,} SKUs")
    except Exception as _e:
        print(f"  BUILDING INCH MAP load skipped ({_e})")
    # Robust inch resolver: FG-code digits 9-10 first (covers ~all SKUs), then
    # the sku_inch_map, then a tiny manual override for lettered codes. This makes
    # the building inch complete (not just map-covered) so the machine-level
    # inch-lock (Step 2) applies to every lot.
    _INCH_VALID = {str(n) for n in range(10, 20)}
    _INCH_OVR = {"ET1658014UXRSMTTL": "14"}
    def _resolve_inch(sku):
        s = str(sku).strip()
        d = s[8:10] if len(s) >= 10 else ""
        if d.isdigit() and d in _INCH_VALID:
            return d
        return _sku_inch.get(s) or _INCH_OVR.get(s)

    # === STEP 2: MACHINE-LEVEL INCH-LOCK from the plant allowed-sets ===
    # Each machine may run only the inches in its allowed-set (inputs/machine_inch.csv),
    # and we PREFER to load it with its dominant inch. When on, phase-5 locks each
    # building machine to an inch from ITS OWN allowed-set (instead of first-come),
    # so e.g. a 16" machine never gets pinned to 13". Toggle with JK_INCH_AWARE=0.
    BUILDING_MACHINE_INCH = os.environ.get("JK_INCH_AWARE", "1") not in ("0", "false", "False")
    # SOFT INCH (default ON): inch is a preference with a size-change penalty, NOT a
    # hard wall. No machine is permanently inch-fixed — the constraint is released
    # for off-pattern work only when better-matched machines are full (e.g. an idle
    # 6909 takes overflow at a penalty). Set JK_SOFT_INCH=0 to restore the hard gate.
    BUILDING_SOFT_INCH = os.environ.get("JK_SOFT_INCH", "1") not in ("0", "false", "False")
    MACHINE_ALLOWED, MACHINE_DOM = {}, {}
    if BUILDING_MACHINE_INCH:
        try:
            _mi = pd.read_csv(INPUTS / "machine_inch.csv", dtype=str)
            for _, _r in _mi.iterrows():
                _m = str(_r["machine"]).strip()
                _al = {x.strip() for x in str(_r.get("allowed_inches", "")).split(",") if x.strip()}
                if _m and _al:
                    MACHINE_ALLOWED[_m] = _al
                    MACHINE_DOM[_m] = str(_r.get("dominant_inch", "")).strip()
            print(f"  STEP2 MACHINE INCH-LOCK: {len(MACHINE_ALLOWED)} machines with allowed-sets")
        except Exception as _e:
            print(f"  STEP2 MACHINE INCH-LOCK load skipped ({_e})")
    _lot_sku1 = {}
    try:
        _sk = pd.read_csv(OUTPUTS / _suff("phase2_lot_skus.csv"))
        for _l, _s in zip(_sk["lot_id"], _sk["skus_set"].astype(str)):
            _toks = [t.strip() for t in str(_s).split(",")
                     if t.strip() and t.strip().lower() != "nan"]
            if _toks:
                _lot_sku1[_l] = _toks[0]
        print(f"  BUILDING lot->SKU: {len(_lot_sku1):,} lots")
    except Exception as _e:
        print(f"  BUILDING lot->SKU load skipped ({_e})")
    # Per-machine changeover from Master_Building_ChangeoverTime
    # (machine_code -> (same_size_min, different_size_min)).
    _bld_co = {}
    try:
        _co = pd.read_csv(INPUTS / "building_changeover.csv")
        for _m, _sm, _dm in zip(_co["machine_code"].astype(str).str.strip(),
                                pd.to_numeric(_co["same_min"], errors="coerce"),
                                pd.to_numeric(_co["diff_min"], errors="coerce")):
            if pd.notna(_sm) and pd.notna(_dm):
                _bld_co[_m] = (float(_sm), float(_dm))
        print(f"  BUILDING CHANGEOVER MAP: {len(_bld_co)} machines")
    except Exception as _e:
        print(f"  BUILDING CHANGEOVER MAP load skipped ({_e})")
    # SKU -> current-running machine(s) (tbs2): the REAL machine each SKU runs on.
    # Used as the building machine set so machines stay single-inch (their real
    # SKUs) and SKU portfolio stays ~1 (up to the 2-3 listed). Overrides routing
    # eligibility for covered SKUs; uncovered SKUs fall back to routing.
    _sku_machines = {}
    try:
        import ast as _ast
        _tm = pd.read_csv(INPUTS / "tbs2_current_running_machine.csv")
        for _rc, _mn in zip(_tm["RecipeCode"].astype(str).str.strip(), _tm["machine_numbers"]):
            try:
                _ml = [str(x).strip() for x in _ast.literal_eval(str(_mn))]
            except Exception:
                _ml = []
            if _ml:
                _sku_machines[_rc] = _ml
        print(f"  BUILDING SKU->MACHINE (tbs2): {len(_sku_machines)} SKUs")
    except Exception as _e:
        print(f"  BUILDING SKU->MACHINE (tbs2) load skipped ({_e})")
    # CUTTER MOTHER-ROLL STATE — per-cutter tracking for parent batching.
    # When a cut variant comes in, dispatcher checks if parent is already loaded
    # AND mother roll has enough MM left → 3 min cut re-set. Else 15 min swap.
    cutter_loaded_parent = defaultdict(str)
    cutter_loaded_lot_id = defaultdict(str)
    cutter_roll_remaining_mm = defaultdict(lambda: 0.0)
    # Load BOM mm-per-cut map ONCE — for cutter-family lots only.
    try:
        _bom_df = _db_or_csv("bom", cfg)
        mm_per_cut_map = build_mm_per_cut_map(_bom_df)
        print(f"  CUTTER MODEL: mm-per-cut entries: {len(mm_per_cut_map):,}")
    except Exception as _e:
        print(f"  CUTTER MODEL: BOM load failed ({_e}) - defaulting to flat 3/15 min")
        mm_per_cut_map = {}

    DEFAULT_MIN_AGE_H = pre_curing_buffer_h
    DEFAULT_MAX_AGE_H = 365 * 24.0
    NS_PER_H = int(3600 * 1e9)
    NS = 1e9
    item_min_age_ns = {}
    item_max_age_ns = {}
    # Standardized building-product aging (policy override, applied by ITEMTYPE):
    #   GREEN TYRES -> min 0 d, max 3 d (72 h)   CARCASS -> min 0 d, max 1 d (24 h)
    # A GT/carcass cannot be built earlier than (curing - max_age) without over-aging,
    # and needs >= min_age rest before it is consumed/cured. These caps override any
    # per-SKU aging_master row for building products; all other items use aging_map.
    #
    # ═══ CTP DEVIATION #14 — GREEN-TYRE SHELF LIFE: THREE NUMBERS IN ONE FILE ═════════
    # WAS: AGE_OVERRIDE_H = {"GREEN TYRES": (0.0, 72.0), ...} — a frozen 72.0.
    # THE CONTRADICTION, all inside this one file plus its sibling phase:
    #     72 h   here (the number the code ACTUALLY executes)
    #     48 h   the TARGET_WIP_HOURS comment at the top of this file ("Green Tyres: 48h
    #            max_aging - shorter buffer")                       <- documentation lie
    #     24 h   the PLANT's own number — and what phase2 ALREADY believes.
    # So phase2 and phase5 DISAGREE TODAY about how long a green tyre may sit.
    # CLASS: (B) POLICY. The code executes 72 h correctly; we want a different number.
    #   So the DEFAULT STAYS 72.0 (= v6's behaviour) and the value is read from config.
    #   The config agent sets green_tyre_max_age_h: 24. When it does, phase2 and phase5
    #   finally agree — and a GT can no longer be built 3 days before its cure.
    # WHAT IT BUYS: at 24 h the GT fresh_floor in place_building_campaigns tightens from
    #   (due - 72 h) to (due - 24 h) — building is pulled toward its curing slot instead
    #   of being allowed to sit on the rack for three days.
    # USER-DIRECTED (2026-07-16, CLEAN PIPELINE): the green-tyre shelf life MUST come from
    # the plant master (inputs/aging_master.csv), NOT from a config knob. v6 hard-coded 72 h
    # and a config override later injected 24 h -- both are EXTERNAL to the source. On CTP all
    # 224 GT codes carry a real aging_master row (MinAging 0.25 h, MaxAging 24 h), so we read
    # it. _ages_ns() below prefers each GT item's OWN aging_master row (per-item source) and
    # falls back to this itemtype scalar only if a GT item has no row (immortal-item guard,
    # CTP #10). The scalar itself is DERIVED FROM the master (the GT rows), never from config.
    _gt_src_ages = [aging_map[_ic] for _ic, _it in item_itype.items()
                    if _it == "GREEN TYRES" and _ic in aging_map]
    if _gt_src_ages:
        _gt_min_age_h = min(a[0] for a in _gt_src_ages)   # source min (0.25 h)
        _gt_max_age_h = min(a[1] for a in _gt_src_ages)   # source max (24 h) -- tightest is honest
        _gt_src = f"inputs/aging_master.csv ({len(_gt_src_ages)} GREEN TYRES rows)"
    else:
        _gt_min_age_h, _gt_max_age_h = 0.0, 24.0
        _gt_src = "plant fallback 24 h (no GREEN TYRES rows in aging_master)"
    AGE_OVERRIDE_H = {"GREEN TYRES": (_gt_min_age_h, _gt_max_age_h), "CARCASS": (0.0, 24.0)}
    print(f"\n  !! GREEN-TYRE SHELF LIFE (from SOURCE): min {_gt_min_age_h:.2f} h / max "
          f"{_gt_max_age_h:.1f} h  [from {_gt_src}]")
    print(f"     per-item aging_master rows drive each GT lot; this scalar is only the "
          f"no-row fallback. phase2/phase6 read the same master. NO config override.")
    print(f"     CARCASS max age: 24.0 h (unchanged; CTP has no carcass lots)")

    # ═══ CTP DEVIATION — TRANSFER TIME FROM THE ROUTING ════════════════════════════
    # v6 never reads `transfer_time_min`. The user instructed that it always be used.
    # It is a MANDATORY LAG between a producer finishing and its consumer starting —
    # the same shape as min_aging — so it is ADDED TO MIN here, at the single funnel
    # every placement gap goes through (the p_min_ns checks at ~:997, ~:1243, ~:1300).
    #
    # It is applied AFTER AGE_OVERRIDE_H, so a green tyre with min=0 and a 90-min
    # building->curing transfer correctly gets a 1.5h floor before it may be cured.
    #
    # NOT added to max: max_aging is a shelf-life CEILING, not a lag. Adding transfer
    # there would EXTEND shelf life, which is the opposite of the truth.
    # ═══ CTP DEVIATION #3 — load the plant changeover matrix (item-pair keyed) ═══
    _co_p = INPUTS / "changeover_lookup.csv"
    if _co_p.exists():
        MACHINE_TO_LINE.update({str(k).strip(): str(v).strip()
                                for k, v in (cfg.get("machine_to_changeover_line") or {}).items()})
        MATRIX_MISSING_MIN.update({str(k).strip(): float(v) for k, v in
                                   (cfg.get("changeover_missing_pair_min") or {}).items()})
        MATRIX_MISSING_BY_TYPE.update({str(k).strip(): (float(v.get("same_type", 15)), float(v.get("other_type", 36)))
                                       for k, v in (cfg.get("changeover_missing_pair_by_type") or {}).items()})
        _co = pd.read_csv(_co_p)
        _co["changeover_min"] = pd.to_numeric(_co["changeover_min"], errors="coerce")
        _co = _co.dropna(subset=["changeover_min"])
        CHANGEOVER_LUT.update({
            (str(r.line).strip(), str(r.from_item).strip(), str(r.to_item).strip()):
                float(r.changeover_min)
            for r in _co.itertuples()
        })
        print(f"  CHANGEOVER MATRIX (plant): {len(CHANGEOVER_LUT):,} rules across "
              f"{len(set(MACHINE_TO_LINE.values()))} lines, {len(MACHINE_TO_LINE)} machine ids "
              f"mapped. Unmapped machines / uncovered pairs fall back to v6 (mixing 2 / 15).")
    else:
        print("  [WARN] inputs/changeover_lookup.csv absent — using v6's name-based rules "
              "only, which give EVERY CTP machine a flat 15 min. Run adapt_inputs.py.")

    _tr_h = {}
    if "transfer_time_h" in lots.columns:
        _t = (lots[["item_code", "transfer_time_h"]]
              .assign(transfer_time_h=lambda d: pd.to_numeric(d["transfer_time_h"],
                                                              errors="coerce").fillna(0.0))
              .groupby("item_code")["transfer_time_h"].max())
        _tr_h = {str(k).strip(): float(v) for k, v in _t.items() if v > 0}
        print(f"  TRANSFER TIME (routing): {len(_tr_h):,} items carry a transfer lag; "
              f"added to min_aging at every producer->consumer gap.")
    else:
        print("  [WARN] lots have no `transfer_time_h` column — transfer time NOT applied.")

    # ═══ CTP DEVIATION #10 — AN ITEM WITH NO AGING ROW MUST NOT GET A 365-DAY SHELF LIFE ══
    # v6: `mn, mx = aging_map.get(item, (DEFAULT_MIN_AGE_H, DEFAULT_MAX_AGE_H))` with
    # DEFAULT_MAX_AGE_H = 365 * 24 = 8,760 h. An item MISSING from the aging master silently
    # becomes IMMORTAL.
    #
    # MEASURED, and it was catastrophic. `CAP 66-MOTHERROLL` has no aging row:
    #   phase2 gave it   72 h   (via config aging_fallback_by_itemtype)
    #   phase5 gave it 8,760 h  (this default)          <- the two phases disagreed by 121x
    # The 8,760 h then DISABLED the only brake in `resync_short_life_components` (:2100,
    # `_pred_cap = predecessor_finish + predecessor_max_age`). 1,376 other lots WERE held back
    # by that guard. Cap strip never was — so the one-way ratchet smeared it 500 h and the
    # slitter finished on 12 JULY for a JUNE plan, while sitting HALF IDLE.
    #
    # AN ABSENT ROW IS THE SAME CLASS OF BUG AS A BLANK CELL (see MEMORY.md's present-but-blank
    # rule). Both are "no information", and both must be LOUD, never silently generous.
    _no_aging_row = []

    def _ages_ns(item):
        if item in item_min_age_ns:
            return item_min_age_ns[item], item_max_age_ns[item]
        _itype = item_itype.get(item, "")
        ov = AGE_OVERRIDE_H.get(_itype)
        # GREEN TYRES: the plant aging_master is the SOURCE OF TRUTH. Use this item's OWN
        # per-item row when it exists (all 224 CTP GT codes have one: 0.25 h / 24 h); fall
        # back to the itemtype scalar (itself master-derived) only if a GT item has no row.
        if _itype == "GREEN TYRES" and item in aging_map:
            mn, mx = aging_map[item]
        elif ov is not None:
            mn, mx = ov
        else:
            if item not in aging_map:
                _no_aging_row.append(item)
            mn, mx = aging_map.get(item, (DEFAULT_MIN_AGE_H, DEFAULT_MAX_AGE_H))
        mn = mn + _tr_h.get(str(item).strip(), 0.0)      # CTP: + routing transfer lag
        mn_ns = int(mn * NS_PER_H)
        mx_ns = int(mx * NS_PER_H)
        item_min_age_ns[item] = mn_ns
        item_max_age_ns[item] = mx_ns
        return mn_ns, mx_ns

    schedule = {}
    infeas   = []
    aging_v  = []
    T0_ns = pd.Timestamp(T0).value
    HORIZON_NS = int(MAX_HORIZON_DAYS * 24 * NS_PER_H)
    horizon_cutoff_ns = T0_ns + HORIZON_NS
    # V6_wave: derive horizon from PLAN's max endTime (not plan_params), so the
    # horizon = actual last block-finish in the plan. plan_params.planEndDate
    # can be stale/conservative; the plan itself is the ground truth.
    horizon = None
    try:
        plan = _db_or_csv("plan", cfg)
        if "endTime" in plan.columns:
            et = pd.to_datetime(plan["endTime"], errors="coerce")
            if et.notna().any():
                horizon = et.max()
                if pd.isna(horizon): horizon = None
    except Exception as _e:
        pass
    # V6_wave: NEG_INF_NS is a "very early" timestamp used as the floor for root
    # lots when no other floor applies.
    NEG_INF_NS = pd.Timestamp("2000-01-01").value
    # Global safety belt for AEST (non-bottleneck items only).
    GLOBAL_MAX_LOOKBACK_H = float(cfg.get("global_max_lookback_h", 168))
    GLOBAL_MAX_LOOKBACK_NS = int(GLOBAL_MAX_LOOKBACK_H * 3600 * NS)
    # SCHEDULE OPEN: plant turn-on date. Floor for root upstream lots and for
    # bottleneck items (which bypass AEST). Set in config; 168 h = 7 days.
    SCHEDULE_OPEN_H = float(cfg.get("schedule_open_h", 168))
    SCHEDULE_OPEN_NS = pd.Timestamp(first_curing - pd.Timedelta(hours=SCHEDULE_OPEN_H)).value
    print(f"  Schedule-open floor: {pd.Timestamp(SCHEDULE_OPEN_NS)}  (= first_curing - {SCHEDULE_OPEN_H:.0f} h)")
    # BR-V3 (2026-10-01): machines open at the PLAN START (T0 = plan.startTime.min), not at the first
    # curing that needs a newly built tyre. With schedule_open_h = 0 the old floor landed on the first
    # build slot (28-Sep 23:00) and left every upstream machine idle for the first 16 h of a live plant.
    if bool(cfg.get("open_machines_at_plan_start", False)) and pd.Timestamp(T0).value < SCHEDULE_OPEN_NS:
        _old_open = pd.Timestamp(SCHEDULE_OPEN_NS)
        SCHEDULE_OPEN_NS = pd.Timestamp(T0).value
        print(f"  [BR-V3] Schedule-open floor moved to the plan start: {pd.Timestamp(SCHEDULE_OPEN_NS)} "
              f"(was {_old_open}, +{(_old_open.value - SCHEDULE_OPEN_NS) / NS_PER_H:.1f} h of upstream time)")
    # UPSTREAM LEAD: let UPSTREAM (mixing/compound/calandar/belt/feeders) turn on
    # earlier than the building floor, so the deep belt cascade pre-builds a buffer
    # before building starts. Building/terminal lots still floor at SCHEDULE_OPEN_NS.
    # JK_UPSTREAM_LEAD_H=72 => upstream starts 3 days before building. Default 0 (off).
    UPSTREAM_LEAD_H = float(os.environ.get("JK_UPSTREAM_LEAD_H", "0") or 0)
    UPSTREAM_OPEN_NS = SCHEDULE_OPEN_NS - int(UPSTREAM_LEAD_H * NS_PER_H)
    if UPSTREAM_LEAD_H > 0:
        print(f"  Upstream-lead floor: {pd.Timestamp(UPSTREAM_OPEN_NS)}  "
              f"(= {UPSTREAM_LEAD_H:.0f} h before building floor)")
    # ═══ CTP DEVIATION #27 — ACTIVATION FLAGS. DEFAULT OFF = v6's OBSERVED BEHAVIOUR. ═══
    # See the block at the top of this file. The LOOKUP is repaired unconditionally; these
    # two flags decide whether the repaired RULE is allowed to bind. OFF selects the
    # original mixed-case constant, which is what production has always executed.
    ENABLE_FEEDER_JIT = bool(cfg.get("enable_feeder_jit", False))
    ENABLE_TARGET_WIP = bool(cfg.get("enable_target_wip", False))
    _FEEDER_JIT_SET = FEEDER_JIT_TYPES_U if ENABLE_FEEDER_JIT else FEEDER_JIT_TYPES
    _TARGET_WIP_MAP = TARGET_WIP_HOURS_U if ENABLE_TARGET_WIP else TARGET_WIP_HOURS
    # ── CTP building-drum TAIL ROPE for the short-shelf shared-cutter families ─────────
    # Belt cutter / ply cutters are 60%-busy non-constraints; AEST front-loads them, ageing
    # 24h-shelf belt/ply. But roping ALL of them tightens kit-readiness and slides the drum
    # (+0.8d). MEASURED: only the TAIL (lots whose material is ready > tail_h before their
    # LFT) actually over-ages badly (4 lots >72h, 105 >48h); the bulk over-24h is within the
    # plant's real tolerance and its early production is LOAD-BEARING (keeps the drum fed).
    # So JIT ONLY the tail (large-slack lots); leave the bulk AEST -> drum unmoved -> makespan
    # flat. per-lot gate applied at placement below. [] = OFF = v6 behaviour.
    _SHORT_SHELF_JIT = {str(t).strip().upper() for t in (cfg.get("jit_short_shelf_itypes") or [])}
    _SHORT_SHELF_BUFFER_H = float(cfg.get("jit_short_shelf_buffer_h", 4.0))     # finish this many h before LFT
    _SHORT_SHELF_TAIL_H   = float(cfg.get("jit_short_shelf_tail_h", 48.0))      # rope only lots with slack > this
    # REMOVE-JIT experiment (place_asap_itypes): place these component types PURE ASAP (from the
    # UPSTREAM_OPEN/runway floor), bypassing the max_aging freshness cap -> they FILL the runway
    # instead of waiting JIT. WARNING: short-shelf ones WILL over-age (that is the point of the test).
    _NO_AEST = {str(t).strip().upper() for t in (cfg.get("place_asap_itypes") or [])}
    _BRC = cfg.get("bottleneck_release") or {}
    _BREL = ({str(t).strip().upper() for t in (_BRC.get("itypes") or [])} if _BRC.get("enabled", False) else set())
    _BREL_MARGIN_NS = int(float(_BRC.get("shelf_margin_h", 2.0)) * NS_PER_H)
    if _BREL:
        print(f"  [V25 BOTTLENECK RELEASE] {sorted(_BREL)}: start when inputs are ready, limited only by shelf life "
              f"(need - (max_aging - {_BRC.get('shelf_margin_h', 2.0)} h))")
    if _NO_AEST:
        print(f"  PLACE-ASAP (JIT/shelf hold-back OFF): {sorted(_NO_AEST)} built ASAP into the runway (WILL age)")
    # EAGER SHELF RELEASE (2026-08-03 USER): the TRUE short-shelf cut-breakers (Cap Strip / SLITTED /
    # PRE CUT ROLL) JIT from empty -> the day-1..3 dip. Give each a rolling buffer ~(max_age - margin)h
    # ahead -- shelf-capped and per-item, so it fills the dip, compresses the ramp, and CANNOT age.
    _EAGER_SHELF = {str(t).strip().upper() for t in (cfg.get("eager_shelf_itypes") or [])}
    _EAGER_MARGIN_H = float(cfg.get("eager_shelf_margin_h", 4.0))
    if _EAGER_SHELF:
        print(f"  EAGER SHELF RELEASE: {sorted(_EAGER_SHELF)} -> rolling buffer (max_age - {_EAGER_MARGIN_H}h), shelf-capped (cannot age)")
    if _SHORT_SHELF_JIT:
        print(f"  SHORT-SHELF TAIL ROPE:  ON  -- {sorted(_SHORT_SHELF_JIT)} JIT'd only when slack > {_SHORT_SHELF_TAIL_H:.0f}h "
              f"(bulk stays AEST -> makespan-neutral)")
    print(f"  FEEDER-JIT (production de-pulse): "
          f"{'ON  -- ' + str(len(FEEDER_JIT_TYPES_U)) + ' item_types bind' if ENABLE_FEEDER_JIT else 'OFF -- v6 observed behaviour (0 item_types bind: case-mismatch)'}")
    print(f"  TARGET-WIP (rolling buffer):      "
          f"{'ON  -- ' + str(len(TARGET_WIP_HOURS_U)) + ' item_types bind' if ENABLE_TARGET_WIP else 'OFF -- v6 observed behaviour (only the 3 accidental-caps keys bind)'}")

    # HORIZON enforcement: if any lot's finish > horizon, flag PAST_HORIZON.
    ENFORCE_HORIZON = bool(cfg.get("enforce_strict_horizon", False))
    HORIZON_LIMIT_NS = pd.Timestamp(horizon).value if (horizon is not None) else None
    print(f"  Strict horizon:      {ENFORCE_HORIZON} (horizon={horizon})")

    # V6_wave PIPELINE PLACEMENT: anchor each lot to its wave window.
    # wave_anchor_ns[wave_id] = T0 + (wave_idx - 1) * wave_duration
    # Each lot starts ASAP within its wave; if predecessors push later, follow them.
    WAVE_DUR_NS = int(3 * 24 * 3600 * NS)
    def _wave_anchor(wave_id):
        try:
            wi = int(str(wave_id).lstrip("W"))
        except (ValueError, TypeError):
            return T0_ns
        # W01 -> anchor T0; W02 -> T0 + 3d; W03 -> T0 + 6d; ...
        return T0_ns + (wi - 1) * WAVE_DUR_NS

    def insert_busy(machine, s_ns, e_ns):
        bisect.insort(machine_busy[machine], (s_ns, e_ns))
        # Track accumulated busy time for tie-break (drains over-loaded machines).
        machine_busy_ns[machine] += max(0, e_ns - s_ns)

    # ── BR-V4 NEIGHBOUR-AWARE CHANGEOVER (2026-10-01, BUSINESS_RULES_LOG.md) ──────────────────
    # Lots are placed in PRIORITY order, not time order, so a lot often lands in an earlier gap
    # between two lots placed before it. v3 charged ONE changeover from the machine's last-PLACED
    # item (often days away) and never reserved the changeover to the NEXT lot; final_repair_pass
    # then pushed 3,489 lots later to fix the gaps, cascading through the week. Here a gap
    # between real time-neighbours P and N fits lot B only if
    #     start(B) >= end(P) + co(P -> B)   and   end(B) + co(B -> N) <= start(N)
    # with co from compute_changeover_min (matrix, 60 s mixer, 160 s butyl clean-out, ...).
    # BUSY_ITEM[(machine, interval start)] = (item at the interval's start, item at its end);
    # intervals without an item (PM / holiday windows) charge no changeover on that side.
    # ── BR-V6 HOLIDAY-AWARE PRODUCTION (2026-10-01, BUSINESS_RULES_LOG.md) ───────────────────
    # Plant-wide holiday windows (downtime_reason rows with the holiday reason on >= min_machines
    # machines). Around each window, per upstream lot with a known need time (LFT):
    #   H1  short-shelf item (shelf <= short_shelf_max_h) needed after the holiday, whose age at use
    #       would exceed its shelf life if made before it -> may not start before the holiday ends
    #   H3  ...and is targeted at the holiday END (first slots of the restart), not JIT later
    #   H2  long-shelf item (shelf >= prebuild_min_shelf_h) needed within prebuild_window_h after the
    #       holiday and still inside its shelf life (minus safety) if finished at the holiday start
    #       -> targeted to finish BEFORE the holiday (pre-built on the last day's spare capacity)
    _HA = cfg.get("holiday_aware") or {}
    HOLIDAYS = []
    if _HA.get("enabled", False):
        try:
            _hd = pd.read_csv(ROOT / "db_pipeline" / "raw" / "jkplanning_CTP__downtime_reason.csv")
            _hd = _hd[_hd["Downtime Reason"].astype(str).str.strip().str.upper()
                      == str(_HA.get("reason", "HOLIDAY")).upper()]
            _hw = _hd.groupby(["Scheduled Start Time", "Scheduled End Time"])["Machine ID"].nunique()
            HOLIDAYS = sorted((pd.Timestamp(a).value, pd.Timestamp(b).value)
                              for (a, b), n in _hw.items() if n >= int(_HA.get("min_machines", 20)))
        except Exception as _e:
            print(f"  [BR-V6] !! holiday windows unreadable ({type(_e).__name__}: {_e}) -- rule inactive")
    _HA_SHORT = float(_HA.get("short_shelf_max_h", 48)) * NS_PER_H
    _HA_LONG = float(_HA.get("prebuild_min_shelf_h", 72)) * NS_PER_H
    _HA_WIN = float(_HA.get("prebuild_window_h", 48)) * NS_PER_H
    _HA_SAFE = float(_HA.get("safety_h", 4)) * NS_PER_H
    _ha_stat = {"H1": 0, "H2": 0}
    if HOLIDAYS:
        print("  [BR-V6] holiday-aware production ON for: "
              + ", ".join(f"{pd.Timestamp(a)} -> {pd.Timestamp(b)}" for a, b in HOLIDAYS))

    def _holiday_targets(lid, earliest_ns, target_start_ns, lft_ns, dur_ns):
        """BR-V6 H1/H2/H3: adjust (earliest, target) of one upstream lot around the holidays."""
        if not HOLIDAYS or lft_ns is None:
            return earliest_ns, target_start_ns
        _mx = _ages_ns(lot_item.get(lid, ""))[1]
        if not _mx or _mx <= 0:
            return earliest_ns, target_start_ns
        for hs, he in HOLIDAYS:
            if lft_ns < he:                                    # needed before the holiday ends
                continue
            if _mx <= _HA_SHORT:
                # H1 + H3: made before hs it would be >= lft - hs old at use
                if target_start_ns < hs and (lft_ns - hs) > (_mx - _HA_SAFE):
                    earliest_ns = max(earliest_ns, he)
                    target_start_ns = max(target_start_ns, he)
                    _ha_stat["H1"] += 1
            elif _mx >= _HA_LONG and lft_ns <= he + _HA_WIN:
                # H2: finish before hs, still inside shelf life at the need time
                if (lft_ns - hs) <= (_mx - _HA_SAFE) and hs - dur_ns >= max(earliest_ns, UPSTREAM_OPEN_NS) \
                        and target_start_ns > hs - dur_ns:
                    target_start_ns = hs - dur_ns
                    _ha_stat["H2"] += 1
        return earliest_ns, target_start_ns

    NB_CO = bool(cfg.get("neighbour_aware_changeover", False))
    BUSY_ITEM = {}
    ITEM_ITYPE = {}
    for _l, _it in lot_item.items():
        ITEM_ITYPE.setdefault(str(_it), lot_item_type.get(_l, ""))
    _nb_stat = {"lots": 0, "co_before_min": 0.0, "co_after_checked": 0}
    if NB_CO:
        print("  [BR-V4] neighbour-aware changeover ON: gaps are checked against the real "
              "time-neighbours (before AND after), not the last-placed item")

    def first_fit_nb(m, earliest_ns, target_start_ns, dur_ns, item, dept, itype):
        """JIT first-fit like first_fit_jit, but every gap is checked with the changeover from
        its real predecessor AND to its real successor. Returns (start_ns, co_before_ns)."""
        busy = machine_busy[m]
        n = len(busy)

        def _co(a, b):
            """changeover a -> b on this machine in ns (0 when either side has no item)."""
            if not a or not b or a == b:
                return 0
            return int(float(compute_changeover_min(m, dept, a, ITEM_ITYPE.get(a, ""),
                                                    b, ITEM_ITYPE.get(b, itype)) or 0) * 60 * NS)

        def _item(iv, at_start):
            v = BUSY_ITEM.get((m, iv[0]))
            return "" if v is None else (v[0] if at_start else v[1])

        idx = bisect.bisect_left(busy, (earliest_ns,))
        best, best_co = None, 0
        for i in range(idx, n + 1):
            prev = busy[i - 1] if i > 0 else None
            nxt = busy[i] if i < n else None
            c_b = _co(_item(prev, False), item) if prev is not None else 0
            lo = earliest_ns if prev is None else max(earliest_ns, prev[1] + c_b)
            if nxt is not None:
                hi = nxt[0] - _co(item, _item(nxt, True)) - dur_ns     # room for B + co(B -> N)
                if lo > hi:
                    continue
                place = max(lo, min(target_start_ns, hi))
            else:
                place = max(lo, target_start_ns)
            if place >= target_start_ns:
                return place, c_b
            if best is None or place > best:
                best, best_co = place, c_b
        return best, best_co

    # ═══ CTP DEVIATION #20 — THE QUANTITY-SUMMING MATERIAL GATE ═══════════════════════
    # THE ONE MATERIAL MODEL. Every placement path now calls THIS and nothing else.
    #
    # WHAT WAS THERE (the "POOLED-MHE PREDECESSOR MODEL"), and why it is (A) DEFECT:
    #   v6 said predecessors of the SAME ITEM are ALTERNATIVES — "any 1 MHE supplies" —
    #   so a consumer could start when the EARLIEST same-item producer finished. It took
    #   a MIN. IT NEVER SUMMED QUANTITY. Its SOLE stated justification was MHE sub-lots,
    #   and its worked example was "15 Stage-1 carcass MHE producers feed 1 Stage-2
    #   consumer; Stage-2 takes the earliest carcass MHE and doesn't wait for all 10."
    #
    #   BOTH HALVES OF THAT JUSTIFICATION ARE ABSENT FROM CTP'S DATA. MEASURED:
    #     * mhe_total == 1 on 6,780 of 6,780 lots (100.0%). phase2_lot_sizing.py PRINTS
    #       "MHE MODEL INERT: no Maximum Run Qty in the MPQ master". CTP has no max MPQ,
    #       so there are NO sub-lots. The drum arithmetic (p_start + p_dur // mhe_total)
    #       is an IDENTITY on CTP: it evaluates to p_finish for every lot. The model's
    #       whole apparatus reduces to the bare MIN.
    #     * There are ZERO lots of item_type "Carcass" in the CTP lot table. The two-stage
    #       carcass->GT flow the example describes DOES NOT EXIST here; GT is built in one
    #       stage directly from components. The example is not merely inapplicable, it is
    #       describing another plant.
    #
    #   A rule whose OWN STATED JUSTIFICATION does not exist on this data is a (A) DEFECT,
    #   NOT a (B) policy. We are not choosing a different rule; we are deleting a rule that
    #   was never true here. On CTP, same-item predecessors are DISTINCT, QUANTITY-CARRYING
    #   LOTS — not interchangeable drums off one rack.
    #
    #   WHAT IT COST: the safety line the whole schedule rested on — "MATERIAL STARVATION
    #   (the number that matters): 0" — was produced by a model that CANNOT DETECT A
    #   SHORTAGE. Measured on the shipping schedule, 44 (consumer,item) groups started with
    #   INCOMPLETE material; median 65.9% of the required quantity present, WORST 33.3%.
    #   e.g. GT 1503 NEO MSIL_L0003 took CAP 66 - CAPSTRIP with 125,466 of 376,398 units
    #   finished. The zero was a decoration.
    #
    # THE RULE THAT REPLACES IT (this function):
    #   A consumer may start only when, for EVERY required input item, the CUMULATIVE
    #   FINISHED-AND-AGED QUANTITY covers what that consumer consumes of that item.
    #
    #       supply(i, t) = Σ lot_qty(p)  over p ∈ preds(consumer, i) with finish(p)+min_age(i) ≤ t
    #       need(i)      = Σ lot_qty(p)  over p ∈ preds(consumer, i)
    #       start ≥ min{ t : supply(i,t) ≥ need(i) }  for every i
    #
    # !! WHERE `need` COMES FROM, AND THE ONE THING YOU MUST KNOW ABOUT IT !!
    #   phase3's edge carries NO QUANTITY — its columns are exactly
    #       producer_lot, producer_item, consumer_lot, consumer_item, via_sku
    #   There is no qty and no fraction. So the ONLY consumption figure formable from
    #   "the lot table's own lot_qty and the DAG's own edges" is the SUM OF THE CONSUMER'S
    #   OWN PREDECESSOR LOTS of that item. That is the number used above, and it is the
    #   same number the user's own E1 measurement used (it reproduces 44 / 33.3% exactly).
    #   NOTHING IS INVENTED: no rate, no default, no guess.
    #
    #   CONSEQUENCE, AND IT IS A REAL ONE — I AM NOT HIDING IT: because `need` is a sum of
    #   the consumer's preds, coverage is reached only when EVERY pred is finished+aged, so
    #   this gate collapses to strict DAG precedence. For items whose edges are a genuine
    #   ALLOCATION that is exactly right; for items whose edges are a COMPATIBILITY FAN-OUT
    #   it is CONSERVATIVE (it may hold a consumer that physically had enough). MEASURED,
    #   Σ(consumer's preds qty) vs the item's TOTAL production:
    #       Ply, Steel Belt              ratio 1.00   <- exact allocation. Gate is EXACT.
    #       Cap Strip                    ratio 26.7   <- pooled. Gate OVERSTATES need 26.7x.
    #       CALANDARED ROLL              ratio up to 81.7  <- pooled. Overstates most.
    #   THE COST IS SMALL AND I MEASURED THAT TOO: the fan-out is on the PRODUCER side, not
    #   the consumer side. Per (consumer,item) group the producer count is 1 in 16,782 of
    #   18,730 groups (89.6%), and MAX 6. Where there is 1 producer, MIN == MAX and this
    #   gate changes NOTHING. Only 1,948 groups (10.4%) are affected at all, and they are
    #   overwhelmingly Cap Strip (median 3 producers).
    #   THE PROPER FIX for the remaining conservatism is a qty/fraction column on phase3's
    #   edge, which I DO NOT OWN. It is a phase3 change. Flagged, not guessed.
    #
    # The sweep below is written as a genuine cumulative-coverage sweep, not a bare max(),
    # so that (a) the code literally executes the stated rule, (b) the audit can report the
    # SHORTFALL FRACTION, and (c) if phase3 ever emits a per-edge quantity, ONLY the `need`
    # line changes and the gate becomes exact for pooled items too.
    import re as _re_aa
    _AA_GLOBAL = [_re_aa.compile(str(p_), _re_aa.I) for p_ in (cfg.get("kit_gate_assume_available") or [])]
    # v135 (user 2026-10-15): ASSUMPTION -- listed compounds (TBR finals with no opening stock) are on the floor for the
    # plan's first `hours`: a lot whose first need is inside that window does not wait for them. Off = v133.
    _WAF = cfg.get("wip_assume_first_day") or {}
    _WAF_ON = bool(_WAF.get("enabled", False))
    _WAF_IT = {str(x).strip() for x in (_WAF.get("items") or [])}
    _WAF_END = int(SCHEDULE_OPEN_NS) + int(float(_WAF.get("hours", 24)) * 3600 * 1e9)
    _WAF_N = set()
    if _WAF_ON:
        print(f"  [WIP ASSUME v135] {sorted(_WAF_IT)} assumed on the floor until {pd.Timestamp(_WAF_END)} "
              f"(lots needed before then do not wait for them)")

    def _waf_skip(lid, ip):
        if not _WAF_ON or str(ip).strip() not in _WAF_IT:
            return False
        fn = lot_first_need.get(lid)
        if fn is None or pd.isna(fn) or int(pd.Timestamp(fn).value) >= _WAF_END:
            return False
        _WAF_N.add(lid)
        return True

    # v69 #2 (user 2026-10-10): BATCH-GROUP RELEASE of compound lots -- a compound lot's batches are usable as they
    # come off the mixer (+ min aging), not only when the whole lot is finished. Each consumer's share is taken in
    # need order (first consumer = first batches): its share is usable at lot start + run time x (cumulative share
    # up to and including it) + min aging. Same lots, runs and batches -- only WHEN a consumer may draw changes.
    _BRC69 = cfg.get("batch_release") or {}
    _BRF69 = {}
    if _BRC69.get("enabled", False) and EDGE_QTY_ON:
        _bt69 = {str(x).strip().upper() for x in (_BRC69.get("item_types") or ["MASTER COMPOUND", "FINAL COMPOUND"])}
        _far69 = pd.Timestamp("2100-01-01").value
        for _p69, _cs69 in successors.items():
            if str(lot_item_type.get(_p69, "")).upper() not in _bt69:
                continue
            _qs69 = [(c, float(edge_qty.get((_p69, c), 0.0) or 0.0)) for c in dict.fromkeys(_cs69)]
            _qs69 = [x for x in _qs69 if x[1] > 0]
            _tot69 = sum(v for _, v in _qs69)
            if _tot69 <= 0:
                continue
            _qs69.sort(key=lambda x: (pd.Timestamp(lot_first_need[x[0]]).value
                                      if (x[0] in lot_first_need and lot_first_need[x[0]] is not None
                                          and not pd.isna(lot_first_need[x[0]])) else _far69, str(x[0])))
            _cum69 = 0.0
            for _c69, _v69 in _qs69:
                _cum69 += _v69
                _BRF69[(_p69, _c69)] = min(1.0, _cum69 / _tot69)
        print(f"  [BATCH RELEASE v69] {len(_BRF69):,} compound -> consumer edges released batch by batch "
              f"(consumer share usable at lot start + run x cumulative share + min aging)")

    def _material_ready_ns(lid):
        """Earliest ns at which cumulative finished+aged supply covers this lot's
        consumption of EVERY required input item. NEG_INF_NS if it has no placed preds."""
        ready = NEG_INF_NS
        by_item = defaultdict(list)
        for p in predecessors.get(lid, ()):
            if p in schedule:
                by_item[lot_item.get(p, "")].append(p)
        for ip, plist in by_item.items():
            if _AA_GLOBAL and any(r_.search(str(ip)) for r_ in _AA_GLOBAL):
                continue          # V5 guarantees this chain (cap strip / edge gum): never gates
            if _WAF_ON and _waf_skip(lid, ip):
                continue          # v135: assumed on the floor for the first day
            p_min_ns, _ = _ages_ns(ip)
            events = []          # (qty available at this ns, qty)
            need = 0.0
            for p in plist:
                # ═══ CTP #33 (F1) ═══ need/supply per edge, not per whole producer lot.
                # OFF: qp = the producer's WHOLE lot_qty on both sides, so `need` is the
                #      sum of every bound pred and cum() only reaches it at the LAST one
                #      -> the gate collapses to strict precedence (v6, and 28.7x wrong).
                # ON:  qp = consume_qty(p -> lid), the FEFO-allocated true consumption.
                #      SUM(qp) == the true BOM consumption, and the same number is the
                #      supply, so no consumer is ever credited quantity another consumer
                #      was already promised.
                qp = (edge_qty.get((p, lid), 0.0) if EDGE_QTY_ON
                      else float(lot_qty.get(p, 0.0) or 0.0))
                need += qp
                _bf69 = _BRF69.get((p, lid)) if _BRF69 else None
                if _bf69 is not None:
                    _s69 = int(schedule[p]["scheduled_start"].value)
                    events.append((_s69 + int((int(schedule[p]["finish_ns"]) - _s69) * _bf69) + p_min_ns, qp))
                else:
                    events.append((schedule[p]["finish_ns"] + p_min_ns, qp))
            # ═══ CTP #34 (F3) ═══ opening stock on the floor at t=0 covers part of it.
            if open_alloc and (lid, ip) not in _OPEN_NETTED:
                _cov = open_alloc.get((lid, ip), 0.0)
                if _cov > 0.0:
                    need -= _cov
                    if need <= 1e-6:
                        continue          # fully covered by stock the plant already holds
            events.sort()
            if need <= 1e-6 and (lid, ip) in _OPEN_NETTED:
                continue                  # v83: whole need of this item came from floor stock (netted into its links)
            if need <= 0.0:
                # No quantity in the data for this item. We do NOT silently drop the DAG
                # edge — an unquantified lot still cannot be consumed before it is made.
                # Fall back to strict precedence. (MEASURED: 0 lots on CTP have lot_qty<=0,
                # so this branch is currently unreachable. It exists so that a future blank
                # cell FAILS SAFE instead of failing open. See MEMORY: the present-but-blank
                # NaN trap.)
                t = events[-1][0]
            else:
                cum = 0.0
                t = events[-1][0]
                for avail_ns, qp in events:
                    cum += qp
                    if cum >= need - 1e-6:
                        t = avail_ns          # supply first COVERS need at this instant
                        break
            if t > ready:
                ready = t
        return ready

    def place_one_lot_jit(lid):
        """Per-lot placement (ASAP upstream / LFT terminal).

        Material availability floor (earliest_ns):
          _material_ready_ns(lid) — the ONE quantity-summing gate (CTP #20). A lot may
          not start until, for EVERY required input item, the cumulative FINISHED-AND-AGED
          quantity covers what this lot consumes of it.

        (The docstring here used to describe a per-MHE drum flow — "first_drum_ready =
         p_start + p_dur / N". On CTP mhe_total == 1 on all 6,780 lots, so N is always 1
         and that expression is an IDENTITY equal to p_finish. It described machinery that
         cannot run on this data. Removed rather than left to mislead.)

        Target start:
          Terminal dept (BUILDING/TBM/CURING) -> LFT-driven (target = LFT - dur).
          Upstream dept (Mixing/Calendar/Cutter/etc.) -> ASAP (target = earliest).
        """
        dur_h = float(duration_h.get(lid, 0))
        dur_ns = int(dur_h * 3600 * NS)
        item = lot_item.get(lid, "")
        machines = lot_machines.get(lid, [])
        if not machines:
            return None

        # === MATERIAL FLOOR — CTP DEVIATION #20 ===
        # WAS: the POOLED-MHE PREDECESSOR MODEL — a MIN over same-item predecessors
        # ("any 1 MHE supplies"), which NEVER SUMMED QUANTITY and therefore could not
        # detect a shortage. Its justification (MHE sub-lots; a two-stage carcass rack)
        # does not exist on CTP's data. (A) DEFECT. See _material_ready_ns() above for
        # the measurements and the full write-up.
        # NOW: the ONE quantity-summing gate. Same-item predecessors are DISTINCT,
        # QUANTITY-CARRYING LOTS and their quantity is SUMMED.
        earliest_ns = _material_ready_ns(lid)

        # ASAP upstream (bounded by max_aging) / LFT terminal
        dept = lot_dept.get(lid, "")
        lft = lft_map.get(lid)
        lft_ns_val = pd.Timestamp(lft).value if (lft is not None and not pd.isna(lft)) else None
        if _is_terminal_dept(dept):
            # FIX 1 (top quick-win): Building has 38% spare capacity but tail extends 11 days.
            # Old: LFT-dur (JIT-late) made building wait → upstream cascade stretched.
            # New: pull building to AEST so the chain compresses upstream.
            aest = aest_map.get(lid)
            if aest is not None and not pd.isna(aest):
                aest_ns = pd.Timestamp(aest).value
                if aest_ns < SCHEDULE_OPEN_NS:
                    aest_ns = SCHEDULE_OPEN_NS
                target_start_ns = max(earliest_ns, aest_ns)
            elif lft_ns_val is not None:
                target_start_ns = max(earliest_ns, lft_ns_val - dur_ns)
            else:
                wave_id = lot_wave_first.get(lid, "Z99")
                target_start_ns = max(earliest_ns, _wave_anchor(wave_id))
        elif lid in _LEVEL_TGT:
            # V44: the levelled 901 plan sets this lot's start (never before its inputs are ready)
            target_start_ns = max(earliest_ns if earliest_ns != NEG_INF_NS else UPSTREAM_OPEN_NS,
                                  UPSTREAM_OPEN_NS, int(_LEVEL_TGT[lid]))
        else:
            # V6_wave SELECTIVE AEST.
            itype = lot_item_type.get(lid, "")
            # SHORT-SHELF TAIL ROPE: only belt/ply lots whose material is ready far (> tail_h)
            # before their LFT would over-age at AEST -> JIT just those. Small-slack lots keep
            # AEST (their early production is load-bearing) so the drum is not perturbed.
            _short_rope = False
            if itype in _SHORT_SHELF_JIT and lft_ns_val is not None:
                _ef = UPSTREAM_OPEN_NS if earliest_ns == NEG_INF_NS else max(earliest_ns, UPSTREAM_OPEN_NS)
                _short_rope = (lft_ns_val - _ef) > int(_SHORT_SHELF_TAIL_H * NS_PER_H)
            # CTP #27: _FEEDER_JIT_SET is the CASE-REPAIRED set when enable_feeder_jit is
            # on, and the original (never-matching) mixed-case set when it is off.
            if (itype in _FEEDER_JIT_SET or _short_rope) and lft_ns_val is not None:
                # PRODUCTION DE-PULSE: place this feeder JIT — finish just before its
                # (already-smooth) deadline instead of AEST-early. Spreads component
                # production to track the level daily demand -> even kit-readiness.
                _floor = UPSTREAM_OPEN_NS if earliest_ns == NEG_INF_NS else max(earliest_ns, UPSTREAM_OPEN_NS)
                # short-shelf rope uses its own buffer -> finishes a little earlier so a cutter
                # hiccup can't starve the pinned drum, while still landing well inside 24h.
                _buf_h = _SHORT_SHELF_BUFFER_H if _short_rope else FEEDER_JIT_BUFFER_H
                _jit = lft_ns_val - dur_ns - int(_buf_h * NS_PER_H)
                target_start_ns = max(_floor, _jit)
            elif _BREL and str(itype).strip().upper() in _BREL and lft_ns_val is not None:
                # V25 BOTTLENECK RELEASE (config bottleneck_release): saturated machines (mixers, 1802 edge
                # filler, bead apexers, 1803) start as soon as inputs are ready -- limited ONLY by shelf
                # life: never earlier than need - (max_aging - margin). No AEST / look-back / safety hold.
                _imx = _ages_ns(lot_item.get(lid, ""))[1]
                _shelf_floor = lft_ns_val - dur_ns - max(_imx - _BREL_MARGIN_NS, 0)
                if lid in _FILL_FLOOR:                      # v47 final feed: shelf life at the LAST use of the lot
                    _shelf_floor = int(_FILL_FLOOR[lid])
                target_start_ns = max(earliest_ns, UPSTREAM_OPEN_NS, _shelf_floor)
            elif str(itype).strip().upper() in _NO_AEST:
                # Bottleneck: pure ASAP from (upstream) schedule-open date.
                if earliest_ns == NEG_INF_NS:
                    target_start_ns = UPSTREAM_OPEN_NS
                else:
                    target_start_ns = max(earliest_ns, UPSTREAM_OPEN_NS)
            else:
                # Non-bottleneck: AEST applies, but FIX 2 caps it at consumer_first_need - max_aging.
                # NEW: target_wip floor — keep N hours of rolling buffer alive.
                aest = aest_map.get(lid)
                lft_floor = lft_map.get(lid)
                item_min_ns, item_max_ns = _ages_ns(lot_item.get(lid, ""))
                safety_ns = int(AGING_SAFETY_BUFFER_H * NS_PER_H)
                aging_cap_ns = None
                # === F3 v3 (REVERTED) SAFE WIP TARGET ===
                # Lesson: target_wip = (max_aging-safety)/2 was too aggressive — caused
                # producer to be placed too far back which broke supply chain.
                # New rule: use itemtype default, but cap at max_aging - safety - headroom (16h).
                # For short-aging items, target_wip = 0 (JIT placement).
                # CTP #27: _TARGET_WIP_MAP is the CASE-REPAIRED map when enable_target_wip
                # is on (all 22 keys reachable), and the original mixed-case map when it is
                # off (only FINAL COMPOUND / CALANDARED ROLL / PRE CUT ROLL MATERIAL hit --
                # the 3 keys that are accidentally already in caps). See the header block.
                target_wip_h_default = _TARGET_WIP_MAP.get(itype, 0.0)
                item_max_h = item_max_ns / NS_PER_H if item_max_ns > 0 else 0.0
                _headroom_h = AGING_SAFETY_BUFFER_H + 8.0   # 16h headroom
                if item_max_h > 0:
                    # Maximum safe WIP buffer leaving headroom inside max_aging
                    max_safe_wip_h = max(0.0, item_max_h - _headroom_h)
                    if target_wip_h_default > 0:
                        # Cap default at max_safe
                        target_wip_h = min(target_wip_h_default, max_safe_wip_h)
                    else:
                        # No itemtype default → JIT (target_wip = 0, no buffer pull)
                        target_wip_h = 0.0
                    # Force short-aging items to JIT (item_max < default+headroom -> 0)
                    if item_max_h < target_wip_h_default + _headroom_h:
                        target_wip_h = 0.0
                else:
                    target_wip_h = 0.0
                # EAGER SHELF RELEASE (2026-08-03 USER): override the JIT-forcing for the true
                # short-shelf cut-breakers -> keep a rolling (max_age - margin) buffer so they fill
                # the day-1..3 dip instead of ramping from empty. aging_cap_ns below still bounds the
                # rack (cannot age). Per-item TRUE shelf: 24h -> ~20h buffer; a short-shelf item fits
                # only a few h (physical residual, accepted -- not forced to a full day).
                if str(itype).strip().upper() in _EAGER_SHELF and item_max_h > _EAGER_MARGIN_H:
                    target_wip_h = item_max_h - _EAGER_MARGIN_H
                target_wip_ns = int(target_wip_h * NS_PER_H)
                wip_cap_ns = None
                if lft_floor is not None and not pd.isna(lft_floor):
                    lft_ns_local = pd.Timestamp(lft_floor).value
                    # max_aging cap: never produce more than max_aging+safety ahead of consumer
                    aging_cap_ns = lft_ns_local - dur_ns - item_max_ns - safety_ns
                    # WIP cap: start no LATER than consumer needs - target_wip_h.
                    if target_wip_h > 0:
                        wip_cap_ns = lft_ns_local - dur_ns - target_wip_ns
                if aest is not None and not pd.isna(aest):
                    aest_ns = pd.Timestamp(aest).value
                    # Safety belt: AEST cannot be earlier than LST - global_max_lookback_h
                    if lft_floor is not None and not pd.isna(lft_floor):
                        lst_ns = pd.Timestamp(lft_floor).value - dur_ns
                        aest_floor_ns = lst_ns - GLOBAL_MAX_LOOKBACK_NS
                        if aest_ns < aest_floor_ns:
                            aest_ns = aest_floor_ns
                    # FIX 2 enforce: never earlier than aging_cap_ns
                    if aging_cap_ns is not None and aest_ns < aging_cap_ns:
                        aest_ns = aging_cap_ns
                    # WIP MAINTENANCE: producer must start no later than wip_cap_ns
                    # (consumer_first_need - target_wip_hours). Keeps buffer alive.
                    if wip_cap_ns is not None and aest_ns > wip_cap_ns:
                        aest_ns = wip_cap_ns
                    # Final floor: never earlier than the UPSTREAM schedule-open date
                    # (lets the deep belt cascade pre-build ahead of the building floor).
                    if aest_ns < UPSTREAM_OPEN_NS:
                        aest_ns = UPSTREAM_OPEN_NS
                    if earliest_ns == NEG_INF_NS:
                        target_start_ns = aest_ns
                    else:
                        target_start_ns = max(earliest_ns, aest_ns)
                else:
                    # No AEST data -> CAPACITY-AWARE FALLBACK.
                    if aging_cap_ns is not None:
                        target_start_ns = max(earliest_ns, aging_cap_ns) if earliest_ns > NEG_INF_NS else aging_cap_ns
                        if target_start_ns < UPSTREAM_OPEN_NS:
                            target_start_ns = UPSTREAM_OPEN_NS
                    else:
                        target_start_ns = max(earliest_ns, UPSTREAM_OPEN_NS) if earliest_ns > NEG_INF_NS else UPSTREAM_OPEN_NS

        # ── PRIORITY PROTECTION ON BOTTLENECK MACHINES (2026-09-27, RCA day-27 TBR) ─────────
        # Placement is one-lot-at-a-time into the earliest free slot, so a compound needed on day 4
        # whose inputs happen to be ready grabbed saturated mixer time on day 1-2 (measured: 47 lots
        # = 30 mixer-h not due before 28-Sep ran on 26-27 Sep while day-27 TBR compound waited).
        # For the configured item types, a lot may not be targeted earlier than
        # (its latest start - protect_buffer_h): urgent lots are unaffected, non-urgent lots stop
        # taking early bottleneck capacity. Config: priority_protect.{itypes, buffer_h}.
        _pp = cfg.get("priority_protect") or {}
        if _pp.get("enabled") and str(lot_item_type.get(lid, "")).upper() in {
                str(t).upper() for t in (_pp.get("itypes") or [])}:
            _lf = lft_map.get(lid)
            if _lf is not None and not pd.isna(_lf):
                _floor_ns = int(pd.Timestamp(_lf).value) - dur_ns - int(float(_pp.get("buffer_h", 24)) * NS_PER_H)
                if _floor_ns > target_start_ns:
                    target_start_ns = _floor_ns

        # BR-V8: compound lots start as early as their inputs allow, within shelf life at the LFT
        if MIX_EDD and str(lot_item_type.get(lid, "")).upper() in _MIX_T:
            _t = UPSTREAM_OPEN_NS if earliest_ns == NEG_INF_NS else max(earliest_ns, UPSTREAM_OPEN_NS)
            _mxa = _ages_ns(lot_item.get(lid, ""))[1]
            if lft_ns_val is not None and _mxa and _mxa > 0:
                _t = max(_t, lft_ns_val - dur_ns - _mxa + _MEDD_SAFE_NS)
            target_start_ns = _t

        # BR-V9: TBR components as early as inputs allow, within shelf life at the building need
        if (TBR_ASAP and str(lot_item.get(lid, "")).strip() in TBR_ITEMS
                and str(lot_item_type.get(lid, "")).upper() not in _MIX_T
                and str(lot_dept.get(lid, "")).upper() not in ("BUILDING", "CURING")):
            _t = UPSTREAM_OPEN_NS if earliest_ns == NEG_INF_NS else max(earliest_ns, UPSTREAM_OPEN_NS)
            _fn = lot_first_need.get(lid)
            _mxa = _ages_ns(lot_item.get(lid, ""))[1]
            if _fn is not None and not pd.isna(_fn) and _mxa and _mxa > 0:
                _t = max(_t, pd.Timestamp(_fn).value - dur_ns - _mxa + _TBA_SAFE_NS)
            target_start_ns = min(target_start_ns, _t)

        # BR-V6: holiday-aware earliest / target (H1-H3)
        if HOLIDAYS:
            earliest_ns, target_start_ns = _holiday_targets(lid, earliest_ns, target_start_ns,
                                                            lft_ns_val, dur_ns)

        # PER-MACHINE CHANGEOVER + DURATION + SMART PICKER.
        # For each candidate machine compute:
        #   - per-machine duration (building MGs use building_cycle_times.csv override)
        #   - per-machine changeover (cutter model, item-type, etc.)
        #   - expected_finish = start + dur_m + co_ns_m
        # Pick by EARLIEST expected_finish (naturally favors fast + lightly-loaded).
        curr_item = lot_item.get(lid, "")
        curr_itype = lot_item_type.get(lid, "")
        curr_qty = float(lot_qty.get(lid, 0) or 0)
        TOLERANCE_NS = int(3600 * NS)
        best_machine = None
        best_start_ns = None
        best_co_ns = None
        best_dur_ns = None
        best_mm_consumed = 0.0
        best_will_swap = False
        best_score = None
        _cands = []
        # v53 RULE B AT PLACEMENT: a new run of a compound starts >= gap (15 h / 24 h) after any earlier or later
        # placed run of it on ANY mixer -- unless the lot JOINS a run (directly behind its end, same mixer), which
        # makes it the same run (bigger run, no extra changeover).
        _g_tgt, _g_join = target_start_ns, {}
        _ci47 = str(curr_item).strip()
        if _GAP_PLACE and _ci47 in _GAP_W:
            _W47 = _GAP_W[_ci47]
            _t0 = max(int(target_start_ns), int(earliest_ns) if earliest_ns != NEG_INF_NS else int(target_start_ns))
            for _rm, _rs, _re in _RUNS47.get(_ci47, ()):
                if _rs - _W47 < _t0 < _rs + _W47 or _rs <= _t0 <= _re:
                    _g_join[_rm] = max(_g_join.get(_rm, 0), _re)
            for _ in range(20):
                _hit = [r for r in _RUNS47.get(_ci47, ()) if r[1] - _W47 < _g_tgt < r[1] + _W47]
                if not _hit:
                    break
                _g_tgt = max(r[1] for r in _hit) + _W47
        for m in machines:
            _tm = _g_tgt if _GAP_PLACE and _ci47 in _GAP_W else target_start_ns
            _jr = _g_join.get(m)
            # Per-machine duration (Building MGs override from cycle_sec map)
            if m in machine_cycle_sec and curr_qty > 0:
                # cycle_sec is seconds per 1 NOS; duration = qty × cycle_sec
                dur_m_ns = int(curr_qty * machine_cycle_sec[m] * NS)
            else:
                dur_m_ns = dur_ns  # routing default
            if _jr is not None:                     # v53: try to join the run directly behind its end
                if NB_CO:
                    _sj, _cj = first_fit_nb(m, earliest_ns, _jr, dur_m_ns, curr_item, lot_dept.get(lid, ""), curr_itype)
                else:
                    _cj = 0
                    _sj = first_fit_jit(machine_busy[m], earliest_ns, _jr, dur_m_ns)
                if _sj <= _jr + 60 * NS:
                    _cands.append((m, _sj, _cj, dur_m_ns, 0.0, False, _sj + dur_m_ns))
                    continue
            # Per-machine changeover
            if m in PARENT_AWARE_CUTTERS and mm_per_cut_map:
                co_min_m, mm_cons, will_swap = compute_changeover_for_cutter(
                    m, machine_last_item.get(m, ""), curr_item, curr_qty,
                    mm_per_cut_map, cutter_loaded_parent, cutter_loaded_lot_id,
                    cutter_roll_remaining_mm, curr_lot_id=lid)
            elif NB_CO:
                # BR-V4: changeover against the REAL neighbours of the gap the lot lands in
                start, co_ns_m = first_fit_nb(m, earliest_ns, _tm, dur_m_ns,
                                              curr_item, lot_dept.get(lid, ""), curr_itype)
                mm_cons, will_swap = 0.0, False
                _cands.append((m, start, co_ns_m, dur_m_ns, mm_cons, will_swap, start + dur_m_ns))
                continue
            else:
                co_min_m = compute_changeover_min(
                    m, lot_dept.get(lid,""),
                    machine_last_item.get(m, ""), machine_last_itype.get(m, ""),
                    curr_item, curr_itype)
                mm_cons, will_swap = 0.0, False
            co_ns_m = int(co_min_m * 60 * NS)
            start = first_fit_jit(machine_busy[m], earliest_ns, _tm, dur_m_ns + co_ns_m)
            # SMART PICKER v2: score by expected_finish_ns (start + dur + co).
            # The 3-min vs 15-min changeover difference is ALREADY encoded in
            # expected_finish_ns via compute_changeover_for_cutter. So we don't
            # need a separate same_parent_bonus — including it as the PRIMARY
            # sort key was the bug that over-concentrated CPJ1218 cuts on WBC
            # (1,914 lots vs WBC NEW's 568 — 3.4x imbalance). Letting
            # expected_finish_ns decide naturally balances load across cutters
            # while still preferring same-parent (because the 12-min savings
            # makes the same-parent machine's expected_finish_ns earlier when
            # both machines have similar queue length).
            expected_finish_ns = start + dur_m_ns + co_ns_m
            _cands.append((m, start, co_ns_m, dur_m_ns, mm_cons, will_swap,
                           expected_finish_ns))
        # ── MACHINE CHOICE ──────────────────────────────────────────────────────
        if not _cands:
            return None
        _cday = None
        if _DAYMIX_ON and str(curr_itype).strip().upper() in ("MASTER COMPOUND", "FINAL COMPOUND"):
            # v49 rule A: a compound already on a mixer in that plant day stays on it that day
            _ci = str(curr_item).strip()
            _cday = lambda ns: (int(ns) - 7 * 3600 * 10 ** 9) // (24 * 3600 * 10 ** 9)
            _ok = [c for c in _cands if _DAYMIX.get((_ci, _cday(c[1])), c[0]) == c[0]]
            if _ok:
                _cands = _ok
        if BALANCE_ALL_MACHINES and BALANCE_ALL_TOL_NS > 0:
            # ═══ CTP DEVIATION #28 — EQUAL LOAD-BALANCING (flag-gated) ══════════════
            # Among machines that can finish WITHIN the tolerance of the earliest
            # achievable finish for this lot, run the LEAST-LOADED eligible machine so
            # no routing-eligible machine idles while a sibling saturates. Ties broken by
            # earliest finish, then machine-id (deterministic). `_cands` already honours
            # routing eligibility (machines) and material readiness (first_fit_jit floors
            # each start at _material_ready_ns), so this fabricates nothing and never
            # delays a lot more than the tolerance beyond its best-possible finish.
            _best_fin = min(c[6] for c in _cands)
            _pool = [c for c in _cands if c[6] - _best_fin <= BALANCE_ALL_TOL_NS]
            _chosen = min(_pool, key=lambda c: (machine_busy_ns[c[0]], c[6], str(c[0])))
        else:
            # SHIPPED SMART PICKER v2 (byte-identical): earliest expected_finish, busy_ns
            # exact-tie-break. min() is stable -> first machine (in `machines` order)
            # achieving the minimum wins, exactly as the original strict-`<` loop did.
            _chosen = min(_cands, key=lambda c: (c[6], machine_busy_ns[c[0]]))
        best_machine, best_start_ns, best_co_ns, best_dur_ns, best_mm_consumed, \
            best_will_swap, _best_fin_ns = _chosen
        co_ns = best_co_ns
        finish_ns = best_start_ns + best_dur_ns
        if _cday is not None:
            _DAYMIX.setdefault((str(curr_item).strip(), _cday(best_start_ns)), best_machine)
        if _GAP_PLACE and _ci47 in _GAP_W:                  # v53: register the run (extend when it joins one)
            _rl = _RUNS47[_ci47]
            for _r in _rl:
                if _r[0] == best_machine and abs(best_start_ns - _r[2]) <= 60 * NS:
                    _r[2] = max(_r[2], finish_ns)
                    break
            else:
                _rl.append([best_machine, int(best_start_ns), int(finish_ns)])
        # Update per-machine last-item AFTER placement
        machine_last_item[best_machine] = curr_item
        machine_last_itype[best_machine] = curr_itype
        # Update cutter mother-roll state
        if best_machine in PARENT_AWARE_CUTTERS and mm_per_cut_map:
            if best_will_swap:
                # Loaded a new mother roll
                cutter_loaded_parent[best_machine] = parent_of(curr_item)
                cutter_loaded_lot_id[best_machine] = lid
                cutter_roll_remaining_mm[best_machine] = max(0.0, MOTHER_ROLL_MAX_MM - best_mm_consumed)
            else:
                # Same roll, just consumed more MM
                cutter_roll_remaining_mm[best_machine] = max(0.0, cutter_roll_remaining_mm[best_machine] - best_mm_consumed)
        return (best_machine, best_start_ns, finish_ns, co_ns)

    def place_campaign(cid):
        c = campaigns[cid]
        wire = c["wire"]
        camp_lots = c["lots"]
        if not camp_lots:
            return False
        elig = None
        for l in camp_lots:
            m_set = set(lot_machines.get(l, []))
            elig = m_set if elig is None else (elig & m_set)
        if not elig:
            for l in camp_lots:
                infeas.append({"lot_id": l, "item": lot_item.get(l,""),
                               "reason": "no_common_machine_for_campaign", "campaign_id": cid})
            return False
        # ═══ CTP DEVIATION #17 — THE SCHEDULER WAS NOT REPRODUCIBLE ═══════════════════
        # (A) DEFECT. See the twin fix in place_building_campaigns() for the full write-up.
        # `elig` is a SET OF MACHINE-ID STRINGS. Python randomises string hashing per
        # process (PYTHONHASHSEED), so `for m in elig` visited machines in a DIFFERENT
        # ORDER ON EVERY RUN. The winner is chosen with a STRICT `<`, so on a score TIE the
        # FIRST machine visited wins — i.e. ties were broken BY THE HASH SEED.
        # Sorting makes the tie-break machine-id order. The SELECTION RULE (min score) is
        # untouched; only the previously-random tie-break becomes deterministic.
        elig = sorted(elig)
        dur_per_lot_ns = {}
        total_dur_ns = 0
        for l in camp_lots:
            d_ns = int(float(duration_h.get(l, 0)) * 3600 * NS)
            dur_per_lot_ns[l] = d_ns
            total_dur_ns += d_ns
        intra_co_ns = int(INTRA_CAMP_CHANGEOVER_MIN * 60 * NS)
        total_dur_ns += intra_co_ns * max(0, len(camp_lots) - 1)
        # CPM-driven: campaign anchored to the earliest LFT of its lots
        camp_target_finish_ns = None
        for l in camp_lots:
            lft = lft_map.get(l)
            if lft is not None and not pd.isna(lft):
                lns = pd.Timestamp(lft).value
                if camp_target_finish_ns is None or lns < camp_target_finish_ns:
                    camp_target_finish_ns = lns
        if camp_target_finish_ns is None:
            # fallback to wave anchor
            wa = None
            for l in camp_lots:
                wid = lot_wave_first.get(l, "Z99")
                a = _wave_anchor(wid)
                if wa is None or a < wa:
                    wa = a
            camp_target_finish_ns = (wa or T0_ns) + total_dur_ns
        target_start_ns = camp_target_finish_ns - total_dur_ns
        # FIX 3: Clamp campaign target start by lookback safety belt to prevent
        # campaigns from being anchored months in the past (e.g. WBC campaigns
        # were starting Jun 2025 because total_dur_ns was 7000+ hours).
        _safety_floor_ns = SCHEDULE_OPEN_NS - GLOBAL_MAX_LOOKBACK_NS
        if target_start_ns < _safety_floor_ns:
            target_start_ns = _safety_floor_ns
        # Material floor for the FRC campaign — no T0 floor (V6_wave change).
        # CTP DEVIATION #20: routed through the ONE quantity-summing gate. This path was
        # ALREADY taking a MAX over every predecessor (it never had the same-item MIN), so
        # it was the only one of the three placement paths that was not lying — but it was
        # still doing its own per-MHE drum arithmetic, which is an identity on CTP
        # (mhe_total==1 everywhere). One model, one function, no second copy to drift.
        min_start_ns = NEG_INF_NS
        for l in camp_lots:
            v = _material_ready_ns(l)
            if v > min_start_ns:
                min_start_ns = v
        best_machine = None
        best_start_ns = None
        best_score = None
        for m in elig:
            cur_earliest = min_start_ns
            if machine_last_camp_wire.get(m, "") and machine_last_camp_wire[m] != wire:
                cd_end = machine_last_camp_end_ns[m] + int(frc_cooldown_min * 60 * NS)
                if cd_end > cur_earliest:
                    cur_earliest = cd_end
            start = first_fit_jit(machine_busy[m], cur_earliest, target_start_ns, total_dur_ns)
            # FIX 2 (campaign path): same load-balance rule as main dispatcher.
            _TOL_NS = int(3600 * NS)
            _delta = abs(start - target_start_ns)
            score = (_delta // _TOL_NS, machine_busy_ns[m], _delta)
            if best_score is None or score < best_score:
                best_machine = m
                best_start_ns = start
                best_score = score
        if best_machine is None:
            for l in camp_lots:
                infeas.append({"lot_id": l, "item": lot_item.get(l,""),
                               "reason": "no_machine_slot_for_campaign", "campaign_id": cid})
            return False
        cur = best_start_ns
        for i, l in enumerate(camp_lots):
            s_ns = cur
            f_ns = cur + dur_per_lot_ns[l]
            schedule[l] = {
                "lot_id":   l,
                "item":     lot_item.get(l, ""),
                "machine":  best_machine,
                "operation": lot_op.get(l, ""),
                "department": lot_dept.get(l, ""),
                "equipment":  lot_eq.get(l, ""),
                "scheduled_start":  pd.Timestamp(s_ns),
                "scheduled_finish": pd.Timestamp(f_ns),
                "duration_h": round(float(duration_h.get(l, 0)), 3),
                "finish_ns": f_ns,
                "campaign_id": cid,
                "wire_type":   wire,
                "is_camp_first": (i == 0),
                "is_camp_last":  (i == len(camp_lots) - 1),
            }
            cur = f_ns + (intra_co_ns if i < len(camp_lots) - 1 else 0)
        insert_busy(best_machine, best_start_ns, cur)
        BUSY_ITEM[(best_machine, best_start_ns)] = (str(lot_item.get(camp_lots[0], "")),
                                                    str(lot_item.get(camp_lots[-1], "")))
        machine_last_camp_wire[best_machine] = wire
        machine_last_camp_end_ns[best_machine] = cur
        for l in camp_lots:
            s_l = schedule[l]["scheduled_start"].value
            for p in predecessors.get(l, ()):
                if p in schedule:
                    p_item = lot_item.get(p, "")
                    p_mn_ns, p_mx_ns = _ages_ns(p_item)
                    gap_ns = s_l - schedule[p]["finish_ns"]
                    if gap_ns < p_mn_ns:
                        aging_v.append({"producer_lot": p, "consumer_lot": l,
                                        "item": p_item, "gap_h": round(gap_ns/3600/NS,2),
                                        "min_aging_h": p_mn_ns/3600/NS, "type": "TOO_FRESH",
                                        "deficit_h": round((p_mn_ns - gap_ns)/3600/NS,2)})
                    elif gap_ns > p_mx_ns:
                        aging_v.append({"producer_lot": p, "consumer_lot": l,
                                        "item": p_item, "gap_h": round(gap_ns/3600/NS,2),
                                        "max_aging_h": p_mx_ns/3600/NS, "type": "EXPIRED",
                                        "excess_h": round((gap_ns - p_mx_ns)/3600/NS,2)})
        return True

    # ============================================================
    # BUILDING CAMPAIGN PLACEMENT (v2) — inch-locked, per-SKU campaigns
    # ------------------------------------------------------------
    # Green-Tyre / Carcass (dept "building") lots are NOT placed by the
    # per-lot JIT loop (which interleaves SKUs -> many changeovers, mixed
    # inches, tiny runs). Instead they are collected and placed here, AFTER
    # the main loop (so all component predecessors are already scheduled):
    #   - each building machine is LOCKED to a single inch (only formula/SKU
    #     changes within it);
    #   - lots are routed to an eligible same-inch machine (load-balanced);
    #   - per machine, lots are grouped by SKU into consecutive campaigns
    #     (A-A-A-B-B-C), ordered by urgency (earliest need first);
    #   - a flat 30-min changeover is inserted only between different SKUs
    #     (0 within a SKU's sub-lots);
    #   - start respects material-ready time (the ONE quantity-summing gate, CTP #20).
    # ============================================================
    # ═══ CTP DEVIATION #20 / E9 — `_bld_earliest_ns` DELETED (DEAD CODE) ══════════════
    # It was a THIRD verbatim copy of the pooled-MHE MIN-over-same-item model, and it was
    # NEVER CALLED — nothing in the file referenced it. Leaving a dead copy of the exact
    # bug being fixed is a landmine for whoever reads this next, so it is deleted rather
    # than commented. The live building floor is `kit_ready` below, which now calls
    # _material_ready_ns() like every other path.
    def _bld_dur_ns(lid, m):
        q = float(lot_qty.get(lid, 0) or 0)
        if m in machine_cycle_sec and q > 0:
            return int(q * machine_cycle_sec[m] * NS)
        return int(float(duration_h.get(lid, 0)) * 3600 * NS)

    def place_building_campaigns(blds):
        if not blds:
            return 0, 0
        CO_SAME = int(BUILDING_CO_SAME_MIN * 60 * NS)
        CO_DIFF = int(BUILDING_CO_DIFF_MIN * 60 * NS)
        DAY_NS = int(24 * 3600 * NS)
        # ═══ E9 — `comp_carts` DELETED (DEAD CODE) ═══════════════════════════════════
        # WAS: a per-component list of "first-drum cart-ready times", built by sweeping the
        # ENTIRE placed schedule, sorted, and then NEVER READ BY ANYTHING. Its comment
        # claimed it "lets a building lot consume the FRESHEST cart available at its start"
        # — a rule that DOES NOT EXIST in the executed code. It was also built on the same
        # inert MHE drum arithmetic. Confirmed dead by reference search; deleted.
        # Per-lot window:
        #   due       = curing deadline (Phase-4 LFT)  -> MUST finish by this.
        #   kit_ready = max over components (first cart ready + comp min_age).
        #   fresh_floor = due - (GT max_age - GT min_age): GT cannot be built
        #               earlier than its own shelf life before curing, else it
        #               expires WAITING for curing.
        #   earliest  = max(kit_ready, fresh_floor, schedule-open).
        BIG = pd.Timestamp("2100-01-01").value
        # CTP #32: per-lot start FLOOR written by the tail-spill repair's aging guard.
        # EMPTY unless building_tail_spill is ON -> the layout below is bit-identical.
        _bld_pin_floor = {}
        info = {}
        for lid in blds:
            sku = _lot_sku1.get(lid, "")
            # ═══ CTP DEVIATION #20 — THE FULL-KIT RULE WAS A HEADCOUNT, NOT A KIT ═════
            # WAS:  _ir = MIN(finish over that item's producers) + min_age
            # i.e. "each component TYPE has A finished lot ready" — ONE lot of each item,
            # whatever its size. It counted ITEM TYPES PRESENT and never once compared
            # QUANTITY. That is not a full-kit rule; it is a full-*checklist* rule. It is
            # the SAME pooled MIN as place_one_lot_jit's, and THIS is the copy that gates
            # every green tyre — all 44 measured incomplete-material starts were GT lots
            # let through by THIS line.
            # NOW: the ONE quantity-summing gate. A GT may not start until the CUMULATIVE
            # FINISHED-AND-AGED QUANTITY of every component covers what it consumes.
            # (A) DEFECT — see _material_ready_ns() for the full write-up and measurements.
            kit_ready = _material_ready_ns(lid)
            lft = lft_map.get(lid)
            due = pd.Timestamp(lft).value if (lft is not None and not pd.isna(lft)) else BIG
            gmin, gmax = _ages_ns(lot_item.get(lid, ""))
            # GT-freshness early floor applies ONLY with a real curing deadline.
            # Without an LFT (due == BIG sentinel) there is no early bound — never
            # push the lot toward the sentinel date.
            fresh_floor = (due - max(0, gmax - gmin)) if due < BIG else NEG_INF_NS
            _cand = [x for x in (kit_ready, fresh_floor, SCHEDULE_OPEN_NS) if x > NEG_INF_NS]
            earliest = max(_cand) if _cand else SCHEDULE_OPEN_NS
            # machine set (MG-consistent):
            # The BOM is exploded per machine-GROUP, so a lot's components are made
            # for its exploded MG (= its routing-eligible machines, which are
            # group-specific). The tbs2 GT machine may belong to a DIFFERENT group
            # than the components were produced for.
            #   GT (stage-2): use the tbs2 machine ONLY IF it's in this lot's MG
            #     group (i.e. present in its routing-eligible set). If the tbs2
            #     machine is a different group, DON'T use it (components wouldn't
            #     match) -> fall back to the lot's own MG machines.
            #   CARCASS (stage-1): not in tbs2 -> use its own (stage-1) MG machines.
            # tbs2 DISABLED: schedule building purely on MG-routing eligibility
            # (group/inch-specific) with load balancing — no single-machine pinning.
            machines = list(lot_machines.get(lid, []))
            info[lid] = {"sku": sku, "inch": _resolve_inch(sku),
                         "machines": machines, "tbs2": set(),
                         "earliest": earliest, "due": due,
                         "due_day": due // DAY_NS,
                         # SCHEDULING IDENTITY = the GT / carcass ITEM code, NOT the
                         # tyre SKU. A GT and its carcass share the SAME SKU code but
                         # are distinct products (GT… vs CAR…) on DISJOINT machine
                         # pools (stage-2 vs stage-1). Grouping by SKU merged them and
                         # collapsed eligibility; grouping by item code keeps them
                         # separate and on their own machines.
                         "code": lot_item.get(lid, "")}

        # ═══ CTP #35 (F2) — THE ONE LAYOUT KEY. Every queue sort in this function goes
        # through here, so the layout, _spill_sim, the EFT pre-sort and the CTB pre-pass
        # can never diverge (they were four literal copies of the same tuple).
        _LAY_BIG = BIG
        _fam_need = {}
        if BUILDING_ORDER_BY_NEED == "family":
            for _l in blds:
                _k = (info[_l]["due_day"], info[_l]["code"])
                _n = lot_last_need.get(_l)
                _n = _LAY_BIG if _n is None else _n
                if _k not in _fam_need or _n < _fam_need[_k]:
                    _fam_need[_k] = _n
        # ═══ CTP #37 — DRUM PRIORITY on the BUILDING LAYOUT key ════════════════════
        # THE CAMPAIGN GUARD. `_dcrit_code` is a pure function of info[l]["code"], so
        # (due_day, crit, code, ...) is a PERMUTATION OF WHOLE CODE GROUPS inside a
        # due_day — a campaign can never be split, which is how this avoids F2's
        # 370 -> 763 changeover blow-up. Changeover COUNT per machine is invariant under
        # the reordering itself; it can still move because the machine ASSIGNMENT
        # upstream (balance / takt / spill) consumes this same order. Changeovers are
        # reported every run — check them.
        _DRUM_ON_LAYOUT = (DRUM_PRIORITY != "off" and DRUM_LAYOUT)
        if _DRUM_ON_LAYOUT and BUILDING_TAIL_SPILL_RESEQ:
            # Rule D rewrites the layout key as a 4-tuple built from _sim_key positions
            # 0..2 (phase5:4128). Under CTP #37 the base key is a 5-tuple whose slot 2 is
            # the item CODE (a string), so `_kp[2] - n` is a TypeError. Fail loudly here
            # rather than let the two levers half-apply.
            raise SystemExit("CONFIG ERROR: drum_priority and building_tail_spill_reseq "
                             "(rule D) both ON — rule D's layout override assumes the "
                             "4-slot CTP #35 key. Turn one off.")
        if _DRUM_ON_LAYOUT:
            print(f"  LAYOUT KEY: (due_day, DRUM_CRIT, code, due, lot_id)  [CTP #37 "
                  f"scope={DRUM_PRIORITY}, per-CODE min = campaign-safe]")

        def _lay_key(l):
            _dc = _dcrit_code(info[l]["code"]) if _DRUM_ON_LAYOUT else None
            if BUILDING_ORDER_BY_NEED == "lot":
                _n = lot_last_need.get(l)
                if _DRUM_ON_LAYOUT:
                    return (info[l]["due_day"], _dc, _LAY_BIG if _n is None else _n,
                            info[l]["due"], info[l]["code"], l)
                return (info[l]["due_day"], _LAY_BIG if _n is None else _n,
                        info[l]["due"], info[l]["code"], l)
            if BUILDING_ORDER_BY_NEED == "family":
                _n = lot_last_need.get(l)
                _n = _LAY_BIG if _n is None else _n
                if _DRUM_ON_LAYOUT:
                    return (info[l]["due_day"], _dc,
                            _fam_need.get((info[l]["due_day"], info[l]["code"]), _LAY_BIG),
                            info[l]["code"], _n, info[l]["due"], l)
                return (info[l]["due_day"],
                        _fam_need.get((info[l]["due_day"], info[l]["code"]), _LAY_BIG),
                        info[l]["code"], _n, info[l]["due"], l)
            if _DRUM_ON_LAYOUT:
                return (info[l]["due_day"], _dc, info[l]["code"], info[l]["due"], l)
            return (info[l]["due_day"], info[l]["code"], info[l]["due"], l)

        # ═══ CTP DEVIATION #36 RULE D — the LAYOUT-ORDER OVERRIDE ══════════════════
        # `_ordk` is EMPTY unless rule D (building_tail_spill_reseq) accepts a campaign
        # promotion, so `_sim_key is _lay_key` in behaviour for every other run and the
        # OFF-equivalence is exact. Every queue sort that lays out a machine goes through
        # _sim_key so the repair, its simulator and the final layout can never diverge.
        _ordk = {}

        def _sim_key(l):
            _k = _ordk.get(l)
            return _lay_key(l) if _k is None else _k

        # ================== CTB (CLEAR-TO-BUILD) MATERIAL GATE ==================
        # Compute a per-lot material_ready_ns floor: the time when CUMULATIVE
        # component supply (opening WIP + finished production) first covers the
        # CUMULATIVE consumption through this lot. Consumption is swept in
        # earliest-order (the order lots want to build), per component, globally.
        material_ready = {}
        if BUILDING_CTB_GATE and blds:
            import numpy as _np
            # 1) per-tyre consumption rates from the Phase-1 demand explosion
            #    rate[(component_item, sku)] = demand_qty(comp) / demand_qty(GT)
            _rate = {}
            try:
                _dem = pd.read_csv(max([q for q in (OUTPUTS / "phase1_demand_updated.csv.gz",
                                                    OUTPUTS / "phase1_demand_updated.csv")
                                        if q.exists()], key=lambda q: q.stat().st_mtime))
                _dem["item_code"] = _dem["item_code"].astype(str).str.strip()
                _dem["sku"] = _dem["sku"].astype(str).str.strip()
                _dem["demand_qty"] = pd.to_numeric(_dem["demand_qty"], errors="coerce").fillna(0.0)
                _bld_codes = {info[l]["code"] for l in blds}
                _gtq = (_dem[_dem["item_code"].isin(_bld_codes)]
                        .groupby("sku")["demand_qty"].sum().to_dict())
                _csum = _dem.groupby(["sku", "item_code"])["demand_qty"].sum()
                for (sku_k, ic), q in _csum.items():
                    g = _gtq.get(sku_k, 0.0)
                    if g > 0:
                        _rate[(ic, sku_k)] = q / g
            except Exception as _e:
                print(f"  CTB: demand-rate load failed ({_e}); gate disabled")
                _rate = {}

            if _rate:
                # 2) opening WIP per component (length units normalised to MM)
                _wip = {}
                try:
                    _inv = pd.read_csv(INPUTS / cfg["files"]["inventory"], dtype=str)
                    _inv["inventory"] = pd.to_numeric(_inv["inventory"], errors="coerce").fillna(0.0)
                    _L2MM = {"M":1000.,"MTR":1000.,"METER":1000.,"METRE":1000.,"CM":10.,"MM":1.}
                    _f = _inv["unit"].astype(str).str.upper().str.strip().map(_L2MM).fillna(1.0)
                    _inv["_q"] = _inv["inventory"] * _f
                    _wip = _inv.groupby("itemcode")["_q"].sum().to_dict()
                except Exception as _e:
                    print(f"  CTB: WIP load failed ({_e}); using 0 opening stock")

                # 3) component set = direct predecessor items of building lots
                _comp_of = {}            # lid -> {component_item: consume_qty}
                _comps = set()
                for lid in blds:
                    sku = info[lid]["sku"]; q = float(lot_qty.get(lid, 0) or 0)
                    cc = {}
                    seen_items = set()
                    for p in predecessors.get(lid, ()):
                        ip = lot_item.get(p, "")
                        if ip in seen_items or ip in _bld_codes:
                            # skip already-seen and carcass/GT items that are ALSO
                            # built in this same layout (their finishes aren't in
                            # `schedule` yet, so their supply can't be gated here).
                            continue
                        seen_items.add(ip)
                        r = _rate.get((ip, sku))
                        if r and r > 0:
                            cc[ip] = q * r
                            _comps.add(ip)
                    _comp_of[lid] = cc

                # 4) per-component supply curve: sorted finish_ns + cumulative qty
                #    (WIP is available from the start of the horizon).
                _sup = {}
                _prod = defaultdict(list)
                for p, srec in schedule.items():
                    ip = lot_item.get(p, "")
                    if ip in _comps:
                        _prod[ip].append((srec["finish_ns"], float(lot_qty.get(p, 0) or 0)))
                for c in _comps:
                    evs = sorted(_prod.get(c, []))
                    fins = _np.array([SCHEDULE_OPEN_NS] + [e[0] for e in evs], dtype=_np.int64)
                    qs   = _np.array([_wip.get(c, 0.0)] + [e[1] for e in evs], dtype=_np.float64)
                    _sup[c] = (fins, _np.cumsum(qs))

                # 5) sweep building lots in earliest-order; accumulate consumption
                #    and find the supply-catch-up time per component.
                _consumed = defaultdict(float)
                # Sweep in the SAME order the per-machine layout will run lots
                # (curing-due order), so the cumulative-consumption frame matches
                # the actual chronological build order as closely as possible.
                _order = sorted(blds, key=_lay_key)          # CTP #35 (F2)
                _n_gated = 0
                for lid in _order:
                    mr = NEG_INF_NS
                    for c, qc in _comp_of[lid].items():
                        need = _consumed[c] + qc
                        _consumed[c] = need
                        fins, cum = _sup.get(c, (None, None))
                        if fins is None:
                            continue
                        # earliest index where cumulative supply >= need
                        idx = int(_np.searchsorted(cum, need, side="left"))
                        if idx >= len(fins):
                            t_ready = int(fins[-1])      # supply never fully covers -> last finish
                        else:
                            t_ready = int(fins[idx])
                        if t_ready > mr:
                            mr = t_ready
                    if mr > info[lid]["earliest"]:
                        material_ready[lid] = mr
                        _n_gated += 1
                print(f"  CTB GATE: {len(_comps)} components tracked | "
                      f"{_n_gated:,}/{len(blds):,} building lots material-gated later than full-kit")
        # ---- lot -> machine assignment ----
        # ═══ E9 — THIS COMMENT USED TO DESCRIBE A RULE THAT DOES NOT EXIST ═══════════
        # WAS: "Each SKU claims as many of its eligible machines as its volume genuinely
        #       needs (= ceil(hours / fair-share)), spreading its lots evenly so no machine
        #       saturates."
        # THERE IS NO SUCH SPREADER. Nothing computes ceil(hours / fair-share); nothing
        # spreads a SKU's lots evenly. WHAT ACTUALLY EXECUTES, below, is:
        #     LPT-ordered CAP-AND-SPILL FIRST-FIT-DECREASING PACKING.
        # Groups are sorted largest-first; a group opens ONE machine and dumps EVERY lot
        # it has onto it until that machine hits MACHINE_CAP_NS, then spills to the next.
        # FFD PACKS — IT DOES NOT BALANCE. That distinction is the whole subject of CTP
        # DEVIATION #19, and reading this comment as if the spreader were real is exactly
        # how the imbalance stayed invisible. The comment is corrected, the RULE IS
        # UNCHANGED.
        machine_inch, machine_load, machine_lots = {}, defaultdict(int), defaultdict(list)
        _all_m, _tot = set(), 0
        for _l in blds:
            _ms = info[_l]["machines"]
            if _ms:
                _tot += _bld_dur_ns(_l, _ms[0]); _all_m.update(_ms)
        # LOAD_CAP = the FAIR SHARE (total building work / eligible machines) in ns.
        # In v6 this was computed and NEVER READ. Balanced mode (CTP #19) is the first and
        # only thing that reads it. NOT DEAD — do not delete.
        LOAD_CAP = (_tot / max(len(_all_m), 1)) if _all_m else 0
        # ═══ E9 — `_pcap`, `machine_skus`, `sku_machines_used` DELETED (DEAD CODE) ════
        #   _pcap(m)          — a per-machine portfolio cap. DEFINED, NEVER CALLED. The
        #                       portfolio limit it returns is enforced NOWHERE.
        #   machine_skus      — WRITE-ONLY. Appended to on every _open(); read by nothing.
        #   sku_machines_used — WRITE-ONLY. Same.
        # All three were the residue of the fair-share spreader described (falsely) above.
        # Deleting them removes the last thing making that comment look true.
        # (BUILDING_FAST_MACHINES / BUILDING_FAST_CYCLE_SEC were read ONLY by _pcap and are
        #  now unreferenced too; they are kept as labelled-dead literals at their
        #  definition, since machine_cycle_sec — a DIFFERENT and very much live map — is
        #  what actually drives building durations.)

        # ---- aggregate lots into ITEM groups (GT / carcass CODE + inch) ----
        # Group by the GT/carcass item CODE (not the tyre SKU): GT and carcass share
        # a SKU code but are different products on disjoint machine pools.
        sku_group = defaultdict(lambda: {"lots": [], "hours_ns": 0, "inch": None,
                                         "elig": None, "no_mach": False})
        for lid in blds:
            elig = info[lid]["machines"]
            if not elig:
                infeas.append({"lot_id": lid, "item": lot_item.get(lid, ""),
                               "reason": "no_building_machine", "campaign_id": ""})
                continue
            s = info[lid]["code"]; inch = info[lid]["inch"]
            g = sku_group[(s, str(inch))]
            g["lots"].append(lid)
            g["hours_ns"] += _bld_dur_ns(lid, elig[0])
            g["inch"] = inch; g["sku"] = s
            # eligible set = intersection across the item's lots (all should match)
            g["elig"] = set(elig) if g["elig"] is None else (g["elig"] & set(elig))
        # items whose lots disagree on eligibility fall back to union (rare)
        for key, g in sku_group.items():
            if not g["elig"]:
                g["elig"] = set()
                for lid in g["lots"]:
                    g["elig"].update(info[lid]["machines"])

        # ====== HEIJUNKA DAILY-LEVELING LAYOUT (option b) ======
        # Level the build to the daily curing takt with high SKU diversity:
        # process day-by-day; each day, round-robin the ripe SKUs (curing due
        # within LEAD days, kit-ready) across eligible machines, building a small
        # MINI-lot mini-campaign per machine-visit. This spreads ~all SKUs being
        # cured across every day -> ~35-40 distinct SKUs/day and a level ~takt qty,
        # instead of front-loaded single-SKU campaigns that dip on trough days.
        if BUILDING_LEVEL:
            from collections import deque
            DAYn  = int(24 * 3600 * NS)
            LEADn = int(BUILDING_LEVEL_LEAD_DAYS * 24 * 3600 * NS)
            MINI  = max(1, int(BUILDING_LEVEL_MINI_CAMPAIGN))
            pend = defaultdict(list); all_m = set()
            for lid in blds:
                if not info[lid]["machines"]:
                    continue          # no-machine infeasibles already logged above
                pend[info[lid]["code"]].append(lid); all_m.update(info[lid]["machines"])
            for c in pend:
                pend[c].sort(key=lambda l: (info[l]["due"], info[l]["earliest"], l))
            pend = {c: deque(v) for c, v in pend.items()}
            mnext = {m: (machine_busy[m][-1][1] if machine_busy.get(m) else SCHEDULE_OPEN_NS)
                     for m in all_m}
            minch = {}; mlast = {}; mlast_inch = {}
            n_placed = n_co = n_late = 0
            _dues = [info[l]["due"] for l in blds if info[l]["due"] < BIG]
            horizon_end = (max(_dues) if _dues else SCHEDULE_OPEN_NS) + 25 * DAYn
            # TAKT: level daily build qty to the demand rate so we neither front-load
            # (which starves later trough days) nor JIT-starve (which tails out).
            _tot_q = sum(float(lot_qty.get(l, 0) or 0) for l in blds)
            _span_days = max(1.0, (horizon_end - 25 * DAYn - SCHEDULE_OPEN_NS) / DAYn)
            TAKT_CAP = (_tot_q / _span_days) * BUILDING_LEVEL_TAKT_HEADROOM

            def _inch_ok2(m, inch):
                if not BUILDING_INCH_LOCK:
                    return True
                return (m not in minch) or inch is None or minch[m] == inch

            def _emit(lid, m, code, inch):
                nonlocal n_placed, n_co, n_late
                start = max(mnext[m], info[lid]["earliest"])
                _mr = material_ready.get(lid)
                if _mr is not None and _mr > start:
                    start = _mr
                if mlast.get(m) is not None and mlast[m] != code:
                    _ia = mlast_inch.get(m); _ib = inch
                    diff = (_ia is not None and _ib is not None and _ia != _ib)
                    co_sm, co_dm = _bld_co.get(str(m), (BUILDING_CO_SAME_MIN, BUILDING_CO_DIFF_MIN))
                    start += int((co_dm if diff else co_sm) * 60 * NS); n_co += 1
                dur = _bld_dur_ns(lid, m); finish = start + dur
                if info[lid]["due"] < BIG and finish > info[lid]["due"]:
                    n_late += 1
                schedule[lid] = {
                    "lot_id": lid, "item": lot_item.get(lid, ""), "machine": m,
                    "operation": lot_op.get(lid, ""), "department": lot_dept.get(lid, ""),
                    "equipment": lot_eq.get(lid, ""),
                    "scheduled_start": pd.Timestamp(start),
                    "scheduled_finish": pd.Timestamp(finish),
                    "duration_h": duration_h.get(lid, 0.0),
                    "campaign_id": "", "wire_type": "",
                    "is_camp_first": False, "is_camp_last": False, "finish_ns": finish,
                }
                bisect.insort(machine_busy[m], (start, finish))
                machine_busy_ns[m] += (finish - start)
                mnext[m] = finish
                if inch is not None:
                    minch.setdefault(m, inch)
                mlast[m] = code; mlast_inch[m] = inch
                n_placed += 1

            day0 = SCHEDULE_OPEN_NS
            while any(pend[c] for c in pend) and day0 < horizon_end:
                de = day0 + DAYn
                day_qty = 0.0          # tyres built this plant-day (for takt leveling)
                progress = True
                while progress:
                    progress = False
                    ripe = []
                    for c, dq in pend.items():
                        if not dq:
                            continue
                        l0 = dq[0]
                        if info[l0]["earliest"] <= de and (info[l0]["due"] <= de + LEADn or info[l0]["due"] >= BIG):
                            ripe.append((info[l0]["due"], c))
                    if not ripe:
                        break
                    ripe.sort()
                    for _, c in ripe:
                        dq = pend[c]
                        if not dq:
                            continue
                        l0 = dq[0]
                        if info[l0]["earliest"] > de:
                            continue
                        if not (info[l0]["due"] <= de + LEADn or info[l0]["due"] >= BIG):
                            continue
                        # LEVELING: a lot is URGENT if its curing is due today/tomorrow
                        # (must build now) — always allowed. Build-AHEAD lots only fill
                        # up to the daily takt cap, so the day stays level and the rest
                        # spreads to later days (keeps every day diverse + ~takt).
                        _urgent = info[l0]["due"] <= de + DAYn
                        if (not _urgent) and day_qty >= TAKT_CAP:
                            continue
                        inch = info[l0]["inch"]
                        cands = [m for m in info[l0]["machines"] if mnext[m] < de and _inch_ok2(m, inch)]
                        if not cands:
                            continue
                        m = min(cands, key=lambda x: mnext[x])
                        built = 0
                        while dq and built < MINI:
                            lid = dq[0]
                            if info[lid]["earliest"] > de:
                                break
                            if not (info[lid]["due"] <= de + LEADn or info[lid]["due"] >= BIG):
                                break
                            if built > 0 and mnext[m] >= de:
                                break
                            _u = info[lid]["due"] <= de + DAYn
                            if (not _u) and day_qty >= TAKT_CAP:
                                break
                            _emit(lid, m, info[lid]["code"], inch)
                            day_qty += float(lot_qty.get(lid, 0) or 0)
                            dq.popleft(); built += 1; progress = True
                day0 = de
            # Leftover (couldn't fit within any day window) -> ASAP on least-loaded eligible.
            for c, dq in list(pend.items()):
                while dq:
                    lid = dq.popleft(); ms = info[lid]["machines"]
                    m = min(ms, key=lambda x: mnext.get(x, SCHEDULE_OPEN_NS))
                    _emit(lid, m, info[lid]["code"], info[lid]["inch"])
            print(f"  BUILDING (LEVELED): placed {n_placed:,} lots on {len(all_m)} machines | "
                  f"{n_co} changeovers | lead={BUILDING_LEVEL_LEAD_DAYS:.0f}d mini={MINI} | "
                  f"late-vs-curing: {n_late:,}")
            return n_placed, n_co

        # ---- place SKUs largest-first with CAP-AND-SPILL load balancing ----
        # Each machine is capped at ~one month of work (MACHINE_CAP_NS). While the
        # cap is reached and an ELIGIBLE sibling still has room, the SKU spills its
        # remaining lots to the least-loaded eligible machine instead of pushing one
        # machine past 100%. Changeovers are cheap (budget ~2,000+/month vs ~300 now),
        # so spreading aggressively for balance is the right trade-off: it levels each
        # routing group toward its average util and pulls the tail back into the month.
        MACHINE_CAP_NS = int(BUILDING_MONTH_CAP_H * 3600 * NS)
        # TWO-PHASE order:
        #   Phase A — SKUs eligible on exactly ONE machine. They have no choice, so
        #     they RESERVE that machine first; flexible SKUs then see the reserved
        #     load and avoid piling on top (which previously forced >100%).
        #   Phase B — all multi-eligible SKUs, largest-volume first (LPT) with
        #     cap-and-spill, balancing into whatever capacity Phase A left.
        # (Sorting ALL SKUs by ascending eligibility instead overloads machines that
        #  are the sole option for several 2-3-eligible SKUs, so we gate strictly on
        #  len==1 for the reservation phase.)
        #  ^^ MEASURED FALSE ON CTP — see CTP DEVIATION #19 at the top of this phase.
        #     Ascending eligibility is the fix, not the trap: peak 637.2 -> 521.9 h with
        #     FEWER changeovers (530 -> 511). Enable with balance_building_load: true.
        _locked = sorted([kv for kv in sku_group.items() if len(kv[1]["elig"]) == 1],
                         key=lambda kv: -kv[1]["hours_ns"])
        _flex   = sorted([kv for kv in sku_group.items() if len(kv[1]["elig"]) > 1],
                         key=lambda kv: -kv[1]["hours_ns"])
        # ═══ CTP DEVIATION #19 — BALANCED MODE. (B) POLICY, DEFAULT OFF ═════════════
        # Three changes, and NOTHING else in this loop moves:
        #   1. ORDER   — groups sorted MOST-CONSTRAINED-FIRST (fewest eligible machines,
        #                then largest), instead of largest-first. This is the one that
        #                does the work.
        #   2. CAP     — each machine capped at its FAIR SHARE (LOAD_CAP, the dead
        #                variable v6 computes and never reads) instead of the calendar
        #                month cap. BUILDING_MONTH_CAP_H IS IGNORED IN THIS MODE.
        #   3. SPILL   — when opening another machine, rank by LOAD first (inch demoted
        #                to a tie-break), the same key the over-cap fallback already uses.
        #                v6 ranks by (inch_penalty, load), and inch penalty DOMINATES:
        #                a machine already inch-locked by another group scores 4 vs 1 for
        #                a virgin machine, so a busy virgin machine beats an idle
        #                off-inch one. That is what strands 3409 (locked to inch 18 by a
        #                single 56.3 h group, then avoided by everything else).
        if BUILDING_BALANCE:
            MACHINE_CAP_NS = int(LOAD_CAP * BUILDING_BALANCE_SLACK)
            _locked = []
            _flex   = sorted(sku_group.items(),
                             key=lambda kv: (len(kv[1]["elig"]), -kv[1]["hours_ns"], kv[0]))
            print(f"  BUILDING BALANCE: ON — most-constrained-first, fair-share cap "
                  f"{MACHINE_CAP_NS/3600/NS:.1f} h (= total/{len(_all_m)} x "
                  f"{BUILDING_BALANCE_SLACK:.2f});  building_month_cap_h "
                  f"({BUILDING_MONTH_CAP_H:.1f} h) IS NOT USED in this mode")

        # ═══ CTP DEVIATION #23 — TIME-PHASED ASSIGNMENT: THE STATE ═══════════════════
        # `machine_load` (scalar, month) gains a TIME AXIS: bucket_load[(m, day)].
        # It does NOT replace machine_load — that stays live, so the month cap, the
        # balanced fair-share cap and every existing print keep working unchanged. The
        # day bucket is an ADDITIONAL gate, applied on top.
        TP_BUCKET_NS = max(1, int(BUILDING_TP_BUCKET_H * 3600 * NS))
        TP_CAP_NS    = int(BUILDING_TP_BUCKET_H * 3600 * NS * BUILDING_TP_SLACK)
        bucket_load  = defaultdict(int)          # (machine, day_index) -> ns charged

        def _tp_bucket(lid):
            """The DAY the press needs this green tyre. None when the lot has no curing
            deadline (due == BIG sentinel) — such lots keep the month-cap rule only."""
            d = info[lid]["due"]
            if d >= BIG:
                return None
            return (d - SCHEDULE_OPEN_NS) // TP_BUCKET_NS

        def _tp_fits(m, lid, b):
            return bucket_load[(m, b)] + _bld_dur_ns(lid, m) <= TP_CAP_NS

        if BUILDING_TIME_PHASED:
            # MOST-CONSTRAINED-FIRST is not optional here, it is the point: a sole-eligible
            # item must claim its machine-DAYS before flexible work can bury them. Same
            # order key as balanced mode, so the two compose cleanly.
            _locked = []
            _flex   = sorted(sku_group.items(),
                             key=lambda kv: (len(kv[1]["elig"]), -kv[1]["hours_ns"], kv[0]))
            print(f"  BUILDING TIME-PHASED: ON — bucket {BUILDING_TP_BUCKET_H:.0f} h, "
                  f"cap {TP_CAP_NS/3600/NS:.1f} h per machine-day "
                  f"(slack {BUILDING_TP_SLACK:.2f}), most-constrained-first"
                  + ("; composed with BUILDING BALANCE" if BUILDING_BALANCE else ""))

        # ═══ IDEA 1 — EARLIEST-FINISH-TIME SPREAD: THE ASSIGNMENT ════════════════════
        # Replaces the cap-and-spill FFD group loop below (which is SKIPPED when this is
        # ON — see the guarded iterable). Hands off exactly what that loop hands off:
        # machine_lots (+ machine_load). The per-machine layout further down is still the
        # ONLY thing that assigns start/finish, so every floor it enforces still applies.
        if BUILDING_SPREAD_EFT:
            _eft_m = set()
            for _l in blds:
                _eft_m.update(info[_l]["machines"])
            # One forward cursor + running code/inch per machine, seeded EXACTLY the way the
            # layout below seeds its own cursor — so the finish this pass predicts is the
            # finish the layout actually produces, not a proxy for it.
            m_cur  = {m: (machine_busy[m][-1][1] if machine_busy.get(m) else SCHEDULE_OPEN_NS)
                      for m in _eft_m}
            m_code = {m: None for m in _eft_m}
            m_inch = {m: None for m in _eft_m}
            STICK_NS = int(max(0.0, BUILDING_SPREAD_EFT_STICKY_MIN) * 60 * NS)

            def _eft_start(m, lid, code, inch, earliest, mr):
                """start/finish this lot would get on m — the LAYOUT'S OWN arithmetic:
                changeover onto the cursor, then floor at earliest (kit_ready / fresh_floor
                / schedule-open) and material_ready. Keep this identical to the layout."""
                c = m_cur[m]
                if m_code[m] is not None and m_code[m] != code:
                    _ia, _ib = m_inch[m], inch
                    diff = (_ia is not None and _ib is not None and _ia != _ib)
                    co_sm, co_dm = _bld_co.get(str(m), (BUILDING_CO_SAME_MIN,
                                                        BUILDING_CO_DIFF_MIN))
                    c += int((co_dm if diff else co_sm) * 60 * NS)
                dur = _bld_dur_ns(lid, m)
                if (GT_JIT_BUILD and info[lid]["due"] < BIG
                        and lot_item_type.get(lid, "") == "GREEN TYRES"):
                    _j = info[lid]["due"] - dur
                    if _j > earliest:
                        earliest = _j
                start = c if c > earliest else earliest
                if mr is not None and mr > start:
                    start = mr
                return start, start + dur, dur

            # Sweep in the SAME key order the per-machine layout sorts by. Every lot is
            # APPENDED to its chosen machine in this order, so each machine_lots[m] comes
            # out ALREADY sorted by that key -> the layout's re-sort is a no-op -> the
            # predicted finish IS the laid-out finish. If this key and the layout's ever
            # diverge, EFT degrades to an estimate — keep them identical.
            _order = sorted([l for l in blds if info[l]["machines"]],
                            key=_lay_key)                    # CTP #35 (F2)
            _n_eft = _n_stick = 0
            for lid in _order:
                code = info[lid]["code"]; inch = info[lid]["inch"]
                earliest = info[lid]["earliest"]; mr = material_ready.get(lid)
                b = _tp_bucket(lid) if BUILDING_TIME_PHASED else None
                cands = []
                for m in sorted(info[lid]["machines"]):      # ROUTING-ELIGIBLE ONLY
                    _st, _fin, _dur = _eft_start(m, lid, code, inch, earliest, mr)
                    # COMPOSED WITH TIME-PHASED: a machine that would overflow the lot's
                    # press-DAY bucket is DEMOTED, never hard-dropped — the same
                    # emergency-feasibility stance as the time-phased fallback below. A lot
                    # is never dropped and its aging window is never silently relaxed.
                    _over = 1 if (b is not None and
                                  bucket_load[(m, b)] + _dur > TP_CAP_NS) else 0
                    cands.append((_over, _fin, m, _dur))
                if not cands:
                    continue        # no-machine lots were already logged infeasible above
                cands.sort(key=lambda t: (t[0], t[1], str(t[2])))
                _best_over, _best_fin = cands[0][0], cands[0][1]
                # STICKY BAND — the anti-shred rule. Pure EFT would jump a lot to a machine
                # that finishes ONE NANOSECOND earlier even when the jump costs a
                # changeover. So among machines finishing within STICK_NS of the best,
                # prefer one ALREADY RUNNING THIS CODE (zero changeover), then least-loaded.
                # Never switch machines for a gain smaller than the changeover it costs.
                _pool = [t for t in cands
                         if t[0] == _best_over and t[1] <= _best_fin + STICK_NS]
                _pool.sort(key=lambda t: (0 if m_code[t[2]] == code else 1,
                                          machine_load[t[2]], str(t[2])))
                _over, _fin, m, _dur = _pool[0]
                if m != cands[0][2]:
                    _n_stick += 1
                _st, _fin, _dur = _eft_start(m, lid, code, inch, earliest, mr)
                m_cur[m] = _fin; m_code[m] = code; m_inch[m] = inch
                machine_lots[m].append(lid); machine_load[m] += _dur
                if inch is not None:
                    machine_inch.setdefault(m, inch)
                if b is not None:
                    bucket_load[(m, b)] += _dur
                _n_eft += 1
            print(f"  BUILDING SPREAD-EFT: ON — {_n_eft:,} lots by earliest-finish over "
                  f"{len(_eft_m)} routing-eligible machines | sticky band "
                  f"{BUILDING_SPREAD_EFT_STICKY_MIN:.0f} min kept {_n_stick:,} lots on "
                  f"their running campaign"
                  + ("; composed with TIME-PHASED day-bucket cap" if BUILDING_TIME_PHASED
                     else ""))

        # When BUILDING_SPREAD_EFT is ON this group loop is SKIPPED (EFT above already
        # produced machine_lots/machine_load). The loop is left UNTOUCHED rather than
        # deleted, so the flag is a true A/B: OFF = v6 cap-and-spill FFD, bit for bit.
        for key, g in ([] if BUILDING_SPREAD_EFT else (_locked + _flex)):
            # ═══ CTP DEVIATION #17 — THE SCHEDULER WAS NOT REPRODUCIBLE ═══════════════
            # WAS: elig = list(g["elig"])          # g["elig"] is a SET of machine-id STRINGS
            # CLASS: (A) DEFECT. Not a policy anyone chose — a rule the code never had.
            #
            # MEASURED, and it invalidates every A/B ever run on this file: the SAME code,
            # SAME inputs and SAME config, run twice in two fresh processes, produced
            #     expired edges 10,465 (47.9%)   and   11,197 (51.3%)
            # a 3.4-percentage-point swing. Pinning PYTHONHASHSEED=0 made it repeat exactly.
            #
            # WHY: Python randomises str hashing per process, so a SET of machine ids
            # iterates in a DIFFERENT ORDER EVERY RUN. `sorted(elig, key=_sel_key)` is a
            # STABLE sort, so whenever two machines tie on _sel_key = (inch_penalty, load)
            # — which they ALWAYS do at the start, when every load is 0 — the tie is broken
            # by the incoming list order, i.e. BY THE HASH SEED. The building line was being
            # laid out by a coin toss.
            #
            # THE REPAIR: sort the eligible set. The SELECTION RULE is UNCHANGED — still
            # (inch_penalty, then least load), still CAP-AND-SPILL / FFD packing. Only the
            # tie-break changes, from hash-random to machine-id order. Every list derived
            # from `elig` below (`cand`, `_pool`, the comprehensions) inherits that order,
            # so the whole pass becomes reproducible.
            #
            # !! WITHOUT THIS, NO MEASUREMENT ON THIS FILE MEANS ANYTHING: two runs of the
            #    same build differ by more than most of the fixes we are arguing about.
            s = g["sku"]; inch = g["inch"]; elig = sorted(g["elig"])
            def _inch_ok(m):
                if not BUILDING_INCH_LOCK:
                    return True
                # SOFT INCH (default): never hard-reject — _inch_penalty ranks instead,
                # so NO machine is permanently inch-fixed; an off-inch machine is used
                # only when better-matched machines are full, at a size-change penalty.
                if BUILDING_SOFT_INCH:
                    return True
                # legacy HARD gate (JK_SOFT_INCH=0): allowed-set is a wall.
                if (BUILDING_MACHINE_INCH and inch is not None
                        and str(m) in MACHINE_ALLOWED and inch not in MACHINE_ALLOWED[str(m)]):
                    return False
                return (m not in machine_inch) or inch is None or machine_inch[m] == inch
            # SOFT INCH penalty tiers (lower = cheaper). Released only at a cost:
            #   0  this is the machine's running/dominant inch, or an allowed inch
            #   1  flexible machine (no plant inch data)
            #   +3 machine already locked to a DIFFERENT inch (switch a busy machine)
            #   4  OFF-pattern inch on an inch-locked machine (size-change penalty)
            def _inch_penalty(x):
                if inch is None or not BUILDING_MACHINE_INCH:
                    return 0
                xs = str(x)
                base = 3 if (x in machine_inch and machine_inch[x] != inch
                             and MACHINE_DOM.get(xs) != inch) else 0
                al = MACHINE_ALLOWED.get(xs)
                if al is None:
                    tier = 1
                elif inch in al:
                    tier = 0
                else:
                    tier = 4
                return base + tier
            def _sel_key(x):
                # BALANCED (CTP #19): LOAD leads, inch is a tie-break — the same key the
                # over-cap fallback uses. v6: inch penalty leads and dominates load.
                if BUILDING_BALANCE:
                    return (machine_load[x], _inch_penalty(x), str(x))
                return (_inch_penalty(x), machine_load[x])
            chosen = []                       # machines this SKU runs on (grows on demand)
            def _open(m):
                if inch is not None:
                    machine_inch.setdefault(m, inch)
                # E9: the machine_skus / sku_machines_used bookkeeping that used to happen
                # here was WRITE-ONLY (never read by anything) and is deleted.
                chosen.append(m)
            # ═══ CTP DEVIATION #31 — per-code takt spread: min machines by DAILY pull ══
            # When OFF (_grp_spread stays False) every branch below behaves EXACTLY as v6:
            # _grp_load never gates anything and the selection key is unchanged. When ON,
            # a long-pole code PRE-OPENS its N_req least-loaded eligible machines and the
            # per-lot pick becomes group-load-first, so the code round-robins evenly across
            # exactly those machines (a press campaign of one code no longer piles on one
            # TBM). No group cap / relax logic is needed: the pre-open claims the machines
            # and the group-load-first key spreads across them.
            _grp_spread = False
            _grp_load   = defaultdict(int)
            if (BUILDING_TAKT_SPREAD and len(elig) > 1
                    and g["hours_ns"] >= BUILDING_TAKT_MIN_HOURS * 3600 * NS):
                # active days = distinct curing-DUE days for this code (spike-robust:
                # a code pulled in a narrow window gets more machines; a code smeared
                # across the month gets fewer). Lots with no due (BIG sentinel) ignored.
                _dd = {info[l]["due_day"] for l in g["lots"] if info[l]["due"] < BIG}
                _active_days = max(1, len(_dd))
                _daily_ns    = g["hours_ns"] / _active_days
                _pmd_ns      = BUILDING_TAKT_PMDH_H * BUILDING_TAKT_TARGET_UTIL * 3600 * NS
                _n_req = max(1, min(int(-(-_daily_ns // _pmd_ns)), len(elig)))  # ceil, clamp
                if _n_req > 1:
                    _grp_spread = True
                    # RESERVE: pre-open the N_req least-loaded eligible machines up front,
                    # so the code claims its share before its lots trickle onto one machine.
                    for _m in sorted(elig, key=_sel_key)[:_n_req]:
                        if _m not in chosen and _inch_ok(_m):
                            _open(_m)
            # distribute lots in due order; spill to a fresh eligible machine whenever
            # every currently-chosen machine is at the monthly cap.
            for lid in sorted(g["lots"], key=lambda l: (info[l]["due"], l)):
                # ═══ CTP DEVIATION #23 — TIME-PHASED ASSIGNMENT: THE RULE ═════════════
                # (C) ALGORITHM. Charge the lot to the DAY ITS PRESS NEEDS IT and refuse
                # any machine whose day is already full. Identical control flow to the
                # month-cap path below (prefer machines already open -> spill to a fresh
                # eligible one -> emergency exit), with `machine_load[m] < MACHINE_CAP_NS`
                # replaced by `_tp_fits(m, lid, b)` and the least-loaded tie-break taken
                # ON THE BUCKET rather than on the month total.
                #
                # CHANGEOVER CONTROL (this is what killed BUILDING_LEVEL, so it is
                # explicit): `avail` is still drawn from `chosen` FIRST — a group only
                # opens a new machine when every machine it ALREADY runs on is full FOR
                # THAT DAY. The campaign structure is preserved; we are not round-robining
                # SKUs across machines the way the takt-cap mode did. MEASURED cost below.
                if BUILDING_TIME_PHASED:
                    b = _tp_bucket(lid)
                    if b is None:
                        avail = [m for m in chosen if machine_load[m] < MACHINE_CAP_NS]
                    else:
                        avail = [m for m in chosen if _tp_fits(m, lid, b)]
                    if not avail:
                        _fresh = [m for m in elig if m not in chosen and _inch_ok(m)
                                  and (machine_load[m] < MACHINE_CAP_NS if b is None
                                       else _tp_fits(m, lid, b))]
                        cand = sorted(_fresh, key=_sel_key)
                        if cand:
                            _open(cand[0]); avail = [cand[0]]
                        else:
                            # EMERGENCY EXIT — no eligible machine has room on this DAY.
                            # Do NOT drop the lot and do NOT silently relax the window:
                            # give it to the eligible machine that is LEAST loaded on that
                            # day (load leads, inch is a tie-break — same logic and same
                            # reasoning as CTP #13's over-cap fallback). The lot will run
                            # late; the layout and the KPI report both surface it.
                            _pool = sorted(elig, key=lambda x: (
                                bucket_load[(x, b)] if b is not None else machine_load[x],
                                _inch_penalty(x), str(x)))
                            if _pool:
                                _best = _pool[0]
                                if _best not in chosen:
                                    _open(_best)
                                avail = [_best]
                            else:
                                avail = list(chosen)
                    if not avail:
                        infeas.append({"lot_id": lid, "item": lot_item.get(lid, ""),
                                       "reason": "no_building_machine", "campaign_id": ""})
                        continue
                    m = min(avail, key=lambda x: (
                        bucket_load[(x, b)] if b is not None else machine_load[x], str(x)))
                    machine_lots[m].append(lid)
                    _d = _bld_dur_ns(lid, m)
                    machine_load[m] += _d
                    if b is not None:
                        bucket_load[(m, b)] += _d
                    continue
                avail = [m for m in chosen if machine_load[m] < MACHINE_CAP_NS]
                if not avail:
                    # spill: dominant-inch-preferred, allowed, under cap, not yet used
                    cand = sorted([m for m in elig if m not in chosen and _inch_ok(m)
                                   and machine_load[m] < MACHINE_CAP_NS],
                                  key=_sel_key)
                    if cand:
                        _open(cand[0]); avail = [cand[0]]
                    else:
                        # ═══ CTP DEVIATION #13 — THE DEAD FALLBACK ════════════════════
                        # WAS:  avail = chosen or sorted(elig, key=_sel_key)[:1]
                        #       if not chosen and avail:
                        #           _open(avail[0])
                        # CLASS: (A) DEFECT — STATED-AND-BROKEN. The comment on this very
                        #   branch declares the rule "no allowed machine has room ->
                        #   least-loaded ELIGIBLE OVERALL". It never ran. `chosen` is
                        #   TRUTHY for every lot after this SKU's first, so the `or` short-
                        #   circuits and `sorted(elig, ...)` is UNREACHABLE. What actually
                        #   executed was "least-loaded of the machines this SKU has ALREADY
                        #   opened" — a strictly smaller pool that IGNORES the cap and
                        #   IGNORES every other eligible machine, however idle it is. The
                        #   rule the code claims to follow was never once executed.
                        # THE REPAIR: execute the stated rule — "least-LOADED eligible
                        #   OVERALL", ranking the WHOLE eligible set by LOAD FIRST, with the
                        #   inch penalty demoted to a TIE-BREAK ("prefers allowed+dominant
                        #   WHERE POSSIBLE" — the parenthetical, not the rule).
                        #
                        # !! THE SORT KEY MATTERS, AND IT IS NOT _sel_key. MEASURED, both ways:
                        #                                 cap=669.6      cap=507
                        #      _sel_key (penalty first)     707.9 h      790.9 h   <- WORSE
                        #      load first (this code)       690.4 h      653.9 h   <- the rule
                        #   Ranking by _sel_key = (inch_penalty, load) makes inch DOMINATE, so
                        #   the overflow of a given size piles onto its lowest-penalty machine
                        #   NO MATTER HOW FULL IT ALREADY IS — and LOWERING the cap then makes
                        #   the peak WORSE, because it only forces MORE lots down this path.
                        #   That is the opposite of what a feasibility fallback is for.
                        #
                        # WHY THE TWO PATHS USE DIFFERENT KEYS (deliberate, not an oversight):
                        #   SPILL path above  — a machine still HAS ROOM. You can afford to be
                        #     picky, so inch preference leads: _sel_key = (penalty, load).
                        #   THIS path         — NOTHING has room. This is the emergency exit.
                        #     Load is the emergency; inch is a nicety. Load leads.
                        #
                        # NOTHING about the CAP-AND-SPILL / FFD packing rule changes. This is
                        # only the over-cap escape hatch that had never once executed.
                        # WHAT IT BUYS: at v6's cap (669.6) it is nearly neutral (707.9 ->
                        #   690.4 h) — the cap barely binds, exactly as expected. Its VALUE is
                        #   that it makes BUILDING_MONTH_CAP_H (CTP #12) a WORKING LEVER:
                        #   cap=507 now LEVELS the line to 653.9 h instead of blowing it out
                        #   to 790.9 h.
                        _pool = sorted(elig, key=lambda x: (machine_load[x],
                                                            _inch_penalty(x), str(x)))
                        if _pool:
                            _best = _pool[0]
                            if _best not in chosen:
                                _open(_best)
                            avail = [_best]
                        else:
                            avail = list(chosen)
                if not avail:
                    infeas.append({"lot_id": lid, "item": lot_item.get(lid, ""),
                                   "reason": "no_building_machine", "campaign_id": ""})
                    continue
                # CTP #31: group-load-first spreads a long-pole code evenly across its
                # pre-opened machines; OFF -> the exact v6 least-loaded pick.
                if _grp_spread:
                    m = min(avail, key=lambda x: (_grp_load[x], machine_load[x], str(x)))
                else:
                    m = min(avail, key=lambda x: machine_load[x])
                machine_lots[m].append(lid)
                _d = _bld_dur_ns(lid, m)
                machine_load[m] += _d
                _grp_load[m] += _d

        # ═══ CTP DEVIATION #32 — LATENESS-GATED TAIL SPILL (C) ALGORITHM ═════════════
        # DEFAULT OFF (`building_tail_spill: false`) — nothing below runs unless enabled.
        #
        # THE DEFECT IT REPAIRS (measured on the 2026-06 CTP PCR plan, baseline 34.815 d):
        #   Every gate that decides how many TBMs a code gets is a MONTH TOTAL with NO
        #   TIME AXIS:
        #     :2570  MACHINE_CAP_NS = LOAD_CAP * 1.06 = 608.3 h   (month)
        #     :2864  avail = [m for m in chosen if machine_load[m] < MACHINE_CAP_NS]
        #     :2932  m = min(avail, key=machine_load)              (month)
        #   TBM 3406 ended the month at 460.4 h — 147.9 h UNDER the cap — so the spill at
        #   :2865 never fired once, and GT1564 NEO (7 eligible TBMs) and GT1824 XPC1
        #   (3 eligible) both ran their whole volume on 3406 alone. 3406's month total is
        #   LOW because it is idle EARLY (10.21 d of its 11.37 d idle is before day 20);
        #   from day 24 on it is back-to-back with SIX codes' tail work and finishes day
        #   32.12, LAST of 11, while 3405 finishes day 27.82 with 242 idle hours. Press
        #   4836 starves on it and sets the 34.815 d makespan.
        #
        # WHY NOT JUST SPREAD (this is the important part):
        #   Naive earliest-finish spreading over all eligible TBMs (building_spread_eft)
        #   was MEASURED at +16.4 pp of over-24 h green-tyre age. Reason: it pulls ALL 911
        #   GT lots to their `earliest` floor, which for a GT is fresh_floor = LFT - 48 h,
        #   so ~every tyre is built the full shelf life ahead of its press and waits.
        #   CORRECTNESS BEATS MAKESPAN — that trade is not acceptable.
        #   The phase-4 LFT is ALSO unusable as the gate: 872 of 911 GT lots already have
        #   earliest > due (phase2 double-allocation, see MEMORY), and phase5's own log
        #   prints `late-vs-curing: 911`. Gating on "finish > LFT" fires for EVERYTHING and
        #   degenerates straight back into the naive spread.
        #
        # THE RULE THAT IS ACTUALLY IMPLEMENTED — three gates, all of which must pass:
        #   1. TAIL GATE      — only lots finishing within TAIL_H of the BUILDING makespan
        #                       are candidates. This is a makespan repair, not a balancer.
        #   2. LATENESS GATE  — only lots whose start exceeds their OWN ready time
        #                       (`earliest` = max(kit_ready, fresh_floor, T0), plus
        #                       material_ready) by more than TOL_H. A lot waiting on
        #                       MATERIAL is never moved: moving it cannot help.
        #   3. MAKESPAN GATE  — the move is accepted only if it strictly cuts
        #                       max(end(donor), end(receiver)) by >= MIN_GAIN_H, i.e. the
        #                       receiver really is idle at that instant. Ties broken on
        #                       machine id, so the pass is deterministic.
        #   AGING GUARD (`..._pin_starts`, default ON): every NON-moved lot carries a
        #   floor equal to its pre-repair start, so removing work from the donor can never
        #   pull another tyre EARLIER than the baseline already built it. Only the
        #   deliberately-moved tail lots change their build time, and even they are still
        #   floored at `earliest` — no tyre is ever built before its freshness floor.
        #
        # EXACTNESS: _spill_sim() is a line-for-line replica of the per-machine layout
        # immediately below (same sort key, same changeover, same JIT floor, same
        # material_ready floor). The layout is PER-MACHINE INDEPENDENT — no cross-machine
        # state — so moving one lot between two machines changes exactly those two
        # timelines and the simulation is not an estimate. Verify with
        # `building_tail_spill_rounds: 0` -> byte-identical to the OFF run.
        if BUILDING_TAIL_SPILL and machine_lots:
            _TOL_NS   = int(BUILDING_TAIL_SPILL_TOL_H * 3600 * NS)
            _TAIL_NS  = int(BUILDING_TAIL_SPILL_TAIL_H * 3600 * NS)
            _GAIN_NS  = int(BUILDING_TAIL_SPILL_MIN_GAIN_H * 3600 * NS)

            def _spill_sim(m, lots_list, pin):
                """EXACT replica of the per-machine layout below. Returns
                ({lid: (start, finish)}, machine_end_cursor)."""
                order = sorted(lots_list, key=_sim_key)      # CTP #35 (F2) + #36 rule D
                cursor = machine_busy[m][-1][1] if machine_busy.get(m) else SCHEDULE_OPEN_NS
                last_sku = None; _last = None; out = {}
                for lid in order:
                    s = info[lid]["code"]
                    if last_sku is not None and s != last_sku:
                        _ia = info[_last]["inch"]; _ib = info[lid]["inch"]
                        diff_inch = (_ia is not None and _ib is not None and _ia != _ib)
                        co_sm, co_dm = _bld_co.get(str(m), (BUILDING_CO_SAME_MIN,
                                                            BUILDING_CO_DIFF_MIN))
                        cursor += int((co_dm if diff_inch else co_sm) * 60 * NS)
                    dur = _bld_dur_ns(lid, m)
                    _e = info[lid]["earliest"]
                    if (GT_JIT_BUILD and info[lid]["due"] < BIG
                            and lot_item_type.get(lid, "") == "GREEN TYRES"):
                        _jf = info[lid]["due"] - dur
                        if _jf > _e:
                            _e = _jf
                    _p = pin.get(lid)
                    if _p is not None and _p > _e:
                        _e = _p
                    start = cursor if cursor > _e else _e
                    _mr = material_ready.get(lid)
                    if _mr is not None and _mr > start:
                        start = _mr
                    finish = start + dur
                    out[lid] = (start, finish)
                    cursor = finish; last_sku = s; _last = lid
                return out, cursor

            def _need_floor(_l, _m2):
                """THE ROPE FLOOR — used by BOTH rules. A green tyre may not be built
                EARLIER than the drum needs it: finish >= last_need => start >=
                last_need - dur. Returns None when the lot has no last_need (then only the
                aging cap bounds the pull). THIS IS THE WHOLE DIFFERENCE between a rope and
                a spread: the receiver can be free for days and the lot still lands at the
                press's own need time. MEASURED — with the floor OFF the repair costs
                +0.22 pp over-24 h; with it ON the SAME 33.35 d schedule comes out at
                -0.60 pp over-24 h and -0.24 pp over-48 h."""
                if not BUILDING_TAIL_SPILL_USE_NEED:
                    return None
                _n = lot_last_need.get(_l)
                if _n is None:
                    return None
                return _n - _bld_dur_ns(_l, _m2)

            # CTP #36 G2 — shared by rule B and rule D (rule D lives outside the rule-B
            # block, so this must be hoisted here or it is a NameError when
            # building_tail_spill_pull_forward is off).
            _HARM_NEED = (BUILDING_TAIL_SPILL_HARM_MODE == "last_need")

            _pin = {}
            _sim0 = {m: _spill_sim(m, machine_lots[m], _pin) for m in list(machine_lots)}
            _ms0 = max((v[1] for v in _sim0.values()), default=SCHEDULE_OPEN_NS)
            if BUILDING_TAIL_SPILL_PIN:
                for _m, (_res, _end) in _sim0.items():
                    for _l, (_st, _fi) in _res.items():
                        _pin[_l] = _st
            _sims = {m: _spill_sim(m, machine_lots[m], _pin) for m in list(machine_lots)}
            _ends = {m: _sims[m][1] for m in _sims}
            _lot_m = {l: m for m, ls in machine_lots.items() for l in ls}
            _moves = []
            for _rnd in range(int(BUILDING_TAIL_SPILL_ROUNDS)
                              if BUILDING_TAIL_SPILL_RULE_A else 0):
                _MS = max(_ends.values()) if _ends else SCHEDULE_OPEN_NS
                _cands = []
                for _m, (_res, _end) in _sims.items():
                    for _l, (_st, _fi) in _res.items():
                        if _fi < _MS - _TAIL_NS:
                            continue
                        if len(info[_l]["machines"]) < 2:
                            continue
                        _rdy = info[_l]["earliest"]
                        _mr = material_ready.get(_l)
                        if _mr is not None and _mr > _rdy:
                            _rdy = _mr
                        if _st - _rdy <= _TOL_NS:
                            continue          # waiting on MATERIAL, not on the machine
                        _cands.append((-_fi, str(_l), _l, _m, _st, _rdy))
                _cands.sort()
                _n_moved = 0
                for _negfi, _ls, _l, _m, _st, _rdy in _cands:
                    if _lot_m.get(_l) != _m:
                        continue                       # already relocated this round
                    _old_pair_base = _ends[_m]
                    _best = None
                    # The candidate's OWN pin must come off BEFORE the trial: the pin is
                    # the aging guard for lots we are NOT moving, and leaving it on would
                    # hold the lot at its old (late) start on every receiver, so no move
                    # could ever show a gain. Restored if no receiver is accepted.
                    _own_pin = _pin.pop(_l, None)
                    for _m2 in sorted(info[_l]["machines"]):
                        if _m2 == _m or _m2 not in _ends:
                            # a routing-eligible machine this layout never opened is still
                            # a legal target: seed it from its own busy list.
                            if _m2 == _m:
                                continue
                            _ends.setdefault(_m2, machine_busy[_m2][-1][1]
                                             if machine_busy.get(_m2) else SCHEDULE_OPEN_NS)
                            _sims.setdefault(_m2, ({}, _ends[_m2]))
                        _oldp = _old_pair_base if _ends[_m2] < _old_pair_base else _ends[_m2]
                        _A = [x for x in machine_lots[_m] if x != _l]
                        _B = list(machine_lots.get(_m2, [])) + [_l]
                        # ROPE: trial the move WITH the need floor already applied, so a
                        # receiver is only credited with a gain it can deliver without
                        # building the tyre before the press needs it.
                        _nfA = _need_floor(_l, _m2)
                        if _nfA is not None:
                            _pin[_l] = _nfA
                        _rA, _eA = _spill_sim(_m, _A, _pin)
                        _rB, _eB = _spill_sim(_m2, _B, _pin)
                        if _nfA is not None:
                            _pin.pop(_l, None)
                        _newp = _eA if _eA > _eB else _eB
                        if _newp > _oldp - _GAIN_NS:
                            continue                   # receiver is not genuinely free
                        _key = (_newp, str(_m2))
                        if _best is None or _key < _best[0]:
                            _best = (_key, _m2, _A, _B, _rA, _eA, _rB, _eB, _oldp)
                    if _best is None:
                        if _own_pin is not None:
                            _pin[_l] = _own_pin      # not moved -> aging guard back on
                        continue
                    _key, _m2, _A, _B, _rA, _eA, _rB, _eB, _oldp = _best
                    # ── JIT LANDING (the aging half of the rule) ────────────────────
                    # The move is justified by the DONOR's tail, not by building this
                    # tyre early. Left alone, the receiver would start it as soon as it
                    # is free — MEASURED: that is where V1's +0.75 pp of over-24 h age
                    # came from (press 4836 got tyres 40-54 h old instead of 0-14 h).
                    # So: push the moved lot AS LATE AS THE RECEIVER ALLOWS while the
                    # pair-end stays at the value that justified the move. Pure gain —
                    # same makespan, fresher tyre. Never later than its old start.
                    if BUILDING_TAIL_SPILL_JIT and _rB.get(_l) is not None:
                        _lo = _rB[_l][0]
                        _hi = _st
                        if _hi > _lo:
                            _ceil = _key[0]
                            _bestp = None
                            for _ in range(26):
                                _mid = (_lo + _hi) // 2
                                if _mid <= _lo:
                                    break
                                _pin[_l] = _mid
                                _tr, _te = _spill_sim(_m2, _B, _pin)
                                if _te <= _ceil:
                                    _bestp = (_mid, _tr, _te); _lo = _mid
                                else:
                                    _hi = _mid
                            _pin.pop(_l, None)
                            if _bestp is not None:
                                _pin[_l] = _bestp[0]
                                _rB, _eB = _bestp[1], _bestp[2]
                    machine_lots[_m] = _A
                    machine_lots[_m2] = _B
                    _sims[_m] = (_rA, _eA); _sims[_m2] = (_rB, _eB)
                    _ends[_m] = _eA; _ends[_m2] = _eB
                    _lot_m[_l] = _m2
                    # NOTE: the moved lot's pin was already removed at the top of this
                    # candidate (so the trial could see a real gain) and, when JIT LANDING
                    # is ON, RE-SET above to its as-late-as-possible start. Do NOT pop it
                    # again here — that was the V2 defect: it silently discarded the JIT
                    # landing and the moved tyre reverted to as-early-as-possible.
                    machine_load[_m] -= _bld_dur_ns(_l, _m)
                    machine_load[_m2] += _bld_dur_ns(_l, _m2)
                    if info[_l]["inch"] is not None:
                        machine_inch.setdefault(_m2, info[_l]["inch"])
                    _moves.append({"round": _rnd, "lot_id": _l, "item": info[_l]["code"],
                                   "from_machine": _m, "to_machine": _m2,
                                   "old_start": str(pd.Timestamp(_st)),
                                   "new_start": str(pd.Timestamp(_rB[_l][0])),
                                   "ready": str(pd.Timestamp(_rdy)),
                                   "slip_before_h": round((_st - _rdy) / 3600 / NS, 2),
                                   "slip_after_h": round((_rB[_l][0] - _rdy) / 3600 / NS, 2),
                                   "pair_end_before_d": round((_oldp - SCHEDULE_OPEN_NS)
                                                              / 86400 / NS, 3),
                                   "pair_end_after_d": round((_key[0] - SCHEDULE_OPEN_NS)
                                                             / 86400 / NS, 3)})
                    _n_moved += 1
                if _n_moved == 0:
                    break

            # ═══ RULE B — IDLE-ABSORPTION PULL-FORWARD ═══════════════════════════════
            # RULE A above only fires when a move shortens a MACHINE's end, so it cannot
            # touch a MID-QUEUE lot. MEASURED, and this is the bigger half of the problem:
            # GT1824 XPC1_L0006 is kit-ready on day 24.98 (material_ready is EMPTY for all
            # 911 GT lots — the wait is 100% machine queue) and 3406 does not start it
            # until day 28.30 — 103.6 h of pure queue behind five other codes. Press 4836
            # idles 1.739 d (day 26.62 -> 28.36) waiting for exactly that tyre. Rule A
            # rejects the move because 3406's END does not change.
            #
            # RULE B moves such a lot ONLY into GENUINE IDLE on an eligible machine:
            #   (i)   it must start at least ..._pull_min_gain_h EARLIER than it does now;
            #   (ii)  the receiver's end may not exceed max(its own end, the donor's end)
            #         — the building makespan can never grow;
            #   (iii) NO other lot on the receiver may finish later than it does now
            #         (tolerance ..._pull_harm_h, default 0). This IS the "machine is idle
            #         at that instant" test: with pin-starts ON every other lot carries a
            #         floor at its current start, so the insert only passes when there is a
            #         real idle window of >= dur + changeover. Nothing is pushed aside.
            #   (iv)  JIT landing again: among the machines that pass, take the one whose
            #         LATEST admissible start is latest, and land it there. The lot is
            #         pulled forward by the MINIMUM that satisfies (i) — never "as early as
            #         possible", which is what makes this different from a naive spread.
            # So every rule-B move is Pareto on the building line: one lot earlier, zero
            # lots later, machine end unchanged or better.
            if BUILDING_TAIL_SPILL_PULL:
                _PTOL  = int(BUILDING_TAIL_SPILL_PULL_TOL_H * 3600 * NS)
                _PGAIN = int(BUILDING_TAIL_SPILL_PULL_GAIN_H * 3600 * NS)
                _PHARM = int(BUILDING_TAIL_SPILL_PULL_HARM_H * 3600 * NS)
                _PMAX  = int(BUILDING_TAIL_SPILL_PULL_MAX_H * 3600 * NS)
                # ── CTP #36 G1: the two admission gates, independently settable. Both
                # default to _PTOL, which is literally the constant they replaced. ──
                _PQUEUE = int(BUILDING_TAIL_SPILL_PULL_QUEUE_H * 3600 * NS)
                _PDRUM  = int(BUILDING_TAIL_SPILL_PULL_DRUM_H * 3600 * NS)
                # ── audit: why every candidate/target was refused (instrumentation only) ──
                _why = [""]
                _dbg_rows = [] if BUILDING_TAIL_SPILL_DEBUG else None

                def _pullB_ok(_l, _m, _m2, _st, _pin_val):
                    """Trial-place _l on _m2 with an optional explicit start floor.
                    Returns (ok, new_start, A, B, rA, eA, rB, eB).
                    Sets _why[0] to the gate that refused (audit only, no behaviour)."""
                    _A = [x for x in machine_lots[_m] if x != _l]
                    _B = list(machine_lots.get(_m2, [])) + [_l]
                    _save = _pin.pop(_l, None)
                    if _pin_val is not None:
                        _pin[_l] = _pin_val
                    try:
                        _rA, _eA = _spill_sim(_m, _A, _pin)
                        _rB, _eB = _spill_sim(_m2, _B, _pin)
                    finally:
                        _pin.pop(_l, None)
                        if _save is not None:
                            _pin[_l] = _save
                    if _eB > (_ends[_m2] if _ends[_m2] > _ends[_m] else _ends[_m]):
                        _why[0] = "ii_receiver_end_grows"
                        return (False, 0, None, None, None, 0, None, 0)          # (ii)
                    _ns = _rB[_l][0]
                    if _ns > _st - _PGAIN:
                        _why[0] = "i_no_gain"
                        return (False, 0, None, None, None, 0, None, 0)          # (i)
                    # (v) AGING CAP. Pulling a green tyre forward buys makespan and COSTS
                    # shelf life. MEASURED on the uncapped run: the whole over-48 h
                    # regression (+0.38 pp, 1,422 tyres) came from four big pulls —
                    # GT 1634 XPC TATA (78 h and 64 h earlier), GT 1894 MAX (55 h),
                    # GT 1482 UHL (41 h) — while the move that actually bought the
                    # makespan, GT1824 XPC1_L0006, only needed 38 h. So cap it.
                    if _PMAX > 0 and _ns < _st - _PMAX:
                        _why[0] = "v_aging_cap"
                        return (False, 0, None, None, None, 0, None, 0)          # (v)
                    _nf = _need_floor(_l, _m2)
                    if _nf is not None and _ns < _nf:
                        _why[0] = "vi_rope"
                        return (False, 0, None, None, None, 0, None, 0)          # (vi) rope
                    # ── (iii) DISPLACEMENT GATE ─────────────────────────────────────
                    # STRICT (CTP #32): no lot already on the receiver may finish even a
                    # tolerance later than it does now. With pin_starts ON that is exactly
                    # "there is a real idle window of >= dur + changeover here".
                    # LAST_NEED (CTP #36 G2): a displaced lot may finish later ONLY IF it
                    # still finishes inside its OWN last_need — i.e. we spend a neighbour's
                    # DRUM SLACK, never its due date. A lot with no last_need stays strict.
                    # This is still a rope: nothing is pushed past its press's need time,
                    # and building a tyre LATER makes it FRESHER, not older.
                    _b4 = _sims[_m2][0]
                    for _j, (_js, _jf) in _rB.items():
                        if _j == _l:
                            continue
                        _o = _b4.get(_j)
                        if _o is None:
                            _why[0] = "iii_unknown_lot"
                            return (False, 0, None, None, None, 0, None, 0)      # (iii)
                        if _jf > _o[1] + _PHARM:
                            if not _HARM_NEED:
                                _why[0] = "iii_displaces"
                                return (False, 0, None, None, None, 0, None, 0)  # (iii)
                            _jn = lot_last_need.get(_j)
                            if _jn is None or _jf > _jn:
                                _why[0] = "iii_displaces_past_need"
                                return (False, 0, None, None, None, 0, None, 0)  # (iii')
                    _why[0] = "OK"
                    return (True, _ns, _A, _B, _rA, _eA, _rB, _eB)

                for _rnd in range(int(BUILDING_TAIL_SPILL_ROUNDS)):
                    _cands = []
                    for _m, (_res, _end) in _sims.items():
                        for _l, (_st, _fi) in _res.items():
                            if len(info[_l]["machines"]) < 2:
                                if _dbg_rows is not None and _rnd == 0:
                                    _dbg_rows.append({"round": _rnd, "lot_id": _l,
                                                      "item": info[_l]["code"], "machine": _m,
                                                      "target": "", "verdict": "gate0_single_machine"})
                                continue
                            _rdy = info[_l]["earliest"]
                            _mr = material_ready.get(_l)
                            if _mr is not None and _mr > _rdy:
                                _rdy = _mr
                            # G1 GATE 1 (queue wait). CTP #32 used _PTOL here.
                            if _st - _rdy <= _PQUEUE:
                                if _dbg_rows is not None and _rnd == 0:
                                    _dbg_rows.append({"round": _rnd, "lot_id": _l,
                                                      "item": info[_l]["code"], "machine": _m,
                                                      "target": "", "verdict": "gate1_queue_wait",
                                                      "queue_h": round((_st - _rdy)/3600/NS, 2)})
                                continue          # waiting on MATERIAL, not the machine
                            # DRUM GATE: only lots the press is ACTUALLY still waiting for.
                            # Ranked by lateness against last_need, worst first.
                            # G1 GATE 2 (drum lateness). CTP #32 used _PTOL here too.
                            _nd = lot_last_need.get(_l) if BUILDING_TAIL_SPILL_USE_NEED else None
                            if _nd is not None:
                                if _fi - _nd <= _PDRUM:
                                    if _dbg_rows is not None and _rnd == 0:
                                        _dbg_rows.append({"round": _rnd, "lot_id": _l,
                                                          "item": info[_l]["code"], "machine": _m,
                                                          "target": "", "verdict": "gate2_drum",
                                                          "queue_h": round((_st - _rdy)/3600/NS, 2),
                                                          "late_vs_need_h": round((_fi - _nd)/3600/NS, 2)})
                                    continue
                                _cands.append((-(_fi - _nd), str(_l), _l, _m, _st, _rdy))
                            else:
                                _cands.append((-(_st - _rdy), str(_l), _l, _m, _st, _rdy))
                    _cands.sort()
                    _n_moved = 0
                    for _neg, _ls, _l, _m, _st, _rdy in _cands:
                        if _lot_m.get(_l) != _m:
                            continue
                        _best = None
                        for _m2 in sorted(info[_l]["machines"]):
                            if _m2 == _m:
                                continue
                            if _m2 not in _ends:
                                _ends[_m2] = (machine_busy[_m2][-1][1]
                                              if machine_busy.get(_m2) else SCHEDULE_OPEN_NS)
                                _sims[_m2] = ({}, _ends[_m2])
                            # Probe AT THE AGING CAP first, not at the earliest possible
                            # landing: the cap is a FLOOR ON THE START, so it has to be
                            # offered to the simulator as a pin. Probing without it and
                            # then rejecting on the cap (the first cut of this code) threw
                            # away every move whose idle window merely STARTS too early —
                            # measured: it silently disabled rule B completely.
                            _probe = (_st - _PMAX) if _PMAX > 0 else None
                            _nf = _need_floor(_l, _m2)
                            if _nf is not None and (_probe is None or _nf > _probe):
                                _probe = _nf
                            _ok, _ns, _A, _B, _rA, _eA, _rB, _eB = _pullB_ok(
                                _l, _m, _m2, _st, _probe)
                            if _dbg_rows is not None:
                                _dbg_rows.append({"round": _rnd, "lot_id": _l,
                                                  "item": info[_l]["code"], "machine": _m,
                                                  "target": _m2,
                                                  "verdict": ("probe_" + _why[0]),
                                                  "queue_h": round((_st - _rdy)/3600/NS, 2),
                                                  "n_elig": len(info[_l]["machines"])})
                            if not _ok:
                                continue
                            # JIT landing: latest admissible start on THIS machine.
                            _lo, _hi = _ns, _st - _PGAIN
                            _bp = (_ns, _A, _B, _rA, _eA, _rB, _eB)
                            for _ in range(24):
                                _mid = (_lo + _hi) // 2
                                if _mid <= _lo:
                                    break
                                _ok2, _ns2, _A2, _B2, _rA2, _eA2, _rB2, _eB2 = _pullB_ok(
                                    _l, _m, _m2, _st, _mid)
                                if _ok2:
                                    _bp = (_ns2, _A2, _B2, _rA2, _eA2, _rB2, _eB2)
                                    _lo = _mid
                                else:
                                    _hi = _mid
                            # prefer the LATEST landing (freshest tyre), then machine id
                            _k = (-_bp[0], str(_m2))
                            if _best is None or _k < _best[0]:
                                _best = (_k, _m2, _bp)
                        if _best is None:
                            continue
                        _k, _m2, (_ns, _A, _B, _rA, _eA, _rB, _eB) = _best
                        _pin[_l] = _ns
                        machine_lots[_m] = _A; machine_lots[_m2] = _B
                        _sims[_m] = (_rA, _eA); _sims[_m2] = (_rB, _eB)
                        _ends[_m] = _eA; _ends[_m2] = _eB
                        _lot_m[_l] = _m2
                        machine_load[_m] -= _bld_dur_ns(_l, _m)
                        machine_load[_m2] += _bld_dur_ns(_l, _m2)
                        if info[_l]["inch"] is not None:
                            machine_inch.setdefault(_m2, info[_l]["inch"])
                        _moves.append({"round": f"B{_rnd}", "lot_id": _l,
                                       "item": info[_l]["code"], "from_machine": _m,
                                       "to_machine": _m2,
                                       "old_start": str(pd.Timestamp(_st)),
                                       "new_start": str(pd.Timestamp(_ns)),
                                       "ready": str(pd.Timestamp(_rdy)),
                                       "slip_before_h": round((_st - _rdy) / 3600 / NS, 2),
                                       "slip_after_h": round((_ns - _rdy) / 3600 / NS, 2),
                                       "pair_end_before_d": round((_ends[_m] - SCHEDULE_OPEN_NS)
                                                                  / 86400 / NS, 3),
                                       "pair_end_after_d": round((_eB - SCHEDULE_OPEN_NS)
                                                                 / 86400 / NS, 3)})
                        _n_moved += 1
                    if _n_moved == 0:
                        break

                # ═══ CTP DEVIATION #36 — RULE C: HEAD-LOT CONTENTION FIX (G3) ═══════
                # DEFAULT OFF (`building_tail_spill_head_fix: false`).
                #
                # THE DEFECT RULE B CANNOT SEE. Rule B's gates are sized for the tail
                # (48 h of queue, 48 h past last_need). The lot that sets the CRITICAL
                # PRESS'S START does not wait 48 h — MEASURED on the shipped 33.315 d run:
                #   press 4836 first cures at t+52.72 h (2.196 d of the 33.315). Its first
                #   block is SKU 1325222416098VURL0 = GT 2256 ROYL.
                #   GT 2256 ROYL_L0001: earliest 36.94 h, due -1.50 h, last_need 32.0 h,
                #   BUILT 51.22 h  ->  queue wait 14.28 h, which is 14.28 <= 48, so rule B
                #   skips it at gate 1 AND at gate 2 (finish - last_need = 24.5 h <= 48).
                # It is NOT neglect and NOT a material shortage: in that window TBM 3401 is
                # 93 % busy building GT 2157 ROYL KIA, and GT 2256 ROYL has SIX eligible
                # TBMs (3401, 3404, 3408, 3409, 3410, 3411). Two codes wanted one machine
                # and the month-total load balancer had no time axis to see it.
                #
                # THE RULE — same machinery as rule B, three gates re-sized for the head:
                #   H1 HEAD WINDOW — the lot must FINISH inside ..._head_window_h of T0.
                #                    This is a start-of-horizon repair, not a balancer; it
                #                    can never touch the tail rule B owns.
                #   H2 PAST DUE    — the lot must already finish AFTER its own `due`
                #                    (phase-4 LFT) or after its `last_need`. A lot that is
                #                    on time is never moved, however long it queued.
                #   H3 CONTENTION  — queue wait > ..._head_tol_h (default 4 h) AND the gain
                #                    on the receiver >= ..._head_min_gain_h (default 2 h).
                # It then reuses _pullB_ok VERBATIM, so every rule-B safety property still
                # holds: the receiver's end may not grow past the pair max (ii), the rope
                # floor last_need - dur is enforced (vi), and no lot already on the receiver
                # is displaced beyond the configured harm mode (iii). Bounded by ROUTING
                # eligibility only.
                if BUILDING_TAIL_SPILL_HEAD_FIX:
                    _HWIN  = SCHEDULE_OPEN_NS + int(BUILDING_TAIL_SPILL_HEAD_WIN_H * 3600 * NS)
                    _HTOL  = int(BUILDING_TAIL_SPILL_HEAD_TOL_H * 3600 * NS)
                    _HGAIN = int(BUILDING_TAIL_SPILL_HEAD_GAIN_H * 3600 * NS)
                    _n_head = 0
                    for _rnd in range(int(BUILDING_TAIL_SPILL_ROUNDS)):
                        _cands = []
                        for _m, (_res, _end) in _sims.items():
                            for _l, (_st, _fi) in _res.items():
                                if len(info[_l]["machines"]) < 2:
                                    continue
                                if _fi > _HWIN:
                                    continue                                   # H1
                                _due = info[_l]["due"]
                                _nd = lot_last_need.get(_l)
                                _late = ((_due < BIG and _fi > _due)
                                         or (_nd is not None and _fi > _nd))
                                if not _late:
                                    continue                                   # H2
                                _rdy = info[_l]["earliest"]
                                _mr = material_ready.get(_l)
                                if _mr is not None and _mr > _rdy:
                                    _rdy = _mr
                                if _st - _rdy <= _HTOL:
                                    continue                                   # H3a
                                _cands.append((_st, str(_l), _l, _m, _st, _rdy))
                        _cands.sort()          # earliest-starting head lot first
                        _n_moved = 0
                        for _key0, _ls, _l, _m, _st, _rdy in _cands:
                            if _lot_m.get(_l) != _m:
                                continue
                            _best = None
                            for _m2 in sorted(info[_l]["machines"]):
                                if _m2 == _m:
                                    continue
                                if _m2 not in _ends:
                                    _ends[_m2] = (machine_busy[_m2][-1][1]
                                                  if machine_busy.get(_m2) else SCHEDULE_OPEN_NS)
                                    _sims[_m2] = ({}, _ends[_m2])
                                _probe = _need_floor(_l, _m2)
                                _ok, _ns, _A, _B, _rA, _eA, _rB, _eB = _pullB_ok(
                                    _l, _m, _m2, _st, _probe)
                                if _dbg_rows is not None:
                                    _dbg_rows.append({"round": f"C{_rnd}", "lot_id": _l,
                                                      "item": info[_l]["code"], "machine": _m,
                                                      "target": _m2,
                                                      "verdict": ("head_" + _why[0]),
                                                      "queue_h": round((_st - _rdy)/3600/NS, 2),
                                                      "n_elig": len(info[_l]["machines"])})
                                if not _ok or _ns > _st - _HGAIN:
                                    continue                                   # H3b
                                # JIT landing: latest admissible start on THIS machine.
                                _lo, _hi = _ns, _st - _HGAIN
                                _bp = (_ns, _A, _B, _rA, _eA, _rB, _eB)
                                for _ in range(24):
                                    _mid = (_lo + _hi) // 2
                                    if _mid <= _lo:
                                        break
                                    _t = _pullB_ok(_l, _m, _m2, _st, _mid)
                                    if _t[0]:
                                        _bp = _t[1:]      # (ns, A, B, rA, eA, rB, eB)
                                        _lo = _mid
                                    else:
                                        _hi = _mid
                                _k = (-_bp[0], str(_m2))
                                if _best is None or _k < _best[0]:
                                    _best = (_k, _m2, _bp)
                            if _best is None:
                                continue
                            _k, _m2, (_ns, _A, _B, _rA, _eA, _rB, _eB) = _best
                            _pin[_l] = _ns
                            machine_lots[_m] = _A; machine_lots[_m2] = _B
                            _sims[_m] = (_rA, _eA); _sims[_m2] = (_rB, _eB)
                            _ends[_m] = _eA; _ends[_m2] = _eB
                            _lot_m[_l] = _m2
                            machine_load[_m] -= _bld_dur_ns(_l, _m)
                            machine_load[_m2] += _bld_dur_ns(_l, _m2)
                            if info[_l]["inch"] is not None:
                                machine_inch.setdefault(_m2, info[_l]["inch"])
                            _moves.append({"round": f"C{_rnd}", "lot_id": _l,
                                           "item": info[_l]["code"], "from_machine": _m,
                                           "to_machine": _m2,
                                           "old_start": str(pd.Timestamp(_st)),
                                           "new_start": str(pd.Timestamp(_ns)),
                                           "ready": str(pd.Timestamp(_rdy)),
                                           "slip_before_h": round((_st - _rdy) / 3600 / NS, 2),
                                           "slip_after_h": round((_ns - _rdy) / 3600 / NS, 2),
                                           "pair_end_before_d": round((_ends[_m] - SCHEDULE_OPEN_NS)
                                                                      / 86400 / NS, 3),
                                           "pair_end_after_d": round((_eB - SCHEDULE_OPEN_NS)
                                                                     / 86400 / NS, 3)})
                            _n_moved += 1; _n_head += 1
                        if _n_moved == 0:
                            break
                    print(f"  HEAD-LOT CONTENTION FIX (CTP #36 G3): ON — {_n_head} lot(s) "
                          f"reassigned | window {BUILDING_TAIL_SPILL_HEAD_WIN_H:.0f} h, "
                          f"queue tol {BUILDING_TAIL_SPILL_HEAD_TOL_H:.1f} h, min gain "
                          f"{BUILDING_TAIL_SPILL_HEAD_GAIN_H:.1f} h")

                if _dbg_rows:
                    try:
                        pd.DataFrame(_dbg_rows).to_csv(
                            OUTPUTS / "phase5_tbm_pullB_debug.csv", index=False)
                        print(f"  [audit] wrote phase5_tbm_pullB_debug.csv "
                              f"({len(_dbg_rows)} rows)")
                    except Exception as _we:
                        print(f"  [audit] pullB debug CSV write failed: {_we}")

            # ═══ CTP DEVIATION #40 (T2) — RULE E: SAME-MACHINE PULL-FORWARD ═════════
            # DEFAULT OFF (`building_tail_spill_same_machine: false`).
            #
            # THE HOLE IN RULES A/B/C. All three are MACHINE REASSIGNMENTS. Every target
            # loop carries `if _m2 == _m: continue` (:4032 rule B, :4168 rule C, :3797
            # rule A), so no lot can ever be pulled earlier ON THE MACHINE IT IS ALREADY
            # ON. MEASURED on the shipped 33.315 d run:
            #   GT1824 XPC1_L0001 — queue wait 88.03 h, refused by rule B at gate (i)
            #   "no_gain", because its only alternatives 3404 and 3410 are 97.0 % / 96.7 %
            #   busy and offer nothing earlier. Meanwhile TBM 3406 — THE MACHINE THE LOT IS
            #   ALREADY ON — has a SINGLE CONTIGUOUS 48.11 h idle block in that window.
            #   The lot is not on the wrong machine. It is in the wrong PLACE IN THE QUEUE.
            #
            # HOW IT DIFFERS FROM RULE D. Rule D's unit of movement is the MAXIMAL
            # CONTIGUOUS RUN of one item code, so it is changeover-neutral BY CONSTRUCTION
            # and it drags every lot of that campaign — including the ones that are on time
            # and would only be aged by moving earlier. Rule E's unit is ONE LOT, so it can
            # take the single late lot out of a campaign and leave the rest where they are;
            # in exchange it is NOT changeover-neutral by construction and has to be guarded
            # explicitly (gate E5). The two are complementary, not substitutes.
            #
            # THE RULE — rule B's gate set, re-expressed for a single machine. The
            # repositioning mechanism is rule D's `_ordk` layout-key override (the ONLY way
            # to move a lot within a machine: `_pin` is a START FLOOR, so pinning a lot
            # earlier than its queue position does nothing at all).
            #   E1 QUEUE   — start - max(earliest, material_ready) > ..._queue_h. A lot
            #                waiting on MATERIAL is not a queue problem and is never moved.
            #   E2 DRUM    — finish - last_need > ..._drum_h. On-time lots are never moved.
            #                A lot with NO last_need is never moved (no drum signal = no
            #                evidence anything is waiting for it).
            #   E3 MINIMAL — insertion points are tried from the LATEST backwards (rule D's
            #                D2); the FIRST that works is taken. Never promoted to the front.
            #   (i)  GAIN  — must start >= ..._gain_h earlier than it does now.
            #   (ii) END   — the machine's own end may not grow.
            #   E5 CO      — the machine's item-code transition count MAY NOT GROW. This is
            #                the guard F2 did not have (F2: 370 -> 763 changeovers).
            #   (iii) HARM — no other lot on the machine may finish later than it does now
            #                (strict), or must still finish inside its own last_need
            #                (harm_mode: last_need). With pin_starts ON this IS the
            #                idle-absorption test: it only passes into a REAL idle window.
            #   (v)  AGING CAP and (vi) THE CTP #32 ROPE (start >= last_need - dur) are
            #                applied as an explicit START FLOOR before the probe, exactly as
            #                rule B does, so a lot never lands earlier than the drum needs it.
            #   E4 JIT     — binary-search the LATEST admissible start inside the accepted
            #                position. The tyre is built as LATE as the hole allows = as
            #                FRESH as possible.
            if BUILDING_TAIL_SPILL_SM:
                _EQ    = int(BUILDING_TAIL_SPILL_SM_QUEUE_H * 3600 * NS)
                _ED    = int(BUILDING_TAIL_SPILL_SM_DRUM_H * 3600 * NS)
                _EG    = int(BUILDING_TAIL_SPILL_SM_GAIN_H * 3600 * NS)
                _EHARM = int(BUILDING_TAIL_SPILL_PULL_HARM_H * 3600 * NS)
                _EMAX  = int(BUILDING_TAIL_SPILL_PULL_MAX_H * 3600 * NS)
                _n_sm = 0
                _sm_gate = defaultdict(int)

                def _runs_E(order):
                    """maximal contiguous same-item-code runs -> [(i0, i1, code)]"""
                    out = []; i = 0
                    while i < len(order):
                        j = i + 1
                        c = info[order[i]]["code"]
                        while j < len(order) and info[order[j]]["code"] == c:
                            j += 1
                        out.append((i, j, c)); i = j
                    return out

                def _harm_E(_r1, _res0, _skip):
                    """(iii) displacement gate. True = no harm."""
                    for _x, (_xs, _xf) in _r1.items():
                        if _x == _skip:
                            continue
                        _f0 = _res0.get(_x)
                        if _f0 is None:
                            return False
                        if _xf <= _f0[1] + _EHARM:
                            continue
                        if not _HARM_NEED:
                            return False
                        _xn = lot_last_need.get(_x)
                        if _xn is None or _xf > _xn:
                            return False
                    return True

                for _rnd in range(int(BUILDING_TAIL_SPILL_ROUNDS)):
                    _n_moved = 0
                    for _m in sorted(machine_lots):
                        _lm = machine_lots[_m]
                        if len(_lm) < 3:
                            continue
                        _res0, _e0 = _spill_sim(_m, _lm, _pin)
                        _cE = []
                        for _l in _lm:
                            _st, _fi = _res0[_l]
                            _rdy = info[_l]["earliest"]
                            _mr = material_ready.get(_l)
                            if _mr is not None and _mr > _rdy:
                                _rdy = _mr
                            if _st - _rdy <= _EQ:
                                _sm_gate["E1_queue_wait"] += 1; continue
                            _nd = lot_last_need.get(_l)
                            if _nd is None:
                                _sm_gate["E2_no_last_need"] += 1; continue
                            if _fi - _nd <= _ED:
                                _sm_gate["E2_on_time"] += 1; continue
                            _cE.append((-(_fi - _nd), str(_l), _l))
                        _cE.sort()
                        for _neg, _ls, _l in _cE:
                            _res0, _e0 = _spill_sim(_m, _lm, _pin)
                            _ord0 = sorted(_lm, key=_sim_key)
                            _runs = _runs_E(_ord0)
                            _nco0 = len(_runs) - 1
                            _st0 = _res0[_l][0]
                            _ri = None
                            for _q, (_i0, _i1, _c) in enumerate(_runs):
                                if _i0 <= _ord0.index(_l) < _i1:
                                    _ri = _q; break
                            if _ri is None or _ri == 0:
                                _sm_gate["E3_already_first_run"] += 1; continue
                            # (v) aging cap + (vi) CTP #32 rope, as an explicit START FLOOR
                            _floor = _need_floor(_l, _m)
                            if _EMAX > 0:
                                _cap = _st0 - _EMAX
                                if _floor is None or _cap > _floor:
                                    _floor = _cap
                            _saveo = _ordk.get(_l)
                            _savep = _pin.pop(_l, None)
                            _acc = None
                            for _j in range(_ri - 1, -1, -1):            # E3 latest first
                                _pj = _ord0[_runs[_j][0]]
                                _kp = _sim_key(_pj)
                                _ordk[_l] = (_kp[0], _kp[1], _kp[2] - 1, _l)
                                _pin.pop(_l, None)
                                if _floor is not None:
                                    _pin[_l] = _floor
                                _r1, _e1 = _spill_sim(_m, _lm, _pin)
                                _ns = _r1[_l][0]
                                _g = "OK"
                                if _ns > _st0 - _EG:
                                    _g = "i_no_gain"
                                elif _e1 > _e0:
                                    _g = "ii_machine_end_grows"
                                elif (BUILDING_TAIL_SPILL_SM_CO_GUARD
                                      and len(_runs_E(sorted(_lm, key=_sim_key))) - 1 > _nco0):
                                    _g = "E5_changeover_grows"
                                elif not _harm_E(_r1, _res0, _l):
                                    _g = "iii_displaces"
                                _sm_gate["probe_" + _g] += 1
                                if _g == "OK":
                                    _acc = (_j, _ns, _r1, _e1); break
                            if _acc is None:
                                _pin.pop(_l, None)
                                if _saveo is None:
                                    _ordk.pop(_l, None)
                                else:
                                    _ordk[_l] = _saveo
                                if _savep is not None:
                                    _pin[_l] = _savep
                                continue
                            _j, _ns, _r1, _e1 = _acc
                            # E4 — JIT LANDING: latest admissible start (freshest tyre)
                            _lo = _floor if _floor is not None else _ns
                            if _lo < _ns:
                                _lo = _ns
                            _hi = _st0 - _EG
                            _bmid = None
                            for _ in range(22):
                                _mid = (_lo + _hi) // 2
                                if _mid <= _lo:
                                    break
                                _pin[_l] = _mid
                                _r2, _e2 = _spill_sim(_m, _lm, _pin)
                                if (_e2 <= _e0 and _r2[_l][0] <= _st0 - _EG
                                        and _harm_E(_r2, _res0, _l)
                                        and (not BUILDING_TAIL_SPILL_SM_CO_GUARD
                                             or len(_runs_E(sorted(_lm, key=_sim_key))) - 1 <= _nco0)):
                                    _bmid = _mid; _r1, _e1 = _r2, _e2; _lo = _mid
                                else:
                                    _hi = _mid
                            _pin.pop(_l, None)
                            if _bmid is not None:
                                _pin[_l] = _bmid
                            elif _floor is not None:
                                _pin[_l] = _floor
                            _r1, _e1 = _spill_sim(_m, _lm, _pin)
                            _sims[_m] = (_r1, _e1); _ends[_m] = _e1
                            _moves.append({"round": f"E{_rnd}", "lot_id": _l,
                                           "item": info[_l]["code"], "from_machine": _m,
                                           "to_machine": _m,
                                           "old_start": str(pd.Timestamp(_st0)),
                                           "new_start": str(pd.Timestamp(_r1[_l][0])),
                                           "ready": "",
                                           "slip_before_h": 0.0,
                                           "slip_after_h": round((_st0 - _r1[_l][0])
                                                                 / 3600 / NS, 2),
                                           "pair_end_before_d": round((_e0 - SCHEDULE_OPEN_NS)
                                                                      / 86400 / NS, 3),
                                           "pair_end_after_d": round((_e1 - SCHEDULE_OPEN_NS)
                                                                     / 86400 / NS, 3)})
                            _n_sm += 1; _n_moved += 1
                    if _n_moved == 0:
                        break
                print(f"  SAME-MACHINE PULL-FORWARD (CTP #40 rule E): ON — {_n_sm} lot(s) "
                      f"repositioned on their OWN machine | gates queue "
                      f"{BUILDING_TAIL_SPILL_SM_QUEUE_H:.1f} h, drum "
                      f"{BUILDING_TAIL_SPILL_SM_DRUM_H:.1f} h, min gain "
                      f"{BUILDING_TAIL_SPILL_SM_GAIN_H:.1f} h, CO guard "
                      f"{'ON' if BUILDING_TAIL_SPILL_SM_CO_GUARD else 'OFF'}")
                print("     rule-E census: " + " | ".join(
                    f"{k} {v:,}" for k, v in sorted(_sm_gate.items(), key=lambda kv: -kv[1])[:8]))

            # ═══ CTP DEVIATION #36 — RULE D: SAME-MACHINE CAMPAIGN RESEQUENCE ═══════
            # DEFAULT OFF (`building_tail_spill_reseq: false`).
            #
            # WHY RULES A/B/C CANNOT REACH THIS. Every one of them is a MACHINE
            # REASSIGNMENT — phase5_forward_placement_v2.py:3589 `if _m2 == _m: continue`.
            # But the layout key is GLOBAL:
            #     _lay_key = (due_day, ITEM CODE, due, lot_id)          (:2479)
            # so all 11 TBMs walk the same due_day axis in lockstep and a lot's landing
            # time is very nearly machine-INDEPENDENT. MEASURED with
            # building_tail_spill_debug: 441 of 584 head probes and both case-B probes were
            # refused at gate (i) "no gain" — the receivers were no earlier at that sort
            # position. Widening the admission gates (G1, queue 8 h / drum 8 h) therefore
            # bought 0.000 d and cost +0.62 pp over-24 h. The wait is NOT on the wrong
            # machine; it is in the wrong PLACE IN THE QUEUE ON ITS OWN MACHINE.
            #
            # THE DEFECT, MEASURED. Inside one due_day the tie-break is the ITEM CODE
            # STRING, which has nothing to do with when a press needs the tyre:
            #   TBM 3403, due_day 20605:  GT 1482 UHL_L0002 (last_need 64 h) is built FIRST
            #     at 40.4 h — 24 h BEFORE it is needed — because "GT 1482" < "GT 1995",
            #     while GT 1995 ROYL KIA_L0002 (last_need 32 h, material-ready 39.3 h) is
            #     built LAST at 88.1 h, 56 h LATE. Presses 4843/4833/4822 idle on it.
            #   TBM 3406, due_day 20623:  a SINGLE 48.11 h idle block sits at 393.8-441.9 h
            #     (not fragments), and GT1824 XPC1_L0001 — material-ready 404.3 h,
            #     last_need 429.7 h — is built at 492.4 h because 33 lots of alphabetically
            #     earlier codes sort ahead of it. Press 4836's 15.18 h gap is exactly that.
            #
            # WHY NOT F2 (order the whole queue by need). MEASURED and REJECTED:
            #   lot-level  (CTP #35 "lot")    34.261 d, over-24 h 32.12 %, 763 changeovers
            #   code-level (CTP #35 "family") 34.271 d, over-24 h 51.29 %, 361 changeovers
            # Lot-level interleaves codes and doubles the changeovers (+262 h); code-level
            # drags a whole campaign to its EARLIEST member's need and ages the rest.
            #
            # RULE D IS A REPAIR, NOT A RE-SORT. Unit of movement = the MAXIMAL CONTIGUOUS
            # RUN of one item code (a campaign). Moving a whole run to another run boundary
            # is CHANGEOVER-NEUTRAL by construction — the number of code transitions cannot
            # rise (it falls if the run lands next to its own code) — and it is asserted,
            # not assumed. Four gates, all must pass:
            #   D1 LATE      — the run must contain a lot finishing > reseq_tol_h past its
            #                  OWN last_need. On-time campaigns are never touched.
            #   D2 MINIMAL   — insertion points are tried from the LATEST backwards and the
            #                  FIRST that works is taken. The campaign is promoted by the
            #                  least that helps, never to the front.
            #   D3 NO HARM   — the machine's end may not grow, the changeover count may not
            #                  grow, and every DISPLACED lot must still finish inside its
            #                  own last_need (harm_mode: last_need) or not move at all
            #                  (harm_mode: strict). We spend a neighbour's DRUM SLACK only.
            #   D4 JIT       — after the position is fixed, binary-search the LATEST start
            #                  the promoted run can take while D3 still holds. A promoted
            #                  tyre is built as LATE as the hole allows = as FRESH as
            #                  possible. Nothing is ever built before its `earliest`, and
            #                  the last_need rope floor still applies.
            if BUILDING_TAIL_SPILL_RESEQ:
                _RTOL  = int(BUILDING_TAIL_SPILL_RESEQ_TOL_H * 3600 * NS)
                _RGAIN = int(BUILDING_TAIL_SPILL_RESEQ_GAIN_H * 3600 * NS)
                _n_res = 0
                _co_guard = 0
                # HARM MODE "net_lateness" — the TOC objective. Instead of a per-lot veto,
                # score the WHOLE machine: total qty-weighted hours past last_need. A
                # promotion is accepted only if that number strictly falls. This is the
                # only mode that can let an ALREADY-LATE campaign slip a little so a
                # bigger/later one can make its drum — the per-lot vetoes (strict and
                # last_need) both refuse any move that touches a lot which is already late,
                # and MEASURED that is what blocked press 4607's gate lot GT T1457 STAR.
                _NET_LAT = (BUILDING_TAIL_SPILL_HARM_MODE == "net_lateness")

                def _lat_score(_res):
                    """qty-weighted lateness-past-last_need, in unit-hours."""
                    _t = 0.0
                    for _l, (_s, _f) in _res.items():
                        _nd = lot_last_need.get(_l)
                        if _nd is None or _f <= _nd:
                            continue
                        _t += float(lot_qty.get(_l, 0) or 0) * (_f - _nd) / 3600.0 / NS
                    return _t

                def _runs_of(order):
                    """maximal contiguous same-item-code runs -> [(i0, i1, code)]"""
                    out = []
                    i = 0
                    while i < len(order):
                        j = i + 1
                        c = info[order[i]]["code"]
                        while j < len(order) and info[order[j]]["code"] == c:
                            j += 1
                        out.append((i, j, c))
                        i = j
                    return out

                for _rnd in range(int(BUILDING_TAIL_SPILL_ROUNDS)):
                    _n_moved = 0
                    for _m in sorted(machine_lots):
                        _lm = machine_lots[_m]
                        if len(_lm) < 3:
                            continue
                        _res0, _e0 = _spill_sim(_m, _lm, _pin)
                        _ord0 = sorted(_lm, key=_sim_key)
                        _runs = _runs_of(_ord0)
                        _nco0 = len(_runs) - 1
                        # D1 — rank campaigns by worst lateness against their own last_need
                        _rank = []
                        for _ri, (_i0, _i1, _c) in enumerate(_runs):
                            _worst = None
                            for _l in _ord0[_i0:_i1]:
                                _nd = lot_last_need.get(_l)
                                if _nd is None:
                                    continue
                                _lt = _res0[_l][1] - _nd
                                if _worst is None or _lt > _worst:
                                    _worst = _lt
                            if _worst is not None and _worst > _RTOL:
                                _rank.append((-_worst, _ri))
                        if not _rank:
                            continue
                        _rank.sort()
                        for _negw, _ri in _rank:
                            # the run indices are only valid against the CURRENT order, so
                            # recompute after every accepted move on this machine.
                            _ord0 = sorted(_lm, key=_sim_key)
                            _runs = _runs_of(_ord0)
                            if _ri >= len(_runs):
                                continue
                            _i0, _i1, _c = _runs[_ri]
                            _R = _ord0[_i0:_i1]
                            if not _R or _i0 == 0:
                                continue
                            _res0, _e0 = _spill_sim(_m, _lm, _pin)
                            _lat0 = _lat_score(_res0) if _NET_LAT else 0.0
                            _old_worst = max(
                                (_res0[_l][1] - lot_last_need[_l]) for _l in _R
                                if lot_last_need.get(_l) is not None)
                            _saveo = {_l: _ordk.get(_l) for _l in _R}
                            _savep = {_l: _pin.pop(_l, None) for _l in _R}
                            _accepted = None
                            # D2 — latest insertion point first (minimal promotion)
                            for _j in range(_ri - 1, -1, -1):
                                _pj = _ord0[_runs[_j][0]]
                                _kp = _sim_key(_pj)
                                for _q, _l in enumerate(_R):
                                    _ordk[_l] = (_kp[0], _kp[1],
                                                 _kp[2] - (len(_R) - _q), _l)
                                _r1, _e1 = _spill_sim(_m, _lm, _pin)
                                _ok = True
                                if _e1 > _e0:
                                    _ok = False                                   # D3 end
                                if _ok and len(_runs_of(sorted(_lm, key=_sim_key))) - 1 > _nco0:
                                    _ok = False                                   # D3 co
                                    _co_guard += 1
                                if _ok and _NET_LAT:
                                    if _lat_score(_r1) >= _lat0:
                                        _ok = False                               # D3 net
                                elif _ok:
                                    for _l, (_s1, _f1) in _r1.items():
                                        if _l in _saveo:
                                            continue
                                        _f0 = _res0[_l][1]
                                        if _f1 <= _f0:
                                            continue
                                        if not _HARM_NEED:
                                            _ok = False; break                    # D3 strict
                                        _nd = lot_last_need.get(_l)
                                        if _nd is None or _f1 > _nd:
                                            _ok = False; break                    # D3 need
                                if _ok:
                                    _new_worst = max(
                                        (_r1[_l][1] - lot_last_need[_l]) for _l in _R
                                        if lot_last_need.get(_l) is not None)
                                    if _new_worst > _old_worst - _RGAIN:
                                        _ok = False                               # gain
                                if _ok:
                                    _accepted = (_j, _kp, _r1, _e1, _new_worst)
                                    break
                            if _accepted is None:
                                for _l in _R:
                                    if _saveo[_l] is None:
                                        _ordk.pop(_l, None)
                                    else:
                                        _ordk[_l] = _saveo[_l]
                                    if _savep[_l] is not None:
                                        _pin[_l] = _savep[_l]
                                continue
                            _j, _kp, _r1, _e1, _nat_worst = _accepted
                            # ── D4 — JIT LANDING, AND THE ROPE ─────────────────────────
                            # The promotion has already been accepted at its NATURAL (as
                            # early as the hole allows) landing, whose worst lateness is
                            # _nat_worst. Two cases, and getting this wrong is what made
                            # the first cut of rule D cost +0.33 pp over-24 h:
                            #   _nat_worst > 0  — the campaign is STILL late even at the
                            #     earliest landing. There is nothing to give back: any later
                            #     start makes it later still. Target = _nat_worst, so the
                            #     search cannot push it back and it keeps the full gain.
                            #   _nat_worst <= 0 — the hole is big enough to build the
                            #     campaign AHEAD of the drum. THAT IS THE ROPE VIOLATION we
                            #     must not commit: push it back until it finishes AT its own
                            #     last_need (target 0) and no earlier.
                            # (The earlier version targeted `_old_worst - _RGAIN`, i.e. it
                            #  landed the campaign as late as a 2 h improvement allowed —
                            #  it threw the gain away AND still aged the tyre.)
                            _jit_target = _nat_worst if _nat_worst > 0 else 0
                            _lo = _r1[_R[0]][0]
                            _hi = _res0[_R[0]][0]
                            if _hi > _lo:
                                _best = None
                                for _ in range(22):
                                    _mid = (_lo + _hi) // 2
                                    if _mid <= _lo:
                                        break
                                    _pin[_R[0]] = _mid
                                    _r2, _e2 = _spill_sim(_m, _lm, _pin)
                                    _good = (_e2 <= _e0)
                                    if _good and _NET_LAT:
                                        _good = _lat_score(_r2) < _lat0
                                    elif _good:
                                        for _l, (_s2, _f2) in _r2.items():
                                            if _l in _saveo:
                                                continue
                                            _f0 = _res0[_l][1]
                                            if _f2 <= _f0:
                                                continue
                                            _nd = lot_last_need.get(_l)
                                            if (not _HARM_NEED) or _nd is None or _f2 > _nd:
                                                _good = False; break
                                    if _good:
                                        _nw = max((_r2[_l][1] - lot_last_need[_l])
                                                  for _l in _R
                                                  if lot_last_need.get(_l) is not None)
                                        _good = _nw <= _jit_target
                                    if _good:
                                        _best = (_mid, _r2, _e2); _lo = _mid
                                    else:
                                        _hi = _mid
                                _pin.pop(_R[0], None)
                                if _best is not None:
                                    _pin[_R[0]] = _best[0]
                                    _r1, _e1 = _best[1], _best[2]
                            for _l in _R:
                                _pin[_l] = _r1[_l][0]
                            _sims[_m] = (_r1, _e1); _ends[_m] = _e1
                            _moves.append({"round": f"D{_rnd}", "lot_id": _R[0],
                                           "item": _c, "from_machine": _m,
                                           "to_machine": _m,
                                           "old_start": str(pd.Timestamp(_res0[_R[0]][0])),
                                           "new_start": str(pd.Timestamp(_r1[_R[0]][0])),
                                           "ready": str(pd.Timestamp(info[_R[0]]["earliest"])),
                                           "slip_before_h": round(_old_worst / 3600 / NS, 2),
                                           "slip_after_h": round(
                                               max((_r1[_l][1] - lot_last_need[_l])
                                                   for _l in _R
                                                   if lot_last_need.get(_l) is not None)
                                               / 3600 / NS, 2),
                                           "pair_end_before_d": round(
                                               (_e0 - SCHEDULE_OPEN_NS) / 86400 / NS, 3),
                                           "pair_end_after_d": round(
                                               (_e1 - SCHEDULE_OPEN_NS) / 86400 / NS, 3),
                                           "n_lots": len(_R)})
                            _n_moved += 1; _n_res += 1
                    if _n_moved == 0:
                        break
                print(f"  CAMPAIGN RESEQUENCE (CTP #36 rule D): ON — {_n_res} campaign(s) "
                      f"promoted on their own machine | harm mode "
                      f"{BUILDING_TAIL_SPILL_HARM_MODE} | tol "
                      f"{BUILDING_TAIL_SPILL_RESEQ_TOL_H:.1f} h, gain "
                      f"{BUILDING_TAIL_SPILL_RESEQ_GAIN_H:.1f} h | changeover guard blocked "
                      f"{_co_guard} placement(s)")
            _ms1 = max(_ends.values()) if _ends else SCHEDULE_OPEN_NS
            # re-simulate WITHOUT pins so the layout below (which has no pins) is what we
            # report; the pins only ever RAISE a start, and the layout re-derives starts
            # from `earliest` alone — so drop them into per-lot floors the layout honours.
            _bld_pin_floor.update(_pin)
            print(f"  BUILDING TAIL-SPILL (CTP #32): ON — {len(_moves)} lot(s) relocated "
                  f"in <= {int(BUILDING_TAIL_SPILL_ROUNDS)} round(s) | building end "
                  f"{(_ms0 - SCHEDULE_OPEN_NS)/86400/NS:.3f} d -> "
                  f"{(_ms1 - SCHEDULE_OPEN_NS)/86400/NS:.3f} d | gates: tail "
                  f"{BUILDING_TAIL_SPILL_TAIL_H:.0f} h, lateness "
                  f"{BUILDING_TAIL_SPILL_TOL_H:.0f} h, min-gain "
                  f"{BUILDING_TAIL_SPILL_MIN_GAIN_H:.1f} h, pin-starts "
                  f"{'ON' if BUILDING_TAIL_SPILL_PIN else 'OFF'}")
            if _moves:
                try:
                    pd.DataFrame(_moves).to_csv(OUTPUTS / "phase5_tbm_spill_moves.csv",
                                                index=False)
                    print(f"  [audit] wrote phase5_tbm_spill_moves.csv ({len(_moves)} moves)")
                except Exception as _we:
                    print(f"  [audit] spill-move CSV write failed: {_we}")

        # ---- per-machine layout: curing-deadline order, SKU-grouped per day ----
        # Lots are ordered by (curing-day, SKU); within a day a SKU runs as one
        # consecutive campaign. Start is floored at earliest (kit ready & not too
        # early to expire). Changeover: same-inch 40 min, different-inch 60 min.
        n_placed = n_co = n_late = 0
        for m, mlots in machine_lots.items():
            # order/changeover by GT|carcass item CODE (not tyre SKU) so consecutive
            # lots of the same product form one campaign on the machine.
            order = sorted(mlots, key=_sim_key)              # CTP #35 (F2) + #36 rule D
            cursor = machine_busy[m][-1][1] if machine_busy.get(m) else SCHEDULE_OPEN_NS
            last_sku = None
            for lid in order:
                s = info[lid]["code"]
                if last_sku is not None and s != last_sku:
                    # "different size" only when BOTH inches are known and differ;
                    # unknown inch defaults to same-size (machine is inch-locked).
                    _ia = info[_last_lid]["inch"]; _ib = info[lid]["inch"]
                    diff_inch = (_ia is not None and _ib is not None and _ia != _ib)
                    # per-machine changeover from Master_Building_ChangeoverTime
                    # (same-size vs different-size); fall back to the flat constants.
                    co_sm, co_dm = _bld_co.get(str(m),
                                               (BUILDING_CO_SAME_MIN, BUILDING_CO_DIFF_MIN))
                    cursor += int((co_dm if diff_inch else co_sm) * 60 * NS)
                    n_co += 1
                dur = _bld_dur_ns(lid, m)
                _earliest = info[lid]["earliest"]
                # LEVER 1 — S2b: JIT terminal GT. Aim finish AT the LFT (due) so the tyre
                # lands fresh at the press, instead of at fresh_floor (build early, wait).
                # jit_floor = due - dur => finish = due when the machine is free; the
                # forward packer's max(cursor, ...) means a busy machine simply packs later
                # (never double-books, never earlier than kit-ready which is already in
                # `earliest`). GREEN TYRES only, and only with a real curing deadline.
                if (GT_JIT_BUILD and info[lid]["due"] < BIG
                        and lot_item_type.get(lid, "") == "GREEN TYRES"):
                    jit_floor = info[lid]["due"] - dur
                    if jit_floor > _earliest:
                        _earliest = jit_floor
                # CTP #32 aging guard: a lot the tail-spill did NOT move may not be built
                # EARLIER than the pre-repair schedule already built it. Dict is EMPTY
                # when building_tail_spill is OFF, so this line is then a no-op.
                _pf = _bld_pin_floor.get(lid)
                if _pf is not None and _pf > _earliest:
                    _earliest = _pf
                start = max(cursor, _earliest)
                # CTB: don't start before cumulative component supply covers it.
                _mr = material_ready.get(lid)
                if _mr is not None and _mr > start:
                    start = _mr
                finish = start + dur
                if info[lid]["due"] < BIG and finish > info[lid]["due"]:
                    n_late += 1
                schedule[lid] = {
                    "lot_id": lid, "item": lot_item.get(lid, ""), "machine": m,
                    "operation": lot_op.get(lid, ""), "department": lot_dept.get(lid, ""),
                    "equipment": lot_eq.get(lid, ""),
                    "scheduled_start": pd.Timestamp(start),
                    "scheduled_finish": pd.Timestamp(finish),
                    "duration_h": duration_h.get(lid, 0.0),
                    "campaign_id": "", "wire_type": "",
                    "is_camp_first": False, "is_camp_last": False,
                    "finish_ns": finish,
                }
                bisect.insort(machine_busy[m], (start, finish))
                machine_busy_ns[m] += (finish - start)
                cursor = finish; n_placed += 1
                last_sku = s; _last_lid = lid
        print(f"  BUILDING CAMPAIGNS: placed {n_placed:,} lots on {len(machine_lots)} "
              f"machines | {n_co} changeovers (per-machine same/diff) | inch-locked | "
              f"late-vs-curing: {n_late:,}")
        try:
            _dbg = [{"lid": l, "code": info[l]["code"],
                     "earliest": pd.Timestamp(info[l]["earliest"]),
                     # CTP #32: the CTB material floor, so "queued behind the machine" can
                     # be told apart from "waiting on components" WITHOUT re-deriving it.
                     "material_ready": (pd.Timestamp(material_ready[l])
                                        if l in material_ready else pd.NaT),
                     "n_elig": len(info[l]["machines"]),
                     "due": (pd.Timestamp(info[l]["due"]) if info[l]["due"] < BIG else pd.NaT),
                     "qty": float(lot_qty.get(l, 0) or 0),
                     "start": (schedule[l]["scheduled_start"] if l in schedule else pd.NaT),
                     "machine": (schedule[l]["machine"] if l in schedule else "")}
                    for l in blds if info[l]["machines"]]
            pd.DataFrame(_dbg).to_csv(OUTPUTS / "building_debug.csv", index=False)
            print(f"  [debug] wrote building_debug.csv ({len(_dbg)} lots)")
        except Exception as _e:
            print(f"  [debug] dump failed: {_e}")
        return n_placed, n_co

    # ══ v56 MIXER-IMPROVEMENT PLANNER (user 2026-10-09, config mix_planner; code in mixer-improvement/) ══════════
    # All compound lots are planned FIRST by the stock-coverage planner (forward simulation of the mixers, every
    # mixing rule built in); the loop below then places everything else around these FIXED lots, the old mixer passes
    # (IL campaign, butyl blocks, BRM-09 joins, rule-repair moves) are skipped and the final repair never moves them.
    _MIXPLAN = cfg.get("mix_planner") or {}
    _MIXPLAN_ON = bool(_MIXPLAN.get("enabled", False))
    _PLANNED = set()
    _MIXPLAN_ROWS = []
    if _MIXPLAN_ON:
        import sys as _sys56
        _sys56.path.insert(0, str(ROOT / "mixer-improvement"))
        from mix_planner import plan as _mp_plan
        _BD56 = cfg.get("butyl_day_block") or {}
        _PB56 = int(_MIXPLAN.get("prep_lot_batches", _BD56.get("prep_lot_batches", 5)))

        def _commit56(l, m, s0, f0):
            bisect.insort(machine_busy[m], (int(s0), int(f0)))
            machine_busy_ns[m] += max(0, int(f0) - int(s0))
            BUSY_ITEM[(m, int(s0))] = (str(lot_item.get(l, "")), str(lot_item.get(l, "")))
            machine_last_item[m] = lot_item.get(l, "")
            schedule[l] = {"lot_id": l, "item": lot_item.get(l, ""), "machine": m, "operation": lot_op.get(l, ""),
                           "department": lot_dept.get(l, ""), "equipment": lot_eq.get(l, ""),
                           "scheduled_start": pd.Timestamp(int(s0)), "scheduled_finish": pd.Timestamp(int(f0)),
                           "duration_h": (duration_h.get(l, 0.0) if abs((int(f0) - int(s0)) / 3.6e12 - float(duration_h.get(l, 0.0) or 0.0)) < 1 / 60
                                          else (int(f0) - int(s0)) / 3.6e12),   # v91: mixer factor (T638 on K430-2) changes the run time
                           "campaign_id": lot_camp_id.get(l, ""),
                           "wire_type": lot_wire.get(l, ""), "is_camp_first": False, "is_camp_last": False,
                           "finish_ns": int(f0)}

        _n56 = [0]

        def _prep56(item, m):
            t_ = lots[lots["item_code"].astype(str).str.strip() == item]
            if t_.empty:
                return None
            tr = t_.iloc[0].to_dict()
            bs = float(tr.get("batch_size_kg") or 0) or float(tr["lot_qty"]) / max(float(tr.get("batches") or 1), 1)
            q = _PB56 * bs
            h = float(tr["duration_hours"]) * q / max(float(tr["lot_qty"]), 1e-9)
            _n56[0] += 1
            p_ = f"{item}_MPREP{_n56[0]:03d}"
            tr.update(lot_id=p_, raw_demand=0.0, lot_qty=q, surplus=q, duration_min=h * 60, duration_hours=h,
                      duration_h=h, first_need=pd.NaT, last_need=pd.NaT, n_blocks=0, n_skus=0, machines=m,
                      n_machines=1, campaign_id=np.nan, batch_id=p_, skus_set="", batches=float(_PB56),
                      machines_l=[m])
            _MIXPLAN_ROWS.append(tr)
            lot_item[p_] = item
            lot_item_type[p_] = str(tr.get("item_type", "")).strip().upper()
            lot_op[p_], lot_dept[p_], lot_eq[p_] = tr["operation"], tr["department"], tr["equipment"]
            lot_machines[p_], duration_h[p_] = [m], h
            lot_first_need[p_], lot_last_need[p_] = pd.NaT, None
            lot_qty[p_] = q
            return p_

        _long56 = {str(x).strip() for x in ((cfg.get("mixing_v47") or {}).get("long_items") or ["IL3", "IL5"])}
        try:
            _fl56 = pd.read_csv(INPUTS / "frc_lengths.csv")
            _b56 = pd.read_csv(INPUTS / "bom.csv", dtype=str, low_memory=False)
            _long56 |= set(_b56.loc[_b56["Parent"].astype(str).str.strip().isin(
                set(_fl56["item"].astype(str).str.strip())), "child"].astype(str).str.strip())
        except Exception as _e56:
            print(f"  [MIX PLANNER v56] !! 4RC compound list: {_e56}")
        _ex56 = {str(x).strip() for g in (_BD56.get("groups") or {}).values() for x in (g.get("friendly") or [])}
        _ctx56 = {"lots": [l for l in lot_ids if l not in schedule], "lot_item": lot_item, "lot_item_type": lot_item_type,
                  "lot_machines": lot_machines, "duration_h": duration_h, "lot_first_need": lot_first_need,
                  "lot_last_need": lot_last_need, "predecessors": predecessors, "ages_ns": _ages_ns,
                  "material_ready_ns": _material_ready_ns, "downtime": DOWNTIME_WIN, "commit": _commit56,
                  "make_prep": _prep56, "open_ns": int(UPSTREAM_OPEN_NS), "long_items": _long56, "exempt": _ex56,
                  "groups": _BD56.get("groups") or {}, "cfg": _MIXPLAN,
                  "fourrc_items": _long56 - {str(x).strip() for x in
                                             ((cfg.get("mixing_v47") or {}).get("long_items") or ["IL3", "IL5"])}}
        # v68 (user 2026-10-10): 901 FIRST -- each skim compound lot must be usable by the time its 901 lot has to start
        # (901 lot need - its run time - buffer_h); the planner ranks it by that instead of the compound lot's own first
        # need (which comes from its earliest consumer in phase 2 and can be up to 80 h later than a 901 lot it feeds)
        _FD = (_MIXPLAN.get("fourrc_due") or {})
        _due68 = {}
        if _FD.get("enabled", False):
            _bf68 = float(_FD.get("buffer_h", 4)) * 3600 * NS
            for _c68, _ps68 in predecessors.items():
                if "901" not in [str(x) for x in (lot_machines.get(_c68) or [])]:
                    continue
                _n68 = lot_first_need.get(_c68)
                if _n68 is None or pd.isna(_n68):
                    continue
                _d68 = int(pd.Timestamp(_n68).value - float(duration_h.get(_c68, 0) or 0) * 3600 * NS - _bf68)
                for _p68 in _ps68:
                    if str(lot_item_type.get(_p68, "")).upper() in ("FINAL COMPOUND", "MASTER COMPOUND"):
                        _due68[_p68] = min(_due68.get(_p68, _d68), _d68)
            print(f"  [MIX PLANNER v68] 901-driven due time for {len(_due68):,} skim-compound lots "
                  f"(901 lot need - run time - {_FD.get('buffer_h', 4)} h)")
        _ctx56["due_need"] = _due68
        # v69 #3 (user 2026-10-10): CLOSED LOOP -- round 2 reads the round-1 schedule's real consumer starts
        _fb69 = {}
        _fbp69 = os.environ.get("CTP_MIXFB")
        if _fbp69 and os.path.exists(_fbp69):
            try:
                _fbd69 = pd.read_csv(_fbp69)
                _fb69 = {str(a): int(b) for a, b in zip(_fbd69["lot_id"], _fbd69["fb_need_ns"])}
                print(f"  [MIX PLANNER v69] closed loop: {len(_fb69):,} compound lots ranked by their round-1 real need")
            except Exception as _e69:
                print(f"  [MIX PLANNER v69] !! feedback unreadable ({_e69})")
        _ctx56["fb_need"] = _fb69
        _st56, _pl56, _wt56 = _mp_plan(_ctx56)
        _PLANNED.update(_pl56)
        if _MIXPLAN_ROWS:
            lots = pd.concat([lots, pd.DataFrame(_MIXPLAN_ROWS)], ignore_index=True)
        try:
            pd.DataFrame(_wt56, columns=["machine", "idle_from_ns", "idle_to_ns", "blocked_candidates"]).assign(
                idle_from=lambda d_: pd.to_datetime(d_.idle_from_ns), idle_to=lambda d_: pd.to_datetime(d_.idle_to_ns)
            ).to_csv(OUTPUTS / "phase5_mix_planner_waits.csv", index=False)
        except Exception:
            pass
        print(f"  [MIX PLANNER v56] {int(_st56.get('planned', 0)):,} compound lots planned on the mixers by stock "
              f"coverage: {int(_st56.get('runs', 0)):,} runs ({int(_st56.get('lots_in_runs', 0)):,} lots, back to back, "
              f"no changeover inside a run), {int(_st56.get('butyl_blocks', 0))} daily butyl / IL blocks, "
              f"{int(_st56.get('prep_lots', 0))} friendly prep lots; mixer waits {_st56.get('idle_h', 0):,.0f} h "
              f"(nothing allowed could run); not plannable {int(_st56.get('left_unplanned', 0))}")
        print("  [MIX PLANNER v56] stats:", {k_: (round(v_, 1) if isinstance(v_, float) else v_) for k_, v_ in _st56.items()})
        _w0 = pd.Timestamp(os.environ.get("CTP_PLAN_START", "2026-10-06 07:00")).value
        _w1 = pd.Timestamp(os.environ.get("CTP_PLAN_END", "2026-10-16 07:00")).value
        _bz = defaultdict(float)
        for _l in _PLANNED:
            _r = schedule[_l]
            _a, _b = max(_r["scheduled_start"].value, _w0), min(int(_r["finish_ns"]), _w1)
            if _b > _a:
                _bz[_r["machine"]] += (_b - _a) / 3.6e12
        print("  [MIX PLANNER v56] mixer busy h in the plan window:", {k_: round(v_) for k_, v_ in sorted(_bz.items())},
              "total", round(sum(_bz.values())))
        if os.environ.get("MIXPLAN_ONLY"):                   # test mode: planner only
            pd.DataFrame([{k_: schedule[_l][k_] for k_ in ("lot_id", "item", "machine", "scheduled_start", "scheduled_finish")}
                          for _l in _PLANNED]).to_csv(OUTPUTS / "phase5_mix_planner_only.csv", index=False)
            raise SystemExit(0)

    print("\n  Placing lots (JIT + campaign-atomic for belt)...")
    placed = 0
    placed_belt_campaigns = 0
    n_orphans_skipped = 0
    n_orphans_forced = 0          # CTP #8 — orphans with REAL qty. We place these.
    forced_orphans = []
    building_lots = []
    import time as _t
    _t0 = _t.time()
    for k, lid in enumerate(topo):
        if k % 1000 == 0 and k > 0:
            _el = _t.time() - _t0
            _rt = k / _el if _el > 0 else 0
            print(f"    [{k:6d}/{len(topo):6d}] placed={placed} infeas={len(infeas)} "
                  f"elapsed={_el:.1f}s rate={_rt:.0f}/s", flush=True)

        if lot_is_belt.get(lid, False):
            cid = lot_camp_id.get(lid, "")
            if cid:
                c = campaigns.get(cid)
                if c is None:
                    continue
                c["ready"] += 1
                if c["ready"] < c["n_total"]:
                    continue
                ok = place_campaign(cid)
                if ok:
                    placed_belt_campaigns += 1
                    placed += sum(1 for l in c["lots"] if l in schedule)
                continue

        # === CASCADING ORPHAN-LOT SKIP ===
        # A lot is skipped if it doesn't reach any terminal (Building / Curing)
        # via the DAG. This catches MULTI-LEVEL orphans:
        #   FRC mother roll -> orphan cutter cut -> orphan Stage-1 carcass
        #   (Stage-2 had its own demand filled elsewhere, so the carcass and
        #    everything upstream of it become effectively orphan.)
        # cascade_orphans was computed BEFORE the placement loop by reverse-topo
        # reachability analysis (see above).
        # ═══ CTP DEVIATION #8 — NEVER DELETE WORK THAT HAS REAL QUANTITY ══════════════
        # v6 SKIPPED any lot that cannot reach a terminal department in the DAG. That is
        # right for a lot nothing needs. It is CATASTROPHIC for a lot whose edge was merely
        # SEVERED by phase3's F1 aging filter.
        #
        # MEASURED, once the CORRECT 24 h cap-strip shelf life was applied (the plant's own
        # table; v6's generic default of 72 h had been hiding it):
        #   phase3 F1 severs an edge when  gap > (max_aging - 8h) = 16 h
        #   -> 6,642 edges severed -> 67 cap-strip lots can no longer reach a terminal
        #   -> v6 SILENTLY SKIPPED them: 7,459,576 m = 55% OF ALL CAP-STRIP DEMAND
        #   -> and still reported `infeas: 0, aging_violations: 0`. A clean bill of health.
        #
        # The plan was building 327,089 green tyres (100% of them) while making less than
        # half the cap strip they need. It is not a schedule; it is a lie.
        #
        # A SHORT SHELF LIFE IS NOT A REASON TO DELETE THE WORK. The material is demanded,
        # the quantity is real (80,000-145,000 m per lot), the machine exists. So we PLACE
        # it and report loudly. Deleting work is never the right answer to a constraint.
        #
        # We still skip a genuine orphan — one with NO quantity. That is v6's real intent.
        if lid in cascade_orphans:
            _q = float(lot_qty.get(lid, 0) or 0)
            if _q <= 0:
                n_orphans_skipped += 1
                continue
            n_orphans_forced += 1
            forced_orphans.append(lid)
            # fall through and place it

        # Building (Green Tyre / Carcass) lots are placed by the campaign pass
        # AFTER this loop (so their predecessors are already scheduled).
        if lot_item_type.get(lid, "") in BUILDING_ITYPES:
            building_lots.append(lid)
            continue

        if lid in _PLANNED:
            continue                                         # v56: planned by the mixing planner
        _gid = _RUNGRP.get(lid)
        _gst = _RUN_STATE.get(_gid) if _gid else None
        if _gst is not None:
            # v55: a later member of an open run -> directly behind the run on the same mixer
            _m55 = _gst["m"]
            _d55 = int(float(duration_h.get(lid, 0)) * 3600 * NS)
            _rs, _rf = _gst["res"]
            if (_rs, _rf) in machine_busy[_m55]:
                machine_busy[_m55].remove((_rs, _rf))
            _rdy = _material_ready_ns(lid)
            _s55 = max(_gst["end"], int(_rdy) if _rdy > NEG_INF_NS else _gst["end"])
            _s55 = first_fit_jit(machine_busy[_m55], _s55, _s55, _d55)
            if _s55 > _gst["end"] + 60 * NS:
                _RUN_STAT["gap_h"] += (_s55 - _gst["end"]) / (3600.0 * NS)
                _nx = [b for b in machine_busy[_m55] if _gst["end"] <= b[0] < _s55]
                if _nx:
                    _RUN_STAT["broken"] += 1
            result = (_m55, _s55, _s55 + _d55, 0)
            _left = sum(int(float(duration_h.get(x, 0)) * 3600 * NS) for x in _RUN_MEM[_gid]
                        if x not in schedule and x != lid)
            _gst["end"] = _s55 + _d55
            if _left > 0:
                _gst["res"] = (_gst["end"], _gst["end"] + _left)
                bisect.insort(machine_busy[_m55], _gst["res"])
            else:
                _gst["res"] = (0, 0)
            _RUN_STAT["members"] += 1
            _DAYMIX.setdefault((str(lot_item.get(lid, "")).strip(), (_s55 - 7 * 3600 * 10 ** 9) // (24 * 3600 * 10 ** 9)), _m55)
        else:
            result = place_one_lot_jit(lid)
        if result is not None and _gid and _gid not in _RUN_STATE:
            # v55: the first member opens the run and reserves the others' time directly behind it
            _left = sum(int(float(duration_h.get(x, 0)) * 3600 * NS) for x in _RUN_MEM[_gid] if x != lid)
            _RUN_STATE[_gid] = {"m": result[0], "end": result[2], "res": (result[2], result[2] + _left)}
            if _left > 0:
                bisect.insort(machine_busy[result[0]], (result[2], result[2] + _left))
        if result is None:
            # H-1 FIX (2026-07-27): place_one_lot_jit returns None when the lot has NO eligible
            # machine (blank routing `machines` -> parse_machines == []) or no candidate slot.
            # v6 silently `continue`d, so the lot vanished with NO trace and the material-ready
            # gate / starvation audit (which key on `p in schedule`) then reported a false "0".
            # LOG it to infeas so the "0 infeas / 0 starvation" lines can never lie again.
            infeas.append({"lot_id": lid, "item": lot_item.get(lid, ""),
                           "reason": ("no_machine" if not lot_machines.get(lid)
                                      else "no_slot"),
                           "campaign_id": ""})
            continue
        machine, start_ns, finish_ns, co_ns = result
        co_ns_int = int(co_ns or 0)
        machine_busy_ns[machine] += max(0, finish_ns - start_ns) + co_ns_int
        if NB_CO and machine not in PARENT_AWARE_CUTTERS:
            # BR-V4: the changeover sits in the gap BEFORE the lot (checked against both neighbours)
            bisect.insort(machine_busy[machine], (start_ns, finish_ns))
            _nb_stat["lots"] += 1
            _nb_stat["co_before_min"] += co_ns_int / 60 / NS
        else:
            bisect.insort(machine_busy[machine], (start_ns, finish_ns + co_ns_int))
        BUSY_ITEM[(machine, start_ns)] = (str(lot_item.get(lid, "")), str(lot_item.get(lid, "")))
        schedule[lid] = {
            "lot_id":           lid,
            "item":             lot_item.get(lid, ""),
            "machine":          machine,
            "operation":        lot_op.get(lid, ""),
            "department":       lot_dept.get(lid, ""),
            "equipment":        lot_eq.get(lid, ""),
            "scheduled_start":  pd.Timestamp(start_ns),
            "scheduled_finish": pd.Timestamp(finish_ns),
            "duration_h":       duration_h.get(lid, 0.0),
            "campaign_id":      lot_camp_id.get(lid, ""),
            "wire_type":        lot_wire.get(lid, ""),
            "is_camp_first":    False,
            "is_camp_last":     False,
            "finish_ns":        finish_ns,
        }
        placed += 1

    if _RUN_MEM:
        for _gid, _gst in _RUN_STATE.items():                # release reservations of members never placed
            if _gst["res"][1] > _gst["res"][0] and _gst["res"] in machine_busy[_gst["m"]]:
                machine_busy[_gst["m"]].remove(_gst["res"])
        print(f"  [MIXING RUN GROUPS v55] {_RUN_STAT['groups']} multi-lot runs placed back to back on one mixer "
              f"({_RUN_STAT['members']} later lots joined their run, no changeover between); waiting for inputs inside "
              f"runs {_RUN_STAT['gap_h']:,.0f} h; runs broken by another lot: {_RUN_STAT['broken']}")
    # ── BR-V10 IL3/IL5 DAILY CAMPAIGN (2026-10-01, BUSINESS_RULES_LOG.md) ─────────────────────
    # Plant rule 3.1.3: IL3 and IL5 (butyl) are produced in ONE continuous campaign per plant day:
    # whichever is needed first runs first, the other right after it (plain 60 s compound
    # changeover, no clean-out), only the items demanded that day, no fixed time of day. Final-
    # mixer rule: B1/B2/B99 must run before the campaign -> a real B99 lot is moved directly in
    # front of it when one can be (B1/B2 are not in routing/BOM); otherwise the butyl clean-out
    # (1 batch of the previous compound) applies. Every moved lot finishes NO LATER than it was
    # placed or its own deadline (placement LFT), whichever is later; the block is placed as late as
    # allowed (fresh compound). Consumers placed earlier are re-timed by final_repair_pass if needed.
    _ILC = cfg.get("il_daily_campaign") or {}
    IL_PINNED = set()
    _il_rows = []
    if _ILC.get("enabled", False) and not _MIXPLAN_ON:
        _il_items = [str(x).strip() for x in (_ILC.get("items") or ["IL3", "IL5"])]
        _il_prep = str(_ILC.get("prep", "B99")).strip()
        _DAY = 24 * 3600 * NS
        _H7 = 7 * 3600 * NS

        def _pday(ns):
            return (int(ns) - _H7) // _DAY

        def _co_ns(m, a, b):
            if not a or a == b:
                return 0
            return int(float(compute_changeover_min(m, lot_dept.get(b_lid_dept.get(b, ""), "MIXING") or "MIXING",
                                                    a, lot_item_type_by_it.get(a, ""), b,
                                                    lot_item_type_by_it.get(b, "")) or 0) * 60 * NS)

        lot_item_type_by_it = {}
        b_lid_dept = {}
        for _l0, _i0 in lot_item.items():
            lot_item_type_by_it.setdefault(str(_i0), lot_item_type.get(_l0, ""))
            b_lid_dept.setdefault(str(_i0), _l0)

        def _unbusy(m, s, f):
            _bz = machine_busy[m]
            for _ix in range(len(_bz)):
                if _bz[_ix][0] == s:
                    _bz.pop(_ix)
                    BUSY_ITEM.pop((m, s), None)
                    return True
            return False

        def _put(lid, m, s, d):
            f = s + d
            bisect.insort(machine_busy[m], (s, f))
            BUSY_ITEM[(m, s)] = (str(lot_item.get(lid, "")), str(lot_item.get(lid, "")))
            schedule[lid]["machine"] = m
            schedule[lid]["scheduled_start"] = pd.Timestamp(int(s))
            schedule[lid]["scheduled_finish"] = pd.Timestamp(int(f))
            schedule[lid]["finish_ns"] = int(f)

        # v12 (2026-10-01): one unified placement per campaign. Fixes from the v11 analysis:
        #  (1) campaigns already placed are FIXED obstacles (like downtime) -- a later campaign's
        #      room-making can no longer push an earlier campaign's lot out of its block;
        #  (2) B99 is chosen FOR the campaign: a whole real B99 lot that can sit directly in front
        #      (inputs ready, its own deadline kept, still within shelf life at its last use);
        #  (3) the block time is chosen for compliance: latest start meeting the IL deadlines, B99
        #      readiness and B99 shelf life; a free window if one exists, else room is made;
        #  (4) a final check merges two campaigns that land on the same production day.
        _il_lots = [l for l, r in schedule.items() if str(r.get("item", "")).strip() in _il_items]
        _groups = defaultdict(list)
        for l in _il_lots:
            _fn = lot_first_need.get(l)
            _k = _pday(pd.Timestamp(_fn).value) if (_fn is not None and not pd.isna(_fn)) \
                else _pday(schedule[l]["scheduled_start"].value)
            _groups[_k].append(l)
        _ln_map = dict(zip(lots["lot_id"], pd.to_datetime(lots.get("last_need", pd.Series(pd.NaT, index=lots.index)),
                                                          errors="coerce")))
        _SAFE = int(float(_ILC.get("prep_safety_h", 4)) * 3600 * NS)
        _LEAD, _TRAIL, _GAP = int(8 * 60 * NS), int(5 * 60 * NS), int(60 * NS)
        _fixed = defaultdict(list)                         # machine -> [(s, f)] of placed campaign lots
        _used_prep = set()
        # v16 (2026-10-04): when NO existing B99 lot can sit in front of a campaign without missing its
        # own deadline (heavy load: IL inputs ready after every free B99 lot is due), a DEDICATED prep
        # lot of prep_lot_batches (BRM-01 minimum, 5) is made directly before the campaign -- the plant's
        # B99 flush; the extra B99 goes to stock. Sized like an existing B99 lot on that mixer
        # (batch_size_kg, kg/h). prep_make_lot: false = v15 behaviour (butyl clean-out instead).
        _MAKE_PREP = bool(_ILC.get("prep_make_lot", False))
        _PREP_B = int(_ILC.get("prep_lot_batches", 5))
        _prep_made = {}                                    # lot_id -> lots-table row (dict)

        def _new_prep(m_):
            _t = lots[(lots["item_code"].astype(str).str.strip() == _il_prep)
                      & lots["machines_l"].apply(lambda x: m_ in (x or []))]
            if _t.empty:
                return None, 0
            tr = _t.iloc[0].to_dict()
            _bs = float(tr.get("batch_size_kg") or 0) or float(tr["lot_qty"]) / max(float(tr.get("batches") or 1), 1)
            _q = _PREP_B * _bs
            _h = float(tr["duration_hours"]) * _q / max(float(tr["lot_qty"]), 1e-9)
            p_ = f"{_il_prep}_PREP{len(_prep_made) + 1:03d}"
            tr.update(lot_id=p_, raw_demand=0.0, lot_qty=_q, surplus=_q, duration_min=_h * 60, duration_hours=_h,
                      first_need=pd.NaT, last_need=pd.NaT, n_blocks=0, n_skus=0, machines=m_, n_machines=1,
                      campaign_id=np.nan, batch_id=p_, skus_set="", batches=float(_PREP_B), machines_l=[m_])
            _prep_made[p_] = tr
            lot_item[p_] = tr["item_code"]
            lot_item_type[p_] = str(tr.get("item_type", "")).strip().upper()
            lot_op[p_], lot_dept[p_], lot_eq[p_] = tr["operation"], tr["department"], tr["equipment"]
            lot_machines[p_], duration_h[p_] = [m_], _h
            lot_first_need[p_], lot_last_need[p_] = pd.NaT, None
            lot_qty[p_] = _q
            schedule[p_] = {"lot_id": p_, "item": tr["item_code"], "machine": m_, "operation": tr["operation"],
                            "department": tr["department"], "equipment": tr["equipment"],
                            "scheduled_start": pd.Timestamp(0), "scheduled_finish": pd.Timestamp(0),
                            "duration_h": _h, "campaign_id": "", "wire_type": "", "is_camp_first": False,
                            "is_camp_last": False, "finish_ns": 0}
            return p_, int(_h * 3600 * NS)
        _n_ok = _n_keep = _n_prep = _n_enf = _n_pushed = _n_late = _n_merge = _n_strand = 0
        _strand = []

        def _obstacles(m_):
            return [(s_, f_) for s_, f_ in machine_busy[m_] if (m_, s_) not in BUSY_ITEM] + _fixed[m_]

        def _clear(m_, a_, b_):
            mv = True
            while mv:
                mv = False
                for s_, f_ in _obstacles(m_):
                    if s_ < b_ and f_ > a_:
                        a_, b_ = f_, f_ + (b_ - a_)
                        mv = True
            return a_, b_

        def _make_room(m_, a_, b_, keep):
            """free [a_, b_): ordinary lots overlapping it move later in order, past fixed obstacles."""
            lot_at = {schedule[l]["scheduled_start"].value: l for l in schedule
                      if str(schedule[l]["machine"]) == m_ and l not in keep and l not in IL_PINNED}
            cursor, n_ = b_, 0
            for s_, f_ in sorted(list(machine_busy[m_])):
                if f_ <= a_:
                    continue
                l_ = lot_at.get(s_)
                if l_ is None:                                # downtime or a fixed campaign lot
                    if s_ <= cursor < f_:
                        cursor = f_
                    continue
                if s_ >= cursor:
                    break
                d_ = f_ - s_
                _unbusy(m_, s_, f_)
                ns_, _ = _clear(m_, cursor, cursor + d_)
                _put(l_, m_, ns_, d_)
                cursor = ns_ + d_ + _GAP
                n_ += 1
            return n_

        def _ready(l):
            v = _material_ready_ns(l)
            return max(UPSTREAM_OPEN_NS, int(v)) if v > NEG_INF_NS else UPSTREAM_OPEN_NS

        def _deadline(l, of):
            _lf = lft_map.get(l)
            _lv = pd.Timestamp(_lf).value if (_lf is not None and not pd.isna(_lf)) else of
            return max(of, _lv)

        def _place_campaign(order, m, dur, orig):
            """place [B99] + order on m; returns (seq, start, end, pushed, late, prep)."""
            offs, t, prev = {}, 0, ""
            for l in order:
                it = str(lot_item.get(l, "")).strip()
                t += _co_ns(m, prev, it)
                offs[l] = t
                t += dur[l]
                prev = it
            total = t
            rel = max(_ready(l) - offs[l] for l in order)
            smax = min(_deadline(l, orig[l][1]) - (offs[l] + dur[l]) for l in order)
            first_it = str(lot_item.get(order[0], "")).strip()
            cop = _co_ns(m, _il_prep, first_it)
            best = None                                   # (late?, S, deadline_p, p, dp)
            for p, r_ in schedule.items():
                if str(r_.get("item", "")).strip() != _il_prep or p in _used_prep or p in IL_PINNED:
                    continue
                if not (str(r_["machine"]) == m or m in (lot_machines.get(p) or [])):
                    continue
                _ps, _pf = r_["scheduled_start"].value, int(r_["finish_ns"])
                dp = _pf - _ps
                S_lo = max(rel, _ready(p) + dp + cop)
                _mxa = _ages_ns(_il_prep)[1]
                _ln = _ln_map.get(p)
                if _mxa and _mxa > 0 and _ln is not None and not pd.isna(_ln):
                    S_lo = max(S_lo, pd.Timestamp(_ln).value - _mxa + _SAFE + cop)   # B99 fresh at its last use
                S_hi = min(smax, _deadline(p, _pf) + cop)                            # IL on time, B99 on time
                if S_lo <= S_hi:
                    cand = (0, S_hi, _deadline(p, _pf), p, dp, S_lo)
                elif S_lo <= _deadline(p, _pf) + cop:                               # IL late, B99 on time
                    cand = (1, S_lo, _deadline(p, _pf), p, dp, S_lo)
                else:
                    if os.environ.get("CTP_IL_DEBUG"):
                        print(f"    [il-dbg] {order[0]} rel={pd.Timestamp(rel)} smax={pd.Timestamp(smax)} | {p} on "
                              f"{r_['machine']} ready={pd.Timestamp(_ready(p))} dl={pd.Timestamp(_deadline(p, _pf))} "
                              f"S_lo={pd.Timestamp(S_lo)} ln={_ln} -> REJECT (B99 would be late)")
                    continue
                if best is None or cand[:3] < best[:3]:
                    best = cand
            if best is not None:
                late, S, _, p, dp, S_lo = best
                _pm, _ps, _pf = str(schedule[p]["machine"]), schedule[p]["scheduled_start"].value, int(schedule[p]["finish_ns"])
                _unbusy(_pm, _ps, _pf)
                head = dp + cop
            else:
                p, dp = _new_prep(m) if _MAKE_PREP else (None, 0)
                head = dp + cop if p else 0
                S = smax if rel <= smax else rel
                S_lo, late = rel, int(rel > smax)
            occ = _LEAD + head + total + _TRAIL
            pushed = 0
            s_free = latest_fit(machine_busy[m], S_lo - head - _LEAD, S - head - _LEAD, occ)
            if s_free is not None and not any(s_ < s_free + occ and f_ > s_free for s_, f_ in _fixed[m]):
                a0 = s_free + _LEAD
            else:
                a0, _ = _clear(m, S - head - _LEAD, S - head - _LEAD + occ)
                a0 += _LEAD
                pushed = _make_room(m, a0 - _LEAD, a0 + head + total + _TRAIL, set(order) | ({p} if p else set()))
            seq = []
            if p:
                _put(p, m, a0, dp)
                seq.append(p)
            for l in order:
                _put(l, m, a0 + head + offs[l], dur[l])
                seq.append(l)
            for l in seq:
                _fixed[m].append((schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"])))
            return seq, a0, a0 + head + total, pushed, int(late), p

        for _d in sorted(_groups):
            G = _groups[_d]
            m = max({str(schedule[l]["machine"]) for l in G},
                    key=lambda x: sum(1 for l in G if str(schedule[l]["machine"]) == x))
            _fn_of = lambda l: (pd.Timestamp(lot_first_need.get(l)).value
                                if lot_first_need.get(l) is not None and not pd.isna(lot_first_need.get(l))
                                else schedule[l]["scheduled_start"].value)
            _first = {}
            for l in G:
                it = str(lot_item.get(l, "")).strip()
                _first[it] = min(_first.get(it, 1 << 62), _fn_of(l))
            order = sorted(G, key=lambda l: (_first[str(lot_item.get(l, "")).strip()], _fn_of(l), l))
            orig = {l: (schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"]),
                        str(schedule[l]["machine"])) for l in order}
            dur = {l: orig[l][1] - orig[l][0] for l in order}
            for l in order:
                _unbusy(orig[l][2], orig[l][0], orig[l][1])
            seq, s0, s1, pushed, late, p = _place_campaign(order, m, dur, orig)
            if p:
                _used_prep.add(p)
                _n_prep += 1
            _n_ok += 1
            _n_enf += int(pushed > 0)
            _n_pushed += pushed
            _n_late += late
            IL_PINNED |= set(seq)
            _il_rows.append({"need_day": pd.Timestamp(int(_d * _DAY + _H7)), "machine": m,
                             "sequence": " -> ".join(str(lot_item.get(l, "")) for l in seq),
                             "lots": ";".join(seq), "prep": _il_prep if p else "",
                             "campaign_start": pd.Timestamp(int(s0)), "campaign_end": pd.Timestamp(int(s1)),
                             "made_room": pushed > 0, "f270_lots_pushed": pushed, "il_late_vs_deadline": bool(late)})

        # (4) production-day check: two campaigns starting on the same production day -> merge the later
        #     one directly behind the earlier one when its inputs are ready by then (deadlines only improve)
        def _il0(r):
            # v16: a campaign's production day is that of its FIRST IL lot (as audit_business_rules and
            # the IL3-IL5 sheet count it), not of its B99 -- a B99 at 06:56 with IL from 07:50 is the
            # 07:00 day, and two such campaigns were missed as "different days".
            return min(schedule[l]["scheduled_start"].value for l in r["lots"].split(";")
                       if l in schedule and str(lot_item.get(l, "")).strip() in _il_items)

        _camps = sorted(_il_rows, key=lambda r: r["campaign_start"])
        for i in range(1, len(_camps)):
            A, B = _camps[i - 1], _camps[i]
            if _pday(_il0(A)) != _pday(_il0(B)) or A["machine"] != B["machine"]:
                continue
            m = A["machine"]
            lotsB = [l for l in B["lots"].split(";") if l and str(lot_item.get(l, "")).strip() in _il_items]
            a_end = max(int(schedule[l]["finish_ns"]) for l in A["lots"].split(";") if l)
            prev_it = str(lot_item.get(A["lots"].split(";")[-1], "")).strip()
            t, offs = a_end, {}
            for l in lotsB:
                it = str(lot_item.get(l, "")).strip()
                t += _co_ns(m, prev_it, it)
                offs[l] = t
                t += int(schedule[l]["finish_ns"]) - schedule[l]["scheduled_start"].value
                prev_it = it
            if any(_ready(l) > offs[l] for l in lotsB):
                # v14 (2026-10-01): inputs not ready right after A -> B cannot merge. Keeping it would put a
                # 2nd campaign on A's production day (rule 3.1.3: once a day). Move the WHOLE B block
                # ([B99] + IL, same order and gaps) to the next production day's start, past fixed blocks.
                lotsAll = [l for l in B["lots"].split(";") if l]
                b0 = min(schedule[l]["scheduled_start"].value for l in lotsAll)
                nd = (_pday(b0) + 1) * _DAY + _H7
                rel_ = {l: schedule[l]["scheduled_start"].value - b0 for l in lotsAll}
                dur_ = {l: int(schedule[l]["finish_ns"]) - schedule[l]["scheduled_start"].value for l in lotsAll}
                span = max(rel_[l] + dur_[l] for l in lotsAll)
                for l in lotsAll:
                    s_, f_ = schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"])
                    _unbusy(m, s_, f_)
                    if (s_, f_) in _fixed[m]:
                        _fixed[m].remove((s_, f_))
                n0, _ = _clear(m, nd + _LEAD, nd + _LEAD + span)
                _make_room(m, n0 - _LEAD, n0 + span + _TRAIL, set(lotsAll))
                for l in lotsAll:
                    _put(l, m, n0 + rel_[l], dur_[l])
                    _fixed[m].append((schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"])))
                B["campaign_start"] = pd.Timestamp(int(n0))
                B["campaign_end"] = pd.Timestamp(int(n0 + span))
                B["moved_to_next_day"] = True
                _n_merge += 1
                continue
            for l in B["lots"].split(";"):
                if not l:
                    continue
                s_, f_ = schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"])
                if (s_, f_) in _fixed[m]:
                    _fixed[m].remove((s_, f_))
                if l in lotsB:                            # only the IL lots move; B's B99 stays booked
                    _unbusy(m, s_, f_)
            _make_room(m, a_end, t + _TRAIL, set(lotsB))
            for l in lotsB:
                _put(l, m, offs[l], int(schedule[l]["finish_ns"]) - schedule[l]["scheduled_start"].value)
                _fixed[m].append((schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"])))
            # v14: B's own prep lot (B99) is no longer part of any campaign -> an ordinary lot again
            # (re-booked at its slot, unpinned); the MERGED block (A + B's IL lots) becomes the reference
            # for the next same-day check, so a third campaign cannot attach behind a stranded B99.
            for l in B["lots"].split(";"):
                if l and l not in lotsB:
                    IL_PINNED.discard(l)
                    _used_prep.discard(l)
                    if l in _prep_made:                   # v16: a prep lot made for B has no use now
                        _unbusy(m, schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"]))
                        del schedule[l], _prep_made[l]
                    elif _ILC.get("regroup_merged", False):
                        _strand.append(l)                 # v18: joins the merged block (B99 -> B99 -> IL)
            B["lots"] = A["lots"] + ";" + ";".join(lotsB)
            B["sequence"] = A["sequence"] + " -> " + " -> ".join(str(lot_item.get(l, "")) for l in lotsB)
            B["campaign_start"], B["campaign_end"] = A["campaign_start"], pd.Timestamp(int(t))
            B["prep"] = A.get("prep", "")
            A["merged_next"] = True
            B["merged_into_previous"] = True
            _n_merge += 1
            if _ILC.get("regroup_merged", False):
                # v18 (BRM-09 + 3.1.3): a merged block ran IL3 -> IL5 -> IL3 -> IL5 (the same compound twice in
                # a shift) and B's B99 was left at its slot, splitting the block. Re-lay the block as
                # [B99 of A] [B99 of B] [all lots of the first-needed IL item] [all lots of the other], as one
                # continuous run, starting no earlier than A's block and no lot before its inputs are ready.
                _all = [x for x in B["lots"].split(";") if x and x in schedule] + [x for x in _strand if x in schedule]
                _strand.clear()
                _pp = [x for x in _all if str(lot_item.get(x, "")).strip() not in _il_items]
                _il = [x for x in _all if str(lot_item.get(x, "")).strip() in _il_items]
                _iord = list(dict.fromkeys(str(lot_item.get(x, "")).strip() for x in
                                           sorted(_il, key=lambda x: schedule[x]["scheduled_start"].value)))
                _il.sort(key=lambda x: (_iord.index(str(lot_item.get(x, "")).strip()), schedule[x]["scheduled_start"].value))
                _pp.sort(key=lambda x: schedule[x]["scheduled_start"].value)
                seq_ = _pp + _il
                du_ = {x: int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value for x in seq_}
                of_, tt_, pv_ = {}, 0, ""
                for x in seq_:
                    it = str(lot_item.get(x, "")).strip()
                    tt_ += _co_ns(m, pv_, it)
                    of_[x] = tt_
                    tt_ += du_[x]
                    pv_ = it
                a0_ = min(schedule[x]["scheduled_start"].value for x in seq_)
                S_ = max([a0_] + [_ready(x) - of_[x] for x in seq_])
                for x in seq_:
                    s_, f_ = schedule[x]["scheduled_start"].value, int(schedule[x]["finish_ns"])
                    _unbusy(m, s_, f_)
                    if (s_, f_) in _fixed[m]:
                        _fixed[m].remove((s_, f_))
                S_, _ = _clear(m, S_, S_ + tt_)
                _make_room(m, S_ - _LEAD, S_ + tt_ + _TRAIL, set(seq_))
                for x in seq_:
                    _put(x, m, S_ + of_[x], du_[x])
                    _fixed[m].append((S_ + of_[x], S_ + of_[x] + du_[x]))
                    IL_PINNED.add(x)
                    if x in _pp:
                        _used_prep.add(x)
                B["lots"] = ";".join(seq_)
                B["sequence"] = " -> ".join(str(lot_item.get(x, "")) for x in seq_)
                B["campaign_start"], B["campaign_end"] = pd.Timestamp(int(S_)), pd.Timestamp(int(S_ + tt_))
                _n_strand += 1
        # ── BR-V2 FINAL-MIXER PREP FOR 30G (v29, 2026-10-06, config butyl_prep_campaign) ─────────────────────
        # The final-mixer rule says B1 / B2 / B99 must run before 30G / IL3 / IL5. IL3/IL5 get it from the IL
        # campaign above; 30G only had the 120 s clean-out penalty (11 of 12 runs in v27 after another
        # compound). Now each plant day's 30G lots form one run placed with a real B99 lot directly in front
        # (same routine as the IL campaign: B99 inputs ready, its own deadline and shelf life kept), or a
        # dedicated B99 prep lot of prep_lot_batches when none fits. Not an IL campaign (own counter / report).
        _BPC = cfg.get("butyl_prep_campaign") or {}
        _n30 = _n30p = _n30l = 0
        if _BPC.get("enabled", False):
            _bp_items = [str(x).strip() for x in (_BPC.get("items") or ["30G"])]
            _bp_lots = [l for l, r in schedule.items()
                        if str(r.get("item", "")).strip() in _bp_items and l not in IL_PINNED]
            _fn30 = lambda l: (pd.Timestamp(lot_first_need.get(l)).value
                               if lot_first_need.get(l) is not None and not pd.isna(lot_first_need.get(l))
                               else schedule[l]["scheduled_start"].value)
            _bg = defaultdict(list)
            for l in _bp_lots:
                _bg[(_pday(_fn30(l)), str(lot_item.get(l, "")).strip())].append(l)
            _bp_rows = []
            for _key in sorted(_bg):
                G = _bg[_key]
                m = max({str(schedule[l]["machine"]) for l in G},
                        key=lambda x: sum(1 for l in G if str(schedule[l]["machine"]) == x))
                order = sorted(G, key=lambda l: (_fn30(l), l))
                orig = {l: (schedule[l]["scheduled_start"].value, int(schedule[l]["finish_ns"]),
                            str(schedule[l]["machine"])) for l in order}
                dur = {l: orig[l][1] - orig[l][0] for l in order}
                for l in order:
                    _unbusy(orig[l][2], orig[l][0], orig[l][1])
                seq, s0, s1, pushed, late, p = _place_campaign(order, m, dur, orig)
                if p:
                    _used_prep.add(p)
                    _n30p += 1
                _n30 += 1
                _n30l += late
                IL_PINNED |= set(seq)
                _bp_rows.append({"need_day": pd.Timestamp(int(_key[0] * _DAY + _H7)), "machine": m,
                                 "sequence": " -> ".join(str(lot_item.get(l, "")) for l in seq),
                                 "lots": ";".join(seq), "prep": _il_prep if p else "",
                                 "start": pd.Timestamp(int(s0)), "end": pd.Timestamp(int(s1)),
                                 "lots_pushed": pushed, "late_vs_deadline": bool(late)})
            if _bp_rows:
                pd.DataFrame(_bp_rows).to_csv(OUTPUTS / "phase5_30g_prep_runs.csv", index=False)
            print(f"  [BR-V2 30G] {_n30} {'/'.join(_bp_items)} runs placed with {_il_prep} directly in front "
                  f"({_n30p} with a {_il_prep} lot); {_n30l} later than their deadline")
        print(f"  [BR-V10] IL3/IL5 daily campaign (v12 logic): {_n_ok} campaigns for {len(_groups)} demand days; "
              f"{_n_prep} with a {_il_prep} lot directly in front; {_n_enf} needed room ({_n_pushed} other mixer lots "
              f"pushed later); {_n_late} IL late vs deadline; {_n_merge} same-day campaigns merged or moved to the next day; "
              f"{_n_strand} merged blocks re-laid as {_il_prep} -> one run per IL item")
        if _prep_made:
            lots = pd.concat([lots, pd.DataFrame(list(_prep_made.values()))], ignore_index=True)
            print(f"  [BR-V10] v16: {len(_prep_made)} dedicated {_il_prep} prep lot(s) of {_PREP_B} batches made "
                  f"(no existing {_il_prep} lot could run in front on time): "
                  + ", ".join(f"{k} {pd.Timestamp(schedule[k]['scheduled_start']):%d-%b %H:%M} "
                              f"{float(v['lot_qty']):,.0f} kg" for k, v in _prep_made.items()))
        if _il_rows:
            pd.DataFrame(_il_rows).to_csv(OUTPUTS / "phase5_il_daily_campaigns.csv", index=False)

    # === Building campaign pass (inch-locked, per-SKU, 30-min CO) ===
    # ─── BUILDING-PLAN ECHO (2026-07-29) ────────────────────────────────────────────────────
    # When the plant supplies a BUILDING plan, building is a PINNED ECHO (structural twin of
    # phase6's curing echo): each plan row keeps its TBM + qty, slides LATER only for component
    # kit-readiness or the TBM's own prev-end. This BYPASSES place_building_campaigns wholesale
    # (all derived levers -- LPT/takt/spread/tail/inch-lock/month-cap -- live inside it), one clean
    # seam. OFF (default) -> the derived pass runs verbatim; the whole path is byte-identical.
    BUILDING_PLAN_ECHO = bool(cfg.get("building_plan_echo", False))

    def _revise_building_schedule_echo():
        nonlocal lots
        import bisect as _bisect
        bp_path = INPUTS / "building_plan.csv"
        if not bp_path.exists():
            print("  [BUILDING ECHO] ON but inputs/building_plan.csv missing -- NO building placed.")
            return 0, 0, 0
        bp = pd.read_csv(bp_path)
        bp["startTime"] = pd.to_datetime(bp["startTime"], errors="coerce")
        bp = bp.dropna(subset=["startTime"]).sort_values("startTime").reset_index(drop=True)
        bp["qty"] = pd.to_numeric(bp["qty"], errors="coerce").fillna(0.0)
        bp = bp[bp["qty"] > 0].reset_index(drop=True)
        # ── DAY-POOL SEQUENCING BY CURING NEED (2026-09-29, config `building_day_pool_sequence`) ──
        # The plant's Shift Schedule is a DAY plan sliced into shifts pro-rata (its Notes sheet): a
        # machine's 3-4 SKUs of a day all read 07:00-15:00 and their order is NOT specified. Taking
        # them in file order built e.g. GT 1402 4th on TBMPCR4 (01:07 next day) while its presses
        # wanted it at 07:00 -> 30-Sep lost ~1,500 cures. Here every row of a (machine, plant-day)
        # may start from the day's 07:00 and the day is built in order of CURING NEED: the time the
        # presses first need this row's tyres = cumulative curing-plan draw of its GT passing
        # (opening GT stock + earlier building-plan rows of that GT).
        if cfg.get("building_day_pool_sequence", False):
            try:
                _pl = pd.read_csv(INPUTS / "plan.csv")
                _pl = _pl[(_pl["skuCode"].astype(str).str.upper() != "CHANGEOVER")]
                _pl["startTime"] = pd.to_datetime(_pl["startTime"], errors="coerce")
                _pl["qty"] = pd.to_numeric(_pl["qty"], errors="coerce").fillna(0.0)
                _bom = pd.read_csv(INPUTS / "bom.csv", dtype=str,
                                   usecols=["Super_parent", "child", "child_description"])
                _bom = _bom[_bom["child_description"].astype(str).str.strip().str.upper()
                            .isin(["GREEN TYRES", "GREEN_TYRE"])]
                _s2g = dict(zip(_bom["Super_parent"].str.strip(), _bom["child"].str.strip()))
                _pl["gt"] = _pl["skuCode"].astype(str).str.strip().map(_s2g)
                bp["_gt"] = bp["skuCode"].astype(str).str.strip().map(_s2g).fillna(
                    bp.get("gtCode", pd.Series("", index=bp.index)).astype(str).str.strip())
                _inv = pd.read_csv(INPUTS / "inventory.csv", low_memory=False)
                _inv["itemcode"] = _inv["itemcode"].astype(str).str.strip()
                _gtw = pd.to_numeric(_inv["inventory"], errors="coerce").fillna(0.0).groupby(_inv["itemcode"]).sum()
                _need = {}
                for _g, _d in _pl.dropna(subset=["gt"]).sort_values("startTime").groupby("gt"):
                    _need[_g] = (_d["startTime"].values, _d["qty"].cumsum().values)
                _cum = defaultdict(float)
                _nt = []
                for _i, _r in bp.sort_values("startTime", kind="mergesort").iterrows():
                    _g = _r["_gt"]
                    _before = _gtw.get(_g, 0.0) + _cum[_g]
                    _cum[_g] += float(_r["qty"])
                    _t = pd.Timestamp.max
                    if _g in _need:
                        _ts, _cs = _need[_g]
                        _k = int(np.searchsorted(_cs, _before + 1e-6, side="right"))
                        if _k < len(_ts):
                            _t = pd.Timestamp(_ts[_k])
                    _nt.append((_i, _t))
                bp["_need_t"] = pd.Series(dict(_nt))
                _day0 = (bp["startTime"] - pd.Timedelta(hours=7)).dt.normalize() + pd.Timedelta(hours=7)
                bp["_orig_start"] = bp["startTime"]
                # BR-1 (2026-09-30, config building_shift_pool): follow the SHIFT-wise plan -- a row may not
                # start before its own plan shift (A 07 / B 15 / C 23); only the ORDER inside the day is ours.
                if not cfg.get("building_shift_pool", False):
                    bp["startTime"] = _day0                  # the whole plant-day is the window
                bp = bp.sort_values(["startTime", "_need_t", "_orig_start"], kind="mergesort").reset_index(drop=True)
                print(f"  [BUILDING ECHO] day-pool sequencing ON: {len(bp)} rows re-ordered inside each "
                      f"(machine, plant-day) by curing need (GT stock {float(sum(_gtw.get(g, 0) for g in set(bp['_gt']))):,.0f} tyres netted first)")
            except Exception as _e:
                print(f"  [BUILDING ECHO] !! day-pool sequencing failed ({type(_e).__name__}: {_e}) -- file order kept")

        # sku -> BOM GT code, taken from phase2's GT lots so `item` matches what phase6/FG use.
        sku2gt = {}
        for _glid in building_lots:
            _s = _lot_sku1.get(_glid, "")
            if _s:
                sku2gt.setdefault(_s, lot_item.get(_glid, ""))
        # ── SHARED-GT FIX (2026-09-26, config `echo_kit_by_gt`) ─────────────────────────────
        # phase2 POOLS one green tyre across every SKU that uses it (skus_set "A, B"). Keying the
        # kit gate by skus_set[0] left SKU B with NO GT lot -> its plan rows were SKIPPED as
        # "unbuildable" (3,163 tyres) and phase6 cured them from nothing. The kit of a green tyre
        # is a property of the GT CODE, not of the SKU it is sold as, so key the gate by GT code
        # and resolve every plan SKU -> GT through the BOM (exact code, no fuzzy).
        _BY_GT = bool(cfg.get("echo_kit_by_gt", False))
        def _bkey(_glid):
            return (str(lot_item.get(_glid, "")).strip() if _BY_GT else _lot_sku1.get(_glid, ""))
        if _BY_GT:
            _built_gts = {str(lot_item.get(_g, "")).strip() for _g in building_lots}
            try:
                _bom = pd.read_csv(INPUTS / "bom.csv", dtype=str,
                                   usecols=["Super_parent", "child", "child_description"])
                _bom = _bom[_bom["child_description"].astype(str).str.strip().str.upper()
                            .isin(["GREEN TYRES", "GREEN_TYRE"])]
                _n_add = 0
                for _sp, _ch in zip(_bom["Super_parent"].astype(str).str.strip(),
                                    _bom["child"].astype(str).str.strip()):
                    if _ch in _built_gts and _sp not in sku2gt:
                        sku2gt[_sp] = _ch
                        _n_add += 1
                print(f"  [BUILDING ECHO] kit gate keyed by GT code: +{_n_add} SKU(s) resolved to a "
                      f"SHARED green-tyre lot via the BOM")
            except Exception as _e:
                print(f"  [BUILDING ECHO] echo_kit_by_gt: BOM read failed ({_e}) -- SKU keying kept")

        # Per-SKU component supply as a FEFO cumulative "tyres of kit ready" curve. For each
        # component ITEM feeding the SKU's GT lots: events (finish_ns + min_age, consume_qty). The
        # qty of item needed for D tyres is D * total_item_consume / total_GT_qty, so kit-ready(D) =
        # earliest ns where EVERY item's cumulative supply covers its share. Excluded components
        # (e.g. edge strip, set aside) have no placed lots -> absent -> not gated (available).
        sku_gt_qty = defaultdict(float)
        sku_item_ev = defaultdict(lambda: defaultdict(list))   # sku -> item -> [(avail_ns, qty)]
        for _glid in building_lots:
            _s = _bkey(_glid)
            if not _s:
                continue
            sku_gt_qty[_s] += float(lot_qty.get(_glid, 0.0) or 0.0)
            for _p in predecessors.get(_glid, ()):
                if _p not in schedule:
                    continue
                _it = lot_item.get(_p, "")
                _pmin, _ = _ages_ns(_it)
                _q = (edge_qty.get((_p, _glid), 0.0) if EDGE_QTY_ON
                      else float(lot_qty.get(_p, 0.0) or 0.0))
                if _q <= 0:
                    continue
                sku_item_ev[_s][_it].append((int(schedule[_p]["finish_ns"] + _pmin), _q))
        # pre-sort + prefix totals
        for _s, _items in sku_item_ev.items():
            for _it, _evs in _items.items():
                _evs.sort()
        sku_item_total = {s: {it: sum(q for _, q in evs) for it, evs in items.items()}
                          for s, items in sku_item_ev.items()}

        # ─── WARM-START RAMP FIX (v2, 2026-07-29) ────────────────────────────────────────────
        # The prior echo gated building ONLY on freshly-placed component lots, so during the ramp
        # (first 48h = ~7% of building) the supply curve sat near zero and EVERY block gated late
        # -> whole-drum slide -> the 58-day blow-up, even with warm start. Fix: credit OPENING
        # COMPONENT WIP exactly as `_material_ready_ns` does (`need -= open_alloc`, :2396-2401).
        # `open_alloc[(consumer_lot, item)]` is the FEFO-allocated opening stock (built :959-1006 from
        # load_wip_by_item -> the same June inventory.csv phase1c nets against). Aggregate it to the
        # SKU level: the first `wip` units of each item are covered on the floor at t0, so ramp blocks
        # are NOT gated late when the stock physically exists.
        _bld_set = set(building_lots)
        sku_item_wip = defaultdict(lambda: defaultdict(float))   # sku -> item -> opening WIP credited
        if open_alloc:
            for (_c, _i), _cov in open_alloc.items():
                if _cov > 0 and _c in _bld_set:
                    _s = _bkey(_c)
                    if _s:
                        sku_item_wip[_s][_i] += float(_cov)
        _wip_credit_tot = sum(v for d in sku_item_wip.values() for v in d.values())

        def _kit_ready(sku, cum_tyres):
            gtq = sku_gt_qty.get(sku, 0.0)
            _kit_ready.bind = ("", NEG_INF_NS)            # REPORT-ONLY: which item set `ready`
            if gtq <= 0:
                return NEG_INF_NS
            ready = NEG_INF_NS
            _wip = sku_item_wip.get(sku, {})
            for _it, _evs in sku_item_ev.get(sku, {}).items():
                tot = sku_item_total[sku][_it]
                # opening WIP covers the FIRST `wip` units of this item at t0 (mirror _material_ready_ns)
                thresh = cum_tyres * tot / gtq - _wip.get(_it, 0.0)
                if thresh <= 1e-6:
                    continue                            # fully covered by opening WIP -> no wait
                cum = 0.0
                t = _evs[-1][0]
                for avail_ns, q in _evs:
                    cum += q
                    if cum >= thresh - 1e-6:
                        t = avail_ns
                        break
                if t > ready:
                    ready = t
                    _kit_ready.bind = (_it, t)
            return ready

        prev_end = {}                     # buildMachine -> last finish ns (scalar cursor; OFF path)
        # ── BUILDING GAP-FILL (2026-07-30) ───────────────────────────────────────────────────
        # OFF (default): scalar `prev_end` cursor -- one kit-late block pushes EVERY later block on
        # that TBM (head-of-line), so a 46h slide on TUHL0 cascades from an earlier stuck block.
        # ON: place each block in the EARLIEST FREE SLOT on its own TBM at/after max(plan_start,
        # kit_ready) -- a kit-ready block fills idle time instead of queuing behind a stuck one
        # (the building twin of phase6 press_gap_fill). Resequences WITHIN a TBM only; never moves a
        # GT to another machine.
        import bisect as _bisect
        _GAPFILL = bool(cfg.get("building_gap_fill", False))
        tbm_busy = defaultdict(list)      # buildMachine -> sorted [(start_ns, finish_ns)]
        for _k, _ws in DOWNTIME_WIN.items():                # BR-10: PM windows on the TBMs
            for _w in _ws:
                _bisect.insort(tbm_busy[_k], _w)

        def _earliest_free_slot(_m, _earliest, _dur):
            _cur = _earliest
            for _s0, _f0 in tbm_busy[_m]:
                if _f0 <= _cur:
                    continue                  # busy interval entirely before us
                if _s0 >= _cur + _dur:
                    return _cur               # fits in the gap before this interval
                if _f0 > _cur:
                    _cur = _f0                # overlaps -> jump past it
            return _cur

        # ── V23 BUILDING CAPACITY (2026-10-06, config capacity_rules_v23.building) ─────────────
        # Build time = qty x routing cycle sec / routing efficiency (JK_CTP_PPDS.CTP_Master_Routing,
        # GT BUILDING, by GT code); every GT change on a TBM is charged the changeover matrix's
        # DIFFERENT-SIZE time of its line (TBR Building 'Size Change', PCR Building BJ 'Inch to Inch').
        # The slot search keeps that gap on BOTH sides of a block, so build + changeover never
        # exceeds the TBM's time. OFF -> plan cycleTime, no changeover (v22).
        _BC = (cfg.get("capacity_rules_v23") or {}).get("building") or {}
        _BC_ON = bool(_BC.get("enabled", False))
        _B_cyc, _B_co, _tbm_gt = {}, {}, {}
        if _BC_ON:
            try:
                _rr = pd.read_csv(ROOT / "db_pipeline" / "raw" / "JK_CTP_PPDS__CTP_Master_Routing.csv",
                                  dtype=str, low_memory=False)
                _rr = _rr[_rr["operation_name"].astype(str).str.strip() == "GT BUILDING"]
                _sec = pd.to_numeric(_rr["proc_time"], errors="coerce")
                _eff = (pd.to_numeric(_rr["efficiency"], errors="coerce").fillna(1.0)
                        if _BC.get("use_routing_efficiency", True) else pd.Series(1.0, index=_rr.index))
                if cfg.get("efficiency_override") is not None:       # v37 (user): efficiency taken as 1
                    _eff = pd.Series(float(cfg["efficiency_override"]), index=_rr.index)
                for _g0, _s0, _e0 in zip(_rr["routed_product"].astype(str).str.strip(), _sec, _eff):
                    if pd.notna(_s0) and _s0 > 0 and _e0 > 0:
                        _B_cyc[_g0] = max(_B_cyc.get(_g0, 0.0), float(_s0) / float(_e0) / 60.0)   # min / tyre
                _cm = pd.read_csv(INPUTS / "changeover_matrix.csv", low_memory=False,
                                  usecols=["machine", "changeover_type", "changeover_time_min"])
                _ct = _cm["changeover_type"].fillna("").astype(str).str.lower()
                _ctm = pd.to_numeric(_cm["changeover_time_min"], errors="coerce")
                for _ln, _pat in ((_BC.get("tbr_line", "TBR Building"), "size change"),
                                  (_BC.get("pcr_line", "PCR Building BJ"), "inch to inch")):
                    _v = _ctm[(_cm["machine"].astype(str).str.strip() == _ln) & _ct.str.contains(_pat)].max()
                    _B_co["TBR" if "TBR" in _ln else "PCR"] = float(_v) if pd.notna(_v) else 0.0
                print(f"  [V23 BUILD CAP] routing cycle/eff for {len(_B_cyc)} GTs; different-size changeover "
                      f"PCR {_B_co.get('PCR', 0):g} min, TBR {_B_co.get('TBR', 0):g} min (changeover matrix)")
            except Exception as _e:
                print(f"  [V23 BUILD CAP] !! masters unreadable ({type(_e).__name__}: {_e}) -- OFF")
                _BC_ON = False

        def _bco_ns(_m, _ga, _gb):
            if not _BC_ON or not _ga or not _gb or _ga == _gb:
                return 0
            return int(_B_co.get("PCR" if str(_m).upper().startswith("TBMPCR") else "TBR", 0.0) * 60 * NS)

        def _bdur(_q, _cyc_plan, _gt):
            _c = _B_cyc.get(_gt) if _BC_ON else None
            return max(int(round(_q * (_c if _c else (_cyc_plan if _cyc_plan > 0 else 1.0)) * 60 * NS)), 1)

        def _slot(_m, _earliest, _dur, _gt):
            """earliest start on TBM _m with the different-size changeover kept before AND after."""
            if not _BC_ON:
                return _earliest_free_slot(_m, _earliest, _dur)
            _cur, _prev = _earliest, None
            for _s0, _f0 in tbm_busy[_m]:
                _g0 = _tbm_gt.get((_m, _s0, _f0))
                if _f0 <= _cur:
                    _prev = (_f0, _g0)
                    continue
                if _prev is not None:
                    _cur = max(_cur, _prev[0] + _bco_ns(_m, _prev[1], _gt))
                if _s0 >= _cur + _dur + _bco_ns(_m, _gt, _g0):
                    return _cur
                _cur, _prev = max(_cur, _f0), (_f0, _g0)
            if _prev is not None:
                _cur = max(_cur, _prev[0] + _bco_ns(_m, _prev[1], _gt))
            return _cur

        cum_by_sku = defaultdict(float)
        _bld_demand = defaultdict(list)   # sku -> [(committed_start_ns, qty)] for the post-echo re-cut

        # ── C2 (2026-09-27): PHYSICAL MATERIAL-LEDGER KIT GATE (config `echo_kit_ledger`) ──────
        # The edge-based gate above reads phase3's QUANTITY SPLIT (ties broken alphabetically;
        # any uncovered remainder charged to the LAST producer lot) -> a small gap became a
        # multi-day wait (GT 5069 waited for BEF001_L0009 at 29-Sep 21:25 while 10,524 m of
        # BEF001 was physically on hand at 27-Sep 07:00 against 6,505 m needed).
        # The ledger answers the physical question instead: per component ITEM, supply =
        # alive opening WIP (t0) + every lot of that item at finish+min_age, MINUS what phase3
        # allocates to NON-building consumers; consumption = each committed build block's
        # tyres x the SKU's per-tyre BOM quantity, in commit order. A block's kit is ready at
        # the first time every item's cumulative supply covers cumulative consumption. Aging
        # stays a REPORT (user directive 2026-07-16) -- expiry is not used to reject supply.
        _LEDGER = bool(cfg.get("echo_kit_ledger", False))
        _L_rate, _L_sup, _L_wip, _L_cons, _L_commits = {}, {}, {}, defaultdict(float), []
        if _LEDGER:
            try:
                _lb = pd.read_csv(INPUTS / "bom.csv", dtype=str,
                                  usecols=["Super_parent", "Parent", "child", "child_quantity"])
                _lb = _lb.assign(sp=_lb["Super_parent"].str.strip(), pa=_lb["Parent"].str.strip(),
                                 ch=_lb["child"].str.strip(),
                                 q=pd.to_numeric(_lb["child_quantity"], errors="coerce").fillna(0.0))
                _gtset = set(_lb.loc[_lb["pa"] == _lb["sp"], "ch"])
                _lb = _lb[_lb["pa"].isin(_gtset) & (_lb["pa"] != _lb["sp"]) & (_lb["q"] > 0)]
                for (_sp, _ch), _qq in _lb.groupby(["sp", "ch"])["q"].sum().items():
                    _L_rate.setdefault(_sp, {})[_ch] = float(_qq)
                # supply lots per item (non-building). The share of an item that goes to NON-building
                # consumers (e.g. GUM STRIP -> TBR STC belts) is taken from the BOM, NOT from phase3's
                # edge split: phase3's per-edge rate divides the SKU's TOTAL child demand (all routes)
                # by one parent's demand, so each STC lot was charged the green tyres' share too
                # (GUM STRIP: 50,250 to STC vs 46,868 made -> false starvation of 101 builds).
                # GT share f(item) = building-plan tyres x per-tyre BOM qty on the GT->item edge,
                # divided by the item's total gross demand (phase1b, all routes). Each lot and the
                # opening WIP supply building with that share.
                _items_needed = {c for d_ in _L_rate.values() for c in d_}
                _gt_need = defaultdict(float)
                for _, _r0 in bp.iterrows():
                    for _it0, _rq in _L_rate.get(str(_r0["skuCode"]).strip(), {}).items():
                        _gt_need[_it0] += float(_r0["qty"]) * _rq
                _gross_tot = defaultdict(float)
                try:
                    _gd = pd.read_csv(max([q_ for q_ in (OUTPUTS / "phase1_demand_updated.csv.gz",
                                                         OUTPUTS / "phase1_demand_updated.csv") if q_.exists()],
                                          key=lambda q_: q_.stat().st_mtime),
                                      usecols=["item_code", "demand_qty"])
                    for _i0, _q0 in _gd.groupby(_gd["item_code"].astype(str).str.strip())["demand_qty"].sum().items():
                        _gross_tot[_i0] = float(_q0)
                except Exception:
                    pass
                _L_share = {i: (min(1.0, _gt_need[i] / _gross_tot[i]) if _gross_tot.get(i, 0) > 0 else 1.0)
                            for i in _items_needed}
                _n_shared = sum(1 for v in _L_share.values() if v < 0.999)
                for _lid, _sc in schedule.items():
                    _it = lot_item.get(_lid, "")
                    if _it in _items_needed and str(_sc.get("department", "")).upper() != "BUILDING":
                        _qv = float(lot_qty.get(_lid, 0.0) or 0.0) * _L_share.get(_it, 1.0)
                        if _qv > 0:
                            _L_sup.setdefault(_it, []).append((_lid, _qv))
                # opening WIP per item (alive at plan start), net of non-building allocations
                _inv = pd.read_csv(INPUTS / "inventory.csv", low_memory=False)
                _inv["itemcode"] = _inv["itemcode"].astype(str).str.strip()
                _inv = _inv[_inv["itemcode"].isin(_items_needed)]
                _q = pd.to_numeric(_inv["inventory"], errors="coerce").fillna(0.0)
                _u = _inv.get("unit", pd.Series("", index=_inv.index)).astype(str).str.upper().str.strip()
                _q = _q * _u.map({"MM": 0.001, "CM": 0.01}).fillna(1.0)
                _t0L = pd.Timestamp(int(bp["startTime"].min().value))
                _pt = pd.to_datetime(_inv.get("produced_time"), errors="coerce", format="mixed")
                _mx = _inv["itemcode"].map(lambda i: _ages_ns(i)[1] / (3600.0 * NS))
                _alive = _pt.isna() | ((_t0L - _pt).dt.total_seconds() / 3600.0 <= _mx)
                for _it, _qq in pd.Series(_q[_alive].values, index=_inv.loc[_alive, "itemcode"]).groupby(level=0).sum().items():
                    _L_wip[_it] = float(_qq)
                for _i in list(_L_wip):
                    _L_wip[_i] = _L_wip[_i] * _L_share.get(_i, 1.0)
                print(f"  [C2 LEDGER] {_n_shared} GT-level items also feed non-building consumers -> building "
                      f"gets its BOM share (e.g. GUM STRIP {100*_L_share.get('GUM STRIP', 1.0):.0f}%)")
                print(f"  [C2 LEDGER] kit gate = physical material ledger: {len(_L_rate)} SKUs, "
                      f"{len(_items_needed)} GT-level component items, {sum(len(v) for v in _L_sup.values())} "
                      f"supply lots, opening WIP on {len(_L_wip)} items")
            except Exception as _e:
                print(f"  [C2 LEDGER] !! setup failed ({type(_e).__name__}: {_e}) -- edge gate kept")
                _LEDGER = False

        def _L_curve(_it):
            """sorted [(avail_ns, qty)] for one item from the CURRENT schedule (+WIP at -inf)."""
            _mn = _ages_ns(_it)[0]
            _ev = [(int(schedule[_L]["finish_ns"] + _mn), _qv) for _L, _qv in _L_sup.get(_it, ())]
            if _L_wip.get(_it, 0.0) > 0:
                _ev.append((NEG_INF_NS, _L_wip[_it]))
            _ev.sort()
            return _ev

        def _L_ready_at(_it, _cum_need, _ev):
            _c = 0.0
            _tol = max(1e-6, 1e-4 * _cum_need)        # lot_qty is rounded to 0.01 upstream
            for _a, _qv in _ev:
                _c += _qv
                if _c >= _cum_need - _tol:
                    return _a
            return None                                   # never covered (true shortage)

        # ASSUMED-AVAILABLE CHAINS (2026-09-29, config `kit_gate_assume_available`): the plant's V5
        # planner runs its own cap-strip / edge-gum upstream check and caps the building plan so that
        # "building never waits for cap strip or edge gum". Items matching these patterns are still
        # produced and aged, but never hold a build block in the kit gate / ledger proof.
        import re as _re
        _AA = [_re.compile(str(p_), _re.I) for p_ in (cfg.get("kit_gate_assume_available") or [])]
        _AA_cache = {}

        def _assumed(_it):
            if _it not in _AA_cache:
                _AA_cache[_it] = any(r_.search(str(_it)) for r_ in _AA)
            return _AA_cache[_it]

        # v79 (user 2026-10-12): FIRST-HOURS ASSUMPTION -- cap strip, edge gum, edge strip and steel belt edge strip are
        # assumed present for build rows PLANNED in the first `hours` of the plan (config kit_gate_assume_first_h:
        # item_types from the item master + code patterns). Those rows are not held for them and do not draw them
        # from the ledger; every later row is gated on them as usual.
        _AF = cfg.get("kit_gate_assume_first_h") or {}
        _AF_ON = bool(_AF.get("enabled", False))
        _AF_END = None
        _AF_TYPES = {str(x).strip().upper() for x in (_AF.get("item_types") or [])}
        _AF_PAT = [_re.compile(str(p_), _re.I) for p_ in (_AF.get("patterns") or [])]
        _AF_cache = {}
        _AF_ity = {}
        _AF_CODES = set()                                  # v80: codes absent from the WIP feed (CTB #1)
        if _AF_ON and _AF.get("codes_file"):
            try:
                _AF_CODES = set(pd.read_csv(INPUTS / str(_AF.get("codes_file")), dtype=str)["itemcode"].astype(str).str.strip())
                print(f"  [FIRST-H ASSUME v80] + {len(_AF_CODES)} building components absent from the WIP feed (CTB #1)")
            except Exception as _e:
                print(f"  [FIRST-H ASSUME v80] !! codes_file unreadable ({_e})")
        if _AF_ON:
            try:
                _im = pd.read_csv(INPUTS / "itemtype_master.csv", dtype=str)
                _AF_ity = dict(zip(_im["ItemCode"].astype(str).str.strip(), _im["ItemType"].astype(str).str.strip().str.upper()))
            except Exception as _e:
                print(f"  [FIRST-H ASSUME v79] !! item master unreadable ({_e}) -- patterns only")
            _AF_END = int(pd.Timestamp(bp["startTime"].min()).value + float(_AF.get("hours", 24)) * 3600 * NS)
            print(f"  [FIRST-H ASSUME v79] build rows planned before {pd.Timestamp(_AF_END)} assume {sorted(_AF_TYPES)} "
                  f"+ {[p_.pattern for p_ in _AF_PAT]} present")
        _L_day1 = {}                                       # build lot -> planned inside the first-hours window

        def _first_item(_it):
            if _it not in _AF_cache:
                _AF_cache[_it] = ((_AF_ity.get(str(_it).strip(), "") in _AF_TYPES) or any(r_.search(str(_it)) for r_ in _AF_PAT)
                                  or str(_it).strip() in _AF_CODES)
            return _AF_cache[_it]

        def _assumed_at(_it, _plan_t):
            return _assumed(_it) or (_AF_ON and _plan_t is not None and _plan_t < _AF_END and _first_item(_it))

        def _L_ready(_sku, _q, _t0):
            _rd, _bind = NEG_INF_NS, ""
            for _it, _r in _L_rate.get(_sku, {}).items():
                if _assumed_at(_it, _t0):
                    continue
                if _it not in _L_sup and _it not in _L_wip:
                    continue                              # not produced, not stocked: not gated
                _ev = _L_curve(_it)
                _t = _L_ready_at(_it, _L_cons[_it] + _q * _r, _ev)
                if _t is None:
                    _t = _ev[-1][0] if _ev else NEG_INF_NS   # fail-safe: wait for the last supply
                if _t > _rd:
                    _rd, _bind = _t, _it
            return _rd, _bind

        def _L_take(_sku, _q, _plan_t=None):
            for _it, _r in _L_rate.get(_sku, {}).items():
                if (_it in _L_sup or _it in _L_wip) and not (_plan_t is not None and _assumed_at(_it, _plan_t)):
                    _L_cons[_it] += _q * _r

        def _L_replay(_tol_ns):
            """Re-check every committed block against the CURRENT schedule. Returns (bad_items, n_bad, rows)."""
            _cum, _bad_items, _n_bad, _rows = defaultdict(float), set(), 0, []
            _curves = {}
            # physical order = build START order (commit order differs once gap-fill / kit-late re-order
            # blocks; a block committed early but starting late must not consume supply "first")
            for _st, _sku, _q, _lid in sorted(_L_commits):
                _late = False
                for _it, _r in _L_rate.get(_sku, {}).items():
                    if (_it not in _L_sup and _it not in _L_wip) or _assumed(_it) or (_L_day1.get(_lid) and _first_item(_it)):
                        continue
                    if _it not in _curves:
                        _curves[_it] = _L_curve(_it)
                    _cum[_it] += _q * _r
                    _t = _L_ready_at(_it, _cum[_it], _curves[_it])
                    _short = _t is None
                    if _short or _t > _st + _tol_ns:
                        _late = True
                        _bad_items.add(_it)
                    _rows.append((_lid, _sku, pd.Timestamp(int(_st)), _it, round(_cum[_it], 3),
                                  (pd.NaT if _short else pd.Timestamp(int(max(_t, bp["startTime"].min().value)))),
                                  _short or _t > _st + _tol_ns))
                _n_bad += int(_late)
            return _bad_items, _n_bad, _rows
        gt_itype = next((lots.loc[lots["item_type"].astype(str).str.upper() == "GREEN TYRES",
                                  "item_type"].iloc[0]
                         for _ in [0] if (lots["item_type"].astype(str).str.upper() == "GREEN TYRES").any()),
                        "Green Tyres")
        new_lot_rows, n_placed, n_kit_late, n_nogt = [], 0, 0, 0
        _gate_rows = []                                   # REPORT-ONLY: per-block kit gate
        _slide_ns_sum = 0
        _slide_ns_max = 0
        _kit_late_by_sku = defaultdict(int)               # makespan meter: who slides
        _n_skipped_unbuildable = 0
        _skipped_skus = set()
        # ── KIT-AWARE DISPATCH (2026-09-30, config `building_kit_aware_dispatch`) ─────────────
        # With a FIXED day order a row whose kit is late held its slot and every row behind it on
        # that TBM waited, though their material was ready: 30-Sep plan, 05-Oct had 15 TBMs idle
        # >4 h (158 TBM-h) while 52 rows started ~22 h late. Now, day by day, the next row placed
        # is the one that can START EARLIEST on its own TBM given the CURRENT material ledger
        # (ties -> curing-need order). Same TBMs, same quantities, never before its kit.
        # ── BR-V7 PLAN-FEASIBLE BUILDING (2026-10-01, BUSINESS_RULES_LOG.md) ────────────────
        # A row may start up to `pull_forward_h` BEFORE its plan shift when its kit is complete and
        # its own TBM is idle (same TBM, same qty). Dispatch looks ahead at the next day(s): a later
        # row is taken only if it can really start inside the current plant day; rows whose kit is
        # late fall back behind ready rows. The published result is a re-levelled building plan.
        _PFC = cfg.get("building_plan_feasible") or {}
        _PF_NS = (int(float(_PFC.get("pull_forward_h", 0)) * 3600 * NS)
                  if _PFC.get("enabled", False) else 0)
        _DAY_NS = int(24 * 3600 * NS)

        _NOPF_NS = int(float(_PFC.get("no_pull_first_h", 0) or 0) * 3600 * NS)   # v81 A4

        def _row_floor(_ps):
            if _NOPF_NS and _ps < SCHEDULE_OPEN_NS + _NOPF_NS:
                return _ps                                # v81 A4: no pull-forward in the plan's first hours
            return max(_ps - _PF_NS, SCHEDULE_OPEN_NS) if _PF_NS else _ps

        _b13 = cfg.get("building_priority") or {}
        _BR13 = {"on": bool(_b13.get("enabled", False)),
                 "starve_ns": int(float(_b13.get("starvation_h", 8)) * 3600 * NS),
                 "jump_ns": int(float(_b13.get("starvation_jump_min", 60)) * 60 * NS)}

        # CD-1..3 (v35, config cover_dispatch): each TBM, when free, takes among its ready rows (start within
        # ready_slack_min of its earliest) the URGENT one -- cover = time until the presses run out of the GT
        # below building_threshold_h -- with the lowest cover, ties to FEWER moulds; with nothing urgent the
        # same GT continues (no changeover), then the cheapest changeover, then the lowest cover.
        _CD = cfg.get("cover_dispatch") or {}
        _CD_ON = bool(_CD.get("enabled", False)) and "_need_t" in bp.columns
        _CD_THR = int(float(_CD.get("building_threshold_h", 8)) * 3600 * NS)
        _CD_SLACK = int(float(_CD.get("ready_slack_min", 60)) * 60 * NS)
        _CD_MOULDS = {}
        if _CD_ON:
            try:
                _pm = pd.read_csv(INPUTS / "plan.csv", usecols=["skuCode", "pressNo"], dtype=str).dropna()
                _pm["gt"] = _pm["skuCode"].str.strip().map(lambda s_: sku2gt.get(s_) or s_)
                _CD_MOULDS = _pm.groupby("gt")["pressNo"].nunique().to_dict()
            except Exception as _e:
                print(f"  [CD BUILD] !! curing plan unreadable for mould counts ({type(_e).__name__}: {_e})")
        _cd_stat = defaultdict(int)

        def _cd_pick(_cands):
            """_cands: [(j, m, st, need_ns, gt, rank)] -> j chosen by CD-1..3 (earliest-free TBM first)."""
            _bym = defaultdict(list)
            for _c in _cands:
                _bym[_c[1]].append(_c)
            _best = None
            for _m, _cs in _bym.items():
                _t0 = min(c[2] for c in _cs)
                _av = [c for c in _cs if c[2] <= _t0 + _CD_SLACK]
                _urg = [c for c in _av if c[3] - c[2] < _CD_THR]
                _last = _tbm_gt.get(_m)
                if _urg:
                    _ch = min(_urg, key=lambda c: (c[3] - c[2], _CD_MOULDS.get(c[4], 0), c[2], c[5]))
                    _kind = "urgent"
                else:
                    _ch = min(_av, key=lambda c: (int(c[4] != _last), _bco_ns(_m, _last, c[4]), c[3] - c[2], c[2], c[5]))
                    _kind = "same_gt" if _ch[4] == _last else "cheapest_co"
                if _best is None or (_t0, _ch[5]) < _best[0]:
                    _best = ((_t0, _ch[5]), _ch[0], _kind)
            _cd_stat[_best[2]] += 1
            return _best[1]

        _CAC69 = cfg.get("curing_aware_building") or {}
        _CA69 = (int((float(_CAC69.get("gt_life_h", 72)) - float(_CAC69.get("margin_h", 8))) * 3600 * NS)
                 if _CAC69.get("enabled", False) else 0)
        _CA69_PEN = int(float(_CAC69.get("penalty_h", 24)) * 3600 * NS)
        _ca69_n = [0]

        def _dispatch_order():
            if not (cfg.get("building_kit_aware_dispatch", False) and _LEDGER and _GAPFILL):
                yield from bp.iterrows()
                return
            _day = (bp["startTime"] - pd.Timedelta(hours=7)).dt.normalize()
            _days = sorted(_day.unique())
            _look = -(-_PF_NS // _DAY_NS) if _PF_NS else 0      # days of look-ahead
            _done = set()
            for _di, _d in enumerate(_days):
                _d_end = int((pd.Timestamp(_d) + pd.Timedelta(hours=31)).value)   # 07:00 next day
                _ahead = set(_days[_di + 1:_di + 1 + _look])
                _rem = [j for j in bp.index[_day == _d] if j not in _done]
                _pool = _rem + [j for j in bp.index[_day.isin(_ahead)] if j not in _done]
                while _rem:
                    _best = None
                    _cdc = []
                    for _rank, _j in enumerate(_pool):
                        _r = bp.loc[_j]
                        _s = str(_r["skuCode"]).strip()
                        if not sku2gt.get(_s):
                            if _j in _rem:
                                _best = (NEG_INF_NS, _rank, _j)
                                break
                            continue
                        _q = float(_r["qty"]); _c = float(_r.get("cycleTime", 0) or 0)
                        _du = _bdur(_q, _c, sku2gt.get(_s))
                        _ps = int(_r["startTime"].value)
                        _kr, _ = _L_ready(_s, _q, _ps)
                        _st = _slot(str(_r["buildMachine"]).strip(), max(_row_floor(_ps), _kr), _du, sku2gt.get(_s))
                        if _j not in _rem and _st >= _d_end:
                            continue                          # a later day's row that can't start today
                        if _CD_ON:
                            _nt0 = _r["_need_t"]
                            _nns = (int(pd.Timestamp(_nt0).value) if pd.notna(_nt0) and _nt0 != pd.Timestamp.max
                                    else _st + 10 ** 6 * 3600 * NS)
                            _cdc.append((_j, str(_r["buildMachine"]).strip(), _st, _nns, sku2gt.get(_s), _rank))
                            continue
                        # BR-1 / BR-3 (v32): feed-hours = time until the presses run out of this GT (curing
                        # need after GT stock) minus the start; a row under starvation_h may jump ahead of a
                        # row that starts up to starvation_jump_min earlier (protect starvation-risk SKUs)
                        _key_st = _st
                        # v69 #5 (user 2026-10-10): CURING-AWARE -- a row whose tyres would wait longer than the green-tyre
                        # life (minus margin) for their presses goes behind rows the presses can take in time (it is
                        # still built, never dropped; GT 72 h rule unchanged)
                        if _CA69 and "_need_t" in _r and pd.notna(_r["_need_t"]) and _r["_need_t"] != pd.Timestamp.max:
                            if pd.Timestamp(_r["_need_t"]).value - (_st + int(_du)) > _CA69:
                                _key_st = _st + _CA69_PEN
                                _ca69_n[0] += 1
                        if _BR13["on"] and "_need_t" in _r and pd.notna(_r["_need_t"]) and _r["_need_t"] != pd.Timestamp.max:
                            if (pd.Timestamp(_r["_need_t"]).value - _st) <= _BR13["starve_ns"]:
                                _key_st = _st - _BR13["jump_ns"]
                        if _best is None or (_key_st, _rank) < _best[:2]:
                            _best = (_key_st, _rank, _j)
                    if _best is None or _best[0] != NEG_INF_NS:
                        if _cdc:
                            _best = (0, 0, _cd_pick(_cdc))
                        elif _best is None:                   # nothing startable today: earliest remaining row
                            _best = (0, 0, _rem[0])
                    _j = _best[2]
                    _pool.remove(_j)
                    if _j in _rem:
                        _rem.remove(_j)
                    _done.add(_j)
                    yield _j, bp.loc[_j]
            if _CD_ON:
                print(f"  [CD BUILD] cover-first dispatch (threshold {_CD_THR / 3600 / NS:g} h, ties: fewer moulds): "
                      f"{_cd_stat['urgent']} urgent picks, {_cd_stat['same_gt']} same-GT continuations, "
                      f"{_cd_stat['cheapest_co']} cheapest-changeover picks")

        for i, r in _dispatch_order():
            sku = str(r["skuCode"]).strip()
            m   = str(r["buildMachine"]).strip()
            q   = float(r["qty"])
            cyc = float(r.get("cycleTime", 0) or 0)      # minutes / tyre (from the plan)
            gt  = sku2gt.get(sku) or str(r.get("gtCode", "")).strip()
            if not sku2gt.get(sku):
                n_nogt += 1                               # SKU absent from phase2 GT lots (data gap)
                # USER DIRECTIVE 2026-07-30: a GT with no routing/BOM (no proc_time/machine) CANNOT be
                # built and only wastes schedule time -- do NOT build it and do NOT carry its demand.
                # (Its cure, if any, is flagged separately as CURED WITHOUT A GREEN TYRE.)
                if bool(cfg.get("skip_unbuildable_building_rows", True)):
                    _n_skipped_unbuildable += 1
                    _skipped_skus.add(sku)
                    continue
            plan_start = int(r["startTime"].value)
            dur_ns = _bdur(q, cyc, gt) if _BC_ON else int(round(q * (cyc if cyc > 0 else 1.0) * 60 * NS))
            _kk = gt if _BY_GT else sku                  # kit-gate key (GT code when echo_kit_by_gt)
            cum_by_sku[_kk] += q
            kr = _kit_ready(_kk, cum_by_sku[_kk])
            if _LEDGER:                                   # C2: physical ledger decides
                kr, _lbind = _L_ready(sku, q, plan_start)
                _kit_ready.bind = (_lbind, kr)
            if _GAPFILL:
                start = _slot(m, max(_row_floor(plan_start), kr), max(dur_ns, 1), gt)
                _is_kit_late = kr > plan_start
            else:
                base = max(plan_start, prev_end.get(m, NEG_INF_NS))
                start = max(base, kr)
                _is_kit_late = kr > base
            _gate_rows.append({                           # REPORT-ONLY gate log (no effect on placement)
                "row": i, "sku": sku, "tbm": m, "gt": gt, "qty": q,
                "plan_start": pd.Timestamp(plan_start), "kit_ready": pd.Timestamp(int(kr)) if kr > NEG_INF_NS else pd.NaT,
                "binding_item": _kit_ready.bind[0],
                "start": pd.Timestamp(int(start)), "kit_late": bool(_is_kit_late)})
            if _is_kit_late:
                n_kit_late += 1
                _kit_late_by_sku[sku] += 1
            _sl = start - plan_start                      # how far this block slid past its plan slot
            if _sl > 0:
                _slide_ns_sum += _sl
                if _sl > _slide_ns_max:
                    _slide_ns_max = _sl
            finish = start + max(dur_ns, 1)
            if _LEDGER:
                _L_take(sku, q, plan_start)
                _L_commits.append((int(start), sku, q, f"BLD{i:05d}"))
                _L_day1[f"BLD{i:05d}"] = bool(_AF_ON and int(pd.Timestamp(plan_start).value) < _AF_END)
            if _GAPFILL:
                _bisect.insort(tbm_busy[m], (start, finish))
                _tbm_gt[(m, start, finish)] = gt
            else:
                if _BC_ON and _tbm_gt.get(m) is not None:
                    start = max(start, prev_end.get(m, NEG_INF_NS) + _bco_ns(m, _tbm_gt[m], gt))
                    finish = start + max(dur_ns, 1)
                prev_end[m] = finish
                _tbm_gt[m] = gt
            lid = f"BLD{i:05d}"
            schedule[lid] = {
                "lot_id": lid, "item": gt, "machine": m,
                "operation": "GT BUILDING", "department": "BUILDING", "equipment": m,
                "scheduled_start": pd.Timestamp(int(start)),
                "scheduled_finish": pd.Timestamp(int(finish)),
                "duration_h": (finish - start) / (3600.0 * NS),
                "campaign_id": "", "wire_type": "",
                "is_camp_first": False, "is_camp_last": False, "finish_ns": int(finish),
            }
            new_lot_rows.append({"lot_id": lid, "lot_qty": q, "lot_uom": "NOS",
                                 "item_type": gt_itype, "operation": "GT BUILDING",
                                 "department": "BUILDING", "item_code": gt})
            _bld_demand[_kk].append((int(start), q))   # frozen build demand for the re-cut
            n_placed += 1

        if _gate_rows:                                    # REPORT-ONLY: why each block started when it did
            _gdf = pd.DataFrame(_gate_rows)
            _gdf.to_csv(OUTPUTS / "phase5_building_echo_gates.csv", index=False)
            if _PF_NS:                                    # BR-V7: the re-levelled (plan-feasible) plan
                _sl = (_gdf["start"] - _gdf["plan_start"]).dt.total_seconds() / 3600
                print(f"  [BR-V7] plan-feasible building: pull-forward {_PF_NS / 3600 / NS:.0f} h; "
                      f"{int((_sl < -0.5).sum())} rows start EARLIER than their plan slot "
                      f"({float(-_sl[_sl < -0.5].sum()):,.0f} h pulled forward), "
                      f"{int((_sl > 0.5).sum())} later, {int(((_sl >= -0.5) & (_sl <= 0.5)).sum())} on time")
        # ── POST-ECHO SHORT-SHELF RE-CUT (2026-07-30) ────────────────────────────────────
        # Build times are now FROZEN. The main dispatcher cut belt/ply JIT to their PHASE2
        # need, but the echo then slid some building blocks later (the drum honestly waiting
        # for its full kit), stranding the already-cut short-shelf component 3-4 days before
        # its ACTUAL build -> over-aged (the belt lots >72h). Re-cut each stranded belt/ply lot
        # LATER into its OWN cutter's existing idle, JIT to the real draw. Makespan-neutral: no
        # BLD/TBM or critical-path lot moves -- only non-constraint cutter work shifts later into
        # idle, and there is no path back to the (already-frozen) kit gate. FEFO-guarded: a lot
        # is never re-cut past the first building block the EARLIER lots+WIP cannot cover, so
        # every committed block keeps its cumulative supply. Flag-off = byte-identical.
        if cfg.get("post_echo_recut_short_shelf", False) and _bld_demand:
            _SS = {str(t).strip().upper() for t in (cfg.get("recut_short_shelf_itypes")
                                                    or ["Steel Belt", "Ply"])}
            _TOL_NS = int(2 * 3600 * NS)
            _ss_groups = defaultdict(dict)                 # (sku,item) -> {lot: cut_qty}
            for _glid in building_lots:
                _s = _bkey(_glid)
                if not _s:
                    continue
                for _p in predecessors.get(_glid, ()):
                    if _p in schedule and lot_item_type.get(_p, "") in _SS:
                        # group by item CODE (matches sku_item_total/ev), filter by item TYPE.
                        # SUPPLY basis MUST match the echo's kit gate exactly: edge_qty summed over
                        # this lot's edges (EDGE_QTY_ON), else lot_qty once. Overcounting supply would
                        # move lots too late and starve blocks (the re-cut VERIFY caught this).
                        _ic = lot_item.get(_p, "")
                        if EDGE_QTY_ON:
                            _ss_groups[(_s, _ic)][_p] = (_ss_groups[(_s, _ic)].get(_p, 0.0)
                                                         + float(edge_qty.get((_p, _glid), 0.0) or 0.0))
                        else:
                            _ss_groups[(_s, _ic)][_p] = float(lot_qty.get(_p, 0.0) or 0.0)
            _lot_bound = {}                                # lot -> latest allowed FINISH ns (tightest)
            for (_s, _it), _lotmap in _ss_groups.items():
                _dem = sorted(_bld_demand.get(_s, []))
                _gtq = sku_gt_qty.get(_s, 0.0)
                _tot = sku_item_total.get(_s, {}).get(_it, 0.0)
                if not _dem or _gtq <= 0 or _tot <= 0:
                    continue
                _pmin, _ = _ages_ns(_it)
                _scum = sku_item_wip.get(_s, {}).get(_it, 0.0)     # opening WIP is the first supply
                _dcum, _c = [], 0.0
                for _st, _q in _dem:
                    _c += _q * _tot / _gtq
                    _dcum.append((_st, _c))
                _order = sorted(_lotmap, key=lambda L: schedule[L]["finish_ns"])
                _ofin = [schedule[L]["finish_ns"] for L in _order]   # ORIGINAL finishes (FEFO order)
                for _k, _L in enumerate(_order):
                    _la = _dem[-1][0]                       # default: last block start
                    for _st, _dc in _dcum:
                        if _dc > _scum + 1e-6:              # first block earlier supply can't cover
                            _la = _st
                            break
                    _scum += _lotmap[_L]
                    _lf = _la - _pmin                       # FEFO-JIT finish for this lot
                    if _k + 1 < len(_order):                # ORDER-PRESERVING CAP: never overtake the
                        _lf = min(_lf, _ofin[_k + 1])       # next lot's ORIGINAL avail, so the FEFO order
                    _lot_bound[_L] = min(_lot_bound.get(_L, _lf), _lf)   # (and its proven cover) survives
            _lot_skus = defaultdict(set)                   # lot -> skus it feeds (for targeted revert)
            for (_s2, _ic2), _lm2 in _ss_groups.items():
                for _L2 in _lm2:
                    _lot_skus[_L2].add(_s2)
            _n_recut = _n_resid = 0
            _orig = {}                                     # moved lot -> (orig_start_ns, orig_finish_ns)
            for _L, _lf in _lot_bound.items():
                _cs = schedule[_L]["scheduled_start"].value
                _cf = schedule[_L]["finish_ns"]
                _dur = _cf - _cs
                if _lf <= _cf + _TOL_NS:                    # already JIT (or the pool needs it early)
                    continue
                _m = str(schedule[_L]["machine"])
                _busy = machine_busy[_m]
                # Remove this lot's OWN interval (match start AND end>=finish so we never delete a
                # DIFFERENT lot that happens to share the start) so the freed slot is reusable and the
                # cutter is not phantom-filled -- else late-processed lots stay stranded. Then take the
                # LATEST free slot up to the FEFO deadline.
                for _ix in range(len(_busy)):
                    if _busy[_ix][0] == _cs:
                        _busy.pop(_ix)
                        break
                _ns = latest_fit(_busy, _cs, _lf - _dur, _dur)
                if _ns is None or _ns <= _cs + _TOL_NS:
                    _bisect.insort(_busy, (_cs, _cf))       # no fresher slot -> restore, leave in place
                    _n_resid += 1
                    continue
                _nf = _ns + _dur
                _bisect.insort(_busy, (_ns, _nf))
                _orig[_L] = (_cs, _cf)
                schedule[_L]["scheduled_start"] = pd.Timestamp(int(_ns))
                schedule[_L]["scheduled_finish"] = pd.Timestamp(int(_nf))
                schedule[_L]["finish_ns"] = int(_nf)
                _n_recut += 1

            # VERIFY against the echo's OWN kit gate (the authority) + REVERT to safety. Recompute
            # _kit_ready with the NEW finishes; any committed block that now starts before its kit is a
            # real starve -> revert every re-cut of that block's SKU and re-check. Converges (a revert
            # only restores an earlier, safer finish). End state: 0 starves, guaranteed executable.
            def _verify_recut():
                if _LEDGER:                               # C2: the ledger is the authority
                    _bi, _nb, _ = _L_replay(int(0.25 * 3600 * NS))   # same 15-min tolerance as the C4 proof
                    return {(None, _i) for _i in _bi}
                _ev = defaultdict(lambda: defaultdict(list))
                for _g in building_lots:
                    _s3 = _bkey(_g)
                    if not _s3:
                        continue
                    for _p in predecessors.get(_g, ()):
                        if _p not in schedule:
                            continue
                        _i3 = lot_item.get(_p, "")
                        _mn3, _ = _ages_ns(_i3)
                        _q3 = (edge_qty.get((_p, _g), 0.0) if EDGE_QTY_ON else float(lot_qty.get(_p, 0.0) or 0.0))
                        if _q3 > 0:
                            _ev[_s3][_i3].append((int(schedule[_p]["finish_ns"] + _mn3), _q3))
                for _s3, _d3 in _ev.items():
                    for _i3, _e3 in _d3.items():
                        _e3.sort()
                _tt = {s: {it: sum(q for _, q in ev) for it, ev in d.items()} for s, d in _ev.items()}
                _bad, _cc = set(), defaultdict(float)     # set of (sku, item) that starve a block
                for _s3, _bl in _bld_demand.items():
                    _g3 = sku_gt_qty.get(_s3, 0.0)
                    if _g3 <= 0:
                        continue
                    _w3 = sku_item_wip.get(_s3, {})
                    for _st3, _q3 in _bl:
                        _cc[_s3] += _q3
                        for _i3, _e3 in _ev.get(_s3, {}).items():
                            _th3 = _cc[_s3] * _tt[_s3][_i3] / _g3 - _w3.get(_i3, 0.0)
                            if _th3 <= 1e-6:
                                continue
                            _cs3, _t3 = 0.0, _e3[-1][0]
                            for _a3, _qq in _e3:
                                _cs3 += _qq
                                if _cs3 >= _th3 - 1e-6:
                                    _t3 = _a3
                                    break
                            if _st3 < _t3 - _TOL_NS:      # THIS item's crossing is late for THIS block
                                _bad.add((_s3, _i3))
                return _bad

            _n_reverted = 0
            for _pass in range(60):
                _bad = _verify_recut()
                if not _bad:
                    break
                _rev = 0
                # FINE revert: undo only the OFFENDER -- the single latest-moved lot of the exact
                # (SKU, ITEM) that starves -- not the whole SKU and not another item. This keeps every
                # innocent move (e.g. belt B1-PBLT009_L0001 when the real offender is a PLY lot).
                for _s5, _i5 in _bad:
                    _cand = [_L for _L in _orig
                             if (_s5 is None or _s5 in _lot_skus.get(_L, set()))
                             and lot_item.get(_L, "") == _i5]
                    if not _cand:
                        continue
                    _L = max(_cand, key=lambda L: schedule[L]["finish_ns"])
                    _cs4, _cf4 = _orig.pop(_L)
                    schedule[_L]["scheduled_start"] = pd.Timestamp(int(_cs4))
                    schedule[_L]["scheduled_finish"] = pd.Timestamp(int(_cf4))
                    schedule[_L]["finish_ns"] = int(_cf4)
                    _rev += 1
                if not _rev:
                    break
                _n_reverted += _rev
                _n_recut -= _rev
            # FINAL: the re-cut must not double-book a cutter -- an overlap the shift-split render-snap
            # would truncate into a phantom sub-minute / zero-duration row. For each machine that got
            # moves, revert any moved lot whose new interval overlaps a neighbour back to its original
            # (overlap-free) slot; repeat until clean. Reverting only restores earlier positions, so the
            # kit gate stays satisfied.
            _mvd_m = {str(schedule[_L]["machine"]) for _L in _orig}
            _n_ovl = 0
            for _op in range(40):
                _iv = defaultdict(list)
                for _lid2, _sc2 in schedule.items():
                    _mm2 = str(_sc2.get("machine", ""))
                    if _mm2 in _mvd_m and str(_sc2.get("department", "")).upper() != "BUILDING":
                        _iv[_mm2].append((_sc2["scheduled_start"].value, int(_sc2["finish_ns"]), _lid2))
                _revd = 0
                for _mm2, _lst in _iv.items():
                    _lst.sort()
                    for _z in range(len(_lst) - 1):
                        _s1, _f1, _l1 = _lst[_z]
                        _s2, _f2, _l2 = _lst[_z + 1]
                        if _s2 < _f1:                              # overlap on this cutter
                            _rl = _l2 if _l2 in _orig else (_l1 if _l1 in _orig else None)
                            if _rl is not None:
                                _cs5, _cf5 = _orig.pop(_rl)
                                schedule[_rl]["scheduled_start"] = pd.Timestamp(int(_cs5))
                                schedule[_rl]["scheduled_finish"] = pd.Timestamp(int(_cf5))
                                schedule[_rl]["finish_ns"] = int(_cf5)
                                _revd += 1; _n_recut -= 1; _n_ovl += 1
                if not _revd:
                    break
            _final_bad = _verify_recut()
            print(f"  [BUILDING ECHO] post-echo short-shelf re-cut: {_n_recut} belt/ply lots re-timed JIT to "
                  f"their real draw (build times frozen); {_n_resid} cutter-bound, {_n_reverted} reverted (kit), "
                  f"{_n_ovl} reverted (overlap)")
            print(f"  [BUILDING ECHO] re-cut VERIFY (echo kit gate): {len(_final_bad)} SKUs starve -- MUST be 0")

            # ── CASCADE RE-CUT (2026-08-03, DURABLE FIX) ──────────────────────────────────────
            # The level-1 re-cut above re-times the FINAL belt/ply to the frozen build, but their
            # FEEDERS (edge strip -> belt, calandered roll -> ply, edge gum -> edge strip) feed
            # belt/ply -- NOT building -- so they stay placed to the PLAN and AGE when the build
            # slides (edge strip 46% / cal roll 23% over shelf). Cascade the re-cut UP the DAG:
            # sweep reverse-topological, re-timing each feeder FEFO-JIT to just before its DIRECT
            # consumer's CURRENT (re-timed) start, into its OWN cutter's idle, LATER-ONLY. Because
            # level-1 moved belt/ply LATER, the feeder gains room -> it never starves its consumer,
            # and only idle cutter work shifts -> makespan-neutral. Pure DAG => month-agnostic: this
            # kills slide-aging on EVERY future month's run, not just this one. Overlap-cleaned like
            # level-1; the run's MATERIAL STARVATION check is the safety net (must stay 0).
            if cfg.get("cascade_recut", True):
                _succ = defaultdict(set)
                for _cc, _pl in predecessors.items():
                    if _cc in schedule:
                        for _pp in _pl:
                            if _pp not in schedule:
                                continue
                            # ── 0-QTY EDGE FILTER (2026-08-06 BUGFIX) ──────────────────────
                            # `predecessors` carries phase3's COMPATIBILITY edges (many
                            # compatible producers per need; FEFO allocates ONE, the rest get
                            # consume_qty==0 -- the B333ARS_L0007 -> B333_L0007 kind). WITHOUT
                            # this filter the cascade's `_earliest = min(consumer start)` can be
                            # PINNED by a 0-qty consumer that happens to be scheduled EARLY, so
                            # the feeder is judged "already JIT" and is NEVER re-timed later
                            # toward its REAL (nonzero, later) consumer -> it stays early and
                            # AGES. The kit gate (~5044) and the level-1 re-cut (~5295) already
                            # drop these edges; the cascade MUST match or it leaks slide-aging.
                            _cq = (edge_qty.get((_pp, _cc), 0.0) if EDGE_QTY_ON
                                   else float(lot_qty.get(_pp, 0.0) or 0.0))
                            if _cq <= 0:
                                continue
                            _succ[_pp].add(_cc)
                _casc_orig = {}
                for _lvl in range(8):                          # bounded reverse-topo sweep to fixpoint
                    _moved = 0
                    for _pp in list(_succ):
                        if lot_item_type.get(_pp, "") not in _SS:   # only the roped short-shelf feeders
                            continue
                        _cons = [_c for _c in _succ[_pp] if _c in schedule]
                        if not _cons:
                            continue
                        _pmn, _ = _ages_ns(lot_item.get(_pp, ""))
                        _earliest = min(schedule[_c]["scheduled_start"].value for _c in _cons)
                        _target_finish = _earliest - _pmn      # finish before its EARLIEST draw (min_aging)
                        _cs = schedule[_pp]["scheduled_start"].value
                        _cf = int(schedule[_pp]["finish_ns"]); _dur = _cf - _cs
                        if _target_finish <= _cf + _TOL_NS:    # already JIT to its consumer
                            continue
                        _m = str(schedule[_pp]["machine"]); _busy = machine_busy[_m]
                        for _ix in range(len(_busy)):
                            if _busy[_ix][0] == _cs:
                                _busy.pop(_ix); break
                        _ns = latest_fit(_busy, _cs, _target_finish - _dur, _dur)
                        if _ns is None or _ns <= _cs + _TOL_NS:
                            _bisect.insort(_busy, (_cs, _cf)); continue
                        _nf = _ns + _dur
                        _bisect.insort(_busy, (_ns, _nf))
                        _casc_orig.setdefault(_pp, (_cs, _cf))
                        schedule[_pp]["scheduled_start"] = pd.Timestamp(int(_ns))
                        schedule[_pp]["scheduled_finish"] = pd.Timestamp(int(_nf))
                        schedule[_pp]["finish_ns"] = int(_nf)
                        _moved += 1
                    if not _moved:
                        break
                # overlap cleanup (identical policy to level-1): revert any moved feeder overlapping a neighbour
                _mvd2 = {str(schedule[_L]["machine"]) for _L in _casc_orig}
                for _op in range(40):
                    _iv = defaultdict(list)
                    for _l2, _sc2 in schedule.items():
                        _mm2 = str(_sc2.get("machine", ""))
                        if _mm2 in _mvd2 and str(_sc2.get("department", "")).upper() != "BUILDING":
                            _iv[_mm2].append((_sc2["scheduled_start"].value, int(_sc2["finish_ns"]), _l2))
                    _revd = 0
                    for _mm2, _lst in _iv.items():
                        _lst.sort()
                        for _z in range(len(_lst) - 1):
                            if _lst[_z + 1][0] < _lst[_z][1]:  # overlap
                                _rl = _lst[_z + 1][2] if _lst[_z + 1][2] in _casc_orig else (
                                      _lst[_z][2] if _lst[_z][2] in _casc_orig else None)
                                if _rl is not None:
                                    _cs5, _cf5 = _casc_orig.pop(_rl)
                                    schedule[_rl]["scheduled_start"] = pd.Timestamp(int(_cs5))
                                    schedule[_rl]["scheduled_finish"] = pd.Timestamp(int(_cf5))
                                    schedule[_rl]["finish_ns"] = int(_cf5)
                                    _revd += 1
                    if not _revd:
                        break
                print(f"  [BUILDING ECHO] CASCADE re-cut: {len(_casc_orig)} feeder lots (edge strip/cal roll/edge gum) "
                      f"re-timed JIT to their DIRECT consumer -- durable slide-aging fix, makespan-neutral")

        if new_lot_rows:
            _nl = pd.DataFrame(new_lot_rows).reindex(columns=lots.columns, fill_value="")
            # keep the numeric qty (reindex filled non-listed cols with "")
            for _c, _vals in {"lot_qty": [row["lot_qty"] for row in new_lot_rows]}.items():
                if _c in _nl.columns:
                    _nl[_c] = _vals
            lots = pd.concat([lots, _nl], ignore_index=True)
        # ══ AGING PULL PASS (2026-09-29, config `aging_pull_retime`) ══════════════════════════════
        # Floor replay of the 29-Sep run: 15% of component quantity reached building PAST max aging
        # (63k tyres). Placement anchors every freshness cap to a lot's FIRST need on the phase2
        # timeline, the eager-shelf rule finishes 24 h items up to 20 h before that, and the echo then
        # slides builds -- while the ledger counts a lot as supply forever. Build times are frozen
        # here, so re-time every component lot against its REAL use:
        #   level 1 (items that feed building): FIFO over the ledger (item-level, ALL SKUs share one
        #     pool; opening WIP first) gives each lot its first-use block start and its last-use time
        #     (pro-rated inside the block). Move the lot LATER, into its own machine's idle, so it
        #     finishes <= first_use - min_age (ledger readiness kept) and >= last_use - max_age.
        #     Lots are processed latest-first and never overtake the next lot (FIFO order kept).
        #   level 2+ (feeders): finish <= earliest consumer start - min_age, as late as idle allows,
        #     when the latest consumer would otherwise draw it past max_age.
        # Only LATER moves into idle -> no build, drum or bottleneck mixer is delayed. Verified with
        # the ledger (revert offenders). `_AGE_FLOOR` (lot -> earliest fresh finish) is handed to the
        # business-rule pass so it cannot pull a lot back into over-age.
        _AGE_FLOOR = {}
        _AGE_L1 = set()                                    # lots re-timed JIT to a build draw
        if cfg.get("aging_pull_retime", False) and _LEDGER and _L_commits:
            _TOLp = int(0.25 * 3600 * NS)
            _MARG = int(float(cfg.get("aging_pull_margin_h", 1.0)) * 3600 * NS)
            _moved_p = {}                                   # lot -> (orig_start, orig_finish)

            def _mv(_L, _desired_fin, _floor_start):
                if _L in IL_PINNED or _L in _PLANNED:       # BR-V10: never pull a lot out of its IL campaign; v56 planned
                    return False
                _cs = schedule[_L]["scheduled_start"].value
                _cf = int(schedule[_L]["finish_ns"]); _dur = _cf - _cs
                if _desired_fin <= _cf + _TOLp:
                    return False
                _m = str(schedule[_L]["machine"]); _busy = machine_busy[_m]
                for _ix in range(len(_busy)):
                    if _busy[_ix][0] == _cs:
                        _busy.pop(_ix); break
                _ns = latest_fit(_busy, max(_cs, _floor_start), _desired_fin - _dur, _dur)
                if _ns is None or _ns <= _cs + _TOLp:
                    _bisect.insort(_busy, (_cs, _cf))
                    return False
                _bisect.insort(_busy, (_ns, _ns + _dur))
                _moved_p.setdefault(_L, (_cs, _cf))
                schedule[_L]["scheduled_start"] = pd.Timestamp(int(_ns))
                schedule[_L]["scheduled_finish"] = pd.Timestamp(int(_ns + _dur))
                schedule[_L]["finish_ns"] = int(_ns + _dur)
                return True

            # build consumption per item (ledger commit order = start order)
            _cons_ev = defaultdict(list)                    # item -> [(start, finish, qty)]
            for _st0, _sku0, _q0, _lid0 in sorted(_L_commits):
                _fn0 = int(schedule[_lid0]["finish_ns"]) if _lid0 in schedule else int(_st0)
                for _it0, _r0 in _L_rate.get(_sku0, {}).items():
                    if _it0 in _L_sup:
                        _cons_ev[_it0].append((int(_st0), _fn0, _q0 * _r0))

            def _fifo_windows(_it):
                """lot -> (first_use_start, last_use_time) under item-level FIFO (WIP first)."""
                _ev = _cons_ev.get(_it, [])
                _lots = sorted(_L_sup.get(_it, ()), key=lambda x: schedule[x[0]]["finish_ns"])
                _out, _S = {}, _L_wip.get(_it, 0.0)
                _cum = [];  _c = 0.0
                for _s, _f, _q in _ev:
                    _cum.append((_s, _f, _c, _q)); _c += _q
                _j = 0
                for _L, _qv in _lots:
                    _S0, _S1 = _S, _S + _qv; _S = _S1
                    while _j < len(_cum) and _cum[_j][2] + _cum[_j][3] <= _S0 + 1e-9:
                        _j += 1
                    if _j >= len(_cum):
                        continue                            # building never draws this lot
                    _first = _cum[_j][0]
                    _k = _j
                    while _k < len(_cum) and _cum[_k][2] + _cum[_k][3] < _S1 - 1e-9:
                        _k += 1
                    _s, _f, _c0, _q = _cum[min(_k, len(_cum) - 1)]
                    _fr = 1.0 if _k >= len(_cum) or _q <= 0 else min(1.0, max(0.0, (_S1 - _c0) / _q))
                    _out[_L] = (_first, int(_s + _fr * (_f - _s)))
                return _out, [L for L, _ in _lots]

            # No order cap: a lot that cannot move (machine busy) simply becomes EARLIER in FIFO and the
            # windows are recomputed next round, so one stuck lot no longer pins every lot before it.
            _n1 = _res1 = 0
            for _rnd in range(int(cfg.get("aging_pull_rounds", 4))):
                _mv_r = 0
                for _it in list(_L_sup):
                    _mn, _mx = _ages_ns(_it)
                    _win, _order = _fifo_windows(_it)
                    for _L in reversed(_order):
                        if _L not in _win:
                            continue
                        _first, _last = _win[_L]
                        _lo = _last - _mx + _MARG              # earliest fresh finish
                        _hi = _first - _mn                     # latest ready finish
                        if int(schedule[_L]["finish_ns"]) < _lo and _mv(_L, _hi, NEG_INF_NS):
                            _mv_r += 1
                _n1 += _mv_r
                if not _mv_r:
                    break
            _dbg = []
            for _it in list(_L_sup):                           # final windows -> floors + residual count
                _mn, _mx = _ages_ns(_it)
                _win, _ = _fifo_windows(_it)
                for _L, (_first, _last) in _win.items():
                    _dbg.append((_L, _it, str(schedule[_L]["machine"]), schedule[_L]["scheduled_start"],
                                 pd.Timestamp(int(schedule[_L]["finish_ns"])), pd.Timestamp(int(_first)),
                                 pd.Timestamp(int(_last)), _mx / (3600.0 * NS), _L in _moved_p,
                                 len(machine_busy.get(str(schedule[_L]["machine"]), []))))
                    _AGE_FLOOR[_L] = _last - _mx + _MARG
                    _AGE_L1.add(_L)
                    if int(schedule[_L]["finish_ns"]) < _last - _mx:
                        _res1 += 1

            # level 2+: feeders re-timed to their (possibly moved) consumers, reverse-topological sweep
            _succp = defaultdict(set)
            for _cc, _pl in predecessors.items():
                if _cc in schedule:
                    for _pp in _pl:
                        if _pp in schedule and not str(_pp).startswith("BLD") and (
                                (edge_qty.get((_pp, _cc), 0.0) if EDGE_QTY_ON else 1.0) or 0) > 0:
                            _succp[_pp].add(_cc)
            _n2 = _res2 = 0
            for _sw in range(8):
                _mvd = 0
                for _pp, _cs_ in _succp.items():
                    _cons = [c for c in _cs_ if c in schedule and not str(c).startswith("BLD")]
                    if not _cons:
                        continue
                    _mn, _mx = _ages_ns(lot_item.get(_pp, ""))
                    _early = min(schedule[c]["scheduled_start"].value for c in _cons)
                    _late = max(schedule[c]["scheduled_start"].value for c in _cons)
                    _lo = _late - _mx + _MARG
                    _AGE_FLOOR[_pp] = max(_AGE_FLOOR.get(_pp, NEG_INF_NS), _lo)
                    if int(schedule[_pp]["finish_ns"]) >= _AGE_FLOOR[_pp]:
                        continue
                    _hi = _early - _mn
                    if _mv(_pp, _hi, NEG_INF_NS):
                        _mvd += 1
                _n2 += _mvd
                if not _mvd:
                    break
            _res2 = sum(1 for _pp in _succp if _pp in _AGE_FLOOR
                        and int(schedule[_pp]["finish_ns"]) < _AGE_FLOOR[_pp])

            # a feeder moved later must not start before ITS inputs are fresh-ready: it only ever moves
            # later, so inputs stay ready; but a level-1 lot's consumer-side limit (ledger) is re-verified.
            _nrev = 0
            _rev_it = defaultdict(int)
            for _pass in range(80):
                _bi, _nb, _ = _L_replay(_TOLp)
                if not _nb:
                    break
                _cand = [L for L in _moved_p if lot_item.get(L, "") in _bi]
                if not _cand:
                    break
                for _i5 in _bi:
                    _c5 = [L for L in _cand if lot_item.get(L, "") == _i5]
                    if not _c5:
                        continue
                    _L = max(_c5, key=lambda L: schedule[L]["finish_ns"])
                    _cs4, _cf4 = _moved_p.pop(_L)
                    _m = str(schedule[_L]["machine"]); _busy = machine_busy[_m]
                    _ns_old = schedule[_L]["scheduled_start"].value
                    for _ix in range(len(_busy)):
                        if _busy[_ix][0] == _ns_old:
                            _busy.pop(_ix); break
                    _bisect.insort(_busy, (_cs4, _cf4))
                    schedule[_L]["scheduled_start"] = pd.Timestamp(int(_cs4))
                    schedule[_L]["scheduled_finish"] = pd.Timestamp(int(_cf4))
                    schedule[_L]["finish_ns"] = int(_cf4)
                    _nrev += 1; _rev_it['ledger:' + _i5] += 1
            # feeder-before-consumer check: a moved lot must still finish (+min_age) before each consumer
            for _pass in range(20):
                _bad = []
                for _pp, _cs_ in _succp.items():
                    if _pp not in _moved_p:
                        continue
                    _mn, _ = _ages_ns(lot_item.get(_pp, ""))
                    for c in _cs_:
                        if c in schedule and int(schedule[_pp]["finish_ns"]) + _mn > schedule[c]["scheduled_start"].value + _TOLp:
                            _bad.append(_pp); break
                if not _bad:
                    break
                for _L in _bad:
                    _rev_it['feeder:' + lot_item.get(_L, '')] += 1
                    _cs4, _cf4 = _moved_p.pop(_L)
                    schedule[_L]["scheduled_start"] = pd.Timestamp(int(_cs4))
                    schedule[_L]["scheduled_finish"] = pd.Timestamp(int(_cf4))
                    schedule[_L]["finish_ns"] = int(_cf4)
                    _nrev += 1
            pd.DataFrame(_dbg, columns=["lot", "item", "machine", "start", "finish", "first_use", "last_use",
                                        "max_h", "moved", "n_busy"]).to_csv(OUTPUTS / "phase5_aging_pull_debug.csv",
                                                                           index=False)
            _bi, _nb, _ = _L_replay(_TOLp)
            print(f"  [AGING PULL] level-1 (feeds building): {_n1} lots re-timed later, {_res1} still over "
                  f"max aging (machine busy / lot spans > shelf); feeders: {_n2} moves, {_res2} still over; "
                  f"{_nrev} reverted; ledger: {_nb} blocks short (MUST be 0) | reverts: "
                  + ", ".join(f"{k}:{v}" for k, v in sorted(_rev_it.items(), key=lambda kv: -kv[1])[:8]))
        # ══ BUSINESS-RULE COMPLIANCE PASS (2026-09-27, businees_rules/*.xlsx) ══════════════════
        # One post-placement pass per upstream machine (config business_rules.rule_repair_pass).
        # Re-sequences + re-times each machine's lots with a list scheduler that enforces:
        #   GEN-07/BRE-05/BRC-03  exact changeover to the REAL time-neighbours (placement took it
        #                         from the last-PLACED item and never checked the next lot)
        #   BR4RC-05              >= 15 min cleanout before/after CAP 66
        #   BRM-06                TR100 cleanout time between master <-> final on the same mixer
        #   BRM-03                after butyl (ML405/ML605/29MG) only butyl-friendly (MB100/MB200/
        #                         XMB98); if none is ready a purge (TR100) is charged
        #   BRM-05                ML405/ML406 followed by MK3 when an MK3 lot is ready
        #   BRM-09/BRE-02/BRC-02  same item kept in ONE continuous run (pulled up to pull_early_h)
        #   BRC-01/CUT-02         Belt-1 followed by Belt-2 of the same code
        # HARD LIMITS (never violated): a lot starts >= max(inputs ready, placed start - pull_early_h,
        # upstream open) and finishes <= max(its placed finish, earliest consumer start - min_age).
        # A machine whose re-sequence cannot meet those limits keeps its original order (changeover
        # still re-timed); if even that fails it is left untouched. Reported per rule.
        _BR = cfg.get("business_rules") or {}
        _CO_MIN_REF = {}
        _SUCC_REF = {}
        _CREEL_REF = {}
        if _BR.get("rule_repair_pass", False):
            _pull = int(float(_BR.get("pull_early_h", 8)) * 3600 * NS)
            _cap66 = float(_BR.get("cap66_cleanout_min", 15))
            _butyl = {str(x) for x in (_BR.get("butyl_compounds") or [])}
            _friendly = {str(x) for x in (_BR.get("butyl_friendly") or [])} | _butyl
            _follow = {str(k): str(v) for k, v in (_BR.get("compound_followers") or {}).items()}
            _clean_item = str(_BR.get("master_final_cleanout_item", "TR100"))
            _clean_min = float(_BR.get("master_final_cleanout_min", 5))
            _MIXERS = {str(x) for x in (_BR.get("mixers") or [])}
            _BDc = cfg.get("butyl_day_block") or {}
            _AFTER_BUTYL = ({str(b) for g in (_BDc.get("groups") or {}).values() for b in (g.get("butyl") or [])}
                            if _BDc.get("enabled", False) else set())
            _AFTER_BUTYL_MIN = float(_BDc.get("cleanout_after_sec", 120)) / 60.0
            _BCAMP = {str(c): i for g in (_BDc.get("groups") or {}).values()
                      for i, cs in enumerate(g.get("campaigns") or []) for c in cs}
            lot_item_type_by_item, _dept_by_item = {}, {}
            for _l0, _i0 in lot_item.items():
                lot_item_type_by_item.setdefault(_i0, str(lot_item_type.get(_l0, "")).upper())
                _dept_by_item.setdefault(_i0, str(lot_dept.get(_l0, "")))
            _succ = defaultdict(list)
            for _c, _pl in predecessors.items():
                for _p in _pl:
                    _q = (edge_qty.get((_p, _c), 0.0) if EDGE_QTY_ON else 1.0)
                    if _q and _q > 0:
                        _succ[_p].append(_c)
            _up = [l for l, r in schedule.items()
                   if str(r.get("department", "")).upper() not in ("BUILDING", "CURING")
                   and not str(l).startswith("BLD")]
            # machine processing order: upstream (low DAG level) first
            _lvl = {}
            def _level(l, _d=0):
                if l in _lvl:
                    return _lvl[l]
                if _d > 60:
                    return 0
                v = 0
                for _p in predecessors.get(l, ()):
                    if _p in schedule:
                        v = max(v, _level(_p, _d + 1) + 1)
                _lvl[l] = v
                return v
            _by_m = defaultdict(list)
            for l in _up:
                _by_m[str(schedule[l]["machine"])].append(l)
            _morder = sorted(_by_m, key=lambda m: (np.median([_level(l) for l in _by_m[m]]), m))

            def _co_min(m, a, b):
                """changeover minutes a -> b on machine m, with the business-rule overrides."""
                if not a or a == b:
                    return 0.0, ""
                ta, tb = lot_item_type_by_item.get(a, ""), lot_item_type_by_item.get(b, "")
                base = float(compute_changeover_min(m, _dept_by_item.get(b, ""), a, ta, b, tb) or 0.0)
                tag = ""
                if "CAP 66" in a.upper() or "CAP 66" in b.upper():
                    if base < _cap66:
                        base, tag = _cap66, "cap66"
                if a in _AFTER_BUTYL and base < _AFTER_BUTYL_MIN and not (a in _BCAMP and _BCAMP.get(a) == _BCAMP.get(b)):
                    base, tag = _AFTER_BUTYL_MIN, "after_butyl"
                if m in _MIXERS:
                    # BR-V2: base is already the butyl clean-out when the sequence is broken
                    # (compute_changeover_min); TR100 master<->final keeps the longer of the two
                    _bhit = BUTYL_RULE is not None and BUTYL_RULE.cleanout(a, b)[0]
                    if {ta, tb} == {"MASTER COMPOUND", "FINAL COMPOUND"} and _clean_item not in (a, b):
                        base, tag = max(base, MIXING_CHANGEOVER_MIN + _clean_min), "tr100"
                    elif _bhit:
                        tag = "butyl_clean"
                    elif BUTYL_RULE is None and a in _butyl and b not in _friendly:
                        base, tag = base + _clean_min, "purge"          # old BRM-03 (rule off)
                return base, tag

            _CO_MIN_REF["f"] = _co_min
            _SUCC_REF["s"] = _succ
            _stat = defaultdict(int)
            _open_ns = int(UPSTREAM_OPEN_NS)
            # build-consumption times per GT-level item (from the material ledger commits): a lot
            # that feeds building may finish as late as the FIRST build block using its item that
            # starts after the lot's current finish (minus min_age) -- no build can starve.
            _item_builds = defaultdict(list)
            for _st0, _sku0, _q0, _lid0 in _L_commits:
                for _it0 in _L_rate.get(_sku0, {}):
                    _item_builds[_it0].append(int(_st0))
            for _k0 in _item_builds:
                _item_builds[_k0].sort()
            _untouched = []
            for _sweep in range(2):
                for m in _morder:
                    if _MIXPLAN_ON and m in _MIXERS:
                        continue                         # v56: mixing planned by the planner
                    L = sorted(_by_m[m], key=lambda l: schedule[l]["scheduled_start"].value)
                    if len(L) < 2:
                        continue
                    info = {}
                    for l in L:
                        st = schedule[l]["scheduled_start"].value; fi = int(schedule[l]["finish_ns"])
                        it = lot_item.get(l, "")
                        _inp = NEG_INF_NS
                        for _p in predecessors.get(l, ()):
                            if _p in schedule and ((edge_qty.get((_p, l), 0.0) if EDGE_QTY_ON else 1.0) or 0) > 0:
                                _inp = max(_inp, int(schedule[_p]["finish_ns"]) + int(_ages_ns(lot_item.get(_p, ""))[0]))
                        # inputs may only LIMIT pulling earlier; a lot the placement legitimately started
                        # before a producer (covered by opening WIP) is never pushed later by this pass
                        rel = max(st - _pull, _open_ns, min(st, _inp))
                        if l in _AGE_FLOOR:                    # aging pull: never pulled back into over-age
                            rel = max(rel, min(st, _AGE_FLOOR[l] - (fi - st)))
                        dl = None
                        for _c in _succ.get(l, ()):
                            if _c in schedule and not str(_c).startswith("BLD"):
                                v = schedule[_c]["scheduled_start"].value - int(_ages_ns(it)[0])
                                dl = v if dl is None else min(dl, v)
                        if it in _item_builds:                 # feeds building directly
                            _bs0 = _item_builds[it]
                            _ix = bisect.bisect_right(_bs0, fi)
                            if _ix < len(_bs0):
                                v = _bs0[_ix] - int(_ages_ns(it)[0])
                                dl = v if dl is None else min(dl, v)
                            elif dl is None:
                                dl = fi + 24 * 3600 * int(NS)    # no later build uses it
                        dl = fi if dl is None else max(fi, dl)
                        if l in _AGE_L1:                       # JIT to its FIFO draw: may not slip later
                            dl = fi
                        info[l] = (rel, dl, fi - st, it)

                    def _run(order_fixed):
                        R = list(L); out = []; t = None; last = ""
                        tags = defaultdict(int)
                        while R:
                            if order_fixed:
                                l = R[0]
                            else:
                                t0 = min(info[x][0] for x in R) if t is None else t
                                ready = [x for x in R if info[x][0] <= t0]
                                if not ready:
                                    t0 = min(info[x][0] for x in R)
                                    ready = [x for x in R if info[x][0] <= t0]
                                edf = min(ready, key=lambda x: (info[x][1], info[x][0]))
                                pref = []
                                if last in _follow:
                                    pref = [x for x in ready if info[x][3] == _follow[last]]
                                if not pref:
                                    pref = [x for x in ready if info[x][3] == last]
                                if not pref and BUTYL_RULE is not None:
                                    # BR-V2 'after': a protected master compound -> an allowed follower
                                    _pa = BUTYL_RULE.preps_after(last)
                                    if _pa:
                                        pref = [x for x in ready if info[x][3] in _pa]
                                elif not pref and last in _butyl:
                                    pref = [x for x in ready if info[x][3] in _friendly]
                                if not pref and last.startswith("B1-"):
                                    pref = [x for x in ready if info[x][3] == "B2-" + last[3:]]
                                l = min(pref, key=lambda x: (info[x][1], info[x][0])) if pref else edf
                                if BUTYL_RULE is not None and BUTYL_RULE.cleanout(last, info[l][3])[0]:
                                    # BR-V2 'before': about to start a protected compound without its
                                    # prep -> run a READY prep lot first if the protected lot still
                                    # meets its limit; else the clean-out is charged. Real lots only.
                                    _pb = BUTYL_RULE.preps_before(info[l][3])
                                    _cand = [x for x in ready if x != l and info[x][3] in _pb]
                                    if _cand:
                                        _p = min(_cand, key=lambda x: (info[x][1], info[x][0]))
                                        c1, _ = _co_min(m, last, info[_p][3]); c2, _ = _co_min(m, info[_p][3], info[l][3])
                                        _bt = max(t if t is not None else info[_p][0], info[_p][0])
                                        if _bt + int((c1 + c2) * 60 * NS) + info[_p][2] + info[l][2] <= info[l][1]:
                                            l = _p
                                if l != edf:
                                    c1, _ = _co_min(m, last, info[l][3]); c2, _ = _co_min(m, info[l][3], info[edf][3])
                                    base_t = max(t if t is not None else info[l][0], info[l][0])
                                    fin_e = base_t + int((c1 + c2) * 60 * NS) + info[l][2] + info[edf][2]
                                    if fin_e > info[edf][1]:
                                        l = edf
                            R.remove(l)
                            c, tag = _co_min(m, last, info[l][3])
                            if tag:
                                tags[tag] += 1
                            prev_end = t if t is not None else NEG_INF_NS
                            stt = max(prev_end + int(c * 60 * NS), info[l][0])
                            fin = stt + info[l][2]
                            out.append((l, stt, fin)); t = fin; last = info[l][3]
                        bad = [l for l, _, f in out if f > info[l][1] + 60 * NS]
                        return out, bad, tags

                    if IL_PINNED and any(l in IL_PINNED for l in L):
                        out, bad, tags = _run(True)      # BR-V10: keep the IL campaign blocks intact
                        mode = "retimed"
                    else:
                        out, bad, tags = _run(False)
                        mode = "resequenced"
                    if bad:
                        out, bad, tags = _run(True)
                        mode = "retimed"
                    if bad:
                        _stat["machines_left_untouched"] += 1 if _sweep == 1 else 0
                        if _sweep == 1:
                            _untouched.append((m, len(bad), len(L)))
                        continue
                    _stat["machines_" + mode] += 1 if _sweep == 1 else 0
                    for k, v in tags.items():
                        if _sweep == 1:
                            _stat[k] += v
                    busy = [iv for iv in machine_busy.get(m, [])]
                    for l, stt, fin in out:
                        schedule[l]["scheduled_start"] = pd.Timestamp(int(stt))
                        schedule[l]["scheduled_finish"] = pd.Timestamp(int(fin))
                        schedule[l]["finish_ns"] = int(fin)
                    machine_busy[m] = sorted((int(stt), int(fin)) for _, stt, fin in out)
            print(f"  [RULES] compliance pass: {_stat.get('machines_resequenced', 0)} machines re-sequenced, "
                  f"{_stat.get('machines_retimed', 0)} re-timed in original order, "
                  f"{_stat.get('machines_left_untouched', 0)} left untouched (limits) | cleanouts: "
                  f"CAP66 {_stat.get('cap66', 0)}, TR100 master<->final {_stat.get('tr100', 0)}, "
                  f"butyl purge {_stat.get('purge', 0)}, BR-V2 butyl clean-outs {_stat.get('butyl_clean', 0)}")
            if _untouched:
                print(f"  [RULES] untouched machines (lots that would finish after their consumer): "
                      + ", ".join(f"{m}({nb}/{n})" for m, nb, n in _untouched[:20]))

            # ── v18 BRM-09 SAME-COMPOUND-ONCE-PER-SHIFT MERGE (2026-10-05, business_rules.same_item_shift_merge)
            # The pass above re-sequences a whole machine or nothing; a mixer with ONE lot that cannot move
            # (F270, K310) keeps every split run. This is local: for each (mixer, shift) where a compound has
            # >1 run, the two runs are joined by moving the later run up behind the earlier one (A) or the
            # earlier run down in front of the later one (B). Only the lots between them shift. Accepted only
            # if, re-timed with the full changeover rules (_co_min: 60 s, butyl clean-out, TR100), every lot
            # starts after its inputs are ready (+ min aging), finishes by its consumers' need (- min aging),
            # the segment still ends before the next lot, and no IL campaign lot is touched. Shift = 8 h
            # block from 07:00 by lot start (as audit_business_rules counts it).
            _SM = _BR.get("same_item_shift_merge") or {}

            def _brm09_pass(_delay_h, _tag):
                """v18 BRM-09 local merge (see note above); _delay_h = allowed consumer delay."""
                _item_builds = defaultdict(list)          # current build starts per GT-level item
                for _st0, _sku0, _q0, _lid0 in _L_commits:
                    for _it0 in _L_rate.get(_sku0, {}):
                        _item_builds[_it0].append(int(_st0))
                for _k0 in _item_builds:
                    _item_builds[_k0].sort()
                _H7n, _SHn = 7 * 3600 * NS, 8 * 3600 * NS
                _shid = lambda t: (int(t) - _H7n) // _SHn
                _sm = defaultdict(int)
                _sm_tried = {}
                # consumers may start up to this much later (final repair re-times them; inputs stay ready)
                _sm_delay = int(float(_delay_h) * 3600 * NS)
                _bym = defaultdict(list)                  # own list: _by_m is re-used by final repair
                for _l1, _r1 in schedule.items():
                    if (str(_r1.get("department", "")).upper() not in ("BUILDING", "CURING")
                            and not str(_l1).startswith("BLD")):
                        _bym[str(_r1["machine"])].append(_l1)
                _sm_list = [str(x) for x in (_SM.get("machines") or [])]
                _sm_excl = {str(x) for x in (_SM.get("exclude") or [])}
                # BR-9 (v32): machines ["ALL"] -> every upstream machine except `exclude`; [] -> the mixers
                _sm_m = ((set(_bym) - _sm_excl) if "ALL" in _sm_list else (set(_sm_list) or {str(x) for x in _MIXERS}))
                for m in sorted(_bym):
                    if m not in _sm_m:
                        continue
                    for _it_round in range(400):
                        L = sorted([l for l in _bym[m] if l in schedule],
                                   key=lambda l: schedule[l]["scheduled_start"].value)
                        st_ = {l: schedule[l]["scheduled_start"].value for l in L}
                        du_ = {l: int(schedule[l]["finish_ns"]) - st_[l] for l in L}
                        it_ = {l: lot_item.get(l, "") for l in L}
                        # runs per shift
                        cand = None
                        by_sh = defaultdict(list)
                        for i, l in enumerate(L):
                            by_sh[_shid(st_[l])].append(i)
                        for _s, ix in sorted(by_sh.items()):
                            runs = []
                            for i in ix:
                                if runs and it_[L[runs[-1][1]]] == it_[L[i]] and runs[-1][1] == i - 1:
                                    runs[-1][1] = i
                                else:
                                    runs.append([i, i])
                            seen = {}
                            for r0, r1 in runs:
                                k_ = it_[L[r0]]
                                if k_ in seen:
                                    key = (seen[k_][0], seen[k_][1], r0, r1)
                                    if (L[seen[k_][0]], L[r0]) not in _sm_tried.get(m, set()):
                                        cand = key
                                        break
                                else:
                                    seen[k_] = (r0, r1)
                            if cand:
                                break
                        if not cand:
                            break
                        a0, a1, b0, b1 = cand
                        _sm_tried.setdefault(m, set()).add((L[a0], L[b0]))
                        seg = L[a0:b1 + 1]
                        if any(l in IL_PINNED for l in seg):
                            _sm["skip_il"] += 1
                            continue
                        r1, mid, r2 = L[a0:a1 + 1], L[a1 + 1:b0], L[b0:b1 + 1]

                        def _lim(l):
                            _inp = NEG_INF_NS
                            for _p in predecessors.get(l, ()):
                                if _p in schedule and ((edge_qty.get((_p, l), 0.0) if EDGE_QTY_ON else 1.0) or 0) > 0:
                                    _inp = max(_inp, int(schedule[_p]["finish_ns"]) + int(_ages_ns(lot_item.get(_p, ""))[0]))
                            rel = max(int(UPSTREAM_OPEN_NS), min(st_[l], _inp))
                            if l in _AGE_FLOOR:                # never pulled back into over-age
                                rel = max(rel, min(st_[l], _AGE_FLOOR[l] - du_[l]))
                            # max aging: pulled earlier, the output must still be within shelf life at its
                            # LAST consumer -> finish no earlier than that consumer's start - max age
                            lo = NEG_INF_NS
                            _mxa = int(_ages_ns(it_[l])[1] or 0)
                            if _mxa > 0:
                                _cs = [schedule[_c]["scheduled_start"].value for _c in _succ.get(l, ()) if _c in schedule]
                                if _cs:
                                    lo = max(_cs) - _mxa
                            dl = None
                            for _c in _succ.get(l, ()):
                                if _c in schedule and not str(_c).startswith("BLD"):
                                    v = schedule[_c]["scheduled_start"].value - int(_ages_ns(it_[l])[0])
                                    dl = v if dl is None else min(dl, v)
                            if it_[l] in _item_builds:
                                _bs0 = _item_builds[it_[l]]
                                _ix = bisect.bisect_right(_bs0, st_[l] + du_[l])
                                if _ix < len(_bs0):
                                    v = _bs0[_ix] - int(_ages_ns(it_[l])[0])
                                    dl = v if dl is None else min(dl, v)
                            fi = st_[l] + du_[l]
                            dl = fi if dl is None else max(fi, dl)
                            if l in _AGE_L1:
                                dl = fi
                            return rel, dl, lo

                        prev_it = it_[L[a0 - 1]] if a0 > 0 else ""
                        prev_end = (st_[L[a0 - 1]] + du_[L[a0 - 1]]) if a0 > 0 else NEG_INF_NS
                        nxt = L[b1 + 1] if b1 + 1 < len(L) else None
                        best = None
                        _why = []
                        for order in (r1 + r2 + mid, mid + r1 + r2):
                            t, last, out, ok, late = None, prev_it, [], True, 0
                            for l in order:
                                rel, dl, lo = _lim(l)
                                c, _ = _co_min(m, last, it_[l])
                                base = max(prev_end if t is None else t, st_[seg[0]] if t is None else 0)
                                s0 = max(base + int(c * 60 * NS), rel)
                                f0 = s0 + du_[l]
                                if f0 > dl + _sm_delay + 60 * NS or (f0 < lo and f0 < st_[l] + du_[l]):
                                    ok = False
                                    _why.append("consumer" if f0 > dl + _sm_delay + 60 * NS else "max_age")
                                    break
                                late += max(0, f0 - dl)
                                out.append((l, s0, f0))
                                t, last = f0, it_[l]
                            # following lots: pushed later only as far as needed (cascade), each within its
                            # consumer limit (+ allowed delay); never an IL campaign lot
                            j = b1 + 1
                            while ok and j < len(L):
                                x = L[j]
                                c, _ = _co_min(m, last, it_[x])
                                if t + int(c * 60 * NS) <= st_[x]:
                                    break
                                if x in IL_PINNED:
                                    ok = False
                                    _why.append("next_lot")
                                    break
                                _r, _d, _lo = _lim(x)
                                s0 = t + int(c * 60 * NS)
                                f0 = s0 + du_[x]
                                if f0 > _d + _sm_delay + 60 * NS:
                                    ok = False
                                    _why.append("next_lot")
                                    break
                                late += max(0, f0 - _d)
                                out.append((x, s0, f0))
                                t, last = f0, it_[x]
                                j += 1
                            if ok and (best is None or (late, t) < (best[2], best[0])):
                                best = (t, out, late)
                        if best is None:
                            _sm["not_fixable"] += 1
                            for _w in set(_why):
                                _sm["why_" + _w] += 1
                            continue
                        for l, s0, f0 in best[1]:
                            schedule[l]["scheduled_start"] = pd.Timestamp(int(s0))
                            schedule[l]["scheduled_finish"] = pd.Timestamp(int(f0))
                            schedule[l]["finish_ns"] = int(f0)
                        _mv = {st_[l] for l, _, _ in best[1]}
                        _bz = [iv for iv in machine_busy.get(m, []) if iv[0] not in _mv]
                        machine_busy[m] = sorted(_bz + [(int(s0), int(f0)) for _, s0, f0 in best[1]])
                        _sm["merged"] += 1
                        _sm["delayed"] += int(best[2] > 0)
                print(f"  [BRM-09 {_tag}] same compound once per shift: {_sm['merged']} split runs joined "
                      f"({_sm['delayed']} of them delay a consumer by <= {_sm_delay / 3600 / NS:g} h), "
                      f"{_sm['not_fixable']} left (a lot would miss its inputs / consumer / next lot), "
                      f"{_sm['skip_il']} left (IL campaign lots) | blocked by: consumer {_sm['why_consumer']}, "
                      f"max aging {_sm['why_max_age']}, next lot {_sm['why_next_lot']}")
                return _sm["merged"]

            if _SM.get("enabled", False) and not _MIXPLAN_ON:     # v56: the planner already runs each compound in one go
                _brm09_pass(float(_SM.get("max_consumer_delay_h", 0)), "before final repair")

        # == FINAL FEASIBILITY REPAIR (2026-09-30, config `final_repair_pass`) ==========================
        # Runs LAST, only ever moves work LATER. Makes the placed schedule obey, exactly:
        #   BR-7   the changeover matrix (+ BR rule overrides: CAP66 15 min, TR100, butyl purge,
        #          mixer fixed changeover) between consecutive lots on every upstream machine
        #   BR-10  PM / MTC downtime windows (jkplanning_CTP.downtime_reason)
        #   BR-12  min AND max aging: a build block is released only when its kit is covered by
        #          material past min aging and NOT past max aging (FEFO; expired = scrap)
        #   BR-66/67 complete kit before release
        # Upstream lots first (time order: changeover after the machine's previous lot, inputs ready),
        # then build blocks (time order: TBM free, aging-valid kit).
        _rep = {}
        def _final_repair(_up_on=True):
            def _co_req(_m, _pa, _cu):
                if not _pa or _pa == _cu:
                    return 0.0
                try:
                    return float(_co_min(_m, _pa, _cu)[0] or 0.0)
                except NameError:
                    return float(compute_changeover_min(_m, "", _pa, "", _cu, "") or 0.0)

            def _skip_down(_m, _st, _du):
                _ws = DOWNTIME_WIN.get(_m, ())
                _moved = True
                while _moved and _ws:
                    _moved = False
                    for _a, _b in _ws:
                        if _st < _b and _st + _du > _a:
                            _st = _b
                            _moved = True
                return _st

            _ups = [l for l, sc in schedule.items() if not str(l).startswith("BLD")
                    and str(sc.get("department", "")).upper() not in ("BUILDING", "CURING")]
            _ups.sort(key=lambda l: (schedule[l]["scheduled_start"].value, str(l)))
            if _COMPACT and not _FLOOR0:             # v53: placement start = the earliest a lot may return to
                _FLOOR0.update({l: int(schedule[l]["scheduled_start"].value) for l in _ups})
                _BFLOOR0.update({lid_: int(st_) for st_, _s_, _q_, lid_ in _L_commits})
                print(f"  [REPAIR COMPACTION v53] placement starts kept as floors for {len(_FLOOR0):,} upstream lots "
                      f"and {len(_BFLOOR0):,} build rows (the repair may move a lot back towards them)")

            _F2R = bool((cfg.get("upstream_gap_fill") or {}).get("floor_to_ready", False))     # v81 A1
            _F2R_X = {str(x) for x in (cfg.get("compress_exclude_machines") or [])}

            def _cfloor(_l, _st):
                """v53: earliest start the repair may give a lot = its placement start (all placement floors), never
                earlier than the shelf life at its latest consumer allows; blocks / pinned / prep lots stay."""
                if (not _COMPACT or _l not in _FLOOR0 or _l in IL_PINNED or "PREP" in str(_l)
                        or str(schedule[_l].get("machine")) == "901"):
                    return _st
                f = min(int(_FLOOR0[_l]), int(_st))
                # v81 A1: on component machines (not mixers / 901) the floor is the upstream open, so a lot moves back into
                # idle as soon as its inputs (+min aging), its machine predecessor (+matrix changeover), PM windows and the
                # shelf life at its latest consumer allow -- machine sequence (and every grouping order) unchanged.
                if _F2R and str(schedule[_l].get("machine")) not in _F2R_X:
                    f = min(f, int(UPSTREAM_OPEN_NS))
                _dd = int(schedule[_l]["finish_ns"]) - int(schedule[_l]["scheduled_start"].value)
                if _l in _AGE_FLOOR:
                    f = max(f, int(_AGE_FLOOR[_l]) - _dd)
                return int(min(f, _st))
            # BR-V10 (v12): an IL campaign (B99 + IL3/IL5) moves as ONE unit -- when any member must start
            # later, the whole block shifts with its internal offsets kept, so it can never be split.
            _camp_of, _camp_done = {}, set()
            for _ci, _cr in enumerate(_il_rows):
                _cl = [x for x in str(_cr.get("lots", "")).split(";") if x in schedule]
                if len(_cl) > 1:
                    _c0 = min(schedule[x]["scheduled_start"].value for x in _cl)
                    for x in _cl:
                        _camp_of[x] = (_ci, schedule[x]["scheduled_start"].value - _c0, _cl)
            _last = {}
            _n_up = _n_co = 0
            _h_up = 0.0
            _RU = bool(((cfg.get("business_rules") or {}).get("same_item_shift_merge") or {}).get("repair_units", False))
            _RU_UNITS = _RU
            _RU = False            # v18: the time-ordered loop below drifted the plan (window cascade); kept, OFF
            if _RU:
                # v18 (2026-10-05): the loop above walked lots in their ORIGINAL start order, so a block that was
                # shifted later could interleave with lots it overtook, and a push could split a same-compound run
                # or carry an IL campaign into a day that already had one. Here: (1) lots are taken in CURRENT
                # start order (heap); (2) consecutive same-item lots on a mixer are a UNIT that moves as one block,
                # like an IL campaign; (3) a unit is placed only when it no longer moves; then the other lots in
                # its window are put after it; (4) an IL campaign landing on a plant day that already has a later
                # campaign absorbs it into one [B99s][IL item 1][IL item 2] block (inputs ready before each lot).
                import heapq as _hq
                _mixset = {str(x) for x in ((cfg.get("business_rules") or {}).get("mixers") or [])}
                _iltems = {str(x).strip() for x in (_ILC.get("items") or ["IL3", "IL5"])}
                _ilprep = str(_ILC.get("prep", "B99")).strip()
                _H7r, _Dr = 7 * 3600 * NS, 24 * 3600 * NS
                _itr = lambda x: str(lot_item.get(x, "")).strip()
                _n_il_campaigns = len(_il_rows)
                _cid = 10 ** 6
                _units = {}                                   # unit id -> [lots] (in block order)
                # each lot in ONE unit: the final assignment (a phase-5 merge leaves the absorbed campaign's
                # row listing the same lots again -- reading the rows would duplicate them)
                for _x, (_ci0, _o0, _cl0) in list(_camp_of.items()):
                    _units.setdefault(_ci0, []).append(_x)
                for _k in list(_units):
                    _units[_k].sort(key=lambda x: schedule[x]["scheduled_start"].value)
                    if len(_units[_k]) < 2:
                        for _x in _units[_k]:
                            _camp_of.pop(_x, None)
                        _units.pop(_k)
                    else:
                        for _x in _units[_k]:
                            _camp_of[_x] = (_k, _camp_of[_x][1], _units[_k])
                _bym2 = defaultdict(list)
                for _x in _ups:
                    _bym2[str(schedule[_x]["machine"])].append(_x)
                for _m2, _L2 in _bym2.items():
                    if _m2 not in _mixset:
                        continue
                    _L2 = sorted(_L2, key=lambda x: schedule[x]["scheduled_start"].value)
                    _i2 = 0
                    while _i2 < len(_L2):
                        _j2 = _i2
                        while (_j2 + 1 < len(_L2) and _itr(_L2[_j2 + 1]) == _itr(_L2[_i2])
                               and _L2[_j2 + 1] not in _camp_of and _L2[_i2] not in _camp_of):
                            _j2 += 1
                        if _j2 > _i2:
                            _g2 = _L2[_i2:_j2 + 1]
                            _c02 = schedule[_g2[0]]["scheduled_start"].value
                            for _x in _g2:
                                _camp_of[_x] = (_cid, schedule[_x]["scheduled_start"].value - _c02, _g2)
                            _units[_cid] = list(_g2)
                            _cid += 1
                        _i2 = _j2 + 1
                _ilu = lambda u: u < _n_il_campaigns or (u >= 2 * 10 ** 6)     # IL campaign units

                def _offs(_lst, _m):
                    o, t, pv = {}, 0, ""
                    for x in _lst:
                        t += int(_co_req(_m, pv, _itr(x)) * 60 * NS) if pv else 0
                        o[x] = t
                        t += int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
                        pv = _itr(x)
                    return o

                def _set_unit(_u, _lst, _start, _m):
                    o = _offs(_lst, _m)
                    for x in _lst:
                        d = int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
                        schedule[x]["scheduled_start"] = pd.Timestamp(int(_start + o[x]))
                        schedule[x]["scheduled_finish"] = pd.Timestamp(int(_start + o[x] + d))
                        schedule[x]["finish_ns"] = int(_start + o[x] + d)
                        _camp_of[x] = (_u, o[x], _lst)
                    _units[_u] = list(_lst)

                def _ready_start(_lst, _m):
                    o = _offs(_lst, _m)
                    return max([NEG_INF_NS] + [int(_material_ready_ns(x)) - o[x] for x in _lst
                                               if _material_ready_ns(x) > NEG_INF_NS])

                _hp = [(schedule[x]["scheduled_start"].value, str(x), x) for x in _ups]
                _hq.heapify(_hp)
                _done, _placed_units = set(), set()
                _n_ilm = 0
                _il_day_used = {}                             # (machine, plant day) -> placed IL unit
                _merge_id = 2 * 10 ** 6
                while _hp:
                    _s0, _k0, _l = _hq.heappop(_hp)
                    if _l in _done:
                        continue
                    _sc = schedule[_l]
                    if _sc["scheduled_start"].value != _s0:
                        _hq.heappush(_hp, (_sc["scheduled_start"].value, str(_l), _l))
                        continue
                    _m = str(_sc["machine"])
                    _st = int(_sc["scheduled_start"].value)
                    _du = max(int(_sc["finish_ns"]) - _st, 0)
                    _est = _st
                    if _m in _last:
                        _pit, _pf = _last[_m]
                        _req = _pf + int(_co_req(_m, _pit, lot_item.get(_l, "")) * 60 * NS)
                        if _req > _est:
                            _est = _req
                            _n_co += 1
                    _mr = _material_ready_ns(_l)
                    if _mr > _est:
                        _est = _mr
                    _u = _camp_of.get(_l, (None,))[0]
                    if _u is not None and _u not in _placed_units:
                        _lst = _units[_u]
                        if _l != _lst[0]:                     # only the head decides the block:
                            # re-align the members behind the head, then let the head come first
                            _set_unit(_u, _lst, schedule[_lst[0]]["scheduled_start"].value, _m)
                            for _x in _lst:
                                _hq.heappush(_hp, (schedule[_x]["scheduled_start"].value, str(_x), _x))
                            continue
                        _blk = max(_est, _ready_start(_lst, _m))
                        _span = int(_offs(_lst, _m)[_lst[-1]]) + (int(schedule[_lst[-1]]["finish_ns"])
                                                                   - schedule[_lst[-1]]["scheduled_start"].value)
                        _blk = _skip_down(_m, _blk, _span)
                        # IL: absorb a LATER campaign that sits on the same plant day
                        if _ILC.get("regroup_merged", False) and _ilu(_u) and any(_itr(x) in _iltems for x in _lst):
                            _first_il = lambda lst, b: b + min(_offs(lst, _m)[x] for x in lst if _itr(x) in _iltems)
                            _day = (_first_il(_lst, _blk) - _H7r) // _Dr
                            # a day that already has a PLACED campaign: start on the next free day
                            _guard = 0
                            while (_m, _day) in _il_day_used and _guard < 60:
                                _blk = max(_blk, (_day + 1) * _Dr + _H7r - (_first_il(_lst, 0)))
                                _blk = _skip_down(_m, _blk, _span)
                                _day = (_first_il(_lst, _blk) - _H7r) // _Dr
                                _guard += 1
                            _oth = [v for k, v in _units.items() if k != _u and _ilu(k) and k not in _placed_units
                                    and v and str(schedule[v[0]]["machine"]) == _m
                                    and schedule[v[0]]["scheduled_start"].value >= _st
                                    and any(_itr(x) in _iltems for x in v)
                                    and (min(schedule[x]["scheduled_start"].value for x in v if _itr(x) in _iltems)
                                         - _H7r) // _Dr == _day]
                            if _oth:
                                _allx = list(dict.fromkeys(_lst + [x for v in _oth for x in v]))
                                _pre = sorted([x for x in _allx if _itr(x) not in _iltems],
                                              key=lambda x: schedule[x]["scheduled_start"].value)
                                _ils = [x for x in _allx if _itr(x) in _iltems]
                                _ord = list(dict.fromkeys(_itr(x) for x in sorted(_ils, key=lambda x: schedule[x]["scheduled_start"].value)))
                                _ils.sort(key=lambda x: (_ord.index(_itr(x)), schedule[x]["scheduled_start"].value))
                                _new = _pre + _ils
                                for _k in [k for k, v in _units.items() if any(x in _allx for x in v)]:
                                    _units.pop(_k, None)
                                _merge_id += 1
                                _u, _lst = _merge_id, _new
                                _blk = _skip_down(_m, max(_blk, _ready_start(_new, _m)), _span)
                                IL_PINNED.update(_new)
                                _n_ilm += 1
                        # place the block NOW at _blk (it keeps its priority, as in the original loop); the
                        # lots it overtakes inside its new window move after it, in their order
                        _set_unit(_u, _lst, _blk, _m)
                        _bend = int(schedule[_lst[-1]]["finish_ns"])
                        _mem = set(_lst)
                        for _y in _bym2.get(_m, ()):
                            if _y in _mem or _y in _done:
                                continue
                            _ys = schedule[_y]["scheduled_start"].value
                            if _st <= _ys < _bend:
                                _yd = int(schedule[_y]["finish_ns"]) - _ys
                                _yn = _bend + (_ys - _st)
                                schedule[_y]["scheduled_start"] = pd.Timestamp(int(_yn))
                                schedule[_y]["scheduled_finish"] = pd.Timestamp(int(_yn + _yd))
                                schedule[_y]["finish_ns"] = int(_yn + _yd)
                        _placed_units.add(_u)
                        if _ilu(_u) and any(_itr(x) in _iltems for x in _lst):
                            _f0 = min(schedule[x]["scheduled_start"].value for x in _lst if _itr(x) in _iltems)
                            _il_day_used[(_m, (_f0 - _H7r) // _Dr)] = _u
                        for _x in _lst:
                            if _x != _l:
                                _hq.heappush(_hp, (schedule[_x]["scheduled_start"].value, str(_x), _x))
                        _st = _est = int(schedule[_l]["scheduled_start"].value)   # this lot is placed by the block
                        _du = max(int(_sc["finish_ns"]) - _st, 0)
                    _est = _skip_down(_m, _est, _du)
                    if _est > _st:
                        _sc["scheduled_start"] = pd.Timestamp(int(_est))
                        _sc["scheduled_finish"] = pd.Timestamp(int(_est + _du))
                        _sc["finish_ns"] = int(_est + _du)
                        _n_up += 1
                        _h_up += (_est - _st) / (3600.0 * NS)
                    _last[_m] = (lot_item.get(_l, ""), int(_sc["finish_ns"]))
                    _done.add(_l)
                if _n_ilm:
                    print(f"  [REPAIR v18] {_n_ilm} IL campaign(s) absorbed a later same-day campaign while being pushed")
                # campaign list for reporting / later passes: one row per final IL unit
                _il_rows[:] = [{"lots": ";".join(v), "machine": str(schedule[v[0]]["machine"])}
                               for k, v in _units.items() if _ilu(k) and v and any(_itr(x) in _iltems for x in v)]
            else:
                if _RU_UNITS and _up_on:
                    # v18: consecutive same-item lots on a mixer move as ONE unit (like an IL campaign), so a
                    # push can never split a run that the BRM-09 pass joined. Each lot in one unit only.
                    _mixset2 = {str(x) for x in ((cfg.get("business_rules") or {}).get("mixers") or [])}
                    _own = {}
                    for _x, (_ci0, _o0, _cl0) in list(_camp_of.items()):
                        _own.setdefault(_ci0, []).append(_x)
                    for _ci0, _xs in _own.items():
                        _xs.sort(key=lambda x: schedule[x]["scheduled_start"].value)
                        _c00 = schedule[_xs[0]]["scheduled_start"].value
                        for _x in _xs:
                            _camp_of[_x] = (_ci0, schedule[_x]["scheduled_start"].value - _c00, _xs)
                    _cidu = 10 ** 6
                    _bymu = defaultdict(list)
                    for _x in _ups:
                        if str(schedule[_x]["machine"]) in _mixset2:
                            _bymu[str(schedule[_x]["machine"])].append(_x)
                    _nu = 0
                    for _mu, _Lu in _bymu.items():
                        _Lu.sort(key=lambda x: schedule[x]["scheduled_start"].value)
                        _iu = 0
                        while _iu < len(_Lu):
                            _ju = _iu
                            while (_ju + 1 < len(_Lu) and lot_item.get(_Lu[_ju + 1]) == lot_item.get(_Lu[_iu])
                                   and _Lu[_iu] not in _camp_of and _Lu[_ju + 1] not in _camp_of):
                                _ju += 1
                            if _ju > _iu:
                                _gu = _Lu[_iu:_ju + 1]
                                _c0u = schedule[_gu[0]]["scheduled_start"].value
                                for _x in _gu:
                                    _camp_of[_x] = (_cidu, schedule[_x]["scheduled_start"].value - _c0u, _gu)
                                _cidu += 1
                                _nu += 1
                            _iu = _ju + 1
                    print(f"  [REPAIR v18] {_nu} same-item runs on the mixers move as one unit")
                _creel_last, _n_creel = {}, 0
                _n_dn53, _h_dn53 = [0], [0.0]                  # v53: lots moved EARLIER (compaction)
                _run47, _n47 = {}, 0                        # v47: compound -> start of its current / last run
                # ── BR-12 (v33): AGING HARD BLOCK for upstream consumers. An upstream lot consumes only input
                # lots ALIVE at its start (finish + min_age <= t <= finish + max_age), FEFO, per input item and
                # pooled over every lot of that item (opening WIP first via open_alloc). Not enough alive supply
                # -> the lot waits for the next fresh supply; none at all -> flagged (remake needed), never
                # consuming expired material silently.
                _AGH = cfg.get("aging_hard_block") or {}
                _AGH_ON = bool(_AGH.get("enabled", False))
                _rem12, _pool12 = {}, defaultdict(list)
                _n12h = _n12s = 0
                _h12 = 0.0
                _short12 = []
                if _AGH_ON:
                    for _x in _ups:
                        _pool12[str(lot_item.get(_x, ""))].append(_x)
                        _rem12[_x] = float(lot_qty.get(_x, 0.0) or 0.0)

                def _needs12(l):
                    nd = defaultdict(float)
                    for p in predecessors.get(l, ()):
                        if p in _rem12:
                            nd[str(lot_item.get(p, ""))] += float((edge_qty.get((p, l), 0.0) if EDGE_QTY_ON
                                                                   else lot_qty.get(p, 0.0)) or 0.0)
                    for it in list(nd):
                        if (l, it) not in _OPEN_NETTED:
                            nd[it] -= float((open_alloc or {}).get((l, it), 0.0) or 0.0)
                        if nd[it] <= 1e-6:
                            nd.pop(it)
                    return nd

                def _alive12(x, t, mn, mx):
                    f = int(schedule[x]["finish_ns"])
                    return f + mn <= t and (mx <= 0 or t <= f + mx)

                def _fresh_time12(l, t):
                    tout, short = t, False
                    for it, n in _needs12(l).items():
                        mn, mx = _ages_ns(it)
                        lots_ = [x for x in _pool12.get(it, ()) if x != l and _rem12.get(x, 0) > 1e-9]
                        if not lots_:
                            continue                       # not a scheduled item (raw material): not gated
                        times = sorted({t} | {int(schedule[x]["finish_ns"]) + mn for x in lots_
                                              if int(schedule[x]["finish_ns"]) + mn > t})
                        ok = None
                        for tt in times[:200]:
                            if sum(_rem12[x] for x in lots_ if _alive12(x, tt, mn, mx)) >= n - max(1e-6, 1e-4 * n):
                                ok = tt
                                break
                        if ok is None:
                            short = True
                        else:
                            tout = max(tout, ok)
                    return tout, short

                def _fresh_take12(l, t):
                    for it, n in _needs12(l).items():
                        mn, mx = _ages_ns(it)
                        for x in sorted([x for x in _pool12.get(it, ()) if x != l and _rem12.get(x, 0) > 1e-9
                                         and _alive12(x, t, mn, mx)], key=lambda x: int(schedule[x]["finish_ns"])):
                            k = min(n, _rem12[x])
                            _rem12[x] -= k
                            n -= k
                            if n <= 1e-9:
                                break
                # ── BR-14 (v33): NO CHANGEOVER IN THE FIRST / LAST 30 MIN OF A SHIFT on upstream machines.
                # An item change (its changeover occupies [start - co, start)) may not overlap a handover window
                # (shift change +- window_min); it moves to the end of the window. Same-item runs continue.
                _HO = cfg.get("shift_handover") or {}
                _HO_ON = bool(_HO.get("enabled", False))
                _HO_W = int(float(_HO.get("window_min", 30)) * 60 * NS)
                _HO_B = [int(h) for h in (_HO.get("shift_starts_h") or [7, 15, 23])]
                _HO_EX = {str(x) for x in (_HO.get("exclude_machines") or [])}
                _n14 = 0

                def _ho_clear(e, co):
                    for _rep in range(6):
                        moved = False
                        day0 = pd.Timestamp(int(e - co)).normalize().value
                        for dd in (-1, 0, 1):
                            for h in _HO_B:
                                b = day0 + dd * 24 * 3600 * NS + h * 3600 * NS
                                if (e - co) < b + _HO_W and (e > b - _HO_W if co > 0 else e >= b - _HO_W):
                                    e = b + _HO_W + co
                                    moved = True
                        if not moved:
                            break
                    return e
                for _l in (_ups if _up_on else []):
                    _sc = schedule[_l]
                    _m = str(_sc["machine"])
                    _st = int(_sc["scheduled_start"].value)
                    _du = max(int(_sc["finish_ns"]) - _st, 0)
                    if _l in _PLANNED:                    # v56: fixed by the mixing planner
                        _last[_m] = (lot_item.get(_l, ""), int(_sc["finish_ns"]))
                        continue
                    _est = _st if (_l in _camp_of and _camp_of[_l][0] < 10 ** 6) else _cfloor(_l, _st)
                    _CRL = locals().get("_CREEL") or _CREEL_REF.get("c")
                    _is_steel = bool(_CRL) and _m == _CRL["m"] and _CRL["cls"](lot_item.get(_l, "")) == "steel"
                    if _is_steel and _m in _creel_last and _CRL["chg"](_creel_last[_m][0], lot_item.get(_l, "")):
                        _crr = _creel_last[_m][1] + _CRL["setup"]          # 4RC rule 5: one creel
                        if _crr > _est:
                            _est = _crr
                            _n_creel += 1
                    if _m in _last:
                        _pit, _pf = _last[_m]
                        _req = _pf + int(_co_req(_m, _pit, lot_item.get(_l, "")) * 60 * NS)
                        if _req > _est:
                            _est = _req
                            _n_co += 1
                    _mr = _material_ready_ns(_l)
                    if _mr > _est:
                        _est = _mr
                    # v47 RULE B: a NEW run of a compound starts >= gap after its previous run's start (same run =
                    # the lot directly continues the same compound on this mixer; block members move with their head)
                    _it47 = str(lot_item.get(_l, "")).strip()
                    _same47 = _m in _last and str(_last[_m][0]).strip() == _it47
                    _mem47 = _l in _camp_of and _camp_of[_l][0] in _camp_done
                    if _it47 in _GAP_W and not _same47 and not _mem47 and _it47 in _run47:
                        _g47 = _run47[_it47] + _GAP_W[_it47]
                        if _g47 > _est:
                            _est = _g47
                            _n47 += 1
                    if _l in _camp_of and _camp_of[_l][0] not in _camp_done:
                        # first member of a campaign: the block start must also satisfy every member's inputs
                        _ci, _off0, _cl = _camp_of[_l]
                        _blk = max([_est] + [int(_material_ready_ns(x)) - (_camp_of[x][1] - _off0) for x in _cl
                                             if _material_ready_ns(x) > NEG_INF_NS]
                                   + [_run47[str(lot_item.get(x, "")).strip()] + _GAP_W[str(lot_item.get(x, "")).strip()]
                                      - (_camp_of[x][1] - _off0) for x in _cl
                                      if x != _l and str(lot_item.get(x, "")).strip() != _it47
                                      and str(lot_item.get(x, "")).strip() in _GAP_W
                                      and str(lot_item.get(x, "")).strip() in _run47])
                        _span = max(int(schedule[x]["finish_ns"]) for x in _cl) - _st
                        _blk = _skip_down(_m, _blk, _span)
                        if _blk > _st or (_COMPACT and _blk < _st and _ci >= 10 ** 6):   # v54: units may move earlier
                            for x in _cl:                     # shift the later members now; the loop keeps them
                                if x == _l:
                                    continue
                                _dx = int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
                                _sx = _blk + (_camp_of[x][1] - _off0)
                                schedule[x]["scheduled_start"] = pd.Timestamp(int(_sx))
                                schedule[x]["scheduled_finish"] = pd.Timestamp(int(_sx + _dx))
                                schedule[x]["finish_ns"] = int(_sx + _dx)
                        _camp_done.add(_ci)
                        _est = _blk if (_COMPACT and _ci >= 10 ** 6) else max(_est, _blk)
                    if _AGH_ON:
                        _t12, _s12 = _fresh_time12(_l, _est)
                        if _t12 > _est:
                            _n12h += 1
                            _h12 += (_t12 - _est) / (3600.0 * NS)
                            _est = _t12
                        if _s12:
                            _n12s += 1
                            _short12.append(_l)
                    _est = _skip_down(_m, _est, _du)
                    if _HO_ON and _m not in _HO_EX and _m in _last and _last[_m][0] != lot_item.get(_l, ""):
                        _co14 = int(_co_req(_m, _last[_m][0], lot_item.get(_l, "")) * 60 * NS)
                        _e14 = _ho_clear(_est, _co14)
                        if _e14 > _est:
                            _n14 += 1
                            _est = _skip_down(_m, _e14, _du)
                    if _AGH_ON:
                        _fresh_take12(_l, _est)
                    if _est != _st:
                        _sc["scheduled_start"] = pd.Timestamp(int(_est))
                        _sc["scheduled_finish"] = pd.Timestamp(int(_est + _du))
                        _sc["finish_ns"] = int(_est + _du)
                        if _est > _st:
                            _n_up += 1
                            _h_up += (_est - _st) / (3600.0 * NS)
                        else:
                            _n_dn53[0] += 1
                            _h_dn53[0] += (_st - _est) / (3600.0 * NS)
                    _last[_m] = (lot_item.get(_l, ""), int(_sc["finish_ns"]))
                    if _it47 in _GAP_W and not _same47:
                        _run47[_it47] = int(_sc["scheduled_start"].value)
                    if _is_steel:
                        _creel_last[_m] = (lot_item.get(_l, ""), int(_sc["finish_ns"]))
                if _COMPACT and _up_on:
                    print(f"  [REPAIR COMPACTION v53] {_n_dn53[0]:,} upstream lots moved EARLIER towards their placement "
                          f"start ({_h_dn53[0]:,.0f} h; machine order, changeovers, inputs, PM, creel and shelf life kept)")
                if _GAP_W and _up_on:
                    print(f"  [REPAIR v47] mixing rule B: {_n47} compound runs held to keep the 15 h / 24 h repeat gap")
                if _n_creel:
                    print(f"  [REPAIR] 4RC rule 5: {_n_creel} steel lots held for the creel")
                if _AGH_ON and _up_on:
                    print(f"  [BR-12] aging hard block: {_n12h} upstream lots waited for FRESH input ({_h12:,.0f} h); "
                          f"{_n12s} with no fresh supply at all (remake needed -> listed)")
                    pd.DataFrame({"lot_id": _short12, "item": [lot_item.get(x, "") for x in _short12]}).to_csv(
                        OUTPUTS / "phase5_aging_no_fresh_supply.csv", index=False)
                if _HO_ON and _up_on:
                    print(f"  [BR-14] handover: {_n14} item changes moved out of the first / last "
                          f"{_HO_W / 60 / NS:.0f} min of a shift")
            _by_m = defaultdict(list)
            for _l in _ups:
                _by_m[str(schedule[_l]["machine"])].append(
                    (int(schedule[_l]["scheduled_start"].value), int(schedule[_l]["finish_ns"])))
            for _m, _iv0 in _by_m.items():
                machine_busy[_m] = sorted(_iv0)

            # build blocks: aging-valid FEFO kit
            _pools = defaultdict(list)                     # item -> [avail, expiry, remaining]
            for _it, _lst in _L_sup.items():
                if _assumed(_it):
                    continue
                _mn, _mx = _ages_ns(_it)
                if not cfg.get("final_repair_hard_max_aging", True):
                    _mx = 2 ** 62 // 4                     # expiry reported, not a release block
                for _L, _qv in _lst:
                    _f = int(schedule[_L]["finish_ns"])
                    _pools[_it].append([_f + _mn, _f + _mx, float(_qv)])
            try:
                _share = _L_share
            except NameError:
                _share = {}
            try:
                _iv = pd.read_csv(INPUTS / "inventory.csv", low_memory=False)
                _iv["itemcode"] = _iv["itemcode"].astype(str).str.strip()
                _iv = _iv[_iv["itemcode"].isin(set(_L_sup) | set(_L_wip))]
                _qq = pd.to_numeric(_iv["inventory"], errors="coerce").fillna(0.0)
                _uu = _iv.get("unit", pd.Series("", index=_iv.index)).astype(str).str.upper().str.strip()
                _qq = _qq * _uu.map({"MM": 0.001, "CM": 0.01}).fillna(1.0)
                _pt = pd.to_datetime(_iv["produced_time"], errors="coerce", format="mixed")
                _t0r = int(bp["startTime"].min().value)
                for _it, _q, _p in zip(_iv["itemcode"], _qq, _pt):
                    if _assumed(_it) or _q <= 0:
                        continue
                    _mn, _mx = _ages_ns(_it)
                    _mx_real = _mx                        # stock already expired at plan start is never
                    if not cfg.get("final_repair_hard_max_aging", True):   # usable (same rule as the ledger)
                        _mx = 2 ** 62 // 4
                    _q = _q * _share.get(_it, 1.0)
                    if pd.isna(_p):
                        _pools[_it].append([NEG_INF_NS, 2 ** 62, _q])
                    else:
                        _pv = int(pd.Timestamp(_p).value)
                        if _pv + _mx_real < _t0r:
                            continue                      # already expired at plan start
                        _pools[_it].append([_pv + _mn, _pv + _mx, _q])
            except Exception as _e:
                print(f"  [REPAIR] !! opening WIP for the aging-valid kit unreadable ({_e}) -- WIP skipped")
            for _it in _pools:
                _pools[_it].sort(key=lambda x: (x[1], x[0]))

            def _avail(_it, _t):
                return sum(p[2] for p in _pools[_it] if p[0] <= _t <= p[1] and p[2] > 1e-9)

            _tbm_last = defaultdict(lambda: NEG_INF_NS)
            _tbm_lgt = {}                                    # V23: last GT on the TBM (changeover)
            _new_commits = []
            _n_b = 0
            _h_b = 0.0
            _n_short = 0
            for _st0, _sku, _q, _lid in sorted(_L_commits):
                _sc = schedule[_lid]
                _m = str(_sc["machine"])
                _s0 = int(_sc["scheduled_start"].value)
                _du = int(_sc["finish_ns"]) - _s0
                _b0 = min(_s0, _BFLOOR0.get(_lid, _s0)) if _COMPACT else _s0      # v53: build may return to placement
                _t = max(_b0, _tbm_last[_m] + _bco_ns(_m, _tbm_lgt.get(_m), str(_sc.get("item", ""))))
                _need = {_it: _q * _r for _it, _r in _L_rate.get(_sku, {}).items()
                         if _it in _pools and not _assumed(_it) and not (_L_day1.get(_lid) and _first_item(_it))}
                for _guard in range(400):
                    _t = _skip_down(_m, _t, _du)
                    _late = [_it for _it, _nq in _need.items()
                             if _avail(_it, _t) < _nq - max(1e-6, 1e-4 * _nq)]
                    if not _late:
                        break
                    _nxt = [p[0] for _it in _late for p in _pools[_it] if p[0] > _t and p[2] > 1e-9]
                    if not _nxt:
                        _n_short += 1
                        break                             # no more supply: release with what exists
                    _t = min(_nxt)
                for _it, _nq in _need.items():            # FEFO take (expired never taken)
                    _left = _nq
                    for _p in _pools[_it]:
                        if _left <= 1e-9:
                            break
                        if _p[2] > 1e-9 and _p[0] <= _t <= _p[1]:
                            _k = min(_left, _p[2])
                            _p[2] -= _k
                            _left -= _k
                if _t != _s0:
                    if _t > _s0:
                        _n_b += 1
                        _h_b += (_t - _s0) / (3600.0 * NS)
                    _sc["scheduled_start"] = pd.Timestamp(int(_t))
                    _sc["scheduled_finish"] = pd.Timestamp(int(_t + _du))
                    _sc["finish_ns"] = int(_t + _du)
                _tbm_last[_m] = int(_sc["finish_ns"])
                _tbm_lgt[_m] = str(_sc.get("item", ""))
                _new_commits.append((int(_t), _sku, _q, _lid))
                _rep[_lid] = int(_t)
            _L_commits[:] = _new_commits
            if _rep and _gate_rows:                         # gate log reports the FINAL start
                for _g in _gate_rows:
                    _k = f"BLD{int(_g['row']):05d}"
                    if _k in _rep:
                        _g["start"] = pd.Timestamp(_rep[_k])
                pd.DataFrame(_gate_rows).to_csv(OUTPUTS / "phase5_building_echo_gates.csv", index=False)
            print(f"  [REPAIR] BR-7/10: {_n_up} upstream lots pushed later ({_h_up:,.0f} h total; "
                  f"{_n_co} for a changeover) | BR-12/66: {_n_b} build blocks later for an aging-valid "
                  f"kit or TBM ({_h_b:,.0f} h total), {_n_short} released with the last supply (short)")

        def _il_day_fix():
            """v18: two IL campaigns whose first IL lot falls on the same plant day (after the repair moved
            them) -> ONE block: [B99s] [all lots of the first IL item] [all lots of the other], starting no
            earlier than the earlier block and no lot before its inputs are ready; other lots on the mixer
            keep their order and move only later. Returns the number of merges."""
            _ili = {str(x).strip() for x in (_ILC.get("items") or ["IL3", "IL5"])}
            _pp = str(_ILC.get("prep", "B99")).strip()
            _cm = locals().get("_co_min") or globals().get("_co_min")
            _co_fn = (lambda m_, a_, b_: float(_co_min_v18(m_, a_, b_)))
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _it = lambda l: str(lot_item.get(l, "")).strip()
            n = 0
            _skipped = 0
            _tried = set()
            _ib = defaultdict(list)                       # current build starts per GT-level item
            for _st0, _sku0, _q0, _lid0 in _L_commits:
                for _it0 in _L_rate.get(_sku0, {}):
                    _ib[_it0].append(int(_st0))
            for _k0 in _ib:
                _ib[_k0].sort()
            _sc_ = locals().get("_succ") or {}

            def _dl(x, fin_old):
                """latest finish that starts no consumer later (consumer start - min aging)."""
                d = None
                for _c in (_SUCC_REF.get("s") or {}).get(x, ()):
                    if _c in schedule and not str(_c).startswith("BLD"):
                        v = schedule[_c]["scheduled_start"].value - int(_ages_ns(_it(x))[0])
                        d = v if d is None else min(d, v)
                _bs0 = _ib.get(_it(x))
                if _bs0:
                    _ix = bisect.bisect_right(_bs0, fin_old)
                    if _ix < len(_bs0):
                        v = _bs0[_ix] - int(_ages_ns(_it(x))[0])
                        d = v if d is None else min(d, v)
                return fin_old if d is None else max(fin_old, d)
            for m in sorted({str(r.get("machine")) for l, r in schedule.items() if _it(l) in _ili}):
                for _round in range(50):
                    seq = sorted([l for l, r in schedule.items() if str(r.get("machine")) == m
                                  and str(r.get("department", "")).upper() not in ("BUILDING", "CURING")
                                  and not str(l).startswith("BLD")],
                                 key=lambda l: schedule[l]["scheduled_start"].value)
                    blocks, i = [], 0
                    while i < len(seq):
                        if _it(seq[i]) in _ili:
                            j = i
                            while j + 1 < len(seq) and _it(seq[j + 1]) in _ili:
                                j += 1
                            k = i
                            while k - 1 >= 0 and _it(seq[k - 1]) == _pp:
                                k -= 1
                            blocks.append(seq[k:j + 1])
                            i = j + 1
                        else:
                            i += 1
                    _day = lambda blk: (min(schedule[x]["scheduled_start"].value for x in blk if _it(x) in _ili) - _H7) // _D
                    pair = next(((A, B) for A, B in zip(blocks, blocks[1:]) if _day(A) == _day(B)
                                 and (A[0], B[0]) not in _tried), None)
                    if pair is None:
                        break
                    A, B = pair
                    blk = A + B
                    pre = sorted([x for x in blk if _it(x) not in _ili], key=lambda x: schedule[x]["scheduled_start"].value)
                    ils = [x for x in blk if _it(x) in _ili]
                    order = list(dict.fromkeys(_it(x) for x in sorted(ils, key=lambda x: schedule[x]["scheduled_start"].value)))
                    ils.sort(key=lambda x: (order.index(_it(x)), schedule[x]["scheduled_start"].value))
                    new = pre + ils
                    du = {x: int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value for x in seq}
                    off, t, pv = {}, 0, ""
                    for x in new:
                        t += int(_co_fn(m, pv, _it(x)) * 60 * NS) if pv else 0
                        off[x] = t
                        t += du[x]
                        pv = _it(x)
                    a0 = schedule[A[0]]["scheduled_start"].value
                    _rd = lambda x: max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))
                    S = max([a0] + [_rd(x) - off[x] for x in new])
                    newset = set(new)
                    before = [x for x in seq if x not in newset and schedule[x]["scheduled_start"].value < a0]
                    others = [x for x in seq if x not in newset and schedule[x]["scheduled_start"].value >= a0]
                    prev_end = int(schedule[before[-1]]["finish_ns"]) if before else NEG_INF_NS
                    prev_it = _it(before[-1]) if before else ""
                    lay, placed, defer = [], False, []

                    def _put_block():
                        nonlocal prev_end, prev_it
                        st0 = max(S, prev_end + (int(_co_fn(m, prev_it, _it(new[0])) * 60 * NS) if prev_it else 0))
                        for x in new:
                            lay.append((x, st0 + off[x], st0 + off[x] + du[x]))
                        prev_end, prev_it = lay[-1][2], _it(new[-1])

                    for x in others:
                        s0 = max(schedule[x]["scheduled_start"].value,
                                 prev_end + (int(_co_fn(m, prev_it, _it(x)) * 60 * NS) if prev_it else 0))
                        if not placed and s0 + du[x] + int(_co_fn(m, _it(x), _it(new[0])) * 60 * NS) > S:
                            _put_block()
                            placed = True
                            s0 = max(schedule[x]["scheduled_start"].value,
                                     prev_end + int(_co_fn(m, prev_it, _it(x)) * 60 * NS))
                        lay.append((x, s0, s0 + du[x]))
                        prev_end, prev_it = s0 + du[x], _it(x)
                    if not placed:
                        _put_block()
                    _tried.add((A[0], B[0]))
                    if any(f0 > _dl(x, int(schedule[x]["finish_ns"])) + 60 * NS for x, s0, f0 in lay):
                        _skipped += 1                   # strict: a consumer would have to start later
                        continue
                    lots_on = {schedule[x]["scheduled_start"].value for x in seq}
                    keep = [iv for iv in machine_busy.get(m, []) if iv[0] not in lots_on]
                    for x, s0, f0 in lay:
                        schedule[x]["scheduled_start"] = pd.Timestamp(int(s0))
                        schedule[x]["scheduled_finish"] = pd.Timestamp(int(f0))
                        schedule[x]["finish_ns"] = int(f0)
                    machine_busy[m] = sorted(keep + [(int(schedule[x]["scheduled_start"].value), int(schedule[x]["finish_ns"]))
                                                     for x in seq])
                    IL_PINNED.update(new)
                    for _r in _il_rows:                 # final repair moves campaigns as units: one unit now
                        _r["lots"] = ";".join(x for x in str(_r.get("lots", "")).split(";") if x and x not in newset)
                    _il_rows.append({"lots": ";".join(new), "machine": m, "merged_after_repair": True})
                    n += 1
            if n or _skipped:
                print(f"  [BR-V10 v18] after the repair: {n} same-day IL campaign pair(s) merged into one B99 -> IL "
                      f"block; {_skipped} left (merging would start a consumer later)")
            return n

        def _il_once_per_day():
            """v20 (plant rule 3.1.3): ONE IL3/IL5 campaign per plant day, 07:00 -> 07:00 (shifts A, B, C).
            For a day with 2+ campaigns the later one is (a) merged into that day's first campaign when its
            inputs are ready by then, else (b) moved whole to the next plant day without a campaign, else
            (c) merged into the next day's campaign. Block = [B99s][all of the first IL item][all of the
            other]. Lots on the mixer that are in the way move later in their order; every consumer that
            would now start before its input is ready (+ min aging) is pushed later, cascading on its own
            machine. Returns (#days fixed, #lots pushed)."""
            _ili = {str(x).strip() for x in (_ILC.get("items") or ["IL3", "IL5"])}
            _pp = str(_ILC.get("prep", "B99")).strip()
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _co = lambda m_, a_, b_: int(_co_min_v18(m_, a_, b_) * 60 * NS) if a_ else 0
            _du = lambda x: int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
            _up = lambda x: (str(schedule[x].get("department", "")).upper() not in ("BUILDING", "CURING")
                             and not str(x).startswith("BLD"))
            _sc_ = _SUCC_REF.get("s") or {}
            n_fix = n_push = 0

            def _mlots(m):
                return sorted([x for x, r in schedule.items() if str(r.get("machine")) == m and _up(x)],
                              key=lambda x: schedule[x]["scheduled_start"].value)

            def _set(x, t):
                d = _du(x)
                schedule[x]["scheduled_start"] = pd.Timestamp(int(t))
                schedule[x]["scheduled_finish"] = pd.Timestamp(int(t + d))
                schedule[x]["finish_ns"] = int(t + d)

            def _lay(m, fixed):
                """fixed: {lot: start} placed exactly; every other lot on m keeps its order and only moves
                later to clear the fixed lots and its changeovers. Returns the lots that moved."""
                moved = []
                busy = sorted((t, t + _du(x), x) for x, t in fixed.items())
                for x, t in fixed.items():
                    if schedule[x]["scheduled_start"].value != t:
                        moved.append((x, int(schedule[x]["finish_ns"])))
                        _set(x, t)
                L = _mlots(m)                              # current order, fixed lots already placed
                prev_end, prev_it = NEG_INF_NS, ""
                for x in L:
                    if x in fixed:
                        if int(schedule[x]["finish_ns"]) >= prev_end:
                            prev_end, prev_it = int(schedule[x]["finish_ns"]), _it(x)
                        continue
                    st0 = schedule[x]["scheduled_start"].value
                    t = max(st0, prev_end + _co(m, prev_it, _it(x)))
                    for bs, be, bx in busy:                # never inside a fixed block
                        if t < be and t + _du(x) > bs:
                            t = max(t, be + _co(m, _it(bx), _it(x)))
                    if t != st0:
                        moved.append((x, int(schedule[x]["finish_ns"])))
                        _set(x, t)
                    prev_end, prev_it = int(schedule[x]["finish_ns"]), _it(x)
                return moved

            def _propagate(moved):
                """push consumers whose input is now ready later than they start (they depended on it)."""
                nonlocal n_push
                q = list(moved)
                while q:
                    p, old_fin = q.pop()
                    fin = int(schedule[p]["finish_ns"])
                    mn = int(_ages_ns(_it(p))[0])
                    for c in _sc_.get(p, ()):
                        if c not in schedule or not _up(c):
                            continue
                        cs = schedule[c]["scheduled_start"].value
                        if cs >= old_fin + mn - 60 * NS and cs < fin + mn:   # it waited for p; now too early
                            m = str(schedule[c]["machine"])
                            _oc = int(schedule[c]["finish_ns"])
                            _set(c, fin + mn)
                            mv = [(c, _oc)] + _lay(m, {})
                            n_push += len(mv)
                            q.extend(mv)

            def _blocks(m):
                L = _mlots(m)
                out, i = [], 0
                while i < len(L):
                    if _it(L[i]) in _ili:
                        j = i
                        while j + 1 < len(L) and _it(L[j + 1]) in _ili:
                            j += 1
                        k = i
                        while k - 1 >= 0 and _it(L[k - 1]) == _pp:
                            k -= 1
                        out.append(L[k:j + 1])
                        i = j + 1
                    else:
                        i += 1
                return out

            _dayof = lambda blk: (min(schedule[x]["scheduled_start"].value for x in blk if _it(x) in _ili) - _H7) // _D

            def _regroup(lots_):
                pre = sorted([x for x in lots_ if _it(x) not in _ili], key=lambda x: schedule[x]["scheduled_start"].value)
                ils = [x for x in lots_ if _it(x) in _ili]
                order = list(dict.fromkeys(_it(x) for x in sorted(ils, key=lambda x: schedule[x]["scheduled_start"].value)))
                ils.sort(key=lambda x: (order.index(_it(x)), schedule[x]["scheduled_start"].value))
                return pre + ils

            def _offs(m, seq):
                o, t, pv = {}, 0, ""
                for x in seq:
                    t += _co(m, pv, _it(x))
                    o[x] = t
                    t += _du(x)
                    pv = _it(x)
                return o

            def _ready(x):
                return max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))

            for m in sorted({str(r.get("machine")) for x, r in schedule.items() if _it(x) in _ili}):
                for _guard in range(60):
                    B = _blocks(m)
                    days = defaultdict(list)
                    for b in B:
                        days[_dayof(b)].append(b)
                    bad = sorted(d for d, v in days.items() if len(v) > 1)
                    if not bad:
                        break
                    d = bad[0]
                    first, late = days[d][0], days[d][1]
                    plan = None
                    # (a) merge the later campaign into the day's first one (inputs ready by then)
                    seq = _regroup(first + late)
                    o = _offs(m, seq)
                    S = max([schedule[first[0]]["scheduled_start"].value] + [_ready(x) - o[x] for x in seq])
                    if (S + min(o[x] for x in seq if _it(x) in _ili) - _H7) // _D == d:
                        plan = {x: S + o[x] for x in seq}
                    # (b) move it whole to the next plant day that has no campaign
                    if plan is None:
                        free = next((dd for dd in range(d + 1, d + 30) if dd not in days), None)
                        nxt = days.get(d + 1)
                        if free == d + 1:
                            seq = list(late)
                            o = _offs(m, seq)
                            S = max([(d + 1) * _D + _H7 - min(o[x] for x in seq if _it(x) in _ili),
                                     schedule[seq[0]]["scheduled_start"].value] + [_ready(x) - o[x] for x in seq])
                            plan = {x: S + o[x] for x in seq}
                        elif nxt:
                            # (c) join the next day's campaign (one block on that day)
                            seq = _regroup(late + nxt[0])
                            o = _offs(m, seq)
                            S = max([schedule[nxt[0][0]]["scheduled_start"].value,
                                     schedule[late[0]]["scheduled_start"].value] + [_ready(x) - o[x] for x in seq])
                            plan = {x: S + o[x] for x in seq}
                    if plan is None:
                        break
                    mv = _lay(m, plan)
                    IL_PINNED.update(plan)
                    n_push += len(mv)
                    _propagate(mv)
                    n_fix += 1
                # v38 BRM-09 inside a campaign: [B99s][all of the first IL item][all of the other] -- a block
                # running IL3 -> IL5 -> IL3 is regrouped in place (IL3 <-> IL5 is a plain changeover, no clean-out)
                for b in _blocks(m):
                    runs_ = [_it(x) for x in b if _it(x) in _ili]
                    runs_ = [y for i_, y in enumerate(runs_) if i_ == 0 or y != runs_[i_ - 1]]
                    if len(runs_) == len(set(runs_)):
                        continue
                    seq = _regroup(b)
                    o = _offs(m, seq)
                    S = max([schedule[b[0]]["scheduled_start"].value] + [_ready(x) - o[x] for x in seq])
                    plan = {x: S + o[x] for x in seq}
                    mv = _lay(m, plan)
                    IL_PINNED.update(plan)
                    n_push += len(mv)
                    _propagate(mv)
                    n_fix += 1
            print(f"  [3.1.3 v20] IL campaign once per plant day (07:00 -> 07:00): {n_fix} day(s) fixed; "
                  f"{n_push} lots re-timed later (mixer + consumers whose input moved)")
            return n_fix

        def _mix_catch_up():
            """v48 (user 2026-10-08): rule B without throttling. A compound lot that would start within the repeat gap
            (15 h / 24 h) of its compound's current run -- i.e. a late window that is already due -- JOINS that run:
            it is moved directly behind the run's last lot on the same mixer (one bigger run, rule A kept), when its
            inputs are ready by then and it is still within shelf life at its last use. The other lots on that mixer
            move later by its run time (the final repair then re-times changeovers / consumers, later-only). Lots in
            butyl / IL blocks and exempt compounds are left alone. Returns #lots joined."""
            if not _GAP_W or not (_MV47.get("catch_up_merge", False)):
                return 0
            _mxs = {str(x) for x in ((cfg.get("business_rules") or {}).get("mixers") or [])}
            _mgn = int(float(_MV47.get("shelf_margin_h", 2)) * 3600 * NS)
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _st = lambda l: int(schedule[l]["scheduled_start"].value)
            _fi = lambda l: int(schedule[l]["finish_ns"])
            _up = lambda l: (not str(l).startswith("BLD")
                             and str(schedule[l].get("department", "")).upper() not in ("BUILDING", "CURING"))
            byit = defaultdict(list)
            for l, r in schedule.items():
                if _up(l) and str(r.get("machine")) in _mxs and _it(l) in _GAP_W:
                    byit[_it(l)].append(l)
            n_j = 0
            for it_, ls in byit.items():
                ls.sort(key=_st)
                W = _GAP_W[it_]
                run0, last = _st(ls[0]), ls[0]
                for b in ls[1:]:
                    if _st(b) >= run0 + W:                 # far enough: a new run
                        run0, last = _st(b), b
                        continue
                    m = str(schedule[last]["machine"])
                    if b in IL_PINNED or last in IL_PINNED:
                        continue
                    if str(schedule[b]["machine"]) != m and not (
                            _DAYMIX_ON and m in [str(y).strip() for y in (lot_machines.get(b) or [])]):
                        continue                            # v49: may join the run on another eligible mixer
                    t = _fi(last)
                    if abs(_st(b) - t) <= 60 * NS:           # already directly behind: same run
                        last = b
                        continue
                    if int(_material_ready_ns(b)) > t:      # inputs not ready yet -> stays (repair holds the gap)
                        continue
                    _ln = lot_last_need.get(b)
                    _mx = _ages_ns(it_)[1]
                    d = _fi(b) - _st(b)
                    if _ln is not None and _mx and _mx > 0 and (int(_ln) - (t + d)) > _mx - _mgn:
                        continue                            # would be over-age at its last use
                    # make room: lots on m starting at/after t move later by the run time of b
                    later = sorted([x for x, r in schedule.items() if x != b and str(r.get("machine")) == m and _up(x)
                                    and _st(x) >= t], key=_st)
                    schedule[b]["machine"] = m
                    schedule[b]["scheduled_start"] = pd.Timestamp(int(t))
                    schedule[b]["scheduled_finish"] = pd.Timestamp(int(t + d))
                    schedule[b]["finish_ns"] = int(t + d)
                    pe = t + d
                    for x in later:
                        if _st(x) < pe:
                            dx = _fi(x) - _st(x)
                            schedule[x]["scheduled_start"] = pd.Timestamp(int(pe))
                            schedule[x]["scheduled_finish"] = pd.Timestamp(int(pe + dx))
                            schedule[x]["finish_ns"] = int(pe + dx)
                            pe += dx
                        else:
                            break
                    last = b
                    n_j += 1
            print(f"  [MIXING v48 CATCH-UP] {n_j} late compound lots joined the current run of their compound "
                  f"(one bigger run on the same mixer instead of waiting the 15 h / 24 h gap)")
            return n_j

        def _mix_day_fix():
            """v49 rule A safety net: after the passes, a compound found on 2+ mixers in one plant day has its lots on the
            other mixer(s) moved behind its run on the mixer that holds most of that day's batches (routing permitting);
            the lots after it on that mixer move later (the final repair re-times them). Returns #lots moved."""
            if not _DAYMIX_ON:
                return 0
            _mxs = {str(x) for x in ((cfg.get("business_rules") or {}).get("mixers") or [])}
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _st = lambda l: int(schedule[l]["scheduled_start"].value)
            _fi = lambda l: int(schedule[l]["finish_ns"])
            grp = defaultdict(list)
            for l, r in schedule.items():
                if (str(r.get("machine")) in _mxs and not str(l).startswith("BLD")
                        and str(lot_item_type.get(l, "")).upper() in ("MASTER COMPOUND", "FINAL COMPOUND")):
                    grp[(_it(l), (_st(l) - _H7) // _D)].append(l)
            n_m = 0
            for (it_, d_), ls in grp.items():
                ms = defaultdict(float)
                for l in ls:
                    ms[str(schedule[l]["machine"])] += _fi(l) - _st(l)
                if len(ms) < 2:
                    continue
                tgt = max(ms, key=lambda k: ms[k])
                for b in sorted([l for l in ls if str(schedule[l]["machine"]) != tgt], key=_st):
                    if b in IL_PINNED or tgt not in [str(y).strip() for y in (lot_machines.get(b) or [])]:
                        continue
                    run = sorted([l for l in ls if str(schedule[l]["machine"]) == tgt], key=_st)
                    t = max(_fi(run[-1]), int(_material_ready_ns(b)))
                    d = _fi(b) - _st(b)
                    later = sorted([x for x, r in schedule.items() if x != b and str(r.get("machine")) == tgt
                                    and not str(x).startswith("BLD") and _st(x) >= _fi(run[-1])], key=_st)
                    schedule[b]["machine"] = tgt
                    schedule[b]["scheduled_start"] = pd.Timestamp(int(t))
                    schedule[b]["scheduled_finish"] = pd.Timestamp(int(t + d))
                    schedule[b]["finish_ns"] = int(t + d)
                    pe = t + d
                    for x in later:
                        if _st(x) < pe:
                            dx = _fi(x) - _st(x)
                            schedule[x]["scheduled_start"] = pd.Timestamp(int(pe))
                            schedule[x]["scheduled_finish"] = pd.Timestamp(int(pe + dx))
                            schedule[x]["finish_ns"] = int(pe + dx)
                            pe += dx
                        else:
                            break
                    n_m += 1
            print(f"  [MIXING v49 RULE A] {n_m} lots moved so that each compound is on ONE mixer per plant day")
            return n_m

        def _butyl_day_blocks():
            """v39 (user 2026-10-06, rules of 01-Oct): ONE butyl block per group per plant day (07:00 -> 07:00), butyl
            and butyl-friendly compounds together in sequence, least changeovers:
              final  (config butyl_day_block.groups.final):  F -> butyl X -> 120 s -> F -> butyl Y -> 120 s -> F -> ...
              master (groups.master, friendly_after: true):  F -> X -> 120 s -> F -> Y -> 120 s -> F -> ... -> F
            F = a real butyl-friendly lot of that day (alternating items where possible), else a prep lot of
            prep_lot_batches. All of a day's lots of one butyl compound run together (IL3 / IL5 in one go). The block
            goes on the group's mixer (routing permitting), starts where the day's butyl production was (not before
            its inputs are ready; a block pushed into the next day merges with it), other lots on the mixer move
            later, consumers whose input moved are re-timed. Returns (#blocks, new prep lot rows)."""
            _BD = cfg.get("butyl_day_block") or {}
            _cl_ns = int(float(_BD.get("cleanout_after_sec", 120)) * NS)
            _PB = int(_BD.get("prep_lot_batches", 5))
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _du = lambda x: int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
            _up = lambda x: (str(schedule[x].get("department", "")).upper() not in ("BUILDING", "CURING")
                             and not str(x).startswith("BLD"))
            _sc_ = _SUCC_REF.get("s") or {}
            _pday = lambda ns: (int(ns) - _H7) // _D
            groups = []
            for gname, g in sorted((_BD.get("groups") or {}).items(), key=lambda kv: not kv[1].get("friendly_after", False)):
                groups.append({"name": gname, "mixer": str(g.get("mixer", "")),
                               "butyl": [str(x) for x in (g.get("butyl") or [])],
                               "friendly": [str(x) for x in (g.get("friendly") or [])],
                               "after": bool(g.get("friendly_after", False)),
                               "campaigns": [[str(y) for y in c_] for c_ in (g.get("campaigns") or [])]})
            all_butyl = {b for g in groups for b in g["butyl"]}
            _campof = {c: (g["name"], i) for g in groups for i, cs in enumerate(g["campaigns"]) for c in cs}
            _samecamp = lambda a, b: a in _campof and _campof.get(a) == _campof.get(b)

            def _co(m, a, b):
                if not a or a == b:
                    return 0
                c = int(_co_min_v18(m, a, b) * 60 * NS)
                if a in all_butyl and not _samecamp(a, b):  # Final Mixer - After Butyl: 120 s clean-out (after a campaign)
                    c = max(c, _cl_ns)
                return c

            def _mlots(m):
                return sorted([x for x, r in schedule.items() if str(r.get("machine")) == m and _up(x)],
                              key=lambda x: schedule[x]["scheduled_start"].value)

            def _set(x, t, m=None):
                d = _du(x)
                if m is not None:
                    schedule[x]["machine"] = m
                schedule[x]["scheduled_start"] = pd.Timestamp(int(t))
                schedule[x]["scheduled_finish"] = pd.Timestamp(int(t + d))
                schedule[x]["finish_ns"] = int(t + d)

            def _lay(m, fixed):
                moved = []
                for x, t in fixed.items():                # the block first, exactly where planned
                    if schedule[x]["scheduled_start"].value != t or str(schedule[x]["machine"]) != m:
                        moved.append((x, int(schedule[x]["finish_ns"])))
                        _set(x, t, m)
                busy = sorted((t, t + _du(x), x) for x, t in fixed.items())
                L = _mlots(m)
                prev_end, prev_it = NEG_INF_NS, ""
                for x in L:
                    if x in fixed:
                        if int(schedule[x]["finish_ns"]) >= prev_end:
                            prev_end, prev_it = int(schedule[x]["finish_ns"]), _it(x)
                        continue
                    st0 = schedule[x]["scheduled_start"].value
                    t = max(st0, prev_end + _co(m, prev_it, _it(x)))
                    for bs, be, bx in busy:
                        if t < be + _co(m, _it(bx), _it(x)) and t + _du(x) + _co(m, _it(x), _it(bx)) > bs:
                            t = max(t, be + _co(m, _it(bx), _it(x)))
                    t = int(_skip_dn(m, t, _du(x)))
                    if t != st0:
                        moved.append((x, int(schedule[x]["finish_ns"])))
                        _set(x, t)
                    prev_end, prev_it = int(schedule[x]["finish_ns"]), _it(x)
                return moved

            def _skip_dn(m, t, d):
                for _a, _b in sorted(DOWNTIME_WIN.get(m, ())):
                    if t < _b and t + d > _a:
                        t = _b
                return t

            def _propagate(moved):
                q = list(moved)
                n_ = 0
                while q:
                    p, old_fin = q.pop()
                    fin = int(schedule[p]["finish_ns"])
                    mn = int(_ages_ns(_it(p))[0])
                    for c in _sc_.get(p, ()):
                        if c not in schedule or not _up(c):
                            continue
                        cs = schedule[c]["scheduled_start"].value
                        if cs < fin + mn:
                            m = str(schedule[c]["machine"])
                            _oc = int(schedule[c]["finish_ns"])
                            _set(c, fin + mn)
                            mv = [(c, _oc)] + _lay(m, {})
                            n_ += len(mv)
                            q.extend(mv)
                return n_

            def _ready(x):
                return max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))

            new_rows, n_blk, n_prep, n_push = [], 0, 0, 0
            report = []

            def _prep(item, m):
                nonlocal n_prep
                t_ = lots[lots["item_code"].astype(str).str.strip() == item]
                if t_.empty:
                    return None
                tr = t_.iloc[0].to_dict()
                bs = float(tr.get("batch_size_kg") or 0) or float(tr["lot_qty"]) / max(float(tr.get("batches") or 1), 1)
                q = _PB * bs
                h = float(tr["duration_hours"]) * q / max(float(tr["lot_qty"]), 1e-9)
                n_prep += 1
                p_ = f"{item}_BPREP{n_prep:03d}"
                tr.update(lot_id=p_, raw_demand=0.0, lot_qty=q, surplus=q, duration_min=h * 60, duration_hours=h,
                          first_need=pd.NaT, last_need=pd.NaT, n_blocks=0, n_skus=0, machines=m, n_machines=1,
                          campaign_id=np.nan, batch_id=p_, skus_set="", batches=float(_PB), machines_l=[m])
                new_rows.append(tr)
                lot_item[p_] = item
                lot_item_type[p_] = str(tr.get("item_type", "")).strip().upper()
                lot_op[p_], lot_dept[p_], lot_eq[p_] = tr["operation"], tr["department"], tr["equipment"]
                lot_machines[p_], duration_h[p_] = [m], h
                lot_first_need[p_], lot_last_need[p_] = pd.NaT, None
                lot_qty[p_] = q
                schedule[p_] = {"lot_id": p_, "item": item, "machine": m, "operation": tr["operation"],
                                "department": tr["department"], "equipment": tr["equipment"],
                                "scheduled_start": pd.Timestamp(0), "scheduled_finish": pd.Timestamp(int(h * 3600 * NS)),
                                "duration_h": h, "campaign_id": "", "wire_type": "", "is_camp_first": False,
                                "is_camp_last": False, "finish_ns": int(h * 3600 * NS)}
                return p_

            def _ok_on(x, m):
                ms = lot_machines.get(x) or []
                return (not ms) or m in [str(y).strip().strip('"') for y in ms] or str(schedule[x]["machine"]) == m

            used = set()
            _bl47 = {}                                       # v47: compound -> start of its run in the previous block
            for G in groups:
                days = defaultdict(list)
                for x, r in schedule.items():
                    if _up(x) and _it(x) in G["butyl"]:
                        days[_pday(r["scheduled_start"].value)].append(x)
                done = set()
                for d in sorted(days):
                    if d in done:
                        continue
                    pool = [x for x in days[d] if x not in used]
                    while True:
                        if not pool:
                            break
                        # the block's mixer: the group's mixer where routing allows every lot, else the mixer of most lots
                        M = G["mixer"] if all(_ok_on(x, G["mixer"]) for x in pool) else \
                            max({str(schedule[x]["machine"]) for x in pool},
                                key=lambda mm: sum(1 for x in pool if str(schedule[x]["machine"]) == mm))
                        stay = [x for x in pool if not _ok_on(x, M)]
                        blk_b = [x for x in pool if _ok_on(x, M)]
                        if not blk_b:
                            break
                        cmp_order = list(dict.fromkeys(_it(x) for x in sorted(
                            blk_b, key=lambda x: schedule[x]["scheduled_start"].value)))
                        cmp_order.sort(key=lambda c: (G["butyl"].index(c) if c in G["butyl"] else 99,))
                        # friendly lots of this day (any mixer, routing allows M), not yet used
                        fr = sorted([x for x, r in schedule.items() if _up(x) and _it(x) in G["friendly"] and x not in used
                                     and _pday(r["scheduled_start"].value) == d and _ok_on(x, M)],
                                    key=lambda x: schedule[x]["scheduled_start"].value)
                        # v41: compounds of one campaign (IL3 + IL5) are ONE unit: one butyl-friendly lot in front of the
                        # campaign, the compounds back to back in one go (plain changeover, no clean-out between)
                        units = []
                        for c in cmp_order:
                            if units and _samecamp(units[-1][-1], c):
                                units[-1].append(c)
                            else:
                                units.append([c])
                        n_slots = len(units) + (1 if G["after"] else 0)
                        slots, prev_f = [], None
                        for k in range(n_slots):
                            cand = [x for x in fr if _it(x) != prev_f] or fr
                            if cand:
                                x = cand[0]
                                fr.remove(x)
                            else:
                                item = next((f for f in G["friendly"] if f != prev_f and
                                             not lots[lots["item_code"].astype(str).str.strip() == f].empty),
                                            next((f for f in G["friendly"]
                                                  if not lots[lots["item_code"].astype(str).str.strip() == f].empty), None))
                                x = _prep(item, M) if item else None
                            if x is not None:
                                slots.append(x)
                                prev_f = _it(x)
                        seq = []
                        for k, un in enumerate(units):
                            if k < len(slots):
                                seq.append(slots[k])
                            for c in un:
                                seq += sorted([x for x in blk_b if _it(x) == c], key=lambda x: schedule[x]["scheduled_start"].value)
                        if G["after"] and len(slots) > len(units):
                            seq.append(slots[len(units)])
                        off, t, pv = {}, 0, ""
                        for x in seq:
                            t += _co(M, pv, _it(x))
                            off[x] = t
                            t += _du(x)
                            pv = _it(x)
                        b0 = min(schedule[x]["scheduled_start"].value for x in blk_b)
                        f0 = off[next(x for x in seq if _it(x) in G["butyl"])]
                        S = max([b0 - f0] + [_ready(x) - off[x] for x in seq if schedule[x]["scheduled_start"].value > 0
                                             or x in blk_b])
                        _fst47 = {}
                        for x in seq:
                            _fst47.setdefault(_it(x), x)
                        for c_, x in _fst47.items():           # v47 rule B: >= gap after this compound's previous block
                            if c_ in _GAP_W and c_ in _bl47:
                                S = max(S, _bl47[c_] + _GAP_W[c_] - off[x])
                        S = int(_skip_dn(M, S, t))
                        d_new = _pday(S + f0)
                        if d_new > d and d_new in days and d_new not in done:
                            pool = pool + [x for x in days[d_new] if x not in used and x not in pool]
                            done.add(d_new)
                            for x in slots:                     # re-pick friendly lots for the merged block
                                if str(x).find("_BPREP") >= 0:
                                    schedule.pop(x, None)
                                    new_rows[:] = [r_ for r_ in new_rows if r_["lot_id"] != x]
                            continue
                        plan = {x: S + off[x] for x in seq}
                        fixed = {x: schedule[x]["scheduled_start"].value for x in used
                                 if str(schedule[x]["machine"]) == M}
                        fixed.update(plan)
                        mv = _lay(M, fixed)
                        n_push += len(mv)
                        for c_, x in _fst47.items():
                            _bl47[c_] = S + off[x]
                        used.update(seq)
                        IL_PINNED.update(seq)
                        n_blk += 1
                        report.append({"group": G["name"], "plant_day": pd.Timestamp(int(d * _D + _H7)), "mixer": M,
                                       "sequence": " -> ".join(_it(x) for x in seq), "lots": ";".join(seq),
                                       "start": pd.Timestamp(int(S)), "end": pd.Timestamp(int(S + t)),
                                       "prep_lots": sum(1 for x in seq if "_BPREP" in x)})
                        pool = [x for x in stay if x not in used]
                        if pool:
                            continue
                        break
            if report:
                pd.DataFrame(report).to_csv(OUTPUTS / "phase5_butyl_day_blocks.csv", index=False)
                _il_rows[:] = [{"lots": r_["lots"], "machine": r_["mixer"]} for r_ in report]
            print(f"  [BUTYL DAY BLOCK v39] {n_blk} daily butyl blocks (final + master, butyl-friendly before"
                  f"{' / after' if any(g['after'] for g in groups) else ''} every butyl compound, 120 s clean-out after it); "
                  f"{n_prep} prep lot(s) of {_PB} batches made; {n_push} lots on the block mixers moved later (consumers: final repair)")
            return n_blk, new_rows

        def _fourrc_one_go_fix():
            """v39 4RC-7 after the repairs: a repair push can carry a run of an item across 07:00 into a plant day
            that has its own run of the item (two runs that day). The later run is moved directly behind the earlier
            one when it is its whole wire / dip run (so every wire run stays whole spools, 4RC-8) and its inputs are
            ready; the lots in between move later (changeovers, one creel, PM kept). Returns #runs moved."""
            _CRL = _CREEL_REF.get("c") or _CREEL
            if not _CRL:
                return 0
            m = _CRL["m"]
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _du = lambda x: int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            n_mv = 0
            for _rep in range(30):
                L = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m and not str(x).startswith("BLD")],
                           key=lambda x: schedule[x]["scheduled_start"].value)
                runs = []
                for x in L:
                    if runs and _it(runs[-1][-1]) == _it(x):
                        runs[-1].append(x)
                    else:
                        runs.append([x])
                seen, hit = {}, None
                for i, r in enumerate(runs):
                    key = (_it(r[0]), (schedule[r[0]]["scheduled_start"].value - _H7) // _D)
                    if key in seen:
                        hit = (seen[key], i)
                        break
                    seen[key] = i
                if hit is None:
                    break
                i0, i1 = hit
                r1 = runs[i1]
                whole = ((i1 == 0 or _w(runs[i1 - 1][-1]) != _w(r1[0])) and
                         (i1 + 1 >= len(runs) or _w(runs[i1 + 1][0]) != _w(r1[0])))
                if not whole:
                    break                                  # moving it would leave part of a spool behind
                order = [x for x in L if x not in set(r1)]
                k = order.index(runs[i0][-1]) + 1
                order[k:k] = r1
                start_i = order.index(runs[i0][-1])
                t = int(schedule[order[start_i]]["finish_ns"])
                last = _it(order[start_i])
                lsw, lsf = None, None
                for x in order[:start_i + 1]:
                    if _CRL["cls"](_it(x)) == "steel":
                        lsw, lsf = _w(x), int(schedule[x]["finish_ns"])
                ok = True
                plan = {}
                for x in order[start_i + 1:]:
                    s = t + int(_co_min_v18(m, last, _it(x)) * 60 * NS)
                    s = max(s, int(_material_ready_ns(x)) if x in r1 else schedule[x]["scheduled_start"].value)
                    if _CRL["cls"](_it(x)) == "steel" and lsw is not None and _w(x) != lsw:
                        s = max(s, lsf + _CRL["setup"])
                    for _a, _b in sorted(DOWNTIME_WIN.get(m, ())):
                        if s < _b and s + _du(x) > _a:
                            s = _b
                    plan[x] = s
                    t, last = s + _du(x), _it(x)
                    if _CRL["cls"](_it(x)) == "steel":
                        lsw, lsf = _w(x), t
                if any(((plan[x] - _H7) // _D) != ((plan[r1[0]] - _H7) // _D) for x in r1[:1]) or \
                        ((plan[r1[0]] - _H7) // _D) != ((schedule[runs[i0][0]]["scheduled_start"].value - _H7) // _D):
                    ok = False                             # joined run would not land in that plant day
                if not ok:
                    break
                for x, s in plan.items():
                    d_ = _du(x)
                    schedule[x]["scheduled_start"] = pd.Timestamp(int(s))
                    schedule[x]["scheduled_finish"] = pd.Timestamp(int(s + d_))
                    schedule[x]["finish_ns"] = int(s + d_)
                n_mv += 1
            if n_mv:
                print(f"  [4RC-7 v39] {n_mv} second run(s) of an item in a plant day (carried over 07:00 by a repair push) "
                      f"joined to the first run; whole spools and one creel kept")
            return n_mv

        def _queue_unblock(machines):
            """v49 (user 2026-10-08): no lot waits behind a lot whose input is missing. On each listed machine (901 and
            the belt cutters) the lots are re-laid in their CURRENT order (so the planned sequence -- wire groups, day
            plan -- is kept); only when the next lot's input is not ready within min_wait_min does the machine run the
            next READY lot in that order instead. The same item continues first (one go); on 901 a steel lot of a new
            wire waits for the creel (240 min after the last steel lot) and a ready fabric / same-wire lot fills the
            reload. Changeovers, PM windows, input readiness and the max-aging floor are kept. Returns #lots moved."""
            _QU = cfg.get("queue_unblock") or {}
            _CRL = _CREEL_REF.get("c") or _CREEL
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _st = lambda x: int(schedule[x]["scheduled_start"].value)
            tol = int(float(_QU.get("min_wait_min", 30)) * 60 * NS)
            n_mv = 0
            for m in machines:
                L = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m and not str(x).startswith("BLD")
                            and str(r.get("department", "")).upper() not in ("BUILDING", "CURING")], key=_st)
                if len(L) < 2:
                    continue
                old = {x: _st(x) for x in L}
                du = {x: int(schedule[x]["finish_ns"]) - old[x] for x in L}
                rdy = {}
                for x in L:
                    r_ = max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))
                    if x in _AGE_FLOOR:
                        r_ = max(r_, int(_AGE_FLOOR[x]) - du[x])
                    rdy[x] = r_
                is4 = bool(_CRL) and m == _CRL["m"]
                setup = _CRL["setup"] if is4 else 0
                rem = list(L)
                t = min(old.values())
                last, lsw, lsf, n_skip = None, None, None, 0
                for _g in range(20 * len(L) + 100):
                    if not rem:
                        break
                    ready = [x for x in rem if rdy[x] <= t]
                    head = rem[0]
                    if head not in ready and rdy[head] - t <= tol:
                        t = rdy[head]                       # short wait: keep the planned order
                        continue
                    if not ready:
                        t = min(rdy[x] for x in rem)
                        continue
                    x = head if head in ready else ready[0]
                    if last is not None and _it(x) != _it(last):
                        same = [y for y in ready if _it(y) == _it(last)]
                        if same:
                            x = same[0]                     # one go: the same item continues
                    if (is4 and _CRL["cls"](_it(x)) == "steel" and lsw is not None and _w(x) != lsw
                            and t < lsf + setup):
                        fill = [y for y in ready if _CRL["cls"](_it(y)) != "steel" or _w(y) == lsw]
                        if fill:
                            x = fill[0]                     # creel reload window: fabric / same wire runs
                    s0 = t
                    if last is not None and _it(last) != _it(x):
                        s0 += int(_co_min_v18(m, _it(last), _it(x)) * 60 * NS)
                    if is4 and _CRL["cls"](_it(x)) == "steel" and lsw is not None and _w(x) != lsw:
                        s0 = max(s0, lsf + setup)
                    s0 = max(s0, rdy[x])
                    for _a, _b in sorted(DOWNTIME_WIN.get(m, ())):
                        if s0 < _b and s0 + du[x] > _a:
                            s0 = _b
                    n_skip += int(x != head)
                    schedule[x]["scheduled_start"] = pd.Timestamp(int(s0))
                    schedule[x]["scheduled_finish"] = pd.Timestamp(int(s0 + du[x]))
                    schedule[x]["finish_ns"] = int(s0 + du[x])
                    n_mv += int(abs(s0 - old[x]) > 60 * NS)
                    t, last = s0 + du[x], x
                    if is4 and _CRL["cls"](_it(x)) == "steel":
                        lsw, lsf = _w(x), t
                    rem.remove(x)
                print(f"  [QUEUE UNBLOCK v49] {m}: {len(L)} lots in their planned order; {n_skip} times a READY lot ran "
                      f"ahead of a lot whose input was not there (no waiting behind it)")
            return n_mv

        def _fourrc_replan():
            """v45: 901 re-planned AFTER the final repair on the real input-ready times -- list scheduling: whenever
            the calender is free it runs the best READY lot (same item, then same CTP_Master_FRC wire, fabric while a
            creel reload is pending, then earliest need), so one lot waiting for compound no longer delays every lot
            behind it. Changeovers from CTP_Master_ChangeoverMatrix, one creel by FRC wire (240 min), PM windows;
            a plant day is filled up to the level band's upper limit unless a lot is due that day. Returns #moved."""
            _CRL = _CREEL_REF.get("c") or _CREEL
            if not _CRL:
                return 0
            m = _CRL["m"]
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _cls = lambda l: _CRL["cls"](_it(l))
            _du = lambda x: int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _day = lambda t: (int(t) - _H7) // _D
            setup = _CRL["setup"]
            L = [x for x, r in schedule.items() if str(r.get("machine")) == m and not str(x).startswith("BLD")]
            if not L:
                return 0
            old = {x: schedule[x]["scheduled_start"].value for x in L}
            du = {x: _du(x) for x in L}
            rdy = {}
            for x in L:
                r_ = max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))
                if x in _AGE_FLOOR:
                    r_ = max(r_, int(_AGE_FLOOR[x]) - du[x])
                rdy[x] = r_
            need = {}
            for x in L:
                _fn = lot_first_need.get(x)
                need[x] = pd.Timestamp(_fn).value if (_fn is not None and not pd.isna(_fn)) else old[x]
            hi = _LEVEL_BAND.get("hi")
            qty = {x: float(lot_qty.get(x, 0.0) or 0.0) for x in L}
            load = defaultdict(float)
            rem = set(L)
            t = min(old.values())
            last, lsw, lsf, n_mv = None, None, None, 0
            for _g in range(10 * len(L) + 100):
                if not rem:
                    break
                ready = [x for x in rem if rdy[x] <= t]
                if not ready:
                    t = min(rdy[x] for x in rem)
                    continue
                d = _day(t)
                if hi:
                    ok = [x for x in ready if load[d] + qty[x] <= hi + 1.0 or need[x] < (d + 1) * _D + _H7]
                    if not ok:
                        t = (d + 1) * _D + _H7
                        continue
                    ready = ok
                cw = _w(last) if last else None
                reload_pending = (lsf is not None and t < lsf + setup)

                def _key(x):
                    same = 0 if (last and _it(x) == _it(last)) else (1 if (cw and _w(x) == cw) else 2)
                    steel_wait = int(_cls(x) == "steel" and lsw is not None and _w(x) != lsw and reload_pending)
                    return (same, steel_wait, need[x], old[x])
                x = min(ready, key=_key)
                s0 = t + (int(_co_min_v18(m, _it(last), _it(x)) * 60 * NS) if last and _it(last) != _it(x) else 0)
                if _cls(x) == "steel" and lsw is not None and _w(x) != lsw:
                    s0 = max(s0, lsf + setup)
                s0 = max(s0, rdy[x])
                for _a, _b in sorted(DOWNTIME_WIN.get(m, ())):
                    if s0 < _b and s0 + du[x] > _a:
                        s0 = _b
                schedule[x]["scheduled_start"] = pd.Timestamp(int(s0))
                schedule[x]["scheduled_finish"] = pd.Timestamp(int(s0 + du[x]))
                schedule[x]["finish_ns"] = int(s0 + du[x])
                n_mv += int(abs(s0 - old[x]) > 60 * NS)
                load[_day(s0)] += qty[x]
                t, last = s0 + du[x], x
                if _cls(x) == "steel":
                    lsw, lsf = _w(x), t
                rem.discard(x)
            _pe = os.environ.get("CTP_PLAN_END")
            _dl = _day(pd.Timestamp(_pe).value) if _pe else max(load) + 1
            print(f"  [4RC RE-PLAN v45] {m}: {n_mv} of {len(L)} lots re-timed on real input-ready times (ready lots go first, "
                  f"no queue blocking); km/day " + ", ".join(f"{pd.Timestamp(int(dd * _D + _H7)):%d-%b} {v / 1000:.1f}"
                                                             for dd, v in sorted(load.items()) if dd < _dl))
            return n_mv

        def _fourrc_load_compact():
            """v60 (user 2026-10-09): no idle INSIDE a loaded dip / spool load on 901.
            (1) PULL: inside a same-wire load the next lot starts right after the previous one (matrix changeover) when its
                inputs are ready, it stays within max aging - safety_h at every consumer, PM is kept and no item gets a
                second run on a plant day -- lots only move EARLIER, order / quantities / wire unchanged.
            (2) FILL: a gap left inside a load (always at a dip / spool boundary) takes a WHOLE fabric dip load from later
                (that bridges no cord change, inputs ready, shelf life kept, not run that day) -- only when the two parts
                of the split load fall on different plant days (one go a day); the second part may start at the next
                07:00 only if no consumer is delayed and nothing behind it moves."""
            _LC = ((cfg.get("fourrc_rules") or {}).get("load_compact") or {})
            if not _LC.get("enabled", False):
                return 0
            _CRL = _CREEL_REF.get("c") or _CREEL
            if not _CRL:
                return 0
            m = _CRL["m"]
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _cls = lambda l: _CRL["cls"](_it(l))
            _st = lambda x: schedule[x]["scheduled_start"].value
            _fi = lambda x: int(schedule[x]["finish_ns"])
            _du = lambda x: _fi(x) - _st(x)
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _day = lambda t: (int(t) - _H7) // _D
            _co = lambda a, b: int(float(_co_min_v18(m, a, b)) * 60 * NS) if a and a != b else 0
            _saf = int(float(_LC.get("safety_h", 4)) * 3600 * NS)
            _pull = int(_LC.get("fill_pull_days", 2))
            _ming = int(float(_LC.get("min_gap_min", 30)) * 60 * NS)
            _D9g = dict(zip(_fl["item"].astype(str).str.strip(), pd.to_numeric(_fl["spool_dip_length_m"], errors="coerce")))

            def _cons(x):
                return [c for c in successors.get(x, ()) if c in schedule
                        and float(edge_qty.get((x, c), 1.0) or 0.0) > 0]

            def _shelf_floor(x):
                mx = int(_ages_ns(_it(x))[1])
                cs = [_st(c) for c in _cons(x)]
                return (max(cs) - mx + _saf - _du(x)) if (mx > 0 and cs) else NEG_INF_NS

            def _deadline(x):
                mn = int(_ages_ns(_it(x))[0])
                cs = [_st(c) for c in _cons(x)]
                return (min(cs) - mn) if cs else None

            def _down(s, d):
                for _a2, _b2 in sorted(DOWNTIME_WIN.get(m, ())):
                    if s < _b2 and s + d > _a2:
                        s = _b2
                return s

            def _seq():
                return sorted([x for x, r in schedule.items() if str(r.get("machine")) == m
                               and not str(x).startswith("BLD")], key=_st)

            def _splits(L, st):
                runs, seen, spl = [], set(), 0
                for x in L:
                    k = (_it(x), _day(st(x)))
                    if runs and runs[-1] == k:
                        continue
                    spl += k in seen
                    seen.add(k)
                    runs.append(k)
                return spl

            def _set(x, s):
                d_ = _du(x)
                schedule[x]["scheduled_start"] = pd.Timestamp(int(s))
                schedule[x]["scheduled_finish"] = pd.Timestamp(int(s + d_))
                schedule[x]["finish_ns"] = int(s + d_)

            # (1) PULL inside loads
            n_p, h_p = 0, 0.0
            _why = defaultdict(int)
            L = _seq()
            for i in range(1, len(L)):
                a, b = L[i - 1], L[i]
                if _w(a) != _w(b) or _st(b) - _fi(a) - _co(_it(a), _it(b)) < 60 * NS:
                    continue
                _rdy, _flr = int(_material_ready_ns(b)), _shelf_floor(b)
                s = max(_fi(a) + _co(_it(a), _it(b)), _rdy, _flr)
                s = _down(s, _du(b))
                if s >= _st(b) - 60 * NS:
                    _why["inputs not ready" if _rdy >= s else ("max aging at a consumer" if _flr >= s else "PM")] += 1
                    continue
                base = _splits(L, _st)
                old = _st(b)
                if _splits(L, lambda y: s if y == b else _st(y)) > base:
                    _why["one go a day"] += 1
                    continue
                _set(b, s)
                n_p += 1
                h_p += (old - s) / (3600.0 * NS)
            # (2) FILL the gaps left inside loads with a whole fabric load from later
            n_f, h_f, n_push = 0, 0.0, 0
            for _rep in range(100):
                L = _seq()
                pos = {x: i for i, x in enumerate(L)}
                loads, cur = [], []
                for x in L:
                    if cur and _w(cur[-1]) != _w(x):
                        loads.append(cur)
                        cur = []
                    cur.append(x)
                if cur:
                    loads.append(cur)
                load_of = {x: k for k, g in enumerate(loads) for x in g}
                done = False
                for i in range(1, len(L)):
                    a, b = L[i - 1], L[i]
                    if _w(a) != _w(b) or _st(b) - _fi(a) - _co(_it(a), _it(b)) < _ming:
                        continue
                    part2 = [x for x in loads[load_of[b]] if pos[x] >= pos[b]]
                    nxt = L[pos[part2[-1]] + 1] if pos[part2[-1]] + 1 < len(L) else None
                    # one go a day: part 1 and part 2 of the split load must be on different plant days
                    p1_days = {_day(_st(x)) for x in loads[load_of[a]] if pos[x] < pos[b] and _it(x) == _it(b)}
                    start2 = _st(b)
                    if _day(start2) in p1_days or any(_day(_st(x)) in p1_days for x in part2 if _it(x) in
                                                       {_it(y) for y in loads[load_of[a]] if pos[y] < pos[b]}):
                        start2 = (_day(_st(b)) + 1) * _D + _H7          # part 2 starts at the next 07:00
                        sh = start2 - _st(b)
                        ok2 = all(_deadline(x) is None or _fi(x) + sh <= _deadline(x) for x in part2)
                        if nxt is not None:
                            ok2 = ok2 and _fi(part2[-1]) + sh + _co(_it(part2[-1]), _it(nxt)) <= _st(nxt)
                        ok2 = ok2 and all(not (_st(x) + sh < _b2 and _fi(x) + sh > _a2)
                                          for x in part2 for _a2, _b2 in DOWNTIME_WIN.get(m, ()))
                        if not ok2:
                            continue
                    else:
                        sh = 0
                    ws, we = _fi(a), start2
                    d = _day(ws)
                    best = None
                    for g in loads:
                        if _cls(g[0]) == "steel" or pos[g[0]] <= pos[b]:
                            continue
                        dd = _day(_st(g[0]))
                        if not (d <= dd <= d + _pull):
                            continue
                        pi, ni = pos[g[0]] - 1, pos[g[-1]] + 1
                        if 0 <= pi and ni < len(L) and _cls(L[pi]) == "steel" and _cls(L[ni]) == "steel" \
                                and _w(L[pi]) != _w(L[ni]) and _CRL["chg"](_it(L[pi]), _it(L[ni])):
                            continue                             # it bridges a cord change -- keep it there
                        s, last, plan = ws, _it(a), {}
                        for x in g:
                            s = max(s + _co(last, _it(x)), int(_material_ready_ns(x)), _shelf_floor(x))
                            s = _down(s, _du(x))
                            plan[x] = s
                            s += _du(x)
                            last = _it(x)
                        if s + _co(last, _it(b)) > we:
                            continue
                        gs = set(g)
                        if any(_it(y) == _it(x) and _day(_st(y)) == _day(sx) for x, sx in plan.items()
                               for y in L if y not in gs):
                            continue                             # filler item already runs that day
                        key = (-sum(_du(x) for x in g), dd)
                        if best is None or key < best[0]:
                            best = (key, plan)
                    if best is None:
                        continue
                    _base = _splits(L, _st)
                    _sv = {x: _st(x) for x in list(part2) + list(best[1])}
                    if sh:
                        for x in part2:
                            _set(x, _st(x) + sh)
                        n_push += 1
                    for x, sx in best[1].items():
                        h_f += _du(x) / (3600.0 * NS)
                        _set(x, sx)
                    if _splits(_seq(), _st) > _base:             # safety: never more item-day splits
                        for x, s0 in _sv.items():
                            _set(x, s0)
                        if sh:
                            n_push -= 1
                        h_f -= sum(_du(x) for x in best[1]) / (3600.0 * NS)
                        continue
                    n_f += 1
                    done = True
                    break
                if not done:
                    break
            # (3) HOLD: idle left in the MIDDLE of a dip / spool (next lot's inputs not ready yet) -> the dip is started
            #     later so it runs in one go: the lots of that dip already made move later, right up to the waiting lot
            #     (order / quantities unchanged, never past the waiting lot, PM kept, no new item-day split)
            n_h, h_h = 0, 0.0
            for _rep in range(200):
                L = _seq()
                moved = False
                cum, prevw = 0.0, None
                cums = {}
                for x in L:
                    if _w(x) != prevw:
                        cum = 0.0
                    cum += float(lot_qty.get(x, 0.0) or 0.0)
                    cums[x] = cum
                    prevw = _w(x)
                for i in range(1, len(L)):
                    a, b = L[i - 1], L[i]
                    if _w(a) != _w(b):
                        continue
                    idle = _st(b) - _fi(a) - _co(_it(a), _it(b))
                    D_ = _D9g.get(_it(a))
                    if idle <= 5 * 60 * NS or not D_:
                        continue
                    r_ = cums[a] % D_
                    if r_ < 1.0 or D_ - r_ < 1.0:
                        continue                                 # at a dip / spool boundary: allowed (reported)
                    grp = [a]                                    # lots back to the start of this dip
                    j = i - 1
                    while j - 1 >= 0 and _w(L[j - 1]) == _w(a):
                        rr = cums[L[j - 1]] % D_
                        if rr < 1.0 or D_ - rr < 1.0:
                            break
                        grp.insert(0, L[j - 1])
                        j -= 1
                    sv = {x: _st(x) for x in grp}
                    t = _st(b) - _co(_it(a), _it(b))
                    ok = True
                    for x in reversed(grp):                      # lay the dip backwards from the waiting lot
                        s0 = t - _du(x)
                        if any(s0 < _b2 and t > _a2 for _a2, _b2 in DOWNTIME_WIN.get(m, ())):
                            ok = False
                            break
                        _set(x, s0)
                        k = grp.index(x)
                        t = s0 - (_co(_it(grp[k - 1]), _it(x)) if k > 0 else 0)
                    if ok and _splits(_seq(), _st) > _splits(L, lambda y: sv.get(y, _st(y))):
                        ok = False
                    if not ok:
                        for x, s0 in sv.items():
                            _set(x, s0)
                        _why["hold refused (PM / one go a day)"] += 1
                        continue
                    n_h += 1
                    h_h += idle / (3600.0 * NS)
                    moved = True
                    break
                if not moved:
                    break
            L = _seq()
            machine_busy[m] = sorted((int(_st(x)), int(_fi(x))) for x in L)
            print(f"  [4RC LOAD COMPACT v60] hold: {n_h} dip(s) / spool(s) started later so they run in one go "
                  f"({h_h:,.1f} h of mid-dip idle removed); not pulled / held: {dict(_why)}")
            print(f"  [4RC LOAD COMPACT v60] pull: {n_p} lots moved earlier inside their dip / spool load ({h_p:,.1f} lot-h); "
                  f"fill: {n_f} in-load gaps filled with a whole fabric dip load ({h_f:,.1f} h; {n_push} second parts "
                  f"started at the next 07:00 so each item stays one go a day)")
            return n_p + n_f + n_h

        def _fourrc_left_shift():
            """v97 (user 2026-10-13): 901 LEFT-SHIFT. After the last 4RC day-plan pass, every WHOLE wire load (consecutive
            lots of one CTP_Master_FRC wire, so dips / spools stay whole and no idle is opened inside them) moves EARLIER
            into the idle in front of it, laid back to back (matrix changeovers). Machine order is unchanged, so wire
            campaigns and fabric in the creel window stay as they are. A load moves only when every lot keeps: inputs ready
            (+ min aging / transfer), the previous lot + matrix changeover, the 240 min creel after the previous steel cord,
            no PM overlap, max aging - safety_h at every consumer, and no new item-day split (one go a day). Never later.
            The final repair then lets the consumers move earlier (floor_to_ready)."""
            _LS = ((cfg.get("fourrc_rules") or {}).get("left_shift") or {})
            if not _LS.get("enabled", False):
                return 0
            _CRL = _CREEL_REF.get("c") or _CREEL
            if not _CRL:
                return 0
            m = _CRL["m"]
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _steel = lambda l: _CRL["cls"](_it(l)) == "steel"
            _st = lambda x: schedule[x]["scheduled_start"].value
            _fi = lambda x: int(schedule[x]["finish_ns"])
            _du = lambda x: _fi(x) - _st(x)
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _day = lambda t: (int(t) - _H7) // _D
            _co = lambda a, b: int(float(_co_min_v18(m, a, b)) * 60 * NS) if a and a != b else 0
            _saf = int(float(_LS.get("safety_h", 4)) * 3600 * NS)
            _minh = int(float(_LS.get("min_gain_min", 10)) * 60 * NS)
            _wins = sorted(DOWNTIME_WIN.get(m, ()))

            def _shelf_floor(x):
                mx = int(_ages_ns(_it(x))[1])
                cs = [_st(c) for c in successors.get(x, ()) if c in schedule
                      and float(edge_qty.get((x, c), 1.0) or 0.0) > 0]
                return (max(cs) - mx + _saf - _du(x)) if (mx > 0 and cs) else NEG_INF_NS

            def _set(x, s):
                d_ = _du(x)
                schedule[x]["scheduled_start"] = pd.Timestamp(int(s))
                schedule[x]["scheduled_finish"] = pd.Timestamp(int(s + d_))
                schedule[x]["finish_ns"] = int(s + d_)

            def _splits(L):
                runs, seen, spl = [], set(), 0
                for x in L:
                    k = (_it(x), _day(_st(x)))
                    if runs and runs[-1] == k:
                        continue
                    spl += k in seen
                    seen.add(k)
                    runs.append(k)
                return spl

            def _repeg_901():
                """v98: 901 output re-allocated FEFO per item: the earliest-ready 901 lots feed the earliest-starting
                consumers, within shelf life first (finish + min aging <= consumer start <= finish + max aging), the rest
                (already over age before) from what is left. Every producer keeps its total, every consumer its need.
                Kept per item only when the over-age quantity does not rise."""
                _bi = defaultdict(list)
                for (_pp, _cc), _qq in list(edge_qty.items()):
                    if (_qq and _qq > 0 and _pp in schedule and _cc in schedule
                            and str(schedule[_pp].get("machine")) == m and not str(_cc).startswith("BLD")):
                        _bi[_it(_pp)].append((_pp, _cc, float(_qq)))
                n_i = n_e = 0
                q_ov0 = q_ov1 = 0.0
                for _item, _rows in _bi.items():
                    _pmin, _pmax = _ages_ns(_item)
                    _sup, _need = defaultdict(float), defaultdict(float)
                    for _pp, _cc, _qq in _rows:
                        _sup[_pp] += _qq
                        _need[_cc] += _qq

                    def _ov(rows):
                        return sum(q for p, c, q in rows if _pmax and _st(c) > _fi(p) + _pmax)
                    if len(_sup) < 2:
                        q_ov0 += _ov(_rows)
                        q_ov1 += _ov(_rows)
                        continue
                    _left = {p: q for p, q in _sup.items()}
                    _cons = sorted(_need, key=lambda c: (_st(c), str(c)))
                    _prod = sorted(_sup, key=lambda p: (_fi(p), str(p)))
                    _new = defaultdict(float)
                    _rem = {}
                    for c in _cons:
                        r = _need[c]
                        for p in _prod:
                            if r <= 1e-9:
                                break
                            if _left[p] <= 1e-9 or _fi(p) + _pmin > _st(c):
                                continue
                            if _pmax and _st(c) > _fi(p) + _pmax:
                                continue
                            t = min(r, _left[p])
                            _new[(p, c)] += t
                            _left[p] -= t
                            r -= t
                        _rem[c] = r
                    for c in _cons:                          # not coverable within shelf life / ready: any supply left
                        r = _rem[c]
                        for p in sorted(_prod, key=lambda p: (_fi(p) + _pmin > _st(c), -_fi(p))):
                            if r <= 1e-9:
                                break
                            if _left[p] <= 1e-9:
                                continue
                            t = min(r, _left[p])
                            _new[(p, c)] += t
                            _left[p] -= t
                            r -= t
                    rows1 = [(p, c, q) for (p, c), q in _new.items()]
                    o0, o1 = _ov(_rows), _ov(rows1)
                    # never a link that is not ready in time where there was none before
                    late0 = sum(q for p, c, q in _rows if _fi(p) + _pmin > _st(c))
                    late1 = sum(q for p, c, q in rows1 if _fi(p) + _pmin > _st(c))
                    if o1 > o0 + 1e-6 or late1 > late0 + 1e-6 or {(p, c) for p, c, _ in rows1} == {(p, c) for p, c, _ in _rows}:
                        q_ov0 += o0
                        q_ov1 += o0
                        continue
                    q_ov0 += o0
                    q_ov1 += o1
                    for _pp, _cc, _ in _rows:
                        edge_qty.pop((_pp, _cc), None)
                        predecessors[_cc] = [x for x in predecessors.get(_cc, []) if x != _pp]
                        successors[_pp] = [x for x in successors.get(_pp, []) if x != _cc]
                    for (_pp, _cc), _qq in _new.items():
                        edge_qty[(_pp, _cc)] = edge_qty.get((_pp, _cc), 0.0) + _qq
                        if _pp not in predecessors[_cc]:
                            predecessors[_cc].append(_pp)
                        if _cc not in successors[_pp]:
                            successors[_pp].append(_cc)
                    n_i += 1
                    n_e += len(_new)
                print(f"  [4RC RE-PEG v98] 901 output re-allocated FEFO within shelf life: {n_i} items, {n_e} links; "
                      f"quantity over max aging {q_ov0:,.0f} -> {q_ov1:,.0f} (totals per lot unchanged)")
                return n_i

            if _LS.get("repeg", False):
                _repeg_901()
            L = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m
                        and not str(x).startswith("BLD")], key=_st)
            loads, cur = [], []
            for x in L:
                if cur and _w(cur[-1]) != _w(x):
                    loads.append(cur)
                    cur = []
                cur.append(x)
            if cur:
                loads.append(cur)
            base = _splits(L)
            n_l, n_x, h_g = 0, 0, 0.0
            _why = defaultdict(int)
            prev = None                                     # last lot already fixed on 901
            last_steel = None                               # (item, finish) of the last steel lot
            for g in loads:
                # compact offsets inside the load (back to back, matrix changeovers)
                off, t, pv = {}, 0, None
                for x in g:
                    t += _co(_it(pv), _it(x)) if pv else 0
                    off[x] = t
                    t += _du(x)
                    pv = x
                T = NEG_INF_NS
                if prev is not None:
                    T = max(T, _fi(prev) + _co(_it(prev), _it(g[0])))
                if _steel(g[0]) and last_steel is not None and _CRL["chg"](last_steel[0], _it(g[0])):
                    T = max(T, last_steel[1] + _CRL["setup"])      # 4RC rule 5: one creel
                for x in g:
                    T = max(T, int(_material_ready_ns(x)) - off[x], _shelf_floor(x) - off[x])
                for _rep in range(50):                          # PM: no lot overlaps a downtime window
                    hit = False
                    for x in g:
                        for a2, b2 in _wins:
                            if T + off[x] < b2 and T + off[x] + _du(x) > a2:
                                T = b2 - off[x]
                                hit = True
                    if not hit:
                        break
                s0 = {x: _st(x) for x in g}
                moved = False
                if T < s0[g[0]] - _minh and all(T + off[x] <= s0[x] for x in g):
                    for x in g:
                        _set(x, T + off[x])
                    if _splits(sorted(L, key=_st)) > base:
                        # try again without leaving the first lot's plant day
                        T2 = max(T, _day(s0[g[0]]) * _D + _H7)
                        if T2 < s0[g[0]] - _minh and all(T2 + off[x] <= s0[x] for x in g) and \
                                not any(T2 + off[x] < b2 and T2 + off[x] + _du(x) > a2 for x in g for a2, b2 in _wins):
                            for x in g:
                                _set(x, T2 + off[x])
                            if _splits(sorted(L, key=_st)) > base:
                                for x in g:
                                    _set(x, s0[x])
                                _why["one go a day"] += 1
                            else:
                                moved = True
                        else:
                            for x in g:
                                _set(x, s0[x])
                            _why["one go a day"] += 1
                    else:
                        moved = True
                else:
                    _why["no earlier slot (inputs / creel / aging / PM / previous lot)"] += 1
                if moved:
                    n_l += 1
                    n_x += len(g)
                    h_g += sum(s0[x] - _st(x) for x in g) / (3600.0 * NS)
                prev = g[-1]
                for x in g:
                    if _steel(x):
                        last_steel = (_it(x), _fi(x))
            L = sorted(L, key=_st)
            machine_busy[m] = sorted((int(_st(x)), int(_fi(x))) for x in L)
            print(f"  [4RC LEFT-SHIFT v97] {n_l} wire loads / {n_x} lots moved earlier ({h_g:,.1f} lot-h earlier); "
                  f"order, creel, changeovers, PM, aging, one go a day kept (item-day splits {base} -> {_splits(L)}); "
                  f"not moved: {dict(_why)}")
            return n_l

        def _edd_resequence():
            """v106 (user 2026-10-14): EARLIEST-DUE-FIRST on listed component machines (1002 treads, 1001 sidewalls).
            v98: late tread / sidewall lots had their inputs ready and the machine was not full, but waited a median
            55 h / 77 h behind family grouping (BR-21 / BR-28, soft). The machine's lots are re-laid from its first
            start: at each point the READY lot with the earliest plan due date (phase-4 LFT) runs next; lots due
            within bucket_h of each other keep the family (BOM compound set) of the previous lot, then the least
            matrix changeover. Matrix changeovers, inputs ready (+ min aging), PM windows and the shelf-life floor
            are kept. The new order is taken only when the machine's total tardiness (h x qty) falls."""
            _ED = cfg.get("edd_resequence") or {}
            if not _ED.get("enabled", False):
                return 0
            _bk = int(float(_ED.get("bucket_h", 4)) * 3600 * NS)
            _ctype = {"MASTER COMPOUND", "FINAL COMPOUND"}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            n_m = 0
            for m in [str(x) for x in (_ED.get("machines") or [])]:
                L = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m
                            and not str(x).startswith("BLD")], key=lambda x: schedule[x]["scheduled_start"].value)
                if len(L) < 2:
                    continue
                st0 = {x: int(schedule[x]["scheduled_start"].value) for x in L}
                du = {x: int(schedule[x]["finish_ns"]) - st0[x] for x in L}
                due = {}
                for x in L:
                    t = lft_map.get(x)
                    due[x] = int(pd.Timestamp(t).value) if t is not None and not pd.isna(t) else int(schedule[x]["finish_ns"])
                rel = {}
                for x in L:
                    r_ = max(int(_material_ready_ns(x)), int(UPSTREAM_OPEN_NS))
                    if x in _AGE_FLOOR:
                        r_ = max(r_, int(_AGE_FLOOR[x]) - du[x])
                    rel[x] = r_
                fam = {x: tuple(sorted({_it(p) for p in predecessors.get(x, ())
                                        if str(lot_item_type.get(p, "")).strip().upper() in _ctype})) or (_it(x),)
                       for x in L}
                qty = {x: float(lot_qty.get(x, 1.0) or 1.0) for x in L}
                co = lambda a, b: int(float(_co_min_v18(m, _it(a), _it(b))) * 60 * NS) if (a and _it(a) != _it(b)) else 0

                def _dn(s, d):
                    for a2, b2 in sorted(DOWNTIME_WIN.get(m, ())):
                        if s < b2 and s + d > a2:
                            s = b2
                    return s

                def _tard(pos):
                    return sum(max(0, pos[x][1] - due[x]) / (3600.0 * NS) * qty[x] for x in L)
                cur = {x: (st0[x], int(schedule[x]["finish_ns"])) for x in L}
                left = set(L)
                t, prev, new = st0[L[0]], None, {}
                while left:
                    rdy = [x for x in left if rel[x] <= t]
                    if not rdy:
                        t = min(rel[x] for x in left)
                        continue
                    d0 = min(due[x] for x in rdy)
                    cand = [x for x in rdy if due[x] <= d0 + _bk]
                    x = min(cand, key=lambda y: (prev is not None and fam[y] != fam[prev], co(prev, y), due[y], str(y)))
                    s = _dn(max(t + co(prev, x), rel[x]), du[x])
                    new[x] = (s, s + du[x])
                    t, prev = s + du[x], x
                    left.discard(x)
                t_old, t_new = _tard(cur), _tard(new)
                if t_new < t_old * 0.98:
                    for x, (s, f) in new.items():
                        schedule[x]["scheduled_start"] = pd.Timestamp(int(s))
                        schedule[x]["scheduled_finish"] = pd.Timestamp(int(f))
                        schedule[x]["finish_ns"] = int(f)
                    machine_busy[m] = sorted(new.values())
                    n_m += 1
                print(f"  [EDD RESEQUENCE v106] {m}: {len(L)} lots, tardiness {t_old:,.0f} -> {t_new:,.0f} qty-h "
                      f"({'taken' if t_new < t_old * 0.98 else 'kept the current order'})")
            return n_m

        def _fourrc_creel_fill():
            """v58 (user 2026-10-09): 901 creel-reload windows filled with fabric. A steel cord change (new CTP_Master_FRC
            wire) leaves the calender idle for the 240 min reload. The window gets a WHOLE fabric dip load (all
            consecutive lots of one FRC wire, so dips / spools stay whole and the load keeps its matrix order) that today
            bridges no cord change, from that plant day or up to pull_days later, when it fits in the window (the next
            steel lot is NOT delayed), its inputs are ready, it stays within shelf life up to its last consumer and no
            item gets a second run on its new plant day. Then (compact) the 901 lots behind move EARLIER into the freed
            time, same order, never later, creel / changeovers / PM / inputs / shelf life kept, one go a day kept."""
            _FC = ((cfg.get("fourrc_rules") or {}).get("creel_fill") or {})
            if not _FC.get("enabled", False):
                return 0
            _CRL = _CREEL_REF.get("c") or _CREEL
            if not _CRL:
                return 0
            m = _CRL["m"]
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _cls = lambda l: _CRL["cls"](_it(l))
            _st = lambda x: schedule[x]["scheduled_start"].value
            _du = lambda x: int(schedule[x]["finish_ns"]) - _st(x)
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _day = lambda t: (int(t) - _H7) // _D
            _co = lambda a, b: int(float(_co_min_v18(m, a, b)) * 60 * NS) if a and a != b else 0
            _pull = int(_FC.get("pull_days", 2))
            _minw = int(float(_FC.get("min_window_min", 30)) * 60 * NS)
            _succ = _SUCC_REF.get("s") or {}
            setup = _CRL["setup"]

            def _down(s, d):
                for _a2, _b2 in sorted(DOWNTIME_WIN.get(m, ())):
                    if s < _b2 and s + d > _a2:
                        s = _b2
                return s

            def _shelf_floor(x):
                """earliest start so x does not age out before its last consumer"""
                mx = int(_ages_ns(_it(x))[1])
                cs = [_st(c) for c in _succ.get(x, ()) if c in schedule]
                return (max(cs) - mx - _du(x)) if (mx > 0 and cs) else NEG_INF_NS

            def _seq():
                return sorted([x for x, r in schedule.items() if str(r.get("machine")) == m
                               and not str(x).startswith("BLD")], key=_st)

            def _loads(L):
                out, cur = [], []
                for x in L:
                    if cur and _w(cur[-1]) != _w(x):
                        out.append(cur)
                        cur = []
                    cur.append(x)
                if cur:
                    out.append(cur)
                return out

            def _is_change(a, b):
                return (_cls(a) == "steel" and _cls(b) == "steel" and _w(a) != _w(b) and _CRL["chg"](_it(a), _it(b)))

            def _splits(L, st):
                runs, seen, spl = [], set(), 0
                for x in L:
                    k = (_it(x), _day(st(x)))
                    if runs and runs[-1] == k:
                        continue
                    spl += k in seen
                    seen.add(k)
                    runs.append(k)
                return spl

            n_f, h_f = 0, 0.0
            _why = defaultdict(int)
            # v119 (user 2026-10-14): on the first lead_days plant day(s) EVERY idle gap of 901 (also the one before its
            # first lot) is a window, not only creel reloads: 901 sat idle on 12-Oct until 19:40 with fabric inputs ready
            # from 11:50 (plant: 901 never stops). Same guards: next lot never delayed, whole loads, one go a day, inputs
            # ready, shelf life. 0 = v118 (creel-reload windows only).
            _LEAD_END = int(UPSTREAM_OPEN_NS) + int(float(_FC.get("lead_days", 0) or 0) * _D)
            _LEAD_ON = _LEAD_END > int(UPSTREAM_OPEN_NS)
            for _rep in range(200):
                L = _seq()
                pos = {x: i for i, x in enumerate(L)}
                LD = _loads(L)
                done = False
                for i in range(-1 if _LEAD_ON else 0, len(L) - 1):
                    if i < 0:
                        a, b = None, L[0]
                        ws = int(UPSTREAM_OPEN_NS)
                    else:
                        a, b = L[i], L[i + 1]
                        ws = int(schedule[a]["finish_ns"])
                        if not _is_change(a, b) and not (_LEAD_ON and ws < _LEAD_END):
                            continue
                    we = _st(b)
                    if we - ws < _minw:
                        continue
                    d = _day(ws)
                    best = None
                    for g in LD:
                        if _cls(g[0]) == "steel" or pos[g[0]] <= i + 1:
                            continue                             # fabric loads later in the sequence only
                        dd = _day(_st(g[0]))
                        if not (d <= dd <= d + _pull):
                            continue
                        p_, n_ = pos[g[0]] - 1, pos[g[-1]] + 1
                        if 0 <= p_ and n_ < len(L) and _is_change(L[p_], L[n_]):
                            _why["bridges another change"] += 1
                            continue                             # it already bridges a cord change -- keep it there
                        _s0 = ws + sum(_du(x) for x in g)
                        if _s0 > we:
                            _why["longer than the window"] += 1
                            continue
                        if max(int(_material_ready_ns(x)) for x in g) > we:
                            _why["inputs not ready in the window"] += 1
                            continue
                        if max(_shelf_floor(x) for x in g) > we:
                            _why["would age out before its consumer"] += 1
                            continue
                        s = ws
                        last = _it(a) if a is not None else ""
                        plan, ok = {}, True
                        for x in g:
                            s = max(s + _co(last, _it(x)), int(_material_ready_ns(x)), _shelf_floor(x))
                            s = _down(s, _du(x))
                            plan[x] = s
                            s += _du(x)
                            last = _it(x)
                        if s + _co(last, _it(b)) > we:
                            _why["does not fit (changeovers / ready / PM)"] += 1
                            continue                             # must fit: the next steel lot keeps its start
                        g_set = set(g)
                        for x, sx in plan.items():               # one go a day on the new plant day
                            if any(_it(y) == _it(x) and _day(_st(y)) == _day(sx) for y in L if y not in g_set):
                                ok = False
                                break
                        if not ok:
                            _why["one go a day"] += 1
                            continue
                        key = (-sum(_du(x) for x in g), dd)      # fill most of the window, nearest day first
                        if best is None or key < best[0]:
                            best = (key, plan)
                    if best is not None:
                        for x, sx in best[1].items():
                            dx = _du(x)
                            h_f += dx / (3600.0 * NS)
                            schedule[x]["scheduled_start"] = pd.Timestamp(int(sx))
                            schedule[x]["scheduled_finish"] = pd.Timestamp(int(sx + dx))
                            schedule[x]["finish_ns"] = int(sx + dx)
                        n_f += 1
                        done = True
                        break
                if not done:
                    break
            # compaction: lots behind move earlier into the freed time (same order, never later)
            n_c, h_c = 0, 0.0
            if (n_f or _FC.get("compact_always", False)) and _FC.get("compact", True):   # v69 #1: pull 901 earlier
                L = _seq()
                base_spl = _splits(L, _st)
                for same_day in (False, True):
                    plan, t, last, lsw, lsf = {}, None, "", None, None
                    for x in L:
                        s0 = _st(x)
                        if t is None:
                            s = s0
                        else:
                            s = max(t + _co(last, _it(x)), int(_material_ready_ns(x)), _shelf_floor(x))
                            if _cls(x) == "steel" and lsw is not None and _w(x) != lsw and _CRL["chg"](lsw_it, _it(x)):
                                s = max(s, lsf + setup)
                            if same_day:
                                s = max(s, _day(s0) * _D + _H7)
                            s = _down(s, _du(x))
                            if s > s0:
                                s = s0
                        plan[x] = s
                        t, last = s + _du(x), _it(x)
                        if _cls(x) == "steel":
                            lsw, lsw_it, lsf = _w(x), _it(x), t
                    if _splits(L, lambda y: plan[y]) <= base_spl:
                        break
                    plan = None
                for x, s in (plan or {}).items():
                    if s < _st(x):
                        dx = _du(x)
                        n_c += 1
                        h_c += (_st(x) - s) / (3600.0 * NS)
                        schedule[x]["scheduled_start"] = pd.Timestamp(int(s))
                        schedule[x]["scheduled_finish"] = pd.Timestamp(int(s + dx))
                        schedule[x]["finish_ns"] = int(s + dx)
            L = _seq()
            machine_busy[m] = sorted((int(_st(x)), int(schedule[x]["finish_ns"])) for x in L)
            print(f"  [4RC CREEL FILL v58] {n_f} creel-reload window(s) filled with a whole fabric dip load ({h_f:,.1f} h of "
                  f"fabric run time into idle reload time; next steel lot never delayed; one go a day, whole dips, inputs "
                  f"ready, shelf life kept); compaction: {n_c} lots earlier ({h_c:,.1f} lot-h); candidates rejected: {dict(_why)}")
            return n_f

        def _fourrc_urgent_pull():
            """v88 (user 2026-10-13): 901 idle gaps held although lots were ready. A later 901 lot that its consumer is
            WAITING for (the consumer starts within slack_h of the lot being ready) moves into an idle gap of >= min_gap_min
            when its inputs are ready, it stays within shelf life up to its last consumer, it fits without delaying the next
            lot (matrix changeovers both sides, PM) and the creel is not disturbed (fabric, or steel on the wire already
            loaded). The ONE-GO-A-DAY order may be broken for such urgent lots only (user); every other 4RC rule kept.
            Most urgent first (earliest consumer). The final repair then re-times the consumers."""
            _UP = ((cfg.get("fourrc_rules") or {}).get("urgent_pull") or {})
            if not _UP.get("enabled", False):
                return 0
            _CRL = _CREEL_REF.get("c") or _CREEL
            if not _CRL:
                return 0
            m = _CRL["m"]
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _cls = lambda l: _CRL["cls"](_it(l))
            _st = lambda x: schedule[x]["scheduled_start"].value
            _du = lambda x: int(schedule[x]["finish_ns"]) - _st(x)
            _co = lambda a, b: int(float(_co_min_v18(m, a, b)) * 60 * NS) if a and a != b else 0
            _succ = _SUCC_REF.get("s") or {}
            _minw = int(float(_UP.get("min_gap_min", 60)) * 60 * NS)
            _slack = int(float(_UP.get("slack_h", 1.0)) * 3600 * NS)
            # v133 (user 2026-10-15): optional item filter + "late vs its own need" test (MONOCHAFER into idle 901 gaps
            # only; no other 901 lot moves -- the gap must hold it with both changeovers, the next lot keeps its start)
            _UPI = {str(v).strip() for v in (_UP.get("items") or [])}

            def _down(s, d):
                for _a2, _b2 in sorted(DOWNTIME_WIN.get(m, ())):
                    if s < _b2 and s + d > _a2:
                        s = _b2
                return s

            def _cons(x):
                return [_st(c) for c in _succ.get(x, ()) if c in schedule]

            def _shelf_floor(x):
                mx = int(_ages_ns(_it(x))[1])
                cs = _cons(x)
                return (max(cs) - mx - _du(x)) if (mx > 0 and cs) else NEG_INF_NS

            n_m, h_m, n_split = 0, 0.0, 0
            _why = defaultdict(int)
            for _rep in range(300):
                L = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m
                            and not str(x).startswith("BLD")], key=_st)
                pos = {x: i for i, x in enumerate(L)}
                best = None
                for i in range(len(L) - 1):
                    a, b = L[i], L[i + 1]
                    ws, we = int(schedule[a]["finish_ns"]), _st(b)
                    if we - ws < _minw:
                        continue
                    lsw = next((_w(y) for y in reversed(L[:i + 1]) if _cls(y) == "steel"), None)
                    for x in L[i + 2:]:
                        if _UPI and _it(x) not in _UPI:
                            continue                             # v133: only the listed items (e.g. MONOCHAFER)
                        cs = _cons(x)
                        if not cs:
                            continue
                        rdy_x = int(schedule[x]["finish_ns"]) + int(_ages_ns(_it(x))[0])
                        if _UP.get("late_vs_need", False):
                            _fn = lot_first_need.get(x)          # v133: moved when ready AFTER its own need time
                            if _fn is None or pd.isna(_fn) or rdy_x <= int(pd.Timestamp(_fn).value):
                                continue
                        elif min(cs) > rdy_x + _slack:
                            continue                             # its consumer is not waiting for it
                        if _cls(x) == "steel" and (_w(x) != lsw or _UP.get("fabric_only", False)):
                            _why["steel (creel / fabric_only)"] += 1
                            continue
                        s = max(ws + _co(_it(a), _it(x)), int(_material_ready_ns(x)), _shelf_floor(x))
                        s = _down(s, _du(x))
                        if s + _du(x) + _co(_it(x), _it(b)) > we:
                            _why["does not fit / inputs not ready"] += 1
                            continue
                        if s >= _st(x):
                            continue
                        if _UP.get("keep_one_go", False) and any(
                                _it(y) == _it(x) and y != x and ((int(_st(y)) - 7 * 3600 * NS) // (24 * 3600 * NS)) ==
                                ((int(s) - 7 * 3600 * NS) // (24 * 3600 * NS)) for y in L):
                            _why["one go a day (strict)"] += 1   # v133: never a 2nd run of the item on that plant day
                            continue
                        key = (min(cs), s)
                        if best is None or key < best[0]:
                            best = (key, x, s)
                if best is None:
                    break
                _, x, s = best
                dx = _du(x)
                h_m += (_st(x) - s) / (3600.0 * NS)
                if any(_it(y) == _it(x) and ((int(_st(y)) - 7 * 3600 * NS) // (24 * 3600 * NS)) ==
                       ((int(s) - 7 * 3600 * NS) // (24 * 3600 * NS)) for y in L if y != x):
                    n_split += 1
                schedule[x]["scheduled_start"] = pd.Timestamp(int(s))
                schedule[x]["scheduled_finish"] = pd.Timestamp(int(s + dx))
                schedule[x]["finish_ns"] = int(s + dx)
                n_m += 1
            L = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m
                        and not str(x).startswith("BLD")], key=_st)
            machine_busy[m] = sorted((int(_st(x)), int(schedule[x]["finish_ns"])) for x in L)
            print(f"  [4RC URGENT PULL v88] {n_m} waited-for 901 lot(s) moved into idle gaps ({h_m:,.1f} lot-h earlier; "
                  f"{n_split} of them joined a plant day that already runs the item -> one-go-a-day relaxed for urgent lots); "
                  f"inputs ready, shelf life, changeovers, PM, creel, next lot kept; rejected: {dict(_why)}")
            return n_m

        def _fourrc_fabric_bridge():
            """v40 user rule 3 on 901 after the repairs: least EFFECTIVE changeover -- a steel cord change costs the
            matrix minutes PLUS the idle one-creel reload (240 min), so steel -> fabric -> steel (fabric runs during
            the reload) is preferred. Every steel cord change with no fabric between gets that plant day's fabric run
            (a whole wire run, its inputs ready) moved into the reload window, when the day's effective changeover
            cost falls and rules 1 / 2 / 4 stay intact (one run per item per day, whole spools, routing run time)."""
            _CRL = _CREEL_REF.get("c") or _CREEL
            if not _CRL:
                return 0
            m = _CRL["m"]
            try:
                _fl = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wm = dict(zip(_fl["item"].astype(str).str.strip(), _fl["wire"].astype(str).str.strip()))
            except Exception:
                _wm = {}
            _it = lambda l: str(lot_item.get(l, "")).strip()
            _w = lambda l: _wm.get(_it(l), _it(l))
            _cls = lambda l: _CRL["cls"](_it(l))
            _du = lambda x: int(schedule[x]["finish_ns"]) - schedule[x]["scheduled_start"].value
            _H7, _D = 7 * 3600 * NS, 24 * 3600 * NS
            _day = lambda t: (int(t) - _H7) // _D
            setup = _CRL["setup"]

            def _lay(order, start_i):
                """sequential re-lay of order[start_i+1:] behind order[start_i] (changeovers, one creel, PM)."""
                t = int(schedule[order[start_i]]["finish_ns"])
                last = _it(order[start_i])
                lsw = lsf = None
                for x in order[:start_i + 1]:
                    if _cls(x) == "steel":
                        lsw, lsf = _w(x), int(schedule[x]["finish_ns"])
                plan = {}
                for x in order[start_i + 1:]:
                    s = max(t + int(_co_min_v18(m, last, _it(x)) * 60 * NS), int(_material_ready_ns(x)))
                    if _cls(x) == "steel" and lsw is not None and _w(x) != lsw:
                        s = max(s, lsf + setup)
                    for _a, _b in sorted(DOWNTIME_WIN.get(m, ())):
                        if s < _b and s + _du(x) > _a:
                            s = _b
                    plan[x] = s
                    t, last = s + _du(x), _it(x)
                    if _cls(x) == "steel":
                        lsw, lsf = _w(x), t
                return plan

            def _score(order, plan):
                """(unbridged cord changes, one-go splits, broken wire runs, effective changeover min)"""
                st = lambda x: plan.get(x, schedule[x]["scheduled_start"].value)
                unb, eff, prev = 0, 0.0, None
                for x in order:
                    if prev is not None and _it(prev) != _it(x):
                        c = float(_co_min_v18(m, _it(prev), _it(x)))
                        if _cls(prev) == "steel" and _cls(x) == "steel" and _w(prev) != _w(x) and _CRL["chg"](_it(prev), _it(x)):
                            unb += 1
                            c += setup / 60 / NS
                        eff += c
                    prev = x
                runs, seen, spl = [], set(), 0
                for x in order:
                    k = (_it(x), _day(st(x)))
                    if runs and runs[-1] == k:
                        continue
                    spl += k in seen
                    seen.add(k)
                    runs.append(k)
                return unb, spl, eff

            n_mv = 0
            for _rep in range(40):
                L = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m and not str(x).startswith("BLD")],
                           key=lambda x: schedule[x]["scheduled_start"].value)
                base = _score(L, {})
                gaps = [i for i in range(len(L) - 1) if _cls(L[i]) == "steel" and _cls(L[i + 1]) == "steel"
                        and _w(L[i]) != _w(L[i + 1])                # a real cord change (CTP_Master_FRC wire_type)
                        and _CRL["chg"](_it(L[i]), _it(L[i + 1]))]
                done = False
                for gi in gaps:
                    a, b = L[gi], L[gi + 1]
                    d = _day(schedule[b]["scheduled_start"].value)
                    # fabric wire runs (whole, so spools stay whole) of that plant day, not bridging another change
                    runs, cur = [], []
                    for x in L:
                        if cur and _w(cur[-1]) != _w(x):
                            runs.append(cur)
                            cur = []
                        cur.append(x)
                    if cur:
                        runs.append(cur)
                    _bd = int(((cfg.get("fourrc_rules") or {}).get("wire_campaign") or {}).get("bridge_days", 0))
                    _sc4 = _SUCC_REF.get("s") or {}

                    def _shelf_ok(run, start_at):
                        t_ = start_at
                        for x in run:
                            t_ += _du(x)
                            mx = int(_ages_ns(_it(x))[1])
                            cs = [schedule[c]["scheduled_start"].value for c in _sc4.get(x, ()) if c in schedule]
                            if mx > 0 and cs and t_ + mx < max(cs):
                                return False
                        return True
                    cand = [r for r in runs if _cls(r[0]) != "steel" and a not in r and b not in r
                            and d <= _day(schedule[r[0]]["scheduled_start"].value) <= d + _bd
                            and (_day(schedule[r[0]]["scheduled_start"].value) == d
                                 or _shelf_ok(r, int(schedule[a]["finish_ns"])))]
                    best = None
                    for r in cand:
                        order = [x for x in L if x not in set(r)]
                        k = order.index(a) + 1
                        order[k:k] = r
                        si = min(order.index(a), L.index(r[0]) - 1 if L.index(r[0]) > 0 else 0)
                        si = max(si, 0)
                        plan = _lay(order, si)
                        sc = _score(order, plan)
                        if sc[0] < base[0] and sc[1] <= base[1] and (best is None or sc < best[0]):
                            best = (sc, order, plan)
                    if best is not None:
                        for x, s in best[2].items():
                            d_ = _du(x)
                            schedule[x]["scheduled_start"] = pd.Timestamp(int(s))
                            schedule[x]["scheduled_finish"] = pd.Timestamp(int(s + d_))
                            schedule[x]["finish_ns"] = int(s + d_)
                        n_mv += 1
                        done = True
                        break
                if not done:
                    break
            Lf = sorted([x for x, r in schedule.items() if str(r.get("machine")) == m and not str(x).startswith("BLD")],
                        key=lambda x: schedule[x]["scheduled_start"].value)
            fin = _score(Lf, {})
            print(f"  [4RC rule 3 v40] {n_mv} fabric run(s) moved into a creel-reload window; steel cord changes with "
                  f"no fabric between: {fin[0]} left; effective changeover {fin[2]:,.0f} min (matrix + idle creel)")
            return n_mv

        def _co_min_v18(m_, a_, b_):
            _f = _CO_MIN_REF.get("f")
            if _f is not None:
                return _f(m_, a_, b_)[0]
            if not a_ or a_ == b_:
                return 0.0
            return float(compute_changeover_min(m_, "", a_, "", b_, "") or 0.0)

        # ══ 4RC RULE 5: ONE CREEL (2026-10-06, config fourrc_rules.creel) ══════════════════════════════
        # Steel cord is fed from ONE creel. When the next steel lot needs a different cord (matrix pair type
        # 'Steel to Steel Changeover'; 'Steel to steel compound change' = same cord, no reload) the creel is
        # reloaded for creel.setup_min (240) AFTER the last steel lot of the old cord ends. The reload runs in
        # PARALLEL: the calender runs fabric meanwhile (if released fabric lots exist) or waits. Only the
        # matrix changeovers are calender time; the 240 min are never added to it (no double counting).
        # Steel / fabric and the pair types come from the changeover matrix only.
        _FR = cfg.get("fourrc_rules") or {}
        _CR = _FR.get("creel") or {}
        _CREEL = None
        if _CR.get("enabled", False):
            import re as _re4
            _m4 = str(_FR.get("machine", "901"))
            _al = {str(k).upper(): str(v).upper() for k, v in (_FR.get("aliases") or {}).items()}
            _ovr = {str(k).upper(): str(v).lower() for k, v in (_FR.get("class_override") or {}).items()}

            def _n4(x):
                k = _re4.sub(r"[\s\-]", "", str(x).upper()).replace("MOTHERROLL", "")
                return _al.get(k, k)
            try:
                _mx4 = pd.read_csv(INPUTS / "changeover_matrix.csv", low_memory=False)
                _mx4 = _mx4[_mx4["machine"].astype(str).str.strip() == str(_FR.get("matrix_line", "4 RC Calendar"))]
                _pt4, _cl4 = {}, {}
                for _f4, _t4, _ty4 in zip(_mx4["from_MaterialCode_O"], _mx4["to_MaterialCode_O"],
                                          _mx4["changeover_type"].fillna("").astype(str)):
                    _pt4[(_n4(_f4), _n4(_t4))] = _ty4.strip().lower()
                    _g = _re4.match(r"(fabric|steel) to (fabric|steel)", _ty4.strip().lower())
                    if _g:
                        _cl4.setdefault(_n4(_f4), _g.group(1))
                        _cl4.setdefault(_n4(_t4), _g.group(2))
                _cl4.update(_ovr)
                _miss4 = defaultdict(int)
                # v42 (user): the creel / wire comes from JK_CTP_PPDS.CTP_Master_FRC -- same wire_type = same creel load
                # (no reload), different wire_type between steel items = reload; class from FRC item_type. The matrix
                # still gives every changeover's type / minutes (and decides only for items with no FRC wire).
                _wire4 = {}
                if str(_FR.get("creel_source", "matrix")).lower() == "frc_wire_type":
                    try:
                        _fl4 = pd.read_csv(INPUTS / "frc_lengths.csv")
                        for _i4, _w4, _c4 in zip(_fl4["item"], _fl4["wire"], _fl4["class"]):
                            if str(_w4).strip() and str(_w4) != "nan":
                                _wire4[_n4(_i4)] = str(_w4).strip()
                            if str(_c4).strip().lower() in ("steel", "fabric"):
                                _cl4[_n4(_i4)] = str(_c4).strip().lower()
                        _cl4.update(_ovr)
                    except Exception as _ew4:
                        print(f"  [4RC v42] !! frc_lengths.csv unreadable ({_ew4}) -- creel by matrix type")

                def _cls4(it):
                    return _cl4.get(_n4(it), "")

                def _chg4(a, b):
                    """True when going from steel cord a to steel cord b needs a creel reload."""
                    na, nb = _n4(a), _n4(b)
                    if na == nb:
                        return False
                    if na in _wire4 and nb in _wire4:          # v42: CTP_Master_FRC wire_type decides
                        return _wire4[na] != _wire4[nb]
                    ty = _pt4.get((na, nb)) or _pt4.get((nb, na))
                    if ty is None:
                        _miss4[(na, nb)] += 1
                        return True                       # not in the matrix: treat as a cord change
                    return "compound" not in ty
                _CREEL = {"m": _m4, "setup": int(float(_CR.get("setup_min", 240)) * 60 * NS),
                          "cls": _cls4, "chg": _chg4, "miss": _miss4}
            except Exception as _e4:
                print(f"  [4RC rule 5] !! changeover matrix unreadable ({_e4}) -- creel rule OFF")
        if _CREEL is not None:
            _m4 = _CREEL["m"]
            _co4 = lambda a, b: int(float(_co_min_v18(_m4, a, b)) * 60 * NS) if a else 0
            _pull4 = int(float(_CR.get("pull_early_h", 8)) * 3600 * NS)

            def _down4(t, d):
                for _a, _b in sorted(DOWNTIME_WIN.get(_m4, ())):
                    if t < _b and t + d > _a:
                        t = _b
                return t
            L4 = sorted([l for l, r in schedule.items() if str(r.get("machine")) == _m4
                         and not str(l).startswith("BLD")], key=lambda l: schedule[l]["scheduled_start"].value)
            if L4:
                it4 = {l: lot_item.get(l, "") for l in L4}
                du4 = {l: int(schedule[l]["finish_ns"]) - schedule[l]["scheduled_start"].value for l in L4}
                st4 = {l: schedule[l]["scheduled_start"].value for l in L4}
                cl4 = {l: _CREEL["cls"](it4[l]) for l in L4}

                def _rel4(l):
                    """earliest start: never earlier than now for steel; fabric may be pulled up to pull_early_h
                    when its inputs are ready (and not back into over-age)."""
                    if cl4[l] != "fabric":
                        return st4[l]
                    r = max(int(UPSTREAM_OPEN_NS), st4[l] - _pull4, int(_material_ready_ns(l)))
                    if l in _AGE_FLOOR:
                        r = max(r, min(st4[l], _AGE_FLOOR[l] - du4[l]))
                    return min(r, st4[l])
                pend = list(L4)
                t = st4[L4[0]]
                last, ls_it, ls_end = "", None, None
                stat = defaultdict(int)
                fill_min = idle_min = 0.0
                out4 = []

                def _place(l, t0):
                    nonlocal t, last, ls_it, ls_end
                    s0 = _down4(t0, du4[l])
                    out4.append((l, s0, s0 + du4[l]))
                    pend.remove(l)
                    t, last = s0 + du4[l], it4[l]
                    if cl4[l] == "steel":
                        ls_it, ls_end = it4[l], s0 + du4[l]
                while pend:
                    nxt = pend[0]                                         # keep the plan order
                    if cl4[nxt] == "steel" and ls_it is not None and _CREEL["chg"](ls_it, it4[nxt]):
                        # prefer a released lot on the SAME cord (no reload) that is due within 12 h
                        same = [l for l in pend[1:] if cl4[l] == "steel" and not _CREEL["chg"](ls_it, it4[l])
                                and st4[l] <= st4[nxt] + 12 * 3600 * NS and st4[l] <= max(t, st4[nxt])]
                        if same:
                            _place(same[0], max(t + _co4(last, it4[same[0]]), st4[same[0]]))
                            stat["same_cord_first"] += 1
                            continue
                        creel_ready = ls_end + _CREEL["setup"]
                        stat["creel_changes"] += 1
                        # fill the reload window with released fabric lots, in plan order
                        while True:
                            fab = [l for l in pend if cl4[l] == "fabric" and _rel4(l) <= creel_ready]
                            put = None
                            for f in fab:
                                s0 = max(t + _co4(last, it4[f]), _rel4(f))
                                if s0 + du4[f] + _co4(it4[f], it4[nxt]) <= creel_ready + 15 * 60 * NS:
                                    put = (f, s0)
                                    break
                            if put is None:
                                break
                            fill_min += du4[put[0]] / 60 / NS
                            _place(put[0], put[1])
                            stat["fabric_in_window"] += 1
                        s_cal = max(t + _co4(last, it4[nxt]), st4[nxt])
                        s_b = max(s_cal, creel_ready)
                        idle_min += max(0, creel_ready - s_cal) / 60 / NS
                        _place(nxt, s_b)
                        continue
                    _place(nxt, max(t + _co4(last, it4[nxt]), st4[nxt]))
                for l, s0, f0 in out4:
                    schedule[l]["scheduled_start"] = pd.Timestamp(int(s0))
                    schedule[l]["scheduled_finish"] = pd.Timestamp(int(f0))
                    schedule[l]["finish_ns"] = int(f0)
                _lots4 = {st4[l] for l in L4}
                machine_busy[_m4] = sorted([iv for iv in machine_busy.get(_m4, []) if iv[0] not in _lots4]
                                           + [(int(s0), int(f0)) for _, s0, f0 in out4])
                _CREEL_REF["c"] = _CREEL
                print(f"  [4RC rule 5] creel on {_m4}: {stat['creel_changes']} cord changes (240-min reload in "
                      f"parallel); {stat['fabric_in_window']} fabric lots ({fill_min:,.0f} min) run in reload "
                      f"windows; calender idle for the creel {idle_min:,.0f} min; {stat['same_cord_first']} same-cord "
                      f"lots taken first; unclassified items: {sorted({it4[l] for l in L4 if not cl4[l]})}; "
                      f"steel pairs not in the matrix: {len(_CREEL['miss'])}")

        # ══ 4RC DAY PLAN (v34, user 2026-10-06, config fourrc_rules.day_plan) ═════════════════════════════
        # (1) each item made in ONE GO per plant day (07:00 -> 07:00): all its lots of the day consecutive;
        # (3) least changeover: items on the same wire / dip code together (steel same cord 25 min, fabric 10),
        #     steel <-> fabric only between wire blocks; a cord change waits for the one-creel reload (240 min)
        #     which a fabric block fills; the day starts with the wire still loaded from the previous day;
        # (2) the spool / dip roll is ENTIRELY consumed before the cord / dip changes: at every wire change the
        #     last lot of the old wire is lengthened by the metres left on its spool (CTP_Master_FRC length; the
        #     extra goes to stock). A day takes the new order only if no lot finishes after its deadline
        #     (first consumer - min aging) that did not already, and its changeover minutes do not rise.
        # ══ v102 (user 2026-10-13): 4RC LOT SPLIT BY CONSUMER TIMING ═══════════════════════════════════
        # A late 901 lot whose consumers are spread so far apart that a lot made when its inputs are ready would be
        # past max aging (- safety_h) for its LAST consumers is held back for those last consumers -- and every early
        # consumer waits with it (v98: 79 % of the late 901 hours). Such a lot is split in two: part A (early
        # consumers + any surplus) is released at its inputs-ready time, part B (late consumers) keeps the original
        # slot. Inputs are shared pro rata, consumers re-linked whole, both parts >= min roll (CTP_Master_FRC) and
        # <= the original (<= one dip); the 4RC day plan below then places both under every 901 rule (one go a day,
        # wire blocks, creel, matrix changeovers), and each part gets its own shelf-life floor.
        _SPL = (cfg.get("fourrc_rules") or {}).get("split_by_consumer") or {}
        if _SPL.get("enabled", False) and _CREEL is not None:
            try:
                _m_s = _CREEL["m"]
                _fls = pd.read_csv(INPUTS / "frc_lengths.csv")
                _minr_s = dict(zip(_fls["item"].astype(str).str.strip(),
                                   pd.to_numeric(_fls["min_roll_m"], errors="coerce") if "min_roll_m" in _fls.columns
                                   else [300.0] * len(_fls)))
                _saf_s = int(float(_SPL.get("safety_h", 4)) * 3600 * NS)
                _marg_s = int(float(cfg.get("aging_pull_margin_h", 1.0)) * 3600 * NS)
                _rows_s, _ns_s, _why_s = [], 0, defaultdict(int)
                _L_s = sorted([l for l, r in schedule.items() if str(r.get("machine")) == _m_s
                               and not str(l).startswith("BLD")], key=lambda l: schedule[l]["scheduled_start"].value)
                for x in _L_s:
                    it_s = str(lot_item.get(x, "")).strip()
                    _mn_s, _mx_s = _ages_ns(it_s)
                    if _mx_s <= 0:
                        continue
                    cons = sorted([c for c in successors.get(x, ()) if c in schedule
                                   and float(edge_qty.get((x, c), 0.0) or 0.0) > 0],
                                  key=lambda c: schedule[c]["scheduled_start"].value)
                    if len(cons) < 2:
                        continue
                    st_s = int(schedule[x]["scheduled_start"].value)
                    fi_s = int(schedule[x]["finish_ns"])
                    du_s = fi_s - st_s
                    if fi_s + _mn_s <= schedule[cons[0]]["scheduled_start"].value:
                        continue                                       # not late for its first consumer
                    rdy_s = max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))
                    lim = rdy_s + du_s + _mx_s - _saf_s                # a lot made at ready serves consumers up to here
                    A = [c for c in cons if schedule[c]["scheduled_start"].value <= lim]
                    B = [c for c in cons if c not in A]
                    if not A or not B:
                        _why_s["no early / late consumer group"] += 1
                        continue
                    q = float(lot_qty.get(x, 0.0) or 0.0)
                    qB = sum(float(edge_qty[(x, c)]) for c in B)
                    qA = q - qB                                        # early consumers + any surplus / stock
                    mr = float(_minr_s.get(it_s) or 300.0)
                    if mr != mr:
                        mr = 300.0
                    if q <= 0 or qA < mr or qB < mr:
                        _why_s["a part below the min roll"] += 1
                        continue
                    _k = 1
                    while f"{x}_S{_k}" in schedule:
                        _k += 1
                    y = f"{x}_S{_k}"
                    duB = int(du_s * qB / q)
                    duA = du_s - duB
                    # dictionaries of the new lot (as the butyl prep lots)
                    lot_item[y] = lot_item.get(x)
                    lot_item_type[y] = lot_item_type.get(x, "")
                    lot_op[y], lot_dept[y], lot_eq[y] = lot_op.get(x), lot_dept.get(x), lot_eq.get(x)
                    lot_machines[y] = lot_machines.get(x)
                    lot_first_need[y], lot_last_need[y] = lot_first_need.get(x), lot_last_need.get(x)
                    duration_h[x] = duA / (3600.0 * NS)
                    duration_h[y] = duB / (3600.0 * NS)
                    lot_qty[x], lot_qty[y] = qA, qB
                    # B keeps the original slot (tail), A is released at its inputs-ready time
                    schedule[y] = dict(schedule[x])
                    schedule[y].update(lot_id=y, scheduled_start=pd.Timestamp(int(st_s + duA)),
                                       scheduled_finish=pd.Timestamp(int(fi_s)), finish_ns=int(fi_s),
                                       duration_h=duB / (3600.0 * NS))
                    a0 = max(rdy_s, max(schedule[c]["scheduled_start"].value for c in A) - _mx_s + _marg_s - duA)
                    a0 = min(a0, st_s)
                    schedule[x].update(scheduled_start=pd.Timestamp(int(a0)), scheduled_finish=pd.Timestamp(int(a0 + duA)),
                                       finish_ns=int(a0 + duA), duration_h=duA / (3600.0 * NS))
                    # inputs shared pro rata
                    for p in list(predecessors.get(x, ())):
                        e = float(edge_qty.get((p, x), 0.0) or 0.0)
                        if e > 0:
                            edge_qty[(p, x)] = e * qA / q
                            edge_qty[(p, y)] = e * qB / q
                        if y not in successors[p]:
                            successors[p].append(y)
                        if p not in predecessors[y]:
                            predecessors[y].append(p)
                    for (cl_, ip_) in [k_ for k_ in list(open_alloc) if k_[0] == x]:
                        cv = float(open_alloc[(cl_, ip_)] or 0.0)
                        open_alloc[(x, ip_)] = cv * qA / q
                        open_alloc[(y, ip_)] = cv * qB / q
                        if (x, ip_) in _OPEN_NETTED:
                            _OPEN_NETTED.add((y, ip_))
                    # late consumers re-linked whole to B
                    for c in B:
                        edge_qty[(y, c)] = edge_qty.pop((x, c))
                        successors[x] = [z for z in successors[x] if z != c]
                        successors[y].append(c)
                        predecessors[c] = list(dict.fromkeys(y if z == x else z for z in predecessors[c]))
                    _AGE_FLOOR[x] = max(schedule[c]["scheduled_start"].value for c in A) - _mx_s + _marg_s
                    _AGE_FLOOR[y] = max(schedule[c]["scheduled_start"].value for c in B) - _mx_s + _marg_s
                    _tr = lots[lots["lot_id"] == x]
                    if not _tr.empty:
                        _r = _tr.iloc[0].to_dict()
                        _r.update(lot_id=y, lot_qty=qB, raw_demand=qB, surplus=0.0, batch_id=y,
                                  duration_hours=duB / (3600.0 * NS), duration_min=duB / (60.0 * NS),
                                  duration_h=duB / (3600.0 * NS))
                        _rows_s.append(_r)
                        _ix = lots.index[lots["lot_id"] == x]
                        lots.loc[_ix, "lot_qty"] = qA
                        for _c_ in ("duration_hours", "duration_h"):
                            if _c_ in lots.columns:
                                lots.loc[_ix, _c_] = duA / (3600.0 * NS)
                    _ns_s += 1
                if _rows_s:
                    lots = pd.concat([lots, pd.DataFrame(_rows_s).reindex(columns=lots.columns)], ignore_index=True)
                machine_busy[_m_s] = sorted((int(r["scheduled_start"].value), int(r["finish_ns"]))
                                            for l, r in schedule.items() if str(r.get("machine")) == _m_s
                                            and not str(l).startswith("BLD"))
                print(f"  [4RC LOT SPLIT v102] {_ns_s} late 901 lots split by consumer timing (early part released at "
                      f"inputs-ready, late part keeps its slot; min roll / dip / max aging kept); not split: {dict(_why_s)}")
            except Exception as _e_s:
                import traceback as _tb_s
                _tb_s.print_exc()
                print(f"  [4RC LOT SPLIT v102] !! failed ({type(_e_s).__name__}: {_e_s}) -- no split")

        _DP = (cfg.get("fourrc_rules") or {}).get("day_plan") or {}
        if _DP.get("enabled", False) and _CREEL is not None:
            try:
                _m9 = _CREEL["m"]
                _fl9 = pd.read_csv(INPUTS / "frc_lengths.csv")
                _wire9 = dict(zip(_fl9["item"].astype(str).str.strip(), _fl9["wire"].astype(str).str.strip()))
                _D9 = dict(zip(_fl9["item"].astype(str).str.strip(), pd.to_numeric(_fl9["spool_dip_length_m"], errors="coerce")))
                _H79, _DAY9 = 7 * 3600 * NS, 24 * 3600 * NS
                _pull9 = int(float(_DP.get("pull_h", 8)) * 3600 * NS)
                _setup9 = _CREEL["setup"]
                L9 = sorted([l for l, r in schedule.items() if str(r.get("machine")) == _m9
                             and not str(l).startswith("BLD")], key=lambda l: schedule[l]["scheduled_start"].value)
                st9 = {l: schedule[l]["scheduled_start"].value for l in L9}
                fi9 = {l: int(schedule[l]["finish_ns"]) for l in L9}
                du9 = {l: fi9[l] - st9[l] for l in L9}
                it9 = {l: str(lot_item.get(l, "")).strip() for l in L9}
                cl9 = {l: _CREEL["cls"](it9[l]) for l in L9}
                wk9 = {l: (_wire9.get(it9[l]) or it9[l]) for l in L9}
                _suc9 = defaultdict(list)
                _set9 = set(L9)
                for _c0, _ps0 in predecessors.items():
                    for _p0 in _ps0:
                        if _p0 in _set9:
                            _suc9[_p0].append(_c0)
                dl9, rel9 = {}, {}
                for l in L9:
                    _mn9 = int(_ages_ns(it9[l])[0])
                    _cs = [schedule[c]["scheduled_start"].value for c in _suc9.get(l, ()) if c in schedule]
                    dl9[l] = max(min(_cs) - _mn9, fi9[l]) if _cs else fi9[l] + 24 * 3600 * NS
                    r_ = max(int(UPSTREAM_OPEN_NS), st9[l] - _pull9, int(_material_ready_ns(l)))
                    if l in _AGE_FLOOR:
                        r_ = max(r_, _AGE_FLOOR[l] - du9[l])
                    rel9[l] = min(r_, st9[l])
                _co9 = lambda a, b: int(float(_co_min_v18(_m9, a, b)) * 60 * NS) if (a and a != b) else 0

                def _dn9(t, d):
                    for _a, _b in sorted(DOWNTIME_WIN.get(_m9, ())):
                        if t < _b and t + d > _a:
                            t = _b
                    return t

                def _sim9(order, t0, last0, lsw0, lsf0, keep=False):
                    out, t, last, lsw, lsf, co, miss = [], t0, last0, lsw0, lsf0, 0, 0
                    for l in order:
                        c = _co9(last, it9[l])
                        s = max(t + c, st9[l] if keep else rel9[l])
                        if cl9[l] == "steel" and lsw is not None and wk9[l] != lsw:
                            s = max(s, lsf + _setup9)                       # one creel: reload after the old cord
                        s = _dn9(s, du9[l])
                        f = s + du9[l]
                        out.append((l, s, f))
                        co += c
                        miss += int(f > dl9[l] + 60 * NS and fi9[l] <= dl9[l] + 60 * NS)
                        t, last = f, it9[l]
                        if cl9[l] == "steel":
                            lsw, lsf = wk9[l], f
                    return out, co, miss, t, last, lsw, lsf

                days9 = defaultdict(list)
                for l in L9:
                    days9[(st9[l] - _H79) // _DAY9].append(l)
                t9, last9, lsw9, lsf9 = st9[L9[0]] if L9 else 0, None, None, None
                newpos9, stat9 = {}, defaultdict(int)
                # v42 wire campaign (user): every CTP_Master_FRC wire_type is ONE creel load / dip roll -- the items that
                # share it run together, and a load may take in the next max_days days' lots of its wire when their
                # inputs are ready and every metre is used within the item's max aging (CTP_Master_Aging) before its
                # last consumer -> fewer creel loads / reloads, whole spools filled by real demand.
                _WC = _FR.get("wire_campaign") or {}
                _WC_ON = bool(_WC.get("enabled", False))
                _WC_D = int(_WC.get("max_days", 3))
                _WC_S = int(float(_WC.get("safety_h", 4)) * 3600 * NS)
                taken9 = set()

                def _shelf_ok9(l, made_by):
                    _mx = int(_ages_ns(it9[l])[1])
                    if _mx <= 0:
                        return True
                    _cs = [schedule[c]["scheduled_start"].value for c in _suc9.get(l, ()) if c in schedule]
                    _last = max(_cs) if _cs else st9[l]
                    return made_by + _mx - _WC_S >= _last

                def _best_order9(items, start_item):
                    """item order inside one wire load with the least CTP_Master_ChangeoverMatrix minutes."""
                    import itertools as _itt
                    if len(items) <= 1:
                        return list(items)
                    best = None
                    for perm in (_itt.permutations(items) if len(items) <= 6 else [tuple(items)]):
                        if start_item in items and perm[0] != start_item:
                            continue
                        c_ = sum(_co9(a_, b_) for a_, b_ in zip(perm, perm[1:]))
                        if best is None or c_ < best[0]:
                            best = (c_, list(perm))
                    return best[1] if best else list(items)
                days9 = {int(k_): v_ for k_, v_ in days9.items()}           # v42: plant-day keys as int (range())
                _WC_NJ0 = [str(p_) for p_ in (_WC.get("no_join_first_day") or [])]     # v90 (default empty = v87)
                _d0_9 = (int(SCHEDULE_OPEN_NS) - _H79) // _DAY9
                for d in sorted(days9):
                    pool = [x for x in days9[d] if x not in taken9]
                    if _WC_ON and pool:
                        _dend = (d + 1) * _DAY9 + _H79
                        _wd = {wk9[x] for x in pool}
                        for dd in range(d + 1, d + 1 + _WC_D):
                            for x in days9.get(dd, []):
                                if x in taken9 or wk9[x] not in _wd:
                                    continue
                                # v90 (user 2026-10-13): on the FIRST plant day, listed items (CAP 66 mother roll) take no
                                # next-day lots into their load -- a longer first dip waits for later compound and the
                                # 4RC-10 hold then starts the whole dip a day late (v88 / v89: cap strip late, PCR day 1 lost)
                                if d == _d0_9 and any(_re.search(p_, str(it9[x])) for p_ in _WC_NJ0):
                                    stat9["join_skipped_day0"] += 1
                                    continue
                                _rc = max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))
                                if x in _AGE_FLOOR:
                                    _rc = max(_rc, _AGE_FLOOR[x] - du9[x])
                                if _rc < _dend and _shelf_ok9(x, _dend + du9[x]):
                                    rel9[x] = min(rel9[x], _rc)
                                    pool.append(x)
                                    taken9.add(x)
                                    stat9["campaign_joined"] += 1
                                elif _rc >= _dend:
                                    stat9["join_inputs_not_ready"] += 1
                                else:
                                    stat9["join_shelf_life"] += 1
                    # v43 level loading (user): 901 output per plant day in the band [min_km, max_km] -- a light day pulls the
                    # next days' lots (inputs ready, every metre used within max aging - safety) until it reaches min_km,
                    # never above max_km; output counted as made (whole spools per wire). Work only moves EARLIER.
                    _LV = _FR.get("level_loading") or {}
                    if _LV.get("enabled", False) and pool:
                        _tmin, _tmax = float(_LV.get("min_km", 30)) * 1000, float(_LV.get("max_km", 35)) * 1000
                        _dend = (d + 1) * _DAY9 + _H79

                        def _est9(pl):
                            _bw = defaultdict(float)
                            for x in pl:
                                _bw[wk9[x]] += float(lot_qty.get(x, 0.0) or 0.0)
                            tot = 0.0
                            for w_, q_ in _bw.items():
                                D_ = next((_D9.get(it9[x]) for x in pl if wk9[x] == w_), None)
                                tot += (math.ceil((q_ - 1.0) / D_) * D_) if (D_ and D_ == D_ and D_ > 0) else q_
                            return tot
                        est = _est9(pool)
                        if est < _tmin:
                            cands = sorted([x for dd in range(d + 1, d + 1 + int(_LV.get("max_days", 3)))
                                            for x in days9.get(dd, []) if x not in taken9 and x not in pool],
                                           key=lambda x: (wk9[x] not in {wk9[y] for y in pool}, st9[x]))   # same wire first
                            for x in cands:
                                _rc = max(int(UPSTREAM_OPEN_NS), int(_material_ready_ns(x)))
                                if x in _AGE_FLOOR:
                                    _rc = max(_rc, _AGE_FLOOR[x] - du9[x])
                                if _rc + du9[x] > _dend or not _shelf_ok9(x, _dend + du9[x]):
                                    continue
                                new = _est9(pool + [x])
                                if new > _tmax:
                                    continue
                                rel9[x] = min(rel9[x], _rc)
                                pool.append(x)
                                taken9.add(x)
                                stat9["level_pulled"] += 1
                                est = new
                                if est >= _tmin:
                                    break
                        stat9["level_days_in_band"] += int(_tmin <= est <= _tmax)
                    taken9.update(pool)
                    if not pool:
                        continue
                    runs = defaultdict(list)                              # item -> its lots (one go)
                    for l in sorted(pool, key=lambda x: st9[x]):
                        runs[it9[l]].append(l)
                    wires = defaultdict(list)                             # wire -> items (first start order)
                    for itm in sorted(runs, key=lambda k: st9[runs[k][0]]):
                        wires[wk9[runs[itm][0]]].append(itm)
                    _NEED9 = bool(_DP.get("need_order", False))          # v98 (user 2026-10-13): order by need time
                    need_i = lambda i: min(dl9[x] for x in runs[i])      # earliest deadline (first consumer - min aging)
                    if _NEED9:
                        for w in list(wires):                            # items of a load: most urgent first (the item
                            wires[w] = sorted(wires[w], key=lambda i: (i != last9, need_i(i)))   # still loaded stays first)
                    elif str(_FR.get("creel_source", "")).lower() == "frc_wire_type":   # v42: cheapest order inside a load
                        for w in list(wires):
                            wires[w] = _best_order9(wires[w], last9 if (last9 in wires[w]) else None)
                    wclass = {w: cl9[runs[items[0]][0]] for w, items in wires.items()}
                    first_w = (lambda w: min(need_i(i) for i in wires[w])) if _NEED9 else \
                        (lambda w: min(st9[runs[i][0]] for i in wires[w]))
                    cur_w = wk9.get(next((x for x in L9 if it9[x] == last9), None), None) if last9 else None
                    steel = sorted([w for w in wires if wclass[w] == "steel"], key=lambda w: (w != lsw9, first_w(w)))
                    fab = sorted([w for w in wires if wclass[w] != "steel"], key=lambda w: (w != cur_w, first_w(w)))
                    blocks = []
                    if _NEED9 and (str(_DP.get("need_mode", "steel_first")).lower() == "urgent_first" or (str(_DP.get("need_mode", "steel_first")).lower() == "urgent_first_day1" and d <= _d0_9)):   # v100 every day; v101 urgent_first_day1 = first plant day only; steel_first = v98
                        # v98b (user 2026-10-13): block by block on the day's timeline -- among the blocks whose first lot
                        # is READY by now take the most urgent (the loaded wire first), else the one ready soonest; after a
                        # steel block whose next steel needs a cord change, a ready fabric block fills the creel reload.
                        rem = list(steel) + list(fab)
                        rel_w = lambda w: min(rel9[x] for i in wires[w] for x in runs[i])
                        dur_w = lambda w: sum(du9[x] for i in wires[w] for x in runs[i])
                        tt, lsw_, lsf_, lw_ = t9, lsw9, lsf9, cur_w
                        def _start_w(w, t_):
                            s_ = max(t_, rel_w(w))
                            if wclass[w] == "steel" and lsw_ is not None and w != lsw_:
                                s_ = max(s_, lsf_ + _setup9)              # one creel: reload after the old cord
                            return s_
                        while rem:
                            # v99b: the most URGENT block goes next; a gap before it (inputs not ready / creel reload)
                            # takes a READY block only if that one finishes before the urgent block can start
                            # (fabric preferred), so a ready block never delays an urgent one
                            wu = min(rem, key=lambda w: (w != lw_, first_w(w)))
                            su = _start_w(wu, tt)
                            w = wu
                            if su > tt:
                                fit = [x for x in rem if x != wu and wclass[x] != "steel" or
                                       (x != wu and (lsw_ is None or x == lsw_))]
                                fit = [x for x in fit if rel_w(x) <= tt and tt + dur_w(x) <= su]
                                if fit:
                                    w = min(fit, key=lambda x: (wclass[x] == "steel", first_w(x)))
                            s_ = _start_w(w, tt)
                            tt = s_ + dur_w(w)
                            if wclass[w] == "steel":
                                lsw_, lsf_ = w, tt
                            lw_ = w
                            blocks.append(w)
                            rem.remove(w)
                        steel, fab = [], []
                    if last9 is not None and cl9.get(next((x for x in L9 if it9[x] == last9), None)) != "steel" and fab:
                        blocks.append(fab.pop(0))                          # continue the running fabric first
                    while steel:
                        blocks.append(steel.pop(0))
                        if steel and fab:
                            blocks.append(fab.pop(0))                      # fabric fills the creel reload
                    blocks += fab
                    order = [l for w in blocks for itm in wires[w] for l in runs[itm]]
                    orig = sorted(pool, key=lambda x: st9[x])
                    so, coo, mo, eo, lo, lswo, lsfo = _sim9(orig, t9, last9, lsw9, lsf9, keep=True)
                    sn, con, mn_, en, ln_, lswn, lsfn = _sim9(order, t9, last9, lsw9, lsf9)
                    if _DP.get("mandatory", False) or (mn_ <= mo and con <= coo):   # v38: rules 1 / 3 always
                        out, t9, last9, lsw9, lsf9 = sn, en, ln_, lswn, lsfn
                        stat9["days_new"] += 1
                        stat9["co_saved"] += (coo - con) / 60 / NS
                    else:
                        out, t9, last9, lsw9, lsf9 = so, eo, lo, lswo, lsfo
                        stat9["days_kept"] += 1
                    for l, s0, f0 in out:
                        newpos9[l] = [s0, f0]
                # (1b) + (2) v38: rules 1 and 2 TOGETHER on the actual timeline.
                #   (2) every consecutive run of one wire / dip code is made in WHOLE spools of its Spool/Dip Length
                #       (CTP_Master_FRC, by item_code): total metres rounded up to k x D, the top-up spread over the
                #       run's lots from the last one back, each <= one spool (4RC-6 kept), extra to stock; 1 m tolerance.
                #   (1) lengthened lots push the sequence, so lots planned for the next day can start before 07:00 and
                #       form a second run of an item in a plant day: that run is moved right after the first one and
                #       the top-up is recomputed from the original quantities. Repeat (<= 40), keep the fewest splits.
                _tol9 = float(_DP.get("tolerance_m", 1.0))
                _du0 = dict(du9)

                def _wire_runs(order):
                    runs, cur = [], []
                    for l in order:
                        if cur and wk9[cur[-1]] != wk9[l]:
                            runs.append(cur)
                            cur = []
                        cur.append(l)
                    if cur:
                        runs.append(cur)
                    return runs

                _TOPUP_ON = bool(_DP.get("whole_spool_topup", True))   # v76 (user 2026-10-11): false = demand metres only

                def _topup(order):
                    ext, nq, du = [], {}, dict(_du0)
                    for run in (_wire_runs(order) if _TOPUP_ON else []):
                        D = _D9.get(it9[run[-1]])
                        if not (D and D == D and D > 0):
                            continue                           # no Spool/Dip Length in CTP_Master_FRC
                        qs = {l: float(lot_qty.get(l, 0.0) or 0.0) for l in run}
                        T = sum(qs.values())
                        k_ = max(1, math.ceil((T - _tol9) / D))
                        extra = k_ * D - T
                        if extra <= _tol9:
                            continue
                        for x in reversed(run):
                            add_m = extra if x == run[0] else min(extra, max(D - qs[x], 0.0))
                            if add_m <= _tol9 / 10:
                                continue
                            du[x] = _du0[x] + (int(_du0[x] * add_m / qs[x]) if qs[x] > 0 else 0)
                            nq[x] = qs[x] + add_m
                            ext.append({"lot_id": x, "item": it9[x], "wire": wk9[x], "spool_dip_length_m": D,
                                        "old_qty": round(qs[x], 1), "added_m": round(add_m, 1),
                                        "reason": "spool / dip entirely consumed before the change"})
                            extra -= add_m
                            if extra <= _tol9 / 10:
                                break
                    return ext, nq, du

                def _lay9(order):
                    ext, nq, du = _topup(order)
                    du9.clear()
                    du9.update(du)
                    sim = _sim9(order, st9[order[0]], None, None, None)[0]
                    runs, prev = [], None
                    for l, s0, f0 in sim:
                        key = (it9[l], (s0 - _H79) // _DAY9)
                        if prev is not None and prev[0] == key:
                            prev[1].append(l)
                        else:
                            prev = [key, [l]]
                            runs.append(prev)
                    first, xr = {}, []
                    for key, ls in runs:
                        if key in first:
                            xr.append((first[key], ls))
                        else:
                            first[key] = ls
                    return sim, xr, ext, nq, du

                ext9, _nq9, seq9 = [], {}, []
                _net9, _drop9, _orig9 = [], set(), {}
                if L9:
                    _ord = sorted(L9, key=lambda l: newpos9[l][0])
                    _best9 = None
                    for _it_ in range(40 if _DP.get("mandatory", False) else 1):
                        sim, xr, ext, nq, du = _lay9(_ord)
                        if _best9 is None or len(xr) < _best9[0]:
                            _best9 = (len(xr), list(_ord), sim, ext, nq, du)
                        if not xr:
                            break
                        f_run, l_run = xr[0]
                        _ls = set(l_run)
                        _ord = [x for x in _ord if x not in _ls]
                        k_ = _ord.index(f_run[-1]) + 1
                        _ord[k_:k_] = l_run
                    stat9["one_go_splits_left"], seq9, sim, ext9, _nq9, du_b = _best9
                    # v59 (user 2026-10-09): the whole-spool / dip top-up is STOCK, not waste. The metres a run gains
                    # to finish its spool (4RC rule: spool entirely consumed) cover the SAME item's later lots first
                    # (oldest stock first, at least min aging before and within max aging - safety_h of every
                    # consumer); a covered lot is cut (never below CTP_Master_FRC min roll) or dropped, its consumers
                    # draw from the earlier lot. One go a day, whole spools, creel, routing run time all kept; a change
                    # is accepted only when the item-day splits do not rise.
                    _NT = _FR.get("spool_netting") or {}
                    if _NT.get("enabled", False):
                        _min9 = dict(zip(_fl9["item"].astype(str).str.strip(),
                                         pd.to_numeric(_fl9["min_roll_m"], errors="coerce")))
                        _saf9 = int(float(_NT.get("safety_h", 4)) * 3600 * NS)
                        _ord = list(seq9)
                        _bad9 = set()
                        _used9 = defaultdict(float)                      # metres of a lot's top-up already given away

                        def _resid_bad9(order, ext):
                            """items whose total made metres end in a residual between 0 and the min roll (4RC-6)"""
                            mt = defaultdict(float)
                            for x in order:
                                mt[it9[x]] += float(lot_qty.get(x, 0.0) or 0.0)
                            for e in ext:
                                mt[e["item"]] += float(e["added_m"])
                            bad = set()
                            for itm, v in mt.items():
                                D_ = _D9.get(itm)
                                if D_ and D_ == D_ and D_ > 0:
                                    r_ = math.ceil((v - _tol9) / D_) * D_ - v
                                    if _tol9 < r_ < float(_min9.get(itm) or 300.0) - _tol9:
                                        bad.add(itm)
                            return bad
                        _rb9 = _resid_bad9(_ord, ext9)
                        for _rn in range(int(_NT.get("max_changes", 400))):
                            _pos = {l: (s0, f0) for l, s0, f0 in sim}
                            _addm = defaultdict(float)
                            for e in ext9:
                                _addm[e["lot_id"]] += e["added_m"]
                            _runid = {l: i for i, r in enumerate(_wire_runs(_ord)) for l in r}
                            stock = defaultdict(list)                    # item -> [source lot, made_ns] (oldest first)
                            hit = None
                            for l in _ord:
                                itm = it9[l]
                                if stock[itm] and l not in _bad9:
                                    _mn, _mx = int(_ages_ns(itm)[0]), int(_ages_ns(itm)[1])
                                    q = float(lot_qty.get(l, 0.0) or 0.0)
                                    cons = sorted([c for c in _suc9.get(l, ()) if c in schedule
                                                   and float(edge_qty.get((l, c), 0.0) or 0.0) > 0],
                                                  key=lambda c: schedule[c]["scheduled_start"].value)
                                    for src, made in stock[itm]:
                                        avail = _addm[src] - _used9[src]
                                        if avail <= _tol9 or _runid[src] == _runid[l] or not cons or q <= 0:
                                            continue
                                        take, got = [], 0.0
                                        for c in cons:                   # whole consumer needs, per consumer shelf life
                                            cs_ = schedule[c]["scheduled_start"].value
                                            eq = float(edge_qty.get((l, c), 0.0) or 0.0)
                                            if made + _mn <= cs_ and (_mx <= 0 or made + _mx - _saf9 >= cs_) \
                                                    and got + eq <= avail + _tol9:
                                                take.append(c)
                                                got += eq
                                        if got <= _tol9:
                                            continue
                                        mr = float(_min9.get(itm) or 300.0)
                                        new_q = 0.0 if len(take) == len(cons) else max(q - got, mr)
                                        if new_q >= q - _tol9:
                                            continue
                                        hit = (l, q, new_q, got, src, take)
                                        break
                                    if hit:
                                        break
                                if l in _addm and _addm[l] - _used9[l] > _tol9:
                                    stock[itm].append((l, _pos[l][1]))
                            if hit is None:
                                break
                            l, q, new_q, got, src, take = hit
                            _sv = (lot_qty.get(l), _du0[l])
                            lot_qty[l] = new_q
                            _du0[l] = int(_du0[l] * new_q / q) if new_q > 0 else 0
                            _o2 = [x for x in _ord if x != l] if new_q <= 0 else _ord
                            sim2, xr2, ext2, nq2, du2 = _lay9(_o2)
                            if len(xr2) > stat9["one_go_splits_left"] or not (_resid_bad9(_o2, ext2) <= _rb9):
                                lot_qty[l], _du0[l] = _sv
                                _bad9.add(l)
                                _lay9(_ord)
                                continue
                            _orig9.setdefault(l, q)
                            _used9[src] += got
                            for c in take:                               # these consumers now draw from the stock lot
                                eq = float(edge_qty.get((l, c), 0.0) or 0.0)
                                edge_qty[(src, c)] = float(edge_qty.get((src, c), 0.0) or 0.0) + eq
                                edge_qty.pop((l, c), None)
                                if l in predecessors[c]:
                                    predecessors[c].remove(l)
                                if src not in predecessors[c]:
                                    predecessors[c].append(src)
                                    successors[src].append(c)
                                    _suc9[src].append(c)
                                if c in _suc9.get(l, []):
                                    _suc9[l].remove(c)
                            _net9.append({"lot_id": l, "item": it9[l], "stock_from_lot": src, "metres_from_stock": round(got, 1),
                                          "consumers_moved": len(take), "old_qty": round(q, 1), "new_qty": round(new_q, 1),
                                          "action": "dropped (covered by spool stock)" if new_q <= 0 else "cut"})
                            if new_q <= 0:
                                _drop9.add(l)
                            _ord = _o2
                            sim, ext9, _nq9, du_b = sim2, ext2, nq2, du2
                            seq9 = _ord
                        print(f"  [4RC SPOOL STOCK v59] {_m9}: {len(_drop9)} lots dropped and "
                              f"{len({e['lot_id'] for e in _net9} - _drop9)} cut -- their need taken from the whole-spool "
                              f"top-up of an earlier run ({sum(e['metres_from_stock'] for e in _net9):,.0f} m); "
                              f"{len(_bad9)} not netted (would split an item-day or leave a residual below the min roll)")
                    du9.clear()
                    du9.update(du_b)
                    for l, s0, f0 in sim:
                        newpos9[l] = [s0, f0]
                for l in _drop9:                                  # v59: covered by spool stock -> not made
                    newpos9.pop(l, None)
                    schedule.pop(l, None)
                    lot_qty[l] = 0.0
                if _drop9:
                    lots = lots[~lots["lot_id"].isin(_drop9)].reset_index(drop=True)
                for l, (s0, f0) in newpos9.items():
                    schedule[l]["scheduled_start"] = pd.Timestamp(int(s0))
                    schedule[l]["scheduled_finish"] = pd.Timestamp(int(f0))
                    schedule[l]["finish_ns"] = int(f0)
                    schedule[l]["duration_h"] = (f0 - s0) / (3600.0 * NS)
                    duration_h[l] = (f0 - s0) / (3600.0 * NS)
                _old9 = {st9[l] for l in L9}
                for l, q0 in _orig9.items():                      # v59: cut lots -> made metres for the checks
                    if l not in _drop9:
                        ext9.append({"lot_id": l, "item": it9[l], "wire": wk9[l], "spool_dip_length_m": _D9.get(it9[l]),
                                     "old_qty": round(q0, 1), "added_m": round(float(lot_qty.get(l, 0.0)) - q0, 1),
                                     "reason": "cut: need covered by the spool stock of an earlier run"})
                if _net9:
                    pd.DataFrame(_net9).to_csv(OUTPUTS / "phase5_spool_netting.csv", index=False)
                machine_busy[_m9] = sorted([iv for iv in machine_busy.get(_m9, []) if iv[0] not in _old9]
                                           + [(int(a), int(b)) for a, b in newpos9.values()])
                lot_qty.update(_nq9)
                if ext9:
                    _ex = dict(_nq9)
                    lots.loc[lots["lot_id"].isin(_ex), "lot_qty"] = lots.loc[lots["lot_id"].isin(_ex), "lot_id"].map(_ex)
                pd.DataFrame(ext9).to_csv(OUTPUTS / "phase5_spool_consumed.csv", index=False)
                print(f"  [4RC LEVEL v43] {_m9}: {stat9['level_pulled']} lots pulled forward into light days; "
                      f"{stat9['level_days_in_band']} plant days planned inside the target band")
                print(f"  [4RC DAY PLAN] {_m9}: {stat9['campaign_joined']} lots joined a wire campaign from the next "
                      f"days (same CTP_Master_FRC wire, within shelf life); not joined: inputs not ready "
                      f"{stat9['join_inputs_not_ready']}, shelf life {stat9['join_shelf_life']}, "
                      f"first-day no-join (v90) {stat9['join_skipped_day0']}")
                print(f"  [4RC DAY PLAN] {_m9}: {stat9['days_new']} plant days in one-go / wire-block order "
                      f"({stat9['one_go_splits_left']} item-day splits left on the actual timeline), "
                      f"{stat9['days_kept']} kept ({stat9['co_saved']:,.0f} changeover min saved); {len(ext9)} lots "
                      f"lengthened so the spool / dip was entirely consumed (+{sum(e['added_m'] for e in ext9):,.0f} m)")
            except Exception as _e9:
                print(f"  [4RC DAY PLAN] !! failed ({type(_e9).__name__}: {_e9}) -- 901 unchanged")

        # ══ BR-21 QUINTOPLEX: SIMILAR TREADS TOGETHER (2026-10-06, config quintoplex_rules) ══════════════
        # Per plant day (07:00 -> 07:00) the PCR tread extruder runs its lots in the order with the LEAST
        # changeover: same die (same item, 0 min) first, then the cheapest matrix transition (A die only
        # 4.5, B same compound type 10, ... H 60), ties to the same main compound, then the same cushion
        # compound (B715 / B815), then the earliest deadline. "Where coverage permits": a lot may start no
        # earlier than its compounds are ready and not so early that its tread exceeds max aging before
        # its first build; it must finish before the first build that needs it (minus min aging), before
        # any other consumer, within max_delay_h, and before its compound inputs expire. A day takes the
        # new order only if every lot keeps its window, the day ends no later and changeover falls.
        # BR-25 (2026-10-06): the same pass runs on the TBR tread Duplex (config duplex_rules) with the
        # compound FAMILY first -- e.g. all T636 treads in one stretch, then all T639 -- then least changeover.
        _FB = cfg.get("family_batching") or {}
        _fb_rules = ([{"enabled": True, "rule": "BR-8", "label": f"family {_fm}", "machine": str(_fm),
                       "matrix_line": str(_fl.get("line", "") if isinstance(_fl, dict) else _fl),
                       "family_mode": (_fl.get("mode", "bom_set") if isinstance(_fl, dict) else "bom_set"),
                       "family_first": True, "look_ahead": True,
                       "max_delay_h": float(_FB.get("max_delay_h", 12)), "max_pull_h": float(_FB.get("max_pull_h", 12))}
                      for _fm, _fl in (_FB.get("machines") or {}).items()] if _FB.get("enabled", False) else [])
        # v70 (user 2026-10-10): PCR BEAD RULES BR-58..61 -- bead winding 1201 and bead apexing 201 / 202 run per plant
        # day grouped by SPOOL / FORMER INCH (from CTP_Master_ChangeoverMatrix 'PCR Bead Winding' types: '... change over
        # 14 to 16' etc.), inside each inch block the cheapest matrix order (former / spacer, then BCID / construction);
        # same window checks as the extruder passes (inputs ready, shelf life, before the first consumer, <= max_delay_h).
        _PBR = cfg.get("pcr_bead_rules") or {}
        if _PBR.get("enabled", False):
            _fb_rules += [{"enabled": True, "rule": "BR-61", "label": f"PCR bead {_bm}", "machine": str(_bm),
                           "matrix_line": str(_bl.get("line", "")), "family_mode": str(_bl.get("mode", "matrix_inch")),
                           "family_first": True, "look_ahead": True,
                           "max_delay_h": float(_PBR.get("max_delay_h", 12)), "max_pull_h": float(_PBR.get("max_pull_h", 12))}
                          for _bm, _bl in (_PBR.get("machines") or {}).items()]
            _BINCH = {}
            try:
                _mxb = pd.read_csv(INPUTS / "changeover_matrix.csv", low_memory=False)
                _mxb = _mxb[_mxb["machine"].astype(str).str.strip() == str(_PBR.get("winding_matrix_line", "PCR Bead Winding"))]
                for _a_, _b_, _t_ in zip(_mxb["from_MaterialCode_O"].astype(str).str.strip(),
                                         _mxb["to_MaterialCode_O"].astype(str).str.strip(),
                                         _mxb["changeover_type"].astype(str)):
                    _mt = re.search(r"(\d\d)\s*to\s*(\d\d)", _t_.replace("to", " to "))
                    if _mt:
                        _BINCH[_a_], _BINCH[_b_] = _mt.group(1), _mt.group(2)
                print(f"  [PCR BEAD v70] spool / former inch known for {len(_BINCH)} bead winding items "
                      f"(14 / 16 / 18 / 20 in) -- winding 1201 and apexing 201 / 202 grouped by inch per plant day")
            except Exception as _eb:
                print(f"  [PCR BEAD v70] !! matrix unreadable ({_eb}) -- grouping by inch off")
        # v70 (user 2026-10-10): CUTTER RULES BR-50 / BR-52 -- belt cutters 801 (PCR) / 802 (TBR): the belt layers of the
        # same building SKU (PCR Belt-1 + Belt-2; TBR B1..B4 + L / R STC) are one family per plant day, so the day's order
        # cuts a SKU's kit together, then the cheapest matrix order; same window checks as the extruder passes.
        _CTR = cfg.get("cutter_rules") or {}
        if _CTR.get("enabled", False):
            _fb_rules += [{"enabled": True, "rule": "BR-50/52", "label": f"belt kit {_cm}", "machine": str(_cm),
                           "matrix_line": str(_cl.get("line", "")), "family_mode": str(_cl.get("mode", "belt_kit")),
                           "family_first": True, "look_ahead": True,
                           "max_delay_h": float(_CTR.get("max_delay_h", 12)), "max_pull_h": float(_CTR.get("max_pull_h", 12))}
                          for _cm, _cl in (_CTR.get("machines") or {}).items()]
        # v71 (user 2026-10-10): BR-41 -- PCR / TBR roller head calenders and TBR pre-assembly: same / compatible compounds
        # together per plant day (1101 / 2201 compound set, 1901 TIL / SQ / EG type blocks then compound)
        _CAL = cfg.get("calender_rules") or {}
        if _CAL.get("enabled", False):
            _fb_rules += [{"enabled": True, "rule": "BR-41", "label": f"compound group {_km}", "machine": str(_km),
                           "matrix_line": str(_kl.get("line", "")), "family_mode": str(_kl.get("mode", "bom_set")),
                           "family_first": True, "look_ahead": True,
                           "max_delay_h": float(_CAL.get("max_delay_h", 12)), "max_pull_h": float(_CAL.get("max_pull_h", 12))}
                          for _km, _kl in (_CAL.get("machines") or {}).items()]
        # v73 (user 2026-10-10): TBR BEAD CHAIN -- on 2403 / 2404 / 2405 an apex lot (TBAPBD) is one family with the wound
        # bead it is built on (TBWB, BOM), so the day's order runs winding -> its apex together (no waiting / aging between)
        _TBC = cfg.get("tbr_bead_chain") or {}
        if _TBC.get("enabled", False):
            _fb_rules += [{"enabled": True, "rule": "TBR-BEAD", "label": f"bead chain {_tm}", "machine": str(_tm),
                           "matrix_line": str(_tl.get("line", "")), "family_mode": "bead_chain",
                           "family_first": True, "look_ahead": True,
                           "max_delay_h": float(_TBC.get("max_delay_h", 12)), "max_pull_h": float(_TBC.get("max_pull_h", 12))}
                          for _tm, _tl in (_TBC.get("machines") or {}).items()]
        # v71 (user 2026-10-10): BR-66 / BR-67 KIT SYNC -- every upstream machine (mixers and 901 excluded) runs its day in
        # the order that lets the most lots finish before their consumer (the next build that needs the item - min aging,
        # or the consumer lot's start - min aging): urgent lot first; a day takes the new order when FEWER lots miss their
        # consumer (or as many miss and changeover / family switches fall), and the day ends no later
        _KS = cfg.get("kit_sync") or {}
        if _KS.get("enabled", False):
            _ks_have = {str(r_.get("machine")) for r_ in _fb_rules}
            _ks_have |= {str((cfg.get(_k) or {}).get("machine", "")) for _k in
                         ("quintoplex_rules", "duplex_rules", "triplex_rules", "quadraplex_rules")
                         if (cfg.get(_k) or {}).get("enabled", False)}
            _fb_rules += [{"enabled": True, "rule": "BR-67", "label": f"kit sync {_ksm}", "machine": str(_ksm),
                           "matrix_line": "", "family_mode": "bom_set", "family_first": False, "look_ahead": True,
                           "max_delay_h": float(_KS.get("max_delay_h", 12)), "max_pull_h": float(_KS.get("max_pull_h", 12))}
                          for _ksm in (_KS.get("machines") or []) if str(_ksm) not in _ks_have]
        # CD-1..3 (v35, config cover_dispatch): every pass below also takes the URGENT lot first (it must start now to
        # finish upstream_margin_h before its first consumer -- changeover + run + min aging/transfer are inside the
        # window), lowest cover first, ties to the item feeding FEWER moulds; else same item, same compound family,
        # cheapest matrix changeover. Next-day lots of the same FAMILY may join today's run (grouping across days).
        # Mixers join the pass (family = main BOM input); butyl / IL / sticky / follower lots stay where they are.
        _CDU = cfg.get("cover_dispatch") or {}
        _CDU_ON = bool(_CDU.get("enabled", False))
        _CDU_M = int(float(_CDU.get("upstream_margin_h", 2)) * 3600 * NS)
        _CDU_ITEM_MOULDS = {}
        if _CDU_ON:
            _mxc = _CDU.get("mixing") or {}
            if _mxc.get("enabled", False):
                _fb_rules += [{"enabled": True, "rule": "CD-3", "label": f"mixing {_mm}", "machine": str(_mm),
                               "family_mode": "main", "family_first": True, "look_ahead": True,
                               "pinned_items": list(_mxc.get("fixed_compounds") or []),
                               "max_delay_h": float(_mxc.get("max_delay_h", 12)), "max_pull_h": float(_mxc.get("max_pull_h", 12))}
                              for _mm in (_mxc.get("machines") or [])]
            try:                                     # moulds an item feeds = distinct presses of the SKUs that use it
                _pmu = pd.read_csv(INPUTS / "plan.csv", usecols=["skuCode", "pressNo"], dtype=str).dropna()
                _sp = _pmu.groupby(_pmu["skuCode"].str.strip())["pressNo"].agg(lambda s_: set(s_.str.strip())).to_dict()
                _bmu = pd.read_csv(INPUTS / "bom.csv", dtype=str, usecols=["Super_parent", "child"]).dropna()
                _acc = defaultdict(set)
                for _sp0, _ch0 in zip(_bmu["Super_parent"].str.strip(), _bmu["child"].str.strip()):
                    _acc[_ch0] |= _sp.get(_sp0, set())
                _CDU_ITEM_MOULDS = {k: len(v) for k, v in _acc.items()}
            except Exception as _e:
                print(f"  [CD UPSTREAM] !! mould counts unavailable ({type(_e).__name__}: {_e}) -- tie-break off")
        _QRL = [cfg.get(_k) or {} for _k in ("quintoplex_rules", "duplex_rules", "triplex_rules", "quadraplex_rules")] + _fb_rules
        if (cfg.get("kit_sync") or {}).get("enabled", False):      # v71 BR-67: urgent-first + accept fewer misses everywhere
            _QRL = [dict(q_, urgent_first=True, accept_on_miss=True) for q_ in _QRL]
        for _QR in _QRL:
            if _QR.get("enabled", False):
                try:
                    import bisect as _bsq
                    _mq = str(_QR.get("machine", "1002"))
                    _DQ, _H7q = int(24 * 3600 * NS), int(7 * 3600 * NS)
                    _maxd = int(float(_QR.get("max_delay_h", 12)) * 3600 * NS)
                    _maxp = int(float(_QR.get("max_pull_h", 12)) * 3600 * NS)
                    _tolq = int(60 * NS)
                    LQ = sorted([l for l, r in schedule.items() if str(r.get("machine")) == _mq
                                 and not str(l).startswith("BLD")], key=lambda l: schedule[l]["scheduled_start"].value)
                    if len(LQ) >= 2:
                        stq = {l: int(schedule[l]["scheduled_start"].value) for l in LQ}
                        fiq = {l: int(schedule[l]["finish_ns"]) for l in LQ}
                        duq = {l: fiq[l] - stq[l] for l in LQ}
                        itq = {l: str(lot_item.get(l, "")) for l in LQ}
                        _coc = {}

                        def _coq(a, b):
                            if not a or a == b:
                                return 0
                            if (a, b) not in _coc:
                                _coc[(a, b)] = int(float(_co_min_v18(_mq, a, b)) * 60 * NS)
                            return _coc[(a, b)]

                        # BOM: main compound (largest qty) and cushion compound of each tread
                        _bq = pd.read_csv(INPUTS / "bom.csv", dtype=str, usecols=["Parent", "child", "child_quantity"])
                        _bq = _bq[_bq["Parent"].str.strip().isin(set(itq.values()))]
                        _bq["q"] = pd.to_numeric(_bq["child_quantity"], errors="coerce").fillna(0.0)
                        _bq["Parent"], _bq["child"] = _bq["Parent"].str.strip(), _bq["child"].str.strip()
                        _main = _bq.sort_values("q", ascending=False).drop_duplicates("Parent").set_index("Parent")["child"].to_dict()
                        _cush_set = {str(x).strip().upper() for x in (_QR.get("family_compounds")
                                                                      or _QR.get("cushion_compounds") or ["B715", "B815"])}
                        _FF = bool(_QR.get("family_first", False)) or _CDU_ON   # BR-25 / CD-3: family before minutes
                        _pin_set = {str(x).strip().upper() for x in (_QR.get("pinned_items") or [])}
                        _PIN = {l for l in LQ if itq[l].strip().upper() in _pin_set}
                        _tag = str(_QR.get("rule", "BR-21")) + " " + str(_QR.get("label", "Quintoplex"))
                        _cush = _bq[_bq["child"].str.upper().isin(_cush_set)].drop_duplicates("Parent").set_index("Parent")["child"].to_dict()
                        if str(_QR.get("family_mode", "")).lower() == "code_prefix":  # v34 BR-41: TIL / SQ / EG type blocks
                            _cush = {itq[l_]: (re.match(r"[A-Za-z]+", itq[l_]) or re.match(r".", itq[l_] or "?")).group(0).upper()
                                     for l_ in LQ}
                            _cush_set = {"TYPE (code prefix: TIL / SQ / EG)"}
                        if str(_QR.get("family_mode", "")).lower() == "itype":      # v31 Quadraplex: sidewall vs shoulder pad
                            _cush = {itq[l_]: str(lot_item_type.get(l_, "") or "").strip().upper() for l_ in LQ}
                            _cush_set = {"SIDEWALL / SHOULDER PAD"}
                        if str(_QR.get("family_mode", "")).lower() == "main":       # v35 mixing: family = main BOM input
                            _cush = {i_: _main.get(i_, i_) for i_ in set(itq.values())}
                            _cush_set = {"main BOM input (master of a final / main polymer of a master)"}
                        if str(_QR.get("family_mode", "")).lower() == "bead_chain":    # v73: apex with its wound bead
                            _wbc = _bq[_bq["child"].str.upper().str.startswith("TBWB")].drop_duplicates("Parent").set_index("Parent")["child"].to_dict()
                            _cush = {i_: "chain " + (i_.strip() if i_.strip().upper().startswith("TBWB") else _wbc.get(i_.strip(), i_.strip()))
                                     for i_ in set(itq.values())}
                            _cush_set = {"BEAD CHAIN (wound bead + its apex)"}
                        if str(_QR.get("family_mode", "")).lower() == "belt_kit":      # v70 BR-50 / BR-52: a SKU's belt kit
                            from collections import Counter as _Ctr
                            _bk = pd.read_csv(INPUTS / "bom.csv", dtype=str, usecols=["Super_parent", "child"]).dropna()
                            _bk["Super_parent"], _bk["child"] = _bk["Super_parent"].str.strip(), _bk["child"].str.strip()
                            _its = {i_.strip() for i_ in itq.values()}
                            _bk = _bk[_bk["child"].isin(_its)]
                            _kit = _bk.groupby("Super_parent")["child"].agg(lambda s_: "+".join(sorted(set(s_))))
                            _kc = _Ctr()
                            for _k_ in _kit.values:
                                for _i_ in _k_.split("+"):
                                    _kc[(_i_, _k_)] += 1
                            _dom = {}
                            for (_i_, _k_), _n_ in _kc.items():
                                if _n_ > _dom.get(_i_, ("", 0))[1]:
                                    _dom[_i_] = (_k_, _n_)
                            _cush = {i_: "kit " + _dom.get(i_.strip(), (i_.strip(), 0))[0] for i_ in set(itq.values())}
                            _cush_set = {"BELT KIT of the building SKU (BOM)"}
                        if str(_QR.get("family_mode", "")).lower() == "matrix_inch":   # v70 BR-61: bead winding by spool inch
                            _cush = {i_: "inch " + _BINCH.get(i_.strip(), "?") for i_ in set(itq.values())}
                            _cush_set = {"SPOOL / FORMER INCH (changeover matrix)"}
                        if str(_QR.get("family_mode", "")).lower() == "bead_inch":     # v70: apex by the inch of its bead
                            _bwc = _bq[_bq["child"].isin(_BINCH)].drop_duplicates("Parent").set_index("Parent")["child"].to_dict()
                            _cush = {i_: "inch " + _BINCH.get(_bwc.get(i_.strip(), ""), "?") for i_ in set(itq.values())}
                            _cush_set = {"BEAD INCH (via its bead winding item)"}
                        if str(_QR.get("family_mode", "")).lower() == "bom_set":    # v30 Triplex: family = all compounds
                            _cush = _bq.groupby("Parent")["child"].agg(lambda s_: "+".join(sorted(set(s_)))).to_dict()
                            _cush_set = {"BOM compound set"}
                        # builds that use each item (material-ledger commits) and consumers of each lot
                        _ibq = defaultdict(list)
                        for _s0, _k0, _q0, _l0 in _L_commits:
                            for _i0 in _L_rate.get(_k0, {}):
                                _ibq[_i0].append(int(_s0))
                        for _k0 in _ibq:
                            _ibq[_k0].sort()
                        _setq = set(LQ)
                        _sucq = defaultdict(list)
                        for _c0, _ps0 in predecessors.items():
                            for _p0 in _ps0:
                                if _p0 in _setq:
                                    _sucq[_p0].append(_c0)
                        relq, dlq = {}, {}
                        for l in LQ:
                            _mn, _mx = _ages_ns(itq[l])
                            _ub = _ibq.get(itq[l], [])
                            _k = _bsq.bisect_left(_ub, fiq[l])
                            nb = _ub[_k] if _k < len(_ub) else None
                            dl = fiq[l] + _maxd
                            if nb is not None:
                                dl = min(dl, nb - _mn)
                            elif not _ub:
                                dl = fiq[l]                            # no ledger info: never later
                            for _c in _sucq.get(l, ()):
                                if _c in schedule and not str(_c).startswith("BLD"):
                                    dl = min(dl, int(schedule[_c]["scheduled_start"].value) - _mn)
                            for _p in predecessors.get(l, ()):
                                if _p in schedule:
                                    dl = min(dl, int(schedule[_p]["finish_ns"]) + int(_ages_ns(lot_item.get(_p, ""))[1]) + duq[l])
                            dlq[l] = max(dl, fiq[l])
                            rl = max(int(UPSTREAM_OPEN_NS), stq[l] - _maxp, int(_material_ready_ns(l)))
                            if nb is not None:
                                rl = max(rl, nb - _mx - duq[l])         # tread not over age at its first build
                            if l in _AGE_FLOOR:
                                rl = max(rl, _AGE_FLOOR[l] - duq[l])
                            relq[l] = min(rl, stq[l])

                        def _dnq(t, d):
                            for _a, _b in sorted(DOWNTIME_WIN.get(_mq, ())):
                                if t < _b and t + d > _a:
                                    t = _b
                            return t

                        def _simq(order, t0, last0, keep=False):
                            """keep=True: the plan order as placed (no pulling earlier, only pushed by changeover)."""
                            out, t, last, co, miss = [], t0, last0, 0, 0
                            for l in order:
                                c = _coq(last, itq[l])
                                s = _dnq(max(t + c, stq[l] if (keep or l in _PIN) else relq[l]), duq[l])
                                f = s + duq[l]
                                co += c
                                miss += int(f > dlq[l] + _tolq)
                                out.append((l, s, f))
                                t, last = f, itq[l]
                            return out, co, miss, t, last

                        def _edd_miss(rest, t0, last0):
                            return _simq(sorted(rest, key=lambda x: dlq[x]), t0, last0)[2]

                        def _greedy(pool, t0, last0):
                            rem, seq, t, last = list(pool), [], t0, last0
                            while rem:
                                best = None
                                for c in rem:
                                    cc = _coq(last, itq[c])
                                    s = _dnq(max(t + cc, relq[c]), duq[c])
                                    f = s + duq[c]
                                    m0 = int(f > dlq[c] + _tolq)
                                    rest = [x for x in rem if x != c]
                                    m1 = _edd_miss(rest, f, itq[c]) if rest else 0
                                    lm, li = _main.get(last or "", ""), _cush.get(last or "", "")
                                    _fc = int(_cush.get(itq[c], "?") != li) if last else 0
                                    if _CDU_ON or _QR.get("urgent_first", False):   # CD-1/2 urgent; v71 BR-67 kit sync
                                        cov = dlq[c] - t
                                        urg = cov < cc + duq[c] + _CDU_M
                                        key = (m0 + m1, 0 if urg else 1, cov if urg else 0,
                                               _CDU_ITEM_MOULDS.get(itq[c], 0) if urg else 0,
                                               int(itq[c] != last), _fc, f - t - duq[c],
                                               int(_main.get(itq[c], "?") != lm), cov, stq[c])
                                    else:
                                        key = None
                                    key = key or ((m0 + m1, _fc, f - t - duq[c], int(_main.get(itq[c], "?") != lm), dlq[c], stq[c])
                                           if _FF else                                  # family first (BR-25)
                                           (m0 + m1, f - t - duq[c],                    # misses, minutes lost (changeover + idle)
                                            int(_main.get(itq[c], "?") != lm), int(_cush.get(itq[c], "?") != li),
                                            dlq[c], stq[c]))
                                    if best is None or key < best[0]:
                                        best = (key, c)
                                seq.append(best[1])
                                rem.remove(best[1])
                                _, _, _, t, last = _simq([best[1]], t, last)
                            return seq

                        def _greedy_pinned(pool, t0, last0):
                            """CD mixing: butyl / IL / sticky / follower lots keep their place; the lots between them are
                            re-sequenced (so B99 stays directly in front of IL3 / IL5 / 30G)."""
                            if not (_PIN & set(pool)):
                                return _greedy(pool, t0, last0)
                            seq, seg = [], []

                            def _flush():
                                tt, ll = (_simq(seq, t0, last0)[3:5] if seq else (t0, last0))
                                return _greedy(seg, tt, ll)
                            for l in sorted(pool, key=lambda x: stq[x]):
                                if l in _PIN:
                                    if seg:
                                        seq += _flush()
                                        seg = []
                                    seq.append(l)
                                else:
                                    seg.append(l)
                            if seg:
                                seq += _flush()
                            return seq

                        def _fsw(order, last0):
                            """family switches along the order (from the item running before it)"""
                            n, p = 0, _cush.get(last0 or "", None)
                            for l in order:
                                f_ = _cush.get(itq[l], None)
                                n += int(p is not None and f_ != p)
                                p = f_
                            return n

                        days = defaultdict(list)
                        for l in LQ:
                            days[(stq[l] - _H7q) // _DQ].append(l)
                        dkeys = sorted(days)
                        taken = set()
                        t, last = stq[LQ[0]], None
                        newpos, stat = {}, defaultdict(int)
                        co_before = sum(_coq(itq[a], itq[b]) for a, b in zip(LQ, LQ[1:])) / 60 / NS
                        for di, d in enumerate(dkeys):
                            pool = [l for l in days[d] if l not in taken]
                            if _QR.get("look_ahead", True) and di + 1 < len(dkeys):
                                d_end = (d + 1) * _DQ + _H7q
                                items_today = {itq[l] for l in pool}
                                fams_today = {_cush.get(itq[l]) for l in pool} - {None} if _CDU_ON else set()
                                for l in days[dkeys[di + 1]]:              # same die (CD-3: or same family), within its window
                                    if ((itq[l] in items_today or _cush.get(itq[l]) in fams_today)
                                            and relq[l] < d_end and l not in _PIN):
                                        pool.append(l)
                            if not pool:
                                continue
                            orig = sorted(pool, key=lambda l: stq[l])
                            so, coo, mo, eo, lo = _simq(orig, t, last, keep=True)
                            g = _greedy_pinned(pool, t, last)
                            sg, cog, mg, eg, lg = _simq(g, t, last)
                            _better = (((_fsw(g, last), cog) < (_fsw(orig, last), coo)) if _FF else cog < coo)
                            if (mg <= mo and _better and eg <= eo) or (_QR.get("accept_on_miss", False) and mg < mo and eg <= eo):
                                out, t, last = sg, eg, lg
                                stat["days_reordered"] += 1
                                stat["saved_min"] += (coo - cog) / 60 / NS
                                stat["pulled_ahead"] += sum(1 for l in pool if l not in days[d])
                                taken.update(pool)
                            else:
                                base = sorted([l for l in days[d] if l not in taken], key=lambda l: stq[l])
                                out, _, mb, t, last = _simq(base, t, last, keep=True)
                                stat["days_kept"] += 1
                                stat["kept_with_miss"] += mb
                                taken.update(base)
                            for l, s0, f0 in out:
                                newpos[l] = (s0, f0)
                        if True:
                            for l, (s0, f0) in newpos.items():
                                schedule[l]["scheduled_start"] = pd.Timestamp(int(s0))
                                schedule[l]["scheduled_finish"] = pd.Timestamp(int(f0))
                                schedule[l]["finish_ns"] = int(f0)
                            _oldq = {stq[l] for l in LQ}
                            machine_busy[_mq] = sorted([iv for iv in machine_busy.get(_mq, []) if iv[0] not in _oldq]
                                                       + [(int(s0), int(f0)) for s0, f0 in newpos.values()])
                            LQ2 = sorted(LQ, key=lambda l: newpos[l][0])
                            co_after = sum(_coq(itq[a], itq[b]) for a, b in zip(LQ2, LQ2[1:])) / 60 / NS
                            print(f"  [{_tag}] {_mq}: family ({'/'.join(sorted(_cush_set))}) switches "
                                  f"{_fsw(LQ, None)} -> {_fsw(LQ2, None)}")
                            print(f"  [{_tag}] {_mq}: {len(LQ)} lots, {stat['days_reordered']} plant days re-sequenced "
                                  f"(least changeover, same die / compound family together), {stat['days_kept']} kept; "
                                  f"{stat['pulled_ahead']} same-die lots joined from the next day; changeover "
                                  f"{co_before:,.0f} -> {co_after:,.0f} min; lots late in the plan order on kept days (unchanged by this pass): "
                                  f"{stat['kept_with_miss']}")
                except Exception as _eq:
                    print(f"  [{_QR.get('rule', 'BR-21')}] !! pass failed ({type(_eq).__name__}: {_eq}) -- machine unchanged")

        if cfg.get("final_repair_pass", False):
            try:                                     # REPORT-ONLY snapshot: placement before the final repair
                pd.DataFrame([{"lot_id": _l, "item": _r.get("item"), "machine": _r.get("machine"),
                               "scheduled_start": _r["scheduled_start"], "scheduled_finish": _r["scheduled_finish"]}
                              for _l, _r in schedule.items()]).to_csv(OUTPUTS / "phase5_pre_repair_snapshot.csv", index=False)
            except Exception:
                pass
            _mix_day_fix()                           # v49: one mixer per compound per plant day
            _mix_catch_up()                          # v48: late windows join the current run (rule B, no throttling)
            _final_repair()                          # ONCE: re-running it drifts the plan (max-aging feedback)
            _SMc = (cfg.get("business_rules") or {}).get("same_item_shift_merge") or {}
            _bp = None if _MIXPLAN_ON else locals().get("_brm09_pass")
            for _rnd in range(int(_SMc.get("rounds", 4))):
                _chg = 0                             # STRICT after the repair: no consumer may start later
                if _bp is not None and _SMc.get("enabled", False):
                    _chg += _bp(0, f"after repair, strict, round {_rnd + 1}")
                if _ILC.get("enabled", False) and _ILC.get("regroup_merged", False) and not _MIXPLAN_ON:
                    _chg += _il_day_fix()
                if not _chg:
                    break
            # v70 (user 2026-10-10): BR-56 -- same item / setup in ONE block per shift on the cutters and slitters: the
            # BRM-09 local join, STRICT (no consumer may start later; inputs + min aging, changeovers, PM kept), only on
            # the machines in cutter_shift_merge (mixers untouched -- planner lots fixed; 901 excluded)
            _CSM = cfg.get("cutter_shift_merge") or {}
            if _CSM.get("enabled", False) and locals().get("_brm09_pass") is not None:
                _sm_save = _SM.get("machines")
                _SM["machines"] = [str(x) for x in (_CSM.get("machines") or [])]
                try:
                    for _rnd in range(int(_CSM.get("rounds", 3))):
                        if not _brm09_pass(0, f"BR-56 cutters / slitters, strict, round {_rnd + 1}"):
                            break
                finally:
                    _SM["machines"] = _sm_save
            # v38 BRM-09 complete: splits the strict rounds could not join (a consumer would start later) are joined
            # with a consumer delay <= post_repair_relaxed_h; the repair then re-times every later lot (later-only,
            # inputs / changeovers / PM kept) and the strict rounds run again on the result.
            _rel = float(_SMc.get("post_repair_relaxed_h", 0) or 0)
            if _rel > 0 and _bp is not None and _SMc.get("enabled", False):
                if _bp(_rel, f"after repair, relaxed (consumer <= {_rel:g} h later)"):
                    _final_repair()
                    for _rnd in range(2):
                        if not _bp(0, f"after the relaxed join + repair, strict, round {_rnd + 1}"):
                            break
            if _ILC.get("enabled", False) and _ILC.get("once_per_plant_day", False) and not _MIXPLAN_ON:
                if _il_once_per_day():
                    _final_repair(_up_on=False)          # build blocks re-timed to the moved material
                    if _bp is not None and _SMc.get("enabled", False):
                        _bp(0, "after the IL day fix, strict")
            if (cfg.get("butyl_day_block") or {}).get("enabled", False):
                try:                                     # v39: last mixing pass -- one butyl block per day
                    _nbd, _rows_bd = _butyl_day_blocks() if not _MIXPLAN_ON else (0, [])
                    if _rows_bd:
                        lots = pd.concat([lots, pd.DataFrame(_rows_bd)], ignore_index=True)
                    if _nbd:
                        _mix_day_fix()                   # v49
                        _mix_catch_up()                  # v48: runs split by the block layout joined again
                        _final_repair()                  # blocks move as units; consumers / creel / PM / aging / builds re-timed
                    if (cfg.get("fourrc_rules") or {}).get("replan_after_repair", False) and _fourrc_replan():
                        _final_repair()                  # v45: consumers re-timed to the re-planned 901
                    _QUc = cfg.get("queue_unblock") or {}
                    if _QUc.get("enabled", False):       # v49: 901 first, then (re-timed) the cutters
                        for _grp in (_QUc.get("machine_groups") or []):
                            if _queue_unblock([str(x) for x in _grp]):
                                _final_repair()
                    if (cfg.get("fourrc_rules") or {}).get("day_plan", {}).get("mandatory", False):
                        for _r4 in range(4):             # v40: rule 1 (one go a day) + rule 3 (fabric in every creel window); v60: 2 -> 4
                            _a4 = _fourrc_one_go_fix()
                            _b4 = _fourrc_fabric_bridge() if (cfg.get("fourrc_rules") or {}).get("fabric_bridge", True) else 0
                            _c4 = _fourrc_creel_fill()           # v58: idle creel-reload windows filled with fabric
                            _d4 = _fourrc_load_compact()         # v60: no idle inside a dip / spool load
                            if not ((cfg.get("fourrc_rules") or {}).get("urgent_pull") or {}).get("after_day_plan", False):
                                _d4 = _fourrc_urgent_pull() or _d4   # v88: waited-for lots into idle gaps (switch, default off)
                            if not (_a4 or _b4 or _c4 or _d4):
                                break
                            _final_repair()              # consumers of the moved 901 lots re-timed
                    if _mix_day_fix():                   # v49: last safety net for rule A, then re-time
                        _final_repair()
                    if ((cfg.get("fourrc_rules") or {}).get("urgent_pull") or {}).get("after_day_plan", False):
                        if _fourrc_urgent_pull():        # v89: once, after the last 4RC day-plan pass (fabric only)
                            _final_repair()              # consumers re-timed to the pulled 901 lots
                    for _r97 in range(int((((cfg.get("fourrc_rules") or {}).get("left_shift") or {}).get("rounds", 2)))):
                        if not _fourrc_left_shift():     # v97: whole 901 loads earlier into idle (switch, default off)
                            break
                        _final_repair()                  # consumers of the earlier 901 lots move earlier (floor_to_ready)
                    if _edd_resequence():                # v106 / v118: earliest-due-first on listed machines (switch, default off)
                        _final_repair()                  # consumers re-timed to the re-laid lots
                except Exception as _ebd:
                    import traceback as _tb
                    _tb.print_exc()
                    print(f"  [BUTYL DAY BLOCK v39] !! failed ({type(_ebd).__name__}: {_ebd})")

        _UGF = cfg.get("upstream_gap_fill") or {}
        if _UGF.get("repeg_compounds", False) or _UGF.get("floor_to_ready", False):
            _n_rp = _n_rc = 0
            if _UGF.get("repeg_compounds", False):
                # v81 A2: a component lot waits for the compound lot phase 3 pegged to it, not for any usable batch of that
                # compound. Re-allocate, per compound, the quantity going to component (non-mixer) consumers FEFO: the
                # earliest-ready batches to the earliest-starting consumers, only from batches still within shelf life at the
                # consumer start. Every producer keeps its total, every consumer its need -> material stays balanced.
                _mixx = {str(x) for x in ((cfg.get("business_rules") or {}).get("mixers") or [])}
                _ctype = {"MASTER COMPOUND", "FINAL COMPOUND"}
                # v83 (user 2026-10-12): ITEM-STOCK ALLOCATION. mode "item_stock": every upstream item, not only compounds,
                # and a consumer's need is net of the opening stock already allocated to it (open_alloc) -- so a batch is no
                # longer reserved for a lot that ran on floor stock (B1144 12/13-Oct -> BEF001 L0001-4). Freed quantity stays
                # with its producer as stock for later consumers. Consumers that are mixer or building lots are not touched.
                _ISTK = str(_UGF.get("repeg_mode", "compounds")).lower() == "item_stock"
                _by_c = defaultdict(list)
                for (_pp, _cc), _qq in list(edge_qty.items()):
                    if (_pp in schedule and _cc in schedule and _qq and _qq > 0
                            and (_ISTK or str(lot_item_type.get(_pp, "")).strip().upper() in _ctype)
                            and str(schedule[_cc].get("machine")) not in _mixx
                            and not str(_cc).startswith("BLD")):
                        _by_c[lot_item.get(_pp, "")].append((_pp, _cc, float(_qq)))
                _n_net = 0
                _q_net = 0.0
                for _it, _rows in _by_c.items():
                    _pmin, _pmax = _ages_ns(_it)
                    _sup = defaultdict(float)
                    _need = defaultdict(float)
                    for _pp, _cc, _qq in _rows:
                        _sup[_pp] += _qq
                        _need[_cc] += _qq
                    _cov_it = {}
                    if _ISTK and open_alloc:
                        for _cc in list(_need):
                            if (_cc, _it) in _OPEN_NETTED:
                                continue
                            _cv = float(open_alloc.get((_cc, _it), 0.0) or 0.0)
                            if _cv > 1e-9:
                                _cov_it[_cc] = min(_cv, _need[_cc])
                                _need[_cc] -= _cov_it[_cc]
                    if len(_sup) < 2 and not _cov_it:
                        continue
                    _supl = sorted([int(schedule[_pp]["finish_ns"]) + _pmin, _pp, _q] for _pp, _q in _sup.items())
                    _cons = sorted(_need, key=lambda x: (int(schedule[x]["scheduled_start"].value), str(x)))
                    _new = []
                    _ok = True
                    for _cc in _cons:
                        _left = _need[_cc]
                        if _left <= 1e-9:
                            continue                            # v83: fully covered by floor stock
                        _cst = int(schedule[_cc]["scheduled_start"].value)
                        for _s in _supl:
                            if _left <= 1e-9:
                                break
                            if _s[2] <= 1e-9:
                                continue
                            if _pmax and _s[0] - _pmin + _pmax < _cst:
                                continue                        # already past shelf life at this consumer
                            _t = min(_left, _s[2])
                            _new.append((_s[1], _cc, _t))
                            _s[2] -= _t
                            _left -= _t
                        if _left > 1e-6:
                            _ok = False
                            break
                    if not _ok:
                        continue                                 # keep phase 3's pegs for this item
                    _old = {(_pp, _cc) for _pp, _cc, _ in _rows}
                    if {(a_, b_) for a_, b_, _ in _new} == _old and not _cov_it:
                        continue
                    for _cc in _cov_it:
                        _OPEN_NETTED.add((_cc, _it))            # gate / audits: cover already netted out
                        _n_net += 1
                        _q_net += _cov_it[_cc]
                    for _pp, _cc in _old:
                        edge_qty.pop((_pp, _cc), None)
                        predecessors[_cc] = [x for x in predecessors.get(_cc, []) if x != _pp]
                        successors[_pp] = [x for x in successors.get(_pp, []) if x != _cc]
                    for _pp, _cc, _t in _new:
                        edge_qty[(_pp, _cc)] = edge_qty.get((_pp, _cc), 0.0) + _t
                        if _pp not in predecessors[_cc]:
                            predecessors[_cc].append(_pp)
                        if _cc not in successors[_pp]:
                            successors[_pp].append(_cc)
                    _n_rc += 1
                    _n_rp += len(_new)
                print(f"  [UPSTREAM GAP FILL v81 A2] compound batches re-allocated FEFO to component lots: {_n_rc} compounds, "
                      f"{_n_rp} producer->consumer links (totals per producer and per consumer unchanged)")
                if _ISTK:
                    print(f"  [ITEM-STOCK v83] all upstream items re-allocated FEFO from item stock: {_n_rc} items; "
                          f"{_n_net} (consumer,item) needs netted of opening stock ({_q_net:,.0f} units no longer "
                          f"reserved from new lots) -- shelf life / min aging / machine order unchanged")
            if _UGF.get("family_reorder", False):
                # v82 (user 2026-10-12): SAME-FAMILY GAP FILL on component machines. An idle gap between A and B on a
                # machine (not a mixer, not 901) takes a LATER lot X of the SAME compound family as A or B (same BOM compound
                # set; on BR-56 cutters / slitters the SAME item as A or B) when X's inputs are ready, it fits with the matrix
                # changeover on both sides, and it is not made earlier than its shelf-life floor allows. Because X joins a
                # family already on that side of the gap, family switches can only stay the same or fall (BR-21/25/28/32/41/61
                # family order kept); the final repair below re-checks every rule (inputs, changeover, PM, aging, sequence).
                _mixx2 = {str(x) for x in ((cfg.get("business_rules") or {}).get("mixers") or [])}
                _xm = {str(x) for x in (cfg.get("compress_exclude_machines") or [])} | _mixx2 | {"901"}
                _cut = {str(x) for x in (((cfg.get("business_rules") or {}).get("cutter_shift_merge") or {}).get("machines")
                                         or cfg.get("cutter_shift_merge", {}).get("machines") or [])}
                _ct2 = {"MASTER COMPOUND", "FINAL COMPOUND"}
                _famc = {}

                def _fam(_l):
                    if _l not in _famc:
                        _cs = sorted({str(lot_item.get(p_, "")) for p_ in predecessors.get(_l, ())
                                      if str(lot_item_type.get(p_, "")).strip().upper() in _ct2})
                        _famc[_l] = tuple(_cs) if _cs else ("ITEM", str(lot_item.get(_l, "")))
                    return _famc[_l]

                def _coq(_m, _a, _b):
                    if not _a or not _b or _a == _b:
                        return 0
                    try:
                        return int(float(_co_min(_m, _a, _b)[0] or 0.0) * 60 * NS)
                    except Exception:
                        return int(float(compute_changeover_min(_m, "", _a, "", _b, "") or 0.0) * 60 * NS)
                _bym = defaultdict(list)
                for _l, _sc in schedule.items():
                    _m = str(_sc.get("machine"))
                    if (str(_l).startswith("BLD") or _m in _xm or _l in _PLANNED or _l in IL_PINNED or "PREP" in str(_l)
                            or str(_sc.get("department", "")).upper() in ("BUILDING", "CURING")):
                        continue
                    _bym[_m].append(_l)
                _n_fr = 0
                _h_fr = 0.0
                _GAPMIN = int(float(_UGF.get("min_gap_min", 30)) * 60 * NS)
                for _m, _ls in _bym.items():
                    _moved_any = True
                    _guard = 0
                    while _moved_any and _guard < 50:
                        _guard += 1
                        _moved_any = False
                        _ls.sort(key=lambda x: int(schedule[x]["scheduled_start"].value))
                        for _i in range(len(_ls) - 1):
                            _A, _B = _ls[_i], _ls[_i + 1]
                            _gs, _ge = int(schedule[_A]["finish_ns"]), int(schedule[_B]["scheduled_start"].value)
                            if _ge - _gs < _GAPMIN:
                                continue
                            _iA, _iB = str(lot_item.get(_A, "")), str(lot_item.get(_B, ""))
                            _best = None
                            for _X in _ls[_i + 2:]:
                                _iX = str(lot_item.get(_X, ""))
                                if _m in _cut:
                                    if _iX not in (_iA, _iB):
                                        continue
                                elif _fam(_X) not in (_fam(_A), _fam(_B)):
                                    continue
                                _du = int(schedule[_X]["finish_ns"]) - int(schedule[_X]["scheduled_start"].value)
                                _t = max(_gs + _coq(_m, _iA, _iX), int(_material_ready_ns(_X)), int(UPSTREAM_OPEN_NS))
                                if _X in _AGE_FLOOR:
                                    _t = max(_t, int(_AGE_FLOOR[_X]) - _du)
                                _ws = DOWNTIME_WIN.get(_m, ())
                                for _a, _b in _ws:
                                    if _t < _b and _t + _du > _a:
                                        _t = _b
                                if _t + _du + _coq(_m, _iX, _iB) > _ge:
                                    continue
                                _due = lot_first_need.get(_X)
                                _due = pd.Timestamp(_due).value if (_due is not None and not pd.isna(_due)) else 2 ** 62
                                if _best is None or _due < _best[0]:
                                    _best = (_due, _X, _t, _du)
                            if _best is not None:
                                _, _X, _t, _du = _best
                                _old = int(schedule[_X]["scheduled_start"].value)
                                schedule[_X]["scheduled_start"] = pd.Timestamp(int(_t))
                                schedule[_X]["scheduled_finish"] = pd.Timestamp(int(_t + _du))
                                schedule[_X]["finish_ns"] = int(_t + _du)
                                if _COMPACT and _X in _FLOOR0:
                                    _FLOOR0[_X] = min(int(_FLOOR0[_X]), int(_t))
                                _n_fr += 1
                                _h_fr += (_old - _t) / (3600.0 * NS)
                                _moved_any = True
                                break
                print(f"  [UPSTREAM GAP FILL v82] same-family reorder: {_n_fr} component lots moved forward into idle gaps "
                      f"({_h_fr:,.0f} h earlier in total) -- same compound family / same item on cutters, inputs + changeover + "
                      "PM + shelf-life floor kept; final repair re-checks")
            _final_repair()                               # A1 floors + A2 pegs: component lots move back into idle
            print("  [UPSTREAM GAP FILL v81 A1] final repair with ready-time floors on component machines "
                  f"(floor_to_ready={bool(_UGF.get('floor_to_ready', False))})")

        if _LEDGER:                                       # C4: material-balance PROOF on the final schedule
            _bi, _nb, _rows = _L_replay(int(0.25 * 3600 * NS))
            pd.DataFrame(_rows, columns=["build_lot", "sku", "build_start", "item", "cum_consumed",
                                         "cum_supply_ready_at", "starts_before_material"]
                         ).to_csv(OUTPUTS / "phase5_material_ledger.csv", index=False)
            print(f"  [C4 LEDGER PROOF] {_nb} of {len(_L_commits)} build blocks start before their "
                  f"material physically exists (15-min tolerance) -- MUST be 0; items: "
                  f"{sorted(_bi)[:8]}  -> phase5_material_ledger.csv")
        _DAYn = 24 * 3600 * NS
        print(f"  [BUILDING ECHO] pinned {n_placed:,} plan rows on {bp['buildMachine'].nunique()} "
              f"TBMs, {int(bp['qty'].sum()):,} tyres; kit-late slides: {n_kit_late:,}; "
              f"SKUs with no phase2 GT lot (data gap): {n_nogt:,}")
        if _n_skipped_unbuildable:
            print(f"  [BUILDING ECHO] SKIPPED {_n_skipped_unbuildable} unbuildable build rows "
                  f"(no routing/BOM GT) across {len(_skipped_skus)} SKU(s): "
                  f"{', '.join(sorted(_skipped_skus))}  -- not built, demand not carried (user directive)")
        print(f"  [BUILDING ECHO] opening component WIP credited to the kit-gate: "
              f"{_wip_credit_tot:,.0f} units across {len(sku_item_wip)} SKUs  (warm-start ramp cover)")
        print(f"  [BUILDING ECHO] drum slide vs plan: max {_slide_ns_max/_DAYn:.2f} d, "
              f"total {_slide_ns_sum/_DAYn:.1f} block-days  "
              f"[makespan meter -- 0 = building rides its 30-day plan exactly]")
        if _kit_late_by_sku:
            _top = sorted(_kit_late_by_sku.items(), key=lambda kv: -kv[1])[:6]
            print(f"  [BUILDING ECHO] most kit-late SKUs: "
                  + ", ".join(f"{s}:{n}" for s, n in _top))
        return n_placed, 0, n_kit_late

    # == UPSTREAM COMPRESS (2026-09-30, config `compress_upstream`) ===============================
    # RCA 05-Oct (30-09 plan): every compound lot is timed JIT to its FIRST consumer by an
    # infinite-capacity CPM, as if all 19 day-5 treads could run at once. They share ONE extruder
    # (1002, ~16-20 h of work), so the day's tread compound B815_L0006 (ready 04-Oct 14:14) let 1002
    # start only at 14:24 and the last 7 treads finished 1-9 h after the 07:00 builds -> the TBM
    # backlog -> 2,095 cures lost on 05-Oct, while F270 sat 14 h idle on 04-Oct. Simulated: with
    # material on time the plan builds 100 %.
    # Fix: a left-shift (semi-active) pass. Upstream lots in time order are moved EARLIER into their
    # own machine's idle, never before: inputs ready (+min aging), upstream open, or
    # last_need - max aging (+margin) -- so nothing is made old. A changeover allowance is kept
    # free on each side; the final repair pass later enforces the exact matrix time.
    if cfg.get("compress_upstream", False):
        _pad = int(float(cfg.get("compress_changeover_pad_min", 30)) * 60 * NS)
        _marg = int(float(cfg.get("compress_aging_margin_h", 2.0)) * 3600 * NS)
        _cx = {str(x) for x in (cfg.get("compress_exclude_machines") or [])}     # v63: mixers / 901 untouched
        _ALTD69 = {str(x).strip().upper() for x in (cfg.get("compress_alt_machine_departments") or [])}   # v69 #4
        _n_alt69 = [0]
        _upl = [l for l, sc in schedule.items() if not str(l).startswith("BLD")
                and not _is_terminal_dept(str(sc.get("department", "")))
                and str(sc.get("machine")) not in _cx and l not in _PLANNED]
        _n_c = 0
        _h_c = 0.0
        for _sweep in range(int(cfg.get("compress_sweeps", 2))):
            _upl.sort(key=lambda l: (schedule[l]["scheduled_start"].value, str(l)))
            _mv_sw = 0
            for _l in _upl:
                _sc = schedule[_l]
                _m = str(_sc["machine"])
                _st = int(_sc["scheduled_start"].value)
                _fi = int(_sc["finish_ns"])
                _du = max(_fi - _st, 1)
                _mr = _material_ready_ns(_l)
                _floor = max(UPSTREAM_OPEN_NS, _mr if _mr > NEG_INF_NS else UPSTREAM_OPEN_NS)
                _ln = lot_last_need.get(_l)
                if _ln is not None:
                    _mx = _ages_ns(lot_item.get(_l, ""))[1]
                    _floor = max(_floor, int(_ln) - int(_mx) + _marg - _du)
                if _floor >= _st - int(0.25 * 3600 * NS) or _l in IL_PINNED:   # BR-V10: campaigns stay whole
                    continue
                _busy = machine_busy[_m]
                _ix = next((i for i, iv in enumerate(_busy) if iv[0] == _st), None)
                if _ix is None:
                    continue                                  # own interval not found: leave it
                _own = _busy.pop(_ix)
                _ns = first_fit_jit(_busy, _floor, _floor, _du + 2 * _pad)
                # v69 #4 (user 2026-10-10): bead winding / apexing lots may take the earliest hole on ANY bead machine
                # their routing allows (2403 / 2404 / 2405 ...), so winding and apexing no longer queue on one machine
                _m2 = _m
                if str(_sc.get("department", "")).strip().upper() in _ALTD69:
                    for _am in [str(x) for x in (lot_machines.get(_l) or [])]:
                        if _am == _m or _am in _cx or _am not in machine_busy:
                            continue
                        _an = first_fit_jit(machine_busy[_am], _floor, _floor, _du + 2 * _pad)
                        if _an is not None and (_ns is None or _an < _ns):
                            _ns, _m2 = _an, _am
                if _ns is None or _ns + _pad >= _st - int(0.25 * 3600 * NS):
                    bisect.insort(_busy, _own)
                    continue
                _ns = _ns + _pad
                if _m2 != _m:
                    _sc["machine"] = _m2
                    _busy = machine_busy[_m2]
                    _n_alt69[0] += 1
                bisect.insort(_busy, (_ns, _ns + _du + (_own[1] - _fi if _own[1] > _fi else 0)))
                _sc["scheduled_start"] = pd.Timestamp(int(_ns))
                _sc["scheduled_finish"] = pd.Timestamp(int(_ns + _du))
                _sc["finish_ns"] = int(_ns + _du)
                _h_c += (_st - _ns) / (3600.0 * NS)
                _mv_sw += 1
            _n_c += _mv_sw
            if not _mv_sw:
                break
        print(f"  [COMPRESS] upstream left-shift: {_n_c:,} lot moves earlier into idle "
              f"({_h_c:,.0f} h total, avg {_h_c / max(_n_c, 1):.1f} h) -- inputs, aging floor and "
              f"changeover allowance respected; v69: {_n_alt69[0]} bead lots moved to another allowed bead machine")

    if os.environ.get("CTP_DUMP_PRE_ECHO"):                 # RCA probe: upstream as first placed
        pd.DataFrame([{"lot_id": k, "item": v.get("item"), "machine": v.get("machine"),
                       "start": v.get("scheduled_start"), "finish": v.get("scheduled_finish")}
                      for k, v in schedule.items()]).to_csv(OUTPUTS / "rca_pre_echo_schedule.csv", index=False)
    if BUILDING_PLAN_ECHO:
        n_bld, n_bld_co, _n_kit_late = _revise_building_schedule_echo()
    else:
        n_bld, n_bld_co = place_building_campaigns(building_lots)
    placed += n_bld

    # ========================================================================
    # S3 -- PER-SKU GREEN-TYRE WIP CAP (the rope, a HARD invariant)
    # ========================================================================
    # A green tyre of a SKU may not be BUILT if it would push the count of un-cured
    # green tyres of that SKU over ceil(cover_h x that SKU's press-consumption rate).
    # The rate is measured from the REVISED (P1) press schedule -- what the press
    # ACTUALLY consumes -- so the cap is inert in pass 1 (no P1 yet) and binds only
    # in pass 2+. A lot that would breach the cap is pushed later into its machine's
    # next free slot and LOGGED loudly. Never silently rotted.
    #
    # Runs on the post-building schedule (SHORT-LIFE RESYNC below is off by default
    # and only re-times COMPONENT lots, not green tyres, so GT positions here are
    # final for reporting). Flag-gated: OFF => this block is a no-op (byte-identical).
    def _enforce_gt_wip_cap():
        import math as _math
        if not bool(cfg.get("green_tyre_wip_cap", False)):
            return
        p1_path = OUTPUTS / "phase6_press_start_P1.csv"
        cover_h = float(cfg.get("green_tyre_wip_cover_h", 24) or 24)
        if not p1_path.exists():
            print("  [S3] green_tyre_wip_cap ON but no P1 snapshot (pass 1) -- the cap "
                  "needs the REVISED press consumption; SKIPPED this pass.")
            return
        try:
            _p1 = pd.read_csv(p1_path)
            _p1["startTime"] = pd.to_datetime(_p1["startTime"], errors="coerce")
            _p1["qty"] = pd.to_numeric(_p1["qty"], errors="coerce").fillna(0.0)
            _p1["skuCode"] = _p1["skuCode"].astype(str).str.strip()
        except Exception as _e:
            print(f"  [S3] P1 snapshot unreadable ({_e}) -- WIP cap SKIPPED.")
            return
        # --- per-SKU cure (consumption) stream from P1: sorted (start_ns, qty) ---
        cure = defaultdict(list)     # sku -> [(start_ns, qty)]
        for s, t, q in zip(_p1.skuCode, _p1.startTime, _p1.qty):
            if pd.notna(t) and q > 0:
                cure[s].append((pd.Timestamp(t).value, float(q)))
        cure_cum = {}                # sku -> (times[], cumqty[])
        cap_by_sku = {}              # sku -> cap (tyres)
        for s, evs in cure.items():
            evs.sort()
            times = [e[0] for e in evs]
            cum, run = [], 0.0
            for _, q in evs:
                run += q; cum.append(run)
            cure_cum[s] = (times, cum)
            total = run
            span_h = max((times[-1] - times[0]) / 3600.0 / NS, cover_h)
            rate = total / span_h                 # tyres per hour cured
            cap_by_sku[s] = max(1, int(_math.ceil(cover_h * rate)))
        # --- green-tyre lots that were placed, grouped by SKU ---
        gt_by_sku = defaultdict(list)   # sku -> [lid]
        for lid, rec in schedule.items():
            if lot_item_type.get(lid, "") != "GREEN TYRES":
                continue
            s = _lot_sku1.get(lid, "")
            if s in cure_cum:
                gt_by_sku[s].append(lid)
        matched = sum(len(v) for v in gt_by_sku.values())
        n_gt_total = sum(1 for lid in schedule if lot_item_type.get(lid, "") == "GREEN TYRES")
        if matched == 0:
            print(f"  [S3] WIP cap: 0 of {n_gt_total:,} placed green-tyre lots matched a "
                  f"P1 cure stream by SKU -- cap INERT this pass (nothing to check).")
            return

        def _cured_at(s, t_ns):
            times, cum = cure_cum[s]
            i = bisect.bisect_right(times, t_ns)
            return cum[i - 1] if i > 0 else 0.0

        def _first_free_after(m, floor_ns, dur_ns):
            t = floor_ns
            for (a, b) in machine_busy.get(m, []):
                if b <= t:
                    continue
                if a >= t + dur_ns:
                    break
                if a < t + dur_ns and b > t:
                    t = max(t, b)
            return t

        def _peak_wip(lids, s):
            # honest: iterate the FULL lot set (delayed lots reflect their new
            # finish_ns; unfixable lots STILL count), netted against P1 cure events.
            ev = [(schedule[lid]["finish_ns"], +float(lot_qty.get(lid, 0) or 0))
                  for lid in lids]
            ev += [(t_ns, -q) for t_ns, q in cure[s]]
            ev.sort()
            wip = pk = 0.0
            for _, d in ev:
                wip += d
                if wip > pk:
                    pk = wip
            return pk

        # freeze the full per-SKU lot set BEFORE enforcement so peak_after counts
        # every lot (including unfixable ones), not just the ones left in the walk.
        full_lots = {s: list(gt_by_sku[s]) for s in gt_by_sku}
        n_pushed = 0
        pushed_hours = []
        unfixable = 0
        peak_before = {s: _peak_wip(full_lots[s], s) for s in gt_by_sku}
        for s in gt_by_sku:
            cap = cap_by_sku[s]
            lots = gt_by_sku[s]
            unfix_lids = set()
            guard = 0
            limit = 4 * len(lots) + 8
            while guard < limit:
                guard += 1
                lots.sort(key=lambda l: schedule[l]["finish_ns"])
                built = 0.0
                offender = None
                for lid in lots:
                    q = float(lot_qty.get(lid, 0) or 0)
                    built += q            # unfixable lots STILL consume WIP -> still counted
                    if lid in unfix_lids:
                        continue
                    fin = schedule[lid]["finish_ns"]
                    wip = built - _cured_at(s, fin)
                    if wip > cap:
                        offender = (lid, q, built)
                        break
                if offender is None:
                    break
                lid, q, built = offender
                if q > cap:
                    # one lot alone exceeds the cap -- delaying cannot fix it. Flag it,
                    # keep counting its qty, and scan past it (never silently dropped).
                    unfix_lids.add(lid)
                    unfixable += 1
                    continue
                # earliest cure time where room opens: cured(t*) >= built - cap
                need = built - cap
                times, cum = cure_cum[s]
                j = bisect.bisect_left(cum, need)
                t_star = times[j] if j < len(times) else times[-1]
                m = schedule[lid]["machine"]
                dur_ns = schedule[lid]["finish_ns"] - schedule[lid]["scheduled_start"].value
                cur_s = schedule[lid]["scheduled_start"].value
                floor = max(t_star - dur_ns, cur_s)     # NEVER earlier
                # remove old interval, find a free later slot, reinsert
                old = (cur_s, schedule[lid]["finish_ns"])
                try:
                    machine_busy[m].remove(old)
                except (ValueError, KeyError):
                    pass
                new_s = _first_free_after(m, floor, dur_ns)
                new_f = new_s + dur_ns
                bisect.insort(machine_busy.setdefault(m, []), (new_s, new_f))
                schedule[lid]["scheduled_start"] = pd.Timestamp(new_s)
                schedule[lid]["scheduled_finish"] = pd.Timestamp(new_f)
                schedule[lid]["finish_ns"] = new_f
                n_pushed += 1
                pushed_hours.append((new_s - cur_s) / 3600.0 / NS)
        peak_after = {s: _peak_wip(full_lots[s], s) for s in gt_by_sku}
        # ---- report ----
        binders = [s for s in gt_by_sku if peak_before[s] > cap_by_sku[s] + 1e-6]
        still_over = [s for s in gt_by_sku if peak_after[s] > cap_by_sku[s] + 1e-6]
        max_ratio_s = max(gt_by_sku, key=lambda s: peak_after[s] / max(cap_by_sku[s], 1))
        print(f"\n  === S3 GREEN-TYRE WIP CAP (rope, cover={cover_h:.0f}h vs P1 press "
              f"consumption) ===")
        print(f"    SKUs with a cap        : {len(cap_by_sku):,}   GT lots checked: "
              f"{matched:,} of {n_gt_total:,}")
        print(f"    cap BOUND (pushed)     : {n_pushed:,} placements"
              + (f"   (max push +{max(pushed_hours):.1f} h)" if pushed_hours else ""))
        print(f"    unfixable (1 lot>cap)  : {unfixable:,} lots logged, left in place")
        print(f"    SKUs over cap BEFORE   : {len(binders):,} / {len(cap_by_sku):,}")
        print(f"    SKUs STILL over AFTER  : {len(still_over):,} / {len(cap_by_sku):,}"
              f"   (residual = lots bigger than a 24h cure of their SKU)")
        print(f"    tightest SKU AFTER     : {max_ratio_s}  peak {peak_after[max_ratio_s]:,.0f}"
              f" vs cap {cap_by_sku[max_ratio_s]:,}")
        # per-SKU ledger to disk
        _rows = [dict(sku=s, cap_tyres=cap_by_sku[s],
                      peak_wip_before=int(round(peak_before[s])),
                      peak_wip_after=int(round(peak_after[s])),
                      bound=(peak_before[s] > cap_by_sku[s] + 1e-6))
                 for s in sorted(gt_by_sku)]
        try:
            pd.DataFrame(_rows).sort_values("peak_wip_after", ascending=False).to_csv(
                OUTPUTS / _suff("phase5_gt_wip_cap.csv"), index=False)
            print(f"    {_suff('phase5_gt_wip_cap.csv')} (per-SKU cap vs peak WIP)")
        except Exception as _e:
            print(f"    [S3] ledger write skipped: {_e}")

    _enforce_gt_wip_cap()

    # === SHORT-LIFE COMPONENT RESYNC (post-building) ===
    # Short-shelf-life components (capstrip 24h, chafer 24h, beads 48h, …) were
    # placed front-loaded against the curing LFT, but building now runs all month,
    # so they age out before their tyre is built. Re-time each short-life component
    # lot LATER to track actual building consumption: spread an item's producer lots
    # across its consumers' timeline so each batch is consumed within its max-age.
    # SAFETY: these last-stage parts are made from long-shelf-life compounds, so a
    # producer lot is only moved as late as (a) its earliest served consumer minus
    # min-age and (b) the point where its OWN inputs would over-age — whichever is
    # tighter. We only ever move LATER (front-loaded -> JIT); never earlier.
    # ═══ CTP DEVIATION #11 — DAG-WIDE PRECEDENCE + AGING AUDIT ════════════════════════
    # The scheduler's ONLY existing aging report (`aging_v`) is written by ONE code path —
    # place_campaign() — so it covers belt campaigns and NOTHING ELSE. That is why phase5
    # can print `aging_violations: 0` while phase4 separately reports 221 EXPIRED. This
    # audit walks EVERY producer->consumer edge in the placed schedule and tells the truth.
    #
    # NEGATIVE GAP = consumer_start < producer_finish. That is a CAUSALITY VIOLATION: a lot
    # consuming material that does not exist yet. It is not a preference, it is not a soft
    # cost — it is a broken schedule. It is counted separately from TOO_FRESH for that
    # reason.
    def _edge_audit():
        """Returns a dict of audit numbers over the CURRENT schedule.
        Only edges where BOTH ends are placed can be evaluated.

        ═══ CTP DEVIATION #21 — E2: THE AUDIT USED TWO CONTRADICTORY MODELS ═════════════
        (A) DEFECT. This ONE function used to count EXPIRED with "every edge is required"
        and STARVATION with "same-item edges are ALTERNATIVES" — two mutually exclusive
        physics, 30 lines apart, and it reported both as fact. The alternatives model is
        the pooled-MHE model, which is dead (CTP #20: no MHE sub-lots on CTP, no carcass
        lots at all). THE AUDIT NOW USES ONE MODEL — the same quantity-summing one the
        placer executes. n_starved is computed by the SAME arithmetic as
        _material_ready_ns(), so the gate and the audit cannot disagree by construction.

        THE NUMBERS, and what each one honestly means:

        n_negative_gap   RAW EDGE COUNT: consumer_start < producer_finish. The right alarm
                         for the RESYNC, which moves one producer at a time and must never
                         land on top of a consumer.

        n_starved_groups (consumer, item) groups where the cumulative FINISHED quantity of
                         that item at the consumer's start did NOT cover what the consumer
                         consumes. THIS IS THE NUMBER THAT MEANS "MATERIAL DID NOT EXIST".
                         It MUST be 0. Under the old alternatives model this was
                         structurally incapable of being non-zero for any item with >1
                         producer — it was a decoration.
        n_starved_lots   distinct consumer lots affected.
        worst_frac       the worst material-present fraction among them (1.0 = fine).

        n_expired_edges  EDGE count where gap > max_age. !! FAN-OUT-INFLATED — see below.
        n_dead_lots      PRODUCER LOTS whose gap to their EARLIEST consumer already exceeds
                         max_age — i.e. the lot rots before ANYONE takes it.
                         THIS IS THE PHYSICAL NUMBER. See the report for why they differ.
        """
        _ne = _nneg = _nexp = _nfresh = 0
        _nneg_alloc = _nneg_zero = 0
        for _pp, _vs in successors.items():
            _sp = schedule.get(_pp)
            if _sp is None:
                continue
            _pfin = _sp["finish_ns"]
            _amn, _amx = _ages_ns(lot_item.get(_pp, ""))
            for _vv in _vs:
                _sv = schedule.get(_vv)
                if _sv is None:
                    continue
                _ne += 1
                _g = _sv["scheduled_start"].value - _pfin
                if _g < 0:
                    _nneg += 1              # raw: consumer starts before THIS producer ends
                    # CTP #33 — SPLIT IT. With per-edge quantities a NEGATIVE GAP on an edge
                    # carrying consume_qty == 0 is NOT a causality violation: that producer
                    # supplies this consumer with NOTHING (it exists only as a compatibility
                    # link from the phase3 fan-link, and its quantity went to a different
                    # consumer). Only a negative gap on an ALLOCATED edge is a real
                    # violation, and THAT is the number that must be 0.
                    if (not EDGE_QTY_ON) or edge_qty.get((_pp, _vv), 0.0) > 1e-6:
                        _nneg_alloc += 1
                    else:
                        _nneg_zero += 1
                elif _g < _amn:
                    _nfresh += 1            # too fresh (min-aging / transfer lag unmet)
                elif _amx > 0 and _g > _amx:
                    _nexp += 1              # expired on the rack (EDGE — inflated by fan-out)

        # ---- STARVATION: the QUANTITY-SUMMING model. Same arithmetic as the gate. ----
        _nstarved_groups = 0
        _starved_lots = set()
        _worst_frac = 1.0
        _starved_by_item = defaultdict(int)
        for _vv, _sv in schedule.items():
            _preds = predecessors.get(_vv, ())
            if not _preds:
                continue
            _cs = _sv["scheduled_start"].value
            _by_item = defaultdict(list)
            for _pp in _preds:
                if _pp in schedule:
                    _by_item[lot_item.get(_pp, "")].append(_pp)
            for _ip, _pl in _by_item.items():
                _need = 0.0
                _have = 0.0
                for _pp in _pl:
                    # CTP #33/#34 — THE AUDIT MUST USE THE GATE'S MODEL. This loop used
                    # `lot_qty` unconditionally; with edge_consume_qty ON that measures the
                    # schedule against a need the placer no longer believes (MEASURED 28.7x
                    # too large), and the audit reported 150 phantom starved groups on a
                    # schedule the gate had fully covered. Same arithmetic as
                    # _material_ready_ns() -> the two cannot disagree, as the docstring says.
                    _qp = (edge_qty.get((_pp, _vv), 0.0) if EDGE_QTY_ON
                           else float(lot_qty.get(_pp, 0.0) or 0.0))
                    _need += _qp
                    if schedule[_pp]["finish_ns"] <= _cs:
                        _have += _qp
                if open_alloc and (_vv, _ip) not in _OPEN_NETTED:
                    _need -= open_alloc.get((_vv, _ip), 0.0)   # stock on the floor at t=0
                if _need > 0 and _have < _need - 1e-6:
                    _nstarved_groups += 1
                    _starved_lots.add(_vv)
                    _starved_by_item[_ip] += 1
                    _f = _have / _need
                    if _f < _worst_frac:
                        _worst_frac = _f

        # ---- AGING, THE HONEST WAY: per PRODUCER LOT, not per EDGE. ----
        # A lot is DEAD only if it has already rotted by the time its FIRST consumer takes
        # it. Its 28th consumer being weeks away says nothing about the lot.
        _dead_lots = 0
        _lots_with_consumer = 0
        _by_itype = defaultdict(lambda: [0, 0, 0, 0])   # itype -> [lots, dead, edges, exp_edges]
        for _pp, _vs in successors.items():
            _sp = schedule.get(_pp)
            if _sp is None:
                continue
            # 0-QTY EDGE FILTER (2026-08-06): the PHYSICAL dead-lot number is "did this lot
            # rot before its REAL earliest consumer". A phantom 0-qty compatibility successor
            # scheduled early would SHRINK min(_starts) and MASK a lot that really rots before
            # its true (later) consumer -> under-count. Aim at real consumers only.
            _starts = [schedule[_v]["scheduled_start"].value for _v in _vs
                       if _v in schedule
                       and (edge_qty.get((_pp, _v), 0.0) if EDGE_QTY_ON
                            else float(lot_qty.get(_pp, 0.0) or 0.0)) > 0]
            if not _starts:
                continue
            _it = lot_item_type.get(_pp, "") or "?"
            _amn, _amx = _ages_ns(lot_item.get(_pp, ""))
            _lots_with_consumer += 1
            _row = _by_itype[_it]
            _row[0] += 1
            _pfin = _sp["finish_ns"]
            for _st in _starts:
                _row[2] += 1
                if _amx > 0 and (_st - _pfin) > _amx:
                    _row[3] += 1
            if _amx > 0 and (min(_starts) - _pfin) > _amx:
                _dead_lots += 1
                _row[1] += 1
        return {
            "edges": _ne, "neg": _nneg, "neg_alloc": _nneg_alloc, "neg_zero": _nneg_zero,
            "expired_edges": _nexp, "fresh": _nfresh,
            "starved_groups": _nstarved_groups, "starved_lots": len(_starved_lots),
            "worst_frac": _worst_frac, "starved_by_item": dict(_starved_by_item),
            "dead_lots": _dead_lots, "lots_with_consumer": _lots_with_consumer,
            "by_itype": {k: list(v) for k, v in _by_itype.items()},
        }

    def resync_short_life_components(short_life_h=72.0):
        # ═══ CTP DEVIATION #11 — THE RESYNC, REPAIRED IN PLACE (BOTH DEFECTS ARE (A)) ═══
        #
        # WHAT THE PASS IS FOR (and it is a legitimate v6 rule, so it STAYS ENABLED):
        #   a short-shelf-life component (cap strip 24 h, chafer 24 h, bead 48 h) placed
        #   front-loaded against the curing LFT will EXPIRE on the rack, because building
        #   now runs all month. Move the producer LATER, toward the moment it is actually
        #   consumed. Late-only ratchet, by design.
        #
        # THE TWO (A) DEFECTS — the code STATED these rules and FAILED TO EXECUTE them:
        #
        #  (A1) IT AIMED AT THE WRONG CONSUMER. The pass built ONE pooled, sorted list of
        #       the ITEM's consumer start-times and handed producer lot #i the i-th
        #       QUANTILE of it. It never once read `successors[_p]` — the consumers THAT
        #       LOT ACTUALLY FEEDS. With cap strip's consumers spanning 06-01 -> 07-05,
        #       producer lot #136 got aimed at a consumer in JULY and was ratcheted into
        #       July to serve a tyre it does not feed. The DAG knows who feeds whom; the
        #       pass refused to ask it.
        #       REPAIR: each lot now aims at min(start of its OWN scheduled successors).
        #       A lot with no scheduled successor is NOT MOVED AT ALL — there is nothing
        #       to resync it toward, and the pooled quantile was pure fabrication.
        #
        #  (A2) IT MOVED A PRODUCER AND NEVER RE-PROPAGATED. Moving a lot later WITHOUT
        #       re-checking its successors breaks the DAG's HARD PRECEDENCE INVARIANT.
        #       This is the root cause, and it was NEVER TOUCHED — the tell was that the
        #       earlier "fixed + LFT brake" attempt reduced impossible edges from 2,688 to
        #       2,688, i.e. BY ZERO. Fixing the AIM cannot fix precedence, because the
        #       violation is created AFTER the aim, by `first_fit_jit`: when the machine is
        #       congested, first-fit OVERSHOOTS the target and lands the producer's finish
        #       PAST its consumer's start. Nothing checked. 2,688 lots ate material that
        #       did not exist yet.
        #       REPAIR: a HARD SUCCESSOR CAP.
        #           succ_fin_cap = min(successor_start) - min_aging(item)
        #       The target is clamped to it, AND — because first_fit_jit may still overshoot
        #       when the machine is full — the RESULT is re-checked against it. If the move
        #       would land past the cap, THE MOVE IS NOT MADE: the lot is restored to its
        #       original slot, byte-for-byte.
        #       This makes a negative gap UNCONSTRUCTIBLE, which is strictly stronger than
        #       re-propagating after the fact: no successor ever needs to move, because no
        #       producer is ever allowed to land on top of one. Transitive re-propagation
        #       is therefore unnecessary BY CONSTRUCTION, not by omission. (Chains within
        #       the pass are safe too: every clamp reads the successor's CURRENT start, so a
        #       successor that has already been moved later simply gives its producer more
        #       room, and a producer moved first is merely conservative — never unsafe.)
        #
        # RETAINED FROM v6 / earlier work (all still (A)-legitimate brakes):
        #   - the LFT brake       : never ratchet a lot past its own deadline
        #   - the predecessor cap : never move so late that MY OWN inputs over-age
        #     (NOTE: an item with NO aging row is IMMORTAL — max_age = 365 d — which
        #      SILENTLY DISABLES this brake. See CTP DEVIATION #10. That is exactly how
        #      CAP 66-MOTHERROLL let cap strip drift 500 h.)
        #   - late-only          : never move a lot earlier
        #
        # SCOPE (v6): only items that (i) are short-life, (ii) are not building products,
        # and (iii) have at least one scheduled BUILDING consumer.
        #
        # ═══ CTP DEVIATION #22 — THE RESYNC IS A ONE-LOT RATCHET IN A MULTI-LEVEL CHAIN ══
        # (B) POLICY — a different SCOPE and a different ITERATION ORDER for a pass whose
        # per-lot rule is UNCHANGED. Default is FALSE = v6's exact behaviour, bit-for-bit.
        #
        # WHY THE PASS FAILS TODAY. MEASURED on the shipping schedule (1,502 dead lots —
        # lots whose gap to their FIRST consumer already exceeds their shelf life):
        #
        #     binding brake                       dead lots   share
        #     PRED OVER-AGE CAP                       847     56.4%
        #     LFT BRAKE                               374     24.9%
        #     machine congestion (genuinely stuck)    271     18.0%
        #     out of resync scope (max_age > 72 h)     10      0.7%
        #
        #  (1) THE PRED OVER-AGE CAP IS A CASCADE, NOT A CONSTRAINT. Of the 847 lots the
        #      pred cap holds, the PINNING PREDECESSOR is:
        #           CALANDARED ROLL        770
        #           FINAL COMPOUND          73
        #           Steel Belt Edge Strip    4
        #      NOT ONE OF THOSE THREE IS IN THE RESYNC'S OWN SCOPE — because the v6 scope
        #      gate demands the item feed BUILDING, and a calendered roll feeds a CUTTER.
        #      So the roll is never re-timed, it stays early, and its 72 h shelf life pins
        #      the belt cut that IS in scope. The belt cut then rots waiting for a green
        #      tyre. The pass is blocked BY THE LOT IT REFUSED TO LOOK AT.
        #      FIX: scope = "any short-life lot with a scheduled successor". The successor
        #      hard-cap below already guarantees precedence for a consumer OF ANY KIND, so
        #      the building gate buys no safety — it only blinds the pass.
        #
        #  (2) ONE PASS CANNOT RE-TIME A CHAIN. roll -> cut -> GT is mutually pinned: the
        #      cut cannot move until the roll moves (pred cap), and the roll cannot move
        #      past the cut (succ cap). A single sweep in arbitrary item order deadlocks.
        #      FIX: sweep in FORWARD TOPOLOGICAL order (the roll is re-timed BEFORE the cut,
        #      so the cut sees the RAISED pred cap in the SAME sweep), and repeat to a
        #      FIXPOINT. The ratchet is late-only and bounded above by the already-placed
        #      building lots, so it is monotone and MUST terminate.
        #
        #  (3) THE LFT BRAKE CLAMPS TO THE VERY NUMBER THAT IS WRONG. The lot's CPM LFT is
        #      computed by phase4 backward from the due date assuming INFINITE CAPACITY at
        #      building. Building is the constraint (51% of all machine-hours, 88.5% peak)
        #      and its lots slip PAST their CPM LST. So the feeder's LFT is a fiction, and
        #      braking on it stops the pass from chasing the consumer to where it ACTUALLY
        #      is. This file's own module constant already says RESPECT_LFT = False and the
        #      PLACER honours that (41.8% of lots finish after their LFT, by design). Only
        #      this repair pass treats the LFT as hard. FIX: make the brake obey
        #      RESPECT_LFT, like everything else in the file.
        #
        # WHAT THIS CANNOT DO, AND I AM NOT PRETENDING OTHERWISE: the pass runs AFTER
        # place_building_campaigns and only ever moves a NON-BUILDING producer LATER,
        # bounded by its already-placed consumer. Building does not move. Curing does not
        # move. THEREFORE THE MAKESPAN IS MATHEMATICALLY INVARIANT UNDER THIS PASS. It
        # cannot make the plan faster and it is not supposed to. What it does is stop the
        # plan from ordering 1,502 lots of material that will have rotted before anyone
        # picks it up.
        NS_H = 3600 * 1e9
        moved = held = 0
        blocked_precedence = 0   # moves REFUSED because they would have created a neg gap
        no_succ_skipped    = 0   # lots with no scheduled successor -> nothing to aim at
        lft_blocked        = 0   # NEW: v6 dropped these on the floor SILENTLY (bare `continue`)

        # DEFAULT FALSE = v6, bit-for-bit. Enable with `resync_chain_mode: true` in
        # config.yaml, or JK_RESYNC_CHAIN=1 in the environment (same pattern this file
        # already uses for JK_UPSTREAM_LEAD_H) so the A/B needs no config edit.
        CHAIN      = bool(cfg.get("resync_chain_mode",
                                  os.environ.get("JK_RESYNC_CHAIN", "0") == "1"))
        # 40 is a CAP, not a cost: the pass stops the moment a sweep moves nothing.
        # MEASURED on CTP: it reaches its fixpoint in 35 sweeps and costs +1 s of wall time.
        # (4 sweeps -> 21.6% dead | 8 -> 20.5% | 16 -> 19.1% | 35 = fixpoint -> 19.0%.)
        MAX_SWEEPS = int(cfg.get("resync_max_sweeps",
                                 os.environ.get("JK_RESYNC_SWEEPS", "40")))

        def _in_scope(_lid):
            if lot_item_type.get(_lid, "") in BUILDING_ITYPES:
                return False
            _mn, _mx = _ages_ns(lot_item.get(_lid, ""))
            return 0 < _mx <= short_life_h * NS_H

        def _try_move(_p):
            """One lot, one attempt. The PER-LOT RULE IS v6's, UNCHANGED."""
            nonlocal moved, held, blocked_precedence, no_succ_skipped, lft_blocked
            _mn, _mx = _ages_ns(lot_item.get(_p, ""))
            # (A1) AIM AT MY OWN SUCCESSORS — every one of them, not just the building
            # ones. A short-life lot can feed a cutter AND a builder; the EARLIEST
            # consumer of ANY kind is the one that binds.
            # ── 0-QTY EDGE FILTER (2026-08-06 BUGFIX, same class as the cascade) ──────
            # `successors` carries phase3's COMPATIBILITY edges (consume_qty==0 for the
            # unallocated compatible producers -- the B333ARS_L0007 -> B333_L0007 kind).
            # An UNFILTERED `min(successor starts)` can be PINNED by a 0-qty consumer that
            # happens to be scheduled EARLY, making `_tgt_start <= _old_start` so the lot is
            # judged "already on target" and is NEVER re-timed later toward its REAL (nonzero,
            # later) consumer -> it stays early and AGES. Aim only at REAL successors. Matches
            # the kit gate (~5044), the level-1 re-cut (~5295) and the cascade (~5407).
            _succ_starts = [schedule[_v]["scheduled_start"].value
                            for _v in successors.get(_p, ())
                            if _v in schedule
                            and (edge_qty.get((_p, _v), 0.0) if EDGE_QTY_ON
                                 else float(lot_qty.get(_p, 0.0) or 0.0)) > 0]
            if not _succ_starts:
                no_succ_skipped += 1
                return False                 # nothing to resync toward -> DO NOT MOVE
            _cstart = min(_succ_starts)      # serve the EARLIEST consumer I actually feed

            _e = schedule[_p]
            _old_start = _e["scheduled_start"].value
            _old_fin   = _e["finish_ns"]
            _dur       = _old_fin - _old_start
            _m         = _e["machine"]

            # (A2) THE HARD PRECEDENCE CAP. My finish may NEVER pass this.
            #      succ_fin_cap = earliest consumer start - my min_aging(+transfer lag)
            _succ_fin_cap = _cstart - _mn

            # target: finish exactly min_age before the consumer I serve
            _tgt_start = _succ_fin_cap - _dur
            _tgt_free  = _tgt_start          # target BEFORE the LFT brake (for the counter)

            # !! BRAKE: never ratchet a lot past its OWN deadline.
            # CTP #22(3): obey the file's own RESPECT_LFT. v6 mode keeps the brake.
            _lft = lft_map.get(_p)
            if _lft is not None and not pd.isna(_lft) and (RESPECT_LFT or not CHAIN):
                _tgt_start = min(_tgt_start, pd.Timestamp(_lft).value - _dur)

            if _tgt_start <= _old_start:
                # CTP #22: v6 `continue`d here with NO COUNTER. A lot the LFT brake pinned
                # was indistinguishable from a lot that was already on target. COUNT IT.
                if _tgt_free > _old_start:
                    lft_blocked += 1
                return False  # only ever move LATER toward consumption
            # !! BRAKE: this lot's OWN inputs must not over-age.
            _pred_cap = None
            for _pp in predecessors.get(_p, ()):
                if _pp not in schedule:
                    continue
                # 0-QTY EDGE FILTER (2026-08-06, same class): skip phase3 compatibility
                # producers this lot does NOT actually consume (consume_qty==0). Their
                # finish+max_age is a PHANTOM over-age cap that would pin this lot early and
                # age its OWN output. A 0-qty edge means no real input -> nothing to over-age.
                _pcq = (edge_qty.get((_pp, _p), 0.0) if EDGE_QTY_ON
                        else float(lot_qty.get(_pp, 0.0) or 0.0))
                if _pcq <= 0:
                    continue
                _pmn, _pmx = _ages_ns(lot_item.get(_pp, ""))
                _cap = schedule[_pp]["finish_ns"] + _pmx
                _pred_cap = _cap if _pred_cap is None else min(_pred_cap, _cap)
            if _pred_cap is not None:
                _tgt_start = min(_tgt_start, _pred_cap - _dur)
            if _tgt_start <= _old_start:
                held += 1
                return False
            # re-slot on the same machine: remove old interval, first-fit at target
            _busy = machine_busy.get(_m)
            if _busy is None:
                return False
            _j = bisect.bisect_left(_busy, (_old_start,))
            if _j < len(_busy) and _busy[_j][0] == _old_start:
                _occ = _busy[_j][1] - _busy[_j][0]   # keeps the changeover tail booked
                _busy.pop(_j)
                _popped = True
            else:
                _occ = _dur
                _popped = False
            if CHAIN:
                # CTP #22(4): ALAP. `_tgt_start` is the LATEST LEGAL START — it is already
                # the min of every brake (successor cap, pred over-age cap, and the LFT
                # brake when RESPECT_LFT). So the right question is "what is the latest
                # free slot AT OR BEFORE _tgt_start?", not "what is the first slot at or
                # after it?". first_fit_jit answers the second question, which is why v6
                # discards the move whenever the target slot is busy.
                _lf = latest_fit(_busy, _old_start, _tgt_start, _occ)
                if _lf is None or _lf <= _old_start:
                    if _popped:
                        bisect.insort(_busy, (_old_start, _old_start + _occ))
                    blocked_precedence += 1     # genuinely no free slot in the legal window
                    return False
                _new_start = _lf
            else:
                _new_start = first_fit_jit(_busy, _old_start, _tgt_start, _occ)
            _new_fin   = _new_start + _dur
            # (A2) RE-CHECK. first_fit_jit returns max(cursor, target) when the machine is
            # congested, so it CAN overshoot the clamp we just applied. This is the exact
            # line where v6 minted its 2,688 impossible edges. If the landing spot would
            # put my finish past a consumer's start, REFUSE THE MOVE and put the lot back
            # exactly where it was.
            # NOTE: in CHAIN mode this branch is unreachable by construction (latest_fit
            # returns s <= _tgt_start <= _succ_fin_cap - _dur). It is retained as an
            # ASSERTION, not as the safety argument — the safety argument is the bound on
            # latest_fit, and the independent proof is the negative-gap edge audit, which
            # must stay at 0.
            if _new_fin > _succ_fin_cap:
                if _popped:
                    bisect.insort(_busy, (_old_start, _old_start + _occ))
                blocked_precedence += 1
                return False
            bisect.insort(_busy, (_new_start, _new_start + _occ))
            _e["scheduled_start"]  = pd.Timestamp(_new_start)
            _e["scheduled_finish"] = pd.Timestamp(_new_fin)
            _e["finish_ns"]        = _new_fin
            if _new_start > _old_start:
                moved += 1
                return True
            return False

        _sweeps = 1
        if CHAIN:
            # CTP #22(1)+(2): every short-life lot with a successor, forward-topological,
            # to a fixpoint. `topo` already exists (it is the placement order) — a producer
            # precedes its consumer in it BY CONSTRUCTION, which is exactly what unlocks
            # the pred cap in the same sweep.
            order = [l for l in topo if l in schedule and _in_scope(l)]
            for _sw in range(MAX_SWEEPS):
                _before = moved
                held = blocked_precedence = no_succ_skipped = lft_blocked = 0
                for _p in order:
                    _try_move(_p)
                _sweeps = _sw + 1
                if moved == _before:
                    break        # fixpoint: nothing moved this sweep
            print(f"  SHORT-LIFE RESYNC [CTP #22 CHAIN MODE]: {len(order):,} lots in scope "
                  f"(v6's building-consumer gate lifted) | {_sweeps} sweep(s) to fixpoint "
                  f"| LFT brake {'ON' if RESPECT_LFT else 'OFF (RESPECT_LFT=False)'}")
        else:
            # v6: item-keyed, building-consumer gate, start-order within item, ONE pass.
            item_prod = defaultdict(list)
            for _lid in schedule:
                if _in_scope(_lid):
                    item_prod[lot_item.get(_lid, "")].append(_lid)
            for _it, _plots in item_prod.items():
                # v6 SCOPE GATE: this item is only resynced if it actually feeds building.
                _has_building_consumer = False
                for _p in _plots:
                    for _v in successors.get(_p, ()):
                        if _v in schedule and lot_item_type.get(_v, "") in BUILDING_ITYPES:
                            _has_building_consumer = True
                            break
                    if _has_building_consumer:
                        break
                if not _has_building_consumer:
                    continue
                for _p in sorted(_plots,
                                 key=lambda l: schedule[l]["scheduled_start"].value):
                    _try_move(_p)

        print(f"  SHORT-LIFE RESYNC: re-timed {moved:,} component lots toward consumption "
              f"| {held:,} held by input over-age limit "
              f"| {blocked_precedence:,} moves REFUSED (would have overrun a consumer) "
              f"| {lft_blocked:,} held by the LFT brake "
              f"| {no_succ_skipped:,} skipped (no scheduled consumer to aim at)"
              + (f"   [counters are for the LAST of {_sweeps} sweep(s)]" if CHAIN else ""))
        return moved

    # ═══ CTP DEVIATION #11 — THE SHORT-LIFE RESYNC IS ENABLED (v6's OWN DEFAULT) ══════
    # It was previously DISABLED behind `cfg.get("enable_short_life_resync", False)` with the
    # key ABSENT from config.yaml — so a v6 algorithm pass was switched off and INVISIBLE.
    # Under "the logic and algo must remain the same", switching a v6 pass off is an
    # UNAUTHORISED ALGORITHM CHANGE, and hiding the switch makes it worse. The default is
    # now True = v6's behaviour. The two (A) defects are repaired INSIDE the pass (above),
    # which is where they belonged all along.
    #
    # HISTORY (measured, and the reason the disable looked defensible):
    #                            EXPIRED        NEGATIVE-GAP        late      last finish
    #   v6 as-shipped         (47%)             2,688 (12.3%)      34.2%       12 Jul
    #   "fixed" + LFT brake   10,301 (47.3%)    2,688 (12.3%)      28.8%        6 Jul
    #   DISABLED              10,783 (49.5%)       62 (0.3%)       14.3%        5 Jul
    # THE TELL: 2,688 -> 2,688. The "fix" changed the impossible-edge count BY ZERO, which
    # proves it never touched the root cause. It fixed the AIM (A1) and left the missing
    # PRECEDENCE RE-CHECK (A2) untouched. The real (A) fix had never been tested. It is
    # tested now — every move is clamped by, and re-verified against, its own successors.
    _aud_pre = _edge_audit()
    _resync_on = bool(cfg.get("enable_short_life_resync", True))
    _resync_src = ("config.yaml:enable_short_life_resync" if "enable_short_life_resync" in cfg
                   else "default True (= v6's own behaviour)")
    if _resync_on:
        _n_moved = resync_short_life_components()
        _aud_post = _edge_audit()
        _neg_created = _aud_post["neg"] - _aud_pre["neg"]
        print(f"  SHORT-LIFE RESYNC: moved {_n_moved:,} lots "
              f"| negative-gap edges created: {_neg_created:+,} "
              f"| expired edges: {_aud_pre['expired_edges']:,} -> {_aud_post['expired_edges']:,}")
        print(f"     [enabled from {_resync_src}]  edges audited: {_aud_post['edges']:,} "
              f"| negative-gap total: {_aud_pre['neg']:,} -> {_aud_post['neg']:,} "
              f"| too-fresh: {_aud_pre['fresh']:,} -> {_aud_post['fresh']:,} "
              f"| STARVED groups: {_aud_pre['starved_groups']:,} -> {_aud_post['starved_groups']:,}")
        if _neg_created > 0:
            print(f"\n  !! ============================================================ !!")
            print(f"  !! THE RESYNC STILL CREATES {_neg_created:,} NEGATIVE-GAP EDGES.")
            print(f"  !! A negative gap is a CAUSALITY VIOLATION — a lot consuming material")
            print(f"  !! that does not exist yet. The successor hard-cap in this pass is")
            print(f"  !! supposed to make that UNCONSTRUCTIBLE, so if you are reading this")
            print(f"  !! line the cap has a hole in it. DO NOT SHIP THIS SCHEDULE.")
            print(f"  !! ============================================================ !!\n")
    else:
        _aud_post = _aud_pre
        print(f"  SHORT-LIFE RESYNC: DISABLED via config (enable_short_life_resync: false).")
        print(f"     !! This SWITCHES OFF A v6 ALGORITHM PASS. Short-life components "
              f"(cap strip 24 h, chafer 24 h, bead 48 h) will be left where the dispatcher "
              f"front-loaded them and will EXPIRE on the rack. Default is TRUE for a reason.")
    _E = max(_aud_post["edges"], 1)
    print(f"  PRECEDENCE AUDIT (all {_aud_post['edges']:,} placed DAG edges): "
          f"negative-gap {_aud_post['neg']:,} ({100*_aud_post['neg']/_E:.1f}%) "
          f"| too-fresh {_aud_post['fresh']:,} ({100*_aud_post['fresh']/_E:.1f}%)")
    if EDGE_QTY_ON:
        print(f"     of the {_aud_post['neg']:,} negative-gap edges, "
              f"{_aud_post['neg_alloc']:,} carry ALLOCATED quantity and "
              f"{_aud_post['neg_zero']:,} carry consume_qty = 0 (compatibility links that "
              f"supply this consumer with NOTHING — never a causality violation).")
        if _aud_post['neg_alloc'] and open_alloc:
            print(f"     an ALLOCATED negative gap is only a REAL violation if the consumer "
                  f"was also SHORT — phase3 allocates the GROSS need to producers and does "
                  f"NOT know about opening stock, so a consumer covered from the floor at "
                  f"t=0 can legitimately start before a producer that carries its "
                  f"allocation. THE COVERAGE TEST IS 'MATERIAL STARVATION' BELOW; that is "
                  f"the number that must be 0, and it is computed with the gate's own "
                  f"arithmetic (edge quantity + opening stock).")
        elif _aud_post['neg_alloc']:
            print(f"     !! {_aud_post['neg_alloc']:,} of them are on ALLOCATED edges with "
                  f"NO opening-stock cover in force — these are REAL causality violations.")

    # ═══ CTP DEVIATION #20 — MATERIAL STARVATION, MEASURED BY QUANTITY ════════════════
    # This line used to read "MATERIAL STARVATION (the number that matters): 0" and it was
    # a DECORATION: it was produced by the pooled-alternatives model, which is structurally
    # INCAPABLE of reporting a shortage for any item with more than one producer. It asked
    # "did ANY producer of this item finish?" — never "was there ENOUGH?".
    # It now asks the only question that matters, and it asks it in QUANTITY.
    _sg = _aud_post["starved_groups"]
    print(f"\n  MATERIAL STARVATION (quantity-summing — the number that matters): "
          f"{_sg:,} (consumer,item) group(s)")
    if _sg == 0:
        print(f"    ZERO lots start with incomplete material. Every consumer's CUMULATIVE "
              f"FINISHED-AND-AGED input quantity covers what it consumes, for every item.")
    else:
        print(f"    !! {_aud_post['starved_lots']:,} consumer lot(s) START WITH INCOMPLETE "
              f"MATERIAL. Worst: only {100*_aud_post['worst_frac']:.1f}% of the required "
              f"quantity was finished at the moment the lot began.")
        print(f"    !! THIS MUST BE 0. A non-zero here means the schedule builds tyres out "
              f"of material that has not been made. DO NOT SHIP IT.")
        for _i, _n in sorted(_aud_post["starved_by_item"].items(), key=lambda kv: -kv[1])[:8]:
            print(f"    !!   {_i:28s} {_n:5d} group(s)")

    # ═══ CTP DEVIATION #21 — E2: THE AGING REPORT. THE 46.4% WAS A FAN-OUT ARTEFACT ═══
    # (A) DEFECT, REPORTING ONLY. The old single number — "expired 10,122 (46.4%)" — counted
    # EDGES. An edge expires when the gap from the producer's finish to THAT consumer's
    # start exceeds max_age. But a Cap Strip lot feeds ~28 consumers spread across the
    # month. Its 28th CONSUMER is weeks away, so that EDGE scores "expired" — while THE LOT
    # ITSELF is consumed fresh (8.4 h) by its FIRST consumer and never rots at all.
    # The edge number is therefore INFLATED BY THE FAN-OUT and is not a physical quantity.
    #
    # MEASURED, and the two numbers do not even correlate:
    #     item              producers  fan-out   %EDGES exp   %LOTS dead before 1st consumer
    #     Cap Strip               136       28       68.9%        0.0%   <- pure artefact
    #     CALANDARED ROLL         101       30       57.0%        1.0%   <- pure artefact
    #     Steel Belt            1,804        1       53.5%       45.4%   <- REAL
    #     Ply                   1,231        1       53.8%       40.4%   <- REAL
    # A single number cannot say both things. BOTH ARE NOW REPORTED, LABELLED.
    _lc = max(_aud_post["lots_with_consumer"], 1)
    print(f"\n  AGING — TWO NUMBERS, BECAUSE THEY MEAN DIFFERENT THINGS:")
    print(f"    % of EDGES expired ........ {_aud_post['expired_edges']:,} / {_aud_post['edges']:,} "
          f"({100*_aud_post['expired_edges']/_E:.1f}%)   <- FAN-OUT-INFLATED. Reference only.")
    print(f"    % of LOTS dead before their FIRST consumer .. "
          f"{_aud_post['dead_lots']:,} / {_aud_post['lots_with_consumer']:,} "
          f"({100*_aud_post['dead_lots']/_lc:.1f}%)   <- THE PHYSICAL NUMBER.")
    print(f"    A lot that feeds 28 consumers scores 27 'expired' EDGES while being consumed "
          f"FRESH by its first. Only the LOT number says material actually rotted.")
    _rows = sorted(_aud_post["by_itype"].items(), key=lambda kv: -kv[1][0])[:10]
    if _rows:
        print(f"    {'item_type':26s} {'lots':>6s} {'fan-out':>8s} {'%EDGES exp':>11s} {'%LOTS dead':>11s}")
        for _it, (_nl, _nd, _nedg, _nexp_e) in _rows:
            _fo = _nedg / max(_nl, 1)
            print(f"    {_it[:26]:26s} {_nl:>6,d} {_fo:>8.1f} "
                  f"{100*_nexp_e/max(_nedg,1):>10.1f}% {100*_nd/max(_nl,1):>10.1f}%")

    _total = _t.time() - _t0
    if HOLIDAYS:
        print(f"  [BR-V6] holiday-aware placement: H1/H3 {_ha_stat['H1']:,} short-shelf lots moved to the "
              f"restart after a holiday; H2 {_ha_stat['H2']:,} long-shelf lots targeted before it")
    if NB_CO:
        print(f"  [BR-V4] neighbour-aware placement: {_nb_stat['lots']:,} upstream lots placed with the "
              f"changeover checked against both real neighbours ({_nb_stat['co_before_min']:,.0f} min of "
              f"changeover reserved before lots)")
    print(f"\n  Placed: {placed:,} / {len(topo):,}   "
          f"belt_campaigns: {placed_belt_campaigns}   "
          f"infeas: {len(infeas):,}   "
          f"aging_violations[CAMPAIGN PATH ONLY]: {len(aging_v):,}   "
          f"orphans_skipped: {n_orphans_skipped:,}   "
          f"orphans_FORCED: {n_orphans_forced:,}   "
          f"placement_time={_total:.1f}s")

    # ═══ CTP DEVIATION #15 — THE LFT CONTRADICTION, STATED HONESTLY ═══════════════════
    # (A) DEFECT, REPORTING ONLY. Nothing about placement changes here.
    #
    # THE CONTRADICTION: phase5 printed `aging_violations: 0` and looked clean — while
    # phase4 separately reported 221 EXPIRED and 41.8% of lots actually finished AFTER
    # their LFT. Two numbers from the same pipeline that cannot both be true, and NEITHER
    # surfaced as a failure. Two separate causes, both now named:
    #   1. `aging_v` is appended in ONE function — place_campaign(). It has never covered
    #      the per-lot JIT path or the building-campaign path. It is a BELT-CAMPAIGN
    #      counter wearing the name of a schedule-wide one. It is now LABELLED as such,
    #      and the DAG-wide audit printed above is the real number.
    #   2. RESPECT_LFT = False (top of file). Phase 5 NEVER treats the CPM's LFT as a
    #      constraint. That is a DELIBERATE v6 POLICY and it is LEFT ALONE — late lots are
    #      PLACED LATE, not dropped. But a policy that is never measured is indistinguish-
    #      able from a bug. So we measure it.
    #
    # "Either respect it or stop pretending." We do not respect it (v6's choice, unchanged)
    # — so we stop pretending.
    try:
        _late_h = []
        _n_with_lft = 0
        for _lid, _rec in schedule.items():
            _l = lft_map.get(_lid)
            if _l is None or pd.isna(_l):
                continue
            _n_with_lft += 1
            _d = (_rec["finish_ns"] - pd.Timestamp(_l).value) / 3600 / NS
            if _d > 0:
                _late_h.append(_d)
        if _n_with_lft:
            _n_late = len(_late_h)
            _pct = 100.0 * _n_late / _n_with_lft
            if _late_h:
                _srt = sorted(_late_h)
                _med = _srt[len(_srt) // 2]
                _worst = _srt[-1]
            else:
                _med = _worst = 0.0
            print(f"\n  LOTS PAST LFT: {_n_late:,} of {_n_with_lft:,} ({_pct:.1f}%) "
                  f"| median {_med:.1f} h late | worst {_worst:.1f} h")
            print(f"    (RESPECT_LFT is False by design — these are NOT dropped, they are "
                  f"placed late. This is v6 policy, left unchanged; it is now VISIBLE.)")
        else:
            print("\n  LOTS PAST LFT: no lot carries an LFT — cannot measure lateness.")
    except Exception as _le:
        print(f"  [WARN] LFT lateness report failed: {_le}")

    # ═══ CTP DEVIATION #10 — report every item that had NO aging row ══════════════════
    # (A) DEFECT, REPORTING ONLY. DEFAULT_MAX_AGE_H = 365 * 24 = 8,760 h. An item MISSING
    # from the aging master does not fail, does not warn — it becomes IMMORTAL.
    #
    # !! THIS MATTERS MORE NOW, NOT LESS. The short-life resync (CTP #11) is ENABLED again.
    # Its ONLY brake against moving a lot too late is the PREDECESSOR MAX-AGE CAP
    #     _pred_cap = predecessor_finish + predecessor_max_age
    # An IMMORTAL predecessor makes that cap effectively +infinity, so the brake SILENTLY
    # DOES NOT EXIST for any lot fed by an immortal item. That is precisely how
    # CAP 66-MOTHERROLL (no aging row -> 8,760 h) let cap strip drift 500 h and put a JUNE
    # plan's slitter on 12 JULY, while the machine sat HALF IDLE.
    if _no_aging_row:
        _uniq = sorted(set(_no_aging_row))
        _prod = [i for i in _uniq if i in set(lots["item_code"].astype(str).str.strip())]
        print(f"\n  !! ================== IMMORTAL ITEMS (NO AGING ROW) ================== !!")
        print(f"  !! {len(_uniq):,} item(s) have NO row in the aging master. v6 silently gives "
              f"them DEFAULT_MAX_AGE_H = {DEFAULT_MAX_AGE_H/24:.0f} DAYS ({DEFAULT_MAX_AGE_H:,.0f} h) "
              f"— i.e. they become IMMORTAL.")
        print(f"  !! An absent row is the SAME CLASS OF BUG as a blank cell: it is 'no "
              f"information', and it must be LOUD, never silently generous.")
        if _prod:
            print(f"  !! {len(_prod)} of them are PRODUCED and therefore SCHEDULED:")
            for i in _prod[:12]:
                print(f"  !!    {i:26s} itype={item_itype.get(i,'<untyped>')}")
            if len(_prod) > 12:
                print(f"  !!    ... and {len(_prod)-12} more")
            print(f"  !! CONSEQUENCE (now that the resync is ENABLED again — CTP #11):")
            print(f"  !!   an immortal item CANNOT EXPIRE, so it DISABLES the predecessor "
                  f"max-age brake")
            print(f"  !!   inside resync_short_life_components(). Every lot fed by one of "
                  f"these items has")
            print(f"  !!   NO upper bound from its own inputs — the only thing still holding "
                  f"it back is its")
            print(f"  !!   LFT and the successor precedence cap. THE AGING BRAKE IS NOT THERE.")
            print(f"  !! FIX: add them to `aging_fallback_by_itemtype` in config.yaml, or to "
                  f"the aging master.")
            print(f"  !!      DO NOT LEAVE THEM IMMORTAL.")
        print(f"  !! ==================================================================== !!")

    # ═══ CTP DEVIATION #8 — report the forced orphans LOUDLY ══════════════════════════
    # These lots could not reach a terminal in the DAG (phase3's F1 aging filter severed
    # their edge), so v6 would have SILENTLY DELETED them. They carry real demand, so we
    # placed them instead. But the severed edge is a REAL SIGNAL — it says the item's shelf
    # life is too short for the lot-sizing bucket it landed in. Never let that go unseen.
    if forced_orphans:
        _fq = {}
        for _l in forced_orphans:
            _it = lot_item.get(_l, "?")
            _e = _fq.setdefault(_it, [0, 0.0])
            _e[0] += 1
            _e[1] += float(lot_qty.get(_l, 0) or 0)
        print(f"\n  !! {n_orphans_forced:,} lot(s) could NOT reach a terminal in the DAG "
              f"(phase3's F1 aging filter severed their edge).")
        print(f"     v6 would have SILENTLY DELETED them. They carry REAL DEMAND, so they "
              f"were PLACED instead (CTP deviation #8):")
        for _it, (_n, _q) in sorted(_fq.items(), key=lambda kv: -kv[1][1]):
            print(f"       {_it:26s} {_n:4d} lots   {_q:>15,.0f} qty")
        print(f"     WHY: the item's max_aging is shorter than the span its lot bucket "
              f"covers, so no consumer sits inside the aging window. The work is real and "
              f"must be produced — but the plant will have to run it tight against the "
              f"shelf life. Check these on the floor.")

    sched_df = pd.DataFrame([{k:v for k,v in s.items() if k != "finish_ns"}
                              for s in schedule.values()])
    # === ATOMIC WRITE (prevents OneDrive sync truncation) ===
    # OneDrive can leave the file half-written if pandas.to_csv is interrupted
    # during sync, causing 40-50% of rows to be silently lost. Write to a tmp
    # file first, then atomic-rename to the final path.
    _sched_final = OUTPUTS / _suff("phase5_schedule.csv")
    _sched_tmp   = OUTPUTS / (".tmp_" + _sched_final.name)
    try:
        sched_df.to_csv(_sched_tmp, index=False)
        if _sched_final.exists():
            try: _sched_final.unlink()
            except Exception: pass
        os.replace(_sched_tmp, _sched_final)
        print(f"    {_sched_final.name} ({len(sched_df):,})")
    finally:
        if _sched_tmp.exists():
            try: _sched_tmp.unlink()
            except Exception: pass

    if not sched_df.empty:
        camp = sched_df[sched_df["campaign_id"].astype(str) != ""]
        if not camp.empty:
            camp_summary = camp.groupby(["wire_type","campaign_id","machine"]).agg(
                n_lots=("lot_id","count"),
                start=("scheduled_start","min"),
                finish=("scheduled_finish","max"),
                items=("item", lambda s: ", ".join(sorted(set(s)))),
            ).reset_index().sort_values(["machine","start"])
            camp_summary["duration_h"] = ((camp_summary["finish"] - camp_summary["start"]).dt.total_seconds()/3600).round(2)
            camp_summary.to_csv(OUTPUTS/_suff("phase5_campaign_schedule.csv"), index=False)

    # ═══ CTP DEVIATION #16 — utilisation_% DIVIDES BY ITSELF ══════════════════════════
    # (A) DEFECT, REPORTING ONLY. No placement changes.
    #
    #     utilisation_% = busy_ns / span_ns,  where span = last_finish - first_start
    #
    # span is derived FROM the busy intervals. For a machine that never idles between its
    # first and last lot, span == busy — so the metric divides by itself and prints ~100%.
    # It is MEANINGLESS ON 28 OF 28 MACHINES. TBM 3407 reported 100.0% while its TRUE load
    # was 674.7 / 720 = 93.7%. A metric that reads 100% for every machine, busy or idle,
    # tells the planner NOTHING and hides the very headroom he is looking for.
    #
    # TRUE utilisation = busy_hours / HORIZON_hours, where the horizon is the PLAN's own
    # window (plan.startTime.min() -> plan.endTime.max()). That is the denominator the
    # plant actually cares about: "how much of the month is this machine working?"
    #
    # The old column is KEPT UNTOUCHED so nothing downstream breaks. The correct one is
    # ADDED alongside it, and it is honest enough to print >100% when a machine is
    # scheduled past the plan horizon (which is itself a finding, not an error).
    _hz_start_ns = T0_ns
    if HORIZON_LIMIT_NS is not None and HORIZON_LIMIT_NS > _hz_start_ns:
        _hz_end_ns = HORIZON_LIMIT_NS
        _hz_src = "plan.endTime.max()"
    elif schedule:
        _hz_end_ns = max(r["finish_ns"] for r in schedule.values())
        _hz_src = "last scheduled finish (no plan horizon available)"
    else:
        _hz_end_ns = _hz_start_ns + int(24 * NS_PER_H)
        _hz_src = "fallback 24 h"
    _horizon_h = max(1e-9, (_hz_end_ns - _hz_start_ns) / 3600 / NS)
    print(f"\n  TRUE-UTILISATION HORIZON: {pd.Timestamp(_hz_start_ns)} -> "
          f"{pd.Timestamp(_hz_end_ns)}  = {_horizon_h:.1f} h  [{_hz_src}]")
    util = []
    for m, intervals in machine_busy.items():
        busy_ns = sum((e - s) for s,e in intervals)
        if intervals:
            span_ns = intervals[-1][1] - intervals[0][0]
            util.append({
                "machine": m, "n_lots": len(intervals),
                "busy_hours": round(busy_ns / 3600 / NS, 2),
                "span_hours": round(span_ns / 3600 / NS, 2),
                # KEPT (broken, but downstream may depend on it): busy / SPAN.
                "utilisation_%": round(busy_ns / max(span_ns,1) * 100, 1),
                # ADDED (the correct one): busy / HORIZON.
                "true_utilisation_%": round((busy_ns / 3600 / NS) / _horizon_h * 100, 1),
                "horizon_hours": round(_horizon_h, 2),
                "first_start": pd.Timestamp(intervals[0][0]),
                "last_finish": pd.Timestamp(intervals[-1][1]),
            })
    util_df = pd.DataFrame(util).sort_values("busy_hours", ascending=False) if util else pd.DataFrame()
    if not util_df.empty:
        _tu = util_df["true_utilisation_%"]
        print(f"    TRUE utilisation across {len(util_df)} machines: "
              f"max {_tu.max():.1f}% | median {_tu.median():.1f}% | min {_tu.min():.1f}% "
              f"| machines over 100% of horizon: {int((_tu > 100).sum())}")
        print(f"    (the legacy `utilisation_%` column is busy/SPAN — it divides by itself "
              f"and reads ~100% for every busy machine. Use `true_utilisation_%`.)")
    if not util_df.empty:
        util_df.to_csv(OUTPUTS/_suff("phase5_machine_utilization.csv"), index=False)
        # === DB output: jkt_machine_utilization_updated ===
        try:
            import sys as _sys
            _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
            from db_loader import write_to_db, _resolve_source
            if _resolve_source(cfg) == "db":
                res = write_to_db(util_df, output_key="machine_utilization", cfg=cfg)
                print(f"    DB write: {res['table']} ({res['rows_written']:,} rows, "
                      f"{'CREATED' if res['created_or_replaced'] else 'TRUNCATED+INSERT'})")
        except Exception as _de:
            print(f"    DB write skipped (machine_utilization): {_de}")

    if infeas:
        pd.DataFrame(infeas).to_csv(OUTPUTS/_suff("phase5_infeasibilities.csv"), index=False)
    if aging_v:
        pd.DataFrame(aging_v).to_csv(OUTPUTS/_suff("phase5_aging_violations.csv"), index=False)

    # ===================================================================
    # PLANT CAP WIP AUDIT - Carcass + Green Tyre rolling inventory
    # ===================================================================
    try:
        TBM_S1 = {"7801","7802","7803","7804","7701","8101","7601",
                  "8001","8002","8003","6909","6911","6801","6802","6803"}
        TBM_S2 = {"8301","8302","8501","8502","8201","7301"}
        wip_audit_rows = []
        if sched_df is not None and not sched_df.empty:
            sd = sched_df.copy()
            sd["scheduled_start"] = pd.to_datetime(sd["scheduled_start"])
            sd["scheduled_finish"] = pd.to_datetime(sd["scheduled_finish"])
            try:
                lot_qty_map = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
            except Exception:
                lot_qty_map = {}
            sd["qty"] = sd["lot_id"].map(lot_qty_map).fillna(0)
            s1 = sd[sd["machine"].isin(TBM_S1)]
            s2 = sd[sd["machine"].isin(TBM_S2)]
            evt = []
            for _, r in s1.iterrows():
                evt.append((r["scheduled_finish"], +float(r["qty"]), "CARCASS_MADE"))
            for _, r in s2.iterrows():
                evt.append((r["scheduled_start"], -float(r["qty"]), "CARCASS_CONSUMED"))
            for _, r in s2.iterrows():
                evt.append((r["scheduled_finish"], +float(r["qty"]), "GT_MADE"))
            evt.sort(key=lambda x: x[0])
            carcass_wip = 0.0
            gt_wip = 0.0
            carcass_peak = 0.0
            carcass_min = 0.0
            gt_peak = 0.0
            for ts, delta, kind in evt:
                if kind == "CARCASS_MADE":
                    carcass_wip += delta
                elif kind == "CARCASS_CONSUMED":
                    carcass_wip += delta
                elif kind == "GT_MADE":
                    gt_wip += delta
                if carcass_wip > carcass_peak: carcass_peak = carcass_wip
                if carcass_wip < carcass_min:  carcass_min = carcass_wip
                if gt_wip > gt_peak:           gt_peak = gt_wip
            if evt:
                t0 = evt[0][0]; tN = evt[-1][0]
                days = pd.date_range(t0.floor("D"), tN.ceil("D"), freq="D")
                evt_idx = 0
                carcass = 0.0; gt = 0.0
                for d in days:
                    while evt_idx < len(evt) and evt[evt_idx][0] < d:
                        _, delta, kind = evt[evt_idx]
                        if kind == "CARCASS_MADE" or kind == "CARCASS_CONSUMED":
                            carcass += delta
                        elif kind == "GT_MADE":
                            gt += delta
                        evt_idx += 1
                    wip_audit_rows.append({
                        "date": str(d.date()),
                        "carcass_wip_eod": int(carcass),
                        "carcass_cap_violation": carcass > 4000,
                        "carcass_negative":     carcass < 0,
                        "green_tyre_cumulative": int(gt),
                    })
            print(f"\n  === CARCASS + GT WIP AUDIT ===")
            print(f"    Carcass WIP range: min={int(carcass_min):,} max={int(carcass_peak):,}  (plant cap 4,000)")
            print(f"    Carcass cap violations: {sum(1 for r in wip_audit_rows if r['carcass_cap_violation'])} days")
            print(f"    Carcass negative days:  {sum(1 for r in wip_audit_rows if r['carcass_negative'])} days")
            print(f"    Total Green Tyres made: {int(gt_peak):,}")
            if wip_audit_rows:
                pd.DataFrame(wip_audit_rows).to_csv(OUTPUTS/_suff("phase5_wip_carcass_gt.csv"), index=False)
                print(f"    {_suff('phase5_wip_carcass_gt.csv')} (refreshed)")
    except Exception as _e:
        print(f"    WIP audit skipped: {_e}")

    # ===================================================================
    # PER-DAY GREEN TYRE PRODUCTION (plant-day = 7 AM -> next 7 AM)
    # Green Tyre output = TBM Stage-2 + BJ GROUP + VMIMAXX + UNISTAGE
    #
    # PRORATED across plant-day boundary:
    # If a lot starts at 06:30 and finishes at 07:30 producing 60 tyres,
    # the model splits production by overlap:
    #   - 06:30 → 07:00 (30 min) → previous plant-day's bucket: 30 tyres
    #   - 07:00 → 07:30 (30 min) → next plant-day's bucket: 30 tyres
    # ===================================================================
    gt_daily_rows = []
    try:
        TBM_S2_GT  = {"8301","8302","8501","8502","8201","7301"}
        BJ_GT      = {"7101","7102","7103","7104","7105","7106","7201"}
        VMI_GT     = {"6001","6002","6003","6004","7001","7002","7003","7004"}
        UNI_GT     = {"7501","7502","7503"}
        ALL_GT_MACHINES = TBM_S2_GT | BJ_GT | VMI_GT | UNI_GT
        if sched_df is None or sched_df.empty:
            print(f"    GT daily: sched_df empty, skipping.")
        else:
            sd = sched_df.copy()
            sd["scheduled_start"]  = pd.to_datetime(sd["scheduled_start"])
            sd["scheduled_finish"] = pd.to_datetime(sd["scheduled_finish"])
            try:
                lot_qty_map = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
            except Exception:
                lot_qty_map = {}
            sd["qty"] = sd["lot_id"].map(lot_qty_map).fillna(0)
            gt_sched = sd[sd["machine"].isin(ALL_GT_MACHINES)].copy()
            print(f"    GT daily: {len(gt_sched):,} GT lots across {len(ALL_GT_MACHINES)} machines")
            if not gt_sched.empty:
                def _mg_label(m):
                    if m in TBM_S2_GT: return "TBM Stage-2"
                    if m in BJ_GT:     return "BJ GROUP"
                    if m in VMI_GT:    return "VMIMAXX GROUP"
                    if m in UNI_GT:    return "UNISTAGE GROUP"
                    return "OTHER"
                gt_sched["mg"] = gt_sched["machine"].apply(_mg_label)

                # === PRORATED BY 7 AM BOUNDARY ===
                # For each lot, walk through every plant-day window that the lot
                # overlaps. Allocate qty proportional to time overlap.
                # plant-day D starts at D 07:00 and ends at (D+1) 07:00.
                pd_bucket = {}  # (plant_day_date, mg) -> qty (float)

                lot_starts  = gt_sched["scheduled_start"].values
                lot_finishes = gt_sched["scheduled_finish"].values
                lot_qtys    = gt_sched["qty"].values.astype(float)
                lot_mgs     = gt_sched["mg"].values

                ONE_DAY = pd.Timedelta(days=1)
                SHIFT_H = pd.Timedelta(hours=7)

                for i in range(len(gt_sched)):
                    s = pd.Timestamp(lot_starts[i])
                    f = pd.Timestamp(lot_finishes[i])
                    q = lot_qtys[i]
                    mg = lot_mgs[i]
                    if q <= 0 or f <= s:
                        continue
                    dur_ns = (f - s).value
                    # First plant-day boundary at or before lot start:
                    # plant_day D starts at (D + 7h). For lot starting at 06:30,
                    # plant_day = (start - 7h).normalize() = previous day.
                    pd_start_of_s = (s - SHIFT_H).normalize() + SHIFT_H
                    # Walk forward through plant-day windows
                    cur_start = max(s, pd_start_of_s)
                    cur_pd    = pd_start_of_s
                    while cur_start < f:
                        cur_end = cur_pd + ONE_DAY
                        seg_end = min(f, cur_end)
                        seg_dur_ns = (seg_end - cur_start).value
                        prorated = q * (seg_dur_ns / dur_ns)
                        plant_day_date = (cur_pd - SHIFT_H).normalize().date() if cur_pd.hour >= 7 else cur_pd.normalize().date()
                        # Simpler: plant_day_date = (cur_pd - 7h).normalize().date()
                        plant_day_date = (cur_pd - SHIFT_H).date()
                        key = (plant_day_date, mg)
                        pd_bucket[key] = pd_bucket.get(key, 0.0) + prorated
                        # advance
                        cur_start = cur_end
                        cur_pd = cur_pd + ONE_DAY

                # Build aggregated DataFrame
                if pd_bucket:
                    rows_long = []
                    for (pd_date, mg), qty in pd_bucket.items():
                        rows_long.append({"plant_day": pd_date, "mg": mg, "qty": qty})
                    long_df = pd.DataFrame(rows_long)
                    agg = (long_df.groupby(["plant_day","mg"])["qty"].sum()
                                  .unstack(fill_value=0).reset_index())
                    for col in ["TBM Stage-2","BJ GROUP","VMIMAXX GROUP","UNISTAGE GROUP"]:
                        if col not in agg.columns:
                            agg[col] = 0
                    agg["TOTAL_GT"] = (agg["TBM Stage-2"] + agg["BJ GROUP"]
                                       + agg["VMIMAXX GROUP"] + agg["UNISTAGE GROUP"])
                    agg["window_start_07AM"] = pd.to_datetime(agg["plant_day"]) + pd.Timedelta(hours=7)
                    agg["window_end_07AM_next"] = pd.to_datetime(agg["plant_day"]) + pd.Timedelta(days=1, hours=7)
                    cols_order = ["plant_day","window_start_07AM","window_end_07AM_next",
                                  "TBM Stage-2","BJ GROUP","VMIMAXX GROUP","UNISTAGE GROUP","TOTAL_GT"]
                    agg = agg[cols_order].sort_values("plant_day").reset_index(drop=True)
                    for c in ["TBM Stage-2","BJ GROUP","VMIMAXX GROUP","UNISTAGE GROUP","TOTAL_GT"]:
                        agg[c] = agg[c].round(0).astype(int)
                    gt_daily_rows = agg.to_dict("records")
                    # === ATOMIC WRITE for GT CSV (prevents OneDrive truncation) ===
                    csv_out = OUTPUTS/_suff("phase5_gt_daily_production.csv")
                    csv_tmp = OUTPUTS/(".tmp_" + csv_out.name)
                    try:
                        agg.to_csv(csv_tmp, index=False)
                        if csv_out.exists():
                            try: csv_out.unlink()
                            except Exception: pass
                        os.replace(csv_tmp, csv_out)
                    finally:
                        if csv_tmp.exists():
                            try: csv_tmp.unlink()
                            except Exception: pass
                    print(f"\n  === PER-DAY GREEN TYRE PRODUCTION (plant-day 7 AM -> 7 AM, prorated) ===")
                    print(f"    {'plant_day':12s} {'TBM_S2':>8s} {'BJ':>8s} {'VMI':>8s} {'UNI':>8s} {'TOTAL':>8s}")
                    for r in gt_daily_rows:
                        print(f"    {str(r['plant_day']):12s} {r['TBM Stage-2']:>8d} {r['BJ GROUP']:>8d} "
                              f"{r['VMIMAXX GROUP']:>8d} {r['UNISTAGE GROUP']:>8d} {r['TOTAL_GT']:>8d}")
                    tot = agg['TOTAL_GT'].sum()
                    print(f"    {'TOTAL':12s} {'':>8s} {'':>8s} {'':>8s} {'':>8s} {int(tot):>8d}")
                    print(f"    {csv_out.name} (written)")
    except Exception as _e:
        import traceback
        print(f"    GT daily audit FAILED: {_e}")
        traceback.print_exc()

    # ===================================================================
    # PLANT-FLOOR SHIFT-WISE SCHEDULE (publishable)
    # COVERS THE FULL PIPELINE: from FINAL MIXING -> ... -> GREEN TYRE.
    # Every lot from sched_df is included (mixers, calenders, FRC, extruders,
    # cutters, building - all of them).
    #
    # Columns: date | machine | shift | item | item_type | process |
    #          start_time | end_time | produce_qty | UOM | lot_id |
    #          Consumption_lot_id (";"-joined component lots from BOM recipe, wave-matched) |
    #          FG_SKU_CODE | FG_DESCRIPTION (Green Tyres / Carcass only, from BOM12JUNE; FormulaCode as-is)
    # Shifts: A 07:00-15:00, B 15:00-23:00, C 23:00-07:00(next day)
    # Lots spanning shift boundary are SPLIT into multiple rows
    # with prorated qty.
    # ===================================================================
    shift_rows = []
    try:
        if sched_df is not None and not sched_df.empty:
            sd_shift = sched_df.copy()
            sd_shift["scheduled_start"]  = pd.to_datetime(sd_shift["scheduled_start"])
            sd_shift["scheduled_finish"] = pd.to_datetime(sd_shift["scheduled_finish"])
            try:
                lot_qty_map   = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
                lot_uom_map   = dict(zip(lots["lot_id"], lots["lot_uom"].astype(str).fillna("")))
                lot_itype_map = dict(zip(lots["lot_id"], lots["item_type"].astype(str).fillna("")))
            except Exception:
                lot_qty_map   = {}
                lot_uom_map   = {}
                lot_itype_map = {}
            # === FG_SKU_CODE / FG_DESCRIPTION (Green Tyres + Carcass only) ===
            # From BOM12JUNE.xlsx: filter MaterialCode_O == produced item code,
            # take the LATEST Revision, then PLCBOMName -> FG_SKU_CODE and
            # FormulaCode -> FG_DESCRIPTION (pasted as-is, no stripping).
            fg_skucode_map, fg_desc_map = {}, {}
            FG_ITYPES = {"Green Tyres", "Carcass"}
            try:
                _bom_fg = pd.read_excel(
                    INPUTS / "BOM12JUNE.xlsx", sheet_name="sheet1",
                    usecols=["MaterialCode_O", "Revision", "PLCBOMName", "FormulaCode"])
                _bom_fg["MaterialCode_O"] = _bom_fg["MaterialCode_O"].astype(str).str.strip()
                _bom_fg["Revision"] = pd.to_numeric(_bom_fg["Revision"], errors="coerce").fillna(-1)
                # latest revision per material code
                _bom_fg = (_bom_fg.sort_values("Revision")
                                  .drop_duplicates("MaterialCode_O", keep="last"))
                fg_skucode_map = dict(zip(_bom_fg["MaterialCode_O"],
                                          _bom_fg["PLCBOMName"].astype(str).fillna("")))
                fg_desc_map = dict(zip(_bom_fg["MaterialCode_O"],
                                       _bom_fg["FormulaCode"].astype(str).fillna("")))
                print(f"  FG MAP (BOM12JUNE): {len(fg_skucode_map):,} material codes loaded")
            except Exception as _fge:
                print(f"  FG MAP load skipped ({_fge})")
            # === Consumption_lot_id: from the PHASE-3 DAG (actual scheduled links) ===
            # phase3_dag_construction builds the real producer_lot -> consumer_lot
            # edges the scheduler used (BOM + block alignment + aging). So a lot's
            # consumed components ARE its DAG predecessors. We read the raw
            # phase3_dag_edges (the full DAG, before the WIP-edge filter) so the
            # mapping matches Phase 3 exactly. A producer that wasn't separately
            # scheduled (covered by WIP) is tagged "WIP:<item>" so the row is never
            # blank; a lot with no DAG predecessors (raw-material leaf) shows
            # "RAW_MATERIAL".
            _dag_preds = defaultdict(list)    # consumer_lot -> [producer_lot]
            try:
                _de = _load_csv(["phase3_dag_edges.csv.gz", "phase3_dag_edges.csv"])
                for _p, _c in zip(_de["producer_lot"], _de["consumer_lot"]):
                    _dag_preds[_c].append(_p)
                _placed_set = set(schedule.keys())
                _kit_cache = {}
                def _consumption_kit(lid):
                    cached = _kit_cache.get(lid)
                    if cached is not None:
                        return cached
                    out, seen = [], set()
                    for p in _dag_preds.get(lid, ()):
                        if p == lid or p in seen:
                            continue
                        seen.add(p)
                        if p in _placed_set:
                            out.append(p)                       # real scheduled producer lot
                        else:
                            out.append(f"WIP:{lot_item.get(p, p)}")  # producer from inventory (not scheduled)
                    if not out:
                        out = ["RAW_MATERIAL"]
                    _kit_cache[lid] = out
                    return out
                print(f"  CONSUMPTION (Phase-3 DAG): {len(_dag_preds):,} consumer lots "
                      f"with predecessors, {len(_de):,} edges")
            except Exception as _ce:
                def _consumption_kit(lid):
                    return []
                print(f"  CONSUMPTION DAG build skipped ({_ce})")

            # ── BUILDING-ECHO REPAIR (CTP #44, 2026-08-04) ───────────────────────────
            # THE DEFECT: the building-plan echo (_revise_building_schedule_echo)
            # replaces the phase-2 green-tyre lots with its own synthetic blocks
            # (lot_id "BLD00024"). Those ids are NOT nodes in the phase-3 DAG, so
            # _consumption_kit() above finds no predecessors for them and EVERY
            # building row falls through to the "RAW_MATERIAL" default -- which is
            # false. Building consumes Steel Belt, Ply, Bead Apex, Inner Liner,
            # SideWall, Tread and Cap Strip lots, all of them scheduled in this very
            # file. It was silent: BUILDING was simply 100% "RAW_MATERIAL".
            #
            # THE REPAIR: bind each BLD block back to a real phase-2 GT lot (in
            # build-time order, by cumulative quantity within its GT code), take THAT
            # lot's DAG predecessors as the kit, and for each component ITEM name the
            # lot that has already FINISHED by the block's start (FEFO). If nothing of
            # that item has finished yet the component came from opening WIP, so it is
            # named "WIP:<item>" -- a row is never blank and can never read as
            # consume-before-make. Same logic as the [build-trace] block in
            # append_all_changeover.py, but computed in-process from `schedule`.
            _bld_kit = {}                       # BLD lot -> [component lot, ...]
            try:
                _gt_rows = lots[lots["item_type"].astype(str)
                                    .str.contains("Green", case=False, na=False)]
                _code_lots = defaultdict(list)  # GT code -> [(phase2 lot, qty)] in lot order
                for _r in _gt_rows.sort_values("lot_id").itertuples():
                    _l2 = str(_r.lot_id)
                    if _l2.startswith("BLD"):
                        continue                # an echo block, not a phase-2 lot
                    _code_lots[str(_r.item_code).strip()].append(
                        (_l2, float(getattr(_r, "lot_qty", 0) or 0)))
                # per GT code, per component ITEM, that item's lots sorted by FINISH
                _fin = {_l: _d.get("scheduled_finish") for _l, _d in schedule.items()}
                _code_item = defaultdict(lambda: defaultdict(list))
                for _code, _gts in _code_lots.items():
                    for _gl, _ in _gts:
                        for _cl in _dag_preds.get(_gl, ()):
                            _f = _fin.get(_cl)
                            if _f is not None and pd.notna(_f):
                                _code_item[_code][_cl.split("_L")[0]].append(
                                    (pd.Timestamp(_f).value, _cl))
                for _code in _code_item:
                    for _itm in _code_item[_code]:
                        _code_item[_code][_itm].sort()
                # the echo's blocks, in build order within each GT code
                _blocks = defaultdict(list)     # GT code -> [(start_ns, BLD lot, qty)]
                for _l, _d in schedule.items():
                    if not str(_l).startswith("BLD"):
                        continue
                    _blocks[str(_d.get("item", "")).strip()].append(
                        (pd.Timestamp(_d["scheduled_start"]).value, str(_l),
                         float(lot_qty_map.get(_l, 0) or 0)))
                _n_bound = _n_wip = 0
                for _code, _bl in _blocks.items():
                    _p2 = _code_lots.get(_code, [])
                    if not _p2:
                        continue                # SKU has no phase-2 GT lot (data gap)
                    _bl.sort()
                    _tot_lot = sum(_q for _, _q in _p2) or 1.0
                    _tot_bld = sum(_q for _, _, _q in _bl) or 1.0
                    _bounds, _cc = [], 0.0
                    for _gl, _q in _p2:         # cumulative-fraction boundary per lot
                        _cc += _q
                        _bounds.append((_gl, _cc / _tot_lot))
                    _items = _code_item.get(_code, {})
                    _all_items = set(_items)    # every component item this GT needs,
                    for _gl, _ in _p2:          # even if none of its lots is scheduled
                        for _cl in _dag_preds.get(_gl, ()):
                            _all_items.add(_cl.split("_L")[0])
                    _cum = 0.0
                    for _sns, _l, _q in _bl:
                        _cum += _q
                        _frac = (_cum - _q / 2.0) / _tot_bld   # block midpoint as a fraction
                        _gl = _p2[-1][0]
                        for _cand, _bf in _bounds:
                            if _frac <= _bf:
                                _gl = _cand
                                break
                        _parts = []
                        for _itm in sorted(_all_items):
                            _ll = _items.get(_itm, ())
                            _k = (bisect.bisect_right([_f for _f, _ in _ll], _sns) - 1) if _ll else -1
                            if _k >= 0:
                                _parts.append(_ll[_k][1])      # FEFO-current, already finished
                            else:
                                _parts.append(f"WIP:{_itm}")   # fed from opening WIP
                                _n_wip += 1
                        _bld_kit[_l] = _parts or ["RAW_MATERIAL"]
                        _n_bound += 1
                if _bld_kit:
                    _base_kit = _consumption_kit
                    def _consumption_kit(lid, _base=_base_kit, _bk=_bld_kit):
                        _k = _bk.get(lid)
                        return _k if _k is not None else _base(lid)
                print(f"  CONSUMPTION (building echo): bound {_n_bound:,} BLD blocks to their "
                      f"phase-2 GT lot + FEFO component lots ({_n_wip:,} kit lines from opening WIP)")
            except Exception as _be:
                print(f"  CONSUMPTION building-echo repair SKIPPED ({_be}) "
                      f"-- BUILDING rows will fall back to RAW_MATERIAL")

            sd_shift["qty"]       = sd_shift["lot_id"].map(lot_qty_map).fillna(0)
            sd_shift["uom"]       = sd_shift["lot_id"].map(lot_uom_map).fillna("")
            sd_shift["item_type"] = sd_shift["lot_id"].map(lot_itype_map).fillna("")

            # Helper: given a timestamp T, return (plant_date, shift_letter, shift_start, shift_end)
            # A: 07:00 -> 15:00 (plant_date = T.date())
            # B: 15:00 -> 23:00 (plant_date = T.date())
            # C: 23:00 -> 07:00 (plant_date = T.date() if T.hour >= 23 else T.date() - 1 day)
            def _shift_of(ts):
                h = ts.hour + ts.minute/60.0 + ts.second/3600.0
                d = ts.normalize()
                if 7 <= h < 15:
                    return d.date(), "A", d + pd.Timedelta(hours=7),  d + pd.Timedelta(hours=15)
                if 15 <= h < 23:
                    return d.date(), "B", d + pd.Timedelta(hours=15), d + pd.Timedelta(hours=23)
                # C-shift: h >= 23 or h < 7
                if h >= 23:
                    return d.date(), "C", d + pd.Timedelta(hours=23), d + pd.Timedelta(days=1, hours=7)
                # h < 7 -> previous day's C-shift
                prev = d - pd.Timedelta(days=1)
                return prev.date(), "C", prev + pd.Timedelta(hours=23), prev + pd.Timedelta(days=1, hours=7)

            ZERO_NS = 0
            # UOMs that must be INTEGER (whole units)
            NOS_UOMS = {"NOS", "NO", "NUMBERS", "EA", "EACH", "PCS", "PIECES"}
            # BR-V1 (2026-10-01): mixing rows carry WHOLE batches of the (compound, mixer) batch size.
            _mixbm5, _MIXT5, _v1 = None, set(), {"lots": 0, "rounded": 0, "kg_delta": 0.0}
            if str((cfg.get("business_rules") or {}).get("mixing_output_uom", "KG")).upper() == "BATCH":
                try:
                    sys.path.insert(0, str(ROOT))
                    from mixing_batches import BatchMap as _BM5, MIX_ITYPES as _MIXT5
                    _mixbm5 = _BM5((cfg.get("business_rules") or {}).get("mixing_batch_source"), root=str(ROOT))
                except Exception as _e:
                    print(f"  [BR-V1] !! batch map unavailable ({type(_e).__name__}: {_e}) -- mixing rows stay kg")
            for _, lot in sd_shift.iterrows():
                lid    = lot["lot_id"]
                item   = lot["item"]
                itype  = str(lot.get("item_type", "")) or ""
                mach   = lot["machine"]
                process = str(lot.get("operation", "")) or ""
                dept   = str(lot.get("department", "")) or ""
                # === Consumption_lot_id: which component lots feed THIS lot ===
                # predecessors[lid] are the producer lots (belt, cap strip, ply,
                # carcass, compounds, ...) that this lot consumes — i.e. its full
                # kit. Lets the user trace which lots were consumed to make this one.
                cons_lots = _consumption_kit(lid)   # Phase-3 DAG predecessors
                consumption_lot_id = ";".join(str(p) for p in cons_lots)
                # FG SKU code + description (only for Green Tyres / Carcass)
                if itype in FG_ITYPES:
                    _ic = str(item).strip()
                    fg_sku_code   = fg_skucode_map.get(_ic, "")
                    fg_description = fg_desc_map.get(_ic, "")
                else:
                    fg_sku_code, fg_description = "", ""
                qty    = float(lot["qty"])
                uom    = str(lot["uom"]).strip()
                s      = pd.Timestamp(lot["scheduled_start"])
                f      = pd.Timestamp(lot["scheduled_finish"])
                if pd.isna(s) or pd.isna(f) or f <= s:
                    continue
                dur_ns = (f - s).value
                # === STEP 1: Collect all shift segments for this lot ===
                lot_segs = []
                cur = s
                while cur < f:
                    pd_date, shift_letter, sh_start, sh_end = _shift_of(cur)
                    seg_start = max(cur, sh_start)
                    seg_end   = min(f, sh_end)
                    if seg_end <= seg_start:
                        cur = sh_end
                        continue
                    seg_dur_ns = (seg_end - seg_start).value
                    prorated_qty = qty * (seg_dur_ns / dur_ns) if dur_ns > 0 else 0.0
                    lot_segs.append({
                        "pd_date": pd_date, "shift": shift_letter,
                        "seg_start": seg_start, "seg_end": seg_end,
                        "prorated": prorated_qty,
                    })
                    cur = sh_end
                if not lot_segs:
                    continue
                # === STEP 2: For NOS UOMs, apply LARGEST-REMAINDER ROUNDING ===
                # so segment qtys are integers AND their sum exactly equals lot qty.
                is_nos = uom.upper() in NOS_UOMS
                _bs5 = (_mixbm5.size(item, mach) if (_mixbm5 is not None and itype.upper() in _MIXT5)
                        else None)
                if _bs5:
                    # BR-V1: whole batches, largest remainder -> the rows sum to the lot's batches
                    _lb = qty / _bs5
                    total_b = int(round(_lb)) if abs(_lb - round(_lb)) < 1e-3 else math.ceil(_lb)
                    _v1["lots"] += 1
                    if total_b * _bs5 - qty > 1e-3:
                        _v1["rounded"] += 1; _v1["kg_delta"] += total_b * _bs5 - qty
                    pb = [seg["prorated"] / _bs5 for seg in lot_segs]
                    fl = [int(math.floor(x + 1e-9)) for x in pb]
                    order = sorted(range(len(lot_segs)), key=lambda i: -(pb[i] - fl[i]))
                    for k in range(max(0, min(total_b - sum(fl), len(lot_segs)))):
                        fl[order[k]] += 1
                    for i, seg in enumerate(lot_segs):
                        seg["batches"] = fl[i]
                        seg["final_qty"] = round(fl[i] * _bs5, 3)
                    lot_segs = [seg for seg in lot_segs if seg["batches"] > 0] or lot_segs[:1]
                elif is_nos:
                    total_int = int(round(qty))
                    floors  = [int(seg["prorated"]) for seg in lot_segs]
                    sum_fl  = sum(floors)
                    residual = total_int - sum_fl
                    # Sort by remainder DESC, distribute residual to highest remainders
                    idx_remainder = sorted(
                        range(len(lot_segs)),
                        key=lambda i: -(lot_segs[i]["prorated"] - floors[i])
                    )
                    final_qtys = list(floors)
                    for k in range(max(0, min(residual, len(lot_segs)))):
                        final_qtys[idx_remainder[k]] += 1
                    for i, seg in enumerate(lot_segs):
                        seg["final_qty"] = final_qtys[i]
                else:
                    for seg in lot_segs:
                        seg["final_qty"] = round(seg["prorated"], 3)
                # === STEP 3: Append to shift_rows ===
                for seg in lot_segs:
                    shift_rows.append({
                        "date":         str(seg["pd_date"]),
                        "machine":      mach,
                        "shift":        seg["shift"],
                        "item":         item,
                        "item_type":    itype,
                        "process":      process,
                        "department":   dept,
                        "start_time":   seg["seg_start"].strftime("%Y-%m-%d %H:%M:%S"),
                        "end_time":     seg["seg_end"].strftime("%Y-%m-%d %H:%M:%S"),
                        "produce_qty":  seg["final_qty"],
                        "UOM":          uom,
                        "lot_id":       lid,
                        "prod_id":      f"P{len(shift_rows):06d}",   # unique production-row id (traceability)
                        "consumption_id": consumption_lot_id,        # component lots consumed (from phase-3 DAG)
                        "FG_SKU_CODE":  fg_sku_code,
                        "FG_DESCRIPTION": fg_description,
                        "batches":       seg.get("batches"),
                        "batch_size_kg": _bs5 if _bs5 else None,
                    })

            if _mixbm5 is not None:
                print(f"  [BR-V1] mixing shift rows in whole batches: {_v1['lots']:,} mixing lots; "
                      f"{_v1['rounded']} placed lots were not whole batches and were rounded up "
                      f"(+{_v1['kg_delta']:,.1f} kg)")
            if shift_rows:
                shift_df = pd.DataFrame(shift_rows)
                # Keep lot_id + prod_id + consumption_id + FG_SKU_CODE for traceability; drop only desc.
                shift_df = shift_df.drop(columns=["FG_DESCRIPTION"], errors="ignore")
                shift_df = shift_df.sort_values(
                    ["date","machine","shift","start_time"]).reset_index(drop=True)
                # Snap out shift-split render overlaps on a machine (raw schedule is overlap-free): if a
                # production segment starts before the previous one ends on the same machine, push its
                # start to that end. Cosmetic -- the authoritative times live in phase5_schedule.
                try:
                    _sd = shift_df
                    _sd["_s"] = pd.to_datetime(_sd["start_time"], errors="coerce")
                    _sd["_e"] = pd.to_datetime(_sd["end_time"], errors="coerce")
                    _sd = _sd.sort_values(["machine", "_s"]).reset_index(drop=True)
                    _prev_e = {}
                    for _j in _sd.index:
                        _m = _sd.at[_j, "machine"]; _s = _sd.at[_j, "_s"]; _e = _sd.at[_j, "_e"]
                        _pe = _prev_e.get(_m)
                        if _pe is not None and pd.notna(_s) and _s < _pe:
                            _sd.at[_j, "start_time"] = _pe.strftime("%Y-%m-%d %H:%M:%S")
                            if pd.notna(_e) and _e < _pe:
                                _sd.at[_j, "end_time"] = _pe.strftime("%Y-%m-%d %H:%M:%S"); _e = _pe
                        _prev_e[_m] = max(_pe, _e) if (_pe is not None and pd.notna(_e)) else (_e if pd.notna(_e) else _pe)
                    shift_df = _sd.drop(columns=["_s", "_e"]).sort_values(
                        ["date", "machine", "shift", "start_time"]).reset_index(drop=True)
                except Exception:
                    pass
                # === ATOMIC WRITE ===
                csv_path = OUTPUTS / _suff("phase5_floor_schedule.csv")
                csv_tmp  = OUTPUTS / (".tmp_" + csv_path.name)
                try:
                    shift_df.to_csv(csv_tmp, index=False)
                    if csv_path.exists():
                        try: csv_path.unlink()
                        except Exception: pass
                    os.replace(csv_tmp, csv_path)
                    print(f"\n  === FLOOR SCHEDULE (shift-wise, publishable) ===")
                    print(f"    Total shift-rows: {len(shift_df):,}")
                    print(f"    Distinct lots:    {shift_df['lot_id'].nunique():,}")
                    print(f"    Distinct items:   {shift_df['item'].nunique():,}")
                    print(f"    Distinct item_types: {shift_df['item_type'].nunique():,}")
                    print(f"    Distinct machines: {shift_df['machine'].nunique():,}")
                    print(f"    Shifts: A 07-15, B 15-23, C 23-07")
                    # === DB output: jkt_floor_endfwd_schedule ===
                    try:
                        import sys as _sys
                        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
                        from db_loader import write_to_db, _resolve_source
                        if _resolve_source(cfg) == "db":
                            res = write_to_db(shift_df, output_key="floor_schedule", cfg=cfg)
                            print(f"    DB write: {res['table']} ({res['rows_written']:,} rows, "
                                  f"{'CREATED' if res['created_or_replaced'] else 'TRUNCATED+INSERT'})")
                    except Exception as _de:
                        print(f"    DB write skipped (floor_schedule): {_de}")
                    # Show item_type coverage (proves end-to-end)
                    itype_counts = shift_df.groupby('item_type').size().sort_values(ascending=False)
                    print(f"    Item-types covered (end-to-end from FINAL MIXING -> GREEN TYRE):")
                    for it, n in itype_counts.head(20).items():
                        print(f"      {str(it)[:35]:35s} : {n:>6,d} shift-rows")
                    print(f"    {csv_path.name} (written)")
                except Exception as _ce:
                    print(f"    floor schedule CSV write FAILED: {_ce}")
                finally:
                    if csv_tmp.exists():
                        try: csv_tmp.unlink()
                        except Exception: pass
    except Exception as _e:
        import traceback
        print(f"    Floor schedule generation FAILED: {_e}")
        traceback.print_exc()

    # ===================================================================
    # SUMMARY XLSX
    # ===================================================================
    try:
        sched_for_sum = sched_df.copy()
        if not sched_for_sum.empty:
            sched_for_sum["scheduled_start"]  = pd.to_datetime(sched_for_sum["scheduled_start"])
            sched_for_sum["scheduled_finish"] = pd.to_datetime(sched_for_sum["scheduled_finish"])
            plan_end = pd.Timestamp(str(horizon)) if horizon is not None else None
            past_h = (sched_for_sum["scheduled_finish"] > plan_end).sum() if plan_end is not None else 0
            overview_rows = [
                ("Total lots placed",        f"{len(sched_for_sum):,}"),
                ("Belt campaigns placed",    f"{placed_belt_campaigns:,}"),
                ("Orphans skipped",          f"{n_orphans_skipped:,}"),
                ("Infeasibilities",          f"{len(infeas):,}"),
                ("Aging violations",         f"{len(aging_v):,}"),
                ("Schedule earliest start",  str(sched_for_sum["scheduled_start"].min())),
                ("Schedule latest finish",   str(sched_for_sum["scheduled_finish"].max())),
                ("Plan horizon (endTime max)", str(plan_end) if plan_end is not None else "(none)"),
                ("Lots PAST_HORIZON",        f"{int(past_h):,}"),
                ("Placement time (s)",       f"{_total:.1f}"),
            ]
            overview = pd.DataFrame(overview_rows, columns=["metric","value"])
            by_dept = (sched_for_sum.groupby("department")
                         .agg(n_lots=("lot_id","count"),
                              first_start=("scheduled_start","min"),
                              last_finish=("scheduled_finish","max"))
                         .reset_index().sort_values("n_lots", ascending=False))
            by_machine = util_df.copy() if not util_df.empty else pd.DataFrame()
            aging_break = pd.DataFrame()
            if aging_v:
                av = pd.DataFrame(aging_v)
                if "excess_h" in av.columns:
                    aging_break = (av.groupby(["item","type"])
                                     .agg(n=("producer_lot","count"),
                                          avg_excess_h=("excess_h","mean"))
                                     .reset_index().sort_values("n", ascending=False))
            past_horizon_break = pd.DataFrame()
            if plan_end is not None:
                ph = sched_for_sum[sched_for_sum["scheduled_finish"] > plan_end]
                if not ph.empty:
                    past_horizon_break = (ph.groupby(["department","machine"])
                                            .size().reset_index(name="n_past_horizon")
                                            .sort_values("n_past_horizon", ascending=False))
            wip_sheet = pd.DataFrame(wip_audit_rows) if wip_audit_rows else pd.DataFrame()
            gt_daily_sheet = pd.DataFrame(gt_daily_rows) if gt_daily_rows else pd.DataFrame()
            floor_sheet = pd.DataFrame(shift_rows) if shift_rows else pd.DataFrame()
            if len(floor_sheet) > 900000:
                floor_sheet = floor_sheet.head(900000)
            sheets = {
                "00_overview": overview,
                "01_by_department": by_dept,
                "03_aging_violations_summary": aging_break,
                "04_past_horizon_breakdown": past_horizon_break,
                "05_carcass_gt_wip_daily": wip_sheet,
                "06_GT_daily_production": gt_daily_sheet,
                "07_floor_schedule_shiftwise": floor_sheet,
            }
            # === ATOMIC WRITE for SUMMARY XLSX ===
            out_xlsx = OUTPUTS/_suff("phase5_summary.xlsx")
            tmp_xlsx = OUTPUTS/(".tmp_" + out_xlsx.name)
            try:
                with pd.ExcelWriter(tmp_xlsx, engine="openpyxl") as w:
                    for name, sh in sheets.items():
                        if isinstance(sh, pd.DataFrame) and not sh.empty:
                            sh.to_excel(w, sheet_name=name[:31], index=False)
                        else:
                            pd.DataFrame({"info": ["(no data)"]}).to_excel(w, sheet_name=name[:31], index=False)
                if out_xlsx.exists():
                    try: out_xlsx.unlink()
                    except Exception: pass
                os.replace(tmp_xlsx, out_xlsx)
                print(f"    {_suff('phase5_summary.xlsx')} (refreshed)")
            finally:
                try:
                    if tmp_xlsx.exists():
                        tmp_xlsx.unlink()
                except Exception:
                    pass
    except Exception as _se:
        print(f"  [WARN] summary xlsx write skipped: {_se}")

    return 0


if __name__ == '__main__':
    import sys as _sys
    import traceback as _tb
    try:
        _rc = main()
        raise SystemExit(_rc if _rc is not None else 0)
    except SystemExit:
        raise
    except Exception:
        _sys.stderr.write(chr(10) + '!! PHASE CRASHED !!' + chr(10))
        _sys.stderr.flush()
        _tb.print_exc()
        _sys.exit(1)
