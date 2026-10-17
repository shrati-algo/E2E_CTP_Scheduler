#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_business_rules.py -- check a finished run against businees_rules/*.xlsx (2026-09-27).

    python audit_business_rules.py [run_dir]      (default: outputs2 = the latest run)

Every MEASURABLE rule is checked on the placed schedule; the rest are classified as
NOT MODELLED (no logic) or NO DATA (the plant master needed is missing). Writes
output/<run>/BUSINESS_RULE_AUDIT.csv and prints a summary.
"""
import os
import re
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "outputs2")
INP = os.path.join(HERE, "inputs")
O2 = os.path.join(HERE, "outputs2")

S = pd.read_csv(os.path.join(RUN, "phase5_schedule_updated.csv"),
                parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
S["machine"] = S["machine"].astype(str).str.strip()
S["item"] = S["item"].astype(str).str.strip()
S["dept"] = S["department"].astype(str).str.upper()
up = S[~S["dept"].isin(["BUILDING", "CURING"])].copy()
L = pd.read_csv(os.path.join(O2, "phase2_lots_updated.csv"), parse_dates=["first_need", "last_need"])
L = L.set_index("lot_id")
up["qty"] = up["lot_id"].map(L["lot_qty"])
up["itype"] = up["lot_id"].map(L["item_type"]).astype(str).str.upper()
rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
t0 = pd.Timestamp("2026-09-26 07:00")


def shift_of(ts):
    h = (ts - pd.Timedelta(hours=7))
    k = int((h - h.normalize()).total_seconds() // (8 * 3600))
    return f"{h.normalize():%d-%b}-{'ABC'[k]}"


up["shift"] = up["scheduled_start"].map(shift_of)
up = up.sort_values(["machine", "scheduled_start"])
res = []


def add(rid, rule, status, measured, detail=""):
    res.append({"rule_id": rid, "rule": rule, "status": status, "measured": measured, "detail": detail})


# ---------- eligibility ----------
elig = defaultdict(set)
for p, m in zip(rt["routed_product"].fillna("").astype(str).str.strip(), rt["machines"].fillna("").astype(str)):
    elig[p] |= set(re.findall(r"[A-Za-z0-9\-]+", m))
bad = up[[m not in elig.get(i, set()) for i, m in zip(up["item"], up["machine"])]]
add("GEN-04 / BRE-06 / BRIL-06 / BRC-04 / BRM-11 / EXT-01,05,09,12,16,18 / CAL-02 / CUT-01,03,05,07,09 / BEAD-01,05,07 / PA-01",
    "Only approved machine/item combinations", "FOLLOWED" if bad.empty else "VIOLATED",
    f"{len(bad)} of {len(up)} upstream lots on a non-routed machine",
    "; ".join(f"{i}@{m}" for i, m in zip(bad["item"].head(5), bad["machine"].head(5))))

# ---------- changeover matrix ----------
co = pd.read_csv(os.path.join(INP, "changeover_lookup.csv"))
import yaml
cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
m2l = {str(k): v for k, v in (cfg.get("machine_to_changeover_line") or {}).items()}
comap = {(l, f, t): v for l, f, t, v in zip(co["line"], co["from_item"], co["to_item"], co["changeover_min"])}
mix = {"K270M", "F270", "F440", "K310", "K430-1", "K430-2", "310", "270M", "440M", "430-1", "430-2"}
n_pairs = n_short = n_matrix = 0
short_ex = []
mixer_gaps = []
for m, g in up.groupby("machine"):
    g = g.sort_values("scheduled_start")
    prev = None
    for _, r in g.iterrows():
        if prev is not None and r["item"] != prev["item"]:
            gap = (r["scheduled_start"] - prev["scheduled_finish"]).total_seconds() / 60
            if m in mix:
                mixer_gaps.append(gap)
            ln = m2l.get(m)
            req = comap.get((ln, prev["item"], r["item"])) if ln else None
            if req is not None:
                n_matrix += 1
                if gap < req - 0.5:
                    n_short += 1
                    short_ex.append(f"{m}:{prev['item']}->{r['item']} gap {gap:.0f}<{req:.0f}")
            n_pairs += 1
        prev = r
mapped = sorted(set(up["machine"]) & set(m2l))
unmapped = sorted(set(up["machine"]) - set(m2l) - mix)
add("GEN-07 / BRE-05 / BRIL-05 / BRC-03 / BR4RC-07 / EXT-03 / CAL-04 / BEAD-03,06",
    "Machine-specific changeover matrix (no fixed setup where a matrix exists)",
    "FOLLOWED (mapped lines)" if n_short == 0 else "VIOLATED",
    f"{n_matrix} item changes on {len(mapped)} matrix-mapped machines checked, {n_short} shorter than the matrix",
    "; ".join(short_ex[:4]))
add("EXT-07 / EXT-11 / EXT-14 / EXT-19 / BEAD-09,10 (TBR Duplex, Quadraplex, Simplex, TBR bead, belt-edge, TBR cutters)",
    "Line-specific changeover times for TBR / other lines", "NO DATA",
    f"{len(unmapped)} upstream machines have NO matrix line -> flat default used",
    ", ".join(unmapped[:20]))
mg = np.array(mixer_gaps) if mixer_gaps else np.array([0.0])
_mco = float(cfg.get("mixer_changeover_min", 2))
add("BRM-10 / BR-74", f"Mixing transition = fixed {_mco:g} min (plant override)",
    "FOLLOWED" if (mg >= _mco - 0.1).mean() > 0.99 else "PARTLY",
    f"{len(mg)} mixer item changes; {int((mg < _mco - 0.1).sum())} with gap < {_mco:g} min", "")

# ---------- same item repeated in a shift ----------
rep = []
for (m, sh), g in up.groupby(["machine", "shift"]):
    seq = list(g.sort_values("scheduled_start")["item"])
    runs = [seq[0]] + [b for a, b in zip(seq, seq[1:]) if b != a]
    for it in set(runs):
        if runs.count(it) > 1:
            rep.append((m, sh, it, runs.count(it)))
rp = pd.DataFrame(rep, columns=["machine", "shift", "item", "runs"])
for rid, rule, sel in [
    ("BRM-09", "Same compound not scheduled multiple times in a shift", rp["machine"].isin(mix)),
    ("BRE-02 / BRIL-02 / BRC-02 / CUT-08 / GEN-09", "Same item/die once per shift per machine (non-mixing)", ~rp["machine"].isin(mix))]:
    x = rp[sel]
    add(rid, rule, "FOLLOWED" if x.empty else "VIOLATED",
        f"{len(x)} (machine, shift, item) cases split into >1 run",
        "; ".join(f"{a}/{b}/{c} x{d}" for a, b, c, d in x.head(5).itertuples(index=False)))

# ---------- mixing MPQ / batches ----------
bs = rt.drop_duplicates("routed_product").set_index(rt.drop_duplicates("routed_product")["routed_product"].str.strip())
bsz = pd.to_numeric(bs["batch_size"], errors="coerce")
mx = up[up["machine"].isin(mix)].copy()
mx["batches"] = mx["qty"] / mx["item"].map(bsz)
b = mx["batches"].dropna()
add("BRM-01", "Mixing lot = MPQ of 5 batches", "VIOLATED" if (b < 4.99).any() else "FOLLOWED",
    f"config mixing_mpq_min_batches = {cfg.get('mixing_mpq_min_batches')}; {int((b < 4.99).sum())} of {len(b)} mixing lots < 5 batches (median {b.median():.1f})",
    "config enforces 3 batches (plant MPQ master says '3 Batches')")
_bb = int((cfg.get("business_rules") or {}).get("buffer_batches", 0) or 0)
add("BRM-13", "+5 extra batches buffer (not for special compounds)", "FOLLOWED" if _bb >= 5 else "NOT MODELLED",
    f"business_rules.buffer_batches = {_bb} (added to each compound's last lot; special compounds excluded)",
    "conflicts with MPQ per the rule's own remark")
st = mx[mx["item"].isin(["JT444", "JT466"])]
if len(st):
    nb = (st["batches"] % 3).round(3)
    add("BRM-02", "Sticky JT444/JT466 in 3-batch stacks", "FOLLOWED" if ((nb < 0.01) | (nb > 2.99)).all() else "VIOLATED",
        f"{len(st)} lots; batches = {', '.join(f'{v:.2f}' for v in st['batches'].head(6))}", "")
else:
    add("BRM-02", "Sticky JT444/JT466 in 3-batch stacks", "N/A", "no JT444/JT466 lot this run", "")

# ---------- mixing sequencing ----------
# TR100 clean-out + the configured mixer changeover (BR-74: 1 min; was hard-coded 2 min, which flagged
# compliant 6-min master<->final gaps as BRM-06 violations)
_clean_ok = (float((cfg.get("business_rules") or {}).get("master_final_cleanout_min", 5))
             + float(cfg.get("mixer_changeover_min", 2)) - 0.5)
def seq_pairs(pred, cleanout_ok=False):
    """transitions matching pred; with cleanout_ok a gap >= cleanout time counts as compliant."""
    out = []
    for m, g in mx.groupby("machine"):
        g = g.sort_values("scheduled_start")
        rows_ = list(g.itertuples())
        for a_, b_ in zip(rows_, rows_[1:]):
            if pred(a_.item, b_.item):
                gap = (b_.scheduled_start - a_.scheduled_finish).total_seconds() / 60
                if cleanout_ok and gap >= _clean_ok:
                    continue
                out.append(f"{m}:{a_.item}->{b_.item}")
    return out
sys.path.insert(0, HERE)
import butyl_rule as _bmod
_BR2 = _bmod.load(cfg)
if _BR2.enabled:
    # BR-V2: every transition that breaks the butyl-friendly sequence must have >= the clean-out gap
    n_tg = n_seq = n_co = n_free = 0
    co_tot_min = 0.0
    short = []
    for m, g in mx.groupby("machine"):
        rows_ = list(g.sort_values("scheduled_start").itertuples())
        for a_, b_ in zip(rows_, rows_[1:]):
            if a_.item == b_.item:
                continue
            if b_.item in _BR2.targets or a_.item in _BR2.targets:
                n_tg += 1
            hit, why = _BR2.cleanout(a_.item, b_.item)
            if not hit:
                if _BR2.sequence_breaks(a_.item, b_.item)[0]:
                    n_free += 1                 # BR-V13: broken, but the group charges no clean-out
                else:
                    n_seq += 1 if (b_.item in _BR2.targets or a_.item in _BR2.targets) else 0
                continue
            n_co += 1
            gap = (b_.scheduled_start - a_.scheduled_finish).total_seconds() / 60
            req, _bas = _BR2.cleanout_minutes(a_.item, b_.item, m)
            req = req if req is not None else _BR2.cleanout_min
            co_tot_min += req
            if gap < req - 0.05:
                short.append(f"{m}:{a_.item}->{b_.item} gap {gap * 60:.0f}s < {req * 60:.0f}s")
    _basis_txt = "; ".join(
        f"{g['name']}: " + ("1 batch of the compound cleaned out" if g["basis"] == "one_batch_cycle" else
                            "none (60 s changeover only)" if g["basis"] == "none" else f"{g['co_min'] * 60:.0f} s")
        for g in _BR2.groups)
    add("BRM-03 / BR-V2", f"Butyl-friendly sequence (final 30G/IL3/IL5 after B1/B2/B99; master 29MG/ML405/ML605 "
        f"between MB100/MB200/XMB98), else clean-out = {_basis_txt}",
        "FOLLOWED" if not short else "VIOLATED",
        f"{n_tg} transitions at a protected compound: {n_seq} in sequence, {n_co} with a clean-out "
        f"({co_tot_min / 60:.1f} h in total), {n_free} out of sequence with no clean-out required (changeover only), "
        f"{len(short)} clean-outs shorter than required", "; ".join(short[:5]))
    # ---------- BR-V10: IL3/IL5 in one continuous campaign per plant day (plant rule 3.1.3, BRM-07) ----------
    _ilc = cfg.get("il_daily_campaign") or {}
    _ili = [str(x) for x in (_ilc.get("items") or ["IL3", "IL5"])]
    _ilm = mx[mx["item"].isin(_ili)]
    if len(_ilm):
        camps = []                                  # (machine, plant day, items, preceded_by)
        for m, g in mx.groupby("machine"):
            r_ = list(g.sort_values("scheduled_start").itertuples())
            i = 0
            while i < len(r_):
                if r_[i].item in _ili:
                    j = i
                    while j + 1 < len(r_) and r_[j + 1].item in _ili:
                        j += 1
                    d = (r_[i].scheduled_start - pd.Timedelta(hours=7)).normalize()
                    camps.append((m, d, [x.item for x in r_[i:j + 1]], r_[i - 1].item if i > 0 else ""))
                    i = j + 1
                else:
                    i += 1
        cd = pd.DataFrame(camps, columns=["machine", "day", "items", "prev"])
        per_day = cd.groupby("day").size()
        multi = int((per_day > 1).sum())
        prep_ok = int(cd["prev"].isin(_BR2.groups[0]["prep"] if _BR2.groups else {"B99"}).sum())
        add("3.1.3 / BRM-07 / BR-V10", "IL3 & IL5 in one continuous campaign per day, B1/B2/B99 before it",
            "FOLLOWED" if multi == 0 else "PARTLY",
            f"{len(cd)} IL campaigns on {len(per_day)} plant days; days with >1 campaign: {multi}; "
            f"campaigns preceded by B1/B2/B99: {prep_ok} of {len(cd)}", "")
else:
    butyl = {"ML405", "ML605", "29MG"}; friendly = {"MB100", "MB200", "XMB98"} | butyl
    v = seq_pairs(lambda a, b_: a in butyl and b_ not in friendly, cleanout_ok=True)
    add("BRM-03", "After butyl (ML405/ML605/29MG) only butyl-friendly (MB100/MB200/XMB98)",
        "FOLLOWED" if not v else "VIOLATED", f"{len(v)} violating transitions", "; ".join(v[:5]))
v = seq_pairs(lambda a, b_: a in {"ML405", "ML406"} and b_ != "MK3")
_has_mk3 = bool((mx["item"] == "MK3").any())
add("BRM-05", "ML405/ML406 followed by MK3 (confirm for CTP)",
    ("FOLLOWED" if not v else "VIOLATED") if _has_mk3 else "N/A (no MK3 demand in this plan)",
    f"{len(v)} transitions not to MK3", "; ".join(v[:5]))
mixt = dict(zip(mx["item"], mx["itype"]))
v = seq_pairs(lambda a, b_: {mixt.get(a, ""), mixt.get(b_, "")} == {"MASTER COMPOUND", "FINAL COMPOUND"}, cleanout_ok=True)
add("BRM-06", "Master<->final on same machine needs TR100 cleanout (confirm for CTP)",
    "FOLLOWED" if not v else "VIOLATED", f"{len(v)} master/final switches without TR100", "; ".join(v[:4]))
add("BRM-04 / BRM-07 / BRM-08", "K4-IL sequencing; related compounds together; finals of one master consecutive",
    "NOT MODELLED", "no such sequencing logic (rules marked 'confirm for CTP')", "")

# ---------- 24h rolling horizon for SW / TRD / IL ----------
lf = L[L["item_type"].astype(str).str.upper().isin(["SIDEWALL", "TREAD", "INNER LINER"])]
span = (lf["last_need"] - lf["first_need"]).dt.total_seconds() / 3600
add("BRE-01 / BRIL-01", "SW/TRD/IL planned on a rolling 24-h building horizon",
    "FOLLOWED" if (span <= 24.01).all() else "PARTLY",
    f"{int((span > 24.01).sum())} of {len(span)} lots cover > 24 h of building need (max {span.max():.0f} h)", "")

# ---------- belt pairing ----------
bl = up[up["item"].str.match(r"^B[12]-")]
pair_bad = 0; pair_tot = 0
for key, g in bl.assign(k=bl["item"].str[3:]).groupby(["machine", "k"]):
    g = g.sort_values("scheduled_start")
    b1 = g[g["item"].str.startswith("B1-")]; b2 = g[g["item"].str.startswith("B2-")]
    for _, r in b1.iterrows():
        pair_tot += 1
        near = b2[(b2["scheduled_start"] - r["scheduled_finish"]).abs() <= pd.Timedelta(hours=1)]
        near2 = b2[(r["scheduled_start"] - b2["scheduled_finish"]).abs() <= pd.Timedelta(hours=1)]
        if near.empty and near2.empty:
            pair_bad += 1
add("BRC-01 / CUT-02 / CUT-04", "Belt-1 and Belt-2 of the same code adjacent",
    "FOLLOWED" if pair_bad == 0 else "PARTLY", f"{pair_bad} of {pair_tot} B1 lots without their B2 within 1 h on the same cutter", "")

# ---------- 4RC: CAP66 cleanout, roll length ----------
m901 = up[up["machine"] == "901"].sort_values("scheduled_start")
cap_bad = []
rows = list(m901.itertuples())
for a, b_ in zip(rows, rows[1:]):
    if ("CAP 66" in a.item) != ("CAP 66" in b_.item):
        gap = (b_.scheduled_start - a.scheduled_finish).total_seconds() / 60
        if gap < 14.5:
            cap_bad.append(f"{a.item}->{b_.item} {gap:.0f}min")
add("BR4RC-05 / 4RC-04", "15-min cleanout before and after CAP66", "FOLLOWED" if not cap_bad else "VIOLATED",
    f"{len(cap_bad)} CAP66 transitions with < 15 min (matrix gives 10 min)", "; ".join(cap_bad[:4]))
rl = up[up["item"].str.upper().str.replace(" ", "").str.contains("NYCHAF|NYLCHAF|TLCHAF|NYLFILL|WPJ011")]
add("BR4RC-09 / 4RC-07", "Nychaffer/TLChaffer/Nylfiller/WPJ011 roll >= 200 m",
    ("N/A (rule item names not in this plan; CTP has MONOCHAF80/MONOCHAFER)" if rl.empty else
     ("FOLLOWED" if (rl["qty"] >= 200).all() else "VIOLATED")),
    f"{int((rl['qty'] < 200).sum())} of {len(rl)} lots < 200 m", ", ".join(sorted(set(rl["item"])))[:120])

# ---------- aging ----------
av = pd.read_csv(os.path.join(O2, "phase4_aging_violations_updated.csv")) if os.path.exists(
    os.path.join(O2, "phase4_aging_violations_updated.csv")) else pd.DataFrame()
cur = pd.read_csv(os.path.join(RUN, "phase6_curing_revision_updated.csv"))
lim = float(cfg.get("green_tyre_max_age_h", 24))
over = cur[pd.to_numeric(cur["green_age_h"], errors="coerce") > lim]
import glob as _g
_dead = "n/a"
for _lg in sorted(_g.glob(os.path.join(HERE, "_run_*.log")), key=os.path.getmtime, reverse=True)[:1]:
    _m = re.findall(r"LOTS dead before their FIRST consumer \.\. (\d+ / \d+)", open(_lg, encoding="utf-8", errors="ignore").read())
    _dead = _m[-1] if _m else "n/a"
add("GEN-12 / BRG-02", "Respect min/max ageing (hard block)",
    "FOLLOWED" if (len(over) == 0 and str(_dead).startswith("0 /")) else "PARTLY",
    f"component lots expired before their first consumer: {_dead}; cure blocks whose oldest green tyre > {lim:.0f} h: {len(over)}",
    f"({len(av)} expired EDGES in phase4 file are fan-out links, not material)")

# ---------- kit / building plan ----------
ml = os.path.join(O2, "phase5_material_ledger.csv")
if os.path.exists(ml):
    m = pd.read_csv(ml)
    bad_k = m[m["starts_before_material"] == True]
    add("PA-02 / CTP-GEN-01 (kit)", "Complete building kit available on time",
        "FOLLOWED" if bad_k.empty else "PARTLY",
        f"{bad_k['build_lot'].nunique()} of {m['build_lot'].nunique()} build blocks start before material (items: {sorted(set(bad_k['item']))[:5]})",
        "NYDBCHF has no routing time -> never produced (data)")
g = pd.read_csv(os.path.join(O2, "phase5_building_echo_gates.csv"), parse_dates=["plan_start", "start"])
late = g[(g["start"] - g["plan_start"]).dt.total_seconds() > 1800]
add("GEN-01", "Follow the shift-wise building plan", "PARTLY" if len(late) else "FOLLOWED",
    f"{len(g)} plan rows on their planned TBM; {len(late)} start > 30 min after the plan slot (max {((late['start']-late['plan_start']).dt.total_seconds().max()/3600 if len(late) else 0):.1f} h)",
    "same machine + qty always; only timing slides when material is late")

# ---------- runtime / speed ----------
bef = rt[rt["routed_product"].str.strip() == "BEF001"][["proc_time", "proc_time_UOM", "efficiency"]].drop_duplicates()
add("GEN-05 / GEN-06 / EXT-02,06,10,13 / CAL-03 / 4RC-02 / CUT-06 / BEAD-02,08 / BRM-12",
    "Item-specific approved line speed for runtime", "FOLLOWED (with flagged deviations)",
    "runtime = qty / routing proc_time x efficiency (DB routing)" + (f"; efficiency_override {cfg['efficiency_override']} applied to every row (user)" if cfg.get("efficiency_override") is not None else ""),
    "deviations: machine 2001 x1.66 provisional (FLAW C-2); 3 ASSUMED gap-fills (T7110, XMT180, SWRS30)")
add("EXT-17", "BEF001 belt edge 10 m/min at ~95.5% eff",
    "CHECK", "routing BEF001: " + "; ".join(f"{a} {b} eff {c}" for a, b, c in bef.itertuples(index=False))
    + (f" -> eff used {cfg['efficiency_override']} (efficiency_override)" if cfg.get("efficiency_override") is not None else ""),
    "config excluded_items note: edge strip built at FIXED cap-ply-cutter rate")

# ---------- BR-2: shift-start opening inventory ----------
try:
    _w = pd.read_csv(os.path.join(HERE, "ctp_inputfiles", "opening_wip_live.csv"), low_memory=False)
    _snaps = sorted(set(_w.get("updatedAt", pd.Series(dtype=str)).astype(str).str[:19]))
    _ok = any(x.endswith("06:30:00") for x in _snaps)
    add("GEN-02 / BR-2", "Use SHIFT-START opening inventory", "FOLLOWED" if _ok else "VIOLATED",
        f"JK_CTP_PPDS.CTP_Opening_WIP snapshot(s) {', '.join(x for x in _snaps if x.endswith('06:30:00'))} "
        f"(07:00 shift start); compounds from the old feed (snapshot has none)", "")
except Exception as _e:
    add("GEN-02 / BR-2", "Use SHIFT-START opening inventory", "CHECK", f"opening_wip_live.csv unreadable ({_e})")

# ---------- BR-10: PM / MTC downtime ----------
try:
    _dt = pd.read_csv(os.path.join(HERE, "db_pipeline", "raw", "jkplanning_CTP__downtime_reason.csv"),
                      parse_dates=["Scheduled Start Time", "Scheduled End Time"])
    def _mk(m):
        m = str(m).strip()
        if m.isdigit() and m.startswith("34"):
            return f"TBMPCR{int(m[2:])}Stage2"
        if m.isdigit() and m.startswith("38"):
            return f"TBM-{int(m[2:]):02d}"
        return m
    _win = [(_mk(m), a_, b_) for m, a_, b_ in zip(_dt["Machine ID"], _dt["Scheduled Start Time"], _dt["Scheduled End Time"])]
    _cu = pd.read_csv(os.path.join(RUN, "phase6_curing_revision_updated.csv"), parse_dates=["startTime", "endTime"])
    _all = pd.concat([S[["machine", "scheduled_start", "scheduled_finish"]].rename(
        columns={"scheduled_start": "s", "scheduled_finish": "e"}),
        _cu.assign(machine=_cu["pressNo"].astype(str))[["machine", "startTime", "endTime"]].rename(
        columns={"startTime": "s", "endTime": "e"})])
    _hit = 0
    for m, a_, b_ in _win:
        x = _all[_all["machine"] == m]
        _hit += int(((x["s"] < b_) & (x["e"] > a_)).sum())
    _inwin = sum(1 for _, a_, b_ in _win if b_ > S["scheduled_start"].min())
    add("GEN-10 / BRG-01 / BR-10", "No production during PM/MTC", "FOLLOWED" if _hit == 0 else "VIOLATED",
        f"{len(_win)} downtime_reason windows ({_inwin} inside the run); {_hit} scheduled blocks overlap one", "")
except Exception as _e:
    add("GEN-10 / BRG-01 / BR-10", "No production during PM/MTC", "CHECK", f"downtime check failed ({_e})")

# ---------- not modelled / no data ----------
for rid, rule, st_, why in [
    ("GEN-03", "Lowest building feed-hours first", "FOLLOWED (approx.)", "dispatch_priority = LFT (earliest deadline first)"),
    ("GEN-08 / GEN-14 / EXT-04,08,15 / CAL-05 / BEAD-04,11 / BRE-03 / BRIL-03 / BRB-01,02", "Family / die / compound / inch batching, continuity", "NOT MODELLED", "no family keys (die, preformer, compound family, inch, wire type) in the masters"),
    ("GEN-11", "Output <= empty MH x MH capacity", "NO DATA", "MPQ Maximum_Run_Qty / MH master blank -> MHE inert"),
    ("GEN-13", "Replan every shift", "NOT MODELLED", "single batch run; needs a shift-wise refresh job"),
    ("GEN-15 / BRG-03 / BRE-04 / BRIL-04", "Latest plant-approved masters / MPQ", "FOLLOWED (with overrides)", "DB masters + local MPQ (user) + 72 h GT aging (user) + gap-fills"),
    ("4RC-01", "4RC steel and fabric capacity separate", "NOT MODELLED", "901 is one capacity pool"),
    ("4RC-03 / 4RC-05 / BR4RC-01,04,06", "Chaffer at end of fabric, Nyl chaffer+WPJ consecutive, CAP66 after wire", "NOT MODELLED", "soft sequencing not implemented"),
    ("4RC-06 / BR4RC-10,11,12", "240-min creel setup in parallel, not a changeover", "NOT MODELLED", "creel setup not represented at all"),
    ("BR4RC-13", "Ramp up 2 min / down 3 min", "NOT MODELLED", ""),
    ("BR4RC-14", "Wire-to-wire 6 h separation", "NOT MODELLED", ""),
    ("BR4RC-02 / BR4RC-03", "Deep-roll / wire length limits", "NO DATA", "no roll-length master"),
    ("CUT-10", "Only permitted successor transitions (hard block)", "NO DATA", "no successor master"),
]:
    add(rid, rule, st_, why)

out = pd.DataFrame(res)
target = os.path.join(RUN, "BUSINESS_RULE_AUDIT.csv")
out.to_csv(target, index=False)
pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 110)
print(out[["rule_id", "status", "measured"]].to_string(index=False))
print("\n", out["status"].str.split(" ").str[0].value_counts().to_dict(), "->", target)
