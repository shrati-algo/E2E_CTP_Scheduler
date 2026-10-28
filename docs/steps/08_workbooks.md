# Step 8: the schedule workbooks

| Script | Writes | Sheets |
|---|---|---|
| `engine/make_ctp_schedule_workbook.py` | `CTP_<run>_SCHEDULE.xlsx` | Summary, Daily PCR TBR, Curing schedule, Building schedule, Sheet1 (process × machine × day pivot), Upstream schedule, Assumptions, Rule compliance |
| `engine/make_e2e_workbook_v13.py` | `<line>/CTP_E2E_SCHEDULE_<line>.xlsx` | E2E Schedule (every floor row), Sums Check (pivot reconciled with the schedule) |

**The upstream sheets use the shift split from step 6.** The building and curing sheets show one row per trolley or press run; a split trolley keeps the `prod_id` of its first piece.

**Known.** The TBR per-line book is not written when its pivot does not reconcile. On v46k the gap was 680 m of CWBP65, an item placed on a stand-in machine because the routing names none. The run continues.

`--skip-workbooks` leaves this step out, which is faster.
