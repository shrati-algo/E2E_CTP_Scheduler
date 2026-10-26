# Repository verification

## v46e reproduced (27 Oct 2026)

The repository was brought up to **v46e** and the run was repeated **from this repository**. It was then compared file by file with the original run `pns-ctp-scheduler/outputs/e2e_v46e`.

### How

```bash
# data: the 22 Oct snapshot + BOM v2 + reference_bom/ + CTP_Master_FRC
cp -r <pns-ctp-scheduler>/data-ppds-v38 data/ppds_2026-10-22_bomv2
cd engine && python refresh_ctp_sql.py --ppds-only --bom-v2 --out ../data/ppds_2026-10-22_bomv2     --only "jkt_bom_pcr,jkt_bom_tbr,reference_bom/jkt_bom_pcr,reference_bom/jkt_bom_tbr,CTP_Master_FRC"
cd ..
python run_pipeline.py --plan-folder plans/october_2026 --data data/ppds_2026-10-22_bomv2 --run v46e_repo
python tools/compare_runs.py <pns-ctp-scheduler>/outputs/e2e_v46e outputs/v46e_repo --report outputs/v46e_compare.csv
```

### Data check before the run
- The **engine BOM** (from `CTP_Master_BOM_v2`) and **`CTP_Master_FRC`**, staged fresh by the new `--bom-v2` option, are **identical** to the files the v46e run used.
- All 24 other master files are byte-identical.
- **`reference_bom/` was not.** The live `CTP_Master_BOM` has changed since the 23 Oct staging: TBR 82,016 rows against 80,819, and 17 PCR cells differ. To reproduce v46e exactly, `reference_bom/` holds the 23 Oct copy, and `_sql_manifest.json` says so.
  - This folder is only used for the types of the bypassed cut codes and the roll-per-metre factor of cut stock.
  - A fresh staging for a new plan should take the live table.

### Result
- **108 of 110 items identical**: every CSV output for PCR and TBR, the floor schedules, fulfilment, blockers, validation, and the sheets of `VALIDATION_REPORT.xlsx`.
- **2 run-identity differences:**
  - the `Run stamp` sheet;
  - one sentence in `BUSINESS_RULES_COMPLIANCE_v15.csv` (BR-17) that quotes the data folder's name (`data-ppds-v38-bomv2` vs `ppds_2026-10-22_bomv2`).
- **Headline, both runs:**
  - PCR built 125,002 / cured **115,384**;
  - TBR built 26,645 / cured **24,034**;
  - **139,418 of 161,670 eligible tyres = 86.2%**;
  - all schedule checks pass.
- **Routing validation: 12 of 14.** The two machine-coverage findings are known: the bypassed cutters and slitters are idle by design. With BOM v2, the TBR nylon-doubling machines 2501 / 2502 are idle too, because v2 drops NYDBCHF; see the CHANGE_LOG v46c entry.
- `python -m unittest discover tests`: 5 tests pass, including a v46d check that a roll reached through two belts is counted once.

### What moved into the repository for v46e

| file | change |
|---|---|
| `engine/ctpio.py` | TDA in-line speed (`_tda_inline`); bypass parents from the routing; cut-code types from a reference BOM (this line only); BOM walk counts each recipe once (`WalkCache.once_made/once_contracted`); `resolve_ref_dir` (looks inside the data folder first) |
| `engine/phase2_wip_netting.py` | cut / slit stock → mother roll (`bypass_stock_to_children`), with conversion factors from `reference_bom/`; transfer-time table |
| `engine/phase3_lot_sizing.py` | 901 lots in whole dips (`_dip_length`, `_dip_lots`); TDA speed table |
| `engine/make_bypass_report.py` (new) | `BYPASS_REPORT_<run>.xlsx`, pipeline step 10 |
| `engine/refresh_ctp_sql.py` | `--bom-v2`: BOM v2 verbatim, pre-v2 BOM into `reference_bom/`, `CTP_Master_FRC` |
| `run_pipeline.py` | v46e; stops early if the data folder lacks `CTP_Master_FRC.csv` or `reference_bom/`; step 10 |
| `config/rules.yaml` | the v46e chain resolved into one file; `reference_bom` paths point inside the data folder |
| `config/history/` | + `rules_v45b`, `rules_v46a` … `rules_v46e` |
| `docs/v38_corrections/` | CHANGE_LOG (to v46e), AUDIT_ISSUES_STATUS_v46e, WIP notes |
| `tests/test_repo_basics.py` | v46 rule keys; walk-once unit test |

Unchanged on purpose: `engine/ctpsql.py` has no credential defaults (they come only from `CTP_DB_*` environment variables), and `engine/br_compliance_v15.py` uses the repo's `reference/` path.

---

## Earlier: v45b reproduced (26 Oct 2026)

When this repository was created (26 Oct 2026), the v45b run was re-run **from this repository** and compared with the original run `pns-ctp-scheduler/outputs/e2e_v45b`.

## How

```bash
python run_pipeline.py --plan-folder plans/october_2026 --data <pns-ctp-scheduler>/data-ppds-v38 --run v45b_repo
python tools/compare_runs.py <pns-ctp-scheduler>/outputs/e2e_v45b outputs/v45b_repo --report outputs/v45b_compare.csv
```

Same plan files (`plan-latest` = `plans/october_2026`), same data snapshot (`data-ppds-v38`), same rules: `config/rules.yaml` is the resolved v45b chain, checked key by key against `config/rules_v45b.yaml` + its 49-file `inherit_from` chain (identical apart from `meta`).

## Result

* **102 of 103 items identical**: 92 CSV files (every phase output for PCR and TBR, floor schedules, fulfilment, blockers, compliance, validation) and 11 sheets of `VALIDATION_REPORT.xlsx`. 'Identical' = same columns, same rows, same values (numbers to 1e-6).
* **1 expected difference**: the `Run stamp` sheet (run name, generation time, plan folder and rules-file path).
* Headline numbers, both runs: PCR built 113,991 / cured 103,234; TBR built 24,237 / cured 22,232; **125,466 of 161,670 eligible tyres cured (77.6%)**; routing validation 12 of 14 (the two known machine-coverage findings); all schedule checks G1, G1b, G1u, G2–G8, G12 pass.

## Changes made while moving the code (none affect the schedule)

| file | change |
|---|---|
| `run_pipeline.py` (new) | replaces `run.py`: takes `--plan-folder` and finds the four plan files by name; writes everything under `--out` (staged plans in `<out>/_plans/<run>/`); same steps, same order, same environment variables |
| `engine/br_compliance_v15.py` | business-rules workbook path -> `reference/master-business-rules/` |
| `engine/ctpsql.py` | database host / user / password / database-name defaults removed; they must come from `CTP_DB_*` environment variables |
| `config/rules.yaml` | the v45b rule chain resolved into one file (`config/history/` keeps the chain) |
| `tools/compare_runs.py`, `tests/test_repo_basics.py` (new) | comparison tool and fast checks |

The resolved rules still contain `debug.log_machines: [2403, 2404, 2405]` (inherited from v44c), so each run also writes `DISPATCH_LOG_debug.csv`. It is diagnostic only and can be removed from `config/rules.yaml` without changing the schedule.

## Item list

| item | verdict |
|---|---|
| `ASSUMED_PURCHASED_ITEMS.csv` | IDENTICAL |
| `ASSUMPTIONS.csv` | IDENTICAL |
| `BUSINESS_RULES_COMPLIANCE_v15.csv` | IDENTICAL |
| `BUSINESS_RULES_MISSED_v15.csv` | IDENTICAL |
| `CHANGEOVER_SOURCE.csv` | IDENTICAL |
| `CHANGEOVER_UNPRICED.csv` | IDENTICAL |
| `DATA_BLOCKERS.csv` | IDENTICAL |
| `DISPATCH_LOG_debug.csv` | IDENTICAL |
| `FULFILMENT_DAYWISE_v15.csv` | IDENTICAL |
| `FULFILMENT_SHORTFALL_v15.csv` | IDENTICAL |
| `MACHINE_COVERAGE.csv` | IDENTICAL |
| `MACHINE_UTILISATION_JOINT.csv` | IDENTICAL |
| `MIXING_RULE_COMPLIANCE.csv` | IDENTICAL |
| `SHELF_LIFE_STUCK.csv` | IDENTICAL |
| `SHELF_LIFE_VIOLATIONS_pcr.csv` | IDENTICAL |
| `SHELF_LIFE_VIOLATIONS_tbr.csv` | IDENTICAL |
| `VALIDATION.csv` | IDENTICAL |
| `VALIDATION_REPORT_blocked.csv` | IDENTICAL |
| `VALIDATION_SUMMARY.csv` | IDENTICAL |
| `WORK_STOLEN.csv` | IDENTICAL |
| `pcr/DATA_BLOCKERS.csv` | IDENTICAL |
| `pcr/LOT_SPLITS_v16.csv` | IDENTICAL |
| `pcr/MPQ_RESOLUTION.csv` | IDENTICAL |
| `pcr/UOM_CONVERSIONS.csv` | IDENTICAL |
| `pcr/campaign_plan.csv` | IDENTICAL |
| `pcr/floor_schedule.csv` | IDENTICAL |
| `pcr/phase0_assumptions.csv` | IDENTICAL |
| `pcr/phase0_effective_plan.csv` | IDENTICAL |
| `pcr/phase0_findings.csv` | IDENTICAL |
| `pcr/phase0_plan_effective.csv` | IDENTICAL |
| `pcr/phase0_plan_full.csv` | IDENTICAL |
| `pcr/phase0_sku_status.csv` | IDENTICAL |
| `pcr/phase1_building_lots.csv` | IDENTICAL |
| `pcr/phase1_demand.csv` | IDENTICAL |
| `pcr/phase2_bypass_wip.csv` | IDENTICAL |
| `pcr/phase2_demand_net.csv` | IDENTICAL |
| `pcr/phase2_wip_allocations.csv` | IDENTICAL |
| `pcr/phase3_assumptions.csv` | IDENTICAL |
| `pcr/phase3_lot_demand.csv` | IDENTICAL |
| `pcr/phase3_lots.csv` | IDENTICAL |
| `pcr/phase4_edges.csv` | IDENTICAL |
| `pcr/phase4_infeasible.csv` | IDENTICAL |
| `pcr/phase4_lot_times.csv` | IDENTICAL |
| `pcr/phase4b_assignment.csv` | IDENTICAL |
| `pcr/phase5_assumptions.csv` | IDENTICAL |
| `pcr/phase5_building_plan.csv` | IDENTICAL |
| `pcr/phase5_machine_util.csv` | IDENTICAL |
| `pcr/phase5_revisions.csv` | IDENTICAL |
| `pcr/phase5_schedule.csv` | IDENTICAL |
| `pcr/phase6_by_department.csv` | IDENTICAL |
| `pcr/phase6_daywise_gt.csv` | IDENTICAL |
| `pcr/phase7_assumptions.csv` | IDENTICAL |
| `pcr/phase7_curing_plan.csv` | IDENTICAL |
| `pcr/phase7_daywise_cured.csv` | IDENTICAL |
| `pcr/phase7_revisions.csv` | IDENTICAL |
| `phase5_floor_schedule_pcr.csv` | IDENTICAL |
| `phase5_floor_schedule_tbr.csv` | IDENTICAL |
| `tbr/DATA_BLOCKERS.csv` | IDENTICAL |
| `tbr/LOT_SPLITS_v16.csv` | IDENTICAL |
| `tbr/MPQ_RESOLUTION.csv` | IDENTICAL |
| `tbr/UOM_CONVERSIONS.csv` | IDENTICAL |
| `tbr/campaign_plan.csv` | IDENTICAL |
| `tbr/floor_schedule.csv` | IDENTICAL |
| `tbr/phase0_assumptions.csv` | IDENTICAL |
| `tbr/phase0_effective_plan.csv` | IDENTICAL |
| `tbr/phase0_findings.csv` | IDENTICAL |
| `tbr/phase0_plan_effective.csv` | IDENTICAL |
| `tbr/phase0_plan_full.csv` | IDENTICAL |
| `tbr/phase0_sku_status.csv` | IDENTICAL |
| `tbr/phase1_building_lots.csv` | IDENTICAL |
| `tbr/phase1_demand.csv` | IDENTICAL |
| `tbr/phase2_bypass_wip.csv` | IDENTICAL |
| `tbr/phase2_demand_net.csv` | IDENTICAL |
| `tbr/phase2_wip_allocations.csv` | IDENTICAL |
| `tbr/phase3_assumptions.csv` | IDENTICAL |
| `tbr/phase3_lot_demand.csv` | IDENTICAL |
| `tbr/phase3_lots.csv` | IDENTICAL |
| `tbr/phase4_edges.csv` | IDENTICAL |
| `tbr/phase4_infeasible.csv` | IDENTICAL |
| `tbr/phase4_lot_times.csv` | IDENTICAL |
| `tbr/phase4b_assignment.csv` | IDENTICAL |
| `tbr/phase5_assumptions.csv` | IDENTICAL |
| `tbr/phase5_building_plan.csv` | IDENTICAL |
| `tbr/phase5_machine_util.csv` | IDENTICAL |
| `tbr/phase5_revisions.csv` | IDENTICAL |
| `tbr/phase5_schedule.csv` | IDENTICAL |
| `tbr/phase6_by_department.csv` | IDENTICAL |
| `tbr/phase6_daywise_gt.csv` | IDENTICAL |
| `tbr/phase7_assumptions.csv` | IDENTICAL |
| `tbr/phase7_curing_plan.csv` | IDENTICAL |
| `tbr/phase7_daywise_cured.csv` | IDENTICAL |
| `tbr/phase7_revisions.csv` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Summary` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Blocked SKUs` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Blocking records` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Assumptions` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Assumption catalogue` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Blocker catalogue` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Assumed tyres` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Data gaps (handled)` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Data gap detail` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Schedule checks` | IDENTICAL |
| `VALIDATION_REPORT.xlsx:Run stamp` | EXPECTED DIFFERENCE |
