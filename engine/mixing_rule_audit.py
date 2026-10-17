#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mixing_rule_audit.py -- row-by-row audit of the mixing rules (user list updated 01-Oct-2026) on a schedule workbook.

    python mixing_rule_audit.py v38     -> latest_schedule/v38/MIXING_RULE_AUDIT_v38.xlsx

Reads the mixer rows of the workbook (Upstream PCR / TBR / PCR+TBR sheets, lot rows) exactly as published and
checks, on each mixer's sequence (consecutive rows of one compound = one run; gap = next start - previous end):
  M1 Final mixer - before butyl : every run of 30G / IL3 / IL5 starts right after a B1 / B2 / B99 run
  M2 Final mixer - after butyl  : after a 30G / IL3 / IL5 or 29MG / ML405 / ML605 run, the next compound starts >= 120 s later
  M3 Master mixer - butyl seq   : every run of 29MG / ML405 / ML605 has MB100 / MB200 / XMB98 directly before AND after
  M4 430-2 master - final cpd   : a final compound (e.g. T638) on K430-2 has >= 120 s clean-out before AND after
  M5 IL3 / IL5 in one go a day  : per plant day (07:00 -> 07:00) all batches of IL3 (and of IL5) in ONE continuous run
  M6 Batches, not KG            : every mixing row in BATCH with a whole number of batches
Literal reading of the rule text; where the plant's configured exception differs (IL3 <-> IL5 inside one campaign)
the row is still reported, with the exception named in 'note'. 'ML045 / ML065' in the rule text = ML405 / ML605
(the codes in BOM / routing).
"""
import os
import sys

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from schedule_changeovers import read_sched

FINAL_BUTYL, FINAL_FRIENDLY = {"30G", "IL3", "IL5"}, {"B1", "B2", "B99"}
MASTER_BUTYL, MASTER_FRIENDLY = {"29MG", "ML405", "ML605"}, {"MB100", "MB200", "XMB98"}
IL_FAMILY = {"IL3", "IL5"}
NAME = {}
CLEAN_S = 120
MIXERS = ["K270M", "K310", "K430-1", "K430-2", "F270", "F440"]
H7 = pd.Timedelta(hours=7)
RULES = [
    ("M1", "Final Mixer – Before Butyl", "A Butyl-friendly compound must be scheduled immediately before running a Butyl compound. Butyl compounds 30G, IL3, IL5; Butyl friendly B1, B2, B99."),
    ("M2", "Final Mixer – After Butyl", "After running a Butyl compound, a mandatory 120-second clean-out must be provided: final 30G / IL3 / IL5 and (rule text) 29MG / ML405 / ML605 ('ML045 / ML065'); gap to the next compound >= 120 s."),
    ("M3", "Master Mixer – Butyl Sequence", "A Butyl-friendly compound (MB100, MB200, XMB98) must be scheduled immediately before and after every Butyl compound (29MG, ML405, ML605; 'ML045 / ML065' in the rule text)."),
    ("M4", "430-2 Master Mixer – Final Compound", "When a final compound (T638) is run on the 430-2 Master Mixer, a mandatory 120-second clean-out must be provided both before and after the compound."),
    ("M5", "IL3 and IL5 together in one go a day", "IL3 and IL5 made together in the final mixer in ONE go once a day: B99 -> IL3 -> IL5 back to back (each compound once, no other compound between)."),
    ("M6", "Mixing output in batches", "The mixing output should be in terms of batches, not KG."),
    ("M7", "One compound in one mixer per day", "One compound should be planned in only one mixer per plant day (07:00 -> 07:00) and not split across multiple mixers."),
    ("M8", "Same compound not repeated within 15 h (24 h IL / 4RC)", "A new run of the same compound starts at least 15 h after the previous run of it started (24 h for IL3 / IL5 and the 4RC compounds); butyl-friendly compounds exempt (config mixing_v47.exempt)."),
]
TARGET_FINAL_BATCHES_PER_SHIFT = 230


def _bmode():
    """v61 (user 2026-10-10): 'block' = the butyl-friendly runs before (and on the master mixer after) the whole butyl
    block; butyl compounds may follow each other (with the 120 s clean-out) without a friendly between them."""
    try:
        import yaml
        return str(((yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {})
                    .get("butyl_day_block") or {}).get("friendly_mode", "each")).lower()
    except Exception:
        return "each"


def _clean_other():
    """v67 (user 2026-10-10): the 120 s clean-out is needed only when a butyl compound is followed by a compound that is
    neither butyl nor butyl-friendly (mix_planner.butyl_cleanout_to: other)."""
    try:
        import yaml
        return str(((yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {})
                    .get("mix_planner") or {}).get("butyl_cleanout_to", "any")).lower() == "other"
    except Exception:
        return False


def _cfg47():
    try:
        import yaml
        return (yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {}).get("mixing_v47") or {}
    except Exception:
        return {}


def _fourrc_cpds():
    try:
        fl = pd.read_csv(os.path.join(HERE, "inputs", "frc_lengths.csv"))
        b = pd.read_csv(os.path.join(HERE, "inputs", "bom.csv"), dtype=str, low_memory=False)
        par = set(fl["item"].astype(str).str.strip())
        return set(b.loc[b["Parent"].astype(str).str.strip().isin(par), "child"].astype(str).str.strip())
    except Exception:
        return set()


def runs_of(m):
    """consecutive rows of one compound on a mixer -> runs with start, end, batches, rows."""
    out = []
    for r in m.itertuples():
        if out and out[-1]["item"] == r.item and out[-1]["machine"] == r.machine:
            o = out[-1]
            o["end"], o["batches"], o["rows"] = max(o["end"], r.end_time), o["batches"] + float(r.produce_qty or 0), o["rows"] + [r.Index]
            o["lots"].add(r.lot_id)
        else:
            out.append({"machine": r.machine, "item": r.item, "item_type": r.item_type, "start": r.start_time,
                        "end": r.end_time, "batches": float(r.produce_qty or 0), "rows": [r.Index], "lots": {r.lot_id}})
    return out


def _final_cleanout_s():
    """v93 (user 2026-10-13): seconds of clean-out required after EVERY final compound (config
    mix_planner.final_cleanout_after_s); 0 = rule not in force (older versions)."""
    try:
        import yaml
        return float((((yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {})
                       .get("mix_planner") or {}).get("final_cleanout_after_s")) or 0)
    except Exception:
        return 0.0


def main(v):
    _FCO = _final_cleanout_s()
    if _FCO > 0 and not any(k == "M9" for k, _, _ in RULES):
        RULES.append(("M9", "Final compound – clean-out after", f"After running every final compound, a mandatory "
                      f"{_FCO:.0f}-second clean-out must be provided before the next compound (user 13-Oct)."))
    if _clean_other():                                    # v67 (user 2026-10-10)
        RULES[:] = [(k, nm, ("After a Butyl compound, a 120-second clean-out (in place of the changeover) is needed only when the "
                             "next compound is neither butyl nor butyl-friendly; butyl -> butyl / friendly takes the normal "
                             "changeover (user 10-Oct). Final 30G / IL3 / IL5, master 29MG / ML405 / ML605.") if k == "M2" else tx)
                    for k, nm, tx in RULES]
    if _bmode() == "block":                               # v61 (user 2026-10-10): butyl friendly at block level
        _t = {"M1": "A Butyl-friendly compound (B1, B2, B99) must run immediately before a Butyl block (30G, IL3, IL5 -- butyl "
                    "compounds may follow each other inside the block with the 120 s clean-out); user 10-Oct, as the plant.",
              "M3": "A Butyl-friendly compound (MB100, MB200, XMB98) must run immediately before and after the Butyl block "
                    "(29MG, ML405, ML605 together, 120 s clean-out between them); user 10-Oct, as the plant."}
        RULES[:] = [(k, nm, _t.get(k, tx)) for k, nm, tx in RULES]
    NAME.update({k: nm for k, nm, _ in RULES})
    vdir = os.path.join(HERE, "latest_schedule", v)
    xl = [f for f in os.listdir(vdir) if f.startswith("CTP_") and "SCHEDULE" in f and f.endswith(".xlsx") and "UNIFIED" not in f][0]
    u = read_sched(os.path.join(vdir, xl), "Upstream")
    m = u[u["machine"].astype(str).isin(MIXERS)].copy()
    m["machine"], m["item"] = m["machine"].astype(str), m["item"].astype(str).str.strip()
    m["start_time"], m["end_time"] = pd.to_datetime(m["start_time"]), pd.to_datetime(m["end_time"])
    m = m.sort_values(["machine", "start_time"]).reset_index(drop=True)
    m["plant_day"] = (m["start_time"] - H7).dt.normalize()
    for k, _, _ in RULES:
        m[k] = "n.a."
    m["note"] = ""
    viol = {k: [] for k, _, _ in RULES}
    occ = {k: 0 for k, _, _ in RULES}
    ev = {k: [] for k, _, _ in RULES}                     # every occurrence, followed or not (evidence workbook)
    _mixkind = lambda i: ("Final mixing" if "FINAL" in str(m.at[i, "process"]).upper() else "Master mixing")

    def mark(rule, idxs, status, note=""):
        for i in idxs:
            m.at[i, rule] = status
            if note and status == "VIOLATION":
                m.at[i, "note"] = (m.at[i, "note"] + " | " if m.at[i, "note"] else "") + f"{NAME.get(rule, rule)}: {note}"

    _BM = _bmode()
    _CLEAN_OTHER = _clean_other()
    for mc, g in m.groupby("machine", sort=False):
        R = runs_of(g)
        for i, r in enumerate(R):
            p = R[i - 1] if i > 0 else None
            n = R[i + 1] if i + 1 < len(R) else None
            gap_b = (r["start"] - p["end"]).total_seconds() if p else None
            gap_a = (n["start"] - r["end"]).total_seconds() if n else None
            base = {"machine": mc, "compound": r["item"], "item_type": r["item_type"], "run_start": r["start"],
                    "run_end": r["end"], "batches": r["batches"], "lots": ";".join(sorted(r["lots"])),
                    "previous_compound": p["item"] if p else "", "gap_before_s": gap_b,
                    "next_compound": n["item"] if n else "", "gap_after_s": gap_a}
            base = {"plant_day": (r["start"] - H7).normalize(), "line": m.at[r["rows"][0], "line"],
                    "mixing": _mixkind(r["rows"][0]), **base}
            # M1
            if r["item"] in FINAL_BUTYL:
                occ["M1"] += 1
                ok = p is not None and p["item"] in FINAL_FRIENDLY
                if _BM == "block" and not ok:                     # v61: inside a butyl block -> look at the block start
                    j = i - 1
                    while j >= 0 and R[j]["item"] in FINAL_BUTYL:
                        j -= 1
                    if j < i - 1 and j >= 0 and R[j]["item"] in FINAL_FRIENDLY:
                        ok, p = True, R[j]
                pp = R[i - 2] if i > 1 else None
                camp_ok = (p is not None and p["item"] in IL_FAMILY and r["item"] in IL_FAMILY and p["item"] != r["item"]
                           and pp is not None and pp["item"] in FINAL_FRIENDLY)
                if camp_ok:
                    ok = True
                note = ("" if ok else "no compound before it on this mixer" if p is None else
                        f"preceded by {p['item']}" + (" (IL3<->IL5 inside one IL campaign: plant exception, config family)"
                                                    if p["item"] in IL_FAMILY and r["item"] in IL_FAMILY else ""))
                mark("M1", r["rows"], "OK" if ok else "VIOLATION", note)
                ev["M1"].append({**base, "result": "FOLLOWED" if ok else "VIOLATION",
                                 "how": ((f"IL campaign {pp['item']} -> {p['item']} -> {r['item']}: butyl-friendly {pp['item']} "
                                          f"directly before the campaign" if camp_ok else
                                          f"butyl-friendly {p['item']} runs directly before it (gap {gap_b:.0f} s)") if ok else note)})
                if not ok:
                    viol["M1"].append({**base, "violation": f"{r['item']} not directly after B1/B2/B99", "note": note})
            # M2
            _bto = (FINAL_BUTYL | FINAL_FRIENDLY) if r["item"] in FINAL_BUTYL else (MASTER_BUTYL | MASTER_FRIENDLY)
            if r["item"] in (FINAL_BUTYL | MASTER_BUTYL) and n is not None and not (
                    r["item"] in IL_FAMILY and n["item"] in IL_FAMILY and r["item"] != n["item"]) and not (
                    _CLEAN_OTHER and n["item"] in _bto):     # v67: butyl -> butyl / friendly = normal changeover
                occ["M2"] += 1
                ok = gap_a is not None and gap_a >= CLEAN_S - 1
                note = "" if ok else f"next {n['item']} starts {gap_a:.0f} s after the butyl run (needs >= {CLEAN_S} s)" + (
                    " (IL3<->IL5 inside one IL campaign)" if n["item"] in IL_FAMILY and r["item"] in IL_FAMILY else "")
                mark("M2", r["rows"], "OK" if ok else "VIOLATION", note)
                ev["M2"].append({**base, "result": "FOLLOWED" if ok else "VIOLATION",
                                 "how": (f"{gap_a:.0f} s clean-out after {r['item']} before {n['item']} starts" if ok else note)})
                if not ok:
                    viol["M2"].append({**base, "violation": f"clean-out after {r['item']} only {gap_a:.0f} s", "note": note})
            # M3
            if r["item"] in MASTER_BUTYL:
                occ["M3"] += 1
                okb = p is not None and p["item"] in MASTER_FRIENDLY
                oka = n is None or n["item"] in MASTER_FRIENDLY
                if _BM == "block":                                # v61: friendly before / after the whole block
                    j = i - 1
                    while j >= 0 and R[j]["item"] in MASTER_BUTYL:
                        j -= 1
                    k = i + 1
                    while k < len(R) and R[k]["item"] in MASTER_BUTYL:
                        k += 1
                    okb = j >= 0 and R[j]["item"] in MASTER_FRIENDLY
                    oka = k >= len(R) or R[k]["item"] in MASTER_FRIENDLY
                    p = R[j] if j >= 0 else None
                    n = R[k] if k < len(R) else None
                bad = ([] if okb else [f"before: {p['item'] if p else 'nothing'}"]) + ([] if oka else [f"after: {n['item']}"])
                note = "; ".join(bad) + ("" if n is not None else " (last run on the mixer: nothing after)" if not bad else "")
                mark("M3", r["rows"], "OK" if not bad else "VIOLATION", note)
                ev["M3"].append({**base, "result": "FOLLOWED" if not bad else "VIOLATION",
                                 "how": (f"{p['item']} directly before, {n['item'] if n else '(last run on the mixer)'} directly after"
                                         if not bad else note)})
                if bad:
                    viol["M3"].append({**base, "violation": f"{r['item']} not between MB100/MB200/XMB98", "note": note})
            # M4
            if mc == "K430-2" and str(r["item_type"]).upper() == "FINAL COMPOUND":
                occ["M4"] += 1
                bad = ([] if (p is None or gap_b >= CLEAN_S - 1) else [f"before {gap_b:.0f} s"]) + \
                      ([] if (n is None or gap_a >= CLEAN_S - 1) else [f"after {gap_a:.0f} s"])
                mark("M4", r["rows"], "OK" if not bad else "VIOLATION", "; ".join(bad))
                ev["M4"].append({**base, "result": "FOLLOWED" if not bad else "VIOLATION",
                                 "how": (f"clean-out {gap_b or 0:.0f} s before / {gap_a or 0:.0f} s after" if not bad else "; ".join(bad))})
                if bad:
                    viol["M4"].append({**base, "violation": "clean-out < 120 s around a final compound on 430-2", "note": "; ".join(bad)})
            # M9 (v93): clean-out after every final compound, any mixer
            if "M9" in occ and str(r["item_type"]).upper() == "FINAL COMPOUND" and n is not None:
                occ["M9"] += 1
                ok = gap_a >= _FCO - 1
                note = "" if ok else f"next {n['item']} starts {gap_a:.0f} s after (needs >= {_FCO:.0f} s)"
                mark("M9", r["rows"], "OK" if ok else "VIOLATION", note)
                ev["M9"].append({**base, "result": "FOLLOWED" if ok else "VIOLATION",
                                 "how": f"{gap_a:.0f} s clean-out after {r['item']} before {n['item']}" if ok else note})
                if not ok:
                    viol["M9"].append({**base, "violation": f"clean-out after final {r['item']} only {gap_a:.0f} s", "note": note})
    # M5 (v41): IL3 and IL5 together in ONE go once a plant day -- one IL campaign (consecutive IL runs on a mixer)
    # starts that day, each IL compound once in it (IL3 -> IL5 back to back); a day's IL rows outside it can only be
    # the tail of the previous day's campaign running past 07:00
    camps = []
    for mc_, g_ in m.groupby("machine", sort=False):
        cur = []
        for r in runs_of(g_):
            if r["item"] in IL_FAMILY:
                cur.append(r)
            else:
                if cur:
                    camps.append(cur)
                cur = []
        if cur:
            camps.append(cur)
    il = m[m["item"].isin(IL_FAMILY)]
    for d, g in il.groupby("plant_day"):
        occ["M5"] += 1
        idx = set(g.index)
        cs = [c for c in camps if any(set(r["rows"]) & idx for r in c)]
        own = [c for c in cs if (c[0]["start"] - H7).normalize() == d]
        tail = [c for c in cs if (c[0]["start"] - H7).normalize() < d]
        items_once = all(len([r["item"] for r in c]) == len(set(r["item"] for r in c)) for c in own)
        day_items = set(g["item"])
        together = (len(own) == 1 and day_items <= set(r["item"] for r in own[0]) | set(r["item"] for c in tail for r in c))
        ok = len(own) <= 1 and items_once and (together or not own) and len(tail) <= 1
        how = ""
        if own:
            c = own[0]
            how = (" -> ".join(f"{r['item']} {r['batches']:g} b" for r in c) +
                   f" back to back in ONE go {c[0]['start']:%d-%b %H:%M}-{c[-1]['end']:%H:%M} on {c[0]['machine']}")
        elif tail:
            how = f"continuation of the IL campaign started the previous plant day ({tail[0][0]['start']:%d-%b %H:%M}, crosses 07:00)"
        note = "" if ok else (f"{len(own)} IL campaigns start that day" if len(own) > 1 else
                              "a compound runs twice in the campaign" if not items_once else
                              "IL3 and IL5 not in the same campaign")
        mark("M5", list(g.index), "OK" if ok else "VIOLATION", note)
        ev["M5"].append({"plant_day": d, "line": g["line"].iloc[0], "mixing": _mixkind(g.index[0]),
                         "compound": " + ".join(sorted(day_items)), "machine": ",".join(sorted(set(g["machine"]))),
                         "campaigns_starting_that_day": len(own), "day_batches": float(pd.to_numeric(g["produce_qty"], errors="coerce").sum()),
                         "lots": ";".join(sorted(set(g["lot_id"]))), "result": "FOLLOWED" if ok else "VIOLATION",
                         "how": how if ok else note})
        if not ok:
            viol["M5"].append({"plant_day": d, "compound": " + ".join(sorted(day_items)), "violation": note,
                               "lots": ";".join(sorted(set(g["lot_id"])))})
    # M6: output in batches
    occ["M6"] = len(m)
    q = pd.to_numeric(m["produce_qty"], errors="coerce")
    ok6 = (m["UOM"].astype(str).str.upper() == "BATCH") & q.notna() & ((q - q.round()).abs() < 1e-6)
    for i in m.index:
        m.at[i, "M6"] = "OK" if ok6[i] else "VIOLATION"
    for i in m.index[~ok6]:
        viol["M6"].append({"machine": m.at[i, "machine"], "compound": m.at[i, "item"], "start_time": m.at[i, "start_time"],
                           "produce_qty": m.at[i, "produce_qty"], "UOM": m.at[i, "UOM"], "lot_id": m.at[i, "lot_id"],
                           "violation": "not whole batches / not BATCH"})
    for i in m.index:
        ev["M6"].append({"plant_day": m.at[i, "plant_day"], "line": m.at[i, "line"], "mixing": _mixkind(i),
                         "machine": m.at[i, "machine"], "compound": m.at[i, "item"], "start_time": m.at[i, "start_time"],
                         "produce_qty": m.at[i, "produce_qty"], "UOM": m.at[i, "UOM"], "lot_id": m.at[i, "lot_id"],
                         "result": "FOLLOWED" if ok6[i] else "VIOLATION",
                         "how": (f"{m.at[i, 'produce_qty']:g} whole batches (UOM BATCH)" if ok6[i]
                                 else "not whole batches / not BATCH")})
    # M7 (v47): one compound on ONE mixer per plant day
    for (it, d), g in m.groupby(["item", "plant_day"]):
        occ["M7"] += 1
        ms = sorted(set(g["machine"]))
        ok = len(ms) == 1
        mark("M7", list(g.index), "OK" if ok else "VIOLATION", "" if ok else f"on {len(ms)} mixers: {', '.join(ms)}")
        ev["M7"].append({"plant_day": d, "line": g["line"].iloc[0], "mixing": _mixkind(g.index[0]), "compound": it,
                         "machine": ", ".join(ms), "rows": len(g),
                         "batches": float(pd.to_numeric(g["produce_qty"], errors="coerce").sum()),
                         "result": "FOLLOWED" if ok else "VIOLATION",
                         "how": f"all {len(g)} rows on {ms[0]}" if ok else f"split over {', '.join(ms)}"})
        if not ok:
            viol["M7"].append({"plant_day": d, "compound": it, "mixers": ", ".join(ms),
                               "violation": "compound on more than one mixer that day",
                               "lots": ";".join(sorted(set(g["lot_id"])))})
    # M8 (v47): repeat gap = start of a run -> start of the next run of the same compound (any mixer)
    cfg_ = _cfg47()
    ex8 = {str(x).strip() for x in (cfg_.get("exempt") or [])}
    long8 = {str(x).strip() for x in (cfg_.get("long_items") or [])} | (
        _fourrc_cpds() if cfg_.get("long_fourrc_compounds", True) else set())
    g15, g24 = float(cfg_.get("gap_h", 15)), float(cfg_.get("gap_h_long", 24))
    byit = {}
    for _, g_ in m.groupby("machine", sort=False):
        for r in runs_of(g_):
            byit.setdefault(r["item"], []).append(r)
    for it, rs in byit.items():
        if it in ex8:
            continue
        rs.sort(key=lambda r: r["start"])
        need = g24 if it in long8 else g15
        for a, b in zip(rs, rs[1:]):
            occ["M8"] += 1
            gap = (b["start"] - a["start"]).total_seconds() / 3600
            ok = gap >= need - 1 / 60
            note = "" if ok else (f"run on {b['machine']} {b['start']:%d-%b %H:%M} starts {gap:.1f} h after the previous run "
                                  f"({a['machine']} {a['start']:%d-%b %H:%M}); needs >= {need:g} h")
            mark("M8", b["rows"], "OK" if ok else "VIOLATION", note)
            ev["M8"].append({"plant_day": (b["start"] - H7).normalize(), "line": m.at[b["rows"][0], "line"],
                             "mixing": _mixkind(b["rows"][0]), "compound": it, "required_gap_h": need,
                             "previous_run": f"{a['machine']} {a['start']:%d-%b %H:%M}",
                             "run": f"{b['machine']} {b['start']:%d-%b %H:%M}", "gap_h": round(gap, 2),
                             "batches": b["batches"], "result": "FOLLOWED" if ok else "VIOLATION",
                             "how": f"{gap:.1f} h after the previous run (>= {need:g} h)" if ok else note})
            if not ok:
                viol["M8"].append({"compound": it, "required_gap_h": need, "previous_run_start": a["start"],
                                   "previous_mixer": a["machine"], "run_start": b["start"], "mixer": b["machine"],
                                   "gap_h": round(gap, 2), "violation": note})
    # rows -> status per row
    rows_chk = {k: int((m[k] != "n.a.").sum()) for k, _, _ in RULES}
    rows_bad = {k: int((m[k] == "VIOLATION").sum()) for k, _, _ in RULES}
    summ = pd.DataFrame([{
        "rule": nm, "rule text (as evaluated)": txt,
        "occurrences checked": occ[k] if k != "M6" else rows_chk[k], "violations": len(viol[k]),
        "compliance_%": (round(100 * (1 - len(viol[k]) / occ[k]), 2) if occ[k] else None),
        "schedule rows checked": rows_chk[k], "schedule rows in violation": rows_bad[k],
        "status": ("NOT APPLICABLE (no such run in this schedule)" if not occ[k] else
                   "FOLLOWED" if not viol[k] else "VIOLATED")} for k, nm, txt in RULES])
    # final mixer batches per shift vs the 230 target (report only; plan window; an idle shift counts as 0)
    fm = m[m["process"].astype(str).str.upper().str.contains("FINAL")].copy()
    ps, pe = os.environ.get("CTP_PLAN_START"), os.environ.get("CTP_PLAN_END")
    if ps and pe:
        fm = fm[(fm["start_time"] >= pd.Timestamp(ps)) & (fm["start_time"] < pd.Timestamp(pe))]
    fm["q"] = pd.to_numeric(fm["produce_qty"], errors="coerce").fillna(0)
    sh = fm.groupby(["machine", "plant_day", "shift"])["q"].sum()
    if ps and pe and len(fm):
        days = pd.date_range(pd.Timestamp(ps).normalize(), pd.Timestamp(pe).normalize() - pd.Timedelta(days=1))
        idx = pd.MultiIndex.from_product([sorted(set(fm["machine"])), days, ["A", "B", "C"]], names=sh.index.names)
        sh = sh.reindex(idx, fill_value=0)
    per = sh.groupby(level=0).agg(["mean", "min", "max", "count"]).round(1) if len(sh) else pd.DataFrame()
    T9 = TARGET_FINAL_BATCHES_PER_SHIFT
    shift_tab = sh.reset_index().rename(columns={"q": "batches"})
    shift_tab["target"] = T9
    shift_tab["vs_target_%"] = (100 * shift_tab["batches"] / T9).round(1)
    tgt = pd.DataFrame([{"rule": f"Final mixer batches per shift (target {T9})",
                         "rule text (as evaluated)": f"average whole batches per shift (A / B / C, every shift of the plan "
                                                     f"window, idle = 0) on each final mixer vs the target {T9} (report, no cap)",
                         "occurrences checked": int(per["count"].sum()) if len(per) else 0, "violations": None,
                         "compliance_%": round(100 * float(sh.mean()) / T9, 1) if len(sh) else None,
                         "schedule rows checked": len(fm), "schedule rows in violation": None,
                         "status": "TARGET (report): " + "; ".join(
                             f"{mc} avg {r['mean']:.0f} / shift (min {r['min']:.0f}, max {r['max']:.0f})"
                             for mc, r in per.iterrows())}])
    summ = pd.concat([summ, tgt], ignore_index=True)
    out = os.path.join(vdir, f"MIXING_RULE_AUDIT_{v}.xlsx")
    _safe = lambda s_: "".join(ch for ch in s_ if ch not in '/\\?*[]:')[:31].strip()
    sheets = [("Summary", summ)] + [(_safe(nm), pd.DataFrame(viol[k]) if viol[k] else
                                     pd.DataFrame([{"result": f"no violations ({occ[k]} occurrences checked)"}]))
                                    for k, nm, _ in RULES]
    cols = ["plant_day", "date", "shift", "machine", "item", "item_type", "start_time", "end_time", "produce_qty", "UOM",
            "lot_id"] + [k for k, _, _ in RULES] + ["note"]
    sheets.append(("Final batches per shift", shift_tab))
    sheets.append(("All mixing rows", m[cols].rename(columns={k: nm for k, nm, _ in RULES})))
    hdr, bold = PatternFill("solid", fgColor="FF1F3864"), Font(bold=True, color="FFFFFFFF")
    red, grn = PatternFill("solid", fgColor="FFFFC7CE"), PatternFill("solid", fgColor="FFC6EFCE")
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in sheets:
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for c in ws[1]:
                c.fill, c.font, c.alignment = hdr, bold, Alignment(wrap_text=True, vertical="center")
            for j, c in enumerate(df.columns, 1):
                w = min(max(10, int(df[c].astype(str).str.len().head(500).max() if len(df) else 10) + 2, len(str(c)) + 2), 70)
                ws.column_dimensions[get_column_letter(j)].width = w
                if "time" in str(c) or str(c) in ("run_start", "run_end", "plant_day"):
                    for r in range(2, len(df) + 2):
                        ws.cell(row=r, column=j).number_format = "dd-mmm-yy hh:mm:ss"
            ws.auto_filter.ref = ws.dimensions
            ws.freeze_panes = "A2"
            if name == "All mixing rows":
                for j, c in enumerate(df.columns, 1):
                    if c in {nm for _, nm, _ in RULES}:
                        for r, val in enumerate(df[c].tolist(), 2):
                            if val == "VIOLATION":
                                ws.cell(row=r, column=j).fill = red
                            elif val == "OK":
                                ws.cell(row=r, column=j).fill = grn
            if name == "Summary":
                for r, st in enumerate(df["status"].tolist(), 2):
                    ws.cell(row=r, column=len(df.columns)).fill = (red if st == "VIOLATED" else grn if st == "FOLLOWED"
                                                                   else PatternFill())
    _evidence(v, vdir, ev, m)
    print(summ[["rule", "occurrences checked", "violations", "compliance_%", "status"]].to_string(index=False))
    print(f"wrote {out}")
    return summ, viol


def _evidence(v, vdir, ev, m):
    """MIXING_RULE_EVIDENCE_<v>.xlsx: every occurrence of every rule (followed or not), split PCR / TBR and final /
    master mixing, with HOW it is followed; summary by rule x line x mixing; every mixing row with its status."""
    rows = []
    for k, nm, txt in RULES:
        d = pd.DataFrame(ev[k])
        for ln in ("PCR", "TBR"):
            for mk in ("Final mixing", "Master mixing"):
                x = d[(d["line"] == ln) & (d["mixing"] == mk)] if len(d) else d
                n_ = len(x)
                f_ = int((x["result"] == "FOLLOWED").sum()) if n_ else 0
                rows.append({"rule": nm, "line": ln, "mixing": mk, "occurrences": n_, "followed": f_,
                             "violations": n_ - f_, "compliance_%": round(100 * f_ / n_, 2) if n_ else None,
                             "status": "NOT APPLICABLE (no occurrence)" if not n_ else
                             ("100% FOLLOWED" if f_ == n_ else "VIOLATED")})
    S = pd.DataFrame(rows)
    T = S.groupby("rule", sort=False).agg(occurrences=("occurrences", "sum"), followed=("followed", "sum")).reset_index()
    T["compliance_%"] = (100 * T["followed"] / T["occurrences"]).round(2).where(T["occurrences"] > 0)
    T["status"] = ["NOT APPLICABLE (no occurrence in this schedule)" if o == 0 else ("100% FOLLOWED" if f == o else "VIOLATED")
                   for o, f in zip(T["occurrences"], T["followed"])]
    T["rule text (as evaluated)"] = [txt for _, _, txt in RULES]
    bad = '/\\?*[]:'
    _safe = lambda s_: "".join(ch for ch in s_ if ch not in bad)[:31].strip()
    cols = ["plant_day", "line", "date", "shift", "machine", "item", "item_type", "process", "start_time", "end_time",
            "produce_qty", "UOM", "lot_id"] + [k for k, _, _ in RULES]
    A = m[cols].rename(columns={k: nm for k, nm, _ in RULES})
    sheets = [("Summary", T), ("By line and mixing", S)]
    sheets += [(_safe(nm), pd.DataFrame(ev[k]) if ev[k] else pd.DataFrame([{"result": "no occurrence in this schedule"}]))
               for k, nm, _ in RULES]
    sheets += [("All mixing rows", A)]
    out = os.path.join(vdir, f"MIXING_RULE_EVIDENCE_{v}.xlsx")
    hdr, bold = PatternFill("solid", fgColor="FF1F3864"), Font(bold=True, color="FFFFFFFF")
    red, grn = PatternFill("solid", fgColor="FFFFC7CE"), PatternFill("solid", fgColor="FFC6EFCE")
    rule_names = {nm for _, nm, _ in RULES}
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in sheets:
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for c in ws[1]:
                c.fill, c.font, c.alignment = hdr, bold, Alignment(wrap_text=True, vertical="center")
            for j, c in enumerate(df.columns, 1):
                w = min(max(10, int(df[c].astype(str).str.len().head(500).max() if len(df) else 10) + 2, len(str(c)) + 2), 70)
                ws.column_dimensions[get_column_letter(j)].width = w
                if "time" in str(c) or str(c) in ("run_start", "run_end", "plant_day"):
                    for r in range(2, len(df) + 2):
                        ws.cell(row=r, column=j).number_format = "dd-mmm-yy hh:mm:ss"
                if str(c) in ("result", "status") or str(c) in rule_names:
                    for r, val in enumerate(df[c].tolist(), 2):
                        sv = str(val)
                        if "VIOLAT" in sv:
                            ws.cell(row=r, column=j).fill = red
                        elif sv.startswith("FOLLOWED") or sv.startswith("100%") or sv == "OK":
                            ws.cell(row=r, column=j).fill = grn
            ws.auto_filter.ref = ws.dimensions
            ws.freeze_panes = "A2"
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "v38")
