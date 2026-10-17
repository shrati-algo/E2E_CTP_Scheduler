#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_scheduler.py — ONE command to run the CTP Chennai PCR end-to-end scheduler.

WHAT IT DOES
  1. (optional) pull the live plant masters from the jkplanning_CTP database
  2. run all 9 phases:  phase0 -> phase1b -> phase1_5 -> phase1c -> phase2
                        -> phase3 -> phase4 -> phase5 -> phase6
  3. save, into  output/latest/ :
        - CTP_FLOOR_SCHEDULE.xlsx     (the floor schedule, multi-sheet)
        - phase5_floor_schedule_updated.csv + key CSVs
        - PHASEWISE/                  (one folder per phase: 00_INPUTS, PHASE1B..PHASE6)
  4. run the health check (verify_run.py) and print ONE verdict.

TWO WAYS TO RUN
  A. LIVE DB (default) — pull ALL plant masters from jkplanning_CTP, adapt, then run.
     Needs db_pipeline/.env (credentials). The DEMAND DRUM (plan) stays the bundled JUNE
     shift sheet, because the DB's CuringSchedule_pcr is APRIL data (a different month).
     Add --db-plan to also pull the April plan (reschedules a different month — deliberate).
         python run_scheduler.py

  B. OFFLINE — run on the adapted CSVs already in inputs/ (the last DB pull, or the bundled
     snapshot). No database needed; reproduces whatever inputs/ currently holds.
         python run_scheduler.py --offline

USEFUL FLAGS
  --from phaseX     re-run only from a phase onward (reuses earlier outputs2/).
  --only phaseX     run just one phase.
  --validate-only   skip the run; just re-run the health check on the last output.
  --out DIR         write to DIR instead of output/latest.

Requires: pandas, openpyxl, pyyaml  (+ sqlalchemy, pymysql for --db).  See requirements.txt.
"""
from __future__ import annotations
import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
O2   = os.path.join(HERE, "outputs2")

# 2026-07-27 USER DIRECTIVE: no hardcoded SKUs. The workbook no longer carves out a fixed
# "WIP Curing" set -- every SKU is built from the DB BOM/routing. (A cure with no built green
# tyre still lands in the main sheet and is flagged by phase6's "CURED WITHOUT A GREEN TYRE".)
WIP_FED_SKUS = set()
FS_COLS = ["date", "machine", "shift", "item", "item_type", "process", "department",
           "start_time", "end_time", "produce_qty", "UOM", "lot_id",
           "prod_id", "consumption_id", "FG_SKU_CODE"]


def _sh(script, extra=(), cwd=HERE):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    print("\n" + "#" * 74 + f"\n#  {script} {' '.join(extra)}\n" + "#" * 74)
    r = subprocess.run([sys.executable, os.path.join(HERE, script), *extra], cwd=cwd, env=env)
    if r.returncode != 0:
        raise SystemExit(f"  {script} FAILED (exit {r.returncode}). See message above.")


def _floor_workbook(out_dir):
    """Write the clean multi-sheet floor-schedule workbook from outputs2/."""
    import pandas as pd
    fs = pd.read_csv(os.path.join(O2, "phase5_floor_schedule_updated.csv"))
    fs["dept_u"] = fs["department"].astype(str).str.strip().str.upper()
    fs["item_s"] = fs["item"].astype(str).str.strip()
    fs["s"] = pd.to_datetime(fs["start_time"], errors="coerce")
    is_cur = fs["dept_u"] == "CURING"
    is_wip = is_cur & fs["item_s"].isin(WIP_FED_SKUS)

    bom_path = os.path.join(HERE, "ctp_inputfiles", "jkt_bom_pcr 15.xlsx")
    is_inv = pd.Series(False, index=fs.index)
    if os.path.exists(bom_path):
        bom = pd.read_excel(bom_path, engine="openpyxl")
        for c in ("Super_parent", "child"):
            bom[c] = bom[c].astype(str).str.strip()
        bom["cd"] = bom["child_description"].astype(str).str.strip()
        sku2gt = {r["Super_parent"]: r["child"]
                  for _, r in bom[bom["cd"].isin(["Green Tyres", "GREEN_TYRE"])].iterrows()}
        first_build = fs[fs["dept_u"] == "BUILDING"].groupby("item")["s"].min()
        build_ready = fs["item_s"].map(sku2gt).map(first_build)
        is_inv = is_cur & ~is_wip & (fs["s"] < build_ready)

    _cols = [c for c in FS_COLS if c in fs.columns]   # tolerate an older floor CSV
    main = fs[~is_wip & ~is_inv][_cols].sort_values(["start_time", "machine"])
    inv  = fs[is_inv][_cols].sort_values(["start_time", "machine"])
    wip  = fs[is_wip][_cols].sort_values(["start_time", "machine"])

    def span(d):
        if not len(d):
            return ("-", "-", 0)
        s = pd.to_datetime(d["start_time"]); e = pd.to_datetime(d["end_time"])
        return (s.min(), e.max(), round((e.max() - s.min()).total_seconds() / 86400, 2))
    summ = pd.DataFrame(
        [["Floor Schedule (mfg + real curing)", len(main), *span(main)],
         ["Inventory GT Curing (from opening-WIP green tyres)", len(inv), *span(inv)],
         ["WIP Curing (green tyre never built)", len(wip), *span(wip)]],
        columns=["Sheet", "Rows", "First start", "Last finish", "Span (days)"])

    out = os.path.join(out_dir, "CTP_FLOOR_SCHEDULE.xlsx")
    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        summ.to_excel(xl, sheet_name="Summary", index=False)
        main.to_excel(xl, sheet_name="Floor Schedule", index=False)
        inv.to_excel(xl, sheet_name="Inventory GT Curing", index=False)
        wip.to_excel(xl, sheet_name="WIP Curing", index=False)
    print(f"  wrote {out}")
    print(summ.to_string(index=False))


def _save_outputs(out_dir):
    """Copy the floor schedule, key CSVs, and the PHASEWISE structure into out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    for f in ("phase5_floor_schedule_updated.csv", "phase5_schedule_updated.csv",
              "phase5_summary_updated.xlsx", "phase5_machine_utilization_updated.csv",
              "phase6_curing_revision_updated.csv", "phase4_aging_violations_updated.csv",
              "phase2_lots_updated.csv", "phase5_spool_consumed.csv", "phase5_aging_no_fresh_supply.csv",
              "phase5_30g_prep_runs.csv"):
        src = os.path.join(O2, f)
        if os.path.exists(src):
            shutil.copy2(src, out_dir)
    wip = os.path.join(HERE, "inputs", "inventory.csv")
    if os.path.exists(wip):
        shutil.copy2(wip, os.path.join(out_dir, "opening_wip_used.csv"))

    pw = os.path.join(out_dir, "PHASEWISE")
    layout = {
        "00_INPUTS":       [os.path.join(HERE, "inputs", x)
                            for x in os.listdir(os.path.join(HERE, "inputs")) if x.endswith(".csv")],
        "PHASE1B_DEMAND":  ["phase1_demand_updated.csv.gz"],
        "PHASE1_5_WAVES":  [os.path.join(HERE, "outputs", x) for x in
                            ("phase1_5_waves.csv", "phase1_5_block_to_wave.csv",
                             "phase1_5_wave_summary.xlsx")],
        "PHASE1C_NETTING": ["phase1_demand_NET_updated.csv.gz"],
        "PHASE2_LOTS":     ["phase2_lots_updated.csv", "phase2_lot_skus_updated.csv",
                            "phase2_bom_shortfall.csv", "phase2_lot_blocks_updated.csv.gz"],
        "PHASE3_DAG":      ["phase3_dag_edges_updated.csv", "phase3_lot_degrees_updated.csv",
                            "phase3_lot_blocks_updated.csv.gz", "phase3_lot_to_skus_updated.csv"],
        "PHASE4_CPM":      ["phase4_lot_times_updated.csv", "phase4_aging_violations_updated.csv",
                            "phase4_aest_window_by_itype_updated.csv"],
        "PHASE5_PLACEMENT":["phase5_floor_schedule_updated.csv", "phase5_schedule_updated.csv",
                            "phase5_machine_utilization_updated.csv", "phase5_summary_updated.xlsx"],
        "PHASE6_CURING":   ["phase6_curing_revision_updated.csv", "phase6_wip_reconciliation.csv",
                            "phase6_expired_opening_wip.csv"],
    }
    for folder, files in layout.items():
        d = os.path.join(pw, folder)
        os.makedirs(d, exist_ok=True)
        for f in files:
            src = f if os.path.isabs(f) else os.path.join(O2, f)
            if os.path.exists(src):
                shutil.copy2(src, d)
    print(f"\n  PHASEWISE output -> {pw}")
    _floor_workbook(out_dir)


def main():
    argv = sys.argv[1:]
    # DB is the DEFAULT source. Pass --offline (alias --no-db) to run on the CSVs in inputs/.
    offline    = ("--offline" in argv) or ("--no-db" in argv)
    db_plan    = "--db-plan" in argv
    val_only   = "--validate-only" in argv
    out_name   = "output/latest"
    if "--out" in argv:
        i = argv.index("--out"); out_name = argv[i + 1]; argv = argv[:i] + argv[i + 2:]
    for flag in ("--db", "--offline", "--no-db", "--db-plan", "--validate-only"):
        while flag in argv:
            argv.remove(flag)
    out_dir = out_name if os.path.isabs(out_name) else os.path.join(HERE, out_name)

    if val_only:
        _sh("verify_run.py", ["--run-dir", out_dir])
        return 0

    # 1. inputs — DEFAULT: live DB (extract masters -> adapt). --offline: the CSVs already in inputs/.
    if offline:
        print("\n=== OFFLINE RUN: using the adapted CSVs already in inputs/ (no DB) ===")
    else:
        print("\n=== LIVE DB: pulling all plant masters from jkplanning_CTP ===")
        print("    (masters from the DB; curing plan = LOCAL June shift sheet; MPQ = LOCAL file --")
        print("     both by user directive. Add --db-plan to pull the DB's APRIL CuringSchedule.)")
        _sh(os.path.join("db_pipeline", "db_extract.py"),
            ["--extra", "--skip", "mpq"] + (["--include", "plan"] if db_plan else []))
        _sh("adapt_inputs.py")
        # AGING from the PLANT spec file (CTE0.06-FR.02) overrides the DB aging (user directive 2026-08-03).
        _sh("apply_aging_spec.py")
        # MPQ from the LOCAL workbook overrides the DB MPQ (user directive 2026-07-27).
        _sh("apply_local_mpq.py")

    # 2. run all phases (pass through --from / --only)
    _sh("run_pipeline.py", argv)

    # 2b. static build-vs-cure reconciliation (report-only; skips if the plans aren't present).
    try:
        _sh("plan_reconciliation.py")
    except SystemExit as e:
        print(f"  [reconcile note: {e}]")

    # 2c. CAPACITY WALL (2026-08-06, report-only). Names any single UPSTREAM machine whose
    #     total work exceeds the 30-day plan window -- a hard makespan floor that sequencing
    #     cannot beat and only a parallel machine fixes. Printed every run so the bottleneck
    #     is stated up-front (e.g. 1901 RHC 39d / 2001 bias cutter 34d), not re-diagnosed.
    try:
        _sh("capacity_wall.py")
    except SystemExit as e:
        print(f"  [capacity-wall note: {e}]")

    # 3. save deliverables
    print("\n" + "#" * 74 + "\n#  SAVE OUTPUTS\n" + "#" * 74)
    _save_outputs(out_dir)

    print("\n" + "=" * 74)
    print(f"  DONE. Deliverables in:  {out_dir}")
    print("        - CTP_FLOOR_SCHEDULE.xlsx   (floor schedule, 4 sheets)")
    print("        - PHASEWISE/                (one folder per phase)")
    print("=" * 74)

    # 4. health check (never fails the run — it only reports)
    try:
        _sh("verify_run.py", ["--run-dir", out_dir])
    except SystemExit as e:
        print(f"  [health check note: {e}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
