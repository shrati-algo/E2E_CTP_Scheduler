#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_day_pool_times.py -- time component demand on the day-pool build sequence (2026-09-30).

phase5 builds each (machine, plant-day) from 07:00 in order of curing need (building_day_pool_sequence),
but phase1b / phase1_5 timed component demand on the plan's cosmetic shift slots -- so a build that now
starts at 07:00 found its inner liner / tread due at the 15:00 slot ("kit late" 113 rows). This writes
the PREDICTED sequenced start/end into inputs/building_plan.csv (row order unchanged, so block ids stay
aligned) and keeps the plant's slot in origStartTime / origEndTime for reporting.
"""
import os
import pathlib
import sys

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "phases"))
import day_pool  # noqa: E402

INP = HERE / "inputs"


def main():
    p = INP / "building_plan.csv"
    bp = pd.read_csv(p)
    if "origStartTime" in bp.columns:                       # idempotent: always start from the slot
        bp["startTime"], bp["endTime"] = bp["origStartTime"], bp["origEndTime"]
    bp["_row"] = range(len(bp))
    seq = day_pool.sequence(bp, INP)
    m = seq.set_index("_row")
    bp["origStartTime"], bp["origEndTime"] = bp["startTime"], bp["endTime"]
    bp["startTime"] = bp["_row"].map(m["_pred_start"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    bp["endTime"] = bp["_row"].map(m["_pred_end"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    sh = (pd.to_datetime(bp["startTime"]) - pd.to_datetime(bp["origStartTime"])).dt.total_seconds() / 3600
    bp.drop(columns="_row").to_csv(p, index=False)
    print(f"  day-pool times: {len(bp)} build rows re-timed to the sequenced start "
          f"(vs plan slot: {int((sh < -0.1).sum())} earlier, {int((sh > 0.1).sum())} later, "
          f"mean {sh.mean():+.1f} h) -> component demand follows the real build order")


if __name__ == "__main__":
    main()
