# Mixing cycle times — plant vs scheduler

## Sources

| | Source |
|---|---|
| **Plant** | `latest_schedule/plant-schedule/MIXING_SCHEDULE_PLANT_formatted.xlsx`, sheet *Source data (as received)*: one plant day, 08-Oct 05:42 → 09-Oct 06:28, 2,916 batches on the 6 mixers |
| **Scheduler** | v72, the current config. Cycle = lot run time ÷ batches in the v72 schedule (same as routing in `BOM_Routing_9oct`, efficiency 1) |
| **Per-compound table** | `PLANT_vs_SCHEDULER_MIXING_CYCLE_TIMES.xlsx` (121 compound × mixer pairs) |

## How the plant cycle time is measured

- Each `prod_id` is one batch; its start is the earliest consumption time of that batch.
- A run is consecutive batches of the same compound on the same mixer.
- The **plant cycle** is the median time from one batch start to the next inside a run.
- Gaps over 20 min inside a run (stops) are left out, so the figure is the true batch-to-batch time.

**Limitation:** this is one plant day. Compounds with only a few batches (< 5) are indicative only.

---

## 1. In simple words

**Per batch, the plant mixes faster than we plan — about 8 % faster on average.**
- 57 of 121 compound × mixer pairs are more than 10 % faster in the plant than in our routing.
- 55 are about the same (±10 %).
- 9 are slower.

**Per day, the plant mixes less than our plan.**
- On 08-Oct the plant made **2,916 batches / 774 t**. The v72 plan makes **about 3,120 batches / 850 t** a day once running (10–14 Oct).
- The plant's mixers were busy about **20–21 h** of the 24 h. Our plan keeps them busy **21–24 h**.
- So the faster cycles are used up by stops, changeovers and breaks the routing does not contain.

**Batch weights differ for several compounds** (e.g. XMT1554 / XMT5718 about 340 kg in the plant vs 185–190 kg in routing; XMB604 190 vs 360 kg; MB614 185 vs 285 kg). This changes the kg per hour more than the cycle time does.

**What this means:** loading the plant cycle times alone would add up to ~8 % mixing capacity on paper. That is only real if the mixers also run as many hours a day as our plan assumes. The bigger gaps are the plant's lost hours per day and the batch weights, which need checking with the plant.

---

## 2. By mixer (weighted by the plant's batches)

| Mixer | Plant cycle (s / batch) | Scheduler (s / batch) | Plant as % of ours | Plant 08-Oct: batches / t / busy h | v72 plan per day (10–14 Oct): batches / t / busy h |
|---|---|---|---|---|---|
| F270 (final) | 113 | 125 | 91 % | 627 / 134 / 20 | 635 / 142 / 22 |
| K310 (final) | 117 | 129 | 91 % | 604 / 138 / 20 | 597 / 132 / 21 |
| F440 (master) | 155 | 170 | 91 % | 477 / 162 / 21 | 474 / 158 / 23 |
| K270M (master) | 182 | 203 | 90 % | 387 / 71 / 19 | 486 / 118 / 23 |
| K430-1 (master) | 184 | 198 | 93 % | 415 / 122 / 21 | 441 / 131 / 24 |
| K430-2 (master) | 178 | 178 | 100 % | 406 / 146 / 20 | 488 / 170 / 23 |
| **All** | **149** | **162** | **92 %** | **2,916 / 774 / 121** | **3,121 / 850 / 136** |

**Plant busy h** = batches × plant cycle. It counts only the batch-to-batch time inside runs, so changeovers and stops are the rest of the 24 h.

---

## 3. Largest differences per compound

### Plant clearly faster than routing (≥ 10 batches on 08-Oct)

| Mixer | Compound | Plant (s) | Ours (s) | Plant % |
|---|---|---|---|---|
| K270M | XMT7219 | 99 | 145 | 68 % |
| F440 | XMB95 | 122 | 165 | 74 % |
| F270 | T7220 | 105 | 140 | 75 % |
| K270M | XMT7233 | 109 | 140 | 77 % |
| K310 | T638 | 104 | 133 | 78 % |
| F270 | T1555 | 104 | 131 | 79 % |
| K270M | XMT573 | 115 | 145 | 79 % |
| K270M | XMB604 | 95 | 117 | 81 % |
| K430-2 | MB230 | 146 | 180 | 81 % |
| K430-1 | MR5 | 165 | 200 | 82 % |
| F440 | MT637 | 255 | 310 | 82 % |
| K310 | B96 | 112 | 135 | 83 % |
| K430-1 | XMT406 | 104 | 125 | 83 % |

IL3 on F270 runs at 86 s vs 125 s, but only 6 batches were measured.

### Plant slower than routing (≥ 10 batches)

| Mixer | Compound | Plant (s) | Ours (s) | Plant % | Note |
|---|---|---|---|---|---|
| K430-2 | XMS1146 | 157 | 100 | 157 % | plant batch 370 kg vs 305 kg in routing |
| K430-2 | T638 | 207 | 133 | 155 % | final compound on a master mixer; routing does not allow K430-2 for T638 (see the earlier comparison) |
| K430-2 | XMT1553 | 145 | 120 | 121 % | plant batch 345 kg vs 190 kg |
| F440 | XMT1554RP | 128 | 107 | 119 % | plant batch 345 kg vs 190 kg |
| K310 | T3427 | 143 | 120 | 119 % | |
| K430-2 | MR560 | 138 | 121 | 114 % | |
| K270M | MT4333H | 213 | 195 | 109 % | |

### Batch weight: plant vs routing (≥ 10 batches, difference ≥ 15 %)

| Mixer | Compound | Plant kg / batch | Routing kg / batch |
|---|---|---|---|
| F440 | XMT1554 / XMT1554RP | 345 | 190 |
| F440 | XMT5718 / XMT5719 | 335 | 185 |
| K430-2 | XMT1553 | 345 | 190 |
| K430-2 | XMS1146 | 370 | 305 |
| K270M | XMB604 | 190 | 360 |
| K270M | MB614 | 185 | 285 |
| K270M | MS1036 | 185 | 290 |
| K270M | S54B | 180 | 290 |
| K430-1 | MR56 | 295 | 370 |
| K430-1 | MR5 | 290 | 375 |
| K430-1 | MB603 | 280 | 350 |
| F440 | MB200 | 223 | 360 |

**Not in routing at all** (no scheduler value): 30GWA (F270), MT153X (K270M), B1 / B2 (K310).

---

## 4. What to do with this

1. **Confirm with the plant**, compound by compound:
   - the cycle time for the 57 pairs that run > 10 % faster and the 9 that run slower;
   - the batch weight where plant and routing differ by > 15 %.

   Update `CTP_Master_Routing` (proc_time, batch_size) where the plant confirms.
2. **Find the lost hours per day.** The plant's mixers were busy only about 20–21 h of 24 on 08-Oct. Breaks, cleaning, waiting for material, starting up? The scheduler plans 21–24 h. If these stops are planned (shift changes, cleaning), they should go in as downtime so our plan isn't optimistic. If they are waiting time, our plan shows what the mixers could do without it.
3. **Add the missing routing rows** (30GWA, MT153X, B1, B2) and fix T638's mixer list if K430-2 is allowed.
4. **Effect on our numbers.** With the plant cycle times and the same busy hours, mixing capacity rises by up to ~8 %. The finals would then make about 100 more batches a day, which closes most of the ~1,000-batch final gap over the 10 days. With the plant's real busy hours (20–21 h), it would not rise. The two changes together decide whether mixing stays the bottleneck.
