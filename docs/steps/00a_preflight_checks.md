# Step 0: pre-flight checks (are the day's tables updated?)

**Code:** `pipeline/preflight.py`.

It runs **first**, before anything is staged or scheduled. **If any check fails, nothing runs** (exit code 2).

```bash
python run_pipeline.py --date 2026-10-28 --check-only     # only the checks
```

## The checks

| Id | Check | FAIL when | WARN when |
|---|---|---|---|
| DB-1 | Database login present | `db.env` / environment lacks host, user or password | |
| DB-2 | Database reachable | the connection fails | |
| BP-PCR, BP-TBR | `CTP_Schedule_BuildingPlan` updated for the day | no plan of that line with `plan_start` on the day | |
| CP-PCR, CP-TBR | `CTP_Schedule_CuringPlan` updated for the day | no curing rows of the **same planner run** (`run_id`) | |
| PL-PCR, PL-TBR | Plan content | | the first start is not the day's 07:00, rows with Qty ≤ 0, or rows ending before they start |
| WIP-1 | `CTP_Opening_WIP` updated for the day | no shift-A sync on the day | an explicit `--wip-sync` was used instead |
| WIP-2 | WIP snapshot complete | rows under 50 % (`--min-snapshot-ratio`) of the usual size (largest of the previous 9 syncs) | |
| WIP-3 | WIP struck near 06:30 | | struck outside 05:30–07:30 |
| GT-PCR-1..3, GT-TBR-1..3 | `CTP_GT_Inventory`: the same three checks, per line | as above | as above |

## Where the results go

- **Terminal:** one line per check, `[PASS]` / `[WARN]` / `[FAIL]`, then a count and *OK to run* or *NOT RUN*.
- `outputs/<run>/PREFLIGHT_CHECKS.md` and `PREFLIGHT_CHECKS.csv`: this run's checks.
- `logs/preflight_history.csv`: every check of every run, appended. It shows on which days which table came in late or incomplete.
- `outputs/<run>/logs/run_<time>.log`: the whole terminal output of the run, including every engine step.

## Examples (run on 28 Oct)

**Plan day 28 Oct.** The plans and GT pass. The WIP shift-A sync was struck at **09:46 with 1,644 rows** against the usual 13,333:

```
[FAIL] WIP-2    CTP_Opening_WIP: snapshot complete    1,644 rows vs usual 13,333 (minimum 50%)
[WARN] WIP-3    CTP_Opening_WIP: struck near 06:30    struck at 09:46
14 passed, 2 warning(s), 1 failed   ->  NOT RUN: fix the failed check(s) above
```

**Plan day 29 Oct** (not yet in the tables):

```
[FAIL] BP-PCR   CTP_Schedule_BuildingPlan PCR: updated for 2026-10-29   no PCR plan with plan_start on 2026-10-29; latest plan days ['2026-10-28']
[FAIL] WIP-1    CTP_Opening_WIP: updated for 2026-10-29 (shift-A sync)  no shift-A sync on 2026-10-29; latest sync 2026-10-28 17:26:23.022
...
2 passed, 0 warning(s), 7 failed   ->  NOT RUN
```

**Running anyway with a snapshot you choose.** Pass `--wip-sync` (or `--gt-sync-pcr` / `--gt-sync-tbr`). WIP-1 then becomes a WARN that names the chosen sync, and WIP-2 still checks it is complete.
