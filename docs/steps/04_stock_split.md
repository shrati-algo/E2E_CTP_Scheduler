# Step 4: the opening stock split PCR / TBR by this plan's demand

**Code:** `engine/run_all.py --only phase0 phase1`, then `engine/stage_wip_v6.py --split-from <run> --also-bom reference_bom --cut-to-cutting-line`.

The stock table holds one floor for both lines. Many items (compounds, beads, fabrics) are used by both lines.

1. Phases 0–1 turn this plan into item demand per line.
2. Each stock row is split between PCR and TBR in proportion to that demand:
   - **`--also-bom reference_bom`**: items known only to the pre-v2 BOM still count as known;
   - **`--cut-to-cutting-line`**: an item wanted by neither line goes to the line that cuts it.

**Writes:** `data/ppds_<day>/opening_wip_{pcr,tbr}.csv` and `_wip_manifest.json` (what moved where).

Same as v46k.
