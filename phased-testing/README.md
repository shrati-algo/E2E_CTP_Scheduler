# Phase-wise test reports, made on every run

Every `python run_pipeline.py` run ends with **step 13**: phase-wise test reports for **that run's schedule**. One folder is written per run, named by the run's `schedule_id`, the same ID the schedule carries in the database:

```
phased-testing/
├── README.md                  this file
├── phase_tests.py             the generator (run_pipeline.py step 13 calls it)
└── reports/
    └── <schedule_id>/         e.g. 20261028_191753_v46k
        ├── SUMMARY.md         one line per phase + overall verdict, failed checks, warnings
        ├── TEST_CASES_PHASE1.md   Demand Explosion
        ├── TEST_CASES_PHASE2.md   Lot Sizing
        ├── TEST_CASES_PHASE3.md   DAG Construction
        ├── TEST_CASES_PHASE4.md   CPM Backward Pass
        ├── TEST_CASES_PHASE5.md   Forward Placement (JIT)
        ├── TEST_CASES_PHASE6.md   Curing Revision
        ├── TEST_CASES_PHASE7.md   Schedule Validation
        ├── checks.csv         every check, machine-readable
        └── result.json        counts per phase, overall verdict
```

**Finding a run.** The folder name is the run's `schedule_id` (`YYYYMMDD_HHMMSS_<version>`). The same run is in the database at `JK_CTP_PPDS.CTP_Schedule_Floor WHERE schedule_id = '<YYYY-MM-DD HH:MM:SS>'`, which is the timestamp part of the ID. The run's own `outputs/<run>/SCHEDULE_ID.json` holds both forms.

## Format

The reports follow `test-report-phasewise/TEST_CASES_PHASE<n>.md`:
- the title `# Test Cases — Phase n: <name>`;
- `## Scope:`, then a one-line `>` intent;
- numbered sections (`## 1. Input Loading Tests`, `### 1.1 …`) of check-boxes;
- a spot-check / verification section;
- `## Acceptance Criteria`, closed by `*End of Phase n test cases.*`.

**The difference.** The reference files are blank checklists. Here **every check is evaluated on the run**, and each line carries its evidence:

| Line | Meaning |
|---|---|
| `- [x] check — evidence` | passed |
| `- [ ] **FAIL** check — evidence` | a rule is broken |
| `- [ ] **WARN** check — evidence` | allowed, but someone should look |
| `- [x] *INFO* check — evidence` | a measured fact (counts, totals) |
| `- [ ] **ERROR** check — message` | the check could not be evaluated. It is counted as a failure, never silently passed |

Every report starts with the run's identity: `schedule_id`, run folder, plan day, the planner runs used, the stock and GT snapshots, when it was tested, and the counts. The acceptance criteria are ticked ✅ / ❌ from the checks. "Tech head sign-off" stays a manual ⬜.

## What each phase checks (this engine)

| Report | Reference name | Engine step | Main checks |
|---|---|---|---|
| 1 | Demand Explosion | `phase0_validate`, `phase1_building_demand` | Plan fully resolved (machine, time, GT code). Data-blocked SKUs listed. Trolleys = effective plan. Every trolley has demand. GT is a BOM child of the SKU. `demand = per_tyre × qty`. No duplicate (trolley, item). One unit per item. `need_by = start − min_ageing − buffer`. Every item routed. Plausible kg of final compound per tyre |
| 2 | Lot Sizing | `phase2_wip_netting`, `phase3_lot_sizing` | Stock snapshot = the plan day's 06:30. `net = required − stock`. `cascade = demand − required`. No expired stock allocated. Unique lot ids, qty > 0. MPQ min / max. Whole batches in mixing. Lot ≤ one shift. Every net demand assigned to a lot, totals equal |
| 3 | DAG Construction | `phase4_cpm` (edges) | Edges join lots of this run. No self / duplicate edges, qty > 0. Edges follow the BOM (bypass bridges reported). No cycles. Nothing downstream feeds mixing. Chain depth |
| 4 | CPM Backward Pass | `phase4_cpm` (times) | Every lot timed, no NaN. `LST = LFT − duration`. `slack = LST − EST`. Producer LFT leaves the consumer time. Critical and infeasible lots reported |
| 5 | Forward Placement | `phase4b_assignment`, `phase5_placement` | Every lot placed, none before 07:00 of the plan day. Routing-allowed machine. No overlap on any machine (shared machines across lines, changeover included). Producer + min ageing before the consumer. Max ageing. Kit gate. Trolleys keep plan machine and order. No trolley overlap |
| 6 | Curing Revision | `phase7_curing` | One press lot per plan row. Forward only. After green tyres. Plan press order. No press overlap. Even quantities, never above plan. `within_horizon` correct, overflow reported. Blocked SKUs outside the KPI |
| 7 | Schedule Validation | floor schedule, `validate_floor_schedule`, `make_validation_report`, `persist_floor` | Every floor row inside one shift; shift pieces add up to the lot. The routing checks of `VALIDATION.csv`. Every schedule gate (G1–G12). Fulfilment. **The database copy**: written under this `schedule_id` and identical on read-back |

A phase is **OK** when it has no FAIL or ERROR and all its acceptance criteria hold. WARN and INFO never block. The overall verdict is in `SUMMARY.md` and `result.json`. It is also printed by `run_pipeline.py` and added to the run's `RUN_SUMMARY.md`.

## Run it again for a finished run

```bash
python phased-testing/phase_tests.py --run <run folder name>          # e.g. v46k_test_27oct
python phased-testing/phase_tests.py --run <run> --out <output root>   # if outputs are elsewhere
```

It only **reads** the run's outputs (`outputs/<run>/`), its staged masters (`data/ppds_<day>/`) and `db_persist/`. It does not query the database. A new report for the same `schedule_id` overwrites that folder.

**Exit code:** 0 when every phase is OK, 1 otherwise. In `run_pipeline.py` this step is **optional**: a failing phase is reported, but it never stops a run whose schedule was made.

**In git.** `phased-testing/reports/` is git-ignored, like `outputs/`, because it is run output with plant data. The generator and this README are tracked.
