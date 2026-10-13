# Known issues, data problems, decisions

Back to [CLAUDE.md](../CLAUDE.md). Status as of 2026-10-12.

## 1. Confirmed bugs / integrity gaps (code)

| # | Symptom | Evidence | Suspected cause | Where | Next step |
|---|---|---|---|---|---|
| B1 | "ledger: 24 blocks short (MUST be 0)" | v82 `run_log.txt`, `[AGING PULL]` line, every round | Aging pull / ledger reverts; reverts listed: GUM STRIP 16, TEG-02 10 | phase 5, aging pull + building echo | Standalone phase-5 run; trace the 24 blocks |
| B2 | "re-cut VERIFY: 1 SKUs starve -- MUST be 0" | v82 log | Post-echo short-shelf re-cut | phase 5 `[BUILDING ECHO]` | Find the SKU, trace |
| B3 | ~576–628 lots "still over max aging" after the aging pull | v82 log | Machine busy / lot spans longer than shelf life | phase 5 `[AGING PULL]` | Quantify which items; shelf-life data (24 h items) |
| B4 | Genealogy shows consumption before production (e.g. `BI-MONO001_L0001` 12-Oct 12:34 ← `MONOCHAFER_L0001` 13-Oct 03:39) | v82 floor schedule `consumption_id` | `consumption_id` comes from links (reservations), while the gate used opening stock. Opening MONOCHAFER also expired ~12-Oct 09:30 by the 72 h master | `phase5`, `strict_links.py` | v83 item-stock mode corrects the links (not kept for output reasons) |
| B5 | 58 green-tyre lots serve more than 1 SKU; kit credited to the first SKU only | v82 log, warning M-1 | Phase-6 adapter | `phase6_curing_revision.py` | Split lot quantity by SKU demand |
| B6 | Misleading log message "THESE ARE THE ONLY GENUINELY DOUBLE-COUNTED TYRES" (6,109) | v82 log, B2 reconciliation | Phase 1c nets 0 green tyres **by design**: the plant building plan already nets green-tyre stock (verified: GT 1844 XPC TML stock 262 / cure 260 / build 0 in the first 24 h) | `phase6_curing_revision.py` `reconcile_wip_with_phase1c` | Reword the message. **Do not net green-tyre WIP** |
| B7 | TARGET-WIP (component buffers) and FEEDER-JIT don't bind ("only the 3 accidental-caps keys bind", "case-mismatch") | v82 log | Config keys in a different letter case | phase 5 | Fix only if buffers are wanted (design item) |
| B8 | `run_v15.py` log saving and `daily_numbers` step added 12-Oct; only smoke-tested | — | — | `run_v15.py` | Check on the next full run |

## 2. Data issues (need the plant)

Full list: `latest_schedule/v82/DATA_ISSUES_FOR_PLANT_v82.md`.

| Area | Issue |
|---|---|
| Compound WIP | Wrong (plant confirmed); 72 % past shelf life by the master; masters 21.9 t only; 132 master codes and 406 components missing from the feed |
| Mixing | Cycle times (57 / 121 pairs >10 % faster in the plant), batch weights (25 pairs), allowed mixers (6), missing compounds 30GWA / MT153X / B1 / B2, recipe "WA", raw-material substitutes, BOM scale errors |
| 4RC | Speeds (CPJ family ~2×), compound : cord ratios, CAP STRIP 120 into CAP 66, spool / dip lengths, creel time, BOM need vs plant output (1.9×) |
| 1802 | 13 m/min routing vs ~10 measured; user mentioned 30 m/min (not seen in plant data); BEF001 5.85–6.15 m per tyre (2 strips?) |
| BOM / routing | BPG01 unit (329,236 m short); CWBP65 no routing (BOM approximated); SKU 1225219015008SXC1E has no building routing (1,082 tyres) |
| Strip line-speed multipliers | EG01 ×15 (450 m/min) etc. unverified |
| Changeover matrix | Missing pairs: 1001 620, 1801 1,678, 1803 54, 1002 3,580; TBR bead 003↔005, 008↔009 |
| Shelf life 24 h of cut components | Belt, ply, apex, body ply, cap strip |
| Plan data | 0.5 % PCR rows on TBMs not allowed; rim-size rule met 9.5 % in the plan itself; TBR mould changes ≤ 3 a day met 30 % in the plan |

## 3. Decisions already made (keep)

| Decision | Date / version |
|---|---|
| No mixer cap; 230 is a target | Standing |
| BOM / routing from `BOM_Routing_9oct` | 09-Oct |
| Shelf life of genealogy report-only; 4RC demand-only (`whole_spool_topup: false`) | v76 |
| Butyl blocks at block level, evening masters (v61); clean-out to `any` (v67 reverted) | — |
| `relaxed_mixing` OFF (evaluation only) | v75_relaxed |
| `aging_relax` OFF | v51 |
| v83 and v84 not kept; config = v82 | 12-Oct |
| No version is called a "baseline" in logs; best = v82 | Standing |

## 4. Approaches that must not be reintroduced without new evidence

See [CLAUDE.md](../CLAUDE.md) §7:
- 901 levelling / re-plan;
- mixing rules by pushing;
- shelf life +30 %;
- starve boost;
- 901-first timing;
- longer cover / smaller lots;
- kit sync;
- ASAP bead chain;
- item-stock + opening netting as default;
- 4RC ahead + fillers;
- green-tyre netting;
- round-up carry-forward;
- horizon extension for output.

## 5. Technical debt / risks

| Risk | Detail |
|---|---|
| `phase5_forward_placement_v2.py` | 13,195 lines; many passes over shared mutable dicts; order-dependent |
| Gate arithmetic | Mirrored in several places (gate, BR-12, audit, echo). Change all together |
| Phase-5 re-allocations | Not written back to `phase3_dag_edges_updated.csv` |
| `inventory.csv` | Rewritten in place |
| No version control and no unit tests | Back up files before patching |
| Old docs | Partly stale (`PHASEWISE_GUIDE.md`, `README.md`) |
| Noise | Run-to-run ±4 %: single-run comparisons under ~3 k cured are not conclusive |
