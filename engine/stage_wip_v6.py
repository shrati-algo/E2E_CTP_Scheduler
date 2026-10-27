#!/usr/bin/env python3
"""stage_wip_v6.py — jkplanning_CTP.opening_wip, made safe to net against.

    python stage_wip_v6.py --split-from e2e_v6_pass1

THE TABLE IS ONE POOL, THE ENGINE NETS PER LINE
  `opening_wip` is a single table covering both lines. Phase 2 nets per line,
  so handing the same rows to PCR and TBR would spend the same physical stock
  twice. Every item is therefore split between the lines IN PROPORTION TO THE
  DEMAND EACH LINE HAS FOR IT, taken from a prior run's phase-1 demand. An
  item only one line wants goes wholly to that line.

WHAT IS DROPPED, AND WHY IT IS SAID OUT LOUD
  * codes in NEITHER BOM — nothing to net them against
  * green tyres — `GREEN_TYRE` is building OUTPUT, not an upstream component.
    Netting it upstream would be wrong; it is written out separately so the
    building stage can use it.

UNITS
  The `unit` column is NOT trusted. Measured against the BOM: 232 items say
  `NUM` where the BOM says `KG`, 275 are blank, and only 27 of 563 agree. The
  QUANTITIES are already in the BOM's units (NUM rows carry mixer batch
  weights, median 230 KG), so the column is dropped and the BOM's unit stands.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import pandas as pd

import ctpio as io


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split-from", default="e2e_v6_pass1",
                    help="run whose phase1_demand.csv sets the PCR/TBR split")
    ap.add_argument("--src", default=None)
    ap.add_argument("--cut-to-cutting-line", action="store_true",
                    help="v46k: stock of a cut / slit code no line demands goes wholly "
                         "to the line whose routing cuts it (bypassed department), "
                         "not 50/50 - only that line can turn it back into its roll")
    ap.add_argument("--also-bom", default="",
                    help="v46i: a second staged folder whose jkt_bom_{line}.csv also "
                         "counts as 'known' - the pre-v2 BOM, so cut / slit codes "
                         "that BOM v2 leaves out still reach their line and are "
                         "turned into mother roll in phase 2 (wip.bypass_conversion_bom)")
    a = ap.parse_args()

    src = pathlib.Path(a.src) if a.src else (io.DATA / "opening_wip.csv")
    w = pd.read_csv(src, encoding="utf-8-sig", low_memory=False)
    w["itemcode"] = w["itemcode"].map(io._s)
    w["inventory"] = pd.to_numeric(w["inventory"], errors="coerce").fillna(0.0)
    w["item_type"] = w["item_type"].map(io._s)
    n0, q0 = len(w), w.inventory.sum()

    print("\n" + "=" * 76)
    print(f"  opening_wip -> per-line WIP   [{src.name}]")
    print("=" * 76)
    print(f"  source rows {n0:,}   qty {q0:,.0f}   items {w.itemcode.nunique():,}")

    # ---- green tyres are building output, not upstream stock --------------
    gt = w[w.item_type.str.upper().str.replace(" ", "_") == "GREEN_TYRE"]
    w = w.drop(gt.index)
    if len(gt):
        gt.to_csv(io.DATA / "opening_gt.csv", index=False, encoding="utf-8-sig")
        print(f"  green tyres held out : {len(gt):,} rows, {gt.inventory.sum():,.0f} "
              f"tyres -> opening_gt.csv (building stage, not upstream)")

    # ---- what each line's BOM knows, and how much each line wants ---------
    dem, known = {}, {}
    for line in ("pcr", "tbr"):
        bom = io.load_bom(line)
        known[line] = set(bom["child"]) | set(bom["Parent"])
        if a.also_bom:
            _b2 = pd.read_csv(io.resolve_ref_dir(a.also_bom) / f"jkt_bom_{line}.csv", dtype=str)
            known[line] |= set(_b2["child"].map(io._s)) | set(_b2["Parent"].map(io._s))
        f = io.OUT_ROOT / a.split_from / line / "phase1_demand.csv"
        dem[line] = (pd.read_csv(f, usecols=["item_code", "demand_qty"])
                     .groupby("item_code").demand_qty.sum().to_dict()
                     if f.exists() else {})
        print(f"  {line.upper()} BOM knows {len(known[line]):,} codes; "
              f"{a.split_from} demands {len(dem[line]):,}")

    cut_of, moved = {"pcr": set(), "tbr": set()}, []
    if a.cut_to_cutting_line:
        for line in ("pcr", "tbr"):
            io.BYPASSED.clear()
            io.schedulable_items(io.load_routing(line))
            cut_of[line] = set(io.BYPASSED)
        io.BYPASSED.clear()
    orphan, rows = [], {"pcr": [], "tbr": []}
    for it, g in w.groupby("itemcode"):
        dp, dt = float(dem["pcr"].get(it, 0.0)), float(dem["tbr"].get(it, 0.0))
        kp, kt = it in known["pcr"], it in known["tbr"]
        if not (kp or kt):
            orphan.append({"itemcode": it, "qty": float(g.inventory.sum()),
                           "item_type": g.item_type.iloc[0],
                           "issue": "in neither BOM"})
            continue
        if dp + dt > 0:                       # split by what each line wants
            share = {"pcr": dp / (dp + dt), "tbr": dt / (dp + dt)}
        elif kp and kt and a.cut_to_cutting_line and (it in cut_of["pcr"]) != (it in cut_of["tbr"]):
            # v46k: a cut code only one line's routing cuts goes to that line
            share = {"pcr": 1.0 if it in cut_of["pcr"] else 0.0,
                     "tbr": 1.0 if it in cut_of["tbr"] else 0.0}
            moved.append((it, "PCR" if share["pcr"] else "TBR", float(g.inventory.sum())))
        elif kp and kt:                       # known to both, wanted by neither
            share = {"pcr": 0.5, "tbr": 0.5}
        else:
            share = {"pcr": 1.0 if kp else 0.0, "tbr": 1.0 if kt else 0.0}
        for line in ("pcr", "tbr"):
            if share[line] <= 0:
                continue
            h = g.copy()
            h["inventory"] = h["inventory"] * share[line]
            rows[line].append(h)

    for it, ln_, q in moved:
        print(f"  cut code to its cutting line: {it:<14s} -> {ln_} only ({q:,.1f})")
    man = {"source": str(src), "source_rows": int(n0), "source_qty": float(q0),
           "cut_to_cutting_line": [{"item": i, "line": l, "qty": q} for i, l, q in moved],
           "green_tyres_held_out": int(len(gt)),
           "orphan_items": len(orphan),
           "orphan_qty": float(sum(o["qty"] for o in orphan)),
           "split_from": a.split_from, "lines": {}}

    for line in ("pcr", "tbr"):
        d = (pd.concat(rows[line], ignore_index=True) if rows[line]
             else pd.DataFrame(columns=w.columns))
        d = d[d.inventory > 0]
        p = io.DATA / f"opening_wip_{line}.csv"
        d.to_csv(p, index=False, encoding="utf-8-sig")
        used = sum(1 for it in d.itemcode.unique() if it in dem[line])
        man["lines"][line] = {"rows": int(len(d)), "qty": float(d.inventory.sum()),
                              "items": int(d.itemcode.nunique()),
                              "items_demanded_by_this_run": int(used)}
        print(f"  {line.upper():4s} -> {len(d):>6,} rows  {d.inventory.sum():>12,.0f} "
              f"qty  {d.itemcode.nunique():>4} items ({used} demanded)")

    if orphan:
        o = pd.DataFrame(orphan).sort_values("qty", ascending=False)
        o.to_csv(io.DATA / "opening_wip_orphans.csv", index=False, encoding="utf-8-sig")
        print(f"\n  ORPHANS — in neither BOM, cannot be netted ({len(o)} items, "
              f"{o.qty.sum():,.0f}):")
        for r in o.head(10).itertuples(index=False):
            print(f"    {r.itemcode:24s}{r.qty:>12,.0f}  {r.item_type}")
        if len(o) > 10:
            print(f"    ... and {len(o)-10} more -> opening_wip_orphans.csv")

    (io.DATA / "_wip_manifest.json").write_text(json.dumps(man, indent=2),
                                                encoding="utf-8")
    print(f"\n[OK] {io.DATA/'_wip_manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
