"""day_pool.py -- one shared day-pool sequencing of the building plan (2026-09-30).

The plant's Shift Schedule is a DAY plan sliced into shifts pro-rata (its Notes sheet): the order
of a machine's SKUs inside a plant-day is not given. phase5 builds each (machine, plant-day) from
07:00 in order of CURING NEED; phase1b must time component demand on that SAME order, otherwise
components are due at the plan's cosmetic slot (e.g. 15:00) while the build now starts at 07:00.

sequence(bp, inputs_dir) -> bp re-ordered with
    _orig_start  the plan's slot start
    _need_t      when the presses first need this row's tyres
    _day0        the plant-day start (07:00) = earliest start for every row of the day
    _pred_start  predicted build start: _day0 + the durations of the rows ahead of it on the machine
"""
from collections import defaultdict

import numpy as np
import pandas as pd


def sequence(bp, inputs_dir):
    bp = bp.copy()
    bp["startTime"] = pd.to_datetime(bp["startTime"], errors="coerce")
    bp["qty"] = pd.to_numeric(bp["qty"], errors="coerce").fillna(0.0)
    pl = pd.read_csv(inputs_dir / "plan.csv")
    pl = pl[pl["skuCode"].astype(str).str.upper() != "CHANGEOVER"].copy()
    pl["startTime"] = pd.to_datetime(pl["startTime"], errors="coerce")
    pl["qty"] = pd.to_numeric(pl["qty"], errors="coerce").fillna(0.0)
    bom = pd.read_csv(inputs_dir / "bom.csv", dtype=str, usecols=["Super_parent", "child", "child_description"])
    bom = bom[bom["child_description"].astype(str).str.strip().str.upper().isin(["GREEN TYRES", "GREEN_TYRE"])]
    s2g = dict(zip(bom["Super_parent"].str.strip(), bom["child"].str.strip()))
    pl["gt"] = pl["skuCode"].astype(str).str.strip().map(s2g)
    gtc = bp["gtCode"].astype(str).str.strip() if "gtCode" in bp else pd.Series("", index=bp.index)
    bp["_gt"] = bp["skuCode"].astype(str).str.strip().map(s2g).fillna(gtc)
    inv = pd.read_csv(inputs_dir / "inventory.csv", low_memory=False)
    gtw = pd.to_numeric(inv["inventory"], errors="coerce").fillna(0.0).groupby(inv["itemcode"].astype(str).str.strip()).sum()
    need = {g: (d["startTime"].values, d["qty"].cumsum().values)
            for g, d in pl.dropna(subset=["gt"]).sort_values("startTime").groupby("gt")}
    cum = defaultdict(float)
    nt = {}
    for i, r in bp.sort_values("startTime", kind="mergesort").iterrows():
        g = r["_gt"]
        before = gtw.get(g, 0.0) + cum[g]
        cum[g] += float(r["qty"])
        t = pd.Timestamp.max
        if g in need:
            ts, cs = need[g]
            k = int(np.searchsorted(cs, before + 1e-6, side="right"))
            if k < len(ts):
                t = pd.Timestamp(ts[k])
        nt[i] = t
    bp["_need_t"] = pd.Series(nt)
    bp["_orig_start"] = bp["startTime"]
    bp["_day0"] = (bp["startTime"] - pd.Timedelta(hours=7)).dt.normalize() + pd.Timedelta(hours=7)
    bp = bp.sort_values(["_day0", "_need_t", "_orig_start"], kind="mergesort").reset_index(drop=True)
    cyc = pd.to_numeric(bp.get("cycleTime", 0), errors="coerce").fillna(0.0)
    dur = pd.to_timedelta(bp["qty"] * cyc, unit="m")
    ends = {}
    pred = []
    for i, r in bp.iterrows():
        m = str(r["buildMachine"]).strip()
        s = max(r["_day0"], ends.get(m, r["_day0"]))
        pred.append(s)
        ends[m] = s + dur.iloc[i]
    bp["_pred_start"] = pred
    bp["_pred_end"] = bp["_pred_start"] + dur
    return bp
