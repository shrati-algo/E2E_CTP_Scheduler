# Known limits

Keep these in mind when reading results. More detail: [reference/known_issues.md](reference/known_issues.md).

## Method

- **No solver.** v133 is rule-based scheduling. The result is a feasible-by-construction schedule under its rules, not an optimum. Several rule checks are PARTIAL or FAIL; see `RULE_ADHERENCE_REPORT`.
- **Run-to-run swing:** about ±4–5 k cured tyres. A small input change re-orders the mixing plan and cascades downstream.

## Database BOM

v133 uses `CTP_Master_BOM` **as it is**. A validation against the SMART BOM found errors in the database BOM (main project `bom_validation/`):

- **Repeated SMART blocks added up.** Some SKUs have SMART rows that were added up several times. GT 1513 XPC1 MSIL is doubled and CAR 1402 XPC TATA is × 8, so their demand is inflated.
- **Tread.** The first compound carries the whole tread weight (about +25 %).
- **MONOCHAFER.** The inner-liner chafer chain gives about ¼ of the MONOCHAFER the parts need.
- **Bias-cut chafer pieces.** These are counted as one piece, so BRTTL / B333 are low.
- **Ply gum strip.** BPG01 is about 7.5× too high.
- **Edge strips and wraps.** These are counted twice (plant to confirm).

A corrected BOM is available in the main project (`bom_validation/CTP_Master_BOM_corrected.csv`). Using it is a different version, not v133.

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
