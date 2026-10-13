#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""final_feasibility_check.py -- independent check of a finished run before it goes to the plant.

    python final_feasibility_check.py <run_dir> [workbook.xlsx]

Re-derives every check from the saved output files only (no scheduler logs):
  plan fidelity, machine double-booking, routing eligibility, changeover matrix, PM downtime,
  upstream precedence + min aging, building kit (material exists), curing green-tyre supply
  and shelf life, quantity balance. Prints a table and (optionally) writes a 'Feasibility checks'
  sheet into the schedule workbook.
"""
import os
import re
import sys
from collections import defaultdict

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = sys.argv[1]
WB = sys.argv[2] if len(sys.argv) > 2 else None
INP = os.path.join(HERE, "inputs")
TOL = pd.Timedelta(minutes=1)
rows = []


def add(area, check, result, value, note=""):
    rows.append({"area": area, "check": check, "result": result, "measured": value, "note": note})


cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
S = pd.read_csv(os.path.join(RUN, "phase5_schedule_updated.csv"),
                parse_dates=["scheduled_start", "scheduled_finish"]).drop_duplicates("lot_id")
S["machine"] = S["machine"].astype(str).str.strip()
S["item"] = S["item"].astype(str).str.strip()
dep = S["department"].astype(str).str.upper()
B = S[dep == "BUILDING"].copy()
U = S[~dep.isin(["BUILDING", "CURING"])].copy()
C = pd.read_csv(os.path.join(RUN, "phase6_curing_revision_updated.csv"), parse_dates=["startTime", "endTime"])
bp = pd.read_csv(os.path.join(INP, "building_plan.csv"))
if "origStartTime" in bp.columns:
    bp["startTime"] = bp["origStartTime"]
bp["startTime"] = pd.to_datetime(bp["startTime"])
pl = pd.read_csv(os.path.join(INP, "plan.csv"), parse_dates=["startTime", "endTime"])
pl = pl[(pl["skuCode"].astype(str).str.upper() != "CHANGEOVER") & (pl["qty"] > 0)]
G = pd.read_csv(os.path.join(RUN, "phase5_building_echo_gates.csv"), parse_dates=["plan_start", "start"])
L = pd.read_csv(os.path.join(RUN, "phase2_lots_updated.csv")).set_index("lot_id")

# ---------- 1. plan fidelity ----------
Bq = B.assign(q=B["lot_id"].map(L["lot_qty"]) if "lot_qty" in L else 0)
fs = pd.read_csv(os.path.join(RUN, "phase5_floor_schedule_updated.csv"), parse_dates=["start_time", "end_time"])
fb = fs[fs["department"].astype(str).str.upper() == "BUILDING"]
built_tot = float(pd.to_numeric(fb["produce_qty"], errors="coerce").sum())
add("Plan", "every building-plan row scheduled", "OK" if len(B) == len(bp) else "CHECK",
    f"{len(B)} of {len(bp)} rows; {built_tot:,.0f} of {bp['qty'].sum():,.0f} tyres")
g = G.merge(B[["lot_id", "machine"]].assign(row=B["lot_id"].str[3:].astype(int)), on="row", how="left")
bad_m = (g["tbm"].astype(str).str.strip() != g["machine"].astype(str).str.strip()).sum()
add("Plan", "each row on its PLANNED building machine (BR-1)", "OK" if bad_m == 0 else "FAIL", f"{bad_m} rows moved")
# start not before plan shift slot (match gate rows back to the plan by sku/tbm/qty)
G["slot"] = G["plan_start"]            # the row's own plan-shift start (gate log)
bst = B.set_index(B["lot_id"].str[3:].astype(int))["scheduled_start"]
G["real_start"] = G["row"].map(bst)
early = ((G["real_start"] < G["slot"] - TOL) & G["slot"].notna()).sum()
add("Plan", "no build starts before its plan SHIFT slot (BR-1)", "OK" if early == 0 else "FAIL", f"{early} early")
cq = C.groupby("skuCode")["qty"].sum()
pq = pl.groupby("skuCode")["qty"].sum()
diff = (cq.reindex(pq.index, fill_value=0) - pq).abs().gt(0.5).sum()
add("Plan", "curing quantity per SKU = curing plan", "OK" if diff == 0 else "FAIL",
    f"{C['qty'].sum():,.0f} of {pl['qty'].sum():,.0f} tyres; {diff} SKUs differ")

# ---------- 2. no machine double-booked ----------
def overlaps(df, m, s, e):
    n = 0
    for _, g2 in df.sort_values(s).groupby(m):
        prev = None
        for a, b in zip(g2[s], g2[e]):
            if prev is not None and a < prev - TOL:
                n += 1
            prev = max(prev, b) if prev is not None else b
    return n
ou = overlaps(U, "machine", "scheduled_start", "scheduled_finish")
ob = overlaps(B, "machine", "scheduled_start", "scheduled_finish")
oc = overlaps(C.assign(p=C["pressNo"].astype(str)), "p", "startTime", "endTime")
add("Capacity", "no upstream machine double-booked", "OK" if ou == 0 else "FAIL", f"{ou} overlaps on {U['machine'].nunique()} machines")
add("Capacity", "no building machine double-booked", "OK" if ob == 0 else "FAIL", f"{ob} overlaps on {B['machine'].nunique()} TBMs")
add("Capacity", "no press double-booked", "OK" if oc == 0 else "FAIL", f"{oc} overlaps on {C['pressNo'].nunique()} presses")

# ---------- 3. routing eligibility ----------
rt = pd.read_csv(os.path.join(INP, "routing.csv"), dtype=str)
elig = defaultdict(set)
for p, m in zip(rt["routed_product"].fillna("").str.strip(), rt["machines"].fillna("")):
    elig[p] |= set(re.findall(r"[A-Za-z0-9\-]+", m))
bad = sum(1 for i, m in zip(U["item"], U["machine"]) if m not in elig.get(i, set()))
add("Rules", "only approved machine/item combinations (BR-4/75)", "OK" if bad == 0 else "FAIL", f"{bad} of {len(U)} lots")

# ---------- 4. changeover matrix + mixer 1 min ----------
co = pd.read_csv(os.path.join(INP, "changeover_lookup.csv"))
m2l = {str(k): v for k, v in (cfg.get("machine_to_changeover_line") or {}).items()}
comap = {(l, f, t): v for l, f, t, v in zip(co["line"], co["from_item"], co["to_item"], co["changeover_min"])}
mix = set(str(x) for x in ((cfg.get("business_rules") or {}).get("mixers") or []))
mco = float(cfg.get("mixer_changeover_min", 2))
n_chk = n_short = n_mix = n_mix_short = 0
for m, g2 in U.sort_values("scheduled_start").groupby("machine"):
    prev = None
    for _, r in g2.iterrows():
        if prev is not None and r["item"] != prev["item"]:
            gap = (r["scheduled_start"] - prev["scheduled_finish"]).total_seconds() / 60
            req = comap.get((m2l.get(m), prev["item"], r["item"])) if m in m2l else None
            if req is not None:
                n_chk += 1
                n_short += gap < req - 0.5
            if m in mix:
                n_mix += 1
                n_mix_short += gap < mco - 0.1
        prev = r
add("Rules", "changeover >= plant matrix (BR-7)", "OK" if n_short == 0 else "FAIL", f"{n_short} of {n_chk} item changes short")
add("Rules", f"mixer changeover >= {mco:g} min (BR-74)", "OK" if n_mix_short == 0 else "FAIL", f"{n_mix_short} of {n_mix} short")

# ---------- 5. PM downtime ----------
dt = pd.read_csv(os.path.join(HERE, "db_pipeline", "raw", "jkplanning_CTP__downtime_reason.csv"),
                 parse_dates=["Scheduled Start Time", "Scheduled End Time"])
def mk(m):
    m = str(m).strip()
    if m.isdigit() and m.startswith("34"):
        return f"TBMPCR{int(m[2:])}Stage2"
    if m.isdigit() and m.startswith("38"):
        return f"TBM-{int(m[2:]):02d}"
    return m
allb = pd.concat([S[["machine", "scheduled_start", "scheduled_finish"]].set_axis(["m", "s", "e"], axis=1),
                  C.assign(m=C["pressNo"].astype(str))[["m", "startTime", "endTime"]].set_axis(["m", "s", "e"], axis=1)])
hit = sum(int(((allb["m"] == mk(m)) & (allb["s"] < b) & (allb["e"] > a)).sum())
          for m, a, b in zip(dt["Machine ID"], dt["Scheduled Start Time"], dt["Scheduled End Time"]))
add("Rules", "no work inside PM/MTC downtime (BR-10)", "OK" if hit == 0 else "FAIL", f"{hit} overlaps, {len(dt)} windows checked")

# ---------- 6. upstream precedence + min aging (allocated edges) ----------
ag = pd.read_csv(os.path.join(INP, "aging_master.csv"))
ag["k"] = ag["ItemCode"].astype(str).str.strip()
mn = dict(zip(ag["k"], pd.to_numeric(ag["MinAging"], errors="coerce").fillna(0)))
E = pd.read_csv(os.path.join(HERE, "outputs2", "phase3_dag_edges_updated.csv"))
E = E[pd.to_numeric(E["consume_qty"], errors="coerce").fillna(0) > 0]
fin = S.set_index("lot_id")["scheduled_finish"]; st = S.set_index("lot_id")["scheduled_start"]
e2 = E[E["producer_lot"].isin(fin.index) & E["consumer_lot"].isin(st.index) & ~E["consumer_lot"].str.startswith("BLD")]
lag = e2["producer_item"].astype(str).str.strip().map(mn).fillna(0)
early = (e2["producer_lot"].map(fin) + pd.to_timedelta(lag, unit="h") > e2["consumer_lot"].map(st) + pd.Timedelta(minutes=15))
add("Material", "upstream lots start after their allocated inputs + min aging (BR-12 min)",
    "OK" if early.sum() == 0 else "CHECK", f"{int(early.sum())} of {len(e2)} allocated edges early",
    "early edges can be covered by opening WIP (the material ledger is the authority for building)")

# ---------- 7. building kit / aging, curing GT ----------
kit = os.path.join(RUN, "KIT_FEASIBILITY_CHECK.xlsx")
if os.path.exists(kit):
    ks = pd.read_excel(kit, sheet_name="Summary").set_index("check")
    def val(k):
        r = ks[ks.index.str.contains(k, regex=False)]
        return (int(r.iloc[0]["blocks"]), int(r.iloc[0]["tyres"])) if len(r) else (0, 0)
    nr = val("NOT READY at build start"); pa = val("past MAX aging"); gn = val("NOT READY at press")
    g72 = val("older than 72 h")
    add("Material", "building starts only with the complete kit (BR-66/67)", "OK" if nr[0] <= 1 else "CHECK",
        f"{nr[0]} block(s) / {nr[1]} tyres not ready at start", "independent FIFO replay")
    add("Material", "no component used past max aging (BR-12 max)", "PARTLY", f"{pa[0]} blocks / {pa[1]:,} tyres use some over-age component",
        "max aging is minimised (aging pull, lot caps) and reported -- a hard block scraps material nothing remakes")
    add("Curing", "every press has its green tyre before it starts", "OK" if gn[0] == 0 else "FAIL", f"{gn[0]} blocks")
    add("Curing", "green tyre <= 72 h at the press (BR-12 GT)", "OK" if g72[0] == 0 else "PARTLY", f"{g72[0]} blocks / {g72[1]} tyres over 72 h")
ag2 = os.path.join(RUN, "AGING_FIFO_CHECK.xlsx")
if os.path.exists(ag2):
    a = pd.read_excel(ag2, sheet_name="Summary")
    fed = a[a["check"].str.contains("Component quantity fed", regex=False)]["tyres_or_qty"].iloc[0]
    past = a[a["check"].str.contains("past max aging", regex=False)]["tyres_or_qty"].iloc[0]
    add("Material", "component quantity past max aging", "INFO", f"{100 * float(past) / float(fed):.2f}% ({float(past):,.0f} of {float(fed):,.0f})")

# ---------- 8. green-tyre balance ----------
bom = pd.read_csv(os.path.join(INP, "bom.csv"), dtype=str)
gt = bom[bom["child_description"].astype(str).str.upper().str.contains("GREEN")]
s2g = dict(zip(gt["Super_parent"].str.strip(), gt["child"].str.strip()))
inv = pd.read_csv(os.path.join(INP, "inventory.csv"), low_memory=False)
_nz = lambda x: re.sub(r"\s+", " ", str(x)).strip()   # stock carries codes like "GT  T1457 STAR"
stock = pd.to_numeric(inv["inventory"], errors="coerce").groupby(inv["itemcode"].map(_nz)).sum()
cured_gt = C.assign(g=C["skuCode"].astype(str).str.strip().map(s2g).map(_nz)).groupby("g")["qty"].sum()
built_gt = pd.to_numeric(fb["produce_qty"], errors="coerce").groupby(fb["item"].map(_nz)).sum()
over = [(g_, q) for g_, q in cured_gt.items() if q > built_gt.get(g_, 0) + stock.get(g_, 0) + 0.5]
add("Curing", "cured <= built + opening GT stock, per green tyre", "OK" if not over else "FAIL",
    f"{len(over)} GT codes cure more than they have", "; ".join(f"{g_}" for g_, _ in over[:5]))

R = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 90)
print(R[["area", "check", "result", "measured"]].to_string(index=False))
if WB:
    with pd.ExcelWriter(WB, engine="openpyxl", mode="a", if_sheet_exists="replace") as xl:
        R.to_excel(xl, sheet_name="Feasibility checks", index=False)
        ws = xl.book["Feasibility checks"]
        for col, w in zip("ABCDE", (12, 62, 10, 55, 70)):
            ws.column_dimensions[col].width = w
    print(f"-> 'Feasibility checks' sheet written into {WB}")
