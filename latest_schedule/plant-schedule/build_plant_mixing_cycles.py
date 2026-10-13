#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_plant_mixing_cycles.py -- v91 (user 2026-10-13): plant mixing cycle time per (compound, mixer) from the plant's
own bookings, 04-Oct 07:00 -> 11-Oct 07:00 (JK_CTP_PPDS.CTP_Plant_Schedule, extract
latest_schedule/v86_assumption_inputs/plant_schedule_04_10oct_raw.csv).

One batch = one prod_id. Cycle = median time between the starts of two consecutive batches of the SAME compound on the
SAME mixer (30 s .. 10 min apart, i.e. inside one run), so charge / mix / discharge are all included, but changeovers
and stops between runs are not. Batch kg = sum of the booked ingredient quantities (median per pair).

    python latest_schedule/plant-schedule/build_plant_mixing_cycles.py
    -> latest_schedule/plant-schedule/plant_mixing_cycles_04_10oct.csv
"""
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "latest_schedule", "v86_assumption_inputs", "plant_schedule_04_10oct_raw.csv")
OUT = os.path.join(HERE, "plant_mixing_cycles_04_10oct.csv")
MIXER = {"Mixer270F": "F270", "Mixer310F": "K310", "Mixer440": "F440", "Mixer431": "K430-1", "Mixer432": "K430-2",
         "Mixer270": "K270M"}


def main():
    d = pd.read_csv(SRC, low_memory=False)
    d = d[d["section"] == "mixing"]
    b = d.groupby("prod_id").agg(m=("machine", "first"), item=("item", "first"), s=("start_time", "min"),
                                 kg=("qty", "sum")).reset_index()
    b["m"] = b["m"].map(MIXER).fillna(b["m"])
    b["item"] = b["item"].astype(str).str.strip()
    b["s"] = pd.to_datetime(b["s"])
    b = b.sort_values(["m", "s"])
    b["gap_s"] = b.groupby("m")["s"].diff().dt.total_seconds()
    b["same"] = b["item"].eq(b.groupby("m")["item"].shift())
    r = b[b["same"] & (b["gap_s"] > 30) & (b["gap_s"] < 600)]
    out = r.groupby(["item", "m"]).agg(n_intervals=("gap_s", "size"), plant_cycle_s=("gap_s", "median"),
                                       plant_batch_kg=("kg", "median")).reset_index()
    tot = b.groupby(["item", "m"]).size().rename("batches_7d").reset_index()
    out = out.merge(tot, on=["item", "m"], how="left").rename(columns={"m": "mixer"})
    out["plant_cycle_s"] = out["plant_cycle_s"].round(1)
    out["plant_batch_kg"] = out["plant_batch_kg"].round(1)
    out["plant_kg_per_h"] = (out["plant_batch_kg"] / out["plant_cycle_s"] * 3600).round(0)
    out.to_csv(OUT, index=False)
    print(f"  {len(out)} compound x mixer pairs from {b['prod_id'].nunique():,} plant batches -> {OUT}")


if __name__ == "__main__":
    main()
