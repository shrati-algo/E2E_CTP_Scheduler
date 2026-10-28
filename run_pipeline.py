#!/usr/bin/env python3
"""run_pipeline.py - the daily CTP end-to-end schedule, straight from the database (v46k engine).

    python run_pipeline.py                      # the current plant day (see below)
    python run_pipeline.py --date 2026-10-28    # the plan of a given plant day

PLANT DAY: a day runs from 06:30 to 06:29 the next morning. Run at any time from
06:30 on day 1 to 06:29 on day 2 and the run is for DAY 1 - its plans, its 06:30
stock and its 06:30 green tyres. --date overrides this.

Everything comes from JK_CTP_PPDS for that plant day; nothing else is needed
except `db.env` (the database login, never in git).

STEPS (each has its own page in docs/steps/)
   0  pre-flight    are the day's tables updated? building plan, curing plan, WIP and
                    GT snapshots - PASS / WARN / FAIL on the terminal, logged; any FAIL
                    and nothing runs (exit 2). --check-only stops after this step.
   1  plan          CTP_Schedule_BuildingPlan + CTP_Schedule_CuringPlan, plan_start on
                    the day (newest planner run if saved more than once)
   2  snapshots     CTP_Opening_WIP + CTP_GT_Inventory: the shift-A (06:30) sync of the
                    day, with completeness gates
   3  masters       BOM v2, routing, FRC, ageing, MPQ, changeover, machines, holidays,
                    downtime ... + the chosen stock / GT snapshots -> data/ppds_<day>/
   4  stock split   the opening stock split PCR / TBR by this plan's own demand
   5  scheduler     engine phases 0-7, both lines (joint upstream)
   6  floor         the shift-wise floor schedule (lots split at 07:00 / 15:00 / 23:00)
   7  checks        routing validation, machine coverage, business / mixing rules
   8  workbooks     the plant's schedule book + per-line book
   9  results       fulfilment day by day, VALIDATION_REPORT (blockers, assumptions, gates)
  10  stock reports bypass conversion, stock present vs consumed, day-1 stock vs building
  11  analysis      day-1 machine starts, shift load balance (engine + published),
                    built-but-not-cured, extruder idle
  12  persist       the complete schedule (upstream + building + curing, shift pieces) is
                    written to JK_CTP_PPDS.CTP_Schedule_Floor under this run's schedule_id,
                    then read back and compared with the outputs (--no-db-write: prepare
                    and check the rows only, write nothing)
  13  phase tests   phase-wise test reports of THIS run (7 phases, every check evaluated)
                    -> phased-testing/reports/<schedule_id>/TEST_CASES_PHASE1..7.md + SUMMARY.md
  14  summary       RUN_SUMMARY.md + console

OUTPUTS: outputs/<run>/  (run = v46k_db_<day> unless --run). Start with RUN_SUMMARY.md.
LOGS:    outputs/<run>/logs/run_<time>.log  - everything printed on the terminal
         logs/preflight_history.csv          - every pre-flight check of every run
EXIT CODE: 0 when the run completed and every routing check passed, 1 otherwise,
2 when a pre-flight check failed (nothing ran), 3 when a required step failed,
4 when the schedule was made but writing it to CTP_Schedule_Floor (or its read-back) failed
(the two machine-coverage findings - bypassed cutters / slitters idle - are known).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
ANALYSIS = ROOT / "analysis"
PHASED = ROOT / "phased-testing"
sys.path.insert(0, str(ROOT))
from pipeline import (db, persist_floor, plan_from_db, preflight,  # noqa: E402
                      shift_production, stock_snapshot)

SKIP_TABLES = "CTP_Live_Inventory,CTP_Live_Inventory_Raw"   # staged by refresh, read by no phase
LOG: list = []
DAY_START = dt.time(6, 30)          # a plant day runs 06:30 -> 06:29 next morning


def plant_day(now: dt.datetime | None = None) -> dt.date:
    """The plant day a run started at `now` belongs to: 06:30 day 1 .. 06:29 day 2 -> day 1."""
    now = now or dt.datetime.now()
    return (now - dt.timedelta(hours=DAY_START.hour, minutes=DAY_START.minute)).date()


class Tee:
    """Everything printed goes to the terminal AND the run log."""
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = path.open("a", encoding="utf-8")
        self.t = sys.__stdout__

    def write(self, s):
        self.t.write(s)
        self.f.write(s)
        return len(s)

    def flush(self):
        self.t.flush()
        self.f.flush()


def step(n, label, script, env, args, optional=False, cwd=ENGINE) -> bool:
    print(f"\n{'=' * 78}\n  [{n}] {label}\n{'=' * 78}", flush=True)
    t = dt.datetime.now()
    p = subprocess.Popen([sys.executable, str(script), *args], cwd=cwd, env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         encoding="utf-8", errors="replace", bufsize=1)
    for line in p.stdout:                    # to the terminal and the log, as it runs
        sys.stdout.write(line)
    rc = p.wait()
    sys.stdout.flush()
    LOG.append({"step": str(n), "what": label, "script": pathlib.Path(script).name,
                "exit": rc, "seconds": round((dt.datetime.now() - t).total_seconds())})
    if rc != 0:
        if optional:
            print(f"  [WARN] {pathlib.Path(script).name} exited {rc} - continuing")
            return False
        print(f"\n[FATAL] {pathlib.Path(script).name} failed (exit {rc}). Stopping.")
        sys.stdout.flush()
        sys.exit(3)
    return True


def check_data(data: pathlib.Path, rules: pathlib.Path) -> None:
    """Stop early if the rules need a master the data folder does not hold."""
    import yaml
    cfg = yaml.safe_load(rules.read_text(encoding="utf-8")) or {}
    need = []
    if (cfg.get("fourrc_dip") or {}).get("enabled"):
        f = (cfg.get("fourrc_dip") or {}).get("frc_file") or "CTP_Master_FRC.csv"
        if not (data / f).exists():
            need.append(f"{f} (4-roll calender dip lengths, rule fourrc_dip)")
    for key, val in (("bypass_cut_code_types_from", cfg.get("bypass_cut_code_types_from")),
                     ("wip.bypass_conversion_bom",
                      (cfg.get("wip") or {}).get("bypass_conversion_bom"))):
        if val and not pathlib.Path(str(val)).is_absolute() and not (data / str(val)).exists():
            need.append(f"{val}/ (pre-v2 BOM, rule {key})")
    if need:
        sys.exit("[FATAL] the data folder lacks what the rules need:\n    " + "\n    ".join(need)
                 + "\n  run again with --restage")


def stage_masters(data: pathlib.Path, day, snaps: dict, env: dict) -> None:
    """Every master from JK_CTP_PPDS + the chosen stock and GT snapshots."""
    data.mkdir(parents=True, exist_ok=True)
    w, g = snaps["wip"], snaps["gt"]
    e = dict(env, CTP_WIP_DATE=w["inventory_date"], CTP_WIP_ASOF=w["sync"],
             CTP_GT_DATE=g["pcr"]["inventory_date"], CTP_GT_ASOF=g["pcr"]["sync"],
             CTP_DATA=str(data), CTP_STAGE=str(data))
    step("3a", "masters from JK_CTP_PPDS (BOM v2, routing, FRC, ageing, ... + stock / GT)",
         ENGINE / "refresh_ctp_sql.py", e,
         ["--ppds-only", "--bom-v2", "--out", str(data), "--skip", SKIP_TABLES])
    if g["tbr"]["sync"] != g["pcr"]["sync"]:
        step("3b", "TBR green tyres at their own snapshot", ENGINE / "refresh_ctp_sql.py",
             dict(e, CTP_GT_DATE=g["tbr"]["inventory_date"], CTP_GT_ASOF=g["tbr"]["sync"]),
             ["--ppds-only", "--bom-v2", "--out", str(data), "--only", "gt_inventory_manual_tbr"])
    for f in (ROOT / "reference" / "masters").glob("*.csv"):
        shutil.copy(f, data / f.name)                  # plant lists, not in the database
    step("3c", "opening stock: compound Batches -> KG", ENGINE / "stage_wip_ppds.py", e, [])
    (data / "_pipeline_stage.json").write_text(json.dumps({
        "plan_day": str(day), "wip_sync": w["sync"], "gt_sync": {k: v["sync"] for k, v in g.items()},
        "snapshots": snaps, "staged_at": dt.datetime.now().isoformat(timespec="seconds"),
        "database": os.environ.get("CTP_PPDS_NAME"),
        "tables": {"bom": "CTP_Master_BOM_v2", "reference_bom": "CTP_Master_BOM",
                   "routing": "CTP_Master_Routing", "wip": "CTP_Opening_WIP",
                   "gt": "CTP_GT_Inventory", "frc": "CTP_Master_FRC",
                   "plan": [plan_from_db.BUILD_T, plan_from_db.CURE_T]}}, indent=2, default=str),
        encoding="utf-8")


def _pairs(s: str | None) -> dict:
    """'PCR=20261028_162333,TBR=...' -> {'PCR': ..., 'TBR': ...}"""
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def write_summary(out: pathlib.Path, run: str, day, plan_info: dict, snaps: dict,
                  warnings: list, pub: dict | None, checks: list | None = None,
                  sid: dict | None = None, val: dict | None = None) -> int:
    L = [f"# Run summary - {run}", "",
         f"Plan day **{day}** · engine {sid['version'] if sid else 'v46k'} · generated "
         f"{dt.datetime.now():%Y-%m-%d %H:%M}", "",
         f"**schedule_id `{sid['schedule_id']}`** (CTP_Schedule_Floor.schedule_id = "
         f"`{sid['db_schedule_id']}`)" if sid else "", "",
         "## Inputs", "", "| What | Source | Value |", "|---|---|---|"]
    for ln, i in plan_info["lines"].items():
        L.append(f"| {ln} plan | {plan_from_db.BUILD_T} / {plan_from_db.CURE_T} | run_id "
                 f"`{i['run_id']}` ({i['plan_id']}), {i['building_tyres']:,} tyres to build on "
                 f"{i['building_machines']} TBMs, {i['curing_tyres']:,} to cure on "
                 f"{i['curing_presses']} presses, {i['first_start']} -> {i['last_end']} |")
    L.append(f"| Stock | CTP_Opening_WIP | sync {snaps['wip']['sync']} ({snaps['wip']['rows']:,} rows, "
             f"{snaps['wip']['chosen_by']}) |")
    for ln in ("pcr", "tbr"):
        s = snaps["gt"][ln]
        L.append(f"| GT {ln.upper()} | CTP_GT_Inventory | sync {s['sync']} ({s['rows']} rows, "
                 f"{s['chosen_by']}) |")
    if checks:
        L += ["", "## Pre-flight checks", "", "| Status | Id | Check | Detail |", "|---|---|---|---|"]
        L += [f"| {c['status']} | {c['id']} | {c['check']} | {c['detail']} |" for c in checks]
    nf = 0
    v = out / "VALIDATION.csv"
    if v.exists():
        d = pd.read_csv(v, encoding="utf-8-sig")
        nf = int((d["verdict"] == "FAIL").sum())
        L += ["", "## Schedule checks", "", f"{len(d) - nf} of {len(d)} passed.", ""]
        L += [f"- FAIL {r.line} {r.test}: {r.detail}" for r in d[d["verdict"] == "FAIL"].itertuples()]
    f = out / "FULFILMENT_DAYWISE_v15.csv"
    if f.exists():
        d = pd.read_csv(f, encoding="utf-8-sig")
        L += ["", "## Fulfilment (data-blocked SKUs excluded)", "",
              "| Line | Day | Built | Build plan | Built % | Cured | Cure plan | Cured % |",
              "|---|---|---|---|---|---|---|---|"]
        for r in d.itertuples(index=False):
            L.append(f"| {r.line} | {r.day} | {r.built:,} | {r.build_plan:,} | {r.built_pct:.1f} | "
                     f"{r.cured:,} | {r.cure_plan:,} | {r.cured_pct:.1f} |")
    if warnings:
        L += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    if pub is not None:
        L += ["", "## Schedule persisted to the database", "",
              f"- table `{pub['table']}`, schedule_id `{pub['db_schedule_id']}` ({pub['schedule_id']})",
              f"- rows prepared {pub.get('prepared', 0):,}: "
              + ", ".join(f"{k} {v:,}" for k, v in sorted((pub.get('prepared_by_department') or {}).items(),
                                                          key=lambda x: str(x[0]))),
              (f"- **written**: {pub.get('inserted', 0):,} rows inserted and committed" if pub.get("written")
               else f"- **not written**: {pub.get('error', '')}")]
        if val:
            L += [f"- read back {val['rows_in_db']:,} rows; missing {val['missing_in_db']}, unexpected "
                  f"{val['unexpected_in_db']}, duplicates {val['duplicates_in_db']}; field mismatches vs "
                  f"the floor schedule {sum(val['floor_vs_prepared_mismatches'].values())}; stages "
                  f"{', '.join(val['stages_present'])} -> **{'PASS' if val['passed'] else 'FAIL'}**"]
        L += ["- details: `db_persist/` (rows_prepared.csv, persist_result.json, validation_result.json)"]
    L += ["", "## Steps", "", "| Step | What | Script | Exit | Seconds |", "|---|---|---|---|---|"]
    L += [f"| {s['step']} | {s['what']} | {s['script']} | {s['exit']} | {s['seconds']} |" for s in LOG]
    L += ["", "## Where to look", "",
          "- `VALIDATION_REPORT.xlsx` - blockers, assumptions, schedule gates",
          f"- `CTP_{run}_SCHEDULE.xlsx` - the plant schedule book",
          "- `phase5_floor_schedule_{pcr,tbr}.csv` - the shift-wise floor schedule",
          "- `analysis/` - day-1 starts, shift balance, built-not-cured, extruder idle",
          "- `plan_from_db/` - the plan rows exactly as read from the database"]
    pt = PHASED / "reports" / (sid["schedule_id"] if sid else "") / "result.json"
    if sid and pt.exists():
        res = json.loads(pt.read_text(encoding="utf-8"))
        L += ["", "## Phase-wise test reports", "",
              f"`phased-testing/reports/{sid['schedule_id']}/` (SUMMARY.md + TEST_CASES_PHASE1..7.md) - "
              f"**{'all phases OK' if res['overall_ok'] else 'NOT OK'}**", "",
              "| Phase | Name | PASS | WARN | FAIL | ERROR | Verdict |", "|---|---|---|---|---|---|---|"]
        for n, x in res["phases"].items():
            L.append(f"| {n} | {x['name']} | {x.get('PASS', 0)} | {x.get('WARN', 0)} | {x.get('FAIL', 0)} | "
                     f"{x.get('ERROR', 0)} | {'OK' if x['ok'] else 'NOT OK'} |")
    (out / "RUN_SUMMARY.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return nf


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily CTP schedule from the database (v46k engine).")
    ap.add_argument("--date", default=None,
                    help="plant day YYYY-MM-DD (default: the current plant day - 06:30 day 1 to "
                         "06:29 day 2 is day 1)")
    ap.add_argument("--run", default=None, help="run name (default v46k_db_<day>)")
    ap.add_argument("--plan-run-id", default=None,
                    help="pin the planner run, e.g. PCR=20261028_162333,TBR=20261028_163653")
    ap.add_argument("--wip-sync", default=None, help="explicit CTP_Opening_WIP updated_at to use")
    ap.add_argument("--gt-sync-pcr", default=None, help="explicit CTP_GT_Inventory PCR updated_at")
    ap.add_argument("--gt-sync-tbr", default=None, help="explicit CTP_GT_Inventory TBR updated_at")
    ap.add_argument("--min-snapshot-ratio", type=float, default=0.5,
                    help="a snapshot below this share of the usual row count is refused")
    ap.add_argument("--restage", action="store_true", help="stage the masters again")
    ap.add_argument("--rules", default=str(ROOT / "config" / "rules.yaml"))
    ap.add_argument("--out", default=str(ROOT / "outputs"), help="output root")
    ap.add_argument("--skip-workbooks", action="store_true")
    ap.add_argument("--skip-analysis", action="store_true")
    ap.add_argument("--no-db-write", action="store_true",
                    help="prepare and check the CTP_Schedule_Floor rows but write nothing")
    ap.add_argument("--test-history-plan", action="store_true",
                    help="TEST ONLY: read the plan from CTP_Schedule_*Plan_History. Needs "
                         "--plan-run-id for BOTH lines; never used by a normal run")
    ap.add_argument("--check-only", action="store_true",
                    help="run the pre-flight checks only, then stop")
    a = ap.parse_args()

    started = dt.datetime.now()
    if a.test_history_plan:
        pr = _pairs(a.plan_run_id)
        if not (pr.get("PCR") and pr.get("TBR")):
            sys.exit("[FATAL] --test-history-plan needs --plan-run-id PCR=<run_id>,TBR=<run_id> "
                     "(explicit planner runs; no default, no fallback)")
        plan_from_db.use_history_tables_for_test()
    day = pd.Timestamp(a.date).date() if a.date else plant_day(started)
    day_how = (f"given with --date" if a.date else
               f"current plant day: started {started:%d %b %H:%M}, plant day runs 06:30 -> 06:29")
    rules = pathlib.Path(a.rules).resolve()
    if not rules.exists():
        sys.exit(f"[FATAL] rule file not found: {rules}")
    version = persist_floor.rules_version(rules)
    sid = persist_floor.make_schedule_id(started, version)       # once per run
    run = a.run or f"{version}_db_{day}"
    out_root = pathlib.Path(a.out).resolve()
    out = out_root / run
    plan_dir = out_root / "_plans" / run
    out.mkdir(parents=True, exist_ok=True)
    (out / "SCHEDULE_ID.json").write_text(json.dumps(sid, indent=2), encoding="utf-8")
    sys.stdout = Tee(out / "logs" / f"run_{dt.datetime.now():%Y%m%d_%H%M%S}.log")
    db.load_db_env()
    env = dict(os.environ, CTP_PLAN_DIR=str(plan_dir), CTP_RULES=str(rules), CTP_OUT=str(out_root),
               PYTHONIOENCODING="utf-8")

    print("=" * 78)
    print(f"  CTP DAILY RUN FROM THE DATABASE   [{run}]   plan day {day}   "
          f"started {started:%Y-%m-%d %H:%M:%S}")
    print(f"  plan day {day} ({day_how}) - plans, 06:30 stock and 06:30 GT of {day}")
    print(f"  schedule_id {sid['schedule_id']}  (CTP_Schedule_Floor.schedule_id {sid['db_schedule_id']})")
    if a.test_history_plan:
        print(f"  *** TEST RUN: plan read from {plan_from_db.BUILD_T} / {plan_from_db.CURE_T}, "
              f"runs {a.plan_run_id} ***")
    print("=" * 78)

    # ---- 0. pre-flight: are the day's tables there? ------------------------
    pins = _pairs(a.plan_run_id)
    gto = {"pcr": a.gt_sync_pcr, "tbr": a.gt_sync_tbr}
    checks = preflight.run(day, pins, a.wip_sync, gto, a.min_snapshot_ratio)
    print("\n" + preflight.show(checks, day))
    preflight.save(checks, day, run, out, ROOT / "logs")
    if preflight.failed(checks):
        print(f"\n  NOT RUN. Checks written to {out / 'PREFLIGHT_CHECKS.md'}")
        sys.stdout.flush()
        return 2
    if a.check_only:
        print("\n  --check-only: stopping after the pre-flight checks.")
        return 0

    # ---- 1-2. plan and snapshots ------------------------------------------
    cn = db.connect()
    try:
        print(f"\n  [1] plan from {plan_from_db.BUILD_T} / {plan_from_db.CURE_T}")
        plan_info = plan_from_db.stage(cn, day, plan_dir, out / "plan_from_db", pins)
        for ln, i in plan_info["lines"].items():
            print(f"      {ln}: run_id {i['run_id']} ({i['plan_id']}) - build {i['building_tyres']:,} "
                  f"tyres / {i['building_machines']} TBMs, cure {i['curing_tyres']:,} / "
                  f"{i['curing_presses']} presses, {i['first_start']} -> {i['last_end']}")
        for n in plan_info["notes"]:
            print(f"      note: {n}")
        print(f"\n  [2] stock and green-tyre snapshots")
        snaps = stock_snapshot.choose(cn, day, a.wip_sync, gto, a.min_snapshot_ratio)
    finally:
        cn.close()
    print(f"      stock  : CTP_Opening_WIP {snaps['wip']['sync']} ({snaps['wip']['rows']:,} rows; "
          f"{snaps['wip']['chosen_by']})")
    for ln in ("pcr", "tbr"):
        s = snaps["gt"][ln]
        print(f"      GT {ln.upper()} : CTP_GT_Inventory {s['sync']} ({s['rows']} rows; {s['chosen_by']})")
    for w in snaps["warnings"]:
        print(f"      [WARN] {w}")

    # ---- 3. masters --------------------------------------------------------
    data = ROOT / "data" / f"ppds_{day}"
    stamp = data / "_pipeline_stage.json"
    want = {"wip": snaps["wip"]["sync"], "gt": {k: v["sync"] for k, v in snaps["gt"].items()}}
    have = json.loads(stamp.read_text(encoding="utf-8")) if stamp.exists() else {}
    if have and not a.restage and have.get("wip_sync") == want["wip"] \
            and have.get("gt_sync") == want["gt"]:
        print(f"\n  [3] masters: {data} (staged {have['staged_at']} with the same snapshots; "
              f"reused - --restage to refresh)")
    else:
        stage_masters(data, day, snaps, env)
    check_data(data, rules)
    if stamp.exists():                      # the run folder knows its inputs from here on
        shutil.copy(stamp, out / "DATA_STAGED.json")
    env.update(CTP_DATA=str(data), CTP_STAGE=str(data))
    for h in (ROOT / "reference" / "plan_helpers").glob("*.csv"):
        shutil.copy(h, plan_dir / h.name)

    # ---- 4-5. stock split, scheduler --------------------------------------
    step("4a", "this plan's demand (phases 0-1) for the stock split", ENGINE / "run_all.py", env,
         ["--run", run, "--only", "phase0", "phase1"])
    step("4b", "opening stock split PCR / TBR by that demand", ENGINE / "stage_wip_v6.py", env,
         ["--split-from", run, "--also-bom", "reference_bom", "--cut-to-cutting-line"])
    step(5, "scheduler: phases 0-7, both lines", ENGINE / "run_all.py", env, ["--run", run])

    # ---- 6-10. outputs -----------------------------------------------------
    step(6, "floor schedule, split at shift changes", ENGINE / "make_floor_schedule_v10.py", env,
         ["--run", run])
    print(f"\n{'=' * 78}\n  [6b] production per shift (only what is made inside each shift)\n{'=' * 78}")
    sys.path.insert(0, str(ANALYSIS))
    import _common as AC
    os.environ["CTP_OUT"] = str(out_root)
    t0, t_end = AC.plan_window(out)
    shift_tables = shift_production.build(out, run, t0, t_end)
    print(shift_production.show(shift_tables))
    LOG.append({"step": "6b", "what": "production per shift", "script": "shift_production.py",
                "exit": 0, "seconds": 0})
    step("7a", "validation against the routing", ENGINE / "validate_floor_schedule.py", env,
         ["--run", run], optional=True)
    step("7b", "machine coverage", ENGINE / "make_machine_coverage.py", env, ["--run", run], optional=True)
    step("7c", "business-rule compliance", ENGINE / "br_compliance_v15.py", env, ["--run", run],
         optional=True)
    step("7d", "mixing-rule compliance", ENGINE / "mixing_compliance.py", env, ["--run", run],
         optional=True)
    if not a.skip_workbooks:
        step("8a", "plant schedule book", ENGINE / "make_ctp_schedule_workbook.py", env,
             ["--run", run, "--out", f"CTP_{run}_SCHEDULE.xlsx"], optional=True)
        step("8b", "per-line schedule + sums book", ENGINE / "make_e2e_workbook_v13.py", env,
             ["--run", run], optional=True)
    step("9a", "fulfilment, day by day", ENGINE / "make_fulfilment_report.py", env, ["--run", run],
         optional=True)
    step("9b", "VALIDATION_REPORT", ENGINE / "make_validation_report.py", env, ["--run", run])
    step("10a", "bypass (cut stock) report", ENGINE / "make_bypass_report.py", env, ["--run", run],
         optional=True)
    step("10b", "stock present vs consumed", ENGINE / "make_wip_presence_report.py", env,
         ["--run", run], optional=True)
    step("10c", "day-1 stock vs building", ENGINE / "make_day1_wip_building_report.py", env,
         ["--run", run], optional=True)

    # ---- 11. analysis ------------------------------------------------------
    if not a.skip_analysis:
        step("11a", "analysis: does every machine start in day-1 shift A?",
             ANALYSIS / "all_machines_day1_start.py", env, ["--run", run], optional=True, cwd=ANALYSIS)
        step("11b", "analysis: shift load balance (engine view, idle causes)",
             ANALYSIS / "shift_load_balance.py", env, ["--run", run], optional=True, cwd=ANALYSIS)
        step("11c", "analysis: shift load balance (published floor schedule)",
             ANALYSIS / "shift_balance_from_floor.py", env, ["--run", run], optional=True, cwd=ANALYSIS)
        step("11d", "analysis: green tyres built but not cured",
             ANALYSIS / "built_not_cured.py", env, ["--run", run], optional=True, cwd=ANALYSIS)
        step("11e", "analysis: extruder idle (1001 triplex, 1801 duplex)",
             ENGINE / "make_machine_idle_report.py", env, ["--run", run, "--machines", "1001,1801"],
             optional=True)

    # ---- 12. persist to CTP_Schedule_Floor ----------------------------------
    print(f"\n{'=' * 78}\n  [12] complete schedule -> JK_CTP_PPDS.{persist_floor.TABLE}"
          f"{' (NO WRITE: --no-db-write)' if a.no_db_write else ''}\n{'=' * 78}")
    t12 = dt.datetime.now()
    val = None
    try:
        pub = persist_floor.persist(out, data, sid, replace=False, execute=not a.no_db_write)
        if pub.get("written") or pub.get("existing_before"):
            print("\n  read-back validation:")
            val = persist_floor.validate(out, sid)
    except Exception as e:                                   # never hides a finished schedule
        pub = {"table": persist_floor.TABLE, "schedule_id": sid["schedule_id"],
               "db_schedule_id": sid["db_schedule_id"], "written": False,
               "error": f"{type(e).__name__}: {e}"}
        print(f"  [FAIL] persistence error: {pub['error']}")
    db_ok = bool(a.no_db_write or (pub.get("written") and val and val.get("passed")))
    LOG.append({"step": "12", "what": f"persist to {persist_floor.TABLE}",
                "script": "persist_floor.py", "exit": 0 if db_ok else 4,
                "seconds": round((dt.datetime.now() - t12).total_seconds())})

    # ---- 13. phase-wise test reports ---------------------------------------
    step(13, "phase-wise test reports (phased-testing/reports/<schedule_id>/)",
         PHASED / "phase_tests.py", env, ["--run", run, "--out", str(out_root)], optional=True, cwd=ROOT)

    # ---- 14. summary -------------------------------------------------------
    (out / "RULES_USED.txt").write_text(str(rules), encoding="utf-8")
    if stamp.exists():
        shutil.copy(stamp, out / "DATA_STAGED.json")
    try:
        sys.path.insert(0, str(ENGINE))
        os.environ.update(CTP_OUT=str(out_root))
        import blockers as BLK
        BLK.merge_run(out_root, run)
    except Exception as e:                                   # report only
        print(f"  [WARN] blocker report unavailable: {type(e).__name__}: {e}")
    warns = plan_info["notes"] + snaps["warnings"] + \
        [f"step {s['step']} ({s['script']}) exited {s['exit']}" for s in LOG if s["exit"]]
    nf = write_summary(out, run, day, plan_info, snaps, warns, pub, checks, sid, val)
    print(f"\n{'=' * 78}\n  RESULT   [{run}]\n{'=' * 78}")
    f = out / "FULFILMENT_DAYWISE_v15.csv"
    if f.exists():
        d = pd.read_csv(f, encoding="utf-8-sig")
        t = d[d["day"] == "TOTAL"]
        for r in t.itertuples(index=False):
            print(f"  {r.line:4s} built {r.built:>8,} / {r.build_plan:<8,} ({r.built_pct:>5.1f}%)   "
                  f"cured {r.cured:>8,} / {r.cure_plan:<8,} ({r.cured_pct:>5.1f}%)")
        if len(t):
            print(f"  BOTH cured {t['cured'].sum():,} / {t['cure_plan'].sum():,} "
                  f"({100 * t['cured'].sum() / max(t['cure_plan'].sum(), 1):.1f}%)")
    print("\n" + shift_production.show(shift_tables))
    print(f"\n  schedule_id {sid['schedule_id']}: "
          + ("NOT written to the database (--no-db-write)" if a.no_db_write else
             f"{pub.get('inserted', 0):,} rows in {persist_floor.TABLE}, read-back "
             f"{'PASS' if val and val.get('passed') else 'FAIL'}" if pub.get("written") else
             f"NOT written: {pub.get('error')}"))
    print(f"  phase tests : {PHASED / 'reports' / sid['schedule_id']}   (SUMMARY.md)")
    print(f"\n  outputs : {out}   (start with RUN_SUMMARY.md)")
    if not db_ok:
        return 4
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
