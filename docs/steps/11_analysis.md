# Step 11: analysis

All files go to `outputs/<run>/analysis/`. The plan window (first shift → last planned shift) is taken from the run itself, so the analysis works for any day.

| Script | Writes | Answers |
|---|---|---|
| `analysis/all_machines_day1_start.py` | `ALL_MACHINES_DAY1_START_<run>.xlsx` | Does every machine (upstream, TBM, press) start in day-1 shift A? If not, why? Each late start is traced back through the lots that feed it to the root cause: a compound mixed at 07:00 plus its rest time, a machine busy first, a rule, the plan itself, or a data blocker |
| `analysis/shift_load_balance.py` | `SHIFT_LOAD_BALANCE_<run>.xlsx` (+ `.shift_detail.csv`) | Every machine, every shift: run, changeover, and idle time by cause (waiting for input, held by the mix plan, BR-14 blackout, kit wait, plan gap, green-tyre wait, no work). A verdict: level within ±20 % of the machine's own average, or not |
| `analysis/shift_balance_from_floor.py` | `SHIFT_BALANCE_PUBLISHED_<run>.xlsx` | The same balance read from the published floor schedule (step 6), next to the old "whole lot in its start shift" view, with the reason for each shift outside the band. Run after `shift_load_balance.py` |
| `analysis/built_not_cured.py` | `BUILT_NOT_CURED_<run>.xlsx` (+ CSVs) | Green tyres built but not cured. Balance per SKU at the plan end; every uncured press lot with its reason (press queue, started not finished, waiting for the lot's last green tyre, no GT, data-blocked); green tyres on the rack by hour; press idle while green tyres wait |
| `engine/make_machine_idle_report.py --machines 1001,1801` | `MACHINE_IDLE_1001_1801_<run>.xlsx` | Triplex and duplex extruders, minute by minute: run, changeover, waiting for compound, BR-14, ready-not-started, no work |

The written explanations of these analyses for 27 Oct (v46k) are kept locally, not in this repo.
