# The v38 schedule problems, in plain words

This note is for planners and managers. It explains what each problem means on the shop floor and what will change after the fix. The technical detail is in [HOW_TO_CORRECT.md](HOW_TO_CORRECT.md).

## The big picture

The October plan asks for **170,478 tyres** between 22 Oct and 1 Nov. The v38 schedule cures **101,210 (59%)**.

There are three kinds of problems:

* **The schedule is wrong in places.** Some lots overlap on the same building machine, and some mould changes get no time. A planner cannot hand these to the floor. They are software bugs, and they are easy to fix (problems 4, 5, 6, 14).
* **The schedule is short.** The plant has enough presses and builders. They stand idle about half the time, because material does not arrive in time (problems 1, 2, 8, 9, 10, 12).
* **Data is missing.** Some SKUs and machines have no master record, so the scheduler cannot plan them (problems 3, 8, 12, 14).

Think of it as a relay race. Mixing hands to calendering and extrusion, they hand to the bead and building machines, and building hands to curing. In v38 the first runners start late, and the delay is passed down the whole chain. The last runner, the press, has plenty of speed but spends half the race waiting for the baton.

---

## The 15 problems

### 1. A slow start (cold start). **Real, and the biggest problem.**
**What happens.** On 22 Oct morning, the floor has stock, but it is spread thin. There is some tread for many kits but bead for only a few, so almost no kit is complete. None of the 217 kits for the first three days is complete from stock. Builders wait, and the delay grows from about 1½ days to almost 4 days. About **33,500 tyres** are lost in the first three days alone.

Two things make it worse:
* The scheduler cannot see stock that was already cut, because the cutters were bypassed (about 40,000 PCR and 31,000 TBR units).
* One compound (B616M) is wrongly listed as a building input for 414 trolleys.

**The fix.**
* Give stock to whole kits first, so a few kits are complete instead of many half-complete.
* Count the cut stock.
* Fix the CAP 66 routing record.
* Allow a short ramp-up at the start, instead of pretending the floor is at full speed at 07:00.

### 2. "Not enough presses". **Partly true.**
**What happens.** 86 PCR presses and 80 TBR presses (not 64) are enough for the plan. But presses cure only 59% (PCR) and 47% (TBR) of the time, because green tyres arrive late. When one lot is late, every lot behind it on that press waits too, even if their green tyres are ready.

**The fix.** First fix the green-tyre supply (1, 4, 5). Then let a press take the next lot whose green tyres are ready, within one day of its plan, on an allowed press only.

### 3. 8,808 tyres that cannot be built. **Real.**
**What happens.**
* 8 SKUs have no bill of materials in the database, so no building is planned for them, and 16 presses sit idle.
* One SKU's green tyre (1422) is missing from the database.
* One SKU has two different green-tyre codes in two tables.
* 325 green tyres already in stock are not used, because the scheduler looks for them only through building lots.

**The fix.**
* Master data adds the missing records (the current decision is to keep the BOM as it is).
* The scheduler uses green-tyre stock by its own code, so the stock gets cured.
* Idle presses are offered to other SKUs.

### 4. Two lots on one building machine at the same time. **Real, must fix first.**
**What happens.** In 300 places, the schedule puts two trolleys on the same builder at once, 644 hours in total, on all 20 builders. It comes from a software bug: when the scheduler re-sorts the trolleys, each one picks up another trolley's run time. The schedule's own check said "0 overlaps" because it checked too early.

**The fix.** Keep each trolley's time attached to that trolley. Run the check on the final schedule, and stop the run if any overlap is found.

### 5. Run time does not match quantity. **Real, the same bug as 4.**
**What happens.** A trolley of 40 tyres may be given the time of a trolley of 10, and the other way round. Fixing 4 fixes this.

### 6. Mould changes take no time. **Real.**
**What happens.** When a lot is delayed, the next lot on that press starts the moment the previous one ends, even if the SKU and mould change. 35 of 60 mould changes get zero minutes. No floor can do that.

**The fix.** Always leave the mould-change time between different SKUs. The schedule will look slightly smaller, but it will be honest.

### 7. "No cutting section". **Partly true.**
**What happens.** Cutters and slitters were removed on purpose (decision for v33). Material goes straight to building, with the minimum ageing time added. That part is correct. But one report calls 467 cut items "assumed infinitely available", which is misleading, and it lists 8 green-tyre codes as raw materials.

**The fix.** Correct the report labels. If cutting later becomes a bottleneck, add the cutters back as capacity checks.

### 8. The 4-roll calender 901 changes over all the time. **Real.**
**What happens.** 901 serves both PCR and TBR. It runs 166 lots with 165 item changes, and switches between PCR and TBR 71 times. It spends **more time between lots (131 h) than running (125 h)**. It is the last item missing for 83% of PCR kits. 4 of its items have no line-speed record.

**The fix.**
* Run 901 in campaigns: the same item or compound family together, in a planned order.
* Run PCR and TBR on 901 in blocks, instead of switching lot by lot.
* Add the 4 missing line-speed records.

This frees about 60 hours of 901 time in the 10 days.

### 9. TBR bead machines unbalanced. **Real.**
**What happens.** Bead machine 2403 is loaded beyond 100%, while 2404 is at 63% and 2405 at 45%. Apexed bead is the last missing item for 178 TBR kits.

**The fix.** Check which items can also run on 2404/2405 and add them in the routing. Let the scheduler give work to the least-loaded machine.

### 10. Idle presses and builders. **Real, a result of the others.**
**What happens.** Presses are in use 53% of the time, builders 54%. All press idle time is waiting for green tyres. There is no separate fix: it goes down as 1, 4, 5, 8 and 9 are fixed. A new report will show idle time by cause.

### 11. Green-tyre age not checked. **Not true.**
**What happens.** The oldest green tyre waits 85 hours, and none exceed the 7-day limit. 244 wait more than 3 days. Nothing is wrong. We will add a watch list for tyres over 72 hours, so they can be cured first.

### 12. Small SKUs dropped. **Real.**
**What happens.** 9 small PCR SKUs (3,564 tyres) get nothing cured. The plan puts them on 28–31 Oct. Once the early delay piles up, they slip past 1 Nov. Most wait for one 901 item (CPJ1218) that has no line-speed record.

**The fix.**
* SKUs with nothing produced yet get priority over others due at about the same time.
* Add the missing record.
* Ask planning whether these SKUs can start earlier.

### 13. Too much compound. **Partly true.**
**What happens.** Compound made is 1.15× (PCR) and 1.10× (TBR) the need, not 1.56×. The surplus is about 494 tonnes. About 72% of it comes from a rule that adds 5 extra batches to every run.

**The fix.** Lower that to 1 extra batch, or add it once per compound instead of per run. That saves about 280 tonnes and about 45 mixer-hours. Rounding up to whole batches stays, because the mixers work in whole batches.

### 14. Reports show old information. **Real.**
**What happens.**
* One compliance report still shows the 26 Sep stock snapshot instead of 22 Oct.
* 58 lot numbers are used twice, once in PCR and once in TBR.
* 117 items use average rates, because their own rate is missing.

**The fix.**
* Read dates from the data, never as fixed text.
* Add PCR/TBR to lot numbers.
* Send the list of missing rates to master data.

### 15. No plan for the end of the month or for updates during it. **Real.**
**What happens.** About 56,000 tyres are built and 69,000 cured after 1 Nov, and the last cure ends 6 Nov. That work is not handed to the next plan. The plan is made once and never refreshed: 0 of 12 shift updates.

**The fix.**
* Hand the unfinished work to the next plan as opening stock.
* Refresh the plan every shift from the latest stock.
* Keep the current shift's lots fixed, so the floor is not disturbed.

---

## What gets fixed first

1. **Make it correct:** overlaps, run times, mould-change time, report dates and lot numbers (4, 5, 6, 14).
2. **Make it start well:** complete kits first, count cut stock, ramp-up (1).
3. **Unblock the bottlenecks:** 901 campaigns, bead balance, missing records (8, 9, 3, 12).
4. **Tidy up:** compound buffer, report labels (13, 7).
5. **Keep it current:** shift-by-shift refresh and month-end handover (15).

## What we need from the plant and master data

* BOMs for the 8 SKUs (if the decision to keep the BOM changes), green tyre 1422, and the correct GT code for SKU 112222982C016MJXM0.
* Line-speed records for CPJ1218, ECOPOLY350, CPJ120 and MONOCHAFER on 901.
* Routing for CAP 66.
* Which bead items can run on 2404/2405.
* Mould-change time per press type.
* Real rates for 117 items now on averages.
