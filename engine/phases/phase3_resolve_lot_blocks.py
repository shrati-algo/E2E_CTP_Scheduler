#!/usr/bin/env python3
"""
phase3_resolve_lot_blocks.py — Resolve EXACT (lot, block, sku) mapping
by replaying Phase 2's lot-claim logic against the demand file.

  >>> CTP PORT of v6_wave_p2/phases/phase3_resolve_lot_blocks.py <<<
  The two-pointer replay ALGORITHM is byte-identical to v6. Only I/O is adapted:
    * reads the NET demand (phase1_demand_NET_updated.csv[.gz]) -- the SAME demand
      Phase 2 sized from -- and phase2_lots_updated.csv.
    * writes outputs2/phase3_lot_blocks_updated.csv.gz + phase3_lot_to_skus_updated.csv.
    * NO /tmp/v5_outputs staging (that hard-coded Unix path breaks on Windows); writes
      to outputs2 atomically via a temp file in the same folder.

WHY v6 HAS THIS (kept for provenance)
  In v6, Phase 2's phase2_lots.csv stored only per-lot SUMMARIES (n_blocks, n_skus,
  first_need, last_need) -- NOT which exact blocks/SKUs each lot served. Phase 3 needs
  that exact mapping, so this helper reconstructs it by replaying the claim.

  ON CTP THIS IS A CROSS-CHECK, NOT A DEPENDENCY. Our Phase 2 already WRITES the exact
  mapping directly (outputs2/phase2_lot_blocks_updated.csv.gz + phase2_lot_skus_updated.csv),
  which is what phase3_dag_construction actually loads. This script re-derives the same
  table independently; run it to VERIFY the two agree (see --check). v6's own run_pipeline
  does NOT invoke this file either -- it is a helper, not a pipeline stage.

ALGORITHM (replay Phase 2 claim, exact -- unchanged from v6)
  For each item:
    Sort demand rows by need_by ascending; sort lots by first_need ascending.
    Two-pointer walk: assign each demand row to the first lot L still open at that time
    (L.last_need >= need_by) whose L.first_need <= need_by.
    A lot's set-of-skus is the union of skus from all its assigned demand rows.

OUTPUTS
  outputs2/phase3_lot_blocks_updated.csv.gz  — (lot_id, item_code, sku, block_id,
                                                need_by, demand_qty)
  outputs2/phase3_lot_to_skus_updated.csv    — (lot_id, n_blocks, n_skus, skus_set)
"""
from __future__ import annotations
import sys, os, pathlib, tempfile
import pandas as pd
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs2"
OUTPUTS.mkdir(exist_ok=True)


def _load_demand() -> pd.DataFrame:
    """The demand Phase 2 sized from = the NET (post-MRP) demand, then the gross fallback."""
    # STALE-FILE FIX (2026-09-26): within each pair take the NEWEST file, never a leftover twin.
    _pairs = (("phase1_demand_NET_updated.csv.gz", "phase1_demand_NET_updated.csv"),
              ("phase1_demand_updated.csv.gz", "phase1_demand_updated.csv"))
    _cands = []
    for _pr in _pairs:
        _ex = [OUTPUTS / c for c in _pr if (OUTPUTS / c).exists()]
        _cands += sorted(_ex, key=lambda q: q.stat().st_mtime, reverse=True)
    for p in _cands:
        if p.exists():
            comp = "gzip" if p.suffix == ".gz" else None
            d = pd.read_csv(p, low_memory=False, compression=comp)
            print(f"  loaded demand from {p.name}  rows={len(d):,}", flush=True)
            return d
    raise FileNotFoundError("no phase1_demand_NET_updated / phase1_demand_updated in outputs2/")


def _load_lots() -> pd.DataFrame:
    for cand in ("phase2_lots_updated.csv", "phase2_lots_updated.csv.gz"):
        p = OUTPUTS / cand
        if p.exists():
            comp = "gzip" if p.suffix == ".gz" else None
            d = pd.read_csv(p, low_memory=False, compression=comp)
            print(f"  loaded lots from {p.name}  rows={len(d):,}", flush=True)
            return d
    raise FileNotFoundError("no phase2_lots_updated.csv in outputs2/")


def _atomic_write(df: pd.DataFrame, name: str, gzip: bool = False):
    """Write to outputs2 via a temp file in the SAME dir, then os.replace (atomic, no /tmp)."""
    dest = OUTPUTS / name
    fd, tmp = tempfile.mkstemp(dir=str(OUTPUTS), suffix=".tmp")
    os.close(fd)
    df.to_csv(tmp, index=False, compression={"method": "gzip", "compresslevel": 1} if gzip else None)
    os.replace(tmp, dest)
    return dest


def resolve() -> pd.DataFrame:
    demand = _load_demand()
    lots = _load_lots()

    demand["need_by"] = pd.to_datetime(demand["need_by"], errors="coerce")
    demand["sku"] = demand["sku"].astype(str).str.strip()
    demand = demand.dropna(subset=["need_by"]).copy()
    lots["first_need"] = pd.to_datetime(lots["first_need"])
    lots["last_need"] = pd.to_datetime(lots["last_need"])

    rows = []
    matched = unmatched = 0
    items = demand["item_code"].unique()
    print(f"\n  Replaying claim for {len(items):,} items…", flush=True)

    demand_grp = demand.groupby("item_code")
    lots_grp = {item: g for item, g in lots.groupby("item_code")}

    for k, item in enumerate(items):
        if k % 100 == 0 and k > 0:
            print(f"    {k:,}/{len(items):,} items processed, {matched:,} matched", flush=True)
        try:
            item_demand = demand_grp.get_group(item)
        except KeyError:
            continue
        if item not in lots_grp:
            unmatched += len(item_demand); continue
        item_lots = lots_grp[item].sort_values("first_need").reset_index(drop=True)

        item_demand = item_demand.sort_values("need_by")
        d_t = item_demand["need_by"].values.astype("datetime64[ns]")
        d_sku = item_demand["sku"].values
        d_blk = item_demand["block_id"].values if "block_id" in item_demand.columns else np.array([""] * len(d_t))
        d_qty = item_demand["demand_qty"].values

        f_arr = item_lots["first_need"].values.astype("datetime64[ns]")
        l_arr = item_lots["last_need"].values.astype("datetime64[ns]")
        lid_arr = item_lots["lot_id"].values
        N = len(item_lots)

        j = 0
        for i in range(len(d_t)):
            t = d_t[i]
            while j < N and l_arr[j] < t:
                j += 1
            if j >= N:
                unmatched += 1; continue
            if f_arr[j] <= t:
                rows.append((lid_arr[j], item, d_sku[i], d_blk[i], d_t[i], d_qty[i]))
                matched += 1
            else:
                unmatched += 1

    print(f"  Matched   demand rows: {matched:,}", flush=True)
    print(f"  Unmatched demand rows: {unmatched:,}", flush=True)
    df = pd.DataFrame(rows, columns=["lot_id", "item_code", "sku", "block_id", "need_by", "demand_qty"])
    n = df["lot_id"].nunique()
    print(f"  Distinct lots with >=1 SKU: {n:,} / {len(lots):,} ({100*n/max(len(lots),1):.1f}%)")
    return df


def main() -> int:
    print("\n" + "=" * 65)
    print("  Phase 3 helper — resolve EXACT lot <-> block <-> sku mapping (CTP)")
    print("=" * 65)
    df = resolve()

    lot_to_skus = df.groupby("lot_id").agg(
        n_blocks=("block_id", "nunique"),
        n_skus=("sku", "nunique"),
        skus_set=("sku", lambda s: ", ".join(sorted(set(s)))),
    ).reset_index()
    print(f"  Avg SKUs per matched lot:   {lot_to_skus['n_skus'].mean():.2f}")
    print(f"  Avg blocks per matched lot: {lot_to_skus['n_blocks'].mean():.2f}")

    b = _atomic_write(df, "phase3_lot_blocks_updated.csv.gz", gzip=True)
    s = _atomic_write(lot_to_skus, "phase3_lot_to_skus_updated.csv")
    print(f"\n[OK] {b}\n     {s}")

    # ── OPTIONAL CROSS-CHECK: does this REPLAY match Phase 2's AUTHORITATIVE direct output? ──
    if "--check" in sys.argv:
        auth = OUTPUTS / "phase2_lot_blocks_updated.csv.gz"
        if auth.exists():
            a = pd.read_csv(auth, compression="gzip")
            key = ["lot_id", "item_code", "sku", "block_id"]
            am = a.groupby(key)["demand_qty"].sum().round(2)
            rm = df.groupby(key)["demand_qty"].sum().round(2)
            merged = pd.concat([am.rename("phase2"), rm.rename("replay")], axis=1).fillna(0)
            diff = merged[(merged["phase2"] - merged["replay"]).abs() > 0.01]
            print("\n  CROSS-CHECK vs phase2_lot_blocks_updated.csv.gz (Phase 2's direct output):")
            print(f"    (lot,item,sku,block) keys: phase2={len(am):,}  replay={len(rm):,}")
            print(f"    keys that DISAGREE on qty (>0.01): {len(diff):,}")
            if len(diff):
                print("    -> the replay is v6's window APPROXIMATION; Phase 2's direct output is the TRUTH.")
                print(diff.head(8).to_string())
            else:
                print("    -> IDENTICAL. The replay faithfully reproduces Phase 2's claim.")
        else:
            print("  --check: phase2_lot_blocks_updated.csv.gz not found; nothing to compare.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
