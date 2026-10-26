# TBR apexed bead: what was wrong, what we changed, what it did, what is left

This note tracks one issue on its own: the **TBR bead section** (bead winding + bead apexing on machines 2403 / 2404 / 2405) and how it caused TBR's cold start. It records every change made, why, how the logic works, where the code is, the effect in numbers, and the issues that remain.

All runs use the same data (`data-ppds-v38`) and the same October plan, so differences come from the code only. "Cured" = tyres cured inside the horizon (22 Oct 07:00 → 1 Nov 07:00), out of the eligible demand (data-blocked SKUs excluded). Builders and presses keep the plan's machine and order in every run listed from v42f on (check G8).

---

## 1. The issue

**From the v38 audit:**
* "TBR bead pool is unbalanced: TDA-1 (2403) at 94% while TDA-2/3 sit at 63% / 45%; 102 TBR kits are gated on apexed beads."
* "Cold start: TBR building hits 11% / 14% / 38% on days 1–3."

**Measured on v38:**

| measure | v38 |
|---|---|
| bead machine load 2403 / 2404 / 2405 | **261 / 173 / 121 h** |
| TBR trolleys whose last missing part is apexed bead | **178 of 279 (64%)** |
| …of the trolleys planned on days 1–3 | **73 of 90** |
| median delay of those trolleys | **72 h** |
| apexing lots late vs their due time / median lateness | 53 of 77 / **40 h** |
| TBR built % of plan, days 1 / 2 / 3 | 11.4 / 13.9 / 37.7 |
| TBR cured | 17,940 |

---

## 2. How the bead section works (routing table only)

**Order** (`CTP_Master_Routing`, e.g. SKU 1121110020016KDC30):

| op seq | step | item | machines (routing) | feeds |
|---|---|---|---|---|
| 20 | final mixing | S54 (bead compound) | mixers | TBWB002 |
| **170** | **bead winding** | TBWB002 (bead bundle) | 2403, 2404, 2405 | TBAPBD003 |
| 20 | final mixing | A1031, A5542, B107 | mixers | TBAPBD003 |
| 30 → 130 → 150 | 901 calender → Bios cutter → chafer slitter (cutters bypassed) | BRTTL → BCFT001 → SNCT001 | 901, … | TBAPBD003 |
| **180** | **apexed bead** | TBAPBD003 | 2403, 2404, 2405 | GT 5001 |
| 190 | GT building | GT 5001 | builder | SKU |

**BOM.** One apexed bead (TBAPBD003) = 2 NOS bead bundle (TBWB002) + 1.970 kg A1031 + 2.354 kg A5542 + 0.400 kg B107 + 3.22 m chafer strip SNCT001.

So **winding always comes first** and apexing second, and **both run on the same three machines**.

**Machine eligibility (routing):**

| items | machines |
|---|---|
| TBWB001, TBWB002, TBWB003 (winding) | 2403, 2404, 2405 |
| TBWB005, TBWB006, TBWB007 (winding) | 2404, 2405 |
| TBAPBD001–005 … (apexing) | 2403, 2404, 2405 |
| TBAPBD006–010 … (apexing) | 2404, 2405 |
| TBAPBD011 / 012 / 013 | 2403+2405 / 2405 / 2403 |
| **TBWB004, TBWB008** | **no machine in the routing** (data gap) |

---

## 3. What we changed, step by step

### Step 1: v43 (e2e_v43b), bead pool balance

**Why.** 2403 carried 258 h while 2405 carried 120 h, although most bead items may run on all three. Each item was kept whole on one machine, and ties went to 2403.

**How.** For `balance.machines` (2403, 2404, 2405): when an item would push its machine past `balance.above_pct` (50%) of the horizon and another allowed machine is lighter, its lots are spread over the machines its **routing allows**. Mixers are not affected.

**Code.** `engine/phase4b_assignment.py` (`BAL_M`, `BAL_PCT`). Rules: `config/rules_v43b.yaml`.

**Effect.** Load 2403 / 2404 / 2405: 258 / 175 / 120 → 228 / 181 / 146 h. TBR cured 17,944 → 18,810 (with the 901 campaigns of the same version).

### Step 2: v44a, queue by the trolley each lot serves

**Why.** On its first day, 2403 ran **only winding** for 49 h. The queue ranked lots by their own due time, and a winding lot is always due earlier than the apexing lot it feeds, so winding always won. For 71 of the 90 day-1–3 TBR trolleys, apexed bead was the last missing part.

**How.** For `dispatch.trolley_need_first_machines` (2403–2405), the queue key is:
1. the start time of the **earliest trolley the lot ultimately serves**, in 8 h windows (traced back from the building lots through the material links);
2. within a window, **the stage closest to the trolley first** (apexing before more winding);
3. the exact need time;
4. the lot's own due time.

Winding still precedes the apexing that needs it: an apexing lot is only ready once its wound beads exist, and the check that apexed quantity never exceeds wound quantity still runs.

**First attempt failed.** Ranking only by trolley need gave an identical schedule. In the first days, winding and apexing lots serve the **same** day-1 trolleys, so it was a tie and winding's earlier due time decided. The "closest stage first" tie-break fixed that.

**Code.** `engine/phase5_placement.py` (`TN_M`, `tneed`, `tstage`, queue key in `push_ready()`). Rules: `config/rules_v44a.yaml`.

**Effect.** TBR days 2 / 3 built: 8.4 / 32.9% → 16.8 / 58.3%. TBR cured +296. Day 1 unchanged.

### Step 3: v44b, opening stock tested for "rested?" per demand

**Why.** Apexing needs compound A1031. The opening stock held **4,830 kg** of A1031, mixed 03:29–04:08 and rested by 07:30–08:08. Phase 2 tested "rested?" only against the **first trolley that ever needs the item** (07:07), so the whole stock was treated as unusable for 10 days and A1031 was mixed again from scratch. The same rule threw away 31k PCR / 16k TBR units of stock, mostly final compound.

**How.** With `wip.maturity_per_demand`, each demand may use stock that has rested by **its own** use time (need-by + minimum ageing). The first-use time stays as the floor, and expiry is unchanged.

**Code.** `engine/phase2_wip_netting.py` (`_draw_at`). Rules: `config/rules_v44b.yaml`.

**Effect.** A1031 stock used (4,830 kg), and +15k TBR units of stock overall. But TBR lost 3,180 tyres. The apexing lots were ready earlier, yet still started 30–45 h after open (explained in step 4).

### Step 4: diagnostic, dispatch log on 2403–2405

**Why.** Find exactly why a ready day-1 apexing lot still lost to winding.

**How.** `debug.log_machines` writes every pick on the logged machines (all queued lots, queue keys, ready time, which rule removed each, the pick). `engine/make_dispatch_log_workbook.py` builds `DISPATCH_LOG_2403_2405.xlsx`.

**What it showed.** The queue order was right: 112 of 118 picks were the best ready lot. But **110 of 118 picks were booked ahead**, a median of **36.9 h** before the lot started. A machine woken while still busy booked its next jobs immediately, choosing only from what was ready at that moment. At 22 Oct 21:15, 2403 booked three winding lots for **25 Oct** trolleys. The day-1 apexing lot became ready at 23 Oct 02:54 and went to the back of that line (it started 24 Oct 09:43).

### Step 5: v44c-all, a machine chooses its next lot only when it is free (current baseline)

**Why.** Remove the early booking found in step 4.

**How.** With `dispatch.choose_when_free: all`, a machine woken while busy is re-queued at the time it becomes free and chooses then, among everything ready at that moment. The queue order from step 2 is unchanged; it is now used at the right moment.

**Code.** `engine/phase5_placement.py` (re-queue right after a machine is taken from the clock; `CWF_ALL` / `CWF_M`). Rules: `config/rules_v44c_all.yaml`. Also tested on the bead machines only (`rules_v44c_bead.yaml`, 119,526 cured); "all" is better on both lines.

**Effect.** Bead picks booked ahead: median 36.9 h → **0.5 h**. First apexing lot: about 30 h → **17.5 h**. TBR days 2 / 3 built 54.5 / 112.8%. TBR cured **23,506**. This step is what made steps 2 and 3 pay off.

---

## 4. Effect in numbers

| measure | v38 | v43g24 | v44a | v44b | **v44c-all (baseline)** |
|---|---|---|---|---|---|
| bead load 2403 / 2404 / 2405 (h) | 261 / 173 / 121 | 249 / 181 / 127 | 249 / 181 / 127 | — | **192 / 198 / 188** |
| first apexing lot (h after open) | 21.3 | 28.5 | 28.5 | 30.1 | **17.5** |
| bead picks booked ahead, median | — | — | — | 36.9 h | **0.5 h** |
| apexing lots late / median lateness | 53 of 77 / 40 h | 54 / 37 h | — | — | **45 / 3 h** |
| TBR trolleys waiting last on apexed bead | 178 of 279 | 210 | 205 | — | **190** |
| …of the trolleys planned days 1–3 | 73 of 90 | 71 | 75 | — | **40** |
| median delay of those trolleys | 72 h | 63 h | — | — | **23 h** |
| median delay, all TBR trolleys | 63 h | 61 h | — | — | **21 h** |
| TBR built %, days 1 / 2 / 3 | 11.4 / 13.9 / 37.7 | 8.5 / 8.4 / 32.9 | 8.5 / 16.8 / 58.3 | 8.5 / 16.8 / 23.0 | **8.5 / 54.5 / 112.8** |
| TBR cured | 17,940 | 19,158 | 19,454 | 16,274 | **23,506** |
| PCR cured | — | 93,968 | 93,882 | 96,258 | **96,808** |
| total cured (of 161,670 eligible) | — | 113,126 | 113,336 | 112,532 | **120,314 (74.4%)** |

All runs pass the schedule checks (G1, G1b, G2–G8, G12).

**Note on "last missing part".** Apexed bead is still the last missing part for 68% of TBR kits. That is expected: apexing is the step just before building, so when everything runs close to time it is usually the last part to arrive. The measure that matters is **how long** trolleys wait for it, which fell from 72 h to 23 h.

---

## 5. Where it stands

| part of the problem | status |
|---|---|
| 2403 overloaded, 2404/2405 underused | **solved**: 192 / 198 / 188 h |
| winding takes the machine while apexing for an earlier trolley waits | **solved** (step 2 + step 5) |
| machines book work far ahead, so urgent lots queue behind | **solved** (step 5): median 0.5 h ahead |
| opening compound stock thrown away by the "rested?" test | **solved** (step 3) |
| apexing lateness | **mostly solved**: median 40 h → 3 h |
| TBR days 2–3 | **solved**: 54.5% / 112.8% built (day 3 catches up the backlog) |
| **TBR day 1** | **open**: 8.5% built |

---

## 6. Remaining issues

| # | issue | detail | what would fix it | owner |
|---|---|---|---|---|
| 1 | **TBR day 1 still 8.5%** | The first apexing lot is ready only ~17.5 h after open. Its compound (A5542; the 1,440 kg of stock is used up early) has to be mixed from 07:00 and then rest 4 h, and the mixers do not mix the apexing compounds first | "Bottleneck compounds first" on the mixers: compounds feeding apexing (A1031, A5542, B107) get priority in the first hours, among lots that are ready from 07:00 | engine (next step) |
| 2 | 45 of 77 apexing lots still late | median only 3 h late now; partly the day-1 effect above, partly capacity (~190 h per machine in 240 h) | issue 1; confirm whether winding really runs on these machines (issue 4) | engine / plant |
| 3 | Once-per-shift rule removes ready lots | the dispatch log shows 20 cases where a ready lot for an earlier trolley was removed by "same item once per shift per machine" (BRC-02) | check whether BRC-02 should apply to bead machines (it was written for other sections) | plant decision |
| 4 | **Do the TDA machines really wind beads?** | The routing puts bead winding (TBWB…) on 2403–2405, but the plant register names them "Bead Apexing TDA 1/2/3 TBR" and lists no TBR bead winder. About **200 h** of the ~580 h on these machines is winding | plant to confirm. If there is a separate winder, add it to `MasterMachines` and the routing; apexing would then have the TDA machines to itself | plant / master data |
| 5 | TBWB004, TBWB008 have no machine in the routing | TBWB004 (~50 h of winding) is placed by assumption A01 (department pool); TBWB008 also has no speed | add their machines (and TBWB008's speed) to `CTP_Master_Routing` | master data |
| 6 | Other TBR blockers now show | with bead waiting reduced, **TBR duplex** is the last missing part for 51 kits (7 in v38), pre-assembly for 24 | look at TBR duplex (1801/1802) next | engine (later) |
| 7 | Apexing also needs calender 901 | chafer strip SNCT001 comes from 901 fabric BRTTL through the bypassed cutter/slitter, so 901 delays can still hold apexing | covered by the 901 work ([FOUR_ROLL_CALENDER_901.md](FOUR_ROLL_CALENDER_901.md)) | engine |

---

## 7. Settings used (all off unless a rules file switches them on)

| setting | file that reads it | meaning |
|---|---|---|
| `balance.machines`, `balance.above_pct` | `phase4b_assignment.py` | spread an item over its routing-allowed machines once its machine passes this share (2403–2405, 50%) |
| `dispatch.trolley_need_first_machines`, `dispatch.trolley_need_bucket_h` | `phase5_placement.py` | queue lots on these machines by the trolley they serve (8 h windows), closest stage first |
| `wip.maturity_per_demand` | `phase2_wip_netting.py` | opening stock may serve a demand once it has rested by that demand's own use time |
| `dispatch.choose_when_free` | `phase5_placement.py` | a busy machine chooses its next lot only when it is free (`all` or a list of machines) |
| `debug.log_machines` | `phase5_placement.py` | write the dispatch log for these machines (diagnostic only) |

Current baseline: `--run e2e_v44c_all --rules config/rules_v44c_all.yaml` (outputs in `outputs/e2e_v44c_all/`, dispatch log `DISPATCH_LOG_2403_2405.xlsx`).
