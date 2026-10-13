#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_warm_wip_kanban.py -- WARM seed that CANNOT age out (levers 1 + 2 fused at t0).

THE BUG this fixes (why warm made the floor WORSE, 92% vs cold 65%):
  build_warm_wip.py seeds every SHORT-shelf item with a FLAT `short_days` (=1 day) of demand,
  born at plan_start - 1h. FEFO then draws that seed FIRST (it is the oldest supply in the pool).
  Any seeded unit NOT consumed within (shelf - 1h) is therefore condemned by FEFO -- even though
  the seed itself is physically fresh. Seeding 1 whole day of a 24 h item guarantees the tail of
  that seed ages out. The seed became a self-inflicted age-out block.

THE RULE (kanban / CONWIP at t0):  on-hand of item i at t0 must be <= shelf_i x draw_rate_i.
  Equivalently: seed item i with EXACTLY the demand it faces inside its OWN shelf window
  [t0, t0 + shelf_i - margin]. Every seeded unit is then consumed before it can rot => the seed
  contributes ZERO FEFO age-outs by construction, while still covering the cold-start ramp.

  LONG-shelf, deep-pipeline items (compound/inner liner/calender, 72-360 h) are what actually
  kill the cold-start SLIDE, and they do not age inside the horizon -- so they still get the
  generous `long_days` window (capped at their own shelf, which is never binding here).

Per-item shelf is read from aging_master (authoritative); item_type SHELF dict is the fallback.

    python build_warm_wip_kanban.py <demand_csv> <bom_csv> <aging_master_csv> <out_csv> \
           [long_days=5] [gt_hours=6] [margin_h=2]

Writes the scheduler inventory schema:
    production_id, produced_time, itemcode, item_type, unit, inventory
`produced_time` = plan_start - margin_h so every seed lot is ALIVE and FRESH at t0
(age = margin_h << shelf), never future-dated.
Also writes <out_csv>.kanban.csv  = per-item kanban cap B_i = shelf_i x peak_local_draw_i,
the cap the in-run rope (lever 2 during execution) must enforce on the SLID drum.
"""
import sys
import pandas as pd
import numpy as np

# fallback shelf (h) by item_type when aging_master has no row. >48h = long-shelf.
SHELF_FALLBACK = {
    "STEEL BELT": 24, "PLY": 24, "CAP STRIP": 24, "CAPSTRIP MOTHER ROLL": 72,
    "SLITTED MATERIAL": 48, "STEEL BELT EDGE STRIP": 48, "RUBBERIZED PLY": 24,
    "GUM STRIP": 24, "EDGE GUM": 72, "EDGE STRIP": 72, "NYLON-1": 24,
    "BEAD APEX": 48, "APEXED BEAD": 48, "BEAD BUNDLE": 72, "GREEN TYRES": 24,
    "PRE CUT ROLL MATERIAL": 72, "TREAD": 72, "CALANDARED ROLL": 96, "SQUEEGEE": 72,
    "INNER LINER": 72, "SIDEWALL": 72, "PRE-ASSEMBLY": 72,
    "FINAL COMPOUND": 120, "MASTER COMPOUND": 360,
}
SHORT_MAX_H = 48.0   # items with shelf <= this are seeded ONLY one shelf-window (kanban)


def main():
    dem_csv, bom_csv, aging_csv, out_csv = sys.argv[1:5]
    long_days = float(sys.argv[5]) if len(sys.argv) > 5 else 5.0
    gt_hours  = float(sys.argv[6]) if len(sys.argv) > 6 else 6.0
    margin_h  = float(sys.argv[7]) if len(sys.argv) > 7 else 2.0
    # 2026-08-03: item_types to NOT seed -> their demand flows to the RUNWAY machines (they run
    # from t-2 producing it fresh, AEST-within-shelf) instead of sitting idle behind a static seed.
    # Pass e.g. "MASTER COMPOUND,FINAL COMPOUND" so the mixers run in the runway (kills the chain ramp).
    no_seed = {t.strip().upper() for t in (sys.argv[8].split(",") if len(sys.argv) > 8 and sys.argv[8] else [])}

    dem = pd.read_csv(dem_csv, low_memory=False)
    dem["need_by"] = pd.to_datetime(dem["need_by"], errors="coerce")
    dem = dem.dropna(subset=["need_by"])
    dem["demand_qty"] = pd.to_numeric(dem["demand_qty"], errors="coerce").fillna(0.0)
    plan_start = dem["need_by"].min()
    unit = dem.groupby("item_code")["demand_uom"].first()

    bom = pd.read_csv(bom_csv, low_memory=False, dtype=str).fillna("")
    itype = (bom.groupby(bom["child"].astype(str).str.strip())["child_description"]
                .first().to_dict()) if "child_description" in bom.columns else {}

    ag = pd.read_csv(aging_csv)
    ag["ItemCode"] = ag["ItemCode"].astype(str).str.strip()
    ag_max = pd.to_numeric(ag.set_index("ItemCode")["MaxAging"], errors="coerce").to_dict()

    def _type(code):
        c = str(code).strip()
        return "Green Tyres" if c.upper().startswith(("GT ", "GT-")) else itype.get(c, "")

    def _shelf_h(code, it):
        m = ag_max.get(str(code).strip())
        if m is not None and pd.notna(m) and m > 0:
            return float(m)
        return float(SHELF_FALLBACK.get(str(it).strip().upper(), 72))

    # cumulative demand of item i inside a window [plan_start, plan_start + hours)
    def _cum_within(hours):
        end = plan_start + pd.Timedelta(hours=hours)
        w = dem[(dem["need_by"] >= plan_start) & (dem["need_by"] < end)]
        return w.groupby("item_code")["demand_qty"].sum()

    long_open = _cum_within(long_days * 24.0)

    # peak local draw rate per item over a rolling shelf-window (units per shelf-window),
    # used to publish the kanban cap the in-run rope must hold.
    def _peak_window(code, shelf_h):
        d = dem[dem["item_code"] == code].sort_values("need_by")
        if d.empty:
            return 0.0
        t = d["need_by"].values.astype("datetime64[ns]")
        q = d["demand_qty"].values
        w = np.timedelta64(int(shelf_h * 3600), "s")
        peak = 0.0
        j = 0
        run = 0.0
        for i in range(len(t)):
            run += q[i]
            while t[i] - t[j] > w:
                run -= q[j]; j += 1
            peak = max(peak, run)
        return peak

    pt = (plan_start - pd.Timedelta(hours=margin_h)).strftime("%Y-%m-%d %H:%M:%S")
    seed_rows, kanban_rows = [], []
    for i, code in enumerate(sorted(long_open.index)):
        it = _type(code)
        if str(it).strip().upper() in no_seed:
            continue                        # demand-driven: this item is made in the runway, not seeded
        shelf = _shelf_h(code, it)
        if it == "Green Tyres":
            seed = _cum_within(gt_hours).get(code, 0.0)
        elif shelf <= SHORT_MAX_H:
            # KANBAN seed: exactly one shelf-window of draw (minus margin) -> cannot age out
            seed = _cum_within(max(shelf - margin_h, 1.0)).get(code, 0.0)
        else:
            # long-shelf: generous ramp cover, but never more than one shelf-window
            seed = min(long_open.get(code, 0.0), _cum_within(shelf).get(code, 0.0))
        # publish kanban cap for the in-run rope stage
        cap = _peak_window(code, shelf)
        if cap > 0:
            kanban_rows.append({"itemcode": str(code).strip(), "item_type": it,
                                "shelf_h": round(shelf, 1),
                                "kanban_cap_units": round(float(cap), 2)})
        if seed <= 0:
            continue
        seed_rows.append({
            "production_id": f"WARMK{i:06d}", "produced_time": pt,
            "itemcode": str(code).strip(), "item_type": it,
            "unit": str(unit.get(code, "")).strip(), "inventory": round(float(seed), 4),
        })

    out = pd.DataFrame(seed_rows, columns=["production_id", "produced_time", "itemcode",
                                           "item_type", "unit", "inventory"])
    out.to_csv(out_csv, index=False)
    pd.DataFrame(kanban_rows).to_csv(out_csv + ".kanban.csv", index=False)

    def _sh(it):
        return SHELF_FALLBACK.get(str(it).strip().upper(), 72)
    out["_sh"] = out["item_type"].map(_sh)
    shortq = out.loc[out["_sh"] <= SHORT_MAX_H, "inventory"].sum()
    longq  = out.loc[out["_sh"] >  SHORT_MAX_H, "inventory"].sum()
    gtq    = out.loc[out["item_type"] == "Green Tyres", "inventory"].sum()
    print(f"  kanban-warm: {len(out):,} items | short-shelf seed = 1 shelf-window "
          f"({shortq:,.0f}u, cannot age out) | long-shelf {long_days}d ({longq:,.0f}u) "
          f"| GT {gt_hours}h ({gtq:,.0f}u)")
    print(f"  wrote seed   -> {out_csv}")
    print(f"  wrote kanban -> {out_csv}.kanban.csv  (per-item cap for the in-run rope)")


if __name__ == "__main__":
    main()
