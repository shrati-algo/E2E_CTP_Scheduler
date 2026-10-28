# Step 9: fulfilment and the validation report

| Script | Writes | Contents |
|---|---|---|
| `engine/make_fulfilment_report.py` | `FULFILMENT_DAYWISE_v15.csv`, `FULFILMENT_SHORTFALL_v15.csv` | Built and cured per line and plant day, against the plan. Data-blocked SKUs are excluded and listed. The shortfall is split into lots outside the horizon / no green tyre |
| `engine/make_validation_report.py` | `VALIDATION_REPORT.xlsx`, `VALIDATION_REPORT_blocked.csv` | **Start here after the summary.** Data blockers (what is missing, which table, how many tyres); every assumption with its code and source; the schedule gates G1–G12 (overlaps, setup times, plan order, green-tyre age, …) |

**Policy, as in every run.** A data-blocked SKU is listed with what is missing and taken out of the final figures. Every other gap is either assumed, with the assumption stated, or reported.

A failure in a FATAL gate (G1 / G2 / G6 / G7) stops the run.
