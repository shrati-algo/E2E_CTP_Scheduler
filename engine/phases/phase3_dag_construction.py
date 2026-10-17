#!/usr/bin/env python3
"""
phase3_dag_construction.py — Build the lot-level production DAG.

WHAT THIS PHASE DOES
  For every lot produced in Phase 2, identify:
    - PREDECESSORS: lots whose item is consumed by this lot (per BOM)
    - SUCCESSORS:   lots that consume this lot's item (per BOM)
    - TERMINAL CONSUMER: the curing block(s) at the bottom of the chain
  This produces the precedence graph the scheduler uses in Phase 4 (CPM)
  to compute critical paths and in Phase 5 (forward placement) to enforce
  that producer finishes before consumer starts.

UPDATES (FRC release)
  - compute_degrees now carries forward belt/FRC campaign columns from
    Phase 2 (is_belt, wire_type, campaign_id, is_campaign_start/end,
    first_need, last_need, min/max_aging_h) so Phase 5 can read them
    directly from phase3_lot_degrees.csv without rejoining phase2_lots.

OUTPUTS
  outputs2/phase3_dag_edges.csv   — producer_lot_id, consumer_lot_id, sku
  outputs2/phase3_lot_degrees.csv — lot_id, n_predecessors, n_successors,
                                    depth_from_root, is_terminal,
                                    is_belt, wire_type, campaign_id, ...
  outputs2/phase3_dag_summary.xlsx — graph stats, depth distribution,
                                     per-MG DAG size, etc.
"""
from __future__ import annotations
import os
import sys, pathlib, yaml, re, tempfile, shutil
import pandas as pd
from collections import defaultdict, deque

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS  = ROOT/"inputs"
OUTPUTS = ROOT/"outputs2"           # NEW WRITE LOCATION
OUTPUTS.mkdir(parents=True, exist_ok=True)
PRIOR_OUTPUTS = ROOT/"outputs"      # Phase 1a / 1.5 outputs (already-run, re-used)
RUN_SUFFIX = "_updated"


def _suff(name):
    """Insert _updated before the extension (handles .csv.gz, .xlsx, .csv)."""
    if name.endswith(".csv.gz"):
        return name[:-7] + RUN_SUFFIX + ".csv.gz"
    if name.endswith(".csv"):
        return name[:-4] + RUN_SUFFIX + ".csv"
    if name.endswith(".xlsx"):
        return name[:-5] + RUN_SUFFIX + ".xlsx"
    return name + RUN_SUFFIX


_STAGE = OUTPUTS
_STAGE.mkdir(parents=True, exist_ok=True)


def _stage_csv(df, name, big_threshold_rows=200000):
    is_big = len(df) > big_threshold_rows
    write_name = _suff(name + ".gz" if is_big else name)
    tmp = _STAGE / write_name
    df.to_csv(tmp, index=False, compression="gzip" if is_big else None)
    final = OUTPUTS / write_name
    return final


def _stage_xlsx(sheets, name):
    write_name = _suff(name)
    tmp = _STAGE / write_name
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        for k, df in sheets.items():
            clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
            df.to_excel(w, sheet_name=clean, index=False)
    final = OUTPUTS / write_name
    return final


def _read(p):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(p, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(p)

# ============================================================
# DB-AWARE MASTER LOADER (auto-inserted by DB-only migration)
# Routes `load master` calls to MySQL when cfg["data_source"]=="db".
# Falls back to the existing local CSV read otherwise.
# ============================================================
def _db_or_csv(_key, _cfg=None):
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        from db_loader import load_input
        return load_input(_key, _cfg)
    except Exception as _e:
        # Fall back to CSV when DB or db_loader is unavailable
        _fp = (_cfg or {}).get("files", {})
        _fname = _fp.get(_key)
        if _fname is None:
            raise
        return _read(INPUTS / _fname)


def load_cfg(): return yaml.safe_load(open(ROOT/"config.yaml"))


def load_lots():
    for name in ("phase2_lots_v2.csv", "phase2_lots.csv", "phase2_lots.csv.gz"):
        p = OUTPUTS/_suff(name)
        if p.exists():
            try:
                df = pd.read_csv(p, low_memory=False)
                # CURING representation lots (L11) are terminal -- scheduled by phase6, not
                # here. Drop them so they create NO DAG edges (zero effect on precedence/CPM).
                if "operation" in df.columns:
                    _n0 = len(df)
                    df = df[df["operation"].astype(str).str.strip().str.upper() != "CURING"]
                    if len(df) < _n0:
                        print(f"    dropped {_n0 - len(df):,} CURING representation lot(s) "
                              f"(terminal; scheduled by phase6)", flush=True)
                print(f"    loaded {p.name}  rows={len(df):,}", flush=True)
                return df
            except Exception as e:
                print(f"    failed {p.name}: {e}"); continue
    raise FileNotFoundError("no phase2_lots*.csv found in outputs2/")


def load_lot_skus():
    p = OUTPUTS/_suff("phase2_lot_skus.csv")
    if not p.exists():
        print(f"    WARNING: {p} not found — falling back to derive_skus_served")
        return None
    ls = pd.read_csv(p)
    out = {}
    for _, r in ls.iterrows():
        skus_str = str(r.get("skus_set", "") or "")
        out[str(r["lot_id"])] = {s.strip() for s in skus_str.split(",") if s.strip()}
    print(f"    loaded phase2_lot_skus.csv  rows={len(out):,}")
    return out


def build_bom_edges(bom):
    """Build per-SKU BOM edge indexes.

    MG STRIPPED 2026-07-22 (user instruction). CTP's BOM is single-variant
    (Equipment == 'TBM PCR' on every row), so the MG filter passed every row and
    every (sku, mg, *) key carried the same constant mg. Keys are now (sku, child)
    and (sku, parent); block-level edges still filter by curing-block overlap in
    build_dag_edges.
    """
    bom["__SP__"] = bom["Super parent"].astype(str).str.strip()

    edges_by_sku = defaultdict(set)
    child_to_parents = defaultdict(set)
    parent_to_children = defaultdict(set)

    for _, r in bom.iterrows():
        sku = r["__SP__"]
        if not sku: continue
        par = str(r["Parent"]).strip()
        ch  = str(r["child"]).strip()
        if not par or not ch: continue
        edges_by_sku[sku].add((par, ch))
        child_to_parents[(sku, ch)].add(par)
        parent_to_children[(sku, par)].add(ch)

    return edges_by_sku, child_to_parents, parent_to_children


def derive_skus_served(lots, plan_curing):
    import numpy as np
    demand = None
    for cand in (OUTPUTS/_suff("phase1_demand.csv"), OUTPUTS/_suff("phase1_demand.csv.gz")):
        if cand.exists():
            try:
                demand = pd.read_csv(cand, low_memory=False)
                print(f"    loaded demand from {cand.name}  rows={len(demand):,}", flush=True)
                break
            except Exception as e:
                print(f"    failed {cand}: {e}", flush=True)
                continue
    if demand is None or "need_by" not in demand.columns:
        return {}

    skus_per_lot = defaultdict(set)
    matched_total = 0

    for item in demand["item_code"].unique():
        item_demand = demand[demand["item_code"] == item]
        item_lots = lots[lots["item_code"] == item]
        if item_lots.empty: continue
        ilots = item_lots.sort_values("first_need").reset_index(drop=True)
        f_arr = pd.to_datetime(ilots["first_need"]).values.astype("datetime64[ns]")
        l_arr = pd.to_datetime(ilots["last_need"]).values.astype("datetime64[ns]")
        lot_ids = ilots["lot_id"].values
        d_item = item_demand.dropna(subset=["need_by"])
        if d_item.empty: continue
        need_arr = pd.to_datetime(d_item["need_by"]).values.astype("datetime64[ns]")
        sku_arr  = d_item["sku"].astype(str).str.strip().values
        for i in range(len(need_arr)):
            t = need_arr[i]
            j = int(np.searchsorted(f_arr, t, side="right")) - 1
            while j >= 0 and l_arr[j] >= t:
                if f_arr[j] <= t <= l_arr[j]:
                    skus_per_lot[lot_ids[j]].add(sku_arr[i])
                    matched_total += 1
                    break
                j -= 1
    print(f"    matched demand rows: {matched_total:,}", flush=True)
    return dict(skus_per_lot)


def load_plan_curing(plan_or_path):
    """Accept either a DataFrame (preferred — from db_loader) OR a path/str.

    In DB-only mode the caller passes the already-loaded plan DataFrame so
    we never touch the filesystem.
    """
    if isinstance(plan_or_path, pd.DataFrame):
        plan = plan_or_path.copy()
    else:
        plan = pd.read_csv(plan_or_path, encoding="latin-1", engine="python",
                            on_bad_lines="skip", dtype=str, quoting=0)
    plan["SKUCode"] = plan["skuCode"].astype(str).str.strip()
    plan = plan[plan["SKUCode"] != "CHANGEOVER"]
    plan["Qty"] = pd.to_numeric(plan["qty"], errors="coerce").fillna(0)
    plan = plan[plan["Qty"] > 0]
    # Plan timestamps are naive IST.
    parsed = pd.to_datetime(plan["startTime"], errors="coerce")
    if hasattr(parsed, "dt") and parsed.dt.tz is not None:
        parsed = parsed.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    plan["effective_start"] = parsed
    return plan[["SKUCode","Qty","effective_start"]].dropna()


def load_blocks_per_lot():
    """V6_wave: build lot_id -> set(block_id) from phase2_lot_blocks.csv(.gz).

    Used to align producer↔consumer edges by curing block — only link a
    producer lot to a consumer lot if their curing-block sets intersect.
    Eliminates W10-producer → W01-consumer mis-edges that bottleneck Building.
    """
    for nm in ("phase2_lot_blocks.csv.gz", "phase2_lot_blocks.csv"):
        p = OUTPUTS / _suff(nm)
        if not p.exists():
            continue
        print(f"    loading block map from {p.name}...", flush=True)
        df = pd.read_csv(p, usecols=["lot_id", "block_id"], low_memory=False)
        bmap = defaultdict(set)
        for lid, b in zip(df["lot_id"].values, df["block_id"].values):
            bmap[lid].add(b)
        print(f"      lots with block mappings: {len(bmap):,}", flush=True)
        return bmap
    print("    WARNING: no phase2_lot_blocks.csv found — block alignment disabled")
    return {}


def index_lots(lots, skus_per_lot, blocks_per_lot):
    """Vectorised: use numpy arrays + zip instead of iterrows."""
    by_item = defaultdict(list)
    cols = ["lot_id","item_code","first_need","last_need","duration_hours","operation"]
    arrs = {c: lots[c].to_numpy() for c in cols}
    # S6 (LEVER 2): lot_qty + item_type carried so the quantity-allocation DAG can
    # size demand (consumer_lot_qty x bom_child_qty) and identify feeders. Absent =>
    # 0.0 / "" (the fan-link path never reads them, so this is harmless when OFF).
    _qty = pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0).to_numpy() \
        if "lot_qty" in lots.columns else None
    _itype = lots["item_type"].astype(str).to_numpy() if "item_type" in lots.columns else None
    dur = arrs["duration_hours"]
    for i in range(len(lots)):
        lid = arrs["lot_id"][i]
        sku_set = skus_per_lot.get(lid, set())
        blk_set = blocks_per_lot.get(lid, set())
        d = dur[i]
        try:
            d = float(d) if d == d else 0.0
        except Exception:
            d = 0.0
        by_item[arrs["item_code"][i]].append({
            "lot_id":      lid,
            "item_code":   arrs["item_code"][i],
            "first_need":  arrs["first_need"][i],
            "last_need":   arrs["last_need"][i],
            "skus_served": sku_set,
            "blocks":      blk_set,
            "duration_hours": d,
            "operation":   arrs["operation"][i],
            "lot_qty":     float(_qty[i]) if _qty is not None else 0.0,
            "item_type":   (_itype[i] if _itype is not None else ""),
        })
    return by_item


def build_dag_edges(lots_by_item, child_to_parents, max_edges=10_000_000,
                    aging_map=None, aging_safety_h=8.0):
    """V6_wave block-aligned DAG (fast variant) + F1 AGING-AWARE FIFO ALLOCATION.

    Pre-indexes producer lots by (item, sku) so the inner loop becomes a
    dict lookup instead of iterating every parent lot.
    Uses a dict keyed on (Lp_id, l_id) to dedupe in-place.

    F1: When linking producer (child) -> consumer (parent), only allow if the
    producer's first_need is within (max_aging[child_item] - safety) hours of
    the consumer's first_need. This prevents one producer batch from feeding
    consumers across multiple aging windows (which would cause expiry).

    For each consumer, the FIFO logic picks the LATEST producer batch (by
    first_need) that satisfies producer.first_need <= consumer.first_need AND
    consumer.first_need - producer.first_need <= max_aging - safety.
    """
    import time as _t
    _t0 = _t.time()
    rows_dict = {}  # (producer_lot, consumer_lot) -> (producer_item, consumer_item, sku)
    n_skipped_block = 0
    n_skipped_aging = 0
    n_kept = 0

    def _max_aging_h(item_code):
        if aging_map is None:
            return None
        v = aging_map.get(item_code)
        if v is None:
            return None
        # (min_h, max_h) tuple
        try:
            return float(v[1])
        except Exception:
            return None

    # Index producer lots: (item, sku) -> list of (lot_id, blocks_set, first_need_ts)
    print(f"      building producer index ...", flush=True)
    prod_index = {}
    for item_P, P_lots in lots_by_item.items():
        for Lp in P_lots:
            blk = Lp.get("blocks", set())
            lid = Lp["lot_id"]
            fn = Lp.get("first_need", None)
            try:
                fn_ts = pd.to_datetime(fn, errors="coerce")
            except Exception:
                fn_ts = pd.NaT
            for sku in Lp["skus_served"]:
                key = (item_P, sku)
                lst = prod_index.get(key)
                if lst is None:
                    prod_index[key] = [(lid, blk, fn_ts)]
                else:
                    lst.append((lid, blk, fn_ts))
    # Sort each producer list by first_need ascending so FIFO match is fast.
    for k in prod_index:
        prod_index[k].sort(key=lambda t: (pd.Timestamp.max if pd.isna(t[2]) else t[2]))
    print(f"      producer index: {len(prod_index):,} (item,sku) keys [+{_t.time()-_t0:.1f}s]", flush=True)

    _items_total = len(lots_by_item)
    _items_done = 0
    for item_I, I_lots in lots_by_item.items():
        _items_done += 1
        if _items_done % 100 == 0:
            print(f"      progress: {_items_done}/{_items_total} items, kept={n_kept:,} skipped_aging={n_skipped_aging:,} elapsed={_t.time()-_t0:.1f}s", flush=True)
        # F1 v3 (REVERTED): use (max_aging - safety) — was overcorrecting at /2.
        # Filter only edges where consumer is BEYOND aging window from producer.
        # Phase 5's F3 self-balance handles tighter placement within window.
        max_age_h = _max_aging_h(item_I)
        if max_age_h is not None:
            aging_window_h = max(0.0, max_age_h - aging_safety_h)
        else:
            aging_window_h = None  # no aging data — accept all
        for L in I_lots:
            l_id = L["lot_id"]
            l_blocks = L.get("blocks", set())
            l_first_need = pd.to_datetime(L.get("first_need", None), errors="coerce")
            # === DETERMINISM (E6). (A) DEFECT. ===
            # `skus_served` and `parents` are SETS OF STRINGS. Python randomises string
            # hashing per process (PYTHONHASHSEED), so a set of strings iterates in a
            # DIFFERENT ORDER EVERY RUN. Two things leaked out of that:
            #   1. ROW ORDER of phase3_dag_edges.csv (random -> 4 runs, 4 md5s).
            #   2. `via_sku` / `producer_item` ATTRIBUTION: the (producer, consumer) PAIR
            #      can be reachable via several (sku, P) routes, and the `k not in rows_dict`
            #      guard below means THE FIRST ROUTE VISITED WINS. Hash order picked it.
            #      MEASURED: 413 pairs got a different attribution run to run.
            # sorted() makes the tie-break DETERMINISTIC. The SELECTION RULE IS UNCHANGED:
            # the same set of routes is visited, the same PAIR SET results (21,831 either
            # way) -- only "which of the equally-valid routes is recorded" is now stable
            # (lowest sku, then lowest parent) instead of hash-random.
            for sku in sorted(L["skus_served"]):
                parents = child_to_parents.get((sku, item_I), set())
                if not parents: continue
                for P in sorted(parents):
                    cand_list = prod_index.get((P, sku))
                    if not cand_list:
                        continue
                    for Lp_id, p_blocks, p_first_need in cand_list:
                        if l_blocks and p_blocks:
                            # isdisjoint short-circuits; ensure smaller set first
                            if len(p_blocks) < len(l_blocks):
                                if p_blocks.isdisjoint(l_blocks):
                                    n_skipped_block += 1
                                    continue
                            else:
                                if l_blocks.isdisjoint(p_blocks):
                                    n_skipped_block += 1
                                    continue
                        # === F1 AGING-AWARE EDGE FILTER ===
                        # consumer (Lp_id, of parent P) is at p_first_need.
                        # producer (l_id, of child item_I) is at l_first_need.
                        # In real plant: producer must be made BEFORE consumer needs it,
                        # AND within max_aging window.
                        # Skip the edge if consumer needs the material more than
                        # (max_aging - safety) hours after producer's first_need —
                        # there will be a LATER producer batch for that consumer.
                        if (aging_window_h is not None
                                and l_first_need is not pd.NaT and p_first_need is not pd.NaT
                                and not pd.isna(l_first_need) and not pd.isna(p_first_need)):
                            gap_h = (p_first_need - l_first_need).total_seconds() / 3600.0
                            if gap_h > aging_window_h:
                                n_skipped_aging += 1
                                continue
                            if gap_h < -aging_window_h:
                                # consumer needs material BEFORE producer is made — wrong direction
                                # (handled by topology elsewhere; skip to be safe)
                                n_skipped_aging += 1
                                continue
                        # FIX: BOM convention is Parent=output, child=ingredient.
                        # Physical production sequence: child produced FIRST → consumed by parent.
                        # So true producer = l_id (lot of child item_I); true consumer = Lp_id (lot of parent P).
                        # Was: k=(Lp_id, l_id), value=(P, item_I, sku) — reversed every edge.
                        k = (l_id, Lp_id)
                        if k not in rows_dict:
                            rows_dict[k] = (item_I, P, sku)
                            n_kept += 1
                            if n_kept >= max_edges:
                                print(f"    [edge cap hit] kept={n_kept:,} skipped_block={n_skipped_block:,} skipped_aging={n_skipped_aging:,}", flush=True)
                                return _rows_dict_to_df(rows_dict)
    print(f"    Block-alignment: kept={n_kept:,}  skipped (no block overlap)={n_skipped_block:,}  skipped (aging window)={n_skipped_aging:,}", flush=True)
    print(f"    Building DataFrame from dict ({len(rows_dict):,} rows)...", flush=True)
    return _rows_dict_to_df(rows_dict)


def build_dag_edges_qty_alloc(lots_by_item, child_to_parents, lot_qty,
                              bom_child_qty, aging_map=None, aging_safety_h=8.0):
    """LEVER 2 / S6 -- FEFO QUANTITY-ALLOCATION DAG (config flag dag_quantity_allocation).

    THE PROBLEM this replaces (see phase4_cpm.py ~:544):
      build_dag_edges() FAN-LINKS each producer (child) lot to EVERY block+aging-
      compatible consumer (parent) lot -- the whole 3-day wave. phase4's backward CPM
      then sets each producer's LFT = min over its consumers of (consumer LST - lag),
      so a producer linked to the whole wave collapses to the wave-START. Feeders are
      then built early in bursts and WAIT -> component over-age (43.9%).

    THE FIX: allocate each producer lot's QUANTITY to the consumer lots it actually
    serves, FEFO (oldest supply -> earliest demand), and emit an edge only where a
    producer contributes quantity. A producer then links to the (contiguous, by
    consume-time) slice of consumers it feeds, so its LFT tracks the LATEST consumer
    it serves, not the wave start. Feeder production spreads to match level demand.

    HARD CONSTRAINTS PRESERVED, byte-for-byte with the fan-link path:
      * block overlap  -- producer.blocks intersect consumer.blocks (or a side has none)
      * aging window   -- |consumer.first_need - producer.first_need| <= max_aging-safety
      * routing sku    -- producer and consumer must share a BOM-routing sku
      * edge direction -- (producer=child lot, consumer=parent lot), (child, parent, sku)
    DETERMINISM: children processed in sorted order; producers FEFO by (first_need,
    lot_id); consumers by (first_need, lot_id); via_sku = min shared sku. No set-
    iteration order leaks. A consumer that cannot be covered (real supply shortage)
    simply gets the producers there are -- it is NEVER given an already-consumed lot,
    which is exactly the double-count the fan-link hid.
    """
    import time as _t
    _t0 = _t.time()
    EPS = 1e-6
    rows_dict = {}                       # (producer_lot, consumer_lot) -> (child, parent, via_sku)

    def _max_aging_h(item_code):
        if aging_map is None:
            return None
        v = aging_map.get(item_code)
        if v is None:
            return None
        try:
            return float(v[1])
        except Exception:
            return None

    # bom_route_skus[(child, parent)] = { sku : this BOM edge is routed via sku }
    # (built from child_to_parents, which is keyed (sku, child) -> {parents})
    print(f"      building BOM route-sku index ...", flush=True)
    bom_route_skus = defaultdict(set)
    for (sku, child), parents in child_to_parents.items():
        for parent in parents:
            bom_route_skus[(child, parent)].add(sku)

    # consumers_of_child[child] = list of dicts {lot_id, blocks, first_need, parent,
    #                                            route_skus (this Lp's skus that route child)}
    print(f"      building consumer index ...", flush=True)
    consumers_of_child = defaultdict(list)
    for (child, parent), route_skus in bom_route_skus.items():
        for Lp in lots_by_item.get(parent, []):
            shared = Lp["skus_served"] & route_skus
            if not shared:
                continue
            consumers_of_child[child].append({
                "lot_id":     Lp["lot_id"],
                "blocks":     Lp.get("blocks", set()),
                "first_need": pd.to_datetime(Lp.get("first_need"), errors="coerce"),
                "parent":     parent,
                "route_skus": shared,
            })
    print(f"      consumer index: {sum(len(v) for v in consumers_of_child.values()):,} "
          f"(child,consumer) links over {len(consumers_of_child):,} child items "
          f"[+{_t.time()-_t0:.1f}s]", flush=True)

    n_kept = n_skip_block = n_skip_aging = n_skip_sku = 0
    n_short = 0                          # consumer-item demands left un-covered (real shortage)
    _children = sorted(consumers_of_child.keys())
    for _ci, child in enumerate(_children):
        if (_ci + 1) % 100 == 0:
            print(f"      alloc progress: {_ci+1}/{len(_children)} children, "
                  f"kept={n_kept:,} elapsed={_t.time()-_t0:.1f}s", flush=True)
        producers = lots_by_item.get(child, [])
        if not producers:
            continue
        # FEFO producers; a stable secondary key on lot_id keeps it deterministic.
        P = sorted(
            ({"lot_id": p["lot_id"], "blocks": p.get("blocks", set()),
              "first_need": pd.to_datetime(p.get("first_need"), errors="coerce"),
              "skus": p["skus_served"],
              "rem": float(lot_qty.get(p["lot_id"], 0.0))} for p in producers),
            key=lambda d: (pd.Timestamp.max if pd.isna(d["first_need"]) else d["first_need"],
                           str(d["lot_id"])))
        C = sorted(consumers_of_child[child],
                   key=lambda d: (pd.Timestamp.max if pd.isna(d["first_need"]) else d["first_need"],
                                  str(d["lot_id"])))
        max_age_h = _max_aging_h(child)
        window_h = None if max_age_h is None else max(0.0, max_age_h - aging_safety_h)
        p_start = 0                      # skip fully-exhausted leading producers
        for c in C:
            cq = bom_child_qty.get((c["parent"], child))
            if cq is None or cq <= 0:
                continue
            need = float(lot_qty.get(c["lot_id"], 0.0)) * float(cq)
            if need <= EPS:
                continue
            c_blocks = c["blocks"]; c_fn = c["first_need"]; c_skus = c["route_skus"]
            while p_start < len(P) and P[p_start]["rem"] <= EPS:
                p_start += 1
            j = p_start
            while need > EPS and j < len(P):
                p = P[j]
                if p["rem"] <= EPS:
                    j += 1
                    continue
                shared = p["skus"] & c_skus
                if not shared:
                    n_skip_sku += 1
                    j += 1
                    continue
                if c_blocks and p["blocks"] and p["blocks"].isdisjoint(c_blocks):
                    n_skip_block += 1
                    j += 1
                    continue
                if (window_h is not None and pd.notna(c_fn) and pd.notna(p["first_need"])):
                    gap_h = (c_fn - p["first_need"]).total_seconds() / 3600.0
                    if gap_h > window_h or gap_h < -window_h:
                        n_skip_aging += 1
                        j += 1
                        continue
                alloc = min(need, p["rem"])
                k = (p["lot_id"], c["lot_id"])
                if k not in rows_dict:
                    rows_dict[k] = (child, c["parent"], min(shared))
                    n_kept += 1
                p["rem"] -= alloc
                need -= alloc
                j += 1
            if need > EPS:
                n_short += 1
    print(f"    QTY-ALLOC: kept={n_kept:,}  skipped (sku)={n_skip_sku:,}  "
          f"(block)={n_skip_block:,}  (aging)={n_skip_aging:,}  "
          f"under-covered consumer-item demands={n_short:,}  [+{_t.time()-_t0:.1f}s]", flush=True)
    print(f"    Building DataFrame from dict ({len(rows_dict):,} rows)...", flush=True)
    return _rows_dict_to_df(rows_dict)


def _rows_dict_to_df(rows_dict):
    import numpy as _np
    n = len(rows_dict)
    pl = _np.empty(n, dtype=object)
    cl = _np.empty(n, dtype=object)
    pi = _np.empty(n, dtype=object)
    ci = _np.empty(n, dtype=object)
    sk = _np.empty(n, dtype=object)
    for i, ((Lp_id, l_id), (P, item_I, sku)) in enumerate(rows_dict.items()):
        pl[i] = Lp_id; cl[i] = l_id; pi[i] = P; ci[i] = item_I; sk[i] = sku
    df = pd.DataFrame({
        "producer_lot": pl, "producer_item": pi,
        "consumer_lot": cl, "consumer_item": ci, "via_sku": sk,
    })
    # DETERMINISM (E6): rows_dict is INSERTION-ordered, and insertion order came from
    # the (now sorted) loops above. Sorting on the edge key as well makes the file
    # byte-stable regardless of how the dict was filled, so `md5 phase3_dag_edges.csv`
    # is a valid regression check. (producer_lot, consumer_lot) is UNIQUE by
    # construction -- it is rows_dict's key -- so this is a total order and no tie-break
    # is needed. Content is untouched; only row order.
    return df.sort_values(["producer_lot", "consumer_lot"],
                          kind="mergesort").reset_index(drop=True)


def compute_degrees(lots, edges):
    """For each lot: degrees + carry FRC campaign + aging cols for Phase 5."""
    extra_cols = [c for c in [
        "is_belt", "wire_type", "campaign_id",
        "is_campaign_start", "is_campaign_end",
        "first_need", "last_need", "min_aging_h", "max_aging_h",
    ] if c in lots.columns]
    base_cols = ["lot_id","item_code","operation","duration_hours"] + extra_cols

    if edges.empty:
        df = lots[base_cols].copy()
        df["n_predecessors"] = 0
        df["n_successors"]   = 0
        df["is_root"]        = True
        df["is_terminal"]    = True
        return df
    succ = edges.groupby("producer_lot").size().rename("n_successors")
    pred = edges.groupby("consumer_lot").size().rename("n_predecessors")
    df = lots[base_cols].copy()
    df = df.merge(succ, left_on="lot_id", right_index=True, how="left").fillna({"n_successors":0})
    df = df.merge(pred, left_on="lot_id", right_index=True, how="left").fillna({"n_predecessors":0})
    df["is_root"]     = df["n_predecessors"] == 0
    df["is_terminal"] = df["n_successors"]   == 0
    df["n_successors"]   = df["n_successors"].astype(int)
    df["n_predecessors"] = df["n_predecessors"].astype(int)
    return df


def compute_depth(lots, edges):
    if edges.empty:
        return {lid: 0 for lid in lots["lot_id"].values}
    succ_of = defaultdict(list)
    pred_of = defaultdict(list)
    for u, v in zip(edges["producer_lot"].values, edges["consumer_lot"].values):
        succ_of[u].append(v)
        pred_of[v].append(u)
    all_lots = lots["lot_id"].values
    in_deg = {l: len(pred_of[l]) for l in all_lots}
    depth  = {l: 0 for l in all_lots}
    q = deque([l for l, d in in_deg.items() if d == 0])
    while q:
        u = q.popleft()
        du = depth[u]
        for v in succ_of[u]:
            if du + 1 > depth[v]: depth[v] = du + 1
            in_deg[v] -= 1
            if in_deg[v] == 0: q.append(v)
    return depth


def build_summary(lots, edges, deg, depth, child_to_parents):
    overview = pd.DataFrame([
        ("Total lots (nodes)",          f"{len(lots):,}"),
        ("Total edges",                 f"{len(edges):,}"),
        ("Root lots (no predecessor)",  f"{int(deg['is_root'].sum()):,}"),
        ("Terminal lots (no successor)",f"{int(deg['is_terminal'].sum()):,}"),
        ("Isolated lots",               f"{int(((deg['is_root'])&(deg['is_terminal'])).sum()):,}"),
        ("Max DAG depth (levels)",      f"{max(depth.values()) if depth else 0}"),
        ("Avg predecessors per lot",    f"{deg['n_predecessors'].mean():.2f}"),
        ("Avg successors per lot",      f"{deg['n_successors'].mean():.2f}"),
        ("Distinct BOM edge keys",      f"{len(child_to_parents):,}"),
    ], columns=["metric","value"])

    by_depth = pd.DataFrame([{"depth_level":d, "n_lots": sum(1 for v in depth.values() if v==d)}
                              for d in sorted(set(depth.values()))])

    by_op = (deg.groupby("operation").agg(
        n_lots=("lot_id","count"),
        avg_pred=("n_predecessors","mean"),
        avg_succ=("n_successors","mean"),
        n_root=("is_root","sum"),
        n_terminal=("is_terminal","sum"),
    ).reset_index().sort_values("n_lots", ascending=False).round(2))

    edges_by_sku = (edges.groupby("via_sku").size().reset_index(name="n_edges")
                          .sort_values("n_edges", ascending=False).head(50)) if not edges.empty else pd.DataFrame()

    # NEW: campaign summary (counts of belt-lot campaign nodes if present)
    camp_overview = pd.DataFrame()
    if "campaign_id" in deg.columns:
        belt = deg[deg["is_belt"].astype(bool)]
        if not belt.empty:
            camp_overview = belt.groupby(["wire_type","campaign_id"]).agg(
                n_lots=("lot_id","count"),
                n_terminal=("is_terminal","sum"),
                n_root=("is_root","sum"),
            ).reset_index().sort_values(["wire_type","campaign_id"])

    return overview, by_depth, by_op, edges_by_sku, camp_overview


# ══════════════════════════════════════════════════════════════════════════════════════
#  CTP DEVIATION #33 (F1) — PER-EDGE CONSUMPTION QUANTITY.  config: edge_consume_qty
# ══════════════════════════════════════════════════════════════════════════════════════
# THE DEFECT. `build_dag_edges` is a COMPATIBILITY FAN-LINK, not an allocation: a child
# lot is linked to EVERY parent lot that (a) shares a routed sku, (b) overlaps a curing
# block, and (c) sits inside |first_need gap| <= max_aging(child) - 8 h  (:385-:414).
# For CAP 66 - CAPSTRIP (max_aging 24 h -> +/-16 h window) that binds FIVE cap-strip lots
# to one green-tyre lot. The edge carries NO QUANTITY, so phase5's material gate
# (_material_ready_ns, :1782) can only form `need` as the SUM OF THE CONSUMER'S BOUND
# PREDECESSOR LOT QUANTITIES. MEASURED on the shipped run:
#     need declared this way, all items : 787,917,887 units
#     TRUE consumption from the BOM     :  27,415,139 units      -> 28.7x OVERSTATED
#     GT 1925 XPC1_L0001 (310 tyres), CAP 66 - CAPSTRIP:
#          declared 504,159 m   true 310 x 73.018 = 22,636 m   -> 22.3x
#          earliest bound cap-strip lot alone = 101,501 m, ready T0+5.48 h (4.5x enough)
#          the gate nonetheless waited for the 5th lot at T0+37.60 h.
# Because cum(supply) can only reach a need equal to the sum of ALL preds at the LAST
# pred, today's gate is arithmetically identical to "wait for every predecessor lot".
#
# THE FIX (this function). The edge SET is untouched -- topology, degrees, depth, phase4's
# backward CPM and every LFT are BIT-IDENTICAL. Only a `consume_qty` COLUMN is added:
#   1. true_need(consumer c, child item i) = lot_qty(c) * rate, where
#          rate = SUM_{s in skus_served(c)} gross_demand(i, s)
#               / SUM_{s in skus_served(c)} gross_demand(parent_item(c), s)
#      read from outputs2/phase1_demand_updated.csv.gz -- the phase-1b explosion, which is
#      the BOM ratio per unit of parent for exactly the skus this lot serves. NOTHING IS
#      INVENTED: no rate, no default. A group whose denominator is 0 falls back to the OLD
#      declared sum (bit-identical to today for that group) and is COUNTED and PRINTED.
#   2. that need is FEFO-ALLOCATED across the consumer's bound producers -- consumers in
#      (first_need, lot_id) order, producers in (first_need, lot_id) order, each producer
#      carrying a GLOBAL remaining balance so no producer is promised twice.
#   3. an unallocated residual (real shortage) is charged to the LAST bound producer, so
#      SUM(consume_qty) == true_need EXACTLY and the gate degenerates to strict precedence
#      for that group -- i.e. it FAILS SAFE, never open. Count printed.
#   Edges that receive no allocation keep consume_qty = 0.0 and are still emitted.
#
# WHY QUANTITY AND NOT THE LINK. Removing the surplus links is what `dag_quantity_allocation`
# does, and it was MEASURED AND REJECTED (config: component FEFO 43.86% -> 47.05%). The link
# is not wrong -- "this cap-strip lot COULD feed this GT lot" is exactly the compatibility
# relation phase4's backward CPM needs to stay feasible. What is wrong is READING a
# compatibility relation AS AN ALLOCATION. Fixing the quantity leaves phase4 untouched;
# fixing the link moves every feeder LFT. Smaller blast radius, and it repairs the number
# that is actually false.
def annotate_edge_consume_qty(edges, lots, skus_per_lot, demand_path_candidates):
    import time as _t
    _t0 = _t.time()
    EPS = 1e-6
    if edges.empty:
        edges["consume_qty"] = []
        return edges

    dem = None
    for p in demand_path_candidates:
        if p.exists():
            try:
                dem = pd.read_csv(p)
                print(f"    F1: gross demand from {p.name}  rows={len(dem):,}", flush=True)
                break
            except Exception as e:
                print(f"    F1: failed {p.name}: {e}")
    if dem is None or not {"item_code", "sku", "demand_qty"}.issubset(dem.columns):
        print("    F1: !! phase1_demand_updated not readable -- consume_qty NOT emitted; "
              "phase5 will fall back to the v6 lot-sum need.")
        return edges

    dem["item_code"] = dem["item_code"].astype(str).str.strip()
    dem["sku"] = dem["sku"].astype(str).str.strip()
    dem["demand_qty"] = pd.to_numeric(dem["demand_qty"], errors="coerce").fillna(0.0)
    dem_by_item = defaultdict(dict)
    for (ic, s), q in dem.groupby(["item_code", "sku"])["demand_qty"].sum().items():
        dem_by_item[ic][s] = float(q)

    lots = lots.copy()
    lots["item_code"] = lots["item_code"].astype(str).str.strip()
    lot_qty = dict(zip(lots["lot_id"],
                       pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)))
    lot_item = dict(zip(lots["lot_id"], lots["item_code"]))
    _fn = pd.to_datetime(lots["first_need"], errors="coerce")
    BIGT = pd.Timestamp.max
    lot_fn = {l: (BIGT if pd.isna(v) else v) for l, v in zip(lots["lot_id"], _fn)}

    _skt = {}
    for l, s in skus_per_lot.items():
        _skt[str(l)] = tuple(sorted(s)) if s else ()

    # ── ALLOCATION FIX (2026-09-27, RCA day-27 TBR) ─ config edge_alloc_fix ────────────────
    # (a) The rate  SUM_s gross(child,s) / SUM_s gross(parent,s)  charges a consumer with the
    #     child demand of EVERY route in its SKUs: when a child has several parents (B176 ->
    #     CP31151 AND CWBHI34; GUM STRIP -> GT AND STC belts) each parent lot was charged the
    #     others' share -> over-allocation, and a day-N consumer spilled onto a later-day lot.
    #     Use the PER-EDGE BOM quantity instead:  rate = SUM_s w_s q_s(P->i)/r_s(P) / SUM_s w_s
    #     (w_s = parent's gross demand for SKU s; q per tyre on that edge; r_s(P) = per-tyre qty
    #     of the parent = SUM of its incoming edges) -- the same BOM rule as phase1b.
    # (b) An uncovered residual is charged to the NEXT producer lot in time (earliest one needed
    #     at/after the consumer), not to the LAST bound lot (which made a small gap a 2-day wait).
    _EFIX = bool((load_cfg() or {}).get("edge_alloc_fix", False))
    _qe, _rp = {}, {}
    if _EFIX:
        try:
            import mrp_time_phased as _tpm
            _b = pd.read_csv(INPUTS / "bom.csv", dtype=str)
            _et = _tpm._edge_table(_b, {})
            _qe = {(s_, p_, c_): q_ for s_, p_, c_, q_ in zip(_et["sku"], _et["par"], _et["ch"], _et["q"])}
            _rpd = _et.groupby(["sku", "ch"])["q"].sum()
            _rp = {(s_, c_): float(v) for (s_, c_), v in _rpd.items()}
            print(f"    F1 ALLOC FIX: per-edge BOM rates for {len(_qe):,} (sku,parent,child) edges; "
                  f"residual -> next producer in time", flush=True)
        except Exception as _e:
            print(f"    F1 ALLOC FIX: !! BOM edge table failed ({_e}) -- legacy rate kept")
            _EFIX = False

    def _edge_rate(P, i, S):
        num = den = 0.0
        for s_ in S:
            w = dem_by_item.get(P, {}).get(s_, 0.0)
            q = _qe.get((s_, P, i)); r = _rp.get((s_, P))
            if w > 0 and q and r:
                num += w * q / r; den += w
        return (num / den) if den > 0 else None

    _den_cache = {}
    def _den(parent, S):
        k = (parent, S)
        v = _den_cache.get(k)
        if v is None:
            d = dem_by_item.get(parent, {})
            v = sum(d.get(s, 0.0) for s in S)
            _den_cache[k] = v
        return v

    grp = defaultdict(list)                       # (consumer_lot, child_item) -> [producer lots]
    for p, c, pi in zip(edges["producer_lot"], edges["consumer_lot"], edges["producer_item"]):
        grp[(c, str(pi).strip())].append(p)

    rem = {}                                      # producer lot -> unallocated quantity left
    for p in edges["producer_lot"].unique():
        rem[p] = float(lot_qty.get(p, 0.0) or 0.0)

    consumers = sorted({c for (c, _i) in grp}, key=lambda c: (lot_fn.get(c, BIGT), str(c)))
    items_of = defaultdict(list)
    for (c, i) in grp:
        items_of[c].append(i)

    edge_q = {}
    n_norate = n_short = 0
    short_units = 0.0
    tot_true = tot_declared = 0.0
    for c in consumers:
        S = _skt.get(str(c), ())
        P = lot_item.get(c, "")
        den = _den(P, S) if S else 0.0
        for i in sorted(items_of[c]):
            plist = sorted(grp[(c, i)], key=lambda p: (lot_fn.get(p, BIGT), str(p)))
            declared = sum(float(lot_qty.get(p, 0.0) or 0.0) for p in plist)
            tot_declared += declared
            num = sum(dem_by_item.get(i, {}).get(s, 0.0) for s in S) if S else 0.0
            _er = _edge_rate(P, i, S) if (_EFIX and S) else None
            if _er is not None:
                need = float(lot_qty.get(c, 0.0) or 0.0) * _er
            elif den <= 0.0 or num <= 0.0:
                need = declared                    # FALLBACK = today's number, bit-identical
                n_norate += 1
            else:
                need = float(lot_qty.get(c, 0.0) or 0.0) * (num / den)
            tot_true += need
            left = need
            for p in plist:
                if left <= EPS:
                    edge_q[(p, c)] = 0.0
                    continue
                take = rem.get(p, 0.0)
                if take > left:
                    take = left
                if take > 0.0:
                    rem[p] = rem.get(p, 0.0) - take
                    left -= take
                edge_q[(p, c)] = take
            if left > EPS and plist:
                _tgt = plist[-1]
                if _EFIX:
                    _cfn = lot_fn.get(c, BIGT)
                    _nxt = [p_ for p_ in plist if lot_fn.get(p_, BIGT) >= _cfn]
                    _tgt = _nxt[0] if _nxt else plist[-1]
                edge_q[(_tgt, c)] = edge_q.get((_tgt, c), 0.0) + left
                n_short += 1
                short_units += left

    edges = edges.copy()
    edges["consume_qty"] = [float(edge_q.get((p, c), 0.0))
                            for p, c in zip(edges["producer_lot"], edges["consumer_lot"])]
    n_zero = int((edges["consume_qty"] <= EPS).sum())
    print(f"    F1 CONSUME-QTY: declared(lot-sum) {tot_declared:,.0f} -> TRUE consumption "
          f"{tot_true:,.0f}  ({tot_declared/max(tot_true,1):.1f}x overstated)")
    print(f"    F1 CONSUME-QTY: {len(edges):,} edges | {n_zero:,} carry 0 (not needed by that "
          f"consumer) | groups with NO demand rate (fallback to lot-sum): {n_norate:,} | "
          f"groups short of bound supply (residual charged to LAST producer -> strict "
          f"precedence): {n_short:,} ({short_units:,.0f} units)  [+{_t.time()-_t0:.1f}s]",
          flush=True)
    return edges


def main():
    print("\n" + "=" * 65)
    print("  PHASE 3 — DAG Construction  (FRC-aware)")
    print("=" * 65)
    cfg = load_cfg()
    fp = cfg["files"]

    import time as _gt
    _g0 = _gt.time()
    print("  Loading inputs...", flush=True)
    bom = _db_or_csv("bom", cfg).rename(columns={
        "Super_parent":"Super parent","Parent_qty":"Parent qty",
        "Parent_unit":"Parent unit","child_quantity":"child quantity",
        "child_Unit":"child Unit"})
    lots = load_lots()
    # MG assignment REMOVED 2026-07-22 (user instruction) — mg_assignment.csv is no longer
    # read; CTP's single-variant BOM made the MG filter and (sku, mg, *) keys inert.
    print(f"    BOM rows: {len(bom):,}   Lots: {len(lots):,}")
    if "is_belt" in lots.columns:
        n_belt = int(lots["is_belt"].astype(bool).sum())
        print(f"    Belt lots (FRC): {n_belt:,}")

    print(f"  [+{_gt.time()-_g0:.1f}s]", flush=True)
    print("  Step 1: build BOM edge index (per SKU)...")
    edges_by_sku, child_to_parents, parent_to_children = build_bom_edges(bom)
    print(f"    SKUs with edges: {len(edges_by_sku):,}")
    print(f"    Distinct child-parent keys: {len(child_to_parents):,}")

    print(f"  [+{_gt.time()-_g0:.1f}s]", flush=True)
    print("  Step 2a: load EXACT lot-SKU mapping from Phase 2...")
    skus_per_lot = load_lot_skus()
    if skus_per_lot is None:
        plan_curing = load_plan_curing(_db_or_csv("plan", cfg))
        skus_per_lot = derive_skus_served(lots, plan_curing)
    n_with_skus = sum(1 for v in skus_per_lot.values() if v)
    print(f"    Lots with >=1 SKU mapping: {n_with_skus:,} / {len(lots):,} "
          f"({n_with_skus/max(len(lots),1)*100:.1f}%)")

    print(f"  [+{_gt.time()-_g0:.1f}s]", flush=True)
    print("  Step 2c: load curing-block map per lot (for block-aligned edges)...")
    blocks_per_lot = load_blocks_per_lot()

    print(f"  [+{_gt.time()-_g0:.1f}s]", flush=True)
    print("  Step 2b: index lots by item...")
    lots_by_item = index_lots(lots, skus_per_lot, blocks_per_lot)
    print(f"    Distinct items lotted: {len(lots_by_item):,}")

    print(f"  [+{_gt.time()-_g0:.1f}s]", flush=True)
    print("  Step 3: build DAG edges (producer -> consumer)...")
    # F1: load aging master for aging-aware edge filtering.
    aging_map_for_edges = None
    try:
        am = _db_or_csv("aging_master", cfg)
        aging_map_for_edges = {}
        for _, r in am.iterrows():
            code = str(r["ItemCode"]).strip()
            if not code: continue
            mx = r["MaxAging"]
            mxu = str(r.get("MaxAgingUnit", "Hours") or "Hours").upper()
            try:
                v = float(mx)
            except (TypeError, ValueError):
                continue
            if mxu.startswith("DAY"):  v *= 24.0
            elif mxu.startswith("MIN"): v /= 60.0
            mn = r["MinAging"]
            mnu = str(r.get("MinAgingUnit", "Hours") or "Hours").upper()
            try:
                mv = float(mn)
            except (TypeError, ValueError):
                mv = 0.0
            if mnu.startswith("DAY"): mv *= 24.0
            elif mnu.startswith("MIN"): mv /= 60.0
            aging_map_for_edges[code] = (mv, v)
        print(f"    aging_map for F1: {len(aging_map_for_edges):,} items loaded")
    except Exception as _e:
        print(f"    aging_map for F1 not loaded ({_e}) - F1 aging filter DISABLED")
    # ═══ LEVER 2 / S6 -- FEFO QUANTITY-ALLOCATION DAG (config flag, default OFF) ═══════
    # OFF => v6's fan-link build_dag_edges (byte-identical). ON => the quantity-allocated
    # DAG that stops feeder LFTs collapsing to the wave-start. See build_dag_edges_qty_alloc.
    _dag_qty = bool(cfg.get("dag_quantity_allocation", False))
    if _dag_qty:
        lot_qty_map = dict(zip(lots["lot_id"],
                               pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)))
        # BOM child qty per (Parent item, child item) -- SUM duplicates, exactly as the
        # FEFO aging auditor does (MEMORY: v6 BOM dedup bug -> SUM, not first).
        _bom = bom.copy()
        _bom["Parent"] = _bom["Parent"].astype(str).str.strip()
        _bom["child"]  = _bom["child"].astype(str).str.strip()
        _cq_col = "child quantity" if "child quantity" in _bom.columns else "child_quantity"
        _bom[_cq_col] = pd.to_numeric(_bom[_cq_col], errors="coerce")
        bom_child_qty = (_bom.dropna(subset=[_cq_col])
                         .groupby(["Parent", "child"])[_cq_col].sum().to_dict())
        print(f"  Step 3: DAG QUANTITY-ALLOCATION mode ON (S6) -- "
              f"{len(bom_child_qty):,} (parent,child) BOM qty keys, "
              f"{len(lot_qty_map):,} lot quantities")
        edges = build_dag_edges_qty_alloc(lots_by_item, child_to_parents,
                                          lot_qty_map, bom_child_qty,
                                          aging_map=aging_map_for_edges, aging_safety_h=8.0)
    else:
        edges = build_dag_edges(lots_by_item, child_to_parents,
                                 aging_map=aging_map_for_edges, aging_safety_h=8.0)
    print(f"    Edges generated: {len(edges):,}")

    # ── CTP #33 (F1) — per-edge CONSUMPTION QUANTITY. DEFAULT OFF = no column emitted,
    #    phase5 falls back to the v6 lot-sum need and the file is byte-identical. ──
    if bool(cfg.get("edge_consume_qty", False)):
        print("  Step 3b: F1 per-edge consume_qty (edge_consume_qty: true) ...", flush=True)
        edges = annotate_edge_consume_qty(
            edges, lots, skus_per_lot,
            # STALE-FILE FIX (2026-09-26): newest first -- a leftover .gz from an earlier month
            # used to win here and every consume_qty rate came from LAST MONTH's demand.
            sorted([p for p in (OUTPUTS / _suff("phase1_demand.csv.gz"),
                                OUTPUTS / _suff("phase1_demand.csv")) if p.exists()],
                   key=lambda p: p.stat().st_mtime, reverse=True))
    else:
        print("  Step 3b: F1 consume_qty OFF (edge_consume_qty: false) -- v6 edge schema.")

    import time as _t4
    _t4_start = _t4.time()
    print("  Step 4: compute degrees & depth...", flush=True)
    deg = compute_degrees(lots, edges)
    depth = compute_depth(lots, edges)
    deg["depth"] = deg["lot_id"].map(depth).fillna(0).astype(int)
    print(f"    Root lots: {int(deg['is_root'].sum()):,}")
    print(f"    Terminal lots: {int(deg['is_terminal'].sum()):,}")
    if "is_belt" in deg.columns:
        belt_term = int(deg[(deg["is_belt"].astype(bool)) & (deg["is_terminal"])].shape[0])
        belt_root = int(deg[(deg["is_belt"].astype(bool)) & (deg["is_root"])].shape[0])
        print(f"    Belt lots: terminal={belt_term:,}  root={belt_root:,}")

    # === ORPHAN DIAGNOSTIC (production-scheduling expert) ===
    # An orphan lot has no successors AND its operation is NOT a terminal
    # (Building/Curing). Phase 2 created it from gross demand but Phase 1c
    # MRP cascade / Phase 3 F1 aging filter / block alignment dropped its
    # consumer linkage. These lots get scheduled by Phase 5 but consume
    # cutter/mixer time WITHOUT serving any downstream demand. Symptom:
    # upstream finishing AFTER downstream (e.g., WBC 6/1 18:21 vs Stage-2
    # 5/31 16:54 — physically impossible).
    TERMINAL_OPS = ("BUILD", "TBM", "CURING", "TYRE")
    def _is_terminal_op(opname):
        s = str(opname or "").upper()
        return any(t in s for t in TERMINAL_OPS)
    if "operation" in deg.columns:
        deg["_is_term"] = deg["operation"].apply(_is_terminal_op)
        orphan_mask = (deg["is_terminal"]) & (~deg["_is_term"])
        n_orphans = int(orphan_mask.sum())
        if n_orphans > 0:
            print(f"\n    *** ORPHAN LOTS: {n_orphans:,} non-terminal lots have NO successors ***")
            print(f"    These will be SKIPPED in Phase 5 (they have no consumer to feed).")
            print(f"    Top items with orphans:")
            orph_items = deg[orphan_mask].groupby("item_code").size().sort_values(ascending=False).head(10)
            for it, n in orph_items.items():
                print(f"      {str(it)[:35]:35s} : {n:>5,d} orphan lots")
        else:
            print(f"    Orphans: 0 (clean DAG)")
        deg = deg.drop(columns=["_is_term"])

    print(f"    [step 4 done in {_t4.time()-_t4_start:.1f}s]", flush=True)
    print("  Step 5: save outputs...", flush=True)
    _stage_csv(edges, "phase3_dag_edges.csv")
    _stage_csv(deg,   "phase3_lot_degrees.csv")

    overview, by_depth, by_op, edges_by_sku, camp_overview = build_summary(lots, edges, deg, depth, child_to_parents)
    sheets = {
        "00_overview":          overview,
        "01_depth_distribution":by_depth,
        "02_by_operation":      by_op,
        "03_top_SKUs_by_edges": edges_by_sku,
        "04_lot_degrees":       deg.head(50000),
        "05_sample_edges":      edges.head(50000),
    }
    if not camp_overview.empty:
        sheets["06_belt_campaigns"] = camp_overview
    if os.environ.get("PHASE3_WRITE_XLSX", "0") == "1":
        out = _stage_xlsx(sheets, "phase3_dag_summary.xlsx")
        print(f"     Summary XLSX: {out}")
    else:
        print(f"     [step 5] xlsx skipped (set PHASE3_WRITE_XLSX=1 to enable)")
    print(f"     Degrees CSV: {OUTPUTS/_suff('phase3_lot_degrees.csv')}")
    return 0


if __name__ == '__main__':
    import sys as _sys
    import traceback as _tb
    try:
        _rc = main()
        raise SystemExit(_rc if _rc is not None else 0)
    except SystemExit:
        raise
    except Exception:
        _sys.stderr.write(chr(10) + '!! PHASE CRASHED !!' + chr(10))
        _sys.stderr.flush()
        _tb.print_exc()
        _sys.exit(1)
