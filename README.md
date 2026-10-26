# CTP scheduler (v46e engine)

End-to-end production scheduler for the CTP plant (PCR + TBR). Given the plant's **building and curing plans** and a snapshot of the **master data and opening stock**, it schedules every upstream lot, from mixing through calendering, extrusion, cutting and beads to building and curing. It keeps the plan's machine and sequence on builders and presses, and reports what can be built and cured, day by day.

This repository is the code of run **v46e** of the `pns-ctp-scheduler` project, restructured so it runs from one command. Running it on the October 2026 plan and the 22 Oct data snapshot (with `CTP_Master_BOM_v2` and `CTP_Master_FRC`) reproduces the v46e results: **139,418 of 161,670 eligible tyres cured (86.2%)**. See `docs/REPO_VERIFICATION.md`.

What v46e adds over v45b (details in `docs/v38_corrections/CHANGE_LOG.md`):

| version | change |
|---|---|
| v46a | opening stock already cut / slit is turned into the mother roll it was cut from; transfer time across the bypassed cutters = minimum ageing (checked) |
| v46b | TBR TDA winds and apexes in one in-line step, at the slower of the two speeds |
| v46c | BOM from `CTP_Master_BOM_v2` (cutters / slitters already removed); the codes in between come from the routing |
| v46d | the BOM walk counts each item's recipe once (fixes a 2x-5x over-count of compounds) |
| v46e | 4-roll calender lots in whole dips (`CTP_Master_FRC` Spool/Dip Length), each run covering up to the shelf window, partial dip when a full one would expire |

## Quick start

```bash
pip install -r requirements.txt

python run_pipeline.py \
    --plan-folder plans/october_2026 \
    --data data/ppds_2026-10-22_bomv2 \
    --run oct26
```

Results go to `outputs/oct26/`. Start with **`VALIDATION_REPORT.xlsx`**, then `CTP_oct26_SCHEDULE.xlsx` and `FULFILMENT_DAYWISE_v15.csv`. A full run takes about 12 minutes.

| argument | meaning |
|---|---|
| `--plan-folder` | folder with the plan files, found by name: `*building*pcr*`, `*building*tbr*` (required), `*curing*pcr*`, `*curing*tbr*` (optional; derived if absent). See `plans/README.md` |
| `--data` | staged master-data folder (PPDS CSV extracts), staged with `refresh_ctp_sql.py --ppds-only --bom-v2` (BOM v2, `reference_bom/`, `CTP_Master_FRC.csv`). Not in git; see `data/README.md` |
| `--run` | run name; outputs go to `<out>/<run>/` |
| `--rules` | rule file (default `config/rules.yaml`, the resolved v46e rules) |
| `--out` | output root (default `outputs/`) |
| `--skip-workbooks` | skip the two schedule workbooks (faster) |

Exit code 0 when the run completed and every routing-validation check passed, 1 otherwise. On the reference data it exits 1 because of two known machine-coverage findings (the bypassed cutters and slitters are idle by design). The schedule checks in `VALIDATION_REPORT.xlsx` all pass.

## Repository layout

```
run_pipeline.py          the one command (stages the plans, runs every step below)
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
  ctpsql.py, refresh_ctp_sql.py, stage_*.py   re-stage master data from the database
config/rules.yaml        the full v46e rule set in one file (resolved)
config/history/          the original 49-file inherit_from chain it was resolved from
reference/               plant business-rules workbook, machine-name helper files
plans/october_2026/      the plan used for the reference runs
data/                    staged master data goes here (git-ignored)
docs/                    what was wrong and what each version changed (start with
                         docs/v38_corrections/CHANGE_LOG.md and AUDIT_ISSUES_STATUS_v46e.md)
tools/compare_runs.py    compare two run folders file by file
outputs/                 run results (git-ignored)
```

## What the engine does (v45b)

* **Data-blocker policy.** SKUs whose master data is missing with no safe stand-in (e.g. no BOM) are excluded from the figures and listed. Gaps that can be filled are filled with **stated assumptions** (A01–A13), all listed in `VALIDATION_REPORT.xlsx`.
* **Builders and presses keep the plan's machine and sequence**; only times shift (check G8). Builder setups follow the actual previous trolley (G1b). Mould changes on presses take the plan's changeover time (G3).
* **Opening stock** is used per batch. Stock is "rested" by each demand's own use time. Stock on bypassed cutter/slitter codes is counted.
* **Upstream dispatch.** A machine chooses its next lot only when it is free. Calender 901 and the TBR bead machines queue lots by the trolley they serve. 901 runs campaign lots (24 h buckets) and releases rolls as it produces them.
* **Checks on every run** (`VALIDATION_REPORT.xlsx`, sheet "Schedule checks"): G1 / G1u no overlaps on builders / upstream machines, G1b builder setups, G2 durations, G3 mould changes, G4 unique lot IDs, G6 blocked demand excluded, G7 assumptions sourced, G8 plan sequence, G12 green-tyre wait. A failing fatal check stops the run.

## Reproducing v45b and checking the integration

```bash
python run_pipeline.py --plan-folder plans/october_2026 --data <data-ppds-v38 folder> --run v45b_repo
python tools/compare_runs.py <pns-ctp-scheduler>/outputs/e2e_v45b outputs/v45b_repo
```
`docs/REPO_VERIFICATION.md` records the comparison made when this repository was created.

## Configuration

All engine settings live in `config/rules.yaml`. The ones added in v39–v45 are explained in `docs/v38_corrections/CHANGE_LOG.md`, `TBR_APEXED_BEAD.md`, `FOUR_ROLL_CALENDER_901.md` and `PCR_COLD_START.md`. The engine reads its locations from environment variables, which `run_pipeline.py` sets: `CTP_DATA`, `CTP_STAGE`, `CTP_PLAN_DIR`, `CTP_RULES`, `CTP_OUT`.

## Not in this repository
* Master data and opening-stock snapshots (plant-confidential, about 60 MB per snapshot).
* Database credentials. `engine/ctpsql.py` reads them only from environment variables (`CTP_DB_*`); never commit a `.env`.
