# Step 3: masters and snapshots staged from JK_CTP_PPDS

**Code:** `engine/refresh_ctp_sql.py --ppds-only --bom-v2` and `engine/stage_wip_ppds.py`, called by `stage_masters()` in `run_pipeline.py`.

**Writes:** `data/ppds_<day>/`.

| Data | Table | File |
|---|---|---|
| BOM | `CTP_Master_BOM_v2` | `jkt_bom_{pcr,tbr}.csv` |
| Pre-v2 BOM (cut-stock factors only) | `CTP_Master_BOM` | `reference_bom/` |
| Routing | `CTP_Master_Routing` | `jkt_routing_{pcr,tbr}.csv` |
| Dip lengths (4-roll calender) | `CTP_Master_FRC` | `CTP_Master_FRC.csv` |
| Ageing | `CTP_Master_Aging` | `jkt_aging_master_{pcr,tbr}.csv` |
| Buffer, MPQ, changeover, machine master, allowable builders, holidays, PM downtime | the matching `CTP_Master_*` tables | see `_sql_manifest.json` |
| Opening stock | `CTP_Opening_WIP` at the step-2 sync | `opening_wip.csv` |
| Green tyres | `CTP_GT_Inventory` at the step-2 syncs | `gt_inventory_manual_{pcr,tbr}.csv` |
| Plant machine register, purchased items | not in the database: copied from `reference/masters/` | |

`stage_wip_ppds.py` turns compound stock held in **batches** into **kg**.

**Reuse.** A second run on the same day reuses `data/ppds_<day>/` when it was staged with **the same snapshots**. Otherwise it stages again. `--restage` forces a fresh read.

**Stamp:** `data/ppds_<day>/_pipeline_stage.json`, copied to `outputs/<run>/DATA_STAGED.json`. It records:
- the plan day;
- the WIP and GT syncs, and how each was chosen;
- the time of staging;
- every table read.

`CTP_Live_Inventory` and `CTP_Live_Inventory_Raw` are skipped: 5.7 M rows that no phase reads.
