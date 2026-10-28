#!/usr/bin/env python3
"""built_not_cured.py - green tyres are built, why are they not cured?

    CTP_DATA=<staged data> CTP_RULES=<rules> CTP_OUT=<out> python analysis/built_not_cured.py --run <run>
    (run_pipeline.py sets the environment.)

Rebuilds phase 7's own green-tyre supply per SKU (GT opening stock as phase 7
shares it, + every trolley at feasible_end + GT min ageing) and walks every
press lot in the curing plan:

  per SKU   stock_usable, built (GT ready inside the horizon), loaded (press lots
            started inside the horizon), cured (lots ENDED inside the horizon),
            GT left on the rack at the horizon end, cure plan not loaded.
  per lot   why it did not start / not finish inside the horizon:
            STARTED_NOT_FINISHED  press started, ends after the horizon
            WAIT_GT_WHOLE_LOT     not all of the lot's green tyres exist yet
                                  (phase 7 gates a lot on its LAST tyre)
            PRESS_QUEUE           its green tyres were ready in time, the press
                                  was still busy with earlier (pushed) lots
            MOULD_CHANGE          the gap was the mould change in front of it
            NO_GT_EVER            the SKU never gets enough green tyres
            DATA_BLOCKED          SKU blocked (no BOM)
  per hour  GT on the rack (built - loaded) by SKU, and press idle hours while
            green tyres of ANOTHER SKU it is planned for sit ready (plan order
            is fixed: G8)

Writes <out>/<run>/analysis/BUILT_NOT_CURED_<run>.xlsx (+ csv copies)
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as CM          # noqa: E402
import ctpio as io            # noqa: E402
import blocker_policy as BP   # noqa: E402


def supply_per_sku(line, OUT, blots, cure):
    """Same construction as phase7_curing.py (v38/v41/v42 GT stock rules)."""
    bom = io.load_bom(line)
    itypes = io.load_itemtypes(bom)
    aging = io.load_aging(line)
    aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)
    plan_start = blots["build_start"].min()
    gt_stock = {}
    g = io._rd(f"gt_inventory_manual_{line}")
    g.columns = [str(c).strip() for c in g.columns]
    for r in g.itertuples(index=False):
        s = io._s(getattr(r, "SKUCode", ""))
        q = pd.to_numeric(getattr(r, "TotalQuantity", 0), errors="coerce")
        if s and pd.notna(q) and q > 0:
            gt_stock[s] = gt_stock.get(s, 0.0) + float(q)
    gt_item = dict(zip(blots["sku"], blots["built_item"]))
    _cgt = (cure.assign(gt_code=cure["gt_code"].map(io._s))
                .query("gt_code != ''").groupby("sku")["gt_code"].first())
    for _sku, _g in _cgt.items():
        gt_item.setdefault(_sku, _g)
    try:
        _stt = pd.read_csv(OUT / BP.STATUS_FILE, dtype=str).fillna("")
        blocked = set(_stt.loc[_stt["state"] == BP.BLOCKED, "sku"])
    except FileNotFoundError:
        blocked = set()
    gt_alloc = defaultdict(float)
    by_gt = {k: q for k, q in gt_stock.items() if k in set(gt_item.values())}
    _c = (cure.assign(gt=cure["sku"].map(gt_item), _blk=cure["sku"].isin(blocked))
              .sort_values(["_blk", "start"], kind="stable"))
    for gc, q in by_gt.items():
        mx_g = float(aging.get(gc, (0.0, 0.0))[1]) or 48.0
        exp_g = plan_start + pd.Timedelta(hours=mx_g)
        left = float(q)
        for r in _c[(_c["gt"] == gc) & (_c["start"] <= exp_g)].itertuples(index=False):
            if left <= 0:
                break
            take = min(left, float(r.qty))
            gt_alloc[r.sku] += take
            left -= take
    out = {}
    for sku, grp in cure.groupby("sku", sort=False):
        mn = float(aging.get(gt_item.get(sku, ""), (0.0, 0.0))[0])
        mx = float(aging.get(gt_item.get(sku, ""), (0.0, 0.0))[1]) or 48.0
        bb = blots[(blots["sku"] == sku) & blots["feasible_end"].notna()].sort_values("feasible_end")
        ev = [(t + pd.Timedelta(hours=mn), float(q), "built") for t, q in zip(bb["feasible_end"], bb["qty"])]
        inv = float(gt_stock.get(sku, 0.0)) + float(gt_alloc.get(sku, 0.0))
        usable = 0.0
        if inv > 0:
            exp = plan_start + pd.Timedelta(hours=mx)
            usable = min(inv, float(grp.loc[grp["start"] <= exp, "qty"].sum()))
            if usable > 0:
                ev = [(plan_start, usable, "stock")] + ev
        out[sku] = {"events": ev, "stock_raw": inv, "stock_usable": usable,
                    "gt": gt_item.get(sku, ""), "min_age_h": mn, "max_age_h": mx}
    # SKUs built but with no curing row at all
    for sku in set(blots["sku"]) - set(out):
        bb = blots[(blots["sku"] == sku) & blots["feasible_end"].notna()]
        out[sku] = {"events": [(t, float(q), "built") for t, q in zip(bb["feasible_end"], bb["qty"])],
                    "stock_raw": 0.0, "stock_usable": 0.0, "gt": gt_item.get(sku, ""),
                    "min_age_h": 0.0, "max_age_h": 0.0}
    return out, plan_start, blocked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    SKU_ROWS, LOT_ROWS, HOUR_ROWS, IDLE_ROWS, DAY_ROWS = [], [], [], [], []
    for line in ("pcr", "tbr"):
        OUT = D / line
        blots = pd.read_csv(OUT / "phase5_building_plan.csv",
                            parse_dates=["build_start", "build_end", "feasible_start", "feasible_end"])
        cp = pd.read_csv(OUT / "phase7_curing_plan.csv",
                         parse_dates=["planned_start", "planned_end", "feasible_start",
                                      "feasible_end", "gt_ready"])
        cure, _ = io.load_curing_plan(line, io.machine_alias(io.load_machines()))
        hz = blots["build_end"].max()
        sup, t0, blocked = supply_per_sku(line, OUT, blots, cure)
        cp = cp.sort_values(["press", "planned_start"], kind="stable").reset_index(drop=True)
        cp["planned_span"] = cp["planned_end"] - cp["planned_start"]

        # ---- per lot: why not cured inside the horizon --------------------
        prev_end = {}
        for r in cp.itertuples(index=False):
            reason, detail = "", ""
            if r.within_horizon:
                reason = "CURED"
            elif r.sku_state == BP.BLOCKED if hasattr(BP, "BLOCKED") else False:
                reason = "DATA_BLOCKED"
            elif pd.isna(r.gt_ready):
                reason = "NO_GT_EVER"
            elif pd.notna(r.feasible_start) and r.feasible_start <= hz:
                reason = "STARTED_NOT_FINISHED"
                detail = f"starts {r.feasible_start:%d %H:%M}, ends {r.feasible_end:%d %H:%M}"
            elif r.gt_ready > hz:
                reason = "WAIT_GT_WHOLE_LOT"
                detail = f"last GT of the lot ready {r.gt_ready:%d %H:%M}"
            else:
                pe = prev_end.get(r.press)
                co = float(r.co_min or 0)
                if pe is not None and pe > max(r.gt_ready, r.planned_start):
                    reason = "PRESS_QUEUE"
                    detail = f"GT ready {r.gt_ready:%d %H:%M}, press busy to {pe:%d %H:%M}"
                elif co > 0:
                    reason = "MOULD_CHANGE"
                    detail = f"{co:.0f} min"
                else:
                    reason = "PLANNED_LATE"
                    detail = f"planned {r.planned_start:%d %H:%M}"
            if pd.notna(r.feasible_end):
                prev_end[r.press] = r.feasible_end
            LOT_ROWS.append({"line": line.upper(), "press": r.press, "sku": r.sku, "qty": r.qty,
                             "planned_start": r.planned_start, "gt_ready": r.gt_ready,
                             "feasible_start": r.feasible_start, "feasible_end": r.feasible_end,
                             "push_h": round(float(r.push_h or 0), 1) if pd.notna(r.push_h) else None,
                             "co_min": r.co_min, "sku_state": r.sku_state,
                             "reason": reason, "detail": detail})

        # ---- per SKU balance at the horizon end ----------------------------
        L = pd.DataFrame([x for x in LOT_ROWS if x["line"] == line.upper()])
        for sku, s in sup.items():
            ev = s["events"]
            stock = sum(q for t, q, k in ev if k == "stock")
            built_rdy = sum(q for t, q, k in ev if k == "built" and t <= hz)
            built_late = sum(q for t, q, k in ev if k == "built" and t > hz)
            b_all = float(blots.loc[blots["sku"] == sku, "qty"].sum())
            l = L[L["sku"] == sku]
            plan = float(l["qty"].sum())
            loaded = float(l.loc[l["feasible_start"].notna() & (l["feasible_start"] <= hz), "qty"].sum())
            cured = float(l.loc[l["reason"] == "CURED", "qty"].sum())
            rack = stock + built_rdy - loaded
            not_loaded = plan - loaded
            # why the rack is not empty
            if rack <= 0.5:
                why = ""
            elif plan == 0:
                why = "NO CURING PLAN for this SKU"
            elif not_loaded <= 0.5:
                why = "BUILT MORE THAN THE CURING PLAN (no press slot left)"
            else:
                rs = l[(l["reason"] != "CURED") & (l["reason"] != "STARTED_NOT_FINISHED")]["reason"]
                why = "next press lot waiting: " + ", ".join(
                    f"{k} {v}" for k, v in rs.value_counts().items())
            SKU_ROWS.append({"line": line.upper(), "sku": sku, "gt": s["gt"],
                             "state": "BLOCKED" if sku in blocked else "",
                             "build_plan": b_all, "gt_stock_usable": round(stock, 0),
                             "built_ready_in_hz": built_rdy, "built_after_hz": built_late,
                             "cure_plan": plan, "loaded_in_hz": loaded, "cured_in_hz": cured,
                             "started_not_finished": loaded - cured,
                             "gt_on_rack_at_hz": round(rack, 0),
                             "cure_plan_not_loaded": not_loaded,
                             "why_gt_left_on_rack": why})

        # ---- GT on the rack per hour, and press idle while GT waits -------
        tgrid = pd.date_range(t0, hz, freq="1h")
        rack_by_sku = {}
        for sku, s in sup.items():
            arr = np.zeros(len(tgrid))
            for t, q, k in s["events"]:
                arr[tgrid >= t] += q
            l = L[(L["sku"] == sku) & L["feasible_start"].notna()]
            for t, q in zip(l["feasible_start"], l["qty"]):
                arr[tgrid >= t] -= q
            rack_by_sku[sku] = arr
        R_ = pd.DataFrame(rack_by_sku, index=tgrid)
        HOUR_ROWS.append(pd.DataFrame({"line": line.upper(), "time": tgrid,
                                       "gt_on_rack": R_.clip(lower=0).sum(axis=1).round(0),
                                       "skus_with_gt_waiting": (R_ > 0.5).sum(axis=1)}))
        # press idle hours with ready GT of an SKU the press is planned to cure LATER
        for press, g in L.groupby("press"):
            g = g.sort_values("planned_start")
            run = [(s_, e_) for s_, e_ in zip(g["feasible_start"], g["feasible_end"]) if pd.notna(s_)]
            skus = list(dict.fromkeys(g["sku"]))
            for h, t in enumerate(tgrid[:-1]):
                busy = any(s_ <= t < e_ for s_, e_ in run)
                if busy:
                    continue
                # next lot on this press not yet started at t
                nxt = g[(g["feasible_start"].isna()) | (g["feasible_start"] > t)]
                if nxt.empty:
                    continue
                head = nxt.iloc[0]
                head_ready = rack_by_sku.get(head["sku"], np.zeros(len(tgrid)))[h] >= head["qty"]
                other = [k for k in nxt["sku"].unique()[1:] if k != head["sku"]
                         and rack_by_sku.get(k, np.zeros(len(tgrid)))[h] > 0.5]
                cause = ("WAIT_PLANNED_START" if head_ready else
                         ("HEAD_OF_LINE: GT of a later SKU waits" if other else "WAIT_GT"))
                IDLE_ROWS.append({"line": line.upper(), "press": press, "time": t,
                                  "head_sku": head["sku"], "head_qty": head["qty"],
                                  "head_gt_on_rack": round(rack_by_sku.get(head["sku"], np.zeros(len(tgrid)))[h], 0),
                                  "later_sku_with_gt": ",".join(other[:3]), "cause": cause})
        # day view
        bd = blots[blots["feasible_end"] <= hz].assign(day=lambda x: io.plant_days(x["feasible_end"]) if hasattr(io, "plant_days") else x["feasible_end"].map(lambda t: io.plant_day(t).date()))
        cd = L[L["reason"] == "CURED"].assign(day=lambda x: x["feasible_end"].map(lambda t: io.plant_day(t).date()))
        ld = L[L["feasible_start"].notna() & (L["feasible_start"] <= hz)].assign(day=lambda x: x["feasible_start"].map(lambda t: io.plant_day(t).date()))
        dd = pd.DataFrame({"built": bd.groupby("day")["qty"].sum(),
                           "loaded_in_press": ld.groupby("day")["qty"].sum(),
                           "cured": cd.groupby("day")["qty"].sum()}).fillna(0)
        dd["line"] = line.upper()
        DAY_ROWS.append(dd.reset_index().rename(columns={"index": "day"}))

    S, Lt = pd.DataFrame(SKU_ROWS), pd.DataFrame(LOT_ROWS)
    H, I = pd.concat(HOUR_ROWS), pd.DataFrame(IDLE_ROWS)
    DY = pd.concat(DAY_ROWS)
    out = CM.analysis_dir(D) / f"BUILT_NOT_CURED_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for nm, df in (("By SKU", S.sort_values(["line", "gt_on_rack_at_hz"], ascending=[True, False])),
                       ("Press lots", Lt), ("Days", DY), ("GT rack by hour", H),
                       ("Press idle with GT", I)):
            df.to_excel(xw, sheet_name=nm, index=False)
            xw.sheets[nm].freeze_panes = "A2"
    for nm, df in (("sku", S), ("lots", Lt), ("idle", I), ("days", DY), ("rack", H)):
        df.to_csv(out.with_name(f"BUILT_NOT_CURED_{a.run}.{nm}.csv"), index=False)
    pd.set_option("display.width", 300)
    print(S.groupby("line")[["build_plan", "gt_stock_usable", "built_ready_in_hz", "built_after_hz",
                             "cure_plan", "loaded_in_hz", "cured_in_hz", "started_not_finished",
                             "gt_on_rack_at_hz", "cure_plan_not_loaded"]].sum())
    print(Lt.groupby(["line", "reason"])["qty"].agg(["count", "sum"]))
    if len(I):
        print(I.groupby(["line", "cause"]).size())
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
