# Implementation plan: from v38 to a corrected scheduler

This plan says in which order to make the changes in [HOW_TO_CORRECT.md](HOW_TO_CORRECT.md), how to make each one, and how to prove it worked before moving on. It adds two rules that apply to every run from now on:

1. **Data-blocked SKU demand is reported, not counted.** Each run writes a `VALIDATION_REPORT`. Demand that cannot be scheduled because data is missing goes to that report. It is taken out of the final figures: out of both the plan (denominator) and the cured count (numerator).
2. **Missing data that can be safely assumed is assumed, and the assumption is stated.** Each assumption has a code, the value used, where the value came from, and the SKUs and tyres that depend on it. Only data that cannot be assumed blocks an SKU.

---

## Part A: the data-blocker and assumption policy

### A1. Three states for every SKU (and every cure lot)

| state | meaning | in final figures? |
|---|---|---|
| **CLEAN** | every master record exists | yes |
| **ASSUMED** | some record is missing, and a stated assumption fills it (Part A2) | yes, with an "assumed" flag and the share of tyres shown separately |
| **DATA_BLOCKED** | a record is missing that cannot be assumed (Part A3) | **no**. Listed in `VALIDATION_REPORT` with its reason and owner |

The state is decided once, in phase 0, and carried on every lot through phase 7. A lot keeps the worst state of its SKU.

### A2. What may be assumed (assumption catalogue)

An assumption is allowed when there is a reasonable stand-in from the same plant's data, and the error it can cause is limited to timing, not to whether the tyre can be made.

| code | missing data | assumption | source of the value | status |
|---|---|---|---|---|
| A01 | machine list for an item | the department's machine pool | routing, same department | exists (ctpio.py:925) |
| A02 | process time | median of rated items in the same department | routing, same department | exists (ctpio.py:947) |
| A03 | batch size | median batch size of the department | routing | exists (v34 fill) |
| A04 | FRC/calender line speed (CPJ1218, ECOPOLY350, CPJ120, MONOCHAFER) | **slowest** master speed of the same item type on the same machine (cautious) | FRC master, same machine | new |
| A05 | mould change time on a press | 120 min PCR, 180 min TBR, until the plant confirms | rules file | new |
| A06 | ageing spec (min/max) | department default ageing | `apply_aging_defaults` | exists |
| A07 | changeover pair missing in the matrix | machine default changeover | `CHANGEOVER_UNPRICED.csv` | exists |
| A08 | routing for a component that the BOM uses (CAP 66) | the component is available from stock or purchase, and the walk does not descend into its compound | BOM item type | new |
| A09 | buffer name does not match | token match on the name | `CTP_Master_Buffer` | exists (NAME_MISMATCH) |
| A10 | holiday or downtime record | machine available 24×7 | — | exists |
| A11 | GT code differs between masters for one SKU (5103 vs 5083) | use the code in the **curing** master, because that is what the press consumes | curing master | new |
| A12 | line speed or rate given only for one line (PCR or TBR) | use the same value for the other line | routing | new |

**Rules for assumptions**

* All assumptions go to one register, `ASSUMPTIONS.csv`, per run. Its columns are: code, item, department, gap, value_used, source, skus_affected, tyres_affected, confidence (high / medium / low).
* Every assumed value is written in the schedule output, on the rows that use it (`assumption_codes` column).
* A run may not invent a value with no source. If no stand-in exists, the case is a blocker (A3).

### A3. What blocks (data blockers)

These have no safe stand-in. Inventing them would schedule tyres the plant cannot make.

| code | missing data | why it cannot be assumed | owner |
|---|---|---|---|
| B01 | **no BOM** for the SKU in `CTP_Master_BOM` | the components and quantities are unknown | master data (BOM) |
| B02 | **green tyre code missing** in PPDS (for example GT 1422) | the building item is unknown | master data |
| B03 | **no allowable press / no mould** for the SKU | cannot cure | curing / mould planning |
| B04 | **no curing cycle time and no plan time** | cure duration unknown | curing master |
| B05 | a BOM component with **no routing, no stock and not purchasable** | cannot be made or bought | master data |

**Partial blocks.** If only some lots of an SKU are blocked (for example 58 TBR tyres in v38), only those lots are excluded.

**GT stock of a blocked SKU.** If a blocked SKU has green tyres in stock, the press may still cure them. Those tyres are shown in the report as "cured from stock, SKU data-blocked" and are **not** added to the final figures, which keeps numerator and denominator consistent.

**Blocked demand does not hold capacity.** Its presses and mould slots are released to other allowable SKUs.

### A4. What `VALIDATION_REPORT` contains (written every run)

File: `outputs/<run>/VALIDATION_REPORT.xlsx`, plus `VALIDATION_REPORT_blocked.csv` and `ASSUMPTIONS.csv`.

| sheet | content |
|---|---|
| Summary | Plan tyres, data-blocked tyres, **eligible tyres**, cured, **fulfilment % of eligible**, fulfilment of assumed lots, per line. One line states: "N tyres on K SKUs excluded for missing data; see Blocked SKUs" |
| Blocked SKUs | SKU, line, tyres, blocker code, missing record (table + key), owner, first day affected, GT stock cured anyway |
| Blocking records | One row per missing record, with the number of SKUs and tyres it blocks. Sorted by tyres, so master data fixes the biggest first |
| Assumptions | The A2 register: code, item, value, source, SKUs and tyres affected, confidence |
| Assumed tyres | Cured tyres that depend on at least one assumption, by code |
| Schedule checks | The gates from Part C (overlaps, duration correlation, zero-gap mould changes, duplicate lot IDs, …), with PASS/FAIL |
| Run stamp | Run id, rules file, data folder, WIP and GT snapshot times, git commit |

All final reports (`FULFILMENT_*`, the schedule workbooks, `make_validation_result.py`, the floor schedule summary) read **eligible demand** from this report, never the raw plan.

### A5. v38 restated under this policy (measured by the v39 code on the v38 outputs)

| line | plan | data-blocked | eligible | cured | fulfilment (v38 as reported) | fulfilment of eligible |
|---|---|---|---|---|---|---|
| PCR | 134,998 | 596 (1 SKU: 1D25214012008QXPC0, B01 no BOM) | 134,402 | 83,270 | 61.7% | **62.0%** |
| TBR | 35,480 | 8,212 (3 SKUs, B01 no BOM) | 27,268 | 17,940 | 50.6% | **65.8%** |
| Total | 170,478 | 8,808 | 161,670 | 101,210 | 59.4% | **62.6%** |

Not blocked, and so kept in the figures:
* The 4 small TBR SKUs (92 tyres) that are in the curing plan but not in the building plan. They have a BOM and a GT routing, so they could cure from GT stock. They lose their tyres because of a code gap (step 5), not missing data.
* The 58 tyres of 112222982C016JUXE0 that had no green tyre.

---

## Part B: implementation order

Each step is a separate version, so a regression can be traced to one change. Every version has its own `config/rules_vNN.yaml`, which inherits from the one before. Run both lines each time with:

```bash
CTP_DATA=data-ppds-v38 python run.py --pcr <pcr plan> --tbr <tbr plan> --run e2e_vNN
```

Use the same data folder (`data-ppds-v38`) for steps 0–10, so that a change in results comes from the code, not the data. Refresh data only at step 11.

### Step 0: safety net (before any change)

1. Freeze `outputs/e2e_v38` as the baseline. Do not overwrite it.
2. Create `tests/`, with one test per gate in Part C. They read a run folder and return PASS/FAIL. Start from the checks already in `audit_v38/audit_v38.py`.
3. Add `tools/compare_runs.py <runA> <runB>`: cured by line, by day, by SKU, plus each gate, side by side.
4. Gate: the tests run on v38 and reproduce the known failures (300 overlaps, −0.035 correlation, 35 zero-gap changes, 58 duplicate IDs).

### Step 1 (v39): data-blocker policy, assumption register, VALIDATION_REPORT

This comes first, so every later number is measured on the right base.

1. **phase0_validate.py**
   * Extend the per-SKU result (`eff`, lines ~126–130). Add `state` (CLEAN / ASSUMED / DATA_BLOCKED) and `blocker_code`, from the existing `ok` / `ok_assumed` flags and findings. `no_bom` → B01. GT not in PPDS → B02. `no_machines` / `no_proc_time` already handled by A01/A02 → ASSUMED.
   * Add the curing-side checks: allowable press, mould and cycle time per SKU (B03, B04).
   * Write `phase0_sku_status.csv` with columns sku, line, tyres, state, blocker_code, missing_table, missing_key, assumption_codes.
2. **ctpio.py**
   * Make `ASSUMPTIONS` the single register for A01–A12. Each entry also carries `confidence`.
   * Add A04, A08, A11 and A12 where the gap is first met (FRC rate lookup, WalkCache descent, GT mapping, routing load).
3. **phase7_curing.py.** Join `phase0_sku_status.csv`, and write `state` and `blocker_code` on each cure lot. Do not schedule DATA_BLOCKED lots, except from GT stock (marked `from_stock_blocked`).
4. **New `engine/make_validation_report.py`.** Builds the sheets in A4. Add it to `run.py` as a required stage, right after `make_fulfilment_report.py`. If it fails, the run fails.
5. **make_fulfilment_report.py**, `make_ctp_schedule_workbook.py`, `make_validation_result.py`, `br_compliance_v15.py`
   * Use eligible demand as the denominator.
   * Show the blocked tyres on a separate line: "excluded, data-blocked: N".
6. Gate: the v39 figures match the A5 table (62.0% / 65.8% / 62.6%). The schedule itself is identical to v38, because nothing else has changed.

### Step 2 (v40): building overlap and duration bug (#4, #5)

1. In `phase5_placement.py`, replace the free `span` variable with a column `blots["span"]` (line 1413). Use the column at line 1580 and at every later use. Check with `grep -n "\bspan\b"`.
2. Move the machine-overlap check (line ~1376) to the end of phase 5, after `_bead_gate` (line 1665) and the kit-gate re-derivation, on the frame that is written.
3. If any overlap is found, stop with an error listing the first 10 pairs.
4. Add a unit test: shuffled trolleys of different lengths; the duration per `building_lot_id` must be kept.
5. Gate: 0 overlaps; duration = plan duration on every row; correlation ≥ 0.5. Expect "built in horizon" to drop slightly (PCR about 82,600, TBR about 20,700). That is correct.

### Step 3 (v41): mould change gap on presses (#6)

1. In `phase7_curing.py`, keep the previous SKU per press. In the push loop (lines 191–205), start no earlier than `free[press] + co_gap(prev_sku, sku)`.
2. `co_gap` uses the plan's `CO_Mins`, then the curing changeover master, then A05.
3. Write `co_min` and `co_source` on each cure lot.
4. Gate: 0 SKU changes with a gap below the required changeover. Cured may drop slightly. Record it.

### Step 4 (v41, same version): audit trail (#14)

1. Lot ID with a line prefix (`phase3_lot_sizing.py:672`). Update every reader that parses `lot_id`.
2. `br_compliance_v15.py:291`: read the snapshot time from the data, not from text.
3. A run stamp in every report (from A4).
4. Gate: 0 duplicate lot IDs; `grep -rn "2026-09-26" engine/` is empty.

### Step 5 (v42): green tyre stock by curing GT code (#3)

1. In phase 7, draw GT stock by the curing master's `SKU → GT_Code` (A11 for conflicts) first, then from building lots.
2. Release presses that carry only DATA_BLOCKED SKUs to other allowable SKUs.
3. Gate: unused GT stock = 0, or each unit has a reason. The 92 tyres show as "cured from stock, data-blocked", outside the final figures.

### Step 6 (v43): cold start (#1). Four sub-steps, one run each.

Do them in this order, cheapest and safest first:

| sub-step | change | file | gate |
|---|---|---|---|
| 6a | map WIP on bypassed cut codes to the building input they feed | `ctpio.py` (`bypass_map`), `phase2_wip_netting.py` | `phase2_bypass_wip.csv` shows about 40k PCR / 31k TBR |
| 6b | A08: stop the walk at unrouted components (CAP 66). B616M leaves the kits | `ctpio.py` ~1193 | 0 trolleys with B616M in the kit |
| 6c | kit-complete FEFO: a kit takes stock only if every component is covered, then leftovers go to partial kits | `phase2_wip_netting.py:190` | day 1–3 kits fully covered ≥ 60% |
| 6d | ramp-up `engine.ramp_h` (start at 8). Infeasible pre-open lots re-anchor to the ramp, not dropped | `phase4_cpm.py`, `phase5_placement.py` | day-1 trolley delay < 12 h and not growing |

Compare after each sub-step. Keep a sub-step only if it does not lower cured tyres on either line. If it helps one line and hurts the other, make it a per-line rule (as found in the Sept lever study).

### Step 7 (v44): calender 901 campaigns (#8)

1. A04 for the 4 missing FRC rates (step 1 already registers it; check the values).
2. In `phase5_placement.py:612`, for machines in `campaign_machines: [901]`, use the key `(camp, line, lft, l)`. Keep a campaign while every other lot's LFT is more than `campaign.window_h` (start at 8 h) away.
3. Order within a campaign by the PPDS changeover matrix.
4. Gate: 901 item changes ≤ 60, PCR↔TBR switches ≤ 15, changeover < 40 h, no new lateness on curing. Try `window_h` at 4, 8 and 12 and keep the best.

### Step 8 (v44, same version): TBR bead balance (#9)

1. List the items routed only to 2403 and ask the plant which can run on 2404/2405. Until they answer, do not add routes; this is a data question, not an assumption.
2. Phase 4b: assign each bead lot to the allowable machine with the least committed hours.
3. Run a stealing pass on the bead pool after `_bead_gate`.
4. Gate: 2403 ≤ 90%; fewer kits blocked by apexed bead than 178.

### Step 9 (v45): press re-sequencing (#2)

1. Replace the fixed push in phase 7 with a per-press ready list. The next lot is the earliest planned lot whose GT is ready, within `curing.resequence_window_h` (start at 24 h).
2. Add the mould change cost (step 3) to the choice. Prefer the same SKU, then the oldest GT (#11).
3. Allowable presses only. BR-10 PM stays.
4. Gate: press cascade median ≤ 4 h; 0 non-allowable presses; cured not lower than v44 on either line.

### Step 10 (v46): small SKUs and compound buffer (#12, #13)

1. Shortage term in the dispatch key: SKUs with cured share below `priority.min_fill` (30%) go first within a `priority.bucket_h` (24 h) LFT bucket.
2. `buffer_batches`: 1 per compound per horizon, not 5 per run (`phase3_lot_sizing.py:651-654`). Specials stay at 0.
3. Gate: 0 eligible SKUs with cured = 0. Compound ratio ≤ 1.05×. No line loses tyres.

### Step 11 (v47): reports, rolling replan, fresh data (#7, #10, #11, #15)

1. Purchased report categories (#7). Idle time by cause (#10). GT age watch at 72 h (#11).
2. `carryover.csv` at horizon end, in opening-WIP format. `engine/rolling.py` replans per shift with a frozen zone `engine.frozen_h` (#15).
3. Refresh the data (`refresh_ctp_sql.py --ppds-only`) and run the final version on today's snapshot. The VALIDATION_REPORT shows which blockers master data has cleared since v38.

---

## Part C: gates checked on every run

They appear in the `Schedule checks` sheet of `VALIDATION_REPORT`. A FAIL on the first five stops the run from being published.

| # | gate | target | since step |
|---|---|---|---|
| G1 | building machine overlaps | 0 | 2 |
| G2 | trolley duration = plan duration | 100% of rows | 2 |
| G3 | press SKU changes with gap < required | 0 | 3 |
| G4 | duplicate lot IDs | 0 | 4 |
| G5 | lots on non-allowable press | 0 | 0 |
| G6 | data-blocked tyres excluded from final figures | yes, and listed | 1 |
| G7 | every assumed value in `ASSUMPTIONS.csv` with a source | yes | 1 |
| G8 | day 1–3 kits fully covered | ≥ 60% | 6 |
| G9 | 901 item changes / line switches | ≤ 60 / ≤ 15 | 7 |
| G10 | 2403 load | ≤ 90% | 8 |
| G11 | eligible SKUs with 0 cured | 0 | 10 |
| G12 | green tyres over max age | 0 | 0 |

## Suggested version map

| version | steps | purpose |
|---|---|---|
| v39 | 0, 1 | right base: blockers excluded, assumptions stated |
| v40 | 2 | building times correct |
| v41 | 3, 4 | curing changeovers, clean audit trail |
| v42 | 5 | GT stock used |
| v43 | 6a–6d | cold start |
| v44 | 7, 8 | 901 and bead bottlenecks |
| v45 | 9 | press re-sequencing |
| v46 | 10 | small SKUs, compound buffer |
| v47 | 11 | reports, rolling replan, fresh data |

v39–v42 are about correctness and should not be skipped. From v43 on, each step is a lever: keep it only if it passes the gates and does not reduce cured tyres on either line.
