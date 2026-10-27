# Day 1 (27 Oct): is stock on the rack, and does building happen from it? (v46k)

**Run:** `e2e_v46k`. Plan `plan-27oct/`; stock from `CTP_Opening_WIP`, 27 Oct shift A (06:31).

**Day 1** = 27 Oct 07:00 → 28 Oct 07:00.

**Full report:** `outputs/e2e_v46k/DAY1_WIP_VS_BUILDING_e2e_v46k.xlsx`, made by `engine/make_day1_wip_building_report.py --run e2e_v46k`. Its sheets: *Summary*, *Why not from stock*, *Trolleys* (one row per day-1 trolley), *Parts* (every kit part of every day-1 trolley), *Last-part lots*.

The **kit** is the parts a builder loads: the green tyre's direct BOM children (tread, sidewall, inner liner, plies, belts' rolls, bead, cap strip, edge strip). A part counts as "from stock" when nothing of it has to be made fresh.

## Answer

**Building does use the stock, but no day-1 kit is complete from stock alone.**

| | PCR | TBR |
|---|---|---|
| Day-1 trolleys (planned tyres) | 44 (13,467) | 29 (2,540) |
| Kit part lines from stock | **225 of 364 (62%)** | **318 of 404 (79%)** |
| Median share of a trolley's kit from stock | 60% | 80% |
| Trolleys whose whole kit is from stock | 6 (4 start on time) | 2 (both late) |
| Trolleys on time | **4** | **0** |
| Tyres built on day 1 | 8,413 (62%) | 664 (26%) |

**Why trolleys start late:**

| | PCR | TBR |
|---|---|---|
| **Waiting for a part made fresh** (its lot finished after the planned start) | 21 (median 9.9 h late) | 11 (median 14.8 h) |
| **Builder still on its previous late trolley** (the plan's sequence is kept) | 19 (median 12.2 h) | 18 (median 18.8 h) |

The second group is a knock-on effect. For example, PCR trolley `PCR_BLD000012` has its **whole kit from stock**, but builder 3402 is busy until 00:53 with `PCR_BLD000011`. That trolley waits for tread `TRD 2167 RAN HPE`, whose stock was expired before 07:00, so the tread is made fresh on 1002.

## Why stock does not cover the rest of the kit

Fresh quantity per reason, in each part's own unit:

| Reason | PCR part lines | PCR qty | TBR part lines | TBR qty |
|---|---|---|---|---|
| **Not enough stock:** earlier day-1 trolleys used it first | 53 | 17,238 | 55 | 6,906 |
| **Not enough stock:** this trolley took what was left, the rest is made fresh | 59 | 14,518 | 27 | 3,461 |
| ⚠️ **Stock was ready before the trolley started, but not 24 h before** (the calendered-roll buffer), so it went to later-day trolleys | 19 | 2,788 | — | — |
| All stock expired before 07:00 | 8 | 3,276 | 1 | 184 |
| No stock of the item in the feed | — | — | 3 | 639 |

1. **The rack does not hold a full day.**
   - Day-1 kit need is bigger than usable stock for **70 of 100 PCR** and **32 of 46 TBR** kit items.
   - The biggest gaps: PCR bead apexes (36 lines, 16,555 pieces), sidewalls, inner liners and treads; TBR pre-assemblies, treads and apexed beads.
2. ⚠️ **The buffer rule holds back rolls that are already ready (PCR, an engine issue).**
   - Phase 2 lets a trolley use a batch only if it has rested by **build start − min ageing − buffer**, and the buffer for calendered rolls is **24 h**.
   - ECOPOLY350, HTPOLY350 and EHT1000 rolls produced late on 26 Oct / early on 27 Oct were ready hours before the day-1 trolleys started. They were refused and drawn by day-2+ trolleys instead.
   - The day-1 trolleys then waited for fresh rolls from 901: **6 trolleys have that roll as their last part**, 6–13 h late. ECOPOLY350 alone was made fresh for 1,686 m on day 1, while 2,351 m of ready stock went to later days.
3. **Expired at the plan start:**
   - PCR treads `TRD 2476 SUP MM` (1,268), `TRD 2167 RAN HPE`, `TRD 1995 ROYL KIA` and `TRD 1684 ROYL`; bead apexes `BA14002`, `BA15012`;
   - TBR apexed bead `TBAPBD006`.
4. **No stock in the feed (TBR):** pre-assembly `TPA-011`, treads `TR5054`, `TR5071`.

## Levers

| # | Lever | Expected effect |
|---|---|---|
| 1 | Let opening stock count as ready at the **trolley's own start**: rest only, no extra buffer for stock that already exists | PCR: up to 2,788 m of fabric rolls stay on day 1; 6 trolleys no longer wait on 901 |
| 2 | On day 1 only, let a builder **start a trolley whose kit is complete** ahead of a late one (relax the plan sequence for day 1) | most of the 19 PCR + 18 TBR "builder busy" trolleys |
| 3 | Make the day-1 shortage parts first: PCR bead apex, sidewall, inner liner, tread; TBR tread on 1801, pre-assembly on 2201 | earlier last parts |
| 4 | Plant: confirm the expired tread / bead stock, and the missing `TPA-011`, `TR5054`, `TR5071` stock | data |

## Root cause: why the fresh last parts arrive late

Each part made on day 1 must have its compound **mixed from 07:00**, then rest **4 h**, then wait its turn on the part machine, then rest again before building.

**PCR** (21 trolleys):
- Last parts: fabric rolls on 901, sidewalls on 1001, inner liners on 1101.
- The parts' machines were busy with other lots (37 h in total) or changing over (11 h). Truly idle time was only 6.5 h.
- Example: ECOPOLY350 needs compound **B350**. 30.7 t of the 37.8 t of B350 stock had expired before 07:00, so B350 is mixed on 104 from 07:00–08:18. It rests until 12:18, then queues on 901 behind other lots (2.9 h run + 0.8 h changeover). The roll is ready at 19:03, so **5 trolleys start about 10 h late**.

**TBR** (11 trolleys):
- Almost all last parts are treads on duplex 1801.
- Their compound **T639** has **no usable stock** (2.7 t, all expired), and **44 t is needed**.
- The mix planner places the first T639 lot on mixer 106 only at **16:26**, 9.5 h into the day. It's due at 23:26 on 26 Oct, but sits behind lots due later: S71 (due 28 Oct 17:15), T15 (due 28 Oct 11:23), B370, T5245 and 4 lots of B186.
- So the treads can only start at 21:31, one after another on 1801. The trolleys are **15–20 h late**.
- The mixers were not full: 12–17 h of work each on day 1. **This is a mixing-order issue, not a capacity issue.**

**Builders:** 19 PCR and 18 TBR trolleys whose own kit was ready wait behind the late trolley on their builder, because the plan's sequence on each builder is kept (check G8).
