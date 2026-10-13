# Assumptions log (per run)

Every assumption a run takes, point by point, with its source and the reason. Newest run first. Add an entry for every new version.

- **Versions before v85:** see `BUSINESS_RULES_LOG.md` (WIP assumptions v79 / v80; evaluation v75_relaxed).
- **Current config = v87** (v85 config saved as `config_v85_before_v87.yaml`). v86 was an evaluation only: its code and config changes were removed after the run, and the files were checked byte for byte against the v85 backups.

---

## Standing data rules added after v86 (apply from the next run)

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| D-1 (2026-10-13) | A compound–mixer pair with **no batch weight in `CTP_Master_mixing`** (206 rows, all NULL, 128 compounds) takes the compound's **`batch_size` from `CTP_Master_Routing`** | `build_sep_inputs.py` (writes `ctp_inputfiles/mixing_master_live.csv`) | User decision. Routing has one size per compound (not per mixer), so a big mixer can get a small-mixer size: XMT507 on K430-2 190 kg, XMS106 on K270M 190 kg, MS65 295 kg. Changes 60 of 130 partly-filled pairs vs v85 (v85 used the largest weight of the compound's other mixers) |

---

## v103 — v98 + finals on their own final mixers first + cap strip / edge gum for the first plant day (2026-10-14)

Same assumptions as v98, except:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| **V103-1** | **Cap strip + edge gum (EG0x / EGSL0x / TEG-0x) present for build rows planned in the first 24 h** (v98: 8 h, V85-1) | `kit_gate_assume_first_h.hours: 24` | User 2026-10-14: assume cap strip / edge gum available for the first day. Edge strip / steel belt edge strip / BEF still checked against stock |

The mixer preference (`mix_planner.final_mixer_first`) is a scheduling choice, not a data assumption (see BUSINESS_RULES_LOG.md).

---

## v102 — v98 + 1802 output x2 + 901 lot split by consumer timing (2026-10-13)

Same assumptions as v98 (8 h cap strip / edge gum, as v85-v100), plus:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| **V102-1** | **1802 belt edge filler makes 2 strips at once**: routing 13 m/min BEF001 counted as 26 m/min of output (a 6 m requirement takes the time of 3 m). Metres made, BOM and consumers unchanged | `routing_output_multiplier: {"1802": 2.0}` (`apply_ctb_approximations.py`) | User 2026-10-13. **Plant to confirm** that 13 m/min is per strip (plant measured about 10 m/min per 150 m roll) |

---

## v101 — v98 + day-1 urgent 901 order + 24 h cap strip / edge gum assumption (2026-10-13)

Same assumptions as v98 (v97 + V97-1/2), except:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| **V101-1** | **Cap strip + edge gum (EG0x / EGSL0x / TEG-0x) present for build rows planned in the first 24 h** (was 8 h, V85-1) | `kit_gate_assume_first_h.hours: 24` | User 2026-10-13: protects day-1 PCR from a late first 901 dip. Edge strip / steel belt edge strip / BEF still checked against stock |

---

## v97 — v87 + bought-in 901 items / compounds (2026-10-13)

Same assumptions as v87, plus:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| **V97-1** | **CWCH30256 and CWBHI34 (901 steel calendered items) are procured from outside (4RCRMS)**: always available, never made on 901; their wire and skim compound demand is gone | config `outsourced_items` | User 2026-10-13 |
| **V97-2** | **TBA827, TCA313, MS43, MS70, MB100 are bought-in compounds**: always available, never mixed | Same | User 2026-10-13. MS43 / MS70 / MB100 are masters, so no change while masters are unlimited (V87-2) |

---

## v91 — v87 + plant cycle times for finals + T638 on K430-2 (2026-10-13), awaiting user decision

Same assumptions as v87. Added for this run:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| V91-1 | **Final-compound cycle time = plant-measured** (median batch-to-batch time, ≥ 20 intervals; 57 compounds; slower values taken too) | `CTP_Plant_Schedule` 04–11 Oct → `plant_mixing_cycles_04_10oct.csv` | User: take the plant-approved cycle time. The DB routing is ~7 % slower on average |
| V91-2 | **T638 may run on K430-2** at 335 kg / 205 s (×1.034 of its routing kg/h), with 120 s clean-out before and after | Plant bookings (222 batches in 7 days) | User: allow finals on master mixers where the plant does it. Only valid while masters are assumed available (V87-2) |

---

## v90 — v87 + CAP 66 first-day dip + B616M / inner-liner priority (2026-10-13), tested, NOT kept

Same assumptions as v87. No new data assumption: these are scheduling choices only. They are recorded in `BUSINESS_RULES_LOG.md` / `RUN_LOG.md`. Result 127,513 (−1,746), starvation 1, so the config is back to v87.

---

## v89 — v87 + inner-liner compound priority + 901 fabric-only pull (2026-10-13), tested, NOT kept

Same assumptions as v87. Added for this run only:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| V89-1 | Compounds of the inner liners (B355, B458, EFA, GB35, GB58, GL6, GL7) ranked 8 h ahead in the mixing planner | `mix_planner.item_priority_h` | 1101 was the root of 26 % of late PCR tyres in v87 |
| V89-2 | Only **fabric** 901 lots may be pulled into idle gaps, once after the last day-plan pass; one go a day relaxed for these urgent lots | `fourrc_rules.urgent_pull` (`fabric_only`, `after_day_plan`) | v88 lost day 1 by moving a steel lot. Result +914 (noise), 4RC-10 96 % -> not kept |

---

## v88 — v87 + 901 urgent pull (2026-10-13), tested, NOT kept

Same assumptions as v87 (V87-1 … V87-5). Added for this run only:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| V88-1 | A 901 lot that its consumer waits for (consumer start within 1 h of the lot being ready) may run in an idle 901 gap of 60 min or more, even if its item already ran that plant day (one go a day relaxed) | `fourrc_rules.urgent_pull` | User OK for urgent lots only. Result −411 (PCR 12 / 13-Oct lost). Switched off again; config = v87 |

---

## v87 — 12-Oct WIP with the 17:26 correction, masters unlimited, BOM / routing / allowable from JK_CTP_PPDS, no CTP_Master_mixing (2026-10-13), current config

**Run (offline: the DB server was unreachable at 11:20; the copies saved 13-Oct 01:04–01:11 were used):**
`$env:CTP_WIP_CORRECTION="2026-10-12 17:26"; python run_v15.py --wip-date 2026-10-12 --wip-time 07:00 --plan-end "2026-10-22 07:00" --plan-dir plan-12oct --name v87 --wip-source mix --offline --compare v85`

**Result:** cured **129,259 / 175,628 (73.6 %)**:
- PCR 106,764 (+14,393 vs v85);
- TBR 22,495 (+4,337).

**Other results:**
- 102 green tyres scrapped (v85: 175);
- material starvation 0;
- rule adherence 71 PASS / 8 PARTIAL / 5 FAIL (v85: 69 / 11 / 5).

Report: `v87/DATA_GAPS_DIPS_UTILISATION_v87.xlsx`.

**All v85 assumptions apply (V85-1 … V85-13), except where changed below:**

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| **V87-1** | **Opening WIP = 12-Oct 06:31 snapshot + the 17:26 correction.** 17:26 lots produced at or before 07:00 that 06:31 lacks are added (119 rows: 111 final-compound lots / 414 batches, plus 8 small component lots). Compound WIP 946,799 → 1,038,344 kg. Lots in both keep the 06:31 qty | `CTP_Opening_WIP`; env `CTP_WIP_CORRECTION` (`build_sep_inputs.py`); `CTP_WIP_CORRECTION_MODE=replace` would take the 17:26 qty | User: use the A-shift correction with the 06:30 data. The 17:26 qty of a lot already in 06:31 is lower because the lot was consumed during the day (343 lots, every component type), so it is not a correction of the 07:00 stock. Lots made after 07:00 are excluded: the scheduler plans that output itself (user choice) |
| **V87-2** | **Master compound stock unlimited.** All 218 master compounds treated like outsourced compounds: MASTER MIXING routing rows removed (23,359), so masters are never made and never short; master mixers get no work | config `masters_unlimited`, `apply_ctb_approximations.py` | User: assume master compound inventory as all available. The master WIP is known to be wrong (80 batches) and the master mixers were the binding constraint (97 % busy in v85) |
| **V87-3** | **Final compound stock from the WIP** (06:31 + V87-1), batches × routing batch weight; expired lots not usable (V85-4) | `CTP_Opening_WIP` | User |
| **V87-4** | **Mixing batch size and cycle from `CTP_Master_Routing`**, not `CTP_Master_mixing` | `business_rules.mixing_batch_source: ctp_inputfiles/routing_live.csv` | User: do not use the master_mixing table. Rule D-1 is therefore not used by this run |
| V87-5 | BOM, routing, aging, changeover, MPQ, FRC, building / curing allowable and cure cycle from `JK_CTP_PPDS` (as v85); plan machine kept and checked against the allowable masters | `master_files: {}` | User. Copies of 13-Oct 01:04–01:11 because the server was down |

---

## v86 — evaluation: v85 + plant flow from DB actuals + mixer preference + plant cycle times (2026-10-13)

**Run:** `run_v15.py --wip-date 2026-10-12 --wip-time 07:00 --plan-end "2026-10-22 07:00" --plan-dir plan-12oct --name v86 --wip-source mix --compare v85`

**Result:** cured **112,716 / 175,628 (64.2 %)** vs v85 110,529:
- PCR 91,797 (−574);
- TBR 20,919 (**+2,761**).

**All assumptions of v85 (below) apply, except V85-5, which is replaced by V86-1.** Added for this run only:

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| **V86-1** | **Plant flow from DB actuals.** Each item's average hourly output in the plant, × 8 h (A shift), is stock at the plan start. Covers components **and compounds**. Replaces the 06-Oct Excel flow (sheet 5, 24 h) | `JK_CTP_PPDS.CTP_Plant_Schedule`, 04-Oct 07:00 → 11-Oct 07:00 (7 days, 135,665 rows). Built by `v86_assumption_inputs/build_plant_flow.py`: 644 items, 404,275 units (finals 92.9 t, masters 172.8 t) | The DB has no forward plant schedule (`CTP_Plant_Schedule` is actual bookings up to 11-Oct 12:33). The plant's recent run rate is the closest DB evidence of what was in progress at 07:00. **Limitations:** all 8 h of output is available at once at 07:00 (optimistic). 477 bookings on 18 items couldn't be converted (item or input not in the BOM, e.g. SQ-00x on ebr, PBP57, 30GWA) |
| V86-1a | Output of one plant booking | Mixing: kg of the batch's ingredients. Other sections: median over its inputs of (consumed qty ÷ BOM qty per unit) | The plant file records only what was consumed. Check: mixing comes to ~797 t/day against ~774 t/day measured on 08-Oct |
| **V86-2** | **Batch weight per (compound, mixer).** On mixer m a lot takes (reference kg ÷ kg on m) × its duration. The reference is the compound's largest DB weight | `CTP_Master_mixing` (332 pairs) + **gap fill from the mixer preference matrix** `plant-schedule/mixer-pref/TBR and PCR compounds batch weight Oct26.xlsx` (69 pairs). 134 pairs have no weight anywhere and keep the lot duration. File: `v86_assumption_inputs/mixer_batch_weights.csv` | The plant mixes at the matrix weight of the mixer used (99 of 110 cases on 08-Oct). The DB master lacks 206 weights |
| **V86-3** | **Mixer preference** = order by kg per hour (batch weight ÷ cycle). On its k-th choice a compound is ranked (k−1) × **6 h** later, so it overflows to a second mixer only when that much more urgent | Same file (`pref_rank`). The 6 h penalty is an **assumption** | `CTP_Master_mixing.machine_pref` is 0 on all rows. The plant used its heaviest mixer for 56 % of batches and spread the rest, keeping every mixer loaded (`MIXER_PREFERENCE_ANALYSIS.md`) |
| **V86-4** | **Plant mixing cycle time where faster** than the DB routing, cycle only (batch weight from V86-2) | `plant-schedule/relaxed_mixing_overrides.csv`: 77 compounds measured on 08-Oct; **144 estimated** by the mixer's plant ratio (0.90–0.97); 40 = routing. Faster than the DB for 214 compounds | The plant ran ~8 % faster per batch on 08-Oct. **One day only; estimates for unmeasured compounds.** Needs plant confirmation |
| V86-5 | No other change | All v85 rules and assumptions; no DB table changed | User: "assumptions only, to see their effect" |

**Not taken (held for plant confirmation):**
- compound used past the aging master (A3);
- 4RC creel ~170 min (C2);
- 4RC fabric twice a day (C3);
- advisory kit gate (C6 / assumption.md #9).

**Not separable:** V86-1 to V86-4 ran together. Their individual effects are unknown without one run per assumption.

---

## v85 — all masters from JK_CTP_PPDS, plan-12oct, WIP 12-Oct shift A (2026-10-13), current config

| # | Assumption | Value / source | Why it was taken |
|---|---|---|---|
| V85-1 | Cap strip + edge gum (EG0x / EGSL0x / TEG-0x) present for build rows planned in the first **8 h** | `kit_gate_assume_first_h` | `plan-12oct/assumption.md` #1. These are not tracked reliably in the WIP |
| V85-2 | Edge strip, steel belt edge strip and BEF001 **not** assumed; checked against stock | Same | assumption.md "removed" list |
| V85-3 | Untracked building components count as **zero** (was ~200–406 assumed available) | `kit_gate_assume_first_h.codes_file` off | assumption.md "removed" list. The log still prints "#1 untracked codes … assumed available", but phase 5 does not use them |
| V85-4 | Stock past its shelf life is **not usable** (`expired_usable_h: 0`); 3,382 lots / 696,557 units | `ctb_wip_assumptions` | assumption.md: only cap strip and edge gum are assumed as stock |
| V85-5 | One day (24 h) of upstream flow as stock at t0, components only (257 items, 245,723 units) | `CTB_WIP_ASSUMPTIONS_v2.xlsx` sheet 5 (06-Oct SMART consumption) | assumption.md #8 CTB_FLOW on; `upstream_flow.csv` was not provided, so the existing sheet was used |
| V85-6 | SKUs without BOM / building routing are skipped (1225219015008SXC1E, 5 rows) | Phase 5 | assumption.md #2 (nothing to check) |
| V85-7 | Items without a make rate get the median of the same operation (gap fill); compounds without a batch size get a default (e.g. T153X 205 kg) | `apply_routing_gapfill.py`, BatchMap | assumption.md #3 / #5 (data gaps) |
| V85-8 | Outsourced compounds TBA827, TCA313, MTCM313 (no routing) are not made and not waited for | Routing | assumption.md #7 |
| V85-9 | Building **waits for every input** (hard kit gate), **not** assumption.md #9 "gate: False" | v77 decision | Keeps the business rule. The advisory gate is available as a what-if |
| V85-10 | Machine efficiency = 1 everywhere | `efficiency_override: 1.0` | User decision (since v37) |
| V85-11 | Plan machine / press per row kept; checked against the masters. Name mapping TBMPCR<n>Stage2 = 34<nn>, TBM-0<n> = TBR-0<n> | `check_db_masters.py` | User decision 13-Oct. The mapping is an assumption (names differ) |
| V85-12 | Compound WIP rows in batches × the routing batch weight (19 rows without a weight kept as counts) | `build_sep_inputs.py` | The WIP feed holds compound in batches |
| V85-13 | Strip line-speed multipliers (EG01 ×15, EG02 ×11, …) | Routing step BR-1 | Unchanged since earlier versions; unverified with the plant |
