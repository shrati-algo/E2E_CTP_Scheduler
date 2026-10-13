# AGENT FINDINGS — the final audit of `CTP_SCHEDULE_lead0h`

**Two independent experts audited the finished schedule. Both reached the same verdict.**

| | |
|---|---|
| **Auditor 1** | Hostile code auditor — re-derived every safety number from the OUTPUT files, ignoring what the pipeline printed |
| **Auditor 2** | Vikram Iyer — CTP Chennai plant. *"Would a shift supervisor run this?"* |
| **Date** | 2026-07-14 |
| **Under audit** | `CTP_SCHEDULE_lead0h/` (zero lead time, makespan 32.66 d) |

---

# THE VERDICT

> ## ❌ **NO. It is not 100% correct, and it is NOT fit to send to the plant.**
>
> **"A shift supervisor would reject it on page one — not because of the 2.66-day overrun, but
> because it cures 247,122 tyres (74% of everything kitted) from green tyres that are already past
> their 24-hour shelf life."**

**What would have to be true for the answer to be YES:**
1. Building would have to be **re-planned against the revised press times** (74% over-age today)
2. The **463 severed DAG edges** would have to be restored
3. **Master mixing** would have to be scheduled (1.77M kg of compound currently comes from nowhere)
4. The **356 h of mould-change time** would have to be re-inserted
5. `04_SUMMARY`'s SAFETY block would have to be **measured instead of typed**

---

# 🔴 THE ROOT CAUSE — and I lost it halfway through

```
  phase5 builds green tyres  ->  against the ORIGINAL press times
  phase6 then SLIDES the presses  ->  6,309 of 6,975 blocks, MEDIAN 47.8 h (max 72.0 h)
  ...and NEVER RE-PLANS BUILDING.

  So the green tyre IS built on time -- and then sits on the rack for TWO DAYS
  while the press it was built for slides away from it.
```

| | |
|---|---|
| **Tyres cured from a green tyre past its 24 h shelf life** | **247,122 of 333,274 = 74.1%** |
| Median green-tyre age at the press | **41.8 h** |
| p90 / worst | **65.0 h / 95.8 h (4 DAYS)** |
| Blocks affected | 4,745 of 6,381 |

`RUN_LOG.txt:480` prints this. **The README's "THREE THINGS TO KNOW" did not include it, and
`04_SUMMARY` did not mention it.**

> **This is the SAME defect I filed as "B1" in the very first issues DOCX** — *"phase6 re-times curing
> after phase5 built the green tyres; nothing re-ages."* **Then I chased the makespan and lost it.**
> It has come back as the single thing that invalidates the schedule.

[CTP-PLANT] `aging_table_pcr.png`: `GREEN_TYRE max = 1 day`. In **June at Sriperumbudur (35–40 °C
ambient)** that 24 h is not a soft number — it is when splice tack dies and scorch starts.
**A green tyre cannot be reworked. It is torn down or scrapped.**
**You cannot build a month's plan on the assumption that 74% of it gets a QA waiver.**

---

# 🔴 MY OWN WORST ERROR — I shipped a check that cannot fail. AGAIN.

`export_schedule.py:172-175` — the **"SAFETY (all must be ZERO)"** block in `04_SUMMARY.xlsx`:

```python
("  DAG negative-gap edges",      "0", "a consumer starting BEFORE its producer finishes"),
("  lots w/ incomplete material", "0", "building started before all inputs existed"),
("  machine double-booking",      "0", ""),
("  press block re-sequenced",    "0", "the plant's own block order is preserved"),
```

**These are STRING LITERALS. Nothing computed them.**
**If every press had been double-booked, `04_SUMMARY` would still have printed `0`.**

I wrote `MEMORY.md`'s rule myself — *"a check that cannot fail is not a check, it is a decoration that
costs you vigilance"* — **and then committed the exact same sin in the export.**

Three of the four happen to be true when recomputed. **One — `lots w/ incomplete material: 0` — was
FALSE.** (FIXED: the block is now measured from the written output.)

---

# BLOCKERS (both auditors)

### B1 — 74% of the month is cured from expired green tyres
See above. **The one fix that matters: re-plan building against the revised press times.**

### B2 — MASTER MIXING is absent, and **it is absent BY CHOICE, not by data**
> ⚠️ **I told the user it "CANNOT be switched on" because 2 compounds have no proc_time. THAT WAS WRONG.**

| | |
|---|---|
| Master compound **demanded** | **3,525,311 kg** |
| Items with a **complete** routing (proc_time + machine + batch) | **86 of 89 = 99.5% of demand** |
| Items genuinely blocked | **3 items / 16,656 kg / 0.5%** |
| **Produced by the schedule** | **0 kg** |
| **UNSOURCED** (after opening stock) | **1,767,980 kg = 70.2%** |
| What it would cost | **682 machine-h on 4 idle Banburys = 23.7% utilisation** |

All **40 of 40** master-compound items go cumulatively negative. FINAL COMPOUND (361 lots) sits
directly on top of it and feeds **every** component family. **This is not a footnote; it is the floor
of the schedule.**

The routing's own `predecessor_rule` says *"Master compound available / raw material available"* —
**the scheduler took that literally and assumed infinite supply.**
(`TBA827` is a **FINAL MIXING** item, not master — I mis-named it.)

### B3 — "0 negative-gap edges" is true only because the violating edges were **DELETED**
`phase3`'s F1 aging filter **SEVERS** producer→consumer edges whose aging window doesn't fit.
`phase5` then **force-places the orphans** (`RUN_LOG:339` — `orphans_FORCED: 463`).

**463 (consumer-lot, item) dependencies have NO DAG EDGE AT ALL** — 126,513 units.
The precedence audit and the material audit **both iterate EDGES**. So those 463 are
**structurally invisible to every gate.**

> ### **A CONSTRAINT DELETED IS NOT A CONSTRAINT SATISFIED.**

Two items (`MONOCHAF80`, `MONOCHAF120`) genuinely starve — peak deficits **4,184** and **967** units —
and the pipeline reports `MATERIAL STARVATION = 0`.

### B4 — 329 press-hours are promised TWICE
| | |
|---|---|
| Plan reserves | **105 blocks / 356.1 h** (56 changeovers @ 5.0 h = 280 h; 48 mould-cleans = 76 h) |
| `phase6:140` | **DELETES them all** |
| Curing blocks that overlap a deleted window | **129 blocks across 60 of 86 presses** |
| **Press time consumed** | **329.0 h of 356.1 h = 92.4%** |

*"Press 3613 on 18-Jun 15:00 is told to cure 44 tyres during the precise 5 hours its mould is out on
the crane."*

**What happens on the floor:** the supervisor does the mould change — because the *next* SKU
physically cannot run without it. The tyres don't happen. Every downstream block on that press
desynchronises.

**There is a flag that fixes this — `reinsert_changeover_blocks: true` — and it is OFF.**

### B5 — 40.7% of ALL material handoffs violate shelf life. **Nothing reports it.**
Re-run against the **placed** schedule:

| | |
|---|---|
| **Edges expiring before consumption** | **8,762 of 21,502 = 40.7%** |
| **Lots expired for EVERY consumer (100% scrap, remake required)** | **1,152 of 4,795 = 24.0%** |
| FINAL COMPOUND (120 h shelf) | **87.8% dead** — 2.43M kg of 2.61M kg |
| Cap Strip (24 h) | **85.3%** |
| Steel Belt (24 h) | **73.8%** |
| Ply (24 h) | **70.9%** |

**Phase 4 reported only 269 — because its own README says it is CAPACITY-BLIND.**
**After Phase 5 places the lots, NOTHING re-checks aging. The gate that would catch this does not
exist.**

---

# HIGH

### H1 — Two files in the same deliverable disagree about machine time by **665 hours**
- `03_MACHINE_LOAD` reports `busy_hours` — **WITH changeover**
- `02_COMPONENT_SCHEDULE` and `04_SUMMARY:COMPONENT_TIME` sum `duration_h` — **WITHOUT**

**Gap: 665.1 h = 5.7% of all machine time.**
**Worst: belt cutter 801 — 572.67 h (03) vs 413.26 h (02/04) — a 159.4 h / 27.8% understatement.**
A planner reading the headline KPI table sees 57%; **the machine needs 79.5%.**
**USE `03` FOR CAPACITY.**

### H2 — Belt cutter 801 rework: over by **55 h**, not 7 h — and only once changeover is counted
```
  572.67 h  scheduled (incl. changeover)
+ 154.00 h  rework processing (598 dead steel-belt lots = 30.4%)
+  48.50 h  rework changeover (598 lots x 4.87 min)
= 775.20 h   vs  720 h available     ->  OVER BY 55.2 h
```
**And this is a LOWER BOUND** — it ignores the 44.8% of lots that are *partially* dead, and it ignores
that re-cut belt sitting > 24 h **expires again**.

[CTP-PLANT] Steel belt is **calendered stock — it cannot be re-milled** once cut; you cannot throw it
back in a Banbury without stripping the wire. [INDUSTRY] Over-age belt coat loses cobalt-NR tack →
**belt-edge separation** — exactly the defect PCR does *not* 100% X-ray for.
**ASK THE PLANT: does CTP re-cut expired belt, or run it anyway?**

### H3 — PROMISE BROKEN: `dispatch_priority` defaults to `lft`, and **the key is absent from config**
`phase5:902` — `_DP = str(cfg.get("dispatch_priority", "lft"))`. **v6 sorts on `item_code`.**
The non-v6 behaviour governs **by default**, with **no config line telling anyone the knob exists** —
and by the code's own admission it **reorders 99.0% of all lots**.

**Other (B)/(C) changes with NO FLAG at all:**
- `_material_ready_ns` (phase5:1534) — a new material model on **every** placement path
- routing `efficiency` 0.96 now **divides every duration** (phase2:180; v6 hard-codes 1.0)
- `transfer_time` added to `min_aging` (phase4:207)
- the plant changeover matrix is activated **by file presence, not by a flag** (phase5:1394)

### H4 — `building_month_cap_h: 480` is a magic number, **and the repo says so**
`CHANGELOG.md:420`: *"⚠️ PROVENANCE UNKNOWN — neither 507 nor 669.6. Nothing measured it."*
`config.yaml:166`: *"NOTHING MEASURED OR DOCUMENTED SUPPORTS 480."*
**It is live and it tightens a real capacity cap.**

Also: `config.yaml`'s *"to reproduce v6, flip these three keys"* recipe is **WRONG** — it omits
`building_time_phased`, `wip_netting_chronological`, and `dispatch_priority`.

### H5 — The incoming plan requires ~89% fleet OEE with **zero PM and zero bladder changes**
Median implied rate: **exactly 2.0000 tyres/cycle** on a 2-mould press. Non-productive budget:
**0.64%**. **There is not one PM block and not one bladder-change block in 86 presses × 720 hours.**

| OEE | 86 presses | 92 presses (incl. the 6 idle) |
|---|---|---|
| 90% | 372,600 | — |
| **85%** | **351,900 — 15,000 SHORT** | **376,000 — FITS** |
| 80% | 331,200 — 35,700 short | 354,300 |

`4812, 4813, 4816, 4837, 4838, 5206` are in `routing.csv` but **NOT in the plan**.
**ASK THE PLANT whether they are available.**

---

# MEDIUM

| # | Finding |
|---|---|
| **M1** | **DEAD CODE.** `PARENT_AWARE_CUTTERS` (phase5:89) — **0 of 7 keys match** (CTP machines are numeric) → `compute_changeover_for_cutter()` is **NEVER CALLED**. `TBM_S1`/`TBM_S2`/`*_GT` (phase5:3593) — **0 of 39 BTP machine IDs exist at CTP** → `RUN_LOG:403` prints **"Total Green Tyres made: 0"** on a run that built **1,279 GT lots**. `build_mm_per_cut_map` filters on `unit == "MM"`; `bom.child_Unit` is KG/M/NOS → **0 of 42,411 rows** can pass. |
| **M2** | **MPQ.** `phase2:138` does `float("3 Batches ")` → raises → `mpq_min = None`. **All 361 FINAL COMPOUND lots get NO MPQ floor**, on the mixer bottleneck. Overall **2,157 of 6,715 lots (32.1%)** get no floor. |
| **M3** | **Machine 601** is mapped to changeover line `Cap Ply Cutter` but runs `EDGE GUM` — item overlap **0 of 3** → **0 of 147** changeovers hit the matrix; all fall to a flat 15 min. |
| **M4** | **Config keys NO PHASE READS:** `resequence_press_queue_by_kit` (**0 refs** — 16 lines of config argue about a knob that does not exist), `mixer_changeover_min` (**0 refs**; `phase5:42` hard-codes 2). |
| **M5** | **307 blocks / 16,244 tyres (4.4%) finish AFTER 2026-07-01 07:00.** The plan had to close on 30 June. [CTP-PLANT] *"For OEM despatch (Hyundai/BMW JIT at Sriperumbudur), a 3-day spill is not a rounding error — it is a missed despatch window."* |
| **M6** | **198 Bead Bundle lots have NO aging row** → silently **IMMORTAL**. |
| **M7** | **66.2% of blocks start ragged mid-shift; 65.5% cross a shift handover.** The plan came in shift-aligned (07:00/15:00/23:00); the schedule no longer maps to the shift report the supervisor actually uses. *"Not physics — but it is why he'd stop reading."* |
| **M8** | **`green_tyre_max_age_h` is a landmine:** phase5 defaults it to **72**, phase6 to **24**. Delete the key and the build side and the cure side **silently run two different plants**. |
| **M9** | **135,065 units of expired component WIP** + **132,762 units with NO shelf-life data that net anyway**. |

---

# ✅ WHAT IS GENUINELY CORRECT (independently verified, from the output files)

- **Machine non-overlap: ZERO.** All 28 component machines **and** all 86 presses.
- **No zero/negative durations. No blank machines. Nothing starts before 2026-06-01 07:00** (zero lead time honoured).
- **Press sequence preserved: 0 of 86 re-sequenced. Mould campaign 56 → 56.**
- **DAG precedence on the 21,738 SURVIVING edges: 0 negative-gap.** *(But see B3 — 463 edges were deleted.)*
- **Makespan 32.66 d, 366,902 tyres, the cure ramp, and the starvation figures all reproduce exactly.**
- **The routing is NOT missing machines.** `pcr machines.png` confirms **11 TBMs, ONE belt cutter (801), ONE bead winder (1201), ONE RHC (1101), 86 presses.** Building at 83% and 801 at 79.5% on a single machine **IS the real plant.** *(My hypothesis that machines were missing is DISPROVED.)*

---

# 🔴 FIVE OF MY OWN CLAIMS WERE WRONG. RECORD THEM.

| I said | The truth |
|---|---|
| Green-tyre age at press: **median ~14 h**, 28.5% over | **41.8 h median. 74.1% over.** *(My `measure.py` rebuilt the FIFO **without seeding the opening WIP** — which is the OLDEST stock in the pool. phase6's own `green_age_h` column is authoritative.)* **This one number inverts the entire verdict.** |
| **"Master mixing CANNOT be switched on"** | **WRONG. 86 of 89 items (99.5% of demand) have a complete routing.** It is absent **by choice**, not by data. 682 h on 4 idle Banburys = 23.7% util. |
| "**51 of 86** presses have < 1 h slack" | **18 of 86.** |
| **33,933** phantom tyres, **2** SKUs | **33,518** never built, **4** SKUs. And `GT 2568 HT2`/`GT 1482 UHL` have **ZERO routing rows** — not merely a missing proc_time. **39** green tyres lack proc_time, not 2. |
| **12.9%** of lots dead before their first consumer | **23.1%** (I was quoting an older run). |
| `95.1% / 97.4%` cure ramp | **95.57% / 97.84%.** |

---

# THE SHORTEST LIST TO FIX IT

## SCHEDULER'S JOB — all four are code/config, **none needs the plant**

| # | Fix | Why |
|---|---|---|
| **1** | **RE-PLAN BUILDING against the REVISED press times.** | **THE ONE THAT MATTERS.** phase6 slides presses by a median **47.8 h** and leaves building where it was. **Until building is re-timed, 74% of the output is over-age and EVERY OTHER NUMBER IS DECORATION.** |
| **2** | `reinsert_changeover_blocks: true` | Stop selling the same **329 press-hours twice**. The schedule gets **longer and TRUE**. |
| **3** | **Add a post-placement aging gate.** | phase4's check is **capacity-blind and therefore useless**. **8,762 violations pass through it untouched.** |
| **4** | **Schedule MASTER MIXING.** | **99.5% of it is routable TODAY.** 682 h on 4 idle Banburys. **It was simply forgotten.** |

## PLANT'S JOB — four questions. **We will invent none of them.**

| # | Question | Decides |
|---|---|---|
| **1** | **`GT 2568 HT2` cycle time** (and `GT 1482 UHL` cycle time + machine). | **33,518 phantom tyres.** |
| **2** | **Real press OEE** — and are `4812/4813/4816/4837/4838/5206` available? | **Whether 366,902 is makeable AT ALL.** At 85% OEE, 86 presses make ~352,000. |
| **3** | **The over-age green-tyre rule** — QA hold, release, or scrap, and at what threshold? | **This is a COMPLIANCE question, not a planning one.** |
| **4** | **Is the cap-strip opening WIP of 3,005 m real?** All 10 rows are ONE itemcode (`CAP 66`), slit in a **single 2-hour window on 31-May**, **16–18 h old at t=0 against a 24 h life** — they **expire in hour 6–8** of the plan. That is **3.4%** of the plant's own 4-hour buffer target (~87,700 m). *"A 2-hour production island with a 16-hour silence before a 24/7 snapshot looks like a **BROKEN EXTRACT**, not a floor state."* | **Cap strip is on the critical path of EVERY tyre.** |

---

# ON THE 2.66-DAY OVERRUN

**It is the LEAST of the problems, and it is a SYMPTOM, not a disease.**

It exists because the presses lost **3,190 hours** in the cold start (first-48h press utilisation:
**22.7%**; plan wanted 22,909 tyres, schedule delivered **5,513** — **76% short**) and **never got them
back** — the presses have no slack.

**Fix building, and the overrun mostly goes away on its own.**

**Root cause nobody had flagged:** every component department starts at **h=0** — but **BUILDING cannot
start until h=29.2**, and the last TBM (3407) not until **h=58.0**. **Components are made into a
vacuum for a day and a half.**

---

*Both auditors read the same files and reached the same verdict independently. Every number here was
computed from `CTP_SCHEDULE_lead0h/` or `ctp_v6_v2/` and can be reproduced. Where the plant has not
supplied a value, this file says ASK THE PLANT and stops.*
