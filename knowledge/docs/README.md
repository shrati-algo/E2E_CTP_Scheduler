# ctp_v6 — CTP (PCR) Production Scheduler

> ### 👉 Reviewing this to fix the data? Read **[`ISSUES.md`](ISSUES.md)** — that is the handover list.
> Everything open is a **master-data** gap. No code change is needed for any of it.

A finite-capacity production scheduler for **JK Tyre CTP Chennai (PCR)**.

It runs the **`v6_wave_p2` algorithm** — the scheduler already proven at BTP Banmore — against
CTP's data. The phase files are **byte-identical copies** of v6's, except for **four deliberate,
documented deviations** (see below). All CTP-specific data adaptation is isolated in a single
file, `adapt_inputs.py`, so the algorithm itself never has to know it is running on a different
plant.

> ### 📊 `CTP_SCHEDULER_REPORT.xlsx` — the one file that tells you everything
> Regenerated **automatically on every `run_all.py`**, so it can never drift out of sync:
> - **`A1` / `A2` — WHAT WE CHANGED.** Every deviation from v6 and every data reshape, with the
>   reason, the measured effect, and the risk if it is reverted.
> - **`B1`…`B7` — WHAT IS BROKEN.** Every master-data gap, with empty `PLEASE_FILL_*` columns
>   for the plant team.

---

## Quick start

```bash
git clone <this-repo>
cd ctp_v6

python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt

python run_all.py
```

That's it. Python **3.10+**. Everything needed is bundled — **no database, no network, no
external paths.** Every path resolves relative to the script, so it runs from any directory.

**⚠️ Out of the box this stops at phase 2 with a `ValueError`. That is expected and correct —
see [Known blocker](#-known-blocker) below.**

### Running parts of it

```bash
python adapt_inputs.py            # rebuild inputs/ from ctp_inputfiles/ only
python run_pipeline.py            # run the phases (assumes inputs/ exists)
python run_pipeline.py --from phase4
python run_pipeline.py --only phase5
python run_pipeline.py --skip phase0

python make_data_issues_report.py # -> CTP_SCHEDULER_REPORT.xlsx
```

---

## How it works

```
ctp_inputfiles/          the plant's raw masters (BOM, routing, curing plan, WIP, …)
        │
        │   adapt_inputs.py     <-- THE ONLY PLACE CTP DIFFERS FROM v6
        ▼
inputs/                  the same data, in v6's exact schema   (generated, git-ignored)
        │
        │   run_pipeline.py     <-- runs v6's phases, in v6's order
        ▼
outputs/  outputs2/      lots, DAG, CPM windows, the schedule  (generated, git-ignored)
```

`inputs/`, `outputs/` and `outputs2/` are **derived** — they are rebuilt on every run and are
not committed. Only `ctp_inputfiles/` is source data.

### The phases

| # | phase | what it does |
|---|---|---|
| 0 | `phase0_validate_inputs` | Validates the inputs. Report only — safe to `--skip`. |
| 1b | `phase1b_demand_explosion` | Curing plan → BOM explosion → per-item demand |
| 1.5 | `phase1_5_wave_builder` | 3-day waves, plus the load-balance pass |
| 1c | `phase1c_mrp_netting` | MRP netting — consumes opening WIP down the BOM |
| 2 | `phase2_lot_sizing` | Lot sizing, wave-aware bucketing, MPQ floor |
| 3 | `phase3_dag_construction` | Producer→consumer DAG + the aging edge filter |
| 4 | `phase4_cpm` | CPM time windows (EST / EFT / LST / LFT) |
| 5 | `phase5_forward_placement_v2` | Finite-capacity forward placement onto machines |
| 6 | `phase6_curing_revision` | Curing revision |

**`phase1a` (machine-group assignment) is not run** — CTP has no MG model. But it is not
*bypassed*: `adapt_inputs.py` synthesises its output, so v6's MG filter still runs **exactly as
written**. CTP's BOM is single-variant (`TBM PCR` on all 42,411 rows), which makes that filter a
no-op — correct by construction. The adapter **refuses to run** if a multi-variant BOM ever
appears, because then the filter would stop being a no-op and silently inflate demand.

---

## The four deviations from v6

Everything else is **byte-identical** to `v6_wave_p2/phases/`. Verify with `diff`, don't take
my word for it.

| # | what | why |
|---|---|---|
| 1 | **Efficiency from the routing** | v6 hardcodes `eff = 1.0`. CTP's routing states **0.96**. `eff` divides, so every operation takes **+4.17%** longer. |
| 2 | **Transfer time from the routing** | v6 never reads `transfer_time_min`. It is a mandatory producer→consumer lag — the same shape as `min_aging` — so it is **added to `min_aging`**. Green tyres get a **90-minute** building→curing floor. |
| 3 | **The plant changeover matrix** | v6 has none: its `compute_changeover_min` is machine-**name**-based (`WBC`/`LTBC`/`FISCHER`…) and matches **0 of CTP's 123 machine IDs**, so everything would fall to a flat 15 min. |
| 4 | **BOM rows are SUMMED, not deduplicated** | **This is a bug fix, not a preference.** See below. |

Deviations 1–2 touch `phase2`, `phase4`, `phase5`. Deviation 3 touches `phase5`.
Deviation 4 touches `phase1b`.

> ### On deviation 4 — v6 was discarding half the belt compound
>
> v6 keyed BOM edges on `(Parent, child)` and, where rows differed only by `grand_parent`,
> **kept the first and discarded the rest.** But `grand_parent` says *which component consumes
> this slice of a shared material* — belt-ply-1 and belt-ply-2 both draw from the same
> calandered roll, with different compound amounts. **Both plies are in every tyre** (227 of 228
> SKUs). v6 provisioned **one belt's rubber for a two-belt tyre**, while the same explosion
> correctly demanded 2.000 steel belts per tyre.
>
> **The BOM proves the rule itself:** `Parent_qty(X) == SUM(child_quantity into X)`.
> Summing → **10,755/10,755 nodes, 0 violations.** v6's rule → **1,292 violations.**
> Chemistry agrees: masterbatch ÷ final-compound = **0.925** summed (correct) vs **0.473**
> first-row-only (chemically impossible).
>
> **Effect:** FINAL COMPOUND **+9.1%**. Every physical item unchanged (Green Tyre 1.000,
> Steel Belt 2.000, Bead 2.000). **A permanent gate now asserts the identity on every run** —
> if a dedup is ever reintroduced, phase 1b stops.
>
> v6's *own* BOM has 7,782 such groups, so **BTP is under-counting its compound too.**
>
> v6's original UOM rule (same edge stated in two different units → pick one) is **preserved
> unchanged** and still fires whenever rows disagree on UOM. CTP has zero such rows.

> **On deviation 3 — the join is the hard part.** The matrix is keyed on a machine **line name**
> (`"4 RC Calendar"`); the routing is keyed on a machine **ID** (`"901"`). **Zero string
> overlap**, and no bridge exists in any input file. The bridge is
> **`machine_to_changeover_line` in `config.yaml`**, built from the plant's own machine list
> (`ctp_inputfiles/pcr machines.png`). **Review that table.** `adapt_inputs.py` hard-fails if it
> names a line the matrix does not contain, so it cannot rot silently.
>
> Curing, GT-building and the mixers have **no line in the matrix** and correctly fall back to
> v6's rule — so **building still uses a flat 15 min changeover.**

---

## 🛑 Known blocker

**Phase 2 crashes on a clean run. This is deliberate. Do not "fix" it by coercing the NaN away.**

```
ValueError: cannot convert float NaN to integer     (phase2_lot_sizing.py:574)
```

Three items in `jkt_routing_pcr 14.xlsx` are **incomplete master records** — no `proc_time`, no
`proc_time_UOM`, **no machine**, and no `batch_size` for the compound:

| routed_product | ItemType | department | NET demand |
|---|---|---|---|
| `GT 2568 HT2` | Green Tyres | BUILDING | 19,036 |
| `GT 1482 UHL` | Green Tyres | BUILDING | 14,670 |
| `TCA313` | FINAL COMPOUND | FINAL MIXING | 239 |

An operation with no cycle time cannot be scheduled, and **no value has been invented.**

**Why the crash is load-bearing:** `phase4_cpm.py:368` does
`pd.to_numeric(..., errors="coerce").fillna(0)`. A NaN duration that survives phase 2 becomes a
**zero-hour lot** — the plan would happily build **33,706 green tyres in no time at all** and
report success. Deleting the routing rows is no better: `phase1b:225` would then drop those
items from demand entirely, trading a loud crash for the **silent omission of two green tyres
from the curing feed**.

**To unblock: fill those three rows in the routing master.** Then `python run_all.py` runs
2→6 straight through.

`python make_data_issues_report.py` writes **`CTP_SCHEDULER_REPORT.xlsx`** — every data gap,
with empty `PLEASE_FILL_*` columns, ready to hand to the plant team.

---

## ⚠️ The bug class that bites everyone: "present-but-blank"

**Read this before adding or refreshing any input.**

Every v6 parser reads numbers like this:

```python
try:    v = float(row["Col"])
except (TypeError, ValueError, KeyError):    v = None
```

That handles a **missing** column or row. But **`float(NaN)` returns `NaN` — it does not
raise.** So a cell that is *present but blank* yields `NaN`, never `None`. And `NaN` defeats
**every** downstream guard, because all NaN comparisons are False:

| guard | with `NaN` |
|---|---|
| `if x is not None and x <= 0:` | **False** → the NaN survives the sanitiser |
| `if x:` | **True** → NaN is truthy |
| `max(0.0, NaN - 8.0)` | **0.0** → the window silently collapses |
| `int(math.ceil(a / x))` | 💥 crash |

**v6's own masters contain zero blanks. That is its input contract.** CTP's masters are full of
them. So there are exactly two legal ways to say "no value":

1. **Omit the column** → `KeyError` fires → `None`. *(Used for CTP's no-max MPQ.)*
2. **Omit the row** → v6's `.get(key, default)` default fires. *(Used for 789 empty aging rows.)*

**A blank cell is neither, and is the one thing v6 cannot survive.**

Also: `adapt_inputs._read()` uses `keep_default_na=False`, so blanks arrive in the adapter as
empty **strings** (`""`), not `NaN` — `.isna()` alone silently matches nothing. Always test
`s.isna() | (s.astype(str).str.strip() == "")`.

Full hazard table, and every design decision with its reasoning, is in **`MEMORY.md`**.

---

## Files

| file | |
|---|---|
| `run_all.py` | **start here** — adapt inputs, then run every phase |
| `adapt_inputs.py` | the only place CTP differs from v6. Every reshape is documented in-line. |
| `run_pipeline.py` | runs the phases in v6's order, with preflight guards |
| `config.yaml` | v6's tuning values + CTP paths + the changeover machine bridge |
| `MEMORY.md` | **the important one.** Every CTP fact, hazard, and design decision. |
| `make_data_issues_report.py` | writes CTP_SCHEDULER_REPORT.xlsx (auto-run by run_all.py) |
| `phases/` | v6's algorithm. Only 3 files differ, all marked `CTP DEVIATION`. |
| `ctp_inputfiles/` | the plant's raw masters (bundled — this repo is self-contained) |

### Inputs

| file | what |
|---|---|
| `jkt_bom_pcr 13 (1).xlsx` | bill of materials |
| `jkt_routing_pcr 14.xlsx` | routing — operations, machines, cycle times, transfer, efficiency |
| `CTP_PCR_Curing_Schedule_2026-07-03 (1).xlsx` | the curing plan (the drum) |
| `opening_wip.csv` | **opening WIP** — 39,510 rows. Netted in phase 1c. |
| `jkt_itemType_master_pcr.csv` | item types |
| `jkt_aging_master_pcr.csv` | min/max aging |
| `MOQ (3).xlsx` | MPQ — **minimum only; CTP has no maximum** |
| `jkt_buffer_master_pcr.csv` | buffer levels |
| `jkt_changeover_matrix_combined (1).xlsx` | the plant changeover matrix |
| `pcr machines.png` | the plant machine list — the changeover machine bridge |

> **The opening WIP matters more than it looks.** `db_loader.py` (in `phases/`) loads it. If
> that file is ever missing, every phase falls back **silently** — phase 1c prints one WARN line
> and carries on with **no WIP at all**, over-producing by ~12%. A phase that warns and
> continues is more dangerous than one that crashes.
