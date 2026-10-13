# Data gaps in the database that force assumptions (status 2026-10-13)

**Databases:** `JK_CTP_PPDS` (and `jkplanning_CTP`).

**Evidence:**
- v85 / v86 run logs;
- `latest_schedule/v85/DB_MASTER_CHECKS_v85.md`;
- the read-only checks of 12–13 Oct;
- `plant-schedule/*.md` comparisons.

**Assumption numbers:** `V85-n` / `V86-n` refer to `latest_schedule/ASSUMPTIONS_LOG.md`.

## 1. Opening stock: `CTP_Opening_WIP`

| Gap | Evidence | Assumption it forces |
|---|---|---|
| Compound held as **one row per batch** with no kg; kg = batches × routing batch weight | 4,375 rows of 1 batch each (12-Oct 06:31) | V85-12 (19 rows without a batch weight stay as counts) |
| **Stale / wrong compound stock**: batches back to 27-Sep, 72 % past shelf life by the aging master; masters only 80 batches. The plant confirmed the compound WIP is wrong | 12-Oct 06:31 snapshot. The 12-Oct 17:26 correction was partial (finals only, no stock prep / masters, no green tyres) | V85-4 (expired stock unusable) and the start-up loss |
| **Items not in the feed**: ~389–406 building components, 132 master codes; cap strip / edge gum / gum strip not reliably tracked | `CTB_WIP_ASSUMPTIONS_v2` sheet 6b; run logs | V85-1 (cap strip + edge gum assumed 8 h), V85-3 |
| **Work in progress at the snapshot is not recorded** (lots on machines at 06:30) | No such table | V86-1 (plant flow as stock) |
| **The 12-Oct 17:26 correction is stored as shift B**, not A, and mixes two things: 111 final-compound lots (414 batches, ~91 t) produced before 07:00 that the 06:31 snapshot missed, and 343 lots that are in both with a **lower** qty (what was left after the day's consumption, not a correction) | `CTP_Opening_WIP` 12-Oct 06:31 vs 17:26 | V87-1 (missing lots added; quantities of lots in both kept from 06:31) |
| **Master compound WIP**: only 80 batches / 4 codes in the snapshot (plant confirmed it is wrong) | 12-Oct 06:31 | V87-2 (masters assumed unlimited) |
| **DB server unreachable** 13-Oct ~11:20 (connect time-out) | v87 run log | v87 used the copies saved 13-Oct 01:04–01:11 (same data as v85) |

## 2. Plant production: `CTP_Plant_Schedule`

| Gap | Evidence | Assumption it forces |
|---|---|---|
| **Actuals only, no forward / running schedule**; ends 11-Oct 12:33, 18.5 h before the 12-Oct 07:00 start | 30-Sep → 11-Oct bookings | V86-1 (8 h of average flow instead of the real in-progress lots) |
| **No output quantity per booking**: only consumed input qty, so output is back-calculated through the BOM | Columns qty / consumed_item | V86-1a |
| 477 bookings on 18 items can't be converted (item or input not in the BOM, e.g. SQ-00x on ebr, PBP57, 30GWA) | `build_plant_flow.py` | Those items get no flow |
| **No upstream flow table** (assumption.md named `upstream_flow.csv`) | Not in the DB | V85-5 (06-Oct Excel sheet used) |

## 3. Mixing: `CTP_Master_mixing`

| Gap | Evidence | Assumption it forces |
|---|---|---|
| **`machine_pref` = 0 on all 557 rows**: no mixer preference | DB check 13-Oct | V86-3 (preference by kg/h + 6 h penalty) |
| **206 rows without a batch weight**: all NULL, none 0; 128 compounds (PCR 149 rows, TBR 57) | `latest_schedule/DB_mixing_rows_without_batch_weight.csv` | **D-1 (from 13-Oct): routing `batch_size` used.** It is one size per compound, so a big mixer can get a small size (XMT507 on K430-2 190 kg vs 380 in the preference sheet) |
| **44 duplicate rows** (22 pairs): the same compound is entered once per line, and PCR rows use `310 / 430-1 / 430-2 / 440M / 270M` while TBR rows use `K310 / K430-1 / K430-2 / F440 / K270M`. Values identical | `DB_MIXING_MASTER_ISSUES.xlsx` | Normalised in code |
| **68 compounds in routing missing** (GB / GT / GL … codes); B333ARS only in the mixing master | Same | Routing values used |
| **Cycle time is the routing value**, not the plant's: the plant is ~8 % faster on 57 of 121 pairs (08-Oct); mixers busy 20–21 h / day in the plant | `PLANT_vs_SCHEDULER_MIXING_CYCLE_TIMES.md` | V86-4 (plant cycles where faster, partly estimated) |
| Compounds mixed but not in routing / BOM (30GWA, MT153X, B1, B2); recipe items "WA" and raw-material substitutes not in the BOM | `PLANT_vs_MASTER_DATA_DIFFERENCES.md` | Not scheduled / reported |

## 4. Routing and line speeds: `CTP_Master_Routing`

| Gap | Evidence | Assumption it forces |
|---|---|---|
| **Items with no make rate** (5 PCR, 3 TBR per assumption.md) | `apply_routing_gapfill.py` log | V85-7 (median of the same operation) |
| **Compounds without a batch size** (e.g. T153X, B617, T4378, XMT4377) | BR-V1 log "gap-filled batch sizes" | V85-7 |
| **Strip line speeds are per strip, not per run** (EG01 30 m/min → ×15) | `[routing] BR-1 line-speed ×N` | V85-13 |
| **Efficiency 0.96 in the DB but not used**; no planned-stop data | `efficiency_override: 1.0` | V85-10 (efficiency 1, no breaks) |
| **Speeds differ from the plant**: 4RC CPJ / ECOPOLY / HTPOLY 1.6–2.1× faster in the plant; **1802 13 m/min vs ~10 measured** | `PLANT_vs_v82_4RC_PROCESS.md`, `BEF_1802_LINE_SPEED_ANALYSIS.md` | Routing kept; flagged |
| **Bias cutter 2001 rate never plant-measured** (calibrated from our own WIP) | `apply_biascutter_calibration.py` | Calibration assumption |
| **No routing** for CWBP65; outsourced TBA827 / TCA313 / MTCM313 | Routing | CWBP65 BOM approximated; V85-8 |

## 5. BOM: `CTP_Master_BOM`

| Gap | Evidence | Assumption it forces |
|---|---|---|
| **SKUs with no BOM / building routing**: SXC1E (1225219015008SXC1E), HRHP0, HRHM0 (PCR), MDL30 (TBR) | assumption.md #2; v85: 5 rows skipped | V85-6 |
| **Unit / quantity errors**: BPG01 → BPGSL01 (~28×, 329 k m short); compound : cord ratios off (CP56542 −13 %, BRTTL −17 %) | Genealogy SHORT; 4RC comparison | Reported |
| **CAP STRIP 120 into CAP 66** missing (plant used 51 t in 9 days) | 4RC plant schedule | BOM as is |
| **4RC metres per tyre**: the plant makes ~1.9× our BOM need | 4RC comparison | BOM as is |
| TBR bead wound + apexed in one step (plant) but two steps in the BOM | TDA schedule | v74 approximation (fold) |

## 6. Aging: `CTP_Master_Aging`

| Gap | Evidence | Assumption it forces |
|---|---|---|
| **Items with no aging spec** (3 PCR) | assumption.md #4 | Ready immediately |
| **24 h shelf life on cut components** (belt, ply, apex, body ply, cap strip) seems short; the plant keeps buffers | Analysis 12-Oct | Cut just in time, so no buffer |
| **Compound shelf life**: the plant uses compound past our max (3.8 % of uses; B176 101 h vs 96, B460 77 h vs 72) | 4RC plant schedule | Our max kept (A3 held) |

## 7. Changeover: `CTP_Master_ChangeoverMatrix`

| Gap | Evidence | Assumption it forces |
|---|---|---|
| **Missing pairs**: 1001: 620, 1801: 1,678, 1803: 54, 1002: 3,580; TBR bead 003↔005, 008↔009 | Run log | Default minutes charged |
| **TBR bead inch per machine** not in any master (plant: 2404 = 22.5″, 2403 / 2405 = 20″) | TDA comparison | Inch rule not applied |
| **4RC creel time** = 240 min rule; plant median ~170 min, sometimes 0 (second creel?) | 4RC comparison | 240 kept |

## 8. Building / curing masters

| Gap | Evidence | Assumption it forces |
|---|---|---|
| **Machine names differ** between plan (TBMPCR<n>Stage2, TBM-0<n>) and masters (34<nn>, TBR-0<n>) | `DB_MASTER_CHECKS_v85.md` | V85-11 (name mapping) |
| **Plan breaks the masters**: 54 cure rows (2,636 tyres) on presses not allowed; 56.5 % of build rows on the preferred machine | Same | Plan kept (user) |
| **Planned downtime** read from `jkplanning_CTP.downtime_reason`; `JK_CTP_PPDS.CTP_Master_DowntimeSchedule` exists but its relation is unclear | Run log | — |
| `CTP_Master_EquipmentEfficiency` (88 rows) exists but is not used | Table list | Efficiency 1 |

## What would remove most assumptions (priority)

1. **Correct compound + stock-prep WIP**, in kg, with produced time, in the 06:30 snapshot, plus the missing components (cap strip, edge gum, gum strip).
2. **A running / forward plant schedule at the snapshot time**, or at least output quantities per booking in `CTP_Plant_Schedule`.
3. **`CTP_Master_mixing` completed:** batch weight for every pair, `machine_pref`, one mixer code style, all compounds.
4. **Plant-confirmed rates:** mixing cycle times, 1802 speed, 4RC speeds, bias cutter rate, strip counts per run, efficiency / planned stops.
5. **BOM fixes:** the 4 SKUs without BOM, BPG01 unit, CWBP65 routing, CAP STRIP 120, 4RC metres per tyre.
6. **Aging and changeover completed:** 24 h items, compound shelf life, missing matrix pairs, TBR bead inch.
7. **One machine-name convention** across plan and masters.
