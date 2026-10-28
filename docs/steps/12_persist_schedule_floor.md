# Step 12: the complete schedule saved to `JK_CTP_PPDS.CTP_Schedule_Floor`

**Code:** `pipeline/persist_floor.py`, called by `run_pipeline.py` after every output is made.

**Every run writes** the complete end-to-end schedule: every upstream lot, every building trolley and every curing lot, cut into shift pieces (step 6). One row per piece. It then **reads the rows back** and compares them with the outputs.

The output files are written exactly as before; the database copy is extra.

- `--no-db-write` prepares and checks the rows without writing.
- Re-persist or re-check a finished run:

  ```bash
  python -m pipeline.persist_floor --run <run> [--replace] [--validate-only]
  ```

## Schedule ID

| | Example | Where |
|---|---|---|
| Schedule ID (text) | `20261028_162333_v46k` | `outputs/<run>/SCHEDULE_ID.json`, run log, `RUN_SUMMARY.md` |
| Run start time | 28 Oct 2026 16:23:33 | part of the text ID |
| Version | `v46k` | `meta.run` in `config/rules.yaml`, the repository's own version label |
| `CTP_Schedule_Floor.schedule_id` | `2026-10-28 16:23:33` | the table |

- The ID is made **once**, when `run_pipeline.py` starts, and every row of the run carries it.
- **The table's `schedule_id` column is `DATETIME NOT NULL`**, commented "run start time of the schedule". It cannot hold the text `…_v46k`.
- So the database stores the **timestamp part**. The text ID ↔ timestamp mapping is 1:1 and is recorded in `SCHEDULE_ID.json`.
- To query a run: `WHERE schedule_id = '2026-10-28 16:23:33'`.
- **Decision (28 Oct 2026): `schedule_id` stays DATETIME.** The run start timestamp identifies the schedule. The version is in `SCHEDULE_ID.json` and in the run's outputs. No schema change.

## Table schema (read from the database, 28 Oct 2026)

- 29 columns. `tyre_type` was added on 28 Oct 2026, `VARCHAR(8)` like the plan tables.
- No primary key.
- Indexes: `idx_schedule (schedule_id)`, `idx_date_shift`, `idx_machine_time`, `idx_item`.
- `STRICT_TRANS_TABLES` is on, so a bad value raises an error instead of being cut short.

## Mapping

Source: `phase5_floor_schedule_{pcr,tbr}.csv`, one row per lot piece, plus the engine files named below.

| Column (type) | From |
|---|---|
| `schedule_id` datetime NOT NULL | run start time (see above) |
| `schedule_date` date NOT NULL | earliest plant date in the schedule (= plan day) |
| `schedule_shift` varchar(5) NOT NULL | shift of the earliest row (the shift the schedule starts in) |
| `tyre_type` varchar(8) | **PCR / TBR**: the floor-schedule file the row comes from (`phase5_floor_schedule_pcr` / `_tbr`). Shared machines (mixers, 901, 1102) carry the line of their lot. Filled on every row |
| `date`, `shift` | floor row: plant day and shift of the piece |
| `machine` varchar(40) | floor row (work-centre id) |
| `machine_name` varchar(120) | floor row; when blank, `CTP_Machine_Master.machine_name` |
| `department` varchar(60), `process` varchar(80) | floor row |
| `section` varchar(20) | `CTP_Machine_Master.section` of the machine: Mixing, Stock Preparation, Building, Curing. NULL if the machine is not in the master |
| `item` varchar(80) | floor row: the item made (building: the green tyre). **Curing: the FG SKU.** The floor file leaves curing items blank, and the cured tyre is the SKU |
| `item_description` varchar(255) | building: `<FG SKU> \| <plan description>`; curing: plan description of the SKU; upstream: BOM description of the item, plus ` \| FG <sku>` when the lot serves one SKU |
| `item_type` varchar(60) | floor row |
| `lot_id` varchar(80) | floor row. Curing rows have none there, so `<LINE>_CUR_<press>_<nnnn>`: one ID per press lot, shared by its shift pieces |
| `start_time`, `end_time` datetime | floor row: the piece inside its shift |
| `produce_qty` decimal(14,3), `UOM` | floor row: the quantity made in that shift |
| `changeover_min` decimal(10,2) | the lot's changeover on its **first** piece: upstream `phase5_schedule.changeover_min`; building `setup_applied_min`; curing `co_min`. **0** on the later shift pieces of the same lot, because it carries on without a changeover. Never empty |
| `changeover_from` varchar(80) | item of the previous lot on the machine, when it differs (first piece) |
| `changeover_type` varchar(120) | building: plan `CO_Type`, else the setup source; curing: `co_source`; upstream: `item change` |
| `consumer_machine` varchar(500) | engine `floor_schedule.csv` consumer of the lot |
| `compound` varchar(80) | upstream non-mixing lots: the compound(s) mixed for that lot (phase-4 producers in a MIXING department) |
| `component_number`, `mhe`, `mhe_count`, `die`, `preformer` | **NULL**: the scheduler has no source (no MHE, die or preformer master) |

**The FG SKU has no column of its own in the table.** It is carried in `item` (curing) and `item_description` (building, and upstream lots that serve one SKU). PCR / TBR is in `tyre_type`.

## Safety

- **Column checks before writing.** Every value is checked against the live column types: NOT NULL, varchar length, decimal range. If anything fails, **nothing is written**, and every failing value is listed in `db_persist/rows_failed_validation.csv`.
- **No duplicates.** If rows already exist for the run's `schedule_id`, they are not written again. `--replace` (standalone command only) deletes **that schedule_id only** and writes it again. Other schedules are never touched.
- **One transaction.** Insert everything (parameterised `executemany`), check the row count, then COMMIT. Any error rolls everything back.
- **A database failure never hides a finished schedule.** All output files are already written. The run ends with exit code 4, and the summary says what failed.

## Read-back validation (`db_persist/validation_result.json`)

1. **Database vs prepared rows, every column:** missing, unexpected and duplicate rows.
2. **Prepared rows vs the floor schedule, row by row:**
   - date, shift, machine, department, process, item_type, UOM, start / end time and quantity;
   - item and lot_id (upstream and building);
   - item = FG SKU (curing).
3. **Stage coverage:** upstream, building and curing row counts and quantity totals, floor vs database.
4. **`tyre_type`:** every row against the file it came from, and the row count per line, floor vs database.

**PASS** only when all counts are 0 and all three stages are present.

## Test-only: plans from the history tables

```bash
python run_pipeline.py --date 2026-10-27 --test-history-plan --plan-run-id PCR=20261027_154658,TBR=20261027_155919 --run v46k_test_27oct
```

`--test-history-plan`:
- reads `CTP_Schedule_*Plan_History`;
- is refused without explicit planner runs for **both** lines;
- prints a TEST RUN banner.

A normal run never reads the history tables (test `test_normal_runs_read_the_live_plan_tables`).
