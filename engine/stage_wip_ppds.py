#!/usr/bin/env python3
"""stage_wip_ppds.py - make a PPDS-only `opening_wip.csv` nettable.

    CTP_DATA=../data-ppds python stage_wip_ppds.py

Run after `refresh_ctp_sql.py --ppds-only`, before `stage_wip_v6.py` splits the
pool per line. Reads and rewrites `<CTP_DATA>/opening_wip.csv` in place.

COMPOUND IS COUNTED IN BATCHES
  Since mid-October `JK_CTP_PPDS.CTP_Opening_WIP` carries FINAL COMPOUND rows
  (it carried none in September), but in `unit = Batches` while the BOM asks
  for KG. The weight of a batch is in the routing - `batch_size` with
  `batch_UNIT = KG` - the same chain `stage_wip_merged.py` uses for planning's
  batch counters. So each Batches row becomes `inventory x batch_size` KG.
  A code the routing cannot price is DROPPED and listed, never guessed.

  The routing read is the staged one in CTP_DATA, so with --ppds-only it is
  CTP_Master_Routing: no planning table is consulted.

MASTER COMPOUND IS STILL ABSENT from the PPDS feed. Nothing here invents it;
phase 2 records the gap as a data blocker.
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

import ctpio as io
import ctpsql


def batch_weights() -> dict:
    kg = {}
    for line in ("pcr", "tbr"):
        r = io.load_routing(line)
        b = r[r["batch_UNIT"].map(io._s).str.upper() == "KG"]
        for k, v in b.groupby(b["routed_product"].map(io._s))["batch_size"].median().items():
            if k and pd.notna(v) and v > 0 and k not in kg:
                kg[k] = float(v)
    return kg


def top_up(w: pd.DataFrame, sync: str, cut: str) -> pd.DataFrame:
    """Add stock produced after the staged sync but at or before `cut`.

    A sync is a snapshot of the floor at the moment it ran. The 22 Oct 08:54
    sync holds material produced up to 06:29; anything made between then and
    the 07:00 bell first appears in a LATER sync. Those rows - and only rows
    whose production_id the staged sync does not already carry - are taken
    from `sync`, so the pool is the floor at `cut`. Their quantity is as that
    later sync records it, which can only be lower than at 07:00 (material
    is consumed, never added back), so this is a floor, not an estimate.
    """
    have_to = pd.to_datetime(w["produced_time"], errors="coerce").max()
    q = ("SELECT production_id, produced_time, item_code AS itemcode, item_type, "
         "unit, inventory, max_aging AS MaxAging, max_aging_unit AS MaxAgingUnit, "
         "min_aging AS MinAging, min_aging_unit AS MinAgingUnit, inventory_date, "
         "shift, updated_at AS updatedAt, mhe_code FROM CTP_Opening_WIP "
         f"WHERE updated_at = '{sync}' AND produced_time > '{have_to}' "
         f"AND produced_time <= '{cut}'")
    add = ctpsql.fetch(q, ctpsql.connect(ctpsql.PPDS_DB))
    add = add[~add["production_id"].isin(set(w["production_id"]))]
    print(f"  top-up from sync {sync}: {len(add):,} row(s) produced "
          f"{have_to} -> {cut}")
    return pd.concat([w, add[[c for c in w.columns if c in add.columns]]],
                     ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-up-sync", default="",
                    help="a LATER CTP_Opening_WIP sync to take post-snapshot rows from")
    ap.add_argument("--cut", default="", help="the bell, e.g. '2026-10-22 07:00:00'")
    a = ap.parse_args()
    src = io.DATA / "opening_wip.csv"
    w = pd.read_csv(src, encoding="utf-8-sig", low_memory=False)
    if a.top_up_sync and a.cut:
        w = top_up(w, a.top_up_sync, a.cut)
    w["itemcode"] = w["itemcode"].map(io._s)
    w["inventory"] = pd.to_numeric(w["inventory"], errors="coerce").fillna(0.0)
    is_b = w["unit"].map(io._s).str.lower() == "batches"

    print("=" * 76)
    print(f"  PPDS opening WIP: Batches -> KG   [{src}]")
    print("=" * 76)
    print(f"  rows {len(w):,}; in Batches {int(is_b.sum()):,} "
          f"({w.loc[is_b, 'inventory'].sum():,.0f} batches, "
          f"{w.loc[is_b, 'itemcode'].nunique()} codes)")

    kg = batch_weights()
    wt = w.loc[is_b, "itemcode"].map(kg)
    lost = w.loc[is_b & wt.reindex(w.index).isna()]
    w.loc[is_b, "inventory"] = w.loc[is_b, "inventory"] * wt
    w.loc[is_b, "unit"] = "KG"
    if len(lost):
        print(f"  [BLOCKER] {len(lost):,} row(s) of {lost['itemcode'].nunique()} code(s) "
              f"have no KG batch_size in CTP_Master_Routing - dropped:")
        for k, n in lost["itemcode"].value_counts().head(10).items():
            print(f"      {k:24s}{n:>5,d} row(s)")
        w = w.drop(lost.index)
    conv = is_b.reindex(w.index, fill_value=False)
    print(f"  converted {int(conv.sum()):,} row(s) -> {w.loc[conv, 'inventory'].sum():,.0f} KG")

    w.to_csv(src, index=False, encoding="utf-8-sig")
    (io.DATA / "_wip_source.json").write_text(json.dumps({
        "table": "JK_CTP_PPDS.CTP_Opening_WIP",
        "converted_batches_rows": int(conv.sum()),
        "dropped_unpriced_rows": int(len(lost)),
        "note": "PPDS only; Batches -> KG via CTP_Master_Routing batch_size",
    }, indent=2), encoding="utf-8")
    print(f"[OK] {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
