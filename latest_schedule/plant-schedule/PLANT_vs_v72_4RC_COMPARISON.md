# 4-roll calender (4RC, machine 901): plant vs v72, rule by rule

## Sources

| | Source |
|---|---|
| **Plant** | `four_roll_schedule.csv`, formatted in `FOUR_ROLL_SCHEDULE_PLANT_formatted.xlsx`. One plant day, 08-Oct 06:58 → 09-Oct 06:57: 87 rolls, 8 items, 8 runs |
| **Scheduler** | v72, the latest best version and current config (`latest_schedule/v72/FOURRC_RULES_v72.xlsx`, sheet *901 lots*) |
| **Masters** | v72 inputs: routing and BOM from `BOM_Routing_9oct`; `CTP_Master_FRC` spool / dip lengths; aging master; 4RC changeover matrix |
| **Tables** | `PLANT_vs_v72_4RC_COMPARISON.xlsx`. Script: `compare_plant_4rc_v72.py` |

### How plant metres are found

The plant file gives rolls and consumed quantities, not metres. The metres are worked out from the BOM:

> metres = kg consumed ÷ BOM kg per metre (`child_quantity ÷ Parent_qty`)

This is done twice, once from the compound and once from the cord / fabric. For 5 of the 8 items the two results agree within 1 %. For CP56542, BRTTL and ECOPOLY350 they differ by 6–17 % (see section 4).

### Which v72 days are compared

v72's 08-Oct is a start-up day: the 4RC runs only 89 min. So the comparison also shows:
- **09-Oct**, a full day in v72;
- the **v72 average for 09–15 Oct**.

---

## 1. In simple words

**The plant follows the sequencing rules we follow.** On 08-Oct the plant:
- made each item in one go;
- kept the same cord together (CPJ1218 → CPJ370 on the same wire);
- ran fabric between the two cord changes;
- left ≥ 4 h between cord changes (5.5 h and 4.3 h);
- used compound inside its aging window.

**The plant breaks three of our rules.** Two of these may be data issues rather than real breaks; the plant needs to confirm all three:
1. **Line speed.** No plant run matches our routing speed within ±5 %.
   - CPJ1218, CPJ370, ECOPOLY350 and HTPOLY350 ran 1.3–2× faster than routing.
   - CP56542, BRTTL, CAP 66 and CWBP60 took 1.4–2.5× longer. This could be slower running, or stops counted inside the roll times.
2. **Spools / dips not used up.** The CPJ1218 + CPJ370 creel ended at 2.34 spools, not a whole number: about 4 km of cord left. ECOPOLY350 ran 5.5–5.9 dips.
3. **CAP 66 uses CAP STRIP 120.** That input is not in our BOM. Our rule 4RC-2 says CAP 66 is made from fabric + B616M only.

**The plant got more out of the 4RC than v72 does on an average day.**

| | Plant 08-Oct | v72 08-Oct | v72 09-Oct | v72 average 09–15 Oct |
|---|---|---|---|---|
| Metres calendered | **55.6 km** (35.9 steel + 19.7 fabric) | 3.2 km | 52.2 km | 37.0 km |
| 4RC busy | 24 h | 1.5 h | 24 h | 18 h (75 %) |
| Items / runs | 8 / 8 | 2 / 2 | 6 / 6 | 6 / 6 |
| Steel cord changes | 2 | 0 | 2 | 1.4 |

- **Plant vs v72's best day:** about the same metres as v72's best day (09-Oct).
- **Plant vs v72's average:** about 50 % more than v72's average.
- **Why v72's 4RC is short of work:** the compounds it needs (B186, B460, B350) come late from mixing, and the day plan only fills the days when demand is due. Mixing is the bottleneck in v72, as reported before.
- **Speed is part of it too.** At our routing speeds, the plant's 55.6 km would need 1,492 min (104 % of a day), so the plant runs some items faster than our routing.

**v72 itself follows every 4RC rule** over the full horizon (official v72 checker), except **rule 3: 21 of 23**. The 2 cord changes fall on days with no fabric to put between them.

---

## 2. Rule by rule

✔ = followed, ✘ = broken, ? = cannot be judged from the data.

| Rule | What it means | Plant 08-Oct | v72 (full horizon, official checker) |
|---|---|---|---|
| **4RC-2** CAP 66 bypass slitted | CAP 66 made from fabric + B616M only | ✘ also consumes **CAP STRIP 120** (4,415 units) | ✔ 0 slitted lots |
| **4RC-3** capacity | ≤ 24 h of running per day | ✔ 1,438 min | ✔ 14 / 14 days |
| **4RC-4** routing line speed | run time = metres ÷ routing speed (efficiency 1, as the scheduler) | ✘ 0 / 8 within ±5 % (50–245 % of routing time) | ✔ 116 / 116 lots |
| **4RC-5** one creel | ≥ 240 min from old cord to new steel | ✔ 2 / 2 (331 and 258 min) | ✔ 25 / 25 |
| **4RC-6** roll lengths | roll ≤ one spool / dip | ✔ 8 / 8 (plant rolls about 640 m) | ✔ 116 / 116 |
| **4RC-7** one go per day | each item in one run per day | ✔ 8 / 8 | ✔ 73 / 73 |
| **4RC-8** spool / dip used up | wire / dip changed only when used up | ✘ 1 / 5 judged (see below) | ✔ 64 / 64 |
| **4RC-9** rule 3 | fabric between steel cord changes | ✔ 2 / 2 | 21 / 23 (91 %; 2 on days with no fabric) |
| **v42** wire campaigns | same wire / dip together | ✔ 7 / 7 loads (CPJ1218 + CPJ370 on wire 1036S34500 back to back) | ✔ 65 / 65 |
| **4RC-9** least changeover | changeover time between items | ? the plant books rolls back to back, so no changeover time is visible | ✔ 71 / 71 (matrix times used) |
| **4RC-10** no idle inside a load | no stop in the middle of a spool / dip | ✔ 1 / 1 | ✔ 65 / 65 (one 5 h wait at a dip boundary, BRTTL) |
| Compound aging | compound used between min and max aging | ✔ all 8 compounds (7–66 h; limits 4–72 h, B616M 120 h) | ✔ (mixing audit) |

**4RC-8 details (plant).** The first and last loads of the day are not judged, because they may start the day before or carry on the day after.

| Load | Spools / dips used (master length) | Verdict |
|---|---|---|
| BRTTL | 0.86 by compound, 1.05 by fabric (dip 2,000 m) | probably whole — the measures disagree |
| CAP 66 | 1.01 (dip 3,000 m) | ✔ |
| CPJ1218 + CPJ370 | 2.34 (spool 12,600 m) | ✘ about 4.3 km of cord taken off the creel, or the spool length in `CTP_Master_FRC` is not what the plant uses |
| ECOPOLY350 | 5.5–5.9 (dip 2,000 m) | ✘ / borderline |
| HTPOLY350 | 1.94 (dip 2,000 m) | borderline (≈ 2) |

**v72 on its own days.**
- **09-Oct:** 6 runs; the same day checks all pass (one go, creel 240 / 291 min with fabric between, campaigns, speed, rolls, whole spools / dips).
- **08-Oct:** only MONOCHAFER and HIRAD; nothing to break.

---

## 3. The plant's day vs v72

| Shift | Plant 08-Oct | v72 08-Oct | v72 09-Oct |
|---|---|---|---|
| A (07–15) | CP56542 (steel) → BRTTL → CAP 66 | MONOCHAFER, then idle | CWBHTCP100 (steel) → CAP 66 |
| B (15–23) | CPJ1218 → CPJ370 (steel, same wire) → ECOPOLY350 | idle | CPJ1218 (2 spools, 25.2 km) |
| C (23–07) | ECOPOLY350 → HTPOLY350 → CWBP60 (steel) | idle → HIRAD at 05:19 | CPJ1218 → ECOPOLY350 → ECOPOLY-EFA → CWCH30256 (steel) |

**Same pattern in both:** steel → fabric → steel → fabric → steel, with the creel changed while fabric runs, and the same wire run together.

**Differences:**
- **Lot size.** The plant makes smaller amounts of more items per day: 8 items, with steel runs of 2.3–22 km. v72 runs whole spools: CPJ1218 = 2 × 12.6 km, CWBHTCP100 = 7 km.
- **Idle time.** v72 sits idle on 08-Oct.
  - The compounds for its next items finish late: B460 at 18:58, B350 at 20:00.
  - The B186 lot (finished 08:20) feeds CWBHTCP100, which the day plan put on 09-Oct.
  - The plant had compound in stock. v72 starts with no compound stock (opening stock is not in the data), so its first days depend on fresh mixing.

---

## 4. Data checks (plant vs our masters)

| Item | Finding | What to confirm |
|---|---|---|
| CAP 66 | Plant code "CAP 66" = our "CAP 66-MOTHERROLL". The plant also consumes **CAP STRIP 120** | Is CAP STRIP 120 an input of CAP 66 (BOM missing a line), or reworked slitted material (then 4RC-2 is broken)? |
| CPJ1218 / CPJ370 / ECOPOLY350 / HTPOLY350 | Ran at 54–71 m/min vs routing 35–42 m/min | Are routing speeds too low? Raising them would add 4RC capacity in our plan |
| CP56542 / BRTTL / CAP 66 / CWBP60 | Ran at 13–20 m/min vs routing 27–45 m/min | Real slow running, or stops / waiting inside the roll booking times? |
| CP56542 (−13 %), BRTTL (−17 %), ECOPOLY350 (−6 %) | Metres from compound ≠ metres from cord / fabric, so the compound : cord ratio differs from our BOM | BOM quantities for these items |
| CPJ1218 + CPJ370 spool, ECOPOLY350 dip | Not whole spools / dips by our `CTP_Master_FRC` lengths (12,600 m / 2,000 m) | Real spool / dip lengths, and whether leftover cord is taken off |
| Compound age | All within the aging master: B333 40–66 h, B370 26–64 h, B96 7–56 h, others 12–48 h | — |

---

## 5. What this means and what to do

1. **Rules.** Our 4RC sequencing rules match what the plant does: one go, one creel, fabric between cord changes, wire campaigns, aging. Nothing to change there.
2. **Plant confirmations** (data only, no code change):
   - CAP STRIP 120 in CAP 66;
   - 4RC routing speeds item by item;
   - spool / dip lengths;
   - the BOM ratio for CP56542 / BRTTL / ECOPOLY350.

   If the faster speeds are confirmed, `CTP_Master_Routing` should be updated. That gives the 4RC more capacity in our plan.
3. **Getting v72 to the plant's 55 km a day.** The 4RC is not short of capacity in v72; it is short of compound and demand early in the horizon.
   - **Opening stock** of compound and calendered rolls at 06-Oct 07:00 would remove the start-up idle (06–08 Oct).
   - **More final-mixer output** keeps the 4RC fed after that. This is the mixing bottleneck already reported.
