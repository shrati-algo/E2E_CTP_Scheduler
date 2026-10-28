"""Step 12 - persist the complete end-to-end schedule to JK_CTP_PPDS.CTP_Schedule_Floor.

    python -m pipeline.persist_floor --run <run> [--replace] [--validate-only]   (re-persist a finished run)
    (run_pipeline.py calls persist() + validate() itself at the end of every run)

SCHEDULE ID
    The run's ID is  YYYYMMDD_HHMMSS_<version>  (run start time + rules meta.run, e.g.
    20261028_162333_v46k). It is made once at the start of run_pipeline.py and written
    to outputs/<run>/SCHEDULE_ID.json.
    CTP_Schedule_Floor.schedule_id is a DATETIME column ("run start time of the
    schedule"), so the database holds the TIMESTAMP part of the ID; the version cannot
    be stored there. The ID text <-> schedule_id timestamp mapping is 1:1 and is kept in
    SCHEDULE_ID.json, the run log and RUN_SUMMARY.md.

ROWS
    Every row of phase5_floor_schedule_{pcr,tbr}.csv - upstream, building and curing, lots
    already cut at 07:00 / 15:00 / 23:00 (step 6) - one database row each. Mapping:
    see docs/steps/12_persist_schedule_floor.md and COLUMN_MAP below.

SAFETY
    * A schedule_id already present in the table is NOT written again (no duplicates);
      --replace deletes THAT schedule_id only and writes it again, in the same
      transaction. Other schedules are never touched.
    * Every row is checked against the table's column types and lengths before the
      write. Any failure: nothing is written, every failing row is listed.
    * One transaction: delete (if --replace) + all inserts, then COMMIT; any error ROLLBACK.
    * Parameterised INSERT (executemany), never string-built values.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from decimal import Decimal

import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from pipeline import db  # noqa: E402

TABLE = "CTP_Schedule_Floor"
COLS = ["schedule_id", "schedule_date", "schedule_shift", "tyre_type", "date", "shift", "machine",
        "machine_name", "department", "section", "process", "item", "item_description",
        "component_number", "item_type", "lot_id", "start_time", "end_time", "produce_qty",
        "UOM", "changeover_min", "changeover_from", "changeover_type", "mhe", "mhe_count",
        "consumer_machine", "compound", "die", "preformer"]
NOT_NULL = {"schedule_id", "schedule_date", "schedule_shift"}

COLUMN_MAP = {
    "schedule_id": "run start time (the timestamp of SCHEDULE_ID)",
    "schedule_date": "earliest plant date in the schedule (= plan day)",
    "schedule_shift": "shift of the earliest row (the shift the schedule starts in)",
    "tyre_type": "PCR / TBR - the floor-schedule file the row comes from "
                 "(phase5_floor_schedule_pcr / _tbr); shared machines carry the line of their lot",
    "date / shift": "floor row: plant day and shift of the piece (lots cut at 07/15/23)",
    "machine / machine_name": "floor row (work-centre id / plant name); a blank name is filled from "
                              "CTP_Machine_Master.machine_name",
    "department / process": "floor row",
    "section": "CTP_Machine_Master.section of the machine (Mixing, Stock Preparation, Building, "
               "Curing, Unclassified); NULL if the machine is not in the master",
    "item / item_type": "floor row: the item made (building: the green tyre); CURING: the FG SKU - "
                        "the floor file leaves curing items blank and the cured tyre is the SKU",
    "item_description": "building / curing: '<FG SKU> | <plan description>'; upstream: BOM "
                        "description of the item (+ ' | FG <sku>' when one SKU is served)",
    "lot_id": "floor lot_id; curing has none in the floor file -> '<LINE>_CUR_<press>_<nnnn>' "
              "(one id per press lot, shared by its shift pieces)",
    "start_time / end_time / produce_qty / UOM": "floor row (the piece inside the shift)",
    "changeover_min": "the lot's changeover on its FIRST piece (upstream phase5_schedule."
                      "changeover_min, building setup_applied_min, curing co_min); 0 on the later "
                      "shift pieces of the same lot (it carries on, no changeover)",
    "changeover_from": "item of the previous lot on the machine, when it differs (first piece)",
    "changeover_type": "building: plan CO_Type else setup source; curing: co_source; upstream: "
                       "'item change' when the item changes with a changeover",
    "consumer_machine": "engine floor_schedule.csv consumer_machine of the lot",
    "compound": "upstream non-mixing lots: the compound(s) mixed for this lot (phase-4 producer "
                "lots in a MIXING department)",
    "component_number, mhe, mhe_count, die, preformer": "NULL - the scheduler has no source for "
                                                        "them (no MHE / die / preformer master)",
}


# ------------------------------------------------------------------ schedule id
def make_schedule_id(started: dt.datetime, version: str) -> dict:
    ts = started.replace(microsecond=0)
    return {"schedule_id": f"{ts:%Y%m%d_%H%M%S}_{version}", "db_schedule_id": f"{ts:%Y-%m-%d %H:%M:%S}",
            "version": version}


def rules_version(rules_path: pathlib.Path) -> str:
    """The version the rule file declares (meta.run, e.g. v46k) - the repo's convention."""
    import yaml
    cfg = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    return str((cfg.get("meta") or {}).get("run") or "unknown")


# ------------------------------------------------------------------ build rows
def _bom_desc(data: pathlib.Path) -> dict:
    out = {}
    for ln in ("pcr", "tbr"):
        p = data / f"jkt_bom_{ln}.csv"
        if p.exists():
            b = pd.read_csv(p, usecols=["child", "child_description"], dtype=str)
            for k, v in zip(b["child"], b["child_description"]):
                if isinstance(k, str) and isinstance(v, str):
                    out.setdefault(k.strip(), v.strip())
    return out


def _sku_desc(run_dir: pathlib.Path) -> dict:
    out = {}
    for p in sorted((run_dir / "plan_from_db").glob("*.csv")):
        if p.name == "plan_catalogue.csv":
            continue
        d = pd.read_csv(p, dtype=str)
        col = "Description" if "Description" in d.columns else (
            "skuDescription" if "skuDescription" in d.columns else None)
        if col:
            for k, v in zip(d["SKUCode"], d[col]):
                if isinstance(k, str) and isinstance(v, str):
                    out.setdefault(k.strip(), v.strip())
    return out


def _machine_master() -> tuple[dict, dict]:
    """machine_no -> section, machine_name from CTP_Machine_Master (machine_no is unique)."""
    cn = db.connect()
    try:
        m = db.fetch("SELECT machine_no, section, machine_name FROM CTP_Machine_Master", cn)
    finally:
        cn.close()
    m["machine_no"] = m["machine_no"].astype(str).str.strip()
    return dict(zip(m["machine_no"], m["section"])), dict(zip(m["machine_no"], m["machine_name"]))


def build_rows(run_dir: pathlib.Path, data_dir: pathlib.Path, sid: dict) -> pd.DataFrame:
    parts = []
    for ln in ("pcr", "tbr"):
        p = run_dir / f"phase5_floor_schedule_{ln}.csv"
        if p.exists():
            d = pd.read_csv(p, encoding="utf-8-sig", dtype={"machine": str, "lot_id": str},
                            parse_dates=["start_time", "end_time"])
            d["_line"] = ln.upper()
            d["_src_row"] = range(1, len(d) + 1)
            d["_src_file"] = p.name
            parts.append(d)
    F = pd.concat(parts, ignore_index=True)
    F["lot_id"] = F["lot_id"].fillna("").astype(str)
    F["shift_part"] = F.get("shift_part", pd.Series("1/1", index=F.index)).fillna("1/1").astype(str)
    F["_first"] = F["shift_part"].str.startswith("1/")

    # curing lot ids: one per press lot, shared by its shift pieces
    cur = F["department"].eq("CURING")
    F["_lot"] = F["lot_id"]
    for (ln, m), g in F[cur].sort_values(["start_time"]).groupby(["_line", "machine"], sort=False):
        n = 0
        ids = []
        for first in g["_first"]:
            n += int(bool(first))
            ids.append(f"{ln}_CUR_{m}_{n:04d}")
        F.loc[g.index, "_lot"] = ids

    # per-lot facts from the engine outputs
    co, prev_item, co_type, consumer, compound = {}, {}, {}, {}, {}
    for ln in ("pcr", "tbr"):
        D = run_dir / ln
        s = pd.read_csv(D / "phase5_schedule.csv", parse_dates=["start", "finish"])
        s["machine"] = s["machine"].astype(str)
        for r in s.itertuples(index=False):
            co[r.lot_id] = float(r.changeover_min or 0) if pd.notna(r.changeover_min) else 0.0
        e = pd.read_csv(D / "phase4_edges.csv")
        mixed = set(s.loc[s["department"].astype(str).str.upper().str.contains("MIXING"), "lot_id"])
        item_of = dict(zip(s["lot_id"], s["item_code"]))
        for c, g in e[e["producer_lot"].isin(mixed)].groupby("consumer_lot"):
            compound[c] = ",".join(sorted({str(item_of[p]) for p in g["producer_lot"]}))
        b = pd.read_csv(D / "phase5_building_plan.csv")
        for r in b.itertuples(index=False):
            co[r.building_lot_id] = float(getattr(r, "setup_applied_min", 0) or 0)
            t = str(getattr(r, "setup_source", "") or "")
            co_type[r.building_lot_id] = t if t and t != "nan" else None
        f = pd.read_csv(D / "floor_schedule.csv", dtype={"lot_id": str})
        for k, v in zip(f["lot_id"], f["consumer_machine"]):
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                consumer.setdefault(k, v.strip())
        c = pd.read_csv(D / "phase7_curing_plan.csv", parse_dates=["feasible_start"])
        c["press"] = c["press"].astype(str)
        for r in c[c["feasible_start"].notna()].itertuples(index=False):
            key = (ln.upper(), r.press, pd.Timestamp(r.feasible_start).round("1s"))
            co[key] = float(r.co_min or 0) if pd.notna(r.co_min) else 0.0
            co_type[key] = (str(r.co_source) if isinstance(r.co_source, str) and r.co_source else None)
    plan_co_type = {}
    for p in (run_dir / "plan_from_db").glob("*BuildingPlan*.csv"):
        d = pd.read_csv(p, dtype=str)
        for sku, m, st, t in zip(d["SKUCode"], d["Machine"], d["StartTime"], d["CO_Type"]):
            if isinstance(t, str) and t.strip() and t.strip().lower() != "none":
                plan_co_type[(str(sku).strip(), pd.Timestamp(st))] = t.strip()

    # previous item on the machine (lot order), for changeover_from
    firsts = F[F["_first"]].sort_values(["machine", "start_time"])
    for m, g in firsts.groupby("machine", sort=False):
        prev = None
        for i, it in zip(g.index, g["item"].astype(str)):
            prev_item[i] = prev if prev is not None and prev != it else None
            prev = it

    bom_desc, sku_desc = _bom_desc(data_dir), _sku_desc(run_dir)
    section, mname = _machine_master()
    first_start = F["start_time"].min()
    first_row = F.loc[F["start_time"] == first_start].iloc[0]
    rows = []
    for i, r in F.iterrows():
        dep = str(r["department"]) if pd.notna(r["department"]) and str(r["department"]).strip() else None
        fg = r["FG_SKU_CODE"] if isinstance(r.get("FG_SKU_CODE"), str) else ""
        item = str(r["item"]) if pd.notna(r["item"]) and str(r["item"]).strip() else None
        if dep == "CURING":
            item = fg or item                      # the cured tyre IS the SKU
            desc = sku_desc.get(fg) if fg else None
        elif dep == "BUILDING":
            desc = f"{fg} | {sku_desc.get(fg, '')}".strip(" |") if fg else None
        else:
            desc = bom_desc.get(str(r["item"]).strip())
            if fg:
                desc = f"{desc or ''} | FG {fg}".strip(" |")
        first = bool(r["_first"])
        lot = r["_lot"] or None
        if dep == "CURING":
            key = (r["_line"], str(r["machine"]), pd.Timestamp(r["start_time"]).round("1s"))
            c_min = co.get(key) if first else 0.0     # later shift piece: no changeover
            c_typ = co_type.get(key) if first and co.get(key) else None
        elif dep == "BUILDING":
            c_min = co.get(lot) if first else 0.0
            c_typ = None
            if first and co.get(lot):
                c_typ = plan_co_type.get((fg, pd.Timestamp(r["start_time"]))) or co_type.get(lot)
        else:
            c_min = co.get(lot) if first else 0.0
            c_typ = "item change" if first and c_min and prev_item.get(i) else None
        rows.append({
            "schedule_id": sid["db_schedule_id"],
            "schedule_date": str(F["date"].min()),
            "schedule_shift": str(first_row["shift"]),
            "tyre_type": r["_line"],
            "date": str(r["date"]), "shift": r["shift"],
            "machine": str(r["machine"]),
            "machine_name": (r.get("machine_name") if isinstance(r.get("machine_name"), str)
                             and r.get("machine_name").strip() else mname.get(str(r["machine"]).strip())),
            "department": dep, "section": section.get(str(r["machine"]).strip()),
            "process": r.get("process"), "item": item,
            "item_description": desc, "component_number": None,
            "item_type": r.get("item_type"), "lot_id": lot,
            "start_time": pd.Timestamp(r["start_time"]).strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": pd.Timestamp(r["end_time"]).strftime("%Y-%m-%d %H:%M:%S"),
            "produce_qty": round(float(r["produce_qty"]), 3), "UOM": r.get("UOM"),
            "changeover_min": (round(float(c_min), 2) if c_min is not None else None),
            "changeover_from": prev_item.get(i) if first else None,
            "changeover_type": c_typ,
            "mhe": None, "mhe_count": None,
            "consumer_machine": consumer.get(lot) if lot else None,
            "compound": compound.get(lot) if lot and dep not in ("BUILDING", "CURING") else None,
            "die": None, "preformer": None,
            "_line": r["_line"], "_src_file": r["_src_file"], "_src_row": r["_src_row"],
        })
    out = pd.DataFrame(rows)
    for c in COLS:
        out[c] = out[c].where(pd.notna(out[c]), None)
    return out


# ------------------------------------------------------------------ checks
def column_types(cn) -> dict:
    d = db.fetch(f"SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
                 f"NUMERIC_SCALE, IS_NULLABLE FROM information_schema.COLUMNS "
                 f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{TABLE}'", cn)
    return {r.COLUMN_NAME: r for r in d.itertuples(index=False)}


def check_rows(rows: pd.DataFrame, types: dict) -> pd.DataFrame:
    """Every value against its column: missing column, NOT NULL, length, numeric range."""
    bad = []
    missing = [c for c in COLS if c not in types]
    if missing:
        bad.append({"row": None, "column": ",".join(missing), "value": None,
                    "reason": "column not in the table"})
    for c in COLS:
        if c not in types:
            continue
        t = types[c]
        v = rows[c]
        if t.IS_NULLABLE == "NO" or c in NOT_NULL:
            for i in rows.index[v.isna()]:
                bad.append({"row": int(rows.at[i, "_src_row"]), "file": rows.at[i, "_src_file"],
                            "column": c, "value": None, "reason": "NULL in a NOT NULL column"})
        if t.DATA_TYPE in ("varchar", "char", "text"):
            junk = v.map(lambda x: isinstance(x, str) and x.strip().lower() in ("nan", "none", "nat"))
            for i in rows.index[junk]:
                bad.append({"row": int(rows.at[i, "_src_row"]), "file": rows.at[i, "_src_file"],
                            "column": c, "value": v[i], "reason": "a missing value written as text"})
        if t.DATA_TYPE in ("varchar", "char") and pd.notna(t.CHARACTER_MAXIMUM_LENGTH):
            n = int(t.CHARACTER_MAXIMUM_LENGTH)
            lens = v.map(lambda x: len(str(x)) if x is not None else 0)
            for i in rows.index[lens > n]:
                bad.append({"row": int(rows.at[i, "_src_row"]), "file": rows.at[i, "_src_file"],
                            "column": c, "value": str(v[i])[:60],
                            "reason": f"length {lens[i]} > {n}"})
        if t.DATA_TYPE == "decimal":
            lim = 10 ** (int(t.NUMERIC_PRECISION) - int(t.NUMERIC_SCALE))
            for i in rows.index[v.map(lambda x: x is not None and abs(float(x)) >= lim)]:
                bad.append({"row": int(rows.at[i, "_src_row"]), "file": rows.at[i, "_src_file"],
                            "column": c, "value": v[i], "reason": f"out of DECIMAL range ({lim:g})"})
    return pd.DataFrame(bad)


# ------------------------------------------------------------------ write
def persist(run_dir: pathlib.Path, data_dir: pathlib.Path, sid: dict, replace: bool = False,
            execute: bool = True, log=print) -> dict:
    out = run_dir / "db_persist"
    out.mkdir(parents=True, exist_ok=True)
    res = {"schedule_id": sid["schedule_id"], "db_schedule_id": sid["db_schedule_id"],
           "table": f"JK_CTP_PPDS.{TABLE}", "written": False}
    rows = build_rows(run_dir, data_dir, sid)
    rows.to_csv(out / "rows_prepared.csv", index=False, encoding="utf-8-sig")
    res["prepared"] = int(len(rows))
    res["prepared_by_department"] = rows.groupby("department").size().to_dict()
    res["prepared_by_section"] = rows["section"].fillna("(not in machine master)").value_counts().to_dict()
    log(f"  schedule_id {sid['schedule_id']}  ->  {TABLE}.schedule_id = {sid['db_schedule_id']}")
    log(f"  rows prepared : {len(rows):,}  "
        + ", ".join(f"{k} {v:,}" for k, v in sorted(res["prepared_by_department"].items())))
    cn = db.connect()
    try:
        bad = check_rows(rows, column_types(cn))
        if len(bad):
            bad.to_csv(out / "rows_failed_validation.csv", index=False, encoding="utf-8-sig")
            res["validation_failures"] = int(len(bad))
            res["error"] = "rows failed the column checks - nothing written"
            log(f"  [FAIL] {len(bad)} value(s) fail the column checks -> db_persist/rows_failed_validation.csv; "
                f"NOTHING written")
            for r in bad.head(10).to_dict("records"):
                log(f"      {r}")
            return _done(out, res)
        cur = cn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM `{TABLE}` WHERE schedule_id = %s", (sid["db_schedule_id"],))
        existing = int(cur.fetchone()[0])
        res["existing_before"] = existing
        if existing and not replace:
            res["error"] = (f"{existing:,} rows already exist for this schedule_id - not written again "
                            f"(use --replace to rewrite this schedule only)")
            log(f"  [SKIP] {res['error']}")
            return _done(out, res)
        if not execute:
            res["error"] = "dry run - nothing written"
            log("  dry run: nothing written")
            return _done(out, res)
        try:
            if existing:
                cur.execute(f"DELETE FROM `{TABLE}` WHERE schedule_id = %s", (sid["db_schedule_id"],))
                res["deleted"] = cur.rowcount
            recs = [tuple(_py(x) for x in t) for t in rows[COLS].itertuples(index=False, name=None)]
            sql = (f"INSERT INTO `{TABLE}` (" + ",".join(f"`{c}`" for c in COLS) + ") VALUES ("
                   + ",".join(["%s"] * len(COLS)) + ")")
            n = 0
            for k in range(0, len(recs), 1000):
                n += cur.executemany(sql, recs[k:k + 1000])
            cur.execute(f"SELECT COUNT(*) FROM `{TABLE}` WHERE schedule_id = %s", (sid["db_schedule_id"],))
            after = int(cur.fetchone()[0])
            if after != len(rows):
                raise RuntimeError(f"row count after insert {after} != prepared {len(rows)}")
            cn.commit()
            res.update(written=True, inserted=n, rows_in_table=after)
            log(f"  [OK] inserted {n:,} rows, committed ({after:,} rows now carry this schedule_id)")
        except Exception as e:                                   # noqa: BLE001
            cn.rollback()
            res["error"] = f"{type(e).__name__}: {e} - rolled back, nothing written"
            log(f"  [FAIL] {res['error']}")
    finally:
        cn.close()
    return _done(out, res)


def _py(x):
    if x is None:
        return None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return None if np.isnan(x) else float(x)
    if isinstance(x, float) and np.isnan(x):
        return None
    return x


def _done(out: pathlib.Path, res: dict) -> dict:
    (out / "persist_result.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return res


# ------------------------------------------------------------------ read back + compare
def _norm(df: pd.DataFrame) -> pd.DataFrame:
    d = df[COLS].copy()
    for c in ("schedule_id", "start_time", "end_time"):
        d[c] = pd.to_datetime(d[c]).dt.strftime("%Y-%m-%d %H:%M:%S")
    for c in ("schedule_date", "date"):
        d[c] = pd.to_datetime(d[c]).dt.strftime("%Y-%m-%d")
    for c in ("produce_qty", "changeover_min", "mhe_count"):
        d[c] = pd.to_numeric(d[c].map(lambda x: float(x) if isinstance(x, Decimal) else x),
                             errors="coerce").round(3)
    for c in COLS:
        if d[c].dtype == object:
            d[c] = d[c].map(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x))
                            else str(x))
    return d


def validate(run_dir: pathlib.Path, sid: dict, log=print) -> dict:
    """Query the table by schedule_id and compare with the prepared rows and the floor files."""
    out = run_dir / "db_persist"
    prep = pd.read_csv(out / "rows_prepared.csv", encoding="utf-8-sig", dtype=str,
                       keep_default_na=False, na_values=[""])
    cn = db.connect()
    try:
        got = db.fetch(f"SELECT {','.join('`'+c+'`' for c in COLS)} FROM `{TABLE}` "
                       f"WHERE schedule_id = '{sid['db_schedule_id']}'", cn)
    finally:
        cn.close()
    A, B = _norm(prep), _norm(got)
    res = {"schedule_id": sid["schedule_id"], "db_schedule_id": sid["db_schedule_id"],
           "rows_prepared": int(len(A)), "rows_in_db": int(len(B))}
    key = COLS
    ca = A.astype(str).value_counts(subset=key, dropna=False)
    cb = B.astype(str).value_counts(subset=key, dropna=False)
    j = pd.concat([ca.rename("prepared"), cb.rename("db")], axis=1).fillna(0)
    res["missing_in_db"] = int((j["prepared"] - j["db"]).clip(lower=0).sum())
    res["unexpected_in_db"] = int((j["db"] - j["prepared"]).clip(lower=0).sum())
    res["duplicates_in_db"] = int((cb[cb > 1] - 1).sum()) - int((ca[ca > 1] - 1).sum())
    # prepared rows are 1:1 with the floor schedule rows (same order): check every floor field
    floor = []
    for ln in ("pcr", "tbr"):
        p = run_dir / f"phase5_floor_schedule_{ln}.csv"
        if p.exists():
            f_ = pd.read_csv(p, encoding="utf-8-sig", dtype=str, keep_default_na=False, na_values=[""])
            f_["_line"] = ln.upper()
            floor.append(f_)
    F = pd.concat(floor, ignore_index=True)
    res["rows_in_floor_schedule"] = int(len(F))
    P_ = prep.reset_index(drop=True)
    diffs = {}
    if len(F) == len(P_):
        cur = F["department"].eq("CURING")
        checks = {
            "tyre_type": (F["_line"], P_["tyre_type"]),
            "date": (F["date"], P_["date"]), "shift": (F["shift"], P_["shift"]),
            "machine": (F["machine"], P_["machine"]), "department": (F["department"], P_["department"]),
            "process": (F["process"], P_["process"]), "item_type": (F["item_type"], P_["item_type"]),
            "UOM": (F["UOM"], P_["UOM"]),
            "start_time": (pd.to_datetime(F["start_time"]), pd.to_datetime(P_["start_time"])),
            "end_time": (pd.to_datetime(F["end_time"]), pd.to_datetime(P_["end_time"])),
            "produce_qty": (pd.to_numeric(F["produce_qty"]).round(3),
                            pd.to_numeric(P_["produce_qty"]).round(3)),
            "item (not curing)": (F.loc[~cur, "item"], P_.loc[~cur, "item"]),
            "lot_id (not curing)": (F.loc[~cur, "lot_id"], P_.loc[~cur, "lot_id"]),
            "item = FG SKU (curing)": (F.loc[cur, "FG_SKU_CODE"], P_.loc[cur, "item"]),
        }
        for k, (x, y) in checks.items():
            x = x.where(x.notna(), None)
            y = y.where(y.notna(), None)
            diffs[k] = int((x.astype(str) != y.astype(str)).sum())
    else:
        diffs["row count"] = abs(len(F) - len(P_))
    res["floor_vs_prepared_mismatches"] = diffs
    grp = lambda d: d["department"].map(lambda x: x if x in ("BUILDING", "CURING") else "UPSTREAM")
    F["produce_qty"] = pd.to_numeric(F["produce_qty"])
    cov = pd.DataFrame({
        "floor_rows": F.groupby(grp(F)).size(),
        "db_rows": B.groupby(grp(B)).size(),
        "floor_qty": F.groupby(grp(F))["produce_qty"].sum().round(3),
        "db_qty": B.groupby(grp(B))["produce_qty"].sum().round(3)}).fillna(0)
    cov.index.name = "stage"
    res["coverage"] = cov.reset_index().to_dict("records")
    res["stages_present"] = sorted(set(grp(B)))
    tt = pd.DataFrame({"floor_rows": F.groupby("_line").size(),
                       "db_rows": B.groupby(B["tyre_type"].fillna("(null)")).size()}).fillna(0)
    res["by_tyre_type"] = {k: {"floor": int(v["floor_rows"]), "db": int(v["db_rows"])}
                           for k, v in tt.iterrows()}
    res["passed"] = bool(res["rows_prepared"] == res["rows_in_db"] == len(F)
                         and res["missing_in_db"] == 0 and res["unexpected_in_db"] == 0
                         and res["duplicates_in_db"] == 0
                         and sum(diffs.values()) == 0
                         and all(v["floor"] == v["db"] for v in res["by_tyre_type"].values())
                         and set(res["stages_present"]) == {"UPSTREAM", "BUILDING", "CURING"})
    j[(j["prepared"] != j["db"])].reset_index().to_csv(out / "validation_row_differences.csv",
                                                      index=False, encoding="utf-8-sig")
    (out / "validation_result.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    log(f"  read back {len(B):,} rows for schedule_id {sid['db_schedule_id']} "
        f"(prepared {len(A):,}, floor schedule {len(F):,})")
    for r in res["coverage"]:
        log(f"      {r['stage']:9s} floor {int(r['floor_rows']):>5,} rows / {r['floor_qty']:>14,.3f}   "
            f"db {int(r['db_rows']):>5,} rows / {r['db_qty']:>14,.3f}")
    log(f"  DB vs prepared rows: missing {res['missing_in_db']}, unexpected {res['unexpected_in_db']}, "
        f"duplicates {res['duplicates_in_db']}")
    log("  prepared vs floor schedule, mismatching rows per field: "
        + ", ".join(f"{k} {v}" for k, v in diffs.items()))
    log("  by tyre_type: " + ", ".join(f"{k} floor {v['floor']:,} / db {v['db']:,}"
                                        for k, v in res["by_tyre_type"].items()))
    log(f"  stages present in the DB: {', '.join(res['stages_present'])}   ->  "
        + ("[PASS]" if res["passed"] else "[FAIL]"))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Persist / validate a finished run in CTP_Schedule_Floor")
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default=str(ROOT / "outputs"))
    ap.add_argument("--replace", action="store_true", help="rewrite this schedule_id only")
    ap.add_argument("--validate-only", action="store_true")
    a = ap.parse_args()
    db.load_db_env()
    run_dir = pathlib.Path(a.out) / a.run
    sid = json.loads((run_dir / "SCHEDULE_ID.json").read_text(encoding="utf-8"))
    st = json.loads((run_dir / "DATA_STAGED.json").read_text(encoding="utf-8")) \
        if (run_dir / "DATA_STAGED.json").exists() else {}
    data = ROOT / "data" / f"ppds_{st.get('plan_day', '')}"
    if not a.validate_only:
        persist(run_dir, data, sid, replace=a.replace)
    return 0 if validate(run_dir, sid)["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
