# Step 6: the shift-wise floor schedule

**Code:** `engine/make_floor_schedule_v10.py --run <run>`.

**Writes:** `outputs/<run>/phase5_floor_schedule_{pcr,tbr}.csv`. One row per machine × lot × shift, in the plant's columns, plus two:

`date, machine, shift, item, item_type, process, department, start_time, end_time, produce_qty, UOM, lot_id, prod_id, consumption_id, FG_SKU_CODE, incomplete_components, lot_qty, shift_part`

**Each lot is booked to every shift it runs in.**
- A lot that runs across 07:00, 15:00 or 23:00 is cut there, and its quantity is split by time.
- Tyres and pieces are whole numbers that add up to the lot. Curing stays in pairs.
- `lot_qty` is the whole lot; `shift_part` says e.g. `2/3`.

Before this change, the whole lot sat in the shift it started in. The 4-roll calender read 8k / 19k / 9k m when it really made 8k / 14k / 14k. `--no-shift-split` gives the old layout.

**Other columns:**
- `consumption_id`: the lots this one consumed (material genealogy). Purchased inputs are named as `PURCHASED_ASSUMED`.
- `incomplete_components`: for a trolley, whether its kit was complete or which part it waited for.

This file feeds steps 7, 8, 11 and 12.
