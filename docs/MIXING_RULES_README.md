# Mixing rules and logic (for applying to another scheduler)

This file lists every mixing rule the CTP scheduler applies:
- **what the rule says;**
- **its value;**
- **where it sits in code / config;**
- **how the logic works.**

It is written so the same rules can be built into another scheduler. The values are those of the latest runs (ncs_v6, v3); they are the same in the scheduler with cutters (v133 / v134).

| | |
|---|---|
| Config | `engine/config.yaml` (blocks named below) |
| Mixing planner | `engine/mixer-improvement/mix_planner.py`, which plans every compound lot |
| Lot sizing | `engine/phases/phase2_lot_sizing.py` |
| Placement and repair | `engine/phases/phase5_forward_placement_v2.py` |
| Butyl rule | `engine/butyl_rule.py` |
| Closed loop | `engine/run_pipeline.py` |
| Audits | `mixing_compliance.py`, `mixing_rule_audit.py`, `rule_adherence_report.py` |

Rule codes (BRM-xx, M-x) are the plant's mixing rule sets of 01-Oct / 08-Oct.

---

## 1. Order in which the rules act

1. **Lot sizing (phase 2):** turn compound need into lots of whole batches, with minimum lot and buffer batches (section 3).
2. **Mixing planner (start of phase 5):** plan **all** master and final compound lots on the mixers first, as a forward simulation that has every rule built in (sections 4–6). These lots are then **fixed**: the rest of the schedule (4RC, extruders, calenders, building) is placed around them, and the repair does not move them.
3. **Phase 5 rule pass:** join split runs of a compound within a shift (BRM-09), and check changeovers / clean-outs once more.
4. **Closed loop (3 rounds):** re-plan the mixers on the real start times of their consumers from the previous round, adding any extra quantity those consumers took.
5. **Audit:** report compliance per rule (section 8).

---

## 2. Mixers and which compound may run where

| Rule | Value | Config |
|---|---|---|
| Mixers | K270M, F270, F440, K310, K430-1, K430-2 | `business_rules.mixers` |
| Allowed mixers per compound | the `machines` column of `CTP_Master_Routing` (MASTER / FINAL MIXING rows) | routing |
| Finals on master mixers | every final compound may **also** run on K270M, K430-1, K430-2, F440, with the routing batch size and cycle | `final_on_master_mixers: {enabled: true, pairs: {"*": [K270M, K430-1, K430-2, F440]}}` |
| Rate on a non-home mixer | run time × factor per "ITEM\|MIXER"; T638 on K430-2 × 1.034 (plant-measured) | `mix_planner.machine_factor` |
| Master compounds | assumed always available, not mixed | `masters_unlimited.enabled: true` |
| Bought-in compounds | never mixed: TBA827, TCA313, MS43, MS70, MB100, MTCM313 | `outsourced_items` + `apply_ctb_approximations.py` |
| No mixer cap | mixers are never capped. 230 batches / shift / final mixer is a **report target only** | `mixing_v47.final_target_batches_per_shift: 230` |
| Maintenance (PM) windows | a lot never runs inside a PM window; it is moved past it | planner `skip_dn()` |

**Why it matters:** routing lists only F270 and K310 for finals. Without finals on the master mixers, the two final mixers run full 22–25 h a day and TBR loses most of its compound (ncs_v5: TBR 30 %; ncs_v6: 79 %).

---

## 3. Lot and batch rules (phase 2)

| Rule | Value | Code | Config |
|---|---|---|---|
| Output in whole batches | every compound lot is rounded **up** to whole batches; kg = batches × batch size | BR-V1 | `business_rules.mixing_output_uom: BATCH` |
| Batch size and cycle | from `CTP_Master_Routing` (`batch_size`, `proc_time`), not `CTP_Master_mixing` | — | `business_rules.mixing_batch_source: ctp_inputfiles/routing_live.csv` |
| Minimum lot | 5 batches for master and final compounds | BRM-01 | `mixing_mpq_min_batches: {MASTER COMPOUND: 5, FINAL COMPOUND: 5}` |
| Buffer batches | +5 batches per compound, not for special / sticky compounds | BRM-13 | `business_rules.buffer_batches: 5`, `special_compounds: [ML405, ML605, 29MG]` |
| Sticky compounds | JT444, JT466 stacked at most 3 batches | BRM-02 | `business_rules.sticky_compounds`, `sticky_stack_batches: 3` |
| Prep (butyl-friendly) lots | 5 batches | BRM-01 | `butyl_day_block.prep_lot_batches: 5`, `mix_planner.prep_lot_batches: 5` |
| Lot within shelf life | a lot is never larger than what is used before its max age, with a 2 h margin | — | `mix_planner.shelf_margin_h: 2` |
| Mixer max / MPQ sheet | not used (user 23-Jul) | — | `use_mixing_mpq_file: false` |

---

## 4. Coverage, repeat gap, one mixer per day

| Rule | Value | Config (`mix_planner`) |
|---|---|---|
| Cover per run | a run takes **every lot of that compound needed in the next 15 h** after its first lot, back to back, with no changeover inside | `cover_h: 15` |
| Long items | **24 h** of need per run for IL3, IL5, B1144, B186, 30G and the compounds that feed 4RC (901) rolls | `cover_h_long: 24`; list in `mixing_v47.long_items`, `long_fourrc_compounds: true` |
| Repeat gap | a compound does **not start a new run within 15 h** of its last run start (24 h for long items) | `gap_h: 15`, `gap_h_long: 24` |
| Exempt from gap | butyl-friendly / prep compounds B99, B1, B2, MB100, MB200, XMB98 | `mixing_v47.exempt` |
| One mixer per day | a compound runs on **one mixer per plant day** (07:00–07:00); it may be a different mixer the next day | planner `daylock` |
| Shelf-life floor | a lot does not start so early that it would pass its max age (− 2 h) before its last use | `shelf_margin_h: 2` |
| Join split runs | a compound's split runs inside one shift are joined into one run if no consumer is delayed by more than 8 h | BRM-09 `business_rules.same_item_shift_merge` |
| Level loading | a peak day's excess final / master demand is made on the days before (shelf-life floor kept) | `mix_planner.level_load.enabled: true` |

---

## 5. Sequencing, changeovers and clean-outs

### 5.1 Time between two lots on a mixer

The time between lot A and lot B is the **largest** of the rules below that apply, plus 1 s margin:

| Case | Time | Code | Config |
|---|---|---|---|
| Different compound (any) | 60 s | — | `mix_planner.changeover_s: 60` (phase 5 repair: `mixer_changeover_min: 1`) |
| Master ↔ final on the same mixer | 60 s + **TR100 clean-out 300 s** = 360 s | BRM-06 | `mix_planner.tr100_s: 300`, `business_rules.master_final_cleanout_item: TR100` |
| After a butyl compound (to a different compound, not inside the IL3 + IL5 campaign) | **120 s clean-out** | BRM-03, BR-V2 | `mix_planner.cleanout_s: 120`, `butyl_sequence_rule.cleanout_sec: 120` |
| After **every** final compound, every mixer | **120 s** | M9 | `mix_planner.final_cleanout_after_s: 120` |
| A final compound on **K430-2** (before or after) | **120 s** | M4 | planner `co()` |
| Same compound back to back | 0 | — | — |

The planner's code (`mix_planner.co()`):
```
co(m, a, b):  if a == b: 0
  c = 60 s
  if one is MASTER and the other FINAL:              c = max(c, 60 + 300 s)   # TR100
  if a is butyl and b not in a's IL campaign:        c = max(c, 120 s)
  if m == K430-2 and a or b is a FINAL:              c = max(c, 120 s)
  if a is a FINAL (final clean-out on):              c = max(c, 120 s)
  return c + 1 s
```

### 5.2 Daily butyl blocks

These come from `butyl_day_block` and `butyl_sequence_rule`. Butyl compounds run **once a day as a block** on a fixed mixer, with a butyl-friendly compound before them (and after, for masters). **A butyl compound must always have a friendly compound directly before it;** if no real friendly lot is ready, a 5-batch prep lot of a friendly compound is made.

| Block | Mixer | Order | Extra |
|---|---|---|---|
| **Final** | F270 | friendly (B99 / B1 / B2) → **IL3 → IL5** (one campaign, no clean-out between) → 120 s → **30G** → 120 s | starts only when its butyl inputs are ready (`start_when_ready`), so F270 never waits inside the block |
| **Master** | K430-2 | friendly (XMB98 / MB200 / MB100) → **29MG → ML405 → ML605** → friendly | not before **17:00** (`earliest_hour: 17`): made in the evening and aged overnight |

More detail:
- **Butyl → butyl or butyl → friendly:** only the 60 s changeover (`butyl_cleanout_to: any` keeps 120 s for butyl → other).
- **Waiting for the repeat gap:** a due butyl item whose 24 h repeat gap ends within 6 h makes the block wait for it, instead of skipping the item that day (`butyl_gap_wait_h: 6`).
- **Broken sequence:**
  - a broken final sequence gets a 120 s clean-out;
  - a broken master sequence gets only the 60 s changeover (BR-V13, `butyl_sequence_rule.final` / `.master`).

### 5.3 Compound followers

ML405 and ML406 are followed by MK3 (BRM-05, `business_rules.compound_followers`).

---

## 6. Priority and timing

| Rule | Value | Config |
|---|---|---|
| Which compound runs next | among the allowed candidates, the one that is **needed soonest** (lowest stock cover) and can start now or within 15 min | `mix_planner.max_wait_min: 15` |
| Inputs first | a final starts only when the master lots it consumes are planned and aged (min aging) | planner `inputs_ok()` / `ready()` |
| 4RC compounds first | compounds that feed 901 rolls (and their masters) ranked **8 h** earlier | `mix_planner.fourrc_priority_h: 8` |
| Item priorities | B616M (CAP 66 compound) 8 h earlier; B1144 and its chain B186 / MB185 / MB184 / MB183 24 h earlier (belt edge filler 1802 never waits) | `mix_planner.item_priority_h` |
| Mixer only waits when nothing can run | the reason is logged | planner |
| Closed loop | rounds 2–3 re-plan the mixers on the round-1 real consumer starts; extra quantity used downstream (4RC, prep runs) is added to the compound lots | `mix_planner.closed_loop: {enabled: true, qty_feedback: true, rounds: 3}` |

---

## 7. The planner step by step (`mix_planner.plan()`)

The planner is a forward event simulation: each time a mixer becomes free at time `t`, it decides what runs next.

```
for each mixer m, whenever it is free at time t:
  1. Butyl block due today on m?  (F270 final block / K430-2 master block, once per plant day,
     after 17:00 for masters, only when its butyl inputs are ready)
       -> run it: friendly -> butyl unit(s) -> clean-out -> friendly ...  ; mark the day done
  2. Otherwise build the candidate list: every compound with lots still to plan where
       - m is an allowed mixer (routing, + master mixers for finals),
       - it is not on another mixer this plant day (one mixer per day),
       - its last run started >= 15 h ago (24 h long items; friendly exempt),
       - its inputs (masters) are planned and ready (<= t + 15 min),
       - it is not too early for its shelf life (max age - 2 h before last use).
  3. Pick the candidate needed soonest (priority hours subtracted: 4RC 8 h, item_priority_h).
  4. Start it at max(t + changeover/clean-out (section 5.1), its ready time), skipping PM windows.
     The run takes every lot of that compound needed within the next 15 h / 24 h, back to back,
     each lot within shelf life at its last use.
  5. Lock the compound to m for the plant day(s) it runs; record the run start (for the repeat gap).
  6. If nothing can run: the mixer waits until the earliest ready time (reason logged).
```

After the planner:
- **Phase 5** places all other machines around these fixed lots.
- **BRM-09** joins split runs within a shift.
- **The closed loop** repeats the plan with the real needs, 3 rounds in all.

---

## 8. How compliance is checked

| Report | What it checks |
|---|---|
| `MIXING_RULE_COMPLIANCE_<run>.xlsx` (`mixing_compliance.py`) | cover, repeat gap, one mixer per day, clean-outs, per compound |
| `MIXING_RULE_AUDIT_<run>.xlsx` (`mixing_rule_audit.py`) | the 01-Oct / 08-Oct rules row by row (M1 friendly before butyl, M3 friendly after, M4 K430-2 120 s, M9 120 s after every final, BRM-01 / 02 / 03 / 05 / 06 / 09 / 13) |
| `MIXING_RULE_EVIDENCE_<run>.xlsx` | the lots behind each check |
| `RULE_ADHERENCE_REPORT_<run>.xlsx` | adherence % per rule, mixing section included |
| `run_log.txt` | planner statistics: prep lots made, butyl gap waits, mixer waits and why |

### Results in ncs_v6 (`RULE_ADHERENCE_REPORT_ncs_v6.xlsx`)

| Rule | Followed | Occurrences |
|---|---|---|
| Whole batches per shift row (BR-V1) | 100 % | 595 |
| Sticky stacks (BRM-02) | 100 % | 23 |
| TR100 master ↔ final (BRM-06) | 100 % | 9 |
| Butyl transitions (BRM-03) | 100 % | 38 |
| Split runs joined (BRM-09) | 100 % | 486 |
| Allowed mixer (BRM-11) | 100 % | 564 |
| One IL campaign a day | 100 % | 8 |
| K430-2 final 120 s (M4) | 100 % | 74 |
| 120 s after every final (M9) | 100 % | 459 |
| Lot ≥ 5 batches (BRM-01) | 97.9 % | 564 |
| Changeover ≥ 1 min (BRM-10) | 99.8 % | 473 |
| One mixer per compound per day | 99.5 % | 382 |
| 15 h / 24 h repeat gap | 98.2 % | 394 |
| 120 s clean-out after butyl | 94.1 % | 17 |
| **Friendly directly before every butyl run** | **57.1 %** | 21 |
| **30G with B1 / B2 / B99 directly in front** | **0 %** | 9 |

**The 30G rule fails** because the final block runs friendly → IL3 → IL5 → 120 s → 30G (`friendly_mode: block`), so 30G has a clean-out in front of it, not a friendly compound. The plant needs to say whether this order is acceptable.

**Not exercised or not checked in ncs_v6:**
- **BRM-05** (ML405 / ML406 followed by MK3) and the master butyl block: master compounds were not mixed (assumed available).
- **BRM-13** buffer batches and **BRM-12** plant cycle times: these have no check.

---

## 9. Applying these rules in another scheduler: checklist

1. **Inputs it needs:**
   - compound lots with need times (from your BOM explosion);
   - per compound: allowed mixers, batch size and cycle time (routing), min / max aging, item type (master / final), and which compounds feed 901;
   - PM windows per mixer.
2. **Size lots** in whole batches: at least 5 batches, +5 buffer batches (not for ML405 / ML605 / 29MG / JT444 / JT466), never more than the shelf life covers.
3. **Plan the mixers first**, with the step-by-step logic in section 7 and the parameters in sections 2–6, and fix those lots before placing anything else.
4. **Changeover function:** use the table in section 5.1, taking the largest time that applies.
5. **Butyl blocks:** one per day per block, on F270 (finals) and K430-2 (masters, after 17:00), always with a friendly compound before them; make a 5-batch prep lot if none is ready.
6. **Allow finals on the master mixers** if masters are bought or assumed available; otherwise the two final mixers become the bottleneck.
7. **Re-plan in a loop** once the downstream starts are known (2 extra rounds).
8. **Audit** each rule after scheduling (section 8). Don't let a later repair move mixing lots.

**Values marked as assumptions in the config** (confirm them with the plant before reuse):
- TR100 clean-out of 5 min (TR100 is not in routing);
- master compounds always available.
