#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plan_building.py -- STAGE B.2: generate the BUILDING plan by PULLING from the curing plan.

The building plan is not an independent schedule. In June the plant ran building as a pull server
sitting ~4.3 h in front of the presses (measured dwell: median 4.34 h, p10 1.18, p90 20.42), with
shift-sized campaigns (median 5.28 h, 2.65 changeovers/TBM/day) and free splitting of a SKU across
TBMs. This module reproduces that: it takes the press draw curve the curing plan implies, shifts it
back by the target dwell, and packs the result onto the TBMs.

WHY PULL AND NOT PUSH -- this is the whole point of the exercise:
  A green tyre has a ~24 h shelf. Build it early and it ages out; build it late and the press
  starves. The plan therefore has exactly one correct shape: a time-shifted copy of the curing plan.
  Any building plan generated independently of the press schedule will be wrong in one of those two
  directions, and the scheduler downstream will inherit the error as either aging or slide.

CAPACITY REALITY (measured): 11.08 TBM-months of work for 11 TBMs -- building is the tighter of the
two resources. Placement is therefore LATEST-FIT against each campaign's deadline: a campaign that
cannot fit late enough is pulled earlier, never later, because building early only spends green-tyre
shelf life (hours out of 24) while building late starves a press immediately.

Output schema is byte-compatible with the scheduler's inputs/building_plan.csv:
    plan_id, skuCode, gtCode, date, buildMachine, shift, startTime, endTime, qty, cycleTime, remarks
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

SHIFT_MIN = 480
BUCKET_MIN = 60      # deadline granularity: presses draw continuously, so date demand hourly


def cure_draw_curve(curing_plan: pd.DataFrame) -> pd.DataFrame:
    """Per-SKU press draw, one row per (sku, shift-slot).

    A curing plan row is a full shift on one press, so the draw is spread evenly across those 8 h.
    Summing over presses gives the rate at which the floor consumes each SKU's green tyres.
    """
    d = curing_plan.copy()
    d["startTime"] = pd.to_datetime(d["startTime"])
    d["endTime"] = pd.to_datetime(d["endTime"])
    g = (d.groupby(["skuCode", "startTime", "endTime", "shift", "date"], as_index=False)["qty"]
          .sum().rename(columns={"qty": "cure_qty"}))
    return g.sort_values("startTime").reset_index(drop=True)


def shift_back(draw: pd.DataFrame, dwell_h: float) -> pd.DataFrame:
    """Move the draw curve back by the target dwell -- the build-by curve.

    Uses continuous time, then re-buckets to shifts, so a 4.34 h lead correctly puts most of a
    shift's tyres in the SAME shift and the early part of it in the previous one, which is what the
    p10=1.18 h / median=4.34 h dwell distribution shows the plant actually doing.
    """
    d = draw.copy()
    off = pd.Timedelta(hours=float(dwell_h))
    d["build_start"] = d["startTime"] - off
    d["build_end"] = d["endTime"] - off
    return d


def _slot_of(ts: pd.Timestamp, grid_start: pd.Timestamp) -> int:
    """Shift index of a timestamp on the 8-hour grid anchored at the plan's first shift."""
    return int((ts - grid_start).total_seconds() // (8 * 3600))


def bucket_to_grid(shifted: pd.DataFrame, grid_start: pd.Timestamp, n_buckets: int,
                   bucket_min: int = BUCKET_MIN) -> dict[tuple[int, str], float]:
    """Split each shifted draw interval across the time buckets it overlaps, pro rata by time.

    GRANULARITY IS NOT COSMETIC. Bucketing to whole 8 h shifts dates every tyre of the first shift
    to 07:00 exactly, so the placement window for that demand collapses to a single instant, only 11
    campaigns (one per TBM) can start there, and everything else is declared opening WIP. A press
    draws continuously through its shift, so an hourly bucket dates each tyre near when it is really
    needed and the window opens up progressively -- which is both more accurate and far cheaper in
    opening stock.
    """
    need: dict[tuple[int, str], float] = {}
    blen = pd.Timedelta(minutes=bucket_min)
    for _, r in shifted.iterrows():
        s, e, q, sku = r["build_start"], r["build_end"], float(r["cure_qty"]), r["skuCode"]
        if e <= s or q <= 0:
            continue
        total = (e - s).total_seconds()
        i0 = int((s - grid_start).total_seconds() // (bucket_min * 60))
        i1 = int((e - pd.Timedelta(seconds=1) - grid_start).total_seconds() // (bucket_min * 60))
        for i in range(i0, i1 + 1):
            lo = grid_start + i * blen
            hi = lo + blen
            ov = (min(e, hi) - max(s, lo)).total_seconds()
            if ov <= 0:
                continue
            # A tyre needed before the plan starts must come from opening stock; clamping it into
            # bucket 0 is what stops the grid going negative.
            j = min(max(i, 0), n_buckets - 1)
            need[(j, sku)] = need.get((j, sku), 0.0) + q * ov / total
    return need


def chunk_into_campaigns(need: dict[tuple[int, str], float], n_buckets: int,
                         cycle: dict[str, float], target_campaign_min: float,
                         max_span_buckets: int = 20) -> list[dict]:
    """Turn the per-shift build-by curve into CAMPAIGN-sized parcels with deadlines.

    Building one shift's demand inside that same shift shatters the plan: 45 SKUs drawn every shift
    across 11 TBMs gives ~3 blocks per TBM-shift = 9 changeovers/TBM/day, against the plant's 2.65.
    The plant does not do that. It builds a SKU in ~5.3 h campaigns that cover SEVERAL shifts of
    press draw, and lets the green tyres wait -- which is free, because the wait is hours against a
    24 h shelf.

    So demand is accumulated per SKU across consecutive shifts until it is worth a campaign, and the
    parcel's DEADLINE is the start of the EARLIEST shift it covers (its first tyre is drawn then).
    The tail parcel is emitted whatever its size -- dropping it would starve a press.
    """
    per_sku: dict[str, list[tuple[int, float]]] = {}
    for (bucket, sku), q in need.items():
        if q > 1e-9:
            per_sku.setdefault(sku, []).append((bucket, q))
    parcels = []
    for sku, items in per_sku.items():
        items.sort()
        cyc = cycle.get(sku, 1.0)
        acc_q, acc_first = 0.0, None
        for bucket, q in items:
            # SPAN CAP: a campaign is deadlined at the FIRST shift it covers, so every extra shift it
            # absorbs is another 8 h the tail of that campaign waits. Sizing on work alone lets a
            # small SKU's campaign cover a week of draw and the plan's lead blows out to a p90 of
            # 56 h -- past the 48 h green-tyre shelf, i.e. a plan that ages its own stock. Capping the
            # span at the plant's observed dwell tolerance keeps campaigns long AND tyres fresh.
            if acc_first is not None and bucket - acc_first >= max_span_buckets:
                parcels.append({"sku": sku, "qty": acc_q, "deadline_bucket": acc_first,
                                "minutes": acc_q * cyc, "cycle": cyc})
                acc_q, acc_first = 0.0, None
            if acc_first is None:
                acc_first = bucket
            acc_q += q
            if acc_q * cyc >= target_campaign_min:
                parcels.append({"sku": sku, "qty": acc_q, "deadline_bucket": acc_first,
                                "minutes": acc_q * cyc, "cycle": cyc})
                acc_q, acc_first = 0.0, None
        if acc_q > 1e-9 and acc_first is not None:
            parcels.append({"sku": sku, "qty": acc_q, "deadline_bucket": acc_first,
                            "minutes": acc_q * cyc, "cycle": cyc})

    # SPLIT OVERSIZED PARCELS ACROSS TBMs.
    # A single shift of the biggest runner is already 486 min of work -- more than one TBM-shift --
    # so chunking alone cannot make every campaign fit a machine. Left whole, such a parcel needs an
    # unbroken 8-11 h window on ONE TBM; as the month fills those windows disappear and the parcel
    # is declared unbuildable (this was the entire 39.7k "opening WIP" figure: its unplaced parcels
    # had a median of 493 min, all above the 480 min shift). Splitting into machine-sized siblings
    # that share a deadline lets them run on different TBMs at once, which is exactly what the MES
    # shows the plant doing with its high-volume SKUs.
    out = []
    for p in parcels:
        if p["minutes"] <= SHIFT_MIN:
            out.append(p)
            continue
        n_split = int(math.ceil(p["minutes"] / SHIFT_MIN))
        for _ in range(n_split):
            out.append({"sku": p["sku"], "qty": p["qty"] / n_split,
                        "deadline_bucket": p["deadline_bucket"],
                        "minutes": p["minutes"] / n_split, "cycle": p["cycle"]})
    out.sort(key=lambda p: (p["deadline_bucket"], -p["minutes"]))
    return out


def schedule_campaigns(parcels: list[dict], n_buckets: int, tbms: list[str], availability: float,
                       shelf_min: float | dict[str, float]) -> tuple[list[dict], list[dict]]:
    """Place each campaign as LATE as it can go while still finishing before its deadline.

    Finite-capacity JIT dispatch on a continuous minute timeline per TBM (not a per-shift bin), so a
    campaign may straddle a shift boundary exactly as the plant's do. Latest-fit keeps green tyres
    fresh; the shelf bound stops a campaign being pulled so far forward that its tyres would age out
    before the press draws them -- the same shelf discipline the downstream scheduler enforces,
    applied here at plan time so the plan cannot ask for something that ages.

    Returns (placed blocks, parcels that could not be placed before t0 = the opening-WIP need).
    """
    horizon = n_buckets * BUCKET_MIN
    # Per TBM: sorted list of (start, end) busy intervals on the minute timeline.
    busy: dict[str, list[tuple[float, float]]] = {t: [] for t in tbms}
    cap_per_tbm = horizon * availability
    used: dict[str, float] = {t: 0.0 for t in tbms}
    placed, unplaced = [], []

    def latest_fit(iv: list[tuple[float, float]], lo: float, hi: float, dur: float):
        """Latest start in [lo, hi-dur] that does not overlap `iv` (which is sorted ascending).

        Walks the busy blocks from the end: one starting after the candidate slot ends is
        irrelevant, one ending before it starts means the slot is clear (and so is everything
        earlier), and an overlap slides the candidate to just before that block.
        """
        cur = hi - dur
        if cur < lo:
            return None
        for s, e in reversed(iv):
            if s >= cur + dur:
                continue            # busy block starts after our slot ends -- no conflict
            if e <= cur:
                break               # block ends before our slot starts -- slot is clear
            cur = s - dur           # overlap: slide to just before this block
            if cur < lo:
                return None
        return cur

    def earliest_fit(iv: list[tuple[float, float]], lo: float, dur: float):
        """Earliest start at/after `lo` that does not overlap `iv` (sorted ascending)."""
        cur = lo
        for s, e in iv:
            if e <= cur:
                continue
            if s >= cur + dur:
                return cur          # fits in the gap before this block
            cur = e                 # overlap: jump past it
        return cur

    def _shelf(sku: str) -> float:
        return float(shelf_min.get(sku, 48 * 60.0)) if isinstance(shelf_min, dict) else float(shelf_min)

    for p in parcels:
        # A TBM delivers tyres CONTINUOUSLY, ~1 per minute, not in a lump at the campaign's end. So
        # the campaign must have STARTED by the time its first tyre is drawn -- from then on its
        # output flows alongside the press draw, and a SKU's presses always draw slower than one TBM
        # builds (that is why one TBM feeds several presses). Requiring the whole campaign to FINISH
        # before its first tyre is drawn instead made every SKU's first campaign unbuildable and
        # invented 10,485 tyres of opening WIP across 32 SKUs that the plant never needed.
        dl = p["deadline_bucket"] * BUCKET_MIN          # when this campaign's first tyre is drawn
        dur = p["minutes"]
        lo = max(dl - _shelf(p["sku"]), 0.0)
        hi = dl + dur                                # start by `dl` == finish by `dl + dur`
        # prefer the least-loaded TBM that can still take it late (spreads load, keeps campaigns whole)
        # Pick the TBM offering the LATEST feasible start, not merely the first least-loaded one that
        # fits. Every hour a campaign runs earlier than it must is an hour its tyres sit as WIP, and
        # taking the first acceptable machine left the plan holding a 14.5 h median float against
        # the plant's 4.3 h. Load is used only to break ties, so balance is still respected.
        best = None
        for t in sorted(tbms, key=lambda k: used[k]):
            if used[t] + dur > cap_per_tbm:
                continue
            st = latest_fit(busy[t], lo, hi, dur)
            if st is not None and (best is None or st > best[1]):
                best = (t, st)
        if best is None:
            # try any TBM ignoring the soft capacity target before declaring it unplaceable
            for t in tbms:
                st = latest_fit(busy[t], lo, hi, dur)
                if st is not None:
                    best = (t, st)
                    break
        if best is None:
            # Nothing fits before the deadline. That does NOT make the campaign unbuildable: the
            # presses simply draw from opening stock while it runs. Place it as EARLY as the
            # machines allow and let the cumulative-deficit calculation price the stock it needs --
            # far more honest than declaring the tyres missing, which is what previously inflated
            # the opening-WIP figure to five times its real size.
            for t in sorted(tbms, key=lambda k: used[k]):
                st = earliest_fit(busy[t], 0.0, dur)
                if st is not None:
                    best = (t, st)
                    break
        if best is None:
            unplaced.append(p)
            continue
        t, st = best
        busy[t].append((st, st + dur))
        busy[t].sort()
        used[t] += dur
        placed.append({"tbm": t, "sku": p["sku"], "qty": p["qty"], "minutes": dur,
                       "cycle": p["cycle"], "start_min": st})
    return merge_adjacent(placed), unplaced


def merge_adjacent(blocks: list[dict], gap_tol_min: float = 1.0) -> list[dict]:
    """Fuse touching same-SKU campaigns on the same TBM into one.

    Splitting oversized parcels across machines, and chunking by work, both hand a TBM the same SKU
    twice in a row. The plant would run that as ONE campaign and change over once; leaving them
    separate inflates the changeover count without changing a single minute of the schedule.
    """
    if not blocks:
        return blocks
    blocks = sorted(blocks, key=lambda b: (b["tbm"], b["start_min"]))
    out: list[dict] = []
    for b in blocks:
        if out:
            p = out[-1]
            if (p["tbm"] == b["tbm"] and p["sku"] == b["sku"]
                    and b["start_min"] - (p["start_min"] + p["minutes"]) <= gap_tol_min):
                p["minutes"] = b["start_min"] + b["minutes"] - p["start_min"]
                p["qty"] += b["qty"]
                continue
        out.append(dict(b))
    return out


def opening_wip_deficit(curing_plan: pd.DataFrame, blocks: list[dict], grid_start: pd.Timestamp,
                        step_min: int = 30) -> dict[str, float]:
    """The stock each SKU must have at t0, as the MAX CUMULATIVE DEFICIT of build behind cure.

    Both machines run continuously, so both curves are continuous: a press consumes evenly across
    its 8 h shift and a TBM delivers evenly across its campaign. The opening stock a SKU needs is
    simply the worst point at which cumulative cure has outrun cumulative build:

        opening_wip(sku) = max over t of ( cure_cum(t) - build_cum(t) ), floored at 0

    This is the only honest definition. Counting "campaigns that missed their deadline" instead
    conflates a scheduling artefact with a stock requirement and, at 8 h bucket resolution, inflated
    the answer roughly fivefold.
    """
    cp = curing_plan.copy()
    cp["startTime"] = pd.to_datetime(cp["startTime"])
    cp["endTime"] = pd.to_datetime(cp["endTime"])
    t0 = grid_start
    horizon = max((cp["endTime"].max() - t0).total_seconds() / 60.0,
                  max((b["start_min"] + b["minutes"] for b in blocks), default=0.0))
    grid = np.arange(0.0, horizon + step_min, step_min)

    def _cum(intervals: list[tuple[float, float, float]]) -> np.ndarray:
        """Cumulative quantity delivered/consumed by each grid time, linear within each interval."""
        out = np.zeros_like(grid)
        for s, e, q in intervals:
            if e <= s or q <= 0:
                continue
            frac = np.clip((grid - s) / (e - s), 0.0, 1.0)
            out += q * frac
        return out

    cure_iv: dict[str, list[tuple[float, float, float]]] = {}
    for _, r in cp.iterrows():
        s = (r["startTime"] - t0).total_seconds() / 60.0
        e = (r["endTime"] - t0).total_seconds() / 60.0
        cure_iv.setdefault(r["skuCode"], []).append((s, e, float(r["qty"])))
    build_iv: dict[str, list[tuple[float, float, float]]] = {}
    for b in blocks:
        build_iv.setdefault(b["sku"], []).append(
            (b["start_min"], b["start_min"] + b["minutes"], b["qty"]))

    out: dict[str, float] = {}
    for sku, ivs in cure_iv.items():
        deficit = _cum(ivs) - _cum(build_iv.get(sku, []))
        peak = float(deficit.max())
        if peak > 0.5:
            out[sku] = peak
    return out


def corridor_check(curing_plan: pd.DataFrame, blocks: list[dict], grid_start: pd.Timestamp,
                   opening: dict[str, float], shelf_h: float | dict[str, float],
                   step_min: int = 30) -> dict:
    """Verify the TWO-SIDED corridor that makes build rate equal cure rate.

        C_i(t) - W_i^0  <=  B_i(t)  <=  C_i(t) + K_i          K_i = shelf_i x draw_rate_i

    The lower edge is starvation; the upper edge is the kanban cap. The upper edge is the one that
    matters here, and it is worth being precise about WHY: because B - C is bounded above by K_i,
    over any window longer than K_i/d_i = shelf_i the build rate CANNOT exceed the cure rate. Rate
    matching is a consequence of the constraint, never something the planner imposes.

    Note what the objective does to the same constraint. Under an ASAP / minimise-build-makespan
    objective the cap BINDS everywhere -- the plan sits hard against the ceiling and WIP == K for the
    whole month, which reads as "the cap did nothing". Under ALAP the plan sits on the FLOOR and the
    cap never binds at all. Identical constraint, opposite behaviour, decided entirely by whether a
    makespan objective is present.
    """
    cp = curing_plan.copy()
    cp["startTime"] = pd.to_datetime(cp["startTime"])
    cp["endTime"] = pd.to_datetime(cp["endTime"])
    t0 = grid_start
    horizon_h = max((cp["endTime"].max() - t0).total_seconds() / 3600.0,
                    max((b["start_min"] + b["minutes"] for b in blocks), default=0.0) / 60.0)
    grid = np.arange(0.0, horizon_h + step_min / 60.0, step_min / 60.0)

    def _cum(iv):
        out = np.zeros_like(grid)
        for s, e, q in iv:
            if e > s and q > 0:
                out += q * np.clip((grid - s) / (e - s), 0.0, 1.0)
        return out

    cure_iv, build_iv = {}, {}
    for _, r in cp.iterrows():
        cure_iv.setdefault(r["skuCode"], []).append(
            ((r["startTime"] - t0).total_seconds() / 3600.0,
             (r["endTime"] - t0).total_seconds() / 3600.0, float(r["qty"])))
    for b in blocks:
        build_iv.setdefault(b["sku"], []).append(
            (b["start_min"] / 60.0, (b["start_min"] + b["minutes"]) / 60.0, b["qty"]))

    def _shelf(sku):
        return float(shelf_h.get(sku, 48.0)) if isinstance(shelf_h, dict) else float(shelf_h)

    breaches, peak_wip_total, worst_ratio = [], 0.0, 0.0
    wip_curve = np.zeros_like(grid)
    for sku, iv in cure_iv.items():
        cure = _cum(iv)
        build = _cum(build_iv.get(sku, [])) + opening.get(sku, 0.0)
        wip = build - cure
        wip_curve += np.clip(wip, 0.0, None)
        peak = float(wip.max())
        # KANBAN CAP = the most that can be DRAWN inside one shelf-length window:
        #     K_i = max over t of ( C_i(t + shelf_i) - C_i(t) )
        # Not shelf x AVERAGE draw rate. A SKU is cured in long press campaigns, so its month-average
        # rate badly understates the rate while it is actually running -- using the average flagged 20
        # SKUs at up to +919% over cap when nothing was actually ageing. Stock is only condemned if it
        # exceeds what the presses can pull through in a shelf window, which is the peak-window draw.
        lag = int(round(_shelf(sku) / (step_min / 60.0)))
        k_i = float((cure[lag:] - cure[:-lag]).max()) if 0 < lag < len(cure) else float(cure[-1])
        peak_wip_total += max(peak, 0.0)
        if k_i > 0:
            worst_ratio = max(worst_ratio, peak / k_i)
            if peak > k_i + 1e-6:
                breaches.append({"sku": sku, "peak_wip": round(peak, 1),
                                 "kanban_cap": round(k_i, 1),
                                 "over_pct": round(100 * (peak / k_i - 1), 1)})
    breaches.sort(key=lambda b: -b["over_pct"])
    return {
        "corridor_breaches": len(breaches),
        "worst_peak_over_cap_pct": round(100 * (worst_ratio - 1), 1) if worst_ratio else 0.0,
        "gt_wip_peak_total": round(float(wip_curve.max()), 0),
        "gt_wip_mean_total": round(float(wip_curve.mean()), 0),
        "top_breaches": breaches[:5],
    }


def emit(blocks: list[dict], grid_start: pd.Timestamp, sku2gt: dict[str, str],
         plan_id: str) -> pd.DataFrame:
    """Turn placed campaigns into building_plan.csv rows (sub-shift blocks, as the plant writes them).

    A campaign is dated and shift-labelled by where it STARTS; one that runs past a shift boundary
    keeps its start's label, which is how the plant's own building plan records a straddling block.
    """
    shift_name = {0: "A", 1: "B", 2: "C"}
    rows = []
    for b in blocks:
        if b["qty"] <= 0 or b["minutes"] <= 0:
            continue
        st = grid_start + pd.Timedelta(minutes=float(b["start_min"]))
        en = st + pd.Timedelta(minutes=float(b["minutes"]))
        slot = int(b["start_min"] // SHIFT_MIN)
        base = grid_start + pd.Timedelta(minutes=slot * SHIFT_MIN)
        rows.append({
            "plan_id": plan_id,
            "skuCode": b["sku"],
            "gtCode": sku2gt.get(b["sku"], ""),
            "date": base.strftime("%Y-%m-%d"),
            "buildMachine": b["tbm"],
            "shift": shift_name[slot % 3],
            "startTime": st.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": en.strftime("%Y-%m-%d %H:%M:%S"),
            "qty": int(round(b["qty"])),
            "cycleTime": round(b["cycle"], 4),
            "remarks": shift_name[slot % 3],
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out[out["qty"] > 0].sort_values(["startTime", "buildMachine"]).reset_index(drop=True)


def build_building_plan(curing_plan: pd.DataFrame, req: pd.DataFrame, tbm_master: pd.DataFrame,
                        rulebook: dict, plan_id: str = "CTP_PCR",
                        gt_shelf_h: float | dict[str, float] = 48.0) -> tuple[pd.DataFrame, dict]:
    """Full building plan, pulled from the curing plan. Returns (plan, report)."""
    draw = cure_draw_curve(curing_plan)
    if draw.empty:
        return pd.DataFrame(), {}
    # DO NOT pre-shift the draw curve by the dwell. The lead is already produced structurally: a
    # campaign is deadlined at the moment its FIRST tyre is drawn but delivers the whole parcel in
    # one ~5 h run, so everything after that first tyre is inherently built ahead. Shifting the curve
    # back as well applies the interlock twice and the plan carries 4.6 h more green-tyre float than
    # it needs (12.8 h median instead of 8.2 h) for no gain -- starvation is 0.000% either way.
    dwell = 0.0
    shifted = shift_back(draw, dwell)

    grid_start = pd.to_datetime(curing_plan["startTime"]).min()
    grid_end = pd.to_datetime(curing_plan["endTime"]).max()
    n_buckets = int(math.ceil((grid_end - grid_start).total_seconds() / (BUCKET_MIN * 60)))
    need = bucket_to_grid(shifted, grid_start, n_buckets)
    demand_total = sum(need.values())

    cycle = dict(zip(req["sku_code"], req["build_cycle_min"]))
    sku2gt = dict(zip(req["sku_code"], req["gt_code"]))
    tbms = [str(t) for t in tbm_master["tbm"]]
    # TBM sizing uses the PLANNING availability (p90 of achieved per-shift busy), not the aggregate
    # busy fraction -- see reverse_engineer.derive_shift_utilisation for why the two differ.
    availability = float(rulebook.get("tbm_planning_availability")
                         or rulebook["tbm_availability_median"])
    target_campaign_min = float(rulebook["campaigns"]["tbm_campaign_h_median"]) * 60.0

    # Green-tyre shelf bounds how early a campaign may be pulled. It comes from the plant's own
    # aging master (48 h for 347 of 352 GT codes), NOT from a guess: at 24 h the placement window is
    # only 3 shifts wide, too narrow to absorb the demand peaks, and the plan then declares a tenth
    # of the month unbuildable when it is merely mistimed.
    shelf_arg = ({sku: float(v) * 60.0 for sku, v in gt_shelf_h.items()}
                 if isinstance(gt_shelf_h, dict) else float(gt_shelf_h) * 60.0)
    # How many shifts of draw one campaign may cover, from the plant's own p90 build->cure dwell
    # (20.42 h -> 3 shifts): the plant tolerates tyres waiting about that long, and no longer.
    max_span = max(int(round(float(rulebook["interlock"]["dwell_h_p90"]) * 60.0 / BUCKET_MIN)), 1)
    parcels = chunk_into_campaigns(need, n_buckets, cycle, target_campaign_min, max_span)
    blocks, unplaced = schedule_campaigns(parcels, n_buckets, tbms, availability, shelf_min=shelf_arg)
    plan = emit(blocks, grid_start, sku2gt, plan_id)

    planned = float(sum(b["qty"] for b in blocks))
    # Unplaced parcels are not a loss: they are demand whose deadline falls in the opening lead
    # window (the presses start at 07:00 on day 1, building leads them by only 4.34 h) or that no
    # TBM could take in time. The plant is never empty at a month boundary; the engine states
    # exactly how much stock the month must OPEN with, and that quantity counts as covered.
    opening = opening_wip_deficit(curing_plan, blocks, grid_start)
    opening_total = float(sum(opening.values()))
    for p in unplaced:                       # truly unbuildable work also has to come from stock
        opening[p["sku"]] = opening.get(p["sku"], 0.0) + p["qty"]
        opening_total += float(p["qty"])
    report = {
        "demand_tyres": round(demand_total, 1),
        "planned_tyres": round(planned, 1),
        # Coverage is BUILD against draw. Opening WIP is a buffer that lets the early draw run before
        # building catches up -- it is not extra supply and must not be added in, or a plan can score
        # over 100% while still building less than the presses consume.
        "coverage_pct": round(100.0 * planned / demand_total, 3) if demand_total else 0.0,
        "campaigns": len(blocks),
        "campaign_min_median": round(float(np.median([b["minutes"] for b in blocks])), 1) if blocks else 0.0,
        "opening_wip_required": {k: round(v, 1) for k, v in opening.items()},
        "opening_wip_total": round(opening_total, 1),
        "tbm_planning_availability": availability,
        "target_campaign_min": round(target_campaign_min, 1),
    }
    report.update(corridor_check(curing_plan, blocks, grid_start, opening, gt_shelf_h))
    return plan, report
