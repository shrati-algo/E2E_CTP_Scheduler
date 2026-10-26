#!/usr/bin/env python3
"""make_dispatch_log_workbook.py - turn DISPATCH_LOG_debug.csv into a readable workbook.

    python make_dispatch_log_workbook.py --run e2e_v44b_dbg

Input : outputs/<run>/DISPATCH_LOG_debug.csv (written by phase5 when
        `debug.log_machines` is set): one row per QUEUED lot at every pick on
        the logged machines.
Output: outputs/<run>/DISPATCH_LOG_2403_2405.xlsx with sheets
        Read me, Summary, Decisions, Losses (a ready lot for an earlier trolley
        that was NOT picked), Candidates (every queued lot at every decision).
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import ctpio as io


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    c = pd.read_csv(D / "DISPATCH_LOG_debug.csv",
                    parse_dates=["time", "trolley_need", "LFT", "ready_at"])
    c["ready_now"] = c["ready_now"].astype(str).str.lower() == "true"
    c["picked"] = c["picked"].astype(str).str.lower() == "true"
    c["removed_by_rule"] = c["removed_by_rule"].fillna("")
    c["stage"] = c["department"].map(
        lambda d: "apexing" if "APEX" in str(d).upper() else
        ("winding" if "WINDING" in str(d).upper() else str(d)))

    rows, losses = [], []
    for pid, g in c.groupby("decision", sort=True):
        pk = g[g["picked"]]
        if pk.empty:
            continue
        p = pk.iloc[0]
        rdy = g[g["ready_now"]]
        elig = rdy[rdy["removed_by_rule"] == ""]
        best = elig.sort_values(["key0", "key1", "key2"]).iloc[0] if len(elig) else p
        earlier = rdy[(rdy["trolley_need"] < p["trolley_need"]) & ~rdy["picked"]]
        if p["campaign_hold"] in (True, "True") and best["candidate_lot"] != p["candidate_lot"]:
            why = "campaign hold kept the item already on the machine"
        elif best["candidate_lot"] != p["candidate_lot"]:
            why = "ranking tie-break (related / belt / wire preference) beat the queue key"
        else:
            why = "best queue key among ready lots"
        rows.append({
            "decision": pid, "time": p["time"], "machine": p["machine"],
            "prev_item": p["prev_item"], "picked_lot": p["candidate_lot"],
            "picked_stage": p["stage"], "picked_trolley_need": p["trolley_need"],
            "picked_key": f"({p['key0']}, {p['key1']}, {p['key2']})",
            "queued": len(g), "ready": len(rdy),
            "ready_removed_by_rules": int((rdy["removed_by_rule"] != "").sum()),
            "best_ready_lot_by_key": best["candidate_lot"],
            "best_ready_trolley_need": best["trolley_need"],
            "ready_lots_for_earlier_trolleys": len(earlier),
            "why_this_pick": why})
        for r in earlier.itertuples(index=False):
            if r.removed_by_rule:
                reason = f"removed by rule: {r.removed_by_rule}"
            elif p["campaign_hold"] in (True, "True"):
                reason = "campaign hold on the item already mounted"
            else:
                reason = "ranking tie-break before the queue key"
            losses.append({
                "decision": pid, "time": p["time"], "machine": p["machine"],
                "waiting_lot": r.candidate_lot, "waiting_stage": r.stage,
                "waiting_trolley_need": r.trolley_need, "waiting_key": f"({r.key0}, {r.key1}, {r.key2})",
                "picked_lot": p["candidate_lot"], "picked_stage": p["stage"],
                "picked_trolley_need": p["trolley_need"],
                "need_gap_h": round((p["trolley_need"] - r.trolley_need).total_seconds() / 3600, 1),
                "reason": reason})
    dec = pd.DataFrame(rows)
    los = pd.DataFrame(losses)
    # when did the picked lot actually START? (booked ahead = start - decision time)
    _st = []
    for ln in ("pcr", "tbr"):
        _f = D / ln / "phase5_schedule.csv"
        if _f.exists():
            _st.append(pd.read_csv(_f, usecols=["lot_id", "start"], parse_dates=["start"]))
    if _st and len(dec):
        _sm = dict(pd.concat(_st).values)
        dec["picked_start"] = dec["picked_lot"].map(_sm)
        dec["booked_ahead_h"] = ((dec["picked_start"] - dec["time"]).dt.total_seconds()
                                 / 3600).round(1)

    summ = [("decisions logged", len(dec)),
            ("decisions where the pick was the best ready key", int((dec["why_this_pick"] == "best queue key among ready lots").sum())),
            ("decisions where campaign hold decided", int(dec["why_this_pick"].str.startswith("campaign").sum())),
            ("decisions where a tie-break decided", int(dec["why_this_pick"].str.startswith("ranking").sum())),
            ("decisions with a ready lot for an EARLIER trolley left waiting", int((dec["ready_lots_for_earlier_trolleys"] > 0).sum()))]
    if "booked_ahead_h" in dec:
        summ += [("picks booked more than 1 h ahead (machine still busy at decision)",
                  int((dec["booked_ahead_h"] > 1).sum())),
                 ("median hours booked ahead", float(dec["booked_ahead_h"].median())),
                 ("max hours booked ahead", float(dec["booked_ahead_h"].max()))]
    if len(los):
        for k, v in los["reason"].value_counts().items():
            summ.append((f"waiting-lot rows: {k}", int(v)))
        ap_ = los[(los["waiting_stage"] == "apexing") & (los["picked_stage"] == "winding")]
        summ.append(("rows: READY APEXING lot waited while WINDING was picked", len(ap_)))
    S = pd.DataFrame(summ, columns=["measure", "value"])
    note = pd.DataFrame({"read me": [
        "Every pick on machines 2403/2404/2405 in the last placement round.",
        "Candidates: every lot queued on the machine at that moment, its queue key "
        "(need window, stage to trolley, need time), when it is ready, and which rule removed it.",
        "Losses: a READY lot serving an EARLIER trolley that was not picked, and why.",
        "Decisions.booked_ahead_h: hours between the decision and the picked lot's start. "
        "Large values mean the machine was still busy and its next lot was booked early, "
        "before later-ready lots (e.g. day-1 apexing) could compete.",
        "Queue key on these machines (v44a): (trolley-need window of 8 h, stages to the "
        "trolley [0 = feeds it], trolley need time, own deadline)."]})

    out = D / "DISPATCH_LOG_2403_2405.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Read me", note), ("Summary", S), ("Decisions", dec),
                         ("Losses", los), ("Candidates", c)):
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(10, w + 2), 48)
            ws.freeze_panes = "A2"
    print(S.to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
