# Data differences: our masters vs the plant schedules

Only **data** differences are listed here, meaning places where a value in our routing, BOM or other masters doesn't match what the plant's own schedules show. Process and practice differences are left out.

## Sources

| | Source |
|---|---|
| **Plant** | One plant day each, 08-Oct: `mixing_schedule.xlsx`, `four_roll_schedule.csv`, `tda_schedule.csv` |
| **Our masters** | v72 inputs: routing and BOM from `BOM_Routing_9oct`; `CTP_Master_FRC`; changeover matrix; item master |
| **Item-level tables** | `PLANT_vs_MASTER_DATA_DIFFERENCES.xlsx` (one row per item / pair) |
| **Mixing checks** | re-run against the v72 masters in `MIXING_PLANT_DATA_CHECKS_v72_masters.xlsx` |

---

## Summary

| Area | Master | Field | Items with a difference |
|---|---|---|---|
| Mixing | Routing | cycle time (`proc_time`) | 59 compound × mixer pairs outside ±10 %: 51 faster in the plant, 8 slower |
| Mixing | Routing | batch weight (`batch_size`) | 25 pairs off by more than 15 % |
| Mixing | Routing | allowed mixers | 6 compounds run on a mixer routing doesn't allow |
| Mixing | Routing + BOM | missing items | 4 compounds have no routing or BOM row |
| Mixing | BOM | recipe | 78 compounds differ |
| 4RC | Routing | line speed | 8 of 8 items differ |
| 4RC | BOM | compound-to-cord ratio | 3 items off by 6–17 % |
| 4RC | BOM | missing input | CAP 66 consumes CAP STRIP 120 |
| 4RC | FRC | spool / dip length | 2 loads didn't end on whole spools / dips |
| 4RC | Item master | item code | 1 (name only) |
| TBR bead | Routing | rate | 9 item × machine pairs |
| TBR bead | Changeover matrix | changeover time | 11 changeovers (6 pairs missing) |
| TBR bead | Machine master | inch per machine | not in our masters |
| TBR bead | BOM | inputs | matches within ±10 % (one item at 89 %) |

---

## 1. Mixing

**Routing: allowed mixers.** 6 compounds ran on mixers that routing doesn't allow:

| Compound | Plant mixer | Routing allows |
|---|---|---|
| T638 | K430-2 | F270 / K310 |
| MA101 | F440 | K430-1 / K430-2 |
| XMT5718, XMT5719 | F440 | K270M / K430-1 |
| S54B | K270M | K430-1 / K430-2 |
| XMT7219 | K430-1 | K270M |

**Routing + BOM: missing items.** 30GWA, MT153X, B1 and B2 were mixed but have **no routing or BOM row**.

**Routing: cycle time.**
- 51 pairs are more than 10 % faster in the plant (median about 87 % of our time).
- 8 pairs are slower: XMS1146 157 vs 100 s, T638 on K430-2, XMT1553, XMT1554RP and T3427.
- The full list is in the xlsx.

**Routing: batch weight** (more than 15 % off):

| Compound (mixer) | Ours (kg) | Plant (kg) |
|---|---|---|
| XMT1554 / XMT1554RP / XMT1553 (F440, K430-2) | 190 | 345 |
| XMT5718 / XMT5719 (F440) | 185 | 335 |
| XMT7219 (K430-1) | 185 | 285 |
| T638 (K430-2) | 225 | 335 |
| XMB604 (K270M) | 360 | 190 |
| S54AR / S54B / MB614 / MS1036 (K270M) | 285–315 | 144–185 |
| MB200 (F440) | 360 | 223 |
| T635 (K310) | 215 | 110 |
| B278 (F270) | 220 | 128 |
| MB185 / MR5 / MR56 / MB603 / XMB98 (K430-1) | 335–375 | 220–310 |
| MB1164 / MT4023 (K430-1), XMS1146 (K430-2) | 295–305 | 350–375 |
| T1464 / T7234 (F270) | 200–210 | 156–162 |

**BOM: recipe differences (78 compounds)**, in four groups:

1. **"WA" in 21 final compounds.**
   - The plant adds an input coded **WA**, which isn't in our BOM.
   - In those batches the master compound is 6–32 % lower than the BOM (e.g. T7220: XMT7219 140 vs 196 kg; T4632: MT4325 143 vs 211 kg).
   - So WA appears to replace part of the master compound. *What is WA?*
2. **Raw-material substitutes.** The plant uses a different raw-material code from the BOM:

   | Plant uses | BOM has | Compounds |
   |---|---|---|
   | 1010RSS300 | 1010010A00 | MB230, MB151G, MB603, MB94 |
   | 1010RS0100 | 1010011A00 | MB183 |
   | 1060X20500 | 1060T30M00 | MT17 |
   | 1011HV3500 | 1011044W00 | MT4333H |

   These are probably approved alternates that the BOM doesn't list.
3. **Different structure.**
   - IL3 consumes IL3 (rework) instead of IL3FC + ML405.
   - S54 consumes S54 with S54AR / S54FC at about half the BOM.
   - B333 uses XMB333 instead of B333ARS.
   - HS106 consumes HS106 and WA.
4. **All ingredients off by the same %.** B350 and MB352 (+83–89 %), and B186 / MB184 / MB185 (−22 to −30 %).
   - The proportions match, so the BOM's quantity per batch (`Parent_qty` against the sum of the ingredients) is scaled differently from the plant's batch.

---

## 2. 4RC (machine 901)

**Routing: line speed** (m/min at efficiency 1, as the scheduler, against the plant's actual):

| Item | Ours | Plant | Plant run time as % of routing time |
|---|---|---|---|
| CPJ1218 | 40 | 65.7 | 61 % |
| CPJ370 | 35 | 70.5 | 50 % |
| ECOPOLY350 | 42 | 59.8 | 70 % |
| HTPOLY350 | 42 | 53.7 | 78 % |
| CP56542 | 28 | 19.7 | 142 % |
| CWBP60 | 27 | 14.3 | 189 % |
| BRTTL | 45 | 18.8 | 239 % |
| CAP 66 | 31 | 12.6 | 245 % |

The slow ones may include stops inside the plant's roll times.

**BOM: compound-to-cord / fabric ratio.** Metres worked out from the compound differ from metres worked out from the cord / fabric:

| Item | Difference |
|---|---|
| CP56542 | −13 % |
| BRTTL | −17 % |
| ECOPOLY350 | −6 % |

**BOM: missing input.** CAP 66 consumes **CAP STRIP 120** (4,415 units), which is not in its BOM.

**FRC master: spool / dip length.**
- CPJ1218 + CPJ370 (wire 1036S34500): the creel changed after 2.34 spools of 12,600 m.
- ECOPOLY350: ran 5.5–5.9 dips of 2,000 m.

These are not whole spools / dips, so either the real lengths differ from our master or the plant doesn't use spools up.

**Item master:** plant "CAP 66" = our "CAP 66-MOTHERROLL".

---

## 3. TBR bead (2403 / 2404 / 2405)

**Routing: rate.**
- Our routing (efficiency 1, as the scheduler): apexing 3 NOS/min and winding 3–4 NOS/min, so **0.58–0.67 min per bead**.
- Plant: **1.6–2.4 beads/min including winding**, so 0.41–0.64 min per bead (61–107 % of our time).
- Per item: TBAPBD008 0.41, 003 / 005 on 2403 0.42–0.43, 006 / 009 0.58–0.64, 2405's items 0.53–0.62.

**Changeover matrix** ("TBR Bead Apexing TDA n"):
- **Missing pairs:** 003 ↔ 005 (20″) and 008 ↔ 009 (22.5″), on every TDA matrix. The plant ran 4 changes on these pairs. 2403's matrix (TDA 1) has no 22.5″ items at all, which fits the plant running 2403 on 20″ only.
- **Where the matrix has a value (20 min die / preformer):** the plant's extra time is mostly **0–6 min**, twice 24–31 min.

**Machine master: inch per machine.**
- Our masters have no inch for the TBR bead machines.
- The plant keeps **22.5″ on 2404** and **20″ on 2403 / 2405**.
- Each bead's inch can be derived from `CTP_Master_BuildingAllowable.size` (`tbr_bead_inch.csv`).

**BOM:** matches. All inputs are within ±10 %, except TBAPBD005's bead wire at 89 %.

---

## Questions for the plant

1. **Mixing.** What is "WA"? Which raw-material substitutes are approved? Real batch weights and cycle times for the pairs above. Routing rows for 30GWA / MT153X / B1 / B2. Whether the 6 compounds may run on the mixers the plant uses.
2. **4RC.** Line speed per item. Real spool / dip lengths. Whether CAP STRIP 120 is a CAP 66 input. BOM for CP56542 / BRTTL / ECOPOLY350.
3. **TBR bead.** Rate per bead with winding included. Changeover times for the missing pairs. Confirm inch per machine (2404 = 22.5″).
