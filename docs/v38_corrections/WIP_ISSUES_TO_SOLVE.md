# Opening-stock (WIP) issues: list to solve step by step

**Status:** only W19 has been worked on (v46a, cut/slit stock). Everything else is still a plan.

| | |
|---|---|
| Base run | **e2e_v45b** (125,466 tyres cured, 77.6% of the demand that can be scheduled) |
| Opening stock | `JK_CTP_PPDS.CTP_Opening_WIP`, 22 Oct 2026 shift A, counted at 06:30 (`data-ppds-v38/opening_wip_*.csv`) |
| Green tyre stock | `CTP_GT_Inventory`, 22 Oct 06:36 (PCR 5,892, TBR 1,299 tyres) |
| Background | [WIP_AVAILABLE_BUT_NO_BUILDING.md](WIP_AVAILABLE_BUT_NO_BUILDING.md) |

**Day 1 today:** PCR builds 4,365 of 12,953 planned tyres, and TBR builds 402 of 2,711.

**Who owns each issue:** **Data** = the data / master owners, **Decision** = your call, **Engine** = a scheduler code change, **Plant** = floor practice.

---

## How we will work through it

1. **One issue at a time.** Each engine fix becomes its own run (v46a, v46b, …), compared against v45b.
2. **A fix is kept only if** all of these hold:
   - total cured tyres don't drop;
   - day-1 tyres built go up;
   - TBR doesn't drop, because PCR and TBR share machines;
   - all fatal schedule checks still pass (no overlaps, unique IDs, and so on).
3. **Every change is written into [CHANGE_LOG.md](CHANGE_LOG.md)** in simple words, and its row here is marked DONE.

---

## Step 1: check the data

These need no engine change, so they can start in parallel with the data owners.

| ID | Issue | Evidence | Effect | What to do | Owner | Status |
|---|---|---|---|---|---|---|
| **W1** | **The stock count misses material that is still on a machine.** The 06:30 count only has finished batches, so anything being made at shift change is missing. | An apexed bead batch BA13001 of 281 pieces was counted at 06:28, two minutes before the cut-off. The apexer was running. | Kits that miss one part would be complete. Not measured. | Add in-process material to the snapshot, or take the count after the shift-A changeover. | Data | OPEN |
| **W2** | **Thin stock of some made parts looks doubtful.** | 288 apexed beads BA13001 against 4,956 wound beads BW13002. The plant normally apexes ahead. | It is the last part for all 3 day-1 trolleys of SKU 1325215813079TUNE3 (1,030 tyres). Other items not checked yet. | Physically count apexed beads (PCR BA…, TBR TBAPBD…) and pre-assembly (TPA-…). Correct the snapshot. | Data / Plant | OPEN |
| **W3** | **Units don't match.** The stock is in one unit and the bill of materials (BOM) in another, e.g. tread stock in pieces (NOS) but demand in metres. | 37 rows flagged in `WIP_vs_DAY1_PLAN_e2e_v45b.xlsx`. Most are items with no stock. | The engine converts with an assumed 1 piece = 1 tyre. If that's wrong, stock is over- or under-counted. | Confirm the unit and the conversion for each flagged item. Fix them in the master data. | Data | OPEN |
| **W4** | **Stock rows that match no BOM item.** | `opening_wip_orphans.csv`: TR5076 (198), TR5075 (165) treads; TPA-029 (60) pre-assembly. | That stock is ignored. | Map them to the right item codes, or confirm they are obsolete. | Data | OPEN |
| **W5** | **Item types missing from the snapshot entirely.** The engine fills them with stand-in stock. | A-WIP-1: 24 h cover for absent item types (PCR 15,263, TBR 5,425 units). A-WIP-2: cap strip / edge gum for the first 8 h (PCR 3,384, TBR 760). Master compound treated as bought in. | Results depend on assumptions, not real stock. | Supply the real stock for these item types. Then switch the stand-ins off. | Data | OPEN (assumed) |
| **W6** | **B616M (cap-strip compound) is a kit part on every PCR trolley.** The routing has no CAP 66 step, so the compound goes straight to the builder. It isn't in the stock either. | Needed on all 40 day-1 PCR trolleys. No PCR trolley can start before about 12:00. | Holds the earliest PCR starts. | Add the CAP 66 routing step. | Data (routing owner) | OPEN |
| **W19** | **Most PCR slitter stock is never used.** The cutters and slitters are skipped in the schedule, but stock already cut or slit **is** counted: each cut code's stock goes against that code's own need, and the mother roll beneath it is reduced by the BOM ratio (v42 `cold_start.net_cut_stock`). That works for most departments, but not for the PCR slitter. | Used over the horizon: PCR cutting 88%, ply cutter 91%, cutter 100%, but **slitter only 15%** (1,128 of 7,311 m; 6,183 m left). TBR belt cutter 99%, chafer slitter 93%, bias cutter and body-ply cutter 100%. 10 PCR codes with stock are never used (e.g. PBP58 651 m, B2-PBLT104 489 m, MONOCHAF120 338 m). TBR has 2 (L-/R-STC001, 42 m each). | Mother roll is made that the slit stock could have covered. Cause not checked yet: maybe no demand for those codes in the horizon, a BOM code mismatch, or the stock is too old. | For each unused code, check whether the plan needs it, whether the BOM uses the same code, and when the stock expires. Then decide: data fix or engine fix. | Data / Engine | **Mostly DONE in v46a.** Cut/slit stock is now turned into the material it was cut from, and 97% (PCR) / 99% (TBR) of that is used. Still open: W20, W21. |
| **W20** | **TBR chafer stock can't be converted: the units differ.** SNCT001–003 are held in stock in metres, but the BOM states them in square metres, and the width needed is missing. The routing also gives 1 input where the BOM gives 3 (BCFT001 / 002 / 005). | 36 batches, 7,293 m. They stay on the chafer code and are netted the old way (6,782 m used). | The engine assumes a unit conversion for these. | Supply the chafer width (m² per m) or hold the stock in m². Make the BOM and routing agree on the input. | Data | OPEN |
| **W21** | **The transfer time across the TBR cutters is 0.** It equals the minimum ageing of each bypassed code, and every TBR cut code (belt, bias, body ply, chafer) has 0 minimum ageing in the ageing master and in the stock rows. PCR gets 4 h from the item-type defaults. | `BYPASS_REPORT_e2e_v46a.xlsx`, sheet "Transfer time": 13 TBR items, all +0 h. | If belts and plies really need a rest after cutting, TBR building is scheduled too early. | Confirm the real minimum rest per TBR cut item and fill the ageing master. | Data / Plant | OPEN |
| **W7** | **Batches already expired at opening.** | 37 PCR and 6 TBR batches are expired at 07:00 (`WIP_USAGE_e2e_v45b.xlsx`). | That stock can't be used. | Confirm with the plant: scrap them, or check whether the ageing limits are right. | Plant / Data | OPEN |

---

### Why some cut stock can't be turned into its mother material

**What we do (v46a):** the cutters and slitters aren't scheduled, so any stock already cut is turned back into the material it was cut from. A cut belt becomes calendered roll. The schedule then uses it from there.

**This works for 90% of the cut stock: all of PCR, and most of TBR.**

**The rest is three TBR chafer codes, SNCT001, SNCT002 and SNCT003 (7,293 m).** They can't be converted because of missing data:

1. **The units don't match.** The stock counts the chafer in **metres** (length). The BOM states it in **square metres** (area). To turn metres into square metres we need the **width** of each chafer, and no table gives it.
2. **The BOM and the routing disagree on what the chafer is made from.** For SNCT001 the BOM lists 3 pre-cut fabrics (BCFT001, BCFT002, BCFT005) but the routing lists only 1 (BCFT001). For SNCT003 the BOM lists BCFT001 and BCFT003, and the routing only BCFT003.
3. **Two of those fabrics, BCFT002 and BCFT005, have no routing step and aren't on the purchased list.** The engine has no record of whether they're made here or bought in, so it treats them as bought in and always available.

**What happens to this stock meanwhile:** it isn't wasted. It's used against the chafer code itself, as before (6,782 of 7,293 m used). It just isn't turned into the fabric roll underneath.

**What fixes it (data owners):**
- the width of each SNCT chafer (m² per metre), or stock counted in m²;
- the BOM and routing made to agree on the chafer's input fabric;
- BCFT002 / BCFT005: add a routing step if made here, or add them to the purchased list if bought in.

The rows for this in the Step 1 table are W20 and W22.

| ID | Issue | Evidence | Effect | What to do | Owner | Status |
|---|---|---|---|---|---|---|
| **W22** | **BCFT002 and BCFT005 have no routing step and aren't on the purchased list.** The engine silently treats them as bought in. | Inputs to chafer SNCT001 in the BOM (195 SKUs). `CTP_Purchased_Items.csv` holds 219 codes, 217 of them master compounds, and no cut or fabric code. | If they're really made here, their making time and material are missing from the schedule. | Owner to confirm made or bought: add a routing row, or add them to the purchased list. | Data (routing / master data) | OPEN |

---

## Step 2: your decisions

These change the rules, so they need your call before any coding.

| ID | Issue | Evidence | Effect | Options | Owner | Status |
|---|---|---|---|---|---|---|
| **W8** | **The engine sets the expiry clock wrongly.** It treats stock as expiring at 07:00 + its maximum age, when the real expiry is production time + maximum age. | 191 stock draws are used after their real expiry. | Today's result is slightly **too optimistic**. Fixing this will lower the numbers a little. | (a) Fix it to the real expiry. (b) Keep it and state it as an assumption. | Decision → Engine | OPEN, waiting for you |
| **W9** | **Day-1 plan order.** A builder waits for its next planned trolley even when a later trolley's kit is ready. | 9 PCR trolleys (1,675 tyres) and 4 TBR trolleys (290 tyres) had a full kit on day 1 but were pushed past day 1. | Up to 1,965 tyres on day 1. | (a) Keep the strict A-B-C order. (b) On day 1 only, let a builder take any of its planned trolleys whose kit is ready. | Decision → Engine | OPEN, waiting for you |

---

## Step 3: engine fixes, in this order

Each fix is one run, compared with v45b before the next one starts. The order puts the cheapest fix with the clearest cause first.

| Order | ID | Issue | Evidence | Proposed fix | Run | Status |
|---|---|---|---|---|---|---|
| 1 | **W10** | **Usable stock is turned down because it is "too fresh" for its due time, and a fresh batch is made that comes out later.** A trolley planned in the first hours has its parts due *before* 07:00 on paper, so stock still resting at 07:00 is called "too late". | HS108: 2,200 kg usable from 09:25 was turned down for trolley PCR_BLD000000. A new 11 kg mix was made instead, usable at 17:50, more than 8 h later. The rule comes from v44b (`wip.maturity_per_demand`). | Take stock whenever it is usable **earlier than a fresh batch could be ready**, not only by the due time. | v46a | OPEN |
| 2 | **W11** | **The earliest trolley's part isn't put first in the queue** on the apexing / pre-assembly machines. | BA13001 for the first trolley is a 7-minute job, ready at 17:50. It waited 11 h on machine 202 behind 6 other apexing jobs. Both apexers were idle from about 08:00 to 18:00, then everything arrived at once. | In the dispatch rule, rank lots by the planned start of the earliest trolley they serve, ahead of other tie-breaks, on 201/202, 2403–2405 and 2201. | v46b | OPEN |
| 3 | **W12** | **Day-1 needs are bundled with later needs in one big lot** on the apexing machines, so day-1 trolleys wait for the whole lot. | BA13001_L00002 = 3,718 beads for 2 day-1 and 2 day-2 trolleys. It started at 09:22 on 23 Oct, ran 4 h, and the day-1 trolleys waited for all of it. | Don't merge day-1 needs with later needs on these machines. Or let each trolley take its share as soon as it is made. | v46c | OPEN |
| 4 | **W13** | **Stock is spread part by part, so no kit is complete.** | Day 1: 0 of 40 PCR and 0 of 31 TBR trolleys have a full kit from stock. An earlier "most complete kit first" trial (v42d/e) cost TBR tyres. | Narrower version: for each building machine's **first planned trolleys only**, give stock to complete their kits first. | v46d | OPEN |
| 5 | **W14** | **The compounds behind the late parts aren't mixed first.** | The last parts come from fresh mixes: B350 at 21:24, S53 at 13:26, A1031 / A5542 at about 21:45. | In the mix planner, mix first the compounds that feed the parts arriving last (ECOPOLY350 inputs, HS108, A5542, A1031, T15, B1144). | v46e | OPEN |
| 6 | — | Apply the W8 / W9 choices from Step 2 (if you chose a change). | — | As decided. | v46f | waiting for Step 2 |

---

## Step 4: plant stock levels

This comes after the engine fixes, because those reduce how much stock the plant needs to hold.

| ID | Issue | Evidence | What to do | Owner | Status |
|---|---|---|---|---|---|
| **W15** | **Opening stock is too low to run day 1 fully to plan.** A part mixed after 07:00 can't reach a builder before about midday. | Extra needed for day 1, from `WIP_TARGET_FIRST_2_DAYS_e2e_v45b.xlsx`: PCR +53,383 m, +28,281 pieces, +4,316 kg; TBR +52,415 m, +6,991 pieces. TBR parts with a 24 h shelf life can't be held for day 2. | Recompute the target **after Step 3**, then agree stock levels with the plant. | Plant | OPEN |
| **W16** | **Green tyre stock** is too low for the first days' curing. | Extra needed: PCR +7,961, TBR +3,560 green tyres. | Same: recompute after Step 3, then agree. | Plant | OPEN |

---

## Step 5: check and close

| ID | Task | Status |
|---|---|---|
| **W17** | Re-run the WIP reports on the final run: `make_wip_vs_day1_report.py`, `make_wip_usage_report.py`, `make_wip_target_report.py`, plus the validation report. Compare day 1 with v45b. | OPEN |
| **W18** | Add a **TBR worked example** to WIP_AVAILABLE_BUT_NO_BUILDING.md. Candidate: SKU 1122129A20000JUH50 (3 day-1 trolleys, 368 tyres, starts about 37 h late, last part TPA-004). Its routing also has no machine for bead winding TBWB002 (filled from the department pool, A01). | OPEN |

---

## Already handled in earlier versions (for reference, nothing to do)

| What | Version | Where it is written up |
|---|---|---|
| Cut-code stock netted against the codes made from it (bypassed cut codes) | v42 | CHANGE_LOG.md |
| Green tyre stock used first for SKUs that can be scheduled | v42 | CHANGE_LOG.md |
| Stock must be rested by each trolley's own due time (`wip.maturity_per_demand`). This rule is now the cause of W10, so W10 refines it. | v44b | CHANGE_LOG.md, TBR_APEXED_BEAD.md |
| Opening-stock corrections for the PCR cold start | v45a / v45b | PCR_COLD_START.md, CHANGE_LOG.md |
| "Most complete kit first" trial. Dropped because it cost TBR tyres; W13 is the narrower retry. | v42d / v42e | CHANGE_LOG.md |
| Cut/slit stock turned into the material it was cut from, with transfer time = the cut code's minimum ageing; both checked in `BYPASS_REPORT_<run>.xlsx`. 128,166 tyres, +2,700 vs v45b. | v46a | CHANGE_LOG.md |
