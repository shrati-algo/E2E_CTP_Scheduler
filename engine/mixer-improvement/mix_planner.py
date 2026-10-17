# -*- coding: utf-8 -*-
"""mix_planner.py -- stock-coverage mixing planner (v56, user 2026-10-09).

Plans ALL compound lots (master + final) on the mixers BEFORE the rest of phase 5 places anything, as a forward
event simulation. Phase 5 then places 901 / extruders / calenders / cutters / building around these FIXED lots and
the repair never moves them.

Decision each time a mixer becomes free (time t):
  1. the daily butyl blocks are candidates once per plant day on their mixer (F270: friendly -> IL3 -> IL5 ->
     120 s -> friendly -> 30G -> 120 s; K430-2: friendly -> 29MG -> 120 s -> friendly -> ML405 -> ... -> friendly);
  2. every other compound whose next lot is allowed on this mixer (routing), is not on another mixer this plant day
     (one mixer per day), has not started a run in the last 15 h (24 h IL / 4RC; butyl-friendly exempt), has its
     master lots planned and ready (finals) and is not too early for its shelf life;
  3. the candidate that can start now (or within a short wait) and is needed SOONEST runs (lowest coverage);
  4. the run takes every lot of that compound needed within the next 15 h / 24 h after the first one (separate lots,
     back to back, no changeover between), as long as each is still within shelf life at its last use and its own
     inputs are ready; the compound then does not come back within 15 h / 24 h;
  5. changeovers: 60 s, TR100 master <-> final 5 min, 120 s after a butyl compound (not inside an IL campaign),
     120 s before / after a final compound on K430-2; PM windows skipped.
The mixer only waits when nothing allowed can run (the reason is logged).
"""
from collections import defaultdict

import pandas as pd

H = 3600 * 10 ** 9
MIN_ = 60 * 10 ** 9


def plan(ctx):
    """ctx keys (from phase 5): lots (ids), lot_item, lot_item_type, lot_machines, duration_h, lot_first_need,
    lot_last_need, predecessors, ages_ns(item) -> (min_ns, max_ns), material_ready_ns(lot), downtime (machine ->
    [(a, b)]), commit(lot, machine, start_ns, finish_ns), make_prep(item, machine) -> lot or None, open_ns,
    long_items (set), exempt (set), groups (butyl_day_block groups), cfg (mix_planner config).
    Returns stats dict and the list of planned lot ids."""
    C = ctx["cfg"]
    W_SHORT, W_LONG = int(float(C.get("cover_h", 15)) * H), int(float(C.get("cover_h_long", 24)) * H)
    WAIT = int(float(C.get("max_wait_min", 15)) * MIN_)
    MARG = int(float(C.get("shelf_margin_h", 2)) * H)
    CO, TR100, CLEAN = int(float(C.get("changeover_s", 60)) * 1e9), int(float(C.get("tr100_s", 300)) * 1e9), \
        int(float(C.get("cleanout_s", 120)) * 1e9)
    li, lt, lm, du = ctx["lot_item"], ctx["lot_item_type"], ctx["lot_machines"], ctx["duration_h"]
    pred, ages, ready_fn = ctx["predecessors"], ctx["ages_ns"], ctx["material_ready_ns"]
    LONG, EXEMPT = ctx["long_items"], ctx["exempt"]
    MIXTYPES = ("MASTER COMPOUND", "FINAL COMPOUND")
    it = lambda l: str(li.get(l, "")).strip()
    typ = lambda l: str(lt.get(l, "")).upper()
    dur = lambda l: int(float(du.get(l, 0) or 0) * H)
    pday = lambda t: (int(t) - 7 * H) // (24 * H)
    # v91 (user 2026-10-13): per-mixer duration factor "ITEM|MIXER" -> x (a compound runs at a different kg/h on a mixer
    # outside its routing home, e.g. T638 on K430-2 at the plant's measured 335 kg / 205 s). Empty = every mixer the same.
    MF = {tuple(str(k).split("|", 1)): float(v) for k, v in (C.get("machine_factor") or {}).items()}
    FCO_AFTER = int(float(C.get("final_cleanout_after_s", 0) or 0) * 1e9)    # v93: 0 = v87 (no clean-out after finals)
    # v124 (user 2026-10-15): a due butyl item whose repeat gap ends within butyl_gap_wait_h of the block start is NOT
    # dropped from that day's block (it was, and then skipped the whole day: IL3 / 30G every 2nd day); the block waits
    # for the gap instead (friendly B99 placed right before the butyl). 24 h gap, one block a day, order kept. 0 = v122
    GWAIT = int(float(C.get("butyl_gap_wait_h", 0) or 0) * H)
    dur_m = lambda l, m: int(dur(l) * MF.get((it(l), str(m)), 1.0))

    FB = ctx.get("fb_need") or {}          # v69 #3: round-1 real need (later than plan when the consumer is late anyway)

    def nd(l):
        v = ctx["lot_first_need"].get(l)
        v = pd.Timestamp(v).value if (v is not None and not pd.isna(v)) else None
        f = FB.get(l)
        return v if f is None else (f if v is None else max(v, f))

    def last(l):
        v = ctx["lot_last_need"].get(l)
        return int(v) if v is not None else nd(l)

    # ---- the compound lots to plan
    L = [l for l in ctx["lots"] if typ(l) in MIXTYPES and lm.get(l)]
    if not L:
        return {"planned": 0}, [], []
    mixers = sorted({str(m) for l in L for m in lm[l]})
    groups = []
    for gname, g in (ctx["groups"] or {}).items():
        groups.append({"name": gname, "m": str(g.get("mixer")), "butyl": [str(x) for x in g.get("butyl") or []],
                       "friendly": [str(x) for x in g.get("friendly") or []],
                       "after": bool(g.get("friendly_after", False)),
                       "mode": str(g.get("friendly_mode", "each")).lower(),        # v61: "block" = friendly at start / end only
                       "hour": g.get("earliest_hour"),                            # v61: block not before this clock hour
                       "ready_gate": bool(g.get("start_when_ready", False)),      # v61: block starts when its butyl is ready
                       "camp": [[str(y) for y in c] for c in (g.get("campaigns") or [])]})
    BUTYL = {b: G for G in groups for b in G["butyl"]}
    FRIEND = {f: G for G in groups for f in G["friendly"]}
    PIN = {**{b: G["m"] for b, G in BUTYL.items()}, **{f: G["m"] for f, G in FRIEND.items()}}
    CAMP = {c: (G["name"], i) for G in groups for i, cs in enumerate(G["camp"]) for c in cs}
    # v136 (user 2026-10-15): cover_long_items -- these compounds plan a run to cover cover_h_long (24 h) of need while
    # their repeat GAP stays the normal 15 h rule (T639: the 13-Oct run covered 15 h, the next run was gap-blocked, 1801 idle)
    COVER_LONG = {str(x).strip() for x in (C.get("cover_long_items") or [])}
    # v137 (user 2026-10-15): cover_items_h {item: hours} -- a run of that compound covers this many hours of need
    # (T638 30 h: the 07:00 day-1 run also makes the next day's T638); the repeat gap is unchanged
    COVER_H = {str(k).strip(): int(float(v) * H) for k, v in (C.get("cover_items_h") or {}).items()}
    W = lambda item: COVER_H.get(item) or (W_LONG if (item in LONG or item in COVER_LONG) else W_SHORT)   # cover window
    # v137: run_join_wait {items, h} -- a run of a listed compound may START up to h later so the next lots of its cover
    # window whose inputs become ready within h are made in the same go (T639 13-Oct: 02:45 run stopped, next run
    # gap-blocked to 17:48). Gap, one-mixer-a-day, shelf life and min aging unchanged. Absent = v133
    _RJ = C.get("run_join_wait") or {}
    RJ_ITEMS = {str(x).strip() for x in (_RJ.get("items") or [])}
    RJ_H = int(float(_RJ.get("h", 0) or 0) * H)
    # v63 (user 2026-10-10): the repeat GAP (rule: 15 h / 24 h) is separate from the run's COVER (20 h / 30 h) -- a run covers
    # longer than the gap so the next run (allowed only after the gap, usable after run time + min aging) never leaves the
    # next section without compound
    G_SHORT = int(float(C.get("gap_h", C.get("cover_h", 15))) * H)
    G_LONG = int(float(C.get("gap_h_long", C.get("cover_h_long", 24))) * H)
    GAP0 = lambda item: G_LONG if item in LONG else G_SHORT
    # v94 (user 2026-10-13, RULE RELAXED): repeat gap for the 4RC skim compounds shortened (gap_h_fourrc, e.g. 12 h
    # instead of 24 h); None / absent = the 15 h / 24 h rule
    _G4 = C.get("gap_h_fourrc")
    _G4 = int(float(_G4) * H) if _G4 not in (None, "", 0) else None
    _F4 = set(ctx.get("fourrc_items") or ())
    GAP = lambda item: _G4 if (_G4 is not None and item in _F4) else GAP0(item)
    # v94 (user 2026-10-13, RULE RELAXED): a compound may use a SECOND mixer on the same plant day when its next lot is
    # needed within second_mixer_cover_h (cover short); 0 = one mixer per compound per plant day
    SECOND_H = int(float(C.get("second_mixer_cover_h", 0) or 0) * H)
    BUCKET = int(float(C.get("urgency_bucket_h", 4)) * H)
    FRC_ITEMS = set(ctx.get("fourrc_items") or ())
    FRC_PRI = int(float(C.get("fourrc_priority_h", 0)) * H)    # v58: 4RC skim compounds (and their masters) ranked ahead
    XPRI = {str(k).strip(): int(float(v) * H) for k, v in (C.get("item_priority_h") or {}).items()}   # v60: extra per item
    # v118 (user 2026-10-14): START-UP priority -- listed compounds ranked ahead only for needs in the first window_h of
    # the plan (TBR 1801 / 1901 compounds late on 12-14 Oct); later days unchanged (v107 ranked them every day: PCR lost)
    _SU = C.get("startup_priority") or {}
    SPRI = ({str(k).strip(): int(float(v) * H) for k, v in (_SU.get("items") or {}).items()}
            if _SU.get("enabled", False) else {})
    SU_END = int(ctx["open_ns"]) + int(float(_SU.get("window_h", 36)) * H)
    spri = lambda x: SPRI.get(it(x), 0) if (SPRI and effn(x) is not None and effn(x) < SU_END) else 0

    # master urgency = the earliest consumer final's need minus that final's cover window and the master's min aging,
    # so the masters of a whole final run are ready before the run starts (masters ahead of finals)
    succ = defaultdict(list)
    Lset = set(L)
    for c in L:
        for p in pred.get(c, ()):
            if p in Lset:
                succ[p].append(c)
    # v62 (user 2026-10-10): LOAD LEVELLING -- a backward finite-capacity pass over the final (then master) mixers,
    # from the latest need back: when a day's need is more than the mixers can make that day, the excess gets an
    # earlier effective need so it is made on the days before (only as early as shelf life allows -- the shelf-life
    # floor in the candidate check still applies). Urgency only; the 15 h / 24 h cover runs still use the real need.
    LV = C.get("level_load") or {}

    def _level(lots, key, n_mach):
        out, t = {}, None
        for l in sorted(lots, key=key, reverse=True):
            k = key(l)
            f = k if t is None else min(k, t)
            out[l] = f
            t = f - dur(l) // max(int(n_mach), 1)
        return out
    EFF = {}
    if LV.get("enabled", False):
        _fin = [l for l in L if typ(l) == "FINAL COMPOUND" and nd(l) is not None]
        EFF = _level(_fin, nd, LV.get("final_mixers", 2))
    DUE = ctx.get("due_need") or {}                                    # v68: 901-driven need of skim compounds

    def effn(l):
        v = EFF.get(l, nd(l))
        d = DUE.get(l)
        return v if d is None else (d if v is None else min(v, d))
    URG = {}
    for l in L:
        u = effn(l) if (nd(l) is not None or l in DUE) else 2 ** 62
        if it(l) in FRC_ITEMS and nd(l) is not None and l not in DUE:
            u -= FRC_PRI                                   # flat 4RC priority only where no 901-driven due exists
        if it(l) in XPRI and nd(l) is not None:
            u -= XPRI[it(l)]
        if nd(l) is not None:
            u -= spri(l)                                   # v118: start-up window only (0 when off)
        for c in succ.get(l, ()):
            if effn(c) is not None:
                u = min(u, effn(c) - W(it(c)) - int(ages(it(l))[0]) - (FRC_PRI if it(c) in FRC_ITEMS else 0)
                        - XPRI.get(it(c), 0) - spri(c))
        URG[l] = u
    if LV.get("enabled", False) and LV.get("masters", True):
        _mas = [l for l in L if typ(l) == "MASTER COMPOUND" and URG[l] < 2 ** 61]
        for l, f in _level(_mas, lambda x: URG[x], LV.get("master_mixers", 4)).items():
            URG[l] = min(URG[l], f)
    stat_lv = sum(1 for l in EFF if EFF[l] < nd(l))
    by_item = defaultdict(list)
    for l in L:
        by_item[it(l)].append(l)
    for k in by_item:
        by_item[k].sort(key=lambda l: (nd(l) if nd(l) is not None else 2 ** 62, l))
    planned = set()
    mixset = set(L)
    free = {m: int(ctx["open_ns"]) for m in mixers}
    lastit = {m: "" for m in mixers}
    daylock = {}
    runstart = {}
    blockday = {}
    blockready = {}
    stat = defaultdict(float)
    stat["levelled_final_lots"] = stat_lv if LV.get("enabled", False) else 0
    waits = []

    def allowed_m(l):
        ms = [str(x) for x in lm[l]]
        p = PIN.get(it(l))
        return [p] if (p and p in ms) else ms

    def co(m, a, b, ta, tb):
        if not a or a == b:
            return 0
        c = CO
        if {ta, tb} == set(MIXTYPES):
            c = max(c, CO + TR100)                          # TR100 clean-out on top of the changeover
        if a in BUTYL and not (a in CAMP and CAMP.get(a) == CAMP.get(b)):
            # v67 (user 2026-10-10): butyl compounds do not react with each other or with a butyl-friendly -> only the
            # normal changeover; the 120 s clean-out REPLACES the changeover only when butyl -> any other compound
            if not (CLEAN_ONLY_OTHER and (b in BUTYL or b in FRIEND)):
                c = max(c, CLEAN)
        if m == "K430-2" and "FINAL COMPOUND" in (ta, tb):
            c = max(c, CLEAN)
        if FCO_AFTER and ta == "FINAL COMPOUND":            # v93 (user 2026-10-13): clean-out after EVERY final compound
            c = max(c, FCO_AFTER)
        return c + 10 ** 9                                  # +1 s margin

    def skip_dn(m, s, d):
        moved = True
        while moved:
            moved = False
            for a, b in sorted(ctx["downtime"].get(m, ())):
                if s < b and s + d > a:
                    s, moved = b, True
        return s

    def inputs_ok(l):
        return all((p not in mixset) or (p in planned) for p in pred.get(l, ()))

    def ready(l):
        r = ready_fn(l)
        return int(ctx["open_ns"]) if r is None or r < -10 ** 17 else max(int(r), int(ctx["open_ns"]))

    def shelf_floor(l):
        mx = ages(it(l))[1]
        lv = last(l)
        if not mx or mx <= 0 or lv is None:
            return -2 ** 62
        return lv - (mx - MARG) - dur(l)

    def place(l, m, s):
        f = s + dur_m(l, m)
        ctx["commit"](l, m, s, f)
        planned.add(l)
        by_item[it(l)].remove(l)
        return f

    def lock(item, m, s, f):
        for d_ in range(pday(s), pday(max(s, f - 1)) + 1):
            daylock[(item, d_)] = m

    def lockable(item, m, s):
        lk = daylock.get((item, pday(s)))
        return lk is None or lk == m

    def friendly_lot(G, m, t, prev_f):
        """a real friendly lot of the group (ready, allowed on m), else a prep lot of the group's friendly item."""
        best = None
        for f in sorted(G["friendly"], key=lambda x: x == prev_f):
            if best is not None and f == prev_f:
                break
            for l in by_item.get(f, []):
                if m in allowed_m(l) and inputs_ok(l) and ready(l) <= t + WAIT and lockable(f, m, t):
                    k = (nd(l) or 2 ** 62)
                    if best is None or k < best[0]:
                        best = (k, l)
                    break
        if best:
            return best[1]
        # prep lot: prefer a different friendly item than the one just used, but use the same one if it is the only one
        # that can be made (a butyl compound must ALWAYS have a friendly compound directly before / after it)
        for f in sorted(G["friendly"], key=lambda x: x == prev_f):
            p = ctx["make_prep"](f, m)
            if p:
                mixset.add(p)
                by_item[f].insert(0, p)
                stat["prep_lots"] += 1
                return p
        stat["no_friendly_possible"] += 1
        return None

    def block_candidate(G, m, t):
        """the group's daily butyl block, if due today on this mixer: list of butyl items to include."""
        if G["m"] != m or blockday.get((G["name"], pday(t))):
            return None
        due = []
        for b in G["butyl"]:
            Ls = [l for l in by_item.get(b, []) if inputs_ok(l)]
            if not Ls:
                continue
            n0 = nd(Ls[0])
            if n0 is not None and n0 > t + W(b) + 24 * H:
                continue                                   # not needed for more than a day
            if runstart.get(b, -2 ** 62) + GAP(b) > t + GWAIT:
                continue                                   # repeat gap (v124: unless it ends within GWAIT -> wait)
            if runstart.get(b, -2 ** 62) + GAP(b) > t:
                stat["butyl_gap_waits"] += 1
            if not lockable(b, m, t):
                continue
            due.append((n0 if n0 is not None else 2 ** 62, b))
        if not due:
            return None
        if G.get("hour") is not None:                       # v61: butyl masters in the evening (age overnight)
            hh = (int(t) // H) % 24                          # plant clock hour (naive local timestamps)
            if hh < int(G["hour"]):
                return None
        if G.get("ready_gate"):                             # v61: start only when the first butyl lot can start at once
            r0 = max(ready(next(l for l in by_item[b] if inputs_ok(l))) for _, b in due)   # ALL its butyl ready
            if r0 > t + WAIT:
                blockready[G["name"]] = r0
                return None
        return sorted(due)

    def run_block(G, m, t, due):
        """friendly -> butyl unit -> (clean) -> friendly -> next unit ... ; campaign items back to back."""
        units, seen = [], set()
        order = [b for _, b in due]
        for b in order:
            if b in seen:
                continue
            ci = CAMP.get(b)
            u = [x for x in order if CAMP.get(x) == ci and ci is not None] if ci is not None else [b]
            u = sorted(u, key=lambda x: order.index(x))
            seen |= set(u)
            units.append(u)
        s, prev, prev_f = t, lastit[m], ""
        first_start = None
        gend = {b: runstart.get(b, -2 ** 62) + GAP(b) for b in order}     # v124: repeat-gap end per butyl item
        for ui, u in enumerate(units):
            fl = friendly_lot(G, m, s, prev_f) if (G["mode"] != "block" or ui == 0) else None
            if fl is not None:
                tf = it(fl)
                _g0 = max(gend.get(x, -2 ** 62) for x in u) - dur(fl)      # v124: friendly ends as the gap ends
                s = skip_dn(m, max(s + co(m, prev, tf, typ_item(prev), typ(fl)), ready(fl), _g0), dur(fl))
                first_start = s if first_start is None else first_start
                f = place(fl, m, s)
                lock(tf, m, s, f)
                prev, prev_f, s = tf, tf, f
            for b in u:
                Ls = [l for l in by_item.get(b, []) if inputs_ok(l)]
                if not Ls:
                    continue
                n0 = nd(Ls[0]) or s
                rs = None
                for l in list(Ls):
                    if l is not Ls[0] and (nd(l) or s) >= n0 + W(b):
                        break
                    s2 = skip_dn(m, max(s + co(m, prev, b, typ_item(prev), typ(l)), ready(l),
                                        gend.get(b, -2 ** 62) if l is Ls[0] else -2 ** 62), dur(l))
                    if l is not Ls[0] and s2 > s + WAIT:
                        break                              # inputs of the next lot not ready: run ends here
                    if rs is None:
                        rs = s2
                    f = place(l, m, s2)
                    prev, s = b, f
                if rs is not None:
                    runstart[b] = rs
                    lock(b, m, rs, s)
                    first_start = rs if first_start is None else first_start
        if G["after"]:
            fl = friendly_lot(G, m, s, prev_f)
            if fl is not None:
                tf = it(fl)
                s = skip_dn(m, max(s + co(m, prev, tf, typ_item(prev), typ(fl)), ready(fl)), dur(fl))
                f = place(fl, m, s)
                lock(tf, m, s, f)
                prev, s = tf, f
        blockday[(G["name"], pday(first_start if first_start is not None else t))] = True
        stat["butyl_blocks"] += 1
        return s, prev

    SB = C.get("starve_boost") or {}
    SB_H = int(float(SB.get("horizon_h", 3)) * H)
    SB_N = int(SB.get("top_finals", 6))
    SB_BOOST = int(float(SB.get("boost_h", 48)) * H)
    FINAL_MIX = {str(x) for x in (SB.get("final_mixers") or ["F270", "K310"])}
    FINAL_ITEMS = sorted({it(l) for l in L if typ(l) == "FINAL COMPOUND"})
    CLEAN_ONLY_OTHER = str(C.get("butyl_cleanout_to", "any")).lower() == "other"   # v67
    _FMF = C.get("final_mixer_first") or {}                                         # v103 (default off = v98)
    FMF_ON = bool(_FMF.get("enabled", False))
    FMF_MARGIN = int(float(_FMF.get("margin_h", 2)) * H)
    if FMF_ON and _FMF.get("final_mixers"):
        FINAL_MIX = {str(x) for x in _FMF["final_mixers"]}
    TYPE_OF_ITEM = {}
    for l in L:
        TYPE_OF_ITEM.setdefault(it(l), typ(l))
    typ_item = lambda x: TYPE_OF_ITEM.get(x, "")

    guard = 0
    while any(by_item.values()) and guard < 400000:
        guard += 1
        m = min(mixers, key=lambda x: free[x])
        t = free[m]
        if t > int(ctx["open_ns"]) + 60 * 24 * H:
            break
        # 1) daily butyl block on this mixer
        did = False
        for G in groups:
            due = block_candidate(G, m, t)
            if due:
                end, prev = run_block(G, m, t, due)
                if end > t:
                    free[m], lastit[m] = end, prev
                    did = True
                    break
        if did:
            continue
        # 2) candidates
        cands = []
        nxt = []
        why = defaultdict(int)
        # v66 (user 2026-10-10): LIVE MASTER PRIORITY -- when a final mixer runs out of planned work within horizon_h,
        # the masters that block the most urgent waiting final lots go to the front on this master mixer (urgency only;
        # every rule check below still applies)
        NEED = set()
        if SB.get("enabled", False) and m not in FINAL_MIX and any(free[fm] <= t + SB_H for fm in FINAL_MIX):
            waiting = []
            for fi in FINAL_ITEMS:
                Ls_ = by_item.get(fi)
                if not Ls_:
                    continue
                l_ = Ls_[0]
                if not inputs_ok(l_):
                    waiting.append((URG.get(l_, 2 ** 62), l_))
            for _, l_ in sorted(waiting)[:SB_N]:
                for p_ in pred.get(l_, ()):
                    if p_ in mixset and p_ not in planned:
                        NEED.add(it(p_))
            if NEED:
                stat["starve_boost_decisions"] += 1
        for item, Ls in by_item.items():
            if not Ls or item in BUTYL:
                continue
            if m not in allowed_m(Ls[0]):
                continue
            l0 = next((l for l in Ls if inputs_ok(l)), None)
            if l0 is None:
                why["master not planned yet"] += 1
                continue
            _short = bool(SECOND_H) and nd(l0) is not None and nd(l0) - t <= SECOND_H
            if not lockable(item, m, t) and not _short:
                why["on another mixer today"] += 1
                continue
            if not lockable(item, m, t):
                stat["second_mixer_used"] += 1           # v94: cover short -> second mixer allowed (rule relaxed)
            if item not in EXEMPT and runstart.get(item, -2 ** 62) + GAP(item) > t:
                nxt.append(runstart[item] + GAP(item))
                stat["cand_blocked_by_gap"] += 1
                why["15 h / 24 h gap"] += 1
                continue
            s0 = max(t + co(m, lastit[m], item, typ_item(lastit[m]), typ(l0)), ready(l0), shelf_floor(l0))
            if s0 > t + WAIT + co(m, lastit[m], item, typ_item(lastit[m]), typ(l0)):
                nxt.append(s0)
                why["shelf-life floor (too early)" if shelf_floor(l0) >= max(ready(l0), t) else "master not ready (aging)"] += 1
                continue
            if not lockable(item, m, s0) and not _short:
                continue
            # v103 (user 2026-10-14): FINALS ON THEIR OWN FINAL MIXERS FIRST -- a master mixer takes a final compound
            # only when no routed final mixer (F270 / K310) can make it in time (free + run + margin > need time);
            # otherwise it is left for the final mixer (all mixing rules still checked there)
            if FMF_ON and m not in FINAL_MIX and typ(l0) == "FINAL COMPOUND":
                _fms = [x for x in FINAL_MIX if x in allowed_m(l0)]
                if _fms:
                    _n0 = nd(l0)
                    _fs = min(max(free[x], ready(l0)) + dur_m(l0, x) for x in _fms)
                    if _n0 is None or _fs + FMF_MARGIN <= _n0:
                        why["final mixer can make it in time (final mixers first)"] += 1
                        stat["fmf_left_to_final_mixer"] += 1
                        continue
                    stat["fmf_overflow_to_master"] += 1
            _u = URG.get(l0, nd(l0) if nd(l0) is not None else 2 ** 62)
            if item in NEED:
                _u -= SB_BOOST                             # v66: a final mixer is about to wait for this master
            # (tested 09-Oct: shortest-first inside a 4 h urgency bucket -- no gain, 1,297 vs 1,301 h; kept off)
            cands.append((_u, 0, s0, item, l0))
        if not cands:
            later = [x for x in nxt if x > t]
            t2 = min([t + WAIT] + later) if later else t + WAIT
            others = [free[x] for x in mixers if x != m and free[x] > t]
            if others:
                t2 = min(t2, min(others))
            t2 = max(t2, t + MIN_)
            waits.append((m, t, t2, "; ".join(f"{k} {v}" for k, v in sorted(why.items(), key=lambda kv: -kv[1]))))
            stat["idle_h"] += (t2 - t) / H
            free[m] = t2
            continue
        cands.sort()
        _, _, s0, item, l0 = cands[0]
        if RJ_H and item in RJ_ITEMS:                       # v137: start later so the next lots join this go
            _acc, _n0, _snew = 0, nd(l0) or s0, s0
            for _j, _l in enumerate(by_item[item][by_item[item].index(l0):]):
                if _j and (nd(_l) or s0) >= _n0 + W(item):
                    break
                if not inputs_ok(_l):
                    break
                _r = ready(_l)
                if _r > s0 + RJ_H:
                    break
                _snew = max(_snew, _r - _acc)
                _acc += dur_m(_l, m)
            if _snew > s0 and lockable(item, m, _snew):
                stat["run_join_waits"] += 1
                stat["run_join_wait_h"] += (_snew - s0) / H
                s0 = _snew
        # 3) the run: every lot of the compound needed within the cover window, back to back
        s = skip_dn(m, s0, dur_m(l0, m))
        rs, n0, prev, k = s, nd(l0) or s, lastit[m], 0
        _seq = by_item[item][by_item[item].index(l0):]
        for l in list(_seq):
            if not inputs_ok(l):
                stat["stop_inputs_not_planned"] += 1
                break
            if k and (nd(l) or s) >= n0 + W(item):
                stat["stop_cover_reached"] += 1
                break
            if k:
                s2 = skip_dn(m, max(s, ready(l)), dur_m(l, m))
                if s2 > s + WAIT:
                    stat["stop_inputs_not_ready"] += 1
                    break
                mx = ages(item)[1]
                if mx and mx > 0 and last(l) is not None and s2 + dur_m(l, m) + mx - MARG < last(l):
                    stat["stop_shelf_life"] += 1
                    break                                  # would age out before its last use: later run
                s = s2
            f = place(l, m, s)
            s, k = f, k + 1
        stat["runs"] += 1
        stat["lots_in_runs"] += k
        runstart[item] = rs
        lock(item, m, rs, s)
        free[m], lastit[m] = s, item
    stat["planned"] = len(planned)
    stat["left_unplanned"] = sum(len(v) for v in by_item.values())
    stat["waits"] = len(waits)
    return dict(stat), sorted(planned), waits
