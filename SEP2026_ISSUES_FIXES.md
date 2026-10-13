# September 2026 run: issues, fixes and impact

**Scope:** combined PCR + TBR run for 26 Sep 07:00 → 1 Oct 07:00 (5 plant days).
- **Plans:** the 4 files in `discard/`.
- **Live data:** WIP from `jkplanning_CTP.opening_wip`, aging from `JK_CTP_PPDS.CTP_Master_Aging`.

**Run it with one command:** `python run_sep_fixed.py`. Output lands in `output/CTP_SEP_PCRTBR_FIXED_V2/`.

**The runs compared below:**
- **Before:** the original run.
- **Fix set 1:** issues #1–#8 in section 2.
- **Fix set 2:** fix set 1 plus the RCA corrections C1, C2 and C4 in section 6. This is the current configuration.

---

## 1. Result

| Metric (plan window ends 1-Oct 07:00) | Before | Fix set 1 | **Fix set 2 (current)** |
|---|--:|--:|--:|
| Last cure | 5-Oct 09:22 | 4-Oct 02:44 | **3-Oct 03:14** |
| Makespan from plan start (plan = 5.0 d) | 9.10 d | 7.82 d | **6.84 d** |
| **Curing demand covered by 1-Oct** (55,286) | 51.3% | 80.7% | **83.2%** |
| Tyres cured after the plan window | 26,911 | 10,674 | **9,276** |
| Cured with a real green tyre (any time) | 82.3% | 98.2% | **98.2%** |
| **Green tyres built by 1-Oct** (55,647) | 43.5% | 93.9% | **95.7%** |
| Curing on its own planned day | 30.6% | 50.5% | **54.6%** |
| Kit shortfall: tyres cured with no green tyre | 9,452 | 629 | **629** |
| Building rows skipped as "unbuildable" | 3,163 tyres | 0 | **0** |
| Maximum building slide vs plan | 3.68 d | 2.68 d | **1.02 d** |
| C4 material proof: builds starting before their material exists | – | – | **only NYDBCHF** (no routing time; data gap) |
| Component aging violations | 0 | 0 | 0 |
| Busiest machine, true utilisation (5-day window) | 57% | 57% | 74% (mixer K430-1); none over capacity |

### Day-wise output, fix set 2 (all products, plant day 07:00–07:00)

| Plant day | Build plan | **Built** | Built on its planned day | Cure plan | **Cured** | Cured on its planned day |
|---|--:|--:|--:|--:|--:|--:|
| 26-Sep | 150 | **150** | 100% | 4,634 | **4,284** | 92% |
| 27-Sep | 6,191 | **6,191** | 100% | 5,522 | **3,610** | 62% |
| 28-Sep | 16,768 | **16,716** | 95% | 13,006 | **9,364** | 56% |
| 29-Sep | 15,520 | **13,558** | 73% | 15,034 | **12,611** | 46% |
| 30-Sep | 17,018 | **16,617** | 74% | 17,090 | **16,141** | 48% |
| 01-Oct | – | 2,415 | – | – | 8,972 | – |
| 02-Oct | – | 0 | – | – | 304 | – |

**Built per day, before the fixes (27–30 Sep):** 2,486 / 10,684 / 4,018 / 6,891.

**Reasons for every late block** are in `output/CTP_SEP_PCRTBR_FIXED_V2/DAYWISE_PLAN_VS_SCHEDULE.xlsx`.
- The report script behind that workbook had two bugs of its own, both now fixed: a block ending exactly at 07:00 was counted in the next day, and cure blocks were matched to plan rows by the wrong index.
- The curing "reason by planned day" tables in the earlier workbook (for fix set 1) are affected; the totals were not.

---

## 2. Issues found and fixed

Each issue lists its type, where it was, what was wrong, the fix, and the measured impact.

### #1: Upstream deadlines ignored the lead time between BOM levels (main cause)
- **Type:** logic / config.
- **Where:** `config.yaml` → `honest_lft_anchor: full`; [phases/phase4_cpm.py](phases/phase4_cpm.py) (honest LFT re-anchor, and AEST = LST − window); root in [phases/phase1b_demand_explosion.py](phases/phase1b_demand_explosion.py).
- **What was wrong:**
  - Phase 1b gives **every BOM level the same `need_by`**, the green-tyre build time: master batch, final compound, tread, liner and green tyre alike.
  - With `full`, phase 4 set each lot's deadline to its **own** last need minus min aging, so the processing time and min aging of the 4–5 levels in between vanished.
  - As a result, **3,000 of 3,565 DAG edges (84%)** had the producer's deadline *after* the consumer's latest start.
  - The earliest-allowed start (AEST) came from those late times, so 102 lots (mostly mixing) could not start before their AEST. The whole chain bunched into 29–30 Sep and queued.
- **Fix:** `honest_lft_anchor: report`. Placement uses the precedence-consistent backward-CPM times; the honest numbers stay in the report only.
- **Impact:**
  - Measured alone: built by 1-Oct 24,229 → 48,606; kit-late blocks 145 → 27; tyres cured after the window 26,911 → 9,293.
  - Aging not worse: lots dead before first use 0.5%.

### #2: Shared green tyre credited to one SKU only
- **Type:** logic.
- **Where:** [phases/phase5_forward_placement_v2.py](phases/phase5_forward_placement_v2.py) (`_lot_sku1`, building-echo kit gate) and [phases/phase6_curing_revision.py](phases/phase6_curing_revision.py) (`KitPool`, `load_gt_code_to_sku`).
- **What was wrong:**
  - Phase 2 pools one green tyre across all SKUs that use it (23 GT lots, e.g. GT 5007 → `1122110020016KUH50`, `1122129A20000JUH50`).
  - Phase 5 keyed the kit gate by `skus_set[0]`, so the other SKUs' build rows were **skipped** (3,163 tyres).
  - Phase 6 treated shared GT codes as "ambiguous" and never mapped them, so **7,512 tyres were cured with no green tyre behind them**.
- **Fix:** new flags `echo_kit_by_gt: true` and `kitpool_key_by_gt: true`.
  - The kit gate, re-cut and verify (phase 5) and the kit pool (phase 6) are keyed by **GT code**.
  - Each SKU maps to its GT through the BOM (exact code).
  - Opening stock of a shared GT is also used.
- **Impact:** skipped build rows 19 → 0; "never built" cures 7,512 → 1; kit shortfall 9,452 → 629.

### #3: A stale June demand file was being read (hidden bug)
- **Type:** logic / file handling.
- **Where:**
  - [phases/phase1b_demand_explosion.py](phases/phase1b_demand_explosion.py) writes `.csv` *or* `.csv.gz`.
  - [phases/phase3_dag_construction.py](phases/phase3_dag_construction.py), [phases/phase3_resolve_lot_blocks.py](phases/phase3_resolve_lot_blocks.py), [phases/phase6_curing_revision.py](phases/phase6_curing_revision.py) and phase 5 read `.gz` first.
- **What was wrong:**
  - The June run left `outputs2/phase1_demand_updated.csv.gz` (232,205 rows) behind, and the September run wrote `.csv` (15,958 rows).
  - **Phase 3 computed every edge consumption quantity from June demand.** That is why a 300 m lot showed 8,063 m allocated (up to 27×) and phase 5 printed a false "REAL SHORTAGE 3.85M".
  - Phase 6's WIP reconciliation printed an impossible **−248,513 tyres**.
- **Fix:** phase 1b deletes the stale twin whenever it writes, and every reader picks the **newest** file.
- **Impact:** edge quantities now come from September demand (15,958 rows), and the false shortage dropped from 3.85M to 0.49M.

### #4: TBR green-tyre WIP codes didn't match the BOM
- **Type:** data.
- **Where:** `jkplanning_CTP.opening_wip.itemcode`; fixed in [build_sep_inputs.py](build_sep_inputs.py).
- **What was wrong:** TBR green tyres are stored under descriptions (`11.00R20JDE++`, `GT 5075 - 295/90R20 JUH XF`), so 1,108 real tyres could never be used.
- **Fix:** re-key them via the plant's own `gt_inventory_manual_pcr/_tbr` (`ItemCode → SKUCode` = BOM GT code), only where that gives exactly one BOM GT. The original code is kept in `itemcode_raw`.
- **Impact:** 1,108 tyres recovered. 78 tyres (`GT 2528 RAN ATW`) are not in the BOM and are reported as unmatched.

### #5: Blank routing `proc_time` meant the item was silently never produced
- **Type:** data.
- **Where:** `jkt_routing_pcr/_tbr`; fixed by the new [apply_routing_gapfill.py](apply_routing_gapfill.py).
- **What was wrong:** phase 2 makes **zero lots** for an item with no `proc_time`. For example, `GT T1457 STAR` (1,301 tyres of `1325214617107MSXT0`) was never built.
- **Fix:**
  - Only items this plan needs are touched; curing rows are skipped.
  - A green tyre takes the plant's own building-plan cycle time.
  - Anything else takes the median of the same operation on the same machines, flagged `ASSUMED_SIBLING_MEDIAN`.
  - Items with no data are left blank and reported. Every fill is logged in `inputs/routing_gapfill_log.csv`.

  | Item | Filled value | Source |
  |---|---|---|
  | GT T1457 STAR | 86.22 SEC | plant building plan |
  | T7110 | 130.26 SEC/BATCH | **assumed** (sibling median) |
  | XMT180 | 110.26 SEC/BATCH | **assumed** |
  | SWRS30 | 30 M/MIN | **assumed** |
  | NYDBCHF, MTCM313, TBA827, TCA313 | not filled | no data at all; still not produced |

- **Impact:** the SKU with that green tyre is now built.

### #6: Phase 1c netted demand against expired WIP
- **Type:** logic.
- **Where:** [phases/phase1c_mrp_netting.py](phases/phase1c_mrp_netting.py) and [phases/db_loader.py](phases/db_loader.py) `load_wip_by_item`.
- **What was wrong:**
  - Phase 1c nets against **all** WIP. Its own header says the expiry filter (#25) was reverted to match v6.
  - About 3,970 rows are already expired at plan start, including 54,108 kg of final compound.
  - Phases 5 and 6 refuse expired stock, so the phases disagreed and building was told to make less than it could actually use.
- **Fix:** `phase1c_exclude_expired_wip: true`. The loader drops rows where `produced_time + MaxAging < plan start` (3,967 rows / 212 items this run).
- **Impact:** a correctness fix. Net demand rises slightly (4,080k → 4,096k units), and the numbers are now consistent across phases.
- **Note:** this reverses the 2026-07-22 "phase 1c = v6 verbatim" directive, deliberately and behind a flag.

### #7: Building cycle time inflated on short shift slices
- **Type:** my own input conversion.
- **Where:** [build_sep_inputs.py](build_sep_inputs.py).
- **What was wrong:** the plant plan slices a day into shifts but keeps the full 8-hour window on every slice, so a 1-tyre slice read as 480 min/tyre.
- **Fix:** short slices use the rate from the same SKU's full slices on that machine. Plant data only.
- **Impact:** 36 slices re-rated, **191.9 phantom building-machine hours removed**.

### #8: The live DB connection fails often
- **Type:** infrastructure.
- **Where:** 35.208.174.2.
- **What was wrong:** the big `opening_wip` table often times out.
- **Fix:** `build_sep_inputs.py` retries 4 times. On total failure it uses the last snapshot in `db_pipeline/raw/<db>__<table>.csv` and prints `SNAPSHOT` clearly.
- **This run:** `opening_wip` came through **LIVE** on the 3rd attempt (33,054 rows); all other tables were live too.

### Tested and ruled out
- **Machine cycle time / capacity:** all upstream machines 2× faster gave almost no change (145 → 139 kit-late blocks). The capacity wall is empty for the 5-day window, and the highest utilisation is 57%.
- **Earlier upstream start:** a 4-day runway instead of 2 gave no change.

---

## 3. What is still open (needs the plant or a later fix)

| # | Item | Why it matters |
|---|---|---|
| O-1 | **Confirm `CTP_Master_Aging` values.** Tread max 24 h (was 72), final compound 72 h (was 120), some master compounds 24 h (was 360). | The old values alone moved "built by 1-Oct" from 24.2k to 36.5k. |
| O-2 | Remaining lateness: 10,674 tyres cured after 1-Oct. Building: 16,694 tyres queued behind a kit-late block on the same machine, rooted mainly in **Inner Liner (1101)** and **Steel Belt (801/802)**. Curing: 20,941 wait for a late green tyre, and 7,522 wait on the press. | The plant plan has no slack on the building machines or presses for 29–30 Sep. A proper fix is level-aware `need_by` in phase 1b (each BOM level offset by its downstream lead time), plus confirmed aging. |
| O-3 | 4 routing items have no `proc_time` at all (NYDBCHF, MTCM313, TBA827, TCA313), and 3 values are **assumed** (T7110, XMT180, SWRS30). | Need plant-measured values. |
| O-4 | Green tyres past shelf life at the press: 95 blocks (was 25). | Not a regression. Before the fix, 676 blocks had no kit at all, so their age couldn't be measured; now 1,956 blocks are measured. Older opening WIP drawn first shows up here. |
| O-5 | Phase-5 "REAL SHORTAGE 486,006" message. | The residual that phase 3 charges to the last producer (a fail-safe), not a production gap. Phase 2 lots match net demand except the O-3 items. The message wording should be fixed. |
| O-6 | 78 tyres of `GT 2528 RAN ATW` WIP are not in the BOM. | A BOM / master data question. |
| O-7 | Bias-cutter ×1.66 calibration (FLAW_REGISTRY C-2) is still provisional. | Unchanged; the plant's answer is pending. |

---

## 4. Files changed / added

| File | Change |
|---|---|
| `config.yaml` | `honest_lft_anchor: report`; `echo_kit_by_gt`, `kitpool_key_by_gt`, `phase1c_exclude_expired_wip` (all true); September inputs (`curing_sep_comb.xlsx`, `building_sep_comb.xlsx`, `aging_sep_live.csv`, `opening_wip_live.csv`); `expected_plan_month: "2026-09"`. Backup: `config.yaml.bak_pre_sep0926`. |
| `phases/phase5_forward_placement_v2.py` | GT-keyed kit gate, re-cut and verify (#2); newest demand file (#3); report-only `phase5_building_echo_gates.csv` (which component held each building block). |
| `phases/phase6_curing_revision.py` | GT-keyed `KitPool` and shared-GT WIP seeding (#2); newest demand file (#3). |
| `phases/phase1b_demand_explosion.py` | Delete the stale `.csv`/`.csv.gz` twin (#3). |
| `phases/phase3_dag_construction.py`, `phases/phase3_resolve_lot_blocks.py` | Newest demand file (#3). |
| `phases/phase1c_mrp_netting.py`, `phases/db_loader.py` | Expired-WIP filter behind a flag (#6). |
| `build_sep_inputs.py` (new) | Builds the September inputs from `discard/` and the live DB; building cycle-time fix (#7); TBR GT-code fix (#4); DB retry and snapshot (#8). |
| `apply_routing_gapfill.py` (new) | Fills blank routing times (#5); writes `inputs/routing_gapfill_log.csv`. |
| `run_sep_fixed.py` (new) | One command for the whole corrected run. |

Every changed phase file is behind a config flag, or is a pure file-selection fix. Setting the flags back reproduces the old behaviour.

## 5. Outputs
- `output/CTP_SEP_PCRTBR_FIXED/CTP_FLOOR_SCHEDULE.xlsx`: the floor schedule.
- `output/CTP_SEP_PCRTBR_FIXED/DAYWISE_PLAN_VS_SCHEDULE.xlsx`: day-wise plan vs schedule, with reasons and root components.
- `output/CTP_SEP_PCRTBR_FIXED/PHASEWISE/`: every phase's files.
- `_run_sep_fixed.log`: full run log. The pre-fix run is `output/CTP_SEP_PCRTBR_LIVEWIP_20260926/` and `_run_sep_livewip.log`.

---

## 6. RCA corrections: C1, C2 and C4 (2026-09-27)

**What prompted them:** the RCA of the day-2 TBR slip. GT 5069, 5070 and 5096 waited about 2 days for BEF001 and R-STC007, while machines 1802 and 802 were **idle on 25–26 Sep**. Their capacity was fine; the scheduler never asked them for enough, because of three defects in the demand chain.

| Layer | Defect | Evidence |
|---|---|---|
| Demand (phase 1c) | (a) green-tyre stock deducted again, although the plant plan is already net of it (`Planned+GT`) | 55,647 → 52,567 green tyres; GT 5070 −95, GT 5096 −66 |
| | (b) a parent's saving over its own shelf-life window removed from the child's shorter window | day-1/2 need wiped |
| | (c) a parent's saving spread pro-rata over all of the child's demand, so one route's stock erased another route's need | 191 of 349 components cut by more than their own stock; GUM STRIP, TEG-02, MB184 at 0 with no stock of their own |
| Allocation (phase 3) | ties broken alphabetically; any uncovered remainder charged to the **last** producer lot | GT 5069 waited for BEF001_L0009 (29-Sep 21:25) while 10,524 m of BEF001 was on hand against 6,505 m needed |
| Gate (phase 5 echo) | "kit ready" read from the phase-3 split, not from physical stock | inherits both problems above |

**C1: time-phased, level-by-level MRP** ([phases/mrp_time_phased.py](phases/mrp_time_phased.py); flag `phase1c_time_phased_mrp: true`)
- Items are processed from the tyre downward. Each is netted against **its own live stock only**, in time order, and only while the stock is still within shelf life at the time of need.
- Only the **net** requirement is passed down to the children, per BOM route and per block. BOM edges use the same per-tyre convention as phase 1b.
- Green tyres are not netted when the building plan is echoed.
- **Parity check on every run:** with zero stock, C1 must reproduce phase 1b cell for cell. It does: 15,958 / 15,958 cells, 0 mismatch. If the check fails, phase 1c falls back to v6.
- **Result:** net demand is 6,135,984 (a 3.3% deduction), against 4,095,811 (35.4%) under v6.
  - Most of the live stock has a short shelf life and expires before the day it would be used, because building only ramps up on day 2. v6 was counting stock that dies unused.
  - Restored day-1/2 need: GT 5070 57 → 152, R-STC007 75 → 142, GUM STRIP 0 → 4,326, BEF001 4,687 → 5,442.

**C2: physical material-ledger kit gate** ([phases/phase5_forward_placement_v2.py](phases/phase5_forward_placement_v2.py); flag `echo_kit_ledger: true`)
- **Supply per component item:** live opening WIP at t0, plus every lot at finish + min aging, minus what phase 3 gives to non-building consumers.
- **Consumption:** each committed build block's tyres × the SKU's per-tyre BOM quantity.
- **A block starts** when every item's cumulative supply covers cumulative consumption. There is no more alphabetical order or last-lot waiting.
- **Aging stays a report**, per the 2026-07-16 directive; it does not reject supply.
- **The belt re-cut check** now replays the same ledger, with a 15-minute tolerance.

**C4: material-balance proof** (`outputs2/phase5_material_ledger.csv`)
- After all re-cuts, every build block is replayed against the final schedule.
- **Result:** all blocks have their material physically available, except the **54 blocks that need NYDBCHF**. NYDBCHF has no `proc_time` in the routing and is never produced; that is a data gap (O-3), not a scheduling error.

**Measured effect of C1 + C2 + C4 (on top of fix set 1):**
- Last cure 4-Oct 02:44 → **3-Oct 03:14**.
- Green tyres built by 1-Oct 93.9% → **95.7%**; day 2 (27-Sep) building is now **100% on time, TBR included**.
- Maximum building slide 2.68 → **1.02 d**.
- Curing demand covered by 1-Oct 80.7% → **83.2%**.

**What limits the plan now (next constraints, not defects):**
1. **The TBR compound → edge-strip chain.** Compound B1144 (F270) → BEF001 (machine 1802) bunches into 29–30 Sep at ~24 h/day, while 1802 ran only 5–8 h/day on 26–28 Sep. The compound is made just in time within its 72 h shelf life, so BEF001 cannot start earlier. Fixing this needs upstream "make earlier inside shelf life" dispatching, and/or plant confirmation of the compound shelf life.
2. **Press sequencing:** 10,919 cure tyres wait behind an earlier late block or a changeover on the same press. The press plan is fixed per press and has no slack.
3. **Building-machine queue:** 8,773 tyres queue behind an earlier late block on the same building machine, mostly on 29–30 Sep.
4. **Data:** NYDBCHF (no routing time) and the 3 assumed routing values (O-3), plus the aging values (O-1).

**Files for C1/C2/C4:**
- **New:** `phases/mrp_time_phased.py`.
- **Changed:** `phases/phase1c_mrp_netting.py` (flag wiring; the v6 cascade moved unchanged into `_v6_cascade`), `phases/phase5_forward_placement_v2.py` (ledger gate, ledger re-cut check, C4 proof), `config.yaml` (`phase1c_time_phased_mrp`, `echo_kit_ledger`).
- **Revert:** set both flags to `false`.


---

## 7. Business rules (businees_rules/*.xlsx), incorporated 2026-09-27

**Where:**
- **Config:** `config.yaml` → `business_rules:`, plus `mixing_mpq_min_batches` and `smooth_lot_span_itypes`.
- **Code:** phase 2 (sticky compounds, buffer batches) and the phase-5 compliance pass.
- **Audit:** `python audit_business_rules.py <run_dir>`.
- **Output:** `output/CTP_SEP_PCRTBR_RULES/` (audit in `BUSINESS_RULE_AUDIT.csv`).

**What was implemented:**
- **BRM-01:** mixing MPQ of 5 batches (the MPQ master said 3; the business rule wins).
- **BRM-02:** JT444 and JT466 lots are rounded up to 3-batch stacks.
- **BRM-13:** +5 buffer batches on each compound's last lot, except the special compounds (butyl, sticky). This adds 292,504 kg across 209 compounds.
- **BRE-01 / BRIL-01:** sidewall, tread and inner-liner lots are capped at a 24-h building span.
- **Phase-5 compliance pass,** per machine. It never lets a lot finish after its consumer needs it, and never pulls a lot more than 8 h earlier. It enforces:
  - **GEN-07:** exact changeover to the real neighbours in time. This fixes a bug: placement took the changeover from the last-*placed* lot and never checked the lot after.
  - **BR4RC-05:** a 15-min CAP66 cleanout.
  - **BRM-06:** a TR100 cleanout (**5 min assumed**; TR100 is not in the routing).
  - **BRM-03 / BRM-05:** mixing sequence (butyl, MK3).
  - **BRM-09 / BRE-02 / BRC-02 / CUT-08:** same item in one run per shift.
  - **BRC-01:** Belt 1 followed by Belt 2.

**Audit, before → after the rules:**

| Rule | Before | After |
|---|---|---|
| BRM-01 MPQ 5 batches | violated (3 batches) | **followed** |
| BRM-02 sticky 3-batch stacks | violated | **followed** |
| BRM-06 TR100 cleanout | 10 violations | **followed (0)** |
| BRM-13 +5 buffer batches | not modelled | **followed** |
| BRE-01 / BRIL-01 24-h horizon | 73 lots over | **followed (0)** |
| GEN-07 changeover matrix | 156 of 408 too short | **29 of 440**, all on 1002 and 901 |
| BRM-09 compound once per shift | 61 | **27** |
| BRE-02 / BRC-02 item once per shift | 19 | **17** |
| BRM-03 butyl sequence | 16 | 15 (on K270M / K430-2) |
| BRC-01 belt pairing | 32 of 71 | 36 of 71 |
| BR4RC-05 CAP66 15 min | 3 | 2 (on 901) |
| BRM-05 ML405 → MK3 | "violated" | **N/A:** no MK3 demand in this plan |

**Why the remaining violations are left:**
- They all sit on **9 machines the pass could not re-sequence without delaying the building plan:** master mixers K270M, F440, K430-1, K430-2 (up to 84% true utilisation), plus 901, 1002, 1901, 2001 and 802.
- On those machines, adding every rule changeover would make some lots finish after the build that needs them. Resolving that needs the plant to accept later building, or to add capacity or shifts.

**Cost of following the rules:**

| Metric | Without rules | With rules |
|---|--:|--:|
| Built by 1-Oct | 95.5% | 92.1% |
| Cured by 1-Oct | 84.2% | 81.3% |
| Last cure | 3-Oct 03:09 | 3-Oct 02:47 |
| Busiest mixer, true utilisation | 74.5% | 84.0% |

**Still not followable (data missing or not modelled):** PM calendar, material-handling capacity, TBR changeover matrices (14 machines), die/compound/inch/wire family keys, successor and roll-length masters, 4RC creel/ramp/wire-gap logic, shift-wise replanning, and a 07:00 opening-stock snapshot.

## 8. Component max aging enforced (2026-09-29, 29 Sep – 5 Oct plan)

### How the problem was found
- **Check used:** `verify_aging_fifo.py <run_dir>` replays the floor. Components are made evenly over each run, and builds use them evenly over each run. Each component item has one first-in-first-out pool shared by all SKUs.
- **Result before the fix:** 15% of component quantity (674k units) reached building past max aging. That is about 63k tyres, 65% of the plan.
- **The scheduler's own report missed it:** it showed only 1.1% of links expired, because it measures against internal phase2 green-tyre lots, not the real build rows.

### Root causes
1. **The kit gate ignored expiry.** The phase5 ledger counted a lot as supply forever. Aging was report-only (directive of 16 Jul).
2. **Placement anchored on the first need.** The eager-shelf rule and the aging cap were measured from a lot's first need on the phase2 timeline, not its last use. 24 h items finished up to 20–25 h before their first draw.
3. **The plan timeline is shift-bucketed.** Phase1b places a whole day of SNCT002 or cap strip demand at 07:00–15:00. Lot sizing therefore saw 8 h lots, while the floor draws the same quantity over 30 h or more, and the tail expires.
4. **The ledger checked in commit order, not start order.** Once gap-fill and kit-late re-ordered blocks, the ledger reported false shortages. That made the aging pull's safety check revert good moves (343 reverts).

### Fixes
| Change | File | Config |
|---|---|---|
| **AGING PULL.** Runs after the building echo, when build times are fixed. It computes an item-level FIFO over the ledger and gives each lot its first use and last use (pro-rated inside the build block). Each lot is moved **later**, into its own machine's idle time, so that it finishes no later than first use − min aging and no earlier than last use − max aging. There is no order cap, so a stuck lot only changes the FIFO order. It runs in 4 rounds, then moves feeders later to meet their consumers. It is verified with the ledger, and offending moves are reverted. It also hands an aging floor to the business-rule pass, which may no longer pull a lot back into over-age or let a just-in-time lot slip. | phase5 | `aging_pull_retime`, `aging_pull_margin_h`, `aging_pull_rounds` |
| The ledger replay now runs in build **start** order. | phase5 `_L_replay` | — |
| **Short-shelf quantity cap.** Lot quantity is at most frac × (max − min aging) × average hourly demand. The fraction is 0.5 for slitted material, rubberized ply and cap strip, and 1.0 for steel belt, bead apex and ply. Compounds are not touched, because the mixers are saturated. | phase2 | `aging_split_qty_frac`, `aging_split_qty_itypes`, `aging_split_short_max_h`, `aging_split_short_frac` |

### Measured results (same inputs; the deliverable is output/CTP_29SEP-05OCT_DELIVERABLE_AGING_20260929)
| Run | Built / cured by plan end | Component quantity past max aging | Tyres with every component fresh |
|---|---|---|---|
| Before | 92.4% / 88.6% | 674k (15.3%) | 14.6k (15%) |
| Pull only (with the commit-order bug) | 92.4% / 88.6% | 551k | 40.6k |
| Pull + quantity cap 0.5 on all ≤48 h items | 89.4% / 86.7% | 19.8k | 77.7k (80%). Belt and master compound over-split: kit-late 164 → 206. |
| Pull + quantity cap on 3 types only | 92.0% / 88.3% | 48k | 54.2k |
| **Final: pull + per-type caps** | **92.3% / 88.5%** (PCR 94.1/89.9, TBR 85.4/82.7) | **69k (−90%; 35k of it is cap strip in 8 rows)** | **61.9k (64%)** |

### What remains
- **About 180 build rows** still have some component past max aging:
  - rubberized ply 8.4% (GUM STRIP and TBP lots);
  - bead apex 5.1%;
  - slitted material 4.7%;
  - belt 2.1%;
  - tread 2.7%, part of it old opening WIP.
- **Cause of the remainder:** the machine is busy near the draw time, or a lot is still larger than one shelf window. A 0.5 cap everywhere clears most of it, but costs 3 points of output.
- **GUM STRIP** is short on 4 TBR build blocks (known, pre-existing).
- **Green tyres:** 18 cure blocks (586 tyres) are pressed past 72 h.
- **Next lever:** have the plant confirm the 24 h DB max aging for belt, apex, ply and tread (the plant spec gives tread 72 h and PCR belt 4 days).

## 9. Live DB inputs and day-pool building sequence (2026-09-29, 29 Sep – 5 Oct)

Inputs now come from the tables the plant planning engine uses. DB host 35.208.174.2; the row counts match the engine's DEV tables.

| Input | Before | Now | Effect |
|---|---|---|---|
| Building cycle time | Shift window ÷ qty. That is wrong: the Shift Schedule is a DAY plan sliced pro-rata (plant Notes sheet), and 3–4 SKUs share one 8 h window. | `JK_CTP_PPDS.CTP_Master_BuildingAllowable` (current, per SKU); fallback `jkt_building_cycle_times`, then `CTP_Master_Routing`. | TBM hours 3,316 → 2,335. PCR 69.8 → 58.8 s per tyre, TBR 236 → 197 s. Actual MES scans ≈ 60 s. |
| BOM | `bom_comb.csv` (2 Sep) | `CTP_Master_BOM` (live) | 278 links on 35 plan SKUs changed: TBR belt wrap halved, chafer SNCT003 +55%, TBR B333 chain +34%, PBP37→BP01 halved. |
| Routing | `routing_comb.csv` (2 Sep) | `CTP_Master_Routing` (live) | 3 rows changed. |
| WIP | `jkplanning_CTP.opening_wip` (old feed) | `JK_CTP_PPDS.CTP_Opening_WIP`, 06:30 snapshot of the plan-start day. Compound rows still come from the old feed, because the snapshot has none. | +~500 lots, e.g. 93 inner-liner lots and 56 calendered rolls. |
| Checked, no change needed | | Curing cycle (`CTP_Master_CuringCycleTime`): identical. Downtime: press 4844 PM on 29 Sep, which the plan already avoids. Holidays: none in the window. | |

**Day-pool sequencing** (phase5, `building_day_pool_sequence`). Each (machine, plant-day) may start from 07:00. Its rows are built in order of CURING NEED, meaning the time the presses first need that row's tyres after GT stock and earlier builds. File order had built GT 1402 fourth on TBMPCR4, which delayed it to 01:07 the next day.

### Results (same 29-09 plan)
| Run | Built / cured by plan end | PCR | TBR | Component qty past max aging | GT > 72 h |
|---|---|---|---|---|---|
| Aging fix only (section 8) | 92.3% / 88.5% | 94.1 / 89.9 | 85.4 / 82.7 | 69k | 586 |
| + live cycle, BOM, routing | 91.7% / 88.4% | 93.5 / 89.7 | 84.5 / 82.7 | 39k | 571 |
| **+ 06:30 WIP + day-pool sequencing** | **98.2% / 94.4%** | **99.2 / 95.5** | **94.5 / 89.9** | **31k (0.7%)** | **552** |

### Remaining
- **30 Sep curing:** 11,602 of 12,594. On day 2 the plan cures green tyres from 07:00 that it only builds during the same day, and there is little GT stock to bridge (plan timing).
- **GUM STRIP:** 7 TBR build blocks on 6 Oct (after the window) are short.
- **Min aging at 07:00:** 3 day-1 blocks start while inner liner IL007 / sidewall SWRS43 are still inside min aging (0.6–5 h).
- **Conti vs BJ:** the DB master has one building cycle per SKU (Conti). If some machines are BJ (+10%), which machines is not in any table.
- **Deliverable:** output/CTP_29SEP-05OCT_DELIVERABLE_LIVE_20260929.

### 9b. Cap strip and edge gum assumed available (V5 guarantee), 2026-09-29
The V5 planner checks cap strip and edge gum supply hour by hour and caps building so that it never waits for them. Config `kit_gate_assume_available` (cap strip / mother roll / CAP 66 slitted, EG01–03, BP01, TEG-01/02, WRAP, EGSL, GUM STRIP): these items are still scheduled and aged, but never gate a lot (`_material_ready_ns`), a build block (ledger) or the kit check. BEF001 (belt edge filler, machine 1802) is NOT included.

| Result | Built / cured | PCR | TBR | Ledger blocks short |
|---|---|---|---|---|
| Before | 98.2 / 94.4 | 99.2 / 95.5 | 94.5 / 89.9 | 7 (GUM STRIP) |
| After | **98.4 / 94.6** | **99.2 / 95.6** | **94.8 / 90.5** | **0** |

What is left:
- **BEF001:** holds 82 TBR blocks (≈7.7k tyres, ≈10 h). This is the main TBR constraint.
- **Inner liner IL056/IL039/IL043:** about 7–9 h late on 13 blocks.
- **Day-1 IL007/SWRS43:** WIP made just before 07:00 is still inside min aging.
- **GUM STRIP aging:** our own GUM STRIP lots are 22% past 48 h. This doesn't matter if V5 supplies GUM STRIP.

### 9c. Line-specific MPQ (2026-09-30)
**The gap.** The MPQ file (`jkt_mpq_corrected.xlsx`, set on 23 Jul) is the plant's PCR sheet only, so TBR items were floored with PCR values. TBR bead apex used 400 against the plant's 33 (+4,440 over-made), and ply used 219 against 130.

**The fix** (`mpq_tbr_from_db`):
- TBR-only items (identified by `CTP_Master_BOM.tyre_type`) take the TBR rows of `JK_CTP_PPDS.CTP_Master_MPQ`.
- If there is no TBR row for the item type, no floor is applied.
- A unit guard applies a floor only in the item's own unit. It refused the plant's STC row (180 M) because the L-STC/R-STC items are in Nos.

**TBR over-make, old → new:**
| Item type | Before | After |
|---|---|---|
| Bead apex | 4,440 | 29 |
| Ply | 537 | 181 |
| Tread | 194 | 0 |
| Edge strip | 3,742 | 0 |
| Steel belt (plant 200 Nos, was 96) | 631 | 4,041 |
| Wound bead (plant 600, was 400) | 2,006 | 3,306 |

**Coverage, built / cured:** 98.4 / 94.6 → 97.4 / 93.5. PCR is 98.2 / 94.5 and TBR 94.1 / 89.5. The PCR inner liners (IL056/IL039/IL043) waited about 3 h longer. BEF001 holds fewer blocks: 82 → 62.

### 9d. Input audit, all sources (2026-09-30)
**Newly live:**
- The changeover matrix is now `CTP_Master_ChangeoverMatrix` (updated 26 Sep, 293k pairs). The 3-Jul xlsx it replaces had 84k pairs with different minutes.
- The 10 TBR machines are mapped to their matrix lines: 1901, 2001, 802, 1801, 1803, 2101, 1102 and 2403–2405. Before this they fell back to a flat 15 min.
- The plan's own `CO_Mins` (68 h) are added to building time.

**Result:** 97.4 / 93.4 built / cured (PCR 98.3 / 94.4, TBR 93.8 / 89.3). Ledger short 0; GT over 72 h: 552.

**Still approximated** (the DB has no data):
- rates for GL7, NYLON23-02, NYDBCHF, SA15001/17001, SW15001 and SPB02/03;
- XMT180 mixer time;
- MTCM313 / TBA827 / TCA313, which are outsourced;
- bias cutter 2001 ×1.66;
- Conti vs BJ building machines.

**Not used:** the buffer master.

### 9e. Output levers tested (2026-09-30)
| Test | Built / cured | Decision |
|---|---|---|
| Base (all live inputs) | 97.4 / 93.4 | current config |
| Item 1: component demand timed on the day-pool predicted build start | 93.6 / 89.8 (kit-late 113 → 226) | **reverted.** Components lose their head start. |
| **Item 2: mixer MPQ 3 batches (DB) + no BRM-13 buffer batches** | **99.8 / 96.0** (PCR 100 / 96.6, TBR 99.0 / 93.8), kit-late 42 | **what-if only.** It overrides business rules BRM-01 and BRM-13, which needs plant approval. The config is saved as `config.yaml.item2_whatif`; the output is in `output/CTP_29SEP_05OCT_ITEM2`. |

## 10. 30-09 plan (v9) and the 5 Oct shortfall (2026-09-30)
The plan runs 29 Sep 07:00 – 6 Oct 07:00: 101,520 build, 106,256 cure.

**Two root causes in our flow for the 5 Oct gap, plus one real capacity limit:**
1. **Fixed day order on each TBM.** A kit-late first row held its slot, and kit-ready rows waited behind it. On 5 Oct, 15 TBMs were idle more than 4 h (158 TBM-h).
   - Fix: `building_kit_aware_dispatch`. Within each day, the next row placed is the one with the earliest feasible start (current material ledger plus TBM free time).
2. **Component LFT was 6–10 h later than the real need.** The CPM LFT is propagated from phase2's internal GT lots, not from the building plan (e.g. IL056 needed at 5 Oct 07:00 had LFT 16:50).
   - Fix: `lft_cap_by_need`, so LFT ≤ first_need − (min aging + transfer). This affected 79 lots, pulled in 11.6 h on average.
3. **Real limit: the mixers (21–23 h/day to 3 Oct) and 1802.** Excluding the mixers from the cap gives back exactly the pre-cap result. PCR compounds and B1144 (for BEF001 on 1802) compete for the same mixer time.

| Run (30-09 plan) | Built / cured | PCR | TBR | 5 Oct built |
|---|---|---|---|---|
| Base | 94.1 / 89.9 | 94.5 / 90.4 | 92.3 / 88.1 | 12,888 |
| + kit-aware dispatch | 94.9 / 90.4 | 95.1 / 90.6 | 94.3 / 90.0 | 13,316 |
| **+ LFT cap by need** (deliverable) | **95.3 / 90.8** | **96.5 / 91.5** | 90.4 / 87.8 | **13,904** |
| + LFT cap, skipping mixers and 1802 | 94.9 / 90.4 | 95.1 / 90.6 | 94.3 / 90.0 | 13,316 |

**Next lever:** mixer MPQ 3 batches with no BRM-13 buffer (item 2). On the 29-09 plan it gave +2.6 pts cure. It needs plant approval.

## 11. Business rules BR-1/2/4/5/7/10/12/66/67/74/77 enforced (2026-09-30, 30-09 plan)
**Config switches:**
- `building_shift_pool` (BR-1)
- `enforce_pm_downtime` (BR-10)
- `mixer_changeover_min: 1`, now actually read by phase5 (BR-74)
- `final_repair_pass` (BR-7/10/12-min/66/67)
- `final_repair_hard_max_aging: false`

**Final repair pass.** Runs last and only moves work later.
- Upstream lots are processed in time order. Each gets the matrix changeover (plus the CAP66, TR100 and purge rules) after the machine's previous lot, has its inputs ready including min aging, and skips PM windows.
- Build blocks are processed in time order. Each needs its TBM free and outside PM, and a FEFO kit.

**Hard max aging (expired material = scrap) was tested and rejected.** Nothing remakes the scrapped quantity, so 176 builds went short. Output fell to 65.6 / 63.1 and aging got worse. Max aging is instead held by the aging pull and the lot caps, and reported.

**Result:** 97.1 / 92.6 built / cured (PCR 98.1 / 93.4, TBR 93.1 / 89.2). Before the rules it was 95.3 / 90.8.

| Audit | Result |
|---|---|
| BR-7 | 0 of 1,666 changes shorter than the matrix (was 731) |
| BR-74 | 0 mixer gaps under 1 min |
| BR-10 | 0 PM overlaps |
| BR-2 | 06:30 snapshot |
| BR-66/67 | ledger proof 0 short |
| BR-12 | max aging on 1.3% of quantity; 223 green tyres over 72 h |

**Still violated outside the requested list:** BRM-03 (butyl follower), BRM-06 (TR100 sequence), BRM-09 / BRE-02 (same item split in a shift) and BRM-01 (2 lots under 5 batches).

**Output folder:** output/CTP_30SEP_BUSINESS_RULES_20260930

### 11b. Independent feasibility check (final_feasibility_check.py), 30-09 business-rules run
All passed:
- plan fidelity: 435/435 rows on the planned TBM, none before its shift; curing qty per SKU = plan;
- no double-booking on 35 upstream machines, 20 TBMs or 163 presses;
- routing 0 illegal; changeover matrix 0 short of 1,666 changes; mixer 1 min: 0 short; PM 0 overlaps;
- every press has its GT; cured ≤ built + GT stock for every GT.

Explained:
- **50 upstream edges start before their allocated input.** They are cap strip / edge gum chain items (V5-guaranteed, `kit_gate_assume_available`) or inputs covered by opening stock.
- **1 build block flagged.** A 0-qty rounding case on 6 Oct, after the plan end.
- **Partly:** max aging, 1.28% of component quantity; GT > 72 h, 223 tyres.

**Workbook:** CTP_29SEP-06OCT_SCHEDULE_CHECKED.xlsx, which adds a 'Feasibility checks' sheet.

### 11c. RCA of the 5 Oct curing gap and what fixes it (30-09 plan)
**Chain of cause:**
1. Day-5 compounds are all queued on saturated final mixers (F270 back to back, 3 Oct 14:22 → 4 Oct 12:00).
2. Tread compound B815 is made 20th of 21 (ready 4 Oct 14:14).
3. The single extruder 1002 (about 18 h for 19 treads) finishes the last treads 1–9 h after the 07:00 builds.
4. The TBMs, planned 80–89% full, never recover.
5. 2,095 cures are lost.

A simulation with material always on time builds 100% of the plan: presses, TBMs and the plan are feasible.

**Tested:**
| Test | Built / cured | 5 Oct cured |
|---|---|---|
| Base | 97.1 / 92.6 | 15,397 |
| `compress_upstream` | same | same (no idle mixer time to move into) |
| `capacity_aware_lft` | 95.7 / 91.2 | 14,796 (reprioritising saturated mixers only moves the shortage) |
| **What-if: mixer MPQ 3 batches + no BRM-13 buffer** | **99.9 / 95.6** (PCR 100 / 95.8, TBR 99.6 / 94.7) | **17,567 of 17,492** |

The what-if output is in output/CTP_30SEP_WHATIF_MIXER3_20260930. It needs a plant decision on BRM-01 / BRM-13 (+38% master compound).

**The config stays rule-compliant** (5 batches + 5 buffer).
