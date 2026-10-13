# Why the numbers stop rising — algorithm-level gaps in mixing, 4RC and the sections between

Status 10-Oct-2026. Reference run: **v65** (best: 111,969 cured by 16-Oct; built PCR 94,610 / TBR 18,180).
All mixing rules are at 100 %. All 4RC checks are at 100 % except rule 3 (fabric between steel cord changes, 89 %).
Evidence comes from runs v57–v68 (`latest_schedule/vNN`, `BUSINESS_RULES_LOG.md`) and the analyses in `v60/MIXING_REVIEW_v60.md` and `v65/MIXING_BOTTLENECKS_v65.md`.

---

## 1. In simple words

The scheduler works like an assembly line of separate planners. Each one plans once, in a fixed order, and passes its result to the next:

```
plan rows ─► lot sizing (phase 2) ─► link lots to their inputs (phase 3) ─► "need" times, unlimited capacity (phase 4)
          ─► mixing planner (fixed) ─► 901 + extruders / calenders / cutters / beads placed ─► building ─► curing
          ─► repair passes (can only move lots LATER)
```

Every rule is checked and kept. The output stops rising for **five structural reasons, not because of any single rule**:

1. **Mixing is planned against the wrong clock.** The "need" times come from a plan that assumes unlimited capacity everywhere. Building actually runs 13 h behind on day 1 and 103–128 h behind by 15-Oct, but mixing never learns this. It makes compounds in the order the paper plan wants, not the order the line can use them.
2. **Nothing is ever pulled earlier.** Once a lot is placed, every later pass can only push it later. Holes left by those pushes are never filled. In PCR, 60 % of the late building quantity sits in sections (extruders, calenders, cutters) that are only 40–60 % busy; their lots simply start later than their inputs allow. After building is placed, the aging passes and repairs push a further 2,791 of 3,506 upstream lots later (median 56 h), and nothing pulls them back.
3. **A compound lot is usable only when the whole lot is finished.** Compound lots carry about 24 h of demand (whole batches, ≥ 5 batches). The next section waits for the last batch, even though the first batches have long finished aging.
4. **Mixing capacity is set by routing values, and the plan starts empty.** The routing cycle times are slower than the plant's (IL3 130 s in routing vs 86 s on the floor). Only 3 master items are in stock at 06-Oct 07:00. The final mixers lose about 1,000 batches in the first two days and run at the need rate afterwards, so they never catch up.
5. **901 and the bead chain follow a fixed sequence.** 901 is planned per plant day in wire loads. Its lots stay in the day they were first placed, so a lot starts a median of about 1 day after its compound is ready. The TBR bead winding and apexing share machines 2403–2405 and queue behind each other.

Re-ordering inside one stage does not help while these stay. Live master priority (v66), 901-driven compound timing (v68), peak levelling (v62), longer cover runs (v63) and smaller lots everywhere (v64) all only **moved** a shortage from one day or line to another. The one change that raised output was moving upstream lots **earlier** into idle time (v65, +4,800 PCR).

---

## 2. What the runs proved

| Run | Change | Cured by 16-Oct | Lesson |
|---|---|---|---|
| v57 | mixing planner (rules built in, mixers first) | 104,759 | planning mixers first beats pushing lots later |
| v58 | 4RC compound priority, creel fill | 104,695 | priority moves the shortage between PCR and TBR |
| v59 | spool top-up used as stock | 106,688 | removing waste on 901 frees time |
| v60 | no idle inside dip loads, B616M priority | 107,314 | |
| v61 | butyl blocks as the plant runs them | 105,953 | F270 no longer waits; K310 gives the hours back |
| v62 | peak levelling of mixing demand | 107,223 | the finals are full from day 3, so there is no room to make peaks early |
| v63 | longer cover runs (20 / 30 h) + smaller lots + left-shift | 95,840 | bigger runs on full mixers starve the next compound |
| v64 | smaller lots + left-shift | 104,282 | lots ×2 → +2–3 % batches from rounding; −6 k PCR, +3 k TBR |
| **v65** | **left-shift only** | **111,969** | pulling lots EARLIER into idle time is the lever |
| v66 | live master priority | 110,352 | moves the shortage (09-Oct fixed, 10–12-Oct worse) |
| v67 | butyl clean-out only towards other compounds | 107,505 | near-identical plans differ by ±4 % (sensitivity) |
| v68 | 901-driven compound timing | 109,111 | compound is still ~2 days late; mixers are full |

**Note:** near-identical mixing plans give results within about ±4 % of each other. Only changes worth more than about 4,000 tyres show reliably in a single run.

---

## 3. Section-wise gaps

### 3.1 Final mixing (F270, K310) and masters (F440, K270M, K430-1, K430-2)

**How it is scheduled now** (`mixer-improvement/mix_planner.py`; details in `mixer-improvement/README.md`):
- A forward simulation of the six mixers. Whenever a mixer is free, it runs the most urgent compound the rules allow, for the next 15 h / 24 h of need, back to back.
- Butyl blocks run once a day: masters in the evening, F270 the next morning.
- Lots it plans are fixed for the rest of the run.

**Facts (v65):**

| | Value |
|---|---|
| Busy over the 10 days | finals 84 %, masters 90–95 % |
| Final mixers on day 1 | F270 47 %, K310 27 % (no master stock) |
| Final batches per shift | 194 (target 230) |
| Final batches made vs plan need | 11,545 of 12,513 (92 %); from 10-Oct 800–1,400 behind |
| Master batches made vs plan need | 17,990 of 18,240 (99 %); 44 % of master batches ready after their own need |
| Master time before a final mixer starves that goes to masters needed 12+ h later | ~25 % |
| Final-mixer idle waiting for a master | 101 h (33 h of it on 06–07 Oct) |

**Algorithm gaps:**

| # | Gap | Effect | How to close it (rules intact) |
|---|---|---|---|
| M-1 | **Open loop.** Urgency = phase-2/4 need times computed with unlimited capacity. The actual downstream starts (days later) never come back to mixing. | Compounds are made for consumers that cannot start yet, while consumers that could start wait. Shows as "44 % of masters late" and "25 % of master time on later masters" at the same time. | **Closed-loop re-plan:** run placement, take each compound's **actual** first-consumer start (and kit-feasible building time), re-run the mixing planner on those needs, place again. 2–3 rounds. The planner keeps every mixing rule, because it re-checks them. |
| M-2 | **Whole-lot release.** A consumer may start only when the whole compound lot is finished + min aging. | Each hand-off (master → final → extruder / calender / 901) waits up to the full lot time (~1–3.5 h per lot). This compounds along the chain. | **Batch-group release:** each lot gives its batches to consumers as they finish (+ min aging). Lot sizes, runs and batch counts are unchanged, so there is no extra rounding — unlike v64's smaller lots. Mixing rules are untouched (runs stay back to back, same batches). |
| M-3 | **Greedy dispatch without look-ahead.** The most urgent candidate runs its full 15 h / 24 h cover even when another compound will run out during that run. | Short starvation gaps for the other compounds; final mixers idle while masters make tails of covers. | Before starting a cover run, check whether another compound will run out before this run ends. If so, run only the part of the cover needed now, as long as the rest can still follow before the 15 h / 24 h gap ends. Otherwise keep the full cover. The repeat gap is still respected. |
| M-4 | **Capacity from routing.** Cycle times and batch weights in CTP_Master_Routing are slower than the plant's (83 plant runs faster; IL3 86 s vs 130 s). | Every mixer is planned slower than it runs; finals have no catch-up margin. | **Data:** plant cycle times and batch weights in routing. With +10–20 % speed the final gap (~1,000 batches) closes. |
| M-5 | **Empty start, hard end.** 3 master items in stock at 06-Oct 07:00; no demand after 16-Oct. | ~1,000 final batches lost on days 1–2; masters idle on 15-Oct. | **Data / rolling plan:** load real opening master / final stock, or start from the previous plan's work in progress; give the planner the next week's need so the last day's masters are useful. |
| M-6 | **Fixed lot sizes from phase 2** (24 h spans, whole batches). | Need comes in peaks (2,362 / 2,375 final batches on 10 / 13-Oct vs ~1,300 / day capacity). | Use M-2 instead of smaller lots. If needed, use 12 h lots only for the highest-volume tread / sidewall / innerliner finals. |

### 3.2 4-roll calender (901)

**How it is scheduled now:**
1. 901 lots are placed by phase 5.
2. They are regrouped per plant day: one run per item per day, wire loads together, whole spools / dips, fabric in creel reloads.
3. Patch passes follow: one-go fix, fabric bridge, creel fill, load compaction, spool stock.
4. The final repair runs, moving lots later only.

**Facts (v65):**

| | Value |
|---|---|
| 901 running | 146.5 h of 240 (61 %) |
| Run time for the metres the plan needs | 120 h |
| Run time incl. whole-spool top-up | 194 h |
| 901 lots finishing after their need | 100 / 111 (median 49 h late) |
| Compound late vs the 901 need | median ~45–50 h, for ~80 % of lots |
| 901 start after its compound is ready | median 20–26 h |
| Idle | 93.5 h = creel reloads with no fabric 33.5 h + held 43 h + changeovers 16 h |
| Steel cord loads | 22 for 7 wires (~88 h of creel reloads) vs ~44 h of fabric demand in the plan |

**Algorithm gaps:**

| # | Gap | Effect | How to close it (all 4RC rules intact) |
|---|---|---|---|
| R-1 | **Fixed day assignment.** A 901 lot stays in the plant day where placement first put it; afterwards only later-only repairs. | 901 starts ~1 day after its compound is ready; 43 h "held" idle. | **901 pull-earlier pass after the last repair:** each lot moves to right behind the previous one as soon as its compound is ready. Same sequence; creel reload, whole spools / dips, one run per item per day and shelf life all checked. The code exists inside creel-fill; run it on its own after every repair. |
| R-2 | **Compound late** (gap M-1 / M-2 / M-4). | 80 % of 901 lots wait for skim compound. | Mixing gaps above. Priority alone does not help (v58, v68). |
| R-3 | **Too many cord changes for the fabric available.** | Rule 3 can't reach 100 %; 33.5 h of reload idle. | Larger wire campaigns (a wire's lots up to 5 days ahead in one load). Already allowed, but 271 lots fail "compound not ready" — this follows from R-2. Then plan fabric dips into the reload windows inside the day plan itself, not as an after-pass. |
| R-4 | **Sequential patch passes** (one-go fix, bridge, creel fill, load compact) on top of a fixed day plan, each with its own acceptance test. | Each pass can only fix what the previous ones left; many good moves are refused (e.g. creel fill: every candidate blocked). | **One 901 sequencing step** that builds each day's sequence from ready times + all 4RC rules together, re-run after the repairs, with consumers re-timed afterwards. v48 tried a re-plan without re-timing the cutters and failed for that reason. |
| R-5 | **Whole-spool top-up** (~74 h of metres beyond demand). | 901 time and compound spent on stock. | v59 spool stock already reuses part of it; the rest needs a plant decision (can a part-used spool come off the creel). |

**On "100 % 901 utilisation":** the plan needs only ~120 h of demand run time (~194 h with whole spools) of the 240 h. Running 901 100 % would mean making metres nobody needs. The right target is **901 on time** (lots ready before their need). R-1 and R-2 address that.

### 3.3 Sections between mixing and building (extruders, calenders, cutters, bead machines)

**Facts (v65):**
- Late building quantity by root cause, measured on the schedule as it was when building was placed (corrected 10-Oct; an earlier version measured against the final schedule and overstated the "section itself" share):

  | Root cause | PCR | TBR |
  |---|---|---|
  | The component's section started its lot later than its inputs allowed (machines 40–60 % busy) | **60 %** | 43 % |
  | Waited for compound | **25 %** | 8 % |
  | Waited for the section before it (TBR: bead winding TBWB / SNCT → apexing TBAPBD, both on 2403–2405) | 10 % | **49 %** |
  | Waited for 901 | 5 % | 0 % |

- After building is placed:
  - the aging passes move 1,951 component lots later (towards their late building rows, so they do not age out);
  - the re-cuts move another 1,188 belt / ply / feeder lots;
  - the repair pushes 1,654 upstream lots and 799 building blocks later.
  - In total, 2,791 of 3,506 upstream lots end a median 56 h later than first placed.

| # | Gap | Effect | How to close it |
|---|---|---|---|
| S-1 | **Later-only repair** for all upstream sections. The v65 left-shift runs only once, before the repairs (27 lots moved, +4.8 k PCR). | Lots pushed late by a repair never come back. | **Left-shift after every repair pass** (mixers and 901 excluded, or 901 via R-1). Inputs + aging, shelf life and the changeover allowance are kept. |
| S-2 | **Placement order.** Lots are placed one by one in a fixed order; a later-placed lot cannot take a hole an earlier one left. | Holes on extruders / calenders while building waits. | S-1 closes most of it. A second placement sweep in need order is the fuller fix. |
| S-3 | **TBR bead chain** on shared machines. Winding and apexing are placed independently. | Apexing lots finish 24–122 h after need; TBR days at 18–850 built. | **Chain-aware sequencing:** place each apexing lot directly after its winding lot on the same machine, or split winding and apexing across 2403–2405. All TBR rules kept. |

### 3.4 Building and curing — starvation, scrap and aged-out material

**Facts (v65):**

| | Value |
|---|---|
| Building row median delay vs plan time | PCR 13 → 103 h, TBR 25 → 128 h (06 → 15-Oct) |
| Green tyres scrapped (> 72 h before a press) | 201 |
| Built but never cured in the window (build surplus) | 3,901 tyres, 70 SKUs |
| Consumer lots starting with incomplete material | 48 (CAP 66, EG02 / EG01, TEG-02, EGSL01 — data / BOM gaps) |
| Compound aging in the final schedule | 0 violations (aging checked at placement and repair) |

| # | Gap | Effect | How to close it |
|---|---|---|---|
| B-1 | **Building follows the plan rows** (plan shift, kit gate). When a kit is late the row slides; the backlog grows every day because nothing upstream has spare speed (M-4, S-1). | Starvation of TBMs on dip days; the backlog grows from 13 h to 103–128 h. | Fix the feeders (M-1, M-2, S-1, S-3). Then build kit-ready rows first within the day (already `building_kit_aware_dispatch`). |
| B-2 | **Building not limited by press take-up.** Some SKUs are built more than 72 h before a press can take them, while other SKUs starve. | 201 green tyres scrapped; 3,901 built but not cured in the window. | **Curing-aware building:** a row is built only if a press for that SKU is free within the green-tyre life (72 h). Otherwise its kit goes to a row whose press is waiting. |
| B-3 | **Data gaps** (CAP 66 / edge-gum BOM chains, items with no aging row). | 48 lots start without full material on paper. | **Data:** BOM / routing rows for those items. |

---

## 4. What to do, in order

| Step | Change | Area | Type | Expected effect | Rules |
|---|---|---|---|---|---|
| 1 | Let the repair bring lots and build rows back towards their placement start (S-1) + 901 pull-earlier every 4RC round (R-1) | all upstream, 901 | scheduling | part of the 60 % "section itself" delay; 901 earlier where its consumers' shelf life allows | all kept (inputs, aging, changeovers, creel, one go a day, whole spools) |
| 2 | Batch-group release of compound lots (M-2) | mixing → all consumers | scheduling | earlier hand-offs at every compound step, without the rounding cost of smaller lots | all kept (same runs, same batches) |
| 3 | Closed-loop re-plan (M-1): mixing re-planned on actual consumer starts, 2–3 rounds | mixing, 901, sections | scheduling | the right compounds earlier; fewer "masters made for later" | all kept (planner re-checks every rule) |
| 4 | TBR bead chain-aware sequencing (S-3) | TBR | scheduling | TBR dips on 10 / 12 / 14-Oct | all kept |
| 5 | Curing-aware building (B-2) | building | scheduling | less GT scrap and build surplus | all kept (72 h) |
| 6 | Plant cycle times and batch weights in routing (M-4) | mixing | data | +10–20 % mixing; closes the ~1,000-batch final gap | — |
| 7 | Opening stock / rolling plan (M-5) | mixing, 901 | data | days 06–09-Oct close to plan | — |

Steps 1–5 can be built and tested now without touching any mixing or 4RC rule. Because of the ±4 % run-to-run spread, each should be judged on daily stability (no dip days) and the late-quantity tables, not only the cured total.
