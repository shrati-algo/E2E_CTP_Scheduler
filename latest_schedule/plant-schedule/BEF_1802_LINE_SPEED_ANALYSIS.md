# Belt edge filler 1802 (BEF001): plant line speed against our routing

| | |
|---|---|
| **Plant data** | `beltedgefiller_schedule.xlsx`: 789 rows = 678 rolls (`prod_id`), 30-Sep 23:58 → 11-Oct 11:58, 25,284 kg of B1144 |
| **Metre check** | `CTP_Opening_WIP` snapshot of 10-Oct 06:30 (`inputs/inventory.csv`). It holds 65 BEF001 rolls in metres under the **same `prod_id`** as the plant file |
| **Our data** | Routing `BOM_Routing_9oct`: BEF001 on 1802, 13 M/MIN, efficiency 0.96. BOM: B1144 1.530354 kg per 6.146 m of BEF001 |
| **Scheduler** | v82 runs 1802 at **13.0 m/min**: efficiency override 1.0, no roll change, no stops other than maintenance windows. Check: 174,535 m in 223.8 h |
| **Scripts** | `bef1.py`, `bef2.py` (session scratchpad) |

---

## 1. How the line speed was calculated

1. **One roll = one `prod_id`.** Each roll has 1–2 rows, one per B1144 batch it consumed. The kg are summed per roll: median **37.5 kg** per roll; 10–90 % range 36.3–38.5 kg.
2. **Roll time.** The bookings form an unbroken chain: 677 of 677 rolls start exactly when the previous roll ends, with no gaps or overlaps. So the roll time (end − start) is the time between two roll completions. It includes roll change and any stop.
3. **Metres per roll, measured.** 64 plant rolls are also in our opening-stock feed with metres. The 47 untouched rolls hold:
   - **150 m** (median; 142–163 m) for 37.5 kg;
   - so **0.250 kg/m**, identical to the BOM's 1.530354 / 6.146 = **0.249 kg/m**.

   The kg-to-metres conversion is therefore confirmed. It is applied to all rolls: m = kg ÷ 0.249. Partly used rolls in the feed (2–125 m) were left out.
4. **Speed per roll** = metres ÷ roll minutes. Rolls of normal size (35–41 kg, 643 rolls) are classed by duration:

   | Class | Rolls | Median roll time | Median speed |
   |---|---|---|---|
   | Normal running (≤ 20 min) | 610 | 15.1 min | **9.97 m/min** |
   | Slow (20–60 min) | 12 | 24.6 min | 6.2 m/min |
   | Stop inside the roll (> 60 min) | 21 | 140 min | 1.1 m/min |

5. **Spread of the running speed** (610 normal rolls):

   | Percentile | 5 % | 10 % | 25 % | 50 % | 75 % | 90 % | 95 % |
   |---|---|---|---|---|---|---|---|
   | m/min | 8.7 | 9.2 | 9.8 | **10.0** | 10.0 | 10.4 | 10.8 |

   - The fastest full rolls take 11–13 min, about 11.5–13.6 m/min.
   - One roll shows 6.5 min (23 m/min); that is a booking outlier.
   - The 15 min per roll includes the roll change. If the change takes about 1 min, the pure line speed is about **10.5–11 m/min**.
6. **Throughput as compound:** 37.5 kg per 15 min = **2.5 kg/min (150 kg/h)** of B1144 through the extruder.

## 2. By plant day (07:00 → 07:00)

| Day | Metres | Rolls | Running h | Slow h | Stopped h | Speed while running (m/min) | Speed over 24 h (m/min) |
|---|---|---|---|---|---|---|---|
| 04-Oct | 12,656 | 84 | 20.7 | 0.4 | 2.4 | 9.9 | 8.8 |
| 05-Oct | 10,943 | 73 | 17.6 | 0.4 | 5.8 | 9.9 | 7.6 |
| 06-Oct | 11,165 | 74 | 18.3 | – | 5.9 | 9.8 | 7.8 |
| 07-Oct | 12,505 | 83 | 20.8 | 0.6 | 2.6 | 9.8 | 8.7 |
| 08-Oct | 10,290 | 71 | 16.3 | 0.8 | 6.9 | 9.7 | 7.1 |
| 09-Oct | 11,143 | 75 | 18.4 | 0.8 | 4.8 | 9.6 | 7.7 |
| 10-Oct | 10,892 | 72 | 17.4 | 0.7 | 6.3 | 9.8 | 7.6 |
| **Average** | **11,370** | 76 | **18.5** | 0.5 | **5.0** | **9.8** | **7.9** |

- 01–03 Oct are left out: 02-Oct was a holiday, and one roll spans 27 h.
- **Stops:** 21 stops of 1.5–3.8 h, roughly one per shift at about 02–05 h, 10–13 h and 18–21 h. Each starts 3–4 h into a shift. They are inside a roll, not between rolls, so the line stops with a roll on it. Possible causes are a meal break, cleaning or a compound change; the plant needs to confirm.

## 3. Plant against our routing

| | Plant (measured) | Routing (master) | v82 scheduler |
|---|---|---|---|
| Line speed while running | **≈ 10 m/min** (10.5–11 without roll change) | 13 m/min × 0.96 = 12.5 | **13 m/min** (efficiency 1) |
| Roll change | in the 15 min per roll | not in routing | not modelled |
| Running hours per day | **18.5 h** (16–21 h) | – | up to 24 h when B1144 is available |
| Metres per day | **11,370** (10,290–12,656) | 18,000 at 24 h | 11,743 average (0 on 12–13 Oct, waiting for B1144) |
| Speed over 24 h | **7.9 m/min** | – | – |

**Our running speed is 30 % faster than the plant's.** The plant runs at 77 % of routing speed while running, and loses another 5 h a day to stops. Over a day, the plant gets 61 % of what routing at 24 h would give.

## 4. About 30 m/min

The plant data shows **no running at 30 m/min**:
- at 30 m/min a 150 m roll would take 5 min, but 95 % of rolls take more than 13 min;
- 30 m/min at 0.25 kg/m would draw 7.5 kg/min (450 kg/h) of B1144, three times what the plant puts through (2.5 kg/min);
- the measured roll lengths (150 m per 37.5 kg) rule out a kg-per-metre error.

So 30 m/min is not what the plant ran in 01–11 Oct. It could be:
- the machine's rated / maximum speed;
- a speed limited by the extruder output (150 kg/h), not by the take-up;
- a value for a different item or setting.

If 1802 can really run at 30 m/min, the extruder or B1144 supply limits it today.

## 5. What it means for TBR

At the plan's SKU mix, BEF001 is **5.85 m per TBR tyre** (BOM 5.3–6.3 m; 6.146 m ≈ two edge strips of 3.07 m). 3,500 TBR a day needs **20,458 m a day**.

| 1802 speed | Hours needed per day for 3,500 TBR | Most TBR per day at 24 h | At the plant's 18.5 running h |
|---|---|---|---|
| 10 m/min (plant) | 34.1 h, **not possible** | about 2,460 | about 1,900 (the plant's actual: 11,370 m ≈ 1,940 TBR) |
| 13 m/min (routing, v82) | 26.2 h, **not possible** | about 3,200 | about 2,470 |
| 30 m/min | 11.4 h (47 %) | about 7,380 | about 5,690 |

## 6. Questions for the plant

1. Is 30 m/min the rated speed of 1802? Why does the line run about 10 m/min: the extruder output, the strip profile, or the take-up?
2. What are the stops of about 2 h per shift?
3. How many TBR tyres were built per day on 04–10 Oct? About 1,940 a day of BEF001 was made. If the plant built about 3,500, then BEF001 per tyre is about 3.1 m (one strip) and the BOM's 6.1 m is double.
4. Is there BEF001 stock or a second source (e.g. another line) that we don't see?

**Proposal for the scheduler, once confirmed:** set 1802 to the confirmed running speed (≈ 10 m/min, or the true value) and enter the stops as downtime. This makes the TBR plan realistic. If 30 m/min is confirmed achievable, a v83 at 30 m/min shows the upside.

---

## 7. How the plant keeps 1802 running, and what v82 does differently

**Sources**
- **Plant:** B1144 batch IDs in `consumption_id`, e.g. `F0220260930014020_2` = mixer F02, mixed 30-Sep 01:40:20, batch 2. From these, the mixing time and the compound age at use are known.
- **v82:** phase 5 schedule, phase 2 lots, phase 3 links (scratchpad `bef3.py`, `bef4.py`).

| | Plant (01–11 Oct) | v82 (10–20 Oct) |
|---|---|---|
| Line speed | ≈ 10 m/min | 13 m/min |
| Running pattern | 18.5 h **every day**; only about one 2 h stop per shift | 7–8 h lots run back to back, then idle. **6 idle gaps of 1–57 h, 89 h idle in 10 days** |
| Waiting for B1144 | **never** | yes, after each daily B1144 run is used up |
| B1144 mixing runs | 15 runs in 11 days, every 6–45 h (median about 19 h), **5–12 batches (1.1–2.6 t)** | **one run a day** at about 09–12 h, **13–46 batches (2.8–9.9 t)**, also feeding EG02 (about 40 % of B1144) |
| Mixed how long before 1802 uses it | **13–28 h ahead** (median about 20 h). The next run is always ready before the current one runs out | **Just in time.** 1802 starts about 3 h after the 4 h minimum aging ends |
| B1144 age at use | median 27 h (6–74 h); 8 uses just over 72 h | mostly 3–24 h; never over 72 h |
| Repeat gap between B1144 runs | 2 gaps shorter than 15 h (6.4 h, 9.0 h) | ≥ 15 h kept (BRM rule) |
| BEF001 per day | **11,370 m** (10,290–12,656) | 11,743 m on average, but 0 on 12–13 Oct and up to 18,700 |

**What the plant does that v82 doesn't**
1. **It keeps about one day of B1144 ahead.** Each run is mixed about 20 h before 1802 needs it, so a short or late run never stops the line. v82 mixes B1144 once a day and 1802 consumes it as soon as it has aged 4 h. When the day's run is small (2.8 t on 13-Oct, 3.4 t on 17-Oct, i.e. 11–14 h of 1802), 1802 stands still until the next day's run.
2. **Small, regular runs.** The plant mixes 5–12 batches about every 19 h, a steady 1–2.6 t. v82's runs swing between 2.8 and 9.9 t with the demand of the next 24 h.
3. **It runs slower but continuously.** Its daily output (11,370 m) equals v82's average, although v82's line speed is 30 % higher.
4. **It bends two rules slightly:** 2 repeat gaps under 15 h, and 8 uses up to 73.6 h. v82 keeps both.
5. **It has planned stops** of about 2 h per shift, which v82 doesn't model. v82 is optimistic there.

**A start-up issue in v82 that is not in the plant**
- 1802 is idle from 12-Oct 05:44 to 14-Oct 14:25 (56.7 h).
- BEF001_L0006 was needed on 13-Oct 07:00 but started 14-Oct 14:25.
- The B1144 mixed on 12 and 13 Oct (B1144_L0001 / L0002) is linked to BEF001_L0001–L0004. Those lots already ran on 10–11 Oct using opening stock: 12.7 t of B1144 and 28.8 km of BEF001.
- So the next lots waited for the 14-Oct run (B1144_L0003).

**Effect:** 50 building rows (4,129 TBR tyres) wait for BEF001: 1,036 on 14–15 Oct, 2,591 on 19-Oct.

**What can be done without breaking the rules**
- **B1144 one run ahead.** Mix the next B1144 run when the remaining cover falls below about 20 h, not on the day it is needed. Runs would be steady (about 2 t, 5–12 batches), still at least 15 h apart and within 72 h.
- **Correct the start-up link.** Link the first B1144 runs to the 1802 lots that still need them, earliest need first, as A2 already does for other components.
- **Expected effect, at 13 m/min:**
  - running 18.5 h a day like the plant: about 14,400 m a day (+23 % on v82), about 2,470 TBR;
  - running 24 h: 18,700 m, about 3,200 TBR.

  The 3,500 TBR target still needs the speed or BOM confirmation in section 6.
