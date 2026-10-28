#!/usr/bin/env python3
"""shift_balance_from_floor.py - shift load balance read from the PUBLISHED floor schedule.

    python analysis/shift_balance_from_floor.py --run <run> [--band 0.20]

Reads `phase5_floor_schedule_{pcr,tbr}.csv` of the run (lots split at 07:00 /
15:00 / 23:00, columns lot_qty + shift_part). The "old report" view - the whole
lot booked to the shift it starts in - is rebuilt from the same file (first
piece of each lot, at its lot_qty) so the two can be compared. Shared machines
(mixers, 901, 1102) are summed over both lines. Per machine and shift, from the
plan's first shift to its last:
  qty   produced in the shift, in the machine's unit
  run_h hours of production in the shift
A machine is LEVEL when every working shift (first .. last-but-one shift with
work) is within +-band of its mean, in hours AND in quantity.
Idle causes per shift are joined from analysis/SHIFT_LOAD_BALANCE_<run>.shift_detail.csv
(made by shift_load_balance.py on the same run - run that first).

Writes <out>/<run>/analysis/SHIFT_BALANCE_PUBLISHED_<run>.xlsx
"""
from __future__ import annotations

import argparse

import pandas as pd

import _common as CM

SH = pd.Timedelta(hours=8)
T0, NSH, LAB = None, 0, []      # set from the run's own plan in main()


def load(D, whole_lot=False):
    f = []
    for ln in ("pcr", "tbr"):
        p = D / f"phase5_floor_schedule_{ln}.csv"
        if not p.exists():
            continue
        d = pd.read_csv(p, encoding="utf-8-sig", parse_dates=["start_time", "end_time"])
        d["line"] = ln.upper()
        f.append(d)
    d = pd.concat(f, ignore_index=True)
    if whole_lot and {"lot_qty", "shift_part"} <= set(d.columns):
        d = d[d["shift_part"].astype(str).str.startswith("1/")].copy()
        d["produce_qty"] = d["lot_qty"]
    d["machine"] = d["machine"].astype(str)
    d["k"] = ((d["start_time"] - T0) / SH).astype(int)
    d["h"] = (d["end_time"] - d["start_time"]).dt.total_seconds() / 3600
    return d[(d["k"] >= 0) & (d["k"] < NSH)]


def group_of(dept):
    return {"BUILDING": "BUILDING", "CURING": "CURING"}.get(str(dept), "UPSTREAM")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--band", type=float, default=0.20)
    a = ap.parse_args()
    global T0, NSH, LAB
    D = CM.run_dir(a.run)
    T0, t_end = CM.plan_window(D)
    NSH = int((t_end - T0) / SH)
    LAB = [f"{(T0 + k * SH - pd.Timedelta(hours=7)):%d}{'ABC'[k % 3]}" for k in range(NSH)]
    new, old = load(D), load(D, whole_lot=True)
    new["group"] = new["department"].map(group_of)
    name = new.groupby("machine")["machine_name"].first() if "machine_name" in new else None
    grp = new.groupby("machine")["group"].first()
    uom = new.groupby("machine")["UOM"].agg(lambda s: "/".join(sorted(s.astype(str).unique())))
    Q = new.pivot_table(index="machine", columns="k", values="produce_qty", aggfunc="sum").reindex(columns=range(NSH)).fillna(0)
    H = new.pivot_table(index="machine", columns="k", values="h", aggfunc="sum").reindex(columns=range(NSH)).fillna(0)
    QO = old.pivot_table(index="machine", columns="k", values="produce_qty", aggfunc="sum").reindex(columns=range(NSH)).fillna(0)

    det = CM.analysis_dir(D) / f"SHIFT_LOAD_BALANCE_{a.run}.shift_detail.csv"
    C = pd.read_csv(det) if det.exists() else pd.DataFrame()
    causes = ["WAIT_INPUT", "HELD", "BR14", "KIT_WAIT", "GT_WAIT", "PLAN_GAP", "NOT_CURED", "NO_WORK"]
    if len(C):
        C["machine"] = C["machine"].astype(str)
        C = C.set_index(["machine", "k"])

    rows = []
    for m in Q.index:
        q, h = Q.loc[m], H.loc[m]
        w = [k for k in range(NSH) if h[k] > 0.05]
        if not w:
            continue
        core = [k for k in range(w[0], w[-1])] or w        # last working shift is partial
        mh, mq = h[core].mean(), q[core].mean()
        bad_h = [k for k in core if abs(h[k] - mh) > a.band * mh]
        bad_q = [k for k in core if abs(q[k] - mq) > a.band * mq]
        qo = QO.loc[m] if m in QO.index else pd.Series(0, index=range(NSH))
        why = []
        for k in sorted(set(bad_h) | set(bad_q)):
            if len(C) and (m, k) in C.index:
                r = C.loc[(m, k)]
                cs = sorted(((float(r.get(c, 0) or 0), c) for c in causes), reverse=True)
                co = float(r.get("changeover_h", 0) or 0)
                above = h[k] > mh * (1 + a.band) or q[k] > mq * (1 + a.band)
                lab = ("ABOVE AVERAGE" if above else
                       cs[0][1] if cs[0][0] > 0.3 else ("CHANGEOVER" if co > 1.0 else "item mix"))
                if above:
                    cs = [(0.0, "")]
                extra = str(r.get("why_" + cs[0][1], "") or "") if cs[0][0] > 0.3 and cs[0][1] else ""
                if extra == "nan":
                    extra = ""
                why.append(f"{LAB[k]} {h[k]:.1f}h: {lab}" + (f" ({extra[:40]})" if extra else "")
                           + (f", CO {co:.1f}h" if co > 1.0 and lab != "CHANGEOVER" else ""))
        rows.append({
            "machine": m, "name": name.get(m, "") if name is not None else "", "group": grp[m],
            "uom": uom[m], "first": LAB[w[0]], "last": LAB[w[-1]], "working_shifts": len(core),
            "mean_h": round(mh, 2), "min_h": round(h[core].min(), 2), "max_h": round(h[core].max(), 2),
            "mean_qty": round(mq, 0), "min_qty": round(q[core].min(), 0), "max_qty": round(q[core].max(), 0),
            "max/min_qty_published": round(q[core].max() / q[core].min(), 1) if q[core].min() > 0 else None,
            "max/min_qty_old_report": (round(qo[core].max() / qo[core].min(), 1) if qo[core].min() > 0 else None),
            "shifts_out_h": len(bad_h), "shifts_out_qty": len(bad_q),
            "verdict": "LEVEL" if not bad_h and not bad_q else
                       ("UNBALANCED" if bad_h else "UNBALANCED (qty only: item mix)"),
            "idle_after_last_h": round(8 * (NSH - 1 - w[-1]) + (8 - h[w[-1]]), 1),
            "out_of_band_shifts": "; ".join(why)})
    V = pd.DataFrame(rows)
    Qp = Q.copy(); Qp.columns = LAB
    Hp = H.copy().round(2); Hp.columns = LAB
    Qop = QO.copy(); Qop.columns = LAB
    # line-level totals per group
    LT = (new.groupby(["line", "group", "k"])["produce_qty"].sum().unstack("k")
          .reindex(columns=range(NSH)).fillna(0).round(0))
    LT.columns = LAB
    out = CM.analysis_dir(D) / f"SHIFT_BALANCE_PUBLISHED_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        V.sort_values(["group", "verdict", "machine"]).to_excel(xw, sheet_name="Verdict", index=False)
        Qp.round(0).to_excel(xw, sheet_name="Qty by shift (published)")
        Hp.to_excel(xw, sheet_name="Hours by shift")
        Qop.round(0).to_excel(xw, sheet_name="Qty by shift (old report)")
        LT.to_excel(xw, sheet_name="Line totals")
        for ws in xw.sheets.values():
            ws.freeze_panes = "B2"
    V.to_csv(out.with_suffix(".verdict.csv"), index=False)
    Qp.round(0).to_csv(out.with_suffix(".qty.csv"))
    Hp.to_csv(out.with_suffix(".hours.csv"))
    print(V.groupby(["group", "verdict"]).size())
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
