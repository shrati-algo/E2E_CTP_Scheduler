# Experiment results and key analyses

Back to [CLAUDE.md](../CLAUDE.md).

Results by dataset are in [version_history_v45_latest.md](version_history_v45_latest.md). **Never compare across datasets A / B / C / D.**

There are no solver metrics (no solver). A full run takes ~45–60 min. Exact per-phase times are in each `run_log.txt` and are not summarised here.

## 1. Current dataset D (plan `plan_files_10oct`, WIP 10-Oct 07:00, window 10–20 Oct; plan cure 173,324)

| Run | Cured | PCR | TBR | Built | GT scrapped | Adherence P / Pa / F (log) | Notes |
|---|---|---|---|---|---|---|---|
| v78 | 119,124 (68.7 %) | 100,380 | 18,744 | 119,108 | 320 | 67 / 7 / 7 | New plan |
| v79 | 113,040 | 95,889 | 17,151 | 111,886 | 420 | 67 / 7 / 7 | Strip items assumed 24 h |
| v80 | 124,413 (71.8 %) | 102,862 | 21,551 | 123,090 | 586 | 67 / 9 / 5 | WIP assumptions |
| v81 | 127,021 (73.3 %) | 105,693 | 21,328 | 126,589 | 708 | 68 / 7 / 6 | Gap fill A1 / A2 |
| **v82** | **127,864 (73.8 %)** | **106,348** | **21,516** | 127,174 | 541 | 68 / 9 / 4 | **Best** |
| v83 | 127,708 | 105,877 | 21,831 | 126,965 | 536 | 69 / 9 / 6 (cell count) | Item-stock allocation |
| v84 | 126,196 | 104,867 | 21,329 | 125,077 | 471 | 69 / 8 / 7 (cell count) | 4RC ahead |

**Command (D):**

```
python run_v15.py --wip-date 2026-10-10 --wip-time 07:00 --plan-end "2026-10-20 07:00" --plan-dir plan_files_10oct --name vNN --wip-source mix
```

(v78 was logged without `--wip-time` / `--wip-source`; the run-log header shows the WIP used.)

Cell counts from `RULE_ADHERENCE_REPORT_*.xlsx`: v82 69 / 10 / 5, v83 69 / 9 / 6, v84 69 / 8 / 7.

**v82 vs v83 vs v84, cured per day:**

| Day | PCR v82 | PCR v83 | PCR v84 | TBR v82 | TBR v83 | TBR v84 |
|---|---|---|---|---|---|---|
| 10 | 12,077 | 12,077 | 12,077 | 2,627 | 2,627 | 2,627 |
| 11 | 9,862 | 9,783 | 9,715 | 2,413 | 2,348 | 2,492 |
| 12 | 4,514 | 5,639 | 4,782 | 2,005 | 1,816 | 2,049 |
| 13 | 8,466 | 8,307 | 8,945 | 1,826 | 2,267 | 1,686 |
| 14 | 10,588 | 12,455 | 12,131 | 803 | 1,612 | 1,234 |
| 15 | 12,201 | 13,025 | 9,331 | 2,465 | 1,475 | 2,745 |
| 16 | 12,932 | 9,423 | 12,147 | 2,879 | 2,450 | 2,491 |
| 17 | 12,818 | 11,191 | 12,671 | 2,872 | 2,189 | 2,419 |
| 18 | 11,334 | 11,652 | 11,356 | 2,085 | 2,996 | 1,640 |
| 19 | 11,558 | 12,325 | 11,712 | 1,541 | 2,051 | 1,946 |

The gains on single days are given back on others; the totals stay within the noise.

## 2. Dataset C best runs (06-Oct plan, to 16-Oct)

| Run | Cured | Note |
|---|---|---|
| v65 | 111,969 (PCR 93,763 / TBR 18,206) | Left-shift |
| v76 | 114,322 | Integration |
| v77 | 114,119 | Building waits for every input |
| v75_relaxed | **118,815 (70.1 % of 169,570)** | Plant mixing data, **evaluation only** |

## 3. Key analyses (evidence for decisions)

| Analysis | Result | File |
|---|---|---|
| Section use vs target (13.5 k PCR + 3.5 k TBR a day) | Needed use: finals 92 %, masters 94 %, 1802 109 %, 1803 98 %, 1801 85 %, building 87–89 %, curing 91 %; other component machines 9–73 %. v82 actual: mixers ~95 %, components 25–56 % | `latest_schedule/v82/CAPACITY_vs_TARGET_v82.md` |
| 12–15 Oct gap (v80) | Algorithmic (A1–A6) vs data (D1–D6) causes | `latest_schedule/v80/GAP_12_15_OCT_v80.md` |
| PCR 12-Oct dip | Tread chain MT4333H → MT4444H (24 h min aging) → T4555H with no stock; inner liner waits for the chafer strip (4RC MONOCHAFER on 13-Oct) and butyl IL3 / ML405 (evening only) | chat 12-Oct (not saved as a file) |
| Steel belt as the binding item | **Wrong reading.** Belts have 24 h shelf life and are cut ~4.4 h before building on purpose; in 71 / 94 cases another input is later | `DATA_ISSUES_FOR_PLANT_v82.md` §4 |
| B1144 / 1802 | B1144 runs are 30–89 h late vs need, not just in time. Making it ahead adds only a few hundred TBR (other TBR inputs ~2 h behind) | chat 12-Oct |
| Item-stock gating estimate | 286 lots held by reservations (1,755 h), 98 late long-shelf-life lots (583 h). Estimated +0.5–2 k; the actual run (v83) was flat | v83 |
| Green-tyre "double count" | The plan already nets green-tyre stock; do not net | [known_issues.md](known_issues.md) |
| 1802 speed | Plant 9.97 m/min while running (610 rolls), 18.5 h / day, 11,370 m / day; 0.250 kg/m confirmed | `plant-schedule/BEF_1802_LINE_SPEED_ANALYSIS.md` |
| 4RC plant vs v82 | Plant 45.8 km / day vs v82 23.1 (BOM need 23.9); creel median 169 min; fabric fillers | `plant-schedule/PLANT_vs_v82_4RC_PROCESS.md` |
| Mixing plant vs routing | Plant ~8 % faster per batch but busy 20–21 h; batch weights differ | `plant-schedule/PLANT_vs_SCHEDULER_MIXING_CYCLE_TIMES.md` |
| Algo Planning BRBC rules | Per version | `plant-schedule/ALGO_PLANNING_RULES_CHECK_v77.md` and `.xlsx` for v77, v78, v80, v81, v82 (none for v79, v83, v84) |
