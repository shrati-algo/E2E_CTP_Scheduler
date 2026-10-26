#!/usr/bin/env python3
"""phase4_cpm.py — precedence DAG + backward CPM against the frozen building plan.

v10 phase 4 on CTP data.

  LFT   latest finish  — any later and the consumer cannot start on time
  LST   LFT - duration
  EST   earliest start — any earlier and the lot OVER-AGES before it is used
  slack LST - EST;  negative = cannot be made in time

THE BACKWARD PASS, per building lot, parents first through the item tree:
    finish_by(item) = min over parents p of start_by(p) - min_aging(item)
    start_by(item)  = finish_by(item) - duration(item)
so MIN AGING IS CHARGED AT EVERY LEVEL, plus a 10 min transfer. That is what
stops the plan assuming material can be consumed the instant it is made.

THE EARLY BOUND — AGEING DRIVES IT, NOT BOM DEPTH (v10, planner 27 Aug 2026)
    lead_h = min(max_aging, max(max_aging * LEAD_SHELF_FRACTION, depth * 24))
    EST    = consumer_start - lead_h - duration
Note the asymmetry v10 documents and that has caught people twice: the shelf
fraction can only ever LOOSEN a window. Tightening must come from the per-type
lead days.

CTP CHANGE: the per-item lead is computed BEFORE anything reads it, and the
CTP-specific correction from ctp_tbr_e2e_scheduler is kept — sizing on
`max_aging - min_aging` rather than `max_aging - lead` was measured there to
cause 91% of expired links.

EDGE QUANTITIES CONSERVE. A parent's share of a child is
    bom_ratio(parent, child) x what this building lot wants of the parent
normalised, so the edges out of a lot sum to what the lot makes.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io

LEAD_SHELF_FRACTION = 0.40          # v10's measured figure
LEAD_DAYS_DEFAULT = 1.0
SHORT_LEAD_DAYS = 0.5
LEAD_DAYS_BY_TYPE = {
    "master compound": 2.5, "final compound": 2.5,
    "rubberized ply": SHORT_LEAD_DAYS, "ply": SHORT_LEAD_DAYS,
    "cap strip": SHORT_LEAD_DAYS, "steel belt": SHORT_LEAD_DAYS,
    "tread": SHORT_LEAD_DAYS, "sidewall": SHORT_LEAD_DAYS,
    "inner liner": SHORT_LEAD_DAYS, "bead apex": SHORT_LEAD_DAYS,
}
RAMP_LEAD_H = 48.0
RAMP_LEAD_SHELF_FRAC = 0.9


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--open-h", type=float, default=24.0,
                    help="hours before the first build that the floor may run")
    args = ap.parse_args()
    OUT = io.outdir(args.run, args.line)

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 4 — precedence graph + backward CPM   [{args.line.upper()}]")
    print("=" * 74)

    alias = io.machine_alias(io.load_machines())
    bom = io.load_bom(args.line)
    rt = io.load_routing(args.line)
    sched = io.schedulable_items(rt)
    itypes = io.load_itemtypes(bom)
    aging = io.load_aging(args.line)
    aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)
    child_idx = io.build_child_index(bom)
    eq = bom["Equipment"].iloc[0]

    bom_ratio: dict[tuple[str, str], float] = {}
    for p, pq, c, cq in zip(bom["Parent"], bom["Parent_qty"], bom["child"],
                            bom["child_quantity"]):
        if p and c and pd.notna(pq) and pq > 0 and cq > 0:
            bom_ratio.setdefault((p, c), float(cq) / float(pq))

    blots = pd.read_csv(OUT / "phase1_building_lots.csv", parse_dates=["build_start"])
    lots = pd.read_csv(OUT / "phase3_lots.csv", parse_dates=["first_need", "last_need"])
    alloc = pd.read_csv(OUT / "phase3_lot_demand.csv", parse_dates=["need_by"])

    open_at = blots["build_start"].min() - pd.Timedelta(hours=args.open_h)
    print(f"  Building lots   : {len(blots):,}   production lots: {len(lots):,}")
    print(f"  Floor opens     : {open_at}   (first build "
          f"{blots['build_start'].min()}, -{args.open_h:g} h)")
    print(f"  BOM ratios      : {len(bom_ratio):,} parent->child pairs")

    dur_h = dict(zip(lots["lot_id"], lots["duration_min"] / 60.0))
    lot_item = dict(zip(lots["lot_id"], lots["item_code"]))

    q_lot_b: dict = defaultdict(float)
    tot_item_b: dict = defaultdict(float)
    serving: dict = defaultdict(list)
    for r in alloc.itertuples(index=False):
        q_lot_b[(r.lot_id, r.building_lot_id)] += float(r.qty)
        tot_item_b[(r.building_lot_id, r.item_code)] += float(r.qty)
        serving[(r.building_lot_id, r.item_code)].append(r.lot_id)
    for k in serving:
        serving[k] = sorted(set(serving[k]))

    lft_of: dict = {}
    est_of: dict = {}
    edges: set = set()
    edge_qty: dict = defaultdict(float)
    cache = io.WalkCache(child_idx, sched, io.bias_items(rt))
    transfer = pd.Timedelta(minutes=io.TRANSFER_MIN)

    def tighten(store, lot, v, keep_min=True):
        cur = store.get(lot)
        if cur is None or (v < cur if keep_min else v > cur):
            store[lot] = v

    for B in blots.itertuples(index=False):
        if (B.sku, eq) not in child_idx:
            continue
        nodes, item_edges, roots = cache.get(B.sku, eq, B.built_item, frozenset())
        if not nodes:
            continue
        depth = io.depth_from_roots(nodes, item_edges, roots)
        parents = defaultdict(list)
        for (p, c) in item_edges:
            parents[c].append(p)

        step_h = {}
        for it in nodes:
            ls = serving.get((B.building_lot_id, it), ())
            step_h[it] = max((dur_h.get(l, 0.0) for l in ls), default=0.0)

        finish_by, start_by = {}, {}
        for it in io.topo_order(nodes, item_edges):
            mn = float(aging.get(it, (0.0, 0.0))[0])
            ready = [start_by[p] for p in parents.get(it, ()) if p in start_by]
            f = (min(ready) if ready else B.build_start) - pd.Timedelta(hours=mn) - transfer
            finish_by[it] = f
            start_by[it] = f - pd.Timedelta(hours=step_h.get(it, 0.0))

        for it in nodes:
            ls = serving.get((B.building_lot_id, it), ())
            if not ls:
                continue
            mx = float(aging.get(it, (0.0, 0.0))[1])
            t = str(itypes.get(it, "")).strip().lower()
            for l in ls:
                tighten(lft_of, l, finish_by[it], True)
                if mx <= 0:
                    continue
                cons = min((start_by[p] for p in parents.get(it, ()) if p in start_by),
                           default=B.build_start)
                days = LEAD_DAYS_BY_TYPE.get(t, depth.get(it, 1) * LEAD_DAYS_DEFAULT)
                lead = min(mx, max(mx * LEAD_SHELF_FRACTION, days * 24.0))
                if RAMP_LEAD_H > 0 and cons < open_at + pd.Timedelta(hours=RAMP_LEAD_H):
                    lead = max(lead, mx * RAMP_LEAD_SHELF_FRAC)
                tighten(est_of, l,
                        cons - pd.Timedelta(hours=lead + dur_h.get(l, 0.0)), False)

            # --- conserve: split the child across its parent ITEMS ----------
            pl = list(parents.get(it, ()))
            w = {p: bom_ratio.get((p, it), 0.0) * tot_item_b.get((B.building_lot_id, p), 0.0)
                 for p in pl}
            wt = sum(w.values())
            for p in pl:
                frac = (w[p] / wt) if wt > 0 else (1.0 / max(len(pl), 1))
                pls = serving.get((B.building_lot_id, p), ())
                p_tot = tot_item_b.get((B.building_lot_id, p), 0.0)
                for cl in ls:
                    cq = q_lot_b.get((cl, B.building_lot_id), 0.0) * frac
                    for pp in pls:
                        edges.add((cl, pp))
                        if p_tot > 0 and cq > 0:
                            edge_qty[(cl, pp)] += cq * q_lot_b.get(
                                (pp, B.building_lot_id), 0.0) / p_tot

    # ---- global backward relaxation over the LOT dag ----------------------
    succ = defaultdict(list)
    indeg = defaultdict(int)
    all_lots = set(lots["lot_id"])
    for p, c in edges:
        succ[p].append(c)
        indeg[c] += 1
    order, dq = [], [n for n in all_lots if indeg[n] == 0]
    while dq:
        u = dq.pop()
        order.append(u)
        for v in succ.get(u, ()):
            indeg[v] -= 1
            if indeg[v] == 0:
                dq.append(v)
    if len(order) < len(all_lots):
        order += [n for n in all_lots if n not in set(order)]

    min_age = {i: pd.Timedelta(hours=float(aging.get(i, (0.0, 0.0))[0]))
               for i in set(lot_item.values())}
    n_t = 0
    for u in reversed(order):
        ss = succ.get(u)
        if not ss:
            continue
        mn = min_age.get(lot_item.get(u, ""), pd.Timedelta(0))
        cap = None
        for v in ss:
            lv = lft_of.get(v)
            if lv is None:
                continue
            c = lv - pd.Timedelta(hours=dur_h.get(v, 0.0)) - mn - transfer
            cap = c if cap is None or c < cap else cap
        if cap is not None and (lft_of.get(u) is None or cap < lft_of[u]):
            lft_of[u] = cap
            n_t += 1
    print(f"  Backward relaxation tightened {n_t:,} lot deadlines")

    rows = []
    for r in lots.itertuples(index=False):
        d = pd.Timedelta(hours=dur_h.get(r.lot_id, 0.0))
        lft = lft_of.get(r.lot_id, r.last_need)
        lst = lft - d
        est = est_of.get(r.lot_id)
        est = max(est, open_at) if est is not None else open_at
        conflict = est > lst
        if conflict:
            est = lst
        rows.append({"lot_id": r.lot_id, "run_id": r.run_id, "item_code": r.item_code,
                     "item_type": r.item_type, "department": r.department,
                     "operation": r.operation, "machines": r.machines,
                     "lot_qty": r.lot_qty, "uom": r.uom, "batches": r.batches,
                     "duration_h": dur_h.get(r.lot_id, 0.0),
                     "EST": est, "LST": lst, "LFT": lft,
                     "slack_h": (lst - est).total_seconds() / 3600.0,
                     "window_conflict": conflict})   # EST > LST: a slack conflict, NOT shelf life
    times = pd.DataFrame(rows)
    times["is_critical"] = times["slack_h"] <= 0
    times["before_open"] = times["LST"] < open_at

    print(f"  Walk cache      : {cache.stats()}")
    print(f"\n  Precedence edges: {len(edges):,}")
    print(f"  Critical lots   : {int(times['is_critical'].sum()):,}"
          f"  ({100*times['is_critical'].mean():.1f}%)")
    print(f"  LST before open : {int(times['before_open'].sum()):,}"
          f"  ({100*times['before_open'].mean():.1f}%)  <- cannot be made in time")
    n_c = int(times["window_conflict"].sum())
    if n_c:
        print(f"  Window conflicts (EST>LST): {n_c:,} ({100*n_c/len(times):.2f}%)"
              f"  <- the lot cannot fit between its earliest start and its deadline")

    print(f"\n  SLACK by department (h)")
    g = times.groupby("department").agg(lots=("lot_id", "size"),
                                        med=("slack_h", "median"),
                                        mn=("slack_h", "min"),
                                        crit=("is_critical", "sum"))
    for d, r in g.sort_values("med").iterrows():
        print(f"    {str(d)[:28]:29s} lots={int(r['lots']):>6,d} median={r['med']:>9.1f}"
              f"  min={r['mn']:>10.1f}  critical={int(r['crit']):>6,d}")

    times.to_csv(OUT / "phase4_lot_times.csv", index=False)
    ed = pd.DataFrame(sorted(edges), columns=["producer_lot", "consumer_lot"])
    ed["qty"] = [edge_qty.get((a, b), 0.0) for a, b in
                 zip(ed.producer_lot, ed.consumer_lot)]
    ed.to_csv(OUT / "phase4_edges.csv", index=False)
    times[times["before_open"] | (times["slack_h"] < 0)].to_csv(
        OUT / "phase4_infeasible.csv", index=False)
    print(f"\n[OK] phase4_lot_times.csv ({len(times):,})  phase4_edges.csv ({len(ed):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
