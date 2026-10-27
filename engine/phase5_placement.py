#!/usr/bin/env python3
"""phase5_placement.py — precedence-correct JIT list-scheduling over the lot DAG.

v10 phase 5 / dag_schedule.py on CTP data. The two passes are v10's exactly.

PASS 1 — FORWARD (feasibility). Lots are released in topological order, so a lot
is placed only once every predecessor is placed. Its start is floored at

    max( floor_open,
         max over predecessors p of ( finish[p] + min_aging(item[p]) + transfer ),
         machine_free + changeover,
         EST )                                  <- the release window

which makes precedence and minimum aging true BY CONSTRUCTION. Among the lots
whose predecessors are done, the next is chosen by (campaign, LFT, lot) so
same-item lots run back to back and the changeover saving is kept. Ready work is
held PER MACHINE and bucketed by item — a single global queue ordered the same
way pops whatever is globally next, not what THIS machine is set up for, which
tripled BTP's setup time.

PASS 2 — BACKWARD RIGHT-SHIFT (freshness). Pass 1 makes everything as EARLY as
possible, which is exactly what expires material. Each lot is then slid as LATE
as it legally can go, in reverse topological order:

    latest_finish = min( LFT,
                         min over successors s of ( start[s] - min_aging - transfer ),
                         start of the next lot on this machine - changeover )

Nothing moves earlier and nothing crosses a successor or a machine neighbour, so
pass 1's feasibility is preserved.

GUARDS CARRIED OVER FROM v10 BECAUSE THE FAILURES THEY PREVENT ARE NOT
BTP-SPECIFIC:
  * a lot that already has a start is never placed again (BTP's double-placement
    bug ran 639 consecutive lots at a 50% duty cycle);
  * the releasability filter and the commit use the SAME clock — a lot that
    passes the filter but cannot start commits the machine through the wait;
  * never-idle may only take work whose material has actually arrived.

THE FULL-KIT GATE. A building trolley cannot start until EVERY component is
finished and has served its minimum aging. --policy shift moves the trolley and
records the revision; --policy frozen reports the shortage instead.
"""
from __future__ import annotations

import argparse
import heapq
import re
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

import ctpio as io
import upstream_rules as UR
import rules as R

DEFAULT_CO_MIN = 7.0          # v10's last resort when no table has anything to say
CAMPAIGN_H = 72.0


# ---------------------------------------------------- changeover role mapping
# THE ROLE MAP LIVES IN config/rules.yaml, NOT HERE.
#
# It used to be a list of regexes guessed from MasterMachines display names.
# That is how the CAP STRIP slitters (701/702) ended up pointed at the matrix's
# "Chaffer Slitter" role -- a different machine -- while the real chaffer
# slitters (2401/2402), the edge gum calender (1102) and the bias cutter (2001)
# got no role at all and fell to a flat default. Measured: 34.2% of all
# transitions were priced by the fallback, every one of them for want of a map
# entry rather than a missing pair in the matrix.
def machine_roles(_machines=None) -> dict[str, str]:
    """machine id -> the matrix's own `machine` value, from the rule file.

    LOWER-CASED, because `load_changeover` indexes the matrix on
    `machine.lower()`. Returning the rule file's own capitalisation silently
    missed every lookup and sent 100% of transitions to the fallback.
    """
    return {str(k): str(v).strip().lower() for k, v in
            ((R.cfg().get("changeover") or {}).get("role_map") or {}).items()}


def unpriced_min() -> float:
    """What a transition the matrix cannot price costs. STRICTLY matrix-only:
    the engine never substitutes a constant of its own."""
    return float((R.cfg().get("changeover") or {}).get("unpriced_min", 0.0))



def _building_allowable(lines, alias) -> dict:
    """SKU -> the set of builders the PLANT allows, from its own matrix.

    Master_Building_Allowable_Machines_{line} is the plant-approved list
    (BRE-06 / BRM-11). TBR states it as `TBM-01..TBM-09`, PCR as bare work
    centre ids, so every entry is resolved through the same alias the plan
    itself is read with - otherwise TBM-06 and 3806 look like two machines.
    """
    import json
    out: dict = {}
    for ln in lines:
        f = io.STAGE / f"Master_Building_Allowable_Machines_{ln}.csv"
        if not f.exists():
            continue
        d = pd.read_csv(f, encoding="utf-8-sig")
        for r in d.itertuples(index=False):
            raw = getattr(r, "Machines", None)
            if not raw or not isinstance(raw, str):
                continue
            try:
                lst = json.loads(raw)
            except Exception:
                continue
            got = set()
            for m in lst:
                rm = io.resolve_machine(str(m), alias) or str(m).strip()
                got.add(str(rm))
            if got:
                out.setdefault(str(r.SKUCode).strip(), set()).update(got)
    return out


def _serialise(idx_list, blots, past_holiday, dur_fn=None):
    """Kit-ready dispatch over one machine's trolleys. Returns {idx: start}.

    Reads `_release` - the moment the KIT is actually ready - never the placed
    time, which is an output of this function and would make it circular.
    """
    pend = list(idx_list)
    out, clock, last = {}, None, None
    while pend:
        if clock is None:
            clock = min(blots.at[i, "_release"] for i in pend)
        ready = [i for i in pend if blots.at[i, "_release"] <= clock]
        if not ready:
            clock = min(blots.at[i, "_release"] for i in pend)
            ready = [i for i in pend if blots.at[i, "_release"] <= clock]
        pick = min(ready, key=lambda i: (blots.at[i, "build_start"],
                                         blots.at[i, "building_lot_id"]))
        st = past_holiday(max(clock, blots.at[pick, "_release"]))
        out[pick] = st
        clock = st + (dur_fn(pick, last) if dur_fn else blots.at[pick, "_dur"])
        last = blots.at[pick, "sku"]
        pend.remove(pick)
    return out


WIND_PAT = "BEAD WINDING"
APEX_PAT = "APEX"


def _bead_gate(sched: pd.DataFrame) -> pd.DataFrame:
    """Keep cumulative BEAD WINDING at or ahead of cumulative BEAD APEXING.

    Returns the schedule with offending apex lots right-shifted. Reports what it
    moved, because a rule that fires silently cannot be audited.
    """
    if not len(sched) or "department" not in sched.columns:
        return sched
    d = sched.copy()
    dep = d["department"].astype(str).str.upper()
    for ln in sorted(d["line"].dropna().unique()):
        m = d["line"] == ln
        wind = d[m & dep.str.contains(WIND_PAT, na=False)]
        apex = d[m & dep.str.contains(APEX_PAT, na=False)
                 & ~dep.str.contains(WIND_PAT, na=False)]
        if not len(wind) or not len(apex):
            continue
        w = wind.sort_values("finish")
        w_time = pd.to_datetime(w["finish"]).to_numpy()
        w_cum = w["lot_qty"].astype(float).cumsum().to_numpy()
        a = apex.sort_values("finish")
        moved = 0
        max_h = 0.0
        for idx, need in zip(a.index, a["lot_qty"].astype(float).cumsum()):
            j = int(np.searchsorted(w_cum, need, side="left"))
            if j >= len(w_time):
                continue                      # winding never covers it: a data gap,
                                              # reported by the checker, not forced
            earliest = pd.Timestamp(w_time[j])
            cur = pd.to_datetime(d.at[idx, "finish"])
            if pd.notna(cur) and cur < earliest:
                dur = pd.to_datetime(d.at[idx, "finish"]) - pd.to_datetime(d.at[idx, "start"])
                d.at[idx, "finish"] = earliest
                d.at[idx, "start"] = earliest - dur
                moved += 1
                max_h = max(max_h, (earliest - cur).total_seconds() / 3600.0)
        if moved:
            print(f"  Bead gate       : {ln.upper()} {moved} apex lot(s) held for "
                  f"winding, max +{max_h:.1f} h")
        else:
            print(f"  Bead gate       : {ln.upper()} winding already leads apexing "
                  f"at every point - nothing moved")
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True,
                    help="pcr | tbr | 'pcr,tbr' for JOINT placement on one calendar")
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--open-h", type=float, default=24.0)
    ap.add_argument("--horizon-extend-h", type=float, default=0.0,
                    help="push the counting horizon this many hours past the "
                         "plan's last build")
    ap.add_argument("--campaign-h", type=float, default=CAMPAIGN_H)
    ap.add_argument("--policy", choices=["shift", "frozen"], default="shift")
    ap.add_argument("--no-right-shift", action="store_true")
    args = ap.parse_args()
    lines = [x.strip() for x in str(args.line).split(",") if x.strip()]
    joint = len(lines) > 1
    OUTS = {ln: io.outdir(args.run, ln) for ln in lines}

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 5 — placement over the lot DAG   "
          f"[{'+'.join(l.upper() for l in lines)}]   (policy: {args.policy})")
    if joint:
        print("  JOINT — both lines dispatch on ONE machine calendar, so a machine "
              "either line uses is booked ONCE (config: shared_machines.joint)")
    print("=" * 74)

    mach_df = io.load_machines()
    roles = machine_roles()
    co_unpriced = unpriced_min()
    co_policy = str((R.cfg().get("changeover") or {}).get("source_policy", "matrix_only"))
    co = io.load_changeover()
    itypes, aging, holidays = {}, {}, []
    for ln in lines:
        _b = io.load_bom(ln)
        _t = io.load_itemtypes(_b)
        _a, _ = io.apply_aging_defaults(io.load_aging(ln), set(_t), _t)
        itypes.update(_t)
        # AGING IS A PROPERTY OF (LINE, ITEM), NOT OF THE ITEM.
        # BR-4 keys the plant's age-limit table on item TYPE, and the SAME code
        # is typed differently on the two lines - so the two specs genuinely
        # disagree. Measured on this data: 840 codes differ on MIN aging and 729
        # on MAX. `aging.update()` let the LAST line processed win for both, so
        # PCR steel belt B1-PBLT001 (PCR spec: rest 4 h) was scheduled on TBR's
        # 0 h and treated as usable the moment it was cut. Keyed per line, each
        # lot now gets its own line's spec.
        for _c, _v in _a.items():
            aging[(ln, _c)] = _v
        holidays += io.load_holidays(ln)

    L, E, A, B = [], [], [], []
    for ln in lines:
        O = OUTS[ln]
        d = pd.read_csv(O / "phase4_lot_times.csv", parse_dates=["EST", "LST", "LFT"])
        d["line"] = ln
        d["key"] = ln + "::" + d["lot_id"].astype(str)
        a = pd.read_csv(O / "phase4b_assignment.csv")
        d["machine"] = d["lot_id"].map(dict(zip(a["lot_id"],
                                                a["machine"].astype(str))))
        L.append(d)
        e = pd.read_csv(O / "phase4_edges.csv")
        e["producer_lot"] = ln + "::" + e["producer_lot"].astype(str)
        e["consumer_lot"] = ln + "::" + e["consumer_lot"].astype(str)
        E.append(e)
        al = pd.read_csv(O / "phase3_lot_demand.csv",
                         usecols=["lot_id", "building_lot_id", "qty", "need_by"],
                         parse_dates=["need_by"])
        al["key"] = ln + "::" + al["lot_id"].astype(str)
        al["bkey"] = ln + "::" + al["building_lot_id"].astype(str)
        al["line"] = ln
        A.append(al)
        b = pd.read_csv(O / "phase1_building_lots.csv",
                        parse_dates=["build_start", "build_end"])
        b["line"] = ln
        b["bkey"] = ln + "::" + b["building_lot_id"].astype(str)
        B.append(b)
    lots = pd.concat(L, ignore_index=True)
    edges = pd.concat(E, ignore_index=True)
    alloc = pd.concat(A, ignore_index=True)
    blots = pd.concat(B, ignore_index=True)
    lots = lots[lots["machine"].notna()].copy()

    # ---- 1b. RE-TIME AT THE CHOSEN MACHINE'S OWN SPEED --------------------
    # v10 re-times every lot from all_machine_spec once the machine is known.
    # CTP has no such column, but BR-1 supplies a real per-(item, machine) spec
    # for the cap strip slitters -- 701 runs 33 MPM x 12 bobins = 396, 702 runs
    # 60 x 16 = 960. Charging both the same rate is exactly the error v10 found
    # worth 12.6% of its schedule.
    _meta = {}
    for ln in lines:
        _meta[ln] = R.apply_speed_rules(
            io.build_routing_meta(io.load_routing(ln), io.machine_alias(mach_df)))
    _n_ret = 0
    _new = []
    for r in lots.itertuples(index=False):
        m = (_meta.get(r.line) or {}).get(r.item_code)
        rbm = (m or {}).get("rate_by_machine") or {}
        base = (m or {}).get("rate_base") or (m or {}).get("proc_time")
        mr = rbm.get(str(r.machine))
        # v46j: all_machine_spec gives this machine's own speed (and, for a
        # mixer, its own cycle and batch weight) - time the lot from it directly
        _md = (io.machine_duration_min(r.lot_qty, m, r.machine)
               if (m or {}).get("speed_source") == "all_machine_spec" else None)
        if _md is not None:
            if abs(_md / 60.0 - r.duration_h) > 1e-9:
                _n_ret += 1
            _new.append(_md / 60.0)
        elif mr and base and abs(mr - base) > 1e-9 and base > 0:
            _new.append(r.duration_h * base / mr)
            _n_ret += 1
        else:
            _new.append(r.duration_h)
    _old_h = float(lots["duration_h"].sum())
    lots["duration_h"] = _new
    if _n_ret:
        print(f"  Machine speeds  : {_n_ret:,} lot(s) re-timed at their own "
              f"machine's rate (BR-1)   work {_old_h:,.0f} h -> "
              f"{lots['duration_h'].sum():,.0f} h")

    open_at = blots["build_start"].min() - pd.Timedelta(hours=args.open_h)
    win = pd.Timedelta(hours=args.campaign_h)
    t0 = lots["LFT"].min()
    lots["campaign"] = ((lots["LFT"] - t0) // win).astype("int64")

    print(f"  Lots to place   : {len(lots):,}   machines {lots['machine'].nunique()}")
    print(f"  Floor opens     : {open_at}")
    _used = set(lots["machine"].astype(str))
    _mapped = sorted(m for m in _used if m in roles)
    _unmapped = sorted(m for m in _used if m not in roles)
    print(f"  Changeover      : jkt_changeover_matrix_combined ONLY "
          f"(policy={co_policy}) — {len(co['pair']):,} pairs, "
          f"{len(co['roles'])} role(s)")
    print(f"                    {len(_mapped)} of {len(_used)} scheduled machine(s) "
          f"map to a role" + (f"; NO ROLE: {', '.join(_unmapped)}" if _unmapped else ""))

    # BR4RC-13: FRC ramps up 2 min and down 3 min on every run. That is
    # machine time the lot occupies but does no work in, so it belongs in the
    # DURATION, not in the changeover - the workbook is explicit that creel
    # setup is not a changeover (BR4RC-12) and the ramps are the same kind of
    # thing: they are part of the run.
    _ramp_n = 0
    def _dur_of(r):
        nonlocal _ramp_n
        h = float(r.duration_h)
        up, dn = UR.ramp_min(getattr(r, "department", ""), getattr(r, "operation", ""))
        if up or dn:
            _ramp_n += 1
            h += (up + dn) / 60.0
        return pd.Timedelta(hours=h)

    dur = {r.key: _dur_of(r) for r in lots.itertuples(index=False)}
    if _ramp_n:
        print(f"  BR4RC-13        : ramp up {UR.R.rule('frc.ramp_up_min')} min + "
              f"down {UR.R.rule('frc.ramp_down_min')} min on {_ramp_n:,} FRC lot(s)")
    item = dict(zip(lots["key"], lots["item_code"]))
    dept = dict(zip(lots["key"], lots["department"]))
    lft = dict(zip(lots["key"], lots["LFT"]))
    est = dict(zip(lots["key"], lots["EST"]))
    camp = dict(zip(lots["key"], lots["campaign"]))
    mach_of = dict(zip(lots["key"], lots["machine"]))
    # SHELF LIFE, THE OTHER HALF OF THE AGING MASTER.
    # `aging[item]` is (min_h, max_h) and until now placement read only [0].
    # Min aging says how long material must REST before it may be used; max
    # aging says how long it may WAIT before it is scrap. Phase 2 applied the
    # max to opening WIP and nothing applied it to material the schedule itself
    # makes, so a lot could be produced and consumed 100+ hours later. Measured
    # before this change: 344 of 2,502 PCR edges and 375 of 1,756 TBR edges
    # consumed material past its own shelf life, worst overrun 145 h.
    line_of = dict(zip(lots["key"], lots["line"]))
    _pairs = set(zip(lots["line"], lots["item_code"]))
    max_age = {k: pd.Timedelta(hours=float(aging.get(k, (0.0, 0.0))[1]))
               for k in _pairs if float(aging.get(k, (0.0, 0.0))[1]) > 0}
    min_age = {k: pd.Timedelta(hours=float(aging.get(k, (0.0, 0.0))[0]))
               for k in _pairs}

    def _age_key(lot):
        """(line, item) for a lot key - the only correct way to age it."""
        return (line_of.get(lot, ""), item.get(lot, ""))
    transfer = pd.Timedelta(minutes=io.TRANSFER_MIN)

    ids = set(lots["key"])
    pred = defaultdict(list)
    succ = defaultdict(list)
    indeg = defaultdict(int)
    for p, c in zip(edges["producer_lot"], edges["consumer_lot"]):
        if p in ids and c in ids:
            pred[c].append(p)
            succ[p].append(c)
            indeg[c] += 1

    # ---- v44a: TROLLEY NEED TIME (bead machines) ------------------------------
    # On 2403/2404/2405 the routing runs BOTH bead winding (op 170) and apexing
    # (op 180). A winding lot is always due EARLIER than the apexing lot it feeds,
    # so a deadline-first queue ran winding first every time: in e2e_v43g24 2403
    # wound beads for its first 49 h while 17 of the 20 apexing lots that were
    # the last part for TBR day-1..3 trolleys queued behind it - although the
    # opening stock already held wound beads for them.
    # With `dispatch.trolley_need_first_machines`, lots on those machines are
    # queued by the build start of the EARLIEST TROLLEY they ultimately serve
    # (passed back from the building lots through the material links), then by
    # their own deadline. Physics is untouched: an apexing lot is only ready
    # once its wound beads exist (stock or an earlier winding lot), and the
    # apex <= wound bead gate still runs after placement.
    TN_M = {str(x).strip() for x in (R.rule("dispatch.trolley_need_first_machines", [])
                                     or [])}
    tneed: dict = {}
    tstage: dict = {}      # stages between the lot and the trolley (0 = feeds it)
    TN_BUCKET = pd.Timedelta(hours=float(R.rule("dispatch.trolley_need_bucket_h", 8) or 8))
    if TN_M:
        _bst = dict(zip(blots["bkey"], blots["build_start"]))
        _direct: dict = {}
        for _k, _bk in zip(alloc["key"], alloc["bkey"]):
            _t = _bst.get(_bk)
            if _t is not None and pd.notna(_t):
                _o = _direct.get(_k)
                _direct[_k] = _t if _o is None else min(_o, _t)
        _out = {l: len(succ.get(l, ())) for l in ids}
        _q = [l for l in ids if _out[l] == 0]
        _seen = 0
        while _q:
            u = _q.pop()
            _seen += 1
            v = _direct.get(u)
            stg = None
            for c in succ.get(u, ()):
                cv = tneed.get(c)
                if cv is not None and (v is None or cv < v):
                    v = cv
                cs = tstage.get(c)
                if cs is not None and (stg is None or cs + 1 < stg):
                    stg = cs + 1
            if v is not None:
                tneed[u] = v
            tstage[u] = 0 if stg is None else stg
            for pp in pred.get(u, ()):
                _out[pp] -= 1
                if _out[pp] == 0:
                    _q.append(pp)
        print(f"  Trolley need    : v44a - machines {sorted(TN_M)} queue by the start of "
              f"the trolley each lot serves ({len(tneed):,} of {len(ids):,} lots traced)")

    co_src = defaultdict(int)          # where each charged minute came from
    # BR-74 (mixing fixed changeover) and BR-14 (no changeover in the first or
    # last 30 min of a shift) - both from the master rule book, both read from
    # the rule file so the plant can change the minutes without a code change.
    # ---- v16 levers, all from the rule file ---------------------------------
    V16_EXCL   = str(R.rule("dispatch.enforce_machine_exclusivity", False)).lower() in ("1","on","true","yes")
    V16_URGENT = str(R.rule("dispatch.urgency_first", False)).lower() in ("1","on","true","yes")
    V16_BREAK  = float(R.rule("dispatch.campaign_break_slack_h", 0) or 0)
    V16_GATE   = str(R.rule("dispatch.material_ready_beats_campaign", False)).lower() in ("1","on","true","yes")
    # ---- v18 ------------------------------------------------------------
    # A machine's ready queue is bucketed BY ITEM, and v16 tested each bucket
    # for releasability on its HEAD alone. A bucket whose most-urgent lot is
    # not yet material-ready therefore reported "nothing to run" even when
    # other lots in the same bucket were ready and the machine was idle.
    # Measured on e2e_v17 PCR machine 2001 (bias cutter, under 31% utilised):
    # a 91-hour hole between the 4-roll calender and the cutter, with a
    # zero-slack critical lot waiting in it. v18 tests the WHOLE bucket.
    V18_SCAN = bool(R.rule("dispatch.scan_whole_bucket", True))

    # ---- v25 -------------------------------------------------------------
    # (a) CHAIN DEPTH. A compound that feeds four more stages must not queue
    # behind one that feeds none. Measured on e2e_v24 TBR: the first stage of
    # the tread chain (MB712) did not start until 1 Oct - 2.5 days after the
    # bell - because 303 master-mix lots shared 4 mixers in deadline order, and
    # a deadline says nothing about how much work still stands behind a lot.
    # Depth is the count of production stages still downstream of it.
    V25_DEPTH = bool(R.rule("dispatch.depth_first", False))
    # (b) LOAD BALANCE. Once the preferred machine is busier than this, an
    # eligible machine with materially less load wins. Measured on e2e_v24 TBR:
    # bead winding 2405 at 96.9% while 2403 - same eligibility - sat at 71%,
    # and bead winding feeds apexed bead, TBR's second constraint.
    V25_BALANCE = float(R.rule("dispatch.balance_above_pct", 0) or 0)
    # ---- v32: NO MACHINE STAYS IDLE --------------------------------------
    # (a) PULL. The campaign release clock (EST) holds a lot even when its
    #     material is ready and its machine has nothing else to do. With
    #     `never_idle_pull` an idle machine releases its own material-ready lots
    #     early instead of sleeping until their campaign opens.
    # (b) STEAL. A lot is pinned to the one machine phase 4b chose. With
    #     `work_stealing` a machine that still has nothing takes the most urgent
    #     material-ready lot queued on ANOTHER machine its routing also allows,
    #     when that machine is busy - BTP v10's never-idle try_steal.
    # Both only take work whose material has ARRIVED (the v10 guard), and a
    # machine the routing never names for any lot can still idle: there is
    # nothing it is allowed to run.
    V32_PULL = bool(R.rule("dispatch.never_idle_pull", False))
    V32_STEAL = bool(R.rule("dispatch.work_stealing", False))
    V32_POLL = pd.Timedelta(hours=float(R.rule("dispatch.idle_poll_h", 1.0) or 1.0))
    mix_fixed_min = float((R.cfg().get("changeover") or {}).get("mixing_fixed_min", 0) or 0)
    co_blackout_min = float((R.cfg().get("changeover") or {}).get("blackout_min", 0) or 0)

    unpriced_by_m = defaultdict(int)

    def co_min(m, a, b, d=""):
        """STRICTLY jkt_changeover_matrix_combined. Nothing else prices a minute."""
        if not a or a == b:
            co_src["same item (0 min)"] += 1
            return 0.0
        role = roles.get(str(m))
        if role:
            v = co["pair"].get((role, a, b))
            if v is not None:
                co_src["matrix: exact (role, from, to) pair"] += 1
                return float(v)
            v = co["role_median"].get(role)
            if v is not None:
                co_src["matrix: role median (pair absent)"] += 1
                return float(v)
        # ---- BR-74: MIXING TRANSITIONS ALWAYS COST 2 MINUTES ----------------
        # The master rule book states this firmly (the old book had it as
        # BRM-10, "need confirmation", so v14 charged nothing). CTP's
        # changeover matrix prices NO mixer at all, so every mixer transition
        # fell through to `unpriced_min` = 0 and mixing setup was understated
        # on the plant's binding constraint. BR-7 is not contradicted: it says
        # not to use a fixed time WHERE A MATRIX EXISTS, and for mixing none
        # does.
        if mix_fixed_min > 0 and "MIXING" in str(d).upper():
            co_src[f"BR-74 mixing fixed {mix_fixed_min:g} min (matrix prices no mixer)"] += 1
            UR.APPLIED["BR-74"].append((str(m), a, b, mix_fixed_min))
            return float(mix_fixed_min)
        co_src["UNPRICED - matrix has no row"] += 1
        unpriced_by_m[str(m)] += 1
        return co_unpriced

    # ---- v43: CAMPAIGN MACHINES (the 4-roll calender 901) -------------------
    # On a shared calender the deadline-first pick switched item on EVERY lot
    # (e2e_v42f: 152 lots, 151 item changes, 66 PCR<->TBR switches, 101 h of
    # changeover against 133 h of running). For machines listed in
    # `campaign.machines`:
    #   * the mounted item is HELD while it has releasable work, unless another
    #     lot is more than `campaign.window_h` hours more urgent (earlier LFT);
    #   * otherwise the next item is chosen among the lots within
    #     `campaign.window_h` of the most urgent one: same LINE as the last lot
    #     first, then the cheapest changeover from the mounted item
    #     (jkt_changeover_matrix_combined), then urgency.
    # Line speed is untouched: it is the routing's proc_time, as everywhere.
    CAMP_M = {str(x).strip() for x in (R.rule("campaign.machines", []) or [])}
    CAMP_WIN = pd.Timedelta(hours=float(R.rule("campaign.window_h", 8) or 8))
    # v46f: the campaign hold-break and pick on 901 judged urgency by the CPM
    # LFT. Once every lot is overdue that LFT is stale: a TBR cord lot whose
    # trolley needs it on the 28th beat CAP 66 needed on the 23rd for 12 picks
    # in a row. With this rule, urgency is the trolley-need time (the v45b
    # queue key), falling back to LFT where a lot has none.
    CAMP_BY_NEED = str(R.rule("campaign.urgency_by_trolley_need", False)).lower() in (
        "1", "true", "on", "yes")

    def co_peek(m, a, b):
        """Changeover minutes from the matrix, for RANKING only (no counters)."""
        if not a or a == b:
            return 0.0
        role = roles.get(str(m))
        if role:
            v = co["pair"].get((role, a, b))
            if v is not None:
                return float(v)
            v = co["role_median"].get(role)
            if v is not None:
                return float(v)
        return float(co_unpriced)

    def co_charged(m, a, b, d=""):
        """The matrix price PLUS the workbook's own named cleanouts.

        BR4RC-05 is not a matrix row - it is a written instruction that CAP 66
        needs 15 minutes of cleanout on the way in and on the way out. It is
        added on top of whatever the matrix says, and counted separately so
        the matrix-only policy stays auditable.
        """
        base = co_min(m, a, b, d)
        extra = UR.cap66_cleanout_min(a, b)
        if extra:
            co_src["BR4RC-05 CAP 66 cleanout (+15 min)"] += 1
        return base + extra

    # ---- BR-14  NO CHANGEOVER IN THE FIRST OR LAST 30 MIN OF A SHIFT --------
    # "Handover and take over activities." A changeover may neither START in
    # the opening 30 minutes of a shift nor still be RUNNING in its closing 30.
    # The lot is slid to the first instant where the whole changeover fits in
    # the clean middle of a shift; it is never split across the boundary.
    _SH = {"A": 7, "B": 15, "C": 23}

    def _shift_bounds(t):
        h = pd.Timestamp(t)
        base = h.normalize()
        for st in (7, 15, 23):
            a = base + pd.Timedelta(hours=st)
            z = a + pd.Timedelta(hours=8)
            if a <= h < z:
                return a, z
        # before 07:00 -> the C shift that began at 23:00 yesterday
        a = base - pd.Timedelta(hours=1)
        return a, a + pd.Timedelta(hours=8)

    def co_window(t, cm):
        """The earliest legal START for a changeover of `cm` minutes."""
        if cm <= 0 or co_blackout_min <= 0:
            return t
        bl = pd.Timedelta(minutes=co_blackout_min)
        cd = pd.Timedelta(minutes=cm)
        for _ in range(4):
            a, z = _shift_bounds(t)
            if t < a + bl:
                t = a + bl
            if t + cd > z - bl:
                t = z + bl              # next shift, past ITS opening blackout
                rule_fired["BR-14"] += 1
                continue
            return t
        return t

    def co_window_back(t, cm):
        """The LATEST legal start for a changeover of `cm` minutes, at or before t.

        The forward pass uses co_window(); pass 2 slides lots LATER toward
        consumption and needs the mirror image, or it puts back the very
        changeovers the forward pass moved out of a handover window.
        """
        if cm <= 0 or co_blackout_min <= 0:
            return t
        bl = pd.Timedelta(minutes=co_blackout_min)
        cd = pd.Timedelta(minutes=cm)
        for _ in range(4):
            a, z = _shift_bounds(t)
            hi, lo = z - bl - cd, a + bl
            if hi < lo:                     # changeover cannot fit this shift
                t = a - bl
                continue
            if t > hi:
                t = hi
            if t < lo:
                t = a - pd.Timedelta(minutes=1)   # step back a shift and retry
                continue
            return t
        return t

    # ---- BR-10  PM / MTC BLOCKS ---------------------------------------------
    pm = io.pm_blocks()
    pm_pushed_h = [0.0]
    pm_hits = [0]

    def pm_clear(m, t, d):
        s2, pushed = io.pm_push(pm, m, t, d)
        if pushed > 0:
            pm_pushed_h[0] += pushed
            pm_hits[0] += 1
            rule_fired["BR-10"] += 1
        return s2

    def past_holiday(t, d=None):
        for a, b in holidays:
            if a <= t < b or (d is not None and t < a < t + d):
                return b
        return t

    start: dict = {}
    finish: dict = {}
    co_of: dict = {}
    mat_ready: dict = {}
    ready_by_m: dict = defaultdict(lambda: defaultdict(list))
    n_ready = defaultdict(int)
    m_free: dict = {}
    _cwf = R.rule("dispatch.choose_when_free", None)
    CWF_ALL = isinstance(_cwf, str) and _cwf.strip().lower() == "all"
    CWF_M = ({str(x).strip() for x in _cwf} if isinstance(_cwf, (list, tuple)) else set())
    if CWF_ALL or CWF_M:
        print(f"  Choose when free: v44c - "
              f"{'every machine' if CWF_ALL else sorted(CWF_M)} picks its next lot only "
              f"when it is free (dispatch.choose_when_free)")
    m_last: dict = {}
    m_seq = defaultdict(list)

    # DEPTH: how many production stages still stand downstream of each lot.
    # Computed once from the precedence graph, memoised, iterative so a deep
    # chain cannot blow the stack.
    depth: dict = {}
    if V25_DEPTH:
        for _k in sorted(ids, key=lambda z: -len(succ.get(z, ()))):
            stack, order = [_k], []
            while stack:
                n = stack.pop()
                if n in depth:
                    continue
                kids = [s for s in succ.get(n, ()) if s not in depth]
                if kids:
                    stack.append(n)
                    stack.extend(kids)
                else:
                    depth[n] = (1 + max((depth[s] for s in succ.get(n, ())),
                                        default=-1))
        _d = pd.Series(list(depth.values()))
        print(f"  Chain depth     : max {int(_d.max()) if len(_d) else 0} stage(s); "
              f"{int((_d >= 3).sum()):,} lot(s) with 3+ stages still downstream "
              f"-> they are dispatched first")

    def push_ready(l):
        mr = open_at
        for p in pred.get(l, ()):
            f = finish.get(p)
            if f is not None:
                c = f + min_age.get(_age_key(p), pd.Timedelta(0)) + transfer
                if c > mr:
                    mr = c
        mat_ready[l] = mr
        if l in FIXED:
            return                    # planned by the mixing planner - not dispatched
        m = mach_of.get(l)
        if m is None:
            for s in succ.get(l, ()):
                indeg[s] -= 1
                if indeg[s] == 0:
                    push_ready(s)
            return
        # v16 BR-16 read correctly: urgency is the KEY, changeover/campaign the
        # tie-break. v15 ordered (camp, lft) so a deadline lost to a campaign
        # clock even on a machine with idle hours to spare.
        # Depth leads the key when it is on: a lot with more stages behind it
        # is worth more hours than one with fewer, whatever their deadlines say.
        # Deadline remains the tie-break, so nothing else about the order moves.
        if TN_M and str(m) in TN_M:
            _tn = tneed.get(l)
            _lf = lft.get(l)
            _lf = _lf if (_lf is not None and pd.notna(_lf)) else pd.Timestamp.max
            _tn = _tn if (_tn is not None and pd.notna(_tn)) else _lf
            # v44a: trolley need first, in buckets of TN_BUCKET; inside a bucket
            # the stage CLOSEST to the trolley goes first (an apexing lot whose
            # inputs are ready finishes a kit; more winding only stocks beads
            # for it). Winding still precedes the apexing that needs it, because
            # that apexing lot is not ready until its beads exist.
            _b = int((_tn - open_at) / TN_BUCKET) if _tn != pd.Timestamp.max else 10**6
            k1 = (_b, tstage.get(l, 0), _tn, _lf, camp[l], l)
        elif V25_DEPTH:
            k1 = (-depth.get(l, 0), lft[l], camp[l], l)
        else:
            k1 = (lft[l], camp[l], l) if V16_URGENT else (camp[l], lft[l], l)
        heapq.heappush(ready_by_m[m][item[l]], k1)
        n_ready[m] += 1

    # ==================================================================
    # v36: THE MIXING PLANNER (MIXING_RULES_README.md). Every mixing lot is
    # planned FIRST by mix_planner.MixPlanner with the mixing rules built in,
    # and FIXED: it is never queued for the dispatcher and the repairs below
    # never move it. Everything else is placed around these lots.
    # ==================================================================
    rule_fired: dict = defaultdict(int)
    # ---- DEBUG: dispatch log for chosen machines (debug.log_machines) -------
    DBG_M = {str(x).strip() for x in (R.rule("debug.log_machines", []) or [])}
    DBG_ROWS: list = []
    DBG_PICK = [0]

    def _dbg_rows(t, m, prev, buckets, releasable, stages, hold, brk, pick, ent_pick):
        """One row per QUEUED lot on machine m at this decision."""
        DBG_PICK[0] += 1
        pid = DBG_PICK[0]
        rel_ids = {e[-1] for v in releasable.values() for e in v}
        for k_, v_ in (buckets or {}).items():
            for e in v_:
                l_ = e[-1]
                g_ = gate(l_)
                ready = l_ in rel_ids
                excl = ""
                if ready:
                    for nm, ss in stages:
                        if k_ not in ss:
                            excl = nm
                            break
                DBG_ROWS.append({
                    "decision": pid, "time": t, "machine": str(m), "prev_item": prev,
                    "candidate_item": k_, "candidate_lot": str(l_).split("::")[-1],
                    "department": dept.get(l_, ""),
                    "key0": e[0], "key1": e[1] if len(e) > 2 else "",
                    "key2": e[2] if len(e) > 3 else "",
                    "trolley_need": tneed.get(l_), "stage_to_trolley": tstage.get(l_),
                    "LFT": lft.get(l_), "ready_at": g_, "ready_now": ready,
                    "removed_by_rule": excl, "campaign_hold": hold, "hold_broken": brk,
                    "picked": bool(ent_pick is not None and l_ == ent_pick[-1]),
                    "picked_item": pick})

    FIXED: set = set()
    MIX_PREP: list = []
    MIX_PLANNER = None
    _mp = R.rule("mix_planner") or {}
    if _mp.get("enabled"):
        import os as _os
        import mix_planner as MP
        _itypes = dict(zip(lots["item_code"], lots.get("item_type", "")))
        mix_keys = [k for k in lots["key"]
                    if "MIXING" in str(dept.get(k, "")).upper()]
        _mixset = set(mix_keys)
        _elig = {}
        for _k, _ms in zip(lots["key"], lots.get("machines", pd.Series([""] * len(lots)))):
            _elig[_k] = sorted({x.strip() for x in str(_ms).split(",")
                                if x.strip() and x.strip().lower() != "nan"})
        # bucket span from phase 3, so a lot's LAST use is known in round 1
        span_h = {}
        for ln in lines:
            try:
                _p3 = pd.read_csv(OUTS[ln] / "phase3_lots.csv",
                                  usecols=["lot_id", "bucket_span_h"])
                for _l, _h in zip(_p3["lot_id"], _p3["bucket_span_h"]):
                    span_h[ln + "::" + str(_l)] = float(_h or 0.0)
            except Exception:
                pass
        # closed loop (README s.6): rounds 2+ re-plan on the REAL consumer
        # starts of the previous round
        rnd = int(_os.environ.get("CTP_MIX_ROUND", "1") or 1)
        prev_start = {}
        if rnd > 1:
            for ln in lines:
                try:
                    _ps = pd.read_csv(OUTS[ln] / "phase5_schedule.csv",
                                      usecols=["lot_id", "start"], parse_dates=["start"])
                    for _l, _s in zip(_ps["lot_id"], _ps["start"]):
                        if pd.notna(_s):
                            prev_start[ln + "::" + str(_l)] = _s
                except Exception:
                    pass
        ML = {}
        lot_qty_of = dict(zip(lots["key"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)))
        for k in mix_keys:
            it = item[k]
            el = _elig.get(k) or [str(mach_of.get(k))]
            cons = [prev_start[c] for c in succ.get(k, ()) if c in prev_start]
            ma = max_age.get(_age_key(k))
            mn = min_age.get(_age_key(k), pd.Timedelta(0))
            if cons:
                need = min(cons) - mn - transfer
                # LAST USE IS CAPPED at the span the lot was SIZED for. One lot
                # feeds many consumer lots through the DAG; taking the latest of
                # them let the shelf floor drag the lot later every round, which
                # delayed its consumers further - a loop that drifted B1144 from
                # 21 Oct to 28 Oct over three rounds and stalled TBR's belt edge
                # filler. The re-plan may tighten timings, never chase lateness.
                last_use = min(max(cons),
                               min(cons) + pd.Timedelta(hours=span_h.get(k, 0.0)))
            else:
                need = lft[k] if pd.notna(lft[k]) else open_at
                last_use = need + pd.Timedelta(hours=span_h.get(k, 0.0)) + mn + transfer
            _mm = None
            for _ln in [line_of[k]] + [x for x in lines if x != line_of[k]]:
                _mm = (_meta.get(_ln) or {}).get(it)
                if _mm:
                    break
            _dbm = {}
            if (_mm or {}).get("spec_mix"):
                for _m in el:
                    _x = io.machine_duration_min(lot_qty_of.get(k, 0.0), _mm, _m)
                    if _x is not None:
                        _dbm[str(_m)] = pd.Timedelta(minutes=_x)
            ML[k] = {"item": it, "line": line_of[k], "dur": dur[k], "elig": el,
                     "dur_by_m": _dbm,
                     "need": need, "last_use": last_use, "max_age": ma,
                     "min_age": mn, "ready": open_at,
                     "preds": [q for q in pred.get(k, ()) if q in _mixset]}
        # compounds that feed the 4-roll calender (901), and their masters
        _fourrc = set()
        for k in mix_keys:
            if any(str(mach_of.get(c)) == str(_mp.get("fourrc_machine", "901"))
                   for c in succ.get(k, ())):
                _fourrc.add(item[k])
                for q in pred.get(k, ()):
                    if q in ML:
                        _fourrc.add(item[q])

        def _legal_start(m, earliest, cm, d):
            cd = pd.Timedelta(minutes=cm)
            s = earliest + cd
            for _ in range(6):
                e2 = co_window(earliest, cm) if cm > 0 else earliest    # BR-14
                s = e2 + cd
                s2 = past_holiday(s, d)
                s2 = pm_clear(m, s2, d)                                  # BR-10
                if s2 <= s:
                    break
                earliest = s2 - cd
                s = s2
            return s

        _prep_n = [0]

        def _make_prep(it, m, ln, batches):
            meta, bs = {}, None
            for _ln in [ln] + [x for x in lines if x != ln]:
                meta = (_meta.get(_ln) or {}).get(it) or {}
                bs = meta.get("batch_size")
                if bs:
                    break
            if not bs:
                return None
            q = float(bs) * int(batches)
            d = pd.Timedelta(minutes=io.op_duration_min(q, meta))
            _prep_n[0] += 1
            return {"key": f"{ln}::{it}_PREP{_prep_n[0]:04d}", "line": ln,
                    "lot_id": f"{it}_PREP{_prep_n[0]:04d}", "item": it,
                    "itype": _itypes.get(it, "") or "FINAL COMPOUND",
                    "qty": q, "batches": int(batches), "dur": d}

        def _itype(it):
            return _itypes.get(it, "") or ""

        MIX_PLANNER = MP.MixPlanner(_mp, ML, {
            "legal_start": _legal_start, "plant_day": io.plant_day,
            "open_at": open_at, "transfer": transfer, "make_prep": _make_prep,
            "itype_of": _itype, "fourrc_compounds": sorted(_fourrc)})
        res = MIX_PLANNER.plan()
        for k, (m, s0, f0, cm) in res.items():
            FIXED.add(k)
            mach_of[k] = m
            start[k], finish[k], co_of[k] = s0, f0, cm
            m_seq[m].append(k)
            dur[k] = f0 - s0
        for pr in MIX_PLANNER.prep:
            MIX_PREP.append(pr)
            k = pr["key"]
            FIXED.add(k)
            start[k], finish[k], co_of[k] = pr["start"], pr["finish"], pr["co_min"]
            dur[k] = pr["finish"] - pr["start"]
            mach_of[k], item[k], dept[k] = pr["machine"], pr["item"], "FINAL MIXING"
            line_of[k] = pr["line"]
            lft[k] = pd.NaT
            m_seq[pr["machine"]].append(k)
        for m, seq in m_seq.items():
            seq.sort(key=lambda x: start[x])
            if seq:
                m_last[m] = item[seq[-1]]
                m_free[m] = finish[seq[-1]]
        st = MIX_PLANNER.stats
        print(f"  MIX PLANNER     : round {rnd}; {len(res):,} of {len(mix_keys):,} mixing "
              f"lot(s) planned and FIXED in {st['runs']:,} run(s); butyl blocks "
              f"{st['blocks']}, prep lots {st['prep_lots']}, follower picks "
              f"{st['followers']}, butyl gap waits {st['butyl_gap_waits']}, mixer "
              f"waits {st['mixer_waits']:,} {dict(MIX_PLANNER.waits)}"
              + (f"; {st['unplanned']} left to the dispatcher" if st['unplanned'] else ""))
        print(f"                    4RC-feeding compounds: {', '.join(sorted(_fourrc)) or '-'}")

    for l in lots["key"]:
        if indeg[l] == 0:
            push_ready(l)
    # the consumers of a FIXED mixing lot are released by its planned finish
    for l in sorted((x for x in FIXED if x in ids), key=lambda x: finish[x]):
        for s2 in succ.get(l, ()):
            indeg[s2] -= 1
            if indeg[s2] == 0:
                push_ready(s2)

    def gate(l):
        e, mr = est.get(l), mat_ready.get(l)
        if e is None or pd.isna(e):
            return mr
        # v16: the campaign release clock exists to stop the dispatcher
        # interleaving a block. It must not hold a lot that is ALREADY going to
        # be late - measured on v15, 53 late lots sat in front of an IDLE
        # machine waiting for their campaign to open.
        if V16_GATE and mr is not None:
            f = lft.get(l)
            if f is not None and not pd.isna(f):
                if mr + dur.get(l, pd.Timedelta(0)) > f:      # negative slack
                    return mr
        return e if mr is None else max(e, mr)

    # state the sequencing rules need
    itype_of = dict(zip(lots["item_code"], lots.get("item_type", "")))
    mounted: set = set()          # (plant-day, shift, machine) -> item mounted
    mixed_shift: set = set()      # BR-73: (plant-day, shift) -> compound mixed
    # BR-73 is stated on the mixing DEPARTMENT, so the gate has to know which
    # machines are mixers. Taken from the lots themselves, not from a list.
    dept_of_m: dict = {}
    for _k, _m in mach_of.items():
        dept_of_m.setdefault(str(_m), str(dept.get(_k, "")))
    m_lastlot: dict = {}          # machine -> the lot it last ran
    # rule_fired is created before the mixing planner (it counts BR-10 there)

    # LOAD so far, per machine, for the balance rule. Kept as a running total
    # rather than recomputed, so the pick stays cheap.
    load_h: dict = defaultdict(float)

    # every machine the routing allows for each lot (phase 4 `machines`)
    elig: dict = {}
    for _k, _ms in zip(lots["key"], lots.get("machines", pd.Series([""] * len(lots)))):
        elig[_k] = {x.strip() for x in str(_ms).split(",")
                    if x.strip() and x.strip().lower() != "nan"}
    _all_m = set(mach_of.values())
    if V32_STEAL:
        _all_m |= {x for v in elig.values() for x in v}
    n_total = len(lots) - sum(1 for k in FIXED if k in ids)
    stolen: list = []
    # ONE pending idle-poll per machine. Without this every pop of an idle
    # machine queued a fresh poll on top of the ones already waiting, so the
    # clock heap grew without bound (measured: 2.8 GB, phase 5 never ended).
    poll_at: dict = {}

    # and never past this: a lot whose inputs never finish must not keep the
    # idle machines polling forever
    poll_until = lots["LFT"].max() + pd.Timedelta(days=10)

    def queue_poll(t, m):
        if t > poll_until:
            return
        if poll_at.get(m) is not None and poll_at[m] > t:
            return                                    # one already pending
        poll_at[m] = t + V32_POLL
        heapq.heappush(clock, (poll_at[m], m))
    pulled = [0]

    def idle_fill(t, m):
        """v32: work for an idle machine - its own early, else someone else's."""
        def ready(l):
            mr = mat_ready.get(l)
            return mr is not None and mr <= t
        if V32_PULL:
            got = {}
            for k, v in ready_by_m.get(m, {}).items():
                rdy = [ent for ent in v if ready(ent[-1])]
                if rdy:
                    heapq.heapify(rdy)
                    got[k] = rdy
            if got:
                for v in got.values():
                    for ent in v:
                        est[ent[-1]] = None          # gate() -> material only
                pulled[0] += 1
                rule_fired["v32-never-idle-pull"] += 1
                return got
        if V32_STEAL:
            best = None
            for m2, bk in ready_by_m.items():
                if m2 == m or n_ready[m2] == 0:
                    continue
                if m_free.get(m2, open_at) <= t:
                    continue                          # owner is free to run it
                for k, v in bk.items():
                    for ent in v:
                        l2 = ent[-1]
                        if m in elig.get(l2, ()) and ready(l2):
                            if best is None or ent < best[0]:
                                best = (ent, m2, k)
            if best:
                ent, m2, k = best
                l2 = ent[-1]
                src = ready_by_m[m2][k]
                src.remove(ent)
                heapq.heapify(src)
                if not src:
                    ready_by_m[m2].pop(k, None)
                n_ready[m2] -= 1
                mach_of[l2] = m
                est[l2] = None
                heapq.heappush(ready_by_m[m][k], ent)
                n_ready[m] += 1
                stolen.append((l2, m2, m, t))
                rule_fired["v32-work-steal"] += 1
                return {k: [ent]}
        return {}

    clock = [(open_at, m) for m in _all_m]
    heapq.heapify(clock)
    n_placed = n_co = 0
    co_total = 0.0
    guard_double = 0

    while clock:
        t, m = heapq.heappop(clock)
        # ---- v44c: CHOOSE THE NEXT LOT ONLY WHEN THE MACHINE IS FREE ---------
        # A machine is woken whenever new work becomes ready - also while it is
        # still busy. It then booked its next lot at once, for a start hours
        # later, choosing only among the lots ready AT THAT MOMENT (e2e_v44b,
        # 2403-2405: 110 of 118 picks booked ahead, median 37 h); a lot that
        # became ready later - the day-1 apexing - could never get in front.
        # With `dispatch.choose_when_free` ("all", or a list of machines) a busy
        # machine is re-queued at the time it becomes free and chooses then,
        # among everything ready at that moment.
        if CWF_ALL or (CWF_M and str(m) in CWF_M):
            _mf = m_free.get(m)
            if _mf is not None and _mf > t + pd.Timedelta(seconds=1):
                heapq.heappush(clock, (_mf, m))
                continue
        buckets = ready_by_m.get(m)
        if not buckets or n_ready[m] == 0:
            if V32_STEAL and n_placed < n_total:
                got = idle_fill(t, m)
                if got:
                    heapq.heappush(clock, (t, m))     # re-enter with the lot queued
                else:
                    queue_poll(t, m)
            continue
        # THE FILTER AND THE COMMIT MUST USE THE SAME CLOCK (v10).
        releasable, next_open = {}, None
        for k, v in buckets.items():
            if not v:
                continue
            if V18_SCAN:
                # EVERY lot in the bucket, not just its head. The filtered heap
                # keeps the same (lft, camp, lot) order, so the pick below still
                # takes the most urgent lot - now the most urgent READY one.
                rdy = []
                for ent in v:
                    g = gate(ent[-1])
                    if g is None or g <= t:
                        rdy.append(ent)
                    elif next_open is None or g < next_open:
                        next_open = g
                if rdy:
                    heapq.heapify(rdy)
                    releasable[k] = rdy
                continue
            g = gate(v[0][-1])
            if g is None or g <= t:
                releasable[k] = v
            elif next_open is None or g < next_open:
                next_open = g
        if not releasable and (V32_PULL or V32_STEAL):
            releasable = idle_fill(t, m)
            if releasable and V18_SCAN:
                buckets = ready_by_m.get(m)
        if not releasable:
            if next_open is not None and next_open > t:
                heapq.heappush(clock, (next_open, m))
            if V32_STEAL and n_placed < n_total and (
                    next_open is None or next_open > t + V32_POLL):
                # wake again to look for work to take, not only at next_open
                queue_poll(t, m)
            continue
        prev = m_last.get(m, "")
        _dbg_on = bool(DBG_M) and str(m) in DBG_M
        _st0 = set(releasable)

        # ---- SEQUENCING RULES ------------------------------------------
        # Every one of these NARROWS the choice; none may empty it. If a rule
        # would leave the machine with nothing to run it is skipped for this
        # pick and counted, because starving a machine to honour an ordering
        # preference costs tyres and the rules are preferences about ORDER,
        # not permissions to idle.
        allowed = releasable

        # BRM-03: after a butyl compound, only butyl or butyl-friendly.
        if UR.butyl_class(prev) == "butyl":
            ok = {k: v for k, v in allowed.items()
                  if UR.butyl_class(k) in ("butyl", "friendly")}
            if ok:
                allowed = ok
                rule_fired["BRM-03"] += 1

        _st1 = set(allowed)
        # BRC-02: the same item code is mounted once per shift per machine.
        if UR.once_per_shift():
            sk = (io.plant_day(t), io.shift_of(t), m)
            ok = {k: v for k, v in allowed.items() if (sk, k) not in mounted}
            if ok and len(ok) < len(allowed):
                allowed = ok
                rule_fired["BRC-02"] += 1

        _st2 = set(allowed)
        # ---- BR-73: THE SAME COMPOUND IS NOT SCHEDULED TWICE IN ONE SHIFT ----
        # BRC-02 above applies the same idea per (machine, shift). BR-73 is
        # stated on the COMPOUND, so it holds across every mixer in the shift:
        # a compound already mixed anywhere this shift is not mixed again.
        if UR.mixing_once_per_shift() and "MIXING" in str(dept_of_m.get(m, "")).upper():
            shk = (io.plant_day(t), io.shift_of(t))
            ok = {k: v for k, v in allowed.items() if (shk, k) not in mixed_shift}
            if ok and len(ok) < len(allowed):
                allowed = ok
                rule_fired["BR-73"] += 1

        _st3 = set(allowed)
        # ---- v16: URGENCY MAY BREAK THE CAMPAIGN HOLD ------------------------
        # v15 held the mounted item while it had ANY releasable work, which
        # saved a changeover and cost a deadline. With 900 idle hours on the
        # machines that deliver late, that is the wrong trade. The hold now
        # yields when another releasable lot cannot afford to wait.
        _break = False
        _camp = str(m) in CAMP_M
        def _urg(l_):
            if CAMP_BY_NEED:
                v_ = tneed.get(l_)
                if v_ is not None and pd.notna(v_):
                    return v_
            return lft.get(l_)
        if _camp and prev in allowed and len(allowed) > 1:
            _lp = _urg(allowed[prev][0][-1])
            for _k, _h in allowed.items():
                if _k == prev:
                    continue
                _f2 = _urg(_h[0][-1])
                if (_f2 is not None and pd.notna(_f2) and _lp is not None
                        and pd.notna(_lp) and _f2 < _lp - CAMP_WIN):
                    _break = True
                    rule_fired["v43-campaign-break"] += 1
                    break
        elif V16_BREAK > 0 and prev in allowed and len(allowed) > 1:
            _hold_end = t + dur.get(allowed[prev][0][-1], pd.Timedelta(0))
            for _k, _h in allowed.items():
                if _k == prev:
                    continue
                _l2 = _h[0][-1]
                _f2 = lft.get(_l2)
                if _f2 is None or pd.isna(_f2):
                    continue
                _slack = (_f2 - (_hold_end + dur.get(_l2, pd.Timedelta(0)))
                          ).total_seconds() / 3600.0
                if _slack < V16_BREAK:
                    _break = True
                    rule_fired["v16-campaign-break"] += 1
                    break

        # campaign hold: stay on the mounted item while it has releasable work
        if prev in allowed and not _break:
            pick = prev
        else:
            # BRC-01: belt 1 and belt 2 of one SKU run together, so the twin of
            # what just ran is preferred over everything else.
            twin = UR.belt_pair_key(prev) if UR.belts_together() else None

            # BR-71: related compounds run continuously - a partner of what
            # just ran is preferred over an unrelated compound.
            relp = UR.related_group(prev)

            def _rank(k):
                rl = 0 if (relp and UR.related_group(k) == relp and k != prev) else 1
                tw = 0 if (twin and UR.belt_pair_key(k) == twin and k != prev) else 1
                # BR4RC-14: prefer NOT to follow wire with wire ("avoid it");
                # if it happens anyway the separation is charged below.
                wr = 1 if (UR.is_wire(prev, itype_of.get(prev, ""))
                           and UR.is_wire(k, itype_of.get(k, ""))) else 0
                return (rl, tw, wr, allowed[k][0][0], allowed[k][0][1], k)

            pick = min(allowed, key=_rank)
            if _camp and prev and len(allowed) > 1:
                _lf = {k: _urg(v[0][-1]) for k, v in allowed.items()}
                _valid = [x for x in _lf.values() if x is not None and pd.notna(x)]
                if _valid:
                    _best = min(_valid)
                    _cand = [k for k, x in _lf.items()
                             if x is not None and pd.notna(x) and x <= _best + CAMP_WIN]
                    _pl = line_of.get(m_lastlot.get(m), "")
                    _same = [k for k in _cand if line_of.get(allowed[k][0][-1], "") == _pl]
                    _pool = _same or _cand
                    _p2 = min(_pool, key=lambda k: (co_peek(m, prev, k), _lf[k], k))
                    if _p2 != pick:
                        rule_fired["v43-campaign-pick"] += 1
                    pick = _p2
            if twin and UR.belt_pair_key(pick) == twin:
                rule_fired["BRC-01"] += 1
            if relp and UR.related_group(pick) == relp and pick != prev:
                rule_fired["BR-71"] += 1
        if _dbg_on:
            _dbg_rows(t, m, prev, buckets, releasable,
                      [("BRM-03 butyl", _st1), ("BRC-02 once per shift", _st2),
                       ("BR-73 compound once per shift", _st3)],
                      bool(prev in allowed and not _break), bool(_break), pick,
                      allowed[pick][0])
        ent = heapq.heappop(allowed[pick])
        l = ent[-1]
        n_ready[m] -= 1
        if V18_SCAN:
            # `allowed[pick]` is a filtered COPY under v18, so the lot has to be
            # taken out of the real bucket too, or it would be offered again.
            src = buckets.get(pick)
            if src is not None:
                try:
                    src.remove(ent)
                    heapq.heapify(src)
                except ValueError:
                    pass
                if not src:
                    buckets.pop(pick, None)
        elif not allowed[pick]:
            buckets.pop(pick, None)
        if l in start:                       # v10's double-placement guard
            guard_double += 1
            heapq.heappush(clock, (t, m))
            continue

        cm = co_charged(m, prev, item[l], dept.get(l, ""))
        earliest = max(t, gate(l) or t)
        # BR4RC-14: a wire-to-wire transition keeps a mandatory separation.
        if (prev and prev != item[l]
                and UR.is_wire(prev, itype_of.get(prev, ""))
                and UR.is_wire(item[l], itype_of.get(item[l], ""))):
            g = UR.wire_gap_h()
            if g > 0:
                earliest = max(earliest, finish.get(m_lastlot.get(m), earliest)
                               + pd.Timedelta(hours=g))
                rule_fired["BR4RC-14"] += 1
        # BR-14 / BR-10 / holidays interact: a holiday or a PM block can push a
        # lot AFTER its changeover window was made legal, which would land the
        # changeover back inside a blackout. So the three are settled together
        # and re-checked until the answer stops moving, rather than applied once
        # in an order that lets the last one undo the first.
        # ---- v16: A MACHINE RUNS ONE LOT AT A TIME ---------------------------
        # `m_free` was declared and never used. Serialisation relied on the
        # clock heap alone, and the v14 wake-up fix pushes DUPLICATE entries, so
        # a machine could be popped while still busy - 803 overlapping lots,
        # concurrency to 8.76x on K430-2 M. The double-placement guard only ever
        # stopped the same LOT twice, never two lots on one machine.
        if V16_EXCL:
            mf = m_free.get(m)
            if mf is not None and earliest < mf:
                earliest = mf
        cd = pd.Timedelta(minutes=cm)
        for _ in range(6):
            e2 = co_window(earliest, cm)            # BR-14
            s = e2 + cd
            s2 = past_holiday(s, dur[l])
            s2 = pm_clear(m, s2, dur[l])            # BR-10
            if s2 <= s:
                s = s2
                break
            earliest = s2 - cd                      # re-aim and test again
            s = s2
        f = s + dur[l]
        start[l], finish[l], co_of[l] = s, f, cm
        if V16_EXCL:
            m_free[m] = f
        load_h[m] += (dur[l].total_seconds() / 3600.0) + (cm / 60.0)
        m_last[m] = item[l]
        m_lastlot[m] = l
        mounted.add(((io.plant_day(s), io.shift_of(s), m), item[l]))
        if "MIXING" in str(dept.get(l, "")).upper():
            mixed_shift.add(((io.plant_day(s), io.shift_of(s)), item[l]))
        m_seq[m].append(l)
        n_placed += 1
        if cm > 0:
            n_co += 1
            co_total += cm
        heapq.heappush(clock, (f, m))
        for s2 in succ.get(l, ()):
            indeg[s2] -= 1
            if indeg[s2] == 0:
                push_ready(s2)
                mm = mach_of.get(s2)
                if mm is not None:
                    # WAKE THE MACHINE, EVEN IF IT IS ALREADY QUEUED LATER.
                    #
                    # This used to be guarded by `not any(x[1] == mm for x in
                    # clock)`: a machine already holding a clock entry was not
                    # given an earlier one. But an entry at a LATE time is
                    # exactly the case that needs the earlier wake-up - the
                    # machine is asleep until then and cannot see work that
                    # became releasable in between.
                    #
                    # Measured on e2e_v13 TBR, machine 901 (the single 4-roll
                    # calender): idle 26 Sep 08:39 -> 27 Sep 16:57, 32 hours,
                    # while CWCH30256 - zero slack, on the critical path, its
                    # compound mixed on the 26th - waited. It ran 13th in the
                    # queue on 28 Sep 23:10, 2.4 days past the slot its own
                    # campaign plan had given it (26 Sep 12:47). The belt
                    # cutter then missed steel chipper, and no TBR trolley
                    # could start until 29 Sep - which is why TBR cured
                    # nothing for the first three days.
                    #
                    # Duplicate entries are harmless: a pop with no releasable
                    # work either re-queues the machine at `next_open` or drops
                    # it, and the double-placement guard covers the rest.
                    heapq.heappush(clock, (max(f, open_at), mm))

    # v32: a stolen lot now belongs to the machine that ran it - every pass
    # after this one (right-shift, freshness, overlap check, output) reads
    # `lots.machine`, so it must say where the lot actually is.
    if V32_PULL or V32_STEAL:
        lots["machine"] = lots["key"].map(mach_of).astype(str)
        print(f"  v32 never-idle  : {pulled[0]:,} idle wake-up(s) released own "
              f"work early; {len(stolen):,} lot(s) taken from a busy machine")
        if stolen:
            pd.DataFrame(stolen, columns=["lot", "from_machine", "to_machine", "at"]
                         ).to_csv(io.OUT_ROOT / args.run / "WORK_STOLEN.csv",
                                  index=False, encoding="utf-8-sig")
    if pm_hits[0]:
        print(f"  BR-10 PM blocks : {pm_hits[0]:,} lot(s) pushed clear of a PM "
              f"window, {pm_pushed_h[0]:,.1f} h of push")
    else:
        print(f"  BR-10 PM blocks : 0 upstream lots hit a PM window "
              f"({sum(len(v) for v in pm.values())} block(s) known, none on an "
              f"upstream machine - the table covers only Curing and Building)")
    _nfix = sum(1 for k in FIXED if k in ids)
    print(f"  Placed          : {n_placed + _nfix:,} of {len(lots):,}"
          + (f"  ({_nfix:,} by the mixing planner)" if _nfix else "")
          + (f"   [{len(lots)-n_placed-_nfix:,} UNPLACED — no releasable path]"
             if n_placed + _nfix < len(lots) else ""))
    _tot = sum(v for k, v in co_src.items() if not k.startswith("same item"))
    print(f"  Changeover source (matrix-only):", flush=True)
    for k, v in sorted(co_src.items(), key=lambda kv: -kv[1]):
        if k.startswith("same item"):
            continue
        print(f"    {k:32s} {v:>7,d}  {100*v/max(_tot,1):5.1f}%")
    if unpriced_by_m:
        print(f"    [UNPRICED] {sum(unpriced_by_m.values()):,} transition(s) on "
              f"{len(unpriced_by_m)} machine(s) the matrix does not cover — "
              f"charged {co_unpriced:g} min, so SETUP IS UNDERSTATED there:")
        for m, n in sorted(unpriced_by_m.items(), key=lambda kv: -kv[1]):
            print(f"      {str(m):22s} {n:>6,d} transition(s)")
        pd.DataFrame([{"machine": m, "unpriced_transitions": n}
                      for m, n in sorted(unpriced_by_m.items(),
                                         key=lambda kv: -kv[1])]).to_csv(
            io.OUT_ROOT / args.run / "CHANGEOVER_UNPRICED.csv", index=False)
    pd.DataFrame([{"source": k, "transitions": v} for k, v in
                  sorted(co_src.items(), key=lambda kv: -kv[1])]).to_csv(
        io.OUT_ROOT / args.run / "CHANGEOVER_SOURCE.csv", index=False)
    print(f"  Changeovers     : {n_co:,}   setup {co_total/60:,.0f} h"
          + (f"   [double-placement guard fired {guard_double}x]" if guard_double else ""))

    # ---------------- pass 2: backward right-shift ------------------------
    n_shift = 0
    n_shift_blocked = 0
    if not args.no_right_shift:
        pos = {}
        for m, seq in m_seq.items():
            seq.sort(key=lambda x: start[x])
            for i, x in enumerate(seq):
                pos[x] = (m, i)
        for l in sorted(start, key=lambda x: start[x], reverse=True):
            if l in FIXED:
                continue                       # mixing lots are fixed (README s.1)
            cap = lft.get(l)
            for s2 in succ.get(l, ()):
                if s2 in start:
                    c = start[s2] - min_age.get(_age_key(l), pd.Timedelta(0)) - transfer
                    cap = c if cap is None or c < cap else cap
            m, i = pos[l]
            seq = m_seq[m]
            if i + 1 < len(seq):
                nxt = seq[i + 1]
                c = start[nxt] - pd.Timedelta(minutes=co_of.get(nxt, 0.0))
                cap = c if cap is None or c < cap else cap
            if cap is None or pd.isna(cap):
                continue
            # slide LATER, never earlier, and never past the cap
            if cap > finish[l] + pd.Timedelta(seconds=1):
                new_s = cap - dur[l]
                # BR-14 again: sliding a lot later drags its changeover with
                # it, and a slide that lands the changeover in a handover
                # window would undo the forward pass. Aim at the latest LEGAL
                # changeover start at or before the target; if that is earlier
                # than where the lot already sits, leave it alone.
                _cd = pd.Timedelta(minutes=co_of.get(l, 0.0))
                if _cd > pd.Timedelta(0):
                    legal = co_window_back(new_s - _cd, co_of.get(l, 0.0))
                    new_s = legal + _cd
                if new_s >= start[l]:
                    start[l], finish[l] = new_s, new_s + dur[l]
                    n_shift += 1
                else:
                    n_shift_blocked += 1
    # ---------------- FRESHNESS REPAIR (max aging is a CONSTRAINT) ---------
    # A producer must not finish so early that its material is scrap by the time
    # its consumer starts. The forward pass makes everything as early as it can,
    # which is exactly what expires material; the right-shift above nudges lots
    # toward consumption but is a PREFERENCE capped at LFT, so it leaves
    # violations behind and does nothing at all when it is switched off.
    #
    # This pass is the constraint. For every edge p -> c it requires
    #     finish[p] + max_age[item[p]]  >=  start[c]
    # and satisfies it the only way that cannot break anything already proven:
    # by moving the PRODUCER LATER. Moving it later can never violate min aging
    # for its own predecessors, and the ceiling keeps it legal for everything
    # downstream:
    #     <= start[consumer] - min_age - transfer   for every consumer
    #     <= start[next lot on the machine] - its changeover
    # A lot whose floor exceeds its ceiling cannot be made fresh at all - the
    # two consumers it serves are further apart than its shelf life - and is
    # REPORTED rather than silently left expired.
    n_fresh = 0
    stuck: dict = {}
    _v0 = sum(1 for l in start for s2 in succ.get(l, ())
              if s2 in start and max_age.get(_age_key(l)) is not None
              and start[s2] > finish[l] + max_age[_age_key(l)] + pd.Timedelta(seconds=1))
    print(f"  Freshness in    : {_v0:,} edge(s) consume expired material before repair"
          f"   [{len(max_age):,} item(s) carry a shelf life]")
    for _pass in range(4):
        pos = {}
        for m, seq in m_seq.items():
            seq.sort(key=lambda x: start[x])
            for i, x in enumerate(seq):
                pos[x] = (m, i)
        moved = 0
        for l in sorted(start, key=lambda x: start[x]):
            if l in FIXED:
                continue                       # mixing lots are fixed (README s.1)
            ma = max_age.get(_age_key(l))
            if ma is None:
                continue
            floor = None                       # earliest LEGAL finish for freshness
            for s2 in succ.get(l, ()):
                if s2 not in start:
                    continue
                need = start[s2] - ma          # finish must be at or after this
                if floor is None or need > floor:
                    floor = need
            if floor is None or floor <= finish[l] + pd.Timedelta(seconds=1):
                continue                       # already fresh
            # THE CEILING IS PHYSICAL ONLY - NOT THE DEADLINE.
            # `lft` is when the lot SHOULD have been done; being past it makes a
            # lot late, which is measured separately and costs tyres in the
            # horizon. Expiry is worse than late: expired material cannot make a
            # tyre at all. So LFT must not stop a lot moving into date. The only
            # real ceilings are the ones that would break something already
            # proven - a consumer's min aging, and the next lot on the machine.
            ceil = None
            for s2 in succ.get(l, ()):
                if s2 in start:
                    c = start[s2] - min_age.get(_age_key(l), pd.Timedelta(0)) - transfer
                    ceil = c if (ceil is None or c < ceil) else ceil
            m, i = pos[l]
            seq = m_seq[m]
            if i + 1 < len(seq):
                nxt = seq[i + 1]
                c = start[nxt] - pd.Timedelta(minutes=co_of.get(nxt, 0.0))
                ceil = c if (ceil is None or c < ceil) else ceil
            target = floor
            if ceil is not None and target > ceil:
                # cannot be made fresh from this position: its consumers are
                # further apart than its own shelf life, or the machine behind it
                # leaves no room. Recorded every pass, keyed by lot, so the last
                # word is the state it finally settles in.
                stuck[l] = {
                    "lot_id": l, "item": item[l],
                    "shelf_life_h": ma.total_seconds() / 3600.0,
                    "finish": finish[l],
                    "earliest_legal_finish": floor,
                    "latest_legal_finish": ceil,
                    "short_by_h": (floor - ceil).total_seconds() / 3600.0}
                continue
            stuck.pop(l, None)
            new_s = target - dur[l]
            _cd = pd.Timedelta(minutes=co_of.get(l, 0.0))
            if _cd > pd.Timedelta(0):
                legal = co_window_back(new_s - _cd, co_of.get(l, 0.0))
                new_s = legal + _cd
            if new_s > start[l]:
                start[l], finish[l] = new_s, new_s + dur[l]
                moved += 1
        n_fresh += moved
        if not moved:
            break
    n_stuck = len(stuck)
    stuck_rows = list(stuck.values())
    if n_fresh or n_stuck:
        print(f"  Freshness       : {n_fresh:,} lot(s) moved later so their material "
              f"is still in date when it is used"
              + (f"; {n_stuck:,} CANNOT be made fresh (consumers further apart "
                 f"than the shelf life) -> SHELF_LIFE_STUCK.csv" if n_stuck else ""))
        if stuck_rows:
            pd.DataFrame(stuck_rows).to_csv(
                io.OUT_ROOT / args.run / "SHELF_LIFE_STUCK.csv",
                index=False, encoding="utf-8-sig")
    else:
        print("  Freshness       : every lot was already consumed inside its shelf life")

    print(f"  Right-shifted   : {n_shift:,} lot(s) slid toward consumption"
          + (f"   [{n_shift_blocked:,} held back by the BR-14 handover window]"
             if n_shift_blocked else ""))

    if MIX_PREP:
        lots = pd.concat([lots, pd.DataFrame([{
            "key": pr["key"], "line": pr["line"], "lot_id": pr["lot_id"],
            "item_code": pr["item"], "item_type": pr["itype"],
            "department": "FINAL MIXING", "operation": "FINAL MIXING",
            "machine": str(pr["machine"]), "lot_qty": pr["qty"], "uom": "KG",
            "batches": pr["batches"], "duration_h": pr["dur"].total_seconds() / 3600.0,
            "LFT": pd.NaT} for pr in MIX_PREP])], ignore_index=True)
    _mixkind = {k: "fixed" for k in FIXED}
    _mixkind.update({pr["key"]: "prep" for pr in MIX_PREP})
    sched = pd.DataFrame([{
        "line": r.line, "lot_id": r.lot_id,
        "item_code": r.item_code, "item_type": r.item_type,
        "department": r.department, "operation": r.operation, "machine": r.machine,
        "lot_qty": r.lot_qty, "uom": r.uom, "batches": r.batches,
        "changeover_min": co_of.get(r.key, 0.0),
        "start": start.get(r.key), "finish": finish.get(r.key),
        # THE DURATION THE LOT ACTUALLY OCCUPIES THE MACHINE FOR, which is
        # `dur[key]` - it carries BR4RC-13's ramp. Writing r.duration_h instead
        # made the ramp invisible in the output: 81 calender lots read as
        # 6.75 h of work while the machine was held for 6.75 h MORE than that,
        # and the rule checker rightly failed it.
        "duration_h": (dur[r.key].total_seconds() / 3600.0
                       if r.key in dur else r.duration_h),
        "work_h": r.duration_h,          # before the ramp, for reference
        "LFT": r.LFT,
        "late_h": max(0.0, (finish[r.key] - r.LFT).total_seconds() / 3600.0)
        if r.key in finish and pd.notna(r.LFT) else 0.0,
        "mix_plan": _mixkind.get(r.key, ""),
    } for r in lots.itertuples(index=False)])
    # MACHINE IS A STRING EVERYWHERE. PCR's ids are all numeric, so a per-line
    # CSV round-trips them as int64 while TBR's stay object (it has the
    # ASSUMED-BEAD WINDING pseudo-machine). Joining the two files then shows a
    # shared machine such as 901 twice. The in-memory calendar was never
    # affected -- this is the written output only.
    sched["machine"] = sched["machine"].astype(str)
    sched.insert(sched.columns.get_loc("machine") + 1, "machine_name",
                 sched["machine"].map(io.machine_names()).fillna(""))
    placed = sched[sched["start"].notna()].copy()
    late = placed[placed["late_h"] > 1e-6]
    print(f"  Late vs LFT     : {len(late):,} lots "
          f"({100*len(late)/max(len(placed),1):.1f}%)"
          f"   worst {late['late_h'].max() if len(late) else 0:,.1f} h")
    print(f"  Schedule span   : {placed['start'].min()}  ->  {placed['finish'].max()}")

    # ---- overlap check, re-derived from the written schedule --------------
    ov = 0
    for m, g in placed.groupby("machine"):
        g = g.sort_values("start")
        prev_f = None
        for s, f in zip(g["start"], g["finish"]):
            if prev_f is not None and s < prev_f - pd.Timedelta(seconds=1):
                ov += 1
            prev_f = max(prev_f, f) if prev_f is not None else f
    print(f"  Machine overlaps: {ov}  (must be 0 — measured on the written schedule)")

    # ---------------- the full-kit gate -----------------------------------
    _pk = placed["line"] + "::" + placed["lot_id"].astype(str)
    fin = dict(zip(_pk, placed["finish"]))

    # ---- v43e: A CALENDER DELIVERS ROLLS AS IT RUNS --------------------------
    # The kit gate took a lot's material as ready only when the WHOLE lot
    # finished. On a continuous machine (the 4-roll calender 901) rolls come off
    # the line all through the run, so the trolley first in line can take its
    # share long before the lot ends. With `campaign.progressive_release`, a
    # lot on a campaign machine releases each trolley's share at
    #     start + duration x (cumulative qty up to that trolley / lot qty)
    # in need-by order - never later than the lot's finish, never before its
    # start. Mixers, extruders etc. are untouched (batch / whole-lot release).
    PROG_REL = str(R.rule("campaign.progressive_release", False)).lower() in (
        "1", "true", "on", "yes")

    def _progressive(sch_keys, sch_start, sch_finish, sch_machine):
        out = {}
        if not PROG_REL or not CAMP_M:
            return out
        st_ = dict(zip(sch_keys, sch_start))
        fi_ = dict(zip(sch_keys, sch_finish))
        mc_ = dict(zip(sch_keys, sch_machine.astype(str)))
        a = alloc[alloc["key"].map(lambda k: mc_.get(k) in CAMP_M)]
        for k, g in a.groupby("key", sort=False):
            s0, f0 = st_.get(k), fi_.get(k)
            if s0 is None or f0 is None or pd.isna(s0) or pd.isna(f0):
                continue
            g = g.sort_values(["need_by", "bkey"], kind="stable")
            tot = float(g["qty"].sum()) or 1.0
            cum = 0.0
            for bk, q in zip(g["bkey"], g["qty"]):
                cum += float(q)
                t = s0 + (f0 - s0) * min(1.0, cum / tot)
                prev = out.get((k, bk))
                out[(k, bk)] = t if prev is None else max(prev, t)
        return out

    _prog = _progressive(_pk, placed["start"], placed["finish"], placed["machine"])
    if PROG_REL:
        print(f"  Progressive rel : v43e - {len(_prog):,} (lot, trolley) share(s) on "
              f"{sorted(CAMP_M)} released as the run produces them")
    it_of = dict(zip(_pk, placed["item_code"]))
    ready_b, cause_b = {}, {}
    missing_b = defaultdict(set)
    for r in alloc.itertuples(index=False):
        f = _prog.get((r.key, r.bkey), fin.get(r.key))
        if fin.get(r.key) is None:
            missing_b[r.bkey].add(r.key)
            continue
        rr = f + min_age.get((r.line, it_of.get(r.key, "")),
                             pd.Timedelta(0)) + transfer
        if r.bkey not in ready_b or rr > ready_b[r.bkey]:
            ready_b[r.bkey] = rr
            cause_b[r.bkey] = r.key

    blots["kit_ready"] = blots["bkey"].map(ready_b)
    blots["blocking_lot"] = blots["bkey"].map(cause_b)
    blots["blocking_item"] = blots["blocking_lot"].map(it_of)
    blots["kit_incomplete"] = blots["bkey"].map(
        lambda b: len(missing_b.get(b, ())) > 0)
    have = blots["kit_ready"].notna() & ~blots["kit_incomplete"]
    blots["shift_h"] = 0.0
    blots.loc[have, "shift_h"] = ((blots.loc[have, "kit_ready"]
                                   - blots.loc[have, "build_start"])
                                  .dt.total_seconds() / 3600.0).clip(lower=0.0)
    blots["feasible_start"] = blots["build_start"]
    if args.policy == "shift":
        mv = blots["shift_h"] > 1e-6
        blots.loc[mv, "feasible_start"] = blots.loc[mv, "kit_ready"]
    # v39: THE RUN TIME IS A COLUMN, SO IT TRAVELS WITH ITS TROLLEY.
    # It used to be a free Series. `blots` is re-sorted with reset_index a few
    # lines down, and `feasible_start + span` then aligned by index LABEL, so
    # every trolley got another trolley's run time: 300 overlapping lots
    # (644 h) on all 20 builders in e2e_v38, corr(duration, qty) = -0.035.
    blots["_span"] = (blots["build_end"] - blots["build_start"]).fillna(pd.Timedelta(0))
    blots["feasible_end"] = blots["feasible_start"] + blots["_span"]

    # ---- v42: A BUILDER'S SETUP BELONGS TO THE TROLLEY THAT ACTUALLY PRECEDES IT
    # The plan writes each trolley's changeover (CO_Mins) INSIDE its own
    # StartTime..EndTime, for the predecessor the PLAN gave it. Once trolleys
    # run in kit-ready order, a product change can follow a trolley that
    # carries no setup (45 changes on 16 builders in e2e_v40). Now:
    #   run time   = plan duration - plan setup
    #   setup      = plan CO_Mins when the plan states one (never removed);
    #                else, on a REAL product change, the plan's median setup
    #                for that SKU, else the line's median setup (A13);
    #                same product as the actual predecessor -> 0.
    SETUP_BY_PRED = str(R.rule("building.setup_by_actual_predecessor", False)
                        ).lower() in ("1", "true", "on", "yes")
    blots["_setup_plan"] = pd.to_timedelta(
        pd.to_numeric(blots["setup_min"], errors="coerce").fillna(0.0), unit="m")
    blots["_run"] = (blots["_span"] - blots["_setup_plan"]).clip(lower=pd.Timedelta(0))
    _st = blots[pd.to_numeric(blots["setup_min"], errors="coerce").fillna(0) > 0]
    _sku_setup = _st.groupby("sku")["setup_min"].median().to_dict()
    _line_setup = _st.groupby("line")["setup_min"].median().to_dict()
    A13_USED: list = []

    def _setup(i, prev_sku):
        """(minutes, source) of the setup trolley i needs after `prev_sku`."""
        pm = float(blots.at[i, "setup_min"] or 0.0)
        if pm > 0:
            return pm, "plan CO_Mins"
        sk = blots.at[i, "sku"]
        if prev_sku is None or prev_sku == sk:
            return 0.0, ""
        if sk in _sku_setup:
            return float(_sku_setup[sk]), "plan median CO_Mins for this SKU"
        return float(_line_setup.get(blots.at[i, "line"], 0.0)), "A13 line median CO_Mins"

    def _dur(i, prev_sku):
        if not SETUP_BY_PRED:
            return blots.at[i, "_span"]
        return blots.at[i, "_run"] + pd.Timedelta(minutes=_setup(i, prev_sku)[0])
    # THE RELEASE TIME: when this trolley's kit is genuinely ready. Everything
    # downstream places against this; `feasible_start` becomes the RESULT.
    blots["_release"] = blots["feasible_start"]

    # ------------------------------------------------------------------
    # A MACHINE BUILDS ONE TROLLEY AT A TIME — BUT IN KIT-READY ORDER.
    #
    # This used to lay the trolleys end to end in PLAN ORDER
    # (sort by build_start), which is where the schedule lost its sequencing.
    # A trolley whose kit was ready early sat behind one whose kit was ready
    # late, purely because the plan listed it later, and every trolley after it
    # inherited that delay plus its own run time.
    #
    # Measured on e2e_v9, TBR machine 3809: lot 2's kit was ready 28 Sep 01:03
    # — FIFTEEN HOURS before lot 1's — yet it was placed second and started
    # 29 Sep 00:51, a 23 h wait for a kit it already had. shift_h then
    # cascaded 9.9 -> 17.9 -> 25.9 -> 33.9 -> 41.9 h down the machine and five
    # of its twelve trolleys fell out of the horizon.
    #
    # v10's dispatcher does not work from a fixed list: a lot becomes eligible
    # when its gate opens and the machine picks from what is RELEASABLE. That
    # is what this now does. At each machine the clock advances to the earliest
    # ready kit; among the trolleys already released at that moment it takes
    # the one the PLAN wanted first, so the plan's intent still breaks ties and
    # nothing is reordered arbitrarily.
    #
    # The kit gate is untouched: a trolley can still never start before
    # kit_ready. This only stops it waiting LONGER than it has to.
    # ------------------------------------------------------------------
    seq_mode = str(R.rule("building.sequence_by", "plan_order")).lower()
    blots = blots.sort_values(["machine", "build_start", "building_lot_id"],
                              kind="stable").reset_index(drop=True)

    if seq_mode == "kit_ready":
        fs = [pd.NaT] * len(blots)
        n_resequenced = 0
        for m, grp in blots.groupby("machine", sort=False):
            pend = [i for i in grp.index if not blots.at[i, "kit_incomplete"]
                    and pd.notna(blots.at[i, "_release"])]
            for i in grp.index:
                if i not in pend:
                    fs[i] = pd.NaT
            clock = None
            order_pos = 0
            last_sku = None
            while pend:
                # release the clock to the earliest kit that is ready
                if clock is None:
                    clock = min(blots.at[i, "_release"] for i in pend)
                ready = [i for i in pend
                         if blots.at[i, "_release"] <= clock]
                if not ready:
                    clock = min(blots.at[i, "_release"] for i in pend)
                    ready = [i for i in pend
                             if blots.at[i, "_release"] <= clock]
                # among the released, honour the PLAN's own intent
                pick = min(ready, key=lambda i: (blots.at[i, "build_start"],
                                                 blots.at[i, "building_lot_id"]))
                if pick != pend[0]:
                    n_resequenced += 1
                st = past_holiday(max(clock, blots.at[pick, "_release"]))
                fs[pick] = st
                dur = _dur(pick, last_sku)          # v42: setup per actual predecessor
                last_sku = blots.at[pick, "sku"]
                clock = st + dur
                pend.remove(pick)
        print(f"  Building seq    : KIT-READY dispatch (v10) — "
              f"{n_resequenced:,} trolley(s) took a slot the plan order "
              f"would have given to a later-ready kit")
    else:
        # PLAN ORDER (v42f: the plant's rule - same machine, same SKU sequence
        # as the plan, only the times move). Each builder takes its trolleys in
        # the plan's order; a trolley starts at the later of its kit being
        # ready and the previous trolley's end, so a late kit holds back every
        # trolley behind it on that builder.
        free: dict = {}
        last: dict = {}
        fs = []
        for i in blots.index:
            s = blots.at[i, "feasible_start"]
            if blots.at[i, "kit_incomplete"] or pd.isna(s):
                fs.append(pd.NaT)
                continue
            mc = blots.at[i, "machine"]
            mf = free.get(mc)
            if mf is not None and s < mf:
                s = mf
            s = past_holiday(s)
            fs.append(s)
            free[mc] = s + _dur(i, last.get(mc))
            last[mc] = blots.at[i, "sku"]
        print("  Building seq    : PLAN ORDER - every builder keeps the plan's "
              "trolley sequence; only times move")
    blots["feasible_start"] = fs

    # ------------------------------------------------------------------
    # MACHINE UTILISATION — move work off a saturated builder onto an idle
    # one, but ONLY where the plant's own allowable matrix permits it.
    #
    # Sequencing fixes the ORDER on a machine; it cannot fix the LOAD. On
    # e2e_v10 the builders were badly unbalanced: TBR 3806 sat 64 h idle at
    # 33% while 3809 ran at 100% and pushed four trolleys past the horizon,
    # and PCR 3409/3407/3410 each held 48 h of idle while 3411 overran. 568
    # machine-hours were idle while 34 trolleys fell out of the window.
    #
    # v10's phase 4b balances load per (machine, DAY) rather than per month,
    # because the plant's KPI is daily. The same idea applies here: a trolley
    # that cannot make the horizon on its planned machine is offered to every
    # OTHER machine the matrix allows for that SKU, and moves to whichever can
    # start it earliest. The kit gate is never bypassed - the candidate start
    # is still >= kit_ready - and a move is kept only if it genuinely pulls
    # the trolley earlier, so this can never make the schedule worse.
    # ------------------------------------------------------------------
    if str(R.rule("building.rebalance", "off")).lower() in ("on", "true", "yes"):
        blots["_dur"] = (blots["build_end"] - blots["build_start"]).fillna(
            pd.Timedelta(0))
        allow = _building_allowable(lines, io.machine_alias(mach_df))
        # MACHINE IDS ARE STRINGS, EVERYWHERE. PCR's are all numeric so the
        # column arrives as int64 while the allowable matrix yields str - and
        # `"3402" in {3402: [...]}` is False, so every candidate machine was
        # silently skipped and the rebalancer moved nothing at all. Same dtype
        # trap that once printed shared machine 901 twice.
        blots["machine"] = blots["machine"].astype(str)
        by_m = {str(m): list(g.index)
                for m, g in blots.groupby("machine", sort=False)}
        horizon0 = blots["build_end"].max() + pd.Timedelta(
            hours=args.horizon_extend_h)
        moved = 0
        for _pass in range(60):
            starts = {}
            for m, idxs in by_m.items():
                live = [i for i in idxs if not blots.at[i, "kit_incomplete"]
                        and pd.notna(blots.at[i, "_release"])]
                starts.update(_serialise(live, blots, past_holiday, _dur))
            # worst offenders first: the latest-finishing trolleys
            late = sorted([i for i, st in starts.items()
                           if st + blots.at[i, "_dur"] > horizon0],
                          key=lambda i: -(starts[i] + blots.at[i, "_dur"]).value)
            did = False
            for i in late:
                sku = str(blots.at[i, "sku"])
                cur = str(blots.at[i, "machine"])
                cand = sorted(allow.get(sku, set()) - {cur})
                if not cand:
                    continue
                best_m, best_st = None, starts[i]
                for m in cand:
                    if m not in by_m:
                        continue
                    trial = [x for x in by_m[m]
                             if not blots.at[x, "kit_incomplete"]
                             and pd.notna(blots.at[x, "_release"])] + [i]
                    st = _serialise(trial, blots, past_holiday, _dur).get(i)
                    if st is not None and st < best_st - pd.Timedelta(minutes=1):
                        best_m, best_st = m, st
                if best_m:
                    by_m[cur].remove(i)
                    by_m.setdefault(best_m, []).append(i)
                    blots.at[i, "machine"] = best_m
                    moved += 1
                    did = True
                    break
            if not did:
                break
        # final placement on the settled assignment
        final = {}
        for m, idxs in by_m.items():
            live = [i for i in idxs if not blots.at[i, "kit_incomplete"]
                    and pd.notna(blots.at[i, "_release"])]
            final.update(_serialise(live, blots, past_holiday, _dur))
        blots["feasible_start"] = [final.get(i, pd.NaT) for i in blots.index]
        blots.drop(columns=["_dur"], inplace=True)
        print(f"  Building rebal  : {moved} trolley(s) moved to an allowable "
              f"machine that could start them sooner")

    blots["feasible_end"] = blots["feasible_start"] + blots["_span"]
    blots["shift_h"] = ((blots["feasible_start"] - blots["build_start"])
                        .dt.total_seconds() / 3600.0).clip(lower=0.0)

    # THE HORIZON IS THE PLAN'S OWN LAST BUILD, optionally extended.
    # Work that finishes after it is real work the plant did - it simply lands
    # too late to count against THIS plan. Extending the horizon does not make
    # the plant faster; it asks "how much would land if the window were longer".
    horizon = blots["build_end"].max() + pd.Timedelta(hours=args.horizon_extend_h)
    blots["within_horizon"] = blots["feasible_end"].notna() & (blots["feasible_end"] <= horizon)

    n_inc = int(blots["kit_incomplete"].sum())
    n_mv = int((blots["shift_h"] > 1e-6).sum())
    print(f"\n  FULL-KIT GATE")
    print(f"    trolleys          : {len(blots):,}   tyres {blots['qty'].sum():,.0f}")
    print(f"    kit INCOMPLETE    : {n_inc:,}  (a component never got placed)")
    print(f"    kit late          : {n_mv:,} "
          f"({100*n_mv/max(len(blots),1):.1f}%)")
    if n_mv:
        s = blots.loc[blots["shift_h"] > 1e-6, "shift_h"]
        print(f"    shift hours       : median {s.median():.1f}  p90 {s.quantile(0.9):.1f}"
              f"  max {s.max():.1f}")
    ok = blots[blots["within_horizon"]]
    print(f"    WITHIN HORIZON    : {ok['qty'].sum():,.0f} of {blots['qty'].sum():,.0f}"
          f" tyres = {100*ok['qty'].sum()/max(blots['qty'].sum(),1):.1f}%"
          f"   (horizon {horizon})")
    top = (blots[blots["shift_h"] > 1e-6].groupby("blocking_item")
           .agg(trolleys=("shift_h", "size"), worst_h=("shift_h", "max"),
                tyres=("qty", "sum")).sort_values("tyres", ascending=False).head(8))
    if len(top):
        print(f"    top blocking items:")
        for i, r in top.iterrows():
            print(f"      {str(i)[:30]:31s} trolleys={int(r['trolleys']):>5,d}"
                  f"  tyres={r['tyres']:>9,.0f}  worst {r['worst_h']:>8.1f} h")

    util = (placed.assign(setup_h=placed["changeover_min"] / 60.0)
            .groupby("machine").agg(lots=("lot_id", "size"),
                                    work_h=("duration_h", "sum"),
                                    setup_h=("setup_h", "sum")))
    util["span_h"] = (horizon - open_at).total_seconds() / 3600.0
    util["util_pct"] = (100 * (util["work_h"] + util["setup_h"]) / util["span_h"]).round(1)
    util = util.sort_values("util_pct", ascending=False)
    print(f"\n  MACHINE UTILISATION (top 10 of {len(util)})")
    for m, r in util.head(10).iterrows():
        nm = mach_df.loc[mach_df.machineDisplayId == str(m), "machineDisplayName"]
        print(f"    {str(m):8s} {(nm.iloc[0] if len(nm) else '')[:30]:31s}"
              f" lots={int(r['lots']):>6,d} work={r['work_h']:>8,.0f} h"
              f" setup={r['setup_h']:>6,.0f} h  {r['util_pct']:>6.1f}%")

    # THE UTILISATION IS A PROPERTY OF THE MACHINE, NOT OF A LINE. In joint mode
    # it is written ONCE at the run root and IS the real load; the per-line split
    # beside it is only each line's share of that machine.
    if joint:
        share = (placed.groupby(["machine", "line"])["duration_h"].sum()
                 .unstack(fill_value=0.0))
        util["line_share"] = ""
        for m in util.index:
            if m in share.index:
                util.loc[m, "line_share"] = " ".join(
                    f"{c}:{share.loc[m, c]:,.0f}h" for c in share.columns
                    if share.loc[m, c] > 0)
        util.insert(0, "machine_name", [io.machine_names().get(str(m), "")
                                        for m in util.index])
        util.to_csv(io.OUT_ROOT / args.run / "MACHINE_UTILISATION_JOINT.csv")

    # WHICH SEQUENCING RULES ACTUALLY FIRED. Printed from the dispatcher's own
    # counters, so "applied" means it changed a pick - not that a flag was set.
    if rule_fired:
        print("  Sequencing      : " + "  ".join(
            f"{k} x{v:,}" for k, v in sorted(rule_fired.items())))
    else:
        print("  Sequencing      : no sequencing rule changed a pick")

    # PLANT RULE — BEAD WINDING LEADS BEAD APEXING.
    # A bead must be WOUND before it can be APEXED, and the floor keeps wound
    # bead ahead of the apexing line rather than level with it. The BOM already
    # carries the precedence lot-by-lot, but that only constrains each apex lot
    # against ITS OWN feeder; it permits the two departments to run neck and
    # neck in aggregate, which is what the plant flagged on the TBR schedule.
    #
    # So the cumulative curve is enforced directly: at no moment may total
    # apexed output exceed total wound output. Any apex lot that would break it
    # is right-shifted to the winding finish that covers it. Shifting LATER only
    # - never earlier - so phase 5's feasibility and aging guarantees hold.
    # v45a: THE AGGREGATE BEAD GATE IS OPTIONAL. It compared TOTAL apexed with
    # TOTAL newly wound quantity per line - across all items, ignoring wound-bead
    # opening stock (PCR: 18,819 units drawn) - and slid apex lots later WITHOUT
    # re-laying their machine: e2e_v44c_all had 81 overlapping lots on PCR
    # apexing machines 201/202, and apex lots fed from stock waited for unrelated
    # winding (BA14006: ready 07:00, started 17:56). Each apex lot already waits
    # for its OWN winding lots through the material links (precedence edges), so
    # `bead_gate.enabled: false` turns the aggregate gate off.
    if str(R.rule("bead_gate.enabled", True)).lower() in ("1", "true", "on", "yes"):
        sched = _bead_gate(sched)
    else:
        print("  Bead gate       : OFF (bead_gate.enabled: false) - each apex lot waits "
              "for its own winding lots through the material links")

    # ---- THE KIT GATE MUST BE RE-DERIVED AFTER THE BEAD GATE MOVES A LOT ----
    # `kit_ready` was computed above from `placed`, and `_bead_gate` then slides
    # apex lots LATER to keep apexed output behind wound output. Any trolley
    # whose binding component is one of those lots was therefore holding a
    # kit_ready from before the move - and its building start could precede the
    # material by hours. Measured on e2e_v21 PCR: BLD000079 started 2.66 h
    # before BA16003_L00002 actually finished.
    #
    # It never bit while a runway hid it: with 96 h of head start the bead gate
    # had almost nothing to correct. With open_h 0 it has plenty, so the gate is
    # recomputed here, on the schedule as finally written, and any trolley it
    # moves is pushed out with it. LATER only - this can never pull a build in.
    _bf = dict(zip(sched["line"] + "::" + sched["lot_id"].astype(str),
                   sched["finish"]))
    _bi = dict(zip(sched["line"] + "::" + sched["lot_id"].astype(str),
                   sched["item_code"]))
    _ready2, _cause2 = {}, {}
    _sk = sched["line"] + "::" + sched["lot_id"].astype(str)
    _prog2 = _progressive(_sk, sched["start"], sched["finish"], sched["machine"])
    for r in alloc.itertuples(index=False):
        f = _prog2.get((r.key, r.bkey), _bf.get(r.key))
        if _bf.get(r.key) is None:
            continue
        rr = f + min_age.get((r.line, _bi.get(r.key, "")),
                             pd.Timedelta(0)) + transfer
        if r.bkey not in _ready2 or rr > _ready2[r.bkey]:
            _ready2[r.bkey] = rr
            _cause2[r.bkey] = r.key
    _new = blots["bkey"].map(_ready2)
    _moved = _new.notna() & blots["kit_ready"].notna() & (_new > blots["kit_ready"])
    n_bead_fix = int(_moved.sum())
    if n_bead_fix:
        _delta = ((_new[_moved] - blots.loc[_moved, "kit_ready"])
                  .dt.total_seconds() / 3600.0)
        blots.loc[_moved, "kit_ready"] = _new[_moved]
        blots.loc[_moved, "blocking_lot"] = blots.loc[_moved, "bkey"].map(_cause2)
        blots.loc[_moved, "blocking_item"] = blots.loc[_moved, "blocking_lot"].map(_bi)
        _late = _moved & (blots["feasible_start"] < blots["kit_ready"])
        blots.loc[_late, "feasible_start"] = blots.loc[_late, "kit_ready"]
        blots.loc[_late, "feasible_end"] = (blots.loc[_late, "feasible_start"]
                                            + blots.loc[_late, "_span"])
        blots.loc[_late, "_release"] = blots.loc[_late, "feasible_start"]
        blots["shift_h"] = ((blots["kit_ready"] - blots["build_start"])
                            .dt.total_seconds() / 3600.0).clip(lower=0.0).fillna(0.0)
        blots["within_horizon"] = (blots["feasible_end"].notna()
                                   & (blots["feasible_end"] <= horizon))
        print(f"  Bead gate       : {n_bead_fix:,} trolley(s) had their kit_ready "
              f"re-derived after the apex right-shift (median +{_delta.median():.2f} h, "
              f"max +{_delta.max():.2f} h); {int(_late.sum()):,} had to start later")
    else:
        print("  Bead gate       : no trolley's kit_ready moved")

    # ---- v39: ONE TROLLEY AT A TIME, ON THE WRITTEN BUILDING PLAN -----------
    # The bead gate above slides a trolley to its new kit_ready but leaves the
    # trolleys behind it where they were, so it can land on top of the next
    # one. Re-lay each builder in its current order: a trolley starts at the
    # later of its own start and the previous trolley's end. Starts only ever
    # move LATER, so the kit gate still holds.
    n_pushed = 0
    blots["setup_applied_min"] = pd.to_numeric(blots["setup_min"], errors="coerce").fillna(0.0)
    blots["setup_source"] = np.where(blots["setup_applied_min"] > 0, "plan CO_Mins", "")
    blots["prev_sku_on_machine"] = ""
    for m, g in blots[blots["feasible_start"].notna()].groupby("machine", sort=False):
        prev_end, prev_sku = None, None
        # v42f: in plan-order mode the re-lay keeps the PLAN's sequence, so a
        # trolley the bead gate delays holds back the ones behind it instead
        # of dropping behind them.
        _key = (["build_start", "building_lot_id"] if seq_mode == "plan_order"
                else ["feasible_start", "build_start"])
        for i in g.sort_values(_key, kind="stable").index:
            if SETUP_BY_PRED:
                mn, src = _setup(i, prev_sku)
                blots.at[i, "setup_applied_min"] = mn
                blots.at[i, "setup_source"] = src
                blots.at[i, "_span"] = blots.at[i, "_run"] + pd.Timedelta(minutes=mn)
                if src.startswith("A13"):
                    A13_USED.append((m, prev_sku, blots.at[i, "sku"], mn))
            blots.at[i, "prev_sku_on_machine"] = prev_sku or ""
            s = blots.at[i, "feasible_start"]
            if prev_end is not None and s < prev_end:
                s = past_holiday(prev_end)
                blots.at[i, "feasible_start"] = s
                n_pushed += 1
            prev_end = s + blots.at[i, "_span"]
            prev_sku = blots.at[i, "sku"]
    blots["feasible_end"] = blots["feasible_start"] + blots["_span"]
    if SETUP_BY_PRED:
        _chg = (blots["prev_sku_on_machine"] != "") & (blots["prev_sku_on_machine"] != blots["sku"])
        _added = _chg & (pd.to_numeric(blots["setup_min"], errors="coerce").fillna(0) == 0)
        print(f"  Builder setups  : {int(_chg.sum())} product change(s) on builders; "
              f"{int(_added.sum())} had no setup in the plan's trolley and got one "
              f"({len(A13_USED)} from A13 line median)")
    for ln in lines:
        _sk = set(blots.loc[blots["line"] == ln, "sku"])
        pd.DataFrame([{"code": "A13", "item": f"{m}:{a}->{b}", "department": "BUILDING",
                       "gap": "builder setup for a change the plan did not place",
                       "value": f"{mn:g} min", "source": "line median of plan CO_Mins"}
                      for m, a, b, mn in A13_USED if b in _sk],
                     columns=["code", "item", "department", "gap", "value", "source"]
                     ).to_csv(OUTS[ln] / "phase5_assumptions.csv", index=False)
    blots["within_horizon"] = (blots["feasible_end"].notna()
                               & (blots["feasible_end"] <= horizon))
    print(f"  Builder re-lay  : {n_pushed:,} trolley(s) moved behind the trolley "
          f"ahead of them on the same builder")

    # ---- v39: GATES G1 + G2, measured on the frame that is WRITTEN ----------
    _p = blots[blots["feasible_start"].notna()]
    _bad_dur = _p[(_p["feasible_end"] - _p["feasible_start"] - _p["_span"]).abs()
                  > pd.Timedelta(seconds=1)]
    _pairs = []
    for m, g in _p.groupby("machine", sort=False):
        g = g.sort_values("feasible_start")
        prev = None
        for r in g.itertuples(index=False):
            if prev is not None and r.feasible_start < prev.feasible_end - pd.Timedelta(seconds=1):
                _pairs.append((m, prev.building_lot_id, prev.feasible_end,
                               r.building_lot_id, r.feasible_start))
            if prev is None or r.feasible_end > prev.feasible_end:
                prev = r
    if SETUP_BY_PRED:
        _nosetup = int(((_p["prev_sku_on_machine"] != "") & (_p["prev_sku_on_machine"] != _p["sku"])
                        & (_p["setup_applied_min"] <= 0)).sum())
        print(f"  Builder G1b     : {_nosetup} product change(s) without a setup (must be 0)")
        if _nosetup:
            raise SystemExit("[FATAL] building plan fails G1b - a product change has no setup")
    print(f"  Builder overlaps: {len(_pairs)}   duration mismatches: {len(_bad_dur)}"
          f"   (G1/G2, must both be 0 — measured on phase5_building_plan)")
    if _pairs or len(_bad_dur):
        for p in _pairs[:10]:
            print(f"    OVERLAP {p[0]}: {p[1]} ends {p[2]} > {p[3]} starts {p[4]}",
                  file=sys.stderr)
        raise SystemExit("[FATAL] building plan fails G1/G2 - not written")
    blots = blots.drop(columns=["_span", "_run", "_setup_plan"])

    if DBG_M:
        _rnd = __import__("os").environ.get("CTP_MIX_ROUND", "1")
        _dp = io.OUT_ROOT / args.run / "DISPATCH_LOG_debug.csv"
        pd.DataFrame(DBG_ROWS).assign(round=_rnd).to_csv(_dp, index=False)
        print(f"  Dispatch log    : {DBG_PICK[0]:,} decision(s) on {sorted(DBG_M)}, "
              f"{len(DBG_ROWS):,} candidate row(s) -> {_dp.name} (round {_rnd})")

    for ln in lines:
        O = OUTS[ln]
        sl = sched[sched["line"] == ln]
        bl = blots[blots["line"] == ln]
        pl = placed[placed["line"] == ln]
        sl.to_csv(O / "phase5_schedule.csv", index=False)
        io.with_machine_name(bl.copy(), "machine").to_csv(
            O / "phase5_building_plan.csv", index=False)
        u = (pl.assign(setup_h=pl["changeover_min"] / 60.0).groupby("machine")
             .agg(lots=("lot_id", "size"), work_h=("duration_h", "sum"),
                  setup_h=("setup_h", "sum")))
        u["span_h"] = (horizon - open_at).total_seconds() / 3600.0
        u["util_pct"] = (100 * (u["work_h"] + u["setup_h"]) / u["span_h"]).round(1)
        u.insert(0, "machine_name", [io.machine_names().get(str(m), "") for m in u.index])
        u.sort_values("util_pct", ascending=False).to_csv(O / "phase5_machine_util.csv")
        bl[bl["shift_h"] > 1e-6].to_csv(O / "phase5_revisions.csv", index=False)
        okl = bl[bl["within_horizon"]]
        io.write_json(O / "phase5_stats.json", {
            "joint": joint, "lines": lines,
            "lots": int((lots["line"] == ln).sum()), "placed": int(len(pl)),
            "changeovers": int((pl["changeover_min"] > 0).sum()),
            "setup_h": float(pl["changeover_min"].sum()) / 60.0,
            "late_lots": int((pl["late_h"] > 1e-6).sum()), "overlaps": int(ov),
            "trolleys": int(len(bl)),
            "kit_incomplete": int(bl["kit_incomplete"].sum()),
            "kit_late": int((bl["shift_h"] > 1e-6).sum()),
            "tyres_plan": float(bl["qty"].sum()),
            "tyres_within_horizon": float(okl["qty"].sum()),
            "horizon": str(horizon)})
        print(f"  [{ln}] GT within horizon {okl['qty'].sum():>10,.0f} / "
              f"{bl['qty'].sum():>10,.0f} = "
              f"{100*okl['qty'].sum()/max(bl['qty'].sum(),1):>5.1f}%")
    print(f"\n[OK] phase5_schedule.csv / phase5_building_plan.csv per line"
          + ("   + MACHINE_UTILISATION_JOINT.csv" if joint else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
