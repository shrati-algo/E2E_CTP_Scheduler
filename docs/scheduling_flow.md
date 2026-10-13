# End-to-end scheduling flow (as implemented in v82)

Back to [CLAUDE.md](../CLAUDE.md).

Order verified from `run_v15.py`, `run_pipeline.py` (`PHASES`) and the order of the log tags in `latest_schedule/v82/run_log.txt`.

**Status labels:**

| Label | Meaning |
|---|---|
| **IMPL** | Implemented and on in v82 |
| **OFF** | Implemented but switched off |
| **PARTIAL** | Partly implemented |
| **NOT IMPL** | Not implemented |

Design: **drum-buffer-rope.** The plant curing plan is the drum (fixed demand on fixed presses). Building follows the plant building plan. Components and compounds are pulled behind building; mixing is planned first as a forward simulation and then fixed.

---

## 1. Input ingestion and validation (IMPL)

| | |
|---|---|
| **Code** | `build_sep_inputs.py` → `adapt_inputs.py` → `build_mpq_items.py` → `apply_biascutter_calibration.py` → `apply_routing_gapfill.py` → `apply_ctb_approximations.py`; phase 0 `phase0_validate_inputs.py` (report only) |
| **Inputs** | 4 plan xlsx (`--plan-dir`), cut to the window; DB tables or `master_files` (see [architecture.md](architecture.md) §5); WIP snapshot by `--wip-date` / `--wip-time` |
| **Outputs** | `inputs/*.csv`, copied to `run_output/PHASEWISE/00_INPUTS` |
| **Rules / approximations** | Efficiency forced to 1 (`efficiency_override`); TBR bead one step (v74); cap-strip alias (v77); WIP assumptions (v80: expired stock usable 24 h, a day of measured flow as stock, untracked codes); line-speed multipliers for strip items (`[routing] BR-1 line-speed ×N`, **unverified with the plant**) |
| **Limits** | WIP feed incomplete (406 components, 132 master codes missing); compound WIP wrong (plant). `inventory.csv` is overwritten in place |

## 2. Demand explosion and priority (IMPL)

| | |
|---|---|
| **Code** | `phases/phase1b_demand_explosion.py`, `phase1_5_wave_builder.py` |
| **What** | Each planned cure row → green tyre → BOM children with need times. Waves of about 3 days |
| **Priority** | Need time (earliest first). No weighted demand priorities; all SKUs equal. The plant plan already holds its own priorities |
| **Notes** | BOM dedup = SUM. Green tyres from opening WIP are not built. The plant building plan already nets green-tyre stock (verified 2026-10-12), so green-tyre WIP must not be netted again |

## 3. Masters (BOM, routing, machines, cycle times) (IMPL)

- BOM / routing from `BOM_Routing_9oct` files.
- Machine eligibility from routing `machines`.
- Rate = `proc_time` × batch / UOM; mixing in whole batches (`mixing_batches.py`, `business_rules.mixing_output_uom: BATCH`).
- 4RC speed per item from routing (`fourrc_rules.routing_speed_only`).
- Building cycle from `CTP_Master_BuildingAllowable` / plan.
- Curing cycle from the plan.

## 4. Netting, time-phased (IMPL)

| | |
|---|---|
| **Code** | `phases/phase1c_mrp_netting.py`, `mrp_time_phased.py` |
| **What** | Opening stock nets gross demand oldest first, only against demand inside each lot's shelf life (v81 analysis: A3 not needed) |
| **Output** | `phase1_demand_NET_updated.csv.gz` |
| **Known** | Nets 0 green tyres, by design of the plan (see §2) |

## 5. Lot generation and sizing (IMPL)

| | |
|---|---|
| **Code** | `phases/phase2_lot_sizing.py` |
| **What** | Net demand → lots by wave buckets and shelf-life window. MPQ floor (`CTP_Master_MPQ` via `mpq_item.csv`). 4RC roll lengths (`CTP_Master_FRC`, `fourrc_rules.roll_lengths`) |
| **Mixing lots** | BR-V1: whole batches (round-up, +210,724 kg in v82). BRM-02: sticky compounds JT444 / JT466 in 3-batch stacks. BRM-13: +5 buffer batches per compound on its last lot (`business_rules.buffer_batches: 5`; +307,630 kg; special compounds ML405 / ML605 / 29MG excluded) |
| **Off** | Deep roll (v44–v46); smoothing lot spans (v63 / v64); backlog merge as one lot (v54) |

## 6. Linking lots (backward pegging) (IMPL)

| | |
|---|---|
| **Code** | `phases/phase3_dag_construction.py` |
| **What** | Producer lot → consumer lot edges by BOM and SKU blocks, with a shelf-life window filter (F1). `consume_qty` = consumer lot qty × per-edge BOM rate, allocated oldest first over the bound producers, with remaining balances. A residual goes to the next producer in time (`edge_alloc_fix`) |
| **Limitation** | Links are **reservations to specific lots**, not item stock. They are partly re-allocated in phase 5 (v81 A2 for compounds; v83 item-stock mode OFF) |

## 7. Time windows (CPM) (IMPL)

| | |
|---|---|
| **Code** | `phases/phase4_cpm.py` |
| **What** | Forward pass (earliest start / finish) and backward pass from the cure slots (latest start / finish) |
| **Lag** | producer → consumer = min aging + transfer time; max aging caps how early a lot may finish |

## 8. Mixing and upstream preparation (IMPL)

| | |
|---|---|
| **Code** | `mixer-improvement/mix_planner.py`, called in phase 5 (`[MIX PLANNER v56]`) |
| **What** | Forward simulation of the 6 mixers. A free mixer runs the most urgent allowed compound and covers 15 h (24 h for long items: IL3 / IL5 / B1144 / 4RC compounds) of need, as separate lots back to back. Rules: no repeat within 15 / 24 h; one mixer per compound per plant day; daily butyl blocks (K430-2 evening: friendly → 29MG → ML405 → ML605 → friendly; F270: friendly → 30G / IL3 → IL5); 120 s clean-out; TR100 5 min |
| **Priorities** | `item_priority_h` (B616M 8; B1144 / B186 / MB185 / MB184 / MB183 24), `fourrc_priority_h` 8, `level_load` (backward capacity levelling) |
| **Lots** | Fixed once planned: no later pass moves them |
| **Closed loop** | (`mix_planner.closed_loop`, `run_pipeline.py`) rounds 2–3 re-plan from round-1 real needs; `qty_feedback` adds extra component quantity into compound lots |
| **Limit** | Mixing is at capacity (masters 92–93 of 96 h a day in v82) |

## 9. Forward placement, machine assignment, material gate (IMPL)

| | |
|---|---|
| **Code** | `phase5_forward_placement_v2.py`: `place_one_lot_jit`, `_material_ready_ns` |
| **What** | Each lot goes to an eligible machine at the earliest time after (a) its inputs are covered and aged, (b) the machine is free (+ matrix changeover), (c) planned downtime, (d) its shelf-life floor (not made so early that it expires before its consumer) |
| **Gate** | For each input item, the time when the cumulative finished-and-aged linked supply ≥ need − opening-stock allocation (`open_alloc`, seeded oldest first at t0). Batch release (v69): a consumer may draw compound batch by batch. Bottleneck release (`[V25 BOTTLENECK RELEASE]`) |

## 10. Sequencing, changeovers, grouping (IMPL, mostly soft / heuristic)

Matrix changeovers everywhere (`changeover_matrix.csv`; missing pairs get a default, logged). Grouping passes per plant day, each accepting a new order only within windows (inputs ready, shelf life, ≤ 12 h move, consumer not delayed):

| Machine(s) | Rule | Log tag |
|---|---|---|
| 1002 Quintoplex (tread) | BR-21 | `[BR-21 Quintoplex]` |
| 1801 Duplex | BR-25 | `[BR-25 Duplex]` |
| 1001 Triplex (sidewall) | BR-28 | `[BR-28 Triplex]` |
| 1803 Quadraplex | BR-32 | `[BR-32 Quadraplex]` |
| PCR bead 1201 / 201 / 202 | BR-61 inch | `[BR-61 …]` |
| Belt cutters 801 / 802 | BR-50 / 52 belt kit | `[BR-50/52 …]` |
| Cutters / slitters | BR-56 same item once per shift | `[BRM-09 BR-56 …]` |
| 1101 / 1901 / 2201 | BR-41 compound group | `[BR-41 …]` |

**4RC 901:** day plan (one go per item per day, wire campaigns, least changeover), creel 240 min, rule 3 (fabric between cord changes), creel fill, load compaction, spool stock (`[4RC …]` tags).

**SKU stickiness in building:** follows the plan's sequence per TBM. Building changeover per the TBM (BJ / CONTI); BRBC checks.

## 11. Building and the kit gate (IMPL)

| | |
|---|---|
| **Code** | phase 5 `[BUILDING ECHO]`, `[C2 LEDGER]`, `[V23 BUILD CAP]`, `building_plan_feasible` |
| **What** | Plan building rows are placed in order. A row starts when a **physical material ledger** of every component (min-aged, within shelf life) covers its tyres (kit gate) |
| **Pull-forward** | Up to 24 h (`pull_forward_h`), none in the first 48 h (`no_pull_first_h`, v81 A4) |
| **First-24 h assumptions** | Strip items and untracked codes (`kit_gate_assume_first_h`) |
| **Proof** | `[C4 LEDGER PROOF]` at the end |
| **Curing-aware** | Rows whose tyres would wait > 64 h for a press go behind (`curing_aware_building`) |

## 12. Aging and just-in-time re-timing (IMPL, with known violations)

- **`[AGING PULL]`:** upstream lots feeding building are moved later toward their consumption to respect max aging. In v82, ~1,920 moved, ~600 still over (logged).
- **Short-shelf re-cut:** belts / ply are re-cut to the actual draw.
- **`aging_hard_block`:** OFF.

## 13. Repair and local re-optimisation (IMPL)

| Pass | What it does |
|---|---|
| `[REPAIR]`, `[REPAIR v18]` | Later-only consistency (inputs, changeovers, machine order) |
| `[REPAIR COMPACTION v53]` | Lots pulled back toward their placement start |
| `compress_upstream` | Left-shift into idle; mixers and 901 excluded |
| `[UPSTREAM GAP FILL v81 A2]` | Compound links re-allocated oldest first to component lots |
| `[UPSTREAM GAP FILL v82]` | Same-family lot into an idle gap |
| `[UPSTREAM GAP FILL v81 A1]` | Final repair with ready-time floors |
| `[C4 LEDGER PROOF]` | Material balance on the final schedule |

## 14. Curing (IMPL)

| | |
|---|---|
| **Code** | `phases/phase6_curing_revision.py` |
| **What** | The plan's curing blocks keep their press and SKU order and slide to when green tyres are ready (FIFO). Green tyre ≥ 2 h old (`gt_transfer_time_min: 120`, BRBC-21) and ≤ 72 h (else scrapped and reported: 541 in v82). Mould-change gap of the plan kept (`curing_keep_changeover`, BRBC-27). Opening-WIP green tyres seeded into the kit pool |
| **NOT IMPL** | Moving a SKU to another press / mould |

## 15. Output, reports, KPIs (IMPL)

| Output | Code |
|---|---|
| Workbook | `make_schedule_v18.py` + `strict_links.py` |
| Rule reports | See [architecture.md](architecture.md) §1 |
| Daily KPI | `daily_numbers.py` |
| Algo Planning BRBC check | `latest_schedule/plant-schedule/check_algo_planning_rules.py` |
| Dashboard | **NOT IMPL** (none in repo) |

**Optional evaluation:** `relaxed_mixing` (OFF; v75_relaxed only).

## Not implemented / planned

- Rolling start from the previous schedule.
- Horizon past the window (`Strict horizon: True`).
- Component target buffers (TARGET-WIP OFF, key-case bug).
- Kit-ready-first building order.
- Dynamic press / mould reassignment.
- Planned breaks / shift stops as downtime.
- A mathematical optimiser.
