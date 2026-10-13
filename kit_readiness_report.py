#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kit_readiness_report.py -- for every build block, when was EACH kit component ready vs build start.

    python kit_readiness_report.py <run_dir> [out.xlsx]

Per component item one shared pool (all SKUs), supply = component lots at finish + min aging (their
building share) + opening WIP at produced_time + min aging. Build blocks consume in START order. A
component is READY for a block at the first time cumulative supply covers cumulative need up to and
including that block. Margin = build start - ready time (>= 0 means ready before the build started).
V5-guaranteed items (cap strip / edge gum) and bought-in / raw items are listed as such.
"""
import os
import re
import sys
from collections import defaultdict

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(RUN, "KIT_READINESS.xlsx")
INP = os.path.join(HERE, "inputs")
norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()
H = pd.Timedelta(hours=1)

cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
AA = [re.compile(p, re.I) for p in (cfg.get("kit_gate_assume_available") or [])]
S = pd.read_csv(os.path.join(RUN, "phase5_schedule_updated.csv"),
                parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
S["item"] = S["item"].map(norm)
lots = pd.read_csv(os.path.join(RUN, "phase2_lots_updated.csv"))
lq = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce")))
ity = dict(zip(lots["item_code"].map(norm), lots["item_type"]))
B = S[S["department"].astype(str).str.upper() == "BUILDING"].copy()
G = pd.read_csv(os.path.join(RUN, "phase5_building_echo_gates.csv"), parse_dates=["plan_start"])
B["row"] = B["lot_id"].str[3:].astype(int)
B = B.merge(G[["row", "sku", "gt", "qty", "plan_start"]], on="row", how="left")
U = S[~S["department"].astype(str).str.upper().isin(["BUILDING", "CURING"])].copy()
U["qty"] = U["lot_id"].map(lq)
ag = pd.read_csv(os.path.join(INP, "aging_master.csv"))
ag["k"] = ag["ItemCode"].map(norm)
mn = dict(zip(ag["k"], pd.to_numeric(ag["MinAging"], errors="coerce").fillna(0)))
bom = pd.read_csv(os.path.join(INP, "bom.csv"), dtype=str)
bom = bom.assign(sp=bom["Super_parent"].map(norm), pa=bom["Parent"].map(norm), ch=bom["child"].map(norm),
                 q=pd.to_numeric(bom["child_quantity"], errors="coerce").fillna(0),
                 u=bom["child_Unit"].astype(str).str.strip())
gts = set(bom.loc[bom["pa"] == bom["sp"], "ch"])
kit = bom[bom["pa"].isin(gts) & (bom["pa"] != bom["sp"]) & (bom["q"] > 0)]
rate = defaultdict(dict)
unit = {}
for (s_, c_), g in kit.groupby(["sp", "ch"]):
    rate[s_][c_] = float(g["q"].sum())
    unit[c_] = g["u"].iloc[0]
for c_, d_ in zip(kit["ch"], kit["child_description"]):
    ity.setdefault(c_, d_)
inv = pd.read_csv(os.path.join(INP, "inventory.csv"), low_memory=False)
inv["k"] = inv["itemcode"].map(norm)
inv["pt"] = pd.to_datetime(inv["produced_time"], errors="coerce", format="mixed")
inv["q"] = pd.to_numeric(inv["inventory"], errors="coerce").fillna(0)

# building share of items that also feed non-building parents (e.g. GUM STRIP -> STC)
need_b = defaultdict(float)
for _, r in B.iterrows():
    for c_, q1 in rate.get(norm(r["sku"]), {}).items():
        need_b[c_] += float(r["produce_qty"] if "produce_qty" in r else r["qty"]) * q1
t0 = B["plan_start"].min()
events = defaultdict(list)          # item -> [(time, +supply)]
for _, r in U[U["item"].isin(need_b)].iterrows():
    events[r["item"]].append((r["scheduled_finish"] + mn.get(r["item"], 0) * H, float(r["qty"] or 0), "lot"))
for _, r in inv[inv["k"].isin(need_b)].iterrows():
    t = t0 if pd.isna(r["pt"]) else r["pt"] + mn.get(r["k"], 0) * H
    events[r["k"]].append((t, float(r["q"]), "stock"))
tot_sup = {it: sum(q for _, q, _ in ev) for it, ev in events.items()}
share = {it: (min(1.0, need_b[it] / tot_sup[it]) if tot_sup.get(it, 0) > need_b[it] * 1.02 and it in
              set(bom.loc[~bom["pa"].isin(gts), "ch"]) else 1.0) for it in need_b}
for it in events:
    events[it] = sorted((t, q * share.get(it, 1.0), k) for t, q, k in events[it])

cum_need = defaultdict(float)
rows = []
blocks = []
for _, b in B.sort_values(["scheduled_start", "row"]).iterrows():
    st = b["scheduled_start"]
    sku = norm(b["sku"])
    n_items = n_ready = 0
    worst = None
    for it, q1 in sorted(rate.get(sku, {}).items()):
        need = float(b["qty"]) * q1
        typ = ity.get(it, "")
        if any(r_.search(it) for r_ in AA):
            rows.append((b["lot_id"], b["machine"], b["gt"], sku, st, b["plan_start"], b["qty"], it, typ,
                         unit.get(it, ""), round(need, 3), None, None, "AVAILABLE (V5 cap strip / edge gum)", ""))
            continue
        if it not in events:
            rows.append((b["lot_id"], b["machine"], b["gt"], sku, st, b["plan_start"], b["qty"], it, typ,
                         unit.get(it, ""), round(need, 3), None, None, "AVAILABLE (bought-in / not produced)", ""))
            continue
        cum_need[it] += need
        c = 0.0
        ready = None
        src = ""
        tol = max(1e-6, 1e-4 * cum_need[it])
        for t, q, k in events[it]:
            c += q
            if c >= cum_need[it] - tol:
                ready, src = t, k
                break
        n_items += 1
        if ready is None:
            status, margin = "NOT READY (supply exhausted)", None
        else:
            margin = (st - ready) / H
            status = "READY before start" if margin >= -0.25 else "LATE"
            n_ready += status == "READY before start"
            worst = margin if worst is None else min(worst, margin)
        rows.append((b["lot_id"], b["machine"], b["gt"], sku, st, b["plan_start"], b["qty"], it, typ,
                     unit.get(it, ""), round(need, 3), ready, None if margin is None else round(margin, 2),
                     status, src))
    blocks.append((b["lot_id"], b["machine"], b["gt"], sku, st, b["plan_start"], b["qty"], n_items, n_ready,
                   None if worst is None else round(worst, 2),
                   "ALL READY" if n_ready == n_items else f"{n_items - n_ready} NOT READY"))

D = pd.DataFrame(rows, columns=["build_lot", "building_machine", "green_tyre", "sku", "build_start", "plan_shift_start",
                                "tyres", "component", "component_type", "unit", "qty_needed", "component_ready_at",
                                "margin_h (start - ready)", "status", "last_supply_from"])
BK = pd.DataFrame(blocks, columns=["build_lot", "building_machine", "green_tyre", "sku", "build_start", "plan_shift_start",
                                   "tyres", "kit_items_checked", "items_ready_before_start",
                                   "tightest_margin_h", "kit_status"])
chk = D[D["margin_h (start - ready)"].notna()]
T = (chk.groupby("component_type")
     .agg(components=("component", "nunique"), checks=("component", "size"),
          ready_before_start=("status", lambda s: int((s == "READY before start").sum())),
          late=("status", lambda s: int((s != "READY before start").sum())),
          min_margin_h=("margin_h (start - ready)", "min"),
          median_margin_h=("margin_h (start - ready)", "median")).round(2).reset_index())
T["ready_%"] = (100 * T["ready_before_start"] / T["checks"]).round(1)
other = (D[D["margin_h (start - ready)"].isna()].groupby("status").agg(component_checks=("component", "size"),
         components=("component", "nunique")).reset_index())
summ = pd.DataFrame([
    ("Build blocks", len(BK)),
    ("  with EVERY checked component ready before the build started", int((BK["kit_status"] == "ALL READY").sum())),
    ("  with some component late / not ready", int((BK["kit_status"] != "ALL READY").sum())),
    ("Component checks (block x component, produced items)", len(chk)),
    ("  ready before start", int((chk["status"] == "READY before start").sum())),
    ("  late / not ready", int((chk["status"] != "READY before start").sum())),
], columns=["check", "count"])
late = D[(D["status"] != "READY before start") & D["margin_h (start - ready)"].notna()]
with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
    summ.to_excel(xl, sheet_name="Summary", index=False)
    T.to_excel(xl, sheet_name="Summary", index=False, startrow=len(summ) + 2)
    other.to_excel(xl, sheet_name="Summary", index=False, startrow=len(summ) + len(T) + 5)
    BK.to_excel(xl, sheet_name="Build blocks", index=False)
    D.to_excel(xl, sheet_name="Block x component", index=False)
    late.to_excel(xl, sheet_name="Late components", index=False)
    for ws in xl.book.worksheets:
        ws.freeze_panes = "A2"
        for col in ws.columns:
            w = max(len(str(c.value)) if c.value is not None else 0 for c in list(col)[:300])
            ws.column_dimensions[col[0].column_letter].width = min(max(10, w + 2), 42)
print(summ.to_string(index=False))
print(T.to_string(index=False))
print(other.to_string(index=False))
if len(late):
    print("\nlate / not ready:")
    print(late[["build_lot", "building_machine", "green_tyre", "build_start", "component", "component_type",
                "qty_needed", "component_ready_at", "margin_h (start - ready)"]].to_string(index=False))
print(f"-> {OUT}")
