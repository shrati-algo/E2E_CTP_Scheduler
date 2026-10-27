#!/usr/bin/env python3
"""run_pipeline.py - one command for a complete CTP end-to-end scheduling run (v46k).

    python run_pipeline.py --plan-folder "C:/path/to/plan-27oct"

That is all it needs. Everything else is taken from the plant database
(JK_CTP_PPDS) for the plan's own start day:

INPUTS
  --plan-folder  a folder holding the plan workbooks/CSVs, found by name
                 (case-insensitive):
                     *building*pcr*   PCR building plan      (required)
                     *building*tbr*   TBR building plan      (required)
                     *curing*pcr*     PCR curing plan        (optional)
                     *curing*tbr*     TBR curing plan        (optional)
                 If a curing plan is absent it is derived from the building plan.
  --run          run name (default: v46k_<plan start date>)
  --data         OPTIONAL: an already staged masters folder. Without it the
                 masters are staged from the database into data/ppds_<plan day>/
                 (reused on the next run of the same day unless --restage).
  --restage      stage the masters again even if data/ppds_<plan day>/ exists
  --rules        rule file (default config/rules.yaml = the resolved v46k rules)
  --out          output root (default ./outputs); results go to <out>/<run>/

DATABASE (read-only)
  Credentials come ONLY from environment variables CTP_DB_HOST, CTP_DB_PORT,
  CTP_DB_USER, CTP_DB_PASSWORD, CTP_PPDS_NAME - or from a local
  file `db.env` in the repository root (KEY=VALUE lines, git-ignored; see
  db.env.example). Nothing is ever written to the database.

WHAT IT DOES, IN ORDER
  A  plan start   the earliest StartTime of the building plans -> plan day
  B  stock syncs  CTP_Opening_WIP and CTP_GT_Inventory: the shift-A (06:30)
                  sync of the plan day - the floor as the plan's first shift opens
  C  masters      engine/refresh_ctp_sql.py --ppds-only --bom-v2: BOM =
                  CTP_Master_BOM_v2, routing = CTP_Master_Routing, reference_bom/ =
                  CTP_Master_BOM, CTP_Master_FRC, ageing, buffer, MPQ, changeover,
                  machine master, allowable machines, holidays, downtime, + the
                  stock and GT syncs of B; then compound Batches -> KG
                  (stage_wip_ppds.py); plant lists from reference/masters/
  0  plans        plan files -> <out>/_plans/<run>/{pcr,tbr}/pe_format/*.xlsx
  1  curing       derive the curing anchor (only if no curing plan was given)
  S  stock split  phases 0-1 give this plan's demand; the stock is split
                  PCR / TBR by it (stage_wip_v6.py --also-bom reference_bom
                  --cut-to-cutting-line)
  2  pipeline     engine/run_all.py phases 0-7, both lines (joint upstream)
  3..10           floor schedule, validation, coverage, rule compliance,
                  workbooks, fulfilment, VALIDATION_REPORT, BYPASS_REPORT
  11, 12          stock present vs consumed; day-1 stock vs building

EXIT CODE
  0 if the pipeline ran and every routing validation check passed, 1 otherwise.
  The two machine-coverage findings (bypassed cutters / slitters idle) are known.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
NEED = ["Machine", "SKUCode", "Qty", "StartTime"]
DB_KEYS = ("CTP_DB_HOST", "CTP_DB_PORT", "CTP_DB_USER", "CTP_DB_PASSWORD",
           "CTP_DB_NAME", "CTP_PPDS_NAME")
# staged by refresh_ctp_sql.py but read by no phase (5.7 M rows) - left out
SKIP_TABLES = "CTP_Live_Inventory,CTP_Live_Inventory_Raw"


# --------------------------------------------------------------------- input
def find_plan_files(folder: pathlib.Path) -> dict:
    """Pick the four plan files out of a folder by their names."""
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".csv")
             and not p.name.startswith("~$")]
    out = {}
    for kind in ("building", "curing"):
        for line in ("pcr", "tbr"):
            hits = [p for p in files
                    if kind in p.name.lower() and re.search(rf"(^|[^a-z]){line}([^a-z]|$)",
                                                            p.name.lower())]
            if len(hits) > 1:
                sys.exit(f"[FATAL] more than one {kind} plan for {line.upper()} in "
                         f"{folder}: {[h.name for h in hits]}")
            out[(kind, line)] = hits[0] if hits else None
    for line in ("pcr", "tbr"):
        if out[("building", line)] is None:
            sys.exit(f"[FATAL] no {line.upper()} building plan in {folder} "
                     f"(expected a file name containing 'building' and '{line}')")
    return out


def _find_header(probe: pd.DataFrame) -> int:
    """The header row is FOUND, not assumed - exports vary in title rows."""
    want = [re.sub(r"[^a-z0-9]", "", n.lower()) for n in NEED]
    for i in range(min(12, len(probe))):
        cells = {re.sub(r"[^a-z0-9]", "", str(v).lower()) for v in probe.iloc[i]}
        if sum(1 for w in want if w in cells) >= 3:
            return i
    return 0


def read_plan(path: pathlib.Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        import openpyxl
        names = openpyxl.load_workbook(path, read_only=True).sheetnames
        sheet = "Shift Schedule" if "Shift Schedule" in names else 0
        probe = pd.read_excel(path, sheet_name=sheet, header=None, nrows=12)
        d = pd.read_excel(path, sheet_name=sheet, header=_find_header(probe))
    else:
        probe = pd.read_csv(path, header=None, nrows=12, dtype=str,
                            encoding="utf-8-sig", on_bad_lines="skip")
        d = pd.read_csv(path, header=_find_header(probe), encoding="utf-8-sig")
    d.columns = [str(c).strip() for c in d.columns]
    missing = [c for c in NEED if c not in d.columns]
    if missing:
        sys.exit(f"[FATAL] {path.name}: header found but missing {missing}")
    d = d[d["Machine"].notna() & d["SKUCode"].notna()].copy()
    d["Qty"] = pd.to_numeric(d["Qty"], errors="coerce").fillna(0.0)
    return d


def stage(plan_dir: pathlib.Path, line: str, src: pathlib.Path,
          curing: pathlib.Path | None) -> dict:
    out = plan_dir / line / "pe_format"
    out.mkdir(parents=True, exist_ok=True)
    d = read_plan(src)
    prod = d[d["Qty"] > 0]
    with pd.ExcelWriter(out / f"ctp_building_schedule_plan_{line}.xlsx", engine="openpyxl") as w:
        d.to_excel(w, sheet_name="Shift Schedule", index=False)
    info = {"rows": len(d), "production_rows": len(prod), "tyres": float(prod["Qty"].sum()),
            "skus": int(prod["SKUCode"].nunique()), "machines": int(prod["Machine"].nunique()),
            "curing": "derived"}
    if curing:
        c = read_plan(curing) if curing.suffix.lower().startswith(".x") else \
            pd.read_csv(curing, encoding="utf-8-sig")
        with pd.ExcelWriter(out / f"ctp_curing_schedule_plan_{line}.xlsx", engine="openpyxl") as w:
            c.to_excel(w, sheet_name="Shift Schedule", index=False)
        info["curing"] = f"supplied ({len(c):,} rows)"
    return info


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
        sys.exit("[FATAL] the data folder lacks what the rules need:\n    "
                 + "\n    ".join(need)
                 + "\n  stage it with: cd engine && python refresh_ctp_sql.py --ppds-only "
                   "--bom-v2 --out <data folder>   (see data/README.md)")


# ------------------------------------------------------------------- driver
def step(n, label, script, env, args, optional=False) -> bool:
    print(f"\n{'=' * 78}\n  [{n}] {label}\n{'=' * 78}", flush=True)
    r = subprocess.run([sys.executable, script, *args], cwd=ENGINE, env=env, text=True)
    if r.returncode != 0:
        if optional:
            print(f"  [WARN] {script} exited {r.returncode} - continuing")
            return False
        sys.exit(f"\n[FATAL] {script} failed (exit {r.returncode}). Stopping.")
    return True




# ----------------------------------------------------------------- database
def load_db_env() -> None:
    """CTP_DB_* from the environment; a local db.env fills only what is unset."""
    f = ROOT / "db.env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in DB_KEYS and v and not os.environ.get(k):
                os.environ[k] = v
    os.environ.setdefault("CTP_PPDS_NAME", "JK_CTP_PPDS")
    os.environ.setdefault("CTP_DB_PORT", "3306")


def db_ready() -> list:
    # every table is read from CTP_PPDS_NAME (--ppds-only); CTP_DB_NAME is not needed
    return [k for k in ("CTP_DB_HOST", "CTP_DB_USER", "CTP_DB_PASSWORD")
            if not os.environ.get(k)]


def plan_day(files: dict) -> tuple:
    """(first build start, plant day). A plant day runs 07:00 -> 07:00."""
    t = []
    for line in ("pcr", "tbr"):
        d = read_plan(files[("building", line)])
        d = d[d["Qty"] > 0]
        t.append(pd.to_datetime(d["StartTime"], errors="coerce").min())
    start = min(x for x in t if pd.notna(x))
    return start, (start - pd.Timedelta(hours=7)).date()


def _ts(x) -> str:
    return pd.Timestamp(x).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def shift_a_syncs(day) -> dict:
    """The shift-A (06:30) syncs of CTP_Opening_WIP and CTP_GT_Inventory for `day`."""
    sys.path.insert(0, str(ENGINE))
    import ctpsql
    cn = ctpsql.connect(os.environ["CTP_PPDS_NAME"])
    try:
        w = ctpsql.fetch("SELECT shift, updated_at, COUNT(*) AS n FROM CTP_Opening_WIP "
                         f"WHERE inventory_date = '{day}' GROUP BY shift, updated_at "
                         "ORDER BY updated_at", cn)
        g = ctpsql.fetch("SELECT tyre_type, shift, updated_at, COUNT(*) AS n FROM "
                         f"CTP_GT_Inventory WHERE inventory_date = '{day}' "
                         "GROUP BY tyre_type, shift, updated_at ORDER BY updated_at", cn)
    finally:
        cn.close()
    wa = w[w["shift"].astype(str).str.strip().str.upper() == "A"]
    if wa.empty:
        sys.exit(f"[FATAL] CTP_Opening_WIP has no shift-A sync on {day} (the plan start day).\n"
                 f"  syncs that day: {w[['shift', 'updated_at', 'n']].to_dict('records')}")
    out = {"wip": _ts(wa["updated_at"].iloc[0]), "wip_rows": int(wa["n"].iloc[0]), "gt": {}}
    for line in ("PCR", "TBR"):
        ga = g[(g["tyre_type"].astype(str).str.upper() == line)
               & (g["shift"].astype(str).str.strip().str.upper() == "A")]
        if ga.empty:
            sys.exit(f"[FATAL] CTP_GT_Inventory has no {line} shift-A sync on {day}.\n"
                     f"  syncs that day: {g.to_dict('records')}")
        out["gt"][line.lower()] = _ts(ga["updated_at"].iloc[0])
    return out


def stage_masters(data: pathlib.Path, day, syncs: dict, env: dict) -> None:
    """Every master from JK_CTP_PPDS + the plan day's shift-A stock and GT."""
    data.mkdir(parents=True, exist_ok=True)
    e = dict(env, CTP_WIP_DATE=str(day), CTP_WIP_ASOF=syncs["wip"],
             CTP_GT_DATE=str(day), CTP_GT_ASOF=syncs["gt"]["pcr"],
             CTP_DATA=str(data), CTP_STAGE=str(data))
    step("C1", "masters from JK_CTP_PPDS (BOM v2, routing, FRC, ageing, ... + stock / GT)",
         "refresh_ctp_sql.py", e,
         ["--ppds-only", "--bom-v2", "--out", str(data), "--skip", SKIP_TABLES])
    if syncs["gt"]["tbr"] != syncs["gt"]["pcr"]:
        step("C1b", "TBR green tyres at their own shift-A sync", "refresh_ctp_sql.py",
             dict(e, CTP_GT_ASOF=syncs["gt"]["tbr"]),
             ["--ppds-only", "--bom-v2", "--out", str(data), "--only", "gt_inventory_manual_tbr"])
    for f in (ROOT / "reference" / "masters").glob("*.csv"):
        shutil.copy(f, data / f.name)                  # plant lists, not in the database
    step("C2", "opening stock: compound Batches -> KG", "stage_wip_ppds.py", e, [])
    (data / "_pipeline_stage.json").write_text(json.dumps({
        "plan_day": str(day), "wip_sync": syncs["wip"], "gt_sync": syncs["gt"],
        "staged_at": dt.datetime.now().isoformat(timespec="seconds"),
        "database": os.environ.get("CTP_PPDS_NAME"),
        "tables": {"bom": "CTP_Master_BOM_v2", "reference_bom": "CTP_Master_BOM",
                   "routing": "CTP_Master_Routing", "wip": "CTP_Opening_WIP",
                   "gt": "CTP_GT_Inventory", "frc": "CTP_Master_FRC"}}, indent=2),
        encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="CTP end-to-end scheduler (v46k engine).")
    ap.add_argument("--plan-folder", required=True, help="folder with the plan files")
    ap.add_argument("--run", default=None, help="run name (default v46k_<plan day>)")
    ap.add_argument("--data", default=None,
                    help="optional pre-staged masters folder (skips the database)")
    ap.add_argument("--restage", action="store_true",
                    help="stage the masters again even if data/ppds_<plan day> exists")
    ap.add_argument("--rules", default=str(ROOT / "config" / "rules.yaml"))
    ap.add_argument("--out", default=str(ROOT / "outputs"), help="output root")
    ap.add_argument("--max-presses", type=int, default=12,
                    help="curing anchor (only when no curing plan is given)")
    ap.add_argument("--skip-workbooks", action="store_true")
    a = ap.parse_args()

    plan_folder = pathlib.Path(a.plan_folder).expanduser().resolve()
    rules = pathlib.Path(a.rules).resolve()
    out_root = pathlib.Path(a.out).resolve()
    for p, what in ((plan_folder, "plan folder"), (rules, "rule file")):
        if not p.exists():
            sys.exit(f"[FATAL] {what} not found: {p}")
    out_root.mkdir(parents=True, exist_ok=True)
    files = find_plan_files(plan_folder)
    start, day = plan_day(files)
    run = a.run or f"v46k_{day}"
    plan_dir = out_root / "_plans" / run
    load_db_env()

    env = dict(os.environ)
    env.update(CTP_PLAN_DIR=str(plan_dir), CTP_RULES=str(rules), CTP_OUT=str(out_root),
               PYTHONIOENCODING="utf-8")

    print("=" * 78)
    print(f"  CTP END-TO-END RUN   [{run}]")
    print("=" * 78)
    print(f"  plan folder : {plan_folder}")
    for (k, ln), f in files.items():
        print(f"      {k:8s} {ln.upper()}: {f.name if f else '(none - derived)'}")
    print(f"  plan start  : {start}   -> plan day {day} (stock as at shift A, 06:30)")
    print(f"  rules       : {rules}")
    print(f"  outputs     : {out_root / run}")

    # ---- A-C. masters + the plan day's stock ---------------------------------
    if a.data:
        data = pathlib.Path(a.data).resolve()
        if not data.exists():
            sys.exit(f"[FATAL] data folder not found: {data}")
        print(f"  masters     : {data}   (given - nothing read from the database)")
    else:
        data = ROOT / "data" / f"ppds_{day}"
        stamp = data / "_pipeline_stage.json"
        if stamp.exists() and not a.restage:
            s = json.loads(stamp.read_text(encoding="utf-8"))
            print(f"  masters     : {data}   (staged {s['staged_at']}; reused - --restage to refresh)")
        else:
            miss = db_ready()
            if miss:
                sys.exit("[FATAL] database credentials missing: " + ", ".join(miss) +
                         "\n  set them as environment variables or in db.env (see db.env.example)")
            syncs = shift_a_syncs(day)
            print(f"  stock sync  : CTP_Opening_WIP {syncs['wip']} ({syncs['wip_rows']:,} rows)")
            print(f"  GT sync     : CTP_GT_Inventory PCR {syncs['gt']['pcr']}, TBR {syncs['gt']['tbr']}")
            print(f"  masters     : {data}   (staging now from {os.environ['CTP_PPDS_NAME']})")
            stage_masters(data, day, syncs, env)
    check_data(data, rules)
    env.update(CTP_DATA=str(data), CTP_STAGE=str(data))

    # ---- 0. stage the plans ------------------------------------------------
    print(f"\n{'=' * 78}\n  [0] staging the plans\n{'=' * 78}")
    for line in ("pcr", "tbr"):
        i = stage(plan_dir, line, files[("building", line)], files[("curing", line)])
        print(f"  {line.upper()}: {i['production_rows']:,} production rows ({i['rows']:,} raw), "
              f"{i['tyres']:,.0f} tyres, {i['skus']} SKUs, {i['machines']} machines; "
              f"curing plan: {i['curing']}")
    for h in (ROOT / "reference" / "plan_helpers").glob("*.csv"):
        shutil.copy(h, plan_dir / h.name)

    # ---- 1. curing anchor ----------------------------------------------------
    if not (files[("curing", "pcr")] and files[("curing", "tbr")]):
        step(1, "deriving the curing anchor from the building plan",
             "make_curing_plan.py", env, ["--max-presses", str(a.max_presses)])
    else:
        print("\n  [1] curing plan supplied - not derived")

    # ---- S. split the stock PCR / TBR by this plan's own demand -------------
    step("S1", "this plan's demand (phases 0-1) for the stock split", "run_all.py", env,
         ["--run", run, "--only", "phase0", "phase1"])
    step("S2", "opening stock split PCR / TBR by that demand", "stage_wip_v6.py", env,
         ["--split-from", run, "--also-bom", "reference_bom", "--cut-to-cutting-line"])

    # ---- 2..12 ---------------------------------------------------------------
    step(2, "the nine phases, both lines", "run_all.py", env, ["--run", run])
    step(3, "floor schedule (the plant's 16 columns)", "make_floor_schedule_v10.py", env,
         ["--run", run])
    step(4, "validation against the routing", "validate_floor_schedule.py", env,
         ["--run", run], optional=True)
    step(5, "machine coverage against the plant register", "make_machine_coverage.py", env,
         ["--run", run], optional=True)
    step(6, "business-rule compliance", "br_compliance_v15.py", env, ["--run", run],
         optional=True)
    step(6, "mixing-rule compliance", "mixing_compliance.py", env, ["--run", run],
         optional=True)
    if not a.skip_workbooks:
        step(7, "workbook - the plant's 6-sheet book", "make_ctp_schedule_workbook.py", env,
             ["--run", run, "--out", f"CTP_{run}_SCHEDULE.xlsx"], optional=True)
        step(7, "workbook - schedule + sums check, per line", "make_e2e_workbook_v13.py", env,
             ["--run", run], optional=True)
    step(8, "fulfilment, day by day", "make_fulfilment_report.py", env, ["--run", run],
         optional=True)
    step(9, "VALIDATION_REPORT - data blockers, assumptions, schedule gates",
         "make_validation_report.py", env, ["--run", run])
    step(10, "BYPASS_REPORT - cut stock converted, transfer time across cutters/slitters",
         "make_bypass_report.py", env, ["--run", run], optional=True)
    step(11, "WIP_PRESENCE_VS_CONSUMPTION - opening stock present vs used",
         "make_wip_presence_report.py", env, ["--run", run], optional=True)
    step(12, "DAY1_WIP_VS_BUILDING - day-1 trolleys, kit from stock, fresh parts, waits",
         "make_day1_wip_building_report.py", env, ["--run", run], optional=True)

    # ---- summary -----------------------------------------------------------
    out = out_root / run
    out.mkdir(parents=True, exist_ok=True)
    (out / "RULES_USED.txt").write_text(str(rules), encoding="utf-8")
    st = data / "_pipeline_stage.json"
    if st.exists():
        shutil.copy(st, out / "DATA_STAGED.json")
    try:
        sys.path.insert(0, str(ENGINE))
        os.environ.update(CTP_OUT=str(out_root))
        import blockers as BLK
        bp = BLK.merge_run(out_root, run)
        bd = pd.read_csv(bp, encoding="utf-8-sig")
        print(f"\n  DATA BLOCKERS: {len(bd):,} row(s) -> {bp.name}")
    except Exception as e:                                   # report only
        print(f"  [WARN] blocker report unavailable: {type(e).__name__}: {e}")

    print(f"\n{'=' * 78}\n  RESULT   [{run}]\n{'=' * 78}")
    if st.exists():
        s = json.loads(st.read_text(encoding="utf-8"))
        print(f"  stock / GT  : CTP_Opening_WIP {s['wip_sync']};  CTP_GT_Inventory {s['gt_sync']}")
    v = out / "VALIDATION.csv"
    nf = 0
    if v.exists():
        d = pd.read_csv(v, encoding="utf-8-sig")
        nf = int((d["verdict"] == "FAIL").sum())
        print(f"  routing validation : {len(d) - nf} of {len(d)} checks passed"
              + ("" if nf == 0 else f"   [{nf} FAILED]"))
        for r in d[d["verdict"] == "FAIL"].itertuples(index=False):
            print(f"      FAIL {r.line} {r.test}: {r.detail}")
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
    print(f"\n  outputs : {out}   (start with VALIDATION_REPORT.xlsx)")
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
