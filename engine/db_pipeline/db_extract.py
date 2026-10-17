#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_extract.py — pull jkplanning_CTP straight into the scheduler's inputs/.

    python db_pipeline/db_extract.py                 # core inputs -> inputs/  (+ raw snapshot)
    python db_pipeline/db_extract.py --raw-only      # snapshot only, do NOT touch inputs/
    python db_pipeline/db_extract.py --extra         # also pull the not-yet-wired tables
    python db_pipeline/db_extract.py --skip plan     # leave inputs/plan.csv alone

WHY A RAW SNAPSHOT TOO: every run also writes the untouched table to
db_pipeline/raw/<table>.csv plus a manifest (row counts + timestamp). The DB moves; when a
schedule looks wrong six weeks later, that snapshot is the only way to prove WHICH data
produced it. Sizing/aging bugs here are silent, so provenance is not optional.

!! PLAN, READ THIS BEFORE USING --include plan:
   CuringSchedule_pcr covers 2026-04-01..04-29 while the shipped run uses a JUNE shift
   sheet. Pulling `plan` therefore RESCHEDULES A DIFFERENT MONTH -- it is EXCLUDED by
   default. Pass --include plan deliberately.
"""
from __future__ import annotations
import argparse
import json
import os
import pathlib
import sys
import time

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent                       # ctp_v6_v2/
INPUTS = ROOT / "inputs"
RAW = HERE / "raw"
sys.path.insert(0, str(HERE))

from db_connect import read_table, list_tables, load_env      # noqa: E402
from table_map import CORE, EXTRA, for_product                # noqa: E402
import adapters                                               # noqa: E402

# 2026-08-01: pull the product's own tables. CTP_PRODUCT=tbr swaps every _pcr table for its
# _tbr twin (shared tables unchanged) via table_map.for_product — so a TBR run snapshots and
# adapts the TBR masters instead of silently pulling PCR.
PRODUCT = os.environ.get("CTP_PRODUCT", "pcr").strip().lower()

# inputs/ filename for each scheduler key (must match config.yaml `files:`)
OUT_NAME = {
    "bom": "bom.csv", "routing": "routing.csv", "plan": "plan.csv",
    "itemtype_master": "itemtype_master.csv", "aging_master": "aging_master.csv",
    "mpq": "mpq.csv", "buffer_master": "buffer_master.csv",
    "inventory": "inventory.csv", "changeover": "changeover_matrix.csv",
    # EXTRA (opt-in via --extra): real plant per-TBM building setup times, replacing the
    # flat 40/60 fallback in phase5. Adapter maps MachineCode/Same/Different -> the csv phase5 reads.
    "building_changeover": "building_changeover.csv",
}
# `plan` is opt-in: see the module docstring.
DEFAULT_SKIP = {"plan"}


def extract(keys, write_inputs=True, raw=True):
    RAW.mkdir(parents=True, exist_ok=True)
    if write_inputs:
        INPUTS.mkdir(parents=True, exist_ok=True)
    manifest, t0 = [], time.time()

    cycle_df = None
    if "plan" in keys:
        try:
            cycle_df = read_table(for_product("Master_Curing_Design_CycleTime_pcr", PRODUCT))
        except Exception as e:
            print(f"  [WARN] cure-time table unavailable ({e!r}); cycleTime from runTime/qty only")

    for key in keys:
        spec = CORE.get(key) or EXTRA.get(key)
        if spec is None:
            print(f"  [SKIP] {key}: not in table_map"); continue
        table = for_product(spec["table"], PRODUCT)
        try:
            df = read_table(table)
        except Exception as e:
            print(f"  [FAIL] {key:<24} {table:<44} {type(e).__name__}: {str(e)[:90]}")
            manifest.append({"key": key, "table": table, "status": "FAILED", "error": str(e)[:200]})
            continue

        if raw:
            df.to_csv(RAW / f"{table}.csv", index=False)

        note = ""
        if write_inputs and key in OUT_NAME:
            out = adapters.adapt(key, df, cycle_df=cycle_df) if key == "plan" \
                else adapters.adapt(key, df)
            dest = INPUTS / OUT_NAME[key]
            out.to_csv(dest, index=False)
            note = f"-> inputs/{OUT_NAME[key]} ({len(out):,} rows)"
            if len(out) != len(df):
                note += f"  [adapter changed row count {len(df):,} -> {len(out):,}]"
        print(f"  [OK]   {key:<24} {table:<44} {len(df):>8,} rows  {note}")
        manifest.append({"key": key, "table": table, "db_rows": len(df),
                         "db_cols": len(df.columns), "written": note, "status": "OK"})

    env = load_env()
    (HERE / "extract_manifest.json").write_text(json.dumps({
        "database": env["DB_NAME"], "host": env["DB_HOST"],
        "extracted_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "elapsed_sec": round(time.time() - t0, 1),
        "tables": manifest,
    }, indent=2), encoding="utf-8")
    print(f"\n  manifest -> {HERE / 'extract_manifest.json'}")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-only", action="store_true", help="snapshot only; do not write inputs/")
    ap.add_argument("--extra", action="store_true", help="also pull the not-yet-wired tables")
    ap.add_argument("--include", nargs="*", default=[], help="force-include a skipped key (e.g. plan)")
    ap.add_argument("--skip", nargs="*", default=[], help="skip these keys")
    ap.add_argument("--list", action="store_true", help="just list the DB tables and exit")
    a = ap.parse_args()

    if a.list:
        t = list_tables()
        print(f"{len(t)} tables"); print(t.to_string(index=False)); return 0

    keys = [k for k in CORE if k not in (DEFAULT_SKIP - set(a.include))]
    keys = [k for k in keys if k not in set(a.skip)]
    if a.extra:
        keys += list(EXTRA)

    env = load_env()
    print("=" * 78)
    print(f"  DB EXTRACT  {env['DB_NAME']} @ {env['DB_HOST']}")
    print("=" * 78)
    skipped = (DEFAULT_SKIP - set(a.include)) - set(a.skip)
    if skipped:
        print(f"  NOT pulled by default: {sorted(skipped)}  (CuringSchedule_pcr is APRIL data; "
              f"the shipped run is JUNE). Use --include plan to override.")
    extract(keys, write_inputs=not a.raw_only)
    print("\n  DONE." + ("" if a.raw_only else "  inputs/ now reflects the database."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
