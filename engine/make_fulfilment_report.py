#!/usr/bin/env python3
"""make_fulfilment_report.py - day-wise fulfilment, PCR and TBR, plus the why.

    python make_fulfilment_report.py --run e2e_v15

THREE TABLES, AND THEY ANSWER DIFFERENT QUESTIONS

  1  BUILDING, day by day. The plan is the frozen building anchor. "Built" is
     bucketed on FEASIBLE_END - the day the trolley actually finishes - never
     on its planned day. Bucketing the achieved figure on the planned day just
     restates the plan and reads 100% every day, which is the commonest way a
     schedule flatters itself.

  2  CURING, day by day, gated on green tyres that actually exist.

  3  WHERE THE SHORTFALL WENT. A tyre that is not cured inside the horizon is
     not lost - it is late. This names how much landed outside the horizon,
     how much never had a green tyre, and how much a PM block pushed.

v39 DATA-BLOCKER POLICY: every figure here is on ELIGIBLE demand. Cure lots of
a DATA_BLOCKED SKU (phase0_sku_status.csv / blocker_policy.py) are taken out of
both the plan and the cured count; their tyres are stated on a separate line
and listed in VALIDATION_REPORT.xlsx.

A DAY CAN EXCEED 100%. That is not an error: a trolley planned for the 27th
that finishes on the 28th is counted on the 28th, so the 28th carries its own
plan plus the carry-in. The TOTAL row is the number that matters.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import blocker_policy as BP
import ctpio as io


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="e2e_v15")
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run

    print("\n" + "=" * 96)
    print(f"  FULFILMENT - day-wise, per line   [{a.run}]")
    print("=" * 96)

    rows, notes = [], []
    for ln in ("pcr", "tbr"):
        b = pd.read_csv(D / ln / "phase5_building_plan.csv",
                        parse_dates=["build_start", "feasible_end"])
        c = pd.read_csv(D / ln / "phase7_curing_plan.csv",
                        parse_dates=["planned_start", "feasible_end"])
        c_raw = c
        if "in_kpi" in c.columns:
            c = c[c["in_kpi"].astype(str).str.lower() == "true"]
        try:
            st = pd.read_csv(D / ln / BP.STATUS_FILE, dtype=str)
            blk = set(st.loc[st["state"] == BP.BLOCKED, "sku"])
            b = b[~b["sku"].astype(str).isin(blk)]
        except FileNotFoundError:
            pass
        excl = c_raw[~c_raw.index.isin(c.index)]
        bw, cw = b[b["within_horizon"]], c[c["within_horizon"]]

        bp = b.groupby(b["build_start"].map(lambda t: io.plant_day(t).date()))["qty"].sum()
        bb = bw.groupby(bw["feasible_end"].map(lambda t: io.plant_day(t).date()))["qty"].sum()
        cp = c.groupby(c["planned_start"].map(lambda t: io.plant_day(t).date()))["qty_planned"].sum()
        cc = cw.groupby(cw["feasible_end"].map(lambda t: io.plant_day(t).date()))["qty"].sum()

        days = sorted(set(bp.index) | set(bb.index) | set(cp.index) | set(cc.index))
        for d in days:
            _bp, _bb = float(bp.get(d, 0)), float(bb.get(d, 0))
            _cp, _cc = float(cp.get(d, 0)), float(cc.get(d, 0))
            rows.append({"line": ln.upper(), "day": d,
                         "build_plan": round(_bp), "built": round(_bb),
                         "built_pct": round(100 * _bb / _bp, 1) if _bp else None,
                         "cure_plan": round(_cp), "cured": round(_cc),
                         "cured_pct": round(100 * _cc / _cp, 1) if _cp else None})
        rows.append({"line": ln.upper(), "day": "TOTAL",
                     "build_plan": round(float(b["qty"].sum())),
                     "built": round(float(bw["qty"].sum())),
                     "built_pct": round(100 * bw["qty"].sum() / max(b["qty"].sum(), 1), 1),
                     "cure_plan": round(float(c["qty_planned"].sum())),
                     "cured": round(float(cw["qty"].sum())),
                     "cured_pct": round(100 * cw["qty"].sum() / max(c["qty_planned"].sum(), 1), 1)})

        # ---- where the shortfall went
        late = c[~c["within_horizon"]]
        nogt = c[c["gt_ready"].isna()] if "gt_ready" in c.columns else c.iloc[0:0]
        pushed = c[c["push_h"] > 0]
        notes.append({
            "line": ln.upper(),
            "cure_plan_raw": round(float(c_raw["qty_planned"].sum())),
            "data_blocked_excluded": round(float(excl["qty_planned"].sum())),
            "data_blocked_skus": int(excl["sku"].nunique()),
            "cure_plan": round(float(c["qty_planned"].sum())),
            "cured_in_horizon": round(float(cw["qty"].sum())),
            "shortfall": round(float(c["qty_planned"].sum() - cw["qty"].sum())),
            "lots_outside_horizon": int(len(late)),
            "tyres_outside_horizon": round(float(late["qty"].sum())),
            "lots_with_no_green_tyre": int(len(nogt)),
            "tyres_with_no_green_tyre": round(float(nogt["qty"].sum())),
            "lots_pushed": int(len(pushed)),
            "median_push_h": round(float(pushed["push_h"].median()), 2) if len(pushed) else 0.0,
            "max_push_h": round(float(pushed["push_h"].max()), 2) if len(pushed) else 0.0,
            "even_lot_rounding_loss": round(
                float(c["qty_planned"].sum() - c["qty"].sum())),
        })

    f = pd.DataFrame(rows)
    n = pd.DataFrame(notes)
    f.to_csv(D / "FULFILMENT_DAYWISE_v15.csv", index=False, encoding="utf-8-sig")
    n.to_csv(D / "FULFILMENT_SHORTFALL_v15.csv", index=False, encoding="utf-8-sig")

    for ln in ("PCR", "TBR"):
        g = f[f["line"] == ln]
        print(f"\n  ---- {ln} ----")
        print(f"  {'day':<12}{'build plan':>12}{'built':>10}{'built %':>10}"
              f"{'cure plan':>12}{'cured':>10}{'cured %':>10}")
        for r in g.itertuples(index=False):
            sep = "  " + "-" * 76 if r.day == "TOTAL" else ""
            if sep:
                print(sep)
            print(f"  {str(r.day):<12}{r.build_plan:>12,}{r.built:>10,}"
                  f"{(str(r.built_pct)+'%' if r.built_pct is not None else '-'):>10}"
                  f"{r.cure_plan:>12,}{r.cured:>10,}"
                  f"{(str(r.cured_pct)+'%' if r.cured_pct is not None else '-'):>10}")
        w = n[n["line"] == ln].iloc[0]
        print(f"    shortfall {w['shortfall']:,} tyres = "
              f"{w['tyres_outside_horizon']:,} landed outside the horizon"
              f" + {w['tyres_with_no_green_tyre']:,} never had a green tyre"
              f" + {w['even_lot_rounding_loss']:,} lost to even-lot rounding")
        print(f"    excluded, data-blocked: {w['data_blocked_excluded']:,} tyres on "
              f"{w['data_blocked_skus']} SKU(s) (raw plan {w['cure_plan_raw']:,})"
              f" - see VALIDATION_REPORT.xlsx")

    tot = f[f["day"] == "TOTAL"]
    print(f"\n  ---- BOTH LINES ----")
    print(f"    build plan {tot['build_plan'].sum():,}   built "
          f"{tot['built'].sum():,}   = "
          f"{100*tot['built'].sum()/max(tot['build_plan'].sum(),1):.1f}%")
    print(f"    excluded, data-blocked: {n['data_blocked_excluded'].sum():,} tyres "
          f"(raw cure plan {n['cure_plan_raw'].sum():,})")
    print(f"    cure  plan {tot['cure_plan'].sum():,} (eligible)   cured "
          f"{tot['cured'].sum():,}   = "
          f"{100*tot['cured'].sum()/max(tot['cure_plan'].sum(),1):.1f}%")
    print(f"\n[OK] {D / 'FULFILMENT_DAYWISE_v15.csv'}")
    print(f"[OK] {D / 'FULFILMENT_SHORTFALL_v15.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
