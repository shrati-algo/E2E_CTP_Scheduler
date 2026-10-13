# CTP Scheduler — Business Rules (authoritative, always apply)

> Plant-confirmed rules that OVERRIDE default/derived values. Newest first.

---

## BR-1 — Effective line speed = PARALLEL rolls/bobins × single-strand speed  (2026-08-03, user-confirmed)

These forming/slitting machines run **several rolls (or bobins) in parallel at once**, so the
**effective line speed (throughput) is the single-strand speed MULTIPLIED by the number of parallel
strands** — NOT the single-strand speed.

```
effective_line_speed = n_parallel_strands × base_speed_per_strand
proc_time(lot)       = lot_length / effective_line_speed
```

`base_speed_per_strand` = the per-roll / per-bobin line speed (m/min = MPM) from routing.
`n_parallel_strands`    = the roll/bobin count below.

### Roll counts per material

**PLANT ROLL TABLE (2026-08-06, user-provided — the authoritative roll counts):**

| Material_Code | Parallel rolls | Width | Roll length | Worked example (base × rolls) |
|---|---|---|---|---|
| EG01  | 15 rolls | 53 mm | 300 m | 30 × 15 = **450 m/min** (base 30 = example) |
| EG02  | 11 rolls | 70 mm | 300 m | base × 11 |
| EG03  | 7 rolls  | 100 mm | 300 m | base × 7 (**ADDED 08-06** — was missing → single-strand) |
| TEG-01 | 11 rolls (belt) / 4 rolls (ply) **TBR** | 70 / 140 mm | 200 m | base × 11 (belt) / × 4 (ply). Feeds Steel Belt Edge Strip → **belt = 11** |
| TEG-02 | 9 rolls  | 80 mm | 150 m | base × 9 (**wired 08-06** — was skipped as "TBR single-strand") |
| CAP 66 | 11 rolls | 144 mm | 300 m | base × 11 (cap strip; mother-roll code `CAP 66-MOTHERROLL`) |
| BPG01 | 7 rolls  | 100 mm | 200 m | base × 7 |

**2026-08-06 CORRECTION:** TEG-01, TEG-02 and EG03 were NOT getting the multiplier (skipped/missing),
so they ran at raw ~13–14 m/min and drove machine **1102 (edge gum calender) to 54 days**. Now wired
in `linespeed_rolls`. **NOT covered by the plant table (stay single-strand): `GUM STRIP`, `BP01`** —
GUM STRIP alone is ~24.7 d on 1102; ask the plant for its roll count if it is also multi-strand.

### Cap strip slitters (base MPM × bobins)

| Machine | Base speed | Bobins | Effective |
|---|---|---|---|
| Cap strip slitter 1 | 33 MPM | 12 bobins | 33 × 12 = **396 MPM** |
| Cap strip slitter 2 | 60 MPM | 16 bobins | 60 × 16 = **960 MPM** |

### Slit forms inherit the parent's roll count (2026-08-06)

A **slit output inherits the roll count of the calender product it is slit from** — the slitter runs
the same N strands in parallel (exactly like EG01 → EGSL01 = both ×15). Traced on the 601 SLITTER
("FABRIC and GUM STRIP SLITTER", plant image: GUM STRIP's input = TEG-02):

| Slit product (601 SLITTER) | ← Parent (BOM child) | Rolls | Note |
|---|---|---|---|
| **GUM STRIP** | TEG-02 | **×9** | was raw 30 → 270 m/min (10.7 d → ~1 d) |
| STC-WRAP | TEG-01 (belt) | ×11 | |
| **TB WRAP B1144** | EG02 | **×11** | was raw 30 → 330 m/min (24.9 d → ~2 d; the 44-day driver on 601) |
| EGSL01 / EGSL02 / BPGSL01 | EG01 / EG02 / BPG01 | ×15 / ×11 / ×7 | (already wired) |

**Rule:** any SLITTER product = its calender parent's roll count. If a new slit code appears
(other `TB WRAP …` / `GUM …` variants), add it to `linespeed_rolls` with the parent's roll count.

**Why it matters:** these are exactly the short-shelf feeders (edge gum / cap strip / belt-ply)
that gate belt & ply supply. Modeling them at single-strand speed **understates cutter throughput
by 7–16×**, which fabricates belt/ply starvation. Always apply the parallel multiplier so the
feeder proc_time is the real (faster) one.

**How to apply in code:** wherever routing gives a per-strand line speed for these codes/machines,
multiply by the roll/bobin count above before computing proc_time. TEG01 splits by consumer
(11 for belt, 4 for ply, TBR). Implemented as `config.yaml -> linespeed_rolls` (applied in
`adapt_inputs.routing()`).

**CRITICAL (2026-08-03): apply to the SLITTER too, not just the calender.** Edge gum flows
calender (EG01 on m/c 1102) -> slitter (EGSL01 on m/c 601 "CAP PLY CUTTER") -> belt. Both run
the N-strip parallelism. If only the calender gets the multiplier, the SLITTER 601 stays at the
base 30 M/MIN and becomes the belt-supply bottleneck (98.5% util) -> the whole drum slides ~8 days
(makespan 33d -> 38d). So multiply the slit outputs with the SAME roll count as their parent:
EGSL01=15, EGSL02=11, BPGSL01=7.  EG03/EGSL03 have NO plant roll count yet -> still at 30 (ask plant).

---

## BR-2 — Chaffer slitter: effective speed = rate × N strips (Width = w*N)  (2026-08-03, user-confirmed)

The **chaffer slitter** (m/c 2401/2402, routing 22 M/MIN) slits the calendered/pre-cut roll into **N
parallel strips at once**. The plant's Width column is `width * N` (e.g. `80*4` = 80 mm × 4 strips).
Effective M/MIN = **22 × N**. Same mechanism as BR-1 (`config linespeed_rolls`).

| MaterialCode | Dept | Width | N | eff (22×N) |
|---|---|---|--:|--:|
| MONOCHAF80 | PCR IL | 80*4 | 4 | 88 |
| MONOCHAF120 | PCR IL | 120*4 | 4 | 88 |
| FILLIPER2 / BRTTL SLT | PCR | 75*2+90*3 | 5 | 110 |
| SNCT001 | PCR / TDA | 75*6 | 6 | 132 |
| SNCT002 | TBR | 50*6 | 6 | 132 |
| SNCT003 | TBR | 120*4 | 4 | 88 |
| SNCT004 | TBR | 100*4 | 4 | 88 |
| EUSNCT001 | TDA | 75*6 | 6 | 132 |
| EUSNCT003 | TBR | 120*4 | 4 | 88 |
| SNCT001/SNCT003 (doubling) | TDA/TBR | 75*3+122*2 | 5 | 110 |

N = sum of the `*` multipliers when two widths are listed (75*2+90*3 → 2+3 = 5).

---

## BR-3 — Bias cutter: 1 cut = 1 tyre = B1 + B2 (pieces_per_cut = 2)  (2026-08-03, user-confirmed)

The **bias cutter** (m/c 2001) cuts the steel-belt calender roll and makes **both belts (B1 + B2) of a
tyre in one cut** — 1 cut = 1 tyre. So `pieces_per_cut["BIAS CUTTER"] = 2` (exactly like BELT CUTTER).
Without this it was modelled at 1 cut = 1 belt = **2× the cuts** → 2001 falsely at 93.8% util → it
became the belt/ply bottleneck that made the schedule tail to ~33 d. Implemented in
`config.yaml -> pieces_per_cut: BIAS CUTTER: 2`.

---

## BR-4 — Authoritative component aging (plant spec CTE0.06-FR.02, PCR)  (2026-08-03, user-supplied full table)

The plant's own age-limit table. Implemented in `apply_aging_spec.py` (item_type → MaxAging h). The per-item
`aging_master.csv` from the DB is OVERRIDDEN by this on every run. **Max age in HOURS** (min in parens):

| our item_type | plant row (§) | Max | Min |
|---|---|--:|--:|
| **Steel Belt** | §14 PCR-STEEL calendered steel belt | **96h (4 d)** | 4h |
| CALANDARED ROLL | §14 PCR calendered textile fabric (ply fabric) | 72h (3 d) | 4h |
| **Ply** | §14 PCR Calendered Textile Fabric (Ply) | **72h (3 d)** | 4h |
| Cut Plies / Breaker (cut form) | §15 (SLITTED MATERIAL / PRE CUT ROLL) | 24h (1 d) | 0 |
| SLITTED MATERIAL / Slitted Material | §15 cut/slitted fabric | 24h | 0 |
| PRE CUT ROLL MATERIAL | §15 cut breaker | 24h | 0 |
| Cap Strip | §15 slitted fabric | 24h | 0 |
| Capstrip Mother Roll | §14 calendered material | 72h | 0 |
| **Edge Gum** | §10 PCR calendered edge gum | **72h (3 d)** | 2h |
| Edge Strip / Steel Belt Edge Strip | §10 PCR slitted edge gum | 48h (2 d) | 0 |
| Inner Liner | §9 PCR IL + squeegee | 72h | 6h |
| SideWall | §11 sidewall / belt edge filler | 72h | 2h |
| Tread | §12 CARBON tread | 72h | 2h |
| *(silica tread)* | §12 SILICA tread | **24h** ⚠ needs silica code list | 2h |
| Bead Apex | §13 apex (manual) | 48h (2 d) | 1h |
| Bead Bundle | §16 PCR bead insulated: covered 72h / **uncovered 48h** | 48h | 0 |
| Bead Wire | §3 bead wire (DOM) | 6 months | NA |
| Green Tyres | §18 PCR 2nd-stage painted GT | 48h (2 d) | 15min |
| MASTER COMPOUND | §5 PCR: non-silica 360h (15 d); **silica 72h**; cobalt/butyl 24h | per-code | 4h |
| FINAL COMPOUND | §7 PCR: non-silica 120h (5 d); **silica/butyl 72h**; belt-skim 96h | cap 120h | 4h |

**Key corrections vs the old DB values:** Steel Belt 24h→**96h** (belt now pre-buildable in the runway);
Edge Gum →**72h** (was 24h, was causing false EG01 violations). **Two FLAGS** needing a plant code-list to
finish: (1) **silica tread** must be 24h not 72h; (2) **silica master/final compound** must be 72h not 360/120h.
**Ply = 72h** (§14 calendered textile fabric, USER-corrected 2026-08-03) → Ply is now pre-buildable in the runway
like Steel Belt; the only genuine 24h short-shelf left are SLITTED MATERIAL / PRE CUT ROLL (§15 cut breaker).
