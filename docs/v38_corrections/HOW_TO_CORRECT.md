# How to correct the 15 v38 issues

Run: `outputs/e2e_v38`. Horizon: 22 Oct 07:00 → 1 Nov 07:00. Data: `data-ppds-v38`. Rules: `config/rules_v38.yaml`.

Each issue below has the same six parts:

1. **Claim.** What was raised.
2. **What v38 shows.** The numbers, recomputed from the run outputs.
3. **Root cause.** Where in the code or data it comes from.
4. **Correction.** The fix, step by step.
5. **Verification.** A test that a fixed run (v39) must pass.
6. **Expected effect.**

All paths are relative to `pns-ctp-scheduler/`. Line numbers are those of the v38 code.

---

## 1. Cold start: thin opening stock starves the first three days

**Claim.** The schedule starts with too little material on the floor, so the first days are lost.

**What v38 shows**

* WIP *is* used. Of the needed quantity, WIP covers PCR 133,771 of 194,545 and TBR 122,922 of 161,286.
* WIP is not where the kits need it. **0 of 217** kits built on days 1–3 are fully covered by opening stock.
* Day-1 stock as a share of day-1 need:

  | line | component | day-1 stock / need |
  |---|---|---|
  | PCR | tread | 37% |
  | PCR | bead apex | 17% |
  | TBR | tread | 13% |
  | TBR | apexed bead | 29% |
  | TBR | pre-assembly | 21% |

* 81 lots in `phase4_infeasible` would have to start up to 24 h *before* 22 Oct 07:00 to meet their building time.
* Trolley delay against plan grows from 37 h on day 1 to about 90 h by day 4, and never recovers. Builders were planned at about 81–83%, so there is no slack to catch up.
* Tyres lost on days 1–3: **PCR 27,028, TBR 6,533**.

**Root cause.** Four separate effects add up.

| # | cause | where |
|---|---|---|
| 1a | FEFO is done per component. Stock of tread for one kit is drawn while that kit's bead is missing, so stock is spread over many kits that are all incomplete | [phase2_wip_netting.py:190](../engine/phase2_wip_netting.py#L190) `allocate()` |
| 1b | Cut stock (cut tread, cut ply, cut belt) is invisible after the cutter bypass. Those codes are no longer "made", so WIP on them is never netted. That is PCR 40,137 and TBR 30,879 units of usable stock ignored | [ctpio.py](../engine/ctpio.py) `schedulable_items` (bypass set) + phase 2 netting only of made items |
| 1c | B616M (a compound) appears as a direct kit item in 414 PCR trolleys. CAP 66 has no routing, so the BOM walk descends into its child and makes the builder wait for a mixer | [ctpio.py:~1193](../engine/ctpio.py#L1193) WalkCache descent of unrouted items |
| 1d | No ramp-up rule. The building plan is frozen (D-8), so the first trolleys are due at 07:00 whether material exists or not | `phase4` CPM backward pass, `--policy shift` |

**Correction**

1. **Kit-complete FEFO (1a).** In `phase2_wip_netting.py`, change `allocate()` from per-component to per-kit:
   * Group demand rows by `building_lot_id`, in build-start order.
   * For each kit, first test whether *every* component can be covered by mature, unexpired stock (`maturity ≤ draw_at`, `expiry ≥ need_by`).
   * Commit the draws only if the whole kit is covered. Otherwise leave the stock for the next kit, and record the kit as `partial`.
   * Do a second pass that gives leftover stock to partial kits, earliest first. Nothing is wasted; complete kits simply get first claim.
2. **Make cut stock visible (1b).** When a cutter/slitter item is bypassed, map its WIP to the parent kit component it feeds. Keep the netting identity, not the routing.
   * In `ctpio.schedulable_items`, return `bypass_map: cut_code → building_input_code`.
   * In phase 2, before netting, add `wip[cut_code]` to `wip[building_input_code]`, converted by the BOM ratio.
   * Log the converted quantity per code to `phase2_bypass_wip.csv`.
3. **Remove B616M from kits (1c).** Two options:
   * Data (preferred): add a routing row for CAP 66 in `CTP_Master_Routing`. The walk then stops at CAP 66 and B616M is used only upstream.
   * Code: in WalkCache, stop the descent at an unrouted item whose type is a component, not a compound, and treat it as purchased. Log it to `ASSUMED_PURCHASED_ITEMS.csv` with reason `unrouted_component`.
4. **Ramp-up window (1d).** Add `engine.ramp_h` (suggested 8–16 h) to `rules_v39.yaml`.
   * Lots in `phase4_infeasible` with a required start before open are re-anchored to `open + ramp offset` in the plan order, not dropped.
   * For the first `ramp_h` hours, place only kits that are fully covered by opening WIP (from step 1). These are known from phase 2.
5. **Data check before the run.** Add a pre-run report (`phase0_opening_cover.csv`): day-1 stock / day-1 need per department. If any department is below 50%, warn and list the items, so the planner can confirm stock or move the first trolleys.

**Verification**

* Day 1–3 kits fully covered ≥ 60% (v38: 0 of 217).
* Converted cut stock appears in `phase2_bypass_wip.csv`, about 40k PCR / 31k TBR.
* `grep B616M` in the kit lists of `phase5_building_plan.csv` returns 0 rows.
* Trolley delay on day 1 < 12 h and not rising day on day.

**Expected effect.** This is the largest single source of lost volume. Recovering half of the day 1–3 loss is worth about 15k tyres. Every later day also gains, because the delay no longer cascades.

---

## 2. Press capacity: "too few presses"

**Claim.** Curing has too few presses for the plan.

**What v38 shows**

* Plan load on presses: PCR 95.4% of 86 presses, TBR 91.9% of **80** presses (not 64). The plan's own changeover time is included: PCR 279 h, TBR 217 h.
* The input plan has 0 press overlaps. It is feasible as written.
* In the schedule, presses cure for only **59% (PCR) and 47% (TBR)** of the horizon.
* Push against plan (`push_h`) grows from 21 to 99 h for PCR and from 41 to 92 h for TBR over the horizon.
* Lateness of green tyres (`gt_ready − planned_start`): median 35 → 89 h for PCR, 50 → 56–72 h for TBR.
* Press cascade (lots pushed only because the lot ahead on the same press was pushed): 0 → 21 h for PCR, 0 → 31 h for TBR.

**Root cause.** Press count is not the limit. Presses wait for green tyres (#1, #4, #5). When a lot is late, the push loop moves every later lot on that press too (cascade), and nothing is re-sequenced. A press that could cure a lot whose green tyres *are* ready instead stays idle behind the late lot. See [phase7_curing.py:191-205](../engine/phase7_curing.py#L191).

**Correction**

1. Fix #1, #4, #5 and #6 first. They set the arrival times of green tyres.
2. **Press re-sequencing.** In `phase7_curing.py`, replace the fixed-order push with a ready-list dispatcher per press:
   * Each press keeps its planned lots as a queue, but the next lot is the earliest planned lot *whose green tyres are ready* by the time the press is free.
   * Allow look-ahead of at most `curing.resequence_window_h` (suggested 24 h), so the mould plan is not scrambled.
   * Apply the mould-change gap (#6) whenever the SKU changes.
   * Keep BR-10 (PM) through `io.pm_push`.
3. **Swap to allowable presses.** If a lot is late and another *allowable* press with the same mould is free, move it. Use `CTP_Master_CuringAllowable`, never a press outside it (see the Sept allowable-press finding).

**Verification**

* Press cascade median ≤ 4 h (v38: up to 21 h / 31 h).
* No lot cured on a non-allowable press.
* Press curing share of horizon rises. Report it; it is limited by green-tyre supply.

**Expected effect.** Tens of thousands of tyres only after #1/#4/#5 are fixed. Re-sequencing alone recovers the cascade share, roughly 5–10%.

---

## 3. 8,808 tyres that can't be built

**Claim.** 8,808 tyres have no way to be built, leaving presses idle.

**What v38 shows**

* Confirmed: **8 SKUs** have no building lot, 8,808 tyres, mostly TBR.
* 16 presses carry only these SKUs. That is 3,840 press-hours idle.
* Four small TBR SKUs (92 tyres) have green tyres in stock, but the stock is not used. 325 GT in stock are unused in total.
* The QXPC0 SKU is the MSIL variant. Its green tyre, 1422, is absent from PPDS. It is not a code mismatch with QXPCT (TATA) or QXPCM (M&M).
* SKU 112222982C016MJXM0 has two GT codes: 5103 in one master and 5083 in another.

**Root cause**

* **Data.** The BOM rows for these SKUs are missing in `CTP_Master_BOM`. The user chose to keep the BOM as it is, so the run is correct for its data.
* **Code.** GT stock is matched only through building lots (`gt_alloc` via `gt_item` in [phase7_curing.py](../engine/phase7_curing.py)). A curing SKU with no building lot never sees its own GT stock.

**Correction**

1. **Data (owner: master data).** Add the missing BOMs, or confirm the SKUs are not in this month's demand. Add GT 1422 to PPDS. Resolve 5103 vs 5083 for 112222982C016MJXM0 in the master that is wrong.
2. **Code: map GT stock by curing GT code.** In phase 7, build the GT demand from the curing master (`SKU → GT_Code`), not from building lots:
   * `gt_need[sku] = cure_qty`. Draw GT stock first by GT_Code, then from building lots.
   * A curing SKU with GT stock but no building lot gets cure lots up to its stock.
3. **Report.** Write `phase7_unbuildable.csv`: SKU, tyres, reason (`no_bom` / `no_gt_master` / `gt_conflict`), presses affected, and GT stock available.
4. **Release presses.** Presses carrying only unbuildable SKUs are released to other allowable SKUs in the re-sequencer (#2).

**Verification**

* The 4 small TBR SKUs cure their 92 tyres from stock.
* Unused GT stock = 0, or each unit has a reason in the report.
* `phase7_unbuildable.csv` lists exactly the SKUs that still lack data.

**Expected effect.** About 100 tyres from stock now. Up to about 8,700 when the BOMs are added.

---

## 4. Building overlaps: two lots on one builder at the same time

**Claim.** The schedule puts overlapping lots on the same building machine.

**What v38 shows**

* **300 lots** overlap, **644 h** in total, on **all 20** builders.
* Recomputed with the correct duration of each trolley, the same schedule has **0 overlaps**.
* The engine prints `Machine overlaps: 0` because its check runs before the bead gate and on columns that are later overwritten.

**Root cause.** An index-alignment bug in [phase5_placement.py](../engine/phase5_placement.py):

```python
# line 1413  — span is a Series indexed by the ORIGINAL row order
span = (blots["build_end"] - blots["build_start"]).fillna(pd.Timedelta(0))
blots["feasible_end"] = blots["feasible_start"] + span
...
# lines 1445-1446 — rows are re-sorted and the index is reset to 0..n-1
blots = blots.sort_values(["machine", "build_start", "building_lot_id"],
                          kind="stable").reset_index(drop=True)
...
# line 1580 — pandas aligns `span` by index LABEL, not position
blots["feasible_end"] = blots["feasible_start"] + span
```

After `reset_index`, row 5 is a different trolley, but `span[5]` is still the old row 5's duration. Each trolley gets another trolley's duration. A long trolley given a short span finishes "early", and the next trolley starts on top of it.

There is a second problem. The overlap check at [line 1376](../engine/phase5_placement.py#L1376) runs before `_bead_gate` ([line 1665](../engine/phase5_placement.py#L1665)), which can move lots later. So the printed `0` does not describe the written schedule.

**Correction**

1. Store the duration as a column, so it moves with its row:
   ```python
   blots["span"] = (blots["build_end"] - blots["build_start"]).fillna(pd.Timedelta(0))
   blots["feasible_end"] = blots["feasible_start"] + blots["span"]
   ...
   blots["feasible_end"] = blots["feasible_start"] + blots["span"]   # line 1580
   ```
   Search the file for every other use of the bare `span` variable after line 1446 and replace it with `blots["span"]`.
2. Move the overlap check to the end of phase 5, after `_bead_gate` and the kit-gate re-derivation, on the frame that is written to `phase5_schedule.csv`.
3. Make the check fatal: if `ov > 0`, raise with the first 10 offending pairs. A schedule with overlaps must not be written.
4. Add a unit test (`tests/test_phase5_span.py`). Build 3 trolleys of different lengths in unsorted order, run the sort and the end calculation, and assert `feasible_end − feasible_start == build_end − build_start` for each `building_lot_id`.

**Verification**

* For every row: `feasible_end − feasible_start == build_end − build_start` (to the second).
* Overlaps on builders = 0, measured from `phase5_schedule.csv` by an outside script (`audit_v38/audit_v38.py` building check).

**Expected effect.** No change in tyres by itself, but the schedule becomes physically possible. "Built in horizon" is currently overstated: PCR 83,779 against a true 82,608, and TBR 20,905 against a true 20,689.

---

## 5. Building durations disconnected from quantity

**Claim.** The time a lot takes on a builder does not follow its quantity.

**What v38 shows**

* Correlation of written duration with lot quantity: **−0.035**. In the input plan it is **0.588**.
* Same trolleys, same quantities. Only the written duration differs.

**Root cause.** The same bug as #4 ([phase5_placement.py:1413 / 1446 / 1580](../engine/phase5_placement.py#L1413)). Durations are shuffled between rows.

**Correction.** The fix in #4 fixes this too. Also add a second check at the end of phase 5:

* Per builder, `duration_h / qty` must lie within ±25% of the plan's cycle time for that SKU. List outliers to `phase5_duration_check.csv`.

**Verification.** Correlation ≥ 0.5, and per-row duration equals the plan duration.

**Expected effect.** As #4.

---

## 6. Mould changes cost zero time

**Claim.** When a press changes from one SKU to another, the schedule gives no time for the mould change.

**What v38 shows**

* **35 of 60** SKU changes on presses have a zero gap in the schedule.
* In the input plan the same changes have a gap (73 changes have a gap).
* The plan carries changeover time: PCR 279 h, TBR 217 h.

**Root cause.** The push loop in [phase7_curing.py:191-205](../engine/phase7_curing.py#L191):

```python
span = (cp["planned_end"] - cp["planned_start"])   # production only
...
f = free.get(r.press)
if f is not None and s < f:
    s = f                      # starts the instant the previous lot ends
...
free[r.press] = s + r.span
```

The planned gap between lots is lost. When a lot is pushed, it starts exactly when the previous lot ends, whatever the SKU. The gap survives only where the lot was not pushed.

**Correction**

1. Before the loop, compute each lot's required changeover against the previous lot on the same press:
   * Same SKU and mould: 0.
   * Different SKU: the plan's `CO_Mins` for that lot if present. Otherwise the changeover master for curing. Otherwise a default `curing.mould_change_min` in rules (suggested 120 min for PCR and 180 min for TBR; confirm with the plant).
2. In the loop:
   ```python
   if f is not None:
       gap = co_gap(prev_sku[r.press], r.sku)
       if s < f + gap:
           s = f + gap
   ```
   Then set `prev_sku[r.press] = r.sku`.
3. Write `co_min` and `co_reason` on each row of `phase7_curing_plan.csv`.
4. In the re-sequencer from #2, add the change cost to the choice of next lot, so same-SKU lots are preferred when ready.

**Verification**

* Press SKU changes with a gap smaller than the required changeover = 0.
* The total changeover time in the schedule is about equal to the plan's (279 h / 217 h) for the lots that ran.

**Expected effect.** Slightly *fewer* tyres in horizon (the schedule was optimistic), but a schedule the floor can follow.

---

## 7. Cutting section missing

**Claim.** The schedule has no cutting or slitting section.

**What v38 shows**

* By the user's instruction (v33), cutters and slitters are bypassed. Material goes from calendering/extrusion straight to building. The transfer time added is the minimum ageing of the skipped components (v34).
* The report `ASSUMED_PURCHASED_ITEMS.csv` lists 1,079 codes as "ASSUMED INFINITELY AVAILABLE". That label is wrong for most of them:

  | group | codes | what they really are |
  |---|---|---|
  | bypassed cut codes | 467 | made, but on a bypassed department |
  | purchased | 219 | correct |
  | raw materials | 393 | correct, but 8 of them are Green Tyre codes, which are not raw materials |

* Cutter capacity and lead time are not modelled, by design.

**Root cause.** Model: the bypass is a deliberate simplification. Report: [make_floor_schedule_v10.py](../engine/make_floor_schedule_v10.py) writes all non-scheduled codes under one label.

**Correction**

1. **Report.** In `make_floor_schedule_v10.py`, write a `category` column with the values `BYPASSED_CUT` / `PURCHASED` / `RAW_MATERIAL` / `GREEN_TYRE_STOCK`. Change the header text so that only `PURCHASED` and `RAW_MATERIAL` say "assumed available".
2. **Check the 8 GT codes.** They should come through GT inventory (#3), not the purchased list.
3. **Model (optional, later).** If cutting becomes a constraint, restore the cutters as capacity-only machines. They would get lot times and changeovers, but no new BOM levels. Do it behind a rule `bypass.capacity_check: true`, so the v33 logic stays the default.
4. Add the bypass assumption to the run's assumption sheet, with the transfer-time rule and the source.

**Verification.** No `BYPASSED_CUT` row says "infinitely available". The 8 GT codes are no longer in the purchased list.

**Expected effect.** None on tyres. The report becomes true.

---

## 8. 4RC 901 run as a changeover machine

**Claim.** The four-roll calender 901 changes item almost every lot.

**What v38 shows**

* **166 lots, 165 item changes, 71 PCR↔TBR switches.**
* Production 125 h against **131 h of gaps**, of which 100 h is charged changeover.
* 901 is the last component to arrive for **83% of PCR** and 28% of TBR kits.
* Line speed against the FRC master: 3 items are slower than the master. 4 items have **no master row**: CPJ1218, ECOPOLY350, CPJ120, MONOCHAFER.

**Root cause**

* **Code.** The dispatch key at [phase5_placement.py:612](../engine/phase5_placement.py#L612) orders by deadline first: `(lft, camp, l)` when `V16_URGENT` is on. Each next lot is the most urgent one, whatever it is. The campaign key (`camp`) is only a tie-break, so campaigns never form.
* **Code.** PCR and TBR are already placed in one joint phase 5 run (`shared_machines.joint: all` in `config/rules.yaml`). Because the key is deadline-first, the queue picks whichever line's lot is due soonest, so 901 alternates between PCR and TBR.
* **Data.** The 4 missing FRC rows fall back to median rates.

**Correction**

1. **Campaign window for 901.** For machines in `campaign_machines: [901]` (rules), use the key `(camp, lft, l)` inside a window. A campaign keeps running while the earliest LFT of any other item is more than `campaign.window_h` hours away (suggested 8 h).
2. **Line blocks on shared machines.** The joint placement already exists. Add a line term to the campaign key on 901: stay on the current line while its next lot's LFT is within `campaign.window_h`, and switch only when the other line's lot would otherwise be late.
3. **Group by compound family**, with the changeover matrix from PPDS deciding the order inside a campaign (light to dark, soft to hard).
4. **Data.** Add FRC master rows for CPJ1218, ECOPOLY350, CPJ120 and MONOCHAFER. Check the 3 slow items against the real line speed.
5. Report per shared machine: lots, changes, line switches, changeover hours.

**Verification**

* 901 item changes ≤ 60 (v38: 165). PCR↔TBR switches ≤ 15 (v38: 71).
* 901 changeover hours < 40 h.
* Share of kits whose last item is from 901 falls well below 83%.

**Expected effect.** About 60 h of 901 time freed in the horizon, roughly a 50% rise in 901 output. 901 is the main kit blocker for PCR, so this is one of the biggest levers after #1.

---

## 9. TBR bead pool unbalanced

**Claim.** One bead machine is overloaded while others are idle.

**What v38 shows**

* Load over 240 h: **2403 at 104%**, 2404 at 63%, 2405 at 45%.
* Apexed bead is the last item to arrive for **178 TBR kits**. Calendering is last for 78, pre-assembly for 12, duplex for 7.

**Root cause.** Phase 4b assignment fixes each lot's machine early, by preferred machine. The phase 5 rebalance moves building trolleys but not bead lots. Work stealing (v32) takes only lots whose allowable list includes the idle machine. It is not applied after `_bead_gate` ([line 1665](../engine/phase5_placement.py#L1665)) moves apex lots later.

**Correction**

1. **Data check.** List the items routed only to 2403. If 2404/2405 can make them (same bead size range), add the routing rows. That is the cheapest fix.
2. **Load-aware assignment (phase 4b).** Assign each bead lot to the allowable machine with the least committed hours at its need date, not to the first preference.
3. **Rebalance after the bead gate.** After `_bead_gate`, run a stealing pass for the bead pool: an idle allowable machine takes the earliest waiting lot.
4. **Keep the bead gate** (apexed ≤ wound) as it is. It is a correct physical rule.

**Verification**

* 2403 ≤ 90%. Spread between the most and least loaded bead machine ≤ 25 points.
* Kits blocked by apexed bead fall clearly below 178.

**Expected effect.** TBR kits complete earlier. With #1, this is TBR's main lever.

---

## 10. Idle presses and builders

**Claim.** Presses and builders stand idle while demand is unmet.

**What v38 shows**

* Presses are in use 53% of the time, builders 54%.
* **100% of press idle time** is time waiting for green tyres. None is caused by missing moulds or PM.

**Root cause.** A consequence, not a separate defect. Builders wait for kits (#1, #8, #9) and presses wait for builders. #4 and #5 make the building times themselves unreliable.

**Correction.** No separate fix. After the fixes for #1, #4–#6, #8 and #9:

1. Report idle time per machine by cause: `waiting_kit`, `waiting_gt`, `changeover`, `PM`, `no_demand`. Add it to `phase7` and `phase5` summaries.
2. Measure again and judge each lever by how much idle time it removes.

**Verification.** The idle-cause report exists. `waiting_gt` share falls.

---

## 11. Green-tyre ageing unchecked

**Claim.** Green tyres may wait too long between building and curing.

**What v38 shows: not reproduced.**

* Max wait 85 h. 0 green tyres over 168 h.
* 2,974 wait over 48 h, and 244 over 72 h.

**Root cause.** None. Phase 7 checks GT ageing against the spec in `apply_aging_defaults`.

**Correction**

1. Add a warning band: list GT waiting more than 72 h in `phase7_gt_age_watch.csv`, so planners can cure them first.
2. In the press re-sequencer (#2), break ties by the oldest GT.
3. Confirm the GT maximum age with the plant. If it is less than 168 h for some SKUs, put those values in the ageing master.

**Verification.** 0 over the maximum. The watch list is written.

---

## 12. Low-volume SKUs dropped

**Claim.** Small SKUs get no production.

**What v38 shows**

* **9 PCR SKUs (3,564 tyres)** have 0 cured. 8 of them can be built.
* Their 17 trolleys are planned on **28–31 Oct**, the last days of the horizon.
* The trolleys are shifted a median 60 h, so none is built inside the horizon. Their GT ready time has a median of 3 Nov, after the horizon ends.
* What holds them: CPJ1218 (8 trolleys, a 901 item with no FRC master row), HTPOLY350 (2), ECOPOLY-EFA (2), CPJ121 (1), IL070 (1).

**Root cause**

* **Plan.** The input plan puts them at the end of the month.
* **Code.** The dispatcher orders by LFT only ([phase5_placement.py:612](../engine/phase5_placement.py#L612)). There is no penalty for leaving an SKU at zero, so the delay that cascades from day 1 pushes the last trolleys out.
* **Data.** CPJ1218 has no FRC master row (#8) and runs on 901, the most crowded machine.

**Correction**

1. **Shortage-aware priority.** Add a term to the dispatch key: SKUs with 0 cured so far, or with cured share below `priority.min_fill` (for example 30%), sort ahead within the same LFT bucket of `priority.bucket_h` (for example 24 h).
2. **Protect the last days.** For trolleys planned in the last 72 h, reserve their 901 lots earlier, so a late kit does not mean a lost SKU.
3. **Data.** Add the FRC row for CPJ1218 (#8).
4. **Planner.** Ask whether these SKUs can be moved earlier in the plan.
5. **Report.** In the summary, list each SKU with demand > 0 and cured = 0, with its blocking item.

**Verification.** 0 SKUs with demand > 0 and cured = 0, or each such SKU is listed with a data reason.

**Expected effect.** Up to 3,564 tyres, and full SKU coverage.

---

## 13. Compound over-produced

**Claim.** Compound is made at 1.56× the need (1.51 M kg surplus).

**What v38 shows: partly.**

* Real ratio of lot quantity to net demand: **1.15× for PCR, 1.10× for TBR**.
* Surplus: **494k kg** (PCR 340,622; TBR 153,659), not 1.51 M kg.
* Of that, **354k kg (72%)** is the +5 batch buffer per run (PCR 231,875; TBR 121,800). The rest comes from rounding up to whole batches.

**Root cause.** [phase3_lot_sizing.py:651-654](../engine/phase3_lot_sizing.py#L651):

```python
_nb = UR.buffer_batches(item, itype, is_special=...)
if _nb and batch and batch > 0:
    target += float(batch) * _nb
```

5 buffer batches are added to *every* run of every non-special compound (331 runs). Whole-batch rounding (v34) is correct and required.

**Correction**

1. In `rules_v39.yaml`, lower the buffer to 1 batch per run for master compounds and 0–1 for finals. Or make it per compound: `buffer_batches: {default: 1, overrides: {...}}`.
2. Better: add the buffer once per compound per horizon, not once per run.
3. Report surplus per compound with its two parts (rounding / buffer) in `phase3_surplus.csv`.
4. Keep the special compounds without a buffer (JT444, JT466, ML405, ML605, 29MG).

**Verification.** Ratio ≤ 1.05×. The buffer share of the surplus is in the report.

**Expected effect.** About 280k kg less compound, or about 45 mixer-hours freed on the master mixers. No loss of tyres, as long as the scrap rate is covered by the remaining buffer.

---

## 14. Audit trail stale

**Claim.** The audit and compliance text does not match this run.

**What v38 shows**

* [br_compliance_v15.py:291](../engine/br_compliance_v15.py#L291) hardcodes the text "2026-09-26 23:23 (26-Sep C shift)" with 7.6 h / 95.6 h staleness. v38 uses the 22 Oct 06:30 snapshot.
* **58 lot IDs are duplicated** between PCR and TBR. The ID is `f"{item}_L{seq:05d}"` ([phase3_lot_sizing.py:672](../engine/phase3_lot_sizing.py#L672)), and each line counts from 1.
* Line speeds by line: not confirmed (0 of 23 shared items differ).
* 101 PCR items and 16 TBR items run on A02 median rates, because their own rate is missing.

**Correction**

1. **Snapshot text.** Read the snapshot time from the staged data (the WIP `asof` already pinned by `CTP_WIP_ASOF`) and compute the staleness from it. Never write the date as text.
2. **Unique lot IDs.** Prefix the line: `lot_id = f"{line.upper()}_{item}_L{seq:05d}"`. Update the readers that parse lot IDs (`engine/make_floor_schedule_v10.py`, `engine/make_ctp_schedule_workbook.py`, and any SQL publish script that keys on `lot_id`).
3. **Default rates.** Write `phase0_default_rates.csv`, listing each item on A02 with the value used. Send it to master data and fill the real rates.
4. **Run stamp.** Every report starts with run id, rules file, data folder, WIP and GT snapshot times, and git commit.

**Verification**

* `grep -rn "2026-09-26" engine/` returns nothing.
* `lot_id` is unique over PCR+TBR.
* The default-rate list exists and gets shorter each run.

---

## 15. No end-of-horizon handling or replanning

**Claim.** The schedule ends at 1 Nov with no handling of unfinished work, and is never re-planned during the horizon.

**What v38 shows**

* Building outside the horizon: 56,089 tyres. Curing outside the horizon: 69,268 tyres. The last cure ends 6 Nov.
* **0 of 12** shift refreshes performed. One forward plan covers all 10 days.

**Root cause.** Model and process. The engine plans once from one snapshot. Lots that run past the end are reported but not carried forward, and the next run does not pick them up as committed work.

**Correction**

1. **Horizon edge.**
   * Mark each lot `in_horizon` / `crosses_end` / `after_end`.
   * Write `carryover.csv` with the WIP and committed lots at the horizon end, in the same format as the opening WIP. That way the next run starts from it.
2. **Frozen zone.** Add `engine.frozen_h` (for example 8 h, one shift). Lots that start in the frozen zone are fixed in the next replan.
3. **Rolling replan.** Add a driver `engine/rolling.py`. For each shift:
   * Pull the newest WIP and GT snapshot (`refresh_ctp_sql.py --ppds-only --only wip,gt`, with `CTP_WIP_ASOF` set to the shift bell).
   * Run phases 2–7 with open = the shift bell and the frozen lots fixed.
   * Write the outputs to `outputs/<run>/shift_<n>/`.
   * Run time per shift must stay under 20 minutes. v38's full run fits.
4. **Report.** Show the plan-versus-actual difference per shift once actuals exist.

**Verification**

* `carryover.csv` exists and is accepted as opening WIP by the next run.
* A test with 3 shifts produces 3 consistent shift plans, with the frozen lots unchanged.

**Expected effect.** No direct gain in the first plan, but the plan stays realistic during the month, which is how the floor will use it.

---

## Summary of code changes for v39

| file | change | issues |
|---|---|---|
| `engine/phase5_placement.py` | `span` as a column (lines 1413, 1580); overlap check moved to the end and made fatal; campaign key for 901; shortage term in the dispatch key (line 612); bead-pool stealing after `_bead_gate` | 4, 5, 8, 9, 12 |
| `engine/phase7_curing.py` | mould-change gap in the push loop (lines 191–205); press re-sequencer; GT stock by curing GT code | 2, 3, 6, 11 |
| `engine/phase2_wip_netting.py` | kit-complete FEFO; bypassed cut stock mapped to the kit input | 1 |
| `engine/ctpio.py` | `bypass_map`; stop the walk at unrouted components | 1 |
| `engine/phase3_lot_sizing.py` | buffer per horizon, not per run (lines 651–654); line prefix on lot IDs (line 672) | 13, 14 |
| `engine/br_compliance_v15.py` | snapshot time read from data (line 291) | 14 |
| `engine/make_floor_schedule_v10.py` | category column in the purchased report | 7 |
| `engine/rolling.py` (new) | shift-by-shift replan, carryover | 15 |
| `config/rules_v39.yaml` | `ramp_h`, `campaign_machines`, `campaign.window_h`, `curing.mould_change_min`, `curing.resequence_window_h`, `priority.min_fill`, `buffer_batches`, `frozen_h` | 1, 2, 6, 8, 12, 13, 15 |

## Data requests (owner: master data / plant)

| item | issue |
|---|---|
| BOMs for the 8 SKUs without a building lot (the user decided to keep the current BOM; reopen if wanted) | 3 |
| GT 1422 in PPDS; resolve GT 5103 vs 5083 for 112222982C016MJXM0 | 3 |
| FRC master rows: CPJ1218, ECOPOLY350, CPJ120, MONOCHAFER; check 3 slow items | 8, 12 |
| Routing for CAP 66 | 1 |
| Which items routed only to 2403 can run on 2404/2405 | 9 |
| Mould change time per press class | 6 |
| Real rates for the 117 items on A02 medians | 14 |
