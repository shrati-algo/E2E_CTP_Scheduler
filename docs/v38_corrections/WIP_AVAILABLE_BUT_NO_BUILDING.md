# Why building can't start on day 1 even though there is WIP

Run: **e2e_v45b**. Opening stock: **CTP_Opening_WIP, 22 Oct 2026 shift A (counted at 06:30)**. Plan start: 22 Oct 07:00.

---

## The short answer

The WIP **is** used. The scheduler takes parts from stock for every day-1 trolley.

But a building machine can only start a trolley when **every** part of its kit is there. Stock covers *most* of each kit, not *all* of it. On average:

- a PCR trolley still needs **6 parts made fresh**
- a TBR trolley still needs **7 parts made fresh**

Making a part from scratch takes a long time:

```
mix the compound  ->  rest 4 h  ->  wait for the machine  ->  run  ->  rest 6-8 h  ->  ready for the trolley
```

So the trolley waits for its **last** missing part. That takes about **18–20 hours**, and the start slips into day 2.

> Think of it like cooking a dish. You have 9 of the 10 ingredients in the fridge. You still can't serve it until you go out and buy the 10th.

---

## The numbers

| | PCR | TBR |
|---|---|---|
| Trolleys the plan puts on day 1 | 40 (12,953 tyres) | 27 (2,711 tyres) |
| Parts taken from stock for them | 924 draws, 181 items | 1,139 draws, 121 items |
| Parts still to be made, per trolley | 6 (1 to 13) | 7 (2 to 17) |
| Trolleys with a full kit by end of day 1 | 21 | 7 |
| Trolleys actually started on day 1 | 12 (4,365 tyres) | 4 (402 tyres) |
| Delay caused by **missing kit parts** | **81%** | **74%** |
| Delay caused by the **building machine** (busy, or keeping the plan order) | 19% | 26% |

**Missing kit parts are the main reason. The building machines are a smaller one.**

---

## Which part is usually the last one missing

**PCR**

| Last part to arrive | Trolleys held | Made on |
|---|---|---|
| Calendered ply, mostly **ECOPOLY350** | 16 | 4-roll calender 901 (fully booked) |
| Apexed bead (BA13001, BA14009, BA13003, BA18009) | 15 | apexing machines 201 / 202 |
| Extruded tread | 5 | extruder 1002 |
| Compound **B616M** | 4 | mixer |

**B616M is needed on all 40 PCR trolleys.** It is mixed 07:00–07:54 and must then rest, so **no PCR trolley can start before about 12:00**. The first one starts at 12:04. B616M is in the kit only because of a **routing gap**: the cap strip (CAP 66) has no step of its own in the routing, so its compound goes straight to the trolley.

**TBR**

| Last part to arrive | Trolleys held | Made on |
|---|---|---|
| Pre-assembly (TPA-004, TPA-002, TPA-020) | 12 | 2201 |
| Apexed bead (TBAPBD…) | 6 | 2403 / 2404 |
| Belt edge filler (BEF001) | 4 | 1802 |
| Duplex tread | 3 | 1801 |

**How long these parts wait**

- Most of them start **4–6 h after their compound is ready**. That is just the resting time, the fastest possible.
- Some wait much longer **in a machine queue**:
  - BA13003: 13.5 h on 202
  - TBAPBD005: 18 h on 2404
  - TBAPBD011: 21.5 h on 2403

---

## Why stock doesn't give complete kits

1. **Stock is shared out part by part.** Each part's stock goes to trolleys in plan order, oldest stock first. The scheduler doesn't check whether the rest of that trolley's kit is there. So many trolleys get *most* of a kit, and none gets *all* of it.
   - We tried "complete kits first" in **v42d/e**. TBR lost tyres because the extra making fought for shared machines, so it was dropped.
2. **There isn't enough of each part.**
   - PCR: of 161 kit parts, only 33 are fully covered by stock and 97 only partly.
   - TBR: of 75 kit parts, only 6 are fully covered and 68 only partly.
   - Stock feeds the first trolleys of an item, then runs out.
3. **Some stock can't be used at 07:00.**
   - Some is still resting, too fresh to use yet.
   - Some is already expired: 37 PCR and 6 TBR batches are dead at opening.
4. **The stock count misses material on the machines.** The 06:30 count leaves out whatever was being made at shift change. On the real floor that finishes in the first hours and would complete several kits.
5. **Some TBR parts can't be kept for long.** They have a 24 h shelf life, so they can't be made far in advance.

---

## The building machine's own share (about 20–25%)

Some trolleys **had** their full kit on day 1 but still didn't start:

- 9 PCR trolleys (1,675 tyres)
- 4 TBR trolleys (290 tyres)

Their building machine was busy with earlier trolleys. Or it was waiting for the **next trolley in its planned order**, even though a later trolley's kit was ready. That waiting comes from the rule we chose: **keep the plan's machine-SKU order (A-B-C), only times may shift**.

---

## What would fix it (biggest gain for least effort first)

| # | Fix | Who | What it gives |
|---|---|---|---|
| 1 | Add the missing **CAP 66 / B616M step** to the routing | Data (routing owner) | One compound fewer in every PCR kit. PCR can start earlier than 12:00. |
| 2 | **Add material that is on the machines** at 06:30 to the opening stock | Data | Completes kits that miss just one part |
| 3 | For each building machine's **first planned trolleys**, use stock to finish their kits first | Engine | Full kits on the first trolleys, instead of most of a kit on many |
| 4 | **Mix the slow compounds first**: the ones feeding the last-missing parts (ECOPOLY350 inputs, HS108, A5542, A1031, T15, B1144) | Engine (mix planner) | Several hours saved on those parts |
| 5 | On **day 1 only**, let a building machine start any planned trolley whose kit is ready | Decision needed: relaxes the A-B-C order | Up to 1,675 PCR + 290 TBR tyres on day 1 |
| 6 | **Hold more opening stock** of the missing parts | Plant | The only way to build the whole day-1 plan |

**Engine fixes alone can't make day 1 run fully to plan.** A part that has to be mixed after 07:00 can't reach a building machine before about midday, because of the resting time plus machine time. The engine fixes shorten the wait. Only more stock at 07:00 removes it.

How much extra stock is needed is in `outputs/e2e_v45b/WIP_TARGET_FIRST_2_DAYS_e2e_v45b.xlsx`.

---

## Worked example: SKU 1325215813079TUNE3 ("all its parts are in WIP")

**The question:** every component of this SKU is in the WIP, so why doesn't building start?

**The short answer:** every part is in the stock **by name**, but one part isn't there **in the quantity needed**. That part is the **apexed bead BA13001**. Making the rest took until the next day, for three reasons, and all three are engine rules, not missing data.

### Its day-1 trolleys

| Trolley | Building machine | Tyres | Plan start | Real start | Late by | Last part to arrive |
|---|---|---|---|---|---|---|
| PCR_BLD000000 | 3410 | 201 | 22 Oct 08:45 | 23 Oct 06:08 | 21.4 h | BA13001 |
| PCR_BLD000022 | 3405 | 300 | 22 Oct 18:09 | 23 Oct 15:46 | 21.6 h | BA13001 |
| PCR_BLD000023 | 3405 | 529 | 22 Oct 23:00 | 23 Oct 20:36 | 21.6 h | BA13001 |

The SKU's data is clean: no blocker and no assumption. The building machines weren't the problem either. 3410 started the moment the kit was ready, and 3405 started 1.3 h after.

### Each part of the kit against the stock

| Part | What it is | Needed for the 3 trolleys | In stock at 06:30 | Result |
|---|---|---|---|---|
| TRD 1503 NEO MSIL | tread | 1,030 tyres' worth | 1,064 pieces | ✅ covered by stock |
| SWRS103 | sidewall | 1,251 m | 1,039 m | mostly stock; 213 m made, arrives in time |
| IL007 | inner liner | 1,030 m | 960 m | ✅ covered by stock |
| CPJ120, HTPOLY350 | plies | 403 m, 388 m | 2,049 m, 1,204 m | ✅ covered by stock |
| BW13002 | wound bead | 2,060 pieces | 4,956 pieces | ✅ plenty |
| **BA13001** | **apexed bead** (the wound bead with its rubber filler fitted) | **2,060 pieces** | **288 pieces** | ❌ **only 14%**: enough for 144 tyres of the first trolley |
| **B616M** | cap-strip compound | 156 kg | **not in stock at all** | made 07:00–07:54, so it doesn't hold these trolleys |

The stock is full of **wound beads** but almost empty of **apexed beads**. Each tyre needs 2 apexed beads, so 1,772 of them had to be apexed fresh on machines **201 / 202**. That one part decides when all three trolleys start.

> The batch of 281 apexed beads was counted at **06:28**, two minutes before the stock count. That suggests the apexing machine was running at shift change, and anything still on the machine isn't in the count.

### Why making the missing apexed beads took a whole day

**1. Usable stock of the filler rubber was passed over, and fresh rubber was mixed instead, which came out later.**

- The filler is compound **HS108**. There were **2,200 kg** in stock, mixed 05:25–05:41. It has to rest 4 h, so it was usable from **09:25**.
- The first trolley was planned for 08:45. On paper its beads were therefore due **the evening before**, at 21 Oct 20:45.
- The scheduler only counts stock if it is usable **by the due time**. 09:25 is after the due time, so it marked the stock as "too late" for this trolley and gave it to later trolleys.
- It then ordered **a new 11 kg mix of HS108**. That was mixed at 13:26 and usable at **17:50**, more than 8 hours **later** than the stock it turned down.

The rule should be: use stock if it is ready **sooner than a fresh batch could be made**, not only if it is ready by the due time.

**2. A 7-minute job waited 11 hours in the queue.**

- The beads for the first trolley (114 pieces) take **7 minutes** on machine 202. They could start at 17:50.
- 202 ran **6 other apexing jobs first** and started this one only at **04:51 the next morning**.
- Both apexing machines were standing **idle from about 08:00 to 18:00**, because every day-1 apexing job was waiting for rubber. Then all of them became ready at the same time in the evening.
- The job for the **earliest trolley of the day** didn't get priority over the others.

**3. Day-1 beads were bundled with day-2 beads in one big lot.**

- Trolleys 022 and 023 need 1,658 beads.
- The engine put them into **one lot of 3,718 beads**, together with the beads for two **day-2** trolleys (062 and 063).
- That lot waited on 202 until **09:22 on 23 Oct**, 13.7 h after its inputs were ready.
- It then runs for 4 hours, and the trolleys wait for **the whole lot** to finish (13:19), not just their share.

### What would have got this SKU building on day 1

| # | Fix | Type | Effect on this SKU |
|---|---|---|---|
| 1 | Use stock when it is ready sooner than a fresh batch (here: HS108 from 09:25 instead of 17:50) | Engine (stock rule) | Bead rubber available more than 8 h earlier |
| 2 | Put the apexing job for the earliest trolley at the front of the queue | Engine (dispatch rule) | First trolley's beads at about 18:00 on 22 Oct instead of 04:58 on 23 Oct |
| 3 | Don't bundle day-1 and day-2 beads into one lot on apexing, or let each trolley take its share as soon as it is made | Engine (lot rule) | Trolleys 022/023 no longer wait for day-2 beads |
| 4 | Count apexed beads physically and include beads still on the machine at 06:30 | Data / plant | If the plant really holds more than 288, the problem disappears |
| 5 | Keep about **1,800 apexed beads BA13001** in stock at 07:00 | Plant | All three trolleys could start on time |

### Same SKU in the latest run, v46a

| Trolley | Plan start | Real start v45b | Real start v46a | Still late by |
|---|---|---|---|---|
| PCR_BLD000000 (3410, 201 tyres) | 22 Oct 08:45 | 23 Oct 06:08 | **23 Oct 03:02** | 18.3 h |
| PCR_BLD000022 (3405, 300) | 22 Oct 18:09 | 23 Oct 15:46 | **23 Oct 13:11** | 19.0 h |
| PCR_BLD000023 (3405, 529) | 22 Oct 23:00 | 23 Oct 20:36 | **23 Oct 18:01** | 19.0 h |

About 2.5–3 h better, because the fresh HS108 is now mixed an hour earlier (12:23). But the cause is the same: **apexed bead BA13001**. There are still only 288 in stock against 2,060 needed. All three faults are still there:
1. **The HS108 stock (usable 09:25) is still passed over** for the first trolley, so a new mix is made (usable 16:48).
2. **The 7-minute bead job for the first trolley still waits about 9 h** on machine 202 (ready about 17:00, run at 01:44), behind 6 other apexing jobs.
3. **Trolleys 022/023 still wait for one 3,718-bead lot** that also carries day-2 beads. It runs 07:37–11:34 on 23 Oct.

Fixes 1–3 in the table above (engine steps W10, W11, W12) are what would change this. None of them is in v46a yet.

**This example shows the general pattern:** "the item is in the WIP" isn't the same as "the trolley can be built". Every part needs to be there **in the full quantity, rested, and not given to another trolley**. One part short is enough to stop the trolley. In this SKU, that part was the apexed bead.

---

## Where the evidence comes from

| What | File |
|---|---|
| Day-1 trolleys: kit ready time and real start time | `outputs/e2e_v45b/{pcr,tbr}/phase5_building_plan.csv` (`build_start`, `kit_ready`, `feasible_start`, `blocking_lot`) |
| Parts taken from stock | `phase2_wip_allocations.csv` |
| Parts made, and which trolley each one feeds | `phase3_lot_demand.csv` |
| When each part was made, and what it waited for | `phase5_schedule.csv` + `phase4_edges.csv` |
| Each day-1 kit part checked against stock | `outputs/e2e_v45b/WIP_vs_DAY1_PLAN_e2e_v45b.xlsx` |
| Worked example: opening stock batches (BA13001, HS108) | `data-ppds-v38/opening_wip_pcr.csv` |
| Worked example: apexing queue on 201 / 202 | `outputs/e2e_v45b/pcr/phase5_schedule.csv` (lots `PCR_BA13001_L00001`, `PCR_BA13001_L00002`, `PCR_HS108_L00001`) |
