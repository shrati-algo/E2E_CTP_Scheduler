#!/usr/bin/env python3
"""make_floor_schedule_v10.py — the floor schedule in the plant's own format.

    python make_floor_schedule_v10.py --run e2e_v10

Writes ONE FILE PER LINE, in the exact 15 columns of
`phase5_floor_schedule_updated 8.csv`:

    date, machine, shift, item, item_type, process, department,
    start_time, end_time, produce_qty, UOM, lot_id,
    prod_id, consumption_id, FG_SKU_CODE

THE THREE COLUMNS THAT ARE NOT JUST A RENAME
  prod_id         a production id, one per row, assigned in time order so the
                  file reads as the floor actually ran it.
  consumption_id  the lot_id(s) this lot CONSUMED, semicolon-separated, taken
                  from the phase-4 precedence edges. `RAW_MATERIAL` where the
                  lot has no scheduled predecessor - a compound mixed from
                  bought-in chemicals, or an item covered by opening stock.
                  This is the material genealogy: every row can be walked back
                  to what fed it.
  FG_SKU_CODE     the finished tyre this work is for, where the chain names one.

Every row is read back from the run's own phase outputs; nothing is recomputed.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io

COLS = ["date", "machine", "shift", "item", "item_type", "process", "department",
        "start_time", "end_time", "produce_qty", "UOM", "lot_id",
        "prod_id", "consumption_id", "FG_SKU_CODE",
        # 16th column, added on plant request: for a BUILDING row, the kit
        # components that were NOT ready together. "COMPLETE" where the whole
        # kit landed on time; the blocking item and its wait where it did not.
        "incomplete_components"]


def _rd(p, **kw):
    return pd.read_csv(p, **kw) if p.exists() else pd.DataFrame()


def build_line(run: str, line: str) -> pd.DataFrame:
    D = io.OUT_ROOT / run / line
    up = _rd(D / "phase5_schedule.csv", parse_dates=["start", "finish"])
    bl = _rd(D / "phase5_building_plan.csv",
             parse_dates=["feasible_start", "feasible_end"])
    cu = _rd(D / "phase7_curing_plan.csv",
             parse_dates=["feasible_start", "feasible_end"])
    ed = _rd(D / "phase4_edges.csv")

    # ---- material genealogy: consumer lot -> the lots that fed it ---------
    # THREE links, not one. The upstream edges alone leave every building and
    # curing row as RAW_MATERIAL, which is false: a trolley consumes its kit and
    # a press consumes a trolley. Both are recorded in the run's own outputs.
    cons = defaultdict(list)
    if len(ed):
        for p_, c_ in zip(ed["producer_lot"].astype(str),
                          ed["consumer_lot"].astype(str)):
            cons[c_].append(p_)

    la = _rd(D / "phase3_lot_demand.csv")
    # link 2: building lot <- the upstream lots phase 3 allocated to its kit
    if len(la) and {"lot_id", "building_lot_id"} <= set(la.columns):
        for l_, b_ in zip(la["lot_id"].astype(str),
                          la["building_lot_id"].astype(str)):
            cons[b_].append(l_)

    # link 3: curing row <- the building lots of the same SKU already finished.
    # Keyed by a synthetic id because phase 7 carries no lot column of its own.
    cure_cons: dict[int, list[str]] = {}
    if len(cu) and len(bl):
        bysku = defaultdict(list)
        for b_, s_, e_ in zip(bl["building_lot_id"].astype(str),
                              bl["sku"].astype(str), bl["feasible_end"]):
            bysku[s_].append((e_, b_))
        for i, (s_, st) in enumerate(zip(cu["sku"].astype(str),
                                         cu["feasible_start"])):
            cure_cons[i] = [b for e, b in sorted(bysku.get(s_, []))
                            if pd.notna(e) and pd.notna(st) and e <= st]

    # ---- ASSUMED INPUTS: purchased codes taken as infinitely available ----
    # A BOM child with no routing is not made here, so the BOM walk contracts it
    # out and it never becomes a lot, never gets a start time and never gates a
    # kit. That is an ASSUMPTION - the plant must actually have the part - and an
    # unlabelled `RAW_MATERIAL` hides it. Name it in the schedule instead.
    bom = io.load_bom(line)
    made = io.schedulable_items(io.load_routing(line))
    # the BOM is path-explicit, so the same child recurs under one parent;
    # the register wants the distinct codes, in a stable order
    _seen: dict[str, set] = defaultdict(set)
    pur_of: dict[str, list[str]] = defaultdict(list)
    for par, ch in zip(bom["Parent"].astype(str), bom["child"].astype(str)):
        if ch and ch not in made and ch not in _seen[par]:
            _seen[par].add(ch)
            pur_of[par].append(ch)
    purchased_all = sorted({c for v in pur_of.values() for c in v})

    # ---- which finished tyre does an upstream lot serve? -----------------
    # phase 3's own allocation names the building lot each upstream lot feeds;
    # the building plan names that lot's SKU. Anything reached by more than one
    # SKU is left blank rather than guessed at.
    fg = {}
    if len(la) and len(bl):
        sku_of_b = dict(zip(bl["building_lot_id"].astype(str), bl["sku"].astype(str)))
        acc = defaultdict(set)
        if {"lot_id", "building_lot_id"} <= set(la.columns):
            for l_, b_ in zip(la["lot_id"].astype(str),
                              la["building_lot_id"].astype(str)):
                s = sku_of_b.get(b_)
                if s:
                    acc[l_].add(s)
        fg = {k: (next(iter(v)) if len(v) == 1 else "") for k, v in acc.items()}

    rows = []
    if len(up):
        rows.append(pd.DataFrame({
            "machine": up["machine"].astype(str),
            "item": up["item_code"], "item_type": up.get("item_type", ""),
            "process": up.get("operation", ""), "department": up.get("department", ""),
            "start_time": up["start"], "end_time": up["finish"],
            "produce_qty": up["lot_qty"], "UOM": up.get("uom", ""),
            "lot_id": up["lot_id"].astype(str),
        }))
    if len(bl):
        # kit completeness, stated per trolley rather than left to a side file
        def _kit(r):
            if bool(getattr(r, "kit_incomplete", False)):
                who = str(getattr(r, "blocking_item", "") or "?")
                return f"INCOMPLETE: {who}"
            sh = float(getattr(r, "shift_h", 0.0) or 0.0)
            if sh > 1e-6:
                who = str(getattr(r, "blocking_item", "") or "?")
                return f"LATE {sh:.1f}h waiting on {who}"
            return "COMPLETE"
        kit = [_kit(r) for r in bl.itertuples(index=False)]
        rows.append(pd.DataFrame({
            "incomplete_components": kit,
            "machine": bl["machine"].astype(str),
            "item": bl.get("built_item", ""), "item_type": bl.get("item_type", "Green Tyres"),
            "process": bl.get("operation", "GT BUILDING"), "department": "BUILDING",
            "start_time": bl["feasible_start"], "end_time": bl["feasible_end"],
            "produce_qty": bl["qty"], "UOM": "NOS",
            "lot_id": bl["building_lot_id"].astype(str),
            "FG_SKU_CODE": bl["sku"].astype(str),
        }))
    if len(cu):
        rows.append(pd.DataFrame({
            "machine": cu["press"].astype(str),
            "item": cu.get("gt_code", ""), "item_type": "Cured Tyre",
            "process": "CURING", "department": "CURING",
            "start_time": cu["feasible_start"], "end_time": cu["feasible_end"],
            "produce_qty": cu["qty"], "UOM": "NOS",
            "lot_id": "", "FG_SKU_CODE": cu["sku"].astype(str),
            "_cure_ix": range(len(cu)),
        }))
    if not rows:
        return pd.DataFrame(columns=COLS + SPLIT_COLS), []

    d = pd.concat(rows, ignore_index=True)
    d = d[d["start_time"].notna()].sort_values(
        ["start_time", "machine"], kind="stable").reset_index(drop=True)

    # the plant reads this to the second; float hours leave sub-second noise
    for c in ("start_time", "end_time"):
        d[c] = pd.to_datetime(d[c]).dt.round("1s")
    d["date"] = d["start_time"].map(lambda t: io.plant_day(t).date())
    d["shift"] = d["start_time"].map(io.shift_of)
    d["prod_id"] = [f"P{i:06d}" for i in range(1, len(d) + 1)]
    if "_cure_ix" not in d.columns:
        d["_cure_ix"] = pd.NA
    def _tag(pc: list[str]) -> str:
        return ("PURCHASED_ASSUMED:" + ";".join(pc[:6])
                + (f"(+{len(pc)-6})" if len(pc) > 6 else ""))

    out, assumed = [], []
    for l, ci, it in zip(d["lot_id"], d["_cure_ix"], d["item"].astype(str)):
        src = cure_cons.get(int(ci), []) if pd.notna(ci) else cons.get(str(l), [])
        # An operation can consume BOTH scheduled lots and bought-in material.
        # Reporting only the lots hid the assumption on 165 PCR and 278 TBR rows
        # - including TBR APEXED BEAD ASSEMBLY, the line's biggest blocker. The
        # purchased tag is therefore APPENDED, never replaced by the genealogy.
        pc = pur_of.get(it, [])
        assumed.append(";".join(pc) if pc else "")
        if src and pc:
            out.append(";".join(src) + " + " + _tag(pc))
        elif src:
            out.append(";".join(src))
        elif pc:
            out.append(_tag(pc))
        else:
            out.append("RAW_MATERIAL")
    d["consumption_id"] = out
    d["_assumed_purchased"] = assumed
    if "FG_SKU_CODE" not in d.columns:
        d["FG_SKU_CODE"] = ""
    if "incomplete_components" not in d.columns:
        d["incomplete_components"] = ""
    # upstream and curing rows are not kit-gated; say so rather than leave blank
    d["incomplete_components"] = d["incomplete_components"].fillna("")
    d.loc[(d["department"] != "BUILDING") & (d["incomplete_components"] == ""),
          "incomplete_components"] = "n/a (not kit-gated)"
    d["FG_SKU_CODE"] = [f if isinstance(f, str) and f else fg.get(str(l), "")
                        for f, l in zip(d["FG_SKU_CODE"].fillna(""), d["lot_id"])]
    d["lot_qty"] = d["produce_qty"]
    d["shift_part"] = "1/1"
    if SPLIT_AT_SHIFT:
        d = split_at_shifts(d)
    return d[COLS + SPLIT_COLS + ["_assumed_purchased"]], purchased_all


# v46k report fix: A LOT IS BOOKED TO EVERY SHIFT IT RUNS IN, NOT ONLY ITS FIRST.
# Until now each row carried the lot's whole quantity in the shift it STARTED,
# so a lot running 20:35-01:14 put all 10,948 m into shift B. 2,099 of 3,450
# v46k rows cross a shift change, and the shift totals swung 8k / 19k / 9k on
# calender 901 when the floor really made 8k / 14k / 14k. A crossing row is now
# cut at 07:00 / 15:00 / 23:00 and its quantity prorated by time (the cut used
# by phase6_floor_schedule.py). NOS rows use largest-remainder rounding so the
# pieces sum back to the lot; curing rows stay in whole PAIRS (check 6).
# `lot_qty` keeps the lot's full quantity, `shift_part` says "2/3" etc.
SPLIT_COLS = ["lot_qty", "shift_part"]
SPLIT_AT_SHIFT = True


def split_at_shifts(d: pd.DataFrame) -> pd.DataFrame:
    from phase6_floor_schedule import shift_rows, NOS_UOMS
    out = []
    for r in d.to_dict("records"):
        st, en, q = r["start_time"], r["end_time"], float(r["produce_qty"] or 0.0)
        if pd.isna(st) or pd.isna(en) or en <= st:
            out.append(r)
            continue
        uom = str(r.get("UOM", "") or "").upper()
        if r.get("department") == "CURING" and abs(q / 2 - round(q / 2)) < 1e-9:
            parts = shift_rows(st, en, q / 2, uom, True)
            for p in parts:
                p["produce_qty"] *= 2
        else:
            parts = shift_rows(st, en, q, uom, uom in NOS_UOMS)
        for i, p in enumerate(parts, 1):
            x = dict(r)
            x.update(start_time=p["start_time"], end_time=p["end_time"],
                     produce_qty=p["produce_qty"], shift_part=f"{i}/{len(parts)}")
            out.append(x)
    s = pd.DataFrame(out)
    s = s.sort_values(["start_time", "machine"], kind="stable").reset_index(drop=True)
    s["date"] = s["start_time"].map(lambda t: io.plant_day(t).date())
    s["shift"] = s["start_time"].map(io.shift_of)
    s["prod_id"] = [f"P{i:06d}" for i in range(1, len(s) + 1)]
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="e2e_v10")
    ap.add_argument("--no-shift-split", action="store_true",
                    help="old layout: a lot's whole quantity in its start shift")
    a = ap.parse_args()
    global SPLIT_AT_SHIFT
    SPLIT_AT_SHIFT = not a.no_shift_split
    OUT = io.OUT_ROOT / a.run

    print("\n" + "=" * 78)
    print(f"  FLOOR SCHEDULE — plant format, one file per line   [{a.run}]")
    print("=" * 78)
    reg = []
    for line in ("pcr", "tbr"):
        d, purchased_all = build_line(a.run, line)
        ass = d.pop("_assumed_purchased") if "_assumed_purchased" in d.columns             else pd.Series([""] * len(d))
        f = OUT / f"phase5_floor_schedule_{line}.csv"
        d = io.with_machine_name(d, "machine")          # v36: plant names
        d.to_csv(f, index=False, encoding="utf-8-sig")
        if not len(d):
            print(f"  {line.upper()}: nothing to write")
            continue
        npur = int(d.consumption_id.str.contains("PURCHASED_ASSUMED").sum())
        print(f"\n  {line.upper()}: {len(d):,} rows -> {f.name}")
        print(f"    span      : {d.start_time.min()}  ->  {d.end_time.max()}")
        print(f"    machines  : {d.machine.nunique()}   departments: {d.department.nunique()}")
        nlot = int((~d.consumption_id.isin(['RAW_MATERIAL'])
                    & ~d.consumption_id.str.startswith('PURCHASED_ASSUMED')).sum())
        print(f"    genealogy : {nlot:,} linked to a producer lot")
        print(f"    ASSUMED   : {npur:,} rows fed by PURCHASED_ASSUMED material; "
              f"{int((d.consumption_id=='RAW_MATERIAL').sum()):,} true RAW_MATERIAL")
        print(f"    FG traced : {int((d.FG_SKU_CODE.fillna('')!='').sum()):,} rows")
        # the register: every purchased code, named, with the assumption stated
        used = sorted({c for v in ass.fillna("") for c in str(v).split(";") if c})
        for c in purchased_all:
            reg.append({"line": line.upper(), "purchased_code": c,
                        "consumed_in_schedule": "YES" if c in used else "no",
                        "assumption": "ASSUMED INFINITELY AVAILABLE - not made "
                                      "here, no routing, never scheduled, never "
                                      "gates a kit",
                        "risk_if_false": "every lot consuming it is unstartable; "
                                         "fulfilment shown is an upper bound"})
    if reg:
        rf = OUT / "ASSUMED_PURCHASED_ITEMS.csv"
        pd.DataFrame(reg).to_csv(rf, index=False, encoding="utf-8-sig")
        n = sum(1 for r in reg if r["consumed_in_schedule"] == "YES")
        print(f"\n[OK] {rf.name} — {len(reg):,} purchased codes assumed available, "
              f"{n:,} of them actually consumed by this schedule")
    return 0


if __name__ == "__main__":
    sys.exit(main())
