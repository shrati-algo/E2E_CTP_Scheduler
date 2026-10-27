# CTP scheduler (v46k engine)

End-to-end production scheduler for the CTP plant (PCR + TBR).
- **Input:** the plant's **building and curing plans**.
- **Read from the plant database:** the master data and the opening stock.
- **What it schedules:** every upstream lot, from mixing through calendering, extrusion and beads to building and curing. It keeps the plan's machine and sequence on builders and presses.
- **What it reports:** what can be built and cured, day by day.

**One command, only a plan folder:**

```bash
pip install -r requirements.txt
cp db.env.example db.env      # fill in the read-only database login (git-ignored)
python run_pipeline.py --plan-folder "C:/path/to/plan-folder"
```

Step-by-step guide: **[RUN_STEP_BY_STEP.md](RUN_STEP_BY_STEP.md)**.

**What it does on its own:**
- reads the plan start;
- takes the opening stock (`CTP_Opening_WIP`) and green tyres (`CTP_GT_Inventory`) from the **shift-A (06:30) sync of the plan's first day**;
- stages every master from `JK_CTP_PPDS`: BOM `CTP_Master_BOM_v2`, routing `CTP_Master_Routing`, `CTP_Master_FRC`, ageing, buffer, MPQ, changeover, machines, holidays, downtime;
- splits the stock PCR / TBR by the plan's own demand;
- schedules both lines and writes all reports to `outputs/v46k_<plan day>/`.

**Verified:** on the 27 Oct 2026 plan (`plans/2026-10-27/`) it reproduces run **v46k**: **60,700 of 74,422 eligible tyres cured (81.6%)**, PCR 51,020, TBR 9,680. Details: `docs/REPO_VERIFICATION.md`.

**What v46f–v46k add over v46e** (details in `docs/v38_corrections/CHANGE_LOG.md`):

| version | change |
|---|---|
| v46f | CAP 66 is made on 4-roll calender 901 (corrected routing / BOM v2); its three assumptions are removed |
| v46g | 901 campaign pick and hold-break judged by when the lot's trolleys need it, not a stale LFT |
| v46h | CAP 66 stock converted to mother roll by its MHE code: FAB ×1, STL ×0.1 (**assumed** 10 m slitted = 1 m), cap strip ×0.00647 (master BOM) |
| v46i | new plan (27–31 Oct), stock and green tyres of the plan day 06:30; the stock split also reads the pre-v2 BOM, so cut stock is kept |
| v46k | TBR chafer SNCT001–003 and BCFT002 cut stock converted to BRTTL (width from the BOM). SNCT001 / SNCT003 pre-cut rolls taken as **alternatives**: an open issue for the plant. Cut stock goes to the line that cuts it |

**Reports added:** `WIP_PRESENCE_VS_CONSUMPTION_<run>.xlsx` (stock present vs used, cut / slit conversion check, CAP 66) and `DAY1_WIP_VS_BUILDING_<run>.xlsx` (every day-1 trolley: kit from stock, parts made fresh, how long each holds the trolley).

| argument | meaning |
|---|---|
| `--plan-folder` | folder with the plan files, found by name: `*building*pcr*`, `*building*tbr*` (required), `*curing*pcr*`, `*curing*tbr*` (optional; derived if absent) |
| `--run` | run name (default `v46k_<plan day>`); outputs go to `<out>/<run>/` |
| `--restage` | read the masters from the database again (otherwise `data/ppds_<plan day>/` is reused) |
| `--data` | use an already staged masters folder instead of the database |
| `--rules` | rule file (default `config/rules.yaml`, the resolved v46k rules) |
| `--out` | output root (default `outputs/`) |
| `--skip-workbooks` | skip the two schedule workbooks (faster) |

**Exit code:** 0 when the run completed and every routing-validation check passed, 1 otherwise. On the reference data it exits 1 because of two known machine-coverage findings: the bypassed cutters and slitters are idle by design. The schedule checks in `VALIDATION_REPORT.xlsx` all pass.

## Repository layout

```
run_pipeline.py          the one command (plan folder -> masters + stock from the DB -> schedule -> reports)
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
plans/2026-10-27/        the 27-31 Oct 2026 plan (v46k reference)
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
python run_pipeline.py --plan-folder plans/2026-10-27
python tools/compare_runs.py <reference run folder> outputs/v46k_2026-10-27
```
`docs/REPO_VERIFICATION.md` records the v46k comparison (and the earlier v45b / v46e ones).

## Configuration

All engine settings live in `config/rules.yaml`. The ones added in v39–v46k are explained in `docs/v38_corrections/CHANGE_LOG.md`, `TBR_APEXED_BEAD.md`, `FOUR_ROLL_CALENDER_901.md` and `PCR_COLD_START.md`. The engine reads its locations from environment variables, which `run_pipeline.py` sets: `CTP_DATA`, `CTP_STAGE`, `CTP_PLAN_DIR`, `CTP_RULES`, `CTP_OUT`.

## Not in this repository
* Master data and opening-stock snapshots (plant-confidential, about 60 MB per snapshot). `run_pipeline.py` stages them per plan day.
* Database credentials. They come only from environment variables (`CTP_DB_*`) or a local `db.env`, which is git-ignored. `engine/ctpsql.py` has no defaults.
