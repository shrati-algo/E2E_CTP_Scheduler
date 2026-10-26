#!/usr/bin/env python3
"""blockers.py — the DATA BLOCKER register.

THE RULE THIS FILE ENFORCES: every number the engine uses comes from a named
database table. There is no fallback file, no rule-file constant, no invented
default. Where the database does not carry what a stage needs, the stage does
NOT quietly substitute something - it records a BLOCKER and carries on with
nothing, so the gap is visible in the output instead of hidden in the result.

A blocker says four things, and a plant engineer can act on each:

    WHERE   the stage and the exact call site that wanted the value
    WHAT    the database table it should have come from, and the key it looked up
    WHY     one of the kinds below
    COST    how much is affected - rows, items, or quantity - where it is countable

KINDS
    MISSING_IN_DB    the table has no row for this key at all
    NO_TABLE         the staged table itself is absent (never refreshed)
    UOM_MISMATCH     the row exists but its unit cannot be reconciled with the
                     unit the consumer works in, so applying it would be wrong
    NAME_MISMATCH    the row exists but under a different name than the BOM uses,
                     so an exact lookup misses it
    AMBIGUOUS        several rows match and they disagree, so no single value is
                     defensible
    DROPPED          rows present in the source were discarded by the pipeline
                     (expired, duplicated, orphaned) - with the reason
    STALE            the row exists and is used, but the snapshot is older than
                     the data it is being netted against

Nothing here changes a number. It only records that a number was not available.
"""
from __future__ import annotations

import pathlib

import pandas as pd

KINDS = ("NO_TABLE", "MISSING_IN_DB", "UOM_MISMATCH", "NAME_MISMATCH",
         "AMBIGUOUS", "DROPPED", "STALE")

REGISTER: list[dict] = []


def add(stage: str, kind: str, table: str, key: str, detail: str,
        line: str = "", referenced_at: str = "", qty=None, uom: str = "",
        impact: str = "") -> None:
    """Record one blocker. `table` is the DATABASE table it should have come from."""
    if kind not in KINDS:
        raise ValueError(f"unknown blocker kind {kind!r}; expected one of {KINDS}")
    REGISTER.append({
        "line": line, "stage": stage, "kind": kind, "db_table": table,
        "key": str(key), "uom": uom,
        "qty_affected": None if qty is None else float(qty),
        "detail": detail, "referenced_at": referenced_at, "impact": impact,
    })


def start(outdir: pathlib.Path, name: str = "DATA_BLOCKERS.csv") -> None:
    """Delete the per-line file so a fresh run does not inherit the last one."""
    p = pathlib.Path(outdir) / name
    if p.exists():
        p.unlink()


def count(kind: str | None = None) -> int:
    return len([r for r in REGISTER if kind is None or r["kind"] == kind])


def clear() -> None:
    REGISTER.clear()


def frame() -> pd.DataFrame:
    cols = ["line", "stage", "kind", "db_table", "key", "uom", "qty_affected",
            "detail", "referenced_at", "impact"]
    if not REGISTER:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(REGISTER)[cols]


def write(outdir: pathlib.Path, name: str = "DATA_BLOCKERS.csv",
          echo: bool = True) -> pathlib.Path:
    """Append this stage's register to the per-line file and print a summary.

    APPEND, not overwrite: each phase runs as its own process, so the register
    only ever holds what THAT phase found. Phase 0 starts the file (see
    `start`); every later phase adds to it. Identical rows are collapsed so a
    re-run of one phase does not duplicate its own findings.
    """
    d = frame()
    p = pathlib.Path(outdir) / name
    if p.exists():
        try:
            old = pd.read_csv(p, encoding="utf-8-sig")
            d = pd.concat([old, d], ignore_index=True).drop_duplicates()
        except Exception:
            pass
    d.to_csv(p, index=False, encoding="utf-8-sig")
    if echo:
        if d.empty:
            print("  DATA BLOCKERS   : none — every value came from its database table")
        else:
            print(f"  DATA BLOCKERS   : {len(d):,} — written to {name}")
            g = d.groupby(["kind", "db_table"]).agg(
                n=("key", "size"), items=("key", "nunique")).reset_index()
            for r in g.sort_values(["kind", "n"], ascending=[True, False]).itertuples(
                    index=False):
                print(f"      {r.kind:<14s} {r.db_table:<26s} "
                      f"{r.n:>5,d} row(s), {r.items:>4,d} distinct key(s)")
    return p


def merge_run(out_root: pathlib.Path, run: str,
              lines=("pcr", "tbr")) -> pathlib.Path:
    """Fold every per-line register of a run into one file at the run root."""
    parts = []
    for ln in lines:
        p = pathlib.Path(out_root) / run / ln / "DATA_BLOCKERS.csv"
        if p.exists():
            d = pd.read_csv(p, encoding="utf-8-sig")
            if len(d):
                d["line"] = d["line"].fillna(ln.upper()).replace("", ln.upper())
                parts.append(d)
    out = pathlib.Path(out_root) / run / "DATA_BLOCKERS.csv"
    d = (pd.concat(parts, ignore_index=True) if parts
         else pd.DataFrame(columns=["line", "stage", "kind", "db_table", "key",
                                    "uom", "qty_affected", "detail",
                                    "referenced_at", "impact"]))
    d.to_csv(out, index=False, encoding="utf-8-sig")
    return out
