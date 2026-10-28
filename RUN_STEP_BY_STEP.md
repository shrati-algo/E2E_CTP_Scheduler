# Running the daily CTP schedule from the database, step by step

You need:
- **Python 3.11+**;
- network access to the plant database (MySQL);
- the database login in a file `db.env`. It is given to you separately and is never in git.

## 1. Get the code

```bash
git clone https://github.com/shrati-algo/E2E_CTP_Scheduler.git
cd E2E_CTP_Scheduler
git checkout v46k-db-pipeline
```

## 2. Install the Python packages (once)

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Linux / Mac:  source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Put the database login in place (once)

Put `db.env` in the repository folder, or copy `db.env.example` to `db.env` and fill it in:

```
CTP_DB_HOST=...
CTP_DB_PORT=3306
CTP_DB_USER=...
CTP_DB_PASSWORD=...
CTP_PPDS_NAME=JK_CTP_PPDS
```

`db.env` is git-ignored. The same names can be set as environment variables instead.

## 4. Run the day

```bash
python run_pipeline.py                      # the current plant day
python run_pipeline.py --date 2026-10-28    # the plan of a given plant day
```

**Which day.** A plant day runs **06:30 → 06:29 the next morning**. Started any time from 06:30 on day 1 until 06:29 on day 2, the run is for **day 1**: day 1's building and curing plans, and day 1's 06:30 WIP and GT snapshots. From 06:30 on day 2 it is for day 2. The terminal prints which day it took and why. `--date` overrides it.

What happens (about 15–20 minutes; every step has a page in `docs/steps/`):

0. **Pre-flight checks:** are today's building plan, curing plan, WIP and GT tables updated and complete? Each check is shown as `[PASS]` / `[WARN]` / `[FAIL]`. **If any fails, nothing runs.** Only the checks: `python run_pipeline.py --check-only`.
1. **Plan:** the day's PCR and TBR building and curing plans from `CTP_Schedule_BuildingPlan` / `CTP_Schedule_CuringPlan`.
2. **Snapshots:** the stock (`CTP_Opening_WIP`) and green tyres (`CTP_GT_Inventory`) of the **shift-A (06:30) sync of that day**.
3. **Masters:** BOM, routing, ageing, … staged into `data/ppds_<day>/`.
4. **Stock split:** stock split PCR / TBR by the plan's demand.
5. **Scheduler:** both lines.
6. **Floor schedule:** shift-wise, lots split at shift changes.
7–10. Checks, workbooks, fulfilment, validation report, stock reports.
11. **Analysis:** day-1 machine starts, shift load balance, built-not-cured, extruder idle.
12. **Database:** the complete schedule (upstream, building, curing, per shift) written to `JK_CTP_PPDS.CTP_Schedule_Floor` under this run's `schedule_id`, then read back and checked.
13. **Phase-wise test reports:** `phased-testing/reports/<schedule_id>/`. Seven reports (`TEST_CASES_PHASE1..7.md`) plus `SUMMARY.md`, with every check evaluated on this run.
14. **Summary:** `RUN_SUMMARY.md`.

## From VS Code

Open the `P&s-PROD` folder (or this repository folder) in VS Code. **Run and Debug** offers:
- *CTP: run the current plant day (06:30 -> 06:29)*;
- *CTP: run a chosen date*: asks for the date;
- *CTP: checks only (chosen date)*;
- *CTP: chosen date + explicit WIP snapshot*.

The plain ▶ Run button on `run_pipeline.py` runs the current plant day (06:30 → 06:29).

## 5. Read the results

Results go to `outputs/v46k_db_<day>/`:

| File | What it tells you |
|---|---|
| `PREFLIGHT_CHECKS.md` | the pre-flight checks of this run |
| `logs/run_<time>.log` | everything that was printed on the terminal |
| `RUN_SUMMARY.md` | **start here**: which plan and snapshots were used, checks, fulfilment, warnings |
| `VALIDATION_REPORT.xlsx` | data blockers, assumptions, schedule gates |
| `CTP_<run>_SCHEDULE.xlsx` | the plant schedule book |
| `phase5_floor_schedule_{pcr,tbr}.csv` | the shift-wise schedule, one row per machine × lot × shift |
| `FULFILMENT_DAYWISE_v15.csv` | built / cured per day and line |
| `analysis/` | the analysis workbooks |
| `plan_from_db/` | the plan rows exactly as read from the database |
| `SCHEDULE_ID.json` | the run's schedule_id (`YYYYMMDD_HHMMSS_v46k`) and the timestamp stored in `CTP_Schedule_Floor` |
| `db_persist/` | the rows written to `CTP_Schedule_Floor`, the write result and the read-back validation |

## If something stops

| Message | What to do |
|---|---|
| `database credentials missing` | step 3 |
| `no PCR building plan in CTP_Schedule_BuildingPlan with plan_start on <day>` | the planner has not saved that day yet; the message lists the days the table holds |
| `CTP_Opening_WIP: no shift-A sync on <day>` | the 06:30 sync is not in the database yet. Wait, or choose one: `--wip-sync "<updated_at>"` |
| `... holds 1,644 rows, under 50% of the usual ... INCOMPLETE snapshot` | the 06:30 sync is partial. Raise it with PPDS, or choose a complete sync explicitly with `--wip-sync`. The summary records the choice |
| `CTP_GT_Inventory PCR/TBR ...` | the same, with `--gt-sync-pcr` / `--gt-sync-tbr` |
| `the data folder lacks ...` | run again with `--restage` |

## The schedule in the database

Every run writes its schedule to `JK_CTP_PPDS.CTP_Schedule_Floor`. Read it back with:

```sql
SELECT * FROM JK_CTP_PPDS.CTP_Schedule_Floor WHERE schedule_id = '<timestamp from SCHEDULE_ID.json>';
```

- `--no-db-write` skips the write.
- Re-check a finished run: `python -m pipeline.persist_floor --run <run> --validate-only`.
- Details: `docs/steps/12_persist_schedule_floor.md`.

The login in `db.env` needs INSERT on that table, plus DELETE if you use `--replace`.
