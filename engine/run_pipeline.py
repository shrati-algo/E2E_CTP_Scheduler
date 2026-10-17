#!/usr/bin/env python3
"""
run_pipeline.py — run v6's phases, unmodified, on CTP data.

`phases/*.py` are byte-identical copies of `v6_wave_p2/phases/*.py`. They are standalone
scripts: each loads its own inputs and writes its own outputs. We invoke them as
subprocesses, exactly as v6's own orchestrator does, so nothing about their behaviour
changes.

Phase order is v6's. phase1a (MG) is NOT run. The MG filter was stripped entirely on
2026-07-22 (CTP's BOM is single-variant, so it carried zero information); mg_assignment.csv
is no longer produced or read. See MEMORY.md.

    phase0 -> phase1b -> phase1_5 -> phase1c -> phase2 -> phase3 -> phase4 -> phase5 -> phase6

Run `python adapt_inputs.py` first.

Usage:
    python run_pipeline.py
    python run_pipeline.py --from phase2
    python run_pipeline.py --only phase5
    python run_pipeline.py --only phase5 phase6        # E5: BOTH now run (was: phase6 only)
    python run_pipeline.py --only phase5,phase6        # comma form also works
    python run_pipeline.py --only phase6 --force       # run despite a STALE upstream
"""
from __future__ import annotations
import os
import sys
import time
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

# S2 -- drum-pinned convergence. The press start of the PRIOR pass, snapshot here
# between passes. phase4 re-anchors building to it; phase6 pins the press to it.
P1_SNAPSHOT = os.path.join(HERE, "outputs2", "phase6_press_start_P1.csv")
P6_OUTPUT   = os.path.join(HERE, "outputs2", "phase6_curing_revision_updated.csv")


def _read_cfg() -> dict:
    import yaml
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _read_press_starts(path: str) -> dict:
    """phase6 csv -> {block_id(int): pandas.Timestamp}. The revised press start."""
    import pandas as pd
    df = pd.read_csv(path, usecols=["block_id", "startTime"])
    df["startTime"] = pd.to_datetime(df["startTime"], errors="coerce")
    out = {}
    for b, t in zip(df["block_id"], df["startTime"]):
        try:
            bi = int(float(str(b).lstrip("Bb")))
        except (ValueError, TypeError):
            continue
        if bi is not None:
            out[bi] = t
    return out


def _write_snapshot(press_starts: dict) -> None:
    """Persist the P1 snapshot phase4/phase6/phase5-S3 read.

    Columns: block_id, startTime, skuCode, qty. startTime comes from `press_starts`
    (P1 or a ratcheted P); skuCode/qty are sourced from the current phase6 output
    (block_id is stable across passes -- same filtered plan -- so this is consistent).
    S3's per-SKU WIP cap needs skuCode + qty to build the press-consumption stream.
    """
    import pandas as pd
    src = pd.read_csv(P6_OUTPUT, usecols=["block_id", "skuCode", "qty"])
    src["block_id"] = pd.to_numeric(src["block_id"], errors="coerce").astype("Int64")
    src = src.dropna(subset=["block_id"])
    src["startTime"] = src["block_id"].map(lambda b: press_starts.get(int(b)))
    src = src.dropna(subset=["startTime"])
    src = src[["block_id", "startTime", "skuCode", "qty"]].sort_values("block_id")
    tmp = P1_SNAPSHOT + ".tmp"
    src.to_csv(tmp, index=False)
    os.replace(tmp, P1_SNAPSHOT)


def run_convergence(max_iters: int) -> int:
    """S2 two-pass (or ratcheting) drum-pinned convergence.

    Pass 1 has ALREADY run (the normal pipeline). Its phase6 output is P1. We
    snapshot P1, then re-run phase4->5->6 with phase4 re-anchored to P1 and phase6
    pinning the press to P1. We assert P2 == P1 (no block moved off its P1 slot).
    If a block's kit still can't reach P1 it ratchets: P_{k+1} = max(P_k, P2),
    up to max_iters extra passes. Honest either way -- prints what happened.
    """
    import pandas as pd
    stages = [p for p in PHASES if p[0] in ("phase4", "phase5", "phase6")]

    def _green_aging():
        """(median green_age_h, qty-% over 24h) from the CURRENT phase6 output.
        This is the FALSIFIABLE check: convergence exists to make green tyres fresher,
        so it must LOWER median dwell and over-24h vs the un-converged pass 1. The old
        'P2 == P1 HOLDS' check only proved the press did not slip -- it could not fail
        for the reason we actually care about (a check that cannot fail)."""
        try:
            df = pd.read_csv(P6_OUTPUT)
            a = pd.to_numeric(df.get("green_age_h"), errors="coerce")
            q = pd.to_numeric(df.get("qty"), errors="coerce").fillna(0)
            ok = a.notna() & (q > 0)
            if not ok.any():
                return (float("nan"), float("nan"))
            return (float(a[ok].median()),
                    100.0 * q[ok & (a > 24)].sum() / q[ok].sum())
        except Exception as e:
            print(f"    [S2] aging read failed: {e}")
            return (float("nan"), float("nan"))

    prev = _read_press_starts(P6_OUTPUT)     # P1 = pass-1 press starts
    _write_snapshot(prev)
    p1_med, p1_pct = _green_aging()          # aging BASELINE (un-converged pass 1)
    print("\n" + "#" * 74)
    print(f"  S2 CONVERGE -- P1 snapshot written ({len(prev):,} blocks). Re-anchoring "
          f"building to the revised press slot and pinning the press.")
    print(f"  S2 CONVERGE -- P1 aging baseline: median {p1_med:.1f} h, over-24h {p1_pct:.1f}%")
    print("#" * 74)

    TOL = pd.Timedelta(seconds=1)
    for extra in range(1, max_iters + 1):
        print("\n" + "#" * 74)
        print(f"  S2 CONVERGE -- PASS {extra + 1} (phase4 re-anchor -> phase5 -> phase6 pin)")
        print("#" * 74)
        for key, script, title in stages:
            if not run_phase(key, script, title):
                print(f"  [S2] pass {extra + 1} FAILED at {key}")
                return 1
        cur = _read_press_starts(P6_OUTPUT)          # P_{k+1}
        # FALSIFIABLE AGING CHECK -- the reason convergence exists. Compared to the
        # un-converged pass 1, NOT to the previous pass, so a ratchet that drifts back
        # up is caught. This can genuinely FAIL (and says so loudly).
        cur_med, cur_pct = _green_aging()
        _aging_ok = (cur_pct <= p1_pct + 1e-9) and (cur_med <= p1_med + 1e-9)
        print(f"    AGING CHECK (pass {extra + 1} vs P1): median "
              f"{p1_med:.1f} -> {cur_med:.1f} h, over-24h {p1_pct:.1f} -> {cur_pct:.1f}%  "
              f"[{'IMPROVED' if _aging_ok else '*** REGRESSED -- convergence not fresher ***'}]")
        # A block that "moved" is one the pin could NOT hold at P1 (kit not ready) --
        # phase6 floors at P1, so cur >= prev always; moved == strictly later.
        common = set(prev) & set(cur)
        moved = [b for b in common if pd.notna(cur[b]) and pd.notna(prev[b])
                 and cur[b] > prev[b] + TOL]
        max_move_h = (max((cur[b] - prev[b]).total_seconds() for b in moved) / 3600.0
                      if moved else 0.0)
        print("\n" + "-" * 74)
        print(f"  S2 CONVERGENCE CHECK (pass {extra + 1}):")
        print(f"    blocks compared      : {len(common):,}")
        print(f"    blocks moved off P{extra}: {len(moved):,}"
              f"   (max +{max_move_h:.1f} h)")
        if not moved:
            print(f"    ASSERTION P{extra + 1} == P{extra}: HOLDS -- converged in "
                  f"{extra} extra pass(es).")
            print("-" * 74)
            return 0
        print(f"    ASSERTION P{extra + 1} == P{extra}: does NOT hold -- {len(moved):,} "
              f"blocks could not make their slot (kit still not ready).")
        print(f"    RATCHET: P{extra + 1} = max(P{extra}, phase6(build_to P{extra})); "
              f"re-running.")
        print("-" * 74)
        prev = {b: (max(prev[b], cur[b]) if b in prev else cur[b]) for b in cur}
        _write_snapshot(prev)

    print(f"\n  [S2] NOT fully converged after {max_iters} extra passes -- "
          f"residual blocks are ratcheted to their achievable slot and the schedule "
          f"is consistent (phase6 output reflects the final P). Reported honestly.")
    return 0

PHASES = [
    ("phase0",   "phase0_validate_inputs.py",      "Validate inputs (report only)"),
    ("phase1b",  "phase1b_demand_explosion.py",    "Demand explosion"),
    ("phase1_5", "phase1_5_wave_builder.py",       "Wave builder (+ balance pass)"),
    ("phase1c",  "phase1c_mrp_netting.py",         "MRP / WIP netting"),
    ("phase2",   "phase2_lot_sizing.py",           "Lot sizing"),
    ("phase3",   "phase3_dag_construction.py",     "DAG construction"),
    ("phase4",   "phase4_cpm.py",                  "CPM time windows"),
    ("phase5",   "phase5_forward_placement_v2.py", "Forward placement"),
    ("phase6",   "phase6_curing_revision.py",      "Curing revision"),
]
PHASE_KEYS = [p[0] for p in PHASES]

# ==============================================================================
# E5 -- STALENESS CHECK.  (A) DEFECT.
# ==============================================================================
# Re-running a SUBSET of the pipeline against stale upstream outputs was SILENT.
# An outputs2/ was observed in exactly that state: phase5/phase6 disagreed with
# phase1-4 by 3.9 DAYS of makespan, because they had been produced from an older
# generation of phase1-4 and nobody could see it.
#
# The chain is LINEAR, so the rule is simple and needs no hashing and no cache:
#   a phase's outputs must be NEWER than the outputs of EVERY phase before it.
# If they are not, that phase was produced from data that has since been replaced.
# It is FILE-MTIME ONLY. That is deliberate -- a hashing/caching framework is a
# whole new machine and nobody asked for one.
#
# The files below are the ones each phase WRITES. A file that does not exist is
# ignored (a phase with no outputs at all has simply never been run here).
PHASE_OUTPUTS = {
    # phase0 is REPORT-ONLY (outputs/phase0_data_validation.xlsx). Nothing reads it,
    # so it can never make anything stale and it is left out of the chain.
    "phase0":   [],
    "phase1b":  ["outputs2/phase1_demand_updated.csv.gz",
                 "outputs2/phase1_demand_updated.csv"],
    "phase1_5": ["outputs/phase1_5_waves.csv",
                 "outputs/phase1_5_block_to_wave.csv"],
    "phase1c":  ["outputs2/phase1_demand_NET_updated.csv.gz"],
    "phase2":   ["outputs2/phase2_lots_updated.csv",
                 "outputs2/phase2_lot_blocks_updated.csv.gz",
                 "outputs2/phase2_lot_skus_updated.csv"],
    "phase3":   ["outputs2/phase3_dag_edges_updated.csv",
                 "outputs2/phase3_lot_degrees_updated.csv"],
    "phase4":   ["outputs2/phase4_lot_times_updated.csv"],
    "phase5":   ["outputs2/phase5_schedule_updated.csv",
                 "outputs2/phase5_floor_schedule_updated.csv"],
    "phase6":   ["outputs2/phase6_curing_revision_updated.csv"],
}
# Filesystem mtime granularity slack, in seconds. Phases take seconds to minutes,
# so this cannot mask a real staleness.
MTIME_TOL_S = 1.0


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds/60.0:.1f} min"
    return f"{seconds/3600.0:.1f} h"


def _existing_outputs(key: str) -> list:
    return [p for p in (os.path.join(HERE, f) for f in PHASE_OUTPUTS.get(key, []))
            if os.path.exists(p)]


def _stamps(key: str):
    """(oldest_mtime, newest_mtime) over the phase's EXISTING outputs, or None."""
    ps = _existing_outputs(key)
    if not ps:
        return None
    ms = [os.path.getmtime(p) for p in ps]
    return (min(ms), max(ms))


def _in_chain(key: str) -> bool:
    """False for report-only phases -- they cannot be stale and cannot stale others."""
    return bool(PHASE_OUTPUTS.get(key))


def chain_status() -> dict:
    """key -> ('ok' | 'stale' | 'absent', reason). Pure file-mtime comparison.

    STALE means: some phase BEFORE this one has outputs newer than this phase's
    oldest output -- i.e. this phase's result was computed from inputs that have
    since been regenerated.
    """
    out = {}
    for i, key in enumerate(PHASE_KEYS):
        if not _in_chain(key):
            out[key] = ("skip", "report-only")
            continue
        st = _stamps(key)
        if st is None:
            out[key] = ("absent", "no outputs on disk")
            continue
        worst_name, worst_t = None, None
        for up in PHASE_KEYS[:i]:
            if not _in_chain(up):
                continue
            ust = _stamps(up)
            if ust is None:
                continue
            if worst_t is None or ust[1] > worst_t:
                worst_name, worst_t = up, ust[1]
        if worst_t is not None and worst_t > st[0] + MTIME_TOL_S:
            out[key] = ("stale", f"{worst_name} is {_ago(worst_t - st[0])} NEWER "
                                 f"than {key}'s outputs")
        else:
            out[key] = ("ok", "")
    return out


def check_inputs_before_run(todo_keys: list, force: bool) -> bool:
    """Refuse (unless --force) to build on upstream that is MISSING or STALE.

    Only the phases we are NOT re-running matter: anything in `todo` is about to be
    regenerated, so its current state is irrelevant.
    """
    if len(todo_keys) == len(PHASE_KEYS):
        return True                      # full run regenerates everything
    last = max(PHASE_KEYS.index(k) for k in todo_keys)
    upstream_not_run = [k for k in PHASE_KEYS[:last]
                        if k not in todo_keys and _in_chain(k)]
    if not upstream_not_run:
        return True

    status = chain_status()
    problems = []
    for k in upstream_not_run:
        state, why = status[k]
        if state == "absent":
            problems.append((k, "MISSING", "this phase has never been run here"))
        elif state == "stale":
            problems.append((k, "STALE", why))
    if not problems:
        print(f"  staleness OK -- {len(upstream_not_run)} upstream phase(s) not being "
              f"re-run, all consistent with their own inputs")
        return True

    print("\n" + "!" * 74)
    print("  UPSTREAM IS NOT SAFE TO BUILD ON")
    print("!" * 74)
    for k, tag, why in problems:
        print(f"  [{tag:7s}] {k:<9} {why}")
    print("\n  You are re-running: " + ", ".join(todo_keys))
    print("  but the phases above -- which those phases READ -- are missing or were")
    print("  produced BEFORE the data they depend on. This is how outputs2/ ended up")
    print("  with phase5/phase6 disagreeing with phase1-4 by 3.9 DAYS of makespan.")
    if force:
        print("\n  --force given: RUNNING ANYWAY. The result is NOT a coherent plan.")
        print("!" * 74)
        return True
    print("\n  Re-run the upstream too, e.g.:  python run_pipeline.py --from "
          f"{problems[0][0]}")
    print("  Or, if you really mean it:      ... --force")
    print("!" * 74)
    return False


def report_stale_after_run(todo_keys: list) -> None:
    """Anything downstream we did NOT re-run is now stale. Say so LOUDLY."""
    status = chain_status()
    stale = [(k, why) for k, (st, why) in status.items()
             if st == "stale" and k not in todo_keys]
    if not stale:
        return
    print("\n" + "!" * 74)
    print("  THESE OUTPUTS ARE NOW STALE -- they were NOT re-run and their inputs changed")
    print("!" * 74)
    for k, why in stale:
        print(f"  [STALE] {k:<9} {why}")
    first = min(stale, key=lambda t: PHASE_KEYS.index(t[0]))[0]
    print(f"\n  outputs2/ does NOT currently hold one coherent plan.")
    print(f"  Fix:  python run_pipeline.py --from {first}")
    print("!" * 74)


def preflight() -> None:
    """The checks that stop a SILENTLY-wrong plan. Each maps to a MEMORY.md hazard."""
    import pandas as pd
    inp = os.path.join(HERE, "inputs")
    if not os.path.exists(os.path.join(inp, "routing.csv")):
        sys.exit("inputs/ is empty — run `python adapt_inputs.py` first.")

    it = pd.read_csv(os.path.join(inp, "itemtype_master.csv"), dtype=str)
    types = {str(t).strip().upper() for t in it["ItemType"].dropna()}
    if "GREEN TYRES" not in types:
        sys.exit("PREFLIGHT FAIL (hazard #2): nothing is typed 'Green Tyres'. v6 keys "
                 "BUILDING_ITYPES and AGE_OVERRIDE_H on that exact string — without it every "
                 "green tyre takes the wrong placement path and its aging silently becomes "
                 "(8h, 8760h) instead of (0, 72).")

    rt = pd.read_csv(os.path.join(inp, "routing.csv"), dtype=str)
    depts = {str(d).upper() for d in rt["department"].dropna()}
    if not any(tok in d for d in depts for tok in ("BUILD", "TBM", "CURING")):
        sys.exit("PREFLIGHT FAIL (hazard #1): no department contains BUILD/TBM/CURING. "
                 "v6's cascade_orphans would treat EVERY lot as an orphan and place nothing, "
                 "without error.")

    # MG preflight REMOVED 2026-07-22 — mg_assignment.csv no longer exists; phase1b/phase3
    # no longer read it (MG stripped as an inert single-variant constant).

    stale = os.path.join(HERE, "outputs2", "WIP_simulation_May2026.csv")
    if os.path.exists(stale):
        sys.exit(f"PREFLIGHT FAIL (hazard #5): {stale} exists. v6's phase5 would net WIP a "
                 "SECOND time on top of phase1c and double-count the stock. Delete it.")

    gt = int((it["ItemType"].astype(str).str.strip().str.upper() == "GREEN TYRES").sum())
    print(f"  preflight OK - {gt} green tyres typed | BUILDING + CURING departments present")


def run_phase(key: str, script: str, title: str) -> bool:
    print("\n" + "=" * 74)
    print(f"  {key.upper():<9} {title}")
    print("=" * 74)
    t0 = time.time()
    # v6's phases print unicode (arrows, symbols). The Windows console is cp1252 and
    # raises UnicodeEncodeError on them. Force UTF-8 for the child's stdio — this changes
    # only how output is ENCODED, never what the phase computes.
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run([sys.executable, os.path.join(HERE, "phases", script)],
                       cwd=HERE, env=env)
    dt = time.time() - t0
    if r.returncode != 0:
        print(f"\n  [{key}] FAILED after {dt:.0f}s (exit {r.returncode})")
        return False
    print(f"  [{key}] ok in {dt:.0f}s")
    return True


def _split_phases(values, flag: str) -> list:
    """E5: accept `--only phase5 phase6` AND `--only phase5,phase6`.

    THE BUG THIS FIXES: `--only` was SINGLE-VALUED (`ap.add_argument("--only")`), so
    `--only phase5 --only phase6` silently kept only the LAST one and ran phase6 ALONE,
    with no warning. It fooled a code auditor mid-audit: his "phase5 is deterministic
    x3" test never ran phase5 at all.
    """
    out = []
    for v in (values or []):
        out.extend(t for t in str(v).replace(",", " ").split() if t)
    bad = [k for k in out if k not in PHASE_KEYS]
    if bad:
        sys.exit(f"unknown phase(s) {bad} for {flag}; expected one of {PHASE_KEYS}")
    return out


def _write_mix_feedback(path: str, buffer_h: float) -> int:
    """v69 #3: for every compound lot, the time its consumers REALLY needed it in the last schedule. A consumer whose
    start was set by this compound (started within 30 min of it being ready) keeps the plan need (it is urgent); a
    consumer that started later for another reason needs the compound only by its real start - buffer_h."""
    import pandas as pd
    O = os.path.join(HERE, "outputs2")
    s = pd.read_csv(os.path.join(O, "phase5_schedule_updated.csv"), parse_dates=["scheduled_start", "scheduled_finish"]
                    ).drop_duplicates("lot_id").set_index("lot_id")
    L = pd.read_csv(os.path.join(O, "phase2_lots_updated.csv"), low_memory=False, parse_dates=["first_need"]
                    ).set_index("lot_id")
    E = pd.read_csv(os.path.join(O, "phase3_dag_edges_updated.csv"), usecols=["producer_lot", "consumer_lot", "consume_qty"])
    E = E[(pd.to_numeric(E["consume_qty"], errors="coerce").fillna(0) > 0)
          & E["producer_lot"].isin(s.index) & E["consumer_lot"].isin(s.index)].copy()
    t = L["item_type"].astype(str).str.upper()
    comp = set(L.index[t.isin(["MASTER COMPOUND", "FINAL COMPOUND"])])
    E = E[E["producer_lot"].isin(comp)]
    mn = (L["min_aging_h"].fillna(0) + L["transfer_time_h"].fillna(0))
    E["ready"] = E["producer_lot"].map(s["scheduled_finish"]) + pd.to_timedelta(E["producer_lot"].map(mn).fillna(0), unit="h")
    E["cst"] = E["consumer_lot"].map(s["scheduled_start"])
    E["plan"] = E["producer_lot"].map(L["first_need"])
    bound = (E["cst"] - E["ready"]) <= pd.Timedelta(minutes=30)
    E["need"] = E["plan"].where(bound, pd.concat([E["plan"], E["cst"] - pd.Timedelta(hours=buffer_h)], axis=1).max(axis=1))
    fb = E.dropna(subset=["need"]).groupby("producer_lot")["need"].min()
    pd.DataFrame({"lot_id": fb.index, "fb_need_ns": fb.astype("int64").values}).to_csv(path, index=False)
    return len(fb)


def _qty_feedback(tol: float = 0.02) -> str:
    """v76 (user 2026-10-11): full upstream / downstream integration of QUANTITY. Phase 5 makes more than phase 2 planned
    for some lots (4RC whole-spool / whole-dip top-ups, butyl-friendly prep runs); their extra input compound was never
    mixed. From the round-1 schedule, every lot's extra quantity is exploded through the scheduler's own per-unit inputs
    (phase-3 DAG edges; BOM for lots phase 2 never had) into the compound lots that feed it -- finals, then their masters,
    recursively -- so the next round's mixing plan makes it. Works from the round-1 originals (saved once)."""
    import math
    import shutil
    import pandas as pd
    O = os.path.join(HERE, "outputs2")
    lp, ep = os.path.join(O, "phase2_lots_updated.csv"), os.path.join(O, "phase3_dag_edges_updated.csv")
    for p in (lp, ep):
        if not os.path.exists(p + ".r1"):
            shutil.copy(p, p + ".r1")
    L = pd.read_csv(lp + ".r1", low_memory=False)
    E = pd.read_csv(ep + ".r1", low_memory=False)
    F = pd.read_csv(os.path.join(O, "phase5_floor_schedule_updated.csv"), low_memory=False)
    F["q"] = pd.to_numeric(F["produce_qty"], errors="coerce").fillna(0)
    bkg = pd.to_numeric(F.get("batch_size_kg"), errors="coerce")
    F.loc[F["UOM"].astype(str).str.upper() == "BATCH", "q"] = F["q"] * bkg      # batches -> kg (lot units)
    made = F.groupby("lot_id")["q"].sum()
    start = pd.to_datetime(F.groupby("lot_id")["start_time"].min(), errors="coerce")
    item_of = F.groupby("lot_id")["item"].first().astype(str).str.strip()
    L = L.set_index("lot_id")
    L["item_code"] = L["item_code"].astype(str).str.strip()
    typ = L["item_type"].astype(str).str.upper()
    comp = set(L.index[typ.isin(["MASTER COMPOUND", "FINAL COMPOUND"])])
    E["consume_qty"] = pd.to_numeric(E["consume_qty"], errors="coerce").fillna(0)
    E["producer_item"] = E["producer_item"].astype(str).str.strip()
    lq = pd.to_numeric(L["lot_qty"], errors="coerce")
    # per-unit inputs of a consumer lot from its edges: {consumer: {input item: [(producer lot, qty per unit)]}}
    per = {}
    for c, g in E[E["consume_qty"] > 0].groupby("consumer_lot"):
        if c in lq.index and lq[c] > 0:
            d = {}
            for p, it, q in zip(g["producer_lot"], g["producer_item"], g["consume_qty"]):
                d.setdefault(it, []).append((p, q / lq[c]))
            per[c] = d
    # BOM per kg for lots phase 2 never had (prep runs): child kg per parent kg
    bom = pd.read_csv(os.path.join(HERE, "inputs", "bom.csv"), dtype=str, low_memory=False,
                      usecols=["Parent", "Parent_qty", "child", "child_quantity"])
    bom = bom.apply(lambda s: s.str.strip())
    bom["r"] = pd.to_numeric(bom["child_quantity"], errors="coerce") / pd.to_numeric(bom["Parent_qty"], errors="coerce")
    bom_r = bom[bom["r"] > 0].groupby(["Parent", "child"])["r"].median()
    lots_of = {it: g.sort_values("first_need") for it, g in L[L.index.isin(comp)].reset_index().groupby("item_code")}
    add = {}                                                              # compound lot -> extra kg
    edge_add = {}                                                         # (producer, consumer) -> extra qty

    def push(lot, extra, depth=0):
        if extra <= 1e-6 or depth > 8:
            return
        ins = per.get(lot)
        if ins is None:                                                  # lot unknown to phase 3 -> BOM of its item
            it = item_of.get(lot, L["item_code"].get(lot, ""))
            ins = {}
            if it in bom_r.index.get_level_values(0):
                for ch, r in bom_r[it].items():
                    cand = lots_of.get(ch)
                    if cand is None or cand.empty:
                        continue
                    t = start.get(lot, pd.NaT)
                    fn = pd.to_datetime(cand["first_need"], errors="coerce")
                    pick = cand[fn >= t].head(1) if pd.notna(t) else cand.head(1)
                    pick = pick if len(pick) else cand.tail(1)
                    ins[ch] = [(pick["lot_id"].iat[0], r)]
        for it, plist in ins.items():
            plist = [(p, r) for p, r in plist if p in comp]
            if not plist:
                continue
            p, r = max(plist, key=lambda x: x[1])                       # the main feeding lot takes the extra
            q = extra * sum(r_ for _, r_ in plist)
            add[p] = add.get(p, 0.0) + q
            edge_add[(p, lot)] = edge_add.get((p, lot), 0.0) + q
            push(p, q, depth + 1)

    n_src = 0
    for lot, m in made.items():
        base = float(lq.get(lot, 0.0)) if lot in lq.index else 0.0
        extra = m - base
        if extra > tol * max(base, 1.0):
            n_src += 1
            push(lot, extra)
    if not add:
        for p in (lp, ep):
            shutil.copy(p + ".r1", p)
        return "no extra quantity to feed back"
    for p, q in add.items():
        bs = float(pd.to_numeric(L.at[p, "batch_size_kg"], errors="coerce") or 0) or (
            float(L.at[p, "lot_qty"]) / max(float(L.at[p, "batches"] or 1), 1))
        b0 = max(float(L.at[p, "batches"] or 1), 1.0)
        b1 = max(b0, math.ceil((float(L.at[p, "lot_qty"]) + q) / bs - 1e-9))
        L.at[p, "lot_qty"] = b1 * bs
        L.at[p, "raw_demand"] = float(L.at[p, "raw_demand"]) + q
        L.at[p, "batches"] = b1
        for c in ("duration_hours", "duration_min"):
            L.at[p, c] = float(L.at[p, c]) * b1 / b0
    E = E.set_index(["producer_lot", "consumer_lot"])
    new = []
    for (p, c), q in edge_add.items():
        if (p, c) in E.index:
            E.loc[(p, c), "consume_qty"] = E.loc[(p, c), "consume_qty"] + q
        else:
            new.append({"producer_lot": p, "producer_item": L.at[p, "item_code"], "consumer_lot": c,
                        "consumer_item": item_of.get(c, ""), "via_sku": "", "consume_qty": q})
    E = pd.concat([E.reset_index(), pd.DataFrame(new)], ignore_index=True)
    L.reset_index().to_csv(lp, index=False)
    E.to_csv(ep, index=False)
    kg = sum(add.values())
    return (f"{n_src} lots made more than planned -> {len(add)} compound lots +{kg:,.0f} kg "
            f"({len(new)} new links)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start")
    # E5: nargs="+" and repeatable. Named phases run in PIPELINE order, never argv order.
    ap.add_argument("--only", nargs="+", action="extend", default=[])
    ap.add_argument("--skip", nargs="*", action="extend", default=[])
    ap.add_argument("--force", action="store_true",
                    help="run even if upstream outputs are MISSING or STALE")
    ap.add_argument("--no-converge", action="store_true",
                    help="S2: force a single pass even if converge_building_to_press "
                         "is on in config (for a byte-identical baseline)")
    a = ap.parse_args()

    os.makedirs(os.path.join(HERE, "outputs"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "outputs2"), exist_ok=True)

    print("=" * 74)
    # ASCII only in this file's OWN prints: the parent process inherits the cp1252
    # console, and an em dash here prints as a mojibake '?'. Same hazard class as the
    # config.yaml ASCII rule. (The CHILD phases get PYTHONIOENCODING=utf-8, so their
    # own unicode is fine -- see run_phase.)
    print("  CTP scheduler - v6_wave_p2 algorithm, unmodified")
    print("=" * 74)
    preflight()

    only = _split_phases(a.only, "--only")
    skip = _split_phases(a.skip, "--skip")

    todo = PHASES
    if only:
        # PIPELINE ORDER, not argv order. De-duplicated.
        todo = [p for p in PHASES if p[0] in set(only)]
        if [p[0] for p in todo] != [k for i, k in enumerate(only) if k not in only[:i]]:
            print(f"  --only reordered to pipeline order: "
                  f"{', '.join(p[0] for p in todo)}")
    elif a.start:
        if a.start not in PHASE_KEYS:
            sys.exit(f"unknown phase {a.start!r}; expected one of {PHASE_KEYS}")
        todo = PHASES[PHASE_KEYS.index(a.start):]
    todo = [p for p in todo if p[0] not in skip]
    if not todo:
        sys.exit("nothing to run (every phase was skipped)")

    todo_keys = [p[0] for p in todo]
    if len(todo) < len(PHASES):
        print(f"  running {len(todo)} of {len(PHASES)} phases: {', '.join(todo_keys)}")
        if not check_inputs_before_run(todo_keys, a.force):
            return 2

    # S2 -- convergence is orchestrated here when the config flag is on AND this run
    # re-computes both phase4 (the re-anchor) and phase6 (the pin / the P1 source).
    cfg = _read_cfg()
    converge_on = (bool(cfg.get("converge_building_to_press", False))
                   and not a.no_converge
                   and "phase4" in todo_keys and "phase6" in todo_keys)
    if converge_on:
        # Pass 1 MUST build to plan need_by, so it must not see a stale P1 snapshot.
        if os.path.exists(P1_SNAPSHOT):
            os.remove(P1_SNAPSHOT)
        print("  [S2] converge_building_to_press=ON -- pass 1 builds to plan need_by "
              "(P1 snapshot cleared); convergence runs after.")

    t0 = time.time()
    for key, script, title in todo:
        if not run_phase(key, script, title):
            return 1
    print("\n" + "=" * 74)
    print(f"  DONE (pass 1) - {len(todo)} phases in {time.time()-t0:.0f}s")
    print("=" * 74)

    if converge_on:
        rc = run_convergence(int(cfg.get("converge_max_iters", 4) or 4))
        if rc != 0:
            return rc

    # v69 #3 (user 2026-10-10): CLOSED-LOOP MIXING -- re-plan the mixers on the round-1 schedule's REAL consumer starts
    _cl = ((cfg.get("mix_planner") or {}).get("closed_loop") or {})
    if _cl.get("enabled", False) and "phase5" in todo_keys and "phase6" in todo_keys:
        for _f in ("phase2_lots_updated.csv.r1", "phase3_dag_edges_updated.csv.r1"):   # v76: fresh round-1 originals
            if os.path.exists(os.path.join(HERE, "outputs2", _f)):
                os.remove(os.path.join(HERE, "outputs2", _f))
        for _rnd in range(2, int(_cl.get("rounds", 2)) + 1):
            _fbp = os.path.join(HERE, "outputs2", "mixplan_feedback.csv")
            if _cl.get("qty_feedback", False):
                print(f"\n  [QTY FEEDBACK v76] round {_rnd}: {_qty_feedback()}")
            _n = _write_mix_feedback(_fbp, float(_cl.get("buffer_h", 2)))
            print(f"\n  [CLOSED LOOP v69] round {_rnd}: {_n:,} compound lots get their real need from round {_rnd - 1}")
            os.environ["CTP_MIXFB"] = _fbp
            for key, script, title in [p for p in PHASES if p[0] in ("phase5", "phase6")]:
                if not run_phase(key, script, title):
                    return 1
            os.environ.pop("CTP_MIXFB", None)

    report_stale_after_run(todo_keys)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
