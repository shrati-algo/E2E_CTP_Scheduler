#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_aging_fifo.py -- time-progressive component aging check of a finished schedule (2026-09-29).

    python verify_aging_fifo.py <run_dir> [slice_min=30]

Stricter / more realistic than verify_kit_feasibility.py (which consumed a whole block's kit at its START):
  * every component production row (floor schedule) is produced EVENLY over its run -> slices,
    opening stock is one slice at its produced_time
  * every build row consumes its kit EVENLY over its run -> slices
  * one pool per component ITEM, shared by ALL SKUs / green tyres (one lot feeds many SKUs)
  * each demand slice takes the OLDEST material that is past min aging and NOT yet past max aging
    (FEFO = FIFO here: one max aging per item). Material that expires unused is counted as scrap.
  * if non-expired material cannot cover the slice -> violation:
        NEEDS EXPIRED   material exists but only past max aging (building would use aged stock)
        NOT READY       no material at all at that moment (building starts before the kit)
Outputs <run_dir>/AGING_FIFO_CHECK.xlsx: Summary, By component, Violations (per build row x item),
Lot usage (every lot -> which SKUs it fed, at what age), Shared lots (lots feeding >1 SKU).
"""
import os
import re
import sys
from collections import defaultdict

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "output", "latest")
SL = pd.Timedelta(minutes=int(sys.argv[2]) if len(sys.argv) > 2 else 30)
INP = os.path.join(HERE, "inputs")
norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()
H = pd.Timedelta(hours=1)


def slices(st, en, qty):
    """split [st, en] into <=SL pieces, qty pro-rata; each piece stamped with its END time."""
    if pd.isna(st) or pd.isna(en) or en <= st:
        return [(en if pd.notna(en) else st, qty)]
    out, t, dur = [], st, (en - st) / H
    while t < en:
        u = min(en, t + SL)
        out.append((u, qty * ((u - t) / H) / dur))
        t = u
    return out


def main():
    F = pd.read_csv(os.path.join(RUN, "phase5_floor_schedule_updated.csv"), parse_dates=["start_time", "end_time"])
    F["item"] = F["item"].map(norm)
    G = pd.read_csv(os.path.join(RUN, "phase5_building_echo_gates.csv"))
    sku_of = dict(zip(G["row"], G["sku"].map(norm)))
    ag = pd.read_csv(os.path.join(INP, "aging_master.csv"))
    ag["k"] = ag["ItemCode"].map(norm)
    mn = dict(zip(ag["k"], pd.to_numeric(ag["MinAging"], errors="coerce").fillna(0)))
    mx = dict(zip(ag["k"], pd.to_numeric(ag["MaxAging"], errors="coerce").fillna(72)))
    inv = pd.read_csv(os.path.join(INP, "inventory.csv"), low_memory=False)
    inv["produced_time"] = pd.to_datetime(inv["produced_time"], errors="coerce", format="mixed")
    inv["k"] = inv["itemcode"].map(norm)
    bom = pd.read_csv(os.path.join(INP, "bom.csv"), dtype=str)
    bom = bom.assign(sp=bom["Super_parent"].map(norm), pa=bom["Parent"].map(norm), ch=bom["child"].map(norm),
                     q=pd.to_numeric(bom["child_quantity"], errors="coerce").fillna(0))
    gts = set(bom.loc[bom["pa"] == bom["sp"], "ch"])
    kit = bom[bom["pa"].isin(gts) & (bom["pa"] != bom["sp"]) & (bom["q"] > 0)]
    rate = defaultdict(dict)
    for (s, c), q in kit.groupby(["sp", "ch"])["q"].sum().items():
        rate[s][c] = float(q)
    ity = dict(zip(F["item"], F["item_type"]))
    ity.update({k: v for k, v in zip(inv["k"], inv["item_type"]) if k not in ity})

    B = F[F["department"].astype(str).str.upper() == "BUILDING"].copy()
    B["row"] = B["lot_id"].str[3:].astype(int)
    B["sku"] = B["row"].map(sku_of)
    kit_items = {c for s in B["sku"].unique() for c in rate.get(s, {})}
    Sx = F[(~F["department"].astype(str).str.upper().isin(["BUILDING", "CURING"])) & F["item"].isin(kit_items)]
    produced = set(Sx["item"]) | set(inv["k"])
    # share of an item that goes to building (rest feeds other components, e.g. GUM STRIP -> STC)
    need_gt = defaultdict(float)
    for _, r in B.iterrows():
        for c, q in rate.get(r["sku"], {}).items():
            need_gt[c] += float(r["produce_qty"]) * q
    other = defaultdict(float)                     # consumed by non-building parents inside this schedule
    kit_par = bom[bom["ch"].isin(kit_items) & ~bom["pa"].isin(gts)]
    par_rate = defaultdict(dict)
    for (p, c), q in kit_par.groupby(["pa", "ch"])["q"].max().items():
        par_rate[p][c] = float(q)
    for _, r in F[F["item"].isin(par_rate)].iterrows():
        for c, q in par_rate[r["item"]].items():
            other[c] += float(r["produce_qty"]) * q
    share = {c: (need_gt[c] / (need_gt[c] + other[c]) if need_gt[c] + other[c] > 0 else 1.0) for c in need_gt}

    # supply slices per item: [ready_at, expires_at, remaining, produced_at, lot]
    pools = defaultdict(list)
    for _, r in Sx.iterrows():
        it = r["item"]
        for t, q in slices(r["start_time"], r["end_time"], float(r["produce_qty"]) * share.get(it, 1.0)):
            pools[it].append([t + mn.get(it, 0) * H, t + mx.get(it, 72) * H, q, t, r["lot_id"]])
    for _, r in inv[inv["k"].isin(kit_items)].iterrows():
        it, pt = r["k"], r["produced_time"]
        q = float(pd.to_numeric(r["inventory"], errors="coerce") or 0) * share.get(it, 1.0)
        pt = pt if pd.notna(pt) else pd.Timestamp("2000-01-01")
        exp = pd.Timestamp("2100-01-01") if pt.year == 2000 else pt + mx.get(it, 72) * H
        pools[it].append([pt + mn.get(it, 0) * H, exp, q, pt, "WIP:" + str(r["production_id"])])
    for it in pools:
        pools[it].sort(key=lambda x: (x[1], x[0]))

    # demand slices, processed in time order
    dem = []
    for _, r in B.iterrows():
        for t, q in slices(r["start_time"], r["end_time"], float(r["produce_qty"])):
            for it, q1 in rate.get(r["sku"], {}).items():
                if it in produced:
                    dem.append((t, it, q * q1, r["row"], r["sku"], r["machine"]))
    dem.sort(key=lambda x: x[0])
    use = []                                       # (lot, item, sku, row, age_h, qty)
    bad = defaultdict(lambda: [0.0, 0.0, 0.0, None, None])  # (row,item) -> need, expired_used, not_ready, first_t, worst_age
    need_tot = defaultdict(float)
    for t, it, need, row, sku, mc in dem:
        need_tot[(row, it)] += need
        left = need
        pool = pools.get(it, [])
        for s in pool:                             # oldest valid first
            if left <= 1e-9:
                break
            if s[2] > 1e-9 and s[0] <= t and s[1] >= t:
                k = min(left, s[2]); s[2] -= k; left -= k
                use.append((s[4], it, sku, row, (t - s[3]) / H, k))
        if left <= 1e-6 * max(need, 1):
            continue
        b = bad[(row, it)]
        b[3] = b[3] or t
        for s in pool:                             # material exists but only past max aging
            if left <= 1e-9:
                break
            if s[2] > 1e-9 and s[0] <= t and s[1] < t:
                k = min(left, s[2]); s[2] -= k; left -= k; b[1] += k
                age = (t - s[3]) / H
                b[4] = max(b[4] or 0, age)
                use.append((s[4], it, sku, row, age, k))
        b[2] += max(left, 0)
        for s in pool:                             # take later supply so the replay continues
            if left <= 1e-9:
                break
            if s[2] > 1e-9:
                k = min(left, s[2]); s[2] -= k; left -= k

    info = B.groupby("row").agg(machine=("machine", "first"), sku=("sku", "first"), gt=("item", "first"),
                                start=("start_time", "min"), end=("end_time", "max"), qty=("produce_qty", "sum"))
    V = []
    for (row, it), (need, exp, nr, t1, worst) in bad.items():
        tot = need_tot[(row, it)]
        if exp + nr <= 1e-4 * tot:
            continue
        i = info.loc[row]
        V.append((row, i["machine"], i["sku"], i["gt"], i["start"], i["end"], i["qty"], it, ity.get(it, ""),
                  mx.get(it, 72), round(tot, 2), round(exp, 2), round(nr, 2), round(100 * (exp + nr) / tot, 1),
                  None if worst is None else round(worst, 1), t1,
                  "NOT READY" if nr > 1e-4 * tot else "NEEDS EXPIRED"))
    V = pd.DataFrame(V, columns=["plan_row", "building_machine", "sku", "green_tyre", "build_start", "build_end", "tyres",
                                 "component", "type", "max_aging_h", "component_needed", "expired_qty_used",
                                 "not_ready_qty", "pct_of_need_bad", "worst_age_h", "first_problem_at", "problem"])
    U = pd.DataFrame(use, columns=["lot", "component", "sku", "plan_row", "age_h", "qty"])
    U["max_aging_h"] = U["component"].map(lambda x: mx.get(x, 72))
    U["type"] = U["component"].map(lambda x: ity.get(x, ""))
    LU = U.groupby(["lot", "component", "type", "max_aging_h"]).agg(
        skus_fed=("sku", "nunique"), build_rows_fed=("plan_row", "nunique"), qty=("qty", "sum"),
        min_age_h=("age_h", "min"), max_age_h=("age_h", "max"),
        skus=("sku", lambda s: ", ".join(sorted(set(s))))).reset_index()
    LU["over_max"] = LU["max_age_h"] > LU["max_aging_h"] + 1e-6
    scrap = [(it, s[4], s[3], round(s[2], 2)) for it, p in pools.items() for s in p
             if s[2] > 1e-3 and s[1] < info["end"].max() and not str(s[4]).startswith("WIP:")]
    SC = pd.DataFrame(scrap, columns=["component", "lot", "produced_at", "qty"]) \
        .groupby(["component", "lot"]).agg(qty_expired_unused=("qty", "sum")).reset_index()

    bad_rows = set(V["plan_row"])
    nr_rows = set(V.loc[V["problem"] == "NOT READY", "plan_row"])
    tot_t = info["qty"].sum()
    summ = pd.DataFrame([
        ("Build rows (plan rows) checked", len(info), int(tot_t)),
        ("  every component fresh (within min..max aging) through the whole run", len(info) - len(bad_rows),
         int(info.loc[~info.index.isin(bad_rows), "qty"].sum())),
        ("  some component only available past MAX aging", len(bad_rows - nr_rows),
         int(info.loc[info.index.isin(bad_rows - nr_rows), "qty"].sum())),
        ("  some component NOT READY (no material at all)", len(nr_rows), int(info.loc[info.index.isin(nr_rows), "qty"].sum())),
        ("Component quantity fed (all items)", "", round(U["qty"].sum(), 0)),
        ("  of which past max aging", "", round(U.loc[U["age_h"] > U["max_aging_h"], "qty"].sum(), 0)),
        ("Lots / stock slices feeding building", LU["lot"].nunique(), ""),
        ("  lots feeding more than one SKU", int((LU["skus_fed"] > 1).sum()), ""),
        ("  lots with some quantity fed past max aging", int(LU["over_max"].sum()), ""),
    ], columns=["check", "count", "tyres_or_qty"])
    U["past_max"] = U["age_h"] > U["max_aging_h"]
    BC = U.groupby(["type", "component"]).apply(lambda d: pd.Series({
        "max_aging_h": d["max_aging_h"].iloc[0], "qty_fed": d["qty"].sum(),
        "qty_past_max": d.loc[d["past_max"], "qty"].sum(),
        "median_age_h": d["age_h"].median(), "build_rows_hit": d.loc[d["past_max"], "plan_row"].nunique()}),
        include_groups=False).reset_index()
    BC["pct_past_max"] = (100 * BC["qty_past_max"] / BC["qty_fed"]).round(1)
    BT = BC.groupby("type").agg(items=("component", "nunique"), max_aging_h=("max_aging_h", "min"),
                                qty_fed=("qty_fed", "sum"), qty_past_max=("qty_past_max", "sum")).reset_index()
    BT["pct_past_max"] = (100 * BT["qty_past_max"] / BT["qty_fed"]).round(1)
    BT["build_rows_hit"] = BT["type"].map(V.groupby("type")["plan_row"].nunique()).fillna(0).astype(int)
    BT = BT.sort_values("build_rows_hit", ascending=False)
    with pd.ExcelWriter(os.path.join(RUN, "AGING_FIFO_CHECK.xlsx")) as xl:
        summ.to_excel(xl, sheet_name="Summary", index=False)
        BT.round(1).to_excel(xl, sheet_name="By type", index=False)
        BC.round(1).sort_values("qty_past_max", ascending=False).to_excel(xl, sheet_name="By component", index=False)
        V.sort_values(["build_start", "plan_row"]).to_excel(xl, sheet_name="Violations", index=False)
        LU.round(1).sort_values(["over_max", "skus_fed"], ascending=False).to_excel(xl, sheet_name="Lot usage", index=False)
        SC.to_excel(xl, sheet_name="Expired unused (scrap)", index=False)
    print(summ.to_string(index=False))
    print("\nby type:\n" + BT.round(1).to_string(index=False))
    return V, U, LU


if __name__ == "__main__":
    main()
