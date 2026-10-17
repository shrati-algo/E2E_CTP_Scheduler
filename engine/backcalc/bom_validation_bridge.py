"""GT / carcass component reader shared with bom_validation (copied: keep the two in step)."""
from bom_engine import norm
import pandas as pd


def smart_components(S, gt):
    k = norm(gt)
    g = S.by_o.get(k)
    if g is None:
        return {}
    g = S._resolve_duplicates(k, g)
    res = {}
    for r in g.itertuples():
        if r.kI in ("NOTAPPLICABLE", "NAN", ""):
            continue
        if r.GRP == "CARCASS" and S.has(r.MaterialCodeGroup_IN):
            for rr in S._resolve_duplicates(r.kI, S.by_o[r.kI]).itertuples():
                if rr.kI not in ("NOTAPPLICABLE", "NAN", ""):
                    res.setdefault(rr.kI, []).append(rr)
            continue
        res.setdefault(r.kI, []).append(r)
    return res


def smart_comp_qty_kg(rs):
    qty = kg = 0.0
    unit = rs[0].UNIT
    for r in rs:
        pc = r.Peace or 1
        qty += r.Length * pc if r.UNIT == "M" else pc
        if r.Weight > 0:
            kg += r.Weight
        elif r.WeightPerMeter > 0 and r.Length > 0:
            kg += r.WeightPerMeter * r.Length * pc
    return qty, unit, kg
