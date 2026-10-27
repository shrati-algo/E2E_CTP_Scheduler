#!/usr/bin/env python3
"""mix_planner.py - plan EVERY mixing lot first, with the mixing rules built in.

Implements MIXING_RULES_README.md sections 2, 4, 5, 6 and 7 for the CTP engine.
Called from phase5_placement.py before the dispatcher; the lots it plans are
FIXED - the dispatcher, the right-shift and the freshness repair never move
them - and every other machine is placed around them.

THE SIMULATION (README s.7). Each time a mixer m is free at t:
  1. a BUTYL BLOCK due today on m (once per plant day, after `earliest_hour`,
     only when its butyl inputs are ready) runs as one unit:
         friendly -> butyl unit(s) in order -> [friendly after]
     with a 5-batch PREP lot of a friendly compound when no real friendly lot
     is ready;
  2. otherwise the candidates are the compounds with lots left where
       - m is an allowed mixer (routing + master mixers for finals),
       - the compound is not on another mixer this plant day,
       - its last run started >= gap_h ago (gap_h_long for long items;
         friendly / prep compounds exempt),
       - its inputs are ready by t + max_wait,
       - it would not pass max age - shelf_margin before its last use;
  3. the one needed SOONEST wins (priority hours subtracted);
  4. it starts at max(t + changeover/clean-out, ready, shelf floor), skipping
     shift-handover blackouts, holidays and PM windows, and the run takes every
     lot of that compound needed within cover_h (cover_h_long) of the first,
     back to back with no changeover inside;
  5. the compound is locked to m for every plant day the run touches;
  6. if nothing can run the mixer waits for the earliest time something can,
     and the reason is counted.

Nothing here invents demand except the prep lots of rule 5.2, which are
written out with no consumer and a `prep` flag.
"""
from __future__ import annotations

import heapq
from collections import defaultdict

import pandas as pd

ONE_S = pd.Timedelta(seconds=1)


class MixPlanner:
    def __init__(self, cfg: dict, lots: dict, ctx: dict):
        """
        cfg   the `mix_planner` block of the rule file
        lots  key -> {item, itype, line, dur (Timedelta), elig [machine ids],
                      need, last_use (Timestamp), max_age (Timedelta|None),
                      ready (Timestamp), preds [mixing keys], min_age}
        ctx   helpers: legal_start(m, earliest, co_min, dur) -> Timestamp,
                       plant_day(t) -> Timestamp, open_at, transfer,
                       make_prep(item, m, line) -> dict|None, itype_of(item)
        """
        self.cfg, self.L, self.ctx = cfg, lots, ctx
        h = lambda k, d: pd.Timedelta(hours=float(cfg.get(k, d)))
        self.cover, self.cover_long = h("cover_h", 15), h("cover_h_long", 24)
        self.gap, self.gap_long = h("gap_h", 15), h("gap_h_long", 24)
        self.margin = h("shelf_margin_h", 2)
        self.max_wait = pd.Timedelta(minutes=float(cfg.get("max_wait_min", 15)))
        self.gap_wait = h("butyl_gap_wait_h", 6)
        self.co_s = float(cfg.get("changeover_s", 60))
        self.tr100_s = float(cfg.get("tr100_s", 300))
        self.clean_s = float(cfg.get("cleanout_s", 120))
        self.final_after_s = float(cfg.get("final_cleanout_after_s", 120))
        self.k4 = str(cfg.get("k430_2_machine", "106"))
        self.k4_s = float(cfg.get("k430_2_final_s", 120))
        up = lambda xs: {str(x).strip().upper() for x in (xs or [])}
        self.long_items = up(cfg.get("long_items"))
        self.exempt = up(cfg.get("gap_exempt"))
        self.butyl = up(cfg.get("butyl_compounds"))
        self.friendly = up(cfg.get("friendly_compounds"))
        self.campaigns = [up(c) for c in (cfg.get("butyl_campaigns") or [])]
        self.followers = {str(k).strip().upper(): str(v).strip().upper()
                          for k, v in (cfg.get("compound_followers") or {}).items()}
        self.prio = {str(k).strip().upper(): pd.Timedelta(hours=float(v))
                     for k, v in (cfg.get("item_priority_h") or {}).items()}
        self.fourrc = up(ctx.get("fourrc_compounds"))
        self.fourrc_h = h("fourrc_priority_h", 8)
        self.factor = {}
        for k, v in (cfg.get("machine_factor") or {}).items():
            it, _, mc = str(k).partition("|")
            self.factor[(it.strip().upper(), mc.strip())] = float(v)
        self.blocks = []
        for b in (cfg.get("butyl_blocks") or []):
            self.blocks.append({
                "name": b.get("name"), "mixer": str(b.get("mixer")),
                "before": [str(x).strip().upper() for x in (b.get("friendly_before") or [])],
                "after": [str(x).strip().upper() for x in (b.get("friendly_after") or [])],
                "sequence": [str(x).strip().upper() for x in (b.get("sequence") or [])],
                "earliest_hour": b.get("earliest_hour"),
                "prep_batches": int(b.get("prep_lot_batches", 5)),
            })
        self.block_items = {x for b in self.blocks for x in b["sequence"]}
        self.stats = defaultdict(int)
        self.waits = defaultdict(int)
        self.result: dict = {}            # key -> (m, start, finish, co_min)
        self.prep: list = []              # prep lots made
        self.runs: list = []              # (item, m, start, finish, n_lots, kind)

    # ------------------------------------------------------------- rules
    def U(self, item):
        return str(item or "").strip().upper()

    def is_final(self, item):
        return "FINAL" in str(self.ctx["itype_of"](item)).upper()

    def is_master(self, item):
        return "MASTER" in str(self.ctx["itype_of"](item)).upper()

    def same_campaign(self, a, b):
        return any(self.U(a) in c and self.U(b) in c for c in self.campaigns)

    def co_s_of(self, m, a, b) -> float:
        """README 5.1: the largest applicable time, + 1 s. 0 for the same item."""
        if not a or self.U(a) == self.U(b):
            return 0.0
        c = self.co_s
        if (self.is_master(a) and self.is_final(b)) or (self.is_final(a) and self.is_master(b)):
            c = max(c, self.co_s + self.tr100_s)                       # BRM-06 TR100
        A, B = self.U(a), self.U(b)
        if A in self.butyl and not self.same_campaign(A, B) \
                and B not in self.butyl and B not in self.friendly:
            c = max(c, self.clean_s)                                   # BRM-03
        if str(m) == self.k4 and (self.is_final(a) or self.is_final(b)):
            c = max(c, self.k4_s)                                      # M4
        if self.is_final(a):
            c = max(c, self.final_after_s)                             # M9
        return c + 1.0

    def dur(self, k, m):
        f = self.factor.get((self.U(self.L[k]["item"]), str(m)), 1.0)
        # v46j: the lot's own time on THIS mixer (all_machine_spec cycle + batch)
        d = (self.L[k].get("dur_by_m") or {}).get(str(m))
        return (d if d is not None else self.L[k]["dur"]) * f

    def is_long(self, it):
        return self.U(it) in self.long_items or self.U(it) in self.fourrc

    def gap_of(self, it):
        return self.gap_long if self.is_long(it) else self.gap

    def cover_of(self, it):
        return self.cover_long if self.is_long(it) else self.cover

    def priority(self, it):
        p = self.prio.get(self.U(it), pd.Timedelta(0))
        if self.U(it) in self.fourrc:
            p = max(p, self.fourrc_h)
        return p

    def ready(self, k):
        r = self.L[k]["ready"]
        for p in self.L[k].get("preds", ()):
            if p not in self.result:
                return None                           # input not planned yet
            f = self.result[p][2] + self.L[p]["min_age"] + self.ctx["transfer"]
            r = max(r, f)
        return r

    def shelf_floor(self, k, m):
        """Earliest START so the lot is still in date at its last use."""
        ma = self.L[k].get("max_age")
        if ma is None:
            return None
        return self.L[k]["last_use"] - (ma - self.margin) - self.dur(k, m)

    # ------------------------------------------------------------- the run
    def take_run(self, it, m, t, covered=None):
        """Plan a run of compound `it` on m starting no earlier than t."""
        ks = self.pend[it]
        k0 = ks[0]
        cm = self.co_s_of(m, self.last[m], it) / 60.0
        r0 = self.ready(k0) or t
        sf = self.shelf_floor(k0, m)
        earliest = max(t, r0 - pd.Timedelta(minutes=cm),
                       (sf - pd.Timedelta(minutes=cm)) if sf is not None else t)
        s = self.ctx["legal_start"](m, earliest, cm, self.dur(k0, m))
        horizon = self.L[k0]["need"] + (covered or self.cover_of(it))
        taken, cur, first = [], s, True
        for k in list(ks):
            if not first:
                if self.L[k]["need"] > horizon or m not in self.L[k]["elig"]:
                    break
                rk = self.ready(k)
                if rk is None or rk > cur:
                    break
                sfk = self.shelf_floor(k, m)
                if sfk is not None and sfk > cur:
                    break
            d = self.dur(k, m)
            st = cur if not first else s
            st = self.ctx["legal_start"](m, st, 0.0, d) if not first else st
            self.result[k] = (m, st, st + d, cm if first else 0.0)
            taken.append(k)
            cur = st + d
            first = False
        for k in taken:
            ks.remove(k)
        pd0 = self.ctx["plant_day"](s)
        pd1 = self.ctx["plant_day"](cur - ONE_S)
        day = pd0
        while day <= pd1:
            self.daylock[(self.U(it), day)] = m
            day += pd.Timedelta(days=1)
        self.last_run[self.U(it)] = s
        self.last[m] = it
        self.free[m] = cur
        self.runs.append((it, m, s, cur, len(taken), "run"))
        self.stats["runs"] += 1
        self.stats["lots"] += len(taken)
        return cur

    def run_prep(self, it, m, t, line, batches):
        p = self.ctx["make_prep"](it, m, line, batches)
        if not p:
            return None
        cm = self.co_s_of(m, self.last[m], it) / 60.0
        s = self.ctx["legal_start"](m, t, cm, p["dur"])
        f = s + p["dur"]
        p.update(machine=m, start=s, finish=f, co_min=cm)
        self.prep.append(p)
        self.last[m] = it
        self.free[m] = f
        self.runs.append((it, m, s, f, 1, "prep"))
        self.stats["prep_lots"] += 1
        return f

    # ------------------------------------------------------------- blocks
    def block_due(self, b, t):
        """Butyl items of block b that are due now (or wait <= gap_wait)."""
        due = []
        for it in b["sequence"]:
            ks = self.pend.get(it) or []
            if not ks:
                continue
            k0 = ks[0]
            if self.L[k0]["need"] > t + self.cover_long:
                continue
            lr = self.last_run.get(it)
            if lr is not None and t < lr + self.gap_of(it):
                if lr + self.gap_of(it) - t <= self.gap_wait:
                    due.append((it, lr + self.gap_of(it)))
                    self.stats["butyl_gap_waits"] += 1
                continue
            due.append((it, t))
        return due

    def run_block(self, b, m, t, due):
        line = self.L[self.pend[due[0][0]][0]]["line"]
        # inputs ready (start_when_ready): every first lot's inputs planned
        starts = []
        for it, _ in due:
            r = self.ready(self.pend[it][0])
            if r is None:
                return None
            starts.append(r)
        t = max([t] + starts)
        # friendly BEFORE: a real friendly lot if one is pending and allowed,
        # otherwise a prep lot
        fr = next((f for f in b["before"] if self.pend.get(f)
                   and m in self.L[self.pend[f][0]]["elig"]), None)
        if fr:
            t = self.take_run(fr, m, t)
            self.stats["friendly_real"] += 1
        elif b["before"]:
            for f in b["before"]:
                tt = self.run_prep(f, m, t, line, b["prep_batches"])
                if tt is not None:
                    t = tt
                    break
        for it, at in due:
            if not self.pend.get(it):
                continue
            t = self.take_run(it, m, max(t, at), self.cover_long)
        if b["after"]:
            fr = next((f for f in b["after"] if self.pend.get(f)
                       and m in self.L[self.pend[f][0]]["elig"]), None)
            if fr:
                t = self.take_run(fr, m, t)
            else:
                for f in b["after"]:
                    tt = self.run_prep(f, m, t, line, b["prep_batches"])
                    if tt is not None:
                        t = tt
                        break
        self.stats["blocks"] += 1
        return t

    # ------------------------------------------------------------- plan
    def plan(self):
        L = self.L
        self.pend = defaultdict(list)
        for k in sorted(L, key=lambda k: (L[k]["need"], k)):
            self.pend[self.U(L[k]["item"])].append(k)
        mixers = sorted({m for k in L for m in L[k]["elig"]})
        open_at = self.ctx["open_at"]
        self.free = {m: open_at for m in mixers}
        self.last = {m: None for m in mixers}
        self.daylock, self.last_run, self.done_blocks = {}, {}, set()
        heap = [(open_at, m) for m in mixers]
        heapq.heapify(heap)
        guard = 0
        while heap and any(self.pend.values()):
            guard += 1
            if guard > 200000:
                self.stats["guard_stop"] += 1
                break
            t, m = heapq.heappop(heap)
            if t < self.free[m]:
                heapq.heappush(heap, (self.free[m], m))
                continue
            pday = self.ctx["plant_day"](t)
            # ---- 1. butyl block --------------------------------------------
            ran = False
            for b in self.blocks:
                if b["mixer"] != str(m) or (b["name"], pday) in self.done_blocks:
                    continue
                eh = b.get("earliest_hour")
                if eh is not None:
                    # plant_day() is the plant DATE at midnight; the plant
                    # day itself runs 07:00 -> 07:00
                    gate = pday + pd.Timedelta(hours=7.0 + (float(eh) - 7.0) % 24)
                    if t < gate:
                        due = self.block_due(b, gate)
                        if due:
                            heapq.heappush(heap, (gate, m))
                        continue
                due = self.block_due(b, t)
                if not due:
                    continue
                f = self.run_block(b, m, t, due)
                if f is None:
                    continue
                self.done_blocks.add((b["name"], pday))
                heapq.heappush(heap, (self.free[m], m))
                ran = True
                break
            if ran:
                continue
            # ---- 2. candidates --------------------------------------------
            cand, nxt = [], []
            fol = self.followers.get(self.U(self.last[m]))
            for it, ks in self.pend.items():
                if not ks or it in self.block_items:
                    continue
                k0 = ks[0]
                if str(m) not in L[k0]["elig"]:
                    continue
                lk = self.daylock.get((it, pday))
                if lk is not None and lk != m:
                    self.waits["daylock"] += 1
                    nxt.append(pday + pd.Timedelta(days=1, hours=7))
                    continue
                lr = self.last_run.get(it)
                if it not in self.exempt and lr is not None and t < lr + self.gap_of(it):
                    self.waits["repeat_gap"] += 1
                    nxt.append(lr + self.gap_of(it))
                    continue
                r = self.ready(k0)
                if r is None:
                    continue
                if r > t + self.max_wait:
                    self.waits["inputs"] += 1
                    nxt.append(r)
                    continue
                sf = self.shelf_floor(k0, m)
                if sf is not None and sf > t + self.max_wait:
                    self.waits["shelf_floor"] += 1
                    nxt.append(sf)
                    continue
                key = L[k0]["need"] - self.priority(it)
                if fol and it == fol:
                    key = pd.Timestamp.min                              # BRM-05
                cand.append((key, it))
            if not cand:
                later = [x for x in nxt if x > t]
                w = min(later) if later else t + pd.Timedelta(hours=1)
                self.stats["mixer_waits"] += 1
                heapq.heappush(heap, (w, m))
                continue
            cand.sort()
            it = cand[0][1]
            if fol and it == fol:
                self.stats["followers"] += 1
            self.take_run(it, m, t)
            heapq.heappush(heap, (self.free[m], m))
        self.stats["unplanned"] = sum(len(v) for v in self.pend.values())
        return self.result
