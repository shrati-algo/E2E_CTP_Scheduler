#!/usr/bin/env python3
"""rules.py — the plant rule file, loaded and applied.

`config/rules.yaml` is the ONLY knob source. Nothing in the engine hard-codes a
plant rule; this module reads the file and applies BR-1..BR-4, recording every
application so the run can report which rule touched which item.

  BR-1  effective line speed = parallel rolls/bobins x single-strand speed,
        including the per-(item, machine) cap strip slitter speeds
  BR-2  chaffer slitter = rate x N strips
  BR-3  one bias/belt cut makes BOTH belts of a tyre  (pieces_per_cut = 2)
  BR-4  authoritative component aging by item type, overriding the DB master
"""
from __future__ import annotations

import os
import pathlib
from collections import defaultdict

import yaml

CFG_PATH = pathlib.Path(os.environ.get("CTP_RULES") or
                        (pathlib.Path(__file__).resolve().parent.parent
                         / "config" / "rules.yaml"))

_CFG: dict | None = None
APPLIED: dict[str, list] = defaultdict(list)     # rule -> [(item, machine, detail)]


def _merge(base: dict, over: dict) -> dict:
    """`over` wins, but only key by key — a section present in the parent and
    absent from the child survives. Without this, naming rules_upstream.yaml
    would silently drop BR-1..BR-5, which live in rules.yaml."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def cfg() -> dict:
    global _CFG
    if _CFG is None:
        # `inherit_from:` pulls the parent in UNDER this one, and the parent
        # may inherit in turn - a variant file inherits rules_upstream.yaml,
        # which inherits rules.yaml, which is where BR-1..BR-5 live. Resolving
        # only ONE level silently dropped them, so the chain is walked to the
        # end (with a seen-set, because a cycle would otherwise hang).
        chain, seen, path = [], set(), CFG_PATH
        while path and path.exists() and str(path) not in seen:
            seen.add(str(path))
            node = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            chain.append(node)
            par = node.get("inherit_from")
            path = (path.parent / str(par)) if par else None
        c = {}
        for node in reversed(chain):          # oldest ancestor first
            c = _merge(c, node)
        _CFG = c
    return _CFG


def rule(path: str, default=None):
    """Read a nested knob by dotted path, e.g. rule('mixing.mpq_batches')."""
    node = cfg()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def engine(key: str, default=None):
    return (cfg().get("engine") or {}).get(key, default)


def _norm(x) -> str:
    return str(x or "").strip()


# ------------------------------------------------------------------ BR-1 / 2
def strand_multiplier(item: str, machine: str | None = None) -> tuple[float, str]:
    """(multiplier, why). 1.0 with an empty reason when no rule applies."""
    ls = cfg().get("linespeed") or {}
    if not ls.get("enabled", True):
        return 1.0, ""
    it = _norm(item)
    if it in set(ls.get("single_strand") or ()):
        return 1.0, "BR-1 single-strand (no plant roll count)"
    rolls = ls.get("rolls") or {}
    if it in rolls:
        return float(rolls[it]), f"BR-1 roll table x{rolls[it]}"
    slit = ls.get("slit_inherits") or {}
    if it in slit:
        n = float(slit[it]["rolls"])
        return n, f"BR-1 slit inherits {slit[it]['parent']} x{n:g}"
    ch = ls.get("chaffer_strips") or {}
    if it in ch:
        return float(ch[it]), f"BR-2 chaffer x{ch[it]} strips"
    return 1.0, ""


def effective_rate(item: str, machine: str | None, base_rate, uom: str,
                   operation: str = "") -> tuple[float | None, str]:
    """The rate to actually schedule at, after BR-1/BR-2/BR-3.

    Returns (rate, why). `why` is empty when the routing's own rate stands.
    """
    if base_rate is None:
        return None, ""
    rate, why = float(base_rate), []

    # BR-1 per-machine override: the two cap strip slitters are NOT the same
    # machine, and this is the only per-(item, machine) spec CTP has.
    pm = ((cfg().get("linespeed") or {}).get("per_machine") or {}).get(_norm(item))
    if pm and machine is not None and _norm(machine) in pm:
        e = pm[_norm(machine)]
        rate = float(e.get("effective")
                     or (float(e["base_mpm"]) * float(e["strands"])))
        why.append(f"BR-1 machine {machine}: {e.get('base_mpm')} x "
                   f"{e.get('strands')} = {rate:g}")
    else:
        m, w = strand_multiplier(item, machine)
        if m != 1.0:
            rate *= m
            why.append(w)

    # BR-3: one cut makes two belts, so the machine does HALF the cuts.
    ppc = cfg().get("pieces_per_cut") or {}
    if ppc.get("enabled", True) and str(uom).upper() == "CUTS/MIN":
        n = (ppc.get("by_operation") or {}).get(_norm(operation))
        if n and float(n) > 1:
            rate *= float(n)
            why.append(f"BR-3 {operation}: 1 cut = {n:g} pieces")

    if why:
        APPLIED["speed"].append((item, machine, "; ".join(why),
                                 float(base_rate), rate))
    return rate, "; ".join(why)


def apply_speed_rules(meta: dict[str, dict]) -> dict[str, dict]:
    """Rewrite each routing item's rate in place (the machine-independent part).

    Items with a per-(item, machine) spec keep the routing rate here and are
    re-timed per machine in phase 5, exactly as v10 re-times from
    all_machine_spec. `rate_by_machine` carries the per-machine answer.
    """
    for it, m in meta.items():
        if m.get("proc_time") is None:
            continue
        pm = ((cfg().get("linespeed") or {}).get("per_machine") or {}).get(_norm(it))
        if pm:
            m["rate_by_machine"] = {
                k: float(v.get("effective") or float(v["base_mpm"]) * float(v["strands"]))
                for k, v in pm.items()}
            # size on the SLOWEST eligible machine, as v10 sizes to the smallest
            # batch: the lot is then legal wherever it is placed
            slow = min(m["rate_by_machine"].values())
            APPLIED["speed"].append((it, "|".join(pm), f"BR-1 per-machine "
                                     f"{sorted(m['rate_by_machine'].values())}",
                                     m["proc_time"], slow))
            m["proc_time"] = slow
            continue
        r, why = effective_rate(it, None, m["proc_time"], m["proc_time_uom"],
                                m.get("operation", ""))
        if why:
            m["proc_time"] = r
    return meta


def machine_rate(item: str, machine: str, meta: dict) -> float | None:
    """The rate for THIS machine, for phase 5's re-timing."""
    rbm = (meta or {}).get("rate_by_machine") or {}
    if _norm(machine) in rbm:
        return rbm[_norm(machine)]
    return (meta or {}).get("proc_time")


# ---------------------------------------------------------------------- BR-4
def bias_operations() -> list[str]:
    """BR-5: the routing operation names that ARE the bias cutter.

    Two spellings for one machine — PCR's routing says `BIAS CUTTER`, TBR's
    says `Bios cutter` — so this cannot be a single constant.
    """
    b = cfg().get("bias_cutter") or {}
    if not b.get("enabled", True):
        return []
    return list(b.get("operations") or [])


def bias_note() -> str:
    b = cfg().get("bias_cutter") or {}
    return str(b.get("rule") or "")


def apply_aging_spec(aging: dict, itypes: dict) -> tuple[dict, list]:
    """Override the DB aging master with the plant's own age-limit table."""
    spec = cfg().get("aging_spec") or {}
    if not spec.get("enabled", True):
        return aging, []
    table = {str(k).strip().lower(): v for k, v in (spec.get("by_item_type") or {}).items()}
    changed = []
    for item, t in itypes.items():
        e = table.get(str(t).strip().lower())
        if not e:
            continue
        old = aging.get(item, (0.0, 0.0))
        new = (float(e.get("min", old[0])), float(e.get("max", old[1])))
        if abs(new[0] - old[0]) > 1e-9 or abs(new[1] - old[1]) > 1e-9:
            changed.append((item, t, old, new))
        aging[item] = new
    if changed:
        APPLIED["aging"].extend(changed)
    return aging, changed


# ------------------------------------------------------------------ reporting
def report(prefix: str = "  ") -> None:
    sp = APPLIED.get("speed") or []
    ag = APPLIED.get("aging") or []
    print(f"{prefix}RULES from {CFG_PATH.name}")
    if sp:
        seen = {}
        for it, mc, why, old, new in sp:
            seen.setdefault(it, (mc, why, old, new))
        print(f"{prefix}  BR-1/BR-2/BR-3 speed  : {len(seen)} item(s) re-rated")
        for it, (mc, why, old, new) in list(seen.items())[:10]:
            print(f"{prefix}    {str(it)[:20]:21s} {old:>7.1f} -> {new:>8.1f}   {why[:52]}")
    else:
        print(f"{prefix}  BR-1/BR-2/BR-3 speed  : nothing matched")
    if ag:
        print(f"{prefix}  BR-4 aging override   : {len(ag)} item(s)")
        byt = {}
        for it, t, old, new in ag:
            byt.setdefault(t, (old, new, 0))
            o, n, c = byt[t]
            byt[t] = (o, n, c + 1)
        for t, (old, new, c) in list(byt.items())[:12]:
            print(f"{prefix}    {str(t)[:26]:27s} {old[0]:g}/{old[1]:g} h -> "
                  f"{new[0]:g}/{new[1]:g} h   ({c} item(s))")
    for f in (cfg().get("aging_spec") or {}).get("open_flags") or []:
        print(f"{prefix}  [OPEN] {f}")
