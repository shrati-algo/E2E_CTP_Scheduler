#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""machine_normalize.py -- canonical machine IDs so PCR and TBR name the SAME physical machine
identically. Required for a COMBINED PCR+TBR schedule: shared machines must resolve to one ID or
the two products silently double-book them.

The 6 Banbury MIXERS are physically shared but the two routings label them differently
(PCR drops/rewrites the K/F prefix). Verified against ctp_inputfiles/CTP Machines.xlsx
('Stock Machines') and the two live floor schedules:

    physical mixer        PCR calls it   TBR calls it   canonical
    Master 270            270M           K270M          K270M
    Master 440            440M           F440           F440
    Master 430-1          430-1          K430-1         K430-1
    Master 430-2          430-2          K430-2         K430-2
    Final  270            F270           F270           F270   (already same)
    Final  310            310            K310           K310

The other shared machines already share an ID across both products, so they need no alias:
    901  (4-Roll Calender), 2001 (Bias cutter), 2401/2402 (Chaffer slitter).
"""

# PCR-form machine id -> canonical id (the TBR/plant-sheet form).
MACHINE_ALIASES = {
    "270M":  "K270M",   # master mixer 270
    "440M":  "F440",    # master mixer 440
    "430-1": "K430-1",  # master mixer 430-1
    "430-2": "K430-2",  # master mixer 430-2
    "310":   "K310",    # final mixer 310
    # F270, K270M, F440, K430-1, K430-2, K310 are already canonical -> map to themselves
}

# Canonical ids of every machine physically shared by PCR and TBR (mixers + calender + cutters).
# A combined scheduler must arbitrate exactly these on ONE calendar; the rest are dedicated lines.
SHARED_MACHINES = {
    "K270M", "F440", "K430-1", "K430-2", "F270", "K310",   # 6 Banbury mixers
    "901",                                                  # 4-Roll Calender
    "2001",                                                 # Bias cutter
    "2401", "2402",                                         # Chaffer slitter
}


def normalize_machine(mid) -> str:
    """Map any product's machine id to its canonical (shared) id."""
    m = str(mid).strip()
    return MACHINE_ALIASES.get(m, m)


def normalize_series(s):
    """Vectorised normalize for a pandas Series of machine ids."""
    return s.astype(str).str.strip().map(lambda m: MACHINE_ALIASES.get(m, m))


def is_shared(mid) -> bool:
    """True if this machine is physically shared between PCR and TBR."""
    return normalize_machine(mid) in SHARED_MACHINES


if __name__ == "__main__":
    for a, b in MACHINE_ALIASES.items():
        print(f"  {a:8} -> {b}")
    print(f"\n  {len(SHARED_MACHINES)} shared machines: {sorted(SHARED_MACHINES)}")
