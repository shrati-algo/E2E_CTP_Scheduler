# Step 5: the scheduler (engine v46k, both lines)

**Code:** `engine/run_all.py --run <run>`. Rules: `config/rules.yaml` (resolved v46k).

| Phase | What it does | Main file (`outputs/<run>/<line>/`) |
|---|---|---|
| 0 | Which plan rows the masters can build; data blockers (no BOM, no routing, …) | `phase0_sku_status.csv`, `phase0_findings.csv` |
| 1 | Building plan → component demand per trolley, with need-by times | `phase1_demand.csv` |
| 2 | Opening stock netted against demand (FEFO, maturity, shelf life, cut-stock conversion) | `phase2_demand_net.csv`, `phase2_wip_allocations.csv` |
| 3 | Lots (lot sizing, campaigns, 4-roll calender dips) | `phase3_lots.csv` |
| 4 | Precedence and timing (CPM) | `phase4_edges.csv`, `phase4_lot_times.csv` |
| campaign / align / 4b | Campaign release; need-by re-aimed; machine assignment | `campaign_plan.csv`, `phase4b_assignment.csv` |
| 5 | Placement on machines, full-kit gate for building | `phase5_schedule.csv`, `phase5_building_plan.csv` |
| 6 | Shift-wise floor schedule (engine view) | `floor_schedule.csv` |
| 7 | Curing, gated on the real green tyres | `phase7_curing_plan.csv` |

The upstream machines (mixers, calenders, extruders, …) are planned **jointly** for PCR and TBR.

**The run stops here if a phase fails.** The steps after this read these files.
