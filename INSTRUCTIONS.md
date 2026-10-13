# Instructions — how to run and validate the CTP scheduler

This is the detailed guide. For a one-line start, see [README.md](README.md).

---

## 1. Prerequisites

- **Python 3.10 – 3.12**
- Install dependencies:
  ```bash
  pip install -r requirements.txt
  ```
  The core run needs `pandas numpy openpyxl pyyaml`. The `--db` refresh additionally needs
  `sqlalchemy pymysql`.
- **Windows:** use a UTF-8 terminal (the scripts set `PYTHONUTF8=1` for the phases automatically).

---

## 2. Run the whole scheduler

### A. Live database (default)
```bash
python run_scheduler.py
```
1. `db_pipeline/db_extract.py --extra` pulls all plant masters from `jkplanning_CTP` into `inputs/`
   (BOM, routing, itemtype, aging, MPQ, buffer, opening WIP, changeover matrix, **and** the extra
   tables incl. the per-TBM `building_changeover`).
2. `adapt_inputs.py` reshapes them into the v6 schema — the DB must flow **through** the adapter (a
   raw DB dump gives 106 d, not ~34).
3. the 9 phases run and save to `output/latest/` → a health note prints.

The **curing plan stays the JUNE shift sheet** (the DB's `CuringSchedule_pcr` is APRIL). To schedule
the DB's April plan on purpose:
```bash
python run_scheduler.py --db-plan
```
You need `db_pipeline/.env` with valid credentials (copy `db_pipeline/.env.example` → `.env`).

Live plant data drifts, so the makespan moves slightly run-to-run (e.g. 34.0–34.5 d as the BOM/routing
grow). That is expected — it is the current database answer, not a fixed number.

### B. Offline run (no database)
```bash
python run_scheduler.py --offline
```
Runs the 9 phases on the CSVs already in `inputs/` — the last DB pull, or the bundled snapshot if you
have never pulled. Use this air-gapped, or to reproduce the last pull exactly.

### Output location
```bash
python run_scheduler.py --out my_run_folder      # write somewhere else instead of output/latest
```

---

## 3. Run just part of the pipeline

The `--from` and `--only` flags pass straight through to `run_pipeline.py`. They **reuse** the earlier
phases' outputs in `outputs2/`, so run the full pipeline once first.

```bash
python run_scheduler.py --from phase5      # re-run phase5 + phase6 only (e.g. after a placement change)
python run_scheduler.py --only phase4      # re-run just the CPM
```

Phase keys: `phase0 phase1b phase1_5 phase1c phase2 phase3 phase4 phase5 phase6`.

Typical use: after editing a building/curing flag in `config.yaml`, `--from phase5` re-places and
re-cures without re-exploding demand.

---

## 4. Validate a run (testing suite)

The validators independently re-derive each phase and check the shipped output. They read
`testing/output/PHASEWISE/`, so point them at the latest run first with `--refresh`.

```bash
# validate EVERY phase of the latest run
python testing/run_tests.py --refresh

# validate ONE phase
python testing/run_tests.py --refresh --phase phase4

# list the phase keys
python testing/run_tests.py --list
```

- Reports (one `.xlsx` per phase) land in **`testing/reports/`**.
- A **PASS** means that phase's output is internally consistent. Some validators report **FAIL/WARN**
  rows that are **documented characteristics**, not defects — e.g. phase1c V10 (v6 over-net), phase1b
  differences from the SUM-dedup and WIP-fed prune, or phase6 cold-start aging. See
  [CHANGES_FROM_V6_WAVE_P2.md](CHANGES_FROM_V6_WAVE_P2.md) §B and "What is NOT a defect".
- The validators do **not** modify the schedule — they only check it.

---

## 5. Reading the results

| File | What |
|------|------|
| `output/latest/CTP_FLOOR_SCHEDULE.xlsx` | the floor schedule (4 sheets — see README) |
| `output/latest/phase5_floor_schedule_updated.csv` | the raw machine-level schedule (manufacturing + curing) |
| `output/latest/phase5_machine_utilization_updated.csv` | per-machine busy hours & utilisation |
| `output/latest/phase6_curing_revision_updated.csv` | the curing schedule (press, SKU, times) |
| `output/latest/PHASEWISE/…` | every phase's intermediate CSVs |
| `output/<RUN_NAME>/` | any earlier run kept with `--out output/<RUN_NAME>`, for diffing |

A **lot id** `GT 1765 ROYL_L0001` = item + lot index. On the floor, one lot may show as several
per-shift rows (a small "tail" row + the main row when it crosses a shift boundary) — that is a
per-shift view, not a small lot.

---

## 6. Changing inputs

- **A different curing plan (month):** replace `ctp_inputfiles/ctppcr_plan_shiftsheet.xlsx` (keep the
  sheet layout), then run `python run_scheduler.py --db` (which re-adapts) — or adapt manually and run.
- **A plant master (BOM, routing, aging, MPQ, changeover):** the authoritative source is the DB; use
  `--db`. To edit by hand for a what-if, change the file in `inputs/` and run the default mode.
- **A scheduler flag / lever:** edit `config.yaml` — every flag is documented inline with its measured
  effect — then `python run_scheduler.py --from <first-affected-phase>`.

---

## 7. The DB layer (`db_pipeline/`)

| File | Role |
|------|------|
| `.env` / `.env.example` | credentials for `jkplanning_CTP` |
| `db_connect.py` | SQLAlchemy engine + `read_table()` |
| `db_extract.py` | pulls tables → `inputs/` (+ a raw snapshot in `db_pipeline/raw/`) |
| `table_map.py` | which DB table feeds which input (CORE) and which exist but aren't wired (EXTRA) |
| `adapters.py` | reshape a raw DB table into the exact schema a phase expects |

Useful:
```bash
python db_pipeline/db_extract.py --list          # list all DB tables
python db_pipeline/db_extract.py --raw-only      # snapshot only; don't touch inputs/
python db_pipeline/db_extract.py --extra         # also pull the not-yet-wired tables
```
Building changeover (`Master_Building_ChangeoverTime_pcr`) is one of the `--extra` tables and is wired
into `inputs/building_changeover.csv` automatically.

---

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError: pandas/yaml/...` | `pip install -r requirements.txt` |
| `sqlalchemy/pymysql is required` | only for `--db`; `pip install sqlalchemy pymysql`, or use the default mode |
| DB connection error on `--db` | check `db_pipeline/.env` (host/user/password); the DB must be reachable from your network |
| Unicode / character errors on Windows | `set PYTHONUTF8=1` before running |
| A validator raises `FileNotFoundError` | run `python run_scheduler.py` first, then `python testing/run_tests.py --refresh` |
| Makespan differs from 34.02 d | you changed an input or a flag — check `config.yaml` and `inputs/`; the default bundle reproduces 34.02 d |
| Health check (`verify_run`) says BLOCKED | it depends on an optional validator set not bundled here; use `testing/run_tests.py` as the primary validation |

---

## 9. What "correct" means here

The plant owner's standing rule is **correctness over makespan** — 34–35 days is acceptable; the
schedule must be right. A run is correct when:
- every item's production ≥ its net demand (no shortfall),
- BOM is fully covered, routing (machine eligibility) is respected, no lot is below its MPQ,
- precedence holds (a green tyre is built before it is cured), and
- the residual issues are the **known, plant-side** ones (mould-copy-bound tail, cold-start aging) —
  not scheduler defects.

The latest run satisfies all of these. See [CHANGES_FROM_V6_WAVE_P2.md](CHANGES_FROM_V6_WAVE_P2.md) for
the full basis.
