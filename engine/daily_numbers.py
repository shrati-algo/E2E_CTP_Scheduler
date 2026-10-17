#!/usr/bin/env python3
"""daily_numbers.py <version> [<compare_version> ...]

Day-by-day achieved numbers of one run (plant day 07:00 -> 07:00), PCR and TBR:
building plan / built, curing plan / cured, % of plan, plus totals. Run automatically at the
end of run_v15.py; can also be run by hand, e.g.  python daily_numbers.py v85 v82

Writes latest_schedule/<version>/DAILY_NUMBERS_<version>.md and .xlsx and prints the table.
Sources (all inside latest_schedule/<version>/run_output):
  plan      PHASEWISE/00_INPUTS/building_plan.csv, plan.csv (the plan window the run used)
  built     phase5_floor_schedule_updated.csv (department BUILDING), line from the BOM green-tyre code
  cured     phase6_curing_revision_updated.csv
Quantities are split across plant days by the share of each row's time inside the day.
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))


def _split(df, s, e, q, key, days):
    out = {}
    h = (df[e] - df[s]).dt.total_seconds().replace(0, np.nan)
    for d in days:
        x = d + pd.Timedelta(days=1)
        ov = ((df[e].clip(d, x) - df[s].clip(d, x)).dt.total_seconds()).clip(lower=0)
        out[d] = (df[q] * ov / h).groupby(df[key]).sum()
    return pd.DataFrame(out).T.reindex(columns=["PCR", "TBR"]).fillna(0.0)


def daily(version):
    R = os.path.join(ROOT, "latest_schedule", version, "run_output")
    I = os.path.join(R, "PHASEWISE", "00_INPUTS")
    line = lambda s: np.where(s.astype(str).str.startswith("11"), "TBR", "PCR")
    bp = pd.read_csv(os.path.join(I, "building_plan.csv"))
    cp = pd.read_csv(os.path.join(I, "plan.csv"))
    for d in (bp, cp):
        d["s"], d["e"], d["line"] = pd.to_datetime(d["startTime"]), pd.to_datetime(d["endTime"]), line(d["skuCode"])
    t0 = min(bp["s"].min(), cp["s"].min()).floor("D") + pd.Timedelta(hours=7)
    t1 = max(cp["e"].max(), bp["e"].max())
    days = list(pd.date_range(t0, t1 - pd.Timedelta(minutes=1), freq="D"))
    bom = pd.read_csv(os.path.join(I, "bom.csv"), low_memory=False, usecols=["Super_parent", "child", "child_description"])
    g = bom[bom["child_description"] == "Green Tyres"].drop_duplicates("child")
    gt_line = dict(zip(g["child"], line(g["Super_parent"])))
    fl = pd.read_csv(os.path.join(R, "phase5_floor_schedule_updated.csv"), low_memory=False)
    b = fl[fl["department"] == "BUILDING"].copy()
    b["s"], b["e"], b["line"] = pd.to_datetime(b["start_time"]), pd.to_datetime(b["end_time"]), b["item"].map(gt_line)
    c = pd.read_csv(os.path.join(R, "phase6_curing_revision_updated.csv"), low_memory=False)
    c["s"], c["e"], c["line"] = pd.to_datetime(c["startTime"]), pd.to_datetime(c["endTime"]), line(c["skuCode"])
    P = {("build plan", k): v for k, v in _split(bp, "s", "e", "qty", "line", days).items()}
    B = {("built", k): v for k, v in _split(b, "s", "e", "produce_qty", "line", days).items()}
    CP = {("cure plan", k): v for k, v in _split(cp, "s", "e", "qty", "line", days).items()}
    C = {("cured", k): v for k, v in _split(c, "s", "e", "qty", "line", days).items()}
    T = pd.DataFrame({**P, **B, **CP, **C}).round(0)
    T.index = [d.strftime("%d-%b") for d in T.index]
    T.loc["TOTAL"] = T.sum()
    for ln in ("PCR", "TBR"):
        T[("cured % of plan", ln)] = (100 * T[("cured", ln)] / T[("cure plan", ln)].replace(0, np.nan)).round(1)
    T[("cured % of plan", "ALL")] = (100 * (T[("cured", "PCR")] + T[("cured", "TBR")])
                                     / (T[("cure plan", "PCR")] + T[("cure plan", "TBR")]).replace(0, np.nan)).round(1)
    return T, t0, t1


def to_md(T, version, t0, t1):
    cols = [("build plan", "PCR"), ("built", "PCR"), ("cure plan", "PCR"), ("cured", "PCR"), ("cured % of plan", "PCR"),
            ("build plan", "TBR"), ("built", "TBR"), ("cure plan", "TBR"), ("cured", "TBR"), ("cured % of plan", "TBR"),
            ("cured % of plan", "ALL")]
    head = ["Plant day", "PCR build plan", "PCR built", "PCR cure plan", "PCR cured", "PCR cured %",
            "TBR build plan", "TBR built", "TBR cure plan", "TBR cured", "TBR cured %", "All cured %"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for i, r in T[cols].iterrows():
        v = [f"{x:,.0f}" if j % 5 != 4 and j != 10 else (f"{x:.1f} %" if x == x else "–") for j, x in enumerate(r)]
        lab = f"**{i}**" if i == "TOTAL" else i
        lines.append("| " + " | ".join([lab] + v) + " |")
    tot = T.loc["TOTAL"]
    cured = tot[("cured", "PCR")] + tot[("cured", "TBR")]
    plan = tot[("cure plan", "PCR")] + tot[("cure plan", "TBR")]
    return (f"# Daily numbers — {version}\n\n"
            f"Plan window {t0:%d-%b %H:%M} → {t1:%d-%b %H:%M}; plant day = 07:00 → 07:00. "
            f"Rows that span two days are split by time.\n\n"
            f"**Cured {cured:,.0f} of {plan:,.0f} planned ({100 * cured / plan:.1f} %)**: "
            f"PCR {tot[('cured', 'PCR')]:,.0f} / {tot[('cure plan', 'PCR')]:,.0f}, "
            f"TBR {tot[('cured', 'TBR')]:,.0f} / {tot[('cure plan', 'TBR')]:,.0f}. "
            f"Built PCR {tot[('built', 'PCR')]:,.0f}, TBR {tot[('built', 'TBR')]:,.0f}.\n\n" + "\n".join(lines) + "\n")


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    v = sys.argv[1]
    T, t0, t1 = daily(v)
    vdir = os.path.join(ROOT, "latest_schedule", v)
    md = to_md(T, v, t0, t1)
    for other in sys.argv[2:]:
        To, _, _ = daily(other)
        a, o = T.loc["TOTAL"], To.loc["TOTAL"]
        md += (f"\n**Against {other}:** cured PCR {a[('cured', 'PCR')] - o[('cured', 'PCR')]:+,.0f}, "
               f"TBR {a[('cured', 'TBR')] - o[('cured', 'TBR')]:+,.0f}, "
               f"total {a[('cured', 'PCR')] + a[('cured', 'TBR')] - o[('cured', 'PCR')] - o[('cured', 'TBR')]:+,.0f}.\n")
    open(os.path.join(vdir, f"DAILY_NUMBERS_{v}.md"), "w", encoding="utf-8").write(md)
    X = T.copy()
    X.columns = [f"{a} {b}" for a, b in X.columns]
    X.to_excel(os.path.join(vdir, f"DAILY_NUMBERS_{v}.xlsx"), sheet_name="Daily numbers")
    print(md)
    print(f"written {os.path.join(vdir, f'DAILY_NUMBERS_{v}.md')} (+ .xlsx)")


if __name__ == "__main__":
    main()
