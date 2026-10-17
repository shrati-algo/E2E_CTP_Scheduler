#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fourrc_sequence_report.py -- 4-roll calender (901) utilisation, idle time and same-wire sequencing breaks.

    python fourrc_sequence_report.py v48   -> latest_schedule/v48/FOURRC_SEQUENCE_REPORT_v48.xlsx

Sheets
  Summary                 901 running / idle hours in the plan window, idle hours by cause, wire loads, breaks
  Idle gaps               every idle gap on 901 with its cause and the lots that were ready at that moment
  Wire sequence breaks    every LATER creel load of a steel wire already loaded in the window (same-wire compounds
                          not run together) with the reason per lot: its compound was not ready when the wire was
                          loaded before / it would be over-age at its last use if made then / order (other)
  901 lots                every 901 lot: line, class, FRC wire, need, input ready, compound it waited for, lateness
Input ready = latest finish + min aging + transfer of the lots it consumed (strict consumption links of the floor
schedule; opening stock / raw material = ready at plan start).
"""
import os
import sys

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
CREEL_MIN = 240


def main(v):
    vdir = os.path.join(HERE, "latest_schedule", v)
    R = os.path.join(vdir, "run_output")
    ST = pd.Timestamp(os.environ.get("CTP_PLAN_START") or "2026-10-06 07:00")
    END = pd.Timestamp(os.environ.get("CTP_PLAN_END") or "2026-10-16 07:00")
    s = pd.read_csv(os.path.join(R, "phase5_schedule_updated.csv"), parse_dates=["scheduled_start", "scheduled_finish"])
    L = pd.read_csv(os.path.join(R, "phase2_lots_updated.csv"), low_memory=False, parse_dates=["first_need", "last_need"])
    f = pd.read_csv(os.path.join(R, "phase5_floor_schedule_updated.csv"), low_memory=False)
    fp = os.path.join(R, "PHASEWISE", "00_INPUTS", "frc_lengths.csv")
    fl = pd.read_csv(fp if os.path.exists(fp) else os.path.join(HERE, "inputs", "frc_lengths.csv"))
    wire = dict(zip(fl["item"].astype(str).str.strip(), fl["wire"].astype(str).str.strip()))
    cls = dict(zip(fl["item"].astype(str).str.strip(), fl["class"].astype(str).str.lower()))
    fin = dict(zip(s["lot_id"], s["scheduled_finish"]))
    it = dict(zip(L["lot_id"], L["item_code"].astype(str).str.strip()))
    mna = dict(zip(L["lot_id"], L["min_aging_h"].fillna(0) + L["transfer_time_h"].fillna(0)))
    fn, ln, mx = (dict(zip(L["lot_id"], L[c])) for c in ("first_need", "last_need", "max_aging_h"))
    f9 = f[f["machine"].astype(str) == "901"]
    cons = f9.groupby("lot_id")["consumption_id"].apply(lambda x: ";".join(x.astype(str)))

    def ready(l):
        r, who = ST, "opening stock / raw material"
        for t in str(cons.get(l, "")).split(";"):
            t = t.strip()
            if not t or t.startswith(("WIP", "OPENING", "RAW", "SHORT")):
                continue
            t = t.split("(")[0].strip()
            if t in fin:
                x = fin[t] + pd.Timedelta(hours=float(mna.get(t, 0) or 0))
                if x > r:
                    r, who = x, it.get(t, t)
        return r, who

    m = s[s["machine"].astype(str) == "901"].sort_values("scheduled_start").reset_index(drop=True)
    m["item"] = m["item"].astype(str).str.strip()
    m["class"] = m["item"].map(cls).fillna("?")
    m["wire"] = m["item"].map(wire).fillna(m["item"])
    m["line"] = m["equipment"].astype(str).str.extract(r"(PCR|TBR)")[0]
    info = {l: ready(l) for l in m["lot_id"]}
    m["input_ready"] = m["lot_id"].map(lambda l: info[l][0])
    m["last_input"] = m["lot_id"].map(lambda l: info[l][1])
    m["first_need"] = m["lot_id"].map(fn)
    m["late_h"] = ((m["scheduled_finish"] - m["first_need"]).dt.total_seconds() / 3600).round(1)
    m["waited_for_input"] = (m["scheduled_start"] - m["input_ready"]).abs() <= pd.Timedelta(minutes=10)
    W = m[m["scheduled_start"] < END].copy()

    # idle gaps
    gaps, prev_f, last_steel = [], ST, None
    for _, r in W.iterrows():
        t0 = max(prev_f, ST)
        gap = (r["scheduled_start"] - t0).total_seconds() / 60
        creel = (last_steel[1] + pd.Timedelta(minutes=CREEL_MIN)
                 if (r["class"] == "steel" and last_steel and last_steel[0] != r["wire"]) else None)
        if gap > 1:
            tol = pd.Timedelta(minutes=10)
            pend = W[W["scheduled_start"] >= r["scheduled_start"]]
            rdy = sorted({x for l2, x in zip(pend["lot_id"], pend["item"]) if info[l2][0] <= t0})
            if abs(r["scheduled_start"] - info[r["lot_id"]][0]) <= tol and info[r["lot_id"]][0] > t0:
                cause = ("no lot ready: waiting for compound" if not rdy else
                         "waiting for compound while other lots were ready (sequence kept)")
            elif creel is not None and abs(r["scheduled_start"] - creel) <= tol:
                cause = "creel reload 240 min (new steel wire)" + ("" if not rdy else ", no fabric used to fill it")
            elif gap <= 95:
                cause = "changeover (CTP_Master_ChangeoverMatrix)"
            else:
                cause = "held although inputs ready (rule: one go a day / spool / creel / repair order)"
            gaps.append({"gap_start": t0, "gap_end": r["scheduled_start"], "idle_h": round(gap / 60, 2), "cause": cause,
                         "next_lot": r["lot_id"], "next_item": r["item"], "line": r["line"], "class": r["class"],
                         "next_waits_for": info[r["lot_id"]][1] if cause.startswith(("no lot", "waiting")) else "",
                         "lots_ready_then": len(rdy), "items_ready_then": ", ".join(rdy)[:200]})
        prev_f = max(prev_f, r["scheduled_finish"])
        if r["class"] == "steel":
            last_steel = (r["wire"], r["scheduled_finish"])
    G = pd.DataFrame(gaps)
    run_h = sum((min(b, END) - a).total_seconds() for a, b in zip(W["scheduled_start"], W["scheduled_finish"])) / 3600
    win_h = (END - ST).total_seconds() / 3600

    # steel creel loads and breaks
    st = W[W["class"] == "steel"].copy()
    st["load"] = (st["wire"] != st["wire"].shift()).cumsum()
    loads = st.groupby("load").agg(wire=("wire", "first"), start=("scheduled_start", "min"), end=("scheduled_finish", "max"),
                                   items=("item", lambda x: ", ".join(dict.fromkeys(x))), lots=("lot_id", list)).reset_index()
    br = []
    for w_, g in loads.groupby("wire"):
        g = g.sort_values("start")
        for (_, a), (_, b) in zip(g.iterrows(), g.iloc[1:].iterrows()):
            for l in b["lots"]:
                rd, who = info[l]
                lastn, mxa = ln.get(l), mx.get(l)
                if rd > a["end"]:
                    why = f"compound {who} not ready when wire {w_} was loaded before (ready {rd:%d-%b %H:%M}, load ended {a['end']:%d-%b %H:%M})"
                    k = "compound not ready"
                elif pd.notna(lastn) and pd.notna(mxa) and (lastn - a["end"]) > pd.Timedelta(hours=float(mxa)):
                    why = f"made with the earlier load it would be over-age at its last use ({lastn:%d-%b %H:%M}, max aging {mxa:g} h)"
                    k = "shelf life (not needed yet)"
                else:
                    why = "inputs ready and within shelf life, but not run in the earlier load (one go a day / spool / order)"
                    k = "order / other rule"
                br.append({"wire": w_, "earlier_load": f"{a['start']:%d-%b %H:%M}-{a['end']:%H:%M} ({a['items']})",
                           "this_load_start": b["start"], "lot": l, "item": it.get(l, ""), "reason_type": k, "reason": why})
    B = pd.DataFrame(br)
    by_cause = (G.groupby("cause")["idle_h"].agg(["count", "sum"]).reset_index().rename(
        columns={"count": "gaps", "sum": "idle_h"}) if len(G) else pd.DataFrame(columns=["cause", "gaps", "idle_h"]))
    summ = [("plan window (h)", round(win_h, 1)), ("901 running (h)", round(run_h, 1)),
            ("901 utilisation %", round(100 * run_h / win_h, 1)), ("901 idle (h)", round(win_h - run_h, 1)),
            ("steel creel loads", len(loads)), ("steel wires", int(loads["wire"].nunique()) if len(loads) else 0),
            ("wires loaded more than once", int((loads.groupby("wire").size() > 1).sum()) if len(loads) else 0),
            ("same-wire lots NOT run with the earlier load of their wire (breaks)", len(B))]
    if len(B):
        for k, n in B["reason_type"].value_counts().items():
            summ.append((f"  breaks: {k}", int(n)))
    S = pd.DataFrame(summ, columns=["measure", "value"])
    lots_tab = W[["lot_id", "item", "line", "class", "wire", "scheduled_start", "scheduled_finish", "first_need", "late_h",
                  "input_ready", "last_input", "waited_for_input"]]
    out = os.path.join(vdir, f"FOURRC_SEQUENCE_REPORT_{v}.xlsx")
    hdr, bold = PatternFill("solid", fgColor="FF1F3864"), Font(name="Arial", bold=True, color="FFFFFFFF")
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in [("Summary", S), ("Idle by cause", by_cause), ("Idle gaps", G), ("Wire sequence breaks", B),
                         ("901 lots", lots_tab)]:
            df = df if len(df) else pd.DataFrame([{"result": "none"}])
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for c in ws[1]:
                c.fill, c.font, c.alignment = hdr, bold, Alignment(wrap_text=True, vertical="center")
            for j, c in enumerate(df.columns, 1):
                w = min(max(10, int(df[c].astype(str).str.len().head(300).max()) + 2, len(str(c)) + 2), 90)
                ws.column_dimensions[get_column_letter(j)].width = w
                if pd.api.types.is_datetime64_any_dtype(df[c]):
                    for r in range(2, len(df) + 2):
                        ws.cell(row=r, column=j).number_format = "dd-mmm-yy hh:mm"
            ws.auto_filter.ref = ws.dimensions
            ws.freeze_panes = "A2"
    print(S.to_string(index=False))
    print(by_cause.to_string(index=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "v48")
