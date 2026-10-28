"""Step 0 - pre-flight checks: are the day's database tables there and complete?

Runs BEFORE anything is staged or scheduled. Every check is PASS, WARN or FAIL;
one FAIL and the run does not start (exit code 2). The table is printed on the
terminal and written to
    outputs/<run>/PREFLIGHT_CHECKS.csv  +  PREFLIGHT_CHECKS.md
    logs/preflight_history.csv          (one line per check, every run, appended)

THE CHECKS (plan day D)
  DB-1   database login present (db.env / environment)
  DB-2   database reachable
  BP-*   CTP_Schedule_BuildingPlan updated for D: a PCR and a TBR plan with
         plan_start on D (newest planner run if saved more than once)
  CP-*   CTP_Schedule_CuringPlan updated for D: rows of the SAME planner run
  PL-*   plan content: production rows, first start = D 07:00, no row ending
         before it starts                                  (WARN only)
  WIP-1  CTP_Opening_WIP updated for D: a shift-A sync on D
  WIP-2  that sync is complete (>= min_ratio x the usual row count)
  WIP-3  that sync was struck near 06:30 (05:30-07:30)     (WARN only)
  GT-*   CTP_GT_Inventory, the same three checks per line (PCR, TBR)
An explicit --wip-sync / --gt-sync-* replaces the shift-A rule for that table;
it is checked for existence and completeness and always shown as a WARN.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import pathlib

import pandas as pd

from . import db, plan_from_db as P, stock_snapshot as S

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _add(out, cid, check, status, detail):
    out.append({"id": cid, "check": check, "status": status, "detail": detail})


def _snapshot_checks(out, prefix, table, s: pd.DataFrame, day, ratio, override):
    """exists / complete / time for one table (one line for GT)."""
    day = pd.Timestamp(day).date()
    if override:
        hit = s[s["updated_at"].map(S._ts).str.startswith(override.strip()[:19])]
        if hit.empty:
            _add(out, f"{prefix}-1", f"{table}: explicit sync exists", FAIL,
                 f"no sync at '{override}'")
            return
        r = hit.iloc[0]
        _add(out, f"{prefix}-1", f"{table}: explicit sync exists", WARN,
             f"{S._ts(r['updated_at'])} ({int(r['n']):,} rows) chosen on the command line, "
             f"not the {day} shift-A sync")
    else:
        a = s[(pd.to_datetime(s["inventory_date"]).dt.date == day) & (s["shift"] == "A")]
        if a.empty:
            last = S._ts(s["updated_at"].max()) if len(s) else "none in the last 3 days"
            _add(out, f"{prefix}-1", f"{table}: updated for {day} (shift-A sync)", FAIL,
                 f"no shift-A sync on {day}; latest sync {last}")
            return
        r = a.iloc[0]
        _add(out, f"{prefix}-1", f"{table}: updated for {day} (shift-A sync)", PASS,
             f"{S._ts(r['updated_at'])}, {int(r['n']):,} rows")
    prev = s[s["updated_at"] < r["updated_at"]].tail(9)
    ref = float(prev["n"].max()) if len(prev) else float(r["n"])
    ok = ref <= 0 or r["n"] >= ratio * ref
    _add(out, f"{prefix}-2", f"{table}: snapshot complete", PASS if ok else FAIL,
         f"{int(r['n']):,} rows vs usual {ref:,.0f} (minimum {ratio:.0%})")
    if not override:
        hh = r["updated_at"].hour + r["updated_at"].minute / 60
        _add(out, f"{prefix}-3", f"{table}: struck near 06:30", PASS if 5.5 <= hh <= 7.5 else WARN,
             f"struck at {r['updated_at']:%H:%M}")


def run(day, pinned_runs=None, wip_override=None, gt_override=None, ratio=0.5) -> list:
    out: list = []
    day = pd.Timestamp(day).date()
    miss = db.missing_credentials()
    _add(out, "DB-1", "database login present", FAIL if miss else PASS,
         ("missing " + ", ".join(miss)) if miss else f"user {os.environ.get('CTP_DB_USER')}")
    if miss:
        return out
    try:
        cn = db.connect()
    except Exception as e:                                   # noqa: BLE001
        _add(out, "DB-2", "database reachable", FAIL, f"{type(e).__name__}: {e}")
        return out
    _add(out, "DB-2", "database reachable", PASS, os.environ.get("CTP_PPDS_NAME", ""))
    try:
        # ---- plans -------------------------------------------------------
        cat = P.catalogue(cn)
        pinned = pinned_runs or {}
        for ln in P.LINES:
            b = cat[(cat["table"] == P.BUILD_T) & (cat["tyre_type"] == ln)]
            if pinned.get(ln):
                bd = b[b["run_id"] == pinned[ln]]
            else:
                bd = b[b["plan_start"].dt.date == day].sort_values("created_at")
            if bd.empty:
                days = sorted({str(x.date()) for x in b["plan_start"]})[-5:]
                _add(out, f"BP-{ln}", f"{P.BUILD_T} {ln}: updated for {day}", FAIL,
                     f"no {ln} plan with plan_start on {day}; latest plan days {days or 'none'}")
                _add(out, f"CP-{ln}", f"{P.CURE_T} {ln}: updated for {day}", FAIL,
                     "no building plan to match")
                continue
            r = bd.iloc[-1]
            more = f"; {len(bd)} runs saved, newest taken" if len(bd) > 1 else ""
            _add(out, f"BP-{ln}", f"{P.BUILD_T} {ln}: updated for {day}", PASS,
                 f"run_id {r['run_id']} ({r['plan_id']}), saved {r['created_at']:%d %b %H:%M}, "
                 f"{int(r['n_rows'])} rows, {int(r['qty']):,} tyres{more}")
            c = cat[(cat["table"] == P.CURE_T) & (cat["tyre_type"] == ln)
                    & (cat["run_id"] == r["run_id"])]
            if c.empty:
                other = cat[(cat["table"] == P.CURE_T) & (cat["tyre_type"] == ln)
                            & (cat["plan_start"].dt.date == day)]
                _add(out, f"CP-{ln}", f"{P.CURE_T} {ln}: updated for {day}", FAIL,
                     f"no curing rows for run_id {r['run_id']}"
                     + (f" (curing has run_id {list(other['run_id'])} for {day})" if len(other) else ""))
            else:
                c = c.iloc[0]
                _add(out, f"CP-{ln}", f"{P.CURE_T} {ln}: updated for {day}", PASS,
                     f"same run_id, {int(c['n_rows'])} rows, {int(c['qty']):,} tyres")
            # content
            d = db.fetch(f"SELECT Qty, StartTime, EndTime FROM {P.BUILD_T} WHERE tyre_type = '{ln}' "
                         f"AND run_id = '{r['run_id']}'", cn)
            d["StartTime"], d["EndTime"] = pd.to_datetime(d["StartTime"]), pd.to_datetime(d["EndTime"])
            t0 = pd.Timestamp(day) + pd.Timedelta(hours=7)
            first = d.loc[d["Qty"] > 0, "StartTime"].min()
            bad = int((d["EndTime"] < d["StartTime"]).sum())
            zero = int((d["Qty"] <= 0).sum())
            issues = []
            if pd.isna(first) or abs((first - t0).total_seconds()) > 3600:
                issues.append(f"first start {first}, expected {t0}")
            if bad:
                issues.append(f"{bad} row(s) end before they start")
            if zero:
                issues.append(f"{zero} row(s) with Qty <= 0")
            _add(out, f"PL-{ln}", f"plan content {ln}", WARN if issues else PASS,
                 "; ".join(issues) or f"starts {first:%d %b %H:%M}, {int((d['Qty'] > 0).sum())} production rows")
        # ---- snapshots ---------------------------------------------------
        _snapshot_checks(out, "WIP", S.WIP_T, S._syncs(cn, S.WIP_T, day), day, ratio, wip_override)
        g = S._syncs(cn, S.GT_T, day, by_line=True)
        g["tyre_type"] = g["tyre_type"].astype(str).str.strip().str.upper()
        for ln in ("PCR", "TBR"):
            _snapshot_checks(out, f"GT-{ln}", f"{S.GT_T} {ln}",
                             g[g["tyre_type"] == ln].drop(columns="tyre_type"), day, ratio,
                             (gt_override or {}).get(ln.lower()))
    finally:
        cn.close()
    return out


def show(rows: list, day) -> str:
    """The table for the terminal (plain ASCII, aligned)."""
    w = max(len(r["check"]) for r in rows) + 2
    L = [f"  PRE-FLIGHT CHECKS - plan day {day}", "  " + "-" * (w + 60)]
    for r in rows:
        mark = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[r["status"]]
        L.append(f"  {mark} {r['id']:<8} {r['check']:<{w}} {r['detail']}")
    n = {s: sum(1 for r in rows if r["status"] == s) for s in (PASS, WARN, FAIL)}
    L.append("  " + "-" * (w + 60))
    L.append(f"  {n[PASS]} passed, {n[WARN]} warning(s), {n[FAIL]} failed"
             + ("   ->  NOT RUN: fix the failed check(s) above" if n[FAIL] else "   ->  OK to run"))
    return "\n".join(L)


def save(rows: list, day, run: str, out_dir: pathlib.Path, log_dir: pathlib.Path) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.insert(0, "checked_at", now)
    df.insert(1, "plan_day", str(day))
    df.insert(2, "run", run)
    df.to_csv(out_dir / "PREFLIGHT_CHECKS.csv", index=False, encoding="utf-8-sig")
    md = ["# Pre-flight checks", "", f"Plan day **{day}**, run `{run}`, checked {now}", "",
          "| Status | Id | Check | Detail |", "|---|---|---|---|"]
    md += [f"| {r['status']} | {r['id']} | {r['check']} | {r['detail']} |" for r in rows]
    (out_dir / "PREFLIGHT_CHECKS.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    h = log_dir / "preflight_history.csv"
    new = not h.exists()
    with h.open("a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(df.columns))
        if new:
            wr.writeheader()
        wr.writerows(df.to_dict("records"))


def failed(rows: list) -> bool:
    return any(r["status"] == FAIL for r in rows)
