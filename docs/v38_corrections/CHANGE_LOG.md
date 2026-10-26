# Change log: scheduler fixes after v38

This file records, in simple words, every change made to the scheduler since the v38 audit: what was wrong, what we changed, which file and lines, and what it did to the results. A new entry is added each time something changes.

All versions use the same input data (`data-ppds-v38`) and the same October plan (`plan-latest`). So any change in the numbers comes from the code, not from the data.

---

## Summary

| version | what changed | cured (of eligible demand) | checks |
|---|---|---|---|
| v38 (starting point) | — | 101,210 of 170,478 = 59.4% (blocked demand still counted) | 300+ building overlaps, 35 zero-minute mould changes, 58 duplicate lot IDs |
| v39 | Data-blocker policy + validation report; building overlap fixed | 101,094 of 161,670 = **62.5%** | overlaps 0; durations correct |
| v40 | Mould-change time on presses; unique lot IDs; audit text read from the run | 100,852 of 161,670 = **62.4%** | G1–G7 all pass |
| v40 (later the same day) | Removed a misleading sentence from the compliance text; workbook rebuilt | unchanged | unchanged |
| v41 | Green-tyre stock found through the curing plan's GT code | 100,894 of 161,670 = **62.4%** (+42) | G1–G7 all pass |
| v42 | Builder setups follow the real predecessor; building lot IDs unique; cut stock counted; BOM self-reference bug fixed; eligible SKUs get GT stock first | 106,830 of 161,670 = **66.1%** (+5,936) | G1, G1b, G2–G7 all pass |
| v42f | v42 + builders keep the plan's machine and sequence (only times shift) | 102,994 of 161,670 = **63.7%** (−3,836 vs v42) | G1, G1b, G2–**G8** all pass |
| v43 (e2e_v43b) | v42f + calender 901 campaigns (PCR/TBR in blocks, cheapest changeover) + TBR bead balance | 106,938 of 161,670 = **66.1%** (+3,944 vs v42f) | G1, G1b, G2–G8 all pass |
| v43c / v43d (trials) | bigger lots on 901 over the shelf-life window (v43d keeps 24 h delivery lots) | v43c 102,196 (TBR −5,110); v43d 113,610 (TBR −138) | pass; not adopted (see entry) |
| **v43g24 (new baseline)** | v43 + 901 progressive release + 901 lots bucketed over 24 h | 113,126 of 161,670 = **70.0%** (+6,188 vs v43; PCR +5,840, TBR +348) | G1, G1b, G2–G8 all pass |
| v44a (candidate) | v43g24 + TBR bead machines queue by the trolley they serve (closest stage first) | 113,336 = **70.1%** (TBR +296, PCR −86); TBR day 2/3 built 16.8% / 58.3% (was 8.4% / 32.9%) | G1, G1b, G2–G8 all pass; decision pending (PCR −86) |
| v44b (trial) | v44a + opening stock "rested?" tested per demand (+31k PCR / +15k TBR units of stock used) | 112,532 (PCR +2,376, TBR −3,180) | pass; not adopted: TBR day 1 still held by bead winding |
| **v44c-all (new baseline)** | v44b + every machine chooses its next lot only when it is free (no booking ahead) | **120,314 = 74.4%** (vs v43g24: PCR +2,840, TBR +4,348) | G1, G1b, G2–G8 all pass |
| v44c-bead | same, bead machines 2403–2405 only | 119,526 = 73.9% | pass; v44c-all better on both lines |
| **v45a (new baseline)** | v44c-all + aggregate bead gate off (removed 81 overlapping lots on PCR 201/202) + new check G1u | 120,314 = 74.4% (unchanged) | G1, G1b, **G1u**, G2–G8 all pass |
| v45b (candidate) | v45a + calender 901 queues by the trolley each lot serves | **125,466 = 77.6%** (PCR +6,426, TBR −1,274) | pass; decision pending (TBR −1,274) |
| v46a (candidate) | v45b + cut / slit stock counted as the material it was cut from (cutters and slitters bypassed); transfer time = minimum ageing, checked | **128,166 = 79.3%** (vs v45b: PCR +2,124, TBR +576) | all 21 checks pass; bypass report 0 breaches |
| v46b (candidate) | v46a + TBR TDA winds and apexes in line: one operation at the slower speed, no separate winding lots | **128,566 = 79.5%** (vs v46a: PCR +64, TBR +336); TBR day 1 built 700 (was 402) | all 21 checks pass |
| v46c (trial) | v46b with the BOM from `CTP_Master_BOM_v2` (cutters / slitters already removed); transfer time from the routing | 134,476 = 83.2% (vs v46b: PCR +5,858, TBR +52), **but not comparable**: see entry (v2 fixes a double-count on PCR, drops real TBR steps) | all 21 checks pass |
| v46d (candidate on BOM v2) | v46c + engine fix: each item's recipe counted once in the BOM walk | **136,470 = 84.4%** (vs v46c: PCR +1,164, TBR +830); engine per-tyre = BOM row sum on every TBR pair | all 21 checks pass |
| v46e (candidate on BOM v2) | v46d + 4-roll calender lots in whole dips (`CTP_Master_FRC`), each run covering up to the shelf window; partial dip when one dip would expire | **139,418 = 86.2%** (vs v46d: PCR +2,940, TBR +8); 901 lots 127 → 65, changeover 69.9 → 36.3 h | all 21 checks pass |

Why the number went down slightly: v38 and v39 counted time that did not exist (trolleys finishing too early, presses changing moulds in 0 minutes). The lower numbers are the true ones.

---

## v39: data-blocker policy

### What was wrong
Demand for SKUs that the database cannot support was counted in the plan. For example, a SKU with no BOM can never be built. Counting it made fulfilment look worse than the schedule really was, and nobody could see which data was missing.

### The new rule (logic)
Every SKU is put in one of three groups:

| group | meaning | counted in the final figures? |
|---|---|---|
| **CLEAN** | all the data exists | yes |
| **ASSUMED** | some data is missing, and a stated assumption fills it (for example "use the department's average process time") | yes, and marked as assumed |
| **DATA_BLOCKED** | data is missing that cannot be guessed safely (no BOM, no green-tyre code, no press, no cure time) | **no**. It is taken out of both the plan and the cured count, and listed in a report |

Rules for assumptions:
* An assumption is allowed only if the stand-in value comes from the plant's own data.
* Each one is written down with its value, its source and the tyres that depend on it.
* A value with no source is never made up. In that case the SKU is blocked.

### What was changed in the code

| file | change |
|---|---|
| `engine/blocker_policy.py` (**new**) | The one list of all assumption codes (A01–A12) and blocker codes (B01–B05), with what each means and who has to fix it. Every other file uses this list |
| `engine/phase0_validate.py` | New function `_sku_status`: decides CLEAN / ASSUMED / DATA_BLOCKED for every SKU, including SKUs that appear only in the curing plan. Writes `phase0_sku_status.csv` and prints the counts |
| `engine/phase7_curing.py` | Tags every curing lot with its SKU's group (`sku_state`, `blocker_code`, `in_kpi`). Prints the raw figure and the **eligible** figure. Nothing is rescheduled; blocked lots are only tagged |
| `engine/make_validation_report.py` (**new**) | Writes `VALIDATION_REPORT.xlsx` on every run. Sheets: Summary, Blocked SKUs, Blocking records, Assumptions, catalogues, Assumed tyres, Data gaps (handled), Schedule checks, Run stamp. Also writes `VALIDATION_SUMMARY.csv`, `VALIDATION_REPORT_blocked.csv` and `ASSUMPTIONS.csv` |
| `engine/make_fulfilment_report.py` | The fulfilment report now uses eligible demand, and prints blocked tyres on their own line ("excluded, data-blocked") |
| `run.py` | New **step 9**: the validation report. It is required, so if it fails, the run fails |
| `config/rules_v39.yaml` (**new**) | Inherits everything from v38; no new settings |

### Result
* **8,808 tyres excluded**, on 4 SKUs, all with no BOM in `CTP_Master_BOM`:
  * 1D25214012008QXPC0 (596)
  * 1121129A20152JDLF0 (3,680)
  * 1122129A20152JULF0 (3,000)
  * 112222982C016MJXM0 (1,532)
* 4 small TBR SKUs that appear only in the curing plan have a BOM, so they are **not** blocked. They lose their tyres because of a code gap that step 5 will fix.
* 160 assumptions listed: A01 = 27, A02 = 117, A03 = 16. Also 348 data gaps the engine already filled with a stand-in.

---

## v39: building overlap fixed

### What was wrong
On every building machine, trolleys overlapped: two trolleys on one builder at the same time. In v38 that was 335 lots and 644 hours, on all 20 builders.

The start times were right, but the end times were wrong. The engine kept each trolley's run time in a separate list, numbered by row. Then it re-sorted the trolleys and renumbered the rows, and later added the old list back by row number. So each trolley got **another trolley's run time**.

Example, builder 3401: trolley BLD000007 (496 tyres, planned 7.98 h) was given 3.00 h. A 31-tyre trolley was given 3.23 h instead of 29 minutes, so it "ended" after the next trolley had started.

### The fix (logic)
1. The run time is now stored **inside the trolley table**, so it moves with its trolley when the table is sorted. End = own start + own planned duration.
2. After the bead-gate step (which can move a trolley later), each builder is walked in order. A trolley that would start before the one ahead of it has finished is pushed back. Trolleys only move later, so none starts before its kit is ready.
3. Just before the building plan is saved, the engine checks every builder for overlaps and every trolley's duration. If either check fails, the run **stops** and lists the first 10 problems.

### What was changed in the code

| file | lines | change |
|---|---|---|
| `engine/phase5_placement.py` | 1418–1419 | run time stored as column `_span`; end = start + `_span` |
| `engine/phase5_placement.py` | 1586 | the second place that set the end time now uses `_span` too (this was the line with the bug) |
| `engine/phase5_placement.py` | bead-gate block | removed a second, separately computed run-time list; uses `_span` |
| `engine/phase5_placement.py` | 1722–1738 | new: re-lay each builder after the bead gate |
| `engine/phase5_placement.py` | 1744–1764 | new: overlap and duration checks on the table that is written; stop if they fail |
| `engine/make_validation_report.py` | 175–181 | checks **G1** (no building overlaps) and **G2** (duration = plan) measured again from the saved file |

### Result
| | v38 | v39 |
|---|---|---|
| overlapping trolleys | 335 lots, 644 h | **0** |
| trolley duration = plan | 20 of 693 | **693 of 693** |
| correlation of duration with tyres | −0.035 | 0.92 (PCR) / 0.996 (TBR) |
| built in horizon | PCR 83,779 / TBR 20,905 (overstated) | PCR 82,608 / TBR 20,689 |
| cured | 101,210 | 101,094 (−116) |

---

## v40: mould-change time on presses

### What was wrong
When a curing lot was late (its green tyres arrived late), it was pushed later, and every lot behind it on the same press was pushed too. Each pushed lot started **the instant the previous lot ended**, even when the SKU and mould changed. 35 of 60 SKU changes on presses had a 0-minute gap. No press can change a mould in 0 minutes.

### The fix (logic)
The plant's curing plan already states the changeover on the first row after a change: `CO_Mins + Mould_Clean_Mins`, 360–520 minutes. The engine now:
1. Keeps that value on each curing lot.
2. Remembers the last SKU on each press. For each lot in press order, it works out the gap:
   * the plan states a changeover on this row → use it (also covers mould cleaning on the same SKU);
   * the SKU changes but the plan states none → use assumption **A05**: 120 min for PCR, 180 min for TBR (from the rules file). It is recorded every time it is used;
   * same SKU, no setup stated → no gap.
3. Starts the lot no earlier than the later of: its green tyres being ready, and the previous lot's end plus the gap. Maintenance blocks still apply after that.
4. Writes `co_min` (the gap) and `co_source` (where it came from) on each lot.

### What was changed in the code

| file | lines | change |
|---|---|---|
| `engine/phase7_curing.py` | 173 | each lot keeps the plan's changeover (`plan_setup_min`) |
| `engine/phase7_curing.py` | 194–233 | the push loop now leaves the changeover gap; tracks the last SKU per press |
| `engine/phase7_curing.py` | 241–252 | writes `co_min` and `co_source`; writes `phase7_assumptions.csv` whenever A05 is used |
| `engine/make_validation_report.py` | 186–201 | check **G3**: every SKU change has a gap at least as long as its required changeover |
| `config/rules_v40.yaml` (**new**) | 28 | `curing.mould_change_min: {pcr: 120, tbr: 180}`, the A05 assumption, to be replaced by the plant's real value |

### Result
| | v38 | v39 | v40 |
|---|---|---|---|
| press SKU changes with 0-minute gap | 35 of 60 | 34 of 60 | **0 of 60** |
| median gap at a SKU change | 0 min | 0 min | **451 min** |
| gaps shorter than the plan's changeover | — | — | **0** |
| where the gap came from | — | — | all 66 from the plan; A05 never needed |
| cured | 101,210 | 101,094 | 100,852 (−242) |

---

## v40: unique lot IDs

### What was wrong
PCR and TBR each numbered their upstream lots from 1. So `B616M_L00001` existed in both lines: 58 duplicate IDs. Reports joined on the lot ID could mix the two lines.

### The fix
Each upstream lot ID now starts with its line: `PCR_B616M_L00001`, `TBR_B616M_L00001`. Before changing it, we checked that no code reads the old format; the engine always joins on `line::lot_id`.

| file | change |
|---|---|
| `engine/phase3_lot_sizing.py` | new module setting `_LINE`, set from `--line` in `main()`; lot ID = `{LINE}_{item}_L{nnnnn}` |
| `engine/make_validation_report.py` | check **G4**: no lot ID is used in both lines |

Result: 58 duplicates → **0**. The schedule itself is unchanged (same 2,582 upstream lots and times).

---

## v40: audit text read from the run

### What was wrong
The business-rule compliance report still printed text written for an old September run on every run: "WIP snapshot 2026-09-26 23:23", "12 shifts", "6 PM blocks inside this horizon".

### The fix
`engine/br_compliance_v15.py` now reads these from the run:
* **Horizon:** first and last building time.
* **Number of shifts:** horizon ÷ 8 h.
* **WIP snapshot:** date and shift from `opening_wip.csv`.
* **PM blocks inside the horizon:** counted from the PM table.

BR-15 now says: "0 of 30 shift refreshes … WIP snapshot 2026-10-22 shift A, 0.5 h stale at the first shift".

What this showed: the PM/downtime table (`pm_mtc_downtime_schedule`) ends on 29 Sep. So **no maintenance is applied in the October horizon at all**. This has been true since v31 and was hidden by the old text. Extending that table is a data job for the plant.

---

## v40 (later the same day): misleading sentence removed

### What was wrong
The rule-compliance sheet of the workbook said the machine-free variable "is declared and never used — the same defect that lets 803 lots overlap". That referred to an old fault on the upstream machines that was fixed in v16. It made the v40 schedule look as if it still had overlaps.

### The fix
* The sentence in `engine/br_compliance_v15.py` (BR-15, "how to close") now describes the current state.
* `BUSINESS_RULES_COMPLIANCE_v15.csv` and `CTP_e2e_v40_SCHEDULE.xlsx` were rebuilt for e2e_v40.

The schedule is unchanged.

Overlaps were recounted in every file that holds the building schedule (workbook sheet, `phase5_building_plan.csv`, floor schedules), counting both trolley-against-the-previous-one and every pair. v39 and v40 show **0**. v31–v38 had 413–460 overlapping lots.

---

## v41: green-tyre stock found through the curing plan's GT code

### What was wrong
Phase 7 learned which green tyre an SKU uses **only from the building lots**. An SKU that the curing plan cures but the building plan never builds could not find its own green tyres on the rack, even though the curing plan names the GT on every row. In v40:
* 4 small TBR SKUs (92 tyres) cured nothing;
* 233 green tyres of data-blocked SKUs were never used.

### The fix (logic)
1. Every curing SKU gets its GT code. It comes from its building lots if it has any, otherwise from the curing plan's `GT_Code` column.
2. If the building plan and the curing plan name **different** GTs for the same SKU, the building GT is kept, because that is the tyre physically built. The conflict is written down as assumption **A11** (1 case: 1123111020016KJC30, GT 5095 vs GT11R20JUC3).
3. Stock is shared among all SKUs that use a GT, earliest curing slot first, and only before the stock expires. This rule is unchanged.
4. Green tyres cured for **data-blocked** SKUs are shown in the validation report ("cured from stock, not counted") and stay out of the final figures.

### What was changed in the code

| file | change |
|---|---|
| `engine/phase7_curing.py` | new block after `gt_item = ...`: fills the SKU → GT map from the curing plan's `GT_Code`, records A11 conflicts, and prints how many SKUs were mapped from the curing plan. A11 rows are added to `phase7_assumptions.csv` |
| `engine/blocker_policy.py` | A11 rewritten to the rule actually used, and marked active |
| `config/rules_v41.yaml` (**new**) | inherits v40; no new settings |

Not done in v41: "free the presses that carry only blocked SKUs". Moving a lot to another press is press re-sequencing, which is planned for v44.

### Result
| | v40 | v41 |
|---|---|---|
| 4 small TBR SKUs (92 tyres) | 0 cured | **90 cured** from stock (the 2-tyre SKU's slot is after its stock expires) |
| stock cured for blocked SKUs (not counted) | 0 | 160 tyres |
| TBR cured, eligible | 17,802 | 17,844 |
| total cured, eligible | 100,852 (62.4%) | **100,894 (62.4%)** |

The 90 new tyres net to +42. The other 48 went missing because blocked SKU 112222982C016MJXM0 now takes 56 tyres of GT 5103 from stock. Eligible SKU 112222982C152MJXM0 uses the same GT and lost 48 tyres. **That is against the blocker policy**: blocked demand must not take stock from eligible demand. Fixed in v42 (eligible SKUs draw GT stock first; blocked SKUs get only what is left).

---

## v42: builder setups, unique building lot IDs, and the cold start

v42 was built in steps. Each step was run on its own and kept only if it passed every check and **neither line lost tyres**. Correctness fixes are always kept.

| run | adds | PCR | TBR | total | decision |
|---|---|---|---|---|---|
| v41 | (base) | 83,050 | 17,844 | 100,894 | — |
| v42a | builder setup follows the trolley actually before it; building lot IDs unique | 83,050 | 17,840 | 100,890 | **kept** (correctness) |
| v42b | + 6a: stock already cut is counted | 87,110 | 19,906 | 107,016 | **kept** (+6,126) |
| v42c | + 6b: BOM self-reference bug fixed | 87,110 | 19,672 | 106,782 | **kept** (correctness, see below) |
| v42d | + 6c: most-complete kits get stock first, both lines | 91,024 | 17,098 | 108,122 | dropped: TBR −2,574 |
| v42e | 6c on PCR only | 91,024 | 17,124 | 108,148 | dropped: TBR still −2,548 |
| 6d | ramp-up window | — | — | — | not run (see below) |
| **v42 (final)** | v42c + eligible SKUs get GT stock first | **87,110** | **19,720** | **106,830 (66.1%)** | **final** |

### Part A1: a builder's setup follows the trolley actually before it

**What was wrong.** In the plant's plan, each trolley's duration already **includes** its changeover (`CO_Mins`): the extra time on trolleys with a setup matches `CO_Mins` almost exactly. That setup was planned for the trolley the **plan** put before it. The scheduler starts trolleys when their kit is ready, so many run in a different order. In v40, 45 product changes on 16 builders came right after a different product with no setup inside the trolley.

**The fix (logic).**
* Run time = plan duration − plan setup.
* Setup:
  * if the plan states a setup on this trolley, keep it (never removed);
  * otherwise, if the product really changes from the trolley actually before it, add the plan's median setup for that SKU (or, if the SKU never has one, the line's median, assumption **A13**);
  * same product as the trolley before it → no setup.
* This is applied everywhere a builder's trolleys are laid out: the kit-ready dispatch, the move-to-another-builder step and the final re-lay.
* New check **G1b**: every product change on a builder has a setup. If not, the run stops.

**Code.**

| file | change |
|---|---|
| `engine/phase5_placement.py` | new `_setup()` / `_dur()` helpers after `_span`; the kit-ready loop, `_serialise()` and the final re-lay use the setup for the actual predecessor; new columns `setup_applied_min`, `setup_source`, `prev_sku_on_machine`; G1b check before writing; A13 uses go to `phase5_assumptions.csv`. Switch: `building.setup_by_actual_predecessor` |
| `engine/make_validation_report.py` | G1b added; G2 compares against run time + applied setup; A13 read from `phase5_assumptions.csv` |
| `engine/blocker_policy.py` | A13 added to the catalogue |

**Result.** 31–60 product changes per run got a setup they did not have. A13 was never needed, because every SKU has a setup somewhere in the plan. G1b: 0 changes without a setup (194 PCR, 133 TBR changes). Cost: 4 tyres.

### Part A2: building lot IDs unique across lines

PCR and TBR both numbered building lots `BLD000000…` (279 shared IDs). They are now `PCR_BLD…` / `TBR_BLD…`. Check G4 now covers building lots too.
Code: `engine/phase1_building_demand.py` (the ID) and `engine/make_validation_report.py` (G4).

### Step 6a: stock already cut is counted

**What was wrong.** Since the cutter/slitter bypass (v33), cut codes are not "made", so the BOM walk skipped past them. Stock lying on the rack under a cut code was never used: about 39,500 PCR and 30,700 TBR units on 22 Oct.

**The fix (logic).** For netting only, the BOM walk keeps the bypassed cut codes. A cut code's own stock is used first. The existing cascade then reduces the demand for the calendered or extruded material underneath it by the same share. Nothing is scheduled on a cutter: the cut code still has no demand row and no lot. Units are converted using the BOM's unit for the cut code.

**Code.** `engine/phase2_wip_netting.py`: switch `cold_start.net_cut_stock`; netting walk = made items + bypassed codes; `unit_bridge()` accepts the BOM units of cut codes; new file `phase2_bypass_wip.csv` lists every draw.

**Result.**
* 30,429 PCR and 29,916 TBR units of cut stock used.
* **+4,060 PCR and +2,066 TBR tyres**: the biggest single gain since v38.
* PCR day-1 trolley delay fell from 37.5 h to 23.8 h; TBR from 54.2 h to 45.4 h.

### Step 6b: BOM self-reference bug (B616M counted 24 times)

**What was wrong.** `CTP_Master_BOM` has 233 PCR rows where an item lists **itself** as a child (`CAP 66 → CAP 66`, also `CAP30`, `GBCAP20`). These items have no routing, so the walk passes through them, and each time it re-entered the same item, until its depth limit of 24. Their children were counted 24 times. B616M came out at 3.63 kg per tyre instead of 0.151 kg: about **1,067,000 kg of PCR demand instead of 44,500 kg**.

**The fix.** The walk skips a row whose child is the same item as its parent.
Code: `engine/ctpio.py`, `WalkCache` (switch `bom.skip_self_reference`). Every phase that walks the BOM gets the fix.

**Result.**
* B616M demand: 1,067,469 kg → 44,478 kg. B616M lots: 25 → 10.
* Mixing time, both lines: 679 h (v41) → 501 h in the final v42.
* PCR cured unchanged, because mixing is not what limits PCR curing now.
* TBR −234. PCR and TBR share mixers and calender 901 in one placement run, so freeing PCR mixer time changes the order TBR lots get picked.

Kept, because without it the plan books about a million kg of compound that isn't needed. This is also most of the "compound over-produced" issue (#13).

### Step 6c: most-complete kits get stock first (measured, dropped)

**Idea.** Give opening stock first to trolleys whose whole kit can come from stock.

**What we found.**
* On 22 Oct no trolley's kit can be covered fully from stock: every kit needs at least one freshly made input, and most miss 5–7. So the rule was relaxed to "kits missing at most 2 inputs go first" (12 PCR, 7 TBR trolleys).
* With the rule on for both lines: PCR +3,914, but TBR −2,574.
* With the rule on **for PCR only**: TBR still lost 2,548, and TBR day-1 delay rose from 46 h to 98 h. PCR's changed priorities take the shared mixers and calender 901 away from TBR.

It fails the "no line loses tyres" rule, so it is **off** in v42. The code stays, switched off (`cold_start.kit_complete_fefo`, which can be set per line). Run `e2e_v42e` is kept as the reference.

### Step 6d: ramp-up window (not run)

The kit gate already starts every trolley at the moment its kit is really ready. A window that admits only kits fully covered by stock would admit nothing (0 of 217). It cannot help, so it was not built.

### Also in v42: eligible SKUs get green-tyre stock first

In v41, blocked SKU 112222982C016MJXM0 took 56 tyres of GT 5103 from stock. That cost eligible SKU 112222982C152MJXM0, which uses the same GT, 48 tyres. Now eligible SKUs draw GT stock first, and blocked SKUs get only what is left.
Code: `engine/phase7_curing.py` (GT stock allocation sorted eligible-first). Result: 112222982C152MJXM0 back to 1,528 cured; TBR +48.

### Result of v42 (final)

| | v41 | v42 |
|---|---|---|
| cured, eligible | 100,894 (62.4%) | **106,830 (66.1%)** |
| PCR / TBR cured | 83,050 / 17,844 | 87,110 / 19,720 |
| built in horizon, PCR / TBR | 82,608 / 20,689 | 90,952 / 22,287 |
| day-1 trolley delay (median), PCR / TBR | 37.5 h / 54.2 h | 22.4 h / 45.7 h |
| cured on days 1–3, PCR / TBR | 13,804 / 1,908 | 18,210 / 2,442 |
| mixing hours, both lines | 679 h | 501 h |
| checks | G1–G7 pass | G1, **G1b**, G2–G7 pass |

---

## v42f: builders keep the plan's machine and sequence (only times shift)

### What was wrong
The plant's rule: the schedule must keep the same machine–SKU pairs as the input plan, in the same order. If the plan says A → B → C on a machine, the schedule must also run A → B → C; only the times may move.

Checked against the input plan, v42 broke this on the **building machines**:

| | PCR | TBR |
|---|---|---|
| trolleys moved to another builder | 17 (4,569 tyres) | 3 (288 tyres) |
| builders running a different order | 10 of 11 | 7 of 9 |
| builders where the SKU sequence (A-B-C) changed | 10 | 6 |

Example, builder 3411: plan TRAT0 → **HRHT0 → HRHT0** → SELS0 → SELS0; v42 TRAT0 → **SELS0 → SELS0** → HRHT0 → HRHT0.

The **curing presses already followed the plan**: same press, same order, on all 86 PCR and 71 TBR presses.

Why: two settings switched on back in v10 and inherited ever since (`config/rules_v10.yaml`):
1. `building.sequence_by: kit_ready`: each builder took the earliest trolley whose kit was ready, not the next one in the plan.
2. `building.rebalance: on`: a late trolley could move to another allowed builder that could start it sooner.

There was also a smaller cause in the v39 re-lay: it sorted by actual start time, so a trolley delayed by the bead step could drop behind the next one.

### The fix (logic)
* Every builder takes its trolleys **in the plan's order**. A trolley starts at the later of: its kit being ready, and the previous trolley (in plan order) finishing. A late kit therefore holds back everything behind it on that builder. Nothing is reordered and nothing moves to another builder.
* The final re-lay sorts by **planned** start, so the plan order is kept even after the bead step delays a trolley.
* Setups follow the actual predecessor, which is now the plan's predecessor, so the plan's own `CO_Mins` apply. In 4 places the plan itself changes product with no `CO_Mins`; those get the plan's median setup for that SKU (check G1b).
* New check **G8**: building and curing each keep the plan's machine and order. With `building.enforce_plan_sequence: true` the run stops if G8 fails.

### What was changed in the code

| file | change |
|---|---|
| `engine/phase5_placement.py` | the plan-order path (used when `building.sequence_by` is not `kit_ready`) rewritten: walks trolleys in plan order per builder and uses the setup for the actual predecessor; the final re-lay sorts by planned start in plan-order mode |
| `engine/make_validation_report.py` | new check **G8** for building (machine and order vs `phase1_building_lots.csv`) and curing (order per press vs planned start); G8 is fatal when `building.enforce_plan_sequence` is on |
| `config/rules_v42f.yaml` (**new**) | `building.sequence_by: plan_order`, `building.rebalance: off`, `building.enforce_plan_sequence: true` |

### Result

| | v42 (kit-ready order) | v42f (plan order) | change |
|---|---|---|---|
| builders whose sequence differs from the plan | 17 of 20 | **0 of 20** | — |
| trolleys on another builder | 20 | **0** | — |
| curing presses reordered | 0 | 0 | — |
| built in horizon, PCR / TBR | 90,952 / 22,287 | 89,368 / 19,970 | −1,584 / −2,317 |
| cured, PCR | 87,110 | 85,050 | −2,060 |
| cured, TBR | 19,720 | 17,944 | −1,776 |
| **cured, total (of 161,670 eligible)** | **106,830 (66.1%)** | **102,994 (63.7%)** | **−3,836** |
| checks | G1–G7 pass | G1, G1b, G2–G8 pass | — |

**What the −3,836 means.** Following the plan's sequence strictly costs about 3,800 tyres, because a builder now waits for a late kit instead of running a trolley whose kit is ready. This is the price of the plant's sequencing rule on this data. Kit-ready order (v42) gets more tyres, but changes the order on 17 of 20 builders.

Run it with `--run e2e_v42f --rules config/rules_v42f.yaml`.

---

## v43: calender 901 campaigns and TBR bead balance (built on v42f)

Line speed is unchanged: it is taken **only** from the routing (`CTP_Master_Routing.proc_time`, metres per minute), as in every version. The plant's 4-roll calender master (`CTP_Master_FRC`) is not read. A cross-check shows the routing is never faster than that master for any 901 item (5 TBR items are slightly slower), so no assumption is needed for 901 speeds.

### What was wrong
* **Calender 901** (shared by PCR and TBR) always ran the most urgent lot next. In v42f it ran 152 lots with 151 item changes and **66 switches between PCR and TBR**, spending 101 h on changeovers against 133 h of running.
* **TBR bead machines**: most bead items may run on any of 2403/2404/2405, but the assignment put almost everything on 2403: 258 h against 175 h on 2404 and 120 h on 2405. Examples: TBAPBD001 is allowed on all three, yet all 54 h went to 2403. TBWB004 has no routing row and is given the department pool (assumption A01), yet all 51 h went to 2403. A balancer that spreads a "hot" item existed but was switched off (`balance_above_pct: 0`).

### The fix (logic)
**901 campaigns** (only for machines listed in `campaign.machines`):
1. **Hold**: the item already on the machine keeps running while it has work ready, unless another lot is more than `campaign.window_h` (8 h) more urgent.
2. **Next item**: among the lots within 8 h of the most urgent one, prefer the **same line** as the last lot (PCR stays PCR, TBR stays TBR), then the **cheapest changeover** from the item on the machine (from the PPDS changeover matrix), then urgency.

Builders and presses are not touched, so the plan's machine and sequence still hold (G8).

**Bead balance** (only for machines listed in `balance.machines`): when an item would push its machine past `balance.above_pct` (50%) of the horizon and another allowed machine is lighter, the item is spread lot by lot over its **routing-allowed** machines, using the existing per-day balancer. Mixers are not affected, so their campaigns stay intact.

### What was changed in the code

| file | change |
|---|---|
| `engine/phase5_placement.py` | new settings `CAMP_M` / `CAMP_WIN` and a ranking-only changeover lookup `co_peek()` next to `co_charged()`; on campaign machines the hold-break rule compares urgency against the 8 h window; the next-item choice prefers same line, then cheapest changeover, then urgency. Counters `v43-campaign-break` and `v43-campaign-pick` in the run log |
| `engine/phase4b_assignment.py` | `balance.machines` / `balance.above_pct`: the existing hot-item spread is switched on for the listed machines only; prints a "Pool balance" line |
| `config/rules_v43a.yaml` (**new**) | v42f + `campaign: {machines: ["901"], window_h: 8}` |
| `config/rules_v43b.yaml` (**new**) | v43a + `balance: {machines: ["2403","2404","2405"], above_pct: 50}`; **this is v43** |

### Result

| | v42f | v43a (901 campaigns) | **v43 = v43b** (+ bead balance) |
|---|---|---|---|
| 901 PCR↔TBR switches | 66 | 16 | **16** |
| 901 item changes | 151 | 144 | 144 |
| 901 changeover hours | 101 h | 82 h | **82 h** |
| 901 last lot finishes | 2 Nov 03:14 | 1 Nov 05:18 | **1 Nov 05:18** (22 h earlier) |
| bead load 2403 / 2404 / 2405 | 258 / 175 / 120 h | 271 / 168 / 109 h | **228 / 181 / 146 h** |
| TBR day-1 trolley delay (median) | 45.4 h | 91.4 h | **45.1 h** |
| cured PCR | 85,050 | 88,128 | **88,128 (+3,078)** |
| cured TBR | 17,944 | 14,218 (−3,726) | **18,810 (+866)** |
| **cured total (of 161,670 eligible)** | **102,994 (63.7%)** | 102,346 | **106,938 (66.1%), +3,944** |
| checks G1–G8 | pass | pass | **pass** |

**Reading the result.**
* 901 campaigns alone (v43a) help PCR but hurt TBR. With fewer line switches, TBR lots on 901 waited longer, and TBR kits were already queuing on the overloaded bead machine 2403. That fails the "no line loses tyres" rule.
* Adding the bead balance (v43b) removes the TBR queue at 2403, and both lines gain. v43 wins back **all of the 3,836 tyres** that following the plan sequence cost (v42f), while keeping the plan's machine and order on every builder and press.
* 901 item changes barely fall (151 → 144). Most 901 lots are different items with no second lot of the same item ready at the same time, so the gain comes from fewer **line** switches and cheaper changeovers, not longer runs of one item.
* 2403 is still the busiest bead machine (228 h, 95% of the 240 h horizon). Only 2 items met the 50% spread condition, and some items (TBAPBD001, TBWB004's pool) still lean to 2403. A lower threshold, or routing rows adding 2404/2405 for 2403-only items, would spread it further.

Run it with `--run e2e_v43b --rules config/rules_v43b.yaml`.

---

## v43c / v43d: bigger lots on calender 901 (measured, not adopted yet)

### What was tried
901 makes only 19 items, but phase 3 cut each into about 8 small lots: buckets broke on every 8 h gap between needs, and were cut again into 24 h delivery lots. So the campaign rule rarely had a second lot of the same item ready to hold on to.

* **v43c** (`campaign.bigger_lots: true`): on 901, an item's demand is bucketed over its **whole shelf-life window** (max ageing − min ageing − 2 h, about 62–66 h), with no gap break and no 24 h cut. MPQ, the A03 8 h cap and whole units still apply, and nothing can be used after it expires.
* **v43d** (`campaign.keep_need_span: true`): the same bigger bucket, but still cut into 24 h delivery lots, so each lot is ready for its own earliest trolleys.

Code: `engine/phase3_lot_sizing.py` (switches `campaign.bigger_lots`, `campaign.lot_window_h`, `campaign.keep_need_span`; prints a "Campaign lots" line). Rules: `config/rules_v43c.yaml`, `config/rules_v43d.yaml`.

### Result

| | v38 | v43 (v43b) | v43c | v43d |
|---|---|---|---|---|
| 901 lots / item changes | 166 / 165 | 152 / 144 | **60 / 51** | 147 / 138 |
| 901 lots ≤ 2 h | 154 | 143 | **39** | 138 |
| PCR↔TBR switches | 71 | 16 | 22 | 19 |
| 901 changeover / all gaps vs production | 100 / 131 h vs 139 h | 82 / 106 h vs 133 h | **36 / 42 h vs 125 h** | 81 / 103 h vs 132 h |
| 901 is last input, PCR / TBR kits | 83% / 28% | 64% / 22% | 42% / **45%** | 71% / 21% |
| cured PCR / TBR | — | 88,128 / 18,810 | 88,496 / **13,700** | **94,938** / 18,672 |
| cured total (of 161,670) | — | 106,938 (66.1%) | 102,196 (63.2%) | 113,610 (70.3%) |
| checks G1–G8 | — | pass | pass | pass |

### What it means
* **v43c fixes the calender pattern** (60 lots, 51 changes, gaps down from 106 h to 42 h), but TBR loses 5,110 tyres. One big lot now feeds up to 66 h of trolleys and finishes only once: TBR item **CP56542** alone held up 115 TBR kits. It fails the "no line loses tyres" rule.
* **v43d keeps the 24 h cut**: the calender pattern is back to almost v43 levels (147 lots, 138 changes), yet PCR gains 6,810 tyres and TBR loses 138. The gain does **not** come from fewer 901 changeovers. 901 lots per item barely change, so it is a knock-on effect through the joint PCR/TBR dispatch. It strictly fails the rule (TBR −138).
* **New root-cause finding.** 901 is not short of machine time and is not waiting for its compound. Its lots start **60–113 h after their compound is ready** (median 94 h PCR, 69 h TBR), while 901 is idle about 103–106 h of the horizon. Something in the release logic (the lot's earliest-start gate or its consumer timing) holds 901 lots back. Bigger lots cannot fix that. This is the next thing to investigate.

---

## v43e–v43g: why 901 lots wait, progressive release, and middle-size lots

The full story of the 4-roll calender is in [FOUR_ROLL_CALENDER_901.md](FOUR_ROLL_CALENDER_901.md). In short:

* **Why 901 lots wait 2.5–4.5 days.** Not a release rule: 132 of 152 lots start straight after the previous 901 lot plus its changeover. In 240 h, 901 runs 133 h, changes over 82 h and is idle only about 24 h. It is fully booked, so lots queue. (An earlier statement that 901 was idle 104 h was wrong; those gaps were mostly changeover time.)
* **Progressive release** (`campaign.progressive_release`, `engine/phase5_placement.py`, `_progressive()`): a trolley's share of a 901 lot is ready as the calender produces it, at lot start + duration × cumulative share in need-by order. Before, it was ready only when the whole lot finished. Used in both kit checks. On v43 alone: +64 tyres (v43e).
* **Bigger 901 lots** (`campaign.bigger_lots` in `engine/phase3_lot_sizing.py`) with **`campaign.lot_window_h`**:

| run | 901 bucket | 901 item changes / changeover | cured PCR | cured TBR | total | decision |
|---|---|---|---|---|---|---|
| v43 | (v43 lot sizing) | 144 / 82 h | 88,128 | 18,810 | 106,938 | previous baseline |
| v43f | 66 h (shelf life) | 51 / 36 h | 90,984 | 13,770 | 104,754 | no: TBR −5,040 |
| **v43g24** | **24 h** | **124 / 73 h** | **93,968** | **19,158** | **113,126 (70.0%)** | **adopted** |
| v43g36 | 36 h | 85 / 49 h | 100,398 | 16,138 | 116,536 | no: TBR −2,672 |

* **Why big buckets hurt TBR.** A big lot cannot start until all its compound is mixed, and it uses 901 time early for material needed days later. TBR item CP56542's first lot moved from 22 Oct to 27 Oct.
* **Shelf life (reported only).** Total violations 385 → 350, but on 901 items 32 → 64, with a median overrun of 25.6 h. A plant check of 901 maximum ageing is worthwhile.

Run the new baseline with `--run e2e_v43g24 --rules config/rules_v43g24.yaml`.

---

## v44a: TBR bead machines queue by the trolley they serve (built on v43g24)

### What was wrong
* **Routing.** Bead **winding** (op 170, TBWB…) comes first and **apexing** (op 180, TBAPBD…) second. One apexed bead needs 2 wound bead bundles, three final compounds (A1031, A5542, B107) and chafer strip SNCT001 (calender 901 BRTTL → cutter → slitter). Both stages run on the same three machines **2403 / 2404 / 2405**. The plant register calls these machines "Bead Apexing TDA 1/2/3 TBR" and lists no TBR bead winder; that is a data question for the plant.
* **What the schedule did** (v43g24): on its first day 2403 ran **only winding** (TBWB001 → TBWB001 → TBWB004 → TBWB002 → …), and its first apexing lot started after **48.8 h**. On 2404 and 2405 the first apexing lot started after 30.6 h and 28.5 h. For **71 of the 90** TBR trolleys planned in the first 3 days, apexed bead was the last missing part. TBR built only **8.5% / 8.4% / 32.9%** of plan on days 1–3.
* **Why.** The queue ranked lots by their own due time, and a winding lot is always due earlier than the apexing lot it feeds. So winding always won, even when an apexing lot had everything it needed and was the last part for a waiting trolley.

### The fix (logic)
For machines listed in `dispatch.trolley_need_first_machines` (2403, 2404, 2405):
1. **Trolley need time.** For every lot, the engine works out the build start of the **earliest trolley it ultimately serves**, passed back from the building lots through the material links (reverse order of the chain).
2. **Queue order on those machines:**
   * first, the trolley need time, in 8 h windows (`dispatch.trolley_need_bucket_h`);
   * within a window, **the stage closest to the trolley first** (apexing before more winding);
   * then the exact need time;
   * then the lot's own due time.
3. **Winding still comes first wherever it must.** An apexing lot only enters the queue once its wound beads exist (from stock or an earlier winding lot), and the check that apexed quantity never exceeds wound quantity still runs after placement. Result: "TBR winding already leads apexing at every point".
4. **Machines come from the routing table only.** TBWB004 and TBWB008 have no machine in the routing; that is a data gap, and they stay on the flagged A01 assumption.

**First attempt (no effect).** Ranking only by trolley need time gave a schedule identical to v43g24. In the first days, the winding lots serve the **same** day-1 trolleys as the apexing lots (for example TBWB005 feeds TBAPBD008, which feeds a day-1 trolley), so it was a tie, and the winding lot's earlier due time decided it. The "closest stage first" tie-break (step 2) is what changed the order.

### What was changed in the code

| file | change |
|---|---|
| `engine/phase5_placement.py` | new block before `co_src`: `TN_M`, `tneed` (trolley need time per lot) and `tstage` (stages between the lot and the trolley), computed in reverse order of the material chain from the building-lot allocations. In `push_ready()`, lots on those machines get the queue key (need window, stage, need time, due time). Prints "Trolley need" |
| `config/rules_v44a.yaml` (**new**) | `dispatch.trolley_need_first_machines: ["2403","2404","2405"]`, `dispatch.trolley_need_bucket_h: 8` |

### Result

| | v43g24 | v44a | change |
|---|---|---|---|
| first apexing lot on 2403 / 2404 / 2405 (hours after open) | 48.8 / 30.6 / 28.5 | 44.8 / 35.1 / 28.5 | small |
| TBR trolleys planned days 1–3: median delay | 61 h | **53 h** | −8 h |
| TBR built % of plan, days 1 / 2 / 3 | 8.5 / 8.4 / 32.9 | **8.5 / 16.8 / 58.3** | days 2–3 much better |
| TBR cured on days 1–3 | 1,596 | **2,136** | +540 |
| TBR trolleys whose last part is apexed bead (all days) | 210 | 205 | −5 |
| cured TBR | 19,158 | **19,454** | +296 |
| cured PCR | 93,968 | 93,882 | **−86** |
| **cured total (of 161,670)** | **113,126 (70.0%)** | **113,336 (70.1%)** | +210 |
| checks G1–G8 | pass | pass | — |

### What it means
* **TBR days 2–3 improve a lot.** Built rises from 8.4% to 16.8% on day 2 and from 32.9% to 58.3% on day 3, because kits are finished one at a time instead of winding beads for many trolleys first.
* **Day 1 does not improve** (8.5%). The first apexing lots still cannot start before about 28 h: their compounds A1031 and A5542 are mixed only late on day 1 (around 22:30) and then need their minimum resting time. The next lever for TBR day 1 is mixing the apex compounds first, the same idea as "901 compounds first" for PCR.
* **PCR −86 tyres** through the shared mixers, a 0.09% change that strictly breaks the "no line loses tyres" rule. **Decision pending** whether to adopt v44a as the baseline.

---

## v44b: opening stock tested for "rested?" per demand (built on v44a)

### What was wrong
Phase 2 decides which opening stock can serve which demand. Stock must have rested its minimum ageing before use. The engine tested this against **one time per item: the first trolley that ever needs the item**, and applied that verdict to every later demand too.

Example, TBR compound **A1031**: the opening stock holds **4,830 kg**, mixed 03:29–04:08 on 22 Oct, so it is rested at 07:30–08:08. The first trolley needing A1031 starts at 07:07, so the stock "was not rested" and was treated as unusable **for the whole 10 days**. A1031 was mixed again from scratch (the mixers reached it only at 21:45 on day 1).

Across both lines this threw away stock that finished resting shortly after its item's first use: **PCR 31,365 units** (26,780 kg final compound, plus inner liner and calendered rolls) and **TBR 15,840 units** (11,410 kg final compound, plus squeegee, calendered rolls and shoulder pad).

### The fix (logic)
With `wip.maturity_per_demand: true`, each demand may use a stock batch that has rested **by that demand's own use time** (need-by + the item's minimum ageing, the same time a freshly made lot would have to be ready).
* The item's first-use time stays as the floor, so the earliest demands are judged exactly as before.
* Expiry is unchanged: still tested at the demand's own need-by.
* Nothing is invented and nothing starts before 07:00.

**Code.** `engine/phase2_wip_netting.py`: new helper `_draw_at(item, need_by, lot)` next to `draw_at_item`, used by the stock draw (`allocate`) and by the kit-coverage test; prints "Maturity test". Rules: `config/rules_v44b.yaml`.

### Result

| | v43g24 | v44a | v44b |
|---|---|---|---|
| opening stock used (PCR / TBR units) | 141,448 / 122,189 | 141,448 / 122,189 | **172,812 / 137,063** |
| A1031 stock used | 0 | 0 | **4,830 kg** |
| PCR built %, days 1 / 2 / 3 | 12.7 / 35.0 / 70.9 | 12.7 / 35.0 / 70.9 | 9.7 / **46.3 / 73.9** |
| PCR cured days 1–3 | 14,674 | 14,674 | **17,862** |
| TBR built %, days 1 / 2 / 3 | 8.5 / 8.4 / 32.9 | 8.5 / 16.8 / 58.3 | 8.5 / 16.8 / **23.0** |
| first TBR apexing lot (h after open, 2403 / 2404 / 2405) | 48.8 / 30.6 / 28.5 | 44.8 / 35.1 / 28.5 | 44.7 / 33.5 / 30.1 |
| cured PCR | 93,968 | 93,882 | **96,258 (+2,376)** |
| cured TBR | 19,158 | 19,454 | **16,274 (−3,180)** |
| **cured total** | **113,126** | 113,336 | 112,532 |
| checks G1–G8 | pass | pass | pass |

### What it means
* **The stock fix is correct and it helps PCR**: 31,000 more units of opening stock are used, PCR day-2 building rises from 35% to 46%, and PCR cures 2,376 more tyres.
* **It did not fix TBR day 1, and TBR lost 3,180 tyres.** The first TBR apexing lots still start 30–45 h after open. Their compounds are now ready by about 23:00 on day 1 (A5542 is the last one, since its 1,440 kg of stock is used up), but **2403 / 2404 / 2405 still run 30+ hours of bead winding first**. Example: on 2403, apexing lot TBAPBD005_L00001 has all inputs by 22 Oct 22:44 (+4 h rest) but starts only on 24 Oct 09:43, while 2403 winds TBWB001/002/004.
* **So the earlier explanation ("A1031 is the reason TBR day 1 is late") was only part of it.** The bigger reason is still the bead machines' queue on day 1: the v44a rule does not stop winding taking the machine there. TBR's loss in v44b comes from the shared machines and the bead assignment shifting (2405 now blocks 8 day-1 trolleys instead of 2).
* **Not adopted yet.** It breaks the "no line loses tyres" rule. Next: find why winding still wins on day 1 when an apexing lot is ready, fix that, then re-run with the stock fix on.

---

## Diagnostic: dispatch log on the bead machines (e2e_v44b_dbg)

**What was added.**
* `engine/phase5_placement.py`: with `debug.log_machines` set, every pick on those machines writes one row per queued lot: queue key, trolley need, stage to trolley, when the lot is ready, which sequencing rule removed it, campaign hold, and the pick. Output: `outputs/<run>/DISPATCH_LOG_debug.csv` (last placement round).
* `engine/make_dispatch_log_workbook.py` (**new**) builds `DISPATCH_LOG_2403_2405.xlsx` with sheets Read me, Summary, Decisions (with **booked_ahead_h**), Losses and Candidates.
* Run with `config/rules_v44b_dbg.yaml`. The schedule is identical to v44b (112,532 cured).

**What it shows** (118 picks on 2403/2404/2405):

| measure | value |
|---|---|
| picks that were the best ready lot by queue key | **112** |
| picks decided by campaign hold | 0 |
| picks decided by a tie-break | 6 |
| ready lot for an earlier trolley removed by the once-per-shift rule (BRC-02) | 20 rows |
| **picks booked more than 1 h ahead of the decision** | **110** |
| median / max hours booked ahead | **36.9 h / 108.7 h** |

**The cause.** It is not a ranking rule. The dispatcher **wakes a machine while it is still busy and books its next lot straight away**, for a start many hours later, choosing only among the lots ready at that moment. Example, 2403:
* At 22 Oct 21:15 it booked three winding lots: TBWB004_L00003 (starts 23 Oct 07:50), TBWB004_L00004 (13:27) and TBWB002_L00005 (19:24). They serve trolleys on **25 Oct**.
* At 23 Oct 02:54, when the day-1 apexing lots became ready, it picked them, but 2403 was already booked to 24 Oct. So TBAPBD005_L00001 (for a **22 Oct 15:00** trolley) starts on **24 Oct 09:43**, booked 30.8 h ahead.

The queue key (v44a) is applied correctly, but a lot that becomes ready later can never get in front of work booked earlier. **Next fix:** a machine still busy is re-queued at its free time instead of booking its next lot early, so the choice is made when the machine is actually free.

---

## v44c: a machine chooses its next lot only when it is free (built on v44b)

### What was wrong (in simple words)
Think of a machine as a barber with a waiting room. Every time new work became ready, the engine woke the machine, **even while it was still busy**, and the machine booked its next job straight away, for a slot hours later, choosing only among the jobs already waiting. The appointment book filled up early. A job that arrived later, however urgent, went to the back.

On 2403–2405 in v44b, **110 of 118** picks were booked ahead of time (median **36.9 h**, max 108.7 h). Example: at 22 Oct 21:15, 2403 booked three winding lots for 25 Oct trolleys. The day-1 apexing lot TBAPBD005_L00001 (for a 22 Oct 15:00 trolley) became ready at 23 Oct 02:54 but could only start on 24 Oct 09:43.

### The fix (logic)
With `dispatch.choose_when_free`, a machine that is woken while busy is **not** asked to choose. It is put back in the queue at the time it becomes free, and at that moment it chooses among **everything ready then**. The queue order itself (v44a: trolley need, stage closest to the trolley, then deadline) is unchanged; it is just used at the right moment.

Unchanged:
* winding still comes before the apexing that needs it;
* nothing starts before 22 Oct 07:00;
* one lot at a time per machine;
* builders and presses keep the plan's machine and order (G8).

Two versions:
* **v44c-all** (`choose_when_free: all`): every upstream machine.
* **v44c-bead** (`choose_when_free: ["2403","2404","2405"]`): bead machines only.

### What was changed in the code

| file | change |
|---|---|
| `engine/phase5_placement.py` | right after a machine is taken from the clock (`t, m = heapq.heappop(clock)`): if the machine is in the choose-when-free set and still busy (`m_free > t`), it is re-queued at `m_free` and skipped; settings `CWF_ALL` / `CWF_M` next to `m_free`; prints "Choose when free" |
| `config/rules_v44c_all.yaml`, `config/rules_v44c_bead.yaml` (**new**) | v44b + the setting (+ dispatch log on 2403–2405) |

### Result

| | v43g24 (baseline) | v44a | v44b | **v44c-all** | v44c-bead |
|---|---|---|---|---|---|
| bead picks booked > 1 h ahead / median hours ahead | — | — | 110 of 118 / 36.9 h | **15 of 118 / 0.5 h** | 15 of 118 / 0.5 h |
| first apexing lot, 2403 / 2404 / 2405 (h after open) | 48.8 / 30.6 / 28.5 | 44.8 / 35.1 / 28.5 | 44.7 / 33.5 / 30.1 | **18.2 / 17.5 / 18.0** | 18.2 / 17.5 / 18.0 |
| TBR built %, days 1 / 2 / 3 | 8.5 / 8.4 / 32.9 | 8.5 / 16.8 / 58.3 | 8.5 / 16.8 / 23.0 | **8.5 / 54.5 / 112.8** | 8.5 / 42.5 / 115.6 |
| PCR built %, days 1 / 2 / 3 | 12.7 / 35.0 / 70.9 | 12.7 / 35.0 / 70.9 | 9.7 / 46.3 / 73.9 | 4.9 / **50.9 / 73.9** | 9.7 / 46.3 / 73.9 |
| cured PCR | 93,968 | 93,882 | 96,258 | **96,808 (+2,840)** | 96,420 (+2,452) |
| cured TBR | 19,158 | 19,454 | 16,274 | **23,506 (+4,348)** | 23,106 (+3,948) |
| **cured total (of 161,670 eligible)** | **113,126 (70.0%)** | 113,336 | 112,532 | **120,314 (74.4%)** | 119,526 (73.9%) |
| shelf-life violations (reported) | 350 | 311 | 355 | **219** | 223 |
| checks G1–G8, G12 | pass | pass | pass | **pass** | pass |

### What it means
* **This is what unlocks the TBR cold start.** With no early booking, the first apexing lots start about **18 h** after open (they were at 28–49 h). TBR day 2 goes from 8.4% to 54.5% built, and day 3 to 112.8% (catching up). TBR cures **4,348 more tyres** than the v43g24 baseline.
* **It also lets the v44b stock fix pay off.** v44b alone lost TBR tyres because the bead machines were booked up. With choose-when-free, the stock fix and the bead queue rule both work.
* **Both lines gain against v43g24**, so the "no line loses tyres" rule is met. **v44c-all** is better than v44c-bead on both lines (PCR +388, TBR +400 more), so choosing when free helps the other machines too.
* **TBR day 1 is still 8.5%.** The first apexing lots are ready about 18 h after open, after day 1's first trolleys. That is the limit set by mixing the apex compounds (A5542 stock is used up) plus their 4 h rest. PCR day 1 drops to 4.9% in v44c-all, but days 2–3 and the total are higher.
* **Shelf-life violations fall** from 350 to 219, because material is made closer to when it is used.

**v44c-all is the new baseline: 120,314 tyres (74.4%).**

The whole TBR apexed-bead story (v43 → v44c) and its remaining issues are in [TBR_APEXED_BEAD.md](TBR_APEXED_BEAD.md).

---

## v45a: aggregate bead gate off, new check G1u (PCR correctness)

### What was wrong
PCR day 1 (v44c-all) built only 4.9% of plan. For 16 of the 40 day-1 trolleys the last missing part was bead apexing (machines 201/202). The cause was the **bead gate** (`_bead_gate` in `phase5_placement.py`):
* it compared **total apexed** against **total newly wound** quantity for the whole line, across all items and units;
* it **ignored wound-bead opening stock** (PCR draws 18,819 wound-bead units from stock);
* so apexing lots fed from stock were slid later to wait for unrelated winding. BA14006, with all inputs from stock and ready at 07:00, started at 17:56 (v44c-all log: "PCR 163 apex lots held for winding, max +19.5 h");
* it moved a lot **without moving the lot behind it on the same machine**: **81 overlapping lots on 201 / 202** in v44c-all. Check G1 only covered builders, so this was not caught.

### The fix (logic)
* Each apexing lot already waits for **its own** winding lots through the material links (precedence edges: e.g. BA15004 waits for BW15005), so the aggregate gate adds only false waits. `bead_gate.enabled: false` switches it off.
* New check **G1u** (fatal): no upstream machine may run two lots at once. Measured on the written `phase5_schedule.csv`.

### Code
* `engine/phase5_placement.py`: the `_bead_gate(sched)` call runs only when `bead_gate.enabled` is true (default true; false from v45a).
* `engine/make_validation_report.py`: check G1u; added to the fatal list.
* `config/rules_v45a.yaml` (**new**).

### Result

| | v44c-all | v45a |
|---|---|---|
| overlapping lots on upstream machines (PCR 201 / 202) | **81** (33 / 48) | **0** |
| first lot on 201 / 202 | 17:56 / 18:30 | **07:00 / 07:00** |
| PCR trolleys starting earlier | — | 44 |
| cured PCR / TBR / total | 96,808 / 23,506 / 120,314 | 96,808 / 23,506 / **120,314** (unchanged) |
| checks | G1u would fail | **G1, G1b, G1u, G2–G8 pass** |

A correctness fix. The tyre count does not change because the trolleys that now get their beads earlier still wait for their calender 901 parts. **v45a is the new baseline.**

---

## v45b: calender 901 queues by the trolley each lot serves (trial)

### What was wrong
901 (shared by PCR and TBR) picked by due time. TBR lots carry **earlier due times** because their chains after 901 are longer (cutters, bead, belts…), so 901 ran almost only TBR lots until 23 Oct 12:26. PCR's **ECOPOLY350**, the last part for **13 of the 40** PCR day-1 trolleys, waited (its compound B350 was ready at 23 Oct 00:00).

### The fix (logic)
The v44a rule, extended to 901: `dispatch.trolley_need_first_machines: ["2403","2404","2405","901"]`. On 901, lots queue by the start time of the trolley they serve (8 h windows), closest stage first, then due time. PCR and TBR lots then compete on real trolley need, not on chain length.

Rules: `config/rules_v45b.yaml`. No new code.

### Result

| | v45a | v45b | change |
|---|---|---|---|
| first PCR lot on 901 | 22 Oct 09:53 | 22 Oct 07:00 | earlier |
| PCR built %, days 1 / 2 / 3 | 4.9 / 50.9 / 73.9 | **8.1 / 49.2 / 103.9** | days 1 and 3 up |
| TBR built %, days 1 / 2 / 3 | 8.5 / 54.5 / 112.8 | 8.5 / 47.8 / 118.4 | day 2 down |
| cured PCR | 96,808 | **103,234** | **+6,426** |
| cured TBR | 23,506 | 22,232 | **−1,274** |
| **cured total** | 120,314 (74.4%) | **125,466 (77.6%)** | **+5,152** |
| checks | pass | pass | — |

### What it means
* PCR gains a lot (+6,426): its 901 items are no longer pushed behind TBR lots that only *look* more urgent.
* TBR loses 1,274: 901 time moves from TBR to PCR. This strictly breaks the "no line loses tyres" rule. **Decision pending.**
* PCR day 1 is still low (8.1%). ECOPOLY350's first lot now runs only at 23 Oct 17:48, because 901 now serves day-1 PCR items in trolley order and ECOPOLY350's compound (B350) is mixed late on day 1. The next lever is **compounds for the bottleneck first** on the mixers (B350 for 901, S53 for PCR bead winding, A1031/A5542 for TBR apexing).

The PCR cold-start story is collected in [PCR_COLD_START.md](PCR_COLD_START.md).

---

## v46a: cut and slit stock counted as the material it was cut from

### What was wrong
The cutters and slitters are bypassed: they are not scheduled, and material goes from the calender or extruder straight to the builder. But the plant's racks still hold material that was **already cut or slit**: belts, plies, chafers, cap strips. On 22 Oct that was 40,137 m on PCR and 30,744 m on TBR.

Until now (v42 rule `cold_start.net_cut_stock`) that stock could only be used against **its own cut code**. In the schedule, nothing is made on a cutter, so the cut code isn't a real step. What the schedule makes and waits for is the **mother material** underneath: the calendered roll, the edge strip and so on.

### The fix (logic)
1. **When the opening stock is read**, every stock batch of a cut or slit code is turned into the **child material it was cut from**. That child stock is then used like any other stock.
2. **Quantity.** The bill of materials gives quantities per tyre, so 1 m of cut code = `child_quantity / Parent_qty` m of child. For a bias-cutter item it is `child_quantity`, because rule BR-5 states those per unit of cutter output.
   - **Codes the plant doesn't make are walked through**, the same way the engine's own BOM walk does. Another bypassed code, or a pre-cut fabric such as BCFT002, is followed down until an item the plant makes is reached. Example: a PCR slit item goes BRTTL SLT → BCFT002 → BRTTL calendered roll.
   - **Different SKUs can reach the cut code by different paths.** The path most SKUs share is used, with the median ratio. Paths are never added together, so nothing is counted twice.
   - **A cut belt that carries an edge strip gives both:** for example B1-PBLT001 → CPJ120 + EG01.
3. **Time.** Converted stock is usable from production time + **the cut code's own minimum ageing**. That is the transfer time across the bypassed cutter or slitter. It also keeps the cut code's maximum ageing.
4. **When conversion isn't possible**, the batch stays on its cut code and is used the old way.
   - The only case: TBR chafers **SNCT001–003** (36 batches, 7,293 m). Stock is in metres, but the bill of materials states them in square metres, and the width needed to convert isn't in the data. The routing also lists 1 input where the BOM lists 3.
   - These are listed as a data gap.
5. **Transfer time for newly made material is unchanged.** The rule `bypass_transfer_from_min_aging` (on since v34) adds the bypassed code's minimum ageing to the material below it. This version **writes it out and checks it** on every link from a lot to the trolley it feeds.
   - PCR: 23 items get +4 h.
   - TBR: 0 h. Every TBR cut code has 0 minimum ageing in the ageing master and in the stock rows, so the rule is applied but the data gives it nothing to add.

### Code
| File | Change |
|---|---|
| `engine/phase2_wip_netting.py` | new `bypass_stock_to_children()` (runs right after `load_wip`, before the stock pool is built); `build_pool` takes the converted row's transfer time (`bypass_min_h`) and tags its batch `…<CUT:<code>`; the A-WIP-1 "absent item type" test uses the stock types as delivered; writes `phase2_bypass_stock_conversion.csv` and `phase2_bypass_transfer.csv` |
| `engine/make_bypass_report.py` | new: `BYPASS_REPORT_<run>.xlsx` (conversion, use, transfer times, the two checks) |
| `config/rules_v46a.yaml` | `wip.bypass_stock_to_children: true` (inherits v45b) |

### Result

| | v45b | v46a | change |
|---|---|---|---|
| cut/slit batches converted (PCR / TBR) | — | 227 / 168 | — |
| child stock created (PCR / TBR) | — | 8,230 m / 5,271 m | — |
| converted stock used by the schedule | — | 97% PCR / 99% TBR | — |
| PCR built, days 1 / 2 / 3 | 4,365 / 7,875 / 12,839 | **4,658 / 9,324 / 13,414** | +293 / +1,449 / +575 |
| TBR built, days 1 / 2 / 3 | 402 / 1,855 / 3,295 | 402 / 1,855 / 3,295 | same |
| cured PCR | 103,234 | **105,358** | **+2,124** |
| cured TBR | 22,232 | **22,808** | **+576** |
| **cured total** | 125,466 (77.6%) | **128,166 (79.3%)** | **+2,700** |
| schedule checks | 21 pass | 21 pass | — |
| transfer-time breaches (lot → trolley; converted stock used too early) | not measured | **0 / 0** | — |

### Notes
* **An earlier v46a attempt on the same day showed 129,564 tyres.** That attempt converted every child row of a cut code at once, which created 15,768 m of phantom pre-cut fabric on TBR and could double-count. The corrected version above is the one to use.
* **How the transfer check measures 901 lots.** Progressive release (v43g) frees each trolley's share of a 901 lot as it is made, so the check measures from that share, not from the end of the lot. Measured that way, every link has at least the 8 h required (4 h own rest + 4 h transfer).
* **Measured on a different basis from v45b.** v45b's "cut stock used" (76% PCR / 97% TBR) was measured in cut-code metres, while v46a measures child metres used, so the two percentages don't compare directly.

---

## v46b: TBR TDA winds and apexes in one go

### What was wrong
The TBR TDA machines (2403 / 2404 / 2405) take **bead wire + compound in** and give an **apexed bead out**, on one machine; the plant register confirms it. The routing lists two steps: bead winding (op 170) and apexing (op 180). Up to v46a the engine therefore booked **two separate lots** on the same machine: first a winding lot, then later an apexing lot.

So it counted winding time **on top of** apexing time, plus changeovers between the two and the wait in between. In v46a that was 41 winding lots, 207 h of run time and 28 h of changeover.

### The fix (logic): user decision, option 1
On the TDA, winding and apexing run **in parallel**, so a bead takes as long as the **slower** of the two.
1. **TBR bead winding is skipped**, like the cutters and slitters: it's added to the bypassed departments. No winding lot is made. Bead wire 1040S28600 and compound S54 go straight into the apexed bead lot.
2. **Each apexed bead item runs at the slower of two speeds**: its own apexing speed, and the speed of the winding item its routing row names. In this data apexing is always the slower (3 beads/min; TBAPBD001 2.034/min) against winding at 3–4/min. TBWB008 has no winding speed in the routing, so the apexing speed is used.
3. **Changeovers** on 2403–2405 now come only from the TDA apexing tables.
4. **PCR is unchanged.** It has a separate winder (1201, department "BEAD WINDING PCR") and separate apexers (201/202).

### Code
| File | Change |
|---|---|
| `engine/ctpio.py` | new `_tda_inline()`, called at the end of `build_routing_meta`, so every phase reads the same speed; the table it uses is kept in `TDA_INLINE` |
| `engine/phase3_lot_sizing.py` | writes `phase3_tda_inline.csv` (per item: apexing speed, winding item and speed, speed used, which step is slower) |
| `config/rules_v46b.yaml` | `bypass_departments` + "BEAD WINDING" (the whole list is repeated, because lists are not merged); `tda_inline: {enabled, apex_departments, winding_departments}` |

### Result

| | v46a | v46b | change |
|---|---|---|---|
| lots on 2403–2405 | 118 (41 winding + 77 apexing) | **77** (apexing only) | −41 |
| run time / changeover on 2403–2405 | 508.8 h / 67.3 h | **301.5 h / 43.3 h** | **−231 h** |
| first apexed bead lot finishes | 23 Oct 01:12 | **22 Oct 16:43** | 8.5 h earlier |
| TBR built, days 1 / 2 / 3 / 4 | 402 / 1,855 / 3,295 / 3,549 | **700 / 1,979 / 3,438** / 3,477 | day 1 +298 |
| PCR built, days 1 / 2 / 3 / 4 | 4,658 / 9,324 / 13,414 / 11,608 | 4,396 / 9,586 / 13,312 / 11,710 | day 1 −262 |
| cured PCR | 105,358 | 105,422 | +64 |
| cured TBR | 22,808 | **23,144** | **+336** |
| **cured total** | 128,166 (79.3%) | **128,566 (79.5%)** | **+400** |
| schedule checks | 21 pass | 21 pass | — |

### What it means
* TBR beads now come much earlier. The first apexed beads are ready on the afternoon of day 1 instead of overnight. TBR day 1 rises from 402 to 700 tyres, and TBR total is now close to v45a (23,144 vs 23,506, still 362 below).
* **TBR day 1 is still held mainly by pre-assembly** (TPA-004 / 002 / 020 on 2201) and the belt edge filler (BEF001). Those are the next levers.
* **PCR day 1 drops by 262 tyres** (total +64). PCR and TBR share the mixers. With TBR beads ready earlier, TBR compounds take mixer time earlier on day 1.

---

## v46c: same as v46b, with the BOM from `CTP_Master_BOM_v2` (trial)

### What changed
* **Data folder `data-ppds-v38-bomv2`** is `data-ppds-v38` with **only** `jkt_bom_pcr/tbr` replaced, read straight from `JK_CTP_PPDS.CTP_Master_BOM_v2`.
  - v2 already skips the cutters and slitters: the green tyre links straight to the calendered roll.
  - Where two belts come from one roll, v2 has two rows (e.g. CPJ1218 0.231 + 0.216 m). They are **kept and added together**. The old staging step drops repeated rows, so it is **not** used here; that would lose a belt.
* **Cutters / slitters: only the transfer time remains.**
  - v2 never names a cut code, so the codes between a roll and the tyre are found from the **routing**: every cutter or slitter row names its input (`bypass_transfer_parents_from_routing`).
  - Their minimum ageing is the transfer time. A cut code's type sets its default rest (BR-4), so the type is read from the pre-v2 BOM for the bypassed codes only and only for the line being scheduled (`bypass_cut_code_types_from`).
  - Result is the same as v46b: PCR 23 rolls +4 h, TBR 0 h.
* **Cut stock in the opening stock still needs a "roll metres per belt metre" factor**, which v2 no longer carries.
  - It is taken from the pre-v2 BOM **as a conversion factor only** (`wip.bypass_conversion_bom`), and only onto items v2 still has. This is a stated assumption.
  - Same as v46b: 8,230 m PCR / 5,271 m TBR; 0 transfer-time breaches.
* Everything else is v46b: TDA in line, rules, plan.

### Code
| File | Change |
|---|---|
| `engine/ctpio.py` | `_bypass_parents` can also read bypass parents from the routing; `apply_aging_defaults` can fill the types of bypassed codes from a reference BOM (this line only) |
| `engine/phase2_wip_netting.py` | `bypass_stock_to_children` takes a reference BOM for the conversion factor of cut codes the run's BOM doesn't contain; each converted row records `ratio_source` |
| `config/rules_v46c.yaml` | inherits v46b; data folder; the three settings above |

### Result

| | v46b | v46c | change |
|---|---|---|---|
| gross component demand PCR / TBR | 2,684,560 / 2,422,168 | 2,506,392 / 2,157,242 | −7% / −11% |
| PCR built, days 1 / 2 / 3 | 4,396 / 9,586 / 13,312 | 5,031 / 11,950 / 12,924 | |
| TBR built, days 1 / 2 / 3 | 700 / 1,979 / 3,438 | 700 / 2,559 / 3,174 | |
| cured PCR | 105,422 | 111,280 | +5,858 |
| cured TBR | 23,144 | 23,196 | +52 |
| **cured total** | 128,566 (79.5%) | **134,476 (83.2%)** | +5,910 |
| schedule checks | 21 pass | 21 pass | — |

### Why the numbers moved: the result is NOT a like-for-like gain

**1. The old BOM was counted twice through the cut codes (an engine bug v2 avoids).**
- When one roll feeds two cut codes (belt 1 + belt 2, or left + right chipper), the walk entered the roll's recipe **once per cut code**, and each time took the material of **both** belts.
- Example, SKU 1D25215013008SXC10: B460 is 0.2385 + 0.2231 = **0.4615 kg per tyre**. v46b used **0.923**.
- **How often the engine's per-tyre quantity differs from the BOM row sum:**

| | v46b (old BOM) | v46c (v2) |
|---|---|---|
| PCR | 87 of 918 (SKU, item) pairs, most at 2× | 4 |
| TBR | 136 of 785 (2× to 5×) | **42** |

- The TBR cases left in v46c are **B186 and B333 at 2×** in 21 SKUs, plus some at 1.4×. Those compounds are still reached through two parents.
- **So v46b and every earlier version overstated these compounds.** Most of v46c's PCR gain is that over-count going away. The proper fix is in the engine's walk: enter each item's recipe once, whatever the BOM. It has not been made yet.

**2. v2 also removes steps that aren't cutters or slitters (a data issue).**
- **TBR nylon doubling NYDBCHF** (TBR DOUBLING 2501/2502): 21,671 m in v46b, gone in v2 together with the chafer fabric under it. BRTTL drops 61%.
- **TBR inner-liner squeegees SQ-001…022** (CALENDARING 1102/1901): 42,803 m in v46b. In v2 the compound B99 hangs straight under the inner liner, so the calendering step disappears.
- These are real made steps on real machines. Without them, TBR is **understated** in v46c.
- PCR is clean: apart from cut codes, v2 drops only 4 unrouted items with no demand.

### Open
* **Fix the walk** (enter each item once) and re-run on both BOMs. Only then can the old BOM and v2 be compared fairly.
* **The BOM v2 owner needs to confirm** whether NYDBCHF and the SQ squeegees were meant to be removed. They aren't cutters or slitters.
* Until both are settled, **v46b stays the reference.** v46c's 134,476 mixes a real correction (the PCR double count) with missing TBR steps.

---

## v46d: each item's recipe counted once (engine fix), on BOM v2

### What was wrong
The engine stores the BOM per SKU as "parent → list of its rows", and that list holds the rows of **every path** the item appears on.

When the walk reached an item, it entered that list **once per parent that led to it**. An item reached by two parents therefore had its whole recipe added twice. Three parents meant three times, and so on.

- On the old BOM this hit belts cut from one roll (B460 at 2×, and so on).
- On BOM v2 it still hit TBR compounds that two components share: **B333 at 2×** and **B186 at about 1.4×**. Before the fix, 374 TBR (SKU, item) pairs on v2 didn't match the BOM.

### The fix (logic)
Each item's recipe is entered **once**, whichever parent reaches it first. The rows already carry the quantities of all paths, so one pass gives the correct total.
- This applies to made items and to bought / bypassed items (`bom.walk_each_item_once: all`).
- Bias-cutter items (BR-5) keep the old way, because their children scale with each path's output.
- The rule is off unless set, so earlier versions are unchanged.

### Check
For every SKU and item, the engine's per-tyre quantity was compared with the plain sum of the BOM rows:

| | before the fix (v46c) | v46d |
|---|---|---|
| TBR pairs that don't match | 374 (offline) / 42 in the plan | **0** |
| PCR pairs that don't match | 36 (offline) / 4 in the plan | 34 (offline) / 4 in the plan |

The 34 PCR pairs left are a **BOM v2 data gap, not the engine**. In 34 SKUs, belt 1's edge strip (EG02 or EG03) still hangs under the old slit code EGSL02 / EGSL03, but v2 no longer links that code to the tyre. Belt 2's strip is linked directly, so only half the edge strip is counted. TBR has no unreachable rows.

### Code
| File | Change |
|---|---|
| `engine/ctpio.py` | `WalkCache`: new `once_made` / `once_contracted` options (rule `bom.walk_each_item_once`: `made` or `all`); an item already entered is not entered again |
| `config/rules_v46d.yaml` | inherits v46c (BOM v2); `bom.walk_each_item_once: all` |

### Result

| | v46b (old BOM) | v46c (BOM v2) | v46d (BOM v2 + fix) |
|---|---|---|---|
| gross component demand PCR | 2,684,560 | 2,506,392 | 2,506,392 |
| gross component demand TBR | 2,422,168 | 2,157,242 | **2,091,651** (−65,591: B333 / B186) |
| PCR built, days 1 / 2 / 3 | 4,396 / 9,586 / 13,312 | 5,031 / 11,950 / 12,924 | 4,958 / 12,437 / 11,008 |
| TBR built, days 1 / 2 / 3 | 700 / 1,979 / 3,438 | 700 / 2,559 / 3,174 | 700 / 2,559 / 3,034 |
| cured PCR | 105,422 | 111,280 | **112,444** |
| cured TBR | 23,144 | 23,196 | **24,026** |
| **cured total** | 128,566 (79.5%) | 134,476 (83.2%) | **136,470 (84.4%)** |
| checks / bypass report | pass / 0 breaches | pass / 0 | pass / 0 |

### What it means
* **TBR now uses exactly the material BOM v2 states.** Before, it carried 65,591 extra units of B333 / B186 to mix, and those took shared mixer time.
* **PCR gains 1,164 tyres** even though its own demand is unchanged. It uses the mixer time TBR no longer needs.
* **TBR is now above v45a** (24,026 vs 23,506).
* **Still open on BOM v2 (data owner):**
  - TBR nylon doubling NYDBCHF and inner-liner squeegees SQ-0xx are missing. They are real steps, so TBR is still somewhat understated.
  - 34 PCR SKUs have the belt-1 edge strip unlinked (EGSL02 / EGSL03).
* **The old BOM still carries the double-count** in v46b and earlier, because the fix is only switched on from v46d. Turning it on for the old BOM would correct it there too, but you asked for v2 only.

---

## v46e: 4-roll calender (901) lots in whole dips

### What was wrong
901 lots were cut by time: one lot per 24 h of need (v43g24). That gave 127 lots and 125 item changes on 901 in v46d, with **69.9 h of changeover against 117 h of running**.

The plant runs the calender **in dips**. One full dip of steel cord or fabric has a fixed length, listed in `JK_CTP_PPDS.CTP_Master_FRC` ("Spool/Dip Length"). For example: CPJ1218 / CPJ120 / CPJ370 12,600 m, CWBP60 6,400 m, fabrics (ECOPOLY350, HTPOLY350, BRTTL…) 2,000 m, MONOCHAFER 1,200 m.

### The fix (logic)
1. A 901 lot opens at the first need not yet covered and may serve only the needs inside the item's **shelf window** (max ageing − min ageing − 2 h, e.g. CPJ1218: 96 − 4 − 2 = 90 h).
2. **If the needs in that window add up to one dip or more:** the lot is the largest **whole number of dips** those needs use up. All of it is used before it expires.
3. **If they add up to less than one dip:** the lot is a **partial dip** of exactly that need, but never below the minimum run (300 m). No dip is run just to expire. This was your decision for low-demand items.
4. **How often an item runs is not fixed.** It follows from dip ÷ daily need:
   - CPJ1218: 1 dip about every 2 days;
   - ECOPOLY350: 4–6 dips every 2–3 days;
   - CPJ370: small partial dips.
5. **Fallbacks:**
   - Items without a dip length in FRC (HTPOLY2000, NYLRAD, PLYTEC, POLYESTER, …) keep v46d sizing.
   - Codes are matched without spaces ("CPJ 1218" = CPJ1218). MONOCHAFER is mapped to FRC's MONOCHAFFER.
6. **Line speed** still comes from the routing only. FRC's line speeds agree, apart from small differences on some TBR steel cords.

### Code
| File | Change |
|---|---|
| `engine/phase3_lot_sizing.py` | new `_dip_length` (reads the FRC file) and `_dip_lots`; `size_item` hands 901 items with a dip length to them; writes `phase3_4rc_dip_lots.csv` |
| `data-ppds-v38-bomv2/CTP_Master_FRC.csv` | staged from `JK_CTP_PPDS.CTP_Master_FRC` (is_current = 1, 48 rows) |
| `config/rules_v46e.yaml` | `fourrc_dip: {enabled, machines: ["901"], frc_file, aliases}` |

### Result

| | v46d | v46e | change |
|---|---|---|---|
| 901 lots (both lines) | 127 | **65** | −62 |
| 901 item changes | 125 | 65 | −60 |
| 901 run time / changeover | 117.1 h / 69.9 h | 112.1 h / **36.3 h** | −34 h changeover |
| PCR lots: whole dips / partial | — | 14 / 27 | |
| TBR lots: whole dips / partial | — | 9 / 15 | |
| PCR built, days 1 / 2 / 3 | 4,958 / 12,437 / 11,008 | 4,396 / 10,167 / **15,937** | days 1–2 down, day 3 up |
| TBR built, days 1 / 2 / 3 | 700 / 2,559 / 3,034 | 700 / 2,559 / 3,315 | |
| cured PCR | 112,444 | **115,384** | +2,940 |
| cured TBR | 24,026 | 24,034 | +8 |
| **cured total** | 136,470 (84.4%) | **139,418 (86.2%)** | **+2,948** |
| checks | 21 pass | 21 pass | — |

### What it means
* **901 spends half as long changing over** (−34 h), and that time goes to production: PCR +2,940.
* **PCR days 1–2 dip slightly** (−562 / −2,270), because one long dip run finishes later than the first of several small lots. Day 3 more than makes up for it (+4,929). Progressive release (v43g) still frees each trolley's share as it is made.
* **Partial dips are common (42 of 65 lots).** Most are on low-demand steel cords (CPJ120, CPJ370, CPJ121, CWBHTCP100, CP31151, CWBHI34, CWCH30256), where one full dip lasts 5–28 days but the shelf life is 3–4 days.
* **Limitation: each line is sized separately.** Rolls used by both lines (CWBHTCP100, BRTTL) get their own runs per line, not one shared dip.

---

## The checks run on every run (Schedule checks sheet of `VALIDATION_REPORT.xlsx`)

| check | what it tests | stops the run if it fails? | v42 |
|---|---|---|---|
| G1 | no two trolleys on one builder at the same time | yes | pass |
| G1b | every product change on a builder has its setup (from v42) | yes | pass |
| G2 | every trolley's duration = run time + applied setup (= plan duration until v41) | yes | pass |
| G3 | every press SKU change has its changeover time | yes (from v40) | pass |
| G4 | no lot ID used in both lines | yes (from v40) | pass |
| G5 | no lot on a press it is not allowed on | not measured here (audit script does it) | — |
| G6 | data-blocked demand taken out of the figures and listed | yes | pass |
| G7 | every assumption has a code and a source | yes | pass |
| G8 | building and curing keep the plan's machine and order (from v42f) | yes, when `building.enforce_plan_sequence` is on | building fails in v42 (kit-ready order), passes in v42f; curing passes in both |
| G12 | no green tyre waits too long before curing | no | pass (max 85.5 h) |

Separately, phase 5 itself stops before writing the building plan if G1 or G2 would fail.

---

## Known open items (not yet changed)

| item | what is wrong | planned fix |
|---|---|---|
| Shared machines | PCR and TBR share mixers and calender 901 in one placement run; a change that helps one line can cost the other (seen in 6b, 6c and v43a) | partly eased in v43 (901 line blocks); kit-first can be retried on v43 |
| TBR bead 2403 | still the busiest bead machine after v43 (228 h, 95% of horizon) | lower `balance.above_pct`, or routing rows adding 2404/2405 for 2403-only items |
| Presses wait behind late lots | press re-sequencing is ruled out by the plant's sequence rule (G8); the lever is faster green-tyre supply in plan order | v44 (upstream priority follows plan order) |
| Exit code 1 at the end of `run.py` | the machine-coverage check fails because the bypassed cutters and slitters are idle; the TBR per-line workbook doesn't reconcile. Both date from v38 | not caused by these changes |
| Other workbooks | some sheets still use the full plan, not eligible demand, as the denominator | to update |

Next: v43 tuning (campaign window, bead threshold, retry kit-first), then v44 (upstream priority follows the plan order on each builder), v45 (small SKUs, compound buffer, report labels), v46 (end of horizon, per-shift replan, fresh data).

---

## How to run a version

```bash
cd pns-ctp-scheduler
python run.py --pcr "plan-latest/ctp_building_schedule_October_2026_pcr 26.xlsx" \
              --tbr "plan-latest/ctp_building_schedule_October_2026_tbr 27.xlsx" \
              --pcr-curing "plan-latest/ctp_curing_schedule_October_2026_pcr 26.xlsx" \
              --tbr-curing "plan-latest/ctp_curing_schedule_October_2026_tbr 27.xlsx" \
              --run e2e_v45a --rules config/rules_v45a.yaml --data data-ppds-v38 --plan-dir plan-e2e_v45a
```

Outputs: `outputs/e2e_v45a/` (current baseline). Start with `VALIDATION_REPORT.xlsx`, then `CTP_e2e_v45a_SCHEDULE.xlsx`.
