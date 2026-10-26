#!/usr/bin/env python3
"""stage_purchased.py — the plant's own purchased / not-made list.

    python stage_purchased.py

WHERE IT COMES FROM
  `CTP Data Gaps Report (2).xlsx`. The plant annotated the gap rows, and its
  Remarks column is the only place in this project that STATES what is bought in
  rather than made:

      "This is a raw material as checked in smart BOM, so we cant have a
       machine for it"
      "raw material"
      "outside mixing so we cant have the mixer machine"

  and separately, codes that should not be planned at all:

      "Obsolete"   "size out"

WHY IT MATTERS MORE THAN ITS SIZE SUGGESTS
  Until now the engine INFERRED the purchased set: a BOM child was purchased if
  and only if the routing did not route it. That inference is silent and it is
  wrong in one direction - a code with a routing row is assumed to be MADE, even
  when the plant says it is bought. All six raw-material codes have a PCR
  routing row, and three of them (MTCM313, TBA827, TCA313) were being SCHEDULED:
  the engine was booking mixer time to produce material the plant purchases.

  With the list declared, those codes are contracted out of the BOM walk like
  any other purchased item - no lot, no machine time, and their own inputs
  disappear with them.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

import ctpio as io

SRC = "CTP Data Gaps Report (2).xlsx"
PURCHASED_PAT = r"raw material|outside"
OBSOLETE_PAT = r"obsolete|size out"


def main() -> int:
    # --types-only: no spreadsheet - the list is ONLY `purchased_item_types`
    # expanded against the staged BOM. Used by database-only runs, where the
    # gaps-report workbook is not an allowed source.
    types_only = "--types-only" in sys.argv[1:]
    p = io.ROOT / SRC
    if not types_only and not p.exists():
        print(f"[FATAL] {p} not found")
        return 1
    rows = []
    for sheet in ([] if types_only else pd.ExcelFile(p).sheet_names):
        d = pd.read_excel(p, sheet_name=sheet)
        if "Code" not in d.columns:
            continue
        for r in d.itertuples(index=False):
            code = io._s(getattr(r, "Code", ""))
            if not code:
                continue
            rows.append({
                "code": code,
                "code_type": io._s(getattr(r, "_1", "")),
                "sheet": sheet,
                "remark": io._s(getattr(r, "Remarks", "")),
            })
    a = pd.DataFrame(rows, columns=["code", "code_type", "sheet", "remark"])
    a["rl"] = a["remark"].str.lower()

    pur = (a[a["rl"].str.contains(PURCHASED_PAT, na=False)]
           .drop_duplicates("code").copy())
    pur["verdict"] = "PURCHASED"
    obs = (a[a["rl"].str.contains(OBSOLETE_PAT, na=False)]
           .drop_duplicates("code").copy())
    obs = obs[~obs["code"].isin(set(pur["code"]))]
    obs["verdict"] = "OBSOLETE"

    # ---- ITEM TYPES THE PLANT CONFIRMED AS ALWAYS AVAILABLE --------------
    # ASSUMPTIONS.MD SS C: "Cap strip and edge gum (including GUM STRIP) are
    # assumed available, following V5's logic." That is a plant-confirmed
    # statement about whole ITEM TYPES, not individual codes, so it is expanded
    # here against the BOM into the codes it actually covers - and written into
    # the same register, so there is ONE purchased list and one mechanism.
    extra = []
    try:
        import rules as R
        types = [io._s(x).strip().lower()
                 for x in (R.rule("purchased_item_types") or []) if io._s(x)]
    except Exception:
        types = []
    if types:
        for line in ("pcr", "tbr"):
            try:
                b = io.load_bom(line)
            except Exception:
                continue
            for c, ty in zip(b["child"].map(io._s), b["child_description"].map(io._s)):
                if c and ty.strip().lower() in types:
                    extra.append({"code": c, "verdict": "PURCHASED",
                                  "code_type": "Item code", "sheet": f"rule:{line}",
                                  "remark": f"plant-confirmed always available "
                                            f"(ASSUMPTIONS.MD C) - item type '{ty}'"})
        ex = pd.DataFrame(extra).drop_duplicates("code") if extra else pd.DataFrame()
        if len(ex):
            ex = ex[~ex["code"].isin(set(pur["code"]) | set(obs["code"]))]
            print(f"  by ITEM TYPE ({', '.join(types)}): {len(ex):,} further code(s)")
            pur = pd.concat([pur, ex], ignore_index=True)

    # ---- CODES THE RULE FILE NAMES AS BOUGHT IN (README s.2) -------------
    codes = []
    try:
        import rules as R
        codes = [io._s(x) for x in (R.rule("purchased_codes") or []) if io._s(x)]
    except Exception:
        pass
    if codes:
        have = set(pur["code"]) | set(obs["code"])
        add = pd.DataFrame([{"code": c, "verdict": "PURCHASED", "code_type": "Item code",
                             "sheet": "rule:purchased_codes",
                             "remark": "bought-in compound, never mixed (MIXING_RULES_README s.2)"}
                            for c in codes if c not in have])
        if len(add):
            print(f"  by CODE (purchased_codes): {len(add)} code(s): {', '.join(add['code'])}")
            pur = pd.concat([pur, add], ignore_index=True)

    out = pd.concat([pur, obs], ignore_index=True)[
        ["code", "verdict", "code_type", "sheet", "remark"]]
    dst = io.DATA / "CTP_Purchased_Items.csv"
    out.to_csv(dst, index=False, encoding="utf-8-sig")

    print("\n" + "=" * 74)
    print(f"  PURCHASED / NOT-MADE LIST  <-  {SRC}")
    print("=" * 74)
    print(f"  PURCHASED (raw material / outside) : {len(pur):,}")
    for r in pur.itertuples(index=False):
        print(f"      {r.code:22s} {r.remark[:52]}")
    print(f"  OBSOLETE / size out                : {len(obs):,}")
    print(f"      {', '.join(obs['code'].head(8))}"
          + (f" ... +{len(obs)-8}" if len(obs) > 8 else ""))

    # how much of this the routing currently contradicts
    for line in ("pcr", "tbr"):
        try:
            made = io.schedulable_items(io.load_routing(line), honour_list=False)
        except TypeError:
            made = io.schedulable_items(io.load_routing(line))
        clash = [c for c in pur["code"] if c in made]
        print(f"  {line.upper()}: {len(clash)} purchased code(s) HAVE a routing row "
              f"- the routing says made, the plant says bought"
              + (f": {', '.join(clash)}" if clash else ""))
    print(f"\n[OK] {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
