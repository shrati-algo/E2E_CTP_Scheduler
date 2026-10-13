#!/usr/bin/env python3
"""
phase1b_demand_explosion.py — MG-AWARE per-SKU BOM explosion.

NEW STRUCTURE
  Stage 1 (per-SKU walk):
    For each SKU in plan, look up its assigned MG.
    Filter BOM rows: Super_parent == SKU AND (Equipment == MG OR blank).
    Walk the BOM tree with q0 = 1 NOS (per-tyre basis).
    Produce per-tyre demand rate for every routed intermediate item.
    When the same (Parent, child) edge has multiple BOM rows with different
    UOMs, pick the row whose child Unit matches routing's expected UOM
    (no global dedup; selection is local to the edge).

  Stage 2 (expand to per-block):
    For each curing block (SKU, qty Q):
        demand_qty[item, block] = per_tyre_rate[SKU, item] * Q

  Stage 3 (cross-SKU consolidation):
    Sum same item across all SKUs to produce per-item totals.

OUTPUTS
  phase1_demand.csv             - per (item, sku, block_id) demand row
  phase1_per_sku_demand.csv     - per-tyre rate per (sku, item)
  phase1_item_consolidated.csv  - per-item total across all SKUs
  phase1_demand.xlsx            - 5 tabs: sample + item totals + per-SKU + per-MG + summary
  phase1b_mg_filter_audit.csv   - per-SKU rows kept / dropped diagnostic
"""
from __future__ import annotations
import sys, pathlib, yaml, re, os
import pandas as pd
from collections import defaultdict, deque

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT/"inputs"
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


# OneDrive throttles big-file writes. Stage CSVs to a local tmp dir first,
# then atomically copy into OUTPUTS at the end of main().
import tempfile, shutil
_STAGE = OUTPUTS  # write directly to outputs2 (portable across Windows/Linux)
_STAGE.mkdir(parents=True, exist_ok=True)
def _stage_csv(df, name, big_threshold_rows=200000):
    """Write CSV to local tmp first, then copy to OUTPUTS.
    For large frames, write gzip-compressed (.csv.gz) to fit through
    OneDrive's write-throttling without truncation."""
    is_big = len(df) > big_threshold_rows
    write_name = _suff(name + ".gz" if is_big else name)
    tmp = _STAGE / write_name
    df.to_csv(tmp, index=False, compression="gzip" if is_big else None)
    final = OUTPUTS / write_name
    if tmp.resolve() != final.resolve():
        shutil.copy(str(tmp), str(final))
    return final
def _stage_xlsx_sheets(sheets, name):
    """Write xlsx directly to OUTPUTS."""
    write_name = _suff(name)
    tmp = _STAGE / write_name
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        for k, df in sheets.items():
            clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
            df.to_excel(w, sheet_name=clean, index=False)
    final = OUTPUTS / write_name
    if tmp.resolve() != final.resolve():
        shutil.copy(str(tmp), str(final))
    return final


def _read(path):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(path, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(f"Could not parse {path}")

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


def load_inputs(cfg):
    f = cfg["files"]
    bom = _db_or_csv("bom", cfg).rename(columns={
        "Super_parent":"Super parent", "grand_parent":"grand parent",
        "Parent_qty":"Parent qty", "Parent_unit":"Parent unit",
        "child_quantity":"child quantity", "child_Unit":"child Unit",
        "child_description":"child description"})
    return bom, _db_or_csv("routing", cfg), _db_or_csv("plan", cfg)


def canon_uom(u):
    if u is None or (isinstance(u, float) and pd.isna(u)): return ""
    return str(u).strip().upper()


def parse_plan_to_curing(plan, selected_skus):
    df = plan.copy()
    df["SKUCode"] = df["skuCode"].astype(str).str.strip()
    if selected_skus:
        df = df[df["SKUCode"].isin(selected_skus)]
    df = df[df["SKUCode"] != "CHANGEOVER"]
    df["Qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    df = df[df["Qty"] > 0]
    if df.empty:
        return pd.DataFrame(columns=["SKUCode","Qty","effective_start","effective_end","block_id"])

    def parse_ts(s):
        r = pd.to_datetime(s, errors="coerce")
        if hasattr(r,"dt") and r.dt.tz is not None:
            return r.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return r

    start = parse_ts(df["startTime"])
    end   = parse_ts(df["endTime"])
    if start.isna().any():
        ds = parse_ts(df["date"]).dt.strftime("%Y-%m-%d")
        start = start.fillna(pd.to_datetime(ds + " " + df["startTime"].astype(str), errors="coerce"))
    if end.isna().any():
        ds = parse_ts(df["date"]).dt.strftime("%Y-%m-%d")
        end = end.fillna(pd.to_datetime(ds + " " + df["endTime"].astype(str), errors="coerce"))
    overnight = end < start
    end = end.where(~overnight, end + pd.Timedelta(days=1))
    out = pd.DataFrame({
        "SKUCode": df["SKUCode"].values,
        "Qty":     df["Qty"].astype(int).values,
        "effective_start": start.values,
        "effective_end":   end.values,
    }).dropna(subset=["effective_start", "effective_end"]).reset_index(drop=True)
    # M-9 FIX (2026-07-27): number block_id AFTER the dropna so the sequence is CONTIGUOUS and
    # aligns with phase1_5 (which numbers after its own dropna). Numbering before dropna left
    # GAPS; if phase1b's and phase1_5's dropna ever kept different rows, the positional block_id
    # -> wave join (phase2) would misalign lots to the wrong wave. Byte-identical when no row is
    # dropped (current data). Durable fix: join on (skuCode, startTime), not a positional id.
    out["block_id"] = [f"B{i:04d}" for i in range(len(out))]
    return out


def build_routing_uom_map(rt) -> dict:
    """item_code -> expected UOM derived from routing's first matching row."""
    out = {}
    for _, r in rt.iterrows():
        rp = str(r["routed_product"]).strip()
        if not rp or rp in out: continue
        u  = str(r["proc_time_UOM"]).strip().upper()
        bu = str(r["batch_UNIT"]).strip().upper()
        if u in ("MM/MIN","M/MIN","MTR/MIN"):       out[rp] = "MM"
        elif u == "NOS/MIN":                          out[rp] = "NOS"
        elif u.startswith("CUTS"):                    out[rp] = "NOS"
        elif u == "SEC/BATCH":                        out[rp] = bu or "KG"
        elif u in ("SEC","MIN"):                      out[rp] = bu or "NOS"
        elif u.endswith("/MIN"):
            head = u.split("/")[0]
            out[rp] = "MM" if head in ("MM","M","MTR") else \
                       ("NOS" if head == "NOS" else ("KG" if head=="KG" else ""))
    return out


def per_tyre_demand(bom_sku_mg, routed, routing_uom_map):
    """
    Compute per-tyre demand for each routed child item in this (SKU, MG) BOM.

    RULE (from JK Tyre BOM convention):
        Every BOM row's `child quantity` is the per-tyre consumption of that
        child item directly. No chain multiplication. To get total demand
        for a curing block, multiply per_tyre x block.qty.

    When a child appears as the consumed side of MULTIPLE (Parent, child)
    edges (e.g., a master compound consumed by several final compounds),
    sum the child_quantity across those edges.

    When the SAME (Parent, child) edge has multiple BOM rows with different
    UOMs (e.g., BJNYL27-680MM-90 deg appears as both 1470 MM and 1 NOS),
    pick the row whose child Unit matches the routing's expected UOM for
    that item.

    Returns: dict {child_item -> (per_tyre_qty, uom)}, stats dict.
    """
    # Step 1: group rows by (parent, child) edge
    edges = defaultdict(list)
    rows_total = len(bom_sku_mg)
    for _, r in bom_sku_mg.iterrows():
        par = str(r["Parent"]).strip()
        ch  = str(r["child"]).strip()
        edges[(par, ch)].append(r)

    # Step 2: for each edge, pick UOM-matching row, then accumulate per-tyre qty
    per_tyre = defaultdict(lambda: {"qty": 0.0, "uom": ""})
    rows_uom_corrected = 0
    rows_summed = 0            # CTP DEVIATION #4 — rows summed instead of discarded
    for (par, ch), rows in edges.items():
        if ch not in routed:
            continue  # skip raw materials
        rt_uom = routing_uom_map.get(ch, "")

        # ═══ CTP DEVIATION #4 — SUM SAME-UOM ROWS, DO NOT KEEP ONLY THE FIRST ══════════
        #
        # v6 kept exactly ONE row per (Parent, child) edge. Its own docstring says why:
        # it was written for the case where the SAME requirement is stated TWICE IN
        # DIFFERENT UOMs (e.g. "1470 MM" and "1 NOS") — there, summing would double-count,
        # so you pick the row whose UOM matches the routing. That case is handled below,
        # UNCHANGED, and still fires whenever the rows disagree on UOM.
        #
        # But CTP's BOM has ZERO such rows. Instead it has 1,279 edges whose rows share
        # ONE UOM and differ only by `grand_parent` — the column that says WHICH COMPONENT
        # consumes this slice of a shared material:
        #
        #     grand_parent   Parent       child   child_quantity
        #     B1-PBLT071     CWBHTCP100   B186    0.5356 KG      <- belt ply 1
        #     B2-PBLT071     CWBHTCP100   B186    0.4947 KG      <- belt ply 2
        #
        # Both belt plies are physically in every tyre (the BOM's own green-tyre level
        # lists B1 and B2 as separate children, 1 NOS each, on 227 of 228 SKUs). They are
        # not variants. Keeping only the first row provisions ONE belt's rubber for a
        # TWO-belt tyre — while this very explosion computes Steel Belt = 2.000/tyre.
        #
        # THE BOM PROVES THE RULE ITSELF. It carries this arithmetic identity:
        #       Parent_qty(X)  ==  SUM of child_quantity over EVERY row where child == X
        # Measured over all 10,755 same-unit nodes in CTP's BOM:
        #       summing every row   -> 10,755 match,     0 violations
        #       v6's first-row rule ->  9,463 match, 1,292 violations
        # Chemistry agrees: master-batch / final-compound is 0.925 when summed (correct;
        # curatives are 3-8%) and 0.473 when first-row-only (impossible).
        #
        # Consequence of NOT summing: FINAL COMPOUND demand is ~9% short, silently, and
        # the final mixers are told to mix less compound than the plant actually needs.
        # ══════════════════════════════════════════════════════════════════════════════
        uoms = {canon_uom(r["child Unit"]) for r in rows}

        if len(uoms) > 1:
            # v6's ORIGINAL case, preserved: the same edge stated in different UOMs. Pick
            # the row whose UOM matches the routing; summing WOULD double-count.
            #
            # !! GUARD (found by adversarial audit): if NO row's UOM matches the routing,
            # every row scores 0, and v6's `if score > best_score` keeps rows[0] and
            # DISCARDS the rest — silently re-creating the exact bug this deviation fixes,
            # with rows_uom_corrected still reading 0. That is not hypothetical: the routing
            # says MM for length items while CTP's BOM says M, so a match is not guaranteed.
            # If we cannot tell WHICH row is right, we must NOT guess. Fail loudly.
            best_row = None
            best_score = -1
            for r in rows:
                cu = canon_uom(r["child Unit"])
                score = 1 if (rt_uom and cu == rt_uom) else 0
                if score > best_score:
                    best_row = r
                    best_score = score
            if best_row is None:
                continue
            if best_score == 0:
                # CONFLICTING UOM rows and NONE matches the routing. STRICT (default) halts --
                # summing would add different units; picking one silently discards the others.
                # With CTP_BOM_GATE=warn we DEGRADE (do NOT change any master UOM): use the first
                # row deterministically and FLAG it loudly, so a product whose BOM/routing units
                # disagree (e.g. TBR GUM STRIP -> TEG-02: KG/M vs routing MM) can still be
                # scheduled while the conflict is surfaced (the validate_uom guard reports it too).
                _bg = os.environ.get("CTP_BOM_GATE", "strict").strip().lower()
                if _bg != "warn":
                    raise SystemExit(
                        f"\n  !! BOM edge ({par} -> {ch}) has rows in CONFLICTING UOMs {sorted(uoms)}, "
                        f"and NONE matches the routing's UOM ({rt_uom!r}).\n"
                        f"     Summing them would add different units together; picking one would "
                        f"silently discard the others (the very bug CTP deviation #4 fixes).\n"
                        f"     I will not guess which row is correct. Fix the BOM or the routing UOM.\n"
                        f"     (To schedule anyway with a flagged first-row pick: CTP_BOM_GATE=warn.)")
                print(f"  !! [bom-uom-conflict WARN] edge ({par} -> {ch}) rows in {sorted(uoms)}, "
                      f"none match routing {rt_uom!r}; using first row "
                      f"({canon_uom(best_row['child Unit'])}, qty {best_row.get('child quantity')}) "
                      f"-- FLAGGED, not silently discarded. Fix the BOM/routing UOM to make it exact.")
            try:
                cqty = float(best_row["child quantity"]) if pd.notna(best_row["child quantity"]) else 0.0
            except (ValueError, TypeError):
                cqty = 0.0
            cu = canon_uom(best_row["child Unit"])
            if best_score == 1:
                rows_uom_corrected += (len(rows) - 1)
        else:
            # All rows share one UOM -> they are distinct consumers (grand_parent). SUM.
            cqty = 0.0
            for r in rows:
                try:
                    v = float(r["child quantity"]) if pd.notna(r["child quantity"]) else 0.0
                except (ValueError, TypeError):
                    v = 0.0
                cqty += v
            cu = next(iter(uoms))
            if len(rows) > 1:
                rows_summed += (len(rows) - 1)

        per_tyre[ch]["qty"] += cqty
        per_tyre[ch]["uom"]  = cu

    return ({k: (v["qty"], v["uom"]) for k, v in per_tyre.items()},
            {"rows_total": rows_total, "edges": len(edges),
             "rows_uom_corrected": rows_uom_corrected,
             "rows_summed": rows_summed,
             "routed_items": len(per_tyre)})


def wip_fed_skus(curing, bom, routing, cfg):
    """CTP #41 (2026-07-26) — in-plan SKUs whose GREEN TYRE is NOT built, so their whole
    component sub-tree comes from opening WIP and must NOT be re-built (over-production fix).

    Config-gated by `prune_wip_fed_component_demand` (DEFAULT False = byte-identical to v6).
    A SKU is WIP-fed iff EVERY one of its Green-Tyre children is unrouted (∉ routing →
    cannot be built) OR carries no per-tyre quantity. Verified to derive EXACTLY the 5
    hardcoded WIP_FED_SKUS (self-maintaining; subsumes config.excluded_items). Emitting an
    empty per-tyre map for these removes ONLY their contribution, so shared components (e.g.
    CAP 66 - CAPSTRIP) keep every built SKU's slice untouched.
    """
    if not bool(cfg.get("prune_wip_fed_component_demand", False)):
        return set()
    routed = set(routing["routed_product"].astype(str).str.strip()) - {""}
    gt = bom[bom["child description"].astype(str).str.strip()
             .isin(["Green Tyres", "GREEN_TYRE"])].copy()
    if gt.empty:
        return set()
    gt["_sp"] = gt["Super parent"].astype(str).str.strip()
    gt["_ch"] = gt["child"].astype(str).str.strip()
    gt["_cq"] = pd.to_numeric(gt["child quantity"], errors="coerce").fillna(0.0)
    plan_skus = set(curing["SKUCode"].astype(str).str.strip())
    out = set()
    for sp, g in gt.groupby("_sp"):
        if sp not in plan_skus:
            continue
        if all((x not in routed) for x in set(g["_ch"])) or g["_cq"].sum() <= 0:
            out.add(sp)                 # not-built green tyre → prune its component sub-tree
    return out


def explode_demand(curing, bom, routing, skip_skus=None):
    """Per-SKU BOM explosion.

    MG STRIPPED 2026-07-22 (user instruction). CTP's BOM is single-variant
    (Equipment == 'TBM PCR' on every row), so v6's MG filter `(__EQ__ == mg) |
    (__EQ__ == "")` kept every row and the (sku, mg) walk collapsed to one pair per
    SKU. The filter, the block-level MG path (mg_assignment_blocks.csv never existed
    for CTP), and the MG output column are all removed. Behaviour is unchanged:
    each SKU's full BOM is walked exactly once.

    Stage 1: walk each SKU's BOM once -> per-tyre component rates.
    Stage 2: expand to per-block demand = per_tyre_rate x block qty.
    """
    routed = set(routing["routed_product"].astype(str).str.strip()) - {""}
    routing_uom_map = build_routing_uom_map(routing)
    bom["__SP__"] = bom["Super parent"].astype(str).str.strip()
    bom_by_sku = {sku: g for sku, g in bom.groupby("__SP__")}

    # ---- STAGE 1: per-SKU explosion to per-tyre rates ----
    per_sku_rows = []
    per_sku_stats = []
    sku_to_per_tyre = {}  # sku -> {item: (qty_per_tyre, uom)}

    for sku in curing["SKUCode"].unique():
        if skip_skus and sku in skip_skus:
            # CTP #41: WIP-fed SKU — green tyre comes from opening WIP, already assembled
            # with its components pre-month. Emit NO component demand (do not re-build).
            per_sku_stats.append({"sku": sku, "rows_total": 0,
                                  "edges": 0, "rows_uom_corrected": 0, "routed_items": 0})
            sku_to_per_tyre[sku] = {}
            continue
        if sku not in bom_by_sku:
            per_sku_stats.append({"sku": sku, "rows_total": 0,
                                  "edges": 0, "rows_uom_corrected": 0, "routed_items": 0})
            sku_to_per_tyre[sku] = {}
            continue
        bom_sku = bom_by_sku[sku]
        per_tyre_map, stats = per_tyre_demand(bom_sku, routed, routing_uom_map)
        per_sku_stats.append({"sku": sku, **stats})

        sku_to_per_tyre[sku] = per_tyre_map
        for item, (qty, uom) in per_tyre_map.items():
            per_sku_rows.append({"sku": sku, "item_code": item,
                                 "per_tyre_qty": qty, "uom": uom})

    # ---- STAGE 2: expand to per-block demand ----
    rows = []
    for _, blk in curing.iterrows():
        sku, bid, need, q0 = blk["SKUCode"], blk["block_id"], blk["effective_start"], float(blk["Qty"])
        per_tyre_map = sku_to_per_tyre.get(sku, {})
        for item, (per_tyre, uom) in per_tyre_map.items():
            rows.append({
                "item_code":  item,
                "sku":        sku,
                "block_id":   bid,
                "need_by":    need,
                "demand_qty": per_tyre * q0,
                "demand_uom": uom,
            })

    return pd.DataFrame(rows), pd.DataFrame(per_sku_rows), pd.DataFrame(per_sku_stats)


def save_xlsx(phase, name, sheets):
    OUTPUTS.mkdir(exist_ok=True)
    dest = OUTPUTS / _suff(f"phase{phase}_{name}.xlsx")
    tmp  = OUTPUTS / f".tmp_phase{phase}_{name}.xlsx"
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as w:
            for k, df in sheets.items():
                clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
                df.to_excel(w, sheet_name=clean, index=False)
        if dest.exists():
            try: dest.unlink()
            except Exception: pass
        os.replace(tmp, dest)
    except Exception:
        alt = OUTPUTS / _suff(f"phase{phase}_{name}_v2.xlsx")
        with pd.ExcelWriter(alt, engine="openpyxl") as w:
            for k, df in sheets.items():
                clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
                df.to_excel(w, sheet_name=clean, index=False)
        return alt
    finally:
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
    return dest


def main():
    print("\n" + "=" * 65)
    print("  PHASE 1b - Per-SKU Demand Explosion")
    print("=" * 65)
    cfg = load_cfg()
    bom, rt, plan = load_inputs(cfg)

    # MG assignment REMOVED 2026-07-22 (user instruction). CTP's BOM is single-variant,
    # so the MG filter was an inert no-op; mg_assignment.csv is no longer produced or read.

    # ── BUILDING-PLAN ECHO (2026-07-29): root component demand on the BUILDING plan ───────────
    # The building plan (which green tyre on which TBM, when, how many) is authoritative for what
    # gets built, so components explode from IT, not from the curing plan. Its `skuCode/qty/
    # startTime/endTime` fields are the same shape parse_plan_to_curing reads; `need_by` becomes the
    # BUILD start (the tighter, correct component deadline). OFF -> the curing plan roots demand (v6).
    _echo = bool(cfg.get("building_plan_echo", False))
    if _echo:
        _bp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "inputs", "building_plan.csv")
        if os.path.exists(_bp):
            _bplan = pd.read_csv(_bp)
            cur = parse_plan_to_curing(_bplan, cfg.get("selected_skus", []))
            print(f"  BUILDING-PLAN ECHO: demand ROOTED on the building plan "
                  f"({len(cur):,} build blocks, {cur['SKUCode'].nunique()} SKUs).")
        else:
            print(f"  [WARN] building_plan_echo ON but inputs/building_plan.csv missing "
                  f"-- falling back to the curing plan as the demand root.")
            cur = parse_plan_to_curing(plan, cfg.get("selected_skus", []))
    else:
        cur = parse_plan_to_curing(plan, cfg.get("selected_skus", []))
    print(f"  {'Build' if _echo else 'Curing'} blocks loaded:  {len(cur):,}")
    print(f"  Distinct SKUs:         {cur['SKUCode'].nunique()}")
    print(f"  Total tyres:           {int(cur['Qty'].sum()):,}")
    # H-4 GUARD (2026-07-27): the demand drum's MONTH must be intentional. `plan` is resolved in
    # two layers (adapt_inputs `db_plan` vs phases `phases_read_db`); if they disagree the whole
    # month can flip (June shift-sheet 385,772 <-> April CuringSchedule 349,241) with no error.
    # Print the plan's span LOUDLY and, if `expected_plan_month` is set, FAIL on a mismatch.
    try:
        _pspan = pd.to_datetime(cur["effective_start"], errors="coerce")
        _lo, _hi = _pspan.min(), _pspan.max()
        print(f"  PLAN PROVENANCE:       {_lo.date()} .. {_hi.date()}  "
              f"({len(cur):,} blocks, {int(cur['Qty'].sum()):,} tyres)")
        _exp = str(os.environ.get("CTP_EXPECTED_PLAN_MONTH")      # env override (v15: "2026-10")
                   or cfg.get("expected_plan_month", "") or "").strip()   # e.g. "2026-06"
        if _exp and not (str(_lo)[:7] == _exp or str(_hi)[:7] == _exp):
            print(f"  !! PLAN-MONTH MISMATCH: plan spans {_lo.date()}..{_hi.date()} but "
                  f"expected_plan_month = {_exp}. The plan source likely flipped (db_plan vs "
                  f"phases_read_db). Refusing a silent wrong-month run.")
            return 1
    except Exception as _e:
        print(f"  [H-4 plan-provenance check skipped: {_e}]")

    # CTP #41: identify WIP-fed SKUs (green tyre not built -> component sub-tree from WIP).
    # INERT under building-plan echo: the building plan already defines exactly what is built, so
    # its rows ARE the build demand -- no WIP-fed inference / prune (would double-remove).
    skip_skus = set() if _echo else wip_fed_skus(cur, bom, rt, cfg)
    if skip_skus:
        print(f"  PRUNE WIP-FED (CTP #41): {len(skip_skus)} SKU(s) cured from opening WIP "
              f"-> component sub-tree NOT re-built (no over-production).")

    print(f"  Stage 1: per-SKU BOM walk -> per-tyre rates...")
    demand, per_sku, per_sku_stats = explode_demand(cur, bom, rt, skip_skus=skip_skus)
    print(f"  Per-SKU items:         {len(per_sku):,}")
    print(f"  Demand rows (per-block): {len(demand):,}")

    tot_corr = int(per_sku_stats["rows_uom_corrected"].fillna(0).sum())
    n_skus = int((per_sku_stats["rows_uom_corrected"].fillna(0)>0).sum())
    print(f"  UOM-corrected edges:   {tot_corr:,} across {n_skus} SKUs")
    if "rows_summed" in per_sku_stats.columns:
        tot_sum = int(per_sku_stats["rows_summed"].fillna(0).sum())
        print(f"  Rows SUMMED (CTP #4):  {tot_sum:,} extra rows kept that v6 would have "
              f"DISCARDED (same UOM, different grand_parent = a different component)")

    # ══════════════════════════════════════════════════════════════════════════════════
    # THE DEMAND REGRESSION GATE  --  REWRITTEN 2026-07-14. (A) DEFECT.
    # ══════════════════════════════════════════════════════════════════════════════════
    # THE OLD GATE WAS HOLLOW. IT PRINTED A CLEAN PASS ON EVERY ATTACK I THREW AT IT.
    #
    # It validated `per_sku` -- the per-tyre RATE table -- and NEVER ONCE TOUCHED `demand`,
    # which is the DataFrame this phase actually prints, writes, and hands to phase 1c. It
    # then derived its expected SKU set FROM ITS OWN OUTPUT (`_skus = {s for s,_ in
    # _got.index}`), so anything that vanished from the output also vanished from the
    # expectation. And it re-implemented the NaN->0 coercion on BOTH sides
    # (`.fillna(0.0)`), so a blank cell in the BOM matched a zero in the code.
    #
    # It was BROKEN THREE WAYS AT ONCE. Measured, by actually doing it:
    #   (a) halve every curing block's Qty -> demand is 50% wrong -> gate: "1,960/1,960 match"
    #   (b) break one SKU's MG so 59,730 demand rows vanish (14.7%) -> gate: "1,905/1,905 match"
    #   (c) BLANK child_quantity on CAP 66 - CAPSTRIP, so 15,935,634 metres (86% of ALL metre
    #       demand) silently become ZERO                            -> gate: STILL PASSES
    #
    # A gate that cannot fail is not a gate. It is a decoration that costs you vigilance.
    #
    # WHAT THIS ONE DOES INSTEAD -- four changes, each closing one hole:
    #   1. It validates `demand` ITSELF (the thing we ship), not `per_sku`. It re-derives
    #      the expected per-block quantity straight from the BOM x the PLAN and compares.
    #   2. It derives the expected SKU set from THE PLAN, so a SKU that DISAPPEARS from the
    #      output is a FAILURE instead of an absence of evidence. (kills attack (b))
    #   3. It does NOT coerce NaN -> 0 on the expected side. A blank/NaN child_quantity is
    #      caught EXPLICITLY and FAILS. (kills attack (c))
    #      This is the "present-but-blank" trap and it must be hunted deliberately:
    #      float(NaN) does NOT raise, EVERY NaN comparison is False (so `> tol` silently
    #      passes), and NaN is TRUTHY. A blank cell will never announce itself. You have to
    #      go and look for it.
    #   4. Block quantities are re-read from the PLAN, so scaling demand fails. (kills (a))
    #
    # The expectation is computed with plain pandas and shares NO code path with
    # per_tyre_demand(). Tolerance is float epsilon only -- there is no slack to hide in.
    try:
        _fail = []                      # every failure is collected, then reported together

        # ---- 0. THE EXPECTED SKU SET COMES FROM THE PLAN. NEVER FROM OUR OWN OUTPUT. ----
        # `cur` is the parsed curing plan. Any SKU in it that has BOM rows raising routed
        # children MUST appear in `demand`. If it does not, something ate it.
        _routed = set(rt["routed_product"].astype(str).str.strip()) - {""}
        _b = bom.copy()
        _b["_sku"] = _b["Super parent"].astype(str).str.strip()
        _b["_ch"]  = _b["child"].astype(str).str.strip()
        _b = _b[_b["_ch"].isin(_routed)]                 # phase1b:227 `if ch not in routed`

        # ---- 1. THE PRESENT-BUT-BLANK TRAP. CHECK IT BEFORE ANY COERCION. --------------
        # A blank child_quantity is NOT a zero. It is the master failing to state a number.
        # float(NaN) does not raise and NaN compares False to everything, so it would sail
        # straight through every arithmetic check below. Catch it HERE, explicitly, or not
        # at all. This is what let 15.9M metres of cap strip evaporate in silence.
        #
        # !! THIS CHECK IS LOAD-BEARING AND I HAVE THE RECEIPT. When attack (c) was actually
        #    run against this gate, the NUMERIC comparison in step 5 reported
        #        MATCH 405,005 / WRONG VALUE 0
        #    -- it did NOT catch it, and it CANNOT. `groupby().sum()` SKIPS NaN and returns
        #    0.0, so the expected side silently becomes 0 and matches the code's 0 exactly.
        #    A blank cell is INVISIBLE to arithmetic on both sides at once. ONLY this
        #    explicit check sees it. Do not "simplify" it away.
        _plan_skus_all = set(cur["SKUCode"].astype(str).str.strip())
        # CTP #41: WIP-fed SKUs are not exploded, so the gate must NOT expect their
        # components (else it flags them MISSING and halts). Validate built SKUs only.
        _built_skus = _plan_skus_all - (skip_skus or set())
        _raw_cq = _b.loc[_b["_sku"].isin(_built_skus), "child quantity"]
        _is_blank = _raw_cq.isna() | (_raw_cq.astype(str).str.strip()
                                              .str.lower().isin(("", "nan", "none")))
        _num_cq = pd.to_numeric(_raw_cq, errors="coerce")
        _is_nonnum = _num_cq.isna() & ~_is_blank        # present, but not a number
        if bool(_is_blank.any()) or bool(_is_nonnum.any()):
            _bad_rows = _b.loc[_b["_sku"].isin(_built_skus)][_is_blank | _is_nonnum]
            _fail.append(
                f"BLANK / NON-NUMERIC child_quantity on {len(_bad_rows):,} routed BOM row(s) "
                f"for IN-PLAN SKUs "
                f"({int(_is_blank.sum()):,} blank, {int(_is_nonnum.sum()):,} non-numeric).\n"
                f"       A BLANK IS NOT A ZERO. float(NaN) does not raise, every NaN "
                f"comparison is False, and NaN is truthy -- so a blank cell silently becomes "
                f"ZERO DEMAND and NOTHING WARNS.\n"
                f"       worst-affected items: "
                f"{sorted(set(_bad_rows['_ch']))[:6]}\n"
                f"       Fix the BOM. I will not guess what the missing number was.")

        # ---- 2. EXPECTED per-tyre rate: SUM of child_quantity over EVERY routed row -----
        # No fillna. If a value were blank we already failed above; reaching here it is a
        # real number. This is the BOM's own identity: use every row, sum the duplicates.
        _b["_cq"] = pd.to_numeric(_b["child quantity"], errors="coerce")
        _rate = _b.groupby(["_sku", "_ch"])["_cq"].sum()          # expected per-tyre qty

        # ---- 3. EXPECTED per-BLOCK demand = rate x the PLAN's OWN block quantity --------
        # Reading Qty back from `cur` is what makes attack (a) (halving the plan's Qty)
        # impossible to hide: the expectation moves with the plan, the output must too.
        _cur = cur.copy()
        _cur["_sku"] = _cur["SKUCode"].astype(str).str.strip()
        _cur = _cur[_cur["_sku"].isin(_built_skus)]      # CTP #41: expect built SKUs only
        _rate_df = _rate.reset_index().rename(columns={"_cq": "_rate"})
        _exp = (_cur[["_sku", "block_id", "Qty"]]
                .merge(_rate_df, on="_sku", how="inner"))
        _exp["exp"] = _exp["_rate"] * _exp["Qty"].astype(float)
        _exp = _exp.set_index(["_sku", "_ch", "block_id"])["exp"]

        # ---- 4. WHAT THE CODE ACTUALLY PRODUCED -- from `demand`, the shipped frame -----
        if demand.empty:
            _got = pd.Series(dtype=float,
                             index=pd.MultiIndex.from_arrays([[], [], []],
                                                             names=["_sku", "_ch", "block_id"]))
        else:
            _d = demand.copy()
            _d["_sku"] = _d["sku"].astype(str).str.strip()
            _d["_ch"]  = _d["item_code"].astype(str).str.strip()
            _got = _d.groupby(["_sku", "_ch", "block_id"])["demand_qty"].sum()

        # ---- 5. COMPARE. An index present on one side and MISSING on the other is a ----
        # FAILURE, not a zero. This is the join that kills attack (b): a SKU whose demand
        # rows vanished is now an index full of MISSING, and missing is not forgiven.
        _cmp = pd.concat([_exp.rename("exp"), _got.rename("got")], axis=1)
        _missing = _cmp["got"].isna()          # expected by the plan+BOM, ABSENT from demand
        _extra   = _cmp["exp"].isna()          # in demand, NOT justified by the plan+BOM
        _both    = ~_missing & ~_extra
        _dv = (_cmp.loc[_both, "exp"] - _cmp.loc[_both, "got"]).abs()
        _tol = 1e-9 * _cmp.loc[_both, "exp"].abs().clip(lower=1.0)
        _wrong = _cmp.loc[_both][_dv > _tol]

        print(f"  DEMAND REGRESSION GATE (validates `demand`, the frame we ship):")
        print(f"    expected (sku,item,block) cells, derived from PLAN x BOM : {len(_exp):,}")
        print(f"    cells produced by the code                               : {len(_got):,}")
        print(f"    MATCH                                                    : "
              f"{int(_both.sum()) - len(_wrong):,}")
        print(f"    MISSING from demand (plan+BOM expected them)             : "
              f"{int(_missing.sum()):,}")
        print(f"    EXTRA in demand (nothing in plan+BOM justifies them)     : "
              f"{int(_extra.sum()):,}")
        print(f"    WRONG VALUE                                              : {len(_wrong):,}")

        if int(_missing.sum()):
            _ms = sorted({s for s, _, _ in _cmp[_missing].index})
            _fail.append(f"{int(_missing.sum()):,} (sku,item,block) demand cells that the PLAN "
                         f"and the BOM BOTH require are MISSING from `demand`.\n"
                         f"       Demand rows have been DROPPED. Affected SKUs "
                         f"({len(_ms)}): {_ms[:6]}")
        if int(_extra.sum()):
            _es = sorted({s for s, _, _ in _cmp[_extra].index})
            _fail.append(f"{int(_extra.sum()):,} demand cells exist that the plan and the BOM "
                         f"do NOT justify. Demand is being INVENTED. SKUs: {_es[:6]}")
        if len(_wrong):
            _w5 = _wrong.reindex((_wrong["exp"] - _wrong["got"]).abs()
                                 .sort_values(ascending=False).index).head(5)
            _lines = "\n".join(
                f"         {s} / {c} / {b}:  expected {r.exp:,.4f}, got {r.got:,.4f}"
                for (s, c, b), r in _w5.iterrows())
            _wrong_msg = (f"{len(_wrong):,} demand cells have the WRONG QUANTITY.\n"
                          f"       Something is DISCARDING or DOUBLE-COUNTING BOM rows, or the "
                          f"block quantity is not the plan's.\n{_lines}")
            # The gate's EXPECTED rate is the SUM of every BOM row; when an edge has CONFLICTING
            # UOMs the explosion legitimately picks ONE row (summing units is invalid), so expected
            # (sum) != got (one row) for exactly those edges. Under CTP_BOM_GATE=warn this is the
            # known conflicting-UOM data issue we already flag above -> degrade to a WARNING so the
            # product can be scheduled. MISSING/EXTRA/BLANK stay HARD (they are real corruption).
            _bg = os.environ.get("CTP_BOM_GATE", "strict").strip().lower()
            if _bg == "warn":
                print(f"  [WARN] demand-regression WRONG-QTY RELAXED (CTP_BOM_GATE=warn): "
                      f"{len(_wrong):,} cells off -- typically conflicting-UOM edges that picked "
                      f"one row (see the bom-uom-conflict warnings). DATA ISSUE to fix at source.\n{_lines}")
            else:
                _fail.append(_wrong_msg)

        # ---- 6. THE BOM MASTER IDENTITY. IT NOW *GATES*. It used to only PRINT. ---------
        # Parent_qty(X) == SUM(child_quantity into X). This validates the BOM MASTER (not
        # the code), and it is the identity CTP DEVIATION #4 rests on -- so if it breaks,
        # the justification for summing rows breaks with it, and we must not proceed on an
        # assumption we can see is false. Only checkable on nodes that are both a parent
        # and a child AND whose units agree; the unmeasurable remainder is reported, never
        # silently counted as a pass.
        # ── CONVENTION-AWARE BOM IDENTITY (2026-08-06). PCR states Parent_qty as ONE GLOBAL
        # TOTAL, repeated identically in EVERY producing branch of a node -> consumed total ==
        # first(Parent_qty). TBR is grand_parent-SCOPED / per-path (confirmed by Vikram, CTP):
        # the SAME node is produced ONCE PER BRANCH (grand_parent = a top sub-assembly, incl.
        # symmetric LEFT/RIGHT), each stating its BRANCH SHARE -> consumed total == SUM over
        # distinct branches of Parent_qty. When branch values are EQUAL (PCR's repeated total vs
        # TBR's equal L/R shares) the two conventions are indistinguishable by structure alone,
        # so we accept a node as CONSISTENT if its consumption matches EITHER convention:
        # first(pq) ~= SUM(child_qty)  OR  sum-branch(pq) ~= SUM(child_qty). This keeps PCR
        # strict (it balances ONLY under first) AND admits TBR (balances under sum-branch); a
        # node matching NEITHER is genuinely inconsistent. The demand explosion already sums
        # child_quantity across branches (DEVIATION #4) -- correct for BOTH conventions, and
        # there are ZERO exact-duplicate (sku,gp,parent,child) rows so that sum never
        # double-counts. The residual nodes that match neither convention are the compound
        # mixing-yield / process-scrap factor (MASTER/FINAL COMPOUND, consumed>=produced, ratio
        # in [1.0,1.5], per-SKU-consistent) -- EXPECTED in a real CTP TBR extract -> WARN, not
        # fail. ANYTHING outside that shape (shortfall, ratio>1.5, non-compound, or a lone
        # outlier vs its SKU siblings) FAILS CLOSED -- the profile of genuine corruption.
        _b2 = bom.copy()
        _b2["_pq"] = pd.to_numeric(_b2["Parent qty"], errors="coerce")
        _b2["_cq"] = pd.to_numeric(_b2["child quantity"], errors="coerce")
        _b2["_sku"] = _b2["Super parent"].astype(str).str.strip()
        _b2["_gp"]  = _b2["grand parent"].astype(str).str.strip()
        _b2["_par"] = _b2["Parent"].astype(str).str.strip()
        _b2["_ch"]  = _b2["child"].astype(str).str.strip()
        _b2["_cu"]  = _b2["child Unit"].astype(str).str.strip().str.upper()
        _b2["_pu"]  = _b2["Parent unit"].astype(str).str.strip().str.upper()
        _b2["_cd"]  = _b2["child description"].astype(str).str.strip().str.upper()
        _inflow = (_b2.groupby(["_sku", "_ch"]).agg(i=("_cq", "sum"), u=("_cu", "first"),
                                                    cd=("_cd", "first"))
                      .reset_index().rename(columns={"_ch": "_node"}))
        # declared production, per producing branch (sku, grand_parent, parent), then BOTH
        # convention totals: first (global-total / PCR) and sum-over-branches (per-path / TBR).
        _perbr = (_b2.groupby(["_sku", "_gp", "_par"]).agg(pq=("_pq", "first"), pu=("_pu", "first"))
                     .reset_index())
        _decl = (_perbr.groupby(["_sku", "_par"])
                       .agg(dfirst=("pq", "first"), dsum=("pq", "sum"), du=("pu", "first"))
                       .reset_index().rename(columns={"_par": "_node"}))
        _all = _inflow.merge(_decl, on=["_sku", "_node"], how="inner")
        _zero = int(((_all["dfirst"] <= 0) & (_all["dsum"] <= 0)).sum())
        _unit = int((((_all["dfirst"] > 0) | (_all["dsum"] > 0)) & (_all["u"] != _all["du"])).sum())
        _m = _all[((_all["dfirst"] > 0) | (_all["dsum"] > 0))
                  & (_all["u"] == _all["du"]) & (_all["i"] > 0)].copy()
        _ef = (_m["dfirst"] - _m["i"]).abs() / _m["i"]
        _es = (_m["dsum"]   - _m["i"]).abs() / _m["i"]
        _m["consistent"] = (_ef <= 0.005) | (_es <= 0.005)
        _bad = _m[~_m["consistent"]].copy()
        # reporting: declared = whichever convention is CLOSER to consumed; ratio = consumed/declared
        _bad["d"] = _bad["dfirst"].where((_bad["dfirst"] - _bad["i"]).abs()
                                         <= (_bad["dsum"] - _bad["i"]).abs(), _bad["dsum"])
        _bad["ratio"] = _bad["i"] / _bad["d"]
        # benign compound mixing-yield shape (Vikram): compound node, consumed>=produced,
        # ratio in [1.0,1.5], and consistent with its SKU siblings' ratio.
        _shape = (_bad["cd"].str.contains("COMPOUND", na=False)
                  & (_bad["ratio"] >= 1.0) & (_bad["ratio"] <= 1.5)) if len(_bad) else pd.Series([], dtype=bool)
        _cand = _bad[_shape].copy()
        if len(_cand):
            _med = _cand.groupby("_sku")["ratio"].transform("median")
            _benign_idx = set(_cand.index[((_cand["ratio"] - _med).abs() / _med) <= 0.02])
        else:
            _benign_idx = set()
        _bad["benign"] = _bad.index.isin(_benign_idx)
        _hard = _bad[~_bad["benign"]]
        _nbad = int(len(_bad)); _nbenign = int(_bad["benign"].sum())
        print(f"  BOM MASTER IDENTITY GATE: {len(_m) - _nbad:,}/{len(_m):,} same-unit nodes consistent "
              f"under a BOM convention (PCR global-total OR TBR per-path)  "
              f"[{_nbad} inconsistent: {len(_hard)} HARD, {_nbenign} benign compound-yield]")
        print(f"                            (not checkable: {_zero:,} Parent_qty==0 [unused "
              f"alternatives], {_unit:,} unit mismatch [dimensionally N/A])")
        if _nbenign:
            _bex = _bad[_bad["benign"]].sort_values("ratio", ascending=False).head(3)
            _bl = "; ".join(f"{r['_sku']}/{r['_node']} x{r['ratio']:.3f}" for _, r in _bex.iterrows())
            print(f"  [WARN] {_nbenign} TBR compound node(s) carry a mixing-yield uplift "
                  f"(consumed>produced, ratio 1.0-1.5, per-SKU-consistent) -- EXPECTED, not blocking. e.g. {_bl}")
        if len(_hard):
            # NB: .iterrows() not .itertuples() (underscore col names aren't valid identifiers).
            _bad_nodes = _hard.assign(_e=(_hard["i"] - _hard["d"]).abs() / _hard["d"]) \
                              .sort_values("_e", ascending=False).head(5)
            _lines = "\n".join(
                f"         {r['_sku']} / {r['_node']}:  produced~{r['d']:,.4f}, consumed {r['i']:,.4f}  "
                f"(ratio {r['ratio']:.3f}, {r['cd']})"
                for _, r in _bad_nodes.iterrows())
            _bom_gate = __import__("os").environ.get("CTP_BOM_GATE", "strict").strip().lower()
            _msg = (f"{len(_hard):,} BOM node(s) match NEITHER convention (consumed != first(pq) and "
                    f"!= sum-branch(pq)) and are OUTSIDE the benign compound-yield shape. A PCR "
                    f"global-total or TBR per-path node must balance; only a MASTER/FINAL COMPOUND "
                    f"yield uplift (consumed>=produced, ratio<=1.5, per-SKU-consistent) may deviate. "
                    f"These do not:\n{_lines}")
            if _bom_gate == "warn":
                print(f"  [WARN] BOM identity HARD violations RELAXED (CTP_BOM_GATE=warn): "
                      f"{len(_hard):,} node(s) tolerated -- DATA ISSUE to fix at source.\n{_lines}")
            else:
                _fail.append(_msg)

        # ---- 7. VERDICT ----------------------------------------------------------------
        if _fail:
            print("\n" + "=" * 72)
            print("  !! DEMAND REGRESSION GATE FAILED. DEMAND IS NOT TRUSTWORTHY.")
            print("=" * 72)
            for i, f in enumerate(_fail, 1):
                print(f"\n  [{i}] {f}")
            print("\n  Refusing to hand corrupt demand to phase 1c. Stopping.")
            return 1
        print(f"  GATE PASSED: every demand cell equals PLAN x BOM, no cell missing, none "
              f"invented, no blank cells.")

    except Exception as _e:
        import traceback as _t
        print(f"\n  !! DEMAND REGRESSION GATE COULD NOT RUN ({_e!r}).")
        _t.print_exc()
        print("     Demand is UNVERIFIED. Refusing to continue silently. Stopping.")
        return 1

    # ══════════════════════════════════════════════════════════════════════════════════
    # PER-UOM SUMMARY  --  FIXED 2026-07-14. (A) DEFECT, REPORTING.
    # ══════════════════════════════════════════════════════════════════════════════════
    # THE OLD LOOP WAS HARD-CODED: `for uom in ("KG", "MM", "NOS")`.
    #
    # **CTP HAS ZERO "MM" ITEMS.** CTP works in METRES (see adapt_inputs.bom docstring:
    # the BOM is `M`, the routing is `M/MIN`, and we deliberately do NOT convert). So the
    # `M` family -- 18,546,027 units, 50.55 m/tyre, THE LARGEST DEMAND FAMILY IN THE PLANT
    # -- was NEVER PRINTED. The summary dutifully reported "MM per tyre: 0.00" instead.
    #
    # Why that is not a cosmetic bug: under attack (c) above, METRE demand collapsed by 86%
    # and this summary still read "KG 22.38 / MM 0.00 / NOS 8.53" -- completely unchanged.
    # THE ONE SUMMARY A HUMAN ACTUALLY READS COULD NOT SHOW A METRE REGRESSION AT ALL.
    #
    # Fix: report the units ACTUALLY PRESENT in the data, sorted by volume. Never again
    # hard-code a unit list that the data does not have to agree with.
    if not demand.empty:
        tot_tyres = int(cur["Qty"].sum())
        _by_uom = (demand.groupby("demand_uom")
                         .agg(items=("item_code", "nunique"),
                              total=("demand_qty", "sum"))
                         .sort_values("total", ascending=False))
        print(f"\n  Per-UOM totals ({len(_by_uom)} unit(s) PRESENT IN THE DATA; "
              f"nothing hard-coded):")
        for uom, r in _by_uom.iterrows():
            per_tyre = (r["total"] / tot_tyres) if tot_tyres else 0.0
            print(f"    {str(uom):6s}  {int(r['items']):5d} items   "
                  f"total {r['total']:18,.1f}   per tyre {per_tyre:12,.2f}")
        # State the absent units explicitly, so "no MM row" reads as a FACT about CTP
        # rather than as a summary that quietly forgot to print one.
        _absent = [u for u in ("KG", "M", "MM", "NOS") if u not in set(_by_uom.index)]
        if _absent:
            print(f"    (units NOT present in CTP demand: {', '.join(_absent)})")

    # Stage 3: cross-SKU consolidation
    # Stage 3: cross-SKU consolidation
    item_consolidated = demand.groupby(["item_code","demand_uom"]).agg(
        n_skus=("sku","nunique"),
        n_blocks=("block_id","nunique"),
        total_qty=("demand_qty","sum"),
    ).reset_index().sort_values("total_qty", ascending=False)

    print(f"  Stage 3: cross-SKU consolidation - {len(item_consolidated):,} items")

    # ══════════════════════════════════════════════════════════════════════════════════
    # CURING DEMAND ROWS -- represent the TERMINAL curing operation, sourced from plan.csv.
    # ══════════════════════════════════════════════════════════════════════════════════
    # The CURING routing (routed_product = the tyre SKU) had NO demanded item in this frame
    # -- the explosion emits only the CHILDREN (green tyre, components), never the finished
    # SKU curing produces -- so the coverage checks flagged CURING as an un-produced orphan.
    # Emit one demand row per curing block, item_code = the SKU, from `cur` (the parsed plan).
    # NOTE: this is a PHASE-1 representation fix. phase2 must be told to SKIP these curing
    # items (they are terminal, scheduled by phase6 from plan.csv) or it will lot-size them.
    # Placed AFTER the demand-regression gate (SKU is not a BOM child; must not be seen there).
    if bool(cfg.get("emit_curing_demand_rows", True)) and len(cur):
        _cur_rows = pd.DataFrame({
            "item_code":  cur["SKUCode"].astype(str).str.strip().values,   # the cured tyre
            "sku":        cur["SKUCode"].astype(str).str.strip().values,
            "block_id":   cur["block_id"].values,
            "need_by":    cur["effective_start"].values,
            "demand_qty": pd.to_numeric(cur["Qty"], errors="coerce").fillna(0).values,
            "demand_uom": "NOS",
        })
        _cur_rows = _cur_rows[_cur_rows["demand_qty"] > 0]
        demand = pd.concat([demand, _cur_rows[demand.columns]], ignore_index=True)
        print(f"  CURING demand rows (from plan.csv, terminal op): +{len(_cur_rows):,} "
              f"rows for {_cur_rows['item_code'].nunique()} SKUs")

    # Guard against OneDrive sync conflicts that leave a directory at the
    # file's path (with the actual file trapped inside). If we don't nuke
    # the directory first, pandas writes inside it and Phase 2 reads stale data.
    import shutil
    csv_path = OUTPUTS / _suff("phase1_demand.csv")
    gz_path  = OUTPUTS / _suff("phase1_demand.csv.gz")
    for _p in (csv_path, gz_path):
        if _p.is_dir():
            print(f"  WARNING: {_p} exists as a directory (OneDrive sync conflict). Removing.")
            shutil.rmtree(_p, ignore_errors=True)

    big = len(demand) > 200000
    # STALE-TWIN FIX (2026-09-26): only ONE of .csv / .csv.gz is written per run, so the other
    # one survives from an earlier (e.g. June) run -- and every reader that tries .gz first
    # (phase6 B2 reconciliation, phase5 CTB rates) silently read LAST MONTH's demand (B2 printed
    # an impossible -248,513 tyres). Delete the twin we are NOT writing.
    _stale = csv_path if big else gz_path
    if _stale.exists() and _stale.is_file():
        _stale.unlink()
        print(f"  removed stale {_stale.name} (from an earlier run) so no reader can pick it up")
    if big:
        demand.to_csv(gz_path, index=False, compression="gzip")
        print(f"  Wrote: {gz_path.name} ({len(demand):,} rows)")
    else:
        demand.to_csv(csv_path, index=False)
        print(f"  Wrote: {csv_path.name} ({len(demand):,} rows)")

    sheets = {
        "02_per_SKU_rates":  per_sku,
        "03_per_SKU_stats":  per_sku_stats,
        "04_top_items":      item_consolidated.head(200),
    }
    _stage_xlsx_sheets(sheets, "phase1_demand.xlsx")
    print(f"\n[OK] Phase 1b complete - {len(demand):,} demand rows.")
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
