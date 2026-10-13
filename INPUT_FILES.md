# Inputs used by the true-feasibility run (output/CTP_30SEP_TRUE_FEASIBILITY_20260930)

**Run command:**

    python run_sep_fixed.py --plans 30-09 --out output/CTP_30SEP_TRUE_FEASIBILITY_20260930

The pipeline order is: build_sep_inputs → adapt_inputs → apply_biascutter_calibration → apply_routing_gapfill → apply_ctb_approximations → run_scheduler (phases 0-6) → capacity_wall → audit.

## A. Database tables read LIVE in this run (host 35.208.174.2)
| # | Database.Table | Rows | Used for |
|---|---|--:|---|
| 1 | JK_CTP_PPDS.CTP_Opening_WIP | 137,069 (snapshot 29-Sep 06:30 used, 11,694 rows) | Opening stock: components, green tyres and compounds (Batches converted to kg) |
| 2 | jkplanning_CTP.opening_wip | 169,356 | Old WIP feed. Now only a fallback for compound rows (0 used in this run) |
| 3 | jkplanning_CTP.gt_inventory_manual_pcr | 799 | Green-tyre ItemCode mapped to the BOM GT code |
| 4 | jkplanning_CTP.gt_inventory_manual_tbr | 851 | Same, for TBR |
| 5 | JK_CTP_PPDS.CTP_Master_BOM | 130,112 | BOM, including `tyre_type` for PCR/TBR item lines |
| 6 | JK_CTP_PPDS.CTP_Master_Routing | 46,816 | Routing: machines, rates, efficiency, batch weight, transfer time |
| 7 | JK_CTP_PPDS.CTP_Master_Aging | 4,934 | Min/max aging per item |
| 8 | JK_CTP_PPDS.CTP_Master_BuildingAllowable | 1,646 | Building cycle time per SKU (primary) |
| 9 | jkplanning_CTP.jkt_building_cycle_times_pcr | 260 | Building cycle time (fallback) |
| 10 | jkplanning_CTP.jkt_building_cycle_times_tbr | 93 | Building cycle time (fallback) |
| 11 | JK_CTP_PPDS.CTP_Master_ChangeoverMatrix | 293,166 | Upstream changeover minutes |
| 12 | JK_CTP_PPDS.CTP_Master_MPQ | 36 | TBR minimum lot quantities |

## B. Database tables read from a SAVED snapshot (db_pipeline/raw/), not refreshed in this run
| # | Table | Snapshot | Used for |
|---|---|---|---|
| 13 | jkplanning_CTP.downtime_reason | 29-Sep 23:33 | PM/MTC windows (BR-10). Only 1 press PM falls in the window. |

## C. Plant plan files (local)
| # | File | Used for |
|---|---|---|
| 14 | 30-09/ctp_curing_schedule_September_2026_pcr 9.xlsx | Curing plan, PCR (Shift Schedule sheet) |
| 15 | 30-09/ctp_curing_schedule_September_2026_tbr 9.xlsx | Curing plan, TBR |
| 16 | 30-09/ctp_building_schedule_September_2026_pcr 9.xlsx | Building plan, PCR (quantities, machines, shift slots, CO_Mins) |
| 17 | 30-09/ctp_building_schedule_September_2026_tbr 9.xlsx | Building plan, TBR |

## D. Local master files (not in the DB, or kept on purpose)
| # | File | Used for | Note |
|---|---|---|---|
| 18 | ctp_inputfiles/jkt_itemType_master_pcr.csv | Item type per item code | Checked: matches the DB for every scheduled item |
| 19 | ctp_inputfiles/jkt_mpq_corrected.xlsx | PCR minimum lot quantities | Plant-corrected PCR sheet |
| 20 | ctp_inputfiles/aging_comb.csv | Aging for 8 items missing in CTP_Master_Aging | Fallback only |
| 21 | config.yaml | All switches and business rules | Machine-to-changeover-line map, aging fallbacks, business rules, flags |

## E. Files generated from A-D (in ctp_inputfiles/ and inputs/)
| File | Made from |
|---|---|
| curing_sep_comb.xlsx, building_sep_comb.xlsx | the plan files (building cycle time from the DB masters + CO_Mins) |
| opening_wip_live.csv → inputs/inventory.csv | CTP_Opening_WIP 06:30 (+ GT code map, compound Batches → kg) |
| bom_live.csv → inputs/bom.csv | CTP_Master_BOM (+ CTB approximations) |
| routing_live.csv → inputs/routing.csv | CTP_Master_Routing (+ bias-cutter ×1.66, gap-fill, CTB approximations, outsourced items removed) |
| aging_sep_live.csv → inputs/aging_master.csv | CTP_Master_Aging + aging_comb fallback + GT 72 h |
| changeover_live.csv → inputs/changeover_matrix.csv, changeover_lookup.csv | CTP_Master_ChangeoverMatrix + config line map |
| mpq_tbr_live.csv | CTP_Master_MPQ (TBR rows) |
| item_tyre_type_live.csv | CTP_Master_BOM.tyre_type |
| inputs/mpq.csv | jkt_mpq_corrected.xlsx |
| inputs/itemtype_master.csv | jkt_itemType_master_pcr.csv |
| inputs/plan.csv, inputs/building_plan.csv | the plan files |
| inputs/routing_gapfill_log.csv, inputs/ctb_approximations_log.csv | logs of every filled or approximated value |

## F. Present but NOT used by this run
- inputs/buffer_master.csv: no phase reads it.
- inputs/mixing_mpq.csv: `use_mixing_mpq_file: false`.
- inputs/building_changeover.csv: building uses the plan's CO_Mins.
- Old local copies: bom_comb.csv, routing_comb.csv, jkt_changeover_matrix_combined (1).xlsx, opening_wip.csv. These were replaced by the DB.
- DB tables checked but not used: BuildingSchedule_* (October plan), CuringSchedule_* (April plan), CTP_Live_Inventory / _Raw (same compound data as CTP_Opening_WIP), CTP_Master_Buffer, CTP_Master_HolidayCalendar (no holiday in the window), CTP_Master_CuringCycleTime (equal to the plan's cycle times).
