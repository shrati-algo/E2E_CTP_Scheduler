#!/usr/bin/env python
"""
Rebuild the COMBINED PCR+TBR master inputs from the LIVE DB (jkplanning_CTP).
2026-08-06. Pulls the updated jkt_*_pcr + jkt_*_tbr master tables, concatenates each
pair (identical schemas), and writes ctp_inputfiles/{bom,routing,aging}_comb.csv.
The building/curing PLANS (building_comb / curing_comb = the June btoc shift-wise
sheets) are NOT touched -- only the masters are refreshed from the DB.
Backs up any existing _comb file to *.bak_db before overwriting.
"""
import os, shutil
import pandas as pd
import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "ctp_inputfiles")

env = {}
for line in open(os.path.join(HERE, "db_pipeline", ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()

conn = pymysql.connect(host=env["DB_HOST"], port=int(env["DB_PORT"]),
                       user=env["DB_USER"], password=env["DB_PASSWORD"],
                       database=env["DB_NAME"], connect_timeout=15)

PAIRS = {
    "bom_comb.csv":     ("jkt_bom_pcr",          "jkt_bom_tbr"),
    "routing_comb.csv": ("jkt_routing_pcr",      "jkt_routing_tbr"),
    "aging_comb.csv":   ("jkt_aging_master_pcr", "jkt_aging_master_tbr"),
}

for out_name, (pcr_t, tbr_t) in PAIRS.items():
    p = pd.read_sql(f"SELECT * FROM {pcr_t}", conn)
    t = pd.read_sql(f"SELECT * FROM {tbr_t}", conn)
    # align columns (schemas are identical, but be defensive about order/extras)
    cols = list(p.columns)
    t = t.reindex(columns=cols)
    comb = pd.concat([p, t], ignore_index=True)
    dst = os.path.join(OUT, out_name)
    if os.path.exists(dst):
        shutil.copy2(dst, dst + ".bak_db")
    comb.to_csv(dst, index=False)
    print(f"  {out_name}: PCR {len(p):,} + TBR {len(t):,} = {len(comb):,} rows  -> {dst}")

conn.close()
print("  DONE. Combined masters refreshed from LIVE DB (plans untouched).")
