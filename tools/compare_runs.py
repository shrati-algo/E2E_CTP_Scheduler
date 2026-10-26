#!/usr/bin/env python3
"""compare_runs.py - prove two runs produced the same schedule.

    python tools/compare_runs.py <run_dir_A> <run_dir_B> [--report out.csv]

Compares every CSV under both run folders (recursively), plus the key sheets of
VALIDATION_REPORT.xlsx. A file is IDENTICAL when it has the same columns, the same
number of rows and the same values (numbers equal to 1e-6 relative). Files that
legitimately carry run-specific text (paths, timestamps) are listed as EXPECTED
DIFFERENCE and their content is shown so the reader can confirm it.

Exit code 0 when every schedule file is identical, 1 otherwise.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import pandas as pd

# files whose differences are only run identity (paths, run names, timestamps)
RUN_SPECIFIC = {"RULES_USED.txt"}
RUN_SPECIFIC_SHEETS = {"Run stamp"}


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    return df


def _equal(a: pd.DataFrame, b: pd.DataFrame) -> tuple[bool, str]:
    if list(a.columns) != list(b.columns):
        return False, f"columns differ: {sorted(set(a.columns) ^ set(b.columns))[:6]}"
    if len(a) != len(b):
        return False, f"rows {len(a)} vs {len(b)}"
    bad = []
    for c in a.columns:
        x, y = a[c], b[c]
        xn, yn = pd.to_numeric(x, errors="coerce"), pd.to_numeric(y, errors="coerce")
        if xn.notna().sum() == x.notna().sum() and yn.notna().sum() == y.notna().sum() \
                and x.notna().sum() > 0:
            same = np.isclose(xn.fillna(0).to_numpy(), yn.fillna(0).to_numpy(),
                              rtol=1e-6, atol=1e-9) & (xn.isna().to_numpy() == yn.isna().to_numpy())
        else:
            same = (x.astype(str).fillna("") == y.astype(str).fillna("")).to_numpy()
        if not same.all():
            bad.append(f"{c} ({int((~same).sum())} cells)")
    return (not bad), ("; ".join(bad[:5]) if bad else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    A, B = pathlib.Path(args.a), pathlib.Path(args.b)
    fa = {p.relative_to(A).as_posix() for p in A.rglob("*.csv")}
    fb = {p.relative_to(B).as_posix() for p in B.rglob("*.csv")}
    rows = []
    for f in sorted(fa | fb):
        if f not in fa or f not in fb:
            rows.append({"file": f, "verdict": "MISSING", "detail": "only in " + ("A" if f in fa else "B")})
            continue
        try:
            da = _norm(pd.read_csv(A / f, encoding="utf-8-sig", low_memory=False))
            db = _norm(pd.read_csv(B / f, encoding="utf-8-sig", low_memory=False))
        except pd.errors.EmptyDataError:
            same = (A / f).read_bytes() == (B / f).read_bytes()
            rows.append({"file": f, "verdict": "IDENTICAL" if same else "DIFFERENT", "detail": "empty file"})
            continue
        ok, why = _equal(da, db)
        rows.append({"file": f, "verdict": "IDENTICAL" if ok else "DIFFERENT", "rows": len(da), "detail": why})
    # key workbook sheets
    for wb in ("VALIDATION_REPORT.xlsx",):
        if (A / wb).exists() and (B / wb).exists():
            xa, xb = pd.ExcelFile(A / wb), pd.ExcelFile(B / wb)
            for sh in xa.sheet_names:
                if sh not in xb.sheet_names:
                    rows.append({"file": f"{wb}:{sh}", "verdict": "MISSING", "detail": "sheet only in A"})
                    continue
                ok, why = _equal(_norm(pd.read_excel(xa, sh)), _norm(pd.read_excel(xb, sh)))
                v = "IDENTICAL" if ok else ("EXPECTED DIFFERENCE" if sh in RUN_SPECIFIC_SHEETS else "DIFFERENT")
                rows.append({"file": f"{wb}:{sh}", "verdict": v, "detail": why})
    R = pd.DataFrame(rows)
    if args.report:
        R.to_csv(args.report, index=False)
    print(R["verdict"].value_counts().to_string())
    bad = R[R["verdict"].isin(["DIFFERENT", "MISSING"])]
    if len(bad):
        print("\nnot identical:")
        print(bad.to_string(index=False))
    exp = R[R["verdict"] == "EXPECTED DIFFERENCE"]
    if len(exp):
        print("\nexpected differences (run identity only):")
        print(exp.to_string(index=False))
    print(f"\n{len(R)} items compared; {int((R['verdict'] == 'IDENTICAL').sum())} identical.")
    return 1 if len(bad) else 0


if __name__ == "__main__":
    sys.exit(main())
