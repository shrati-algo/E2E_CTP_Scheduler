#!/usr/bin/env python3
"""run_pipeline.py - one command for a complete CTP end-to-end scheduling run.

    python run_pipeline.py --plan-folder plans/october_2026 --data data/ppds_snapshot \
                           --run oct26

INPUTS
  --plan-folder  a folder holding the plan workbooks/CSVs. Files are found by
                 name (case-insensitive):
                     *building*pcr*   PCR building plan      (required)
                     *building*tbr*   TBR building plan      (required)
                     *curing*pcr*     PCR curing plan        (optional)
                     *curing*tbr*     TBR curing plan        (optional)
                 If a curing plan is absent it is derived from the building plan.
  --data         the staged masters folder (PPDS CSV extracts: BOM, routing,
                 changeover matrix, opening WIP, GT inventory, ...). Not kept in git;
                 see data/README.md. The v46e rules need it staged with
                 `refresh_ctp_sql.py --ppds-only --bom-v2`: BOM = CTP_Master_BOM_v2,
                 reference_bom/ = the pre-v2 BOM, and CTP_Master_FRC.csv.
  --rules        rule file (default config/rules.yaml = the resolved v46e rules)
  --out          output root (default ./outputs); results go to <out>/<run>/

WHAT IT DOES, IN ORDER
  0  stage      plan files -> <out>/_plans/<run>/{pcr,tbr}/pe_format/*.xlsx
  1  curing     derive the curing anchor (only if no curing plan was given)
  2  pipeline   engine/run_all.py  phases 0-7, both lines (joint upstream)
  3  floor      the plant's 16-column floor schedule
  4  validate   checks against the routing
  5  coverage   every register machine + its hours
  6  rules      business-rule compliance, mixing-rule compliance
  7  workbooks  the schedule workbooks
  8  fulfilment day-wise built / cured
  9  report     VALIDATION_REPORT.xlsx (data blockers, assumptions, schedule gates)
 10  bypass     BYPASS_REPORT.xlsx (cut stock -> mother roll, transfer-time checks)

EXIT CODE
  0 if the pipeline ran and every routing validation check passed, 1 otherwise
  (the same convention as the original run.py).
"""
from __future__ import annotations

import argparse
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


def main() -> int:
    ap = argparse.ArgumentParser(description="CTP end-to-end scheduler (v46e engine).")
    ap.add_argument("--plan-folder", required=True, help="folder with the plan files")
    ap.add_argument("--data", required=True, help="staged masters folder (PPDS extracts)")
    ap.add_argument("--run", required=True, help="run name, e.g. oct26")
    ap.add_argument("--rules", default=str(ROOT / "config" / "rules.yaml"))
    ap.add_argument("--out", default=str(ROOT / "outputs"), help="output root")
    ap.add_argument("--max-presses", type=int, default=12,
                    help="curing anchor (only when no curing plan is given)")
    ap.add_argument("--skip-workbooks", action="store_true")
    a = ap.parse_args()

    plan_folder = pathlib.Path(a.plan_folder).resolve()
    data = pathlib.Path(a.data).resolve()
    rules = pathlib.Path(a.rules).resolve()
    out_root = pathlib.Path(a.out).resolve()
    for p, what in ((plan_folder, "plan folder"), (data, "data folder"), (rules, "rule file")):
        if not p.exists():
            sys.exit(f"[FATAL] {what} not found: {p}")
    plan_dir = out_root / "_plans" / a.run
    files = find_plan_files(plan_folder)
    check_data(data, rules)

    print("=" * 78)
    print(f"  CTP END-TO-END RUN   [{a.run}]")
    print("=" * 78)
    print(f"  plan folder : {plan_folder}")
    for (k, ln), f in files.items():
        print(f"      {k:8s} {ln.upper()}: {f.name if f else '(none - derived)'}")
    print(f"  masters     : {data}")
    print(f"  rules       : {rules}")
    print(f"  outputs     : {out_root / a.run}")

    # ---- 0. stage the plans ------------------------------------------------
    print(f"\n{'=' * 78}\n  [0] staging the plans\n{'=' * 78}")
    for line in ("pcr", "tbr"):
        i = stage(plan_dir, line, files[("building", line)], files[("curing", line)])
        print(f"  {line.upper()}: {i['production_rows']:,} production rows ({i['rows']:,} raw), "
              f"{i['tyres']:,.0f} tyres, {i['skus']} SKUs, {i['machines']} machines; "
              f"curing plan: {i['curing']}")
    for h in (ROOT / "reference" / "plan_helpers").glob("*.csv"):
        shutil.copy(h, plan_dir / h.name)

    env = dict(os.environ)
    env.update(CTP_DATA=str(data), CTP_STAGE=str(data), CTP_PLAN_DIR=str(plan_dir),
               CTP_RULES=str(rules), CTP_OUT=str(out_root), PYTHONIOENCODING="utf-8")

    # ---- 1..9 --------------------------------------------------------------
    if not (files[("curing", "pcr")] and files[("curing", "tbr")]):
        step(1, "deriving the curing anchor from the building plan",
             "make_curing_plan.py", env, ["--max-presses", str(a.max_presses)])
    else:
        print("\n  [1] curing plan supplied - not derived")
    step(2, "the nine phases, both lines", "run_all.py", env, ["--run", a.run])
    step(3, "floor schedule (the plant's 16 columns)", "make_floor_schedule_v10.py", env,
         ["--run", a.run])
    step(4, "validation against the routing", "validate_floor_schedule.py", env,
         ["--run", a.run], optional=True)
    step(5, "machine coverage against the plant register", "make_machine_coverage.py", env,
         ["--run", a.run], optional=True)
    step(6, "business-rule compliance", "br_compliance_v15.py", env, ["--run", a.run],
         optional=True)
    step(6, "mixing-rule compliance", "mixing_compliance.py", env, ["--run", a.run],
         optional=True)
    if not a.skip_workbooks:
        step(7, "workbook - the plant's 6-sheet book", "make_ctp_schedule_workbook.py", env,
             ["--run", a.run, "--out", f"CTP_{a.run}_SCHEDULE.xlsx"], optional=True)
        step(7, "workbook - schedule + sums check, per line", "make_e2e_workbook_v13.py", env,
             ["--run", a.run], optional=True)
    step(8, "fulfilment, day by day", "make_fulfilment_report.py", env, ["--run", a.run],
         optional=True)
    step(9, "VALIDATION_REPORT - data blockers, assumptions, schedule gates",
         "make_validation_report.py", env, ["--run", a.run])
    step(10, "BYPASS_REPORT - cut stock converted, transfer time across cutters/slitters",
         "make_bypass_report.py", env, ["--run", a.run], optional=True)

    # ---- summary -----------------------------------------------------------
    out = out_root / a.run
    out.mkdir(parents=True, exist_ok=True)
    (out / "RULES_USED.txt").write_text(str(rules), encoding="utf-8")
    try:
        sys.path.insert(0, str(ENGINE))
        os.environ.update(CTP_OUT=str(out_root))
        import blockers as BLK
        bp = BLK.merge_run(out_root, a.run)
        bd = pd.read_csv(bp, encoding="utf-8-sig")
        print(f"\n  DATA BLOCKERS: {len(bd):,} row(s) -> {bp.name}")
    except Exception as e:                                   # report only
        print(f"  [WARN] blocker report unavailable: {type(e).__name__}: {e}")

    print(f"\n{'=' * 78}\n  RESULT   [{a.run}]\n{'=' * 78}")
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
    print(f"\n  outputs : {out}   (start with VALIDATION_REPORT.xlsx)")
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
