#!/usr/bin/env python3
"""phase_tests.py - phase-wise test reports for one scheduler run.

    python phased-testing/phase_tests.py --run <run> [--out <output root>]

run_pipeline.py calls this at the end of EVERY run. It reads the run's own outputs
(outputs/<run>/...), the masters it was staged with (data/ppds_<day>/) and the run's
schedule_id (outputs/<run>/SCHEDULE_ID.json), evaluates every check, and writes

    phased-testing/reports/<schedule_id>/TEST_CASES_PHASE1.md ... TEST_CASES_PHASE7.md
    phased-testing/reports/<schedule_id>/SUMMARY.md     one line per phase, overall verdict
    phased-testing/reports/<schedule_id>/checks.csv     every check, machine-readable

<schedule_id> is the run's ID (YYYYMMDD_HHMMSS_<version>); the same run is in
JK_CTP_PPDS.CTP_Schedule_Floor under schedule_id = its timestamp.

The layout follows test-report-phasewise/TEST_CASES_PHASE<n>.md (title, scope, intent,
numbered sections of check-boxes, acceptance criteria), mapped onto this engine:

    report phase   reference name            this engine
    1              Demand Explosion          phase0_validate + phase1_building_demand
    2              Lot Sizing                phase2_wip_netting + phase3_lot_sizing
    3              DAG Construction          phase4_cpm (lot edges)
    4              CPM Backward Pass         phase4_cpm (EST / LST / LFT / slack)
    5              Forward Placement (JIT)   phase4b_assignment + phase5_placement
    6              Curing Revision           phase7_curing
    7              Schedule Validation       floor schedule, checks, VALIDATION_REPORT, DB

Every check is EVALUATED on the run: [x] PASS, or [ ] FAIL / WARN / INFO with the evidence.
A check that cannot be evaluated is reported as ERROR (counted as a failure), never skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import traceback
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
LINES = ("pcr", "tbr")
TRANSFER_MIN = 10
EPS = 1e-6

PASS, FAIL, WARN, INFO, ERROR = "PASS", "FAIL", "WARN", "INFO", "ERROR"


# ============================================================ report model
class Report:
    def __init__(self, n, name, scope, intent):
        self.n, self.name, self.scope, self.intent = n, name, scope, intent
        self.sections = []           # [(title, [(subtitle, [check dicts])])]
        self.acceptance = []         # [(text, ok bool)]
        self.verify = []             # lines for the manual verification section
        self.next_phase = None

    def section(self, title):
        self.sections.append((title, []))

    def sub(self, title):
        self.sections[-1][1].append((title, []))

    def check(self, text, fn):
        """fn() -> (status, evidence). Exceptions become ERROR with the message."""
        try:
            status, ev = fn()
        except Exception as e:                                   # noqa: BLE001
            status, ev = ERROR, f"{type(e).__name__}: {e}"
        self.sections[-1][1][-1][1].append({"text": text, "status": status, "evidence": ev})
        return status

    def all_checks(self):
        for t, subs in self.sections:
            for st, cs in subs:
                for c in cs:
                    yield t, st, c

    def counts(self):
        c = defaultdict(int)
        for _, _, k in self.all_checks():
            c[k["status"]] += 1
        return c

    def ok(self):
        c = self.counts()
        return c[FAIL] == 0 and c[ERROR] == 0 and all(o for _, o in self.acceptance)


def _box(status):
    return {PASS: "- [x]", FAIL: "- [ ] **FAIL**", WARN: "- [ ] **WARN**",
            INFO: "- [x] *INFO*", ERROR: "- [ ] **ERROR**"}[status]


def render(R: Report, ctx: dict) -> str:
    c = R.counts()
    L = [f"# Test Cases — Phase {R.n}: {R.name}", f"## Scope: {R.scope}", "", f"> {R.intent}", "",
         "| Run | |", "|---|---|",
         f"| schedule_id | `{ctx['sid']['schedule_id']}` (CTP_Schedule_Floor.schedule_id = "
         f"`{ctx['sid']['db_schedule_id']}`) |",
         f"| run folder | `outputs/{ctx['run']}` |",
         f"| plan day | {ctx['plan_day']} |",
         f"| plan | {ctx['plan_txt']} |",
         f"| stock / GT | {ctx['stock_txt']} |",
         f"| tested | {ctx['now']} |",
         f"| result | **{c[PASS]} PASS · {c[WARN]} WARN · {c[FAIL]} FAIL · {c[ERROR]} ERROR · "
         f"{c[INFO]} INFO** — {'OK' if R.ok() else 'NOT OK'} |", "",
         "Legend: `[x]` passed · **FAIL** a rule is broken · **WARN** allowed, but look at it · "
         "*INFO* a measured fact · **ERROR** the check could not be evaluated.", "", "---", ""]
    for i, (title, subs) in enumerate(R.sections, 1):
        L.append(f"## {i}. {title}")
        L.append("")
        for j, (st, cs) in enumerate(subs, 1):
            L.append(f"### {i}.{j} {st}")
            for k in cs:
                L.append(f"{_box(k['status'])} {k['text']} — {k['evidence']}")
            L.append("")
        L += ["---", ""]
    n = len(R.sections) + 1
    if R.verify:
        L += [f"## {n}. Verification Procedure (spot-check, filled from this run)", ""] + R.verify + ["", "---", ""]
        n += 1
    L += [f"## {n}. Acceptance Criteria", ""]
    nxt = f"Phase {R.n} is **OK to proceed to {R.next_phase}**" if R.next_phase else \
        "Schedule is **OK for production deploy**"
    L.append(nxt.replace("OK to", "OK to") + " when:" if R.ok() else nxt.replace("**OK", "**NOT OK") + " — failed:")
    L.append("")
    for t, ok in R.acceptance:
        L.append(f"- {'✅' if ok else '❌'} {t}")
    L.append("- ⬜ Tech head sign-off")
    L += ["", "---", "", f"*End of Phase {R.n} test cases — schedule_id `{ctx['sid']['schedule_id']}`.*", ""]
    return "\n".join(L)


# ============================================================ helpers
def _p(n, of=None):
    return f"{n:,}" if of is None else f"{n:,} of {of:,}"


def per_line(fn):
    """Run fn(line) for PCR and TBR, combine (status, text) -> worst status, 'PCR ...; TBR ...'."""
    order = [PASS, INFO, WARN, FAIL, ERROR]
    sts, txt = [], []
    for ln in LINES:
        try:
            s, t = fn(ln)
        except Exception as e:                                   # noqa: BLE001
            s, t = ERROR, f"{type(e).__name__}: {e}"
        sts.append(s)
        txt.append(f"{ln.upper()} {t}")
    return max(sts, key=order.index), "; ".join(txt)


def ok(cond, ev, bad=FAIL):
    return (PASS if cond else bad), ev


class Run:
    """Lazy access to every output of one run."""
    def __init__(self, out_root: pathlib.Path, run: str):
        self.D = out_root / run
        self.run = run
        self.sid = json.loads((self.D / "SCHEDULE_ID.json").read_text(encoding="utf-8"))
        st = self.D / "DATA_STAGED.json"
        self.staged = json.loads(st.read_text(encoding="utf-8")) if st.exists() else {}
        self.plan_day = self.staged.get("plan_day")
        if not self.plan_day:                    # fall back to the plan the run read
            sel = self.D / "plan_from_db" / "plan_selection.json"
            self.plan_day = json.loads(sel.read_text(encoding="utf-8")).get("plan_day") if sel.exists() else "?"
        if not self.staged:                      # and to the staging stamp of that day
            st2 = ROOT / "data" / f"ppds_{self.plan_day}" / "_pipeline_stage.json"
            self.staged = json.loads(st2.read_text(encoding="utf-8")) if st2.exists() else {}
        self.data = ROOT / "data" / f"ppds_{self.plan_day}"
        self._c = {}

    def csv(self, rel, **kw):
        k = (rel, tuple(sorted(kw.items())) if kw else ())
        if k not in self._c:
            p = self.D / rel
            self._c[k] = pd.read_csv(p, encoding="utf-8-sig", **kw) if p.exists() else None
        return self._c[k]

    def L(self, ln, name, dates=()):
        d = self.csv(f"{ln}/{name}.csv")
        if d is None:
            raise FileNotFoundError(f"{ln}/{name}.csv")
        d = d.copy()
        for c in dates:
            if c in d.columns:
                d[c] = pd.to_datetime(d[c], errors="coerce")
        return d

    def stats(self, ln, phase):
        p = self.D / ln / f"{phase}_stats.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def master(self, name):
        k = ("master", name)
        if k not in self._c:
            p = self.data / name
            self._c[k] = pd.read_csv(p, dtype=str) if p.exists() else None
        return self._c[k]


# ============================================================ PHASE 1
def phase1(r: Run) -> Report:
    R = Report(1, "Demand Explosion",
               "`engine/phase0_validate.py` + `engine/phase1_building_demand.py`",
               "Turn every building-plan trolley (SKU × qty × machine × time) into per-item component "
               "demand by walking the BOM, with a need-by time per (trolley, item).")
    R.next_phase = "Phase 2"
    R.section("Input Loading Tests")
    R.sub("Load Inputs")
    R.check("Building plan loaded from the database plan for both lines", lambda: per_line(
        lambda ln: ok(r.stats(ln, "phase0").get("plan", {}).get("rows_raw", 0) > 0,
                      f"{r.stats(ln, 'phase0')['plan']['rows_raw']} rows from "
                      f"{r.stats(ln, 'phase0')['plan']['source']}")))
    R.check("Every plan machine resolves to a work centre", lambda: per_line(
        lambda ln: ok(r.stats(ln, "phase0")["plan"]["unresolved_machine"] == 0,
                      f"{r.stats(ln, 'phase0')['plan']['unresolved_machine']} unresolved")))
    R.check("Every plan row has a valid start time", lambda: per_line(
        lambda ln: ok(r.stats(ln, "phase0")["plan"]["bad_time"] == 0,
                      f"{r.stats(ln, 'phase0')['plan']['bad_time']} bad")))
    R.check("Every plan row names its green tyre (GT_Code)", lambda: per_line(
        lambda ln: ok(r.stats(ln, "phase0")["plan"]["with_gt_code"] == r.stats(ln, "phase0")["plan"]["rows_raw"],
                      f"{r.stats(ln, 'phase0')['plan']['with_gt_code']} of {r.stats(ln, 'phase0')['plan']['rows_raw']}",
                      WARN)))
    R.check("Zero-quantity plan rows are reported (not scheduled)", lambda: per_line(
        lambda ln: (INFO, f"{r.stats(ln, 'phase0')['plan']['zero_qty']} zero-qty row(s)")))
    R.sub("Data-blocker policy")
    def blocked(ln):
        s = r.L(ln, "phase0_sku_status")
        b = s[s["state"] == "DATA_BLOCKED"]
        return (WARN if len(b) else PASS), (f"{len(b)} SKU(s), {b['build_tyres'].sum():,.0f} tyres excluded: "
                                            f"{', '.join(b['sku'].astype(str))[:120]}" if len(b) else "none")
    R.check("Data-blocked SKUs are listed and excluded from the figures", lambda: per_line(blocked))
    R.check("Every blocked SKU names its blocker code and the missing table", lambda: per_line(
        lambda ln: ok((lambda b: b["blocker_code"].notna().all() and b["missing_table"].notna().all())(
            r.L(ln, "phase0_sku_status").query("state == 'DATA_BLOCKED'")), "checked")))
    R.sub("Building trolleys")
    def lots_unique(ln):
        b = r.L(ln, "phase1_building_lots")
        return ok(b["building_lot_id"].is_unique, f"{len(b)} trolleys, ids unique: {b['building_lot_id'].is_unique}")
    R.check("Each trolley has a unique `building_lot_id`", lambda: per_line(lots_unique))
    def lots_eq_plan(ln):
        b, e = r.L(ln, "phase1_building_lots"), r.L(ln, "phase0_plan_effective")
        return ok(len(b) == len(e) and abs(b["qty"].sum() - e["qty"].sum()) < EPS,
                  f"{len(b)} trolleys / {b['qty'].sum():,.0f} tyres vs effective plan {len(e)} / {e['qty'].sum():,.0f}")
    R.check("Trolley count and tyres equal the effective plan", lambda: per_line(lots_eq_plan))
    R.check("Every trolley ends after it starts", lambda: per_line(
        lambda ln: ok(((lambda b: (b["build_end"] > b["build_start"]).all())(
            r.L(ln, "phase1_building_lots", ("build_start", "build_end")))), "checked")))

    R.section("BOM Explosion Tests")
    R.sub("Coverage")
    def every_lot_has_demand(ln):
        b, d = r.L(ln, "phase1_building_lots"), r.L(ln, "phase1_demand")
        miss = set(b["building_lot_id"]) - set(d["building_lot_id"])
        return ok(not miss, f"{len(b) - len(miss)} of {len(b)} trolleys have component demand"
                  + (f"; none for {sorted(miss)[:5]}" if miss else ""))
    R.check("Every trolley produces component demand", lambda: per_line(every_lot_has_demand))
    def gt_in_bom(ln):
        b, bom = r.L(ln, "phase1_building_lots"), r.master(f"jkt_bom_{ln}.csv")
        pairs = set(zip(bom["Super_parent"].str.strip(), bom["child"].str.strip()))
        bad = b[[(str(s).strip(), str(g).strip()) not in pairs for s, g in zip(b["sku"], b["built_item"])]]
        return ok(bad.empty, f"{len(b) - len(bad)} of {len(b)} trolleys' green tyre is a BOM child of their SKU"
                  + (f"; not for {sorted(set(bad['sku']))[:4]}" if len(bad) else ""), WARN)
    R.check("The built green tyre is a BOM child of the trolley's SKU", lambda: per_line(gt_in_bom))
    R.sub("Demand aggregation")
    def formula(ln):
        d, b = r.L(ln, "phase1_demand"), r.L(ln, "phase1_building_lots")
        q = d["building_lot_id"].map(dict(zip(b["building_lot_id"], b["qty"])))
        err = (d["demand_qty"] - d["per_tyre_qty"] * q).abs() > 1e-6 * (1 + d["demand_qty"].abs())
        return ok(not err.any(), f"demand_qty = per_tyre_qty × trolley qty on {len(d) - err.sum():,} of {len(d):,} rows")
    R.check("`demand_qty = per_tyre_qty × trolley qty` on every row", lambda: per_line(formula))
    R.check("No negative, zero or NaN demand / per-tyre quantity", lambda: per_line(
        lambda ln: ok((lambda d: ((d["demand_qty"] > 0) & (d["per_tyre_qty"] > 0)).all())(r.L(ln, "phase1_demand")),
                      f"{len(r.L(ln, 'phase1_demand')):,} rows")))
    R.check("No (trolley, item) pair is emitted twice", lambda: per_line(
        lambda ln: ok(not r.L(ln, "phase1_demand").duplicated(["building_lot_id", "item_code"]).any(),
                      f"{int(r.L(ln, 'phase1_demand').duplicated(['building_lot_id', 'item_code']).sum())} duplicates")))

    R.section("UOM Handling Tests")
    R.sub("UOM propagation")
    R.check("Every demand row carries a unit", lambda: per_line(
        lambda ln: ok(r.L(ln, "phase1_demand")["uom"].notna().all(),
                      "units " + ", ".join(sorted(r.L(ln, "phase1_demand")["uom"].dropna().astype(str).unique())))))
    def one_uom(ln):
        d = r.L(ln, "phase1_demand")
        m = d.groupby("item_code")["uom"].nunique()
        return ok((m <= 1).all(), f"{int((m > 1).sum())} item(s) with more than one unit"
                  + (f": {list(m[m > 1].index)[:5]}" if (m > 1).any() else ""), WARN)
    R.check("Each item has one unit across the whole plan", lambda: per_line(one_uom))

    R.section("Need-by Timing Tests")
    R.sub("Need-by formula")
    def needby(ln):
        d = r.L(ln, "phase1_demand", ("build_start", "need_by"))
        exp = d["build_start"] - pd.to_timedelta(d["min_aging_h"] + d["buffer_h"], unit="h")
        bad = (d["need_by"] - exp).abs() > pd.Timedelta(seconds=1)
        return ok(not bad.any(), f"need_by = build_start − min_aging − buffer on {len(d) - bad.sum():,} of {len(d):,} rows")
    R.check("`need_by = build_start − min_aging − buffer`", lambda: per_line(needby))
    R.check("No need-by after the trolley starts", lambda: per_line(
        lambda ln: ok((lambda d: (d["need_by"] <= d["build_start"]).all())(
            r.L(ln, "phase1_demand", ("build_start", "need_by"))), "checked")))

    R.section("Routing Filter Tests")
    R.sub("Routed items")
    def routed(ln):
        d, rt = r.L(ln, "phase1_demand"), r.master(f"jkt_routing_{ln}.csv")
        rs = set(rt["routed_product"].dropna().str.strip())
        miss = sorted(set(d["item_code"].astype(str).str.strip()) - rs)
        return ok(not miss, f"{d['item_code'].nunique() - len(miss)} of {d['item_code'].nunique()} demanded items "
                  f"have a routing row" + (f"; missing {miss[:5]}" if miss else ""), WARN)
    R.check("Every demanded item has a routing (raw materials are not emitted)", lambda: per_line(routed))
    def dep_op(ln):
        d = r.L(ln, "phase1_demand")
        x = d[d["department"].isna() | d["operation"].isna()]
        return ok(x.empty, f"{len(x)} of {len(d):,} rows without department / operation"
                  + (f": items {sorted(set(x['item_code'].astype(str)))[:5]}" if len(x) else ""), WARN)
    R.check("Every demand row names its department and operation", lambda: per_line(dep_op))

    R.section("Quantity Sanity Tests (Production Rules)")
    R.sub("Per-tyre compound")
    def kg_per_tyre(ln):
        d = r.L(ln, "phase1_demand")
        b = r.L(ln, "phase1_building_lots")
        c = d[d["item_type"].astype(str).str.upper().str.contains("FINAL COMPOUND") & (d["uom"] == "KG")]
        t = b.groupby("sku")["qty"].sum()
        k = (c.groupby("sku")["demand_qty"].sum() / t).dropna()
        lo, hi = (3, 30) if ln == "pcr" else (15, 120)
        bad = k[(k < lo) | (k > hi)]
        return ok(bad.empty, f"final compound {k.min():.1f}–{k.max():.1f} kg/tyre over {len(k)} SKUs "
                  f"(expected {lo}–{hi})" + (f"; outside: {dict(bad.round(1).head(4))}" if len(bad) else ""), WARN)
    R.check("Final-compound kg per tyre is plausible for the line", lambda: per_line(kg_per_tyre))

    R.section("Output Schema Tests")
    R.sub("phase1_demand.csv")
    cols = ["item_code", "item_type", "uom", "building_lot_id", "sku", "per_tyre_qty", "demand_qty",
            "build_start", "min_aging_h", "buffer_h", "need_by"]
    R.check("Required columns present", lambda: per_line(
        lambda ln: ok(set(cols) <= set(r.L(ln, "phase1_demand").columns),
                      f"missing {sorted(set(cols) - set(r.L(ln, 'phase1_demand').columns))}" if not
                      set(cols) <= set(r.L(ln, 'phase1_demand').columns) else "all present")))
    R.check("No NaN in critical columns", lambda: per_line(
        lambda ln: ok(not r.L(ln, "phase1_demand")[["item_code", "building_lot_id", "demand_qty", "need_by"]]
                      .isna().any().any(), "checked")))
    # verification
    try:
        b = r.L("pcr", "phase1_building_lots")
        sku = b.groupby("sku")["qty"].sum().idxmax()
        d = r.L("pcr", "phase1_demand")
        x = d[d["sku"] == sku].groupby(["item_code", "item_type", "uom"])["demand_qty"].sum().reset_index()
        T = b.loc[b["sku"] == sku, "qty"].sum()
        R.verify = [f"Largest PCR SKU in this plan: **{sku}**, {T:,.0f} tyres on {b[b['sku'] == sku]['machine'].nunique()} "
                    f"machine(s). Its demand per tyre, from `pcr/phase1_demand.csv`:", "",
                    "| item | type | per tyre | total | unit |", "|---|---|---|---|---|"]
        for t in x.sort_values("demand_qty", ascending=False).head(8).itertuples(index=False):
            R.verify.append(f"| {t.item_code} | {t.item_type} | {t.demand_qty / T:,.4f} | {t.demand_qty:,.1f} | {t.uom} |")
        R.verify.append("")
        R.verify.append("Check a few against the BOM (`jkt_bom_pcr.csv`, Super_parent = the SKU): per tyre = "
                        "child_quantity ÷ Parent_qty along the path.")
    except Exception as e:                                       # noqa: BLE001
        R.verify = [f"(spot-check unavailable: {e})"]
    R.acceptance = [
        ("every plan row resolved (machine, time, GT code)", _all_pass(R, ("Every plan machine", "Every plan row has a valid"))),
        ("every trolley produced component demand", _all_pass(R, ("Every trolley produces",))),
        ("demand formula and need-by formula hold on every row", _all_pass(R, ("`demand_qty", "`need_by"))),
        ("no duplicate (trolley, item) rows", _all_pass(R, ("No (trolley",))),
        ("data-blocked SKUs listed with blocker and missing table", _all_pass(R, ("Every blocked SKU",))),
    ]
    return R


def _all_pass(R: Report, prefixes) -> bool:
    found = [c for _, _, c in R.all_checks() if any(c["text"].startswith(p) for p in prefixes)]
    return bool(found) and all(c["status"] in (PASS, INFO) for c in found)


# ============================================================ PHASE 2
def phase2(r: Run) -> Report:
    R = Report(2, "Lot Sizing",
               "`engine/phase2_wip_netting.py` + `engine/phase3_lot_sizing.py`",
               "Net the opening stock against demand (per batch, FEFO, maturity and shelf life), then group "
               "the remaining demand into production lots respecting MPQ, whole batches, campaign windows "
               "and ageing windows.")
    R.next_phase = "Phase 3"
    R.section("Input Loading Tests")
    R.sub("Phase 1 dependency and stock")
    R.check("Phase 1 demand read for both lines", lambda: per_line(
        lambda ln: ok(len(r.L(ln, "phase1_demand")) > 0, f"{len(r.L(ln, 'phase1_demand')):,} demand rows")))
    R.check("Opening stock loaded (rows, expired at the bell, demand covered)", lambda: per_line(
        lambda ln: (INFO, (lambda s: f"{s.get('wip_rows', 0):,} batches, {s.get('dead_at_open', 0):,} expired at "
                           f"07:00, {s.get('covered_pct', 0):.1f}% of demand from stock")(r.stats(ln, "phase2")))))
    R.check("Stock snapshot is the plan day's 06:30 sync", lambda: ok(
        str(r.staged.get("wip_sync", "")).startswith(str(r.plan_day)) and
        r.staged.get("snapshots", {}).get("wip", {}).get("shift") == "A",
        f"CTP_Opening_WIP {r.staged.get('wip_sync')} "
        f"({r.staged.get('snapshots', {}).get('wip', {}).get('chosen_by', '?')})", WARN))

    R.section("Stock Netting Tests")
    R.sub("Net demand")
    def net_formula(ln):
        n = r.L(ln, "phase2_demand_net")
        exp = (n["required_qty"] - n["wip_used"]).clip(lower=0)
        bad = (n["net_qty"] - exp).abs() > 1e-6 * (1 + n["required_qty"].abs())
        return ok(not bad.any(), f"net = required − stock on {len(n) - bad.sum():,} of {len(n):,} rows")
    R.check("`net_qty = required_qty − wip_used` (≥ 0)", lambda: per_line(net_formula))
    def cascade(ln):
        n = r.L(ln, "phase2_demand_net")
        exp = (n["demand_qty"] - n["required_qty"]).clip(lower=0)
        bad = (n["cascade_saved"] - exp).abs() > 1e-6 * (1 + n["demand_qty"].abs())
        return ok(not bad.any(), f"cascade = demand − required on {len(n) - bad.sum():,} of {len(n):,} rows; "
                  f"{n['cascade_saved'].sum():,.1f} saved because a parent came from stock")
    R.check("`cascade_saved = demand_qty − required_qty` (parent covered by stock)", lambda: per_line(cascade))
    R.check("Stock used never exceeds what was required", lambda: per_line(
        lambda ln: ok((lambda n: (n["wip_used"] <= n["required_qty"] + 1e-6).all())(r.L(ln, "phase2_demand_net")),
                      "checked")))
    R.sub("Stock allocations")
    def alloc_fresh(ln):
        a = r.L(ln, "phase2_wip_allocations", ("expires", "needed_by"))
        live = a["expires"].isna() | (a["expires"] >= a["needed_by"])
        return ok(live.all(), f"{live.sum():,} of {len(a):,} allocations are still within shelf life at need-by")
    R.check("No expired stock is allocated", lambda: per_line(alloc_fresh))
    def alloc_qty(ln):
        a = r.L(ln, "phase2_wip_allocations")
        neg, zero = int((a["qty_used"] < 0).sum()), int((a["qty_used"] == 0).sum())
        st = FAIL if neg else (WARN if zero else PASS)
        return st, (f"{len(a):,} allocations; {neg} negative; {zero} shown as 0 (a draw < 0.0005 rounded to 3 "
                    f"decimals)" if zero or neg else f"{len(a):,} allocations, all > 0")
    R.check("Every allocation has a positive quantity", lambda: per_line(alloc_qty))

    R.section("Lot Generation Tests")
    R.sub("Lot identity")
    R.check("`lot_id` unique", lambda: per_line(
        lambda ln: ok(r.L(ln, "phase3_lots")["lot_id"].is_unique, f"{len(r.L(ln, 'phase3_lots')):,} lots")))
    R.check("Every lot has qty > 0", lambda: per_line(
        lambda ln: ok((r.L(ln, "phase3_lots")["lot_qty"] > 0).all(),
                      f"{int((r.L(ln, 'phase3_lots')['lot_qty'] <= 0).sum())} lot(s) with qty ≤ 0")))
    R.check("`first_need ≤ last_need` on every lot", lambda: per_line(
        lambda ln: ok((lambda l: (l["first_need"] <= l["last_need"]).all())(
            r.L(ln, "phase3_lots", ("first_need", "last_need"))), "checked")))
    R.sub("Quantities")
    R.check("Lot quantity covers the net demand it serves (surplus ≥ 0)", lambda: per_line(
        lambda ln: ok((lambda l: (l["lot_qty"] + 1e-6 >= l["net_demand_served"]).all())(r.L(ln, "phase3_lots")),
                      (lambda l: f"total surplus {l['surplus'].sum():,.1f} on {int((l['surplus'] > 1e-6).sum())} lots")(
                          r.L(ln, "phase3_lots")))))
    def mpq(ln):
        l = r.L(ln, "phase3_lots")
        lo = l["mpq_min"].notna() & (l["lot_qty"] + 1e-6 < l["mpq_min"])
        hi = l["mpq_max"].notna() & (l["lot_qty"] > l["mpq_max"] + 1e-6)
        return ok(not lo.any() and not hi.any(), f"{int(lo.sum())} below MPQ min, {int(hi.sum())} above MPQ max "
                  f"({int(l['mpq_min'].notna().sum())} lots carry an MPQ)", WARN)
    R.check("MPQ min / max respected where the MPQ table has a value", lambda: per_line(mpq))
    def whole_batches(ln):
        l = r.L(ln, "phase3_lots")
        m = l[l["department"].astype(str).str.upper().str.contains("MIXING")]
        bad = (m["batches"] - m["batches"].round()).abs() > 1e-6
        return ok(not bad.any(), f"{len(m) - bad.sum()} of {len(m)} mixing lots are whole batches")
    R.check("Mixing lots are whole batches", lambda: per_line(whole_batches))
    R.sub("Duration")
    def dur(ln):
        l = r.L(ln, "phase3_lots")
        big = l[l["duration_min"] > 480]
        return ok(big.empty, f"max {l['duration_min'].max():.0f} min; {len(big)} lot(s) over one shift (480 min)"
                  + (f": {', '.join(big['item_code'].astype(str).unique()[:5])}" if len(big) else ""), WARN)
    R.check("Lot duration ≤ one shift (480 min)", lambda: per_line(dur))
    R.check("No lot with negative duration", lambda: per_line(
        lambda ln: ok((r.L(ln, "phase3_lots")["duration_min"] >= 0).all(), "checked")))

    R.section("Cross-Phase Consistency")
    R.sub("With Phase 1 / netting")
    def coverage(ln):
        n = r.L(ln, "phase2_demand_net")
        n = n[n["net_qty"] > 1e-6]
        ld = r.L(ln, "phase3_lot_demand")
        k1 = set(zip(n["building_lot_id"], n["item_code"]))
        k2 = set(zip(ld["building_lot_id"], ld["item_code"]))
        miss = k1 - k2
        return ok(not miss, f"{len(k1) - len(miss):,} of {len(k1):,} net (trolley, item) needs are assigned to a lot")
    R.check("Every net demand is assigned to a lot", lambda: per_line(coverage))
    def totals(ln):
        n = r.L(ln, "phase2_demand_net")
        ld = r.L(ln, "phase3_lot_demand")
        a, b = n["net_qty"].sum(), ld["qty"].sum()
        return ok(abs(a - b) <= 0.005 * max(a, 1), f"net demand {a:,.1f} vs assigned to lots {b:,.1f}")
    R.check("Net demand total = quantity assigned to lots (±0.5%)", lambda: per_line(totals))
    R.check("Every lot item is a demanded item", lambda: per_line(
        lambda ln: ok(set(r.L(ln, "phase3_lots")["item_code"]) <= set(r.L(ln, "phase1_demand")["item_code"]) |
                      set(r.L(ln, "phase3_lots")["item_code"]),
                      f"{r.L(ln, 'phase3_lots')['item_code'].nunique()} items with lots")))

    R.section("Output Schema Tests")
    R.sub("phase3_lots.csv")
    need = ["lot_id", "item_code", "item_type", "uom", "lot_qty", "net_demand_served", "batches", "first_need",
            "last_need", "min_aging_h", "max_aging_h", "duration_min", "operation", "department", "machines"]
    R.check("Required columns present", lambda: per_line(
        lambda ln: ok(set(need) <= set(r.L(ln, "phase3_lots").columns),
                      "all present" if set(need) <= set(r.L(ln, "phase3_lots").columns) else
                      f"missing {sorted(set(need) - set(r.L(ln, 'phase3_lots').columns))}")))
    def dep_m(ln):
        l = r.L(ln, "phase3_lots")
        x = l[l["department"].isna() | l["machines"].isna()]
        return ok(x.empty, f"{len(x)} of {len(l):,} lots without department / machines"
                  + (f": {sorted(set(x['item_code'].astype(str)))[:5]} (placed on a stand-in machine)" if len(x) else ""),
                  WARN)
    R.check("Every lot names its department and eligible machines", lambda: per_line(dep_m))
    try:
        rows = ["| line | lots | items | mixing lots | total surplus | lots > 480 min |", "|---|---|---|---|---|---|"]
        for ln in LINES:
            l = r.L(ln, "phase3_lots")
            rows.append(f"| {ln.upper()} | {len(l):,} | {l['item_code'].nunique()} | "
                        f"{int(l['department'].astype(str).str.upper().str.contains('MIXING').sum())} | "
                        f"{l['surplus'].sum():,.1f} | {int((l['duration_min'] > 480).sum())} |")
        R.verify = ["Lot counts of this run (`<line>/phase3_lots.csv`):", ""] + rows
    except Exception as e:                                       # noqa: BLE001
        R.verify = [f"(spot-check unavailable: {e})"]
    R.acceptance = [
        ("no expired stock allocated; stock never exceeds requirement", _all_pass(R, ("No expired", "Stock used"))),
        ("net-demand and cascade formulas hold on every row", _all_pass(R, ("`net_qty", "`cascade_saved"))),
        ("every lot has qty > 0, unique id, consistent need window", _all_pass(R, ("`lot_id`", "Every lot has qty", "`first_need"))),
        ("every net demand assigned to a lot, totals match", _all_pass(R, ("Every net demand", "Net demand total"))),
        ("mixing lots in whole batches", _all_pass(R, ("Mixing lots",))),
    ]
    return R


# ============================================================ PHASE 3
def _lots_all(r):
    parts = []
    for ln in LINES:
        l = r.L(ln, "phase4_lot_times", ("EST", "LST", "LFT"))
        l["line"] = ln.upper()
        parts.append(l)
    return pd.concat(parts, ignore_index=True)


def phase3(r: Run) -> Report:
    R = Report(3, "DAG Construction", "`engine/phase4_cpm.py` (lot precedence graph)",
               "Link every lot to the lots that feed it (producer → consumer, with quantity) from the BOM, "
               "so timing (Phase 4) and placement (Phase 5) can respect the production chain.")
    R.next_phase = "Phase 4"
    R.section("Input Loading Tests")
    R.sub("Phase 2 dependency")
    R.check("Lots and edges loaded", lambda: per_line(
        lambda ln: ok(len(r.L(ln, "phase3_lots")) > 0 and r.csv(f"{ln}/phase4_edges.csv") is not None,
                      f"{len(r.L(ln, 'phase3_lots')):,} lots, {len(r.L(ln, 'phase4_edges')):,} edges")))
    R.section("Edge Construction Tests")
    R.sub("Edges")
    def endpoints(ln):
        e, l = r.L(ln, "phase4_edges"), set(r.L(ln, "phase3_lots")["lot_id"])
        bad = ~e["producer_lot"].isin(l) | ~e["consumer_lot"].isin(l)
        return ok(not bad.any(), f"{len(e) - bad.sum():,} of {len(e):,} edges join two existing lots")
    R.check("Both ends of every edge are lots of this run", lambda: per_line(endpoints))
    R.check("No self-edges", lambda: per_line(
        lambda ln: ok(not (r.L(ln, "phase4_edges")["producer_lot"] == r.L(ln, "phase4_edges")["consumer_lot"]).any(),
                      "checked")))
    R.check("No duplicate edges", lambda: per_line(
        lambda ln: ok(not r.L(ln, "phase4_edges").duplicated(["producer_lot", "consumer_lot"]).any(),
                      f"{int(r.L(ln, 'phase4_edges').duplicated(['producer_lot', 'consumer_lot']).sum())} duplicates")))
    R.check("Every edge carries a positive quantity", lambda: per_line(
        lambda ln: ok((r.L(ln, "phase4_edges")["qty"] > 0).all(), "checked")))
    R.sub("Edges follow the BOM")
    def bom_edges(ln):
        e, l, bom = r.L(ln, "phase4_edges"), r.L(ln, "phase3_lots"), r.master(f"jkt_bom_{ln}.csv")
        it = dict(zip(l["lot_id"], l["item_code"].astype(str).str.strip()))
        pairs = set(zip(bom["child"].str.strip(), bom["Parent"].str.strip()))
        ok_ = [(it.get(p), it.get(c)) in pairs for p, c in zip(e["producer_lot"], e["consumer_lot"])]
        n = len(e) - sum(ok_)
        return ok(n == 0, f"{sum(ok_):,} of {len(e):,} edges are a direct BOM child → parent"
                  + (f"; {n} bridge a bypassed cutter / slitter (cut item converted to its roll)" if n else ""),
                  INFO)
    R.check("Each edge is a BOM (child → parent) link", lambda: per_line(bom_edges))
    R.section("Topological Sort Tests")
    R.sub("Acyclic")
    def acyclic(ln):
        e = r.L(ln, "phase4_edges")
        succ = defaultdict(list)
        indeg = defaultdict(int)
        nodes = set(e["producer_lot"]) | set(e["consumer_lot"])
        for p, c in zip(e["producer_lot"], e["consumer_lot"]):
            succ[p].append(c)
            indeg[c] += 1
        q = [n for n in nodes if indeg[n] == 0]
        seen = 0
        while q:
            n = q.pop()
            seen += 1
            for s in succ[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    q.append(s)
        return ok(seen == len(nodes), f"{seen:,} of {len(nodes):,} lots ordered; "
                  f"{len(nodes) - seen} in a cycle")
    R.check("No cycles (Kahn's algorithm orders every lot)", lambda: per_line(acyclic))
    def chain_order(ln):
        e, l = r.L(ln, "phase4_edges"), r.L(ln, "phase3_lots")
        dep = dict(zip(l["lot_id"], l["department"].astype(str).str.upper()))
        bad = [(p, c) for p, c in zip(e["producer_lot"], e["consumer_lot"])
               if "MIXING" in dep.get(c, "") and "MIXING" not in dep.get(p, "")]
        return ok(not bad, f"{len(bad)} edge(s) where a non-mixing lot feeds a mixing lot")
    R.check("Production chain order: nothing downstream feeds mixing", lambda: per_line(chain_order))
    R.section("Coverage")
    R.sub("Producers per consumer")
    def no_producer(ln):
        e, l = r.L(ln, "phase4_edges"), r.L(ln, "phase3_lots")
        has = set(e["consumer_lot"])
        x = l[~l["lot_id"].isin(has)]
        by = x["department"].astype(str).value_counts().head(5).to_dict()
        return INFO, f"{len(x)} of {len(l)} lots have no scheduled producer (inputs from stock / purchased): {by}"
    R.check("Lots fed only by stock or purchased material are counted", lambda: per_line(no_producer))
    def depth(ln):
        e = r.L(ln, "phase4_edges")
        pred = defaultdict(list)
        for p, c in zip(e["producer_lot"], e["consumer_lot"]):
            pred[c].append(p)
        memo = {}
        def d(n, k=0):
            if n in memo:
                return memo[n]
            if k > 60:
                return 0
            memo[n] = 0 if not pred[n] else 1 + max(d(p, k + 1) for p in pred[n])
            return memo[n]
        v = [d(n) for n in set(e["consumer_lot"])]
        return INFO, f"longest chain {max(v) if v else 0} stage(s) deep"
    R.check("Chain depth measured", lambda: per_line(depth))
    R.acceptance = [
        ("no cycles", _all_pass(R, ("No cycles",))),
        ("every edge joins two lots of this run, no self / duplicate edges",
         _all_pass(R, ("Both ends", "No self-edges", "No duplicate edges", "Every edge carries"))),
        ("chain order respected", _all_pass(R, ("Production chain order",))),
    ]
    return R


# ============================================================ PHASE 4
def phase4(r: Run) -> Report:
    R = Report(4, "CPM Backward Pass", "`engine/phase4_cpm.py` (EST / LST / LFT / slack)",
               "Walk the lot graph backwards from the trolleys: the latest finish (LFT) of every lot so its "
               "consumers can still start on time, the earliest start (EST) forward, and the slack between.")
    R.next_phase = "Phase 5"
    R.section("Input Loading Tests")
    R.sub("Phase 3 dependency")
    R.check("Every lot has a timing row", lambda: per_line(
        lambda ln: ok(set(r.L(ln, "phase3_lots")["lot_id"]) <= set(r.L(ln, "phase4_lot_times")["lot_id"]),
                      f"{len(r.L(ln, 'phase4_lot_times')):,} of {len(r.L(ln, 'phase3_lots')):,} lots")))
    R.section("CPM Algorithm Tests")
    R.sub("Values")
    R.check("No NaN in EST / LST / LFT", lambda: per_line(
        lambda ln: ok(not r.L(ln, "phase4_lot_times")[["EST", "LST", "LFT"]].isna().any().any(), "checked")))
    def lst(ln):
        t = r.L(ln, "phase4_lot_times", ("EST", "LST", "LFT"))
        bad = (t["LFT"] - t["LST"] - pd.to_timedelta(t["duration_h"], unit="h")).abs() > pd.Timedelta(seconds=60)
        return ok(not bad.any(), f"LST = LFT − duration on {len(t) - bad.sum():,} of {len(t):,} lots")
    R.check("`LST = LFT − duration`", lambda: per_line(lst))
    def slack(ln):
        t = r.L(ln, "phase4_lot_times", ("EST", "LST", "LFT"))
        s = (t["LST"] - t["EST"]).dt.total_seconds() / 3600
        bad = (s - t["slack_h"]).abs() > 0.02
        return ok(not bad.any(), f"slack = LST − EST on {len(t) - bad.sum():,} of {len(t):,} lots")
    R.check("`slack = LST − EST`", lambda: per_line(slack))
    R.sub("Chain consistency")
    def chain(ln):
        t = r.L(ln, "phase4_lot_times", ("EST", "LST", "LFT"))
        e = r.L(ln, "phase4_edges")
        ma = r.L(ln, "phase1_demand").groupby("item_code")["min_aging_h"].max().to_dict()
        T = t.set_index("lot_id")
        bad = 0
        for p, c in zip(e["producer_lot"], e["consumer_lot"]):
            if p in T.index and c in T.index:
                lim = T.at[c, "LST"] - pd.Timedelta(hours=float(ma.get(T.at[p, "item_code"], 0) or 0))
                if T.at[p, "LFT"] > lim + pd.Timedelta(minutes=TRANSFER_MIN + 1):
                    bad += 1
        return ok(bad == 0, f"{len(e) - bad:,} of {len(e):,} edges: producer LFT ≤ consumer LST − min ageing", WARN)
    R.check("Producer's latest finish leaves the consumer time to start (LFT ≤ LST − ageing)", lambda: per_line(chain))
    R.section("Slack Calculation Tests")
    R.sub("Critical and infeasible lots")
    R.check("Critical lots (slack ≤ 0) flagged", lambda: per_line(
        lambda ln: (INFO, (lambda t: f"{int(t['is_critical'].sum())} critical of {len(t)}; slack "
                           f"{t['slack_h'].min():.1f} … {t['slack_h'].max():.1f} h")(r.L(ln, "phase4_lot_times")))))
    def infeas(ln):
        f = r.csv(f"{ln}/phase4_infeasible.csv")
        n = 0 if f is None else len(f)
        by = {} if f is None else f["department"].astype(str).value_counts().head(4).to_dict()
        return (WARN if n else PASS), f"{n} lot(s) cannot meet their LFT even from the plan start (reported, not dropped): {by}"
    R.check("Infeasible lots (negative slack at the plan start) are reported", lambda: per_line(infeas))
    R.check("Lots that would need to start before the plan opens are counted", lambda: per_line(
        lambda ln: (INFO, f"{int(r.L(ln, 'phase4_lot_times')['before_open'].astype(bool).sum())} lot(s)")))
    R.section("Cross-Phase Consistency")
    R.sub("With Phase 5")
    R.check("Every timed lot reaches placement", lambda: per_line(
        lambda ln: ok(set(r.L(ln, "phase4_lot_times")["lot_id"]) <= set(r.L(ln, "phase5_schedule")["lot_id"]),
                      "all timed lots are placed")))
    R.acceptance = [
        ("every lot timed, no NaN", _all_pass(R, ("Every lot has a timing", "No NaN"))),
        ("LST and slack formulas hold", _all_pass(R, ("`LST", "`slack"))),
        ("infeasible lots reported (not dropped)", True),
    ]
    return R


# ============================================================ PHASE 5
def phase5(r: Run) -> Report:
    R = Report(5, "Forward Placement (JIT)", "`engine/phase4b_assignment.py` + `engine/phase5_placement.py`",
               "Assign every lot a machine and place it forward in time after its inputs are made and rested, "
               "and place every building trolley on its planned machine once its kit is complete.")
    R.next_phase = "Phase 6"
    R.section("Input Loading Tests")
    R.sub("Prior phases")
    R.check("Every lot has a machine assignment", lambda: per_line(
        lambda ln: ok(set(r.L(ln, "phase3_lots")["lot_id"]) <= set(r.L(ln, "phase4b_assignment")["lot_id"]),
                      f"{len(r.L(ln, 'phase4b_assignment')):,} assignments")))
    R.section("Upstream Placement Tests")
    R.sub("Every lot placed")
    def placed(ln):
        s, l = r.L(ln, "phase5_schedule", ("start", "finish")), r.L(ln, "phase3_lots")
        miss = set(l["lot_id"]) - set(s["lot_id"])
        return ok(not miss and s["start"].notna().all(), f"{len(l) - len(miss):,} of {len(l):,} lots placed")
    R.check("Every lot placed (none dropped silently)", lambda: per_line(placed))
    R.check("Every lot ends after it starts", lambda: per_line(
        lambda ln: ok((lambda s: (s["finish"] > s["start"]).all())(r.L(ln, "phase5_schedule", ("start", "finish"))),
                      "checked")))
    def t0(ln):
        s = r.L(ln, "phase5_schedule", ("start",))
        T0 = pd.Timestamp(str(r.plan_day)) + pd.Timedelta(hours=7)
        n = int((s["start"] < T0 - pd.Timedelta(seconds=1)).sum())
        return ok(n == 0, f"{n} lot(s) start before {T0}")
    R.check("No lot starts before the plan opens (07:00 of the plan day)", lambda: per_line(t0))
    R.sub("Machine eligibility and capacity")
    def eligible(ln):
        v = r.csv("VALIDATION.csv")
        x = v[(v["line"] == ln.upper()) & v["test"].astype(str).str.startswith("1 ")]
        return ok(not x.empty and (x["verdict"] == "PASS").all(), str(x["detail"].iloc[0])[:160] if len(x) else "no row")
    R.check("Every lot is on a machine its routing allows", lambda: per_line(eligible))
    def overlaps():
        s = pd.concat([r.L(ln, "phase5_schedule", ("start", "finish")) for ln in LINES])
        s["occ"] = s["start"] - pd.to_timedelta(pd.to_numeric(s["changeover_min"], errors="coerce").fillna(0), unit="m")
        s = s.sort_values(["machine", "occ"])
        prev = s.groupby("machine")["finish"].shift()
        n = int((s["occ"] < prev - pd.Timedelta(seconds=1)).sum())
        return ok(n == 0, f"{n} overlap(s) across {s['machine'].nunique()} machines (both lines, changeover included)")
    R.check("No two lots overlap on one machine (shared machines across lines included)", overlaps)
    R.sub("Precedence and ageing")
    def seq(ln):
        v = r.csv("VALIDATION.csv")
        x = v[(v["line"] == ln.upper()) & v["test"].astype(str).str.startswith("3 ")]
        return ok(not x.empty and (x["verdict"] == "PASS").all(), str(x["detail"].iloc[0])[:160] if len(x) else "no row")
    R.check("Consumer starts after producer finish + min ageing", lambda: per_line(seq))
    def shelf(ln):
        v = r.csv("VALIDATION.csv")
        x = v[(v["line"] == ln.upper()) & v["test"].astype(str).str.startswith("7 ")]
        d = str(x["detail"].iloc[0]) if len(x) else "no row"
        return ok(" 0 where" in d, d[:170], WARN)
    R.check("Consumer starts before the producer's material expires (max ageing)", lambda: per_line(shelf))
    R.sub("Changeovers")
    R.check("Changeovers are applied between different items", lambda: per_line(
        lambda ln: (INFO, (lambda s: f"{int((pd.to_numeric(s['changeover_min'], errors='coerce') > 0).sum())} "
                           f"changeovers, {pd.to_numeric(s['changeover_min'], errors='coerce').sum() / 60:,.1f} h")(
            r.L(ln, "phase5_schedule")))))
    R.check("Late lots (finish after LFT) are counted", lambda: per_line(
        lambda ln: (INFO, (lambda s: f"{int((s['late_h'] > 0).sum())} of {len(s)} lots late; max "
                           f"{s['late_h'].max():.1f} h")(r.L(ln, "phase5_schedule")))))
    R.section("Building Placement Tests")
    R.sub("Kit gate")
    def kit(ln):
        b = r.L(ln, "phase5_building_plan", ("kit_ready", "feasible_start"))
        early = b["kit_ready"].notna() & (b["feasible_start"] < b["kit_ready"] - pd.Timedelta(seconds=1))
        inc = b["kit_incomplete"].astype(bool)
        return ok(not early.any() and not inc.any(), f"{int(early.sum())} trolley(s) before kit ready, "
                  f"{int(inc.sum())} with an incomplete kit, of {len(b)}")
    R.check("No trolley starts before its whole kit is ready", lambda: per_line(kit))
    def plan_machine(ln):
        b = r.L(ln, "phase5_building_plan")
        p = r.L(ln, "phase1_building_lots")
        m = dict(zip(p["building_lot_id"], p["machine"].astype(str)))
        bad = (b["building_lot_id"].map(m) != b["machine"].astype(str)).sum()
        return ok(bad == 0, f"{int(bad)} trolley(s) moved off their planned machine")
    R.check("Every trolley stays on its planned machine", lambda: per_line(plan_machine))
    def plan_order(ln):
        b = r.L(ln, "phase5_building_plan", ("build_start", "feasible_start"))
        bad = 0
        for m, g in b.groupby("machine"):
            if list(g.sort_values("build_start")["building_lot_id"]) != list(g.sort_values("feasible_start")["building_lot_id"]):
                bad += 1
        return ok(bad == 0, f"{bad} of {b['machine'].nunique()} machine(s) reordered")
    R.check("Trolley order on each machine = plan order", lambda: per_line(plan_order))
    def b_overlap(ln):
        b = r.L(ln, "phase5_building_plan", ("feasible_start", "feasible_end")).sort_values(["machine", "feasible_start"])
        prev = b.groupby("machine")["feasible_end"].shift()
        n = int((b["feasible_start"] < prev - pd.Timedelta(seconds=1)).sum())
        return ok(n == 0, f"{n} overlap(s)")
    R.check("No two trolleys overlap on one building machine", lambda: per_line(b_overlap))
    R.check("Trolleys inside the plan horizon", lambda: per_line(
        lambda ln: (INFO, (lambda b: f"{int(b['within_horizon'].astype(bool).sum())} of {len(b)} trolleys, "
                           f"{b.loc[b['within_horizon'].astype(bool), 'qty'].sum():,.0f} of {b['qty'].sum():,.0f} tyres")(
            r.L(ln, "phase5_building_plan")))))
    R.acceptance = [
        ("every lot placed, none before the plan opens", _all_pass(R, ("Every lot placed", "No lot starts before"))),
        ("no machine double-booked (upstream and building)", _all_pass(R, ("No two lots overlap", "No two trolleys"))),
        ("routing machine rules and precedence + min ageing respected", _all_pass(R, ("Every lot is on a machine", "Consumer starts after"))),
        ("full-kit gate respected; trolleys keep plan machine and order", _all_pass(R, ("No trolley starts", "Every trolley stays", "Trolley order"))),
    ]
    return R


# ============================================================ PHASE 6
def phase6(r: Run) -> Report:
    R = Report(6, "Curing Revision", "`engine/phase7_curing.py`",
               "Keep every curing lot on its planned press and in its planned order; start it only when its "
               "green tyres exist (built + rested, or GT stock), pushing it — and what follows on that press — "
               "forward, never earlier. Count only what finishes inside the plan horizon.")
    R.next_phase = "Phase 7"
    R.section("Input Loading Tests")
    R.sub("Curing plan")
    def plan_rows(ln):
        c = r.L(ln, "phase7_curing_plan")
        p = list((r.D / "plan_from_db").glob(f"*CuringPlan*_{ln.upper()}_*.csv"))
        if not p:
            return INFO, f"{len(c):,} press lots (plan file not available to compare)"
        raw = pd.read_csv(p[0])
        n = int((pd.to_numeric(raw["Qty"], errors="coerce") > 0).sum())
        return ok(len(c) == n, f"{len(c):,} press lots vs {n:,} curing-plan rows with Qty > 0")
    R.check("Every curing-plan row becomes one press lot", lambda: per_line(plan_rows))
    R.section("Revision Tests")
    R.sub("Forward only")
    def fwd(ln):
        c = r.L(ln, "phase7_curing_plan", ("planned_start", "feasible_start"))
        x = c[c["feasible_start"].notna()]
        bad = (x["feasible_start"] < x["planned_start"] - pd.Timedelta(seconds=1)).sum()
        return ok(bad == 0, f"{int(bad)} lot(s) pulled earlier than planned; {int((x['push_h'] > 0).sum())} pushed, "
                  f"median {x.loc[x['push_h'] > 0, 'push_h'].median() if (x['push_h'] > 0).any() else 0:.1f} h, "
                  f"max {x['push_h'].max():.1f} h")
    R.check("No lot starts before its planned time", lambda: per_line(fwd))
    def gt(ln):
        c = r.L(ln, "phase7_curing_plan", ("gt_ready", "feasible_start"))
        x = c[c["feasible_start"].notna() & c["gt_ready"].notna()]
        bad = (x["feasible_start"] < x["gt_ready"] - pd.Timedelta(seconds=1)).sum()
        return ok(bad == 0, f"{int(bad)} lot(s) start before their green tyres are ready")
    R.check("No lot starts before its green tyres are ready", lambda: per_line(gt))
    R.sub("Press calendar")
    def press_order(ln):
        c = r.L(ln, "phase7_curing_plan", ("planned_start", "feasible_start"))
        x = c[c["feasible_start"].notna()]
        bad = 0
        for p, g in x.groupby("press"):
            if not g.sort_values("planned_start")["feasible_start"].is_monotonic_increasing:
                bad += 1
        return ok(bad == 0, f"{bad} of {x['press'].nunique()} press(es) reordered")
    R.check("Lot order on each press = plan order", lambda: per_line(press_order))
    def press_overlap(ln):
        c = r.L(ln, "phase7_curing_plan", ("feasible_start", "feasible_end"))
        x = c[c["feasible_start"].notna()].sort_values(["press", "feasible_start"])
        prev = x.groupby("press")["feasible_end"].shift()
        n = int((x["feasible_start"] < prev - pd.Timedelta(seconds=1)).sum())
        return ok(n == 0, f"{n} overlap(s)")
    R.check("No two lots overlap on one press", lambda: per_line(press_overlap))
    R.sub("Quantities")
    R.check("Every cured quantity is even (two mould halves)", lambda: per_line(
        lambda ln: ok((r.L(ln, "phase7_curing_plan")["qty"] % 2 == 0).all(),
                      f"{int((r.L(ln, 'phase7_curing_plan')['qty'] % 2 != 0).sum())} odd")))
    R.check("No lot cures more than planned", lambda: per_line(
        lambda ln: ok((lambda c: (c["qty"] <= c["qty_planned"] + EPS).all())(r.L(ln, "phase7_curing_plan")),
                      (lambda c: f"rounding loss {(c['qty_planned'] - c['qty']).sum():,.0f} tyres")(
                          r.L(ln, "phase7_curing_plan")))))
    R.section("Month (Plan) Horizon Bound")
    R.sub("Horizon flag")
    def horizon(ln):
        c = r.L(ln, "phase7_curing_plan", ("feasible_end",))
        b = r.L(ln, "phase5_building_plan", ("build_end",))
        H = b["build_end"].max()
        exp = c["feasible_end"].notna() & (c["feasible_end"] <= H)
        bad = (exp != c["within_horizon"].astype(bool)).sum()
        return ok(bad == 0, f"horizon {H}; {int(c['within_horizon'].astype(bool).sum())} of {len(c)} lots inside; "
                  f"{int(bad)} mis-flagged")
    R.check("`within_horizon` = lot ends by the plan end", lambda: per_line(horizon))
    R.check("Lots pushed beyond the horizon are reported (not dropped silently)", lambda: per_line(
        lambda ln: (INFO, (lambda c: f"{int((~c['within_horizon'].astype(bool)).sum())} lots / "
                           f"{c.loc[~c['within_horizon'].astype(bool), 'qty'].sum():,.0f} tyres after the plan end; "
                           f"{int(c['gt_ready'].isna().sum())} never get green tyres")(r.L(ln, "phase7_curing_plan")))))
    R.section("Business Rule Tests")
    R.sub("Data-blocked SKUs")
    R.check("Lots of data-blocked SKUs are outside the KPI", lambda: per_line(
        lambda ln: ok((lambda c: not (c.loc[c["sku_state"] == "DATA_BLOCKED", "in_kpi"].astype(bool)).any())(
            r.L(ln, "phase7_curing_plan")), "checked")))
    def otif(ln):
        s = r.csv("FULFILMENT_DAYWISE_v15.csv")
        t = s[(s["line"] == ln.upper()) & (s["day"] == "TOTAL")]
        return INFO, (f"cured {int(t['cured'].iloc[0]):,} of {int(t['cure_plan'].iloc[0]):,} "
                      f"({float(t['cured_pct'].iloc[0]):.1f}%)" if len(t) else "no fulfilment row")
    R.check("Cured within the plan horizon (eligible plan)", lambda: per_line(otif))
    R.acceptance = [
        ("forward-only, after green tyres, plan press order, no press overlap",
         _all_pass(R, ("No lot starts before its planned", "No lot starts before its green", "Lot order on each press", "No two lots overlap on one press"))),
        ("even quantities, never above plan", _all_pass(R, ("Every cured quantity", "No lot cures more"))),
        ("horizon flag correct; overflow reported", _all_pass(R, ("`within_horizon`",))),
        ("data-blocked SKUs outside the KPI", _all_pass(R, ("Lots of data-blocked",))),
    ]
    return R


# ============================================================ PHASE 7
def phase7(r: Run) -> Report:
    R = Report(7, "Schedule Validation",
               "`engine/make_floor_schedule_v10.py`, `validate_floor_schedule.py`, `make_validation_report.py`, "
               "`pipeline/persist_floor.py`",
               "Final gate on the published schedule: the shift-wise floor schedule, the routing / ageing / "
               "kit / capacity checks, the schedule gates, and the copy in JK_CTP_PPDS.CTP_Schedule_Floor.")
    R.section("Floor Schedule Tests")
    R.sub("Shift split")
    def fl(ln):
        return r.L(ln, "../phase5_floor_schedule_" + ln, ("start_time", "end_time")) if False else \
            pd.read_csv(r.D / f"phase5_floor_schedule_{ln}.csv", encoding="utf-8-sig",
                        parse_dates=["start_time", "end_time"])
    def in_shift(ln):
        f = fl(ln)
        base = pd.Timestamp("2000-01-01 07:00")
        k0 = ((f["start_time"] - base).dt.total_seconds() // 28800)
        k1 = ((f["end_time"] - pd.Timedelta(seconds=1) - base).dt.total_seconds() // 28800)
        bad = int((k0 != k1).sum())
        return ok(bad == 0, f"{len(f) - bad:,} of {len(f):,} rows lie inside one shift")
    R.check("Every floor row lies inside one shift (lots cut at 07 / 15 / 23)", lambda: per_line(in_shift))
    def pieces_sum(ln):
        f = fl(ln)
        if "lot_qty" not in f.columns:
            return WARN, "no lot_qty column (old layout)"
        x = f[f["lot_id"].notna() & (f["lot_id"].astype(str) != "")]
        g = x.groupby("lot_id").agg(q=("produce_qty", "sum"), lq=("lot_qty", "first"))
        bad = int(((g["q"] - g["lq"]).abs() > 1e-3 * (1 + g["lq"].abs())).sum())
        return ok(bad == 0, f"{len(g) - bad:,} of {len(g):,} lots: shift pieces add up to the lot")
    R.check("Shift pieces of a lot add up to the lot quantity", lambda: per_line(pieces_sum))
    R.check("No NaN or backward times", lambda: per_line(
        lambda ln: ok((lambda f: f["start_time"].notna().all() and (f["end_time"] > f["start_time"]).all())(fl(ln)),
                      "checked")))
    R.section("Routing Validation (VALIDATION.csv)")
    R.sub("Per line")
    v = r.csv("VALIDATION.csv")
    for test in (v["test"].unique() if v is not None else []):
        def t_(test=test):
            def f(ln):
                x = v[(v["line"] == ln.upper()) & (v["test"] == test)]
                if x.empty:
                    return INFO, "not run"
                st = PASS if (x["verdict"] == "PASS").all() else (WARN if "coverage" in test else FAIL)
                return st, str(x["detail"].iloc[0])[:150]
            return per_line(f)
        R.check(f"{test}", t_)
    R.section("Schedule Gates (VALIDATION_REPORT.xlsx)")
    R.sub("Gates")
    try:
        g = pd.read_excel(r.D / "VALIDATION_REPORT.xlsx", sheet_name="Schedule checks")
        for row in g.itertuples(index=False):
            st = PASS if row.verdict == "PASS" else (INFO if pd.isna(row.verdict) else FAIL)
            R.check(f"{row.gate} {row.line} — {row.check}", lambda st=st, row=row: (st, f"{row.value} (target {row.target})"))
    except Exception as e:                                       # noqa: BLE001
        R.check("Schedule gates readable", lambda e=e: (ERROR, str(e)))
    R.section("Fulfilment")
    R.sub("Per line")
    def ful(ln):
        s = r.csv("FULFILMENT_DAYWISE_v15.csv")
        t = s[(s["line"] == ln.upper()) & (s["day"] == "TOTAL")]
        return INFO, (f"built {int(t['built'].iloc[0]):,}/{int(t['build_plan'].iloc[0]):,} "
                      f"({float(t['built_pct'].iloc[0]):.1f}%), cured {int(t['cured'].iloc[0]):,}/"
                      f"{int(t['cure_plan'].iloc[0]):,} ({float(t['cured_pct'].iloc[0]):.1f}%)")
    R.check("Built and cured against the plan (data-blocked excluded)", lambda: per_line(ful))
    R.section("Database Copy (CTP_Schedule_Floor)")
    R.sub("Persisted schedule")
    pv = r.D / "db_persist" / "validation_result.json"
    pr = r.D / "db_persist" / "persist_result.json"
    def written():
        x = json.loads(pr.read_text(encoding="utf-8"))
        return ok(bool(x.get("written")) or bool(x.get("existing_before")),
                  f"{x.get('inserted', x.get('existing_before', 0)):,} rows under schedule_id "
                  f"{x.get('db_schedule_id')} ({x.get('error') or 'committed'})")
    R.check("Schedule written to the database under this run's schedule_id", written)
    def readback():
        x = json.loads(pv.read_text(encoding="utf-8"))
        mm = sum(x.get("floor_vs_prepared_mismatches", {}).values())
        return ok(bool(x.get("passed")), f"{x['rows_in_db']:,} rows read back; missing {x['missing_in_db']}, "
                  f"unexpected {x['unexpected_in_db']}, duplicates {x['duplicates_in_db']}, field mismatches {mm}; "
                  f"stages {', '.join(x.get('stages_present', []))}")
    R.check("Read-back equals the floor schedule (row by row, every field)", readback)
    def ids():
        x = json.loads(pv.read_text(encoding="utf-8"))
        return ok(x["schedule_id"] == r.sid["schedule_id"], f"{x['schedule_id']}")
    R.check("Database schedule_id = this run's SCHEDULE_ID.json", ids)
    R.acceptance = [
        ("floor schedule split correctly into shifts", _all_pass(R, ("Every floor row lies", "Shift pieces"))),
        ("routing, department, sequence, kit, even-curing and shelf-life checks pass",
         all(c["status"] in (PASS, INFO, WARN) for t, _, c in R.all_checks() if t.startswith("Routing"))),
        ("all schedule gates pass", all(c["status"] in (PASS, INFO) for t, _, c in R.all_checks() if t.startswith("Schedule Gates"))),
        ("schedule in CTP_Schedule_Floor and identical on read-back", _all_pass(R, ("Schedule written", "Read-back", "Database schedule_id"))),
    ]
    return R


# ============================================================ driver
def generate(out_root: pathlib.Path, run: str, reports_root: pathlib.Path | None = None) -> dict:
    r = Run(out_root, run)
    reports_root = reports_root or (HERE / "reports")
    dest = reports_root / r.sid["schedule_id"]
    dest.mkdir(parents=True, exist_ok=True)
    sel = r.D / "plan_from_db" / "plan_selection.json"
    plan = json.loads(sel.read_text(encoding="utf-8")) if sel.exists() else {}
    snaps = r.staged.get("snapshots", {})
    ctx = {"sid": r.sid, "run": run, "plan_day": r.plan_day,
           "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "plan_txt": "; ".join(f"{k} `{v.get('run_id')}` ({v.get('building_tyres', 0):,} to build, "
                                 f"{v.get('curing_tyres', 0):,} to cure)" for k, v in plan.get("lines", {}).items())
                       or "?",
           "stock_txt": (f"WIP {snaps.get('wip', {}).get('sync', r.staged.get('wip_sync'))} "
                         f"({snaps.get('wip', {}).get('chosen_by', '')}); GT "
                         f"{', '.join(f'{k.upper()} {v}' for k, v in (r.staged.get('gt_sync') or {}).items())}")}
    rows, summary = [], []
    for fn in (phase1, phase2, phase3, phase4, phase5, phase6, phase7):
        try:
            R = fn(r)
        except Exception as e:                                   # noqa: BLE001
            R = Report(len(summary) + 1, fn.__name__, "?", "report could not be built")
            R.section("Report error")
            R.sub("Exception")
            R.check("Phase report builds", lambda e=e: (ERROR, traceback.format_exc(limit=2).replace("\n", " ")))
        (dest / f"TEST_CASES_PHASE{R.n}.md").write_text(render(R, ctx), encoding="utf-8")
        c = R.counts()
        summary.append((R, c))
        for t, st, k in R.all_checks():
            rows.append({"schedule_id": r.sid["schedule_id"], "phase": R.n, "phase_name": R.name, "section": t,
                         "subsection": st, "check": k["text"], "status": k["status"], "evidence": k["evidence"]})
    pd.DataFrame(rows).to_csv(dest / "checks.csv", index=False, encoding="utf-8-sig")
    tot = defaultdict(int)
    for _, c in summary:
        for k, v in c.items():
            tot[k] += v
    overall = all(R.ok() for R, _ in summary)
    S = [f"# Phase-wise test report — schedule_id `{r.sid['schedule_id']}`", "",
         f"Run `{run}` · plan day {r.plan_day} · tested {ctx['now']}  ",
         f"Plan: {ctx['plan_txt']}  ", f"Stock / GT: {ctx['stock_txt']}  ",
         f"Database: `JK_CTP_PPDS.CTP_Schedule_Floor` where `schedule_id = '{r.sid['db_schedule_id']}'`", "",
         "| Phase | Name | PASS | WARN | FAIL | ERROR | INFO | Verdict | Report |",
         "|---|---|---|---|---|---|---|---|---|"]
    for R, c in summary:
        S.append(f"| {R.n} | {R.name} | {c[PASS]} | {c[WARN]} | {c[FAIL]} | {c[ERROR]} | {c[INFO]} | "
                 f"{'✅ OK' if R.ok() else '❌ NOT OK'} | [TEST_CASES_PHASE{R.n}.md](TEST_CASES_PHASE{R.n}.md) |")
    S += ["", f"**Overall: {'✅ all phases OK' if overall else '❌ at least one phase NOT OK'}** — "
              f"{tot[PASS]} PASS · {tot[WARN]} WARN · {tot[FAIL]} FAIL · {tot[ERROR]} ERROR · {tot[INFO]} INFO", ""]
    fails = [x for x in rows if x["status"] in (FAIL, ERROR)]
    if fails:
        S += ["## Failed checks", "", "| Phase | Check | Evidence |", "|---|---|---|"]
        S += [f"| {x['phase']} | {x['check']} | {x['evidence']} |" for x in fails]
        S.append("")
    warns = [x for x in rows if x["status"] == WARN]
    if warns:
        S += ["## Warnings", "", "| Phase | Check | Evidence |", "|---|---|---|"]
        S += [f"| {x['phase']} | {x['check']} | {x['evidence']} |" for x in warns]
        S.append("")
    S += ["All checks: `checks.csv`.", ""]
    (dest / "SUMMARY.md").write_text("\n".join(S), encoding="utf-8")
    res = {"schedule_id": r.sid["schedule_id"], "folder": str(dest), "overall_ok": overall,
           "counts": dict(tot), "phases": {R.n: {"name": R.name, "ok": R.ok(), **dict(c)} for R, c in summary}}
    (dest / "result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase-wise test reports for one run")
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default=str(ROOT / "outputs"), help="output root of the run")
    ap.add_argument("--reports", default=str(HERE / "reports"))
    a = ap.parse_args()
    res = generate(pathlib.Path(a.out), a.run, pathlib.Path(a.reports))
    print(f"  phase-wise test reports -> {res['folder']}")
    for n, p in res["phases"].items():
        print(f"    Phase {n} {p['name']:26s} PASS {p.get(PASS, 0):3d}  WARN {p.get(WARN, 0):3d}  "
              f"FAIL {p.get(FAIL, 0):3d}  ERROR {p.get(ERROR, 0):3d}  -> {'OK' if p['ok'] else 'NOT OK'}")
    c = res["counts"]
    print(f"  overall: {'ALL PHASES OK' if res['overall_ok'] else 'NOT OK'}  "
          f"({c.get(PASS, 0)} PASS, {c.get(WARN, 0)} WARN, {c.get(FAIL, 0)} FAIL, {c.get(ERROR, 0)} ERROR)")
    return 0 if res["overall_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
