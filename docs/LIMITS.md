# Known limits

Keep these in mind when reading results. More detail: [reference/known_issues.md](reference/known_issues.md).

## Method

- **Cutting time is a lag, not a machine.** Cutter / slitter capacity is not modelled; the cutting time is the configured transfer minutes. The values are assumptions until the plant confirms them.
- **Start-up.** The first day's kit waits on 4RC rolls directly. With the database BOM as it is (v1) this gave a start-up dip.
- **No solver.** The engine is rule-based scheduling. The result is a feasible-by-construction schedule under its rules, not an optimum. Several rule checks are PARTIAL or FAIL; see `RULE_ADHERENCE_REPORT`.
- **Run-to-run swing:** about ±4–5 k cured tyres. A small input change re-orders the mixing plan and cascades downstream.

## BOM

- **Default BOM.** By default the **corrected** database BOM is used: the high-confidence corrections against SMART.
- **Still differing:** the edge strip / wrap count (B1144, B127, EG01 / EG02; plant to confirm) and the items marked for review. `BACK_CALCULATION_<run>.xlsx` lists every remaining difference with a suggestion.
- **Database BOM as it is:** `--bom db`. It has the known errors listed in the main project's `bom_validation/README.md`.

## Data issues (from the project)

- **Plant confirmation pending:**
  - compound WIP quality;
  - mixing cycle times and batch weights;
  - 1802 line speed;
  - 4RC speeds;
  - some routing gaps (CWBP65; one SKU with no building routing).
- **Limiting sections in v133:**
  - PCR: bead apexing 201 / 202 and inner-liner calender 1101 (via bias cutter 2001);
  - TBR: duplex tread 1801.
- **Snapshot dates.** The offline snapshot copy holds masters from 13 / 14-Oct and WIP up to 15-Oct 15:28. Newer plans need `--db live`.

## Integrity checks to watch in `run_log.txt`

- `STARVED groups` should be 0.
- `ledger … short (MUST be 0)` and `SKUs starve -- MUST be 0` were not always 0 in earlier versions; check them.
- Green tyres scrapped (past 72 h).
