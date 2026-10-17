# -*- coding: utf-8 -*-
"""
adapters.py — turn a RAW jkplanning_CTP table into the exact schema the phases expect.

Most tables need NOTHING: jkt_bom_pcr, jkt_routing_pcr, jkt_itemType_master_pcr,
jkt_aging_master_pcr, jkt_buffer_master_pcr, opening_wip_pcr and
jkt_changeover_matrix_combined already carry the same column names the workbooks did
(verified against the live DB 2026-07-23).

Two do NOT, and both are documented below:
  * mpq  -- DB column names differ AND the DB carries the SAME item-type naming faults we
            already fixed in jkt_mpq_corrected.xlsx. Loading it raw would silently undo
            that fix, so the SAME corrections are applied here.
  * plan -- CuringSchedule_pcr is the shift-level curing drum but uses different column
            names, and carries no CycleTime_min.
"""
from __future__ import annotations
import pandas as pd
import numpy as np

# ── mpq: USER-CONFIRMED item-type corrections (2026-07-23) ───────────────────────
# The DB's jkt_mpq_pcr is byte-identical to the plant workbook, faults included:
# build_mpq_map keys on itemtype_master's ItemType, so a name that does not match is a
# SILENTLY DEAD MPQ. These renames are the user's own confirmations.
MPQ_RENAME = {
    "Calendar Roll (spool)": "CALANDARED ROLL",
    "Edge strip":            "Steel Belt Edge Strip",
}
# Rows we cannot map to an itemtype_master type. DROPPED rather than guessed, so they can
# never mis-apply. Tracked as open plant questions (MPQ_PLANT_CONFIRMATION_QUESTIONS.xlsx).
MPQ_UNMAPPED = {
    "Calendra Roll (Dipped Cord)",   # 300 M  - CALANDARED ROLL variant? unconfirmed
    "Edge gum",                      # 570/300 M - user will confirm later
    "Chaffer IL( mono 80, mono 120)",# = MONOCHAF80/120 -> SLITTED MATERIAL (ITEM-level, not a type)
    "BRTTL",                         # = BRTTL -> CALANDARED ROLL (ITEM-level, not a type)
    "White SW (Quinto)",             # 1800 M - matches no item and no type
}


def adapt_mpq(df: pd.DataFrame) -> pd.DataFrame:
    """jkt_mpq_pcr -> v6 mpq schema (Item Type / Minimum Run Qty / UOM / Fraction Allowed).

    Duplicate item types (Tread x2, Edge strip x4) are collapsed to ONE row whose
    'Minimum Run Qty' is the plant's values as free text -- phase2's _parse_mpq then takes
    the SMALLEST, so no size is forced to over-produce. Compounds are written as the TEXT
    '3 Batches' so _parse_mpq returns None: they are sized by the routing batch_size and the
    mixing-MPQ rule, NOT floored to 3 pieces (writing a numeric 3 is a real trap).

    'Maximum Run Qty' is deliberately NOT written. v6 supports "no max" only as an ABSENT
    COLUMN (`except KeyError -> None`); a blank column becomes NaN and blows up later.
    """
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    it = "Item_Type" if "Item_Type" in d.columns else "Item Type"
    mn = "Minimum_Run_Qty" if "Minimum_Run_Qty" in d.columns else "Minimum Run Qty"
    d[it] = d[it].astype(str).str.strip()
    d = d[~d[it].isin(MPQ_UNMAPPED)]
    d[it] = d[it].replace(MPQ_RENAME)

    rows = []
    for t, g in d.groupby(it, sort=False):
        uom = str(g["UOM"].iloc[0]).strip() if "UOM" in g.columns else ""
        vals = pd.to_numeric(g[mn], errors="coerce").dropna().tolist()
        if str(uom).lower().startswith("batch"):
            val = "3 Batches" if not vals else f"{int(min(vals))} Batches"
        elif len(vals) > 1:
            val = ", ".join(str(int(v)) if float(v).is_integer() else str(v) for v in vals)
        elif vals:
            v = vals[0]
            val = int(v) if float(v).is_integer() else v
        else:
            val = ""
        rows.append({"Item Type": t, "Minimum Run Qty": val,
                     "UOM": uom, "Fraction Allowed": "NO"})
    return pd.DataFrame(rows)


def adapt_plan(df: pd.DataFrame, cycle_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """CuringSchedule_pcr -> v6 plan schema.

        skuCode <- sizeCode        pressNo <- machineName      qty <- scheduleQuantity
        startTime/endTime/shift/date/remarks map by name.

    !! cycleTime: CuringSchedule_pcr has NO CycleTime_min. Master_Curing_Design_CycleTime_pcr
       has a DESIGN 'Cure Time', but it is NOT the same number: measured on the live data the
       shift sheet says 24/27/15 where the design table says 19/22/14 -- the plan value
       carries load/unload allowance. We therefore DERIVE it from the schedule itself
       (runTime / qty) and fall back to the design table, never silently swapping one for the
       other. Rows keep MOULD_CLEANING / C/O; v6 filters those itself (phase1_5:64-69).
    """
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    qty = pd.to_numeric(d.get("scheduleQuantity"), errors="coerce").fillna(0)
    run = pd.to_numeric(d.get("runTime"), errors="coerce")

    out = pd.DataFrame({
        "plan_id":   "CTP_PCR",
        "skuCode":   d["sizeCode"].astype(str).str.strip(),
        "date":      pd.to_datetime(d.get("date"), errors="coerce"),
        "pressNo":   d["machineName"].astype(str).str.strip(),
        "shift":     d.get("shift", "").astype(str).str.strip(),
        "startTime": pd.to_datetime(d.get("startTime"), errors="coerce"),
        "endTime":   pd.to_datetime(d.get("endTime"), errors="coerce"),
        "qty":       qty,
        "remarks":   d.get("remarks", "").astype(str).str.strip(),
    })
    # derived cycle time (min per tyre on this press); NaN where qty == 0 (cleaning rows)
    with np.errstate(divide="ignore", invalid="ignore"):
        ct = np.where(qty > 0, run / qty.replace(0, np.nan), np.nan)
    out["cycleTime"] = pd.to_numeric(ct, errors="coerce").round(2)

    if cycle_df is not None and len(cycle_df):
        c = cycle_df.copy()
        c.columns = [str(x).strip() for x in c.columns]
        sap = next((x for x in c.columns if "sap" in x.lower()), None)
        cur = next((x for x in c.columns if "cure" in x.lower()), None)
        if sap and cur:
            m = dict(zip(c[sap].astype(str).str.strip(),
                         pd.to_numeric(c[cur], errors="coerce")))
            fill = out["skuCode"].map(m)
            out["cycleTime"] = out["cycleTime"].fillna(fill)
    return out[["plan_id", "skuCode", "date", "pressNo", "shift",
                "startTime", "endTime", "qty", "cycleTime", "remarks"]]


def adapt_buffer(df: pd.DataFrame) -> pd.DataFrame:
    """jkt_buffer_master_pcr already matches; just normalise header whitespace."""
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    return d


def adapt_building_changeover(df: pd.DataFrame) -> pd.DataFrame:
    """Master_Building_ChangeoverTime_pcr -> phase5's building_changeover.csv schema.

    DB carries per-TBM setup minutes:
        MachineCode, 'Same Size(Minutes)', 'Different Size(Minutes)'  (+ man/cost columns)
    phase5 (phase5_forward_placement_v2.py:_bld_co) reads:
        machine_code, same_min, diff_min
    and charges same_min when the previous code on that machine is the SAME tyre size,
    diff_min otherwise. Machines are inch-locked, so it is almost always same_min.
    This REPLACES the flat 40/60 fallback constants with real plant values (28/60 for
    3401-05, 22/42 for 3406-11).
    """
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    mc   = next((c for c in d.columns if c.lower().replace(" ", "") in ("machinecode", "machine_code")), None)
    same = next((c for c in d.columns if "same" in c.lower() and "size" in c.lower()), None)
    diff = next((c for c in d.columns if "different" in c.lower() and "size" in c.lower()), None)
    if not (mc and same and diff):
        raise KeyError(f"building_changeover: expected MachineCode/Same Size/Different Size, got {list(d.columns)}")
    out = pd.DataFrame({
        "machine_code": pd.to_numeric(d[mc], errors="coerce").astype("Int64").astype(str),
        "same_min":     pd.to_numeric(d[same], errors="coerce"),
        "diff_min":     pd.to_numeric(d[diff], errors="coerce"),
    })
    return out.dropna(subset=["same_min", "diff_min"]).reset_index(drop=True)


# key -> adapter. Anything absent is a straight pass-through.
ADAPTERS = {
    "mpq": adapt_mpq,
    "plan": adapt_plan,
    "buffer_master": adapt_buffer,
    "building_changeover": adapt_building_changeover,
}


def adapt(key: str, df: pd.DataFrame, **kw) -> pd.DataFrame:
    fn = ADAPTERS.get(key)
    if fn is None:
        d = df.copy()
        d.columns = [str(c).strip() for c in d.columns]
        return d
    return fn(df, **kw) if key == "plan" else fn(df)
