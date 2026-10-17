#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download the raw PCR + TBR input tables from the DB into db_pipeline/raw/."""
import os, sys, pathlib, time
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db_connect import read_table, load_env
RAW = HERE / "raw"; RAW.mkdir(exist_ok=True)

PAIRS = [  # (pcr_table, tbr_table)
    ("jkt_bom_pcr", "jkt_bom_tbr"),
    ("jkt_routing_pcr", "jkt_routing_tbr"),
    ("jkt_itemType_master_pcr", "jkt_itemType_master_tbr"),
    ("jkt_aging_master_pcr", "jkt_aging_master_tbr"),
    ("jkt_mpq_pcr", "jkt_mpq_tbr"),
    ("jkt_buffer_master_pcr", "jkt_buffer_master_tbr"),
    ("jkt_building_cycle_times_pcr", "jkt_building_cycle_times_tbr"),
    ("Master_Building_ChangeoverTime_pcr", "Master_Building_ChangeoverTime_tbr"),
    ("Master_Building_Allowable_Machines_pcr", "Master_Building_Allowable_Machines_tbr"),
    ("Master_Curing_Allowable_Machines_pcr", "Master_Curing_Allowable_Machines_tbr"),
    ("Master_Curing_Design_CycleTime_pcr", "Master_Curing_Design_CycleTime_tbr"),
    ("MasterMachines_pcr", "MasterMachines_tbr"),
    ("BuildingSchedule_pcr", "BuildingSchedule_tbr"),
    ("CuringSchedule_pcr", "CuringSchedule_tbr"),
    ("Daily_GT_Inventory_pcr", "Daily_GT_Inventory_tbr"),
]
SHARED = ["jkt_changeover_matrix_combined", "Master_WC_Master", "Master_Mapping_Mould_SKU"]

env = load_env()
print(f"  DB {env['DB_NAME']} @ {env['DB_HOST']}")
tables = [t for pair in PAIRS for t in pair] + SHARED
ok = 0
for t in tables:
    try:
        t0 = time.time()
        df = read_table(t)
        df.to_csv(RAW / f"{t}.csv", index=False)
        print(f"  OK  {t:48s} {len(df):>8,} rows  ({time.time()-t0:.1f}s)")
        ok += 1
    except Exception as e:
        print(f"  FAIL {t:48s} {e}")
print(f"\n  DONE. {ok}/{len(tables)} tables -> {RAW}")
