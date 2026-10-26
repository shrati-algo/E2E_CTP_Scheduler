#!/usr/bin/env python3
"""mixing_compliance.py - every MIXING_RULES_README.md rule, measured FROM the
written schedule (outputs/<run>/{pcr,tbr}/phase5_schedule.csv).

    python mixing_compliance.py --run e2e_v36

Writes outputs/<run>/MIXING_RULE_COMPLIANCE.csv (one row per rule: followed,
occurrences, %, detail) and MIXING_RULE_EVIDENCE.csv (the lots behind every
failure). Nothing is recomputed from the planner's own state - a rule the
schedule does not show being followed is reported as not followed.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import pandas as pd

import ctpio as io
import rules as R
import mix_planner as MP


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--line", default="")      # accepted for run_all symmetry
    a = ap.parse_args()
    OUT = io.OUT_ROOT / a.run
    cfg = R.rule("mix_planner") or {}

    S = []
    meta, itypes = {}, {}
    for ln in ("pcr", "tbr"):
        f = OUT / ln / "phase5_schedule.csv"
        if not f.exists():
            continue
        d = pd.read_csv(f, parse_dates=["start", "finish"])
        d["line"] = ln
        S.append(d)
        mt = io.build_routing_meta(io.load_routing(ln), io.machine_alias(io.load_machines()))
        for k, v in mt.items():
            meta.setdefault(k, v)
        b = io.load_bom(ln)
        itypes.update(io.load_itemtypes(b))
    S = pd.concat(S, ignore_index=True)
    S["machine"] = S["machine"].astype(str)
    S["item_type"] = S["item_type"].fillna(S["item_code"].map(itypes))
    mx = S[S["department"].astype(str).str.upper().str.contains("MIXING")
           & S["start"].notna()].copy().sort_values(["machine", "start"])
    U = lambda x: str(x or "").strip().upper()

    planner = MP.MixPlanner(cfg, {}, {
        "itype_of": lambda it: itypes.get(it, "") or
        str(mx.loc[mx["item_code"] == it, "item_type"].head(1).squeeze() or ""),
        "fourrc_compounds": []})
    rows, ev = [], []

    def add(rule, code, ok, n, detail="", bad=None):
        rows.append({"rule": rule, "code": code, "followed": int(ok), "occurrences": int(n),
                     "pct": round(100.0 * ok / n, 1) if n else None, "detail": detail})
        if bad is not None and len(bad):
            b = bad.copy()
            b.insert(0, "rule", rule)
            ev.append(b)

    cols = ["line", "lot_id", "item_code", "item_type", "machine", "machine_name",
            "lot_qty", "batches", "start", "finish", "mix_plan"]
    cols = [c for c in cols if c in mx.columns]

    # ---- s.3 lots -------------------------------------------------------
    bs = mx["item_code"].map(lambda i: (meta.get(i) or {}).get("batch_size"))
    nb = mx["lot_qty"] / bs
    whole = (nb - nb.round()).abs() < 1e-6
    add("Output in whole batches", "BR-V1", int(whole.sum()), int(bs.notna().sum()),
        "lot kg / routing batch_size is an integer", mx[bs.notna() & ~whole][cols])
    mn = int((R.engine("mix_mpq_batches", 5)))
    sticky = {U(x) for x in (R.rule("mixing.sticky_compounds") or [])}
    real = mx[mx.get("mix_plan", "") != "prep"]
    big = (real["batches"] >= mn) | real["item_code"].map(U).isin(sticky)
    add(f"Lot >= {mn} batches", "BRM-01", int(big.sum()), len(real),
        "sticky compounds are exempt (capped at 3)", real[~big][cols])
    st = mx[mx["item_code"].map(U).isin(sticky)]
    add("Sticky stack <= 3 batches", "BRM-02", int((st["batches"] <= 3).sum()), len(st),
        ", ".join(sorted(sticky)), st[st["batches"] > 3][cols])

    # ---- s.2 mixers ---------------------------------------------------------
    masters = {m for v in meta.values() if U(v.get("department")) == "MASTER MIXING"
               for m in v.get("machines", [])}
    allowed = []
    for r in mx.itertuples(index=False):
        ms = set((meta.get(r.item_code) or {}).get("machines", []))
        if "FINAL" in U(r.item_type) and io.final_on_master_mixers():
            ms |= masters
        allowed.append(r.machine in ms if ms else True)
    allowed = pd.Series(allowed, index=mx.index)
    add("Allowed mixer (routing + finals on master mixers)", "BRM-11",
        int(allowed.sum()), len(mx), "", mx[~allowed][cols])
    bought = {U(x) for x in (R.rule("purchased_codes") or [])}
    mixed_bought = mx[mx["item_code"].map(U).isin(bought)]
    add("Bought-in compounds never mixed", "s.2", int(len(mixed_bought) == 0), 1,
        f"{len(mixed_bought)} lot(s) of {', '.join(sorted(bought))}", mixed_bought[cols])

    # ---- s.4 one mixer per day, repeat gap ---------------------------------
    mx["pday"] = mx["start"].map(io.plant_day)
    per = mx.groupby(["item_code", "pday"])["machine"].nunique()
    add("One mixer per compound per plant day", "s.4", int((per == 1).sum()), len(per),
        "", None)
    # runs = consecutive lots of one compound on one mixer, back to back
    runs = []
    for m, g in mx.groupby("machine"):
        g = g.sort_values("start")
        cur = None
        for r in g.itertuples(index=False):
            if cur and cur["item"] == r.item_code and r.start <= cur["finish"] + pd.Timedelta(minutes=1):
                cur["finish"] = r.finish
                cur["n"] += 1
            else:
                if cur:
                    runs.append(cur)
                cur = {"machine": m, "item": r.item_code, "start": r.start,
                       "finish": r.finish, "n": 1, "type": r.item_type}
        if cur:
            runs.append(cur)
    RU = pd.DataFrame(runs)
    exempt = {U(x) for x in (cfg.get("gap_exempt") or [])}
    long_items = {U(x) for x in (cfg.get("long_items") or [])}
    ok = n = 0
    badgap = []
    for it, g in RU.sort_values("start").groupby("item"):
        if U(it) in exempt:
            continue
        gap = pd.Timedelta(hours=float(cfg.get("gap_h_long" if U(it) in long_items else "gap_h", 15)))
        st_ = list(g["start"])
        for x, y in zip(st_, st_[1:]):
            n += 1
            if y - x >= gap - pd.Timedelta(seconds=1):
                ok += 1
            else:
                badgap.append({"item_code": it, "run_start": x, "next_run_start": y,
                               "gap_h": round((y - x).total_seconds() / 3600, 2)})
    add("15 h / 24 h repeat gap between runs", "s.4", ok, n,
        "4RC feeders use the planner's 24 h only where also in long_items",
        pd.DataFrame(badgap))
    # BRM-09: split runs of a compound inside one shift
    RU["shift"] = RU["start"].map(lambda t: (io.plant_day(t), io.shift_of(t)))
    sp = RU.groupby(["item", "shift"]).size()
    add("No split runs of a compound within a shift", "BRM-09", int((sp == 1).sum()), len(sp))

    # ---- s.5 changeovers / clean-outs --------------------------------------
    need_ok = need_n = 0
    tr = [0, 0]; bu = [0, 0]; k4 = [0, 0]; m9 = [0, 0]
    k4m = str(cfg.get("k430_2_machine", "106"))
    badco = []
    butyl = {U(x) for x in (cfg.get("butyl_compounds") or [])}
    friendly = {U(x) for x in (cfg.get("friendly_compounds") or [])}
    for m, g in mx.groupby("machine"):
        g = g.sort_values("start").reset_index(drop=True)
        for i in range(1, len(g)):
            a_, b_ = g.loc[i - 1], g.loc[i]
            if U(a_.item_code) == U(b_.item_code):
                continue
            req = planner.co_s_of(m, a_.item_code, b_.item_code) - 1.0
            got = (b_.start - a_.finish).total_seconds()
            need_n += 1
            hit = got + 0.5 >= req
            need_ok += hit
            if not hit:
                badco.append({"machine": m, "from": a_.item_code, "to": b_.item_code,
                              "required_s": req, "actual_s": got})
            af, bf = "FINAL" in U(a_.item_type), "FINAL" in U(b_.item_type)
            am, bm = "MASTER" in U(a_.item_type), "MASTER" in U(b_.item_type)
            if (am and bf) or (af and bm):
                tr[1] += 1; tr[0] += got + 0.5 >= 360
            if U(a_.item_code) in butyl and U(b_.item_code) not in butyl \
                    and U(b_.item_code) not in friendly:
                bu[1] += 1; bu[0] += got + 0.5 >= 120
            if m == k4m and (af or bf):
                k4[1] += 1; k4[0] += got + 0.5 >= 120
            if af:
                m9[1] += 1; m9[0] += got + 0.5 >= 120
    add("Changeover / clean-out >= the largest applicable time", "s.5.1",
        need_ok, need_n, "", pd.DataFrame(badco))
    add("TR100 master <-> final (360 s)", "BRM-06", tr[0], tr[1])
    add("120 s clean-out after butyl", "BRM-03", bu[0], bu[1])
    add("K430-2 final 120 s", "M4", k4[0], k4[1])
    add("120 s after every final", "M9", m9[0], m9[1])

    # ---- s.5.2 butyl blocks -------------------------------------------------
    camps = [{U(x) for x in c} for c in (cfg.get("butyl_campaigns") or [])]
    fb = [0, 0]; g30 = [0, 0]
    for m, g in RU.sort_values("start").groupby("machine"):
        g = g.reset_index(drop=True)
        for i in range(len(g)):
            it = U(g.loc[i, "item"])
            if it not in butyl:
                continue
            prev = U(g.loc[i - 1, "item"]) if i else ""
            same_camp = any(it in c and prev in c for c in camps)
            if same_camp or (prev in butyl):
                continue                                 # inside the block
            fb[1] += 1; fb[0] += prev in friendly
            if it == "30G":
                g30[1] += 1; g30[0] += prev in {"B1", "B2", "B99"}
    add("Friendly directly before every butyl run", "M1", fb[0], fb[1])
    add("30G with B1 / B2 / B99 directly in front", "M1-30G", g30[0], g30[1],
        "README: the final block runs IL5 -> 120 s -> 30G, so 30G follows IL5")
    il = RU[RU["item"].map(U).isin({"IL3", "IL5"})].copy()
    if len(il):
        il["pday"] = il["start"].map(io.plant_day)
        days = il.groupby("pday")["machine"].nunique()
        add("One IL campaign a day", "s.5.2", int((days == 1).sum()), len(days))

    # ---- PM windows, fixed lots, report targets ----------------------------
    pm = io.pm_blocks()
    inpm = 0
    for r in mx.itertuples(index=False):
        for s0, f0 in pm.get(str(r.machine), []):
            if r.start < f0 and r.finish > s0:
                inpm += 1
                break
    add("No mixing lot inside a PM window", "s.2", len(mx) - inpm, len(mx))
    if "mix_plan" in mx.columns:
        fx = mx["mix_plan"].fillna("").isin(["fixed", "prep"])
        add("Mixing lots planned and fixed by the planner", "s.1", int(fx.sum()), len(mx),
            "the rest were left to the dispatcher")
    tgt = float(cfg.get("final_target_batches_per_shift", 230))
    fin = mx[mx["item_type"].astype(str).str.upper().str.contains("FINAL")].copy()
    fin["shift"] = fin["start"].map(lambda t: (io.plant_day(t), io.shift_of(t)))
    bps = fin.groupby(["machine", "shift"])["batches"].sum()
    add(f"Final batches per shift per mixer <= {tgt:g} (report target)", "s.2",
        int((bps <= tgt).sum()), len(bps), f"max {bps.max() if len(bps) else 0:g}")
    prep = mx[mx.get("mix_plan", "") == "prep"]
    add("Prep (friendly) lots are 5 batches", "BRM-01 prep",
        int((prep["batches"] == 5).sum()), len(prep))

    C = pd.DataFrame(rows)
    C.to_csv(OUT / "MIXING_RULE_COMPLIANCE.csv", index=False, encoding="utf-8-sig")
    if ev:
        pd.concat(ev, ignore_index=True).to_csv(OUT / "MIXING_RULE_EVIDENCE.csv",
                                                index=False, encoding="utf-8-sig")
    print("=" * 78)
    print(f"  MIXING RULE COMPLIANCE   [{a.run}]   {len(mx):,} mixing lots, {len(RU):,} runs")
    print("=" * 78)
    for r in C.itertuples(index=False):
        pct = "  n/a" if r.pct is None else f"{r.pct:5.1f}%"
        print(f"  {r.code:12s} {r.rule[:52]:52s} {pct}  ({r.followed:,}/{r.occurrences:,})")
    print(f"[OK] {OUT / 'MIXING_RULE_COMPLIANCE.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
