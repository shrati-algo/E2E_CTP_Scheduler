#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_warm_wip.py — synthesise a WARM-START opening-WIP snapshot from the plan itself.

WHY: the DB's opening_wip is wrong/missing for TBR (no opening_wip_tbr table) and colder than
reality for PCR, so the schedule starts COLD -> the building drum slides tens of days during a
ramp that a real running floor would never see. A steady-state floor holds, at t0, roughly the
inventory it consumes over the pipeline's own lead time. So we seed each item with the demand it
faces in the FIRST `warm_days` of the plan: enough to cover the ramp, no more. This is a realistic
warm start, not a makespan cheat -- it only stops the cold-start slide; it does not shorten the
30-day building plan.

    python build_warm_wip.py <demand_csv> <bom_csv> <out_csv> [long_days=5] [short_days=1] [gt_days=1]

Writes the scheduler's inventory.csv schema:
    production_id, produced_time, itemcode, item_type, unit, inventory
`produced_time` = plan_start - 1h so every seed lot is alive at t0 (never future-dated).

SHELF-AWARE windows (the correct warm start): the ramp slide is caused by the DEEP-PIPELINE
components (compound -> calender -> inner liner) that take days to build, so building waits for
them. Those are LONG-SHELF (72-360 h) and do NOT age -> seed them GENEROUSLY (long_days) to warm
the ramp, exactly as a real plant stocks compound. SHORT-SHELF components (belt/ply/cap strip/bead
apex, <=48 h) are cut fast and JIT -> seeding them deep just makes the surplus AGE, so give them a
SHORT window (short_days). GREEN TYRES get gt_days (24 h shelf; a small curing warm-up).
"""
import sys
import pandas as pd

# per item_type shelf life (h) -- drives the seed window. >48h = long-shelf (safe to stock deep).
SHELF = {
    "STEEL BELT": 24, "PLY": 24, "CAP STRIP": 24, "CAPSTRIP MOTHER ROLL": 24,
    "SLITTED MATERIAL": 24, "STEEL BELT EDGE STRIP": 24, "RUBBERIZED PLY": 24,
    "GUM STRIP": 24, "EDGE GUM": 24, "EDGE STRIP": 24, "NYLON-1": 24,
    "BEAD APEX": 48, "APEXED BEAD": 48, "GREEN TYRES": 24,
    "PRE CUT ROLL MATERIAL": 72, "TREAD": 72, "CALANDARED ROLL": 72, "SQUEEGEE": 72,
    "INNER LINER": 72, "SIDEWALL": 72, "PRE-ASSEMBLY": 72, "BEAD BUNDLE": 72,
    "FINAL COMPOUND": 120, "MASTER COMPOUND": 360,
}

def main():
    dem_csv = sys.argv[1]
    bom_csv = sys.argv[2]
    out_csv = sys.argv[3]
    long_days = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0
    short_days = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
    gt_days = float(sys.argv[6]) if len(sys.argv) > 6 else 1.0

    dem = pd.read_csv(dem_csv, low_memory=False)
    dem["need_by"] = pd.to_datetime(dem["need_by"], errors="coerce")
    dem = dem.dropna(subset=["need_by"])
    dem["demand_qty"] = pd.to_numeric(dem["demand_qty"], errors="coerce").fillna(0.0)

    plan_start = dem["need_by"].min()
    unit = dem.groupby("item_code")["demand_uom"].first()

    # item_type: the BOM child_description IS the ItemType (per project memory); GT -> Green Tyres
    bom = pd.read_csv(bom_csv, low_memory=False, dtype=str).fillna("")
    itype = {}
    if "child" in bom.columns and "child_description" in bom.columns:
        itype = (bom.groupby(bom["child"].astype(str).str.strip())["child_description"]
                    .first().to_dict())

    def _type(code):
        c = str(code).strip()
        if c.upper().startswith("GT ") or c.upper().startswith("GT-"):
            return "Green Tyres"
        return itype.get(c, "")

    def _cum_to(days):
        end = plan_start + pd.Timedelta(days=days)
        w = dem[(dem["need_by"] >= plan_start) & (dem["need_by"] < end)]
        return w.groupby("item_code")["demand_qty"].sum()

    long_open = _cum_to(long_days)
    short_open = _cum_to(short_days)
    gt_open = _cum_to(gt_days)

    def _window_qty(code, it):
        sh = SHELF.get(str(it).strip().upper(), 72)      # unknown -> treat as long-ish (72)
        if it == "Green Tyres":
            return gt_open.get(code, 0.0)
        if sh <= 48:                                     # short-shelf -> minimal seed (avoid aging)
            return short_open.get(code, 0.0)
        return long_open.get(code, 0.0)                  # long-shelf/deep-pipeline -> generous seed

    pt = (plan_start - pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for i, code in enumerate(sorted(long_open.index)):
        it = _type(code)
        qty = _window_qty(code, it)
        if qty <= 0:
            continue
        rows.append({
            "production_id": f"WARM{i:06d}",
            "produced_time": pt,
            "itemcode": str(code).strip(),
            "item_type": it,
            "unit": str(unit.get(code, "")).strip(),
            "inventory": round(float(qty), 4),
        })
    out = pd.DataFrame(rows, columns=["production_id", "produced_time", "itemcode",
                                      "item_type", "unit", "inventory"])
    out.to_csv(out_csv, index=False)
    gt = out[out["item_type"] == "Green Tyres"]["inventory"].sum()
    out["_sh"] = out["item_type"].str.upper().map(lambda t: SHELF.get(str(t).strip(), 72))
    longq = out[out["_sh"] > 48]["inventory"].sum(); shortq = out[out["_sh"] <= 48]["inventory"].sum()
    print(f"  warm-WIP: {len(out):,} items | long-shelf window {long_days}d ({longq:,.0f}u), "
          f"short-shelf {short_days}d ({shortq:,.0f}u), GT {gt_days}d ({gt:,.0f}u)")
    print(f"  wrote -> {out_csv}")

if __name__ == "__main__":
    main()
