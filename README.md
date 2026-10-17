# JK Tyre CTP end-to-end scheduler: without cutters and slitters (no-cutter-slitter v3)

**What it does:** the plant's curing and building plans (PCR and TBR) become a timed, machine-level floor schedule, from mixing through calendering and extrusion to building and curing.

**What is different:** **cutters and slitters are taken out of the schedule.** The tyre builder (and the inner-liner calender / bead apexer) takes the source material directly: the 4-roll-calender roll, the edge gum and the mother roll. TBM kit demand is back-calculated from the BOM and routing to calenders, extruders and mixing. Every value is compared with the SMART BOM.

```powershell
python run_schedule.py --plans plans\plan-130ct
```

| | |
|---|---|
| **Version** | no-cutter-slitter v3 settings (`engine/config.yaml`) = v133 + cutter / slitter bypass + corrected database BOM |
| **Results** | plan-12oct, 12-Oct WIP: **152,200 / 175,628 cured (86.7 %)**, against v133's 132,919 with cutters.<br>plan-130ct, 13-Oct 06:31 WIP: **130,366 / 142,096 cured (91.7 %)**, against 91,222 with cutters.<br>See [docs/NO_CUTTER_SLITTER.md](docs/NO_CUTTER_SLITTER.md) |
| **Cutting time** | kept as a configurable transfer time per section (`engine/config.yaml` → `cutter_bypass.sections.<section>.transfer_min`). Assumed: belt / ply / body-ply cutters 120 min, bias cutter 90, slitters 60 |
| **BOM** | the database BOM with the high-confidence corrections against SMART (`engine/bom_corrected/`). `--bom db` uses the database BOM as it is |
| **Method** | heuristic, rule-based scheduling (no mathematical solver). Quality is judged by tyres cured and rule compliance |
| **Run time** | about 10–60 minutes, depending on the computer |
| **Database** | read-only. Offline by default (snapshot copy in `engine/db_pipeline/raw`); `--db live` reads the database |

> **This public branch holds code, configuration and documentation only.** The plant plans, the offline database copy, the corrected BOM file, the SMART export and the reference data files are not included (repository rule). To run, add them as listed in [engine/DATA_NOT_IN_REPO.md](engine/DATA_NOT_IN_REPO.md), or use `--db live` (with your own `.env`) and `--bom db`.

## Quick start

1. **Install:** Python 3.11 or newer, then:
   ```powershell
   pip install -r engine\requirements.txt
   ```
2. **Make a plan folder** with exactly one of each workbook: `ctp_curing_schedule_*_pcr*.xlsx`, `ctp_curing_schedule_*_tbr*.xlsx`, `ctp_building_schedule_*_pcr*.xlsx` and `ctp_building_schedule_*_tbr*.xlsx`. Each needs a `Shift Schedule` sheet. Details: [docs/INPUTS.md](docs/INPUTS.md).
3. **Check:**
   ```powershell
   python run_schedule.py --plans my-plan --check-only
   ```
   It shows the files, the window, the WIP snapshot, the transfer times and the BOM in use.
4. **Run:**
   ```powershell
   python run_schedule.py --plans my-plan
   ```
5. **Results** are in `outputs\ncs_my-plan\`:
   - the schedule workbook;
   - `DAILY_NUMBERS_*.md`;
   - **`BACK_CALCULATION_*.xlsx`**: TBM kit → calenders / extruders → mixing, DB vs SMART, machine hours;
   - the rule reports and the run log.

Full guide: [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md).

## Folder layout

```
ctp-scheduler-no-cutter-slitter/
  run_schedule.py          single entry point (plan folder in -> schedule + reports out)
  docs/                    documentation (start with HOW_TO_RUN.md and NO_CUTTER_SLITTER.md)
  plans/                   plan folders (plant plans are not in this public repo; see plans/README.md)
  outputs/<run name>/      results of each run
  engine/                  the scheduler (v133 engine + the no-cutter-slitter changes) -- do not rename files inside
    config.yaml            settings; cutter_bypass section = which sections are bypassed and their transfer minutes
    build_sep_inputs.py    plan files + database -> engine inputs; _cutter_bypass collapses the cut / slit items
    apply_ctb_approximations.py   WIP assumptions; cutter_bypass_stock converts cut-part stock to its source material
    backcalc/              back calculation vs SMART (BACK_CALCULATION_<run>.xlsx)
    bom_corrected/         corrected database BOM (CTP_Master_BOM_corrected.csv)
    reference/             SMART BOM export (for the back calculation)
    Master Data/           plant change-over master (4RC roll widths for the back calculation)
    phases/, mixer-improvement/, *_checks.py, ...   as in v133
    db_pipeline/           read-only database access + raw/ = snapshot copy
```

## Documentation

| Document | Content |
|---|---|
| [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md) | Install, run, every option, live database, reproducing the results, troubleshooting |
| [docs/NO_CUTTER_SLITTER.md](docs/NO_CUTTER_SLITTER.md) | What changes against v133: bypass, transfer time, stock conversion, corrected BOM, back calculation, results |
| [docs/INPUTS.md](docs/INPUTS.md) | Plan files, database tables, opening WIP, reference files |
| [docs/OUTPUTS.md](docs/OUTPUTS.md) | Every output file |
| [docs/PIPELINE.md](docs/PIPELINE.md) | What happens in a run |
| [docs/LIMITS.md](docs/LIMITS.md) | Known limits |
| [docs/reference/](docs/reference/) | Engine reference documents carried over from the project |
