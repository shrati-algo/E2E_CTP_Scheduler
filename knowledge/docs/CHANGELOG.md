# ctp_v6_v2 — THE COMPLETE CHANGELOG

**Every change. Why we made it. What it cost. What it bought.**
Nothing here is assumed. Every number was measured from this folder's own files and can be
reproduced. Where the plant has not supplied a value, this file says **ASK THE PLANT** and stops.

> **THE HEADER OF `MEMORY.md` IS NOW FALSE.** It says *"`phases/*.py` are byte-identical copies of
> `v6_wave_p2/phases/*.py`"*. **In v2 that is not true.** Six phase files are modified. This file is
> the honest record of what changed and why.

---

## THE CLASSIFICATION TEST — applied to EVERY change

The user's standing constraint: **"the LOGIC should be the SAME — we correct INSIDE it."**

| Class | Definition | Approval needed? |
|---|---|---|
| **(A) DEFECT** | The code contains a rule it **fails to execute**, or a rule whose **own stated justification does not exist in CTP's data**. | **NO** — just fix it |
| **(B) POLICY** | The code executes its rule **correctly**; we want a **different** rule. Same machinery. | **YES** — behind a config flag, **default = v6's behaviour** |
| **(C) ALGORITHM** | New machinery, new rules. | **Must be explicitly asked for** |

**The question that does the work:** *"Is the rule STATED-AND-BROKEN, or EXECUTED-AND-DISLIKED?"*

**The contract:** with every `(B)`/`(C)` flag OFF, the pipeline reproduces v6's output **byte-for-byte**.
That turns *"we did not change the algorithm"* from a claim into a **test you can run**.

---

> ## 🔴 STOP — READ `AGENT_FINDINGS.md` FIRST
>
> Two independent experts audited the finished schedule (2026-07-14). **Both said it is NOT fit to
> send to the plant.**
>
> **74% of tyres (247,122 of 333,274) are cured from green tyres already PAST their 24 h shelf life.**
> phase6 slides the presses by a median of **47.8 hours** and **never re-plans building** — so the
> green tyre is built on time, then rots on the rack for two days while its press slides away.
>
> The makespan numbers below are **REAL**. They are also **NOT THE POINT**. Fix building first.

---

# PART 1 — THE RESULT

| | v1 (start) | **v2 FINAL** |
|---|---|---|
| **MAKESPAN** (lead 24 h) | **61.0 d** | **31.66 d** |
| **MAKESPAN** (**ZERO lead** — what the user requires) | 61.0 d | **32.66 d** |
| Cured in 30 days | 65.9% | **97.67%** |
| **Cured in 35 days** | 78.8% | **100.00%** |
| Building peak | 690 h (96%) | **597 h (83%)** |
| **DAG negative-gap edges** *(on the SURVIVING edges)* | **2,688** | **0** |
| **Lots starting with incomplete material** | **44** | **0** |
| Mould campaign (SKU transitions) | 56 | **56** |
| Changeovers | 530 | 531 |
| **Deterministic across runs** | **NO** | **YES** (md5-identical) |

**Shipped: 31.66 d, 100% cured by day 35.  At ZERO lead time: 32.66 d, still 100% by day 35.**

### 🔴 THE NUMBERS THIS TABLE USED TO CARRY WERE WRONG. THE AUDIT CORRECTED THEM.

| I reported | **The truth** | Why I was wrong |
|---|---|---|
| Green-tyre age at press: **13.8 h** median | **41.8 h median. 74.1% of tyres (247,122 of 333,274) are OVER the 24 h shelf life.** | My `measure.py` rebuilt the FIFO **without seeding the opening WIP** — which is the **OLDEST** stock in the pool. phase6's own `green_age_h` column is authoritative. **THIS ONE NUMBER INVERTS THE VERDICT.** |
| **LOTS dead before first consumer: 12.9%** | **23.1%** | I was quoting an older run. |
| **`MATERIAL STARVATION`: "0 — a REAL check"** | **Narrower than it looks.** phase3's aging filter **SEVERS** 463 edges; phase5 force-places the orphans. Those dependencies **have no edge**, so they are invisible to every gate. **A constraint deleted is not a constraint satisfied.** |
| Cure ramp **95.1% / 97.4%** | **95.57% / 97.84%** | stale run. |

**See `AGENT_FINDINGS.md` for the full audit and the five other claims of mine that were wrong.**

---

# PART 2 — THE THREE BUGS THAT COST THE MOST

## 🔴 BUG A — `phase6:333`: the kit pool was drained in ALPHABETICAL PRESS ORDER. **Cost: 21 DAYS.**

```python
pool = KitPool(build_df)                    # ONE global per-SKU FIFO of green tyres
for press, g in plan.groupby("pressNo"):    # <<<< pandas sorts the keys → ALPHABETICAL
    for _, r in g.iterrows():
        kit_ready = pool.take(sku, qty)     #      but it drains the GLOBAL pool
```

The first press **alphabetically** walks its **entire month** and eats every early green tyre before
the second press is looked at. Press `5205`'s block starts **07:00 on 1 June** and was handed kit
built **2 July** — a **748-hour head delay**.

**Three proofs it is a DEFECT, not a policy:**
1. v6's OWN docstring (`:280`) cites Rule *"consume them chronologically"*. The code does not.
2. v6's OWN spec (`:10`) says *"For each press **independently**"*. A shared mutable FIFO makes them coupled.
3. **We renamed the presses** — same machines, same work — and the makespan changed by **4.2 days**.
   *A scheduler's answer cannot depend on the SPELLING of an equipment ID.*

**FIX (A):** one global pass in `startTime` order; `prev_end` becomes a per-press dict.
The formula `max(orig_start, kit_ready, prev_end)` is **unchanged, character for character**.

**AFTER THE FIX, the rename test gives an IDENTICAL result — to the second.**

⚠️ **`phase6_curing_revision.py` was BYTE-IDENTICAL to `v6_wave_p2`'s. BTP Banmore has this bug too.**

---

## 🔴 BUG B — building load had **NO TIME DIMENSION**. **Cost: 5.5 DAYS.**

```python
machine_load = defaultdict(int)     # <<<< A SINGLE SCALAR per machine. No time axis.
avail = [m for m in chosen if machine_load[m] < MACHINE_CAP_NS]   # MONTH cap only
```

Cap-and-spill balances the **month total**. It never asks *"can this machine do this work on the DAY
the press needs it?"* Measured, per machine per week (capacity **168 h/week**):

```
           wk0    wk1    wk2    wk3    wk4  |  TOTAL     util
  3407:   280!   201!     0      0      0   |   481 h    67%   ← 2 IMPOSSIBLE weeks, then 3 IDLE
  3410:    30    120    239!    88     29   |   507 h    70%
  3409:    31     40     37    112     62   |   283 h    39%   ← NEARLY EMPTY while 3407 drowns
  ( ! = more work than 168 h. PHYSICALLY IMPOSSIBLE. )
  5 of 55 machine-weeks are over capacity.
```

Machine 3407's **total** load is 67% — but week 0 needs **280 h against a 168 h capacity**. It falls
behind, never catches up, and sits idle for three weeks.

**FIX — CTP DEVIATION #23, `building_time_phased` (C) ALGORITHM, default OFF:**
`bucket_load[(machine, day)]` gives `machine_load` a **time axis**. A lot is charged to the **day its
press needs it**; a machine is refused if that day is full; the lot spills to an eligible machine with
room **in that same day**. Groups run **most-constrained-first** so sole-eligible items claim their
machine-days before flexible work buries them.

> ⚠️ **THE BUCKET MUST BE A DAY, NOT A WEEK.** Every GT lot's build window (`due − earliest`) is at
> most **22.5 h** (median 20.6 h), because `green_tyre_max_age_h = 24`. **Work cannot move across
> weeks at all** — a green tyre built a week early expires before it cures. The only freedom that
> exists is moving work across **MACHINES inside the same ~24 h window**. A weekly bucket would model
> a freedom the plant does not have. *(My original "balance by WEEK" diagnosis was WRONG. This is the
> correction that made the fix work.)*

| | before | **after** |
|---|---|---|
| makespan | 37.31 d | **31.84 d** |
| 30d / 35d cured | 93.05% / 99.06% | **97.02% / 100.00%** |
| press starvation (max) | 194.9 h | **47 h** |
| lots dead before consumer | 29.8% | **18.4%** |
| **CHANGEOVERS** | **530** | **530 — NO COST** |

**Why this survives where v6's `BUILDING_LEVEL` died (it made the makespan 36.2 → 64.6 d):**
`BUILDING_LEVEL` round-robined SKUs in 8-lot mini-campaigns, **deliberately shredding campaigns**, and
capped daily **output**. Time-phasing **preserves campaigns** (a group only opens a new machine when
every machine it already runs on is full **for that day**) and caps **nothing**.

---

## 🔴 BUG C — v6 lets a **"tie-break" become THE key**. Found **THREE** times.

Whenever the **leading fields** of a sort key are **constant or inert on CTP's data**, the trailing
"tie-break" silently becomes **the entire scheduling policy** — and nobody chose it.

| # | Where | The degenerate key | Cost |
|---|---|---|---|
| 1 | `phase6:333` | `groupby("pressNo")` → **alphabetical press order** | **21 DAYS** (BUG A) |
| 2 | `phase5` building | `elig` was a **SET OF STRINGS**; Python hash-randomises per process and `sorted()` is stable → ties broken **BY THE HASH SEED**. **The building line was laid out by a coin toss** — two runs of the same build gave **47.9% vs 51.3%** expired edges. | **every measurement was noise** |
| 3 | `phase5:692` | `_key = (wave, mhe_index, first_need, item_code, lot_id)`. In the cold start: **wave constant (1 value)**, **`first_need` constant (1 value across all 208 wave-1 lots)**, `mhe_total == 1` on **6,780/6,780** lots → **`item_code` decides the WHOLE cold start, alphabetically.** `S53` — a **1.9-MINUTE** bead compound on the critical path to the first green tyre — is **19th alphabetically** and **waits 8.3 HOURS**. Bead apexing then sits idle h+10.8 → h+21.2. | most of the 30 h TBM cold-start idle |

**THE TEST THAT CATCHES IT:** for every sort key, run `nunique()` on **each field**, on **THIS plant's
real lot table**. If a leading field is constant, **the key has collapsed.**

```
  CTP measured:   mhe_total   nunique = 1    ← CONSTANT
                  campaign_id nunique = 1    ← CONSTANT
                  wire_type   nunique = 1    ← CONSTANT
  Cold start:     wave_first  nunique = 1    ← CONSTANT
                  first_need  nunique = 1    ← CONSTANT
                  item_code   nunique = 201  ← so THIS decides everything
```

---

# PART 3 — ALL 23 DEVIATIONS

## The original 11 (v1) — see `MEMORY.md` for the full write-ups

| # | What | Class | Why |
|---|---|---|---|
| 1 | `efficiency` read from the routing | (A) | v6 has no efficiency column. CTP's routing gives 0.96. `eff` **divides** → +4.17% duration. |
| 2 | `transfer_time` read from the routing | (A) | v6 has none. Added to `min_aging`, **never** to `max_aging`. |
| 3 | The plant **changeover matrix** wired in | (B) | CTP has one; v6 does not. Bridged by `machine_to_changeover_line` (routing uses machine **IDs**, the matrix uses machine **NAMES** — **zero string overlap**). |
| 4 | **SUM the BOM rows**, don't take the first | (A) | `grand_parent` is the BOM's **branch key** — it says *which component* consumes each slice. v6's first-row rule gave a **2-belt tyre 1 belt's rubber**. The BOM's own identity `Parent_qty(X) == SUM(child_quantity into X)` holds **10,755/10,755** when summed; **1,292 violations** under v6's rule. FINAL COMPOUND +9.1%. |
| 5 | `db_loader.py` **replaced** | (A) | v6's contained a **hard-coded production MySQL URL and password in plain text**, and converted **M→MM** — which inflated CTP's WIP **1000×** (the plant had 1,621 m; the scheduler believed 1,621 km). CTP is **file-based only**, and is denominated in **METRES**. |
| 6 | An **unknown UOM must RAISE**, not guess | (A) | v6's `qty * proc_time` fallback produced **9,580% mixer utilisation**. |
| 7 | `pieces_per_cut: BELT CUTTER: 2` | (A) | **PROVEN BY ARITHMETIC:** 733,804 belts ÷ 15 cuts/min ÷ 0.96 = **849 h** on **ONE** 720-h machine = **118% — IMPOSSIBLE**. The plant *is* making this plan on one cutter, so 1 cut ≠ 1 ply. Without it, 801 goes to **161%** and becomes THE bottleneck instead of GT building. |
| 8 | **Never delete work that has quantity** | (A) | v6 silently dropped **55% of cap strip**. |
| 9 | phase1c cascade ratio = `SUM(child_qty)/Parent_qty` **within a SKU** | (A) | Settled by the pipeline's own oracle: `gross(B186)/gross(CWBHTCP100) = 1.780061`. The wrong weight was 0.920672; the correct one 1.779967. 97 of 4,534 pairs wrong, **+102,356 phantom units**. |
| 10 | **No aging row ≠ IMMORTAL** | (A) | `phase5:886 DEFAULT_MAX_AGE_H = 365*24` → an item missing from the aging master silently gets a **365-DAY** shelf life. `CAP 66-MOTHERROLL` got **8,760 h** from phase5 while phase2 gave it **72 h** — **the two phases disagreed 121× about one item.** |
| 11 | (v1) short-life resync **DISABLED** → **(v2) RE-ENABLED with its defects fixed inside** | (A) | See #22. |

## The v2 deviations (12–23)

| # | What | Class | Why / what it bought |
|---|---|---|---|
| **12** | `BUILDING_MONTH_CAP_H` made **cfg-driven** | (B) | Was a hard-coded `31*24*0.90 = 669.6 h`. Default unchanged. |
| **13** | The **dead over-cap fallback** at `:1889` repaired | (A) | `avail = chosen or sorted(elig, key=_sel_key)[:1]` — **`chosen` is TRUTHY after lot #1**, so `sorted(elig,…)` was **UNREACHABLE**. The comment's stated rule ("least-loaded eligible overall") **never once executed**. ⚠️ **Inert alone** (peak stays 690.4 h) — it only matters once the cap is lowered. **Lowering the cap WITHOUT this repair makes the peak WORSE (690 → 791 h).** |
| **14** | Green-tyre shelf life: **THREE numbers in one file** | (B) | `AGE_OVERRIDE_H = {"GREEN TYRES": (0.0, 72.0)}` at `:896`; a comment at `:62` says **48 h**; the plant says **24 h** (confirmed **twice** — `aging_master.csv` AND the plant's own aging-table image). phase2 already believed 24 h — **the two phases disagreed TODAY.** Now `cfg.get("green_tyre_max_age_h", 72.0)`; config ships **24**. |
| **15** | The **LFT contradiction**, stated honestly | (A) | `RESPECT_LFT = False`, yet phase5 printed `aging_violations: 0` while phase4 separately reported **221 EXPIRED**, and **41.8%** of lots finish after their LFT. Now: **`LOTS PAST LFT: 2,080 of 6,780 (30.7%) | median 14.6 h | worst 312.8 h`**. *Either respect it or stop computing it — do not do both.* |
| **16** | `utilisation_%` **divides by itself** | (A) | It was `busy / SPAN`, and for a busy machine `span == busy`. **Meaningless on 28 of 28 machines.** TBM 3407 printed `100.0%` when its true load was 674.7/720 = **93.7%**. Added `true_utilisation_%`; kept the old column. |
| **17** | 🚨 **THE SCHEDULER WAS NOT REPRODUCIBLE** | (A) | `elig` was a **set of machine-id STRINGS**. Python hash-randomises string hashing **per process**, and `sorted()` is **stable**, so ties (which **always** happen at the start, when every load is 0) were broken **BY THE HASH SEED**. **Same code, same inputs, two runs: expired edges 10,465 (47.9%) vs 11,197 (51.3%).** **The building line was laid out by a coin toss.** ⚠️ **Every A/B measured before this fix was noise.** |
| **18** | `BUILDING_LEVEL` made cfg-driven | (B) | v6 ships it **OFF** with the note *"naive takt-cap hurt throughput"*. Its lead is **3.0 days** — **BTP's** GT freshness. CTP's green tyre lives **24 h**, so the mode had **never been tested with a lead that matches the shelf life**. **TESTED 2026-07-14: turning it ON makes the makespan 36.2 → 64.6 d. v6's note was RIGHT. LEAVE IT OFF.** |
| **19** | **BUILDING LOAD BALANCING** | (B) | BUILDING was the **only** unbalanced department (spread **49 points**; every other dept ≤ 22). **3409 sat at 39.4%** while 3403 was at 88.5% — and 3409 was **legally eligible for 1,419.5 h** of work and was given **283.4 h**. Cause: cap-and-spill is **first-fit-decreasing PACKING**; FFD **packs, it does not balance**. Fix: **ascending-eligibility group order** + fair-share cap + load-first spill. **Peak 637 → 543 h, spread 49 → 14 pts, 3409 39% → 67%, changeovers 530 → 470 (FEWER).** Cost: green-tyre expiry **28.9% → 31.1% (WORSE)**. |
| **20** | `lot_mhe_total` is **UNREAD — it is INERT on CTP** | (A) | `mhe_total == 1` on **6,780 / 6,780** lots. CTP's MPQ master correctly has **no `Maximum Run Qty`** column. phase2 itself prints *"MHE MODEL INERT"*. |
| **21** | 🚨 **The audit used TWO CONTRADICTORY MODELS** | (A) | `_edge_audit` counted **EXPIRED** with *every edge required*, then counted **STARVATION** with *same-item edges as ALTERNATIVES* — **30 lines apart in the same function.** The pooled-MHE model (*"any 1 MHE supplies"*) **never sums quantity**, and its only justification is **MHE sub-lots — which do not exist on CTP** (and whose worked example is a **Carcass→GT** flow; **CTP has ZERO Carcass lots**. *It was describing another plant.*) **`MATERIAL STARVATION = 0` was a TAUTOLOGY.** Honest re-check: **44 lots** started building with incomplete material; the worst had **33.3%**. Now **quantity-summing**: **44 → 0**, negative-gap edges **52 → 0**. **HONEST COST: makespan 36.21 → 37.31 d.** *Today's shorter number was being bought by starting lots with material that did not exist yet.* |
| **22** | The **resync**: RE-ENABLED (v6's default) with **two (A) defects fixed inside** + a **missing primitive** written | (A) | I had **disabled** a v6 pass **and hidden it** (the key was not in config, so `cfg.get(…, False)` silently defaulted it OFF). **Root cause of the pass's own breakage:** it moved a **producer** later and **never re-propagated precedence to that producer's successors** — creating **2,688 physically impossible edges** (a consumer starting **before** its producer finishes). *The tell I missed: an earlier "fix + LFT brake" reduced impossible edges from 2,688 to **2,688** — i.e. by **ZERO**. The root cause had never been touched.* Also: `first_fit_jit` is a **FORWARD** placer being used for a **BACKWARD (ALAP)** ratchet — when the target slot is busy it overshoots **past** the deadline and the whole move is **discarded**. **The `latest_fit()` primitive did not exist in the file. It was written.** Result: **negative-gap edges created: +0.** `resync_chain_mode` (optional): lots dead **29.8% → 19.0%**, expired edges **47.9% → 31.8%** — **makespan unchanged (37.31 d in both).** |
| **23** | **TIME-PHASED BUILDING** | **(C)** | See BUG B. **The user asked for it in their own words.** Default OFF. |

## The cold-start deviations (24–26) — added last

| # | What | Class | Why / what it bought |
|---|---|---|---|
| **24** | 🚨 **THE COLD START DISPATCHED ALPHABETICALLY** | **(A)** | `_key = (wave, mhe_index, first_need, item_code, lot_id)`. **6,714 of 6,780 lots (99.0%)** sit in a tie-group where wave/mhe/first_need are all **equal**, so **the ALPHABET alone orders them**. The cold-start group is **201 lots with 201 distinct item codes** — so `item_code` decides **everything**. `S53` — a **1.9-MINUTE** bead compound on the critical path to the first green tyre — ranked **151/201** and **ran at h=8.30**. Final mixing sat at **96.3%** on 2 machines, drained alphabetically. **FIX: sort by `LFT` (EDD on the derived press deadline).** `S53` now runs at **h=0.00, rank 1/201**. Default ON; `dispatch_priority: item_code` restores v6 **bit-for-bit**. ⚠️ **My prescribed fix (LST / "critical-path distance") makes it WORSE** — `LST = LFT − duration` **promotes a long job purely for being long**. Measured: `lst` → 32.01 d; `slack` → 32.01 d; **`lft` → 31.45 d.** |
| **25** | **phase1c netted against stock that was ALREADY DEAD** | **(A)** | phase1c had **no age check**. It told building to make **441 fewer green tyres** on the strength of stock **already past its shelf life at plan start**. **The plan silently UNDER-BUILT by 441 tyres.** *(phase6's F5 seeder already age-checked correctly — the two phases disagreed.)* **441 → 0.** ⚠️ **HONEST COST: +0.08 d.** Correct and expected — it removes 134,802 units of dead stock, so net demand rises and the plant must actually make it. |
| **26** | **WIP was SMEARED PRO-RATA, not consumed CHRONOLOGICALLY** | **(B)** *flag, default OFF* | Stock **physically on the floor at t=0** was spread evenly over the item's whole `max_aging` window. **The scheduler mixed compound it already had.** `IL5`: **3,729 kg on the floor**, first-24 h demand **2,479 kg** — **fully covered** — yet **1,314 kg still had to be mixed**, occupying a mixer at H0 and **gating the first green tyre**. ⚠️ **CLASS (B), NOT (A)** — the file's own ALGORITHM section says *"Apply per-item **RATIO** to demand rows"*. **A ratio IS a pro-rata smear. v6 does exactly what it says it does.** So it needs a flag. `wip_netting_chronological`. **IL5's first-24 h net demand: 1,314 → 0** (same for S53, T15, PCRT15, B278). **First new green tyre: 11.24 h → 3.80 h. TBM utilisation in the first 24 h: 4.5% → 23.4%.** |
| **(24b)** | The WIP join **missed an item on a DOUBLE SPACE** | (A) | `inventory.csv` spells one green tyre `"GT  T1457 STAR"`. phase1c joined on the **raw** itemcode and never matched it (despite **10,378 tyres** of gross demand), while phase6's F5 **did** — so building made them **and** F5 seeded them. **6 tyres double-counted → 0.** Fixed by collapsing internal whitespace **exactly as phase6's `_norm_code` does — EXACT MATCH ONLY AFTER THAT, NO FUZZY.** |

> ### ⚠️ THE PRIZE I PROMISED FOR #26 DID NOT EXIST — RECORD IT
> I told the user, on another expert's number, that fixing phase1c would take the makespan
> **32.84 → ~30.7–31.0 d**. **Measured: 32.84 → 32.65 d. A gain of 0.19 days, not two.**
> I passed on a number **without testing it**. The agent who implemented it said so plainly:
> *"This is **not** the single highest-value change available. The independent investigation that
> produced that number is wrong."* **Never relay an expert's number as fact without measuring it.**

## Two MORE dead-code bugs — FOUND, FLAGGED, NOT FIXED (same class: a lookup that silently misses)

`phase5:567` **uppercases** `item_type`. Two constants are keyed in **mixed case** and therefore
**can never match**:

```
  FEEDER_JIT_TYPES = {"Tread", "SideWall", "Inner Liner", "Rubberized Ply",
                      "Cap Strip", "Pre Cap Strip"}
     ->  6 of 6 keys are mixed-case  ->  ZERO can ever match
     ->  THE ENTIRE PRODUCTION DE-PULSE / FEEDER-JIT PATH IS 100% DEAD CODE ON CTP.

  TARGET_WIP_HOURS  ->  22 keys.  ONLY 3 ARE REACHABLE:
                        FINAL COMPOUND, CALANDARED ROLL, PRE CUT ROLL MATERIAL
     ->  19 DIE SILENTLY -> 0.0 -> JIT.  Including:
         Green Tyres, Cap Strip, Tread, SideWall, Inner Liner, Carcass,
         Bead Bundle, Bead Apex ...
```

**Both would change behaviour materially. Neither is the assigned bug. They are recorded, not smuggled.**

---

# PART 4 — CHECKS THAT COULD NOT FAIL (and now can)

**Three times I handed the user a "safety" number that was structurally incapable of returning
anything but PASS — and quoted all three as evidence the schedule was sound.**

| The decoration | Why it could never fail | Now |
|---|---|---|
| **`MATERIAL STARVATION = 0`** (phase5) | The pooled-MHE model treats same-item producers as **alternatives** and **never sums quantity**. Its only justification (MHE sub-lots) **does not exist on CTP**. | **Quantity-summing.** Found **44** real violations, worst **33.3%** of material present. Now **0**. |
| **`ALL INVARIANTS HOLD`** (phase6) | All four are **consequences of the loop that produced the data**. `candidate_start = max(orig, …) ≥ orig` ⟹ *"blocks pulled EARLIER: 0"* **can never be non-zero**. | Rewritten to **re-read the written CSV** and **re-join the original plan**. Then the file was **corrupted 6 ways** — it caught **6 of 6**. A `_selftest_verifier` re-runs it against a corrupted copy **every run**. |
| **The phase1b demand gate** | Validated `per_sku`, **never `demand`**. Derived its SKU set **from its own output**. Coerced `NaN→0` on **both** sides. | Rewritten. **Fails all three attacks that used to pass:** demand halved → FAIL; a broken SKU (59,730 rows vanish) → FAIL; a **blank `child_quantity`** that silently zeroed **86% of all metre demand** → FAIL. |

**THE RULE: before quoting any check as evidence, ask *"what input would make this FAIL?"* If you
cannot construct one, it is not a check — it is a decoration that costs you vigilance.**

---

# PART 5 — THE COLD START (the current binding constraint)

The user requires **ZERO LEAD TIME**. Today `schedule_open_h: 24` — **158 lots / ~190 machine-hours run
BEFORE the plan opens.**

```
  schedule_open_h    lead    makespan    30d       35d       GT late   press starve max
       24 h          24 h    31.84 d    97.02%   100.00%      41%          47 h
       12 h          12 h    32.18 d    96.12%   100.00%      47%          56 h
        0 h           0 h    32.84 d    94.84%   100.00%      55%          64 h
```

**Zero lead time costs 1.0 day. 35-day cure stays at 100%.**

### What actually happens, hour by hour

```
  Presses need 510 tyres/h FROM HOUR 0.  They have NO SLACK:
      51 of 86 presses have < 1 h of slack. Median slack: 0.2 h in a 720 h horizon.
      => EVERY cold-start hour lands 1:1 on the makespan.

  Usable opening green-tyre WIP  : 4,252 tyres  (6,139 gross − 627 expired − 1,260 unmatched)
                                 = 8.3 HOURS of press cover
  Green-tyre bank goes NEGATIVE  : HOUR 7
  First NEW green tyre at a press: HOUR 36.99
  => the TBMs sit at 0% UTILISATION for the first 30 HOURS.
  Worst deficit in the first 72 h: 29,190 tyres. The bank NEVER recovers.
```

### ⚠️ A MEASUREMENT ARTEFACT I PROPAGATED — and it nearly hid the real bug

I reported: *"every component is built on time; only the green tyre is late."* **That was FALSE.**

`last_need` is the **LAST** press block a lot serves, **not the first**. A compound lot serving blocks
from **1–8 June** has `last_need` = **8 June**. It can finish **5 June**, be **FOUR DAYS LATE for the
1 June blocks it also has to serve**, and still score **"−72 h EARLY"**.

The correlation is exact:

| bucket span (`last_need − first_need`) | what my metric said |
|---|---|
| **64 h** — compound, tread, sidewall, liner, bead apex, calandered roll | *"0% late, −30 to −190 h EARLY"* |
| **8 h** — green tyre, steel belt, ply, cap strip | *"41–55% LATE"* |

**The metric was measuring AGING-WINDOW WIDTH, not punctuality.** Measured against the press's **actual**
need in the cold start: **100% of Bead Apex, Tread, Inner Liner, SideWall, Bead Bundle, Calandered Roll
and Final Compound are LATE.**

**That false premise would have stopped any expert from looking at `phase1c`. Only the one who TESTED
it found the real bug.**

### The two cold-start bugs (fixes in progress)

**COLD-START BUG 1 — `phase1c:303-313`: WIP is SMEARED PRO-RATA, not consumed CHRONOLOGICALLY.**
```python
demand["ratio"] = (orig_iw - saving) / orig_iw
demand["demand_qty"] = demand["demand_qty"] * ratio      # <<<< PRO-RATA SMEAR
```
Stock **physically on the floor at t=0** is spread evenly over a **64-hour** demand window.
**Measured on `IL5`** (the compound at the head of the cold-start critical chain):
```
  IL5 on the floor        : 3,729 kg   (100% alive, 120 h shelf)
  IL5 gross demand, 1st 24h: 2,479 kg   ← the floor stock covers it COMPLETELY
  IL5 STILL TO MIX        : 1,314 kg   ← the lot survives, occupies a mixer at H0,
                                          and GATES THE FIRST GREEN TYRE
```
**The scheduler mixes compound it already has.** 536 tonnes of final compound sits on the floor and
the scheduler cannot see it. Same pattern on `S53`, `T15`, `PCRT15`, `B278`.

**COLD-START BUG 2 — `phase5:692`: the cold start dispatches ALPHABETICALLY.** (See BUG C, #3.)

### THE FLOOR — 30 DAYS IS NOT REACHABLE AT ZERO LEAD TIME

```
  phase5 as-is, 0 lead                                 : 32.60 d  (actual 32.84 d)
  infinite component capacity, nothing banked          : 30.70 d
  + FINAL COMPOUND banked (the plant HAS 536 t)        : 30.33 d   ← THE ABSOLUTE FLOOR
  the drum alone (press 4833, 718 h, first block +70h) : 29.99 d
```

**The makespan driver is press 4833, NOT press 4604** (4604 has the most work — 719.78 h — but it
starts at hour 0 and has 226 usable green tyres of its own SKU).

### What CAN be banked — and what cannot

```
  MASTER COMPOUND    360 h shelf  → 15 days.  CAN bank.
  FINAL COMPOUND     120 h shelf  →  5 days.  CAN bank.  ← 536 t ALREADY ON THE FLOOR. THIS IS THE LEVER.
  Tread/SideWall/Liner/
  Bead Apex/Cal. Roll 72 h shelf  →  3 days.  CAN bank.
  ─────────────────────────────────────────────────────────
  GREEN TYRE / Ply /
  Steel Belt          24 h shelf  →  CANNOT be banked meaningfully.
```

**Green tyres CANNOT be banked. The level where banking is LEGAL and HELPS is FINAL COMPOUND.**

---

# PART 6 — WHAT THE PLANT MUST ANSWER (we will invent none of these)

| # | Question | What it decides |
|---|---|---|
| **1** | **CAP STRIP SLITTER: how many knives/bobbins on 701/702 per pass — and does machine 601 PRE-SLIT the cap ply into sub-rolls?** The plant's own machine list says **601 = seq 120 (Calender roll → Cap ply)** and **701/702 = seq 140, SUB-SEQUENCE 120 (Cap ply → Capstrip)**. **The routing has NO seq-120 operation at all** — it feeds 701 from seq 30 (the calender). The BOM says the mother roll expands **154.441×** (σ = 0.017, all 224 SKUs). Our override says **12 bobbins**. **12.87 × 12 = 154.44** — the arithmetic closes ONLY if 601 does a missing 12.87× step. | Whether Cap Strip is the plant's **#2 component (851 h)** or its **#12 (66 h)**. **785 machine-hours = 7.2% of the whole plant.** |
| **2** | **SILICA vs CARBON: the flag for the tread items. ONE COLUMN.** There is **no usable flag anywhere**: not in routing, itemtype, aging or MPQ. The BOM's 26 `child_description` values include *Carbon black* and **never Silica**. `changeover_matrix.csv` names silica constantly — **but 224 of 265 treads are labelled BOTH carbon AND silica by different rows** (it is a *pair*-level descriptor, not an item property). `buffer_master.csv` has *"Master compound Silica"* / *"Final Compound Silica"* — but **`itemtype_master` has no such ItemType, so those rows are INERT** (and `buffer_master.csv` is **dead code** — `load_buffer()` is called by **no phase**). | Tread expiry **42.8% (carbon)** vs **86.2% (silica)** = **596,883 m of 667,253 m** = **89% of all June tread**. |
| **3** | **BEAD BUNDLE shelf life.** Your **own** aging master **HAS all 53 `BW#####` rows — `MinAging` and `MaxAging` are BLANK.** (The **present-but-blank trap, in the plant's own file.**) The plant's aging-table image has **no bead row either**. → phase2 gives them **72 h**; phase5 gives them **8,760 h**. **The two phases disagree 121.7× TODAY.** | At a **72 h** shelf: 0.8% of bead edges expired. At **24 h**: 26.5%. At **8 h**: **100%.** A latent landmine. **DO NOT GUESS.** |
| **4** | **`proc_time` for `GT 2568 HT2` and `GT 1482 UHL`.** Machine `3409` is **already in the file**; peer green tyres run **39.5–82 SEC** (median 65). | **33,933 tyres (9.2% of the plan) are currently CURED WITH NO GREEN TYRE.** Plus **543 machine-hours (5.0% of ALL work)** building components for tyres that are never assembled. |
| **5** | **INNER LINER — inventory holds `SQ-001…SQ-020`; the BOM and routing use `IL002…IL090`. ZERO of 30 codes match.** These `SQ-*` codes appear in **no BOM row and no routing row**. Legacy code, or a different item? | Inner liner is the **#1 cold-start gap**: 19 items with **zero usable WIP**, **35,525 units short in the first 48 h**. |
| **6** | **FOUR OPERATIONS ARE ABSENT FROM THE ROUTING**, and four of your machines carry **ZERO load**: seq **60** (`1102` Edge Gum Calender), seq **110** (`2001` Bias Cutter → chafer), seq **120** (`601` Cap Ply Cutter), seq **160** (`2501/2502` Chafer Doubling). | The plan is missing an **unknown** number of machine-hours. The chafer chain runs only its **middle step**. |
| **7** | **EDGE GUM's raw material `EG01/EG02/EG03` DOES NOT EXIST IN THE BOM** — not as a child, not as a parent, nowhere in 42,411 rows. 150 lots / 172.7 h are made from a material **nothing produces and nothing mixes**. | FINAL MIXING is understated by the **entire** edge-gum compound demand — **unquantifiable**, because the BOM row that would size it does not exist. |
| **8** | **BEAD APEXING: your machine list says PCR = 3 machines. The routing has 2** (`201`, `202`). Is there an `0203`? | Would take 201/202 from ~60% to ~40%. |
| **9** | **GT BUILDING eligibility: 4 green tyres are eligible on exactly ONE machine.** `GT 1402 XPC TATA` (320.5 h) and `GT 1412 XPC MM` (86.2 h) are **both locked to 3404** = **406.7 h that cannot move anywhere**. | **A sole-eligible green tyre cannot be balanced, cannot be spilled, and cannot be recovered.** It is a single point of failure **written into the routing master** — and it is why the building hall cannot be fully levelled. |
| **10** | **CONFIRM IN WRITING: 1 belt-cutter cut = B1 + B2.** | The arithmetic already **proves** it must be (849 h on a 720 h machine otherwise). But `801` at **80.7%** on **ONE** machine deserves it in writing. |
| **11** | **EXTRUSION: the plant's own kg/h rating for the Triplex (1001) and Quintoplex (1002).** Their rate spreads (2.6× / 2.4×) obey **no law we can verify** (kg/min CV 0.33–0.43). | **NOT proof of an error** — extruder line speed legitimately depends on profile geometry. But we cannot rule one out. |
| **12** | **Is `inventory.csv` a real floor snapshot?** Every item type's **oldest** lot lands within **~1 HOUR** of its own MaxAging — the signature of a `WHERE produced_time > T0 − MaxAging` query, **never netted for what May actually consumed**. | The **COMPOUND** figures are unusable (**16.7× the plant's own buffer target**). The **semi-finished and green-tyre** figures **do** look real. |
| **13** | **Re-run the curing LP at 82–85% press OEE, not 100%.** The plan's implied tyres-per-cycle is a **median of 2.000** on a 2-cavity press. It budgets **0.64%** non-productive time; real is **15–18%**. | At **85% OEE**, 86 presses make **~352,000** tyres — **not 366,902**. There are **6 presses in routing that the plan never uses** (`4812, 4813, 4816, 4837, 4838, 5206`). At **92** presses the plan becomes feasible. |

---

# PART 7 — THINGS WE PROVED **WRONG** (do not re-investigate)

| Claimed | The truth |
|---|---|
| Bead apex has a **72 h MINIMUM** aging | **FALSE.** `MinAging = 0.0`, `MaxAging = 72.0`. We had swapped MIN for MAX. Across all 2,108 aging rows the **only** MIN values are `{0, 0.25, 2, 4, 6}` h. **ZERO items have a MIN aging ≥ 24 h.** |
| *"The code reads MaxAging into the min slot"* | **RETRACTED.** The code reads it **correctly**. This was an **expert reasoning correctly FROM A FALSE PREMISE I FED HIM**. |
| **Bead winder 8–63 NOS/MIN is a 7.9× error** | **FALSE — it is the BEST data in the file.** The item code carries the **rim diameter** (`BW12001`, `BW13001`…). `rate × π × D` = a **constant 60.3 m/min wire speed**. And `proc_time × bead MASS` is constant: **4.68–7.29 kg/min, CV 0.089, corr(log mass, log rate) = −0.993.** The winder is **wire-feed-limited**. A 19-inch bead has 5× the wire of a 12-inch bead, so it takes 5× longer. **This is what CORRECT data looks like. DO NOT TOUCH IT.** |
| **Steel Belt Edge Strip is on the wrong machine (601)** | **FALSE.** The plant's OWN machine list, row 601: *"Cap Ply Cutter — output: Cap ply, **Edge gum** — Sequence 120, **130**"*. The routing runs EDGE GUM at **seq 130 on 601**. **Verbatim match.** |
| **SideWall 1.293 m/tyre is wrong — a tyre has TWO sidewalls** | **FALSE.** It is a **TWIN PROFILE** — the Triplex extrudes both sidewalls in one strip and splits at the applicator. Mass check: **1.680 kg/m × 1.38 m = 2.32 kg/tyre = 1.16 kg per sidewall.** (A *single* sidewall at 2.32 kg would be **63% of the tread mass** — impossible.) |
| **Green Tyre 0.897 NOS/tyre should be 1.0** | **It is a SYMPTOM, not a rate error.** `1 − 0.0919 = 0.9081`. **33,706 of 366,902 tyres (9.19%)** belong to the 2 SKUs with **no GT BUILDING routing row**. Arithmetically exact. |
| **Duplicate BOM lines double-count demand** | **RETRACTED.** Rows differing only by `grand_parent` are **legitimate** — they say *which* component consumes each slice. They must be **SUMMED**. **The bug was in the SCHEDULER, not the BOM.** |
| **9 SKUs extrude a tread with ZERO compound** | **RETRACTED. THE BOM IS SOUND.** It lists every compound **OPTION**; each SKU consumes **ONE** and zeroes the rest. **Proved by MASS:** a 1-compound tread = **1.858 kg/m**, a 5-compound tread = **1.860**. *Identical.* |
| **`resync_short_life_components` causes the 34% lateness** | **RETRACTED.** `phase6:333` drove the makespan. But the **durable** charge against the resync (2,688 causality violations) still stood — **on completely different grounds than I gave.** |
| **"Balance the building load by WEEK"** | **WRONG — and this was load-bearing.** Every GT lot's build window is **≤ 22.5 h** (median 20.6 h). **Work cannot move across weeks AT ALL.** The bucket had to be a **DAY**. |
| **"Steel Belt's 45% expiry is a plant capacity truth"** | **WRONG. It is 88% a scheduler bug.** Of 1,543 lots that die before their first consumer, **the machine WAS FREE in the correct window for 1,420 (92%)**. Hall's condition holds on **all 13 machine groups with ZERO overload** — including cutter 801 **with its real 1.40× changeover inflation charged**. |
| **"All material is built on time; only the green tyre is late"** | **WRONG — a MEASUREMENT ARTEFACT.** See PART 5. |
| The plan is **213 SKUs** | **FALSE.** `plan.csv` has **36 unique SKUs, 34 producing.** |
| **Capstrip Mother Roll needs a shelf life from the plant** | **ALREADY ANSWERED.** User confirmed 2026-07-13: *"aging 0 to max 24 hr"*. (I had guessed **72 h — wrong.**) |
| **Mixer changeover / the halobutyl clean-out is a BLOCKER** | **ANSWERED. PLANT-CONFIRMED 2026-07-14: 2 minutes, ANY mixer, ANY compound pair, including halobutyl.** This is **already** what v6 does. **No code change. Do not re-raise it.** |

---

# PART 8 — THE HIDDEN CAPACITY OVERRUN (the number that matters most)

**The schedule looks feasible on paper ONLY because it never charges for remaking the material that rots.**

```
  Steel Belt lots that die before their first consumer : 546 of 1,990  (27.4%)
  Hours to remake them                                 : 167.3 h

  BELT CUTTER 801  (ONE machine, no alternate):
     currently booked        : 580.9 h
     + remake the dead belt  : 167.3 h
     = TRUE REQUIREMENT      : 748.2 h
     available in the horizon: 720.0 h
     >>> 103.9%  —  INFEASIBLE
```

**If the plant actually remade the belt that rots, cutter 801 does not fit in the month.**

**Fixing the aging does not make the plan FASTER. It makes the plan TRUE.**

---

# PART 9 — THE CONFIG. WHAT IS SHIPPED, AND WHAT v6 WOULD DO.

> ⚠️ **THIS CONFIG IS NOT v6's DEFAULT.** Every deviation is listed. Flip these to reproduce v6.

| key | **shipped** | v6's value | what the deviation buys |
|---|---|---|---|
| `schedule_open_h` | `24` | 24 | **The user wants `0`.** Costs 1.0 day (31.84 → 32.84 d). |
| `building_time_phased` | **`true`** | *(did not exist)* | **makespan 37.31 → 31.84 d. Changeovers 530 → 530 (no cost).** |
| `enable_short_life_resync` | `true` | true *(v6 runs it unconditionally)* | Restoring v6's default was **right**. Its own (A) defects are now fixed inside. |
| `green_tyre_max_age_h` | **`24`** | 72 (hard-coded in `AGE_OVERRIDE_H`) | **The plant's number.** Ends a live 3× disagreement between phase2 and phase5. |
| `building_month_cap_h` | **`480`** | 669.6 (`31*24*0.90`) | ⚠️ **PROVENANCE UNKNOWN** — neither 507 nor 669.6. Nothing measured it. **Review this.** |
| `seed_kitpool_from_inventory` | **`true`** | *(v6's pool starts empty)* | Closes the 4,127-tyre hole; moves the makespan **21.7 h**. ⚠️ **The CODE default is `True` — deleting the key does NOT restore v6.** |
| `balance_building_load` | `false` | *(did not exist)* | ON: peak 637 → 543 h, 3409 39% → 67%, **FEWER** changeovers (530 → 470). **COST: GT expiry 28.9% → 31.1%.** |
| `resync_chain_mode` | `false` | *(did not exist)* | ON: lots dead 29.8% → 19.0%. **Makespan UNCHANGED.** |
| `building_level` | `false` | false | **TESTED: `true` → makespan 36.2 → 64.6 d. v6's note was RIGHT. LEAVE OFF.** |
| `resequence_press_queue_by_kit` | `false` | *(did not exist)* | **DECLINED — A TRAP.** Press SKU-transitions **56 → 339** (+283 mould changes with **ZERO** reserved press time = 566–1,132 press-hours, against **19.8 h** of total fleet idle). And phase6 **deletes** the changeover rows, so it charges **zero** for a mould change — it would optimise against a cost function **blind to the cost**. |
| `schedule_master_mixing` | `false` | false | ⚠️ **IT CANNOT BE TURNED ON.** 2 master compounds (`MTCM313`, `TBA827`) have **no proc_time**. It is a **DATA hole**, not a policy. 3.52M kg unscheduled; 4 Banburys idle. |
| `reinsert_changeover_blocks` | `false` | *(did not exist)* | phase6:141 **deletes 105 blocks / 356 h** of reserved mould-change + cleaning press time — and when a block is delayed, `prev_end` binds and it **eats that window**. |
| `mixer_changeover_min` | `2` | 2 | **PLANT-CONFIRMED 2026-07-14.** Any mixer, any pair, including halobutyl. **Already what v6 does.** |

---

# PART 10 — FILES DELETED, AND WHY

| File | Why |
|---|---|
| **`planning_max_aging.csv`** | **I INVENTED IT. The plant never supplied one.** Its own `Notes` column said *"from ctp_v6/config.yaml"* — **config laundered into a CSV to look like a plant master.** All 10 values happened to equal the chemistry max, so it was a **no-op — by coincidence, not by construction.** But `phase4_cpm.py:370-377` **DID read it**, and `CAP30` / `CAP STRIP 28` carry **72 h** chemistry while the file said Cap Strip = **24 h**. The day the plant loosened `CAP 66` to 72 h, this invented file would have **silently cut it back to 24 h on 136 lots and nobody would have known why.** **A landmine. Deleted.** |

---

*Every number here was measured from `ctp_v6_v2/` and can be reproduced. Both modes are deterministic
across repeated runs. Where the plant has not supplied a value, this file says ASK THE PLANT and stops.*
