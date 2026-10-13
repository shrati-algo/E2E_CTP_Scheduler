# Bug: phase6 curing tail — head-of-line blocking amplifies a WIP-date glitch (4×)

**Status:** DIAGNOSED (2026-07-27), fix spec ready, **not yet implemented**.
**Severity:** High for off-month / mismatched-WIP runs. **Latent** on the shipped June run.
**Where:** `phases/phase6_curing_revision.py` (curing revision). Data side: opening-WIP snapshot.
**Found by:** running the DB's **April** plan (`CuringSchedule_pcr`) — makespan blew out to **72.8 days**
for a 29-day / 349,241-tyre plan (June, for comparison, is 34.5 d for *more* tyres).

---

## 1. Symptom

Running the April plan:
- **All green tyres are built by day 29.5** (building has spare capacity), yet curing runs to **day 72.8**.
- 94% cured by day 29, then a **gap (days 40–59 nearly empty)**, then **15,750 tyres (300 blocks,
  24 SKUs, 52 presses) stranded at days 55–72**.
- The stranded presses sit **IDLE for 35–40 days**, then resume at day 60 (e.g. press 4820 cures to
  day 20, idle 40 days, one block at day 60).
- Demand is still 100% met (349,241 cured) — nothing is dropped; it is a **timing** blow-up.

A capacity-bound tail would be *continuous*; a **gap-then-chunk** means blocks are being *forced* late.

---

## 2. Root cause — a spark and an amplifier

### 2a. Spark (DATA) — a wrong-month WIP snapshot, seeded without a clamp
The April plan was run with the **June** opening-WIP snapshot (`opening_wip_used.csv`, every
`produced_time` = 2026-05-29 … 2026-06-01). Under the **April** clock (plan_start = 2026-04-01 07:00),
each seed lot is dated **day ~59–61**. Phase6's kit-pool seeder (**F5**, `seed_kitpool_from_inventory`)
sets `scheduled_finish = produced_time` **with no clamp** (`load_inventory_seed`, ~line 809), so those
tyres are treated as *"available at day 60"* — violating F5's own stated precondition (lines 606–611:
"every seed timestamp is ≤ plan start"). Opening WIP is physically on the floor at **t0**; it can never
become available in the future.

Per SKU the future-dated WIP count ≈ the apparent "shortfall": GT 1402 → QXPCT **276 WIP** dated day
60.9 (shortfall 277); GT 2568 → HRHB0 **344 WIP** (shortfall 343). FIFO drains the real building lots
(day 1–25) first, then the last ~276 QXPCT tyres are satisfied by the **day-60 WIP seed** → those blocks
come out READY with `kit_ready ≈ day 60`, green_age ≈ 0. **~4,145 tyres total become kit-ready at day 60
instead of t0.** (So the earlier "1.2% under-build" reading was a *misdiagnosis*: phase1c netting is
correct and the tyres exist — they were just seeded with the wrong timestamp.)

### 2b. Amplifier (CODE) — scalar `prev_end` head-of-line blocking
Phase6's core rule (`revise_curing_schedule`):
```
candidate_start = max( original_start , kit_ready , prev_end_on_press )
new_end        = candidate_start + ORIGINAL DURATION
```
`prev_end` is a **per-press scalar cursor** (`prev_end = {}` ~line 1324, applied ~lines 1378–1380,
updated ~line 1453). Any block placed late for *any* reason advances that press's cursor, and **every
later block on the press inherits it** via `max(…, prev_end)` — regardless of whether that block's own
kit was ready weeks earlier.

Traced on press 4606:
```
blk 50  1D25214012008QXPCT  start day 60.3  kit_ready day 60  green_age≈0   ← spark (day-60 WIP seed)
blk 51  1325225518111HRHB0  start day 60.6  kit_ready day 17  green_age~1049 ← kit READY, dragged
blk 52+ …35 more            start day 60-72 kit_ready day 17-28              ← all head-of-line blocked
```
Of the 300 tail blocks, **~212 have kit_ready ≤ day 30 (innocent victims)**; only **~71** have a genuine
day-60 kit. **A ~4,145-tyre glitch is amplified 4× into a 15,750-tyre / 40-day tail.**

### Why June is fine
June uses a warm, in-month WIP snapshot (kit_ready max ≈ building max ≈ day 31), so there is no day-60
seed and the short/late blocks land at the *end* of a press queue with nothing behind them. **The
amplifier is latent in June** — April merely triggers it. (The known "alphabetical kit-pool" fix, F1,
addressed a *different* part of phase6; this `prev_end` cascade is separate and still live.)

---

## 3. How to correct — three fixes, in order of leverage

### FIX 0 (true root, DATA): use an in-month opening-WIP snapshot
Schedule the April plan with an **April-consistent** opening WIP, not June's. This alone removes the
whole tail. Neither code fix substitutes for feeding the right-month snapshot.

### FIX A (spark, CODE): clamp the WIP seed to plan-start — ~3 lines
In `load_inventory_seed` (build ~lines 745–812), enforce F5's own precondition. Opening WIP is on the
floor at t0, so:
```python
# flag + clamp future-dated seed timestamps to plan_start
gt.loc[gt["skuCode"].notna() & gt["_pt"].notna()
       & (gt["_pt"] > pd.Timestamp(plan_start)), "_disp"] = "FUTURE_DATED"
...
"scheduled_finish": fresh["_pt"].clip(upper=pd.Timestamp(plan_start)),
```
Report the `FUTURE_DATED` count loudly (same channel as E8 expired). Optional gate
`clamp_wip_seed_to_plan_start` (default off). Effect: the 276 QXPCT / 344 HRHB0 seeds sort to the FRONT
of the FIFO (finish = t0), the drain-blocks get `kit_ready ≈ day 25`, the day-60 kit disappears.

### FIX B (amplifier, CODE — the durable fix): per-press gap-fill de-blocking
Replace the scalar `prev_end` with a per-press **occupied-interval list**; each block takes the
**earliest free slot ≥ candidate_start** on its press. A kit-late block takes a late slot; a following
READY block drops into the **hole in front of it** instead of inheriting the lateness. `pressNo` never
changes. Config flag **`press_gap_fill`** (default `False` ⇒ today's `else` branch verbatim ⇒ June
byte-identical).

Two helpers (mirror `_clear_blocked`): `_earliest_free_slot(intervals, start, duration)` and
`_insert_interval(intervals, s, e)`. In `revise_curing_schedule`, after `prev_end = {}`:
```python
GAP_FILL = bool(cfg.get("press_gap_fill", False))
press_busy = {}                      # pressNo -> sorted [(start,end)]
if GAP_FILL and REINSERT:
    for p, ws in blocked_idx.items():
        press_busy[p] = sorted((pd.Timestamp(a), pd.Timestamp(b)) for a, b in ws)
```
Branch replacing lines ~1378–1380 / 1394–1398 / 1453 (keep the `else` identical to today):
```python
if GAP_FILL:
    candidate_start = _earliest_free_slot(press_busy.get(press), candidate_start, duration)
    new_end = candidate_start + duration
    _insert_interval(press_busy.setdefault(press, []), candidate_start, new_end)
else:
    pe = prev_end.get(press)
    if pe is not None and pe > candidate_start:
        candidate_start = pe
    if REINSERT:
        candidate_start = _clear_blocked(candidate_start, duration, blocked_idx.get(press, ()))
    new_end = candidate_start + duration
    prev_end[press] = new_end
```
The kit-ready floor, P1 floor and streaming gate stay ABOVE this branch (untouched — they only raise
`candidate_start`). In `verify_invariants`, relax ONLY the within-press order (check 4) and SKU-
transition (check 5) checks under a new `allow_reorder=True` (print as INFO); everything else unchanged.

**Interaction (important):** FIX B alone clears the ~212 innocent victims (tail → ~4,100) but the ~71
genuinely-day-60-kit blocks stay pinned (a block may never cure before its kit). **FIX A (or FIX 0)
removes the spark; FIX B removes the amplification.** Ship both — orthogonal, minimal, phase6-contained.

---

## 4. Correctness (agent-verified) — invariants hold, June cannot regress

With `press_gap_fill` OFF → literally the current code → **byte-identical**. With it ON:
- **Duration preserved** (`new_end = start + duration`).
- **No block before its `original_start`** (`candidate_start` starts at `orig_start`; free-slot only moves forward).
- **No block before its `kit_ready`** (kit floor applied before gap-fill, which only advances).
- **No press double-booking** (free-slot uses the same overlap predicate as the E4 check, then inserts).
- **`pressNo` / `skuCode` / `qty` / `cycleTime` immutable** (only start/end rewritten — strictly stronger than best-press, which mutates pressNo).
- **June makespan can only stay equal or improve**: the earliest free slot ≥ candidate_start is always
  ≤ the scalar-path start (`max(candidate_start, running_max_end)`), so no press last-end grows. Green
  ages can only improve. Default path charges no in-model changeover, so reordering costs no schedule time.
- **Deliberately relaxed:** within-press ordering / SKU-transitions (reported INFO) — occasionally one
  extra mould change where a late block is moved behind an early follower.

---

## 5. Reproduce
```
# pull the April plan and run (uses live DB masters + the DB April CuringSchedule)
python run_scheduler.py --db-plan --out output/april_run
# inspect the tail
#   output/april_run/phase6_curing_revision_updated.csv  -> kit_ready vs startTime, green_age_h
```
The June default (`python run_scheduler.py`) does not exhibit the tail.

## 6. Related
`knowledge/memory/` → `v6-phase6-alphabetical-kit-pool-bug` (F1, the *other* phase6 fix),
`ctp-plan-is-warm-we-run-cold` (cold-start), `ctp-extra-5days-is-single-machine-pinning` (June tail is
a *different*, mould-bound cause). This bug is specifically the **`prev_end` head-of-line amplifier +
future-dated WIP seed**.
