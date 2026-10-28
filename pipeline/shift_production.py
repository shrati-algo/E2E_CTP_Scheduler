"""Step 6b - what is made IN each shift (from the shift-split floor schedule).

A lot that runs across 07:00 / 15:00 / 23:00 is already cut there by step 6, so
each piece counts only in the shift it was made in. This step sums the pieces:

    outputs/<run>/SHIFT_PRODUCTION_<run>.csv     line x department x UOM x date x shift
    outputs/<run>/SHIFT_PRODUCTION_<run>.xlsx    + Building / Curing tyres per shift,
                                                   Upstream per machine per shift

Only rows inside the plan window (first shift .. last planned shift) are counted
per shift; anything after it is reported as "after plan end".
"""
from __future__ import annotations

import pathlib

import pandas as pd

SH = pd.Timedelta(hours=8)


def build(run_dir: pathlib.Path, run: str, t0: pd.Timestamp, t_end: pd.Timestamp) -> dict:
    parts = []
    for ln in ("pcr", "tbr"):
        p = run_dir / f"phase5_floor_schedule_{ln}.csv"
        if p.exists():
            d = pd.read_csv(p, encoding="utf-8-sig", parse_dates=["start_time", "end_time"])
            d["line"] = ln.upper()
            parts.append(d)
    d = pd.concat(parts, ignore_index=True)
    d["group"] = d["department"].map(lambda x: x if x in ("BUILDING", "CURING") else "UPSTREAM")
    k = ((d["start_time"] - t0) / SH).astype(int)
    lab = [f"{(t0 + i * SH - pd.Timedelta(hours=7)):%d %b} {'ABC'[i % 3]}" for i in range(int((t_end - t0) / SH))]
    d["shift_label"] = [lab[i] if 0 <= i < len(lab) else ("before plan" if i < 0 else "after plan end")
                        for i in k]
    order = lab + ["after plan end"]
    s = (d.groupby(["line", "group", "department", "UOM", "date", "shift", "shift_label"])["produce_qty"]
           .sum().round(3).reset_index())
    s.to_csv(run_dir / f"SHIFT_PRODUCTION_{run}.csv", index=False, encoding="utf-8-sig")
    tables = {}
    for g in ("BUILDING", "CURING"):
        x = d[d["group"] == g].pivot_table(index="line", columns="shift_label", values="produce_qty",
                                          aggfunc="sum").reindex(columns=[c for c in order if c in
                                          set(d.loc[d["group"] == g, "shift_label"])]).fillna(0).round(0)
        tables[g] = x
    up = (d[d["group"] == "UPSTREAM"].pivot_table(index=["line", "machine", "UOM"], columns="shift_label",
                                                  values="produce_qty", aggfunc="sum")
            .reindex(columns=[c for c in order if c in set(d["shift_label"])]).fillna(0).round(1))
    with pd.ExcelWriter(run_dir / f"SHIFT_PRODUCTION_{run}.xlsx", engine="openpyxl") as w:
        tables["BUILDING"].to_excel(w, sheet_name="Building tyres per shift")
        tables["CURING"].to_excel(w, sheet_name="Curing tyres per shift")
        up.to_excel(w, sheet_name="Upstream per machine")
        s.to_excel(w, sheet_name="All (long)", index=False)
    return tables


def show(tables: dict) -> str:
    L = []
    for g, x in tables.items():
        L.append(f"  {g} - tyres made in each shift")
        cols = list(x.columns)
        short = [(c[:2] + c[-1]) if c[:1].isdigit() else "after" for c in cols]
        L.append("    line  " + " ".join(f"{c:>7}" for c in short))
        for ln, r in x.iterrows():
            L.append(f"    {ln:5s} " + " ".join(f"{v:>7,.0f}" for v in r.values)
                     + f"   total {r.sum():,.0f}")
    return "\n".join(L)
