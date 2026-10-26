# Audit issues: where they stand in v46e

How the 15 issues from the scheduler audit stand in the latest version, **e2e_v46e**.

**Result: 6 solved, 7 partly solved, 2 not solved.**

| | |
|---|---|
| Run | `outputs/e2e_v46e/` (rules `config/rules_v46e.yaml`, data `data-ppds-v38-bomv2/`) |
| BOM | `JK_CTP_PPDS.CTP_Master_BOM_v2` |
| Opening stock | `CTP_Opening_WIP`, 22 Oct 2026 shift A (counted 06:30) |
| Cured | **139,418 of 161,670 schedulable tyres = 86.2%** (PCR 115,384, TBR 24,034) |
| Checks | all 21 schedule checks pass |
| Audit baseline | the audit was written on run v38 |

Every "now" figure below was measured on v46e.

---

## Summary

| # | Issue | Status |
|---|---|---|
| 1 | No opening stock used (cold start) | 🟡 Partly solved |
| 2 | Cure plan bigger than press capacity | 🔴 Not solved |
| 3 | 8,808 tyres that have no BOM | 🟡 Partly solved |
| 4 | Building overlaps | 🟢 Solved |
| 5 | Building time not linked to quantity | 🟢 Solved |
| 6 | Mould changes take 0 minutes | 🟢 Solved |
| 7 | Cutting section missing | 🟡 Partly solved |
| 8 | 4-roll calender run as a changeover machine | 🟢 Solved |
| 9 | TBR bead machines unbalanced | 🟢 Solved |
| 10 | Presses and builders idle | 🟡 Partly solved |
| 11 | Green tyre wait unchecked | 🟢 Solved |
| 12 | Low-volume SKUs dropped | 🟡 Partly solved |
| 13 | Compound made 1.56× | 🟡 Partly solved |
| 14 | Audit trail stale | 🟡 Partly solved |
| 15 | No end-of-horizon handling / replanning | 🔴 Not solved |

---

## 🟢 Solved (6)

### 4. Building overlaps
- **Audit:** 284 of 693 lots overlapped another lot on the same machine (721 h double-booked).
- **Now:** **0 overlaps** on building machines and on upstream machines (checks G1, G1b, G1u).
- **How:** each machine is laid out lot after lot from its real free time, with setup between products (v39, v42, v45a).

### 5. Building time not linked to quantity
- **Audit:** correlation between lot size and duration ≈ 0.
- **Now:** correlation **0.92 (PCR) / 1.00 (TBR)**. Every trolley's time = its run time + its setup (check G2).
- **How:** v39 / v42.

### 6. Mould changes take 0 minutes
- **Audit:** 35 of 60 SKU changes on presses had a 0-minute gap.
- **Now:** **0 of 63** real changes are shorter than the required changeover (check G3; median required 450 min).
  - The few 0-minute rows in the curing file are changes *into* the 4 data-blocked SKUs, which never run.
- **How:** v40 (mould-change time from the plan / A05).

### 8. 4-roll calender (901) run as a changeover machine
- **Audit:** 166 lots, 131 h of changeover against 123 h of running. 901 was the last part for 79% of PCR kits and 52% of TBR kits.
- **Now:**
  - **65 lots, 36 h of changeover against 112 h of running.**
  - 901 is the last part for **19% of PCR** and **0% of TBR** trolleys.
- **How:**
  - Campaigns and progressive release (v43).
  - Queueing by the trolley each lot serves (v45b).
  - **Lots in whole dips** from `CTP_Master_FRC`, each run covering up to the shelf window, with a partial dip when a full dip would expire (v46e).
- **Left:** rolls used by both lines (CWBHTCP100, BRTTL) are sized per line, not as one shared dip.

### 9. TBR bead machines unbalanced
- **Audit:** TDA 2403 at 94% while 2404 / 2405 were at 63% / 45%. 102 kits were waiting on apexed beads.
- **Now:**
  - The three machines are evenly loaded: **44 / 48 / 49%**.
  - **14** trolleys have the apexed bead as their last part.
- **How:**
  - Balancing the bead pool (v43).
  - Queueing by trolley need (v44a).
  - **Winding and apexing as one in-line step** at the slower of the two speeds (v46b).

### 11. Green tyre wait unchecked
- **Audit:** 62,148 tyres cured after waiting more than 168 h (max 318 h).
- **Now:** longest wait **73 h (PCR) / 61 h (TBR)**. **0 tyres** wait more than 168 h (check G12).

---

## 🟡 Partly solved (7)

### 1. No opening stock used (cold start)
- **Audit:** no opening stock used.
  - PCR built 11 / 26 / 61% on days 1–3.
  - TBR built 11 / 14 / 38%.
- **Now:** opening stock **is** used: 171,605 PCR and 129,968 TBR stock units.

  | Built on day | 1 | 2 | 3 |
  |---|---|---|---|
  | PCR | **34%** | 75% | 116% |
  | TBR | **26%** | 93% | 118% |

- **Still open:** day 1. Every trolley still needs about 6–7 parts made fresh, and stock covers most of each kit but not all of it.
  - Details: [WIP_AVAILABLE_BUT_NO_BUILDING.md](WIP_AVAILABLE_BUT_NO_BUILDING.md).
  - Planned fixes W10–W14: [WIP_ISSUES_TO_SOLVE.md](WIP_ISSUES_TO_SOLVE.md).

### 3. 8,808 tyres that have no BOM
- **Audit:** 4 SKUs had no BOM (3 TBR = 8,212 tyres; 1 PCR = 596 tyres), and 16 TBR presses were idle.
- **Now:** the same 4 SKUs. They are left out of the final figures and listed in every run's validation report (check G6, data-blocker policy from v39).
- **Still open:** the BOMs are still missing, BOM v2 included, so 16 TBR presses are still idle.
- **Owner:** BOM master data.

### 7. Cutting section missing
- **Audit:** 411 cut codes were treated as bought in and always available.
- **Now:**
  - Cutters and slitters are skipped **on purpose** (your decision). Only the transfer time is applied: the minimum ageing of the codes in between.
  - BOM v2 already leaves them out.
  - **90% of cut stock** is turned back into the mother roll it was cut from (v46a).
- **Still open:**
  - Cutter capacity and lead time are not modelled (by design).
  - The TBR chafers SNCT001–003 can't be converted: their stock is in metres, the BOM gives square metres, and the width is missing (data).

### 10. Presses and builders idle
- **Audit:** presses about 60% busy, builders about 45%.
- **Now:** inside the 10-day horizon:
  - presses **85% (PCR) / 65% (TBR)**;
  - builders **78% / 70%**.
- **Still open:** the TBR press figure includes the 16 presses that belong to data-blocked SKUs (issue 3). Day-1 starvation (issue 1).

### 12. Low-volume SKUs dropped
- **Audit:** 12 PCR SKUs (≈3,400 tyres) got 0% inside the horizon.
- **Now:** **3 PCR SKUs (366 tyres) + 1 TBR SKU (2 tyres)** at 0%.
- **Still open:** no priority rule or penalty for small SKUs yet.

### 13. Compound made 1.56×
- **Audit:** compound made at 1.56× need (+1.51 million kg).
- **Now:** compound made ÷ gross need:
  - **1.11 (PCR) / 0.96 (TBR)**;
  - measured against the need left after opening stock: 1.32 / 1.12.
- **How:** the BOM walk now counts each recipe once (v46d), and BOM v2 removes the double count through the cut codes.
- **Still open:** the 3-batch minimum run and the 5-batch quality buffer per run (BRM-13) still add extra.

### 14. Audit trail stale
- **Audit:** the stock date and PM calendar were out of date; 58 lot IDs were duplicated across lines; the same item had different speeds by line; 101 items ran on a department-median speed.
- **Now:**
  - The compliance text reads the dates from the run (v40).
  - Lot IDs are unique (check G4).
  - **0 of 20** items used by both lines have different speeds. Speed comes only from the routing.
- **Still open (data):**
  - **117 items** still have no speed in the routing and use the department median (assumption A02).
  - **No October PM calendar** exists (it ends 29 Sep).

---

## 🔴 Not solved (2)

### 2. Cure plan bigger than press capacity
- **Audit:** the plan needs 95% of all PCR presses and 115% of the usable TBR presses, 24×7. Lateness climbs to about 95 h.
- **Now:**
  - The plan still needs **95% (PCR)** and **92% of 80 TBR presses = 115% of the 64 usable**.
  - Lateness against the plan now **levels off at about 33 h** instead of climbing.
- **Why it isn't solved:** the plan itself asks for more press time than exists. The engine can only show it.
- **Owner:** planning (a smaller cure plan, or more presses / moulds).

### 15. No end-of-horizon handling or replanning
- **Audit:** 42,268 building and 69,268 curing tyres were placed after the plan end with only a flag.
- **Now:** placed after 1 Nov:
  - **PCR:** 5,787 building / 19,614 curing tyres;
  - **TBR:** 390 building / 11,342 curing tyres.

  They are only flagged (`within_horizon = False`). There is no shift-by-shift replanning.
- **Owner:** engine, still open (v38 issue #15).

---

## What's left, by owner

| Owner | Issues | What is needed |
|---|---|---|
| **Planning** | 2 | a cure plan that fits press capacity |
| **Master data** | 3, 7, 14 | BOMs for the 4 SKUs; chafer width (m² per m); speeds for 117 routing items; October PM calendar |
| **Engine** | 1, 12, 13, 15 | day-1 fixes W10–W14; priority for small SKUs; compound buffer per run; end-of-horizon handling and replanning |
| **By design** | 7 (part) | cutters and slitters skipped, rest time only |

## Where each fix is written up
- [CHANGE_LOG.md](CHANGE_LOG.md): every version v39 → v46e, with what changed and the numbers.
- [FOUR_ROLL_CALENDER_901.md](FOUR_ROLL_CALENDER_901.md): issue 8.
- [TBR_APEXED_BEAD.md](TBR_APEXED_BEAD.md): issue 9.
- [PCR_COLD_START.md](PCR_COLD_START.md), [WIP_AVAILABLE_BUT_NO_BUILDING.md](WIP_AVAILABLE_BUT_NO_BUILDING.md), [WIP_ISSUES_TO_SOLVE.md](WIP_ISSUES_TO_SOLVE.md): issue 1.
- `outputs/e2e_v46e/VALIDATION_REPORT.xlsx`: the checks (G1–G12) and the blocked SKUs.
