# Schedule v98 -- business rules in force

**Run:** `v98: opening WIP 2026-10-12 07:00 | plan 2026-10-12 07:00:00 .. 2026-10-22 07:00:00 | plans C:\Users\shradha\Downloads\JKT-CTP-SCHEDULER-main\JKT-CTP-SCHEDULER-main\plan-12oct`

## New in this version

- No new rule (same rules, new data or settings).

## Switched off in this version (config)

- **BR-1** (General rules (BR-1..BR-17)): Building plan followed shift-wise: same TBM and qty, start in the plan shift (early at most one shift); starvation-risk SKUs (feed-hours <= 8 h) built first (`building_plan_feasible.pull_forward_h, building_priority`)
- **BR-3** (General rules (BR-1..BR-17)): Lowest building feed-hours first: time until the presses run out of the GT after GT stock decides the building order (`building_priority`)
- **BR-8** (General rules (BR-1..BR-17)): Family batching (same item, then BOM input family, then least changeover) per plant day on all matrix-mapped upstream machines, within coverage (`family_batching`)
- **BR-9** (General rules (BR-1..BR-17)): Same item not split into several blocks in one shift on any upstream machine (except 4RC), unless a consumer would starve (`business_rules.same_item_shift_merge.machines: [ALL]`)
- **BR-12** (General rules (BR-1..BR-17)): Aging hard block: upstream lots consume only input alive at their start (min..max aging, FEFO), else wait for fresh supply / flagged; building waits for an aging-valid kit; green tyres > 72 h scrapped (`aging_hard_block`)
- **BR-14** (General rules (BR-1..BR-17)): No item change in the first / last 30 min of a shift (07:00 / 15:00 / 23:00) on upstream machines (`shift_handover`)
- **BR-16** (General rules (BR-1..BR-17)): Similar urgency -> fewer changeovers, longer runs: family passes (no lot made late first), MPQ pooling, 4RC spool runs; reported (`family_batching / *_rules`)
- **BR-12 strict** (General rules (BR-1..BR-17)): Aging limits from JK_CTP_PPDS.CTP_Master_Aging (Hours/Days/Month=30 d/Min), green tyres 72 h (user); hard block on every consumption, no remake (`run_v15.py CTP_AGING_SOURCE=db, aging_hard_block`)
- **BR-37** (IL / calendering): IL made to net building need after opening IL stock; one lot covers <= 24 h of building need (no excess / early IL) (`mpq_pooling.max_span_h_itypes`)
- **BR-41** (IL / calendering): Per plant day: TBR RHC type blocks (TIL / SQ / EG) then compound groups; PCR RHC and 2201 compound groups together (`family_batching 1901 / 1101 / 2201`)
- **4RC-8** (4-roll calender (901)): Spool / dip roll (CTP_Master_FRC, per wire code) ENTIRELY consumed before the cord / dip change; last lot lengthened, extra to stock (`fourrc_rules.day_plan`)
- **CD-1** (Dispatch (all sections)): Cover first: cover_h = (stock + aged WIP) / consumption rate (moulds x cure rate per mould; upstream: the consumers' rate). A SKU / item under its threshold is urgent and runs next, lowest cover first. Threshold: building 8 h (one shift; 90th-pct restock 5.6 h PCR / 4.6 h TBR + margin); upstream item = matrix changeover + lot run time (routing) + min aging (CTP_Master_Aging) + transfer + 2 h (`cover_dispatch`)
- **CD-2** (Dispatch (all sections)): Equal cover: the SKU / item feeding FEWER moulds goes first (user, 2026-10-06) (`cover_dispatch`)
- **CD-3** (Dispatch (all sections)): No urgent item: same item continues, then the same compound family, then the cheapest changeover in CTP_Master_ChangeoverMatrix (least minutes), then the next cheapest; a run breaks only when another item becomes urgent, at a lot / MPQ / spool end; grouping may cross plant days within shelf life (`cover_dispatch`)
- **4RC level loading v43** (4-roll calender (901)): 901 output 30-35 km per plant day: a light day pulls the next 3 days' lots forward (inputs ready, within max aging - 4 h), never above 35 km, work only moves earlier; mixers feed 901 first (skim compounds + masters of every 901 lot due 24 h earlier) (`fourrc_rules.level_loading, protect_bottleneck_feed.machines`)
- **4RC deep roll v44** (4-roll calender (901)): Dipped-fabric items in FULL dips (CTP_Master_FRC Spool/Dip Length, 2,000 m for most): 48 h need >= one dip -> floor(need / dip) dips (4,400 m -> 2 dips); 48 h need < one dip -> extended to 72 h and decided again; never a partial dip, dip stock covers the next demand first (`fourrc_rules.deep_roll`)
- **4RC level plan v44** (4-roll calender (901)): Option C: band = average daily 901 need +- 5 km (made metres, whole spools / dips); 901 planned FIRST (made metres = whole Spool/Dip Length per FRC wire): peaks moved to earlier days, light days pull the next 3 days' lots, always within max aging - 4 h; each lot gets a target start and its feeding compounds (skim final, master, re-mill) are back-scheduled from it (start - min aging - transfer) so the mixers make them in time (`fourrc_rules.level_plan`)
- **MIX-1MIXER v47** (Mixing): One compound is planned on only ONE mixer per plant day, never split across mixers: each compound gets one routing-eligible mixer (butyl / butyl-friendly on their block mixer, the rest balanced by run time) (`mixing_v47.one_mixer_per_day`)
- **MIX-GAP v47** (Mixing): One run makes at once at least the next 15 h of demand (24 h for IL3 / IL5 and the 4RC compounds) and the same compound is not started again within 15 h / 24 h of its previous run's start; butyl-friendly B99 / B1 / B2 / MB100 / MB200 / XMB98 exempt (`mixing_v47.repeat_gap`)
- **MIX-CATCHUP v48** (Mixing): A late compound window that is already due joins the current run of its compound on the same mixer (one bigger run) instead of waiting the 15 h / 24 h gap -- inputs ready and within shelf life at its last use (`mixing_v47.catch_up_merge`)
- **4RC ready-first v48** (4-roll calender (901)): 901 re-planned after the repair WITHOUT the level band: whenever it is free it runs a READY lot (same item, same CTP_Master_FRC wire, fabric during a creel reload, earliest need); a lot whose compound is missing is skipped, not waited for; FOURRC_SEQUENCE_REPORT lists idle time and same-wire sequencing breaks with reasons (`fourrc_rules.replan_after_repair`)
- **QUEUE-UNBLOCK v49** (General rules): No lot waits behind a lot whose input is missing: on 901 and the belt cutters (802, 801) lots keep their planned order, but when the next lot's input is not ready within 30 min the machine runs the next READY lot (same item first; 901 creel 240 min kept and filled with fabric / same wire) (`queue_unblock`)
- **MIX-1MIXER-DAY v49** (Mixing): Rule A as stated: one compound on ONE mixer per PLANT DAY (07:00-07:00), chosen at placement by load -- the mixer may differ between days (v47 / v48 fixed one mixer for the whole plan); butyl-group compounds stay on their block mixer (`mixing_v47.one_mixer_mode`)
- **MIX-PLACE v53** (Mixing): 08-Oct mixing rules applied AT PLACEMENT: a compound lot joins its compound's run (directly behind it, same mixer) or starts >= 15 h / 24 h after any run of it on any mixer; one mixer per compound per plant day chosen at placement (`mixing_v47.placement_aware`)
- **MIX-FEED v47** (Mixing): Final mixers target ~230 batches per shift (report, no cap): master compounds made early enough that every final lot can start from its own shelf-life floor (last use - (max aging - 2 h)) (`mixing_v47.final_feed`)
- **4RC re-plan v45** (4-roll calender (901)): 901 re-planned after the final repair on the real input-ready times: whenever the calender is free it runs the best READY lot (same item, same FRC wire, fabric during a creel reload, earliest need) up to the level band per day -- no lot waits behind another lot's compound; deep-roll decisions made as one lot per dip (4RC-6) (`fourrc_rules.replan_after_repair`)
- **4RC deep roll v46** (4-roll calender (901)): Deep Roll quantity = the HIGHER of the 48 h requirement and the requirement inside the item's max-aging window (CTP_Master_Aging), in FULL dips only (floor, at least 1): 1.4 vs 2.1 -> 2 dips, 1.4 vs 1.8 -> 1 dip (`fourrc_rules.deep_roll.mode`)

## All rules in force, by section

### General rules (BR-1..BR-17)

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-2 | Shift-start opening stock (DB snapshot at plan start); lots sized on net demand (+MPQ / batch / buffer only) | v32 | `run_v15.py --wip-date / mpq_pooling` |
| BR-4 | Only approved (routing) machine / item combinations; hard block | v32 | `routing (check)` |
| BR-5 | Item-specific line speed from routing (mixing: routing min/batch, no calibration / cap) | v32 | `mixer_capacity_calibration.enabled: false` |
| BR-7 | Previous -> next item matrix changeover on every matrix-mapped machine; machines without a matrix line reported | v32 | `machine_to_changeover_line` |
| BR-10 | No production inside PM / MTC windows; available minutes exclude them (cutter shear change: no data) | v32 | `downtime_reason` |
| BR-15 | Replan each shift: run_shift_refresh.py with the shift-start WIP snapshot, live masters / downtime / latest plans | v33 | `run_shift_refresh.py` |
| BR-17 | Latest plant-approved masters: live JK_CTP_PPDS each run, current rows only, data stamp + overrides in the README | v33 | `build_sep_inputs.py (live)` |

### Inputs and plan

| Rule | What it does | Since | Config |
|---|---|---|---|
| GEN-02 | Opening WIP from the live DB snapshot of the plan start day (07:00); plant day 07:00 -> 07:00 | v15 | `run_v15.py --wip-date / --wip-time` |
| GEN-01 | Plan window cut from the plant plan files (building + curing, PCR + TBR) to --plan-end | v15 | `run_v15.py --plan-end` |
| V22 | Plan files from latest_plan_file; all JK_CTP_PPDS masters read LIVE each run (BOM, routing, changeover matrix, MPQ, FRC, WIP) | v22 | `--plan-dir latest_plan_file (no --offline)` |
| EFF-1 | Efficiency taken as 1.0 for every routing row (user): upstream / mixing run time = routing time, build time = routing cycle; was routing 0.96 (cap ply slitter 0.78); curing unchanged | v37 | `efficiency_override: 1.0` |

### Mixing

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-V1 | Mixing planned in whole batches; batch size = routing batch_size (kg) | v15 | `business_rules.mixing_output_uom` |
| BRM-01 | Mixing lot >= 3 batches (MPQ) x the compound's routing batch weight | v15 | `mixing_mpq_min_batches` |
| BRM-13 | +5 buffer batches on each compound's last lot (not special / sticky compounds) | v15 | `business_rules.buffer_batches` |
| BRM-10 / BR-74 | Compound changeover on a mixer = 60 s | v15 | `mixer_changeover_min` |
| BRM-06 | Master <-> final switch on one mixer: TR100 clean-out 5 min | v15 | `business_rules.master_final_cleanout_min` |
| BRM-02 | Sticky compounds (JT444, JT466) in stacks of 3 batches | v15 | `business_rules.sticky_compounds` |
| BR-V2 / BR-V13 | Butyl sequence: final mixer B1/B2/B99 before 30G/IL3/IL5 (else clean-out); master: no clean-out, 60 s changeover only | v15 | `business_rules.butyl_sequence_rule` |
| BRM-09 | Same compound once per (mixer, shift): split runs joined | v18 | `business_rules.same_item_shift_merge` |
| 3.1.3 / BRM-07 | IL3/IL5 in ONE campaign per plant day (07:00 -> 07:00) on F270, B99 directly in front | v20 | `il_daily_campaign.once_per_plant_day` |
| BUTYL-120 | Butyl clean-out fixed 120 s for all runs | v28 | `business_rules.butyl_sequence_rule.cleanout_sec` |
| MIX-CAP | NO mixer batch cap (user, from v32 and every run after): no 230/145 calibration, no 220 master / 160 final limit; mixer time = routing min/batch as-is | v28 | `mixer_capacity_calibration.enabled: false` |
| BR-V2 30G | 30G run placed with a real B99 lot (or a B99 prep lot) directly in front, like IL3/IL5 | v29 | `butyl_prep_campaign` |
| BRM-09 v38 | Same compound once per (mixer, shift) applied completely: after the strict rounds the remaining splits are joined with a consumer delay <= 8 h and the repair re-times the later lots | v38 | `same_item_shift_merge.post_repair_relaxed_h` |
| BUTYL-DAY v39 | ONE butyl block per plant day (07:00 -> 07:00) per group, butyl + butyl-friendly together: final (F270) B99 -> IL3 -> 120 s -> B99 -> IL5 -> 120 s -> B99 -> 30G -> 120 s; master (K430-2) friendly -> 29MG -> 120 s -> friendly -> ML405 -> 120 s -> friendly -> ML605 -> 120 s -> friendly; real friendly lots first, else 5-batch prep lots; IL3 / IL5 each in one go | v39 | `butyl_day_block` |
| IL-CAMPAIGN v41 | IL3 and IL5 made together in the final mixer in ONE go once a plant day: B99 (butyl-friendly) -> IL3 -> IL5 back to back (plain changeover, no clean-out between), 120 s clean-out after the campaign; 30G keeps its own B99 in front | v41 | `butyl_day_block.groups.final.campaigns` |
| MIX-PLANNER v56 | Stock-coverage mixing planner: all compound lots planned first on the mixers (forward, needed-soonest first, masters ahead of finals); runs cover 15 h / 24 h (IL / 4RC), separate lots back to back, no changeover, no repeat within that time, one mixer per plant day, daily butyl / IL blocks with friendly + clean-outs; the repair never moves mixing lots | v56 | `mix_planner` |

### 4-roll calender (901)

| Rule | What it does | Since | Config |
|---|---|---|---|
| 4RC-7 | Each item made in ONE GO per plant day (07:00 -> 07:00) | v34 | `fourrc_rules.day_plan` |
| 4RC-9 | Least changeover: same wire together, steel <-> fabric only between wire blocks, fabric fills the creel reload | v34 | `fourrc_rules.day_plan` |
| 4RC-2 | CAP 66 slitted material bypassed: mother roll -> B616M + fabric, no slitted lots | v21 | `fourrc_rules.bypass_cap66_slitted` |
| 4RC-3 | Capacity: steel and fabric minutes per plant day vs available, separately and combined (report) | v21 | `fourrc_rules.capacity_check` |
| 4RC-4 | Run time = metres / (routing proc_time x efficiency) for every 901 lot | v21 | `fourrc_rules.routing_speed_only` |
| 4RC-5 | One creel: new steel cord >= 240 min after the old cord, reload in parallel (fabric fills the window), not stacked on the changeover | v21 | `fourrc_rules.creel` |
| 4RC-6 | Roll lengths (CTP_Master_FRC): run <= one spool / dip length, made at once; min roll 300 m; no residual < 300 m; fewest runs | v27 | `fourrc_rules.roll_lengths` |
| 4RC-1..3 v38 | Applied completely: every plant day in one-go / wire-block / least-changeover order (fabric between steel cords); every run of a wire / dip code made in WHOLE spools of its Spool/Dip Length (CTP_Master_FRC by item_code), top-up spread over the run's lots, each <= one spool (4RC-6 kept), extra to stock; 1 m tolerance | v38 | `fourrc_rules.day_plan.mandatory` |
| 4RC rule 3 v40 | Least EFFECTIVE changeover: steel cord change = matrix minutes + idle 240 min creel reload, so steel -> fabric -> steel is preferred; after the last repair every steel cord change without fabric between gets that plant day's fabric run moved into the reload window (whole wire run, inputs ready, one run per item per day kept) | v40 | `fourrc_rules.fabric_bridge` |
| 4RC wire campaign v42 | CTP_Master_FRC wire_type = one creel load / dip roll: items sharing a wire run together in whole Spool/Dip Length, cheapest CTP_Master_ChangeoverMatrix order inside, a load may take the next 3 days' lots of its wire within max aging; creel reload (240 min) only when steel items change wire_type; changeover type / minutes from the matrix; reload windows filled with fabric (next day's dip within shelf life) | v42 | `fourrc_rules.creel_source / wire_campaign` |

### Extrusion - Quintoplex PCR tread (1002)

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-18 | Quintoplex only for routing-approved tread / sidewall codes | v24 | `routing (check)` |
| BR-19 | Tread-code-specific line speed (routing M/MIN x efficiency) | v24 | `routing (check)` |
| BR-20 | Changeover = matrix class A-H time (4.54 - 60 min) from item -> to item | v24 | `changeover matrix 'Quintoplex PCR Tread'` |
| BR-21 | Per plant day: same die and compatible compound family together, least changeover, within coverage (24 h window from v25) | v24 | `quintoplex_rules` |

### Extrusion - Duplex TBR tread (1801)

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-22 | Duplex only for routing-approved TBR treads | v25 | `routing (check)` |
| BR-23 | Tread-code-specific line speed (routing M/MIN x efficiency) | v25 | `routing (check)` |
| BR-24 | Matrix: die 4.76, die+preformer 4.92, compound 12.5 / 14 min; pair missing in the matrix = 14 min | v25 | `changeover_missing_pair_min` |
| BR-25 | Per plant day: one BOM compound family (T636 / T639) at a stretch, then least changeover | v25 | `duplex_rules` |

### Extrusion - Triplex PCR sidewall (1001)

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-26 | Triplex only for routing-approved sidewall codes | v30 | `routing (check)` |
| BR-27 | Sidewall-code-specific line speed (routing M/MIN x efficiency) | v30 | `routing (check)` |
| BR-28 | Matrix: die+preformer 3 min, compound/barrel/flow channel 28 min (missing pair = 28); per plant day each compound family (full BOM compound set) at a stretch -> fewest compound changes | v30 | `triplex_rules` |

### Extrusion - Quadraplex TBR sidewall / shoulder pad (1803)

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-29 | Quadraplex only for routing-approved TBR sidewall / shoulder-pad items | v31 | `routing (check)` |
| BR-30 | Item-specific Quadraplex line speed (routing M/MIN x efficiency) | v31 | `routing (check)` |
| BR-31 | Matrix: die 2, die+preformer 3, sidewall->shoulder pad 36, shoulder pad->sidewall 34, shoulder-pad compound 15 min; missing pair 36 (SW<->SP) / 15 | v31 | `changeover_missing_pair_by_type` |
| BR-32 | Per plant day all sidewalls in one block and all shoulder pads in one block: the 34-36 min SW<->SP changeover at most once a day; day starts with the group already running | v31 | `quadraplex_rules` |

### IL / calendering

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-38 | IL codes only on their routed machine (1101 / 1901 / 2201) | v34 | `routing (check)` |
| BR-39 | Code-specific IL line speed from routing; Expansion Work Out conversion: no data (BOM used) | v34 | `routing (check)` |

### Upstream (all machines)

| Rule | What it does | Since | Config |
|---|---|---|---|
| BR-7/10 | Final repair: every upstream lot after its inputs + min aging + transfer time, changeovers kept, PM/MTC windows blocked | v15 | `final_repair_pass` |
| BOTTLENECK | Saturated machines (mixers, 1802, bead apex, 1803) start when inputs are ready, limited only by shelf life | v25 | `bottleneck_release` |

### MPQ (minimum production quantity)

| Rule | What it does | Since | Config |
|---|---|---|---|
| MPQ-DB | Every CTP_Master_MPQ row (PCR + TBR) mapped to its items: tread by rim size, calendar roll spool / dipped cord, chaffer, STC, BRTTL, white SW, edge strip / gum (gauge not in DB -> smallest) | v26 | `mpq_item_level` |
| MPQ-POOL | Small same-item demand pooled up to the MPQ within shelf life before the floor (no per-lot over-production) | v26 | `mpq_pooling` |

### Building (TBM)

| Rule | What it does | Since | Config |
|---|---|---|---|
| BLD-ECHO | Building follows the plant building plan: same TBM, same quantity, later only for kit readiness | v15 | `building_plan_echo` |
| B1 | A SKU builds only on TBMs its GT is routed to | v23 | `routing (check)` |
| B2 | Build time = qty x routing cycle / efficiency; every GT change charged the matrix different-size time (TBR 24, PCR 60) | v23 | `capacity_rules_v23.building` |
| BR-V7 | Idle TBM may start its own ready plan row up to 24 h early | v25 | `building_plan_feasible` |
| C4 | No build block starts before its material physically exists (material ledger) | v15 | `echo_kit_ledger` |

### Curing (presses)

| Rule | What it does | Since | Config |
|---|---|---|---|
| CUR-ECHO | Curing follows the plant curing plan (pinned press echo) | v15 | `phase6` |
| C1 | A SKU cures only on its routing presses | v23 | `routing (check)` |
| C2 | q_shift = tyres a press cures in a shift = floor(2 x 480 / (routing cure + allowance)); block <= q_shift, press time = qty x 480 / q_shift | v23 | `capacity_rules_v23.curing` |

### Green tyres and aging

| Rule | What it does | Since | Config |
|---|---|---|---|
| AGE | Component aging: min / max aging from the aging master, transfer time from routing added to min aging | v15 | `aging master` |
| AGE-DB | Aging min / max from JK_CTP_PPDS.CTP_Master_Aging (Hours / Days / Month = 720 h / Min), green tyres 72 h (user); applied as in v26: inputs + min aging + transfer before use, max aging on build kits, over-age upstream use reported (no BR-12 hard block) | v36 | `run_v15.py CTP_AGING_SOURCE=db` |
| GT-72 | Green tyre shelf life 72 h; tyres past it are scrapped, never cured over age | v19 | `green_tyre_scrap_expired` |

## Master data used (BR-17)

| Table | Rows | Latest change in the table |
|---|--:|---|
| CTP_Master_Aging | 4,951 | - |
| CTP_Master_BOM | 133,179 | - |
| CTP_Master_BuildingAllowable | 2,450 | 2026-10-12 15:18:06.698000 |
| CTP_Master_ChangeoverMatrix | 293,166 | 2026-09-26 19:29:23 |
| CTP_Master_FRC | 48 | 2026-10-06 11:30:58 |
| CTP_Master_MPQ | 36 | - |
| CTP_Master_Routing | 48,164 | - |
| CTP_Master_mixing | 557 | 2026-09-23 17:03:03.329000 |
| CTP_Opening_WIP | 757,838 | 2026-10-13 00:00:00 |

**Values not taken from a plant master (overrides):**

| Setting | Value | Approved by |
|---|---|---|
| Green tyre shelf life | 72 h | user |
| Mixer capacity calibration | off (routing) | user |
| Butyl clean-out | 120 s | user |
| Missing matrix pairs | {'Duplex TBR Tread': 14, 'Triplex PCR SW': 28} / {'Quadraplex TBR SW': {'same_type': 15, 'other_type': 36}} | user |
| Edge strip / gum gauge | smallest DB value (gauge not in DB) | pending plant |
| Building changeover | different-size time on every GT change (v23) | user; BJ/CONTI pending |

## Files in this folder

- `BUILD_CURE_VALIDATION_v98.xlsx` -- Building / curing eligibility and capacity, TBM use by day
- `CTP_12OCT-22OCT_SCHEDULE_v98.xlsx` -- schedule
- `DUPLEX_RULES_v98.xlsx` -- BR-22..25
- `FOURRC_RULES_v98.xlsx` -- 4-roll calender rules 2-6
- `FOURRC_SEQUENCE_REPORT_v98.xlsx`
- `GENERAL_RULES_v98.xlsx` -- BR-1..BR-10
- `MIXING_RULE_AUDIT_v98.xlsx`
- `MIXING_RULE_COMPLIANCE_v98.xlsx` -- Mixing rules (incl. BR-V2 prep in front)
- `MIXING_RULE_EVIDENCE_v98.xlsx`
- `MPQ_COMPLIANCE_v98.xlsx` -- Lots vs CTP_Master_MPQ, over-production
- `QUADRAPLEX_RULES_v98.xlsx` -- BR-29..32
- `QUINTOPLEX_RULES_v98.xlsx` -- BR-18..21
- `RULE_ADHERENCE_REPORT_v98.xlsx`
- `TRIPLEX_RULES_v98.xlsx` -- BR-26..28

Full history and results: `latest_schedule/BUSINESS_RULES_LOG.md`.
