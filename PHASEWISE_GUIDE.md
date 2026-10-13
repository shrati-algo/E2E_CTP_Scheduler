# Phasewise guide — what each phase does, reads, and writes

The scheduler runs **9 phases** in a fixed order. Each phase is a standalone Python program that
reads the previous phases' outputs (from `outputs2/` at run time; saved into `output/latest/PHASEWISE/`
after the run) plus the adapted inputs in `inputs/`. This is a **drum-buffer-rope** design: the
**curing plan is the drum** (fixed demand), building paces to it, and components are roped just-in-time
behind building.

Pipeline order:

```
phase0 → phase1b → phase1_5 → phase1c → phase2 → phase3 → phase4 → phase5 → phase6
validate  explode   waves     netting    lots     DAG      CPM     place    cure
```

Data flow at a glance:

```
plan.csv (curing drum)  ─┐
bom.csv, routing.csv    ─┼─► demand ─► waves ─► net demand ─► LOTS ─► DAG ─► timed DAG ─► FLOOR ─► cured floor
inventory.csv (WIP)     ─┘   1b        1_5       1c            2       3      4 (CPM)      5         6
aging/mpq/changeover ────────────────────────────── used across 2,4,5,6 ───────────────────────────►
```

---

### phase0 — Validate inputs  *(report only)*
- **Purpose:** sanity-check every input master before anything runs — missing columns, blank cells
  (the "present-but-blank NaN trap"), broken references.
- **Reads:** everything in `inputs/`.
- **Writes:** `outputs/phase0_data_validation.xlsx` (report only — nothing downstream reads it).
- **Why it matters:** v6 survives a *missing* column but not a *blank* cell (`float(NaN)` doesn't
  raise); phase0 surfaces those before they corrupt a lot size silently.

### phase1b — Demand explosion
- **Purpose:** turn the curing plan into **per-component demand**. For each planned green tyre, walk
  the BOM one level and emit how much of each child (ply, bead, tread, sidewall, belt, compound…) is
  needed, and when.
- **Reads:** `plan.csv`, `bom.csv`, `routing.csv`.
- **Writes:** `phase1_demand_updated.csv.gz`.
- **CTP specifics:** BOM dedup uses **SUM not first** (deviation #4 — a 2-belt tyre gets 2 belts'
  rubber); WIP-fed SKUs' component sub-tree is **pruned** (CTP #41 — green tyres that arrive from
  opening WIP are never built, so their components must not be exploded). Explosion is **flat**
  per-Super_parent (child_quantity is per-tyre; no chain multiplication).

### phase1_5 — Wave builder
- **Purpose:** bucket the demand into **time waves** (≈3-day windows) so lot-sizing and sequencing
  work on manageable, time-localised batches.
- **Reads:** `phase1_demand_updated.csv.gz`, `plan.csv`.
- **Writes:** `outputs/phase1_5_waves.csv`, `phase1_5_block_to_wave.csv`, `phase1_5_wave_summary.xlsx`.
- **CTP specifics:** v6-identical.

### phase1c — MRP netting
- **Purpose:** subtract **opening WIP** (component stock already on the floor) from gross demand, so
  the schedule only *builds what is missing*.
- **Reads:** `phase1_demand_updated.csv.gz`, `inventory.csv` (opening WIP snapshot).
- **Writes:** `phase1_demand_NET_updated.csv.gz`.
- **CTP specifics:** **v6-verbatim** by plant-owner directive (2026-07-22). Nets at the *component*
  level (an item's own loose stock); the green-tyre-level WIP case is handled separately in phase1b
  (CTP #41). Known to over-net ~1.5 M — an accepted v6 characteristic (validator V10 = WARN).

### phase2 — Lot sizing
- **Purpose:** turn net demand into **actual production lots** — sized by MPQ (minimum run quantity),
  duration caps, and aging windows; assigned an equipment type, a wave, and a first/last-need time.
- **Reads:** `phase1_demand_NET_updated.csv.gz`, `routing.csv`, `mpq.csv`, `mixing_mpq.csv`,
  `aging_master.csv`, waves.
- **Writes:** `phase2_lots_updated.csv` (the lot master — one row per lot), `phase2_lot_skus_updated.csv`,
  `phase2_lot_blocks_updated.csv.gz`, `phase2_bom_shortfall.csv`.
- **CTP specifics:** MPQ from the corrected plant file; compounds sized by Banbury batch × routing, not
  a piece floor; transfer time carried on each lot for phase4. Every lot ≥ its MPQ, ≤ its duration cap.

### phase3 — DAG construction
- **Purpose:** build the **precedence graph** — connect each producer lot to the consumer lots it
  feeds (component lot → the green-tyre lots that use it), respecting the wave/aging window.
- **Reads:** `phase2_lots_updated.csv`, lot-blocks, `bom.csv`, `aging_master.csv`.
- **Writes:** `phase3_dag_edges_updated.csv`, `phase3_lot_degrees_updated.csv`,
  `phase3_lot_blocks_updated.csv.gz`, `phase3_lot_to_skus_updated.csv`.
- **CTP specifics:** the **F1 early-window edge filter is kept v6** (plant-owner directive). It links
  a feeder to all green tyres in its 3-day wave — this over-linking is the root of the reported C4/C6
  tightness (made honest in phase4's report mode, not by changing the graph).

### phase4 — CPM (critical-path timing)
- **Purpose:** put **times** on the DAG. A forward pass gives each lot its Earliest Start/Finish; a
  backward pass from the curing deadlines gives Latest Start/Finish; **slack = LST − EST**.
- **Reads:** `phase3_*`, `phase2_lots_updated.csv`, `aging_master.csv`, `plan.csv` (curing anchors).
- **Writes:** `phase4_lot_times_updated.csv` (EST/EFT/LST/LFT/slack per lot),
  `phase4_aging_violations_updated.csv`, `phase4_aest_window_by_itype_updated.csv`.
- **CTP specifics:** the producer→consumer **lag = min_aging + routing transfer time** (applied
  symmetrically in both passes). `max_aging` caps how early a lot may finish (else it expires). Curing
  terminals are anchored to `curing_slot − min_aging`. `honest_lft_anchor: report` and `c6_jit_audit`
  make the C4/C6 *reports* truthful without moving any lot. Negative slack on the busiest green tyres is
  a conservative anchor + cold-start artifact — the forward pass still ends day 29.34 (feasible).

### phase5 — Forward placement (the floor schedule)
- **Purpose:** assign every lot to a **specific machine at a specific time** — LPT-ordered cap-and-spill
  packing, campaign grouping, changeovers, and the material-readiness (kit) gate.
- **Reads:** `phase4_lot_times_updated.csv`, `phase2_lots_updated.csv`, `routing.csv`,
  `changeover_matrix.csv`, `building_changeover.csv`, `changeover_lookup.csv`.
- **Writes:** `phase5_floor_schedule_updated.csv` (the floor schedule), `phase5_schedule_updated.csv`,
  `phase5_machine_utilization_updated.csv`, `phase5_summary_updated.xlsx`.
- **CTP specifics:** building is **inch-locked** (each TBM pinned to one tyre size); building changeover
  = the **DB per-TBM values** (28/60 for 3401–05, 22/42 for 3406–11). `building_takt_spread`,
  `spread_eft`, `building_tail_spill`, and the streaming kit-gate are the makespan/aging levers.
  Orphans are **placed late, never deleted** (`RESPECT_LFT=False`).

### phase6 — Curing revision
- **Purpose:** produce the **curing schedule** — a faithful echo of the plan's press assignments,
  slid in time to when each green tyre is actually ready, plus opening-WIP green tyres seeded into the
  kit pool.
- **Reads:** `phase5_floor_schedule_updated.csv`, `plan.csv`, `inventory.csv`, `aging_master.csv`.
- **Writes:** `phase6_curing_revision_updated.csv`, `phase6_wip_reconciliation.csv`,
  `phase6_expired_opening_wip.csv`, and **appends curing rows to** `phase5_floor_schedule_updated.csv`.
- **CTP specifics:** phase6 **never moves a mould** — it only slides time on the *same* press the plan
  named. That is why the makespan tail is mould-bound: the plan pins each SKU to the presses where its
  mould physically sits. Tyres cured from opening-WIP green tyres (built before their own build, or
  never built) are separated into the workbook's "Inventory GT Curing" / "WIP Curing" sheets.

---

## Reading the outputs

- **`output/latest/CTP_FLOOR_SCHEDULE.xlsx`** — the deliverable. Sheet *Floor Schedule* = manufacturing
  + real curing; *Inventory GT Curing* = cures fed by opening-WIP green tyres; *WIP Curing* = cures
  whose green tyre was never built.
- **`output/latest/PHASEWISE/`** — one folder per phase, the intermediate CSVs above. This is what the
  testing suite validates.
- A **lot id** like `GT 1765 ROYL_L0001` is item + sequential lot index. On the floor, one lot can
  appear as several **shift-rows** (a 463-unit lot crossing a shift boundary shows a small tail row +
  the main row) — that is a per-shift *view*, not a small lot.
