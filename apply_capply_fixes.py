#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_capply_fixes.py -- run AFTER adapt_inputs, BEFORE run_pipeline.
   1) Cap Ply cutter line speeds (user 2026-07-30): EGSL01=450, EGSL02/03=350, CAP 66/CAP30=350 M/MIN
      -- fixes the broken 30 M/MIN edge-strip rate on m/c 601 (was the makespan bottleneck).
   2) Cap strip opening WIP += 32*1800 = 57,600 M (real CAP STRIP, not mother roll)."""
import pandas as pd, os
HERE = os.path.dirname(os.path.abspath(__file__))
INP = os.path.join(HERE, "inputs")

rt = pd.read_csv(os.path.join(INP, "routing.csv"))
rt["_rp"] = rt["routed_product"].astype(str).str.strip()
rates = {"EGSL01": 450.0, "EGSL02": 350.0, "EGSL03": 350.0,
         "CAP 66 - CAPSTRIP": 350.0, "CAP30 - CAPSTRIP": 350.0}
for code, v in rates.items():
    m = rt["_rp"] == code
    if m.any():
        old = sorted(rt.loc[m, "proc_time"].unique())
        rt.loc[m, "proc_time"] = v
        rt.loc[m, "proc_time_UOM"] = "M/MIN"
        print(f"  [capply] {code:20} proc_time {old} -> {v} M/MIN ({int(m.sum())} rows)")
rt.drop(columns=["_rp"]).to_csv(os.path.join(INP, "routing.csv"), index=False)

inv = pd.read_csv(os.path.join(INP, "inventory.csv"))
add = {"production_id": "WIP_CAPSTRIP_ADD", "produced_time": "2026-06-01 06:59:00",
       "itemcode": "CAP 66 - CAPSTRIP", "item_type": "Cap Strip", "unit": "M", "inventory": 32 * 1800}
inv = pd.concat([inv, pd.DataFrame([add])], ignore_index=True)
inv.to_csv(os.path.join(INP, "inventory.csv"), index=False)
print(f"  [capstrip WIP] added CAP 66 - CAPSTRIP = {32*1800:,} M (real cap strip)")
