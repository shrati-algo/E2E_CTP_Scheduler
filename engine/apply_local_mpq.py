#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_local_mpq.py -- write inputs/mpq.csv from a LOCAL MPQ workbook, overriding the DB MPQ.

2026-07-27 USER DIRECTIVE: masters come from the DB, but the MPQ (and the curing plan) are taken
from LOCAL files. `adapt_inputs.py` (DB mode) writes inputs/mpq.csv from the DB; this runs AFTER it
and replaces that file with the local workbook, so the local MPQ always wins.

Source workbook columns:  child_description | Capacity | UOM   (child_description == the Item Type).
Output (phase2 format):   Item Type | Minimum Run Qty | UOM | Fraction Allowed
  - Batches UOM -> "<n> Batches" (phase2 sizes compounds by routing batch, not a piece floor).
  - Fraction Allowed = "NO" for every row (CTP convention).

Path is config `mpq_local_xlsx` (default ctp_inputfiles/mpq_user_20260727.xlsx).
"""
from __future__ import annotations
import os, sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))


def _cfg():
    try:
        import yaml
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def apply(xlsx_path=None):
    cfg = _cfg()
    xlsx_path = xlsx_path or cfg.get("mpq_local_xlsx") or os.path.join(
        HERE, "ctp_inputfiles", "mpq_user_20260727.xlsx")
    if not os.path.isabs(xlsx_path):
        xlsx_path = os.path.join(HERE, xlsx_path)
    if not os.path.exists(xlsx_path):
        print(f"  [apply_local_mpq] SKIP -- local MPQ workbook not found: {xlsx_path}")
        return False
    src = pd.read_excel(xlsx_path, engine="openpyxl")
    src.columns = [str(c).strip() for c in src.columns]
    it = next((c for c in src.columns if c.lower() in ("child_description", "item type", "item_type")), src.columns[0])
    cap = next((c for c in src.columns if c.lower() in ("capacity", "minimum run qty", "min run qty")), src.columns[1])
    uom = next((c for c in src.columns if c.lower() == "uom"), src.columns[2])
    rows = []
    for _, r in src.iterrows():
        t = str(r[it]).strip()
        if not t or t.lower() == "nan":
            continue
        u = str(r[uom]).strip()
        v = r[cap]
        mrq = (f"{int(float(v))} Batches" if str(u).lower().startswith("batch")
               else (str(int(float(v))) if float(v) == int(float(v)) else str(v)))
        rows.append({"Item Type": t, "Minimum Run Qty": mrq, "UOM": u, "Fraction Allowed": "NO"})
    out = pd.DataFrame(rows)
    dest = os.path.join(HERE, "inputs", "mpq.csv")
    out.to_csv(dest, index=False)
    print(f"  [apply_local_mpq] wrote inputs/mpq.csv from {os.path.basename(xlsx_path)} "
          f"({len(out)} item types) -- LOCAL MPQ overrides the DB MPQ.")
    return True


if __name__ == "__main__":
    sys.exit(0 if apply(sys.argv[1] if len(sys.argv) > 1 else None) else 1)
