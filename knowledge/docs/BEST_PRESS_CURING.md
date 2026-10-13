# Best-Press Curing — mould-capped earliest-finish re-placement (phase6)

**Status:** implemented, config-gated, **default OFF**. Turn on with `best_press_curing: true`.
**Measured:** verbatim phase6 **37.00 d → 34.95 d** (−2.05 d), physically valid, deterministic.
**File:** `phases/phase6_curing_revision.py` (`best_press_reassign`, `_check_schedule_bestpress`,
`_curing_press_eligibility`, `_mould_caps_from_plan`).

---

## 1. What it does

phase6's default is the **verbatim v6 port**: every curing block stays on the press
`plan.csv` named for it and is only ever slid **later** (`revise_curing_schedule`). That
pins the makespan to `plan.csv`'s press choice — a SKU the LP put on **one** press cures
serially even though routing allows it on many.

When `best_press_curing: true`, phase6 runs one extra pass that **re-places every curing
block onto the routing-eligible press with the earliest finish**, under three hard,
physically-real constraints. It reuses the green-tyre → block match that
`revise_curing_schedule` already made (via the FIFO `KitPool`); only the **press** and the
**start time** move.

## 2. The algorithm

```
For every curing block (dispatched in kit_ready, then original-start order):
    Find allowable presses                         (routing.csv CURING rows)
      ↓
    ★ kit-gate    : start ≥ this block's kit_ready  (the green tyre must exist)
      ↓
    ★★ mould-cap  : a SKU may not run on more presses AT ONCE than it has moulds
                    (default cap = the plan's OWN max concurrency — a proven lower bound)
      ↓
    ★ earliest finish : start = max(kit_ready, press_free, mould_free);
                        +5 h changeover if the press switches SKU (mould change)
      ↓
    Choose the press with the minimum finish, breaking ties DETERMINISTICALLY:
        1. earliest finish
        2. prefer the press already holding this SKU  (keep the mould campaign, no changeover)
        3. least-loaded press                          (spread work)
        4. lowest press number                         (final tie-break)
      ↓
    Assign press; occupy one mould slot for the block's duration
```

**Preserved:** `block_id`, `skuCode`, `qty`, `cycleTime`, each block's **duration**, and its
`kit_ready` / `kit_oldest`.
**Rewritten:** `pressNo`, `startTime`, `endTime`, `shift`, `date`, `green_age_*`, `remarks`
(a ` | BEST_PRESS` tag is appended).

## 3. Why the mould-cap is the critical step

Without it, "choose minimum finish" would put a SKU on **as many of its allowable presses as
it likes** — e.g. `RXC10` routes to **68** presses, so a naive list-scheduler would run 8–10
`RXC10` blocks at once. But the plant has a **finite number of moulds**; running 8 concurrent
needs 8 moulds. Assuming moulds that may not exist produces a **physically impossible**
schedule (this was the earlier "33.9 d" prototype's defect).

The cap **defaults to the plan's own max concurrency per SKU** — a **proven lower bound**: the
plan already runs that many at once, so that many moulds demonstrably exist. It **never invents
a mould.** If the plant supplies real physical counts, put them in
`curing_mould_counts: { "<skuCode>": <n> }` and they supersede the plan-derived cap per SKU.

```
Examples of the plan-derived cap (proven LB):
  1D25218Z14008RXC10 : 1   (plan runs it serially on ONE press, 3613 — though routing allows 68)
  1325219515091HURL2 : 5
  1325217515084HURL2 : 4
  1325224716111SELS0 : 9
```

## 4. What limits the result (the building floor)

34.95 d is **not** further reducible by any curing change. Diagnosis of the tail:

- The schedule is **mould- and building-bound**, not press-contention-bound
  (block-start gate split ≈ mould 4268 / press 2096 / kit 611).
- Relaxing the mould caps entirely only reaches ~34.5 d, because the **last green tyre is not
  built until ≈ day 33.4** (the building floor). Curing cannot finish before that + one cure.

**To go below ~34.9 d you fix BUILDING, not curing** — front-load the constrained SKUs' green
tyres (the building rope), so the last `kit_ready` arrives earlier. That is a phase5 lever.

## 5. Why the three sibling ideas did **not** help

| idea | verdict | why |
|---|---|---|
| **Best-press selection** | ✅ **works, −2.05 d** | uses existing moulds efficiently, earliest-finish |
| Curing tail optimization | ⚠️ ~0.2 d | the tail is the building floor + mould-1 SKUs; moving presses can't beat either |
| Scarcity priority | ❌ no help / worse | bottleneck is moulds+building, **not** press contention; scarcity-first breaks kit-gating (→ 53 d) |

## 6. The E4 gate for this path

`pressNo`, per-press sequence and SKU-transitions change **on purpose** here, so the v6-path
checks that forbid them are replaced. `verify_invariants` re-reads the written CSV and re-derives
eligibility + caps **independently** from `routing.csv` + the re-loaded plan, then checks
(`_check_schedule_bestpress`):

- rows 1:1 with the plan; `skuCode` / `qty` / `cycleTime` immutable (pressNo **may** change)
- durations preserved
- **no press double-booking**
- **every press routing-eligible** for its SKU  *(new)*
- **no SKU over its mould cap** concurrently  *(new)*
- READY blocks never start before their kit

A built-in self-test moves one block to a never-eligible press and requires the gate to FAIL,
proving the gate is a gate (`selftest] non-eligible-press corruption detected: YES`).

## 7. Determinism

The eligible-press list is **sorted** (not a `set`) and every tie-break is total, so the
makespan is **reproducible run-to-run** (verified identical across `PYTHONHASHSEED` = 1/42/999).
A set would iterate in Python hash order, which varies per process and made an early prototype
wobble between 34.7 and 35.3 d — that bug is fixed.

## 8. How to turn it on

In `config.yaml`:
```yaml
best_press_curing:    true       # default false = verbatim v6 port
curing_changeover_min: 300        # 5 h mould change (the plan's own CHANGEOVER value)
curing_changeover_tolerance_h: 0  # 0 = max makespan gain; raise for fewer changeovers (sec.10)
curing_mould_counts:  {}          # OPTIONAL plant-supplied physical mould counts, per SKU
```
Then re-run phase6 (or the pipeline). `plan.csv` and `routing.csv` are **READ ONLY** — no plant
data file is modified.

## 10. Changeover (mould-change) count — the trade-off

The mould-change **time** is unchanged: **5 h (300 min) per change**, taken verbatim from
`plan.csv`'s own CHANGEOVER rows (`curing_changeover_min: 300`). It is not invented and not
altered. And every changeover's 5 h is **already counted** inside the makespan.

But the mould-change **count** rises, because earliest-finish spreads a SKU across more presses:

| `curing_changeover_tolerance_h` | makespan | mould changes |
|---|---|---|
| verbatim (best-press OFF) | 37.00 d | **56** |
| **0** (default) | **34.95 d** | 270 |
| 2 | 36.83 d | 177 |
| 5 | 45.97 d | 72 |
| 8 | 61.17 d | 3 |

**The makespan gain and the changeover count are tightly coupled — you cannot have both.** The
2-day gain *is* the extra mould changes: to finish earlier under the mould-cap you must use
whichever press is free, which means more switches. Forcing campaign continuity (few changeovers)
makes moulds idle-wait for "their" press and the makespan explodes. **There is no operating point
with ≈56 changeovers and makespan < 37 d — the verbatim plan (37.0 d / 56) is already
changeover-optimal.**

Context on the 270: over a 35-day horizon across 86 presses that is **≈3 mould changes per press
per month**, and each 5 h is already in the schedule. Use `curing_changeover_tolerance_h` to pick a
point on the frontier: `0` for the full 34.95 d (accept 270), a small value to shave changeovers
for a little makespan, or leave best-press OFF (37.0 d / 56) if minimising mould changes is the
priority.

## 9. The one open plant question

To go **below the building floor** (≈33–34 d) the building rope must front-load the constrained
SKUs. To go **below ≈28.5 d** — the hard mould floor of `091HURL2` (5 moulds) — the plant must
add moulds. Best-press cannot, and will not, invent them. **Ask the plant** for the physical
mould count of `091HURL2`, `084HURL2`, `SELS0`, and any "2-mould press" before raising any
`curing_mould_counts` entry above its plan-derived value.
