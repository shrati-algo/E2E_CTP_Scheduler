# Running the CTP scheduler, step by step

You need: **Python 3.11+**, network access to the plant database (MySQL, read-only), and a plan folder.

## 1. Get the code

```bash
git clone https://github.com/shrati-algo/E2E_CTP_Scheduler.git
cd E2E_CTP_Scheduler
git checkout v46k-plan-folder-pipeline
```

## 2. Install the Python packages (once)

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Linux / Mac:  source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Give the database login (once)

Copy `db.env.example` to `db.env` in the same folder and fill in host, user and password. `db.env` is git-ignored, so it never leaves your machine.

You can set the same names as environment variables instead (`CTP_DB_HOST`, `CTP_DB_PORT`, `CTP_DB_USER`, `CTP_DB_PASSWORD`, `CTP_PPDS_NAME`).

The scheduler only **reads** from `JK_CTP_PPDS`; it never writes.

## 4. Put the plan files in a folder

One folder, with the four plan workbooks. Files are found **by name** (case does not matter):

| File name contains | What | Needed? |
|---|---|---|
| `building` and `pcr` | PCR building plan | yes |
| `building` and `tbr` | TBR building plan | yes |
| `curing` and `pcr` | PCR curing plan | optional (derived if missing) |
| `curing` and `tbr` | TBR curing plan | optional (derived if missing) |

Example: `plans/2026-10-27/`, the plan of 27–31 Oct 2026, is in the repository.

## 5. Run

```bash
python run_pipeline.py --plan-folder "C:/path/to/plan-folder"
```

For example `python run_pipeline.py --plan-folder plans/2026-10-27`.

What happens (about 15 minutes):
1. **Plan start.** The plan start is read from the building plans. 27 Oct 07:00 → **plan day 2026-10-27**.
2. **Stock and green tyres.** Taken from the **shift-A sync (06:30) of the plan day**: `CTP_Opening_WIP` and `CTP_GT_Inventory`.
3. **Master data.** Every master is staged from `JK_CTP_PPDS` into `data/ppds_<plan day>/`:

   | Data | Table |
   |---|---|
   | BOM | `CTP_Master_BOM_v2` |
   | Routing | `CTP_Master_Routing` |
   | Pre-v2 BOM (cut-stock factors only) | `CTP_Master_BOM` |
   | Dip lengths | `CTP_Master_FRC` |
   | Others | ageing, buffer, MPQ, changeover matrix, machine master, allowable builders, holidays, downtime |

   The next run on the same plan day reuses this folder. Use `--restage` to read the database again.
4. **Stock split.** The opening stock is split PCR / TBR by this plan's own demand.
5. **Scheduling and reports.** The scheduler runs for both lines, then all reports.

## 6. Read the results

Results go to `outputs/v46k_<plan day>/`:

| File | What it tells you |
|---|---|
| `VALIDATION_REPORT.xlsx` | **start here**: blocked SKUs, assumptions, schedule checks (all should PASS) |
| `CTP_v46k_<day>_SCHEDULE.xlsx` | the plant schedule book |
| `FULFILMENT_DAYWISE_v15.csv` | built / cured per day, per line |
| `WIP_PRESENCE_VS_CONSUMPTION_<run>.xlsx` | opening stock present vs used, cut / slit conversion check, CAP 66 |
| `DAY1_WIP_VS_BUILDING_<run>.xlsx` | day-1 trolleys: kit from stock, parts made fresh, how long each waits |
| `DATA_STAGED.json` | which stock / GT sync and which tables were used |

The console ends with the totals, for example:
```
PCR  built 59,894 / 61,607 ( 97.2%)   cured 51,020 / 61,034 ( 83.6%)
TBR  built 11,887 / 13,411 ( 88.6%)   cured  9,680 / 13,388 ( 72.3%)
```

**Exit code** 1 is normal on this data: two known machine-coverage findings (the bypassed cutters and slitters are idle by design).

## Options

| Option | Meaning |
|---|---|
| `--run NAME` | name of the output folder (default `v46k_<plan day>`) |
| `--restage` | read the masters from the database again |
| `--data FOLDER` | use an already staged masters folder (no database needed) |
| `--out FOLDER` | output root (default `outputs/`) |
| `--skip-workbooks` | skip the two schedule workbooks (faster) |

## If something stops

| Message | Fix |
|---|---|
| `database credentials missing` | step 3 |
| `CTP_Opening_WIP has no shift-A sync on <day>` | the 06:30 stock of the plan day is not in the database yet; wait for the sync, or check the plan start |
| `no PCR building plan in <folder>` | a file name must contain `building` and `pcr` |
| `the data folder lacks ...` | run again with `--restage` |
