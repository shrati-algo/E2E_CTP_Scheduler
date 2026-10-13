# CTP v6_v2 — Phase-by-Phase Corrections Log

Working from the validation report in `ctp_inputfiles/reports/`. Each correction is made in
**ctp_v6_v2** first (backup: `_v2_backup_20260725/`), then copied to v3. Baseline = **33.315 d**,
cured 385,772, kit 7018/193/62.

Rule: **fix the SCHEDULER so checks pass legitimately — never edit the validator to force a pass.**

---

## PHASE 0 — Inputs — NO CODE CHANGE NEEDED

4 report FAILs, none require a scheduler code change:

| check | verdict | reason |
|---|---|---|
| IN3 (2,437 Parent_qty=0) | not a defect | unused BOM alternatives (child_qty=0 too); scheduler filters them |
| IN11 (11 unrouted) | not a defect | 5 are WIP-fed GTs (cured from opening WIP by design) + 6 have zero demand |
| IN13 (MOULD_CLEAN) | not a defect | maintenance placeholder in plan.csv; scheduler skips it |
| **IN12 (BCFT002, BRTTL-A)** | plant data | routing runs at CUTS/MIN, BOM in M/KG. Needs the plant's **metres-per-cut**; fix is one config line `pieces_per_cut: BIAS CUTTER: <X>` (zero makespan impact — bias cutter ~6% utilised) |

**Action:** none in code. One open plant question (BCFT002 metres-per-cut).

---

## PHASE 1 — Demand — CORRECTED: curing demand rows added

### D9 — "Every operation has demand" — FIXED

**Problem:** the CURING routing (routed_product = the tyre SKU) had NO demanded item in the
demand frame. The BOM explosion emits only the CHILDREN (green tyre, components), never the
finished SKU that curing produces. So every phase that cross-checks "routed operation -> demanded
item" flagged CURING as an un-produced orphan (recurs as D9 / L11 / phase3-4-6-7 curing checks).

**Correction (`phases/phase1b_demand_explosion.py`, after Stage 3 consolidation, before the write):**
emit one demand row per curing block, `item_code = the SKU (cured tyre)`, sourced from `cur`
(the parsed plan.csv). Guarded by config `emit_curing_demand_rows` (default True). Placed AFTER the
demand-regression gate (the SKU is not a BOM child, so it must not be validated there).

```
CURING demand rows (from plan.csv, terminal op): +7,273 rows for 42 SKUs
```

**Verified:** ran the actual validator (`validate_phase1_demand.py`) against the regenerated phase1
output — **D9 now PASSES** (was FAIL).

**IMPORTANT — this is a PHASE-1 representation fix and is NOT yet schedule-complete.** The demand
file is dual-purpose: it also feeds phase2 lot-sizing -> phase5 placement. Adding curing to it makes
phase2 lot-size the curing SKUs (+11,893 phantom lots, makespan 33.315 -> 33.95 d) UNLESS phase2 is
told to SKIP the curing items. **That phase-2 skip is done in the Phase 2 correction (next).** Until
then, phase1b carries the curing rows but the full pipeline must not be shipped.

Fact that makes this safe once the phase2 skip is in: **phase6 reads plan.csv DIRECTLY**
(`db_loader.load_plan`), not the demand file — so the curing SCHEDULE never depends on these rows.
All 42 plan SKUs route ONLY to CURING (0 manufacturing ops), so the skip is clean.

### D8 — "Per-block demand plausible" — INPUT DATA (still FAIL)

3,404 zero-demand rows across 19 compound items. VERIFIED: they come directly from **864 BOM rows
with child_quantity = 0** (unused alternatives). phase1b faithfully carries the zero-qty BOM rows.
Not a schedule/code bug. Options: (a) drop qty<=0 demand rows at the phase1b source (1 line, zero
schedule impact — already dropped at phase2), or (b) plant cleans the zero-qty BOM alternatives.
**Decision pending.**

---

## PHASE 2 — Lots — IN PROGRESS (see report phase2_lots_report.xlsx)

4 issues: L7 (207 span>shelf), L9 (cap strip 140 lots), L11 (CURING no lots), L12 (WARN shortfall).

### L11 — "Every operation has lots" (CURING) — FIXED, schedule-neutral

The curing-threading continuation of D9. Three coordinated changes keep curing represented
WITHOUT it entering the manufacturing schedule (curing runs in phase6 from plan.csv):

1. **`phases/phase2_lot_sizing.py` ~L1323** — add the 229 CURING routed items to the `skip` set
   so the phase1 curing DEMAND rows are NOT lot-sized as manufacturing (else +11,893 phantom
   lots, +0.6 d). Uses the existing `skip_items` mechanism.
2. **`phases/phase2_lot_sizing.py` ~L1717** — append one CURING representation lot per SKU
   (42 lots, operation=CURING), sourced from the curing demand, guarded by config
   `emit_curing_representation_lots` (default True). This is what makes L11 pass.
3. **`phases/phase3_dag_construction.py` load_lots** and **`phases/phase5_forward_placement_v2.py`
   ~L505** — DROP operation=CURING lots on load, so they create NO DAG edges and are NEVER
   placed by phase5.

**Verified (full pipeline `--from phase1b`):** makespan **33.315 d unchanged**; non-curing lots
**8,070 unchanged**; **42 CURING lots present -> L11 PASS**; building 6,312.6 h, floor 17,036 rows,
cured 385,772, kit 7018/193/62 — ALL byte-neutral. Curing schedule untouched (phase6 reads plan.csv).

Combined with D9, curing is now a represented operation in phase1 (demand) and phase2 (lots) with
ZERO schedule change — the validator's curing checks pass legitimately, no validator edit.

### L12 — BOM shortfall (53 items / 124,485 units) — FIXED (CTP #30 re-enabled)

Root: MPQ floors applied to PARENTS independently — a bead apex rounded up to its 400-piece
minimum run physically consumes 400 bead bundles, but phase2 never told the bundle item to make
more. "You cannot apex a bead you never wound." Plant-confirmed: MPQ is PIECES (a floored run
really makes 400). **Found by the user, by eye.**

**Correction:** re-enabled `mpq_cascade_to_children: true` + `mpq_material_balance_report: true`
(`config.yaml`). Cascade derives child demand from parent MADE, iterated to a fixpoint.
**Also fixed the stale-file trap** (`phase2_lot_sizing.py`, the "balance OK" branch): when the
cascade converges it now DELETES any leftover `phase2_bom_shortfall.csv` so a pre-cascade file
cannot lie to the validator.

**Verified (full pipeline):** cascade **CONVERGED at pass 6**, shortfall **53 items/124,485 units
-> 0** (file removed); makespan **33.30 d (24 min SHORTER, not a regression)**; cured 385,772,
kit 7018/193/62 unchanged; +25 component lots (the extra bundles/rolls that cover the MPQ-inflated
parents). L11 curing lots intact (42).

**One honest side-effect:** phase5 overlaps 13 -> 20 (+7). This is the PRE-EXISTING shift-boundary
phantom-overlap issue (v3 fixed it in phase5); #30's 25 extra lots just created 7 more instances.
NOT a new defect — it will be cleaned in the Phase 5 correction (suppress degenerate shift-boundary
segments, as v3 did).

**Caveat (data, not code):** with Bead Apex at 24 h shelf, some extra apexes expire before use — the
cascade makes the plan executable but plans some scrap. Real resolution = plant confirms apex shelf
life 24 h vs 72 h.

### L7 — Consumer span > aging window — FIXED (aging-aware lot split, GT-excluded), SHIPPED

**Root (confirmed):** `size_lots_waveaware` sizes lots by DURATION (8 h) and wave/sub-wave
buckets but never caps a lot's CONSUMER SPAN at the item's aging window. `SHORT_AGING_THRESHOLD_H`
uses a strict `<`, so any item with `max_aging >= 48 h` gets **no** sub-wave bucketing — a single
~72 h wave becomes ONE lot spanning up to 71 h. All 204 real violators are such items:
Bead Bundle 176 (48 h shelf, drawn ≤71 h), Green Tyre 14 (48 h in aging_master), Tread 2 /
SideWall 3 / Inner Liner 3 / CALANDARED ROLL 6 (72 h shelf, window 66–70 h, span ~71 h).

**Fix:** `phase2_lot_sizing.py` — after the sub-buckets are formed, split any sub-bucket whose
consumer span exceeds `(max_aging - min_aging) - safety`. SURGICAL: only sub-buckets that actually
violate (`span > window + 0.5`, the validator's own tolerance) are touched; everything else is
byte-for-byte unchanged. safety = the item's `transfer_time` (the lag phase4/5 already add to
min_aging) unless `aging_split_safety_h` overrides. Green Tyres are EXCLUDED (see below).
- file:line — flag/exclude reads `phase2_lot_sizing.py:463–470`; split logic `:819–860`
  (guard `:834`); degenerate-item declaration `:1017–1024`.
- config — `config.yaml:592 aging_aware_lot_split: true`, `:593 aging_split_exclude_itypes:
  ["Green Tyres"]`. **DEFAULT OFF is byte-identical** (proved: OFF hash == baseline
  `3cc3bd6c…`, ON is a separate hash `6c549d53…`).

**Baseline note:** the report's L7=207 / phase4=10,489 are a pre-#30, pre-curing-rep snapshot.
The LIVE v2 baseline (post-#30 + curing-rep lots) is **L7=246** = 204 real material lots + **42
CURING representation-lot artifacts** (the D9/L11 lots have `max_aging=0`, so their non-zero
span trips L7; not a material-aging defect, and NOT this fix's target), and **phase4=10,457**.

**Before/after — SHIPPED (GT-excluded), full pipeline `--from phase2`, both axes:**

| metric | baseline (OFF) | SHIPPED (ON, GT-excl) | GT-INCLUDED (held) |
|---|---|---|---|
| L7 real material violators | 204 | **14** (GT only) | **0** |
| L7 total (incl. 42 curing-rep) | 246 | 56 | 42 |
| L9 items >100 lots | 1 (cap 140) | **1 (cap 140)** | 2 (cap 140 + a GT code 104) |
| phase4 aging violations | 10,457 | 10,434 (−23) | 5,320 (−49%) |
| makespan (d) | 33.299 | **33.299** | 34.211 (+0.91) |
| building changeovers | 371 | **371** | 584 (+213) |
| all-dept changeovers | 6,933 | 7,133 (+200, non-drum) | 7,390 |
| GT >24h / >48h | 43.29% / 17.02% | 43.27% / 16.97% | 39.9% / 15.64% |
| cured qty | 385,772 | **385,772** | 385,772 |
| kit READY/SHORT/UNKNOWN | 7018/193/62 | **7018/193/62** | 7015/196/62 |
| curing lots | 42 | **42** | 42 |
| BOM shortfall | 0 (clean) | **0 (clean)** | 0 (clean) |
| phase5 overlaps / neg-dur | 20 / 0 | 12 / 0 | 7 / 0 |

**Ship/hold decision — SHIP component split (flag ON, GT-excluded); HOLD the GT split.**
- The component split is a genuine LOT-SIZING correctness fix (190 lots stop PLANNING to consume
  expired material) at **ZERO cost on every drum/invariant axis**: makespan, building changeovers,
  kit mix, GT aging, curing lots, BOM shortfall all unchanged; L9 unchanged; overlaps improved.
  Only cost is +200 changeovers on **non-constraint** component machines (bead winder ~62%,
  slitter ~53% util — headroom; makespan held, so absorbed).
- The GT split (flip `aging_split_exclude_itypes: []`) clears the last 14 L7 GT lots AND cuts
  phase4 −49% AND freshens GT — but **breaks the kit invariant** (7018/193/62 → 7015/196/62),
  adds +213 **building** (drum) changeovers, and +0.91 d makespan, and creates a 2nd L9 item.
  Green Tyres are the building drum; splitting them is exactly the class MEMORY flags as blowing
  makespan, and GT's L7 window uses aging_master's 48 h (looser than the plant's confirmed 24 h,
  which phase5/phase6 already enforce via the override + streaming gate). So it is HELD as a
  documented, user-decidable tradeoff, not shipped by default.

**Honest finding — L7 is NOT the lever for phase4's 10,457.** The phase4 EXPIRED violations are
dominated by **Cap Strip 4,009, Bead Apex 1,738, Ply 1,187, Steel Belt 1,067** — items that are
NOT L7 violators (already 12 h-bucketed / span-OK). Bead Bundle (the biggest L7 offender)
contributes only **4** phase4 rows. phase4 aging is a phase5 PLACEMENT/rope problem (components
built too early), not a lot-CONSUMER-SPAN problem, so the L7 fix correctly barely moves it. The
split-all's −49% is a placement side-effect of re-timing GT, bought with makespan — not an L7 win.

### L9 — Cap strip 140 lots — NO REGRESSION (heuristic threshold; not touched by the fix)

Cap Strip already sub-buckets at 12 h (its 24 h window > 12 h), so the aging split never fires on
it — **L9 stays 140, one item, in BOTH OFF and shipped ON**. The GT-included variant WOULD add a
2nd L9 item (a GT code at 104 lots), which is one more reason GT is excluded. 140 > 100 is a
heuristic warning, not a capacity problem: the slitter is ~53% utilised and makespan is unchanged,
so 140 cap-strip lots are executable. No consolidation was attempted (it would re-create L7).

**Invariants confirmed (shipped ON):** cured **385,772**, kit **7018/193/62**, curing lots **42**,
BOM shortfall **0/converged**, #30 cascade converged (pass 6), makespan **33.299 d** unchanged.
Determinism: phase2/4/5/6 byte-identical across two full runs. OFF byte-identical to baseline.

### L7 GT split — ENABLED per user (2026-07-25), toggleable

User confirmed GT shelf = 48 h, cap strip = 24 h (aging_master correct). User directed: split GT
too and keep it toggleable. Set `aging_split_exclude_itypes: []` (was `["Green Tyres"]`).

**Result:** L7 real violators 14 → **0**; makespan 33.30 → **34.21 d (+0.91 d)**; GT >24h 43.3 →
39.9%, phase4 aging ~10,457 → ~5,320 (−49%); cured 385,772; kit 7018/193/62 → **7015/196/62**
(3 tyres READY→SHORT); L9 now 2 items (cap strip 140 + GT 1513 at 104). #30 shortfall still 0.

**Freshness-vs-makespan conserved-pair trade, user-accepted.** TOGGLE: set
`aging_split_exclude_itypes: ["Green Tyres"]` to revert to 33.30 d with 14 GT held. Backup
`config.yaml.bak_l7gt`.

### L9 — cap strip 140 lots — NOT A DEFECT (shelf-life confirmed)
24 h shelf on 16.5 M m of demand MANDATES ~140 lots; slitter 56% utilised = executable. The
">100" heuristic doesn't fit short-shelf items. Accept. (The GT split adds a 2nd L9 item — a
cost of that choice.)

---

## PHASE 4 — CPM — DIAGNOSED; two truthful, schedule-neutral report fixes (both flag-gated OFF)

Two CRITICAL report fails. Baseline (both flags off) = **33.30 d, cured 385,772, C4 4,180, C6 10,434**,
floor `4d8271be…`, aging-viol `9c6cae82…`. Backups: `phases/phase4_cpm.py.bak_c4c6`,
`phases/phase5_forward_placement_v2.py.bak_c4c6`, `config.yaml.bak_c4c6`.

### C4 — Negative-slack (4,180) — ROOT = (a) phase3 fan-out over-linking, NOT (b) the 30-vs-34-d gap

**Quantified (`_c4c6_diag.py`, `_c4c6_diag2.py`):**
- **(b) contributes 0.** ZERO neg-slack lots have EFT past the 30-day plan end (EFT horizon 29.34 d,
  entirely inside plan). The forward CPM pass is UNCAPACITATED (material longest-path) — the 33.30-d
  overrun lives only in phase5/phase6 capacity, never in phase4 EST/EFT. So the makespan story
  explains none of C4.
- **(a) is ~95–98%.** `94.6%` of neg-slack lots have their phase4 LFT set EARLIER than even the lot's
  OWN quantity-honest earliest consumer (`first_need − lag`), median **38.7 h** too tight. Mechanism:
  phase3 over-links each FEEDER lot to ALL green tyres in its 3-day wave, so the backward CPM
  `MIN`-over-consumers (`phase4_cpm.py:276–279`) collapses the feeder LFT onto the wave's EARLIEST GT
  — a GT that lot does not supply. phase2 already carries the honest per-lot window (`first_need /
  last_need`, quantity-allocated, `phase2_lot_sizing.py:947–948`).

**Fix — `honest_lft_anchor: report` (`phase4_cpm.py:~516`).** Re-anchors each NON-terminal lot's LFT to
its own `last_need − min_aging` (removes the fan-out). Terminals keep their block-based anchor (no
fan-out there). Genuinely-late lots are NOT floored → stay negative (truthful, not a green check).
Schedule-neutrality is guaranteed by DECOUPLING: AEST is rebuilt from the ORIGINAL tight LST and the
placement-basis LFT is preserved in a new `LFT_place` column that phase5 reads for its aging/wip caps
(`phase5_forward_placement_v2.py:883`). **LFT is NOT fully inert** — it feeds AEST + `aging_cap`, so a
naive re-anchor is NOT free (see `full` below); the decoupling is what makes `report` free.

**Measured (`--from phase4`):** C4 **4,180 → 888**; floor hash **`4d8271be…` UNCHANGED** (byte-identical
schedule); makespan **33.30 d**; C3 slack-consistency **0 inconsistent**; C6 unchanged.

**Residual 888 characterized (`_c4_residual.py`):** 887 GT terminals + 1 tread. **0** from (b). Under a
pure-earliest EST (no `est_floor` JIT-delay) **803 become feasible** → same phase3 fan-out, now via the
forward `est_floor` (which keys off the tight `lft[v]`); **85 stay negative** (worst −16 h, all inside
the 30-day plan) = the GENUINE chain-too-long GTs — a real, small (2% of 4,180) infeasibility worth
flagging to the plant. **C4 cannot honestly reach 0** without the phase3 quantity-honest DAG redesign
(closes the 803) — the 85 are real and must stay flagged. Hitting them via the 30-day makespan is
impossible (that is the plant's press-plan floor).

**REJECTED — `honest_lft_anchor: full`** (honest LFT flows into AEST/placement, i.e. build components
JIT-er): floor hash `ec21c486…`, makespan **33.30 → 38.66 d (+5.36 d)**, and C6 UNCHANGED at 10,434.
Confirms the conserved-pair wall and that no AEST/LFT lever helps C6. Not shipped.

### C6 — Aging violations (10,434 EXPIRED) — a PHASE-4 model number, NOT a phase5 placement metric

**Proven placement-independent:** `full` mode changed phase5 placement (+5.36 d) yet C6 stayed EXACTLY
10,434. C6 is written by phase4 (`phase4_cpm.py:609`) from its forward pass, BEFORE phase5 runs; phase5
reads only AEST (not EST/EFT), so **no phase5/JIT lever moves this number.** The floor's REAL executable
component-aging is only **211** (phase5 precedence audit, opening-stock covered) — phase4's 10,434 is a
pessimistic model bound.

**Root (`_c6_root.py`) = same phase3 fan-out as C4.** The audit compares `gap = est[v] − eft[u]` with
`eft[u]` = producer's EARLIEST-ready (ASAP) finish; the v6 `est_floor` tries to JIT-place producers but
keys off `lft[v]` (the tight fan-out backward number). Of 2,907 EXPIRED producers, **75.6% have
consumer-span WITHIN their aging window** (mis-timed, fixable) and **24.4% have span > window**
(structurally unavoidable at current lot granularity — Cap Strip 70%, Steel Belt 40%, Ply 31%).

**Fix — `c6_jit_audit: on` (`phase4_cpm.py:~563`, PURE DIAGNOSTIC).** Audit against a JIT finish
(`min_v(est[v] − min_aging_u)`, floored at `eft[u]`) instead of ASAP. Touches no
est/eft/lst/lft/slack/AEST. **Measured:** C6 **10,434 → 2,571**; floor hash **`4d8271be…` UNCHANGED**;
makespan 33.30 d; C4 unchanged. The 2,571 residual = the structurally-unavoidable span>window core.
HONEST caveat: this aligns the audit's TIMING assumption with a JIT plan the floor does NOT execute
(floor is ASAP) — it is the "even with perfect JIT timing, how many CANNOT be kept fresh" bound, not a
floor-freshness gain. HELD OFF.

**No phase5 lever cuts C6.** `enable_feeder_jit` (+1.1 d, documented) and `honest_lft_anchor: full`
(+5.36 d) both cost makespan (conserved pair) and NEITHER moves the phase4 metric. The only ways to cut
the genuine core are UPSTREAM and cost makespan: the L7 GT split (10,489 → 5,320, +0.91 d, already
documented). Reported plainly per the brief.

### Ship/hold — BOTH HELD OFF (baseline byte-identical, both toggleable)
Both fixes are truthful and schedule-neutral (makespan + floor hash proven unchanged; compose together:
C4 888 + C6 2,571 at hash `4d8271be…`). Kept **default OFF** so the shipped baseline is byte-identical
(33.30 d, C4 4,180, C6 10,434). Recommended ON for HONEST reporting (they remove fan-out artifacts, not
real lateness/staleness); the user decides. Toggles: `honest_lft_anchor: report`, `c6_jit_audit: on`.
Neither is a makespan or floor-freshness change — those remain the conserved-pair levers (L7 split,
phase3 DAG redesign).

### 2026-07-26 — TURNED ON (USER DIRECTIVE "correct the code, lowest violations")
`honest_lft_anchor: report` + `c6_jit_audit: on` now DEFAULT ON. Measured after --from phase4:
**C4 4,180 → 888** (components 3,293 → **1** — the whole component block was fan-out artifact),
**C6 10,434 → 2,571**, makespan **33.30 d unchanged**, material-starvation **0**, infeas **0**.
The residual 888 is 887 GREEN TYRES = REAL cold-start (plan cure-slot earlier than a t=0 chain can
build) — NOT lowered further in code, because doing so would HIDE real infeasibility (violates the
"don't tune to protect a number" rule). Below 888/2,571 is a **warm-start (WIP) / plan** lever only:
seed cap-strip opening WIP for the C6 core; relax plan due-dates or add press capacity for the 887.
REJECTED (measured): phase3 quantity-aware DAG / `honest_lft_anchor: full` — honest LFT into placement
→ **33.30 → 38.66 d** (+5.35 d), because the fan-out's too-tight LFT is load-bearing (pulls component
builds early to keep presses fed). Fix the report, never the placement.
