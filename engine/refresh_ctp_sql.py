#!/usr/bin/env python3
"""refresh_ctp_sql.py - stage every CTP master the engine reads, from TWO databases.

    python refresh_ctp_sql.py --out ../data-sql
    python refresh_ctp_sql.py --out ../data-sql --no-ppds    # planning only

READ ONLY. Every statement is a SELECT; nothing is written back to either DB.

WHY TWO DATABASES
  jkplanning_CTP  375 tables. The planning store. Still the source for the
                  item-type, holiday, cycle-time and curing-press masters.
  JK_CTP_PPDS      The MDM store: cleaner, snake_case, and versioned
                  (version_no / is_current / valid_from / valid_to).

WHICH SIDE WINS, AND WHY - every choice below was MEASURED, not assumed
  PPDS WINS
    bom / routing / changeover   BY INSTRUCTION, 2026-10-23. PPDS now carries
                            CTP_Master_BOM, CTP_Master_Routing and
                            CTP_Master_ChangeoverMatrix, which it did not when
                            this file was first written. BOM and routing are
                            planning's columns plus `tyre_type` (row and SKU
                            counts identical on 2026-10-23); the matrix has
                            planning's columns and its 293,166 rows.
                            Neither matrix prices a mixer, so BR-74's fixed
                            mixing minute is unaffected.
    Master_WC_Master        CTP_Machine_Master.machine_no == WCNAME on 323/323
                            rows, which settles the machine_no/machine_name
                            ambiguity. Description -> machine_name.
    opening_wip             CTP_Opening_WIP matches planning to the decimal for
                            2026-09-26 (33,054 rows, 480,525.578347). Pure
                            column rename; PPDS adds `shift`.
  NO FALLBACK BETWEEN THE TWO DATABASES. A table named as PPDS-sourced is
  staged from PPDS or not at all; planning is never silently substituted.

  PLANNING WINS
    jkt_aging_master_{line} PPDS merges both lines into ONE table with no
                            tyre_type column, but the lines genuinely DISAGREE:
                            537 of 2,412 shared codes differ (B127 max-aging is
                            96 h on PCR, 120 h on TBR). Worse, PPDS disagrees
                            with planning on 1,150 of 2,648 codes (24MG: 360 h
                            vs 24 h). Taking PPDS would silently rewrite every
                            shelf-life decision.
    Master_WC_Master_Curing PPDS is SKU x press; planning is press x MOULD
                            POSITION (LHS/RHS). Phase 7 needs the mould pairing,
                            which PPDS does not carry in any form.
    holiday / itemType      absent from PPDS. (MPQ and buffer are read from
                            PPDS - see PPDS_REFERENCE.)
  MERGED - neither side alone is correct
    Master_Building_Allowable_Machines_{line}
                            PPDS is the better SHAPE (one row per SKU x machine,
                            with is_allowed and cycle_time_sec) and its content
                            matches planning exactly on shared SKUs - but it
                            covers FEWER: PCR 226 of 287, TBR only 87 of 230.
                            Taking PPDS alone would strand 204 SKUs with no
                            allowable machine at all. So PPDS is primary and
                            planning fills the gap, with `source` naming which.

Every adapter below converts PPDS into the EXACT file the loaders already read,
so no phase changes. The manifest records, per table, which database it came
from and which adapter ran.
"""
from __future__ import annotations

import argparse
import os
import datetime as dt
import json
import pathlib
import sys

import pandas as pd

import ctpsql

PLANNING = ctpsql.CFG["database"]          # jkplanning_CTP
PPDS = ctpsql.PPDS_DB                      # JK_CTP_PPDS

# ---------------------------------------------------------------- planning only
# BOM, routing and the changeover matrix moved to PPDS on 2026-10-23 (see
# PPDS_ADAPTERS); what remains here PPDS does not carry, or gets wrong.
PLANNING_TABLES = [
    "jkt_itemType_master_pcr", "jkt_itemType_master_tbr",
    # MPQ and BUFFER ARE NO LONGER READ FROM PLANNING. The engine reads the
    # PPDS masters CTP_Master_MPQ and CTP_Master_Buffer (below), which carry the
    # `Parameters` band and both lines in one table. These two stay staged for
    # cross-checking only - no phase reads them.
    "jkt_mpq_pcr", "jkt_mpq_tbr",
    "jkt_buffer_master_pcr", "jkt_buffer_master_tbr",
    "jkt_holiday_calendar_pcr", "jkt_holiday_calendar_tbr",
    "jkt_building_cycle_times_pcr", "jkt_building_cycle_times_tbr",
    # curing: PPDS grain cannot express mould positions
    "Master_WC_Master_Curing_pcr", "Master_WC_Master_Curing_tbr",
    # BR-10 "Exclude PM/Cutter Shear change engineering downtime from available
    # minutes." v14 did NOT read this table and DATA_GAPS.md recorded the 128
    # unaccounted press-hours as a known blocker. v15 wires it: 76 rows of
    # machine x window, so a PM block is now a hole in the machine calendar
    # rather than an optimistic assumption.
    "pm_mtc_downtime_schedule",
]

CURRENT = "is_current = 1"                 # SCD2: never read superseded rows

# --ppds-only: no table is read from jkplanning_CTP at all. Set by main().
PPDS_ONLY = False

# ------------------------------------------------------------- PPDS sync clock
# The eight tables the plant syncs into PPDS, and when. Six run three times a
# day on the shift boundaries; two are struck once for the 07:00 plant day.
# These are SOURCED FROM PPDS BY INSTRUCTION - planning is not consulted for
# them, even where it holds a same-named table.
#
# The schedule is recorded so a refresh can SAY whether it is reading fresh data
# or something that missed its window; a stale master is otherwise invisible.
PPDS_SYNC = {
    "CTP_Master_MPQ":          "master - on change",
    "CTP_Master_Buffer":       "master - on change",
    "CTP_Master_SKUDesc":      "06:30, 14:30, 22:30",
    "CTP_Master_Aging":        "06:30, 14:30, 22:30",
    "CTP_Curing_Production":   "06:30, 14:30, 22:30",
    "CTP_Opening_WIP":         "06:30, 14:30, 22:30",
    "CTP_Live_Inventory":      "06:30, 14:30, 22:30",
    "CTP_Live_Inventory_Raw":  "06:30, 14:30, 22:30",
    "CTP_GT_Inventory":        "daily 07:15 (for the 07:00 plant day)",
    "CTP_Running_Moulds":      "daily 07:15 (for the 07:00 plant day)",
}


# ------------------------------------------------------------------- adapters
def ad_machine_master(cn_ppds, cn_plan) -> pd.DataFrame:
    """CTP_Machine_Master -> Master_WC_Master's four columns.

    machine_no is WCNAME (323/323 exact); machine_name is the Description.
    """
    d = ctpsql.fetch(
        "SELECT machine_id, machine_no, machine_name, tyre_type, section "
        "FROM CTP_Machine_Master WHERE is_active = 1", cn_ppds)
    return pd.DataFrame({
        "wcID": d["machine_id"],
        "WCNAME": d["machine_no"].astype(str).str.strip(),
        "Description": d["machine_name"].astype(str).str.strip(),
        "Tyre_Type": d["tyre_type"].astype(str).str.strip(),
        "Section": d["section"].astype(str).str.strip(),
    })


WIP_PARTIAL_FRAC = 0.5          # a snapshot under half the usual size is partial


def _latest_complete(counts: pd.DataFrame) -> object | None:
    """The newest inventory_date that is not a half-written snapshot.

    MAX(inventory_date) is the wrong pick. Measured 2026-09-28: PPDS's newest
    snapshot was 2026-09-27 shift B with 7,406 rows / 130,833 qty - 22% of a
    full day - because only one shift had synced. Staging it would have handed
    phase 2 a fifth of the real floor stock and collapsed fulfilment for a
    reason that has nothing to do with the plant. So a date carrying less than
    WIP_PARTIAL_FRAC of the median day is skipped in favour of the last full one.
    """
    if not len(counts):
        return None
    med = float(counts["n"].median())
    ok = counts[counts["n"] >= WIP_PARTIAL_FRAC * med]
    src = ok if len(ok) else counts
    return src.sort_values("inventory_date").iloc[-1]["inventory_date"]


def ad_opening_wip(cn_ppds, cn_plan) -> pd.DataFrame:
    """CTP_Opening_WIP -> opening_wip's column names. PPDS ONLY, by instruction.

    PPDS syncs three times a day on the shift boundaries, so the newest row set
    can be a sync caught half-written: measured 2026-09-28, the head snapshot was
    2026-09-27 shift B with 7,406 rows / 130,833 qty - 22% of a full day - while
    2026-09-26 shift C held 33,054 / 480,525. Staging the head would have handed
    phase 2 a fifth of the floor stock and collapsed fulfilment for a reason that
    has nothing to do with the plant, so the last COMPLETE snapshot is taken.

    Planning is NOT consulted, even when it is a day fresher. That is the
    instruction, and the cost is stated in the run header rather than silently
    worked around.
    """
    cnt = ctpsql.fetch(
        "SELECT inventory_date, COUNT(*) n FROM CTP_Opening_WIP "
        "GROUP BY inventory_date", cn_ppds)
    # CTP_WIP_DATE pins the inventory_date (a plan that starts on a past day
    # must net against THAT day's floor, not the newest one).
    day = os.environ.get("CTP_WIP_DATE", "").strip() or _latest_complete(cnt)
    head = cnt.sort_values("inventory_date").iloc[-1]["inventory_date"] if len(cnt) else None
    # WHICH SYNC OF THAT DAY. PPDS strikes CTP_Opening_WIP at 06:30, 14:30 and
    # 22:30 and keeps all three, so "the stock on 29 Sep" is three different
    # numbers. For a plan that starts at 07:00 the 06:30 strike is the right
    # one: it is the floor as the shift opened. The 14:30 strike already
    # contains shift-A production, which the plan is itself scheduling - netting
    # against it would credit the plant twice for the same tyres.
    #
    #   29 Sep 06:30   7,587 rows   160,910 units   newest produced 06:59:55
    #   29 Sep 14:30   8,266 rows   258,765 units   newest produced 14:28:51
    #
    # CTP_WIP_ASOF pins the strike ('2026-09-29 06:30:00'); unset takes the
    # latest, which is the right default for a run made during the day.
    asof = os.environ.get("CTP_WIP_ASOF", "").strip()
    sql = ("SELECT production_id, produced_time, item_code, item_type, unit, "
           "inventory, max_aging, max_aging_unit, min_aging, min_aging_unit, "
           "inventory_date, shift, updated_at FROM CTP_Opening_WIP "
           "WHERE inventory_date = '" + str(day) + "'")
    if asof:
        sql += " AND updated_at = '" + asof + "'"
    d = ctpsql.fetch(sql, cn_ppds)
    if asof and not len(d):
        raise RuntimeError(f"CTP_Opening_WIP has no sync at {asof} on {day}")
    d = d.rename(columns={
        "item_code": "itemcode", "max_aging": "MaxAging",
        "max_aging_unit": "MaxAgingUnit", "min_aging": "MinAging",
        "min_aging_unit": "MinAgingUnit", "updated_at": "updatedAt"})
    d.attrs["wip_source"] = (f"CTP_Opening_WIP @ {day}"
                            + (f" sync {asof}" if asof else " (latest sync)"))
    d.attrs["wip_db"] = PPDS
    note = "" if str(head) == str(day) else f"  [head {head} skipped: partial sync]"
    print(f"      wip snapshot: {day}"
          + (f" @ {asof} ({len(d):,} rows, {float(d['inventory'].sum()):,.0f} units)"
             if asof else " (latest sync)") + note)
    return d


def ad_aging(line: str, cn_ppds, cn_plan) -> pd.DataFrame:
    """CTP_Master_Aging -> jkt_aging_master_{line}. PPDS ONLY, by instruction.

    TWO CONSEQUENCES, BOTH REAL, BOTH STATED RATHER THAN HIDDEN
      1. PPDS carries NO tyre_type, so the SAME 4,934 rows are written to both
         lines. Planning's two tables genuinely disagree on 537 of 2,412 shared
         codes (B127 max-aging 96 h on PCR, 120 h on TBR); that distinction is
         gone. Whichever line was right, both now get one answer.
      2. PPDS disagrees with planning on 1,150 of 2,648 codes (24MG: 360 h in
         planning, 24 h here - 15x). Shelf life drives need_by, the CPM, the
         placement floor and the curing gate, so this MOVES THE SCHEDULE.
    """
    return ctpsql.fetch(
        "SELECT ItemCode, MaxAging, MinAging, MaxAgingUnit, MinAgingUnit "
        "FROM CTP_Master_Aging", cn_ppds)


def ad_aging_pcr(a, b):
    return ad_aging("pcr", a, b)


def ad_aging_tbr(a, b):
    return ad_aging("tbr", a, b)


def _gt_inventory(line: str, cn_ppds, cn_plan) -> pd.DataFrame:
    """CTP_GT_Inventory -> gt_inventory_manual_{line}, phase 7's GT opening stock.

    NEW INPUT. Phase 7 has been reporting "GT opening stock: none" because the
    file never existed, so every cured tyre had to be built inside the window.
    Green tyres on the rack at plan start are real supply and shorten the chain.
    Phase 7 reads SKUCode + TotalQuantity, so the PPDS columns are renamed to
    those; the stock still perishes against max aging inside phase 7.
    """
    # CTP_GT_DATE pins the day; unset takes the newest.
    day = os.environ.get("CTP_GT_DATE", "").strip()
    when = ("'" + day + "'") if day else (
        "(SELECT MAX(inventory_date) FROM CTP_GT_Inventory WHERE tyre_type = '"
        + line.upper() + "')")
    d = ctpsql.fetch(
        "SELECT sku_code, gt_inventory, inventory_date FROM CTP_GT_Inventory "
        "WHERE tyre_type = '" + line.upper() + "' AND inventory_date = " + when,
        cn_ppds)
    if day and not len(d):
        raise RuntimeError(f"CTP_GT_Inventory has no {line.upper()} rows on {day}")
    # ONE SNAPSHOT. CTP_GT_Inventory keeps a row per shift (A 06:3x, B 14:3x,
    # C 23:2x) for every date, so a whole-date read SUMMED three snapshots of
    # the same rack (22 Oct PCR: 5,892 + 5,429 + 5,010 = 16,331). CTP_GT_ASOF
    # pins the snapshot by updated_at, as CTP_WIP_ASOF does for the WIP; unset,
    # the latest snapshot of the day is taken.
    asof = os.environ.get("CTP_GT_ASOF", "").strip()
    snap = ctpsql.fetch(
        "SELECT updated_at FROM CTP_GT_Inventory WHERE tyre_type = '" + line.upper()
        + "' AND inventory_date = " + when + " ORDER BY updated_at", cn_ppds)
    pick = asof or (str(snap["updated_at"].iloc[-1]) if len(snap) else "")
    if pick:
        d = ctpsql.fetch(
            "SELECT sku_code, gt_inventory, inventory_date, shift, updated_at "
            "FROM CTP_GT_Inventory WHERE tyre_type = '" + line.upper() + "' AND "
            "updated_at = '" + pick + "'", cn_ppds)
        if not len(d):
            raise RuntimeError(f"CTP_GT_Inventory has no {line.upper()} snapshot at {pick}")
        print(f"      gt snapshot {line}: {pick} shift {d['shift'].iloc[0]} "
              f"({len(d)} GT codes, {pd.to_numeric(d['gt_inventory']).sum():,.0f} tyres)")
    return pd.DataFrame({"SKUCode": d["sku_code"].astype(str).str.strip(),
                         "TotalQuantity": pd.to_numeric(d["gt_inventory"],
                                                        errors="coerce").fillna(0.0),
                         "inventory_date": d["inventory_date"]})


def ad_gt_pcr(a, b):
    return _gt_inventory("pcr", a, b)


def ad_gt_tbr(a, b):
    return _gt_inventory("tbr", a, b)


def _allowable(line: str, cn_ppds, cn_plan) -> pd.DataFrame:
    """PPDS rows + planning fallback -> Master_Building_Allowable_Machines_{line}.

    The loader (phase5_placement.py) parses a JSON-ish list in `Machines`, so the
    per-machine PPDS rows are collapsed back into that shape. cycle_time_sec is
    carried through as a new column: additive, so nothing breaks.
    """
    sql = ("SELECT sku_code, sku_description, size, machine_no, cycle_time_sec "
           "FROM CTP_Master_BuildingAllowable "
           "WHERE " + CURRENT + " AND is_allowed = 1 AND tyre_type = %s")
    p = ctpsql.fetch(sql.replace("%s", "'" + line.upper() + "'"), cn_ppds)
    rows = []
    for sku, g in p.groupby(p["sku_code"].astype(str).str.strip()):
        machines = sorted({str(m).strip() for m in g["machine_no"]
                           if str(m).strip()})
        ct = pd.to_numeric(g["cycle_time_sec"], errors="coerce").dropna()
        rows.append({"SKUCode": sku,
                     "SKUDescription": str(g["sku_description"].iloc[0]),
                     "Size": str(g["size"].iloc[0]),
                     "Machines": json.dumps(machines),
                     "cycle_time_sec": float(ct.median()) if len(ct) else None,
                     "source": PPDS})
    out = pd.DataFrame(rows)

    # fill the SKUs PPDS does not carry (PCR 61, TBR 143 as measured) - unless
    # the run is PPDS-only, where a SKU PPDS does not list has no machine
    if PPDS_ONLY:
        out.attrs["wip_source"] = "CTP_Master_BuildingAllowable (no planning fill)"
        return out
    old = ctpsql.fetch(
        "SELECT * FROM `Master_Building_Allowable_Machines_" + line + "`",
        cn_plan)
    have = set(out["SKUCode"]) if len(out) else set()
    gap = old[~old["SKUCode"].astype(str).str.strip().isin(have)].copy()
    if len(gap):
        gap["cycle_time_sec"] = None
        gap["source"] = PLANNING
    return pd.concat([out, gap], ignore_index=True)


def ad_allow_pcr(a, b):
    return _allowable("pcr", a, b)


def ad_allow_tbr(a, b):
    return _allowable("tbr", a, b)


BOM_COLS = ["Super_parent", "Equipment", "grand_parent", "Parent", "Parent_qty",
            "Parent_unit", "child", "child_quantity", "child_Unit",
            "child_description"]
ROUTING_COLS = ["finished_product", "finished_product_stock",
                "successor_products", "routed_product", "operation_seq",
                "operation_name", "department", "Equipment", "machines",
                "alt_machine_count", "flow_type", "predecessor_rule",
                "input_components_from_bom", "proc_time", "proc_time_UOM",
                "batch_size", "batch_UNIT", "transfer_time_min", "is_primary",
                "efficiency", "all_machine_spec"]


def _by_line(table: str, cols: list[str], line: str, cn_ppds) -> pd.DataFrame:
    """One line's rows of a PPDS master that splits lines on `tyre_type`.

    PPDS keeps PCR and TBR in one table; planning kept one table per line. The
    `tyre_type` filter and the planning column list reproduce the file the
    loaders already read, so no phase changes. An empty result is an error, not
    a file: a line with no BOM or routing must stop the refresh, not stage a
    blank master.
    """
    d = ctpsql.fetch(
        "SELECT " + ", ".join("`" + c + "`" for c in cols) + " FROM `" + table
        + "` WHERE tyre_type = '" + line.upper() + "'", cn_ppds)
    if not len(d):
        raise RuntimeError(f"{table} has no {line.upper()} rows")
    return d


# Which group name each line's loader expects in `Equipment` (see _bom).
BOM_GROUP = {"pcr": "TBM PCR", "tbr": "TBM TBR"}
BOM_EDGE = ["Super_parent", "grand_parent", "Parent", "child"]


def _bom(line: str, cn_ppds) -> pd.DataFrame:
    """CTP_Master_BOM -> jkt_bom_{line}, in the shape the loader walks.

    WHAT CHANGED IN THE SOURCE (measured 2026-10-23; planning's jkt_bom_* now
    holds the same rows, so this is the plant's BOM, not a PPDS quirk)
      * `Equipment` is the MACHINE that makes the row's Parent (20 values on
        PCR), where the loader expects ONE group per line: phases 0-4 take
        `bom["Equipment"].iloc[0]` and walk only (SKU, that group).
      * A parent that more than one machine can make is listed once PER MACHINE,
        with its whole subtree under each: IL3 under F270 F and again under K310,
        and every master batch under each of F270 M / F430 1 / F430 2 / F440.
        The quantities in the copies agree to ~0.01%.
      * Raw materials now hang under every master batch. They have no routing,
        so the walk contracts them out as purchased - no change in effect.

    THE CONVERSION
      Quantities are still per-tyre absolute along a path, so a real repeat of
      an edge is a different (component, parent, child) - the old BOM never
      carried two rows on that key (0 on PCR and TBR). The machine copies DO
      share it. So one row per (SKU, grand_parent, Parent, child) is kept - the
      first listed, i.e. the first machine alternative - and `Equipment` is set
      to the line's group.

      Checked against the 2026-09-28 BOM: 47,708 rows vs 46,208 (PCR), 76,857
      vs 83,904 (TBR); green tyre, bead, apex, steel cord and tread edges match
      exactly. Compound and chemical edges differ where the recipes themselves
      changed (e.g. R158 -> MR157 1.1704 kg vs 0.5852) - that is the BOM as the
      plant now states it, and is not corrected here.
    """
    d = _by_line("CTP_Master_BOM", BOM_COLS, line, cn_ppds)
    for c in BOM_EDGE:
        d[c] = d[c].astype(str).str.strip()
    n0 = len(d)
    d = d.drop_duplicates(BOM_EDGE, keep="first").copy()
    d["Equipment"] = BOM_GROUP[line]
    print(f"      bom {line}: {n0:,} rows -> {len(d):,} after dropping "
          f"alternative-machine copies")
    return d


def ad_bom_pcr(a, b):
    return _bom("pcr", a)


def ad_bom_tbr(a, b):
    return _bom("tbr", a)


def ad_routing_pcr(a, b):
    return _by_line("CTP_Master_Routing", ROUTING_COLS, "pcr", a)


def ad_routing_tbr(a, b):
    return _by_line("CTP_Master_Routing", ROUTING_COLS, "tbr", a)


def ad_changeover(cn_ppds, cn_plan) -> pd.DataFrame:
    """CTP_Master_ChangeoverMatrix -> jkt_changeover_matrix_combined.

    Same columns as planning's, both lines in one table already, so no
    reshaping. It prices no mixer - neither did planning's - so mixing still
    takes `changeover.mixing_fixed_min` (BR-74) in phase 5.
    """
    return ctpsql.fetch(
        "SELECT machine, from_MaterialCode_O, to_MaterialCode_O, "
        "changeover_letter, changeover_time_min, changeover_type, updatedAt "
        "FROM CTP_Master_ChangeoverMatrix", cn_ppds)


def _holiday(line: str, cn_ppds) -> pd.DataFrame:
    """CTP_Master_HolidayCalendar -> jkt_holiday_calendar_{line} (same columns
    plus `tyre_type`, which carries the line). Only used with --ppds-only."""
    d = ctpsql.fetch("SELECT * FROM CTP_Master_HolidayCalendar WHERE tyre_type = '"
                     + line.upper() + "'", cn_ppds)
    return d.drop(columns=["tyre_type"])


def ad_holiday_pcr(a, b):
    return _holiday("pcr", a)


def ad_holiday_tbr(a, b):
    return _holiday("tbr", a)


def ad_downtime(cn_ppds, cn_plan) -> pd.DataFrame:
    """CTP_Master_DowntimeSchedule -> pm_mtc_downtime_schedule (identical
    columns). Only used with --ppds-only."""
    return ctpsql.fetch("SELECT * FROM CTP_Master_DowntimeSchedule", cn_ppds)


# With --ppds-only these REPLACE every planning table the engine reads. What is
# NOT replaced, and why:
#   Master_WC_Master_Curing_{line}  PPDS has no LHS/RHS mould position. The
#       loader only uses it to label presses as Curing and skips it when absent;
#       with the curing plan supplied, nothing else reads it.
#   jkt_itemType_master_*, jkt_mpq_*, jkt_buffer_master_*,
#   jkt_building_cycle_times_*      staged for cross-checking only; no phase
#       reads them (MPQ and buffer come from CTP_Master_MPQ / _Buffer).
PPDS_ONLY_ADAPTERS = {
    "jkt_holiday_calendar_pcr": (ad_holiday_pcr, "CTP_Master_HolidayCalendar"),
    "jkt_holiday_calendar_tbr": (ad_holiday_tbr, "CTP_Master_HolidayCalendar"),
    "pm_mtc_downtime_schedule": (ad_downtime, "CTP_Master_DowntimeSchedule"),
}


# ---- v46c / v46e: BOM v2, the FRC master and the reference BOM ---------------
def _bom_v2(line: str, cn_ppds) -> pd.DataFrame:
    """CTP_Master_BOM_v2 -> jkt_bom_{line}, VERBATIM.

    v2 already leaves the cutters and slitters out: the green tyre points
    straight at the calendered roll. A roll that feeds two belts therefore
    appears as two rows with the same (SKU, grand_parent, Parent, child) key
    and different quantities - two belts, which the walk must ADD. So unlike
    `_bom` nothing is de-duplicated; v2 has one Equipment group per line.
    """
    d = _by_line("CTP_Master_BOM_v2", BOM_COLS, line, cn_ppds)
    for c in BOM_EDGE:
        d[c] = d[c].astype(str).str.strip()
    d["Equipment"] = BOM_GROUP[line]
    print(f"      bom v2 {line}: {len(d):,} rows (verbatim)")
    return d


def ad_bom_v2_pcr(a, b):
    return _bom_v2("pcr", a)


def ad_bom_v2_tbr(a, b):
    return _bom_v2("tbr", a)


def ad_frc(cn_ppds, cn_plan) -> pd.DataFrame:
    """CTP_Master_FRC -> CTP_Master_FRC (current rows): the 4-roll calender's
    Spool/Dip Length per item, read by phase 3 (`fourrc_dip`)."""
    return ctpsql.fetch("SELECT * FROM CTP_Master_FRC WHERE is_current = 1", cn_ppds)


# --bom-v2: the engine BOM becomes CTP_Master_BOM_v2, and the pre-v2 BOM is kept
# in reference_bom/ - only for the types of the bypassed cut codes and the
# roll-per-metre factor of cut stock (rules bypass_cut_code_types_from /
# wip.bypass_conversion_bom = "reference_bom").
BOM_V2_ADAPTERS = {
    "jkt_bom_pcr": (ad_bom_v2_pcr, "CTP_Master_BOM_v2"),
    "jkt_bom_tbr": (ad_bom_v2_tbr, "CTP_Master_BOM_v2"),
    "reference_bom/jkt_bom_pcr": (ad_bom_pcr, "CTP_Master_BOM"),
    "reference_bom/jkt_bom_tbr": (ad_bom_tbr, "CTP_Master_BOM"),
    "CTP_Master_FRC": (ad_frc, "CTP_Master_FRC"),
}


# target file name -> (adapter, source table in PPDS)
PPDS_ADAPTERS = {
    # --- the backbone, from PPDS by instruction (2026-10-23) -------------
    "jkt_bom_pcr": (ad_bom_pcr, "CTP_Master_BOM"),
    "jkt_bom_tbr": (ad_bom_tbr, "CTP_Master_BOM"),
    "jkt_routing_pcr": (ad_routing_pcr, "CTP_Master_Routing"),
    "jkt_routing_tbr": (ad_routing_tbr, "CTP_Master_Routing"),
    "jkt_changeover_matrix_combined": (ad_changeover, "CTP_Master_ChangeoverMatrix"),
    # --- the eight instructed PPDS tables -------------------------------
    "opening_wip": (ad_opening_wip, "CTP_Opening_WIP"),
    "jkt_aging_master_pcr": (ad_aging_pcr, "CTP_Master_Aging"),
    "jkt_aging_master_tbr": (ad_aging_tbr, "CTP_Master_Aging"),
    "gt_inventory_manual_pcr": (ad_gt_pcr, "CTP_GT_Inventory"),
    "gt_inventory_manual_tbr": (ad_gt_tbr, "CTP_GT_Inventory"),
    # --- masters PPDS also does better ----------------------------------
    "Master_WC_Master": (ad_machine_master, "CTP_Machine_Master"),
    "Master_Building_Allowable_Machines_pcr":
        (ad_allow_pcr, "CTP_Master_BuildingAllowable + planning"),
    "Master_Building_Allowable_Machines_tbr":
        (ad_allow_tbr, "CTP_Master_BuildingAllowable + planning"),
}

# Staged verbatim for reference and cross-checking. Not read by any phase today;
# named here so the sync clock covers them and nobody has to guess the source.
#   CTP_Live_Inventory      a second stock feed, carrying `match_rule` - which
#                           names WHY a code did not map, and is the direct lead
#                           on the 261 BOM-orphan codes
#   CTP_Live_Inventory_Raw  the unmatched machine dump behind it
#   CTP_Curing_Production   cured actuals per SKU per month, for back-testing
#   CTP_Master_SKUDesc      SKU descriptions
#   CTP_Running_Moulds      mould fitted per press, with life vs target - the
#                           input a mould-availability constraint would need
# CTP_Master_MPQ and CTP_Master_Buffer are staged verbatim but ARE READ by the
# engine - ctpio.load_mpq and ctpio.load_buffer take them as their only source.
# They keep the plant's own column names, the `Parameters` band and the
# `tyre_type` split, so no adapter is wanted: an adapter would be one more place
# a value could change shape between the table and the schedule.
PPDS_REFERENCE = ["CTP_Master_MPQ", "CTP_Master_Buffer",
                  "CTP_Live_Inventory", "CTP_Live_Inventory_Raw",
                  "CTP_Curing_Production", "CTP_Master_SKUDesc",
                  "CTP_Running_Moulds"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(
        pathlib.Path(__file__).resolve().parent.parent / "data-sql"))
    ap.add_argument("--no-ppds", action="store_true",
                    help="stage everything from jkplanning_CTP only")
    ap.add_argument("--ppds-only", action="store_true",
                    help="read NOTHING from jkplanning_CTP: planning tables are "
                         "replaced by PPDS_ONLY_ADAPTERS or left out, and the "
                         "allowable matrix gets no planning fill")
    ap.add_argument("--only", default="",
                    help="comma-separated target files to restage, e.g. "
                         "jkt_bom_pcr,jkt_routing_tbr. Every other staged file "
                         "and its manifest entry is left as it is - a full "
                         "refresh overwrites the files later staging steps "
                         "write (stage_local_masters, stage_wip_merged)")
    ap.add_argument("--bom-v2", action="store_true",
                    help="v46c+: stage CTP_Master_BOM_v2 as the engine BOM (verbatim), "
                         "the pre-v2 BOM into reference_bom/, and CTP_Master_FRC")
    a = ap.parse_args()
    global PPDS_ONLY
    PPDS_ONLY = a.ppds_only
    if a.ppds_only and a.no_ppds:
        ap.error("--ppds-only and --no-ppds contradict each other")
    adapters = dict(PPDS_ADAPTERS)
    if a.ppds_only:
        adapters.update(PPDS_ONLY_ADAPTERS)
    if a.bom_v2:
        adapters.update(BOM_V2_ADAPTERS)
    only = {t.strip() for t in a.only.split(",") if t.strip()}
    if a.no_ppds and only & set(PPDS_ADAPTERS):
        ap.error("--no-ppds cannot restage a PPDS-sourced table")
    unknown = only - set(PLANNING_TABLES) - set(adapters) - set(PPDS_REFERENCE)
    if unknown:
        ap.error("--only names no staged table: " + ", ".join(sorted(unknown)))
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"  planning : {PLANNING} @ {ctpsql.CFG['host']}:{ctpsql.CFG['port']}"
          + ("   [NOT READ --ppds-only]" if a.ppds_only else " (read only)"))
    print(f"  mdm      : {PPDS}" + ("   [SKIPPED --no-ppds]" if a.no_ppds else ""))
    print(f"  staging -> {out}\n")

    now = dt.datetime.now().isoformat(timespec="seconds")
    man_path = out / "_sql_manifest.json"
    if only and man_path.exists():
        # a partial restage UPDATES the manifest: the untouched tables keep the
        # entry that describes the file actually on disk
        man = json.loads(man_path.read_text(encoding="utf-8"))
        man["partial_restages"] = man.get("partial_restages", []) + [
            {"at": now, "tables": sorted(only)}]
    else:
        man = {"host": ctpsql.CFG["host"], "planning_db": PLANNING,
               "ppds_db": None if a.no_ppds else PPDS,
               "ppds_only": bool(a.ppds_only),
               "staged_at": now, "tables": {}}
    want = (lambda t: t in only) if only else (lambda t: True)
    missing: list[str] = []

    cn_plan = None if a.ppds_only else ctpsql.connect(PLANNING)
    cn_ppds = None if a.no_ppds else ctpsql.connect(PPDS)
    try:
        # ---- 1. the backbone, from planning ---------------------------------
        print(f"  [{PLANNING}]")
        for t in ([] if a.ppds_only else PLANNING_TABLES):
            if not want(t):
                continue
            try:
                d = ctpsql.fetch("SELECT * FROM `" + t + "`", cn_plan)
            except Exception as e:
                missing.append(t)
                print(f"    [MISSING] {t:40s} {str(e)[:48]}")
                continue
            d.to_csv(out / f"{t}.csv", index=False, encoding="utf-8-sig")
            man["tables"][t] = {"rows": int(len(d)), "db": PLANNING, "staged_at": now,
                                "source_table": t, "adapter": None,
                                "columns": list(map(str, d.columns))}
            print(f"    {t:40s}{len(d):>9,d} rows")

        # ---- 2. the masters PPDS does better, adapted to the same schema ----
        head = PPDS if not a.no_ppds else PLANNING + " (fallback)"
        print(f"\n  [{head}]")
        for target, (fn, src) in adapters.items():
            if not want(target):
                continue
            try:
                if a.no_ppds:
                    raise RuntimeError("--no-ppds")
                d = fn(cn_ppds, cn_plan)
            except Exception as e:
                # NO FALLBACK. This branch used to stage planning's same-named
                # table instead, "so a refresh is never left half-staged" - and
                # that is precisely how the engine came to run on
                # jkplanning_CTP.opening_wip while the manifest read
                # `CTP_Opening_WIP @ 2026-09-26`. The two tables are not the
                # same data: planning carried 1,055,890 units in `NUM`, PPDS
                # 58,890 in `NOS`/`M` for the same day, and every kilogram of
                # MASTER/FINAL COMPOUND in the plan came from a table nobody had
                # chosen. A half-staged refresh that SAYS so is safer than a
                # complete one that is quietly wrong.
                missing.append(target)
                print(f"    [MISSING - NO FALLBACK] {target:30s} {str(e)[:44]}")
                print(f"               refused to substitute {PLANNING}.{target}"
                      f" - the source is {src}")
                continue
            else:
                # an adapter may legitimately choose planning (ad_opening_wip
                # takes the fresher complete snapshot), so it names the db it
                # actually read rather than being assumed to have used PPDS.
                db = d.attrs.get("wip_db", PPDS)
            (out / f"{target}.csv").parent.mkdir(parents=True, exist_ok=True)
            d.to_csv(out / f"{target}.csv", index=False, encoding="utf-8-sig")
            man["tables"][target] = {"rows": int(len(d)), "db": db, "staged_at": now,
                                     "source_table": d.attrs.get("wip_source", src),
                                     "adapter": fn.__name__,
                                     "columns": list(map(str, d.columns))}
            man["tables"][target]["ppds_sync"] = PPDS_SYNC.get(
                src.split(" +")[0].strip())
            print(f"    {target:40s}{len(d):>9,d} rows   <- {src}")

        # ---- 3. PPDS reference tables, staged verbatim ---------------------
        if not a.no_ppds:
            print(f"\n  [{PPDS} — reference, not read by any phase]")
            for t in PPDS_REFERENCE:
                if not want(t):
                    continue
                try:
                    d = ctpsql.fetch("SELECT * FROM `" + t + "`", cn_ppds)
                except Exception as e:
                    missing.append(t)
                    print(f"    [MISSING] {t:39s} {str(e)[:48]}")
                    continue
                d.to_csv(out / f"{t}.csv", index=False, encoding="utf-8-sig")
                man["tables"][t] = {"rows": int(len(d)), "db": PPDS, "staged_at": now,
                                    "source_table": t, "adapter": None,
                                    "ppds_sync": PPDS_SYNC.get(t),
                                    "read_by_engine": False,
                                    "columns": list(map(str, d.columns))}
                print(f"    {t:40s}{len(d):>9,d} rows")
    finally:
        if cn_plan:
            cn_plan.close()
        if cn_ppds:
            cn_ppds.close()

    man["missing"] = (sorted(set(man.get("missing", [])) - only | set(missing))
                      if only else missing)
    (out / "_sql_manifest.json").write_text(json.dumps(man, indent=2),
                                            encoding="utf-8")
    n_p = sum(1 for v in man["tables"].values() if v["db"] == PLANNING)
    n_m = len(man["tables"]) - n_p
    print(f"\n  {len(man['tables'])} table(s) staged "
          f"({n_p} from {PLANNING}, {n_m} from {PPDS}), {len(missing)} missing")
    print(f"[OK] {out/'_sql_manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
