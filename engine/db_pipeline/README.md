# CTP DB pipeline — `jkplanning_CTP`

Reads the CTP planning database instead of the `ctp_inputfiles/` workbooks.

```
jkplanning_CTP  ──▶  adapt_inputs.py  ──▶  inputs/*.csv  ──▶  phases 0…6
   (21 PCR tables)     transforms!         adapted           unchanged
```

## THE ONE RULE — the database flows THROUGH `adapt_inputs`, never around it

`adapt_inputs` is **not** a column renamer. It applies plant-confirmed overrides, item-type
gap fills, UOM conversions and MPQ parsing. Piping raw tables straight to the phases looks
like it works and silently produces a **wrong schedule**. Both failures below were measured,
not theorised:

| Bypass | Raw DB says | adapt_inputs makes it | Consequence |
|---|---|---|---|
| `routing` CAP PLY SLITTER | 60 M/MIN @ eff 0.96 | **396 M/MIN @ 0.78** | slitter 5.4× slow → 4,773 h not 890 h → **makespan 106.00 d instead of 34.78 d** |
| `itemtype_master` | 2,862 rows, **zero** `Green Tyres` | 3,096 rows, **221** green tyres | preflight abort: *"nothing is typed 'Green Tyres'"* |

So `phases_read_db` defaults to **false**: the phases always read `inputs/`.

## Usage

```bash
# 1. credentials (never commit this file)
#    db_pipeline/.env  ->  DB_USER / DB_PASSWORD / DB_HOST / DB_PORT / DB_NAME

# 2. choose the source in config.yaml
data_source: db        # read jkplanning_CTP     ('csv' = ctp_inputfiles/)
db_plan:     false     # keep the JUNE shift sheet (see PLAN below)

# 3. run exactly as before
python adapt_inputs.py     # pulls the DB, applies every adaptation, writes inputs/
python run_pipeline.py

# optional: raw snapshot for provenance (does not touch inputs/)
python db_pipeline/db_extract.py --raw-only
python db_pipeline/db_extract.py --list        # list all 303 tables
```

## Files

| File | Purpose |
|---|---|
| `.env` | credentials. **Keep out of git.** Real env vars override it. |
| `db_connect.py` | the only place a connection is opened; `read_table`, `list_tables`, `columns_of` |
| `table_map.py` | scheduler input key → DB table. Nothing guesses a table name. |
| `adapters.py` | raw table → the schema phases expect (only `mpq` and `plan` need one) |
| `db_extract.py` | raw snapshot + `extract_manifest.json` (row counts, timestamp) |
| `raw/` | untouched per-table CSV snapshot — the provenance trail |

## Table mapping (verified live, MySQL 9.3.0)

| input | table | rows |
|---|---|---|
| bom | `jkt_bom_pcr` | 42,580 |
| routing | `jkt_routing_pcr` | 14,752 |
| itemtype | `jkt_itemType_master_pcr` | 2,862 |
| aging | `jkt_aging_master_pcr` | 2,656 |
| mpq | `jkt_mpq_pcr` | 22 |
| buffer | `jkt_buffer_master_pcr` | 18 |
| inventory | `opening_wip_pcr` | 39,854 |
| changeover | `jkt_changeover_matrix_combined` | 210,984 |
| plan *(opt-in)* | `CuringSchedule_pcr` | 6,732 |

## ⚠️ PLAN is opt-in (`db_plan: false`)

`CuringSchedule_pcr` covers **2026-04-01 → 04-29**; the shipped run uses a **June** shift
sheet. Enabling it **reschedules a different month**. It also has no `CycleTime_min`:
`adapt_plan` derives `runTime/qty` and falls back to `Master_Curing_Design_CycleTime_pcr`,
which is *not* the same number (sheet 24/27/15 vs design 19/22/14 — the plan value carries
load/unload allowance).

## MPQ is corrected on the way in

`jkt_mpq_pcr` is byte-identical to the workbook, **naming faults included**. Since
`build_mpq_map` keys on `itemtype_master.ItemType`, a name that does not match is a
*silently dead* MPQ. `adapters.adapt_mpq` applies the user-confirmed fixes:
`Calendar Roll (spool)`→`CALANDARED ROLL`, `Edge strip`→`Steel Belt Edge Strip`, collapses
duplicate rows (Tread, Edge strip) to the smallest, writes compounds as `"3 Batches"` text,
and drops the 5 unmappable rows (`Edge gum`, `BRTTL`, `Chaffer IL`, `White SW (Quinto)`,
`Calendra Roll (Dipped Cord)`) rather than guessing. 22 rows → 12 applied.

## Available in the DB, not yet wired

`config.yaml` calls some of these "NOT PROVIDED by CTP". **They exist** — see `table_map.EXTRA`:
`jkt_building_cycle_times_pcr` (245), `Master_Building_ChangeoverTime_pcr` (11),
`Master_Building_Allowable_Machines_pcr` (223 — relevant to the single-machine-pinning
makespan problem), `Master_Curing_Allowable_Machines_pcr` (300),
`Master_Curing_Design_CycleTime_pcr` (248), `Master_WC_Master` (290),
`current_running_moulds_pcr` (88), `gt_inventory_manual_pcr` (28).

## Verified

Full 9-phase run on live DB data: **makespan 34.96 d** (file-based 34.78 d). The 0.18 d gap
is the DB being newer — +169 BOM rows, +59 routing rows. Everything else byte-identical.
