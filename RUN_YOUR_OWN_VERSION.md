# Running the CTP scheduler yourself: new versions, database or custom files, daily numbers

This guide covers:
- how to produce a new schedule version with one command;
- where every input comes from (the database or a file), and how to switch an input to your own file;
- what each run writes, including the **daily achieved numbers** that every run now prints and saves.

All commands are for **PowerShell**, run from the project folder:

```powershell
cd C:\Users\shradha\Downloads\JKT-CTP-SCHEDULER-main\JKT-CTP-SCHEDULER-main
```

---

## 1. Quick start

```powershell
python run_v15.py --wip-date 2026-10-10 --wip-time 07:00 --plan-end "2026-10-20 07:00" --plan-dir plan_files_10oct --name v85 --wip-source mix --compare v82
```

| Option | Meaning |
|---|---|
| `--wip-date`, `--wip-time` | Plan start and opening-WIP time. The run takes the latest WIP snapshot at or before this time on that day; 07:00 picks the 06:30 snapshot |
| `--plan-end` | End of the plan window, e.g. `"2026-10-20 07:00"`. Instead you can give `--days 10` |
| `--plan-dir` | Folder with the four plan files (section 4) |
| `--name` | Version name. The output folder is `latest_schedule\<name>\`. **Use a new name every run**: an existing name is overwritten |
| `--wip-source mix` | Components from `JK_CTP_PPDS.CTP_Opening_WIP`; compounds and green tyres from the old feed `jkplanning_CTP.opening_wip`. This is what v78–v84 used. `old` = old feed only |
| `--compare v82` | Optional. Adds "against v82" totals to the daily numbers. You can name several versions |
| `--offline` | Do not contact the database; use the last downloaded tables in `db_pipeline\raw\` (section 3.3) |

A run takes about 45–60 minutes. It ends with:

```
ALL STEPS DONE -> latest_schedule\v85\CTP_10OCT-20OCT_SCHEDULE_v85.xlsx
  daily numbers: latest_schedule\v85\DAILY_NUMBERS_v85.md
```

The daily table is also printed on screen just before that line (section 6).

**To stop a run:** press `Ctrl + C` in its window. If it was started in the background, end its `python.exe` processes in Task Manager.

---

## 2. One-time setup

```powershell
pip install -r requirements.txt
```

**Database login:** `db_pipeline\.env` holds the login. Never share or commit this file.

```
DB_HOST=<server>
DB_PORT=3306
DB_USERNAME=<user>        (DB_USER= is accepted too)
DB_PASSWORD=<password>
```

**Check the connection and which WIP snapshots exist:** see `HOW_TO_RUN_V15.md`, section 1. It has a one-line command that lists the snapshots of `opening_wip` and `CTP_Opening_WIP`. Choose `--wip-date` / `--wip-time` from that list.

---

## 3. Where every input comes from

### 3.1 Read from the database on every run

`build_sep_inputs.py` reads each table below live. It saves a copy in `db_pipeline\raw\<db>__<table>.csv`, and the later steps use that copy.

| Table | Used for |
|---|---|
| `JK_CTP_PPDS.CTP_Opening_WIP` | Opening stock: components (with `--wip-source mix`) |
| `jkplanning_CTP.opening_wip` | Opening stock: compounds and green tyres (old feed) |
| `JK_CTP_PPDS.CTP_Master_BOM` | BOM. **Currently replaced by a file**, see 3.2 |
| `JK_CTP_PPDS.CTP_Master_Routing` | Machines, speeds and cycle times, batch sizes. **Currently replaced by a file**, see 3.2 |
| `JK_CTP_PPDS.CTP_Master_Aging` | Minimum and maximum aging (shelf life) |
| `JK_CTP_PPDS.CTP_Master_ChangeoverMatrix` | Changeover times per machine |
| `JK_CTP_PPDS.CTP_Master_MPQ` | Minimum / maximum run quantities |
| `JK_CTP_PPDS.CTP_Master_FRC` | 4RC wire / dip, spool lengths |
| `JK_CTP_PPDS.CTP_Master_BuildingAllowable` | Allowed building machines and cycle times |
| `jkplanning_CTP.jkt_building_cycle_times_pcr` / `_tbr` | Building cycle times (fallback) |
| `jkplanning_CTP.gt_inventory_manual_pcr` / `_tbr` | Green-tyre code ↔ SKU mapping for the WIP |
| `jkplanning_CTP.downtime_reason` | Planned machine downtime |

If the server does not answer (4 tries), the run uses the last saved copy and says so in the log: `server unreachable -> using SNAPSHOT`.

### 3.2 Replacing any table with your own file (`master_files`)

In `config.yaml`, the `master_files` section maps a table name to a file. The run then reads that file instead of the database:

```yaml
master_files:                       # table name: file (path relative to the project folder, or absolute)
  CTP_Master_BOM: latest_schedule/BOM_Routing_9oct/CTP_Master_BOM_v0.xlsx
  CTP_Master_Routing: latest_schedule/BOM_Routing_9oct/CTP_Master_Routing_v0.xlsx
```

This is the current setting: BOM and routing come from the 9-Oct files, not the database.

**Rules for a custom file:**
- It works for **any table in 3.1**: write the table name exactly as listed, without the `JK_CTP_PPDS.` part.
- The file must have **the same columns as the database table**. The easiest start is to copy `db_pipeline\raw\<db>__<table>.csv`, edit it and save it under a new name.
- For `.xlsx`, the sheet must be named exactly like the table (e.g. sheet `CTP_Master_BOM`). A `.csv` needs no sheet name.
- The log confirms each file it used: `[file] JK_CTP_PPDS.CTP_Master_BOM: 129,671 rows FROM FILE ...`.

**Examples:**

| You want | Put in `master_files` |
|---|---|
| Your own aging master | `CTP_Master_Aging: my_inputs/aging_corrected.csv` |
| Corrected opening WIP (e.g. the plant's fixed compound stock) | `CTP_Opening_WIP: my_inputs/opening_wip_fixed.csv` (columns as in `db_pipeline\raw\JK_CTP_PPDS__CTP_Opening_WIP.csv`, incl. `updated_at` = snapshot time) and/or `opening_wip: my_inputs/old_feed_fixed.csv` for compounds |
| Plant mixing cycle times / batch weights | A corrected copy of the routing file in `CTP_Master_Routing` |
| Changeover matrix with the missing pairs | `CTP_Master_ChangeoverMatrix: my_inputs/matrix.csv` |

### 3.3 Using the database for everything

1. **Delete or comment out the two `master_files` lines** (BOM and routing). Put `#` in front:

   ```yaml
   master_files:
   #  CTP_Master_BOM: latest_schedule/BOM_Routing_9oct/CTP_Master_BOM_v0.xlsx
   #  CTP_Master_Routing: latest_schedule/BOM_Routing_9oct/CTP_Master_Routing_v0.xlsx
   ```

2. **Turn off the WIP assumptions that come from files** (both are on since v80):

   | Setting | What it does | For DB only |
   |---|---|---|
   | `ctb_wip_assumptions.enabled` | Uses `CTB_WIP_ASSUMPTIONS_v2.xlsx`: expired stock usable 24 h, one day's measured flow as stock, untracked components available on day 1 | `false` |
   | `kit_gate_assume_first_h.enabled` | Strip items (cap strip, edge strip, steel belt edge strip) assumed available for the first 24 h | `false` |

   With both off, day 1–2 will be much lower (v78 / v79 levels). That is the real effect of the gaps in the WIP feed.

3. **Run without `--offline`.** Check the log: every table should show `[db] ... LIVE`.

**Inputs that are always files:**
- the **plan files** (section 4);
- `INPUT_TABLES_30SEP_TRUE/.../JK_CTP_PPDS__CTP_Master_Routing.csv`, the batch-size source for whole-batch mixing (`business_rules.mixing_batch_source`). Point it to `db_pipeline/raw/JK_CTP_PPDS__CTP_Master_Routing.csv` to use the latest database routing.

### 3.4 Offline runs

`--offline` uses the copies in `db_pipeline\raw\` from the last successful database read. It is useful when the server is unreachable, or to repeat a run on exactly the same data. `master_files` still applies.

---

## 4. Plan files

Put the four plant plan files in one folder and pass it with `--plan-dir`:

```
ctp_curing_schedule_<Month>_2026_pcr*.xlsx
ctp_curing_schedule_<Month>_2026_tbr*.xlsx
ctp_building_schedule_<Month>_2026_pcr*.xlsx
ctp_building_schedule_<Month>_2026_tbr*.xlsx
```

- Exactly one file of each kind, each with a `Shift Schedule` sheet.
- A file may cover the whole month. Only rows inside `--wip-date … --plan-end` are used.

Current folders:
- `plan_files_10oct` (PCR 12 / TBR 13), used for v78 onwards;
- `latest_plan_file` (06-Oct plan).

---

## 5. Making a new version

1. **Choose a name** one above the last one in `latest_schedule\` (e.g. `v85`).
2. **Change what you want to test** in `config.yaml` (rules, switches) or in `master_files` (data). Keep a copy first:

   ```powershell
   copy config.yaml config_before_v85.yaml
   ```

   The current `config.yaml` is the best version, **v82**.
3. **Run** with `--name v85 --compare v82`.
4. **Read** `latest_schedule\v85\DAILY_NUMBERS_v85.md` and the rule reports (section 6).
5. **If you keep it**, add a short entry to `latest_schedule\BUSINESS_RULES_LOG.md`: what changed, the result, kept or not. **If you don't**, restore the config: `copy config_before_v85.yaml config.yaml`.

**To reproduce v82 exactly:**

```powershell
python run_v15.py --wip-date 2026-10-10 --wip-time 07:00 --plan-end "2026-10-20 07:00" --plan-dir plan_files_10oct --name v82_rerun --wip-source mix --compare v82
```

**For a new WIP day / plan**, e.g. WIP 13-Oct 07:00 on a new plan folder:

```powershell
python run_v15.py --wip-date 2026-10-13 --wip-time 07:00 --days 10 --plan-dir plan_files_13oct --name v85 --wip-source mix
```

---

## 6. What every run writes (`latest_schedule\<name>\`)

| File | Content |
|---|---|
| **`DAILY_NUMBERS_<name>.md` / `.xlsx`** | **Per plant day (07:00 → 07:00), PCR and TBR: build plan, built, cure plan, cured, cured % of plan, plus totals** (and differences against `--compare` versions) |
| `CTP_<start>-<end>_SCHEDULE_<name>.xlsx` | The full schedule in the plant workbook format |
| `run_log.txt` | The complete log of the run (saved automatically; password lines are never written) |
| `README_<name>.md` | The rules in force in this version, by section |
| `RULE_ADHERENCE_REPORT_<name>.xlsx` | Pass / partial / fail per business rule |
| `MIXING_RULE_COMPLIANCE_…`, `MIXING_RULE_AUDIT_…`, `FOURRC_RULES_…`, `QUINTOPLEX / DUPLEX / TRIPLEX / QUADRAPLEX_RULES_…`, `MPQ_COMPLIANCE_…`, `GENERAL_RULES_…`, `BUILD_CURE_VALIDATION_…` | Rule checks per section |
| `run_output\` | All phase outputs (lots, links, machine schedule, curing) |

**Daily numbers for any existing version, or a comparison afterwards:**

```powershell
python daily_numbers.py v85 v82 v84
```

This writes `DAILY_NUMBERS_v85.md` / `.xlsx` with the totals against v82 and v84, and prints the table.

---

## 7. Troubleshooting

| Message | Meaning / fix |
|---|---|
| `Access denied for user …` | User or password in `db_pipeline\.env` is wrong |
| `server unreachable -> using SNAPSHOT …` | Database not reachable; the run continues on the saved copy. Re-run later for live data |
| `… unreachable and no snapshot in db_pipeline\raw` | No database and no saved copy: connect once, or use `master_files` |
| `need exactly one curing/pcr plan in …` | The plan folder has zero or more than one file of that kind (section 4) |
| `[file] … FROM FILE …` missing for a file you set | The table name in `master_files` is misspelled, or the xlsx sheet name differs from the table name |
| Daily numbers all 0 | The plan window has no rows: check `--plan-dir` and the dates |
| `<step>.py failed (exit 1)` | Open `latest_schedule\<name>\run_log.txt`; the error is just above that line |
