#!/usr/bin/env python3
"""check_db_masters.py <version>

v85 (user 2026-10-13): the schedule keeps the plant plan's building machine and curing press per row; this report checks
them against the JK_CTP_PPDS masters (read-only):
  CTP_Master_BuildingAllowable   building row on an allowed machine for its SKU
  CTP_Master_BuildingPreference  building row on the SKU's rank-1 machine (released, current)
  CTP_Master_CuringAllowable     cure row on an allowed press for its SKU
  CTP_Master_CuringCycleTime     cure cycle used (plan) = master cycle
Machine names: plan TBMPCR<n>Stage2 = master 34<nn> (PCR); TBM-0<n> = TBM-0<n> / TBR-0<n> (TBR). The mapping is an
ASSUMPTION (names differ between plan and master) and is reported.
Writes latest_schedule/<version>/DB_MASTER_CHECKS_<version>.md / .xlsx.
"""
import os
import re
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "db_pipeline"))


def _db():
    from db_connect import load_env
    import pymysql
    c = load_env()
    con = pymysql.connect(host=c["DB_HOST"], port=int(c["DB_PORT"]), user=c["DB_USER"], password=c["DB_PASSWORD"],
                          database="JK_CTP_PPDS", connect_timeout=30, read_timeout=300)
    q = lambda s: pd.read_sql(s, con)
    out = {"ba": q("SELECT sku_code, machine_no FROM CTP_Master_BuildingAllowable WHERE is_current=1 AND is_allowed=1"),
           "bp": q("SELECT sku_code, machine_no, preference_rank FROM CTP_Master_BuildingPreference WHERE is_current=1 AND is_released=1"),
           "ca": q("SELECT sku_code, machine_no FROM CTP_Master_CuringAllowable WHERE is_current=1 AND is_allowed=1"),
           "cc": q("SELECT sku_code, cycle_time_min FROM CTP_Master_CuringCycleTime WHERE is_current=1")}
    con.close()
    return out


def tbm_keys(m):
    m = str(m).strip()
    g = re.match(r"TBMPCR(\d+)Stage2$", m)
    if g:
        return {f"34{int(g.group(1)):02d}"}
    g = re.match(r"TBM-0?(\d+)$", m)
    if g:
        n = int(g.group(1))
        return {f"TBM-{n:02d}", f"TBR-{n:02d}"}
    return {m}


def main():
    v = sys.argv[1]
    R = os.path.join(ROOT, "latest_schedule", v, "run_output")
    import warnings
    warnings.filterwarnings("ignore")
    M = _db()
    s = lambda x: x.astype(str).str.strip()
    ba = set(zip(s(M["ba"].sku_code), s(M["ba"].machine_no)))
    pref1 = M["bp"][pd.to_numeric(M["bp"].preference_rank, errors="coerce") == 1]
    p1 = {}
    for k, m in zip(s(pref1.sku_code), s(pref1.machine_no)):
        p1.setdefault(k, set()).add(m)
    ca = set(zip(s(M["ca"].sku_code), s(M["ca"].machine_no)))
    cyc = M["cc"].assign(k=s(M["cc"].sku_code)).drop_duplicates("k").set_index("k").cycle_time_min
    rows = []
    # building (plan rows as scheduled)
    bp = pd.read_csv(os.path.join(R, "PHASEWISE", "00_INPUTS", "building_plan.csv"))
    bp["sku"] = s(bp.skuCode)
    bp["keys"] = bp.buildMachine.map(tbm_keys)
    bp["allowed"] = [any((k_, x) in ba for x in ks) for k_, ks in zip(bp.sku, bp["keys"])]
    bp["has_pref"] = bp.sku.isin(p1.keys())
    bp["on_pref1"] = [bool(p1.get(k_, set()) & ks) for k_, ks in zip(bp.sku, bp["keys"])]
    rows += [("Building", "row on a machine allowed for its SKU (CTP_Master_BuildingAllowable)", len(bp), int(bp.allowed.sum()),
              int(bp.loc[~bp.allowed, "qty"].sum())),
             ("Building", "row on the SKU's rank-1 machine (CTP_Master_BuildingPreference; SKUs with a preference)",
              int(bp.has_pref.sum()), int(bp.on_pref1.sum()), int(bp.loc[bp.has_pref & ~bp.on_pref1, "qty"].sum()))]
    # curing (phase 6 = plan presses)
    cu = pd.read_csv(os.path.join(R, "phase6_curing_revision_updated.csv"), low_memory=False)
    cu["sku"] = s(cu.skuCode)
    cu["press"] = cu.pressNo.astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    cu["allowed"] = [(k_, p) in ca for k_, p in zip(cu.sku, cu.press)]
    pl = pd.read_csv(os.path.join(R, "PHASEWISE", "00_INPUTS", "plan.csv"))   # cycle the scheduler was given
    pl["sku"] = s(pl.skuCode)
    pl["m_cyc"] = pl.sku.map(cyc)
    has = pl.m_cyc.notna()
    same = (pd.to_numeric(pl.cycleTime, errors="coerce") - pl.m_cyc).abs() <= 0.05
    rows += [("Curing", "cure row on a press allowed for its SKU (CTP_Master_CuringAllowable)", len(cu), int(cu.allowed.sum()),
              int(pd.to_numeric(cu.loc[~cu.allowed, "qty"], errors="coerce").sum())),
             ("Curing", "input cure cycle (plan.csv) = CTP_Master_CuringCycleTime (rows whose SKU has a master value)", int(has.sum()),
              int((has & same).sum()), int(pd.to_numeric(pl.loc[has & ~same, "qty"], errors="coerce").sum()))]
    T = pd.DataFrame(rows, columns=["area", "check", "rows", "followed", "qty_not_followed"])
    T["compliance %"] = (100 * T.followed / T.rows.replace(0, pd.NA)).round(1)
    det = {"Building not allowed": bp.loc[~bp.allowed, ["sku", "gtCode", "buildMachine", "qty", "startTime"]],
           "Building not rank-1": bp.loc[bp.has_pref & ~bp.on_pref1, ["sku", "gtCode", "buildMachine", "qty"]]
           .assign(rank1=lambda d: d.sku.map(lambda k_: ", ".join(sorted(p1.get(k_, []))))),
           "Curing not allowed": cu.loc[~cu.allowed, ["sku", "press", "qty", "startTime"]]}
    vd = os.path.join(ROOT, "latest_schedule", v)
    with pd.ExcelWriter(os.path.join(vd, f"DB_MASTER_CHECKS_{v}.xlsx")) as xw:
        T.to_excel(xw, "Summary", index=False)
        for n, d in det.items():
            d.to_excel(xw, n[:31], index=False)
    md = [f"# DB master checks — {v}", "",
          "Plan machines kept per row (user choice); checked against JK_CTP_PPDS masters. Machine-name mapping assumed: "
          "TBMPCR<n>Stage2 = 34<nn>, TBM-0<n> = TBM-0<n> / TBR-0<n>.", "",
          "| Area | Check | Rows | Followed | % | Qty not followed |", "|---|---|---|---|---|---|"]
    md += [f"| {r.area} | {r.check} | {r.rows:,} | {r.followed:,} | {r['compliance %']} | {r.qty_not_followed:,} |"
           for _, r in T.iterrows()]
    open(os.path.join(vd, f"DB_MASTER_CHECKS_{v}.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
