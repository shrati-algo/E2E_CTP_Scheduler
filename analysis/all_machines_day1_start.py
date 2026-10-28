#!/usr/bin/env python3
"""all_machines_day1_start.py - does every machine start in day-1 shift A? If not, why?

    python analysis/all_machines_day1_start.py --run <run>

Reads only the run's outputs. Day 1 = the plan's first plant day (07:00-15:00 = shift A).

Three groups:
  UPSTREAM  mixers, extruders, calenders, bead, TDA, super assembly (phase5_schedule)
  BUILDING  tyre building machines (phase5_building_plan)
  CURING    presses (phase7_curing_plan)
plus register machines that carry no work at all (MACHINE_COVERAGE).

For each machine: first start, hours run / changeover in shift A (27 Oct 07-15),
and the cause of the gap between 07:00 and the first start. A wait for inputs
is traced back through the producing lots ("chain") to its root: a lot that
started at 07:00 (process + rest time), a machine busy with other lots first,
or a rule.

Writes <out>/<run>/analysis/ALL_MACHINES_DAY1_START_<run>.xlsx
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

import _common as C

T0 = A_END = None          # set from the run's own plan in main()
TR = pd.Timedelta(minutes=10)
TOL = pd.Timedelta(minutes=5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    global T0, A_END
    D = C.run_dir(a.run)
    T0, _ = C.plan_window(D)
    A_END = T0 + pd.Timedelta(hours=8)
    cfg = json.loads((D / "DATA_STAGED.json").read_text()) if (D / "DATA_STAGED.json").exists() else {}

    # ---------------- upstream: both lines on one timeline per machine -------
    S, E, MA = [], [], {}
    for ln in ("pcr", "tbr"):
        s = pd.read_csv(D / ln / "phase5_schedule.csv", parse_dates=["start", "finish", "LFT"])
        s["co"] = pd.to_numeric(s["changeover_min"], errors="coerce").fillna(0.0)
        s["occ"] = s["start"] - pd.to_timedelta(s["co"], unit="m")
        s["line"] = ln.upper()
        S.append(s)
        e = pd.read_csv(D / ln / "phase4_edges.csv")
        E.append(e)
        d = pd.read_csv(D / ln / "phase1_demand.csv")
        for k, v in d.groupby("item_code")["min_aging_h"].max().items():
            MA[(ln.upper(), k)] = float(v or 0)
    S = pd.concat(S, ignore_index=True)
    S["machine"] = S["machine"].astype(str)
    E = pd.concat(E, ignore_index=True)
    idx = S.drop_duplicates("lot_id").set_index("lot_id")
    prod_of = E.groupby("consumer_lot")["producer_lot"].apply(list).to_dict()

    def ready_of(p):
        x = idx.loc[p]
        return x["finish"] + pd.Timedelta(hours=MA.get((x["line"], x["item_code"]), 0.0)) + TR

    def inputs(lot):
        """(ready time of the latest input, that producer lot) or (T0, None)."""
        best, key = None, None
        for p in prod_of.get(lot, ()):
            if p not in idx.index:
                continue
            rt = ready_of(p)
            if best is None or rt > best:
                best, key = rt, p
        return (best, key) if key else (T0, None)

    def chain(lot, depth=0):
        """Walk the binding input back to its root. Returns list of steps."""
        steps = []
        cur = lot
        while depth < 8:
            rdy, p = inputs(cur)
            if p is None:
                break
            x = idx.loc[p]
            st = x["occ"]
            prdy, _ = inputs(p)
            m = str(x["machine"])
            before = S[(S["machine"] == m) & (S["occ"] < st) & (S["occ"] >= T0)].sort_values("occ")
            if prdy >= st - pd.Timedelta(minutes=1) and _ is not None:
                why = "waited for its own input"
            elif st <= T0 + TOL:
                why = "started 07:00 - process + rest time"
            elif len(before):
                why = ("machine busy first with " +
                       ", ".join(f"{b.item_code}" for b in before.head(6).itertuples()) +
                       (f" (+{len(before) - 6} more)" if len(before) > 6 else ""))
            else:
                why = "inputs ready, start held by a rule (campaign / mix plan / BR-14)"
            steps.append({"item": x["item_code"], "machine": m,
                          "machine_name": x.get("machine_name", ""),
                          "start": st, "finish": x["finish"],
                          "rest_h": MA.get((x["line"], x["item_code"]), 0.0),
                          "ready": ready_of(p), "why": why})
            if why != "waited for its own input":
                break
            cur, depth = p, depth + 1
        return steps

    def fmt_chain(steps):
        return " <- ".join(
            f"{s['item']} on {s['machine']} {s['start']:%d %H:%M}-{s['finish']:%H:%M}"
            f" (+{s['rest_h']:g} h rest, ready {s['ready']:%d %H:%M})" for s in steps) + \
            (f"  ROOT: {steps[-1]['why']}" if steps else "")

    up_rows, up_gaps = [], []
    for m, g in S.groupby("machine"):
        g = g.sort_values("occ")
        name = g["machine_name"].iloc[0]
        lines = "/".join(sorted(g["line"].unique()))
        a_run = a_co = 0.0
        for r in g.itertuples(index=False):
            s0, s1 = max(r.start, T0), min(r.finish, A_END)
            if s1 > s0:
                a_run += (s1 - s0).total_seconds() / 3600
            c0, c1 = max(r.occ, T0), min(r.start, A_END)
            if c1 > c0:
                a_co += (c1 - c0).total_seconds() / 3600
        f = g.iloc[0]
        rdy, p = inputs(f["lot_id"])
        steps = chain(f["lot_id"]) if p is not None else []
        if f["occ"] <= T0 + TOL:
            status, cause = "STARTS 07:00", ""
        else:
            if p is not None and rdy >= f["occ"] - pd.Timedelta(minutes=1):
                cause = "WAIT INPUT: " + fmt_chain(steps)
            elif f["co"] > 0 and any(abs((f["occ"] - (T0.normalize() + pd.Timedelta(hours=h, minutes=30))).total_seconds()) < 90
                                     for h in (7, 15, 23)):
                cause = "BR-14 blackout"
            else:
                cause = (f"inputs ready {max(rdy, T0):%d %H:%M}; start held by a rule "
                         f"(campaign release / mix plan)")
            status = "LATE IN SHIFT A" if f["occ"] < A_END else "NOT IN SHIFT A"
        up_rows.append({"group": "UPSTREAM", "machine": m, "machine_name": name, "line": lines,
                        "department": f["department"], "first_start": f["occ"],
                        "late_h": round(max(0.0, (f["occ"] - T0).total_seconds() / 3600), 2),
                        "shiftA_run_h": round(a_run, 2), "shiftA_changeover_h": round(a_co, 2),
                        "shiftA_idle_h": round(8 - a_run - a_co, 2),
                        "first_lot": f["lot_id"], "first_item": f["item_code"],
                        "first_LFT": f["LFT"], "status": status, "cause": cause,
                        "binding_input": steps[0]["item"] if steps else "",
                        "root": steps[-1]["why"] if steps else ""})
        # every idle gap in shift A (after the first start too)
        free = T0
        for r in g.itertuples(index=False):
            if r.occ >= A_END:
                if free < A_END:
                    up_gaps.append({"machine": m, "from": free, "to": A_END, "next_item": r.item_code,
                                    "why": "next lot starts after shift A: " +
                                           (fmt_chain(chain(r.lot_id)) if inputs(r.lot_id)[1] else "held by rule")})
                break
            if r.occ > free + pd.Timedelta(minutes=2):
                rr, pp = inputs(r.lot_id)
                why = ("WAIT INPUT: " + fmt_chain(chain(r.lot_id))
                       if pp is not None and rr >= r.occ - pd.Timedelta(minutes=1)
                       else "inputs ready; BR-14 / rule")
                up_gaps.append({"machine": m, "from": free, "to": r.occ, "next_item": r.item_code, "why": why})
            free = max(free, r.finish)

    # ---------------- building ----------------------------------------------
    b_rows = []
    for ln in ("pcr", "tbr"):
        b = pd.read_csv(D / ln / "phase5_building_plan.csv",
                        parse_dates=["build_start", "build_end", "feasible_start", "feasible_end", "kit_ready"])
        b["machine"] = b["machine"].astype(str)
        for m, g in b.groupby("machine"):
            g = g.sort_values("feasible_start")
            f = g.iloc[0]
            a_run = sum(max(0.0, (min(r.feasible_end, A_END) - max(r.feasible_start, T0)).total_seconds() / 3600)
                        for r in g.itertuples(index=False))
            plan_first = g["build_start"].min()
            blk = str(f.get("blocking_lot", "") or "")
            blk_lot = blk.split("::", 1)[1] if "::" in blk else blk
            steps = []
            if blk_lot in idx.index:
                x = idx.loc[blk_lot]
                prdy, pp = inputs(blk_lot)
                head = {"item": x["item_code"], "machine": str(x["machine"]), "start": x["occ"],
                        "finish": x["finish"], "rest_h": MA.get((x["line"], x["item_code"]), 0.0),
                        "ready": ready_of(blk_lot)}
                if pp is not None and prdy >= x["occ"] - pd.Timedelta(minutes=1):
                    head["why"] = "waited for its own input"
                    steps = [head] + chain(blk_lot)
                else:
                    before = S[(S["machine"] == head["machine"]) & (S["occ"] < x["occ"]) & (S["occ"] >= T0)]
                    head["why"] = ("started 07:00 - process + rest time" if x["occ"] <= T0 + TOL else
                                   "machine busy first with " + ", ".join(before.sort_values("occ")["item_code"].head(6))
                                   if len(before) else "inputs ready, start held by a rule")
                    steps = [head]
            if f["feasible_start"] <= T0 + TOL:
                status, cause = "STARTS 07:00", "kit from stock / ready at 07:00"
            else:
                status = "LATE IN SHIFT A" if f["feasible_start"] < A_END else "NOT IN SHIFT A"
                if plan_first > T0 + TOL and f["feasible_start"] <= plan_first + TOL:
                    cause = f"the PLAN starts this machine at {plan_first:%d %H:%M}"
                elif steps:
                    cause = (f"kit ready {f['kit_ready']:%d %H:%M}, last part {f['blocking_item']}: "
                             + fmt_chain(steps))
                else:
                    cause = f"kit ready {f['kit_ready']:%d %H:%M}, last part {f.get('blocking_item', '')}"
            b_rows.append({"group": "BUILDING", "machine": m, "machine_name": f["machine_name"],
                           "line": ln.upper(), "plan_first_start": plan_first,
                           "first_start": f["feasible_start"],
                           "late_h": round(max(0.0, (f["feasible_start"] - T0).total_seconds() / 3600), 2),
                           "shiftA_run_h": round(a_run, 2), "first_trolley": f["building_lot_id"],
                           "first_sku": f["sku"], "kit_ready": f["kit_ready"],
                           "last_part": f.get("blocking_item", ""), "status": status, "cause": cause,
                           "root": steps[-1]["why"] if steps else ""})

    # ---------------- curing --------------------------------------------------
    c_rows = []
    for ln in ("pcr", "tbr"):
        c = pd.read_csv(D / ln / "phase7_curing_plan.csv",
                        parse_dates=["planned_start", "feasible_start", "feasible_end", "gt_ready"])
        b = pd.read_csv(D / ln / "phase5_building_plan.csv", parse_dates=["feasible_start", "feasible_end"])
        c["press"] = c["press"].astype(str)
        for m, g in c.groupby("press"):
            g = g.sort_values("feasible_start")
            f = g.iloc[0]
            a_run = sum(max(0.0, (min(r.feasible_end, A_END) - max(r.feasible_start, T0)).total_seconds() / 3600)
                        for r in g.itertuples(index=False))
            plan_first = g["planned_start"].min()
            pa = g[g["planned_start"] == plan_first].iloc[0]
            if f["feasible_start"] <= T0 + TOL:
                status, cause = "STARTS 07:00", "green tyres from GT inventory"
            else:
                status = "LATE IN SHIFT A" if f["feasible_start"] < A_END else "NOT IN SHIFT A"
                if plan_first > T0 + TOL and f["feasible_start"] <= plan_first + TOL:
                    cause = f"the PLAN starts this press at {plan_first:%d %H:%M}"
                else:
                    bb = b[b["sku"] == pa["sku"]].sort_values("feasible_end")
                    src = (f"first trolley of {pa['sku']} on TBM {bb.iloc[0]['machine']} "
                           f"{bb.iloc[0]['feasible_start']:%d %H:%M}-{bb.iloc[0]['feasible_end']:%d %H:%M}"
                           if len(bb) else f"{pa['sku']} not built in the plan horizon")
                    _gr = f"{pa['gt_ready']:%d %H:%M}" if pd.notna(pa['gt_ready']) else "never"
                    cause = (f"no GT stock for {pa['sku']}; green tyres ready {_gr} ({src})")
                    if pd.notna(pa.get("blocker_code")) and str(pa.get("blocker_code")):
                        cause += f"; blocker {pa['blocker_code']}"
            c_rows.append({"group": "CURING", "machine": m, "machine_name": f["press_name"], "line": ln.upper(),
                           "plan_first_start": plan_first, "first_start": f["feasible_start"],
                           "late_h": round(max(0.0, (f["feasible_start"] - T0).total_seconds() / 3600), 2),
                           "shiftA_run_h": round(a_run, 2), "first_sku": pa["sku"],
                           "gt_ready": pa["gt_ready"], "sku_state": pa.get("sku_state", ""),
                           "status": status, "cause": cause})

    # ---------------- machines with no work at all ---------------------------
    cov = pd.read_csv(D / "MACHINE_COVERAGE.csv")
    none = cov[cov["lots"] == 0][["machine", "name", "line", "issue"]].copy()

    UP, BU, CU = pd.DataFrame(up_rows), pd.DataFrame(b_rows), pd.DataFrame(c_rows)
    out = C.analysis_dir(D) / f"ALL_MACHINES_DAY1_START_{a.run}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for nm, df in (("Upstream", UP.sort_values(["status", "late_h"])),
                       ("Upstream shift-A gaps", pd.DataFrame(up_gaps)),
                       ("Building", BU.sort_values(["line", "late_h"])),
                       ("Curing", CU.sort_values(["line", "late_h"])),
                       ("No work", none)):
            df.to_excel(xw, sheet_name=nm[:31], index=False)
            ws = xw.sheets[nm[:31]]
            for j, col in enumerate(df.columns, start=1):
                w = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)])
                ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max(9, w + 2), 70)
            ws.freeze_panes = "A2"
    for nm, df in (("UPSTREAM", UP), ("BUILDING", BU), ("CURING", CU)):
        print(f"  {nm:9s}: " + ", ".join(f"{k} {v}" for k, v in df["status"].value_counts().items()))
    print(f"  NO WORK  : {len(none)} register machine(s)")
    print(f"[OK] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
