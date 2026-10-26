#!/usr/bin/env python3
"""phase7_curing.py — the curing plan, gated by the green tyres that actually exist.

v10 phase 7 on CTP data.

WHERE IT SITS
    components -> BUILDING (full-kit gate) -> GT
    GT + GT opening stock -> CURING (this phase) -> cured tyres

The curing plan is the plant's own frozen file, exactly like the building plan.
It is never rewritten; a shortfall shows up as a press lot landing on a later
day, reported as a revision.

WHAT GATES A CURING LOT
  1. GREEN TYRE SUPPLY. Matching is CUMULATIVE per SKU: the n-th cured tyre
     needs the n-th green tyre, whichever trolley it came off. Supply is
         GT opening stock (on the rack at plan start, SHELF-LIFE BOUND)
       + every GT trolley at feasible_end + min aging
  2. PRESS CAPACITY. Once a lot is pushed for GT, everything behind it on that
     press is pushed too. A press cannot cure two tyres at once.

GT OPENING STOCK PERISHES. Only what the presses can consume before
`plan start + max aging` counts; the rest is written off, exactly as on the floor.
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
from collections import defaultdict

import pandas as pd

import blocker_policy as BP
import ctpio as io
import rules as R


# ------------------------------------------------------- even curing lots
# PLANT RULE: a press cures tyres in PAIRS - two mould halves, LHS and RHS - so
# a press lot is always an EVEN number. An odd figure cannot be run: it implies
# half a press cycle. Rounding DOWN is the only safe direction; rounding up
# would cure a tyre that was never built and would break the cumulative GT gate
# above, which matches the n-th cured tyre to the n-th green one.
#
# The shortfall is kept in `qty_planned` so the rounding is visible and can be
# summed, rather than quietly vanishing from the plan.
def even_qty(q) -> float:
    try:
        v = float(q)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    return float(int(v // 2) * 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v1")
    ap.add_argument("--horizon-extend-h", type=float, default=0.0)
    args = ap.parse_args()
    OUT = io.outdir(args.run, args.line)

    print("\n" + "=" * 74)
    print(f"  CTP PHASE 7 — curing, gated on real green tyres   [{args.line.upper()}]")
    print("=" * 74)

    bom = io.load_bom(args.line)
    itypes = io.load_itemtypes(bom)
    aging = io.load_aging(args.line)
    aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)

    alias = io.machine_alias(io.load_machines())
    cure, cstats = io.load_curing_plan(args.line, alias)
    print(f"  Plan source     : {cstats['source']}")

    blots = pd.read_csv(OUT / "phase5_building_plan.csv",
                        parse_dates=["build_start", "build_end",
                                     "feasible_start", "feasible_end"])
    plan_start = blots["build_start"].min()
    horizon = blots["build_end"].max() + pd.Timedelta(hours=args.horizon_extend_h)

    # The building and curing plans come from the SAME solver run, so they
    # already share a window. Overlap is asserted, not assumed.
    ov_lo, ov_hi = max(plan_start, cure["start"].min()), min(horizon, cure["end"].max())
    if ov_hi <= ov_lo:
        print(f"  [BLOCKING] the curing plan ({cure['start'].min()} -> "
              f"{cure['end'].max()}) does not overlap the building plan "
              f"({plan_start} -> {horizon}). Curing cannot be gated.")
        return 2
    print(f"  Overlap         : {ov_lo}  ->  {ov_hi}")
    print(f"  Press lots      : {len(cure):,}   presses {cure['press'].nunique()}"
          f"   SKUs {cure['sku'].nunique()}   tyres {cure['qty'].sum():,.0f}")
    print(f"  Window          : {cure['start'].min()}  ->  {cure['end'].max()}")

    # ---- GT opening stock, and it perishes --------------------------------
    gt_stock = {}
    try:
        g = io._rd(f"gt_inventory_manual_{args.line}")
        g.columns = [str(c).strip() for c in g.columns]
        for r in g.itertuples(index=False):
            s = io._s(getattr(r, "SKUCode", ""))
            q = pd.to_numeric(getattr(r, "TotalQuantity", 0), errors="coerce")
            if s and pd.notna(q) and q > 0:
                gt_stock[s] = gt_stock.get(s, 0.0) + float(q)
        print(f"  GT opening stock: {len(gt_stock)} SKU(s), "
              f"{sum(gt_stock.values()):,.0f} green tyres on the rack")
    except FileNotFoundError:
        print(f"  GT opening stock: none (no gt_inventory_manual_{args.line})")

    gt_item = dict(zip(blots["sku"], blots["built_item"]))

    # ---- v41: EVERY CURING SKU KNOWS ITS GREEN TYRE ------------------------
    # Until v40 the SKU -> GT map came ONLY from building lots, so an SKU the
    # curing plan cures but the building plan never builds could never find
    # its own stock on the rack (e2e_v40: 92 TBR tyres on 4 small SKUs, plus
    # 233 GT of data-blocked SKUs, never used). The curing plan names the GT
    # on every row (GT_Code); it now fills the map wherever the building plan
    # has no lot. Where both name a GT and they differ, the building lot's GT
    # is kept - that is the tyre physically made - and the conflict is
    # recorded as A11.
    a11 = []
    _cgt = (cure.assign(gt_code=cure["gt_code"].map(io._s))
                .query("gt_code != ''").groupby("sku")["gt_code"].first())
    for _sku, _g in _cgt.items():
        if _sku not in gt_item:
            gt_item[_sku] = _g
        elif str(gt_item[_sku]).strip() != _g:
            a11.append({"code": "A11", "item": _sku, "department": "CURING",
                        "gap": "GT code differs: building vs curing plan",
                        "value": f"{gt_item[_sku]} (building) kept; curing plan says {_g}",
                        "source": "building lot = the tyre physically built"})
    n_from_cure = int(sum(1 for k in _cgt.index if k not in set(blots["sku"])))
    print(f"  GT per SKU      : {len(gt_item)} SKU(s) mapped; {n_from_cure} from the "
          f"curing plan's GT_Code (no building lot); {len(a11)} conflict(s) -> A11")

    # ---- v38: GT STOCK IS KEYED BY GT CODE, NOT BY SKU --------------------
    # JK_CTP_PPDS.CTP_GT_Inventory names the GREEN TYRE ("GT 1513 XPC1 MSIL"),
    # while the loop below looks stock up by finished SKU. Until v38 nothing
    # ever matched (v31-v37: 16,331 PCR / 3,957 TBR tyres loaded, 0 used). Each
    # GT's stock is now shared among the SKUs that build that GT, earliest
    # curing slot first, and only for slots before the stock expires (plan
    # start + the GT's max ageing, as before). Stock keyed by a SKU code still
    # matches that SKU directly.
    gt_alloc = defaultdict(float)
    _by_gt = {g: q for g, q in gt_stock.items() if g in set(gt_item.values())}
    # v42: ELIGIBLE DEMAND DRAWS GT STOCK FIRST. In v41 a DATA_BLOCKED SKU
    # (112222982C016MJXM0) took 56 tyres of GT 5103 that an eligible SKU sharing
    # that GT needed (-48 tyres). Blocked SKUs now get only what is left.
    try:
        _stt = pd.read_csv(OUT / BP.STATUS_FILE, dtype=str).fillna("")
        _blocked = set(_stt.loc[_stt["state"] == BP.BLOCKED, "sku"])
    except FileNotFoundError:
        _blocked = set()
    if _by_gt:
        _c = (cure.assign(gt=cure["sku"].map(gt_item),
                          _blk=cure["sku"].isin(_blocked))
                  .sort_values(["_blk", "start"], kind="stable"))
        for g, q in _by_gt.items():
            mx_g = float(aging.get(g, (0.0, 0.0))[1]) or 48.0
            exp_g = plan_start + pd.Timedelta(hours=mx_g)
            left = float(q)
            for r in _c[(_c["gt"] == g) & (_c["start"] <= exp_g)].itertuples(index=False):
                if left <= 0:
                    break
                take = min(left, float(r.qty))
                gt_alloc[r.sku] += take
                left -= take
        print(f"  GT stock by GT  : {len(_by_gt)} GT code(s) match the plan; "
              f"{sum(gt_alloc.values()):,.0f} of {sum(_by_gt.values()):,.0f} green "
              f"tyre(s) allocated to {len(gt_alloc)} SKU(s) before they expire")
    rows = []
    n_gated = 0
    for sku, grp in cure.groupby("sku", sort=False):
        grp = grp.sort_values("start")
        b = blots[(blots["sku"] == sku) & blots["feasible_end"].notna()] \
            .sort_values("feasible_end")
        mn = float(aging.get(gt_item.get(sku, ""), (0.0, 0.0))[0])
        mx = float(aging.get(gt_item.get(sku, ""), (0.0, 0.0))[1]) or 48.0
        ready = (b["feasible_end"] + pd.Timedelta(hours=mn)).to_numpy() if len(b) \
            else np.array([], dtype="datetime64[ns]")
        supply = b["qty"].cumsum().to_numpy() if len(b) else np.array([])

        inv = float(gt_stock.get(sku, 0.0)) + float(gt_alloc.get(sku, 0.0))
        if inv > 0:
            expiry = plan_start + pd.Timedelta(hours=mx)
            usable = min(inv, float(grp.loc[grp["start"] <= expiry, "qty"].sum()))
            if usable > 0:
                ready = np.concatenate([np.array([plan_start.to_datetime64()]), ready])
                supply = np.concatenate([np.array([usable]), usable + supply])

        need = grp["qty"].cumsum().to_numpy()
        if len(supply) == 0:
            gate = [pd.NaT] * len(grp)
        else:
            idx = np.searchsorted(supply, need, side="left")
            short = idx >= len(ready)
            idx = np.clip(idx, 0, len(ready) - 1)
            gate = [pd.NaT if s else pd.Timestamp(ready[i]) for s, i in zip(short, idx)]
        for r, g_ in zip(grp.itertuples(index=False), gate):
            rows.append({"sku": sku, "press": r.press, "qty": even_qty(r.qty),
                         "qty_planned": float(r.qty),
                         "planned_start": r.start, "planned_end": r.end,
                         "gt_ready": g_, "plan_setup_min": float(r.setup_min or 0)})
            if pd.notna(g_) and g_ > r.start:
                n_gated += 1

    cp = pd.DataFrame(rows)
    cp["feasible_start"] = cp[["planned_start", "gt_ready"]].max(axis=1)
    cp.loc[cp["gt_ready"].isna(), "feasible_start"] = pd.NaT

    # ---- press capacity: everything behind a pushed lot is pushed too -----
    span = (cp["planned_end"] - cp["planned_start"]).fillna(pd.Timedelta(minutes=45))
    cp["span"] = span
    cp = cp.sort_values(["press", "planned_start"], kind="stable").reset_index(drop=True)
    # ---- BR-10  PM / MTC: a press under PM cures nothing ---------------------
    # 76 blocks exist in jkplanning_CTP.pm_mtc_downtime_schedule and v14 read
    # none of them, so every press was treated as available 24x7. Six of those
    # blocks (96 press-hours) fall inside this plan's horizon. A lot is moved
    # WHOLE to the far side of the block - a press is not restarted mid-cycle
    # after a 16-hour shutdown.
    pm = io.pm_blocks(args.line)
    pm_hits, pm_h = 0, 0.0

    # ---- v40: A MOULD CHANGE TAKES TIME, EVEN WHEN THE LOT IS PUSHED --------
    # Until v39 a pushed lot started the instant the lot ahead of it ended,
    # whatever the SKU: 34 of 60 press SKU changes had a ZERO gap in e2e_v39.
    # Now the gap before a lot is
    #   * the plan's own CO_Mins + Mould_Clean_Mins on that lot's row, when the
    #     SKU changes or the row states a setup (mould clean on the same SKU);
    #   * A05 (curing.mould_change_min, per line) when the SKU changes and the
    #     plan states no setup - an ASSUMPTION, recorded in
    #     phase7_assumptions.csv and in VALIDATION_REPORT;
    #   * 0 on the same SKU with no stated setup.
    _a05 = R.rule("curing.mould_change_min", {}) or {}
    a05_min = float((_a05.get(args.line) if isinstance(_a05, dict) else _a05) or
                    (120.0 if args.line == "pcr" else 180.0))
    a05_used = []
    prev_sku: dict = {}
    co_min, co_src = [], []
    free: dict = {}
    out = []
    for r in cp.itertuples(index=False):
        s = r.feasible_start
        if pd.isna(s):
            out.append(pd.NaT)
            co_min.append(0.0)
            co_src.append("")
            continue
        p_sku = prev_sku.get(r.press)
        changed = p_sku is not None and p_sku != r.sku
        if r.plan_setup_min > 0 and p_sku is not None:
            gap, src = float(r.plan_setup_min), "plan CO_Mins+Mould_Clean_Mins"
        elif changed:
            gap, src = a05_min, "A05 assumption"
            a05_used.append({"press": r.press, "from_sku": p_sku, "to_sku": r.sku})
        else:
            gap, src = 0.0, ""
        co_min.append(gap)
        co_src.append(src)
        f = free.get(r.press)
        if f is not None and s < f + pd.Timedelta(minutes=gap):
            s = f + pd.Timedelta(minutes=gap)
        prev_sku[r.press] = r.sku
        s, pushed = io.pm_push(pm, r.press, s, r.span)      # BR-10
        if pushed > 0:
            pm_hits += 1
            pm_h += pushed
        out.append(s)
        free[r.press] = s + r.span
    cp["feasible_start"] = out
    cp["co_min"], cp["co_source"] = co_min, co_src
    _nchg = int(sum(1 for x in co_src if x))
    print(f"  Mould changes   : {_nchg:,} lot(s) carry a changeover gap "
          f"({sum(1 for x in co_src if x.startswith('plan')):,} from the plan's CO_Mins, "
          f"{len(a05_used):,} from A05 = {a05_min:.0f} min)")
    _a = pd.DataFrame([{"code": "A05", "item": f"{u['press']}:{u['from_sku']}->{u['to_sku']}",
                        "department": "CURING", "gap": "mould change time",
                        "value": f"{a05_min:.0f} min",
                        "source": "rules curing.mould_change_min (plan states no CO_Mins)"}
                       for u in a05_used],
                      columns=["code", "item", "department", "gap", "value", "source"])
    _a = pd.concat([_a, pd.DataFrame(a11, columns=_a.columns)], ignore_index=True)
    _a.to_csv(OUT / "phase7_assumptions.csv", index=False)
    if pm:
        _inhz = sum(1 for m, b in pm.items() for a, z in b
                    if z > cp["planned_start"].min() and a < horizon)
        print(f"  BR-10 PM blocks : {len(pm)} press(es) have PM, {_inhz} block(s) "
              f"inside the horizon -> {pm_hits:,} press lot(s) pushed, {pm_h:,.1f} h")
    else:
        print(f"  BR-10 PM blocks : none staged (pm_mtc_downtime_schedule absent)")
    cp["feasible_end"] = cp["feasible_start"] + cp["span"]
    cp["within_horizon"] = cp["feasible_end"].notna() & (cp["feasible_end"] <= horizon)
    cp["push_h"] = ((cp["feasible_start"] - cp["planned_start"])
                    .dt.total_seconds() / 3600.0).clip(lower=0.0)

    cured = float(cp.loc[cp["within_horizon"], "qty"].sum())
    total = float(cp["qty"].sum())
    n_nogt = int(cp["gt_ready"].isna().sum())
    print(f"\n  CURING GATE")
    print(f"    press lots gated by GT : {n_gated:,}")
    print(f"    press lots with NO GT  : {n_nogt:,} "
          f"({100*n_nogt/max(len(cp),1):.1f}%) — the building plan never made enough")
    if (cp['push_h'] > 0).any():
        p = cp.loc[cp["push_h"] > 0, "push_h"]
        print(f"    push hours             : median {p.median():.1f}  max {p.max():.1f}")
    print(f"    CURED : {cured:,.0f} / {total:,.0f} = "
          f"{100*cured/max(total,1):.1f}%   (horizon {horizon})   <- raw plan")

    # ---- v39 DATA-BLOCKER POLICY: tag every cure lot with its SKU's state ----
    # Nothing is rescheduled here. Blocked demand is taken out of the FIGURES,
    # never hidden: it stays in this file, tagged, and VALIDATION_REPORT lists it.
    try:
        st = pd.read_csv(OUT / BP.STATUS_FILE, dtype=str).fillna("")
        st = st.drop_duplicates("sku").set_index("sku")
        cp["sku_state"] = cp["sku"].map(st["state"]).fillna(BP.CLEAN)
        cp["blocker_code"] = cp["sku"].map(st["blocker_code"]).fillna("")
        cp["assumption_codes"] = cp["sku"].map(st["assumption_codes"]).fillna("")
    except FileNotFoundError:
        print(f"    [WARN] {BP.STATUS_FILE} missing - every lot treated as CLEAN")
        cp["sku_state"], cp["blocker_code"], cp["assumption_codes"] = BP.CLEAN, "", ""
    cp["in_kpi"] = cp["sku_state"].map(BP.in_kpi)
    k = cp[cp["in_kpi"]]
    e_cured = float(k.loc[k["within_horizon"], "qty"].sum())
    e_total = float(k["qty"].sum())
    print(f"    DATA-BLOCKED (excluded): {total - e_total:,.0f} tyres on "
          f"{cp.loc[~cp['in_kpi'], 'sku'].nunique()} SKU(s)")
    print(f"    CURED of ELIGIBLE      : {e_cured:,.0f} / {e_total:,.0f} = "
          f"{100*e_cured/max(e_total,1):.1f}%   <- the final figure")

    cp["day"] = cp["feasible_end"].map(
        lambda t: io.plant_day(t).date() if pd.notna(t) else None)
    cp["plan_day"] = cp["planned_start"].map(lambda t: io.plant_day(t).date())
    day = (cp.groupby("plan_day")["qty"].sum().rename("plan").to_frame()
           .join(cp[cp["within_horizon"]].groupby("day")["qty"].sum().rename("cured"))
           .fillna(0.0))
    day["pct"] = (100 * day["cured"] / day["plan"].replace(0, pd.NA)).round(1)
    print(f"\n  DAY-WISE CURED")
    print(f"    {'day':12s}{'plan':>10s}{'cured':>10s}{'%':>8s}")
    for d, r in day.iterrows():
        print(f"    {str(d):12s}{r['plan']:>10,.0f}{r['cured']:>10,.0f}"
              f"{(r['pct'] if pd.notna(r['pct']) else 0):>7.1f}%")

    cp = io.with_machine_name(cp, "press", "press_name")   # v36: plant names
    cp.drop(columns=["span"]).to_csv(OUT / "phase7_curing_plan.csv", index=False)
    day.to_csv(OUT / "phase7_daywise_cured.csv")
    cp[cp["push_h"] > 0].drop(columns=["span"]).to_csv(OUT / "phase7_revisions.csv",
                                                       index=False)
    io.write_json(OUT / "phase7_stats.json",
                  {"press_lots": int(len(cp)), "tyres_plan": total,
                   "tyres_cured": cured, "cured_pct": 100 * cured / max(total, 1),
                   "lots_no_gt": n_nogt})
    print(f"\n[OK] phase7_curing_plan.csv ({len(cp):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
