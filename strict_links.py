#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""strict_links.py -- strict lot_id / consumption_id genealogy for the schedule workbook (2026-10-01).

The phase-3 DAG links are allocation links made before placement: after phase 5 moves lots, some
links point at a lot that is not ready yet when the consumer starts (its share really came from
opening stock). This module rebuilds every link with a time-aware material ledger so that each
link is physically possible:

  demand  per consumer row and BOM child = row qty x (child_quantity / parent qty per tyre in the
          lot's UOM) for the lot's own SKU (median over SKUs when unknown); curing = 1 GT per tyre
  supply  opening stock (inputs/inventory.csv, each production_id is a lot, usable at
          produced_time + min aging) + scheduled lots released per floor-schedule (shift) row:
            upstream rows -> the row's quantity usable at the row end + min aging (phase-5 kit gate)
            building rows -> green tyres emitted one by one across the row, each curable at its
                             emission + GT transfer time (phase-6 S6 streaming gate, routing 90 min)
  timing  upstream / building rows draw their whole input at the row start (kit must be complete);
          a cure block draws tyre k at start + k x duration / qty (the press consumes linearly)
  rule    first-expiry-first-out from supply usable by then and not past max aging; every kg / m /
          tyre is drawn once, so no quantity is consumed twice

consumption_id tokens (';' separated, each with the quantity drawn):
  <lot_id>(q)              a scheduled lot of the plan
  OPENING_WIP:<prod_id>(q) an opening-stock lot (production_id of the inventory table)
  RAW_MATERIAL:<item>      a bought-in input (never produced in the plan, no opening stock)
  SHORT:<item>(q)          demand not covered by any ready, unexpired supply at the start
"""
import heapq
from collections import defaultdict

import pandas as pd

import os
EPS = 1e-6
_NO_EXPIRY = os.environ.get("CTP_LINK_NO_EXPIRY") == "1"      # diagnostics only: ignore max aging
_NO_MINAGE = os.environ.get("CTP_LINK_NO_MINAGE") == "1"      # diagnostics only: ignore min aging
_KEEP_EXPIRED = os.environ.get("CTP_LINK_SCRAP_EXPIRED") != "1"  # v76: aging is a REPORT (config) -> past-shelf-life stock
                                                                   # is still drawn (after in-life stock) and labelled EXPIRED
TOL = pd.Timedelta(minutes=1)


def _hours(x):
    return pd.Timedelta(hours=float(x)) if pd.notna(x) else pd.Timedelta(0)


def _fmt(q):
    return f"{q:.3f}".rstrip("0").rstrip(".") if q < 100 else f"{q:.0f}"


class Ledger:
    """FEFO supply per item; entries are (usable, expiry, seq, id, qty)."""

    def __init__(self, ag):
        # v76 (2026-10-11): aging values carry a unit (CTP_Master_Aging MinAgingUnit / MaxAgingUnit: Hours / Days).
        # Reading them as hours made a 3-day shelf life 3 h -> opening stock and lots "expired" -> false SHORT links.
        def _unit(col):
            if col not in ag.columns:
                return pd.Series(1.0, index=ag.index)
            u = ag[col].astype(str).str.strip().str.upper()
            return u.map(lambda x: 24.0 if x.startswith("DAY") else 720.0 if x.startswith("MONTH")
                         else 1 / 60 if x.startswith("MIN") else 1.0)
        mn = pd.to_numeric(ag.MinAging, errors="coerce") * _unit("MinAgingUnit")
        mx = pd.to_numeric(ag.MaxAging, errors="coerce") * _unit("MaxAgingUnit")
        self.mn = {str(k).strip(): _hours(v) for k, v in zip(ag.ItemCode, mn)}
        self.mx = {str(k).strip(): _hours(v) for k, v in zip(ag.ItemCode, mx)}
        self.pending = defaultdict(list)   # heap by usable time -> not ready yet
        self.ready = defaultdict(list)     # heap by expiry -> FEFO
        self.seq = 0
        self.expired = defaultdict(float)
        self.old = defaultdict(list)       # v76: stock past max aging, kept (aging is a report) -> drawn last, labelled

    def add(self, item, made, sid, qty, delay=pd.Timedelta(0), minage=True):
        if qty <= EPS:
            return
        mx = self.mx.get(item, pd.Timedelta(0))
        exp = made + mx if mx > pd.Timedelta(0) else pd.Timestamp.max
        self.seq += 1
        mn = pd.Timedelta(0) if (_NO_MINAGE or not minage) else self.mn.get(item, pd.Timedelta(0))
        heapq.heappush(self.pending[item], (made + delay + mn, exp, self.seq, sid, [qty]))

    def draw(self, item, t, need):
        pend, rdy = self.pending[item], self.ready[item]
        while pend and pend[0][0] <= t + TOL:
            u, exp, s, sid, q = heapq.heappop(pend)
            heapq.heappush(rdy, (exp, u, s, sid, q))
        used = []
        while need > EPS and rdy:
            exp, u, s, sid, q = rdy[0]
            if exp < t and not _NO_EXPIRY:                # past max aging -> kept aside (aging is a report)
                heapq.heappop(rdy)
                if _KEEP_EXPIRED:
                    heapq.heappush(self.old[item], (u, exp, s, sid, q))
                else:
                    self.expired[item] += q[0]
                continue
            take = min(need, q[0])
            used.append((sid, take, u))
            q[0] -= take
            need -= take
            if q[0] <= EPS:
                heapq.heappop(rdy)
        old = self.old[item]                              # v76: only when no in-life stock is left -- oldest last
        while need > EPS and old:
            u, exp, s, sid, q = old[0]
            take = min(need, q[0])
            used.append(("EXPIRED:" + sid, take, u))
            self.expired[item] += take
            q[0] -= take
            need -= take
            if q[0] <= EPS:
                heapq.heappop(old)
        return used, need

    def has_any(self, item):
        return bool(self.pending.get(item) or self.ready.get(item))


def bom_ratios(bom):
    """Input per unit of a parent lot, in the lot's own UOM.
    BOM rows give per tyre: child_quantity of each child under Parent, and Parent_qty in Parent_unit.
    A lot planned in another unit (e.g. TEG-02 in M, Parent_unit KG) takes its per-tyre quantity in
    that unit from the BOM row where the parent itself is consumed (child == Parent, child_Unit).
    Returns (per-SKU {(sku, parent, uom): [(child, ratio)]}, generic {(parent, uom): [(child, median ratio)]})."""
    b = bom.copy()
    for c in ("Super_parent", "Parent", "child", "Parent_unit", "child_Unit"):
        b[c] = b[c].astype(str).str.strip()
    for c in ("Parent_qty", "child_quantity"):
        b[c] = pd.to_numeric(b[c], errors="coerce")
    b = b[b.child_quantity > 0]
    per = defaultdict(dict)                      # (sku, item) -> {unit: qty per tyre}
    for r in b.drop_duplicates(["Super_parent", "Parent", "Parent_unit"]).itertuples():
        if r.Parent_qty > 0:
            per[(r.Super_parent, r.Parent)].setdefault(r.Parent_unit.upper(), r.Parent_qty)
    for r in b.drop_duplicates(["Super_parent", "child", "child_Unit"]).itertuples():
        per[(r.Super_parent, r.child)].setdefault(r.child_Unit.upper(), r.child_quantity)
    sku = defaultdict(list)
    gen_vals = defaultdict(list)
    b = b[b.Parent != b.child]
    for r in b.drop_duplicates(["Super_parent", "Parent", "child"]).itertuples():
        for u, q in per[(r.Super_parent, r.Parent)].items():
            if q > 0:
                ratio = r.child_quantity / q
                sku[(r.Super_parent, r.Parent, u)].append((r.child, ratio))
                gen_vals[(r.Parent, u, r.child)].append(ratio)
    gen = defaultdict(list)
    for (P, u, C), v in gen_vals.items():
        gen[(P, u)].append((C, float(pd.Series(v).median())))
    return sku, gen


def build(fs, bom, inv, ag, cu=None, sku2gt=None, t0=None, lot_sku=None, gt_transfer_min=90.0, lot_inputs=None):
    """Return ({lot_id: consumption_id} for upstream+building lots, {cure row index: consumption_id},
    genealogy DataFrame, stats dict). lot_sku = {lot_id: FG sku} picks the SKU's own BOM ratios."""
    led = Ledger(ag)
    kids_sku, kids = bom_ratios(bom)
    lot_sku = lot_sku or {}
    tt = pd.Timedelta(minutes=float(gt_transfer_min or 0))
    inv = inv.copy()
    inv["itemcode"] = inv["itemcode"].astype(str).str.strip()
    inv["q"] = pd.to_numeric(inv["inventory"], errors="coerce").fillna(0)
    inv["t"] = pd.to_datetime(inv["produced_time"], errors="coerce", format="mixed").fillna(t0 or pd.Timestamp.min)
    inv = inv[(inv.q > 0) & (inv.unit.astype(str).str.upper() != "BATCHES")]
    for r in inv.itertuples():
        led.add(r.itemcode, r.t, f"OPENING_WIP:{r.production_id}", r.q)

    rows = fs.assign(item=fs["item"].astype(str).str.strip(),
                     q=pd.to_numeric(fs.produce_qty, errors="coerce").fillna(0),
                     bld=fs.department.astype(str).str.upper() == "BUILDING")
    for r in rows.itertuples():
        if r.bld and r.q >= 1 and r.end_time > r.start_time:
            n = int(round(r.q))                           # streamed: tyre j leaves the TBM at s + j*(f-s)/n
            step = (r.end_time - r.start_time) / n
            for j in range(1, n + 1):
                led.add(r.item, r.start_time + step * j, r.lot_id, 1.0, delay=tt, minage=False)
        else:
            led.add(r.item, r.end_time, r.lot_id, float(r.q), delay=tt if r.bld else pd.Timedelta(0))
    lots = rows.groupby("lot_id").agg(item=("item", "first"), uom=("UOM", "first"), dep=("department", "first"))
    produced = set(lots["item"]) | set(inv.itemcode)

    # demand events: (time, kind, key, part) -- kind 0 = a lot's shift row, 1 = one tyre of a cure block
    ev = [(r.start_time, 0, r.lot_id, r.q) for r in rows.itertuples() if r.q > 0]
    if cu is not None:
        for i, r in cu.iterrows():
            n = max(int(round(float(r.qty))), 0)
            step = (r.endTime - r.startTime) / n if n else pd.Timedelta(0)
            ev += [(r.startTime + step * k, 1, i, 1.0) for k in range(n)]
    ev.sort(key=lambda x: (x[0], x[1], str(x[2])))
    acc = defaultdict(lambda: defaultdict(float))     # consumer -> supply token -> qty
    short = defaultdict(lambda: defaultdict(float))
    raw = defaultdict(dict)
    first_t = {}
    gen = []
    st = defaultdict(int)
    for t, kind, key, part in ev:
        ck = (kind, key)
        first_t.setdefault(ck, t)
        if kind == 0:
            r = lots.loc[key]
            u = str(r.uom).strip().upper()
            kl = kids_sku.get((lot_sku.get(key), r["item"], u)) or kids.get((r["item"], u), [])
            if lot_inputs and key in lot_inputs:            # v76: the scheduler's own input per unit (phase-3 DAG)
                per_u = dict(kl)
                per_u.update(lot_inputs[key])
                kl = list(per_u.items())
            need_list = [(c, part * ratio) for c, ratio in kl]
            if not kl:
                raw[ck]["RAW_MATERIAL:NO_BOM_INPUTS"] = 1
        else:
            gt = (sku2gt or {}).get(str(cu.at[key, "skuCode"]).strip())
            if gt is None:
                raw[ck]["NO_GT_IN_BOM"] = 1
                continue
            need_list = [(gt, part)]
        for c, need in need_list:
            if c not in produced:
                raw[ck][f"RAW_MATERIAL:{c}"] = 1
                continue
            used, left = led.draw(c, t, need)
            for sid, q, usable in used:
                gen.append((key, kind, t, c, sid, q, usable))
                acc[ck][sid if sid.startswith("EXPIRED:") else ("OPENING_WIP:" + c) if sid.startswith("OPENING_WIP:") else sid] += q
            if left > max(EPS, 1e-4 * need):
                short[ck][c] += left
                gen.append((key, kind, t, c, "SHORT", left, pd.NaT))
    cmap, cure_map = {}, {}
    for ck in first_t:
        toks = [f"{sid}({_fmt(q)})" for sid, q in acc[ck].items()]
        toks += [f"SHORT:{c}({_fmt(q)})" for c, q in short[ck].items()]
        toks += list(raw[ck])
        (cure_map if ck[0] else cmap)[ck[1]] = ";".join(toks)
        st["links"] += len(acc[ck])
        st["raw"] += sum(1 for x in raw[ck] if x.startswith("RAW_MATERIAL:") and x != "RAW_MATERIAL:NO_BOM_INPUTS")
    st["expired_items"] = len(led.expired)
    g = pd.DataFrame(gen, columns=["consumer", "kind", "consumer_start", "input_item", "supply_id", "qty", "supply_usable"])
    g["ready_at_draw"] = g.supply_usable.isna() | (g.supply_usable <= g.consumer_start + TOL)
    # one genealogy row per (consumer, input, supply): first/last draw, latest unit usable, all units ready?
    g = g.groupby(["consumer", "kind", "input_item", "supply_id"], as_index=False, sort=False).agg(
        consumer_start=("consumer_start", "min"), last_draw=("consumer_start", "max"), qty=("qty", "sum"),
        supply_usable=("supply_usable", "max"), ready_at_draw=("ready_at_draw", "all"))
    g["supply_type"] = g.supply_id.map(lambda x: "SHORT" if x == "SHORT" else
                                       "EXPIRED_STOCK" if x.startswith("EXPIRED:") else
                                       "OPENING_WIP" if x.startswith("OPENING_WIP:") else "SCHEDULED_LOT")
    g["supply_id"] = g.supply_id.str.replace("EXPIRED:", "", regex=False).str.replace("OPENING_WIP:", "", regex=False)
    return cmap, cure_map, g, dict(st)
