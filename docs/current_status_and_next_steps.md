# Current status and next steps (as of 2026-10-12)

Back to [CLAUDE.md](../CLAUDE.md).

## 1. State

| | |
|---|---|
| **Current config** | `config.yaml` = **v82** (verified: `upstream_gap_fill.repeg_mode: compounds`, `mix_planner.fourrc_priority_h: 8`, `fourrc_rules.creel_fill.pull_days: 2`, `fourrc_rules.load_compact.fill_pull_days: 2`) |
| **Best run** | `latest_schedule/v82/`: cured 127,864 / 173,324 (73.8 %), PCR 106,348 (77.0 %), TBR 21,516 (61.0 %) |
| **Dataset of the best run** | Plan `plan_files_10oct` (PCR 12 / TBR 13); WIP 10-Oct 07:00 (`CTP_Opening_WIP` 06:32 + old feed, `--wip-source mix`); BOM / routing `BOM_Routing_9oct` files; window 10–20 Oct |
| **Last experiments** | v83 (item-stock allocation, 127,708) and v84 (4RC compound ahead + fillers, 126,196): both not kept |

**Code present but switched off:**
- `repeg_mode: item_stock` (v83), with the gate fix `_OPEN_NETTED`;
- `fourrc_due`, `starve_boost`, `kit_sync`, `queue_unblock`, `aging_relax`, `relaxed_mixing`, `mixing_v47` (all its variants), `level_plan`, `level_loading`, `deep_roll`, `replan_after_repair`, `aging_hard_block`.

**New tooling this session (2026-10-12):**
- `daily_numbers.py`;
- `run_v15.py` now writes `latest_schedule/<name>/run_log.txt` and runs `daily_numbers.py`. **Not yet run end to end** (smoke-tested only);
- user guide [RUN_YOUR_OWN_VERSION.md](../RUN_YOUR_OWN_VERSION.md).

**v82 daily numbers** (`latest_schedule/v82/DAILY_NUMBERS_v82.md`):

| Day | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 Oct |
|---|---|---|---|---|---|---|---|---|---|---|
| PCR cured | 12,077 | 9,862 | **4,514** | 8,466 | 10,588 | 12,201 | 12,932 | 12,818 | 11,334 | 11,558 |
| TBR cured | 2,627 | 2,413 | 2,005 | 1,826 | **803** | 2,465 | 2,879 | 2,872 | 2,085 | 1,541 |

## 2. Blockers

All need plant data, not code:

| Blocker | Why it matters | Source |
|---|---|---|
| **Compound opening WIP is wrong** (plant confirmed) | Start-up dip; mixing never catches up | `latest_schedule/v82/DATA_ISSUES_FOR_PLANT_v82.md` §1 |
| **Mixing cycle times / batch weights** | Mixing is the bottleneck; v75_relaxed showed +7.1 k cured | `plant-schedule/PLANT_vs_SCHEDULER_MIXING_CYCLE_TIMES.md` |
| **1802 speed (13 vs ~10 m/min) and BEF001 m per tyre** | Caps TBR at ~3,200 a day; the user mentioned 30 m/min, not seen in plant data | `plant-schedule/BEF_1802_LINE_SPEED_ANALYSIS.md` |
| **24 h shelf life of cut components** | Forces cutting just before building, so no buffer | `DATA_ISSUES_FOR_PLANT_v82.md` §4 |
| **4RC BOM need vs the plant's output (1.9×), 4RC speeds, creel time** | 4RC realism | `plant-schedule/PLANT_vs_v82_4RC_PROCESS.md` |
| **BOM / routing errors** | BPG01 unit (329 k m short), CWBP65 routing, SKU 1225219015008SXC1E without building routing | `DATA_ISSUES_FOR_PLANT_v82.md` §9–10 |

## 3. Pending user decisions

- **4RC rule changes** (from `PLANT_vs_v82_4RC_PROCESS.md`): creel 240 min → plant's ~170 min; a fabric item may run twice a day as a filler. **Not approved.**
- **v83's correct genealogy at a small loss of grouping:** offered (`repeg_mode: item_stock`); the user hasn't chosen it.
- **Rolling start** from the previous day's schedule: proposed, needs the previous schedule as input.

## 4. Recommended next steps

In order. Each one is a new version under the v82 config, with a log entry.

1. **When the plant sends corrected compound WIP:** put it in `master_files` (`CTP_Opening_WIP` and / or `opening_wip`, same columns as the DB), re-run v82 unchanged and compare with `daily_numbers.py vNN v82`. Expected +6 k to +15 k (estimate only; `CAPACITY_vs_TARGET_v82.md` and the chat analysis).
2. **When plant mixing data is confirmed:** update the routing file in `BOM_Routing_9oct`, or a new file in `master_files`; re-run.
3. **Integrity:** drive the "MUST be 0" checks to 0:
   - ledger 24 blocks short;
   - 1 SKU starving;
   - ~600 lots over max aging after the aging pull.

   Investigate in phase 5 (the aging pull and building echo blocks) with a standalone phase-5 run.
4. **Correctness, not output:**
   - genealogy from item stock (v83 code; its cost was extruder grouping);
   - split multi-SKU green-tyre lots (warning M-1).
5. **Design items without new data** (each needs its own test version):
   - a kit-ready-first building order within the BRBC rules;
   - component target buffers (the TARGET-WIP code exists but is off because of a key-case mismatch);
   - a rolling start.
6. **Keep the user's data list current** (`DATA_ISSUES_FOR_PLANT_v82.md`).
