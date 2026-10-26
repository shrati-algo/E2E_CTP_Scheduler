#!/usr/bin/env python3
"""make_validation_report.py — v39 VALIDATION_REPORT, written on EVERY run.

    python make_validation_report.py --run e2e_v39

THE DATA-BLOCKER POLICY (blocker_policy.py), made visible:

  * demand that cannot be scheduled because a master record is missing and no
    safe stand-in exists (B-codes) is LISTED here and taken OUT of the final
    figures - out of the plan and out of the cured count;
  * every gap closed by an assumption (A-codes) is listed with the value used,
    where it came from, and the SKUs / tyres that depend on it;
  * the schedule gates (G-codes) are measured on the written outputs.

Writes, under outputs/<run>/:
  VALIDATION_REPORT.xlsx          Summary, Blocked SKUs, Blocking records,
                                  Assumptions, Assumption catalogue,
                                  Assumed tyres, Schedule checks, Run stamp
  VALIDATION_REPORT_blocked.csv   one row per blocked SKU
  VALIDATION_SUMMARY.csv          the final figures, per line
  ASSUMPTIONS.csv                 the single assumption register

Exit code 1 if a FATAL gate fails (G1, G1b, G1u, G2, G3, G4, G6, G7), so run.py stops.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

import pandas as pd

import blocker_policy as BP
import ctpio as io
import rules as RULES

LINES = ("pcr", "tbr")
FATAL = {"G1", "G1b", "G1u", "G2", "G3", "G4", "G6", "G7"}   # v40: G3 + G4; v42: G1b; v45a: G1u


def _csv(p: pathlib.Path, **kw) -> pd.DataFrame:
    return pd.read_csv(p, **kw) if p.exists() else pd.DataFrame()


def _true(s: pd.Series) -> pd.Series:
    return s.astype(str).str.lower().isin(("true", "1"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run

    summ, blocked, records, asm_rows, assumed, checks = [], [], [], [], [], []
    gaps = []
    sched_all = []
    for ln in LINES:
        O = D / ln
        st = _csv(O / BP.STATUS_FILE, dtype=str).fillna("")
        cp = _csv(O / "phase7_curing_plan.csv",
                  parse_dates=["planned_start", "planned_end", "gt_ready",
                               "feasible_start", "feasible_end"])
        bp = _csv(O / "phase5_building_plan.csv",
                  parse_dates=["build_start", "build_end", "feasible_start", "feasible_end"])
        dem = _csv(O / "phase1_demand.csv", usecols=["item_code", "sku"])
        sc = _csv(O / "phase5_schedule.csv", usecols=["lot_id"])
        sched_all.append(sc.assign(line=ln))
        if not bp.empty:
            sched_all.append(pd.DataFrame({"lot_id": bp["building_lot_id"].astype(str),
                                           "line": ln}))
        if cp.empty or st.empty:
            checks.append({"gate": "G6", "line": ln.upper(), "check": "SKU status present",
                           "value": "missing", "target": "phase0_sku_status.csv + tags",
                           "verdict": "FAIL", "since_step": 1,
                           "note": "phase 0 / phase 7 did not write the policy columns"})
            continue
        if "in_kpi" not in cp.columns:
            state = cp["sku"].map(st.drop_duplicates("sku").set_index("sku")["state"])
            cp["sku_state"] = state.fillna(BP.CLEAN)
            cp["in_kpi"] = cp["sku_state"].map(BP.in_kpi)
        cp["in_kpi"] = _true(cp["in_kpi"])
        cp["within_horizon"] = _true(cp["within_horizon"])
        k, x = cp[cp["in_kpi"]], cp[~cp["in_kpi"]]

        # ---- Summary ------------------------------------------------------
        raw, blk = float(cp["qty_planned"].sum()), float(x["qty_planned"].sum())
        elig = float(k["qty_planned"].sum())
        cured = float(k.loc[k["within_horizon"], "qty"].sum())
        ka = k[k["sku_state"] == BP.ASSUMED]
        summ.append({
            "line": ln.upper(), "plan_tyres_raw": round(raw),
            "data_blocked_tyres": round(blk), "data_blocked_skus": int(x["sku"].nunique()),
            "eligible_tyres": round(elig), "cured_eligible": round(cured),
            "fulfilment_pct_eligible": round(100 * cured / elig, 1) if elig else None,
            "cured_raw": round(float(cp.loc[cp["within_horizon"], "qty"].sum())),
            "fulfilment_pct_raw_plan": round(100 * float(cp.loc[cp["within_horizon"], "qty"].sum())
                                             / raw, 1) if raw else None,
            "assumed_tyres_eligible": round(float(ka["qty_planned"].sum())),
            "assumed_tyres_cured": round(float(ka.loc[ka["within_horizon"], "qty"].sum())),
            "cured_from_stock_blocked_not_counted":
                round(float(x.loc[x["within_horizon"], "qty"].sum())),
        })

        # ---- Blocked SKUs + Blocking records -------------------------------
        sb = st[st["state"] == BP.BLOCKED]
        for r in sb.itertuples(index=False):
            lots = x[x["sku"] == r.sku]
            meta = BP.BLOCKERS.get(r.blocker_code, {})
            blocked.append({
                "line": ln.upper(), "sku": r.sku, "blocker_code": r.blocker_code,
                "missing": meta.get("what", r.note), "missing_table": r.missing_table,
                "missing_key": r.missing_key, "why_not_assumed": meta.get("why", ""),
                "owner": meta.get("owner", ""), "source": r.source,
                "cure_tyres_excluded": round(float(lots["qty_planned"].sum())),
                "build_tyres_excluded": round(float(pd.to_numeric(r.build_tyres))),
                "cure_lots": int(len(lots)), "presses": int(lots["press"].nunique()),
                "first_plan_day": (str(io.plant_day(lots["planned_start"].min()).date())
                                   if len(lots) else ""),
                "cured_from_stock_not_counted":
                    round(float(lots.loc[lots["within_horizon"], "qty"].sum())),
            })

        # ---- Assumptions (register) ---------------------------------------
        a0 = _csv(O / "phase0_assumptions.csv")
        a3 = _csv(O / "phase3_assumptions.csv")
        a5 = _csv(O / "phase5_assumptions.csv")
        a7 = _csv(O / "phase7_assumptions.csv")
        reg = pd.concat([a0.assign(phase="phase0"), a3.assign(phase="phase3"),
                         a5.assign(phase="phase5"), a7.assign(phase="phase7")],
                        ignore_index=True)
        btyres = dict(zip(st["sku"], pd.to_numeric(st["build_tyres"], errors="coerce")))
        sku_of = dem.groupby("item_code")["sku"].apply(set).to_dict() if not dem.empty else {}
        for r in reg.itertuples(index=False):
            skus = sku_of.get(r.item, set())
            cat = BP.ASSUMPTIONS.get(r.code, {})
            asm_rows.append({
                "line": ln.upper(), "code": r.code, "item": r.item,
                "department": getattr(r, "department", ""), "gap": r.gap,
                "value_used": r.value, "source": r.source,
                "rule": cat.get("rule", ""), "confidence": cat.get("confidence", ""),
                "skus_affected": len(skus),
                "tyres_affected": round(sum(btyres.get(s, 0) or 0 for s in skus)),
                "phase": r.phase,
            })

        # ---- data gaps the engine closed with a stand-in (blockers.py) ------
        # Not DATA_BLOCKED under the policy - no demand is excluded for them -
        # but each one is a value the database did not supply, so it is shown.
        g = _csv(O / "DATA_BLOCKERS.csv", encoding="utf-8-sig")
        if len(g):
            gaps.append(g.assign(line=ln.upper()))

        # ---- Assumed tyres, by code ---------------------------------------
        if len(ka):
            e = ka.assign(code=ka["assumption_codes"].fillna("").astype(str).str.split(",")
                          ).explode("code")
            for c, g in e[e["code"] != ""].groupby("code"):
                assumed.append({"line": ln.upper(), "code": c,
                                "rule": BP.ASSUMPTIONS.get(c, {}).get("rule", ""),
                                "skus": int(g["sku"].nunique()),
                                "plan_tyres": round(float(g["qty_planned"].sum())),
                                "cured_tyres": round(float(g.loc[g["within_horizon"], "qty"].sum()))})

        # ---- Schedule gates -----------------------------------------------
        L = ln.upper()
        p = bp[bp["feasible_start"].notna()].copy()
        ov, ovh = 0, 0.0
        for m, g in p.groupby("machine"):
            g = g.sort_values("feasible_start")
            pe = None
            for s, f in zip(g["feasible_start"], g["feasible_end"]):
                if pe is not None and s < pe - pd.Timedelta(seconds=1):
                    ov += 1
                    ovh += (min(pe, f) - s).total_seconds() / 3600
                pe = f if pe is None else max(pe, f)
        checks.append({"gate": "G1", "line": L, "check": "building machine overlaps",
                       "value": f"{ov} lots / {ovh:.1f} h", "target": "0",
                       "verdict": "PASS" if ov == 0 else "FAIL", "since_step": 2, "note": ""})
        _plan = p["build_end"] - p["build_start"]
        if "setup_applied_min" in p.columns:
            # v42: run time (plan duration - plan setup) + the setup actually applied
            _plan = (_plan - pd.to_timedelta(pd.to_numeric(p["setup_min"], errors="coerce")
                                              .fillna(0), unit="m")
                     + pd.to_timedelta(pd.to_numeric(p["setup_applied_min"], errors="coerce")
                                       .fillna(0), unit="m"))
        dd = ((p["feasible_end"] - p["feasible_start"]) - _plan).abs()
        nbad = int((dd > pd.Timedelta(seconds=1)).sum())
        corr = (p["feasible_end"] - p["feasible_start"]).dt.total_seconds().corr(p["qty"])
        q = p.sort_values(["machine", "feasible_start"])
        prv_b = q.groupby("machine")["sku"].shift(1)
        _chg_b = prv_b.notna() & (prv_b != q["sku"])
        _need = (pd.to_numeric(q["setup_applied_min"], errors="coerce").fillna(0)
                 if "setup_applied_min" in q.columns else
                 pd.to_numeric(q["setup_min"], errors="coerce").fillna(0))
        _ns = int((_chg_b & (_need <= 0)).sum())
        checks.append({"gate": "G1b", "line": L,
                       "check": "every product change on a builder has its setup",
                       "value": f"{_ns} of {int(_chg_b.sum())} changes without setup",
                       "target": "0", "verdict": "PASS" if _ns == 0 else "FAIL",
                       "since_step": "v42", "note": ""})
        # ---- G1u (v45a): no upstream machine runs two lots at once ----------
        _su = _csv(O / "phase5_schedule.csv", parse_dates=["start", "finish"])
        _ov_u, _ov_m = 0, set()
        if not _su.empty:
            _su["machine"] = _su["machine"].astype(str)
            for _m, _g in _su[_su["start"].notna()].groupby("machine"):
                _g = _g.sort_values("start")
                _pe = None
                for _a, _f in zip(_g["start"], _g["finish"]):
                    if _pe is not None and _a < _pe - pd.Timedelta(seconds=1):
                        _ov_u += 1
                        _ov_m.add(_m)
                    _pe = _f if _pe is None else max(_pe, _f)
        checks.append({"gate": "G1u", "line": L, "check": "upstream machine overlaps",
                       "value": f"{_ov_u} lots on {sorted(_ov_m) or 'no machine'}",
                       "target": "0", "verdict": "PASS" if _ov_u == 0 else "FAIL",
                       "since_step": "v45a", "note": ""})
        checks.append({"gate": "G2", "line": L, "check": "trolley duration = run time + applied setup",
                       "value": f"{len(p)-nbad} of {len(p)} rows; corr(dur, qty)={corr:.3f}",
                       "target": "100% of rows", "verdict": "PASS" if nbad == 0 else "FAIL",
                       "since_step": 2, "note": ""})
        c2 = cp[cp["feasible_start"].notna()].sort_values(["press", "feasible_start"])
        prv = c2.groupby("press").shift(1)
        chg = c2[prv["sku"].notna() & (prv["sku"] != c2["sku"])]
        gap = (chg["feasible_start"] - prv.loc[chg.index, "feasible_end"]).dt.total_seconds() / 60
        if "co_min" in chg.columns:
            need = pd.to_numeric(chg["co_min"], errors="coerce").fillna(0)
            z = int((gap < need - 1).sum())
            z0 = int((gap < 1).sum())
            v = f"{z} of {len(chg)} below required (zero gap: {z0}); median required {need.median():.0f} min"
            lab = "press SKU changes with gap < required changeover"
        else:
            z = int((gap < 1).sum())
            v, lab = f"{z} of {len(chg)}", "press SKU changes with zero gap"
        checks.append({"gate": "G3", "line": L, "check": lab, "value": v, "target": "0",
                       "verdict": "PASS" if z == 0 else "FAIL", "since_step": 3,
                       "note": "" if "co_min" in chg.columns else
                               "fixed in step 3 (mould-change gap); reported, not fatal yet"})
        # ---- G8 (v42f): same machine and same order as the input plan --------
        b1 = _csv(O / "phase1_building_lots.csv", usecols=["building_lot_id", "machine"])
        if not b1.empty and not p.empty:
            pm = dict(zip(b1["building_lot_id"].astype(str), b1["machine"].astype(str)))
            pp = p.assign(machine=p["machine"].astype(str),
                          plan_machine=p["building_lot_id"].astype(str).map(pm))
            moved = int((pp["plan_machine"].notna() & (pp["plan_machine"] != pp["machine"])).sum())
            reord = 0
            for _m, _g in pp.groupby("machine"):
                _pa = list(_g.sort_values(["build_start", "building_lot_id"])["building_lot_id"])
                _pb = list(_g.sort_values(["feasible_start", "build_start"])["building_lot_id"])
                reord += int(_pa != _pb)
            ok8 = moved == 0 and reord == 0
            checks.append({"gate": "G8", "line": L,
                           "check": "building: same machine and same trolley order as the plan",
                           "value": f"{moved} trolley(s) on another machine; "
                                    f"{reord} of {pp['machine'].nunique()} builder(s) reordered",
                           "target": "0 / 0", "verdict": "PASS" if ok8 else "FAIL",
                           "since_step": "v42f", "note": ""})
        c3 = cp[cp["feasible_start"].notna()]
        reord_c = 0
        for _m, _g in c3.groupby("press"):
            _pa = list(_g.sort_values(["planned_start"]).index)
            _pb = list(_g.sort_values(["feasible_start", "planned_start"]).index)
            reord_c += int(_pa != _pb)
        checks.append({"gate": "G8", "line": L,
                       "check": "curing: same press and same lot order as the plan",
                       "value": f"0 lot(s) on another press (fixed by design); "
                                f"{reord_c} of {c3['press'].nunique()} press(es) reordered",
                       "target": "0", "verdict": "PASS" if reord_c == 0 else "FAIL",
                       "since_step": "v42f", "note": ""})
        age = ((cp["feasible_start"] - cp["gt_ready"]).dt.total_seconds() / 3600).dropna()
        mx = float(age.max()) if len(age) else 0.0
        checks.append({"gate": "G12", "line": L, "check": "green-tyre wait before cure (max)",
                       "value": f"{mx:.1f} h; {int((age > 72).sum())} lots > 72 h",
                       "target": "<= 168 h", "verdict": "PASS" if mx <= 168 else "FAIL",
                       "since_step": 0, "note": "wait from GT ready to cure start"})
        tagged = cp["sku_state"].isin([BP.CLEAN, BP.ASSUMED, BP.BLOCKED]).all()
        checks.append({"gate": "G6", "line": L,
                       "check": "data-blocked demand excluded from final figures and listed",
                       "value": f"{round(blk):,} tyres excluded, {len(sb)} SKU(s) listed",
                       "target": "every lot tagged; every blocked SKU listed",
                       "verdict": "PASS" if tagged and x["sku"].nunique() <= len(sb) else "FAIL",
                       "since_step": 1, "note": ""})
        nosrc = int(reg["source"].isna().sum()) if "source" in reg else 0
        unk = sorted(set(reg["code"]) - set(BP.ASSUMPTIONS)) if "code" in reg else []
        checks.append({"gate": "G7", "line": L, "check": "every assumption has a code and a source",
                       "value": f"{len(reg)} assumption(s); {nosrc} without source; "
                                f"unknown codes {unk or 'none'}",
                       "target": "0 without source, 0 unknown",
                       "verdict": "PASS" if nosrc == 0 and not unk else "FAIL",
                       "since_step": 1, "note": ""})

    # duplicate lot ids across lines (G4)
    sa = pd.concat(sched_all, ignore_index=True) if sched_all else pd.DataFrame()
    if not sa.empty:
        dup = sa.groupby("lot_id")["line"].nunique()
        nd = int((dup > 1).sum())
        checks.append({"gate": "G4", "line": "BOTH",
                       "check": "lot ids (upstream + building) unique across lines",
                       "value": f"{nd} duplicated", "target": "0",
                       "verdict": "PASS" if nd == 0 else "FAIL", "since_step": 4,
                       "note": "line-prefixed lot ids (v40)"})
    checks.append({"gate": "G5", "line": "BOTH", "check": "lots on non-allowable press",
                   "value": "not measured here", "target": "0", "verdict": "N/A",
                   "since_step": 0, "note": "measured by audit_v38/audit_v38.py curing checks"})

    S = pd.DataFrame(summ)
    if len(S):
        tot = S.drop(columns=["line"]).sum(numeric_only=True)
        row = {"line": "TOTAL", **tot.to_dict()}
        row["fulfilment_pct_eligible"] = round(100 * tot["cured_eligible"]
                                               / max(tot["eligible_tyres"], 1), 1)
        row["fulfilment_pct_raw_plan"] = round(100 * tot["cured_raw"]
                                               / max(tot["plan_tyres_raw"], 1), 1)
        S = pd.concat([S, pd.DataFrame([row])], ignore_index=True)
    B = pd.DataFrame(blocked)
    R = (B.groupby(["blocker_code", "missing", "missing_table", "missing_key", "owner"])
          .agg(skus=("sku", "nunique"), cure_tyres=("cure_tyres_excluded", "sum"))
          .reset_index().sort_values("cure_tyres", ascending=False)) if len(B) else pd.DataFrame()
    A = pd.DataFrame(asm_rows)
    AC = pd.DataFrame([{"code": c, **{k: v for k, v in m.items()},
                        "used_in_this_run": int((A["code"] == c).sum()) if len(A) else 0}
                       for c, m in BP.ASSUMPTIONS.items()])
    BC = pd.DataFrame([{"code": c, **m} for c, m in BP.BLOCKERS.items()])
    T = pd.DataFrame(assumed)
    G = pd.concat(gaps, ignore_index=True) if gaps else pd.DataFrame()
    GS = (G.groupby(["line", "kind", "db_table"]).agg(rows=("key", "size"),
                                                      keys=("key", "nunique"))
           .reset_index().sort_values(["line", "rows"], ascending=[True, False])
          if len(G) else pd.DataFrame())
    C = pd.DataFrame(checks)
    stamp = _stamp(a.run)

    D.mkdir(parents=True, exist_ok=True)
    S.to_csv(D / "VALIDATION_SUMMARY.csv", index=False, encoding="utf-8-sig")
    B.to_csv(D / "VALIDATION_REPORT_blocked.csv", index=False, encoding="utf-8-sig")
    A.to_csv(D / "ASSUMPTIONS.csv", index=False, encoding="utf-8-sig")
    _xlsx(D / "VALIDATION_REPORT.xlsx", S, B, R, A, AC, BC, T, C, stamp, GS, G)

    # ---- console ----------------------------------------------------------
    print("\n" + "=" * 78)
    print(f"  VALIDATION REPORT   [{a.run}]   (data-blocker policy, v39)")
    print("=" * 78)
    for r in S.itertuples(index=False):
        print(f"  {r.line:5s} raw plan {r.plan_tyres_raw:>9,.0f}  blocked {r.data_blocked_tyres:>7,.0f}"
              f"  eligible {r.eligible_tyres:>9,.0f}  cured {r.cured_eligible:>9,.0f}"
              f"  = {r.fulfilment_pct_eligible}%  (raw {r.fulfilment_pct_raw_plan}%)")
    for r in (B.itertuples(index=False) if len(B) else []):
        print(f"    BLOCKED {r.line} {r.blocker_code} {r.sku:22s} {r.cure_tyres_excluded:>6,} "
              f"tyres  <- {r.missing_table} [{r.missing_key}]")
    if len(G):
        print(f"  data gaps closed by a stand-in: {len(G)} row(s) "
              f"({', '.join(f'{k}={n}' for k, n in G['kind'].value_counts().items())})")
    print(f"  assumptions: {len(A)} row(s)  " +
          (", ".join(f"{c}={n}" for c, n in A["code"].value_counts().sort_index().items())
           if len(A) else ""))
    for r in C.itertuples(index=False):
        print(f"    {r.gate:4s} {r.line:5s} {r.verdict:5s} {r.check}: {r.value}")
    _fatal = set(FATAL)
    if str(RULES.rule("building.enforce_plan_sequence", False)).lower() in ("1", "true", "on", "yes"):
        _fatal.add("G8")                    # v42f: plan sequence is a hard rule
    fail = C[(C["verdict"] == "FAIL") & C["gate"].isin(_fatal)]
    print(f"\n[OK] {D / 'VALIDATION_REPORT.xlsx'}")
    if len(fail):
        print(f"[FATAL] gate(s) failed: {sorted(set(fail['gate']))}", file=sys.stderr)
        return 1
    return 0


def _stamp(run: str) -> pd.DataFrame:
    root = io.ROOT if hasattr(io, "ROOT") else pathlib.Path(__file__).resolve().parents[1]
    data = pathlib.Path(os.environ.get("CTP_DATA", ""))
    rows = [("run", run), ("generated_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("rules", os.environ.get("CTP_RULES", "")), ("data_folder", str(data)),
            ("plan_dir", os.environ.get("CTP_PLAN_DIR", "")),
            ("CTP_WIP_ASOF", os.environ.get("CTP_WIP_ASOF", "(not set in this shell)")),
            ("CTP_GT_ASOF", os.environ.get("CTP_GT_ASOF", "(not set in this shell)"))]
    for f, keys in (("_sql_manifest.json", ("staged_at", "ppds_only")),
                    ("_wip_source.json", ("table", "note")),
                    ("_wip_manifest.json", ("source", "source_rows"))):
        try:
            j = json.loads((data / f).read_text(encoding="utf-8"))
            rows += [(f"{f}:{k}", str(j.get(k, ""))) for k in keys]
        except Exception:
            pass
    try:
        g = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
        d = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "engine"],
                           capture_output=True, text=True, timeout=20).stdout.strip()
        rows.append(("git_commit", g + (" + uncommitted engine changes" if d else "")))
    except Exception:
        pass
    return pd.DataFrame(rows, columns=["field", "value"])


def _xlsx(path, S, B, R, A, AC, BC, T, C, stamp, GS, G):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    red, grn, amb = (PatternFill("solid", fgColor=c) for c in ("F8CBAD", "C6EFCE", "FFEB9C"))
    hdr = PatternFill("solid", fgColor="1F4E78")
    note = pd.DataFrame({"read me": [
        "Final figures are on ELIGIBLE demand: plan minus DATA_BLOCKED demand.",
        "DATA_BLOCKED = a master record is missing and no safe stand-in exists (B-codes). "
        "Those tyres are excluded from plan AND cured, and listed in 'Blocked SKUs'.",
        "ASSUMED = a gap is filled by a stated assumption (A-codes, 'Assumptions'). "
        "Those tyres ARE counted; their share is shown separately.",
        "Fix the rows in 'Blocking records' (largest first) to bring demand back in.",
        "'Data gaps (handled)' = values the database did not supply that the engine "
        "closed with a stated stand-in (column 'impact' says what was applied); "
        "no demand is excluded for them."]})
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        sheets = [("Summary", S), ("Blocked SKUs", B), ("Blocking records", R),
                  ("Assumptions", A), ("Assumption catalogue", AC),
                  ("Blocker catalogue", BC), ("Assumed tyres", T),
                  ("Data gaps (handled)", GS), ("Data gap detail", G),
                  ("Schedule checks", C), ("Run stamp", stamp)]
        for name, df in sheets:
            df = df if len(df) else pd.DataFrame({"(none)": []})
            df.to_excel(xw, sheet_name=name, index=False,
                        startrow=len(note) + 2 if name == "Summary" else 0)
            ws = xw.sheets[name]
            r0 = len(note) + 3 if name == "Summary" else 1
            if name == "Summary":
                for i, t in enumerate(note["read me"], start=1):
                    ws.cell(row=i, column=1, value=t).font = Font(italic=True)
            for c in ws[r0]:
                c.font, c.fill = Font(bold=True, color="FFFFFF"), hdr
                c.alignment = Alignment(wrap_text=True, vertical="top")
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[get_column_letter(j)].width = min(max(10, w + 2), 60)
            ws.freeze_panes = ws.cell(row=r0 + 1, column=1)
            if name == "Schedule checks" and "verdict" in df:
                vc = list(df.columns).index("verdict") + 1
                for i, v in enumerate(df["verdict"], start=r0 + 1):
                    ws.cell(row=i, column=vc).fill = {"PASS": grn, "FAIL": red}.get(v, amb)
            if name == "Blocked SKUs" and len(df):
                for row in ws.iter_rows(min_row=r0 + 1, max_row=r0 + len(df)):
                    for c in row:
                        c.fill = red


if __name__ == "__main__":
    sys.exit(main())
