# Opening stock in v46i: present vs consumed, and the cut / slit conversion check

**Run:** `e2e_v46i`. Plan `plan-27oct/`, 27–31 Oct.

**Opening stock:** `CTP_Opening_WIP`, 27 Oct shift A, sync 06:31:17.

**Full report:** `outputs/e2e_v46i/WIP_PRESENCE_VS_CONSUMPTION_e2e_v46i.xlsx`, made by `engine/make_wip_presence_report.py --run e2e_v46i`. Its sheets: *Summary*, *By status*, *By item*, *By batch*, *Cut-slit check*, *Not converted*, *CAP 66*, *Green tyres*.

**How it was built:** the report rebuilds the stock pool exactly as phase 2 does:
1. one row per trolley;
2. CAP 66 converted by MHE code;
3. cut / slit stock converted to its child;
4. ageing;
5. FEFO batches.

So every batch matches what the schedule drew. The rebuilt conversion matches the run's `phase2_bypass_stock_conversion.csv` on both lines.

Quantities are in each row's own unit and are never added across units.

---

## 1. Present vs consumed

| Line | Unit | Present | Dead at plan start | Not needed by this plan | Usable | **Consumed** | % of usable | Expired unused | Left |
|---|---|---|---|---|---|---|---|---|---|
| PCR | KG | 583,814 | **418,075** | 10,680 | 165,739 | **138,799** | 84% | 16,260 | 0 |
| PCR | M | 70,868 | 8,364 | 2,975 | 62,504 | **56,152** | 90% | 3,377 | 0 |
| PCR | NOS | 46,737 | 2,867 | 1,481 | 43,870 | **41,070** | 94% | 1,319 | 0 |
| TBR | KG | 448,264 | **348,221** | 0 | 100,043 | **97,346** | 97% | 2,204 | 493 |
| TBR | M | 79,514 | 4,893 | 14,729 | 74,621 | **59,891** | 80% | 0 | 0 |
| TBR | NOS | 5,336 | 960 | 320 | 4,376 | **3,986** | 91% | 71 | 0 |
| PCR | green tyres | 4,806 | | | | **4,772** | 99% | | |
| TBR | green tyres | 1,031 | | | | **1,029** | 100% | | |

- **Most compound stock is already expired when the plan starts.** That is PCR 418,075 kg (2,408 final compound batches) and TBR 348,221 kg (1,653 batches). These batches are about **9 days old (median 213–226 h) against a 72–120 h shelf life**.
  - The 27 Oct feed reports this compound as stock, but it can't be used.
  - The plant should confirm whether it really is on the floor, or whether the feed keeps rows that were consumed long ago.
- **Usable stock is almost all used:** 84–97% of usable stock is consumed. Per item (sheet *By item*):
  - 179 items: all usable stock consumed;
  - 59: not demanded by this plan;
  - 23: no usable stock;
  - 9: demand met, with surplus;
  - **30: stock left over while the item is still made**, because that stock wasn't usable in time for the demands.
  - The largest of the 30: PCR B127 (1,698 kg left), TBR 30G (1,637 kg), PCR EG03 (861 m), TBR B714 (567 kg).
- ⚠️ **544 batches are drawn for a need after their real expiry** (produced time + max ageing): 327 PCR and 197 TBR compound batches, plus a few rolls and beads.
  - The cause: phase 2 restarts the shelf-life clock at the 07:00 plan start ("opening clock"), so stock alive at 07:00 gets its full shelf life from then.
  - This is the open item "WIP expiry from the bell, not the produced time". Fixing it would remove some of this stock.

## 2. Are cutter / slitter components converted into their children? Yes, with three exceptions

The cutters and slitters are bypassed, so stock still in its cut or slit form is turned back into the BOM child it was cut from.

| Line | Cut / slit codes in stock | Batches | Cut / slit stock | → Child stock | Children | Child consumed |
|---|---|---|---|---|---|---|
| PCR | 100 | 304 | 64,765 | 10,216 | 16 | **8,537 (84%)** |
| TBR | 59 | 249 | 54,349 | 9,434 | 11 | **8,176 (87%)** |

- **Biggest children:**
  - PCR: CPJ1218 3,299 (2,906 used), ECOPOLY350 1,625, HTPOLY350 916, EG01 706, CPJ370 702, MONOCHAFER 647 (all used).
  - TBR: CWBP60 1,737, CP56542 1,425, BRTTL 972, CP31151 931, TEG-01 914, CWBHI34 881.
- **Where the factors come from:** every factor comes from the **pre-v2 master BOM** (`data-ppds-v38`), because BOM v2 has no cut-code rows. It's recorded as "conversion BOM (assumed factor)". Each batch is converted on its own row, with its own production time; the transfer time across the cutter is the cut code's minimum ageing.

**Not converted** (sheet *Not converted*):

| Stock | Qty | Why | Effect |
|---|---|---|---|
| TBR chafers `SNCT001` / `SNCT002` / `SNCT003` (59 batches) | 12,432 m | stock in **M**, the BOM gives **M²**, chafer width missing | stays on the cut code; not used. Owner: master data (width) |
| TBR `BCFT002` (2 batches) | 266 m | **split to the wrong line.** It is PCR bias-cut stock (cutter 2001, from BRTTL). No line had demand for it, so the split gave TBR half, and TBR has no such cutter | wasted. **Fix:** `stage_wip_v6` should send a cut code to the line whose routing cuts it |
| Dropped at staging, in neither BOM: blank code (859), `TSW-017` 477, `R-STC005` 296, `L-STC005` 295, `PBP46` 280, `TR5076` 199, `TPA-029` 153, `TR5075` 12 | 2,571 | no BOM row | not usable (see ITEM_CODE_MISMATCHES.md) |

## 3. CAP 66 (converted by MHE code)

| Form (MHE) | Rows | Stock | → Mother roll (`CAP 66`) | Dead at plan start | Consumed |
|---|---|---|---|---|---|
| FAB (mother roll) | 28 | 8,457.1 m | 8,457.1 m | 9 rolls, 2,700.9 m (produced 21–24 Oct, 72 h shelf life) | **19 rolls, 5,756.2 m: every usable roll** |
| STL (slitted, ×0.1 **assumed**) | 1 | 315.0 m | 31.5 m | — | **31.5 m** |
| empty (cap strip, ×0.00647 master BOM) | 1 | 150.9 m | 0.98 m | dead | — |
| **Total** | 30 | | **8,489.6 m** | 2,701.9 m | **5,787.7 m** |

CAP 66 is converted and consumed correctly. All usable CAP 66 stock is used, and 901 makes 2 more CAP 66 lots for the rest.

## What to fix

| # | Fix | Owner |
|---|---|---|
| 1 | Confirm the ~766 t of compound the feed shows at about 9 days old: real stock or stale rows? | stock feed / plant |
| 2 | Measure max ageing from the produced time, not the plan start (544 late draws) | engine |
| 3 | Send each cut code's stock to the line that cuts it (`BCFT002` → PCR) | engine (`stage_wip_v6`) |
| 4 | Chafer width for `SNCT001`–`003` (m² per m) | master data |
| 5 | Conversion factor for CAP 66 STL in the BOM (10 : 1 is assumed) | master data |
