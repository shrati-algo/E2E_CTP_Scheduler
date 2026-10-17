# Inputs

A run uses three kinds of input:

1. the **plan folder** you give with `--plans`;
2. **database tables**, either live or from the snapshot copy;
3. **reference files** shipped inside `engine/`.

## 1. Plan folder (the "drum")

These are the plant's plan exports, one workbook each:

| Pattern | Content |
|---|---|
| `ctp_curing_schedule_*_pcr*.xlsx` | PCR curing plan per press and shift |
| `ctp_curing_schedule_*_tbr*.xlsx` | TBR curing plan per press and shift |
| `ctp_building_schedule_*_pcr*.xlsx` | PCR building plan per tyre-building machine (TBM) and shift |
| `ctp_building_schedule_*_tbr*.xlsx` | TBR building plan per TBM and shift |

**Sheet `Shift Schedule`.** The header row is found automatically within the first 10 rows (the row holding `Machine`, `SKUCode` and `StartTime`).

| Column | Used for |
|---|---|
| `Machine` | press (curing) / TBM (building) |
| `SKUCode` | tyre SKU, which links to the BOM `Super_parent` |
| `StartTime`, `EndTime` | slot; rows outside the plan window are dropped |
| `Qty` | tyres |
| `Shift` | building shift |
| `GT_Code`, `Description` | building: green tyre code, description |
| `CO_Mins`, `CO_Type` | building: planned changeover minutes / type |

**How the plans are used:**

- **Curing plan:** this is the demand. The presses and slots are kept, and phase 6 only re-times cure blocks on the same presses to when green tyres are ready.
- **Building plan:** this is the TBM plan, and component demand is exploded from it (`building_plan_echo`). TBM minutes are re-rated with the database building cycle times.
- **Window:** plan rows are kept when `StartTime` is in [plan start, plan end).

Other sheets in the workbooks (summaries, notes) are ignored.

## 2. Database tables

All access is read-only. Tables are read live with `--db live`, or from `engine/db_pipeline/raw/<db>__<table>.csv` offline.

| Database.table | Used for |
|---|---|
| `JK_CTP_PPDS.CTP_Master_BOM` | BOM per SKU: green tyre → parts → rolls / strips → compounds → raw materials |
| `JK_CTP_PPDS.CTP_Master_Routing` | operation, machines, speed, batch size, transfer time per item |
| `JK_CTP_PPDS.CTP_Master_Aging` | min / max aging (shelf life) per item |
| `JK_CTP_PPDS.CTP_Master_ChangeoverMatrix` | changeover minutes between items per machine |
| `JK_CTP_PPDS.CTP_Master_MPQ` | minimum / maximum run quantities (TBR rows) |
| `JK_CTP_PPDS.CTP_Master_FRC` | 4-roll calender spool / dip lengths |
| `JK_CTP_PPDS.CTP_Master_mixing` | mixing batch weights per compound and mixer |
| `JK_CTP_PPDS.CTP_Master_BuildingAllowable` | building cycle time per SKU and machine |
| `JK_CTP_PPDS.CTP_Opening_WIP` | **opening WIP** (several snapshots a day) |
| `jkplanning_CTP.opening_wip` | older WIP feed (`--wip-source old`; fallback for compounds) |
| `jkplanning_CTP.jkt_building_cycle_times_pcr / _tbr` | building cycle times (fallback) |
| `jkplanning_CTP.gt_inventory_manual_pcr / _tbr` | green-tyre stock entered by hand |
| `jkplanning_CTP.downtime_reason` | machine downtime |

**Opening WIP.** The plan start is the opening-WIP time. With `--wip-source mix` (the v133 default), the engine takes the `CTP_Opening_WIP` snapshot at or before the plan start: components, compounds and green tyres. Compound batches are converted to kg with the routing batch weight.

`--wip-correction` adds the lots of a later snapshot that were produced before the plan start. This is for snapshots that missed late postings.

**WIP assumptions.** These are applied as in v133 (`config.yaml` → `ctb_wip_assumptions`, from the file `CTB_WIP_ASSUMPTIONS_v2.xlsx`):

- expired stock has 0 h of extra life;
- 24 h of measured upstream flow is added for items that have a flow rate;
- untracked building components are assumed present.

There is also a first-24-h assumption for cap strip and edge gum (`kit_gate_assume_first_h`).

## 3. Reference files inside `engine/`

| File | Used for |
|---|---|
| `config.yaml` | all settings (v133 + `cutter_bypass` + corrected BOM in `master_files`) |
| `bom_corrected/CTP_Master_BOM_corrected.csv` | the corrected database BOM, read instead of `CTP_Master_BOM` (`--bom db` turns it off) |
| `reference/SMART_BOM.xlsx` (+ `backcalc/cache/smart_bom.pkl`) | SMART BOM export, used only by the back calculation |
| `Master Data/Change Over Master/` | plant change-over master; 4RC roll widths for the back calculation |
| `line_speed_by_item_3_months 1.xlsx` | plant line speeds. v133 takes the faster of this and the database routing (`linespeed_file`), except inner liner, chafer slitters, TBR body ply and belt edge filler |
| `CTB_WIP_ASSUMPTIONS_v2.xlsx` | WIP assumptions (above) |
| `Algo Planning Business Rules 1.xlsx`, `businees_rules/Upstream Business Rules.xlsx` | rule texts for the reports / workbook |
| `output-format/CTP_e2e_v18_SCHEDULE.xlsx` | template of the plant schedule workbook |
| `ctp_inputfiles/*` (fixed files) | machine list (`CTP Machines.xlsx`), MPQ files, legacy masters still named as fallbacks |
| `inputs/building_changeover.csv` | building changeover rules |

The engine rewrites the other files in `ctp_inputfiles/` and `inputs/` on every run. These are working files; don't edit them.
