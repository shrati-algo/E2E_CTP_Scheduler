#!/usr/bin/env python3
"""
phase2_lot_sizing.py — WAVE-AWARE lot sizing (V6_wave).

NEW APPROACH (vs old span_h sizer):
  1. Each curing block is tagged with a wave_id (Phase 1.5).
  2. For each item I:
       max_carry_waves = floor(max_aging[I] / wave_duration_days)
       Walk demand wave-by-wave, group consecutive waves up to
       (max_carry_waves + 1) waves where the resulting span ≤ max_aging.
       Each group becomes ONE lot (subject to MPQ + lot-size cap).
  3. MPQ floor applied (overproduce surplus when raw < mpq_min).
  4. LOT-SIZE CAP: if lot duration > max_lot_duration_h, split into N sub-lots
     of ≤ max_lot_duration_h each. Sub-lots share first/last_need.
  5. FRC special sizer (BELT items): wire campaigns per wave (8000 m mandatory).
     NON-BELT FRC items (Ply, Cap-strip carrier, etc.) sized as regular items —
     they will be placed during 6h+ cool-down windows by Phase 5.

OUTPUT
  outputs2/phase2_lots_updated.csv
  outputs2/phase2_lot_blocks_updated.csv.gz
  outputs2/phase2_lot_skus_updated.csv
  outputs2/phase2_lots_updated.xlsx
"""
from __future__ import annotations
import sys, pathlib, yaml, math, re, os
import pandas as pd
from collections import defaultdict
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT/"inputs"
OUTPUTS = ROOT/"outputs2"           # NEW WRITE LOCATION
OUTPUTS.mkdir(parents=True, exist_ok=True)
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

# --- staging helpers ---
def _ensure_writable(p):
    """OneDrive sync conflicts can leave a directory at a file's path (with the
    actual file trapped inside). If that's the case, nuke the directory before
    pandas writes — otherwise to_csv silently writes to the wrong path and
    downstream phases read stale data from a previous run."""
    p = pathlib.Path(p)
    if p.is_dir():
        print(f"  WARNING: {p} exists as a directory (OneDrive sync conflict). "
              f"Removing before write.")
        shutil.rmtree(p, ignore_errors=True)


def _stage_csv(df, name, big_threshold_rows=200000):
    is_big = len(df) > big_threshold_rows
    write_name = _suff(name + ".gz" if is_big else name)
    p = OUTPUTS / write_name
    # ── STALE-SIBLING GUARD (2026-07-29) ─────────────────────────────────────────────────────
    # This writes EITHER `_updated.csv` OR `_updated.csv.gz` depending on ROW COUNT, and every
    # downstream reader tries the .gz FIRST. If a PRIOR run landed on the other side of the
    # threshold (e.g. July had 394k blocks -> .gz; June has 112k -> .csv), the stale variant would
    # SHADOW this fresh file and the whole run reads a DIFFERENT MONTH silently. Delete the sibling
    # so exactly one variant exists on disk. (This bit the June btoc run: 88d phantom makespan.)
    sibling = OUTPUTS / _suff(name if is_big else name + ".gz")
    if sibling != p and sibling.exists():
        try:
            sibling.unlink()
        except Exception:
            pass
    _ensure_writable(p)
    if is_big:
        # Use compression level 1 (fastest) to avoid timeouts on big block files
        df.to_csv(p, index=False, compression={"method":"gzip","compresslevel":1})
    else:
        df.to_csv(p, index=False)
    return p


def _stage_xlsx_sheets(sheets, name):
    p = OUTPUTS / _suff(name)
    _ensure_writable(p)
    with pd.ExcelWriter(p, engine="openpyxl") as w:
        for k, df in sheets.items():
            clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
            df.to_excel(w, sheet_name=clean, index=False)
    return p


def _db_or_csv(_k, _cfg=None):
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        from db_loader import load_input
        return load_input(_k, _cfg)
    except Exception:
        _fp = (_cfg or {}).get('files', {})
        return _read(INPUTS / _fp[_k])


def _read(p):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(p, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(p)


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


def build_itype_map(it):
    return {str(r["ItemCode"]).strip(): str(r["ItemType"]).strip()
            for _, r in it.iterrows() if pd.notna(r.get("ItemCode"))}


def _parse_mpq(s):
    """Robustly turn the plant's free-text 'Minimum Run Qty' into ONE numeric floor.
    The MPQ master (MOQ *.xlsx) is human-authored and carries several non-numeric formats;
    plain float() drops every one of them to None (no floor -> silent under-flooring). This
    parses them EXPLICITLY so they can never silently vanish -- and so they do not pop up as
    a parsing surprise again:

      "400"                     -> 400.0            (plain number)
      "310, 206"  (Tread)       -> 206.0            (size-dependent: 12-14in=310 / 15-20in=206;
                                                     we use the SMALLEST as the universal floor so
                                                     no tread size is forced to over-produce. Plant
                                                     note preserved in config.yaml -> mpq_notes.)
      "9*1800"    (Cap Strip)   -> 16200.0          (N*M product)
      "Spool length (300)"      -> 300.0            (bracketed number)
      "3 Batches" / "Trial"     -> None             (no piece floor; compounds are sized by the
                                                     Banbury batch_size in routing, not by MPQ)
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.upper() in ("NAN", "NONE", "TRIAL", ""):
        return None
    if "batch" in s.lower():                              # "3 Batches" -> compound, batch-sized
        return None                                        #   (NOT a 3-piece floor)
    try:
        return float(s)                                   # plain number
    except ValueError:
        pass
    m = re.fullmatch(r'\s*([\d.]+)\s*\*\s*([\d.]+)\s*', s)  # N*M  (e.g. Cap Strip "9*1800")
    if m:
        return float(m.group(1)) * float(m.group(2))
    m = re.search(r'\(\s*([\d.]+)\s*\)', s)                # "(300)"  (Spool length)
    if m:
        return float(m.group(1))
    nums = re.findall(r'[\d.]+', s)                        # comma multi-value (Tread) -> smallest
    if nums:
        return min(float(n) for n in nums)
    return None                                            # text like "3 Batches" -> no floor


# ── LINE-SPECIFIC MPQ state (set in main when mpq_tbr_from_db is on) ─────────────────────
_MPQ_TBR = None            # DB TBR type (upper) -> (min, max, uom, False)
_ITEM_TT = {}              # item -> PCR / TBR / BOTH (CTP_Master_BOM.tyre_type)
_MPQ_UNIT_SKIPS = []       # (item, itype, floor, mpq_uom, item_uom) floors refused by the unit guard
_MPQ_ITEM = None           # v26: item -> (mpq_min, uom) from inputs/mpq_item.csv (build_mpq_items.py)
_MPQ_POOL = {"on": False, "max_h": 96.0, "merged": 0}   # v26: pool small same-item lots up to MPQ
_FRC = {}                  # v27: calender item -> spool / dip length D (CTP_Master_FRC), made at once
_FRC_STAT = {"runs": 0, "res_closed": 0}
_FRC_CLS = {}              # v44: calender item -> steel / fabric (CTP_Master_FRC item_type)
_FRC_SPAN = {}             # v98: class -> max hours of demand one calender lot pools (fourrc_rules.pool_span_h)
_DEEP = {"on": False, "h48": 48.0, "h72": 72.0, "classes": {"fabric"}, "lots": 0, "rolls": 0, "ext72": 0,
         "mode": "48_72", "aging_won": 0}
# v47 (user 2026-10-08): a compound run makes AT ONCE at least the next cover_h (15 h) of demand -- cover_h_long (24 h)
# for IL3 / IL5 and the 4RC compounds -- so the same compound is not repeated within that time. Butyl-friendly
# compounds (exempt list) are not grouped. The window is cut to the shelf life when that is shorter.
_MIXCW = {"on": False, "w": 15.0, "w_long": 24.0, "long": set(), "exempt": set(), "margin_h": 2.0,
          "lots": 0, "items": 0, "short": set()}


def _fourrc_compounds():
    """compounds that feed the 4-roll calender: BOM children of the CTP_Master_FRC items (inputs/frc_lengths.csv)."""
    try:
        fl = pd.read_csv(INPUTS / "frc_lengths.csv")
        b = pd.read_csv(INPUTS / "bom.csv", dtype=str, low_memory=False)
        par = set(fl["item"].astype(str).str.strip())
        return set(b.loc[b["Parent"].astype(str).str.strip().isin(par), "child"].astype(str).str.strip())
    except Exception as _e:
        print(f"  [WARN] v47 4RC compound list: {type(_e).__name__}: {_e}")
        return set()
# our item type -> the plant's TBR MPQ row
_TBR_TYPE_MAP = {"BEAD APEX": "BEAD APEX", "BEAD BUNDLE": "WOUND BEAD", "STEEL BELT": "BELT",
                 "TREAD": "TREAD", "SIDEWALL": "SIDEWALL", "INNER LINER": "INNER LINER", "PLY": "PLY",
                 "SHOULDER PAD": "SHOULDER PAD", "CALANDARED ROLL": "CALENDAR ROLL (SPOOL)"}


def _mpq_tbr_lookup(item, itype):
    code = str(item).upper()
    if "STC" in code and "STC" in _MPQ_TBR:
        return _MPQ_TBR["STC"]
    if ("CHF" in code or "CHAF" in code) and "CHAFFER" in _MPQ_TBR:
        return _MPQ_TBR["CHAFFER"]
    key = _TBR_TYPE_MAP.get(str(itype).strip().upper())
    return _MPQ_TBR.get(key, (None, None, "", False)) if key else (None, None, "", False)


def build_mpq_map(mpq):
    """Returns: itype -> (mpq_min, mpq_max, uom, fraction_allowed)
    mpq_max is the MHE capacity — used to SPLIT lots into MHE-sized sub-lots.
    mpq_min is parsed via _parse_mpq() so the plant's free-text formats (Tread "310, 206",
    Cap Strip "9*1800", "Spool length (300)") never silently drop to None again."""
    out = {}
    for _, r in mpq.iterrows():
        t = str(r["Item Type"]).strip()
        if not t: continue
        mn = _parse_mpq(r.get("Minimum Run Qty"))
        try: mx = float(r["Maximum Run Qty"])
        except (TypeError, ValueError, KeyError): mx = None
        uom = str(r.get("UOM", "")).strip().upper()
        frac_str = str(r.get("Fraction Allowed", "NO")).strip().upper()
        fraction_allowed = frac_str in ("YES","Y","TRUE","1")
        out[t] = (mn, mx, uom, fraction_allowed)
    return out


def build_belt_wire_map(bw_df):
    out = {}
    if bw_df is None or bw_df.empty: return out
    for _, r in bw_df.iterrows():
        b = str(r.get("belt_item", "")).strip()
        w = str(r.get("wire_type", "")).strip()
        if b and w:
            out[b] = w
    return out


def parse_machines(s: str):
    if not s: return []
    out = []
    for tok in str(s).split(","):
        m = tok.strip().strip("'\"")
        if m and m.upper() not in ("NAN", "NONE", ""):
            out.append(m)
    return out


def build_routing_meta(rt):
    _EOV = (load_cfg() or {}).get("efficiency_override")
    out = {}
    for _, r in rt.iterrows():
        rp = str(r["routed_product"]).strip()
        if not rp or rp in out: continue
        try: pt = float(r["proc_time"])
        except (TypeError, ValueError): pt = 0.0
        try: bs = float(r["batch_size"])
        except (TypeError, ValueError): bs = 0.0
        # ═══ CTP DEVIATION #1 of 2 — EFFICIENCY FROM THE ROUTING ═══════════════════
        # v6 hardcoded `eff = 1.0` ("100% efficiency override (per user spec)") and
        # IGNORED this column. CTP's routing states efficiency = 0.96, and the user
        # instructed: "use the efficiency from the routing as well".
        # `eff` DIVIDES in op_duration_min, so 0.96 makes every operation take
        # 1/0.96 = +4.17% longer. That is the intended meaning of efficiency.
        # NaN-safe: a blank must fall back to 1.0, NOT become NaN (see MEMORY.md,
        # "present-but-blank" — float(NaN) does not raise, and NaN <= 0 is False).
        try: eff = float(r["efficiency"])
        except (TypeError, ValueError, KeyError): eff = 1.0
        if not (eff == eff) or eff <= 0: eff = 1.0        # NaN or non-positive -> 1.0
        if _EOV is not None: eff = float(_EOV)            # v37 (user): efficiency_override -> every routing row
        # ═══ CTP DEVIATION #2 of 2 — TRANSFER TIME FROM THE ROUTING ════════════════
        # v6 NEVER reads transfer_time_min (zero references in all 9 phases). The user
        # instructed: "there is a transfer time column in that, make change in the code
        # according, always use that". It is a mandatory lag between a producer
        # finishing and its consumer being able to start — the same SHAPE as min_aging,
        # so it is added to min_aging at the single funnel in phase4 (get_min_aging) and
        # phase5 (_ages_ns). It is carried down the pipeline as a lot column below.
        # NOT added to max_aging: that is a shelf-life CEILING, not a lag.
        try: tt = float(r["transfer_time_min"])
        except (TypeError, ValueError, KeyError): tt = 0.0
        if not (tt == tt) or tt < 0: tt = 0.0             # NaN or negative -> 0.0
        machines = parse_machines(str(r["machines"]))
        out[rp] = {
            "proc_time":  pt,
            "proc_uom":   str(r["proc_time_UOM"]).strip().upper(),
            "batch_size": bs,
            "batch_uom":  str(r["batch_UNIT"]).strip().upper(),
            "efficiency": max(0.01, eff),
            "transfer_time_min": tt,
            "machines":   machines,
            "operation":  str(r["operation_name"]).strip(),
            "department": str(r["department"]).strip(),
            "equipment":  str(r["Equipment"]).strip(),
        }
    return out


# CTP DEVIATION #7 — what ONE cut of a cutter produces. Loaded from config.yaml in main().
# Empty dict = 1 piece per cut = v6's exact behaviour.
PIECES_PER_CUT: dict[str, int] = {}


def _norm_op(name):
    """Canonicalise an operation name for the pieces_per_cut lookup so a CUTS/MIN
    doubling can never be silently lost to spelling drift in the routing export.
    Uppercase, collapse internal whitespace, and fix the known 'Bios cutter' typo
    (the combined routing spells the BIAS CUTTER 'Bios cutter'; without this the
    lookup missed and defaulted pieces_per_cut to 1 -> 2x the bias-cut work ->
    m/c 2001 took 56.5 d instead of ~28 d). BOTH the config keys and the routing
    operation are passed through this, so they match regardless of case / spacing /
    the Bios<->Bias typo. 2026-08-06."""
    s = " ".join(str(name).strip().upper().split())
    return s.replace("BIOS", "BIAS")

# ── PER-MIXER MPQ (the plant's 'mixing mpq' sheet). Loaded from inputs/mixing_mpq.csv
#    in main(). WIRED 2026-07-23 (USER: "we have to read this also ... always used").
#    machine -> {"min_batches":float|None, "min_kg":float|None, "max_kg":float|None}
#    Empty dict = previous behaviour (config flat 3-batch floor, no maximum).
MIXING_MPQ: dict[str, dict] = {}


# ── MIXER CAPACITY CALIBRATION (v28, config mixer_capacity_calibration) ───────────────────────────
# Routing MIN/BATCH is the mixing time only; the floor makes ~230 final / ~145 master batches per mixer
# per day (user, 2026-10-06; WIP batch timestamps: 3.75 min batch-to-batch in a run, ~6 min over a day).
# Every compound's min/batch on a mixing operation is multiplied by ONE factor per operation so that a
# fully busy mixer makes the plant's average; compound-to-compound differences in routing are kept.
_MIX_CAL = {}              # operation name (upper) -> factor


def op_duration_min(qty, meta, item_code=None, lot_uom=None):
    _d = _op_duration_min_raw(qty, meta, item_code, lot_uom)
    _f = _MIX_CAL.get(str(meta.get("operation", "")).strip().upper())
    return _d * _f if _f else _d


def _op_duration_min_raw(qty, meta, item_code=None, lot_uom=None):
    """Compute operation duration in minutes for `qty` of `item_code`.
    Assumes lot_qty UOM matches routing proc_time UOM (e.g., NOS lot + NOS/MIN rate).
    Item-code argument kept for future routing-derived overrides; currently unused.
    """
    pt, u, bs, eff = meta["proc_time"], meta["proc_uom"], meta["batch_size"], meta["efficiency"]
    try: qty = float(qty)
    except (TypeError, ValueError): return 0.0
    if not (qty == qty) or qty <= 0: return 0.0
    if pt <= 0: return 0.0
    if u in ("MM/MIN", "M/MIN", "MTR/MIN"):       return (qty / pt) / eff
    if u == "NOS/MIN":                            return (qty / pt) / eff
    if u == "SEC/BATCH" and bs > 0:               return math.ceil(qty / bs) * pt / 60.0 / eff
    # CTP DEVIATION #6b — MIN/BATCH is proc_time in MINUTES per batch (raw routing exports use it
    # for 72% of rows). This is EXACTLY what adapt_inputs.routing() did (MIN/BATCH ×60 -> SEC/BATCH,
    # which then ÷60): ceil(qty/bs) batches × pt min/batch / eff. Dimensionally sound; not a guess.
    if u == "MIN/BATCH" and bs > 0:               return math.ceil(qty / bs) * pt / eff
    if u == "SEC" and bs > 0:                     return (qty / bs) * pt / 60.0 / eff
    if u == "SEC":                                return (qty * pt) / 60.0 / eff
    if u == "MIN":                                return (qty * pt) / eff

    # ═══ CTP DEVIATION #7 — CUTS/MIN, AND WHAT ONE CUT ACTUALLY PRODUCES ══════════════
    # USER: "cuts/min is nos/min ... 15 cut/min that is tyres/min. 1 cut gives us b1 and b2
    #        required for 1 tyre."
    #
    # v6 has NO `CUTS/MIN` branch. It fell through to the generic `endswith("/MIN")` rule
    # -> `qty / proc_time`, which SILENTLY assumes 1 cut = 1 piece.
    #
    # That is right for the PLY CUTTER (1 cut = 1 ply) and WRONG for the BELT CUTTER, where
    # ONE cut yields BOTH belt plies (B1 + B2) for one tyre. Demand is raised in NOS of belt
    # ply (2.000/tyre), so the number of CUTS is qty / 2.
    #
    #   v6  (1 cut = 1 ply) : 733,804 / 15 / 0.96 = 849 h on ONE machine with 720  -> 118%
    #   correct (1 cut = 2) : 366,902 / 15 / 0.96 = 425 h                          ->  59%
    #
    # `pieces_per_cut` lives in config.yaml — declared, not hidden in a fallthrough. Default
    # is 1, which reproduces v6 exactly.
    if u == "CUTS/MIN":
        ppc = PIECES_PER_CUT.get(_norm_op(meta.get("operation", "")), 1)
        return (qty / max(1, ppc)) / pt / eff

    if u.endswith("/MIN"):                        return (qty / pt) / eff

    # ═══ CTP DEVIATION #6 — AN UNKNOWN UOM MUST RAISE, NOT GUESS ══════════════════════
    # v6 ended with `return (qty * pt) / eff` — a silent guess for ANY UOM it does not
    # recognise. That guess MULTIPLIES a quantity by a rate, which is dimensionally wrong
    # for every rate-like UOM, and it fails CATASTROPHICALLY and SILENTLY.
    #
    # MEASURED: run the phases on CTP's RAW routing (72% MIN/BATCH, which v6 has no branch
    # for) and this line multiplies KILOGRAMS by MINUTES. The mixers then report
    # 9,580% and 7,518% utilisation — and NOTHING errors.
    #
    # `adapt_inputs.routing()` converts MIN/BATCH -> SEC/BATCH precisely so this never
    # happens. That conversion is load-bearing, and this guard is what stops it from
    # silently rotting: if a NEW unrecognised UOM ever appears in a refreshed routing,
    # the run STOPS instead of inventing a duration.
    raise ValueError(
        f"phase2.op_duration_min: UNRECOGNISED proc_time_UOM {u!r} on item {item_code!r} "
        f"(proc_time={pt}, batch_size={bs}).\n"
        f"    v6's original code silently fell back to `qty * proc_time`, which MULTIPLIES a "
        f"quantity by a rate. On CTP's raw MIN/BATCH routing that produced 9,580% mixer "
        f"utilisation with no error at all.\n"
        f"    I will not guess a duration. Either add an explicit branch for {u!r}, or "
        f"convert it to a UOM v6 understands in adapt_inputs.routing() "
        f"(as MIN/BATCH -> SEC/BATCH already is)."
    )


LOT_COLUMNS = [
    "lot_id","item_code","item_type","operation","department","equipment",
    "raw_demand","lot_qty","lot_uom","surplus",
    "duration_min","duration_hours",
    "first_need","last_need","n_blocks","n_skus",
    "min_aging_h","max_aging_h",
    "transfer_time_h",                      # CTP DEVIATION — routing transfer_time_min / 60
    "mpq_min","mpq_uom","fraction_allowed",
    "is_belt","machines","n_machines",
    "wire_type","campaign_id","is_campaign_start","is_campaign_end",
    # NEW: wave attrs
    "wave_ids","wave_first","wave_last",
    # NEW: MHE attrs (Phase 5 uses these for per-MHE material flow)
    "batch_id","mhe_index","mhe_total",
    # lot -> SKU set: written to phase2_lot_skus.csv so Phase 3 can match
    # producer<->consumer by SKU (must be regenerated each run, else GT/Carcass
    # sub-lots renumber and the DAG loses their edges).
    "skus_set",
]


def _bom_required(lots, bom, gross_dem, q_sku):
    """required[child] = SUM over parents p of made[p] * ratio(p->c).  BOM-derived, exact.

    THE DEFECT THIS MEASURES (CTP #30) — found by the USER, by eye, from two totals in the
    floor schedule, AFTER the pipeline printed "all_invariants_hold: YES".
        phase1b explodes the BOM from DEMAND. THIS phase then applies the MPQ floor PER ITEM,
        INDEPENDENTLY (`lot_qty_full = max(raw_qty, mpq_min)`, :~687), with NO feedback to
        children. Both sides are locally correct. THEY NEVER TALK. A parent rounded up to its
        MPQ floor creates demand its children were never told about.
        MEASURED on the shipped 35.85 d baseline: 53 items short, 124,485 units uncovered —
        Bead Bundle 73,023 (25 items), CALANDARED ROLL 34,185 (5), FINAL COMPOUND 16,402 (18).
        Worst: BW16002 needs 17,200, has 7,232 + 705 WIP -> 9,263 short, driven by BA16014.
        Bead chain, in the plant's own words: you cannot apex a bead you never wound.

    WHY NOTHING ELSE CATCHES IT — do not try to fix this in phase5:
        `_material_ready_ns` (phase5:~1708) CANNOT. Its `need` is summed from THE SAME plist
        that builds its `events`, so `cum >= need` is a TAUTOLOGY — it always resolves to
        max(finish + min_age) = plain DAG precedence. And phase3's edges carry NO quantity
        (producer_lot, producer_item, consumer_lot, consumer_item, via_sku). There is no
        BOM-derived requirement anywhere in phase5. A gate there would be decoration.
        (416 of 1,109 bead-apex lots don't even HAVE a bundle edge — phase3 granularity.)

    WHY THIS CANNOT BE A TAUTOLOGY (the test MEMORY demands: what would make it FAIL?):
        `required` comes from the BOM x this phase's made-qty. `available` comes from the lot
        table + inventory. TWO INDEPENDENT SOURCES. It fails whenever
            sum_p made[p]*ratio(p->c) > made[c] + usable_wip[c].

    THE RATIO IS cq / per_tyre(parent) — NOT cq / Parent_qty.
        Identical on 2,367 of 2,568 routed->routed BOM rows; DIFFERENT on 201 (CALANDARED ROLL
        151, Steel Belt Edge Strip 42, FINAL COMPOUND 8). e.g. PBP08 is 1.345 M as a PARENT of
        ECOPOLY350 but 1 NOS as a CHILD of the GT — cq/Parent_qty mixes the units and
        understates the CALANDARED ROLL cascade by ~2,479 M. (phase1c's CTP #9 uses
        cq/Parent_qty; right for cap strip where Parent_qty == per-tyre, wrong on those 201.
        FLAGGED, not fixed here — it is phase1c's.)

    GROSS demand is deliberate: netting already removed the WIP that the caller credits
    separately. Crediting it twice understates the shortfall.
    """
    made = lots.groupby(lots["item_code"].astype(str).str.strip())["lot_qty"].sum()
    d_is = gross_dem.groupby(["item_code", "sku"])["demand_qty"].sum()
    per = defaultdict(dict)
    for s, p, c, q in zip(bom["Super_parent"], bom["Parent"], bom["child"], bom["_cq"]):
        if p not in made.index or c not in made.index:
            continue
        w = float(d_is.get((p, s), 0.0)); Q = float(q_sku.get(s, 0.0) or 0.0)
        if w <= 0 or Q <= 0:
            continue
        pt_rate = w / Q                     # the parent's own per-tyre rate in THIS sku
        if pt_rate > 0:
            per[(p, c)][s] = per[(p, c)].get(s, 0.0) + q / pt_rate
    required = defaultdict(float)
    for (p, c), by_sku in per.items():
        num = den = 0.0
        for s, r in by_sku.items():
            w = float(d_is.get((p, s), 0.0)); num += w * r; den += w
        if den > 0:
            required[c] += float(made.get(p, 0.0)) * (num / den)
    return required, made


def _empty_camp_fields():
    return {"wire_type":"", "campaign_id":"",
            "is_campaign_start": False, "is_campaign_end": False}


WAVE_DURATION_DAYS = 3.0

# === FIX #2 v3 (REVERTED to safe scope) SUB-WAVE BUCKETING ===
# Items with max_aging shorter than the wave (72h) need finer-grained batches.
# Lesson learned: sub-bucketing FC items (24h buckets) created 39% MORE FC lots,
# adding 12h of changeover overhead on mixer 0203 -> FC supply slipped 2 days
# -> cascaded through FRC -> cutters -> building -> horizon overrun.
#
# Reverted to: only short-aging items (max_aging < 48h) get sub-bucketed.
# FC items (max_aging 72-96h) keep their original 72h wave bucket — fewer
# mixer changeovers, smoother FC supply.
SHORT_AGING_THRESHOLD_H  = 48.0   # apply 12h sub-bucketing for items < 48h
SUB_WAVE_BUCKET_H_SHORT  = 12.0   # bucket size for items with max_aging < 48h
SUB_WAVE_BUCKET_H_MEDIUM = 24.0   # bucket size (daily) for all other non-mixing items
SUB_WAVE_SAFETY_H        = 8.0    # safety inside max_aging when checking span

# === HYBRID WAVE GRANULARITY (mixing=3-day, others=1-day) ===
# Plant decision: keep MIXING (compounds) batched at the 3-day wave for Banbury
# campaign efficiency (and because sub-bucketing FINAL COMPOUND historically slipped
# the mixer -> horizon overrun). Pace EVERYTHING ELSE to DAILY (24h) buckets so
# short-life components are produced fresh against the smooth daily curing and the
# building runs smaller, more frequent, multi-SKU days. Costs more changeovers
# downstream (within budget), buys component freshness + smoother building.
MIXING_ITYPES = {"MASTER COMPOUND", "FINAL COMPOUND"}


def size_lots_waveaware(demand, wave_map, rt_meta, aging_map, itype_map, mpq_map, cfg,
                          skip_items=None):
    """Wave-bucketed lot sizing — FAST numpy-array implementation."""
    import numpy as np
    skip_items = skip_items or set()
    max_dur_min = cfg["max_lot_duration_h"] * 60.0
    wave_dur_h = WAVE_DURATION_DAYS * 24

    # ═══ AGING-AWARE LOT SPLIT (L7) — config, DEFAULT OFF => byte-identical to v6 ═════════
    # See the block in the sub-bucketing loop below. When ON, a lot whose consumer span
    # exceeds the item's aging WINDOW (max_aging - min_aging) is split so the material made
    # at a lot's start is still inside shelf life when its LAST consumer draws it. safety
    # defaults to the item's own transfer_time (the lag already folded into min_aging in
    # phase4/5); a flat aging_split_safety_h overrides it.
    _aging_split = bool(cfg.get("aging_aware_lot_split", False))
    _flat_safety = cfg.get("aging_split_safety_h", None)
    # Item types the split must NOT touch. Green Tyres are the BUILDING DRUM: splitting
    # their lots multiplies building changeovers and moves the makespan (measured), and
    # GT freshness is already governed by phase5/phase6 (24h override + streaming gate),
    # not by this component-shelf-life split. Default excludes them; empty list = split all.
    _aging_excl = {str(t).strip().upper()
                   for t in (cfg.get("aging_split_exclude_itypes",
                                     ["Green Tyres"]) or [])}
    # PRODUCTION-SMOOTHING SPLIT (2026-08-02): long-shelf items (master/final compound, calendered
    # roll) have a HUGE aging window, so the aging split never fires and ONE lot carries many days
    # of demand (measured: master compound lots span ~8.8 days). A batch finishes all at once, so
    # its whole multi-day quantity is pulled to its FIRST consumer -> the day-0 mixer must make ~9
    # days of compound at once, saturating the ramp and delaying building. Cap the split window for
    # these types at a SMOOTHING window (not their shelf) so each lot is chunked to ~daily, spreading
    # production so the ramp only makes the near-term share. [] = OFF (byte-identical).
    _smooth_itypes = {str(t).strip().upper() for t in (cfg.get("smooth_lot_span_itypes") or [])}
    _smooth_span_h = float(cfg.get("smooth_lot_span_h", 48.0))
    _smooth_by_it = {str(k).strip().upper(): float(v) for k, v in (cfg.get("smooth_lot_span_h_by_itype") or {}).items()}   # v63
    if _smooth_itypes:
        print(f"  PRODUCTION-SMOOTHING SPLIT: {sorted(_smooth_itypes)} lots capped to {_smooth_span_h:.0f}h "
              f"consumer-span (spreads compound so the ramp isn't forced to make many days at once)")

    lots = []
    block_assignments = []
    _uncostable_ops = []   # present-but-blank routing rows (declared holes, not crashes)
    _aging_degenerate = set()   # items whose window <= safety: cannot split to fit (declared)

    # Attach wave_id and sort once
    demand = demand.copy()
    demand["wave_id"] = demand["block_id"].map(wave_map).fillna("W99")
    demand = demand.sort_values(["item_code","need_by"]).reset_index(drop=True)

    # ---------------- WIP SUBTRACTION (V6_wave) ----------------
    # Load plant's daily 7-AM inventory snapshot. Switched from old
    # WIP_simulation_May2026.csv to inventory_combined (via db_loader).
    # FIFO: earliest-need demand rows are covered first.
    wip_by_item = {}
    try:
        import sys as _sys
        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from db_loader import load_wip_by_item
        wip_by_item = load_wip_by_item(cfg)
    except Exception as _e:
        # Fallback: legacy WIP file if present
        wip_path = OUTPUTS / "WIP_simulation_May2026.csv"
        if wip_path.exists():
            wip = pd.read_csv(wip_path)
            for _, r in wip.iterrows():
                item = str(r["itemcode"]).strip()
                try:
                    q = float(r["availableinventory"])
                except Exception:
                    continue
                wip_by_item[item] = wip_by_item.get(item, 0.0) + q
        else:
            print(f"  [WARN] Inventory load failed ({_e}); proceeding with no WIP")
    if wip_by_item:
        print(f"  WIP loaded: {len(wip_by_item):,} items, {sum(wip_by_item.values()):,.0f} units (pre-cap)")

        # FIX 3: ENFORCE PLANT-WIDE FLOOR CAPS per item-type (plant physical limits)
        # Plant cannot store more than 4,000 Carcass NOS or 10,000 GT NOS at once.
        # Cap individual item-types at the plant total, allocated PROPORTIONALLY to
        # each item's current WIP share.
        PLANT_ITEM_TYPE_CAPS = {
            "Carcass":     4000.0,    # plant floor space hard cap
            "Green Tyres": 10000.0,   # plant floor space hard cap
        }
        try:
            _itype = _db_or_csv("itemtype_master", cfg)
            itype_map = dict(zip(_itype["ItemCode"].astype(str).str.strip(),
                                  _itype["ItemType"].astype(str).str.strip()))
            for itype_name, cap_total in PLANT_ITEM_TYPE_CAPS.items():
                # Find all WIP items of this item-type
                items_of_type = {it: q for it, q in wip_by_item.items()
                                 if itype_map.get(it, "") == itype_name}
                if not items_of_type:
                    continue
                cur_total = sum(items_of_type.values())
                if cur_total <= cap_total:
                    print(f"  PLANT_CAP[{itype_name}]: WIP {cur_total:,.0f} <= cap {cap_total:,.0f}  (no clip)")
                    continue
                # Clip proportionally
                scale = cap_total / cur_total
                clipped = 0.0
                for it, q in items_of_type.items():
                    new_q = q * scale
                    wip_by_item[it] = new_q
                    clipped += (q - new_q)
                print(f"  PLANT_CAP[{itype_name}]: clipped {cur_total:,.0f} -> {cap_total:,.0f}  (removed {clipped:,.0f} phantom WIP)")
        except Exception as e:
            print(f"  PLANT_CAP step skipped (no itemtype_master / error): {e}")

        # FIX 4: Cap MASTER COMPOUND WIP at <= 50% of its CASCADED demand.
        # MC was previously set to 10x demand (effectively infinite). After Phase 1c
        # MRP cascade, MC WIP showed 575M vs cascaded demand 4.85M (118x over) which
        # nullifies all MC production -> upstream feeders break. Now cap MC per-item
        # at half of its remaining demand in phase1_demand_NET.
        try:
            net_path = OUTPUTS / "phase1_demand_NET_updated.csv.gz"
            if net_path.exists():
                _net = pd.read_csv(net_path)
                mc_items = {it for it, t in itype_map.items() if t == "MASTER COMPOUND"}
                mc_demand_by_item = _net[_net["item_code"].isin(mc_items)].groupby("item_code")["demand_qty"].sum().to_dict()
                clipped_mc = 0.0
                n_clipped = 0
                for it in list(wip_by_item.keys()):
                    if itype_map.get(it, "") != "MASTER COMPOUND":
                        continue
                    demand_qty = mc_demand_by_item.get(it, 0.0)
                    mc_cap = max(0.0, demand_qty * 0.5)
                    if wip_by_item[it] > mc_cap:
                        clipped_mc += (wip_by_item[it] - mc_cap)
                        wip_by_item[it] = mc_cap
                        n_clipped += 1
                if n_clipped > 0:
                    print(f"  PLANT_CAP[MASTER COMPOUND]: capped {n_clipped} items at 50% of cascaded demand "
                          f"(removed {clipped_mc:,.0f} phantom WIP)")
        except Exception as e:
            print(f"  MC WIP cap skipped: {e}")
        print(f"  WIP after plant caps: {len(wip_by_item):,} items, {sum(wip_by_item.values()):,.0f} units")

    # Check if NET demand file exists - if so, MRP cascade already handled WIP
    _skip_wip_sub = (OUTPUTS / "phase1_demand_NET_updated.csv.gz").exists()
    if wip_by_item and not _skip_wip_sub:
        import numpy as np
        orig_by_item = demand.groupby("item_code")["demand_qty"].sum().to_dict()
        # Vectorized FIFO subtraction:
        # Within each item (already sorted by need_by), compute cumulative qty
        # and subtract WIP. Rows whose cumsum <= wip are fully covered (=0).
        # The "straddling" row gets the residual.
        demand_qty_arr = demand["demand_qty"].fillna(0.0).to_numpy(dtype=float)
        item_arr = demand["item_code"].to_numpy()
        cumsum = pd.Series(demand_qty_arr).groupby(item_arr).cumsum().to_numpy()
        wip_arr = np.array([wip_by_item.get(it, 0.0) for it in item_arr], dtype=float)
        # Remaining demand BEFORE this row was needed at all
        prior_cum = cumsum - demand_qty_arr
        # WIP used by THIS row = max(0, min(demand_qty, wip - prior_cum))
        wip_for_row = np.maximum(0.0, np.minimum(demand_qty_arr, wip_arr - prior_cum))
        new_qty_arr = demand_qty_arr - wip_for_row
        n_zeroed = int(((demand_qty_arr > 0) & (new_qty_arr == 0)).sum())
        demand["demand_qty"] = new_qty_arr
        print(f"  After WIP subtraction: {n_zeroed:,} demand rows fully covered")
        n_pos = int((demand["demand_qty"] > 0).sum())
        print(f"  Remaining demand rows with qty > 0: {n_pos:,}")

        new_by_item = demand.groupby("item_code")["demand_qty"].sum().to_dict()
        audit_rows = []
        all_items = set(orig_by_item) | set(wip_by_item)
        for item in all_items:
            orig = orig_by_item.get(item, 0.0)
            new = new_by_item.get(item, 0.0)
            wip_avail = wip_by_item.get(item, 0.0)
            wip_used = max(0.0, min(orig, wip_avail))
            audit_rows.append({
                "item_code": item,
                "original_demand_qty": round(orig, 3),
                "wip_available": round(wip_avail, 3),
                "wip_used": round(wip_used, 3),
                "new_demand_qty": round(new, 3),
            })
        audit_df = pd.DataFrame(audit_rows).sort_values("wip_used", ascending=False)
        audit_path = OUTPUTS / _suff("phase2_wip_subtraction_audit.csv")
        _ensure_writable(audit_path)
        audit_df.to_csv(audit_path, index=False)
        print(f"  Wrote audit -> {audit_path.name}")

        demand = demand[demand["demand_qty"] > 0].reset_index(drop=True)
    # ---------------- END WIP SUBTRACTION ----------------
    # Convert critical columns to numpy arrays once
    all_items = demand["item_code"].values
    all_need_by = pd.to_datetime(demand["need_by"]).values.astype("datetime64[ns]")
    all_qty = pd.to_numeric(demand["demand_qty"], errors="coerce").fillna(0).values.astype(float)
    all_wave = demand["wave_id"].values
    all_block = demand["block_id"].values
    all_sku = demand["sku"].astype(str).values
    all_uom = demand["demand_uom"].astype(str).values

    # Find item boundaries
    item_change = np.r_[True, all_items[1:] != all_items[:-1]]
    item_starts = np.where(item_change)[0]
    item_ends   = np.r_[item_starts[1:], len(all_items)]

    for k in range(len(item_starts)):
        item = all_items[item_starts[k]]
        if item in skip_items: continue
        meta = rt_meta.get(item)
        if not meta: continue
        # ═══ PRESENT-BUT-BLANK NaN TRAP ══════════════════════════════════════════════════
        # v6 survives a MISSING routing row (the `if not meta` line above), but NOT a row
        # that is PRESENT with a blank proc_time / blank UOM -- float(NaN) does not raise, so
        # a blank cell flows into op_duration_min and crashes ~150 lines deep. The NEW routing
        # (jkt_routing_pcr 20) added building rows for green tyres that were formerly ABSENT
        # but left proc_time / machine / UOM blank. A blank row must behave IDENTICALLY to an
        # absent row: SKIP the op (the SKU becomes an unbuilt/phantom-cure exactly as before),
        # DECLARE it loudly. NOTHING IS INVENTED -- we do not guess a duration.
        _pt = meta.get("proc_time"); _u = str(meta.get("proc_uom", "")).strip().upper()
        _pt_bad = (_pt is None) or (_pt != _pt) or (float(_pt) <= 0.0)
        if _pt_bad or _u in ("", "NAN", "NONE"):
            _uncostable_ops.append((str(item), str(meta.get("operation", "")),
                                    str(meta.get("department", "")), _pt, _u))
            continue
        a, b = item_starts[k], item_ends[k]
        # Slice arrays for this item
        need_by = all_need_by[a:b]
        qty     = all_qty[a:b]
        wave_id = all_wave[a:b]
        block_id = all_block[a:b]
        sku_arr = all_sku[a:b]
        d_uom = all_uom[a]
        # Filter zero qty
        m = qty > 0
        if not m.any(): continue
        need_by = need_by[m]; qty = qty[m]; wave_id = wave_id[m]
        block_id = block_id[m]; sku_arr = sku_arr[m]

        itype = itype_map.get(item, "")
        # New 4-tuple: (min, max, uom, fraction_allowed). Backward compatible 3-tuple supported.
        mpq_info = mpq_map.get(itype, (None, None, "", False))
        # ── LINE-SPECIFIC MPQ (2026-09-29, config mpq_tbr_from_db) ─────────────────────────
        # A TBR-only item takes the plant's TBR row (JK_CTP_PPDS.CTP_Master_MPQ), never the PCR
        # sheet; no TBR row for its type -> no floor. Compounds keep the batch rule (unchanged).
        if _MPQ_TBR is not None and _ITEM_TT.get(item) == "TBR" and "COMPOUND" not in str(itype).upper():
            mpq_info = _mpq_tbr_lookup(item, itype)
        # ── ITEM-LEVEL MPQ (v26, config mpq_item_level): every CTP_Master_MPQ row (PCR + TBR, tread by
        # rim size, calendar roll spool / dipped cord, chaffer, STC, BRTTL, white SW, edge strip / gum)
        # mapped to the item by build_mpq_items.py. It is the authority: an item with no row has no floor.
        if _MPQ_ITEM is not None and "COMPOUND" not in str(itype).upper():
            _ri = _MPQ_ITEM.get(str(item).strip())
            mpq_info = (_ri[0], None, _ri[1], False) if _ri else (None, None, "", False)
        # UNIT GUARD: a floor applies only in the item's own unit (never '200 Nos' on a metre item)
        if mpq_info[0] is not None and len(mpq_info) >= 3:
            _mu = str(mpq_info[2] if len(mpq_info) == 4 else mpq_info[1]).upper().replace("NOS", "NOS").strip()
            _du = str(d_uom).upper().strip()
            if _mu and _du and _mu not in ("BATCHES",) and _mu != _du and not (
                    {_mu, _du} <= {"NOS", "NO", "PCS"}):
                _MPQ_UNIT_SKIPS.append((item, itype, mpq_info[0], _mu, _du))
                mpq_info = (None, None, "", False)
        if len(mpq_info) == 3:
            mpq_min, mpq_uom, fraction_allowed = mpq_info
            mpq_max = None
        else:
            mpq_min, mpq_max, mpq_uom, fraction_allowed = mpq_info
        if mpq_min is not None and mpq_min <= 0: mpq_min = None
        if mpq_max is not None and mpq_max <= 0: mpq_max = None

        # ── MIXING MPQ (compounds) — NOW READ FROM THE PLANT'S 'mixing mpq' SHEET ──────
        # WIRED 2026-07-23 (USER: "we have to read this also ... this should be always used").
        # Before this, NOTHING read that sheet: the floor came from the flat config value and
        # there was NO maximum at all -- which is exactly why phase2 printed
        #   "MHE MODEL INERT: no Maximum Run Qty -> mhe_total = 1"   on every run.
        #
        # The sheet is PER MIXER. An item's eligible mixers come from routing `machines`
        # (163 compounds: 35 have 1 mixer, 88 have 2, 21 have 3, 19 have 4), so we resolve:
        #
        #   MIN  min_batches from the sheet (smallest across the item's eligible mixers)
        #        x the ROUTING's per-ITEM batch_size. The routing is the authority for THIS
        #        compound's batch (e.g. B350 = 210 kg) whereas the sheet's min_kg is a
        #        per-MACHINE nominal (F270 = 220, 310 = 230) -- they are NOT the same number,
        #        so we take batches from the sheet and the batch SIZE from the routing.
        #   MAX  the SMALLEST eligible mixer's capacity, so the lot is runnable WHEREVER
        #        phase5 later places it -- phase5 chooses the machine and knows nothing about
        #        mixer capacity, so sizing to the largest mixer would emit un-runnable lots.
        #        (measured: 115 of 586 compound lots exceed it today; largest lot 51,394 kg
        #        vs the biggest mixer 47,250 -- i.e. lots that cannot be mixed as printed.)
        # USER-DIRECTED 2026-07-23 (REVERSAL): `use_mixing_mpq_file: false` -> do NOT use the
        # 'mixing mpq' sheet. Master/Final compound go back to the flat
        #       lot >= mixing_mpq_min_batches(3) x ROUTING batch_size
        # exactly as before the sheet was wired. NOTE: the sheet's min_batches is also 3 on
        # every mixer, so the FLOOR is unchanged either way -- what is dropped is the per-mixer
        # MAXIMUM, and with it the cap that kept 115 compound lots inside their mixer's
        # capacity (largest lot goes back to 51,394 kg vs a 47,250 kg biggest mixer).
        # Set the flag true to restore it.
        if cfg.get("apply_mixing_mpq", True):
            _bs   = float(meta.get("batch_size") or 0) if meta else 0.0
            _mach = list(meta.get("machines") or []) if meta else []
            _use_sheet = bool(cfg.get("use_mixing_mpq_file", False))
            _rows = [MIXING_MPQ[m] for m in _mach if m in MIXING_MPQ] if _use_sheet else []
            _mb   = (cfg.get("mixing_mpq_min_batches") or {}).get(itype)
            if _rows:
                _sheet_mb = [r["min_batches"] for r in _rows if r.get("min_batches")]
                if _sheet_mb:
                    _mb = min(_sheet_mb)                      # sheet wins over the config value
                _caps = [r["max_kg"] for r in _rows if r.get("max_kg")]
                if _caps:
                    _cap = min(_caps)
                    mpq_max = _cap if mpq_max is None else min(mpq_max, _cap)
            if _mb and _bs > 0:
                _floor = float(_mb) * _bs
                mpq_min = _floor if mpq_min is None else max(mpq_min, _floor)

        mn_age, mx_age = aging_map.get(item, (0.0, 72.0))
        # USER-DIRECTED green-tyre shelf-life override (2026-07-16). Keeps the plant
        # aging_master.csv (24h) INTACT; when green_tyre_age_override_master is ON,
        # config green_tyre_max_age_h supersedes the master for GREEN TYRES only.
        # OPERATING value, NOT plant-confirmed (plant data = 24h). OFF => byte-identical.
        if (itype == "Green Tyres" and cfg.get("green_tyre_age_override_master")
                and float(cfg.get("green_tyre_max_age_h", 0) or 0) > 0):
            mx_age = float(cfg["green_tyre_max_age_h"])
        # === WAVE-CARRY (smooths production load by combining adjacent waves) ===
        # Items with max_aging significantly longer than wave_duration can absorb
        # later-wave demand into earlier-wave batches. This SHIFTS production load
        # backward in time, reducing late-wave concentration (W08/W09/W10 spike).
        #
        # Plant rule: producer batch span must fit within max_aging window so
        # late consumers don't see expired material (enforced by span check below).
        #   max_aging >= 3 * wave_duration_h (>=216h): combine up to 2 forward waves
        #   max_aging >= 2 * wave_duration_h (>=144h): combine up to 1 forward wave
        #   max_aging  <  2 * wave_duration_h (<144h): no carry (Carcass, FC 72-96h)
        if mx_age >= 3 * wave_dur_h:
            WAVE_CARRY_CAP = 2
        elif mx_age >= 2 * wave_dur_h:
            WAVE_CARRY_CAP = 1
        else:
            WAVE_CARRY_CAP = 0
        max_carry = min(WAVE_CARRY_CAP, max(0, int(mx_age // wave_dur_h)))
        max_window_ns = np.timedelta64(int(mx_age * 3600 * 1e9), "ns")

        # Build wave_ids_in_order (preserve chronological)
        seen_waves = []
        seen_set = set()
        for w in wave_id:
            if w not in seen_set:
                seen_waves.append(w); seen_set.add(w)
        wave_ids_in_order = seen_waves
        # Indices per wave
        wave_demand_idx = {}
        for wid in wave_ids_in_order:
            wave_demand_idx[wid] = np.where(wave_id == wid)[0]

        # ── DEEP ROLL (v44, user, config fourrc_rules.deep_roll): a dipped-fabric calender item is made in FULL dips
        # (Spool/Dip Length, CTP_Master_FRC). At each decision point (the first demand not yet covered by stock) the
        # demand of the next horizon_h (48 h) is taken: >= one dip -> floor(demand / D) full dips (4,400 m -> 2 dips);
        # < one dip -> the horizon is extended to extend_h (72 h) and decided again (floor, at least one dip, since a
        # partial dip is never made). Dip metres beyond the covered demand stay in stock and cover the next demand
        # first. Each decision is one lot (its waves are replaced by these groups; no MPQ pooling for the item).
        _DEEP_T = {}
        _Dd = _FRC.get(str(item).strip())
        if (_DEEP["on"] and _Dd and _FRC_CLS.get(str(item).strip(), "") in _DEEP["classes"] and len(qty) > 0):
            _h48 = np.timedelta64(int(_DEEP["h48"] * 3600 * 1e9), "ns")
            _h72 = np.timedelta64(int(_DEEP["h72"] * 3600 * 1e9), "ns")
            _ord = np.argsort(need_by, kind="stable")
            _groups, _stock, _k = [], 0.0, 0
            while _k < len(_ord):
                r0 = _ord[_k]
                if _groups and _stock >= float(qty[r0]) - 1e-6:          # stock from the last dips covers it
                    _stock -= float(qty[r0])
                    _groups[-1][0].append(r0)
                    _k += 1
                    continue
                t0 = need_by[r0]
                rest = float(qty[r0]) - _stock
                d48 = rest + float(sum(qty[x] for x in _ord[_k + 1:] if need_by[x] - t0 < _h48))
                if _DEEP["mode"] == "max_aging":
                    # v46 (user): dips for the 48 h need AND for the need inside the item's max-aging window
                    # (CTP_Master_Aging); the HIGHER one decides, in FULL dips only (floor; at least one dip):
                    # 1.4 vs 2.1 -> 2 dips, 1.4 vs 1.8 -> 1 dip.
                    _hA = np.timedelta64(int(float(mx_age) * 3600 * 1e9), "ns") if (mx_age == mx_age and mx_age) else _h48
                    dA = rest + float(sum(qty[x] for x in _ord[_k + 1:] if need_by[x] - t0 < _hA))
                    n_ = max(1, int(math.floor(max(d48, dA) / _Dd + 1e-9)))
                    _DEEP["aging_won"] += int(math.floor(dA / _Dd + 1e-9) > math.floor(d48 / _Dd + 1e-9))
                elif d48 >= _Dd - 1e-6:
                    n_ = int(math.floor(d48 / _Dd + 1e-9))
                else:
                    d72 = rest + float(sum(qty[x] for x in _ord[_k + 1:] if need_by[x] - t0 < _h72))
                    n_ = max(1, int(math.floor(d72 / _Dd + 1e-9)))
                    _DEEP["ext72"] += 1
                n_ = max(n_, int(math.ceil(rest / _Dd - 1e-9)))              # never split a demand row
                _avail = _stock + n_ * _Dd
                grp = [r0]
                _avail -= float(qty[r0])
                _k += 1
                while _k < len(_ord) and float(qty[_ord[_k]]) <= _avail + 1e-6:
                    _avail -= float(qty[_ord[_k]])
                    grp.append(_ord[_k])
                    _k += 1
                _stock = _avail
                _groups.append((grp, n_ * _Dd))
                _DEEP["lots"] += 1
                _DEEP["rolls"] += n_
            wave_ids_in_order = [f"DEEP{gi:03d}" for gi in range(len(_groups))]
            wave_demand_idx = {w: np.array(sorted(g, key=lambda x: need_by[x])) for w, (g, _) in zip(wave_ids_in_order, _groups)}
            _DEEP_T = {w: q for w, (_, q) in zip(wave_ids_in_order, _groups)}
            max_carry = 0
            max_window_ns = np.timedelta64(int(1e18), "ns")

        # ── v47 MIXING COVER WINDOWS (config mixing_v47): one run of a compound = all its demand in the next cover_h
        # (15 h; 24 h for IL3 / IL5 and 4RC compounds) from the first demand not yet covered; the next run starts at
        # the first demand after that window. Window cut to shelf life (max - min aging - transfer - margin).
        _CW_T = False
        if (_MIXCW["on"] and not _DEEP_T and str(itype).strip().upper() in ("MASTER COMPOUND", "FINAL COMPOUND")
                and str(item).strip() not in _MIXCW["exempt"] and len(qty) > 0):
            _w_h = _MIXCW["w_long"] if str(item).strip() in _MIXCW["long"] else _MIXCW["w"]
            _shelf_h = (float(mx_age) - float(mn_age) - float(meta.get("transfer_time_min", 0.0) or 0.0) / 60.0
                        - _MIXCW["margin_h"]) if mx_age == mx_age and mx_age else _w_h
            if _shelf_h < _w_h:
                _MIXCW["short"].add(f"{item} ({_shelf_h:.1f} h)")
                _w_h = max(_shelf_h, 1.0)
            _wn = np.timedelta64(int(_w_h * 3600 * 1e9), "ns")
            _ord = np.argsort(need_by, kind="stable")
            _cg, _k = [], 0
            while _k < len(_ord):
                _t0 = need_by[_ord[_k]]
                _g = []
                while _k < len(_ord) and need_by[_ord[_k]] - _t0 < _wn:
                    _g.append(_ord[_k])
                    _k += 1
                _cg.append(_g)
            wave_ids_in_order = [f"CW{gi:03d}" for gi in range(len(_cg))]
            wave_demand_idx = {w: np.array(sorted(g, key=lambda x: need_by[x])) for w, g in zip(wave_ids_in_order, _cg)}
            max_carry = 0
            max_window_ns = np.timedelta64(int(1e18), "ns")
            _CW_T = True
            _MIXCW["items"] += 1
            _MIXCW["lots"] += len(_cg)

        # Walk waves; build buckets of up to (max_carry+1) consecutive waves
        # while staying inside max_aging window
        i = 0
        i = 0
        seq = 1
        machines_str = ", ".join(meta["machines"])
        n_machines = len(meta["machines"])
        op_name = meta["operation"]
        dept = meta["department"]
        equip = meta["equipment"]

        # === FIX #2 v3 SUB-WAVE BUCKETING (short-aging only) ===
        # Only sub-bucket items with max_aging < 48h (Carcass, Cap Strip, R.Steel Belt).
        # FC items (72-96h) stay at wave granularity to keep mixer changeovers low.
        if mx_age > 0 and mx_age < SHORT_AGING_THRESHOLD_H:
            sub_wave_bucket_h = SUB_WAVE_BUCKET_H_SHORT     # 12h
            sub_wave_bucket_ns = np.timedelta64(int(sub_wave_bucket_h * 3600 * 1e9), "ns")
            use_sub_wave = True
        else:
            sub_wave_bucket_ns = None
            use_sub_wave = False

        _pool_carry = None                                 # v26 MPQ pooling: small bucket held for the next wave
        _frc_res = 0.0                                     # v27: open spool / dip residual of this calender item
        while i < len(wave_ids_in_order):
            # Bucket waves [i..j) — extend while (j-i) <= max_carry and within aging window
            bucket_idx = np.array(wave_demand_idx[wave_ids_in_order[i]])
            first_need_arr = need_by[bucket_idx]
            first_need = first_need_arr[0]
            bucket_waves = [wave_ids_in_order[i]]
            j = i + 1
            while j < len(wave_ids_in_order) and (j - i) <= max_carry:
                wid = wave_ids_in_order[j]
                idx = wave_demand_idx[wid]
                last_in_wave = need_by[idx[-1]]
                if (last_in_wave - first_need) > max_window_ns:
                    break
                bucket_waves.append(wid)
                bucket_idx = np.concatenate([bucket_idx, idx])
                j += 1

            # FIX #2: For short-aging items, split bucket_idx into sub-buckets
            # by need_by time so each sub-bucket fits in (max_aging - safety).
            if use_sub_wave and len(bucket_idx) > 0 and not _CW_T:      # v47: a cover window is one run
                # bucket_idx is already chronologically ordered (demand was sorted by need_by).
                sub_buckets = []
                cur_sub = [bucket_idx[0]]
                cur_start_ns = need_by[bucket_idx[0]]
                for k_idx in bucket_idx[1:]:
                    if (need_by[k_idx] - cur_start_ns) > sub_wave_bucket_ns:
                        sub_buckets.append(np.array(cur_sub))
                        cur_sub = [k_idx]
                        cur_start_ns = need_by[k_idx]
                    else:
                        cur_sub.append(k_idx)
                if cur_sub:
                    sub_buckets.append(np.array(cur_sub))
            else:
                sub_buckets = [bucket_idx]

            # ═══ AGING-AWARE LOT SPLIT (L7). config aging_aware_lot_split, default OFF. ═══
            # THE DEFECT: an item whose max_aging >= SHORT_AGING_THRESHOLD_H (48h) gets NO
            # sub-wave bucketing above, so a single ~72h wave becomes ONE lot spanning up to
            # 71h. If that exceeds the item's aging WINDOW (max_aging - min_aging) the earliest
            # consumers age out before the lot is fully drawn -- exactly the L7 report (Bead
            # Bundle 48h shelf drawn over 71h). Fix: split any sub-bucket whose consumer span
            # exceeds the window, so each resulting lot fits inside shelf life.
            #   * SURGICAL: only sub-buckets that ACTUALLY violate (span > window + 0.5, the
            #     validator's own tolerance) are touched. Non-violating lots -- including
            #     cap-strip, already 12h-bucketed under its 24h window -- are left byte-for-
            #     byte unchanged, so L9 is not inflated by this pass.
            #   * cap = window - safety. safety = the item's transfer_time (the physical lag
            #     phase4/5 already add to min_aging) unless aging_split_safety_h overrides.
            #   * campaigns preserved: the split is per-item and chronological, so a bead/GT
            #     campaign stays one item's contiguous run, just chunked to shelf life.
            if _aging_split and str(itype).strip().upper() not in _aging_excl and not _CW_T:
                _win_h = max(0.0, float(mx_age) - float(mn_age))
                # 2026-09-29 AGING PULL: the need_by timeline is the PLAN; real draws spread and slide,
                # so a short-shelf (<= aging_split_short_max_h) lot spanning its FULL window ages on the
                # floor (SNCT002: ~30 h of real use per lot vs 24 h shelf). Size those to a fraction.
                if float(mx_age) <= float(cfg.get("aging_split_short_max_h", 0) or 0):
                    _win_h *= float(cfg.get("aging_split_short_frac", 1.0))
                # PRODUCTION-SMOOTHING: for long-shelf compound/calendered roll, cap the split
                # window at the smoothing window (not the 15-day shelf) so a lot cannot carry
                # many days of demand into its first consumer's ramp.
                if str(itype).strip().upper() in _smooth_itypes:
                    _sp_ = _smooth_by_it.get(str(itype).strip().upper(), _smooth_span_h)    # v63: per item type
                    _win_h = min(_win_h, _sp_) if _win_h > 0 else _sp_
                _safety_h = (float(_flat_safety) if _flat_safety is not None
                             else float(meta.get("transfer_time_min", 0.0) or 0.0) / 60.0)
                _cap_h = _win_h - _safety_h
                if _win_h > 0 and _cap_h > 0:
                    _cap_ns = np.timedelta64(int(_cap_h * 3600 * 1e9), "ns")
                    _new_subs = []
                    for _sb in sub_buckets:
                        _sp_h = ((need_by[_sb[-1]] - need_by[_sb[0]]) / np.timedelta64(1, "h")
                                 if len(_sb) > 1 else 0.0)
                        if _sp_h > _win_h + 0.5:                 # ONLY the L7 violators
                            _cur = [_sb[0]]; _start = need_by[_sb[0]]
                            for _k in _sb[1:]:
                                if (need_by[_k] - _start) > _cap_ns:
                                    _new_subs.append(np.array(_cur))
                                    _cur = [_k]; _start = need_by[_k]
                                else:
                                    _cur.append(_k)
                            _new_subs.append(np.array(_cur))
                        else:
                            _new_subs.append(_sb)
                    sub_buckets = _new_subs
                elif _win_h > 0 and _cap_h <= 0:
                    # window <= safety: no split can fit the material inside shelf life.
                    # Declare it; never invent a feasible lot that isn't.
                    _aging_degenerate.add(str(item))

            # ═══ SHORT-SHELF QUANTITY CAP (2026-09-29, config aging_split_qty_frac) ═══
            # need_by is SHIFT-bucketed (a whole day's SNCT002 / cap strip at 07:00-15:00), so the
            # span splits above see an 8 h lot while the floor draws it over 30+ h and the tail
            # expires. Cap a short-shelf lot's QUANTITY at frac x (shelf window) x average hourly
            # demand, splitting the sub-bucket by cumulative qty (row granularity).
            _qf = float(cfg.get("aging_split_qty_frac", 0) or 0)
            _qti = cfg.get("aging_split_qty_itypes")
            if isinstance(_qti, dict):                    # per-item-type fraction {itype: frac}
                _qd = {str(k).strip().upper(): float(v) for k, v in _qti.items()}
                _qf = _qd.get(str(itype).strip().upper(), 0.0)
                _qti = None
            if (_qf > 0 and not _CW_T and float(mx_age) <= float(cfg.get("aging_split_short_max_h", 0) or 0)
                    and str(itype).strip().upper() not in _aging_excl and len(qty) > 0
                    and (not _qti
                         or str(itype).strip().upper() in {str(t).strip().upper() for t in _qti})):
                _hz_h = max(24.0, float((need_by[-1] - need_by[0]) / np.timedelta64(1, "h")) + 24.0)
                _cap_q = float(qty.sum()) / _hz_h * max(1.0, float(mx_age) - float(mn_age)) * _qf
                if _cap_q > 0:
                    _new_subs = []
                    for _sb in sub_buckets:
                        if float(qty[_sb].sum()) <= _cap_q * 1.1:
                            _new_subs.append(_sb); continue
                        _cur, _acc = [], 0.0
                        for _k in _sb:
                            if _cur and _acc + float(qty[_k]) > _cap_q:
                                _new_subs.append(np.array(_cur)); _cur, _acc = [], 0.0
                            _cur.append(_k); _acc += float(qty[_k])
                        if _cur:
                            _new_subs.append(np.array(_cur))
                    sub_buckets = _new_subs

            # ── MPQ POOLING (v26, config mpq_pooling): a sub-bucket whose demand is below the MPQ would be
            # rounded UP on its own (11 lots x 3 batches for 1.1 t of MT658). Instead it is joined with the
            # next sub-bucket(s) of the same item -- also across waves -- until it reaches the MPQ, as long
            # as the joined demand spans no more than the shelf window (max - min aging - transfer) and
            # mpq_pooling.max_span_h. The floor then applies once to real demand, never to phantom surplus.
            if _DEEP_T:
                sub_buckets = [bucket_idx]                         # v44: one deep-roll decision = one lot (no splits)
            _thr = _FRC.get(str(item).strip()) or mpq_min          # v27: a calender item pools up to its spool / dip
            if _DEEP_T:
                _thr = None                                        # v44: deep-roll groups are the lots
            if _MPQ_POOL["on"] and _thr:
                _pw_h = float(mx_age) - float(mn_age) - float(meta.get("transfer_time_min", 0.0) or 0.0) / 60.0
                if str(item).strip() not in _FRC:
                    _pw_h = min(_pw_h, _MPQ_POOL["max_h"])
                elif _FRC_SPAN.get(_FRC_CLS.get(str(item).strip(), "")):
                    # v98 (user 2026-10-13): a 4RC lot pools at most this many hours of demand (still <= one dip,
                    # min roll kept) so one roll does not serve consumers days apart (fabric 24 h / steel 48 h)
                    _pw_h = min(_pw_h, float(_FRC_SPAN[_FRC_CLS.get(str(item).strip(), "")]))
                _cap_t = _MPQ_POOL.get("itype_h", {}).get(str(itype).strip().upper())
                if _cap_t:                                         # BR-37 (v34): IL covers <= 24 h of building need
                    _pw_h = min(_pw_h, float(_cap_t))
                _pw = np.timedelta64(int(max(_pw_h, 0.0) * 3600 * 1e9), "ns")
                _sbs = list(sub_buckets)
                if _pool_carry is not None:
                    if _sbs and (need_by[_sbs[0][-1]] - need_by[_pool_carry[0]]) <= _pw:
                        _sbs[0] = np.concatenate([_pool_carry, _sbs[0]])
                        _MPQ_POOL["merged"] += 1
                    else:
                        _sbs.insert(0, _pool_carry)
                    _pool_carry = None
                _mg = []
                for _sb in _sbs:
                    if (_mg and float(qty[_mg[-1]].sum()) < _thr
                            and (need_by[_sb[-1]] - need_by[_mg[-1][0]]) <= _pw):
                        _mg[-1] = np.concatenate([_mg[-1], _sb])
                        _MPQ_POOL["merged"] += 1
                    else:
                        _mg.append(_sb)
                if _mg and float(qty[_mg[-1]].sum()) < _thr and j < len(wave_ids_in_order):
                    _pool_carry = _mg.pop()                    # wait for the next wave's demand
                sub_buckets = _mg

            # Inner loop: each sub-bucket becomes one batch.
            for sub_bucket_idx in sub_buckets:
                bucket_idx = sub_bucket_idx
                # Aggregate bucket
                qb = qty[bucket_idx]
                nb = need_by[bucket_idx]
                raw_qty = float(qb.sum())
                first_n = nb[0]
                last_n  = nb[-1]
                sku_set = set(sku_arr[bucket_idx].tolist())

                # MPQ floor (round up to MPQ-min if Fraction Allowed)
                if fraction_allowed and mpq_min:
                    lot_qty_full = math.ceil(max(raw_qty, mpq_min) / mpq_min) * mpq_min
                elif mpq_min:
                    lot_qty_full = max(raw_qty, mpq_min)
                else:
                    lot_qty_full = raw_qty
                # ── 4RC ROLL LENGTHS (v27, CTP_Master_FRC): the run is taken from loaded spools / dips of
                # length D. The open residual of this item is used first; a residual left below the minimum
                # roll (an infeasible roll) is made now instead, so every spool / dip ends at 0 or >= min.
                _D = _FRC.get(str(item).strip())
                _deep_now = bool(_DEEP_T and bucket_waves and bucket_waves[0] in _DEEP_T)
                if _deep_now:
                    lot_qty_full = float(_DEEP_T[bucket_waves[0]])       # v44: whole dips decided by the 48 / 72 h rule
                    _FRC_STAT["runs"] += 1                               # v45: _D kept -> one lot per dip (4RC-6)
                if _D and not _deep_now:
                    _mr = float(mpq_min or 300.0)
                    _need = max(lot_qty_full, _mr)
                    if _need <= _frc_res + 1e-6:
                        _left = _frc_res - _need
                    else:
                        _k = math.ceil((_need - _frc_res) / _D - 1e-9)
                        _left = _k * _D + _frc_res - _need
                    if 1e-6 < _left < _mr:                            # infeasible residual -> make it now
                        _need += _left
                        _left = 0.0
                        _FRC_STAT["res_closed"] += 1
                    _frc_res = _left
                    lot_qty_full = _need
                    _FRC_STAT["runs"] += 1

                # SPLIT by lot duration cap (max_dur_min)
                dur_full_min = op_duration_min(lot_qty_full, meta, item_code=item, lot_uom=d_uom)
                n_dur  = max(1, int(math.ceil(dur_full_min / max_dur_min)))
                mhe_total_batch = max(1, int(math.ceil(lot_qty_full / mpq_max))) if mpq_max else 1
                # === MHE-aligned sub-lots (CARCASS + GREEN TYRES ONLY) ===
                # CRITICAL: only Carcass and Green Tyres should split per-MHE because their
                # MHE cart = 20 NOS literally exists in the plant. For mixers (FC/MC), FRC
                # (mother rolls), Tread, Belt, etc. — production is BATCH (not MHE) so
                # over-splitting causes changeover explosion and saturates upstream machines.
                #
                # Past disaster: N_SUB_MAX=40 globally caused FRC to balloon from 750 lots
                # to 5,081 lots (97% util, finished 7/11 — +41 days past horizon).
                #
                # Items where MHE-per-sub-lot makes physical sense:
                #   Carcass, Green Tyres -> N_SUB_MAX_MHE (up to 40 per batch)
                # All other items keep the batch-level cap (N_SUB_MAX_DEFAULT = 6).
                N_SUB_MAX_DEFAULT = 6
                N_SUB_MAX_MHE     = 20   # enough for typical Carcass/GT batches (50-200 NOS)
                MHE_ALIGNED_ITYPES = ("Carcass", "Green Tyres")
                # MIXING (2026-07-23): the per-mixer max_kg from the plant's 'mixing mpq'
                # sheet is a PHYSICAL limit -- a 13,548 kg lot cannot be mixed in one run on
                # a 9,000 kg mixer. The default cap of 6 sub-lots clamps that split (MB352
                # needs ~10) and leaves lots that CANNOT be worked as printed. The FRC
                # disaster that motivated the 6 does not apply here: mixer changeover is
                # 2 min (plant-confirmed), so a deeper split in mixing is cheap.
                MIXING_ITYPES    = ("MASTER COMPOUND", "FINAL COMPOUND")
                N_SUB_MAX_MIXING = int(cfg.get("mixing_max_sublots", 40) or 40)
                if _D:                                       # v27: pieces of at most one spool / dip, one run
                    n_subs = max(n_dur, int(math.ceil(lot_qty_full / _D - 1e-9)))
                elif itype in MHE_ALIGNED_ITYPES and mpq_max:
                    # 1 sub-lot per MHE — matches plant reality
                    n_subs = max(n_dur, min(mhe_total_batch, N_SUB_MAX_MHE))
                elif itype in MIXING_ITYPES and mpq_max:
                    n_subs = max(n_dur, min(mhe_total_batch, N_SUB_MAX_MIXING))
                elif mpq_max:
                    # Other items with MPQ — keep original batch cap to avoid changeover explosion
                    n_subs = max(n_dur, min(mhe_total_batch, N_SUB_MAX_DEFAULT))
                else:
                    # No MPQ — split only by duration cap
                    n_subs = n_dur
                mhe_per_sub = max(1, int(math.ceil(mhe_total_batch / n_subs))) if mpq_max else 1
                sub_qty = lot_qty_full / n_subs
                sub_dur = op_duration_min(sub_qty, meta, item_code=item, lot_uom=d_uom)
                sub_raw = raw_qty / n_subs
                wave_str = ", ".join(bucket_waves)

                sub_ids = []
                batch_id = f"{item}_B{seq:04d}"
                # === DE-PULSE DEADLINES ===
                # The bucket spans the whole wave (e.g. 3 days of smooth demand). Stamping
                # every sub-lot with the bucket's earliest need (first_n = wave start) piles
                # 3 days of deadlines onto one day -> 60k/day spikes the line can't build.
                # Instead, spread each sub-lot's need_by across the demand it actually covers:
                # sub-lot s serves the qty slice [s*sub_raw, (s+1)*sub_raw], so its deadline is
                # the need_by at that cumulative-demand position. Lot SIZE/COUNT is unchanged
                # (no changeover explosion); only the deadlines fan out to the real curing days,
                # which also lands each lot closer to consumption (fresher).
                _cum = np.cumsum(qb) if len(qb) else np.array([0.0])
                _tot = float(_cum[-1]) if len(_cum) else 0.0
                def _need_at(frac_qty):
                    if _tot <= 0 or len(nb) == 0:
                        return first_n
                    pos = int(np.searchsorted(_cum, min(frac_qty, _tot)))
                    return nb[min(pos, len(nb) - 1)]
                for s in range(n_subs):
                    sub_id = f"{item}_L{seq:04d}"
                    seq += 1
                    sub_ids.append(sub_id)
                    _fn_s = _need_at(s * sub_raw)
                    _ln_s = _need_at(min((s + 1) * sub_raw, _tot))
                    if _D:                                   # v27: the run is made at once -> one deadline
                        _fn_s = first_n
                    lots.append({
                        "lot_id":          sub_id,
                        "item_code":       item,
                        "item_type":       itype,
                        "operation":       op_name,
                        "department":      dept,
                        "equipment":       equip,
                        "raw_demand":      round(sub_raw, 2),
                        "lot_qty":         round(sub_qty, 2),
                        "lot_uom":         d_uom,
                        "surplus":         round(sub_qty - sub_raw, 2),
                        "duration_min":    round(sub_dur, 2),
                        "duration_hours":  round(sub_dur/60, 3),
                        "first_need":      pd.Timestamp(_fn_s),
                        "last_need":       pd.Timestamp(_ln_s),
                        "n_blocks":        len(bucket_idx),
                        "n_skus":          len(sku_set),
                        "min_aging_h":     mn_age,
                        "max_aging_h":     mx_age,
                        "transfer_time_h": meta.get("transfer_time_min", 0.0) / 60.0,
                        "mpq_min":         mpq_min,
                        "mpq_uom":         mpq_uom,
                        "fraction_allowed": fraction_allowed,
                        "is_belt":         False,
                        "machines":        machines_str,
                        "n_machines":      n_machines,
                        "wire_type":       "",
                        "campaign_id":     "",
                        "is_campaign_start": False,
                        "is_campaign_end":   False,
                        "wave_ids":        wave_str,
                        "wave_first":      bucket_waves[0],
                        "wave_last":       bucket_waves[-1],
                        "batch_id":        batch_id,
                        "mhe_index":       s,
                        "mhe_total":       mhe_per_sub,
                        "skus_set":        ", ".join(sorted(str(x) for x in sku_set)),
                    })

                # Block-assignments: map only this sub-bucket's demand rows to sub-lots.
                # (FIX #2: each sub-bucket's batch feeds only its own demand window,
                # not the entire wave's demand.)
                n_sub = max(1, len(sub_ids))
                for sub_id in sub_ids:
                    for idx in bucket_idx:
                        block_assignments.append({
                            "lot_id":     sub_id,
                            "item_code":  item,
                            "block_id":   block_id[idx],
                            "sku":        sku_arr[idx],
                            "need_by":    pd.Timestamp(need_by[idx]),
                            "demand_qty": float(qty[idx]) / n_sub,
                        })

            i = j

    if _uncostable_ops:
        _by_item = {}
        for _it, _op, _dp, _pt, _u in _uncostable_ops:
            _by_item[_it] = (_op, _dp, _pt, _u)
        print(f"\n  {'!' * 74}")
        print(f"  !! {len(_by_item)} item(s) have a PRESENT-BUT-BLANK routing row (proc_time / UOM")
        print(f"  !! missing) -> SKIPPED and DECLARED, treated exactly like an absent row (v6-")
        print(f"  !! survivable). These ops are NOT scheduled; NOTHING is invented.")
        for _it, (_op, _dp, _pt, _u) in sorted(_by_item.items()):
            print(f"  !!     {_it:<16s} {_dp:<12s} {_op:<14s} proc_time={_pt!r}, uom={_u!r}")
        print(f"  !! ASK THE PLANT for the missing proc_time / machine / UOM to schedule them.")
        print(f"  {'!' * 74}")
    if _aging_degenerate:
        print(f"\n  {'!' * 74}")
        print(f"  !! AGING-AWARE SPLIT: {len(_aging_degenerate)} item(s) have an aging WINDOW")
        print(f"  !! (max_aging - min_aging) <= the safety margin -- NO lot split can keep the")
        print(f"  !! material inside shelf life for its consumer span. NOT split (would be a")
        print(f"  !! lie). These stay L7-flagged and need a plant aging/routing correction:")
        for _it in sorted(_aging_degenerate):
            print(f"  !!     {_it}")
        print(f"  {'!' * 74}")
    return pd.DataFrame(lots), pd.DataFrame(block_assignments)


def size_wire_campaigns_per_wave(demand, wave_map, rt_meta, aging_map, itype_map,
                                  belt_to_wire, cfg):
    """FRC wire-campaign sizing PER WAVE.

    For each wave, for each wire, produce 8000m mandatory campaigns (with pad).
    Belt lots within campaigns split by 200k-400k mm.
    Aging window: each lot's first_need to last_need bounded by max_aging.
    """
    LOT_MAX_MM  = float(cfg.get("frc_belt_lot_max_mm", 400000))
    LOT_MIN_MM  = float(cfg.get("frc_belt_lot_min_mm", 200000))
    CAMP_MAX_MM = float(cfg.get("frc_campaign_max_m", 8000)) * 1000.0
    PAD         = bool(cfg.get("frc_pad_to_floor", True))
    max_dur_min = cfg["max_lot_duration_h"] * 60.0

    if not belt_to_wire:
        return pd.DataFrame(columns=LOT_COLUMNS), pd.DataFrame()

    belt_items = set(belt_to_wire.keys())
    bd = demand[demand["item_code"].isin(belt_items)].copy()
    if bd.empty:
        return pd.DataFrame(columns=LOT_COLUMNS), pd.DataFrame()
    bd["wire_type"] = bd["item_code"].map(belt_to_wire)
    bd["demand_qty"] = pd.to_numeric(bd["demand_qty"], errors="coerce").fillna(0.0)
    bd = bd[bd["demand_qty"] > 0]
    if bd.empty:
        return pd.DataFrame(columns=LOT_COLUMNS), pd.DataFrame()
    bd["wave_id"] = bd["block_id"].map(wave_map).fillna("W99")

    lots_out = []
    block_assignments = []
    seq_per_item = defaultdict(int)

    # Per (wave, wire), build belt lots, then pack into campaigns of 8000m
    waves_sorted = sorted(bd["wave_id"].unique())
    for wave in waves_sorted:
        wave_demand = bd[bd["wave_id"] == wave]
        for wire, wg in wave_demand.groupby("wire_type", sort=False):
            wg = wg.sort_values("need_by").reset_index(drop=True)

            # Step 1: per-item raw lots within this wave (single item per lot)
            raw_lots = []
            for item, g in wg.groupby("item_code"):
                g = g.sort_values("need_by").reset_index(drop=True)
                meta = rt_meta.get(item)
                if not meta: continue
                mn_age, mx_age = aging_map.get(item, (0.0, 72.0))
                # All demand in one wave (≤3 days) is within max_aging (typically 96h)
                remain = g["demand_qty"].astype(float).values.copy()
                need_arr = g["need_by"].values
                blk_arr = g["block_id"].values
                sku_arr = g["sku"].values
                N = len(g)
                i = 0
                while i < N:
                    if remain[i] <= 1e-6: i += 1; continue
                    qty = 0.0
                    claimed = []
                    sku_set = set()
                    first = need_arr[i]; last = first
                    j = i
                    while j < N:
                        if remain[j] <= 1e-6: j += 1; continue
                        space = LOT_MAX_MM - qty
                        if space <= 1e-6: break
                        take = min(remain[j], space)
                        qty += take
                        claimed.append((blk_arr[j], sku_arr[j], need_arr[j], take))
                        sku_set.add(sku_arr[j])
                        last = need_arr[j]
                        remain[j] -= take
                        if remain[j] <= 1e-6: j += 1
                        if qty >= LOT_MAX_MM - 1e-6: break
                    if qty > 0:
                        # MPQ floor for belt
                        if qty < LOT_MIN_MM:
                            qty = LOT_MIN_MM
                        raw_lots.append({
                            "item": item, "wire": wire,
                            "raw": sum(c[3] for c in claimed), "qty": qty,
                            "claimed": claimed,
                            "first_need": first, "last_need": last,
                            "sku_set": sku_set,
                        })
                    i = j if j > i else i + 1

            if not raw_lots: continue

            # Step 2: pack into campaigns within this wave (each ≤ 8000m)
            raw_lots.sort(key=lambda x: x["first_need"])
            camp_seq = 1
            camp_buffer = []
            camp_qty = 0.0

            def emit_campaign():
                nonlocal camp_seq, camp_buffer, camp_qty
                if not camp_buffer: return
                cid = f"CAMP_{wire}_{wave}_{camp_seq:04d}"
                # Pad last lot if PAD enabled and campaign short
                if PAD and camp_qty < CAMP_MAX_MM:
                    deficit = CAMP_MAX_MM - camp_qty
                    last_lot = camp_buffer[-1]
                    add = min(deficit, LOT_MAX_MM - last_lot["qty"])
                    if add > 0:
                        last_lot["qty"] += add
                        camp_qty += add

                for k, L in enumerate(camp_buffer):
                    item = L["item"]
                    seq_per_item[item] += 1
                    lot_id = f"{item}_L{seq_per_item[item]:04d}"
                    meta = rt_meta[item]
                    itype = itype_map.get(item, "")
                    mn_age, mx_age = aging_map.get(item, (0.0, 72.0))
                    dur = op_duration_min(L["qty"], meta, item_code=item, lot_uom=meta.get("batch_uom","MM"))
                    row = {
                        "lot_id":          lot_id,
                        "item_code":       item,
                        "item_type":       itype,
                        "operation":       meta["operation"],
                        "department":      meta["department"],
                        "equipment":       meta["equipment"],
                        "raw_demand":      round(L["raw"], 2),
                        "lot_qty":         round(L["qty"], 2),
                        "lot_uom":         "MM",
                        "surplus":         round(L["qty"] - L["raw"], 2),
                        "duration_min":    round(dur, 2),
                        "duration_hours":  round(dur/60, 3),
                        "first_need":      L["first_need"],
                        "last_need":       L["last_need"],
                        "n_blocks":        len(L["claimed"]),
                        "n_skus":          len(L["sku_set"]),
                        "min_aging_h":     mn_age,
                        "max_aging_h":     mx_age,
                        "transfer_time_h": meta.get("transfer_time_min", 0.0) / 60.0,
                        "mpq_min":         LOT_MIN_MM,
                        "mpq_uom":         "MM",
                        "fraction_allowed": False,
                        "is_belt":         True,
                        "machines":        ", ".join(meta["machines"]),
                        "n_machines":      len(meta["machines"]),
                        "wire_type":       wire,
                        "campaign_id":     cid,
                        "is_campaign_start": (k == 0),
                        "is_campaign_end":   (k == len(camp_buffer) - 1),
                        "wave_ids":        wave,
                        "wave_first":      wave,
                        "wave_last":       wave,
                        # Belt lots are already MHE-sized (400m mother-roll each)
                        "batch_id":        lot_id,
                        "mhe_index":       0,
                        "mhe_total":       1,
                        "skus_set":        ", ".join(sorted(str(x) for x in L["sku_set"])),
                    }
                    lots_out.append(row)
                    for blk, sku, need, q in L["claimed"]:
                        block_assignments.append({
                            "lot_id": lot_id, "item_code": item,
                            "block_id": blk, "sku": sku, "need_by": need,
                            "demand_qty": float(q),
                        })
                camp_seq += 1
                camp_buffer = []
                camp_qty = 0.0

            for L in raw_lots:
                if camp_qty + L["qty"] > CAMP_MAX_MM and camp_buffer:
                    emit_campaign()
                camp_buffer.append(L)
                camp_qty += L["qty"]
            emit_campaign()

    return pd.DataFrame(lots_out, columns=LOT_COLUMNS), pd.DataFrame(block_assignments)


def main():
    print("\n" + "=" * 65)
    print("  PHASE 2 — Wave-aware Lot Sizing (V6_wave)")
    print("=" * 65)
    cfg = load_cfg(); fp = cfg["files"]

    # CTP DEVIATION #7 — pieces per cut (see op_duration_min). Declared in config, not hidden.
    PIECES_PER_CUT.update({_norm_op(k): int(v)
                           for k, v in (cfg.get("pieces_per_cut") or {}).items()})
    if PIECES_PER_CUT:
        print(f"  PIECES PER CUT (CTP #7): {PIECES_PER_CUT}  — v6 silently assumed 1 cut = "
              f"1 piece for CUTS/MIN; the BELT CUTTER makes BOTH belts in one cut.")

    # ── PER-MIXER MPQ from the plant's 'mixing mpq' sheet (inputs/mixing_mpq.csv, written by
    #    adapt_inputs.mixing_mpq). ALWAYS read (USER 2026-07-23). Supplies the compound floor
    #    (batches) AND the per-mixer MAXIMUM that used to be missing entirely -> MHE inert.
    try:
        _mm = _db_or_csv("mixing_mpq", cfg) if fp.get("mixing_mpq") else pd.read_csv(INPUTS / "mixing_mpq.csv")
        for _, _r in _mm.iterrows():
            _m = str(_r.get("machine", "")).strip()
            if not _m or _m.lower() in ("nan", "none", ""):
                continue
            def _fv(k):
                try:
                    v = float(_r.get(k))
                    return v if v == v and v > 0 else None     # NaN-safe (present-but-blank trap)
                except (TypeError, ValueError):
                    return None
            MIXING_MPQ[_m] = {"min_batches": _fv("min_batches"),
                              "min_kg": _fv("min_kg"), "max_kg": _fv("max_kg")}
    except Exception as _e:
        print(f"  [WARN] mixing_mpq.csv unavailable ({_e!r}) — falling back to the flat "
              f"config mixing_mpq_min_batches, and NO mixer maximum (MHE stays inert).")
    if MIXING_MPQ and bool(cfg.get("use_mixing_mpq_file", False)):
        _mk = [v["max_kg"] for v in MIXING_MPQ.values() if v.get("max_kg")]
        print(f"  PER-MIXER MPQ: {len(MIXING_MPQ)} mixers ({', '.join(MIXING_MPQ)}) — "
              f"max_kg {min(_mk):,.0f}-{max(_mk):,.0f}. A lot is capped to its SMALLEST "
              f"eligible mixer so it is runnable wherever phase5 places it.")
    elif MIXING_MPQ:
        print(f"  PER-MIXER MPQ: loaded {len(MIXING_MPQ)} mixers but NOT IN USE "
              f"(use_mixing_mpq_file: false, USER-DIRECTED). Compounds use the flat "
              f"{(cfg.get('mixing_mpq_min_batches') or {})} x routing batch_size, and there "
              f"is NO per-mixer maximum.")

    # MRP NETTING: prefer Phase 1c's cascaded NET demand if available.
    # Phase 1c walks BOM tree per-block with cascading WIP subtraction (downstream-first).
    # If NET demand exists, use it AND skip the per-item WIP subtraction below
    # (the cascade has already done the work).
    net_gz = OUTPUTS / "phase1_demand_NET_updated.csv.gz"
    csv_gz = OUTPUTS / _suff("phase1_demand.csv.gz")
    if net_gz.exists():
        csv_in = net_gz
        skip_legacy_wip_subtract = True
        print(f"  Loading NET demand (post-MRP-cascade): {csv_in.name}")
    else:
        csv_in = csv_gz if csv_gz.exists() else OUTPUTS / _suff("phase1_demand.csv")
        skip_legacy_wip_subtract = False
    if not csv_in.exists():
        print(f"  ERROR: {csv_in} not found"); return 2
    demand = pd.read_csv(csv_in)
    demand["need_by"]    = pd.to_datetime(demand["need_by"], errors="coerce")
    demand["demand_qty"] = pd.to_numeric(demand["demand_qty"], errors="coerce")
    print(f"  Demand rows: {len(demand):,}  (skip_legacy_wip_subtract={skip_legacy_wip_subtract})")

    # Wave map
    wave_csv = PRIOR_OUTPUTS / "phase1_5_block_to_wave.csv"
    if not wave_csv.exists():
        print(f"  ERROR: {wave_csv} missing — run Phase 1.5 first"); return 2
    wmap = pd.read_csv(wave_csv)
    wave_map = dict(zip(wmap["block_id"], wmap["wave_id"]))
    print(f"  Waves loaded: {len(set(wave_map.values()))}")

    rt    = _db_or_csv("routing", cfg)
    am    = _db_or_csv("aging_master", cfg)
    itype = _db_or_csv("itemtype_master", cfg)
    mpq   = _db_or_csv("mpq", cfg)

    # ═══ CTP #30 — BOM + plan qty, for the MPQ material balance (see _bom_required) ═══════
    # phase2 has never loaded the BOM: it works from ALREADY-EXPLODED demand + routing + mpq.
    # That is precisely why the MPQ round-up could never reach a child — this phase had no way
    # to know what a parent consumes. These two loads are the whole new dependency.
    # GROSS demand (phase1_demand, pre-netting) is deliberate: netting already removed the WIP
    # credited separately in the balance, and crediting it twice understates the shortfall.
    _bom_c = pd.DataFrame(); _gross_dem = pd.DataFrame(); _q_sku = {}
    try:
        _b = _db_or_csv("bom", cfg)
        # COLUMN NAMING IS NOT STABLE ACROSS LOADERS: _db_or_csv/db_loader yields SPACES
        # ('Super parent', 'child quantity'); a bare read_csv of inputs/bom.csv yields
        # UNDERSCORES ('Super_parent', 'child_quantity'). Accept either rather than guess —
        # guessing here silently disables the balance and it fails OPEN.
        _b = _b.rename(columns={c: str(c).strip().replace(" ", "_") for c in _b.columns})
        for _c in ("Super_parent", "Parent", "child"):
            _b[_c] = _b[_c].astype(str).str.strip()
        _b["_cq"] = pd.to_numeric(_b["child_quantity"], errors="coerce")
        _nb = int(_b["_cq"].isna().sum())
        if _nb:
            # PRESENT-BUT-BLANK: a blank cq is UNKNOWN, not zero. v6 survives a missing column,
            # never a blank cell. Declare it; never silently require nothing.
            print(f"  [WARN] CTP #30: {_nb:,} BOM row(s) have a blank/non-numeric "
                  f"child_quantity — requirement UNKNOWN, not zero. EXCLUDED; the balance "
                  f"below is a LOWER BOUND.")
        _bom_c = _b[_b["_cq"] > 0]
        _gz = OUTPUTS / _suff("phase1_demand.csv.gz")
        _gross_dem = pd.read_csv(_gz, low_memory=False)
        _gross_dem["item_code"] = _gross_dem["item_code"].astype(str).str.strip()
        _gross_dem["demand_qty"] = pd.to_numeric(_gross_dem["demand_qty"], errors="coerce")
        _pl = _db_or_csv("plan", cfg)
        _pl["qty"] = pd.to_numeric(_pl["qty"], errors="coerce")
        _q_sku = _pl.groupby(_pl["skuCode"].astype(str).str.strip())["qty"].sum().to_dict()
    except Exception as _e:
        # FAIL LOUD, NOT SILENT. An unrunnable check is not a passing check.
        print(f"  {'!' * 74}")
        print(f"  !! CTP #30: could not load BOM/plan for the MPQ material balance ({_e!r}).")
        print(f"  !! THE BALANCE IS UNVERIFIED — the plan may consume material it never makes.")
        print(f"  !! (mpq_cascade_to_children cannot run without these.)")
        print(f"  {'!' * 74}")
        if bool(cfg.get("mpq_cascade_to_children", False)):
            return 1
    bw_name = fp.get("belt_wire_mapping")
    try:
        bw_df = _db_or_csv("belt_wire_mapping", cfg)
    except Exception:
        bw_df = pd.DataFrame()

    aging_map = build_aging_map(am)
    itype_map = build_itype_map(itype)
    mpq_map   = build_mpq_map(mpq)
    global _MPQ_TBR, _ITEM_TT
    if cfg.get("mpq_tbr_from_db", False):
        try:
            _src = ROOT / "ctp_inputfiles"
            _mt = pd.read_csv(_src / "mpq_tbr_live.csv")
            _MPQ_TBR = {str(r["Item_Type"]).strip().upper():
                        (_parse_mpq(r["Minimum_Run_Qty"]), None, str(r["UOM"]).strip().upper(), False)
                        for _, r in _mt.iterrows()}
            _tt = pd.read_csv(_src / "item_tyre_type_live.csv")
            _ITEM_TT = dict(zip(_tt["item"].astype(str).str.strip(), _tt["tyre_type"]))
            print(f"  LINE-SPECIFIC MPQ: {len(_MPQ_TBR)} TBR rows from CTP_Master_MPQ applied to "
                  f"{sum(1 for v in _ITEM_TT.values() if v == 'TBR'):,} TBR-only items "
                  f"(PCR + shared items keep the PCR sheet)")
        except Exception as _e:
            _MPQ_TBR = None
            print(f"  [WARN] mpq_tbr_from_db: {type(_e).__name__}: {_e} -- PCR sheet used for all")
    global _MPQ_ITEM
    if cfg.get("mpq_item_level", False):
        try:
            _mi = pd.read_csv(INPUTS / "mpq_item.csv")
            _mi = _mi.dropna(subset=["mpq_min"])
            _MPQ_ITEM = {str(a).strip(): (float(b), str(c).strip().upper()) for a, b, c in
                         zip(_mi["item"], _mi["mpq_min"], _mi["uom"])}
            print(f"  ITEM-LEVEL MPQ: {len(_MPQ_ITEM):,} items from CTP_Master_MPQ (inputs/mpq_item.csv) -- PCR + TBR")
        except Exception as _e:
            _MPQ_ITEM = None
            print(f"  [WARN] mpq_item_level: {type(_e).__name__}: {_e} -- item-type MPQ used")
    _FRC.clear()
    if (cfg.get("fourrc_rules") or {}).get("roll_lengths", {}).get("enabled", False):
        try:
            _fl = pd.read_csv(INPUTS / "frc_lengths.csv").dropna(subset=["spool_dip_length_m"])
            _FRC.update({str(a).strip(): float(b) for a, b in zip(_fl["item"], _fl["spool_dip_length_m"])})
            _FRC_CLS.update({str(a).strip(): str(c).strip().lower() for a, c in zip(_fl["item"], _fl["class"])})
            _FRC_SPAN.clear()
            _FRC_SPAN.update({str(k).strip().lower(): float(v) for k, v in
                              (((cfg.get("fourrc_rules") or {}).get("pool_span_h") or {}).items()) if v})
            if _FRC_SPAN:
                print(f"  4RC POOL SPAN v98: a calender lot pools at most {_FRC_SPAN} h of demand")
            _dr = (cfg.get("fourrc_rules") or {}).get("deep_roll") or {}
            _DEEP.update(on=bool(_dr.get("enabled", False)), h48=float(_dr.get("horizon_h", 48)),
                         h72=float(_dr.get("extend_h", 72)), mode=str(_dr.get("mode", "48_72")).strip().lower(),
                         classes={str(x).strip().lower() for x in (_dr.get("classes") or ["fabric"])})
            print(f"  4RC ROLL LENGTHS: {len(_FRC)} calender items made in runs of <= one spool / dip (CTP_Master_FRC)"
                  + (f"; DEEP ROLL ON for {sorted(_DEEP['classes'])} ({_DEEP['h48']:g} h, extended to {_DEEP['h72']:g} h)" if _DEEP["on"] else ""))
        except Exception as _e:
            print(f"  [WARN] 4RC roll lengths: {type(_e).__name__}: {_e} -- MPQ only")
    _mv = cfg.get("mixing_v47") or {}
    _MIXCW.update(on=bool(_mv.get("enabled", False) and _mv.get("cover_windows", True)), lots=0, items=0, short=set(),
                  w=float(_mv.get("cover_h", 15)), w_long=float(_mv.get("cover_h_long", 24)),
                  margin_h=float(_mv.get("shelf_margin_h", 2)),
                  exempt={str(x).strip() for x in (_mv.get("exempt") or [])})
    if _MIXCW["on"]:
        _MIXCW["long"] = {str(x).strip() for x in (_mv.get("long_items") or [])} | (
            _fourrc_compounds() if _mv.get("long_fourrc_compounds", True) else set())
        print(f"  v47 MIXING COVER WINDOWS: one run = the next {_MIXCW['w']:g} h of demand ({_MIXCW['w_long']:g} h for "
              f"{len(_MIXCW['long'])} IL / 4RC compounds: {sorted(_MIXCW['long'])}); exempt {sorted(_MIXCW['exempt'])}")
    _pc = cfg.get("mpq_pooling") or {}
    _MPQ_POOL["on"] = bool(_pc.get("enabled", False))
    _MPQ_POOL["max_h"] = float(_pc.get("max_span_h", 96))
    _MPQ_POOL["itype_h"] = {str(k).strip().upper(): float(v) for k, v in (_pc.get("max_span_h_itypes") or {}).items()}
    rt_meta   = build_routing_meta(rt)
    _MIX_CAL.clear()
    _mc = cfg.get("mixer_capacity_calibration") or {}
    if _mc.get("enabled", False):
        for _op, _tgt in (("FINAL MIXING", _mc.get("final_batches_per_mixer_day")),
                          ("MASTER MIXING", _mc.get("master_batches_per_mixer_day"))):
            _r = [(m["proc_time"] / (60.0 if m.get("proc_uom") == "SEC/BATCH" else 1.0)) / m["efficiency"]
                  for m in rt_meta.values()
                  if str(m.get("operation", "")).strip().upper() == _op
                  and m.get("proc_uom") in ("MIN/BATCH", "SEC/BATCH") and m.get("proc_time", 0) > 0]
            if _tgt and _r:
                import statistics as _st
                _med = float(_st.median(_r))
                _MIX_CAL[_op] = (1440.0 / float(_tgt)) / _med
                print(f"  [MIXER CALIBRATION] {_op}: routing median {_med:.2f} min/batch (with efficiency) -> "
                      f"x{_MIX_CAL[_op]:.2f} = {1440.0 / float(_tgt):.2f} min/batch on average "
                      f"= {float(_tgt):g} batches per mixer per day")
    belt_to_wire = build_belt_wire_map(bw_df)

    print(f"  Belt mapping: {len(belt_to_wire)} items, {len(set(belt_to_wire.values()))} wires")
    print(f"  Wave duration: {WAVE_DURATION_DAYS} days")

    # ══════════════════════════════════════════════════════════════════════════════════
    # THE MHE SUB-LOT MODEL IS INERT ON CTP. (A) DEFECT -- REPORTING ONLY. 2026-07-14.
    # ══════════════════════════════════════════════════════════════════════════════════
    # size_lots_waveaware() carries TWO tuning mechanisms, and BOTH are keyed on mpq_max:
    #     mhe_total_batch    = ceil(lot_qty_full / mpq_max)                        (:646)
    #     MHE_ALIGNED_ITYPES = ("Carcass", "Green Tyres") -> N_SUB_MAX_MHE  = 20   (:662)
    #     every other item with an MPQ max                -> N_SUB_MAX_DEFAULT = 6 (:665)
    # `mpq_max` is read from the MPQ master's `Maximum Run Qty` column.
    #
    # **CTP'S MPQ FILE HAS NO SUCH COLUMN -- AND THAT IS CORRECT.** The plant has no max
    # MPQ, and adapt_inputs.mpq() OMITS the column DELIBERATELY (writing it blank would
    # push NaN into `int(ceil(qty / NaN))` and detonate; see that docstring). So
    # build_mpq_map's `except (..., KeyError): mx = None` fires for EVERY item type, and:
    #     - mpq_max is None everywhere        -> mhe_total == 1 on EVERY lot
    #     - n_subs collapses to n_dur         -> lots split by the 8h duration cap ALONE
    #     - N_SUB_MAX_MHE and N_SUB_MAX_DEFAULT are DEAD CODE. They do nothing.
    #
    # This is v6's own designed no-max behaviour, so it is NOT a wrong answer. What WAS
    # wrong: TWO WHOLE TUNING MECHANISMS WERE DEAD AND NOTHING SAID SO. Anyone tuning
    # N_SUB_MAX_MHE would have got zero response from the scheduler and no explanation.
    # Silence about a dead control is how people lose days.
    #
    # !! THE MODEL IS NOT DELETED, AND NO MHE CAPACITY IS INVENTED. It is only made to
    #    ANNOUNCE that it is doing nothing. To make it live, the PLANT must supply a real
    #    MHE capacity in the MPQ master.
    _has_max_col = "Maximum Run Qty" in list(getattr(mpq, "columns", []))
    _n_types_with_max = sum(1 for v in mpq_map.values()
                            if len(v) >= 2 and v[1] is not None)
    MHE_INERT = (_n_types_with_max == 0)
    if MHE_INERT:
        print(f"\n  {'!' * 74}")
        print(f"  !! MHE MODEL INERT: no Maximum Run Qty in the MPQ master -> mhe_total = 1")
        print(f"  !! on ALL lots. ('Maximum Run Qty' column present: {_has_max_col}; "
              f"item types with a max: {_n_types_with_max} of {len(mpq_map)}.)")
        print(f"  !!")
        print(f"  !! DEAD AS A RESULT: mhe_total_batch, N_SUB_MAX_MHE (=20),")
        print(f"  !! N_SUB_MAX_DEFAULT (=6), and the MHE_ALIGNED_ITYPES carve-out for")
        print(f"  !! Carcass / Green Tyres. Lots are split by the {cfg['max_lot_duration_h']}h "
              f"duration cap ALONE.")
        print(f"  !!")
        print(f"  !! Either SUPPLY AN MHE CAPACITY (plant data, in the MPQ master) or DELETE")
        print(f"  !! the model. Nothing has been invented here.")
        print(f"  {'!' * 74}")
    else:
        print(f"  MHE model ACTIVE: {_n_types_with_max} item type(s) carry a Maximum Run Qty.")

    # Belt sizing (FRC) — per wave
    print(f"\n  Sizing FRC wire campaigns per wave...")
    belt_lots, belt_blocks = size_wire_campaigns_per_wave(
        demand, wave_map, rt_meta, aging_map, itype_map, belt_to_wire, cfg)
    print(f"  Belt lots: {len(belt_lots):,}")
    if not belt_lots.empty:
        print(f"  Belt campaigns: {belt_lots['campaign_id'].nunique()}")

    # Non-belt sizing — wave-aware with carry-over and lot-size cap
    print(f"\n  Sizing non-belt lots (wave + carry-over + lot-size cap)...")
    skip = set(belt_to_wire.keys()) if belt_to_wire else set()

    # CURING items (the tyre SKUs) are TERMINAL — scheduled by phase6 from plan.csv, never
    # manufactured here. Skip them so the phase1 curing DEMAND rows do not create phantom
    # manufacturing lots (they would otherwise be placed by phase5, +11,893 lots / +0.6 d).
    # A separate, controlled curing-lot REPRESENTATION is appended after sizing (for L11),
    # tagged operation=CURING and skipped by phase5. See `emit_curing_representation_lots`.
    _curing_items = {rp for rp, m in rt_meta.items()
                     if str((m or {}).get("operation", "")).strip().upper() == "CURING"}
    skip |= _curing_items
    if _curing_items:
        print(f"  CURING items skipped from manufacturing lot-sizing: {len(_curing_items)} "
              f"(terminal; scheduled by phase6 from plan.csv)")

    # ══════════════════════════════════════════════════════════════════════════════════
    # MASTER MIXING IS NEVER SCHEDULED.  (B) POLICY -- FLAGGED, DEFAULT OFF (= v6).
    # ══════════════════════════════════════════════════════════════════════════════════
    # v6 carries a NAMED, DELIBERATE modelling assumption right here:
    #     "MASTER COMPOUND is treated as always available -- never produced."
    # Every MC item goes into the skip set, so ZERO master-compound lots are created and
    # MC never reaches the floor schedule.
    #
    # !! THIS IS NOT A BUG AND I AM NOT TREATING IT AS ONE. The code executes its stated
    #    rule, correctly and on purpose. Changing it is therefore (B) POLICY -- it needs a
    #    DECISION, not a patch. So it gets a FLAG, and THE DEFAULT IS v6's BEHAVIOUR.
    #
    # WHAT THE ASSUMPTION COSTS (reported loudly below, every single run, so it can never
    # be quietly forgotten):
    #     - master-compound lots created            : ZERO
    #     - master-compound demand left unscheduled : ~3.5 MILLION kg
    #     - Banburys 270M / 430-1 / 430-2 / 440M    : ABSENT from the utilisation report
    #                                                 ENTIRELY -- as if they did not exist
    #     - and the routing HAS 6,972 fully-populated MASTER MIXING rows. The data to
    #       schedule it is right there. We are choosing not to.
    #
    # !! WARNING TO WHOEVER FLIPS THIS ON -- READ THIS FIRST.
    #    Turning `schedule_master_mixing` ON makes the schedule **LONGER, NOT SHORTER.**
    #    It does not "unlock capacity". It ADDS A WHOLE UPSTREAM STAGE that is simply
    #    ABSENT today: 3.5M kg of mixing that must now be produced, sequenced, and aged
    #    BEFORE final mixing can start. Every downstream lot inherits a new predecessor.
    #    Expect the horizon to move OUT. That is the honest cost of modelling reality, and
    #    it is exactly why this is a decision and not a fix.
    schedule_master_mixing = bool(cfg.get("schedule_master_mixing", False))
    mc_skip = {it for it, t in itype_map.items() if str(t).strip().upper() == "MASTER COMPOUND"}

    if mc_skip and not schedule_master_mixing:
        skip |= mc_skip
        # ---- REPORT THE SKIPPED TONNAGE LOUDLY. EVERY RUN. NO EXCEPTIONS. ----
        _mc_dem = demand[demand["item_code"].isin(mc_skip)]
        _mc_qty = float(pd.to_numeric(_mc_dem["demand_qty"], errors="coerce").fillna(0).sum())
        _mc_items_live = sorted(set(_mc_dem["item_code"].astype(str)))
        _mc_uoms = sorted(set(_mc_dem["demand_uom"].astype(str))) if "demand_uom" in _mc_dem else []
        # Machines the SKIPPED master-mixing work would have used...
        _mc_mach = {m for it in _mc_items_live
                      for m in (rt_meta.get(it, {}) or {}).get("machines", [])}
        # ...versus machines that STILL get work from everything we DO schedule.
        # BE PRECISE HERE. A mixer that also does FINAL mixing is NOT absent from the
        # report -- it is merely UNDER-LOADED. Only a mixer used EXCLUSIVELY by master
        # mixing vanishes completely. Blurring the two would overstate the damage, and an
        # overstated warning gets ignored just as fast as a missing one.
        _live_items = {str(i) for i in demand["item_code"].unique()} - set(mc_skip)
        _live_mach = {m for it in _live_items
                        for m in (rt_meta.get(it, {}) or {}).get("machines", [])}
        _gone = sorted(_mc_mach - _live_mach)     # never scheduled at all
        _partial = sorted(_mc_mach & _live_mach)  # still scheduled, but short of master work
        print(f"\n  {'#' * 74}")
        print(f"  ## MASTER MIXING NOT SCHEDULED  (v6 assumption: 'always available')")
        print(f"  ##   schedule_master_mixing = False   <- v6 default. This is a POLICY (B).")
        print(f"  ##")
        print(f"  ##   MASTER COMPOUND items skipped     : {len(mc_skip):,} "
              f"({len(_mc_items_live):,} of them carry real demand)")
        print(f"  ##   DEMAND LEFT UNSCHEDULED           : {_mc_qty:,.0f} "
              f"{'/'.join(_mc_uoms) if _mc_uoms else 'units'}")
        print(f"  ##   Mixers ABSENT FROM THE SCHEDULE ENTIRELY (master mixing is ALL they do):")
        print(f"  ##       {', '.join(_gone) if _gone else '(none)'}")
        print(f"  ##   Mixers merely UNDER-LOADED (they also do final mixing, so they still"
              f" appear):")
        print(f"  ##       {', '.join(_partial) if _partial else '(none)'}")
        print(f"  ##")
        print(f"  ##   The {len(_gone)} machine(s) above will NOT appear in the utilisation "
              f"report AT ALL.")
        print(f"  ##   The routing HAS the MASTER MIXING rows; we are CHOOSING not to use them.")
        print(f"  ##   Set `schedule_master_mixing: true` in config.yaml to model it -- but")
        print(f"  ##   note it makes the schedule LONGER (it adds an upstream stage).")
        print(f"  {'#' * 74}")
    elif mc_skip and schedule_master_mixing:
        _mc_dem = demand[demand["item_code"].isin(mc_skip)]
        _mc_qty = float(pd.to_numeric(_mc_dem["demand_qty"], errors="coerce").fillna(0).sum())
        _mc_live = sorted({str(i) for i in _mc_dem["item_code"].unique()})
        print(f"\n  {'#' * 74}")
        print(f"  ## MASTER MIXING **IS** BEING SCHEDULED  (schedule_master_mixing = true)")
        print(f"  ##   This DEPARTS FROM v6, deliberately and by config.")
        print(f"  ##   {len(mc_skip):,} MASTER COMPOUND items are now PRODUCED, carrying")
        print(f"  ##   {_mc_qty:,.0f} units of demand onto the Banburys as a NEW UPSTREAM STAGE.")
        print(f"  ##   EXPECT A LONGER SCHEDULE, not a shorter one.")
        print(f"  {'#' * 74}")

        # ---- PRE-FLIGHT: THE ON PATH IS BLOCKED BY A DATA HOLE. SAY SO *HERE*. ----------
        # MEASURED 2026-07-14, the first time this flag was ever actually turned on: phase2
        # CRASHED, ~600 lines deep, inside op_duration_min:
        #     ValueError: UNRECOGNISED proc_time_UOM 'NAN' on item 'MTCM313'
        #
        # The crash is CORRECT -- op_duration_min refuses to guess a duration, exactly as it
        # should. But dying deep in the sizer with no context is a terrible way to learn that
        # your PLANT DATA is incomplete. So we check it UP FRONT and name the items.
        #
        # !! THIS CORRECTS A PREMISE. The routing does have 6,972 MASTER MIXING rows, but they
        #    are NOT all "fully populated": of the 89 master compounds that carry real demand,
        #    2 (MTCM313, TBA827) have routing rows with NO proc_time and NO proc_time_UOM.
        #    Master mixing therefore CANNOT simply be switched on today -- it needs those
        #    cycle times from the plant first. NOTHING IS INVENTED HERE.
        # !! NOTE THE KEY NAMES. build_routing_meta() stores the UOM as `proc_uom`, NOT
        #    `proc_time_UOM`. My first version of this check used the routing's column name
        #    and got "" back for EVERY item -- it reported 17 healthy master compounds as
        #    broken. A .get() with the wrong key does not raise; it just quietly returns the
        #    default and lies to you. Same family of trap as the blank cell.
        _uncostable = []
        for _it in _mc_live:
            _m = rt_meta.get(_it)
            if not _m:
                _uncostable.append((_it, "no routing row at all"))
                continue
            _u = str(_m.get("proc_uom", "")).strip().upper()
            _pt = _m.get("proc_time")
            # float(NaN) does NOT raise, so a blank proc_time arrives here as NaN, not as a
            # missing key. `_pt != _pt` is the NaN test that actually works.
            _pt_bad = (_pt is None) or (_pt != _pt) or (float(_pt) <= 0.0)
            if _u in ("", "NAN", "NONE") or _pt_bad:
                _uncostable.append((_it, f"proc_time={_pt!r}, proc_uom={_u!r}"))
        if _uncostable:
            # USER-DIRECTED (2026-07-16): a master compound with NO cycle time is SKIPPED and
            # ASSUMED ALREADY AVAILABLE (on-hand stock, not produced) -- NOT a hard stop.
            # NOTHING IS INVENTED: we do not guess a duration; we treat the item as available
            # (the same "always available" assumption v6 makes for ALL master compound, applied
            # now to just the uncostable ones). Declared LOUDLY so it is never forgotten, and
            # the plant can still supply the missing cycle times to schedule them properly.
            _unc_codes = {c for c, _ in _uncostable}
            skip |= _unc_codes
            _uq = pd.to_numeric(
                _mc_dem[_mc_dem["item_code"].isin(_unc_codes)]["demand_qty"],
                errors="coerce").fillna(0)
            print(f"\n  {'!' * 74}")
            print(f"  !! {len(_uncostable)} master compound(s) have NO cycle time -> SKIPPED and")
            print(f"  !! ASSUMED ALREADY AVAILABLE (not produced). Declared, not invented.")
            for _it, _why in _uncostable:
                _q = float(pd.to_numeric(
                    _mc_dem[_mc_dem["item_code"] == _it]["demand_qty"],
                    errors="coerce").fillna(0).sum())
                print(f"  !!     {_it:<14s} demand={_q:>10,.1f}  ({_why})")
            print(f"  !! total demand assumed-available: {float(_uq.sum()):,.1f} units.")
            print(f"  !! ASK THE PLANT for their cycle times to schedule them for real.")
            print(f"  {'!' * 74}")
            # fall through -- the remaining (costable) master compounds schedule normally.

    # ═══ CTP DEVIATION #30 — MPQ CASCADE. (A) DEFECT REPAIR. DEFAULT OFF = v6 bit-for-bit ══
    # See _bom_required() above for the full defect. Short version: the MPQ round-up on a
    # PARENT never becomes demand on its CHILD, so the plan consumes material it never makes
    # (53 items, 124,485 units; Bead Bundle 73,023).
    #
    # THE RULE WHEN ON: derive child demand from the parent's MADE qty, not its DEMAND, and
    # iterate to a fixpoint. Each pass re-derives from the ORIGINAL demand (x_{n+1} = F(x_0
    # scaled)), never from its own output — compounding would double-count the prior pass.
    #
    # IT PROVABLY CANNOT EXPLODE: made = max(raw, mpq_min) is monotone and 1-LIPSCHITZ in raw
    # (d/dx max(x,m) is 0 or 1), so a delta of child demand lifts child production by AT MOST
    # that delta, on a finite-depth DAG (495/495 items, 0 cycles). It is also SELF-DAMPING:
    # cascaded demand pushes buckets ABOVE their floors, which REMOVES surplus. MEASURED
    # offline: converges in 2 passes; total MPQ surplus FALLS 169,334 -> 167,598 even as
    # production rises +132,128 (+0.46%).
    #
    # SIMPLIFICATION, DECLARED: the uplift is applied PRO-RATA across a child's existing
    # demand rows, preserving their need_by shape. The exact attribution (decompose by each
    # parent BUCKET's own inflation and attach at that parent's need_by) is finer-grained;
    # pro-rata is chosen because it is auditable and because a child's demand rows already
    # mirror its parents' timing. If a child's parents inflate very unevenly ACROSS TIME this
    # under/over-states WHEN the extra is needed — never HOW MUCH. VERIFY against
    # outputs2/phase2_bom_shortfall.csv: with the flag ON that report must go to ZERO.
    #
    # !! COST — THIS MAKES THE PLAN LONGER, AND THAT IS THE CORRECT ANSWER. The bundles are
    #    not there. MEASURED offline: +62.3 h of 12,103 h (+0.51%); Bead Bundle +75,860 units
    #    (+42.9 h on 1201, a SINGLE winder: 62.2% -> 68.2% true util, 238.6 h idle within its
    #    used days); CALANDARED ROLL +37,781 (+15.6 h). BUILDING — THE CONSTRAINT — GETS
    #    +0.00 h, because Green Tyres has NO MPQ and is the parent of every inflating item, so
    #    nothing cascades into it. 0 new lots => 0 new changeovers (all 210 bundle buckets stay
    #    under max_lot_duration_h=8.0). Makespan effect NOT predicted — measure it.
    #
    # !! BEFORE YOU TRUST THIS, ONE PLANT QUESTION DECIDES WHETHER IT IS EVEN RIGHT:
    #    "Bead apex minimum run is 400 Nos. If only 250 are needed, does the machine PHYSICALLY
    #     PRODUCE 400 apexes (consuming 400 bundles), or produce 250 and just HOLD THE LINE for
    #     the minimum run time?"  If the answer is TIME, this cascade is VOID — lot_qty should
    #    stay 250 and only duration_min should reflect the floor, and the shortfall vanishes
    #    with zero extra winding (apex 750,930 vs bundle supply 755,503 = slack +4,573).
    #    ALSO UNRESOLVED: Bead Apex MaxAging is 24 h, so the 77,596 surplus apexes CANNOT go to
    #    stock — cascading schedules real bundles to make apexes that EXPIRE. Apex already has
    #    1,695 expiry violations (2nd worst item type). Correct, and deliberately wasteful.
    _cascade   = bool(cfg.get("mpq_cascade_to_children", False))
    _max_pass  = int(cfg.get("mpq_cascade_max_passes", 6))
    _dem_work  = demand
    _converged = not _cascade
    # Usable opening WIP, age-gated at plan_start with the plant's OWN shelf life. Computed
    # ONCE: it is a property of the floor at t=0 and does not change with the cascade.
    # It is SUPPLY, and the shortfall must be measured against supply — see the loop.
    _wip_alive = {}
    if _cascade:
        try:
            _iv = _db_or_csv("inventory", cfg)
            _iv["_it"] = _iv["itemcode"].astype(str).str.strip()
            _iv["_q"] = pd.to_numeric(_iv["inventory"], errors="coerce")
            _iv["_pt"] = pd.to_datetime(_iv["produced_time"], errors="coerce", format="mixed")
            _shelf = dict(zip(am["ItemCode"].astype(str).str.strip(),
                              pd.to_numeric(am["MaxAging"], errors="coerce")))
            _t0 = pd.Timestamp(str(_pl["startTime"].min()))
            for _it, _q, _pt in zip(_iv["_it"], _iv["_q"], _iv["_pt"]):
                if not (_q == _q) or _q <= 0:
                    continue
                _sl = _shelf.get(_it)
                # NO aging row => silent 365-day shelf life (MEMORY: immortal items).
                # Blank produced_time => declare, don't guess: keep it, don't kill it.
                if _sl is None or not (_sl == _sl) or pd.isna(_pt) \
                   or (_t0 - _pt).total_seconds() / 3600.0 <= _sl:
                    _wip_alive[_it] = _wip_alive.get(_it, 0.0) + _q
            print(f"  MPQ CASCADE: usable opening WIP credited on {len(_wip_alive):,} items "
                  f"({sum(_wip_alive.values()):,.0f} units, age-gated at plan_start)")
        except Exception as _e:
            # No WIP credit => the balance is STRICTER (more uplift), never falsely clean.
            print(f"  [WARN] MPQ CASCADE: opening-WIP credit unavailable ({_e!r}). "
                  f"Cascading WITHOUT it — the uplift will be an OVER-estimate, not an under.")
    for _pass in range(_max_pass if _cascade else 1):
        _MPQ_POOL["merged"] = 0
        other_lots, other_blocks = size_lots_waveaware(
            _dem_work, wave_map, rt_meta, aging_map, itype_map, mpq_map, cfg, skip_items=skip)
        if _FRC:
            print(f"  [4RC ROLL LENGTHS] {_FRC_STAT['runs']:,} calender runs sized to spools / dips; "
                  f"{_FRC_STAT['res_closed']} residuals below the minimum roll made at once instead")
            _FRC_STAT.update(runs=0, res_closed=0)
            if _DEEP["on"]:
                print(f"  [DEEP ROLL v44] {_DEEP['lots']} dip lots = {_DEEP['rolls']} full dips decided on the {_DEEP['h48']:g} h "
                      f"requirement; {_DEEP['ext72']} decisions extended to {_DEEP['h72']:g} h (48 h need < one dip)"
                      + (f"; v46 mode max_aging: higher of 48 h / max-aging need in full dips, max-aging need decided "
                         f"{_DEEP['aging_won']} of {_DEEP['lots']}" if _DEEP["mode"] == "max_aging" else ""))
        if _MIXCW["on"]:
            print(f"  [MIXING COVER v47] {_MIXCW['items']} compounds sized in {_MIXCW['lots']} cover windows "
                  f"({_MIXCW['w']:g} h / {_MIXCW['w_long']:g} h); window cut to shelf life for: {sorted(_MIXCW['short']) or 'none'}")
            _MIXCW.update(items=0, lots=0, short=set())
        if _MPQ_POOL["on"]:
            print(f"  [MPQ POOLING] {_MPQ_POOL['merged']:,} small demand buckets joined into a neighbour "
                  f"(same item, within shelf window / {_MPQ_POOL['max_h']:g} h) before the MPQ floor")
        if not _cascade:
            break
        _req, _made = _bom_required(other_lots, _bom_c, _gross_dem, _q_sku)
        # Uplift a child by its UNCOVERED SHORTFALL only:
        #       shortfall = required_by_parents_MADE  -  made  -  usable_opening_WIP
        #
        # !! DO NOT SCALE DEMAND UP TO `required`. I did that first and it was WRONG: `_req`
        #    is GROSS-BOM-derived while `demand` here is NET (phase1c already subtracted the
        #    opening WIP). Scaling net demand up to a gross requirement ADDS THE WIP NETTING
        #    BACK on every child, every pass — it inflated 260 items by +1,218,331 units on
        #    pass 0 alone (vs +132,128 expected for the WHOLE cascade) and would not converge.
        #    The comparison must be like-for-like: a requirement against SUPPLY (made + WIP),
        #    never against net demand.
        #
        # Never uplift DOWN: this repairs an UNDER-statement and must not become a back-door
        # demand cut (MEMORY: never delete work that has quantity).
        _base = _dem_work.groupby("item_code")["demand_qty"].sum()
        _add = {}
        for _c, _r in _req.items():
            _short = _r - float(_made.get(_c, 0.0)) - float(_wip_alive.get(_c, 0.0))
            if _short > 1.0 and float(_base.get(_c, 0.0)) > 0:
                _add[_c] = _short
        if not _add:
            _converged = True
            print(f"  MPQ CASCADE pass {_pass}: CONVERGED — every child now covers its "
                  f"parents' MADE quantity")
            break
        # spread each child's uplift PRO-RATA across its own demand rows, preserving need_by shape
        _nxt = _dem_work.copy()
        _tot_by = _nxt.groupby("item_code")["demand_qty"].transform("sum")
        _addv = _nxt["item_code"].map(_add).fillna(0.0)
        _share = (_nxt["demand_qty"] / _tot_by.where(_tot_by > 0, 1.0)).fillna(0.0)
        _nxt["demand_qty"] = _nxt["demand_qty"] + _addv * _share
        _delta = float(_nxt["demand_qty"].sum() - _dem_work["demand_qty"].sum())
        _fac = _add
        _detail = ""
        if len(_fac) <= 5:
            _detail = "  [" + ", ".join(f"{k}:+{v:.1f}" for k, v in sorted(_fac.items())) + "]"
        print(f"  MPQ CASCADE pass {_pass}: {len(_fac):,} child item(s) uplifted, "
              f"child demand moved {_delta:+,.0f} units{_detail}")
        _dem_work = _nxt
        if abs(_delta) < 1.0:
            _converged = True
            break
    if _MPQ_UNIT_SKIPS:
        _us = pd.DataFrame(_MPQ_UNIT_SKIPS, columns=["item", "itype", "floor", "mpq_uom", "item_uom"]).drop_duplicates()
        print(f"  MPQ UNIT GUARD: {len(_us)} item(s) NOT floored -- MPQ unit differs from the item unit: "
              + ", ".join(f"{r.item} ({r.floor:g} {r.mpq_uom} vs {r.item_uom})" for r in _us.head(12).itertuples()))
    if _cascade and not _converged:
        print(f"  {'!' * 74}")
        print(f"  !! MPQ CASCADE DID NOT CONVERGE in {_max_pass} passes. It is provably")
        print(f"  !! non-expansive (max(raw,mpq) is 1-Lipschitz on an acyclic BOM), so this")
        print(f"  !! means a BOM CYCLE or a ratio > 1 inside a loop. NOT ignoring it.")
        print(f"  {'!' * 74}")
        return 1
    print(f"  Non-belt lots: {len(other_lots):,}")

    if belt_lots.empty:
        belt_lots = pd.DataFrame(columns=LOT_COLUMNS)
    if not other_lots.empty:
        for c in LOT_COLUMNS:
            if c not in other_lots.columns:
                if c in ("wire_type","campaign_id","wave_ids","wave_first","wave_last","batch_id"):
                    other_lots[c] = ""
                elif c in ("mhe_index","mhe_total"):
                    other_lots[c] = 0 if c == "mhe_index" else 1
                else:
                    other_lots[c] = False
        other_lots = other_lots[LOT_COLUMNS]
    if not belt_lots.empty:
        belt_lots = belt_lots[LOT_COLUMNS]

    lots = pd.concat([belt_lots, other_lots], ignore_index=True)

    # ═══ CTP #30 — THE MATERIAL BALANCE. ════════════════════════════════════════════════
    # USER-DIRECTED 2026-07-23: REVERTED TO v6 (v6 has NO such report). Gated OFF by
    # `mpq_material_balance_report` (default False). Set it true to restore the CTP #30 check.
    # This is the check that CAN fail (see _bom_required): `required` is BOM x made-qty,
    # `available` is the lot table + inventory — two INDEPENDENT sources. Contrast the three
    # gates that missed this entirely: phase5's _material_ready_ns (`need` is a tautology),
    # the aging check (reports, never rejects), phase4's revision loop (provably empty).
    # WITH mpq_cascade_to_children ON, THIS REPORT MUST GO TO ZERO. That is the proof.
    if bool(cfg.get("mpq_material_balance_report", False)) and len(_bom_c) and len(_gross_dem):
        try:
            _req, _made_m = _bom_required(lots, _bom_c, _gross_dem, _q_sku)
            _wip = {}
            try:
                _iv = _db_or_csv("inventory", cfg)
                _iv["_it"] = _iv["itemcode"].astype(str).str.strip()
                _iv["_q"] = pd.to_numeric(_iv["inventory"], errors="coerce")
                _iv["_pt"] = pd.to_datetime(_iv["produced_time"], errors="coerce", format="mixed")
                _shelf = dict(zip(am["ItemCode"].astype(str).str.strip(),
                                  pd.to_numeric(am["MaxAging"], errors="coerce")))
                _t0 = pd.Timestamp(str(_pl["startTime"].min()))
                for _it, _q, _pt in zip(_iv["_it"], _iv["_q"], _iv["_pt"]):
                    if not (_q == _q) or _q <= 0:
                        continue
                    _sl = _shelf.get(_it)
                    # NO aging row => silent 365-day shelf life (MEMORY: immortal items).
                    # Blank produced_time => declare, don't guess: count it, do not kill it.
                    if _sl is None or not (_sl == _sl) or pd.isna(_pt) \
                       or (_t0 - _pt).total_seconds() / 3600.0 <= _sl:
                        _wip[_it] = _wip.get(_it, 0.0) + _q
            except Exception:
                pass    # no WIP credit => the balance is STRICTER, never falsely clean
            _rows = [{"item_code": c, "item_type": itype_map.get(c, ""),
                      "required_by_parents_MADE": round(r, 2),
                      "made": round(float(_made_m.get(c, 0.0)), 2),
                      "usable_opening_wip": round(_wip.get(c, 0.0), 2),
                      "shortfall": round(r - float(_made_m.get(c, 0.0)) - _wip.get(c, 0.0), 2)}
                     for c, r in _req.items()
                     if r - float(_made_m.get(c, 0.0)) - _wip.get(c, 0.0) > 1.0]
            if _rows:
                _sf = pd.DataFrame(_rows).sort_values("shortfall", ascending=False)
                _tot = float(_sf["shortfall"].sum())
                print(f"\n  {'!' * 74}")
                print(f"  !! MPQ OVER-PRODUCTION DOES NOT CASCADE DOWN THE BOM  (CTP #30)")
                print(f"  !! {len(_sf)} item(s) are REQUIRED in greater quantity than this plan MAKES,")
                print(f"  !! because a PARENT was rounded UP to its MPQ floor and nothing told the")
                print(f"  !! CHILD. TOTAL UNCOVERED: {_tot:,.0f} units.")
                print(f"  !! These lots WILL be scheduled and phase5 will NOT object — its material")
                print(f"  !! gate is DAG-precedence only and carries no BOM quantity.")
                for _t, _g in _sf.groupby("item_type")["shortfall"].agg(["count", "sum"]) \
                                  .sort_values("sum", ascending=False).head(6).iterrows():
                    print(f"  !!   {_t:<22s} {int(_g['count']):>3d} item(s)  {_g['sum']:>12,.0f} units")
                print(f"  !! Set mpq_cascade_to_children: true to make the plan EXECUTABLE.")
                print(f"  !! (The plan gets LONGER. That is CORRECT — the material is not there.)")
                print(f"  {'!' * 74}")
                _stage_csv(_sf, _suff("phase2_bom_shortfall.csv"))
                if bool(cfg.get("mpq_balance_gate", False)):
                    print("  mpq_balance_gate: refusing to ship a plan that consumes material "
                          "it never makes. Stopping.")
                    return 1
            else:
                print(f"  CTP #30 material balance: OK — every child covers its parents' MADE qty.")
                # STALE-FILE TRAP: phase2_bom_shortfall.csv is only written WHEN short. A
                # leftover from a prior (pre-cascade) run would LIE to any reader/validator
                # that the plan is still short. The balance is clean now, so clear it.
                _stale_sf = OUTPUTS / _suff("phase2_bom_shortfall.csv")
                if _stale_sf.exists():
                    try:
                        _stale_sf.unlink()
                        print(f"  (removed stale phase2_bom_shortfall.csv — balance is clean)")
                    except Exception:
                        # Fall back to overwriting with an explicit 0-row report.
                        pd.DataFrame(columns=["item_code", "item_type",
                            "required_by_parents_MADE", "made", "usable_opening_wip",
                            "shortfall"]).to_csv(_stale_sf, index=False)
        except Exception as _e:
            import traceback; traceback.print_exc()
            print(f"  !! CTP #30 MATERIAL BALANCE COULD NOT RUN ({_e!r}) — UNVERIFIED.")
    if belt_blocks.empty: belt_blocks = pd.DataFrame()
    if other_blocks.empty: other_blocks = pd.DataFrame()
    block_assignments = pd.concat([belt_blocks, other_blocks], ignore_index=True)

    # ── v54 MIXING BACKLOG MERGE (user 2026-10-09, config mixing_v47.backlog_merge) ─────────────────────────────
    # Rule B (no repeat of a compound within 15 h / 24 h on any mixer) must not make a late compound WAIT. A quick
    # capacity estimate (per mixing department: its mixers in parallel, lots in due order, routing run time, never
    # earlier than shelf life allows) gives every compound lot an estimated mixing start. Consecutive lots of the same
    # compound whose estimated starts fall within the gap (15 h; 24 h IL / 4RC) are MERGED into ONE lot = one longer
    # run that makes the backlog plus the next window at once. Limits: the merged lot stays inside shelf life at its
    # last use (max aging - margin) and its run time <= max_run_h. Butyl-friendly (exempt) compounds are not merged.
    _mv54 = cfg.get("mixing_v47") or {}
    if _MIXCW["on"] and _mv54.get("backlog_merge", False) and not lots.empty:
        try:
            _ps = pd.Timestamp(os.environ.get("CTP_PLAN_START") or pd.to_datetime(lots["first_need"]).min())
            _mg = float(_mv54.get("shelf_margin_h", 2))
            _cap_h = float(_mv54.get("max_run_h", 24))
            _cl = lots[lots["item_type"].astype(str).str.upper().isin(["MASTER COMPOUND", "FINAL COMPOUND"])].copy()
            _cl["q_fn"] = pd.to_datetime(_cl["first_need"], errors="coerce")
            _cl["q_ln"] = pd.to_datetime(_cl["last_need"], errors="coerce").fillna(_cl["q_fn"])
            _cl["q_d"] = pd.to_numeric(_cl["duration_hours"], errors="coerce").fillna(0.0)
            _cl["q_mx"] = pd.to_numeric(_cl["max_aging_h"], errors="coerce")
            _est = {}
            import heapq as _hq54
            for _dep, _g in _cl.groupby(_cl["department"].astype(str).str.upper()):
                _ms = set()
                for _x in _g["machines"].astype(str):
                    _ms |= {t.strip().strip('"') for t in _x.replace(";", ",").split(",") if t.strip()}
                _k = max(1, len(_ms))
                _free = [_ps.value] * _k
                _hq54.heapify(_free)
                for _r in _g.sort_values("q_fn").itertuples():
                    _rel = _ps.value
                    if pd.notna(_r.q_fn) and _r.q_mx == _r.q_mx and _r.q_mx > 0:
                        _rel = max(_rel, (_r.q_fn - pd.Timedelta(hours=float(_r.q_mx) - _mg)).value - int(_r.q_d * 3.6e12))
                    _f0 = _hq54.heappop(_free)
                    _s0 = max(_rel, _f0)
                    _est[_r.lot_id] = _s0
                    _hq54.heappush(_free, _s0 + int(_r.q_d * 3.6e12))
            _map, _n_m, _n_runs = {}, 0, 0
            _keep = []
            _grp_mode = str(_mv54.get("backlog_merge")).lower() == "group"   # v55: separate lots, one run
            _grp = {}
            _bdg = set()
            for _gg in ((cfg.get("butyl_day_block") or {}).get("groups") or {}).values():
                _bdg |= {str(x).strip() for x in list(_gg.get("butyl") or []) + list(_gg.get("friendly") or [])}
            for _it, _g in _cl.groupby(_cl["item_code"].astype(str).str.strip()):
                if _it in _MIXCW["exempt"] or (_grp_mode and _it in _bdg):
                    continue                           # butyl / IL compounds: daily butyl blocks handle them
                _W = (_MIXCW["w_long"] if _it in _MIXCW["long"] else _MIXCW["w"]) * 3.6e12
                _g = _g.sort_values(["q_fn", "lot_id"])
                _run = None
                for _r in _g.itertuples():
                    if _run is not None:
                        _h = _run["d"] + _r.q_d
                        _shelf_ok = not (_r.q_mx == _r.q_mx and _r.q_mx > 0) or (
                            _r.q_ln.value - (_run["s"] + int(_h * 3.6e12)) <= (float(_r.q_mx) - _mg) * 3.6e12)
                        if _est[_r.lot_id] < _run["s"] + _W and _h <= _cap_h and _shelf_ok:
                            if _grp_mode:
                                _grp[_r.lot_id] = _run["id"]
                                _grp[_run["id"]] = _run["id"]
                                _run["d"] = _h
                                _n_m += 1
                                continue
                            _map[_r.lot_id] = _run["id"]
                            _run["d"] = _h
                            _n_m += 1
                            continue
                    _run = {"id": _r.lot_id, "s": _est[_r.lot_id], "d": _r.q_d}
                    _n_runs += 1
            if _grp_mode:
                lots["mixrun_id"] = lots["lot_id"].map(lambda x: f"RUN_{_grp[x]}" if x in _grp else "")
                print(f"  [MIXING RUN GROUPS v55] {_n_m} compound lots join the run of an earlier lot of their compound "
                      f"(estimated mixing start within 15 h / 24 h): {len(set(_grp.values()))} multi-lot runs; lots stay "
                      f"SEPARATE (each usable when finished), placed back to back on one mixer, no changeover between")
            if _map:
                _li = lots.set_index("lot_id")
                _sum = ["raw_demand", "lot_qty", "surplus", "duration_min", "duration_hours", "n_blocks"]
                for _c in _sum:
                    if _c in _li.columns:
                        _li[_c] = pd.to_numeric(_li[_c], errors="coerce")
                for _src, _dst in _map.items():
                    for _c in _sum:
                        if _c in _li.columns:
                            _li.at[_dst, _c] = (_li.at[_dst, _c] or 0) + (_li.at[_src, _c] or 0)
                    _li.at[_dst, "first_need"] = min(pd.Timestamp(_li.at[_dst, "first_need"]), pd.Timestamp(_li.at[_src, "first_need"]))
                    _li.at[_dst, "last_need"] = max(pd.Timestamp(_li.at[_dst, "last_need"]), pd.Timestamp(_li.at[_src, "last_need"]))
                    _sk = set(str(_li.at[_dst, "skus_set"]).split(", ")) | set(str(_li.at[_src, "skus_set"]).split(", "))
                    _sk.discard("") ; _sk.discard("nan")
                    _li.at[_dst, "skus_set"] = ", ".join(sorted(_sk))
                    if "n_skus" in _li.columns:
                        _li.at[_dst, "n_skus"] = len(_sk)
                    if "wave_ids" in _li.columns:
                        _li.at[_dst, "wave_ids"] = f"{_li.at[_dst, 'wave_ids']}, {_li.at[_src, 'wave_ids']}"
                        _li.at[_dst, "wave_last"] = _li.at[_src, "wave_last"]
                lots = _li.drop(index=list(_map)).reset_index()
                if not block_assignments.empty:
                    block_assignments["lot_id"] = block_assignments["lot_id"].map(lambda x: _map.get(x, x))
            print(f"  [MIXING BACKLOG MERGE v54] {_n_m} compound lots merged into the run of their compound "
                  f"(estimated mixing start within 15 h / 24 h of that run's start) -> {_n_runs} runs; "
                  f"run cap {_cap_h:g} h, shelf life kept")
        except Exception as _e54:
            import traceback as _tb54
            _tb54.print_exc()
            print(f"  [MIXING BACKLOG MERGE v54] !! skipped ({type(_e54).__name__}: {_e54})")

    print(f"\n  TOTAL lots: {len(lots):,}")
    tot_h = lots["duration_hours"].sum()
    print(f"  Total work hours: {tot_h:,.0f} h")
    print(f"  Lots > 8h: {(lots['duration_min'] > 480).sum()}")

    # ---- MHE: state the MEASURED consequence, not just the prediction. (A), reporting. --
    # The warning above says the model is inert. This PROVES it on the lots we just built,
    # so the claim can never drift away from the code.
    if not lots.empty and "mhe_total" in lots.columns:
        _mhe1 = int((pd.to_numeric(lots["mhe_total"], errors="coerce").fillna(1) == 1).sum())
        _mix_active = bool(MIXING_MPQ) and bool(cfg.get("use_mixing_mpq_file", False))
        if MHE_INERT and not _mix_active:
            print(f"  MHE MODEL INERT (confirmed): mhe_total == 1 on {_mhe1:,} of "
                  f"{len(lots):,} lots. The MHE sub-lot split never fired. "
                  f"Supply a Maximum Run Qty in the MPQ master, or delete the model.")
        elif MHE_INERT:
            # 2026-07-23: the MPQ master still has no "Maximum Run Qty" (so MHE is inert for
            # NON-compound items), but the plant's 'mixing mpq' sheet now supplies a real
            # per-mixer maximum, and compound lots ARE capped by it. Saying "inert" flat out
            # would be misleading. mhe_total == 1 here is the CORRECT outcome: every sub-lot
            # already fits inside one mixer run.
            print(f"  MHE: no Maximum Run Qty in the MPQ master -> inert for NON-compound "
                  f"items. Compounds ARE capped by the per-mixer max_kg from the 'mixing mpq' "
                  f"sheet (mhe_total == 1 on {_mhe1:,} of {len(lots):,} lots = every sub-lot "
                  f"fits one mixer run).")
        else:
            print(f"  MHE: mhe_total == 1 on {_mhe1:,} of {len(lots):,} lots.")

    # ══════════════════════════════════════════════════════════════════════════════════
    # CURING REPRESENTATION LOTS (L11) -- one lot per curing SKU, tagged operation=CURING.
    # ══════════════════════════════════════════════════════════════════════════════════
    # Curing is TERMINAL: scheduled by phase6 from plan.csv, never manufactured here. But the
    # coverage check "every routed operation has lots" needs CURING to appear. Emit a lot per
    # curing SKU, sourced from the (skipped) curing demand rows. phase3 and phase5 SKIP
    # operation=CURING, so these create NO DAG edges and are NEVER placed -> ZERO schedule change.
    if bool(cfg.get("emit_curing_representation_lots", True)) and _curing_items:
        _cd = demand[demand["item_code"].astype(str).str.strip().isin(_curing_items)].copy()
        if len(_cd):
            _cd["item_code"] = _cd["item_code"].astype(str).str.strip()
            _g = _cd.groupby("item_code").agg(
                lot_qty=("demand_qty", "sum"), first_need=("need_by", "min"),
                last_need=("need_by", "max"), n_blocks=("block_id", "nunique")).reset_index()
            _rows = []
            for _, _r in _g.iterrows():
                _sku = _r["item_code"]; _m = rt_meta.get(_sku, {}) or {}
                _rows.append({
                    "lot_id": f"{_sku}_CURE_L0001", "item_code": _sku,
                    "item_type": "Cured Tyre", "operation": "CURING",
                    "department": _m.get("department", "CURING"),
                    "equipment": _m.get("equipment", "TBM PCR"),
                    "raw_demand": _r["lot_qty"], "lot_qty": _r["lot_qty"], "lot_uom": "NOS",
                    "surplus": 0.0, "duration_min": 0.0, "duration_hours": 0.0,
                    "first_need": _r["first_need"], "last_need": _r["last_need"],
                    "n_blocks": int(_r["n_blocks"]), "n_skus": 1,
                    "min_aging_h": 0.0, "max_aging_h": 0.0, "transfer_time_h": 0.0,
                    "mpq_min": 0.0, "mpq_uom": "NOS", "fraction_allowed": False,
                    "is_belt": False, "machines": ";".join(_m.get("machines", []) or []),
                    "n_machines": len(_m.get("machines", []) or []), "wire_type": "",
                    "campaign_id": "", "is_campaign_start": False, "is_campaign_end": False,
                    "wave_ids": "", "wave_first": "", "wave_last": "", "batch_id": "",
                    "mhe_index": 0, "mhe_total": 1, "skus_set": _sku})
            _cure_lots = pd.DataFrame(_rows).reindex(columns=lots.columns)
            lots = pd.concat([lots, _cure_lots], ignore_index=True)
            print(f"  CURING representation lots appended: {len(_cure_lots)} "
                  f"(operation=CURING; skipped by phase3/phase5; L11 only, schedule unchanged)")

    # ── BR-V1 MIXING IN BATCHES (2026-10-01) ─────────────────────────────────────────────
    # Mixing is planned in whole Banbury batches, not kg: every MASTER/FINAL COMPOUND lot is
    # rounded UP to a whole number of batches of the source routing's batch size. Duration is
    # unchanged -- the MIN/BATCH rate already charged ceil(kg / batch_size) batches of time.
    # The added kg is surplus. BRM-02 / BRM-13 below then add whole batches on top.
    _mixbm = None
    if str((cfg.get("business_rules") or {}).get("mixing_output_uom", "KG")).upper() == "BATCH" and len(lots):
        try:
            sys.path.insert(0, str(ROOT))
            from mixing_batches import BatchMap, MIX_ITYPES
            _mixbm = BatchMap((cfg.get("business_rules") or {}).get("mixing_batch_source"), root=str(ROOT))
            _q0 = pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)
            _q = _q0.copy()
            _ismix = lots["item_type"].astype(str).str.upper().isin(MIX_ITYPES)
            _nobs = set()
            for i in lots.index[_ismix]:
                bs = _mixbm.size(lots.at[i, "item_code"])
                if not bs:
                    _nobs.add(str(lots.at[i, "item_code"]).strip()); continue
                _q[i] = math.ceil(round(_q0[i] / bs, 6)) * bs
            ch = (_q - _q0).abs() > 1e-6
            lots.loc[ch, "lot_qty"] = _q[ch].round(2)
            if "surplus" in lots.columns:
                lots.loc[ch, "surplus"] = (pd.to_numeric(lots.loc[ch, "surplus"], errors="coerce").fillna(0)
                                           + (_q - _q0)[ch]).round(2)
            print(f"  [BR-V1] mixing in BATCHES: {int(_ismix.sum())} compound lots, {int(ch.sum())} rounded UP "
                  f"to whole batches (+{float((_q - _q0)[ch].sum()):,.0f} kg); no batch size: {sorted(_nobs)}; "
                  f"gap-filled batch sizes: {_mixbm.fallback}")
        except Exception as _e:
            _mixbm = None
            print(f"  [BR-V1] !! mixing batch rounding not applied ({type(_e).__name__}: {_e})")

    # ── BUSINESS RULES BRM-02 / BRM-13 (2026-09-27, businees_rules/*.xlsx) ─────────────────
    # BRM-02: sticky compounds (JT444, JT466) are mixed in 3-batch STACKS -> round every lot UP
    #         to a multiple of 3 x routing batch_size.
    # BRM-13: +5 extra batches per compound to cover technical hold / quality -- NOT for special
    #         compounds (rule remark: it would violate their MPQ logic). Added to the compound's
    #         LAST lot (the horizon's buffer), duration scaled with the quantity.
    _br = cfg.get("business_rules") or {}
    if _br and len(lots):
        try:
            _rt2 = pd.read_csv(INPUTS / "routing.csv", dtype=str)
            _bsz = (pd.to_numeric(_rt2["batch_size"], errors="coerce")
                    .groupby(_rt2["routed_product"].astype(str).str.strip()).max().to_dict())
            _mixi = {"MASTER COMPOUND", "FINAL COMPOUND"}
            _it = lots["item_code"].astype(str).str.strip()
            _ty = lots["item_type"].astype(str).str.upper()
            _q0 = pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)
            _q = _q0.copy()
            _sticky = {str(x).strip() for x in (_br.get("sticky_compounds") or [])}
            _stack = int(_br.get("sticky_stack_batches", 3) or 3)
            n_st = 0
            for i in lots.index[_it.isin(_sticky)]:
                bs = float(_bsz.get(_it[i]) or 0)
                if bs > 0:
                    unit = _stack * bs
                    nq = -(-_q[i] // unit) * unit
                    if nq > _q[i] + 1e-6:
                        _q[i] = nq; n_st += 1
            n_buf = 0
            _extra = int(_br.get("buffer_batches", 0) or 0)
            _special = {str(x).strip() for x in (_br.get("special_compounds") or [])} | _sticky
            if _extra > 0:
                _mixlots = lots[_ty.isin(_mixi) & ~_it.isin(_special)].copy()
                _mixlots["_fn"] = pd.to_datetime(_mixlots["first_need"], errors="coerce")
                for _item, g in _mixlots.groupby(_mixlots["item_code"].astype(str).str.strip()):
                    bs = float(_bsz.get(_item) or 0)
                    if bs > 0:
                        i = g.sort_values("_fn").index[-1]
                        _q[i] = _q[i] + _extra * bs; n_buf += 1
            ch = _q != _q0
            if ch.any():
                f = (_q / _q0.where(_q0 > 0)).fillna(1.0)
                lots.loc[ch, "lot_qty"] = _q[ch].round(2)
                for c in ("duration_min", "duration_hours"):
                    if c in lots.columns:
                        lots.loc[ch, c] = (pd.to_numeric(lots.loc[ch, c], errors="coerce") * f[ch]).round(3)
                if "surplus" in lots.columns:
                    lots.loc[ch, "surplus"] = (pd.to_numeric(lots.loc[ch, "surplus"], errors="coerce").fillna(0)
                                               + (_q - _q0)[ch]).round(2)
            print(f"  [RULES] BRM-02 sticky 3-batch stacks: {n_st} lots rounded up; "
                  f"BRM-13 +{_extra} buffer batches: {n_buf} compounds "
                  f"(+{float((_q - _q0).sum()):,.0f} kg total; special compounds excluded: {sorted(_special)})")
        except Exception as _e:
            print(f"  [RULES] !! BRM-02/13 not applied ({type(_e).__name__}: {_e})")

    if _mixbm is not None:                            # BR-V1: required batches per mixing lot
        _bsz1 = lots["item_code"].map(lambda c: _mixbm.size(c))
        _ismix1 = lots["item_type"].astype(str).str.upper().isin(MIX_ITYPES) & _bsz1.notna()
        lots["batch_size_kg"] = _bsz1.where(_ismix1)
        lots["batches"] = (pd.to_numeric(lots["lot_qty"], errors="coerce") / _bsz1).where(_ismix1).round(3)
        _frac = lots.loc[_ismix1, "batches"] % 1
        print(f"  [BR-V1] {int(_ismix1.sum())} mixing lots = {float(lots.loc[_ismix1, 'batches'].sum()):,.0f} batches; "
              f"non-whole after BRM-02/13: {int(((_frac > 1e-3) & (_frac < 1 - 1e-3)).sum())}")

    OUTPUTS.mkdir(parents=True, exist_ok=True)
    out_csv_path = OUTPUTS / _suff("phase2_lots.csv")
    _ensure_writable(out_csv_path)
    out_csv = _stage_csv(lots, "phase2_lots.csv")
    print(f"  Wrote: {out_csv}")

    if block_assignments is not None and not block_assignments.empty:
        b_csv = _stage_csv(block_assignments, "phase2_lot_blocks.csv")
        print(f"  Wrote: {b_csv}")

    if not lots.empty and "skus_set" in lots.columns:
        skus = lots[["lot_id","skus_set"]].copy()
        skus_csv = OUTPUTS / _suff("phase2_lot_skus.csv")
        skus.to_csv(skus_csv, index=False)
        print(f"  Wrote: {skus_csv}")

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
