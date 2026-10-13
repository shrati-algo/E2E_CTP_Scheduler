#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mpq_checks.py -- MPQ compliance of a finished run against JK_CTP_PPDS.CTP_Master_MPQ (2026-10-06).

    python mpq_checks.py <run_output_dir> [--xlsx out.xlsx]

  every upstream lot >= its item's MPQ (inputs/mpq_item.csv, built by build_mpq_items.py from the DB master);
  compounds >= 3 batches x the item's routing batch weight;
  over-production = made - demand (phase 2 raw_demand) by item and in machine hours.
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))


def main(run, out=None):
    L = pd.read_csv(os.path.join(run, "phase2_lots_updated.csv"), low_memory=False)
    S = pd.read_csv(os.path.join(run, "phase5_schedule_updated.csv"),
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
    S["min"] = (S["scheduled_finish"] - S["scheduled_start"]).dt.total_seconds() / 60
    L = L.merge(S[["lot_id", "machine", "min"]], on="lot_id", how="inner")
    L = L[~L["lot_id"].astype(str).str.startswith("BLD") & ~L["item_type"].astype(str).str.upper().eq("GREEN TYRES")]
    L["item_code"] = L["item_code"].astype(str).str.strip()
    L["qty"] = pd.to_numeric(L["lot_qty"], errors="coerce")
    L["raw"] = pd.to_numeric(L["raw_demand"], errors="coerce")
    mi = pd.read_csv(os.path.join(HERE, "inputs", "mpq_item.csv")).set_index("item")
    rt = pd.read_csv(os.path.join(HERE, "db_pipeline", "raw", "JK_CTP_PPDS__CTP_Master_Routing.csv"), dtype=str)
    bs = pd.to_numeric(rt["batch_size"], errors="coerce").groupby(rt["routed_product"].str.strip()).max()
    comp = L["item_type"].astype(str).str.upper().str.contains("COMPOUND")
    L["mpq"] = np.where(comp, 3 * L["item_code"].map(bs), L["item_code"].map(mi["mpq_min"]))
    L["mpq_row"] = np.where(comp, "3 batches x batch weight", L["item_code"].map(mi["master_row"]))
    L["mpq_uom"] = np.where(comp, "KG", L["item_code"].map(mi["uom"]))
    L["unit_ok"] = comp | L["mpq_uom"].isna() | (L["mpq_uom"].astype(str).str.upper()
                                                 .replace({"NO": "NOS", "PCS": "NOS"})
                                                 == L["lot_uom"].astype(str).str.upper().replace({"NO": "NOS", "PCS": "NOS"}))
    L["note"] = L["item_code"].map(mi["note"]).fillna("")
    chk = L[L["mpq"].notna() & L["unit_ok"]]
    chk = chk.assign(below=chk["qty"] < chk["mpq"] - 1e-6)
    L["extra"] = (L["qty"] - L["raw"]).clip(lower=0)
    L["extra_h"] = L["min"] * L["extra"] / L["qty"].replace(0, np.nan) / 60
    T = chk.groupby(["item_type", "mpq_row"], dropna=False).agg(
        lots=("qty", "size"), mpq=("mpq", "first"), below_mpq=("below", "sum"), smallest_lot=("qty", "min"),
        median_lot=("qty", "median")).reset_index()
    ov = L.groupby(np.where(comp, "COMPOUNDS (mixers)", "OTHER UPSTREAM")).agg(
        lots=("qty", "size"), made=("qty", "sum"), demand=("raw", "sum"), extra=("extra", "sum"),
        extra_machine_h=("extra_h", "sum")).reset_index().rename(columns={"index": "group"})
    ov["extra_%"] = (100 * ov["extra"] / ov["made"]).round(1)
    R = pd.DataFrame([
        {"check": "upstream lots with an MPQ (DB master)", "value": len(chk)},
        {"check": "lots below MPQ (must be 0)", "value": int(chk["below"].sum())},
        {"check": "lots with no MPQ row in CTP_Master_MPQ", "value": int(L["mpq"].isna().sum())},
        {"check": "lots skipped: MPQ unit differs from the item unit", "value": int((~L["unit_ok"]).sum())},
        {"check": "items on the smallest gauge row (gauge not in the DB)",
         "value": int(L.loc[L["note"].str.contains("GAUGE"), "item_code"].nunique())}])
    pd.set_option("display.width", 220)
    print(R.to_string(index=False))
    print(T.round(1).to_string(index=False))
    print(ov.round(1).to_string(index=False))
    if out:
        with pd.ExcelWriter(out) as xw:
            R.to_excel(xw, sheet_name="Summary", index=False)
            T.to_excel(xw, sheet_name="By item type", index=False)
            ov.to_excel(xw, sheet_name="Over-production", index=False)
            chk[chk["below"]][["lot_id", "item_code", "item_type", "machine", "qty", "lot_uom", "mpq", "mpq_row",
                               "note"]].to_excel(xw, sheet_name="Lots below MPQ", index=False)
            L.groupby(["item_code", "item_type"]).agg(lots=("qty", "size"), made=("qty", "sum"), demand=("raw", "sum"),
                                                      extra=("extra", "sum"), extra_machine_h=("extra_h", "sum"),
                                                      mpq=("mpq", "first"), mpq_row=("mpq_row", "first")).reset_index() \
                .sort_values("extra_machine_h", ascending=False).to_excel(xw, sheet_name="By item", index=False)
        print("wrote", out)
    return R


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], a[a.index("--xlsx") + 1] if "--xlsx" in a else None)
