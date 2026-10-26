#!/usr/bin/env python3
"""ctp_campaign.py — CTP port of v10's `frc_campaign.py`.

    python ctp_campaign.py --line pcr --run e2e_v7

THE PROBLEM, IN CTP'S OWN NUMBERS
  IL056 needs 8.0 h of calender time and is smeared over 88.4 h; IL039 10.2 h
  over 76.7 h; IL054 6.4 h over 87.0 h. Machine 1101 is not busy - each lot is
  simply placed just-in-time for its own consumer, so the LAST one lands on
  30 Sep and building waits for it. BTP wrote the same diagnosis down:
  "the problem is fragmentation, not small demand".

WHY A DISPATCH RULE CANNOT FIX IT  (v10's reasoning, unchanged)
  The dispatcher picks ONE LOT at a time and every rule can only refuse or
  redirect that pick; nothing assembles a block and then places it.

THE MECHANISM, PORTED
  The campaign order is written back as a RELEASE SCHEDULE. Every lot of a
  campaign gets the same release time (EST), and the next campaign on that
  machine is released when this one would finish. A lot can only be picked
  once released, so the dispatcher cannot interleave them - it runs the block
  because nothing else is open to it.

WHAT IS *NOT* PORTED, AND WHY
  v10 groups by PATTERN and fills a whole DEEP ROLL, sequencing wire against
  fabric with chafer closers and wire-to-wire gaps. CTP has none of the masters
  that needs - no pattern master, no deep-roll master (BR4RC-02), no wire-type
  master (BRB-02). Those are in the rule file's `unimplementable` list and are
  NOT faked here. CTP campaigns group by ITEM CODE, which is the honest
  analogue of "pattern" when no pattern master exists.

SAFETY RAILS, kept exactly as v10 has them
  * the deadline (LFT) is never touched by this module
  * a campaign only gathers lots whose need-by is within the lookahead of its
    first lot, so a block is never built out of material wanted days apart
  * the lookahead is capped by the item's OWN max aging - a campaign can never
    pull work so far forward that it would perish before it is needed
  * no lot is released later than `LFT - duration`, so campaigning can never
    make a lot miss its own deadline
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd

import ctpio as io
import rules as R

# Departments worth campaigning. These are the ones CTP fragments: the
# calender and the slitters/cutters, where one item is visited many times for
# a few hundred metres each. Mixing is deliberately excluded - it has its own
# MPQ/campaign treatment in phase 3 and v10 keeps the two separate too.
DEFAULT_DEPTS = [
    "CALENDERING", "CALENDARING", "CAP PLY SLITTER", "SLITTER",
    "Chaffer Slitter", "PLY CUTTER", "CUTTING", "BELT CUTTER",
    "TBR Body ply CUTTER", "Bios cutter", "BIAS CUTTER",
]


def _norm(x) -> str:
    return str(x or "").strip().upper()


def build_campaigns(lots: pd.DataFrame, aging: dict, lookahead_h: float,
                    max_campaign_h: float) -> list[dict]:
    """Gather each item's lots, in need-by order, into campaigns.

    Mirrors v10 `build_campaigns`, with ITEM CODE standing in for PATTERN and
    a work-hours cap standing in for the deep-roll multiple.
    """
    taken: set = set()
    out: list[dict] = []
    order = list(lots.index)                       # already sorted by LFT
    for first in order:
        if first in taken:
            continue
        item = lots.at[first, "item_code"]
        # THE LOOKAHEAD IS A HARD CAP, AND AGING CAPS IT FURTHER.
        # v10: "never reach further than 96 h wire / 72 h fabric, whether the
        # roll completes or not". Here the item's own shelf life is the second
        # bound - pulling work forward past it would simply spoil the material.
        mx = float(aging.get(item, (0.0, 0.0))[1] or 0.0)
        reach_h = min(lookahead_h, mx) if mx > 0 else lookahead_h
        reach_until = lots.at[first, "LFT"] + pd.Timedelta(hours=reach_h)

        same = [first] + [o for o in order
                          if o not in taken and o != first
                          and lots.at[o, "item_code"] == item
                          and lots.at[o, "LFT"] <= reach_until]

        # HOW MUCH ONE CAMPAIGN MAY TAKE.
        # v10 bounds this (MAX_ROLLS_PER_CAMPAIGN) because an unbounded
        # campaign "emptied the window in one" - CAP 66 once gathered 68.3 h
        # into a single block and starved the next day. Same guard, in hours.
        picked, hours = [], 0.0
        for lot in same:
            d = float(lots.at[lot, "duration_h"])
            if picked and hours + d > max_campaign_h:
                break
            picked.append(lot)
            hours += d

        taken.update(picked)
        out.append({
            "item": item,
            "machine": str(lots.at[first, "machine"]),
            "lots": picked,
            "hours": hours,
            "earliest_needby": min(lots.at[l, "LFT"] for l in picked),
            "qty": sum(float(lots.at[l, "lot_qty"]) for l in picked),
        })
    return out


def sequence_campaigns(camps: list[dict], opening: pd.Timestamp) -> list[dict]:
    """Order campaigns by need-by and lay them on their machine's clock.

    ONE CLOCK, OWNED BY THE SEQUENCER — v10 had two timing passes that drifted
    a day apart; the times are stamped here as the order is decided, so the
    release schedule and the plan cannot disagree.
    """
    free: dict[str, pd.Timestamp] = {}
    for c in sorted(camps, key=lambda c: (c["earliest_needby"], c["item"])):
        m = c["machine"]
        s = max(free.get(m, opening), opening)
        c["starts_at"] = s
        c["ends_at"] = s + pd.Timedelta(hours=c["hours"])
        free[m] = c["ends_at"]
    return sorted(camps, key=lambda c: c["starts_at"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v7")
    ap.add_argument("--lookahead-h", type=float, default=None)
    ap.add_argument("--max-campaign-h", type=float, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    OUT = io.outdir(a.run, a.line)
    ltf = OUT / "phase4_lot_times.csv"
    if not ltf.exists():
        print(f"  [campaign] no {ltf.name} — nothing to do")
        return 1

    look = a.lookahead_h if a.lookahead_h is not None else \
        float(R.rule("campaign.lookahead_h", 48.0))
    caph = a.max_campaign_h if a.max_campaign_h is not None else \
        float(R.rule("campaign.max_campaign_h", 12.0))
    depts = {_norm(d) for d in (R.rule("campaign.departments") or DEFAULT_DEPTS)}

    print("\n" + "=" * 74)
    print(f"  CTP CAMPAIGN — release-schedule blocks   [{a.line.upper()}]")
    print("=" * 74)

    lt = pd.read_csv(ltf, low_memory=False)
    for c in ("EST", "LST", "LFT"):
        lt[c] = pd.to_datetime(lt[c], errors="coerce")
    lt["lot_id"] = lt["lot_id"].astype(str)

    bom = io.load_bom(a.line)
    aging = io.load_aging(a.line)
    aging, _ = io.apply_aging_defaults(aging, set(io.load_itemtypes(bom)),
                                       io.load_itemtypes(bom))

    m = lt[lt["department"].map(_norm).isin(depts) & lt["LFT"].notna()].copy()
    if not len(m):
        print("  no lots in the campaign departments — nothing to do")
        return 0
    # one machine per lot: the first of its allowable list
    m["machine"] = m["machines"].map(
        lambda v: str(v).split(",")[0].strip().strip('"') if pd.notna(v) else "")
    m = m.sort_values("LFT", kind="stable")
    m.index = range(len(m))

    camps = build_campaigns(m, aging, look, caph)
    opening = m["EST"].min()
    seq = sequence_campaigns(camps, opening)

    print(f"  lots considered : {len(m):,} in {m.department.nunique()} department(s)")
    print(f"  lookahead       : {look:g} h, capped by each item's own shelf life")
    print(f"  campaign cap    : {caph:g} h of work")
    print(f"  campaigns built : {len(seq):,}")

    # ---- write back as a RELEASE SCHEDULE --------------------------------
    row_of = {lid: i for i, lid in enumerate(lt["lot_id"])}
    rereleased, plan = 0, []
    for pos, c in enumerate(seq):
        for lot in c["lots"]:
            lid = str(m.at[lot, "lot_id"])
            r = row_of.get(lid)
            if r is None:
                continue
            latest_safe = lt.at[r, "LFT"] - pd.Timedelta(
                hours=float(lt.at[r, "duration_h"]))
            new = min(c["starts_at"], latest_safe)     # the safety rail
            if pd.notna(new) and new != lt.at[r, "EST"]:
                lt.at[r, "EST"] = new
                rereleased += 1
            plan.append({"campaign": pos, "item_code": c["item"],
                         "machine": c["machine"], "lot_id": lid,
                         "start": c["starts_at"], "end": c["ends_at"],
                         "campaign_hours": round(c["hours"], 2),
                         "lots_in_campaign": len(c["lots"]),
                         "earliest_needby": c["earliest_needby"]})

    late = sum(1 for c in seq if c["ends_at"] > c["earliest_needby"])
    print(f"  lots re-released: {rereleased:,}")
    print(f"  campaigns ending after their earliest need-by: {late}/{len(seq)}")

    if a.dry_run:
        print("  [dry-run] nothing written")
        return 0

    # EST may now exceed LST for a lot the campaign could not pull far enough;
    # clamp exactly as phase 4 does, and keep the flags honest.
    lt["LST"] = lt["LFT"] - pd.to_timedelta(
        pd.to_numeric(lt["duration_h"], errors="coerce").fillna(0.0), unit="h")
    bad = lt["EST"] > lt["LST"]
    lt.loc[bad, "EST"] = lt.loc[bad, "LST"]
    lt["slack_h"] = (lt["LST"] - lt["EST"]).dt.total_seconds() / 3600.0
    lt["is_critical"] = lt["slack_h"] <= 0
    lt.to_csv(ltf, index=False)
    pd.DataFrame(plan).to_csv(OUT / "campaign_plan.csv", index=False)
    print(f"  windows clamped : {int(bad.sum()):,}")
    print(f"\n[OK] {ltf.name} (re-released) + campaign_plan.csv ({len(plan):,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
