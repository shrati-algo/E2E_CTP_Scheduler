#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""schedule_changeovers.py -- changeover / clean-out rows for the schedule workbook (2026-10-06, user).

Between two consecutive lots of DIFFERENT items on a machine the workbook shows a light-blue row
CHANGEOVER or CLEANOUT with its duration, from the same rules the scheduler charges (phase5):
  upstream   plant changeover matrix (inputs/changeover_lookup.csv via config machine_to_changeover_line);
             pair missing in the matrix -> changeover_missing_pair_min / _by_type; no line -> 15 min
  mixers     mixer_changeover_min (1); butyl sequence broken -> butyl clean-out (120 s) INSTEAD of it;
             master <-> final switch -> TR100 clean-out (changeover + master_final_cleanout_min)
  4RC 901    CAP 66 before / after -> clean-out of at least cap66_cleanout_min (15)
  building   GT change -> different-size changeover (changeover matrix: PCR 'Inch to Inch', TBR 'Size Change')
read_sched(xlsx, prefix) reads a schedule kind back (all its PCR / TBR / shared sheets, lot rows only).
"""
import os
import sys

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
LIGHT_BLUE = "FFDDEBF7"


class Changeovers:
    def __init__(self, inputs=os.path.join(HERE, "inputs")):
        cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
        br = cfg.get("business_rules") or {}
        self.m2l = {str(k).strip(): str(v).strip() for k, v in (cfg.get("machine_to_changeover_line") or {}).items()}
        self.miss = {str(k).strip(): float(v) for k, v in (cfg.get("changeover_missing_pair_min") or {}).items()}
        self.miss_t = {str(k).strip(): (float(v.get("same_type", 15)), float(v.get("other_type", 36)))
                       for k, v in (cfg.get("changeover_missing_pair_by_type") or {}).items()}
        self.mix_co = float(cfg.get("mixer_changeover_min", 2))
        self.mixers = {str(x) for x in (br.get("mixers") or [])}
        self.cap66 = float(br.get("cap66_cleanout_min", 15))
        self.tr100_item = str(br.get("master_final_cleanout_item", "TR100"))
        self.tr100 = float(br.get("master_final_cleanout_min", 5))
        bd = cfg.get("butyl_day_block") or {}
        self.after_butyl = ({b for g in (bd.get("groups") or {}).values() for b in (g.get("butyl") or [])}
                            if bd.get("enabled") else set())
        self.after_butyl_min = float(bd.get("cleanout_after_sec", 120)) / 60.0
        self.lut = {}
        p = os.path.join(inputs, "changeover_lookup.csv")
        if os.path.exists(p):
            c = pd.read_csv(p)
            c["changeover_min"] = pd.to_numeric(c["changeover_min"], errors="coerce")
            for r in c.dropna(subset=["changeover_min"]).itertuples():
                self.lut[(str(r.line).strip(), str(r.from_item).strip(), str(r.to_item).strip())] = float(r.changeover_min)
        self.butyl = None
        try:
            sys.path.insert(0, HERE)
            import butyl_rule
            b = butyl_rule.load(cfg)
            self.butyl = b if b.enabled else None
        except Exception:
            self.butyl = None
        # building: different-size changeover per line (phase5 V23 capacity rules)
        bc = (cfg.get("capacity_rules_v23") or {}).get("building") or {}
        self.bld = {"PCR": 60.0, "TBR": 24.0}
        try:
            m = pd.read_csv(os.path.join(inputs, "changeover_matrix.csv"), low_memory=False,
                            usecols=["machine", "changeover_type", "changeover_time_min"])
            ct = m["changeover_type"].fillna("").astype(str).str.lower()
            mins = pd.to_numeric(m["changeover_time_min"], errors="coerce")
            for ln, pat, key in ((bc.get("tbr_line", "TBR Building"), "size change", "TBR"),
                                 (bc.get("pcr_line", "PCR Building BJ"), "inch to inch", "PCR")):
                v = mins[(m["machine"].astype(str).str.strip() == ln) & ct.str.contains(pat)].max()
                if pd.notna(v):
                    self.bld[key] = float(v)
        except Exception:
            pass

    def upstream(self, m, a, b, ta="", tb=""):
        """(minutes, kind, basis) for a -> b on upstream machine m; kind = CHANGEOVER / CLEANOUT."""
        if not a or a == b:
            return 0.0, "", ""
        m = str(m).strip()
        line = self.m2l.get(m)
        base, basis = None, ""
        if line:
            base = self.lut.get((line, a, b))
            if base is not None:
                basis = f"changeover matrix ({line})"
            elif line in self.miss_t:
                s_, x_ = self.miss_t[line]
                base = s_ if str(ta).upper() == str(tb).upper() else x_
                basis = f"pair not in matrix ({line}): item-type default"
            elif line in self.miss:
                base, basis = self.miss[line], f"pair not in matrix ({line}): plant default"
        kind = "CHANGEOVER"
        if base is None:
            if m in self.mixers:
                base, basis = self.mix_co, "mixer compound changeover"
                if self.butyl is not None:
                    cm, _ = self.butyl.cleanout_minutes(a, b, m)
                    if cm is not None:
                        base, basis, kind = cm, f"butyl clean-out ({self.butyl.cleanout(a, b)[1]})", "CLEANOUT"
            else:
                base, basis = 15.0, "no matrix line (default)"
        if "CAP 66" in a.upper() or "CAP 66" in b.upper():
            if base < self.cap66:
                base, basis, kind = self.cap66, "CAP 66 clean-out", "CLEANOUT"
        if m in self.mixers and {str(ta).upper(), str(tb).upper()} == {"MASTER COMPOUND", "FINAL COMPOUND"} \
                and self.tr100_item not in (a, b):
            if self.mix_co + self.tr100 >= base:
                base, basis, kind = self.mix_co + self.tr100, "TR100 master <-> final clean-out", "CLEANOUT"
        if a in self.after_butyl and base <= self.after_butyl_min:   # v39 Final Mixer - After Butyl
            base, basis, kind = self.after_butyl_min, f"after-butyl clean-out ({a})", "CLEANOUT"
        if m == "K430-2" and "FINAL COMPOUND" in {str(ta).upper(), str(tb).upper()} and base < 2.0:
            base, basis, kind = 2.0, "430-2 final-compound clean-out", "CLEANOUT"   # 430-2 Master Mixer - Final Compound
        return float(base), kind, basis

    def building(self, line, a, b):
        if not a or a == b:
            return 0.0, "", ""
        return self.bld.get(line, 0.0), "CHANGEOVER", f"different-size changeover ({line} building)"


def with_changeovers(df, mcol, icol, scol, ecol, kind_fn, row_fill):
    """Sort by machine + start, add duration_min / row_type, and insert a CHANGEOVER / CLEANOUT row between
    consecutive lots of different items on a machine. kind_fn(prev_row, row) -> (minutes, kind, basis);
    row_fill(prev_row, row, minutes, kind, basis) -> dict of the inserted row's display values."""
    d = df.sort_values([mcol, scol]).reset_index(drop=True)
    d.insert(0, "row_type", "LOT")
    d["duration_min"] = ((pd.to_datetime(d[ecol]) - pd.to_datetime(d[scol])).dt.total_seconds() / 60).round(1)
    out = []
    prev = None
    for r in d.to_dict("records"):
        if prev is not None and prev[mcol] == r[mcol] and str(prev[icol]) != str(r[icol]):
            mins, kind, basis = kind_fn(prev, r)
            if mins and mins > 0:
                st = pd.Timestamp(prev[ecol])
                row = {c: None for c in d.columns}
                row.update(row_fill(prev, r, mins, kind, basis))
                row.update({"row_type": kind, mcol: r[mcol], scol: st, ecol: st + pd.Timedelta(minutes=mins),
                            "duration_min": round(mins, 2)})
                out.append(row)
        out.append(r)
        prev = r
    return pd.DataFrame(out, columns=d.columns)


def read_sched(xlsx, prefix):
    """All sheets of one schedule kind ('Building', 'Curing', 'Upstream'): the PCR / TBR / shared sheets of
    v38+ workbooks or the single '<kind> schedule' sheet of older ones; lot rows only."""
    x = pd.ExcelFile(xlsx)
    names = [s for s in x.sheet_names if s.startswith(prefix + " ")]
    d = pd.concat([pd.read_excel(x, s) for s in names], ignore_index=True) if names else pd.DataFrame()
    if "row_type" in d.columns:
        d = d[d["row_type"] == "LOT"].drop(columns=["row_type", "duration_min"], errors="ignore")
    return d.reset_index(drop=True)
