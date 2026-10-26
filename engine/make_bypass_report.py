#!/usr/bin/env python3
"""make_bypass_report.py - the bypassed cutters / slitters, from a run's own outputs.

    python make_bypass_report.py --run e2e_v46a

Two questions, both lines:

  1. CUT / SLIT STOCK -> CHILD MATERIAL (wip.bypass_stock_to_children, v46a)
     which opening-stock batches of a cut/slit code were turned into which child
     item, at what BOM ratio, and how much of that child stock the schedule used.
     Source: phase2_bypass_stock_conversion.csv + phase2_wip_allocations.csv
     (converted batches carry "<CUT:<code>" in their batch_id).

  2. TRANSFER TIME ACROSS THE BYPASS = MINIMUM AGEING (bypass_transfer_from_min_aging)
     for every item made just below a bypassed code: the min ageing of the
     bypassed code that is added to its own, and a check on the schedule that
     every lot of such an item finished at least (own + transfer) hours before
     the trolley it feeds started building; and that every converted stock draw
     was usable (produced + cut code's min ageing) before its trolley started.
     Source: phase2_bypass_transfer.csv, phase5_schedule.csv, phase3_lot_demand.csv,
     phase5_building_plan.csv.

Output: outputs/<run>/BYPASS_REPORT_<run>.xlsx
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

import ctpio as io
import rules as RULES


def _read(p, **k):
    try:
        return pd.read_csv(p, **k)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    conv_all, use_all, tr_all, lotchk_all, stkchk_all, summ = [], [], [], [], [], []
    for ln in ("pcr", "tbr"):
        O = D / ln
        conv = _read(O / "phase2_bypass_stock_conversion.csv", parse_dates=["produced_time"])
        al = _read(O / "phase2_wip_allocations.csv", parse_dates=["usable_from", "needed_by"])
        tr = _read(O / "phase2_bypass_transfer.csv")
        bp = _read(O / "phase5_building_plan.csv", parse_dates=["feasible_start", "build_start"])
        S = _read(O / "phase5_schedule.csv", parse_dates=["start", "finish"])
        ld = _read(O / "phase3_lot_demand.csv")
        start = dict(zip(bp.get("building_lot_id", []), bp.get("feasible_start", [])))

        # ---- 1. conversion and use ------------------------------------------
        cv = conv[conv.get("status", pd.Series(dtype=str)) == "CONVERTED"] if len(conv) else conv
        if len(al):
            al["cut_code"] = al["batch_id"].astype(str).str.extract(r"<CUT:(.+)$")[0]
            cu = al[al["cut_code"].notna()].copy()
        else:
            cu = pd.DataFrame(columns=["cut_code", "item_code", "qty_used", "building_lot_id",
                                       "usable_from"])
        if len(cv):
            u = (cv.groupby(["cut_code", "department", "child", "child_unit"])
                   .agg(cut_batches=("production_id", "nunique"), cut_qty=("cut_qty", "sum"),
                        ratio=("ratio", "median"), child_qty=("child_qty", "sum"),
                        transfer_min_ageing_h=("transfer_min_ageing_h", "max"))
                   .reset_index())
            used = cu.groupby(["cut_code", "item_code"])["qty_used"].sum()
            u["child_qty_used"] = [float(used.get((c, k), 0.0)) for c, k in zip(u["cut_code"], u["child"])]
            u["child_qty_left"] = (u["child_qty"] - u["child_qty_used"]).clip(lower=0)
            u["used_pct"] = (100 * u["child_qty_used"] / u["child_qty"].where(u["child_qty"] > 0)).round(1)
            u.insert(0, "line", ln.upper())
            use_all.append(u)
            conv_all.append(conv.assign(line=ln.upper()))
        elif len(conv):
            conv_all.append(conv.assign(line=ln.upper()))

        # converted stock: usable before its trolley started?
        if len(cu):
            cu["trolley_start"] = cu["building_lot_id"].map(start)
            cu["slack_h"] = ((cu["trolley_start"] - cu["usable_from"]).dt.total_seconds() / 3600).round(2)
            cu["ok"] = cu["slack_h"] >= -1e-6
            cu.insert(0, "line", ln.upper())
            stkchk_all.append(cu[["line", "building_lot_id", "sku", "item_code", "cut_code", "batch_id",
                                  "produced_time", "usable_from", "trolley_start", "slack_h", "ok",
                                  "qty_used"]])

        # ---- 2. transfer time ------------------------------------------------
        if len(tr):
            tr_all.append(tr)
            eff = tr.drop_duplicates("child_item").set_index("child_item")
            items = set(eff.index[eff["transfer_added_h"] > 0])
            if len(S) and len(ld) and items:
                fin = dict(zip(S["lot_id"], S["finish"]))
                sta = dict(zip(S["lot_id"], S["start"]))
                mc = dict(zip(S["lot_id"], S["machine"].astype(str)))
                x = ld[ld["item_code"].isin(items)].copy()
                x["lot_finish"] = x["lot_id"].map(fin)
                # campaign.progressive_release: on campaign machines each trolley's
                # share is released as the run produces it (phase5 _progressive),
                # so the rest is measured from that share, not the lot's end
                prog = str(RULES.rule("campaign.progressive_release", False)).lower() in (
                    "1", "true", "on", "yes")
                camp = {str(m).strip() for m in (RULES.rule("campaign.machines", []) or [])}
                x["ready_basis"] = "lot finish"
                if prog and camp:
                    x["need_by"] = pd.to_datetime(x["need_by"], errors="coerce")
                    for lot, g in x[x["lot_id"].map(mc).isin(camp)].groupby("lot_id"):
                        allg = ld[ld["lot_id"] == lot].assign(
                            need_by=lambda d: pd.to_datetime(d["need_by"], errors="coerce"))
                        allg = allg.sort_values(["need_by", "building_lot_id"], kind="stable")
                        s0, f0 = sta.get(lot), fin.get(lot)
                        tot = float(allg["qty"].sum()) or 1.0
                        share = dict(zip(allg["building_lot_id"],
                                         [s0 + (f0 - s0) * min(1.0, c / tot)
                                          for c in allg["qty"].cumsum()]))
                        x.loc[g.index, "lot_finish"] = g["building_lot_id"].map(share)
                        x.loc[g.index, "ready_basis"] = "share of lot (progressive release)"
                x["machine"] = x["lot_id"].map(mc)
                x["trolley_start"] = x["building_lot_id"].map(start)
                x["own_min_ageing_h"] = x["item_code"].map(eff["child_own_min_ageing_h"])
                x["transfer_h"] = x["item_code"].map(eff["transfer_added_h"])
                x["required_gap_h"] = x["own_min_ageing_h"] + x["transfer_h"]
                x["actual_gap_h"] = ((x["trolley_start"] - x["lot_finish"]).dt.total_seconds() / 3600).round(2)
                x["ok"] = x["actual_gap_h"] + 1e-6 >= x["required_gap_h"]
                x = x[x["lot_finish"].notna() & x["trolley_start"].notna()]
                x.insert(0, "line", ln.upper())
                lotchk_all.append(x[["line", "item_code", "lot_id", "machine", "ready_basis", "lot_finish",
                                     "building_lot_id", "trolley_start", "own_min_ageing_h",
                                     "transfer_h", "required_gap_h", "actual_gap_h", "ok"]])

        lc = lotchk_all[-1] if lotchk_all and lotchk_all[-1]["line"].iat[0] == ln.upper() else pd.DataFrame()
        sc = stkchk_all[-1] if stkchk_all and stkchk_all[-1]["line"].iat[0] == ln.upper() else pd.DataFrame()
        kept = conv[conv["status"] != "CONVERTED"] if len(conv) else conv
        summ += [
            (ln.upper(), "cut/slit stock batches converted", int(cv["production_id"].nunique()) if len(cv) else 0),
            (ln.upper(), "cut/slit codes converted", int(cv["cut_code"].nunique()) if len(cv) else 0),
            (ln.upper(), "cut/slit stock (own units)",
             round(float(cv.drop_duplicates("production_id")["cut_qty"].sum()), 1) if len(cv) else 0),
            (ln.upper(), "child stock created", round(float(cv["child_qty"].sum()), 1) if len(cv) else 0),
            (ln.upper(), "child items receiving stock", int(cv["child"].nunique()) if len(cv) else 0),
            (ln.upper(), "child stock used by the schedule", round(float(cu["qty_used"].sum()), 1) if len(cu) else 0),
            (ln.upper(), "batches kept on the cut code (not converted)", int(len(kept))),
            (ln.upper(), "items below a bypassed cutter/slitter", int(tr["child_item"].nunique()) if len(tr) else 0),
            (ln.upper(), "  of which with a transfer time > 0",
             int(tr.drop_duplicates("child_item")["transfer_added_h"].gt(0).sum()) if len(tr) else 0),
            (ln.upper(), "transfer rule on (bypass_transfer_from_min_aging)",
             bool(tr["rule_on"].iat[0]) if len(tr) else None),
            (ln.upper(), "lot -> trolley links checked (transfer > 0)", int(len(lc))),
            (ln.upper(), "  links breaking own + transfer ageing", int((~lc["ok"]).sum()) if len(lc) else 0),
            (ln.upper(), "converted stock draws checked", int(len(sc))),
            (ln.upper(), "  draws used before usable (produced + cut min ageing)",
             int((~sc["ok"]).sum()) if len(sc) else 0),
        ]

    S = pd.DataFrame(summ, columns=["line", "metric", "value"])
    U = pd.concat(use_all, ignore_index=True) if use_all else pd.DataFrame()
    C = pd.concat(conv_all, ignore_index=True) if conv_all else pd.DataFrame()
    T = pd.concat(tr_all, ignore_index=True) if tr_all else pd.DataFrame()
    LC = pd.concat(lotchk_all, ignore_index=True) if lotchk_all else pd.DataFrame()
    SC = pd.concat(stkchk_all, ignore_index=True) if stkchk_all else pd.DataFrame()
    BY_CHILD = (U.groupby(["line", "child", "child_unit"])[["child_qty", "child_qty_used", "child_qty_left"]]
                  .sum().round(1).reset_index() if len(U) else pd.DataFrame())
    note = pd.DataFrame({"read me": [
        f"Run {a.run}. Cutters and slitters are bypassed (not scheduled).",
        "1) Stock already cut or slit is turned back into the BOM child it was cut from, when the "
        "opening stock is read: child qty = cut qty x child_quantity / Parent_qty (median over BOM "
        "rows; bias cutter: child_quantity). A cut belt that carries an edge strip gives both.",
        "Converted stock is usable at produced_time + the cut code's own minimum ageing "
        "(the transfer time across the bypass) and keeps the cut code's maximum ageing.",
        "2) Everything newly made below a bypassed code gets the bypassed code's minimum ageing added "
        "to its own (transfer time). 'Transfer check' tests every such lot against the trolley it "
        "feeds; 'Stock check' tests every converted stock draw against its trolley start.",
        "A transfer time of 0 means the bypassed code's minimum ageing is 0 in the ageing master "
        "and in the stock rows - the rule is applied, the data gives it nothing to add."]})
    out = D / f"BYPASS_REPORT_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Read me", note), ("Summary", S), ("Converted by cut code", U),
                         ("Converted by child", BY_CHILD), ("Conversion rows", C),
                         ("Transfer time", T), ("Transfer check", LC), ("Stock check", SC)):
            (df if len(df) else pd.DataFrame({"(none)": []})).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 60)
            ws.freeze_panes = "A2"
    pd.set_option("display.width", 200)
    print(S.to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
