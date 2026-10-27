# Items with no machine or speed in `all_machine_spec` (v46j)

**From v46j on, machines and speeds come only from `JK_CTP_PPDS.CTP_Master_Routing_v2.all_machine_spec`**, one speed per machine. The table was updated 27 Oct 2026 18:43.
- The old routing's `proc_time` is **not** used.
- Where the spec gives no speed, **nothing is assumed**: no department median (A01/A02). The item becomes data blocker **B06**. Every SKU that needs it is `DATA_BLOCKED`, is not scheduled, and is left out of the final figures.

**Full list:** `outputs/e2e_v46j/MACHINE_SPEED_GAPS.xlsx`. Its sheets: *Summary*, *Blocking (plan needs it)*, *Machines dropped-added*, *TBR winding*, *Whole table no spec*, *Unresolved keys*.

---

## 1. Blocking: the 27 Oct plan needs these, and they have no speed ⛔

The routing names a machine for each of these items, but `all_machine_spec` is **empty on every row**.

| Line | Item | v2 department / operation | Machine named in v2 | SKUs blocked | Build tyres blocked |
|---|---|---|---|---|---|
| **TBR** | **`BEF001`** (belt edge filler) | Extruder / BEF Extruder TBR | **1802** | **20 of 20** | **13,411 (all TBR)** |
| TBR | `TPA-002` | Super Assembly | **2201** | 2 | 2,250 |
| TBR | `TPA-003` | Super Assembly | 2201 | 2 | 590 |
| TBR | `TPA-004` | Super Assembly | 2201 | 4 | 2,861 |
| TBR | `TPA-007` | Super Assembly | 2201 | 1 | 167 |
| TBR | `TPA-011` | Super Assembly | 2201 | 1 | 756 |
| TBR | `TPA-016` | Super Assembly | 2201 | 1 | 1,418 |
| TBR | `TPA-018` | Super Assembly | 2201 | 1 | 460 |
| TBR | `TPA-020` | Super Assembly | 2201 | 4 | 3,479 |
| TBR | `TPA-024` | Super Assembly | 2201 | 4 | 1,430 |
| PCR | `SWRS30` (sidewall) | Extruder / Triplex Extruder PCR | 1001 | 1 | 1,000 |
| PCR | `T7110` (final compound) | Mixing / Final Mixer | F270 F (102), K310 F (104) | 1 (same SKU) | (same 1,000) |

**Effect on the 27 Oct plan:**
- **TBR: every SKU is blocked.** All 20 need `BEF001` on 1802, and each also needs a pre-assembly (`TPA-…`) on 2201. **Machines 1802 and 2201 have no speed for any item in the whole table.** So TBR is not scheduled at all: 0 of 18,074 cure-plan tyres are eligible. 3 more TBR SKUs are blocked for having no BOM (B01, unchanged).
- **PCR:** 1 SKU (`1325221417096HRHE0`: 1,000 build / 850 cure tyres) is blocked. The other 30 are scheduled.

**Fix (routing v2 owner):** fill `all_machine_spec` with speeds for:
- **1802**, the BEF extruder (MPM);
- **2201**, the TBR super assembly (MPM, or pieces per minute);
- `SWRS30` on 1001 (MPM);
- `T7110` on F270 F / K310 F (cycle time in seconds per batch, plus batch weight).

With those in place, the next run schedules TBR again with no other change.

## 2. Machines that change

The spec decides which machines an item may run on.

| What | Count (items the plan uses) | Note |
|---|---|---|
| **Final compounds on master mixers 101 / 103 / 105 / 106** | 79 items each | The old rule (`final_on_master_mixers`) let finals run on master mixers. The spec gives finals a cycle time **only on F270 F (102) and K310 F (104)**, so finals now run only there. Master mixers carry no work in v46j (master compound is still taken as available, as before). |
| Finals the spec lists on **one** final mixer only | 102: 7, 104: 26 | e.g. an item the spec gives only on 102 no longer goes to 104 |
| Master compounds | 101: 6, 105: 13, 106: 11 dropped; 103: 4 added | master compounds are not scheduled in this run (assumed available) |
| `EG03` | 1102 → **1101** | the spec names only 1101 (18 MPM); the old routing named 1102 |
| PCR / TBR other | see sheet | 236 PCR + 149 TBR item–machine pairs dropped in all |

## 3. TBR bead winding (feeds the TDA in-line rule)

`TBWB004` and `TBWB008` have no speed. With no winding speed, the apexed bead is timed on its own apexing speed. **No effect in v46j**, because TBR is not scheduled. Fill these before TBR runs again.

## 4. The whole table

**79 routing-v2 items** (building and curing left out) have no spec on some or all rows:

| | Plan uses the item | Plan doesn't use it |
|---|---|---|
| PCR | Extruder 1, Mixing 4 | Bead 4, Calender 6, Extruder 21, Mixing 5 |
| TBR | Bead 1, Extruder 1, Super Assembly 9 | Bead 1, Extruder 9, Super Assembly 17 |

Three of the 4 PCR mixing items the plan uses are **master** compounds. They are not scheduled (taken as available), so they block nothing today. They would block as soon as master mixing is scheduled.

## 5. Machine keys

Every spec key resolves to a machine in `CTP_Machine_Master`:
- **Names in brackets:** `F270 M` → 101, `F270 F` → 102, `F440 M` → 103, `K310 F` → 104, `K430-1 M` → 105, `K430-2 M` → 106.
- **Numbers:** leading zeros are dropped (`0201` → 201).

## How speed is read

| Spec | Engine rate | On the machine the lot is placed on |
|---|---|---|
| `{"value": v, "uom": "MPM"}` | v M/MIN | minutes = quantity ÷ v |
| `{"value": v, "uom": "NOS/MIN"}` | v NOS/MIN | minutes = quantity ÷ v |
| `{"cycle_time": c, "cycle_time_uom": "SEC/BATCH", "batch_weight": w}` | c ÷ 60 MIN/BATCH, batch w kg **of that mixer** | minutes = ⌈kg ÷ w⌉ × c ÷ 60 |

- **Balancing on real speeds:** phase 4b chooses between machines using each machine's own speed. Phase 5 then times each lot at the speed of the machine it lands on (e.g. B127: 102 = 220 kg / 150.26 s, 104 = 240 kg / 130.26 s).
- **Roll multipliers still apply:** the spec gives the single-strand speed, as the routing did (EG02 12 MPM), so the BR-1 roll table still multiplies it (EG02 × 11 = 132 M/MIN).
- **Not used any more:** the plant mixing factor `T638|106 = 1.034` is set to 1.0, because the spec now states each mixer's own cycle. The routing `proc_time` is not used.
