#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mes_queue_times.py -- measure real stage-to-stage lag from the plant MES production log (2026-09-27).

Source: JK_CTP_PPDS.CTP_Live_Inventory_Raw (every stock-machine lot with machine + SyncTime).
For every BOM edge child -> parent where BOTH are produced in the MES window, take each child lot's
production time t and the FIRST production of the parent item after t:
      lag = t(first parent run after the child lot) - t(child lot)
      queue = lag - min_aging(child)          (clipped at 0)
The parent's machine decides the STAGE (where the queue happens). Median, P75 and P90 per stage.
This is the plant's own history; it is the time a released input waited before the next stage
started using that item (queue + setup + transfer), not a model value.
"""
import os
import re
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "db_pipeline", "raw", "JK_CTP_PPDS__CTP_Live_Inventory_Raw.csv")
STAGE = {"mm": "mixer", "fourroll": "calender", "tbril": "calender", "quadraplex": "extruder",
         "quintoplexpcr": "extruder", "triplex": "extruder", "duplex": "extruder", "belttbr": "cutter",
         "beltpcr": "cutter", "fisher": "cutter", "tbrbodyply": "cutter", "bodyplypcr": "cutter",
         "biaspcr": "cutter", "beadpcr": "bead", "bestryapex": "bead", "tda": "bead",
         "chafferpcr": "slitter", "chaffertbr": "slitter", "doubling": "slitter",
         "superassembly": "assembly", "edgegum": "edge", "beltedgefiller": "edge", "ebr": "edge"}
norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()


def main():
    r = pd.read_csv(RAW, parse_dates=["SyncTime"], low_memory=False)
    r["item"] = r["ItemCode"].map(norm)
    r["stage"] = r["Machine"].astype(str).str.lower().map(STAGE)
    ev = {i: np.sort(g["SyncTime"].values) for i, g in r.groupby("item")}
    st = r.groupby("item")["stage"].agg(lambda s: s.mode().iloc[0] if s.notna().any() else None)
    bom = pd.read_csv(os.path.join(HERE, "ctp_inputfiles", "bom_comb.csv"), dtype=str)
    edges = {(norm(c), norm(p)) for c, p in zip(bom["child"], bom["Parent"])}
    ag = pd.read_csv(os.path.join(HERE, "inputs", "aging_master.csv"))
    mn = dict(zip(ag["ItemCode"].map(norm), pd.to_numeric(ag["MinAging"], errors="coerce").fillna(0)))
    rows = []
    for c, p in edges:
        if c not in ev or p not in ev or st.get(p) is None:
            continue
        tp = ev[p]
        for t in ev[c]:
            k = np.searchsorted(tp, t, side="right")
            if k < len(tp):
                lag = (tp[k] - t) / np.timedelta64(1, "h")
                if lag <= 7 * 24:                       # ignore links with no consumption within a week
                    rows.append((st[p], c, p, lag, max(0.0, lag - mn.get(c, 0.0))))
    d = pd.DataFrame(rows, columns=["stage", "child", "parent", "lag_h", "queue_h"])
    per_edge = d.groupby(["stage", "child", "parent"])["queue_h"].median().reset_index()
    out = per_edge.groupby("stage")["queue_h"].agg(
        median="median", p75=lambda x: x.quantile(0.75), p90=lambda x: x.quantile(0.9), n_links="size").round(1)
    out["n_lot_pairs"] = d.groupby("stage").size()
    return out, d


if __name__ == "__main__":
    o, _ = main()
    print(o.to_string())
