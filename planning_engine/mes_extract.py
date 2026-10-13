#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mes_extract.py -- STAGE A.1: stream the June MES production files into a compact cache.

The MES exports are huge (curing 55 MB, TBM stage-2 34 MB, stage-1 consumption 213 MB) and are
re-read by every reverse-engineering / validation pass, so they are streamed ONCE with openpyxl
read_only and cached as parquet.  Nothing here interprets the data -- it only lifts the columns
the planner needs, normalises types, and records what it dropped.

Sources (OneDrive MES export, June 2026 PCR):
  curingpcr june month_.xlsx            -- one row per CURE event (per cavity)
      iD, wcID, gtbarCode, pressbarCode, mouldNo, dtandTime, cycleStart, cycleUpdate,
      MouldCodeLH/RH, BladderCodeLH/RH, ...
  tbmpcrstage2 o_production june month.xlsx  -- one row per GREEN TYRE completed on a TBM
      machineName, productionID (== curing.gtbarCode), itemCode (GT code), dtandTime
  tbmpcrstage1 o_production june month.xlsx  -- one row per carcass completed (stage 1)

THE JOIN THAT MAKES THIS POSSIBLE:
      tbmpcrstage2.productionID  ==  curing.gtbarCode
  i.e. every physical green tyre carries a barcode from the building drum into the press. That
  single key yields, per tyre: which TBM built it, when, which GT/SKU it is, which press+mould
  cured it, and when -- which is the whole build->cure interlock the planner has to reproduce.

Usage:
    python -m planning_engine.mes_extract [--mes-dir DIR] [--force]
Writes to planning_engine/_cache/:
    cure_events.parquet, build_events.parquet, build_stage1.parquet, extract_report.txt
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"
# The MES export lives outside the scheduler package (OneDrive drop). Overridable via --mes-dir
# or CTP_MES_DIR so the engine is not tied to one machine's layout.
DEFAULT_MES_DIR = Path(
    os.environ.get(
        "CTP_MES_DIR",
        r"C:\Users\91810\Desktop\rev scheduler ctp\OneDrive_1_19-7-2026",
    )
)

CURING_FILE = "curingpcr june month_.xlsx"
STAGE2_FILE = "tbmpcrstage2 o_production june month.xlsx"
STAGE1_FILE = "tbmpcrstage1 o_production june month.xlsx"


def _stream_sheet(path: Path, want: list[str], chunk: int = 200_000):
    """Yield DataFrames of `want` columns from the first worksheet of an xlsx, streaming.

    openpyxl read_only keeps memory flat; pandas.read_excel would materialise the whole sheet.
    Missing columns raise immediately -- a silently absent column would corrupt every downstream
    derivation, so it must fail loud here.
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.values
        hdr = [str(h).strip() if h is not None else "" for h in next(rows)]
        missing = [c for c in want if c not in hdr]
        if missing:
            raise KeyError(f"{path.name}: missing columns {missing}; header was {hdr}")
        idx = [hdr.index(c) for c in want]
        while True:
            block = list(itertools.islice(rows, chunk))
            if not block:
                break
            yield pd.DataFrame(
                [[r[i] if i < len(r) else None for i in idx] for r in block], columns=want
            )
    finally:
        wb.close()


def _to_dt(s: pd.Series) -> pd.Series:
    """MES timestamps arrive as datetime OR as 'YYYY-MM-DD HH:MM:SS.ffffff' strings."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    return pd.to_datetime(s.astype(str).str.strip(), errors="coerce")


def _barcode(s: pd.Series) -> pd.Series:
    """Barcodes are zero-padded numeric strings; Excel sometimes hands them back as floats."""
    out = s.astype(str).str.strip()
    out = out.str.replace(r"\.0$", "", regex=True)
    return out.str.upper()


def extract_curing(mes_dir: Path, report: list[str]) -> pd.DataFrame:
    want = [
        "iD", "wcID", "gtbarCode", "pressbarCode", "mouldNo", "dtandTime",
        "cycleStart", "cycleUpdate", "MouldCodeLH", "MouldCodeRH",
    ]
    parts, n_raw = [], 0
    t0 = time.time()
    for df in _stream_sheet(mes_dir / CURING_FILE, want):
        n_raw += len(df)
        df["gtbarCode"] = _barcode(df["gtbarCode"])
        press_raw = df["pressbarCode"].astype(str).str.strip().str.upper()
        # 'pressbarCode' is press number + cavity side, e.g. '4831RHS' / '4831LHS'. A press has two
        # cavities that cure independently, so keep the side -- press capacity is counted in
        # cavities, not presses.
        df["press"] = press_raw.str.replace(r"(LHS|RHS)$", "", regex=True).str.strip()
        df["cavity"] = press_raw.str.extract(r"(LHS|RHS)$", expand=False).fillna("")
        df["cure_time"] = _to_dt(df["dtandTime"])
        df["cycle_start"] = _to_dt(df["cycleStart"])
        df["cycle_update"] = _to_dt(df["cycleUpdate"])
        parts.append(
            df[["iD", "wcID", "gtbarCode", "press", "cavity", "mouldNo",
                "MouldCodeLH", "MouldCodeRH", "cure_time", "cycle_start", "cycle_update"]]
        )
        print(f"    curing… {n_raw:,} rows", end="\r", flush=True)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    n_bad_t = int(out["cure_time"].isna().sum())
    n_bad_b = int((out["gtbarCode"].isin(["", "NAN", "NONE"])).sum())
    out = out[out["cure_time"].notna()].reset_index(drop=True)
    report.append(
        f"curing   : {n_raw:,} raw rows -> {len(out):,} kept "
        f"({n_bad_t:,} unparseable cure_time dropped, {n_bad_b:,} blank barcodes flagged) "
        f"in {time.time()-t0:.0f}s"
    )
    return out


def extract_build(mes_dir: Path, fname: str, label: str, report: list[str]) -> pd.DataFrame:
    want = ["iD", "machineName", "productionID", "itemCode", "ItemName", "quantity", "dtandTime"]
    parts, n_raw = [], 0
    t0 = time.time()
    for df in _stream_sheet(mes_dir / fname, want):
        n_raw += len(df)
        df["gtbarCode"] = _barcode(df["productionID"])
        df["build_time"] = _to_dt(df["dtandTime"])
        df["qty"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(1.0)
        # 'TBMPCR10Stage2' -> machine number 10, so it can be matched to the plan's buildMachine.
        df["tbm"] = (df["machineName"].astype(str).str.strip()
                     .str.extract(r"TBMPCR\s*(\d+)", expand=False))
        df["item"] = df["itemCode"].astype(str).str.strip()
        parts.append(df[["iD", "machineName", "tbm", "gtbarCode", "item", "ItemName",
                         "qty", "build_time"]])
        print(f"    {label}… {n_raw:,} rows", end="\r", flush=True)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    n_bad = int(out["build_time"].isna().sum())
    out = out[out["build_time"].notna()].reset_index(drop=True)
    report.append(
        f"{label:9s}: {n_raw:,} raw rows -> {len(out):,} kept ({n_bad:,} unparseable build_time "
        f"dropped) in {time.time()-t0:.0f}s"
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Cache the June MES exports as parquet.")
    ap.add_argument("--mes-dir", default=str(DEFAULT_MES_DIR))
    ap.add_argument("--force", action="store_true", help="re-extract even if the cache exists")
    ap.add_argument("--skip-stage1", action="store_true",
                    help="stage-1 carcass output is only needed for TBM stage balance")
    a = ap.parse_args()

    mes_dir = Path(a.mes_dir)
    if not mes_dir.is_dir():
        print(f"ERROR: MES dir not found: {mes_dir}", file=sys.stderr)
        return 2
    CACHE.mkdir(parents=True, exist_ok=True)

    jobs = [
        ("cure_events.parquet", lambda r: extract_curing(mes_dir, r)),
        ("build_events.parquet", lambda r: extract_build(mes_dir, STAGE2_FILE, "stage2", r)),
    ]
    if not a.skip_stage1:
        jobs.append(
            ("build_stage1.parquet", lambda r: extract_build(mes_dir, STAGE1_FILE, "stage1", r))
        )

    report: list[str] = [f"MES extract from: {mes_dir}"]
    for name, fn in jobs:
        dst = CACHE / name
        if dst.exists() and not a.force:
            report.append(f"{name}: cached ({dst.stat().st_size/1e6:.1f} MB) -- use --force to redo")
            print(f"  {name}: cached, skipping")
            continue
        print(f"  extracting {name} …")
        df = fn(report)
        df.to_parquet(dst, index=False)
        print(f"  wrote {dst}  ({len(df):,} rows, {dst.stat().st_size/1e6:.1f} MB)")

    (CACHE / "extract_report.txt").write_text("\n".join(report), encoding="utf-8")
    print("\n".join("  " + line for line in report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
