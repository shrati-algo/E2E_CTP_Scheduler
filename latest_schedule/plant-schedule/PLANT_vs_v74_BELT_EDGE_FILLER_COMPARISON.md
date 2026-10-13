# Belt edge filler 1802 (BEF001): plant vs v74 / v72

## Sources

| | Source |
|---|---|
| **Plant** | `beltedgefiller_schedule.xlsx`: 789 rows, 678 bookings, 30-Sep 23:58 → 11-Oct 11:58. Formatted (data unchanged) in `BELT_EDGE_FILLER_SCHEDULE_PLANT_formatted.xlsx` |
| **Scheduler** | v74 (current config) and v72, machine 1802 |
| **Common window** | the full plant days 06–10 Oct |
| **Tables** | `PLANT_vs_v74_BELT_EDGE_FILLER_COMPARISON.xlsx`. Script: `format_compare_plant_bef.py` |

The plant books only the B1144 used (kg) per booking. Two conversions are used throughout:
- **Metres:** kg ÷ the BOM's 0.249 kg/m (1.530 kg per 6.146 m).
- **TBR tyre equivalent:** metres ÷ 6.13 m per tyre.

---

## 1. In simple words

**The plant's edge filler runs slower but steadily. Ours runs faster but stops and starts.**

| 06–10 Oct, per day | Plant | v74 | v72 |
|---|---|---|---|
| Metres of edge strip | **11,200** | 12,900 | 13,400 |
| TBR tyres it can supply | **about 1,830** | about 2,110 | about 2,190 |
| Running hours | **19.1 h** | 16.6 h | 17.2 h |
| Running speed | **about 9.8 m/min** | 13 m/min (routing) | 13 m/min |
| How it stops | about 3 stops a day of about 2 h, one per shift; never idle between bookings | 6 idle gaps of 8–18 h in 10 days, waiting for B1144 | 6 gaps of 6–14 h |

1. **Speed is a data difference.** The plant makes about 37.5 kg (about 150 m) in 15 min, which is **9.8 m/min**. Our routing says **13 m/min**, so our plan is about 33 % faster per hour than the plant.
2. **The plant doesn't wait for compound. We do.**
   - The plant's 1802 runs every day. It loses about 5–7 h a day to regular stops of about 2 h, roughly 3–4 h into each shift (around 02–03 h, 10–11 h, 18–19 h).
   - v74's 1802 sits idle for 8–18 h at a time until the next B1144 is ready.
3. **The plant keeps B1144 stock ahead.**
   - **Compound age:** the plant uses B1144 at **6–74 h old (median 27 h)**, about a day's stock. v74 makes B1144 just in time, so 1802 starts as soon as its 4 h minimum aging ends.
   - **Mixing runs:** the plant mixed B1144 in 15 runs over 11 days, median 18 h apart. Two runs were only 6–9 h apart, which our 15 h no-repeat rule wouldn't allow.
   - **Over the limit:** 8 uses were just past the 72 h maximum (72.1–73.6 h).
4. **Neither the plant nor our plan reaches the TBR target.** At about 11,200 m a day, the plant's 1802 supplies **about 1,800 TBR tyres a day**. The TBR plan is about 3,600 a day. Even at our 13 m/min and 24 h, 1802 gives about 3,050.
   - Either the plant really builds about 1,800 TBR tyres a day, or BEF001 per tyre is less than the 6.1 m in the BOM.
   - **This is the key question for the plant.**

**Over 06–10 Oct:** the plant made 56.0 km, v74 64.6 km and v72 67.2 km. Our plan already gets more from 1802 than the plant, only because of the higher routing speed. It loses about 7 h a day waiting for B1144, which the plant avoids by mixing ahead.

---

## 2. By plant day

| Day | Plant metres | Plant running / stopped h | v74 metres | v74 running h | v72 metres | v72 running h |
|---|---|---|---|---|---|---|
| 06-Oct | 11,165 | 19.0 / 5.5 | 18,721 | 24.0 | 18,721 | 24.0 |
| 07-Oct | 12,505 | 21.3 / 3.2 | 11,708 | 15.0 | 11,708 | 15.0 |
| 08-Oct | 10,290 | 17.5 / 6.9 | 15,901 | 20.4 | 15,490 | 19.9 |
| 09-Oct | 11,143 | 19.0 / 5.4 | 7,401 | 9.5 | 7,812 | 10.0 |
| 10-Oct | 10,892 | 18.6 / 6.2 | 10,882 | 14.0 | 13,495 | 17.3 |
| **Total** | **55,995** | 95.4 / 27.2 | **64,613** | 82.9 | **67,226** | 86.2 |

The plant's other days are similar: 01–05 Oct 10,900–12,700 m a day. 02-Oct was a holiday.

---

## 3. Rules and data

| Item | Plant | v74 | Comment |
|---|---|---|---|
| Machine / item / input | 1802, BEF001, B1144 only | same | matches routing and BOM |
| Line speed (routing 13 m/min) | **about 9.8 m/min** | 13 m/min | data difference: confirm the real speed |
| B1144 kg per metre (BOM 0.249) | not measurable (no metres in the file) | 0.249 | confirm per-roll metres |
| BEF001 per TBR tyre (BOM 6.1 m) | supply ≈ 1,800 tyres a day | — | confirm with the plant's TBR builds on 06–10 Oct |
| B1144 aging 4–72 h | 774 uses OK, **8 over 72 h** (up to 73.6 h), 0 under 4 h | enforced | plant runs about a day of stock |
| B1144 repeat ≥ 15 h (our mixing rule) | 2 of 14 gaps shorter (6.4 h, 9.0 h) | followed | the plant mixes B1144 when 1802 needs it |
| Continuous running | about 3 stops a day of about 2 h (inside bookings) | idle gaps of 8–18 h | different cause: plant stops are routine, ours are compound waits |

---

## 4. What this means

1. **Our 13 m/min is optimistic.** At the plant's 9.8 m/min, 1802 can supply only about 2,300 TBR tyres a day even running 24 h. With the plant's stops it gives about 1,800.
   - If the plant confirms 9.8 m/min and 6.1 m per tyre, 1802 is the hard limit for TBR, and 3,600 tyres a day is not reachable without a second edge-filler machine or a faster line.
2. **Keeping B1144 a day ahead, as the plant does, would remove v74's 8–18 h waits**, about 7 h a day.
   - It is a scheduling change and breaks no rule: give B1144 and its chain (B186 → MB185 → MB184) priority, and make each B1144 run cover 24 h.
   - The plant's own pattern shows this is how it keeps 1802 running.
3. **Questions for the plant:**
   - the 1802 line speed and metres per roll;
   - BEF001 metres per TBR tyre;
   - the plant's TBR building output for 06–10 Oct, to check against the roughly 1,800 a day the edge filler supplied;
   - what the regular 2 h stop each shift is.
