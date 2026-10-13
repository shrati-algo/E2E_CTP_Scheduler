# Business Rules Log: CTP schedule versions

This log records every business rule applied to the CTP end-to-end schedule. Each rule produces a new schedule version.
Versions are cumulative: **vN = v(N-1) + the rule(s) logged under vN.**
Each version keeps its own folder (`latest_schedule/vN/`) with the run output, log and template-format Excel. Earlier versions are never overwritten.

---

## Common inputs (all versions unless a version says otherwise)

| Input | Source |
|---|---|
| Curing and building plans | `latest_schedule/data/ctp_{curing,building}_schedule_September_2026_{pcr,tbr} 10.xlsx`. Window 28-Sep 07:00 → 08-Oct 07:00. Cure plan: PCR 110,532, TBR 27,298. |
| Master tables | All 13 tables in `INPUT_TABLES_30SEP_TRUE/01_DB_tables/`: BOM, routing, aging, MPQ, changeover, building allowable, cycle times, GT inventory maps, downtime |
| Local masters | `INPUT_TABLES_30SEP_TRUE/03_local_masters/`: item type, PCR MPQ, aging fallback, config |
| Opening WIP | `JK_CTP_PPDS.CTP_Opening_WIP`, snapshot **28-Sep 06:30** (for the 07:00 plan start): 11,523 rows, 6,371 green tyres used |
| Output format | `output-format/CTP_29SEP-06OCT_SCHEDULE.xlsx`, filled by `make_schedule_from_template.py` |

---

## v0: Baseline (02-Oct plant holiday)

| | |
|---|---|
| Date | 2026-09-30 |
| Rule | **02-Oct is a plant holiday.** No production on any machine from 02-Oct 07:00 to 03-Oct 07:00. |
| How | A HOLIDAY window for all 222 machines (166 presses, 20 TBMs, 36 upstream machines) added to the working copy `db_pipeline/raw/jkplanning_CTP__downtime_reason.csv`. The scheduler's PM/downtime rule (BR-10) blocks those windows. The original table in `INPUT_TABLES_30SEP_TRUE` is unchanged. |
| Verified | 0 floor rows and 0 curing blocks overlap 02-Oct. The audit marks BR-10 FOLLOWED. |
| Output | `latest_schedule/run_output_holiday/`, `latest_schedule/CTP_28SEP-08OCT_SCHEDULE_HOLIDAY_02OCT.xlsx` |

| Line | Cure plan | Cured by plan end | Cured % | Built % |
|---|--:|--:|--:|--:|
| PCR | 110,532 | 82,591 | 74.7 | 76.5 |
| TBR | 27,298 | 18,476 | 67.7 | 68.0 |
| ALL | 137,830 | 101,067 | **73.3** | 74.8 |

Schedule end: 11-Oct 18:37.

---

## v1: Mixing output in batches (BR-V1)

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v0 |
| Rule | **Mixing output UOM.** Mixing is planned in **batches**, not kilograms. The kg-based planning logic is converted to batch-based planning. The scheduler output gives the **required number of batches** and the **batch allocation for each mixer**. |
| Conversion source | `INPUT_TABLES_30SEP_TRUE/01_DB_tables/JK_CTP_PPDS__CTP_Master_Routing.csv`: the MIXING operation rows, `batch_size` (`batch_UNIT` = KG), read per (compound, mixer) |
| Config switch | `config.yaml → business_rules.mixing_output_uom: BATCH` (set to `KG` to switch the rule off) and `mixing_batch_source` (the table above) |

### Kg-to-batch map for each mixer
- The source's mixer names are mapped to the scheduler's names with `machine_normalize.py`: 270M → K270M, 440M → F440, 430-1 → K430-1, 430-2 → K430-2, 310 → K310, F270 → F270.
- The map has 261 compounds and 543 (compound, mixer) pairs. In the source, every compound has **the same batch size on every mixer that can run it**. The map is still keyed per mixer, so a mixer-specific size would be picked up automatically if the table ever had one.
- Two compounds have a **blank** batch size in the source. They use the gap-filled value already in the working routing, which the plant should confirm:
  - **XMT180: 300 kg**, a median of similar compounds (`routing_gapfill_log.csv`)
  - **GL7: 215 kg**

### How the scheduler applies the rule
1. **Lot sizing (phase 2):** every master and final compound lot is rounded **up** to whole batches. Sticky 3-batch stacks (BRM-02) and +5 buffer batches (BRM-13) then add whole batches on top. Each lot records `batches` and `batch_size_kg`.
2. **Mixer time:** unchanged. The routing rate is minutes per batch, and the scheduler already charged a partial batch as a full batch of time.
3. **Shift split (phase 5):** each mixing lot is split into shift rows of **whole batches**. Batches are shared out by time, and leftover batches go to the rows with the largest fractional share, so a lot's rows add up exactly to its batches. Row kg = batches × batch size.
4. **Output:** the floor schedule CSV gets `batches` and `batch_size_kg` columns. In the Excel, mixing rows on *Upstream schedule* show **batches with UOM = BATCH**, so the *Sheet1* pivot totals batches for each mixer. Two sheets are appended after the template's six:
   - **Mixing batches:** for each mixer and compound, the batch size, batches, kg, runs, and first and last run, plus a MIXER TOTAL row per mixer.
   - **Mixer batch plan:** batches per mixer per plant day, with totals.

### Verification
- 1,738 mixing lots and 1,864 mixing shift rows: **0 rows with a fractional batch**, 0 rows where kg ≠ batches × batch size, and 0 rows with zero batches.
- Batches in the shift rows equal the phase 2 lot batches for every lot (25,262 = 25,262).
- Before this rule, 1,243 of 1,738 lots (and 1,393 shift rows) were not whole batches.
- **Independent cross-check (2026-10-01).** A separate script re-read the source table without using the v1 code. All checks passed:
  - The batch size on every row equals the source value for that compound on that mixer.
  - Every lot's batches = ceil(v0 kg ÷ batch size), for 1,718 of 1,718 lots; the 20 sticky lots are multiples of 3.
  - No lot gained 1 batch or more of kg, and no compound has less kg than in v0.
  - The placed time of all 1,731 lots checked equals batches × minutes per batch ÷ efficiency, to within 0.5 min.
  - Every shift row is within 1 batch of its share of the lot's time.

### Result

**Required batches: 25,262** (6,821,570 kg), across 6 mixers:

| Mixer | Batches | kg |
|---|--:|--:|
| F270 | 5,175 | 1,156,880 |
| K310 | 4,956 | 1,094,780 |
| K430-2 | 4,054 | 1,388,760 |
| F440 | 3,875 | 1,261,205 |
| K270M | 3,717 | 910,120 |
| K430-1 | 3,485 | 1,009,825 |

| Line | Cure plan | Cured by plan end | Cured % | vs v0 | Built % | vs v0 |
|---|--:|--:|--:|--:|--:|--:|
| PCR | 110,532 | 80,527 | 72.9 | −1.8 | 74.3 | −2.2 |
| TBR | 27,298 | 18,571 | 68.0 | +0.3 | 68.4 | +0.4 |
| ALL | 137,830 | 99,098 | **71.9** | **−1.4** | 73.1 | −1.7 |

Schedule end: 11-Oct 17:29, 1 h earlier than v0. Kit shortfall is 0, and 3,891 of 3,891 cure blocks are scheduled.

**Notes**
- Rounding to whole batches adds **+173,734 kg** of compound (+2.6%, lot level) as surplus stock, at no extra mixer time (1,159 h vs 1,161 h in v0). That is under 1 batch per rounded lot, about 140 kg on average.
- The −1.4-point fulfilment change is **not** caused by extra mixer load. The larger compound lots change how compound is allocated to consumer lots (phase 3), and that changes the placement order downstream. It has not been analysed further.

| Business-rule audit | v0 | v1 |
|---|--:|--:|
| BRM-09: same item split into more than one run in a shift (mixers) | 98 | 93 |
| BRE-02 group: the same for extruders and cutters | 216 | 186 |
| BRM-03: transitions that break the rule | 35 | 40 |
| BRM-06: master/final switches without TR100 | 1 | 2 |
| BRM-01: mixing lots below 5 batches | 2 | 2 |

**Output:** `latest_schedule/v1/CTP_28SEP-08OCT_SCHEDULE_v1.xlsx`, `latest_schedule/v1/run_output/`, `latest_schedule/v1/run_log.txt`

**Code changed:**
- `mixing_batches.py` (new): the batch-size map
- `phases/phase2_lot_sizing.py`: whole-batch lot rounding and the batches column
- `phases/phase5_forward_placement_v2.py`: whole-batch shift split
- `make_schedule_from_template.py`: BATCH output and the two new sheets
- `config.yaml`: `mixing_output_uom`, `mixing_batch_source`

---

## Rule check: 60-second mixing changeover (BR-74 / BRM-10), already in effect, no new version

| | |
|---|---|
| Date | 2026-10-01 |
| Rule | **Mixing changeover time.** A 60-second changeover applies between different compounds on a mixer. The scheduler must include it when working out machine availability. |
| Source (decided with the user) | `config.yaml → mixer_changeover_min: 1` (minutes). No separate Mixing Changeover Time Master exists in the input tables. `CTP_Master_ChangeoverMatrix` has no mixer lines. |
| How it is applied | Phase 5 reserves the changeover as busy time on the mixer directly before each lot, counted with the lot's run time, so no other work can use it. This has been in effect since v0, so **v1 already includes it and no new version was created.** |
| Verified on v1 (floor schedule) | 1,712 compound-to-compound changes on 6 mixers. The smallest gap on **every mixer is 1.0 min** and **0 are shorter**. Same compound back to back runs with 0 gap (no changeover). The audit marks BRM-10 / BR-74 FOLLOWED. |

| Mixer | Compound changes | Smallest gap (min) | Under 1 min |
|---|--:|--:|--:|
| F270 | 263 | 1.0 | 0 |
| F440 | 340 | 1.0 | 0 |
| K270M | 250 | 1.0 | 0 |
| K310 | 260 | 1.0 | 0 |
| K430-1 | 219 | 1.0 | 0 |
| K430-2 | 380 | 1.0 | 0 |

**Possible refinement (not applied):** during placement the changeover is worked out against the last lot *placed* on the mixer. The scheduler can slot a lot into an earlier gap, so that isn't always the lot running just before it. The minute is also charged when the same compound runs again. The final schedule still meets the rule, as the table shows.

---

## v2: Butyl-friendly compound sequencing (BR-V2)

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v1 |
| Status | Implemented on 2026-10-01 and **run on 2026-10-01**. Results are at the end of this section. |
| Rule: final mixer | Before running **30G, IL3 or IL5**, one of **B1, B2 or B99** must run. If not, a **160 s clean-out** is used **instead of** the changeover. |
| Rule: master mixer | **Before and after** running **29MG, ML405 or ML605**, one of **MB100, MB200, XMB98 or XMB98 Repass** must run. If not, a **160 s clean-out**, and no changeover is added because the mixer is already cleaned. |
| Config switch | `config.yaml → business_rules.butyl_sequence_rule` (`enabled: true`, `cleanout_sec: 160`, the final and master groups). When on, it **replaces the old BRM-03 rule** (5-min purge after butyl, "after" side only). |

### Gap rule for compound A followed by compound B on a mixer (`butyl_rule.py`)
| Case | Gap |
|---|---|
| Same compound continues (A = B) | 0 |
| Final: B is 30G/IL3/IL5 and A is not B1/B2/B99 | **160 s clean-out**, replacing the 60 s changeover |
| Master: B is 29MG/ML405/ML605 and A is not an allowed compound (before) | **160 s clean-out**, no changeover |
| Master: A is 29MG/ML405/ML605 and B is not an allowed compound (after) | **160 s clean-out**, no changeover |
| One transition breaking both checks (for example ML405 → ML605) | **One** 160 s clean-out |
| Also a master↔final switch (TR100 rule BRM-06) | The longer of the two |
| Anything else | The 60 s changeover (BR-74) |

A protected compound is **not** an allowed neighbour of another protected compound: IL3 → IL5 and ML405 → ML605 both need the clean-out, as the rule is written. `targets_count_as_prep: true` would relax this.

### How the scheduler applies it (phase 5)
1. **Placement.** `compute_changeover_min` returns 160 s instead of 60 s on a mixer transition that breaks the sequence, so the clean-out is reserved as mixer busy time.
2. **Re-sequencing** (`rule_repair_pass`), for each mixer:
   - **After** a protected master compound, it prefers a ready MB100/MB200/XMB98 lot next.
   - **Before** a protected compound, if the previous lot isn't an allowed one, it first runs a **ready** allowed-predecessor lot (B99, MB100, …), as long as the protected lot still meets its deadline.
   - It only reorders **real lots that have demand**. It never adds extra B99 or MB100 lots just to prepare the mixer.
   - Where no allowed lot fits, the clean-out is charged.
3. **Final repair** (`final_repair_pass`) uses the same gap calculation and only moves work later, so every transition that breaks the sequence ends up with at least 160 s.
4. **Audit.** BRM-03 becomes "BRM-03 / BR-V2". It reports transitions at protected compounds that followed the sequence vs took a clean-out, and fails if any clean-out gap is under 160 s.
5. **Excel.** A **Mixer clean-outs** sheet is appended, listing each clean-out: mixer, from → to, start, gap, reason and lots.

### Where the rule CANNOT be applied (source `CTP_Master_Routing` / `CTP_Master_BOM`)
| Item | Finding | Effect |
|---|---|---|
| **B1** | Not in routing or BOM | Can never be scheduled. On the final mixers, **B99 is the only usable allowed predecessor.** |
| **B2** | Not in routing or BOM | Same as B1 |
| **XMB98 Repass** | Not in routing or BOM (only XMB98 exists) | Can never be scheduled. The master allowed compounds are MB100, MB200 and XMB98. |
| **ML405, ML605 on K270M** | Routed to K270M and K430-2, but **none of MB100/MB200/XMB98 is routed to K270M** | Every ML405/ML605 run on K270M **always** takes clean-outs (before and after); the sequence can never be followed there. |
| 30G on K310 | B99 is routed to K310 | OK |
| 29MG | Runs only on K430-2, where all three allowed compounds run | OK |

`python butyl_rule.py` prints this report from the source tables.

### Dry check (read-only, on the existing v1 sequence)
The v1 sequence has **120** transitions that would need a clean-out: F270 21, K270M 36, K310 2 and K430-2 61. The re-sequencing is expected to cut the avoidable ones where B99 or MB100/MB200/XMB98 lots are ready.

**Code changed:**
- `butyl_rule.py` (new): the rule and the applicability report
- `phases/phase5_forward_placement_v2.py`: placement changeover, re-sequencing preferences and gap calculation
- `audit_business_rules.py`: the BRM-03 / BR-V2 check
- `make_schedule_from_template.py`: the Mixer clean-outs sheet
- `config.yaml`: `butyl_sequence_rule`

### Result (v2 run)
**Rule check (audit BRM-03 / BR-V2 FOLLOWED):**
- 149 transitions at protected compounds: **28 in sequence** and **121 with a 160 s clean-out**.
- **0** clean-outs shorter than 160 s (the smallest gap is exactly 160 s).
- Clean-outs by mixer: K270M 49, K430-2 46, F270 23, K310 3.
- The 60 s changeover still holds on every other mixer transition (BRM-10: 0 of 1,700 under 1 min).
- The TR100 master↔final rule (BRM-06) is now FOLLOWED: 0 switches without TR100, vs 2 in v1.

| Line | Cure plan | Cured by plan end | Cured % | vs v1 | Built % | vs v1 |
|---|--:|--:|--:|--:|--:|--:|
| PCR | 110,532 | 79,801 | 72.2 | −0.7 | 73.5 | −0.8 |
| TBR | 27,298 | 18,545 | 67.9 | −0.1 | 68.1 | −0.3 |
| ALL | 137,830 | 98,346 | **71.4** | **−0.5** | 72.5 | −0.6 |

Schedule end: 11-Oct 20:25, 3 h later than v1. Kit shortfall is 0, and 3,891 of 3,891 cure blocks are scheduled.

**Notes**
- Most clean-outs cannot be avoided with this data:
  - On K270M, no allowed compound can run at all.
  - On F270/K310, B99 is the only allowed predecessor and has just 10 lots.
- The re-sequencing step could only reorder 4 machines. 27 machines were left in their original order, because reordering would have made a lot finish after the step that uses it. That limits how many allowed-predecessor lots could be moved in front of a protected lot.
- The clean-outs add about 100 s each over the normal changeover (121 × 100 s ≈ 3.4 h of mixer time). That is the likely main cause of the −0.5-point change, but it has not been traced lot by lot. The changed mixer sequence also shifts later steps.

**Output:** `latest_schedule/v2/CTP_28SEP-08OCT_SCHEDULE_v2.xlsx` (with the Mixer clean-outs sheet), `latest_schedule/v2/run_output/`, `latest_schedule/v2/run_log.txt`

---

## v3: Machines open at the plan start (BR-V3)

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v2 |
| Why | The v2 build-delay trace (`trace_build_delays.py`) showed the first builds starting 13–19 h after their plan slot. All upstream work (mixing, components) only began at **28-Sep 23:00**, even though the plan and the stock snapshot start at **28-Sep 07:00**. The machines' open time was taken from the first curing that needs a newly built tyre, with `schedule_open_h: 0`, and that landed on the first build slot. The config's own note says the intent was "every machine starts at plan start". |
| Rule | Every machine is available from the **plan start** (the earliest curing plan start time, 28-Sep 07:00). This is real plant time; no production before the plan start is invented. |
| Config switch | `config.yaml → open_machines_at_plan_start: true` (`false` = v2 behaviour) |
| Code | `phases/phase5_forward_placement_v2.py`: the machines' open time moves to T0 = the plan start, when that is earlier. The run log shows "+16.0 h of upstream time". |

### Result
- First upstream work: 28-Sep **07:00** (v2: 23:00). First build: 28-Sep **23:00**, exactly on its plan slot (v2: 29-Sep 12:34).
- The 5 builds planned for 28-Sep 23:00 start 0–3.5 h after their slot (v2: 13.6–19.5 h).
- For all 45 builds planned before 30-Sep 07:00, the median delay is **0.8 h** (v2: 16.7 h), and 5 are exactly on time.

| Line | Cure plan | Cured by plan end | Cured % | vs v2 | Built % | vs v2 |
|---|--:|--:|--:|--:|--:|--:|
| PCR | 110,532 | 86,369 | 78.1 | **+5.9** | 80.1 | +6.6 |
| TBR | 27,298 | 19,722 | 72.2 | **+4.3** | 75.4 | +7.3 |
| ALL | 137,830 | 106,091 | **77.0** | **+5.6** (+7,745 tyres) | 79.2 | +6.7 |

The biggest daily gains are on 29-Sep (PCR cured 7,777 vs 3,610; TBR 1,890 vs 810) and 28-Sep (PCR 4,302 vs 3,532). Some later days are lower than in v2 (for example 06-Oct PCR 6,588 vs 9,070), because work that previously piled up late is now done earlier. Schedule end: **11-Oct 07:48**, 12.6 h earlier than v2. Kit shortfall 0; 3,891 of 3,891 cure blocks scheduled.

| Business-rule audit | v2 | v3 |
|---|--:|--:|
| BRM-03 / BR-V2 butyl sequence | FOLLOWED (121 clean-outs of 149) | FOLLOWED (126 clean-outs of 155, all ≥ 160 s) |
| BRM-06: master/final switches without TR100 | 0 | **2** (worse) |
| BRM-09: same item split in a shift (mixers) | 84 | 91 |
| BRE-02 group: the same for other machines | 218 | 175 |
| GEN-01: builds starting > 30 min after the plan slot | 669 of 677 | **584 of 677** |

**Output:** `latest_schedule/v3/CTP_28SEP-08OCT_SCHEDULE_v3.xlsx`, `latest_schedule/v3/run_output/` (including `BUILD_DELAY_TRACE.csv`), `latest_schedule/v3/run_log.txt`

---

## v4: Changeover checked against the real neighbours (BR-V4), TESTED, NEUTRAL

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v3 |
| Why | The v3 analysis of the 05–07 Oct dip found that `final_repair_pass` pushed **3,892 upstream lots later (3,785 h; 3,489 of them for a changeover)**. Placement worked out a lot's changeover from the machine's **last-placed** item and reserved it after the lot. Lots are placed in priority order, not time order, so a lot often lands in an earlier gap: the charged changeover had the wrong "from" item, and the changeover to the next lot wasn't reserved at all. |
| Rule | When a lot is placed into a gap between real neighbours P and N: start ≥ end(P) + co(P→B), and end(B) + co(B→N) ≤ start(N). The changeover time comes from the same function as before (matrix, 60 s mixer, 160 s butyl, TR100). |
| Config switch | `config.yaml → neighbour_aware_changeover: true` (`false` = v3) |
| Code | `phases/phase5_forward_placement_v2.py`: `first_fit_nb` (the gap search), `BUSY_ITEM` (the item at each end of every placed interval), and single upstream lots reserve only their run time. Cutters with mother-roll logic and building are unchanged. |

### Result
| | v3 | v4 |
|---|--:|--:|
| Repair pushes: upstream lots (for a changeover) | 3,892 (3,489) | **2,115 (2,049)** |
| Repair pushes: total hours | 3,785 h | 3,856 h |
| Build blocks pushed for kit or TBM | 582 (1,250 h) | 545 (1,326 h) |
| Median build delay, plan days 03 / 04 / 05 / 06 / 07-Oct | 5.7 / 10.1 / 22.3 / 34.0 / 43.3 h | 3.2 / 6.6 / 21.5 / 32.5 / 49.2 h |

| Line | Cured by plan end | Cured % | vs v3 | Built % | vs v3 |
|---|--:|--:|--:|--:|--:|
| PCR | 86,023 | 77.8 | −0.3 | 79.6 | −0.5 |
| TBR | 19,740 | 72.3 | +0.1 | 76.0 | +0.6 |
| ALL | 105,762 | **76.7** | **−0.3** | 78.9 | −0.3 |

Schedule end: 11-Oct 02:12, 5.6 h earlier than v3. Kit shortfall 0. The changeover matrix check (GEN-07) is still FOLLOWED (0 of 2,500 shorter), the butyl rule is FOLLOWED (121 clean-outs, all ≥ 160 s), and BRM-06 has 2 violations, as in v3.

**Conclusion:** there are fewer repair pushes (about −1,800 lots) and a slightly better 03–04 Oct, but the **05–07 Oct dip is unchanged** and the total pushed hours are the same. Wrong changeovers at placement are **not** the main cause of the dip. About 2,000 changeover pushes remain, because later steps move lots again *after* placement without checking neighbours: 891 belt/ply lots re-timed to when builds draw them, and 378 feeder lots re-timed after them. Those re-timing steps (lever #2, "24 h-life components tied to late builds") are the next suspect.

**Output:** `latest_schedule/v4/CTP_28SEP-08OCT_SCHEDULE_v4.xlsx`, `latest_schedule/v4/run_output/`, `latest_schedule/v4/run_log.txt`

---

## Analysis: the 05–07 Oct dip (on v4; no version change)

Date: 2026-10-01. v4 stays as the baseline (`neighbour_aware_changeover: true`).

1. **The delay is created when builds are first placed (building echo), not in the later repair steps.** Comparing each build's start at the building echo (`phase5_building_echo_gates.csv`) with its final start: the final repair adds **0 h** to builds, and all 16,305 build-hours of slide come from kits not ready by the plan slot. Median delay by plan day: 28-Sep 1.2 h, 29-Sep 2.0, 30-Sep 11.4, **01-Oct 35.4** (anything that misses 01-Oct also waits out the 24 h holiday), 03-Oct 16.9, 04-Oct 21.1, 05-Oct 27.9, 06-Oct 39.0, 07-Oct 49.0.
2. **The re-timing of short-life components (lever #2) is not the cause.** It runs after build times are frozen, only moves belt/ply lots into idle cutter time, and is checked so that no build is left short.
3. **The deadlines are aimed correctly.** The curing-based latest start for green tyres is a median 5 h *earlier* than the building plan slot, so components are aimed at the right time. They are produced late.
4. **What holds the 251 kit-late builds planned 05–07 Oct (kit a median 34.6 h late):**
   - **TBR: BEF001 belt edge filler on 1802** (73 builds). 1802 is loaded to **95%** of all available time (216 h = 28-Sep 07:00 → 08-Oct 07:00 minus the holiday). The TBR bead machines 2403/2404/2405 are at 89–92%, and TPA pre-assembly (2201) and TBAPBD003 are also late. **This is a capacity limit.**
   - **PCR: treads on extruder 1002** (TRD 2267/1765/1583/1513…). 1002 is only 62% loaded, but 24 of 29 late tread lots were waiting for their **final compound** (F270 16, K310 13). That compound finished a median 12.7 h after its own deadline, with the final mixer busy 18–21 of the previous 24 h. **The final mixers F270/K310 (85–86% loaded, 80–96% on these days) and the master mixers (90–93%) are the limit for PCR.**
5. **Why it builds up and then recovers:** the plan keeps the mixers, 1802 and the TBR bead machines at about 90–95% every working day, so there's almost no spare time to catch up. The holiday removes 24 h, and each day's shortfall carries into the next. After 08-Oct no new plan work competes, so the backlog clears on 08–09 Oct.

**Levers (not applied):**
- Reduce the mixing load:
  - The BRM-13 +5 buffer batches add **+298,010 kg** (about 4.4% of mixing).
  - Whole-batch rounding (v1) adds +173,734 kg.
- Give the final mixers' time to compound that is due soonest.
- Add capacity, overtime or a parallel machine on **1802** for TBR (flagged as a "hard wall" by the capacity check).
- Accept that the plan's daily rate exceeds sustainable mixing and 1802 capacity after the holiday.

---

## v5: No BRM-13 buffer batches (CURRENT BASELINE since 2026-10-01, by user decision)

> The config was first restored to 5 after this what-if. The user then chose v5 as the version to fall back to, so **`buffer_batches: 0` is the baseline setting.** The plant rule value is 5.

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v4 |
| Why | The dip analysis found the mixers at 90–93% (master) and 85–86% (final) of all available time, and PCR treads waiting for final compound. BRM-13 adds +5 batches to each non-special compound's last lot (+298,010 kg). |
| Change | `business_rules.buffer_batches: 5 → 0` for this run only. It is a **what-if that switches off a plant rule**, so the config was **restored to 5** after the run. |

### Result
- **Mixing: 24,192 batches** (v4: 25,262; −1,070).
- **Mixer load** falls to 83–90% of available time (K430-1 90, K270M 88, F440 85, K430-2 85, F270 83, K310 83), from 90–93% (master) and 85–86% (final).
- **Median build delay** on plan days 04 / 05 / 06 / 07-Oct: 9.6 / 20.5 / 31.2 / **40.6 h** (v4: 6.6 / 21.5 / 32.5 / 49.2).

| Line | Cured by plan end | Cured % | vs v4 | Built % | vs v4 |
|---|--:|--:|--:|--:|--:|
| PCR | 87,316 | 79.0 | **+1.2** | 80.7 | +1.1 |
| TBR | 19,666 | 72.0 | −0.3 | 75.6 | −0.4 |
| ALL | 106,982 | **77.6** | **+0.9** (+1,220 tyres) | 79.7 | +0.8 |

- PCR cured on 05 / 06 / 07-Oct: 12,207 / 7,117 / 9,298 (v4: 11,736 / 6,602 / 8,631).
- Schedule end: 11-Oct 00:28. Kit shortfall 0. The butyl rule is FOLLOWED. BRM-13 shows "NOT MODELLED" (0 buffer batches).

**Conclusion:** easing the mixers helps **PCR** a little (+1.2 points), as expected, since PCR is limited by mixing. **TBR doesn't improve**, because TBR is limited by 1802 and the TBR bead lines, which this change doesn't touch. The dip is reduced but not removed. Whether the buffer can be reduced (for example to 2 batches, or mixed after the plan window) is the plant's decision.

**Output:** `latest_schedule/v5/CTP_28SEP-08OCT_SCHEDULE_v5.xlsx`, `latest_schedule/v5/run_output/`, `latest_schedule/v5/run_log.txt`

---

## Analysis: the 05–07 Oct dip on v5 (the baseline), by line

Date: 2026-10-01. Scripts: `trace_build_delays.py` (PCR); TBR builds matched to plan rows by building machine + quantity.

| Plan day | 29-Sep | 30-Sep | 01-Oct | 03-Oct | 04-Oct | 05-Oct | 06-Oct | 07-Oct |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| **PCR** median build delay (h) | 0.1 | 0.0 | 0.4 | 3.3 | 9.6 | 20.5 | 31.2 | 40.6 |
| **TBR** median build delay (h) | 3.5 | 23.8 | 47.0 | 23.2 | 26.0 | 31.7 | 42.8 | 44.3 |

**PCR:** on time until 01-Oct, then the delay grows every day after the holiday.
- Of the 177 builds planned 04–07 Oct, 111 waited for a component and 63 for their building machine (a knock-on effect).
- The components holding them up were steel belt (801), bead apex (201/202), ply (1103/1104), cap strip (CAP 66), inner liner (1101) and tread (1002). These machines are only **19–72% busy**.
- The components started a median **22 h after their latest start**. In **65 of 111** cases their own input arrived after that latest start.
- The chain goes back to the **mixers**: master mixers are loaded to **85–90% of all available time (93–97% on working days)** and final mixers to 83%.

**TBR:** already about 24 h behind on 30-Sep and 47 h on 01-Oct (a slip plus the holiday), and it never catches up.
- Of the 173 builds planned 04–07 Oct, 91 waited for a component and 80 for their building machine.
- The component holding them up is mostly **steel belt on 802** (54 builds; 802 itself is about 50% busy). Those belts wait for **calendered steel roll (901)** or **edge strip (601)**, and 36 of 54 inputs arrived after the belt's latest start.
- The TBR-only machines are at the limit: **1802 at 95%**, TBR bead lines 2403/2404/2405 at 88–93%, and TBR sidewall 1803 at 86%.

**Cause:** the plan runs the shared mixers and the TBR-only lines at about 85–95% of all available time. There's no spare time to absorb a slip, and the holiday removes 24 h at the busiest point, so the shortfall builds up until the plan ends. The component machines are not the limit.

---

## v6: Holiday-aware production H1–H3 (BR-V6), TESTED, NO EFFECT, reverted to v5

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v5 |
| Why | In v4, about 55 short-life lots (24 h: steel belt, ply, bead apex, rubberized ply, cap strip) were made in the 48 h before the 02-Oct holiday but first used after it, past their shelf life. 47 of the 90 builds planned on 01-Oct slid across the holiday, and 54 of 90 builds in the first 24 h after it waited for their kit. |
| Rules | For any plant-wide HOLIDAY window in the downtime table: **H1** a short-life item (≤ 48 h) needed after the holiday is not made before it if it would age out; **H3** it is aimed at the restart instead; **H2** a long-life item (≥ 72 h) needed within 48 h after the holiday is aimed to finish before it (4 h safety margin inside shelf life). |
| Config switch | `config.yaml → holiday_aware` (now `enabled: false`) |
| Code | `phases/phase5_forward_placement_v2.py`: `HOLIDAYS` (read from downtime_reason) and `_holiday_targets()`, applied to each upstream lot's earliest start and target during placement. |

### Result
The rules only triggered for **12 lots (H1/H3) and 1 lot (H2)**. Cured and built by plan end are **identical to v5** (77.6%, 106,982 tyres), and the schedule end is the same to the second. Short-life lots made before the holiday and used after it, past shelf life: about 52 (v4: about 55). The audit is unchanged. **Per the user's instruction, the baseline goes back to v5.**

### Why it had no effect
At **placement** time, the components that later go stale **weren't needed after the holiday**. Their need time (from curing) was before 02-Oct, so H1 correctly left them alone. They only become "needed after the holiday" **later**, when the building echo finds the build's kit incomplete and slides the **build** across the holiday. By then the component is already made, and the re-timing step can't move it across the holiday because the post-holiday cutter time is already full. The holiday logic is sound, but it sits at the wrong step.

### What would work (not built)
Apply the holiday logic **where builds are placed (the building echo)**:
- **(a)** When a build is about to slide across a holiday, re-plan its short-life components **after** the holiday, next to the rescheduled build, instead of consuming stale material.
- **(b)** Protect the **last pre-holiday build rows**, so their kits are prioritised and they don't slide.
- **(c)** Give post-holiday cutter time first to the short-life components of the earliest builds.

**Output (kept for reference):** `latest_schedule/v6/CTP_28SEP-08OCT_SCHEDULE_v6.xlsx`, `latest_schedule/v6/run_output/`, `latest_schedule/v6/run_log.txt`

---

## v7: Plan-feasible building (BR-V7), TESTED, WORSE, baseline stays v5

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v5 |
| Idea | Re-level the building plan to what the kits allow, instead of letting rows slide and pile up after the plan. |
| Rule | A build row may start **up to 24 h before its plan shift** when its kit is complete and its own TBM is idle (same TBM, same quantity). When filling a day, the dispatch also looks at the next day's rows and only takes one if it can start within the current day. Rows with late kits fall behind ready rows. The result is published as a **revised building plan** with **revised daily targets**. |
| Config switch | `config.yaml → building_plan_feasible: {enabled, pull_forward_h: 24}` (now `enabled: false`) |
| Code | `phases/phase5_forward_placement_v2.py`: `_row_floor` (pull-forward), look-ahead in `_dispatch_order`, a `[BR-V7]` summary line. `make_schedule_from_template.py`: sheets **Revised building plan** and **Revised daily targets**. The runner copies `phase5_building_echo_gates.csv` into the run folder. |

### Result
- 129 rows started earlier than their plan slot (1,123 h pulled forward), 531 later and 17 on time.

| Line | Cured by plan end | vs v5 | Cured % | vs v5 |
|---|--:|--:|--:|--:|
| PCR | 86,989 | −327 | 78.7 | −0.3 |
| TBR | 19,527 | −139 | 71.5 | −0.5 |
| ALL | 106,516 | **−466** | **77.3** | **−0.3** |

- **Audit:** BRM-06 now has **2 violations** (v5: 0). Everything else is unchanged: butyl FOLLOWED, changeover matrix FOLLOWED, downtime FOLLOWED.
- **Schedule end:** 11-Oct 08:33, 8 h later than v5.

**Why it's worse:** pulling rows forward **uses up the scarce components earlier**. The rows pulled forward took kit material that rows due sooner needed. PCR built on 28-Sep went up (3,160 vs 1,804), but PCR built on 01-Oct fell to 9,323 (v5: 13,091), and curing on 03–05 Oct is lower. With component supply (and behind it the mixers and 1802) fixed, re-ordering building doesn't add output, and pulling work early takes material from the rows that need it first. This confirms that capacity is the limit, not the order of the building plan.

**Still useful:** the v7 Excel's **Revised daily targets** sheet shows the building rate the kits can actually support per line and day. For example, PCR 05–07 Oct is about 8,000–9,500 a day against a plan of about 14,000, and TBR 06-Oct is 1,994 against 3,674. v5's "built" column shows the same thing for the baseline.

**Output (kept for reference):** `latest_schedule/v7/CTP_28SEP-08OCT_SCHEDULE_v7.xlsx`, `latest_schedule/v7/run_output/`, `latest_schedule/v7/run_log.txt`

---

## v8: Mixers in due-date order (BR-V8), TESTED, WORSE OVERALL, baseline stays v5

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v5 |
| Why | In v5, **949 of 1,740 mixer lots (55%)** ran while another lot that was ready and due at least 12 h earlier waited behind them (K270M 73%). |
| Rule | Master and final compound lots are placed **first, in order of their latest finish (LFT)**, instead of (3-day wave, first-need date). Their start target is **as early as their inputs allow**, capped so the compound is still within shelf life (minus 4 h) at its latest finish. |
| Config switch | `config.yaml → mixer_edd: {enabled, safety_h: 4}` (now `enabled: false`) |
| Code | `phases/phase5_forward_placement_v2.py`: the `[BR-V8]` block before the placement key (wave "W00", first_need = LFT for compound lots), and the compound start target in `place_one_lot_jit`. |

### Result
| Line | Cured by plan end | vs v5 | Cured % | vs v5 | Built % | vs v5 |
|---|--:|--:|--:|--:|--:|--:|
| PCR | 84,846 | −2,470 | 76.8 | **−2.2** | 79.7 | −1.0 |
| TBR | 20,201 | **+535** | 74.0 | **+2.0** | 77.7 | +2.1 |
| ALL | 105,048 | **−1,934** | **76.2** | **−1.4** | 79.3 | −0.4 |

- **Schedule end:** 10-Oct 17:06, 7 h earlier than v5. Worst build delay 52 h (v5: 87 h).
- **Audit:**
  - BRM-09 (same compound split within a shift) fell from 107 to 49 cases.
  - The butyl rule is FOLLOWED, with 86 clean-outs (v5: 120).
  - BRM-06 has **2 violations** (v5: 0).
  - The 60 s changeover, changeover matrix and downtime rules are still FOLLOWED.
- **Due-date inversions on the mixers** only fell from 55% to 50%. The later steps (first-fit into gaps, building-driven re-timing, repairs) undo most of the placement order.
- **Day by day:**
  - **PCR** gains on 01-Oct (+1,094 cured) and 05-Oct (+1,218), but **06–07 Oct get worse** (−1,288 / −2,178). PCR built on 06-Oct collapses to 1,975.
  - **TBR** gains on 30-Sep (+1,049) and 01-Oct (+437).

**Conclusion:** mixing everything in due-date order as early as possible helps **TBR** (its compound comes earlier, easing the 802/1802 chain) but hurts **PCR** on 06–07 Oct. The likely reason, not yet traced lot by lot: making compound early crowds mixer time, so compound for the later PCR days gets pushed back. Overall it's −1.4 points, so it isn't adopted. **Possible follow-up:** apply due-date order only to TBR compounds, or only in a window (for example the next 48 h), to keep the TBR gain without the PCR loss.

**Output (kept for reference):** `latest_schedule/v8/CTP_28SEP-08OCT_SCHEDULE_v8.xlsx`, `latest_schedule/v8/run_output/`, `latest_schedule/v8/run_log.txt`

---

## v9: TBR components as early as inputs allow (BR-V9, CTP-GEN-01), TESTED, NO EFFECT, baseline stays v5

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v5 |
| Why | The 12 TBR builds planned for 29-Sep 23:00 started 12–32 h late (median 24.5 h). |
| Rule | TBR-only components (item_tyre_type_live = TBR; compounds and building excluded) aim to start as early as their inputs allow, capped so they're still within shelf life (minus 4 h) at their building-plan need. |
| Config switch | `config.yaml → tbr_components_asap: {enabled, safety_h: 4}` (now `enabled: false`) |
| Code | `phases/phase5_forward_placement_v2.py`: the `[BR-V9]` block (TBR item list) and the TBR target in `place_one_lot_jit` |

### Result
The rule reached 1,493 TBR lots, but **cured and built per day, per line and in total are identical to v5** (77.6%), with the same schedule end and the same audit.

### Root cause found (diagnostic re-run of v5 in `latest_schedule/v5_diag/`, which reproduces v5 exactly)
1. When the builds were placed, every one of the 12 late TBR builds was held by **TBR pre-assembly TPA-xxx (machine 2201)**, with its kit 12–29 h late. The belts and ply named in the first trace were only re-timed afterwards, to line up with the late builds.
2. Each TPA lot started the moment its **TBR inner liner TIL-xxx (calender 1901)** was usable. 2201 was busy only 8.4 h over 28-Sep to 01-Oct.
3. Each TIL lot waited for its **squeegee SQ-xxx**, also on 1901.
4. The squeegees for the 29-Sep night builds need **final compound B99** (a TBR compound). Its batches finished on **F270 at 29-Sep 18:14 and K310 at 19:36** and, after 4 h minimum aging, were usable at 22:14 and 23:36. **1901 could not start before 22:24**, which is why it sat idle for about 29 h. Squeegee and inner liner then run one after another on the same calender, each followed by aging, then pre-assembly, so the builds land on 30-Sep night.

**Conclusion:** the TBR 29-Sep delay is set by **when B99 is mixed on the final mixers** (F270/K310, 85–90% loaded), not by component targets. **Next lever:** mix **B99 and the other TBR-chain compounds** earlier, in due-date order, on the final mixers. This is the narrow form of v8, which gave TBR +2.0 points when applied to all compounds.

**Output:** `latest_schedule/v9/run_output/`, `latest_schedule/v9/run_log.txt`

---

## v10: Clean-out = one batch of the compound + IL3/IL5 daily campaign (BR-V10)

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v5 |
| Rule 1: clean-out time | When the butyl sequence is broken (final: B1/B2/B99 before 30G/IL3/IL5; master: MB100/MB200/XMB98/XMB98 Repass before and after 29MG/ML405/ML605), the clean-out is **the time to process ONE batch of the compound being cleaned out**, meaning the compound in the mixer before the clean-out: **T (s) = proc_time (MIN/BATCH, CTP_Master_Routing, that mixer) × 60 ÷ efficiency.** It replaces the changeover. One clean-out per transition. |
| Rule 2: IL3/IL5 (plant rule 3.1.3, BRM-07) | IL3 and IL5 run in **one continuous campaign per plant day** on F270. Whichever is needed first runs first. Only the items demanded that day are included. IL3 ↔ IL5 uses the normal **60 s compound changeover, no clean-out**. No fixed time of day. **B99 runs directly before the campaign** where possible (B1 and B2 are not in routing or BOM); otherwise the one-batch clean-out applies. |
| Config switch | `business_rules.butyl_sequence_rule.cleanout_basis: one_batch_cycle`, `final.family: [IL3, IL5]`, `il_daily_campaign: {enabled, items, prep: B99}` |
| Code | `butyl_rule.py` (`load_cycles`, `cleanout_minutes`, family), `phases/phase5_forward_placement_v2.py` (clean-out length, the `[BR-V10]` campaign step, F270 order kept in the re-sequencing step), `audit_business_rules.py` (per-transition required clean-out, check 3.1.3/BRM-07), `make_schedule_v18.py` (sheet **IL3-IL5 daily plan**, clean-out basis columns) |

### How the IL campaign is placed
Each plant day's IL3/IL5 lots, grouped by the day they're needed, form one block, with a B99 lot in front when that's feasible.
- **First choice:** a free window on F270 between when the inputs are ready and each lot's deadline.
- **When no window exists** (all 8 days here): the block goes at the latest start that meets the earliest deadline, or when its inputs are ready. The other F270 lots overlapping it are pushed later in their existing order (134 lots), skipping downtime. The final repair then re-times anything that depends on them.

### Result
| Line | Cured by plan end | vs v5 | Cured % | vs v5 | Built by plan end | vs v5 |
|---|--:|--:|--:|--:|--:|--:|
| PCR | 86,930 | −386 | 78.6 | −0.4 | 86,418 | +51 |
| TBR | 19,888 | **+222** | 72.9 | **+0.9** | 20,259 | +226 |
| ALL | 106,818 | **−164** | **77.5** | **−0.1** | 106,677 | +277 |

- **Clean-outs:** 108, totalling **5.6 h** (F270 median 125 s, K310 139 s, master mixers 203 s). **All 108 gaps are at least their required one-batch time.**
- **IL campaigns:**
  - **9 campaigns on 8 production days**, with IL3 and IL5 back to back (60 s between) on every day both were demanded (29/30-Sep, 03–07 Oct).
  - **B99 directly in front on 3 of 9** (29-Sep, 04-Oct, 07-Oct). The rest follow another compound with a one-batch clean-out (for example 125–136 s).
  - **28-Sep has 2 IL5-only runs**, because the IL5 lots needed on 29-Sep and 30-Sep were both made on the 28-Sep production day.
- **Audit:**
  - BRM-03 / BR-V2 FOLLOWED (0 clean-outs short).
  - 3.1.3 / BRM-07 **PARTLY**: 1 day with 2 campaigns, B99 prep on 3 of 9.
  - BRM-09 100 cases (v5 107); BRE-02 group 201 (v5 189).
  - **BRM-06 has 2 violations** (v5 0).
  - The 60 s changeover, changeover matrix, downtime and kit rules are all FOLLOWED; kit shortfall 0.
- **Schedule end:** 10-Oct 23:51, 37 min earlier than v5.

**Output:** `latest_schedule/v10/CTP_28SEP-08OCT_SCHEDULE_v10.xlsx` (v18 format, with the **IL3-IL5 daily plan** and **Mixer clean-outs** sheets), `latest_schedule/v10/run_output/`, `latest_schedule/v10/run_log.txt`

---

## v11: BRM-13 buffer batches applied (+5 per compound)

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v10 |
| Check | BRM-13 ("Always produce +5 extra batches to cover technical hold and quality issues", Upstream Business Rules.xlsx row 17) was **implemented but switched off**: `buffer_batches: 0` from v5 to v10. The v10 audit showed "NOT MODELLED" and the v10 log showed "+0 buffer batches: 0 compounds". |
| Rule | +5 batches on each compound's **last lot** in the plan. **Not** for special compounds (ML405, ML605, 29MG) or sticky compounds (JT444, JT466), as the rule's remark requires. |
| Config | `business_rules.buffer_batches: 5` |

### Result
- **Buffer:** 214 compounds got +5 batches, **+298,010 kg**. Total mixing goes from 24,192 to **25,262 batches** (6,528 t → 6,822 t).
- **Audit:**
  - **BRM-13 FOLLOWED**.
  - BRM-09 (same compound split in a shift) improved to 73 cases (v10: 100).
  - The butyl rule is FOLLOWED (102 one-batch clean-outs, 5.1 h).
  - The IL campaign runs on 8 days, B99 in front on 2 of 9.
  - BRM-06 is still 2 violations.
  - Kit shortfall 0.

| Line | Cured by plan end | vs v10 | Cured % | vs v10 | Built by plan end | vs v10 |
|---|--:|--:|--:|--:|--:|--:|
| PCR | 85,035 | −1,895 | 76.9 | −1.7 | 83,745 | −2,673 |
| TBR | 19,378 | −510 | 71.0 | −1.9 | 19,569 | −690 |
| ALL | 104,412 | **−2,406** | **75.8** | **−1.7** | 103,314 | −3,363 |

- Most of the loss is on **07-Oct**: PCR cured 7,300 vs 9,471, PCR built 6,873 vs 9,481. The extra batches are added to each compound's **last** lot, which loads the mixers at the end of the plan.
- **Schedule end:** 11-Oct 08:59, 9 h later than v10.

**Output:** `latest_schedule/v11/CTP_28SEP-08OCT_SCHEDULE_v11.xlsx` (v18 format), `latest_schedule/v11/run_output/`, `latest_schedule/v11/run_log.txt`

---

## v12: IL3/IL5 campaign rule made 100% compliant (fixes to BR-V10)

| | |
|---|---|
| Date | 2026-10-01 |
| Builds on | v11 |
| Rule (clarified by the user) | Whenever IL3/IL5 is produced, it must be in **one continuous campaign**, with **B1/B2/B99 directly in front**. A campaign is needed only when IL is actually made; there's no requirement for a campaign every working day. |

### Issues found in the v10/v11 logic, and the fixes
1. **Campaigns made later pushed earlier campaigns apart.** My room-making step moved an earlier campaign's IL5 lot as if it were an ordinary lot. **Fix:** placed campaigns (B99 + IL) are fixed obstacles, like downtime.
2. **B99 was only used if an existing lot happened to fit.** **Fix:** a whole real B99 lot is chosen **for** each campaign: inputs ready, its own deadline kept, and within shelf life at its last use. The block time is then chosen to satisfy the IL deadlines, B99 readiness and B99 shelf life together.
3. **Later steps detached lots from the block.** The **aging pull pass** (and the earlier-pull sweep) re-timed IL lots individually, which moved IL5_L0002 to 29-Sep 06:19. **Fix:** campaign lots are excluded from both. The **final repair** now shifts a campaign **as one unit**, keeping its offsets.
4. **A production-day check** merges two campaigns that land on the same day. It wasn't needed in this run.

### Result
- **Audit 3.1.3 / BRM-07 / BR-V10: FOLLOWED.** 8 IL campaigns on 8 production days, **0 days with more than one campaign**, **B99 directly in front on 8 of 8**, IL3 ↔ IL5 60 s apart.
- **28-Sep** is now one campaign (B99 → IL5 → IL5).
- **04-Oct** has a 58 min idle gap between its two IL3 lots, but no other compound in between. The final repair shifted the block for an input.

| Line | Cured by plan end | vs v11 | Cured % | vs v11 |
|---|--:|--:|--:|--:|
| PCR | 84,938 | −97 | 76.8 | −0.1 |
| TBR | 19,286 | −92 | 70.6 | −0.4 |
| ALL | 104,224 | **−188** | **75.6** | **−0.2** |

- **Cost:** making room for the B99 + IL blocks pushed 287 other F270 lots later. 6 of 8 campaigns finish after their IL deadline (B99's own inputs come late), and the final repair re-times their consumers.
- **Schedule end:** 11-Oct 11:41.
- **Other rules:**
  - BRM-13 FOLLOWED; butyl one-batch clean-outs FOLLOWED (95, 4.8 h).
  - BRM-09 76 cases; BRM-06 still 2 violations.
  - Kit shortfall 0.

**Output:** `latest_schedule/v12/CTP_28SEP-08OCT_SCHEDULE_v12.xlsx` (v18 format), `latest_schedule/v12/run_output/`, `latest_schedule/v12/run_log.txt`

---

## v13 — butyl clean-out per mixer group (user, 2026-10-01) — ACTIVE

**Rule (replaces the one-batch clean-out of v10):**
- **Final mixer:** before 30G / IL3 / IL5 run B1, B2 or B99. If the sequence is broken, charge a **120 s clean-out** in place of the changeover.
- **Master mixer:** before and after 29MG / ML405 / ML605 run MB100 / MB200 / XMB98 / XMB98 Repass. If the sequence is broken, there is **no clean-out**, only the normal 60 s compound-to-compound changeover.

**Implementation:**
- `config.yaml` `business_rules.butyl_sequence_rule`: `final.cleanout_basis: fixed`, `final.cleanout_sec: 120`, `master.cleanout_basis: none`.
- `butyl_rule.py`: the clean-out is now set per group. A transition that breaks the master sequence is still counted (`sequence_breaks`) but costs only the changeover.
- The audit reports breaks with no clean-out required separately.
- The IL3 ↔ IL5 family (no clean-out) and the IL campaign rule are unchanged.

**Result** (cured by plan end):

| Line | v12 | v13 | Δ |
|---|--:|--:|--:|
| PCR | 84,938 (76.8%) | 88,008 (79.6%) | +3,070 |
| TBR | 19,286 (70.6%) | 19,660 (72.0%) | +374 |
| ALL | 104,224 (75.6%) | **107,668 (78.1%)** | **+3,444 (+2.5 pt)** |

- **Built:** 103,140 → 107,433 (80.4%).
- **Clean-outs:** 95 clean-outs (4.8 h of mixer time) became 8 × 120 s (0.3 h), all 30G on F270/K310. 96 master-sequence breaks now cost 60 s each.
- **Other rules:** BRM-03/BR-V2 FOLLOWED (0 short clean-outs); 3.1.3 IL campaign FOLLOWED 8/8; BRM-13 FOLLOWED; BRM-06 still 2 violations.
- **Strict genealogy:** 0 links drawn before usable, 0 unknown ids; 1,066 lots with a SHORT input (v12: 1,116).
- **Best version so far, so it is kept.** v13 is the new baseline.

**Output:** `latest_schedule/v13/CTP_28SEP-08OCT_SCHEDULE_v13.xlsx` (v18 format, strict lot / consumption ids), `latest_schedule/v13/run_output/`, `latest_schedule/v13/run_log.txt`

---

## v14 — aging from `aging_comb.csv` only (user, 2026-10-01)

**What changed:** aging (min / max) comes from the local `ctp_inputfiles/aging_comb.csv` only, identical to `INPUT_TABLES_30SEP_TRUE/03_local_masters/aging_comb.csv`. `JK_CTP_PPDS.CTP_Master_Aging` is not read.

**Switch:** `CTP_AGING_SOURCE=comb` in `build_sep_inputs.py`, set by the v14 runner.

**aging_comb has several rows per item:** 5,409 rows for 2,997 items, one per section.
- Conflicting values (116 items on max, 183 on min) are resolved to the **strictest**: shortest shelf life, longest minimum aging.
- 540 items with no values are dropped. `adapt_inputs` fills them from the plant item-type table, or the 0/72 h default.

**Unchanged:** the 72 h green-tyre shelf life (a config rule, not part of the table) and every v13 mixing rule:
- whole batches;
- 60 s changeover;
- 120 s final-mixer butyl clean-out, none on the master mixers;
- IL3/IL5 campaign;
- +5 buffer batches.

**Largest aging differences vs the DB table** (scheduled items, median):

| Item type | DB table | aging_comb |
|---|---|---|
| Master compound, shelf life | 96 h | **360 h** |
| Final compound, shelf life | 72 h | **120 h** |
| Steel belt / edge strip, shelf life | 48–72 h | **24 h** |
| Bead apex / bundle, shelf life | 24 h | **48 h** |
| TBR pre-assembly (TPA-xxx), min aging | 0 h | **8 h** |

**Scheduler fix (phase 5, IL campaign same-day check).** The first v14 run had 2 IL campaigns on 06-Oct (3.1.3 PARTLY). After a merge, the merged campaign's B99 stayed behind and the next campaign attached to it.
- After a merge, the merged block is the reference for the next check, and the stranded B99 becomes an ordinary lot again.
- A campaign that cannot merge moves whole to the next production day.

**Result** (cured by plan end):

| Line | v13 | v14 | Δ |
|---|--:|--:|--:|
| PCR | 88,008 (79.6%) | 89,597 (81.1%) | +1,589 |
| TBR | 19,660 (72.0%) | 18,978 (69.5%) | −682 |
| ALL | 107,668 (78.1%) | **108,575 (78.8%)** | **+907** |

- **Built:** 107,433 → 107,840.
- **Mixing:** 1,738 → 1,504 lots (24,146 batches); longer compound shelf life means fewer, larger lots.
- **PCR gains early** (29–30 Sep +2,694), because compounds can be made further ahead.
- **TBR loses** on 04-Oct and 07–08 Oct, from the 8 h TPA minimum aging.
- **06-Oct:** builds planned that day slip about 26 h (tread / sidewall extrusion binding), as in v13.

**Rules:**
- BRM-03/BR-V2 FOLLOWED (7 × 120 s clean-outs, 0 short).
- **3.1.3 FOLLOWED** (5 campaigns on 5 days, B99 before 5/5).
- BRM-13 FOLLOWED; BRM-10 FOLLOWED; BRM-02 FOLLOWED.
- BRM-06 2 violations (as v13); BRM-01 2 lots < 5 batches (as v13).
- GT > 72 h: 26 blocks (v13: 28).

**Strict genealogy:** 0 draws before usable, 0 unknown ids, 0 over-drawn; 1,058 lots with a SHORT input (v13: 1,066).

**Output:** `latest_schedule/v14/CTP_28SEP-08OCT_SCHEDULE_v14.xlsx`, `latest_schedule/v14/run_output/`, `latest_schedule/v14/run_log.txt`

---

## v15 — v14 rules, opening WIP of 03-Oct-2026 07:00, October plan (user, 2026-10-04)

**What changed:** no rule changes; `config.yaml` is as in v14, and `CTP_AGING_SOURCE=comb` is still set. Only the inputs are new:

- **Plan:** `plan-3ct/` (October plan files, 01-Oct → 01-Nov), cut to **03-Oct 07:00 → 13-Oct 07:00**, 10 days like v14.
  - Curing: 4,873 of 14,420 rows, 173,888 tyres.
  - Building: 875 of 2,544 rows, 174,187 tyres.
  - No holiday in the window.
- **DB read live** (`db_pipeline/.env`): BOM, routing, building allowable, cycle times, changeover matrix, MPQ, WIP, GT map and downtime.
  - The earlier downloads are kept in `db_pipeline/raw_backup_before_v15/`.
  - The 02-Oct HOLIDAY rows were merged back into `raw/…downtime_reason.csv`, so v14 can still be rerun offline.

**Opening WIP (WIP MIX, user choice).** `JK_CTP_PPDS.CTP_Opening_WIP` has no 03-Oct snapshot; its latest is 02-Oct 06:30.
- **Compounds and green tyres** come from `jkplanning_CTP.opening_wip` of 03-Oct, latest lot produced 06:50:
  - 4,450 final + 85 master batches = 977.8 t;
  - 6,811 green tyres.
- **Components** come from `CTP_Opening_WIP` 02-Oct 06:30: 2,182 rows (tread, sidewall, ply, belts, bead, IL, cap strip …).
- The old feed's own component rows are **not** used: blank unit, tread not in NOS, no TBR belt / nylon / chipper types, and about 21k rows with no item type.

**Code (all switches; unset = v14 behaviour):**
- `build_sep_inputs.py`: `CTP_PLAN_START` / `CTP_PLAN_END` (plan window), `CTP_WIP_CUTOFF`, and `CTP_WIP_MIX`; also refreshes `downtime_reason`.
- `phase1b`: `CTP_EXPECTED_PLAN_MONTH`.
- `db_connect.py`: accepts `DB_USERNAME` as an alias of `DB_USER`.
- `make_schedule_v18.py`: the window and WIP text come from the run.
- New runner `run_v15.py` and guide `HOW_TO_RUN_V15.md`.

**Result** (by plan end 13-Oct 07:00):

| Line | Build plan | Built | Cure plan | Cured |
|---|--:|--:|--:|--:|
| PCR | 137,191 | 86,598 (63.1%) | 137,006 | 86,462 (63.1%) |
| TBR | 36,996 | 22,180 (60.0%) | 36,882 | 22,313 (60.5%) |
| ALL | 174,187 | 108,778 (62.4%) | 173,888 | **108,775 (62.6%)** |

- **Not comparable with v14 (78.8%).** The October plan is about **17,400 tyres per day**, against about 14,800 per working day in v14. Cured tyres by plan end are about the same as v14 (108.8k vs 108.6k).
- **Capacity wall:** 1802 steel-belt edge strip needs 12.0 days of work in a 10-day window, with no alternate machine. Bead apex 2403–2405 needs 10.4–10.9 days. Mixers K430-1, K270M, F270 and K430-2 need 10.1–10.7 days.
- **Slip:** the work spills to 13–17 Oct. Building drops on 10–12 Oct (11-Oct: 3,457 built against a plan of 17,478) and catches up on 13–16 Oct.
- **Mixing:** 2,049 lots, all ≥ 5 batches; 32,589 batches, including the spill after 13-Oct.

**Rules:**
- BRM-03/BR-V2 APPLIED (11 clean-outs, 0 short).
- BRM-01 APPLIED (0 lots < 5 batches; v14 had 2).
- BRM-13 APPLIED; BRM-10 APPLIED.
- BRM-06 2 violations, as v14.
- **3.1.3 / BRM-07 PARTLY:** 6 campaigns on 6 days, but only 4 of 6 follow B99. The 12-Oct campaign follows S53 (120 s clean-out applied); the 13-Oct campaign is outside the window and follows T5245.
- BRG-01: the DB has no PM / MTC window dated in 03–13 Oct.
- GT > 72 h: 22 blocks.

**Strict genealogy:** 32,375 links; 0 drawn before usable; 0 unknown ids. 1,846 lots have a SHORT input (v14: 1,058), from the heavier plan.

**Output:** `latest_schedule/v15/CTP_03OCT-13OCT_SCHEDULE_v15.xlsx`, `latest_schedule/v15/run_output/`, `latest_schedule/v15/run_log.txt`. Rerun with `python run_v15.py` (see `HOW_TO_RUN_V15.md`).

> **Found later (v16):** v15 curing dropped **5,574 opening green tyres** as "no produced_time" because of a date-format bug, so v15's cured figure is understated by about 3,300. Fixed in v16.

---

## v16 (first run, WIP mix; superseded, kept as `latest_schedule/v16_wipmix/`) — IL campaign: B99 always in front (dedicated prep lot) + mixed-format stock dates fixed (user, 2026-10-04)

**Same inputs as v15:** WIP 03-Oct 07:00, October plan 03 → 13 Oct, run `--offline` on the tables v15 read live, so nothing in the data changed.

**1. Rule fix — 3.1.3 / BRM-07 (B1/B2/B99 before every IL3/IL5 campaign)**
- **Problem in v15:** 2 of 6 campaigns (12-Oct and 13-Oct) had no B99 in front.
  - The campaign may only move an *existing* B99 lot in front if that lot stays on time for its own consumers.
  - Under the October load the IL inputs are ready late, after every free B99 lot is already due. That held for 10 free lots across 3 campaigns. So none could move, and the 120 s butyl clean-out was used instead.
- **Fix:** if no existing B99 lot fits, a **dedicated B99 prep lot** of `prep_lot_batches` (5, the BRM-01 minimum) is made directly before the campaign. This is the plant's B99 flush; the extra B99 goes to stock.
  - The lot is sized like an existing B99 lot on that mixer (230 kg per batch, same kg/h).
  - It is pinned with the campaign.
  - If its campaign merges into another one the same day, the prep lot is removed.
- **Switch:** `config.yaml` `il_daily_campaign.prep_make_lot: true` and `prep_lot_batches: 5`. `false` gives v15 behaviour.
- **Code:** `phases/phase5_forward_placement_v2.py`, BR-V10 block (`_new_prep`). The env `CTP_IL_DEBUG=1` prints why each B99 candidate is rejected.

**2. Bug fix — opening-stock dates in mixed formats**
- The WIP-mix stock combines two feeds: `…:59.483` (CTP_Opening_WIP) and `…:36` (old feed).
- pandas guessed one format from the first row and returned NaT for every row in the other format.
- **Curing (phase 6)** then dropped those green tyres as "age unknowable, NOT seeded": v15 seeded **975** opening green tyres instead of **6,549**.
- **Planning (phase 1c and others)** skips the shelf-life check on NaT rows, so expired stock could be netted. In v15 it happened to be harmless (all 5,836 affected rows were alive green tyres), but a test run read from CSV netted about 1,000 expired compound and green-tyre rows.
- **Fix:** `format="mixed"` in `phases/db_loader.py`, `phases/mrp_time_phased.py`, `phases/phase5_forward_placement_v2.py` (GT seed), `phases/phase6_curing_revision.py` (GT seed) and `strict_links.py`. Phase 2 and the phase-5 aging pull already had it.
- **v13 and v14 are not affected:** their stock came from a single feed with a single format (checked: 0 unparsed rows).

**Result** (by plan end 13-Oct 07:00):

| Line | v15 cured | v16 cured | Δ |
|---|--:|--:|--:|
| PCR | 86,462 (63.1%) | 89,211 (65.1%) | +2,749 |
| TBR | 22,313 (60.5%) | 22,826 (61.9%) | +513 |
| ALL | 108,775 (62.6%) | **112,037 (64.4%)** | **+3,262 (+1.8 pt)** |

- **Built is unchanged** (108,778, 62.4%). The building rows are identical to v15's, row by row.
- **The IL fix costs nothing:** 2 prep lots, `B99_PREP001` on 12-Oct 17:56 and `B99_PREP002` on 13-Oct 15:48, 5 batches / 1,150 kg each, about 11 min each on F270. 6 other mixing rows moved.
- **The whole cure gain** comes from the 5,574 opening green tyres now being cured.

**Rules:**
- **3.1.3 / BRM-07 FOLLOWED 6/6**: 6 campaigns on 6 days, B99 directly in front of all 6, 60 s changeover (v15: 4/6).
- BRM-01 APPLIED (0 lots < 5 batches).
- BRM-03/BR-V2 APPLIED (9 clean-outs, 0 short).
- BRM-13 APPLIED.
- BRM-06 2 violations (as v14 and v15).
- GT > 72 h: 28 cure blocks (v15: 22). More opening green tyres are now used, and some are older.

**Strict genealogy:** 37,047 links; 0 drawn before usable; 0 unknown ids. 1,777 lots have a SHORT input (v15: 1,846).
- The prep lots' output is consumed (SQ-018_L0012 draws both).
- **Open gap:** their master input XMB98 is not planned upstream. PREP001 gets 439 kg from XMB98_L0009 and is short 644 kg; PREP002 is short 1,084 kg.

**Output:** moved to `latest_schedule/v16_wipmix/` when v16 was redone on the 03-Oct old feed only (next section).

---

## v16 (final) — opening WIP from `jkplanning_CTP.opening_wip` 03-Oct (07:00 stock) ONLY (user, 2026-10-04)

**What changed vs the first v16 run:** the user decided that **all** opening stock comes from the old feed's 03-Oct snapshot. No component stock comes from `CTP_Opening_WIP` 02-Oct any more; 02-Oct was a plant holiday, but production restarted on 02-Oct at 18:34. The rules are the same as the first v16 run, plus one IL fix.

**How the old feed is made usable** (`build_sep_inputs.py`, `CTP_WIP_SOURCE=old`; runner `--wip-source old`, now the default):
- **20,976 rows with no item_type are dropped.** Their item codes are corrupted (`\x14\x14720`, …). Most are compound batch records, and only 183 match a main-feed lot.
- **Compounds:** 4,535 rows = 1 batch each × the routing batch weight = 976.8 t, as before.
- **Components:** 892 rows had a blank unit and get the item's **BOM unit** (738 M, 113 NOS, 26 KG). 15 items are not in the BOM.
  - **Tread** is in metres in the old feed (about 3.1 m per piece), which matches the BOM (M).
  - **Note:** the main feed gives tread in NOS and nothing converts it, so v14 – v16-first counted tread pieces as metres (about 3× understated).
- **Not in the old feed at all:** BELT-1..4, NYLON, STEEL CHIPPER, BELT EDGE FILLER, BODYPLY, SQUEEGEE, APEXED BEAD, PRE ASSEMBLY. These start at zero stock.
- **Stock used:** 12,238 rows, all dated 03-Oct: 6,811 green tyres, 4,535 compound batches, 892 component rows.

**IL fix (3.1.3):** with the new stock, the first run of this version had 7 campaigns on 6 days. Phase 5's same-day check dated a campaign by its **B99** (12-Oct 06:56 = plant day 11-Oct), while the audit and the IL sheet date it by its **first IL lot** (after 07:00 = 12-Oct).
- The check now uses the first IL lot (`_il0` in the BR-V10 block).
- Result: 6 campaigns on 6 days, B99 directly in front of all 6, 60 s gap each. One dedicated prep lot, `B99_PREP001` (13-Oct 07:08, 5 batches, 1,150 kg).

**Result** (by plan end 13-Oct 07:00):

| Line | v16 first (WIP mix) | **v16 (old feed 03-Oct)** | Δ |
|---|--:|--:|--:|
| PCR cured | 89,211 (65.1%) | 88,390 (64.5%) | −821 |
| TBR cured | 22,826 (61.9%) | 22,708 (61.6%) | −118 |
| **ALL cured** | 112,037 (64.4%) | **111,098 (63.9%)** | −939 |
| ALL built | 108,778 (62.4%) | 107,485 (61.7%) | −1,293 (PCR) |

- **The small drop is expected.** The 03-Oct stock is the real, smaller stock after the 02-Oct evening production restart; for example, sidewall has 127 rows against 310 on 02-Oct. And the belt / nylon / chipper types are absent.
- **Opening green tyres:** 6,549 seeded into curing.
- **Netting:** 275k units netted (WIP mix: 302k).

**Rules:**
- **3.1.3 / BRM-07 FOLLOWED 6/6** (1 campaign a day, B99 before each).
- BRM-01 APPLIED (0 of 2,076 lots < 5 batches).
- BRM-03/BR-V2 APPLIED (10 clean-outs, 0 short).
- BRM-13 APPLIED.
- BRM-06 2 violations (unchanged).
- BRG-01: no PM in the window.
- GT > 72 h: 33 cure blocks.

**Strict genealogy:** 37,055 links; 0 drawn before usable; 0 unknown ids. 1,727 lots have a SHORT input (WIP mix: 1,777).

**Output:** `latest_schedule/v16/CTP_03OCT-13OCT_SCHEDULE_v16.xlsx`, `latest_schedule/v16/run_output/`, `latest_schedule/v16/run_log.txt`.
- Rerun: `python run_v15.py --name v16 --offline`; `--wip-source old` is the default.
- Live run for another day: `python run_v15.py --wip-date YYYY-MM-DD --name v17`.

---

## v17 — v16 rules, opening WIP 04-Oct-2026 06:30 (user, 2026-10-05)

**Stock:** there is no 06:30 snapshot for 04-Oct. `CTP_Opening_WIP` and `CTP_Live_Inventory_Raw`, the tables with 06:30 snapshots, both stopped on 02-Oct.
- Used instead: `jkplanning_CTP.opening_wip` 04-Oct (the 07:00 stock), read live.
- **355 lots produced after 06:30 dropped** (341 green tyres, 6 tread, 5 final compound, 3 others), via `CTP_WIP_CUTOFF`.
- Rows with no item_type dropped (20,749); compounds = 1 batch × the routing batch weight (975.7 t); blank component units filled from the BOM.

**Plan:** October plan cut to 04-Oct 07:00 → 14-Oct 07:00 (174,056 to cure).

**Run:** `python run_v15.py --wip-date 2026-10-04 --wip-time 06:30 --plan-end "2026-10-14 07:00" --name v17` (live).

**Result:** built 112,512 (64.6%), **cured 115,932 (66.6%)**.

**Violations:**
- BRM-09: 95.
- BRM-06: 4 (an audit error, see v18).
- 3.1.3: 8 campaigns on 7 days.
- Green tyre > 72 h: 27 cure blocks.

**Output:** `latest_schedule/v17/CTP_04OCT-14OCT_SCHEDULE_v17.xlsx`.

---

## v18 — mixing rules: BRM-09, BRM-06, 3.1.3 (user, 2026-10-05)

Same inputs as v17 (`--offline`).

**Changes:**
1. **BRM-06 was an audit bug.** The scheduler leaves 6 min between master and final (1 min changeover + 5 min TR100). The audit wanted 6.5 min because it still assumed a 2-min changeover (BR-74 overrode it to 1 min). `audit_business_rules.py` now reads `mixer_changeover_min`. → **0**.
2. **BRM-09 (a compound once per shift per mixer)** — `business_rules.same_item_shift_merge`.
   - A local pass joins a compound's split runs inside a shift: the later run moves up, or the earlier run moves down; the lots in between, and any following lots, shift as needed.
   - Each move is accepted only if every moved lot starts after its inputs are ready (+ min aging), stays within max aging at its last consumer, and touches no IL campaign lot.
   - Consumers may start up to **8 h later** (`max_consumer_delay_h`); final repair then re-times them.
   - The pass runs before final repair, then again strictly (no consumer delay) after it.
   - Final repair now moves each same-item run on a mixer as one unit (`repair_units`).
3. **3.1.3 IL campaign** — `il_daily_campaign.regroup_merged`.
   - When two campaigns merge on one day, the block is re-laid as B99 → B99 → all of the first IL item → all of the other. It used to run IL3 → IL5 → IL3 → IL5, i.e. a BRM-09 violation, and the second campaign's B99 split the block.
   - Phase 5 now dates a campaign by its first IL lot.
   - A strict post-repair check merges two same-day campaigns if no consumer has to start later.

**Not changed — final repair runs once.** It is not a fixed point: run twice on the same schedule, it pushes ~3,300 lots again, because a lot must be fresh at its *last* consumer, so consumer moves feed back. Repeating it drifted the whole plan later (2.7 days after 13 rounds). A time-ordered rewrite of the repair was tried and dropped (window cascade, plan into November); its code is in the file, switched off.

**Result:**
- Built 110,341 (63.3%); cured **114,052 (65.5%)**, −1,880 vs v17. That is the cost of the consumer delays.
- **BRM-09: 95 → 9.** The 9 are runs that final repair pushed into the same shift; joining them now would start a consumer later.
- **BRM-06 0.**
- **3.1.3 PARTLY:** 7 campaigns, B99 before each, but 2 days with 2 campaigns (final repair carried a campaign into the next day; merging would start a consumer later).
- BRM-01, BRM-03, BRM-10, BRM-13 FOLLOWED.
- C4 ledger proof: 0 builds before material.

**Output:** `latest_schedule/v18/CTP_04OCT-14OCT_SCHEDULE_v18.xlsx`.

---

## v19 — green-tyre shelf life enforced (user, 2026-10-05)

v18 + `green_tyre_scrap_expired: true`.
- Phase 6 takes green tyres oldest first. A tyre that is past 72 h (`green_tyre_max_age_h`) at the time the press can start the block is now **scrap**: removed from the pool, never cured.
- If the block then waits for newer tyres and its oldest tyre would be over age at the real start, the draw is undone and redone from that start (up to 5 tries).

**Result:**
- **452 green tyres scrapped. Cure blocks with a tyre over 72 h: 27 → 0** (oldest tyre 71.6 h).
- Built 110,341; cured **114,269 (65.7%)**, +217 vs v18.
- The +217 is not real output. The cure counts include **kit-SHORT blocks**, which are placed even though their green tyres do not exist (all versions do this). SHORT tyres: v18 1,864, v19 2,073 (+209). Net of SHORT, v18 and v19 cure the same.
- **Mixing rules as v18:** BRM-09 9; 3.1.3 2 days with 2 campaigns; BRM-06 0; BRM-01, BRM-03, BRM-10, BRM-13 FOLLOWED.
- **Component aging:** the audit does not measure it ("n/a"). The strict genealogy never draws a component before it is usable (min aging) or past max aging (0 draws before usable; 377 items' expired stock is scrapped, never linked). 1,974 lots show a SHORT input: material not available in time or in quantity, not material used out of its aging window.

**Output:** `latest_schedule/v19/CTP_04OCT-14OCT_SCHEDULE_v19.xlsx`, `run_output/`, `run_log.txt`.

---

## v20 — IL campaign exactly once per plant day, 07:00 → 07:00 (user, 2026-10-05)

v19 + `il_daily_campaign.once_per_plant_day: true`. Same inputs (`--offline`).

**Rule (plant rule 3.1.3):** one IL3/IL5 campaign per plant day, i.e. shifts A + B + C, from 07:00 to 07:00 the next day. It is counted on the first IL lot of the campaign.

**Method** (phase 5, after the single final repair — `_il_once_per_day`). For each plant day with 2+ campaigns, the later campaign is:
- (a) merged into that day's first campaign as [B99s][all of the first IL item][all of the other], when its inputs are ready by then; else
- (b) moved whole to the next plant day if that day has no campaign (first IL lot at or after 07:00); else
- (c) joined to the next day's campaign.

After each fix:
- Lots on F270 that are in the way move later, in their order.
- Every consumer that had waited for a moved lot and now starts before it is ready (+ min aging) is pushed later, cascading on its own machine. This is readiness only, without the max-aging feedback that makes the full repair drift.
- The build blocks are re-timed (final repair, building part only).
- The strict BRM-09 pass runs again.

**Result:**
- 3 days fixed. **4 campaigns on 4 plant days, 1 per day, B99 before each.**
  - 10-Oct day: 11-Oct 04:34.
  - 12-Oct day: 13-Oct 03:12, 8 lots.
  - 14-Oct day: 14-Oct 13:15.
  - 15-Oct day: 16-Oct 00:44.
  - No campaign on 04–09 Oct, 11 and 13 Oct: the IL compound inputs are ready late under the October load.
- 6,077 lots re-timed later; 403 build blocks later.
- Built 110,184; **cured 113,448 (65.2%)**, −821 vs v19, almost all TBR (21,996 vs 22,744).
- GT > 72 h: 0. C4 ledger proof: 0 builds before material.
- BRM-09 still 9 (the same post-repair cases).

**Mixing-rule compliance = followed / occurrences** (`mixing_compliance.py`, counted on the final schedule):

| Version | Followed / occurrences | Overall | Average over rules |
|---|--:|--:|--:|
| v17 | 10,367 / 10,463 | 99.08% | 98.08% |
| v18 | 10,312 / 10,323 | 99.89% | 95.95% |
| v19 | 10,312 / 10,323 | 99.89% | 95.95% |
| **v20** | **10,287 / 10,296** | **99.91%** | **99.95%** |

**v20 by rule:**
- **100%:** BR-V1 whole batches 2,233; BRM-01 ≥ 5 batches 2,065; BRM-02 sticky stacks 26; BRM-10 changeover 1,921; BRM-06 TR100 4; BRM-03 butyl 73; BRM-11 allowed mixer 2,065; 3.1.3 one campaign per day 4/4; B99 first 4/4.
- **BRM-09:** 1,907 / 1,916 (99.53%).
- **Kept out of the %:** BRM-12 and BRM-13 (settings), BRM-05 (no MK3), BRM-04 and BRM-08 (not modelled).

The per-rule workbook is `latest_schedule/v20/MIXING_RULE_COMPLIANCE_v17-v20.xlsx`.

**Caveat (component aging):** `final_repair_hard_max_aging: false`, so the build kit does not block on component max aging. Where only over-age stock exists, the strict genealogy shows the input as SHORT.

**Output:** `latest_schedule/v20/CTP_04OCT-14OCT_SCHEDULE_v20.xlsx`, `run_output/`, `run_log.txt`.

---

## v21 — four-roll calendar (901) rules 2, 3, 4, 5; WIP 01-Oct-2026 07:00 (user, 2026-10-06)

**Inputs:**
- **Plan:** the October plan files (`plan-3ct`), from their first day: 01-Oct 07:00 → 11-Oct 07:00 (155,568 to cure, 153,893 to build).
- **Stock:** `jkplanning_CTP.opening_wip` for 01-Oct (07:00 stock, newest lot 06:59:53), 11,971 usable rows; 22,255 rows with no item_type were dropped.
- **Masters:** offline, the same table downloads as v17–v20.
- **Rules:** every v20 rule is kept.
- **Not applied:** the CAP66 changeover rule (formerly "rule 1") was removed at the user's request.

**4RC rules** (`config.yaml` → `fourrc_rules`):
- **Rule 2, bypass `CAP 66- slitted material`** (`build_sep_inputs._cap66_bypass`):
  - BOM: 216 rows `CAP 66-MOTHERROLL → slitted` become 432 rows mother roll → B616M 1.1607 kg/m + fabric 000000001032281000 0.2224 kg/m; 432 slitted rows are dropped.
  - Routing: the slitted operation (2401/2402) is dropped; the mother roll's inputs become fabric + B616M.
  - Result: **0 slitted lots**.
- **Rule 4, 901 run time = metres ÷ (routing `proc_time` × efficiency)** (`adapt_inputs`):
  - `linespeed_rolls` is not applied on 901, so CAP 66-MOTHERROLL runs at 31 m/min instead of 341.
  - CP 200 and NYLRAD are flagged "NO LINE SPEED IN ROUTING".
  - Result: **186 of 186 lots match**.
- **Rule 5, one creel:**
  - **Steel/fabric and cord change come from the changeover matrix only:** "Steel to Steel Changeover" or "Dual groove" means the cord changes; "Steel to steel compound change" means the same cord. CWBP60 is assumed steel (`class_override`).
  - **Phase 5 sequencing pass on 901, before final repair:**
    - keeps the plan order;
    - takes a same-cord lot first when one is due within 12 h;
    - otherwise starts the next steel lot at max(calender free, end of the old cord + 240);
    - fills the window with released fabric lots, pulled at most 8 h earlier, inputs ready.
  - **Final repair** also holds a steel lot until its creel is ready.
  - **The 240 min are never added to calender time.**
  - **Result:** 23 cord changes; 41 fabric lots (1,218 min) run inside reload windows; the calender waits 1,586 min for the creel.
- **Rule 3, steel/fabric capacity per plant day vs 1,440 min** (minus PM on 901; report only; `fourrc_checks.py`):
  - Every day OK, both separately and combined.
  - Busiest days: steel 46% on 07-Oct, fabric 30% on 01-Oct, combined 63% on 05-Oct.

**Checks** (`latest_schedule/v21/FOURRC_RULES_v21.xlsx`):

| Rule | Check | Result |
|---|---|---|
| 2 | No slitted lots | 1/1 |
| 4 | Run time matches routing | 186/186 |
| 5 | Creel ≥ 240 min | **21/23 (91.3%)** |
| 5 | Creel not double-counted | 23/23 |
| 3 | Capacity, separately | 13/13 |
| 3 | Capacity, combined | 13/13 |

The 2 creel misses are the CP31151 ↔ CWBP60 "Dual groove" pair on 14-Oct, after plan end. The IL-campaign-day fix moved those lots after final repair (its consumer push has no creel check).

**Result** (by 11-Oct 07:00):

| Line | Built | Cured |
|---|--:|--:|
| PCR | 87,194 | 87,400 (71.4%) |
| TBR | 20,227 | 19,588 (59.2%) |
| **ALL** | **107,421 (69.8%)** | **106,988 (68.8%)** |

- **Not comparable with v20,** which has a different stock date and window.
- Green tyres > 72 h: 0 (326 scrapped).
- 3.1.3: 4 IL campaigns on 4 plant days, B99 4/4.
- C4 ledger proof: 0 builds before material.
- **Mixing compliance 99.70%** (8,875 / 8,902). BRM-09 98.3% (27 left); all other mixing rules 100%.

**Output:** `latest_schedule/v21/` (`CTP_01OCT-11OCT_SCHEDULE_v21.xlsx`, `FOURRC_RULES_v21.xlsx`, `MIXING_RULE_COMPLIANCE_v21.xlsx`, `run_output/`, `run_log.txt`).
- Runner: `python run_v15.py --wip-date 2026-10-01 --wip-time 07:00 --plan-end "2026-10-11 07:00" --name v21 --offline`.
- The aborted v21 run of 05-Oct is kept in `latest_schedule/_aborted_v21_20261005/`.

---

## v22 — v21 rules; new plan files `latest_plan_file`; WIP 06-Oct-2026 06:30 (user, 2026-10-06)

**Rules:** every v21 rule; nothing changed.
- Mixing: whole batches, MPQ 5, +5 buffer, butyl sequence, TR100, 60 s changeover, BRM-09 joins.
- IL campaign: once per plant day 07:00 → 07:00, with B99 in front.
- Green-tyre shelf life 72 h, enforced.
- 4RC rules 2, 3, 4, 5.

**Inputs (live DB, read 06-Oct):**
- **Plan:** `latest_plan_file` (4 files, 06-Oct 07:00 → 01-Nov 07:00), cut to **06-Oct 07:00 → 16-Oct 07:00**: 169,570 to cure, 167,030 to build.
- **Stock:** `JK_CTP_PPDS.CTP_Opening_WIP` snapshot **06-Oct 06:30**, 12,850 rows (`--wip-source mix`, which takes the full main-feed snapshot).
  - `jkplanning_CTP.opening_wip` for 06-Oct now holds the identical rows (same 12,845 production ids). The old feed changed shape: no untyped rows, units filled.
  - The stock now **includes pre-assembly** (TPA-004 538.6 m, TPA-020 173.6 m, …), belts, chippers, body ply, squeegee and inner liner.
  - Compounds: 4,381 batches = 944 t. Green tyres: 1,222 re-keyed to BOM codes, 97 unmatched.
- **Masters:** today's BOM, routing, changeover matrix and MPQ; 4RC rule 2 applied (432 slitted rows dropped). Downtime: 76 live rows, none inside the window.
- **Local copies:** the previous downloads are kept in `db_pipeline/raw_backup_before_v22/`; the merged downtime copy (02-Oct holiday rows) was restored after the run.

**Result** (by 16-Oct 07:00):

| Line | Built | Cured |
|---|--:|--:|
| PCR | 94,459 | 95,046 (71.2%) |
| TBR | 21,204 | 22,066 (61.2%) |
| **ALL** | **115,663 (69.2%)** | **117,112 (69.1%)** |

- **TBR builds 363 green tyres on the first day (06-Oct),** where the 04-Oct stock gave 0, because pre-assembly is now in stock.

**Checks:**
- **4RC** (`FOURRC_RULES_v22.xlsx`):
  - No slitted lots: 1/1.
  - Routing line speed: 198/198.
  - **Creel ≥ 240 min: 26/26.**
  - Creel not stacked: 25/26. The one exception has a gap over 255 min with no fabric between, so the steel lot waited for something else.
  - Capacity: every day OK. Peaks: steel 44%, fabric 28%, combined 64%.
- **Mixing compliance 99.79%** (9,588 / 9,608). BRM-09 98.9% (20 left); every other mixing rule 100%.
- **Other rules:** 3.1.3 5 campaigns on 5 days, B99 5/5. Green tyres > 72 h: 0 (45 scrapped). BRM-06 0 violations. C4: 0 builds before material. Genealogy: 0 drawn before usable; 1,833 lots with a SHORT input.

**Known data caveat:** the main feed gives tread in **NOS**, while the BOM uses **M**. Tread stock is netted as metres, about 3× understated (as in v14–v16 first run).

**Output:** `latest_schedule/v22/` (`CTP_06OCT-16OCT_SCHEDULE_v22.xlsx`, `CTP_06OCT-16OCT_SCHEDULE_v22_UNIFIED.xlsx`, `FOURRC_RULES_v22.xlsx`, `MIXING_RULE_COMPLIANCE_v22.xlsx`, `run_output/`, `run_log.txt`).
- Runner: `python run_v15.py --wip-date 2026-10-06 --wip-time 07:00 --plan-end "2026-10-16 07:00" --plan-dir latest_plan_file --name v22 --wip-source mix`.

---

## v23 — v22 + building and curing capacity rules (user, 2026-10-06)

**Rules:** every v22 rule, plus `capacity_rules_v23` in `config.yaml`.
- **B1 building eligibility:** a SKU builds only on the TBMs its GT is routed to. This already held: the building echo keeps the plan's TBM.
- **B2 building capacity:** build time + changeover time fits the TBM's available minutes.
  - **Build time** = qty × routing `GT BUILDING` proc_time ÷ routing efficiency (0.96), by GT code. v22 used the plan cycle with no efficiency.
  - **Changeover:** every GT change on a TBM is charged the changeover matrix's different-size time (`JK_CTP_PPDS.CTP_Master_ChangeoverMatrix`):
    - TBR Building "Size Change": 24 min;
    - PCR Building BJ "Inch to Inch": 60 min. The masters do not say which TBM is BJ or CONTI (42 min), so the larger is used.
  - **Where it applies:** the echo's slot search keeps the gap before and after a block, and so does the final repair, which re-places build blocks on a per-TBM cursor. The repair packed blocks back to back in the first v23 attempt (`run_log_attempt1.txt`); it now adds the changeover too.
- **C1 curing eligibility:** a SKU cures only on its routing `CURING` presses. This already held.
- **C2 curing capacity:**
  - **q_shift** = tyres one press can cure in a shift = ⌊2 cavities × 480 ÷ (routing cure min + allowance)⌋, with allowance 2.5 min for PCR and 6.0 min for TBR. Cure time comes from routing (MIN, or SEC ÷ 60). No DB table holds a per-shift quantity.
  - A plan cure block never exceeds q_shift: **181 blocks trimmed, 297 tyres**, so the plan is 169,273.
  - Each block holds its press for qty × 480 ÷ q_shift min, without being capped at the plan window. Phase 6's free-slot search keeps blocks from overlapping.

**Inputs:** the same plan files, window (06-Oct 07:00 → 16-Oct 07:00) and WIP as v22: `JK_CTP_PPDS.CTP_Opening_WIP` 06-Oct 06:30, 12,850 rows. All tables were read **live** from the DB in the run (previous copies are in `db_pipeline/raw_backup_before_v23/`).

**Result** (lots finished by 16-Oct 07:00; v22 counted the same way):

| | v22 | v23 |
|---|--:|--:|
| Built PCR / TBR | 94,471 / 21,204 | 93,465 / 21,204 |
| Built total | 115,675 (69.3%) | 114,669 (68.7%) |
| Cured total (within_horizon) | 114,984 (67.8%) | 114,316 (67.4%) |
| Build rows on the plan's TBM | 100% | 100% |

- The output stays about 69% of plan for a different reason: build blocks start a median of about 55 h after their plan slot, because their component kit is late (760 kit-late rows). Capacity is not the limit.

**Checks** (`validate_build_cure.py`, `BUILD_CURE_VALIDATION_v23.xlsx`, against `JK_CTP_PPDS.CTP_Master_Routing` and the changeover matrix):

| Check | v22 exceptions | v23 exceptions |
|---|--:|--:|
| B1 TBM routed for the GT | 0 / 1,415 | **0 / 1,433** |
| B2 TBM-day build + changeover ≤ 1440 (PCR 60) | 49 / 281 | **0 / 285** |
| B2 TBM-day, PCR at CONTI 42 | 27 / 148 | **0 / 149** |
| B2 lot run time = qty × cycle ÷ eff | 854 / 892 | **0 / 892** |
| B2 changeover gap ≥ different-size time | 302 / 480 | **0 / 417** |
| C1 press allowed by routing | 0 / 4,800 | **0 / 4,800** |
| C2 line-day ≤ presses × 1440 | 0 / 31 | **0 / 31** |
| C2 press-day ≤ 1440 | 212 / 2,226 | **0 / 2,273** |
| C2 block qty ≤ q_shift | 181 / 4,800 | **0 / 4,800** |

- **Available minutes:** 1440 per plant day. The downtime table has no rows inside 06–16 Oct.
- **Excel sheet splits:** the building sheet splits lots at shift ends and rounds each piece to whole tyres. The validator spreads each lot's routing time over its pieces, so lot totals are exact.
- **Other rules:**
  - 4RC: 2, 4 and 5 (creel) 100%, creel not stacked 28/29, capacity all days OK.
  - Mixing compliance 99.77% (9,594 / 9,616).
  - IL 5/5.
  - C4: 0 builds before material.
  - Green tyres > 72 h: 0 cured over age, **204 scrapped** (v22: 45). Trimmed cure blocks leave some tyres waiting past 72 h.
  - **Cured without a green tyre: 95** (v22: 15).

**Output:** `latest_schedule/v23/` (`CTP_06OCT-16OCT_SCHEDULE_v23.xlsx`, `CTP_06OCT-16OCT_SCHEDULE_v23_UNIFIED.xlsx`, `BUILD_CURE_VALIDATION_v23.xlsx`, `FOURRC_RULES_v23.xlsx`, `MIXING_RULE_COMPLIANCE_v23.xlsx`, `run_output/`, `run_log.txt`).
- Runner: `python run_v15.py --wip-date 2026-10-06 --wip-time 07:00 --plan-end "2026-10-16 07:00" --plan-dir latest_plan_file --name v23 --wip-source mix`.
- Validator: `python validate_build_cure.py latest_schedule/v23/CTP_06OCT-16OCT_SCHEDULE_v23.xlsx --xlsx out.xlsx`.

---

## v24 — v23 + PCR tread Quintoplex rules BR-18..21 (user, 2026-10-06)

**Rules:** every v23 rule, plus `quintoplex_rules` in `config.yaml` (machine 1002, matrix line "Quintoplex PCR Tread"). Masters are read live from `JK_CTP_PPDS`: routing, BOM and changeover matrix.
- **BR-18 eligibility:** a tread runs only on its routing machines (1002 for 228 treads and 10 sidewalls). Already followed; now checked.
- **BR-19 line speed:** run time = metres ÷ (routing proc_time in M/MIN × efficiency 0.96), per tread code. Already followed; now checked.
- **BR-20 changeover:** the matrix pair time, classes A–H:

  | Class | Meaning | Min |
  |---|---|--:|
  | A | Die & preformer change | 4.54 |
  | B | Silica↔silica / carbon↔carbon compound | 10 |
  | F | Cushion compound | 17 |
  | G | Silica compound | 17.42 |
  | D | Single-cap carbon → dual-cap carbon | 30 |
  | E | Dual-cap carbon → dual-cap silica | 32 |
  | C | Single-cap carbon → dual-cap silica | 40 |
  | H | White sidewall compound | 60.04 |

  The matrix is asymmetric (it differs by direction for 32k pairs), so it is always read from item → to item. Already followed; the class letter is now reported.
- **BR-21 similar together (new pass in phase 5,** after the 4RC creel pass and before the final repair). For each plant day (07:00 → 07:00), the 1002 lots run in the order with the least changeover:
  - **Order:** same die (same item, 0 min) first, then the cheapest matrix transition. Ties go to the same main compound (largest BOM quantity, e.g. T2200, PCRT2), then the same cushion compound (B715 / B815; these are the cushion compounds, and a B715↔B815 change is mostly class F, 17 min), then the earliest deadline.
  - **Look-ahead:** a next-day lot of the same item may join today's run.
  - **Coverage window for each lot:**
    - start no earlier than its compounds are ready (and at most 12 h earlier);
    - start no so early that the tread exceeds max aging before its first build;
    - finish before the first build that uses the tread (minus min aging), before any other consumer, at most 12 h later, and before its compound inputs expire.
  - **When a day takes the new order:** when it has no more late lots than the plan order, less changeover, and ends no later. Otherwise the day keeps the plan order.

**Inputs:** the same plan files, window (06-Oct 07:00 → 16-Oct 07:00) and WIP (`CTP_Opening_WIP` 06-Oct 06:30) as v23, read live.

**Result** (`QUINTOPLEX_RULES_v24.xlsx`, compared with v23):

| 1002 (Quintoplex) | v23 | v24 |
|---|--:|--:|
| Changeover min | 3,481 | **3,035 (−446, −12.8%)** |
| Cheap transitions A + B | 64 | **86** |
| Expensive transitions D + E + G | 103 | **77** |
| F (cushion) / H / C | 17 / 4 / 0 | 20 / 4 / 0 |
| Same-die follow-ons | 3 | 4 |
| B715↔B815 switches | 17 | 20 |

- The pass re-sequenced 7 plant days and kept 5 in plan order; 13 same-die lots were joined from the next day.
- **B715↔B815 switches rose (17 → 20)** because the pass minimises total minutes: the cushion compound is only a tie-break, and an F change (17 min) can be cheaper than the D/E/G it replaces.
- **Why less than the earlier 37% estimate:** that estimate ignored deadlines and aging. The coverage window (12 h, first build) limits how far lots can move. Raising `max_delay_h` / `max_pull_h` would give more, at more aging or kit risk.
- **Output:** built 114,571 (v23: 114,669) and cured 114,258 (v23: 114,316), counting lots finished by 16-Oct 07:00.

**Checks:**
- BR-18: 192/192 lots on routed machines and 189/189 routed treads on 1002. BR-19: 192/192. BR-20: 187/187.
- C4: 0 builds before material.
- Building/curing capacity (`BUILD_CURE_VALIDATION_v24.xlsx`): every check 0 exceptions.
- Mixing 99.77%.
- 4RC as in v23 (creel not stacked 28/29).
- Green tyres > 72 h cured: 0 (204 scrapped). Cured without a green tyre: 95 (as v23).

**Output:** `latest_schedule/v24/` (`CTP_06OCT-16OCT_SCHEDULE_v24.xlsx`, `CTP_06OCT-16OCT_SCHEDULE_v24_UNIFIED_WITH_UPSTREAM.xlsx`, `QUINTOPLEX_RULES_v24.xlsx`, `BUILD_CURE_VALIDATION_v24.xlsx`, `FOURRC_RULES_v24.xlsx`, `MIXING_RULE_COMPLIANCE_v24.xlsx`, `run_output/`, `run_log.txt`).
- Runner: `python run_v15.py --wip-date 2026-10-06 --wip-time 07:00 --plan-end "2026-10-16 07:00" --plan-dir latest_plan_file --name v24 --wip-source mix`. It now also runs `quintoplex_checks.py` and `validate_build_cure.py`.
- Compare with another run: `python quintoplex_checks.py latest_schedule/v24/run_output --baseline latest_schedule/v23/run_output --xlsx out.xlsx`.

---

## v25 — v24 + Duplex BR-22..25, Quintoplex 24 h window, bottleneck release, TBM fill (user, 2026-10-06)

**Rules:** every v24 rule, plus:
- **Duplex TBR tread (`duplex_rules`, machine 1801, matrix "Duplex TBR Tread"):**
  - BR-22 eligibility, BR-23 line speed and BR-24 matrix (die 4.76 / die+preformer 4.92 / compound 12.5–14 min) were already followed; now checked.
  - **BR-24:** a pair missing from the matrix is charged **14 min** (`changeover_missing_pair_min`, was the generic 15). 1,678 pairs of routed Duplex items are missing; 1,656 of them involve 8 items with no routing speed. Listed in `latest_schedule/v24/DUPLEX_MISSING_MATRIX_PAIRS.xlsx`.
  - **BR-25:** each plant day runs one BOM compound family at a stretch (T636 treads together, T639 treads together), then the least changeover. It uses the BR-21 pass with `family_first: true`.
  - Tread↔apex (29.01 min) does not apply: no apex is routed to 1801.
- **Quintoplex BR-21 window:** 24 h (`max_pull_h` / `max_delay_h`, was 12).
- **Bottleneck release (`bottleneck_release`):**
  - **Item types:** master compound, final compound, Steel Belt Edge Strip (BEF001 on 1802), bead bundle (2403–2405) and shoulder pad (1803).
  - **Rule:** they start when their inputs are ready, limited only by shelf life (need − (max aging − 2 h)), without the AEST, look-back or 6 h safety hold.
- **TBM fill (BR-V7, `building_plan_feasible` on, 24 h):** an idle TBM may start a ready row of its own plan up to 24 h early.

**Inputs:** the same plan, window and WIP as v24, read live.

**Result** (finished by 16-Oct 07:00):

| | v24 | v25 |
|---|--:|--:|
| Built | 114,571 | 114,394 |
| Cured | 114,258 | 114,238 |
| TBM use, mean of 200 machine-days | 59.7% | 59.7% |
| TBM machine-days idle / < 50% | 13 / 67 | 12 / 60 |
| Green tyres scrapped > 72 h | 204 | 145 |
| Cured without a green tyre | 95 | 55 |
| Quintoplex changeover (pass) | 3,481 → 3,035 | 3,459 → 2,987 |
| Duplex T636/T639 switches | 22 | 19 → 13 |

**Checks:**
- Building/curing capacity: 0 exceptions.
- 4RC: all 100%.
- Mixing 99.8%. IL 1 per day.
- C4: 0 builds before material.
- Quintoplex BR-18/19/20 100%, BR-21 63.4% (24 h test).
- Duplex BR-22/23/24 100%, BR-25 93.1% compatible / 96.8% within coverage.

**Why the TBMs are still not full** (diagnosis, `outputs2/phase5_pre_repair_snapshot.csv`):
- The plan loads the TBMs at about 91%, but 747 of 892 rows wait for components (median 55 h).
- **The mixing room is the binding constraint.** The master mixers need about 99% of the 10 days and the final mixers about 96%. They run 93–96% before the final repair. The repair then adds the mixer changeovers and pushes 5,011 of 6,105 upstream lots about 9 h later each.
- **73% of the opening compound stock (689 of 944 t) is past its maximum age** (aging master: final compound 48–120 h, master compound 96–360 h; most stock was made 21–30 Sep). It cannot be used, so the mixers remake it: 1.1 million kg, about 179 mixer-hours above the net need.
- Other bottlenecks: 1802 BEF001 needs 114% of the window (single machine); bead apex 2403–2405 need 102% combined; 1803 needs 97%.
- The bottleneck release did not raise their use, because these machines are starved by the mixing room, not held back.

**Output:** `latest_schedule/v25/` (`CTP_06OCT-16OCT_SCHEDULE_v25.xlsx`, `..._UNIFIED_WITH_UPSTREAM.xlsx`, `BUILD_CURE_VALIDATION_v25.xlsx` (with *TBM use by day*), `QUINTOPLEX_RULES_v25.xlsx`, `DUPLEX_RULES_v25.xlsx`, `FOURRC_RULES_v25.xlsx`, `MIXING_RULE_COMPLIANCE_v25.xlsx`, `run_output/`, `run_log.txt`).
- Runner: `python run_v15.py --wip-date 2026-10-06 --wip-time 07:00 --plan-end "2026-10-16 07:00" --plan-dir latest_plan_file --name v25 --wip-source mix`.

---

## v26 — v25 + full MPQ from JK_CTP_PPDS.CTP_Master_MPQ (user, 2026-10-06)

**Rules:** every v25 rule, plus:

- **Item-level MPQ.** `build_mpq_items.py` (run after `adapt_inputs.py`) maps every PCR and TBR row of `CTP_Master_MPQ` to the items the scheduler makes. The result is `inputs/mpq_item.csv`: 1,235 items (902 PCR, 329 TBR, 4 on both lines). Phase 2 uses each item's own minimum (`mpq_item_level: true`).
  - **Before v26**, phase 2 used only the PCR sheet, by item type. TBR rows came from a static file and only for items used on TBR alone.
- **How the rows are mapped:**

  | Rows | Applied to |
  |---|---|
  | Calendar Roll (spool) 300 m / (Dipped Cord) 300 m | Steel and fabric cord (class from the 4RC matrix) |
  | BRTTL 280 m | BRTTL calendered roll and BRTTL slitted |
  | Tread PCR 310 m (12–14 inch) / 206 m (15–20 inch) | By the rim size of the SKUs using the tread (`CTP_Master_BuildingAllowable.size`) |
  | Tread TBR 38 m | TBR treads |
  | Chaffer IL (mono 80/120) 300 m | MONOCHAF80 / MONOCHAF120 |
  | Chaffer TBR 350 m | SNCT and NYDBCHF |
  | White SW (Quinto) 1,800 m | PCR sidewalls on 1002 |
  | STC 180 m | STC-WRAP |
  | Wound Bead 600 / Belt 200 / Ply 130 / Bead Apex 33 / Shoulder Pad 70 / SideWall 70 | TBR (Ply 130 includes body ply TBP*) |
  | All PCR rows | As before |

- **Compounds:** 3 batches × the item's batch weight, unchanged.
- **Edge strip and edge gum:** these rows are gauge-specific, but no JK_CTP_PPDS table holds an item's gauge. They use the smallest value (1,350 m / 300 m) and are flagged; `mpq_item_overrides` in config can set the exact value. 8 items are affected.
- **MPQ pooling** (`mpq_pooling`, max 96 h): a small demand bucket is joined to the next one of the same item, across waves too, until it reaches the MPQ, within the shelf window (max − min aging − transfer). The floor then applies once to real demand. 694 buckets were joined.

**Result** (finished by 16-Oct 07:00):

| | v25 | v26 |
|---|--:|--:|
| Lots below the DB MPQ | 195 | **0** |
| Upstream lots | 6,105 | 5,413 |
| Compound made beyond demand | 900 t (144 mixer-h) | **583 t (94 mixer-h)** |
| Built PCR / TBR | 93,190 / 21,204 | **95,405** / 20,192 |
| Built total | 114,394 | **115,597** |
| Cured PCR / TBR | 92,281 / 21,957 | **95,435** / 19,667 |
| Cured total | 114,238 | **115,102** |
| TBM use (mean) / idle machine-days | 59.7% / 12 | 59.3% / 8 |
| Quintoplex changeover (pass) | 3,459 → 2,987 | 3,349 → 2,671 |

- **PCR gains; TBR loses.** TBR's larger minimum runs (belt 200, body ply 130, wound bead 600) make the TBR chain wait longer. Bead apex use dropped from about 77% to about 65%.

**Checks:**
- Building/curing capacity: 0 exceptions.
- Quintoplex BR-18/19/20 100%, BR-21 71.9%.
- Duplex BR-22/23/24 100%, BR-25 96.3%.
- Mixing 99.72%. C4: 0 builds before material. Green tyres > 72 h cured: 0.
- **4RC creel 24/27:** the 3 short reloads (95–230 min) are all on 17-Oct, i.e. spill-over after the plan window, moved by a pass that runs after the final repair. Inside the window: 100%.

**Output:** `latest_schedule/v26/` (`CTP_06OCT-16OCT_SCHEDULE_v26.xlsx`, `..._UNIFIED_WITH_UPSTREAM.xlsx`, `MPQ_COMPLIANCE_v26.xlsx`, `BUILD_CURE_VALIDATION_v26.xlsx`, `QUINTOPLEX_RULES_v26.xlsx`, `DUPLEX_RULES_v26.xlsx`, `FOURRC_RULES_v26.xlsx`, `MIXING_RULE_COMPLIANCE_v26.xlsx`, `run_output/`, `run_log.txt`).
- Runner: `python run_v15.py --wip-date 2026-10-06 --wip-time 07:00 --plan-end "2026-10-16 07:00" --plan-dir latest_plan_file --name v26 --wip-source mix`. It now also runs `build_mpq_items.py` and `mpq_checks.py`.

---

## v27 — v26 + 4RC roll lengths from JK_CTP_PPDS.CTP_Master_FRC (user, 2026-10-06)

**Rule (4RC-6, `fourrc_rules.roll_lengths`):** "respect approved min/max roll lengths for special materials; avoid infeasible residual rolls; make the spool/dip length at once; minimise changeovers."

- **Source:** `CTP_Master_FRC` (48 materials; read live by `build_sep_inputs.py`). `build_mpq_items.py` writes `inputs/frc_lengths.csv`: 36 calender items with a spool/dip length D, 4 without (CWBHT40S564, HTPOLY2000, NYLRAD, PLYTEC → minimum roll only).
  - Codes are matched without spaces, hyphens or MOTHERROLL (e.g. CPJ 120 → CPJ120, CAP 66 → CAP 66-MOTHERROLL), and with the alias MONOCHAFFER → MONOCHAFER.
- **Phase 2:**
  - **Pooling:** a calender item's demand is pooled (also across waves) up to D within its shelf window (max − min aging − transfer; calendered roll 4–72 h).
  - **Run size:** each run is the largest the shelf life allows and **never longer than D**. A run above one spool is made as consecutive pieces of ≤ D with one deadline, so it runs at once.
  - **Spool leftovers:** the open spool/dip residual of the item is used first. A residual below the minimum roll (`spool_length` 300 m = MPQ) is made now instead, so every spool/dip ends at 0 or ≥ 300 m.
- **Unchanged:** MPQ pooling for all other items, and every other 4RC rule (creel, line speed, capacity).

**Result:**

| 4-roll calender 901 | v26 | v27 |
|---|--:|--:|
| Lots / runs | 181 / 162 | **125 / 121** |
| Changeovers | 161 | **120 (−25%)** |
| Lots longer than one spool/dip | 14 | **0** |
| Materials left with a residual < 300 m | 3 | **0** |
| Creel ≥ 240 min | 24/27 | **18/18** |

- 79 runs were sized to spools/dips by phase 2; 7 residuals below 300 m were made at once instead.
- **Plant output:** built 113,625 (v26: 115,597) and cured 114,000 (v26: 115,102). Larger calender runs are made earlier and hold 901 longer.

**Checks:**
- **4RC:** all 100% (roll lengths, minimum, residuals, creel, line speed, capacity).
- **MPQ:** 0 lots below.
- **Building/curing capacity:** 0 exceptions.
- **Quintoplex:** BR-18/19/20 100%, BR-21 61.6%.
- **Duplex:** BR-22/23/24 100%, BR-25 96.8%.
- **Mixing:** 99.85%.
- **Other:** C4 0. Green tyres > 72 h cured: 0 (177 scrapped).

**Open (user question 2026-10-06):** mixer throughput. The routing min/batch (final about 2.2, master about 2.8) ÷ 0.96 plus the 1-min changeover gives about 400–650 batches/mixer/day, against about 230 (final) / 145 (master) on the floor. The missing time is losses no table records. A mixer performance factor is proposed, pending the user's per-mixer figures.

**Output:** `latest_schedule/v27/` (`CTP_06OCT-16OCT_SCHEDULE_v27.xlsx`, `..._UNIFIED_WITH_UPSTREAM.xlsx`, `FOURRC_RULES_v27.xlsx` (sheet *Roll lengths (FRC)*), `MPQ_COMPLIANCE_v27.xlsx`, `BUILD_CURE_VALIDATION_v27.xlsx`, `QUINTOPLEX_RULES_v27.xlsx`, `DUPLEX_RULES_v27.xlsx`, `MIXING_RULE_COMPLIANCE_v27.xlsx`, `run_output/`, `run_log.txt`).

---

## v28 — v27 + mixer capacity calibrated to plant averages (user, 2026-10-06)

**Rule (`mixer_capacity_calibration`):** a final mixer makes on average **230 batches/day**, a master mixer **145 batches/day** (user).
- **Before:** routing MIN/BATCH (final median 2.17, master 2.78 min with efficiency) is the mixing time only. The schedule planned 400–650 batches per mixer per day.
- **Floor evidence** (`CTP_Opening_WIP` batch timestamps, 3,522 consecutive final batches): 3.75 min batch-to-batch inside a run; about 480 final batches per day in total, i.e. about 240 per mixer. See `latest_schedule/v23/MIXING_GAP_ANALYSIS.xlsx`.
- **Phase 2 `op_duration_min`:** every compound's min/batch × one factor per operation — final ×2.88 (→ 6.26 min/batch on average), master ×3.57 (→ 9.93). Compound differences, batch size (routing), transfer time, the 60 s changeover, the 120 s butyl clean-out (`fixed`, from v28) and all mixing rules are unchanged.

**Result:**

| Batches per day (avg / max) | F270 | K310 | F440 | K270M | K430-1 | K430-2 |
|---|--:|--:|--:|--:|--:|--:|
| v27 | 530 / 643 | 537 / 618 | 422 | 411 | 408 | 442 |
| v28 | 164 / 227 | 146 / 214 | 116 / 141 | 129 / 149 | 126 / 142 | 141 / 151 |

- **Output:** built **55,601** (PCR 46,680, TBR 8,921; 33% of the 10-day plan) and cured **58,603**, against 113,625 / 114,000 in v27.
- **At the plant's real mixer rates the 10-day plan cannot be made in 10 days:** the mixing room delivers about half the compound it needs.
- Every other check is unchanged: MPQ 0 below, capacity 0 exceptions, 4RC 100%.

## v29 — v28 + 30G gets B99 in front (final-mixer butyl rule, user, 2026-10-06)

**Rule (`butyl_prep_campaign`, items [30G]):** "FINAL MIXER: before 30G / IL3 / IL5 the mixer must have run B1, B2 or B99".
- **Before:** IL3/IL5 got B99 from the IL campaign. 30G only got the 120 s clean-out penalty (v27: 11 of 12 30G runs after another compound; the audit still counted them as followed).
- **Now:** after the IL campaigns, each plant day's 30G lots form one run on F270/K310 with a real B99 lot moved directly in front (B99 inputs ready, its own deadline and shelf life kept), or a 5-batch B99 prep lot. This is the same routine as the IL campaign, but not counted as an IL campaign. Report: `outputs2/phase5_30g_prep_runs.csv`.
- **New strict check in `MIXING_RULE_COMPLIANCE`:** "BR-V2 prep in front" — a target run must start right after B1/B2/B99 (or its IL family). A clean-out does not count.

**Result:**

| | v28 | v29 |
|---|--:|--:|
| 30G runs with B99 in front | 0 / 11 | **9 / 9** (all with a real B99 lot; 6 later than their deadline) |
| IL3 / IL5 with B99 in front | 7/7, 5/5 | 7/7, 5/5 |
| Built | 55,601 | 55,547 |
| Cured | 58,603 | 58,341 |

- **Mixing** 99.98%. **MPQ** 0 below. **4RC / capacity** 100%.
- **Quintoplex** BR-21 66.5%. **Duplex** BR-25 97.4%.
- **Green tyres scrapped > 72 h: 2,201** (slow mixing leaves built tyres waiting); 0 cured over age. **Cured without a green tyre: 486.**

**Output:** `latest_schedule/v28/`, `latest_schedule/v29/` (schedule, `..._UNIFIED_WITH_UPSTREAM.xlsx` and all check workbooks).

---

## v30 — v29 + Triplex PCR sidewall rules BR-26..28, plus a README for every version (user, 2026-10-06)

**Rules (`triplex_rules`, machine 1001, matrix "Triplex PCR SW"):**
- **BR-26 eligibility** (routing: 77 sidewall codes, 1001 only) and **BR-27 line speed** (routing M/MIN × 0.96) were already followed; they are now checked. 4 routed items have no speed: SUSSW02, SW03SUS, SWRS30, SWRS63.
- **BR-28 changeover:** matrix A die+preformer **3.0 min**, B compound + barrel pushout + flow channel **28.0 min**. Here the matrix agrees with the BOM (same compounds → A, different → B). A pair missing from the matrix is charged **28 min** (`changeover_missing_pair_min`); 450 pairs of routed items are missing.
- **BR-28 "minimise repeated compound changes":** the plant-day pass of BR-21/BR-25 runs on 1001 with family = the sidewall's full BOM compound set (`family_mode: bom_set`, e.g. JT444+R158), family first, then least changeover, 12 h window.

**Result** (`TRIPLEX_RULES_v30.xlsx`):

| 1001 Triplex | v29 | v30 |
|---|--:|--:|
| BR-26 / BR-27 | 100% / 161 of 161 | 100% / 161 of 161 |
| BR-28 gap ≥ matrix time | 159 of 160 | **159 of 159** |
| Switch is same die or same compound family | 59.6% | **64.6%** |
| … or no same-family lot was possible within coverage | 80.8% | **86.3%** |
| Compound-family switches vs minimum | 51 vs 28 | **45 vs 30** |
| Changeover minutes (pass) | — | 2,105 → 1,902 |

- **Only 4 of 28 plant days could be re-sequenced:** with the calibrated (slow) mixers, compounds arrive late and most reorders would delay a tyre build or end the day later.
- **Plant output unchanged:** built 55,547, cured 58,341.
- **All other checks as v29:** mixing 99.98%; MPQ 0 below; capacity and 4RC 100%; Quintoplex BR-21 66.5%; Duplex BR-25 97.4%.

**README per version:** `make_version_readme.py` (run at the end of every `run_v15.py`) writes `latest_schedule/vNN/README_vNN.md`: the run, the rules new in that version, every rule in force by section (inputs, mixing, 4RC, Quintoplex, Duplex, Triplex, upstream, MPQ, building, curing, aging) with the version it came in and its config key, and the files in the folder. READMEs were also written for v22–v29. A new rule needs one row in its `RULES` register.

**Output:** `latest_schedule/v30/` (schedule, `..._UNIFIED_WITH_UPSTREAM.xlsx`, `TRIPLEX_RULES_v30.xlsx` and all check workbooks, `README_v30.md`).

---

## v31 — v30 + Quadraplex TBR sidewall / shoulder-pad rules BR-29..32 (user, 2026-10-06)

**Rules (`quadraplex_rules`, machine 1803, matrix "Quadraplex TBR SW"):**
- **BR-29 eligibility** (routing: 19 TBR sidewalls TSW-… and 9 shoulder pads SP…, 1803 only) and **BR-30 line speed** (routing M/MIN × 0.96) were already followed; now checked. TSW-024 has no speed.
- **BR-31 changeover:** matrix A die 2, B die+preformer 3, C sidewall→shoulder pad 36, D shoulder pad→sidewall 34, E shoulder-pad compound 15 min. Missing pairs (264 blank + TSW-024) are charged **36 min between sidewall and shoulder pad, 15 within a type** (`changeover_missing_pair_by_type`).
- **BR-32 (user):** "sidewall ↔ shoulder-pad changeovers, the most expensive ones, at once in a day, not multiple times". The plant-day pass runs on 1803 with family = item type (`family_mode: itype`, family first):
  - all sidewalls in one block and all shoulder pads in one block;
  - the day starts with the group running at the end of the previous day;
  - inside a block: main compound, then least changeover;
  - 12 h window; the day keeps its plan order if grouping would make a lot late or end the day later.

**Result** (`QUADRAPLEX_RULES_v31.xlsx`):

| 1803 Quadraplex | v30 | v31 |
|---|--:|--:|
| BR-29 / BR-30 / BR-31 | 100% | 100% |
| Sidewall ↔ shoulder-pad switches (whole run) | 15 | **13** |
| Same plant day: switches vs minimum | 11 vs 5 | **8 vs 4** |
| Plant days without a repeated SW↔SP switch | 22 of 26 | **23 of 26** |
| Changeover minutes (pass) | 1,025 | **894** |

- 7 plant days were re-sequenced and 9 same-die lots joined from the next day.
- **The repeats left (SP → SW → SP inside one day) are on 06-Oct, 10-Oct and 05-Nov.** On those days grouping would have made a shoulder pad or sidewall late for its tyre build, so the plan order was kept.
- **Output unchanged:** built 55,547, cured 58,341.
- **Other checks:** mixing 99.98%; MPQ 0 below; capacity and 4RC 100%; Quintoplex BR-21 66.5%; Duplex BR-25 97.4%; Triplex BR-28 86.3%.

**Output:** `latest_schedule/v31/` (schedule, `..._UNIFIED_WITH_UPSTREAM.xlsx`, `QUADRAPLEX_RULES_v31.xlsx` and all check workbooks, `README_v31.md`: 4 new rules, 49 in force).

---

## v32 — general rules BR-1..BR-10; mixing back on routing times, no cap (user, 2026-10-06)

**Mixing (user):** do not change routing data for mixing and do not cap batches. `mixer_capacity_calibration.enabled: false` → routing min/batch ÷ efficiency, as v27.

**Rules:**
- **BR-1:** building rows keep their plan TBM and quantity; early start at most one shift (`building_plan_feasible.pull_forward_h: 8`, was 24).
- **BR-1 / BR-3:** feed-hours = time until the presses run out of the GT (curing need after GT stock) − start. A row with ≤ 8 h goes ahead of a row that could start up to 60 min earlier (`building_priority`).
- **BR-8:** the plant-day family pass now also runs on 18 more matrix-mapped machines (`family_batching`: RHC 1101/1901, cutters 1103/1104/801/802/601/2001/2101, slitters 2401/2402, edge gum 1102, bead 1201/201/202/2403-2405). Family = BOM input set; same item, then family, then least matrix changeover, within coverage.
- **BR-9:** the same-item-in-a-shift merge on ALL upstream machines (`same_item_shift_merge.machines: [ALL]`, exclude 901).
- **BR-2 / BR-4 / BR-5 / BR-7 / BR-10:** already followed; now reported in `GENERAL_RULES_v32.xlsx` (`general_rules_checks.py`).

**Result** (finished by 16-Oct 07:00):

| | v27 | v31 | v32 |
|---|--:|--:|--:|
| Built PCR / TBR | 93,487 / 20,138 | 46,680 / 8,867 | **92,540 / 16,130** |
| Built total | 113,625 | 55,547 | **108,670** |
| Cured total | 114,000 | 58,341 | **108,286** |

- Mixing on routing times restores output.
- **TBR is −4,000 vs v27:** the BR-9 merges delay some consumers up to 8 h (233 of 258 joins), and the family passes reorder the TBR cutters and bead.

| Check | v32 |
|---|---|
| BR-1 on the planned TBM | 100% |
| BR-1 start in plan shift | 3.3% (rows wait for kits) |
| BR-2 / BR-4 / BR-7 (matrix machines) / BR-10 PM | 100% |
| BR-3 press fed | 99.7% |
| BR-5 run time | 99.98% |
| BR-8 machine-days at the family minimum | 44.8% (family switches fell on every machine in the pass logs, e.g. 801 394→314, 802 253→207, 1104 109→81) |
| BR-9 one block per shift | 97.6% |
| BR-7 machines without a matrix line / BR-10 shear change | no data |

- **Other checks:** mixing 99.83%; MPQ 0 below; capacity and 4RC 100%; Duplex BR-25 97.4%; Triplex BR-28 83.9%; Quadraplex BR-32 85.6%; Quintoplex BR-21 59.5%.
- **Green tyres:** 285 scrapped, 0 cured over age; 109 cured without a green tyre.

**Output:** `latest_schedule/v32/` (schedule, `..._UNIFIED_WITH_UPSTREAM.xlsx`, `GENERAL_RULES_v32.xlsx` and all check workbooks, `README_v32.md`: 9 new rules, 58 in force).

---

## v33 — BR-12 / BR-14 / BR-15 / BR-16 / BR-17 (BR-13 NCM not yet); WIP 06-Oct 07:00 (user, 2026-10-06)

**Rules:**
- **BR-12 `aging_hard_block`:** in the final repair, an upstream lot consumes only input alive at its start (min..max aging, FEFO over all lots of the item, opening WIP via open_alloc first). Not enough → wait for fresh supply; none → flagged.
- **BR-14 `shift_handover`:** no item change (changeover window) within 30 min of 07:00 / 15:00 / 23:00 on upstream machines.
- **BR-15 `run_shift_refresh.py`:** per-shift run with the shift-start WIP snapshot, live masters, downtime and plans. Executed-production feed not in the DB yet.
- **BR-16:** reported; the behaviour comes from the family passes, MPQ pooling and 4RC spool runs.
- **BR-17:** `is_current` filter; the README carries the master-data stamp and the overrides list.
- **Checks:** `GENERAL_RULES_vNN.xlsx` now covers BR-12, BR-14, BR-15, BR-16 and BR-17.

**Result:**

| | v32 | v33 |
|---|--:|--:|
| Built | 108,670 | **42,168** |
| Cured | 108,286 | **42,481** |

- **BR-12 waits:** 146 upstream lots waited for fresh input (1,813 h); 904 had no fresh supply at all. The waits cascade to building.
- **BR-14:** 490 item changes moved.
- **Phase-5 experiment on the v33 inputs** (built by 16-Oct): v32 107,566 / **BR-14 only 105,072 (−2.3%)** / BR-12 + BR-14 41,368. **The BR-12 hard block, as implemented, causes the collapse:** compounds are made early (bottleneck release, short final-compound shelf life of 48–120 h) and expire when their real consumers slide later, so consumers wait for fresh lots that are promised elsewhere.
- **`aging_hard_block` is set back to `enabled: false` pending the user's decision**; `shift_handover` stays on.

**Output:** `latest_schedule/v33/` (schedule and all check workbooks, `README_v33.md`: 5 new rules, 63 in force). Not recommended for use because of the output drop.

---

## v34 — strict aging from CTP_Master_Aging, IL rules BR-37/38/39/41, 4RC one-go / full spool / least changeover (user, 2026-10-06)

**Rules (BR-12 remake, BR-40 and BR-13 not implemented, per user):**
- **BR-12 strict aging:**
  - **Source:** `JK_CTP_PPDS.CTP_Master_Aging` (run_v15 `CTP_AGING_SOURCE=db`, was aging_comb.csv since v14); 4,936 items + 8 kept from aging_comb.
  - **Units:** Month = 720 h (fixed in phases 2/4/5, Month was read as hours).
  - **Green tyres:** 72 h (user).
  - **Hard block:** `aging_hard_block` on, no remake.
- **BR-37:** IL lot ≤ 24 h of building need (`mpq_pooling.max_span_h_itypes`).
- **BR-41:** 1901 type blocks by code prefix (TIL / SQ / EG) then compound; 2201 added to family batching.
- **BR-38 / BR-39:** checked. Expansion Work Out: no data.
- **4RC `day_plan`:**
  - one go per item per plant day;
  - same wire / dip together, steel ↔ fabric between wire blocks, fabric fills the creel reload;
  - spool / dip entirely consumed before the change: the last lot of the wire is lengthened (`phase5_spool_consumed.csv`).

**Result:**

| | v32 | exp_mixcap_off (v33 rules, BR-12 off) | v34 |
|---|--:|--:|--:|
| Built PCR / TBR | 92,540 / 16,130 | 90,685 / 15,841 | 37,478 / 7,112 |
| Built total | 108,670 | 106,526 | **44,590** |
| Cured total | 108,286 | 105,665 | **46,186** |

**Two causes of the drop:**
1. **Strict aging without remake:** 120 lots waited (1,986 h); **1,080 lots have no fresh supply**. DB limits are much stricter for master compound (360 → 120 h), final compound (120 → 72 h) and treads (72 → 24 h).
2. **Full-spool rule:** **94 calender lots lengthened by +234,497 m** (about the whole 10-day 901 need again), which occupies 901 and the material behind it.

**Checks:**

| Check | v34 |
|---|---|
| Building / curing capacity | 100% |
| BR-1 on planned TBM / BR-2 / BR-4 / BR-7 / BR-10 / BR-14 | 100% / 100% / 100% / 100% / 100% / 99.7% |
| BR-37 / BR-38 / BR-39 | 100% |
| BR-41 | 56.8% |
| BR-12: max aging 96.4%, min aging 99.0%, upstream fresh input 80.0%, green tyres cured over age 0 | |
| 4RC-7 one go per day | 88.8% (was 77%) |
| 4RC-8 spool fully used before a change | 60.6% (was 5%) |
| 4RC-6 lot ≤ one spool | **84.3%** (lengthened lots exceed one spool) |
| 4RC creel / capacity / line speed | 100% |
| Mixing | 99.89% |
| MPQ | 0 below |
| Extruders BR-21 / BR-25 / BR-28 / BR-32 | 52.9 / 96.3 / 87.6 / 76.0% |

- **Green tyres:** 1,517 scrapped; 415 cured without a green tyre.

**Output:** `latest_schedule/v34/` (schedule, `..._UNIFIED_WITH_UPSTREAM.xlsx`, all check workbooks, `README_v34.md`).

---

## v35 — cover-first dispatch, same compound family together, cheapest changeover next (user, 2026-10-06)

**Run:** WIP 06-Oct 07:00, plan end 16-Oct 07:00, live JK_CTP_PPDS, no mixer cap. Built 43,327 (PCR 36,153 / TBR 7,174) vs v34 44,587; cured 47,189 vs 47,325.
**Dispatch:** building 867 urgent picks / 25 cheapest-changeover / 0 same-GT of 892 rows (almost every GT is already under 8 h cover when its kit arrives). Mixers joined the family pass but changed little (main-BOM-input families are nearly one per compound; 0–4 fewer switches per mixer).
**Adherence:** 82 scored checks, 60 PASS / 11 PARTIAL / 11 FAIL, average 91.9 % (v34 93.3 %), weighted 96.9 % (same). CD-3 TBM-day same GT together 72.6 %, mixer-day families together 28.9 %. BR-12 fresh input 82.4 % (v34 80.0 %), 4RC-7 93.2 % (88.8 %), 4RC-8 67.4 % (60.6 %), BR-32 80.8 % (76.0 %); BR-28 plant-day 35.7 % (57.1 %), BR-41 46.5 % (56.8 %).
**Check fix:** 4RC-3 now splits a lot's minutes over the plant days it runs in (a 471 min lot starting 05:13 was booked whole on its start day -> false 109.7 %).

One dispatch rule for every machine whenever it becomes free:

| Rule | Logic |
|---|---|
| CD-1 cover first | cover_h = (stock + aged WIP) ÷ consumption rate. Building: GT stock ÷ (moulds × tyres per mould-hour). Upstream: the item's consumers' rate. Under the threshold = urgent → lowest cover runs next. |
| Threshold | **Building 8 h** (90th-percentile restock in v34: PCR 60 CO + 264 build + 15 aging ≈ 5.6 h, TBR ≈ 4.6 h, + margin). **Upstream, per item:** matrix changeover + lot run time (routing) + min aging (CTP_Master_Aging) + transfer time + 2 h. |
| CD-2 tie-break | Equal cover → the SKU / item feeding **fewer moulds** first (user). |
| CD-3 grouping | No urgent item → same item continues → same compound family → cheapest changeover in CTP_Master_ChangeoverMatrix → next cheapest. A run breaks only when another item becomes urgent, at a lot / MPQ / spool end. Grouping may cross plant days within shelf life. |

Hard rules stay above it: eligibility, routing speed, matrix changeover time, aging, BR-14 handover, PM/MTC, MPQ, 4RC creel / full spool, butyl sequence, IL one campaign a day.

---

## v36 — v26 rule set + v27..v31 rules + aging from CTP_Master_Aging (user, 2026-10-06)

**Rules in force:** v26 rules + 4RC-6 FRC roll lengths (v27), butyl clean-out 120 s (v28), 30G with B99 prep (v29), Triplex BR-26..28 (v30), Quadraplex BR-29..32 (v31); aging min/max from JK_CTP_PPDS.CTP_Master_Aging (Month = 720 h), green tyres 72 h, applied as in v26 (min aging + transfer before use, max aging on build kits, over-age upstream use reported). No mixer cap.
**Switched off (config, marked `v36`):** building_priority, building_plan_feasible.pull_forward_h 24 (was 8), family_batching, same_item_shift_merge.machines [] (mixers only), aging_hard_block, shift_handover, mpq_pooling.max_span_h_itypes {}, fourrc_rules.day_plan, cover_dispatch.
**Result:** built 104,091 (PCR 90,826 / TBR 13,265), cured 103,316 (PCR 90,343 / TBR 12,974) = 60.9 % of plan (v26 117,450; v35 47,189). Final repair pushes 29,940 h in total (v35: 701,010 h).
**Adherence:** 71 scored checks (rules switched off are measured, not scored): 61 PASS / 5 PARTIAL / 5 FAIL, average 95.6 %, weighted 99.4 %. 4RC all 100 %; building / curing / MPQ 100 %; mixing 99.9 %; gaps BR-21 / 25 / 28 / 32 plant-day grouping (23-69 %).
**TBR below v26:** 300 of 413 late TBR build rows wait for TBR apexed bead TBAPBD*, whose max aging is **24 h in CTP_Master_Aging** (48 h in the older aging file v26 used) -> bead made ahead expires before the build.
**Fix:** a run shipped the previous run's `phase5_spool_consumed.csv` (written only when the 4RC day plan is on) -> false 4RC-4 28.9 % / 4RC-3 76.9 %; phase5 now deletes such optional outputs at start.

---

## v37 — v36 + efficiency taken as 1 everywhere (user, 2026-10-06)

**Rule EFF-1:** `efficiency_override: 1.0` -- every routing efficiency = 1.0 (was 0.96; cap ply slitter 0.78 incl. bobbin changes) in upstream / mixing run time (phase2), building time (phase5), the butyl clean-out helper, and the checks (build/cure, extruders, 4RC, audit). Curing unchanged (cure + allowance).
**Result:** built 105,555 (PCR 90,853 / TBR 14,701), cured 106,209 (PCR 92,032 / TBR 14,178) = 62.6 % of plan (v36 103,316; v26 117,450).
**Adherence:** 71 scored checks, 61 PASS / 5 PARTIAL / 5 FAIL, average 96.1 %, weighted 99.5 %; 4RC / building / curing / MPQ / IL 100 %, mixing 99.9 %, general 99.8 %; gaps = extruder plant-day grouping BR-21 / 25 / 28 / 32 (38-58 %).
**TBR:** still ~0 built on 12-13 Oct -- TBR apexed bead TBAPBD* max aging 24 h in CTP_Master_Aging (48 h in v26's file).

---

## v38 — v37 + 4-roll calender rules 1-3 and mixing BRM-09 applied completely (user, 2026-10-06)

**4RC (901):** `fourrc_rules.day_plan` ON with `mandatory: true` -- every plant day in one-go / wire-block / least-changeover order (fabric between steel cords). Rules 1 + 2 computed together on the actual timeline: every consecutive run of a wire / dip code made in WHOLE spools of its Spool/Dip Length (JK_CTP_PPDS.CTP_Master_FRC by item_code; all 19 items on 901 mapped, one length per wire), top-up spread over the run's lots from the last back, each <= one spool (4RC-6 kept); a second run of an item in a plant day is moved after the first and the top-up recomputed. 1 m tolerance (3000.01 m = one 3000 m spool) in scheduler and check (fixes false 4RC-8 violations and near-full-spool over-production).
**Mixing:** BRM-09 relaxed round after the strict ones (`post_repair_relaxed_h: 8`) + repair re-times later lots; IL campaign regrouped in place to B99 -> all IL3 -> all IL5.
**Result:** built 98,945 (PCR 85,974 / TBR 12,972), cured 98,185 (PCR 85,402 / TBR 12,782) = 57.9 % (v37 106,209). Extra calendered metres to stock (whole spools): +178,055 m on 96 lots.
**Adherence:** 73 scored checks, 63 PASS / 5 PARTIAL / 5 FAIL, average 96.35 %, weighted 99.4 %. 4RC every rule 100 % (4RC-6 / 7 / 8, creel, capacity, line speed); mixing 99.98 % (BRM-09 99.69 %: 5 splits -- B99 twice where 30G and the IL campaign share a shift (both need B99 directly in front), 3 joins blocked by consumer / max aging); building / curing / MPQ 100 %.
**Rule 3 detail:** 15 cord changes, 10 with fabric between (8 of 9 inside the plan window); the others had no fabric lot left to place (19-20 Oct has no fabric demand). The 2 matrix 'steel to steel changeover' pairs are CWBHTCP100 -> CP31151, both wire 1036S31100 (same spool).

---

## v39 — v38 + daily butyl block and the 01-Oct mixing rules applied completely (user, 2026-10-06)

**Rule:** one butyl block per group per plant day (07:00 -> 07:00), butyl + butyl-friendly together (`butyl_day_block`): master on K430-2 friendly -> 29MG -> 120 s -> friendly -> ML405 -> 120 s -> friendly -> ML605 -> 120 s -> friendly (K270M cannot make MB100 / MB200 / XMB98); final on F270 B99 -> IL3 -> 120 s -> B99 -> IL5 -> 120 s -> B99 -> 30G -> 120 s. Real friendly lots first, else 5-batch prep lots (19 made). 120 s after-butyl clean-out in the scheduler's shared changeover function (every pass and repair keeps it). Blocks move only as units in the final repair. 4RC-7 check after the last repair (second run carried over 07:00 joined when whole spools stay whole).
**Mixing audit (MIXING_RULE_AUDIT_v39.xlsx):** Final Mixer – Before Butyl 100 % (17), Final Mixer – After Butyl 100 % (42, final + master butyl), Master Mixer – Butyl Sequence 100 % (25), IL3 or IL5 100 % (10), batches 100 % (1,941 rows); 430-2 Master Mixer – Final Compound not applicable (no final compound on K430-2).
**4RC:** every rule 100 % (one go per day 81/81, spool consumed 68/68, lot <= one spool, creel, capacity, line speed).
**Result:** built 84,631, cured 84,994 = 50.1 % (v38 96,402 corrected).
**Trade-offs:** older internal checks now count the butyl block differently -- '3.1.3 exactly one IL campaign' 0 % (B99 between IL3 and IL5 splits the old 'consecutive IL lots' campaign; IL3 and IL5 are each one run a day), BRM-09 98.55 % (B99 / friendly compounds run several times a shift inside the block, by design).
**Fix:** DAILY_REPORT.xlsx (built / cured) was only built when missing -> a rerun into the same folder kept the previous attempt's numbers (v38 reported 98,185, true 96,402); now rebuilt when the run is newer.

---

## v40 — v39 + 4RC rule 3: steel <-> fabric, least effective changeover (user, 2026-10-06)

**Rule:** effective changeover on 901 = JK_CTP_PPDS.CTP_Master_ChangeoverMatrix minutes ('4 RC Calendar': from_MaterialCode_O, to_MaterialCode_O, changeover_type, changeover_time_min) + the idle one-creel reload (240 min, config) for a direct steel cord change; after the last repair every steel cord change (different CTP_Master_FRC wire_type) without fabric between gets that plant day's fabric wire run moved into the reload window when the effective cost falls and rules 1 / 2 / 4 hold; then a final repair.
**4RC:** rule 1 one go a day 100 % (81), rule 2 spool consumed 100 % (68), rule 4 routing line speed 100 % (121), lot <= one spool / creel / capacity 100 %; rule 3 fabric in every cord change: 100 % inside 06-16 Oct (8 / 8), 13 / 14 overall -- the one miss is 21 Oct (three steel cords, one fabric lot that day; a fabric item is never split, rule 1). Changeovers: steel->fabric 18, fabric->steel 16, fabric->fabric 19, same-cord compound change 10.
**Data note:** CTP_Master_ChangeoverMatrix types CPJ1218 -> CPJ120 'steel to steel changeover' (creel reload), CTP_Master_FRC gives both wire 1036S34500 (same spool) -- the check counts it as no cord change; the scheduler still waits the 240 min reload there.
**Mixing:** all 01-Oct rules 100 % (Before Butyl 15, After Butyl 40, Master Butyl Sequence 25, IL3 or IL5 9, batches 1,938); 430-2 final compound not applicable.
**Result:** built 85,142, cured 85,597 = 50.5 %. TBM-days idle 47 of 200 (TBR 0 % on 12, 14, 15 Oct) -- material late, not TBM capacity. Raw (bought-in) materials are assumed available (no stock / delivery table).

---

## v41 — v40 + IL3 and IL5 together in one go a day (user, 2026-10-07)

**Rule:** IL3 and IL5 made together in the final mixer (F270) in ONE go once a plant day: B99 (butyl-friendly) -> IL3 -> IL5 back to back (plain changeover, no clean-out between), 120 s clean-out after the campaign; 30G keeps its own B99 in front (`butyl_day_block.groups.final.campaigns: [["IL3","IL5"]]`). Rest unchanged.
**Mixing audit / evidence (MIXING_RULE_EVIDENCE_v41.xlsx):** Before Butyl 17 / 17, After Butyl 37 / 37, Master Butyl Sequence 25 / 25, IL3 and IL5 together in one go a day 6 / 6, batches 1,937 / 1,937 -> all 100 %; 430-2 final compound not applicable. Older internal checks: one IL campaign a day 100 %, BRM-09 98.68 % (butyl-friendly lots repeat inside the daily block).
**4RC:** rules 1 / 2 / 4 100 %; rule 3 100 % inside 06-16 Oct, 13 / 14 overall (21 Oct: three steel cords, one fabric lot).
**Result:** built 85,456, cured 85,770 = 50.6 %; 9 butyl prep lots (v40: 13).

---

## v42 — v41 + 4RC wire campaigns, one creel rule from CTP_Master_FRC (user, 2026-10-07)

**Rule:** creel reload (240 min) only when steel items change CTP_Master_FRC wire_type (one rule in every pass and the check); changeover type / minutes from CTP_Master_ChangeoverMatrix; items sharing a wire run together as one load in whole Spool/Dip Length, cheapest matrix order inside; a load may take the next 3 days' lots of its wire within max aging - 4 h; reload windows filled with fabric (next day's dip within shelf life). TBR mixer priority NOT applied (user).
**4RC vs v41 (same FRC-wire definition):** creel reload kept 100 % (32 / 32) vs 71 % (20 / 28, v41 skipped 8 real reloads); fabric in cord changes 86.7 % overall, 15 / 18 inside 06-16 Oct (v41 13 / 18); idle waiting for the creel inside the window 720 min (v41 856); one go / whole spool / lot <= spool / line speed / capacity 100 %; 6 lots joined a wire campaign (shelf life and input readiness limit it); loads 74 (v41 69), item changes 81 (v41 76).
**Mixing:** all 01-Oct rules 100 %.
**Result:** built 81,156, cured 82,719 (v41 85,456 / 85,770) -- correct reloads add waits; 901 running 52.5 % of the window (v41 57.5 %).
**Fix during the run:** day-plan plant-day keys were floats -> range() failed; first v42 attempt stopped and rerun.

---

## v43 — v42 + 901 level loading 30-35 km/day + mixers feed 901 first (user, 2026-10-07)

**Rule:** protect_bottleneck_feed.machines ["901"] (251 compound lots feeding 901 due 24 h earlier); fourrc_rules.level_loading 30-35 km (a light day pulls the next 3 days' lots when inputs are ready and within max aging).
**Result:** levelling pulled 0 lots -- the next days' 901 lots never had their compound ready by the end of a light day (compounds are made just in time, median 5.4 h before the 901 start); daily km 26.6 / 18.2 / 38.8 / 37.6 / 11.2 / 35.4 / 20.0 / 9.0 / 34.6 / 39.8 -> 1 of 10 days in the band. First TBR 901 lot 08-Oct 10:20 (v42 20:50). Built 76,288, cured 79,457 (v42 81,156 / 82,719): the 24 h feed priority takes mixer time from other compounds.
**Rules:** mixing 01-Oct rules all 100 %; 4RC one go / spool consumed / creel / line speed / capacity 100 %, rule 3 88.5 %; 4RC-6 per-item residual 94.7 % (CPJ370 22.8 m -- shares wire 1036S34500, the wire run is whole spools; the check should be per wire).
**Conclusion:** levelling needs the 901 plan to be levelled FIRST and the feeding compounds back-scheduled to it (before mixing placement), not a pull after the fact.

---

## v44 — v43 rules + 901 level plan first (option C: average need +- 5 km) + Deep Roll 48 / 72 h (user, 2026-10-07) -- not a baseline

**Rules added:** fourrc_rules.deep_roll (dipped fabric in full dips: 48 h need >= one dip -> floor(need / dip) dips, else extended to 72 h, never a partial dip; 33 dip lots = 55 dips, 11 decisions extended to 72 h); fourrc_rules.level_plan mode average (avg need 41.7 km/day -> band 36.7-46.7; plan 7 / 9 days in band; 26 feeding compounds back-scheduled); need date fixed to the building-plan first need; v43 blanket feed priority and after-the-fact pull switched off.
**Result:** levelling NOT achieved in the final schedule -- km/day 35.6 / 33.1 / 31.9 / 20.6 / 16.4 / 24.4 / 2.0 / 20.6 / 41.0: the final repair pushed 54 of 75 901 lots by a median 68.6 h, only 4 of them waiting for their own inputs (head-of-line: the repair keeps 901's order, one waiting lot delays every lot behind it). Whole-spool top-up +124 km (v43 +186 km). Built 80,928, cured 81,459 (v42 82,719, v43 79,457). First TBR 901 lot 08-Oct 01:45.
**Rules:** mixing 01-Oct rules all 100 %; 4RC one go / spool consumed / creel / line speed / capacity 100 %, rule 3 88.9 %; **4RC-6 lot <= one dip 82.7 %** -- a deep-roll lot of n dips (e.g. ECOPOLY350 8 km = 4 dips) is one lot; must be split into one lot per dip.

---

## v45 — v44 + one lot per dip + 901 re-plan after the final repair (user, 2026-10-07) -- not a baseline

**Changes:** deep-roll decisions emitted as one lot per dip (4RC-6); `fourrc_rules.replan_after_repair`: 901 list-scheduled on real input-ready times (ready lots first, same item / wire, fabric during a reload, earliest need, level band cap) + a final repair.
**Result:** 4RC-6 back to 100 %, but 901 still uneven (km/day 30.2 / 14.6 / 25.9 / 19.0 / 15.7 / 34.4 / 19.8 / 48.2 / 37.8 / 23.0; 3 / 10 in the band) and output fell: built 72,491, cured 72,721 (v44 81,459). The skim compounds come days after the 901 need even with back-scheduling, e.g. lots needed 07-Oct: B186 09-Oct 22:41, B350 10-Oct, B460 11-Oct, B176 11-Oct; needed 12-Oct: B186 / B96 / B333 / B350 / B616M 17-19 Oct -- the mixers cannot meet those due times.
**Rules:** mixing 01-Oct rules all 100 %; 4RC line speed / capacity / roll lengths / creel 100 %, one go 95.7 %, spool consumed 98.5 %, rule 3 83.3 %, creel not stacked 96.7 % (the re-plan runs whatever is ready).
**Conclusion:** levelling 901 (v43-v45) is limited by compound supply from the mixers, not by the 4RC logic; each attempt cost output.

---

## v46 — v45 + Deep Roll = higher of 48 h and max-aging requirement, full dips only (user, 2026-10-07) -- not a baseline

**Rule (user):** at each decision point, dips needed for the next 48 h and dips needed inside the item's max-aging window (`CTP_Master_Aging`) are both calculated; the HIGHER requirement decides, in **full dips only** (rounded down, at least one dip, never a partial dip). Example: 48 h 1.4 / max aging 2.1 -> 2 dips; 48 h 1.4 / max aging 1.8 -> 1 dip. Replaces the v44 48 h -> 72 h extension. Dip metres left over stay in stock and cover the next demand. All other v45 rules unchanged.
**Config:** `fourrc_rules.deep_roll.mode: max_aging` (`48_72` = v44/v45). Code: `phases/phase2_lot_sizing.py` (DEEP ROLL block).
**Result:** 26 deep-roll decisions = 55 full dips (v45: 33 decisions, 55 dips); max-aging requirement decided 11 of 26. Built 65,238, cured 65,458 (PCR 59,119 / TBR 6,339) vs v45 72,721 (TBR 10,923). Mixing rules 100 % (BRM-09 98.9 %); 4RC one go 100 % (v45 95.7 %), creel not stacked 100 %, rule 3 85.7 % (83.3 %), spool consumed 98.4 %, wire-load cheapest order 80 % (100 %); 901 still uneven (km/day 30.8 / 14.0 / 20.6 / 24.3 / 21.4 / 40.5 / 18.4 / 50.9 / 31.7 / 28.4). Run stopped in SAVE OUTPUTS (PC asleep) and was resumed from the finished phase outputs.

---

## v47 — v42 4RC settings + three mixing rules (user, 2026-10-08) -- not a baseline

**Base:** v42 4-roll calender settings: level loading (v43), level plan (v44), Deep Roll (v44 / v46) and re-plan (v45) switched OFF; every other rule as in v46.
**Rules:**
- **A (one mixer per day):** one compound is planned on only ONE mixer per plant day, never split across mixers. Each compound gets one routing-eligible mixer for the plan: butyl and butyl-friendly compounds on their block mixer, single-mixer compounds keep theirs, the rest balanced by run time. `mixing_v47.one_mixer_per_day`.
- **B (no repeat within 15 h / 24 h):** a run makes at once at least the next 15 h of demand (24 h for IL3 / IL5 and the compounds that feed the 4RC items, from BOM + CTP_Master_FRC); the same compound does not start a new run within 15 h / 24 h of its previous run's start (final repair, later-only; daily butyl blocks). Butyl-friendly compounds exempt: B99 / B1 / B2 (user) and MB100 / MB200 / XMB98 (they must run between the butyl compounds). Window cut to shelf life when shorter. `mixing_v47.cover_windows`, `repeat_gap`.
- **C (final mixers ~230 batches per shift, target):** master lots are due before the earliest final that uses them and may start from their own shelf-life floor; each final lot may start from its shelf-life floor at its LAST use (last need - (max aging - 2 h)). `mixing_v47.final_feed`. Reported in MIXING_RULE_AUDIT ("Final batches per shift"), not a cap.
**v42 measured with the new checks:** one mixer per day 87.4 % (164 of 1,304 compound-days split), no repeat within 15 h / 24 h 69.0 % (404 of 1,302), final mixers F270 148 / K310 128 batches per shift.
**Result:** rules met -- one mixer per day 100 % (1,786 compound-days), no repeat within 15 h / 24 h 100 % (1,604 repeats), all 01-Oct mixing rules 100 %, 4RC rules as v42 (one go / spool / creel / capacity 100 %, rule 3 85.7 %). **Output collapsed:** built 62,730 / cured 65,986 (v42 81,156 / 82,719); TBR built 981 (v42 10,442). Final mixers 70 (F270) / 88 (K310) batches per shift (v42 148 / 128); busy F270 31 %, K310 38 %, masters 52-62 % (v42 56-78 %); final batches in the window 4,720 (v42 8,285). **Cause:** the repeat gap throttles catch-up -- the compounds are already late (v42: 530 of 573 final lots finish after their first need), so a compound's next 15 h / 24 h window is due at once, but its run must wait 15 h / 24 h after the previous run: mixers idle while late lots wait (master mixers 40-48 % idle although 1,123 of 1,405 master lots are late). Fix needed: a late window joins the current run of the compound (one bigger run) instead of waiting. A crash in the first attempt (NS used before definition) was fixed and the run restarted.

---

## v48 — v47 + mixing catch-up + 901 ready-first (user, 2026-10-08) -- not a baseline

**Changes:**
- **Mixing catch-up (`mixing_v47.catch_up_merge`):** a compound lot that would start within the 15 h / 24 h gap of its compound's current run (a late window already due) joins that run directly behind it on the same mixer -- one bigger run -- when its inputs are ready and it stays within shelf life at its last use. Rules A / B still hold (one mixer, no repeat within the gap); the gap only holds back windows that are not due yet.
- **901 ready-first (`fourrc_rules.replan_after_repair: true`, level plan OFF):** after the repair 901 is re-planned on real input-ready times: whenever it is free it runs a READY lot -- same item, then same CTP_Master_FRC wire, then fabric during a creel reload, then earliest need. A lot whose compound is missing is skipped, not waited for. One go a day / whole spool / fabric bridge passes and the final repair run after it.
- **New report `FOURRC_SEQUENCE_REPORT_vNN.xlsx`:** 901 utilisation, idle hours by cause, every idle gap, and every same-wire sequencing break (a later creel load of a wire already loaded) with the reason per lot (compound not ready / shelf life / order). v42 measured: 52.5 % utilisation, 113.9 h idle (38.4 h waiting for compound while other lots were ready, 31.4 h held although ready, 28.1 h creel reloads, 16.1 h changeovers), 15 breaks all "compound not ready".
**Result: FAILED -- do not use.** Mixing rules all 100 % (one mixer per day 1,609 / 1,609, repeat gap 1,385 / 1,385, 01-Oct rules 100 %); catch-up joined 254 + 15 late lots. Output collapsed: built 20,535 (PCR 19,554 / TBR 981), cured 26,307 (v47 65,986, v42 82,719). 901 utilisation 37.6 % (v47 40.2 %, v42 52.5 %); 4RC one go 87.8 %, spool consumed 77.0 %, rule 3 70.6 % (the re-plan reorders whole spools / runs). Final mixers 73 / 98 batches per shift. PCR builds wait for steel belts (from 901 steel, started later by the re-plan) and inner liner; TBR (981, same as v47) waits for BEF001 / shoulder pads / apex beads -- compound-fed components. **Findings:** (1) the 901 ready-first re-plan hurts output (as in v45) -- revert; (2) mixing rules A + B as implemented choke compound supply: mixers 31-62 % busy while most compound lots are late. Rule A gives each compound ONE mixer for the WHOLE plan (stricter than the rule: one mixer per DAY), so an idle mixer can never take a compound assigned to a busy one.

---

## v49 — v42 4RC base + mixing rules (A per day, B + catch-up, final feed) + queue unblock (user, 2026-10-08) -- not a baseline

**Base (user):** v42 4-roll calender settings -- level loading, level plan, Deep Roll and the 901 re-plan OFF.
**Mixing:** rule A as stated -- one compound on ONE mixer per PLANT DAY, chosen at placement by load (may differ between days; butyl-group compounds stay on their block mixer) + a final safety-net pass (`mixing_v47.one_mixer_mode: day`); rule B 15 h / 24 h with the v48 catch-up (a late window joins the current run, also on another eligible mixer); final-mixer feed (v47); all 01-Oct rules.
**Queue unblock (`queue_unblock`):** on 901 and the belt cutters 802 / 801 the lots keep their planned order, but when the next lot's input is not ready within 30 min the machine runs the next READY lot (same item first; on 901 the 240 min creel is kept and filled with fabric / same-wire lots). 901 first, then the cutters, each followed by the final repair. Addresses the v42 / v48 finding that one waiting lot blocks every lot behind it.
**Result: FAILED -- do not use.** Built 20,634 (PCR 19,955 / TBR 679), cured 26,437. Mixing: 01-Oct rules 100 %, repeat gap 100 %, one mixer per day 99.4 % (9 of 1,531). Mixers busy only 18-38 % of the window (v47 30-62 %), 737 mixing lots in the window (v47 1,158), median mixing lot 244 h late (v47 66 h); F270 idle 69 h (9-12 Oct). 901 utilisation 24.8 % (170 h "held although ready" by the report's measure; its compounds are days late); 4RC one go 92.9 %, spool 76.7 %, rule 3 76.2 %. **Cause:** the post-placement passes (catch-up inserts 266 + 44 lots, day fix 52 + 21 + 16, queue unblock) each push the lots behind them later, and the repair is later-only and single-pass in start order, so pushes cascade (e.g. final T638_L0003 starts 12-Oct 05:12 while its master MT6381_L0003, pushed later afterwards, finishes 13-Oct 00:24). v48 and v49 share the catch-up and both fell to ~20.5 k built; v47 without it built 62.7 k. **Conclusion:** rules B / A must be built into PLACEMENT (a compound lot placed as part of its compound's run, never inside the gap) instead of being enforced afterwards by pushing.

---

## v50 — EXPERIMENT: v47 + shelf life (max aging) +30 % (user, 2026-10-09) -- not a baseline

**Base:** v47 settings exactly (v42 4RC; mixing rules A one mixer for the plan, B 15 h / 24 h, final feed; no catch-up, no queue unblock, no 901 re-plan).
**Change (`aging_relax`, build_sep_inputs.py):** every item's max aging from CTP_Master_Aging x 1.3 (e.g. 72 h -> 93.6 h, 24 h -> 31.2 h); green tyres kept at 72 h; min aging unchanged. Opening WIP stock that was over-age at 72 h becomes usable where it is younger than the new limit.
**Result:** built 55,094 (PCR 52,679 / TBR 2,415), cured 58,957 (PCR 55,466 / TBR 3,491) vs v47 62,730 / 65,986 (TBR cured 2,030). Mixing rules all 100 %; 4RC 11 / 13 checks at 100 % (creel not stacked 88.9 %, rule 3 93.3 %); 901 utilisation 39.2 % (v47 40.2 %); final mixers F270 85 / K310 79 batches per shift (v47 70 / 88). **Not a clean comparison:** the live masters changed between the v47 run (08-Oct) and this run (09-Oct) -- CTP_Master_Routing 46,811 -> 47,752 rows, CTP_Master_BOM 130,107 -> 131,897; compound demand +7 % (raw 7.64 -> 8.17 M kg). Control run v51 = v47 settings on the same (09-Oct) data, aging not relaxed.

---

## v51 — CONTROL for v50: v47 settings on the 09-Oct live data, aging NOT relaxed (2026-10-09) -- not a baseline

**Purpose:** separate the effect of +30 % shelf life (v50) from the change in the live masters since v47.
**Result:** built 57,568 (TBR 1,964), cured 62,247 (PCR 59,231 / TBR 3,016); mixing rules 100 % (one mixer per day 99.9 %); 4RC 9 / 13 at 100 % (creel 96.0, spool ends 94.7, one go 96.2, rule 3 84.2); 901 38.1 %; 1,322 green tyres scrapped. **Effect of +30 % shelf life (v50 vs v51, same data):** cured -3,290 (-5.3 %: PCR -3,765, TBR +475), built -2,474; compound demand -3 % (more opening stock usable); 4RC better (11 / 13 vs 9 / 13, rule 3 93.3 vs 84.2 %); 901 39.2 vs 38.1 %; GT scrapped 1,402 vs 1,322. **Conclusion: relaxing shelf life by 30 % does NOT improve output; it helps TBR and 4RC slightly but costs PCR.** Config: aging_relax off (plant values).

---

## v52 — v42 settings on the updated (09-Oct) live data, same plan files, opening WIP 06-Oct 07:00 (user, 2026-10-09) -- not a baseline

**Settings:** as v42 -- 01-Oct mixing rules, 4RC rules with wire campaigns / one creel by FRC wire; the 08-Oct mixing rules (`mixing_v47`: one mixer per day, 15 h / 24 h gap, final feed) OFF; level loading / level plan / Deep Roll / 901 re-plan / queue unblock / catch-up / aging relax OFF. Plans `latest_plan_file` (October PCR 6 / TBR 7), WIP `jkplanning_CTP.opening_wip` 06-Oct 07:00.
**Result:** built 71,435 (PCR 64,422 / TBR 7,013), cured 73,155 (PCR 65,134 / TBR 8,021); 1,240 green tyres scrapped. 01-Oct mixing rules 100 % inside the plan window (one IL day with two campaigns on 20-Oct, after the plan end: 8 / 9 overall); one mixer per day 90.2 %, 15 h / 24 h gap 73.5 % (not applied). 4RC 12 / 13 checks at 100 % (rule 3 83.3 %); 901 utilisation 51.9 %; final mixers F270 137 / K310 128 batches per shift. **Reading:** same settings as v42 lose 9,564 cured on the 09-Oct masters (v42 82,719 on 08-Oct data); on the same data the 08-Oct mixing rules cost 10,908 cured (v51 62,247 vs v52 73,155), mostly TBR (3,016 vs 8,021).

---

## v53 — v52 + 08-Oct mixing rules AT PLACEMENT + repair compaction (user, 2026-10-09) -- not a baseline

**Data:** 09-Oct live masters (updated BOM / routing), October plan files, opening WIP 06-Oct 07:00. 4RC as v42 / v52.
**Finding that led to it (v52):** at placement the mixers are 79-97 % busy (1,294 h); the passes after it (IL campaign room 798 lots, butyl day blocks 1,746 lots, each final repair ~4,000 upstream lots pushed later, 35-102 k h per pass) move 1,768 of 1,819 mixing lots later (median 51 h) and the later-only repair never fills the holes again -> 56-75 % busy (950 h).
**Changes:**
- **08-Oct rules at placement** (`mixing_v47.placement_aware`, `one_mixer_mode: day`): each run covers the next 15 h (24 h IL3 / IL5 / 4RC compounds) of demand; a compound lot either JOINS its compound's run (directly behind it, same mixer = same run, no changeover) or starts >= 15 h / 24 h after any run of it on any mixer; one mixer per compound per plant day, chosen by load at placement (may differ between days); final safety-net pass + gap floor in the repair. Catch-up, final feed and queue unblock OFF.
- **Repair compaction** (`repair_compaction`): the final repair may move a lot back towards its PLACEMENT start instead of only later -- machine order, changeovers, inputs, PM, creel, shelf life at its latest consumer and the aging floor kept; IL / butyl block lots and prep lots stay where the block passes put them. Build rows likewise (back towards their placement start, aging-valid kit kept).
**Result: FAILED -- do not use.** Built 45,456, cured 50,865 (TBR 2,443) vs v52 73,155. Mixing rules 100 % (one mixer per day 99.0 %, repeat gap 100 %); final mixers 67 / 65 batches per shift; 4RC 9 / 13. Cause: rule B at placement pushed every lot that could not join its compound's run 15-24 h later (most compounds are already late, and the mixer is rarely free right after the run) -> F270 / K310 already ~80 h idle at placement (131 h busy vs 202 h in v52); each repair then pushed ~4,000 upstream lots (75-270 k h); compaction moved only 8 lots earlier (bug: same-item units on the mixers were excluded).

---

## v54 — v52 data + 08-Oct rules by BACKLOG MERGE (one longer run) + working repair compaction (user, 2026-10-09) -- not a baseline

**Rule B as the user stated it:** a late compound runs the batches needed in a stretch straight after each other on the same mixer as ONE longer run, so its supply is ready for at least the next 15 h (24 h IL / 4RC) and it does not come back within that time. **Implementation (`mixing_v47.backlog_merge`, phase 2):** a capacity estimate per mixing department (its mixers in parallel, lots in due order, routing run time, not earlier than shelf life allows) gives each compound lot an estimated mixing start; consecutive lots of a compound whose estimated starts fall within 15 h / 24 h of the run's start are merged into one lot (run <= 24 h, shelf life at the last use kept; butyl-friendly not merged). Placement-time gap OFF; the repair's gap check stays as safety net. Rule A one mixer per plant day at placement (+ safety pass). **Repair compaction fixed:** same-compound units on the mixers may move earlier as a block; IL / butyl block lots and 901 lots stay; no latest-consumer check (aging floor kept).
**Result: FAILED -- do not use.** Built 27,394, cured 33,509 (TBR 4,208); mixing rules 100 % (one mixer per day 99.6 %, repeat gap 99.9 %); mixers busy 317 h in the window (v52 950 h); final mixers 36 / 43 batches per shift; 4RC 11 / 13. 318 lots merged into 1,785 runs. **Cause:** the scheduler makes a lot's material available only when the WHOLE lot is finished, so a merged run of up to 24 h releases the compound for its first users up to 24 h later than separate lots would; those users and everything after them are pushed (first repair 450,448 h of pushes vs 81,278 h in v52), and the repair's gap check still held 126-145 runs. Compaction moved only 130 lots earlier. **Lesson:** a longer run must stay SEPARATE lots placed back to back (each batch-lot usable when it finishes), not one big lot.

---

## v55 — v54 with RUN GROUPS: batches stay separate per need, run back to back, no changeover (user, 2026-10-09) -- not a baseline

**Rule B as the user restated it:** segregate the batches as per need, but do not do changeovers between them -- the goal is changeover-time reduction while providing at least 15 h (24 h IL / 4RC) to the next sections; the compound must not come back within that time.
**Implementation:** phase 2 (`mixing_v47.backlog_merge: group`): the same capacity estimate as v54 groups consecutive lots of a compound whose estimated mixing starts fall within 15 h / 24 h -- but the lots stay SEPARATE (each its own need, each usable when it finishes); butyl / IL compounds are left to the daily butyl blocks. Phase 5: the first member placed opens the run on the best mixer and RESERVES the other members' run time directly behind it; each later member fills its slot (at its inputs-ready time) on the same mixer -- no other compound between, no changeover. Rule A one mixer per plant day; repair gap check as safety net; repair compaction on; placement-time gap off.
**Result: FAILED -- do not use.** Built 43,825, cured 49,412 (TBR 3,346); mixing rules 100 % (one mixer per day 99.4 %, gap 100 %); mixers busy 534 h; final mixers 62 / 72 batches per shift; 4RC 10 / 13. 308 lots in 302 runs, but the later batches' inputs (masters) were often ready much later: 4,466 h waiting inside runs, 222 runs broken by another lot; the repair held 164-191 runs per pass (first repair 612,397 h of pushes). **Conclusion v47-v55:** at ~100 % mixer load with most compounds late, a hard 15 h / 24 h no-repeat rule always makes something wait. On the 09-Oct data: best output v52 (73,155; 08-Oct rules 90.2 / 73.5 %); best with the 08-Oct rules ~100 % v51 (62,247).

---

## v56 — MIXER-IMPROVEMENT PLANNER (stock-coverage mixing planner) on v42 rules (user, 2026-10-09) -- not a baseline

**Data:** BOM / routing from `latest_schedule/BOM_Routing_9oct` (config `master_files`), other masters live; plan files of 06-Oct (`latest_plan_file`); opening WIP 06-Oct 07:00.
**Logic (`mix_planner`, code `mixer-improvement/mix_planner.py`):** all compound lots (master + final) are planned FIRST as a forward simulation of the six mixers. Each time a mixer is free it runs the allowed compound needed soonest (masters ranked by their finals' cover window, so masters come ahead of finals); a run takes every lot of that compound needed in the next 15 h (24 h IL3 / IL5 / 4RC compounds), separate lots back to back with no changeover, within shelf life; the compound does not come back within 15 h / 24 h; one mixer per compound per plant day; daily butyl / IL blocks (friendly before / after, IL3 + IL5 in one go, 120 s clean-out after butyl, 120 s around finals on K430-2, TR100 master <-> final); friendly prep lots where no real friendly lot fits. Phase 5 then places everything else around these FIXED lots; the old mixer passes (IL campaign, butyl day blocks, BRM-09 joins, rule-repair moves on mixers) are off and the final repair never moves a planned lot. 4RC / extrusion rules as v42 / v52. 08-Oct `mixing_v47` switches off (the planner applies those rules); repair compaction off.
**Planner test (planner only, previous run's lots):** 2,188 compound lots planned, 30 butyl / IL blocks, 33 prep lots; mixers busy 1,310 h in the plan window (v47 665 h, v52 950 h).
**Result:** built 104,903, cured **104,483** (PCR 87,301 / TBR 17,183) -- v42 82,719, v52 73,155, v47 65,986; mixers busy 1,292 h in the window (v52 950 h); final mixers F270 187 / K310 193 batches per shift; 901 utilisation 64.7 % (v42 52.5 %); 119 green tyres scrapped (v52 1,240); one mixer per day 99.6 %, repeat gap 97.8 %, IL3 + IL5 one go 100 %, batches 100 %. **But the butyl sequence rules were broken:** Before Butyl 52.2 %, After Butyl 92.7 %, Master Butyl Sequence 20.6 %, TR100 8.3 %. Causes (fixed in v57): (1) the planner skipped the friendly compound when the only friendly item available was the one just used (B1 / B2 / MB-friendly had no lots or prep template); (2) the pre-repair BRM-09 join and the aging pull still moved planned mixing lots (0 s clean-outs); (3) TR100 used 5 min instead of changeover 1 min + TR100 5 min. 4RC 11 / 13 (spool ends 94.7 %, rule 3 77.8 %).

---

## v57 — v56 with the butyl-sequence fixes (user, 2026-10-09) -- not a baseline

**Fixes:** a friendly compound is always placed directly before / after every butyl compound (same friendly item reused or a prep lot when no other exists); planned mixing lots are excluded from the pre-repair BRM-09 join and the aging pull; TR100 = 1 min changeover + 5 min clean-out. Planner test: B99 directly before every 30G / IL3 / IL5 on F270; MB100 / MB200 / XMB98 directly before and after every 29MG / ML405 / ML605 on K430-2; mixers busy 1,301 h in the window.
**Result (best so far):** built 104,668, cured **104,759** (PCR 86,788 / TBR 17,972) vs v42 82,719 (+26.6 %), v52 73,155, v47 65,986. **All mixing rules 100 %** (Before Butyl 24 / 24, After Butyl 54 / 54, Master Butyl Sequence 31 / 31, IL3 + IL5 one go 12 / 12, batches 1,950 / 1,950, one mixer per day 1,514 / 1,514, no repeat within 15 h / 24 h 1,336 / 1,336); internal BRM-09 (one run per shift) 98.4 %. Mixers busy 1,300 h in the window (F270 200, K310 206, F440 221, K270M 221, K430-1 232, K430-2 220); final mixers 191 / 196 batches per shift (target 230). 901 utilisation 66.1 % (v42 52.5 %); 4RC 12 / 13 checks at 100 % (rule 3 steel <-> fabric 81.5 %). Extrusion family sequencing (BR-21 / 25 / 28 / 32, already partial in v42 / v52) lower on "family changes per plant day" (14-54 %) with the higher volume. 252 green tyres scrapped (v42 834, v52 1,240).

## v58 — v57 + 4RC creel fill + 4RC compound priority (user, 2026-10-09) -- not a baseline

**Rules added:** (1) `fourrc_rules.creel_fill`: every idle creel-reload window on 901 may take a WHOLE fabric dip load (all consecutive lots of one FRC wire) that bridges no other cord change, from that plant day or up to 2 days later, only when it fits without delaying the next steel lot, its inputs are ready, it does not age out before its last consumer and no item gets a second run that day; then the 901 lots behind move earlier (never later). (2) `mix_planner.fourrc_priority_h: 8`: the 4RC skim compounds and their masters are ranked 8 h earlier in the mixing planner (shelf-life floor still applies).
**Result:** built 104,999 (v57 104,668), cured **104,695** (v57 104,759; PCR 88,494 / TBR 16,200 vs 86,788 / 17,972). All mixing rules 100 % (one mixer per day 1,513 / 1,513, no repeat within 15 h / 24 h 1,321 / 1,321, butyl sequences 100 %); mixers busy 1,303 h. 901 utilisation **67.0 %** (v57 66.1 %), idle 79.3 h (creel reload not filled 26.7 h, held 36.0 h, changeovers 16.2 h); 4RC rule 3 **85.7 %** (24 / 28; v57 81.5 %), all other 4RC checks 100 %. 238 green tyres scrapped.
**Why the creel fill moved nothing:** over all unfilled windows only 17 fabric loads were within 2 days, and each was blocked by a rule: 8 already sit between another cord change (moving them only moves the gap), 6 would age out before their consumer if made that early, 2 have inputs not ready in the window, 1 is longer than the window. The remaining unfilled creel windows can only be filled by breaking shelf life or by more fabric demand / earlier fabric inputs -- the gain in v58 came from the planner priority and the v40 bridge (3 runs moved).

## v59 — v58 + whole-spool top-up used as stock + wider wire campaigns (user, 2026-10-09) -- not a baseline

**Why:** in v58, 901 made 59 % more metres than demanded inside the window (fabric +41 %, steel +74 %, ~60 h of 901 time): the 4RC rule "spool / dip entirely consumed before the change" lengthens every run to a whole spool / dip, and the extra metres were never counted against the item's later demand.
**Rules added:** (1) `fourrc_rules.spool_netting`: the top-up of a run is STOCK of that item; the item's later lots take from it first (oldest stock first, per consumer: at least min aging before and within max aging - 4 h of each consumer start); a covered lot is cut (never below the CTP_Master_FRC min roll) or dropped, and those consumers draw from the earlier lot. A change is refused when it would split an item-day (one go a day) or leave an item's total with a residual below the min roll (4RC-6). (2) `fourrc_rules.wire_campaign.max_days` 3 -> 5 (a wire load may take that wire's lots of the next 5 days when inputs are ready and within shelf life). Checker fix: `general_rules_checks.py` BR-5 uses the phase-2 lot quantity as base for cut 901 lots.
**Result:** built **107,118** (v57 104,668), cured **106,688** (v57 104,759, +1,929; PCR 90,033 / TBR 16,656). 901: 7 lots dropped, 54 cut, 65,957 m taken from spool stock; made in the window 310.6 km (fabric 110.6 / steel 200.0) vs 339.0 km in v58; 901 running 144.5 h (utilisation 60.2 % -- lower by design: ~16 h less spent on metres nobody needs). Option 2 added nothing: only 12 lots joined (271 later lots had inputs not ready, 14 would age out). All mixing rules 100 % (one mixer per day 1,513, no repeat within 15 h / 24 h 1,321, butyl sequences, IL3 + IL5 one go); 4RC: all checks 100 % except rule 3 steel <-> fabric **87.0 %** (v57 81.5 %); rule adherence report 71 PASS / 6 PARTIAL / 5 FAIL (same as v57); extrusion family sequencing BR-21 / 25 / 32 up, BR-28 day check down (35.7 -> 23.1 %). 159 green tyres scrapped (v57 252).

## v60 — v59 + no idle inside a dip / spool load + B616M priority (user, 2026-10-09) -- not a baseline

**Rules added:** (1) `fourrc_rules.load_compact`: inside a same-wire load on 901 -- PULL the next lot right behind the previous one (inputs ready, max aging - 4 h at every consumer, PM, one go a day; only earlier); FILL a gap left inside a load with a whole fabric dip load from later only when the two parts fall on different plant days (part 2 may start at the next 07:00 if no consumer is delayed); HOLD: idle in the MIDDLE of a dip / spool (next lot's compound not ready) is removed by starting that dip later so it runs in one go (order / quantities unchanged, PM, no new item-day split; consumers re-timed). 4RC fix-up rounds 2 -> 4. (2) `mix_planner.item_priority_h: {B616M: 8}` (CAP 66 compound, extra to the 4RC priority). (3) New check **4RC-10** in `fourrc_checks.py`: idle inside a load at a dip / spool boundary (report) and in the middle of a dip / spool (scored).
**Result:** cured **107,314** (v59 106,688, +626; PCR 90,667 / TBR 16,646), built 105,963 (v59 107,118). Idle inside loads 7.2 h, all at a dip boundary (v59 38.1 h); mid-dip idle 0 (2 dips held, 12.9 h removed); pull / fill moved nothing (blocked by max aging at a consumer -- CAP 66 24 h). 901 running 146.0 h (60.8 %). All mixing rules 100 %; 4RC all 100 % (incl. 4RC-10) except rule 3 steel <-> fabric 87.5 % (21 / 24); rule adherence 72 PASS / 6 PARTIAL / 5 FAIL. 102 green tyres scrapped (v59 159).

## v61 — v60 + butyl blocks as the plant runs them (user, 2026-10-10) -- not a baseline

**Rules changed (user decision 10-Oct):** (1) butyl-friendly at block level: K430-2 friendly -> 29MG -> ML405 -> ML605 -> friendly (no friendly between the butyl masters); F270 friendly -> 30G / IL3 -> IL5 (butyl compounds together). The 120 s clean-out is kept after every butyl compound (except IL3 -> IL5, one campaign) -- the plant's own gaps between butyl compounds are ~1.5-6 min. (2) Butyl masters made in the evening (`butyl_day_block.groups.master.earliest_hour: 17`) and aged overnight (max aging 1 day kept). (3) F270's butyl block starts only when all its butyl masters are aged (`start_when_ready`); F270 runs other finals until then. Checks moved to block level as decided: `mixing_rule_audit.py` M1 / M3 and `mixing_compliance.py` BR-V2 prep in front / 3.1.3 (`butyl_day_block.friendly_mode: block`).
**Result:** cured **105,953** (v60 107,314, -1,361; PCR 88,034 / TBR 17,919 vs 90,667 / 16,646), built **107,081** (v60 105,963, +1,118). Mixing rules all 100 % (block level); F270 waits inside the butyl block gone, F270 shift A 195 batches (v60 153), F270 busy 208 h (v60 197) but K310 200 h (v60 211) -- final mixers together unchanged (~408 h); final batches in the window 11,646 (v60 11,569); finals behind the plan need by ~940 batches at plan end (v60 ~1,040). 901 utilisation 64.8 % (v60 60.8 %); 4RC all 100 % except rule 3 85.2 %. Rule adherence 73 PASS / 5 PARTIAL / 5 FAIL. 234 green tyres scrapped (v60 102). The F270 time freed is taken from K310 (the finals are limited by master arrival and the demand peaks), so the gain shows in TBR and built, not in total cured.

## v62 — v61 + load levelling of the mixing demand peaks (user, 2026-10-10) -- not a baseline

**Rule added:** `mix_planner.level_load`: a backward finite-capacity pass over the final mixers (2), then the master mixers (4), from the latest need back; when a day's need exceeds what the mixers can make that day, the excess gets an earlier effective need (urgency only) so it is made on the days before. The shelf-life floor still applies; the 15 h / 24 h cover runs use the real need; all butyl / mixing rules unchanged (v61 block-level butyl).
**Result:** cured **107,223** (v61 105,953, +1,270; v60 107,314), built **109,487** (highest so far; v61 107,081, v60 105,963); PCR 88,987 / TBR **18,235**. Mixing rules all 100 % (0 violations); mixers busy 1,299 h in the window (F270 205, K310 202, F440 222, K270M 216, K430-1 230, K430-2 223); final batches per shift F270 194 / K310 194; 901 utilisation 62.6 %; 4RC all 100 % except rule 3 84.6 %; rule adherence 73 PASS / 5 PARTIAL / 5 FAIL; 166 green tyres scrapped. Planner log: from 08-Oct the final mixers wait only 1-2 h a day, so the peaks cannot be made much earlier -- the gain is from the right compounds earlier (built +2,406), not from more mixer hours. Remaining limits: final-mixer capacity (~1,300 batches / day vs peaks ~2,370) and start-up master stock.

## v63 / v64 / v65 — tests for stable daily PCR / TBR (user, 2026-10-10) -- not baselines

All three keep every mixing rule at 100 % (0 violations) and every 4RC check at 100 % except rule 3 (steel <-> fabric, 84-89 %).
- **v63** = v62 + (A) planner cover runs 20 h / 30 h with the repeat gap kept at 15 h / 24 h (`mix_planner.gap_h / gap_h_long`), (B) compound lots sized to ~8 h (final) / 12 h (master) of need (`smooth_lot_span_h_by_itype`, phase 2), (C) upstream left-shift ON (`compress_upstream`, mixers and 901 excluded via `compress_exclude_machines`). Cured PCR 77,929 / TBR 17,923 on the plan days (v62 88,995 / 18,236) -- worse.
- **v64** = v62 + (B) + (C). Cured PCR 83,069 / TBR **21,221**: (A) costs ~5 k PCR (bigger runs on full mixers make the next compound wait); (B) costs ~6 k PCR (lots x2, +2-3 % batches from whole-batch / 5-batch rounding) but helps TBR.
- **v65** = v62 + (C) only (cover 15 h / 24 h, lot sizes as v62). Cured PCR **93,771** / TBR 18,206 on the plan days (v62 88,995 / 18,236), built PCR 94,618. Left-shift: 27 upstream lots moved earlier (53 h). Rule adherence 73 PASS / 5 PARTIAL / 5 FAIL (same as v62).
Config now = v65 (A and B off, C on).

## v66 — v65 + live master priority (user, 2026-10-10) -- not a baseline

**Rule tested:** `mix_planner.starve_boost`: when F270 / K310 run out of planned work within 3 h, the masters blocking the 6 most urgent waiting final lots get 48 h more urgency on the master mixers (order only; all mixing rules checked as before).
**Result:** cured 110,352 (v65 111,969; PCR 93,400 / TBR 16,952), built 110,874. Mixing rules 100 % (0 violations); 4RC all 100 % except rule 3 (25 / 28). The 09-Oct PCR dip is gone (2,550 -> 12,386 built) but 10-12 Oct lose 2,100-4,200 PCR a day and TBR -1,250: the master mixers are full, so the priority only moves the shortage in time. **Switched off again (config = v65).** Master capacity (routing cycle times / batch weights) and opening master stock are the remaining levers -- data, not scheduling.

## v67 — v65 + butyl clean-out only towards other compounds (user, 2026-10-10) -- not a baseline

**Rule changed (user 10-Oct):** butyl compounds do not react with each other or with a butyl-friendly: butyl -> butyl and butyl -> friendly take the normal 60 s changeover; the 120 s clean-out (in place of the changeover) only when a butyl compound is followed by a compound that is neither butyl nor butyl-friendly. `mix_planner.butyl_cleanout_to: other`; `business_rules.butyl_sequence_rule.targets_count_as_prep: true`; audit M2 rule text and check updated.
**Result:** mixing rules 100 % (0 violations; After Butyl 12 / 12 clean-outs, Before Butyl 24 / 24, Master Butyl Sequence 31 / 31, BRM-03 100 %); 4RC all 100 % except rule 3 (24 / 29). Mixers busy 1,300 h (v65 1,298). Cured 107,505 (PCR 90,532 / TBR 16,973), built 106,182 -- v65 111,969. The rule saves only minutes of mixer time; the -4.5 k comes from the changed order / timing of compound lots propagating downstream (building cascade), i.e. the run-to-run spread of the schedule for near-identical mixing plans is about +-4 %. Config keeps this rule (it is the plant rule).
(Later on 10-Oct the user asked to move back to v65: config butyl clean-out back to `any`, `targets_count_as_prep: false`.)

## v68 — v65 + 901-first compound timing + fabric bridge 2 days (user, 2026-10-10) -- not a baseline

**Scheduling changes:** (1) `mix_planner.fourrc_due`: each skim-compound lot is ranked by when its 901 lot must start (901 lot need - 901 run time - 4 h) instead of its own phase-2 first need (which was up to 24-80 h later than a 901 lot it feeds); masters follow through the planner's master urgency; replaces the flat 8 h 4RC bonus where a due exists (83 lots). (2) `fourrc_rules.wire_campaign.bridge_days` 1 -> 2 (a creel reload may take a whole fabric dip load up to 2 plant days ahead, within shelf life).
**Result:** cured 109,111 (v65 111,969; PCR 90,945 / TBR 18,166), built 110,166. Mixing 0 violations; 4RC all 100 % except rule 3 (23 / 27). 901: 150.6 h running (62.8 %; v65 146.5 h / 61.1 %), idle 89.4 h (creel reload 34.8, held 41.6, changeover 13.0), 22 steel creel loads, 107 of 114 lots finish after their need (median 49.6 h late). Mixers 1,302 h. **No gain** -- the master / final mixers are full, so ranking skim compounds earlier only moves the shortage; skim compound is ready a median ~50 h after the 901 need for ~80 % of 901 lots, and 901 then starts a median ~1 day after its compound is ready (901 day-plan sequencing). **Config set back to v65.**

## v69 — v65 + five algorithm changes (user, 2026-10-10) -- not a baseline

**Changes:** (1) `repair_compaction` ON (lots and build rows pushed late may come back towards their placement start) + 901 pulled earlier every 4RC round (`creel_fill.compact_always`); (2) `batch_release`: compound lots usable batch by batch (+ min aging), consumers in need order (4,973 edges); (3) `mix_planner.closed_loop`: phase 5 + 6 re-run with the mixers re-planned on round-1 real needs (1,732 compound lots); (4) `compress_alt_machine_departments`: bead lots may move to another allowed bead machine (2 moved); (5) `curing_aware_building`: rows whose tyres would wait > 64 h for a press go behind.
**Result:** cured 111,814 (v65 111,969; PCR 92,706 / TBR **19,108** vs 93,763 / 18,206), built 112,762. Mixing 0 violations; 4RC all 100 % except rule 3 (21 / 23); rule adherence 73 PASS / 5 PARTIAL / 5 FAIL (same as v65). 901 utilisation 64.2 % (v65 61.1 %); green tyres scrapped 179 (v65 201). Compaction moved only ~190 lots (~450 h) back -- most late lots are held by shelf life at their late building rows. Total flat (within the +-4 % spread): the changes shift output between days / lines (TBR +900, PCR -1,050; PCR dip moved from 09-Oct to 12-Oct). Mixing capacity and the empty start remain the limit.

## v70 — v69 + PCR bead rules + cutter / slitter rules (user, 2026-10-10) -- not a baseline

**Rules added:** `pcr_bead_rules` (BR-61): bead winding 1201 grouped per plant day by spool / former inch (14 / 16 / 18 / 20 in, from CTP_Master_ChangeoverMatrix 'PCR Bead Winding' types), then cheapest matrix order; bead apexing 201 / 202 grouped by the inch of their bead. `cutter_rules` (BR-50 / BR-52): belt cutters 801 / 802 -- a building SKU's belt kit (BOM: PCR Belt-1 + Belt-2; TBR B1..B4 + L / R STC) cut together per plant day. `cutter_shift_merge` (BR-56): same item one block per shift on 801 / 802 / 2101 / 2001 / 1103 / 1104 / 2401 / 2402 / 601 / 701 / 702, strict (no consumer later). BR-58 / 59 / 60 and BR-49 / 51 / 53 / 55 / 57 were already met (routing machines, routing rates, matrix changeovers, BOM precedence). All passes use the per-day window checks (inputs ready, shelf life, before the first consumer, <= 12 h move, day not longer, changeover must fall).
**Result:** cured 112,368 (v69 111,814; PCR 93,254 / TBR 19,113), built 113,058. Mixing 0 violations; 4RC all 100 % except rule 3 (21 / 23); rule adherence 73 PASS / 5 PARTIAL / 5 FAIL. 1201 inch switches 83 -> 46 (minimum 23), changeover 2,726 -> 2,299 min; 201 / 202 inch switches 76 -> 49 / 79 -> 67; 801 belt-kit switches 340 -> 298, Belt-1 with its Belt-2 within 1 h 193 -> 201 of 261; 802 kit switches 217 -> 189, kit spread median 18.0 -> 16.2 h; BR-56 one block per shift 1,401 / 1,518 -> 1,408 / 1,508 (103 splits left: joining them would delay a consumer). Bead / belt lots finishing after need: 1201 100 -> 93, 201 114 -> 109, 801 507 -> 501. 219 green tyres scrapped (v69 179).

## v71 — v70 + BR-41 compound grouping + BR-66 / BR-67 kit sync (user, 2026-10-10) -- not a baseline

**Rules added:** `calender_rules` (BR-41): per plant day 1101 / 2201 grouped by compound set, 1901 by TIL / SQ / EG type then compound. `kit_sync` (BR-66 / BR-67): every upstream machine except mixers and 901 runs urgent-first by its consumers' deadline, and every per-day pass (extruders, bead, cutters, calenders) accepts a day order with fewer lots late for their consumer. Not applied (need data): TBR bead inch rules BR-63 / 64 / 65, BR-40 blank RHC pairs, BR-39 per-code IL speed / Expansion Work Out.
**Result:** cured 110,317 (v70 112,368; PCR 90,757 / TBR 19,560), built 113,922. Mixing 0 violations; 4RC all 100 % except rule 3 (24 / 27); rule adherence 73 PASS / 4 PARTIAL / 6 FAIL (Triplex BR-28 coverage 81 -> 73 %). BR-41: 1101 compound-set switches 70 -> 63 (minimum 32), 2201 68 -> 66, 1901 unchanged (14, minimum 9: no day could be re-ordered within its windows). Kit sync changed little: upstream lots after their need 3,125 -> 3,112, pre-assembly late 77 / 82 (same), kit-late building rows PCR 355 -> 348, TBR 418 (same). Urgent-first conflicts with the grouping rules: 1201 inch switches 46 -> 56, extruder family checks BR-21 / 25 / 28 / 32 lower. Building never starts without its full kit (0 rows). 198 green tyres scrapped.

## v72 — v70 + BR-41 compound grouping, kit sync OFF (user, 2026-10-10) -- not a baseline

**Rules:** all v70 rules (mixing, 4RC, PCR bead BR-58..61, cutters / slitters BR-49..57) + `calender_rules` BR-41 (1101 / 2201 compound set, 1901 type block then compound); `kit_sync` OFF. Not applied (need data): BR-63 / 64 / 65, BR-40 blank RHC pairs, BR-39.
**Result:** cured 111,332 (v70 112,368, v71 110,317; PCR 91,130 / TBR **20,202**), built **114,316** (highest so far; PCR 93,730 / TBR 20,586). Mixing 0 violations; 4RC all 100 % except rule 3 (21 / 23); rule adherence 73 PASS / 4 PARTIAL / 6 FAIL (Triplex BR-28 coverage 81.4 -> 78.3 %, now under the 80 % line). BR-41: 1101 compound-set switches 70 -> 60 (minimum 30), 2201 68 -> 65, 1901 14 (minimum 9, unchanged). BR-61 1201 inch switches 49 (minimum 20). Building never starts without its full kit. PCR dip moved to 09-Oct (1,506 built); TBR steadier (1,342-3,233 from 11-Oct).

## v73 — v72 + extruder / bead corrections (user, 2026-10-10) -- not a baseline

**Changes:** (1) `bottleneck_release.itypes` + Tread, SideWall, Bead Apex (start as soon as inputs are ready, as early as shelf life allows -- PCR one shift ahead, TBR bottlenecks early); (2) `mix_planner.item_priority_h` +8 h for B1144, S54, B715, T639, B606, R5606, A1133 (TBR bottleneck compounds); (3) `tbr_bead_chain`: wound bead + its apex grouped per plant day on 2403-2405.
**Result:** cured 107,896 (v72 111,332; PCR 90,937 / TBR **16,959** vs 20,202), built 110,077. Mixing 0 violations; 4RC rule 3 78.6 % (v72 91.3 %), other 4RC 100 %. PCR about the same (1002 lots late 173 -> 169, 1802 / 1801 busier); TBR worse: bead 2403-2405 busy 71 / 61 / 61 % -> 57 / 49 / 53 %, idle 54-74 h -> 90-108 h, bead lots late a median 110-118 h (v72 69-82 h); TBR building delay up to 142 h. Starting TBR bead apex at its shelf-life floor (plan need - 22 h) and the bead-chain grouping took bead machine time from the lots building waited for. **Not kept; config to be set back to v72.**

## v74 — v72 + TBR bead wound and apexed in one step (user, 2026-10-11) -- not a baseline

**Change:** `tbr_bead_one_step` (apply_ctb_approximations.py): as the plant TDA does (tda_schedule.csv, TDA1/2/3 = 2403/2404/2405, every booking consumes bead wire + S54 together with the apex compounds), the wound bead TBWB* is no longer a separate lot: S54 and bead wire 1040S28600 are direct inputs of the apexed bead TBAPBD* (same quantity per bead), 220 TBWB routing rows dropped, S54 feeds TBAPBD. Apexed-bead routing rate unchanged (3 NOS/min, efficiency 1 as everywhere -- `efficiency_override: 1.0`). All other config = v72.
**Result:** cured 110,423 to 16-Oct (v72 111,343; PCR 90,415 / TBR 20,008 vs 91,139 / 20,204), built 111,055 (PCR 90,578 / TBR 20,477). Within the +-4 % run-to-run spread. 2403 / 2404 / 2405 busy 70 / 61 / 61 % -> 36 / 33 / 34 % (no winding time, 222.7 h freed), apexed beads made 42,847 -> 44,933; TBR building rows held by bead apex 144 -> 40 (12,421 -> 3,647 tyres, median wait 80 -> 34 h) -- but the TBR wait moves to the steel belt edge strip BEF001 on 1802 (155 -> 247 rows, 13,157 -> 20,662 tyres, median 114 h), so TBR output does not rise. Mixing rules 100 % (same as v72); 4RC all 100 % except rule 3 21 / 25 (v72 21 / 23); rule adherence 73 PASS / 4 PARTIAL / 6 FAIL (same as v72); 212 green tyres scrapped (v72 219).

## v75 — v74 + B1144 a day ahead for the belt edge filler 1802 (user, 2026-10-11) -- not a baseline

**Change:** as the plant (beltedgefiller_schedule.xlsx: 1802 never waits, B1144 used at median 27 h old), `mix_planner.item_priority_h` B1144 / B186 / MB185 / MB184 / MB183 = 24 h (ranked a day ahead; shelf-life floor kept) and `mixing_v47.long_items` + B1144 (each run covers 24 h of need, 24 h repeat gap). All other config = v74.
**Result:** cured 111,679 to 16-Oct (v74 110,423, v72 111,343; PCR 90,590 / TBR **21,089** vs 90,415 / 20,008), built 111,662. 1802 busy 70 -> 83 %, edge strip 131.1 -> 155.4 km, idle gaps 6 (64 h) -> 5 (41 h; the two big ones are start-up 08-Oct 11.3 h and 09/10-Oct 14.6 h after a 15-batch B1144 run); B1144 11 runs / 349 batches (v74 9 / 263). TBR rows held by the edge strip 247 -> 60 (20,662 -> 5,008 tyres); TBR now waits mainly for inner liner (111 rows) and steel belt (82). Mixing rules as v74 (BRM-09 7 not followed, same as v74), 4RC 100 % (rule 3 included), rule adherence 74 PASS / 3 PARTIAL / 6 FAIL; 191 green tyres scrapped (v74 212).

## v75_relaxed — EVALUATION ONLY: v75 + plant mixing cycle times / batch weights (user, 2026-10-11) -- not kept, v75 stays best

**Change (data relaxation, config `relaxed_mixing`, ON only for this run):** per compound the more favourable of routing and the plant 08-Oct measurement (`latest_schedule/plant-schedule/build_relaxed_mixing.py` -> `relaxed_mixing_overrides.csv`): 77 measured compounds take the plant cycle + batch weight when the plant's kg/h is higher (median +15 %), 144 unmeasured take routing x the mixer's plant ratio (0.90-0.93), 40 unchanged; batch weight never lowered. 1802 kept at the routing 13 m/min (user). MPQ and all rules as v75.
**Result:** cured 118,815 to 16-Oct (**70.1 %** of 169,570; v75 111,679 = 65.9 %), built 120,334 (72.0 %). PCR cured 98,683 (+8,093; 73.9 %), TBR 20,132 (-957; 55.8 %; 13-Oct TBR dip 292 built -- steel belt / rubberized ply / edge strip). Mixers 79-91 % busy (v75 83-95 %). MPQ: 0 lots below MPQ; mixing BRM-09 5 not followed (v75 7); 4RC rule 3 23 / 25, 4RC-6 residual 18 / 19; rule adherence 72 PASS / 5 PARTIAL / 6 FAIL; 223 green tyres scrapped. (rule_adherence_report.py now treats version names like v75_relaxed as v75 for not-scored rules.)

## v76 — v75 + full upstream / downstream integration (user, 2026-10-11) -- not a baseline

**Changes:** (1) `fourrc_rules.day_plan.whole_spool_topup: false` -- the 4RC makes the demanded metres only (as the plant, which changed cord at 2.34 spools); rule 4RC-8 (spool entirely consumed) is no longer enforced (v75 made 446.9 km for 282.0 km of demand, 117 km never used, its compound never mixed). (2) `mix_planner.closed_loop.qty_feedback: true` (run_pipeline._qty_feedback) -- quantity phase 5 makes beyond phase 2 (prep runs, any 4RC extra) is exploded through the scheduler's own inputs (DAG edges / BOM) into the feeding finals and masters before round 2 (v76: +42 t on 13 compound lots). (3) Workbook genealogy (strict_links.py / make_schedule_v18.py): aging read with its unit (3 Days was 3 h), upstream lot inputs from the scheduler's DAG quantities, green tyres curable at emission + 90 min transfer (as phase 6 S6), stock past shelf life linked as EXPIRED_STOCK (aging is a report) instead of a false SHORT. Shelf life stays report-only (user).
**Result:** cured 114,322 to 16-Oct (67.4 %; v75 111,679), built 114,678 (68.7 %); PCR cured 93,434 / TBR 20,888. 901 makes exactly the demand (281,957 m), busy 66 -> 41 %. Genealogy: consumer lots with a SHORT input 6,257 (v75 as published) -> 521 (curing 151, building 38, upstream 332); remaining SHORT = BOM unit errors (BPG01 -> BPGSL01 368 t, CAP 66 - CAPSTRIP 72 k, TEG-02 / EG01) and ~7-9 t master gaps; 1,335 consumer lots draw stock past max aging (reported). Mixing as v75 (BRM-09 7), MPQ 0 below, 4RC rule 3 26 / 28 (4RC-8 not scored), rule adherence 72 PASS / 5 PARTIAL / 5 FAIL; 157 green tyres scrapped.

## v77 — v76 + building waits for every input, cap-strip opening alias, 3 closed-loop rounds (user, 2026-10-11) -- not a baseline

**Changes:** (1) `kit_gate_assume_available: []` -- building is gated on cap strip, mother roll, edge gum, wrap and gum strip too (v29-v76 assumed them available, after the plant planner's "building never waits for cap strip / edge gum"); (2) `inventory_item_alias` "CAP 66|Cap Strip" -> "CAP 66 - CAPSTRIP" (8,610 m opening cap strip was under the plant code); (3) `mix_planner.closed_loop.rounds: 3` -- quantity feedback also after round 2 (+42 t, then +84 t).
**Result:** cured 114,119 to 16-Oct (67.3 %; v76 114,322), built 114,972 (68.8 %); PCR cured 92,970 / TBR 21,149. PCR 06-Oct built only 116 (v76 6,855): with cap strip gated, day 1 waits for cap strip (opening 8.6 km vs ~46 m per PCR tyre), recovered from 07-Oct; PCR 09-Oct 2,533. Genealogy: consumer lots with a SHORT input 470 (building 17, all < 1 unit; curing 135; upstream 318); remaining SHORT = BPG01 -> BPGSL01 BOM error (368 km) and ~2 % master compound (butyl-friendly prep runs re-made each round: MB200, MB94, MB208...). Scheduler ledger proof: 20 build blocks before GUM STRIP exists, all on 20-21 Oct (after the window). Mixing BRM-09 10 not followed, MPQ 0 below, 4RC rule 3 23 / 27, rule adherence 71 PASS / 5 PARTIAL / 6 FAIL, 157 green tyres scrapped.

## v78 — v77 rules + BRBC-21 / BRBC-27 on the 10-Oct plan (user, 2026-10-11) -- not a baseline

**Run:** plan `plan_files_10oct` (PCR 12 / TBR 13), opening WIP CTP_Opening_WIP 10-Oct 06:32 (12,885 rows, no compound rows), window 10-Oct 07:00 -> 20-Oct 07:00 (`run_v15.py --wip-date 2026-10-10 --plan-end "2026-10-20 07:00" --plan-dir plan_files_10oct --name v78`). A first v78 on the 06-Oct plan was stopped (user) -> `latest_schedule/v78_stopped_06oct`.
**Changes vs v77:** `gt_transfer_time_min: 120` (Algo Planning BRBC-21: green tyre 2 h before curing) and `curing_keep_changeover: true` (phase6: the plan's mould-change minutes move with the SKU sequence when cure blocks slide; BRBC-27).
**Result:** plan 173,002 build / 173,324 cure; built 119,108 (68.8 %), cured 119,124 (68.7 %); PCR built / cured 100,465 / 100,380 (72.7 %), TBR 18,643 / 18,744 (53.6 / 53.1 %). BRBC-27 100 % (v77 46-50 %), BRBC-21 PCR 80 % / TBR 93 % (plan 72 / 61 %). Genealogy: 466 consumer lots with a SHORT input (building 12, curing 165, upstream 289). New plan's own data issues: 15 building rows on TBMs not in routing (B1 98.9 %), 239 cure blocks on presses not in routing (C1 95.1 %), 21 PCR rim changes > 1 inch, puncture guard > 40 / day on 4 days. Mixing BRM-09 7, MPQ 0 below, 4RC creel 26 / 27, rule 3 20 / 26, rule adherence 67 PASS / 7 PARTIAL / 7 FAIL, 320 green tyres scrapped.

## v79 — v78 + cap strip / edge gum / edge strip / steel belt edge strip assumed present for the first 24 h (user, 2026-10-12) -- not a baseline

**Change:** `kit_gate_assume_first_h` (phase5): build rows planned before plan start + 24 h (11-Oct 07:00) are not held for, and do not draw, items of type Cap Strip / Edge Strip / Steel Belt Edge Strip (edge gum EG0x / EGSL0x / TEG are typed Steel Belt Edge Strip; BEF001) + code patterns; every later row is gated as in v78. Same plan (`plan_files_10oct`), WIP 10-Oct 07:00, window 10-20 Oct, all v78 rules.
**Result:** day 1 PCR built 6,179 (v78 1,417), cured 8,882 (v78 4,793); TBR day 1 unchanged (373 built: TBR day 1 waits for tread / inner liner / bead apex, not these items). 10-day total lower: built 111,886 (64.7 %; v78 119,108), cured 113,040 (65.2 %; v78 119,124) -- PCR cured 95,889 / TBR 17,151. Day-1 kit-late rows still 62 of 77, now held by tread 3,883 / sidewall 3,835 / inner liner 2,365 / bead apex 1,996 / ply 1,122 tyres. 420 green tyres scrapped (v78 320). Mixing BRM-09 8, MPQ 0 below, 4RC rule 3 20 / 24, adherence 67 / 7 / 7; genealogy SHORT consumer lots 498.

## v80 — v79 + the plan generator's WIP assumptions (CTB_WIP_ASSUMPTIONS_v2.xlsx) (user, 2026-10-12) -- not a baseline

**Run:** plan `plan_files_10oct`, WIP snapshot 10-Oct 06:30 (CTP_Opening_WIP 06:32), window 10-20 Oct, all v78 / v79 rules.
**Changes (`ctb_wip_assumptions`, apply_ctb_approximations.py + phase5 `kit_gate_assume_first_h.codes_file`):** #11 expired opening stock usable 24 h from t0 (3,472 lots / 715,099 units, mostly compound); #2 one day of measured upstream flow (sheet 5, SMART TBM consumption) added as stock at t0 for 256 components (225,761 units); #1 406 building components absent from the WIP feed (sheet 6b, semi-finished, no compounds, no flow rate) assumed available for rows planned in the first 24 h; #3 / 13 / 14 strip items free 24 h (v79).
**Result:** built 123,090 (71.1 %), cured 124,413 (71.8 %) -- best on the 10-Oct plan (v78 119,124, v79 113,040); PCR cured 102,862 (74.5 %), TBR 21,551 (61.1 %). Day 1: PCR built 12,918 / plan 12,308, TBR 3,019 / 2,893 (v78 1,417 / 373); day-1 kit-late rows 17 of 77 (v78 62). Dip once the assumed day-1 supply is used: PCR 14-Oct 3,048 built, TBR 14-Oct 154 / 19-Oct 888. 586 green tyres scrapped (v78 320). Mixing 4 not followed, MPQ 0 below, 4RC creel 24 / 25, rule 3 22 / 25, adherence 67 / 9 / 5, genealogy SHORT consumer lots 469.

## v81 — v80 + upstream gap fill (A1 / A2) and no day-1 pull-forward (A4) (user, 2026-10-12) -- not a baseline

**Changes** (`latest_schedule/v80/GAP_12_15_OCT_v80.md`): A1 `upstream_gap_fill.floor_to_ready` -- the final repair may move a component lot (not mixers / 901) back to its ready time (inputs + min aging, machine predecessor + matrix changeover, PM, shelf life at the consumer; machine sequence kept) + one extra repair at the end; A2 `upstream_gap_fill.repeg_compounds` -- compound batches re-allocated FEFO to component lots (33 compounds / 1,805 links; totals per producer and per consumer unchanged); A4 `building_plan_feasible.no_pull_first_h: 48`. A3 (time-limited netting) not needed: phase 1c TP-MRP already nets each opening lot FEFO only against demand within its life.
**Result:** built 126,589 (73.2 %), cured 127,021 (73.3 %; v80 124,413, +2.1 %); PCR cured 105,693 (76.6 %, +2,831), TBR 21,328 (60.4 %, -223). 12-15 Oct: PCR built 34,128 (v80 29,411), cured 31,208 (27,878); TBR unchanged (6,026 / 6,322). The PCR dip moves from 14-Oct (3,048 -> 11,486) to 13-Oct (9,935 -> 2,624). Component-machine idle on 11-15 Oct barely falls (e.g. 1002 55 -> 52 h, 801 81 -> 75 h): with the machine sequence kept, a lot still waits behind a predecessor that waits for compound. Rules: mixing BRM-09 4 (= v80), MPQ 0 below, 4RC creel 100 % (v80 96 %), rule 3 21 / 24, build / cure validation = v80, adherence 68 PASS / 7 PARTIAL / 6 FAIL (v80 67 / 9 / 5); per-day extruder family checks lower: BR-25 1801 plant days 84.6 -> 76.9 %, BR-28 1001 42.9 -> 25.0 %; 708 green tyres scrapped (v80 586).

## v82 — v81 + same-family gap fill on component machines (user, 2026-10-12) -- not a baseline

**Change:** `upstream_gap_fill.family_reorder` (phase5, before the last repair): an idle gap (>= 30 min) between A and B on a component machine (not mixers / 901) takes a later lot X of the same compound family as A or B (same item on BR-56 cutters / slitters), earliest-due first, when X's inputs are ready, it fits with the matrix changeover both sides, PM and its shelf-life floor; family switches cannot rise; the final repair re-checks. 328-343 lots moved per round (~6,800 h earlier). Rolling start and data fixes not included (need the previous day's schedule / plant data).
**Result:** cured 127,864 (73.8 %; v81 127,021, v80 124,413), built 127,174; PCR cured 106,348 (77.0 %), TBR 21,516 (61.0 %). 10-15 Oct cured PCR 57,707 (v81 53,145, v80 50,681), TBR 12,139 (11,294 / 10,529); later days give some back (PCR 18-Oct 8,696 built). 541 green tyres scrapped (v81 708). Extruder sequencing better (BR-21 54.8 %, BR-28 82.5 %, BR-32 89.3 %; per-day family: BR-25 84.6 %, BR-32 61.5 %), adherence 68 PASS / 9 PARTIAL / 4 FAIL (v81 68 / 7 / 6); MPQ 0 below; mixing BRM-09 5 (v81 4), 4RC rule 3 22 / 26 (v81 21 / 24); Algo Planning: BRBC-19 mould changes per day PCR 85.7 % (v81 93.8 %), TBR 64.7 % (70.6 %); BRBC-21 PCR 80.3 % (85.7 %).

## v83 — v82 + material allocated from item stock (user, 2026-10-12) -- tested, NOT kept (config back to v82)

**Change:** `upstream_gap_fill.repeg_mode: item_stock` (phase5, final re-allocation before the last repair).
- Every upstream item's batches go oldest-ready first to the earliest-starting consumer lot, within shelf life at that lot's start. Before, only compounds were re-allocated (v81 A2).
- A consumer's need is net of the opening stock already allocated to it, so no new batch stays reserved for a lot that ran on floor stock (e.g. B1144 of 12/13-Oct held for BEF001 L0001-L0004).
- The material gate, the BR-12 aging block and the starvation audit skip the opening-stock deduction for those pairs (`_OPEN_NETTED`). A pair whose whole need came from floor stock is not gated.
- Mixer and building consumers are untouched. Min aging, shelf life, machine order, changeovers and grouping rules are unchanged.

**First run failed:** cured 53,228. A fully covered lot kept a zero-quantity link, so the gate fell back to strict precedence (BEF001_L0001 10-Oct 07:00 -> 13-Oct). Fixed in the gate, then re-run.

**Result (re-run):** cured 127,708 (73.7 %; v82 127,864), built 126,965.
- PCR cured 105,877 (-473), TBR 21,831 (+315).
- Daily cured shifts: PCR 12-Oct 5,639 (v82 4,514), 14-Oct 12,455 (10,588), 15-Oct 13,025 (12,201); but 16-Oct 9,423 (12,932) and 17-Oct 11,191 (12,818).
- About 1,300-1,500 (consumer, item) needs netted of opening stock per round (570-745 t units). Material starvation 0. Green tyres scrapped 536 (v82 541).

**Rules:**
- 4RC rule 3 84.6 % -> 91.3 %.
- Worse: 1801 BR-25 84.6 % -> 76.9 % (PARTIAL -> FAIL), 1803 BR-32 day rule 61.5 % -> 41.7 %, 1002 BR-21 compatible-next 54.8 % -> 48.9 %.
- Adherence 69 PASS / 9 PARTIAL / 6 FAIL (v82 69 / 10 / 5).

**Verdict:** net flat and family grouping worse, so v82 stays the current config. The code stays behind the switch.

## v84 — v82 + 4RC run closer to the plant (user, 2026-10-12) -- tested, NOT kept (config back to v82)

**Basis:** `plant-schedule/PLANT_vs_v82_4RC_PROCESS.md`. The plant keeps 4RC compound about a day ahead and fills creel changes with fabric made early.

**Changes (config only; all 4RC / mixing rules checked by the existing mechanisms):**
- `mix_planner.fourrc_priority_h` 8 -> 24.
- `fourrc_rules.creel_fill.pull_days` 2 -> 3.
- `fourrc_rules.load_compact.fill_pull_days` 2 -> 3.

**Result:**
- Cured 126,196 (v82 127,864, -1,668): PCR 104,867 (-1,483), TBR 21,329 (-187). Green tyres scrapped 471.
- 4RC unchanged: 23.1 km a day, 121.5 h idle in the window (v82 124 h).
- Creel fill found no candidate. With demand-only 4RC (`whole_spool_topup: false`) there is no fabric work beyond need to pull forward; the plant makes about 1.9x the BOM need.
- Rule 3 84.6 % -> 92 %. Adherence 69 PASS / 8 PARTIAL / 7 FAIL.

**Verdict:** not kept.

## v85 — all masters from JK_CTP_PPDS, plan-12oct, WIP 12-Oct shift A, plan-12oct/assumption.md (user, 2026-10-13)

**Run:** `run_v15.py --wip-date 2026-10-12 --wip-time 07:00 --plan-end "2026-10-22 07:00" --plan-dir plan-12oct --name v85 --wip-source mix`.

**Data:**
- Plan `plan-12oct` (PCR 13 / TBR 14; new layout read correctly): window 12-Oct 07:00 → 22-Oct 07:00, cure plan 175,628, build plan 174,943.
- WIP `CTP_Opening_WIP` 12-Oct 06:31 (shift A, 12,263 rows; compounds 4,375 batches → 946.8 t; 0 unmatched green tyres).

**Masters, all live from the DB** (`master_files: {}`; the 9-Oct BOM / routing files are no longer used):
- BOM 133,179 rows; Routing 48,164; ChangeoverMatrix; MPQ; FRC; Aging; BuildingAllowable (building cycle).
- **CTP_Master_mixing** for mixing batch weights (`business_rules.mixing_batch_source: ctp_inputfiles/mixing_master_live.csv`, written by `build_sep_inputs.py`; before: a 30-Sep file copy).
- Mixing allowed mixers and cycle times: CTP_Master_mixing = DB routing for all 257 shared compounds.
- Curing cycle: the plan = CTP_Master_CuringCycleTime for all 4,857 rows with a master value.
- Plan machines / presses kept per row (user) and checked by the new `check_db_masters.py`.

**Assumptions (plan-12oct/assumption.md):**
- `kit_gate_assume_first_h`: 8 h, Cap Strip + edge gum only (EG0x / EGSL / TEG); no untracked codes.
- `ctb_wip_assumptions.expired_usable_h: 0`, so expired stock is not usable: 3,382 lots / 696,557 units.
- Upstream flow 24 h kept: 257 items, 245,723 units.
- The kit gate stays hard: assumption #9 "gate False" is NOT applied (v77 decision).

**Result:** cured **110,529 of 175,628 (62.9 %)**: PCR 92,371 (65.9 %), TBR 18,158 (51.3 %). Built PCR 90,279 / TBR 18,877.
- **17-Oct dip:** PCR built 1,552 / cured 4,449, TBR 582 / 343.
- From 15-Oct essentially every build row is kit-late, waiting for tread, sidewall, ply, steel belt (PCR) and body ply, pre-assembly (TBR).
- Masters run 93 of 96 h every day; finals only 17 / 27 h on 12 / 13 Oct. Mixing lots are a median 22–24 h late for needs from 19-Oct.
- 175 green tyres scrapped.

**Rules:**
- Mixing BRM-09 99.7 %, BRM-01 99.7 %; MPQ 100 %.
- 4RC creel 100 %, rule 3 89.3 %, one go 100 %.
- Extruder day-family rules fail (BR-21 21 %, BR-28 31 %, BR-32 64 %).
- Adherence cells 69 PASS / 11 PARTIAL / 5 FAIL.
- Must-be-0 checks: C4 ledger 21–23 build blocks before GUM STRIP exists; 1 SKU starve; ~760 lots over max aging after the pull. Material starvation 0.

**DB master checks** (`DB_MASTER_CHECKS_v85.md`):
- Building rows on allowed machines 100 %; on the rank-1 preferred machine 56.5 % (55,471 tyres not).
- Cure rows on allowed presses 98.9 % (54 rows / 2,636 tyres not; e.g. 1D25214012008QXPCT on 3615).

**DB data issues:**
- CTP_Master_mixing: machine_pref 0 on all 557 rows; 206 rows without batch weight; 44 duplicate rows from two mixer code styles (310 / K310).
- 68 routing mixing compounds not in CTP_Master_mixing; B333ARS not in routing.
- 19 WIP compound rows without a batch weight.
- SKU 1225219015008SXC1E without a building routing (5 rows skipped).
- Plan machine names (TBMPCR<n>Stage2, TBM-0<n>) differ from the masters (34<nn>, TBR-0<n>).

**Comparable?** Not with v82: different plan, WIP, window and masters.

## v86 — EVALUATION: v85 + plant flow from DB actuals + mixer preference + plant cycle times (user, 2026-10-13) -- code / config restored to v85

**Assumptions:** detailed point by point in `latest_schedule/ASSUMPTIONS_LOG.md`.
1. 8 h of the plant's average output (04–10 Oct, `CTP_Plant_Schedule`) as stock at t0, components and compounds. Replaces the 24 h of 06-Oct sheet-5 flow.
2. Batch weight per mixer (CTP_Master_mixing + gap fill from the preference matrix) and mixer preference by kg per hour (6 h per rank).
3. Plant mixing cycle where faster (`relaxed_mixing_overrides.csv`, cycle only).

**Temporary code:** `apply_ctb_approximations.py` (flow_source db_actuals, cycle_only) and `mix_planner.py` (`durm`, preference penalty). Removed after the run; files identical to the v85 backups. Inputs and scripts kept in `latest_schedule/v86_assumption_inputs/`.

**Result:** cured **112,716 (64.2 %)** vs v85 110,529: PCR 91,797 (−574), **TBR 20,919 (+2,761)**.
- Daily swings bigger: PCR 17-Oct 4,444, 18-Oct 2,128 cured; then 20 / 21-Oct 97–102 %.
- Mixers 1,312 h. 191 green tyres scrapped.

**Rules:**
- BRM-09 99.8 %, BRM-01 99.5 % (PARTIAL), MPQ 100 %.
- 4RC rule 3 75 % (v85 89 %), one go 100 %.
- Adherence cells 68 / 11 / 6.
- C4 ledger 14 build blocks before GUM STRIP (MUST be 0). Starvation 0.

**Verdict:** not adopted (user: evaluation only). The +2.2 k is within the ±4 % run spread except TBR. The three assumptions ran together, so their separate effects are unknown.

---

## v87 — v85 + 12-Oct 17:26 WIP correction + master compound unlimited + mixing from routing (no CTP_Master_mixing) (user, 2026-10-13) -- current config

**What changed** (assumptions V87-1 … V87-5 in `ASSUMPTIONS_LOG.md`):
- **Opening WIP:** the 06:31 snapshot plus the 17:26 lots produced before 07:00 that 06:31 missed: 119 rows, of which 111 final-compound lots / 414 batches. Compound WIP 946,799 → 1,038,344 kg. Code: `build_sep_inputs.py`, env `CTP_WIP_CORRECTION` (unset = v85 behaviour).
- **Master compounds unlimited:** config `masters_unlimited.enabled`; `apply_ctb_approximations.py` drops the 23,359 MASTER MIXING routing rows (218 masters), the same way as the outsourced compounds.
- **Mixing batch sizes from `CTP_Master_Routing`:** `mixing_batch_source: ctp_inputfiles/routing_live.csv`; `CTP_Master_mixing` not used.
- BOM / routing / allowable from `JK_CTP_PPDS` as in v85.
- **DB server unreachable (11:20)**, so the run was **offline**, on the copies saved 13-Oct 01:04–01:11. Same master data as v85.
- v85 config saved as `config_v85_before_v87.yaml`.

**Result:** cured **129,259 / 175,628 (73.6 %)**:
- PCR 106,764 (+14,393 vs v85), TBR 22,495 (+4,337);
- built 128,592;
- 102 green tyres scrapped (v85 175).

**Dips** (< 70 % of cure plan):
- PCR 15 / 16-Oct: 901 sequencing, K310 full;
- TBR 14 / 16 / 17 / 20 / 21-Oct: 1802 full at 24 h/day, 901, K310.

**Utilisation:**
- final mixers 97 %, 1802 83 %, building 63 %, curing 63 %;
- master mixers idle (assumption).

Full analysis: `v87/DATA_GAPS_DIPS_UTILISATION_v87.xlsx`, made by the new `make_run_analysis.py`.

**Rules:**
- BRM-01 100 %, BRM-09 100 %, MPQ 100 %, 4RC-3 / 4RC-6 100 %.
- Adherence cells 71 / 8 / 5 (v85 69 / 11 / 5). Extruder family-switch rules still FAIL (as v85).
- Starvation 0.
- Still not 0: C4 ledger 23 build blocks before GUM STRIP; 1 SKU starves in echo re-cut (MUST be 0); 630–670 lots over max aging after the pull.

**Verdict:** highest cured on the plan-12oct dataset (v85 110,529, v86 112,716). Most of the gain comes from the unlimited-master **assumption**: it removes the master mixers, the v85 bottleneck. This is not a plant-verified capacity: the plant must actually have the master compound.

---

## v88 — v87 + 901 urgent pull (one go a day relaxed for waited-for lots) (user, 2026-10-13) -- tested, NOT kept (config back to v87)

**Change:**
- New phase-5 pass `_fourrc_urgent_pull`, config `fourrc_rules.urgent_pull`, default off.
- Runs inside the 4RC rounds after load-compact. A later 901 lot whose consumer starts within 1 h of the lot being ready moves into an idle 901 gap of ≥ 60 min when:
  - its inputs are ready;
  - it stays within shelf life;
  - changeovers both sides and PM fit;
  - the creel is kept (fabric, or steel on the loaded wire);
  - the next lot is not delayed.
- One go a day may break for these lots only (user OK).
- Bias cutter unchanged (user: keep 2 × 1.66).
- Same data and run as v87 (offline, `CTP_WIP_CORRECTION`).

**Result:** cured **128,848 (73.4 %)**, v87 129,259 (**−411**, within noise):
- PCR 107,031 (+267), TBR 21,817 (−678);
- 24 green tyres scrapped (v87 102);
- starvation 0.

**Daily shift:**
- PCR 12-Oct 55.7 % (v87 83.0 %), 13-Oct 44.0 % (75.4 %);
- PCR 14 / 15-Oct up (82.7 / 89.3 %), 21-Oct 101 %.

**Why day 1 was lost:**
- The pull moved a steel lot (CWBHTCP100) into the 12-Oct 17:32 → 13-Oct 13:25 gap. The rule passes after it (one go a day / day plan / compaction) then queued the whole 12-Oct 901 sequence behind it.
- 901 ran nothing on 12-Oct: CAP 66 mother rolls moved 12-Oct 10:05 → 13-Oct 08:50.
- So cap strip on 701 / 702 was a day late and all 11 PCR stage-2 TBMs lost ~60 % of their 12-Oct output.

901 idle stayed at 99 h; "held although ready" stayed at 50 h.

**Rules:**
- 4RC-7 one go a day 94.4 % (v87 100 %), 4RC-9 steel ↔ fabric 81.8 % (90 %), 4RC-10 no idle inside a load 98.6 % (100 %).
- Adherence cells 69 / 10 / 5 (v87 71 / 8 / 5).

**Verdict:** not kept. A version that could work would only move fabric lots, or run the pull after the last one-go / day-plan pass so later passes can't reorder the day.

---

## v103 — v98 + finals on own final mixers first + 24 h cap strip / edge gum (user, 2026-10-14) -- NOT kept, config back to v98

- **Change:** `mix_planner.final_mixer_first` (a master mixer takes a final only when no routed final mixer can make it by need time: free + run + 2 h); `kit_gate_assume_first_h.hours` 8 -> 24 (V103-1).
- **Result:** 128,017 (−3,358 vs v98): PCR **105,717** (+2,226; 12-Oct 87 %), TBR 22,300 (−5,584; 15-Oct 20 %, 21-Oct 35 %). Only 5–13 final runs overflowed to master mixers; F270 / K310 89 % busy, masters ~0 %. Mixing capacity is the root again on PCR / TBR 14-Oct. All mixing rules 100 %.
- **Finding:** the "can make it in time" test looks only at each final mixer's current free time, not the queue it will take, so almost nothing overflowed. Day-1 PCR is fixed by the 24 h assumption (also seen in v101).

## v102 — v98 + 901 lot split by consumer timing + 1802 output x2 (user, 2026-10-13) -- NOT kept, config back to v98

- **901 split** (`fourrc_rules.split_by_consumer`): a late 901 lot whose last consumers pin it by max aging is split; the early part is released at inputs-ready, the late part keeps its slot (min roll, <= dip, own shelf-life floor; the 4RC day plan places both). 17–20 lots split per round.
- **1802 x2** (`routing_output_multiplier {"1802": 2.0}`, assumption V102-1): BEF001 13 -> 26 m/min of output; 1802 load 95 % -> 48 %.
- **Result:** 126,456 (−4,919 vs v98): PCR 98,914, TBR 27,542. 901 late lots 52 -> 44, late hours 816 -> 715. TBR did not rise: 1802 was not what limited TBR (now 2001, K270M compound, 1803, 901). PCR 12-Oct 58 %, 16 / 18 / 19-Oct 44–53 %; 393 green tyres scrapped.
- **Code fix:** `fourrc_checks.py` takes the floor metres for split lots (crashed on NaN metres).

## v101 — v98 + day-1 urgent 901 order + 24 h cap strip / edge gum (user, 2026-10-13) -- NOT kept, config back to v98

- **Change:** `day_plan.need_mode: urgent_first_day1` (first plant day only); `kit_gate_assume_first_h.hours` 8 -> 24 (assumption V101-1).
- **Result:** 124,668 (−6,707 vs v98): PCR 98,534, TBR 26,134. PCR 12-Oct 88 % cured (v98 56 %) but 16-Oct 20 % and 17-Oct 39 %; 479 green tyres scrapped.
- **Finding:** every run that fixes days 12–14 collapses 16 / 17-Oct (v99, v100, v101). Config back to v98 (`config_v98.yaml`).

## v100 — v98 + 901 day blocks urgent-first (user, 2026-10-13) -- NOT kept, config back to v98

- **Change:** `day_plan.need_mode: urgent_first` -- most urgent block next; a gap before it (inputs / creel) filled only by a ready block that finishes before it can start.
- **Result:** 127,352 (−4,023 vs v98): PCR 99,980, TBR 27,372. Day 1 fixed (12-Oct 11,322 built) but PCR 16-Oct 22 % and 18-Oct 40 %; 51 late 901 lots (v98 63).
- **Finding:** each change to the 901 day order moves the PCR dip to another day (v98 day 1, v99 15–19 Oct, v100 16 / 18-Oct): greedy one-pass ordering + later repair passes cascade. `need_mode: steel_first` = v98 (kept in config).

## v99 — v98 + 901 day blocks ready-first (user, 2026-10-13) -- NOT kept

- **Change:** with `day_plan.need_order`, each day's blocks are chosen on the timeline: among READY blocks the most urgent; ready fabric fills a creel reload.
- **Result:** 121,353 (−10,022 vs v98): PCR 94,846, TBR 26,507. PCR 12-Oct fixed (11,322 built), but PCR 15 / 16 / 19-Oct fell to 59 / 40 / 28 %: a ready, less urgent block overtook an urgent one about to become ready (901 root of 33–55 % of late PCR tyres).
- **Replaced in v100:** urgent block first; a gap before it is filled only by a ready block that finishes before the urgent one can start.

## v98 — v97 + mixing settings again + 901 smaller lots / re-peg / need order (user, 2026-10-13) -- day-1 issue fixed in v99

- **Mixing (user):** `final_on_master_mixers "*"`, `mix_planner.final_cleanout_after_s: 120`, `plant_mixing_cycles` on (finals, faster only).
- **901 #1 smaller lots:** `fourrc_rules.pool_span_h {fabric: 24, steel: 48}` (phase 2: a calender lot pools at most this much demand, still <= one dip, min roll kept); `wire_campaign.max_days` 5 -> 2.
- **901 #2 re-peg:** `left_shift.repeg` -- 901 output re-allocated FEFO within shelf life before the left-shift (over-age 901 quantity 16,481 -> 6,273 in round 1; never rises).
- **901 #3 need order:** `day_plan.need_order` -- wire blocks / items of each plant day by need time (first consumer - min aging); loaded wire first.
- **Result:** cured **131,375 (74.8 %)**: PCR 103,491, TBR **27,884** (best). +559 vs v97, +2,116 vs v87. Mixers 17–44 % busy. Mixing audit: every rule 100 % incl. M4 (51) and M9 (472). Left-shift moved 21 loads / 55 lots in round 2.
- **Problem:** PCR 12-Oct only 4,139 built: the need order still put steel first, so steel CPJ121 (ready 18:00) ran before ready fabric and CAP 66 / ECOPOLY350 / BRTTL went to 13-Oct (slitter 2401 starved). 276 green tyres scrapped (v97: 108).

## v97 — v87 + bought-in items + 901 left-shift (user, 2026-10-13)

- **Change 1 (data, user):** procured from outside -> routing rows removed, always available, never made (`outsourced_items`, `apply_ctb_approximations.py`): 901 items CWCH30256 (442 rows), CWBHI34 (220); compounds MS43, MS70, MB100 (no effect while masters are unlimited), TBA827, TCA313 (already outsourced).
- **Change 2 (code):** `fourrc_rules.left_shift` -- after the last 4RC pass, whole wire loads move EARLIER into idle (same order; inputs + min aging, matrix changeover, 240 min creel, PM, max aging - 4 h at consumers, one go a day kept; never later), then the final repair re-times consumers.
- **Result:** cured **130,816 / 175,628 (74.5 %)**: PCR 106,494 (−270 vs v87), TBR 24,322 (+1,827). 108 green tyres scrapped; starvation 1 SKU and 23 GUM STRIP ledger blocks (same as v87).
- **Finding:** the left-shift moved 1 load in 3 rounds. 901 loads are held by the previous lot (back to back) or by shelf life: one dip is pegged to consumers spread over days (v87: 134 of 1,309 links over max aging).
- **Rules:** none changed; 901 hard rules kept by construction.

## v96 — v95 + faster plant cycle times for master compounds (user, 2026-10-13) -- NOT kept; config back to v87 (user: v87 is the default version)

- **Change:** `plant_mixing_cycles.operations` = FINAL + MASTER MIXING, faster only (124 compounds: 44 finals, 80 masters). Masters mixed for real (`masters_unlimited: false`), standing mixing settings on.
- **Result:** cured 114,769 / 175,628 (65.3 %): PCR 94,954, TBR 19,815. vs v95 −1,130 (noise); vs v87 −14,490. PCR 18-Oct 4,402 cured.
- **Rules:** no rule changed; reports in `v96/`.
- **After the run (user):** v87 is the default version. `config.yaml` set back to v87: `masters_unlimited: true`, `final_on_master_mixers` off, `final_cleanout_after_s: 0`, `plant_mixing_cycles` off; all v88–v96 switches off. v96 config saved as `config_v96.yaml`.

## v95 — check: current settings with masters mixed for real (user, 2026-10-13) -- config back to masters unlimited

**Change:** `masters_unlimited: false`; everything else as the current config (finals on master mixers, 120 s after finals, faster cycles, all rules).

**Result:** 115,899 (66.0 %): +5,370 vs v85, −11,913 vs v94; PCR 96,317, TBR 19,582.

**Mixers:**
- master mixers 95–97 % busy (205–225 h masters, only 7–22 h finals);
- F270 / K310 67 % / 54 % (waiting for masters);
- mixing is the root of ~68 % of late PCR tyres and ~51 % of late TBR tyres.

**Rules:** all 100 %.

**Answer:** mixing is still the bottleneck when masters are real.

---

## v94 — standing settings + two mixing rules RELAXED (user, 2026-10-13) -- relaxations switched off again

**Rules relaxed (user):**
- **#1** repeat gap for 4RC skim compounds 24 h → 12 h (`mix_planner.gap_h_fourrc`);
- **#2** second mixer on the same plant day when the next lot is needed within 8 h (`mix_planner.second_mixer_cover_h`).

Code: `mix_planner.py` (`GAP`, `_short`). No other rule touched.

**Result:** 127,812 (+557 vs v93, −1,447 vs v87); PCR 102,749, TBR 25,063.

**Rule checks:**
- M8 87 %: 52 runs used the shorter gap;
- M7 100 %: the second mixer was never needed;
- all other mixing rules 100 % (M4 66, M9 480); 4RC as v93; starvation 0.

**901:** compound-not-ready wire breaks 22 → 18; still 81 / 138 lots late.

**Verdict:** gain within noise. Relaxations off; the bottleneck is 901 sequencing (PCR) and 1802 (TBR), not mixing rules.

---

## v93 — standing mixing settings + 901-driven skim compound timing + CAP 66 first-day rule (user, 2026-10-13) -- 4RC changes NOT kept

**Change:**
- **Standing settings (user, all runs):** every final may run on K270M / K430-1 / K430-2 / F440 (routing batch / cycle); 120 s clean-out after every final (new planner rule, audit M9); faster plant cycles.
- **Plus:** `mix_planner.fourrc_due` on (80 skim lots); `wire_campaign.no_join_first_day: [CAP 66]`.

**Result:** 127,255 (−2,004 vs v87); PCR 102,442, TBR 24,813 (+2,318).

**Mixing:**
- all six mixers 21–41 % busy;
- **M4 (K430-2 final, 120 s before / after) 64 / 64, M9 (120 s after every final) 464 / 464**;
- all other mixing rules 100 %; MPQ 100 %.

**901:** 66.6 % busy, 86 / 144 lots late, 27 wire breaks (22 compound not ready); 4RC rules 100 % except rule 3 87 %.

**Lost:** PCR 15-Oct (51 %) and 18-Oct (34 %): 901, 1101 inner liner, 1001 / 1002 tread.

**Verdict:** 4RC changes off; the standing mixing settings stay on.

---

## v92 — v91 with faster plant cycle times only (user, 2026-10-13) -- tested, NOT kept (switches off = v87)

**Change:** `plant_mixing_cycles.faster_only: true` (44 finals faster; the 13 slower plant values ignored) + T638 on K430-2.

**Result:** 125,662 (−3,597 vs v87); PCR 103,427, TBR 22,235.

**Rules:** mixing 100 % incl. M4, MPQ 100 %, starvation 0, cells 70 / 10 / 5.

**Lost:**
- PCR 12 / 13-Oct: the CAP 66 dip was pushed to 13-Oct by the 901 day plan + 4RC-10 hold, as in v88 / v89;
- TBR 18-Oct: 0 built; the TBMs waited ~1.5 days for 802 belts / chipper and 1802 BEF001.

**Verdict:** not kept. Day 1 needs the v90 CAP 66 first-day rule whenever compound timing changes.

---

## v91 — v87 + plant cycle times for finals + T638 on K430-2 (user, 2026-10-13) -- awaiting user decision (switches off = v87)

**Change** (config `config_v91.yaml`):
- `plant_mixing_cycles`: FINAL MIXING proc_time = the plant's median batch-to-batch time 04–11 Oct. Only pairs with ≥ 20 intervals; 57 compounds, 44 faster, 13 slower. Files: `plant-schedule/build_plant_mixing_cycles.py`, `plant_mixing_cycles_04_10oct.csv`. Code: `apply_ctb_approximations.py`.
- `final_on_master_mixers {T638: [K430-2]}`: the only final the plant runs on a master mixer.
- `mix_planner.machine_factor "T638|K430-2": 1.034` (the plant's 5,877 kg/h); code in `mix_planner.py` (`dur_m`) and phase-5 `_commit56` (`duration_h`).

**K430-2 final-compound rule** (120 s clean-out before and after a final, e.g. T638):
- already implemented in `mix_planner.co` (any final on K430-2) and audited as M4 in `mixing_rule_audit.py`;
- v91 is the first run with a final on K430-2: 1 run / 14 rows, **100 % followed**.

**Result:** 129,309 (+50 vs v87):
- PCR 104,215 (−2,549), TBR **25,094 (+2,599)**;
- F270 / K310 busy 87 % / 86 % (v87 98 % / 97 %); K430-2 16 h of T638;
- 1802 busy 93 %; starvation 0.

**Rules:**
- all mixing rules 100 %, MPQ 100 %;
- 1803 BR-32 and 1001 BR-28 family sequencing lower, 4RC wire-campaign matrix order 80 %;
- cells 71 / 8 / 6.

**Verdict:** total equal (noise). The user decides whether to adopt the plant data.

---

## v90 — v87 + CAP 66 first-day dip protected + B616M 24 h + inner-liner compounds 8 h (user, 2026-10-13) -- tested, NOT kept (config back to v87)

**Change** (config `config_v90.yaml`):
- `fourrc_rules.wire_campaign.no_join_first_day: ["^CAP 66-MOTHERROLL$"]` (new, phase-5 4RC day plan): no next-day lots join the first plant day's CAP 66 load. It skipped 9 lots per round.
- `mix_planner.item_priority_h`: B616M 8 → 24; B355 / B458 / EFA / GB35 / GB58 / GL6 / GL7 at 8.
- No rule relaxed.

**Result:** **127,513 (72.6 %)**, −1,746 vs v87:
- PCR 105,492, TBR 22,021; 18 green tyres scrapped.
- Day 1–2 fixed: CAP 66 at 12-Oct 07:00; PCR 86.5 % / 83.3 %.
- New PCR dip on 16-Oct (33.4 %, 1,612 built) and 17-Oct (53.1 %). Stage-2 TBMs idle ~1 day; 15–16 Oct kit-late rows bound by inner liner (IL056 / IL039 / IL043 / IL054, 10.6 k tyres).

**Rules:**
- **material starvation 1 (TBPGUM, MUST be 0)**;
- 4RC v42 wire campaigns 83 %; extruder family rules lower;
- cells 70 / 8 / 6.

**Why:** the final mixers are full (98 %). Priority for B616M and the inner-liner compounds pushed other compounds later, and inner liner still arrived late (1101 grouping vs compound runs). The day-1 fix alone is sound.

**Verdict:** not kept; config back to v87. Next test, if wanted: the CAP 66 first-day rule **alone** (no priority changes).

---

## v89 — v87 + inner-liner compounds 8 h ahead + 901 fabric-only urgent pull after the day plan (user, 2026-10-13) -- tested, NOT kept (config back to v87)

**Change** (config saved as `config_v89.yaml`):
- **(a) Mixing by building need for 1101:** `mix_planner.item_priority_h` 8 h for B355, B458, EFA, GB35, GB58, GL6, GL7. Mixing gap, cover and shelf-life rules unchanged.
- **(b) The v88 pull restricted to fabric lots** (`fabric_only`), run once per round after the last 4RC day-plan / one-go pass (`after_day_plan`), followed by the final repair. It moved 23 / 19 / 19 lots.

**Result:** cured **130,173 (74.1 %)**, +914 vs v87 (within the ±4 % noise):
- PCR 107,500 (+736), TBR 22,673 (+178);
- 85 green tyres scrapped; starvation 0;
- C4 ledger 24 GUM STRIP blocks (v87 23).

**Daily shift vs v87:**
- PCR 12-Oct 55.7 % (v87 83.0 %), 13-Oct 45.1 % (75.4 %), the same loss as v88;
- PCR 15–21-Oct up (79.9–92.4 %).

**Rules:**
- 4RC-7 one go a day 89.3 % (v87 100 %; relaxed for urgent lots, user OK);
- **4RC-10 no idle inside a dip 96.2 % (v87 100 %)**;
- 4RC-9 90.9 %; mixing 100 %;
- adherence cells 69 / 10 / 5 (v87 71 / 8 / 5).

**Why PCR 12 / 13-Oct is lost in v88 and v89** (phase-5 trace of `CAP 66-MOTHERROLL_L0001`):
- The 4RC day plan joins more next-day lots into the CAP 66 dip (22 lots joined, v87 16).
- The load-compact **HOLD** (rule 4RC-10: no idle inside a dip) then starts the whole dip later so it runs in one go. The dip's later lots wait for B616M (K310, ready 12-Oct afternoon).
- So the first CAP 66 mother roll moves from 12-Oct 07:00 / 10:05 to 12-Oct 17:16 (standalone) and 13-Oct 08:33 (full run), although it runs on opening-stock compound.
- Cap strip on 701 / 702 is then a day late and the PCR stage-2 TBMs lose ~6,000 tyres.
- The hold has no check for a waiting consumer.

**Verdict:** not kept. The gain is within noise, 4RC-10 is lower (user: no rule violated) and the day-1 loss repeats. Config back to v87 (`urgent_pull.enabled: false`, inner-liner priorities removed).

---

## Change notes (not rules)

| Date | Note |
|---|---|
| 2026-10-06 | **Experiment: mixer cap 220 master / 160 final batches per mixer per day** (`latest_schedule/exp_mixcap_off`, `exp_mixcap_on`; same data offline, all v33 rules with BR-12 off). **Without the cap:** built 106,526 (PCR 90,685 / TBR 15,841), cured 105,665; batches per mixer per day F270 458, K310 517, masters 383-420; 6,973 t mixed. **With the cap:** built 44,952 (PCR 37,675 / TBR 7,277), cured 50,878 (**−58% built, −52% cured**); batches per mixer per day F270 130 (max 156), K310 133 (max 153), masters 171-193 (max 198-215); 2,759 t mixed; TBM use 22.8% vs 52.3%; 2,467 green tyres scrapped vs 270. All rule checks are similar in both (capacity, MPQ, 4RC, mixing about 100%). The config stays without the cap (user: routing, no cap). |
| 2026-10-06 | **Butyl clean-out = 120 s for all runs (user).** `business_rules.butyl_sequence_rule`: the default changes from `one_batch_cycle` / 160 s to `fixed` / **120 s**; the final mixers were already fixed 120 s. The master mixers keep `none` (BR-V13: a broken master sequence takes only the 60 s changeover). The mixer changeover itself stays 60 s (`mixer_changeover_min: 1`). Applies from v28. |
| 2026-10-01 | **`config.yaml` encoding damaged and repaired.** A PowerShell edit for v5 read the UTF-8 file as Windows-1252 and garbled every non-ASCII character. It was reversed exactly and checked line by line against `INPUT_TABLES_30SEP_TRUE/03_local_masters/config.yaml`: only the intended rule settings differ. No run used the damaged file. |
| 2026-10-01 | **Fixed an Excel time error.** `make_schedule_from_template.py` passed times to Excel so that they were treated as UTC and shown in IST, which made every time in the Excel **5 h 30 min early**. The CSV outputs were never affected. Fixed, and all earlier workbooks were regenerated and checked against their CSVs: `output/CTP_30SEP_TRUE_RERUN/CTP_29SEP-06OCT_SCHEDULE.xlsx`, `latest_schedule/CTP_28SEP-08OCT_SCHEDULE.xlsx` and `latest_schedule/CTP_28SEP-08OCT_SCHEDULE_HOLIDAY_02OCT.xlsx`. |
| 2026-10-01 | **Strict lot_id / consumption_id** (`strict_links.py`, used by `make_schedule_v18.py`; the schedule itself is unchanged). Before this, consumption_id came from phase-3 allocation links, made before placement. 637 upstream, 176 building and 1,903 curing links pointed at a lot that was **not ready yet** when the consumer started. Now every link is drawn from a time-aware material ledger. **Supply** is opening stock, one lot per production_id, plus scheduled lots released per shift row: upstream rows at the row end + min aging, and green tyres streamed one by one + 90 min transfer (the phase-6 S6 rule). **Demand** is the SKU's own BOM ratio in the lot's UOM; upstream and building rows draw their whole kit at the row start, and cure blocks draw tyre by tyre. Supply is used first-expiry-first-out and never past max aging, and each unit is drawn once. **Tokens:** `lot(qty)`, `OPENING_WIP:<item>(qty)` (production ids in the *Lot genealogy* sheet), `RAW_MATERIAL:<item>`, `SHORT:<item>(qty)`. **Checks (v12):** 29,713 links; 0 drawn before usable; 0 unknown ids; 0 lots over-drawn; consumption_id = genealogy for every lot. **1,116 lots have a SHORT input** (198 curing, 274 building, 644 upstream). 491 are quantity gaps: the item's total supply < demand, e.g. MB184 → MB185 is 42.8 t short because the BRM-13 buffer and batch rounding of MB185 are not exploded upstream, and TB WRAP B1144 is 29 t short. 665 are timing gaps: the supply exists but finishes after the consumer starts, e.g. CAP 66, GUM STRIP. New sheets: *Lot genealogy* and *Genealogy check*; new flags: `input_short`, `kit_incomplete`, `gt_short`. |
