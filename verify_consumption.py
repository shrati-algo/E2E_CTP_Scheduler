#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_consumption.py -- prove the CTP #44 repair on every run it touched.

Checks, per run:
  C1  no BUILDING row left as RAW_MATERIAL or blank
  C2  no CURING row left blank
  C3  no consume-before-make: every named lot FINISHES at or before the row STARTS
  C4  nothing but `consumption_id` changed vs the .bak_cons44 backup
  C5  every named component lot actually exists in the schedule (no invented ids)
"""
import glob
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SENTINEL = ("RAW_MATERIAL", "NO_GT_BUILT", "", "nan", "None")


def check(run):
    f = os.path.join(run, "phase5_floor_schedule_updated.csv")
    bak = f + ".bak_cons44"
    name = os.path.basename(run.rstrip("/\\"))
    if not os.path.exists(f):
        return None
    d = pd.read_csv(f, low_memory=False)
    dept = d["department"].astype(str).str.upper()
    not_co = d["process"].astype(str).str.upper() != "CHANGEOVER"
    b = d[dept.str.contains("BUILDING", na=False) & not_co]
    c = d[dept.str.contains("CURING", na=False) & not_co]

    r = {"run": name, "rows": len(d)}
    r["C1"] = int((b["consumption_id"].astype(str).isin(["RAW_MATERIAL", "nan"])
                   | b["consumption_id"].isna()).sum())
    r["C2"] = int(c["consumption_id"].isna().sum())

    prod = d[not_co].copy()
    prod["_e"] = pd.to_datetime(prod["end_time"], errors="coerce")
    fin = prod.groupby("lot_id")["_e"].max()
    known = set(fin.index.astype(str))

    late = miss = refs = 0
    for _, row in pd.concat([b, c]).iterrows():
        st = pd.to_datetime(row["start_time"], errors="coerce")
        for p in str(row["consumption_id"]).split(";"):
            p = p.strip()
            if not p or p.startswith("WIP:") or p in SENTINEL:
                continue
            refs += 1
            if p not in known:
                miss += 1
                continue
            e = fin.get(p)
            if pd.notna(e) and pd.notna(st) and e > st:
                late += 1
    r["C3"] = late
    r["C5"] = miss
    r["refs"] = refs

    r["C4"] = "no backup"
    if os.path.exists(bak):
        o = pd.read_csv(bak, low_memory=False)
        if len(o) != len(d) or list(o.columns) != list(d.columns):
            r["C4"] = "SHAPE CHANGED %s->%s" % (o.shape, d.shape)
        else:
            bad = []
            for col in o.columns:
                if col == "consumption_id":
                    continue
                a, bb = o[col], d[col]
                same = ((a.isna() & bb.isna()) | (a.astype(str) == bb.astype(str))).all()
                if not same:
                    bad.append(col)
            r["C4"] = "OK" if not bad else "CHANGED: " + ",".join(bad)
    return r


if __name__ == "__main__":
    root = os.path.join(HERE, "output")
    runs = [d for d in glob.glob(os.path.join(root, "*"))
            if os.path.isdir(d) and os.path.exists(
                os.path.join(d, "phase5_floor_schedule_updated.csv.bak_cons44"))]
    runs.sort(key=os.path.getmtime, reverse=True)
    print("%-32s %7s %6s %6s %6s %6s %8s  %s" %
          ("RUN", "rows", "C1raw", "C2blk", "C3late", "C5miss", "refs", "C4 other-cols"))
    fails = 0
    for run in runs:
        r = check(run)
        if not r:
            continue
        ok = (r["C1"] == 0 and r["C2"] == 0 and r["C3"] == 0 and r["C5"] == 0
              and r["C4"] in ("OK", "no backup"))
        fails += 0 if ok else 1
        print("%-32s %7d %6d %6d %6d %6d %8d  %s  %s" %
              (r["run"], r["rows"], r["C1"], r["C2"], r["C3"], r["C5"], r["refs"],
               r["C4"], "PASS" if ok else "*** FAIL ***"))
    print("\n%d run(s) checked, %d failing" % (len(runs), fails))
    sys.exit(1 if fails else 0)
