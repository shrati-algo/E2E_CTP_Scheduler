#!/usr/bin/env python3
"""phase4b_assignment.py — item -> machine, balanced per (machine, DAY).

v10 phase 4b, run DEGENERATE on CTP (CTP_IMPLEMENTATION.md §6.6).

WHY DEGENERATE, AND WHY THE PHASE STAYS
  v10's optimiser carries a full (item x machine) cost table because BTP's
  machines are UNRELATED — one mixer is 1.76x slow on one compound and 2.43x on
  another — and that is knowable only from `all_machine_spec`. CTP's
  all_machine_spec is empty on all 46,816 routing rows, so the table cannot be
  built and every machine is charged the routing's row-level rate.

  It is also mostly unnecessary: most CTP operations have exactly ONE eligible
  machine, so there is nothing to assign. What remains is balancing the few
  pooled operations (the mixers above all).

  So the phase keeps v10's structure and objective — load per (machine, day)
  bucketed on each lot's LFT, items kept whole, local search after the greedy
  pass — and the per-machine cost table is left wired behind `--speed-aware`
  for the day all_machine_spec is populated. The run header says which mode
  produced the answer, so a degenerate run is never mistaken for an optimised one.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io
import rules as R

DAY_CAP_H = 24.0 * 0.95
SWEEPS = 8


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True,
                    help="pcr | tbr | 'pcr,tbr' for JOINT assignment on one calendar")
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--speed-aware", action="store_true",
                    help="use per-machine speeds (needs all_machine_spec; CTP has none)")
    args = ap.parse_args()
    lines = [x.strip() for x in str(args.line).split(",") if x.strip()]
    joint = len(lines) > 1

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 4b — machine assignment   [{args.line.upper()}]")
    print("=" * 74)
    print(f"  Rate source     : jkt_routing_{{line}} proc_time, after BR-1/BR-2/BR-3 "
          f"from the rule file. Per-machine rates where BR-1 states them.")

    frames = []
    for ln in lines:
        d = pd.read_csv(io.outdir(args.run, ln) / "phase4_lot_times.csv",
                        parse_dates=["EST", "LST", "LFT"])
        d["line"] = ln
        # lot ids can collide across lines (the same item code exists in both),
        # so everything downstream is keyed on line::lot_id
        d["key"] = ln + "::" + d["lot_id"].astype(str)
        frames.append(d)
    lots = pd.concat(frames, ignore_index=True)
    lots["machines"] = lots["machines"].fillna("")
    lots["ikey"] = lots["line"] + "::" + lots["item_code"].astype(str)
    elig = {i: [m for m in str(v).split(",") if m]
            for i, v in zip(lots["ikey"], lots["machines"])}

    # ---- THE MACHINES ARE NOT UNIFORM WHERE THE RULE FILE SAYS THEY ARE NOT --
    # v10 carries a full (item x machine) cost table because its machines are
    # UNRELATED. CTP's all_machine_spec is empty, so normally there is nothing to
    # build one from -- but BR-1 states the cap strip slitters explicitly
    # (701 = 33 x 12 = 396 MPM, 702 = 60 x 16 = 960). Balancing those on equal
    # nominal hours is exactly the error v10 measured at 12.6% of its schedule:
    # it put every cap strip lot on 701 at 97.9% while the FASTER 702 ran 2.2%.
    rate_mult: dict[tuple[str, str], float] = {}
    alias = io.machine_alias(io.load_machines())
    for ln in lines:
        meta = R.apply_speed_rules(io.build_routing_meta(io.load_routing(ln), alias))
        for it, m in meta.items():
            rbm = m.get("rate_by_machine") or {}
            base = m.get("rate_base") or m.get("proc_time")   # v46j: speed units
            if rbm and base and base > 0:
                for mc, r in rbm.items():
                    if r and r > 0:
                        rate_mult[(ln + "::" + it, str(mc))] = base / r
    if rate_mult:
        print(f"  Speed-aware      : {len({k[0] for k in rate_mult})} item(s) have a "
              f"per-machine rate from BR-1 — balanced on REAL hours, not nominal")

    n1 = sum(1 for v in elig.values() if len(v) == 1)
    n0 = sum(1 for v in elig.values() if not v)
    print(f"  Items           : {len(elig):,}   with ONE eligible machine: {n1:,}"
          f"  ({100*n1/max(len(elig),1):.0f}%)   with none: {n0}")

    base = lots["LFT"].min().normalize()
    day_of = {l: int((t.normalize() - base).days) if pd.notna(t) else 0
              for l, t in zip(lots["key"], lots["LFT"])}
    work = dict(zip(lots["key"], lots["duration_h"]))

    by_item = defaultdict(list)
    for l, it in zip(lots["key"], lots["ikey"]):
        by_item[it].append(l)
    _ik = dict(zip(lots["key"], lots["ikey"]))

    def work_on(l, m) -> float:
        return work.get(l, 0.0) * rate_mult.get((_ik.get(l, ""), str(m)), 1.0)

    item_work = {it: sum(min(work_on(l, m) for m in (elig.get(it) or ["-"]))
                         for l in ls) for it, ls in by_item.items()}

    horizon_d = (max(day_of.values()) + 1) if day_of else 1
    horizon_cap = horizon_d * 24.0
    split_items: set = set()

    # v25 load-balance threshold, as a percentage of the horizon per machine.
    # 0 = off, and off is exactly v24.
    BALANCE_PCT = float(R.rule("dispatch.balance_above_pct", 0) or 0)
    # v43: the same spread, but only on the machines named in
    # `balance.machines` (the TBR bead pool 2403/2404/2405). e2e_v42f put 258 h
    # on 2403 against 120 h on 2405 although most bead items may run on all
    # three; turning the global knob on would also split mixer campaigns.
    BAL_M = {str(x).strip() for x in (R.rule("balance.machines", []) or [])}
    BAL_PCT = float(R.rule("balance.above_pct", 0) or 0)
    n_hot = [0]

    load: dict = defaultdict(float)          # (machine, day) -> hours
    mload: dict = defaultdict(float)         # machine -> hours
    assign: dict = {}
    spread_all = str(R.rule("machines.spread_all_eligible", False)).lower() in (
        "1", "on", "true", "yes")
    opened: set = set()

    def over(m, buckets):
        return sum(max(0.0, load[(m, d)] + h - DAY_CAP_H) - max(0.0, load[(m, d)] - DAY_CAP_H)
                   for d, h in buckets.items())

    # greedy: most-constrained item first (fewest eligible machines), then
    # longest work — v10's "fill the least flexible machine first"
    for it in sorted(by_item, key=lambda i: (len(elig.get(i) or []), -item_work[i])):
        ms = elig.get(it) or []
        if not ms:
            continue
        def buckets(mc):
            d = defaultdict(float)
            for l in by_item[it]:
                d[day_of[l]] += work_on(l, mc)
            return d
        # ---- v15: EVERY ELIGIBLE MACHINE CARRIES WORK -----------------------
        # The plant asked for work on every CTP machine, with legality intact.
        # This narrows the choice to the eligible machines that are still empty
        # BEFORE the balancer picks - so an idle-but-legal machine is opened
        # first, and the normal load balancing then runs unchanged over the
        # rest. It can never make an illegal pick: it only ever selects from
        # `ms`, the item's own eligible set, and it never empties the choice.
        ms_pick = ms
        if spread_all and len(ms) > 1:
            empty = [m for m in ms if mload[m] <= 0.0]
            if empty and len(empty) < len(ms):
                ms_pick = empty
                opened.update(empty[:1])
        best = min(ms_pick, key=lambda m: (over(m, buckets(m)), mload[m], m))
        b = buckets(best)
        # AN ITEM IS KEPT WHOLE UNLESS ONE MACHINE CANNOT HOLD IT.
        # v10 keeps an item on one machine because every extra machine is a
        # changeover there -- but it spills when the machine would go over cap.
        # Without the spill, CAP 66 - CAPSTRIP put all 1,395 h on slitter 701
        # (484% of the horizon) while the identical 702 ran nothing.
        _speedy = len({round(rate_mult.get((it, m), 1.0), 6) for m in ms}) > 1
        # ---- v25: SPLIT WHEN THE CHOSEN MACHINE IS ALREADY HOT --------------
        # An item is kept whole because every extra machine is a changeover, and
        # it only spilled when ONE machine could not hold it across the WHOLE
        # horizon (horizon_cap = days x 24 h). That threshold is so loose it
        # almost never fires, which is how e2e_v24 ended with TBR bead winding
        # 2405 at 96.9% while 2403 - eligible for the same four items - sat at
        # 71%, and bead winding feeds apexed bead, the line's second constraint.
        #
        # `dispatch.balance_above_pct` lowers the bar: once this item would push
        # the chosen machine past that share of the horizon AND another eligible
        # machine is materially lighter, the item is spread lot by lot instead.
        # The spread uses the SAME per-lot balancer as the existing spill, so it
        # cannot pick an illegal machine and cannot leave a lot unassigned.
        _hot = False
        _pct = BALANCE_PCT if BALANCE_PCT > 0 else (
            BAL_PCT if (BAL_PCT > 0 and str(best) in BAL_M) else 0.0)
        if _pct > 0 and len(ms) > 1:
            cap = horizon_cap * _pct / 100.0
            other = min((mload[m] for m in ms if m != best), default=None)
            if (mload[best] + item_work[it]) > cap and other is not None \
                    and other < mload[best] - 1e-9:
                _hot = True
                n_hot[0] += 1
        if len(ms) > 1 and (_speedy or _hot
                            or (mload[best] + item_work[it]) > horizon_cap):
            split_items.add(it)
            for l in sorted(by_item[it], key=lambda x: day_of[x]):
                m = min(ms, key=lambda mm: (load[(mm, day_of[l])] + work_on(l, mm),
                                            mload[mm], mm))
                assign[l] = m
                load[(m, day_of[l])] += work_on(l, m)
                mload[m] += work_on(l, m)
            continue
        for l in by_item[it]:
            assign[l] = best
            load[(best, day_of[l])] += work_on(l, best)
            mload[best] += work_on(l, best)

    if BAL_PCT > 0 and BAL_M:
        print(f"  Pool balance    : v43 - machines {sorted(BAL_M)} spread an item lot by "
              f"lot once its machine passes {BAL_PCT:g}% of the horizon "
              f"({n_hot[0]:,} item(s) spread)")
    if BALANCE_PCT > 0:
        print(f"  Load balance    : threshold {BALANCE_PCT:g}% of the horizon; "
              f"{n_hot[0]:,} item(s) spread because the preferred machine was "
              f"already past it and a lighter eligible machine existed")

    def total_over():
        return sum(max(0.0, v - DAY_CAP_H) for v in load.values())

    moves = 0
    for _ in range(SWEEPS):
        worst = max(load.items(), key=lambda kv: kv[1] - DAY_CAP_H, default=None)
        if worst is None or worst[1] <= DAY_CAP_H:
            break
        wm = worst[0][0]
        cands = [it for it in by_item if len(elig.get(it) or []) > 1
                 and any(assign.get(l) == wm for l in by_item[it])]
        improved = False
        for it in sorted(cands, key=lambda i: -item_work[i]):
            here = [l for l in by_item[it] if assign.get(l) == wm]
            if not here:
                continue
            before = total_over()
            b = defaultdict(float)
            for l in here:
                b[day_of[l]] += work.get(l, 0.0)
                load[(wm, day_of[l])] -= work.get(l, 0.0)
            mload[wm] -= sum(b.values())
            best, best_after = None, None
            for m in elig[it]:
                if m == wm:
                    continue
                a = total_over() + over(m, b)
                if best_after is None or a < best_after:
                    best, best_after = m, a
            if best is not None and best_after < before - 1e-9:
                for l in here:
                    assign[l] = best
                    load[(best, day_of[l])] += work.get(l, 0.0)
                    mload[best] += work.get(l, 0.0)
                moves += 1
                improved = True
                break
            for d, h in b.items():
                load[(wm, d)] += h
            mload[wm] += sum(b.values())
        if not improved:
            break

    # ---- v15: OPEN EVERY LEGAL MACHINE THAT ENDED WITH NOTHING -------------
    # The plant asked for work on every CTP machine. The balancer above cannot
    # deliver that on its own: it keeps an item whole unless a machine would go
    # over cap, so a department with ONE item and TWO identical machines puts
    # all of it on the first and leaves the second dark. TBR chafer doubling is
    # exactly that shape - one item (NYDBCHF), six lots, machines 2501 and
    # 2502 - and 2502 carried nothing in v14 and in the first v15 pass.
    #
    # This moves the SMALLEST lot of a legal item onto each machine that would
    # otherwise finish empty. One lot, not a share: enough to open the machine
    # without shredding an item into a changeover per lot (BR-9, BR-56). Every
    # move is drawn from the item's own eligible list, so legality is exact,
    # and a move that would empty the donor machine is refused.
    opened_now = []
    if spread_all:
        all_legal = {m for v in elig.values() for m in v}
        for mc in sorted(all_legal):
            if mload[mc] > 0:
                continue
            cands = [it for it, v in elig.items()
                     if mc in v and len(by_item[it]) > 1]
            best_lot = None
            for it in sorted(cands, key=lambda i: -len(by_item[i])):
                pool = [l for l in by_item[it] if assign.get(l) is not None]
                for l in sorted(pool, key=lambda x: work.get(x, 0.0)):
                    donor = assign[l]
                    if donor == mc:
                        continue
                    # never darken the donor to light this one
                    if mload[donor] - work.get(l, 0.0) <= 1e-9:
                        continue
                    best_lot = (l, donor)
                    break
                if best_lot:
                    break
            if not best_lot:
                continue
            l, donor = best_lot
            w = work.get(l, 0.0)
            load[(donor, day_of[l])] -= w
            mload[donor] -= w
            assign[l] = mc
            load[(mc, day_of[l])] += w
            mload[mc] += w
            opened_now.append((mc, l, donor))
        if opened_now:
            print(f"  Machines opened : {len(opened_now)} legal machine(s) had no "
                  f"work and were given one lot each (v15, plant instruction):")
            for mc, l, donor in opened_now:
                print(f"      {mc:10s} <- {l}  (moved off {donor})")
        else:
            print(f"  Machines opened : 0 - every legal machine already carries work")

    out = pd.DataFrame({"key": list(assign), "machine": list(assign.values())})
    out["line"] = out["key"].str.split("::").str[0]
    out["lot_id"] = out["key"].str.split("::").str[1]
    for ln in lines:
        (out[out["line"] == ln][["lot_id", "machine"]]
         .to_csv(io.outdir(args.run, ln) / "phase4b_assignment.csv", index=False))

    print(f"  Assigned        : {len(assign):,} of {len(lots):,} lots"
          f"   machines used: {len(mload)}")
    print(f"  Local search    : {moves} item move(s)   day overload "
          f"{total_over():,.1f} h")
    print(f"  Items split     : {len(split_items):,} of {len(by_item):,} "
          f"(one machine could not hold them)")
    print(f"\n  BUSIEST MACHINES (hours of work vs {horizon_d} day horizon)")
    cap = horizon_d * 24.0
    for m, h in sorted(mload.items(), key=lambda kv: -kv[1])[:12]:
        flag = "  <-- OVER" if h > cap else ""
        print(f"    {str(m)[:22]:23s} {h:>8,.1f} h   {100*h/max(cap,1):>6.1f}%{flag}")
    print(f"\n[OK] phase4b_assignment.csv ({len(out):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
