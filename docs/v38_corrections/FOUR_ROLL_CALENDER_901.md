# The 4-roll calender (machine 901): what was wrong, what we changed, what it did

This note tracks one issue from the v38 audit: the 4-roll calender, machine **901**. It records:
* what the problem was;
* every change tried, why, and how it works in the logic;
* where the code is;
* the effect in numbers.

All runs use the same input data (`data-ppds-v38`) and the same October plan, so differences come from the code only. "Cured" means tyres cured inside the horizon (22 Oct 07:00 → 1 Nov 07:00), out of **161,670 eligible tyres** (the 8,808 data-blocked tyres are excluded; see `CHANGE_LOG.md`).

---

## 1. The issue as raised on v38

> The 4-roll calender is run as a changeover machine. 166 lots, 165 item changes, lots of 0.1–2 h alternating PCR fabric and TBR steel; about 131 h of changeover gaps against 123 h of production. It is the last-finishing component for 79% of PCR kits and 52% of TBR kits, the single biggest cause of building starvation.

### What v38 actually shows (measured from `outputs/e2e_v38`)

| point | claim | measured on v38 |
|---|---|---|
| lots / item changes | 166 / 165 | **166 / 165** ✔ |
| short lots (≤ 2 h) | most | **154** (median 0.62 h) ✔ |
| alternating PCR / TBR | yes | **71** PCR↔TBR switches ✔ |
| gaps vs production | 131 h vs 123 h | **131 h** of gaps vs **139 h** of production (gaps = changeover + idle) ✔ |
| last-finishing input, PCR kits | 79% | **83%** ✔ |
| last-finishing input, TBR kits | 52% | **28%**. For TBR the biggest blocker is bead machine **2403**, not 901 |

The PCR part of the claim holds. The TBR share was overstated.

### Where 901's line speed comes from
* The engine uses the **routing only**: `JK_CTP_PPDS.CTP_Master_Routing`, columns `proc_time` + `proc_time_UOM` (metres per minute), staged as `data-ppds-v38/jkt_routing_{pcr,tbr}.csv`.
* Lot time = metres ÷ speed (`engine/ctpio.py`, `op_duration_min`, the `M/MIN` branch).
* Decision (user): **keep using the routing speed only.**
* The plant's 4-roll calender master `CTP_Master_FRC` is not used by the engine. Cross-check: of the 28 items the routing puts on 901, 19 are in that master. The routing is never faster than it; 5 TBR items are slightly slower; 9 items have no master row.

---

## 2. What we changed, step by step

Each step was a separate run. A step is kept only if it passes every schedule check (G1–G8, including "builders and presses keep the plan's machine and order") and **neither PCR nor TBR loses tyres**.

### Step 1: v43a, calender campaigns (dispatch rule)

**Why.** 901 always took the most urgent lot next, whatever it was, so it switched item and line almost every lot.

**How (logic).** For machines listed in `campaign.machines` (here only 901):
1. **Hold.** Keep running the item already on the machine while it has another lot ready, unless some other lot is more than `campaign.window_h` (8 h) more urgent.
2. **Choose the next item.** Look only at lots within 8 h of the most urgent one. Prefer:
   * the **same line** as the last lot (PCR stays PCR, TBR stays TBR);
   * then the **cheapest changeover** from the item on the machine (PPDS changeover matrix `jkt_changeover_matrix_combined`);
   * then urgency.

**Code.** `engine/phase5_placement.py`: settings `CAMP_M`, `CAMP_WIN` and the ranking-only lookup `co_peek()` (next to `co_charged()`); the hold-break test and the next-item choice in the dispatch loop (counters `v43-campaign-break` and `v43-campaign-pick` in the run log). Rules: `config/rules_v43a.yaml`.

**Effect.** PCR↔TBR switches fell from 66 to 16, and changeover from 101 h to 82 h. But TBR lost 3,726 tyres. TBR lots on 901 now waited for the PCR block to finish, and TBR kits were already queuing on the overloaded bead machine 2403. **Not kept on its own.**

### Step 2: v43b (= **v43, the current baseline**), campaigns + TBR bead balance

**Why.** The TBR loss in step 1 came from bead machine 2403 (258 h of work against 120 h on 2405), even though most bead items are allowed on 2403, 2404 and 2405.

**How.** For machines listed in `balance.machines` (2403, 2404, 2405): when an item would push its machine past `balance.above_pct` (50%) of the horizon and another allowed machine is lighter, its lots are spread over the machines its **routing allows**. Mixers are not affected.

**Code.** `engine/phase4b_assignment.py` (`BAL_M`, `BAL_PCT`; prints "Pool balance"). Rules: `config/rules_v43b.yaml`.

**Effect.** Both lines gain (see table). **Kept: this is v43.**

### Step 3: v43c, bigger lots on 901 (lot sizing)

**Why.** 901 makes only **19 different items**, but phase 3 cut each into about 8 small lots. Buckets broke on every 8 h gap between needs, and were cut again into 24 h delivery lots. Two lots of the same item were almost never ready at the same time, so the campaign hold in step 1 had nothing to hold on to (item changes only fell from 151 to 144).

**How.** With `campaign.bigger_lots: true`, an item made on a campaign machine is bucketed over its **whole shelf-life window** (max ageing − min ageing − 2 h safety, about 62–66 h). There is no gap break and no 24 h cut. MPQ minimum and maximum, the 8 h run-length cap (A03) and whole-unit rounding still apply. Because the window is the shelf life, nothing is used after it expires.

**Code.** `engine/phase3_lot_sizing.py`, `size_item()` (`_camp_lot`; switches `campaign.bigger_lots`, `campaign.lot_window_h`; prints "Campaign lots"). Rules: `config/rules_v43c.yaml`.

**Effect.** The calender pattern is largely fixed: 60 lots, 51 item changes, changeover 36 h, gaps 42 h. But **TBR lost 5,110 tyres**. One TBR item, **CP56542**, held up 115 TBR kits. **Not kept.**

### Step 4: v43d, bigger buckets but keep the 24 h delivery lots

**Why.** Try to keep step 3's grouping without one big lot finishing late.

**How.** `campaign.keep_need_span: true`: the bucket covers the shelf-life window, but it is still cut into 24 h delivery lots.

**Code.** Same function in `phase3_lot_sizing.py` (`CAMP_KEEP_SPAN`). Rules: `config/rules_v43d.yaml`.

**Effect.** The calender pattern went back to almost v43 levels (147 lots, 138 changes). PCR gained 6,810 tyres and TBR lost 138. The gain does **not** come from fewer 901 changeovers; 901 lots per item barely changed. It is a side effect through the shared PCR/TBR placement run, so it cannot be credited to the 901 fix. **Not kept** (TBR −138, cause not understood).

### Step 5: finding the real cause of the 2.5–4.5-day wait

**Question.** 901 lots start 60–113 h after their compound is ready. Is a release rule holding them back?

**What the data shows** (v43, each 901 lot classified by what it waited for):
* **132 of 152** lots start straight after the previous 901 lot plus its changeover, so **901 is busy back to back**;
* 0 lots start because of their earliest-start limit, and 0 because their compound only just arrived.

In 240 h, 901 **runs 133 h, changes over 82 h and is idle only about 24 h**. It is fully booked, and lots wait in a **queue**.

**Correction to an earlier statement.** I said 901 was idle about 104 h. That was wrong: most of the "gaps" are changeover time. So the lever is **less changeover time**, which is exactly what bigger lots give.

### Step 6: v43e, progressive release (how a calender delivers)

**Why.** In step 3, the engine treated a lot's material as ready only when the **whole lot** finished. A calender produces rolls continuously, so the first trolley in line can take its share long before the lot ends.

**How.** With `campaign.progressive_release: true`, a lot on a campaign machine releases each trolley's share at:

> lot start + lot duration × (quantity needed up to and including this trolley ÷ lot quantity)

Trolleys are taken in need-by order. A share is never ready later than the lot's finish or earlier than its start. This is applied in both kit checks: the main kit gate and the re-check after the bead step. Mixers and other batch machines still release a lot only when it finishes.

**Code.** `engine/phase5_placement.py`: allocations now read `qty` and `need_by`; new function `_progressive()` (after `fin = ...` in the kit-gate section); both kit-gate loops use it; prints "Progressive rel". Rules: `config/rules_v43e.yaml` (on v43), `config/rules_v43f.yaml` (on v43c).

**Effect.**
* On v43 (v43e): +64 tyres, almost no change, because v43's lots are small anyway.
* On the bigger lots (v43f): PCR +2,856 against v43, but TBR still 13,770.

So progressive release is a correct model and is kept in code behind its switch, **but it does not remove the TBR loss**.

### Step 7: why big lots still hurt TBR

Looking at the first lots on 901 in v43f:
* A big lot **cannot start until all of its compound is mixed**. In v43f, 901 sits idle until 12:12 on day 1 (in v43 it starts at 07:00).
* The first big lots use 901 time to make material that is needed days later. TBR's CP56542 got its first lot on **27 Oct** in v43f, against **22 Oct** in v43.

**Conclusion.** The lot size has to sit **between** v43's tiny lots and the 66 h window.

### Step 8: v43g24 / v43g36, middle-size buckets (**v43g24 adopted**)

**How.** As v43f (bigger lots + progressive release), but the 901 bucket is capped through `campaign.lot_window_h`:
* **24 h** in `config/rules_v43g24.yaml`;
* **36 h** in `config/rules_v43g36.yaml`.

Inside that window the bucket does not break on need gaps and is not cut again. Each trolley takes its share as the calender produces it.

**Effect.**

* **24 h buckets (v43g24): both lines gain.**
  * PCR 93,968 (+5,840 against v43), TBR 19,158 (+348). **Total 113,126 (70.0%)**, +6,188 against v43.
  * 901: 130 lots (from 152), item changes 124 (from 144), line switches 11 (from 16), changeover 73 h (from 82), all gaps 96 h (from 106).
  * TBR kits whose last input is 901: **22% → 8%**.
  * Every check passes. **Adopted as the new baseline.**
* **36 h buckets (v43g36): rejected.** Total 116,536 and a bigger calender gain (92 lots, 85 changes, changeover 49 h, PCR kits waiting last on 901 down to 43%), but TBR loses 2,672 tyres. That fails the "no line loses tyres" rule.

**Shelf life (reported, not a stop rule).** Material used after its maximum age, all edges:

| run | all violations | on 901 items | median overrun on 901 items |
|---|---|---|---|
| v43 | 385 | 32 | 42.9 h |
| v43g24 | **350** | **64** | 25.6 h |
| v43g36 | 316 | 49 | 49.9 h |

v43g24 has fewer violations overall, but more on 901 items (64 vs 32, with smaller overruns). These are 901 rolls used after their maximum age because the trolley that needs them is late. It's worth a plant check of 901 maximum ageing.

---

## 3. Effect in numbers

| run | what it adds | 901 lots / item changes | lots ≤ 2 h | PCR↔TBR switches | changeover / all gaps vs production | 901 last input PCR / TBR | cured PCR | cured TBR | **cured total** | kept? |
|---|---|---|---|---|---|---|---|---|---|---|
| v38 | — (audit baseline) | 166 / 165 | 154 | 71 | 100 / 131 h vs 139 h | 83% / 28% | — | — | 101,210 of 170,478 (blocked demand counted) | — |
| v42f | builders keep plan order (base for v43) | 152 / 151 | 143 | 66 | 101 / 128 h vs 133 h | 75% / 24% | 85,050 | 17,944 | 102,994 (63.7%) | base |
| v43a | 901 campaigns | 152 / 144 | 143 | 16 | 82 / — vs 133 h | — | 88,128 | 14,218 | 102,346 | no (TBR −3,726) |
| **v43 (v43b)** | **+ TBR bead balance** | **152 / 144** | **143** | **16** | **82 / 106 h vs 133 h** | **64% / 22%** | **88,128** | **18,810** | **106,938 (66.1%)** | **yes (baseline)** |
| v43c | + bigger 901 lots (66 h) | 60 / 51 | 39 | 22 | 36 / 42 h vs 125 h | 42% / 45% | 88,496 | 13,700 | 102,196 | no (TBR −5,110) |
| v43d | bigger buckets, 24 h lots kept | 147 / 138 | 138 | 19 | 81 / 103 h vs 132 h | 71% / 21% | 94,938 | 18,672 | 113,610 | no (TBR −138, gain not from 901) |
| v43e | v43 + progressive release | 152 / 144 | 143 | 16 | 82 / 106 h vs 133 h | 62% / 22% | 88,192 | 18,810 | 107,002 | switch kept, effect ≈ 0 |
| v43f | v43c + progressive release | 60 / 51 | 39 | 22 | 36 / 42 h vs 125 h | 41% / 43% | 90,984 | 13,770 | 104,754 | no (TBR −5,040) |
| **v43g24** | **24 h buckets + progressive release** | **130 / 124** | **111** | **11** | **73 / 96 h vs 131 h** | **66% / 8%** | **93,968** | **19,158** | **113,126 (70.0%)** | **yes, new baseline** |
| v43g36 | 36 h buckets + progressive release | 92 / 85 | 65 | 11 | 49 / 62 h vs 128 h | 43% / 2% | 100,398 | 16,138 | 116,536 | no (TBR −2,672) |

All runs pass checks G1–G8. In every run, builders and presses keep the plan's machine and order.

---

## 4. Where it stands now (baseline **v43g24**)

| v38 point | v38 | v43g24 | status |
|---|---|---|---|
| alternating PCR fabric / TBR steel | 71 switches | **11** | **fixed** |
| 165 item changes | 165 | **124** | partly (−25%) |
| lots of 0.1–2 h | 154 | **111** | partly (−28%) |
| changeover time | 100 h | **73 h** | partly (−27%) |
| gaps vs production | 131 h vs 139 h | **96 h vs 131 h** | partly |
| 901 last input, PCR kits | 83% | **66%** | partly; still the #1 PCR blocker |
| 901 last input, TBR kits | 28% | **8%** | **fixed** for TBR |
| why lots wait 2.5–4.5 days | — | — | **explained**: 901 is fully booked (a queue), mostly by changeovers |
| cured tyres (of 161,670 eligible) | — | **113,126 (70.0%)** | +6,188 against v43, +10,132 against v42f |

**What is left.**
* Item changes are still high (124), because 24 h buckets still give about 7 lots per item.
* Bigger buckets (36 h) cut changeover further (49 h), but TBR loses tyres. The limit is that a big lot cannot start until all its compound is mixed. The next lever is therefore **upstream**: mix the compound for 901 items earlier, or in step with the 901 bucket. That belongs with v44 (upstream priority follows the plan order).

## 5. Settings (all off unless a rules file switches them on)

| setting | file that reads it | meaning |
|---|---|---|
| `campaign.machines` | `phase5_placement.py`, `phase3_lot_sizing.py` | machines that run campaigns (901) |
| `campaign.window_h` | `phase5_placement.py` | how much more urgent another lot must be to break a campaign (8 h) |
| `balance.machines`, `balance.above_pct` | `phase4b_assignment.py` | spread an item over its allowed machines once its machine passes this share (2403/2404/2405, 50%) |
| `campaign.bigger_lots` | `phase3_lot_sizing.py` | bucket campaign items over their shelf-life window |
| `campaign.lot_window_h` | `phase3_lot_sizing.py` | cap on that bucket (0 = shelf life only) |
| `campaign.keep_need_span` | `phase3_lot_sizing.py` | keep the 24 h delivery cut inside the bigger bucket |
| `campaign.progressive_release` | `phase5_placement.py` | a trolley's share of a campaign lot is ready as it is produced |

## 6. Needed from the plant

| item | why |
|---|---|
| Routing for **CAP 66** (the changeover matrix shows it runs on 901) | today the cap-strip load on 901 is not visible |
| 4RC master rows for CPJ1218, ECOPOLY350, CPJ120, MONOCHAFER, the three cap-strip mother rolls, CP 200 and NYLRAD | so the routing speeds can be cross-checked (the engine keeps using the routing) |
| Confirmation of 901 minimum / maximum run lengths per item | bigger lots should respect the plant's real limits, not only shelf life |
| Which bead items can also run on 2404/2405 (routing rows for 2403-only items) | 2403 is still at 95% after v43 |
