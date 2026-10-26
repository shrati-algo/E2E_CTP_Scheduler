#!/usr/bin/env python3
"""upstream_rules.py — the plant's `Upstream Business Rules.xlsx`, in code.

Every function here answers ONE rule and names the Rule ID it came from. Each
call that actually changes something is recorded in APPLIED, so the run can
print exactly which rule touched which item and by how much — a rule that
fires silently is a rule nobody can audit.

Rules the workbook marks "Need confirmation if applicable for CTP" are read
from the config as disabled and return a no-op. They are NOT approximated.
"""
from __future__ import annotations

import math
from collections import defaultdict

import rules as R

APPLIED: dict[str, list] = defaultdict(list)


def _norm(x) -> str:
    return str(x or "").strip().lower()


def _on(path: str, default=True) -> bool:
    v = R.rule(path, default)
    return bool(v)


# ----------------------------------------------------------------- BRM-01
def mpq_batches() -> int:
    """BRM-01 — the scheduled quantity follows MPQ = 5 batches.

    OVERRIDES CTP's own jkt_mpq (which states 3). The workbook is the plant's
    instruction and the DB is the plant's record; where they disagree on a
    *rule*, the instruction wins.
    """
    return int(R.rule("mixing.mpq_batches", 5))


# ----------------------------------------------------------------- BRM-13
def buffer_batches(item: str, item_type: str, is_special: bool = False) -> int:
    """BRM-13 — +5 extra batches for technical hold and quality, EXCEPT for
    special compounds, where the extra would violate the MPQ."""
    n = int(R.rule("mixing.buffer_batches", 0) or 0)
    if n <= 0 or "compound" not in _norm(item_type):
        return 0
    if is_special and _on("mixing.buffer_excludes_special", True):
        APPLIED["BRM-13-skipped"].append((item, "special compound"))
        return 0
    APPLIED["BRM-13"].append((item, item_type, n))
    return n


# ----------------------------------------------------------------- BRM-02
def sticky_stack(item: str) -> int | None:
    """BRM-02 — sticky compounds (JT444, JT466) stack only 3 batches high, so
    a lot may not exceed 3 batches. Used in PCR sidewall."""
    sticky = {_norm(x) for x in (R.rule("mixing.sticky_compounds") or [])}
    if _norm(item) in sticky:
        n = int(R.rule("mixing.sticky_stack_batches", 3))
        APPLIED["BRM-02"].append((item, n))
        return n
    return None


# ----------------------------------------------------------------- BRM-03
def butyl_class(item: str) -> str | None:
    """BRM-03 — butyl compounds run continuously; only butyl-friendly
    compounds may follow them. Returns 'butyl', 'friendly', or None."""
    if not _on("mixing.butyl.enabled", True):
        return None
    b = {_norm(x) for x in (R.rule("mixing.butyl.compounds") or [])}
    f = {_norm(x) for x in (R.rule("mixing.butyl.friendly_after") or [])}
    n = _norm(item)
    return "butyl" if n in b else ("friendly" if n in f else None)


# --------------------------------------------------------------- BR4RC-09
def min_roll_m(item: str) -> float | None:
    """BR4RC-09 — Nychaffer, TLChaffer, Nylfiller, WPJ011: minimum 200 m roll."""
    names = {_norm(x) for x in (R.rule("frc.min_roll_items") or [])}
    n = _norm(item)
    if any(k in n or n in k for k in names):
        m = float(R.rule("frc.min_roll_m", 0) or 0)
        if m > 0:
            APPLIED["BR4RC-09"].append((item, m))
            return m
    return None


# --------------------------------------------------------------- BR4RC-13
def ramp_min(department: str, operation: str) -> tuple[float, float]:
    """BR4RC-13 — FRC ramps up 2 min and down 3 min on every run."""
    d, o = _norm(department), _norm(operation)
    if "calend" in d or "calend" in o or "frc" in d or "four roll" in o:
        up = float(R.rule("frc.ramp_up_min", 0) or 0)
        dn = float(R.rule("frc.ramp_down_min", 0) or 0)
        if up or dn:
            APPLIED["BR4RC-13"].append((operation, up, dn))
            return up, dn
    return 0.0, 0.0


# --------------------------------------------------------------- BR4RC-05
def cap66_cleanout_min(from_item: str, to_item: str) -> float:
    """BR4RC-05 — 15 minutes of cleanout BEFORE and AFTER CAP 66.

    Charged on the transition INTO cap66 and on the transition OUT of it, so a
    cap66 campaign costs the cleanout once at each end, not once per lot.
    """
    names = [_norm(x) for x in (R.rule("frc.cap66_items") or [])]
    if not names:
        return 0.0

    def is66(x):
        n = _norm(x)
        return any(k and (k in n or n.startswith("cap 66") or n.startswith("cap66"))
                   for k in names)

    a, b = is66(from_item), is66(to_item)
    if a != b:                                  # entering or leaving cap66
        m = float(R.rule("frc.cap66_cleanout_min", 0) or 0)
        if m > 0:
            APPLIED["BR4RC-05"].append((from_item, to_item, m))
            return m
    return 0.0


# --------------------------------------------------------------- BR4RC-14
def wire_gap_h() -> float:
    """BR4RC-14 — wire-to-wire transitions keep a 6-hour separation."""
    return float(R.rule("frc.wire_to_wire_gap_h", 0) or 0)


def is_wire(item: str, item_type: str) -> bool:
    t = _norm(item_type)
    return "wire" in t or "steel cord" in t or "steel tyre cord" in t


# ----------------------------------------------------------- BRC-01/BRC-02
def belts_together() -> bool:
    """BRC-01 — belt 1 and belt 2 of the same SKU are scheduled together."""
    return _on("cutters.belts_together", False)


def belt_pair_key(item: str) -> str | None:
    """`B1-PBLT009` and `B2-PBLT009` are the two belts of one SKU — the pair
    key is the code with the B1/B2 prefix stripped."""
    n = str(item or "").strip()
    up = n.upper()
    for p in ("B1-", "B2-", "B1_", "B2_"):
        if up.startswith(p):
            return n[len(p):]
    return None


def once_per_shift() -> bool:
    """BRC-02 — the same item code is scheduled once in a shift."""
    return _on("cutters.once_per_shift", False)


# ------------------------------------------------------- BRE-01 / BRIL-01
def rolling_horizon_h(item_type: str) -> float:
    """BRE-01 / BRIL-01 — SideWall, Tread and Inner Liner are planned against
    a rolling 24-hour building-demand horizon rather than the whole month."""
    t = _norm(item_type)
    for sec in ("extrusion", "il_calendering"):
        names = [_norm(x) for x in (R.rule(f"{sec}.rolling_applies_to") or [])]
        if any(k and k in t for k in names):
            h = float(R.rule(f"{sec}.rolling_horizon_h", 0) or 0)
            if h > 0:
                APPLIED["BRE-01/BRIL-01"].append((item_type, h))
                return h
    return 0.0


# ------------------------------------------------------------------ report
def unimplementable() -> dict:
    return R.rule("unimplementable") or {}


def pending() -> dict:
    return R.rule("mixing.pending_confirmation") or {}


def report(prefix: str = "  ") -> None:
    print(f"{prefix}UPSTREAM BUSINESS RULES — {R.rule('meta.source', '(no source)')}")
    if APPLIED:
        print(f"{prefix}  applied:")
        for rid, hits in sorted(APPLIED.items()):
            items = {h[0] for h in hits}
            print(f"{prefix}    {rid:16s} {len(hits):>6,} time(s) on {len(items):>4} item(s)")
    else:
        print(f"{prefix}  applied: (none fired)")
    p = pending()
    if p:
        print(f"{prefix}  NOT APPLIED — workbook says confirm with the plant ({len(p)}):")
        for rid, v in p.items():
            print(f"{prefix}    {rid:10s} {str(v.get('rule'))[:70]}")
    u = unimplementable()
    if u:
        print(f"{prefix}  BLOCKED — CTP has no data for these ({len(u)}):")
        for rid, v in u.items():
            print(f"{prefix}    {rid:10s} missing: {str(v.get('missing'))[:62]}")

# =====================================================================
#  v15 - THE MASTER RULE BOOK (BR-1 .. BR-77)
#
#  master-business-rules/Upstrean Business rules.xlsx re-issues seven rules
#  the old book marked "Need confirmation if applicable for CTP" (BRM-04..10)
#  as FIRM instructions, and adds new ones. Everything below answers a rule of
#  that book and records every firing in APPLIED, so "applied" means it changed
#  a pick - not that a flag was set.
# =====================================================================


def _norm_code(x) -> str:
    return str(x or "").strip().upper()


# ----------------------------------------------------------------- BR-73
def mixing_once_per_shift() -> bool:
    """BR-73 - the same compound is not scheduled twice in one shift."""
    return _on("mixing.once_per_shift", False)


# ----------------------------------------------------------------- BR-71
_REL_CACHE: dict | None = None


def related_group(item: str) -> str | None:
    """BR-71 - related compounds run continuously (e.g. IL3 with IL5).

    Returns the group key an item belongs to, so the dispatcher can prefer a
    partner of the compound already mounted over an unrelated one.
    """
    global _REL_CACHE
    if not _on("mixing.related_together", False):
        return None
    if _REL_CACHE is None:
        _REL_CACHE = {}
        for i, grp in enumerate(R.rule("mixing.related_groups") or []):
            for c in (grp or []):
                _REL_CACHE[_norm_code(c)] = f"REL{i}"
    g = _REL_CACHE.get(_norm_code(item))
    if g:
        APPLIED["BR-71"].append((item, g))
    return g


# ----------------------------------------------------------------- BR-72
def finals_consecutive() -> bool:
    """BR-72 - a master with two finals has both finals scheduled together."""
    return _on("mixing.finals_consecutive", False)


def finals_of_master(bom) -> dict[str, list[str]]:
    """master compound -> the final compounds made from it, READ FROM THE BOM.

    BR-72 needs the master->final relation and CTP states it nowhere as a
    master; it is only implicit in the BOM, where a FINAL COMPOUND has a
    MASTER COMPOUND as a child. So it is derived, not assumed.
    """
    out: dict[str, list[str]] = defaultdict(list)
    try:
        for r in bom.itertuples(index=False):
            par, ch = _norm_code(getattr(r, "Parent", "")), _norm_code(getattr(r, "child", ""))
            if par and ch and ch.startswith("M") and not par.startswith("M"):
                if par not in out[ch]:
                    out[ch].append(par)
    except Exception:
        return {}
    return {k: v for k, v in out.items() if len(v) >= 2}


# ----------------------------------------------------------------- BR-69
def must_follow(item: str) -> str | None:
    """BR-69 - after ML405 / ML406, MK3 must be produced."""
    m = {_norm_code(k): _norm_code(v)
         for k, v in (R.rule("mixing.follow_with") or {}).items()}
    return m.get(_norm_code(item))


# ----------------------------------------------------------------- BR-70
def cleanout_compound() -> str:
    """BR-70 - TR100 between a master and a final on the SAME machine."""
    c = R.rule("mixing.cleanout.compound") or ""
    return _norm_code(c) if _on("mixing.cleanout.enabled", False) else ""


def cleanout_needed(prev_type: str, next_type: str) -> bool:
    """True when the machine is crossing master -> final (or back)."""
    a, b = _norm(prev_type), _norm(next_type)
    if not a or not b:
        return False
    am, bm = "master" in a, "master" in b
    af, bf = "final" in a, "final" in b
    return (am and bf) or (af and bm)


# ----------------------------------------------------------------- BR-68
def k4_machine() -> str:
    """BR-68 - Banbury K4 sequencing around IL compound.

    CTP has no mixer called K4. Until the plant names it, this returns "" and
    the rule cannot fire; the compliance report says so rather than guessing
    which of the five mixers is meant.
    """
    return str(R.rule("mixing.k4.machine") or "").strip()


# ----------------------------------------------------------------- BR-63/64
def tbr_bead_inch(description: str) -> str | None:
    """BR-63 / BR-64 - the bead inch family, read off the item description.

    TBR bead items carry their rim size in the description (10.00R20 -> 20in,
    295/80R22.5 -> 22.5in). No bead master exists, but the inch IS recoverable,
    so the inch family can be formed and the 20 <-> 22.5 changeover charged.
    """
    import re as _re
    if not _on("bead.tbr_inch_from_description", False):
        return None
    m = _re.search(r"R\s*(\d{2}(?:\.\d)?)", str(description or "").upper())
    return m.group(1) if m else None


def tbr_inch_changeover_min(a_desc: str, b_desc: str) -> float:
    """BR-64 - 20in <-> 22.5in bead winding changeover is about 20 minutes."""
    a, b = tbr_bead_inch(a_desc), tbr_bead_inch(b_desc)
    if a and b and a != b:
        m = float(R.rule("bead.tbr_inch_changeover_min", 0) or 0)
        if m > 0:
            APPLIED["BR-64"].append((a, b, m))
            return m
    return 0.0

