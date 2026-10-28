# Step 13: run summary

**Writes:** `outputs/<run>/RUN_SUMMARY.md`. Read this first. It holds:

1. **Inputs:**
   - the planner run used for each line (`run_id`, `plan_id`), with tyres, machines and plan window;
   - the stock and green-tyre syncs, and **how they were chosen** (policy or explicit).
2. **Schedule checks:** passed / failed, with the reason for each failure.
3. **Fulfilment:** built and cured per line and day, against the plan, data-blocked SKUs excluded.
4. **Warnings:**
   - plan data notes;
   - snapshots chosen by hand or struck at an unusual time;
   - any step that failed.
5. **Database:** the schedule_id, rows prepared and written to `CTP_Schedule_Floor`, and the read-back validation (PASS / FAIL).
6. **Steps:** every step with its exit code and run time.

**Also written:**
- `DATA_STAGED.json`: the snapshots and tables used;
- `RULES_USED.txt`;
- `DATA_BLOCKERS.csv`: every data blocker of both lines.

**Exit code:**
- 0: every routing check passed;
- 1: otherwise (on current data, the known machine-coverage finding);
- other: the run stopped early, and the console says why.
