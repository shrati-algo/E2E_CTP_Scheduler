#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fourrc_checks.py -- four-roll calendar (901) rules on a finished run (2026-10-06).

    python fourrc_checks.py <run_output_dir> [--xlsx out.xlsx]

  rule 2  CAP 66 slitted material bypassed: no 'CAP 66- slitted material' lots
  rule 3  capacity: steel / fabric run minutes per plant day (07:00 -> 07:00) at routing line speed x
          efficiency vs 1440 min minus PM/MTC minutes on 901 -- separately and combined (report)
  rule 4  run time = metres / (routing proc_time x efficiency) for every 901 lot (+-1 min)
  rule 5  one creel: a steel lot that needs a different cord (matrix 'Steel to Steel Changeover') starts
          >= 240 min after the last lot of the old cord, and the 240 is not stacked on the 64-min changeover
Steel / fabric and pair types come from the changeover matrix (machine '4 RC Calendar') only.
"""
import math
import os
import re
import sys

import numpy as np
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))


def main(run, out=None):
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    fr = cfg.get("fourrc_rules") or {}
    M = str(fr.get("machine", "901"))
    setup = float((fr.get("creel") or {}).get("setup_min", 240))
    al = {str(k).upper(): str(v).upper() for k, v in (fr.get("aliases") or {}).items()}
    ovr = {str(k).upper(): str(v).lower() for k, v in (fr.get("class_override") or {}).items()}

    def n(x):
        k = re.sub(r"[\s\-]", "", str(x).upper()).replace("MOTHERROLL", "")
        return al.get(k, k)

    inp = os.path.join(run, "PHASEWISE", "00_INPUTS")
    mx = pd.read_csv(os.path.join(HERE, "inputs", "changeover_matrix.csv"), low_memory=False)
    mx = mx[mx["machine"].astype(str).str.strip() == str(fr.get("matrix_line", "4 RC Calendar"))]
    pt, cl = {}, {}
    for f, t, ty in zip(mx["from_MaterialCode_O"], mx["to_MaterialCode_O"], mx["changeover_type"].fillna("").astype(str)):
        pt[(n(f), n(t))] = ty.strip().lower()
        g = re.match(r"(fabric|steel) to (fabric|steel)", ty.strip().lower())
        if g:
            cl.setdefault(n(f), g.group(1))
            cl.setdefault(n(t), g.group(2))
    cl.update(ovr)
    # the PLANT routing table (JK_CTP_PPDS.CTP_Master_Routing, last download) -- not the adapted copy
    rt = pd.read_csv(os.path.join(HERE, "db_pipeline", "raw", "JK_CTP_PPDS__CTP_Master_Routing.csv"),
                     dtype=str, low_memory=False)
    on = rt["machines"].astype(str).map(lambda s: M in {x.strip().strip('"') for x in re.split(r"[,;/|]", s)})
    r9 = rt[on].copy()
    r9["k"] = r9["routed_product"].astype(str).str.strip()
    spd = pd.to_numeric(r9["proc_time"], errors="coerce").groupby(r9["k"]).min()
    eff = pd.to_numeric(r9["efficiency"], errors="coerce").groupby(r9["k"]).first().fillna(1.0)
    if cfg.get("efficiency_override") is not None:      # v37: same efficiency as the scheduler
        eff = eff * 0 + float(cfg["efficiency_override"])

    S = pd.read_csv(os.path.join(run, "phase5_schedule_updated.csv"),
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
    S["item"] = S["item"].astype(str).str.strip()
    L = pd.read_csv(os.path.join(run, "phase2_lots_updated.csv"), low_memory=False).set_index("lot_id")
    q = S[S["machine"].astype(str).str.strip() == M].sort_values("scheduled_start").copy()
    q["metres"] = q["lot_id"].map(L["lot_qty"])
    _sp = os.path.join(run, "phase5_spool_consumed.csv")          # v34: lots lengthened to finish the spool
    if os.path.exists(_sp) and os.path.getsize(_sp) > 2:
        try:
            _e = pd.read_csv(_sp)
            if len(_e):
                q["metres"] = q["metres"] + q["lot_id"].map(_e.groupby("lot_id")["added_m"].sum()).fillna(0)
        except Exception:
            pass
    # v102: lots split by consumer timing in phase 5 (<lot>_S<k>) and their parents are not in phase2_lots with their
    # new quantities -> take the metres actually scheduled on the floor for those lots
    _sl = q["lot_id"].astype(str).str.contains(r"_S\d+$")
    if _sl.any():
        _fp5 = os.path.join(run, "phase5_floor_schedule_updated.csv")
        if os.path.exists(_fp5):
            _F = pd.read_csv(_fp5, low_memory=False, usecols=["lot_id", "produce_qty"])
            _fm = _F.groupby("lot_id")["produce_qty"].sum()
            _par = set(q.loc[_sl, "lot_id"].astype(str).str.replace(r"_S\d+$", "", regex=True))
            _use = _sl | q["lot_id"].isin(_par)
            q.loc[_use, "metres"] = q.loc[_use, "lot_id"].map(_fm).fillna(q.loc[_use, "metres"])
    q["class"] = q["item"].map(lambda x: cl.get(n(x), ""))
    q["speed"] = q["item"].map(spd)
    q["eff"] = q["item"].map(eff).fillna(float(cfg.get("efficiency_override") or 0.96))
    q["run_min_routing"] = q["metres"] / (q["speed"] * q["eff"])
    q["run_min_sched"] = (q["scheduled_finish"] - q["scheduled_start"]).dt.total_seconds() / 60
    q["day"] = (q["scheduled_start"] - pd.Timedelta(hours=7)).dt.normalize()
    rows = []

    def add(rule, what, occ, ok, note=""):
        rows.append({"rule": rule, "occurrence": what, "occurrences": int(occ), "followed": int(ok),
                     "compliance_%": round(100 * ok / occ, 2) if occ else None, "note": note})

    # rule 2
    sl = S[S["item"] == "CAP 66- slitted material"]
    add("4RC-2 bypass slitted", "'CAP 66- slitted material' lots (must be 0)", 1, int(len(sl) == 0),
        f"{len(sl)} slitted lots; {int((S['item'] == 'CAP 66-MOTHERROLL').sum())} mother-roll lots on {M}")
    # rule 4
    chk = q.dropna(subset=["run_min_routing"])
    ok4 = (chk["run_min_sched"] - chk["run_min_routing"]).abs() <= 1.0
    nospeed = sorted(q[q["speed"].isna()]["item"].unique())
    add("4RC-4 routing line speed", f"{M} lot run time = metres / (proc_time x efficiency) +- 1 min", len(chk), int(ok4.sum()),
        f"no routing speed: {nospeed}" if nospeed else "")
    # rule 5
    cr, last = [], None
    seq = q.to_dict("records")
    _wsrc = str(fr.get("creel_source", "matrix")).lower() == "frc_wire_type"     # v42: CTP_Master_FRC wire_type
    _fpw = os.path.join(HERE, "inputs", "frc_lengths.csv")
    _wmap = ({n(i): str(w).strip() for i, w in zip(pd.read_csv(_fpw)["item"], pd.read_csv(_fpw)["wire"])}
             if _wsrc and os.path.exists(_fpw) else {})
    for r in seq:
        if r["class"] != "steel":
            continue
        if last is not None and n(last["item"]) != n(r["item"]):
            ty = pt.get((n(last["item"]), n(r["item"]))) or pt.get((n(r["item"]), n(last["item"])))
            if _wmap.get(n(last["item"])) and _wmap.get(n(r["item"])):
                ty = "steel to steel changeover" if _wmap[n(last["item"])] != _wmap[n(r["item"])] else "same wire compound change"
            if ty is None or "compound" not in ty:
                fab = [x for x in seq if last["scheduled_finish"] <= x["scheduled_start"] < r["scheduled_start"]]
                gap = (r["scheduled_start"] - last["scheduled_finish"]).total_seconds() / 60
                cr.append({"old_cord_lot": last["lot_id"], "old_item": last["item"], "old_end": last["scheduled_finish"],
                           "new_cord_lot": r["lot_id"], "new_item": r["item"], "new_start": r["scheduled_start"],
                           "matrix_type": ty or "NOT IN MATRIX", "gap_min": round(gap, 1),
                           "fabric_lots_between": len(fab),
                           "fabric_min_between": round(sum(x["run_min_sched"] for x in fab), 1),
                           "creel_respected": gap >= setup - 1,
                           "not_double_counted": bool(fab) or gap <= setup + 15})
        last = r
    CR = pd.DataFrame(cr)
    if len(CR):
        add("4RC-5 creel (one creel)", f"cord change: next steel >= {setup:g} min after the old cord", len(CR),
            int(CR["creel_respected"].sum()))
        add("4RC-5 creel (one creel)", "creel time not stacked on the serial changeover", len(CR),
            int(CR["not_double_counted"].sum()))
    # rule 3
    dt = pd.read_csv(os.path.join(HERE, "db_pipeline", "raw", "jkplanning_CTP__downtime_reason.csv"))
    dt = dt[dt["Machine ID"].astype(str).str.strip() == M]
    # v35: a lot's routing minutes are split over the plant days it actually runs in (a lot starting at 05:13 and
    # running 7.8 h is mostly next-day load), not booked whole on its start day
    sp = []
    for _, r in q.iterrows():
        s0, f0 = r["scheduled_start"], r["scheduled_finish"]
        tot = max((f0 - s0).total_seconds(), 1.0)
        d0 = (s0 - pd.Timedelta(hours=7)).normalize()
        while d0 + pd.Timedelta(hours=7) < f0:
            a, b = max(s0, d0 + pd.Timedelta(hours=7)), min(f0, d0 + pd.Timedelta(hours=31))
            if b > a:
                sp.append({"day": d0, "class": r["class"],
                           "run_min_routing": r["run_min_routing"] * (b - a).total_seconds() / tot})
            d0 += pd.Timedelta(days=1)
    cap = pd.DataFrame(sp).pivot_table(index="day", columns="class", values="run_min_routing", aggfunc="sum").fillna(0.0)
    for c in ("steel", "fabric"):
        if c not in cap:
            cap[c] = 0.0
    avail = []
    for d in cap.index:
        a, b = d + pd.Timedelta(hours=7), d + pd.Timedelta(hours=31)
        pm = 0.0
        for s0, e0 in zip(pd.to_datetime(dt["Scheduled Start Time"]), pd.to_datetime(dt["Scheduled End Time"])):
            pm += max(0.0, (min(e0, b) - max(s0, a)).total_seconds() / 60)
        avail.append(1440.0 - pm)
    cap["available_min"] = avail
    cap["steel_%"] = (100 * cap["steel"] / cap["available_min"]).round(1)
    cap["fabric_%"] = (100 * cap["fabric"] / cap["available_min"]).round(1)
    cap["combined_%"] = (100 * (cap["steel"] + cap["fabric"]) / cap["available_min"]).round(1)
    cap["status_separate"] = (cap[["steel_%", "fabric_%"]].max(axis=1) <= 100).map({True: "OK", False: "OVER"})
    cap["status_combined"] = (cap["combined_%"] <= 100).map({True: "OK", False: "OVER"})
    cap = cap.reset_index().rename(columns={"steel": "steel_min", "fabric": "fabric_min"})
    add("4RC-3 capacity (separately)", "plant day with steel and fabric each <= available", len(cap),
        int((cap["status_separate"] == "OK").sum()))
    add("4RC-3 capacity (combined)", "plant day with steel + fabric <= available", len(cap),
        int((cap["status_combined"] == "OK").sum()))
    # rule 6 (v27): CTP_Master_FRC roll lengths -- every lot between the minimum roll and one spool / dip,
    # spools / dips end at 0 or >= minimum (no infeasible residual); runs = consecutive same-item lots
    RL = pd.DataFrame()
    _fp = os.path.join(HERE, "inputs", "frc_lengths.csv")
    if os.path.exists(_fp):
        fl = pd.read_csv(_fp).set_index("item")
        q["D"] = q["item"].map(fl["spool_dip_length_m"])
        q["min_roll"] = q["item"].map(fl["min_roll_m"]).fillna(300.0)
        q["run"] = (q["item"] != q["item"].shift()).cumsum()
        x = q.dropna(subset=["D"])
        add("4RC-6 roll lengths", "lot <= one spool / dip length (max roll)", len(x), int((x["metres"] <= x["D"] + 1.0).sum()))
        add("4RC-6 roll lengths", "lot >= minimum roll length (spool_length / MPQ)", len(q),
            int((q["metres"] >= q["min_roll"] - 0.5).sum()))
        RL = x.groupby("item").agg(lots=("metres", "size"), runs=("run", "nunique"), made_m=("metres", "sum"),
                                   D=("D", "first"), min_roll=("min_roll", "first")).reset_index()
        RL["spools_dips"] = np.ceil(RL["made_m"] / RL["D"] - 1e-9).astype(int)
        RL["residual_m"] = (RL["spools_dips"] * RL["D"] - RL["made_m"]).round(1)
        RL["residual_ok"] = (RL["residual_m"] < 0.5) | (RL["residual_m"] >= RL["min_roll"] - 0.5)
        add("4RC-6 roll lengths", "spool / dip ends at 0 or >= minimum roll (no infeasible residual)", len(RL),
            int(RL["residual_ok"].sum()))
        add("4RC-6 roll lengths", f"changeovers on {M} (item switches; report)", int((q["item"] != q["item"].shift()).sum() - 1),
            int((q["item"] != q["item"].shift()).sum() - 1), f"{q['run'].nunique()} runs for {len(q)} lots")
    # rules 7-9 (v34, user): one go per item per plant day; spool / dip entirely consumed before a change;
    # least changeover (minutes by type, steel / fabric blocks per day)
    q["run"] = (q["item"] != q["item"].shift()).cumsum()
    od = q.groupby(["day", "item"])["run"].nunique()
    add("4RC-7 one go per day", "item made in ONE run per plant day (07:00 -> 07:00)", len(od), int((od <= 1).sum()))
    if os.path.exists(_fp):
        fl2 = pd.read_csv(_fp).set_index("item")
        q["wire"] = q["item"].map(fl2["wire"]).fillna(q["item"]) if "wire" in fl2.columns else q["item"]
        rem, cur, bad = {}, None, []
        for r in q.itertuples():
            w, D = r.wire, r.D
            if cur is not None and w != cur and rem.get(cur, 0) > 1.0:
                bad.append((r.scheduled_start, cur, round(rem[cur])))
            if D == D and D:                                 # v38: 1 m tolerance (3000.01 m = one 3000 m spool)
                rr = rem.get(w, 0.0)
                rr = rr - r.metres if r.metres <= rr + 1.0 else math.ceil((r.metres - rr - 1.0) / D) * D + rr - r.metres
                rem[w] = max(rr, 0.0)
            cur = w
        nchg = int((q["wire"] != q["wire"].shift()).sum() - 1)
        add("4RC-8 spool entirely consumed", "wire / dip change only after its spool is fully used", nchg, nchg - len(bad),
            f"{len(bad)} changes with metres left, e.g. {bad[:3]}" if bad else "")
    # user rule 3 (v40): every steel cord change has fabric running in between (the creel reload window), on every
    # plant day that has a fabric lot on 901; days without any fabric lot cannot comply (reported, not scored)
    if len(CR):
        fd = set(q.loc[q["class"] == "fabric", "day"])
        CR["plant_day"] = (pd.to_datetime(CR["new_start"]) - pd.Timedelta(hours=7)).dt.normalize()
        CR["fabric_that_day"] = CR["plant_day"].isin(fd)
        _wf = pd.read_csv(_fp).set_index("item")["wire"].astype(str).to_dict() if os.path.exists(_fp) else {}
        CR["same_wire"] = [bool(_wf.get(a)) and _wf.get(a) == _wf.get(b) for a, b in zip(CR["old_item"], CR["new_item"])]
        ap = CR[CR["fabric_that_day"] & ~CR["same_wire"]]          # same CTP_Master_FRC wire = same spool, no cord change
        add("4RC-9 steel<->fabric (rule 3)", "steel cord change with fabric between (days with fabric on 901)", len(ap),
            int((ap["fabric_lots_between"] > 0).sum()),
            f"{int((~CR['fabric_that_day']).sum())} cord change(s) on days with no fabric lot (cannot comply, not scored); "
            f"{int(CR['same_wire'].sum())} pair(s) on the same FRC wire (one spool, not a cord change)")
    if _wmap:
        import itertools as _itt
        q["wire"] = q["item"].map(lambda x: _wmap.get(n(x), n(x)))
        q["load"] = (q["wire"] != q["wire"].shift()).cumsum()
        steel_loads = q[q["class"] == "steel"]["load"].nunique()
        add("4RC v42 wire campaigns", "report: steel creel loads / dip loads (same FRC wire_type run together)",
            int(q["load"].nunique()), int(q["load"].nunique()), f"{steel_loads} steel creel loads, "
            f"{int(q[q['class'] != 'steel']['load'].nunique())} fabric dip loads")
        co_ = {(n(a), n(b)): float(t) for a, b, t in zip(mx["from_MaterialCode_O"], mx["to_MaterialCode_O"],
                                                         pd.to_numeric(mx["changeover_time_min"], errors="coerce"))}
        nl = ok_l = 0
        for _, g in q.groupby("load"):
            items = list(dict.fromkeys(g["item"]))
            if len(items) < 2:
                continue
            nl += 1
            cost = lambda o: sum(co_.get((n(a), n(b)), 0.0) for a, b in zip(o, o[1:]))
            best = min(cost(pp) for pp in _itt.permutations(items) if pp[0] == items[0]) if len(items) <= 6 else cost(items)
            ok_l += cost(items) <= best + 0.01
        add("4RC v42 wire campaigns", "items inside a wire load in the cheapest matrix order", nl, ok_l)
    # v60 (user): 4RC-10 -- no idle INSIDE a loaded dip / spool: between two lots of the same FRC wire the calender only
    # takes the matrix changeover; idle there is reported (at a dip / spool boundary) and scored (in the middle of a dip)
    if os.path.exists(_fp) and "D" in q.columns:
        _w10 = dict(zip(pd.read_csv(_fp)["item"].astype(str).str.strip(), pd.read_csv(_fp)["wire"].astype(str).str.strip()))
        q["wire10"] = q["item"].map(lambda x: _w10.get(x, x))
        _co10 = {(n(a), n(b)): float(t) for a, b, t in zip(mx["from_MaterialCode_O"], mx["to_MaterialCode_O"],
                                                          pd.to_numeric(mx["changeover_time_min"], errors="coerce"))}
        rows10, cum, prev = [], 0.0, None
        for r in q.itertuples():
            if prev is not None and prev.wire10 == r.wire10:
                gap = (r.scheduled_start - prev.scheduled_finish).total_seconds() / 60
                co = 0.0 if prev.item == r.item else (_co10.get((n(prev.item), n(r.item))) or 0.0)
                idle = gap - co
                if idle > 5.0:
                    D_ = r.D if r.D == r.D and r.D else None
                    at_b = D_ is None or abs(cum / D_ - round(cum / D_)) < 0.001 or cum % D_ < 1.0 or D_ - cum % D_ < 1.0
                    rows10.append((prev.lot_id, r.lot_id, round(idle / 60, 2), bool(at_b)))
            else:
                cum = 0.0
            cum += r.metres if r.metres == r.metres else 0.0
            prev = r
        nb = sum(1 for x in rows10 if x[3])
        nm = sum(1 for x in rows10 if not x[3])
        add("4RC-10 no idle inside a load", "report: idle between dips / spools of a loaded wire (at a dip / spool boundary)",
            nb, nb, f"{sum(x[2] for x in rows10 if x[3]):.1f} h" + (f", e.g. {[(a, b, h) for a, b, h, _ in rows10 if _][:3]}" if nb else ""))
        nload = int(q["wire10"].ne(q["wire10"].shift()).sum())
        add("4RC-10 no idle inside a load", "no idle in the MIDDLE of a dip / spool (loads)", nload, nload - nm,
            f"{nm} mid-dip idle gap(s): {[(a, b, h) for a, b, h, x in rows10 if not x][:3]}" if nm else "")
    _lv = fr.get("level_loading") or {}
    if not _lv.get("enabled", False) and (fr.get("level_plan") or {}).get("enabled", False):
        _lv = dict(fr.get("level_plan"), enabled=True)          # v44: the level plan's band
    if _lv.get("enabled", False):              # v43: daily 901 output (made metres incl. whole-spool top-up) in the band
        _pe = os.environ.get("CTP_PLAN_END")
        dk = q.groupby("day")["metres"].sum() / 1000
        if _pe:
            dk = dk[dk.index < (pd.Timestamp(_pe) - pd.Timedelta(hours=7)).normalize()]
        lo, hi = float(_lv.get("min_km", 30)), float(_lv.get("max_km", 35))
        if str(_lv.get("mode", "band")).lower() == "average" and len(dk):   # v44 option C: average +- band_km
            lo, hi = float(dk.mean()) - float(_lv.get("band_km", 5)), float(dk.mean()) + float(_lv.get("band_km", 5))
        add("4RC level loading", f"plant day output {lo:g}-{hi:g} km (inside the plan window)", len(dk),
            int(((dk >= lo - 0.05) & (dk <= hi + 0.05)).sum()),
            "km/day: " + ", ".join(f"{d_:%d-%b} {v_:.1f}" for d_, v_ in dk.items()))
    tq = q.copy()
    tq["prev"], tq["pcl"] = tq["item"].shift(), tq["class"].shift()
    tq = tq[tq["prev"].notna() & (tq["prev"] != tq["item"])]
    tq["type"] = [pt.get((n(a), n(b))) or pt.get((n(b), n(a))) or "not in matrix" for a, b in zip(tq["prev"], tq["item"])]
    add("4RC-9 least changeover", "report: changeovers by matrix type", len(tq), len(tq), str(tq["type"].value_counts().to_dict()))
    R = pd.DataFrame(rows)
    print(R.to_string(index=False))
    print(cap.round(0).to_string(index=False))
    if out:
        with pd.ExcelWriter(out) as xw:
            R.to_excel(xw, sheet_name="Summary", index=False)
            cap.to_excel(xw, sheet_name="Capacity by day", index=False)
            CR.to_excel(xw, sheet_name="Creel changes", index=False)
            RL.to_excel(xw, sheet_name="Roll lengths (FRC)", index=False)
            q[["lot_id", "item", "class", "scheduled_start", "scheduled_finish", "metres", "speed", "eff",
               "run_min_routing", "run_min_sched"]].to_excel(xw, sheet_name="901 lots", index=False)
        print("wrote", out)
    return R


if __name__ == "__main__":
    a = sys.argv[1:]
    o = a[a.index("--xlsx") + 1] if "--xlsx" in a else None
    main(a[0], o)
