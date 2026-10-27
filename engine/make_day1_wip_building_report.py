#!/usr/bin/env python3
"""make_day1_wip_building_report.py - day 1: is stock on the rack, and does it build?

    python make_day1_wip_building_report.py --run e2e_v46k

For every trolley the plan puts on day 1 (first plant day, 07:00 -> 07:00):

  Trolleys    planned vs actual start, tyres built on day 1, how much of its kit
              came from opening stock, its last part and WHY it started late:
                ON TIME
                KIT: last part made fresh  - a part had to be made; its lot
                                             finished after the planned start
                KIT: last part from stock  - stock had to rest (min ageing)
                BUILDER BUSY               - kit ready, the builder was still
                                             on its previous (late) trolley
  Parts       each part of each day-1 trolley: needed, from stock, made fresh,
              and why stock did not cover the rest:
                no stock in the feed / all stock expired before 07:00 /
                stock used up by earlier needs / stock not rested in time /
                stock expires before the need
  Last parts  the upstream lots that finished last, their machine and timing

Needs WIP_PRESENCE_VS_CONSUMPTION_<run>.xlsx (made first if missing).
Writes outputs/<run>/DAY1_WIP_VS_BUILDING_<run>.xlsx
"""
from __future__ import annotations

import argparse
import subprocess
import sys

import pandas as pd

import ctpio as io
import rules as R

EPS_H = 0.05


def tyres_on(r, t0, t1) -> float:
    """Tyres of one trolley finished inside [t0, t1): even rate after setup."""
    st = r.feasible_start + pd.Timedelta(minutes=float(r.setup_applied_min or 0))
    en = r.feasible_end
    if pd.isna(st) or pd.isna(en):
        return 0.0
    st = min(st, en)
    if en <= st:
        return float(r.qty) if t0 <= en < t1 else 0.0
    a, b = max(st, t0), min(en, t1)
    return float(r.qty) * max(0.0, (b - a).total_seconds()) / (en - st).total_seconds()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    D = io.OUT_ROOT / a.run
    wp = D / f"WIP_PRESENCE_VS_CONSUMPTION_{a.run}.xlsx"
    if not wp.exists():
        subprocess.run([sys.executable, str(io.ROOT / "engine" / "make_wip_presence_report.py"),
                        "--run", a.run], check=True)
    BB = pd.read_excel(wp, sheet_name="By batch")
    BB["produced_time"] = pd.to_datetime(BB["produced_time"])

    trol, parts, last, fresh_rows = [], [], [], []
    camp = {str(x).strip() for x in (R.rule("campaign.machines", []) or [])}
    prog = str(R.rule("campaign.progressive_release", False)).lower() in ("1", "true", "on", "yes")
    tr = pd.Timedelta(minutes=io.TRANSFER_MIN)
    for ln in io.scheduled_lines(a.run):
        O = D / ln
        b = pd.read_csv(O / "phase5_building_plan.csv",
                        parse_dates=["build_start", "build_end", "feasible_start",
                                     "feasible_end", "kit_ready"])
        t0 = b["build_start"].min().normalize() + pd.Timedelta(hours=io.SHIFT_START_H)
        t1 = t0 + pd.Timedelta(days=1)
        net = pd.read_csv(O / "phase2_demand_net.csv", parse_dates=["need_by", "build_start"])
        dem = pd.read_csv(O / "phase1_demand.csv", parse_dates=["need_by", "build_start"])
        alloc = pd.read_csv(O / "phase2_wip_allocations.csv", parse_dates=["needed_by", "usable_from"])
        # the KIT: the green tyre's direct BOM children (BOM v2 has no cutter step)
        bom = io.load_bom(ln)
        kit_of = {(sp, par): set() for sp, par in zip(bom["Super_parent"], bom["Parent"])}
        for sp, par, ch in zip(bom["Super_parent"], bom["Parent"], bom["child"]):
            kit_of[(sp, par)].add(ch)
        fresh_q = net.groupby(["building_lot_id", "item_code"])["net_qty"].sum()
        stock_q = alloc.groupby(["building_lot_id", "item_code"])["qty_used"].sum()
        sch = pd.read_csv(O / "phase5_schedule.csv", parse_dates=["start", "finish", "LFT"])
        sch["key"] = ln + "::" + sch["lot_id"].astype(str)
        sch_by = sch.set_index("key")
        bb = BB[BB["line"] == ln.upper()].copy()
        bb["ready"] = bb["produced_time"] + pd.to_timedelta(bb["min_ageing_h"].fillna(0), unit="h")
        bb["engine_expiry"] = t0 + pd.to_timedelta(bb["max_ageing_h"].fillna(1e6), unit="h")
        bb.loc[bb["max_ageing_h"].fillna(0) <= 0, "engine_expiry"] = pd.Timestamp.max

        bstart = dict(zip(b["building_lot_id"], b["build_start"]))
        # ---- fresh parts: which lot makes each trolley's share, and when it is ready
        ld = pd.read_csv(O / "phase3_lot_demand.csv", parse_dates=["need_by"])
        ld["key"] = ln + "::" + ld["lot_id"].astype(str)
        ld = ld.merge(sch[["key", "machine", "machine_name", "start", "finish", "item_type"]]
                      if "machine_name" in sch.columns else sch[["key", "machine", "start", "finish", "item_type"]],
                      on="key", how="left")
        rel = {}
        if prog:                       # 901 releases rolls as it runs (campaign.progressive_release)
            for k, g in ld[ld["machine"].astype(str).isin(camp)].groupby("key", sort=False):
                g = g.sort_values(["need_by", "building_lot_id"], kind="stable")
                tot = float(g["qty"].sum()) or 1.0
                cum = 0.0
                s0, f0 = g["start"].iloc[0], g["finish"].iloc[0]
                for bl_, q in zip(g["building_lot_id"], g["qty"]):
                    cum += float(q)
                    t = s0 + (f0 - s0) * min(1.0, cum / tot)
                    rel[(k, bl_)] = max(rel.get((k, bl_), t), t)
        mage = dem.groupby("item_code")["min_aging_h"].max().to_dict()
        ld["release"] = [rel.get((k, b_), f) for k, b_, f in zip(ld["key"], ld["building_lot_id"], ld["finish"])]
        ld["ready"] = ld["release"] + pd.to_timedelta(ld["item_code"].map(mage).fillna(0), unit="h") + tr
        # builder order: previous trolley on the same builder
        b = b.sort_values(["machine", "feasible_start"])
        b["prev_end"] = b.groupby("machine")["feasible_end"].shift(1)
        b["prev_lot"] = b.groupby("machine")["building_lot_id"].shift(1)
        d1 = b[(b["build_start"] >= t0) & (b["build_start"] < t1)]

        for r in d1.itertuples(index=False):
            kit = kit_of.get((r.sku, r.built_item), set())
            dd = dem[(dem["building_lot_id"] == r.building_lot_id) & dem["item_code"].isin(kit)]
            n = dd.groupby("item_code").agg(item_type=("item_type", "first"), uom=("uom", "first"),
                                            required_qty=("demand_qty", "sum"),
                                            need_by=("need_by", "min")).reset_index()
            n["wip_used"] = [float(stock_q.get((r.building_lot_id, i), 0.0)) for i in n["item_code"]]
            n["net_qty"] = [float(fresh_q.get((r.building_lot_id, i), 0.0)) for i in n["item_code"]]
            deeper = net[(net["building_lot_id"] == r.building_lot_id) & ~net["item_code"].isin(kit)]
            need_q = float(n["required_qty"].sum())
            delay = (r.feasible_start - r.build_start).total_seconds() / 3600
            kit_late = ((r.kit_ready - r.build_start).total_seconds() / 3600
                        if pd.notna(r.kit_ready) else None)
            setup = pd.Timedelta(minutes=float(r.setup_applied_min or 0))
            bl = sch_by.loc[r.blocking_lot] if (isinstance(r.blocking_lot, str)
                                                and r.blocking_lot in sch_by.index) else None
            if delay <= EPS_H:
                why = "ON TIME"
            elif pd.notna(r.kit_ready) and r.kit_ready >= r.feasible_start - pd.Timedelta(hours=EPS_H):
                why = ("KIT: last part made fresh" if bl is not None
                       else "KIT: last part from stock (rest time)")
            elif pd.notna(r.prev_end) and r.prev_end + setup >= r.feasible_start - pd.Timedelta(hours=EPS_H):
                why = "BUILDER BUSY (previous trolley late)"
            else:
                why = "OTHER (shift / sequence)"
            fresh = n[n["net_qty"] > 1e-6]
            trol.append({
                "line": ln.upper(), "trolley": r.building_lot_id, "builder": r.machine,
                "sku": r.sku, "green_tyre": r.built_item, "tyres": r.qty,
                "planned_start": r.build_start, "actual_start": r.feasible_start,
                "late_h": round(delay, 2),
                "tyres_built_day1": round(tyres_on(r, t0, t1)),
                "kit_parts": int(n["item_code"].nunique()),
                "kit_parts_from_stock": int((n["net_qty"] <= 1e-6).sum()),
                "kit_parts_made_fresh": int(fresh["item_code"].nunique()),
                "kit_from_stock_pct": (round(100 * float((n["required_qty"] - n["net_qty"]).clip(lower=0).sum())
                                             / need_q, 1) if need_q else None),
                "deeper_items_made_fresh": int(deeper["item_code"].nunique()),
                "fresh_parts": ", ".join(sorted(fresh["item_code"].unique())),
                "kit_ready": r.kit_ready, "last_part": r.blocking_item,
                "last_part_machine": (bl["machine"] if bl is not None else ""),
                "last_part_lot_start": (bl["start"] if bl is not None else pd.NaT),
                "last_part_lot_finish": (bl["finish"] if bl is not None else pd.NaT),
                "previous_trolley": r.prev_lot, "previous_trolley_end": r.prev_end,
                "why": why})

            # ---- fresh parts of this trolley, lot by lot ------------------------
            fl = ld[(ld["building_lot_id"] == r.building_lot_id) & ld["item_code"].isin(kit)]
            for it, g in fl.groupby("item_code"):
                pr = g["ready"].max()
                nn = n[n["item_code"] == it]
                fresh_rows.append({
                    "line": ln.upper(), "trolley": r.building_lot_id, "builder": r.machine,
                    "sku": r.sku, "tyres": r.qty, "planned_start": r.build_start,
                    "actual_start": r.feasible_start,
                    "part": it, "item_type": g["item_type"].iloc[0],
                    "needed": round(float(nn["required_qty"].sum()), 2),
                    "from_stock": round(float(nn["wip_used"].sum()), 2),
                    "made_fresh": round(float(g["qty"].sum()), 2),
                    "lots": ", ".join(g["lot_id"].astype(str).unique()),
                    "machine": ", ".join(g["machine"].astype(str).unique()),
                    "lot_start": g["start"].min(), "lot_finish": g["finish"].max(),
                    "trolley_share_released": g["release"].max(),
                    "rest_h": float(mage.get(it, 0) or 0),
                    "part_ready": pr,
                    "makes_trolley_wait_h": round(max(0.0, (pr - r.build_start).total_seconds() / 3600), 2),
                    "is_last_part": it == r.blocking_item,
                    "part_ready_minus_kit_ready_min": (round((pr - r.kit_ready).total_seconds() / 60, 1)
                                                       if pd.notna(r.kit_ready) else None)})

            # ---- parts: why stock did not cover -------------------------------
            for g in n.itertuples(index=False):
                it = g.item_code
                req, used, fr = float(g.required_qty), float(g.wip_used), float(g.net_qty)
                need = g.need_by
                ib = bb[bb["item"] == it]
                live = ib[ib["status"] != "DEAD AT OPEN"]
                if fr <= 1e-6:
                    reason = "covered by stock"
                elif ib.empty:
                    reason = "no stock of this item in the feed"
                elif live.empty:
                    reason = "all stock expired before 07:00 (dead at open)"
                else:
                    left = live[live["left_qty"] > (live["present_qty"] * 1e-3).clip(lower=0.01)]
                    early = alloc[(alloc["item_code"] == it) & (alloc["needed_by"] <= need)
                                  & (alloc["building_lot_id"] != r.building_lot_id)]
                    # stock drawn by a LATER trolley although it had rested before
                    # THIS trolley's start: refused here only because the stock must
                    # be ready by build_start - buffer hours (phase 1 need_by)
                    held = alloc[(alloc["item_code"] == it)
                                 & (alloc["building_lot_id"].map(bstart) > r.build_start)
                                 & (alloc["usable_from"] <= r.build_start)]
                    if len(held):
                        reason = ("stock ready before the trolley start, but not by start - "
                                  f"buffer ({float(dem.loc[dem['item_code'] == it, 'buffer_h'].max()):g} h)"
                                  " - given to later trolleys")
                    elif left.empty:
                        reason = ("stock not enough: this trolley took what was left, rest made fresh"
                                  if used > 1e-6 else
                                  "stock not enough: used up by earlier trolleys first"
                                  if len(early) else
                                  "stock not enough: used up by later-need trolleys (netting order)")
                    elif (left["ready"] > need).all():
                        reason = "stock left but not rested in time (min ageing)"
                    elif (left["engine_expiry"] < need).all():
                        reason = "stock left but expires before the need"
                    else:
                        reason = "stock left; FEFO gave it to a later need"
                parts.append({
                    "line": ln.upper(), "trolley": r.building_lot_id, "builder": r.machine,
                    "item": it, "item_type": g.item_type, "uom": g.uom,
                    "needed": round(req, 3), "from_stock": round(used, 3),
                    "made_fresh": round(fr, 3), "need_by": need,
                    "stock_present": round(float(ib["present_qty"].sum()), 3),
                    "stock_dead_at_open": round(float(ib.loc[ib["status"] == "DEAD AT OPEN",
                                                             "present_qty"].sum()), 3),
                    "stock_usable": round(float(live["present_qty"].sum()), 3),
                    "stock_left_unused": round(float(live["left_qty"].sum()), 3),
                    "is_last_part": it == r.blocking_item,
                    "why_not_from_stock": reason})

        # last-part upstream lots
        for k in d1["blocking_lot"].dropna().unique():
            if k in sch_by.index:
                x = sch_by.loc[k]
                last.append({"line": ln.upper(), "lot": x["lot_id"], "item": x["item_code"],
                             "department": x["department"], "machine": x["machine"],
                             "machine_name": x.get("machine_name", ""), "qty": x["lot_qty"],
                             "start": x["start"], "finish": x["finish"],
                             "trolleys_waiting": int((d1["blocking_lot"] == k).sum())})

    T, P, L = pd.DataFrame(trol), pd.DataFrame(parts), pd.DataFrame(last)
    F = pd.DataFrame(fresh_rows)
    if len(F):
        F = F.sort_values(["line", "planned_start", "trolley", "part_ready"],
                          ascending=[True, True, True, False])
        # per trolley: its fresh parts in one line, longest wait first
        FT = (F.groupby(["line", "trolley", "builder", "tyres", "planned_start", "actual_start"], sort=False)
              .apply(lambda g: pd.Series({
                  "fresh_parts": len(g),
                  "fresh_parts_by_wait": "; ".join(f"{p} ({m}) {w:.1f} h" for p, m, w in
                                                  zip(g["part"], g["machine"], g["makes_trolley_wait_h"])),
                  "longest_wait_h": g["makes_trolley_wait_h"].max(),
                  "last_part": g.loc[g["makes_trolley_wait_h"].idxmax(), "part"]}),
                     include_groups=False).reset_index())
        FT["actual_late_h"] = ((FT["actual_start"] - FT["planned_start"]).dt.total_seconds() / 3600).round(2)
        FT["extra_wait_after_kit_h"] = (FT["actual_late_h"] - FT["longest_wait_h"]).clip(lower=0).round(2)
    else:
        FT = pd.DataFrame()
    summ = []
    for ln, g in T.groupby("line"):
        pg = P[P["line"] == ln]
        summ.append({"line": ln, "day1_trolleys": len(g), "planned_tyres": g["tyres"].sum(),
                     "tyres_built_day1": g["tyres_built_day1"].sum(),
                     "trolleys_on_time": int((g["why"] == "ON TIME").sum()),
                     "trolleys_kit_all_from_stock": int((g["kit_parts_made_fresh"] == 0).sum()),
                     "trolleys_kit_all_from_stock_on_time": int(((g["kit_parts_made_fresh"] == 0)
                                                                  & (g["why"] == "ON TIME")).sum()),
                     "median_kit_from_stock_pct": g["kit_from_stock_pct"].median(),
                     "late_last_part_fresh": int((g["why"] == "KIT: last part made fresh").sum()),
                     "late_last_part_stock_rest": int(g["why"].str.startswith("KIT: last part from stock").sum()),
                     "late_builder_busy": int(g["why"].str.startswith("BUILDER BUSY").sum()),
                     "late_other": int(g["why"].str.startswith("OTHER").sum()),
                     "kit_part_lines": len(pg),
                     "kit_part_lines_from_stock": int((pg["made_fresh"] <= 1e-6).sum()),
                     "kit_part_lines_made_fresh": int((pg["made_fresh"] > 1e-6).sum())})
    S = pd.DataFrame(summ)
    W = (P[P["made_fresh"] > 1e-6].groupby(["line", "why_not_from_stock"])
         .agg(part_lines=("item", "size"), items=("item", "nunique"),
              trolleys=("trolley", "nunique"), last_parts=("is_last_part", "sum"))
         .reset_index().sort_values(["line", "part_lines"], ascending=[True, False]))
    out = D / f"DAY1_WIP_VS_BUILDING_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in (("Summary", S), ("Fresh parts by trolley", FT),
                         ("Fresh parts detail", F), ("Why not from stock", W), ("Trolleys", T),
                         ("Parts", P), ("Last-part lots", L)):
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 45)
            ws.freeze_panes = "A2"
    print(S.to_string(index=False))
    print(W.to_string(index=False))
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
