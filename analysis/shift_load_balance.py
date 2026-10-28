#!/usr/bin/env python3
"""shift_load_balance.py - is each machine's load level from shift to shift?

    python analysis/shift_load_balance.py --run <run> [--band 0.20]

For EVERY machine (upstream, TBM, press), per shift (A 07-15, B 15-23,
C 23-07), from the plan start to the plan end (both from the run's own plan):
  run_h, changeover_h, qty (its own unit), and the idle hours split by cause:
    WAIT_INPUT  next lot's inputs not made + rested yet (binding input named)
    BR14        changeover blackout around the shift change
    HELD        inputs ready, the dispatcher / campaign / mix plan held the lot
    NO_WORK     nothing left on the machine (its demand is finished)
    NOT_CURED   (press) the plan still has lots here, but their green tyres
                never arrive (SKU not built in time / data-blocked)
    KIT_WAIT    (TBM) next trolley's kit not ready
    PLAN_GAP    (TBM / press) the plan itself has nothing there
    GT_WAIT     (press) green tyres not ready
Balance test: a machine is LEVEL when every working shift is within +-band of
its mean working-shift run hours (and of its mean qty). The working window is
the machine's first to last shift with work; the empty tail after its demand is
done is reported separately (NO_WORK) and not counted as imbalance.

Writes <out>/<run>/analysis/SHIFT_LOAD_BALANCE_<run>.xlsx (+ .shift_detail.csv)
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import pandas as pd

import _common as C

TR = pd.Timedelta(minutes=10)
SH = pd.Timedelta(hours=8)
T0 = T_END = None          # set from the run's own plan in main()
SHIFTS: list = []


def sname(t):
    k = int((t - T0) / SH)
    day = (T0 + k * SH - pd.Timedelta(hours=7)).date()
    return k, f"{day:%d %b} {'ABC'[k % 3]}"


WHY = defaultdict(set)


def spread(rows, m, t0, t1, field, val, extra=None):
    """add `val` (hours if val is None) for [t0,t1) into per-shift buckets."""
    tot = max((t1 - t0).total_seconds(), 1e-9)      # whole interval: qty is prorated on it
    t0, t1 = max(t0, T0), min(t1, T_END)
    if t1 <= t0:
        return
    t = t0
    while t < t1:
        k, _ = sname(t)
        e = min(t1, T0 + (k + 1) * SH)
        h = (e - t).total_seconds() / 3600
        rows[(m, k)][field] += h if val is None else val * (e - t).total_seconds() / tot
        if extra:
            WHY[(m, k, field)].add(extra)
        t = e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--band", type=float, default=0.20)
    a = ap.parse_args()
    global T0, T_END, SHIFTS
    D = C.run_dir(a.run)
    T0, T_END = C.plan_window(D)
    SHIFTS = [T0 + i * SH for i in range(int((T_END - T0) / SH))]
    rows = defaultdict(lambda: defaultdict(float))
    meta = {}

    # ---------------- upstream ------------------------------------------------
    S, E, MA = [], [], {}
    for ln in ("pcr", "tbr"):
        s = pd.read_csv(D / ln / "phase5_schedule.csv", parse_dates=["start", "finish", "LFT"])
        s["co"] = pd.to_numeric(s["changeover_min"], errors="coerce").fillna(0.0)
        s["occ"] = s["start"] - pd.to_timedelta(s["co"], unit="m")
        s["line"] = ln.upper()
        S.append(s)
        E.append(pd.read_csv(D / ln / "phase4_edges.csv"))
        d = pd.read_csv(D / ln / "phase1_demand.csv")
        for k, v in d.groupby("item_code")["min_aging_h"].max().items():
            MA[(ln.upper(), k)] = float(v or 0)
    S = pd.concat(S, ignore_index=True)
    S["machine"] = S["machine"].astype(str)
    E = pd.concat(E, ignore_index=True)
    idx = S.drop_duplicates("lot_id").set_index("lot_id")
    prod_of = E.groupby("consumer_lot")["producer_lot"].apply(list).to_dict()

    def inputs(lot):
        best, key = None, None
        for p in prod_of.get(lot, ()):
            if p not in idx.index:
                continue
            x = idx.loc[p]
            rt = x["finish"] + pd.Timedelta(hours=MA.get((x["line"], x["item_code"]), 0.0)) + TR
            if best is None or rt > best:
                best, key = rt, p
        return best, key

    def br14(t, co):
        return co > 0 and any(abs((t - (t.normalize() + pd.Timedelta(hours=h, minutes=30))).total_seconds()) < 90
                              for h in (7, 15, 23))

    for m, g in S.groupby("machine"):
        if m.startswith("ASSUMED"):
            continue
        g = g.sort_values("occ")
        meta[m] = {"group": "UPSTREAM", "name": g["machine_name"].iloc[0],
                   "line": "/".join(sorted(g["line"].unique())), "dept": g["department"].iloc[0],
                   "uom": "/".join(sorted(g["uom"].astype(str).unique()))}
        free = T0
        for r in g.itertuples(index=False):
            if r.occ > free + pd.Timedelta(seconds=30):
                rdy, p = inputs(r.lot_id)
                g_end = r.occ
                # BR-14 owns at most the blackout window (30 min) + the changeover
                # that could not start inside it, at the END of the gap
                if br14(r.occ, r.co):
                    b0 = max(free, r.occ - pd.Timedelta(minutes=60))
                    if p is not None and rdy is not None:
                        b0 = max(b0, min(rdy, r.occ))
                    spread(rows, m, b0, r.occ, "BR14", None)
                    g_end = b0
                if g_end > free:
                    if p is not None and rdy >= g_end - pd.Timedelta(minutes=1):
                        x = idx.loc[p]
                        spread(rows, m, free, g_end, "WAIT_INPUT", None, f"{x['item_code']}@{x['machine']}")
                    else:
                        r0 = min(max(free, rdy), g_end) if rdy is not None else free
                        if r0 > free:
                            x = idx.loc[p]
                            spread(rows, m, free, r0, "WAIT_INPUT", None, f"{x['item_code']}@{x['machine']}")
                        spread(rows, m, r0, g_end, "HELD", None,
                               f"{r.item_code} (LFT {(f'{r.LFT:%d %H:%M}' if pd.notna(r.LFT) else '-')}{', mix plan' if str(getattr(r, 'mix_plan', '')) == 'fixed' else ''})")
            if r.co > 0:
                spread(rows, m, r.occ, r.start, "changeover_h", None)
            spread(rows, m, r.start, r.finish, "run_h", None)
            spread(rows, m, r.start, r.finish, "qty", float(r.lot_qty))
            free = max(free, r.finish)
        spread(rows, m, free, T_END, "NO_WORK", None)

    # ---------------- building ------------------------------------------------
    for ln in ("pcr", "tbr"):
        b = pd.read_csv(D / ln / "phase5_building_plan.csv",
                        parse_dates=["build_start", "build_end", "feasible_start", "feasible_end", "kit_ready"])
        b["machine"] = b["machine"].astype(str)
        for m, g in b.groupby("machine"):
            g = g.sort_values("feasible_start")
            meta[m] = {"group": "BUILDING", "name": g["machine_name"].dropna().iloc[0] if g["machine_name"].notna().any() else "TBM",
                       "line": ln.upper(), "dept": "BUILDING", "uom": "tyres"}
            free = T0
            for r in g.itertuples(index=False):
                fs = r.feasible_start
                if fs > free + pd.Timedelta(seconds=30):
                    if pd.notna(r.kit_ready) and r.kit_ready >= fs - pd.Timedelta(minutes=1):
                        spread(rows, m, free, fs, "KIT_WAIT", None, str(r.blocking_item))
                    else:
                        spread(rows, m, free, fs, "PLAN_GAP", None)
                spread(rows, m, fs, r.feasible_end, "run_h", None)
                spread(rows, m, fs, r.feasible_end, "qty", float(r.qty))
                free = max(free, r.feasible_end)
            spread(rows, m, free, T_END, "NO_WORK", None)
    # ---------------- curing --------------------------------------------------
    for ln in ("pcr", "tbr"):
        c = pd.read_csv(D / ln / "phase7_curing_plan.csv",
                        parse_dates=["planned_start", "feasible_start", "feasible_end", "gt_ready"])
        c["press"] = c["press"].astype(str)
        for m, g in c.groupby("press"):
            meta[m] = {"group": "CURING", "name": g["press_name"].iloc[0], "line": ln.upper(),
                       "dept": "CURING", "uom": "tyres"}
            uncured = g[g["feasible_start"].isna()]
            g = g[g["feasible_start"].notna()].sort_values("feasible_start")
            free = T0
            for r in g.itertuples(index=False):
                fs = r.feasible_start
                if fs > free + pd.Timedelta(seconds=30):
                    if pd.notna(r.gt_ready) and r.gt_ready >= fs - pd.Timedelta(minutes=1):
                        spread(rows, m, free, fs, "GT_WAIT", None, str(r.sku))
                    else:
                        spread(rows, m, free, fs, "PLAN_GAP", None)
                spread(rows, m, fs, r.feasible_end, "run_h", None)
                spread(rows, m, fs, r.feasible_end, "qty", float(r.qty))
                free = max(free, r.feasible_end)
            # planned press time whose green tyres never come (SKU not built / blocked)
            if len(uncured) and free < T_END:
                why = ",".join(sorted({f"{x.sku}{'(' + str(x.blocker_code) + ')' if pd.notna(x.blocker_code) else ''}"
                                       for x in uncured.itertuples()}))[:200]
                spread(rows, m, free, T_END, "NOT_CURED", None, why)
            else:
                spread(rows, m, free, T_END, "NO_WORK", None)

    # ---------------- per shift table ----------------------------------------
    causes = ["WAIT_INPUT", "HELD", "BR14", "KIT_WAIT", "GT_WAIT", "PLAN_GAP", "NOT_CURED", "NO_WORK"]
    out = []
    for (m, k), v in rows.items():
        r = {"machine": m, **meta.get(m, {}), "k": k, "shift": sname(T0 + k * SH)[1],
             "run_h": round(v["run_h"], 2), "changeover_h": round(v["changeover_h"], 2), "qty": round(v["qty"], 1)}
        for c in causes:
            r[c] = round(v[c], 2)
            r["why_" + c] = ", ".join(sorted(WHY.get((m, k, c), set())))[:300]
        out.append(r)
    P = pd.DataFrame(out).sort_values(["group", "machine", "k"])
    P = P[P["k"] < len(SHIFTS)]

    # ---------------- balance verdict ----------------------------------------
    ver = []
    for m, g in P.groupby("machine"):
        work = g[g["run_h"] > 0.05]
        if work.empty:
            ver.append({"machine": m, **meta[m], "verdict": "NO WORK", "working_shifts": 0})
            continue
        win = g[(g["k"] >= work["k"].min()) & (g["k"] <= work["k"].max())]
        # the last working shift is usually partial (the machine's demand ends) - judged separately
        core = win[win["k"] < work["k"].max()] if len(win) > 2 else win
        mean_h, mean_q = core["run_h"].mean(), core["qty"].mean()
        lo, hi = mean_h * (1 - a.band), mean_h * (1 + a.band)
        out_band = core[(core["run_h"] < lo) | (core["run_h"] > hi)]
        qlo, qhi = mean_q * (1 - a.band), mean_q * (1 + a.band)
        q_out = core[(core["qty"] < qlo) | (core["qty"] > qhi)]
        idle = core[causes].sum()
        top = idle.sort_values(ascending=False)
        lowest = core.sort_values("run_h").iloc[0]
        low_cause = max(causes, key=lambda c: lowest[c])
        ver.append({"machine": m, **meta[m],
                    "first_shift": work["shift"].iloc[0], "last_shift": work["shift"].iloc[-1],
                    "working_shifts": len(core), "mean_run_h": round(mean_h, 2),
                    "min_run_h": round(core["run_h"].min(), 2), "max_run_h": round(core["run_h"].max(), 2),
                    "shifts_outside_band_h": len(out_band),
                    "mean_qty": round(mean_q, 0), "min_qty": round(core["qty"].min(), 0),
                    "max_qty": round(core["qty"].max(), 0), "max_over_min_qty": round(core["qty"].max() / max(core["qty"].min(), 1e-9), 1),
                    "shifts_outside_band_qty": len(q_out),
                    "verdict": "LEVEL" if len(out_band) == 0 and len(q_out) == 0 else
                               ("UNBALANCED (hours)" if len(out_band) else "UNBALANCED (qty only - item mix)"),
                    "main_idle_cause": top.index[0] if top.iloc[0] > 0 else "",
                    "idle_by_cause_h": "; ".join(f"{c} {v:.1f}" for c, v in top.items() if v > 0.05),
                    "lowest_shift": lowest["shift"], "lowest_run_h": lowest["run_h"],
                    "lowest_shift_cause": f"{low_cause} {lowest[low_cause]:.1f} h " + lowest.get("why_" + low_cause, ""),
                    "tail_no_work_h": round(g[g["k"] > work["k"].max()]["NO_WORK"].sum() + g[g["k"] == work["k"].max()]["NO_WORK"].sum(), 1)})
    V = pd.DataFrame(ver)
    Hh = P.pivot_table(index="machine", columns="k", values="run_h", aggfunc="sum").fillna(0)
    Hh.columns = [sname(T0 + k * SH)[1] for k in Hh.columns]
    Qq = P.pivot_table(index="machine", columns="k", values="qty", aggfunc="sum").fillna(0).round(0)
    Qq.columns = Hh.columns
    outp = C.analysis_dir(D) / f"SHIFT_LOAD_BALANCE_{a.run}.xlsx"
    with pd.ExcelWriter(outp, engine="openpyxl") as xw:
        for nm, df, ix in (("Verdict", V.sort_values(["group", "verdict", "machine"]), False),
                           ("Run hours by shift", Hh, True), ("Qty by shift", Qq, True),
                           ("Shift detail", P, False)):
            df.to_excel(xw, sheet_name=nm, index=ix)
            ws = xw.sheets[nm]
            ws.freeze_panes = "B2"
    P.to_csv(outp.with_suffix(".shift_detail.csv"), index=False)
    pd.set_option("display.width", 400); pd.set_option("display.max_colwidth", 160); pd.set_option("display.max_rows", 400)
    print(V.groupby(["group", "verdict"]).size())
    print(f"[OK] {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
