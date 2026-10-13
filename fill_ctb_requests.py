#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fill_ctb_requests.py -- fill discard/new/CTB_data_requests.xlsx from the data we hold (2026-09-27).

Every filled cell carries its SOURCE and a STATUS:
  FILLED       plant / DB data (MES line speeds, DB routing, DB aging, DB BOM)
  ESTIMATE     derived (sibling median, scheduler model) -- usable, but needs plant confirmation
  NEEDS PLANT  nothing we hold can answer it
"""
import os
import re
import shutil

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
F = os.path.join(HERE, "discard", "new", "CTB_data_requests.xlsx")
SP_DB = os.path.join(HERE, "db_pipeline", "raw")
bak = F.replace(".xlsx", "_ORIGINAL.xlsx")
if not os.path.exists(bak):
    shutil.copy2(F, bak)
rq = pd.read_excel(bak, sheet_name=None)
norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()

rt = pd.read_csv(os.path.join(HERE, "ctp_inputfiles", "routing_comb.csv"), dtype=str)
rt["rp"] = rt["routed_product"].map(norm)
rt["pt"] = pd.to_numeric(rt["proc_time"], errors="coerce")
bom = pd.read_csv(os.path.join(HERE, "ctp_inputfiles", "bom_comb.csv"), dtype=str)
agm = pd.read_csv(os.path.join(SP_DB, "JK_CTP_PPDS__CTP_Master_Aging.csv"), dtype=str)
agm["k"] = agm["ItemCode"].map(norm)
old = pd.read_csv(os.path.join(HERE, "ctp_inputfiles", "aging_comb.csv"), dtype=str)
old["k"] = old["ItemCode"].map(norm)
wip = pd.read_csv(os.path.join(SP_DB, "jkplanning_CTP__opening_wip.csv"), low_memory=False)
wip["k"] = wip["itemcode"].map(norm)
wip26 = wip[wip["inventory_date"].astype(str).str[:10] == "2026-09-26"]
ls = pd.read_excel(os.path.join(HERE, "ctp_inputfiles", "CTP_Machine_LineSpeed_Filled.xlsx"), sheet_name=None)
lsa = pd.concat([ls["PCR"].assign(line="PCR"), ls["TBR"].assign(line="TBR")], ignore_index=True)
lsa["mid"] = lsa["Machine Id"].astype(str).str.strip()
lsa["typ"] = lsa["Material (Item Type)"].astype(str).str.upper().str.strip()
out = {}

# ---------------- 1 Missing machine rate ----------------
s1 = rq["1 Missing machine rate"].copy()
vals = []
for _, r in s1.iterrows():
    it = norm(r["item"]); rr = rt[rt["rp"] == it]
    v = u = src = ""; st = "NEEDS PLANT"
    have = rr[rr["pt"].notna()]
    mach = sorted(set(re.findall(r"[A-Za-z0-9\-]+", " ".join(rr["machines"].fillna("")))))
    if len(have):
        h = have.iloc[0]
        v, u, src, st = h["pt"], h["proc_time_UOM"], "DB jkt_routing (proc_time present)", "FILLED"
        if str(r["reason"]).startswith("rate is in a master but in a different unit"):
            if str(h["proc_time_UOM"]).upper().startswith("M/"):
                src += f" -- routing already gives the rate in METRES ({h['pt']:g} {h['proc_time_UOM']}, m/c {', '.join(mach)}): the unit mismatch is solved, no cut length needed"
            else:
                src += f" -- routing rate is in PIECES ({h['proc_time_UOM']}); the BOM counts metres -> cut length per piece (m) still needed"
                st = "NEEDS PLANT"
    else:
        # plant MES machine-level average for the same machine + item type
        bt = str(r["bom_type"]).upper().strip()
        m = lsa[lsa["mid"].isin(mach) & (lsa["typ"] == bt) & pd.to_numeric(lsa["PLANT proc_time (fill)"], errors="coerce").notna()]
        if len(m):
            v = float(pd.to_numeric(m["PLANT proc_time (fill)"]).median()); u = m["PLANT proc_time UOM (fill)"].iloc[0]
            src = f"PLANT MES 1-month average for m/c {', '.join(sorted(set(m['mid'])))} / {bt} (CTP_Machine_LineSpeed_Filled.xlsx) -- machine-level, not item-level"
            st = "ESTIMATE"
        else:
            op = rr["operation_name"].iloc[0] if len(rr) else ""
            sib = rt[(rt["operation_name"] == op) & rt["pt"].notna() & rt["machines"].fillna("").map(
                lambda x: bool(set(re.findall(r"[A-Za-z0-9\-]+", x)) & set(mach)))]
            if len(sib):
                uu = sib["proc_time_UOM"].mode().iloc[0]; sib = sib[sib["proc_time_UOM"] == uu]
                v = float(sib["pt"].median()); u = uu
                src = f"median of {sib['rp'].nunique()} items on the same operation ({op}) and machine(s) {', '.join(mach)} in DB routing"
                st = "ESTIMATE"
            else:
                src = f"no rate in DB routing, MES line-speed file or sibling items (operation {op or '-'}, m/c {', '.join(mach) or '-'})"
    vals.append((v, u, ", ".join(mach), src, st))
s1[["our_value", "our_unit", "machines_in_routing", "source", "status"]] = pd.DataFrame(vals, index=s1.index)
out["1 Missing machine rate"] = s1

# ---------------- 2 / 3 Aging ----------------
def aging_cols(df, key="item"):
    rows = []
    for _, r in df.iterrows():
        k = norm(r[key])
        o = old[old["k"] == k]; w = wip26[wip26["k"] == k]
        o_mn = o["MinAging"].iloc[0] if len(o) else ""; o_mx = o["MaxAging"].iloc[0] if len(o) else ""
        w_mn = sorted(set(pd.to_numeric(w["MinAging"], errors="coerce").dropna())) if len(w) else []
        w_mx = sorted(set(pd.to_numeric(w["MaxAging"], errors="coerce").dropna())) if len(w) else []
        rows.append((o_mn, o_mx, ", ".join(f"{x:g}" for x in w_mn), ", ".join(f"{x:g}" for x in w_mx)))
    return pd.DataFrame(rows, index=df.index,
                        columns=["old_jkt_aging_master_Min_h", "old_jkt_aging_master_Max_h",
                                 "opening_wip_Min_h", "opening_wip_Max_h"])
# ---------------- PLANT AGING SPEC (CTP DOC CTE0.06-FR.02 Rev 12, 20.01.2024) ----------------
# The spec is by component CLASS (not item code). Class comes from item type / name / BOM:
# a master is FIRST master if no master compound sits under it, else SECOND/REPASS. Silica /
# butyl / cobalt content is NOT in our data -> the spec's default row is used and flagged.
_ity = {}
try:
    _itm = pd.read_csv(os.path.join(HERE, "inputs", "itemtype_master.csv"), dtype=str)
    _ity = dict(zip(_itm.iloc[:, 0].map(norm), _itm.iloc[:, 1].astype(str).str.upper()))
except Exception:
    pass
_kids = bom.groupby(bom["Parent"].map(norm))["child"].agg(lambda x: {norm(v) for v in x})
_line = bom.groupby(bom["child"].map(norm))["Super_parent"].agg(
    lambda x: "TBR" if x.astype(str).str.startswith("11").any() else "PCR")
BUTYL = {"ML405", "ML605", "29MG"}
def plant_spec(item):
    """-> (spec_row, min_h, max_h, status, note)"""
    k = norm(item); t = _ity.get(k, ""); ln = _line.get(k, "")
    if k.startswith("IL ") or (t == "INNER LINER" and ln != "TBR" and not k.startswith(("TIL", "SQ", "TPA"))):
        return "9a PCR Inner liner+squeegee", 6, 72, "FILLED (plant spec)", "PCR inner liner: min 6 h, max 3 days"
    if t == "TREAD" or k.startswith(("TR", "TRD")):
        return "12a Carbon tread (PCR/TBR)", 2, 72, "FILLED (plant spec, carbon)", "if it is a SILICA tread: 2 h / 1 day (24 h)"
    if t == "MASTER COMPOUND" or k.startswith(("M1", "M2", "MT", "MB", "XMT", "XMB", "ML", "MG")):
        if k in BUTYL:
            return "5g Master with butyl", 4, 24, "FILLED (plant spec)", ""
        if k == "MB231":
            return "5a Non-silica master (MB231)", 4, 96, "FILLED (plant spec)", ""
        under_master = any(_ity.get(c, "") == "MASTER COMPOUND" for c in _kids.get(k, set()))
        if under_master or k.startswith(("M2", "XMT")):
            return "5d/5e Silica >10 phr SECOND/REPASS master", 8, 72, "FILLED (plant spec, silica series)", "non-silica would be 4 h / 15 days"
        return "5c Silica >10 phr FIRST master", 24, 72, "FILLED (plant spec, silica series)", "min 24 h is CORRECT for a silica first master; non-silica would be 4 h / 15 days"
    if t == "FINAL COMPOUND" or re.match(r"^(T|B|GT|IL)\d", k):
        return "7a Non-silica final (PCR)", 4, 120, "FILLED (plant spec, non-silica)", "if silica >10 phr: 8 h / 3 days"
    if re.match(r"^1[12]\d{4}", k) and len(k) >= 16:
        return "19 Cured tyre (warehouse)", 0, 8760, "FILLED (plant spec)", "SKU-level code: max 1 year, no minimum"
    if k.startswith("AP"):
        return "13 Apex manual application (TBR) / 16 Apexed bead", 1, 48, "ESTIMATE (confirm class)", "apex: 1 h / 2 days (apexed bead: 1 day)"
    return "1/2 Raw material (dipped fabric, steel wire)", "", "", "NEEDS PLANT", "spec refers to CORP/TECH/A-110"

def _spec_with_max(item, db_max):
    r = list(plant_spec(item))
    try:
        db_max = float(db_max)
    except Exception:
        db_max = None
    # the DB max reveals the silica class of a final compound: 3 days => silica final (7c)
    if r[0].startswith("7a") and db_max is not None and db_max <= 72:
        r = ["7c Silica >10 phr final (PCR)", 8, 72, "FILLED (plant spec, silica: DB max 3 days)", "non-silica would be 4 h / 5 days"]
    return tuple(r)

def spec_cols(df, key="item"):
    return pd.DataFrame([_spec_with_max(v, m) for v, m in zip(df[key], df.get("MaxAging_h", [None] * len(df)))], index=df.index,
                        columns=["PLANT_SPEC_row", "PLANT_SPEC_Min_h", "PLANT_SPEC_Max_h", "spec_status", "spec_note"])

s2 = rq["2 Aging min over max"].copy()
s2 = pd.concat([s2, aging_cols(s2)], axis=1)
def sug2(r):
    try:
        mn, mx = float(r["old_jkt_aging_master_Min_h"]), float(r["old_jkt_aging_master_Max_h"])
        if mn <= mx:
            return f"{mn:g} / {mx:g}", "ESTIMATE", "previous DB master jkt_aging_master_pcr/tbr has a consistent pair"
    except Exception:
        pass
    if r["opening_wip_Min_h"] and r["opening_wip_Max_h"]:
        return f"{r['opening_wip_Min_h']} / {r['opening_wip_Max_h']}", "ESTIMATE", "opening_wip carries these limits"
    return "", "NEEDS PLANT", f"no consistent value in any source; min {r['MinAging_h']} h > max {r['MaxAging_h']} h is impossible"
s2[["suggested_Min_Max_h", "status", "source"]] = s2.apply(lambda r: pd.Series(sug2(r)), axis=1)
s2 = pd.concat([s2, spec_cols(s2)], axis=1)
s2["status"] = np.where(s2["spec_status"].str.startswith("NEEDS"), s2["status"], s2["spec_status"])
out["2 Aging min over max"] = s2

s3 = rq["3 Aging to confirm (24h+)"].copy()
s3 = pd.concat([s3, aging_cols(s3)], axis=1)
s3 = pd.concat([s3, spec_cols(s3)], axis=1)
s3["status"] = s3["spec_status"]
s3["verdict_vs_DB"] = s3.apply(lambda r: "DB matches plant spec" if (str(r["PLANT_SPEC_Min_h"]) != "" and float(r["MinAging_h"]) == float(r["PLANT_SPEC_Min_h"]) and float(r["MaxAging_h"]) == float(r["PLANT_SPEC_Max_h"]))
                               else f"DB {r['MinAging_h']}/{r['MaxAging_h']} h -> spec {r['PLANT_SPEC_Min_h']}/{r['PLANT_SPEC_Max_h']} h", axis=1)
s3["note"] = s3.apply(lambda r: "old master differs -> " + f"{r['old_jkt_aging_master_Min_h']}/{r['old_jkt_aging_master_Max_h']} h"
                      if str(r["old_jkt_aging_master_Min_h"]) not in ("", str(r["MinAging_h"]), f"{float(r['MinAging_h']):.1f}") else "same in old master", axis=1)
out["3 Aging to confirm (24h+)"] = s3

# ---------------- 4 No BOM below ----------------
s4 = rq["4 No BOM below component"].copy()
v4 = []
for _, r in s4.iterrows():
    k = bom[bom["Parent"].map(norm) == norm(r["component"])]
    _src_bom = "DB jkt_bom (combined)"
    if not len(k):
        for _f in ("jkt_bom_pcr.csv", "jkt_bom_tbr.csv"):
            _bb = pd.read_csv(os.path.join(SP_DB, _f), dtype=str, low_memory=False)
            _kk = _bb[_bb["Parent"].map(norm) == norm(r["component"])]
            if len(_kk):
                k, _src_bom = _kk, f"DB snapshot db_pipeline/raw/{_f} (older jkt_bom; missing from the current combined BOM)"
                break
    if len(k):
        kk = k[["child", "child_quantity", "child_Unit"]].drop_duplicates()
        qz = (pd.to_numeric(kk["child_quantity"], errors="coerce").fillna(0) == 0).all()
        v4.append(("; ".join(f"{a} {b} {c}" for a, b, c in kk.head(5).values),
                   "NEEDS PLANT" if qz else "FILLED",
                   "DB jkt_bom has the input row(s) but child_quantity = 0 -> quantity needed" if qz else _src_bom))
    else:
        v4.append(("", "NEEDS PLANT", "no input row in DB jkt_bom"))
s4[["bom_rows_found", "status", "source"]] = pd.DataFrame(v4, index=s4.index)
out["4 No BOM below component"] = s4

# ---------------- 5 Batch weight ----------------
s5 = rq["5 Batch weight per compound"].copy()
bw = rt[pd.to_numeric(rt["batch_size"], errors="coerce") > 0].groupby("rp").agg(
    bs=("batch_size", lambda x: pd.to_numeric(x).median()), unit=("batch_UNIT", "first"))
s5["batch_weight_kg"] = s5["item_code"].map(norm).map(bw["bs"])
s5["unit_in_routing"] = s5["item_code"].map(norm).map(bw["unit"])
s5["source"] = np.where(s5["batch_weight_kg"].notna(), "DB jkt_routing batch_size", "not in DB routing")
s5["status"] = np.where(s5["batch_weight_kg"].notna(), "FILLED", "NEEDS PLANT")
out["5 Batch weight per compound"] = s5

# ---------------- 6 Queue time ----------------
s6 = rq["6 Queue time per stage"].copy()
q6 = {}
try:
    S = pd.read_csv(os.path.join(HERE, "output", "CTP_SEP_NEW_PLAN_V2", "phase5_schedule_updated.csv"),
                    parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id").set_index("lot_id")
    E = pd.read_csv(os.path.join(HERE, "outputs2", "phase3_dag_edges_updated.csv")); E = E[E["consume_qty"] > 0]
    ag = pd.read_csv(os.path.join(HERE, "inputs", "aging_master.csv")).drop_duplicates("ItemCode").set_index("ItemCode")
    rdy = {}
    for c, g in E.groupby("consumer_lot"):
        ps = [p for p in g["producer_lot"] if p in S.index]
        if ps and c in S.index:
            rdy[c] = max(S.at[p, "scheduled_finish"] + pd.Timedelta(hours=float(ag["MinAging"].get(S.at[p, "item"], 0) or 0)) for p in ps)
    def stage(op):
        o = str(op).upper()
        for k, v in [("MIX", "mixer"), ("CALEND", "calender"), ("RHC", "calender"), ("EXTRU", "extruder"), ("DUPLEX", "extruder"),
                     ("QUADRA", "extruder"), ("EDGE", "edge"), ("SLIT", "slitter"), ("BEAD", "bead"), ("CUTTER", "cutter"),
                     ("ASSEMBLY", "assembly"), ("DOUBLING", "slitter")]:
            if k in o:
                return v
        return "other"
    w = pd.DataFrame([(stage(S.at[c, "operation"]), (S.at[c, "scheduled_start"] - t).total_seconds() / 3600)
                      for c, t in rdy.items()], columns=["stage", "wait_h"])
    w = w[w["wait_h"] >= 0]
    q6 = w.groupby("stage")["wait_h"].agg(["median", lambda x: x.quantile(0.9), "size"])
except Exception as e:
    print("queue model failed:", e)
rows = []
for st in s6["stage"]:
    if st in getattr(q6, "index", []):
        r = q6.loc[st]
        rows.append((round(r["median"], 1), round(r.iloc[1], 1), int(r["size"]), "ESTIMATE (our scheduler, capacity-limited run 26-30 Sep) -- NOT plant-measured"))
    else:
        rows.append(("", "", 0, "NEEDS PLANT"))
s6[["model_median_wait_h", "model_p90_wait_h", "n_lots", "status"]] = pd.DataFrame(rows, index=s6.index)
try:
    import mes_queue_times as _mq
    _mo, _ = _mq.main()
    s6["queue_hours"] = s6["stage"].map(_mo["median"])
    s6["MES_p75_h"] = s6["stage"].map(_mo["p75"]); s6["MES_p90_h"] = s6["stage"].map(_mo["p90"])
    s6["MES_bom_links"] = s6["stage"].map(_mo["n_links"]); s6["MES_lot_pairs"] = s6["stage"].map(_mo["n_lot_pairs"])
    s6["status"] = np.where(s6["MES_bom_links"].fillna(0) >= 5, "FILLED (plant MES history 11-26 Sep)",
                            "ESTIMATE (MES, too few links -- confirm)")
    s6["source"] = ("JK_CTP_PPDS.CTP_Live_Inventory_Raw: time from a component lot's production to the next "
                    "production of its BOM consumer, minus the component's min aging (queue + setup + transfer + "
                    "any stock buffering); median of per-link medians. queue_hours = median, typical-to-worst = P75..P90")
except Exception as _e:
    print("MES queue failed:", _e)
out["6 Queue time per stage"] = s6

# ---------------- answers 3, 4, 7-10 ----------------
til = wip26[wip26["item_type"].astype(str).str.upper() == "INNER LINER"]["k"]
pcr_il = sorted(x for x in set(til) if re.match(r"^IL\d", x))
tbr_il = sorted(x for x in set(til) if not re.match(r"^IL\d", x))
cap = sorted(set(wip26[wip26["item_type"].astype(str).str.upper() == "CAP STRIP"]["k"]))
gum = int((wip26["k"] == "GUM STRIP").sum())
gt_tt = rt[rt["operation_name"].astype(str).str.contains("BUILD", case=False)]
tt = pd.to_numeric(gt_tt["transfer_time_min"], errors="coerce")
ans = pd.DataFrame([
    ("3", "Frozen daily snapshot of opening_wip", "CONFIRMED PROBLEM (from our side)",
     f"On 27-Sep the DB table held TWO snapshots at once (inventory_date 26-Sep: 33,054 rows; 27-Sep 01:23: 34,762 rows) and the 26-Sep rows were re-written at 20:23 (shift B) -- stock consumed between 07:00 and 20:23 is missing. Our loader now keeps only the plan-start date's snapshot and saves db_pipeline/raw/<db>__<table>.csv on every pull.",
     "NEEDS PLANT/DB team: keep a frozen 07:00 snapshot per day (history table)"),
    ("4", "Component types missing from stock feed (26-Sep)", "CHECKED in opening_wip 26-Sep",
     f"PCR Inner Liner (IL0xx) rows: {len(pcr_il)} -> CONFIRMED MISSING; Inner Liner rows present are TBR only ({', '.join(tbr_il[:8])}); Cap Strip: {cap or 'none'} (only CAP 66); TBR GUM STRIP rows: {gum} -> CONFIRMED MISSING.",
     "NEEDS PLANT: confirm where missing IL / cap strip / gum strip stock is tracked"),
    ("7", "Mixing batch weights", "FILLED (see sheet 5)",
     f"{int(s5['batch_weight_kg'].notna().sum())} of {len(s5)} compounds have batch_size in DB jkt_routing (batch_UNIT KG)", ""),
    ("8", "Raw materials always available?", "CONFIRMED YES (plant, 2026-09-27)",
     "Raw materials (polymers, carbon black, chemicals, steel cord, fabric) are always available -- no raw-material stock check needed. Our scheduler already assumes this.", "none"),
    ("9", "Builds start mid-shift?", "CONFIRMED YES, ANYTIME (plant, 2026-09-27)",
     "A build may start at any time the kit is ready, not only at 07:00/15:00/23:00. Our scheduler already does this (building echo starts at the exact kit-ready time).", "CTB: allow mid-shift starts"),
    ("10", "Green-tyre transfer 90 min, both lines?", "CONFIRMED 90 min (plant, 2026-09-27)",
     f"Building -> press transfer is 90 min on both lines; matches jkt_routing transfer_time_min on all {int(tt.notna().sum())} GT BUILDING rows (values {sorted(set(tt.dropna().astype(int)))}).", "none"),
], columns=["request", "topic", "status", "what we found", "open action"])
out["Answers 3,4,7-10"] = ans

# ---------------- EXPERT APPROXIMATIONS for what the plant has not supplied (2026-09-27) ----------------
APPROX_RATE = {
    "GL7": (2.171, "MIN/BATCH", "F270, 310", "median of 48 PCR final compounds on F270/310; batch 215 kg"),
    "NYLON23-02": (22.0, "M/MIN", "2501, 2502", "doubling has no rate anywhere -> chafer slitter 2401/2402 median (same fabric, preceding step)"),
    "TBWB008": (3.75, "NOS/MIN", "2403, 2404, 2405 (TDA)", "median of TBWB001-007; TBR winding runs on the TDA (CTP Machines.xlsx: spool + final batch in)"),
    "SA15001": (15.38, "NOS/MIN", "201, 202", "median of 75 PCR bead apex items"),
    "SA17001": (15.38, "NOS/MIN", "201, 202", "median of 75 PCR bead apex items"),
    "SW15001": (30.0, "NOS/MIN", "1201", "median of 50 PCR wound beads"),
    "SPB02": (15.0, "CUTS/MIN", "1103, 1104", "median of 124 PCR plies"),
    "SPB03": (15.0, "CUTS/MIN", "1103, 1104", "median of 124 PCR plies"),
    "TBPGUM": (38.8, "M/MIN", "2101", "12.5 cuts/min x cut length 3.10 m (BOM: 3.10 m TBPGUM per body ply) = 38.8 m/min"),
}
OUTSOURCED = {"MTCM313", "TBA827", "TCA313"}
s1 = out["1 Missing machine rate"]
for i, r in s1.iterrows():
    it = norm(r["item"])
    if it in OUTSOURCED:
        s1.loc[i, ["our_value", "our_unit", "source", "status"]] = ["n/a", "", "OUTSOURCED (made outside the plant) -> purchased, always available; no in-plant rate needed", "CLOSED (outsourced)"]
    elif it in APPROX_RATE and not str(r["status"]).startswith("FILLED"):
        v, u, m, why = APPROX_RATE[it]
        s1.loc[i, ["our_value", "our_unit", "machines_in_routing", "source", "status"]] = [v, u, m, why, "APPROX (expert)"]
    elif it == "TBPGUM":
        v, u, m, why = APPROX_RATE[it]
        s1.loc[i, ["our_value", "our_unit", "source", "status"]] = [v, u, why, "APPROX (expert)"]
s2 = out["2 Aging min over max"]
for i, r in s2.iterrows():
    it = norm(r["item"])
    if it == "000000001054115400":
        s2.loc[i, ["PLANT_SPEC_row", "spec_note", "status"]] = ["raw material (spec -> CORP/TECH/A-110)", "raw materials confirmed always available -> not scheduled; aging not needed", "CLOSED (raw material)"]
    elif it == "AP0011T1":
        s2.loc[i, ["PLANT_SPEC_Min_h", "PLANT_SPEC_Max_h", "status"]] = [1, 48, "APPROX (expert: spec 13 apex)"]
BOMAPX = {"B4-TBLT006": "CP 200 0.3866 M per belt (median of 11 sibling TBR belt-4)",
          "TBAPBD002": "S45 1.596 KG per apexed bead (median of 13 sibling TBR apexed beads)",
          "TBP013": "CWBP65 1.2744 M per body ply (median of 12 sibling TBR body plies)",
          "TBP021": "CWBP65 1.2744 M per body ply (median of 12 sibling TBR body plies)"}
s4 = out["4 No BOM below component"]
for i, r in s4.iterrows():
    if norm(r["component"]) in BOMAPX and str(r["status"]).startswith("NEEDS"):
        s4.loc[i, ["bom_rows_found", "status", "source"]] = [BOMAPX[norm(r["component"])], "APPROX (expert)",
                                                            "input row exists with qty 0 in every BOM version -> sibling median"]
s6 = out["6 Queue time per stage"]
s6.loc[s6["stage"] == "mixer", "status"] = "APPROX (MES, 2 links -- confirm)"
ans = out["Answers 3,4,7-10"]
ans.loc[ans["request"] == "3", "open action"] = "APPROX until DB fixed: use only the plan-start date's snapshot (done in our loader); DB team to freeze 07:00 rows"
ans.loc[ans["request"] == "4", "open action"] = "APPROX until fed: missing PCR inner liner / TBR GUM STRIP stock taken as 0 (all of it made new) -- conservative"
ans.loc[ans["request"].isin(["3", "4"]), "status"] = ans.loc[ans["request"].isin(["3", "4"]), "status"] + " | APPROX in use"

# ---------------- summary ----------------
def cnt(df):
    s = df["status"].astype(str)
    return (int(s.str.startswith(("FILLED", "CLOSED")).sum()), int(s.str.startswith(("ESTIMATE", "APPROX")).sum()), int(s.str.startswith("NEEDS").sum()))
summ = []
for k in ["1 Missing machine rate", "2 Aging min over max", "3 Aging to confirm (24h+)", "4 No BOM below component",
          "5 Batch weight per compound", "6 Queue time per stage"]:
    a, b, c = cnt(out[k]); summ.append((k, len(out[k]), a, b, c))
summ = pd.DataFrame(summ, columns=["sheet", "rows", "FILLED / CLOSED (plant or DB data)", "ESTIMATE / APPROX (usable, confirm later)", "NEEDS PLANT (left)"])
out = {"SUMMARY - what is left": summ, **out}

with pd.ExcelWriter(F, engine="openpyxl") as xl:
    for k, df in out.items():
        df.to_excel(xl, sheet_name=k[:31], index=False)
print(summ.to_string(index=False))
print("\nwritten:", F, "| original kept:", bak)
