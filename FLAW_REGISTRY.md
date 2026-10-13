# CTP scheduler — flaw registry (multi-expert audit, 2026-07-27)

Four expert agents audited the full codebase (config/plausibility, placement+curing, core math
phase2/3/4, and demand/netting/adapters). Deliberate CTP deviations were excluded by instruction.
Severity: **Critical** (wrong/lost output, silent) · **High** · **Medium** · **Low** (latent/inert).

All four audits are complete (config, placement+curing, core-math phase2/3/4, demand/netting/adapters).

---

## CRITICAL — silent, loses/​fabricates real output

### C-1 · CTP #41 prune deletes the components of 14,065 tyres that have NO green-tyre source
- **Where:** `phase1b_demand_explosion.py:314-342` (`wip_fed_skus` trigger), gate carve-out `:532`; interacts with `adapt_inputs.py:203-214` (`excluded_items`).
- **What:** CTP #41's *existence* is fine, but its **trigger is wrong**. It declares a SKU "WIP-fed → don't build its components" iff every Green-Tyre child is **unrouted** — and never checks that any WIP actually exists. Those GTs are unrouted only because `excluded_items` dropped them (no plant `proc_time`), so the prune does the **opposite** of what `excluded_items` promises. **Verified on the June plan:**

  | Pruned SKU | GT child | cures | GT WIP |
  |---|---|--:|--:|
  | 1325216814085TURL2 | GT 1684 ROYL | 4,638 | 23 |
  | 1325218515088TURL1 | GT 1865 ROYL RENO | 3,414 | 195 |
  | 1325219416089HURL1 | GT 1916 ROYL | 1,616 | 1 |
  | 1325219515091HURL3 | **GT 1965 ROYL** | **3,732** | **0** |
  | 1325222318098HRHE0 | GT 2258 RAN HPE | 900 | 16 |
  | **total** | | **14,300** | **235 (1.6%)** |

  So ~**14,065 tyres** are "cured" with **no green tyre and no components built** — this IS the
  `CURED WITHOUT A GREEN TYRE: 14,251` line we had normalized. The demand-regression gate can't catch
  it (`_built_skus = plan − skip` explicitly excludes these SKUs).
- **Fix:** make "WIP-fed" mean WIP-fed: prune only the portion each SKU's opening **GT WIP actually
  covers** (`cure − GT_WIP`); for the uncovered remainder either keep exploding components (restore
  `excluded_items` behaviour) or raise the loud declared-hole warning. Do **not** infer WIP from absence
  of routing. Separate the two causes (unrouted-because-excluded vs genuinely WIP-covered).

---

### C-2 · Machine 2001 (bias cutter) — the #1 makespan constraint rests on TWO unverified numbers (2026-09-25)
- **Where:** `inputs/routing.csv` (546 `CUTS/MIN` rows, machine 2001) · `config.yaml -> pieces_per_cut: BIAS CUTTER: 2` (BR-3) · reported by `capacity_wall.py`.
- **What:** 2001 is the only HARD WALL in the June run — 886.6 h = **36.9 d of work in a 720 h window (123.1%)**, producing a **12-day late curing tail (52,517 tyres, 11.6% of plan, past 2026-06-30)**. Both inputs behind that number are unverified, and they **pull in opposite directions**:

  | Input | Value | Status |
  |---|---|---|
  | Rate | 7–8 `CUTS/MIN` | **NEVER MEASURED.** `CTP_PCR_Machine_LineSpeed_Verify_CHECKED.xlsx` leaves `PLANT line speed (measured)` **and** `Match` blank for 2001: *"per-piece/cut rate not derivable from lot-level MES (observed lot cycle ~2813s)"* |
  | `pieces_per_cut` | 2 (BR-3) | **RATIONALE DOES NOT FIT THIS MACHINE.** BR-3 says *"cuts the steel-belt roll, makes B1+B2"*, but the plant says 2001 is `Ply roll -> CHAFER`, and the BOM gives parents `MONOCHAF80/120` / `BRTTL SLT` at **1.0, 2.0 and 4.0 NOS/tyre** — no B1/B2 pairing. `BCFT001` (220 BOM rows, the largest group) is demanded in **METRES** on a `CUTS/MIN` machine |

  **Scenario spread — the answer flips completely on which input is corrected:**

  | | 2001 load | Verdict |
  |---|--:|---|
  | As modelled (pre-patch) | 36.9 d | 123% — wall, 12-d tail |
  | Rate calibrated only | 22.2 d | fits window |
  | `pieces_per_cut` 2→1 only | 73.8 d | plan physically impossible |

  The pre-patch 36.9 d is the **net of a too-slow rate and a too-generous pieces-per-cut partially cancelling**. It is not trustworthy as a bottleneck *or* as a clearance — and it was about to justify a parallel-machine capex recommendation.
- **Measured from our own production data:** `inputs/inventory.csv` (= DB `opening_wip_pcr`) carries **350 real bias-cutter events** — the machine is in `production_id` as `BIAS<DDMM><YYYY><HHMMSS>` — over 2026-05-17→06-01: **21.9 events/day, median cycle 48.8 min, 17.8 run-h/day**. Projected over the 30-day window at plan volume: **534 h = 22.2 d (fits)**. Independently corroborates the MES note (2,813 s vs 2,927 s measured). Factor = 886.6/534 = **1.66×**; `8 × 1.66 = 13.3 CUTS/MIN` is coherent beside the BELT CUTTER's 15.
- **Applied (PROVISIONAL):** `apply_biascutter_calibration.py` — ×1.66 on 2001's `CUTS/MIN` rows, preserving the per-item split (7→11.62, 8→13.28). Patches `inputs/routing.csv` because `--offline` skips `adapt_inputs.py` and so never applies `config.yaml` overrides. Reversible: `--revert`.
- **Fix / OPEN — two questions for the plant, which must be answered TOGETHER:**
  1. **How many pieces come off one cut** — for BI-MONO (chafer), and for `BCFT003`/`BCFT006` which the BOM needs 4 per tyre? If 1 cut = 1 piece, the 1.66× must become ~3.3×.
  2. **What does `BCFT001` being measured in METRES mean on a `CUTS/MIN` machine?** (unit mismatch — `pieces_per_cut` divides metres by 2 today)
  3. Secondary: confirm 2001 really is one physical unit shared by PCR *and* TBR. The DB has **no machine master for cutting at all** (`MasterMachines_pcr` = 92 rows, **all Curing**; `Master_WC_Master` has no 2001), so `CTP Machines.xlsx` is the sole source.

---

## 0. FIXED THIS SESSION (shipped + validated)

| ID | Flaw | Fix | Result |
|----|------|-----|--------|
| **F-A** | Future-dated WIP seed → "day-60" kit | `clamp_wip_seed_to_plan_start` + loud flag | ✅ April 0-WIP 72.8→37.25 d |
| **F-B** | phase6 `prev_end` head-of-line (4× tail) | `press_gap_fill` (per-press free-slot) | ✅ June 34.47 d unchanged |
| **H-1** | Silent no-machine lot drop | log to `infeas` (reason no_machine/no_slot) | ✅ June infeas still 0 (no blanks) |
| **H-3** | GT terminal-anchor collapse → over-age | `honest_terminal_anchor` (own last_need) | ✅ **June GT over-age 11.9 → 6.1%** for **+0.5 d** (34.47→35.0) |
| **H-4** | Plan month can silently flip | loud PLAN PROVENANCE banner + `expected_plan_month` assert | ✅ prints June 06-01..07-01 |
| **M-1** | kit → skus_set[0] only | loud guard, **GT-lots only** (0 multi-SKU → inert; components excluded) | ✅ no false alarm |
| **M-4** | est-floor off collapsed lft | subsumed by H-3 (est-floor now keys off the honest anchor) | ✅ |
| **M-9** | block_id numbered before dropna | number after dropna (contiguous, matches phase1_5) | ✅ byte-identical |
| **M-5/M-6/L-8** | stale comments (24 vs 48, 480 vs 648, sticky 40) | corrected in config | ✅ docs only |

All hard invariants hold on June + April; self-test still catches a corrupted schedule.
**C-1 DOWNGRADED** (user-correct): the excluded SKUs have no build routing, so building their
components would just make orphans — CTP #41 is right to prune. The 14,065 stay flagged
(`CURED WITHOUT A GREEN TYRE`); action is only "don't count them as demand-met." No code change.

**Still open:** **H-2** (one-line `gt_transfer_time_min: 90` — plant-confirmed; DB column is blank so
can't be auto-pulled); **M-2** (changeover-neighbour under gap-fill — intricate placement change,
deferred so it can be done + tested carefully, not rushed); the remaining M/L hardening items.

---

## 1. HIGH — fix before trusting a run

### H-1 · Silent drop of no-machine component lots (starvation gate lies)
- **Where:** `phase5_forward_placement_v2.py:2444, 2622` (`return None`); caller `:4920` (`continue`); gates that then hide it `:2375`, `:5230`.
- **What:** `place_one_lot_jit` returns `None` when a lot's `machines` list is empty — the caller just `continue`s with **no `infeas` log** (unlike the building path). The lot vanishes; the material-ready gate and starvation audit **exclude the dropped predecessor**, so a green tyre that needs it is scheduled/cured anyway and the run still prints **`infeas: 0`, `MATERIAL STARVATION: 0`.** A clean bill of health that is false. Also defeats CTP #8 (a force-kept orphan with no machines is silently dropped).
- **Trigger:** a routing row with a **blank `machines` cell** → `parse_machines → []` (the present-but-blank NaN trap).
- **Fix:** log `{lot_id, item, reason:"no_machine"}` to `infeas` on the `None` return; count `need` over ALL predecessors and subtract dropped producers so starvation surfaces.

### H-2 · GT→curing 90-min transfer silently applied as 0
- **Where:** `phase6_curing_revision.py:1292-1306`; `config.yaml:263` (`press_streaming_kit_gate:true`), `:264` (`gt_transfer_time_min` commented out); data `inputs/routing.csv`.
- **What:** the streaming gate derives `GT_TRANSFER_H` from routing's `GT BUILDING` `transfer_time_min`. All **224** GT-building rows in the DB-adapted routing have that column **blank** (other depts carry it), so `GT_TRANSFER_H = 0.0`. A press may cure a green tyre **90 min before it can physically reach the press.** The same 0 propagates through phase4's `get_min_aging` lag and phase5 — absent end-to-end. Config comment two lines up asserts "the workbook says 90."
- **Fix:** uncomment `gt_transfer_time_min: 90` (override path bypasses the blank column) OR re-apply the routing patch that restores GT-building `transfer_time_min=90` after every `adapt_inputs`. Confirm 90 min with the plant.

### H-3 · Green-tyre terminal anchors COLLAPSE (de-pulsed deadlines discarded)
- **Where:** `phase2_lot_sizing.py:988-1001` (block fan-out maps every sub-lot to the whole bucket) vs `:936-948` (de-pulse writes per-sub-lot `first/last_need`); `phase4_cpm.py:429-437` (`earliest_curing = need_by.min()`); false "no fan-out" comments `phase4:524,542`.
- **What:** phase2 de-pulses each GT sub-lot's own `first/last_need` across real curing days, but still maps **every** sub-lot to **every** demand row of the bucket. phase4 anchors terminals to the **min** over that wide block set → every GT sub-lot in a wave anchors to the wave's **earliest** curing block, collapsing up to 72h of deadlines onto one instant. Not inert: the collapsed LFT feeds `LFT_place` (phase5 fresh-floor → GT built early → waits → over-age) and `est_floor_from_aging` (components build too early → over-age). `honest_lft_anchor` skips terminals, so it does NOT repair this. A plausible contributor to the ~50% GT / ~44% component over-age.
- **Fix:** anchor terminals to the lot's **own** de-pulsed `last_need − min_age` (mirror what honest_lft_anchor does for non-terminals), or de-pulse `block_assignments` too (each sub-lot gets only the blocks in its window).

### H-4 · Plan provenance is unguarded — the scheduling MONTH can silently flip (June ↔ April)
- **Where:** `phases/db_loader.py:157-197`, `adapt_inputs.py:236-257` (`db_plan`), `config.yaml` (`data_source:db`, `db_plan:false`), `table_map.py:60-66`.
- **What:** `"plan"` is resolved in **two** independent layers — `adapt_inputs` honours `db_plan`, the phases honour `phases_read_db`. If they ever disagree, adapt writes the June shift-sheet (7,369 rows / 385,772) while the phases read the April `CuringSchedule_pcr` (6,732 / 349,241) — an **entire-month** switch with no error (the code's own comment cites "95.33 d instead of 34.78 d"). Because C-1's pruned-SKU set is plan-dependent, the loaded month silently changes both the demand and which SKUs get pruned. *(Note: the live oscillation the auditor saw was partly my own April/June test runs during the audit; the two-layer fragility is the real latent issue.)*
- **Fix:** resolve `"plan"` in ONE place; after adapt, assert `plan.csv`'s date span matches the configured source and fail loudly on mismatch; stamp month/source into a sidecar checked at phase1b entry.

---

## 2. MEDIUM

| ID | Flaw | Where | Fix |
|----|------|-------|-----|
| **M-1** | phase6 v2 adapter credits a whole building lot's kit to `skus_set[0]` only → multi-SKU lots mis-attributed (phantom SHORT on the others, phantom-early kit on #1) | `phase6:540-543` | split lot qty across its SKUs by demand share, or assert single-SKU and fail loudly |
| **M-2** | Changeover charged against the last **dispatched** item, not the **time-adjacent** lot — wrong when `first_fit_jit` fills an earlier gap (CTP matrix magnifies 5-vs-50 / 3-vs-15) | `phase5:2601-2606` | recompute co against the item occupying the interval immediately before `start` |
| **M-3** | `aging_hours` NaN trap: a blank/NULL cell → `float(NaN)` (no raise) → **crash** in phase2 (`int(NaN//h)`), **silent-void** in phase4 (`lft` stays BIG = infinite slack) | `phase2:107-113`, `phase4:85-91` | explicit `if v != v: return 0.0`; declare blank-max as a hole, not NaN |
| **M-4** | Live `est_floor_from_aging` keys the freshness floor off the collapsed `lft[v]`; `c6_jit_audit` fixed only the **diagnostic**, not the est-feeding computation | `phase4:288-301` | key the floor off the honest consumer ref (`est[v]` / de-pulsed first_need) when honest anchoring is on |
| **M-5** | `green_tyre_max_age_h: 24` + "confirmed 24h" comments contradict the live source (aging_master = **48h**, user-confirmed) — inert but a landmine; also GT `MinAging = 0` vs plant 15 min | `config:170,718`; `inputs/aging_master.csv` | set to 48, rewrite the stale block; confirm GT min-age (15 min) with plant |
| **M-6** | Deviation table + balance analysis say `building_month_cap_h = 480`; live value is **648** — doc misreports the cap by 168h and the balancer's tradeoffs are stale | `config:19-23,341-350,1589` vs `:351` | update docs to 648 (or re-measure at 648); no schedule change |
| **M-7** | `building_takt_target_util: 0.65` is an assumed availability, not plant OEE (drives TBM-per-code spread) | `config:408` | ask plant for real building OEE; annotate as a tuning knob |
| **M-8** | MPQ provenance ambiguous: config names `jkt_mpq_corrected.xlsx` but DB-mode pulls `jkt_mpq_pcr` — a pure-DB refresh could silently revert the bead-apex MPQ fix | `config:541` vs `table_map:37-43` | confirm the DB table carries the corrected values, or pin the corrected xlsx |
| **M-9** | `block_id` is derived **independently and with different filters** in phase1b vs phase1_5 (NaT criterion, numbering-vs-dropna order, `selected_skus`) → the wave join can silently misalign lots to the wrong wave (`W99`). Benign today (0 offending rows) | `phase1b:129-163` vs `phase1_5:62-76`, used `phase2:480` | compute `block_id` once in a shared helper; or join on `(skuCode,startTime)` not a positional id |
| **M-10** | phase1c nets GT build demand against **already-expired** GT WIP (608 of 4,632 units >24h old at plan_start) → under-builds green tyres → curing shortfall. Flows from the accepted v6-verbatim deviation | `phase1c:204-206,218-219` | (if bounding) gate WIP credit by `produced_time+max_aging ≥ plan_start` for short-shelf types, or report the expired-but-credited qty |

---

## 3. LOW (latent / inert / heuristic-quality)

| ID | Flaw | Where |
|----|------|-------|
| **L-1** | `verify_invariants` joins on a **positional** `block_id` re-derived from a 2nd plan load — spurious FAIL/garbage if the two loads ever differ (e.g. DB `SELECT` with no `ORDER BY`) | `phase6:2393` vs `:1969` |
| **L-2** | best-press dispatch orders by `kit_ready` (**newest** tyre), not `kit_oldest` — anti-FEFO (opt-in; `best_press_curing:false`) | `phase6:1592` |
| **L-3** | `balance_all_machines` picks least-loaded by **month-total** `machine_busy_ns` (no time axis) — can concentrate work in a window | `phase5:2635` |
| **L-4** | `"SEC"` proc-time with `batch_size` omits the `ceil` that `SEC/BATCH` uses → understates machine time for partial batches | `phase2:281` |
| **L-5** | `_parse_mpq` treats a thousands separator as a list: `"1,800" → min(1,800) = 1` (catastrophic under-floor) — latent, CTP's current values parse fine | `phase2:165-167` |
| **L-6** | `fraction_allowed` YES/NO branches look inverted vs standard MPQ semantics — inert (adapt_inputs writes all "NO") | `phase2:874-877` |
| **L-7** | `DEFAULT_MAX_AGE_H = 365×24` for an item missing an aging row (the Capstrip-Mother-Roll trap) — mitigated by itemtype fallbacks | `phase5:2093,2217` |
| **L-8** | `building_spread_eft_sticky_min: 40` comment calls 40 "the same-size changeover" — real is 22-28; dead `CO_SAME/CO_DIFF=40/60` locals are a landmine | `config:1019`, `phase5:2824` |
| **L-9** | `db_plan: false` is load-bearing (DB CuringSchedule is APRIL vs June masters) — needs a guard so it can't be flipped without refreshing the plan month | `config:79` |
| **L-10** | Four building levers ON; tradeoffs measured individually (esp. balance measured at cap=480) — re-measure jointly after any data change | `config:406,408,421` |
| **L-11** | `phase3_resolve_lot_blocks.py` is an unused window-approximation (not in the pipeline) — flag so nobody re-wires it as authoritative | `phase3_resolve:121-133` |
| **L-12** | `MOULD_CLEAN` rows are never name-filtered (only `qty>0` saves them); adapters docstring wrongly claims v6 filters them. A `MOULD_CLEAN` row with `qty>0` becomes a phantom SKU | `phase1b:134`, `phase1_5:65`, `adapters.py:89` |
| **L-13** | `phase0` coverage diagnostics use the **abandoned first-row** BOM dedup (not phase1b's SUM) → the validation report can certify a BOM shape the explosion doesn't use | `phase0:190-191` |
| **L-14** | `adapt_plan` (DB path) derives `cycleTime = runTime/qty`, ignoring **2-cavity** presses → cure duration off by the cavity factor if `db_plan` is ever enabled | `adapters.py:107-110` |
| **L-15** | `_parse_mpq` thousands-separator bug (dup of L-5, listed under core-math) / `phase1c:203 orig_total` dead code | — |

---

## 4. Checked and CLEARED (so they are not re-audited)
Machine double-booking (none — occupancy re-derived and self-tested); determinism (sorted/mergesort
everywhere on shipped paths); phase6 FIFO global start-order (F1, correct); shift-split floor view
(qty-conserving, no phantom rows); `_streaming_gate` math; edge direction + F1 sign in phase3; CPM
forward/backward ordering; edge-consume-qty FEFO (no double-count); MPQ-cascade fixpoint (converges);
WIP FIFO vectorised alloc; ns conversions (no overflow); building sec/tyre + dual-cavity + curing cycle
times + mixer 2-min (all plant-sourced, plausible); `pre_curing_buffer_h` 8-vs-72 (no conflict);
current opening WIP is NOT future-dated on the June run (produced ≤ T0).

---

## Recommended order of work
1. **C-1** (CTP #41 prune deletes 14,065 tyres' components on a false-WIP premise) — the one that silently
   fabricates cures. Fix the trigger first.
2. **H-1** (silent no-machine drop) — loses real work AND blinds the "0 starvation / 0 infeas" lines.
3. **H-2** (restore 90-min GT transfer) — one config line; affects aging end-to-end.
4. **H-3** (terminal-anchor collapse) — biggest lever on the ~50% GT over-age.
5. **H-4** (plan-provenance guard) — cheap assert that stops a silent month flip.
6. **M-2/M-3/M-4/M-9** (changeover-neighbour, NaN trap, est-floor, block_id) — correctness.
7. Doc/landmine cleanups (M-5, M-6, L-8, L-12, L-13) — no schedule change, prevent regressions.

## Tally
2 FIXED (F-A, F-B) · **1 CRITICAL** (C-1) · **4 HIGH** (H-1..H-4) · **10 MEDIUM** (M-1..M-10) ·
**15 LOW** (L-1..L-15). Zero of the HIGH/CRITICAL crash — every one is *silent*, which is what makes
them dangerous. None is a deliberate CTP deviation.
