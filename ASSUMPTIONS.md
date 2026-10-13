# CTP scheduler: assumptions in use (as of 2026-09-30)

These apply to the 29 Sep – 6 Oct plan runs, 30-09 folder (v9 plan).

**Tag in the "Status" column:**
- **CONFIRMED**: you, or the plant through you, confirmed it.
- **DB**: taken from live DB data.
- **OURS**: our own assumption or approximation. The plant should confirm these.

## A. Data sources

| # | Assumption | Status |
|---|---|---|
| A1 | The plan is the plant's plan workbooks (`30-09/…pcr 9 / tbr 9.xlsx`). The DB plan tables (`BuildingSchedule_*`, `CuringSchedule_*`) are for October and April, so they are not used. | OURS |
| A2 | The DB host is 35.208.174.2. It is treated as equal to DEV 136.112.107.154, because the table row counts match. | OURS |
| A3 | Component WIP is `JK_CTP_PPDS.CTP_Opening_WIP`: the 06:30 snapshot of the plan-start day is taken as the 07:00 shift-start stock. | DB |
| A4 | Compound WIP comes from the old feed `jkplanning_CTP.opening_wip`, same date, because the 06:30 snapshot has no compound rows. | OURS |
| A5 | Green-tyre stock codes are mapped to BOM GT codes via `gt_inventory_manual_pcr/_tbr` (only where there is exactly one match). 78 tyres of GT 2528 RAN ATW are not in the BOM and are left out. | DB |
| A6 | WIP already past max aging at plan start is not counted. | OURS |
| A7 | BOM is `CTP_Master_BOM` and routing is `CTP_Master_Routing`, both live. They replace the 2 Sep local copies. | DB |
| A8 | Building cycle time is `CTP_Master_BuildingAllowable`, falling back to `jkt_building_cycle_times`, then routing. One value per SKU (the Conti value). Which machines are BJ (about 10% slower) is unknown. | DB / OURS |
| A9 | The plan's `CO_Mins` (planned changeover minutes) are added to building time. | OURS |
| A10 | Curing cycle time is taken from the plan. Checked equal to `CTP_Master_CuringCycleTime`. | DB |
| A11 | The changeover matrix is `CTP_Master_ChangeoverMatrix`. 10 TBR machines are mapped to matrix lines by matching item codes (1901, 2001, 802, 1801, 1803, 2101, 1102, 2403–2405). | DB / OURS |
| A12 | MPQ: PCR items use the corrected PCR sheet. TBR-only items use the TBR rows of `CTP_Master_MPQ`. Items used by both lines use the PCR sheet. | DB / OURS |
| A13 | A lot's line (PCR, TBR or both) comes from the `tyre_type` column of `CTP_Master_BOM`. | DB |
| A14 | MPQ unit guard: a floor applies only when its unit equals the item's unit. So the STC row (180 M) is not applied to L/R-STC chippers, which are counted in Nos. | OURS |
| A15 | TBR edge strips, wraps and BEF001 have no minimum (there is no TBR MPQ row). | OURS |
| A16 | The buffer master (PCR only) is not used; no phase reads it. | — |
| A17 | PM/MTC downtime is `jkplanning_CTP.downtime_reason`. Machine IDs 34xx map to TBMPCR<n>Stage2 and 38xx to TBM-0<n>. | DB / OURS |
| A18 | Holidays come from `CTP_Master_HolidayCalendar`; there are none in the window. | DB |
| A19 | When the DB link fails (4 retries), the last saved snapshot in `db_pipeline/raw` is used, with a loud warning. | OURS |
| A20 | Only the plan-start date's WIP snapshot is used; several snapshots would double-count. | OURS |

## B. How the plant plan is read

| # | Assumption | Status |
|---|---|---|
| B1 | The Shift Schedule is a day plan sliced into shifts pro-rata (plant Notes sheet). So the order within a machine-day is ours. | Plant notes |
| B2 | BR-1: a build row may not start before its own plan shift (A 07:00, B 15:00, C 23:00). It may start later. | OURS (rule reading) |
| B3 | Within a day, a building machine takes the row that can start earliest with its kit ready. Ties go by curing need. | OURS |
| B4 | Every building row stays on its planned building machine, and curing on its planned press. The press sequence is kept. | OURS |
| B5 | The plan's green-tyre and curing quantities are net of stock (the plan's `Planned+GT`), so green tyres are not netted again. | OURS |
| B6 | The plant day runs 07:00 → 07:00. Daily figures are pro-rated by time within each block. | OURS |
| B7 | Coverage is measured at the plan end (6 Oct 07:00). Work after that is counted as spill. | OURS |
| B8 | A row with no BOM or routing GT is not built (none in this plan). | USER directive |

## C. Plant-confirmed operating facts

| # | Assumption | Status |
|---|---|---|
| C1 | Raw materials (polymer, carbon black, chemicals, steel cord, fabric) are always available. | CONFIRMED |
| C2 | Green-tyre building may start any time the kit is ready, including mid-shift. | CONFIRMED |
| C3 | Green-tyre transfer time from building to press is 90 min (PCR and TBR). | CONFIRMED |
| C4 | Green-tyre shelf life is 72 h for all green tyres. The DB had 0 h on 26 codes. | CONFIRMED |
| C5 | MTCM313, TBA827 and TCA313 are mixed outside (purchased, always available); they are removed from routing. | CONFIRMED |
| C6 | Cap strip and edge gum (cap strip chain, EG01–03, BP01, TEG-01/02, WRAP, EGSL, GUM STRIP) are assumed available, because V5 guarantees them. They are still scheduled and aged, but never hold a build. | CONFIRMED (V5 logic) |
| C7 | The belt cutter and bias cutter make B1 + B2 in one cut (1 cut = 1 tyre). | CONFIRMED |

## D. Approximated data (the plant has not supplied these)

| # | Item | Value used | Basis | Status |
|---|---|---|---|---|
| D1 | GL7 (final mixing) | 2.171 min/batch on F270/310 | median of 48 PCR finals | OURS |
| D2 | NYLON23-02, NYDBCHF (doubling 2501/2502) | 22 m/min | chafer slitter median | OURS |
| D3 | SA15001, SA17001 (bead apex) | 15.38 Nos/min on 201/202 | median of 75 apexes | OURS |
| D4 | SW15001 (bead winding) | 30 Nos/min on 1201 | median of 50 | OURS |
| D5 | SPB02, SPB03 (ply) | 15 cuts/min on 1103/1104 | median of 124 plies | OURS |
| D6 | TBPGUM | 38.8 m/min | 12.5 cuts/min × 3.10 m | OURS |
| D7 | TBWB001–008 (TBR bead winding) | TDA 2403–2405; TBWB008 3.75 Nos/min | no TBR winding machine in the masters | OURS |
| D8 | XMT180 (master mixing) | 110.26 s/batch | sibling median | OURS |
| D9 | T7110, SWRS30 | 130.26 s/batch, 30 m/min | sibling median (earlier run) | OURS |
| D10 | BOM B4-TBLT006 → CP 200 | 0.3866 M/unit | median of 11 sibling belts | OURS |
| D11 | BOM TBAPBD002 → S45 | 1.596 KG/unit | median of 13 siblings | OURS |
| D12 | BOM TBP013 / TBP021 → CWBP65 | 1.2744 M/unit | median of 12 siblings | OURS |
| D13 | Aging AP0011T1 | min 1 h / max 48 h | plant spec 13 (manual apex) | OURS |
| D14 | Bias cutter 2001 | ×1.66 speed calibration | measured throughput vs model (provisional) | OURS |
| D15 | TR100 master↔final clean-out | 5 min | TR100 not in routing | OURS |
| D16 | Butyl purge when no butyl-friendly compound follows | 5 min (same as TR100) | BRM-03 fallback | OURS |
| D17 | Press time | qty ÷ 2 cavities × (cure time + 2.5 min PCR / 6.0 min TBR) | assumed load/unload allowance | OURS |
| D18 | Items with no aging row (BP01, CAP 66- slitted, CAP30- slitted) | effectively no expiry | no DB row (all V5 cap strip / edge gum items) | OURS |
| D19 | Aging fallback by item type where an item has no row (GT 0.25/24 h, tread and sidewall 2/72, belt, edge strip and ply max 24, inner liner 6/72, apex 72, cap strip 24) | config `aging_fallback_by_itemtype` | OURS |
| D20 | Missing PCR inner-liner and TBR GUM STRIP stock taken as 0 (all made new) | CTB request 4 | OURS (conservative) |
| D21 | Mixer stage queue time | from MES, 2 links only | CTB sheet: approximate | OURS |
| D22 | Tread MPQ "310, 206" | smallest (206) used for all sizes | plant note: size-dependent | OURS |
| D23 | Cap strip MPQ "9*1800" | 16,200 M | parsed | OURS |

## E. Business rules as implemented

| # | Rule | How it is applied | Status |
|---|---|---|---|
| E1 | BRM-01 mixing MPQ | 5 batches (the DB says 3); business rule wins | CONFIRMED (rules file) |
| E2 | BRM-13 / BR-77 hold batches | +5 on each compound's last lot, except special (butyl/sticky) compounds | CONFIRMED |
| E3 | BRM-02 sticky compounds | JT444/JT466 in 3-batch stacks | CONFIRMED |
| E4 | BR-74 mixer changeover | 1 min (plant override of the stated 2 min) | CONFIRMED |
| E5 | BR-7 changeover | enforced in a final pass (only moves work later), plus CAP66 15 min, TR100 and purge | CONFIRMED |
| E6 | BR-10 PM | windows blocked on presses, building machines and upstream machines | CONFIRMED |
| E7 | BR-12 min aging | hard. Max aging: minimised (aging pull, lot caps) and reported, **not** a hard block, because hard blocking scrapped material with no remake (65.6/63.1 %) | OURS (decision) |
| E8 | BR-66/67 | a build starts only when the whole kit exists (FEFO ledger) | CONFIRMED |
| E9 | BRE-01 / BRIL-01 | sidewall, tread and inner-liner lots cover at most 24 h of building | CONFIRMED |
| E10 | BRM-03 butyl follower, BRM-06 TR100 sequence, BRM-09 / BRE-02 one run per shift | partly followed (purge time added; the sequence is not forced) | OURS / open |
| E11 | Not modelled | family/die/inch batching, MH capacity, shift re-planning, 4RC creel/ramp/wire rules, successor masters | — |

## F. Scheduler design assumptions

| # | Assumption | Status |
|---|---|---|
| F1 | Upstream may start 48 h before the plan start (runway, `schedule_open_h`). | OURS |
| F2 | Upstream lots are at most 8 h long (`max_lot_duration_h`). Short-shelf lots are capped to part of their shelf window: slitted material, rubberized ply and cap strip 0.5; belt, apex and ply 1.0. | OURS |
| F3 | Component due time is capped at the real building-plan need minus min aging (the internal CPM time was 6–10 h late). | OURS |
| F4 | An aging pull moves component lots later, into idle time, to their FIFO use. Lots are never made earlier than max aging allows. | OURS |
| F5 | FIFO/FEFO consumption: one pool per item shared by all SKUs. Opening WIP is used first. | OURS |
| F6 | A component that feeds building and other parts (e.g. GUM STRIP → STC) gives building its BOM share. | OURS |
| F7 | Mixers are scheduled (masterbatch and final). | OURS |
| F8 | Tested and turned **off** because they didn't help: timing demand on the day-pool order; compress (left-shift); capacity-aware due times; hard max aging. | TESTED |
| F9 | Curing: a press starts as green tyres flow in (streaming gate). Presses fill gaps; a block never moves to another press. | OURS |

## G. What-if only (not in the real schedule)

| # | Assumption | Result |
|---|---|---|
| G1 | Mixer MPQ 3 batches with no +5 hold batches | 99.9% built / 95.6% cured; 5 Oct fully cured. Needs plant approval to break BRM-01/13. |
| G2 | Tread max aging 72 h (plant spec) instead of DB 24 h | Not yet run; expected to help treads on 1002. |

## H. Open for the plant to confirm

- **Aging values:** DB 24 h for tread, belt, apex and ply, against the plant spec (tread 72 h, PCR belt 4 days).
- **Conti vs BJ:** which building machines are BJ.
- **Rates:** the D1–D9 rates.
- **Calibrations:** the D14 bias cutter factor, the D15 TR100 time and the D17 press allowances.
- **STC MPQ unit:** 180 M, but the item is counted in Nos.
- **TBR MPQs:** edge strips, wraps and BEF001.
- **Butyl rule (BRM-03):** is a clean-out acceptable instead of a friendly follower?
- **BRM-01 / BRM-13 mixer batches:** see G1.
- **Building-machine slack:** the plan loads the building machines at 80–89%.
- **BEF001 capacity:** machine 1802 is a hard wall, running 24 h/day.

## I. Material taken as AVAILABLE without being produced or checked (checked on the 30-09 run)

| # | Material | How it is treated | Status |
|---|---|---|---|
| I1 | **All raw materials** in this plan's BOMs: 207 small chemicals, 28 synthetic rubbers, 17 rubber chemicals, 11 natural rubbers, 10 nylon fabrics, 8 carbon blacks, 7 steel cords, 5 process oils, 2 bead wires, 1 misc (1012099500) | Never scheduled or stock-checked; unlimited | CONFIRMED |
| I2 | **Cap strip chain:** CAP 66 / CAP30 capstrip (kit items for 28 + 1 SKUs, **no opening stock**), their mother rolls, CAP 66- / CAP30- slitted | Scheduled and aged, but never holds a lot or build (V5 guarantee) | CONFIRMED (V5) |
| I3 | **Edge gum chain:** EG01–03, BP01, TEG-01/02, TB WRAP / STC-WRAP, EGSL, **GUM STRIP** (TBR kit item for 24 SKUs, **no opening stock**) | Same as I2 | CONFIRMED (V5) |
| I4 | **Outsourced compounds** MTCM313, TBA827, TCA313 | Removed from routing; purchased, always available | CONFIRMED |
| I5 | **BPG01** (edge strip used by BPGSL01) | No routing, no stock, so passed as available by default | OURS (**found now; plant to confirm or give a rate**) |
| I6 | **No-expiry items** BP01, CAP 66- slitted, CAP30- slitted (no aging row) | Never expire | OURS |
| I7 | **Compound stock** from the old `opening_wip` feed (4,699 rows; the 06:30 snapshot has no compounds) | Counted as usable from t0 | OURS |
| I8 | **Green-tyre opening stock** (5,186 tyres) | Usable from t0 (72 h shelf life from produced time) | DB |
| I9 | **Stock usable only after min aging from its produced time**; stock already expired at plan start is not counted. No stock rows without a timestamp in this run. | — | OURS |
| I10 | **NOT assumed available:** BEF001 (belt edge filler, 1802) and every other component; each must be produced or in stock before a build starts | — | — |

## J. Cycle time / timing assumptions

| # | What | Value used | Source / basis | Status |
|---|---|---|---|---|
| J1 | Green-tyre build time | Per-SKU seconds from `CTP_Master_BuildingAllowable` (374 rows), else routing GT BUILDING (49 rows); the same on every TBM | DB. Only the Conti value is used; BJ is about 10% slower, but which TBMs are BJ is unknown. | DB / OURS |
| J2 | Building changeover | The plan's own `CO_Mins` per row (68 h in the plan). No building changeover matrix is used. | plant plan | OURS |
| J3 | Curing time | The plan's `cycleTime` (equal to `CTP_Master_CuringCycleTime`) | DB | DB |
| J4 | Press block length | qty ÷ **2 cavities** × (cure time + **2.5 min PCR / 6.0 min TBR** load/unload) | 2 cavities on every press and the allowances are assumed | OURS |
| J5 | Press mould changes | Taken from the plan's CHANGEOVER rows | plant plan | — |
| J6 | Upstream run time | qty ÷ routing `proc_time` ÷ **efficiency** (0.96 on 46,531 rows, 0.78 on 228) | DB routing; the user asked to apply efficiency | DB |
| J7 | Mixer lot time | batches (qty ÷ routing batch weight) × routing sec/batch | DB routing | DB |
| J8 | Approximated rates | GL7, NYLON23-02, NYDBCHF, SA15001/17001, SW15001, SPB02/03, TBPGUM, TBWB, XMT180, T7110, SWRS30 (see D1–D9) | medians of similar items | OURS |
| J9 | Bias cutter 2001 | Routing rate ×1.66 | calibrated to measured throughput (provisional) | OURS |
| J10 | Belt / bias cutter | 1 cut = 1 tyre (B1 + B2) | | CONFIRMED |
| J11 | **BEF001 rate** | Routing: **13 m/min at 96%**; the plant rule sheet (EXT-17) says **10 m/min at about 95.5%** | Mismatch: if the plant value is right, 1802 is about 30% slower than modelled | **OURS: plant must confirm** |
| J12 | Changeover, upstream | `CTP_Master_ChangeoverMatrix` on 23 mapped machines; **flat 15 min default on 6 machines with no matrix line: 1802, 2201, 2501, 2502, 701, 702** | DB + default | OURS |
| J13 | Cutters (parent-aware) | 3 min for the same mother roll, 15 min otherwise (when the matrix has no pair) | v6 rule | OURS |
| J14 | Mixer changeover | 1 min; + TR100 5 min on master↔final; + butyl purge 5 min | BR-74 / assumed | CONFIRMED / OURS |
| J15 | CAP66 clean-out | 15 min minimum | BR4RC-05 | CONFIRMED |
| J16 | Transfer lag | Routing `transfer_time_min` (up to 90 min) added to min aging on every producer → consumer step; green tyre → press 90 min | DB / confirmed | DB / CONFIRMED |
| J17 | Aging | Min / max from `CTP_Master_AGING` (DB); green tyre 72 h; fallbacks by item type where an item has no row | DB / CONFIRMED / OURS |
| J18 | Machine availability | 24 h × 7 days, 3 × 8 h shifts, **no breaks, meals, breakdowns or speed loss** beyond routing efficiency; only the downtime_reason PM windows are blocked | OURS |
| J19 | Plant day | 07:00 → 07:00; shifts A 07–15, B 15–23, C 23–07 | plant | — |
| J20 | Upstream runway | Upstream may start up to 48 h before the plan start | config | OURS |
| J21 | Max upstream lot length | 8 h | config | OURS |
