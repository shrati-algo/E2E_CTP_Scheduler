# How to run the CTP schedule (v15) for any opening-WIP date and time

v15 runs the full CTP schedule with every rule up to v14, starting from the opening WIP in the database at a date and time you choose:

- mixing in whole batches;
- 60 s changeover;
- 120 s butyl clean-out on the final mixers and none on the master mixers;
- IL3/IL5 daily campaign, always with B99 directly in front. Since v16, a 5-batch B99 prep lot is made when no existing B99 lot fits (`config.yaml` → `il_daily_campaign.prep_make_lot`);
- +5 buffer batches;
- aging taken from `aging_comb.csv` only.

The run covers mixing, upstream, building and curing, and writes the Excel workbook.

One command does everything: **`python run_v15.py`**.

---

## 1. One-time setup

All commands run from the project folder in **PowerShell**:

```powershell
cd C:\Users\shradha\Downloads\JKT-CTP-SCHEDULER-main\JKT-CTP-SCHEDULER-main
pip install -r requirements.txt
```

### Database credentials: `db_pipeline\.env`

```powershell
copy db_pipeline\.env.example db_pipeline\.env
notepad db_pipeline\.env
```

Fill in the real values. Use no quotes and no spaces around `=`:

```
DB_USER=root
DB_PASSWORD=<password>
DB_HOST=<server IP>
DB_PORT=3306
DB_NAME=jkplanning_CTP
```

`DB_USERNAME=` is also accepted in place of `DB_USER=`. Never share or commit this file.

**Check the connection and list the available WIP snapshots:**

```powershell
python -c "import sys; sys.path.insert(0,'db_pipeline'); from db_connect import load_env; import pymysql; c=load_env(); con=pymysql.connect(host=c['DB_HOST'],port=int(c['DB_PORT']),user=c['DB_USER'],password=c['DB_PASSWORD'],database='jkplanning_CTP'); cur=con.cursor(); cur.execute('SELECT LEFT(inventory_date,10), COUNT(*) FROM opening_wip GROUP BY 1 ORDER BY 1 DESC LIMIT 7'); print('old feed      ', cur.fetchall()); con.select_db('JK_CTP_PPDS'); cur.execute('SELECT updated_at, COUNT(*) FROM CTP_Opening_WIP GROUP BY 1 ORDER BY 1 DESC LIMIT 7'); print('CTP_Opening_WIP', cur.fetchall())"
```

The output tells you which WIP dates you can run:

- **old feed** lists one snapshot per date. It holds compounds and green tyres.
- **CTP_Opening_WIP** lists snapshots by time. It holds the full stock, including all components.

`Access denied for user 'shradha'` means the user line in `.env` is missing or misspelled.

---

## 2. Plan files

Put the four plant plan files in one folder. The default folder is `plan-3ct\`.

```
ctp_curing_schedule_<Month>_2026_pcr*.xlsx
ctp_curing_schedule_<Month>_2026_tbr*.xlsx
ctp_building_schedule_<Month>_2026_pcr*.xlsx
ctp_building_schedule_<Month>_2026_tbr*.xlsx
```

The folder must hold exactly one file of each kind, and each file needs a `Shift Schedule` sheet. A file can cover the whole month: the run keeps only the rows inside the plan window.

---

## 3. Run it

### v15, as delivered: WIP 03-Oct-2026 07:00, 10-day plan

```powershell
python run_v15.py
```

This is the same as:

```powershell
python run_v15.py --wip-date 2026-10-03 --wip-time 07:00 --days 10 --plan-dir plan-3ct --name v15
```

### Change the WIP date and time

The **WIP time is also the plan start**: the schedule starts at the moment the stock was counted.

| What you want | Command |
|---|---|
| WIP of 05-Oct 07:00, 10-day plan, saved as v17 | `python run_v15.py --wip-date 2026-10-05 --wip-time 07:00 --name v17` |
| WIP of 05-Oct 07:00, plan up to 12-Oct 07:00 | `python run_v15.py --wip-date 2026-10-05 --plan-end "2026-10-12 07:00" --name v17` |
| WIP of 05-Oct 07:00, 7-day plan | `python run_v15.py --wip-date 2026-10-05 --days 7 --name v17` |
| WIP of 05-Oct 15:00, start of shift B | `python run_v15.py --wip-date 2026-10-05 --wip-time 15:00 --name v17_15h` |
| Plan to the end of the month | `python run_v15.py --wip-date 2026-10-03 --plan-end "2026-11-01 07:00" --name v15_month` |
| New plan files in another folder | `python run_v15.py --wip-date 2026-11-02 --plan-dir C:\plans\nov --name v17` |
| No DB access: reuse the last downloaded tables | `python run_v15.py --wip-date 2026-10-03 --offline --name v15_offline` |

**Always give a new `--name`.** The same name overwrites that version's folder, so earlier versions such as v15 stay untouched only if you change it.

### All options

| Option | Default | Meaning |
|---|---|---|
| `--wip-date` | `2026-10-03` | Opening-WIP day, `YYYY-MM-DD` |
| `--wip-time` | `07:00` | WIP time and plan start, `HH:MM`. Use a shift start (07:00, 15:00 or 23:00) so whole plan rows are kept. |
| `--days` | `10` | Plan length when `--plan-end` is not given |
| `--plan-end` | start + `--days` | Plan end, `"YYYY-MM-DD HH:MM"` |
| `--plan-dir` | `plan-3ct` | Folder with the 4 plan files |
| `--name` | `v15` | Output folder: `latest_schedule\<name>\` |
| `--offline` | off | Don't contact the DB; use `db_pipeline\raw\*.csv` |
| `--wip-source` | `old` | `old` = all stock from opening_wip of the WIP date; `mix` = components from CTP_Opening_WIP (section 4) |
| `--no-wip-mix` | off | `mix` only: no fall-back to an earlier CTP_Opening_WIP snapshot |

### Save the console log

```powershell
python run_v15.py --wip-date 2026-10-05 --name v17 *> latest_schedule\v17_run_log.txt
```

---

## 4. How the opening WIP is chosen

**Default (`--wip-source old`, since v16): ALL stock comes from `jkplanning_CTP.opening_wip` of `--wip-date`.** This is the daily snapshot of the 07:00 stock. If that date has no snapshot, the latest earlier date is used.

To make it usable:

- **Rows with no item type are dropped.** Their item codes are corrupted; about 21k a day, mostly compound batch records.
- **Compounds:** each row is 1 batch, counted as 1 × the compound's routing batch weight, in kg.
- **Components with a blank unit** get the item's BOM unit (M / NOS / KG). Tread comes out in metres.
- **Not in this feed:** BELT-1..4, NYLON, STEEL CHIPPER, BELT EDGE FILLER, BODYPLY, SQUEEGEE, APEXED BEAD and PRE ASSEMBLY. They start at zero stock.

**Alternative (`--wip-source mix`, how v15 ran):**

- Components come from the latest `JK_CTP_PPDS.CTP_Opening_WIP` snapshot at or before `--wip-time`. This feed has the full component list, but **it stopped updating on 02-Oct 06:30**.
- Compounds and green tyres come from the old feed of `--wip-date`.

Check what is available before choosing a date: the command in section 1 lists both feeds' latest snapshots.

The log shows exactly what was used:

```powershell
Select-String -Path latest_schedule\v17_run_log.txt -Pattern "PLAN WINDOW|opening_wip snapshots|WIP OLD FEED|WIP MIX|WIP SOURCE|COMPOUND STOCK"
```

The stock actually loaded is saved in `ctp_inputfiles\opening_wip_live.csv` and `latest_schedule\<name>\run_output\opening_wip_used.csv`.

---

## 5. What the run does, step by step

| # | Step | What it does |
|---|---|---|
| 1 | `build_sep_inputs.py` | Reads the 4 plan files and keeps the plan window. Reads from the **live DB**: BOM, routing, building allowable, cycle times, opening WIP, GT code map, changeover matrix, MPQ and downtime (PM). Every table read is also saved to `db_pipeline\raw\` for offline reruns. |
| 2 | `adapt_inputs.py` | Turns these into the scheduler inputs in `inputs\` |
| 3 | `apply_biascutter_calibration.py`, `apply_routing_gapfill.py`, `apply_ctb_approximations.py` | Routing corrections, the same as v14 |
| 4 | `run_scheduler.py --offline` | Phases 1 → 6: demand, netting, lots, DAG, CPM, placement, curing. Rules come from `config.yaml`. |
| 5 | `capacity_wall.py` | Machine load report |
| 6 | `audit_business_rules.py` | Rule compliance (`BUSINESS_RULE_AUDIT.csv`) |
| 7 | `make_schedule_v18.py` | The Excel workbook, with strict lot_id and consumption_id |

The rules are fixed by `config.yaml` and the runner. **Nothing changes between v14 and v15 except the WIP and the plan.** The runner sets these environment variables itself, so you never need to set them by hand:

```
CTP_AGING_SOURCE=comb        aging from aging_comb.csv only (v14 rule)
CTP_BOM_GATE=warn            combined PCR+TBR BOM check
CTP_PLAN_DIR / CTP_PLAN_START / CTP_PLAN_END
CTP_WIP_DATE / CTP_WIP_CUTOFF / CTP_WIP_MIX
CTP_EXPECTED_PLAN_MONTH      month of the plan start (guards against a wrong-month plan)
```

---

## 6. Outputs

```
latest_schedule\<name>\
  CTP_<DDMMM>-<DDMMM>_SCHEDULE_<name>.xlsx    the schedule (v15: CTP_03OCT-13OCT_SCHEDULE_v15.xlsx)
  run_output\
    phase5_floor_schedule_updated.csv         every machine row: mixing, upstream, building
    phase6_curing_revision_updated.csv        curing blocks
    DAILY_REPORT.xlsx                         plan vs scheduled, day by day, PCR / TBR
    BUSINESS_RULE_AUDIT.csv                   rule compliance
    opening_wip_used.csv                      stock used at plan start
    PHASEWISE\                                each phase's own outputs
```

The workbook has these sheets: Summary, Daily PCR TBR, Curing schedule, Building schedule, Sheet1, Upstream schedule, Assumptions, Rule compliance, Mixing batches, Mixer batch plan, Mixer clean-outs, IL3-IL5 daily plan, Lot genealogy and Genealogy check.

---

## 7. Troubleshooting

| Message | Cause and fix |
|---|---|
| `Access denied for user ...` | Wrong user or password in `db_pipeline\.env` |
| `[db] ... attempt 1/4 failed` then `using SNAPSHOT` | DB not reachable, so the last downloaded tables were used. **The WIP is then not from your date.** Check the VPN or network and rerun. |
| `!! CTP_Opening_WIP has no snapshot <= ...` | No main-feed snapshot and the WIP MIX is off. Only compounds and green tyres are in stock. |
| `need exactly one curing/pcr plan in ...` | The plan folder has 0 or 2+ files of one kind |
| `PLAN-MONTH MISMATCH` | The plan rows don't fall in the month of `--wip-date`. Check `--plan-dir`. |
| `PLAN WINDOW ... 0 of N rows kept` | The WIP date is outside the plan files' dates |
| `UnicodeEncodeError` | Run `$env:PYTHONUTF8 = "1"` once in the PowerShell window, then rerun |
