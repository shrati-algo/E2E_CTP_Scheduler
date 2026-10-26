#!/usr/bin/env python3
"""ctpsql.py — the live connection to jkplanning_CTP, and the WC staging step.

    python ctpsql.py --stage          # pull the WC masters into ../data/
    python ctpsql.py --tables         # list every table with its row count

READ ONLY. Nothing in this module writes to the database; every statement is a
SELECT. Credentials come from the repository's own `export_db_tables.py`
defaults, overridable by CTP_DB_HOST / _PORT / _USER / _PASSWORD / _NAME.

WHY THE WC MASTERS ARE STAGED RATHER THAN READ LIVE
  A schedule has to be reproducible. Reading the register live means a run made
  today cannot be repeated tomorrow if someone edits a work centre in between.
  So the three tables are pulled into ../data/ with a stamp, and the engine
  reads the staged copy — the same reasoning as BTP's db_staging step.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAGE = pathlib.Path(os.environ.get("CTP_STAGE") or (ROOT / "data"))

CFG = {
    "host": os.environ.get("CTP_DB_HOST", ""),
    "port": int(os.environ.get("CTP_DB_PORT", "3306")),
    "user": os.environ.get("CTP_DB_USER", ""),
    "password": os.environ.get("CTP_DB_PASSWORD", ""),
    "database": os.environ.get("CTP_DB_NAME", ""),
}

# The work-centre register, which SUPERSEDES MasterMachines.
#   Master_WC_Master            327 work centres, wcID is the canonical key
#   Master_WC_Master_Curing_pcr  92 presses x 2 mould positions (LHS/RHS)
#   Master_WC_Master_Curing_tbr  80 presses x 2
WC_TABLES = ["Master_WC_Master",
             "Master_WC_Master_Curing_pcr",
             "Master_WC_Master_Curing_tbr"]


# The MDM database. Masters live here in a versioned (SCD2) form; the
# transactional backbone - BOM, routing, changeover - does NOT, so this is a
# SECOND source, never a replacement. See refresh_ctp_sql.SOURCES.
PPDS_DB = os.environ.get("CTP_PPDS_NAME", "")


def connect(db: str | None = None):
    """A read-only connection. `db` overrides the default schema.

    Passing the schema per-connection rather than mutating CFG keeps the two
    databases independent: a failure staging one cannot leave the module
    pointing at the other.
    """
    import pymysql
    cfg = dict(CFG)
    if db:
        cfg["database"] = db
    return pymysql.connect(connect_timeout=30, read_timeout=300, **cfg)


def fetch(sql: str, cn=None, db: str | None = None) -> pd.DataFrame:
    own = cn is None
    cn = cn or connect(db)
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return pd.read_sql(sql, cn)
    finally:
        if own:
            cn.close()


def stage(tables=None) -> None:
    STAGE.mkdir(parents=True, exist_ok=True)
    cn = connect()
    man = {"database": CFG["database"], "host": CFG["host"],
           "staged_at": dt.datetime.now().isoformat(timespec="seconds"),
           "tables": {}}
    try:
        for t in (tables or WC_TABLES):
            d = fetch(f"SELECT * FROM `{t}`", cn)
            p = STAGE / f"{t}.csv"
            d.to_csv(p, index=False, encoding="utf-8-sig")
            man["tables"][t] = {"rows": int(len(d)),
                                "columns": list(map(str, d.columns))}
            print(f"  staged {t:32s} {len(d):>6,d} rows -> {p.name}")
    finally:
        cn.close()
    (STAGE / "_wc_manifest.json").write_text(json.dumps(man, indent=2),
                                             encoding="utf-8")
    print(f"  manifest -> {STAGE/'_wc_manifest.json'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--tables", action="store_true")
    args = ap.parse_args()
    print(f"  jkplanning_CTP @ {CFG['host']}:{CFG['port']} (read only)")
    if args.tables:
        d = fetch("SELECT table_name, table_rows FROM information_schema.tables "
                  f"WHERE table_schema='{CFG['database']}' ORDER BY table_name")
        print(d.to_string(index=False))
    if args.stage or not args.tables:
        stage()
    return 0


if __name__ == "__main__":
    sys.exit(main())
