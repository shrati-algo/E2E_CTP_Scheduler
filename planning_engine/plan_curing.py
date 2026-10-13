#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plan_curing.py -- STAGE B.1: generate the CURING plan (the anchor) from a production requirement.

Curing is the anchor: the press schedule fixes what the whole plant must feed. This module turns a
monthly requirement (SKU -> tyres) into `plan.csv`, the per-press-per-shift press schedule the
scheduler consumes.

THE PROBLEM, as the MES states it (see reverse_engineer.py for the measurements):
  * 86.0 press-months of work must fit on 91 presses -> 94.5% loaded. Tight.
  * Giving every GT its own whole presses needs 112 presses. The 26-press gap between 112 and 86 is
    fractional-demand waste, and it is bigger than the 5-press slack. So the planner MUST pack
    several GTs onto a shared press -- which is precisely why the plant ran 146 changeovers.
  * A changeover costs a shift (measured gap between press campaigns: median 7.2 h), so packing is
    not free and campaigns must stay long (plant median 195 h).
  * A GT can never run on more presses at once than it has mould sets (max concurrent presses
    observed in June: 19 for the big runner, 2 median).

THE ALGORITHM (three steps, each mirroring an observed plant behaviour):
  1. SIZE     -- press-months per GT = qty / (horizon x availability / cure_cycle). Split into a
                 whole-press part (dedicated all month, zero changeovers) and a fractional remainder.
  2. PACK     -- first-fit-decreasing the fractional remainders onto shared presses, so each shared
                 press carries a few long campaigns instead of many short ones. Enforces the mould
                 cap and a minimum campaign length.
  3. SEQUENCE -- lay campaigns out on each press across the month, insert a one-shift changeover
                 between them, and spread the idle shifts evenly (the plant's 86.7% availability
                 appears in the plan as absent press-shift rows, not as reduced per-shift qty).

Output schema is byte-compatible with the scheduler's existing inputs/plan.csv:
    plan_id, skuCode, date, pressNo, shift, startTime, endTime, qty, cycleTime, remarks
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field

import pandas as pd

SHIFTS = [("A", 7), ("B", 15), ("C", 23)]     # name -> start hour; each 8 h, C wraps past midnight
SHIFT_MIN = 480


@dataclass
class Campaign:
    """One uninterrupted run of a SKU on one press."""
    press: str
    sku: str
    gt: str
    cycle_min: float          # minutes per tyre for the PRESS (both cavities)
    qty: float                # tyres this campaign must deliver
    shift_first: int = 0      # index into the month's shift grid
    shift_last: int = 0
    shifts_used: list[int] = field(default_factory=list)


def _shift_grid(start_date: pd.Timestamp, days: int) -> list[tuple[pd.Timestamp, pd.Timestamp, str, pd.Timestamp]]:
    """The month as a flat list of (start, end, shift_name, calendar_date) slots.

    Shift C starts at 23:00 and runs into the next day; its `date` stays the day it started, which
    is how the plant's own plan files date it.
    """
    grid = []
    for d in range(days):
        day = start_date + pd.Timedelta(days=d)
        for name, hour in SHIFTS:
            s = day.normalize() + pd.Timedelta(hours=hour)
            grid.append((s, s + pd.Timedelta(hours=8), name, day.normalize()))
    return grid


def size_presses(req: pd.DataFrame, availability: float, horizon_min: float) -> pd.DataFrame:
    """Step 1 -- how many presses each SKU needs, split whole + fractional.

    `tyres_per_press_month` derates by the measured availability (86.7%), so the plan is executable
    rather than nominal. The mould cap is applied here: a SKU that needs more presses than it has
    mould sets is capacity-infeasible and is flagged, never silently over-allocated.
    """
    r = req.copy()
    r["tyres_per_press_month"] = horizon_min * availability / r["cure_cycle_min_tyre"]
    r["press_months"] = r["qty_month"] / r["tyres_per_press_month"]
    r["n_full"] = r["press_months"].apply(math.floor).astype(int)
    r["frac"] = r["press_months"] - r["n_full"]
    r["mould_cap"] = r["mould_sets_instant"].fillna(99).astype(int)
    r["presses_wanted"] = r["n_full"] + (r["frac"] > 1e-9).astype(int)
    r["mould_infeasible"] = r["presses_wanted"] > r["mould_cap"]
    # A mould-capped SKU must run its presses harder; it cannot spread wider. Fold the excess back
    # into the allowed presses so the requirement is still met (and flag it -- it means overtime).
    r.loc[r["mould_infeasible"], "n_full"] = r.loc[r["mould_infeasible"], "mould_cap"]
    r.loc[r["mould_infeasible"], "frac"] = 0.0
    return r


def pack_presses(sized: pd.DataFrame, presses: list[str], press_pref: dict[str, set[str]],
                 min_campaign_frac: float) -> list[Campaign]:
    """Steps 2 + 3 -- assign SKUs to physical presses and cut them into campaigns.

    Dedicated presses first (a whole-press SKU gets presses to itself for the month: zero
    changeovers, which is what the plant's 0.05 chg/press/day really means). Then the fractional
    remainders are first-fit-decreasing packed onto the presses left over, largest first, so the
    few shared presses carry long campaigns rather than many short ones.

    `press_pref` maps SKU -> presses that physically ran it in June. Preferring those keeps the
    plan mountable (the mould fits) and makes the reverse-engineering comparison meaningful.
    """
    free = list(presses)
    used_by: dict[str, list[tuple[str, float]]] = {}   # press -> [(sku, press-month share)]

    def take(sku: str, n: int) -> list[str]:
        """Pull n presses for `sku`, preferring ones that ran it in June."""
        pref = [p for p in free if p in press_pref.get(sku, set())]
        picked = pref[:n]
        if len(picked) < n:
            picked += [p for p in free if p not in picked][: n - len(picked)]
        for p in picked:
            free.remove(p)
        return picked

    sized = sized.sort_values("press_months", ascending=False).reset_index(drop=True)

    # --- dedicated whole presses -------------------------------------------------------------
    for _, row in sized.iterrows():
        n = int(row["n_full"])
        if n <= 0:
            continue
        for p in take(row["sku_code"], n):
            used_by.setdefault(p, []).append((row["sku_code"], 1.0))

    # --- fractional remainders, first-fit-decreasing onto shared presses ----------------------
    fracs = (sized[sized["frac"] > 1e-9][["sku_code", "frac"]]
             .sort_values("frac", ascending=False).values.tolist())
    shared: dict[str, float] = {}
    for sku, f in fracs:
        f = float(f)
        host = None
        for p, load in shared.items():
            # a campaign must stay long enough to be worth a mould change, and the press must have
            # room once the changeover shift is paid for
            if load + f <= 1.0 - _co_share() and f >= min_campaign_frac:
                host = p
                break
        if host is None:
            if free:
                host = take(sku, 1)[0]
                shared[host] = 0.0
            else:
                # No press left: put it on the least-loaded shared press. The plan will show this
                # press over 100% -- a real capacity signal, not something to hide.
                host = min(shared, key=shared.get) if shared else None
                if host is None:
                    raise RuntimeError("no presses available at all")
        shared[host] = shared.get(host, 0.0) + f
        used_by.setdefault(host, []).append((sku, f))

    camps: list[Campaign] = []
    meta = sized.set_index("sku_code")
    for press, items in used_by.items():
        for sku, share in items:
            row = meta.loc[sku]
            camps.append(Campaign(press=press, sku=sku, gt=str(row["gt_code"]),
                                  cycle_min=float(row["cure_cycle_min_tyre"]),
                                  qty=float(row["qty_month"]) * share / max(
                                      float(row["press_months"]), 1e-9)))
    return camps


def _co_share() -> float:
    """A changeover costs one shift out of a 90-shift month."""
    return 1.0 / 90.0


def sequence_and_emit(camps: list[Campaign], start_date: pd.Timestamp, days: int,
                      plan_id: str, gt_desc: dict[str, str]) -> pd.DataFrame:
    """Lay the campaigns out on the shift grid and emit plan.csv rows.

    Per press: campaigns run back to back in demand order, each preceded by a one-shift changeover
    (except the first). Within a campaign, only as many shifts as the qty needs are emitted and the
    idle shifts are spread evenly through the window -- so a press that is 87% loaded shows 87% of
    its shifts present, which is exactly how the plant's own plan encodes availability.
    """
    grid = _shift_grid(start_date, days)
    n_slots = len(grid)
    rows = []
    by_press: dict[str, list[Campaign]] = {}
    for c in camps:
        by_press.setdefault(c.press, []).append(c)

    # Presses are walked in a stable order and their idle-shift patterns are offset ROUND-ROBIN
    # across that order. A pseudo-random offset per press (crc32) decorrelates on average but still
    # leaves shift-to-shift variance, and downstream that variance is what the TBMs have to absorb:
    # the building plan's peak shift ran 63% over capacity purely from press-idle clustering. A
    # round-robin offset makes the number of presses idle in each shift as near equal as arithmetic
    # allows, which is the flattest press draw the requirement admits.
    press_order = sorted(by_press)
    n_press = max(len(press_order), 1)
    for press_idx, press in enumerate(press_order):
        cs = by_press[press]
        cs.sort(key=lambda c: -c.qty)
        # split the month's slots between campaigns proportionally to their qty, paying one slot of
        # changeover per campaign after the first
        total_q = sum(c.qty for c in cs) or 1.0
        co_slots = len(cs) - 1
        usable = n_slots - co_slots
        cursor = 0
        for i, c in enumerate(cs):
            if i > 0:
                cursor += 1                       # the changeover shift: no row emitted
            window = int(round(usable * c.qty / total_q)) if len(cs) > 1 else usable
            window = max(window, 1)
            lo, hi = cursor, min(cursor + window, n_slots)
            cursor = hi
            per_shift = SHIFT_MIN / c.cycle_min
            need = max(int(math.ceil(c.qty / per_shift)), 1)
            span = max(hi - lo, 1)
            need = min(need, span)
            # Choose which shifts a press IDLES, rather than which it works, and stagger the choice
            # per press. Selecting worked shifts directly (Bresenham on `need`) re-synchronises every
            # press onto the same idle slots, and the plant's daily output then oscillates 10.4k /
            # 14.3k instead of holding the flat ~12.9k the MES shows. Spreading the `span - need`
            # idle shifts evenly and rotating that pattern by a per-press phase decorrelates them,
            # which flattens the day curve without changing any campaign's total.
            n_idle = span - need
            if n_idle <= 0:
                slots = list(range(lo, lo + span))
            else:
                phase = int(press_idx * span / n_press) + i
                skip = {(int(m * span / n_idle) + phase) % span for m in range(n_idle)}
                slots = [lo + k for k in range(span) if k not in skip]
            left = c.qty
            for s_i in slots:
                if s_i >= n_slots or left <= 0:
                    break
                st, en, sh, day = grid[s_i]
                q = min(per_shift, left)
                left -= q
                rows.append({
                    "plan_id": plan_id,
                    "skuCode": c.sku,
                    "date": day.strftime("%Y-%m-%d"),
                    "pressNo": press,
                    "shift": sh,
                    "startTime": st.strftime("%Y-%m-%d %H:%M:%S"),
                    "endTime": en.strftime("%Y-%m-%d %H:%M:%S"),
                    "qty": int(round(q)),
                    "cycleTime": round(c.cycle_min, 3),
                    "remarks": gt_desc.get(c.gt, c.gt),
                })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["startTime", "pressNo"]).reset_index(drop=True)


def build_curing_plan(req: pd.DataFrame, press_master: pd.DataFrame, rulebook: dict,
                      start_date: pd.Timestamp, days: int = 30,
                      plan_id: str = "CTP_PCR") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full curing plan. Returns (plan_rows, sizing_report)."""
    availability = float(rulebook["press_availability_median"])
    horizon_min = days * 24 * 60.0
    sized = size_presses(req, availability, horizon_min)

    presses = list(press_master["press"].astype(str))
    # SKU -> presses that ran its GT in June (mould-fit preference)
    press_pref: dict[str, set[str]] = {}
    gt2press: dict[str, set[str]] = {}
    for _, r in press_master.iterrows():
        for gt in str(r["gts_run"]).split("|"):
            gt2press.setdefault(gt, set()).add(str(r["press"]))
    for _, r in req.iterrows():
        press_pref[r["sku_code"]] = gt2press.get(str(r["gt_code"]), set())

    min_campaign_frac = float(rulebook["campaigns"]["press_campaign_h_p25"]) / (days * 24.0)
    camps = pack_presses(sized, presses, press_pref, min_campaign_frac)
    gt_desc = dict(zip(req["gt_code"], req["gt_code"]))
    plan = sequence_and_emit(camps, start_date, days, plan_id, gt_desc)
    return plan, sized
