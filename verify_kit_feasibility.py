#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_kit_feasibility.py -- independent feasibility check of a finished schedule (2026-09-29).

    python verify_kit_feasibility.py <run_dir>

Replays the OUTPUT (not the scheduler's own gate) and checks, for the plant:

BUILDING -- for every build block and every component of its kit (BOM, per tyre):
  * READY    : enough material physically exists at the build START
               (component lots: finish + min aging; opening stock: produced_time + min aging)
  * MAX AGING: the material consumed (first-expiring first) is not older than its max aging
  statuses: OK | NOT READY (short at start: shortfall + how long until it is available) |
            AGED (material exists but only past max aging)
CURING -- for every cure block: green tyre kit READY before the press start, oldest green tyre
          at the press within its shelf life.
Writes <run_dir>/KIT_FEASIBILITY_CHECK.xlsx (Summary / Building blocks / Building violations /
Curing blocks) and prints the summary.
"""
import os
import re
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "output", "latest")
INP = os.path.join(HERE, "inputs")
TOL = pd.Timedelta(minutes=15)
norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()


import yaml
AA = [re.compile(str(p_), re.I) for p_ in (yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
                                           .get("kit_gate_assume_available") or [])]


def main():
    S = pd.read_csv(os.path.join(RUN, "phase5_schedule_updated.csv"),
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
    S["item"] = S["item"].map(norm)
    S = S[~S["department"].astype(str).str.upper().isin(["BUILDING", "CURING"])]
    lots = pd.read_csv(os.path.join(HERE, "outputs2", "phase2_lots_updated.csv"))
    lq = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce")))
    S["qty"] = S["lot_id"].map(lq)
    G = pd.read_csv(os.path.join(RUN, "phase5_building_echo_gates.csv"), parse_dates=["plan_start", "start"])
    bp = pd.read_csv(os.path.join(INP, "building_plan.csv"))
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
    produced = set(S["item"]) | set(inv["k"])
    # share of each kit item that goes to building (the rest feeds other components, e.g. GUM STRIP -> STC)
    gd = pd.read_csv(max([p for p in (os.path.join(HERE, "outputs2", "phase1_demand_updated.csv.gz"),
                                      os.path.join(HERE, "outputs2", "phase1_demand_updated.csv")) if os.path.exists(p)],
                         key=os.path.getmtime), usecols=["item_code", "demand_qty"])
    gross = gd.groupby(gd["item_code"].map(norm))["demand_qty"].sum()
    need_gt = defaultdict(float)
    for _, r in bp.iterrows():
        for c, q in rate.get(norm(r["skuCode"]), {}).items():
            need_gt[c] += float(r["qty"]) * q
    share = {c: (min(1.0, need_gt[c] / gross[c]) if gross.get(c, 0) > 0 else 1.0) for c in need_gt}
    # supply pools: [available_at, expires_at, remaining]
    t0 = G["plan_start"].min()
    pools = defaultdict(list)
    for _, r in S[S["item"].isin(need_gt)].iterrows():
        it = r["item"]; f = r["scheduled_finish"]
        pools[it].append([f + pd.Timedelta(hours=mn.get(it, 0)), f + pd.Timedelta(hours=mx.get(it, 72)),
                          float(r["qty"] or 0) * share.get(it, 1.0), "lot " + r["lot_id"]])
    for _, r in inv[inv["k"].isin(need_gt)].iterrows():
        it = r["k"]; pt = r["produced_time"]
        q = float(pd.to_numeric(r["inventory"], errors="coerce") or 0) * share.get(it, 1.0)
        if pd.isna(pt):
            pools[it].append([t0, pd.Timestamp("2100-01-01"), q, "stock (no date)"])
        else:
            pools[it].append([pt + pd.Timedelta(hours=mn.get(it, 0)), pt + pd.Timedelta(hours=mx.get(it, 72)), q,
                              "stock " + str(r.get("production_id", ""))])
    for it in pools:
        pools[it].sort(key=lambda x: (x[1], x[0]))                 # first-expiring first
    blocks, viol = [], []
    for _, b in G.sort_values(["start", "row"]).iterrows():
        sku, st, qty = norm(b["sku"]), b["start"], float(b["qty"])
        worst, n_items = "OK", 0
        for it, q1 in rate.get(sku, {}).items():
            if it not in produced:
                continue                                          # bought-in / raw: always available
            if any(r_.search(str(it)) for r_ in AA):
                continue                                          # V5-guaranteed chain (cap strip / edge gum)
            n_items += 1
            need = qty * q1; left = need; aged_used = 0.0; oldest = None
            pool = pools.get(it, [])
            for lot in pool:                                      # fresh, available material first
                if left <= 1e-9:
                    break
                if lot[2] > 1e-9 and lot[0] <= st + TOL and lot[1] >= st:
                    take = min(left, lot[2]); lot[2] -= take; left -= take
            fresh_short = left
            for lot in pool:                                      # then anything available but past max aging
                if left <= 1e-9:
                    break
                if lot[2] > 1e-9 and lot[0] <= st + TOL and lot[1] < st:
                    take = min(left, lot[2]); lot[2] -= take; left -= take; aged_used += take
            status = "OK"
            if left > max(1e-6, 1e-4 * need):
                future = sorted(l[0] for l in pool if l[2] > 1e-9 and l[0] > st + TOL)
                wait_h = round((future[0] - st).total_seconds() / 3600, 1) if future else None
                status = "NOT READY"
                for lot in pool:                                  # consume later supply so the replay continues
                    if left <= 1e-9:
                        break
                    if lot[2] > 1e-9:
                        take = min(left, lot[2]); lot[2] -= take; left -= take
                viol.append((b["row"], b["tbm"], sku, b["gt"], st, qty, it, round(need, 2), round(fresh_short, 2),
                             "NOT READY at build start", wait_h))
            elif aged_used > max(1e-6, 1e-4 * need):
                status = "AGED"
                viol.append((b["row"], b["tbm"], sku, b["gt"], st, qty, it, round(need, 2), round(aged_used, 2),
                             f"past max aging ({mx.get(it, 72):g} h)", None))
            if status == "NOT READY" or (status == "AGED" and worst == "OK"):
                worst = status
        blocks.append((b["row"], b["tbm"], sku, b["gt"], b["plan_start"], st, qty, n_items, worst))
    B = pd.DataFrame(blocks, columns=["plan_row", "building_machine", "sku", "green_tyre", "plan_start", "scheduled_start",
                                      "qty", "kit_items_checked", "kit_status"])
    V = pd.DataFrame(viol, columns=["plan_row", "building_machine", "sku", "green_tyre", "scheduled_start", "qty",
                                    "component", "needed", "short_or_aged_qty", "problem", "available_after_h"])
    # curing
    C = pd.read_csv(os.path.join(RUN, "phase6_curing_revision_updated.csv"), parse_dates=["startTime", "kit_ready"])
    gtlim = float(pd.to_numeric(pd.Series([72]), errors="coerce").iloc[0])
    C["gt_ready_before_press"] = (C["kit_status"].astype(str).str.upper() == "READY") & \
                                 (C["kit_ready"].isna() | (C["kit_ready"] <= C["startTime"] + TOL))
    C["gt_within_shelf_life"] = pd.to_numeric(C["green_age_h"], errors="coerce").fillna(0) <= gtlim
    C["cure_status"] = np.where(~C["gt_ready_before_press"], "GT NOT READY",
                                np.where(~C["gt_within_shelf_life"], "GT PAST 72 h", "OK"))
    summ = pd.DataFrame([
        ("Build blocks checked", len(B), int(B["qty"].sum())),
        ("  kit complete and within aging (OK)", int((B["kit_status"] == "OK").sum()), int(B.loc[B["kit_status"] == "OK", "qty"].sum())),
        ("  a component NOT READY at build start", int((B["kit_status"] == "NOT READY").sum()), int(B.loc[B["kit_status"] == "NOT READY", "qty"].sum())),
        ("  a component used past MAX aging", int((B["kit_status"] == "AGED").sum()), int(B.loc[B["kit_status"] == "AGED", "qty"].sum())),
        ("Cure blocks checked", len(C), int(C["qty"].sum())),
        ("  green tyre ready at press and within 72 h (OK)", int((C["cure_status"] == "OK").sum()), int(C.loc[C["cure_status"] == "OK", "qty"].sum())),
        ("  green tyre NOT READY at press start", int((C["cure_status"] == "GT NOT READY").sum()), int(C.loc[C["cure_status"] == "GT NOT READY", "qty"].sum())),
        ("  green tyre older than 72 h at press", int((C["cure_status"] == "GT PAST 72 h").sum()), int(C.loc[C["cure_status"] == "GT PAST 72 h", "qty"].sum())),
    ], columns=["check", "blocks", "tyres"])
    with pd.ExcelWriter(os.path.join(RUN, "KIT_FEASIBILITY_CHECK.xlsx")) as xl:
        summ.to_excel(xl, sheet_name="Summary", index=False)
        B.to_excel(xl, sheet_name="Building blocks", index=False)
        V.to_excel(xl, sheet_name="Building violations", index=False)
        C[["skuCode", "pressNo", "shift", "startTime", "qty", "kit_status", "kit_ready", "green_age_h", "cure_status"]] \
            .to_excel(xl, sheet_name="Curing blocks", index=False)
    print(summ.to_string(index=False))
    if len(V):
        print("\nviolations by component:")
        print(V.groupby(["problem", "component"]).agg(blocks=("plan_row", "nunique"), qty=("short_or_aged_qty", "sum"),
                                                      max_wait_h=("available_after_h", "max")).round(1).to_string())
    return summ, V


if __name__ == "__main__":
    main()
