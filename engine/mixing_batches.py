#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mixing_batches.py -- BR-V1 (2026-10-01): mixing is planned in BATCHES, not kilograms.

The KG -> batch conversion is the routing's Banbury batch size (batch_size, batch_UNIT = KG) of
each MIXING operation, read per (compound, mixer) from the SOURCE table

    JK_CTP_PPDS.CTP_Master_Routing   (config business_rules.mixing_batch_source)

Mixer ids are canonicalised with machine_normalize (PCR '270M' == K270M, '440M' == F440, ...),
so the map is keyed by the scheduler's machine ids. A compound whose source row has a BLANK
batch_size falls back to the adapted inputs/routing.csv value (a flagged gap-fill) and is
reported in `fallback`.

    python mixing_batches.py          # print the map summary
"""
import os
import re

import pandas as pd

from machine_normalize import normalize_machine

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCE = os.path.join("INPUT_TABLES_30SEP_TRUE", "01_DB_tables", "JK_CTP_PPDS__CTP_Master_Routing.csv")
MIX_ITYPES = {"MASTER COMPOUND", "FINAL COMPOUND"}


def _ids(s):
    return [normalize_machine(m) for m in re.findall(r'"([^"]+)"', str(s))] or \
           [normalize_machine(m) for m in str(s).replace(";", ",").split(",") if m.strip()]


def _pairs(path):
    """(compound, canonical mixer) -> batch size kg, from one routing file's MIXING rows."""
    rt = pd.read_csv(path, dtype=str, low_memory=False)
    mx = rt[rt["operation_name"].astype(str).str.upper().str.contains("MIXING")]
    out = {}
    for comp, machines, bs in zip(mx["routed_product"].astype(str).str.strip(), mx["machines"],
                                  pd.to_numeric(mx["batch_size"], errors="coerce")):
        if pd.isna(bs) or bs <= 0:
            continue
        for m in _ids(machines):
            out[(comp, m)] = max(out.get((comp, m), 0.0), float(bs))
    return out


class BatchMap:
    """Batch size (kg) of a compound, per mixer. `size(item, machine)`; `size(item)` = the
    compound's batch size when every mixer agrees (the CTP source: they always do)."""

    def __init__(self, source=None, root=HERE):
        self.source = source or DEFAULT_SOURCE
        src = self.source if os.path.isabs(self.source) else os.path.join(root, self.source)
        self.pairs = _pairs(src)
        self.fallback = {}
        inp = os.path.join(root, "inputs", "routing.csv")
        if os.path.exists(inp):
            have = {c for c, _ in self.pairs}
            for k, v in _pairs(inp).items():
                if k[0] not in have and k not in self.pairs:
                    self.pairs[k] = v
                    self.fallback[k[0]] = v
        self.by_item = {}
        for (c, _), v in self.pairs.items():
            self.by_item.setdefault(c, set()).add(v)

    def size(self, item, machine=None):
        item = str(item).strip()
        if machine is not None:
            v = self.pairs.get((item, normalize_machine(machine)))
            if v:
                return v
        vs = self.by_item.get(item)
        return max(vs) if vs else None      # mixers disagree -> the larger batch (never under-mix)


if __name__ == "__main__":
    bm = BatchMap()
    multi = {c: sorted(v) for c, v in bm.by_item.items() if len(v) > 1}
    print(f"source: {bm.source}")
    print(f"{len(bm.by_item)} compounds, {len(bm.pairs)} (compound, mixer) pairs; "
          f"mixer-specific sizes: {len(multi)}; fallback to inputs/routing.csv: {bm.fallback}")
