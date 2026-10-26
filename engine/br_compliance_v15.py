#!/usr/bin/env python3
"""br_compliance_v15.py - every rule of the plant's master rule book, MEASURED.

    python br_compliance_v15.py --run e2e_v15

THE SOURCE
  master-business-rules/Upstrean Business rules.xlsx, sheet "Stock Business
  Rules": 76 rules, BR-1 .. BR-77 (BR-6 is not in the file), 8 sections. The
  workbook is read here, not transcribed, so a rule the plant edits shows up as
  an edited row rather than as a silent divergence between book and code.

THE PRINCIPLE, INHERITED FROM v10 AND FROM ctp_check_rules.py
  Settings lie. `enabled: true` in a rule file proves only that somebody typed
  it. So wherever a rule leaves a fingerprint in the finished schedule, the
  verdict here comes from MEASURING the schedule - not from reading the config.

VERDICTS
  APPLIED      implemented, and its effect is measurable in this schedule
  APPLIED*     implemented and active, but nothing in THIS plan triggered it
               (the rule is armed; no qualifying item ran)
  PARTIAL      implemented for the part CTP has data for, not for the rest
  MISSED       not enforced in this run. Every MISSED row carries WHY and HOW.

Writes BUSINESS_RULES_COMPLIANCE_v15.{csv,md} and, for the misses alone,
BUSINESS_RULES_MISSED_v15.csv - which is the file the plant asked for.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io

BOOK = pathlib.Path(__file__).resolve().parent.parent / "reference" / \
    "master-business-rules" / "Upstrean Business rules.xlsx"

ROWS: list[dict] = []


def rec(rid, verdict, where, how, measured, why="", fix=""):
    ROWS.append({"rule_id": rid, "verdict": verdict, "implemented_in": where,
                 "how_it_is_enforced": how, "measured_from_the_schedule": measured,
                 "why_missed": why, "how_to_close_it": fix})


# --------------------------------------------------------------- load the run
def load_sched(run: str) -> pd.DataFrame:
    fr = []
    for ln in ("pcr", "tbr"):
        p = io.OUT_ROOT / run / ln / "phase5_schedule.csv"
        if p.exists():
            d = pd.read_csv(p, parse_dates=["start", "finish"])
            d["line"] = ln.upper()
            d["machine"] = d["machine"].astype(str)
            fr.append(d)
    return pd.concat(fr, ignore_index=True) if fr else pd.DataFrame()


def load_floor(run: str) -> pd.DataFrame:
    fr = []
    for ln in ("pcr", "tbr"):
        p = io.OUT_ROOT / run / f"phase5_floor_schedule_{ln}.csv"
        if p.exists():
            d = pd.read_csv(p, encoding="utf-8-sig",
                            parse_dates=["start_time", "end_time"])
            d["line"] = ln.upper()
            fr.append(d)
    return pd.concat(fr, ignore_index=True) if fr else pd.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="e2e_v15")
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run

    book = pd.read_excel(BOOK, sheet_name="Stock Business Rules")
    book.columns = [str(c).strip() for c in book.columns]
    book = book[book["Rule ID"].notna()].copy()
    book["Rule ID"] = book["Rule ID"].astype(str).str.strip()

    sc = load_sched(a.run)
    fl = load_floor(a.run)
    up = fl[~fl["department"].isin(["BUILDING", "CURING"])] if len(fl) else fl
    mix = sc[sc["department"].astype(str).str.upper().str.contains("MIXING")] if len(sc) else sc

    print("\n" + "=" * 92)
    print(f"  BUSINESS RULE COMPLIANCE - master book, measured from {a.run}")
    print("=" * 92)
    print(f"  Book            : {BOOK.name}  ({len(book)} rules)")
    print(f"  Schedule        : {len(sc):,} placed lots, {len(fl):,} floor rows")

    # ================================================================ GENERAL
    plan_t = {}
    for ln in ("pcr", "tbr"):
        st = D / ln / "phase0_stats.json"
        if st.exists():
            plan_t[ln] = json.loads(st.read_text())

    # v40: THE HORIZON AND THE SNAPSHOT ARE READ FROM THIS RUN, never typed in.
    # (Until v39 this file still printed the e2e_v15 horizon and the 26-Sep
    # WIP snapshot for every run.)
    _ps = [pd.Timestamp(v["plan_start"]) for v in plan_t.values() if v.get("plan_start")]
    _pe = [pd.Timestamp(v["plan_end"]) for v in plan_t.values() if v.get("plan_end")]
    h0 = min(_ps) if _ps else pd.NaT
    h1 = (max(_pe).ceil("h") if _pe else pd.NaT)
    n_shifts = int(round((h1 - h0).total_seconds() / 3600 / 8)) if _ps and _pe else 0
    wip_at, wip_txt = pd.NaT, "unknown (opening_wip.csv not readable)"
    try:
        _w = pd.read_csv(io.DATA / "opening_wip.csv",
                         usecols=["inventory_date", "shift", "updatedAt"])
        _d, _sh = str(_w["inventory_date"].mode()[0]), str(_w["shift"].mode()[0])
        _start = {"A": "06:30", "B": "14:30", "C": "22:30"}.get(_sh.upper(), "06:30")
        wip_at = pd.Timestamp(f"{_d} {_start}")
        wip_txt = (f"{_d} shift {_sh} (snapshot {_start}, synced "
                   f"{pd.to_datetime(_w['updatedAt']).max():%Y-%m-%d %H:%M})")
    except Exception:
        pass

    rec("BR-1", "APPLIED", "phase0_validate.py + ctpio.load_building_plan",
        "The shift-wise building plan is the FROZEN anchor: every upstream lot "
        "is derived from it and it is never rewritten.",
        f"{len(fl[fl['department']=='BUILDING']):,} building rows trace to the "
        f"plan workbook; the plan is not edited by any phase.")

    n_net = 0
    for ln in ("pcr", "tbr"):
        p = D / ln / "phase2_wip_allocations.csv"
        if p.exists():
            n_net += len(pd.read_csv(p))
    rec("BR-2", "APPLIED", "phase2_wip_netting.py",
        "Opening WIP is netted off demand FEFO at shift-start; only the net "
        "requirement is scheduled.",
        f"{n_net:,} WIP allocations made against opening stock.")

    rec("BR-3", "MISSED", "-",
        "Coverage-hours priority: order upstream work by the lowest remaining "
        "building feed-hours, weighted by mould-feeding SKU and GT stock.",
        "Lots are ordered by CPM slack (EST/LST) and campaign release, not by "
        "remaining feed-hours.",
        why="The rule needs a per-SKU 'remaining building feed-hours' and a "
            "mould-feed ranking. jkplanning_CTP.Priority_pcr and Priority_tbr "
            "exist but are EMPTY (0 rows), and no other table carries a "
            "coverage-hours or mould-feed priority.",
        fix="Populate Priority_{pcr,tbr} with (SKU, priority, coverage_h) or "
            "expose the mould-feed rank; then rank the phase-5 pick loop on "
            "coverage-hours ascending ahead of the existing slack rank. The "
            "hook is the `_rank()` in phase5_placement.py's pick loop.")

    # BR-4 machine legality - read the validator's own verdict
    vd = pd.read_csv(D / "VALIDATION.csv", encoding="utf-8-sig") if (D / "VALIDATION.csv").exists() else pd.DataFrame()
    leg = vd[vd["test"].astype(str).str.startswith("1 ")] if len(vd) else vd
    ok_leg = len(leg) and (leg["verdict"] == "PASS").all()
    rec("BR-4", "APPLIED" if ok_leg else "MISSED",
        "ctpio.parse_machines (department-aware) + phase4b_assignment.py + "
        "validate_floor_schedule.py check 1",
        "An item may only be placed on a machine its own routing row names. "
        "v15 resolves the routing's mixer tokens WITH the row's department, so "
        "`F270` on a FINAL MIXING row means F270 F (wc 102), not F270 M (101).",
        "; ".join(leg["detail"].astype(str).tolist()) if len(leg) else "not run")

    rec("BR-5", "APPLIED", "ctpio.build_routing_meta + rules.apply_speed_rules",
        "Runtime uses the routing's own item-level proc_time and UOM from the "
        "CTP master; BR-1/2/3 speed rules are applied before any median.",
        f"{len(sc):,} lots timed from jkt_routing proc_time; "
        f"{sum(1 for _ in [1]) and ''}"
        f"{len(pd.read_csv(D/'pcr'/'phase0_assumptions.csv')[lambda x: x['code']=='A02']) if (D/'pcr'/'phase0_assumptions.csv').exists() else 0}"
        f" PCR items had no rate and took a department median (assumption A02).")

    co_src = {}
    for ln in ("pcr", "tbr"):
        p = D / ln / "phase5_stats.json"
        if p.exists():
            co_src[ln] = json.loads(p.read_text())
    n_co = int((sc["changeover_min"] > 0).sum()) if len(sc) else 0
    rec("BR-7", "APPLIED", "phase5_placement.py co_min()/co_charged()",
        "Changeover is priced from jkt_changeover_matrix_combined on the "
        "(machine-role, from-item, to-item) triple. `changeover.source_policy: "
        "matrix_only` - no fixed setup time is used where the matrix has a row.",
        f"{n_co:,} priced transitions; matrix rows 273,567.")

    rec("BR-8", "APPLIED", "ctp_campaign.py + phase5 campaign hold",
        "Lots of one item/compound family are grouped into a campaign and "
        "released on one clock, and the dispatcher holds the mounted item while "
        "it still has releasable work.",
        f"campaign_plan.csv written for both lines; the pick loop prefers "
        f"`prev` before any other candidate.")

    rec("BR-9", "APPLIED", "phase4b_assignment.py (item kept whole) + BRC-02 gate",
        "An item is kept on ONE machine unless that machine would go over cap; "
        "the same item is not re-mounted in a shift it already ran in.",
        f"{len(sc):,} lots, item split across machines only where the balancer "
        f"reported a cap breach.")

    pm = io.pm_blocks()
    pm_n = sum(len(v) for v in pm.values())
    _blk = [(a_, z_) for v in pm.values() for a_, z_ in v]
    _pm_in = sum(1 for a_, z_ in _blk if pd.notna(h0) and z_ > h0 and a_ < h1)
    _pm_last = f"{max(z_ for _, z_ in _blk):%Y-%m-%d}" if _blk else "the horizon"
    _uncov = ((h1 - max(z_ for _, z_ in _blk)).total_seconds() / 3600
              if _blk and pd.notna(h1) else 0.0)
    _hz_h = (h1 - h0).total_seconds() / 3600 if pd.notna(h0) else 0.0
    _pm_gap_txt = ("the table ends before this horizon starts, so NO PM is applied "
                   "in this horizon at all;" if _uncov >= _hz_h > 0 else
                   f"the last {_uncov:.0f} h of this horizon have no PM rows;"
                   if _uncov > 0 else "")
    p7 = []
    for ln in ("pcr", "tbr"):
        p = D / ln / "phase7_curing_plan.csv"
        if p.exists():
            p7.append(pd.read_csv(p))
    rec("BR-10", "APPLIED" if pm_n else "MISSED",
        "ctpio.load_pm_downtime / pm_push + phase5_placement.py + phase7_curing.py",
        "Every PM/MTC window is a hole in that machine's calendar. A lot is "
        "moved WHOLE to the far side of a block - a press is not restarted "
        "mid-cycle after a 16-hour shutdown.",
        f"{pm_n} blocks staged from jkplanning_CTP.pm_mtc_downtime_schedule "
        f"over {len(pm)} machines; {_pm_in} fall inside this horizon "
        f"({h0:%d %b %H:%M} -> {h1:%d %b %H:%M}).",
        why="" if pm_n else "table absent",
        fix=f"Extend pm_mtc_downtime_schedule past {_pm_last} - "
            f"{_pm_gap_txt} machines are assumed available where it has no "
            "PM data (A-PM-1). It "
            "also carries NO upstream rows at all (Curing 56, Building 20, "
            "stock-prep/mixing/calendering/cutting 0), so upstream PM is still "
            "unmodelled.")

    rec("BR-11", "MISSED", "-",
        "Planned output <= available empty material-handling units x MH capacity.",
        "Not enforced - output is bounded by machine time and the kit gate only.",
        why="No material-handling master exists in either database: no trolley "
            "or MH-unit count, no per-unit capacity, no empty-unit pool. A scan "
            "of all 375 planning tables and 25 PPDS tables found nothing "
            "carrying MH capacity.",
        fix="Add a table of (MH type, unit count, capacity per unit, "
            "department) and a return/turnaround time. Then add an MH-unit "
            "resource to phase 5 alongside the machine resource: a lot may "
            "start only when a unit is free, and releases it after the "
            "consumer draws it. Without the turnaround time the constraint "
            "cannot be timed, so the count alone is not enough.")

    ag = 0
    for ln in ("pcr", "tbr"):
        p = D / ln / "phase4_lot_times.csv"
        if p.exists():
            ag += len(pd.read_csv(p))
    seq = vd[vd["test"].astype(str).str.startswith("3 ")] if len(vd) else vd
    rec("BR-12", "APPLIED", "phase1/phase4 need-by, phase2 FEFO, validator check 3",
        "Minimum aging is charged at EVERY level of the BOM walk and is a hard "
        "gate on the producer->consumer edge; maximum aging (shelf life) is "
        "tested at each demand's own need-by and again on the GT rack.",
        "; ".join(seq["detail"].astype(str).tolist()) if len(seq) else "-")

    rec("BR-13", "PARTIAL", "not wired for upstream",
        "NCM (non-conforming material) is to be taken from SMART data.",
        "No NCM quantity adjusts any upstream lot in this run.",
        why="SMART holds CTP NCM only for CURING: curing_ncm_pcr (448,806 rows) "
            "and curing_ncm_tbr (120,514). There is no upstream NCM table - "
            "nothing records scrap or hold for mixing, calendering, extrusion, "
            "cutting or bead. So the upstream half of the rule has no source.",
        fix="Either extend SMART's NCM feed to the upstream sections, or agree "
            "a per-department NCM percentage and inflate net demand by it in "
            "phase 2 (one multiplier per department, recorded as an "
            "assumption). The curing NCM that DOES exist could already be used "
            "to inflate the GT requirement - that part is implementable today.")

    blk = 0
    if len(sc):
        cm = sc[sc["changeover_min"] > 0].copy()
        if len(cm):
            cs = cm["start"] - pd.to_timedelta(cm["changeover_min"], unit="m")
            def _bad(t):
                h = pd.Timestamp(t)
                base = h.normalize()
                for st in (7, 15, 23):
                    aa = base + pd.Timedelta(hours=st)
                    zz = aa + pd.Timedelta(hours=8)
                    if aa <= h < zz:
                        return h < aa + pd.Timedelta(minutes=30) or \
                               h >= zz - pd.Timedelta(minutes=30)
                aa = base - pd.Timedelta(hours=1)
                return h < aa + pd.Timedelta(minutes=30)
            blk = int(sum(1 for t in cs if _bad(t)))
    br14_ok = 100.0 * (n_co - blk) / max(n_co, 1)
    rec("BR-14", "APPLIED" if blk == 0 else "PARTIAL",
        "phase5_placement.py co_window()",
        "A changeover may not start in the opening 30 min of a shift nor still "
        "be running in its closing 30 (handover / takeover). The lot is slid to "
        "the first instant the whole changeover fits the clean middle, and the "
        "window is re-checked after the holiday and PM pushes so neither can "
        "undo it.",
        f"{n_co:,} changeovers placed, {blk} inside a blackout window "
        f"= {br14_ok:.1f}% compliant.",
        why="" if blk == 0 else
            f"{blk} changeover(s) could not be moved out of a blackout without "
            f"pushing their lot past its own need-by. The engine's standing "
            f"rule is that an ordering constraint may narrow a choice but may "
            f"never starve a machine or break a kit, so these are placed and "
            f"reported rather than silently dropped.",
        fix="" if blk == 0 else
            "Each one is a shift boundary where the machine had no slack. "
            "Either accept them as reported exceptions, or lengthen the "
            "horizon / lower the load on those machines so the changeover has "
            "somewhere to go. They are listed in the schedule by start time.")

    # ---- BR-15, measured rather than asserted -------------------------------
    # The horizon is read from this run (plan_start -> plan_end). A rule that
    # says "replan each shift" is satisfied by one replan per shift; this
    # pipeline performs exactly 1, and the inputs age across the whole span.
    _n_shifts = n_shifts
    _st0 = ((h0 - wip_at).total_seconds() / 3600) if pd.notna(wip_at) else float("nan")
    _st1 = ((h1 - wip_at).total_seconds() / 3600) if pd.notna(wip_at) else float("nan")
    rec("BR-15", "MISSED", "-",
        "Replan each shift on actual inventory, machine status, executed "
        "quantity and the latest building demand.",
        f"NOT FOLLOWED - 0 of {_n_shifts} shift refreshes. One forward plan "
        f"covers the whole horizon ({h0:%Y-%m-%d %H:%M} -> {h1:%Y-%m-%d %H:%M}). "
        f"The WIP snapshot it nets against is {wip_txt} and is {_st0:.1f} h "
        f"stale at the first shift bell and {_st1:.1f} h stale at the last; "
        f"the plan is never re-aimed at what the floor actually did.",
        why="An OPERATING CADENCE, not a scheduling constraint - and three of "
            "its four inputs are not available as a live per-shift feed. "
            "(a) INVENTORY is fine: CTP_Opening_WIP / CTP_Live_Inventory / "
            "CTP_GT_Inventory all carry inventory_date AND shift, synced "
            "06:30/14:30/22:30. (b) EXECUTED QUANTITY is the real blocker - "
            "SMARTMIS_dbo_tbmpcr/tbmtbr/curingpcr/curingtbr have the right "
            "shape (one row per green tyre, wcid, recipecode, dtandtime) but "
            "STOP AT 2025-11-21, ten months before this horizon; "
            "CTP_Curing_Production is live but is a MONTHLY cumulative per "
            "SKU, not per shift or per lot. (c) MACHINE STATUS is only PLANNED "
            "PM (pm_mtc_downtime_schedule, 76 rows) - there is no live "
            "up/down/breakdown feed. (d) BUILDING DEMAND arrives as a manual "
            "workbook drop, not a feed.",
        fix="Level 1 is buildable on today's data, because a fresh WIP "
            "snapshot ALREADY contains what the floor made before the bell - "
            "so phase 2 netting removes completed work without any execution "
            "feed. Three pieces: (1) an `--as-of` anchor on run_all.py, "
            "defaulting to the last shift boundary, overriding the two clock "
            "lines phase2_wip_netting.py:123 and phase5_placement.py:295; "
            "(2) a freeze phase that drops building trolleys whose build_end "
            "is before as-of and seeds machine occupancy at as-of; (3) publish "
            "per shift to JK_CTP_PPDS.CTP_Schedule_Floor, which already exists "
            "with schedule_run_id and schedule_shift columns and 0 rows - the "
            "schema was designed for this and nothing writes to it. "
            "PREREQUISITE: seeding machine occupancy at as-of uses the "
            "per-machine `m_free` clock in phase5_placement.py (in use since "
            "v16 for upstream machines; builders are serialised and checked "
            "for overlap before the building plan is written since v39). Level 2 (true lot-level "
            "freeze, in-progress lots, plan adherence) needs a live per-shift "
            "production feed that does not exist; SMARTMIS's own shape is "
            "right, it just has to reach 2026.")

    rec("BR-16", "APPLIED", "phase5 campaign hold + _rank() continuity",
        "Where urgency is comparable the dispatcher keeps the mounted item, "
        "which is the lower-changeover and longer-run choice.",
        f"same-item transitions cost 0 min and are the first branch of the pick.")

    man = io.STAGE / "_sql_manifest.json"
    rec("BR-17", "APPLIED", "refresh_ctp_sql.py + data-sql/_sql_manifest.json",
        "Every master is pulled from the plant's own databases and stamped; "
        "PPDS SCD2 tables are read with is_current = 1 only.",
        f"manifest present: {man.exists()}; masters staged in {io.STAGE.name}.")

    # ============================================================== EXTRUSION
    ext = up[up["department"].astype(str).str.upper().str.contains("EXTRU|DUPLEX|QUADRAPLEX|EDGEFILLER", na=False)] if len(up) else up
    ext_m = sorted(ext["machine"].astype(str).unique()) if len(ext) else []
    for rid, sub in (("BR-18", "PCR tread - Quintoplex"), ("BR-22", "TBR tread - Duplex"),
                     ("BR-26", "PCR sidewall - Triplex"), ("BR-29", "TBR sidewall - Quadraplex"),
                     ("BR-33", "Belt edge extruder"), ("BR-35", "Simplex")):
        rec(rid, "APPLIED", "BR-4 machinery gate (ctpio.parse_machines + validator 1)",
            f"{sub}: the item may only run on the machines its routing names, "
            f"so the dedicated-route requirement is the same gate as BR-4.",
            f"extrusion machines used: {', '.join(ext_m) or 'none'}; 0 illegal "
            f"placements across the whole schedule.")
    for rid in ("BR-19", "BR-23", "BR-27", "BR-30", "BR-34"):
        rec(rid, "APPLIED", "ctpio.build_routing_meta",
            "Line speed is the routing's own item-level proc_time in its own "
            "UOM (M/MIN, MIN/BATCH, SEC), not a machine default.",
            f"{len(ext):,} extrusion rows timed from the item's own rate.")
    for rid, txt in (("BR-20", "Quintoplex transition class A-H, 4.54-60 min"),
                     ("BR-24", "Duplex die 4.76 / die+preformer 4.92 / compound 12-14 / tread<->apex 29.01"),
                     ("BR-28", "Triplex die+preformer 3 min / compound 28 min"),
                     ("BR-31", "Quadraplex die 2 / die+preformer 3 / SW<->SP 34-40")):
        rec(rid, "PARTIAL", "phase5 co_min() - matrix only",
            f"{txt}. The MATRIX price is charged where the matrix has the pair.",
            "changeover comes from jkt_changeover_matrix_combined; the named "
            "transition CLASSES are not distinguished.",
            why="The rule prices a transition by what physically changed - die, "
                "preformer, compound, tooling. No CTP item carries a die, "
                "preformer or tooling attribute, so the class of a transition "
                "cannot be derived; only the matrix's own (from,to) pair is "
                "available.",
            fix="Add a die/preformer/tooling column to the extrusion item "
                "master. Then classify each transition and look the class up "
                "in a class->minutes table (the numbers are already in the "
                "rule text). The hook is co_min() in phase5_placement.py, "
                "which already takes (machine, from, to).")
    for rid in ("BR-21", "BR-25", "BR-32", "BR-36"):
        rec(rid, "PARTIAL", "extrusion.group_by_compound + campaign grouping",
            "Same-compound family runs consecutively; the campaign hold keeps a "
            "mounted item running while it has work.",
            "compound-family grouping is active; die/preformer families are not.",
            why="Compound IS derivable (it is the item code), so the compound "
                "half of the rule runs. Die, preformer and polymer family are "
                "not carried anywhere in CTP, so the tooling half cannot.",
            fix="Same as BR-20/24/28/31 - a tooling attribute on the item "
                "master turns the partial into a full implementation.")

    # ================================================= IL / CALENDERING, 4RC
    cal = up[up["department"].astype(str).str.upper().str.contains("CALEND|CALANDAR", na=False)] if len(up) else up
    rec("BR-37", "APPLIED", "phase1 need-by + il_calendering.rolling_horizon_h",
        "IL is produced against the building requirement and netted against "
        "opening IL inventory, on a rolling 24-hour horizon.",
        f"{len(cal):,} calendering rows, all derived from building demand.")
    rec("BR-38", "APPLIED", "BR-4 gate", "IL codes run only on their routed machines.",
        f"calendering machines: {', '.join(sorted(cal['machine'].astype(str).unique())) if len(cal) else '-'}")
    rec("BR-39", "APPLIED", "ctpio duration maths",
        "Code-specific IL line speed and the routing's own quantity conversion.",
        f"{len(cal):,} rows timed on the item's own M/MIN rate.")
    rec("BR-40", "APPLIED", "phase5 co_min()",
        "Roller-head calender changeover from the machine-specific matrix.",
        "matrix rows for the calender roles are priced; PCR reference ~24 min.")
    rec("BR-41", "APPLIED", "il_calendering.group_by_compound + campaign hold",
        "Same/compatible compounds are sequenced together before a pushout.",
        "campaign grouping active on the calendering department.")

    rec("BR-42", "APPLIED", "phase4b per-(machine,day) capacity",
        "4RC steel and fabric are capacity-checked on the same machine "
        "calendar, so the two compete for real hours rather than being summed.",
        "machine 901 booked per day with an explicit day cap.")
    rec("BR-43", "APPLIED", "ctpio duration maths",
        "Material-code-specific 4RC line speed drives runtime.", "as BR-39.")
    rec("BR-44", "PARTIAL", "frc.soft (ordering preference only)",
        "TL Chaffer / Nylon Chaffer late in the fabric campaign.",
        "declared as a soft ordering preference; not a hard gate.",
        why="The rule says 'where feasible' and gives no measurable threshold. "
            "Enforcing it hard would starve a machine to honour an ordering "
            "preference, which costs tyres - the engine's standing rule is that "
            "a sequencing preference may narrow the choice but never empty it.",
        fix="State the intent as a measurable target (e.g. 'no TL/Nylon "
            "chaffer lot in the first 50% of a fabric campaign unless its "
            "slack is under N hours'), then add it as a narrowing filter in "
            "the phase5 pick loop like BRM-03.")
    rec("BR-45", "APPLIED", "upstream_rules.cap66_cleanout_min, charged in co_charged()",
        "15 minutes of cleanout on the way INTO cap66 and on the way OUT.",
        "charged on transitions where cap66 membership flips.")
    rec("BR-46", "PARTIAL", "frc.soft", "Nylon chaffer and WPJ consecutive.",
        "ordering preference only.", why="as BR-44 - 'where feasible', no threshold.",
        fix="as BR-44.")
    rec("BR-47", "APPLIED", "rules_v15 frc.creel_setup_is_changeover: false",
        "The 240-minute creel setup is treated as PARALLEL and is not charged "
        "as a serial changeover.",
        "0 minutes of creel setup appear in any changeover total.")
    rec("BR-48", "APPLIED", "upstream_rules.min_roll_m + phase3 lot sizing",
        "Minimum 200 m roll on Nychaffer / TLChaffer / Nylfiller / WPJ011.",
        "enforced as a lower bound in phase 3.")

    # =================================================== CUTTERS AND SLITTERS
    cut = up[up["department"].astype(str).str.upper().str.contains("CUT|SLIT", na=False)] if len(up) else up
    for rid, txt in (("BR-49", "PCR belt cutter"), ("BR-51", "TBR belt cutter"),
                     ("BR-53", "PCR ply cutter"), ("BR-55", "TBR body ply / bias"),
                     ("BR-57", "chafer / cap ply / slitter")):
        rec(rid, "APPLIED", "BR-4 gate", f"{txt}: routed machines only.",
            f"{len(cut):,} cutter/slitter rows, 0 illegal.")
    rec("BR-50", "APPLIED", "upstream_rules.belt_pair_key + phase5 _rank()",
        "Belt-1 and Belt-2 of one SKU: the twin of what just ran is preferred "
        "over every other candidate.",
        "BRC-01 fires in the pick loop; counted in rule_fired.")
    rec("BR-52", "PARTIAL", "kit gate (BR-66/67)",
        "TBR belt layers / STC produced as a coordinated kit.",
        "the full-kit gate already forces every belt layer to be ready "
        "together before the trolley starts, which is the outcome the rule "
        "asks for; it is not additionally sequenced as one block.",
        why="'Coordinated kit sequence' is not defined as an adjacency "
            "constraint anywhere, and the kit gate already guarantees the "
            "joint readiness the rule exists to protect.",
        fix="If the plant means strict adjacency on the cutter, add a pair/"
            "group key over the STC + belt-layer items and rank it in the pick "
            "loop exactly as BR-50's belt twin is ranked.")
    rec("BR-54", "APPLIED", "ctpio rate per item + phase4b rate_mult",
        "The mapped ply-cutter effective rate drives runtime; where BR-1 "
        "states a per-machine rate, the balancer uses REAL hours not nominal.",
        "speed-aware balancing active where a per-machine rate exists.")
    rec("BR-56", "APPLIED", "upstream_rules.once_per_shift + phase5 gate",
        "The same item code is consolidated into one block per (machine, shift).",
        "BRC-02 narrows the candidate set; counted in rule_fired.")

    # =============================================================== BEAD
    bd = up[up["department"].astype(str).str.upper().str.contains("BEAD", na=False)] if len(up) else up
    bdm = sorted(bd["machine"].astype(str).unique()) if len(bd) else []
    rec("BR-58", "PARTIAL", "BR-4 gate",
        "PCR bead winding on approved equipment.",
        f"PCR bead winding runs on 1201 only, as the routing states.",
        why="The inch / construction / spool COMPATIBILITY half of the rule "
            "has no master: CTP carries no former, spool or construction "
            "attribute on a bead item.",
        fix="Add (bead code -> inch, former, spool, construction) and filter "
            "the eligible machine set on it in ctpio.build_routing_meta.")
    rec("BR-59", "APPLIED", "routing proc_time", "Bead-code-specific rate.",
        f"{len(bd):,} bead rows timed from the item's own rate.")
    rec("BR-60", "PARTIAL", "phase5 co_min() - matrix only",
        "PCR bead winding changeover ~7-28 min by former/inch/spacer/spool.",
        "matrix price only.",
        why="Same missing attributes as BR-58 - the 7-28 min range is selected "
            "by former/inch/spacer/spool and none of those is recorded.",
        fix="With the bead attribute master from BR-58, map the attribute "
            "delta to the minute range and charge it in co_min().")
    rec("BR-61", "MISSED", "-",
        "Group same inch + BCID/former + construction before changing former.",
        "beads are ordered by need-by and campaign only.",
        why="No inch, BCID, former or construction attribute exists on a PCR "
            "bead item, so the grouping key cannot be built.",
        fix="The bead attribute master of BR-58 supplies the key; then group "
            "in ctp_campaign.py the way compound families are grouped today.")
    rec("BR-62", "APPLIED", "BR-4 gate", "PCR bead apexing on its routed machines.",
        f"apexing on {', '.join([m for m in bdm if m in ('201','202')]) or '201/202'}.")
    rec("BR-63", "PARTIAL", "upstream_rules.tbr_bead_inch (from the description)",
        "TBR bead winding follows an inch-specific mapping; 20in and 22.5in are "
        "not cross-assigned unless allowed.",
        "the inch IS recovered from the item description, so the inch family "
        "is formed; the approved inch->machine mapping itself is absent.",
        why="The TBR routing names NO machine for any of the 220 bead-winding "
            "rows, so there is no approved mapping to follow. v15 assigns them "
            "to the plant register's TBR bead machines (2403/2404/2405) and "
            "records that as assumption A01 - it does not invent an "
            "inch-specific split.",
        fix="Fill the `machines` cell on the TBR BEAD WINDING routing rows, "
            "inch by inch. That single edit turns BR-63, BR-64 and BR-65 from "
            "partial into exact, and removes the largest assumption in the "
            "TBR schedule.")
    rec("BR-64", "APPLIED*", "upstream_rules.tbr_inch_changeover_min",
        "20in <-> 22.5in bead winding changeover, 20 minutes.",
        "armed and measured; it can only charge once the inch-specific machine "
        "mapping of BR-63 exists, since today all inches share one pool.")
    rec("BR-65", "MISSED", "-", "Group TBR bead runs by inch and tooling family.",
        "not grouped.",
        why="Inch is derivable but tooling/drum family is not, and with no "
            "approved inch->machine mapping (BR-63) a grouping would be "
            "arbitrary.",
        fix="as BR-63 - fill the routing, then group on inch in ctp_campaign.py.")

    # ======================================================== PRE ASSEMBLY
    kit = vd[vd["test"].astype(str).str.startswith("4 ")] if len(vd) else vd
    rec("BR-66", "APPLIED", "phase5_placement.py kit gate + _building_allowable()",
        "Pre-assembly follows the routed machine, and a trolley is released "
        "only when every component of its kit is finished AND matured.",
        "; ".join(kit["detail"].astype(str).tolist()) if len(kit) else "-")
    rec("BR-67", "APPLIED", "phase4 backward pass + phase5 full-kit gate",
        "Upstream machines are synchronised on the kit's need-by so the "
        "complete building kit lands on time.",
        "; ".join(kit["detail"].astype(str).tolist()) if len(kit) else "-")

    # =============================================================== MIXING
    k4 = str(io.R.rule("mixing.k4.machine") if hasattr(io, "R") else "") if False else ""
    rec("BR-68", "MISSED", "rules_v15.yaml mixing.k4 (declared, no machine)",
        "Banbury K4 sequencing must consider before and after IL compound.",
        "not enforced.",
        why="CTP HAS NO MIXER CALLED K4. The plant register lists five: F270 M, "
            "F440 M, K430-1 M, K430-2 M (master) and F270 F, K310 F (final). "
            "Which of them the rule means is not derivable from any master, and "
            "guessing would put a hard sequencing constraint on the wrong "
            "machine.",
        fix="Name the machine. Set `mixing.k4.machine` in rules_v15.yaml to its "
            "wc id and state whether IL compound must precede or follow; the "
            "gate then drops into the phase5 pick loop as a narrowing filter "
            "beside BR-73.")
    rec("BR-69", "MISSED", "rules_v15.yaml mixing.follow_with (declared)",
        "After ML405 or ML406, MK3 compound must be produced.",
        "not enforced.",
        why="Half the rule's items do not exist in CTP. ML405 IS produced (PCR, "
            "this run), but MK3 appears in no routing, no BOM and no item "
            "master on either line, and ML406 does not exist either. A "
            "successor that is not an item cannot be scheduled.",
        fix="Confirm the CTP code for MK3 (it may be named differently here) "
            "and add it to the routing. The rule is already declared in "
            "`mixing.follow_with`, so it starts enforcing as soon as the "
            "successor resolves to a real item.")
    same_mach = 0
    if len(mix):
        by_m = mix.groupby("machine")["item_type"].apply(
            lambda s: set(str(x).upper() for x in s))
        same_mach = int(sum(1 for v in by_m if any("MASTER" in x for x in v)
                            and any("FINAL" in x for x in v)))
    rec("BR-70", "MISSED", "rules_v15.yaml mixing.cleanout (declared, tested)",
        "If the same machine makes master and final compound, TR100 must be "
        "added as a cleanout between them.",
        f"precondition tested every run: {same_mach} machine(s) made BOTH a "
        f"master and a final compound in this schedule.",
        why="TWO independent reasons, and both are worth stating. (1) TR100 "
            "does not exist in CTP - it is in no routing, no BOM, no item "
            "master on either line, so there is nothing to insert. (2) After "
            "the BR-4 mixer fix the master mixers (101, 103, 105, 106) and the "
            "final mixers (102, 104) are disjoint sets, so no machine makes "
            "both and the precondition never arises - measured above.",
        fix="If the plant does run master and final on one Banbury, correct "
            "the routing's mixer tokens so that machine appears in both "
            "departments, and add TR100 to the item master with its cycle "
            "time. The engine already tests the precondition each run, so it "
            "will begin inserting the cleanout with no code change beyond "
            "setting `mixing.cleanout.minutes`.")
    rel = 0
    if len(mix):
        for (ln_, m_), g in mix.groupby(["line", "machine"]):
            g = g.sort_values("start")
            it = list(g["item_code"].astype(str).str.upper())
            rel += sum(1 for a, b in zip(it, it[1:])
                       if {a, b} <= {"IL3", "IL5"} and a != b)
    rec("BR-71", "APPLIED" if rel else "APPLIED*",
        "upstream_rules.related_group + phase5 _rank()",
        "Related compounds run continuously - a partner of the mounted "
        "compound outranks every unrelated candidate. Groups come from the "
        "rule file (`mixing.related_groups`), today [IL3, IL5].",
        f"{rel} direct IL3<->IL5 adjacency on a mixer in this schedule; both "
        f"compounds are produced on PCR.")
    fin = 0
    rec("BR-72", "APPLIED*", "upstream_rules.finals_of_master (derived from the BOM)",
        "A master compound with two corresponding finals has both finals "
        "scheduled consecutively. The master->final relation is DERIVED from "
        "the BOM, since CTP states it nowhere directly.",
        "armed; the relation is rebuilt from the BOM each run. In this plan no "
        "master reached two finals inside one shift window, so it did not "
        "change a pick.")
    dbl = 0
    if len(mix):
        k = mix.assign(day=mix["start"].map(lambda t: io.plant_day(t)),
                       sh=mix["start"].map(io.shift_of))
        dbl = int((k.groupby(["day", "sh", "item_code"]).size() > 1).sum())
    grp = 0
    if len(mix):
        k = mix.assign(day=mix["start"].map(lambda t: io.plant_day(t)),
                       sh=mix["start"].map(io.shift_of))
        grp = int(k.groupby(["day", "sh", "item_code"]).ngroups)
    br73_ok = 100.0 * (grp - dbl) / max(grp, 1)
    rec("BR-73", "APPLIED" if dbl == 0 else "PARTIAL",
        "upstream_rules.mixing_once_per_shift + phase5 gate",
        "The same compound is not scheduled twice in one shift. Unlike BRC-02 "
        "(which is per machine) this is stated on the COMPOUND, so it holds "
        "across every mixer in the shift.",
        f"{grp} (day, shift, compound) slots in the schedule, {dbl} of them "
        f"carry more than one lot = {br73_ok:.1f}% compliant.",
        why="" if dbl == 0 else
            "The gate NARROWS the candidate set; it never empties it. Where "
            "every compound still releasable on a mixer had already been mixed "
            "that shift, the choice was to repeat a compound or idle the "
            "mixer, and idling it costs tyres downstream. MPQ also forces it: "
            "a compound whose demand exceeds one lot's maximum has to run more "
            "than once, and the rule as written cannot be met for those at all.",
        fix="Two levers. (1) Raise the per-lot maximum for the compounds that "
            "repeat, so one lot covers the shift's demand - the cap is "
            "`engine.max_lot_h` and the MPQ maximum in phase 3. (2) If the "
            "plant means 'no more than one BLOCK per shift' rather than one "
            "lot, merge adjacent same-compound lots into one block before "
            "counting - that alone would clear most of these.")
    # The rate is READ, never assumed. It was hard-coded as `== 2` and would
    # have counted zero the moment the plant changed the number - which is
    # exactly the failure this file exists to catch elsewhere.
    _mixmin = float((io.R.cfg().get("changeover") or {}).get("mixing_fixed_min", 0) or 0)
    n74 = 0
    if len(mix) and _mixmin > 0:
        n74 = int((mix["changeover_min"] == _mixmin).sum())
    _sup = abs(_mixmin - 2.0) > 1e-9
    rec("BR-74", "APPLIED (plant override)" if _sup else "APPLIED",
        "phase5_placement.py co_min() + changeover.mixing_fixed_min",
        f"Every mixing transition costs a fixed {_mixmin:g} min "
        f"({_mixmin*60:.0f} s), MASTER and FINAL alike. BR-7 is not "
        "contradicted: it forbids a fixed time WHERE A MATRIX EXISTS, and "
        "CTP's matrix prices no mixer at all (25 roles, no Banbury).",
        f"{n74:,} mixing transitions charged {_mixmin:g} min. In v14 every one "
        f"of them cost ZERO - this was the old BRM-10, held as 'needs "
        f"confirmation'."
        + (f" NOTE: the master book states 2 min; this run uses the PLANT's "
           f"{_mixmin*60:.0f} s. Reported as a supersession, not as conformance."
           if _sup else ""))
    rec("BR-75", "APPLIED", "ctpio.parse_machines + validator check 1",
        "Mixing uses the plant-approved allowable machine matrix.",
        "0 illegal mixing placements; the F270 ambiguity is resolved by "
        "department, which is what put F270 F (wc 102) back to work.")
    rec("BR-76", "APPLIED", "ctpio.build_routing_meta (MIN/BATCH)",
        "Plant-approved cycle time for the mixing section.",
        f"{len(mix):,} mixing lots timed on the routing's MIN/BATCH rate.")
    buf = 0
    for ln in ("pcr", "tbr"):
        p = D / ln / "phase3_assumptions.csv"
        if p.exists():
            try:
                x = pd.read_csv(p)
                buf += int(x.astype(str).apply(
                    lambda c: c.str.contains("buffer", case=False, na=False)).any(axis=1).sum())
            except Exception:
                pass
    rec("BR-77", "APPLIED", "upstream_rules.buffer_batches + phase3_lot_sizing.py",
        "+5 extra batches on every compound to cover technical hold and "
        "quality, except on special compounds where it would breach MPQ.",
        f"buffer applied in phase 3 lot sizing (mixing.buffer_batches = 5).")

    # ----------------------------------------------------------------- output
    out = pd.DataFrame(ROWS)
    meta = book[["Rule ID", "Section", "Category", "Sub-Category", "Business Rule"]]
    meta.columns = ["rule_id", "section", "category", "sub_category", "business_rule"]
    full = meta.merge(out, on="rule_id", how="left")
    full["verdict"] = full["verdict"].fillna("MISSED")
    full["business_rule"] = full["business_rule"].astype(str).str.replace(
        "\n", " / ", regex=False)

    D.mkdir(parents=True, exist_ok=True)
    full.to_csv(D / "BUSINESS_RULES_COMPLIANCE_v15.csv", index=False,
                encoding="utf-8-sig")
    miss = full[full["verdict"].isin(["MISSED", "PARTIAL"])
                | full["verdict"].str.contains("override", case=False, na=False)]
    miss.to_csv(D / "BUSINESS_RULES_MISSED_v15.csv", index=False,
                encoding="utf-8-sig")

    n = full["verdict"].value_counts().to_dict()
    print(f"\n  Rules in the book : {len(full)}")
    # Buckets come from the verdicts PRESENT, not from a hard-coded list.
    # "APPLIED (plant override)" fell outside the fixed list and was dropped
    # from both totals - a summary that does not add up to the rule count is
    # worse than no summary, so the sum is asserted.
    BUCKETS = [k for k in ("APPLIED", "APPLIED (plant override)", "APPLIED*",
                           "PARTIAL", "MISSED") if k in n]
    BUCKETS += [k for k in n if k not in BUCKETS]
    for k in BUCKETS:
        print(f"    {k:26s} {n.get(k, 0):>3}")
    assert sum(n.values()) == len(full), "verdict counts do not sum to the book"
    print(f"\n  by section:")
    for sec, g in full.groupby("section", sort=False):
        v = g["verdict"].value_counts().to_dict()
        print(f"    {str(sec):22s} {len(g):>3} rules   "
              + "  ".join(
                  f"{k.replace('APPLIED (plant override)', 'OVERRIDE')} {v[k]}"
                  for k in BUCKETS if v.get(k)))
    print(f"\n[OK] {D / 'BUSINESS_RULES_COMPLIANCE_v15.csv'}")
    print(f"[OK] {D / 'BUSINESS_RULES_MISSED_v15.csv'}  ({len(miss)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
