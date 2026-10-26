#!/usr/bin/env python3
"""make_curing_plan.py — derive the curing anchor the plan-files do not carry.

    python make_curing_plan.py

WHY THIS EXISTS
  Phase 7 gates curing on the green tyres that actually exist, but it needs a
  curing plan to gate. The lot-wise solver normally supplies one. The
  September plan-files supply only a BUILDING schedule, so the anchor has to
  be built here — from the plant's own masters, not invented:

    cycle time      jkt_routing_<line>, the CURING row for the SKU
    allowable press the same row's `machines` list
    mould positions Master_WC_Master_Curing_<line> (each press has LHS + RHS)

  Nothing here decides WHETHER a tyre can be cured — phase 7 still does that,
  against real green tyres. This only says which press would take it and when,
  so phase 7 has something to gate.

ASSUMPTIONS — stated because they are mine, not the plant's
  A-CURE-1  A press cures `mould positions` tyres per cycle (2 on every CTP
            press: LHS and RHS). The master carries no per-mould SKU
            restriction for these SKUs, so both positions are treated as
            interchangeable.
  A-CURE-2  Green tyres are available at the BUILD END of their plan row.
  A-CURE-3  A building row is spread over at most `--max-presses` presses so
            one row does not monopolise the press floor; each press lot is
            aimed at roughly one shift of work.
  A-CURE-4  No press changeover or mould change is charged. CTP has no press
            changeover master, so charging a guess would be worse than zero.
"""
from __future__ import annotations

import argparse
import math
import pathlib
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io

TARGET_BLOCK_MIN = 480.0          # aim each press lot at about one shift


def cycle_min(entry: dict) -> float | None:
    """Curing cycle per TYRE, in minutes, from the routing's own UOM."""
    v, u = entry.get("proc_time"), str(entry.get("uom") or "").upper()
    if v is None or float(v) <= 0:
        return None
    v = float(v)
    if u == "SEC":
        return v / 60.0
    if u in ("MIN", "MIN/BATCH", ""):
        return v
    if u == "HOURS":
        return v * 60.0
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-presses", type=int, default=12)
    a = ap.parse_args()

    print("\n" + "=" * 76)
    print("  CTP — deriving the curing anchor from the building plan")
    print("=" * 76)

    alias = io.machine_alias(io.load_machines())
    for line in ("pcr", "tbr"):
        rt = io.load_routing(line)
        ci = io.cure_index(rt)
        plan, pstats = io.load_building_plan(line, alias)

        # presses and how many mould positions each has
        pm = pd.read_csv(io.STAGE / f"Master_WC_Master_Curing_{line}.csv",
                         encoding="utf-8-sig")
        pm = pm[pm.get("is_active", 1) == 1]
        pm["press"] = pm["machineName"].map(
            lambda x: str(int(float(x))) if pd.notna(x) else "")
        moulds = pm.groupby("press").size().to_dict()

        free: dict[str, pd.Timestamp] = defaultdict(
            lambda: plan["build_start"].min())
        rows, skipped = [], defaultdict(float)

        for r in plan.sort_values("build_end").itertuples(index=False):
            e = ci.get(r.sku)
            if not e:
                skipped["no CURING routing row"] += r.qty
                continue
            cm = cycle_min(e)
            if cm is None:
                skipped["CURING row has no cycle time"] += r.qty
                continue
            presses = [p for p in (e.get("machines") or []) if str(p) in moulds]
            if not presses:
                skipped["no allowable press in the press master"] += r.qty
                continue

            ready = r.build_end                       # A-CURE-2
            qty = float(r.qty)
            # how many presses to spread this row over (A-CURE-3)
            mavg = max(1, int(round(sum(moulds[str(p)] for p in presses) / len(presses))))
            need_min = math.ceil(qty / mavg) * cm
            k = max(1, min(len(presses), a.max_presses,
                           int(math.ceil(need_min / TARGET_BLOCK_MIN))))
            chosen = sorted(presses, key=lambda p: free[str(p)])[:k]
            per = qty / k

            for p in chosen:
                p = str(p)
                n_m = max(1, moulds.get(p, 2))
                q = per
                dur = pd.Timedelta(minutes=math.ceil(q / n_m) * cm)   # A-CURE-1
                s = max(free[p], ready)
                f = s + dur
                free[p] = f
                rows.append({
                    "Machine": p, "Date": io.plant_day(s).date(),
                    "Shift": io.shift_of(s), "SKUCode": r.sku,
                    "Description": r.description, "Qty": round(q, 2),
                    "StartTime": s, "EndTime": f,
                    "CycleTime_min": round(cm, 3), "GT_Inventory": 0,
                    "GT_Code": r.gt_code, "Moulds": n_m, "CO_Mins": 0.0,
                })

        d = pd.DataFrame(rows)
        out = io.PE_DIR / line / "pe_format"
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"ctp_curing_schedule_sept2026_{line}.xlsx"
        with pd.ExcelWriter(p, engine="openpyxl") as w:
            d.to_excel(w, sheet_name="Shift Schedule", index=False)

        print(f"\n  {line.upper()}")
        print(f"    building rows   : {len(plan):,}   tyres {plan.qty.sum():,.0f}")
        print(f"    curing rows     : {len(d):,}   tyres {d.Qty.sum():,.0f}"
              if len(d) else "    curing rows     : 0")
        if len(d):
            print(f"    presses used    : {d.Machine.nunique()} of {len(moulds)}")
            print(f"    span            : {d.StartTime.min()} -> {d.EndTime.max()}")
        for k, v in skipped.items():
            print(f"    [SKIPPED] {k}: {v:,.0f} tyres")
        print(f"    -> {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
