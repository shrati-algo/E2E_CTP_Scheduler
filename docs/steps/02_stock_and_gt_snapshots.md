# Step 2: the stock (WIP) and green-tyre snapshots of the day

**Code:** `pipeline/stock_snapshot.py`.

## Policy

PPDS takes a snapshot of `CTP_Opening_WIP` and `CTP_GT_Inventory` at about 06:30, 14:30 and 22:30, and keeps all three. The run uses **the shift-A (06:30) snapshot of the plan day**: the floor as the plan's first shift opens.

The 14:30 snapshot already contains shift-A production, which the plan is itself scheduling. Using it would count those tyres twice.

## Gates

| Gate | When it trips | Result |
|---|---|---|
| The day has a shift-A sync | No `shift = 'A'` row for the plan day | **Stop.** Every sync of the last 3 days is listed |
| The sync is complete | Its row count is under 50 % (`--min-snapshot-ratio`) of the usual size. Usual size = the largest of the previous 9 syncs | **Stop.** Using it would treat most of the floor as empty |
| The sync time | Not between 05:30 and 07:30 | **Warning** in the summary |

The gates run separately for the WIP table and for GT PCR and GT TBR.

## When it stops, you choose

Choose one snapshot explicitly:

```bash
python run_pipeline.py --date 2026-10-28 --wip-sync "2026-10-27 23:26:13"
python run_pipeline.py --date 2026-10-28 --gt-sync-pcr "2026-10-28 06:44:15" --gt-sync-tbr "..."
```

An explicit choice is used as given. It is printed, written to `DATA_STAGED.json`, and listed under "Warnings" in `RUN_SUMMARY.md`. **The pipeline never falls back on its own.**

## Seen in the database (28 Oct)

| Sync | Rows | |
|---|---|---|
| 27 Oct A 06:31 | 13,333 | complete |
| 27 Oct B 14:31 | 13,424 | complete |
| 27 Oct C 23:26 | 13,662 | complete |
| **28 Oct A 09:46** | **1,644** | **incomplete, and struck at 09:46, not 06:30. Refused** |
| 28 Oct B 14:31 | 13,463 | complete |

The 25 Oct syncs and the 26 Oct A and B syncs were partial too (1,400–2,000 rows). **This is a feed problem to raise with PPDS.**

GT on 28 Oct: shift A at 06:44, PCR 27 codes, TBR 24 codes. Complete.

## How the chosen snapshots reach the engine

Step 3 stages them with these settings:
- `CTP_WIP_DATE` / `CTP_WIP_ASOF`: the WIP sync's `inventory_date` / `updated_at`;
- `CTP_GT_DATE` / `CTP_GT_ASOF`: the same for GT; TBR is staged separately when its sync differs from PCR's.
