# CTP Chennai — End-to-End Production Scheduler (v2)

A self-contained, deterministic **finite-capacity production scheduler** for JK Tyre's
**CTP Chennai** plant (PCR radial, TBR, or a combined PCR+TBR run).

It takes the plant's **curing plan as the demand drum** and produces a fully sequenced,
**machine-level floor schedule** covering the entire chain —

```
mixing → calendering / extrusion → cutting & slitting → bead / belt / ply prep → building (TBM) → curing (press)
```

— while honouring BOM, routing, MPQ (minimum production quantity), machine eligibility,
changeover times, opening WIP, and **component shelf life (aging)**.

The scheduling engine is a port of the `v6_wave_p2` wave scheduler onto CTP data, with a
registry of deliberate, documented deviations
(see [CHANGES_FROM_V6_WAVE_P2.md](CHANGES_FROM_V6_WAVE_P2.md)).

> **Design principle enforced throughout this repo: correctness beats makespan.**
> A schedule that is one day shorter but physically unexecutable is a defeat. Every
> tuning flag in `config.yaml` records its *measured* effect, not its intended one.

---

## Table of contents

1. [What it produces](#1-what-it-produces)
2. [Quick start](#2-quick-start)
3. [Requirements](#3-requirements)
4. [Configuration — `.env` and `config.yaml`](#4-configuration--env-and-configyaml)
5. [How the pipeline works — the 9 phases](#5-how-the-pipeline-works--the-9-phases)
6. [Inputs](#6-inputs)
7. [Outputs](#7-outputs)
8. [Validation](#8-validation)
9. [Verified run](#9-verified-run)
10. [Repository layout](#10-repository-layout)
11. [Running modes and flags](#11-running-modes-and-flags)
12. [Troubleshooting](#12-troubleshooting)
13. [Documentation index](#13-documentation-index)
14. [Data confidentiality](#14-data-confidentiality)

---

## 1. What it produces

The deliverable is **`output/latest/CTP_FLOOR_SCHEDULE.xlsx`**, a four-sheet workbook:

| Sheet | Contents |
|---|---|
| **Summary** | Row counts and the date span of each sheet |
| **Floor Schedule** | Every manufacturing operation + real curing: date, machine, shift, item, item type, process, department, start/end time, quantity, UOM, lot id, FG SKU |
| **Inventory GT Curing** | Cures fed by opening-WIP green tyres (cured before their own build exists) |
| **WIP Curing** | Cures whose green tyre was never built in this horizon |

Alongside it, `output/latest/PHASEWISE/` holds one folder per phase with every
intermediate CSV — the audit trail, and what the validation suite checks.

---

## 2. Quick start

```bash
# 1. clone and install
git clone <your-repo-url>
cd ctp_endtoend_scheduler_v2
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell:  .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. run — offline, on the input CSVs bundled in inputs/  (NO database needed)
python run_scheduler.py --offline

# 3. results
#    output/latest/CTP_FLOOR_SCHEDULE.xlsx
#    output/latest/PHASEWISE/

# 4. (optional) validate every phase of that run
python testing/run_tests.py --refresh
```

A full offline run takes roughly **13 minutes** on a normal laptop and needs about
**4 GB of free RAM** (phase 5 holds the whole placement in memory).

To run against the **live plant database** instead, fill in `db_pipeline/.env`
(see [§4](#4-configuration--env-and-configyaml)) and drop the flag:

```bash
python run_scheduler.py
```

> **Windows:** run in a UTF-8 terminal. `run_scheduler.py` sets `PYTHONUTF8=1` for its
> child processes automatically; if you invoke a phase directly and hit
> `UnicodeEncodeError`, set it yourself first.

---

## 3. Requirements

**Python 3.10 or newer.**

```bash
pip install -r requirements.txt              # core + database driver
pip install -r requirements-dev.txt          # optional: validation suite, MES tooling
```

| Package | Floor | Needed for |
|---|---|---|
| `pandas` | ≥1.5 | every phase — the pipeline is DataFrame-based |
| `numpy` | ≥1.23 | numeric kernels in phases 2, 4, 5 |
| `openpyxl` | ≥3.1 | reading `.xlsx` plant masters, writing the floor-schedule workbook |
| `PyYAML` | ≥6.0 | `config.yaml` (each phase loads it independently) |
| `SQLAlchemy` | ≥2.0 | **live DB mode only** |
| `PyMySQL` | ≥1.0 | **live DB mode only** — MySQL driver |
| `networkx` | ≥3.0 | *optional* — phase-3 validator cycle detection (falls back to manual DFS) |
| `pyarrow` | ≥14.0 | *optional* — `planning_engine/` MES parquet cache only |

**Verified environment:** a complete offline run was executed on
**Python 3.14.3 · pandas 3.0.2 · numpy 2.4.4 · openpyxl 3.1.5 · PyYAML 6.0.3**
(Windows 11). The pipeline is also known to run on Python 3.10–3.12 with pandas 2.x.

---

## 4. Configuration — `.env` and `config.yaml`

There are two configuration surfaces, and they do different jobs.

### 4.1 `.env` — credentials and run-time switches

**[`.env.example`](.env.example) at the repo root documents every environment variable
the code reads.** Copy it and fill in what you need.

Two mechanisms, and the distinction matters:

**(a) Database credentials** are read from a dedicated file, **`db_pipeline/.env`**,
loaded by `db_pipeline/db_connect.py`. Real environment variables of the same name
override the file, so CI or cron can inject them without touching disk.

```bash
cp .env.example db_pipeline/.env      # Windows:  copy .env.example db_pipeline\.env
# then edit db_pipeline/.env:
#   DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME
```

`db_pipeline/.env` is **git-ignored**. Never commit it.

**(b) Everything else** is a plain environment variable read directly by the phases.
Nothing auto-loads a file for these — export them in your shell:

```bash
export CTP_PRODUCT=tbr        # bash / git-bash
$env:CTP_PRODUCT = "tbr"      # PowerShell
set CTP_PRODUCT=tbr           # cmd.exe
```

| Variable | Default | Effect |
|---|---|---|
| `CTP_PRODUCT` | `pcr` | `pcr` or `tbr` — swaps every `*_pcr` master table for its `*_tbr` counterpart. For a **combined** run leave it unset and use the `*_comb` masters already set in `config.yaml`. |
| `CTP_BOM_GATE` | `strict` | phase1b BOM-identity gate. `strict` hard-fails on a reconciliation violation (use for production). `warn` logs and continues — **required for the combined PCR+TBR run**, whose TBR rows use a per-path rather than global-total BOM convention. |
| `DB_OFFLINE` | unset | `1` reads the pre-downloaded dumps in `db_pipeline/raw/` instead of the live server. Also applied automatically as a fallback when a live read fails. |
| `JK_INCH_AWARE` | `1` | Match green-tyre rim inch to building-machine inch range. `0` disables. |
| `JK_SOFT_INCH` | `1` | Inch match is a penalty, not a wall. `0` restores the hard gate. |
| `JK_UPSTREAM_LEAD_H` | `0` | Hours of head start for upstream component work. Raising it front-loads components **and ages them**. |
| `JK_RESYNC_CHAIN` | `0` | Enable the phase-5 short-shelf chain re-sync sweep (also a `config.yaml` key; the env var exists for A/B runs). |
| `JK_RESYNC_SWEEPS` | `40` | Max sweeps for the above. |
| `PHASE3_WRITE_XLSX` | `0` | `1` makes phase 3 also emit its DAG as `.xlsx` (slow, large). |
| `CTP_MES_DIR` | unset | MES export directory for `planning_engine/` only — never used by the scheduler. |

### 4.2 `config.yaml` — the scheduling policy

`config.yaml` (~120 KB, heavily commented inline) holds every algorithmic setting: file
names, lot-sizing caps, aging policy, placement levers, curing policy. Each flag carries
its **measured** effect in a comment beside it.

Two blocks to read before changing anything:

- The **DEVIATION TABLE** at the top of the file — every key whose shipped value differs
  from v6's behaviour, with the exact values needed to reproduce v6 exactly.
- The **`files:` / `ctp_files:`** blocks (around line 546) — which adapted CSV and which
  source plant master each logical input maps to.

Note that **each phase opens `config.yaml` itself** as a separate subprocess. There is no
CLI override for these values; editing the file is the only mechanism, deliberately so.

---

## 5. How the pipeline works — the 9 phases

This is a **drum-buffer-rope** design: the **curing plan is the drum** (fixed demand),
building paces to it, and components are roped just-in-time behind building.

```
phase0 → phase1b → phase1_5 → phase1c → phase2 → phase3 → phase4 → phase5 → phase6
validate  explode    waves     netting    lots     DAG      CPM     place    cure
```

```
plan.csv (curing drum)  ─┐
bom.csv, routing.csv    ─┼─► demand ─► waves ─► net demand ─► LOTS ─► DAG ─► timed DAG ─► FLOOR ─► cured floor
inventory.csv (WIP)     ─┘    1b       1_5        1c           2       3       4 (CPM)      5         6
aging / mpq / changeover ──────────────────────── used across 2, 4, 5, 6 ───────────────────────────►
```

| Phase | Does | Key output |
|---|---|---|
| **phase0** | Validates every input master — missing columns, blank cells (the "present-but-blank NaN trap"), broken references. Report only. | `phase0_data_validation.xlsx` |
| **phase1b** | **Demand explosion** — turns the curing plan into per-component demand by walking the BOM. | `phase1_demand_updated.csv.gz` |
| **phase1_5** | **Wave builder** — buckets demand into ~3-day time waves for lot sizing and sequencing. | `phase1_5_waves.csv` |
| **phase1c** | **MRP netting** — subtracts opening WIP so the schedule builds only what is missing. | `phase1_demand_NET_updated.csv.gz` |
| **phase2** | **Lot sizing** — net demand becomes real production lots, sized by MPQ, duration caps and aging windows. | `phase2_lots_updated.csv` |
| **phase3** | **DAG construction** — connects each producer lot to the consumer lots it feeds. | `phase3_dag_edges_updated.csv` |
| **phase4** | **CPM** — forward pass gives EST/EFT, backward pass from curing deadlines gives LST/LFT, slack = LST − EST. Producer→consumer lag = `min_aging` + routing transfer time. | `phase4_lot_times_updated.csv` |
| **phase5** | **Forward placement** — every lot onto a specific machine at a specific time: LPT cap-and-spill packing, campaign grouping, changeovers, material-readiness (kit) gate. Orphans are placed late, **never deleted**. | `phase5_floor_schedule_updated.csv` |
| **phase6** | **Curing revision** — the curing schedule as a faithful echo of the plan's press assignments, slid to when each green tyre is actually ready. **Never moves a mould.** | `phase6_curing_revision_updated.csv` |

Full detail — inputs, outputs, and the CTP-specific behaviour of each phase — is in
**[PHASEWISE_GUIDE.md](PHASEWISE_GUIDE.md)**.

After the phases, three report-only steps run:
`plan_reconciliation.py` (build-vs-cure balance), `capacity_wall.py` (names any single
upstream machine whose total work exceeds the plan window — a hard makespan floor that
sequencing cannot beat), and `verify_run.py` (health verdict).

---

## 6. Inputs

### 6.1 `inputs/` — what the phases actually read

These are the **adapted** CSVs, in the schema the phases expect. They are produced by
`adapt_inputs.py` from the source masters (or from a live DB pull), and are committed so
that `--offline` runs reproducibly straight after a clone.

| File | What it is |
|---|---|
| `plan.csv` | **The drum** — the curing plan (press, SKU, quantity, slot) |
| `building_plan.csv` | The plant's building schedule, echoed by phase 5 |
| `bom.csv` | Bill of materials, parent → child with per-tyre quantities |
| `routing.csv` | Operation sequence, machine eligibility, rates, transfer times, efficiency |
| `itemtype_master.csv` | Item → item-type classification |
| `aging_master.csv` | Per-item **min / max shelf life** — the aging constraint |
| `mpq.csv`, `mixing_mpq.csv` | Minimum production quantities (piece floors, Banbury batch sizes) |
| `buffer_master.csv` | Buffer sizing by item type |
| `inventory.csv` | **Opening WIP** — the t=0 stock snapshot on the floor |
| `changeover_matrix.csv`, `changeover_lookup.csv`, `building_changeover.csv` | Setup times, including the real per-TBM building setup values |

`inputs/` also holds several `inventory_*.csv` and `warm_wip_*.csv` variants — saved
opening-WIP scenarios from past experiments (cold start, warm start, kanban seeding).
Point `config.yaml`'s `inventory:` key at one to reproduce that scenario.

### 6.2 `ctp_inputfiles/` — the source plant masters

The original plant workbooks and DB-table exports that `adapt_inputs.py` reads
(`bom_comb.csv`, `routing_comb.csv`, `curing_comb.xlsx`, `building_comb.xlsx`,
`aging_comb.csv`, `jkt_mpq_corrected.xlsx`, `opening_wip.csv`, the changeover matrix,
and the plant aging spec PDF). The mapping from logical name to file is the
`ctp_files:` block in `config.yaml`.

### 6.3 `db_pipeline/` — the live database layer

`db_connect.py` (one connection point, credentials from `.env` only — nothing hard-codes a
password), `db_extract.py` (pulls tables), `table_map.py` (table → logical input),
`adapters.py`. Extracted tables are cached in `db_pipeline/raw/` and used automatically as
a fallback if the live server is unreachable. See
[`db_pipeline/README.md`](db_pipeline/README.md).

---

## 7. Outputs

```
output/latest/
├── CTP_FLOOR_SCHEDULE.xlsx              ← THE DELIVERABLE (4 sheets)
├── phase5_floor_schedule_updated.csv    ← the same floor schedule as raw CSV
├── phase5_schedule_updated.csv
├── phase5_machine_utilization_updated.csv
├── phase5_summary_updated.xlsx
├── phase6_curing_revision_updated.csv
├── phase4_aging_violations_updated.csv
├── phase2_lots_updated.csv
├── opening_wip_used.csv                 ← exactly which WIP snapshot this run consumed
└── PHASEWISE/
    ├── 00_INPUTS/          ← a copy of every input CSV used
    ├── PHASE1B_DEMAND/
    ├── PHASE1_5_WAVES/
    ├── PHASE1C_NETTING/
    ├── PHASE2_LOTS/
    ├── PHASE3_DAG/
    ├── PHASE4_CPM/
    ├── PHASE5_PLACEMENT/
    └── PHASE6_CURING/
```

`--out DIR` writes anywhere you like instead of `output/latest`.

**Reading a lot id:** `GT 1765 ROYL_L0001` is item + sequential lot index. On the floor a
single lot can appear as several **shift-rows** (a 463-unit lot crossing a shift boundary
shows a small tail row plus the main row) — that is a per-shift *view*, not a small lot.

**Machine utilisation:** use the `true_utilisation_%` column, not the legacy
`utilisation_%`. The legacy column divides busy time by its own span and therefore reads
near 100% for every busy machine; `true_utilisation_%` divides by the plan horizon.

> **Note:** run outputs are **git-ignored** (`output/`, `outputs/`, `outputs2/` total
> several GB). They are regenerated by every run.

---

## 8. Validation

```bash
python testing/run_tests.py --refresh     # validate every phase of the latest run
python testing/run_tests.py --list        # list the phase keys
python testing/run_tests.py --phase phase5
```

`testing/` holds one validator per phase (`validate_phase1b.py` … `validate_phase6.py`),
each running a numbered battery of checks — quantity conservation, BOM reconciliation,
MPQ floors, DAG acyclicity, CPM consistency, machine eligibility, no double-booking,
aging windows. Reports land in `testing/reports/` (git-ignored).

A separate, lighter health verdict runs automatically at the end of every scheduler run
(`verify_run.py`).

> **On checks that cannot fail:** before quoting any check as a safety guarantee, ask
> what input would make it *fail*. Several historically "green" checks in this project
> turned out to be structurally incapable of failing. See
> [FLAW_REGISTRY.md](FLAW_REGISTRY.md).

---

## 9. Verified run

A complete offline run of this repository, on the configuration as committed
(combined PCR + TBR masters, June 2026 curing plan):

```bash
python run_scheduler.py --offline
```

| | |
|---|---|
| Exit status | **0 — all 9 phases completed** |
| Wall-clock | ~13 minutes |
| Floor-schedule rows | 31,446 |
| Schedule span | 2026-05-30 07:00 → 2026-07-13 00:30 (**43.73 days**, including the pre-plan runway) |
| Green tyres built | 448,401 |
| Tyres cured | 452,096 |
| Opening-WIP green tyres consumed | 4,934 |
| Build/cure balance | 55 SKUs balanced, 20 short (1,217 tyres), 37 surplus (2,456 tyres) |

**Capacity wall reported by the same run** — the hard floor sequencing cannot beat:

| Machine | Component | Work | Alternates | Verdict |
|---|---|---|---|---|
| `2001` | PRE CUT ROLL MATERIAL | 34.1 d | 0 | **HARD WALL** — no alternate machine in routing |
| `1802` | Steel Belt Edge Strip | 30.5 d | 0 | **HARD WALL** — no alternate machine in routing |

So the makespan floor for this input set is ≈34.1 days plus the downstream chain. Getting
below it requires the plant to add a parallel machine, not a scheduling change. This is
the honest number: shorter figures elsewhere in the history (e.g. the 34.02-day PCR-only
run) were measured on a **PCR-only** configuration, not this combined one.

**Clone reproducibility.** The same run was then repeated in a clean directory containing
*only* the files this repository actually commits — no `outputs2/`, no `.env`, no ignored
file present. It again exited 0 and produced an identical schedule (31,446 rows,
2026-05-30 → 2026-07-13, 43.73 days). A fresh clone therefore runs and produces output
with nothing beyond `pip install -r requirements.txt`.

---

## 10. Repository layout

```
ctp_endtoend_scheduler_v2/
├── run_scheduler.py             ← MAIN ENTRY POINT
├── run_pipeline.py              ← phase orchestrator (subprocess per phase)
├── adapt_inputs.py              ← plant masters / DB tables → the inputs/ schema
├── config.yaml                  ← all scheduling policy, documented inline
├── requirements.txt             ← core dependencies
├── requirements-dev.txt         ← optional extras
├── .env.example                 ← EVERY environment variable, documented
├── .gitignore
│
├── phases/                      ← the 9 phase programs
│   ├── phase0_validate_inputs.py
│   ├── phase1b_demand_explosion.py
│   ├── phase1_5_wave_builder.py
│   ├── phase1c_mrp_netting.py
│   ├── phase2_lot_sizing.py
│   ├── phase3_dag_construction.py
│   ├── phase3_resolve_lot_blocks.py
│   ├── phase4_cpm.py
│   ├── phase5_forward_placement_v2.py
│   ├── phase6_curing_revision.py
│   └── db_loader.py
│
├── inputs/                      ← adapted CSVs the phases read
├── ctp_inputfiles/              ← source plant masters
├── db_pipeline/                 ← live jkplanning_CTP layer (+ .env.example)
├── testing/                     ← per-phase validators + run_tests.py
├── planning_engine/             ← separate MES reverse-engineering tool (not the scheduler)
├── knowledge/
│   ├── memory/                  ← 63 decision notes: every measured experiment and why
│   └── docs/                    ← CHANGELOG, PHASE_CORRECTIONS, DB pipeline readme
│
├── verify_run.py                ← end-of-run health verdict
├── capacity_wall.py             ← names the hard upstream machine bottleneck
├── plan_reconciliation.py       ← build-vs-cure balance report
├── audit_schedule.py            ← standalone schedule auditor
├── apply_aging_spec.py          ← overlay the plant aging spec onto aging_master
├── apply_local_mpq.py           ← overlay the local MPQ workbook onto DB MPQ
├── build_warm_wip*.py           ← construct warm-start WIP scenarios
│
├── README.md                    ← you are here
├── INSTRUCTIONS.md              ← detailed how-to: all run modes, troubleshooting
├── PHASEWISE_GUIDE.md           ← each phase in full
├── BUSINESS_RULES.md            ← plant-confirmed rules (BR-1 … BR-4)
├── CHANGES_FROM_V6_WAVE_P2.md   ← the deviation registry
├── FLAW_REGISTRY.md             ← known defects, severity, status
└── PHASE6_CURING_TAIL_BUG.md    ← one deep-dive post-mortem
```

---

## 11. Running modes and flags

```bash
python run_scheduler.py                    # DEFAULT: pull masters from the live DB, then run
python run_scheduler.py --offline          # run on the CSVs already in inputs/ (no DB)
python run_scheduler.py --db-plan          # also pull the DB's curing plan (see note below)
python run_scheduler.py --out DIR          # write deliverables to DIR
python run_scheduler.py --validate-only    # skip the run, re-verify the last output
python run_scheduler.py --from phase4      # re-run from a phase onward, reusing outputs2/
python run_scheduler.py --only phase5      # run a single phase
python run_scheduler.py --only phase5,phase6
```

`--from` and `--only` are passed straight through to `run_pipeline.py`, which can also be
invoked on its own. Intermediate state lives in `outputs2/` between phases, so `--from`
is only meaningful when a previous run left that directory populated.

**About `--db-plan`:** in the default DB run the masters (BOM, routing, aging, MPQ,
changeover, opening WIP) come from the database, but the **demand drum stays the local
curing plan**. This is deliberate — the database's `CuringSchedule_pcr` table holds a
*different month* from the bundled plan. Pass `--db-plan` only when you intend to
schedule the database's month instead.

Two overlays also run in DB mode by plant directive: `apply_aging_spec.py` (the plant
aging spec supersedes DB aging) and `apply_local_mpq.py` (the corrected local MPQ
workbook supersedes DB MPQ).

---

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Database credentials incomplete: missing [...]` | `db_pipeline/.env` is absent or blank. Either fill it in, or run `--offline`. |
| `live DB unreachable -> using downloaded raw/<table>.csv` | Informational. The live server was unreachable and the cached dump in `db_pipeline/raw/` was used instead. |
| `UnicodeEncodeError` on Windows | Set `PYTHONUTF8=1` before running a phase directly. `run_scheduler.py` already does this for its children. |
| `... load skipped ([Errno 2] No such file ...)` | Benign, and expected — see "Optional files that are absent" below. |
| phase1b fails on a BOM identity violation | Expected under `CTP_BOM_GATE=strict` for a combined PCR+TBR run. Set `CTP_BOM_GATE=warn` — the TBR rows use a per-path BOM convention. |
| `--from phaseX` fails or produces nothing | `outputs2/` has no prior state, or it is stale. Run the full pipeline once. `--force` overrides a stale-upstream guard. |
| Health check "skipped in the portable package" | Expected. The legacy phase-0..7 report validators are not bundled; `testing/run_tests.py` is the primary validation. |
| Makespan longer than expected | Run `python capacity_wall.py`. If it names a HARD WALL machine, no amount of sequencing will beat it — that machine needs a parallel unit. |
| `MemoryError` in phase 5 | Phase 5 holds the full placement in memory. Close other applications; ~4 GB free is the practical minimum. |

### Optional files that are absent

Four optional inputs are **not present** in this repository. Every run — including the
verified one in [§9](#9-verified-run) — logs a `load skipped` line for each and continues
normally. They are listed here so the messages are not mistaken for a broken clone:

| Missing file | Consequence |
|---|---|
| `inputs/sku_inch_map.csv` | **`JK_INCH_AWARE` has no inch data, so building inch-locking is inert** despite defaulting to on. Supply this file to activate it. |
| `inputs/machine_inch.csv` | Same — the phase-5 step-2 machine inch-lock does not engage. |
| `inputs/tbs2_current_running_machine.csv` | The building SKU→machine warm-start hint is unavailable. |
| `inputs/BOM12JUNE.xlsx` | The optional FG-mapping overlay is not applied. |

---

## 13. Documentation index

| Document | Read it for |
|---|---|
| **[INSTRUCTIONS.md](INSTRUCTIONS.md)** | Every run mode, single-phase runs, validation, troubleshooting |
| **[PHASEWISE_GUIDE.md](PHASEWISE_GUIDE.md)** | What each of the 9 phases does, reads, writes, and why |
| **[BUSINESS_RULES.md](BUSINESS_RULES.md)** | Plant-confirmed rules — line speed as rolls × single-strand (BR-1), chaffer slitter (BR-2), bias cutter pieces-per-cut (BR-3), the authoritative component aging table (BR-4) |
| **[CHANGES_FROM_V6_WAVE_P2.md](CHANGES_FROM_V6_WAVE_P2.md)** | Every deviation from base v6: what, why, how it helped, why it is correct |
| **[FLAW_REGISTRY.md](FLAW_REGISTRY.md)** | Known defects with severity and status |
| **[PHASE6_CURING_TAIL_BUG.md](PHASE6_CURING_TAIL_BUG.md)** | Deep dive on the curing-tail defect |
| **`config.yaml`** | Every flag, with its measured effect, inline |
| **`knowledge/memory/`** | 63 decision notes — the experimental trail behind every choice |
| **[`db_pipeline/README.md`](db_pipeline/README.md)** | The live database layer |

---

## 14. Data confidentiality

This repository contains **real JK Tyre manufacturing data** — bills of materials, routings
and rates, production plans, and opening WIP for the CTP Chennai plant.

**Use a private repository.** Do not publish this publicly without clearance from the
plant.

Secrets hygiene already in place:

- `db_pipeline/.env` (real database credentials) is **git-ignored**; only
  `.env.example`, with placeholder values, is tracked.
- No password, host, or credential is hard-coded anywhere in the source —
  `db_pipeline/db_connect.py` is the single connection point and reads `.env` only.
- If a filled-in `.env` was ever committed in an earlier revision, **rotate the password**;
  removing the file in a later commit does not remove it from git history.
