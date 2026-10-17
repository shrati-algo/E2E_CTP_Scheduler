#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""butyl_rule.py -- BR-V2 (2026-10-01): butyl-friendly compound sequencing on the mixers.

  FINAL MIXER : before 30G / IL3 / IL5 the mixer must have run B1, B2 or B99.
  MASTER MIXER: before AND after 29MG / ML405 / ML605 the mixer must run MB100, MB200, XMB98 or
                XMB98 Repass.
  If the sequence is not followed, a CLEAN-OUT CYCLE of 160 s is charged INSTEAD OF the normal
  changeover (the clean-out already cleans the mixer, so no changeover is added on top).

BR-V13 (2026-10-01, user): the clean-out is set PER GROUP (group keys override the global ones):
  final : cleanout_basis fixed, cleanout_sec 120 -> a broken sequence costs a 120 s clean-out
  master: cleanout_basis none                     -> NO clean-out; only the normal compound-to-compound
          changeover (mixer_changeover_min, 60 s). The break is still counted (sequence_breaks).

Rules (config business_rules.butyl_sequence_rule), for a transition a -> b on one mixer:
  a == b (same compound continues)                        -> no changeover, no clean-out
  group 'before' check: b is a target, a is not a prep   -> clean-out
  group 'after'  check: a is a target, b is not a prep   -> clean-out
  one transition breaking both checks                    -> ONE clean-out, not two
A target is NOT a prep for another target (literal rule; `targets_count_as_prep: true` relaxes it).

    python butyl_rule.py        # applicability report: which codes / mixers the rule cannot use
"""
import os
import re

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))


class ButylRule:
    def __init__(self, block):
        block = block or {}
        self.enabled = bool(block.get("enabled", False))
        self.cleanout_min = float(block.get("cleanout_sec", 160)) / 60.0      # fixed / fallback
        # BR-V10: cleanout_basis one_batch_cycle -> clean-out = ONE batch of the compound being
        # cleaned out (the compound in the mixer before the clean-out):
        #     T_cleanout (s) = proc_time (MIN/BATCH, CTP_Master_Routing, that mixer) x 60 / efficiency
        self.basis = str(block.get("cleanout_basis", "fixed")).strip().lower()
        self.cycle_min = {}          # (compound, canonical mixer) -> minutes per batch (proc_time / eff)
        self.cycle_any = {}          # compound -> minutes per batch on any mixer (fallback)
        self.groups = []
        for name in ("final", "master"):
            g = block.get(name) or {}
            tg = {str(x).strip() for x in g.get("targets") or []}
            pr = {str(x).strip() for x in g.get("prep") or []}
            if block.get("targets_count_as_prep", False):
                pr |= tg
            fam = {str(x).strip() for x in g.get("family") or []}   # e.g. IL3<->IL5: plain changeover
            self.groups.append({"name": name, "targets": tg, "prep": pr, "family": fam,
                                "check": {str(x).lower() for x in g.get("check") or ["before"]},
                                # BR-V13: per-group clean-out (none = only the normal changeover)
                                "basis": str(g.get("cleanout_basis", self.basis)).strip().lower(),
                                "co_min": float(g.get("cleanout_sec", self.cleanout_min * 60)) / 60.0})
        self.targets = set().union(*(g["targets"] for g in self.groups)) if self.groups else set()

    def load_cycles(self, source, root=HERE):
        """(compound, mixer) -> minutes for ONE batch = proc_time / efficiency (MIN/BATCH rows)."""
        import sys
        sys.path.insert(0, root)
        from machine_normalize import normalize_machine
        path = source if os.path.isabs(source) else os.path.join(root, source)
        rt = pd.read_csv(path, dtype=str, low_memory=False)
        try:                                                  # v37 (user): efficiency taken as 1 everywhere
            import yaml as _y
            _eov = (_y.safe_load(open(os.path.join(root, "config.yaml"), encoding="utf-8")) or {}).get("efficiency_override")
        except Exception:
            _eov = None
        if _eov is not None:
            rt["efficiency"] = str(_eov)
        mx = rt[rt["operation_name"].astype(str).str.upper().str.contains("MIXING")]
        for c, ms, pt, u, ef in zip(mx["routed_product"].astype(str).str.strip(), mx["machines"],
                                    pd.to_numeric(mx["proc_time"], errors="coerce"), mx["proc_time_UOM"],
                                    pd.to_numeric(mx["efficiency"], errors="coerce")):
            if pd.isna(pt) or str(u).strip().upper() != "MIN/BATCH":
                continue
            v = float(pt) / (float(ef) if ef and ef > 0 else 1.0)
            for m in re.findall(r'"([^"]+)"', str(ms)):
                self.cycle_min[(c, normalize_machine(m))] = v
            self.cycle_any.setdefault(c, v)
        # gap-filled compounds (e.g. XMT180 / GL7) live only in the adapted routing
        inp = os.path.join(root, "inputs", "routing.csv")
        if os.path.exists(inp):
            ri = pd.read_csv(inp, dtype=str, low_memory=False)
            ri = ri[ri["operation_name"].astype(str).str.upper().str.contains("MIXING")]
            if _eov is not None:
                ri = ri.assign(efficiency=str(_eov))
            for c, pt, u, ef in zip(ri["routed_product"].astype(str).str.strip(),
                                    pd.to_numeric(ri["proc_time"], errors="coerce"), ri["proc_time_UOM"],
                                    pd.to_numeric(ri["efficiency"], errors="coerce")):
                if c in self.cycle_any or pd.isna(pt):
                    continue
                uu = str(u).strip().upper()
                m_ = float(pt) / 60.0 if uu == "SEC/BATCH" else float(pt) if uu == "MIN/BATCH" else None
                if m_:
                    self.cycle_any[c] = m_ / (float(ef) if ef and ef > 0 else 1.0)
        return self

    def cleanout_minutes(self, a, b, machine=None):
        """(minutes, basis) of the clean-out a -> b on `machine`, or (None, '') when none is needed.
        Several broken groups on one transition -> ONE clean-out, the longest."""
        best = (None, "")
        for g in self._broken(a, b, charged_only=True):
            v = (self._batch_minutes(a, machine) if g["basis"] == "one_batch_cycle"
                 else (g["co_min"], f"fixed {g['co_min'] * 60:.0f} s ({g['name']})"))
            if best[0] is None or v[0] > best[0]:
                best = v
        return best

    def _batch_minutes(self, a, machine):
        a = str(a or "").strip()
        v = None
        if machine is not None:
            try:
                from machine_normalize import normalize_machine
                v = self.cycle_min.get((a, normalize_machine(machine)))
            except Exception:
                v = self.cycle_min.get((a, str(machine)))
        if v is None:
            v = self.cycle_any.get(a)
        return (v, f"1 batch of {a}") if v else (self.cleanout_min, f"fallback {self.cleanout_min * 60:.0f} s (no rate for {a})")

    def _broken(self, a, b, charged_only):
        """groups whose sequence a -> b breaks (with the reason in g['_why'])."""
        a, b = str(a or "").strip(), str(b or "").strip()
        if not self.enabled or not a or not b or a == b:
            return []
        out = []
        for g in self.groups:
            if charged_only and g["basis"] == "none":
                continue
            if a in g["family"] and b in g["family"]:
                continue                                  # same butyl family in one campaign
            why = []
            if "before" in g["check"] and b in g["targets"] and a not in g["prep"]:
                why.append(f"{g['name']}: {b} not preceded by {'/'.join(sorted(g['prep'] - g['targets']))}")
            if "after" in g["check"] and a in g["targets"] and b not in g["prep"]:
                why.append(f"{g['name']}: {a} not followed by {'/'.join(sorted(g['prep'] - g['targets']))}")
            if why:
                out.append(dict(g, _why="; ".join(why)))
        return out

    def cleanout(self, a, b):
        """(True, reason) when a -> b on a mixer needs the clean-out cycle, else (False, '')."""
        br = self._broken(a, b, charged_only=True)
        return (True, "; ".join(g["_why"] for g in br)) if br else (False, "")

    def sequence_breaks(self, a, b):
        """(True, reason) when a -> b breaks ANY group's sequence, charged with a clean-out or not."""
        br = self._broken(a, b, charged_only=False)
        return (True, "; ".join(g["_why"] for g in br)) if br else (False, "")

    def preps_before(self, b):
        """compounds that, run immediately before b, avoid its 'before' clean-out."""
        return set().union(*[g["prep"] for g in self.groups if "before" in g["check"] and b in g["targets"]] or [set()])

    def preps_after(self, a):
        """compounds that, run immediately after a, avoid its 'after' clean-out."""
        return set().union(*[g["prep"] for g in self.groups if "after" in g["check"] and a in g["targets"]] or [set()])


def load(cfg, root=HERE):
    br = cfg.get("business_rules") or {}
    r = ButylRule(br.get("butyl_sequence_rule"))
    if r.enabled and any(g["basis"] == "one_batch_cycle" for g in r.groups):
        r.load_cycles(br.get("mixing_batch_source")
                      or "INPUT_TABLES_30SEP_TRUE/01_DB_tables/JK_CTP_PPDS__CTP_Master_Routing.csv", root)
    return r


# ---------------------------------------------------------------- applicability report
def applicability(cfg, root=HERE):
    """Which rule compounds exist in the source routing / BOM, and on which mixers a target can
    run but none of its prep compounds can (so the sequence can never be followed there)."""
    import sys
    sys.path.insert(0, root)
    from machine_normalize import normalize_machine
    blk = (cfg.get("business_rules") or {}).get("butyl_sequence_rule") or {}
    src_dir = os.path.join(root, "INPUT_TABLES_30SEP_TRUE", "01_DB_tables")
    rt = pd.read_csv(os.path.join(src_dir, "JK_CTP_PPDS__CTP_Master_Routing.csv"), dtype=str, low_memory=False)
    mx = rt[rt["operation_name"].astype(str).str.upper().str.contains("MIXING")]
    mixers = {}
    for c, ms in zip(mx["routed_product"].astype(str).str.strip(), mx["machines"]):
        mixers.setdefault(c, set()).update(normalize_machine(m) for m in re.findall(r'"([^"]+)"', str(ms)))
    bom = pd.read_csv(os.path.join(src_dir, "JK_CTP_PPDS__CTP_Master_BOM.csv"), dtype=str, low_memory=False,
                      usecols=["Parent", "child"])
    in_bom = set(bom["Parent"].astype(str).str.strip()) | set(bom["child"].astype(str).str.strip())

    rows, gaps = [], []
    for name in ("final", "master"):
        g = blk.get(name) or {}
        tg = [str(x).strip() for x in g.get("targets") or []]
        pr = [str(x).strip() for x in g.get("prep") or []]
        for role, codes in (("target", tg), ("prep", pr)):
            for c in codes:
                rows.append({"group": name, "role": role, "compound": c,
                             "in_routing": c in mixers, "in_bom": c in in_bom,
                             "mixers": ", ".join(sorted(mixers.get(c, ()))) or "-",
                             "usable": c in mixers})
        prep_mixers = set().union(*[mixers.get(p, set()) for p in pr]) if pr else set()
        for t in tg:
            for m in sorted(mixers.get(t, ())):
                if m not in prep_mixers:
                    gaps.append({"group": name, "target": t, "mixer": m,
                                 "why": f"none of {'/'.join(pr)} is routed to {m} -> every {t} run on {m} "
                                        f"needs the clean-out ({'before and after' if 'after' in (g.get('check') or []) else 'before'})"})
    return pd.DataFrame(rows), pd.DataFrame(gaps)


if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    r = load(cfg)
    print(f"butyl_sequence_rule enabled={r.enabled} clean-out={r.cleanout_min * 60:.0f} s")
    codes, gaps = applicability(cfg)
    print("\nCOMPOUNDS (source JK_CTP_PPDS.CTP_Master_Routing / CTP_Master_BOM):")
    print(codes.to_string(index=False))
    print("\nMIXERS WHERE THE SEQUENCE CAN NEVER BE FOLLOWED (clean-out every time):")
    print(gaps.to_string(index=False) if len(gaps) else "  none")
