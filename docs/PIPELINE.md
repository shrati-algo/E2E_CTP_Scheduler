# What happens in a run

`run_schedule.py` checks the plan folder, works out the window, and calls the engine driver `engine/run_v15.py`. The driver runs these steps in order; every one prints to the run log.

## A. Inputs

| Step | Script | What it does |
|---|---|---|
| 1 | `build_sep_inputs.py` | **Plans:** reads the four plan files, cuts them to the window, and re-rates TBM minutes with the database building cycle times.<br>**Masters:** reads the database masters (BOM, routing, aging, changeover, MPQ, FRC, mixing).<br>**master steps:** BOM from the corrected file, the SP003 fix, line speeds from `line_speed_by_item_3_months 1.xlsx` (faster of the two), then the **cutter / slitter bypass** (`_cutter_bypass`). The bypass collapses the 473 cut / slit items into their consumers, drops their routing rows and adds the cutting transfer minutes to the source items.<br>**Opening WIP:** reads the snapshot at the plan start, plus the correction |
| 2 | `adapt_inputs.py` | converts everything to the engine's input tables in `inputs/` (plan, building plan, BOM, routing, inventory, aging, changeover, MPQ …) |
| 3 | `build_mpq_items.py` | item-level minimum / maximum run quantities |
| 4 | `apply_biascutter_calibration.py`, `apply_routing_gapfill.py` | bias-cutter rate calibration; routing gaps filled |
| 5 | `apply_ctb_approximations.py` | WIP assumptions (expired life, 24 h flow, untracked components); **cut-part opening stock converted to its source material** (`cutter_bypass_stock`) |

## B. Scheduling (`run_scheduler.py` → `run_pipeline.py` → `phases/`)

| Phase | What it does |
|---|---|
| 0 | input validation (report only) |
| 1b | demand explosion: the building plan × the BOM gives per-tyre demand for every item (all BOM rows added) |
| 1.5 | wave builder |
| 1c | MRP netting of the opening WIP, time-phased, first-expiry-first-out |
| 2 | lot sizing: MPQ, whole mixing batches, buffer batches, 4RC roll lengths, MONOCHAFER 2-day campaign |
| 3 | DAG: links producer lots to consumer lots |
| 4 | CPM time windows (lag = min aging + routing transfer time) |
| 5 | **forward placement.**<br>**Mixing first:** the mixing planner (`mixer-improvement/mix_planner.py`) fixes the mixing lots, applying cover / gap rules, the butyl block (with the v133 6 h gap wait) and clean-outs.<br>**Placement:** every lot is placed with a material gate.<br>**4RC:** day plan, creel and wire campaigns, then the v133 MONOCHAFER urgent pull.<br>**Grouping:** extruder / calender / cutter / bead grouping passes.<br>**Building:** building echo with a physical kit ledger.<br>**Repairs:** aging pulls, gap fill, a final repair, then a ledger proof |
| 6 | curing re-timing: the plan's cure blocks on the **same presses**, moved to when green tyres are ready. The mould-change gap is kept, green tyres need 2 h and expire at 72 h |

Phases 5 and 6 then run again in **three closed-loop rounds**, feeding quantities back each time.

## C. Reports

`capacity_wall.py`, `audit_business_rules.py`, `make_schedule_v18.py` (the plant workbook), mixing compliance / audit, 4RC checks and sequence report, extruder rule checks (quintoplex, duplex, triplex, quadraplex), `validate_build_cure.py`, `mpq_checks.py`, `general_rules_checks.py`, `rule_adherence_report.py`, `make_version_readme.py`, `daily_numbers.py`, and **`backcalc/back_calculation.py`** (back calculation vs SMART).

More detail per stage: [reference/scheduling_flow.md](reference/scheduling_flow.md), [reference/architecture.md](reference/architecture.md), [reference/optimization_model.md](reference/optimization_model.md).
