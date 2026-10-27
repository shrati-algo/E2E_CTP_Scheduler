#!/usr/bin/env python3
"""stage_wip_mhe.py - add `mhe_code` to opening stock that is already staged.

    CTP_DATA=../data-ppds-v38-bomv2-cap66-mhe python stage_wip_mhe.py

The MHE (stand / trolley) code says WHAT FORM a stock row is in when the item
code alone does not. CAP 66 is the case that needs it: every CAP 66 row in
CTP_Opening_WIP carries item_code `CAP 66`, but

    4RC/FAB/nnn   mother roll off the 4-roll calender (CAP 66 itself)
    4RC/STL/nnn   slitted material / pre-cap strip   (CAP 66-MOTHERROLL)
    (empty)       10 mm cap strip off the cutter     (CAP 66 - CAPSTRIP)

Phase 2 turns each form back into mother roll (`wip.mhe_conversion`).

Older stagings did not select `mhe_code`. This looks it up for each staged row
by (production_id, updatedAt) - the exact sync row that was staged - so no row,
quantity or date changes; only the column is added. READ-ONLY on the database.
"""
from __future__ import annotations

import pandas as pd

import ctpio as io
import ctpsql


def main() -> int:
    files = [io.DATA / f for f in ("opening_wip.csv", "opening_wip_pcr.csv",
                                   "opening_wip_tbr.csv")]
    files = [f for f in files if f.exists()]
    w0 = pd.read_csv(files[0], dtype=str)
    syncs = sorted(w0["updatedAt"].dropna().unique())
    q = ("SELECT production_id, updated_at, mhe_code FROM CTP_Opening_WIP "
         "WHERE updated_at IN (" + ", ".join(f"'{s}'" for s in syncs) + ")")
    m = ctpsql.fetch(q, ctpsql.connect(ctpsql.PPDS_DB))
    m["updatedAt"] = pd.to_datetime(m.pop("updated_at")).dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]
    m["production_id"] = m["production_id"].astype(str)
    m["mhe_code"] = m["mhe_code"].fillna("").astype(str).str.strip()
    m = m.drop_duplicates(["production_id", "updatedAt"])
    for f in files:
        w = pd.read_csv(f, dtype=str)
        if "mhe_code" in w.columns:
            w = w.drop(columns="mhe_code")
        n = len(w)
        w = w.merge(m, on=["production_id", "updatedAt"], how="left")
        assert len(w) == n, f"{f.name}: row count changed"
        hit = int(w["mhe_code"].notna().sum())
        w["mhe_code"] = w["mhe_code"].fillna("")
        w.to_csv(f, index=False, encoding="utf-8-sig")
        cap = w[w["itemcode"].astype(str).str.strip() == "CAP 66"]
        print(f"  {f.name:<22} {n:>6,} rows, {hit:,} matched a DB row; "
              f"CAP 66 MHE kinds: "
              f"{cap['mhe_code'].str.extract(r'^[^/]*/([^/]*)/')[0].fillna('(none)').value_counts().to_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
