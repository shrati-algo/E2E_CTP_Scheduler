# 4-roll calender (4RC, machine 901): how the plant runs it vs v82

| | |
|---|---|
| **Plant** | `four_roll_schedule (1).csv`: 3,661 rows, 836 rolls, 30-Sep → 11-Oct. Full plant days used: 01-Oct and 03–10 Oct (02-Oct was a holiday), 9 days |
| **v82** | `latest_schedule/v82/FOURRC_RULES_v82.xlsx`, sheet *901 lots*: 10–19 Oct, 10 days |
| **Masters** | v82 inputs: routing and BOM (`BOM_Routing_9oct`), `CTP_Master_FRC` (wire / dip, spool length), aging master |
| **Tables** | `PLANT_vs_v82_4RC_PROCESS.xlsx`. Script: `compare_plant_4rc_v82.py` |
| **Plant metres** | Worked out from kg consumed ÷ BOM kg per metre (compound, or cord / fabric if no compound) |
| **Earlier one-day comparison** | `PLANT_vs_v72_4RC_COMPARISON.md` (08-Oct only) |

---

## 1. The plant's process (what it does every day)

**A repeating cycle:** one steel run, then 2–4 fabric runs, then the next steel run. The new cord creel is loaded while the fabric runs. Example, 06–07 Oct:

| Time | Runs | Class |
|---|---|---|
| 06-Oct 12:45–22:11 | CPJ120 → CPJ370 → CPJ1218 (same wire 1036S34500) | steel |
| 22:11–01:00 | MONOCHAFER → ECOPOLY350 | fabric |
| 01:00–04:27 | CWBHTCP100 | steel |
| 04:27–07:29 | HTPOLY350 → ECOPOLY350 → CAP 66 | fabric |
| 07:29–13:57 | CWBP60 | steel |
| 13:57–18:54 | ECOPOLY-EFA → ECOPOLY350 → CAP 66 → HIRAD | fabric |
| 18:54–22:33 | CPJ121 | steel |

Key features:
1. **The machine never stops between runs.** Rolls are booked back to back, with 0.03 h idle a day. Stops happen inside rolls.
2. **Fabric fills the creel change.** Old steel end → new steel start takes a **median 169 min** (25–75 %: 122–297 min), with fabric running in between. Five of the 25 cord changes go straight from one cord to another with no gap at all.
3. **The same wire is kept together.** CPJ120 → CPJ370 → CPJ1218 share wire 1036S34500, and CWBHTCP100 + CP31151 share 1036S31100; there are 7 item switches on the same wire.
4. **Fabric items are used as fillers.** ECOPOLY350 is run 15 times in 9 days, often twice a day, at 11.2 km a day. CAP 66, HTPOLY350 and HIRAD are used the same way.
5. **Volume: 45.8 km a day** (24.9 steel + 20.9 fabric), 9 runs and 8 items a day, median run 4.5 km.
6. **Partial creels are reused.** 28 of 48 creel / cord loads are used in more than one run, some over up to 10 days. The plant takes a part-used creel off and puts it back later.
7. **Compound is about a day old when used.** Median age is 15–27 h (S36 53 h, B458 63 h). The plant keeps compound stock ahead.
8. **CAP 66 uses three inputs:** 39.9 t of B616M, 7.8 t of fabric and **51.3 t of CAP STRIP 120**. CAP STRIP 120 is not in our BOM.

---

## 2. v82's process

| Per day | Plant (9 days) | v82 (10 days) |
|---|---|---|
| Metres calendered | **45.8 km** (24.9 steel / 20.9 fabric) | **23.1 km** (11.6 / 11.5) |
| Runs / items | 9.0 / 8.0 | 5.7 / 5.7 |
| Idle between runs | **0.03 h** | **6.9 h** |
| Cord changes | 1.7 | 1.2 |
| Items in more than one run | 0.9 | 0 |
| Same wire loaded twice in a day | 1.1 | 0 |
| Median run length | 4.5 km | 4.1 km |

- v82 follows the **same steel → fabric → steel pattern**: rule 3 is met on 11 of 12 in-day cord changes, and there is always ≥ 240 min for the creel.
- **v82 runs only what demand needs.** The BOM explosion of the plan needs **23.9 km a day** from the 4RC, and v82 makes 23.1 km.
- v82 runs at **routing speed**, and the rest of the day the 4RC waits for compound (start-up 10–13 Oct: 12–19 km a day) or simply has no demand.
- **The plant makes about 1.9× our BOM need.** By item:

  | Item | Plant km / day | BOM need km / day | Ratio |
  |---|---|---|---|
  | CPJ1218 | 8.1 | 2.7 | 3× |
  | ECOPOLY350 | 11.2 | 4.2 | 2.7× |
  | BRTTL | 1.1 | 3.2 | 0.3× |
  | CAP 66 | 3.1 | 3.5 | 0.9× |

  So either our BOM metres per tyre differ from the plant's, or the plant builds stock / feeds other users. This is a key data question.

---

## 3. Rules: plant vs v82

| Rule | Plant (9 days) | v82 (official checker, 10 days) | Comment |
|---|---|---|---|
| 4RC-2 CAP 66 from fabric + B616M only | ✘ also CAP STRIP 120 (51 t) | ✔ | BOM line missing, or rework? |
| 4RC-4 routing line speed | ✘ 7 of 17 items within ±20 % | ✔ 116 / 116 (by construction) | See section 4 |
| 4RC-5 one creel, ≥ 240 min between cords | ✘ 9 of 25 (36 %); median 169 min, 5 changes with 0 min | ✔ 26 / 26 | The plant's creel change is 2–3 h, sometimes none (second creel?) |
| 4RC-9 rule 3, fabric between cord changes | ~ 20 of 25 (80 %) | ~ 22 / 26 (85 %) | Similar |
| 4RC-7 each item in one run per day | ~ 64 of 72 item-days (89 %) | ✔ 100 % | The plant splits filler fabrics |
| v42 same wire together | ~ 10 repeated loads in 9 days | ✔ | The plant reloads a wire in the same day |
| 4RC-8 spool / dip used up | ✘ part-used creels taken off and reused later | switched off (measured only) | Matches the plant's practice |
| 4RC-6 roll ≤ one spool / dip | ✔ (rolls 280–1,110 m) | ✔ | — |
| 4RC-10 no idle inside a load | ✔ 0 idle between runs | ✔ (idle sits between loads) | — |
| Compound aging (max) | ~ 88 of 2,310 uses over max (3.8 %): B176 up to 101 h (max 96), B460 77 h, B370 95 h (max 72) | ✔ 0 | Plant slightly over our aging master |

---

## 4. Line speeds (plant rolls without a stop) vs routing

| Item | Plant m/min (running) | Routing m/min | Plant ÷ routing |
|---|---|---|---|
| CPJ1218 | 83.7 | 40 | **2.1** |
| HTPOLY350 | 72.1 | 42 | **1.7** |
| CPJ120 / CPJ121 / CPJ370 | 64–70 | 35–40 | **1.8** |
| ECOPOLY350 | 69.3 | 42 | **1.6** |
| CWBHTCP100 | 50.3 | 28 | **1.8** |
| HIRAD | 48.4 | 45 | 1.1 |
| CWBHI34 / CP56542 / CWBP60 | 24–35 | 27–37 | 0.9–1.0 |
| CAP 66 / BRTTL / CP31151 | 28–34 | 31–45 | 0.8–0.9 |
| MONOCHAFER / EHT1000 / ECOPOLY-EFA | 20–29 | 27–45 | 0.6–0.7 |

Including the stops inside rolls, the plant's averages are 25–40 % lower.

---

## 5. How to run v82's 4RC closer to the plant

**Data to confirm first (no rule change):**
1. **4RC speeds per item** (section 4). Update `CTP_Master_Routing` where the plant confirms.
2. **Creel change time.** Is it 240 min, or 2–3 h? Is there a second creel (the 5 direct cord-to-cord changes)?
3. **BOM metres per tyre for the 4RC items.** The plant makes about 1.9× our need (CPJ1218 3×, ECOPOLY350 2.7×, BRTTL 0.3×).
4. **CAP STRIP 120 as a CAP 66 input** (51 t in 9 days).
5. **Spool / creel lengths** in `CTP_Master_FRC`.

**Scheduler changes that keep our rules (can be done now):**

| Change | What it does | Rule kept |
|---|---|---|
| **Fabric fillers ahead of need** | Fill the creel-change and idle time with the next fabric lots (ECOPOLY350, HTPOLY350, CAP 66, HIRAD) made ahead of need, within their shelf life and with their compound ready. That is how the plant uses those hours | 4RC rules, shelf life, compound min aging. v58 creel fill tried this but was blocked by shelf life / inputs; with compound available it would work |
| **Compound a day ahead for the 4RC compounds** (B186, B460, B350, B96, B616M) | Removes v82's 6.9 h/day idle waiting for compound. Same idea as B1144 a day ahead | Mixing rules (15 / 24 h cover and repeat gap) |
| **Same wire back to back** | Already done (v42 campaigns) | — |

**Rule changes (your decision):**

| Option | Effect |
|---|---|
| Allow a fabric item in **two runs a day** when it fills a creel change | 4RC-7 one-go would become steel only, as the plant does |
| **Creel time per the plant's figure** instead of 240 min | Shorter cord changes |
| **Max aging per the plant's practice** for 4RC compounds (B176 / B460 / B370) | Only if the plant confirms the aging master is too strict |

The 4RC is not the bottleneck in v82: it needs 45 % of the day at routing speed. Running it more like the plant mostly makes the schedule realistic and builds a fabric buffer. The output gain only comes once compound is available earlier (mixing) and if the plant confirms the BOM metres and speeds.
