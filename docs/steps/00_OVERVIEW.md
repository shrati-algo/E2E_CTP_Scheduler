# Daily pipeline from the database: overview

One command makes the whole schedule for one plant day:

```bash
python run_pipeline.py                      # the current plant day (06:30 -> 06:29)
python run_pipeline.py --date 2026-10-28    # a given plant day
```

The engine and rules are **v46k**, unchanged. The difference from the plan-folder version: the plan is read from the database instead of Excel files. Stock, green tyres and every master also come from the database for that day.

| Step | Page | What it does | Main output |
|---|---|---|---|
| 0 | [00a_preflight_checks.md](00a_preflight_checks.md) | **Checks the day's tables are updated and complete** (building plan, curing plan, WIP, GT). PASS / WARN / FAIL on the terminal; any FAIL and nothing runs | `PREFLIGHT_CHECKS.md`, `logs/preflight_history.csv` |
| 1 | [01_plan_from_db.md](01_plan_from_db.md) | Reads the day's building and curing plans from `CTP_Schedule_BuildingPlan` / `CTP_Schedule_CuringPlan` | `plan_from_db/` |
| 2 | [02_stock_and_gt_snapshots.md](02_stock_and_gt_snapshots.md) | Picks the 06:30 (shift A) stock and green-tyre snapshot of the day, and refuses an incomplete one | console + `DATA_STAGED.json` |
| 3 | [03_masters.md](03_masters.md) | Copies every master (BOM, routing, ageing, …) and the snapshots into `data/ppds_<day>/` | `data/ppds_<day>/` |
| 4 | [04_stock_split.md](04_stock_split.md) | Splits the opening stock PCR / TBR by this plan's demand | `data/ppds_<day>/opening_wip_{pcr,tbr}.csv` |
| 5 | [05_scheduler.md](05_scheduler.md) | Engine phases 0–7, both lines | `pcr/`, `tbr/` |
| 6 | [06_floor_schedule.md](06_floor_schedule.md) | The shift-wise floor schedule (lots cut at 07:00 / 15:00 / 23:00) | `phase5_floor_schedule_{pcr,tbr}.csv` |
| 6b | [06b_shift_production.md](06b_shift_production.md) | Tyres and upstream quantity made in each shift (split lots counted only in the shift they were made in) | `SHIFT_PRODUCTION_<run>.csv/.xlsx` |
| 7 | [07_checks.md](07_checks.md) | Routing validation, machine coverage, business and mixing rules | `VALIDATION.csv`, … |
| 8 | [08_workbooks.md](08_workbooks.md) | The plant schedule book and the per-line book | `CTP_<run>_SCHEDULE.xlsx` |
| 9 | [09_fulfilment_and_validation_report.md](09_fulfilment_and_validation_report.md) | Built / cured day by day; data blockers, assumptions, gates | `FULFILMENT_DAYWISE_v15.csv`, `VALIDATION_REPORT.xlsx` |
| 10 | [10_stock_reports.md](10_stock_reports.md) | Cut-stock conversion; stock present vs consumed; day-1 stock vs building | 3 workbooks |
| 11 | [11_analysis.md](11_analysis.md) | Day-1 machine starts, shift load balance, built-not-cured, extruder idle | `analysis/` |
| 12 | [12_persist_schedule_floor.md](12_persist_schedule_floor.md) | **The complete schedule written to `JK_CTP_PPDS.CTP_Schedule_Floor`** under the run's `schedule_id`, read back and compared with the outputs | `SCHEDULE_ID.json`, `db_persist/` |
| 13 | [../../phased-testing/README.md](../../phased-testing/README.md) | **Phase-wise test reports of this run**: 7 reports in the `test-report-phasewise` format, every check evaluated | `phased-testing/reports/<schedule_id>/` |
| 14 | [13_summary.md](13_summary.md) | One page with inputs, results, warnings and step times | `RUN_SUMMARY.md` |

**Results go to** `outputs/v46k_db_<day>/`, or the name given with `--run`. Start with `RUN_SUMMARY.md`.

**Database rules:**
- Steps 1–3 only **read**.
- The only write is step 12: rows of this run's `schedule_id` in `CTP_Schedule_Floor` (`--no-db-write` turns it off). No other table is written, and no other schedule is touched.
- The login comes from `db.env` (git-ignored) or environment variables.

**When the pipeline stops on its own:**
- a failed pre-flight check (exit 2): no plan for the day, no curing plan of the same planner run, no shift-A stock or GT sync for the day, or an incomplete sync;
- a failed engine phase (exit 3).

**Logs:** everything printed on the terminal is also in `outputs/<run>/logs/run_<time>.log`. Every pre-flight check is appended to `logs/preflight_history.csv`.

It never falls back silently: you choose, and the choice is recorded.
