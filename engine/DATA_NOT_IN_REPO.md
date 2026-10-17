# Data not in this repository

This branch is public, so it holds **code, configuration and documentation only**. To run, place these files locally. Paths are relative to `engine/`. Get them from the project team or a private copy of the packaged repo.

| Path | What it is | Needed for |
|---|---|---|
| `db_pipeline/raw/<db>__<table>.csv` | offline database copy (one CSV per table, list below) | `--db offline` (the default). With `--db live` the engine downloads them itself (needs `db_pipeline/.env`) |
| `db_pipeline/.env` | database login (copy `.env.example`) | `--db live` only. **Never commit** |
| `line_speed_by_item_3_months 1.xlsx` | plant line speeds (v133 `linespeed_file`) | every run |
| `CTB_WIP_ASSUMPTIONS_v2.xlsx` | plan generator WIP assumptions (`ctb_wip_assumptions`) | every run |
| `Algo Planning Business Rules 1.xlsx` | rule texts (phase 6 / reports) | every run |
| `businees_rules/Upstream Business Rules.xlsx` | upstream rule texts (schedule workbook) | every run |
| `output-format/CTP_e2e_v18_SCHEDULE.xlsx` | plant schedule workbook template | every run |
| `ctp_inputfiles/` fixed files | `CTP Machines.xlsx`, `MOQ 5_treadM.xlsx`, `mixing_mpq_ctp.xlsx`, `mpq_user_20260727.xlsx`, `jkt_mpq_corrected.xlsx`, `CTP_PCR_Machine_LineSpeed_Verify_CHECKED.xlsx` and the other `CTP_Machine_LineSpeed_*.xlsx`, `jkt_*` legacy masters, `aging_comb.csv`, `bom_comb.csv`, `routing_comb.csv`, `building_comb.xlsx`, `curing_comb.xlsx`, `ctppcr_plan_shiftsheet.xlsx`, `opening_wip.csv` | every run (fallbacks / reference) |
| `inputs/building_changeover.csv` | building changeover rules | every run |
| `bom_corrected/CTP_Master_BOM_corrected.csv` | the corrected database BOM (from the main project's `bom_validation/bom_corrections.py`) | default runs (`--bom corrected`); not needed with `--bom db` |
| `reference/SMART_BOM.xlsx` (and optionally `backcalc/cache/smart_bom.pkl`) | SMART BOM export | the back calculation (`BACK_CALCULATION_<run>.xlsx`) |
| `Master Data/Change Over Master/` (at least `4RC.xlsx`) | plant change-over master: 4RC roll widths | the back calculation |

**Database tables in `db_pipeline/raw/`:**

- `JK_CTP_PPDS`: `CTP_Master_BOM`, `CTP_Master_Routing`, `CTP_Master_Aging`, `CTP_Master_ChangeoverMatrix`, `CTP_Master_MPQ`, `CTP_Master_FRC`, `CTP_Master_mixing`, `CTP_Master_BuildingAllowable`, `CTP_Opening_WIP`.
- `jkplanning_CTP`: `opening_wip`, `jkt_building_cycle_times_pcr`, `jkt_building_cycle_times_tbr`, `gt_inventory_manual_pcr`, `gt_inventory_manual_tbr`, `downtime_reason`.

The reference results (v3: 130,366 cured on plan-130ct; v2: 152,200 on plan-12oct) were verified with the private packaged copy: masters from the 13 / 14-Oct database pull, WIP from the 15-Oct pull.

The engine creates the folders `latest_schedule/`, `outputs/` and `outputs2/`, and the working files in `ctp_inputfiles/` and `inputs/`, by itself.
