# -*- coding: utf-8 -*-
"""
db_connect.py — the ONE place that opens a connection to jkplanning_CTP.

Credentials come from db_pipeline/.env (or real environment variables, which win).
NOTHING here hard-codes a password: if .env is missing the import still works and
get_engine() raises a clear, actionable error instead of a stack trace.

    from db_connect import get_engine, read_table, table_exists
"""
from __future__ import annotations
import os
import pathlib
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"

_DEFAULTS = {"DB_HOST": "", "DB_PORT": "3306", "DB_NAME": "jkplanning_CTP",
             "DB_USER": "", "DB_PASSWORD": ""}


def load_env(env_file: pathlib.Path = ENV_FILE) -> dict:
    """Read .env into a dict. REAL environment variables override the file, so a
    CI/cron run can inject credentials without editing anything on disk."""
    cfg = dict(_DEFAULTS)
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if not cfg.get("DB_USER") and cfg.get("DB_USERNAME"):   # accept DB_USERNAME as an alias
        cfg["DB_USER"] = cfg["DB_USERNAME"]
    return cfg


def db_url(cfg: dict | None = None, hide_password: bool = False) -> str:
    c = cfg or load_env()
    missing = [k for k in ("DB_HOST", "DB_USER", "DB_NAME") if not c.get(k)]
    if missing:
        raise RuntimeError(
            f"Database credentials incomplete: missing {missing}.\n"
            f"   Put them in {ENV_FILE} (DB_USER/DB_PASSWORD/DB_HOST/DB_PORT/DB_NAME) "
            f"or export them as environment variables.")
    pw = "***" if hide_password else c["DB_PASSWORD"]
    return (f"mysql+pymysql://{c['DB_USER']}:{pw}@{c['DB_HOST']}:{c.get('DB_PORT', 3306)}"
            f"/{c['DB_NAME']}?charset=utf8mb4")


_ENGINE = None


def get_engine(cfg: dict | None = None, force_new: bool = False):
    """Cached SQLAlchemy engine. pool_pre_ping so a stale connection is retried, not raised."""
    global _ENGINE
    if _ENGINE is not None and not force_new:
        return _ENGINE
    try:
        from sqlalchemy import create_engine
    except ImportError as e:
        raise RuntimeError("sqlalchemy is required for DB mode:  pip install sqlalchemy pymysql") from e
    try:
        import pymysql  # noqa: F401
    except ImportError as e:
        raise RuntimeError("pymysql is required for DB mode:  pip install pymysql") from e

    _ENGINE = create_engine(db_url(cfg), pool_pre_ping=True, pool_recycle=3600,
                            connect_args={"connect_timeout": 30})
    return _ENGINE


def table_exists(name: str, cfg: dict | None = None) -> bool:
    from sqlalchemy import text
    c = cfg or load_env()
    with get_engine(c).connect() as con:
        q = text("SELECT COUNT(*) FROM information_schema.TABLES "
                 "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t")
        return bool(con.execute(q, {"s": c["DB_NAME"], "t": name}).scalar())


def read_table(name: str, cfg: dict | None = None, where: str | None = None,
               limit: int | None = None) -> pd.DataFrame:
    """SELECT * FROM `name`. Backticked, so table names with dots/case survive.

    OFFLINE fallback: the live DB (35.208.174.2) is intermittent. If DB_OFFLINE=1, or if the
    live read fails, use the pre-downloaded raw CSV in db_pipeline/raw/<name>.csv. This keeps the
    adapter reading the REAL table (incl. the TBR tables) instead of silently falling back to the
    PCR workbook."""
    import os as _os, pathlib as _pl
    _raw = _pl.Path(__file__).resolve().parent / "raw" / f"{name}.csv"
    if _os.environ.get("DB_OFFLINE", "").strip().lower() in ("1", "true", "yes") and _raw.exists():
        return pd.read_csv(_raw, low_memory=False)
    from sqlalchemy import text
    sql = f"SELECT * FROM `{name}`"
    if where:
        sql += f" WHERE {where}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    try:
        with get_engine(cfg).connect() as con:
            return pd.read_sql(text(sql), con)
    except Exception:
        if _raw.exists():
            print(f"  [db_connect] live DB unreachable -> using downloaded raw/{name}.csv")
            return pd.read_csv(_raw, low_memory=False)
        raise


def list_tables(cfg: dict | None = None) -> pd.DataFrame:
    from sqlalchemy import text
    c = cfg or load_env()
    with get_engine(c).connect() as con:
        return pd.read_sql(text(
            "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=:s ORDER BY TABLE_NAME"), con, params={"s": c["DB_NAME"]})


def columns_of(name: str, cfg: dict | None = None) -> pd.DataFrame:
    from sqlalchemy import text
    c = cfg or load_env()
    with get_engine(c).connect() as con:
        return pd.read_sql(text(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=:s AND TABLE_NAME=:t ORDER BY ORDINAL_POSITION"),
            con, params={"s": c["DB_NAME"], "t": name})


if __name__ == "__main__":
    c = load_env()
    print("URL:", db_url(c, hide_password=True))
    t = list_tables(c)
    print(f"CONNECTED — {len(t)} tables in {c['DB_NAME']}")
