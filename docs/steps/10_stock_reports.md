# Step 10: stock reports

| Script | Writes | Answers |
|---|---|---|
| `engine/make_bypass_report.py` | `BYPASS_REPORT_<run>.xlsx` | Cut / slit stock converted back to its mother roll; transfer times across the bypassed cutters and slitters |
| `engine/make_wip_presence_report.py` | `WIP_PRESENCE_VS_CONSUMPTION_<run>.xlsx` | For every stock batch: present, used, expired, or not needed. Cut-slit conversion check, CAP 66, green tyres |
| `engine/make_day1_wip_building_report.py` | `DAY1_WIP_VS_BUILDING_<run>.xlsx` | Day-1 trolleys: which kit parts came from stock and which were made fresh, when each fresh part was ready, and how long it held the trolley |

These are report only. A failure here does not stop the run.
