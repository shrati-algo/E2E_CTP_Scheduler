# PCR cold start: what holds up the first days, what we changed, what is left

This note tracks the PCR cold-start problem on its own: why PCR builds so little in the first days after 22 Oct 07:00, and what has been done about it. Same data (`data-ppds-v38`) and October plan in every run; "cured" is out of the eligible demand; builders and presses keep the plan's machine and order (check G8).

---

## 1. Starting point (v44c-all)

| measure | value |
|---|---|
| PCR built % of plan, days 1 / 2 / 3 | **4.9 / 50.9 / 73.9** |
| PCR trolleys planned day 1 | 40, median delay 23 h |
| last missing part of day-1 trolleys | **calender 901: 15–16**, **bead apexing 201/202: 16**, extruder 6, final mixing 2 |
| most common day-1 blocking item | **ECOPOLY350** (901), 13 trolleys |

Opening stock is used. 18,819 wound-bead units, 4,920 apexed-bead units, and B350 / ECOPOLY350 / HS108 stock are all drawn. With the v44b fix, stock that rests just after 07:00 is also used.

---

## 2. Cause 1: the bead gate held apexing and broke the machine timeline (fixed in v45a)

**What happened.** The bead gate compared **total apexed** with **total newly wound** quantity for the whole line, across all items. It **ignored wound-bead stock**, so apexing lots fed from stock waited for unrelated winding. Example: BA14006 (all inputs from stock) could start at 07:00 but was moved to 17:56; BA18004 to 18:30. The gate also moved lots without moving the lot behind them, leaving **81 overlapping lots on 201 / 202**.

**Fix (v45a).** The aggregate gate is switched off (`bead_gate.enabled: false`). Each apexing lot still waits for **its own** winding lots through the material links. New fatal check **G1u**: no upstream machine runs two lots at once.

**Effect.** 0 overlaps; 201 and 202 start at 07:00; 44 PCR trolleys start earlier. Cured unchanged (120,314), because those trolleys still wait for 901.

---

## 3. Cause 2: calender 901 served TBR first (v45b, decision pending)

**What happened.** 901 is shared by PCR and TBR and picked by due time. TBR lots have **earlier due times** only because their chains after 901 are longer, so until 23 Oct 12:26 901 ran almost only TBR lots. PCR's ECOPOLY350 (13 day-1 trolleys) waited, although its compound was ready at 23 Oct 00:00.

**Fix (v45b).** 901 queues by the **start time of the trolley each lot serves** (8 h windows, closest stage first), the rule that worked on the TBR bead machines.

**Effect.**

| | v45a | v45b |
|---|---|---|
| PCR built %, days 1 / 2 / 3 | 4.9 / 50.9 / 73.9 | **8.1 / 49.2 / 103.9** |
| cured PCR | 96,808 | **103,234 (+6,426)** |
| cured TBR | 23,506 | 22,232 (**−1,274**) |
| cured total | 120,314 | **125,466 (77.6%)** |

PCR gains a lot. TBR loses some 901 time, so v45b strictly breaks the "no line loses tyres" rule. **Decision pending.**

---

## 4. Cause 3: compounds for the bottleneck are mixed late (open)

From 07:00, the mixers do not know which compounds feed the machines that hold up the first trolleys:

| compound | feeds | first mixed (v44c-all) | stock |
|---|---|---|---|
| **B350** | ECOPOLY350 on 901 (13 PCR day-1 trolleys) | 22 Oct **21:24** (mixer 102 ran TBR B99, PCR IL3/IL5, TBR 30G first) | 2,100 kg, used |
| **S53** | PCR bead winding (1201) | 22 Oct **13:26** (mixer 103) | none |
| A1031 / A5542 | TBR apexing | 22 Oct ~21:45 | 4,830 / 1,440 kg, used |

**Proposed fix.** "Bottleneck compounds first": from 07:00, for the first hours, compounds feeding 901, the bead machines and other day-1 blockers go first on the mixers, among lots ready from 07:00. Mixing is planned by the **mix planner** (`engine/mix_planner.py`), so the priority has to be set there, not only in the dispatcher.

---

## 5. Other day-1 blockers (open)

| blocker | trolleys (day 1) | note |
|---|---|---|
| extruders 1002 / 1102 | 5–7 | not analysed yet |
| PCR bead winding 1201 | — | starts ~17:50, waiting for compound S53 (cause 3) |

---

## 6. Summary of runs

| run | change | PCR d1 / d2 / d3 built % | cured PCR | cured TBR | total | status |
|---|---|---|---|---|---|---|
| v44c-all | (start) | 4.9 / 50.9 / 73.9 | 96,808 | 23,506 | 120,314 | previous baseline |
| **v45a** | bead gate off + G1u | 4.9 / 50.9 / 73.9 | 96,808 | 23,506 | 120,314 | **baseline** (correctness) |
| v45b | + 901 queues by trolley need | **8.1 / 49.2 / 103.9** | **103,234** | 22,232 | **125,466** | decision pending (TBR −1,274) |

Settings: `bead_gate.enabled` (phase5), `dispatch.trolley_need_first_machines` (phase5). Runs: `config/rules_v45a.yaml`, `config/rules_v45b.yaml`.
