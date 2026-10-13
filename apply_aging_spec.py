#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_aging_spec.py -- rebuild inputs/aging_master.csv from the PLANT ageing spec
   (CTP DOC CTE0.06-FR.02, Rev 12, 20.01.2024 -- PCR radial components).
   Run AFTER adapt_inputs (which regenerates aging from the DB/workbook), BEFORE run_pipeline.

   Fixes the DB values that disagree with the plant spec. MAX age in HOURS per PCR item type:
"""
import pandas as pd, os
HERE = os.path.dirname(os.path.abspath(__file__)); INP = os.path.join(HERE, "inputs")

# item_type -> (MAX_h, MIN_h) straight from the plant PCR spec.  D = days*24.
SPEC = {
    # --- the real DB errors (too SHORT -> false violations) ---
    "Steel Belt Edge Strip": (48,  0),   # 10d Slitted edge gum = 2 Days   (DB had 24) *** the big one
    "Bead Apex":             (48,  1),   # 13  Apex = 2 Days                (DB had 24)
    "Capstrip Mother Roll":  (72,  0),   # 14  Calendered material = 3 Days (DB had 24)
    "Bead Wire":             (4320,0),   # 3   Bead wire = 6 Months         (DB had 48)
    # --- already correct per spec (set explicitly so the file is fully spec-derived) ---
    "Cap Strip":             (24,  0),   # 15  Slitted fabric = 1 Day
    "Green Tyres":           (48,  0),   # 18  PCR 2nd-stage (painted) GT = 2 Days
    "Inner Liner":           (72,  6),   # 9   PCR inner liner = 3 Days
    "SideWall":              (72,  2),   # 11  Extruded sidewall = 3 Days
    "CALANDARED ROLL":       (72,  4),   # 14  Calendered textile fabric = 3 Days
    "SLITTED MATERIAL":      (24,  0),   # 15  Slitted fabric = 1 Day
    "PRE CUT ROLL MATERIAL": (24,  0),   # 15  Cut fabric = 1 Day
    "Ply":                   (72,  4),   # 14 PCR Calendered Textile Fabric (Ply) = 3 Days (USER 2026-08-03: our "Ply" is the CALENDERED ply fabric, not the cut breaker; was 24h). Cut plies/breaker = SLITTED MATERIAL/PRE CUT ROLL @24h.
    "Steel Belt":            (96,  4),   # 14 PCR-STEEL Calendered Steel Belt = 4 Days (plant spec CTE0.06-FR.02 §14; USER 2026-08-03 correction, was 24h). Cut breaker = PRE CUT ROLL MATERIAL @24h.
    "Bead Bundle":           (48,  0),   # 16  Bead insulated (uncovered) = 2 Days (covered = 3 Days/72h)
    # --- 2026-08-03 USER full-table additions (CTE0.06-FR.02) ---
    "Edge Gum":              (72,  2),   # 10  PCR calendered edge gum = 3 Days (was 24 -> cleared EG01 violations)
    "Edge Strip":            (48,  0),   # 10  PCR slitted edge gum = 2 Days
    "Tread":                 (72,  2),   # 12  Carbon tread = 3 Days  (SILICA tread = 1 Day/24h -- needs silica code list, FLAG)
    "Slitted Material":      (24,  0),   # 15  Slitted fabric = 1 Day (casing variant of SLITTED MATERIAL)
    # Compounds keep per-code values; Final capped below. SILICA master/final = 72h (needs silica flag).
}
FINAL_COMPOUND_CAP_H = 120.0   # 7 PCR final compound: non-silica 5 Days max -> cap the stray 360h rows

bom = pd.read_csv(os.path.join(INP, "bom.csv"))
c2t = {str(r["child"]).strip(): str(r["child_description"]).strip() for _, r in bom.iterrows()}

ag = pd.read_csv(os.path.join(INP, "aging_master.csv"))
code = "ItemCode" if "ItemCode" in ag.columns else "itemcode"
ag.to_csv(os.path.join(INP, "aging_master.csv.bak_prespec"), index=False)  # backup
ag["_it"] = ag[code].astype(str).str.strip().map(c2t)

def _cur_max_h(r):
    try: v = float(r["MaxAging"])
    except: return None
    u = str(r["MaxAgingUnit"]).upper()
    return v*24 if "DAY" in u else (v/60 if "MIN" in u else v)

n_fix = 0
for i, r in ag.iterrows():
    it = r["_it"]
    if it in SPEC:
        mx, mn = SPEC[it]
        ag.at[i, "MaxAging"] = mx; ag.at[i, "MaxAgingUnit"] = "HRS"
        ag.at[i, "MinAging"] = mn; ag.at[i, "MinAgingUnit"] = "HRS"
        n_fix += 1
    elif str(it).strip().upper() == "FINAL COMPOUND":
        if (_cur_max_h(r) or 0) > FINAL_COMPOUND_CAP_H:
            ag.at[i, "MaxAging"] = FINAL_COMPOUND_CAP_H; ag.at[i, "MaxAgingUnit"] = "HRS"; n_fix += 1

ag.drop(columns=["_it"]).to_csv(os.path.join(INP, "aging_master.csv"), index=False)
print(f"  [aging spec] rebuilt inputs/aging_master.csv from plant spec CTE0.06-FR.02 -- {n_fix} rows set.")
print(f"  [aging spec] KEY: Steel Belt Edge Strip 24h -> 48h, Bead Apex 24h->48h, "
      f"Capstrip Mother Roll 24h->72h, Bead Wire->6mo, Final Compound capped 360h->120h.")
