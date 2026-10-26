#!/usr/bin/env python3
"""ctpio.py — input loaders and the shared BOM walk for the CTP e2e engine.

This is the CTP counterpart of v10's `phases/inputs_v7.py` + `phases/bom_walk.py`
+ `phases/mpq_rules.py`. The LOGIC is v10's; only the sources change.

QUANTITY SEMANTICS (same as BTP, verified on CTP)
  jkt_bom child_quantity is the PER-TYRE ABSOLUTE quantity of that child, not a
  ratio against its parent. So
      component demand = child_quantity * building_qty
  with NO chain multiplication. Where the walked region reaches the same
  component by more than one edge, the edge quantities are SUMMED — the CTP BOM
  is path-explicit (grand_parent names the branch), so one item can legitimately
  appear under several branches of the same tyre.

UNITS
  CTP states M / NOS / KG in both the BOM and the WIP dump, and the routing
  rates are in the same units. Unlike BTP there is no metre/millimetre split, so
  nothing is normalised here.
"""
from __future__ import annotations

import json
import math
import os
import difflib
import pathlib
import re
from collections import defaultdict

import pandas as pd

import blockers as BLK
import rules as R

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path(os.environ.get("CTP_DATA") or (ROOT.parent / "CTP-DATA"))
OUT_ROOT = pathlib.Path(os.environ.get("CTP_OUT") or (ROOT / "outputs"))

# THE PLAN COMES FROM THE LOT-WISE SOLVER'S OWN SEPTEMBER RUN.
#
# CTP-DATA's BuildingSchedule_{line} covers only 21-30 Sep and its
# CuringSchedule_{line} is APRIL 2026 — five months apart, so the two cannot be
# gated against each other at all. `lotwise_ctp_scheduler/optimal-sept2026`
# holds the solver's own full-month pair in the planning-engine "Shift Schedule"
# format — the same shape BTP's BTP_Schedule_BuildingPlan uses — and building
# and curing there agree to within 0.05%. That is the frozen anchor.
PE_DIR = pathlib.Path(os.environ.get("CTP_PLAN_DIR") or
                      (ROOT.parent / "lotwise_ctp_scheduler" / "optimal-sept2026"))

# WHICH ROUTING OPERATIONS ARE A BUILDING ANCHOR.
# CTP has ONE per tyre - its PCR BOM carries no Carcass item type at all, and
# TBR's PRE ASSEMBLY is ordinary upstream work on 2201. BTP is genuinely
# two-stage: a carcass is built on TBM STAGE1 and the green tyre on STAGE2, so
# a BTP run must anchor on both. Overridden by CTP_BUILD_OPS (comma-separated);
# the default keeps CTP's behaviour byte-for-byte.
BUILD_OPS = tuple(x.strip().upper() for x in
                  (os.environ.get("CTP_BUILD_OPS") or "GT BUILDING").split(",")
                  if x.strip())
CURE_OP = "CURING"
SHIFT_START_H = 7                     # plant day 07:00 -> 07:00
TRANSFER_MIN = float(R.engine("transfer_min", 10.0))

# Aging defaults for items with no master row. Ported from v10 (the alternative
# is max_aging = 0, which silently disables the whole release-window branch).
DEFAULT_MASTER_AGING_H = float(R.engine("default_master_aging_h", 360.0))
DEFAULT_FINAL_AGING_H = float(R.engine("default_final_aging_h", 120.0))


def outdir(run: str, line: str) -> pathlib.Path:
    p = OUT_ROOT / run / line
    p.mkdir(parents=True, exist_ok=True)
    return p


def _rd(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / f"{name}.csv", encoding="utf-8-sig", low_memory=False)


def _s(x) -> str:
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x).strip()


# ----------------------------------------------------------------- masters
def load_bom(line: str) -> pd.DataFrame:
    b = _rd(f"jkt_bom_{line}")
    for c in ("Super_parent", "Equipment", "grand_parent", "Parent", "child",
              "child_Unit", "child_description", "Parent_unit"):
        b[c] = b[c].map(_s)
    b["child_quantity"] = pd.to_numeric(b["child_quantity"], errors="coerce").fillna(0.0)
    b["Parent_qty"] = pd.to_numeric(b["Parent_qty"], errors="coerce")
    return b


def load_routing(line: str) -> pd.DataFrame:
    r = _rd(f"jkt_routing_{line}")
    for c in ("finished_product", "routed_product", "operation_name", "department",
              "Equipment", "machines", "proc_time_UOM", "batch_UNIT"):
        r[c] = r[c].map(_s)
    for c in ("proc_time", "batch_size", "transfer_time_min", "efficiency", "operation_seq"):
        r[c] = pd.to_numeric(r[c], errors="coerce")
    return r


def load_aging(line: str, itypes: dict | None = None) -> dict[str, tuple[float, float]]:
    """item -> (min_aging_h, max_aging_h)."""
    a = _rd(f"jkt_aging_master_{line}")

    def hrs(v, unit) -> float:
        v = pd.to_numeric(v, errors="coerce")
        if pd.isna(v):
            return 0.0
        u = str(unit or "").strip().lower()
        if u.startswith("day"):
            return float(v) * 24.0
        if u.startswith("min"):
            return float(v) / 60.0
        return float(v)                      # hours, or unstated

    out: dict[str, tuple[float, float]] = {}
    for r in a.itertuples(index=False):
        it = _s(r.ItemCode)
        if not it:
            continue
        mn = hrs(r.MinAging, getattr(r, "MinAgingUnit", ""))
        mx = hrs(r.MaxAging, getattr(r, "MaxAgingUnit", ""))
        prev = out.get(it)
        out[it] = (max(mn, prev[0]) if prev else mn, max(mx, prev[1]) if prev else mx)
    return out


def apply_aging_defaults(aging: dict, items, itypes: dict) -> tuple[dict, list]:
    """BR-4 first, then the missing-row defaults for whatever BR-4 does not cover.

    The plant's own age-limit table OVERRIDES the DB master (BR-4); a missing row
    still means NO release window, not zero aging, so the defaults stay for
    anything the table is silent on.
    """
    # v34: item-type aliases for the BR-4 lookup ONLY. CTP_Master_BOM renamed
    # PCR's cut belts and plies (BELT1/BELT2 were "Steel Belt", PLY1/PLY2 were
    # "Ply" in the 28 Sep BOM), so BR-4's by-type rest times stopped matching
    # them. `aging_spec_type_alias` restores the match without touching the
    # types anything else reads.
    # v46c: a BOM that already skips the cutters (CTP_Master_BOM_v2) never names
    # a cut code, so BR-4 cannot find the TYPE of the bypassed codes whose
    # minimum ageing is the transfer time. Their type is read from the BOM
    # named in `bypass_cut_code_types_from` (the staged pre-v2 BOM), for the
    # bypassed codes only.
    _ref = R.rule("bypass_cut_code_types_from")
    if _ref:
        _cut = {p for ps in _bypass_parents().values() for p in ps} - set(itypes)
        if _cut:
            # THIS line's reference BOM only: the bypass-parent map spans both
            # lines (shared rolls), and a PCR belt's rest must not reach TBR.
            _refs = {}
            for _ln in ("pcr", "tbr"):
                _f = resolve_ref_dir(_ref) / f"jkt_bom_{_ln}.csv"
                if _f.exists():
                    _refs[_ln] = pd.read_csv(_f, dtype=str,
                                             usecols=["child", "child_description"])
            _own = max(_refs, key=lambda k: len(set(_refs[k]["child"].map(_s)) & set(itypes)),
                       default=None)
            _rm = {}
            if _own:
                _b = _refs[_own]
                for c, d in zip(_b["child"].map(_s), _b["child_description"]):
                    if c in _cut and c not in _rm and isinstance(d, str):
                        _rm[c] = d.strip()
            itypes = dict(itypes)
            itypes.update(_rm)
            BYPASS_TYPES_FILLED.clear()
            BYPASS_TYPES_FILLED.update(_rm)
    _alias = {str(k).strip(): str(v).strip()
              for k, v in (R.rule("aging_spec_type_alias") or {}).items()}
    if _alias:
        itypes = {k: _alias.get(str(v).strip(), v) for k, v in itypes.items()}
    aging, _ = R.apply_aging_spec(aging, itypes)

    # ---- PER-ITEM OVERRIDES from the rule file --------------------------
    # BR-4 keys on item TYPE, which cannot express "this one code is wrong".
    # An override is an explicit, named ASSUMPTION about a single item and is
    # recorded as one - never silently merged into the master.
    for code, v in (R.rule("aging_overrides") or {}).items():
        mn = float((v or {}).get("min_h", aging.get(code, (0.0, 0.0))[0]) or 0.0)
        mx = float((v or {}).get("max_h", aging.get(code, (0.0, 0.0))[1]) or 0.0)
        aging[code] = (mn, mx)

    filled = []
    for it in items:
        if aging.get(it, (0.0, 0.0))[1] > 0:
            continue
        t = str(itypes.get(it, "")).strip().lower()
        if "master" in t and "compound" in t:
            aging[it] = (aging.get(it, (0.0, 0.0))[0], DEFAULT_MASTER_AGING_H)
            filled.append((it, t, DEFAULT_MASTER_AGING_H))
        elif "compound" in t:
            aging[it] = (aging.get(it, (0.0, 0.0))[0], DEFAULT_FINAL_AGING_H)
            filled.append((it, t, DEFAULT_FINAL_AGING_H))
    add_bypass_transfer(aging)
    return aging, filled


# ------------------------------------------------- v34: bypass transfer time
# When cutters / slitters are bypassed (`bypass_departments`) the material
# still needs the rest those skipped components would have taken. The plant's
# instruction: the TRANSFER TIME across a bypass is the MINIMUM AGEING of every
# component in between, up to the consumer. Every phase gaps a producer and
# its consumer by the PRODUCER's min ageing (phase 1 need_by, phase 4 CPM,
# phase 5 material-ready and kit gate, the validator's sequence check), so the
# gap is added to the producer's min ageing - one place, every phase agrees.
_BYP_PARENTS = None                  # item -> set of bypassed BOM parents


def _bypass_parents() -> dict:
    global _BYP_PARENTS
    if _BYP_PARENTS is not None:
        return _BYP_PARENTS
    _BYP_PARENTS = {}
    byp = bypass_departments()
    if not byp:
        return _BYP_PARENTS
    bypassed, par = set(), defaultdict(set)
    for line in ("pcr", "tbr"):
        try:
            rt = load_routing(line)
            b = load_bom(line)
        except Exception:
            continue
        bypassed |= {p for p, d in zip(rt["routed_product"], rt["department"])
                     if p and _s(d).upper() in byp}
        for pa, ch in zip(b["Parent"], b["child"]):
            if pa and ch and pa != ch:
                par[ch].add(pa)
        # v46c: a BOM that already skips the cutters / slitters (CTP_Master_BOM_v2)
        # never names a cut code, so "what sits between this roll and the
        # tyre" is read from the ROUTING: every bypassed cutter / slitter row
        # names the item it is made from (input_components_from_bom).
        try:
            import rules as _R
            from_rt = bool(_R.rule("bypass_transfer_parents_from_routing", False))
        except Exception:
            from_rt = False
        if from_rt and "input_components_from_bom" in rt.columns:
            for p, d, ins in zip(rt["routed_product"], rt["department"],
                                 rt["input_components_from_bom"].astype(str)):
                if not p or _s(d).upper() not in byp:
                    continue
                for x in ins.split(","):
                    x = _s(x)
                    if x and x.lower() != "nan" and x != p:
                        par[x].add(p)
    _BYP_PARENTS = {c: {p for p in ps if p in bypassed} for c, ps in par.items()}
    _BYP_PARENTS = {c: ps for c, ps in _BYP_PARENTS.items() if ps}
    return _BYP_PARENTS


BYPASS_GAP: dict = {}                # item -> hours added (last computed)
BYPASS_TYPES_FILLED: dict = {}       # v46c: cut code -> type read from the reference BOM


def resolve_ref_dir(p) -> pathlib.Path:
    """A folder named in the rules: absolute as given; otherwise looked up
    inside the staged data folder first (repo layout: <data>/reference_bom),
    then next to the engine's project root (working layout: data-ppds-v38)."""
    q = pathlib.Path(str(p))
    if q.is_absolute():
        return q
    for base in (STAGE, DATA, ROOT):
        if (base / q).exists():
            return base / q
    return ROOT / q


def add_bypass_transfer(aging: dict) -> None:
    try:
        import rules as _R
        on = bool(_R.rule("bypass_transfer_from_min_aging", False))
    except Exception:
        on = False
    if not on:
        return
    bp = _bypass_parents()
    if not bp:
        return
    memo: dict = {}

    def up(x, seen=()):
        # min ageing of bypassed x plus the longest bypassed chain above it
        if x in memo:
            return memo[x]
        if x in seen:
            return 0.0
        v = float(aging.get(x, (0.0, 0.0))[0]) + max(
            (up(p, seen + (x,)) for p in bp.get(x, ())), default=0.0)
        memo[x] = v
        return v

    bypassed = {p for ps in bp.values() for p in ps}
    BYPASS_GAP.clear()
    for c, ps in bp.items():
        if c in bypassed:
            continue                  # itself skipped - never a producer
        g = max(up(p) for p in ps)
        if g > 0:
            mn, mx = aging.get(c, (0.0, 0.0))
            aging[c] = (float(mn) + g, float(mx))
            BYPASS_GAP[c] = g


MPQ_SOURCE = {"line": "", "path": "", "rows": 0}


def load_mpq(line: str) -> pd.DataFrame:
    """The MPQ master. ONE SOURCE: JK_CTP_PPDS.CTP_Master_MPQ. No fallback.

    Staged verbatim as `CTP_Master_MPQ.csv` - the plant's own columns, the
    `Parameters` band that discriminates a 12-14 inch tread (310 M) from a
    15-20 inch one (206 M), and the `tyre_type` split.

    THE OLD FALLBACKS ARE GONE ON PURPOSE. v16 preferred a hand-placed file
    under `mpq-data/` and fell back to `jkplanning_CTP.jkt_mpq_{line}`. Either
    one could answer a question the database had not been asked, and a run could
    silently use a floor no table stated. If the staged master is absent the run
    now gets NOTHING and a NO_TABLE blocker, because a missing floor that is
    visible is safer than a floor that came from somewhere nobody can name.
    """
    cols = ["Item_Type", "Parameters", "Minimum_Run_Qty", "Maximum_Run_Qty",
            "UOM", "tyre_type"]
    try:
        d = _rd("CTP_Master_MPQ")
    except FileNotFoundError:
        BLK.add("load_mpq", "NO_TABLE", "JK_CTP_PPDS.CTP_Master_MPQ", "(whole table)",
                "staged file data-sql/CTP_Master_MPQ.csv does not exist - refresh it",
                line=line.upper(), referenced_at="ctpio.load_mpq",
                impact="NO minimum run quantity is applied anywhere in this run")
        MPQ_SOURCE.update(line=line, path="(absent)", rows=0)
        return pd.DataFrame(columns=cols)
    if "tyre_type" in d.columns:
        d = d[d["tyre_type"].map(_s).str.upper() == line.upper()]
    if not len(d):
        BLK.add("load_mpq", "MISSING_IN_DB", "JK_CTP_PPDS.CTP_Master_MPQ",
                f"tyre_type={line.upper()}", "the table carries no row for this line",
                line=line.upper(), referenced_at="ctpio.load_mpq",
                impact="NO minimum run quantity is applied on this line")
    # Maximum_Run_Qty is NULL on every row of the plant's table. Said once, here,
    # rather than discovered again by every reader of the lot sizes.
    if "Maximum_Run_Qty" in d.columns and len(d):
        n_max = int(pd.to_numeric(d["Maximum_Run_Qty"], errors="coerce").notna().sum())
        if n_max == 0:
            BLK.add("load_mpq", "MISSING_IN_DB", "JK_CTP_PPDS.CTP_Master_MPQ",
                    "Maximum_Run_Qty", "column is NULL on every row of this line",
                    line=line.upper(), referenced_at="ctpio.load_mpq",
                    impact="no upper bound on a lot; campaigns are bounded by time only")
    MPQ_SOURCE.update(line=line, path="CTP_Master_MPQ.csv (JK_CTP_PPDS)",
                      rows=int(len(d)))
    return d.reset_index(drop=True)


# ---- name canon for the buffer / MPQ joins ------------------------------
# The two plant tables and the BOM spell the same material differently. These
# three helpers pull a loose spelling onto the vocabulary of whichever MASTER
# is being joined - nothing is translated into a term no table uses.
_BUF_DIGIT = {"one": "1", "two": "2", "three": "3", "four": "4",
              "five": "5", "six": "6"}


def _btok(s) -> list:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", _s(s).lower()).split() if t]


def _bstem(t: str) -> str:
    t = _BUF_DIGIT.get(t, t)
    for suf in ("ized", "ed", "es", "s"):
        if len(t) > 4 and t.endswith(suf):
            return t[:-len(suf)]
    return t


def _bcanon(tokens, vocab: set) -> set:
    """Pull loose spellings onto the master's own vocabulary. Nothing else."""
    out = set()
    vl = sorted(vocab)
    for t in tokens:
        t = _bstem(t)
        if t in vocab:
            out.add(t)
            continue
        m = difflib.get_close_matches(t, vl, n=1, cutoff=0.8)
        if m:                                   # Appex -> apex, Calandar -> calendar
            out.add(m[0])
            continue
        for i in range(3, max(3, len(t) - 2)):  # SideWall -> side + wall
            if t[:i] in vocab and t[i:] in vocab:
                out |= {t[:i], t[i:]}
                break
        else:
            out.add(t)
    return out


def load_buffer(line: str, bom=None, rt=None) -> dict:
    """Buffer hours. ONE SOURCE: JK_CTP_PPDS.CTP_Master_Buffer. No fallback.

    `need_by = build_start - min_aging - buffer_hours(item_type)`. Every hour
    figure is a `Buffer Level (Hrs)` cell of that table, filtered to this line's
    `tyre_type`. The rule-file `buffer_aliases` and `buffer_overrides` hooks are
    GONE: an alias is a name the database does not state and an override is an
    hour figure the database does not state, and neither can be audited against
    the plant's own master.

    What remains is a NAME join, not a value decision. The master names 18 (PCR)
    / 19 (TBR) item types and the BOM names 30 / 38; only 6 and 7 match by
    spelling. Three steps, all on database evidence:

        EXACT    the BOM's name IS the master's name (case / spacing ignored)
        NAME     every token of a master term appears in the BOM name, under a
                 spelling canon built from the master's OWN vocabulary
        CONTEXT  no name link, so the item's ROUTING (department + operation,
                 from jkt_routing_{line}) is asked instead

    EVERY non-EXACT resolution is recorded as a NAME_MISMATCH blocker, because
    the fix belongs in the database: the two tables should call the same
    material the same thing. Every unreached produced type keeps 0 h and is
    recorded as MISSING_IN_DB. A tie on different hours is refused as AMBIGUOUS.
    """
    BUFFER_RESOLVED.clear()
    BUFFER_UNRESOLVED.clear()
    BUFFER_ALIASED.clear()
    BUFFER_ASSUMED.clear()
    try:
        b = _rd("CTP_Master_Buffer")
    except FileNotFoundError:
        BLK.add("load_buffer", "NO_TABLE", "JK_CTP_PPDS.CTP_Master_Buffer",
                "(whole table)",
                "staged file data-sql/CTP_Master_Buffer.csv does not exist",
                line=line.upper(), referenced_at="ctpio.load_buffer",
                impact="every need_by loses its buffer; deadlines sit at build_start")
        return {}
    if "tyre_type" in b.columns:
        b = b[b["tyre_type"].map(_s).str.upper() == line.upper()]
    col_t = next((c for c in b.columns if "item" in c.lower()), b.columns[0])
    col_h = next((c for c in b.columns if "buffer" in c.lower()), b.columns[-1])
    master: dict = {}
    for tm, h in zip(b[col_t], pd.to_numeric(b[col_h], errors="coerce")):
        tm = _s(tm).strip()
        if not tm:
            continue
        if pd.isna(h):
            # the plant states the TYPE but leaves the hours blank
            BLK.add("load_buffer", "MISSING_IN_DB", "JK_CTP_PPDS.CTP_Master_Buffer",
                    tm, "row exists but `Buffer Level (Hrs)` is NULL",
                    line=line.upper(), referenced_at="ctpio.load_buffer", uom="h",
                    impact="this item type takes 0 h of buffer")
            continue
        master[tm] = float(h)
    out = {k.lower(): v for k, v in master.items()}
    if not master:
        return out

    # the median of what the plant DOES state, for A-BUF-1
    try:
        import rules as _RB
        BUF_MEDIAN_ASSUMED = str(
            _RB.rule("assumptions.buffer_median_for_missing", False)
        ).lower() in ("1", "on", "true", "yes")
    except Exception:
        BUF_MEDIAN_ASSUMED = False
    _median_buf = (float(pd.Series(list(master.values())).median())
                   if master else None)

    vocab: set = set()
    for tm in master:
        vocab |= {_bstem(x) for x in _btok(tm)}
    mtok = {tm: _bcanon(_btok(tm), vocab) for tm in master}

    try:
        bom = load_bom(line) if bom is None else bom
        rt = load_routing(line) if rt is None else rt
    except Exception:
        return out
    itype_of = dict(zip(bom["child"], bom["child_description"]))
    ctx, produced = {}, set()
    for rp, dp, op in zip(rt.get("routed_product", []), rt.get("department", []),
                          rt.get("operation_name", [])):
        it = itype_of.get(rp)
        if not isinstance(it, str) or not it.strip():
            continue
        produced.add(it)
        ctx.setdefault(it, set()).update(_btok(dp) + _btok(op))

    for item in sorted({x for x in bom["child_description"].dropna().unique()
                        if _s(x).strip()}):
        k = _s(item).strip().lower()
        if k in out:                                   # EXACT - nothing to report
            continue
        if item not in produced:                       # purchased: never scheduled
            BUFFER_UNRESOLVED[k] = "purchased - no routing, no buffer applied"
            continue
        own = _bcanon(_btok(item), vocab)
        cand = [(len(mtok[x]), x) for x in master if mtok[x] and mtok[x] <= own]
        how = "NAME"
        if not cand:
            wide = _bcanon(_btok(item) + sorted(ctx.get(item, set())), vocab)
            cand = [(len(mtok[x] & wide), x) for x in master
                    if len(mtok[x] & wide) >= -(-len(mtok[x]) // 2)]
            how = "CONTEXT"
        if not cand:
            # ---- ASSUMPTION A-BUF-1 ---------------------------------------
            # A type the master does not name took 0 h, which asserts the plant
            # wants it ready at the very hour it is consumed. That is not what a
            # missing row means - it means "not specified yet". Assuming zero is
            # itself an assumption, and the optimistic one: it aims the item as
            # late as possible and is why several of these types head a builder
            # queue. A-BUF-1 substitutes the MEDIAN buffer this line's master
            # actually states, so an unspecified item is treated like a typical
            # one rather than like an urgent one. Reversible:
            # `assumptions.buffer_median_for_missing: false` restores 0 h.
            if BUF_MEDIAN_ASSUMED and _median_buf is not None \
                    and k not in ("green tyres",):
                out[k] = _median_buf
                BUFFER_ASSUMED[k] = _median_buf
                BLK.add("load_buffer", "MISSING_IN_DB",
                        "JK_CTP_PPDS.CTP_Master_Buffer", item,
                        "no row names this item type, and its routing reaches "
                        "none either",
                        line=line.upper(), referenced_at="ctpio.load_buffer",
                        uom="h",
                        impact=f"ASSUMPTION A-BUF-1: {_median_buf:g} h applied - the "
                               f"median of the {len(master)} buffers this line does "
                               f"state. Was 0 h. Needs the plant's real figure")
                continue
            BUFFER_UNRESOLVED[k] = "no master term reachable by name or routing"
            BLK.add("load_buffer", "MISSING_IN_DB", "JK_CTP_PPDS.CTP_Master_Buffer",
                    item, "no row names this item type, and its routing department "
                          "and operation reach none either",
                    line=line.upper(), referenced_at="ctpio.load_buffer", uom="h",
                    impact="0 h buffer - need_by sits at build_start - min_aging")
            continue
        best = max(n for n, _ in cand)
        top = sorted({x for n, x in cand if n == best})
        hrs = {master[x] for x in top}
        if len(hrs) > 1:
            BUFFER_UNRESOLVED[k] = ("ambiguous: " + " / ".join(
                f"{x} {master[x]:g} h" for x in top))
            BLK.add("load_buffer", "AMBIGUOUS", "JK_CTP_PPDS.CTP_Master_Buffer",
                    item, "matches " + " and ".join(f"'{x}' ({master[x]:g} h)"
                                                    for x in top)
                          + " - the rows disagree, so none is applied",
                    line=line.upper(), referenced_at="ctpio.load_buffer", uom="h",
                    impact="0 h buffer")
            continue
        h = hrs.pop()
        out[k] = h
        BUFFER_RESOLVED[k] = (how, " | ".join(top), h)
        BLK.add("load_buffer", "NAME_MISMATCH", "JK_CTP_PPDS.CTP_Master_Buffer",
                item, f"BOM calls it '{item}', the master calls it "
                      f"'{' | '.join(top)}' ({h:g} h) - joined by "
                      + ("token match on the name" if how == "NAME"
                         else "the item's routing department/operation"),
                line=line.upper(), referenced_at="ctpio.load_buffer", uom="h",
                impact=f"{h:g} h applied; rename in the DB to make the join exact")
    return out

# (BOM term -> the master term it inherited from, and the hours). Filled by
# load_buffer for a rule-file alias only - the automatic resolutions go to
# BUFFER_RESOLVED instead, so a manual carry is never mistaken for a DB one.
BUFFER_ALIASED: dict = {}

# BOM term -> (how, master term(s), hours) for every buffer the DATABASE
# resolver derived. Printed by phase 1: an inference nobody can audit is an
# invention with extra steps.
BUFFER_RESOLVED: dict = {}

# BOM term -> why no buffer could be taken from the master. 0 h is applied and
# the reason is stated; nothing is guessed to fill the hole.
BUFFER_UNRESOLVED: dict = {}

# BOM term -> the hours A-BUF-1 substituted where the master states none.
BUFFER_ASSUMED: dict = {}

STAGE = pathlib.Path(os.environ.get("CTP_STAGE") or (ROOT / "data"))


def load_machines() -> pd.DataFrame:
    """The work-centre register, from Master_WC_Master (+ the curing tables).

    THIS SUPERSEDES MasterMachines. Measured on the live database:

      Master_WC_Master              327 work centres  <- a strict SUPERSET
      MasterMachines                217               <- every one of them is
                                                         also in WC_Master
      in WC_Master, not MasterMachines               106

    `wcID` is the canonical key and is the same key `machine_name_provenance.csv`
    uses, so the two finally agree. `WCNAME` is the machine code the routing and
    the plans write.

    Only 51 of the 327 rows carry `Section` / `Tyre_Type`; the other 276 are
    mostly curing presses, and `Master_WC_Master_Curing_{line}` classifies 172
    of those — 92 PCR + 80 TBR presses, each with its LHS/RHS mould positions
    and an `is_active` flag. Those are folded in here so a press is not reported
    as an unclassified work centre.

    The frame keeps the MasterMachines column NAMES so every caller is unchanged.
    """
    w = pd.read_csv(STAGE / "Master_WC_Master.csv", encoding="utf-8-sig")
    w["WCNAME"] = w["WCNAME"].map(_s)
    m = pd.DataFrame({
        "machineDisplayId": w["WCNAME"],
        "machineDisplayName": w["Description"].map(_s),
        "section": w["Section"].map(_s),
        "tyreType": w["Tyre_Type"].map(_s),
        "wcID": w["wcID"],
    })
    # the curing tables classify the presses the register leaves blank
    moulds: dict[str, int] = {}
    for line in ("pcr", "tbr"):
        p = STAGE / f"Master_WC_Master_Curing_{line}.csv"
        if not p.exists():
            continue
        c = pd.read_csv(p, encoding="utf-8-sig")
        c = c[c.get("is_active", 1) == 1]
        c["mc"] = c["machineName"].map(
            lambda v: _s(int(v)) if pd.notna(v) else "")
        for mc, g in c.groupby("mc"):
            if not mc:
                continue
            moulds[mc] = int(g["machine_mould"].nunique())
            i = m.index[m["machineDisplayId"] == mc]
            if len(i):
                if not m.at[i[0], "section"]:
                    m.at[i[0], "section"] = "Curing"
                if not m.at[i[0], "tyreType"]:
                    m.at[i[0], "tyreType"] = line.upper()
    m["mould_positions"] = m["machineDisplayId"].map(moulds).fillna(0).astype(int)
    m.loc[m["machineDisplayName"] == "", "machineDisplayName"] = \
        m.loc[m["machineDisplayName"] == "", "machineDisplayId"]
    return m


def load_holidays(line: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    try:
        h = _rd(f"jkt_holiday_calendar_{line}")
    except FileNotFoundError:
        return []
    out = []
    for r in h.itertuples(index=False):
        a = pd.to_datetime(getattr(r, "start_date", None), errors="coerce")
        b = pd.to_datetime(getattr(r, "end_date", None), errors="coerce")
        if pd.notna(a) and pd.notna(b):
            out.append((a.normalize() + pd.Timedelta(hours=SHIFT_START_H),
                        b.normalize() + pd.Timedelta(hours=SHIFT_START_H + 24)))
    return out


def load_changeover() -> dict:
    """(machine_role, from_item, to_item) -> minutes, plus a per-role median."""
    c = _rd("jkt_changeover_matrix_combined")
    c["machine"] = c["machine"].map(_s)
    c["from_MaterialCode_O"] = c["from_MaterialCode_O"].map(_s)
    c["to_MaterialCode_O"] = c["to_MaterialCode_O"].map(_s)
    c["changeover_time_min"] = pd.to_numeric(c["changeover_time_min"],
                                             errors="coerce").fillna(0.0)
    pair = {}
    for r in c.itertuples(index=False):
        pair[(r.machine.lower(), r.from_MaterialCode_O, r.to_MaterialCode_O)] = \
            float(r.changeover_time_min)
    role_med = (c[c.changeover_time_min > 0].groupby(c["machine"].str.lower())
                ["changeover_time_min"].median().to_dict())
    return {"pair": pair, "role_median": role_med,
            "roles": sorted(set(c["machine"].str.lower()))}


# --------------------------------------------------- machine identity (§7)
_MODEL_MIXER = {
    "f270": "101", "270m": "101", "k270m": "101", "f270 m": "101",
    "f270f": "102", "270f": "102", "f270 f": "102",
    "f440": "103", "440m": "103", "f440m": "103",
    "k310": "104", "310": "104",
    "k430-1": "105", "430-1": "105",
    "k430-2": "106", "430-2": "106",
}


def machine_alias(machines: pd.DataFrame) -> dict[str, str]:
    """Every spelling -> canonical machineDisplayId. See CTP_IMPLEMENTATION §7."""
    alias: dict[str, str] = {}

    def put(k, v):
        k = str(k).strip().lower()
        if k:
            alias.setdefault(k, v)

    # THE AUTHORITATIVE MAP, where it exists. machine_name_provenance.csv is the
    # lot-wise solver's own reconciliation of wcID / machine_code / changeover
    # name / WC-master description, and it is the only place the TBR builders'
    # plan names (SAV-1..3, MESNAC-1..6) are tied to 3801-3809. It also records
    # that NONE of the nine is carried by Master_Building_Allowable_Machines_tbr.
    try:
        prov = pd.read_csv(PE_DIR / "machine_name_provenance.csv")
        for r in prov.itertuples(index=False):
            code = _s(getattr(r, "machine_code", ""))
            if not code:
                continue
            put(code, code)
            for col in ("changeover_master_Machine_Name", "wc_master_Description"):
                v = _s(getattr(r, col, ""))
                if v and v.lower() != "nan":
                    put(v, code)
            # `TBM 01`..`TBM 09` IS THE TBR ROUTING'S OWN SPELLING, and it
            # cannot be derived from the description any more: Master_WC_Master
            # calls 3801-3803 "UNISTAGE MACHINE 1-3", not "Tyre Building
            # Machine #1 TBR". The provenance file's wcID IS the position
            # (0-based), so TBM 01 = wcID 0 = 3801. Authoritative, and it does
            # not collide with PCR because only TBR rows are keyed this way.
            if _s(getattr(r, "line", "")).upper() == "TBR":
                try:
                    n = int(getattr(r, "wcID")) + 1
                except (TypeError, ValueError):
                    continue
                put(f"TBM {n:02d}", code)
                put(f"TBM{n:02d}", code)
                put(f"TBM-{n:02d}", code)
    except (FileNotFoundError, OSError):
        pass

    for r in machines.itertuples(index=False):
        mid = r.machineDisplayId
        put(mid, mid)
        put(r.machineDisplayName, mid)
        # "Tyre Building Machine #4 TBR" -> TBM 04 / TBMTBR4StageN
        m = re.search(r"#\s*(\d+)\s*(PCR|TBR)", str(r.machineDisplayName), re.I)
        if m:
            n, ln = int(m.group(1)), m.group(2).upper()
            # `TBM 04` IS A TBR SPELLING ONLY — and it collides.
            #
            # Both lines number their builders from 1, so generating the bare
            # `TBM NN` key for PCR machines too let PCR claim it first
            # (setdefault) and `TBM 04` in the TBR routing resolved to 3404,
            # a PCR drum. Measured: all 217 TBR GT BUILDING rows. It never
            # reached the schedule because building is the frozen anchor and
            # its machine comes from the plan, not the routing — but it is
            # wrong, and it would bite the moment routing eligibility were
            # read for anything. Only the TBR routing uses this spelling
            # (PCR: 0 rows), so only TBR machines claim the key.
            if ln == "TBR":
                put(f"TBM {n:02d}", mid)
                put(f"TBM{n:02d}", mid)
                put(f"TBM-{n:02d}", mid)
            put(f"TBM{ln}{n}Stage1", mid)
            put(f"TBM{ln}{n}Stage2", mid)
        m = re.search(r"Mixer\s*#\s*\d+\s*\(([^)]+)\)", str(r.machineDisplayName), re.I)
        if m:
            put(m.group(1).replace(" ", ""), mid)
    for k, v in _MODEL_MIXER.items():
        put(k, v)
    return alias


def resolve_machine(tok: str, alias: dict[str, str]) -> str | None:
    t = str(tok).strip().strip('"').strip()
    if not t:
        return None
    return alias.get(t.lower()) or (t if t in alias.values() else None)


# Machine tokens the routing names that are NOT in MasterMachines. They are kept
# as pseudo-machines rather than dropped -- machine 2201 runs EVERY TBR pre
# assembly, so dropping it would delete the carcass-equivalent for the whole
# line and the run would still report success. Collected here for the gap report.
UNREGISTERED: dict[str, int] = {}


# ------------------------------------------------- v15: BR-4 / BR-75 mixers
# THE ROUTING NAMES A MIXER BY ITS BASE NAME, AND THE BASE NAME IS AMBIGUOUS.
#
# `F270` appears on 3,990 PCR FINAL MIXING rows and on MASTER MIXING rows, but
# the plant has TWO different machines whose base name is F270: `F270 M`
# (wc 101, a Master Mixer) and `F270 F` (wc 102, a Final Mixer). The flat alias
# map resolves the token by spelling alone, so it answered 101 for both -- which
# put FINAL MIXING work on a MASTER mixer (6 rows failed the routing-legality
# audit in v14) and left the real second Final Mixer, F270 F, with no work at
# all (one of v14's three idle PCR machines).
#
# v15 resolves a mixer token WITH THE DEPARTMENT OF THE ROW. The map is in the
# rule file (`machines.mixer_roles`), not here, so the plant can correct it
# without a code change. Nothing else in the resolution changes.
MIXER_DEPT_HITS: dict[str, int] = {}


def _mixer_roles() -> dict:
    try:
        import rules as _R
        m = _R.rule("machines.mixer_roles") or {}
    except Exception:
        m = {}
    return {str(k).strip().upper(): {str(a).strip().lower(): str(b).strip()
                                     for a, b in (v or {}).items()}
            for k, v in m.items()}


def parse_machines(v, alias: dict | None = None,
                   department: str | None = None) -> list[str]:
    toks = [t.strip().strip('"') for t in re.split(r"[,\|]", str(v or "")) if t.strip()]
    if alias is None:
        return [t for t in toks if t]
    roles = _mixer_roles().get(str(department or "").strip().upper(), {})
    out = []
    for t in toks:
        r = roles.get(t.strip().lower())
        if r:
            MIXER_DEPT_HITS[f"{department}:{t}->{r}"] =                 MIXER_DEPT_HITS.get(f"{department}:{t}->{r}", 0) + 1
        else:
            r = resolve_machine(t, alias)
        if r is None:
            UNREGISTERED[t] = UNREGISTERED.get(t, 0) + 1
            r = t                      # keep it; flag it
        out.append(r)
    return [m for m in out if m]


# ------------------------------------------------------------- routing meta
def build_routing_meta(rt: pd.DataFrame, alias: dict) -> dict[str, dict]:
    """routed_product -> {operation, department, machines, rate, uom, batch, seq}.

    One row per item: CTP repeats the same operation for every finished_product
    that uses the item, so the first complete row wins and the machine lists are
    unioned (an item legal on 1103 for one SKU is legal on it, full stop).
    """
    meta: dict[str, dict] = {}
    for r in rt.itertuples(index=False):
        it = r.routed_product
        if not it or r.operation_name == CURE_OP:
            continue
        m = meta.get(it)
        mach = parse_machines(r.machines, alias, getattr(r, "department", None))
        if m is None:
            meta[it] = {
                "operation": r.operation_name, "department": r.department,
                "machines": list(dict.fromkeys(mach)),
                "proc_time": (float(r.proc_time) if pd.notna(r.proc_time) else None),
                "proc_time_uom": r.proc_time_UOM,
                "batch_size": (float(r.batch_size) if pd.notna(r.batch_size) else None),
                "batch_unit": _s(getattr(r, "batch_UNIT", "")),
                "transfer_min": (float(r.transfer_time_min)
                                 if pd.notna(r.transfer_time_min) else TRANSFER_MIN),
                "efficiency": (float(r.efficiency) if pd.notna(r.efficiency) else 1.0),
                "seq": (float(r.operation_seq) if pd.notna(r.operation_seq) else 999.0),
                "raw_machines": _s(r.machines),
            }
        else:
            for x in mach:
                if x not in m["machines"]:
                    m["machines"].append(x)
            if m["proc_time"] is None and pd.notna(r.proc_time):
                m["proc_time"] = float(r.proc_time)
                m["proc_time_uom"] = r.proc_time_UOM
                m["batch_size"] = (float(r.batch_size) if pd.notna(r.batch_size) else None)
                m["batch_unit"] = _s(getattr(r, "batch_UNIT", "")) or m["batch_unit"]
            # a batch size and its unit travel together: an item whose first
            # row carried neither must not end up with one and not the other
            if m.get("batch_size") is None and pd.notna(r.batch_size):
                m["batch_size"] = float(r.batch_size)
            if not m.get("batch_unit") and _s(getattr(r, "batch_UNIT", "")):
                m["batch_unit"] = _s(r.batch_UNIT)
    _tda_inline(rt, meta)
    extend_final_on_master(meta)
    return meta


TDA_INLINE: list = []                # last applied: one row per apexed-bead item


def _tda_inline(rt: pd.DataFrame, meta: dict) -> None:
    """v46b: TBR TDA WINDS AND APEXES IN LINE (rule `tda_inline`).

    The TDA (2403-2405) takes bead wire + compound and gives an apexed bead;
    winding and apexing run in parallel on the one machine, so a bead takes
    as long as the SLOWER of the two. The winding department is bypassed
    (`bypass_departments`), so no winding lot exists; here each apexed-bead
    item's rate becomes min(its own apexing rate, the routing rate of the
    winding item(s) its routing row names as input). A winding item without a
    stated rate does not lower it. PCR (separate winder 1201 + apexers 201/202)
    is untouched: only the departments named in the rule are used.
    """
    try:
        cfg = R.rule("tda_inline") or {}
    except Exception:
        cfg = {}
    if not cfg or not cfg.get("enabled"):
        return
    apex = {_s(x).upper() for x in (cfg.get("apex_departments") or [])}
    wind = {_s(x).upper() for x in (cfg.get("winding_departments") or [])}
    if not apex or not wind:
        return
    wrate = {it: (m["proc_time"], _s(m.get("proc_time_uom")).upper()) for it, m in meta.items()
             if _s(m.get("department")).upper() in wind and m.get("proc_time")}
    inputs: dict = defaultdict(set)
    for r in rt.itertuples(index=False):
        if _s(getattr(r, "department", "")).upper() not in apex or not r.routed_product:
            continue
        for x in str(getattr(r, "input_components_from_bom", "") or "").split(","):
            x = _s(x)
            if x:
                inputs[r.routed_product].add(x)
    TDA_INLINE.clear()
    for it, ins in inputs.items():
        m = meta.get(it)
        if not m or not m.get("proc_time"):
            continue
        own, uom = float(m["proc_time"]), _s(m.get("proc_time_uom")).upper()
        w = [(wrate[x][0], x) for x in sorted(ins) if x in wrate and wrate[x][1] == uom]
        slow = min([own] + [v for v, _ in w])
        TDA_INLINE.append({"item": it, "apex_rate": own, "uom": uom,
                           "winding_items": ",".join(x for _, x in w) or
                           ",".join(sorted(x for x in ins if x not in wrate and x.upper().startswith("TBWB"))),
                           "winding_rate": (min(v for v, _ in w) if w else None),
                           "rate_used": slow,
                           "slower_step": ("winding" if slow < own else "apexing")})
        if slow < own:
            m["proc_time"] = slow


def master_mixers(meta: dict) -> list:
    """Every machine the routing names for MASTER MIXING, in routing order."""
    out: list = []
    for m in meta.values():
        if _s(m.get("department")).upper() == "MASTER MIXING":
            for x in m["machines"]:
                if x not in out:
                    out.append(x)
    return out


def final_on_master_mixers() -> bool:
    try:
        import rules as _R
        return bool(_R.rule("machines.final_on_master_mixers", False))
    except Exception:
        return False


def extend_final_on_master(meta: dict) -> None:
    """v33: FINAL MIXING items may also run on the master mixers."""
    if not final_on_master_mixers():
        return
    mm = master_mixers(meta)
    for m in meta.values():
        if _s(m.get("department")).upper() == "FINAL MIXING":
            for x in mm:
                if x not in m["machines"]:
                    m["machines"].append(x)


# ------------------------------------------------------- the assumption layer
# v10's data_validation DISQUALIFIES a SKU whose components cannot be timed or
# placed. On CTP TBR that is every SKU -- five bead-wire codes carry no work
# centre at all -- so a strict run schedules nothing and says nothing useful.
#
# So the engine reports BOTH: what is schedulable on the data as it stands
# (STRICT, = v10's answer), and what is schedulable once each gap is closed by a
# NAMED, RECORDED assumption (ASSUME). Every assumption lands in
# phase0_assumptions.csv with the item, the gap, the value used and where it
# came from, so no number in the plan is unattributable.
#
# The codes match the ones the existing CTP gap register already uses.
ASSUMPTIONS: list[dict] = []


def _dept_register(department: str) -> list[str]:
    """BR-4/BR-75: the machines the PLANT REGISTER lists for a department.

    Read from the rule file (`machines.department_register`) so the mapping is
    the plant's statement, not an inference made in code. Used only where the
    routing states no machine at all -- it never widens a stated machine list.
    """
    try:
        import rules as _R
        m = _R.rule("machines.department_register") or {}
    except Exception:
        m = {}
    key = str(department or "").strip().upper()
    for k, v in m.items():
        if str(k).strip().upper() == key:
            return [str(x).strip() for x in (v or []) if str(x).strip()]
    return []


def apply_assumptions(meta: dict[str, dict]) -> dict[str, dict]:
    """Fill missing machines / rates from the department, and record each one.

    BR-1/BR-2/BR-3 are applied FIRST, so a department median used to fill a
    missing rate is a median of REAL effective rates, not of single-strand ones.
    """
    meta = R.apply_speed_rules(meta)
    by_dept_mach: dict[str, list[str]] = defaultdict(list)
    by_dept_rate: dict[str, list[tuple[str, float, float | None]]] = defaultdict(list)
    all_rate: dict[str, list[float]] = defaultdict(list)
    for it, m in meta.items():
        d = m["department"]
        for x in m["machines"]:
            if x not in by_dept_mach[d]:
                by_dept_mach[d].append(x)
        if m["proc_time"] and m["proc_time_uom"]:
            by_dept_rate[d].append((m["proc_time_uom"], m["proc_time"], m["batch_size"]))
            all_rate[m["proc_time_uom"]].append(m["proc_time"])

    def median(v):
        v = sorted(v)
        return v[len(v) // 2] if v else None

    for it, m in meta.items():
        d = m["department"] or "(unstated)"
        op = str(m["operation"]).upper()
        if not m["machines"]:
            pool = by_dept_mach.get(m["department"]) or []
            reg = _dept_register(m["department"])
            if pool:
                m["machines"] = list(pool)
                src = f"department pool {','.join(pool)}"
            elif reg:
                # v15 / BR-4 + BR-75. The routing states NO machine for this
                # item AND no other item in the department states one either.
                # v14 answered with a pseudo work centre `ASSUMED-<dept>`,
                # which is not a machine: 17 TBR bead-winding rows were
                # scheduled on it and every one failed the routing-legality
                # audit, and the real bead machines carried none of that work.
                #
                # v15 answers with the PLANT REGISTER instead - the machines
                # the plant actually lists for that department. It is still an
                # assumption (the routing does not say it), so it is recorded
                # as one, but it is an assumption made of real machines with
                # real capacity rather than an invented one with infinite
                # capacity.
                m["machines"] = list(reg)
                src = f"plant register for {d} -> {','.join(reg)} (routing states none)"
            else:
                m["machines"] = [f"ASSUMED-{d}"]
                src = "no machine anywhere in the department - single pseudo work centre"
            ASSUMPTIONS.append({"code": "A01", "item": it, "department": d,
                                "operation": m["operation"], "gap": "machines",
                                "value": ",".join(m["machines"]), "source": src})
        if (m["proc_time"] is None or not m["proc_time_uom"]) and op not in ("GT BUILDING", "CURING"):
            cand = by_dept_rate.get(m["department"]) or []
            if cand:
                uom = collections_mode([c[0] for c in cand])
                rate = median([c[1] for c in cand if c[0] == uom])
                bs = median([c[2] for c in cand if c[0] == uom and c[2]]) or None
                src = f"median of {len(cand)} rated item(s) in the same department"
            else:
                uom = "M/MIN" if all_rate.get("M/MIN") else (
                    max(all_rate, key=lambda k: len(all_rate[k])) if all_rate else "M/MIN")
                rate = median(all_rate.get(uom, [])) or 10.0
                bs = None
                src = f"no rated item in the department — line median for {uom}"
            m["proc_time"], m["proc_time_uom"] = float(rate), uom
            # fill the batch size ONLY where the routing states none - a stated
            # batch (T7110: 200 kg) must not be replaced by a department median
            if bs and uom == "MIN/BATCH" and not m.get("batch_size"):
                m["batch_size"] = bs
                m["batch_unit"] = m.get("batch_unit") or "KG"
            ASSUMPTIONS.append({"code": "A02", "item": it, "department": d,
                                "operation": m["operation"], "gap": "proc_time",
                                "value": f"{rate:g} {uom}", "source": src})
    return meta


def collections_mode(xs):
    c: dict = {}
    for x in xs:
        c[x] = c.get(x, 0) + 1
    return max(c, key=c.get) if c else None


def routed_product_index(rt: pd.DataFrame) -> dict[tuple, str]:
    """(finished_product, Equipment, operation) -> routed_product."""
    idx = {}
    for r in rt.itertuples(index=False):
        if r.operation_name in BUILD_OPS:
            idx[(r.finished_product, r.Equipment, r.operation_name)] = r.routed_product
    return idx


def bias_items(rt: pd.DataFrame) -> frozenset[str]:
    """Items the BIAS / Bios cutter makes — the BR-5 set.

    The operation names come from the rule file, because the two CTP routings
    spell the same machine (2001) differently: PCR says `BIAS CUTTER`, TBR says
    `Bios cutter`. Matching on the machine alone would be wrong — 2001 is not
    exclusively a bias cutter in every routing row.
    """
    ops = {o.strip().lower() for o in R.bias_operations()}
    if not ops:
        return frozenset()
    return frozenset(
        r.routed_product for r in rt.itertuples(index=False)
        if str(r.operation_name).strip().lower() in ops and r.routed_product)


def cure_index(rt: pd.DataFrame) -> dict[str, dict]:
    """finished_product -> {gt, machines, proc_time, uom} for the CURING row."""
    out = {}
    for r in rt.itertuples(index=False):
        if r.operation_name != CURE_OP:
            continue
        out.setdefault(r.finished_product, {
            "machines": parse_machines(r.machines),
            "proc_time": (float(r.proc_time) if pd.notna(r.proc_time) else None),
            "uom": r.proc_time_UOM,
        })
    return out


PURCHASED_DECLARED: dict = {}      # code -> the plant's own remark


def declared_purchased() -> dict:
    """The plant's STATED purchased / not-made codes, from the gaps report.

    Staged by `stage_purchased.py` out of `CTP Data Gaps Report (2).xlsx`, whose
    Remarks column is the only place in this project that says what is bought in
    rather than made ("raw material", "outside mixing"). Read once and cached.
    """
    if PURCHASED_DECLARED:
        return PURCHASED_DECLARED
    try:
        d = _rd("CTP_Purchased_Items")
    except FileNotFoundError:
        return PURCHASED_DECLARED
    for c, v, rem in zip(d["code"].map(_s), d["verdict"].map(_s),
                         d["remark"].map(_s)):
        if not c:
            continue
        # PURCHASED AND OBSOLETE ARE NOT THE SAME THING AND MUST NOT SHARE A
        # BUCKET. "Purchased" means the plant buys it, so assuming it is on hand
        # when needed is a reasonable, stated assumption. "Obsolete" or "size
        # out" means it is DISCONTINUED - assuming it is infinitely available is
        # the opposite of the truth, and would hide a plan that asks for
        # something nobody can supply. An obsolete code is therefore NOT
        # contracted out; if anything actually demands it the run says so.
        if v.upper() == "PURCHASED":
            PURCHASED_DECLARED[c] = f"{v}: {rem}" if rem else v
        elif v.upper() == "OBSOLETE":
            OBSOLETE_DECLARED[c] = rem or "obsolete"
    return PURCHASED_DECLARED


OBSOLETE_DECLARED: dict = {}       # code -> the plant's own remark


def declared_obsolete() -> dict:
    """Codes the plant calls obsolete or size-out. NOT assumed available.

    These are discontinued, so there is no honest default: they can be neither
    bought nor made. Nothing substitutes for them - a run that genuinely needs
    one reports it and the plan has to change.
    """
    declared_purchased()               # fills both registers
    return OBSOLETE_DECLARED


def schedulable_items(rt: pd.DataFrame, honour_list: bool = True) -> set[str]:
    """Items the plant MAKES. Everything else is purchased and assumed available.

    THE ROUTING IS NOT THE ONLY AUTHORITY. Until v27 this was inferred purely
    from the routing: a BOM child was purchased if and only if no routing row
    routed it. That inference is silent and it is wrong in one direction - a code
    WITH a routing row is assumed made, even where the plant says it is bought.
    All six codes its own gaps report calls raw material have a PCR routing row,
    and three (MTCM313, TBA827, TCA313) were being scheduled: the engine booked
    mixer time to produce material the plant purchases.

    So a DECLARED purchased code is removed from the made set even when the
    routing routes it. The plant's statement outranks a routing row, and every
    such case is reported rather than applied quietly.
    """
    made = {r for r in rt["routed_product"] if r}
    if honour_list:
        dec = declared_purchased()          # purchased ONLY - never obsolete
        if dec:
            made -= set(dec)
        # v33 BYPASS: items made in a `bypass_departments` department are taken
        # out of the made set, so the walk contracts them and their inputs feed
        # the consumer directly (cutters/slitters -> straight to building).
        byp = bypass_departments()
        if byp:
            for p, d in zip(rt["routed_product"], rt["department"]):
                if p and _s(d).upper() in byp:
                    BYPASSED[p] = _s(d)
            made -= set(BYPASSED)
    return made


BYPASSED: dict = {}                 # code -> the bypassed department


def bypassed_departments_raw() -> list:
    try:
        import rules as _R
        return [str(x) for x in (_R.rule("bypass_departments") or [])]
    except Exception:
        return []


def bypass_departments() -> set:
    return {_s(x).upper() for x in bypassed_departments_raw() if _s(x)}


def load_itemtypes(bom: pd.DataFrame) -> dict[str, str]:
    t: dict[str, str] = {}
    for c, d in zip(bom["child"], bom["child_description"]):
        if c and c not in t:
            t[c] = d
    return t


# ------------------------------------------------------------ duration
def op_duration_min(qty: float, meta: dict, efficiency: float = 1.0) -> float:
    """v10's op_duration_min, with CTP's UOM set (MIN/BATCH, not SEC/BATCH)."""
    if not meta:
        return 0.0
    rate, uom = meta.get("proc_time"), str(meta.get("proc_time_uom") or "").upper()
    if rate is None or rate <= 0:
        return 0.0
    eff = efficiency if efficiency and efficiency > 0 else 1.0
    q = abs(float(qty))
    if uom == "MIN/BATCH":
        bs = meta.get("batch_size") or 0.0
        n = math.ceil(q / bs - 1e-9) if bs > 0 else 1
        return max(1, n) * rate / eff
    if uom == "SEC":
        return q * rate / 60.0 / eff
    if uom == "MIN":
        return rate / eff
    if uom in ("M/MIN", "NOS/MIN", "CUTS/MIN"):
        return q / rate / eff
    return q * rate / 60.0 / eff


def batches_for(qty: float, meta: dict) -> float:
    bs = (meta or {}).get("batch_size") or 0.0
    return math.ceil(qty / bs - 1e-9) if bs > 0 else 0.0


# ------------------------------------------------------------- BOM walk
def build_child_index(bom: pd.DataFrame) -> dict[tuple, dict]:
    """(Super_parent, Equipment) -> {parent: [(child, qty, unit, desc), ...]}."""
    idx: dict[tuple, dict] = {}
    for sp, eq, par, ch, q, unit, desc in zip(
            bom["Super_parent"], bom["Equipment"], bom["Parent"], bom["child"],
            bom["child_quantity"], bom["child_Unit"], bom["child_description"]):
        if not sp or not par or not ch:
            continue
        idx.setdefault((sp, eq), {}).setdefault(par, []).append(
            (ch, float(q), unit, desc))
    return idx


class WalkCache:
    """Memoised subtree walk, keyed (sku, equipment, root, stop-set).

    Same contract as v10's bom_walk.WalkCache:
      nodes  item -> {per_tyre, uom, item_type}
      edges  (parent, child) -> per-tyre quantity of the child through that edge
      roots  item -> per-tyre quantity where the item hangs off the walk root
    """

    def __init__(self, child_idx: dict, schedulable: set[str],
                 bias: frozenset[str] = frozenset()):
        self.idx = child_idx
        self.sched = schedulable
        # BR-5: items whose CHILDREN are chain-multiplied by their own output.
        self.bias = bias
        self._c: dict = {}
        self.hit = self.miss = 0
        # v42 COLD START 6b: A ROW THAT NAMES AN ITEM AS ITS OWN CHILD IS NOT A
        # LEVEL. CTP_Master_BOM carries 233 PCR rows like CAP 66 -> CAP 66.
        # Through a CONTRACTED (unrouted) item the walk re-entered it until the
        # depth guard (24), summing the children each time: B616M came out at
        # 24x its BOM quantity (0.151 -> 3.63 kg per tyre; 1.07M kg of PCR
        # demand). With the rule on, a self-reference is skipped.
        try:
            import rules as _R
            self.skip_self = str(_R.rule("bom.skip_self_reference", False)).lower() in (
                "1", "true", "on", "yes")
        except Exception:
            self.skip_self = False
        # v46d: EACH ITEM'S RECIPE IS COUNTED ONCE. `build_child_index` keys the
        # tree on the parent alone, so tree[X] already holds X's rows from EVERY
        # path of a path-explicit BOM. Descending into X once per PARENT that
        # reaches it (the old per-edge guard) added all of them again for every
        # extra parent: B460 under a roll feeding belt 1 and belt 2 came out at
        # 2x. With the rule on, a made item is entered once (BR-5 bias items
        # keep the per-edge descent, their children scale with each path), and
        # with `all` a contracted item too.
        try:
            import rules as _R
            _wo = str(_R.rule("bom.walk_each_item_once", "") or "").lower()
        except Exception:
            _wo = ""
        self.once_made = _wo in ("made", "all", "true", "on", "yes", "1")
        self.once_contracted = _wo == "all"

    def get(self, sku: str, eq: str, root: str, stop_at: frozenset):
        key = (sku, eq, root, stop_at)   # self.bias is fixed per instance
        v = self._c.get(key)
        if v is not None:
            self.hit += 1
            return v
        self.miss += 1
        v = self._walk(sku, eq, root, stop_at)
        self._c[key] = v
        return v

    def _walk(self, sku, eq, root, stop_at):
        tree = self.idx.get((sku, eq))
        if not tree:
            return {}, {}, {}
        nodes: dict = {}
        edges: dict = defaultdict(float)
        roots: dict = defaultdict(float)
        seen_edge = set()
        seen_node = set()

        def descend(parent: str, attach_to: str | None, depth: int,
                    mult: float = 1.0):
            if depth > 24:
                return
            for ch, q, unit, desc in tree.get(parent, ()):
                if ch in stop_at or ch == root:
                    continue
                if self.skip_self and ch == parent:
                    continue                      # v42 6b: self-reference
                # BR-5 carries the bias cutter's output down ONE level, onto
                # its own children. `mult` is 1.0 everywhere else, which is
                # CTP's per-tyre-absolute rule untouched.
                qq = q * mult
                if ch not in self.sched:
                    # purchased / not made here: contract it out, keep walking.
                    # A BYPASSED bias cutter still multiplies its children by
                    # its own output (BR-5) - bypassing the machine does not
                    # change what the BOM quantities mean.
                    if self.once_contracted and ch not in self.bias:
                        if ch in seen_node:
                            continue
                        seen_node.add(ch)
                    descend(ch, attach_to, depth + 1,
                            qq if ch in self.bias else mult)
                    continue
                n = nodes.get(ch)
                if n is None:
                    nodes[ch] = {"per_tyre": qq, "uom": unit, "item_type": desc}
                else:
                    # path-explicit BOM: the same item under another branch SUMS
                    n["per_tyre"] += qq
                if attach_to is None:
                    roots[ch] += qq
                else:
                    edges[(attach_to, ch)] += qq
                if self.once_made and ch not in self.bias:
                    go = ch not in seen_node
                    seen_node.add(ch)
                else:
                    go = (parent, ch) not in seen_edge
                if go:
                    seen_edge.add((parent, ch))
                    # A BIAS / Bios CUTTER item's children are multiplied by
                    # the output the cutter actually makes (BR-5). Everything
                    # below that reverts to per-tyre absolute.
                    descend(ch, ch, depth + 1, qq if ch in self.bias else 1.0)

        descend(root, None, 0)
        return nodes, dict(edges), dict(roots)

    def stats(self) -> str:
        tot = self.hit + self.miss
        return f"{self.hit:,}/{tot:,} hits ({100*self.hit/max(tot,1):.1f}%), {len(self._c)} trees"


def topo_order(nodes: dict, edges: dict) -> list[str]:
    indeg = {n: 0 for n in nodes}
    succ = defaultdict(list)
    for (p, c) in edges:
        if p in nodes and c in nodes:
            succ[p].append(c)
            indeg[c] += 1
    q = [n for n, d in indeg.items() if d == 0]
    out = []
    while q:
        u = q.pop()
        out.append(u)
        for v in succ.get(u, ()):
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(out) < len(nodes):
        out += [n for n in nodes if n not in set(out)]
    return out


def depth_from_roots(nodes: dict, edges: dict, roots: dict) -> dict[str, int]:
    d = {n: 1 for n in nodes}
    for u in topo_order(nodes, edges):
        for (p, c) in edges:
            if p == u and c in d:
                d[c] = max(d[c], d[u] + 1)
    return d


# ------------------------------------------------------------- plan
PLAN_STAGE_RE = re.compile(r"Stage\s*\d+\s*$", re.I)


def load_building_plan(line: str, alias: dict) -> tuple[pd.DataFrame, dict]:
    """BuildingSchedule_{line} -> the canonical frozen plan frame.

    CTP writes the changeover as COLUMNS on the production row, not as separate
    CHANGEOVER rows, so v10's setup-carry collapses to reading the span.
    """
    p = _rd(f"BuildingSchedule_{line}")
    df = pd.DataFrame({
        "machine_raw": p["machineId"].map(_s),
        "sku": p["sizeCode"].map(_s),
        "qty": pd.to_numeric(p["scheduleQuantity"], errors="coerce").fillna(0.0),
        "build_start": pd.to_datetime(p["startTime"], errors="coerce"),
        "build_end": pd.to_datetime(p["endTime"], errors="coerce"),
        "shift": p["shift"].map(_s),
        "sfg_type": p["sfgType"].map(_s) if "sfgType" in p.columns else "",
        "description": p["description"].map(_s) if "description" in p.columns else "",
    })
    co_s = pd.to_datetime(p.get("changeOverStartTime"), errors="coerce")
    co_e = pd.to_datetime(p.get("changeOverEndTime"), errors="coerce")
    df["setup_min"] = ((co_e - co_s).dt.total_seconds() / 60.0).fillna(0.0).clip(lower=0.0)
    # STRIP THE StageN SUFFIX. The schedule is the only table in the CTP
    # planning set written in the stage-id namespace; everything else uses the
    # numeric display id. See CTP_IMPLEMENTATION.md §7.
    df["machine"] = [resolve_machine(m, alias) or "" for m in df["machine_raw"]]
    stats = {
        "rows_raw": len(df),
        "unresolved_machine": int((df["machine"] == "").sum()),
        "non_gt_rows": int((df["sfg_type"].str.upper() != "GT").sum()),
        "zero_qty": int((df["qty"] <= 0).sum()),
        "bad_time": int(df["build_start"].isna().sum()),
    }
    keep = (df["qty"] > 0) & df["build_start"].notna()
    return df[keep].reset_index(drop=True), stats


HEADER_SCAN_ROWS = 12


def _find_header_row(probe: pd.DataFrame, need: list[str]) -> int:
    """v10's find_header_row: the header is FOUND, not assumed.

    The pe_format workbooks carry a title row and a blank above the real header
    on the BUILDING sheet and none on the CURING sheet, and how many varies
    between exports. Guessing a fixed skiprows breaks the next file.
    """
    def norm(x):
        return re.sub(r"[^a-z0-9]", "", str(x).lower())
    want = [norm(n) for n in need]
    for i in range(min(HEADER_SCAN_ROWS, len(probe))):
        cells = {norm(v) for v in probe.iloc[i].tolist()}
        if sum(1 for w in want if w in cells) >= max(3, len(want) - 1):
            return i
    return 0


def _pe_file(line: str, kind: str) -> pathlib.Path:
    hits = sorted((PE_DIR / line / "pe_format").glob(
        f"ctp_{kind}_schedule_*_{line}.xlsx"))
    if not hits:
        raise FileNotFoundError(
            f"no ctp_{kind}_schedule_*_{line}.xlsx under {PE_DIR / line / 'pe_format'}")
    return hits[-1]                       # newest match wins, as v10 resolves inputs


def _pe_sheet(path: pathlib.Path, need: list[str]) -> pd.DataFrame:
    probe = pd.read_excel(path, sheet_name="Shift Schedule", header=None,
                          nrows=HEADER_SCAN_ROWS)
    h = _find_header_row(probe, need)
    d = pd.read_excel(path, sheet_name="Shift Schedule", header=h)
    d.columns = [str(c).strip() for c in d.columns]
    return d


def load_building_plan(line: str, alias: dict) -> tuple[pd.DataFrame, dict]:
    """The frozen building anchor, from the lot-wise solver's September run.

    Planning-engine "Shift Schedule" shape: Machine, Date, Shift, SKUCode, Qty,
    CO_Mins, StartTime, EndTime, Machine_Group, CO_Type, GT_Code, Description.

    GT_Code is carried on the row, so the built item never has to be inferred
    from the routing — which removes the resolution failure that cost BTP
    342,393 tyres when its routing's `machines` column came back empty.
    """
    path = _pe_file(line, "building")
    p = _pe_sheet(path, ["Machine", "SKUCode", "Qty", "StartTime"])
    p = p[p["Machine"].notna() & p["SKUCode"].notna()].copy()
    df = pd.DataFrame({
        "machine_raw": p["Machine"].map(_s),
        "sku": p["SKUCode"].map(_s),
        "qty": pd.to_numeric(p["Qty"], errors="coerce").fillna(0.0),
        "build_start": pd.to_datetime(p["StartTime"], errors="coerce"),
        "build_end": pd.to_datetime(p["EndTime"], errors="coerce"),
        "shift": p["Shift"].map(_s) if "Shift" in p.columns else "",
        "gt_code": p["GT_Code"].map(_s) if "GT_Code" in p.columns else "",
        "plan_mg": p["Machine_Group"].map(_s) if "Machine_Group" in p.columns else "",
        "co_type": p["CO_Type"].map(_s) if "CO_Type" in p.columns else "",
        "description": p["Description"].map(_s) if "Description" in p.columns else "",
        "setup_min": (pd.to_numeric(p["CO_Mins"], errors="coerce").fillna(0.0)
                      if "CO_Mins" in p.columns else 0.0),
    })
    df["machine"] = [resolve_machine(m, alias) or "" for m in df["machine_raw"]]
    stats = {
        "source": str(path.name), "rows_raw": int(len(df)),
        "unresolved_machine": int((df["machine"] == "").sum()),
        "zero_qty": int((df["qty"] <= 0).sum()),
        "bad_time": int(df["build_start"].isna().sum()),
        "with_gt_code": int((df["gt_code"] != "").sum()),
    }
    keep = (df["qty"] > 0) & df["build_start"].notna()
    return df[keep].reset_index(drop=True), stats


def _num_col(d: pd.DataFrame, name: str) -> pd.Series:
    """A numeric column, or a column of zeros when the file does not carry it."""
    if name in d.columns:
        return pd.to_numeric(d[name], errors="coerce").fillna(0.0)
    return pd.Series(0.0, index=d.index)


def load_curing_plan(line: str, alias: dict | None = None) -> tuple[pd.DataFrame, dict]:
    """The frozen curing plan, from the same September run as the building plan."""
    path = _pe_file(line, "curing")
    c = _pe_sheet(path, ["Machine", "SKUCode", "Qty", "StartTime"])
    c = c[c["Machine"].notna() & c["SKUCode"].notna()].copy()
    df = pd.DataFrame({
        "press_raw": c["Machine"].map(_s),
        "sku": c["SKUCode"].map(_s),
        "qty": pd.to_numeric(c["Qty"], errors="coerce").fillna(0.0),
        "start": pd.to_datetime(c["StartTime"], errors="coerce"),
        "end": pd.to_datetime(c["EndTime"], errors="coerce"),
        "shift": c["Shift"].map(_s) if "Shift" in c.columns else "",
        "gt_code": c["GT_Code"].map(_s) if "GT_Code" in c.columns else "",
        "gt_inventory": (pd.to_numeric(c["GT_Inventory"], errors="coerce").fillna(0.0)
                         if "GT_Inventory" in c.columns else 0.0),
        "cycle_min": (pd.to_numeric(c["CycleTime_min"], errors="coerce")
                      if "CycleTime_min" in c.columns else pd.NA),
        # `c.get("Mould_Clean_Mins", 0)` returns the INT 0 when the column is
        # absent, and pd.to_numeric on a scalar has no .fillna - so a curing
        # file without that column crashed the loader. Both columns are now
        # optional, which they always should have been.
        "setup_min": (_num_col(c, "CO_Mins") + _num_col(c, "Mould_Clean_Mins")),
    })
    df["press"] = [resolve_machine(m, alias) or _s(m) for m in df["press_raw"]] \
        if alias else df["press_raw"]
    keep = (df["qty"] > 0) & df["start"].notna()
    return df[keep].reset_index(drop=True), {"source": str(path.name),
                                             "rows_raw": int(len(df))}


# ------------------------------------------------------ BR-10  PM / MTC
# "Exclude PM/Cutter Shear change engineering downtime from available minutes.
#  Do not schedule inside blocked time."
#
# v14 did not read this table. DATA_GAPS.md recorded it as a known blocker -
# 76 rows, 128 press-hours unaccounted - so every v14 fulfilment figure was
# optimistic by whatever those hours would have cost. v15 reads it.
#
# WHAT THE TABLE ACTUALLY COVERS, because it matters for what this can claim:
#   section  Curing   56 blocks   896 h   (presses)
#   section  Building 20 blocks   160 h   (builders)
#   section  upstream  0 blocks     0 h   <- NOTHING. Stock-prep, mixing,
#                                            calendering and cutting carry no
#                                            PM rows at all, so this gate can
#                                            only bite curing and building.
#   plan_month 2026-09 only; the last block ends 2026-09-29 23:00.
def load_pm_downtime(line: str | None = None) -> pd.DataFrame:
    """machine -> blocked windows, from jkplanning_CTP.pm_mtc_downtime_schedule."""
    try:
        d = _rd("pm_mtc_downtime_schedule")
    except FileNotFoundError:
        return pd.DataFrame(columns=["machine", "start", "end", "reason", "section"])
    d = d.copy()
    d["machine"] = d["machine_id"].map(
        lambda x: str(int(float(x))) if pd.notna(x) else "")
    d["start"] = pd.to_datetime(d["start_time"], errors="coerce")
    d["end"] = pd.to_datetime(d["end_time"], errors="coerce")
    d["reason"] = d.get("downtime_reason", "")
    d["section"] = d.get("section", "")
    if line:
        tl = d.get("tyre_line")
        if tl is not None:
            d = d[tl.astype(str).str.strip().str.upper() == line.upper()]
    d = d[d["start"].notna() & d["end"].notna() & (d["machine"] != "")]
    return d[["machine", "start", "end", "reason", "section"]].reset_index(drop=True)


def pm_blocks(line: str | None = None) -> dict[str, list[tuple]]:
    """{machine: [(start, end), ...]} sorted, ready for a forward scan."""
    d = load_pm_downtime(line)
    out: dict[str, list[tuple]] = {}
    for r in d.itertuples(index=False):
        out.setdefault(str(r.machine), []).append((r.start, r.end))
    for k in out:
        out[k].sort()
    return out


def pm_push(blocks: dict, machine: str, s, dur) -> tuple:
    """BR-10: slide a lot forward until it clears every PM block on its machine.

    Returns (start, pushed_hours). A lot is never SPLIT across a PM window -
    the plant does not resume a half-finished run after a 16-hour shutdown -
    so it is moved whole to the far side.
    """
    b = blocks.get(str(machine))
    if not b:
        return s, 0.0
    t0 = s
    for _ in range(len(b) + 1):
        moved = False
        for a, z in b:
            if s < z and (s + dur) > a:        # overlaps the block
                s = z
                moved = True
        if not moved:
            break
    return s, (s - t0).total_seconds() / 3600.0


def load_wip(line: str) -> pd.DataFrame | None:
    """Opening stock. ONE SOURCE: JK_CTP_PPDS.CTP_Opening_WIP, split per line.

    TWO THINGS THE RAW TABLE DOES THAT A NETTING PASS MUST NOT INHERIT.

    1. IT REFRESHES THREE TIMES A DAY AND KEEPS EVERY REFRESH. The same physical
       trolley appears once per sync under one `production_id`, identical in
       every column but `updatedAt`. Measured on the 29-Sep tread rows: 412 rows
       for 225 trolleys (PCR) and 166 for 100 (TBR) - summing the rows inflates
       stock by about 80%. Only the LATEST row per production_id is real.

    2. IT IS A DATED SNAPSHOT. If the staged snapshot is older than the plan it
       is being netted against, the stock is understated by everything the plant
       has made since, and the run will build material it already has. That is
       recorded as STALE rather than silently accepted.
    """
    try:
        w = _rd(f"opening_wip_{line}")
    except FileNotFoundError:
        BLK.add("load_wip", "NO_TABLE", wip_table(),
                f"opening_wip_{line}", "staged per-line file does not exist",
                line=line.upper(), referenced_at="ctpio.load_wip",
                impact="every component is made from scratch; fulfilment is a floor")
        return None
    w["itemcode"] = w["itemcode"].map(_s)
    # `format="mixed"` parses each row on its own terms. Without it a column
    # holding two timestamp shapes silently becomes NaT for the minority shape,
    # and every one of those batches is dropped from the pool unnoticed.
    w["produced_time"] = pd.to_datetime(w["produced_time"], errors="coerce",
                                        format="mixed")
    _nat = int(w["produced_time"].isna().sum())
    if _nat:
        BLK.add("load_wip", "DROPPED", wip_table(), "(unparseable produced_time)",
                f"{_nat:,} row(s) carry a produced_time that cannot be read, so "
                f"they can be neither matured nor expired",
                line=line.upper(), referenced_at="ctpio.load_wip",
                qty=float(w.loc[w["produced_time"].isna(), "inventory"].sum()),
                impact="those batches are invisible to the netting pass")
    w["inventory"] = pd.to_numeric(w["inventory"], errors="coerce").fillna(0.0)

    # ---- 1. one row per physical trolley -----------------------------------
    upd = next((c for c in w.columns if c.lower() in ("updatedat", "updated_at")), None)
    if "production_id" in w.columns and upd:
        n0, q0 = len(w), float(w["inventory"].sum())
        w = (w.sort_values(upd, kind="stable")
               .groupby(["production_id", "itemcode"], as_index=False, sort=False)
               .tail(1).reset_index(drop=True))
        n1, q1 = len(w), float(w["inventory"].sum())
        if n0 != n1:
            BLK.add("load_wip", "DROPPED", wip_table(),
                    "(duplicate production_id)",
                    f"{n0 - n1:,} of {n0:,} row(s) were earlier refreshes of a "
                    f"trolley already counted - only the latest `{upd}` per "
                    f"production_id is kept",
                    line=line.upper(), referenced_at="ctpio.load_wip",
                    qty=q0 - q1,
                    impact=f"stock read as {q1:,.0f} not {q0:,.0f} - summing the raw "
                           f"rows would overstate it by {100 * (q0 - q1) / max(q1, 1):.0f}%")

    # ---- 2. is the snapshot current? ---------------------------------------
    if "inventory_date" in w.columns:
        snap = pd.to_datetime(w["inventory_date"], errors="coerce").max()
        newest = w["produced_time"].max()
        if pd.notna(snap):
            WIP_SNAPSHOT[line] = {"inventory_date": snap, "newest_produced": newest,
                                  "rows": int(len(w)),
                                  "qty": float(w["inventory"].sum()),
                                  "last_sync": (pd.to_datetime(w[upd],
                                                               errors="coerce").max()
                                                if upd else None)}
    return w


def wip_table() -> str:
    """The database table the staged opening inventory actually came from."""
    try:
        import json as _j
        return _j.loads((DATA / "_wip_source.json").read_text(encoding="utf-8"))["table"]
    except Exception:
        return "JK_CTP_PPDS.CTP_Opening_WIP"


# line -> the snapshot the staged WIP actually carries. Phase 2 compares it with
# the plan it is netting against and raises STALE if the plan has moved on.
WIP_SNAPSHOT: dict = {}


_MNAMES: dict | None = None


def machine_names() -> dict:
    """machine id -> the plant's own name, from Master_WC_Master (CTP_Machine_
    Master in a PPDS run): '101' -> 'Mixer #1 (F270 M)', '4601' -> '46" LTM
    CURING PRESS NO 1'. A machine the register does not name maps to ''."""
    global _MNAMES
    if _MNAMES is None:
        try:
            m = load_machines()
            _MNAMES = {_s(a): _s(b) for a, b in zip(m["machineDisplayId"],
                                                     m["machineDisplayName"])
                       if _s(a)}
            _MNAMES = {k: ("" if v == k else v) for k, v in _MNAMES.items()}
        except Exception:
            _MNAMES = {}
    return _MNAMES


def with_machine_name(df, col: str, name_col: str | None = None):
    """Insert `<col>_name` (or name_col) right after `col`."""
    if col not in df.columns:
        return df
    nc = name_col or f"{col}_name"
    if nc in df.columns:
        df = df.drop(columns=[nc])
    df.insert(df.columns.get_loc(col) + 1, nc,
              df[col].map(lambda v: machine_names().get(_s(v).split(".")[0], ""))
              )
    return df


def plant_day(ts):
    return (pd.to_datetime(ts) - pd.Timedelta(hours=SHIFT_START_H)).normalize()


def shift_of(ts) -> str:
    h = pd.to_datetime(ts).hour
    return "A" if 7 <= h < 15 else ("B" if 15 <= h < 23 else "C")


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
