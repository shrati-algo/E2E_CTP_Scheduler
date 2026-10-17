# JK Tyre CTP end-to-end scheduler: v133

**What it does:** turns the plant's curing and building plans (PCR and TBR) into a timed, machine-level floor schedule.

- **Upstream:** mixing, the 4-roll calender, calenders, extruders, cutters, slitters and bead.
- **Downstream:** tyre building and curing.
- **Starting point:** the opening WIP in the plant database.
- **Also produced:** daily numbers and business-rule compliance reports.

This repository is the **v133** version of the scheduler, packaged to run from **one command on a folder of plan files**.

```powershell
python run_schedule.py --plans plans\plan-12oct
```

| | |
|---|---|
| **Version** | v133 (`engine/config.yaml`) |
| **Reference result** | plan-12oct, opening WIP 12-Oct 07:00, window 12-Oct 07:00 → 22-Oct 07:00: **132,919 of 175,628 tyres cured (75.7 %)**, PCR 104,672 and TBR 28,247. See [docs/V133.md](docs/V133.md) |
| **Method** | Heuristic, rule-based scheduling (MRP netting, lot sizing, precedence graph, forward placement with a material gate, rule repair passes, mixing planner, curing re-timing). There is no mathematical solver, so there is no objective value or optimality gap. Quality is judged by tyres cured and rule compliance |
| **Run time** | about 10–60 minutes, depending on the computer (the verification run took 10 min) |
| **Database** | read-only. Offline by default, using the snapshot copy in `engine/db_pipeline/raw`. `--db live` reads the database |

> **This public branch holds code, configuration and documentation only.** The plant plans, the offline database copy and the reference data files are not included (repository rule). To run, add them as listed in [engine/DATA_NOT_IN_REPO.md](engine/DATA_NOT_IN_REPO.md), or use `--db live` with your own `.env`.

## Quick start

1. **Install** Python 3.11 or newer (tested on 3.13), then:
   ```powershell
   pip install -r engine\requirements.txt
   ```
2. **Make a plan folder** with the four plant plan workbooks. There must be exactly one of each:
   ```
   my-plan\
     ctp_curing_schedule_<anything>_pcr<anything>.xlsx
     ctp_curing_schedule_<anything>_tbr<anything>.xlsx
     ctp_building_schedule_<anything>_pcr<anything>.xlsx
     ctp_building_schedule_<anything>_tbr<anything>.xlsx
   ```
   Each needs a `Shift Schedule` sheet. Details: [docs/INPUTS.md](docs/INPUTS.md).
3. **Check the inputs.** This takes seconds and does not run the schedule:
   ```powershell
   python run_schedule.py --plans my-plan --check-only
   ```
   It shows the four files found, the plan window, and which opening-WIP snapshot will be used.
4. **Run it:**
   ```powershell
   python run_schedule.py --plans my-plan
   ```
5. **Results** are in `outputs\v133_my-plan\`:
   - the schedule workbook;
   - `DAILY_NUMBERS_*.md`, the plan / built / cured table per plant day;
   - the rule-compliance reports;
   - the run log.

Full guide (options, live database, reproducing the reference, troubleshooting): [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md).

## Folder layout

```
ctp-scheduler-v133/
  run_schedule.py          single entry point (checks the plan folder, works out the window, runs the engine, collects results)
  README.md                this file
  docs/                    documentation (start with HOW_TO_RUN.md)
  plans/                   plan folders (plant plans are not in this public repo; see plans/README.md)
  outputs/<run name>/      results of each run (copied here at the end of the run)
  engine/                  the v133 scheduler itself -- do not rename files inside
    config.yaml            v133 settings (233 sections; key ones in docs/V133.md)
    run_v15.py             engine driver (run_schedule.py calls it)
    build_sep_inputs.py    plan files + database -> engine inputs
    adapt_inputs.py, apply_*.py, build_mpq_items.py   input preparation
    run_scheduler.py, run_pipeline.py                 phase runner (phases 0 -> 6, then 3 closed-loop rounds)
    phases/                phase 0 .. 6 (phase5_forward_placement_v2.py = placement and all repair passes)
    mixer-improvement/     mixing planner
    *_checks.py, *_report.py, daily_numbers.py, make_schedule_v18.py   reports and the plant workbook
    db_pipeline/           read-only database access + raw/ = snapshot copy of the database tables
    ctp_inputfiles/, inputs/   reference files + working files rewritten on every run
    output-format/, businees_rules/, *.xlsx           workbook template, rule sheets, line-speed and WIP-assumption files
    latest_schedule/<run name>/   full engine output of each run (incl. run_output/ working csv files)
```

## Documentation

| Document | Content |
|---|---|
| [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md) | Install, run, every option, live database, reproducing v133, troubleshooting |
| [docs/INPUTS.md](docs/INPUTS.md) | Plan file format, database tables used, opening WIP, reference files |
| [docs/OUTPUTS.md](docs/OUTPUTS.md) | Every output file and how to read it |
| [docs/PIPELINE.md](docs/PIPELINE.md) | What happens in a run, step by step |
| [docs/V133.md](docs/V133.md) | What v133 is: key settings, how it was reached, reference daily numbers |
| [docs/LIMITS.md](docs/LIMITS.md) | Known limits and data issues to keep in mind |
| [docs/reference/](docs/reference/) | Detailed engine documents carried over from the project (architecture, scheduling flow, business rules, model, known issues) |

## Important

- **Results vary from run to run** by about ±4–5 k cured tyres when inputs change slightly. Differences below that are not significant.
- **The database BOM has known errors.** Examples: two SKUs with SMART rows added several times, the tread first-compound weight, and the MONOCHAFER chain. v133 uses the BOM **as it is**. See [docs/LIMITS.md](docs/LIMITS.md).
- **Database credentials are never stored here.** `engine/db_pipeline/.env` is needed only for `--db live`; copy `.env.example`.
