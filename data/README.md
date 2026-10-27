# data/: staged master data (not in git)

`run_pipeline.py --plan-folder <folder>` fills this folder by itself. It creates `data/ppds_<plan day>/`, read-only, from `JK_CTP_PPDS`.

**What it stages:**

| File(s) | Source |
|---|---|
| `jkt_bom_{pcr,tbr}.csv` | `CTP_Master_BOM_v2` (verbatim) |
| `reference_bom/jkt_bom_{pcr,tbr}.csv` | `CTP_Master_BOM`. Pre-v2, used only for the types of bypassed cut codes and the roll-per-metre factor of cut stock |
| `jkt_routing_{pcr,tbr}.csv` | `CTP_Master_Routing` |
| `CTP_Master_FRC.csv` | `CTP_Master_FRC` (4-roll calender dip lengths) |
| `opening_wip*.csv` | `CTP_Opening_WIP`, the **shift-A (06:30) sync of the plan day**, with `mhe_code`. Then: compound Batches → KG; PCR / TBR split by the plan's own demand |
| `gt_inventory_manual_{pcr,tbr}.csv` | `CTP_GT_Inventory`, the **shift-A (06:30) sync of the plan day** |
| ageing, buffer, MPQ, changeover matrix, machine master, allowable builders, holidays, downtime | their `CTP_Master_*` tables |
| `PLANT_MACHINE_REGISTER.csv`, `CTP_Purchased_Items.csv` | copied from `reference/masters/` (plant lists, not in the database) |
| `_sql_manifest.json`, `_pipeline_stage.json` | what was staged, from which table and sync, and when |

- **Reuse:** the next run for the same plan day reuses the folder. `--restage` reads the database again.
- **Your own data folder:** `--data <folder>` uses a folder you staged yourself, and the database is not needed.
- **Why it isn't committed:** a snapshot is about 60 MB and plant-confidential, so it is never committed.

## Staging by hand (what run_pipeline.py does)

```bash
# credentials ONLY from environment variables or db.env (see db.env.example)
cd engine
CTP_WIP_DATE=2026-10-27 CTP_WIP_ASOF="2026-10-27 06:31:17.841" \
CTP_GT_DATE=2026-10-27 CTP_GT_ASOF="2026-10-27 06:30:00.000" \
  python refresh_ctp_sql.py --ppds-only --bom-v2 --out ../data/ppds_2026-10-27 \
         --skip CTP_Live_Inventory,CTP_Live_Inventory_Raw
cp ../reference/masters/*.csv ../data/ppds_2026-10-27/
CTP_DATA=../data/ppds_2026-10-27 python stage_wip_ppds.py
# the PCR / TBR split needs this plan's phase-1 demand; run_pipeline.py does it as step S
```
