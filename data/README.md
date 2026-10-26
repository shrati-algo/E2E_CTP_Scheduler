# data/ - staged master data (not in git)

`run_pipeline.py --data <folder>` reads a folder of CSV extracts from the plant's
`JK_CTP_PPDS` database (BOM, routing, changeover matrix, opening WIP, GT inventory,
machine register, ageing/buffer/MPQ masters, holidays, downtime, ...).

The v46e reference run used `data/ppds_2026-10-22_bomv2`: the 22 Oct 2026 snapshot
`data-ppds-v38` with the BOM from `CTP_Master_BOM_v2`, the pre-v2 BOM in `reference_bom/`
(used only for the types of the bypassed cut codes and the roll-per-metre factor of cut
stock) and `CTP_Master_FRC.csv` (4-roll calender dip lengths). The v45b run used `data-ppds-v38`
(opening WIP shift A 22 Oct, GT inventory 22 Oct 06:36:03). It is about 60 MB and
plant-confidential, so it is not committed - put it here, e.g. `data/ppds_2026-10-22/`.

## Re-staging from the database
```bash
# credentials come ONLY from environment variables (never commit them)
export CTP_DB_HOST=...  CTP_DB_PORT=3306  CTP_DB_USER=...  CTP_DB_PASSWORD=...
export CTP_DB_NAME=...  CTP_PPDS_NAME=JK_CTP_PPDS
export CTP_STAGE=data/ppds_YYYY-MM-DD
export CTP_WIP_DATE=YYYY-MM-DD CTP_WIP_ASOF="YYYY-MM-DD HH:MM:SS"   # opening WIP snapshot
export CTP_GT_DATE=YYYY-MM-DD  CTP_GT_ASOF="YYYY-MM-DD HH:MM:SS"    # same snapshot for GT stock
cd engine && python refresh_ctp_sql.py --ppds-only --bom-v2 --out ../data/ppds_YYYY-MM-DD
# --bom-v2 (needed by the v46e rules): BOM <- CTP_Master_BOM_v2 verbatim (no de-dup: two
#   rows GT -> one roll are two belts), reference_bom/ <- CTP_Master_BOM, + CTP_Master_FRC
```
