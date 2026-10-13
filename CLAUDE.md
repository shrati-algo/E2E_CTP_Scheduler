# CLAUDE.md — JK Tyre CTP end-to-end scheduler: project context and handover

| | |
|---|---|
| **Document version** | 1.0 |
| **Last updated** | 2026-10-12 |
| **Latest verified code state** | `config.yaml` = **v82** settings. Code includes the v83 / v84 switches, both off |
| **Best verified run** | **v82**: 127,864 cured of 173,324 planned (73.8 %), plan `plan_files_10oct`, window 10-Oct 07:00 → 20-Oct 07:00 |
| **Version control** | **None.** The folder is not a git repository: no branch, no commit hash. Version = folder `latest_schedule/vNN/` + `latest_schedule/BUSINESS_RULES_LOG.md` |
| **Working directory** | `C:\Users\shradha\Downloads\JKT-CTP-SCHEDULER-main\JKT-CTP-SCHEDULER-main` (Windows, PowerShell + Git Bash, Python 3.13) |

**Revision history**

| Doc version | Date | Change |
|---|---|---|
| 1.0 | 2026-10-12 | First handover. History v45 → v84, current state v82 |

> **Verified vs not.** Statements here were checked against code, `config.yaml`, run logs or `BUSINESS_RULES_LOG.md` on 2026-10-12, unless marked **(unverified)** or **(needs plant confirmation)**. Numbers come from the run logs / log entries named.

---

## Contents

1. What this project is
2. Current state (source of truth)
3. How to run
4. Pipeline in one page
5. Rules that must not be broken
6. What limits output (findings)
7. Do not repeat (rejected approaches)
8. Known issues (short list)
9. Instructions for the next Claude session
10. Supporting documents
11. Information that could not be recovered

---

## 1. What this project is

A **heuristic, rule-based end-to-end production scheduler** for the JK Tyre CTP plant, covering:
- **upstream:** mixing on 6 mixers, 4-roll calender 901, extruders, calenders, cutters, bead;
- **downstream:** tyre building and curing, PCR and TBR.

Inputs:
- the plant's **curing and building plan** (Excel), which is the demand ("the drum");
- opening WIP and masters (BOM, routing, aging, changeover, MPQ, FRC), from MySQL databases `JK_CTP_PPDS` / `jkplanning_CTP` or from files.

Output: a timed, machine-level floor schedule plus rule-compliance reports, in the plant workbook format.

**There is no mathematical solver** (no MILP / CP-SAT; checked: no `ortools` / `pulp` / `pyomo` / `gurobi` imports). It is:
- MRP netting;
- lot sizing;
- a precedence graph (DAG);
- critical-path (CPM) time windows;
- forward list-scheduling with a material gate;
- a long chain of rule-specific repair passes;
- a forward simulation for mixing (`mix_planner`);
- curing re-timing on the plan's presses.

So there is **no objective value, optimality gap or solver status.** Quality is measured by business KPIs: cured / built tyres against plan, rule-compliance %, green tyres scrapped. See [docs/optimization_model.md](docs/optimization_model.md).

**Main KPI used throughout:** tyres **cured** inside the plan window, PCR and TBR, against the plan. Per plant day, 07:00 → 07:00.

---

## 2. Current state (source of truth)

> **DEFAULT VERSION = v98 (user 2026-10-14: "move to v98").** `config.yaml` = `config_v98.yaml`: 131,375 / 175,628 cured (74.8 %), PCR 103,491, TBR 27,884. v98 = v87 + bought-in items (CWCH30256, CWBHI34, TBA827, TCA313, MS43, MS70, MB100; `outsourced_items`) + finals on master mixers + 120 s clean-out after every final + faster plant cycles (finals) + 901: lot pool span 24 h fabric / 48 h steel, wire load <= 2 days ahead, FEFO re-peg, left-shift, day blocks by need (`need_mode: steel_first`). Switches added later are OFF (v99–v102 tested, not kept; see `latest_schedule/RUN_LOG.md`). Known v98 issue: PCR 12-Oct 56 % (901 day-1 order). Code changed after the v98 run (new switches, defaults = v98); a re-run on the current code has not been done yet.
>
> **DEFAULT VERSION = v87 (user 2026-10-13, after v96).** `config.yaml` = v87 settings again; the standing mixing settings below (finals on master mixers, 120 s after every final, plant cycles) are OFF. v96 (v95 + plant cycles for masters, masters mixed for real) = 114,769, not kept; config `config_v96.yaml`.
>
> **Update 2026-10-13 (later): v87 is the current config.** It is v85 plus three changes:
> - the 12-Oct 17:26 WIP lots made before 07:00 (env `CTP_WIP_CORRECTION="2026-10-12 17:26"`);
> - master compounds unlimited (`masters_unlimited`);
> - mixing batch sizes from `CTP_Master_Routing`, not `CTP_Master_mixing`.
>
> Result **129,259 / 175,628 cured (73.6 %)**: PCR 106,764, TBR 22,495. The run was offline (DB down; copies from 13-Oct 01:04–01:11).
> Report: `latest_schedule/v87/DATA_GAPS_DIPS_UTILISATION_v87.xlsx` (`python make_run_analysis.py vNN`). The v85 config is `config_v85_before_v87.yaml`.
>
> **STANDING SETTINGS from 13-Oct (user: all runs until said otherwise; now on in `config.yaml`, not yet run):**
> - final compounds may run on the master mixers K270M / K430-1 / K430-2 / F440 (`final_on_master_mixers "*"`; routing batch / cycle; T638 on K430-2 at the plant rate);
> - 120 s clean-out after EVERY final compound (`mix_planner.final_cleanout_after_s`, audit M9), plus the K430-2 rule (120 s before and after, M4);
> - faster plant cycle times only (`plant_mixing_cycles.faster_only`).
>
> **v93** (first run with them, plus 4RC compound timing + CAP 66 rule): 127,255 cured. Mixing is no longer a limit (mixers 21–41 % busy); limits are now 901, 1802, 1101 and 1001 / 1002. The 4RC changes were switched off again. The pre-change config is in the session scratchpad (`config_v87_before_v93.yaml`); `config_v87_before_v88.yaml` in the repo is the v87 config.
>
> **v95 (check):** same settings with masters mixed for real: 115,899. Master mixers 95–97 % busy, so mixing is the bottleneck again without the unlimited-master assumption. Config back to masters unlimited.
>
> **v94 (mixing rules relaxed by the user, then switched off):** 4RC skim gap 12 h + second mixer gave 127,812 (+557 vs v93, noise). The bottleneck is 901 sequencing and 1802, not mixing.
>
> **v92 (NOT kept):** faster plant cycles only + T638 on K430-2 = 125,662 (−3,597). The PCR day-1 CAP 66 dip was lost again; TBR 18-Oct 0 built.
>
> **v91 (awaiting user decision, switches off):** plant cycle times for finals + T638 on K430-2 (120 s clean-out rule M4 100 %). Result 129,309 (= v87): PCR −2,549, TBR +2,599 (25,094, best TBR). Config `config_v91.yaml`.
>
> **v90 (tested, NOT kept):** CAP 66 first-day dip + B616M 24 h + inner-liner priority gave 127,513 (−1,746) with starvation 1. Day 1 was fixed but PCR 16-Oct collapsed. **All runs are listed in `latest_schedule/RUN_LOG.md`.**
>
> **v89 (tested, NOT kept):** inner-liner compounds 8 h ahead + 901 fabric-only pull: 130,173 (+914, noise), 4RC-10 96 %. PCR 12 / 13-Oct lost: the 4RC load-compact hold delays the CAP 66 dip. Config saved as `config_v89.yaml`; `config.yaml` = v87.
>
> **v88 (tested, NOT kept):** v87 + the 901 urgent pull (`fourrc_rules.urgent_pull`, now `enabled: false`) gave 128,848 (−411). It lost PCR 12 / 13-Oct: 901 ran nothing on 12-Oct. See the log.
>
> **Earlier the same day: v85 was the current config.** It differs from the v82 description below in five ways:
> - **every master comes live from `JK_CTP_PPDS`** (`master_files: {}`);
> - **mixing batch weights come from `CTP_Master_mixing`** (`mixing_batch_source: ctp_inputfiles/mixing_master_live.csv`);
> - **plan `plan-12oct`, WIP 12-Oct shift A, window 12–22 Oct**;
> - **assumptions from `plan-12oct/assumption.md`**: first-hours assumption 8 h, cap strip + edge gum only, no untracked codes, `expired_usable_h: 0`;
> - result **110,529 / 175,628 cured (62.9 %)**, with a 17-Oct dip.
>
> **v86 (evaluation only, not adopted):** plant flow from DB actuals, mixer preference and plant cycle times gave 112,716 (TBR +2,761). Code and config were restored to v85 afterwards.
> **Every run's assumptions are logged point by point in `latest_schedule/ASSUMPTIONS_LOG.md`; add an entry for each new version.**
> See the v85 entry in `BUSINESS_RULES_LOG.md` and `latest_schedule/v85/DAILY_NUMBERS_v85.md` / `DB_MASTER_CHECKS_v85.md`. The pre-v85 config is saved as `config_v82_before_v85.yaml`. v85 is not comparable with v82 (different plan, WIP and masters).

| Item | Value (verified 2026-10-12) |
|---|---|
| Config | `config.yaml`, settings of **v82**. Backups of earlier configs are in the session scratchpad, not in the repo |
| Best run | `latest_schedule/v82/`. Command in section 3 |
| v82 result | Cured **127,864** (PCR 106,348 / TBR 21,516); built 127,174; 541 green tyres scrapped; rule adherence 68 PASS / 9 PARTIAL / 4 FAIL per the log entry. A raw count of PASS / PARTIAL / FAIL cells over both sheets of `RULE_ADHERENCE_REPORT_v82.xlsx` gives 69 / 10 / 5; use the same counting method when comparing. Daily table: `latest_schedule/v82/DAILY_NUMBERS_v82.md` (127,866 there, rounding of the day split) |
| Plan | `plan_files_10oct/` (PCR 12 / TBR 13), window 10–20 Oct, 173,002 build / 173,324 cure |
| Opening WIP | `CTP_Opening_WIP` snapshot 10-Oct 06:32 (components) + old feed `jkplanning_CTP.opening_wip` (compounds, green tyres), `--wip-source mix` |
| BOM / routing | **Files** `latest_schedule/BOM_Routing_9oct/*.xlsx` (config `master_files`), not the DB. User decision 09-Oct |
| WIP assumptions | `ctb_wip_assumptions` ON (`CTB_WIP_ASSUMPTIONS_v2.xlsx`), `kit_gate_assume_first_h` ON (v79 / v80) |
| Last two experiments | v83 (item-stock allocation) and v84 (4RC closer to plant): **both NOT kept**; config reverted to v82 |
| Latest tooling | `daily_numbers.py` (new) and `run_v15.py` (now saves `run_log.txt` itself and runs `daily_numbers.py`; **not yet exercised in a full run**). User guide: [RUN_YOUR_OWN_VERSION.md](RUN_YOUR_OWN_VERSION.md) |

**Why v82 is the baseline:** it has the highest cured total on the current dataset (10-Oct plan + 10-Oct WIP), with all mixing rules ~100 % and MPQ 0 below. v83 / v84 on the same data were lower or flat. Results on other datasets (06-Oct plan, v56–v77) are **not comparable** (different plan, WIP and window).

**Run-to-run noise:** about ±4 %. v67 showed near-identical mixing plans differ by this much through downstream cascades. Differences < ~2–3 k cured are not significant.

---

## 3. How to run (verified commands)

PowerShell, from the project folder. The DB login is in `db_pipeline/.env`. **Never print it.**

```powershell
# reproduce the current baseline (v82 config); about 45-60 min
python run_v15.py --wip-date 2026-10-10 --wip-time 07:00 --plan-end "2026-10-20 07:00" --plan-dir plan_files_10oct --name vNN --wip-source mix --compare v82

# daily numbers (plan / built / cured per plant day), any existing version(s)
python daily_numbers.py v82 v84

# re-run the reports that read run_log.txt (needed for runs made before run_v15 saved its own log)
python general_rules_checks.py latest_schedule/vNN/run_output --xlsx latest_schedule/vNN/GENERAL_RULES_vNN.xlsx
python rule_adherence_report.py vNN
python make_version_readme.py vNN

# plant comparisons / checks
python latest_schedule/plant-schedule/check_algo_planning_rules.py vNN plan_files_10oct
python latest_schedule/plant-schedule/compare_plant_4rc_v82.py
```

**Before this session's change to `run_v15.py`**, the run log was copied by hand:
1. `grep -v -i password <log> > latest_schedule/vNN/run_log.txt`;
2. then the three report commands above.

`run_v15.py` now writes `run_log.txt` itself (lines with "password" left out).

**Phase 5 alone** (debugging; minutes, not an hour):
1. copy `latest_schedule/vNN/run_output/PHASEWISE/PHASE*/*` and `run_output/*.csv` into `outputs2/`;
2. `PYTHONUTF8=1 python phases/phase5_forward_placement_v2.py`.

Full user guide (DB vs custom files, `master_files`, offline runs, outputs): [RUN_YOUR_OWN_VERSION.md](RUN_YOUR_OWN_VERSION.md).

---

## 4. Pipeline in one page

`run_v15.py` runs these steps in order (verified from code):

```
build_sep_inputs.py           DB / master_files -> ctp_inputfiles/*, db_pipeline/raw/*  (plan files cut to the window, WIP snapshot, masters)
adapt_inputs.py               -> inputs/*.csv (plan.csv, building_plan.csv, bom.csv, routing.csv, inventory.csv, ...)
build_mpq_items.py            item-level MPQ
apply_biascutter_calibration.py, apply_routing_gapfill.py
apply_ctb_approximations.py   v74 TBR bead one step, v77 alias, v80 WIP assumptions (inventory.csv rewritten in place)
run_scheduler.py --offline    -> run_pipeline.py: phase0 -> 1b -> 1_5 -> 1c -> 2 -> 3 -> 4 -> 5 -> 6, then closed-loop rounds 2..3 (phases 5-6 again)
reports: capacity_wall, audit_business_rules, make_schedule_v18 (workbook), mixing_compliance, fourrc_checks,
         fourrc_sequence_report, quintoplex_checks (x4: quintoplex / duplex / triplex / quadraplex), validate_build_cure,
         mpq_checks, general_rules_checks, mixing_rule_audit, rule_adherence_report, make_version_readme, daily_numbers
```

| Phase | What it does |
|---|---|
| **phase1b** | Explodes the curing plan through the BOM |
| **phase1c** | Nets opening WIP, time-phased, first-expiry-first-out |
| **phase2** | Makes lots: MPQ, whole mixing batches, BRM-13 +5 buffer batches, 4RC roll lengths |
| **phase3** | Links producer lots to consumer lots, with FEFO consume quantities |
| **phase4** | CPM windows (lag = min aging + transfer) |
| **phase5** | 13,195 lines; does almost everything. Mixing planner first (fixed lots) → forward placement with the material gate → 4RC day plan / creel / wire campaigns → extruder / calender / cutter / bead grouping passes → building "echo" with a physical kit ledger → aging pulls → repairs → v81 / v82 gap fill → final repair → C4 ledger proof |
| **phase6** | Re-times the plan's curing blocks on the **same presses** to when green tyres are ready; keeps the mould-change gap (BRBC-27); green tyres need 2 h (BRBC-21) and expire at 72 h |

Details: [docs/scheduling_flow.md](docs/scheduling_flow.md), [docs/architecture.md](docs/architecture.md).

---

## 5. Rules that must not be broken (user decisions, standing)

| # | Rule |
|---|---|
| 1 | **Never print or paste DB credentials**; filter "password" from any output. Read-only DB access only (`db_pipeline/db_connect.py`). |
| 2 | **No mixer cap.** 230 batches / shift / final mixer is a *target* (report only). |
| 3 | **BOM / routing from `latest_schedule/BOM_Routing_9oct`** (config `master_files`) until the user says otherwise. Don't change routing mixing data except in explicit evaluation runs (`relaxed_mixing` stays off). |
| 4 | **Don't declare any version a "baseline"** in logs. Report the best version; the user decides. |
| 5 | **Keep the plant workbook format** (`make_schedule_v18.py`, `output-format/CTP_e2e_v18_SCHEDULE.xlsx`). |
| 6 | **Log every version** in `latest_schedule/BUSINESS_RULES_LOG.md`: change, result, rules, kept or not. |
| 7 | **Shelf life / aging of genealogy is report-only** (user choice); the scheduler itself enforces min / max aging. |
| 8 | **4RC makes demand metres only** (`whole_spool_topup: false`, user choice v76). |
| 9 | "Don't implement, just explain" means exactly that. When asked to stop runs, stop **all** python run processes. |
| 10 | **Never edit `config.yaml` with PowerShell `Get-Content \| Set-Content`.** It damaged the UTF-8 encoding once. Use the Edit tool or Python with `encoding='utf-8'`. |
| 11 | Don't scan the user's home folder. |
| 12 | Business rules (mixing 01-Oct / 08-Oct sets, 4RC rules, extruder / calender / cutter / bead rules, building / curing BRBC rules) must be kept. A change that lowers a rule's compliance must be reported. |

Full rule table with code locations: [docs/business_rules.md](docs/business_rules.md).

---

## 6. What limits output (verified findings)

1. **Mixing is the binding constraint.** At the target (13,500 PCR + 3,500 TBR a day), finals need 92 % and masters 94 % of 24 h before changeovers. v82 masters already run 92–93 of 96 h a day. The start-up loss on 10–11 Oct (~280 t of finals) is never recovered.
   - Source: `latest_schedule/v82/CAPACITY_vs_TARGET_v82.md`.
2. **Start-up (cold start).** There is almost no usable master compound in the opening WIP (21.9 t; 72 % of opening compound "expired" by the aging master). Multi-stage master chains (e.g. MT4333H → MT4444H with 24 h min aging → T4555H) need ~2 days. The plant has confirmed the compound WIP is wrong.
3. **TBR ceiling = belt edge filler 1802.**
   - At routing 13 m/min it needs 26 h/day for 3,500 TBR, so it can supply at most ~3,200 TBR a day.
   - The plant runs ≈10 m/min (measured: 150 m rolls of 37.5 kg every ~15 min).
   - Source: `latest_schedule/plant-schedule/BEF_1802_LINE_SPEED_ANALYSIS.md`.
4. **Component machines are not the constraint** (25–56 % used). Items with 24 h shelf life (belts, ply, apex, cap strip) are deliberately cut just before building. Items with 72 h shelf life are late because their compound is late.
5. **The end-of-window dip** comes from carried backlog. A longer horizon would not raise in-window output (checked).

---

## 7. Do not repeat (rejected approaches; evidence in the log)

| Approach | Versions | Result |
|---|---|---|
| 901 level loading / level plan / ready-first re-plan | v43–v45, v48 | Output fell; 901 is limited by compound supply |
| Mixing rules enforced by pushing lots after placement (gap, catch-up, backlog merge, run groups) | v47–v55 | 20–66 k cured. Fixed by planning the mixers first (v56) |
| Shelf life +30 % | v50 / v51 | −5.3 % cured |
| Live master priority / 901-first compound timing | v66, v68 | Only moves the shortage in time |
| Longer cover runs (20 / 30 h), smaller compound lots | v63, v64 | PCR −5 k / −6 k |
| Kit sync urgent-first | v71 | Conflicts with grouping rules |
| ASAP extruders / bead chain | v73 | TBR −3.2 k |
| Item-stock allocation with opening-stock netting (`repeg_mode: item_stock`) | v83 | Flat (−158), extruder family rules worse |
| 4RC compounds a day ahead + fabric fillers 3 days | v84 | −1,668; no filler candidates exist with demand-only 4RC |
| Netting green-tyre WIP again in phase 1c | analysis only | **Wrong.** The plant's building plan already nets green-tyre stock (cure − build in the first 24 h tracks the stock). The run-log warning "DOUBLE-COUNTED" is misleading |
| Carrying the mixing round-up into the next lot | analysis only | ≤ 676 batches / ~30 mixer-h; needs a lot-linking (DAG) change; risk > gain |
| Longer horizon to fix the end dip | analysis only | Does not raise in-window output |

---

## 8. Known issues (short list; full list in [docs/known_issues.md](docs/known_issues.md))

**Integrity checks the code itself says must be 0 are not 0 in v82:**
- "ledger: 24 blocks short (MUST be 0)";
- "1 SKUs starve -- MUST be 0";
- ~600 lots "still over max aging" after the aging pull.

**Other issues:**
- **Genealogy inconsistency:** e.g. `BI-MONO001_L0001` (12-Oct 12:34) is recorded as consuming `MONOCHAFER_L0001`, which is made 13-Oct 03:39. Links are reservations, not physical issue.
- **Multi-SKU green-tyre lots:** 58 lots serve more than 1 SKU, and the kit is credited to the first SKU only (warning M-1).
- **Data issues awaiting the plant:**
  - compound WIP; mixing cycle times / batch weights;
  - 1802 speed and BEF001 metres per tyre;
  - 4RC speeds, and the 4RC BOM need (the plant makes 1.9×);
  - BPG01 unit error; CWBP65 routing; one SKU with no building routing; CAP STRIP 120 into CAP 66;
  - strip line-speed multipliers (EG01 ×15 = 450 m/min);
  - missing matrix pairs; TBR bead inch per machine.

  List for the plant: `latest_schedule/v82/DATA_ISSUES_FOR_PLANT_v82.md`.
- **Unknown accuracy:** `PHASEWISE_GUIDE.md` and parts of `README.md` describe older (v6-era) behaviour in places. Trust code + config + the log over those guides.

---

## 9. Instructions for the next Claude session

1. **Read this file and the linked docs completely before changing code.** Then read the newest entries (v78 → v84) of `latest_schedule/BUSINESS_RULES_LOG.md`, and the memory index if present (`~/.claude/projects/.../memory/MEMORY.md`).
2. **Verify the repository matches this document.** At minimum:
   - `config.yaml` → `upstream_gap_fill.repeg_mode: compounds`;
   - `mix_planner.fourrc_priority_h: 8`;
   - `fourrc_rules.creel_fill.pull_days: 2`;
   - `fourrc_rules.load_compact.fill_pull_days: 2`;
   - `master_files` has the two `BOM_Routing_9oct` lines;
   - `latest_schedule/v82/` exists.

   If anything differs, ask the user before running.
3. **The source of truth is `config.yaml` + the code, not the highest version number.** v83 / v84 are higher but worse. Experimental switches are left in code with defaults that reproduce v82.
4. **Preserve business rules and working behaviour.** No rewrites, no duplicate modules, no speculative fixes. `phases/phase5_forward_placement_v2.py` is 13 k lines of interacting passes. Put a change behind a config switch whose default reproduces the current baseline, and back the file up first.
5. **Trace impact across stages:**
   - the mixing planner fixes mixing lots;
   - phase 5 passes run in a fixed order, and the final repair re-checks everything;
   - the closed loop re-runs phases 5–6 three times and feeds quantities back;
   - phase 6 depends on the building floor schedule;
   - the rule reports and the workbook read phase-5 / phase-6 outputs **and `run_log.txt`**.
6. **Validate on the same dataset as the baseline** (`plan_files_10oct`, WIP 10-Oct 07:00, window 10–20 Oct). Report:
   - daily PCR / TBR built and cured (`daily_numbers.py vNN v82`);
   - rule adherence (pass / partial / fail), mixing BRM-09, MPQ below, 4RC rule 3;
   - green tyres scrapped, material starvation (must stay 0);
   - the ±4 % noise.
7. **Debug cheaply:** run phase 5 standalone (section 3) before a full ~1 h run. v83's first run collapsed (53 k) from a gate edge case that a standalone check found in minutes.
8. **Never claim a schedule is feasible, optimal or production-ready** without evidence. There is no solver optimality. Rule compliance is reported per check, and several checks are partial or fail.
9. **After every meaningful change:**
   - add a version entry to `BUSINESS_RULES_LOG.md`;
   - make sure `latest_schedule/vNN/run_log.txt` exists and re-run the reports;
   - update this file's section 2 and [docs/version_history_v45_latest.md](docs/version_history_v45_latest.md).
10. **Explain to the user in plain language**, with numbers and evidence. Correct earlier statements when data shows they were wrong; this happened several times. Don't treat a proposed fix as confirmed until a run shows it.

---

## 10. Supporting documents

| File | Content |
|---|---|
| [docs/current_status_and_next_steps.md](docs/current_status_and_next_steps.md) | State, blockers, pending decisions, next steps |
| [docs/architecture.md](docs/architecture.md) | Directories, entry points, modules, data files, config, DB, fragile interfaces |
| [docs/scheduling_flow.md](docs/scheduling_flow.md) | Stage by stage: purpose, inputs / outputs, code, rules, limits |
| [docs/business_rules.md](docs/business_rules.md) | Rules with config keys, code location, hard / soft / report |
| [docs/optimization_model.md](docs/optimization_model.md) | The (heuristic) model: netting, lot sizing, gate, CPM, mixing planner, repairs, curing; formulas |
| [docs/version_history_v45_latest.md](docs/version_history_v45_latest.md) | v45 → v84, version by version |
| [docs/experiment_results.md](docs/experiment_results.md) | Results by dataset, best runs, analyses |
| [docs/known_issues.md](docs/known_issues.md) | Bugs, data issues, technical debt, decisions |
| [RUN_YOUR_OWN_VERSION.md](RUN_YOUR_OWN_VERSION.md) | User guide: running, DB vs custom files, outputs |
| `latest_schedule/BUSINESS_RULES_LOG.md` | **Primary history** (v0 → v84, 1,950 lines) |
| `latest_schedule/plant-schedule/*.md` | Plant vs scheduler comparisons (mixing, 4RC, TDA, belt edge filler, master data, Algo Planning rules) |
| `latest_schedule/v80/GAP_12_15_OCT_v80.md`, `latest_schedule/v82/CAPACITY_vs_TARGET_v82.md`, `latest_schedule/v82/DATA_ISSUES_FOR_PLANT_v82.md` | Key analyses |
| `mixer-improvement/README.md` | Mixing planner |

---

## 11. Information that could not be recovered

- **No git history.** Code diffs per version can't be reconstructed. Code changes are known only from:
  - log entries;
  - backups in the Claude session scratchpad (`phase5_pre_v79/v81/v82/v83.bak`, `config_v74..v83*.bak`), which are **outside the repo** and may be gone;
  - comments in code tagged `vNN (user …)`.
- **v45–v55 code paths** are mostly still in `phase5` behind switches that are off. Their exact original behaviour can't be guaranteed after later edits.
- **Run commands for v45–v77** are not all recorded. The pattern was `run_v15.py --wip-date 2026-10-06 --wip-time 07:00 --plan-end "2026-10-16 07:00" --plan-dir latest_plan_file --name vNN --wip-source mix` (logged for v24–v26). Each version's `run_log.txt` header shows its WIP / plan / window.
- **Solver metrics** (objective, gap, runtime per solve) don't exist: there is no solver. Full run time is ~45–60 min. Per-phase timings are in each `run_log.txt` **(not summarised)**.
- **Plant data confirmations** are still open (sections 6 and 8).
