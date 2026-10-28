# Step 6b: production per shift (only what is made inside each shift)

**Code:** `pipeline/shift_production.py`.

It reads the shift-split floor schedule (step 6). A lot that runs across 07:00, 15:00 or 23:00 is already cut there, so **each piece counts only in the shift it was made in**.

| File | Contents |
|---|---|
| `SHIFT_PRODUCTION_<run>.csv` | line × department × UOM × date × shift, quantity made |
| `SHIFT_PRODUCTION_<run>.xlsx` | Building tyres per shift; Curing tyres per shift; Upstream per machine per shift; all rows |

The building and curing tables are also printed on the terminal, at step 6b and again at the end of the run.

- **Shifts:** labelled `27A`, `27B`, `27C`, … from the plan's first shift to its last planned shift.
- **"after" column:** work the schedule places after the plan end, i.e. lots pushed past the horizon. Those are not counted as fulfilled.

**Example: test_runv1 (27 Oct plan), building, tyres made in each shift:**

```
        27A    27B    27C    28A    28B    28C    29A  ...  31C   after
PCR   1,483  2,835  4,101  3,417  4,088  4,644  4,289  ... 3,314  1,219
TBR     139     87    449    949    867    903  1,107  ...   953  1,199
```
