# Algo Planning Business Rules: plan vs scheduler (v77)

## What was checked

| | |
|---|---|
| **Rules** | `Algo Planning Business Rules 1.xlsx`, sheets PCR and TBR (BRG, BRBC) and their additional notes |
| **Plan** | the input plan for 06-Oct 07:00 → 16-Oct 07:00: `latest_plan_file` building and curing files (892 building rows, 4,800 cure blocks) |
| **Scheduler** | v77's building and curing schedule (rows slid later where kits were late; spill to 16–22 Oct) |
| **Masters** | `CTP_Master_BuildingAllowable` (allowed TBMs); mould counts, descriptions (TT/TL, rim size), changeover minutes and month demand from the plan workbooks; `downtime_reason` (PM / MTC); opening green tyres from the 06-Oct snapshot |
| **Tables** | `ALGO_PLANNING_RULES_CHECK_v77.xlsx`; script `check_algo_planning_rules.py` |

Every rule is measured the same way on both sides. Day = plant day (07:00 → 07:00).

---

## 1. In simple words

**The plan itself does not follow every rule.** It breaks:
- **BRBC-02** (one SKU on one TBM): 11 PCR and 23 TBR SKUs are on 2 or more TBMs;
- **BRBC-04** (minimum curing lot): PCR 88 %, TBR 55 %;
- **BRBC-19** (mould changes per day, TBR ≤ 3): 4 of 10 days;
- **BRBC-21** (green tyre aged 2 h before curing): only 60 % PCR, 68 % TBR;
- **BRBC-30** (green-tyre WIP band): never inside the band, though never above its upper limit;
- smaller misses on BRBC-17, BRBC-37 and BRBC-06.

**The scheduler keeps every rule tied to machines, presses and quantities:**
- allowed TBMs and presses;
- SKU-to-machine assignment (same as the plan);
- tube type per TBM;
- SKU and changeover limits per day;
- curing capacity;
- mould count;
- 72 h green-tyre age.

**It does better than the plan on:**
- **BRBC-21**, 2 h green-tyre aging: PCR 81 % against 60 %, TBR 87 % against 68 %.
- **TBR mould changes per day**: 75 % against 40 %.

**It does worse than the plan where the slide changes timing:**
- **BRBC-27, mould-change time kept:** 50 % / 46 % against 100 %. This is a real gap: when cure blocks slide, the new SKU starts right after the old one, with no changeover time. The fix is coded but switched off; see section 4.
- **BRBC-04, building lots per day:** 79 % / 69 % against 100 %. A plan row that slides across 07:00 is split over two plant days.
- **BRBC-03, one campaign per SKU-day:** 86 % / 84 % against 95 % / 89 %. Kit-aware dispatch builds the next ready row when a kit is late.
- **BRBC-12 / 06, SKUs per TBM-day / shift:** 98 % / 94 % against 100 % / 97 %.
- **BRBC-19 PCR:** 93 % against 100 %.

**Can't be checked (no data):** BRBC-07 sister SKUs, BRBC-09 drum range, BRBC-28 month-opening continuity. BRG-01 has no PM / MTC window in October.

---

## 2. Rule by rule

Each cell shows followed / checked (compliance).

| Rule | Check | PCR plan | PCR v77 | TBR plan | TBR v77 |
|---|---|---|---|---|---|
| **BRG-01** PM / MTC | rows inside a PM window | no PM in Oct | no PM in Oct | no PM in Oct | no PM in Oct |
| **BRG-02** aging from SMART | data source | CTP_Master_Aging (DB) | same | same | same |
| **BRG-03** plant MPQ master | data source | CTP_Master_MPQ (DB); 0 lots below MPQ | same | same | same |
| **BRBC-28** built ≤ month demand | SKUs within demand | 36 / 37 (97 %) | 36 / 37 (97 %) | 28 / 30 (93 %) | 28 / 30 (93 %) |
| **BRBC-01** allowed TBMs | rows on an allowed TBM | 448 / 448 ✔ | 727 / 727 ✔ | 432 / 432 ✔ | 681 / 681 ✔ |
| **BRBC-02** SKU on one TBM | SKUs on one TBM | 26 / 37 (70 %) ✘ | 26 / 37 (70 %) ✘ | 7 / 30 (23 %) ✘ | 7 / 30 (23 %) ✘ |
| **BRBC-03** one campaign per SKU-day | SKU-days in one run | 193 / 203 (95 %) | 148 / 172 (86 %) | 201 / 225 (89 %) | 178 / 211 (84 %) |
| **BRBC-04** min building lot (200 / 72 per day) | SKU × TBM × day | 240 / 240 ✔ | 216 / 274 (79 %) | 253 / 253 ✔ | 222 / 320 (69 %) |
| **BRBC-04** min curing lot (300 / 150) | SKU runs on a press | 29 / 33 (88 %) | 50 / 61 (82 %) | 22 / 40 (55 %) ✘ | 37 / 57 (65 %) ✘ |
| **BRBC-06** TBR SKUs per TBM-day (TL 5 / TT 4) | TBM-days | — | — | 90 / 90 ✔ | 131 / 133 (98 %) |
| **BRBC-06** TBR ≤ 2 SKUs per TBM-shift | TBM-shifts | — | — | 263 / 270 (97 %) | 324 / 343 (94 %) |
| **BRBC-06** TBR ≤ 16 GTs per TBM | TBMs | — | — | 9 / 9 ✔ (max 12) | 9 / 9 ✔ |
| **BRBC-08** TBR TBM: TT or TL only | TBMs | — | — | 9 / 9 ✔ | 9 / 9 ✔ |
| **BRBC-12** SKUs per day (< 25 / < 28) | days | 10 / 10 ✔ (max 23) | 15 / 15 ✔ (max 22) | 10 / 10 ✔ (max 24) | 15 / 15 ✔ (max 22) |
| **BRBC-12** PCR ≤ 4 SKUs per module-day | TBM-days | 110 / 110 ✔ | 147 / 150 (98 %) | — | — |
| **BRBC-13** building changeovers per day (≤ 24 / 27) | days | 10 / 10 ✔ (max 22) | 15 / 15 ✔ (max 20) | 10 / 10 ✔ (max 27) | 15 / 15 ✔ (max 23) |
| **BRBC-16** allowed presses | cure blocks | 2,448 ✔ | 2,448 ✔ | 2,352 ✔ | 2,352 ✔ |
| **BRBC-17** TBR press one tube type | presses | — | — | 72 / 80 (90 %) | 72 / 80 (90 %) |
| **BRBC-19** mould changes per day (≤ 4 / ≤ 3) | days | 10 / 10 ✔ | 14 / 15 (93 %) | 4 / 10 (40 %) ✘ | 12 / 16 (75 %) |
| **BRBC-21** green tyre ≥ 2 h before cure | tyres | 60 % ✘ | 81 % | 68 % ✘ | 87 % |
| **BRBC-27** mould-change time kept | press SKU changes | 24 / 24 ✔ | 13 / 26 (50 %) ✘ | 28 / 28 ✔ | 13 / 28 (46 %) ✘ |
| **BRBC-30** GT WIP in band (4,500–4,800 / 1,200–1,500) | hours | 0 % (range −1,835 to 4,906) | 0 % | 0 % (−432 to 1,147) | 1 % |
| **BRBC-30** GT WIP ≤ upper limit (storage) | hours | 100 % | 100 % | 100 % | 100 % |
| **BRBC-32** cured within 72 h | tyres | 100 % (13 over) | 99.9 % (160 over) | 100 % | 99.8 % (75 over) |
| **BRBC-33** FIFO | method | assumed | oldest first (by design) | assumed | oldest first |
| **BRBC-34** rim change ≤ 1 inch | size changes | 2 / 2 ✔ | 4 / 4 ✔ | none | none |
| **BRBC-36** curing capacity (2 cavities, load / unload, 94 / 95 %) | blocks | 100 % | 100 % | 100 % | 100 % |
| **BRBC-37** presses ≤ eligible moulds | SKU-days | 235 / 235 ✔ | 302 / 302 ✔ | 224 / 231 (97 %) | 310 / 319 (97 %) |
| **BRBC-20** activate only presses needed | report | 80–83 presses / day | 9–80 (follows GT supply) | 71–80 | 2–80 |
| **BRBC-05** align building with curing | see BRBC-21 / 30 / 32 | | press idle 18,565 h (waiting for GT) | | |
| **BRBC-07 / 09 / 28** sister SKUs, drum range, month opening | — | no data | no data | no data | no data |
| **Note** puncture guard ≤ 40 / day | days | not built in window | — | — | — |
| **Note** white sidewall ≤ 2 SKUs | — | no WSW SKU in the plan | — | — | — |

**How to read some of these:**
- **BRBC-30, the WIP figures:** the lowest point is below zero because the opening snapshot holds fewer green tyres for some SKUs than the plan cures on day 1. The plan then relies on green tyres not in our opening data.
- **BRBC-32, scheduler:** green tyres older than 72 h are scrapped (157 in v77), not cured. The "over 72 h" counts above come from FIFO matching, which doesn't scrap.

---

## 3. Why the scheduler differs from the plan

| Rule | Cause |
|---|---|
| BRBC-27 mould-change time | When blocks slide, phase 6 fills the press from the previous block's end. At a SKU change the plan's changeover gap (210–430 min, including mould cleaning) is lost. The plan's changeover rows are dropped; the option to pin them (`reinsert_changeover_blocks`) pins them at their *plan* times, which no longer sit between the two SKUs after a slide. |
| BRBC-04 building lot per day, BRBC-03 one campaign | Rows that slide across 07:00 are split over two plant days. Kit-aware dispatch builds the next ready row when a kit is late. |
| BRBC-19 / BRBC-12 / BRBC-06 small misses | Slides bunch some SKU or mould changes onto the same day or shift. |
| BRBC-21 (better than the plan, not 100 %) | The scheduler releases a green tyre 90 min after building (routing GT transfer time); the rule asks for 2 h. |

---

## 4. Fixes prepared (switches, OFF in v77)

| Rule | Switch | What it does |
|---|---|---|
| BRBC-27 (and "GT not produced before mould change completion") | `curing_keep_changeover: true` (`phase6_curing_revision.py`) | In phase 6's free-slot search, every placed block of a different SKU is widened by the plan's changeover minutes for that press and SKU (fallback `curing_changeover_min`, 300). The mould change moves with the sequence. |
| BRBC-21 | `gt_transfer_time_min: 120` | A green tyre is curable 2 h after it is built (was 90 min). |

Both will cost a little output: presses wait for the mould change, green tyres wait 30 min longer.

**Not prepared** (need rules or data the scheduler doesn't have):
- per-day building lot minimums and per-shift SKU caps after a slide;
- sister-SKU / drum-range / month-opening rules;
- GT trolley and storage by inch.
