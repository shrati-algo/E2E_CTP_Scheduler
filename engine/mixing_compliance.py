#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mixing_compliance.py -- mixing-rule compliance of a finished run, as followed / occurrences (2026-10-05).

    python mixing_compliance.py <run_output_dir> [<run_output_dir> ...] [--xlsx out.xlsx]

Counts every occurrence of each measurable mixing rule on the run's FINAL schedule
(phase5_schedule_updated.csv / phase5_floor_schedule_updated.csv). Rules that are settings rather
than per-lot events (BRM-12, BRM-13), not modelled (BRM-04, BRM-08) or not applicable (BRM-05 when
there is no MK3 demand) are listed but kept out of the percentage.
"""
import os
import sys

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _shift(t):
    h = t - pd.Timedelta(hours=7)
    return f"{h.normalize():%d-%b}-{'ABC'[int((h - h.normalize()).total_seconds() // 28800)]}"


def measure(run):
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    br = cfg.get("business_rules") or {}
    mixers = {str(x) for x in (br.get("mixers") or [])}
    S = pd.read_csv(os.path.join(run, "phase5_schedule_updated.csv"),
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
    S["machine"] = S["machine"].astype(str).str.strip()
    S["item"] = S["item"].astype(str).str.strip()
    mx = S[S["machine"].isin(mixers)].sort_values(["machine", "scheduled_start"]).copy()
    fs = pd.read_csv(os.path.join(run, "phase5_floor_schedule_updated.csv"), low_memory=False)
    fs["machine"] = fs["machine"].astype(str).str.strip()
    fm = fs[fs["machine"].isin(mixers)]
    lots_b = fm.groupby("lot_id")["batches"].sum()
    itype = dict(zip(fm["lot_id"], fm["item_type"].astype(str).str.upper()))
    mx["itype"] = mx["lot_id"].map(itype).fillna("")
    mx["batches"] = mx["lot_id"].map(lots_b)
    rows = []

    def add(rule, what, occ, ok, note=""):
        rows.append({"rule": rule, "occurrence": what, "occurrences": int(occ), "followed": int(ok),
                     "violations": int(occ - ok), "compliance_%": round(100.0 * ok / occ, 2) if occ else None,
                     "note": note})

    b = pd.to_numeric(fm["batches"], errors="coerce")
    add("BR-V1", "mixer shift row in whole batches", b.notna().sum(), ((b - b.round()).abs() < 1e-6).sum())
    lb = mx["batches"].dropna()
    add("BRM-01", "mixing lot >= 5 batches (MPQ)", len(lb), (lb >= 5 - 1e-6).sum())
    st = mx[mx["item"].isin([str(x) for x in (br.get("sticky_compounds") or [])])]["batches"].dropna()
    k = int(br.get("sticky_stack_batches", 3))
    add("BRM-02", f"sticky lot in stacks of {k} batches", len(st), ((st % k).abs() < 1e-6).sum())

    # transitions on each mixer
    co_min = float(cfg.get("mixer_changeover_min", 1))
    tr_ok = float(br.get("master_final_cleanout_min", 5)) + co_min - 0.5
    n_chg = n_chg_ok = n_mf = n_mf_ok = 0
    for m, g in mx.groupby("machine"):
        r = list(g.sort_values("scheduled_start").itertuples())
        for a, c in zip(r, r[1:]):
            if a.item == c.item:
                continue
            gap = (c.scheduled_start - a.scheduled_finish).total_seconds() / 60
            n_chg += 1
            n_chg_ok += gap >= co_min - 0.1
            if {a.itype, c.itype} == {"MASTER COMPOUND", "FINAL COMPOUND"} and str(br.get("master_final_cleanout_item", "TR100")) not in (a.item, c.item):
                n_mf += 1
                n_mf_ok += gap >= tr_ok
    add("BRM-10", f"compound change on a mixer, gap >= {co_min:g} min", n_chg, n_chg_ok)
    add("BRM-06", "master<->final switch with TR100 clean-out", n_mf, n_mf_ok,
        f"gap >= {tr_ok + 0.5:g} min ({co_min:g} min changeover + {br.get('master_final_cleanout_min', 5)} min TR100)")

    # BRM-03 / BR-V2 from the run's audit (same rule engine as the scheduler)
    au = pd.read_csv(os.path.join(run, "BUSINESS_RULE_AUDIT.csv"))
    a3 = au[au["rule_id"].astype(str).str.contains("BRM-03")]
    if len(a3):
        import re
        txt = str(a3["measured"].iloc[0])
        n = int(re.search(r"(\d+) transitions", txt).group(1))
        short = int(re.search(r"(\d+) clean-outs shorter", txt).group(1))
        add("BRM-03 / BR-V2", "transition at a butyl / protected compound", n, n - short,
            "in sequence, or clean-out >= required (final 120 s; master: changeover only)")

    # BR-V2 strict (v29): every run of a final-mixer target starts right after a prep compound (B1/B2/B99) or
    # its family (IL3 <-> IL5) -- a clean-out alone does NOT count here
    bsr = br.get("butyl_sequence_rule") or {}
    fin = bsr.get("final") or {}
    tg = [str(x) for x in (fin.get("targets") or ["30G", "IL3", "IL5"])]
    pp = set(str(x) for x in (fin.get("prep") or ["B1", "B2", "B99"]))
    fam = set(str(x) for x in (fin.get("family") or []))
    # v61 (user 2026-10-10, as the plant): friendly in front of the butyl BLOCK -- butyl compounds may follow each other
    _blk = str((cfg.get("butyl_day_block") or {}).get("friendly_mode", "each")).lower() == "block"
    for t_ in tg:
        n_ = ok_ = 0
        for m, g in mx.groupby("machine"):
            r = list(g.sort_values("scheduled_start").itertuples())
            for k in range(1, len(r)):
                a, c = r[k - 1], r[k]
                if c.item == t_ and a.item != t_:
                    n_ += 1
                    ok = (a.item in pp) or (a.item in fam and t_ in fam)
                    if not ok and _blk and a.item in tg:
                        j = k - 1
                        while j >= 0 and r[j].item in tg:
                            j -= 1
                        ok = j >= 0 and r[j].item in pp
                    ok_ += ok
        if n_:
            add("BR-V2 prep in front", f"{t_} run starts right after B1/B2/B99" + (" or its IL family" if t_ in fam else "")
                + (" (or inside a butyl block that starts right after B1/B2/B99)" if _blk else ""), n_, ok_)

    # BRM-09: (mixer, shift, compound) in one continuous run
    mx["shift"] = mx["scheduled_start"].map(_shift)
    n9 = ok9 = 0
    for (m, s), g in mx.groupby(["machine", "shift"]):
        seq = list(g.sort_values("scheduled_start")["item"])
        runs = [seq[0]] + [y for x, y in zip(seq, seq[1:]) if y != x]
        for it in set(runs):
            n9 += 1
            ok9 += runs.count(it) == 1
    add("BRM-09", "(mixer, shift, compound) as one run", n9, ok9)

    # BRM-11: mixer allowed in the routing
    rt = pd.read_csv(os.path.join(run, "PHASEWISE", "00_INPUTS", "routing.csv"), dtype=str) \
        if os.path.exists(os.path.join(run, "PHASEWISE", "00_INPUTS", "routing.csv")) \
        else pd.read_csv(os.path.join(HERE, "inputs", "routing.csv"), dtype=str)
    allow = {}
    for p, ms in zip(rt["routed_product"].astype(str).str.strip(), rt["machines"].astype(str)):
        allow.setdefault(p, set()).update(x.strip().strip('"').strip("'").strip()
                                          for x in ms.replace(";", ",").split(",") if x.strip().strip('"'))
    ok11 = sum(1 for it, m in zip(mx["item"], mx["machine"]) if m in allow.get(it, set()))
    add("BRM-11", "mixing lot on a plant-allowed mixer", len(mx), ok11)

    # 3.1.3 / BRM-07: IL campaigns per plant day (07:00 -> 07:00) and B99 in front
    ilc = cfg.get("il_daily_campaign") or {}
    ili = {str(x) for x in (ilc.get("items") or ["IL3", "IL5"])}
    prep = str(ilc.get("prep", "B99"))
    camps = []
    for m, g in mx.groupby("machine"):
        r = list(g.sort_values("scheduled_start").itertuples())
        i = 0
        while i < len(r):
            if r[i].item in ili:
                j = i
                while j + 1 < len(r) and r[j + 1].item in ili:
                    j += 1
                k = i - 1
                if _blk:                                    # v61: walk back over the butyl block to its friendly
                    while k >= 0 and r[k].item in set(tg):
                        k -= 1
                camps.append(((r[i].scheduled_start - pd.Timedelta(hours=7)).normalize(),
                              r[k].item if k >= 0 else ""))
                i = j + 1
            else:
                i += 1
    days = pd.Series([d for d, _ in camps]).value_counts()
    add("3.1.3 / BRM-07", "plant day (07:00->07:00) with exactly one IL campaign", len(days), int((days == 1).sum()))
    if _blk:
        add("3.1.3", "IL campaign with a butyl-friendly (B1/B2/B99) in front of its butyl block", len(camps),
            sum(p in pp for _, p in camps))
    else:
        add("3.1.3", f"IL campaign with {prep} directly in front", len(camps), sum(p == prep for _, p in camps))
    df = pd.DataFrame(rows)
    tot = df["occurrences"].sum()
    fol = df["followed"].sum()
    summary = {"occurrences": int(tot), "followed": int(fol), "overall_%": round(100.0 * fol / tot, 2),
               "rule_average_%": round(df["compliance_%"].mean(), 2)}
    return df, summary


EXCLUDED = pd.DataFrame([
    ("BRM-12", "plant-approved mixing cycle time", "setting: runtime = qty / routing proc_time x efficiency"),
    ("BRM-13", "+5 buffer batches per compound", "setting: buffer_batches = 5 on each compound's last lot"),
    ("BRM-05", "ML405/ML406 followed by MK3", "not applicable: no MK3 demand in this plan"),
    ("BRM-04", "K4 sequencing around IL compounds", "not modelled (plant: 'confirm for CTP')"),
    ("BRM-08", "both finals of one master consecutively", "not modelled (plant: 'confirm for CTP')"),
], columns=["rule", "description", "why not in the %"])


if __name__ == "__main__":
    args = sys.argv[1:]
    out = None
    if "--xlsx" in args:
        out = args[args.index("--xlsx") + 1]
        args = args[:args.index("--xlsx")]
    allrows, sums = [], []
    for run in args:
        name = os.path.basename(os.path.dirname(os.path.abspath(run)))
        df, sm = measure(run)
        df.insert(0, "version", name)
        allrows.append(df)
        sums.append({"version": name, **sm})
        print(f"\n== {name}: {sm['followed']:,} of {sm['occurrences']:,} followed = {sm['overall_%']}% "
              f"(average over rules {sm['rule_average_%']}%)")
        print(df[["rule", "occurrences", "followed", "violations", "compliance_%"]].to_string(index=False))
    if out:
        with pd.ExcelWriter(out) as xw:
            pd.DataFrame(sums).to_excel(xw, "Summary", index=False)
            pd.concat(allrows).to_excel(xw, "By rule", index=False)
            EXCLUDED.to_excel(xw, "Not in the %", index=False)
        print("wrote", out)
