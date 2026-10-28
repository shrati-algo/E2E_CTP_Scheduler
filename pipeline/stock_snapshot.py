"""Step 2 - which stock (WIP) and green-tyre (GT) snapshot the run uses.

POLICY: the shift-A sync of the plan day - the floor as the plan's first shift
opens (PPDS strikes CTP_Opening_WIP and CTP_GT_Inventory at about 06:30, 14:30
and 22:30 and keeps all three).

QUALITY GATES (a bad snapshot is never used silently)
    1. the plan day HAS a shift-A sync                         else FATAL
    2. the sync is COMPLETE: its row count is at least
       `min_ratio` (default 0.5) x the usual size (the
       largest of the previous 9 syncs)                                         else FATAL
    3. the sync was struck near 06:30 (05:30 - 07:30)          else WARNING
A FATAL lists every sync of the surrounding days and tells you how to choose
one explicitly with --wip-sync / --gt-sync-pcr / --gt-sync-tbr. An explicit
choice is honoured as given, recorded in DATA_STAGED.json and printed in the
run summary - the pipeline never falls back on its own.
"""
from __future__ import annotations

import sys

import pandas as pd

from . import db

WIP_T, GT_T = "CTP_Opening_WIP", "CTP_GT_Inventory"


def _ts(x) -> str:
    return pd.Timestamp(x).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _syncs(cn, table, day, extra_where="", by_line=False):
    d0 = (pd.Timestamp(day) - pd.Timedelta(days=3)).date()
    cols = "tyre_type, " if by_line else ""
    d = db.fetch(f"SELECT {cols}inventory_date, shift, updated_at, COUNT(*) AS n FROM {table} "
                 f"WHERE inventory_date BETWEEN '{d0}' AND '{pd.Timestamp(day).date()}' "
                 f"{extra_where} GROUP BY {cols}inventory_date, shift, updated_at "
                 f"ORDER BY updated_at", cn)
    d["updated_at"] = pd.to_datetime(d["updated_at"])
    d["shift"] = d["shift"].astype(str).str.strip().str.upper()
    return d


def _judge(s: pd.DataFrame, day, what: str, min_ratio: float, override: str | None):
    """Pick and gate one sync from a table of syncs (one line / one table)."""
    day = pd.Timestamp(day).date()
    show = s.assign(updated_at=s["updated_at"].map(_ts))[["inventory_date", "shift",
                                                          "updated_at", "n"]]
    if override:
        hit = s[s["updated_at"].map(_ts).str.startswith(override.strip()[:19])]
        if hit.empty:
            sys.exit(f"[FATAL] {what}: no sync at '{override}'. Syncs:\n{show.to_string(index=False)}")
        r = hit.iloc[0]
        return {"sync": _ts(r["updated_at"]), "inventory_date": str(r["inventory_date"]),
                "shift": r["shift"], "rows": int(r["n"]), "chosen_by": "explicit (command line)",
                "warnings": [f"{what}: sync {_ts(r['updated_at'])} chosen explicitly "
                             f"(policy is the plan day's shift-A sync)"]}
    a = s[(pd.to_datetime(s["inventory_date"]).dt.date == day) & (s["shift"] == "A")]
    if a.empty:
        sys.exit(f"[FATAL] {what}: no shift-A sync on {day}.\n{show.to_string(index=False)}\n"
                 f"  choose one explicitly with the option named below, or wait for the sync.")
    r = a.iloc[0]
    # the usual size: the largest of the previous 9 syncs, so a run of
    # partial syncs (25-26 Oct had five) cannot drag the reference down
    prev = s[s["updated_at"] < r["updated_at"]].tail(9)
    ref = float(prev["n"].max()) if len(prev) else float(r["n"])
    warn = []
    if ref > 0 and r["n"] < min_ratio * ref:
        sys.exit(f"[FATAL] {what}: the shift-A sync of {day} ({_ts(r['updated_at'])}) holds "
                 f"{int(r['n']):,} rows, under {min_ratio:.0%} of the usual {ref:,.0f} - "
                 f"an INCOMPLETE snapshot. Using it would treat most of the floor as empty.\n"
                 f"{show.to_string(index=False)}\n"
                 f"  Decide which snapshot is right and pass it explicitly, e.g. the last "
                 f"complete sync before 07:00 on {day}.")
    hh = r["updated_at"].hour + r["updated_at"].minute / 60
    if not 5.5 <= hh <= 7.5:
        warn.append(f"{what}: shift-A sync struck at {r['updated_at']:%H:%M}, not near 06:30 - "
                    f"material produced between 06:30 and then is already in it")
    return {"sync": _ts(r["updated_at"]), "inventory_date": str(r["inventory_date"]),
            "shift": "A", "rows": int(r["n"]), "chosen_by": "policy (plan day shift A)",
            "reference_rows": round(ref), "warnings": warn}


def choose(cn, day, wip_override=None, gt_override=None, min_ratio: float = 0.5) -> dict:
    gt_override = gt_override or {}
    w = _judge(_syncs(cn, WIP_T, day), day, f"{WIP_T}", min_ratio, wip_override)
    g = _syncs(cn, GT_T, day, by_line=True)
    g["tyre_type"] = g["tyre_type"].astype(str).str.strip().str.upper()
    gt = {}
    for ln in ("PCR", "TBR"):
        gt[ln.lower()] = _judge(g[g["tyre_type"] == ln].drop(columns="tyre_type"), day,
                                f"{GT_T} {ln}", min_ratio, gt_override.get(ln.lower()))
    return {"wip": w, "gt": gt,
            "warnings": w["warnings"] + gt["pcr"]["warnings"] + gt["tbr"]["warnings"]}
