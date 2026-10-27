#!/usr/bin/env python3
"""run_all.py — the CTP e2e pipeline, phase by phase, exactly as v10 runs.

    python run_all.py                 # both lines, run e2e_v1
    python run_all.py --line pcr      # one line
    python run_all.py --only phase5   # one phase
    python run_all.py --skip phase0

Each phase reads the previous phase's CSVs out of outputs/<run>/<line>/, so the
order is not negotiable and a failure aborts the line rather than running on
with stale inputs — v10's own rule.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time

import rules as R

ROOT = pathlib.Path(__file__).resolve().parent

# Phases that must see BOTH lines at once when the rule file says the machines
# are shared. Assignment and placement are the only two that book a machine, so
# they are the only two that can double-book one.
JOINT_PHASES = {"phase4b", "phase5"}

PIPELINE = [
    ("phase0", "phase0_validate.py", "Effective plan — what the masters can build"),
    ("phase1", "phase1_building_demand.py", "Building plan -> component demand"),
    ("phase2", "phase2_wip_netting.py", "WIP netting (per-batch FEFO + cascade)"),
    ("phase3", "phase3_lot_sizing.py", "Runs and lots (campaign-bounded)"),
    ("phase4", "phase4_cpm.py", "Precedence DAG + backward CPM"),
    # v10 order: the campaign re-times the calender/slitter lots, THEN the
    # upstream need-by is re-aimed at that campaign, and only then is the
    # machine assigned. Running assignment first would assign against
    # deadlines the campaign is about to abandon.
    ("campaign", "ctp_campaign.py", "Campaign blocks -> release schedule"),
    ("align", "ctp_aligned_needby.py", "Re-aim upstream need-by at the campaign"),
    ("phase4b", "phase4b_assignment.py", "Machine assignment"),
    ("phase5", "phase5_placement.py", "Placement over the DAG + full-kit gate"),
    ("phase6", "phase6_floor_schedule.py", "Floor schedule (datetime + shift wise)"),
    ("phase7", "phase7_curing.py", "Curing, gated on real green tyres"),
]
def _extra(pid: str) -> list[str]:
    e = R.engine
    if pid == "phase4":
        return ["--open-h", str(e("open_h", 24))]
    if pid == "phase5":
        a = ["--open-h", str(e("open_h", 24)),
             "--campaign-h", str(e("campaign_h", 72)),
             "--policy", str(e("policy", "shift")),
             "--horizon-extend-h", str(e("horizon_extend_h", 0))]
        # PASS 2 IS A CHOICE, NOT A FIXTURE. The backward right-shift slides every
        # lot toward its consumer for freshness. That is right when there is
        # slack to spend; it is actively wrong when the plant is already behind,
        # because it holds a machine idle in front of work that is late.
        # `dispatch.right_shift: false` turns it off and every lot runs as early
        # as its material allows.
        if str(R.rule("dispatch.right_shift", True)).lower() in ("0","off","false","no"):
            a.append("--no-right-shift")
        return a
    if pid == "phase7":
        return ["--horizon-extend-h", str(e("horizon_extend_h", 0))]
    if pid == "phase3":
        return ["--mix-batches", str(e("mix_mpq_batches", 3)),
                "--max-lot-h", str(e("max_lot_h", 8)),
                "--efficiency", str(e("efficiency", 1.0))]
    return []


def _no_eligible_demand(run: str, line: str) -> bool:
    import ctpio as _io
    import pandas as _pd
    p = _io.OUT_ROOT / run / line / "phase0_plan_effective.csv"
    try:
        return p.exists() and len(_pd.read_csv(p)) == 0
    except Exception:
        return True                  # an empty file has no header to read


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--line", nargs="+", default=["pcr", "tbr"])
    ap.add_argument("--only", nargs="+", default=[])
    ap.add_argument("--skip", nargs="+", default=[])
    args = ap.parse_args()

    todo = [p for p in PIPELINE
            if (not args.only or p[0] in args.only) and p[0] not in args.skip]
    # `campaign.enabled: false` removes both steps, so a run can be compared
    # with and without campaigning by swapping one line of the rule file.
    if not bool(R.rule("campaign.enabled", False)):
        todo = [p for p in todo if p[0] not in ("campaign", "align")]
    joint_mode = (str((R.cfg().get("shared_machines") or {}).get("joint", "all"))
                  .lower() not in ("", "none", "off", "0", "false"))
    joint = joint_mode and len(args.line) > 1

    # v36 CLOSED LOOP (MIXING_RULES_README s.6): phase 5 is run `rounds` times;
    # rounds 2+ re-plan the mixers on the previous round's real consumer starts
    _mp = R.rule("mix_planner") or {}
    _cl = (_mp.get("closed_loop") or {}) if _mp.get("enabled") else {}
    mix_rounds = int(_cl.get("rounds", 1)) if _cl.get("enabled") else 1

    def run(pid, script, label, target):
        if pid == "phase5" and mix_rounds > 1:
            for k in range(1, mix_rounds + 1):
                rc = _run(pid, script,
                          f"{label}  (mixing closed loop, round {k}/{mix_rounds})",
                          target, {"CTP_MIX_ROUND": str(k)})
                if rc != 0:
                    return rc
            return 0
        return _run(pid, script, label, target, {})

    def _run(pid, script, label, target, env_extra):
        import os
        env = dict(os.environ)
        env.update(env_extra)
        t = time.time()
        print(f"\n>>> {pid}  {label}   [{target}]", flush=True)
        rc = subprocess.run(
            [sys.executable, str(ROOT / script), "--line", target,
             "--run", args.run] + _extra(pid), cwd=str(ROOT), env=env).returncode
        print(f"  -> {'OK' if rc == 0 else f'FAILED rc={rc}'} in {time.time()-t:.1f}s",
              flush=True)
        return rc

    t0 = time.time()
    jnt = [p for p in todo if p[0] in JOINT_PHASES] if joint else []
    split = min([todo.index(p) for p in jnt], default=len(todo))
    if joint:
        print(f"\n  SHARED MACHINES: joint="
              f"{(R.cfg().get('shared_machines') or {}).get('joint')}"
              f"  ->  {[p[0] for p in jnt]} run ONCE across "
              f"{'+'.join(args.line)}, so no machine is booked twice.")
    skipped = []
    for line in list(args.line):
        print("\n" + "#" * 74)
        print(f"#  CTP e2e — run {args.run}   LINE {line.upper()}")
        print("#" * 74)
        for pid, script, label in todo[:split]:
            if run(pid, script, label, line) != 0:
                print(f"\n[ABORT] {line}/{pid} failed", file=sys.stderr)
                return 1
            # v46j: a line whose every SKU is DATA_BLOCKED has nothing to
            # schedule; it is stopped after phase 0 (its blocked demand is
            # still reported) instead of crashing on an empty plan
            if pid == "phase0" and _no_eligible_demand(args.run, line):
                print(f"\n  [LINE STOPPED] {line.upper()}: no eligible demand after phase 0 - "
                      f"every SKU is DATA_BLOCKED (see phase0_sku_status.csv)")
                skipped.append(line)
                break
    if skipped:
        args.line = [x for x in args.line if x not in skipped]
        joint = joint_mode and len(args.line) > 1
        jnt = [p for p in todo if p[0] in JOINT_PHASES] if joint else []
    if jnt:
        print("\n" + "#" * 74)
        print(f"#  CTP e2e — run {args.run}   JOINT  "
              f"{'+'.join(a.upper() for a in args.line)}")
        print("#" * 74)
        for pid, script, label in jnt:
            if run(pid, script, label, ",".join(args.line)) != 0:
                print(f"\n[ABORT] joint/{pid} failed", file=sys.stderr)
                return 1
    for line in args.line:
        for pid, script, label in todo[split:]:
            if joint and pid in JOINT_PHASES:
                continue
            if run(pid, script, label, line) != 0:
                print(f"\n[ABORT] {line}/{pid} failed", file=sys.stderr)
                return 1
    print(f"\n{'='*74}\n  PIPELINE OK  ({time.time()-t0:.1f}s)\n{'='*74}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
