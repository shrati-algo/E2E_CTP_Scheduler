# Version history v45 → v84

Back to [CLAUDE.md](../CLAUDE.md).

**Primary source:** `latest_schedule/BUSINESS_RULES_LOG.md` (lines ~1660–1947). The full text, with every rule figure, is there; this is a condensed, checked digest. Version output folders are in `latest_schedule/vNN/`. Dates are from the log (2026).

**"Cured"** = tyres cured inside the plan window. The windows differ by dataset, so **compare only within a dataset block**:

| Dataset | Versions | Plan / WIP / window | Comparable? |
|---|---|---|---|
| **A** | v42–v49 | 08-Oct live masters, plan `latest_plan_file` (06-Oct), WIP 06-Oct 07:00, window 06–16 Oct | Within A only |
| **B** | v50–v55 | 09-Oct live masters (BOM / routing changed: +7 % compound demand), same plan / WIP | Within B only |
| **C** | v56–v77 | BOM / routing from `BOM_Routing_9oct` files, plan `latest_plan_file`, WIP 06-Oct 07:00, window to 16-Oct | Within C (±4 % noise) |
| **D** | v78–v84 | Plan `plan_files_10oct` (PCR 12 / TBR 13), WIP 10-Oct 06:30 / 07:00 (`--wip-source mix`), window 10-Oct 07:00 → 20-Oct 07:00, plan cure 173,324 | **Current**; within D |

Reference point in A: **v42 = 82,719 cured** (the earlier best before v45).

---

## Summary table

| Version | Date | Dataset | Main change | Cured (PCR / TBR) | Kept? |
|---|---|---|---|---|---|
| v45 | 07-Oct | A | One lot per dip; 901 re-plan after repair | 72,721 | No |
| v46 | 07-Oct | A | Deep roll = max(48 h, max-aging need), full dips | 65,458 (59,119 / 6,339) | No |
| v47 | 08-Oct | A | v42 4RC + mixing rules A / B / C (one mixer, 15 / 24 h gap, final feed) | 65,986 | No (collapse) |
| v48 | 08-Oct | A | + mixing catch-up + 901 ready-first | 26,307 | **No (failed)** |
| v49 | 08-Oct | A | Rule A per day + catch-up + queue unblock | 26,437 | **No (failed)** |
| v50 | 09-Oct | B | Shelf life +30 % (experiment) | 58,957 | No |
| v51 | 09-Oct | B | Control for v50 | 62,247 | — (control) |
| v52 | 09-Oct | B | v42 settings on new masters | 73,155 (65,134 / 8,021) | Reference |
| v53 | 09-Oct | B | Mixing rules at placement + repair compaction | 50,865 | **No (failed)** |
| v54 | 09-Oct | B | Backlog merge (one long lot) | 33,509 | **No (failed)** |
| v55 | 09-Oct | B | Run groups (separate lots back to back) | 49,412 | **No (failed)** |
| **v56** | 09-Oct | C | **Mixing planner (forward simulation, rules built in)** | 104,483 (87,301 / 17,183) | **Yes (architecture change)** |
| v57 | 09-Oct | C | Butyl-sequence fixes | 104,759 | Yes |
| v58 | 09-Oct | C | 4RC creel fill + 4RC compound priority 8 h | 104,695 | Yes |
| v59 | 09-Oct | C | Whole-spool top-up as stock; wire campaigns 5 days | 106,688 | Yes |
| v60 | 09-Oct | C | Load compaction inside 901 loads; B616M priority | 107,314 | Yes |
| v61 | 10-Oct | C | Butyl blocks as the plant runs them (block-level friendly, evening masters) | 105,953 | Yes (plant rule) |
| v62 | 10-Oct | C | Mixing level load (backward capacity) | 107,223 (built 109,487) | Yes |
| v63 | 10-Oct | C | Cover 20 / 30 h + smaller lots + left-shift | PCR 77,929 / TBR 17,923 (plan days) | No |
| v64 | 10-Oct | C | Smaller lots + left-shift | PCR 83,069 / TBR 21,221 | No |
| **v65** | 10-Oct | C | Left-shift (`compress_upstream`) only | **111,969** (93,763 / 18,206) | Yes |
| v66 | 10-Oct | C | Live master priority (starve boost) | 110,352 | No |
| v67 | 10-Oct | C | Butyl clean-out only toward other compounds | 107,505 | Reverted to v65 (user) |
| v68 | 10-Oct | C | 901-first compound timing + fabric bridge 2 days | 109,111 | No |
| v69 | 10-Oct | C | Compaction, batch release, closed loop, alternative bead machine, curing-aware building | 111,814 (92,706 / 19,108) | Yes |
| v70 | 10-Oct | C | PCR bead BR-61, cutter BR-50 / 52 / 56 | 112,368 | Yes |
| v71 | 10-Oct | C | + BR-41 + kit sync | 110,317 | No (kit sync) |
| v72 | 10-Oct | C | v70 + BR-41, kit sync off | 111,332 (built 114,316) | Yes |
| v73 | 10-Oct | C | ASAP extruders / bead + TBR priorities + bead chain | 107,896 (TBR 16,959) | No |
| v74 | 11-Oct | C | TBR bead wound + apexed in one step | 110,423 | Yes |
| v75 | 11-Oct | C | B1144 a day ahead (1802 never waits) | 111,679 (TBR 21,089) | Yes |
| v75_relaxed | 11-Oct | C | **Evaluation:** plant mixing cycle times / batch weights | 118,815 | No (evaluation) |
| v76 | 11-Oct | C | Integration: 4RC demand-only, quantity feedback, genealogy fixes | 114,322 | Yes |
| v77 | 11-Oct | C | Building gated on every input; cap-strip alias; 3 closed-loop rounds | 114,119 | Yes |
| v78 | 11-Oct | D | v77 + BRBC-21 (2 h green tyre) + BRBC-27 (mould-change time) on the 10-Oct plan | 119,124 (100,380 / 18,744) | Yes |
| v79 | 12-Oct | D | Strip items assumed for the first 24 h | 113,040 | Kept as part of v80 |
| v80 | 12-Oct | D | Plan generator's WIP assumptions | 124,413 (102,862 / 21,551) | Yes |
| v81 | 12-Oct | D | Gap fill A1 / A2 + no day-1 pull-forward A4 | 127,021 (105,693 / 21,328) | Yes |
| **v82** | 12-Oct | D | Same-family gap fill | **127,864 (106,348 / 21,516)** | **Yes: current** |
| v83 | 12-Oct | D | Item-stock allocation + opening-stock netting | 127,708 (105,877 / 21,831) | No |
| v84 | 12-Oct | D | 4RC compounds a day ahead + fabric fillers 3 days | 126,196 (104,867 / 21,329) | No |

Other folders:
- `v78_stopped_06oct`: v78 started on the 06-Oct plan and stopped on user request;
- `v16_wipmix`, `exp_mixcap_*`: before v45.

---

## Details and lessons

### v45–v46: 901 levelling (dataset A)
- **Problem:** 901 output uneven.
- **Change:** one lot per dip; 901 list-scheduled on ready times after repair (v45); deep roll by max-aging need (v46).
- **Result:** output fell to 72,721 and then 65,458.
- **Lesson:** 901 is limited by compound supply from the mixers, not by 4RC logic. **Don't reintroduce levelling or the re-plan.**

### v47–v55: the 08-Oct mixing rules enforced by moving lots (datasets A / B)
- **Rules introduced:**
  - A: one mixer per day;
  - B: no repeat within 15 / 24 h, each run covers 15 / 24 h;
  - C: final feed / 230 target.
- **What happened:**
  - Enforcing them after placement (v47), with catch-up (v48 / v49), at placement (v53), as merged lots (v54) or as run groups (v55) all cut output (20–66 k).
  - Cause: most compounds were already late at ~100 % mixer load; every push cascaded downstream, and the repair was later-only.
  - v54 also showed that a merged long lot makes its material available only at the end of the lot.
- **Lesson:** build the rules into planning: v56.

### v50–v51: shelf life +30 % (B)
- Cured −5.3 % on the same data. Not kept. `aging_relax` OFF.

### v56–v57: mixing planner (C), the key architecture change
- `mixer-improvement/mix_planner.py` plans all compound lots first (forward simulation, rules built in); phase 5 never moves them.
- **Result:** 104,483 → 104,759, with every mixing rule at 100 % after the v57 butyl fixes.
- Mixers busy 1,300 h in the window (v52: 950 h).

### v58–v62: 4RC and mixing refinements (C)
- Creel fill moved nothing: no eligible fabric loads.
- Spool netting: +1.9 k.
- Load compaction: +0.6 k.
- Plant-style butyl blocks: −1.4 k, kept as the plant rule.
- Level load: built +2.4 k.

### v63–v65: daily stability tests (C)
- Longer cover runs cost ~5 k PCR; smaller lots ~6 k PCR.
- **Left-shift alone gave v65 = 111,969.**

### v66–v68 (C)
- Master priority (v66) and 901-first timing (v68) only move the shortage.
- v67 showed the **±4 % run-to-run spread**.

### v69–v73 (C)
- **v69:** algorithm changes; total flat, TBR +900.
- **v70:** bead / cutter grouping rules, +0.5 k.
- **v71:** kit sync (urgent-first) conflicts with grouping; off in v72.
- **v72:** BR-41, the best built.
- **v73:** ASAP + bead chain cost TBR −3.2 k.

### v74–v77 (C)
- **v74:** TBR bead in one step, as the plant TDA does. Freed bead time; TBR wait moved to 1802.
- **v75:** B1144 a day ahead: TBR +1.1 k.
- **v75_relaxed:** evaluation only; +7.1 k from plant mixing data.
- **v76:** 4RC demand-only (117 km of unused roll removed); quantity feedback; genealogy fixes (unit of aging, DAG quantities, EXPIRED_STOCK label).
- **v77:** building gated on cap strip / edge gum too. Day 1 PCR collapsed (116 built), so v79 / v80 added the assumptions.

### v78–v82: the new plan (dataset D)
- **v78:** new plan and WIP; BRBC-21 / BRBC-27. 119,124.
- **v79:** strip items assumed for the first 24 h. Day 1 up, total down (113,040).
- **v80:** CTB WIP assumptions. 124,413; dip on 14-Oct.
- **v81:** gap fill A1 / A2, A4. 127,021; the dip moved to 13-Oct.
  - Analysis: `latest_schedule/v80/GAP_12_15_OCT_v80.md`.
  - A3 (time-limited netting) was not needed.
- **v82:** same-family gap fill. **127,864, current best.**

### v83: item-stock allocation (D), not kept
- **Code:** `upstream_gap_fill.repeg_mode: item_stock` plus `_OPEN_NETTED` handling in the gate, BR-12 and the audit.
- **First run collapsed** (53,228). A fully covered lot kept a zero-quantity leftover link, so the gate fell back to strict precedence. Fixed with `if need <= 1e-6 and (lid, ip) in _OPEN_NETTED: continue`.
- **Re-run:** 127,708. Better days 12 / 14 / 15 Oct, worse 16 / 17 Oct. 1801 / 1803 / 1002 family rules worse.
- Kept only as a switch.

### v84: 4RC closer to the plant (D), not kept
- `fourrc_priority_h` 8 → 24; creel fill / load compaction pull 2 → 3 days.
- 126,196.
- No filler candidates exist with the demand-only 4RC; the plant runs ~1.9× our BOM need.
- 4RC unchanged (23.1 km a day).

---

## Analyses done without a version (2026-10-11 / 12)

| Topic | Finding | Location |
|---|---|---|
| Plant vs scheduler | Mixing, 4RC (1 day and 9 days), TDA bead, belt edge filler, master data | `latest_schedule/plant-schedule/*.md` |
| Data issues by section | — | `latest_schedule/v82/DATA_ISSUES_FOR_PLANT_v82.md` |
| Capacity vs target 13.5 k PCR / 3.5 k TBR | Mixing 92–94 % needed, 1802 109 % | `latest_schedule/v82/CAPACITY_vs_TARGET_v82.md` |
| 1802 line speed | Plant ~10 m/min | `latest_schedule/plant-schedule/BEF_1802_LINE_SPEED_ANALYSIS.md` |
| Green-tyre double count, round-up carry-forward | Rejected | [known_issues.md](known_issues.md) |
| End-of-window dip | Backlog; horizon extension not useful | chat analysis 12-Oct |
