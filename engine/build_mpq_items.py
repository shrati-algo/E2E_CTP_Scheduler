#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_mpq_items.py -- item-level MPQ from JK_CTP_PPDS.CTP_Master_MPQ (2026-10-06, v26).

    python build_mpq_items.py            -> inputs/mpq_item.csv (+ console summary)

Every row of the plant MPQ master (PCR and TBR) is mapped to the items the scheduler makes:
  * line        from CTP_Master_BOM.tyre_type of the item (PCR / TBR / both); an item used by both
                lines takes the LARGER of its two minimums (one run satisfies both masters)
  * item type   inputs/itemtype_master.csv (the type phase 2 uses)
  * Tread PCR   by rim size of the SKUs that use the tread (CTP_Master_BuildingAllowable.size):
                12-14 inch -> 310 m, 15-20 inch -> 206 m (mixed sizes -> the larger, 310)
  * Calendar roll  steel cord -> 'Calendar Roll (spool)', fabric -> 'Calendra Roll (Dipped Cord)'
                (class from the changeover matrix '4 RC Calendar', as the 4RC rules); BRTTL -> 'BRTTL'
  * Chaffer     PCR MONOCHAF80 / MONOCHAF120 -> 'Chaffer IL (mono 80, mono 120)'; TBR chaffer codes
                (CHF / CHAF / SNCT) -> 'Chaffer'
  * White SW    a PCR sidewall routed to the Quintoplex 1002 -> 'White SW (Quinto)'
  * STC         TBR item with STC in the code, in metres -> 'STC'; STC belt pieces (NOS) -> 'Belt'
  * Edge strip / edge gum: the master is gauge-specific (0.7 / 0.8 / 1 / 1.6) but NO table in
                JK_CTP_PPDS carries an item's gauge -> the SMALLEST value of the row is used and the
                item is flagged 'GAUGE NOT IN DB' (config mpq_item_overrides sets the real value)
  * Compounds   unchanged: 3 batches x the item's batch weight (mixing rule, phase 2)
config.yaml `mpq_item_overrides: {item: qty}` wins over everything.
"""
import os
import re

import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "db_pipeline", "raw")
INP = os.path.join(HERE, "inputs")


def main():
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    mq = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_MPQ.csv"))
    mq["t"] = mq["Item_Type"].astype(str).str.strip().str.upper()
    mq["p"] = mq["Parameters"].astype(str).str.strip()
    mq["q"] = pd.to_numeric(mq["Minimum_Run_Qty"], errors="coerce")
    mq["u"] = mq["UOM"].astype(str).str.strip().str.upper()
    mq["tt"] = mq["tyre_type"].astype(str).str.strip().str.upper()

    def row(tt, name, param=None):
        x = mq[(mq["tt"] == tt) & (mq["t"] == name.upper())]
        if param is not None:
            x = x[x["p"].str.contains(param, case=False, regex=False)]
        return (float(x["q"].min()), x["u"].iloc[0]) if len(x) else (None, "")

    itype = pd.read_csv(os.path.join(INP, "itemtype_master.csv"), dtype=str)
    itype = dict(zip(itype["ItemCode"].str.strip(), itype["ItemType"].astype(str).str.strip()))
    bom = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_BOM.csv"), dtype=str, low_memory=False)
    for c in ("Super_parent", "Parent", "child", "tyre_type", "child_Unit"):
        bom[c] = bom[c].astype(str).str.strip()
    tt = pd.concat([bom[["child", "tyre_type"]].rename(columns={"child": "item"}),
                    bom[["Parent", "tyre_type"]].rename(columns={"Parent": "item"})])
    tt = tt.groupby("item")["tyre_type"].agg(lambda s: {v.upper() for v in s if v.upper() in ("PCR", "TBR")})
    unit = bom.drop_duplicates("child").set_index("child")["child_Unit"].str.upper()
    rt = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_Routing.csv"), dtype=str, low_memory=False)
    rt["k"] = rt["routed_product"].astype(str).str.strip()
    mach = rt.groupby("k")["machines"].agg(lambda s: {x.strip().strip('"') for v in s for x in re.split(r"[,;]", str(v))})
    sz = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_BuildingAllowable.csv"), dtype=str)
    if "is_current" in sz.columns:                         # BR-17: current approved rows only
        sz = sz[pd.to_numeric(sz["is_current"], errors="coerce").fillna(1) == 1]
    sku_size = pd.to_numeric(sz["size"], errors="coerce").groupby(sz["sku_code"].str.strip()).first()
    # rim sizes of the SKUs that use each item
    sp = bom.groupby("child")["Super_parent"].agg(set)
    # 4RC class (steel / fabric) from the changeover matrix, as fourrc_checks
    mx = pd.read_csv(os.path.join(INP, "changeover_matrix.csv"), low_memory=False)
    mx = mx[mx["machine"].astype(str).str.strip() == "4 RC Calendar"]
    cl = {}
    nrm = lambda x: re.sub(r"[\s\-]", "", str(x).upper())
    for f, t_, ty in zip(mx["from_MaterialCode_O"], mx["to_MaterialCode_O"], mx["changeover_type"].fillna("").astype(str)):
        g = re.match(r"(fabric|steel) to (fabric|steel)", ty.strip().lower())
        if g:
            cl.setdefault(nrm(f), g.group(1))
            cl.setdefault(nrm(t_), g.group(2))
    cl.update({str(k).upper(): str(v).lower() for k, v in ((cfg.get("fourrc_rules") or {}).get("class_override") or {}).items()})
    ovr = {str(k).strip(): float(v) for k, v in (cfg.get("mpq_item_overrides") or {}).items()}

    def resolve(item, ity, line):
        """-> (qty, uom, master row, note) for one line, or (None, '', '', note)"""
        T, code = ity.upper(), item.upper()
        ms = mach.get(item, set())
        if "COMPOUND" in T:
            return None, "", "", "compound: 3 batches x batch weight (mixing rule)"
        if T == "CALANDARED ROLL":
            if code.startswith("BRTTL"):
                return (*row(line, "BRTTL"), "BRTTL", "") if line == "PCR" else (*row(line, "Calendar Roll (spool)"), "Calendar Roll (spool)", "")
            nm = "Calendra Roll (Dipped Cord)" if cl.get(nrm(item)) == "fabric" else "Calendar Roll (spool)"
            return (*row(line, nm), nm, "" if nrm(item) in cl else "cord class not in matrix -> spool")
        if T == "SLITTED MATERIAL":
            if code.startswith("BRTTL") and line == "PCR":
                return (*row(line, "BRTTL"), "BRTTL", "BRTTL slitted")
            if line == "PCR" and "MONOCHAF" in code:
                return (*row(line, "Chaffer IL( mono 80, mono 120)"), "Chaffer IL (mono 80, mono 120)", "")
            if line == "TBR" and any(k in code for k in ("CHF", "CHAF", "SNCT")):
                return (*row(line, "Chaffer"), "Chaffer", "")
            return None, "", "", "no MPQ row for this slitted item"
        if T == "TREAD":
            if line == "TBR":
                return (*row(line, "Tread"), "Tread", "")
            sizes = {sku_size.get(s) for s in sp.get(item, set())} - {None}
            sizes = {s for s in sizes if s == s}
            small = any(12 <= s <= 14 for s in sizes)
            if not sizes:
                return (*row(line, "Tread", "15 to 20"), "Tread 15-20 inch", "rim size unknown -> 15-20 inch row")
            return ((*row(line, "Tread", "12 to 14"), "Tread 12-14 inch", f"sizes {sorted(sizes)}") if small
                    else (*row(line, "Tread", "15 to 20"), "Tread 15-20 inch", f"sizes {sorted(sizes)}"))
        if T == "SIDEWALL":
            if line == "PCR" and "1002" in ms:
                return (*row(line, "White SW (Quinto)"), "White SW (Quinto)", "PCR sidewall on Quintoplex 1002")
            return (*row(line, "SideWall"), "SideWall", "")
        if T in ("STEEL BELT EDGE STRIP", "EDGE STRIP"):
            if line == "TBR":
                if "STC" in code:
                    return (*row(line, "STC"), "STC", "")
                return None, "", "", "no TBR MPQ row"
            if "1102" in ms:                                   # edge gum calender
                q, u = row(line, "Edge gum")
                return q, u, "Edge gum (smallest range)", "GAUGE NOT IN DB -> 300 (0.7/0.8 = 570)"
            q, u = row(line, "Edge strip")
            return q, u, "Edge strip (smallest gauge)", "GAUGE NOT IN DB -> 1350 (0.7=3990, 0.8=3000, 1=1650)"
        if T == "STEEL BELT":
            return (*row(line, "Steel Belt" if line == "PCR" else "Belt"), "Steel Belt" if line == "PCR" else "Belt", "")
        if T in ("PLY", "RUBBERIZED PLY"):
            if unit.get(item, "") == "M":
                return None, "", "", "ply item in metres -> no Nos row"
            return (*row(line, "Ply"), "Ply", "")
        simple = {"BEAD BUNDLE": ("Bead Bundle", "Wound Bead"), "BEAD APEX": ("Bead Apex", "Bead Apex"),
                  "INNER LINER": ("Inner Liner", "Inner Liner"), "CAP STRIP": ("Cap Strip", None),
                  "SHOULDER PAD": (None, "Shoulder Pad")}
        if T in simple:
            nm = simple[T][0 if line == "PCR" else 1]
            return (*row(line, nm), nm, "") if nm else (None, "", "", f"no {line} MPQ row")
        return None, "", "", "no MPQ row for this item type"

    items = sorted(set(itype) & (set(bom["child"]) | set(bom["Parent"])))
    rows = []
    for it in items:
        ity = itype.get(it, "")
        lines = sorted(tt.get(it, set())) or ["PCR"]
        res = {ln: resolve(it, ity, ln) for ln in lines}
        cand = [(r[0], r[1], f"{ln}: {r[2]}") for ln, r in res.items() if r[0]]
        if it in ovr:
            q, u, src = ovr[it], (cand[0][1] if cand else ""), "config mpq_item_overrides"
        elif cand:
            q, u, src = max(cand)
        else:
            q, u, src = None, "", ""
        rows.append({"item": it, "item_type": ity, "tyre_type": "+".join(lines), "mpq_min": q, "uom": u,
                     "master_row": src, "note": "; ".join(r[3] for r in res.values() if r[3])})
    out = pd.DataFrame(rows)
    out = out[out["item_type"].str.upper().ne("GREEN TYRES") & out["item_type"].ne("")]
    out.to_csv(os.path.join(INP, "mpq_item.csv"), index=False)
    # 4RC ROLL LENGTHS (CTP_Master_FRC): calender item -> spool / dip length D (max run, made at once) and
    # the minimum roll length (spool_length). Item codes matched without spaces / hyphens / MOTHERROLL.
    try:
        fr = pd.read_csv(os.path.join(RAW, "JK_CTP_PPDS__CTP_Master_FRC.csv"))
        fr = fr[pd.to_numeric(fr.get("is_current", 1), errors="coerce").fillna(1) == 1]
        al = {str(k).upper(): str(v).upper() for k, v in ((cfg.get("fourrc_rules") or {}).get("aliases") or {}).items()}
        key = lambda x: (lambda k: al.get(k, k))(re.sub(r"[\s\-]", "", str(x).upper()).replace("MOTHERROLL", ""))
        fk = {key(c): r for c, r in zip(fr["item_code"], fr.to_dict("records"))}
        cal = sorted({i for i, t in itype.items() if str(t).upper() in ("CALANDARED ROLL", "CAPSTRIP MOTHER ROLL")}
                     | {i for i, m in mach.items() if "901" in m})
        fl = []
        for it in cal:
            r = fk.get(key(it))
            if r is None:
                continue
            d = pd.to_numeric(r.get("Spool/Dip Length"), errors="coerce")
            fl.append({"item": it, "frc_item": r["item_code"], "class": r.get("item_type"),
                       "wire": str(r.get("wire_type") or "").strip(),
                       "spool_dip_length_m": d if d == d else None,
                       "min_roll_m": pd.to_numeric(r.get("spool_length"), errors="coerce")})
        fl = pd.DataFrame(fl)
        fl.to_csv(os.path.join(INP, "frc_lengths.csv"), index=False)
        print(f"  [FRC lengths] {fl['spool_dip_length_m'].notna().sum()} calender items with a spool/dip length, "
              f"{fl['spool_dip_length_m'].isna().sum()} without (min roll only) -> inputs/frc_lengths.csv")
    except Exception as _e:
        print(f"  [FRC lengths] !! CTP_Master_FRC not usable ({type(_e).__name__}: {_e})")
    s = out.dropna(subset=["mpq_min"])
    print(f"  [MPQ items] {len(s):,} items get an MPQ from CTP_Master_MPQ "
          f"({(s['tyre_type'] == 'PCR').sum()} PCR, {(s['tyre_type'] == 'TBR').sum()} TBR, "
          f"{s['tyre_type'].str.contains('[+]').sum()} both); gauge not in DB: "
          f"{out['note'].str.contains('GAUGE').sum()} items -> inputs/mpq_item.csv")
    return out


if __name__ == "__main__":
    o = main()
    pd.set_option("display.width", 250)
    print(o.dropna(subset=["mpq_min"]).groupby(["item_type", "master_row"]).size().to_string())
    print("no MPQ:", o[o["mpq_min"].isna()].groupby("item_type").size().to_dict())
