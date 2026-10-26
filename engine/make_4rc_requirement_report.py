#!/usr/bin/env python3
"""make_4rc_requirement_report.py - day-wise calendered-roll requirement on the
4-roll calender (machine 901) per item, and how much of it was fulfilled.

    python make_4rc_requirement_report.py --run e2e_v45b [--machine 901]

For every item the routing puts on the machine (PCR and TBR):

  requirement   gross metres the building plan needs (phase1_demand.demand_qty),
                bucketed on the plant day (07:00-07:00) of its need-by time
  from stock    covered by opening stock - directly or through stock of a cut code
                made from it (gross - net)
  to make       net metres the calender has to produce (phase2_demand_net.net_qty)
  produced      metres of the item's 901 lots, on the plant day the lot FINISHES
  fulfilment    each net demand is matched to the lot that serves it
                (phase3_lot_demand) and judged by that lot's finish:
                  on time        lot finished at or before the need-by time
                  late           finished after need-by, inside the horizon
                  after horizon  finished after the plan's last build (or not placed)

Need-by times before the plan start (the first trolleys) are counted on day 1.

Output: outputs/<run>/4RC_REQUIREMENT_<run>.xlsx
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import ctpio as io


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--machine", default="901")
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    M = str(a.machine)
    req_rows, ful_rows, lot_rows = [], [], []
    horizon_all = []
    for ln in ("pcr", "tbr"):
        O = D / ln
        rt = io.load_routing(ln)
        items = {p for p, ms in zip(rt["routed_product"], rt["machines"].astype(str))
                 if p and M in [x.strip().strip('"') for x in ms.replace('"', "").split(",")]}
        bl = pd.read_csv(O / "phase5_building_plan.csv", parse_dates=["build_start", "build_end"])
        opening, horizon = bl["build_start"].min(), bl["build_end"].max()
        horizon_all.append(horizon)
        day0 = io.plant_day(opening).normalize() if hasattr(io.plant_day(opening), "normalize") \
            else pd.Timestamp(io.plant_day(opening))

        def pday(t):
            if pd.isna(t):
                return pd.NaT
            d = pd.Timestamp(io.plant_day(max(t, opening)))
            return d.normalize()

        g = pd.read_csv(O / "phase1_demand.csv", parse_dates=["need_by", "build_start"])
        g = g[g["item_code"].isin(items)]
        n = pd.read_csv(O / "phase2_demand_net.csv", parse_dates=["need_by"])
        n = n[n["item_code"].isin(items)]
        net_map = n.groupby(["building_lot_id", "item_code"])["net_qty"].sum().to_dict()
        g["net_qty"] = [net_map.get(k, 0.0) for k in zip(g["building_lot_id"], g["item_code"])]
        g["from_stock"] = (g["demand_qty"] - g["net_qty"]).clip(lower=0)
        g["day"] = g["need_by"].map(pday)
        g["line"] = ln.upper()
        req_rows.append(g)

        s = pd.read_csv(O / "phase5_schedule.csv", parse_dates=["start", "finish", "LFT"])
        s["machine"] = s["machine"].astype(str)
        s = s[s["item_code"].isin(items)].copy()
        s["day"] = s["finish"].map(pday)
        s["line"] = ln.upper()
        lot_rows.append(s)
        fin = dict(zip(s["lot_id"], s["finish"]))
        onm = dict(zip(s["lot_id"], s["machine"]))

        ld = pd.read_csv(O / "phase3_lot_demand.csv", parse_dates=["need_by"])
        ld = ld[ld["item_code"].isin(items)].copy()
        ld["finish"] = ld["lot_id"].map(fin)
        ld["machine"] = ld["lot_id"].map(onm)
        ld["day"] = ld["need_by"].map(pday)
        ld["status"] = [
            "after horizon" if (pd.isna(f) or f > horizon) else
            ("on time" if f <= nb else "late") for f, nb in zip(ld["finish"], ld["need_by"])]
        ld["late_h"] = ((ld["finish"] - ld["need_by"]).dt.total_seconds() / 3600).clip(lower=0)
        ld["line"] = ln.upper()
        ful_rows.append(ld)

    G = pd.concat(req_rows, ignore_index=True)
    S = pd.concat(lot_rows, ignore_index=True)
    F = pd.concat(ful_rows, ignore_index=True)

    # ---- day x item table ---------------------------------------------------
    k = ["line", "item_code", "day"]
    req = G.groupby(k).agg(requirement_m=("demand_qty", "sum"), from_stock_m=("from_stock", "sum"),
                           to_make_m=("net_qty", "sum")).reset_index()
    prod = (S[S["machine"] == M].groupby(k).agg(produced_on_4rc_m=("lot_qty", "sum"),
                                                 lots=("lot_id", "size")).reset_index())
    ful = F.pivot_table(index=k, columns="status", values="qty", aggfunc="sum", fill_value=0).reset_index()
    for c in ("on time", "late", "after horizon"):
        if c not in ful.columns:
            ful[c] = 0.0
    ful = ful.rename(columns={"on time": "made_on_time_m", "late": "made_late_m",
                              "after horizon": "made_after_horizon_m"})
    T = req.merge(prod, on=k, how="outer").merge(ful, on=k, how="outer").fillna(0)
    T = T.sort_values(k).reset_index(drop=True)
    T["covered_on_time_m"] = T["from_stock_m"] + T["made_on_time_m"]
    T["fulfilled_on_time_pct"] = (100 * T["covered_on_time_m"] /
                                  T["requirement_m"].replace(0, float("nan"))).astype(float).round(1)
    T["cum_to_make_m"] = T.groupby(["line", "item_code"])["to_make_m"].cumsum()
    T["cum_produced_m"] = T.groupby(["line", "item_code"])["produced_on_4rc_m"].cumsum()
    T["cum_balance_m"] = T["cum_produced_m"] - T["cum_to_make_m"]
    T["day"] = pd.to_datetime(T["day"]).dt.date

    # ---- item summary -------------------------------------------------------
    I = T.groupby(["line", "item_code"]).agg(
        requirement_m=("requirement_m", "sum"), from_stock_m=("from_stock_m", "sum"),
        to_make_m=("to_make_m", "sum"), produced_on_4rc_m=("produced_on_4rc_m", "sum"),
        lots=("lots", "sum"), made_on_time_m=("made_on_time_m", "sum"),
        made_late_m=("made_late_m", "sum"), made_after_horizon_m=("made_after_horizon_m", "sum")
    ).reset_index()
    I["fulfilled_on_time_pct"] = (100 * (I["from_stock_m"] + I["made_on_time_m"]) /
                                  I["requirement_m"].replace(0, float("nan"))).astype(float).round(1)
    I["fulfilled_in_horizon_pct"] = (100 * (I["from_stock_m"] + I["made_on_time_m"] + I["made_late_m"]) /
                                     I["requirement_m"].replace(0, float("nan"))).astype(float).round(1)
    late_h = F[F["status"] != "on time"].groupby(["line", "item_code"])["late_h"].median()
    I["median_late_h"] = [round(float(late_h.get(x, 0) or 0), 1) for x in zip(I["line"], I["item_code"])]
    I = I.round(1)

    # ---- day totals ---------------------------------------------------------
    DT = T.groupby(["line", "day"])[["requirement_m", "from_stock_m", "to_make_m", "produced_on_4rc_m",
                                     "made_on_time_m", "made_late_m", "made_after_horizon_m"]].sum().reset_index()
    DT["fulfilled_on_time_pct"] = (100 * (DT["from_stock_m"] + DT["made_on_time_m"]) /
                                   DT["requirement_m"].replace(0, float("nan"))).astype(float).round(1)
    DT = DT.round(1)

    # ---- matrices (item x day) ---------------------------------------------
    def mat(col):
        x = T.pivot_table(index=["line", "item_code"], columns="day", values=col,
                          aggfunc="sum", fill_value=0)
        x.columns = [str(c) for c in x.columns]
        x["TOTAL"] = x.sum(axis=1)
        return x.round(0).reset_index()

    lots = S[S["machine"] == M][["line", "lot_id", "item_code", "lot_qty", "uom", "start", "finish",
                                 "duration_h", "changeover_min", "LFT", "late_h"]].sort_values("start")
    note = pd.DataFrame({"read me": [
        f"Run {a.run}. Machine {M} = 4-roll calender (4RC), shared by PCR and TBR. Units: metres.",
        "Day = plant day 07:00-07:00. Requirement is bucketed on its need-by time "
        "(needs before the plan start are counted on day 1); produced metres on the day the lot finishes.",
        "requirement = gross metres the building plan needs; from_stock = covered by opening stock "
        "(directly or via stock of a cut code made from it); to_make = net metres 901 must produce.",
        "made_on_time / made_late / made_after_horizon: each net demand judged by the finish of the "
        "lot that serves it (phase3_lot_demand) against its need-by time.",
        "fulfilled_on_time_pct = (from_stock + made_on_time) / requirement.",
        "cum_balance = cumulative produced - cumulative to-make (negative = behind)."]})
    out = D / f"4RC_REQUIREMENT_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Read me", note), ("Item summary", I), ("Day totals", DT),
                         ("Day x item", T), ("Requirement matrix", mat("requirement_m")),
                         ("To-make matrix", mat("to_make_m")),
                         ("Produced matrix", mat("produced_on_4rc_m")),
                         ("On-time matrix", mat("made_on_time_m")), ("4RC lots", lots)):
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 40)
            ws.freeze_panes = "C2" if name.endswith("matrix") else "A2"
    pd.set_option("display.width", 250)
    print(I.to_string(index=False))
    print()
    print(DT.to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
