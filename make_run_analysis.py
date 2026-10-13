#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_run_analysis.py -- one workbook per version (user 2026-10-13, v87):
    DATA_GAPS_DIPS_UTILISATION_<v>.xlsx with sheets
      1 Daily numbers        plan / built / cured per plant day (from DAILY_NUMBERS_<v>.xlsx)
      2 Dip reasons          days a line cures < DIP_PCT % of plan: the root machine behind the late build rows,
                             why (mixing capacity / machine sequencing / no input) and that machine's hours that day
      3 Utilisation section  busy % per section per plant day (+ output qty)
      4 Utilisation machine  busy hours and % per machine per plant day
      5 Data gaps            latest_schedule/DB_DATA_GAPS.md (DB gaps + the assumption each forces) + this run's log counts

    python make_run_analysis.py v87
Busy hours are clipped to each plant day (07:00 -> 07:00); % = busy / 24 h. Changeovers are inside the lot times.
"""
import collections
import os
import re
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
DIP_PCT = 70.0
MIX = {"F270", "K310", "F440", "K270M", "K430-1", "K430-2"}
SECTION = {"CALENDARING": "CALENDERING", "Bios cutter": "BIAS CUTTER", "Chaffer Slitter": "CHAFER SLITTER"}


def plant_days(t0, n):
    return [t0 + pd.Timedelta(days=i) for i in range(n)]


def busy(s, f, d):
    e = d + pd.Timedelta(days=1)
    return ((f.clip(d, e) - s.clip(d, e)).dt.total_seconds() / 3600).clip(lower=0)


def main(v):
    V = os.path.join(ROOT, "latest_schedule", v)
    R = os.path.join(V, "run_output")
    X = pd.read_excel(os.path.join(V, f"DAILY_NUMBERS_{v}.xlsx"), index_col=0)
    days = [d for d in X.index if d != "TOTAL"]
    t0 = pd.Timestamp(f"{days[0]}-2026 07:00")
    D = plant_days(t0, len(days))

    S = pd.read_csv(os.path.join(R, "phase5_schedule_updated.csv"), low_memory=False).drop_duplicates("lot_id")
    S["s"] = pd.to_datetime(S["scheduled_start"]); S["f"] = pd.to_datetime(S["scheduled_finish"])
    S["machine"] = S["machine"].astype(str)
    S["section"] = S["department"].astype(str).replace(SECTION).str.upper()
    C = pd.read_csv(os.path.join(R, "phase6_curing_revision_updated.csv"), low_memory=False)
    C["s"] = pd.to_datetime(C["startTime"]); C["f"] = pd.to_datetime(C["endTime"])
    C = C.assign(machine=C["pressNo"].astype(str), section="CURING " + C["plan_id"].astype(str).str.replace("CTP_", ""),
                 qty=pd.to_numeric(C["qty"], errors="coerce"))
    F = pd.read_csv(os.path.join(R, "phase5_floor_schedule_updated.csv"), low_memory=False)
    F["s"] = pd.to_datetime(F["start_time"]); F["f"] = pd.to_datetime(F["end_time"])

    # ---------------- utilisation per machine / day
    rows = []
    for name, frame in (("lots", S), ("press", C)):
        for d in D:
            h = busy(frame["s"], frame["f"], d)
            g = frame.assign(h=h)[h > 0].groupby(["section", "machine"])["h"].sum()
            for (sec, m), x in g.items():
                rows.append((sec, m, d.strftime("%d-%b"), round(x, 2)))
    U = pd.DataFrame(rows, columns=["section", "machine", "day", "busy_h"])
    # a machine used by two sections (K310 master + final) is listed under each, with its hours per section
    UM = U.pivot_table(index=["section", "machine"], columns="day", values="busy_h", aggfunc="sum", fill_value=0)
    UM = UM[[d.strftime("%d-%b") for d in D if d.strftime("%d-%b") in UM.columns]]
    UM["total_h"] = UM.sum(axis=1)
    UM["avg_util_%"] = (UM["total_h"] / (24 * len(D)) * 100).round(1)
    UM = UM.reset_index()

    # output per section per day (floor rows clipped pro rata)
    F["section"] = F["department"].astype(str).replace(SECTION).str.upper()
    F["dur"] = (F["f"] - F["s"]).dt.total_seconds() / 3600
    F["q"] = pd.to_numeric(F.get("produce_qty"), errors="coerce").fillna(0)
    out = []
    for d in D:
        h = busy(F["s"], F["f"], d)
        q = np.where(F["dur"] > 0, F["q"] * h / F["dur"].replace(0, np.nan), 0)
        g = F.assign(q2=q).groupby("section")["q2"].sum()
        for sec, x in g.items():
            out.append((sec, d.strftime("%d-%b"), x))
    for d in D:
        h = busy(C["s"], C["f"], d)
        dur = (C["f"] - C["s"]).dt.total_seconds() / 3600
        g = C.assign(q2=C["qty"] * h / dur.replace(0, np.nan)).groupby("section")["q2"].sum()
        for sec, x in g.items():
            out.append((sec, d.strftime("%d-%b"), x))
    O = pd.DataFrame(out, columns=["section", "day", "qty"]).pivot_table(index="section", columns="day", values="qty",
                                                                        aggfunc="sum", fill_value=0).round(0)
    sec_n = UM.groupby("section")["machine"].nunique()
    SU = U.groupby(["section", "day"])["busy_h"].sum().unstack(fill_value=0)
    SU = SU[[c for c in UM.columns if re.match(r"\d\d-\w\w\w$", str(c))]]
    SU = (SU.div(sec_n * 24, axis=0) * 100).round(1)
    SU.insert(0, "machines", sec_n)
    SU["avg_util_%"] = SU.drop(columns="machines").mean(axis=1).round(1)
    SU = SU.sort_values("avg_util_%", ascending=False)
    unit = F.groupby("section")["UOM"].agg(lambda s: s.dropna().astype(str).mode().iat[0] if s.notna().any() else "") \
        if "UOM" in F.columns else pd.Series(dtype=str)
    O.insert(0, "unit", O.index.map(lambda s: unit.get(s, "tyres" if s.startswith(("CURING", "BUILDING")) else "")))

    # ---------------- dip reasons (root machine behind late build rows; logic of the 13-Oct dip analysis)
    P = os.path.join(R, "PHASEWISE")
    L = pd.read_csv(os.path.join(P, "PHASE2_LOTS", "phase2_lots_updated.csv"), low_memory=False).set_index("lot_id")
    mn = (L["min_aging_h"].fillna(0) + L["transfer_time_h"].fillna(0)).to_dict()
    mx = L["max_aging_h"].to_dict()
    SI = S.set_index("lot_id")
    E = pd.read_csv(os.path.join(P, "PHASE3_DAG", "phase3_dag_edges_updated.csv"), low_memory=False)
    E = E[(E["consume_qty"] > 0) & E["producer_lot"].isin(SI.index) & E["consumer_lot"].isin(SI.index)]
    E["rdy"] = E["producer_lot"].map(SI["f"]) + pd.to_timedelta(E["producer_lot"].map(mn).fillna(0), unit="h")
    latest = E.sort_values("rdy").groupby("consumer_lot").tail(1).set_index("consumer_lot")

    def root(l, depth=0):
        m = SI["machine"].get(l, "?")
        if m in MIX or depth > 8:
            return m, "mixing capacity (compound made as fast as the mixer allows)" if m in MIX else "deep chain"
        if l not in latest.index:
            return m, "machine capacity / sequencing (no late input)"
        inp = latest.loc[l]
        wait = (SI["s"][l] - inp["rdy"]).total_seconds() / 3600
        if wait > 2 and (mx.get(l) or 999) > 24:
            return m, "machine sequencing (lot waited after its input was ready)"
        return root(inp["producer_lot"], depth + 1)

    B = pd.read_csv(os.path.join(P, "00_INPUTS", "bom.csv"), low_memory=False,
                    usecols=["Super_parent", "child", "child_description"])
    g = B[B["child_description"] == "Green Tyres"].drop_duplicates("child")
    gl = dict(zip(g["child"], np.where(g["Super_parent"].astype(str).str.startswith("11"), "TBR", "PCR")))
    b = F[F["department"] == "BUILDING"].copy()
    b["line"] = b["item"].map(gl)
    b["ins"] = b["consumption_id"].fillna("").astype(str).str.split(";")
    ex = b[["prod_id", "s", "produce_qty", "line", "ins"]].explode("ins")
    ex = ex[ex["ins"].isin(SI.index)]
    ex["rdy"] = ex["ins"].map(SI["f"]) + pd.to_timedelta(ex["ins"].map(mn).fillna(0), unit="h")
    mday = {(m, dd): h for (sec, m, dd), h in U.groupby(["section", "machine", "day"])["busy_h"].sum().items()}
    mday2 = collections.defaultdict(float)
    for (m, dd), h in mday.items():
        mday2[(m, dd)] += h
    res = []
    for ln in ("PCR", "TBR"):
        pct = X.loc[days, f"cured {ln}"] / X.loc[days, f"cure plan {ln}"] * 100
        for dname, p in pct.items():
            if p >= DIP_PCT:
                continue
            day = D[days.index(dname)]
            w = ex[(ex["line"] == ln) & (ex["s"] >= day - pd.Timedelta(days=1)) & (ex["s"] < day + pd.Timedelta(days=1))]
            q = w.drop_duplicates("prod_id").set_index("prod_id")["produce_qty"]
            lastin = w.sort_values("rdy").groupby("prod_id").tail(1)
            cnt = collections.Counter()
            for pid, l in zip(lastin["prod_id"], lastin["ins"]):
                cnt[root(l)] += float(q.get(pid, 0))
            tot = sum(cnt.values()) or 1
            dd = day.strftime("%d-%b"); dprev = (day - pd.Timedelta(days=1)).strftime("%d-%b")
            for (m, why), val in cnt.most_common(4):
                res.append({"line": ln, "dip_day": dname, "cure_plan": int(X.loc[dname, f"cure plan {ln}"]),
                            "cured": int(X.loc[dname, f"cured {ln}"]), "cured_%": round(p, 1),
                            "root_machine": m, "reason": why, "tyres_built_behind_it": int(val),
                            "share_%": round(100 * val / tot), "machine_busy_h_day_before": round(mday2.get((m, dprev), 0), 1),
                            "machine_busy_h_dip_day": round(mday2.get((m, dd), 0), 1)})
    DR = pd.DataFrame(res)
    if not DR.empty:                                   # name the cause by how full the root machine was (>= 20 h = full)
        full = DR[["machine_busy_h_day_before", "machine_busy_h_dip_day"]].max(axis=1) >= 20
        mixr = DR["root_machine"].isin(MIX)
        DR["reason"] = np.select(
            [mixr & full, mixr & ~full, ~mixr & full, DR["reason"].str.startswith("machine sequencing")],
            ["MIXING CAPACITY: mixer full, compound made as fast as it allows",
             "COMPOUND LATE, mixer not full: start-up / waiting for its master or raw input / lot order",
             "MACHINE CAPACITY: machine full, its lots queue",
             "SEQUENCING: machine not full, lot waited after its input was ready (rule order / grouping)"],
            "MACHINE NOT FULL, no late input: lot order / changeover grouping")
    if DR.empty:
        DR = pd.DataFrame([{"line": "-", "dip_day": f"no day below {DIP_PCT:g} % of cure plan"}])

    # ---------------- data gaps: DB_DATA_GAPS.md tables + this run's log counts
    gaps = []
    md = os.path.join(ROOT, "latest_schedule", "DB_DATA_GAPS.md")
    sec = ""
    if os.path.exists(md):
        for ln_ in open(md, encoding="utf-8"):
            if ln_.startswith("## "):
                sec = ln_[3:].strip()
            elif ln_.startswith("|") and not re.match(r"^\|\s*-", ln_) and "Gap |" not in ln_:
                c = [x.strip().replace("**", "") for x in ln_.strip().strip("|").split("|")]
                if len(c) >= 3:
                    gaps.append({"area": sec, "gap": c[0], "evidence": c[1], "assumption it forces / status": c[2],
                                 "source": "DB_DATA_GAPS.md"})
    log = os.path.join(V, "run_log.txt")
    pats = [("WIP", r"WIP SOURCE:.*|WIP CORRECTION.*|BATCHES -> KG.*"),
            ("Mixing master", r"\[approx\] masters.*"),
            ("Routing", r"gap-filled.*|no make rate.*"),
            ("Changeover", r"missing.*pairs.*|no matrix.*"),
            ("BOM", r".*rows skipped.*|.*no BOM.*"),
            ("Integrity", r".*MUST be 0.*|.*CURED WITHOUT A GREEN TYRE.*|.*green tyres scrapped.*")]
    if os.path.exists(log):
        txt = open(log, encoding="utf-8", errors="replace").read().splitlines()
        seen = set()
        for area, pat in pats:
            for t in txt:
                t2 = t.strip().lstrip("!# ").strip()
                if re.search(pat, t2, re.I) and "password" not in t2.lower() and t2 not in seen and len(seen) < 400:
                    seen.add(t2)
                    gaps.append({"area": f"{area} (this run's log)", "gap": t2[:300], "evidence": "run_log.txt",
                                 "assumption it forces / status": "", "source": v})
    chk = os.path.join(V, f"DB_MASTER_CHECKS_{v}.md")
    if os.path.exists(chk):
        for t in open(chk, encoding="utf-8"):
            if t.startswith("|") and re.search(r"\d", t) and not re.match(r"^\|\s*-", t):
                c = [x.strip().replace("**", "") for x in t.strip().strip("|").split("|")]
                gaps.append({"area": "Plan vs DB building / curing masters", "gap": " | ".join(c), "evidence": f"DB_MASTER_CHECKS_{v}.md",
                             "assumption it forces / status": "plan machine kept (user)", "source": v})
    G = pd.DataFrame(gaps)

    path = os.path.join(V, f"DATA_GAPS_DIPS_UTILISATION_{v}.xlsx")
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        X.to_excel(xw, sheet_name="Daily numbers")
        DR.to_excel(xw, sheet_name="Dip reasons", index=False)
        SU.to_excel(xw, sheet_name="Utilisation section")
        O.to_excel(xw, sheet_name="Output section")
        UM.to_excel(xw, sheet_name="Utilisation machine", index=False)
        G.to_excel(xw, sheet_name="Data gaps", index=False)
    from openpyxl import load_workbook
    from openpyxl.styles import Font
    wb = load_workbook(path)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                c.font = Font(name="Arial", size=10, bold=c.row == 1)
        for col in ws.columns:
            w = max(len(str(c.value or "")) for c in col[:200])
            ws.column_dimensions[col[0].column_letter].width = min(max(9, w + 2), 70)
        ws.freeze_panes = "B2"
    wb.save(path)
    print(f"  wrote {path}")
    print(f"  dip rows {len(DR)}; sections {len(SU)}; machines {len(UM)}; data-gap rows {len(G)}")
    print(SU.to_string())
    print(DR.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "v87"))
