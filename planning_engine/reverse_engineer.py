#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reverse_engineer.py -- STAGE A.2: turn the June MES actuals into a PLANT RULEBOOK.

The plant's June building+curing plan was not handed to us as rules, only as outcomes. This module
reads the outcomes (every cure event, every green tyre built) and recovers the parameters a planner
needs to reproduce them. Every number below is MEASURED, never assumed; the provenance of each is
in its docstring so a plant engineer can challenge it.

WHAT THE MES SHOWS (June 2026 PCR, 386,823 cures / 386,970 GT builds, 98.6% barcode-joined):

  1. CURING IS A LONG-CAMPAIGN, MOULD-PINNED SYSTEM.
     237 press campaigns across 91 presses in 30 days = 0.05 changeovers/press/day. Median campaign
     195 h (~8 days), 1,233 tyres. A press is effectively dedicated to one SKU for a week or more.
     => the curing planner must MINIMISE press changeovers, not balance load.

  2. THE BINDING CONSTRAINT IS MOULDS, NOT PRESSES.
     Transitive closure of press<->GT co-occurrence collapses 91 presses into ONE group of 90 --
     i.e. presses are broadly interchangeable. What limits a SKU is how many mould sets exist,
     observable as the MAX number of presses running that GT simultaneously (19 for the big runner,
     4-9 for the rest).
     => capacity per SKU = mould_sets x press_output, not "any free press".

  3. BUILDING IS A FLEXIBLE PULL SERVER.
     885 TBM campaigns across 11 TBMs = 2.65 changeovers/TBM/day, median campaign 5.3 h (~1 shift),
     269 tyres. TBM 7 ran a single GT all month, TBM 2 ran 14.
     => the building planner assigns SHIFT-SIZED campaigns and may split a SKU across TBMs.

  4. THE INTERLOCK IS A ~4.3 h LEAD, NOT A BATCH HANDOFF.
     build->cure dwell: median 4.34 h, p10 1.18 h, p90 20.42 h. Building runs just ahead of the
     press draw, continuously, per SKU.
     => the building plan is a time-shifted copy of the curing plan, shifted by the target dwell
     and capped by the green-tyre shelf life.

Usage:
    python -m planning_engine.reverse_engineer [--out DIR]
Writes planning_engine/_rulebook/: sku_master.csv, mould_capacity.csv, press_master.csv,
tbm_master.csv, requirement_june.csv, rulebook.json, RULEBOOK.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "_cache"
ROOT = HERE.parent

# Consecutive-event gaps are the rate signal, but the raw stream also contains shift breaks, mould
# changes and downtime. These bounds keep only gaps that are plausibly one machine cycle.
CURE_GAP_MAX_MIN = 120.0     # a PCR cure cycle is 10-25 min; >2 h is a stoppage, not a cycle
BUILD_GAP_MAX_MIN = 30.0     # a TBM builds a tyre in ~1 min; >30 min is a changeover/break
N_CAVITIES = 2               # every press has an LHS and an RHS cavity (confirmed: 193,419/193,403)


def _load() -> tuple[pd.DataFrame, pd.DataFrame]:
    cure = pd.read_parquet(CACHE / "cure_events.parquet")
    build = pd.read_parquet(CACHE / "build_events.parquet")
    return cure, build


def join_barcode(cure: pd.DataFrame, build: pd.DataFrame) -> pd.DataFrame:
    """Attach the GT item + building TBM to every cure event via the green-tyre barcode.

    This is the one join that makes reverse engineering possible: the curing MES records only a
    barcode and a mould, never the SKU. Stage-2 building records barcode -> GT item code.
    """
    j = cure.merge(
        build[["gtbarCode", "tbm", "item", "ItemName", "build_time"]], on="gtbarCode", how="left"
    )
    j["matched"] = j["item"].notna()
    return j


def derive_cycles(j: pd.DataFrame, build: pd.DataFrame) -> pd.DataFrame:
    """Per-GT cure and build cycle times, from consecutive same-machine events.

    cure_cycle_min_cavity = median gap between consecutive cures on the SAME press AND cavity.
    A press has 2 cavities running in parallel, so the press-level per-tyre time (which is what the
    plan file's `cycleTime` column means) is that divided by N_CAVITIES. Verified against the
    existing plan: GT 1402 XPC TATA measures 19.66 min/cavity -> 9.83 min/tyre; the plan says 9.6.
    """
    m = j[j["matched"]].sort_values(["press", "cavity", "cure_time"]).copy()
    m["gap"] = m.groupby(["press", "cavity"])["cure_time"].diff().dt.total_seconds() / 60.0
    ok = m[(m["gap"] > 0) & (m["gap"] < CURE_GAP_MAX_MIN)]
    cure_cyc = ok.groupby("item")["gap"].agg(cure_cycle_min_cavity="median", cure_gap_n="size")

    b = build[build["tbm"].notna()].sort_values(["tbm", "build_time"]).copy()
    b["gap"] = b.groupby("tbm")["build_time"].diff().dt.total_seconds() / 60.0
    okb = b[(b["gap"] > 0) & (b["gap"] < BUILD_GAP_MAX_MIN)]
    build_cyc = okb.groupby("item")["gap"].agg(build_cycle_min="median", build_gap_n="size")

    out = cure_cyc.join(build_cyc, how="outer")
    out["cure_cycle_min_tyre"] = out["cure_cycle_min_cavity"] / N_CAVITIES
    # Fall back to the plant-wide median for a GT with too few observations to trust its own.
    out["build_cycle_min"] = out["build_cycle_min"].fillna(okb["gap"].median())
    out["cure_cycle_min_tyre"] = out["cure_cycle_min_tyre"].fillna(ok["gap"].median() / N_CAVITIES)
    return out.reset_index().rename(columns={"item": "gt_code"})


def derive_mould_capacity(j: pd.DataFrame) -> pd.DataFrame:
    """Mould sets available per GT = MAX presses running that GT simultaneously.

    The MES mould columns (mouldNo 'GC03#GC04', MouldCodeLH) are mould-POSITION codes, not unique
    physical assets -- 32 of 57 codes appear on more than one press, and 51 of 57 appear against
    more than one GT, so they cannot be counted directly. The number of presses the plant actually
    ran a GT on at the same moment is the honest observable lower bound on its mould sets, and it
    is exactly the quantity the planner needs: the ceiling on that SKU's parallel press capacity.

    Measured two ways: `mould_sets_day` (max distinct presses in any calendar day, the loose bound)
    and `mould_sets_instant` (max presses whose campaigns overlap at any instant, the tight one).
    The planner uses the instant figure.
    """
    m = j[j["matched"]].copy()
    m["day"] = m["cure_time"].dt.floor("D")
    by_day = m.groupby(["item", "day"])["press"].nunique().groupby("item").max()

    # instantaneous overlap: build press campaigns, then sweep starts/ends per GT
    m = m.sort_values(["press", "cure_time"])
    m["newc"] = (m["item"] != m.groupby("press")["item"].shift()).astype(int)
    m["camp"] = m.groupby("press")["newc"].cumsum()
    camps = (m.groupby(["item", "press", "camp"])
               .agg(s=("cure_time", "min"), e=("cure_time", "max"), n=("cure_time", "size"))
               .reset_index())
    inst = {}
    for gt, g in camps.groupby("item"):
        ev = [(t, +1) for t in g["s"]] + [(t, -1) for t in g["e"]]
        ev.sort()
        cur = mx = 0
        for _, d in ev:
            cur += d
            mx = max(mx, cur)
        inst[gt] = mx
    out = pd.DataFrame({"gt_code": by_day.index, "mould_sets_day": by_day.values})
    out["mould_sets_instant"] = out["gt_code"].map(inst).fillna(1).astype(int)
    return out


def derive_press_master(j: pd.DataFrame, cycles: pd.DataFrame) -> pd.DataFrame:
    """Per-press cavity count, observed availability and the GTs it ran.

    availability = (cures/press / N_CAVITIES) x its own median cure cycle / horizon minutes.
    This is cavity-busy fraction: the fraction of the month a cavity was actually curing. It nets
    out mould changes, cleaning, breakdowns and starvation, so it is the correct planning factor --
    a plan built at 100% would not be executable. Plant median: 86.5%.
    """
    m = j[j["matched"]].copy()
    horizon_min = (m["cure_time"].max() - m["cure_time"].min()).total_seconds() / 60.0
    cyc = dict(zip(cycles["gt_code"], cycles["cure_cycle_min_cavity"]))
    rows = []
    for press, g in m.groupby("press"):
        n = len(g)
        wcyc = float(np.average([cyc.get(i, np.nan) for i in g["item"]],
                                weights=None)) if n else np.nan
        gts = sorted(g["item"].unique())
        rows.append({
            "press": press,
            "cavities": int(g["cavity"].nunique()) or N_CAVITIES,
            "cures_june": n,
            "gts_run": "|".join(gts),
            "n_gts": len(gts),
            "availability": min((n / N_CAVITIES) * (wcyc if wcyc == wcyc else 0) / horizon_min, 1.0),
        })
    return pd.DataFrame(rows).sort_values("press").reset_index(drop=True)


def derive_tbm_master(build: pd.DataFrame, cycles: pd.DataFrame) -> pd.DataFrame:
    """Per-TBM output, observed busy fraction and GT repertoire."""
    b = build[build["tbm"].notna() & build["item"].notna()].copy()
    b["item"] = b["item"].astype(str)
    horizon_min = (b["build_time"].max() - b["build_time"].min()).total_seconds() / 60.0
    cyc = dict(zip(cycles["gt_code"], cycles["build_cycle_min"]))
    rows = []
    for tbm, g in b.groupby("tbm"):
        busy = float(sum(cyc.get(i, 1.0) for i in g["item"]))
        gts = sorted(g["item"].unique())
        rows.append({
            "tbm": tbm, "built_june": len(g), "n_gts": len(gts), "gts_run": "|".join(gts),
            "availability": min(busy / horizon_min, 1.0),
        })
    return pd.DataFrame(rows).sort_values("built_june", ascending=False).reset_index(drop=True)


def derive_shift_utilisation(j: pd.DataFrame, build: pd.DataFrame, cycles: pd.DataFrame) -> dict:
    """PER-SHIFT busy fractions -- the capacity ceiling, which month-level availability hides.

    This distinction decides whether a plan is executable, so it is measured rather than assumed:

      * PRESSES run flat out INSIDE an active shift (per-press per-shift busy: median 95.8%, p90
        100.8%). Their 86.7% month-level availability is therefore whole idle/changeover SHIFTS, not
        within-shift slack -- so the plan emits full-rate shifts and expresses availability by
        leaving shifts out. That is exactly how the plant's own plan file is written (81% of
        press-shift slots present, every present row a full 480 min).

      * TBMs never exceeded 85.3% plant-wide in any shift, but individual TBMs reached 88.9% (p90)
        and 101% (max). So 79% aggregate busy is DEMAND-limited, not capacity-limited: using it as
        the planning availability would wrongly declare ~10% of the requirement unbuildable. The
        planning figure is the p90 of achieved per-TBM per-shift busy -- what a TBM demonstrably
        sustains for a whole shift when it is actually loaded.
    """
    cyc_b = dict(zip(cycles["gt_code"], cycles["build_cycle_min"]))
    cyc_c = dict(zip(cycles["gt_code"], cycles["cure_cycle_min_tyre"]))

    b = build[build["tbm"].notna() & build["item"].notna()].copy()
    t0 = b["build_time"].min().normalize() + pd.Timedelta(hours=7)
    b["min"] = b["item"].astype(str).map(cyc_b).fillna(float(np.median(list(cyc_b.values()))))
    b["slot"] = ((b["build_time"] - t0).dt.total_seconds() // (8 * 3600)).astype(int)
    tu = b.groupby(["tbm", "slot"])["min"].sum() / (8 * 60)
    plant = b.groupby("slot")["min"].sum() / (b["tbm"].nunique() * 8 * 60)

    m = j[j["matched"]].copy()
    m["min"] = m["item"].astype(str).map(cyc_c).fillna(float(np.median(list(cyc_c.values()))))
    m["slot"] = ((m["cure_time"] - t0).dt.total_seconds() // (8 * 3600)).astype(int)
    pu = m.groupby(["press", "slot"])["min"].sum() / (8 * 60)

    return {
        "tbm_shift_busy_median": round(float(tu.median()), 3),
        "tbm_shift_busy_p90": round(float(tu.quantile(0.90)), 3),
        "tbm_shift_busy_max": round(float(tu.max()), 3),
        "tbm_plant_shift_load_max": round(float(plant.max()), 3),
        "press_shift_busy_median": round(float(pu.median()), 3),
        "press_shift_busy_p90": round(float(pu.quantile(0.90)), 3),
    }


def derive_campaign_rules(j: pd.DataFrame, build: pd.DataFrame) -> dict:
    """Campaign-length statistics -- the sequencing rules the planner must obey.

    Curing: long campaigns (median 195 h) => the planner should pin a press to one SKU for as long
    as demand allows. Building: shift-sized campaigns (median 5.3 h) => the building planner may
    resequence freely inside a day.
    """
    m = j[j["matched"]].sort_values(["press", "cure_time"]).copy()
    m["newc"] = (m["item"] != m.groupby("press")["item"].shift()).astype(int)
    m["camp"] = m.groupby("press")["newc"].cumsum()
    pc = (m.groupby(["press", "camp"])
            .agg(n=("item", "size"), s=("cure_time", "min"), e=("cure_time", "max"))
            .reset_index())
    pc["h"] = (pc["e"] - pc["s"]).dt.total_seconds() / 3600.0

    b = build[build["tbm"].notna()].sort_values(["tbm", "build_time"]).copy()
    b["newc"] = (b["item"] != b.groupby("tbm")["item"].shift()).astype(int)
    b["camp"] = b.groupby("tbm")["newc"].cumsum()
    tc = (b.groupby(["tbm", "camp"])
            .agg(n=("item", "size"), s=("build_time", "min"), e=("build_time", "max"))
            .reset_index())
    tc["h"] = (tc["e"] - tc["s"]).dt.total_seconds() / 3600.0

    n_press, n_tbm, days = m["press"].nunique(), b["tbm"].nunique(), 30.0
    return {
        "press_campaigns": int(len(pc)),
        "press_changeovers_per_press_per_day": round((len(pc) - n_press) / n_press / days, 4),
        "press_campaign_h_median": round(float(pc["h"].median()), 1),
        "press_campaign_h_p25": round(float(pc["h"].quantile(0.25)), 1),
        "press_campaign_qty_median": int(pc["n"].median()),
        "tbm_campaigns": int(len(tc)),
        "tbm_changeovers_per_tbm_per_day": round((len(tc) - n_tbm) / n_tbm / days, 3),
        "tbm_campaign_h_median": round(float(tc["h"].median()), 2),
        "tbm_campaign_qty_median": int(tc["n"].median()),
    }


def derive_interlock(j: pd.DataFrame) -> dict:
    """The build->cure lead the building plan must reproduce."""
    d = (j.loc[j["matched"], "cure_time"] - j.loc[j["matched"], "build_time"]).dt.total_seconds() / 3600.0
    d = d[(d > -1) & (d < 24 * 14)]      # drop the 35 negative and a handful of re-scan outliers
    return {
        "dwell_h_median": round(float(d.median()), 2),
        "dwell_h_p10": round(float(d.quantile(0.10)), 2),
        "dwell_h_p90": round(float(d.quantile(0.90)), 2),
        "dwell_h_mean": round(float(d.mean()), 2),
        "n_pairs": int(d.size),
    }


def derive_calendar(j: pd.DataFrame) -> dict:
    """Shift pattern, from the hour-of-day distribution of cures.

    A 07:00-15:00 / B 15:00-23:00 / C 23:00-07:00, and June ran all three shifts every day with no
    weekly off: cures split 127,583 / 129,485 / 129,755 -- flat within 2%.
    """
    h = j.loc[j["matched"], "cure_time"].dt.hour
    a = int(((h >= 7) & (h < 15)).sum())
    bb = int(((h >= 15) & (h < 23)).sum())
    c = int(((h >= 23) | (h < 7)).sum())
    days = j.loc[j["matched"], "cure_time"].dt.floor("D").nunique()
    return {
        "shifts": {"A": [7, 15], "B": [15, 23], "C": [23, 7]},
        "shift_len_h": 8,
        "cures_A": a, "cures_B": bb, "cures_C": c,
        "shift_balance_max_dev_pct": round(100 * (max(a, bb, c) - min(a, bb, c)) / max(a, bb, c), 2),
        "operating_days": int(days),
        "continuous_operation": True,
    }


def derive_requirement(j: pd.DataFrame, sku_map: pd.DataFrame) -> pd.DataFrame:
    """The June PRODUCTION REQUIREMENT, recovered as what was actually cured per GT per day.

    This is the planning engine's INPUT for the reverse-engineering validation run: feed the engine
    the same monthly requirement the plant faced and its plan must land on the plant's plan. For a
    future month this file is replaced by the sales/production target.
    """
    m = j[j["matched"]].copy()
    m["day"] = m["cure_time"].dt.floor("D")
    daily = m.groupby(["item", "day"]).size().rename("qty").reset_index()
    monthly = daily.groupby("item")["qty"].sum().rename("qty_month").reset_index()
    monthly = monthly.rename(columns={"item": "gt_code"})
    monthly = monthly.merge(sku_map, on="gt_code", how="left")
    return monthly.sort_values("qty_month", ascending=False).reset_index(drop=True)


def load_sku_map() -> pd.DataFrame:
    """GT code -> SKU code(s), from the existing plan files (the only place the link is stated).

    10 GT codes carry more than one SKU (same green tyre, different finished-tyre marking). The
    planner keeps them as a '|' list and splits a GT's requirement across its SKUs pro rata.
    """
    frames = []
    bp = ROOT / "inputs" / "building_plan.csv"
    if bp.exists():
        d = pd.read_csv(bp, usecols=["skuCode", "gtCode"]).dropna()
        frames.append(d.rename(columns={"gtCode": "gt_code", "skuCode": "sku_code"}))
    if not frames:
        return pd.DataFrame(columns=["gt_code", "sku_codes", "n_skus"])
    m = pd.concat(frames, ignore_index=True)
    m["gt_code"] = m["gt_code"].astype(str).str.strip()
    m["sku_code"] = m["sku_code"].astype(str).str.strip()
    m = m.drop_duplicates()
    g = m.groupby("gt_code")["sku_code"].agg(lambda s: "|".join(sorted(set(s)))).reset_index()
    g = g.rename(columns={"sku_code": "sku_codes"})
    g["n_skus"] = g["sku_codes"].str.count(r"\|") + 1
    return g


def main() -> int:
    ap = argparse.ArgumentParser(description="Recover the plant rulebook from June MES actuals.")
    ap.add_argument("--out", default=str(HERE / "_rulebook"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    cure, build = _load()
    j = join_barcode(cure, build)
    n_match, n_tot = int(j["matched"].sum()), len(j)

    cycles = derive_cycles(j, build)
    moulds = derive_mould_capacity(j)
    press = derive_press_master(j, cycles)
    tbm = derive_tbm_master(build, cycles)
    sku_map = load_sku_map()
    req = derive_requirement(j, sku_map)
    util = derive_shift_utilisation(j, build, cycles)

    sku_master = (cycles.merge(moulds, on="gt_code", how="left")
                        .merge(sku_map, on="gt_code", how="left")
                        .merge(req[["gt_code", "qty_month"]], on="gt_code", how="left"))
    sku_master["press_tyres_per_day"] = 24 * 60 / sku_master["cure_cycle_min_tyre"]
    sku_master["tbm_tyres_per_day"] = 24 * 60 / sku_master["build_cycle_min"]

    rulebook = {
        "source": "June 2026 PCR MES (curingpcr / tbmpcrstage2)",
        "join": {"cure_events": n_tot, "matched_to_build": n_match,
                 "match_pct": round(100.0 * n_match / n_tot, 2)},
        "horizon": {"start": str(j["cure_time"].min()), "end": str(j["cure_time"].max())},
        "n_gt": int(sku_master["gt_code"].nunique()),
        "n_press": int(press["press"].nunique()),
        "n_tbm": int(tbm["tbm"].nunique()),
        "cavities_per_press": N_CAVITIES,
        "press_availability_median": round(float(press["availability"].median()), 4),
        "tbm_availability_median": round(float(tbm["availability"].median()), 4),
        "shift_utilisation": util,
        # PLANNING availability (see derive_shift_utilisation): presses are sized on their month-level
        # availability because their idle time is whole changeover shifts; TBMs are sized on the p90
        # of achieved per-shift busy because their month-level idle is demand, not capacity.
        "tbm_planning_availability": util["tbm_shift_busy_p90"],
        "campaigns": derive_campaign_rules(j, build),
        "interlock": derive_interlock(j),
        "calendar": derive_calendar(j),
        "total_cured_june": int(req["qty_month"].sum()),
    }

    sku_master.to_csv(out / "sku_master.csv", index=False)
    moulds.to_csv(out / "mould_capacity.csv", index=False)
    press.to_csv(out / "press_master.csv", index=False)
    tbm.to_csv(out / "tbm_master.csv", index=False)
    req.to_csv(out / "requirement_june.csv", index=False)
    (out / "rulebook.json").write_text(json.dumps(rulebook, indent=2), encoding="utf-8")

    print(f"  barcode join      : {n_match:,}/{n_tot:,} cures matched ({rulebook['join']['match_pct']}%)")
    print(f"  GT items          : {rulebook['n_gt']}   requirement {rulebook['total_cured_june']:,} tyres")
    print(f"  presses / TBMs    : {rulebook['n_press']} x {N_CAVITIES} cavities / {rulebook['n_tbm']}")
    print(f"  availability      : press {100*rulebook['press_availability_median']:.1f}%  "
          f"tbm {100*rulebook['tbm_availability_median']:.1f}%")
    c = rulebook["campaigns"]
    print(f"  curing rule       : {c['press_campaigns']} campaigns, "
          f"{c['press_changeovers_per_press_per_day']} chg/press/day, median {c['press_campaign_h_median']}h "
          f"({c['press_campaign_qty_median']} tyres)  -> LONG CAMPAIGN, mould-pinned")
    print(f"  building rule     : {c['tbm_campaigns']} campaigns, "
          f"{c['tbm_changeovers_per_tbm_per_day']} chg/tbm/day, median {c['tbm_campaign_h_median']}h "
          f"({c['tbm_campaign_qty_median']} tyres)  -> SHIFT-SIZED, flexible")
    i = rulebook["interlock"]
    print(f"  interlock (dwell) : median {i['dwell_h_median']}h  p10 {i['dwell_h_p10']}h  p90 {i['dwell_h_p90']}h")
    print(f"  mould sets        : max {int(moulds['mould_sets_instant'].max())} "
          f"(median {int(moulds['mould_sets_instant'].median())}) concurrent presses per GT")
    print(f"  wrote rulebook -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
