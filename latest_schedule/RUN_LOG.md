# Run log

One entry per scheduler run, newest first. Add an entry for every run.

**Shared data (from v85):**
- plan `plan-12oct` (PCR 13 / TBR 14);
- window 12-Oct 07:00 → 22-Oct 07:00;
- cure plan 175,628 (PCR 140,238 / TBR 35,390);
- opening WIP `JK_CTP_PPDS.CTP_Opening_WIP` 12-Oct shift A (06:31);
- masters from `JK_CTP_PPDS`.

**Where the details are:**
- full change and rule details: `BUSINESS_RULES_LOG.md`;
- assumptions: `ASSUMPTIONS_LOG.md`;
- daily numbers: `vNN/DAILY_NUMBERS_vNN.md`;
- dips / utilisation / data gaps: `vNN/DATA_GAPS_DIPS_UTILISATION_vNN.xlsx`.

**Standard command (v87 onwards):**

```powershell
$env:CTP_WIP_CORRECTION = "2026-10-12 17:26"; $env:CTP_WIP_CORRECTION_MODE = "add"
python run_v15.py --wip-date 2026-10-12 --wip-time 07:00 --plan-end "2026-10-22 07:00" --plan-dir plan-12oct --name vNN --wip-source mix --offline --compare v87
python make_run_analysis.py vNN
```

`--offline` is used because the DB server was unreachable from 13-Oct ~11:20. The copies saved 13-Oct 01:04–01:11 are used, the same data as v85.

## Default version = v98 (user 2026-10-14)

`config.yaml` = `config_v98.yaml` (131,375 cured). All switches added in v99–v102 are off. The v87 default below is superseded.

## Default version = v87 (user 2026-10-13, after v96)

`config.yaml` is back on the **v87** settings: masters unlimited; finals only on final mixers; no 120 s clean-out after every final (the K430-2 rule M4 stays); routing cycle times (no plant cycles). The v88–v96 switches are all off. v96 config: `config_v96.yaml`.

## Standing settings (user 2026-10-13) — SUPERSEDED by the v87 default above

- **Finals on master mixers:** `final_on_master_mixers.pairs: {"*": [K270M, K430-1, K430-2, F440]}`.
  - The preference sheet gives no final weights on master mixers, so routing batch size and cycle are used.
  - T638 on K430-2 runs at the plant's 335 kg / 205 s.
- **120 s clean-out after every final compound**, on every mixer: `mix_planner.final_cleanout_after_s: 120`, audit **M9**. The K430-2 rule (120 s before and after a final, M4) stays.
- **Faster plant cycle times only:** `plant_mixing_cycles` with `faster_only: true`.
- **Not yet run:** the next run (v93) is the first with all three.

## Summary

| Run | Finished | Change vs base | Cured | PCR | TBR | Rule cells PASS / PARTIAL / FAIL | Kept? |
|---|---|---|---|---|---|---|---|
| v103 | 14-Oct 02:15 | v98 + finals on their own final mixers first (masters only as overflow, margin 2 h) + cap strip / edge gum 24 h | 128,017 (72.9 %) | **105,717** | 22,300 | mixing audit all 100 % | **No:** −3,358 vs v98. PCR +2,226 (12-Oct 87 %), TBR −5,584: only 5–13 final runs overflowed, F270 / K310 back to 89 % busy (mixing capacity root on 14-Oct), master mixers ~0 %. Config back to v98 |
| v102 | 14-Oct 01:00 | v98 + 901 lot split by consumer timing + 1802 output x2 (assumption V102-1) | 126,456 (72.0 %) | 98,914 | 27,542 | — | **No:** −4,919 vs v98. 901 late lots 52 -> 44 (late hours −12 %), 1802 load 95 % -> 48 % but TBR −342 (1802 was not the TBR limit). PCR 12-Oct 58 %, 16 / 18 / 19-Oct 44–53 %. Report step fourrc_checks fixed for split lots. Config back to v98 |
| v101 | 13-Oct 23:00 | v98 + 901 urgent-first on day 1 only + cap strip / edge gum assumed 24 h (was 8 h) | 124,668 (71.0 %) | 98,534 | 26,134 | — | **No:** −6,707 vs v98. Day 1 fixed (PCR 12-Oct 12,199 cured, 88 %) but PCR 16-Oct 20 %, 17-Oct 39 %; 479 green tyres scrapped. Config back to v98 |
| v100 | 13-Oct 21:40 | v98 + 901 day blocks URGENT-FIRST (gap before an urgent block filled only by a ready block that ends in time) | 127,352 (72.5 %) | 99,980 | 27,372 | — | **No:** −4,023 vs v98. Day 1 fixed (PCR 12-Oct 11,322 built) but PCR 16-Oct 22 %, 18-Oct 40 %. Config back to v98 (`day_plan.need_mode: steel_first`) |
| v99 | 13-Oct 20:30 | v98 + 901 day blocks READY-FIRST (most urgent among ready blocks; ready fabric fills creel reloads) | 121,353 (69.1 %) | 94,846 | 26,507 | — | **No:** −10,022 vs v98. Day 1 fixed (PCR 12-Oct built 11,322), but ready blocks overtook more urgent ones: PCR 15–19 Oct collapsed (901 root 33–55 %). Replaced by urgent-first in v100 |
| v98 | 13-Oct 19:20 | v97 + mixing (finals on master mixers, 120 s after every final, faster plant cycles for finals) + 901: lots pool ≤ 24 h fabric / 48 h steel, wire load takes ≤ 2 days ahead, FEFO re-peg of 901 output, day blocks by need time | **131,375 (74.8 %)** | 103,491 | **27,884** | mixing audit all 100 % (M4 51 / 51, M9 472 / 472) | +559 vs v97, +2,116 vs v87. TBR best so far. PCR day 1 lost (12-Oct 4,139 built): need order put steel CPJ121 (ready 18:00) before ready fabric, CAP 66 / ECOPOLY / BRTTL moved to 13-Oct -> fixed in v99. 276 green tyres scrapped (v97 108) |
| v97 | 13-Oct 18:00 | v87 + **bought-in items** (CWCH30256, CWBHI34 on 901; TBA827, TCA313, MS43, MS70, MB100 compounds) + 901 left-shift of whole wire loads | **130,816 (74.5 %)** | 106,494 | 24,322 | see `RULE_ADHERENCE_REPORT_v97.xlsx` | Highest total so far: +1,557 vs v87 (TBR +1,827 from no longer making CWBHI34 / CWCH30256; PCR −270, noise). Left-shift moved only 1 load: 901 lots are held by the previous lot and by shelf-life pegging |
| v96 | 13-Oct 15:37 | v95 + **faster plant cycle times for MASTER compounds too** (124 compounds: 44 finals + 80 masters); masters mixed for real | 114,769 (65.3 %) | 94,954 | 19,815 | see `RULE_ADHERENCE_REPORT_v96.xlsx` | No: −1,130 vs v95 (noise), −14,490 vs v87; PCR 18-Oct 4,402. Config back to v87 (user: v87 is the default) |
| v95 | 13-Oct 16:45 | Standing settings (finals on master mixers, 120 s after every final, faster cycles), all rules, **masters mixed for real** (unlimited-master assumption OFF) | 115,899 (66.0 %) | 96,317 | 19,582 | 71 / 11 / 5 | Check run: shows mixing IS the bottleneck when masters are real (master mixers 95–97 % busy). +5,370 vs v85; config back to masters unlimited |
| v94 | 13-Oct 15:50 | Standing settings + **RULES RELAXED (user):** #1 4RC skim compounds repeat gap 24 h → 12 h; #2 second mixer same day when cover < 8 h | 127,812 (72.8 %) | 102,749 | 25,063 | 71 / 10 / 5 | Relaxations off again: +557 vs v93 (noise), −1,447 vs v87; #1 used 52 ×, #2 never used |
| v93 | 13-Oct 15:05 | Standing settings (finals on master mixers, 120 s after every final, faster plant cycles) + skim compounds timed to 901 lots (`fourrc_due`) + CAP 66 first-day rule | 127,255 (72.5 %) | 102,442 | 24,813 | 71 / 9 / 6 | No: −2,004 vs v87; all mixing rules 100 % (M4 64 / 64, M9 464 / 464); PCR 15-Oct 51 %, 18-Oct 34 % |
| v92 | 13-Oct 14:20 | v91 with plant cycle times only where **faster** (44 finals; 13 slower ones keep routing) + T638 on K430-2 | 125,662 (71.6 %) | 103,427 | 22,235 | 70 / 10 / 5 | No: −3,597; PCR 12 / 13-Oct lost again (CAP 66 dip on 13-Oct); TBR 18-Oct 0 built |
| v91 | 13-Oct 13:45 | v87 + plant cycle times for 57 final compounds (04–11 Oct bookings) + T638 allowed on K430-2 (plant practice, 120 s clean-out before / after) | 129,309 (73.6 %) | 104,215 | **25,094** | 71 / 8 / 6 | Awaiting user decision: total = v87 (+50); TBR best so far (+2,599), PCR −2,549; mixing rules 100 % incl. M4 |
| v90 | 13-Oct 13:05 | v87 + CAP 66 first-day dip protected (B616M 24 h ahead, no next-day lots in the first CAP 66 dip) + inner-liner compounds 8 h ahead | 127,513 (72.6 %) | 105,492 | 22,021 | 70 / 8 / 6 | No: −1,746; starvation 1 (TBPGUM); PCR 16-Oct 33 % |
| v89 | 13-Oct 12:24 | v87 + inner-liner compounds 8 h ahead + 901 fabric-only pull after the day plan | 130,173 (74.1 %) | 107,500 | 22,673 | 69 / 10 / 5 | No: +914 is noise; 4RC-10 96 %; PCR 12 / 13-Oct lost |
| v88 | 13-Oct 12:08 | v87 + 901 urgent pull (steel and fabric, inside the 4RC rounds) | 128,848 (73.4 %) | 107,031 | 21,817 | 69 / 10 / 5 | No: −411; 901 idle on 12-Oct |
| **v87** | 13-Oct 11:40 | v85 + 17:26 WIP correction + masters unlimited + mixing batch sizes from routing (no `CTP_Master_mixing`) | **129,259 (73.6 %)** | 106,764 | 22,495 | 71 / 8 / 5 | **Yes, current config** |
| v86 | 13-Oct 01:22 | v85 + plant flow from DB actuals + mixer preference + plant cycle times (evaluation) | 112,716 (64.2 %) | 91,797 | 20,919 | 68 / 11 / 6 | No: evaluation only |
| v85 | 13-Oct 00:46 | All masters from `JK_CTP_PPDS`, plan-12oct, WIP 12-Oct shift A | 110,529 (62.9 %) | 92,371 | 18,158 | 69 / 11 / 5 | Base for v86–v87 |

Material starvation is 0 in every run listed except v90 (1 group, TBPGUM). Run-to-run noise is about ±4 % (~5,000 tyres).

## Entries

### v95 (13-Oct 16:45): check run (config back to masters unlimited)
- **Why:** user asked whether mixing is still a bottleneck when master compound is not assumed unlimited.
- **Changes:**
  - same as the current config (standing settings, every rule; v93 4RC changes and v94 relaxations off);
  - but `masters_unlimited.enabled: false`: master compounds are mixed on the master mixers again, and finals use them only when they have time.
  - Master WIP: the 06:31 snapshot (80 batches; expired lots not usable).
- **Config:** `config_v95.yaml`.
- **Result:** 115,899 (66.0 %): +5,370 vs v85 (same masters-mixed basis), −11,913 vs v94.
  - PCR 96,317, TBR 19,582; 659 green tyres scrapped.
  - Dips: PCR 14 / 17 / 18-Oct, TBR 14–18 Oct.
- **Mixer load in the window:**

  | Mixer | Masters | Finals | Busy |
  |---|---|---|---|
  | F440 | 205 h | 22 h | 95 % |
  | K270M | 219 h | 11 h | 96 % |
  | K430-1 | 225 h | 7 h | 97 % |
  | K430-2 | 216 h | 14 h | 96 % |
  | F270 | — | 160 h | 67 % |
  | K310 | — | 129 h | 54 % |

  The final mixers are not full: finals wait for their masters. 258 of 1,691 mixing lots finish after the window.
- **Root of late tyres:** PCR K310 32 %, F270 18 %, F440 10 %, K430-2 8 % (**~68 % mixing**), 1101 17 %; TBR F270 21 %, K430-2 18 %, F440 12 % (**~51 % mixing**), 802 16 %, 901 10 %. 901 only 46 % busy, 1802 53 %.
- **Rules:** all mixing rules 100 % (M4 22, M9 513, butyl, IL, one mixer per day 1,409, 15 h / 24 h gap 1,243); cells 71 / 11 / 5.
- **Answer:** with masters mixed for real, **mixing is the bottleneck: master mixing capacity**. The master mixers have no time to spare for finals.

### v94 (13-Oct 15:50): relaxations switched off again (rule-compliant config)
- **Why:** user asked to break two mixing rules to raise fulfilment; no other rule to be broken.
- **Changes vs the standing settings** (v93 4RC changes off):
  - **RULE #1 RELAXED:** `mix_planner.gap_h_fourrc: 12`. The 4RC skim compounds (BOM children of the FRC items: B460, B96, B186, B176, B370, …) may start a new run 12 h after the previous one (rule: 24 h).
  - **RULE #2 RELAXED:** `mix_planner.second_mixer_cover_h: 8`. A compound may run on a second mixer the same plant day when its next lot is needed within 8 h (rule: one mixer per compound per plant day).
- The rule checks M7 / M8 still report every break, so their counts show how often each relaxation was used.
- **Config:** `config_v94.yaml`.
- **Result:** 127,812 (+557 vs v93, −1,447 vs v87); PCR 102,749, **TBR 25,063**; 171 green tyres scrapped; starvation 0.
- **Relaxed rules used:**
  - **#1** M8 shows 52 of 401 repeats inside the normal gap (87 %): the 12 h gap was used 52 times;
  - **#2** never used (M7 one mixer per day 382 / 382 = 100 %; planner counter 0). With six mixers mostly idle, a compound's whole need already fits in one run on one mixer.
- **All other rules unchanged:** M4 66 / 66, M9 480 / 480, butyl / IL / batches 100 %; MPQ 100 %; 4RC 100 % (rule 3 as before); extruder family rules as before. Cells 71 / 10 / 5.
- **901:** wire breaks from "compound not ready" 22 → 18 (others 9); 81 / 138 lots late (median 23 h); idle 88 h (held 38 h, creel 30 h, changeover 20 h).
- **Mixing:** mixers 20–45 % busy, but 199 k candidate blocks by the 15 h gap (non-4RC compounds); mixer idle 970 h.
- **Bottlenecks now:**
  - PCR: **901** is the top root on every dip day (29–47 %), then 1101 inner liner, 1001 tread, F270 / K270M compound timing;
  - TBR: **1802** (27–42 %, 95 % busy), then 802 / 2001 cutters.
- **Verdict:** the gain is within noise. The two relaxations are switched off again; the standing settings stay on.

### v93 (13-Oct 15:05): 4RC changes not kept (standing settings stay on)
- **Why:** user asked to combine the mixing and 4RC changes, with no rule violated.
- **Changes vs v87:**
  - **standing settings:** every final may also run on K270M / K430-1 / K430-2 / F440 (routing batch / cycle; T638 on K430-2 at the plant rate); 120 s clean-out after every final (M9); faster plant cycles only;
  - **4RC:** `mix_planner.fourrc_due` on (skim compound ranked by its 901 lot's start: 901 need − run time − 4 h); `wire_campaign.no_join_first_day: ["^CAP 66-MOTHERROLL$"]`.
- **Rules:** none relaxed; 901 urgent pull off.
- **Config:** `config_v93.yaml`.
- **Result:** 127,255 (−2,004 vs v87, −2,054 vs v91).
  - PCR 102,442 (−4,322 vs v87), **TBR 24,813 (+2,318)**.
  - 311 green tyres scrapped; starvation 0.
  - Day 1–2 fixed: PCR 83 % / 81 %.
  - New PCR dips: 15-Oct 51 %, 18-Oct 34 %.
- **Mixing is no longer a limit:**
  - all six mixers 21–41 % busy (F270 98 h, F440 83, K270M 74, K310 71, K430-1 55, K430-2 50);
  - **M4 64 / 64 and M9 464 / 464 followed**;
  - BRM-01 / 09, one mixer per day, 15 h / 24 h gap: 100 %; MPQ-DB 100 %.
- **Still late:** some compounds ("compound late, mixer not full"). The mixing rules (one mixer per compound per day, the 24 h repeat gap for the 4RC compounds, cover window) decide the timing, not mixer time.
- **901:**
  - 66.6 % busy, 80 h idle (creel 37 h, held 25 h, changeover 18 h);
  - 86 of 144 lots late (median 23 h);
  - 27 wire breaks, 22 "compound not ready" (B460 8, B96 6, B186 5).
  - `fourrc_due` did not cure the breaks.
- **Other limits:** 1802 95 % busy (TBR); PCR 17 / 18-Oct kit-late rows bound by inner liner (1101 full) and tread (1001 / 1002).
- **Rules:** 4RC all 100 % except rule 3 (steel ↔ fabric) 87 %; extruder family rules as before; cells 71 / 9 / 6.
- **Verdict:** 4RC changes (`fourrc_due`, CAP 66 first-day) switched off again; the standing mixing settings stay on (user).

### v92 (13-Oct 14:20): not kept (switches off = v87)
- **Change:** v91 with `plant_mixing_cycles.faster_only: true`: the plant cycle only where it beats routing (44 finals); the 13 slower plant values ignored. T638 on K430-2 kept. Config `config_v92.yaml`.
- **Result:** 125,662 (−3,597 vs v87, −3,647 vs v91); PCR 103,427, TBR 22,235; 249 green tyres scrapped; starvation 0.
- **Rules:** mixing all 100 % incl. M4 (1 run, 100 %), MPQ 100 %, cells 70 / 10 / 5.
- **Why lower:**
  - (1) **PCR 12 / 13-Oct lost again** (55.7 % / 44.7 %). The CAP 66 mother roll moved to 13-Oct 08:40 behind CWBHTCP100: the same 901 day-plan + 4RC-10 hold effect as v88 / v89. The changed compound timing flipped which lots joined the first CAP 66 dip.
  - (2) **TBR 18-Oct 0 built:** all TBR TBMs idle ~1.5 days (18-Oct 01:00 → 19-Oct 12:00–17:00), waiting for TBR belts / chipper on 802 and BEF001 on 1802.
  - The final mixers had slack (86 %), so the losses come from the 901 / 802 sequence, not mixing.
- **Lesson:** day 1 depends on how the first CAP 66 dip is formed. Any change in compound timing can flip it. The v90 CAP 66 first-day rule fixed this; it should be combined with any mixing change.

### v91 (13-Oct 13:45): awaiting user decision (config switches off = v87)
- **Why:** the final mixers are the hard limit (97–98 %). User: take plant-approved cycle times, allow finals on master mixers where the plant does it, and keep the K430-2 clean-out rule.
- **Changes** (switches; `enabled: false` = v87):
  - `plant_mixing_cycles`: FINAL MIXING `proc_time` = the plant's median batch-to-batch time (04–11 Oct bookings, `plant_mixing_cycles_04_10oct.csv`, `build_plant_mixing_cycles.py`). Only pairs with ≥ 20 intervals; 57 compounds, 44 faster and 13 slower; scaled per kg of the routing batch.
  - `final_on_master_mixers: {T638: [K430-2]}`: the only final the plant ran on a master mixer (222 batches in 7 days).
  - `mix_planner.machine_factor "T638|K430-2": 1.034`: the plant's 335 kg / 205 s on K430-2.
- **Rule check:** the 120 s clean-out before and after a final on K430-2 was **already implemented** (mix_planner `co`: any final compound on K430-2) and is checked by `mixing_rule_audit` M4. v87 had no final on K430-2, so M4 had nothing to test.
- **Config:** `config_v91.yaml`.
- **Result:** 129,309 (+50 vs v87).
  - PCR 104,215 (−2,549), **TBR 25,094 (+2,599, 70.9 %, the highest TBR of any run)**.
  - Starvation 0; 173 green tyres scrapped.
- **Mixers:**
  - F270 / K310 busy 87 % / 86 % (v87 98 % / 97 %): ~51 mixer-h freed by the faster plant cycles.
  - Same compound made (2,963 t, 13,345 batches): lots are sized on demand, so the freed time does not add output by itself.
  - K430-2 ran T638: 12 lots / 428 batches / 16.4 h.
- **Other machines:** 1802 busy 93 % (v87 83 %); 901 63 %.
- **PCR dips** 16-Oct 64 %, 17-Oct 46 %: K310 full on those days, 901 sequencing, 1001 full.
- **Rules:**
  - M4 (120 s clean-out around a final on K430-2) checked on 1 run / 14 rows: **100 % followed**;
  - BRM-01 / 02 / 03 / 06 / 09, one mixer per day, 15 h / 24 h gap: 100 %; MPQ 100 %;
  - lower than v87: 1803 family sequencing (BR-32 lot-level 72.7 %, v87 90.9 %), 1001 BR-28 day-level 0 %, 4RC wire-campaign matrix order 80 % (new FAIL row);
  - cells 71 / 8 / 6.
- **Verdict:** total equal to v87 (noise); the mix moves from PCR to TBR. Uses plant-measured data instead of routing for finals. **User to decide** whether to adopt. Config switches are off until then.

### v90 (13-Oct 13:05): not kept
- **Why:** v88 / v89 lost PCR on 12–13 Oct. The 4RC day plan joined next-day lots into the first CAP 66 dip, and the 4RC-10 hold then started the dip on 13-Oct, so cap strip was late. The inner-liner compound priority looked useful in v89 (PCR 15–21 Oct up) but was mixed with the 901 pull.
- **Changes** (switches; empty / old values = v87):
  - `fourrc_rules.wire_campaign.no_join_first_day: ["^CAP 66-MOTHERROLL$"]` (code: phase 5 4RC day plan);
  - `mix_planner.item_priority_h`: B616M 8 → 24, plus B355 / B458 / EFA / GB35 / GB58 / GL6 / GL7 at 8;
  - 901 urgent pull stays off.
- **Rules:** none relaxed. The first CAP 66 dip still runs in one go, just shorter. Mixing gap / cover / shelf life unchanged.
- **Config:** `config_v90.yaml`.
- **Result:** 127,513 (−1,746 vs v87); PCR 105,492 (−1,272), TBR 22,021 (−474); 18 green tyres scrapped.
- **Day 1–2 fixed:** the CAP 66 dip ran at 12-Oct 07:00 (v87 10:05). PCR 12-Oct 86.5 %, 13-Oct 83.3 % (v89 56 % / 45 %).
- **New dip:** PCR 16-Oct 33.4 % (1,612 built) and 17-Oct 53.1 %.
  - All 11 PCR stage-2 TBMs were idle ~1 day on 16 / 17-Oct.
  - Kit-late rows of 15–16 Oct were bound mainly by **inner liner** (IL056, IL039, IL043, IL054: 10.6 k tyres), then tread / sidewall.
  - The dip roots trace to 901 sequencing, the full final mixers (K310 / F270) and 1201.
  - With the final mixers full, the extra priority for B616M (24 h) and the inner-liner compounds (8 h) delayed other compounds. Inner-liner lots still came late, because 1101's grouping and the compound runs did not line up.
- **Rules:**
  - starvation **1** (TBPGUM on 2101, MUST be 0);
  - 4RC v42 wire campaigns 83 % (new PARTIAL / FAIL), 4RC-9 95.7 %;
  - extruder family rules lower;
  - cells 70 / 8 / 6.
- **Verdict:** not kept; config back to v87 (`no_join_first_day: []`, B616M 8, inner-liner priorities removed). The day-1 protection works and can be reused, but only without the extra mixer priorities, which cost more than they gain.

### v89 (13-Oct 12:24): not kept
- **Changes:** inner-liner compounds 8 h ahead; 901 fabric-only urgent pull, once after the last day-plan pass. Config `config_v89.yaml`.
- **Result:** 130,173 (+914 vs v87).
  - PCR 12-Oct 55.7 %, 13-Oct 45.1 %; 15–21 Oct 80–92 %.
  - 85 green tyres scrapped.
- **Rules:** 4RC-7 89 % (relaxed for urgent lots), **4RC-10 96 %**, mixing 100 %.
- **Why the day-1 loss:** traced in phase 5 (`CAP 66-MOTHERROLL_L0001`). The 4RC-10 hold moved the CAP 66 dip to 13-Oct 08:33, so cap strip was ready 13-Oct 16:20, and all 11 PCR stage-2 TBMs were idle from ~15:00 on 12-Oct.

### v88 (13-Oct 12:08): not kept
- **Changes:** 901 urgent pull (steel and fabric) inside the 4RC rounds; one go a day relaxed for urgent lots.
- **Result:** 128,848 (−411). PCR 12-Oct 55.7 %, 13-Oct 44.0 %.
- **Rules:** 4RC-7 94 %, 4RC-9 82 %.
- **Note:** a steel lot pulled into the 12 / 13-Oct gap, then the later passes queued the 12-Oct 901 sequence behind it.

### v87 (13-Oct 11:40): current config
- **Changes:**
  - WIP 06:31 + 17:26 lots made before 07:00 (+111 final lots / 414 batches);
  - masters unlimited (`masters_unlimited`);
  - mixing batch sizes from `CTP_Master_Routing`.
- **Result:** 129,259; final mixers 97–98 % busy; 1802 83 %; building 63 %; curing 63 %.
- **Note:** offline (DB down).

### v86 (13-Oct 01:22): evaluation, not kept
- **Changes:** plant flow from `CTP_Plant_Schedule`, mixer preference and batch weights, plant cycle times.
- **Result:** 112,716 (TBR +2,761 vs v85). Code and config restored to v85 afterwards.

### v85 (13-Oct 00:46)
- **Changes:** all masters live from `JK_CTP_PPDS`; plan-12oct; WIP 12-Oct shift A; assumptions from `plan-12oct/assumption.md`.
- **Result:** 110,529.
