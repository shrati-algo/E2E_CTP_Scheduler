#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_build_cure.py -- READ-ONLY check of a finished schedule's building + curing sheets (2026-10-06).

    python validate_build_cure.py <schedule.xlsx> [--xlsx exceptions.xlsx]

Masters (last download of JK_CTP_PPDS in db_pipeline/raw):
  CTP_Master_Routing          GT BUILDING: SKU -> GT, allowed TBMs, cycle sec, efficiency
                              CURING     : SKU -> allowed presses, cure time (MIN / SEC)
  CTP_Master_ChangeoverMatrix building lines: 'different size' time = TBR Building 'Size Change' (24),
                              PCR Building BJ / CONTI 'Inch to Inch - Setup Change' (60 / 42)
  downtime_reason             PM / MTC windows (available = 1440 - downtime per plant day 07:00->07:00)

  B1 eligibility  every build row's TBM is in the routing machine list of its SKU's GT
  B2 capacity     per TBM per plant day: sum(qty x cycle_sec / eff)/60 + changeovers x different-size
                  time <= available minutes (every GT change is a changeover; PCR checked at BJ 60 = worst
                  case, and at CONTI 42 -- the masters do not say which TBM is BJ / CONTI)
  C1 eligibility  every cure block's press is in the routing CURING machine list of its SKU
  C2 capacity     q_shift = cavities x 480 / (cure + allowance) (plant formula, allowance 2.5 PCR / 6.0 TBR);
                  cure minutes of a block = qty x 480 / q_shift; per line per plant day
                  sum <= presses x available, and per press per plant day sum <= available
"""
import os
import re
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "db_pipeline", "raw")
DAY = pd.Timedelta(hours=7)
CAV, ALW = 2.0, {"PCR": 2.5, "TBR": 6.0}


def mlist(s):
    return {x.strip().strip('"') for x in re.split(r"[,;]", str(s)) if x.strip().strip('"')}


def tbm_code(m):
    m = str(m).strip()
    g = re.match(r"TBMPCR(\d+)Stage2$", m)
    if g:
        return f"34{int(g.group(1)):02d}"
    g = re.match(r"TBM-?\s*0?(\d+)$", m)
    if g:
        return f"TBM {int(g.group(1)):02d}"
    return m


def by_day(s, e, val):
    """split val over plant days pro-rata to the [s, e) overlap -> {day: share}"""
    out, tot = {}, (e - s).total_seconds()
    d = (s - DAY).normalize()
    while d + DAY < e:
        a, b = max(s, d + DAY), min(e, d + DAY + pd.Timedelta(days=1))
        if b > a:
            out[d] = out.get(d, 0.0) + (val * (b - a).total_seconds() / tot if tot > 0 else val)
        d += pd.Timedelta(days=1)
    return out or {(s - DAY).normalize(): val}


def main(xlsx, out=None):
    rt = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_Routing.csv"), dtype=str, low_memory=False)
    mx = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_ChangeoverMatrix.csv"), dtype=str, low_memory=False)
    dt = pd.read_csv(os.path.join(RAW, "jkplanning_CTP__downtime_reason.csv"))
    dt["s"] = pd.to_datetime(dt["Scheduled Start Time"], format="mixed")
    dt["e"] = pd.to_datetime(dt["Scheduled End Time"], format="mixed")
    dt["m"] = dt["Machine ID"].astype(str).str.strip()

    def avail(machines, d):
        a, b = d + DAY, d + DAY + pd.Timedelta(days=1)
        x = dt[dt["m"].isin(machines) & (dt["e"] > a) & (dt["s"] < b)]
        return 1440.0 - sum((min(e, b) - max(s, a)).total_seconds() / 60 for s, e in zip(x["s"], x["e"]))

    # different-size changeover time per building line, from the matrix
    mx["t"] = pd.to_numeric(mx["changeover_time_min"], errors="coerce")
    ty = mx["changeover_type"].fillna("").str.lower()
    DS = {"TBR": mx.loc[(mx["machine"] == "TBR Building") & ty.str.contains("size change"), "t"].max(),
          "PCR_BJ": mx.loc[(mx["machine"] == "PCR Building BJ") & ty.str.contains("inch to inch"), "t"].max(),
          "PCR_CONTI": mx.loc[(mx["machine"] == "PCR Building CONTI") & ty.str.contains("inch to inch"), "t"].max()}
    print("different-size changeover (matrix):", DS)
    rows = []

    def add(rule, what, occ, ok, note=""):
        rows.append({"rule": rule, "check": what, "occurrences": int(occ), "followed": int(ok),
                     "exceptions": int(occ - ok), "compliance_%": round(100 * ok / occ, 2) if occ else None,
                     "note": note})

    # ---------------------------------------------------------------- BUILDING
    rb = rt[rt["operation_name"] == "GT BUILDING"].copy()
    rb["sku"] = rb["finished_product"].str.strip()
    rb["gt"] = rb["routed_product"].str.strip()
    rb["sec"] = pd.to_numeric(rb["proc_time"], errors="coerce")
    rb["eff"] = pd.to_numeric(rb["efficiency"], errors="coerce").fillna(1.0)
    _eov = __import__("yaml").safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
                                              encoding="utf-8")).get("efficiency_override")
    if _eov is not None:                                  # v37: same efficiency as the scheduler
        rb["eff"] = float(_eov)
    rb["ms"] = rb["machines"].map(mlist)
    by_sku = rb.set_index("sku")
    gt_ms = rb.groupby("gt")["ms"].agg(lambda s: set().union(*s))
    gt_sec = rb.groupby("gt").agg(sec=("sec", "max"), eff=("eff", "first"))

    B = __import__("schedule_changeovers").read_sched(xlsx, "Building")     # PCR / TBR sheets, lot rows
    B["start_time"], B["end_time"] = pd.to_datetime(B["start_time"]), pd.to_datetime(B["end_time"])
    B["sku"], B["item"] = B["sku"].astype(str).str.strip(), B["item"].astype(str).str.strip()
    B["mcode"] = B["machine"].map(tbm_code)
    B["routed_gt"] = B["sku"].map(by_sku["gt"]) if by_sku.index.is_unique else B["sku"].map(by_sku["gt"].groupby(level=0).first())
    B["allowed"] = B["item"].map(gt_ms)
    B["allowed"] = [a if isinstance(a, set) else gt_ms.get(g, set()) if isinstance(g, str) else set()
                    for a, g in zip(B["allowed"], B["routed_gt"])]
    B["eligible"] = [m in a for m, a in zip(B["mcode"], B["allowed"])]
    B["gt_matches_routing"] = B["routed_gt"].isna() | (B["routed_gt"] == B["item"])
    B["no_routing"] = [not a for a in B["allowed"]]
    B["allowed_machines"] = B["allowed"].map(lambda a: ", ".join(sorted(a)))
    e1 = B[~B["eligible"]]
    add("B1 building eligibility", "build row on a TBM its GT is routed to", len(B), int(B["eligible"].sum()),
        f"{int(B['no_routing'].sum())} rows whose GT has no GT BUILDING routing; "
        f"{int((~B['gt_matches_routing']).sum())} rows whose GT differs from the routing GT of the SKU")

    B["cyc_sec"] = B["item"].map(gt_sec["sec"])
    B["eff"] = B["item"].map(gt_sec["eff"]).fillna(float(__import__("yaml").safe_load(open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"), encoding="utf-8")).get("efficiency_override") or 0.96))
    B["build_min"] = B["qty"] * B["cyc_sec"] / B["eff"] / 60
    B["sched_min"] = (B["end_time"] - B["start_time"]).dt.total_seconds() / 60
    # the sheet splits a lot at shift ends and rounds each piece's qty: spread the LOT's routing time
    # over its pieces by their scheduled minutes (lot total unchanged)
    _lk = B["lot_id"].astype(str)
    _lt, _ls = B.groupby(_lk)["build_min"].transform("sum"), B.groupby(_lk)["sched_min"].transform("sum")
    B["build_min"] = (_lt * B["sched_min"] / _ls).where(_ls > 0, B["build_min"])
    LOT = B.groupby(_lk).agg(routing_min=("build_min", "sum"), sched_min=("sched_min", "sum"))
    B = B.sort_values(["machine", "start_time"]).reset_index(drop=True)
    B["prev_item"] = B.groupby("machine")["item"].shift()
    B["changeover"] = B["prev_item"].notna() & (B["prev_item"] != B["item"])
    B["gap_before_min"] = (B["start_time"] - B.groupby("machine")["end_time"].shift()).dt.total_seconds() / 60
    B["co_min"] = B["changeover"] * B["line"].map(lambda l: DS["TBR"] if l == "TBR" else DS["PCR_BJ"])
    B["co_min_c"] = B["changeover"] * B["line"].map(lambda l: DS["TBR"] if l == "TBR" else DS["PCR_CONTI"])
    agg = {}
    for r in B.itertuples():
        # changeover minutes sit in the gap just before the new GT -> charged to the day(s) they fall in
        for col, val in (("co_min", r.co_min), ("co_min_c", r.co_min_c)):
            if val > 0:
                for d, v in by_day(r.start_time - pd.Timedelta(minutes=val), r.start_time, val).items():
                    k = (r.line, r.machine, r.mcode, d)
                    a = agg.setdefault(k, {"lots": 0, "qty": 0.0, "build_min": 0.0, "changeovers": 0,
                                           "co_min": 0.0, "co_min_c": 0.0, "no_cycle_rows": 0})
                    a[col] += v
        for d, v in by_day(r.start_time, r.end_time, r.build_min if pd.notna(r.build_min) else 0.0).items():
            k = (r.line, r.machine, r.mcode, d)
            a = agg.setdefault(k, {"lots": 0, "qty": 0.0, "build_min": 0.0, "changeovers": 0,
                                           "co_min": 0.0, "co_min_c": 0.0, "no_cycle_rows": 0})
            a["build_min"] += v
        k = (r.line, r.machine, r.mcode, (r.start_time - DAY).normalize())
        a = agg.setdefault(k, {"lots": 0, "qty": 0.0, "build_min": 0.0, "changeovers": 0,
                                           "co_min": 0.0, "co_min_c": 0.0, "no_cycle_rows": 0})
        a["lots"] += 1
        a["qty"] += r.qty
        a["changeovers"] += int(r.changeover)
        a["no_cycle_rows"] += int(pd.isna(r.build_min))
    BD = pd.DataFrame([{"line": k[0], "machine": k[1], "routing_machine": k[2], "plant_day": k[3], **v}
                       for k, v in agg.items()]).sort_values(["line", "machine", "plant_day"])
    BD["available_min"] = [avail({m, m.replace("TBM 0", "380").replace("TBM ", "38")}, d)
                           for m, d in zip(BD["routing_machine"], BD["plant_day"])]
    tbr = BD["line"] == "TBR"
    BD["required_min"] = BD["build_min"] + BD["co_min"]
    BD["util_%"] = (100 * BD["required_min"] / BD["available_min"]).round(1)
    BD["fits"] = BD["required_min"] <= BD["available_min"] + 0.5
    BD["required_min_CONTI"] = BD["build_min"] + BD["co_min_c"]
    BD["fits_CONTI"] = BD["required_min_CONTI"] <= BD["available_min"] + 0.5
    add("B2 building capacity", "TBM-day: build + changeovers x different-size time <= available "
        f"(PCR at BJ {DS['PCR_BJ']:g}, TBR {DS['TBR']:g})", len(BD), int(BD["fits"].sum()))
    p = BD[~tbr]
    add("B2 building capacity", f"PCR TBM-day at CONTI {DS['PCR_CONTI']:g} min", len(p), int(p["fits_CONTI"].sum()))
    add("B2 building capacity", "lot run time = qty x routing cycle / efficiency (+-1 min)", len(LOT),
        int(((LOT["sched_min"] - LOT["routing_min"]).abs() <= 1.0).sum()))
    co = B[B["changeover"]]
    need = co["line"].map(lambda l: DS["TBR"] if l == "TBR" else DS["PCR_BJ"])
    need_c = co["line"].map(lambda l: DS["TBR"] if l == "TBR" else DS["PCR_CONTI"])
    add("B2 building capacity", "changeover: gap before the new GT >= different-size time (BJ for PCR)",
        len(co), int((co["gap_before_min"] >= need - 0.5).sum()))
    add("B2 building capacity", "changeover: gap before the new GT >= different-size time (CONTI for PCR)",
        len(co), int((co["gap_before_min"] >= need_c - 0.5).sum()))
    COX = co.assign(required_co_min=need, required_co_min_CONTI=need_c)
    COX = COX[COX["gap_before_min"] < COX["required_co_min"] - 0.5]

    # ---------------------------------------------------------------- CURING
    rc = rt[rt["operation_name"] == "CURING"].copy()
    rc["sku"] = rc["routed_product"].str.strip()
    rc["cure"] = pd.to_numeric(rc["proc_time"], errors="coerce")
    rc.loc[rc["proc_time_UOM"].astype(str).str.upper() == "SEC", "cure"] /= 60
    rc["ms"] = rc["machines"].map(mlist)
    rc = rc.groupby("sku").agg(ms=("ms", lambda s: set().union(*s)), cure=("cure", "first"))
    C = __import__("schedule_changeovers").read_sched(xlsx, "Curing")
    C["startTime"], C["endTime"] = pd.to_datetime(C["startTime"]), pd.to_datetime(C["endTime"])
    C["skuCode"] = C["skuCode"].astype(str).str.strip()
    C["press"] = C["pressNo"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    C["allowed"] = C["skuCode"].map(rc["ms"]).map(lambda a: a if isinstance(a, set) else set())
    C["eligible"] = [p in a for p, a in zip(C["press"], C["allowed"])]
    C["no_routing"] = C["allowed"].map(lambda a: not a)
    C["allowed_presses"] = C["allowed"].map(lambda a: ", ".join(sorted(a)))
    add("C1 curing eligibility", "cure block on a press allowed by the routing", len(C), int(C["eligible"].sum()),
        f"{int(C['no_routing'].sum())} blocks whose SKU has no CURING routing")
    C["cure_routing_min"] = C["skuCode"].map(rc["cure"])
    C["cure_used"] = C["cure_routing_min"].fillna(C["cycleTime"])
    C["q_shift"] = (CAV * 480 // (C["cure_used"] + C["line"].map(ALW))).clip(lower=1)   # tyres / press / shift
    C["over_q_shift"] = C["qty"] > C["q_shift"]
    C["cure_min"] = C["qty"] * 480 / C["q_shift"]
    C["sched_min"] = (C["endTime"] - C["startTime"]).dt.total_seconds() / 60
    C["cycle_differs_from_routing"] = (C["cure_routing_min"] - C["cycleTime"]).abs() > 0.05
    pp, lp = {}, {}
    for r in C.itertuples():
        for d, v in by_day(r.startTime, r.endTime, r.cure_min).items():
            pp[(r.line, r.press, d)] = pp.get((r.line, r.press, d), 0.0) + v
    PD = pd.DataFrame([{"line": k[0], "press": k[1], "plant_day": k[2], "cure_min": v} for k, v in pp.items()])
    PD["available_min"] = [avail({p}, d) for p, d in zip(PD["press"], PD["plant_day"])]
    PD["util_%"] = (100 * PD["cure_min"] / PD["available_min"]).round(1)
    PD["fits"] = PD["cure_min"] <= PD["available_min"] + 0.5
    LD = PD.groupby(["line", "plant_day"]).agg(presses=("press", "nunique"), cure_min=("cure_min", "sum"),
                                               available_min=("available_min", "sum")).reset_index()
    LD["util_%"] = (100 * LD["cure_min"] / LD["available_min"]).round(1)
    LD["fits"] = LD["cure_min"] <= LD["available_min"] + 0.5
    add("C2 curing capacity", "line-day: cure minutes (q_shift rate) <= presses x available", len(LD),
        int(LD["fits"].sum()))
    add("C2 curing capacity", "press-day: cure minutes (q_shift rate) <= available", len(PD), int(PD["fits"].sum()))
    add("C2 curing capacity", "cure block qty <= q_shift (tyres a press cures in a shift)", len(C),
        int((~C["over_q_shift"]).sum()))
    add("C2 curing capacity", "cure block: schedule cycleTime = routing cure time", int(C["cure_routing_min"].notna().sum()),
        int((~C["cycle_differs_from_routing"] & C["cure_routing_min"].notna()).sum()), "info")
    PS = C.sort_values(["press", "startTime"]).copy()
    PS["prev_end"] = PS.groupby("press")["endTime"].shift()
    ov = PS[PS["prev_end"] > PS["startTime"] + pd.Timedelta(seconds=30)]
    add("C2 curing capacity", "press blocks do not overlap", len(PS), len(PS) - len(ov))

    # TBM use per plant day inside the plan window (0 % rows included)
    _end = os.environ.get("CTP_PLAN_END")
    d0 = (B["start_time"].min() - DAY).normalize()
    d1 = (pd.Timestamp(_end) - DAY).normalize() if _end else (B["end_time"].max() - DAY).normalize()
    use = []
    for (ln, m), g in B.groupby(["line", "machine"]):
        for d in pd.date_range(d0, d1 - pd.Timedelta(days=1) if _end else d1, freq="D"):
            a_, b_ = d + DAY, d + DAY + pd.Timedelta(days=1)
            busy = sum(max(0.0, (min(e, b_) - max(s_, a_)).total_seconds() / 60) for s_, e in zip(g["start_time"], g["end_time"]))
            use.append({"line": ln, "machine": m, "plant_day": d, "busy_min": round(busy, 1),
                        "use_%": round(100 * busy / 1440, 1)})
    TU = pd.DataFrame(use)
    print(f"TBM use per plant day: mean {TU['use_%'].mean():.1f}% | idle days {int((TU['use_%'] < 0.5).sum())} | "
          f"<50% {int((TU['use_%'] < 50).sum())} of {len(TU)} | by line {TU.groupby('line')['use_%'].mean().round(1).to_dict()}")
    R = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(R.to_string(index=False))
    if out:
        bc = ["line", "date", "shift", "machine", "mcode", "item", "sku", "routed_gt", "allowed_machines",
              "start_time", "end_time", "qty", "lot_id"]
        cc = ["line", "skuCode", "press", "allowed_presses", "shift", "startTime", "endTime", "qty", "cycleTime",
              "cure_routing_min", "lot_id"]
        with pd.ExcelWriter(out) as xw:
            R.to_excel(xw, sheet_name="Summary", index=False)
            pd.DataFrame({"master": ["different-size changeover min"] * 3 + ["cure allowance min"] * 2 + ["cavities"],
                          "key": ["TBR Building", "PCR Building BJ", "PCR Building CONTI", "PCR", "TBR", "all"],
                          "value": [DS["TBR"], DS["PCR_BJ"], DS["PCR_CONTI"], ALW["PCR"], ALW["TBR"], CAV]}
                         ).to_excel(xw, sheet_name="Masters used", index=False)
            e1[bc].to_excel(xw, sheet_name="B1 exceptions", index=False)
            BD[~BD["fits"] | ~BD["fits_CONTI"]].to_excel(xw, sheet_name="B2 TBM-day exceptions", index=False)
            COX[["line", "machine", "prev_item", "item", "start_time", "gap_before_min", "required_co_min",
                 "required_co_min_CONTI", "lot_id"]].to_excel(xw, sheet_name="B2 changeover gaps", index=False)
            BD.to_excel(xw, sheet_name="B2 TBM-day all", index=False)
            C[~C["eligible"]][cc].to_excel(xw, sheet_name="C1 exceptions", index=False)
            LD.to_excel(xw, sheet_name="C2 line-day", index=False)
            PD[~PD["fits"]].to_excel(xw, sheet_name="C2 press-day exceptions", index=False)
            ov[["line", "press", "skuCode", "prev_end", "startTime", "endTime", "qty", "lot_id"]].to_excel(
                xw, sheet_name="C2 press overlaps", index=False)
            C[C["cycle_differs_from_routing"]][cc].to_excel(xw, sheet_name="Cure time vs routing", index=False)
            TU.pivot_table(index=["line", "machine"], columns=TU["plant_day"].dt.strftime("%d-%b"),
                           values="use_%").reset_index().to_excel(xw, sheet_name="TBM use by day", index=False)
        print("wrote", out)
    return R, B, BD, C, PD, LD, e1, COX, ov


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], a[a.index("--xlsx") + 1] if "--xlsx" in a else None)
