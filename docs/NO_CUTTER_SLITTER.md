# What changes against v133

All v133 settings stay. On top of them there are two changes:

- cutters and slitters are bypassed;
- the BOM is the corrected database BOM.

## 1. Cutters and slitters bypassed

This is the config section `cutter_bypass`, implemented in `engine/build_sep_inputs.py` → `_cutter_bypass`.

**Which items.** Every item routed on one of these operations is taken out (473 items):

| Section | Operations (DB routing) | Machines | Items |
|---|---|---|---|
| `belt_cutter` | BELT CUTTER | 801, 802 | PCR / TBR belts, chippers |
| `ply_cutter_pcr` | PLY CUTTER | 1103, 1104 | PCR plies |
| `body_ply_tbr` | TBR Body ply CUTTER | 2101 | TBR body plies, TBPGUM |
| `bias_cutter` | Bios cutter / BIAS CUTTER | 2001 | BI-MONO, BCFT |
| `chafer_slitter` | Chaffer Slitter | 2401, 2402 | MONOCHAF, SNCT, BRTTL SLT |
| `cap_ply_slitter` | CAP PLY SLITTER | 701, 702 | cap strip |
| `cap_ply_cutter` | CAP PLY CUTTER, SLITTER | 601 | edge strips, wraps, gum strip |

**What happens in the BOM.**

- **Rows re-parented.** The rows of a cut item are moved to its consumer. Database BOM rows are absolute per-tyre quantities per branch, so nothing is re-scaled: the tyre needs exactly the source material the BOM states.
- **Example (GT 2777 BLA HT):**
  - before: GT → B1-PBLT060 (1 piece) → CPJ1218 0.4675 m + EGSL01 → EG01, and the same for belt 2;
  - after: GT → **CPJ1218 0.9168 m** + **EG01 0.598 m**.
- **Chains collapse top-down.** For example IL078 → MONOCHAF80 → BI-MONO001 → MONOCHAFER becomes IL078 → MONOCHAFER.
- **Checked:** per SKU and item, every non-cut quantity per tyre is unchanged by the collapse.

**Routing:** the cutter / slitter rows are dropped, so those machines are not scheduled.

**Cutting / slitting time = transfer time (configurable).**

- Each section's `transfer_min` is added to the routing transfer time of the **source** item (the 4RC roll, edge gum, mother roll), summed along the chain. Compounds are never delayed.
- Examples: CPJ1218 10 + 120 = 130 min; MONOCHAFER 10 + 90 + 60 = 160 min; EG01 10 + 120 + 60 = 190 min.
- The values are **assumed** typical tyre-plant times to cut / slit a batch, let it relax, handle it and move it to the TBM. Change them in `engine/config.yaml`.
- `enabled: false` gives v133 behaviour for this part.

**Opening stock of cut parts** (belts, plies, chafers, strips) is booked as the source material it holds (`apply_ctb_approximations.cutter_bypass_stock`). For example, a belt's metres become its roll metres plus its edge gum.

## 2. Corrected database BOM

`config.yaml` → `master_files.CTP_Master_BOM` = `bom_corrected/CTP_Master_BOM_corrected.csv`. The database itself is never changed.

**How it was made.** The database BOM was checked against the SMART BOM, and only the **high-confidence** corrections were applied:

| Correction | What was wrong |
|---|---|
| Repeated SMART blocks | some SKUs had SMART rows added up several times: GT 1513 XPC1 MSIL × 2, GT 1402 XPC TATA × 8 … |
| Tread | the first compound carried the whole tread weight |
| MONOCHAFER chain | about ¼ of what the parts need |
| Bias-cut chafer pieces | BRTTL / B333 low |
| Ply gum strip | BPG01 / B278 quantities swapped |
| SP003 | kg per tyre × 2 |

**Result of the corrections:**

- 60 of 61 planned tyres balance within 3 %, against 37 before;
- base material in the plan window is 3,466 t (SMART: 3,438 t), against 4,006 t before.

**Not applied:** the edge strip / wrap count (plant to confirm), and items marked for review.

The correction workbook and method are in the main project (`bom_validation/BOM_CORRECTIONS.xlsx`, `bom_corrections.py`). `--bom db` runs on the database BOM as it is.

## 3. Back calculation (`BACK_CALCULATION_<run>.xlsx`)

This is produced after every run by `engine/backcalc/back_calculation.py`.

**What it calculates:**

- TBM plan × database quantity per tyre of each kit item gives the 4RC roll metres, extrudate metres, bead pieces and so on;
- each item's own BOM then gives the compound kg;
- the routing rate gives machine hours.

**SMART comparison.** Every value is compared with SMART's per-tyre value: the SMART green-tyre / carcass rows are walked down, with the weight kept through each cut. Each difference gets a suggested correction.

**Sheets:**

| Sheet | Content |
|---|---|
| Bypass map | each removed cut item, its machine, transfer minutes and the source(s) taken directly |
| Transfer time added | minutes added per source item |
| By item type | DB vs SMART per item type |
| Items (window) | every item with DB / SMART quantity and machine hours |
| Machine load | hours per machine |
| Per SKU back calc | per SKU and item, with the suggestion |
| Stock converted | cut-part stock converted to source items |

## 4. Results

**Same data as the with-cutter runs:**

| Dataset | With cutters (v133 engine, DB BOM) | Without cutters, DB BOM (v1) | **Without cutters, corrected BOM** |
|---|---|---|---|
| plan-12oct, 12-Oct 06:31 WIP (+17:26 correction), 12 → 22-Oct | 132,919 (75.7 %) | 143,991 (82.0 %) | **152,200 (86.7 %)** (v2) |
| plan-130ct, 13-Oct 06:31 WIP, 13 → 23-Oct | 91,222 (64.2 %) | – | **130,366 (91.7 %)** (v3) |

**v3 daily numbers (plan-130ct, cured %):**

| Day | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | Total |
|---|---|---|---|---|---|---|---|---|---|
| PCR | 85.8 | 89.6 | 97.1 | 92.2 | 95.9 | 91.4 | 96.6 | 105.1 | **94.1** |
| TBR | 80.4 | 67.0 | 72.1 | 87.5 | 83.1 | 90.5 | 85.9 | 88.1 | **82.1** |

**Reading these numbers:**

- **What drives the gain.** Removing the cutter step takes away the bias-cutter / slitter / belt-cutter waits between 901 and the TBM. The corrected BOM removes inflated demand (GT 1513, GT 1402) from the shared calenders, extruders and mixers.
- **Two changes at once.** In the right-hand column both changes act together; the middle column (v1) shows the bypass alone.
- **Noise:** about ±4–5 k cured from run to run.
