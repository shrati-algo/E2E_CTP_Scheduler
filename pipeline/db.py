"""Database login and a read-only connection to JK_CTP_PPDS.

Credentials come ONLY from environment variables, or from a local `db.env` in
the repository root (KEY=VALUE lines, git-ignored). A value already set in the
environment always wins over db.env.
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
DB_KEYS = ("CTP_DB_HOST", "CTP_DB_PORT", "CTP_DB_USER", "CTP_DB_PASSWORD",
           "CTP_DB_NAME", "CTP_PPDS_NAME")


def load_db_env() -> None:
    f = ROOT / "db.env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in DB_KEYS and v and not os.environ.get(k):
                os.environ[k] = v
    os.environ.setdefault("CTP_PPDS_NAME", "JK_CTP_PPDS")
    os.environ.setdefault("CTP_DB_PORT", "3306")


def missing_credentials() -> list:
    return [k for k in ("CTP_DB_HOST", "CTP_DB_USER", "CTP_DB_PASSWORD")
            if not os.environ.get(k)]


def connect():
    """A connection to CTP_PPDS_NAME. Callers only SELECT (the publish step is
    the single exception and is off unless explicitly asked for)."""
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    import ctpsql
    return ctpsql.connect(os.environ["CTP_PPDS_NAME"])


def fetch(sql: str, cn):
    import ctpsql
    return ctpsql.fetch(sql, cn)
