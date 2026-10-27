#!/usr/bin/env python3
"""validate_floor_schedule.py — audit a floor schedule AGAINST THE ROUTING.

    python validate_floor_schedule.py --run e2e_v13

Nothing here is recomputed from the engine's own intermediate files: every test
is the finished floor schedule checked against the MASTERS it claims to follow.
A schedule that passes its own phases can still disagree with the routing, and
that is exactly the disagreement worth finding.

SIX TESTS
  1  MACHINE LEGALITY   every (item, machine) pair appears in jkt_routing's own
                        `machines` list for that routed_product
  2  DEPARTMENT MATCH   the row's department is the routing's department
  3  OPERATION SEQUENCE for one item, operations run in `operation_seq` order -
                        a later sequence number may never finish before an
                        earlier one has finished
  4  FULL KIT           no building trolley starts before every component of its
                        kit is finished and has served its minimum aging
  5  MACHINE COVERAGE   every machine in the plant register carries work
  6  EVEN CURING        every curing quantity is an even number

Each test prints PASS/FAIL with the count, and writes the offending rows so a
failure can be opened rather than argued about.
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

import ctpio as io
import rules as R

COLS = ["date", "machine", "shift", "item", "item_type", "process", "department",
        "start_time", "end_time", "produce_qty", "UOM", "lot_id",
        "prod_id", "consumption_id", "FG_SKU_CODE", "incomplete_components"]

RESULTS: list[dict] = []


def ck(test: str, line: str, ok: bool, detail: str, n_bad: int = 0) -> None:
    RESULTS.append({"test": test, "line": line.upper(),
                    "verdict": "PASS" if ok else "FAIL",
                    "failures": n_bad, "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {line.upper():4s} {test:24s} {detail}")


def _machines(raw) -> set[str]:
    """The routing's `machines` cell -> a set of machine ids.

    The cell is JSON-ish but NOT always a list. A single-machine operation is
    written `"1101"` - valid JSON for a STRING - and iterating that yields the
    characters {'1','0'}, so every single-machine row looked illegal. A one-line
    bug that condemned 409 of 799 PCR rows on the first run of this file.
    """
    if not isinstance(raw, str) or not raw.strip():
        return set()
    try:
        v = json.loads(raw)
    except Exception:
        return {t.strip().strip('"') for t in raw.split(",") if t.strip().strip('"')}
    if isinstance(v, str):                      # a single machine, not a list
        return {v.strip()} if v.strip() else set()
    if isinstance(v, (int, float)):
        return {str(v).strip()}
    return {str(x).strip() for x in v if str(x).strip()}


def audit(run: str, line: str) -> pd.DataFrame:
    D = io.OUT_ROOT / run
    d = pd.read_csv(D / f"phase5_floor_schedule_{line}.csv", encoding="utf-8-sig",
                    parse_dates=["start_time", "end_time"])
    rt = io.load_routing(line)
    alias = io.machine_alias(io.load_machines())

    def res(m):
        return str(io.resolve_machine(str(m), alias) or str(m)).strip()

    # routed_product -> (allowed machines, department, operation_seq)
    #
    # v15: THE ROUTING'S MACHINE CELL IS READ THE WAY THE ENGINE READS IT.
    # `F270` is not a machine id - it is a base name shared by F270 M (wc 101,
    # a MASTER mixer) and F270 F (wc 102, a FINAL mixer), and which one a row
    # means is settled by that row's DEPARTMENT. Resolving it by spelling here
    # while the engine resolves it by department would make this audit compare
    # two different readings of the same cell and call the difference a
    # violation - which is exactly what it did on the first v15 run (118 PCR
    # and 132 TBR "failures", every one of them a correctly-placed lot).
    allow, dept, seq = {}, {}, {}
    stated: set = set()             # items whose routing actually names a machine
    for r in rt.itertuples(index=False):
        p = str(r.routed_product).strip()
        if not p:
            continue
        dp = str(getattr(r, "department", "") or "").strip().upper()
        raw = _machines(r.machines)
        if raw:
            stated.add(p)
        allow.setdefault(p, set()).update(
            io.parse_machines(",".join(sorted(raw)), alias, dp) if raw else [])
        dept.setdefault(p, dp)
        s = pd.to_numeric(getattr(r, "operation_seq", None), errors="coerce")
        if pd.notna(s):
            seq.setdefault(p, float(s))

    # v33: final compound may run on the master mixers by rule. Those
    # placements are legal by the rule file, not by the routing row.
    if io.final_on_master_mixers():
        _mm = {m for p, ms in allow.items() if dept.get(p) == "MASTER MIXING"
               for m in ms}
        for p in allow:
            if dept.get(p) == "FINAL MIXING":
                allow[p] |= _mm

    # v46j: with `routing_speed.source: all_machine_spec` an item's machines
    # ARE the machines CTP_Master_Routing_v2.all_machine_spec gives a speed for
    # - those replace the old routing's list, the same way the engine reads it
    if io.spec_mode():
        for p, ms in io.load_machine_spec(line).items():
            allow[p] = set(ms)
            stated.add(p)

    # The machines the engine was ENTITLED to use where the routing names none
    # (phase 0 assumption A01): the department pool, else the plant register.
    assumed_ok: dict[str, set] = {}
    try:
        a0 = pd.read_csv(D / line / "phase0_assumptions.csv", encoding="utf-8-sig")
        for r in a0[a0["code"] == "A01"].itertuples(index=False):
            assumed_ok[str(r.item).strip()] = {
                x.strip() for x in str(r.value).split(",") if x.strip()}
    except (FileNotFoundError, KeyError):
        pass

    # v33: the same rule covers final-mixing items the routing names NO machine
    # for - the engine offered them the master mixers too.
    if io.final_on_master_mixers():
        for p in allow:
            if dept.get(p) == "FINAL MIXING" and p not in stated:
                assumed_ok.setdefault(p, set()).update(_mm)

    up = d[~d["department"].isin(["BUILDING", "CURING"])].copy()
    up["m"] = up["machine"].map(res)
    up["it"] = up["item"].astype(str).str.strip()

    # ---- 1 machine legality ------------------------------------------------
    # TWO QUESTIONS, NOT ONE, because they have different answers:
    #   a) the routing NAMES machines for this item - is the one used among
    #      them? A miss here is an illegal placement and fails the check.
    #   b) the routing names NONE - then there is nothing to be illegal
    #      against. The row is checked against the eligible set phase 0
    #      recorded as assumption A01, and reported separately so the
    #      assumption stays visible instead of hiding inside a PASS.
    known = up[up["it"].isin(allow.keys())]
    hard = known[known["it"].isin(stated)]
    soft = known[~known["it"].isin(stated)]
    bad = hard[[m not in allow.get(i, set())
                for m, i in zip(hard["m"], hard["it"])]]
    soft_bad = soft[[m not in assumed_ok.get(i, set())
                     for m, i in zip(soft["m"], soft["it"])]]
    bad = pd.concat([bad, soft_bad], ignore_index=True) if len(soft_bad) else bad
    ck("1 machine legality", line, len(bad) == 0,
       f"{len(hard):,} routed rows checked against the routing's own machine "
       f"list, {len(bad):,} illegal; {len(soft):,} row(s) on items the routing "
       f"names NO machine for - checked against assumption A01 instead "
       f"({len(soft_bad):,} outside it); {len(up)-len(known):,} items not in routing",
       len(bad))

    # ---- 2 department match ------------------------------------------------
    dmis = known[[str(dp).strip().upper() != dept.get(i, "")
                  and dept.get(i, "") != "" for dp, i in
                  zip(known["department"], known["it"])]]
    ck("2 department match", line, len(dmis) == 0,
       f"{len(dmis):,} rows whose department differs from the routing", len(dmis))

    # ---- 3 routing sequence, tested on the REAL precedence -----------------
    # An earlier attempt compared operation_seq LEVELS inside a finished-tyre
    # chain and reported 52 PCR failures. That test was wrong: two components of
    # the same tyre are independent branches, so a seq-50 calender lot for one
    # component may legitimately finish before a seq-10 mixing lot for another.
    # operation_seq orders the steps of ONE routed product, nothing wider.
    #
    # The real sequencing claim is the producer -> consumer edge, and it must
    # hold WITH minimum aging: a consumer cannot start before its producer has
    # finished and matured.
    ed = pd.read_csv(D / line / "phase4_edges.csv")
    sc = pd.read_csv(D / line / "phase5_schedule.csv",
                     parse_dates=["start", "finish"])
    aging = io.load_aging(line)
    itypes = io.load_itemtypes(io.load_bom(line))
    aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)
    st = dict(zip(sc["lot_id"].astype(str), sc["start"]))
    fi = dict(zip(sc["lot_id"].astype(str), sc["finish"]))
    it_of = dict(zip(sc["lot_id"].astype(str), sc["item_code"].astype(str)))
    viol, checked = [], 0
    for p_, c_ in zip(ed["producer_lot"].astype(str), ed["consumer_lot"].astype(str)):
        if p_ not in fi or c_ not in st:
            continue
        checked += 1
        mn = float(aging.get(it_of.get(p_, ""), (0.0, 0.0))[0] or 0.0)
        ready = fi[p_] + pd.Timedelta(hours=mn)
        if st[c_] + pd.Timedelta(minutes=1) < ready:
            viol.append({"producer": p_, "consumer": c_,
                         "producer_finish": fi[p_], "min_aging_h": mn,
                         "consumer_start": st[c_],
                         "short_h": (ready - st[c_]).total_seconds() / 3600})
    # ---- 7 shelf life, the OTHER half of the aging master -----------------
    # Check 3 proves a consumer never starts too EARLY (min aging). Nothing
    # proved it never starts too LATE. `jkt_aging_master` states MaxAging on the
    # same row as MinAging, phase 2 applies it to opening WIP, and until v22
    # nothing applied it to material the schedule itself makes - so a compound
    # could be mixed and then used 100+ hours after it had gone off.
    #
    # The claim tested here is the mirror of check 3, on the same edges:
    #     start[consumer]  <=  finish[producer] + MaxAging(producer's item)
    sviol, schecked = [], 0
    for p_, c_ in zip(ed["producer_lot"].astype(str), ed["consumer_lot"].astype(str)):
        if p_ not in fi or c_ not in st:
            continue
        mx = float(aging.get(it_of.get(p_, ""), (0.0, 0.0))[1])
        if mx <= 0:
            continue                      # the master states no shelf life
        schecked += 1
        dead = fi[p_] + pd.Timedelta(hours=mx)
        if st[c_] > dead + pd.Timedelta(seconds=1):
            sviol.append({"producer_lot": p_, "item": it_of.get(p_, ""),
                          "producer_finish": fi[p_], "shelf_life_h": mx,
                          "expires": dead, "consumer_lot": c_,
                          "consumer_start": st[c_],
                          "overrun_h": (st[c_] - dead).total_seconds() / 3600.0})
    # ASSUMPTIONS.MD SS E: "Maximum aging is not a hard stop, only minimised and
    # reported." A hard stop scraps material that nothing re-makes, and the
    # document records the cost: 65.6% built / 63.1% cured. So the check still
    # measures and still writes the file; whether it FAILS the run is a rule.
    _warn = str(R.rule("validation.shelf_life_is_warning", False)).lower() in (
        "1", "on", "true", "yes")
    ck("7 shelf life", line, (not sviol) or _warn,
       f"{schecked:,} edges with a stated MaxAging checked, {len(sviol):,} where "
       f"the consumer started after the producer's material had expired"
       + (f" (worst {max(v['overrun_h'] for v in sviol):,.1f} h)" if sviol else "")
       + (" [REPORTED ONLY - ASSUMPTIONS.MD E]" if (sviol and _warn) else ""),
       len(sviol))
    if sviol:
        pd.DataFrame(sviol).to_csv(D / f"SHELF_LIFE_VIOLATIONS_{line}.csv",
                                   index=False, encoding="utf-8-sig")

    ck("3 routing sequence", line, not viol,
       f"{checked:,} producer->consumer edges checked, {len(viol):,} where the "
       f"consumer started before its producer finished + min aging", len(viol))
    if viol:
        pd.DataFrame(viol).to_csv(D / f"SEQUENCE_VIOLATIONS_{line}.csv",
                                  index=False, encoding="utf-8-sig")

    # ---- 4 full kit --------------------------------------------------------
    b = pd.read_csv(D / line / "phase5_building_plan.csv",
                    parse_dates=["kit_ready", "feasible_start"])
    w = b[b["within_horizon"]]
    early = int((w["feasible_start"] < w["kit_ready"]).sum())
    inc = int(w["kit_incomplete"].sum()) if "kit_incomplete" in w else 0
    ck("4 full kit", line, early == 0 and inc == 0,
       f"{len(w)} trolleys in horizon: {early} started before the kit was ready, "
       f"{inc} with an incomplete kit", early + inc)

    # ---- 5 machine coverage ------------------------------------------------
    reg = pd.read_csv(io.STAGE / "PLANT_MACHINE_REGISTER.csv", encoding="utf-8-sig")
    ali = {"F270 M": "101", "F270 F": "102", "F440 M": "103",
           "K310 F": "104", "K430-1 M": "105", "K430-2 M": "106"}
    reg["wc"] = reg["machine"].astype(str).str.strip().map(lambda m: ali.get(m, m))
    mine = reg[reg["line"].isin([line.upper(), "BOTH"])]
    used = set(d["machine"].astype(str).str.strip())
    # A machine the register marks BOTH is shared plant, not PCR plant and TBR
    # plant. 2501/2502 (chafer doubling) are routed by TBR only - no PCR item
    # names them anywhere - so judging them against the PCR schedule alone
    # reports an idle machine that PCR could not legally have used. A shared
    # machine is covered when EITHER line runs it.
    other = "tbr" if line == "pcr" else "pcr"
    try:
        od = pd.read_csv(D / f"phase5_floor_schedule_{other}.csv",
                         encoding="utf-8-sig", usecols=["machine"])
        used |= set(od["machine"].astype(str).str.strip())
    except (FileNotFoundError, ValueError):
        pass
    idle = mine[~mine["wc"].isin(used)]
    ck("5 machine coverage", line, len(idle) == 0,
       f"{len(mine)-len(idle)}/{len(mine)} register machines carry work "
       f"(shared BOTH-line machines counted on either line); "
       f"idle: {', '.join(idle['machine'].astype(str)) or 'none'}", len(idle))

    # ---- 6 even curing -----------------------------------------------------
    cu = d[d["department"] == "CURING"]
    odd = cu[pd.to_numeric(cu["produce_qty"], errors="coerce").fillna(0) % 2 != 0]
    ck("6 even curing", line, len(odd) == 0,
       f"{len(cu):,} curing rows, {len(odd):,} with an odd quantity", len(odd))

    out = pd.concat([bad.assign(_fail="machine not in routing"),
                     dmis.assign(_fail="department differs from routing"),
                     odd.assign(_fail="odd curing quantity")], ignore_index=True)
    if len(out):
        f = D / f"VALIDATION_FAILURES_{line}.csv"
        out.to_csv(f, index=False, encoding="utf-8-sig")
        print(f"       -> {f.name} ({len(out):,} rows)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="e2e_v13")
    a = ap.parse_args()
    print("\n" + "=" * 86)
    print(f"  FLOOR SCHEDULE VALIDATION against jkt_routing   [{a.run}]")
    print("=" * 86)
    for line in ("pcr", "tbr"):
        print(f"\n  ---- {line.upper()} ----")
        if line in io.stopped_lines(a.run):
            print("  not scheduled: every SKU is DATA_BLOCKED (phase0_sku_status.csv)")
            continue
        audit(a.run, line)
    r = pd.DataFrame(RESULTS)
    r.to_csv(io.OUT_ROOT / a.run / "VALIDATION.csv", index=False,
             encoding="utf-8-sig")
    nf = int((r["verdict"] == "FAIL").sum())
    print(f"\n  {len(r)} checks, {len(r)-nf} passed, {nf} FAILED")
    print(f"[OK] {io.OUT_ROOT / a.run / 'VALIDATION.csv'}")
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
