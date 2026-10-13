# Mixer preference matrix: how the plant uses it, and the proposed rule

| | |
|---|---|
| **Matrix** | `mixer-pref/TBR and PCR compounds batch weight Oct26.xlsx`, sheets TBR and PCR. Per final compound: batch weight on F270 / K310. Per master stage: batch weight on K270M / F440 / K430-1 / K430-2. "--" means the compound does not run on that mixer. 76 finals, 166 masters, 393 allowed compound × mixer pairs |
| **Plant schedule** | `mixing_schedule.xlsx`, one day: 08-Oct 05:42 → 09-Oct 06:28, 2,916 batches. **One day only, so the conclusions are indicative** |
| **Scheduler** | v82 (routing from `BOM_Routing_9oct`) |
| **Scripts** | `mp1.py` (parse), `mp2.py` (plant), `mp3.py` (routing / v82), in the session scratchpad |
| **Mixer codes** | Mixer270F = F270, Mixer310F = K310, Mixer440 = F440, Mixer431 = K430-1, Mixer432 = K430-2, Mixer270 = K270M |

## 1. What the matrix contains

| Mixer | Stage | Batch weight (kg, median / range) |
|---|---|---|
| F270 | final | 215 (200–235) |
| K310 | final | 230 (215–240) |
| K430-2 | master | 360 (330–400) |
| F440 | master | 342 (320–380) |
| K430-1 | master | 295 (265–315) |
| K270M | master | 185 (175–190) |

Mixers allowed per compound:
- finals: 39 have one mixer, 37 have two;
- masters: 68 have one, 73 two, 18 three, 2 four.

## 2. How the plant used it on 08-Oct

| Check | Result |
|---|---|
| Compounds of the day found in the matrix | 116 of 122 (2,865 of 2,916 batches). Not in it: 30GWA, B1, B2, MS1066, MT153X, T7110 |
| **Batches on a mixer the matrix allows** | **2,714 of 2,865 (95 %)** |
| On a mixer the matrix does not allow | 151 batches, 9 compounds: T638 on K430-2 (32), JT444 on K310 (30), MA101 on F440 (20), XMT5718 / 5719 on F440 (31), XMS1146 on K430-2 (15), S54B on K270M (10), XMT7219 on K430-1 (9), MB100 on K430-2 (4) |
| **Batch weight = the matrix weight for the mixer used (±5 %)** | **99 of 110 compound × mixer pairs** |
| Compound on one mixer for the whole day | 119 of 122 (split: MB185, T638, XMT7219) |
| Compounds with a choice, run on their heaviest-batch mixer | **803 of 1,431 batches (56 %)**: finals 309 / 562, masters 494 / 869 |

**Second-choice mixers carry a lot of work:**

| Mixer used instead of the heavier one | Batches | Examples |
|---|---|---|
| F270 instead of K310 (finals) | 253 | T4088, T1555, PCRT15, T15, B1144, B815, T6730 |
| K270M instead of K430-1 / K430-2 | 183 | XMB604, XMT507, MT5717, XMT572 / 573, MB614 |
| K430-1 instead of K430-2 | 107 | MR56, XMB98, MR5 |
| F440 instead of K430-2 | 85 | XMB175, MT34, MB151G |

**Mixer loads that day:**

| Mixer | Batches |
|---|---|
| F270 | 627 |
| K310 | 604 |
| F440 | 477 |
| K430-1 | 415 |
| K430-2 | 406 |
| K270M | 387 |

Every mixer is kept busy.

**What this means:** the plant uses the matrix as
1. the list of mixers a compound **may** run on, and
2. the batch weight to use on each of them.

It does **not** always send a compound to its biggest batch. It spreads the compounds so that all six mixers stay loaded. Each compound stays on one mixer for the day. The small K270M is filled with the compounds it is allowed to make.

## 3. Our scheduler today

| Check | Result |
|---|---|
| Mixer eligibility | From **routing**, which allows **66 more compound × mixer pairs** than the matrix (e.g. 30G, A1133, B107, B460 on K310; MB100 / MB200 on K430-2). The matrix never allows a pair that routing doesn't |
| Batch weight | Routing = matrix (±2 %) in only **217 of 384** shared pairs |
| v82 mixer choice | Compounds with a choice: 53 % of batches on the heaviest mixer. **About 4,200 batches** on mixers the matrix doesn't allow |
| Matrix vs routing coverage | 87 mixing compounds in routing are not in the matrix; 5 matrix compounds are not in routing |

## 4. Proposed business rule (to confirm with the plant)

1. **Allowed mixers = the matrix.** A compound may run only on a mixer with a batch weight in the matrix. "--" = never. Routing alone is not enough. The 9 exceptions above need the plant to say whether they are approved alternates.
2. **Batch weight = the matrix value for that mixer.** Quantity is planned in whole batches of that weight, so the number of batches depends on the mixer chosen.
3. **Preference order among the allowed mixers:** the mixer that makes the compound fastest in kg per hour (matrix batch weight ÷ cycle time). This usually means the biggest batch:
   - finals: K310 → F270;
   - masters: K430-2 → F440 → K430-1 → K270M.
4. **Second choice when the first is full:** go down the list rather than wait, if the compound would otherwise be late. Every mixer should have work. The small mixer takes compounds whose bigger mixers are fully booked.
5. **One mixer per compound per plant day**, which our current rule already does. Fixed exceptions take priority:
   - the daily butyl blocks (K430-2 masters, F270 IL3 / IL5 / 30G);
   - single-mixer compounds.
6. **Cost of a second choice** = the extra batches (lighter batch) and time. The planner picks the combination that delivers the day's need with the fewest mixer hours, while every mixer stays loaded.

### In simple words
Each compound has a short list of mixers it is allowed to run on, each with its own batch size.
- First choice: the mixer that makes it fastest, usually the one with the biggest batch.
- Each day, compounds that can only run on one mixer are placed first.
- Then each remaining compound goes to its first-choice mixer if there's room. If not, it goes to its next allowed mixer, so no mixer stands idle and no compound waits.
- A compound stays on the mixer it was given for the whole day.
- On a second-choice mixer the batch is smaller, so more batches are needed.

### Open questions for the plant
1. Is the matrix the full list of allowed mixers, so routing's extra 66 pairs should be removed? Are the 9 exceptions (T638 on K430-2, JT444 on K310, …) approved?
2. Is "fastest kg per hour first" the plant's real order? Or is there a fixed home mixer per compound, e.g. finals on F270 by habit?
3. What are the cycle times per compound × mixer to go with these batch weights? `PLANT_vs_SCHEDULER_MIXING_CYCLE_TIMES.xlsx` has one day's measurements.
4. A few more days of the plant's mixing schedule, to confirm the pattern beyond 08-Oct.
