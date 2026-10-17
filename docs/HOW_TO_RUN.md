# How to run the scheduler without cutters and slitters

## 1. Install (once)

- **Python:** 3.11 or newer (tested on 3.13), on Windows, Linux or macOS.
- **Packages:** from the repository folder, run:
  ```powershell
  pip install -r engine\requirements.txt
  ```
  The main ones are pandas, numpy, openpyxl, pyyaml and pymysql (the last only for `--db live`).
- **Disk:** about 300 MB for the repository, plus about 90 MB per run in `engine/latest_schedule/<run>`.

## 2. Prepare a plan folder

Put the four plant plan workbooks in one folder. There must be exactly one of each:

| File name pattern | Content |
|---|---|
| `ctp_curing_schedule_*_pcr*.xlsx` | PCR curing plan |
| `ctp_curing_schedule_*_tbr*.xlsx` | TBR curing plan |
| `ctp_building_schedule_*_pcr*.xlsx` | PCR building plan |
| `ctp_building_schedule_*_tbr*.xlsx` | TBR building plan |

- **Sheet:** each must have a sheet `Shift Schedule`. Its header row must contain at least `Machine`, `SKUCode`, `StartTime`, `EndTime` and `Qty`. The building plans also need `GT_Code`, `Description`, `CO_Mins` and `Shift`.
- **Format:** these are the plant's own export files and are used as they are. More in [INPUTS.md](INPUTS.md).
- **Location:** you can keep plan folders anywhere. `plans/` is just a convenient place.

## 3. Check, then run

```powershell
# check only (seconds): files found, rows / qty / dates per file, plan window, opening-WIP snapshot
python run_schedule.py --plans plans\plan-130ct --check-only

# run (about 10-60 min depending on the computer)
python run_schedule.py --plans plans\plan-130ct
```

While it runs, every step is printed and also written to `engine/latest_schedule/<run>/run_log.txt`. At the end, the cured totals are printed and the results are copied to `outputs/<run>/`.

## 4. Options

| Option | Default | Meaning |
|---|---|---|
| `--plans FOLDER` | (required) | folder with the four plan workbooks |
| `--name NAME` | `ncs_<folder name>` | run name: the folder under `outputs/` and `engine/latest_schedule/` |
| `--start "YYYY-MM-DD HH:MM"` | 07:00 of the first curing day | plan start, which is also the **opening-WIP time** |
| `--end "YYYY-MM-DD HH:MM"` | 07:00 after the last curing day | plan end. Plan rows starting at or after it are left out |
| `--db offline\|live` | `offline` | `offline`: use the snapshot copy `engine/db_pipeline/raw`. `live`: read the database (read-only; needs `.env`, see section 6) and refresh the copy |
| `--wip-source mix\|old` | `mix` | `mix` (as v133): opening WIP from `JK_CTP_PPDS.CTP_Opening_WIP`, the latest snapshot at or before the plan start. `old`: `jkplanning_CTP.opening_wip` of the plan-start day |
| `--wip-correction "YYYY-MM-DD HH:MM"` | none | also adds the lots of a later WIP snapshot that were produced before the plan start. v133 used `"2026-10-12 17:26"` |
| `--bom corrected\|db` | `corrected` | `corrected`: `engine/bom_corrected/CTP_Master_BOM_corrected.csv` (as v2 / v3). `db`: the database `CTP_Master_BOM` as it is |
| `--compare NAME …` | none | earlier run name(s) in `engine/latest_schedule/` to compare the daily numbers with |
| `--check-only` | | check the inputs and print the run plan, then stop |

**Plant day:** 07:00 → 07:00. The default window is therefore whole plant days covering every curing row in the plan files. If a plan file carries extra rows beyond the days you want, set `--end`.

## 5. Reproduce the reference results

```powershell
# v3: plan-130ct, opening WIP 13-Oct 06:31, window 13 -> 23-Oct (as the v3 run)
python run_schedule.py --plans plans\plan-130ct --end "2026-10-23 07:00" --name ncs_reproduce_v3

# v2: plan-12oct, opening WIP 12-Oct 06:31 + 17:26 correction, window 12 -> 22-Oct
python run_schedule.py --plans plans\plan-12oct --end "2026-10-22 07:00" --wip-correction "2026-10-12 17:26" --name ncs_reproduce_v2
```

- **Expected v3:** 130,366 / 142,096 cured (PCR 107,260, TBR 23,106).
- **Verified 17-Oct-2026:** the v3 command gave 130,366, with the daily table identical to the original v3 run.
- **Expected v2:** 152,200 / 175,628 cured (PCR 122,988, TBR 29,212).
- Daily tables: [NO_CUTTER_SLITTER.md](NO_CUTTER_SLITTER.md).

## 5a. Change the cutting / slitting transfer time

Edit `engine/config.yaml` → `cutter_bypass.sections.<section>.transfer_min`; `default_transfer_min` is used for sections without a value. `--check-only` prints the values in force. The minutes are added to the source item's transfer time, summed along a chain (see [NO_CUTTER_SLITTER.md](NO_CUTTER_SLITTER.md)).

## 6. Using the live database (`--db live`)

1. **Credentials:** copy `engine/db_pipeline/.env.example` to `engine/db_pipeline/.env` and fill in the host, port, user and password.
   - **Never** commit or share this file.
   - The engine never prints the password.
2. **Run:** `python run_schedule.py --plans my-plan --db live`
   - Every table the engine reads is fetched again (read-only `SELECT`). The snapshot copy in `engine/db_pipeline/raw` is refreshed with it, so later offline runs use the new data.
   - If the server cannot be reached, the engine says so and uses the existing copy.
3. **Opening WIP:** `JK_CTP_PPDS.CTP_Opening_WIP` keeps several snapshots a day (about 06:30, 14:30 and 23:30). The engine takes the latest one at or before the plan start. Check which one with `--check-only`.

**Nothing is ever written to the database.** The engine only reads.

## 7. Offline snapshot copy: what is in it

`engine/db_pipeline/raw/` holds one CSV per database table (`<database>__<table>.csv`).

| Content | Date of the copy |
|---|---|
| Masters (BOM, routing, aging, changeover, MPQ, mixing, building cycle times, …) | 13-Oct / 14-Oct, as in the v133 run |
| Opening WIP (`CTP_Opening_WIP`, `opening_wip`) | the 15-Oct pull, with snapshots 10-Oct → 15-Oct 15:28 |

The 15-Oct WIP pull's 12-Oct snapshots are identical row for row to those v133 used.

For a plan that starts after 15-Oct, use `--db live` (or refresh the copy), or the engine falls back to an older WIP snapshot. `--check-only` warns about this.

## 8. Troubleshooting

| Message | Cause / fix |
|---|---|
| `need exactly one curing plan for PCR …` | file missing or two matching files in the folder; keep exactly one of each |
| `sheet 'Shift Schedule' has no header row …` | wrong workbook or sheet renamed; the plant export must keep the `Shift Schedule` sheet |
| `WARNING: the snapshot copy has no CTP_Opening_WIP snapshot on …` | the offline copy does not reach the plan date; use `--db live` |
| `engine stopped (exit N)` | open `engine/latest_schedule/<run>/run_log.txt`; the last lines name the step that failed |
| `[db] … attempt 1/4 failed` lines in an offline run | expected: offline runs block the database connection and use the copy |
| run is slow | normal: 10–60 min depending on the computer; phase 5 and the three closed-loop rounds take most of it |

## 9. Re-running only part of a run (advanced)

- **Reports only.** The run's working files stay in `engine/latest_schedule/<run>/run_output/`, and the reports can be re-made from them. From inside `engine/`:
  ```powershell
  python daily_numbers.py <run> [<other run> ...]
  python rule_adherence_report.py <run>
  python general_rules_checks.py latest_schedule\<run>\run_output --xlsx latest_schedule\<run>\GENERAL_RULES_<run>.xlsx
  ```
- **Engine driver directly.** The full set of driver options (as used in the project) is `python run_v15.py --help`, run inside `engine/`.
