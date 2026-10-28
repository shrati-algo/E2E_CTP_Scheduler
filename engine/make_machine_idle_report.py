#!/usr/bin/env python3
"""make_machine_idle_report.py - why is a machine idle? Minute by minute, per shift.

    python make_machine_idle_report.py --run e2e_v46k --machines 1001,1801

For each machine every minute of the horizon (plan start 07:00 onward) is one of
  RUN                      a lot is running
  CHANGEOVER               the setup before a lot
  WAIT: compound           the next lot's inputs are not made + rested yet
                           (input lot finish + its min ageing + transfer)
  WAIT: BR-14 blackout     inputs ready, but a changeover may not start within
                           changeover.blackout_min of a shift change
  WAIT: ready, not started inputs ready and no rule blocks it - the dispatcher
                           had not released / chosen the lot
  NO WORK                  nothing left on this machine
Inputs come from phase4_edges (producer lot -> this lot), producer times from
phase5_schedule of the same line, min ageing from phase1_demand.

Writes outputs/<run>/MACHINE_IDLE_<machines>_<run>.xlsx:
  Shifts   hours per cause per shift (A 07-15, B 15-23, C 23-07)
  Gaps     every idle gap, its cause, the next lot, the input that held it
           (compound, mixer, mix start / finish, ready time)
  Lots     every lot on the machine with its input-ready time
  Inputs   for the lots the machine waited for: what the mixer did before
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import ctpio as io
import rules as R


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--machines", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    want = [m.strip() for m in a.machines.split(",") if m.strip()]
    bl = pd.Timedelta(minutes=float((R.cfg().get("changeover") or {}).get("blackout_min", 0) or 0))
    tr = pd.Timedelta(minutes=io.TRANSFER_MIN)

    S, E, MA = {}, {}, {}
    for ln in ("pcr", "tbr"):
        s = pd.read_csv(D / ln / "phase5_schedule.csv", parse_dates=["start", "finish", "LFT"])
        s["co"] = pd.to_numeric(s["changeover_min"], errors="coerce").fillna(0.0)
        s["occ"] = s["start"] - pd.to_timedelta(s["co"], unit="m")
        s["line"] = ln.upper()
        S[ln] = s
        E[ln] = pd.read_csv(D / ln / "phase4_edges.csv")
        d = pd.read_csv(D / ln / "phase1_demand.csv")
        MA[ln] = d.groupby("item_code")["min_aging_h"].max().to_dict()
    ALL = pd.concat(S.values(), ignore_index=True)
    b = pd.read_csv(D / "pcr" / "phase5_building_plan.csv", parse_dates=["build_start"])
    t0 = b["build_start"].min().normalize() + pd.Timedelta(hours=io.SHIFT_START_H)
    t_end = ALL["finish"].max().ceil("D") + pd.Timedelta(hours=io.SHIFT_START_H)

    def shift_of(t):
        h = (t - pd.Timedelta(hours=io.SHIFT_START_H)).hour
        return "A" if h < 8 else ("B" if h < 16 else "C")

    lots_out, gaps_out, inputs_out, minutes = [], [], [], []
    for ln, s in S.items():
        idx = s.drop_duplicates("lot_id").set_index("lot_id")
        for m in want:
            g = s[s["machine"].astype(str) == m].sort_values("occ")
            if g.empty:
                continue
            name = g["machine_name"].iloc[0] if "machine_name" in g.columns else ""
            # input-ready time of every lot
            rec = []
            for r in g.itertuples(index=False):
                prs = [p for p in E[ln].loc[E[ln]["consumer_lot"] == r.lot_id, "producer_lot"]
                       if p in idx.index]
                best, key = None, None
                for p in prs:
                    x = idx.loc[p]
                    rt = x["finish"] + pd.Timedelta(hours=float(MA[ln].get(x["item_code"], 0) or 0)) + tr
                    if best is None or rt > best:
                        best, key = rt, p
                rec.append({"lot": r.lot_id, "item": r.item_code, "LFT": r.LFT,
                            "occ": r.occ, "start": r.start, "finish": r.finish,
                            "co_min": r.co, "run_h": r.work_h, "qty": r.lot_qty,
                            "inputs_ready": best if best is not None else t0,
                            "input_lot": key or "", "input_item": idx.loc[key, "item_code"] if key else "stock",
                            "input_machine": str(idx.loc[key, "machine"]) if key else "",
                            "input_start": idx.loc[key, "start"] if key else pd.NaT,
                            "input_finish": idx.loc[key, "finish"] if key else pd.NaT})
            L = pd.DataFrame(rec)
            L["machine"], L["line"], L["machine_name"] = m, ln.upper(), name
            lots_out.append(L)

            # walk the timeline: gaps between lots, split by cause
            free = t0
            for i, r in enumerate(L.itertuples(index=False)):
                if r.occ > free:
                    gs, ge = free, r.occ
                    rdy = max(r.inputs_ready, t0)
                    parts = []
                    if rdy >= ge - pd.Timedelta(minutes=1):
                        parts.append((gs, ge, "WAIT: compound"))
                    else:
                        if rdy > gs:
                            parts.append((gs, rdy, "WAIT: compound"))
                        x0 = max(gs, rdy)
                        # ends right after a shift change's blackout -> BR-14
                        a_ = ge.floor("h")
                        in_bl = (r.co_min > 0 and bl > pd.Timedelta(0) and
                                 any(abs((ge - (ge.normalize() + pd.Timedelta(hours=h) + bl)).total_seconds()) < 90
                                     for h in (io.SHIFT_START_H, io.SHIFT_START_H + 8, io.SHIFT_START_H + 16)))
                        parts.append((x0, ge, "WAIT: BR-14 blackout" if in_bl else "WAIT: ready, not started"))
                    for p0, p1, why in parts:
                        if p1 <= p0:
                            continue
                        gaps_out.append({"line": ln.upper(), "machine": m, "gap_from": p0, "gap_to": p1,
                                         "hours": round((p1 - p0).total_seconds() / 3600, 2),
                                         "shift": shift_of(p0), "cause": why,
                                         "next_lot": r.lot, "next_item": r.item, "next_LFT": r.LFT,
                                         "overdue_at_gap_h": round(max(0.0, (p0 - r.LFT).total_seconds() / 3600), 1),
                                         "held_by_input": r.input_item if why == "WAIT: compound" else "",
                                         "input_machine": r.input_machine if why == "WAIT: compound" else "",
                                         "input_mixed": (f"{r.input_start:%d %H:%M}-{r.input_finish:%H:%M}"
                                                         if why == "WAIT: compound" and pd.notna(r.input_start) else ""),
                                         "inputs_ready": r.inputs_ready})
                        minutes.append((m, ln.upper(), p0, p1, why))
                if r.co_min > 0:
                    minutes.append((m, ln.upper(), r.occ, r.start, "CHANGEOVER"))
                minutes.append((m, ln.upper(), r.start, r.finish, "RUN"))
                free = max(free, r.finish)
            if free < t_end:
                minutes.append((m, ln.upper(), free, t_end, "NO WORK"))

            # what the input machines did before the lots this machine waited for
            for k in L.loc[L["input_lot"] != "", "input_lot"].unique():
                x = idx.loc[k]
                before = ALL[(ALL["machine"].astype(str) == str(x["machine"])) & (ALL["start"] < x["start"])
                             & (ALL["start"] >= t0)].sort_values("start")
                for q in before.itertuples(index=False):
                    inputs_out.append({"for_machine": m, "input_lot": k, "input_item": x["item_code"],
                                       "input_LFT": x["LFT"], "mixer": x["machine"],
                                       "mixer_ran_before": q.item_code, "line": q.line,
                                       "start": q.start, "finish": q.finish, "its_LFT": q.LFT,
                                       "needed_later_than_waiting_lot": q.LFT > x["LFT"]})

    # ---- per shift --------------------------------------------------------
    rows = []
    for m, ln, p0, p1, why in minutes:
        t = p0
        while t < p1:
            day = (t - pd.Timedelta(hours=io.SHIFT_START_H)).normalize()
            sh = shift_of(t)
            k = {"A": 0, "B": 8, "C": 16}[sh]
            s_end = day + pd.Timedelta(hours=io.SHIFT_START_H + k + 8)
            e = min(p1, s_end)
            rows.append({"machine": m, "line": ln, "plant_day": day.date(), "shift": sh,
                         "cause": why, "hours": (e - t).total_seconds() / 3600})
            t = e
    M = pd.DataFrame(rows)
    SH = (M.pivot_table(index=["machine", "line", "plant_day", "shift"], columns="cause",
                        values="hours", aggfunc="sum").fillna(0).round(2).reset_index())
    G, Lt, I = pd.DataFrame(gaps_out), pd.concat(lots_out, ignore_index=True), pd.DataFrame(inputs_out).drop_duplicates()
    out = D / f"MACHINE_IDLE_{'_'.join(want)}_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Shifts", SH), ("Gaps", G), ("Lots", Lt), ("Inputs", I)):
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 40)
            ws.freeze_panes = "A2"
    print(SH.to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
