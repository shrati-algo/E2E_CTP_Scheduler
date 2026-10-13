# How mixing is scheduled — the mixing planner

Code: `mixer-improvement/mix_planner.py`, called from `phases/phase5_forward_placement_v2.py`. Settings: `config.yaml` → `mix_planner` and `butyl_day_block`.
In use since v56; current form v61 / v62. Runs and results: `latest_schedule/BUSINESS_RULES_LOG.md`.

---

## 1. In simple words

**What it does.** Before anything else in the plant is scheduled, the planner decides every master and final compound lot on the six mixers: F270 and K310 make finals; F440, K270M, K430-1 and K430-2 make masters. It steps forward in time like the shop floor:

1. **Whenever a mixer becomes free**, it looks at every compound that mixer may make.
2. **It drops the ones a rule forbids right now:**
   - the compound is already running on another mixer today;
   - it started a run less than 15 h ago (24 h for IL3 / IL5 and the 4RC compounds);
   - its masters are not mixed and aged yet;
   - it would age out before it is used.
3. **From what is left it runs the most urgent compound** — the one whose next section runs out first.
4. **It makes enough of it at once** to cover the next 15 h (24 h for IL / 4RC). The batches run back to back with no changeover, but never more than shelf life allows.
5. **Butyl compounds run once a day as a block**, exactly as the plant does:
   - K430-2, in the evening: friendly → 29MG → ML405 → ML605 → friendly.
   - F270, the next morning, once those masters have aged overnight: friendly → 30G → IL3 → IL5.
   - A 120 s clean-out follows each butyl compound.
6. **A mixer only waits when nothing allowed can run.** The reason is written to `outputs2/phase5_mix_planner_waits.csv`.

**What happens next.** The lots the planner fixes are never moved again. The rest of the plant — 901, the extruders, calenders, cutters and building — is scheduled around them. Each section can start a lot as soon as its compound is mixed and aged.

**Why it was built this way.** Earlier versions cut lots first and then pushed them later to satisfy the 08-Oct mixing rules. Every push rippled down the line, the mixers stood idle, and output fell (v47–v55: 33–66 k cured). Planning the mixers first, with the rules built in, keeps all the rules at 100 % and the mixers busy.

---

## 2. The rules and how the planner meets each one

| Rule (plant rule list 01-Oct / 08-Oct) | How the planner meets it | Check |
|---|---|---|
| One compound on one mixer per plant day (07:00 → 07:00) | the compound is locked to the first mixer it runs on that plant day | audit M7 |
| Not repeated within 15 h (24 h IL3 / IL5 and 4RC compounds); friendly compounds exempt | a run covers 15 h / 24 h of need, and a compound cannot start a new run inside that window | audit M8 |
| Final mixer — butyl-friendly (B1 / B2 / B99) before the butyl block (30G / IL3 / IL5) | the daily butyl block starts with a friendly; user decision 10-Oct, as the plant: one friendly in front of the whole block | audit M1, BR-V2, 3.1.3 |
| Final mixer — 120 s clean-out after butyl | the changeover after any butyl compound is ≥ 120 s, except IL3 → IL5 inside one IL campaign | audit M2 |
| Master mixer — friendly (MB100 / MB200 / XMB98) before and after the butyl block (29MG / ML405 / ML605) | the daily K430-2 block is friendly → butyl masters → friendly | audit M3 |
| IL3 and IL5 together in one go a day | IL3 → IL5 run back to back as one campaign inside the F270 butyl block | audit M5, 3.1.3 |
| Final compound on K430-2: 120 s clean-out before and after | changeover rule in the planner | audit M4 |
| Output in batches; at least 5 batches per lot | whole batches only; prep (friendly) lots are 5 batches | audit M6, BRM-01 |
| Changeovers: 60 s; master ↔ final TR100 + 5 min | changeover rule in the planner | BRM-10, BRM-06 |
| Min / max aging (CTP_Master_Aging) | a lot is usable only after min aging; a run is cut so it is not past max aging (minus 2 h) at its last use; a lot is not made so early that it would age out | phase 5 aging checks |
| No mixer cap | none; the 230 batches per shift per final mixer is a target, not a limit | audit "final batches per shift" |
| MT871-type compounds with short shelf life | cover only what aging allows | — |

The audit in `MIXING_RULE_AUDIT_vNN.xlsx` re-checks every mixing row of every run. All rules have been at 100 % since v57.

---

## 3. How it decides, in detail

### 3.1 Urgency ("who runs out first")

- **Final lot:** its first need, which is when the next section first needs it.
- **Master lot:** the earliest consumer final's need, minus that final's cover window and the master's min aging. So a final run's masters are ready before the run starts.
- **4RC compounds** — skim compounds of the 901 items, and their masters: ranked 8 h earlier (`fourrc_priority_h: 8`).
- **B616M** (CAP 66): a further 8 h earlier (`item_priority_h: {B616M: 8}`).
- **v62 load levelling** (`level_load`): a backward pass over the final mixers, then the master mixers. When a day's need is more than the mixers can make that day, the excess gets an earlier target, so it is made on the days before. The shelf-life floor still stops anything being made too early.

### 3.2 Candidate check when a mixer is free at time t

1. The compound is allowed on this mixer (CTP_Master_Routing). Butyl and friendly compounds are pinned to their block mixer.
2. It is not locked to another mixer this plant day.
3. Its last run started at least 15 h / 24 h ago (friendlies exempt).
4. All its master lots are already planned, and ready (finish + min aging) within 15 min.
5. It is not earlier than its shelf-life floor: last use − (max aging − 2 h) − run time.
6. The most urgent remaining candidate wins. If there is none, the mixer waits until the next time something becomes possible, and the reason is logged.

### 3.3 The run

The chosen compound's lots are taken in need order, back to back, with no changeover. The run stops when:

- the next lot is needed more than 15 h / 24 h after the first; or
- the next lot's inputs are not ready; or
- it would be past shelf life at its last use.

### 3.4 Daily butyl blocks (`butyl_day_block`)

| Mixer | When | Sequence |
|---|---|---|
| K430-2 (masters) | once per plant day, not before 17:00 (`earliest_hour: 17`); masters age overnight, within their 1-day max aging | friendly → 29MG → 120 s → ML405 → 120 s → ML605 → 120 s → friendly |
| F270 (finals) | once per plant day, only when **all** of the block's butyl masters are aged (`start_when_ready`); until then F270 runs other finals | friendly → 30G → 120 s → IL3 → IL5 → 120 s → next |

The friendly is a real lot when one is due. Otherwise the planner makes a 5-batch prep lot.

### 3.5 Hand-over to the rest of phase 5

- Planned lots go into the schedule with their mixer and times, and are marked fixed. The IL campaign pass, butyl day blocks, BRM-09 joins, aging pull, rule repair and final repair all skip them.
- Every other section uses `_material_ready_ns`: a lot starts only when the compound it consumes is mixed and past min aging.

---

## 4. How mixing utilisation was raised

| Version | What changed | Mixer busy h in the window (6 mixers, max 1,440) | Cured by 16-Oct |
|---|---|---|---|
| v42 | lot sizing + placement, then repairs | 962 | 82,719 |
| v47 | 08-Oct mixing rules enforced by pushing lots later | 713 | 65,986 |
| v52 | v42 settings, 09-Oct data | 950 | 73,155 |
| **v57** | **mixing planner** (rules built in, mixers first) | **1,300** | **104,759** |
| v60 | + 4RC spool stock, no idle inside a dip load, B616M priority | 1,306 | **107,314** |
| v61 | + butyl blocks as the plant (friendly at start / end; masters in the evening) | 1,308 | 105,953 |
| v62 | + load levelling of the final / master demand peaks (built 109,487, highest) | 1,299 | 107,223 |

**Where the gain came from:**

1. **No lot is pushed later after it is planned.** In the old flow, pushing lots later left holes nobody could fill (v47–v55). Now each lot is placed once.
2. **Never idle while something allowed can run.** The planner always picks the next possible compound instead of waiting for the planned one.
3. **Cover runs replace many small lots.** One run covers the next 15 h / 24 h, so there are fewer changeovers and the repeat rule holds without waiting.
4. **Masters ahead of finals.** Masters are ranked by their consumer final's need minus cover and aging, so finals rarely wait for a master after start-up.
5. **Butyl blocks don't block F270 (v61).** Before, F270 ran the friendly at 07:00 and waited 4–5 h a day for 29MG mixed the same morning. Now the masters are made the evening before and the F270 block starts only when they are aged. F270 shift A went from 153 to 195 batches.

**What still limits mixing (v61):**

| Limit | Size | Lever |
|---|---|---|
| Start-up (06–07 Oct): almost no master stock (3 master items, about 20 t), so finals wait for masters | ~26 h on the final mixers | real master / final stock at 06-Oct 07:00, or a rolling plan |
| Final mixer capacity: about 1,300 batches a day for 2 mixers; need peaks of about 2,370 batches on 10-Oct and 13-Oct; from 08-Oct the finals idle only 1–2 h a day | finals about 940 batches behind the plan need at plan end | plant cycle times (the plant runs faster than routing, e.g. IL3 86 s vs 130 s) and batch weights in CTP_Master_Routing; more final capacity |
| End of plan: masters idle on 15-Oct (nothing left to make before plan end) | ~25 h | rolling plan |

---

## 5. How better mixing feeds the 4-roll calender (901) and raises its output

901 makes the steel and fabric plies, and needs a skim compound (a final compound) for every lot. The chain:

1. **The planner ranks the 901 skim compounds and their masters ahead** (8 h, plus 8 h more for B616M / CAP 66), and gives them a 24 h cover. So 901 gets a day of compound at once.
2. **901 starts a lot as soon as its skim compound is mixed and aged.** Earlier compound means earlier 901 lots, which means earlier cutting, building and curing.
3. **901 rules then use that time well:**
   - wire / dip loads run together, in whole spools / dips (4RC-6, 4RC-8);
   - one run per item per plant day (4RC-7);
   - fabric runs inside the 240-min creel reload (rule 3);
   - v59: spool top-up counted as stock for the item's later lots, so no metres are made that nobody needs;
   - v60: no idle in the middle of a dip / spool (4RC-10).

**Effect:**

| Version | 901 utilisation | Note |
|---|---|---|
| v42 | 52.5 % | old mixing, compound often late |
| v57 | 66.1 % | mixing planner: compound ready earlier |
| v58 | 67.0 % | 4RC compound priority |
| v59 | 60.2 % | lower by design: about 16 h less spent making metres nobody needs (spool stock) — more tyres cured |
| v60 | 60.8 % | idle inside dip loads 38 h → 7 h |
| v61 | 64.8 % | butyl blocks as the plant |
| v62 | 62.6 % | load levelling; TBR cured 18,235 (highest) |

All 4RC checks are at 100 % from v59 on, except rule 3 (fabric between steel cord changes, 85–88 %). The remaining cases are blocked by shelf life, inputs not ready, or no fabric that day.

---

## 6. Files, settings and test mode

| What | Where |
|---|---|
| Planner | `mixer-improvement/mix_planner.py` (`plan(ctx)` returns stats, planned lot ids, waits) |
| Hook into phase 5 | `phases/phase5_forward_placement_v2.py`, block "v56 MIXER-IMPROVEMENT PLANNER" before "Placing lots" |
| Settings | `config.yaml` → `mix_planner` (cover_h 15, cover_h_long 24, max_wait_min 15, shelf_margin_h 2, changeover_s 60, tr100_s 300, cleanout_s 120, prep_lot_batches 5, fourrc_priority_h, item_priority_h, level_load) and `butyl_day_block` (groups, friendly_mode block, earliest_hour, start_when_ready) |
| BOM / routing source | `config.yaml` → `master_files` (`latest_schedule/BOM_Routing_9oct/*_v0.xlsx`) |
| Wait log | `outputs2/phase5_mix_planner_waits.csv` (mixer, idle from / to, blocked candidates by reason) |
| Idle analysis | `python mixer-improvement/analysis_scripts/mixer_idle.py vNN` |
| Mixing rule audit | `latest_schedule/vNN/MIXING_RULE_AUDIT_vNN.xlsx`, `MIXING_RULE_COMPLIANCE_vNN.xlsx` |
| Mixing review (v60) | `latest_schedule/v60/MIXING_REVIEW_v60.md` |
| Planner only (about 1 min, uses the current `outputs2` lots) | `MIXPLAN_ONLY=1 python phases/phase5_forward_placement_v2.py` (writes `outputs2/phase5_mix_planner_only.csv`) |
| Full run | `python run_v15.py --wip-date 2026-10-06 --wip-time 07:00 --plan-end "2026-10-16 07:00" --plan-dir latest_plan_file --name vNN --wip-source mix` |
