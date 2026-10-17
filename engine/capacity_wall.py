#!/usr/bin/env python
"""
CAPACITY WALL report (2026-08-06).  Report-only; never fails the run.

WHY THIS EXISTS.  A single machine whose TOTAL WORK exceeds the plan window is a
hard floor on the makespan: no amount of scheduling/sequencing/warm-start can make
a machine that needs 39 days of work finish in 30.  That is NOT a scheduler bug and
NOT fixable in code -- it needs a parallel machine from the plant.  This report makes
every such wall VISIBLE at the end of every run (with its component and whether the
routing even has an alternate machine), so the bottleneck is named up-front next
month instead of being re-diagnosed by hand.

Reads: outputs2/phase5_floor_schedule_updated.csv  (the placed floor)
       inputs/routing.csv                          (alt_machine_count + siblings)
Usage:  python capacity_wall.py [window_days=30]
"""
import os, sys, re
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
FS   = os.path.join(HERE, "outputs2", "phase5_floor_schedule_updated.csv")
RT   = os.path.join(HERE, "inputs", "routing.csv")


def _machines(spec):
    """Pull machine tokens out of a routing 'machines' cell like '\"1101\", \"1901\"'."""
    return set(re.findall(r"[A-Za-z0-9\-]+", str(spec))) if spec is not None else set()


def main():
    window = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    if not os.path.exists(FS):
        print(f"  [capacity-wall] no floor schedule at {FS}; skipped.")
        return 0
    try:
        d = pd.read_csv(FS, dtype=str)
        d["s"] = pd.to_datetime(d["start_time"], errors="coerce")
        d["e"] = pd.to_datetime(d["end_time"], errors="coerce")
        d = d.dropna(subset=["s", "e"]).copy()
        d["bh"] = (d["e"] - d["s"]).dt.total_seconds() / 3600.0
        d["mc"] = d["machine"].astype(str).str.strip()
        d["dept"] = d["department"].astype(str).str.upper()

        # UPSTREAM machines only -- BUILDING/CURING are the PINNED 30-day drum by design;
        # a component machine over the window is the thing that starves that drum.
        up = d[~d["dept"].str.contains("BUILD") & ~d["dept"].str.contains("CUR")]
        per = (up.groupby("mc")
                 .agg(work_d=("bh", lambda x: x.sum() / 24.0),
                      component=("item_type", lambda s: s.astype(str).value_counts().index[0]),
                      last=("e", "max"))
                 .reset_index())

        # routing: machine -> alt_machine_count, and operation -> sibling machines (for context)
        alt_by_mc, op_of_mc, mcs_of_op = {}, {}, {}
        if os.path.exists(RT):
            r = pd.read_csv(RT, dtype=str)
            for _, row in r.iterrows():
                op = str(row.get("operation_name", "")).strip()
                mset = _machines(row.get("machines", ""))
                try:
                    alt = int(float(row.get("alt_machine_count", 0) or 0))
                except (TypeError, ValueError):
                    alt = 0
                for m in mset:
                    alt_by_mc[m] = max(alt_by_mc.get(m, 0), alt)
                    op_of_mc.setdefault(m, op)
                    mcs_of_op.setdefault(op, set()).add(m)

        over = per[per["work_d"] > window].sort_values("work_d", ascending=False)
        print("\n" + "#" * 74)
        print(f"#  CAPACITY WALL  (upstream machines over the {window:.0f}-day plan window)")
        print("#" * 74)
        if over.empty:
            print(f"  None. Every upstream machine's total work fits inside {window:.0f} days.")
            return 0
        print("  A machine whose total WORK exceeds the window is a HARD floor on the")
        print("  makespan -- it cannot be sequenced away; it needs a PARALLEL machine.")
        print(f"  {'MACHINE':<8}{'COMPONENT':<26}{'WORK-d':>7}  {'ALTS':>4}  VERDICT")
        for _, row in over.iterrows():
            mc = row["mc"]; alt = alt_by_mc.get(mc, 0)
            op = op_of_mc.get(mc, "")
            sibs = sorted(mcs_of_op.get(op, set()) - {mc})
            if alt > 0:
                verdict = f"has {alt} alternate(s) -- rebalance possible"
            elif sibs:
                verdict = f"NO per-item alt; op also on {','.join(sibs)} (plant must confirm capability)"
            else:
                verdict = "HARD WALL -- no alternate machine in routing"
            print(f"  {mc:<8}{str(row['component'])[:25]:<26}{row['work_d']:>7.1f}  {alt:>4}  {verdict}")
        floor = over["work_d"].max()
        worst = over.iloc[0]["mc"]
        print(f"\n  => Makespan FLOOR ~= {floor:.1f} d (machine {worst}) + the downstream chain.")
        print(f"     Sequencing / warm-start / gap-fill CANNOT beat this. To go lower, the")
        print(f"     PLANT must add a parallel machine for the HARD WALL row(s) above.")
    except Exception as e:
        print(f"  [capacity-wall] report failed ({e!r}); skipped (run not affected).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
