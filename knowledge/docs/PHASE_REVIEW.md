# CTP SCHEDULER — PHASE-BY-PHASE CORRECTION TRACKER

**Goal (user's rule):** the schedule must be **100% CORRECT**. 34-35 days is fine.
**Ground rule:** v6's LOGIC stays the same. We only (A) fix defects, (B) flag policy choices
(default = v6), and (C) add new machinery ONLY when the user explicitly asks.

Baseline for this review = **all (B)/(C) flags at v6-default (OFF)**. So what runs now is
**"v6 logic + the (A) defect fixes only."**

---

## STATUS BOARD

| phase | vs v6 | decision | status |
|---|---|---|---|
| **phase0** validate | BYTE-IDENTICAL | keep as-is | ✅ **LOCKED** |
| **phase1_5** waves | BYTE-IDENTICAL | keep as-is | ✅ **LOCKED** |
| **phase1b** demand | (A) fixes only: BOM SUM #4, demand gate, per-UOM | keep | ✅ **LOCKED** (user cleared) |
| **phase1c** netting | pro-rata KEPT (v6). (A) only: #9, #24, #25 | keep pro-rata | ✅ **LOCKED** (user cleared) |
| **phase2** lot sizing | CTP data: efficiency #1, transfer #2, pieces_per_cut #7; (A): UOM-raise #6 | keep all 4 | ✅ **LOCKED** (user cleared) |
| **phase3** DAG | (A) only: determinism (sorted) #17 | keep | ✅ **LOCKED** (user cleared) |
| **phase4** CPM | #28 converge **REMOVED** entirely. Kept: #2 transfer in min_aging | keep #2, drop #28 | ✅ **LOCKED** (user: "remove this") |
| **phase5** placement | (A) kept: determinism #17, quantity-summing #21, no-immortal #10, never-delete #8, fallback #13. Report-only: #11/#15/#16/#20/#27. CTP data: #3. Flags OFF. **cap 480 -> 648 (CTP: 30d x 24 x 0.90)** | keep A/report/CTP-data; cap=648 | ✅ **LOCKED** (user cleared) |
| **phase6** curing | (A): **F1 chronological kit-pool** (the 21-day fix). Report-only: F2/F3/F4/F6. Flags OFF: F5 seed, F7 reinsert | keep F1 + reports; F5/F7 left OFF | ✅ **LOCKED** (user: option A) |

---

## THE ONE CORRECTNESS PROBLEM STILL OPEN

**74% of tyres are cured from green tyres past their 24 h shelf life.**

- Mechanism: phase5 builds green tyres against the ORIGINAL press times; phase6 slides the presses
  ~48 h; **nobody tells building.** The tyre is built on time, then rots ~42 h on the rack.
- Lives in: the **phase4 → phase5 → phase6** loop (there is no feedback arrow back to building).
- The fix `converge_building_to_press` (#28) has been **REMOVED** at the user's instruction
  (2026-07-15) — from phase4_cpm.py, run_pipeline.py and config.yaml. The 74% over-age is now
  UNSOLVED again; the user will direct a different correction.
- Tested and DEAD (do not re-chase): `resync_chain_mode`, `reinsert_changeover_blocks`,
  cold-start WIP seeding — none move the 74%.

---

## DECISION LOG (append as we go)

- 2026-07-15 — phase0, phase1_5: byte-identical, LOCKED.
- 2026-07-15 — phase1b: (A) defect fixes only, LOCKED.
- 2026-07-15 — phase1c: **pro-rata (v6) kept** — A/B showed chronological gives no correctness gain
  (both 0 neg-gap, both 51% over-age, makespan identical). LOCKED.
- 2026-07-15 — reverted all (B)/(C) flags to v6-default. Backup: `config.yaml.before_revert`.
- 2026-07-15 — phase4: **#28 converge REMOVED** entirely (phase4_cpm.py, run_pipeline.py, config). Kept #2 transfer. LOCKED.
- 2026-07-15 — phase5: kept (A) defects + report-only + #3 CTP changeover. **building_month_cap_h 480 -> 648** (user caught that v6's 31-day base is wrong for CTP's 30-day June; correct = 30 x 24 x 0.90 = 648). OFF flags left inert. LOCKED.
  - NOTE: with all optimisations OFF (pure v6 logic + defect fixes), makespan is now ~41 d. The 32-37 d
    numbers came from the (B)/(C) optimisations we turned off. Makespan-vs-correctness to be resolved
    after the phase review is complete.
- 2026-07-15 — phase6: kept F1 (chronological kit pool, the 21-day defect fix) + report-only
  F2/F3/F4/F6. F5 (seed inventory) and F7 (reinsert changeover) left OFF (flags, v6 behaviour).
  Nothing deleted. LOCKED. **ALL 9 PHASES NOW REVIEWED AND LOCKED.**
