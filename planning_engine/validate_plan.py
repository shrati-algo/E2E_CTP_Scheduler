#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate_plan.py -- STAGE C: does the engine reproduce the plant?

The engine is only trustworthy for a FUTURE month if, fed the month the plant actually faced, it
lands on the plan the plant actually ran. This module scores the generated plan against the June MES
actuals on the dimensions that decide whether a plan is executable:

  volume        -- per-SKU monthly quantity (must be exact; anything else is a different plan)
  press count   -- presses used per SKU vs presses the plant used, and the mould cap
  changeovers   -- press changeovers/day and TBM changeovers/day vs measured 0.05 and 2.65
  campaigns     -- median press campaign hours vs measured 195 h
  interlock     -- realised build->cure lead in the generated pair vs measured 4.34 h median
  daily balance -- day-by-day cure volume vs actual (a plan that is right in total but wrong in
                   shape starves the floor in week 1 and floods it in week 4)

Usage:
    python -m planning_engine.validate_plan [--plan-dir planning_engine/_plan]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"
RULEBOOK = HERE / "_rulebook"


def _actual_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    cure = pd.read_parquet(CACHE / "cure_events.parquet")
    build = pd.read_parquet(CACHE / "build_events.parquet")
    j = cure.merge(build[["gtbarCode", "tbm", "item"]], on="gtbarCode", how="left")
    return j[j["item"].notna()].copy(), build[build["tbm"].notna() & build["item"].notna()].copy()


def _campaigns(df: pd.DataFrame, machine: str, key: str, tcol: str,
               endcol: str | None = None) -> pd.DataFrame:
    """Consecutive same-item runs on a machine.

    `endcol` matters: a campaign made of ONE block has zero span measured start-to-start, so a plan
    whose campaigns are single blocks would score 0 h against the plant's 5.28 h. Measured
    start-to-END it reports the real length.
    """
    d = df.sort_values([machine, tcol]).copy()
    d["newc"] = (d[key] != d.groupby(machine)[key].shift()).astype(int)
    d["camp"] = d.groupby(machine)["newc"].cumsum()
    agg = {"item": (key, "first"), "n": (key, "size"), "s": (tcol, "min"),
           "e": ((endcol or tcol), "max")}
    g = d.groupby([machine, "camp"]).agg(**agg).reset_index()
    g["h"] = (g["e"] - g["s"]).dt.total_seconds() / 3600.0
    return g


def validate(plan_dir: Path) -> dict:
    curing = pd.read_csv(plan_dir / "plan.csv")
    building = pd.read_csv(plan_dir / "building_plan.csv")
    req = pd.read_csv(plan_dir / "requirement_used.csv")
    rb = json.loads((RULEBOOK / "rulebook.json").read_text(encoding="utf-8"))
    act_cure, act_build = _actual_frames()

    curing["startTime"] = pd.to_datetime(curing["startTime"])
    building["startTime"] = pd.to_datetime(building["startTime"])
    sku2gt = dict(zip(req["sku_code"], req["gt_code"]))
    curing["gt"] = curing["skuCode"].map(sku2gt).fillna(curing["skuCode"])
    building["gt"] = building["skuCode"].map(sku2gt).fillna(building["skuCode"])

    res: dict = {}

    # --- volume ------------------------------------------------------------------------------
    plan_gt = curing.groupby("gt")["qty"].sum()
    act_gt = act_cure.groupby("item").size()
    both = plan_gt.index.union(act_gt.index)
    p = plan_gt.reindex(both).fillna(0)
    a = act_gt.reindex(both).fillna(0)
    res["volume"] = {
        "plan_total": int(p.sum()), "actual_total": int(a.sum()),
        "total_err_pct": round(100 * (p.sum() - a.sum()) / a.sum(), 3),
        "per_gt_mean_abs_err_pct": round(float((100 * (p - a).abs() / a.clip(lower=1)).mean()), 2),
        "gts_within_2pct": int((100 * (p - a).abs() / a.clip(lower=1) <= 2).sum()),
        "n_gt": int(len(both)),
    }

    # --- press usage vs mould cap -------------------------------------------------------------
    plan_press = curing.groupby("gt")["pressNo"].nunique()
    act_press = act_cure.groupby("item")["press"].nunique()
    moulds = pd.read_csv(RULEBOOK / "mould_capacity.csv").set_index("gt_code")["mould_sets_instant"]
    cmp_press = pd.DataFrame({"plan": plan_press, "actual": act_press, "mould_cap": moulds}).fillna(0)
    res["presses"] = {
        "plan_presses_used": int(curing["pressNo"].nunique()),
        "actual_presses_used": int(act_cure["press"].nunique()),
        "gts_over_mould_cap": int((cmp_press["plan"] > cmp_press["mould_cap"]).sum()),
        "median_plan_vs_actual_presses": [int(cmp_press["plan"].median()),
                                          int(cmp_press["actual"].median())],
    }

    # --- changeovers / campaigns --------------------------------------------------------------
    curing["endTime"] = pd.to_datetime(curing["endTime"])
    building["endTime"] = pd.to_datetime(building["endTime"])
    pc = _campaigns(curing.rename(columns={"pressNo": "m"}), "m", "gt", "startTime", "endTime")
    days = curing["startTime"].dt.floor("D").nunique()
    n_press = curing["pressNo"].nunique()
    bc = _campaigns(building.rename(columns={"buildMachine": "m"}), "m", "gt", "startTime", "endTime")
    n_tbm = building["buildMachine"].nunique()
    res["campaigns"] = {
        "plan_press_chg_per_press_per_day": round((len(pc) - n_press) / n_press / days, 4),
        "actual_press_chg_per_press_per_day": rb["campaigns"]["press_changeovers_per_press_per_day"],
        "plan_press_campaign_h_median": round(float(pc["h"].median()), 1),
        "actual_press_campaign_h_median": rb["campaigns"]["press_campaign_h_median"],
        "plan_tbm_chg_per_tbm_per_day": round((len(bc) - n_tbm) / n_tbm / days, 3),
        "actual_tbm_chg_per_tbm_per_day": rb["campaigns"]["tbm_changeovers_per_tbm_per_day"],
        "plan_tbm_campaign_h_median": round(float(bc["h"].median()), 2),
        "actual_tbm_campaign_h_median": rb["campaigns"]["tbm_campaign_h_median"],
    }

    # --- interlock: realised lead between the two generated plans -----------------------------
    # Per SKU, walk both cumulative curves and measure the gap at matched cumulative quantity --
    # the plan's own FEFO dwell, directly comparable to the MES 4.34 h median.
    #
    # A green tyre is only available when its campaign FINISHES, and a press starts drawing at the
    # START of its shift. Crediting build at its start (or cure at its end) would flatter the plan by
    # a whole campaign length and hide real starvation, so the pessimistic end of each is used.
    # Opening WIP is supply too: the engine states what stock the month must start with, and those
    # tyres are available at t0. Leaving them out would make every SKU's build curve lag its cure
    # curve by that quantity and report a systematic negative lead that does not exist.
    opening_wip: dict[str, float] = {}
    plan_report = plan_dir / "building_report.json"
    if plan_report.exists():
        rep = json.loads(plan_report.read_text(encoding="utf-8"))
        for sku, q in (rep.get("opening_wip_required") or {}).items():
            gt = sku2gt.get(sku, sku)
            opening_wip[gt] = opening_wip.get(gt, 0.0) + float(q)
    t0 = building["startTime"].min()

    # Both curves are CONTINUOUS: a press consumes evenly across its shift, a TBM delivers evenly
    # across its campaign. Crediting a campaign's whole output at its finish (or a shift's whole
    # draw at its start) is a lumpy approximation that reports starvation where none exists -- it
    # scored 30% negative lead on a plan that never actually runs a press dry.
    step_h = 0.5
    end_h = max((curing["endTime"].max() - t0).total_seconds(),
                (building["endTime"].max() - t0).total_seconds()) / 3600.0
    grid = np.arange(0.0, end_h + step_h, step_h)

    def _cum(df: pd.DataFrame, scol: str, ecol: str) -> np.ndarray:
        out = np.zeros_like(grid)
        s = (df[scol] - t0).dt.total_seconds().values / 3600.0
        e = (df[ecol] - t0).dt.total_seconds().values / 3600.0
        q = df["qty"].values.astype(float)
        for si, ei, qi in zip(s, e, q):
            if ei > si and qi > 0:
                out += qi * np.clip((grid - si) / (ei - si), 0.0, 1.0)
        return out

    leads, starve_pts, starve_tot, worst_deficit = [], 0, 0, 0.0
    for gt, cg in curing.groupby("gt"):
        bg = building[building["gt"] == gt]
        cure_cum = _cum(cg, "startTime", "endTime")
        build_cum = _cum(bg, "startTime", "endTime") if not bg.empty else np.zeros_like(grid)
        build_cum = build_cum + opening_wip.get(gt, 0.0)     # stock is available from t0
        deficit = cure_cum - build_cum
        worst_deficit = max(worst_deficit, float(deficit.max()))
        starve_pts += int((deficit > 1e-6).sum())
        starve_tot += len(grid)
        # horizontal gap between the curves: when was the tyre the press draws at time t built?
        lv = np.linspace(0.05, 0.95, 19) * cure_cum[-1]
        t_cure = np.interp(lv, cure_cum, grid)
        t_build = np.interp(lv, build_cum, grid)
        leads.extend((t_cure - t_build).tolist())
    leads = np.array([x for x in leads if -48 < x < 240])
    res["interlock"] = {
        "plan_lead_h_median": round(float(np.median(leads)), 2) if leads.size else None,
        "plan_lead_h_p10": round(float(np.percentile(leads, 10)), 2) if leads.size else None,
        "plan_lead_h_p90": round(float(np.percentile(leads, 90)), 2) if leads.size else None,
        "actual_dwell_h_median": rb["interlock"]["dwell_h_median"],
        "actual_dwell_h_p90": rb["interlock"]["dwell_h_p90"],
        # the metric that matters: fraction of plan time any SKU is short of green tyres
        "starved_time_pct": round(100.0 * starve_pts / max(starve_tot, 1), 3),
        "worst_deficit_tyres": round(worst_deficit, 1),
        "opening_wip_credited": round(sum(opening_wip.values()), 1),
    }

    # --- daily shape -------------------------------------------------------------------------
    pd_day = curing.assign(d=curing["startTime"].dt.floor("D")).groupby("d")["qty"].sum()
    ad_day = act_cure.assign(d=act_cure["cure_time"].dt.floor("D")).groupby("d").size()
    common = pd_day.index.intersection(ad_day.index)
    if len(common):
        pv, av = pd_day.reindex(common), ad_day.reindex(common)
        res["daily_shape"] = {
            "days": int(len(common)),
            "mean_abs_err_pct": round(float((100 * (pv - av).abs() / av.clip(lower=1)).mean()), 2),
            "worst_day_err_pct": round(float((100 * (pv - av).abs() / av.clip(lower=1)).max()), 2),
            "plan_daily_median": int(pv.median()), "actual_daily_median": int(av.median()),
        }
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Score the generated plan against June MES actuals.")
    ap.add_argument("--plan-dir", default=str(HERE / "_plan"))
    a = ap.parse_args()
    res = validate(Path(a.plan_dir))

    v = res["volume"]
    print(f"  VOLUME     plan {v['plan_total']:,} vs actual {v['actual_total']:,} "
          f"({v['total_err_pct']:+.2f}%) | per-GT mean abs err {v['per_gt_mean_abs_err_pct']}% | "
          f"{v['gts_within_2pct']}/{v['n_gt']} GTs within 2%")
    p = res["presses"]
    print(f"  PRESSES    plan uses {p['plan_presses_used']} vs actual {p['actual_presses_used']} | "
          f"GTs over mould cap: {p['gts_over_mould_cap']} (must be 0)")
    c = res["campaigns"]
    print(f"  CURING SEQ chg/press/day {c['plan_press_chg_per_press_per_day']} vs actual "
          f"{c['actual_press_chg_per_press_per_day']} | campaign median "
          f"{c['plan_press_campaign_h_median']}h vs {c['actual_press_campaign_h_median']}h")
    print(f"  BUILD SEQ  chg/tbm/day {c['plan_tbm_chg_per_tbm_per_day']} vs actual "
          f"{c['actual_tbm_chg_per_tbm_per_day']} | campaign median "
          f"{c['plan_tbm_campaign_h_median']}h vs {c['actual_tbm_campaign_h_median']}h")
    i = res["interlock"]
    print(f"  INTERLOCK  plan lead median {i['plan_lead_h_median']}h (p10 {i['plan_lead_h_p10']} "
          f"p90 {i['plan_lead_h_p90']}) vs actual dwell {i['actual_dwell_h_median']}h "
          f"(p90 {i['actual_dwell_h_p90']})")
    print(f"  STARVATION press short of green tyres {i['starved_time_pct']}% of plan time "
          f"(worst gap {i['worst_deficit_tyres']:,.0f} tyres) with "
          f"{i['opening_wip_credited']:,.0f} opening WIP credited")
    if "daily_shape" in res:
        d = res["daily_shape"]
        print(f"  DAILY      mean abs err {d['mean_abs_err_pct']}% over {d['days']} days "
              f"(worst {d['worst_day_err_pct']}%) | plan/day {d['plan_daily_median']:,} vs "
              f"actual {d['actual_daily_median']:,}")
    (Path(a.plan_dir) / "validation.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(f"  wrote -> {Path(a.plan_dir) / 'validation.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
