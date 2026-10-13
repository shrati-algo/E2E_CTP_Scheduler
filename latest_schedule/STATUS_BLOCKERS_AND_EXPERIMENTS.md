# CTP scheduler — status, blockers, experiments and results

| | |
|---|---|
| **Date** | 2026-10-14 |
| **Default version** | **v98** (`config.yaml` = `config_v98.yaml`) |
| **Dataset** | Plan `plan-12oct`, window 12-Oct 07:00 → 22-Oct 07:00. Opening WIP `JK_CTP_PPDS.CTP_Opening_WIP` 12-Oct 06:31, plus lots from the 17:26 correction made by 07:00. Masters from `JK_CTP_PPDS` (offline copies of 13-Oct 01:04–01:11) |
| **Plan** | 175,628 tyres cured: PCR 140,238, TBR 35,390 |
| **Run-to-run noise** | About ±4 % (±5,000 tyres). Differences under about 3,000 are not significant |

Details per run: [RUN_LOG.md](RUN_LOG.md). Rules per version: [BUSINESS_RULES_LOG.md](BUSINESS_RULES_LOG.md). Assumptions: [ASSUMPTIONS_LOG.md](ASSUMPTIONS_LOG.md).

---

## 1. Where we are (v98)

| | Plan | v98 | % |
|---|---|---|---|
| **Cured, total** | 175,628 | **131,375** | **74.8 %** |
| Cured PCR | 140,238 | 103,491 | 73.8 % |
| Cured TBR | 35,390 | 27,884 | 78.8 % |
| Built PCR / TBR | 139,742 / 35,201 | 105,318 / 29,163 | 75.4 % / 82.8 % |

- **Rules.** Mixing is 100 %, including the K430-2 120 s rule (51 / 51) and the 120 s clean-out after every final (472 / 472). All 901 hard rules are 100 %. Overall: 70 rule checks PASS, 9 PARTIAL, 4 FAIL. The partial and failed checks are soft extruder-grouping targets (BR-21 / 25 / 28 / 32), the plan's own eligibility exceptions, and BR-12 aging at 98.5 %.
- **Integrity.**
  - 23 build blocks are short of GUM STRIP (a data issue).
  - 1 SKU starves.
  - 276 green tyres are scrapped.
  - These were already present in v87.
- **What v98 contains, on top of v87:**
  - **Bought-in items:** CWCH30256 and CWBHI34 (901); TBA827, TCA313, MS43, MS70 and MB100 (compounds).
  - **Mixing:** finals may run on the master mixers; 120 s clean-out after every final; faster plant cycle times for finals.
  - **901:**
    - lots pool at most 24 h (fabric) or 48 h (steel) of demand;
    - a wire load takes lots from at most 2 days ahead;
    - 901 output is re-pegged first-ready-first within shelf life;
    - whole wire loads are left-shifted into idle time;
    - each day's blocks are ordered by need time (`need_mode: steel_first`).
- **Weak days:**
  - PCR 12-Oct at 56 %: the 901 day-1 order puts steel first, so cap strip and BRTTL go to the next day.
  - PCR 13-Oct at 61 %.
  - PCR 18-Oct at 43 %.
  - TBR 14-Oct at 44.5 %.

---

## 2. Current blockers (v98)

Each built tyre is traced back to the input that arrived last, then to the machine that made it. The share is the share of built tyres held up by that machine.

### PCR

| # | Blocker | Share | Machine load | Cause |
|---|---|---|---|---|
| 1 | **901 four-roll calender: sequencing** | **40 %** | 64 % | Not capacity. 79 % of 901's late hours come from **shelf life of a later consumer of the same roll**: one roll feeds consumers days apart, so it is made for the last one and the first ones wait. Another 15 % is the fixed day order (901 idle, ready lot waiting) |
| 2 | **K430-2: compound late** | 15 % | 17 % | Finals placed on this master mixer are made late (order / mixer choice, with its 120 s before-and-after clean-out) |
| 3 | 1101 inner-liner calender | 13 % | 79 % | Nearly full, plus lot order |
| 4 | 1001 Triplex sidewall | 12 % | 74 % | Full on some days, plus lot order |
| 5 | 1201 PCR bead winding | 7 % | 72 % | Lot order |
| 6 | 2001 bias cutter | 5 % | 90 % | Lot order |
| 7 | Day-1 start-up | — | — | Cap strip and edge gum are assumed in stock for only the first 8 h (V85-1). Day-1 PCR depends on the first CAP 66 dip from 901 |

### TBR

| # | Blocker | Share | Machine load | Cause |
|---|---|---|---|---|
| 1 | **1802 belt edge filler** | **39 %** | 94 % | Full (13 m/min routing). **But relieving 1802 alone does not raise TBR** (v102: 1802 at 48 % busy, TBR −342); the limits below take over |
| 2 | 901 | 18 % | — | Same sequencing problem |
| 3 | 2001 bias cutter | 11 % | 90 % | Lot order |
| 4 | Final compounds late (F270 / K310 / K270M / F440) | 17 % together | — | Mostly start-up (days 1–3) |
| 5 | 1803 Quadraplex / 1801 Duplex | 6 % / 4 % | 84 % / 77 % | Nearly full |

### Capacity ceiling ([ceiling_analysis.py](plant-schedule/ceiling_analysis.py), [CEILING_v98.txt](v98/CEILING_v98.txt))

- **No upstream machine lacks capacity for the full plan.** Lots were run earliest-due-first from 07:00 on 12-Oct, ignoring input readiness and changeovers. On that basis, every machine finishes all its lots by the plan's latest finish time; only 1 lot is 0.5 h late.
- **1802 has almost no slack:** it needs 228 of 240 h, so a realistic TBR ceiling is about 95–98 % of plan.
- **Most of the gap (about 44,000 tyres) is timing and sequencing, not capacity.** Late lots pile up on the short-shelf-life cutters (1103 / 1104, 801 / 802, 2101) and bead apexing (201 / 202) behind late 901 and calender output.

### 901 blocking rules ([fourrc_blocking_rules.py](plant-schedule/fourrc_blocking_rules.py))

| Rule that kept a late 901 lot back (v98) | Late lots | Late hours |
|---|---|---|
| Max aging at a later consumer of the same roll | 41 of 52 | 643 of 816 (79 %) |
| Fixed day-plan order (901 idle) | 6 | 125 |
| Dip / wire load held for another lot (4RC-10 hold / wire campaign) | 2 | 35 |
| 901 busy with other lots | 2 | 11 |
| One go a day | 1 | 2 |
| Creel reload 240 min | 0 | 0 |

Compound supply is **not** what blocks 901: almost every late 901 lot had its compound and fabric ready hours to days before it ran.

### Data waiting on the plant

- **1802:** real line speed (plant measured about 10 m/min against 13 in routing) and whether it makes 2 strips at once. BEF001 metres per tyre.
- **901:** speeds (plant 1.6–2.1× routing), creel time (plant about 170 min against 240), and BOM metres (the plant makes 1.9×).
- **GUM STRIP:** stock or rate (23 build blocks short).
- **Cap strip / edge gum:** the start-up assumption (8 h now; 24 h tested).
- **Others:** compound WIP accuracy, CWBP65 routing, BPG01 unit, the SKU without building routing, TBR bead inch per machine.

---

## 3. What has been tried (v85 → v103)

| Run | Change | Cured | PCR | TBR | Result |
|---|---|---|---|---|---|
| v85 | All masters live from `JK_CTP_PPDS`, plan-12oct, WIP 12-Oct shift A | 110,529 | 92,371 | 18,158 | Base |
| v86 | v85 + plant flow from DB actuals + mixer preference + plant cycle times | 112,716 | 91,797 | 20,919 | Evaluation only |
| **v87** | v85 + 17:26 WIP correction + **master compounds assumed unlimited** + batch sizes from routing | **129,259** | 106,764 | 22,495 | Kept (default until v98) |
| v88 | v87 + 901 urgent pull inside the 4RC rounds | 128,848 | 107,031 | 21,817 | No: 901 idle on 12-Oct |
| v89 | v87 + inner-liner compounds 8 h ahead + 901 fabric-only pull | 130,173 | 107,500 | 22,673 | No: noise; 4RC-10 at 96 %; PCR 12 / 13-Oct lost |
| v90 | v87 + CAP 66 first-day protection + B616M 24 h + inner-liner priority | 127,513 | 105,492 | 22,021 | No: starvation 1; PCR 16-Oct 33 % |
| v91 | v87 + plant cycle times for finals + T638 on K430-2 (120 s rule) | 129,309 | 104,215 | 25,094 | Flat (TBR +, PCR −) |
| v92 | v91, faster cycles only | 125,662 | 103,427 | 22,235 | No: PCR day 1 lost |
| v93 | Finals on master mixers, 120 s after every final, faster cycles + skim compounds timed to 901 + CAP 66 rule | 127,255 | 102,442 | 24,813 | No: mixers relieved (21–41 % busy), limit moved to 901 / 1802 |
| v94 | v93 + **two mixing rules relaxed** (4RC skim gap 24 → 12 h; second mixer when cover < 8 h) | 127,812 | 102,749 | 25,063 | No: noise; relaxations switched off |
| v95 | v93 settings with masters **mixed for real** | 115,899 | 96,317 | 19,582 | Check: master mixers at 95–97 % → mixing **is** the limit without the unlimited-master assumption |
| v96 | v95 + faster plant cycles for masters too | 114,769 | 94,954 | 19,815 | No |
| v97 | v87 + **bought-in items** + 901 left-shift | 130,816 | 106,494 | 24,322 | Kept as base for v98 (TBR + from bought-in 901 items) |
| **v98** | v97 + mixing settings again + 901 smaller lots / re-peg / need order | **131,375** | 103,491 | **27,884** | **Default (best)** |
| v99 | v98 + 901 day blocks ready-first | 121,353 | 94,846 | 26,507 | No: day 1 fixed, 15–19 Oct collapsed |
| v100 | v98 + 901 urgent-first | 127,352 | 99,980 | 27,372 | No: day 1 fixed, 16 / 18-Oct collapsed |
| v101 | v98 + urgent-first on day 1 only + cap strip / edge gum 24 h | 124,668 | 98,534 | 26,134 | No: day 1 at 88 %, 16 / 17-Oct collapsed |
| v102 | v98 + 901 lot split by consumer timing + 1802 output ×2 | 126,456 | 98,914 | 27,542 | No: 901 late lots 52 → 44, but dips moved; TBR flat despite 1802 at 48 % |
| v103 | v98 + finals on own final mixers first + cap strip / edge gum 24 h | 128,017 | **105,717** | 22,300 | No: PCR +2,226 (day 1 at 87 %), TBR −5,584 (final mixers back to 89 %) |

Earlier history (v45–v84, other datasets): [docs/version_history_v45_latest.md](../docs/version_history_v45_latest.md), [BUSINESS_RULES_LOG.md](BUSINESS_RULES_LOG.md).

---

## 4. What we learned

1. **Assuming master compounds are available is worth about 15,000 tyres** (v95 / v96 against v93 / v98). With masters mixed for real, the master mixers are 95–97 % busy and mixing is the limit.
2. **Letting finals run on the master mixers removes the final-mixer limit.** v98: mixers 17–44 % busy, all mixing rules 100 %. Forcing finals back onto F270 / K310 (v103) makes them 89 % busy again and TBR drops.
3. **Buying in CWCH30256 / CWBHI34 helps TBR** (v97: +1,827 TBR).
4. **Changing the 901 order moves the dip; it doesn't remove it** (v98 day 1, v99 15–19 Oct, v100 16 / 18-Oct, v101 16 / 17-Oct). The scheduler builds the plan once, step by step, then runs repair passes; it has no look-ahead, so a gain on one day is paid for on another.
5. **901's real limit is shelf-life pegging, not capacity or compound.** One roll feeds consumers days apart. Splitting such lots (v102) cut late 901 lots by 15 % but did not raise the total.
6. **1802 is not the only TBR limit.** Halving its load (v102) left TBR flat; 2001, 901 and the mixers take over.
7. **The day-1 PCR dip is fixed by assuming cap strip / edge gum for 24 h** (v101, v103: 12-Oct at 87–88 %). So far that gain has come with mid-week losses.
8. **Relaxing mixing rules (v94) gave nothing measurable.**

---

## 5. Recommended next steps

| # | Step | Expected | Effort |
|---|---|---|---|
| 1 | **v98 + only the 24 h cap strip / edge gum assumption** (no other change) | Day-1 PCR +3,000–4,000 if the later days hold | 1 run |
| 2 | **Milder mixer preference:** a master mixer takes a final when a final mixer is above about 70 % booked that day | Recover the PCR of v103 without its TBR loss | About 30 min code + 1 run |
| 3 | **Earliest-due-first on 1101 / 1001 / 1201 / 2001**, with family grouping as a tie-break (soft rules BR-21 / 28 reported) | Targets about 30 % of PCR and 11 % of TBR hold-ups | 1–2 days |
| 4 | **901 constraint model** (OR-Tools CP-SAT, already installed): all 901 hard rules as constraints, plus split lots, minimising tyre lateness over the full 10 days | Fixes the 40 % / 18 % 901 share without moving dips; estimate 85–90 % of plan together with step 3 | About 1 day for 901; 1–2 weeks to extend to the cutters / extruders |
| 5 | **Upper-bound / plan feasibility report** | States the best possible under the rules, and which plan rows can't be met and why | 1–2 days |
| 6 | **Plant data confirmations** (section 2) | Required for the last few percent, especially TBR (1802) and 901 timing | Plant |

**Realistic target:**
- current scheduler with tuning: about 133,000–138,000 cured (76–79 %);
- with the 901 solver and due-date sequencing: about 85–90 % (estimate).

100 % of plan is not reachable under the current hard rules and data: start-up (day 1), 1802 with no slack, and shelf-life / one-go rules. The bound report (step 5) would quantify this.
