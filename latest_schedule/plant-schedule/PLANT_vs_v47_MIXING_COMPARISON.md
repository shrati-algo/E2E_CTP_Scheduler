# Plant mixing schedule vs v47 — comparison, rule compliance and data checks

Plant data: `mixing_schedule.xlsx` (plant actual, 08-Oct 05:42 → 09-Oct 06:28, 2,916 batches, data not changed).
v47: the v47 schedule (`latest_schedule/v47/MIXING_SCHEDULE_v47.xlsx`) in the **same window**.
Masters used for the checks = the files v47 used (routing, BOM, aging, item master of the v47 run).
Full detail: `PLANT_vs_v47_MIXING_COMPARISON.xlsx` (sheets: Overview, Rule compliance, Plant rule violations, v47 rule violations, Plant data checks, Plant aging detail, By mixer, By shift, By compound, Notes). Script: `compare_plant_v47.py`.

---

## Part 1 — In simple words

1. **The plant mixes far more than v47 plans:** 2,916 batches in the day vs 1,690 in v47 (+73 %). On the final mixers the plant made **1,231** batches (about **205 per shift per mixer**) vs **589** in v47 (about 98). In the night shift (C) v47 runs only 82 final batches, the plant 522.
2. **The plant runs faster and with different batch weights than our masters say.** In 83 runs the plant's batch-to-batch time is shorter than the routing cycle (e.g. IL3 86 s vs 130 s in routing), and 34 compound/mixer pairs use batch weights far from the routing batch size (e.g. XMT1554 / XMT5718 ≈ 340 kg vs 185–190 kg in routing; MB200 223 kg vs 360 kg). **v47 is limited by these master values — this is a main reason the scheduled mixers look under-loaded.**
3. **The plant breaks several mixing rules on this day:** butyl sequences without the friendly compound in between, two clean-outs shorter than 120 s, 3 compounds on two mixers in one day, and 14 of 19 repeats of a compound within 15 h. **v47 has no rule violation** on the same day.
4. **The plant uses master compound older than the shelf life in our aging master:** XMB175 at 63–64 h, XMB95 at 44–45 h, XMB98 at 25–33 h (master data: max 24 h); MT4444H, MT1400, MT1511 at 74–114 h (max 72 h). Either the plant uses over-age masters, or the 24 h / 72 h limits in CTP_Master_Aging are too strict — which also makes the scheduler wait (see `MIXING_ANALYSIS.md`).
5. **Six compounds ran on mixers the routing does not allow** (e.g. T638 on K430-2, MA101 / XMT5718 / XMT5719 on F440, S54B on K270M, XMT7219 on K430-1), and 4 compounds have no mixing row in routing (30GWA, MT153X, B1, B2).

---

## Part 2 — Details

### 2.1 Volume and pattern (same window)

| | Plant | v47 |
|---|---|---|
| Batches | **2,916** | 1,690 |
| Final / master | 1,231 / 1,685 | 589 / 1,101 |
| Compounds | 122 | 111 |
| Runs (consecutive same compound on a mixer) | 144 | 117 |
| Compound changes on the mixers | 138 | 111 |
| Compounds in both / plant only / v47 only | 70 / 52 / 41 | |

| Mixer | Plant batches | v47 batches | Plant runs | v47 runs |
|---|---|---|---|---|
| F270 (final) | 627 | 279 | 38 | 15 |
| K310 (final) | 604 | 310 | 25 | 12 |
| F440 | 477 | 290 | 22 | 23 |
| K270M | 387 | 273 | 18 | 22 |
| K430-1 | 415 | 300 | 19 | 22 |
| K430-2 | 406 | 238 | 22 | 23 |

| Shift | Final plant | Final v47 | Master plant | Master v47 |
|---|---|---|---|---|
| A | 304 | 257 | 521 | 441 |
| B | 405 | 250 | 580 | 349 |
| C | 522 | 82 | 584 | 311 |

Compounds the plant made that v47 did not (largest): B96 70, B176 67, B99 65, S46 50, XMB175 42, A1133 40 batches — 4RC / TBR compounds that v47 had placed later. v47-only (largest): MT3704 25, T9496 24, MB231 22, T4027 20, MS43 19.

### 2.2 Mixing-rule compliance

| Rule | Plant checked | Plant violations | Plant % | v47 checked | v47 violations | v47 % |
|---|---|---|---|---|---|---|
| Final Mixer – Before Butyl | 2 | 2 | 0 % | 0 | 0 | – |
| Final Mixer – After Butyl (120 s) | 6 | 2 | 66.7 % | 1 | 0 | 100 % |
| Master Mixer – Butyl Sequence | 3 | 3 | 0 % | 1 | 0 | 100 % |
| 430-2 Master Mixer – Final Compound | 1 | 0 | 100 % | 0 | 0 | – |
| IL3 and IL5 together in one go a day | 1 | 0 | 100 % | 0 | 0 | – |
| One compound in one mixer per day | 124 | 3 | 97.6 % | 110 | 0 | 100 % |
| Same compound not within 15 h (24 h IL / 4RC) | 19 | 14 | 26.3 % | 94 | 0 | 100 % |
| Changeover time (60 s; TR100 5 min) — *indicative for the plant* | 138 | 22 | 84.1 % | 117 | 0 | 100 % |
| Mixing output in batches | 2,916 | 0 | 100 % | 132 rows | 0 | 100 % |

**Plant violations (main ones):**
- *Before Butyl* (F270): IL3 at 08-Oct 07:14 directly after 30G (no B1 / B2 / B99); 30GWA at 07:31 directly after IL3.
- *After Butyl*: 30G → IL3 only ~101 s (F270, 08-Oct 07:14); ML405 → ML605 only ~94 s (K430-2, 08-Oct 21:12).
- *Master Butyl Sequence* (K430-2, 08-Oct evening): 29MG → ML405 → ML605 back to back — no MB100 / MB200 / XMB98 between the butyl compounds (MB100 only before 29MG and after ML605).
- *One mixer per day*: XMT7219 (K270M + K430-1), T638 (K310 + K430-2), MB185 (K430-1 + K430-2).
- *Repeat within 15 h*: T7220 (runs 2.6 h and 2.2 h apart), T4026 (1.4 h), MT4024 (1.9 h), XMT4025 (2.3 h), MT637 (2.3 h), MB185 (2.8 h and 14.1 h), T638 (6.7 h, other mixer), HS106 (9.4 h), T1555 (10.4 h), T4555H (10.5 h), T4088 (10.7 h), MB94 (11.7 h).
- *Changeover* (indicative — the plant file has no batch end time, so the end of a run is estimated from its batch rhythm): 22 compound changes look shorter than 60 s (or 5 min for master ↔ final, e.g. MS1066 on F270).
- The first run of each mixer in the file (before 08-Oct ~06:30) cannot be checked against what ran before it.

### 2.3 Plant data checked against the masters v47 uses

| Check | Findings | Examples |
|---|---|---|
| D1 Mixer allowed by routing (CTP_Master_Routing machines) | **6 violations**, 4 no data | T638 on K430-2 (routing F270 / K310); MA101 on F440 (K430-1 / K430-2); XMT5718, XMT5719 on F440 (K270M / K430-1); S54B on K270M (K430-1 / K430-2); XMT7219 on K430-1 (K270M). No routing row: 30GWA, MT153X, B1, B2 |
| D2 Batch weight vs routing batch_size (> ±5 %) | **34** compound / mixer pairs | XMT1554 / XMT1554RP 345 kg vs 190 (+82 %); XMT5718 / 5719 335 vs 185 (+81 %); MB200 223 vs 360 (−38 %); MB614 185 vs 285; MS1036 185 vs 290; B278 128 vs 220 |
| D3 Recipe vs BOM (items and kg per batch, > ±5 %) | **78** deviations, 4 no BOM | 29MG: 1055083100 56 kg vs 65 (−14 %); A1031: XMA103 195 vs 219 kg; A5542: item "WA" not in BOM |
| D4 Aging of consumed compounds (CTP_Master_Aging) | **117 consumption rows over max aging** (of 3,795 checked) | XMB175 63–64 h (max 24); XMB95 44–45 h (max 24); XMB98 25–33 h (max 24); MT4444H 78–114 h, MT1400 109 h, MT1511 74 h (max 72). No "too fresh" (min aging) cases |
| D5 Run ≥ 5 batches (BRM-01) | **11 runs** | T7220 2, T1464 2, T7234 2, S54AR 2, MB185 2, HS106 3, T7110 3, ML605 3, B278 4, R130 4, R158 4 |
| D6 Batch cycle vs routing | **83 runs faster** than routing (< 90 %), 2 slower | IL3 86 s vs 130; 30G 121 vs 136; B107 110 vs 136 s |
| D7 Sticky compounds in stacks of 3 | none | — |

### 2.4 What this means for the scheduler

- **Capacity in the masters is lower than on the floor.** Routing cycles are longer and several batch sizes smaller than the plant uses, so the scheduler plans fewer batches per mixer-hour than the plant achieves (plant ≈ 205 final batches per shift per mixer — close to the 230 target).
- **Shelf life:** the plant routinely uses 24 h masters at 25–64 h and 72 h masters at 74–114 h. If that is accepted practice, CTP_Master_Aging should be corrected; it is one of the reasons the scheduler waits for masters.
- **Routing mixers:** compounds the plant runs on other mixers (D1) and items missing from routing (30GWA, MT153X, B1, B2) should be added to CTP_Master_Routing if those are valid.
- **Rules:** the plant itself does not follow the butyl sequence, the 15 h repeat rule or one-mixer-per-day on this day; v47 follows all of them but produces far fewer batches.

| # | Plant data action | Effect on the schedule |
|---|---|---|
| 1 | Confirm routing cycle times (min/batch) for the mixers (D6) | more batches per hour in the plan |
| 2 | Confirm batch sizes (D2) | correct kg per batch and batches needed |
| 3 | Confirm shelf life of XMB95 / XMB175 / XMB98 (24 h) and MT4444H / MT1400 / MT1511 (72 h) (D4) | fewer waits for masters |
| 4 | Add the allowed mixers / missing items to routing (D1) | the plan can use the same mixers as the floor |
| 5 | Check BOM recipes with large differences (D3) | correct consumption |
