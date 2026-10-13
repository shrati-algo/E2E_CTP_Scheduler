# TBR bead (TDA 2403 / 2404 / 2405): plant vs v72, rules and cycle times

## Sources

| | Source |
|---|---|
| **Plant** | `tda_schedule.csv`, formatted in `TDA_SCHEDULE_PLANT_formatted_2403-2405.xlsx`. One plant day, 08-Oct 07:00 → 09-Oct 07:00. TDA1 / TDA2 / TDA3 = 2403 / 2404 / 2405 |
| **Scheduler** | v72, the latest best version. Its lots on 2403–2405 come from the *Upstream schedule* sheet |
| **Masters** | Routing and BOM from `BOM_Routing_9oct`; changeover matrix ("TBR Bead Apexing TDA 1/2/3", "TBR Bead Winding"); aging master |
| **Bead inch** | `tbr_bead_inch.csv`. Read-only DB: `CTP_Master_BuildingAllowable.size` of the TBR SKUs that use each bead. All 7 beads the plant ran have one clear inch: 001 / 003 / 005 / 011 are 20″, 006 / 008 / 009 are 22.5″ |
| **Tables** | `PLANT_vs_v72_TDA_COMPARISON.xlsx`. Script: `compare_plant_tda_v72.py` |

**How plant beads are counted.** The file has no output quantity. Beads per booking = bead wire, A5542 and SNCT001 used ÷ the BOM quantity per bead. A normal booking is about 33 beads in 12–16 min.

**Which v72 days are compared.** v72's 08-Oct is a start-up day, so the v72 average for 09–15 Oct is shown as well.

---

## 1. In simple words

**1. The plant winds and apexes a bead in one step. v72 does it in two.**
- **Plant:** every booking uses the bead wire + S54 (winding) and A1031 / A5542 / B107 / SNCT001 (apexing) together.
- **v72:** first makes the wound bead (TBWB) as its own lot on the same machine, then apexes it in a second lot. Winding takes about **24 of v72's 51 busy hours a day** on these machines.
- **Time per bead:** the plant spends **0.41–0.64 min**. v72 spends **0.58–0.67 min** (winding + apex at routing rates, efficiency 1). So the plant needs about **61–107 %** of v72's time for the same bead.

**2. The plant makes more beads.**

| | Plant 08-Oct | v72 08-Oct | v72 average 09–15 Oct |
|---|---|---|---|
| Apexed beads | **6,458** (about 3,230 TBR tyres) | 2,249 | 4,766 (about 2,380 tyres) |
| Machine hours used (of 72) | 72 (about 23 h of it stops inside long bookings) | 31 | 51 |
| of which winding as a separate step | 0 | 18.8 h | 24.4 h |
| Apex runs / item switches | 14 / 11 | 4 / 6 | 7 / 9 |

- **Per machine:** v72 runs each bead machine only about **16–17 h a day**, about 8 h winding and 8 h apexing.
- **What the plant does differently:** it runs the machines all day, and it doesn't spend separate time on winding.

**3. The plant keeps one inch per machine. v72 does not.**
- **Plant:** 2404 ran only 22.5″ beads; 2403 and 2405 ran only 20″ beads (BR-63).
- **v72:** mixes 20″ and 22.5″ on the same machine on **17 of 44 machine-days**. v72 has no TBR inch rule: it was left out for lack of inch data, which we now have.

**4. Changeovers.**
- **Plant:** books one roll after another, so the changeover can't be seen as a gap. The extra time inside the first booking after an item change is mostly **0–6 min**, twice 24–31 min. The matrix says 20 min (die and preformer change).
- **v72:** keeps the matrix time for apexing. For winding inch changes it uses **15 min instead of the matrix 20 min** on 7 switches. The 2403–2405 machines point to the *apexing* matrix, which has no winding rows.

**5. Grouping (one run per item per day, BR-65).**
- **Plant:** splits items: 5 of 9 machine-item pairs in one run. For example, 2404 ran TBAPBD008 in 3 blocks.
- **v72:** **89 %** in one run (87 of 98).

**6. Compound age.**
- **Plant:** used **B107 past its 72 h maximum 19 times** (up to 79 h) and **S54 past 120 h 36 times** (up to 130 h). 97 compound rows have no batch ID.
- **v72:** all compound use within aging (mixing audit: 0 violations).

---

## 2. Rule by rule

✔ = followed, ✘ = broken, ? = cannot be seen in the data.

| Rule | What it means | Plant 08-Oct | v72 08-Oct | v72 06–19 Oct |
|---|---|---|---|---|
| **BR-58 / 59** approved machine and rate | item only on a routing machine; time from the routing rate | ✔ 9 / 9 | ✔ 8 / 8 | ✔ 161 / 161 |
| **BR-60** matrix changeover (die / preformer 20 min) | gap at an item change ≥ matrix | ? no gaps between bookings; extra time 0–6 min mostly | see BR-64 row | 42 / 49 (86 %) |
| **BR-63** inch per machine | no 20″ / 22.5″ cross-assignment | ✔ 3 / 3: 2404 = 22.5″, 2403 / 2405 = 20″ | ✔ 3 / 3 | ✘ 27 / 44 (61 %) |
| **BR-64** 20″ ↔ 22.5″ changeover 20 min | inch change gets 20 min | ✔ no inch change made | ✘ 2 winding inch changes at 15 min | ✘ 7 winding inch changes at 15 min |
| **BR-65** group by inch / tooling, one run per item | fewer die changes | ✘ 5 / 9 (56 %) | 2 / 3 | 87 / 98 (89 %) |
| Winding + apexing | how the bead is made | one step, one booking | two lots | two lots (24 h a day winding) |
| Compound aging | B107 / S54 / A1031 / A5542 within the aging master | ✘ B107 19, S54 36 uses over the maximum | ✔ | ✔ |
| Bead shelf life (TBAPBD 24 h) | bead used within 24 h | ? no building data | ✔ window | ✔ window |

**Plant changeovers** (sheet *Plant changeovers*): 11 item changes.
- Matrix pairs **missing** for 003 ↔ 005 and 008 ↔ 009 (on all three TDA matrices). TDA 1 (2403) has no 22.5″ items at all. TDA 2 / 3 have 006 ↔ 008 / 009 at 20 min and 20″ ↔ 22.5″ at 60 min (size drum change and compound push-out).
- v72 falls back to its default changeover there.

---

## 3. Cycle time per bead

**Plant figures** = beads ÷ minutes in normal bookings (≤ 45 min), winding included.
**v72 figures** = routing at efficiency 1 (as the scheduler): apexing 3 beads/min; winding 3.75–4 (20″) or 3 (22.5″) beads/min.

| Machine | Item | Inch | Plant beads/min | Plant min per bead | v72 min per bead (winding + apex) | Plant as % of v72 |
|---|---|---|---|---|---|---|
| 2403 | TBAPBD003 | 20″ | 2.35 | 0.43 | 0.58 | 74 % |
| 2403 | TBAPBD001 | 20″ | 2.00 | 0.50 | 0.60 | 83 % |
| 2403 | TBAPBD005 | 20″ | 2.36 | 0.42 | 0.58 | 72 % |
| 2404 | TBAPBD008 | 22.5″ | 2.44 | 0.41 | 0.67 | 61 % |
| 2404 | TBAPBD006 | 22.5″ | 1.72 | 0.58 | 0.67 | 87 % |
| 2404 | TBAPBD009 | 22.5″ | 1.56 | 0.64 | 0.67 | 96 % |
| 2405 | TBAPBD003 | 20″ | 1.62 | 0.62 | 0.58 | 107 % |
| 2405 | TBAPBD001 | 20″ | 1.87 | 0.53 | 0.60 | 88 % |
| 2405 | TBAPBD011 | 20″ | 1.66 | 0.60 | 0.58 | 103 % |

- **Compared with v72's apexing step alone,** the plant is 23–92 % slower (about 2.0 vs 3.0 beads/min). That is expected, because the plant's booking also includes the winding.
- **Compared with what v72 really books** (winding + apex), the plant is faster for every item except TBAPBD003 / 011 on 2405.
- **2405 is the plant's slowest machine.** It made 1,284 beads, against 2,826 on 2403. It had 7 long bookings (stops) and the slowest normal bookings.

---

## 4. What this means and what to fix

1. **Winding as a separate step (biggest effect).**
   - The plant shows that winding happens inside the apexing booking on the TDA. v72's separate TBWB lots came from an approximation (`apply_ctb_approximations.py`: "no TBR bead-winding machine in any master … winding happens on the TDA").
   - **Fix:** confirm with the plant, then treat winding + apex as one operation on 2403–2405 at the plant's rate (about 2.0–2.4 beads/min).
   - **Effect:** frees about 24 machine-hours a day on the TBR bottleneck. At the plant's rate the three machines can make about 6,500–8,000 beads a day, enough for 3,250–4,000 TBR tyres (target 3,500).
2. **Inch per machine (BR-63 / 65).** Use the inch table (`tbr_bead_inch.csv`): 22.5″ on 2404, 20″ on 2403 / 2405, like the plant.
   - Only TBAPBD012 and TBWB001 are mixed-inch. TBAPBD001 uses TBWB001 but itself goes only into 20″ SKUs.
   - Fewer inch changes in v72 (17 machine-days mixed now).
3. **Winding changeover (BR-64).** Look winding switches up in the "TBR Bead Winding" matrix (20 min) instead of the apexing matrix. Only needed while winding stays a separate step.
4. **Plant data to confirm:**
   - the missing matrix pairs (003 ↔ 005, 008 ↔ 009);
   - the real changeover (the plant's data suggests well under 20 min);
   - why 2405 ran slow and stopped for about 13 h;
   - B107 / S54 used past their maximum age.

None of this has been implemented. Points 1–3 change the routing / scheduling of 2403–2405 and need your go-ahead.
