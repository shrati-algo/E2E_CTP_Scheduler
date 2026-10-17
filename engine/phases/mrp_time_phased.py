# -*- coding: utf-8 -*-
"""
mrp_time_phased.py -- C1 (2026-09-27): TIME-PHASED, LEVEL-BY-LEVEL MRP for phase1c.

Enabled by config `phase1c_time_phased_mrp: true` (phase1c calls run(); otherwise the v6
cascade runs unchanged).

WHY (SEP2026_ISSUES_FIXES.md, RCA of the day-2 TBR slip). The v6 cascade in phase1c
  (a) nets GREEN-TYRE stock although the plant's building plan is ALREADY net of it
      (plan sheet: Planned+GT = Planned_Units + GT_Inventory) -> every component under-sized;
  (b) computes a parent's saving over the PARENT's shelf-life window and removes it from the
      CHILD's (shorter) window -> 3 days of saving wipe the child's day-1/2 need;
  (c) smears a parent's saving PRO-RATA over ALL of the child's demand, so stock on one route
      (steel-belt STC WIP) erases demand another route needs (GT -> GUM STRIP directly).
  Measured: 191 of 349 components had day-1/2 need cut by MORE than their own stock.

THE ALGORITHM (classic MRP, per SKU / per build block / per BOM route):
  * the SKU node carries the build quantity T of each block (the building plan);
  * items are processed in BOM topological order (tyre -> raw); an item's GROSS requirement
    in a block is SUM over its parents p of  NET(p) x q(p->c) / r(p)   where q(p->c) is the
    per-tyre quantity on that BOM edge (JK convention: child_quantity is per tyre, same-UOM
    rows summed -- identical to phase1b.per_tyre_demand) and r(p) = SUM of q(*->p);
  * the item is netted against ITS OWN opening stock only, CHRONOLOGICALLY (earliest need
    first), FEFO, and only with lots still alive (produced_time + max_aging >= need_by);
  * only the NET requirement is passed to the children (so a parent's stock reduces its
    children on ITS OWN route and time, never another route's);
  * green tyres are NOT netted when the building plan is echoed (the plan is already net).

PARITY GATE: with every stock set to zero the algorithm must reproduce phase1b's gross
demand cell-for-cell. It is checked on every run; if it fails, run() returns None and
phase1c falls back to the v6 cascade (loudly).
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

import numpy as np
import pandas as pd

_TO_M = {"MM": 0.001, "CM": 0.01, "MTR": 1.0, "M": 1.0}


def _canon(u):
    return "" if u is None or (isinstance(u, float) and pd.isna(u)) else str(u).strip().upper()


def _edge_table(bom: pd.DataFrame, routing_uom: dict) -> pd.DataFrame:
    """(sku, parent, child) -> per-tyre qty, same rule as phase1b.per_tyre_demand."""
    b = bom.copy()
    b["sku"] = b["Super_parent"].astype(str).str.strip()
    b["par"] = b["Parent"].astype(str).str.strip()
    b["ch"] = b["child"].astype(str).str.strip()
    b["q"] = pd.to_numeric(b["child_quantity"], errors="coerce").fillna(0.0)
    b["u"] = b["child_Unit"].map(_canon)
    out = []
    for (s, p, c), g in b.groupby(["sku", "par", "ch"], sort=False):
        us = set(g["u"])
        if len(us) <= 1:
            q = float(g["q"].sum()); u = next(iter(us)) if us else ""
        else:                                     # same edge in conflicting UOMs: routing wins
            ru = routing_uom.get(c, "")
            hit = g[g["u"] == ru]
            r0 = hit.iloc[0] if len(hit) else g.iloc[0]
            q, u = float(r0["q"]), r0["u"]
        if q > 0:
            out.append((s, p, c, q, u))
    return pd.DataFrame(out, columns=["sku", "par", "ch", "q", "u"])


def _stock_lots(inv: pd.DataFrame, max_h: dict, demand_uom: dict, skip: set) -> dict:
    """item -> list of [expiry_ts, qty] sorted FEFO (oldest expiry first)."""
    if inv is None or inv.empty:
        return {}
    d = inv.copy()
    d["itemcode"] = d["itemcode"].astype(str).str.strip()
    d = d[~d["itemcode"].isin(skip)]
    d["qty"] = pd.to_numeric(d["inventory"], errors="coerce").fillna(0.0)
    d = d[d["qty"] > 0]
    u = d.get("unit", pd.Series("", index=d.index)).map(_canon)
    du = d["itemcode"].map(demand_uom).fillna("")
    f = np.where(u.isin(["MM", "CM"]) & (du == "M"), u.map(_TO_M).fillna(1.0), 1.0)
    d["qty"] = d["qty"] * f
    pt = pd.to_datetime(d.get("produced_time"), errors="coerce", format="mixed")
    mh = d["itemcode"].map(max_h).fillna(72.0)
    exp = pt + pd.to_timedelta(mh, unit="h")
    FAR = pd.Timestamp("2200-01-01")                     # no timestamp: never expires (v6 view)
    lots = defaultdict(list)
    for it, e, q in zip(d["itemcode"], exp, d["qty"]):
        lots[it].append([FAR if pd.isna(e) else pd.Timestamp(e), float(q)])
    for it in lots:
        lots[it].sort(key=lambda x: x[0])
    return lots


def _mrp(edges, blocks, lots, no_net):
    """Core. blocks: DataFrame sku, block_id, need_by, T. Returns {item: {(sku,bid): net}}."""
    by_sku_par = defaultdict(list)                 # (sku, par) -> [(child, q)]
    rate = defaultdict(float)                      # (sku, item) -> per-tyre qty
    children, indeg, nodes = defaultdict(set), defaultdict(int), set()
    for s, p, c, q in zip(edges["sku"], edges["par"], edges["ch"], edges["q"]):
        by_sku_par[(s, p)].append((c, q))
        rate[(s, c)] += q
        if c not in children[p]:
            children[p].add(c); indeg[c] += 1
        nodes.update((p, c))
    q_ = deque(sorted(n for n in nodes if indeg[n] == 0))
    order, seen = [], set()
    ind = dict(indeg)
    while q_:
        n = q_.popleft(); order.append(n); seen.add(n)
        for c in sorted(children[n]):
            ind[c] -= 1
            if ind[c] == 0:
                q_.append(c)
    order += sorted(nodes - seen)                  # cycle safeguard

    need_by = {(s, b): t for s, b, t in zip(blocks["sku"], blocks["block_id"], blocks["need_by"])}
    gross = defaultdict(lambda: defaultdict(float))  # item -> (sku,bid) -> qty
    for s, b, T in zip(blocks["sku"], blocks["block_id"], blocks["T"]):
        gross[s][(s, b)] += float(T)               # the SKU node = tyres to build in the block
        rate[(s, s)] = 1.0
    net = {}
    for it in order:
        g = gross.get(it)
        if not g:
            continue
        rows = sorted(g.items(), key=lambda kv: (need_by.get(kv[0], pd.Timestamp.max), kv[0]))
        n = {}
        stock = lots.get(it) if it not in no_net else None
        for key, qty in rows:
            left = qty
            if stock:
                t = need_by.get(key)
                for lot in stock:
                    if left <= 1e-9:
                        break
                    if lot[1] <= 1e-12 or (t is not None and lot[0] < t):
                        continue                   # empty, or expired before this need
                    take = min(left, lot[1]); lot[1] -= take; left -= take
            n[key] = left
        net[it] = n
        for key, nq in n.items():
            if nq <= 1e-12:
                continue
            s = key[0]
            rp = rate.get((s, it), 0.0)
            for c, q in by_sku_par.get((s, it), ()):
                gross[c][key] += (nq * q / rp) if rp > 0 else (q * nq)
    return net


def run(demand: pd.DataFrame, cfg: dict, plan_start, bom: pd.DataFrame, routing: pd.DataFrame,
        inv: pd.DataFrame, max_aging_map: dict, itype_map: dict):
    """Return the NET demand frame (phase1b schema) or None if the parity gate fails."""
    t0 = time.time()
    d = demand.copy()
    d["item_code"] = d["item_code"].astype(str).str.strip()
    d["sku"] = d["sku"].astype(str).str.strip()
    try:
        import phase1b_demand_explosion as p1b
        ruom = p1b.build_routing_uom_map(routing)
    except Exception:
        ruom = {}
    bom = bom.rename(columns={"Super parent": "Super_parent", "child quantity": "child_quantity",
                              "child Unit": "child_Unit"})
    skus = set(d["sku"])
    edges = _edge_table(bom[bom["Super_parent"].astype(str).str.strip().isin(skus)], ruom)

    # green tyres = the SKU's direct BOM children
    gt_items = set(edges.loc[edges["par"] == edges["sku"], "ch"])
    gt_rows = d[d["item_code"].isin(gt_items)]
    blocks = (gt_rows.groupby(["sku", "block_id"], as_index=False)
              .agg(need_by=("need_by", "min"), T=("demand_qty", "sum")))
    uom = d.groupby("item_code")["demand_uom"].first().to_dict()

    # ---- PARITY GATE: zero stock must reproduce phase1b's gross demand exactly ----
    net0 = _mrp(edges, blocks, {}, set())
    chk = d[["item_code", "sku", "block_id", "demand_qty"]].copy()
    chk["mrp"] = [net0.get(i, {}).get((s, b), 0.0)
                  for i, s, b in zip(chk["item_code"], chk["sku"], chk["block_id"])]
    bad = chk[(chk["demand_qty"] - chk["mrp"]).abs() > 1e-6 * np.maximum(1.0, chk["demand_qty"].abs())]
    tot, badq = float(chk["demand_qty"].sum()), float((chk["demand_qty"] - chk["mrp"]).abs()[bad.index].sum())
    print(f"  [C1 TP-MRP] parity gate (zero stock == phase1b gross): {len(chk)-len(bad):,}/{len(chk):,} "
          f"cells match; mismatch qty {badq:,.1f} of {tot:,.0f}")
    if badq > 1e-4 * max(tot, 1.0):
        print("  [C1 TP-MRP] !! PARITY GATE FAILED -> falling back to the v6 cascade. Examples:")
        print(bad.head(8).to_string(index=False))
        return None

    echo = bool(cfg.get("building_plan_echo", False))
    no_net = set(gt_items) if echo else set()
    lots = _stock_lots(inv, max_aging_map, uom, no_net)
    stock_in = sum(q for v in lots.values() for _, q in v)
    net = _mrp(edges, blocks, lots, no_net)
    stock_left = sum(q for v in lots.values() for _, q in v)

    out = d.drop(columns=[c for c in ("hours_from_start",) if c in d.columns]).copy()
    out["demand_qty"] = [round(net.get(i, {}).get((s, b), 0.0), 4)
                         for i, s, b in zip(out["item_code"], out["sku"], out["block_id"])]
    out = out[out["demand_qty"] > 0]
    gq, nq = float(d["demand_qty"].sum()), float(out["demand_qty"].sum())
    print(f"  [C1 TP-MRP] green-tyre stock netted: {'NO (building plan is echoed & already net of GT stock)' if echo else 'yes'}")
    print(f"  [C1 TP-MRP] opening stock usable (alive, own item): {stock_in:,.0f} units; "
          f"consumed by netting {stock_in-stock_left:,.0f}")
    print(f"  [C1 TP-MRP] gross {gq:,.0f} -> net {nq:,.0f}  (saved {gq-nq:,.0f}, {100*(gq-nq)/max(gq,1):.1f}%)"
          f"   [{time.time()-t0:.1f}s]")
    return out
