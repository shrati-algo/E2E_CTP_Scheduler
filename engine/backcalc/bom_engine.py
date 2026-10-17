"""BOM validation engine: the scheduler's DB BOM (JK_CTP_PPDS.CTP_Master_BOM) checked against the SMART BOM.

Read-only. Nothing here changes the scheduler, its inputs or the database.

How the two BOMs are written
----------------------------
DB BOM (CTP_Master_BOM): one block of rows per tyre SKU (Super_parent). Every row is an ABSOLUTE per-tyre quantity
for one branch of the tyre (grand_parent = the top component the row belongs to, e.g. B1-PBLT060). The same parent can
appear in several branches (CPJ1218 feeds belt 1 and belt 2); its Parent_qty is then the tyre total, while each row's
child_quantity is that branch's share. The scheduler (phase1b) adds all branches together. So the correct
"per unit of parent" ratio is  child_quantity / (that branch's quantity of the parent), never child / Parent_qty.

SMART BOM: one block of rows per output item (MaterialCode_O) and revision. Per-metre data:
  WeightPerMeter (kg of that input in 1 m of the output), Width (mm) x SquareMeterWeight (kg/m2),
  Weight (kg per piece), Length, Peace (pieces), Angle. 4RC rolls give kg/m2 only (the roll width is in Master Data).
Cutting / slitting keeps the weight: 1 m of a cut part holds (its width x roll kg/m2) kg of the roll, so the roll metres
used = that kg / (roll kg per metre). This is how every expected value below is calculated.
"""
import math
import os
import re
from functools import lru_cache

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # scheduler-no-cutter-slitter
HERE = os.path.dirname(os.path.abspath(__file__))
DB_BOM = os.path.join(ROOT, "db_pipeline", "raw", "JK_CTP_PPDS__CTP_Master_BOM.csv")
SMART_XLSX = os.path.join(ROOT, "reference", "SMART_BOM.xlsx")          # SMART BOM export (engine/reference)
SMART_CACHE = os.path.join(HERE, "cache", "smart_bom.pkl")
MASTER = os.path.join(ROOT, "Master Data", "Change Over Master")
PLAN = os.path.join(ROOT, "inputs", "plan.csv")

TOL_OK = 0.03          # within 3 % = OK
TOL_SMALL = 0.10       # 3-10 % = small difference

BASE_GROUPS = {"FINAL COMPOUND", "MASTER COMPOUND", "STEEL TYRE CORD", "NYLON FABRIC", "BEAD WIRE"}
ASSEMBLY_HINTS = ("BUILDING", "MODULE", "TBM", "PRE ASSEMBLY", "APEXED BEAD", "BEAD APEXING")


def norm(code):
    """Match key: upper case, no spaces / hyphens / underscores; numeric codes without leading zeros
    (the DB writes 000000001034284300 where SMART writes 1034284300)."""
    k = re.sub(r"[\s\-_]+", "", str(code).upper())
    return k.lstrip("0") if k.isdigit() else k


def num(v):
    try:
        f = float(v)
        return f if f == f else 0.0
    except (TypeError, ValueError):
        return 0.0


# ----------------------------------------------------------------------------------------------- loading
def load_db():
    d = pd.read_csv(DB_BOM, low_memory=False)
    for c in d.columns:
        if d[c].dtype == object:
            d[c] = d[c].astype(str).str.strip()
    d["Parent_unit"] = d.Parent_unit.str.upper()
    d["child_Unit"] = d.child_Unit.str.upper()
    return d


def load_smart():
    if os.path.exists(SMART_CACHE) and os.path.getmtime(SMART_CACHE) >= os.path.getmtime(SMART_XLSX):
        x = pd.read_pickle(SMART_CACHE)
    else:
        x = pd.read_excel(SMART_XLSX, sheet_name="in")
        os.makedirs(os.path.dirname(SMART_CACHE), exist_ok=True)
        x.to_pickle(SMART_CACHE)
    for c in ["MaterialCode_O", "MaterialCodeGroup_IN", "MaterialGroup_IN", "Unit", "EquipmentGroup"]:
        x[c] = x[c].astype(str).str.strip()
    for c in ["Weight", "WeightPerMeter", "Width", "SquareMeterWeight", "Revision", "Peace", "Length", "Angle",
              "Gauge", "PHR"]:
        x[c] = pd.to_numeric(x[c], errors="coerce").fillna(0.0)
    x = x[x.Revision == x.groupby("MaterialCode_O").Revision.transform("max")].copy()
    x["kO"] = x.MaterialCode_O.map(norm)
    x["kI"] = x.MaterialCodeGroup_IN.map(norm)
    x["GRP"] = x.MaterialGroup_IN.str.upper()
    x["UNIT"] = x.Unit.str.upper()
    return x


def load_roll_widths():
    """4RC roll width (m) per roll, Master Data 4RC.xlsx Sheet2 (fallback Sheet1 'Width')."""
    f = os.path.join(MASTER, "4RC.xlsx")
    w = {}
    s1 = pd.read_excel(f, sheet_name="Sheet1", header=None)
    for _, r in s1.iloc[3:].iterrows():
        if isinstance(r[0], str) and num(r[2]) > 0:
            w[norm(r[0])] = (num(r[2]), "4RC.xlsx Sheet1")
    s2 = pd.read_excel(f, sheet_name="Sheet2", header=None)
    for _, r in s2.iloc[1:].iterrows():
        if isinstance(r[0], str) and num(r[1]) > 0:
            w[norm(r[0])] = (num(r[1]), "4RC.xlsx Sheet2")
    alias = {"MONOCHAFFER": "MONOCHAFER", "CWBP650": "CWBP65"}
    for a, b in alias.items():
        if norm(a) in w and norm(b) not in w:
            w[norm(b)] = (w[norm(a)][0], w[norm(a)][1] + f" (named {a})")
    return w


def load_plan():
    if not os.path.exists(PLAN):
        return pd.Series(dtype=float)
    p = pd.read_csv(PLAN)
    return p.groupby(p.skuCode.astype(str).str.strip()).qty.sum()


# ----------------------------------------------------------------------------------------------- SMART model
class Smart:
    def __init__(self, x, roll_w):
        self.x = x
        self.roll_w = roll_w
        self.by_o = {k: g for k, g in x.groupby("kO")}
        self.code = x.drop_duplicates("kO").set_index("kO").MaterialCode_O.to_dict()
        self.eg = x.drop_duplicates("kO").set_index("kO").EquipmentGroup.to_dict()
        self.notes = []
        self.dup_factor = {}
        self.dup_info = {}

    def has(self, item):
        return norm(item) in self.by_o

    def is_assembly(self, k):
        eg = self.eg.get(k, "").upper()
        return any(h in eg for h in ASSEMBLY_HINTS)

    def is_base(self, r):
        return r.GRP in BASE_GROUPS or r.kI not in self.by_o

    def rows(self, item, prefer=()):
        """Input rows of `item`. Inputs of the same group but different codes are ALTERNATIVES on cutters /
        slitters (e.g. SNCT001 from BCFT001 / BCFT005 / BCFT002); keep the one the DB uses, else the first.
        Repeated rows of the SAME code are real (TBP027 has TBPGUM on both edges) and are all kept."""
        k = norm(item)
        g = self.by_o.get(k)
        if g is None:
            return []
        g = g[~g.kI.isin(["NOTAPPLICABLE", "NAN", ""])]
        if any(q in self.eg.get(k, "") for q in ("F270", "K310", "F430", "K270", "K430", "K440")):
            g = g.drop_duplicates("kI")          # mixing recipes: one row per ingredient
        g = self._resolve_duplicates(k, g)
        out = []
        if self.is_assembly(k):
            return list(g.itertuples())
        pref = {norm(p) for p in prefer}
        for grp, gg in g.groupby("GRP", sort=False):
            codes = list(dict.fromkeys(gg.kI))
            if grp in BASE_GROUPS or len(codes) == 1:
                out += list(gg.itertuples())
                continue
            keep = next((c for c in codes if c in pref), codes[0])
            out += list(gg[gg.kI == keep].itertuples())
        return out

    def consumer_rows(self, k):
        if not hasattr(self, "_cons"):
            self._cons = {kk: gg for kk, gg in self.x.groupby("kI")}
        return self._cons.get(k)

    def _resolve_duplicates(self, k, g):
        """The SMART export repeats some items' WHOLE row block k times (IL018 x3, GT 1513 XPC1 MSIL x2,
        CAR 1402 XPC TATA x8 -- copies may differ slightly, e.g. Peace 1 in one copy and 2 in the others).
        A block is a k-fold copy when every input code appears exactly k >= 2 times; it is counted once, using the
        most common row of each code. A partial repeat is real (TBP027 lists TBPGUM twice, one per edge, and
        SMART's own GT row 5.049 kg/m counts both)."""
        cnt = g.groupby("kI").size()
        if len(cnt) == 0 or cnt.min() < 2 or cnt.nunique() != 1:
            return g
        key = ["kI", "GRP", "WeightPerMeter", "Width", "SquareMeterWeight", "Weight", "Length", "Peace"]
        pick = (g.groupby(key, dropna=False).size().rename("n").reset_index()
                .sort_values("n", ascending=False).drop_duplicates("kI"))
        out = g.merge(pick[key], on=key, how="inner").drop_duplicates("kI")
        out = out.set_index(g.drop_duplicates("kI").index[:len(out)]) if len(out) == g.kI.nunique() else out
        self.dup_factor[k] = int(cnt.iloc[0])
        varies = int((g.groupby("kI")[key[2:]].nunique() > 1).any(axis=1).sum())
        self.dup_info[k] = (self.code.get(k, k), int(cnt.iloc[0]), varies)
        return out

    def alternatives(self, item):
        k = norm(item)
        g = self.by_o.get(k)
        if g is None or self.is_assembly(k):
            return {}
        res = {}
        for grp, gg in g.groupby("GRP"):
            codes = list(dict.fromkeys(gg.MaterialCodeGroup_IN))
            if grp not in BASE_GROUPS and len(codes) > 1:
                res[grp] = codes
        return res

    def piece_len_m(self, item):
        g = self.by_o.get(norm(item))
        if g is None:
            return 0.0
        L = g.Length.max()
        return L / 1000.0 if L > 50 else L

    @lru_cache(maxsize=None)
    def _whole_on_first(self, item):
        """True when the first row's Weight is the whole piece (= sum of WeightPerMeter x length), the tread pattern."""
        g = self.by_o.get(norm(item))
        pl = self.piece_len_m(item)
        if g is None or not pl:
            return False
        tot = g.drop_duplicates("kI").WeightPerMeter.sum() * pl
        w = g.Weight.max()
        return bool(tot > 0 and w > 0 and abs(w / tot - 1) <= 0.03 and (g.Weight > 0).sum() < g.kI.nunique())

    def roll_width(self, item):
        return self.roll_w.get(norm(item), (0.0, ""))

    def kg_link(self, item, r, unit):
        """kg of input r in 1 m (unit M) or 1 piece (unit NOS) of `item`; returns (kg, how)."""
        if unit == "NOS":
            pl = self.piece_len_m(item)
            if r.WeightPerMeter > 0 and pl > 0 and self._whole_on_first(item):
                # tread pattern: SMART puts the WHOLE piece weight in 'Weight' of the 1st row -> WeightPerMeter x length
                return r.WeightPerMeter * pl, f"WeightPerMeter x piece length {pl:g} m"
            if r.Weight > 0:
                return r.Weight, "Weight (kg per piece)"
            if r.WeightPerMeter > 0 and pl > 0:
                return r.WeightPerMeter * pl, f"WeightPerMeter x piece length {pl:g} m"
            if r.Width > 0 and r.SquareMeterWeight > 0 and pl > 0:
                return r.Width / 1000 * r.SquareMeterWeight * pl, "Width x kg/m2 x piece length"
            return 0.0, "no per-piece data"
        if r.WeightPerMeter > 0:
            return r.WeightPerMeter, "WeightPerMeter"
        if r.Width > 0 and r.SquareMeterWeight > 0:
            return r.Width / 1000 * r.SquareMeterWeight, f"Width {r.Width:g} mm x {r.SquareMeterWeight:g} kg/m2"
        if r.SquareMeterWeight > 0:
            w, src = self.roll_width(item)
            if w > 0:
                return w * r.SquareMeterWeight, f"roll width {w:g} m ({src}) x {r.SquareMeterWeight:g} kg/m2"
            return 0.0, "4RC roll width missing in Master Data"
        if r.Weight > 0:
            return r.Weight, "Weight (used as kg per m)"
        return 0.0, "no data"

    @lru_cache(maxsize=None)
    def kgpm(self, item, prefer=()):
        """Total base-material kg in 1 m of `item`."""
        return sum(self.explode(item, 1.0, "M", prefer).values())

    def explode(self, item, qty, unit, prefer=(), depth=0):
        """Base materials (final compounds, cord, fabric, wire) in `qty` of `item` -> {code: kg}.
        Linear in qty, so the result for 1 unit is cached and scaled."""
        if qty == 0 or depth > 12:
            return {}
        one = self._explode1(item, unit, tuple(prefer), depth)
        return {k: v * qty for k, v in one.items()}

    @lru_cache(maxsize=None)
    def _explode1(self, item, unit, prefer, depth):
        qty = 1.0
        out = {}
        rows = self.rows(item, prefer)
        if unit == "KG":
            tot = sum(self.kg_link(item, r, "M")[0] for r in rows if self.is_base(r))
            for r in rows:
                if self.is_base(r) and tot > 0:
                    out[r.MaterialCodeGroup_IN] = out.get(r.MaterialCodeGroup_IN, 0) + qty * self.kg_link(item, r, "M")[0] / tot
            return out
        for r in rows:
            kl, _ = self.kg_link(item, r, unit)
            if self.is_base(r):
                if r.GRP in BASE_GROUPS:
                    out[r.MaterialCodeGroup_IN] = out.get(r.MaterialCodeGroup_IN, 0) + qty * kl
                elif r.UNIT == "KG":       # an intermediate in kg with no SMART BOM of its own (apex profile AP 0018)
                    key_ = "UNRESOLVED|" + r.MaterialCodeGroup_IN
                    out[key_] = out.get(key_, 0) + qty * kl
                continue
            if r.UNIT == "NOS":
                sub = self.explode(r.MaterialCodeGroup_IN, qty * (r.Peace or 1), "NOS", prefer, depth + 1)
            elif r.UNIT == "KG":
                sub = self.explode(r.MaterialCodeGroup_IN, qty * kl, "KG", prefer, depth + 1)
            else:
                k_in = self.kgpm(r.MaterialCodeGroup_IN, prefer)
                sub = self.explode(r.MaterialCodeGroup_IN, qty * kl / k_in, "M", prefer, depth + 1) if k_in > 0 else {}
            for kk, v in sub.items():
                out[kk] = out.get(kk, 0) + v
        return out

    def expected_link(self, parent, child, punit, cunit, prefer=()):
        """Expected quantity of `child` per 1 unit of `parent` (in the DB's units). Returns dict."""
        rows = [r for r in self.rows(parent, prefer) if r.kI == norm(child)]
        alt_used = ""
        if not rows:
            for grp, codes in self.alternatives(parent).items():
                if norm(child) in {norm(c) for c in codes}:
                    rows = [r for r in self.by_o[norm(parent)].itertuples() if r.kI == norm(child)]
                    alt_used = f"alternative input in SMART group {grp}: {', '.join(codes)}"
        if not rows:
            return None
        kl = sum(self.kg_link(parent, r, punit)[0] for r in rows)
        how = "; ".join(sorted({self.kg_link(parent, r, punit)[1] for r in rows}))
        if kl == 0:
            for r2 in self.rows(parent, prefer):
                if not self.is_base(r2) and r2.UNIT == "KG":
                    k2, h2 = self.kg_link(parent, r2, punit)
                    sub = self.explode(r2.MaterialCodeGroup_IN, k2, "KG", prefer)
                    for kk, v in sub.items():
                        if norm(kk) == norm(child):
                            kl, how = v, f"through {r2.MaterialCodeGroup_IN} ({h2})"
        r0 = rows[0]
        res = {"smart_unit": "KG" if (r0.GRP in BASE_GROUPS) else r0.UNIT, "how": how, "alt": alt_used,
               "smart_width_mm": r0.Width, "smart_angle": r0.Angle, "smart_rows": len(rows),
               "kg_link": kl, "all_base_kg": sum(self.kg_link(parent, r, punit)[0]
                                                 for r in self.rows(parent, prefer) if self.is_base(r))}
        if res["smart_unit"] == "KG" or cunit == "KG":
            res["expected"] = kl
            res["basis"] = f"kg of {child} in 1 {'m' if punit == 'M' else 'piece'} of {parent} = {how}"
        elif res["smart_unit"] == "NOS" or cunit == "NOS":
            res["expected"] = sum((r.Peace or 1) for r in rows)
            res["basis"] = f"pieces of {child} per {parent} (SMART Peace)"
            if cunit == "M":
                k_in = self.kgpm(child, prefer)
                res["expected"] = kl / k_in if k_in > 0 else float("nan")
                res["basis"] = f"weight kept: {kl:.4f} kg / {k_in:.4f} kg per m of {child}"
        else:
            k_in = self.kgpm(child, prefer)
            res["expected"] = kl / k_in if k_in > 0 else float("nan")
            res["child_kg_per_m"] = k_in
            res["basis"] = f"weight kept: {kl:.4f} kg ({how}) / {k_in:.4f} kg per m of {child}"
        return res


# ----------------------------------------------------------------------------------------------- DB model
def db_branch_tables(d):
    """Per SKU: top-level rows (GT -> components), and per-branch quantity of every item."""
    d = d.copy()
    d["is_top"] = d.grand_parent == d.Super_parent
    q = (d[~d.is_top].groupby(["Super_parent", "grand_parent", "child", "child_Unit"]).child_quantity.sum()
         .reset_index().rename(columns={"child": "item", "child_quantity": "branch_qty", "child_Unit": "branch_unit"}))
    top = d[d.is_top & (d.child_description.str.upper() != "GREEN TYRES")]
    gt = d[d.is_top & (d.child_description.str.upper() == "GREEN TYRES")][["Super_parent", "child"]].rename(
        columns={"child": "GT"})
    return d, q, top, gt


def classify(rel, db_unit_issue, total_on_one, how):
    if rel is None or (isinstance(rel, float) and math.isnan(rel)):
        return "NOT CHECKABLE", ""
    if db_unit_issue:
        return "UNIT OF MEASURE", db_unit_issue
    if abs(rel - 1) <= TOL_OK:
        return "OK", ""
    if total_on_one:
        return "CALCULATION: whole weight on one input", total_on_one
    for k in (2, 3, 4, 5, 6, 7, 8, 10, 15):
        if abs(rel - k) / k <= TOL_OK:
            return f"CALCULATION: x{k}", f"DB is {k} times SMART"
        if abs(rel - 1 / k) * k <= TOL_OK:
            return f"CALCULATION: 1/{k}", f"DB is 1/{k} of SMART"
    if abs(rel - 1) <= TOL_SMALL:
        return "SMALL DIFFERENCE (3-10 %)", ""
    return "CALCULATION: different value", ""
