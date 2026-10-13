# Changes from the base v6_wave_p2 scheduler

This scheduler is a **port of `v6_wave_p2`** (the JK Tyre BTP wave scheduler) onto **CTP Chennai
PCR** data. The nine phases are, wherever possible, **byte-identical** to v6 — they run as
sub-processes exactly as v6's own orchestrator runs them. Every place we deviate is listed here
with **what changed, why, how much it helped (measured), and why it is correct.**

Guiding rule (from the plant owner, verbatim): **"Correctness beats makespan. 34–35 days is fine.
Never tune to protect a makespan number."** Nothing below was done to make a number look good; each
change is either a *data correction*, a *v6-faithfulness decision*, or a *measured, invariant-safe
lever* that the plant owner signed off on.

Baseline reference: the current shipped run is **34.02 days**, 385,772 tyres cured, all 7,776 lots
placed, every hard constraint (qty ≥ demand, BOM, routing, MPQ, precedence) clean.

---

## A. Data corrections — wrong or missing plant data (NOT tuning)

These fix places where v6's logic silently mis-read, or never read, a CTP plant master. Each is a
**correction to data**, verified against the plant's own files/DB.

| # | Change | Why v6 was wrong on CTP | How it helped |
|---|--------|--------------------------|----------------|
| 1 | **Transfer time from routing is a mandatory producer→consumer lag** | v6 never reads `transfer_time_min`. On CTP the routing carries real transfer times (e.g. the **90-min green-tyre → curing** transfer) that gate when a consumer can start. | Correct hand-off timing; the DB had silently dropped the 90-min GT transfer — restoring it moved aging 51.7 → 47.1 %. |
| 2 | **Routing owns efficiency + transfer** | CTP routing has `efficiency` and `transfer_time_min` columns v6 ignores. | Durations and lags now match the plant's own routing. |
| 3 | **Plant changeover matrix** (`jkt_changeover_matrix_combined`) | v6 derives setup minutes from *machine names* (WBC/LTBC/…); none of CTP's numeric machine IDs match, so every pair fell to a flat 15 min. | 167,921 real changeover rules across 11 lines now apply; mixer stays 2 min (plant-confirmed). |
| 4 | **Building changeover from the DB** (`Master_Building_ChangeoverTime_pcr`) | We had assumed a flat 40/60 min. The DB has the real per-TBM values: **3401–3405 = 28/60, 3406–3411 = 22/42**. | Real plant setup times; makespan 33.16 → 34.02 d — a curing-tail reshuffle, not a cost (see §D of the makespan note). |
| 5 | **BOM dedup uses SUM, not first** (phase1b DEVIATION #4) | v6 kept the *first* duplicate BOM row, so a tyre needing **2 belts** got **1 belt's** rubber. | Belt/compound quantities are now correct — a load-bearing fix (reverting under-builds belt & compound). |
| 6 | **MPQ source = `jkt_mpq_corrected.xlsx`** | The raw plant MPQ left Steel-Belt/Ply blank and used item-type names that didn't match the master (a silently-dead MPQ). The corrected file has exact item-type names, the PCR sheet, and collapsed duplicate rows. | Bead-apex over-production 68 % → 11 %; makespan 42.67 → 40.12 d. |
| 7 | **Aging fallback by item type** | CTP's per-item aging master has holes; the plant's own aging table (PCR column) fills them. | Item types without a row get the plant's real shelf life instead of a blanket default. |

**Why these are correct:** every one is traceable to a plant file or the live DB, not invented. The
per-item master always wins; the fallbacks only fill declared holes. Provenance for all of them lives
in `db_pipeline/table_map.py` (which DB table feeds which input) and `knowledge/docs/`.

---

## B. v6-faithfulness decisions — where we deliberately did NOT change v6

The plant owner directed that these stay **v6-verbatim**, even though earlier experiments existed.
They are listed so nobody "re-fixes" them.

| Phase | Decision | Why |
|-------|----------|-----|
| **phase1c (MRP netting)** | Reverted to **v6-identical** (2026-07-22). Undid CTP #9/#24/#25/#26 + an aging-netting change. | The v6 netting over-nets ~1.5 M and breaks slit geometry, but that is v6's accepted behaviour; the "fix" was not a real gain (34.78 d was not genuine). Validator flags this as V10 WARN — an expected characteristic. |
| **phase3 F1 (early-window edge filter)** | Kept **v6** — the F1 early-window defect (119 orphans) is NOT patched. | A real v6-inherited defect, but the plant owner chose faithfulness; the orphans carry real demand and are *placed late*, never dropped (see C-#8). |
| **phase4 (CPM)** | Cleaned to **v6-verbatim + only** the transfer-time-from-routing lag. Removed a probe and a convergence loop (both were gated off; makespan unchanged 34.965 d). | Keeps the CPM a faithful v6 critical-path pass; only the one sanctioned CTP lag is added. |
| **phase1_5 (wave builder)** | Already v6-identical — no change. | — |

---

## C. Executability & honesty — making hidden behaviour visible (schedule unchanged)

These do **not** move a single lot. They make v6's silent behaviour *auditable*, so a naive user sees
the truth instead of a green checkmark.

| # | Change | What it exposes | Schedule impact |
|---|--------|------------------|-----------------|
| 8 | **Orphans are PLACED, not deleted** (`RESPECT_LFT=False`, CTP deviation #8) | v6 would *silently delete* lots whose latest-finish can't be met. They carry real demand, so we place them **late** and print the count. | 0 lots lost (vs v6 silently dropping them). |
| 9 | **`honest_lft_anchor: report`** | The C4 negative-slack count was inflated by phase3 fan-out over-linking each feeder to its whole 3-day wave. Report mode re-anchors each non-terminal lot to its *own* last-need for the report. | C4 **4,180 → 888**, byte-identical schedule (placement reads the original tight anchor). |
| 10 | **`c6_jit_audit: on`** | The C6 "expired" count assumed producers finish ASAP; the JIT audit times them to finish just before their earliest consumer. | C6 **10,434 → 2,571**, reporting honesty only — phase5 reads none of it, schedule byte-identical. |
| 11 | **CTP #30 material-balance check** | A balance check that *can actually fail* (most safety checks were tautologies). The plant owner found the MPQ surplus by eye; this makes it visible. | Report-only; closed 53 items / 124,485 units of over-production for zero makespan when acted on. |

**Why correct:** none of C changes a build or cure decision — verified byte-identical. They only make
the numbers *truthful*, which is the whole point of a "run-and-trust" layer.

---

## D. Measured levers — config-gated, invariant-safe, plant-owner approved

Each of these is a **flag in `config.yaml`** with a measured effect. They were kept ON only because
they help *without* breaking an invariant, and the plant owner accepted the trade. Turning any of
them off is a supported, documented operation.

| # | Flag | Measured effect | Correctness |
|---|------|-----------------|-------------|
| 12 | `wip_netting_chronological: true` | 40.12 → 38.79 d **and** aging better | Stops a mixer running at hour 0 to make compound the plant already has in WIP. Pure win. |
| 13 | `building_takt_spread: true` | 38.79 → 37.02 d; spreads 27 → 18 single-pinned codes | **Load-bearing** — OFF costs +3.2 d. +133 building changeovers is the only cost. |
| 14 | `spread_eft` (building EFT placement) | 40.12 → **35.85 d** | The main makespan lever; costs +8.9 pp GT over-age (a real trade the owner accepted). |
| 15 | `building_tail_spill: true` (CTP #32) | 34.81 → 33.35 d **and** aging better | Ropes tail lots to their real last-need (not the inert LFT); +8 changeovers only. |
| 16 | F1 edge-qty + F3 component-WIP gate | first build 37.7 h → 11.1 h; aging 46.5 → 43.3 % | The material-readiness gate had asked 28.7× too much (phase3 fan-link has no quantity). |
| 17 | streaming press kit-gate (S6) | 34.81 d, aging 51.7 → 47.1 % | The only lever that improves makespan **and** aging together. |
| 18 | `prune_wip_fed_component_demand: true` (CTP #41) | removed ~168 k units of over-production + 20 orphans; 33.30 → 33.16 d | Green tyres that arrive from opening WIP were never built, yet their full component sub-tree was exploded. Prune it. Cured total unchanged. **The plant owner found this by eye.** |

**Why correct:** every lever ships with a measured A/B (in `knowledge/memory/`), preserves the hard
invariants (qty ≥ demand, mass balance, MPQ, routing), and has a documented cost. None was tuned to a
target; each is the *smallest* change that removes a specific defect.

---

## E. Infrastructure

| Change | What |
|--------|------|
| **DB pipeline** (`data_source: db`) | Reads the live `jkplanning_CTP` database *through* `adapt_inputs` (never straight into `inputs/` — bypassing the adapter gave 106 d instead of 34.78 d). The demand drum (plan) stays the JUNE shift sheet because the DB's `CuringSchedule_pcr` is APRIL data. |
| **`excluded_items`** | ~11 items with no routing proc_time (incl. the WIP-fed green tyres) are excluded from building — declared loudly, not silently skipped. |
| **`verify_run.py`** | A "run-and-trust" health check that classifies each residual FAIL into *data gap* / *known report* / *real blocker*, so a naive user gets one verdict. |
| **Testing suite** (`testing/`) | Nine phasewise validators that independently re-derive each phase and check the shipped output. |

---

## What is NOT a defect (do not re-raise)

- **Negative slack on 887 green-tyre lots** — a conservative terminal-anchor + cold-start artifact; the
  forward pass still ends day 29.34 (inside the 30-day plan). Feasible.
- **The makespan tail (press 4836, SKU 1325221417096HRHE0)** — proven **mould-copy-bound** (only 2
  physical mould copies exist). Not a scheduler bug; needs a plant mould, not code.
- **Cap-strip 24 h "over-age" in a FEFO audit** — a single 16 M-metre shared pool modelled as discrete
  lots; slit JIT in reality. The physically meaningful green-tyre freshness is **11.9 %** over 48 h,
  close to the plant's own ~7 %.
- **Zero-qty / tiny building-lot rows on the floor** — shift-boundary render fragments of normal
  400-unit lots, not real lots.

See `knowledge/memory/` for the full measured history behind every line above.
