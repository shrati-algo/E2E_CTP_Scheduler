#!/usr/bin/env python3
"""ctp_aligned_needby.py — CTP port of v10's `frc_aligned_needby.py`.

    python ctp_aligned_needby.py --line pcr --run e2e_v7

THE LOOP THIS CLOSES  (v10's own words, and the same hole exists in CTP)
  Phase 4 walks backward from the building plan and gives every lot a deadline
  in ONE pass. At the moment it runs the campaign does not exist, so a calender
  lot's deadline comes from the cutter that consumes it, and the compound's
  deadline comes from that.

  `ctp_campaign.py` then re-times those lots onto its own clock. Mixing is
  therefore working to a moment the campaign plan has abandoned.

  The planner's rule, quoted in v10: "first we decide the frc plan according to
  its downstream, then mixing needed by decided by the frc plan."

WHAT IT DOES
  Runs AFTER ctp_campaign.py. Pins each campaigned lot to the start the
  campaign actually gave it, then carries that backward through the lot DAG so
  every upstream lot inherits a deadline meaning "be ready for the campaign",
  not "be ready for what the cutter would have wanted".

      latest_finish(producer) = start(consumer) - min_aging(producer)

  which is the rule phase 4 already applies at every level - the only thing
  that changes is where the chain is anchored. A lot feeding several consumers
  takes the tightest of them.

WHAT IT DOES NOT DO
  The pin travels UPSTREAM only. Building and curing keep the deadlines the
  building plan gave them; nothing downstream moves. Lot sizing is untouched,
  so this is safe to run between phase 4 and phase 4b without rebuilding.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import pandas as pd

import ctpio as io


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", required=True, choices=["pcr", "tbr"])
    ap.add_argument("--run", default="e2e_v7")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    OUT = io.outdir(a.run, a.line)
    ltf, edf = OUT / "phase4_lot_times.csv", OUT / "phase4_edges.csv"
    cpf = OUT / "campaign_plan.csv"
    for f in (ltf, edf, cpf):
        if not f.exists():
            print(f"  [align] missing {f.name} — nothing to do")
            return 1

    print("\n" + "=" * 74)
    print(f"  CTP ALIGNED NEED-BY — re-aim upstream at the campaign  "
          f"[{a.line.upper()}]")
    print("=" * 74)

    lt = pd.read_csv(ltf, low_memory=False)
    lt["lot_id"] = lt["lot_id"].astype(str)
    for c in ("EST", "LST", "LFT"):
        lt[c] = pd.to_datetime(lt[c], errors="coerce")

    ed = pd.read_csv(edf, low_memory=False)
    producers_of = defaultdict(list)
    for p, c in zip(ed["producer_lot"].astype(str), ed["consumer_lot"].astype(str)):
        producers_of[c].append(p)

    cp = pd.read_csv(cpf)
    cp["lot_id"] = cp["lot_id"].astype(str)
    cp["start"] = pd.to_datetime(cp["start"], errors="coerce")
    starts = {l: s for l, s in zip(cp["lot_id"], cp["start"]) if pd.notna(s)}

    bom = io.load_bom(a.line)
    itypes = io.load_itemtypes(bom)
    aging = io.load_aging(a.line)
    aging, _ = io.apply_aging_defaults(aging, set(itypes), itypes)

    item_of = dict(zip(lt["lot_id"], lt["item_code"].astype(str)))
    dur_of = dict(zip(lt["lot_id"],
                      pd.to_numeric(lt["duration_h"], errors="coerce").fillna(0.0)))
    latest_finish = dict(zip(lt["lot_id"], lt["LFT"]))

    def min_age_h(lot: str) -> float:
        return float(aging.get(item_of.get(lot, ""), (0.0, 0.0))[0] or 0.0)

    # 1. the campaigned lots: the campaign owns these outright
    planned, shift = {}, []
    for lot, s in starts.items():
        if lot not in latest_finish:
            continue
        planned[lot] = s
        prev = latest_finish[lot]
        if pd.notna(prev):
            shift.append((s + pd.Timedelta(hours=dur_of.get(lot, 0.0))
                          - prev).total_seconds() / 3600.0)
        latest_finish[lot] = s + pd.Timedelta(hours=dur_of.get(lot, 0.0))
    sh = pd.Series(shift) if shift else pd.Series([0.0])
    print(f"  lots pinned to the campaign : {len(planned):,}  "
          f"(median {sh.median():+.0f} h, p5 {sh.quantile(.05):+.0f} h, "
          f"p95 {sh.quantile(.95):+.0f} h)")

    # 2. breadth-first up the graph. A lot is revisited only when a consumer
    #    pulls it EARLIER than anything already has, so the walk converges and
    #    the tightest consumer wins.
    pulled, frontier, guard = [], list(planned.items()), 0
    while frontier and guard < 200:
        guard += 1
        nxt = []
        for consumer, cstart in frontier:
            for prod in producers_of.get(consumer, ()):
                if prod not in latest_finish:
                    continue
                deadline = cstart - pd.Timedelta(hours=min_age_h(prod))
                cur = latest_finish.get(prod)
                if cur is not None and pd.notna(cur) and deadline >= cur:
                    continue
                if cur is not None and pd.notna(cur):
                    pulled.append((deadline - cur).total_seconds() / 3600.0)
                latest_finish[prod] = deadline
                pstart = deadline - pd.Timedelta(hours=dur_of.get(prod, 0.0))
                planned[prod] = pstart
                nxt.append((prod, pstart))
        frontier = nxt
    pl = pd.Series(pulled) if pulled else pd.Series([0.0])
    print(f"  upstream deadlines pulled in: {len(pulled):,}  "
          f"(median {pl.median():+.0f} h, p5 {pl.quantile(.05):+.0f} h)")

    # 3. write the new window back, clamping exactly as phase 4 does
    before = lt[["lot_id", "department", "LFT"]].copy()
    out = lt.copy()
    out["LFT"] = out["lot_id"].map(latest_finish)
    out["LST"] = out["LFT"] - pd.to_timedelta(
        pd.to_numeric(out["duration_h"], errors="coerce").fillna(0.0), unit="h")
    bad = out["EST"] > out["LST"]
    out.loc[bad, "EST"] = out.loc[bad, "LST"]
    out["slack_h"] = (out["LST"] - out["EST"]).dt.total_seconds() / 3600.0
    if "max_age_conflict" in out.columns:
        out["max_age_conflict"] = out["max_age_conflict"].astype(bool) | bad
    out["is_critical"] = out["slack_h"] <= 0
    print(f"  lots whose window closed    : {int(bad.sum()):,}")

    j = before.merge(out[["lot_id", "LFT"]], on="lot_id", suffixes=("_b", "_a"))
    j["moved_h"] = (j["LFT_a"] - j["LFT_b"]).dt.total_seconds() / 3600.0
    mv = j[j["moved_h"].abs() > 0.01]
    if len(mv):
        print("\n  need-by shift by department (negative = wanted earlier):")
        g = mv.groupby("department")["moved_h"].agg(
            lots="size", median="median", p5=lambda s: s.quantile(0.05)).round(1)
        print(g.to_string())

    if a.dry_run:
        print("\n  [dry-run] nothing written")
        return 0
    out.to_csv(ltf, index=False)
    print(f"\n[OK] {ltf.name} re-aimed at the campaign plan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
