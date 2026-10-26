#!/usr/bin/env python3
"""phase2_wip_netting.py — net component demand against the per-batch WIP snapshot.

v10 phase 2 on CTP data. The two rules that took v10 the longest to get right
are kept exactly:

  MATURED?  tested at draw_at — the first consumer run for this item. Testing
            maturity against need_by charges min aging TWICE, because need_by is
            already build_start - min_aging.
  ALIVE?    tested at THIS demand's own need_by. Judging a batch once, early,
            and then letting it serve every later demand is what put 33.2% of
            BTP's draws past expiry.

THE OPENING CLOCK (v10 V7_WIP_OPENING_CLOCK, on here)
  MIN aging runs from the REAL produced_time — physics does not restart because
  a plan does. MAX aging runs from FLOOR OPEN — whatever is in date on the rack
  at the bell is material the floor can pick up, and it gets its full shelf life
  from that moment. A batch already dead at the bell never enters the pool.

CASCADE
  Covering an assembly with WIP also removes the need to make what went into it.
  The saving flows down real per-edge BOM quantities via out_frac, exactly as
  v10 does, which matters because the CTP BOM is path-explicit and an item can
  hang off several branches of one tyre.

TBR HAS NO opening_wip TABLE. --no-wip is then the honest run: every component
is made from scratch behind an opening runway. It is stated, never defaulted.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import pandas as pd

import blockers as BLK
import ctpio as io
import rules as R

ANCHOR_TYPES = {"green tyres", "carcass"}


# =====================================================================
#  UNIT RECONCILIATION - M <-> NOS, from the BOM and the routing
# =====================================================================
#  CTP_Opening_WIP and the BOM disagree on the unit for 152 items, in both
#  directions, and the disagreement is not an error - it is two true statements
#  about the same material:
#
#      a belt, a ply, a chipper  the floor stores the ROLL and counts METRES;
#                                the BOM consumes finished CUTS and counts NOS
#      a tread                   the floor stores CUT PIECES and counts NOS;
#                                the BOM consumes the LENGTH and counts M
#
#  Both readings need one number - THE METRES IN ONE PIECE - and the BOM states
#  it directly. A recipe row for a cut item is written for one piece of it, and
#  `Parent_qty` is how long that piece is:
#
#      Parent      Parent_qty  Parent_unit   child        child_qty  child_Unit
#      B1-PBLT001       1.662            M   CPJ120           0.202           M
#
#  and the tyre above consumes it as `1.0 NOS`. So one piece of B1-PBLT001 is
#  1.662 m. The same row for a tread reads 1.695 M and the tyre consumes
#  `1.695 M` - the length IS the piece.
#
#  THE FACTOR IS PER SKU, NOT PER CODE. The same ply code is cut to different
#  lengths for different tyres - PBP04 is 1.34 m under 1325214617107MSXT0 and
#  0.93 m under 1D25114012008QXPC0 - so a single figure per item code would be
#  wrong for one of them. Measured on this data, `Parent_qty` is single-valued
#  for every (Super_parent, Parent) pair: 310 pairs on PCR and 1,380 on TBR,
#  none ambiguous. The demand row already knows its SKU, so the lookup is exact.
#
#  THE ROUTING IS THE GUARD, NOT THE SOURCE. A conversion is only legitimate
#  where the item really is a continuous length cut into pieces, and the routing
#  says so in its own rate unit: M/MIN (made as length), CUTS/MIN or NOS/MIN
#  (made as pieces). An item whose routing states neither is NOT converted - the
#  blocker stands, because nothing in the database licenses the arithmetic.
#
#      BOM says NOS, stock says M   ->  pieces  = metres / metres_per_piece
#      BOM says M,   stock says NOS ->  metres  = pieces x metres_per_piece
#
#  Nothing is rounded here. A part-used roll stays a part-used roll.
# =====================================================================
CUT_RATES = {"CUTS/MIN", "NOS/MIN"}
LEN_RATES = {"M/MIN"}


def metres_per_piece(bom) -> dict:
    """(sku, item) -> the length of ONE piece, from the BOM's own recipe row."""
    m = bom[bom["Parent_unit"].map(io._s).str.upper() == "M"]
    out = {}
    for sp, par, q in zip(m["Super_parent"].map(io._s), m["Parent"].map(io._s),
                          pd.to_numeric(m["Parent_qty"], errors="coerce")):
        if sp and par and pd.notna(q) and q > 0:
            out[(sp, par)] = float(q)
    return out


def unit_bridge(wip, dem, rt, bom, extra_units: dict | None = None) -> tuple:
    """Everything needed to turn a demand quantity into the stock's own unit.

    Returns (factor_of, unconvertible) where
      factor_of(sku, item) -> multiply a BOM-unit quantity by this to get the
                              quantity in the WIP's unit, or None to refuse
      unconvertible        -> {item: reason} for the blocker register
    """
    mpp = metres_per_piece(bom)
    wu = {}
    for ic, u in zip(wip["itemcode"].map(io._s), wip["unit"].map(io._s)):
        u = u.strip().upper()
        if ic and u and u not in ("NAN", "NONE"):
            wu.setdefault(ic, u)
    du = {}
    for ic, u in zip(dem["item_code"].map(io._s), dem["uom"].map(io._s)):
        u = u.strip().upper()
        if ic and u:
            du.setdefault(ic, u)
    for ic, u in (extra_units or {}).items():
        if ic and u:
            du.setdefault(ic, str(u).strip().upper())
    rates = {}
    for rp, u in zip(rt.get("routed_product", []), rt.get("proc_time_UOM", [])):
        rp, u = io._s(rp), io._s(u).strip().upper()
        if rp and u:
            rates.setdefault(rp, set()).add(u)

    bad = {}

    def factor_of(sku, item):
        w, d = wu.get(item), du.get(item)
        if not w or not d or w == d:
            return 1.0
        r = rates.get(item, set())
        mp = mpp.get((io._s(sku), item))
        if not (r & (CUT_RATES | LEN_RATES)):
            bad[item] = (f"stock in {w}, BOM in {d}, and the routing states no "
                         f"M/MIN or CUTS/MIN rate - nothing licenses a conversion")
            return None
        if mp is None:
            bad[item] = (f"stock in {w}, BOM in {d}, but the BOM has no "
                         f"Parent_qty in M for this item under SKU {sku}")
            return None
        if d == "NOS" and w == "M":
            return mp                      # pieces -> metres
        if d == "M" and w == "NOS":
            return 1.0 / mp                # metres -> pieces
        bad[item] = f"stock in {w}, BOM in {d} - not an M/NOS pair"
        return None

    return factor_of, bad


def build_pool(wip: pd.DataFrame, aging: dict, opening: pd.Timestamp) -> dict:
    pool: dict[str, list[dict]] = defaultdict(list)
    n_dead = n_rows = 0
    dead_qty = 0.0

    def hrs(v, unit):
        v = pd.to_numeric(v, errors="coerce")
        if pd.isna(v):
            return None
        u = str(unit or "").strip().lower()
        return float(v) * 24.0 if u.startswith("day") else float(v)

    for i, r in enumerate(wip.itertuples(index=False)):
        item, q, t = r.itemcode, float(r.inventory), r.produced_time
        if not item or q <= 0 or pd.isna(t):
            continue
        n_rows += 1
        fb = aging.get(item, (0.0, 0.0))
        mn = hrs(getattr(r, "MinAging", None), getattr(r, "MinAgingUnit", "")) or fb[0]
        mx = hrs(getattr(r, "MaxAging", None), getattr(r, "MaxAgingUnit", "")) or fb[1]
        # v46a: stock converted from a bypassed cutter/slitter code carries the
        # CUT code's own rest as its transfer time (0 allowed) - the child's
        # master ageing (which already includes the bypass gap) must not apply.
        _bm = getattr(r, "bypass_min_h", None)
        if _bm is not None and pd.notna(_bm):
            mn = float(_bm)
        _src = getattr(r, "converted_from", "")
        _src = _src if isinstance(_src, str) else ""
        if mx and mx > 0:
            if t + pd.Timedelta(hours=mx) < opening:      # dead at the bell
                n_dead += 1
                dead_qty += q
                continue
            expiry = opening + pd.Timedelta(hours=mx)      # fresh clock from the bell
        else:
            expiry = pd.Timestamp.max
        pool[item].append({"ready": t + pd.Timedelta(hours=mn or 0.0),
                           "expiry": expiry, "qty": q, "opening": q,
                           "produced": t, "row": i + 2, "src": _src})
    for it in pool:
        pool[it].sort(key=lambda b: b["expiry"])          # FEFO
        for n, b in enumerate(pool[it], 1):
            b["batch_id"] = f"{it}#B{n:02d}" + (f"<CUT:{b['src']}" if b.get("src") else "")
    return pool, {"rows": n_rows, "dead_at_open": n_dead, "dead_qty": dead_qty}


def bypass_stock_to_children(wip: pd.DataFrame, bom: pd.DataFrame, itypes: dict,
                             aging: dict, bias: frozenset, line: str,
                             sched: set | None = None,
                             conv_bom: pd.DataFrame | None = None) -> tuple:
    """v46a: STOCK OF A BYPASSED CUTTER / SLITTER CODE IS STOCK OF ITS CHILDREN.

    The cutters and slitters are not scheduled (`bypass_departments`), so the
    material the plant already cut or slit is turned back into the BOM child it
    was cut from (calendered roll, extruded strip, ...) when the opening stock is
    read, and that child stock is netted like any other.

    Quantity: the BOM is per tyre absolute, so 1 unit of the cut code holds
    child_quantity / Parent_qty units of its child. A BIAS cutter item (BR-5)
    already states its children per unit of its own output, so there the ratio
    is child_quantity. A child the plant does not make here (another bypassed
    code, or a contracted pre-cut code) is walked through to the made item
    below it, multiplying the ratios.

    Time: the converted stock is usable at produced_time + the CUT code's own
    minimum ageing - the transfer time across the bypassed cutter / slitter -
    and keeps the cut code's maximum ageing.

    A row is kept unchanged (and left to `cold_start.net_cut_stock`) when its
    unit differs from the BOM's parent unit, or the BOM gives it no child.
    """
    byp = set(io.BYPASSED)
    sched = set(sched or ())
    if not byp or wip is None or wip.empty:
        return wip, pd.DataFrame()
    # Follow the engine's own BOM walk, SKU tree by SKU tree: from the cut code
    # go down through every child the plant does not make here (another
    # bypassed code, or a contracted / pre-cut code such as BCFT002) until a
    # MADE item is reached - that is where the stock lands. Ratios multiply
    # along the path. SKUs can reach different items from the same cut code;
    # the path most SKUs share is used (never several paths summed), with the
    # median ratio over those SKUs.
    def _ratios(bomdf):
        b = bomdf.copy()
        for _c in ("Super_parent", "Equipment", "Parent", "child"):
            b[_c] = b[_c].map(io._s)
        b = b[(b["child"] != "") & (b["child"] != b["Parent"])]
        pq = pd.to_numeric(b["Parent_qty"], errors="coerce")
        cq = pd.to_numeric(b["child_quantity"], errors="coerce")
        b["ratio"] = [c if p_ in bias else (c / q if q and q > 0 else float("nan"))
                      for p_, c, q in zip(b["Parent"], cq, pq)]
        b = b[b["ratio"].notna() & (b["ratio"] > 0)]
        tree = defaultdict(dict)
        for (sp, eqp, p_, c), g in b.groupby(["Super_parent", "Equipment", "Parent", "child"],
                                             sort=False):
            tree[(sp, eqp, p_)][c] = (float(g["ratio"].mean()), io._s(g["child_Unit"].iat[0]),
                                      io._s(g["Parent_unit"].iat[0]).upper())
        skus_of = defaultdict(set)
        for (sp, eqp, p_) in tree:
            if p_ in byp:
                skus_of[p_].add((sp, eqp))

        def walk(sk, code, mult, path, out, units, via):
            for c, (r, cu, _pu) in tree.get((sk[0], sk[1], code), {}).items():
                m = mult * r
                if ((c in byp or c not in sched) and (sk[0], sk[1], c) in tree
                        and c not in path and len(path) < 8):
                    walk(sk, c, m, path + (c,), out, units, via)
                else:
                    out[c] = out.get(c, 0.0) + m
                    units[c] = cu
                    via[c] = " > ".join(path + (c,))

        rat = {}
        for code, sks in skus_of.items():
            sigs, units, via = defaultdict(list), {}, {}
            for sk in sorted(sks):
                out = {}
                walk(sk, code, 1.0, (code,), out, units, via)
                if out:
                    sigs[tuple(sorted(out))].append(out)
            if not sigs:
                continue
            sig, maps = max(sigs.items(), key=lambda kv: (len(kv[1]), -len(kv[0])))
            sk0 = sorted(sks)[0]
            pu = next(iter(tree[(sk0[0], sk0[1], code)].values()))[2]
            rat[code] = [{"child": c,
                          "ratio_total": float(pd.Series([m[c] for m in maps]).median()),
                          "ratio_min": min(m[c] for m in maps), "ratio_max": max(m[c] for m in maps),
                          "bom_rows": len(maps), "skus_all": len(sks),
                          "other_paths": len(sigs) - 1,
                          "child_unit": units.get(c, ""), "parent_unit": pu,
                          "via": via.get(c, code)} for c in sig]
        return rat

    rat = _ratios(bom)
    for c in rat:
        for k in rat[c]:
            k["ratio_source"] = "run BOM"
    # v46c: a BOM that already skips the cutters (CTP_Master_BOM_v2) has no
    # cut-code rows, so the roll-per-metre ratio of cut stock is taken from
    # `wip.bypass_conversion_bom` (the staged pre-v2 BOM) - a CONVERSION
    # FACTOR ONLY, and only onto items the run's own BOM still contains.
    if conv_bom is not None and len(conv_bom):
        have = set(bom["Parent"].map(io._s)) | set(bom["child"].map(io._s))
        for c, ks in _ratios(conv_bom).items():
            if c in rat:
                continue
            ks = [dict(k, ratio_source="conversion BOM (assumed factor)") for k in ks
                  if k["child"] in have]
            if ks:
                rat[c] = ks

    def leaves(code):
        return rat.get(code, [])

    def hrs(v, unit):
        v = pd.to_numeric(v, errors="coerce")
        if pd.isna(v):
            return None
        return float(v) * 24.0 if str(unit or "").strip().lower().startswith("day") else float(v)

    keep, add, rep = [], [], []
    dept = dict(io.BYPASSED)
    for r in wip.itertuples(index=False):
        code = r.itemcode
        if code not in byp:
            keep.append(r._asdict())
            continue
        q = float(r.inventory or 0)
        wu = io._s(r.unit).upper()
        kids = leaves(code)
        base = {"line": line.upper(), "cut_code": code, "department": dept.get(code, ""),
                "production_id": getattr(r, "production_id", ""),
                "produced_time": r.produced_time, "cut_qty": q, "cut_unit": wu}
        if not kids:
            keep.append(r._asdict())
            rep.append(dict(base, status="KEPT - BOM gives this code no child"))
            continue
        pu = kids[0]["parent_unit"]
        if wu and pu and wu != pu:
            keep.append(r._asdict())
            rep.append(dict(base, status=f"KEPT - stock unit {wu} vs BOM unit {pu}"))
            continue
        # transfer time = the cut code's own minimum ageing (row first, master next)
        mn = hrs(getattr(r, "MinAging", None), getattr(r, "MinAgingUnit", ""))
        mn = mn if (mn is not None and mn > 0) else float(aging.get(code, (0.0, 0.0))[0] or 0.0)
        mx = hrs(getattr(r, "MaxAging", None), getattr(r, "MaxAgingUnit", ""))
        mx = mx if (mx is not None and mx > 0) else float(aging.get(code, (0.0, 0.0))[1] or 0.0)
        for k in kids:
            d = r._asdict()
            d.update({"itemcode": k["child"], "inventory": q * k["ratio_total"],
                      "unit": k["child_unit"],
                      "item_type": itypes.get(k["child"], d.get("item_type", "")),
                      "production_id": f"{d.get('production_id', '')}>{k['child']}",
                      "MinAging": mn, "MinAgingUnit": "Hours",
                      "MaxAging": mx if mx > 0 else None,
                      "MaxAgingUnit": "Hours",
                      "bypass_min_h": mn, "converted_from": code})
            add.append(d)
            rep.append(dict(base, status="CONVERTED", child=k["child"],
                            child_unit=k["child_unit"], via=k["via"],
                            ratio=round(k["ratio_total"], 6),
                            ratio_min=round(k["ratio_min"], 6),
                            ratio_max=round(k["ratio_max"], 6), skus_on_path=k["bom_rows"],
                            skus_with_code=k["skus_all"], other_paths=k["other_paths"],
                            ratio_source=k.get("ratio_source", "run BOM"),
                            bias_cutter=code in bias,
                            child_qty=round(q * k["ratio_total"], 3),
                            transfer_min_ageing_h=mn,
                            usable_from=r.produced_time + pd.Timedelta(hours=mn),
                            max_ageing_h=mx if mx > 0 else None))
    out = pd.DataFrame(keep + add)
    if "bypass_min_h" not in out.columns:
        out["bypass_min_h"] = float("nan")
    if "converted_from" not in out.columns:
        out["converted_from"] = ""
    out["converted_from"] = out["converted_from"].fillna("")
    return out, pd.DataFrame(rep)


def allocate(batches, need_qty, need_by, draw_at):
    """FEFO draw. Maturity at draw_at, expiry at this demand's own need_by."""
    taken, draws = 0.0, []
    for b in batches:
        if b["qty"] <= 0:
            continue
        if b["ready"] > draw_at:
            continue                                  # not yet matured
        if b["expiry"] < need_by:
            continue                                  # dead when this demand wants it
        use = min(b["qty"], need_qty - taken)
        b["qty"] -= use
        taken += use
        draws.append((b, use))
        if taken >= need_qty - 1e-9:
            break
    return taken, draws


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--no-wip", action="store_true")
    args = ap.parse_args()
    OUT = io.outdir(args.run, args.line)

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 2 — WIP netting (per-batch FEFO + exact cascade)   [{args.line.upper()}]")
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

    # ---- v42 COLD START 6a: STOCK ALREADY CUT IS STOCK ----------------------
    # Since the v33 bypass, cutter/slitter codes are not "made", so the BOM walk
    # skips straight past them and their stock on the rack was never netted
    # (about 40k PCR / 31k TBR units on 22 Oct). With the rule on, the NETTING
    # walk keeps the bypassed codes as nodes: a cut code's stock is drawn first
    # and the exact cascade then removes the matching demand from the
    # calendered / extruded material beneath it. Nothing is scheduled on a
    # cutter - the bypassed code has no demand row and no lot.
    NET_CUT = str(R.rule("cold_start.net_cut_stock", False)).lower() in (
        "1", "true", "on", "yes")
    walk_set = set(sched) | (set(io.BYPASSED) if NET_CUT else set())
    bypass_units = {}
    if NET_CUT:
        for c, u in zip(bom["child"].map(io._s), bom["child_Unit"].map(io._s)):
            if c in io.BYPASSED and u:
                bypass_units.setdefault(c, u)
        print(f"  Cut-stock netting   : ON - {len(io.BYPASSED):,} bypassed code(s) "
              f"netted against their own stock (cold_start.net_cut_stock)")

    lots = pd.read_csv(OUT / "phase1_building_lots.csv", parse_dates=["build_start"])
    dem = pd.read_csv(OUT / "phase1_demand.csv", parse_dates=["build_start", "need_by"])
    opening = lots["build_start"].min()

    _WIPT = io.wip_table()
    wip = None if args.no_wip else io.load_wip(args.line)
    if wip is None:
        pool, wstats = {}, {"rows": 0, "dead_at_open": 0, "dead_qty": 0.0}
        why = "--no-wip" if args.no_wip else f"NO opening_wip_{args.line} TABLE EXISTS"
        print(f"  WIP                 : IGNORED ({why}) — every component is made "
              f"from scratch behind the opening runway")
        print(f"  [GAP] this is the single largest assumption in the {args.line.upper()} run")
    else:
        _wip_types_raw = {str(x).strip().lower() for x in wip["item_type"].dropna().unique()}
        # ---- v46a: cut / slit stock becomes stock of the material it was cut from
        BYP_CONV = str(R.rule("wip.bypass_stock_to_children", False)).lower() in (
            "1", "true", "on", "yes")
        if BYP_CONV:
            _cb = R.rule("wip.bypass_conversion_bom")
            _cbom = None
            if _cb:
                _cbp = io.resolve_ref_dir(_cb) / f"jkt_bom_{args.line}.csv"
                _cbom = pd.read_csv(_cbp, dtype=str)
                print(f"  Cut-stock ratios    : from {_cbp.parent.name}/{_cbp.name} where the run's "
                      f"BOM has no cut-code rows (wip.bypass_conversion_bom - ASSUMED factor)")
            wip, _conv = bypass_stock_to_children(wip, bom, itypes, aging,
                                                  io.bias_items(rt), args.line, sched, _cbom)
            _conv.to_csv(OUT / "phase2_bypass_stock_conversion.csv", index=False)
            if len(_conv):
                _c = _conv[_conv["status"] == "CONVERTED"]
                _k = _conv[_conv["status"] != "CONVERTED"]
                print(f"  Cut stock -> child  : ON - {_c['production_id'].nunique():,} cut/slit "
                      f"batch(es) on {_c['cut_code'].nunique()} code(s) "
                      f"({_c.drop_duplicates('production_id')['cut_qty'].sum():,.0f} unit(s)) "
                      f"turned into {_c['child_qty'].sum():,.0f} unit(s) of "
                      f"{_c['child'].nunique()} child item(s); {len(_k)} batch(es) kept as cut "
                      f"code (unit / no child)   -> phase2_bypass_stock_conversion.csv")
                for _r in _k.itertuples(index=False):
                    BLK.add("phase2_wip", "UOM_MISMATCH", _WIPT, _r.cut_code,
                            f"bypassed cut/slit stock not converted to its child: {_r.status}",
                            line=args.line.upper(),
                            referenced_at="phase2_wip_netting.py bypass_stock_to_children",
                            qty=float(_r.cut_qty),
                            impact="left on the cut code (netted by cold_start.net_cut_stock "
                                   "if that rule is on)")
        pool, wstats = build_pool(wip, aging, opening)
        print(f"  WIP snapshot        : {wstats['rows']:,} batch row(s), "
              f"{len(pool):,} item(s) with stock")
        print(f"  Opening clock       : min aging from real production time, max aging "
              f"from the bell ({opening})")
        print(f"                        {wstats['dead_at_open']:,} batch(es) / "
              f"{wstats['dead_qty']:,.0f} unit(s) already dead at the bell — discarded")

        # ---- THE THREE WAYS REAL STOCK FAILS TO REACH THE NETTING PASS ----
        snap = io.WIP_SNAPSHOT.get(args.line) or {}
        # 1. the snapshot predates the plan it is being netted against
        sd = snap.get("inventory_date")
        if sd is not None and pd.notna(sd) and sd < opening.normalize():
            BLK.add("phase2_wip", "STALE", _WIPT,
                    f"inventory_date={pd.Timestamp(sd).date()}",
                    f"the staged snapshot is {(opening.normalize() - pd.Timestamp(sd)).days} "
                    f"day(s) older than the plan's first build "
                    f"({opening}); everything produced since is invisible",
                    line=args.line.upper(),
                    referenced_at="phase2_wip_netting.py build_pool",
                    qty=float(snap.get("qty") or 0.0),
                    impact="stock is understated - the run remakes material the "
                           "plant already holds")
        # 1b. the snapshot is the newest the table has, but the DAY it covers is
        # already behind - or its own sync sequence never finished. PPDS syncs
        # CTP_Opening_WIP at 06:30, 14:30 and 22:30; a day whose last sync is
        # 14:30 is missing a third of its own production.
        newest_sync = snap.get("last_sync")
        _pinned = bool(os.environ.get("CTP_WIP_ASOF", "").strip())
        today = pd.Timestamp.now().normalize()
        if sd is not None and pd.notna(sd) and pd.Timestamp(sd) < today:
            BLK.add("phase2_wip", "STALE", _WIPT,
                    f"inventory_date={pd.Timestamp(sd).date()}",
                    f"the newest snapshot the table holds is "
                    f"{pd.Timestamp(sd).date()}, but today is {today.date()}"
                    + (f"; the strike used is {newest_sync}, PINNED by "
                       f"CTP_WIP_ASOF - that is the floor as the plan's first "
                       f"shift opened, not a missed sync"
                       if _pinned else
                       (f"; its last sync was {newest_sync} of the 06:30 / 14:30 "
                        f"/ 22:30 sequence" if newest_sync is not None else "")),
                    line=args.line.upper(),
                    referenced_at="phase2_wip_netting.py build_pool",
                    qty=float(snap.get("qty") or 0.0),
                    impact="anything produced since is invisible to the netting pass")

        # 2. batches discarded as expired before the first demand
        if wstats["dead_at_open"]:
            BLK.add("phase2_wip", "DROPPED", _WIPT,
                    "(expired at the opening bell)",
                    f"{wstats['dead_at_open']:,} batch(es) had already passed "
                    f"produced_time + max_aging by {opening}",
                    line=args.line.upper(),
                    referenced_at="phase2_wip_netting.py build_pool",
                    qty=float(wstats["dead_qty"]),
                    impact="this stock is not available to any demand")
        # 3. AN ENTIRE ITEM TYPE THE PLAN NEEDS IS ABSENT FROM THE TABLE.
        # This is the loudest failure and the easiest to miss, because a type
        # with no rows looks exactly like a type with no stock. Measured on
        # JK_CTP_PPDS.CTP_Opening_WIP: it carries no MASTER COMPOUND and no
        # FINAL COMPOUND row on ANY date, while jkplanning_CTP.opening_wip
        # carries 18,515 of them. Every kilogram of compound is therefore mixed
        # from scratch, which is most of the upstream load on both lines.
        # types as the FEED holds them (before any v46a cut-stock conversion)
        have = _wip_types_raw
        want = (dem.groupby(dem["item_type"].astype(str).str.strip())["demand_qty"]
                   .sum().sort_values(ascending=False))
        for it, q in want.items():
            if not it or it.strip().lower() in have:
                continue
            if str(it).strip().lower() in ANCHOR_TYPES:
                continue                       # the built tyre itself, not stock
            BLK.add("phase2_wip", "MISSING_IN_DB", _WIPT,
                    it, "the table holds no row of this item type on any date, so "
                        "the plan treats the plant as holding none of it",
                    line=args.line.upper(),
                    referenced_at="phase2_wip_netting.py build_pool",
                    qty=float(q),
                    impact="every unit is made from scratch. If the plant does hold "
                           "stock of it, the feed for this type is missing from the "
                           "table the engine is instructed to read")

        # ---- ASSUMPTION A-WIP-1 : an absent item type is not a zero --------
        # A type with NO ROW AT ALL in the feed looks identical to a type the
        # plant genuinely holds none of, and the engine cannot tell them apart.
        # Treating it as zero asserts the floor was empty of it at 07:00, which
        # for something like TBR MASTER COMPOUND (2.39M units of demand, not one
        # row in the feed) is almost certainly the feed being incomplete rather
        # than the plant being empty.
        #
        # A-WIP-1 grants an absent type `assumptions.absent_type_cover_h` hours
        # of its OWN consumption rate as opening stock, matured and dated at the
        # bell. Bounded, uniform, and priced per type in the register. 0 = off,
        # which is exactly v28.
        _cov_h = float(R.rule("assumptions.absent_type_cover_h", 0) or 0)
        if _cov_h > 0:
            _span_h = max(
                (dem["build_start"].max() - opening).total_seconds() / 3600.0, 1.0)
            _granted = []
            for it, q in want.items():
                if not it or it.strip().lower() in have:
                    continue
                if str(it).strip().lower() in ANCHOR_TYPES:
                    continue
                items_of = sorted({c for c, ty in zip(
                    dem["item_code"].astype(str), dem["item_type"].astype(str))
                    if ty.strip() == it})
                if not items_of:
                    continue
                per_h = float(q) / _span_h
                grant_total = per_h * _cov_h
                # split across the type's codes in proportion to their demand
                dq = (dem[dem["item_type"].astype(str).str.strip() == it]
                      .groupby(dem["item_code"].astype(str))["demand_qty"].sum())
                tot = float(dq.sum()) or 1.0
                for code, cq in dq.items():
                    g = grant_total * float(cq) / tot
                    if g <= 0:
                        continue
                    pool.setdefault(code, []).append({
                        "ready": opening, "expiry": pd.Timestamp.max,
                        "qty": g, "opening": g, "produced": opening,
                        "row": -1, "batch_id": f"{code}#ASSUMED"})
                _granted.append((it, grant_total))
            if _granted:
                print(f"  [A-WIP-1]           : {len(_granted)} item type(s) absent "
                      f"from the feed granted {_cov_h:g} h of their own consumption "
                      f"as opening stock — "
                      f"{sum(g for _, g in _granted):,.0f} unit(s) ASSUMED")
                for it, g in sorted(_granted, key=lambda x: -x[1])[:6]:
                    print(f"      {str(it)[:30]:31s} {g:>14,.0f}")
                for it, g in _granted:
                    BLK.add("phase2_wip", "MISSING_IN_DB", _WIPT, it,
                            "the feed holds no row of this item type on any date",
                            line=args.line.upper(),
                            referenced_at="phase2_wip_netting.py A-WIP-1",
                            qty=g,
                            impact=f"ASSUMPTION A-WIP-1: {_cov_h:g} h of cover "
                                   f"({g:,.0f} unit(s)) assumed present at the bell, "
                                   f"instead of zero. Needs a real stock figure")

        # ---- ASSUMPTION A-WIP-2 : named codes covered for the first N hours ----
        # The plant states that cap strip and edge gum are on hand for the start
        # of the plan, but not for the whole horizon. A-WIP-2 grants each listed
        # code exactly the demand of the building lots that START within
        # `assumptions.first_hours_cover.hours` of the plan start, as matured
        # stock at the bell; everything after that must be made. Codes the plan
        # does not demand get nothing. Off when the list is empty.
        _fh = R.rule("assumptions.first_hours_cover") or {}
        _fh_h = float((_fh or {}).get("hours", 0) or 0)
        _fh_codes = {str(c).strip() for c in ((_fh or {}).get("codes") or [])}
        if _fh_h > 0 and _fh_codes:
            _cut = opening + pd.Timedelta(hours=_fh_h)
            _d = dem[dem["item_code"].astype(str).str.strip().isin(_fh_codes)
                     & (dem["build_start"] < _cut)]
            _g = _d.groupby(_d["item_code"].astype(str).str.strip())["demand_qty"].sum()
            for code, g in _g.items():
                if g <= 0:
                    continue
                pool.setdefault(code, []).append({
                    "ready": opening, "expiry": pd.Timestamp.max,
                    "qty": float(g), "opening": float(g), "produced": opening,
                    "row": -1, "batch_id": f"{code}#FIRST{_fh_h:g}H"})
                BLK.add("phase2_wip", "MISSING_IN_DB", _WIPT, code,
                        f"no stock row covers this code at the bell; stock "
                        f"assumed for building that starts before {_cut}",
                        line=args.line.upper(),
                        referenced_at="phase2_wip_netting.py A-WIP-2",
                        qty=float(g),
                        impact=f"ASSUMPTION A-WIP-2: first {_fh_h:g} h of demand "
                               f"({g:,.0f}) on hand at the bell; the rest is made")
            print(f"  [A-WIP-2]           : {len(_g)} code(s) covered for the first "
                  f"{_fh_h:g} h ({float(_g.sum()):,.0f} unit(s) ASSUMED); "
                  f"{len(_fh_codes - set(_g.index))} listed code(s) not needed in that window")

        # 4. a unit the BOM and the stock do not share AND cannot be bridged.
        # 4b. STOCK WITH NO UNIT AT ALL. Not a mismatch - an absence.
        # It is netted anyway, on the assumption the units agree, and
        # that assumption is written down rather than left implicit.
        du = {}
        for ic, u in zip(dem["item_code"].astype(str), dem["uom"].astype(str)):
            du.setdefault(ic, set()).add(u.strip().upper())
        seen = set()
        for ic, u, it, q in zip(wip["itemcode"].astype(str),
                                wip["unit"].astype(str),
                                wip["item_type"].astype(str),
                                wip["inventory"]):
            wu = u.strip().upper()
            if wu in ("", "NAN", "NONE"):
                wu = ""
            if ic not in du or ic in seen:
                continue
            if not wu:
                seen.add(ic)
                BLK.add("phase2_wip", "MISSING_IN_DB",
                        _WIPT, ic,
                        f"the stock row carries no `unit`, so it cannot be checked "
                        f"against the {'/'.join(sorted(x for x in du[ic] if x))} "
                        f"the BOM demands (item_type {it})",
                        line=args.line.upper(),
                        referenced_at="phase2_wip_netting.py allocate",
                        qty=float(wip.loc[wip["itemcode"].astype(str) == ic,
                                          "inventory"].sum()),
                        impact="netted anyway, on the assumption the units agree")
                continue
            # A GENUINE M/NOS DISAGREEMENT IS NO LONGER A BLOCKER HERE.
            # `unit_bridge` reconciles it at the draw, from the BOM's own
            # Parent_qty and the routing's rate unit, and only refuses where the
            # database licenses nothing - those refusals are registered after the
            # netting pass, where the refusal is actually known.

    # the unit bridge, built once from the BOM, the routing and the stock itself
    _rt_for_units = io.load_routing(args.line)
    if wip is not None:
        factor_of, _unconvertible = unit_bridge(wip, dem, _rt_for_units, bom,
                                                bypass_units)
    else:
        factor_of, _unconvertible = (lambda s, i: 1.0), {}
    CONVERSIONS: list = []

    gross = float(dem["demand_qty"].sum())
    print(f"  Demand rows (gross) : {len(dem):,}   qty {gross:,.0f}")

    need_by_map = {(r.building_lot_id, r.item_code): r.need_by
                   for r in dem.itertuples(index=False)}
    # the first consumer run for each item: the earliest build that wants it
    draw_at_item = dem.groupby("item_code")["build_start"].min().to_dict()

    # ---- v44b: "HAS IT RESTED?" IS ASKED FOR EACH DEMAND, NOT ONCE PER ITEM ----
    # Until v44a every demand for an item tested stock maturity against ONE time:
    # the first trolley that ever needs the item. Stock made in the hours before
    # the bell (e.g. 4,830 kg of TBR A1031 mixed 03:29-04:08, 4 h rest -> ready
    # 07:30-08:08) missed that first trolley (07:07) by minutes and was then
    # treated as unusable for the WHOLE plan - 31k PCR / 16k TBR units, mostly
    # final compound, were remade from scratch.
    # With `wip.maturity_per_demand`, a demand may use a batch that has rested
    # by that demand's own use time (need_by + the item's min aging = the time a
    # freshly made lot would have to be ready). The first-use time stays as the
    # floor, so the earliest demands are judged exactly as before; expiry is
    # unchanged (still this demand's own need_by).
    MAT_PER_DEMAND = str(R.rule("wip.maturity_per_demand", False)).lower() in (
        "1", "true", "on", "yes")

    def _draw_at(item, nb, L):
        da = draw_at_item.get(item, L.build_start)
        if not MAT_PER_DEMAND or nb is None or pd.isna(nb):
            return da
        use_t = nb + pd.Timedelta(hours=float(aging.get(item, (0.0, 0.0))[0] or 0.0))
        return max(da, use_t)

    if MAT_PER_DEMAND:
        print("  Maturity test   : v44b - per demand (stock rested by each demand's own "
              "use time), first-use time kept as the floor (wip.maturity_per_demand)")

    cache = io.WalkCache(child_idx, walk_set, io.bias_items(rt))
    wip_used_by: dict = {}
    req_by: dict = {}
    wip_used_item: dict = defaultdict(float)
    allocations: list[dict] = []

    # ---- v42 COLD START 6c: WHOLE KITS GET STOCK FIRST -----------------------
    # FEFO used to run trolley by trolley in plan order and component by
    # component, so opening stock was spread thin: tread for many trolleys,
    # bead for a few, and 0 of 217 day-1..3 kits came out complete. With the
    # rule on, trolleys starting inside `cold_start.kit_complete_window_h` whose
    # EVERY building input can be covered from mature, unexpired stock are
    # netted first; every other trolley follows in plan order and takes what
    # is left. Nothing is discarded - complete kits simply get first claim.
    _kf = R.rule("cold_start.kit_complete_fefo", False)
    if isinstance(_kf, dict):                 # per line, e.g. {pcr: true, tbr: false}
        _kf = _kf.get(args.line, False)
    KIT_FIRST = str(_kf).lower() in ("1", "true", "on", "yes")
    KIT_WIN_H = float(R.rule("cold_start.kit_complete_window_h", 72) or 72)

    def _kit_short(L):
        """(inputs that stock cannot cover, inputs) for one trolley's kit."""
        if (L.sku, eq) not in child_idx:
            return 99, 0
        nodes, edges, roots = cache.get(L.sku, eq, L.built_item, frozenset())
        short = n = 0
        for item, per in roots.items():
            if per <= 0 or str(itypes.get(item, "")).strip().lower() in ANCHOR_TYPES:
                continue
            n += 1
            batches = pool.get(item) or []
            fac = factor_of(L.sku, item) if batches else 1.0
            if not batches or fac is None:
                short += 1
                continue
            nb = need_by_map.get((L.building_lot_id, item), L.build_start)
            da = _draw_at(item, nb, L)
            have = sum(b["qty"] for b in batches
                       if b["qty"] > 0 and b["ready"] <= da and b["expiry"] >= nb)
            if have + 1e-9 < per * float(L.qty) * fac:
                short += 1
        return short, n

    def _net_lot(L):
        if (L.sku, eq) not in child_idx:
            return
        nodes, edges, roots = cache.get(L.sku, eq, L.built_item, frozenset())
        if not nodes:
            return
        qty = float(L.qty)
        incoming = defaultdict(list)
        for (p, c), q in edges.items():
            incoming[c].append((p, q))
        full = {i: nodes[i]["per_tyre"] * qty for i in nodes}
        req, out_frac = {}, {}
        for item in io.topo_order(nodes, edges):
            base = roots.get(item, 0.0) * qty
            for p, q in incoming.get(item, ()):
                base += q * qty * out_frac.get(p, 1.0)
            req[item] = base
            req_by[(L.building_lot_id, item)] = base
            if base <= 0:
                out_frac[item] = 0.0
                continue
            if str(itypes.get(item, "")).strip().lower() in ANCHOR_TYPES:
                out_frac[item] = 1.0
                continue
            nb = need_by_map.get((L.building_lot_id, item), L.build_start)
            batches = pool.get(item)
            # THE DRAW HAPPENS IN THE STOCK'S UNIT, then comes back in the BOM's.
            # `base` is what the tyre needs, in the BOM's unit; the pool counts
            # what the floor holds, in the floor's unit.
            fac = factor_of(L.sku, item) if batches else 1.0
            if batches and fac is None:
                used, draws = 0.0, []            # refused - blocker already logged
            elif batches:
                used_w, draws = allocate(batches, base * fac, nb,
                                         _draw_at(item, nb, L))
                used = used_w / fac
                if fac != 1.0 and used_w:
                    CONVERSIONS.append({
                        "building_lot_id": L.building_lot_id, "sku": L.sku,
                        "item_code": item, "item_type": itypes.get(item, ""),
                        "needed_bom_units": round(base, 3),
                        "needed_in_stock_units": round(base * fac, 3),
                        "factor_bom_to_stock": round(fac, 6),
                        "metres_per_piece": round(fac if fac > 1 else 1.0 / fac, 4),
                        "drawn_stock_units": round(used_w, 3),
                        "drawn_bom_units": round(used, 3)})
            else:
                used, draws = 0.0, []
            if used:
                wip_used_by[(L.building_lot_id, item)] = used
                wip_used_item[item] += used
                for b, q in draws:
                    allocations.append({
                        "building_lot_id": L.building_lot_id, "sku": L.sku,
                        "item_code": item, "item_type": itypes.get(item, ""),
                        "batch_id": b["batch_id"], "produced_time": b["produced"],
                        "usable_from": b["ready"],
                        "expires": b["expiry"] if b["expiry"] != pd.Timestamp.max else pd.NaT,
                        "needed_by": nb, "qty_used": round(q, 3)})
            out_frac[item] = 0.0 if full[item] <= 0 else (base - used) / full[item]

    _order = list(lots.sort_values("build_start", kind="stable").itertuples(index=False))
    if KIT_FIRST:
        # "Most complete first": inside the window, trolleys are netted in order
        # of how many building inputs stock CANNOT cover (0 first, then 1, ...),
        # plan order breaking ties; trolleys missing more than
        # `cold_start.kit_max_missing` inputs, and all later trolleys, follow in
        # plan order. Every kit needs at least one freshly made input on
        # 22 Oct, so a strict "whole kit from stock" test selects nobody.
        _cut = opening + pd.Timedelta(hours=KIT_WIN_H)
        _maxm = int(R.rule("cold_start.kit_max_missing", 1) or 0)
        _sh = {L.building_lot_id: _kit_short(L) for L in _order if L.build_start < _cut}
        _dist = pd.Series([v[0] for v in _sh.values()]).value_counts().sort_index()
        _first = sorted([L for L in _order if L.building_lot_id in _sh
                         and _sh[L.building_lot_id][0] <= _maxm],
                        key=lambda L: (_sh[L.building_lot_id][0], L.build_start))
        _fk = {L.building_lot_id for L in _first}
        _order = _first + [L for L in _order if L.building_lot_id not in _fk]
        print(f"  Kit-first FEFO      : ON - {len(_first):,} of {len(_sh):,} trolley(s) in "
              f"the first {KIT_WIN_H:g} h miss <= {_maxm} input(s) from stock and are "
              f"netted first; inputs missing per kit: "
              f"{ {int(k): int(v) for k, v in _dist.items()} }")
    for L in _order:
        _net_lot(L)

    lot_item = list(zip(dem["building_lot_id"], dem["item_code"]))
    dem["required_qty"] = [req_by.get(k, g) for k, g in zip(lot_item, dem["demand_qty"])]
    dem["wip_used"] = [wip_used_by.get(k, 0.0) for k in lot_item]
    dem["cascade_saved"] = (dem["demand_qty"] - dem["required_qty"]).clip(lower=0.0)
    dem["net_qty"] = (dem["required_qty"] - dem["wip_used"]).clip(lower=0.0)

    keep = dem[dem["net_qty"] > 1e-9].copy()
    net = float(keep["net_qty"].sum())
    print(f"\n  Net demand rows     : {len(keep):,}   qty {net:,.0f}")
    print(f"  Saved directly (WIP): {dem['wip_used'].sum():,.0f}")
    print(f"  Saved by cascade    : {dem['cascade_saved'].sum():,.0f}")
    print(f"  Total covered       : {gross-net:,.0f}  "
          f"({100*(gross-net)/max(gross,1):.1f}% of gross)")

    alloc = pd.DataFrame(allocations)
    if not alloc.empty:
        alloc["shelf_life_left_h"] = ((alloc["expires"] - alloc["needed_by"])
                                      .dt.total_seconds() / 3600).round(2)
        print(f"  Draws on stock      : {len(alloc):,} on "
              f"{alloc['batch_id'].nunique():,} batch(es); "
              f"{int((alloc['shelf_life_left_h'] < 0).sum())} past expiry (must be 0)")
        print(f"\n  Top items netted from WIP:")
        t = (alloc.groupby(["item_code", "item_type"])["qty_used"].sum()
             .sort_values(ascending=False).head(10))
        for (i, ty), q in t.items():
            print(f"    {str(i)[:24]:25s}{str(ty)[:24]:25s}{q:>16,.0f}")
    alloc.to_csv(OUT / "phase2_wip_allocations.csv", index=False)
    if NET_CUT:
        _bw = (alloc[alloc["item_code"].isin(set(io.BYPASSED))] if not alloc.empty
               else pd.DataFrame(columns=["item_code", "item_type", "qty_used",
                                          "building_lot_id"]))
        _bw.to_csv(OUT / "phase2_bypass_wip.csv", index=False)
        _tot = {}
        if wip is not None:
            _w = wip[wip["itemcode"].map(io._s).isin(set(io.BYPASSED))]
            _tot = {"codes_with_stock": int(_w["itemcode"].nunique()),
                    "stock_units": float(_w["inventory"].sum())}
        print(f"  Cut stock netted    : {float(_bw['qty_used'].sum()) if len(_bw) else 0:,.0f} "
              f"unit(s) on {(_bw['item_code'].nunique() if len(_bw) else 0)} cut code(s), "
              f"{(_bw['building_lot_id'].nunique() if len(_bw) else 0)} trolley(s); "
              f"stock on bypassed codes {_tot.get('stock_units', 0):,.0f} on "
              f"{_tot.get('codes_with_stock', 0)} code(s)   -> phase2_bypass_wip.csv")
    # ---- v34/v46a: the transfer time across every bypassed cutter / slitter ----
    # For each item made just below a bypassed code, the min ageing of the
    # bypassed code(s) above it is ADDED to its own min ageing (ctpio
    # add_bypass_transfer). Written out so the rule can be checked per item.
    _bpar = io._bypass_parents()
    _trows = []
    for _c, _ps in sorted(_bpar.items()):
        if _c in io.BYPASSED or _c not in itypes:
            continue
        _g = float(io.BYPASS_GAP.get(_c, 0.0))
        _eff = float(aging.get(_c, (0.0, 0.0))[0] or 0.0)
        for _p in sorted(_ps):
            _trows.append({"line": args.line.upper(), "child_item": _c,
                           "item_type": itypes.get(_c, ""),
                           "bypassed_parent": _p, "parent_department": io.BYPASSED.get(_p, ""),
                           "parent_min_ageing_h": float(aging.get(_p, (0.0, 0.0))[0] or 0.0),
                           "child_own_min_ageing_h": round(_eff - _g, 3),
                           "transfer_added_h": _g, "child_effective_min_ageing_h": _eff,
                           "rule_on": bool(R.rule("bypass_transfer_from_min_aging", False))})
    pd.DataFrame(_trows).to_csv(OUT / "phase2_bypass_transfer.csv", index=False)
    print(f"  Bypass transfer     : {len({r['child_item'] for r in _trows}):,} child item(s) "
          f"below a bypassed cutter/slitter, {sum(1 for c, g in io.BYPASS_GAP.items() if g > 0 and c in itypes):,} "
          f"with a transfer time > 0 (= min ageing of the bypassed code)   -> phase2_bypass_transfer.csv")
    keep.to_csv(OUT / "phase2_demand_net.csv", index=False)
    io.write_json(OUT / "phase2_stats.json", {
        "gross": gross, "net": net, "wip_rows": wstats["rows"],
        "dead_at_open": wstats["dead_at_open"], "no_wip": bool(wip is None),
        "covered_pct": 100 * (gross - net) / max(gross, 1)})
    # ---- units the database could NOT license a conversion for ------------
    # `_unconvertible` is filled lazily, as the netting pass meets each item, so
    # it is only complete once that pass is over. Reported here rather than
    # guessed at earlier.
    if wip is not None:
        for _ic, _why in sorted(_unconvertible.items()):
            BLK.add("phase2_wip", "UOM_MISMATCH", _WIPT,
                    _ic, _why, line=args.line.upper(),
                    referenced_at="phase2_wip_netting.py unit_bridge",
                    qty=float(wip.loc[wip["itemcode"].map(io._s) == _ic,
                                      "inventory"].sum()),
                    impact="stock left out of the netting pass; the item is made "
                           "from scratch")

    if CONVERSIONS:
        cv = pd.DataFrame(CONVERSIONS)
        cv.to_csv(OUT / "UOM_CONVERSIONS.csv", index=False, encoding="utf-8-sig")
        n_it = cv["item_code"].nunique()
        print(f"  Unit bridge     : {n_it} item(s) netted across an M/NOS boundary, "
              f"{len(cv):,} draw(s)   -> UOM_CONVERSIONS.csv")
        for it, g in list(cv.groupby("item_type"))[:6]:
            print(f"      {str(it)[:24]:25s} {g['item_code'].nunique():>3d} item(s)  "
                  f"drew {g['drawn_bom_units'].sum():>12,.1f} (BOM units)")
    BLK.write(OUT)
    print(f"\n[OK] phase2_demand_net.csv ({len(keep):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
