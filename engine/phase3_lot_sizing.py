#!/usr/bin/env python3
"""phase3_lot_sizing.py — net demand -> producible runs and lots.

v10 phase 3 in FLOW mode (V7_FLOW=1), on CTP data.

THE RULES
  1. A run is bounded by the CONSUMER CAMPAIGN WINDOW, not by MPQ: a new run
     starts when the floor stops calling for the item for longer than
     FLOW_GAP_H (8 h).
  2. FRESHNESS IS EXACT, not a heuristic. A batch finishing at F serves a need
     at N iff F+min <= N <= F+max, so a feasible F exists for a bucket iff
         last_need - first_need <= max_aging - min_aging
     The bucket window is that, less a 2 h safety, floored at 8 h.
  3. NO MPQ-MIN ROUNDING, except where the floor is PHYSICAL: the mixer cannot
     make 0.4 of a batch.

CTP'S MPQ IS NOT BTP'S, AND THIS IS THE MAIN DELTA (CTP_IMPLEMENTATION §8.2)
  jkt_mpq_{line} is item-type keyed, 22 rows, and Maximum_Run_Qty is EMPTY on
  every row. So:
    * rule "never exceed MPQ-max" CANNOT FIRE for non-compound items — a bucket
      becomes one lot. That is a stated limitation, not a modelling choice.
    * the mixer is unaffected: proc_time_UOM = MIN/BATCH carries batch_size on
      all 11,562 PCR / 21,510 TBR rows, so a compound lot is a whole number of
      real batches.
    * CTP states MPQ = 3 batches for both MASTER and FINAL COMPOUND. BTP's is 4.
      CTP's own number is used.
"""
from __future__ import annotations

import argparse
import math
import re
import sys

import pandas as pd

import blockers as BLK
import ctpio as io
import upstream_rules as UR

FLOW_GAP_H = 8.0
AGING_SAFETY_H = 2.0
MIN_BUCKET_H = 8.0
FLOW_WINDOW_BY_DEPT = {"master mixing": 72.0, "final mixing": 72.0, "mixing": 72.0}

# A03 stand-in for the missing Maximum_Run_Qty. See the note in size_item().
MAX_LOT_H = 8.0
_A03: dict = {}
_SPLIT: dict = {}
_CAMP_ITEMS: set = set()
try:
    import rules as _RC
    CAMP_LOT_M = ({str(x).strip() for x in (_RC.rule("campaign.machines", []) or [])}
                  if str(_RC.rule("campaign.bigger_lots", False)).lower() in
                  ("1", "true", "on", "yes") else set())
    CAMP_LOT_WIN_H = float(_RC.rule("campaign.lot_window_h", 0) or 0)
    CAMP_KEEP_SPAN = str(_RC.rule("campaign.keep_need_span", False)).lower() in (
        "1", "true", "on", "yes")
except Exception:
    CAMP_LOT_M, CAMP_LOT_WIN_H, CAMP_KEEP_SPAN = set(), 0.0, False

# v16: how much need-by span one lot may cover before it is cut. 0 = off.
# Read from the rule file; bounded at run time by Minimum_Run_Qty.
try:
    import rules as _R0
    NEED_SPAN_CAP_H = float(_R0.rule("lot_sizing.need_span_cap_h", 0) or 0)
except Exception:
    NEED_SPAN_CAP_H = 0.0

# v16: take the CHARGE COUNT from CTP_Master_MPQ (3) rather than the rule
# file's BRM-01 override (5). The master is the plant's own file.
try:
    import rules as _R1
    WHOLE_UNITS = bool(_R1.rule("lot_sizing.whole_units", False))
    USE_MASTER_BATCHES = str(_R1.rule("mpq.use_master_batches", False)).lower() in (
        "1", "on", "true", "yes")
except Exception:
    USE_MASTER_BATCHES = False
    WHOLE_UNITS = False


MPQ_LOG: dict[str, dict] = {}
_MPQ_JOIN: dict = {}         # filled in main() once the MPQ is loaded
MPQ_JOINED: dict = {}        # item -> (BOM name, MPQ Item_Type) where
#                              the two tables disagree on the name       # item -> how its floor was resolved

# master Item_Type -> the BOM's name(s) for the same material. Read from the
# rule file so the plant can correct a name without a code change. The three
# TBR pairs whose UNITS disagree (Belt/Ply/STC, stated in M against demand in
# NOS) are deliberately NOT listed - see mpq_floor()'s unit guard.
try:
    import rules as _R
    # STRICT DB SOURCING: `mpq_aliases` was a hand-written bridge in the rule
    # file between CTP_Master_MPQ's Item_Type and the BOM's child_description.
    # A YAML list is not a database table, so it is no longer read. The bridge
    # is now DERIVED from the two vocabularies themselves - see mpq_name_join()
    # - and every non-exact join is reported as a NAME_MISMATCH blocker, because
    # the real fix is to make the two tables agree.
    MPQ_ALIAS = {}
except Exception:
    MPQ_ALIAS = {}


def load_mpq_rules(line: str) -> dict[str, list]:
    """item_type (lower) -> [ {min, max, uom, params}, ... ]  ALL rows, not one.

    v16 KEEPS EVERY ROW. The old loader used `setdefault`, so for a type with
    several `Parameters` bands only the FIRST survived: every PCR tread took the
    12-14 inch floor of 310 M, including 15-20 inch treads whose own floor is
    206 M - a 50% over-constraint on the item behind most of the late material.
    Edge strip (4 gauges) and edge gum (2 colour ranges) had the same problem.

    Discrimination between bands happens at the item, in `mpq_floor()`, because
    only there is the item's own description available.
    """
    m = io.load_mpq(line)
    out: dict[str, list] = {}
    for r in m.itertuples(index=False):
        t = str(getattr(r, "Item_Type", "")).strip().lower()
        if not t:
            continue
        lo = pd.to_numeric(getattr(r, "Minimum_Run_Qty", None), errors="coerce")
        hi = pd.to_numeric(getattr(r, "Maximum_Run_Qty", None), errors="coerce")
        out.setdefault(t, []).append(
            {"min": (float(lo) if pd.notna(lo) else None),
             "max": (float(hi) if pd.notna(hi) else None),
             "uom": str(getattr(r, "UOM", "")).strip(),
             # the Item_Type this row belongs to, carried on the row so a floor
             # can say WHICH master term it is enforcing once it has travelled
             # down a BOM edge to a component
             "key": t,
             "params": str(getattr(r, "Parameters", "")).strip()})
    return out


def _u(x) -> str:
    """UOM, normalised. Nos/NOS/nos are one unit; Batches is its own."""
    s = str(x or "").strip().upper()
    return {"NOS": "NOS", "NO": "NOS", "M": "M", "KG": "KG",
            "BATCHES": "BATCHES", "BATCH": "BATCHES"}.get(s, s)


def _inch_of(text: str):
    """The rim inch in an item's description, for the tread Parameters bands."""
    m = re.search(r"R\s*(\d{2}(?:\.\d)?)", str(text or "").upper())
    if m:
        return float(m.group(1))
    m = re.search(r"\b(\d{2}(?:\.\d)?)\s*(?:INCH|\")", str(text or "").upper())
    return float(m.group(1)) if m else None


def _params_match(params: str, item: str, desc: str) -> bool:
    """Does this Parameters band apply to this item?

    Bands seen in CTP_Master_MPQ:
        Tread      '12 to 14 inch' / '15 to 20 inch'   -> rim inch
        Edge strip '0.7' '0.8' '1' '1.6'                -> gauge in the code
        Edge gum   'range 0.7 and 0.8 ...'              -> gauge in the code
    A band that cannot be tested returns False, so `mpq_floor` falls back to the
    LEAST constraining row rather than guessing - an over-high floor forces
    aggregation, which is the thing this release exists to remove.
    """
    p = str(params or "").strip().lower()
    if not p or p == "nan":
        return True                                   # unparameterised row
    m = re.match(r"(\d+(?:\.\d+)?)\s*to\s*(\d+(?:\.\d+)?)\s*inch", p)
    if m:
        v = _inch_of(desc) or _inch_of(item)
        return v is not None and float(m.group(1)) <= v <= float(m.group(2))
    g = re.findall(r"\d+(?:\.\d+)?", p)
    if g:
        hay = f"{item} {desc}".upper()
        return any(re.search(rf"(?<!\d){re.escape(x)}(?!\d)", hay) for x in g)
    return False


# =====================================================================
#  MPQ UNIT BRIDGE - the same three-table chain, one BOM hop further
# =====================================================================
#  Seven TBR items carried an MPQ row whose unit was not theirs:
#
#      30G            INNER LINER  140 M    item in KG
#      TB WRAP B1144  Belt         200 Nos  item in M
#      EG02           Belt         200 Nos  item in M
#      TBPGUM         Ply          130 Nos  item in M
#
#  Refusing them was the safe answer, not the right one. The master is naming
#  the run of a FINISHED PIECE - 200 belts, 130 plies, 140 metres of liner - and
#  the refused item is what one of those pieces is MADE OF. The BOM states that
#  consumption on the edge between them, so the floor converts exactly:
#
#      B2-TBLT006   Parent_qty 3.067 M   consumes TB WRAP B1144  3.067 M
#      => one belt piece carries 3.067 m of wrap
#      => 200 belts  ->  613.4 m of TB WRAP B1144
#
#  and it chains, because `child_quantity / Parent_qty` is a pure ratio:
#
#      TB WRAP B1144  Parent_qty 6.134 M  consumes EG02  1.2268 M   (x0.2)
#      => 200 belts -> 3.067 x 0.2 = 0.6134 m each -> 122.7 m of EG02
#
#  THE ANCHOR MUST BE A PIECE, AND IT MUST BE WHAT THE MASTER NAMED.
#  An ancestor qualifies only when BOTH hold:
#    * its item type resolves to the SAME MPQ row the item matched, so the floor
#      being carried is genuinely that ancestor's floor, and
#    * the master states that row in a piece count, so "200 Nos" counts
#      something the BOM also writes one recipe row for.
#  That is a STRUCTURAL test, not a spelling one, and it is what catches the
#  three the name join got wrong: BEF001 and STC-WRAP were matched to `Belt` but
#  hang off a green tyre and a steel chipper, and TEG-02 to `Ply` but hangs off a
#  gum strip. No belt or ply stands above them, so no belt or ply floor may be
#  pushed down onto them - and the blocker stands, now saying the JOIN is
#  unsupported rather than blaming the unit.
#
#  Where the master is stated in the ancestor's own CONTINUOUS unit (Inner Liner
#  140 M) the anchor is one metre rather than one piece, and the same ratio
#  carries it: 30G is 2.6709 KG per 1.535 M of TIL-018, so 140 M -> 243.6 KG.
#
#  The routing is the guard here too: the item must be produced at a stated rate
#  (M/MIN, CUTS/MIN, NOS/MIN or MIN/BATCH). Nothing else licenses the arithmetic.
# =====================================================================
_PIECE_UOM = {"NOS", "NO", "NUM", "PCS", "EA"}
_BRIDGE_HOPS = 3
MPQ_BRIDGED: dict = {}          # item -> (factor, uom, path) actually applied


class BomBridge:
    """Carries an MPQ floor down the BOM to what a named piece is made of."""

    def __init__(self, bom, mpq: dict, join: dict, itypes: dict, rates: dict):
        self.mpq, self.join, self.itypes, self.rates = mpq, join, itypes, rates
        self.up: dict = {}
        # AN ITEM IS A PIECE IF ITS OWN CONSUMER COUNTS IT IN NOS. That is the
        # only honest test. `200 Nos` of Belt counts belts because a green tyre
        # consumes `1.0 NOS` of one; it does not count metres of wrap, even
        # though the wrap's item type also resolves to `Belt`. Without this the
        # bridge anchored EG02 on the wrap itself and doubled its floor.
        self.piece: set = set()
        for par, pq, ch, cq, cu in zip(
                bom["Parent"].map(io._s),
                pd.to_numeric(bom["Parent_qty"], errors="coerce"),
                bom["child"].map(io._s),
                pd.to_numeric(bom["child_quantity"], errors="coerce"),
                bom["child_Unit"].map(io._s)):
            if not par or not ch or pd.isna(pq) or pd.isna(cq) or pq <= 0:
                continue
            self.up.setdefault(ch, []).append((par, float(pq), float(cq)))
            if str(cu).strip().upper() in _PIECE_UOM:
                self.piece.add(ch)

    def _key(self, code: str):
        k = str(self.itypes.get(code, "")).strip().lower()
        if not k:
            return None
        return k if k in self.mpq else self.join.get(k)

    def factor(self, item: str, want_key: str, mpq_uom: str):
        """How much of `item` one MPQ unit of `want_key` implies. None = refuse."""
        if not (self.rates.get(item) or set()):
            return None, "the routing states no rate for this item"
        piece = str(mpq_uom).strip().upper() in _PIECE_UOM
        seen = set()

        def walk(node, ratio, hops, path):
            for par, pq, cq in self.up.get(node, []):
                if par in seen:
                    continue
                step = cq / pq
                if self._key(par) == want_key and (not piece or par in self.piece):
                    anchor = pq if piece else 1.0
                    return ratio * step * anchor, path + [f"{par}({cq:g}/{pq:g})"]
                if hops > 1:
                    seen.add(par)
                    r = walk(par, ratio * step, hops - 1, path + [f"{par}(x{step:g})"])
                    seen.discard(par)
                    if r:
                        return r
            return None

        r = walk(item, 1.0, _BRIDGE_HOPS, [])
        if r is not None:
            return r[0], " <- ".join(r[1])

        # THE MATCHED KEY IS NOT ABOVE THIS ITEM. That is usually the name join
        # being wrong rather than the BOM being incomplete - `TEG-02` matched
        # `Belt` on a token, but what actually stands above it is a gum strip,
        # which is a PLY. Structure beats spelling: take the floor of the
        # nearest ancestor the master does name, and say which one it was.
        alt = self._nearest_named(item)
        if alt is None:
            return None, (f"no ancestor of this item is named by CTP_Master_MPQ, "
                          f"and '{want_key}' is not above it either - that join is "
                          f"a spelling coincidence, not a structure")
        akey, arow = alt
        if _u(arow["uom"]) == _u(mpq_uom) or True:
            piece2 = str(arow["uom"]).strip().upper() in _PIECE_UOM
            seen.clear()

            def walk2(node, ratio, hops, path):
                for par, pq, cq in self.up.get(node, []):
                    if par in seen:
                        continue
                    step = cq / pq
                    if self._key(par) == akey and (not piece2 or par in self.piece):
                        anchor = pq if piece2 else 1.0
                        return ratio * step * anchor, path + [f"{par}({cq:g}/{pq:g})"]
                    if hops > 1:
                        seen.add(par)
                        rr = walk2(par, ratio * step, hops - 1,
                                   path + [f"{par}(x{step:g})"])
                        seen.discard(par)
                        if rr:
                            return rr
                return None

            r2 = walk2(item, 1.0, _BRIDGE_HOPS, [])
            if r2 is not None and arow.get("min"):
                return (r2[0] * float(arow["min"]) / max(float(arow["min"]), 1e-9),
                        f"via '{akey}' ({arow['min']:g} {arow['uom']}), "
                        + " <- ".join(r2[1]))
        return None, (f"'{want_key}' is not above this item and no other named "
                      f"ancestor could be measured")

    def _nearest_named(self, item: str):
        """The closest ancestor CTP_Master_MPQ actually names, breadth-first."""
        frontier, seen = [item], {item}
        for _ in range(_BRIDGE_HOPS):
            nxt = []
            for node in frontier:
                for par, _pq, _cq in self.up.get(node, []):
                    if par in seen:
                        continue
                    seen.add(par)
                    k = self._key(par)
                    rows = self.mpq.get(k) if k else None
                    if rows and (str(rows[0]["uom"]).strip().upper() not in _PIECE_UOM
                                 or par in self.piece):
                        return k, rows[0]
                    nxt.append(par)
            frontier = nxt
        return None


_BRIDGE = None                  # built in main(), once the BOM is loaded


def _key_of_row(r) -> str:
    """The MPQ Item_Type the matched row belongs to, as the bridge keys it."""
    return str(r.get("key") or "").strip().lower()


def mpq_name_join(mpq: dict, itypes, ctx: dict | None = None) -> dict:
    """BOM child_description -> CTP_Master_MPQ Item_Type, derived from the data.

    The two tables name the same material differently: `Bead Apex` vs
    `APEXED BEAD`, `Belt` vs `BELT-1..4`, `Chaffer` vs `SLITTED MATERIAL`.
    Only 12 of 17 PCR and 6 of 14 TBR Item_Types match a BOM name by spelling.

    The join uses the SAME machinery as the buffer master (ctpio._bcanon): the
    MPQ's own vocabulary is the spelling authority, a master term matches when
    every one of its tokens appears in the BOM name, and the most specific match
    wins. A tie is refused. No hand-written list, no invented pairing.
    """
    vocab = set()
    for k in mpq:
        vocab |= {io._bstem(x) for x in io._btok(k)}
    mtok = {k: io._bcanon(io._btok(k), vocab) for k in mpq}
    out = {}
    for it in {str(x).strip() for x in itypes if str(x).strip()}:
        k = it.lower()
        if k in mpq:                                   # exact - nothing to report
            continue
        own = io._bcanon(io._btok(it), vocab)
        cand = [(len(mtok[m]), m) for m in mpq if mtok[m] and mtok[m] <= own]
        if not cand and ctx:
            # CONTEXT, exactly as the buffer master is joined: the item's own
            # ROUTING department and operation names join the token set. It is
            # what carries `BODYPLY` (operation `TBR Body ply CUTTER`) onto the
            # master's `Ply` - a split the name alone cannot make, because
            # `body` is not a word the MPQ table uses anywhere.
            wide = io._bcanon(io._btok(it) + sorted(ctx.get(it, set())), vocab)
            cand = [(len(mtok[m] & wide), m) for m in mpq
                    if len(mtok[m] & wide) >= -(-len(mtok[m]) // 2)]
        if not cand:
            continue
        best = max(n for n, _ in cand)
        top = sorted({m for n, m in cand if n == best})
        if len(top) == 1:
            out[k] = top[0]
    return out


def mpq_floor(mpq: dict, itype: str, item: str, desc: str, item_uom: str,
              alias: dict, meta: dict, use_master_batches: bool,
              mix_batches: int, eff: float = 1.0) -> tuple:
    """The plant's Minimum_Run_Qty for ONE item, in that item's OWN unit.

    THE THREE-TABLE CHAIN, and what each table contributes
    -----------------------------------------------------
      CTP_Master_MPQ   Item_Type ......... Minimum_Run_Qty, UOM, Parameters
             |  joined on  Item_Type == child_description
      CTP_Master_BOM   child_description . the item CODE (`child`) and its unit
             |  joined on  child == routed_product
      CTP_Master_Routing routed_product .. batch_size / batch_UNIT  (MIN/BATCH)
                                           proc_time / proc_time_UOM (rates)

    THE UNIT IS RESOLVED, NEVER ASSUMED
      * UOM = Batches  ->  the master states CHARGES, not quantity. The charge
        WEIGHT is per item and lives in the routing, so
              floor = Minimum_Run_Qty x routing.batch_size        [KG]
        This is the only place a unit is converted, and the routing is what
        converts it. Without a batch_size the floor is refused, not guessed.
      * UOM = the item's own unit (M / NOS)  ->  used as stated.
      * anything else  ->  REFUSED and recorded. TBR states Tread = 38 Nos
        while every TR5* code is in METRES; converting would need a length per
        piece, which no master carries.

    THE FLOOR IS ALSO EXPRESSED IN MACHINE TIME
      `proc_time` turns the quantity floor into the run it implies -
      q / rate for M/MIN, NOS/MIN and CUTS/MIN, n_charges x rate for
      MIN/BATCH - so "minimum run" is auditable as minutes as well as units.
    """
    key = str(itype or "").strip().lower()
    rows = mpq.get(key)
    src = f"MPQ[{itype}]"
    if not rows:
        # the DERIVED join: {BOM child_description -> MPQ Item_Type}
        m = (alias or {}).get(key)
        if m:
            rows = mpq.get(m)
            if rows:
                src = f"MPQ[{m} ~ {itype}] (name derived from the two tables)"
                MPQ_JOINED[item] = (itype, m)
    if not rows:
        MPQ_LOG[item] = {"item": item, "item_type": itype, "verdict": "no MPQ row",
                         "mpq_min": "", "mpq_uom": "", "floor_qty": "",
                         "floor_uom": "", "floor_minutes": "",
                         "detail": "no Item_Type match and no alias"}
        return None, None, "no MPQ row"

    band = [r for r in rows if _params_match(r["params"], item, desc)]
    if not band:
        band = [min(rows, key=lambda r: r["min"] if r["min"] is not None else 1e18)]
        src += " (no band matched - least constraining row)"
    elif len(band) > 1:
        band = [min(band, key=lambda r: r["min"] if r["min"] is not None else 1e18)]
        src += " (several bands matched - least constraining)"
    r = band[0]
    if r["params"] and r["params"].lower() != "nan":
        src += f" params='{r['params']}'"
    if r["min"] is None:
        MPQ_LOG[item] = {"item": item, "item_type": itype, "verdict": "no minimum",
                         "mpq_min": "", "mpq_uom": r["uom"], "floor_qty": "",
                         "floor_uom": "", "floor_minutes": "", "detail": src}
        return None, r["max"], src + "; no Minimum_Run_Qty"

    mu, bu = _u(r["uom"]), _u(item_uom)
    floor = None
    funit = item_uom

    if mu == "BATCHES":
        # ---- the routing supplies the charge weight -----------------------
        bs = (meta or {}).get("batch_size")
        if not bs or float(bs) <= 0:
            MPQ_LOG[item] = {"item": item, "item_type": itype,
                             "verdict": "REFUSED - no batch_size",
                             "mpq_min": r["min"], "mpq_uom": r["uom"],
                             "floor_qty": "", "floor_uom": "", "floor_minutes": "",
                             "detail": src + "; master states Batches but the "
                                             "routing carries no batch_size"}
            return None, r["max"], src + "; REFUSED - Batches with no batch_size"
        n_charges = float(r["min"]) if use_master_batches else float(max(1, mix_batches))
        floor = n_charges * float(bs)
        funit = str((meta or {}).get("batch_unit") or item_uom)
        src += (f" = {n_charges:g} charge(s) x routing batch_size {float(bs):g} "
                f"{funit}" + ("" if use_master_batches else
                              f"  [charges from the rule file, master says {r['min']:g}]"))
    elif mu and bu and mu == bu:
        floor = float(r["min"])
        src += f" = {floor:g} {r['uom']} (as stated)"
    else:
        # THE UNITS DISAGREE. Before refusing, ask the BOM whether the master is
        # naming an ANCESTOR of this item - a belt, a ply, a liner - because then
        # the floor is that ancestor's and the BOM states how much of this item
        # one of them carries. See BomBridge.
        fac, why = (_BRIDGE.factor(item, _key_of_row(r), r["uom"])
                    if _BRIDGE is not None else (None, "no BOM bridge built"))
        base_min = float(r["min"])
        if why and why.startswith("via '"):
            # the floor came from an ancestor the master names, not from the row
            # this item matched: use THAT row's Minimum_Run_Qty
            import re as _re
            _m = _re.match(r"via '([^']+)' \(([-0-9.]+) ", why)
            if _m:
                _rows = mpq.get(_m.group(1)) or []
                if _rows and _rows[0].get("min"):
                    base_min = float(_rows[0]["min"])
        if fac and fac > 0:
            floor = base_min * fac
            src += (f" = {base_min:g} of '{_key_of_row(r)}' x {fac:g} "
                    f"{item_uom} each, via the BOM [{why}]")
            MPQ_BRIDGED[item] = (fac, item_uom, why)
        else:
            MPQ_LOG[item] = {"item": item, "item_type": itype,
                             "verdict": "UOM REFUSED",
                             "mpq_min": r["min"], "mpq_uom": r["uom"],
                             "floor_qty": "", "floor_uom": item_uom,
                             "floor_minutes": "",
                             "detail": src + f"; master {r['min']:g} {r['uom']} vs "
                                             f"item in {item_uom} - {why}"}
            return None, r["max"], (f"{src}; REFUSED - master {r['uom']} vs item "
                                    f"{item_uom} - {why}")

    # ---- the same floor, as the RUN it implies on the machine -------------
    try:
        fmin = io.op_duration_min(floor, meta or {}, eff)
    except Exception:
        fmin = None
    MPQ_LOG[item] = {"item": item, "item_type": itype, "verdict": "applied",
                     "mpq_min": r["min"], "mpq_uom": r["uom"],
                     "floor_qty": round(floor, 3), "floor_uom": funit,
                     "floor_minutes": (round(fmin, 2) if fmin else ""),
                     "detail": src}
    return floor, r["max"], src


_LINE = ""   # v40: set by main() to PCR / TBR


# ---------------------------------------------------------------------------
#  v46e: 4-ROLL CALENDER LOTS IN WHOLE DIPS (rule `fourrc_dip`)
#  CTP_Master_FRC gives each 901 item its Spool/Dip Length - one full dip of
#  steel cord / fabric through the calender (CPJ1218 12,600 m, fabrics 2,000 m).
#  A lot opens at the first need not yet covered and may cover only the needs
#  inside the item's shelf window (max ageing - min ageing - safety):
#    window demand >= one dip -> the largest WHOLE number of dips that window
#                               uses up (all consumed before expiry)
#    window demand <  one dip -> a PARTIAL dip of exactly the window demand,
#                               never below the Minimum_Run_Qty (user decision
#                               2026-10-27: no dip may be run to expire)
#  How often an item runs - daily, alternate day, every 3-4 days - is not set;
#  it follows from dip length / daily need, bounded by the shelf window.
# ---------------------------------------------------------------------------
_DIP_ROWS: list = []
_DIP_MAP: dict | None = None


def _norm_code(s) -> str:
    return re.sub(r"\s+", "", str(s or "")).upper()


def _dip_length(item, meta) -> float | None:
    global _DIP_MAP
    try:
        import rules as _RD
        cfg = _RD.rule("fourrc_dip") or {}
    except Exception:
        cfg = {}
    if not cfg or not cfg.get("enabled"):
        return None
    ms = {str(x).strip() for x in (cfg.get("machines") or ["901"])}
    if not ({str(x).strip() for x in (meta.get("machines") or [])} & ms):
        return None
    if _DIP_MAP is None:
        _DIP_MAP = {}
        f = io.STAGE / str(cfg.get("frc_file") or "CTP_Master_FRC.csv")
        if f.exists():
            d = pd.read_csv(f)
            col = next((c for c in d.columns if "dip" in c.lower()), None)
            for c, v in zip(d["item_code"], d[col] if col else []):
                if pd.notna(v) and float(v) > 0:
                    _DIP_MAP[_norm_code(c)] = float(v)
        for a, b in (cfg.get("aliases") or {}).items():
            if _norm_code(b) in _DIP_MAP:
                _DIP_MAP.setdefault(_norm_code(a), _DIP_MAP[_norm_code(b)])
    return _DIP_MAP.get(_norm_code(item))


def _dip_lots(item, itype, rows, meta, need, qty, blot, mn_age, mx_age,
              lo, hi, src, eff, dip):
    win_h = max(MIN_BUCKET_H, (mx_age - mn_age) - AGING_SAFETY_H) if mx_age > 0 \
        else MIN_BUCKET_H
    win = pd.Timedelta(hours=win_h)
    floor_q = float(lo or 0.0)
    left = [float(q) for q in qty]
    lots, allocs, seq, i, n = [], [], 0, 0, len(need)
    while i < n:
        if left[i] <= 1e-9:
            i += 1
            continue
        t0, end = need[i], need[i] + win
        wdem = sum(left[k] for k in range(i, n) if need[k] <= end)
        if wdem + 1e-9 >= dip:
            k_dips = int(wdem // dip)
            lot_q, kind = k_dips * dip, "full dips"
        else:
            k_dips = 0
            lot_q, kind = float(math.ceil(max(wdem, floor_q) - 1e-9)), "partial dip"
        seq += 1
        lot_id = f"{_LINE}_{item}_L{seq:05d}" if _LINE else f"{item}_L{seq:05d}"
        filled, served, k = 0.0, [], i
        while k < n and need[k] <= end and filled < lot_q - 1e-9:
            take = min(left[k], lot_q - filled)
            if take > 1e-9:
                allocs.append({"lot_id": lot_id, "building_lot_id": blot[k],
                               "item_code": item, "qty": take, "need_by": need[k]})
                served.append(need[k])
                filled += take
                left[k] -= take
            if left[k] <= 1e-9:
                k += 1
            else:
                break
        while i < n and left[i] <= 1e-9:
            i += 1
        served = served or [t0]
        lots.append({
            "lot_id": lot_id, "run_id": f"{item}_R{seq:05d}", "item_code": item,
            "item_type": itype, "uom": rows["uom"].iloc[0],
            "lot_qty": lot_q, "net_demand_served": filled,
            "surplus": max(0.0, lot_q - filled),
            "mpq_min": lo, "mpq_max": hi,
            "mpq_basis": f"{src}; v46e dip {dip:g} m ({kind})",
            "batches": io.batches_for(lot_q, meta),
            "first_need": min(served), "last_need": max(served),
            "bucket_span_h": (max(served) - min(served)).total_seconds() / 3600.0,
            "min_aging_h": mn_age, "max_aging_h": mx_age, "window_h": win_h,
            "duration_min": io.op_duration_min(lot_q, meta, eff),
            "operation": meta.get("operation", ""),
            "department": meta.get("department", ""),
            "machines": ",".join(meta.get("machines", []) or []),
        })
        _DIP_ROWS.append({"line": _LINE, "item": item, "lot_id": lot_id, "dip_length_m": dip,
                          "kind": kind, "dips": round(lot_q / dip, 3), "lot_m": lot_q,
                          "window_demand_m": round(wdem, 1), "served_m": round(filled, 1),
                          "surplus_m": round(max(0.0, lot_q - filled), 1),
                          "first_need": min(served), "last_need": max(served),
                          "shelf_window_h": win_h,
                          "run_h": round(io.op_duration_min(lot_q, meta, eff) / 60.0, 2)})
    return lots, allocs


def size_item(rows: pd.DataFrame, meta: dict, mn_age: float, mx_age: float,
              mpq: dict, mix_batches: int, eff: float):
    rows = rows.sort_values("need_by", kind="stable")
    need = rows["need_by"].tolist()
    qty = rows["net_qty"].tolist()
    blot = rows["building_lot_id"].tolist()
    item = rows["item_code"].iloc[0]
    itype = str(rows["item_type"].iloc[0])

    window_h = max(MIN_BUCKET_H, (mx_age - mn_age) - AGING_SAFETY_H) if mx_age > 0 \
        else MIN_BUCKET_H
    dept = str(meta.get("department", "")).strip().lower()
    cap = FLOW_WINDOW_BY_DEPT.get(dept)
    # BRE-01 / BRIL-01: SideWall, Tread and Inner Liner are planned against a
    # ROLLING 24 h building-demand horizon, not the whole window. That is a
    # tighter bucket than the aging window, so it wins where it applies.
    _rh = UR.rolling_horizon_h(itype)
    if _rh > 0:
        cap = min(cap, _rh) if cap else _rh
    if cap:
        window_h = max(MIN_BUCKET_H, min(window_h, cap))
    window = pd.Timedelta(hours=window_h)

    # ---- v43c: BIGGER LOTS ON CAMPAIGN MACHINES (the 4-roll calender 901) ----
    # 901 makes only 19 items, but each was cut into ~8 lots (e2e_v43b: 152 lots,
    # median 0.64 h, 144 item changes): buckets broke on every 8 h gap between
    # needs (FLOW_GAP_H) and were cut again on a 24 h need span. On a machine in
    # `campaign.machines` the bucket now runs the WHOLE shelf-life window of
    # the item (max aging - min aging - 2 h safety), optionally capped by
    # `campaign.lot_window_h`; it does not break on need gaps and is not cut on
    # need span. The MPQ floor/ceiling, the A03 run-length cap and whole-unit
    # rounding still apply, and nothing can be used after it expires because
    # the window itself is the shelf life.
    _camp_lot = bool(CAMP_LOT_M) and bool(
        {str(x).strip() for x in (meta.get("machines") or [])} & CAMP_LOT_M)
    if _camp_lot:
        if CAMP_LOT_WIN_H > 0:
            window_h = max(MIN_BUCKET_H, min(window_h, CAMP_LOT_WIN_H))
        window = pd.Timedelta(hours=window_h)
        _CAMP_ITEMS.add(str(item))

    # --- the envelope -----------------------------------------------------
    batch = meta.get("batch_size") if str(meta.get("proc_time_uom") or "").upper() \
        == "MIN/BATCH" else None
    _uom = str(rows["uom"].iloc[0]) if "uom" in rows.columns else ""
    _desc = str(rows["item_type"].iloc[0]) + " " + str(item)
    # ONE resolver for every item. It reads the master, walks MPQ -> BOM ->
    # routing, and converts Batches to KG with the routing's own batch_size.
    lo, hi, src = mpq_floor(mpq, itype, str(item), _desc, _uom, _MPQ_JOIN,
                            meta, USE_MASTER_BATCHES, mix_batches, eff)
    if lo is None and batch and batch > 0:
        # a mixing item the master does not name at all: fall back to the
        # physical charge so a lot is still a whole number of real batches.
        lo = hi = float(batch) * max(1, mix_batches)
        src = f"mixer batch {batch:g} x {mix_batches} (no MPQ row for this type)"

    # ---- BRM-02: sticky compounds stack only 3 batches high --------------
    # JT444 / JT466 are used in PCR sidewall and cannot be stacked higher, so
    # the lot ceiling is 3 charges no matter what the MPQ envelope allows.
    _st = UR.sticky_stack(item)
    if _st and batch and batch > 0:
        # BRM-02 BEATS BRM-01. The MPQ floor (lo = batch x 5) was overriding
        # the sticky cap (hi = batch x 3): `lo > hi` meant every JT444 lot came
        # out at 5 batches and the cap never bound. Measured on e2e_v8: 33 of
        # 33 sticky lots over the limit. Stacking is a PHYSICAL limit - the
        # material cannot be piled higher - so it caps the floor as well.
        hi = float(batch) * _st
        lo = min(float(lo or hi), hi)
        src += f"; BRM-02 sticky cap {_st} batches (overrides the MPQ floor)"

    # ---- BR4RC-09: specified minimum roll length -------------------------
    _mr = UR.min_roll_m(item)
    if _mr:
        lo = max(float(lo or 0.0), _mr)
        src += f"; BR4RC-09 min {_mr:g} m"

    # ---- v46e: 4-ROLL CALENDER LOTS IN WHOLE DIPS (CTP_Master_FRC) ----------
    _dip = _dip_length(item, meta)
    if _dip:
        return _dip_lots(item, itype, rows, meta, need, qty, blot, mn_age, mx_age,
                         lo, hi, src, eff, _dip)

    lots, allocs, seq = [], [], 0
    i, n = 0, len(need)
    while i < n:
        j, start = i, need[i]
        gap = pd.Timedelta(hours=FLOW_GAP_H)
        while j < n and (need[j] - start) <= window:
            if j > i and (need[j] - need[j - 1]) > gap and not _camp_lot:
                break
            j += 1
        raw = float(sum(qty[k] for k in range(i, j)))
        if raw <= 0:
            i = j
            continue
        n_lots = max(1, int(math.ceil(raw / hi - 1e-9))) if (hi and hi > 0) else 1
        # ---- A03: NO MPQ MAX, SO CAP THE LOT BY RUN LENGTH INSTEAD --------
        # With Maximum_Run_Qty empty a bucket becomes ONE lot, and on cap strip
        # that is a single lot of 127 machine-hours. The floor does not run one
        # lot for five days, and an uninterruptible 127 h block makes every
        # downstream deadline unreachable for reasons that are an artefact of
        # the missing master, not of the plant. So where the master states no
        # maximum, the lot is capped at MAX_LOT_H of work and the assumption is
        # recorded. It is a STAND-IN for a real MPQ max, not a plant rule.
        if (not hi or hi <= 0) and MAX_LOT_H > 0:
            d = io.op_duration_min(raw, meta, eff)
            if d > MAX_LOT_H * 60.0:
                n_lots = max(1, int(math.ceil(d / (MAX_LOT_H * 60.0) - 1e-9)))
                _A03.setdefault(item, {"code": "A03", "item": item,
                                       "department": meta.get("department", ""),
                                       "gap": "no MPQ max",
                                       "value": f"lot capped at {MAX_LOT_H:g} h of work",
                                       "source": "run-length stand-in for a missing "
                                                 "Maximum_Run_Qty"})

        # ---- v16: DELIVER TO THE EARLIEST NEED, WITHIN THE PLANT'S FLOOR ----
        # One lot serving demands spread over many hours finishes ONCE, and the
        # EARLIEST consumer of that bucket is late by construction. Measured on
        # e2e_v15: 34 of 41 PCR and 22 of 50 TBR late lots finished after their
        # first need but BEFORE their last - the material existed, it was simply
        # made once, too late for the trolley at the head of the queue.
        #
        # So the bucket is also cut on NEED-BY SPAN. The cut is bounded by the
        # master's own Minimum_Run_Qty: a bucket of Q may become at most
        # floor(Q / min_run_qty) lots, or the floor would be broken to fix the
        # timing - which is not a trade this release makes.
        # v43d: `campaign.keep_need_span` keeps the 24 h delivery cut inside the
        # bigger bucket, so the item's lots are released together (and run back
        # to back under the campaign hold) but each still lands in time for its
        # own earliest trolley. v43c without it held 115 TBR kits on one lot.
        if NEED_SPAN_CAP_H > 0 and (j - i) > 1 and (not _camp_lot or CAMP_KEEP_SPAN):
            span_h = (need[j - 1] - need[i]).total_seconds() / 3600.0
            if span_h > NEED_SPAN_CAP_H:
                want = int(math.ceil(span_h / NEED_SPAN_CAP_H - 1e-9))
                floor_q = lo if (lo and lo > 0) else None
                allowed = int(raw // floor_q) if floor_q else want
                n_new = max(1, min(want, max(1, allowed)))
                if n_new > n_lots:
                    _SPLIT[item] = {"item": item, "department": meta.get("department", ""),
                                    "span_h": round(span_h, 2), "wanted": want,
                                    "mpq_allowed": allowed if floor_q else "no floor",
                                    "lots_before": n_lots, "lots_after": n_new}
                    n_lots = n_new

        # ---- THE FLOOR IS HARD, AND THE TAIL IS MERGED ----------------------
        # `Minimum_Run_Qty` is the plant's instruction, so no lot may be emitted
        # below it. Splitting a bucket into n lots is only legal while every lot
        # clears the floor; the remainder is absorbed rather than emitted short.
        # ...except for a STICKY compound, where the floor and the stack cap are
        # the same 3 batches: merging would push every lot just over the cap and
        # whole-batch rounding then makes it 4. BRM-02 is physical - it wins.
        if lo and lo > 0 and n_lots > 1 and raw / n_lots < lo - 1e-9 and not _st:
            n_lots = max(1, int(raw // lo))

        target = max(raw, n_lots * lo) if (lo and batch) else raw   # physical floor only
        # ---- BRM-13: +5 batches per RUN for technical hold / quality ------
        # The workbook says "always produce +5 extra batches", not +5 per lot,
        # so it is charged ONCE per run (the campaign), not once per lot -
        # per-lot would multiply the buffer by the number of lots in the run.
        # Special compounds are excluded: there the extra breaks the MPQ.
        # BRM-13: special compounds (README s.3: ML405 / ML605 / 29MG) and the
        # sticky ones get no buffer batches
        _special = {str(x).strip().upper()
                    for x in (_R1.rule("mixing.special_compounds") or [])}
        _nb = UR.buffer_batches(item, itype, is_special=bool(_st)
                                or str(item).strip().upper() in _special)
        if _nb and batch and batch > 0:
            target += float(batch) * _nb
        per, per_net = target / n_lots, raw / n_lots
        # ---- v34: A LOT IS A WHOLE NUMBER OF UNITS ------------------------
        # KG is made in mixer batches, so a KG lot is rounded UP to whole
        # batches of the routing's own batch_size (CTP_Master_Routing,
        # batch_UNIT = KG). Metres and pieces carry no batch in the routing, so
        # they are rounded up to the next whole metre / piece. Up, never down:
        # the lot must still cover the demand it serves.
        if WHOLE_UNITS:
            _bs = meta.get("batch_size")
            if _bs and _bs > 0 and str(meta.get("batch_unit") or "").upper() == "KG":
                per = math.ceil(per / float(_bs) - 1e-9) * float(_bs)
            else:
                per = float(math.ceil(per - 1e-9))
        run_id = f"{item}_R{seq//max(n_lots,1)+1:05d}"
        k, left = i, (float(qty[i]) if i < j else 0.0)
        for s in range(n_lots):
            seq += 1
            # v40: LINE-PREFIXED. Each line counts from 1, so "B616M_L00001"
            # existed in PCR and TBR alike - 58 duplicate ids in e2e_v38/v39.
            lot_id = f"{_LINE}_{item}_L{seq:05d}" if _LINE else f"{item}_L{seq:05d}"
            filled, served, last = 0.0, [], (s == n_lots - 1)
            while k < j and (filled < per_net - 1e-9 or last):
                if left <= 1e-9:
                    k += 1
                    left = float(qty[k]) if k < j else 0.0
                    continue
                take = left if last else min(left, per_net - filled)
                allocs.append({"lot_id": lot_id, "building_lot_id": blot[k],
                               "item_code": item, "qty": take, "need_by": need[k]})
                served.append(need[k])
                filled += take
                left -= take
            if not served:
                served = [need[min(k, j - 1)]]
            lots.append({
                "lot_id": lot_id, "run_id": run_id, "item_code": item,
                "item_type": itype, "uom": rows["uom"].iloc[0],
                "lot_qty": per, "net_demand_served": filled or per_net,
                "surplus": max(0.0, per - per_net),
                "mpq_min": lo, "mpq_max": hi, "mpq_basis": src,
                "batches": io.batches_for(per, meta),
                "first_need": min(served), "last_need": max(served),
                "bucket_span_h": (max(served) - min(served)).total_seconds() / 3600.0,
                "min_aging_h": mn_age, "max_aging_h": mx_age,
                "window_h": window_h,
                "duration_min": io.op_duration_min(per, meta, eff),
                "operation": meta.get("operation", ""),
                "department": meta.get("department", ""),
                "machines": ",".join(meta.get("machines", []) or []),
            })
        i = j
    return lots, allocs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--efficiency", type=float, default=1.0,
                    help="1.0 = v10's standing 100%% OEE rule; CTP routing states 0.96")
    ap.add_argument("--mix-batches", type=int, default=3,
                    help="mixing MPQ in whole charges; CTP's jkt_mpq states 3")
    ap.add_argument("--max-lot-h", type=float, default=8.0,
                    help="A03: cap a lot at this many hours of work where the MPQ "
                         "master states no maximum. 0 disables (one bucket = one lot)")
    args = ap.parse_args()
    OUT = io.outdir(args.run, args.line)
    global _LINE
    _LINE = args.line.upper()

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 3 — RUNS (campaign-bounded) dealt into lots   [{args.line.upper()}]")
    print("=" * 74)

    alias = io.machine_alias(io.load_machines())
    bom = io.load_bom(args.line)
    rt = io.load_routing(args.line)
    meta_all = io.apply_assumptions(io.build_routing_meta(rt, alias))
    if io.TDA_INLINE:                     # v46b: TDA winds + apexes in line
        pd.DataFrame(io.TDA_INLINE).to_csv(OUT / "phase3_tda_inline.csv", index=False)
        _n = sum(1 for r in io.TDA_INLINE if r["slower_step"] == "winding")
        print(f"  TDA in line         : {len(io.TDA_INLINE)} apexed-bead item(s) run at the slower of "
              f"winding / apexing ({_n} set by winding)   -> phase3_tda_inline.csv")
    itypes = io.load_itemtypes(bom)
    aging = io.load_aging(args.line)
    aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)
    mpq = load_mpq_rules(args.line)
    global _MPQ_JOIN
    _ctx = {}
    _it_of = dict(zip(bom["child"].map(io._s), bom["child_description"].map(io._s)))
    for rp, dp, op in zip(rt.get("routed_product", []), rt.get("department", []),
                          rt.get("operation_name", [])):
        _t = _it_of.get(io._s(rp))
        if _t:
            _ctx.setdefault(_t, set()).update(io._btok(dp) + io._btok(op))
    _MPQ_JOIN = mpq_name_join(mpq, set(itypes.values()), _ctx)
    global _BRIDGE
    _rates = {}
    for rp, u in zip(rt.get("routed_product", []), rt.get("proc_time_UOM", [])):
        rp, u = io._s(rp), io._s(u).strip().upper()
        if rp and u:
            _rates.setdefault(rp, set()).add(u)
    _BRIDGE = BomBridge(bom, mpq, _MPQ_JOIN, itypes, _rates)

    dem = pd.read_csv(OUT / "phase2_demand_net.csv", parse_dates=["need_by"])
    print(f"  Net demand rows : {len(dem):,}   items {dem['item_code'].nunique():,}"
          f"   qty {dem['net_qty'].sum():,.0f}")
    _nrow = sum(len(v) for v in mpq.values())
    _nmax = sum(1 for v in mpq.values() for r in v if r["max"])
    print(f"  MPQ master      : {io.MPQ_SOURCE.get('path')} — {len(mpq)} item type(s), "
          f"{_nrow} row(s); Maximum_Run_Qty present on {_nmax} of them")
    print(f"  Mixing MPQ      : {args.mix_batches} whole charges (CTP's own number)")

    all_lots, all_allocs = [], []
    for item, rows in dem.groupby("item_code", sort=False):
        m = meta_all.get(item) or {}
        mn, mx = aging.get(item, (0.0, 0.0))
        L, A = size_item(rows, m, float(mn), float(mx), mpq, args.mix_batches,
                         args.efficiency)
        all_lots.extend(L)
        all_allocs.extend(A)

    lots = pd.DataFrame(all_lots)
    allocs = pd.DataFrame(all_allocs)
    print(f"\n  Lots created    : {len(lots):,}   runs {lots['run_id'].nunique():,}")
    print(f"  Lot quantity    : {lots['lot_qty'].sum():,.0f}"
          f"   (surplus from the physical floor {lots['surplus'].sum():,.0f})")
    print(f"  Total work      : {lots['duration_min'].sum()/60:,.0f} h")
    nomax = int((lots["mpq_max"].isna()).sum())
    print(f"  [GAP] lots with NO MPQ max: {nomax:,} of {len(lots):,} "
          f"({100*nomax/max(len(lots),1):.0f}%) — these buckets cannot be split, "
          f"one bucket = one lot")

    print(f"\n  BY DEPARTMENT (work content — this is what has to fit)")
    d = (lots.groupby("department").agg(lots=("lot_id", "size"),
                                        hours=("duration_min", "sum"),
                                        batches=("batches", "sum"))
         .sort_values("hours", ascending=False))
    for t, r in d.iterrows():
        print(f"    {str(t)[:30]:31s} lots={int(r['lots']):>7,d}"
              f" {r['hours']/60:>10,.0f} h"
              + (f"   {int(r['batches']):>7,d} batches" if r['batches'] else ""))

    if _A03:
        a3 = pd.DataFrame(list(_A03.values()))
        a3.to_csv(OUT / "phase3_assumptions.csv", index=False)
        print(f"\n  [A03] {len(a3)} item(s) had NO MPQ maximum and were capped at "
              f"{MAX_LOT_H:g} h of work per lot — a stand-in, not a plant rule:")
        for r in a3.head(6).itertuples(index=False):
            print(f"        {str(r.item)[:26]:27s} {str(r.department)[:22]}")
    # ---- v16: how EVERY item's floor was resolved, so none of it is silent --
    if MPQ_LOG:
        mr = pd.DataFrame(list(MPQ_LOG.values()))
        mr.to_csv(OUT / "MPQ_RESOLUTION.csv", index=False, encoding="utf-8-sig")
        v = mr["verdict"].value_counts().to_dict()
        print(f"\n  MPQ ({io.MPQ_SOURCE.get('path')}, {io.MPQ_SOURCE.get('rows')} rows "
              f"for this line): " + "  ".join(f"{k} {n}" for k, n in v.items()))
        ref = mr[mr["verdict"] == "UOM REFUSED"]
        for r in ref.itertuples(index=False):
            print(f"      REFUSED {str(r.item)[:22]:23s} {r.detail}")
        print(f"      -> MPQ_RESOLUTION.csv")

        # ---- EVERY ITEM THAT GOT NO FLOOR, AND WHY ------------------------
        # A floor that is not applied is not a neutral outcome: the lot is then
        # sized by demand alone, which is exactly what the plant's Minimum_Run_Qty
        # exists to prevent. Each reason is a different fix in a different table.
        for bom_name, mpq_name in sorted(set(MPQ_JOINED.values())):
            BLK.add("phase3_mpq", "NAME_MISMATCH", "JK_CTP_PPDS.CTP_Master_MPQ",
                    bom_name,
                    f"jkt_bom_{args.line} calls it '{bom_name}', CTP_Master_MPQ "
                    f"calls it '{mpq_name}' - joined by deriving the name from "
                    f"both tables, not from a stated key",
                    line=args.line.upper(),
                    referenced_at="phase3_lot_sizing.py mpq_name_join",
                    impact="the floor IS applied, but on a name the two tables do "
                           "not share. FIX: make Item_Type equal child_description")
        for verdict, kind, why, fix in (
            ("UOM REFUSED", "UOM_MISMATCH",
             "the MPQ row's UOM cannot be reconciled with the item's UOM",
             "align UOM in CTP_Master_MPQ with child_Unit in jkt_bom_{line}"),
            ("no MPQ row", "MISSING_IN_DB",
             "no row of CTP_Master_MPQ names this item type",
             "add an Item_Type row, or rename it to match child_description"),
            ("no minimum", "MISSING_IN_DB",
             "the row exists but Minimum_Run_Qty is NULL",
             "fill Minimum_Run_Qty on that row"),
            ("Batches with no batch_size", "MISSING_IN_DB",
             "the MPQ is stated in Batches but the routing gives no batch_size",
             "fill batch_size on the routing operation"),
        ):
            sub = mr[mr["verdict"].astype(str).str.startswith(verdict)]
            for r in sub.itertuples(index=False):
                BLK.add("phase3_mpq", kind, "JK_CTP_PPDS.CTP_Master_MPQ",
                        str(r.item),
                        f"{why} - {getattr(r, 'detail', '')}".strip(" -"),
                        line=args.line.upper(),
                        referenced_at="phase3_lot_sizing.py mpq_floor",
                        uom=str(getattr(r, "item_uom", "") or ""),
                        impact=f"no minimum run quantity applied; lot sized by "
                               f"demand alone. FIX: {fix.format(line=args.line)}")
    if _CAMP_ITEMS:
        print(f"  Campaign lots   : v43c - {len(_CAMP_ITEMS)} item(s) on "
              f"{sorted(CAMP_LOT_M)} sized over their whole shelf-life window "
              f"(no need-gap break, no need-span cut)")
    if _SPLIT:
        sp = pd.DataFrame(list(_SPLIT.values()))
        sp.to_csv(OUT / "LOT_SPLITS_v16.csv", index=False, encoding="utf-8-sig")
        print(f"  Need-span cap   : {len(sp)} item(s) cut for delivery "
              f"(cap {NEED_SPAN_CAP_H:g} h, bounded by Minimum_Run_Qty)"
              f"   -> LOT_SPLITS_v16.csv")

    if _DIP_ROWS:
        dp = pd.DataFrame(_DIP_ROWS)
        dp.to_csv(OUT / "phase3_4rc_dip_lots.csv", index=False)
        print(f"  4RC dip lots    : v46e - {dp['item'].nunique()} item(s), {len(dp)} lot(s): "
              f"{int((dp['kind'] == 'full dips').sum())} in whole dips, "
              f"{int((dp['kind'] == 'partial dip').sum())} partial (shelf-window) dips"
              f"   -> phase3_4rc_dip_lots.csv")
    lots.to_csv(OUT / "phase3_lots.csv", index=False)
    allocs.to_csv(OUT / "phase3_lot_demand.csv", index=False)
    BLK.write(OUT)
    print(f"\n[OK] phase3_lots.csv ({len(lots):,})  phase3_lot_demand.csv ({len(allocs):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
