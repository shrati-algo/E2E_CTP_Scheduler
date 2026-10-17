> **Carried over from the main project** (written 12-Oct for v82, the engine design is unchanged in v133). Paths refer to the engine, which here lives in `engine/`. For this version's settings see [../NO_CUTTER_SLITTER.md](../NO_CUTTER_SLITTER.md).

# Architecture

Back to [CLAUDE.md](../CLAUDE.md). Verified against the file system and code on 2026-10-12. No git repository.

## 1. Directories

| Path | Content |
|---|---|
| `run_v15.py` | **Entry point.** Runs every step in order (see [scheduling_flow.md](scheduling_flow.md)). Options: `--wip-date --wip-time --plan-end/--days --plan-dir --name --wip-source {old,mix} --offline --no-wip-mix --compare` |
| `run_scheduler.py` → `run_pipeline.py` | Runs phases 0–6 (`PHASES` list), staleness check, closed-loop rounds (`mix_planner.closed_loop`), quantity feedback `_qty_feedback` |
| `phases/` | `phase0_validate_inputs.py`, `phase1b_demand_explosion.py`, `phase1_5_wave_builder.py`, `phase1c_mrp_netting.py` (+ `mrp_time_phased.py`), `phase2_lot_sizing.py`, `phase3_dag_construction.py`, `phase4_cpm.py`, **`phase5_forward_placement_v2.py` (13,195 lines)**, `phase6_curing_revision.py`, `db_loader.py`, `day_pool.py` |
| `mixer-improvement/mix_planner.py` | Mixing planner (v56+), called from phase 5. Its own README |
| `build_sep_inputs.py` | Reads DB tables or `master_files`, plan files, WIP snapshot → `ctp_inputfiles/`, `db_pipeline/raw/` |
| `adapt_inputs.py` | → `inputs/*.csv` (`plan.csv`, `building_plan.csv`, `bom.csv`, `routing.csv`, `inventory.csv`, `aging_master.csv`, `changeover_matrix.csv`, `frc_lengths.csv`, `mpq*.csv`, …) |
| `apply_ctb_approximations.py` | v74 `tbr_bead_one_step`, v77 `inventory_item_alias`, v80 `ctb_wip_assumptions` (rewrites `inputs/inventory.csv` **in place**), `relaxed_mixing` |
| `apply_biascutter_calibration.py`, `apply_routing_gapfill.py`, `build_mpq_items.py` | Input corrections |
| `make_schedule_v18.py` + `strict_links.py` | Plant-format workbook and genealogy (`lot_id` / `consumption_id`, SHORT / EXPIRED_STOCK / OPENING_WIP labels) |
| Check scripts | `mixing_compliance.py`, `mixing_rule_audit.py`, `fourrc_checks.py`, `fourrc_sequence_report.py`, `quintoplex_checks.py` (also duplex / triplex / quadraplex via `--rules`), `validate_build_cure.py`, `mpq_checks.py`, `general_rules_checks.py`, `rule_adherence_report.py`, `make_version_readme.py`, `audit_business_rules.py`, `capacity_wall.py`, **`daily_numbers.py`** |
| `config.yaml` | ~2,300 lines; every rule switch and parameter, heavily commented with version tags |
| `db_pipeline/` | `db_connect.py` (`load_env`, `read_table`, offline fallback), `.env` (**secret**), `raw/` (last DB copies), `raw_backup_before_vNN/` |
| `inputs/` | Adapted inputs of the **last** run (overwritten each run) |
| `outputs2/` | Phase outputs during a run; moved to `latest_schedule/<name>/run_output/` at the end |
| `latest_schedule/` | Versions `v1 … v84` (+ `v16_wipmix`, `v75_relaxed`, `v78_stopped_06oct`, `exp_mixcap_*`), **`BUSINESS_RULES_LOG.md`**, `BOM_Routing_9oct/`, `plant-schedule/` (plant schedules and comparison scripts / reports) |
| `plan_files_10oct/`, `latest_plan_file/`, `plan-3ct/` | Plant plan files (4 xlsx each) |
| `CTB_WIP_ASSUMPTIONS_v2.xlsx` | The plan generator's WIP assumptions (v80) |
| `output-format/CTP_e2e_v18_SCHEDULE.xlsx` | Workbook template |

**Older docs:** `README.md`, `PHASEWISE_GUIDE.md`, `HOW_TO_RUN_V15.md`, `BUSINESS_RULES.md`, `ASSUMPTIONS.md`, `FLAW_REGISTRY.md`, `INPUT_FILES.md`, `INSTRUCTIONS.md`. Some parts describe the v6-era design; trust code / config / the log where they differ.

## 2. Version output folder (`latest_schedule/vNN/`)

| File | Content |
|---|---|
| `run_output/` | phase-5 / phase-6 outputs + `PHASEWISE/00_INPUTS` (a copy of the inputs used) and `PHASE*` folders |
| Schedule workbook | `CTP_<start>-<end>_SCHEDULE_vNN.xlsx` |
| Rule reports | `*_RULES_vNN.xlsx`, `MIXING_*`, `MPQ_*`, `GENERAL_RULES_*`, `RULE_ADHERENCE_REPORT_*`, `BUILD_CURE_VALIDATION_*` |
| Notes | `README_vNN.md` (rules in force), `run_log.txt`, `DAILY_NUMBERS_vNN.md` / `.xlsx` |

## 3. Key data files (in `run_output/`)

| File | Columns (main) |
|---|---|
| `PHASEWISE/PHASE2_LOTS/phase2_lots_updated.csv` | lot_id, item_code, item_type, department, lot_qty, lot_uom, raw_demand, surplus, duration_hours, first_need, last_need, min/max_aging_h, transfer_time_h, mpq_min, machines, batches, batch_size_kg, skus_set |
| `PHASEWISE/PHASE3_DAG/phase3_dag_edges_updated.csv` | producer_lot, consumer_lot, producer_item, consumer_item, via_sku, consume_qty. **Phase 3's links, not the phase-5 re-allocations** |
| `phase5_schedule_updated.csv` | lot_id, item, machine, scheduled_start / finish, duration_h, campaign_id, wire_type |
| `phase5_floor_schedule_updated.csv` | Per shift row: machine, item, item_type, department, start / end, produce_qty, UOM, lot_id, prod_id, consumption_id, FG_SKU_CODE (blank for building; take the line from the green-tyre code) |
| `phase5_building_echo_gates.csv` | Building row gate: plan_start, kit_ready, binding_item, start, kit_late |
| `phase6_curing_revision_updated.csv` | Plan curing rows re-timed: pressNo, startTime, endTime, qty, kit_status, green ages |
| `PHASEWISE/00_INPUTS/building_plan.csv`, `plan.csv` | Plan inside the window. Line: SKU starting "11" = TBR (`plan_id` is CTP_PCR for all rows, so don't use it) |

## 4. Configuration

- One file, `config.yaml`. Every phase loads it itself. Keys are tagged `vNN (user date)` with what was tested.
- `master_files` replaces any table that `build_sep_inputs._table()` reads with a file. Same columns; for xlsx the sheet name = the table name.
- Environment set by `run_v15.py` for every step:
  - `CTP_PLAN_DIR`, `CTP_PLAN_START`, `CTP_PLAN_END`, `CTP_WIP_DATE`, `CTP_WIP_CUTOFF`, `CTP_WIP_MIX`, `CTP_WIP_SOURCE`, `CTP_AGING_SOURCE=db`;
  - `DB_OFFLINE=1` for every step after `build_sep_inputs` (they read `db_pipeline/raw`);
  - `PYTHONUTF8=1`.

## 5. Database

| Database | Tables |
|---|---|
| `JK_CTP_PPDS` | `CTP_Opening_WIP`, `CTP_Master_BOM`, `CTP_Master_Routing`, `CTP_Master_Aging`, `CTP_Master_ChangeoverMatrix`, `CTP_Master_MPQ`, `CTP_Master_FRC`, `CTP_Master_BuildingAllowable` |
| `jkplanning_CTP` | `opening_wip` (old feed), `gt_inventory_manual_pcr` / `_tbr`, `jkt_building_cycle_times_pcr` / `_tbr`, `downtime_reason` |

- Access: read-only `SELECT *` with 4 retries; on failure the last snapshot in `db_pipeline/raw/` is used and logged.
- **Credentials only in `db_pipeline/.env`.**

## 6. Conventions (observed)

| Area | Convention |
|---|---|
| Phases | Standalone scripts reading / writing CSV in `outputs2/` |
| `phase5_forward_placement_v2.py` | One huge `main` with nested closures (`_material_ready_ns`, `_final_repair`, `_cfloor`, …) over shared dicts (`schedule`, `predecessors`, `successors`, `edge_qty`, `open_alloc`, `lot_item`, …). Times in **ns integers** (`NS`) |
| Config | Every behaviour behind a config switch whose comment records the version and the result |
| Logging | `print` with a bracketed tag per pass (e.g. `[UPSTREAM GAP FILL v81 A2]`, `[ITEM-STOCK v83]`, `[4RC CREEL FILL v58]`). The rule reports **parse `run_log.txt`** for some checks (BR-12 / 15 / 17, …) |
| Tests | No unit-test suite in use. Validation = the check scripts per version + the log numbers + plant comparisons |
| Patching | Large patches applied with small Python scripts doing exact string replacement plus `assert s.count(anchor) == 1`. Back up files first |

## 7. Fragile spots

| Spot | Why fragile |
|---|---|
| `inputs/inventory.csv` | Rewritten in place by `apply_ctb_approximations.py`; the original snapshot isn't kept |
| `phase3_dag_edges_updated.csv` | Does not contain the phase-5 re-allocations (v81 A2 / v83). Genealogy (`strict_links.py`) and analysis scripts reading it see phase 3's links |
| Material gate `_material_ready_ns` | Its shared arithmetic (edge_qty − open_alloc) is mirrored in three places (gate, BR-12 `_needs12`, starvation audit) and in the building echo. Change all of them together. v83's first run collapsed from a zero-quantity leftover link |
| Closed loop | Rounds 2–3 re-read phase outputs and re-plan the mixers from round-1 needs. A change in phase 5 also changes mixing timing |
| Line split | Use the SKU prefix "11" = TBR. Green-tyre line comes from the BOM Super_parent. `plan_id` is unreliable |
| `config.yaml` encoding | UTF-8 with non-ASCII comments; PowerShell `Set-Content` corrupts it |
| Stale runs | Never run two pipelines at once: they share `inputs/` and `outputs2/` |
