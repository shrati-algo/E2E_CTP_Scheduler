# CTP scheduler (v46k engine)

End-to-end production scheduler for the CTP plant (PCR + TBR).
- **Input:** the plant's **building and curing plans**.
- **Read from the plant database:** the master data and the opening stock.
- **What it schedules:** every upstream lot, from mixing through calendering, extrusion and beads to building and curing. It keeps the plan's machine and sequence on builders and presses.
- **What it reports:** what can be built and cured, day by day.

**This branch (`v46k-db-pipeline`): one command per plant day, everything from the database.**

```bash
pip install -r requirements.txt
cp db.env.example db.env      # fill in the database login (git-ignored)
python run_pipeline.py                      # the current plant day
python run_pipeline.py --date 2026-10-28    # a given plant day
```

| Read from `JK_CTP_PPDS` | For plant day D |
|---|---|
| **Checked first** | building plan, curing plan, WIP and GT updated and complete for D. PASS / WARN / FAIL on the terminal and in the log; **any FAIL and nothing runs** |
| Plan | `CTP_Schedule_BuildingPlan` + `CTP_Schedule_CuringPlan`, `plan_start` on D (newest planner run if saved more than once) |
| Stock | `CTP_Opening_WIP`, the **shift-A (06:30) sync of D**. Refused if incomplete |
| Green tyres | `CTP_GT_Inventory`, the shift-A sync of D, per line |
| Masters | BOM `CTP_Master_BOM_v2`, routing `CTP_Master_Routing`, `CTP_Master_FRC`, ageing, buffer, MPQ, changeover, machines, allowable builders, holidays, downtime |

**Outputs:** `outputs/v46k_db_<D>/`, everything the v46k run wrote, plus:
- the shift-split floor schedule;
- `analysis/`: day-1 machine starts, shift load balance, built-not-cured, extruder idle;
- **the complete schedule written to `JK_CTP_PPDS.CTP_Schedule_Floor`** (upstream + building + curing, one row per lot piece per shift) under the run's `schedule_id` `YYYYMMDD_HHMMSS_v46k` (the table stores its timestamp), then read back and checked; details in `db_persist/`;
- **phase-wise test reports** of the run in `phased-testing/reports/<schedule_id>/` (7 phases, every check evaluated; see `phased-testing/README.md`);
- `RUN_SUMMARY.md`: **start here**.

**Docs:**
- every step has its own page: **[docs/steps/00_OVERVIEW.md](docs/steps/00_OVERVIEW.md)**;
- how to run it on a new machine: **[RUN_STEP_BY_STEP.md](RUN_STEP_BY_STEP.md)**.

| argument | meaning |
|---|---|
| `--date` | plant day `YYYY-MM-DD`. Default: the **current plant day**. A plant day runs **06:30 → 06:29 next morning**, so a run from 06:30 on 28 Oct to 06:29 on 29 Oct is for 28 Oct: its plans, its 06:30 stock, its 06:30 GT |
| `--run` | run name (default `v46k_db_<day>`) |
| `--plan-run-id` | pin the planner run, e.g. `PCR=20261028_162333,TBR=20261028_163653` |
| `--wip-sync`, `--gt-sync-pcr`, `--gt-sync-tbr` | use an explicit snapshot (`updated_at`) when the day's shift-A sync is missing or incomplete; recorded in the summary |
| `--min-snapshot-ratio` | a snapshot below this share of the usual row count is refused (default 0.5) |
| `--restage` | read the masters from the database again |
| `--skip-workbooks`, `--skip-analysis` | leave those steps out (faster) |
| `--check-only` | run only the pre-flight checks (are the day's tables updated?) and stop |
| `--no-db-write` | prepare and check the `CTP_Schedule_Floor` rows, write nothing |
| `--test-history-plan` | **test only**: plan from `CTP_Schedule_*Plan_History`; needs `--plan-run-id` for both lines |
| `--rules`, `--out` | rule file (default `config/rules.yaml`, v46k), output root |

The engine is v46k, unchanged. On the 27 Oct plan, v46k cured **60,700 of 74,422 eligible tyres (81.6 %)**; see `docs/REPO_VERIFICATION.md`. The database plan is put into exactly the columns the engine read from the Excel plans; a test checks this on the 27 Oct plan.

**What v46f–v46k add over v46e** (details in `docs/v38_corrections/CHANGE_LOG.md`):

| version | change |
|---|---|
| v46f | CAP 66 is made on 4-roll calender 901 (corrected routing / BOM v2); its three assumptions are removed |
| v46g | 901 campaign pick and hold-break judged by when the lot's trolleys need it, not a stale LFT |
| v46h | CAP 66 stock converted to mother roll by its MHE code: FAB ×1, STL ×0.1 (**assumed** 10 m slitted = 1 m), cap strip ×0.00647 (master BOM) |
| v46i | new plan (27–31 Oct), stock and green tyres of the plan day 06:30; the stock split also reads the pre-v2 BOM, so cut stock is kept |
| v46k | TBR chafer SNCT001–003 and BCFT002 cut stock converted to BRTTL (width from the BOM). SNCT001 / SNCT003 pre-cut rolls taken as **alternatives**: an open issue for the plant. Cut stock goes to the line that cuts it |

**Reports added:** `WIP_PRESENCE_VS_CONSUMPTION_<run>.xlsx` (stock present vs used, cut / slit conversion check, CAP 66) and `DAY1_WIP_VS_BUILDING_<run>.xlsx` (every day-1 trolley: kit from stock, parts made fresh, how long each holds the trolley).

**Exit code:** 0 when the run completed and every routing-validation check passed, 1 otherwise; 2 when a pre-flight check failed (nothing ran); 3 when a required step failed; 4 when the schedule was made but writing it to `CTP_Schedule_Floor` (or its read-back check) failed. On the reference data it exits 1 because of two known machine-coverage findings: the bypassed cutters and slitters are idle by design. The schedule checks in `VALIDATION_REPORT.xlsx` all pass.

## Repository layout

```
run_pipeline.py          the one command (plan + stock + masters from the DB -> schedule -> reports -> analysis)
pipeline/                preflight.py, plan_from_db.py, stock_snapshot.py, shift_production.py, persist_floor.py, db.py
analysis/                day-1 starts, shift load balance (2), built-not-cured
docs/steps/              one page per pipeline step
RUN_STEP_BY_STEP.md      how to run it on a new machine
db.env.example           template for the read-only database login (copy to db.env, git-ignored)
engine/                  the scheduler (flat Python modules, run as scripts)
  run_all.py               phases 0-7 for both lines
  phase0_validate.py       what the masters can build; SKU state CLEAN / ASSUMED / DATA_BLOCKED
  phase1_building_demand.py building plan -> component demand (BOM walk)
  phase2_wip_netting.py    opening stock netted per batch (FEFO, cascade, cut stock)
  phase3_lot_sizing.py     runs and lots (MPQ, whole batches, campaign lots on 901)
  phase4_cpm.py            precedence graph + backward schedule
  ctp_campaign.py, ctp_aligned_needby.py   campaign blocks, re-aimed need-by
  phase4b_assignment.py    machine assignment (bead-pool balance)
  phase5_placement.py      upstream dispatch + building placement (plan order, kit gate)
  phase6_floor_schedule.py floor schedule
  phase7_curing.py         curing gated on green tyres (plan press order, mould changes)
  mix_planner.py           mixing plan (MIXING_RULES_README)
  ctpio.py, rules.py, upstream_rules.py, blockers.py, blocker_policy.py   shared code
  make_*.py, validate_*.py, br_compliance_v15.py, mixing_compliance.py   reports
  make_dispatch_log_workbook.py, make_wip_usage_report.py                 diagnostics
  make_bypass_report.py    cut stock -> mother roll, transfer-time checks (pipeline step 10)
  make_wip_presence_report.py, make_day1_wip_building_report.py   stock used / day-1 kits (steps 11, 12)
  stage_wip_ppds.py, stage_wip_v6.py   compound Batches -> KG; stock split PCR / TBR (step S)
  ctpsql.py, refresh_ctp_sql.py, stage_*.py   re-stage master data from the database
config/rules.yaml        the full v46k rule set in one file (resolved)
config/history/          the original 49-file inherit_from chain it was resolved from
reference/               plant business-rules workbook, machine-name helpers, masters/ (plant machine register,
                         purchased-items list: not in the database)
plans/2026-10-27/        the 27-31 Oct 2026 plan as Excel (v46k reference; used by the plan-format test)
plans/october_2026/      the 22-31 Oct plan (v45b / v46e reference)
data/                    masters staged per plan day (git-ignored; see data/README.md)
docs/                    what was wrong and what each version changed (start with
                         docs/v38_corrections/CHANGE_LOG.md and AUDIT_ISSUES_STATUS_v46e.md)
tools/compare_runs.py    compare two run folders file by file
outputs/                 run results (git-ignored)
```

## What the engine does

* **Data-blocker policy.** SKUs whose master data is missing with no safe stand-in (e.g. no BOM) are excluded from the figures and listed. Gaps that can be filled are filled with **stated assumptions** (A01–A13), all listed in `VALIDATION_REPORT.xlsx`.
* **Builders and presses keep the plan's machine and sequence**; only times shift (check G8). Builder setups follow the actual previous trolley (G1b). Mould changes on presses take the plan's changeover time (G3).
* **Opening stock** is used per batch. Stock is "rested" by each demand's own use time. Stock on bypassed cutter/slitter codes is counted.
* **Upstream dispatch.** A machine chooses its next lot only when it is free. Calender 901 and the TBR bead machines queue lots by the trolley they serve. 901 runs campaign lots (24 h buckets) and releases rolls as it produces them.
* **Checks on every run** (`VALIDATION_REPORT.xlsx`, sheet "Schedule checks"): G1 / G1u no overlaps on builders / upstream machines, G1b builder setups, G2 durations, G3 mould changes, G4 unique lot IDs, G6 blocked demand excluded, G7 assumptions sourced, G8 plan sequence, G12 green-tyre wait. A failing fatal check stops the run.

## Checking a run against a reference

```bash
python tools/compare_runs.py <reference run folder> outputs/v46k_db_<day>
```
`docs/REPO_VERIFICATION.md` records the v46k comparison (plan-folder version).

## Configuration

All engine settings live in `config/rules.yaml`. The ones added in v39–v46k are explained in `docs/v38_corrections/CHANGE_LOG.md`, `TBR_APEXED_BEAD.md`, `FOUR_ROLL_CALENDER_901.md` and `PCR_COLD_START.md`. The engine reads its locations from environment variables, which `run_pipeline.py` sets: `CTP_DATA`, `CTP_STAGE`, `CTP_PLAN_DIR`, `CTP_RULES`, `CTP_OUT`.

## Not in this repository
* Master data and opening-stock snapshots (plant-confidential, about 60 MB per snapshot). `run_pipeline.py` stages them per plan day.
* Database credentials. They come only from environment variables (`CTP_DB_*`) or a local `db.env`, which is git-ignored. `engine/ctpsql.py` has no defaults.
