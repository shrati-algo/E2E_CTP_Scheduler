# Item codes that don't match across the BOM, routing, opening stock and FRC

**What was checked:** every material code in the five `JK_CTP_PPDS` tables, as staged for run **e2e_v46e**. Each code was compared table against table.

| Table | Source |
|---|---|
| BOM v2 | `CTP_Master_BOM_v2` |
| Master BOM | `CTP_Master_BOM` (23 Oct staging) |
| Routing | `CTP_Master_Routing` |
| Opening stock | `CTP_Opening_WIP`, 22 Oct shift A |
| FRC | `CTP_Master_FRC` |

**Full list:** `outputs/CODE_CONSISTENCY_REPORT.xlsx`, sheets *Spelling variants*, *Stage-suffix variants*, *Coverage gaps*, *Gap summary*. Nothing was changed in any table or in the engine.

**Left out as expected:**
- SKU codes in the routing's curing rows;
- raw-material numbers;
- cut / slit codes. BOM v2 removes the cutters on purpose; 471 such codes are still in the routing and 117 in the stock.

---

## 1. One item, different codes in different tables (stage suffixes): **real problem**

| Item | BOM v2 / master BOM | Opening stock | FRC | Routing |
|---|---|---|---|---|
| Cap strip 66 | `CAP 66` | `CAP 66` (3,853 m) | `CAP 66` | `CAP 66-MOTHERROLL` → `CAP 66- slitted material` → `CAP 66 - CAPSTRIP` |
| Cap strip 30 | `CAP30` | — | `CAP30` | `CAP30-MOTHERROLL` → `CAP30- slitted material` → `CAP30 - CAPSTRIP` |
| Cap strip GB20 | `GBCAP20` | — | `GBCAP20` | `GBCAP20-MOTHERROLL` → `GBCAP20- slitted material` → `GBCAP20 - CAPSTRIP` |

The routing describes cap strip in three stages: the mother roll on the 4-roll calender, then slitting, then the cap strip. The BOM, stock and FRC use **one plain code** that the routing never mentions.

**What this does in v46e (measured):**
- **Cap strip is never calendered.** No `CAP…` lot is scheduled. `CAP 66` has no routing, so the engine treats it as bought in.
- **Its compound B616M (44,478 kg) goes straight into every PCR kit.** This is the "CAP 66 routing gap" (W6 in WIP_ISSUES_TO_SOLVE.md); this check shows its cause.
- **The 3,853 m of `CAP 66` stock is never used.** No demand row asks for `CAP 66`.
- **The FRC dip lengths for CAP 66 / CAP30 / GBCAP20 (3,000–5,000 m) are never used** either.

**Fix (data owner):** agree one code per stage, used by all four tables. For example, the BOM could list `CAP 66-MOTHERROLL` under the tyre, with stock and FRC on the same code. Or the routing could use `CAP 66` for the mother roll.

### 1a. CAP 66 stock in three forms under one code: converted by MHE code (v46h, 27 Oct)

Since v46f the routing and BOM v2 agree on `CAP 66` (the mother roll, made on 901). But the stock feed books **every** CAP 66 row as item code `CAP 66`, whatever form it is in. Only the `mhe_code` column tells the forms apart. From v46h each row is turned into mother-roll metres before netting (`wip.mhe_conversion` in `config/rules_v46h.yaml`):

| MHE code | Form | Code in the routing | 1 m of stock = | Source of the factor |
|---|---|---|---|---|
| `4RC/FAB/nnn` | mother roll, off the 4-roll calender | `CAP 66` | **1 m** mother roll | same item |
| `4RC/STL/nnn` | slitted material / pre-cap strip, off the slitter | `CAP 66-MOTHERROLL` | **0.1 m** mother roll | ⚠️ **ASSUMED** (user, 27 Oct 2026): 10 m slitted = 1 m mother roll |
| empty | 10 mm cap strip, off the cutter | `CAP 66 - CAPSTRIP` | **0.00647 m** mother roll | `CTP_Master_BOM` (same on all 221 SKUs) |

**The conversion-factor gap (data owner):**
- `CAP 66-MOTHERROLL` (the slitted form) has **no row in `CTP_Master_BOM` or `CTP_Master_BOM_v2`**. It appears only in `CTP_Master_Routing`, as the input of cap ply slitter 701 / 702. So no table says how many metres of slitted material come from 1 m of mother roll. The 10 : 1 factor is a stated assumption until the BOM carries it.
- **The names conflict:** the routing calls the slitter output "MOTHERROLL", while both BOMs describe `CAP 66` (the calender output) as "Cap Strip (MOTHER ROLL)".
- **Fix:** add the step `CAP 66-MOTHERROLL` → `CAP 66` with its metre ratio to the BOM, and rename the routing code so "mother roll" means only the calender output.

**Effect by snapshot** (mother-roll metres after conversion):

| Snapshot | FAB | STL (assumed ×0.1) | Cap strip (×0.00647) | Total mother roll |
|---|---|---|---|---|
| 22 Oct A (the October run) | 13 rows, 3,853.5 m | — | — | **3,853.5 m** |
| 26 Oct C | 29 rows, 8,772.1 m | 1 row, 315.0 → 31.5 m | 1 row, 150.9 → 0.98 m | 8,804.6 m |
| 27 Oct A | 28 rows, 8,457.1 m | 1 row, 315.0 → 31.5 m | 1 row, 150.9 → 0.98 m | 8,489.6 m |
| 27 Oct B | 4 rows, 1,224.7 m | — | — | 1,224.7 m |

How much the assumption matters: 31.5 m of mother roll on 27 Oct A, about 0.4% of the CAP 66 stock.

**Staging:** the stock extract now selects `mhe_code` (`refresh_ctp_sql.py`, `stage_wip_ppds.py`). For stock staged before this change, `engine/stage_wip_mhe.py` adds the column, matched row by row on (production_id, sync time). Nothing else in the file changes. Per-row detail: `outputs/<run>/pcr/phase2_mhe_conversion.csv`.

### 1b. TBR chafer SNCT001–003 → pre-cut roll BCFT → BRTTL (v46k): OPEN ISSUE

- **The units differ:** the stock is in **M**, while the BOM states SNCT as a parent in **M²**. v46k takes the width from the BOM itself: SNCT001 0.140, SNCT002 0.100, SNCT003 0.496 m² per m.
- ⚠️ **Open (plant / BOM owner):** in `CTP_Master_BOM`, **SNCT001 lists 3 pre-cut rolls (BCFT001, BCFT002, BCFT005)** on every SKU, and **SNCT003 lists 2 (BCFT001, BCFT003)**. Are these **alternatives** (one roll is used) or **all used together**?
  - Until confirmed, v46k takes them as **alternatives** (the lower figure): SNCT001 → 700 m BRTTL, SNCT003 → 929 m.
  - If all are used together, it is about 2–3× that.
- **Routing gap:** `BCFT002` has a TBR BOM row (→ BRTTL) but no TBR routing row; only BCFT001 / 003 / 004 / 006 are on bias cutter 2001. The feed books its stock as `tyre_type = PCR`, on rack `TBR/BCR/…`.

## 2. Same code, written differently (spaces / spelling)

| FRC code | Code used elsewhere | Handled in v46e? |
|---|---|---|
| `CPJ 120` | `CPJ120` | ✅ the dip lookup ignores spaces |
| `CPJ 1218` | `CPJ1218` | ✅ |
| `ECOPOLY 350` | `ECOPOLY350` | ✅ |
| `MONOCHAFFER` | `MONOCHAFER` (BOM, routing, stock) | ✅ through an alias in the rules (`fourrc_dip.aliases`) |
| `ECOPOLY340` | closest is `ECOPOLY350` | ⚠️ probably a different fabric; confirm |
| `GB121` | closest is `GB1218` | ⚠️ probably a different code; confirm |

Other near-matches the check flagged. These are guesses to confirm:
- **Routing `GT 2166 RAN HPE` / `TRD 2166 RAN HPE`:** the BOM has `…2167 RAN HPE`.
- **Routing `MT571` / `MT571MC`:** the BOM has `MT5717` / `MT5717MC`.
- **BOM `TRD 2528 RAN ATW`:** no routing; nearest is `TRD 2568 RAN AT`.
- **Routing input text `B107 Master`:** the BOM uses `B107`. This is the apex compound for TBR apexed beads.

**Fix:** the FRC master should use the BOM's spelling (no spaces, `MONOCHAFER`).

## 3. Codes in one table but missing from another

| What | Codes | Effect in v46e |
|---|---|---|
| **Stock of steps BOM v2 dropped:** TBR `NYDBCHF` and `SQ-002 / 003 / 005 / 012 / 013 / 018 / 022` | 8 codes in the stock | this stock can't be used (no BOM row asks for it). Same root cause as the missing doubling and squeegee steps in v46c / v46d |
| **Routing has the step, BOM v2 doesn't:** TBR `NYDBCHF`, `NYLON23-02` (doubling), `SQ-001…022` (squeegees), `SP008`, `S45`, `MS44`, `CP 200`, GT 5012 / 5016 / 5041 / 5075 / 5076 / 5086 / 5501 / 5503; PCR `SIL01`, `SUSSW01`, `SWRS57`, `SPB02`, `SPB03`, `SBP01`, `SA16001`, `GT/TRD 1875 ALP` | 18 PCR, 49 TBR | mostly SKUs or items the plan doesn't use. **NYDBCHF and SQ-0xx are real steps that v2 removed** (TBR understated) |
| **BOM v2 makes it, routing has no step:** PCR `SWRS59`, `SWRS64`, `SWRS66`, `TRD 1654 EZO TOU`, `TRD 2528 RAN ATW`, `TRD 2557 HPE KIA`; TBR `TR5032`, `TR5033`, `TR5034`, `TR5046` | 10 | **no effect now:** none of their SKUs is in the October plan. If one is planned later, the engine will treat that part as bought in |
| **FRC only:** `EU034HI`, `EU114`, `EU31151`, `EU500`, `EU56542`, `EU650`, `EU700`, `EUBRTTL` (EU = export versions?), `CP32256`, `CP32272`, `CP55072`, `CPJ114`, `CWBHT40S564`, `CWBHTCP51`, `ECOPOLY 13`, `CAP STRIP 28`, `NYLCHAFFER`, `POLYESTER` | 18 | no effect (no BOM demand) |

---

## Recheck on today's extracts (27 Oct 2026)

**What was extracted:** fresh read-only copies from `JK_CTP_PPDS`, saved in `check_2026-10-27/` (nothing the runs use was overwritten):
- BOM v2;
- master BOM;
- routing;
- **opening stock 27 Oct shift B** (synced 12:26; latest row per trolley: 8,888 PCR + 3,644 TBR rows);
- FRC.

**Report:** `check_2026-10-27/outputs/CODE_CONSISTENCY_REPORT.xlsx`.

**The masters haven't changed.** The spelling variants (3) and stage-suffix groups (4: CAP 66, CAP30, GBCAP20, BRTTL / BRTTL SLT) are exactly as above. Everything new comes from today's stock snapshot:

| Finding (27 Oct stock) | Detail | Effect if this snapshot is used |
|---|---|---|
| **CAP 66 stock has doubled** | 7,626 m (was 3,853 m) | still unusable: no BOM demand row for `CAP 66` |
| **782 stock rows have no `tyre_type`** (14 items) | `BRTTL` 3,923 m, `CWBHTCP100` 1,386 m, `EG02` 1,166 m, `TSW-017` 477 m, `TR5075` / `TR5076` and compounds `B186` (365 rows), `B714`, `B1144`, `B715`, `B333`, `JV06` | **new problem:** stock is split by `tyre_type`, so these rows would reach **neither** line |
| **TBR green tyres in the stock table under descriptive names** | e.g. `GT 5076 - 295/90R20 JDE XF` (162), `GT 5083 - 295/80R22.5 JUM XM` (109), `GT 5095 - 11.00R20 JUC3` (129), plus size-only names like `10.00 R 20 JDE`, `11.00R20JDE++`, `8.25R20JUH5`; 23 codes in all | BOM codes are `GT 5076`, …; the size-only names can't be matched without a lookup. ⚠️ Green-tyre stock normally comes from `CTP_GT_Inventory`; confirm which table should be used |
| **Chipper strips `L-STC005` / `R-STC005`** (≈272 m each) | in neither BOM v2 nor the routing | unusable |
| **PCR ply `PBP46`** (280 m) | not in BOM v2 and not routed; closest is `PBP146` (check) | unusable |
| **TBR `NYDBCHF` 1,081 m, `SQ-002 / 005 / 013 / 018`** (279–1,399 m) | still in stock, still missing from BOM v2 | unusable on BOM v2 (same as 22 Oct) |

More cut / slit codes are in today's stock (105 PCR, 63 TBR, against 74 / 43). They are expected: they're turned into mother roll when the stock is read.

---

## What to fix first

| # | Fix | Owner | Effect |
|---|---|---|---|
| 1 | **One code per cap-strip stage** across BOM, routing, stock and FRC (CAP 66, CAP30, GBCAP20) | master data | cap strip gets scheduled on 901; B616M leaves every PCR kit; 3,853 m of stock becomes usable |
| 2 | Put **NYDBCHF and the SQ squeegees** back in BOM v2 (or confirm they were removed on purpose) | BOM v2 owner | TBR doubling / calendering work and their stock count again |
| 3 | FRC spellings to match the BOM (`CPJ1218`, `ECOPOLY350`, `MONOCHAFER`); confirm `ECOPOLY340`, `GB121` | FRC owner | the engine's alias can be removed |
| 4 | Confirm the near-matches (`…2166` vs `…2167`, `MT571` vs `MT5717`, `B107 Master` vs `B107`) | routing owner | avoids silent "bought in" items when those SKUs are planned |
| 5 | Add routing for the 10 BOM-made items with no step | routing owner | only matters when their SKUs are planned |
| 6 | **Fill `tyre_type` on every `CTP_Opening_WIP` row** (782 empty on 27 Oct shift B) | stock feed owner | BRTTL / CWBHTCP100 / EG02 / compound stock becomes visible to a line |
| 7 | Green tyres in `CTP_Opening_WIP`: use the BOM's GT code (`GT 5076`, not `GT 5076 - 295/90R20 JDE XF` or a size name), or confirm they belong only in `CTP_GT_Inventory` | stock feed owner | green-tyre stock matched correctly |
| 8 | Map `L-STC005` / `R-STC005` and `PBP46` to valid codes | master data | that stock becomes usable |

**How this was produced:** the staged CSVs for v46e (`data-ppds-v38-bomv2/`, and `data-ppds-v38/` for the master BOM) were read and compared. Codes were matched after removing spaces / hyphens / case, then after removing stage suffixes (MOTHERROLL, slitted material, CAPSTRIP, SLT), then by close spelling.

Matches of the last kind are marked "(check)". They are suggestions, not facts.
