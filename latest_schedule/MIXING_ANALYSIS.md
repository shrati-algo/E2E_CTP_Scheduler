# Mixing analysis — why the mixers do not make enough batches, and what the mixing rules cost

Plan window: 06-Oct 07:00 → 16-Oct 07:00 (240 h) · October plan files · opening WIP snapshot 06-Oct 07:00.
Versions analysed: v42 (best output on the 08-Oct masters), v47 (08-Oct mixing rules, first attempt), v52 (v42 rules on the updated 09-Oct masters), v53 / v54 (later attempts at the 08-Oct rules), v55 (run groups — results at the end).
Mixers: final F270, K310 · master F440, K270M, K430-1, K430-2. Mixing time = CTP_Master_Routing min/batch, efficiency 1, **no batch cap**.

---

## Part 1 — In simple words

1. **The plan asks for almost all of the mixers' time from the first hour.** To make every batch needed before 16-Oct, the final mixers would have to run 91–101 % of the time and the master mixers 95–104 %. Even a perfect schedule could not make all of it in 10 days. With the updated masters (09-Oct) the need went up by about 10 %.
2. **The final mixers wait for master compound.** A final batch can only start when its master batch is mixed and rested. Little usable compound is in stock at the start, so on 6–8 Oct everything is due at once and finals wait for masters.
3. **The biggest avoidable loss: the schedule loses mixer time after the first plan.** When the lots are first placed, the mixers are 79–97 % busy. The later steps (fitting the IL campaign, the daily butyl blocks, and the repair passes) push almost every mixing lot later — by about two days in the middle — and the repair can only move lots later, never back. That leaves holes where the mixer stands idle while other ready work waits.
4. **One master compound appears in the final mixer — T857.** It is labelled "master compound" in the item-type master, but its routing and recipe say it is a final compound (made from master MT872 + final chemicals on F270 / 310). Only the label is wrong.
5. **The 08-Oct rules (one mixer per compound per day; no repeat within 15 h / 24 h) cost output in every way tried so far,** because each attempt made late compounds wait (v47, v53), pushed lots around afterwards (v48, v49), or made one huge lot whose first batches could not be used until the whole lot was finished (v54). v55 tries the way the plant described it: the batches stay separate, run back to back on one mixer with no changeover, and the compound does not come back within 15 h / 24 h.

**Final mixers today:** about 137 (F270) and 128 (K310) batches per shift against the 230 target (v52).

---

## Part 2 — Details

### 2.1 Mixing rules and compliance by version

| Rule | v42 | v47 | v52 | v53 | v54 |
|---|---|---|---|---|---|
| Final Mixer – Before Butyl | 100 % | 100 % | 100 % | 100 % | 100 % |
| Final Mixer – After Butyl (120 s) | 100 % | 100 % | 100 % | 100 % | 100 % |
| Master Mixer – Butyl Sequence | 100 % | 100 % | 100 % | 100 % | 100 % |
| IL3 and IL5 together in one go a day | 100 % | 100 % | 100 % in the window* | 100 % | 100 % |
| Mixing output in batches | 100 % | 100 % | 100 % | 100 % | 100 % |
| One compound in one mixer per day | 87.4 % | 100 % | 90.2 % | 99.0 % | 99.6 % |
| Same compound not within 15 h (24 h IL / 4RC) | 69.0 % | 100 % | 73.5 % | 100 % | 99.9 % |
| **Cured by 16-Oct** | **82,719** (08-Oct masters) | 65,986 | **73,155** | 50,865 | 33,509 |
| Final batches / shift F270 / K310 (target 230) | 148 / 128 | 70 / 88 | 137 / 128 | 67 / 65 | 36 / 43 |

\* one plant day with two IL campaigns, on 20-Oct (after the plan end).
Other mixing checks (lot ≥ 5 batches, sticky stacks, 60 s changeover, TR100 master↔final clean-out, plant-allowed mixer, one IL campaign per day with B99 in front) are 100 % in all of these versions.

### 2.2 Capacity needed vs available (plan window)

| | Batches needed before 16-Oct | Run time (routing) | Mixer time needed | Mixer time available | Load |
|---|---|---|---|---|---|
| Final (v42) | 12,513 | ≈ 2.1 min | 438 h | 2 × 240 = 480 h | **91 %** |
| Final (v52) | 13,825 | ≈ 2.1 min | 484 h | 480 h | **101 %** |
| Master (v42) | 18,240 | ≈ 3.0 min | 912 h | 4 × 240 = 960 h | **95 %** |
| Master (v52) | 19,906 | ≈ 3.0 min | 995 h | 960 h | **104 %** |

Made in the window: v42 final 8,296 / master 13,512; v52 final 7,905 / master 13,594. 230 batches per shift at 2.1 min = a full 8 h shift with no gap at all.

### 2.3 Where the mixer time goes (idle analysis)

Every idle gap on every mixer, classified by what the next lot was waiting for.

| | v42 | v47 | v52 |
|---|---|---|---|
| Mixers busy (each) | 134–187 h (56–78 %) | 74–148 h (31–62 %) | 134–181 h (56–75 %) |
| Idle total (6 mixers) | 478 h | 727 h | 488 h |
| — next lot waiting for its master / compound | 313 h | 259 h | 292 h |
| — inputs ready but held by sequence / repair order | 148 h | 449 h | 161 h |
| — held by the shelf-life start limit | 0 h | 0 h | 0 h |
| **Idle while other READY lots existed for that mixer** | **438 h** | **684 h** | **442 h** |
| Compound lots finishing after their first need | 1,458 / 1,760 | 1,757 / 2,080 | 1,571 / 1,812 |

Per mixer in v52 (idle h): F270 96 (57 waiting for master), K310 106 (63 waiting for master), F440 86, K270M 75, K430-1 68, K430-2 59.

**Example (v52, K310):** idle 13.2 h from 09-Oct 16:16 to 10-Oct 05:26 waiting for the master of R5606, while B370 (4RC compound, needed 07-Oct), A5542, T3427, JT444, T4088 (needed 08–09 Oct) had their inputs ready and were late.

### 2.4 The mixers are full at placement — the later steps empty them (v52)

| | Mixer hours busy in the window |
|---|---|
| Right after placement | **1,294 h** (F270 202, K310 189, F440 223, K270M 227, K430-1 232, K430-2 221) |
| Final schedule | **950 h** (F270 144, K310 134, F440 154, K270M 165, K430-1 172, K430-2 181) |

What moves the lots (v52 run log):
- IL3 / IL5 daily campaign needs room: 798 mixer lots pushed later.
- Daily butyl blocks: 1,746 lots on the block mixers moved later.
- Each final-repair pass: ~4,000 upstream lots pushed later (35,000–102,000 h per pass), to keep changeovers, inputs ready and aging.
- Result: 1,768 of 1,819 mixing lots end later than placed — median 51 h, 90th percentile 90 h — and only 7 earlier.

The repair can only move a lot later and keeps the order on each machine, so a hole left by a pushed lot is never filled by another ready lot.

### 2.5 Master compound in the final mixer — T857

| Source | T857 |
|---|---|
| itemtype_master / BOM description | MASTER COMPOUND |
| CTP_Master_Routing | FINAL MIXING on F270 / 310, 1.83 min/batch, 200 kg |
| BOM recipe | MT872 (master, made on 430-2) + T857FC (final chemicals) |
| Used in | treads TRD 2658 / 2617 / 2455 / 2367 / 2177 … |

The scheduler picks the mixer from routing only, so T857 runs on F270 / K310 (6–7 lots per version). By recipe and routing it is a final compound; **the item type should be corrected to FINAL COMPOUND** (plant data). No other master compound runs on a final mixer, and no final on a master mixer. CTP_Master_BuildingAllowable is not involved (it only covers building machines).

### 2.6 The 08-Oct rules — what was tried and why it failed

| Version | How the rule was applied | Cured | Why it lost output |
|---|---|---|---|
| v47 | each compound fixed to ONE mixer for the whole plan; repair holds a run until 15 h / 24 h after the previous one | 65,986 | the fixed mixer removed flexibility; late compounds waited out the gap while mixers idled |
| v48 | v47 + late lots joined the current run afterwards + 901 re-plan | 26,307 | inserting lots afterwards pushed everything behind them; the pushes cascaded |
| v49 | one mixer per DAY + catch-up + queue unblock | 26,437 | same cascading pushes (final T638 started before its master was finished) |
| v53 | gap rule applied at placement | 50,865 | a lot that could not join its run directly was pushed 15–24 h; mixers already idle at placement |
| v54 | late batches merged into one big lot | 33,509 | a lot is usable only when the whole lot is finished, so a 24 h run made the first users wait up to 24 h |
| v55 | batches stay separate, run back to back on one mixer, no changeover | 49,412 | the later batches of a run were often not ready when their slot came (their master was mixed later): 4,466 h of waiting inside runs, 222 of 302 runs broken by another lot, then held by the gap check |

### 2.7 What can be done while following the rules

| # | Fix | What it addresses | Status |
|---|---|---|---|
| 1 | Keep each compound's due batches separate but back to back on one mixer (run groups) | 08-Oct rules without waiting; fewer changeovers | v55 |
| 2 | Reserve the IL-campaign and butyl-block slots at placement instead of inserting them afterwards | removes the 798 + 1,746 pushes | proposed |
| 3 | Let a mixer run a ready lot when the next lot waits for its master (inside the repair, keeping all mixing rules) | the ~440 h of idle with ready work waiting | proposed |
| 4 | Masters first: priority for 24 h-shelf masters and 4RC compounds on 6–8 Oct | finals and 901 waiting for masters | proposed |
| 5 | Correct T857 item type | master compound shown in final mixing | plant data |
| 6 | Confirm master run times (2.8–3.2 min/batch), the 0 kg master stock and the 24 h master shelf life; check why the 09-Oct BOM / routing raised compound demand ~10 % | capacity ceiling | plant data |

### 2.9 v56 / v57 — stock-coverage mixing planner (mixer-improvement)

| | v42 | v52 | v47 (all rules) | **v57 (planner)** |
|---|---|---|---|---|
| Cured by 16-Oct | 82,719 | 73,155 | 65,986 | **104,759** |
| PCR / TBR cured | 71,628 / 11,091 | 65,134 / 8,021 | 63,956 / 2,030 | **86,788 / 17,972** |
| Mixer busy in the window (6 mixers) | 962 h | 950 h | 713 h | **1,300 h** |
| Final batches / shift F270 / K310 | 148 / 128 | 137 / 128 | 70 / 88 | **191 / 196** |
| 01-Oct mixing rules | 100 % | 100 % in window | 100 % | **100 %** |
| One mixer per day / no repeat 15 h / 24 h | 87 / 69 % | 90 / 74 % | 100 / 100 % | **100 / 100 %** |
| 901 utilisation | 52.5 % | 51.9 % | 40.2 % | **66.1 %** |

All compound lots are planned first as a forward simulation of the mixers (needed-soonest first, masters ahead of finals, runs covering 15 h / 24 h, daily butyl / IL blocks); nothing pushes them afterwards. v56 (first build) reached the same output but broke the butyl sequence (friendly compound skipped, a join pass still moving planned lots, TR100 too short); fixed in v57. Code: `mixer-improvement/mix_planner.py`; BOM / routing from `BOM_Routing_9oct`.

### 2.8 v55 — run groups (results)

| | v51 (v47 settings, 09-Oct data) | v52 (08-Oct rules off) | **v55** |
|---|---|---|---|
| Built / cured | 57,568 / 62,247 | 71,435 / 73,155 | **43,825 / 49,412** |
| TBR cured | 3,016 | 8,021 | 3,346 |
| 01-Oct mixing rules | 100 % | 100 % in the window | 100 % |
| One compound in one mixer per day | 100 % | 90.2 % | 99.4 % |
| Same compound not within 15 h / 24 h | 99.9 % | 73.5 % | 100 % |
| Mixers busy in the window (6 mixers) | 668 h | 950 h | 534 h |
| Final batches / shift F270 / K310 | 71 / 88 | 137 / 128 | 62 / 72 |
| 4RC checks at 100 % | 9 / 13 | 12 / 13 | 10 / 13 |

**What happened:** 308 lots were grouped into 302 runs of separate batches. When the first batch of a run was placed, the time of the others was reserved behind it — but the later batches' inputs (their master compound) were often ready much later, so the mixer waited inside the run (4,466 h in total) and in 222 runs another compound had to go between, splitting the run. The repair then held 164-191 runs per pass to keep the 15 h gap, and the pushes cascaded (first repair 612,397 h).

**Conclusion after v47-v55:** with this plan and data the mixers are needed ~100 % of the time and most compounds start late, so any way of forcing "no repeat within 15 h / 24 h" as a hard rule makes some mixer or consumer wait, and the waiting spreads. On the 09-Oct data:
- **best output:** v52 -- 73,155 cured, 08-Oct rules met 90.2 % / 73.5 %;
- **best output with the 08-Oct rules met (~100 %):** v51 -- 62,247 cured (-15 %).

---

*Files per version: `MIXING_RULE_AUDIT_vNN.xlsx` (rule-by-rule violations, final batches per shift), `MIXING_RULE_EVIDENCE_vNN.xlsx` (every occurrence and how it is followed), `MIXING_RULE_COMPLIANCE_vNN.xlsx` (internal mixing checks), `RULE_ADHERENCE_REPORT_vNN.xlsx`. History of every change: `BUSINESS_RULES_LOG.md`.*
