#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_ctb_approximations.py -- expert APPROXIMATIONS for data the plant has not supplied yet
(2026-09-27), so the scheduler can run end-to-end. Every value is logged to
inputs/ctb_approximations_log.csv with its basis. Replace each with the plant value when it arrives.

Run AFTER adapt_inputs.py, apply_biascutter_calibration.py and apply_routing_gapfill.py.
"""
import os
import re

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
INP = os.path.join(HERE, "inputs")
norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()

# ---- machine rates / routes (item -> operation, department, machines, proc_time, uom, basis) ----
ROUTES = {
    "GL7":        ("FINAL MIXING", "FINAL MIXING", '"F270", "310"', 2.171, "MIN/BATCH",
                   "median of 48 PCR final compounds on F270/310 (DB routing); batch 215 kg"),
    "NYLON23-02": ("TBR DOUBLING", "TBR DOUBLING", '"2501","2502"', 22.0, "M/MIN",
                   "chafer doubling 2501/2502 has no rate anywhere -> chafer slitter 2401/2402 median (same fabric, preceding step)"),
    "NYDBCHF":    ("TBR DOUBLING", "TBR DOUBLING", '"2501","2502"', 22.0, "M/MIN",
                   "as NYLON23-02 (286 MES lots on doubling confirm the route)"),
    "SA15001":    ("BEAD APEXING", "BEAD APEXING", '"201", "202"', 15.38, "NOS/MIN",
                   "median of 75 PCR bead apex items on 201/202"),
    "SA17001":    ("BEAD APEXING", "BEAD APEXING", '"201", "202"', 15.38, "NOS/MIN",
                   "median of 75 PCR bead apex items on 201/202"),
    "SW15001":    ("BEAD WINDING PCR", "BEAD WINDING", '"1201"', 30.0, "NOS/MIN",
                   "median of 50 PCR wound beads on 1201"),
    "SPB02":      ("PLY CUTTER", "PLY CUTTER", '"1103", "1104"', 15.0, "CUTS/MIN",
                   "median of 124 PCR plies on 1103/1104"),
    "SPB03":      ("PLY CUTTER", "PLY CUTTER", '"1103", "1104"', 15.0, "CUTS/MIN",
                   "median of 124 PCR plies on 1103/1104"),
    "TBPGUM":     (None, None, None, 38.8, "M/MIN",
                   "12.5 cuts/min x 3.10 m per piece (TBPGUM is 3.10 m per body ply in the BOM) = 38.8 m/min"),
}
# TBR wound beads (TBWB*): no TBR bead-winding machine in any master; CTP Machines.xlsx shows the TBR TDA
# (2403/2404/2405) takes "Spool, Final batch" -> winding happens on the TDA. Rate: keep the routing value,
# TBWB008 (blank) = median of TBWB001-007 = 3.75 NOS/MIN.
TBWB_MACHINES = '"2403","2404","2405"'
# ---- outsourced (bought-in) compounds: not made in the plant -> purchased material, always available ----
OUTSOURCED = {"MTCM313", "TBA827", "TCA313"}
# ---- BOM quantities that are 0 in every BOM version: nearest sibling median (same parent family,
#      same child type), in the siblings' unit ----
BOM_FIX = {
    ("B4-TBLT006", "CP 200"): (0.3866, "M", "median calendered roll per TBR belt-4 (11 sibling belts)"),
    ("TBAPBD002", "S45"):     (1.596, "KG", "median final compound per TBR apexed bead (13 siblings)"),
    ("TBP013", "CWBP65"):     (1.2744, "M", "median calendered steel per TBR body ply (12 siblings)"),
    ("TBP021", "CWBP65"):     (1.2744, "M", "median calendered steel per TBR body ply (12 siblings)"),
}
AGING = {"AP0011T1": (1.0, 48.0, "plant spec 13: apex manual application 1 h / 2 days")}


def _cfg():
    try:
        import yaml
        return yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {}
    except Exception:
        return {}


def tbr_bead_one_step(log):
    """v74 (user 2026-10-11): the plant's TDA (2403/2404/2405) winds and apexes a TBR bead in ONE booking
    (tda_schedule.csv: every booking consumes bead wire + S54 together with the apex compounds). The wound
    bead TBWB* is therefore not a separate lot: its inputs (S54, bead wire) become direct inputs of the apexed
    bead TBAPBD* (quantity per bead unchanged), the TBWB routing rows are dropped and S54 feeds TBAPBD.
    The apexed-bead routing rate is kept (master data unchanged)."""
    bom = pd.read_csv(os.path.join(INP, "bom.csv"), dtype=str)
    sp, pa, ch = bom["Super_parent"].map(norm), bom["Parent"].map(norm), bom["child"].map(norm)
    pw = bom[ch.str.startswith("TBWB") & pa.str.startswith("TBAPBD")]
    link = {(norm(s), norm(c)): (norm(p), float(pq), float(q))
            for s, p, pq, c, q in zip(pw["Super_parent"], pw["Parent"], pw["Parent_qty"], pw["child"], pw["child_quantity"])}
    under = [link.get((s, p)) for s, p in zip(sp, pa)]
    n_moved = 0
    for i, (u, wq) in enumerate(zip(under, bom["Parent_qty"])):
        if u is None:
            continue
        p, ppq, qpw = u
        bom.iat[i, bom.columns.get_loc("child_quantity")] = repr(float(bom.iat[i, bom.columns.get_loc("child_quantity")]) * qpw / float(wq))
        bom.iat[i, bom.columns.get_loc("Parent_qty")] = repr(ppq)
        bom.iat[i, bom.columns.get_loc("Parent")] = p
        n_moved += 1
    bom = bom.drop(index=pw.index)
    bom.to_csv(os.path.join(INP, "bom.csv"), index=False)

    rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
    rp, fp = rt["routed_product"].map(norm), rt["finished_product"].map(norm)
    tb = rp.str.startswith("TBWB")
    succ = {(f, r): norm(s) for f, r, s in zip(fp[tb], rp[tb], rt.loc[tb, "successor_products"])}
    kids = {}                                       # TBWB -> its inputs (original BOM), for the TBAPBD text fields
    for p_, c_ in zip(pa, ch):
        if p_.startswith("TBWB"):
            kids.setdefault(p_, set()).add(c_)
    n_succ = 0
    for i in rt.index[~tb]:
        k = (fp[i], norm(rt.at[i, "successor_products"]))
        if k in succ:
            rt.at[i, "successor_products"] = succ[k]
            n_succ += 1
    ap = rp.str.startswith("TBAPBD")
    for i in rt.index[ap]:
        for col in ("input_components_from_bom", "flow_type", "predecessor_rule"):
            v = str(rt.at[i, col])
            for w, cs in kids.items():
                if w in v:
                    cs = sorted(cs)
                    if col == "input_components_from_bom":
                        v = v.replace(w, ",".join(cs))
                    elif col == "flow_type":
                        v = v.replace(f"{w} PREP", ",".join(f"{c} PREP" for c in cs))
                    else:
                        v = v.replace(f"{w} Master compound available", " / ".join(f"{c} Master compound available" for c in cs))
            rt.at[i, col] = v
    rt = rt[~tb]
    rt.to_csv(os.path.join(INP, "routing.csv"), index=False)
    log.append(("process", "TBWB* -> TBAPBD*", f"{len(pw)} BOM links removed, {n_moved} input rows moved to the apexed bead; "
                f"{int(tb.sum())} winding routing rows dropped, {n_succ} successors re-pointed",
                "v74 user 2026-10-11: plant TDA winds + apexes in one booking (tda_schedule.csv)"))


def inventory_alias(log, alias):
    """v77 (user 2026-10-11): opening-stock codes that differ from the BOM / scheduler code, keyed
    "<code>|<item_type>" -> scheduler code (e.g. cap strip booked as 'CAP 66' = 'CAP 66 - CAPSTRIP')."""
    for f in ("inventory.csv",):
        p = os.path.join(INP, f)
        if not os.path.exists(p):
            continue
        inv = pd.read_csv(p, dtype=str, low_memory=False)
        n = 0
        for k, new in alias.items():
            code, _, ityp = str(k).partition("|")
            m = inv["itemcode"].map(norm) == norm(code)
            if ityp:
                m &= inv["item_type"].map(norm).str.upper() == norm(ityp).upper()
            inv.loc[m, "itemcode"] = new
            n += int(m.sum())
            log.append(("alias", f"{code} ({ityp or 'any'})", f"-> {new}: {int(m.sum())} opening-stock rows",
                        "v77 user 2026-10-11: opening stock under a different code than the BOM"))
        inv.to_csv(p, index=False)


def ctb_wip_assumptions(log, c):
    """v80 (user 2026-10-12): the plan generator's own WIP assumptions (CTB_WIP_ASSUMPTIONS_v2.xlsx), applied to the
    opening stock so the scheduler starts from the same WIP picture the plan was made with:
      #11 stock past its max age is still usable -> expired opening lots get `expired_usable_h` of life from t0;
      #2  measured upstream flow (sheet '5 Upstream flow rates', tyres/day) with no lead time -> `flow_hours` of that
          flow is added as opening stock (one lot per item, usable at t0), qty = rate x BOM qty per tyre;
      #1  building components absent from the WIP feed are taken as available -> their codes (sheet '6b', SEMI-FINISHED,
          no compounds, no flow rate) are written to inputs/ctb_untracked_codes.csv for phase 5's first-hours assume."""
    import numpy as np
    t0 = pd.Timestamp(os.environ.get("CTP_PLAN_START") or "2026-10-10 07:00")
    xl = os.path.join(HERE, c.get("file", "CTB_WIP_ASSUMPTIONS_v2.xlsx"))
    inv = pd.read_csv(os.path.join(INP, "inventory.csv"), dtype=str, low_memory=False)
    ag = pd.read_csv(os.path.join(INP, "aging_master.csv"), dtype=str)
    U = {"DAYS": 24.0, "DAY": 24.0, "HOURS": 1.0, "HOUR": 1.0, "MONTH": 720.0}
    mx, mn = {}, {}
    for r in ag.itertuples():
        k = norm(r.ItemCode)
        try:
            mx[k] = float(r.MaxAging) * U.get(str(r.MaxAgingUnit).strip().upper(), 1.0)
            mn[k] = float(r.MinAging) * U.get(str(r.MinAgingUnit).strip().upper(), 1.0)
        except (TypeError, ValueError):
            pass
    # --- #11 expired opening stock usable for `expired_usable_h`
    eu = float(c.get("expired_usable_h", 24))
    pt = pd.to_datetime(inv["produced_time"], errors="coerce", format="mixed")
    code = inv["itemcode"].map(norm)
    age = (t0 - pt).dt.total_seconds() / 3600
    mxa = code.map(mx)
    exp = age > mxa
    newp = [t0 - pd.Timedelta(hours=max(m_ - eu, mn.get(k_, 0.0))) for m_, k_ in zip(mxa[exp], code[exp])]
    inv.loc[exp, "produced_time"] = [str(x) for x in newp]
    q = pd.to_numeric(inv["inventory"], errors="coerce").fillna(0)
    log.append(("ctb-wip", "#11 expired stock", f"{int(exp.sum())} lots / {q[exp].sum():,.0f} units usable {eu:g} h from t0",
                "CTB_WIP_ASSUMPTIONS_v2 #11: stock past max age is still listed / used by the plan"))
    # --- #2 one day of measured upstream flow as opening stock
    fl = pd.read_excel(xl, "5 Upstream flow rates")
    bom = pd.read_csv(os.path.join(INP, "bom.csv"), dtype=str, low_memory=False)
    bom = bom.apply(lambda s: s.str.strip() if s.dtype == object else s)
    bom["cq"] = pd.to_numeric(bom["child_quantity"], errors="coerce")
    per = (bom.drop_duplicates(["Super_parent", "Parent", "child"]).groupby(["Super_parent", "child"])["cq"].sum()
           .groupby(level=1).median())
    unit = bom.drop_duplicates("child").set_index("child")["child_Unit"].to_dict()
    it = pd.read_csv(os.path.join(INP, "itemtype_master.csv"), dtype=str)
    ity = dict(zip(it["ItemCode"].map(norm), it["ItemType"].astype(str).str.strip()))
    fh = float(c.get("flow_hours", 24))
    add, flow_items = [], set()
    for k, rate in fl.groupby(fl["Item"].map(norm))["Tyres/day"].max().items():
        if str(ity.get(k, "")).upper() == "CARCASS" or k not in per.index or not rate > 0:
            continue
        qty = float(rate) * float(per[k]) * fh / 24.0
        flow_items.add(k)
        add.append({"production_id": f"CTB_FLOW_{k}", "produced_time": str(t0 - pd.Timedelta(hours=mn.get(k, 0.0))),
                    "itemcode": k, "item_type": ity.get(k, ""), "unit": unit.get(k, ""), "inventory": round(qty, 3)})
    inv = pd.concat([inv, pd.DataFrame(add)], ignore_index=True)
    inv.to_csv(os.path.join(INP, "inventory.csv"), index=False)
    log.append(("ctb-wip", "#2 upstream flow", f"{len(add)} items: {fh:g} h of measured flow added as stock at t0 "
                f"({sum(a['inventory'] for a in add):,.0f} units)", "CTB_WIP_ASSUMPTIONS_v2 #2 / sheet 5 (SMART TBM consumption)"))
    # --- #1 building components absent from the feed -> assumed available in the first hours (phase 5)
    ms = pd.read_excel(xl, "6b Missing - full list")
    ms = ms[(ms["Class"].astype(str).str.upper() == "SEMI-FINISHED")
            & ~ms["Description / type"].astype(str).str.upper().isin(["MASTER COMPOUND", "FINAL COMPOUND"])]
    codes = sorted({norm(x) for x in ms["Component code"]} - flow_items)
    pd.DataFrame({"itemcode": codes}).to_csv(os.path.join(INP, "ctb_untracked_codes.csv"), index=False)
    log.append(("ctb-wip", "#1 untracked codes", f"{len(codes)} building components absent from the WIP feed -> "
                "assumed available for rows planned in the first hours", "CTB_WIP_ASSUMPTIONS_v2 #1 / sheet 6b"))


def relaxed_mixing(log, path):
    """EVALUATION ONLY (user 2026-10-11, v75_relaxed): mixing cycle time / batch weight per compound from the relaxed
    table (latest_schedule/plant-schedule/build_relaxed_mixing.py: plant 08-Oct values where more favourable)."""
    ov = pd.read_csv(os.path.join(HERE, path))
    ov = {norm(i): (c, k) for i, c, k in zip(ov["item"], ov["relaxed_cycle_s"], ov["relaxed_batch_kg"])}
    rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
    mix = rt["operation_name"].astype(str).str.upper().str.contains("MIXING")
    n = 0
    for i in rt.index[mix]:
        it = norm(rt.at[i, "routed_product"])
        if it not in ov:
            continue
        c, k = ov[it]
        f = 60.0 if str(rt.at[i, "proc_time_UOM"]).upper().startswith("MIN") else 1.0
        rt.at[i, "proc_time"] = repr(round(float(c) / f, 4))
        rt.at[i, "batch_size"] = repr(float(k))
        n += 1
    rt.to_csv(os.path.join(INP, "routing.csv"), index=False)
    log.append(("relaxed", "mixing", f"{len(ov)} compounds, {n} routing rows: cycle / batch kg from {path}",
                "EVALUATION v75_relaxed (user 2026-10-11): plant cycle times + batch weights where more favourable"))


def plant_mixing_cycles(log, c):
    """v91 (user 2026-10-13): plant cycle time for FINAL compounds (plant-approved: measured in the plant's own bookings
    04-11 Oct, build_plant_mixing_cycles.py). Per compound the measured pair on one of its routing mixers with the most
    intervals (>= min_intervals) sets proc_time, per kg of the routing batch (cycle x routing kg / plant kg). Faster AND
    slower values are both taken. Batch size, machines and every other routing value unchanged."""
    from machine_normalize import normalize_machine
    pc = pd.read_csv(os.path.join(HERE, c["file"]))
    pc = pc[pc["n_intervals"] >= int(c.get("min_intervals", 20))]
    best = {}
    for r in pc.itertuples():
        best.setdefault(norm(r.item), []).append((int(r.n_intervals), normalize_machine(str(r.mixer)), float(r.plant_cycle_s),
                                                  float(r.plant_batch_kg)))
    rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
    ops = {o.upper() for o in c.get("operations", ["FINAL MIXING"])}
    n_rows, items, faster, slower = 0, set(), 0, 0
    for i in rt.index[rt["operation_name"].astype(str).str.upper().str.strip().isin(ops)]:
        it = norm(rt.at[i, "routed_product"])
        if it not in best:
            continue
        ms = {normalize_machine(m) for m in re.findall(r'"+([^"]+)"+', str(rt.at[i, "machines"]))}
        cand = sorted([x for x in best[it] if x[1] in ms], reverse=True)
        if not cand:
            continue
        _, _, cyc, kg = cand[0]
        bs = pd.to_numeric(rt.at[i, "batch_size"], errors="coerce")
        if bs == bs and bs > 0 and kg > 0:
            cyc = cyc * float(bs) / kg
        u = str(rt.at[i, "proc_time_UOM"]).upper().strip()
        old = float(pd.to_numeric(rt.at[i, "proc_time"], errors="coerce"))
        new = cyc / 60.0 if u.startswith("MIN") else cyc
        if c.get("faster_only", False) and not new < old:
            continue                                     # v92: plant value only where it is faster than the routing
        if it not in items:
            faster += new < old
            slower += new > old
        rt.at[i, "proc_time"] = repr(round(new, 4))
        n_rows += 1
        items.add(it)
    rt.to_csv(os.path.join(INP, "routing.csv"), index=False)
    log.append(("plant-cycle", "FINAL MIXING", f"{len(items)} compounds ({faster} faster, {slower} slower{', faster only' if c.get('faster_only') else ''}), {n_rows} routing rows",
                f"v91 user 2026-10-13: plant-approved cycle (median of the plant's batch-to-batch time, {c['file']})"))


def final_on_master_mixers(log, pairs):
    """v91 (user 2026-10-13): final compounds the plant runs on a MASTER mixer (T638 on K430-2, 222 batches 04-11 Oct) may
    be planned there too: the mixer is added to the compound's FINAL MIXING routing rows. The 120 s clean-out before and
    after a final on K430-2 is applied by the mixing planner (co) and checked by mixing_rule_audit M4."""
    rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
    fm = rt["operation_name"].astype(str).str.upper().str.strip() == "FINAL MIXING"
    for item, mixers in (pairs or {}).items():
        # v93 (user 2026-10-13): key "*" = EVERY final compound may also run on these master mixers
        m = fm & ((rt["routed_product"].map(norm) == norm(item)) if item != "*" else True)
        for i in rt.index[m]:
            cur = str(rt.at[i, "machines"])
            for mx in mixers:
                if f'"{mx}"' not in cur:
                    cur = f'{cur}, "{mx}"'
            rt.at[i, "machines"] = cur
        log.append(("final-on-master", item, f"{', '.join(mixers)} added to {int(m.sum())} FINAL MIXING rows",
                    "v91 user 2026-10-13: the plant runs it there (04-11 Oct bookings); 120 s clean-out before / after"))
    rt.to_csv(os.path.join(INP, "routing.csv"), index=False)


def main():
    log = []
    rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
    rp = rt["routed_product"].map(norm)
    for it, (op, dept, mach, pt, uom, why) in ROUTES.items():
        m = rp == it
        if not m.any():
            continue
        if op:
            rt.loc[m, "operation_name"] = op
            rt.loc[m, "department"] = dept
            rt.loc[m, "machines"] = mach
        rt.loc[m, "proc_time"] = str(pt)
        rt.loc[m, "proc_time_UOM"] = uom
        if uom == "MIN/BATCH":
            rt.loc[m, "batch_size"] = "215.0"; rt.loc[m, "batch_UNIT"] = "KG"
        log.append(("rate", it, f"{pt} {uom} on {mach or 'routing machine'}", why))
    tb = rp.str.startswith("TBWB")
    rt.loc[tb, "machines"] = TBWB_MACHINES
    blank = tb & pd.to_numeric(rt["proc_time"], errors="coerce").isna()
    rt.loc[blank, "proc_time"] = "3.75"; rt.loc[blank, "proc_time_UOM"] = "NOS/MIN"
    log.append(("machine", "TBWB001-008", f"TDA {TBWB_MACHINES}; TBWB008 3.75 NOS/MIN",
                "no TBR bead-winding machine in any master; CTP Machines.xlsx: TDA takes spool+final batch"))
    # v97 (user 2026-10-13): more bought-in items from config (4RC: CWCH30256, CWBHI34; mixing: MS43, MS70, MB100)
    _xo = {norm(x) for x in ((_cfg().get("outsourced_items") or {}).get("items") or [])} \
        if (_cfg().get("outsourced_items") or {}).get("enabled") else set()
    _outs = OUTSOURCED | _xo
    out = rp.isin(_outs)
    for it in sorted(_outs):
        n_r = int((rp == it).sum())
        log.append(("outsourced", it, f"removed from routing ({n_r} rows) -> purchased, always available",
                    "user 2026-09-27: made outside (outside mixing)" if it in OUTSOURCED
                    else "v97 user 2026-10-13: procured from outside"))
    rt = rt[~out]                                   # no routing row -> treated as purchased (raw) material
    # v87 (user 2026-10-13): master compound stock assumed unlimited -> master compounds are treated like the
    # outsourced ones above: no routing row, so phase 1b skips them as raw material (always available, not made).
    if (_cfg().get("masters_unlimited") or {}).get("enabled"):
        mm = rt["operation_name"].astype(str).str.upper().str.strip() == "MASTER MIXING"
        n_it = rt.loc[mm, "routed_product"].map(norm).nunique()
        rt = rt[~mm]
        log.append(("masters", "MASTER MIXING", f"{int(mm.sum())} routing rows / {n_it} master compounds removed "
                    "-> always available, not mixed", "v87 user 2026-10-13: master compound inventory assumed all available"))
    # v102 (user 2026-10-13): machine output multiplier -- the machine makes k x its routing rate (1802 belt edge
    # filler: 2 strips at once, so a 6 m requirement takes the time of 3 m). Rate units (.../MIN) x k, time units
    # (MIN/..., SEC/...) / k. Quantities, BOM and lots unchanged -> every consumer still gets its full metres.
    for _mch, _k in ((_cfg().get("routing_output_multiplier") or {}).items()):
        _k = float(_k or 1.0)
        if _k == 1.0:
            continue
        _mm = rt["machines"].astype(str).str.replace('"', "").str.split(",").map(
            lambda l: str(_mch) in [s.strip() for s in l])
        _u = rt["proc_time_UOM"].astype(str).str.upper().str.strip()
        _pt = pd.to_numeric(rt["proc_time"], errors="coerce")
        _rate = _mm & _u.str.endswith("/MIN") & ~_u.str.startswith(("MIN", "SEC")) & _pt.notna()
        _time = _mm & _u.str.startswith(("MIN", "SEC")) & _pt.notna()
        rt.loc[_rate, "proc_time"] = (_pt[_rate] * _k).astype(str)
        rt.loc[_time, "proc_time"] = (_pt[_time] / _k).astype(str)
        for _it in sorted(set(rt.loc[_rate | _time, "routed_product"].map(norm))):
            log.append(("output_multiplier", _it, f"machine {_mch}: routing rate x{_k:g}",
                        "v102 user 2026-10-13: machine makes 2 strips at once (requirement / 2 in time)"))
    rt.to_csv(os.path.join(INP, "routing.csv"), index=False)

    bom = pd.read_csv(os.path.join(INP, "bom.csv"), dtype=str)
    for (p, c), (q, u, why) in BOM_FIX.items():
        m = (bom["Parent"].map(norm) == p) & (bom["child"].map(norm) == c)
        if m.any():
            bom.loc[m, "child_quantity"] = str(q)
            bom.loc[m, "child_Unit"] = u
            log.append(("bom", f"{p} -> {c}", f"{q} {u} per unit", why))
    bom.to_csv(os.path.join(INP, "bom.csv"), index=False)

    ag = pd.read_csv(os.path.join(INP, "aging_master.csv"), dtype=str)
    for it, (mn, mx, why) in AGING.items():
        m = ag["ItemCode"].map(norm) == it
        if m.any():
            ag.loc[m, ["MinAging", "MaxAging", "MinAgingUnit", "MaxAgingUnit"]] = [str(mn), str(mx), "Hours", "Hours"]
            log.append(("aging", it, f"{mn:g} / {mx:g} h", why))
    ag.to_csv(os.path.join(INP, "aging_master.csv"), index=False)

    if _cfg().get("tbr_bead_one_step", {}).get("enabled"):
        tbr_bead_one_step(log)
    ia = _cfg().get("inventory_item_alias") or {}
    if ia:
        inventory_alias(log, ia)
    cw = _cfg().get("ctb_wip_assumptions") or {}
    if cw.get("enabled"):
        ctb_wip_assumptions(log, cw)
    rm = _cfg().get("relaxed_mixing") or {}
    if rm.get("enabled"):
        relaxed_mixing(log, rm.get("file"))
    pmc = _cfg().get("plant_mixing_cycles") or {}
    if pmc.get("enabled"):
        plant_mixing_cycles(log, pmc)
    fom = _cfg().get("final_on_master_mixers") or {}
    if fom.get("enabled"):
        final_on_master_mixers(log, fom.get("pairs"))

    pd.DataFrame(log, columns=["kind", "item", "value_used", "basis"]).to_csv(
        os.path.join(INP, "ctb_approximations_log.csv"), index=False)
    for r in log:
        print(f"  [approx] {r[0]:<10} {r[1]:<22} {r[2]:<45} <- {r[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
