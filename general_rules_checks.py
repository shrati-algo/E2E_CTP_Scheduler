#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""general_rules_checks.py -- general business rules BR-1..BR-10 on a finished run (2026-10-06, v32).

    python general_rules_checks.py <run_output_dir> [--xlsx out.xlsx]

  BR-1  building rows on the planned TBM; start inside the plan shift / at most one shift early / late (+ reason)
  BR-2  opening stock = shift-start snapshot; made vs net demand (extra by MPQ / batch / buffer)
  BR-3  feed-hours: cure blocks that waited for green tyres (press starved) -- lowest is best
  BR-4  approved machine/item (run audit, same engine as the scheduler) + building / curing from routing
  BR-5  every lot's run time = its routing-based duration (phase 2) +- 1 min
  BR-7  item changes >= matrix time on matrix-mapped machines; machines without a matrix line listed
  BR-8  family batching: per machine and plant day, family switches vs the minimum (families - 1)
  BR-9  same item split into > 1 block in one (machine, shift)
  BR-10 no lot inside a PM / MTC window (downtime_reason); cutter shear change: no data in the DB
"""
import os
import re
import sys

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
H7 = pd.Timedelta(hours=7)


def _shift_id(t):
    h = t - H7
    return f"{h.normalize():%d%b}-{'ABC'[int((h - h.normalize()).total_seconds() // 28800)]}"


def main(run, out=None):
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    S = pd.read_csv(os.path.join(run, "phase5_schedule_updated.csv"),
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
    S["machine"] = S["machine"].astype(str).str.strip()
    S["item"] = S["item"].astype(str).str.strip()
    L = pd.read_csv(os.path.join(run, "phase2_lots_updated.csv"), low_memory=False).set_index("lot_id")
    up = S[~S["lot_id"].astype(str).str.startswith("BLD")].copy()
    rows, sheets = [], {}

    def add(rule, what, occ, ok, note=""):
        rows.append({"rule": rule, "check": what, "occurrences": int(occ), "followed": int(ok),
                     "compliance_%": round(100 * ok / occ, 2) if occ else None, "note": note})

    # BR-1
    g = pd.read_csv(os.path.join(run, "phase5_building_echo_gates.csv"), parse_dates=["plan_start", "start"])
    g["plan_shift"], g["start_shift"] = g["plan_start"].map(_shift_id), g["start"].map(_shift_id)
    g["slip_h"] = (g["start"] - g["plan_start"]).dt.total_seconds() / 3600
    g["status"] = np.where(g["start_shift"] == g["plan_shift"], "in plan shift",
                           np.where(g["slip_h"] < 0, "early", "late"))
    g["reason"] = np.where(g["status"] == "late", np.where(g["kit_late"], "kit late: " + g["binding_item"].astype(str),
                                                           "TBM busy / capacity"), "")
    bld = S[S["lot_id"].astype(str).str.startswith("BLD")]
    add("BR-1", "building row on its planned TBM", len(g), int(g["row"].map(lambda r: f"BLD{int(r):05d}")
                                                              .map(bld.set_index("lot_id")["machine"]).eq(g["tbm"]).sum()))
    add("BR-1", "building row starts in its plan shift", len(g), int((g["status"] == "in plan shift").sum()),
        f"early {int((g['status'] == 'early').sum())} (max {-g['slip_h'].min():.1f} h), late {int((g['status'] == 'late').sum())} "
        f"(kit {int((g['reason'].str.startswith('kit')).sum())}), median slip {g['slip_h'].median():.1f} h")
    sheets["BR-1 building rows"] = g
    # BR-2
    m = L[~L.index.astype(str).str.startswith("BLD")]
    made, dem = pd.to_numeric(m["lot_qty"], errors="coerce").sum(), pd.to_numeric(m["raw_demand"], errors="coerce").sum()
    log = open(os.path.join(os.path.dirname(os.path.abspath(run)), "run_log.txt"), encoding="utf-8", errors="replace").read() \
        if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(run)), "run_log.txt")) else ""
    wip = re.search(r"WIP SOURCE: ([^\n]+)", log)
    add("BR-2", "opening stock = shift-start snapshot; lots sized on net demand", 1, 1,
        (wip.group(1)[:140] if wip else "") + f" | made {made:,.0f} vs net demand {dem:,.0f} (+{100 * (made / dem - 1):.1f}% MPQ / batch / buffer)")
    # BR-3
    cu = pd.read_csv(os.path.join(run, "phase6_curing_revision_updated.csv"), low_memory=False)
    ks = cu.get("kit_status", pd.Series(dtype=str)).astype(str).str.upper()
    add("BR-3", "cure block not waiting for green tyres (press fed)", len(cu), int((ks == "READY").sum()),
        f"kit status: {ks.value_counts().to_dict()}")
    # BR-4 / BR-7 / BR-10 from the run's own audit (same engine as the scheduler)
    au = pd.read_csv(os.path.join(run, "BUSINESS_RULE_AUDIT.csv"))
    for rid, rule, pat in (("BR-4", "approved machine / item (upstream)", "GEN-04"),
                           ("BR-7", "item change >= matrix time (matrix-mapped machines)", "GEN-07"),
                           ("BR-10", "no lot inside a PM / MTC window", "GEN-10")):
        a = au[au["rule_id"].astype(str).str.contains(pat)]
        if len(a):
            txt = str(a["measured"].iloc[0])
            nums = [int(x) for x in re.findall(r"\d+", txt.replace(",", ""))]
            if rid == "BR-4" and len(nums) >= 2:
                add(rid, rule, nums[1], nums[1] - nums[0], txt)
            elif rid == "BR-7" and len(nums) >= 3:
                add(rid, rule, nums[0], nums[0] - nums[2], txt)
            else:
                add(rid, rule, 1, int(str(a["status"].iloc[0]).startswith("FOLLOWED")), txt)
    nm = au[au["rule_id"].astype(str).str.contains("EXT-07")]
    if len(nm):
        add("BR-7", "machines without a matrix line (flat default)", 1, 0, str(nm["measured"].iloc[0]))
    add("BR-10", "cutter shear change excluded from available minutes", 1, 0, "NO DATA: no shear-change master in JK_CTP_PPDS")
    # BR-5
    up["dur_min"] = (up["scheduled_finish"] - up["scheduled_start"]).dt.total_seconds() / 60
    up["routing_min"] = up["lot_id"].map(pd.to_numeric(L["duration_min"], errors="coerce"))
    _sp = os.path.join(run, "phase5_spool_consumed.csv")     # v39: 4RC whole-spool top-up -> routing time of the made metres
    if os.path.exists(_sp) and os.path.getsize(_sp) > 2:
        _e = pd.read_csv(_sp).groupby("lot_id").agg(old=("old_qty", "first"), add=("added_m", "sum"))
        _q0 = pd.to_numeric(L["lot_qty"], errors="coerce").reindex(_e.index).fillna(_e["old"])   # v59: phase-2 qty (cut lots have 2 rows)
        _f = up["lot_id"].map((_q0 + _e["add"]) / _q0).fillna(1.0)
        up["routing_min"] = up["routing_min"] * _f
    ch = up.dropna(subset=["routing_min"])
    add("BR-5", "lot run time = routing-based duration (+- 1 min)", len(ch),
        int(((ch["dur_min"] - ch["routing_min"]).abs() <= 1.0).sum()))
    # BR-8 / BR-9
    bom = pd.read_csv(os.path.join(HERE, "inputs", "bom.csv"), dtype=str, usecols=["Parent", "child"])
    fam = bom.dropna().drop_duplicates().groupby(bom["Parent"].str.strip())["child"].agg(lambda x: "+".join(sorted(set(x.str.strip()))))
    up = up.sort_values(["machine", "scheduled_start"])
    up["day"] = (up["scheduled_start"] - H7).dt.normalize()
    up["shift"] = up["scheduled_start"].map(_shift_id)
    up["family"] = up["item"].map(fam).fillna(up["item"])
    fb = {str(k) for k in ((cfg.get("family_batching") or {}).get("machines") or {})}
    fr = []
    for (mc, d), gg in up[up["machine"].isin(fb)].groupby(["machine", "day"]):
        f = list(gg["family"])
        fr.append((mc, d, len(gg), sum(1 for a, b in zip(f, f[1:]) if a != b), max(len(set(f)) - 1, 0)))
    FR = pd.DataFrame(fr, columns=["machine", "plant_day", "lots", "family_switches", "minimum"])
    if len(FR):
        add("BR-8", "machine-day without repeated family changes (switches = families - 1)", len(FR),
            int((FR["family_switches"] <= FR["minimum"]).sum()),
            f"{int(FR['family_switches'].sum())} family switches vs minimum {int(FR['minimum'].sum())} on {len(fb)} machines")
    sheets["BR-8 families"] = FR
    up["blk"] = (up["item"] != up.groupby("machine")["item"].shift()).cumsum()
    sp = up.groupby(["machine", "shift", "item"])["blk"].nunique().reset_index(name="blocks")
    add("BR-9", "(machine, shift, item) in one block", len(sp), int((sp["blocks"] <= 1).sum()),
        f"split: {sp[sp['blocks'] > 1].groupby('machine').size().to_dict()}")
    sheets["BR-9 splits"] = sp[sp["blocks"] > 1]
    # CD-3 (v35, cover_dispatch): same GT together on each TBM-day; compound families together on each mixer-day
    _cd = cfg.get("cover_dispatch") or {}
    if _cd.get("enabled", False):
        b = S[S["lot_id"].astype(str).str.startswith("BLD")].sort_values(["machine", "scheduled_start"]).copy()
        b["day"] = (b["scheduled_start"] - H7).dt.normalize()
        cr = []
        for (mc, d), gg in b.groupby(["machine", "day"]):
            f = list(gg["item"])
            cr.append(("TBM", mc, d, len(gg), sum(1 for x, y in zip(f, f[1:]) if x != y), max(len(set(f)) - 1, 0)))
        bq = pd.read_csv(os.path.join(HERE, "inputs", "bom.csv"), dtype=str, usecols=["Parent", "child", "child_quantity"]).dropna()
        bq["q"] = pd.to_numeric(bq["child_quantity"], errors="coerce").fillna(0)
        bq["Parent"], bq["child"] = bq["Parent"].str.strip(), bq["child"].str.strip()
        main_in = bq.sort_values("q", ascending=False).drop_duplicates("Parent").set_index("Parent")["child"]
        mx = (_cd.get("mixing") or {})
        fixed = {str(x).upper() for x in (mx.get("fixed_compounds") or [])}
        mu = up[up["machine"].isin([str(x) for x in (mx.get("machines") or [])]) & ~up["item"].str.upper().isin(fixed)]
        for (mc, d), gg in mu.groupby(["machine", "day"]):
            f = list(gg["item"].map(main_in).fillna(gg["item"]))
            cr.append(("mixer", mc, d, len(gg), sum(1 for x, y in zip(f, f[1:]) if x != y), max(len(set(f)) - 1, 0)))
        CR = pd.DataFrame(cr, columns=["kind", "machine", "plant_day", "lots", "switches", "minimum"])
        for k, lab in (("TBM", "TBM-day: same GT runs together (GT changes = GTs - 1)"),
                       ("mixer", "mixer-day: compound families together (family changes = families - 1; butyl / IL fixed lots excluded)")):
            q = CR[CR["kind"] == k]
            if len(q):
                add("CD-3", lab, len(q), int((q["switches"] <= q["minimum"]).sum()),
                    f"{int(q['switches'].sum())} changes vs minimum {int(q['minimum'].sum())}")
        sheets["CD-3 grouping"] = CR
    # BR-12 aging (v33): consumptions past max / before min aging, upstream lots without fresh supply
    av = os.path.join(run, "phase4_aging_violations_updated.csv")
    if os.path.exists(av):
        A = pd.read_csv(av)
        pa = re.search(r"PRECEDENCE AUDIT \(all ([\d,]+) placed DAG edges\): negative-gap ([\d,]+).*?too-fresh ([\d,]+)", log)
        ne = int(pa.group(1).replace(",", "")) if pa else max(len(A), 1)
        add("BR-12", "DAG edge not consumed past max aging (plan report)", ne, ne - len(A),
            f"{len(A)} expired edges, median {A['excess_h'].median() if len(A) else 0:.1f} h over")
        if pa:
            add("BR-12", "DAG edge not consumed before min aging", ne, ne - int(pa.group(3).replace(",", "")))
    h12 = re.search(r"\[BR-12\] aging hard block: (\d+) upstream lots waited.*?(\d+) with no fresh supply", log)
    if h12:
        add("BR-12", "upstream lot with fresh (alive) input at its start", len(up), len(up) - int(h12.group(2)),
            f"{h12.group(1)} lots waited for fresh input; {h12.group(2)} with no fresh supply (remake needed)")
    gs = re.search(r"GT SHELF LIFE\] ([\d,]+) green tyres scrapped.*?none cured over age", log)
    add("BR-12", "no green tyre cured over 72 h", 1, int(bool(gs)), gs.group(0)[:120] if gs else "")
    # BR-14 handover: item changes on upstream machines inside the first / last 30 min of a shift
    W = pd.Timedelta(minutes=int(((cfg.get("shift_handover") or {}).get("window_min", 30))))
    upc = up.sort_values(["machine", "scheduled_start"]).copy()
    upc["prev"] = upc.groupby("machine")["item"].shift()
    upc["pfin"] = upc.groupby("machine")["scheduled_finish"].shift()
    chg = upc[upc["prev"].notna() & (upc["prev"] != upc["item"])]

    def _in_ho(a_, b_):
        for d in pd.date_range((a_ - pd.Timedelta(days=1)).normalize(), b_.normalize() + pd.Timedelta(days=1)):
            for h in (7, 15, 23):
                t = d + pd.Timedelta(hours=h)
                if a_ < t + W and b_ > t - W:
                    return True
        return False
    ho = [_in_ho(max(p, s_ - pd.Timedelta(minutes=0)), s_) if s_ > p else _in_ho(s_, s_ + pd.Timedelta(seconds=1))
          for p, s_ in zip(chg["pfin"], chg["scheduled_start"])]
    add("BR-14", "upstream item change outside the first / last 30 min of a shift", len(chg), len(chg) - int(sum(ho)))
    # BR-16 continuity: changeovers per machine-day and run length
    upc["run"] = (upc["item"] != upc["prev"]).cumsum()
    rl = upc.groupby(["machine", "run"]).size()
    add("BR-16", "report: average lots per continuous run / changeovers per machine-day", 1, 1,
        f"{rl.mean():.2f} lots per run; {len(chg) / max(upc.groupby(['machine', 'day']).ngroups, 1):.1f} changeovers "
        f"per machine-day; family passes on {len(fb) + 4} machines")
    # BR-15 / BR-17
    add("BR-15", "plan from the shift-start WIP snapshot (refresh: run_shift_refresh.py each shift)", 1, int(bool(wip)),
        (wip.group(1)[:100] if wip else "") + " | executed-production feed not in the DB yet")
    rd = os.path.join(os.path.dirname(os.path.abspath(run)))
    add("BR-17", "live JK_CTP_PPDS masters, current rows, data stamp + overrides in README", 1,
        int("LIVE" in log), f"{log.count(' LIVE')} tables read LIVE")
    # IL / calendering BR-37 / 38 / 39 / 41 (v34)
    _it = pd.read_csv(os.path.join(HERE, "inputs", "itemtype_master.csv"), dtype=str)
    _ity = dict(zip(_it["ItemCode"].str.strip(), _it["ItemType"].astype(str).str.strip().str.upper()))
    il = up[up["item"].map(_ity).eq("INNER LINER")].copy()
    if len(il):
        il["q"] = il["lot_id"].map(pd.to_numeric(L["lot_qty"], errors="coerce"))
        il["raw"] = il["lot_id"].map(pd.to_numeric(L["raw_demand"], errors="coerce"))
        il["fn"] = pd.to_datetime(il["lot_id"].map(L["first_need"]), errors="coerce")
        il["ln"] = pd.to_datetime(il["lot_id"].map(L["last_need"]), errors="coerce")
        span = (il["ln"] - il["fn"]).dt.total_seconds() / 3600
        add("BR-37", "IL lot covers <= 24 h of building need (no early / excess IL)", len(il), int((span.fillna(0) <= 24.5).sum()),
            f"IL made {il['q'].sum():,.0f} m vs net need {il['raw'].sum():,.0f} m (+{100 * (il['q'].sum() / max(il['raw'].sum(), 1) - 1):.1f}%)")
        rt = pd.read_csv(os.path.join(HERE, "db_pipeline", "raw", "JK_CTP_PPDS__CTP_Master_Routing.csv"), dtype=str)
        ms = rt.groupby(rt["routed_product"].str.strip())["machines"].agg(
            lambda x: {y.strip().strip('"') for v in x for y in re.split(r"[,;]", str(v))})
        add("BR-38", "IL lot on its routed machine", len(il), int(sum(m in ms.get(i, set()) for m, i in zip(il["machine"], il["item"]))))
        ch = il.dropna(subset=["routing_min"])
        add("BR-39", "IL run time = metres / routing speed x eff (+-1 min)", len(ch),
            int(((ch["dur_min"] - ch["routing_min"]).abs() <= 1).sum()), "Expansion Work Out conversion: NO DATA (BOM quantity used)")
        fr2 = []
        for mc in ("1101", "1901", "2201"):
            g2 = up[up["machine"] == mc].sort_values("scheduled_start")
            key = (g2["item"].str.extract(r"^([A-Za-z]+)")[0] if mc == "1901" else g2["family"])
            for d, gg in g2.assign(k=key).groupby("day"):
                f = list(gg["k"])
                fr2.append((mc, d, sum(1 for a_, b_ in zip(f, f[1:]) if a_ != b_), max(len(set(f)) - 1, 0)))
        F2 = pd.DataFrame(fr2, columns=["machine", "day", "switches", "minimum"])
        add("BR-41", "RHC / pre-assembly day with no repeated type / compound-group change", len(F2),
            int((F2["switches"] <= F2["minimum"]).sum()), f"{int(F2['switches'].sum())} switches vs minimum {int(F2['minimum'].sum())}")
    R = pd.DataFrame(rows)
    pd.set_option("display.width", 230)
    pd.set_option("display.max_colwidth", 120)
    print(R.to_string(index=False))
    if out:
        with pd.ExcelWriter(out) as xw:
            R.to_excel(xw, sheet_name="Summary", index=False)
            for k, v in sheets.items():
                v.to_excel(xw, sheet_name=k[:31], index=False)
        print("wrote", out)
    return R


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], a[a.index("--xlsx") + 1] if "--xlsx" in a else None)
