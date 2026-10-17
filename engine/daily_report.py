#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""daily_report.py -- day-wise PLAN vs SCHEDULED, PCR / TBR, building and curing (2026-09-27).

    python daily_report.py <run_dir> <plan_dir>

Plant day = 07:00 -> 07:00. Scheduled quantities are PRO-RATED BY TIME inside each block
(a cure block 00:33-08:30 counts the share cured before 07:00 in that day, the rest in the next).
Earlier reports counted a whole block on the day it ENDED, which understated in-day output.
Writes <run_dir>/DAILY_REPORT.xlsx and prints the tables.
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "output", "latest")
PLANS = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "discard")
DAY = pd.Timedelta(days=1)


def _tbr_skus():
    s = set()
    for f in glob.glob(os.path.join(PLANS, "*tbr*.xlsx")):
        d = pd.read_excel(f, sheet_name="Shift Schedule", header=None)
        h = d.index[d.apply(lambda r: "SKUCode" in set(map(str, r)), axis=1)][0]
        s |= set(d.iloc[h + 1:, list(d.iloc[h]).index("SKUCode")].astype(str).str.strip())
    return s


def _spread(df, start, end, qty, days):
    """quantity per plant day, pro-rated by the time the block spends in each day."""
    out = np.zeros(len(days))
    st = df[start].values.astype("datetime64[ns]"); en = df[end].values.astype("datetime64[ns]")
    q = df[qty].values.astype(float)
    for i, d0 in enumerate(days):
        a = np.datetime64(d0); b = np.datetime64(d0 + DAY)
        dur = (en - st).astype("timedelta64[s]").astype(float)
        ov = (np.minimum(en, b) - np.maximum(st, a)).astype("timedelta64[s]").astype(float)
        frac = np.where(dur > 0, np.clip(ov / np.where(dur > 0, dur, 1), 0, 1), ((st >= a) & (st < b)).astype(float))
        out[i] = float((q * frac).sum())
    return out


def main():
    tbr = _tbr_skus()
    prod = lambda s: "TBR" if str(s).strip() in tbr else "PCR"
    pl = pd.read_csv(os.path.join(HERE, "inputs", "plan.csv"), parse_dates=["startTime", "endTime"])
    pl = pl[(pl["skuCode"].astype(str).str.upper() != "CHANGEOVER") & (pl["qty"] > 0)].reset_index(drop=True)
    bp = pd.read_csv(os.path.join(HERE, "inputs", "building_plan.csv"))
    if "origStartTime" in bp.columns:                  # report against the PLANT's slots
        bp["startTime"], bp["endTime"] = bp["origStartTime"], bp["origEndTime"]
    bp["startTime"] = pd.to_datetime(bp["startTime"]); bp["endTime"] = pd.to_datetime(bp["endTime"])
    cu = pd.read_csv(os.path.join(RUN, "phase6_curing_revision_updated.csv"), parse_dates=["startTime", "endTime"])
    fs = pd.read_csv(os.path.join(RUN, "phase5_floor_schedule_updated.csv"), parse_dates=["start_time", "end_time"])
    bd = fs[fs["department"].astype(str).str.upper() == "BUILDING"].copy()
    pl["line"] = pl["skuCode"].map(prod); bp["line"] = bp["skuCode"].map(prod); cu["line"] = cu["skuCode"].map(prod)
    bd["line"] = np.where(bd["machine"].astype(str).str.startswith("TBM-"), "TBR", "PCR")
    t0 = (min(pl["startTime"].min(), bp["startTime"].min()) - pd.Timedelta(hours=7)).normalize() + pd.Timedelta(hours=7)
    t_end = max(cu["endTime"].max(), bd["end_time"].max(), pl["endTime"].max())
    plan_end = max(pl["endTime"].max(), bp["endTime"].max())
    days = list(pd.date_range(t0, t_end, freq="D"))
    rows = []
    for L in ["PCR", "TBR", "ALL"]:
        f = (lambda d: d) if L == "ALL" else (lambda d: d[d["line"] == L])
        a = _spread(f(bp), "startTime", "endTime", "qty", days)
        b = _spread(f(bd), "start_time", "end_time", "produce_qty", days)
        c = _spread(f(pl), "startTime", "endTime", "qty", days)
        e = _spread(f(cu), "startTime", "endTime", "qty", days)
        for i, d in enumerate(days):
            rows.append((L, d.strftime("%d-%b"), a[i], b[i], c[i], e[i]))
    T = pd.DataFrame(rows, columns=["line", "day", "build_plan", "built", "cure_plan", "cured"]).round(0)
    T = T[(T[["build_plan", "built", "cure_plan", "cured"]].sum(axis=1) > 0.5)]
    for c in ["build_plan", "built", "cure_plan", "cured"]:
        T[c] = T[c].astype(int)
    # coverage by the plan end
    cov = []
    for L in ["PCR", "TBR", "ALL"]:
        f = (lambda d: d) if L == "ALL" else (lambda d: d[d["line"] == L])
        bb = f(bd); cc = f(cu)
        bdone = _spread(bb, "start_time", "end_time", "produce_qty", [t0])[0] if False else None
        def done(df, s, e, q):
            dur = (df[e] - df[s]).dt.total_seconds()
            fr = ((plan_end - df[s]).dt.total_seconds() / dur.where(dur > 0, 1)).clip(0, 1)
            return float((df[q] * fr).sum())
        cov.append((L, int(f(bp)["qty"].sum()), done(bb, "start_time", "end_time", "produce_qty"),
                    int(f(pl)["qty"].sum()), done(cc, "startTime", "endTime", "qty")))
    C = pd.DataFrame(cov, columns=["line", "build_plan", "built_by_plan_end", "cure_plan", "cured_by_plan_end"])
    C["built_%"] = (100 * C["built_by_plan_end"] / C["build_plan"]).round(1)
    C["cured_%"] = (100 * C["cured_by_plan_end"] / C["cure_plan"]).round(1)
    C[["built_by_plan_end", "cured_by_plan_end"]] = C[["built_by_plan_end", "cured_by_plan_end"]].round(0).astype(int)
    with pd.ExcelWriter(os.path.join(RUN, "DAILY_REPORT.xlsx")) as xl:
        C.to_excel(xl, sheet_name="Coverage by plan end", index=False)
        T.to_excel(xl, sheet_name="Daily PCR TBR", index=False)
    print(f"plan window ends {plan_end} | last cure {cu['endTime'].max()} | last build {bd['end_time'].max()}")
    print(C.to_string(index=False))
    for L in ["PCR", "TBR", "ALL"]:
        print(f"\n{L}"); print(T[T["line"] == L].drop(columns="line").to_string(index=False))


if __name__ == "__main__":
    main()
