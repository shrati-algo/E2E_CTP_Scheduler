> **⚠ SUPERSEDED — written 2026-09-26, describes e2e_v1–v3.**
> The current run is **e2e_v14** and several things below have since
> changed (virtual WIP removed, two-database pipeline, bead gate, even
> curing lots, the v14 dispatch fix).
> **Start from [CLAUDE_CONTEXT.md](CLAUDE_CONTEXT.md)** — the single
> source of truth — and [HOW_TO_RUN.md](HOW_TO_RUN.md) for commands.
> Kept for background on the original port only.

---

# Architecture — the end-to-end scheduling logic

How the engine in `pns-product-21golive/scheduling engine/v10` works, stage by
stage: what each stage is given, what it decides, the rule or formula it decides
it with, and what it hands on. Every rule below is one the plant stated; where
the engine enforces something the plant's own data does not support, it says so.

**Contents**

1. [The central idea](#1-the-central-idea)
2. [Data model and masters](#2-data-model-and-masters)
3. [Stage 0 — ETL and the effective plan](#3-stage-0--etl-and-the-effective-plan)
4. [Phase 1 — building plan to component demand](#4-phase-1--building-plan-to-component-demand)
5. [Phase 2 — WIP netting](#5-phase-2--wip-netting)
6. [Phase 3 — runs, lots and MPQ](#6-phase-3--runs-lots-and-mpq)
7. [Phase 4 — the DAG and backward CPM](#7-phase-4--the-dag-and-backward-cpm)
8. [Interlude — the calender is planned, not dispatched](#8-interlude--the-calender-is-planned-not-dispatched)
9. [Phase 4b — machine assignment as an optimisation](#9-phase-4b--machine-assignment-as-an-optimisation)
10. [Phase 5 — placement, and the dispatcher's rule stack](#10-phase-5--placement-and-the-dispatchers-rule-stack)
11. [The building gates](#11-the-building-gates)
12. [Containers and MHE](#12-containers-and-mhe)
13. [Phases 6 and 7 — the floor sheet and curing](#13-phases-6-and-7--the-floor-sheet-and-curing)
14. [The business-rule catalogue](#14-the-business-rule-catalogue)
15. [Invariants, guards and feedback loops](#15-invariants-guards-and-feedback-loops)
16. [Things that were tried and rejected](#16-things-that-were-tried-and-rejected)

---

## 1. The central idea

**Building is the anchor, and it is frozen.**

An earlier generation (v6) read a *curing* plan and invented a building schedule
from it. v7 inverted that: the building schedule is **given**, and the engine
plans only what is upstream of building. v10 is a fork of v7 that changes how
production is sized and how material is handed over, but keeps the anchor.

Three consequences follow directly, and most of the design is downstream of them:

1. **Machine-group assignment disappears.** The plan names the machine and the
   group on every row, so the engine never has to decide which drum builds a
   SKU. The routing is still needed for the second lookup — *which item* a SKU
   builds on that group — because only the routing knows it.
2. **Every deadline is absolute, not relative.** Build start times are fixed, so
   a component's deadline is a wall-clock instant, not a position in a sequence.
   That is what makes the backward CPM pass exact.
3. **Infeasibility is reported, not hidden.** If the kit cannot be there, the
   engine says which trolley, by how much, and what blocked it, rather than
   quietly re-planning around it.

The plant day runs **07:00 → 07:00**: shift A 07–15, B 15–23, C 23–07.

---

## 2. Data model and masters

### 2.1 The BOM is a tree, and the quantity semantics are unusual

`BTP_Master_BOM` is keyed `(Super_parent, Equipment)` — SKU and machine group —
so the *same* SKU built on BJ GROUP and on TWO STAGE TBM gets two independent
trees, because they build different green-tyre item codes.

```
<sku>
└── GT…              Green Tyres      <- built by a GT BUILDING plan row
    ├── TRD…         Tread
    └── CAR…         Carcass          <- built by a CAR BUILDING plan row, its own anchor
        ├── IL…      Inner Liner
        ├── SSW…     SideWall
        └── BEAD…    Bead Apex
```

**`child_quantity` is the PER-TYRE ABSOLUTE quantity, not a ratio against the
parent.** Verified: one SKU's recipe children under item T542 sum to exactly
3.046 kg, which is precisely what the Tread row asks for. So:

```
component demand = child_quantity × building_qty        (no chain multiplication)
```

Two things fall out of that, and they are why the traversal in `bom_walk.py` can
be as simple as it is:

- An edge's quantity depends only on its **child**, so a purchased raw material
  can be contracted out of the graph without disturbing any quantity.
- The cascade ratio for an edge P → C is just `per_tyre[C] / per_tyre[P]`, which
  is why phase 2 can net exactly even where an item has several parents (3.87%
  of item/tree pairs do).

### 2.2 Why the walk must stop at anchors

Both building rows are given. Each must claim **only its own** components, so
the walk from a built item **stops at any item that is itself a scheduled
anchor**. A flat `Super_parent == sku` filter would double-count every carcass
component against the GT row for all 78 two-stage SKUs. Single-stage groups
(BJ / VMIMAXX / UNISTAGE) have no carcass node at all — verified on all 219 such
SKUs.

Anchors are keyed per `(sku, MG)`: a GT row stops at the carcass of **its own**
sku+group, never at some other SKU's carcass.

The walk is memoised by `(sku, MG, built item)` — 138 distinct trees, 99.7%
cache hits — which is what makes 54,404 trolleys tractable.

### 2.3 Item types, aging and eligibility

- **Item type** comes from the BOM's `child_description`. `itemtype_master.csv`
  is deliberately *not* used: it is a stale superset covering items outside the
  effective-SKU scope. A built item that only ever appears as a Parent has no
  type, so the operation settles it — a GT BUILDING row builds "Green Tyres", a
  CAR BUILDING row a "Carcass".
- **Aging** (`min_aging`, `max_aging`) comes from `BTP_Master_Aging`. Min aging
  is maturity: material cannot be consumed before it. Max aging is shelf life.
  A missing row defaults to **master 15 days / final 5 days** — before that
  default, a missing row meant max aging 0, and the whole release-window
  calculation sits inside `if mx > 0`, so two codes received a 539 h window
  against a 40 h median.
- **Machine eligibility** comes from the routing alone; `all_machine_spec`
  carries the per-machine cycle time. Machines are **unrelated, not uniform** —
  mixer 0204 is 1.76× slow on one compound and 2.43× slow on another — so there
  is no per-machine constant that models the plant.
- **MPQ** is keyed `(Machine, Output, MHE, UOM)`. Resolution order is the
  machine-specific row, then an `(output, UOM)` family fallback, then the
  mixer's `batch_size` for compounds. **UOM is part of the key**: the sheet
  lists Sidewall as both 70 meter (CSTA) and 40 Nos (LT), and blending them
  gives a nonsense envelope.

### 2.4 Two meanings inside the MPQ sheet

Where `min == max` the row is a **container** (a trolley holds 40 treads, full
stop). Where `min < max` it is a **physical roll range** (a spool takes 20–230
plies; a mother roll 200,000–400,000 mm). Capacity is `max` in both cases, but
they must not be blended — for a container that is the container, for a roll it
is the biggest roll the machine will wind.

---

## 3. Stage 0 — ETL and the effective plan

### 3.1 ETL (`ETL_PIPELINE/`)

The database is the source of truth; nothing compares against a workbook or
prefers one. `etl_db.py` holds a single `REGISTRY` mapping the engine's input
names (`bom`, `routing`, `aging`, `mpq`, `changeover`, `building_plan`,
`curing_plan`, `inventory`) to table names, so the mapping is stated once.

`etl_extract.py` stages each table as an `.xlsx` in `db_staging/` and records
table, row count and timestamp in `_manifest.json`.

**Why stage files at all, rather than read the DB from the engine?** Because
reproducing last Tuesday's schedule is impossible against a live table. A dated
staging folder makes a run repeatable; it also decouples the ETL from the twenty
loaders in `inputs_v7`.

The three live tables — building plan, curing plan, inventory — are guarded:
`allow_empty: False`. A run staged from a zero-row plan produces a schedule for
nothing and reports success.

### 3.2 The effective plan (`data_validation.py`)

Runs **between** the plan extract and phase 1, and rewrites `building_plan.csv`
in place (keeping the original as `.full`).

A plan row whose components carry no `all_machine_spec` does not fail — it is
scheduled against nothing, waits forever, and lands years past the horizon. On
one run that was 15,328 lots sitting in 2027–2031 and a GT figure of 54% that
looked like a capacity problem and was not.

The test, per `(SKU, machine group)`:

1. Walk the BOM from `Super_parent = SKU`, `Equipment = group`, stopping **at**
   master compound — its children are raw materials the plant buys.
2. Every item found must resolve to a `routed_product` in the routing.
3. That routing row must carry `all_machine_spec`.
4. **BUILDING and CURING are exempt** — their times come from the plan itself.

Any component failing 2 or 3 disqualifies the whole `(SKU, group)`: a green tyre
cannot be built without all of its parts. The output states the shortfall up
front — e.g. "98 combinations planned, 81 schedulable, 256,254 of 280,457 GT" —
so it is a number you decide about rather than a surprise.

**Group-name aliasing lives here and in phase 1** and is load-bearing:

| Plan `Machine_Group` | BOM `Equipment` | Operation implied |
|---|---|---|
| `TBM STAGE1` | TWO STAGE TBM | CAR BUILDING (the carcass) |
| `TBM STAGE2` | TWO STAGE TBM | GT BUILDING |
| `PS` | VMIMAXX GROUP | — |

`PS` appears on 219 plan rows and exists nowhere in the routing's Equipment
column; without the alias it resolves to nothing.

---

## 4. Phase 1 — building plan to component demand

`phases/phase1_building_demand.py`

### 4.1 Reading the plan

The header row is **found**, not assumed: portal exports carry a varying number
of title rows above the real header, so the first row carrying at least 3 of the
4 required column names within the first 12 rows is taken as the header.
Column names are matched through an alias table, so the plant's own workbook is
read without an editing step.

Four row classes are filtered, and the distinction between them matters:

| Row class | Test | Treatment |
|---|---|---|
| **Changeover** | `SKUCode ∈ {CHANGEOVER, CO, SETUP, IDLE, -}` | dropped, but its **minutes are carried** onto the next production row of the same machine as `setup_min` (consecutive CO rows accumulate) |
| **Zero-qty** | `qty <= 0` | dropped, nothing carried |
| **Expired material** | `CO_Type ∈ {expired_carcass, expired_GT}` | dropped like a zero-qty row. **Dangerous rows**: they carry a real SKU and a real quantity, so a SKU-keyed test passes them straight through as production — 76 rows / 11,686 tyres on one plan, a whole month of upstream demand for tyres nobody will build |
| **PM / MTC** | `CO_Type ∈ {PM, MTC}` | dropped, and **held back from the setup carry**. A changeover *moves* with the production it sets up; maintenance does not — it is a named machine unavailable at a named time. Written out to `phase1_machine_downtime.csv` for phase 5 to block the machine with. Before this, 118 trolleys / 1,744 tyres were built inside PM windows |

### 4.2 Resolving each row to the item it builds

```
plan row  --(plan's own Machine_Group)-->  MG
(sku, MG, operation)  --routing-->  routed_product = the item actually built
```

**The plan names the group; the routing only confirms it.** This used to resolve
the group from the routing, so a routing whose `machines` column was empty made
every plan row unresolvable — exactly what happened on 17 Sep 2026: 1,653 rows
dropped as `unknown_building_machine`, 342,393 of 363,615 tyres gone from a
valid plan.

A cross-check still runs and warns when the plan's `Machine_Group` disagrees
with what the routing implies for that machine — a disagreement means the plan
and the master disagree about the plant layout, which would silently
mis-attribute the whole BOM.

### 4.3 The trolley split

The plant takes green tyres and carcasses off the drum onto a **trolley**, and
the MPQ sheet caps that trolley at **16 NOS** for both GT and carcass. So a plan
row is not one event:

```
plan row  547 units   2026-07-19 23:00 -> 2026-07-20 06:59   (479 min)
          unit_time = 479 / 547 = 0.8757 min
   T001   16 units    23:00:00 -> 23:14:00
   T002   16 units    23:14:00 -> 23:28:01
   ...
   T035    3 units    06:56:22 -> 06:59:00   remainder, ends exactly on time
```

Times are prorated across the row's own duration; the changeover is paid once,
on the first trolley only.

This is not presentation. A component feeding trolley 35 is genuinely needed
~8 hours later than one feeding trolley 1, so `need_by` gets that much sharper.
5,453 plan rows become **54,404 trolleys** and demand goes from 235 k to **2.36 M
rows**. A component ends up with a median of **126 distinct need-by times**, and
outside mixing (where the run hold collapses them) that granularity survives all
the way to the machine.

`V7_BUILD_MPQ` overrides the trolley size; raising it deliberately coarsens the
timing. `--no-trolley-split` disables it entirely.

### 4.4 The deadline

```
need_by = build_start − min_aging(component) − needby_shift_h
```

That is the **bare minimum**: the component finishes, ages exactly its minimum,
and lands the instant the build opens. Nothing absorbs a late machine, a queue
or a changeover. `V7_NEEDBY_SHIFT_H` (global) and `V7_NEEDBY_SHIFT_BY_TYPE`
(per item type) pull it back further — and that is **not free**: it makes phase
2's expiry test looser (admitting stock that dies between the new deadline and
the real build start) and moves every LFT back, releasing production earlier.

### 4.5 Wash-away compounds

TR100 is a cleaning purge. It sits in the BOM in exactly one place — as a child
of K4 — so its whole 22,610 kg of "demand" is reclaim inside K4's own recipe.
Two switches, and they do different things:

- `V7_BOM_EXCLUDE_ITEMS` removes the item's own demand row.
- `V7_BOM_STOP_AT` (default `TR100`) stops the walk **at** it, so its 82 child
  rows are never demanded either. It is then assumed always available, which is
  what a purge compound is.

Excluding without stopping would still demand TR100's recipe.

**Output:** `phase1_building_lots.csv` (the anchors), `phase1_demand.csv` (one
row per component × trolley), `phase1_unresolved.csv` (with the reason). The
unresolved file is **deleted** when a run has none, so a clean plan never looks
like it still has holes.

---

## 5. Phase 2 — WIP netting

`phases/phase2_wip_netting.py`

Two things this does that the previous generation could not: **per-batch
time-aware allocation**, and an **exact cascade**.

### 5.1 The stock pool

The snapshot is one row per production batch, carrying `produced_time` and that
batch's own MinAging / MaxAging (which beat the aging master — they describe the
material actually on the floor).

Three subtleties, each of which cost a real failure to find:

- **`child_code` first, not `item_code`.** `BTP_Live_Inventory` carries both;
  `child_code` is the engine's key, already resolved at source. Measured against
  the BOM's `child` column, `child_code` matches 8,796 of 8,796 rows and
  `item_code` matches 6,848. Asking for `itemcode` first netted a fifth of the
  WIP away with no error and no warning.
- **RP stock is pooled under the base code and aged under the RP entry.** RP is
  reprocessed material: physically it is the base compound and is consumed as
  such, but it ages differently — MT4444 needs 8 h before use, MT4444RP needs 0.
  Without this, 290 rows covering 21 real compounds are simply invisible.
- **Length items are normalised to MM**, matching demand. Inventory is in
  metres, the BOM in MM.

Batches are sorted **FEFO** (earliest expiry first) and numbered in the order
they will be drawn.

### 5.2 The opening clock

Planner, 13 Sep 2026: *"take all lots which are not expired at floor open — they
are usable, the rest are discarded. While consuming them, consider their
production time as floor open time for checking MAX aging, and their ACTUAL
production time for MIN aging."*

Two clocks, deliberately, because they answer different questions:

| Question | Measured from | Why |
|---|---|---|
| **Is it ripe?** (min aging) | the real `produced_time` | a batch made at 04 Sep 20:29 with 8 h maturity is ripe at 05 Sep 04:29. Physics does not restart because a plan does |
| **Is it dead?** (max aging) | **floor open** | whatever is in date on the rack at the bell is material the floor can pick up, and it gets its full shelf life from that moment |

A lot already dead at the bell never enters the pool, so nothing gets a fresh
clock it has not earned. A 96 h code opened 05 Sep 07:00 is usable to **09 Sep
07:00 and not a minute later** — measured, 0 of 152,311 draws past it.

`V7_WIP_OPENING_CLOCK` is the single rule that supersedes the two stacked gates
it replaced (`OPENING_ALIVE` + a separate `opening_ceiling`), which disagreed by
twelve minutes on 4.1 M units of one code.

### 5.3 When is a batch actually consumed?

This is the single most-revised decision in the engine, and the history is worth
keeping because both wrong answers are plausible:

| Test | Result |
|---|---|
| at the tyre's `build_start` | right only for a component that goes straight on the drum. MB184 is drawn when MB185 is *mixed*, ~24 h earlier |
| at phase 1's `need_by` | shallow — `need_by` ignores every stage in between. Recovered only 52% of what was recoverable |
| alive at **any** time its consumer runs | far too loose. One code's consumer runs 659 times over 11 days, so every batch found some run inside its 4-day shelf, passed, and then served demand six days after it died: **54,316 draws (33.2%) taken after expiry**, 7.8 M units, worst 393 h |
| the consumer run **at or after** this demand's need_by | too tight — when that run lands well after need_by the batch died in between |

**The settled rule splits the two questions:**

```
matured?   tested at draw_at   = the first consumer run for this item
alive?     tested at need_by   = THIS demand's own deadline
```

Maturity at one instant, so aging is charged once. Expiry against each demand's
own deadline, so a batch judged alive early cannot go on serving demand after it
dies.

Two supplied corrections make that test honest, and both are **previous-run
outputs**:

- `V7_WIP_CONSUMER_SCHEDULE` → a `phase5_schedule.csv`, giving the real times
  each item's immediate consumers ran.
- `V7_WIP_NEEDBY_FILE` → a `phase4_lot_times.csv`, giving the **chain-aware**
  deadline. Phase 1's `need_by` is shallow by the depth of the chain — one
  master compound's phase-1 deadline was 08 Sep 09:16 against a chain-aware
  05 Sep 11:47, 69.5 h late — so live stock was written off, phase 3 raised
  demand to *make* it, and one belt lot stalled 22.7 h, emptying the calender's
  whole C shift on day 1.

The chain correction **only ever moves a deadline earlier**, so it can never
admit stock a later draw would find dead. Neither file ever guesses: without
them, the behaviour is exactly as before.

### 5.4 Allocation and the cascade

Building lots are processed **by `build_start` ascending**, so the earliest
builds claim existing stock first — the plant's real behaviour. Within a lot,
the component tree is walked **top-down** in topological order, because a
parent's netting result changes how much of its children is still required.

```
req[item]   = own root demand + Σ over parents p of ( edge_qty(p,item) × qty × out_frac[p] )
out_frac[i] = (req[i] − wip_used[i]) / full[i]
```

`out_frac` is measured against the **full** requirement, so a child's share
scales by how much of its parent actually gets made. The saving flows down real
per-edge quantities — which matters precisely because some items hang off more
than one parent.

**Never netted:**

- **Anchors** (Green Tyres, Carcass) — they are the frozen building rows.
- **Cleaning compounds** (`V7_NO_NET_ITEMS`, default K4/MK3/TR100). A mixer is
  cleaned by *mixing* a batch through it; stock on a rack cannot clean a
  machine. This also broke monotonicity: recovering the RP stock covered 106
  cleaning batches from inventory, phase 3 made no lot for them, phase 5 still
  needed the clean and wrote a CLEAN UP row — which lands wherever the sandwich
  demands it, on the critical path. **GT fell 1,845 tyres on more inventory**,
  which is not a thing a plan may do.

**Output:** `phase2_demand_net.csv`, plus `phase2_wip_usage.csv`,
`phase2_wip_allocations.csv` (which batch, against which build, with shelf life
left **measured against `needed_by`**, never against the instant the rule tested
it — a guard that measures against its own reference marks its own work, and
did, passing a run clean while 8 of one code's 34 draws were post-expiry).

---

## 6. Phase 3 — runs, lots and MPQ

`phases/phase3_lot_sizing.py`

### 6.1 v7 rules, and what v10 changed

v7's three rules:

1. A lot is at least MPQ-min; demand below that is rounded up and the difference
   recorded as surplus.
2. A lot never exceeds MPQ-max. More than that is **separate lots, each with its
   own `lot_id`** — never a bigger lot.
3. A lot may only serve demand it can stay fresh for. A batch finishing at F
   serves a need at N iff `F + min ≤ N ≤ F + max`, so for the earliest need N₁
   and latest Nₖ in a bucket a feasible F exists **iff `Nₖ − N₁ ≤ max − min`**.
   That is exact, not a heuristic.

**v10's change (`V7_FLOW=1`), from the planner:** *"There is no lot sizing of
production — we can produce in one go whatever quantity is needed. Consumption
is in lots, and those lots are divided by demand and MHE capacity."*

| | v7 | v10 FLOW |
|---|---|---|
| production | lots sized by MPQ min/max | one continuous **run** per demand cluster |
| what bounds it | MPQ, then shelf life | the **consumer campaign window**, plus shelf life, plus physical roll maxima |
| MPQ-min rounding | always | **gone** — except the mixer, where "min" is the physical batch size, and the roll-making departments, where a 24 m roll is not a thing |
| splitting a bucket | **even** lots, to keep each above MPQ-min | **full containers, then one remainder** — the physical shape |

A run ends when the floor stops calling for the item for longer than
`V7_FLOW_GAP_H` (8 h). All lots of a run share a `run_id`, so placement can keep
them contiguous.

The building hop can be sized this way **because building is frozen**: the
consumer's machine and time are known at phase 1. A deeper hop, whose consumer
is itself scheduled, cannot use this definition unchanged.

### 6.2 The bucket window

```
window_h = max( MIN_BUCKET_H(8), (max_aging − min_aging) − AGING_SAFETY_H(2) )
```

then narrowed, in FLOW, by whichever of these applies:

- the item's own shelf life × `V7_FLOW_WINDOW_SHELF_FRAC`, or `V7_FLOW_WINDOW_H`
  for items with no shelf life recorded;
- a per-department cap (`V7_FLOW_WINDOW_BY_DEPT`, default mixing 72 h);
- for a **small compound** (≤ ~12 batches/day measured over its own demand span)
  the window grades itself as `max_aging × 0.5` — 82 of 139 compounds are mounted,
  run for six minutes and dismounted, carrying 127 h of the mixers' changeover.

**The window is what a run MAY span, not what it SHOULD.** A compound with a
360 h shelf and daily demand never trips the gap rule, so the whole month
collapsed into one run — 326 batches at worst — and the machine was encouraged
to work ahead on it. The measured answer, over five identical-input runs, was
**cap the mixers and leave the rest to freshness**:

| window rule | GT | cover | visits | setup |
|---|---:|---:|---:|---:|
| none (freshness only) | 99.61% | 0.48 | 25,793 | 3,221 h |
| 24 h everywhere | 99.25% | 0.43 | 31,139 | 3,526 h |
| 72 h on mixing, 24 elsewhere | 99.43% | 0.41 | 32,875 | 3,600 h |
| shelf / 2 per item | 99.49% | 0.47 | 33,776 | 3,605 h |

Shelf/2 lands in the worst corner and both halves push the wrong way: for a
15-day master, half is 7.5 days (barely a cap); for tread (36 h) and cap strip
(12 h) it is far too tight.

### 6.3 Lot arithmetic

```
n_lots = ceil(raw / hi)
target = max(raw, n_lots × lo)        # only where a floor is physical
per    = target / n_lots              # guaranteed inside [lo, hi]
```

Raising the **total** is what keeps `per` under the ceiling. Reducing `n` — the
obvious move — silently pushes lots above MPQ-max whenever `lo == hi`, which is
fixed roll sizes and mixer batches, i.e. most of this plant.

Demand is handed to lots **sequentially**: each lot takes a contiguous slice of
the bucket's demand in need order. That keeps the lot→demand graph sparse (one
pass, not a cross product) and gives every lot its own tight deadline window
instead of the whole bucket's — which is what backward scheduling needs.

### 6.4 Mixing MPQ — where the real fix had to go

The planner: *"MPQ on mixing is 4 batches."* Four attempts to enforce that in
the **dispatcher** all failed (+332, +1, +130, +608 tyres; sub-MPQ blocks stayed
at 26%), because the pick was never the cause — **the lot was that small.** Of
581 short blocks, only 3% resumed the same compound within two blocks, so they
were not mounts split by cleaning either.

Sizing the lot at four mixer charges in phase 3 removed the granularity
entirely: **573 sub-MPQ blocks → 0**, for 64 tyres, at the cost of +6.7% more
compound from rounding.

Two further details, each of which had bitten:

- **Two places set the mixing lot size.** `mpq_rules.py` and then a later
  override in phase 3 that took it back down to one machine batch. Setting only
  the first did nothing.
- **Size to the SMALLEST batch among eligible machines.** The MPQ comes from the
  routing's row-level `batch_size`, an average across machines. One compound's
  lot landed at 257.54 kg between a 215 kg and a 300.1 kg machine batch, and
  `ceil()` bought a whole extra batch: 81.8 h spent against 40.8 h of work —
  **41 h wasted on one compound**. The plan's own work content was half what the
  floor spends.
- **TR100 is exempt** — a purge is one charge, never four.

### 6.5 Deep rolls

FRC output is cut so each **pattern's** output lands on whole deep rolls. Two
traps, both handled:

- **The roll belongs to the PATTERN, not the item code.** The creel box is one
  size: CPJ1218 and CPJ1218_B370 are both pattern H and share one mount. A
  pattern's roll is the largest of its members'.
- **A deep roll is a length of belt in MM, on the calender.** Pattern resolution
  goes through `parent_of()`, so a *cutting* lot for a CPJ1218 belt also carries
  pattern H — and its quantity is in NOS. Adding those to the same running total
  made every "whole roll" come out 46–85 m short. The roll belongs to FRC alone.

A pattern whose whole month cannot fill `V7_FRC_DEEP_ROLL_MIN_FILL` (3) rolls is
**exempt** and produced to demand.

---

## 7. Phase 4 — the DAG and backward CPM

`phases/phase4_cpm.py`

Every production lot gets a window it must be made in:

| | Meaning |
|---|---|
| **LFT** | latest finish — any later and its consumer cannot start on time |
| **LST** | `LFT − duration` |
| **EST** | earliest start — any earlier and the lot **over-ages** before it is used |
| **slack** | `LST − EST`; negative means the lot cannot be made in time |

### 7.1 The backward pass, per building lot

Walking the item tree in topological order (**parents first**, because a child's
deadline derives from when its parent starts):

```
finish_by(item) = min over parents p of ( start_by(p) ) − min_aging(item)
start_by(item)  = finish_by(item) − duration(item)
```

with the base case `finish_by = build_start − min_aging` for anything hanging
directly off the built item. **Min aging is charged at every level**, which is
what stops the plan assuming material can be consumed the instant it is made.
A 10-minute transfer is charged on every producer→consumer link.

The step duration per item is the **longest** lot serving it, so the backward
pass never assumes an optimistic step.

**Why per building lot?** A component lot serves a contiguous slice of demand
(phase 3 fills lots sequentially), so on average only ~1.9 lots serve any
`(building lot, item)` pair. The expansion stays sparse, and every edge is
attributable to the tyre that caused it — which a DAG built on item codes alone
cannot do.

### 7.2 The early bound — the release window

```
EST = consumer_start − lead_h − duration
lead_h = min( max_aging, max( max_aging × shelf_frac, depth_days × 24 ) )
```

**Ageing drives the release window, not BOM depth** (planner, 27 Aug 2026).
Depth says where an item sits in the tree; it says nothing about how long it
keeps, and the two answers differ wildly — a 24 h tread and a 360 h compound at
the same depth were given the same lead.

`LEAD_SHELF_FRACTION = 0.40` is not a guess; it is the fraction measured and
settled for compound. **More lead is not free** — widening final compound to 0.9
took expiry from 0.5% to 15.4% and median age from 43.5 h to 93.3 h against a
120 h shelf. Per-type overrides exist for the mother rolls (0.75), the cut belts
and plies (0.40), and compound (0.40).

Note the asymmetry, and it has caught people twice: `max(shelf, depth×24)` means
**the shelf fraction can only ever LOOSEN a window.** Tightening has to come
from `LEAD_DAYS_BY_TYPE`, which is where the short-life cut items live at 0.5
days — a 24 h part at `depth × 24 h` was allowed to be finished a whole shelf
life before it was due, with zero margin, and the trolley then slipped a median
9–14 h.

**The opening ramp** (`RAMP_LEAD_H = 48`, `RAMP_LEAD_SHELF_FRAC = 0.9`): for
consumers building inside the first 48 h, the window opens as wide as the shelf
life allows. Day 1's kit is only 826 machine-h against 1,836 h available, so day
1 is not capacity-bound — but the ply cutter's first lot starts 31.8 h after the
floor opens, leaving 4.2 h to do 21 h of work per machine. The chain physically
allows it to start at ~8.6 h; the release lead is what held it to 30 h+.
Mid-month the tight lead is right — there is a backlog, and making early only
ages material. At the ramp there is no backlog, and the alternative to making
early is **not building at all**.

### 7.3 Edge quantities — conservation

An item with two parents used to be charged its **full** quantity inside every
parent iteration, so it promised twice what it makes: 8,969 of 68,016 producer
lots (13.2%) over-promised, worst on the mother rolls (99.3% over, median 2.00×).
One real roll making 389,649 MM had twelve edges promising 702,708 MM.

Now a parent's share is `bom_ratio(p, item) × (what this building lot wants of
p)`, normalised — so **the edges out of a lot sum to what the lot makes**, which
is the conservation any material model has to stand on.

### 7.4 The global relaxation

The per-building-lot pass is not yet consistent across the graph: a consumer lot
that also serves an *earlier* building lot has its own LFT pulled back, but its
producers were only tightened against the later one. A sweep of the lot DAG in
reverse topological order propagates every such tightening until

```
LFT(producer) ≤ LST(consumer) − min_aging(producer)
```

holds everywhere.

### 7.5 Max-aging conflicts

A lot whose `EST > LST` feeds consumers spread further apart than its own shelf
life: made early enough for the last one, it has already expired by the first.
Phase 4 **clamps** the window so placement stays possible and **flags** the lot
(`max_age_conflict`). 736 lots (0.38%), 713 of them master compound. The honest
fix is to split such lots in phase 3, which needs a feedback loop from CPM back
into lot sizing that does not yet exist.

---

## 8. Interlude — the calender is planned, not dispatched

`frc_campaign.py`, then `frc_aligned_needby.py`

The four-roll calender (FRC) is the plant's hardest bottleneck, and it is the
one machine the engine does **not** dispatch. Seven in-dispatcher approaches
were tried first and all seven failed.

**Why a dispatcher cannot do this.** It picks **one lot at a time**, and every
rule can only refuse or redirect that pick; nothing assembles a roll and then
places it. A 12,400 m roll of CPJ1218 is 31 lots whose release times spread over
a median 48.6 h, so the campaign runs dry at ~58% of a roll however the roll
rules are tuned.

**The planner's own method**, reproduced: sort FRC lots by needed-by; take the
earliest row's pattern; gather that pattern's lots in needed-by order until a
whole deep roll is assembled; run it in one go; then sequence the blocks by the
floor's rules. His day 1: 41,230 m in 1,218 of 1,440 min, only 150 min of
changeover, 222 min spare.

**How it works with the dispatcher rather than against it.** A lot can only be
picked once it is *released*, so the campaign order is written back as a
**release schedule**: every lot of a campaign gets the same release time, and
the next campaign is released at the moment this one would finish. The
dispatcher then cannot interleave them — nothing else is open to it.

Safety: LFT is never touched; a campaign may only gather lots whose need-by is
within the look-ahead of its first lot (wire 96 h, fabric 72 h — a wire roll is
far more lots); and the gathering window is **the item's own shelf life**, not a
flat wire/fabric number (one wire code lives only 72 h, and a flat 96 h let the
builder gather past the point the material dies).

**`frc_aligned_needby.py` closes the loop.** Phase 4 ran *before* the campaign
plan existed, so every compound was aimed at the deadline the belt **cutter**
implied. Measured, every one of 2,648 FRC lots ended up running earlier than the
deadline phase 4 had handed mixing — a median 66 h earlier. This walks back up
the lot DAG from the campaign, charging each item's own min aging at every
level, and rewrites the upstream deadlines. **The pin travels upstream only**:
cutting, building and curing keep the building plan's dates.

Measured: 11,748 upstream deadlines pulled in by a median 34 h; mixing lots with
no slack left go from 63 to 3,740 (9% of 42,874) while the **median** mixing
slack barely moves (54.5 h → 54.3 h). It sharpens the 9% that FRC genuinely
needs earlier without squeezing the other 91%.

---

## 9. Phase 4b — machine assignment as an optimisation

`phases/assignment.py`

Assignment used to be a side effect of phase 5: it ranked machines by load and
charged each the routing's **row-level** `proc_time`, which is the average across
every machine that row lists. Phase 5 then re-timed each lot at the chosen
machine's real speed — so the balancer levelled numbers that were never
comparable, and nothing went back to fix it.

Measured on August: **2,285.7 h of an 18,120 h schedule — 12.6% — is work sitting
on a machine slower than a legal sibling.** Mixer 0204 finished at 99.0% on
*fewer* lots than 0205/0206 while those sat at 67.9% and 71.0%, because 0204 is
the slowest mixer in the plant and the balancer could not see it.

**What it optimises.** The plant's KPI is 95–105% of planned GT **every day**,
not a good monthly total. So the objective is load per **(machine, day)**,
bucketed by each lot's LFT:

```
primary   : total hours above the daily cap, summed over (machine, day)
secondary : total work hours          <- where the 2,285 h is won
tie-break : keep an item on ONE machine  <- every extra machine is a changeover
```

Two caps, and the second is not implied by the first: `DAY_CAP_H = 24 × 0.95`
and a **month** cap (`31 × 24 × 0.95`) — work spread thinly over 31 days never
trips a daily cap and can still bury a machine for the month. Leaving the month
cap out took the first version of this phase to GT 79.4%.

**The unit of assignment is the ITEM**, not the lot, so a code costs one
changeover on one machine. Belt codes slit from the same pass are grouped
further — they are one operation on one roll and cannot be in two places. A
group may only go where **every** member is legal (the intersection); if that
comes out empty, its members are assigned individually rather than stranded.

**Greedy, then local search.** The greedy pass is order-dependent, so up to 8
sweeps move whole items off the worst `(machine, day)` while a legal sibling has
room, keeping a move only if total overload falls.

**Preference ranking** (`V7_RANK_PREF`) sits **above** the mixing history and
**below** capacity, so a preference can never overload its machine. The rank is
**not in the routing**: 100% of the routing's 16,709 multi-machine mixing rows
are in plain ascending numeric order, so scoring on it means "prefer the
lowest-numbered mixer". The real preference is the plant's own production log —
one row per compound, one column per mixer, the share of batches each actually
ran. 50% of 401 compounds run 100% on one machine; final is 98.1% on two mixers.

---

## 10. Phase 5 — placement, and the dispatcher's rule stack

`phases/phase5_placement.py` orchestrates; `phases/dag_schedule.py` (8,474 lines)
is the dispatcher.

### 10.1 Why placement runs over the graph

Sequencing each machine on its own let consumers start before their producers
finished (**53.8% of edges**) and left material expiring before use (**67.3% of
links**). Both have the same root cause: machine order was decided without
consulting the graph. So placement runs over the precedence DAG in two passes.

### 10.2 Pass 1 — forward, feasibility

Lots are released in topological order; a lot is only placed once every
predecessor is placed. Its start is floored at:

```
start = max( floor_open,
             max over predecessors p of ( finish[p] + min_aging(item[p]) + 10 min ),
             machine_free + changeover,
             release window (EST),
             material_ready )
```

which makes precedence and minimum aging true **by construction** rather than by
hope.

**Ready work is held per machine and bucketed by item.** A single global queue
ordered by `(campaign, item)` *looks* like it campaigns, but the lot it pops is
whatever is globally next — not what **this** machine is set up for — which
tripled setup time (756 h → 2,828 h) the moment precedence ordering came in.

**Two clocks must agree.** The releasability filter used to test `est` alone
(phase 4's planning window) while the commit started the lot at `earliest` (when
its material genuinely exists). When `earliest > est` the lot passed the filter,
could not run, and the machine **committed to it and stood idle** for the
difference while other work sat ready — traced on one calender, seven hours lost.

**Never-idle, and its guard.** A machine with nothing releasable may steal work
from a neighbour or reach forward (step-up). But `V7_NI_REQUIRE_READY=1` is
essential: on one run the full-width slitter correctly refused a 24 h-shelf lot
that would expire by 37 minutes, then took a 72 h one **whose material did not
arrive for 43.3 hours** — and committing booked the machine through the whole
wait while 485 released, material-ready lots queued behind it. The code's own
comment says it: *"Grabbing one of these does not avoid the idle, it commits the
machine THROUGH it."*

**Double placement.** `dag_schedule` refuses to place a lot that already has a
start. Before that guard, every mother-roll lot on one slitter was popped and
committed **twice**, 3:20 apart; each commit advanced `m_free` by the full run
time, so the machine paid twice for work it did once — a 50% duty cycle for 639
consecutive lots.

### 10.3 The rule stack — how a pick is actually made

Roughly sixty rules sit between "the machine is free" and "this lot runs". They
are applied in three distinct positions, and **the position is the design
decision**:

| Position | Why | Example |
|---|---|---|
| **Candidate filter** (before any pick rule) | ~30 later rules re-pick from `buckets`; a preference at the pick site gets silently undone | the mixing daily ceiling; the one-visit cap; the calender profile hold |
| **Pick preference** | genuine ranking among legal candidates | campaign hold, coverage, urgency |
| **Last word** (after everything) | the rule must not be negotiable | one-visit-per-shift; the mixing MPQ floor; whole-roll FRC |

This was learned expensively. The calender profile rule was first implemented at
the pick site: it fired 32 times and moved profile changes 633 → 615, a 2.8%
dent on a 328 h problem — because twenty-odd later rules reassign `pick_item`
and every one of them undid it.

The main families:

**Continuity and visits.** A compound the mixer has already finished with this
shift is *closed*; its remaining batches wait rather than dragging the machine
back for a second mount (`V7_ONE_VISIT_DEPTS`, `V7_MAX_VISITS_PER_SHIFT`). There
are two escapes — a deadline inside 2 h, or nothing else ready — and one
exception to the escape: a shift requirement under 24 batches is never split,
because re-opening a spent visit for one batch is worse than idling briefly.

**Campaign hold.** `hold = switch_cost × gain`, ceilinged per department. The
ceiling is not cosmetic: the plant's matrix prices a tread↔sidewall die change
at 50 min, and `50 × 600 = 500 hours` — longer than the month — so urgency
switched off entirely, the machine never left its mounted type, and **GT fell
99.5% → 45.1% even though extruder setup dropped 1,399 h → 935 h.** Every GT
trolley needs tread *and* sidewall; a machine that will not alternate starves
the kit no matter how little setup it books. **Setup hours were never the
objective.**

**Step-up.** Any machine that would otherwise sit idle may reach forward for
work it is allowed to do, bounded by **the shelf life of what it makes**, per
department: compound 4 days, mother rolls 2–3 days, bead 1.5, the 24 h cutters
0.75. The cutters are deliberately *not* raised to their full 24 h — measured,
GT 96.6% → 96.3%. Their idle is the shelf life, not a scheduling gap: a belt cut
any earlier expires before its build.

**Run hold.** Every container of a run takes the run's **earliest** EST, so the
run becomes releasable as one block and the campaign hold keeps the machine on
it. Only 34.6% of multi-container runs were contiguous without it, with a median
of 20 lots of *other* items sitting inside a run. What is **not** touched:
`mat_ready` and LFT — so this can never schedule production before its inputs,
and urgency still orders the queue. It moves only the artificial half of the gate.

The bound on it is **physics, not a fraction**: a lot may not be made so early
that it expires before its own consumer —

```
earliest safe finish = consumer_start − max_aging,  consumer_start = LFT + min_aging
```

A fraction of the lot's own EST measures the pull from an already-early point
and has nothing to do with when the material goes off; for final compound it was
32 h **tighter than physics** against a 70 h release spread, so a 16-lot run
could never gather and ran as six blocks of five.

**FRC sequencing** is the largest single family — creel gates, wire/fabric
alternation, deep-roll wholeness, chafer placement, the CAP 66 opener. See §14.

**Physical constraints** — no two lots overlap on a machine (0 violations on 47
machines at one-second resolution); a machine never runs through its PM window;
nobody works on a plant holiday; a belt pair is one pass off one roll; a die is
mounted once per shift.

### 10.4 Pass 2 — backward right-shift (JIT)

The forward pass makes everything as **early** as possible, which is exactly
what expires material. So each lot is slid as **late** as it legally can go, in
reverse topological order:

```
latest_finish = min( LFT,
                     min over successors s of ( start[s] − min_aging ),
                     start of the next lot on this machine − changeover )
```

Nothing moves earlier and nothing crosses a successor or a machine neighbour, so
pass 1's feasibility is preserved while the wait before consumption — the thing
that causes max-aging violations — is squeezed out.

**The calender does not move**: its times come from the campaign plan as input.
A separate pass walks each calender's lots **in plan order** and pushes any that
start before the previous finished — and it **preserves the changeover**, because
the gap the plan left between two lots *is* the changeover, computed from the
floor's own table. Pushing only far enough to stop overlapping treated that
reserved setup as free air: 54 of 364 transitions ran short, including 8
fabric→wire changes with **zero** time to re-thread the creel.

### 10.5 Result is feasible by construction on…

precedence, minimum aging, and machine capacity. **Max aging is improved but not
guaranteed**: if the graph itself forces a long wait — a shared compound feeding
consumers weeks apart — no sequencing can fix it, and the residue is reported
rather than hidden.

---

## 11. The building gates

Applied in `phase5_placement.main::apply_building_gates`, re-applied after every
feedback round.

### 11.1 The full-kit gate

```
kit_ready(trolley) = max over components c of ( finish(c) + min_aging(c) )
```

A trolley cannot start until **every** component is finished *and* has served
its minimum aging. `blocking_lot` / `blocking_item` name the one that bound it.

- `--policy shift` — the trolley moves to `kit_ready` and the move is recorded
  as a revision.
- `--policy frozen` — the trolley does not move and the gap is reported as a
  shortage.

`V7_BUILD_EARLY` (off by default) lets a trolley whose kit is ready *before* its
slot be pulled forward, capped by green-tyre shelf life. It is off because it
changes the plan's own sequence, which exists for reasons the scheduler cannot
see — press and mould assignment, despatch order. Measured with it off: **not
one of 40,224 green tyres was built early** and 97.1% were built late by a
median 25.8 h, while days 1–5 ran at 82% of plan and days 11–30 at 102%.

### 11.2 The carcass → GT gate

A green tyre on TBM stage 2 is built **from** a carcass made on stage 1. The BOM
walk deliberately stops at the carcass (so its components are not double-counted
against the GT), which means this dependency is **not in the component kit** and
has to be applied separately — otherwise every two-stage GT looks feasible the
moment its own components land.

**Matching is cumulative, not trolley-to-trolley**: carcasses sit on a rack, so
the n-th green tyre needs the n-th carcass, whichever trolley it came off.
Opening carcass stock enters as supply at plan start — but **it perishes**. Only
the part the presses can consume inside `plan_start + max_aging` counts; the
rest is written off, exactly as on the floor. The pairing is written out
(`producer_lot → consumer_lot`, hop `CARCASS->GT`) so the aging audit and the
DAG do not each re-derive it and get slightly different answers.

### 11.3 The machine re-sequence, and the plan-day budget

Shifting trolleys individually ignores the machine — three trolleys of one plan
row landed on one drum at the same instant. A building machine builds **one
trolley at a time**, so after all gates have moved things, each machine's
trolleys are laid end to end in planned order, starting at
`max(own feasible time, machine free)`. The SKU-change setup the plant's own
plan states (`CO_Mins`) is charged; closing the gaps without paying it invents
capacity the floor does not have.

**The plan's shift pattern is a capacity limit** (`V7_PLAN_DAY_CAP`, on by
default). `machine free` alone treats every machine as available 24 h a day, so
backlog is poured into hours the plan never gave. Measured: the month matched
almost exactly — 13,626 building hours against a planned 13,806 — but the
**distribution** did not. 5 Sep ran 69% of its planned hours and 11 Sep ran
115%; one machine given 4.8 h by the plan ran 21.7. Each machine is therefore
budgeted **by plant day** from the plan's own build windows. This cannot invent
capacity and cannot exceed the plan — it only stops the peaks the dips are
borrowed into.

Plant holidays are applied here too, because building takes its slot from the
plan and never reaches the dispatcher's holiday block: 12,330 green tyres were
reported on a day the plan itself marks `Holiday = Y`.

---

## 12. Containers and MHE

Two separate models, for two different materials.

**`phases/mhe.py` — compound leaves mixing on a carrier.** A carrier is filled
with **up to 4 whole batches of one compound**; a batch belongs to exactly one
carrier, permanently. It serves **one machine at a time for the whole of that
machine's run** — the exclusivity is temporal, not permanent. It need not be
full to move and need not be emptied at one machine: a remainder **travels with
it**. Expiry is per batch against the draw that consumes it, and because batches
go oldest-first, the batch that must survive to the end is the **newest** one.

This also makes mixer campaigning *structural*: four batches of one compound on
one carrier means a-a-a-a-then-b is a consequence of the container, not a
sequencing preference a step-up can quietly bypass.

Two earlier attempts each satisfied half the rule: whole batches per
(compound, machine) rounded every carrier up and invented +2,461 batches (9.4%
of all mixing); free quantity draw let one carrier slice a little off many
batches, so 57.8% held more than four (worst eighteen). **The fault in both was
treating the carrier as a per-machine bucket.** It is a physical object: fill it
once with whole batches, then move it.

**`phases/containers.py` — the building hop.** Same four rules, sized from the
MPQ sheet's own MHE column: one container serves one machine at a time; only the
boundary container is partial; the remainder travels; a consumer may wait up to
`--wait-h` before a fresh container is opened and the event is recorded as a
**forced split** — the price of the tolerance, stated rather than hidden.

It runs **after phase 5** because whether two consumers collide depends on which
machine each is on and when, and neither exists until placement has happened.

**Scope limit, stated plainly:** the container model covers the building hop and
compound. **It does not cover the cutters** — 0 of 250,952 draw events have a
cutting consumer, so a cut has no modelled consumption moment. "One lot, one
machine" and the roll-drain rules therefore run on a **proxy**: the consuming
lot's own start/finish stands in for "the roll is on the stand". A mother roll
is not an object in this model — no mount, no remaining metres, no dismount.

---

## 13. Phases 6 and 7 — the floor sheet and curing

### 13.1 Phase 6 — the floor schedule

Turns nanosecond timestamps into what a supervisor reads: **one** table covering
the whole chain — mixing, calendering, extrusion, cutting, slitting, bead **and**
the building trolleys — split by plant shift.

A lot crossing a shift boundary becomes several rows with quantity prorated by
time, and NOS quantities use **largest-remainder rounding** so the split rows sum
back to exactly the lot quantity, never 15 + 15 for a lot of 31.

`consumer_machine` names the **immediate** consumer by machine name, semicolon
separated where a row feeds several — and the **building group** (BJ, TWO STAGE
TBM, VMIMAXX, UNISTAGE) where a lot goes straight to the drum, because that is
how the floor talks about building.

Reporting conventions that are part of the contract:

- Mixing is counted in **batches** — one charge of the vessel — derived from the
  row's own kilos ÷ one charge, never a flat number.
- On the floor sheet `produce_qty = batches = charges`, and `lots = 1` on a
  lot's first row and 0 on the rest. So summing `batches` gives charges and
  summing `lots` gives lots. (`batches` used to hold the lot marker, which read
  `produce_qty 4` beside `batches 1` on the same row.)
- A lot straddling a shift is two rows that sum to the lot; the mixer does not
  stop — gap is 0 s in all 197 splits.
- Inventory is in metres, the BOM in MM.

### 13.2 Phase 7 — curing

The curing plan is the plant's own frozen file, exactly like the building plan,
and is never rewritten. A shortfall shows up as a press lot landing on a later
day, reported as a revision.

Two gates:

1. **Green-tyre supply**, cumulative per SKU — the same rule the carcass gate
   uses one level down. Supply is GT opening stock (shelf-life bound, perishes)
   plus every GT trolley at `feasible_start + span + min_aging`.
2. **Press capacity.** Once a lot is pushed for GT, everything behind it on that
   press is pushed too, honouring the changeover and mould-clean minutes the plan
   carries. A press cannot cure two tyres at once.

The month horizon is **derived from the plan**, not fixed: 07:00 on the 1st of
the following month. A July constant left in place for an August plan puts every
lot past the horizon and reports 0% cured, which reads like a scheduling failure
rather than a stale constant.

---

## 14. The business-rule catalogue

Grouped by department. Every rule is as the planner stated it; the parameter
name is given so it can be found. Where the plant's own data disagrees with a
rule the engine enforces, that is stated — those are the honest gaps.

### 14.1 The plan

| Rule | Implementation |
|---|---|
| The building plan is an input | phase 1 reads it; nothing reschedules building |
| The plant day runs 07:00 → 07:00 | A 07–15, B 15–23, C 23–07 |
| A component's deadline | `need_by = build_start − its own min aging` |
| Every deeper level | `deadline = earliest consumer's deadline − own min aging − 10 min transfer` |
| Holidays | `V7_PLANT_HOLIDAYS`. **Mixing observes it; `frc_campaign.py` does not** |
| Two-stage tyres | `CAR BUILDING` is stage 1 and is **not** an extra tyre. Count GT BUILDING only — 542,612, not 658,318 |

### 14.2 FRC — the four-roll calender

**Deep rolls**

| Rule | Detail |
|---|---|
| Seven codes must make a whole deep roll | `V7_FRC_ROLL_CODES` — ECOPOLY1500, BJNYL27, CAP 66, UNIHT1330, EHT1000, CPJ114, CPJ1218 |
| Everything else | makes the next 2 days of demand (`V7_FRC_DEMAND_DAYS`) |
| **The roll belongs to the PATTERN, not the item code** | the creel box is one size; a pattern's roll is the largest of its members' |
| Roll sizes | H 12,400 m · B 3,480 · D 3,000 · J/A/O/L 2,000 |
| A campaign may reach forward | 96 h wire, 72 h fabric, never further |
| A campaign may only start if its first lot is wanted within 36 h | `V7_FRC_CAMPAIGN_MAX_LEAD_H` |
| The gathering window is the item's own shelf life | not a flat wire/fabric number |

*Measured, ph78:* 236 of 242 pattern blocks on a whole roll ±500 m — **98%**,
against the plant's own **91%**.

**Sequencing**

| Rule | Detail |
|---|---|
| Two wires of different pattern need a 6 h gap | the creel change is physical — `V7_FRC_WIRE_GAP_H=6.0` |
| That gap must be filled with fabric, at least 5 h of it | `V7_FRC_FABRIC_GAP_H` |
| The gap cannot be bought out | `V7_FRC_WIRE_GAP_LOOKAHEAD_H=0`, `_CRITICAL_H=-1` |
| CAP 66 runs first coming off wire | `V7_FRC_AFTER_WIRE_FIRST` |
| No limit on wire per day, no 24 h repeat gate | the planner: *"if these rules are getting followed then nothing else is required"* |

*Measured:* 0 of 48 wire changes under 6 h; minimum 7.00 h, median 8.05 h.
**Two gaps against the rule as stated:** 14 of 48 gaps carry under 5 h of fabric
(worst 2.59 h in a 7 h gap ≈ 4.4 h of dead calender); and the plant itself
breaks the 6 h gate on 17 of 78 pattern changes, so it is the plant's **norm**,
not its hard constraint — enforcing it absolutely costs ~21,000 tyres.

**Chafers** — priority order as the planner set it: *TL and NYL never touching
outranks running at the block end.*

| Rule | Detail |
|---|---|
| **TLCHAFER and NYLCHAFER never run back to back** | absolute. Confirmed: 51 chafer mounts over 40 days, **zero** adjacencies |
| Chafers run only at the fabric end, before a wire | never mid-block |
| The closing sequence | `NYL · WPJ011 · TL` or `TL · WPJ011 · NYL` |
| If no separator at all | run **one** chafer only |
| **NYLCHAFER and WPJ011 are one creel (pattern Q) and are made together** | they stay in ONE campaign; the closer orders the codes *inside* it. Splitting Q by item code took pairing 19/19 → 2/12 |
| Chafer lots sized by need-by, floor 200 m | `V7_FRC_CHAFER_MIN_M` |
| Both chafers every plant day | `V7_FRC_CHAFER_DAILY=1`. **The plant does this on 15 of 40 days** |
| A wire never waits for a chafer | |

**Changeover**

| From → to | Minutes |
|---|---:|
| fabric → fabric | 0 |
| fabric → fabric where either side is CAP 66 | 15 |
| wire → fabric | 30 |
| fabric → wire | 60 |
| wire → wire, different pattern | 360 (the 6 h gap, containing the 60 min re-thread) |
| wire → wire, same pattern | 0 |

*Measured:* 0 short in 2,626 transitions — every category meets its rule at the
**minimum**, not just the median. **The CAP 66 15-minute setup has no support in
the plant data** (104 transitions, median gap 0 min) and is charged 70×/month.

FRC output goes to seven machines and no building group: wire → the WBCs;
CAP 66 / CAP30 → the full-width slitter; the chafers and WPJ011 → Mid Land
Cutter; ply → HT/LT.

### 14.3 Mixing

The planner's own description: *"I pick the most urgent item and produce all its
lots in one go — but only the batches equal to the **average daily requirement**,
not 3 or 5 days… If another upcoming compound has very little slack I make only
4 batches of the current one, then start the critical one now."*

| Rule | Parameter | State |
|---|---|---|
| **MPQ is 4 batches** — applied at **lot sizing**, not in the dispatcher | `V7_MIX_MPQ_BATCHES=4` | in |
| TR100 exempt (a purge is one charge). **K4 and MK3 are NOT** — they clean but are also real compounds | `V7_MIX_MPQ_EXEMPT=TR100` | in |
| **A compound is mounted once per shift**, cleaning excluded | `V7_ONE_VISIT_DEPTS`, `V7_MAX_VISITS_PER_SHIFT` | in, leaks 5.4% |
| **Under 24 batches of demand in a shift is never split** | `V7_MIX_NO_SPLIT_SHIFT` | built, **off** — cost 9,228 tyres |
| **Never more than 1.0–1.2× one day's average**, averaged over the days that compound is wanted | `V7_MIX_DAY_CAP_X` | 1.0 in ph78 |
| Block ceiling 60 batches | `V7_MIX_MAX_BLOCK` | **inert — never fires** |
| Changeover between compounds | **2 minutes** — a cleaning batch *is* the changeover | in |
| A final waits for its master: finish + min aging + 10 min | | in, **0 violations in 162,668 draws** |
| IL3+IL5 cleaned with K4; ML405+ML406 with MK3 | `V7_SANDWICH_RULES` | one machine at a time, may swap between shifts |
| master→final cleans with TR100, final→master with MK3 | `V7_TYPE_CLEAN_RULES` | cascades after the item sandwich |
| A compound may only run where its own history allows | `V7_MIX_PCT_FILE`, `V7_MIX_PCT_MIN` | from the production log, never the routing |

**The day cap's denominator matters more than its multiplier.** Averaging a
compound's month over 26 calendar days instead of **the days it is wanted** gave
140 compounds a median cap of **2 batches/day** and cost 19,767 tyres.

**Validated against 244,747 batches of the plant's own production:**

| | Plant | ph78 |
|---|---:|---:|
| Batches per day | 2,423 mean, 2,481 median, **max 2,790** | median 2,037, max 3,193 |
| Median run | **18** | 12 |
| Largest run ever | **122** | **236** |
| Runs over 60 | 0.2% | 8.5% |
| Runs of 1–3 | 8% | **0** |

**The plant is far stricter about placement and far looser about eligibility.**
On one day every B-final ran on one mixer, all masters on two others — but
**every machine ran every day**. The engine's 0206 carries 9–13 compounds where
the routing allows 79, and sits idle 4–8 days. That asymmetry is the cause of
both the over-long blocks and the idle mixer; it is not a cap failure.

**Mixing is aimed at a buffer, not at a roll.** Daily correlation between a
compound and the FRC code it feeds is weak (0.27 same day, −0.38 at D+1). The
calender runs wire in alternating bursts while the compound is mixed almost
every day.

### 14.4 WIP and material

| Rule | Detail |
|---|---|
| **Expiry is tested at the moment the immediate consumer draws** | not when the tyre is built |
| **Expiry is tested against each demand's own need-by** | "alive at *any* time its consumer runs" let 54,319 draws (33.2%) take dead stock |
| Stock in date at 07:00 on plan start is usable | `V7_WIP_OPENING_ALIVE=1` |
| **…but only until plan start + that item's own max aging** | measured: 0 of 152,311 draws past it |
| Opening stock counts as matured at 07:00 | `V7_WIP_OPENING_MATURED=1`. Compound made during the plan still ages per lot |
| We cannot use expired material | absolute |
| A missing aging row: master 15 days, final 5 days | `V7_DEFAULT_MASTER_AGING_H` / `_FINAL_` |
| **The WIP feedback files must point at a CURRENT run** | pointed at a stale run, phase 2 believed one code was not wanted for 22 h, and **FRC lost the whole of shift C on day 1** |

Standing guard: `check_wip_expiry.py` re-tests everything phase 2 discarded,
from the run's own outputs — so it still fires if the test is ever changed back.

### 14.5 The floor

| Rule | Detail |
|---|---|
| No two lots overlap on a machine | **0** on 47 machines, at one-second resolution |
| A machine never runs through its PM window | `V7_MACHINE_DOWNTIME_FILE` |
| A lot starts at `max(planned_start, material_ready)` | `material_ready = producer finish + min aging + 10 min` |
| Never-idle must not take material that has not arrived | `V7_NI_REQUIRE_READY=1` |
| A lot is never placed twice | |
| A belt never idles more than 20 h | `V7_BELT_MAX_REVISIT_H` |
| Slitters making the same code share the work | `V7_SPLIT_ITEMS` — stickiness is off for forced splits, or the incumbent bonus wins every tie and one twin runs nothing |
| belt1 + belt2 go to the SAME cutter, run together, no changeover between them | one roll, one pass, 100% of pairs |
| One die mount per shift, strict | `V7_DIE_STRICT` — the die is read from the extruder changeover matrix itself |
| One roll, run to the end | a cut comes off ONE roll, twins off the same roll, a roll drained before the next is opened |

### 14.6 Rules enforced that the plant's data does not support

1. The CAP 66 15-minute setup — 104 transitions, median gap 0 min.
2. The 6 h creel gate as absolute — the plant breaks it on 17 of 78.
3. Both chafers every day — the plant does it on 15 of 40.
4. Days 1–2 planned above the plant's own ceiling — 3,115 and 3,073 batches
   where the plant has never exceeded 2,790 in 101 days.

---

## 15. Invariants, guards and feedback loops

### 15.1 Invariants the engine holds by construction

| Invariant | Where it is made true |
|---|---|
| A consumer never starts before its producer finishes + min aging + transfer | forward pass floor |
| No two lots overlap on a machine | `m_free` + changeover, and the FRC no-overlap pass |
| Edge quantities out of a lot sum to what the lot makes | phase 4 normalised parent weights |
| Building quantity is conserved through the trolley split | 836,453 units, verified |
| Lots never exceed MPQ-max | phase 3 arithmetic raises the total, never reduces `n` |
| Nothing is produced inside a PM window or a plant holiday | downtime blocks + holiday push |

### 15.2 Guards that exist because the obvious version failed

- **`check_rules.py`** reads a run's **output**, never its settings. Settings
  lie: an empty env var reads as "on" and is off.
- **`check_wip_expiry.py`** re-derives the expiry test from the run's own
  outputs rather than from the netting code.
- **The audit column must not share the rule's reference time.** `shelf_life_left_h`
  is measured against `needed_by`, not against the instant `allocate()` tested
  the batch at — the latter made the check mark its own work and it passed a run
  as clean while one batch served demand 39 h after it died.
- **`chk` in the run scripts** — an input resolved by glob fails silently, so
  every phase's output file is asserted non-empty immediately after it runs.
- **Deleting `phase1_unresolved.csv`** when a run has none, so a clean plan does
  not inherit the previous run's holes.
- **`push_schedule.py` replaces, never appends** — re-running the same
  (plan_id, run_stamp) would otherwise silently double every quantity.
- **One run at a time**, enforced by a module lock in `scheduler_api.py`.

### 15.3 The three feedback loops

The pipeline is not a straight line. Three arrows point backwards, and each
exists because a stage needs information that only a later stage produces:

1. **Phase 5 → phase 2**, across runs. Expiry must be tested at the moment the
   immediate consumer draws, which only exists once something is scheduled.
   Supplied by `V7_WIP_CONSUMER_SCHEDULE`.
2. **Phase 4 → phase 2**, across runs. The chain-aware deadline is computed by
   phase 4, which runs after netting. Supplied by `V7_WIP_NEEDBY_FILE`, and it
   only ever moves a deadline **earlier**.
3. **FRC campaign → phase 4**, within a run. `frc_aligned_needby.py` re-dates
   upstream deadlines onto the campaign the calender will actually run.

A fourth loop is **missing and known**: phase 4's max-aging conflicts should
feed back into phase 3 lot sizing so such lots are split rather than clamped.

### 15.4 Scoping outputs

A run writes to `outputs/<month>/<run>/`, and `outputs/<month>/LATEST` names the
newest completed one. The two levels answer different questions — *which plan*
and *which attempt* — and collapsing them has caused both failures: one run's
phase 4 reading another month's phase 3 produced a schedule that looked
catastrophic and was simply the wrong month's data.

---

## 16. Things that were tried and rejected

Recorded so they are not rebuilt blind. Each was measured.

| Attempt | Result |
|---|---|
| **Pushing a closed compound to another mixer** | moved 7,852 lots and collapsed the plan, GT 99.7% → 75.6%. The target was chosen by ready-backlog, which is not load |
| **Pinning compounds to a machine by history** | GT −3.5, carcass −6.4; the pin had no capacity cap and packed one mixer to 104% |
| **Shelf/2 as a global run window** | worst of both — most visits AND most standing compound. Right only for low-volume compounds, where it is now used |
| **The critical 4 h FRC wire gap** | fired on 60 of 88 changes with a "will it miss" test, making the exception the rule |
| **Smaller lots for short-shelf items (0.25×)** | GT 96.6% → 88.2%, expired 63% → 83%. Splitting a lot does **not** spread production in time: same-item lots cost zero changeover, so the scheduler campaigns the fragments back to back at the same front-loaded moment |
| **The cap-strip lead exemption** | released up to 96 h ahead against a 24 h shelf; 9,869 of 9,932 lots already expired before anyone touched them |
| **Mixing planned as its own line** | 343,063 tyres vs 508,761; days 8–13 built nothing |
| **Four dispatcher-side fixes for sub-MPQ mixing blocks** | +332, +1, +130, +608 tyres; sub-MPQ stayed at 26%. The lot size was the cause, not the pick |
| **No-split-in-shift at ≤24 / ≤12 batches** | −9,228 and −4,719 tyres |
| **Unbounded run hold** | compound written off past shelf 221,416 → 530,724 kg; consumers short 580 → 4,655 |
| **Widening final compound's lead to 0.9 of shelf** | expiry 0.5% → 15.4%, median age 43.5 h → 93.3 h on a 120 h shelf |
| **A campaign-hold ceiling that is too high** | GT 99.5% → 45.1% while extruder setup *dropped* 1,399 h → 935 h |
| **All wire first, all fabric last** | minimised wire changes but pushed two thirds of FRC's work to month end; within-horizon 83.4% → 59.7% |
| **Waking a dormant machine** | −41,236 tyres by breaking campaign batching |
| **Cutters reaching their full 24 h shelf** | GT 96.6% → 96.3%. Their idle *is* the shelf life |

Two meta-lessons run through all of them:

1. **Setup hours are not the objective.** Several changes that cut changeover
   substantially cost tens of thousands of tyres, because the kit needs
   alternation.
2. **A pick-site rule gets undone.** Around thirty rules downstream reassign the
   pick. A rule that must hold has to be a candidate filter or a last word.
