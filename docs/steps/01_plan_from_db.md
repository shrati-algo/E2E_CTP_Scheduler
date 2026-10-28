# Step 1: the plan, read from the database

**Code:** `pipeline/plan_from_db.py`.

## Tables

| Table | One row is | Columns used |
|---|---|---|
| `JK_CTP_PPDS.CTP_Schedule_BuildingPlan` | one building machine × one shift slice | `run_id, plan_id, tyre_type, plan_start, Date, Shift, Machine, Machine_Group, SKUCode, CO_Type, Qty, StartTime, EndTime, CO_Mins, GT_Code, Description, created_at` |
| `JK_CTP_PPDS.CTP_Schedule_CuringPlan` | one press × one shift slice | `run_id, plan_id, tyre_type, plan_start, Date, Shift, Machine, SKUCode, skuDescription, StartTime, EndTime, Qty, CO_Mins, Mould_Clean_Mins, CycleTime_min, GT_Inventory, Remarks, GT_Code, created_at` |

**Only these two tables are read, every day.** The `*_History` tables are read only with the explicit test flag `--test-history-plan`, which also needs `--plan-run-id` for both lines. See `12_persist_schedule_floor.md`. A normal run never reads them.

## Row order

Rows are read **in the table's `id` order**, which is the planner's own row order (identical to its Excel export). The engine numbers trolleys in row order, so a different order changes trolley IDs and some tie-breaks.

On the 27 Oct plan, reading by machine and start time gave a 0.3 % different curing result. Reading by `id` reproduces v46k exactly.

## Which plan is taken

1. For plant day **D**, the plan of each line (PCR, TBR) is the one whose `plan_start` falls on D. Example: `PCR_2026-10-28`, plan_start 28 Oct 07:00.
2. **If the planner saved that day more than once** (several `run_id`), the newest `created_at` is taken. The others are listed in the run summary.
   - To use a different one: `--plan-run-id PCR=20261028_162333,TBR=20261028_163653`.
3. The building and curing rows of a line come from the **same** `run_id`. If the curing rows are missing, the run stops.
4. **No plan for the day:** the run stops and lists the days the table does hold.

## What is written

- The rows are put into **the same columns as the planner's Excel "Shift Schedule"**, so the engine reads them exactly as before:
  - `outputs/_plans/<run>/<line>/pe_format/ctp_building_schedule_plan_<line>.xlsx`;
  - `outputs/_plans/<run>/<line>/pe_format/ctp_curing_schedule_plan_<line>.xlsx`.
- `outputs/<run>/plan_from_db/`:
  - the raw rows as read, one CSV per table and line;
  - `plan_catalogue.csv`: every plan the tables hold;
  - `plan_selection.json`: what was taken, with counts.

## Same input as the Excel plans

The Excel exports carry some extra columns that the engine never reads. They are not in the tables, and they are not needed:
- building: `Run_Qty`, `Slice`;
- curing: `wc_id`, `Qty_Delivered`, `Nameplate_Seated`, `Press_Hours`.

`skuDescription` becomes `Description`.

The test `tests/test_repo_basics.py::test_db_plan_gives_the_engine_the_same_plan_as_excel` loads the 27 Oct Excel plans both ways and checks the engine sees identical rows.

## Checks (reported in the summary, nothing is repaired)

- rows with `Qty <= 0` (the engine ignores them);
- rows ending before they start, or starting before the plan day's 07:00;
- SKUs built but never cured;
- SKUs cured but not built (they can only cure from GT stock).

## Example: 28 Oct

| Line | run_id | Build | Cure |
|---|---|---|---|
| PCR | `20261028_162333` | 54,371 tyres on 11 TBMs | 54,530 on 86 presses |
| TBR | `20261028_163653` | 14,179 tyres on 9 TBMs | 14,814 on 80 presses |

Both run 28 Oct 07:00 → 1 Nov 07:00.
