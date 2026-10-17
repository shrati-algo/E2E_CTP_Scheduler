#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quintoplex_checks.py -- tread extruder rules on a finished run (2026-10-06).

    python quintoplex_checks.py <run_output_dir> [--rules quintoplex_rules|duplex_rules]
                                [--baseline <other run_output_dir>] [--xlsx out.xlsx]

  quintoplex_rules (PCR tread, 1002): BR-18 eligibility, BR-19 line speed, BR-20 matrix A-H, BR-21 same die /
      compatible compound family (matrix class A die only / B same compound type) together
  duplex_rules (TBR tread, 1801):     BR-22 eligibility, BR-23 line speed, BR-24 matrix (die 4.76 / die+preformer
      4.92 / compound 12.5-14; a pair missing from the matrix is charged changeover_missing_pair_min), BR-25 same
      compound FAMILY (BOM family_compounds, e.g. T636 / T639) at a stretch
  eligibility  every lot on the machine is routed to it; no routed item runs elsewhere
  line speed   run time = metres / (routing proc_time M/MIN x efficiency) +- 1 min
  changeover   gap before every item change >= matrix time; class letter per transition
  sequencing   a switch is followed when the next lot is compatible, or when coverage did not permit a compatible
               lot next (none had its inputs finished, was within max_pull_h, and could go first without the
               current lot finishing after its first tyre build)
Masters: last download of JK_CTP_PPDS (db_pipeline/raw): CTP_Master_Routing, CTP_Master_ChangeoverMatrix,
CTP_Master_BOM.
"""
import os
import re
import sys

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "db_pipeline", "raw")
PROFILE = {"quintoplex_rules": {"rules": ("BR-18", "BR-19", "BR-20", "BR-21"), "machine": "1002",
                                "line": "Quintoplex PCR Tread", "compat": "matrix_ab", "fam": ["B715", "B815"]},
           "duplex_rules": {"rules": ("BR-22", "BR-23", "BR-24", "BR-25"), "machine": "1801",
                            "line": "Duplex TBR Tread", "compat": "family", "fam": ["T636", "T639"]},
           "triplex_rules": {"rules": ("BR-26", "BR-27", "BR-28", "BR-28"), "machine": "1001",
                             "line": "Triplex PCR SW", "compat": "family", "fam": []},
           "quadraplex_rules": {"rules": ("BR-29", "BR-30", "BR-31", "BR-32"), "machine": "1803",
                                "line": "Quadraplex TBR SW", "compat": "family", "fam": []}}


def load(rk):
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    qr, pf = cfg.get(rk) or {}, PROFILE[rk]
    M, line = str(qr.get("machine", pf["machine"])), str(qr.get("matrix_line", pf["line"]))
    miss_min = (cfg.get("changeover_missing_pair_min") or {}).get(line)
    rt = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_Routing.csv"), dtype=str, low_memory=False)
    rt["ms"] = rt["machines"].astype(str).map(lambda s: {x.strip().strip('"') for x in re.split(r"[,;]", s)})
    rq = rt[rt["ms"].map(lambda s: M in s)].copy()
    rq["k"] = rq["routed_product"].str.strip()
    spd = pd.to_numeric(rq["proc_time"], errors="coerce").groupby(rq["k"]).first()
    eff = pd.to_numeric(rq["efficiency"], errors="coerce").groupby(rq["k"]).first().fillna(1.0)
    if cfg.get("efficiency_override") is not None:      # v37: same efficiency as the scheduler
        eff = eff * 0 + float(cfg["efficiency_override"])
    mx = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_ChangeoverMatrix.csv"), dtype=str, low_memory=False)
    mx = mx[mx["machine"].astype(str).str.strip() == line]
    mx["t"] = pd.to_numeric(mx["changeover_time_min"], errors="coerce")
    rows_any = set(zip(mx["from_MaterialCode_O"].str.strip(), mx["to_MaterialCode_O"].str.strip()))
    mx = mx.dropna(subset=["t"])                                   # blank time = not in the matrix
    key = list(zip(mx["from_MaterialCode_O"].str.strip(), mx["to_MaterialCode_O"].str.strip()))
    tmin = dict(zip(key, mx["t"]))
    let = dict(zip(key, mx["changeover_letter"].fillna("")))
    bom = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_BOM.csv"), dtype=str, low_memory=False,
                      usecols=["Parent", "child", "child_quantity"])
    bom["Parent"], bom["child"] = bom["Parent"].str.strip(), bom["child"].str.strip()
    fam = set(str(x).upper() for x in (qr.get("family_compounds") or qr.get("cushion_compounds") or pf["fam"]))
    cu = bom[bom["child"].str.upper().isin(fam)].drop_duplicates("Parent").set_index("Parent")["child"]
    _ity = pd.read_csv(os.path.join(HERE, "inputs", "itemtype_master.csv"), dtype=str)
    ity = dict(zip(_ity["ItemCode"].str.strip(), _ity["ItemType"].astype(str).str.strip().str.upper()))
    mbt = (cfg.get("changeover_missing_pair_by_type") or {}).get(line)
    if str(qr.get("family_mode", "")).lower() == "itype":              # Quadraplex: sidewall vs shoulder pad
        cu = pd.Series(ity)
        fam = {"SIDEWALL / SHOULDER PAD"}
    if str(qr.get("family_mode", "")).lower() == "bom_set":            # Triplex: family = the full compound set
        cu = bom.drop_duplicates(["Parent", "child"]).groupby("Parent")["child"].agg(lambda x: "+".join(sorted(set(x))))
        fam = {"BOM compound set"}
    bom["q"] = pd.to_numeric(bom["child_quantity"], errors="coerce").fillna(0)
    main = bom.sort_values("q", ascending=False).drop_duplicates("Parent").set_index("Parent")["child"]
    allrouted = set(rq["k"])
    return dict(M=M, line=line, spd=spd, eff=eff, tmin=tmin, let=let, cu=cu, main=main, allrouted=allrouted,
                bom=bom, miss_min=miss_min, rows_any=rows_any, ity=ity, mbt=mbt, pull_h=float(qr.get("max_pull_h", 12)), pf=pf, fam=sorted(fam))


def seq(run, D):
    S = pd.read_csv(os.path.join(run, "phase5_schedule_updated.csv"),
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
    S["item"] = S["item"].astype(str).str.strip()
    L = pd.read_csv(os.path.join(run, "phase2_lots_updated.csv"), low_memory=False).set_index("lot_id")
    q = S[S["machine"].astype(str).str.strip() == D["M"]].sort_values("scheduled_start").reset_index(drop=True)
    q["metres"] = q["lot_id"].map(L["lot_qty"])
    q["run_min_routing"] = q["metres"] / (q["item"].map(D["spd"]) * q["item"].map(D["eff"]))
    q["run_min_sched"] = (q["scheduled_finish"] - q["scheduled_start"]).dt.total_seconds() / 60
    q["prev_item"] = q["item"].shift()
    q["gap_min"] = (q["scheduled_start"] - q["scheduled_finish"].shift()).dt.total_seconds() / 60
    chg = q["prev_item"].notna() & (q["prev_item"] != q["item"])
    q["class"] = [(D["let"].get((a, b)) or ("missing" if (a, b) not in D["tmin"] else "?")) if c
                  else ("same die" if isinstance(a, str) else "") for a, b, c in zip(q["prev_item"], q["item"], chg)]
    def _miss(a, b):
        if D.get("mbt"):                                   # by item type (Quadraplex SW <-> SP)
            return float(D["mbt"]["same_type"] if D["ity"].get(a) == D["ity"].get(b) else D["mbt"]["other_type"])
        return float(D["miss_min"] or 0)
    q["co_min"] = [float(D["tmin"].get((a, b), _miss(a, b))) if c else 0.0
                   for a, b, c in zip(q["prev_item"], q["item"], chg)]
    q["main_compound"] = q["item"].map(D["main"])
    q["family"] = q["item"].map(D["cu"])
    q["plant_day"] = (q["scheduled_start"] - pd.Timedelta(hours=7)).dt.normalize()
    return S, q


def summary(q, D):
    c = q[q["class"].isin(list("ABCDEFGH"))]
    cs = q["family"].dropna()
    return {"lots": len(q), "item changes": int((q["prev_item"].notna() & (q["prev_item"] != q["item"])).sum()),
            "same-die follow-ons": int((q["class"] == "same die").sum()),
            "changeover min": round(q["co_min"].sum(), 1),
            **{f"class {k}": int((c["class"] == k).sum()) for k in "ABCDEFGH"},
            "pairs missing in matrix": int((q["class"] == "missing").sum()),
            f"family {'/'.join(D['fam'])} switches": int((cs != cs.shift()).sum() - 1) if len(cs) else 0,
            "main-compound switches": int((q["main_compound"] != q["main_compound"].shift()).sum() - 1)}


def _toks(s):
    return [t for t in re.findall(r"([^;()]+)\(", str(s)) if not t.startswith(("OPENING_WIP", "RAW_MATERIAL", "SHORT"))]


def seq_rule(run, q, D):
    """Sequencing compliance per switch. FOLLOWED when the next lot is compatible -- same die, or (Quintoplex) matrix
    class A / B, or (Duplex) the same BOM compound family -- or when coverage did not permit a compatible lot next."""
    pull_h = D["pull_h"]
    xs = [f for f in os.listdir(os.path.dirname(os.path.abspath(run))) if f.endswith(".xlsx") and "_SCHEDULE_" in f
          and "UNIFIED" not in f]
    X = os.path.join(os.path.dirname(os.path.abspath(run)), xs[0])
    up = __import__("schedule_changeovers").read_sched(X, "Upstream")       # PCR / TBR / shared, lot rows
    bd = __import__("schedule_changeovers").read_sched(X, "Building")
    up_end = pd.to_datetime(up["end_time"]).groupby(up["lot_id"].astype(str)).max()
    inputs = up.groupby(up["lot_id"].astype(str))["consumption_id"].agg(lambda s: {t for v in s for t in _toks(v)})
    first_use = {}
    for st, cid in zip(pd.to_datetime(bd["start_time"]), bd["consumption_id"]):
        for t in _toks(cid):
            if t not in first_use or st < first_use[t]:
                first_use[t] = st

    def compat(a, b):
        if a == b:
            return True
        if D["pf"]["compat"] == "family":
            fa, fb = D["cu"].get(a), D["cu"].get(b)
            return fa is not None and fa == fb
        return D["let"].get((a, b)) in ("A", "B")
    rows, recs = [], q.to_dict("records")
    for i in range(1, len(recs)):
        x, y = recs[i - 1], recs[i]
        ok = compat(x["item"], y["item"])
        alt = None
        if not ok:
            for z in recs[i + 1:]:
                if (z["scheduled_start"] - y["scheduled_start"]).total_seconds() > pull_h * 3600:
                    break
                if not compat(x["item"], z["item"]):
                    continue                               # not compatible with the previous item either
                rdy = max([up_end.get(p, pd.Timestamp.min) for p in inputs.get(str(z["lot_id"]), ())] or [pd.Timestamp.min])
                if rdy > y["scheduled_start"]:
                    continue                               # its compounds were not ready yet
                fu = first_use.get(str(y["lot_id"]))
                dz = (z["scheduled_finish"] - z["scheduled_start"])
                if fu is not None and y["scheduled_finish"] + dz > fu:
                    continue                               # the current lot would be late for its build
                alt = z["lot_id"]
                break
        rows.append({"plant_day": y["plant_day"], "from_item": x["item"], "from_family": x["family"],
                     "to_lot": y["lot_id"], "to_item": y["item"], "to_family": y["family"], "class": y["class"],
                     "co_min": y["co_min"], "compatible": ok, "compatible_lot_available": alt,
                     "followed": ok or alt is None})
    return pd.DataFrame(rows)


def missing_pairs(D, q):
    """every ordered pair of items routed to the machine that the matrix lacks (or has without a time)"""
    items = sorted(D["allrouted"])
    b = D["bom"][D["bom"]["Parent"].isin(items)].drop_duplicates(["Parent", "child"])
    comp = b.groupby("Parent").apply(lambda g: ", ".join(f"{c} {float(v):g}" for c, v in zip(g["child"], g["q"])),
                                     include_groups=False)
    used = q[q["class"] == "missing"].groupby(["prev_item", "item"]).size()
    rows = []
    for a in items:
        for c in items:
            if a != c and (a, c) not in D["tmin"]:
                rows.append({"from_item": a, "to_item": c,
                             "matrix_table": ("row present, time and class blank" if (a, c) in D["rows_any"]
                                              else "no row"),
                             "from_compounds_kg": comp.get(a, ""),
                             "to_compounds_kg": comp.get(c, ""), "from_family": D["cu"].get(a, ""),
                             "to_family": D["cu"].get(c, ""), "from_main": D["main"].get(a, ""),
                             "to_main": D["main"].get(c, ""),
                             "charged_min": (D["mbt"]["same_type"] if D["ity"].get(a) == D["ity"].get(c) else D["mbt"]["other_type"])
                             if D.get("mbt") else D["miss_min"],
                             "times_in_this_schedule": int(used.get((a, c), 0))})
    return pd.DataFrame(rows)


def main(run, base=None, out=None, rk="quintoplex_rules"):
    D = load(rk)
    M, (r1, r2, r3, r4) = D["M"], D["pf"]["rules"]
    S, q = seq(run, D)
    rows = []

    def add(rule, what, occ, ok, note=""):
        rows.append({"rule": rule, "check": what, "occurrences": int(occ), "followed": int(ok),
                     "compliance_%": round(100 * ok / occ, 2) if occ else None, "note": note})
    add(f"{r1} eligibility", f"lot on {M} is routed to {M}", len(q), int(q["item"].isin(D["allrouted"]).sum()))
    elsewhere = S[S["item"].isin(D["allrouted"])]
    add(f"{r1} eligibility", f"routed item lot runs on {M}", len(elsewhere),
        int((elsewhere["machine"].astype(str).str.strip() == M).sum()))
    chk = q.dropna(subset=["run_min_routing"])
    nospd = sorted(i for i in D["allrouted"] if pd.isna(D["spd"].get(i)))
    add(f"{r2} line speed", "run time = metres / (routing M/MIN x eff) +- 1 min", len(chk),
        int(((chk["run_min_sched"] - chk["run_min_routing"]).abs() <= 1).sum()),
        f"routed items with no speed (not scheduled here unless listed in the sequence): {nospd}" if nospd else "")
    c = q[q["co_min"] > 0]
    nm = int((q["class"] == "missing").sum())
    add(f"{r3} matrix changeover", "gap before an item change >= matrix time", len(c),
        int((c["gap_min"] >= c["co_min"] - 0.5).sum()),
        f"{nm} switches on pairs missing in the matrix, charged {D['miss_min']} min" if nm else "")
    B = seq_rule(run, q, D)
    what = "same die or same compound family" if D["pf"]["compat"] == "family" else "same die or compatible family (A / B)"
    add(f"{r4} sequencing", f"switch is {what}", len(B), int(B["compatible"].sum()))
    add(f"{r4} sequencing", "... or coverage did not permit a compatible lot next", len(B), int(B["followed"].sum()),
        f"{int((~B['followed']).sum())} switches where a compatible lot was available")
    # repeated compound-family changes: per plant day, switches vs the minimum (families that day - 1)
    _dd = []
    for _d, _g in q.groupby("plant_day"):
        _f = list(_g["family"].fillna(_g["item"]))
        _sw = sum(1 for a_, b_ in zip(_f, _f[1:]) if a_ != b_)
        _dd.append((_d, _sw, max(len(set(_f)) - 1, 0)))
    _dd = pd.DataFrame(_dd, columns=["plant_day", "family_switches", "minimum"])
    add(f"{r4} sequencing", "plant day without repeated compound-family changes (switches = families - 1)",
        len(_dd), int((_dd["family_switches"] <= _dd["minimum"]).sum()),
        f"{int(_dd['family_switches'].sum())} family switches vs minimum {int(_dd['minimum'].sum())}")
    R = pd.DataFrame(rows)
    sm = {"this run": summary(q, D)}
    if base:
        sm["baseline"] = summary(seq(base, D)[1], D)
    SM = pd.DataFrame(sm)
    day = q.groupby("plant_day").agg(lots=("lot_id", "size"), changeover_min=("co_min", "sum"),
                                     same_die=("class", lambda s: int((s == "same die").sum())),
                                     family_switches=("family", lambda s: int((s.dropna() != s.dropna().shift()).sum() - 1)
                                                      if s.notna().any() else 0)).reset_index()
    MP = missing_pairs(D, q)
    pd.set_option("display.width", 220)
    print(R.to_string(index=False))
    print(f"\n{r4} sequencing" + (" (vs baseline)" if base else "") + ":")
    print(SM.to_string())
    _chg = (f"{D['mbt']['same_type']:g} / {D['mbt']['other_type']:g} min (same / other item type)" if D.get("mbt")
            else f"{D['miss_min']} min")
    print(f"\npairs of routed {M} items missing in the matrix: {len(MP)} (charged {_chg}); "
          f"used in this schedule: {int(MP['times_in_this_schedule'].sum()) if len(MP) else 0}")
    if out:
        with pd.ExcelWriter(out) as xw:
            R.to_excel(xw, sheet_name="Summary", index=False)
            SM.reset_index().rename(columns={"index": "measure"}).to_excel(xw, sheet_name=f"{r4} sequencing", index=False)
            day.to_excel(xw, sheet_name="By plant day", index=False)
            B.to_excel(xw, sheet_name=f"{r4} switches", index=False)
            MP.to_excel(xw, sheet_name="Missing matrix pairs", index=False)
            q[["plant_day", "lot_id", "item", "main_compound", "family", "scheduled_start", "scheduled_finish",
               "metres", "run_min_sched", "run_min_routing", "prev_item", "class", "co_min", "gap_min"]].to_excel(
                xw, sheet_name=f"{M} sequence", index=False)
        print("wrote", out)
    return R, SM


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], a[a.index("--baseline") + 1] if "--baseline" in a else None,
         a[a.index("--xlsx") + 1] if "--xlsx" in a else None,
         a[a.index("--rules") + 1] if "--rules" in a else "quintoplex_rules")
