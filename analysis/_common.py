"""Shared helpers for the analysis scripts: where a run lives and its plan window."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SHIFT_START_H = 7


def out_root() -> Path:
    return Path(os.environ.get("CTP_OUT") or (ROOT / "outputs"))


def run_dir(run: str) -> Path:
    return out_root() / run


def analysis_dir(D: Path) -> Path:
    p = D / "analysis"
    p.mkdir(parents=True, exist_ok=True)
    return p


def plan_window(D: Path) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(first shift start, end of the last planned shift) of the run's building plans.

    The first shift starts at the plant day's 07:00; the end is the last planned
    build end, rounded up to the next shift change (07:00 / 15:00 / 23:00).
    """
    s, e = [], []
    for ln in ("pcr", "tbr"):
        p = D / ln / "phase5_building_plan.csv"
        if p.exists():
            b = pd.read_csv(p, parse_dates=["build_start", "build_end"])
            s.append(b["build_start"].min())
            e.append(b["build_end"].max())
    t0 = min(x for x in s if pd.notna(x))
    t0 = (t0 - pd.Timedelta(hours=SHIFT_START_H)).normalize() + pd.Timedelta(hours=SHIFT_START_H)
    t1 = max(x for x in e if pd.notna(x))
    k = -(-(t1 - t0) // pd.Timedelta(hours=8))          # ceil to whole shifts
    return t0, t0 + k * pd.Timedelta(hours=8)
