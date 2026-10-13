# The scheduling model (heuristic, no solver)

Back to [CLAUDE.md](../CLAUDE.md).

There is **no optimisation solver and no objective function** in this project. Checked: no `ortools` / `pulp` / `pyomo` / `gurobi` / `cvxpy` / `scipy.optimize` imports. The schedule is built by a sequence of deterministic rules. The formulas below are what the code computes. Notation: t = time, q = quantity; times in phase 5 are integer nanoseconds.

## 1. Demand and netting

- Gross need of item *i* for cure block *b*: `D(i,b) = qty_b × Σ_paths BOM_rate(i | GT_b)` (BOM dedup = SUM), with need time from the plant plan.
- Time-phased netting (phase 1c): each opening lot *w* of item *i* (produced t_w, shelf life MX_i) covers demand oldest first, only for need times `t ≤ t_w + MX_i`. Net demand = gross − covered.
- **Priority:** need time only. **No priority weights** between SKUs or lines. PCR / TBR balance comes from the plan.

## 2. Lot sizing (phase 2)

- Lots per item from net demand in wave buckets, limited by shelf life: a lot covers demand within `MX_i − safety` of its first need.
- `lot_qty = max(net_bucket, MPQ_min)`.
- Mixing: `lot_qty = ⌈lot_qty / batch_size⌉ × batch_size` (BR-V1); JT444 / JT466 to a multiple of `3 × batch_size`; last lot of each compound `+ 5 × batch_size` (BRM-13).
- Duration = `lot_qty / rate` (rate from routing `proc_time` at efficiency 1). For mixing, duration is counted in whole batches.

## 3. Links and quantities (phase 3)

- Edge p → c when p's item is a BOM child of c's item for a SKU c serves, the wave blocks overlap, and `|fn_c − fn_p| ≤ MX_child − 8 h`.
- Consumption: `need(c,i) = lot_qty(c) × rate(c→i)` (per-edge BOM rate). It is allocated oldest first over c's bound producers using each producer's remaining quantity; a residual goes to the next producer in time.

## 4. Time windows (phase 4, CPM)

- `lag(p→c) = MN_i + transfer_i`; earliest start `EST_c ≥ EFT_p + lag`.
- `LFT` from the cure slot backwards: `LFT_p ≤ LST_c − lag`.
- Max aging: `EFT_p ≥ consume_c − MX_i` (not made too early).

## 5. Material gate (phase 5, `_material_ready_ns`)

For lot *c* and each input item *i*, with linked producers p₁…pₙ sorted by availability time `a_p = finish_p + MN_i` (or the batch-release time for compounds):

```
need_i = Σ_p edge_qty(p,c) − open_alloc(c,i)        # opening stock allocated at t0 (F3); not subtracted when (c,i) ∈ _OPEN_NETTED (v83 mode only)
ready_i = min { a_pk : Σ_{j≤k} edge_qty(p_j,c) ≥ need_i }   # covered when need_i ≤ 0
start_c ≥ max_i ready_i
```

Building uses a **physical item ledger** per SKU instead (`[C2 LEDGER]`): cumulative supply of every component (aged, alive) ≥ cumulative tyres × per-tyre BOM.

## 6. Machine placement and sequence

```
start_c = max( material ready, machine free + changeover(prev→c), plan / downtime windows, shelf-life floor )
shelf-life floor = latest_consumer_start − (MX_i − margin)
```

- Machine: earliest feasible eligible machine.
- Changeover from the matrix (sequence-dependent): **same item → 0**; family / type pairs → matrix minutes; missing pair → default (logged).
- Grouping passes (BR-21 / 25 / 28 / 32 / 41 / 50 / 52 / 56 / 61) re-order lots inside a plant day only if:
  - every moved lot keeps its inputs ready, its shelf life and its consumers;
  - no move is longer than 12 h;
  - total changeover does not rise.

## 7. Mixing planner (`mix_planner.py`)

Event simulation over the 6 mixers. When mixer *m* is free at time t:

```
candidates = compounds allowed on m, not running on another mixer this plant day,
             last run start ≤ t − gap (15 h / 24 h long), masters ready (aged), shelf life ok
urgency(k) = earliest need of k − priority_h(k) [− fourrc_priority_h] (level_load can bring needs earlier)
run k* = argmin urgency; take all lots of k* needed in [t, t + cover] (15 h / 24 h) back to back, 0 changeover
```

- Daily butyl blocks are inserted.
- Clean-out of 120 s after butyl, TR100 5 min, changeover 60 s.
- Wait reasons are written to `outputs2/phase5_mix_planner_waits.csv`.

## 8. Repairs and gap fill

| Pass | What it does |
|---|---|
| Repair (later-only) | Restores precedence, changeover and capacity |
| Compaction | Moves earlier toward the placement start where all checks hold |
| v81 A1 | Floor = upstream open on component machines, so a lot may move back to its ready time; machine order unchanged |
| v81 A2 | For each compound: total per producer and per consumer kept; links re-allocated earliest-ready producer → earliest-start consumer, skipping batches expired at the consumer start |
| v82 family reorder | An idle gap ≥ 30 min between A and B takes the earliest-due later lot X of the same family (same item on cutters) if X fits with changeovers, inputs, downtime and its aging floor |

## 9. Curing (phase 6)

- Each plan cure block keeps its press and order.
- `start = max(plan start, green tyres ready (built + 120 min), press free incl. the plan's mould-change gap)`.
- A green tyre older than 72 h is scrapped (counted).
- No press / mould reassignment.

## 10. KPIs used to judge a version (business, not solver)

| KPI | Source |
|---|---|
| Cured (and built) tyres per plant day, PCR / TBR, against plan | `daily_numbers.py` |
| Rule compliance | Pass / partial / fail and per-rule % (`rule_adherence_report.py` and section reports) |
| Green tyres scrapped | log |
| Material starvation | must be 0 |
| MPQ below | must be 0 |
| Mixing BRM-09 count; 4RC rule 3 | section reports |
| Utilisation per machine / section | ad-hoc analyses, e.g. `CAPACITY_vs_TARGET_v82.md` |

## 11. How the model changed from v45 to now

| Change | Versions | Why |
|---|---|---|
| Mixing rules enforced by later pushes → **planned first in a forward simulation** with the rules built in | v47–v55 → v56 / v57 | Pushes cascaded; output fell to 20–66 k |
| 4RC demand-only (no whole-spool top-up) | v76 | 117 km made and never used |
| Closed loop and quantity feedback | v69 / v76 / v77 | Re-plan mixing from real needs |
| Building gated on every input + physical ledger | v77 | Cap strip / edge gum used to be assumed available |
| Gap fill / re-allocation on component machines | v81 / v82 | Idle while ready lots waited |
| Opening-stock assumptions from the plan generator | v80 | Day 1 |

All have evidence in `BUSINESS_RULES_LOG.md`.
