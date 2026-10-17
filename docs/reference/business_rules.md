> **Carried over from the main project** (written 12-Oct for v82, the engine design is unchanged in v133). Paths refer to the engine, which here lives in `engine/`. For this version's settings see [../NO_CUTTER_SLITTER.md](../NO_CUTTER_SLITTER.md).

# Business rules: where and how they are enforced (v82)

Back to [CLAUDE.md](../CLAUDE.md). Full rule history: `latest_schedule/BUSINESS_RULES_LOG.md`; rules in force per version: `latest_schedule/vNN/README_vNN.md`.

**Type** = how the code treats the rule:

| Type | Meaning |
|---|---|
| **HARD** | Placement never breaks it; a check reports it at 100 % |
| **SOFT** | A pass tries; compliance < 100 % allowed |
| **HEUR** | Ordering heuristic |
| **REPORT** | Measured only |
| **OFF** | Switched off |

The v82 compliance figures below are from the log or reports.

## Mixing (rules of 01-Oct and 08-Oct)

| Rule | Implementation | Type | v82 |
|---|---|---|---|
| Output in whole batches (BR-V1) | phase2 round-up; `mixing_batches.py` | HARD | 100 % |
| +5 buffer batches per compound (BRM-13) | phase2, last lot of each compound; special compounds excluded | HARD | applied |
| Sticky JT444 / JT466 in 3-batch stacks (BRM-02) | phase2 | HARD | applied |
| One compound on one mixer per plant day | `mix_planner` | HARD | 100 % |
| Not repeated within 15 h (24 h IL3 / IL5 / B1144 / 4RC compounds); friendly exempt | `mix_planner` (`cover_h`, `gap_h`, `_long`) | HARD | 100 % |
| Butyl-friendly before the butyl block (B1 / B2 / B99 → 30G / IL3 / IL5), block level | `butyl_day_block` + planner | HARD | 100 % |
| 120 s clean-out after butyl (`butyl_cleanout_to: any`) | planner | HARD | 100 % |
| Master butyl sequence friendly → 29MG → ML405 → ML605 → friendly, evening (`earliest_hour: 17`) | `butyl_day_block` | HARD | 100 % |
| IL3 + IL5 in one go a day | `il_daily_campaign` | HARD | 100 % |
| TR100 master ↔ final 5 min | planner (`tr100_s`) | HARD | — |
| **430-2 master mixer – final compound** (e.g. T638): 120 s clean-out before AND after a final on K430-2 | planner `mix_planner.co` (any final on K430-2); audit `mixing_rule_audit.py` M4 | HARD | not applicable in v82–v87 (no final on K430-2); v91 / v92 100 % (1 run, 14 rows) |
| Finals allowed on master mixers (`final_on_master_mixers`; v91 / v92: T638 → K430-2; **standing from 13-Oct: every final on K270M / K430-1 / K430-2 / F440**, user) | `apply_ctb_approximations.py` adds the mixers to the routing | SWITCH (on) | — |
| **120 s clean-out after every final compound**, any mixer (user 13-Oct) | planner `mix_planner.co` (`final_cleanout_after_s`); audit `mixing_rule_audit.py` M9 | HARD | from v93 |
| Same compound one run per shift (BRM-09) | planner + repair | SOFT | 5 not followed |
| Final mixers 230 batches / shift | report (`final_target_batches_per_shift`) | REPORT (**no cap**, user) | ~82 % of target |

## 4-roll calender 901

| Rule | Implementation | Type | v82 |
|---|---|---|---|
| 4RC-2 CAP 66 from fabric + B616M (no slitted lots) | `bypass_cap66_slitted` | HARD | 100 % |
| 4RC-3 capacity per day | check | REPORT / HARD | 100 % |
| 4RC-4 run time = metres / routing speed | `routing_speed_only` | HARD | 100 % |
| 4RC-5 one creel, 240 min between steel cords | `fourrc_rules.creel` | HARD | 100 % |
| 4RC-6 roll ≤ spool / dip | `roll_lengths` | HARD | 100 % |
| 4RC-7 item in one go per plant day | `day_plan` | HARD | 100 % |
| 4RC-8 spool consumed | `whole_spool_topup: false` (user v76) | OFF / REPORT | 3 % |
| 4RC-9 rule 3 fabric between cord changes | rule-3 pass, creel fill | SOFT | 84.6 % |
| Wire campaigns (same FRC wire together) | `wire_campaign` | HARD | 100 % |
| 4RC-10 no idle inside a load | `load_compact` | SOFT | 100 % |

**The plant itself breaks 4RC-4, 4RC-5, 4RC-7, wire-once, 4RC-2 and max aging** in its schedule. See `plant-schedule/PLANT_vs_v82_4RC_PROCESS.md`.

## Extrusion, calender, cutters, bead

| Rule | Machines | Type | v82 |
|---|---|---|---|
| Matrix changeover respected (BR-7 / 20 / 24 / 28 / 31) | all | HARD | 100 % |
| BR-21 family sequencing | 1002 | SOFT | 54.8 % compatible-next; day rule 25 % (FAIL) |
| BR-25 | 1801 | SOFT | 84.6 % day rule |
| BR-28 | 1001 | SOFT | 82.5 % / day rule 27 % (FAIL) |
| BR-32 | 1803 | SOFT | 89.3 % / day rule 61.5 % (FAIL) |
| BR-41 compound groups | 1101 / 2201 / 1901 | SOFT | not scored in v82 |
| BR-50 / 52 belt kit together per day | 801 / 802 | SOFT | — |
| BR-56 same item once per shift | cutters / slitters | SOFT | ~119 splits left |
| BR-61 PCR bead by inch | 1201 / 201 / 202 | SOFT | — |
| TBR bead wound + apexed in one step (v74) | 2403–2405 | data approximation | — |
| TBR bead inch per machine BR-63 / 64 / 65 | — | **NOT IMPL** (needs data) | — |

## Aging

| Rule | Implementation | Type |
|---|---|---|
| Min aging before use | gate lag | HARD (gate) |
| Max aging (shelf life) at use | shelf-life floor at placement, aging pull, re-cut | SOFT in practice: ~600 lots still over after the pull (log) |
| BR-12 edges not consumed before min / after max | `general_rules_checks.py` | REPORT: 97 % / 535 expired edges |
| Green tyre ≥ 2 h (BRBC-21), ≤ 72 h (scrap) | phase 6 | HARD (scrap counted) |
| Genealogy shelf life | `strict_links.py` | REPORT-only (user) |

## Building and curing (Algo Planning rules BRBC, `Algo Planning Business Rules 1.xlsx`)

| Rule | Implementation | Type / v82 |
|---|---|---|
| Kit complete before a build starts | building echo ledger | HARD, but "24 blocks short" logged |
| Allowed TBM only (BRBC-01) | plan + routing | Plan has 0.5 % rows on non-allowed TBMs |
| Allowed presses / mould count / tube type (BRBC-16 / 37 / 17) | plan echo | as plan |
| Mould change ≤ 4 (PCR) / 3 (TBR) a day (BRBC-19) | — | REPORT: PCR 85.7 %, TBR 64.7 % (the plan itself 30 % TBR) |
| Mould-change time kept (BRBC-27) | `curing_keep_changeover` | HARD 100 % |
| Green-tyre WIP band (BRBC-30) | — | REPORT: ~0 % in band (too low) |
| Rim move ≤ 1 inch (BRBC-34) | — | REPORT (the plan is 9.5 %) |
| Curing capacity (BRBC-36) | phase 6 | HARD |

Check script: `latest_schedule/plant-schedule/check_algo_planning_rules.py <ver> <plan_dir>`.

## Data assumptions in force (not plant-confirmed)

- Efficiency 1 everywhere.
- 1802 at 13 m/min.
- Strip line-speed multipliers BR-1 (EG01 ×15, EG02 ×11, EG03 ×7, BPG01 ×7, TEG-02 ×9, TB WRAP B1144 ×11).
- CWBP65 BOM approximated at 1.2744 m.
- WIP assumptions v80.
- First-24 h strip items.
- `CWBP60` class = steel (`class_override`).
