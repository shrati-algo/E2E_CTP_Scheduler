# Outputs

At the end of a run, `run_schedule.py` copies the result files to **`outputs/<run name>/`**. The large working files stay in `engine/latest_schedule/<run name>/run_output/`.

## In `outputs/<run name>/`

| File | What it is |
|---|---|
| `CTP_<start>-<end>_SCHEDULE_<run>.xlsx` | **The floor schedule** in the plant workbook format: every machine, lot, start / end time, quantity, consumption links, per section (mixing, calender, extruders, cutters, bead, building, curing) |
| `DAILY_NUMBERS_<run>.md` / `.xlsx` | **Plan vs built vs cured per plant day** (07:00 → 07:00), PCR and TBR, with totals. Start here |
| `RULE_ADHERENCE_REPORT_<run>.xlsx` | adherence % per business rule and section (PASS / PARTIAL / FAIL) |
| `GENERAL_RULES_<run>.xlsx` | general rules (BR-1 …): plan shift, machine, family changes, blocks |
| `MIXING_RULE_COMPLIANCE_<run>.xlsx`, `MIXING_RULE_AUDIT_<run>.xlsx`, `MIXING_RULE_EVIDENCE_<run>.xlsx` | mixing rules (cover, gaps, clean-outs, butyl block …), lot by lot |
| `FOURRC_RULES_<run>.xlsx`, `FOURRC_SEQUENCE_REPORT_<run>.xlsx` | 4-roll calender 901 rules, utilisation, idle causes, wire / dip sequence |
| `QUINTOPLEX_RULES_*.xlsx`, `DUPLEX_RULES_*.xlsx`, `TRIPLEX_RULES_*.xlsx`, `QUADRAPLEX_RULES_*.xlsx` | extruder rules per line |
| `MPQ_COMPLIANCE_<run>.xlsx` | lots below / above min / max run quantity (must be 0 below) |
| `BUILD_CURE_VALIDATION_<run>.xlsx` | building ↔ curing consistency (green-tyre age, press feed) |
| `README_<run>.md` | rules in force, by section |
| `run_log.txt` | full log of the run (password lines never written) |
| `RUN_INFO.txt` | how the run was called (plans, window, database, engine command) |

## In `engine/latest_schedule/<run name>/run_output/`

| File | What it is |
|---|---|
| `phase5_floor_schedule_updated.csv` | machine-level schedule, one row per production block (machine, item, start, end, quantity, lot) |
| `phase5_schedule_updated.csv` | lot-level schedule |
| `phase6_curing_revision_updated.csv` | curing blocks after re-timing on the plan's presses |
| `phase5_building_echo_gates.csv` | per building row: planned start, kit-ready time, **binding item** (what held the TBM), actual start |
| `phase5_machine_utilization_updated.csv` | hours busy per machine |
| `phase2_lots_updated.csv` | all production lots |
| `phase4_aging_violations_updated.csv` | aging checks |
| `opening_wip_used.csv` | the opening WIP as used |
| `CTP_FLOOR_SCHEDULE.xlsx`, `DAILY_REPORT.xlsx` | engine-format schedule and daily report |
| `BUSINESS_RULE_AUDIT.csv` | rule audit rows |
| `PHASEWISE/` | every phase's outputs (demand, netting, lots, DAG, CPM, placement, curing) |

## Reading the result

- **Cured tyres against the cure plan** is the main measure: total, then per day. The daily table shows **where** output was lost.
- **Weak day:** a day with a low cured % is usually held by one upstream section. Use `phase5_building_echo_gates.csv`: for the late building rows, group the `binding_item` column to see which material was late.
- **Integrity lines in `run_log.txt`:**
  - `STARVED groups: 0`, meaning no material starvation;
  - `green tyres scrapped`, meaning green tyres past 72 h;
  - `lots below MPQ (must be 0)`.
- **Noise:** about ±4–5 k cured from run to run with slightly different inputs.
