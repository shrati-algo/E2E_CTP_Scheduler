# v38 schedule: the 15 issues and how to fix them

This folder covers the 15 issues raised about schedule run **`e2e_v38`**: the October plan, 22 Oct 07:00 → 1 Nov 07:00, PCR + TBR, data from `JK_CTP_PPDS`. Each issue was checked again against the run's own outputs before anything was written here.

v38 result: **101,210 of 170,478 tyres cured (59.4%)**. PCR cured 83,270 and TBR cured 17,940.

| file | for whom | what it holds |
|---|---|---|
| [HOW_TO_CORRECT.md](HOW_TO_CORRECT.md) | engineers | For each issue: what was claimed, what the v38 data shows, the root cause (file:line), the fix step by step, a test that proves the fix, and the expected effect |
| [EXPLAINER.md](EXPLAINER.md) | planners, management | The same 15 issues in plain words: what goes wrong on the floor and what changes after the fix |
| [PCR_COLD_START.md](PCR_COLD_START.md) | everyone | PCR first-day problem: bead gate bug (fixed), 901 serving TBR first, compounds mixed late |
| [TBR_APEXED_BEAD.md](TBR_APEXED_BEAD.md) | everyone | The TBR bead section (winding + apexing on 2403–2405) and TBR cold start: every change, why, how, code, numbers, remaining issues |
| [FOUR_ROLL_CALENDER_901.md](FOUR_ROLL_CALENDER_901.md) | everyone | The 4-roll calender (901) issue on its own: what was wrong, every change tried (why, how, code), the numbers, and where it stands |
| [CHANGE_LOG.md](CHANGE_LOG.md) | everyone | Every change actually made since v38 (v39, v40, …), in simple words: what was wrong, the logic of the fix, files and lines, results |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | engineers | The order to make the changes (v39 → v47), each step's code edits and gates, and the data-blocker policy: blocked SKUs go to a `VALIDATION_REPORT` and out of the final figures; missing data that can be assumed is assumed and listed |

Nothing in the scheduler was changed. These are documents only.

## Verdict table

**Verdict.** Confirmed means the v38 outputs reproduce the issue as stated. Partly means it is real but the stated number or cause is wrong. Not reproduced means the data does not show it.

**Type.** Code is a bug or missing logic in the engine. Model is a deliberate simplification that now hurts. Data is a missing or wrong master record. Process is how the plan is run.

| # | issue | verdict | severity | type | the core fact |
|---|---|---|---|---|---|
| 1 | Cold start | Confirmed | Critical | Code + Data | 0 of 217 day 1–3 kits fully covered. Days 1–3 lose PCR 27,028 and TBR 6,533 tyres |
| 2 | Press capacity | Partly | High | Code | Presses cure only 59% (PCR) and 47% (TBR) of the horizon. TBR has 80 presses, not 64. The real loss is the green-tyre push, not press count |
| 3 | 8,808 tyres can't be built | Confirmed | High | Data | 8 SKUs have no building lot. 16 presses (3,840 press-h) idle. 325 GT in stock are not used |
| 4 | Building overlaps | Confirmed | **Critical** | Code | 300 lots overlap, 644 h in total, on all 20 builders. Caused by an index bug in phase 5 |
| 5 | Durations disconnected | Confirmed | **Critical** | Code | Correlation between written duration and quantity is −0.035 (the plan has 0.588). Same bug as #4 |
| 6 | Mould changes cost zero | Confirmed | High | Code | 35 of 60 SKU changes on presses have a zero gap. The push loop drops the changeover |
| 7 | Cutting section missing | Partly | Medium | Model + Report | Bypassed on purpose since v33, with transfer time added. But 467 cut codes are mislabelled "infinitely available" |
| 8 | 4RC 901 run as a changeover machine | Confirmed | High | Code + Data | 166 lots, 165 item changes, 71 PCR↔TBR switches. 125 h running against 131 h of gaps |
| 9 | TBR bead pool unbalanced | Confirmed | High | Code | 2403 is loaded to 104%, 2404 to 63%, 2405 to 45%. Apexed bead is the last item to arrive for 178 TBR kits |
| 10 | Idle presses and builders | Confirmed | High | Consequence | Presses are 53% used, builders 54%. 100% of press idle time is waiting for green tyres |
| 11 | Green-tyre ageing unchecked | **Not reproduced** | Low | — | Max age 85 h, 0 over 168 h. 244 over 72 h are worth a check, but there are no violations |
| 12 | Low-volume SKUs dropped | Confirmed | Medium | Code + Data | 9 PCR SKUs (3,564 tyres) have 0 cured. They are planned on 28–31 Oct and then pushed past 1 Nov |
| 13 | Compound over-produced 1.56× | Partly | Medium | Config | Real ratio is 1.15× PCR and 1.10× TBR, a 494k kg surplus. 354k kg of it is the +5 batch buffer |
| 14 | Audit trail stale | Confirmed | Medium | Code | Hardcoded 26 Sep snapshot text. 58 lot IDs are duplicated across PCR and TBR. 117 items run on default rates |
| 15 | No end-of-horizon handling or replanning | Confirmed | High | Model + Process | 56,089 tyres are built and 69,268 cured after 1 Nov. 0 of 12 shift refreshes are done |

## Fix order

The detailed, step-by-step version (with versions, files and gates) is in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

Fix the bugs that make the schedule wrong before tuning what makes it short.

1. **Correctness: #4 and #5.** This is one bug, in [phase5_placement.py:1413-1581](../engine/phase5_placement.py#L1413). Until it is fixed, every building time in v38 is wrong.
2. **Correctness: #6.** Keep the mould-change gap in the curing push ([phase7_curing.py:176-218](../engine/phase7_curing.py#L176)).
3. **Correctness: #14.** Fix the stale audit text and the duplicate lot IDs, so the reports can be trusted for the remaining steps.
4. **Volume: #1 cold start.** Group kits for FEFO, make cut stock visible, take B616M out of the kits, and allow a ramp-up window.
5. **Volume: #8 and #9.** Run 901 in campaigns, add the 4 FRC master rows, and balance the TBR bead pool.
6. **Volume: #3.** Fix the master data: GT 1422 and the GT 5103/5083 conflict. Map GT stock by the curing GT code.
7. **Volume: #12.** Add a shortage penalty to the dispatcher so small SKUs are not left at the end of the month.
8. **Efficiency: #13.** Cut the buffer from 5 batches to 1–2 per run.
9. **Process: #15.** Add rolling replanning per shift, and carry work in progress across horizons.
10. #2 and #10 improve on their own once 1, 4, 5 and 6 are done. Measure them again; no separate fix is needed.
11. #7 is a reporting fix. #11 needs no fix, only an alert at 72 h.

## How to verify a fixed run (v39)

Run the checks in [HOW_TO_CORRECT.md](HOW_TO_CORRECT.md), under "Verification" in each issue. The short gate list:

| gate | v38 | target |
|---|---|---|
| building overlaps | 300 lots / 644 h | 0 |
| corr(duration, qty) on builders | −0.035 | ≥ 0.5 (plan has 0.588) |
| press SKU changes with zero gap | 35 of 60 | 0 |
| day 1–3 kits fully covered | 0 of 217 | ≥ 60% |
| 2403 load | 104% | ≤ 90% |
| low-volume SKUs with 0 cured | 9 | 0 (or explained by data) |
| duplicate lot_id across lines | 58 | 0 |
| tyres cured | 101,210 | report; do not tune to it |

Evidence for every number: `outputs/e2e_v38/`, `data-ppds-v38/`, `plan-e2e_v38/`, `audit_v38/V38_SCHEDULE_AUDIT.xlsx`.
