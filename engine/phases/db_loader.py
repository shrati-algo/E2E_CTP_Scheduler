#!/usr/bin/env python3
"""
db_loader.py — CTP input loader.  FILE-BASED ONLY.  NO DATABASE.

═══════════════════════════════════════════════════════════════════════════════════════
  THIS IS NOT v6's db_loader. IT IS A CTP REPLACEMENT. TWO THINGS CHANGED, BOTH ON PURPOSE.
═══════════════════════════════════════════════════════════════════════════════════════

It keeps the NAME `db_loader` only because six BYTE-IDENTICAL v6 phases import it by that
name (`from db_loader import load_input`). Renaming it would mean editing those phases.

────────────────────────────────────────────────────────────────────────────────────────
CHANGE 1 — NO DATABASE. Everything comes from the local `inputs/` folder.
────────────────────────────────────────────────────────────────────────────────────────
    USER INSTRUCTION: "we are using all the files from the local folder only".

v6's version could read from MySQL (`data_source: db`, sqlalchemy, `write_to_db`) and it
carried a HARD-CODED PRODUCTION DB URL AND PASSWORD in plain text. CTP has no DB and never
will. All of it is gone — no sqlalchemy, no engine, no credentials, no DB writes.
`write_to_db` / `get_engine` survive only as explicit NO-OPS so the phases' optional
`from db_loader import write_to_db` blocks still resolve. They never do anything.

────────────────────────────────────────────────────────────────────────────────────────
CHANGE 2 — 🔴 NO METRE→MILLIMETRE CONVERSION.  **CTP WORKS IN METRES.**
────────────────────────────────────────────────────────────────────────────────────────
    USER INSTRUCTION: "in ctp use m and m/min only".

This is the important one. v6's `load_wip_by_item` hard-coded BTP's unit convention:

        _LEN_TO_MM = {"M": 1000.0, "MTR": 1000.0, "CM": 10.0, "MM": 1.0}
        qty = qty * factor        # "the BOM/demand explosion denominates them in MM"

TRUE FOR BTP. FALSE FOR CTP:

        BOM length unit       BTP/v6: MM (4,538 rows)     CTP: M (2,301 rows)
        routing length rate   BTP/v6: MM/MIN (4,854)      CTP: M/MIN (2,279)

CTP's demand leaves phase1b in **METRES**. Multiplying CTP's METRE-denominated inventory by
1000 to "match MM demand" inflated the WIP 1000x. MEASURED, before this fix:

        BCFT002 : the file says      1,621 M of stock
                  phase1c was handed 1,621,353            (x1000)
                  demand was            21,800 M

        items phase1c believed were FULLY COVERED by WIP : 51 of 51
        items ACTUALLY fully covered                     :  1 of 51

**The plant had 1,621 metres. The scheduler believed it had 1,621 KILOMETRES**, netted the
demand away, and never produced it. No error. No warning. That is the whole bug.

CTP is internally consistent in METRES and needs NO conversion anywhere:

        demand     (BOM)      M          phase1b emits M
        rate       (routing)  M/MIN      op_duration_min: qty(M) / rate(M/MIN) = minutes  OK
        inventory  (WIP)      M          netted against M demand, 1:1                     OK
        MPQ floor             M          compared against M lot quantities                OK

So this loader passes length quantities through UNCHANGED. The only scaling it will ever
apply is a genuine mismatch INSIDE the length family (a row stated in MM or CM when the
demand is in M) — converted TO METRES, and announced loudly, never silently.
"""
from __future__ import annotations

import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"

# logical key -> (default filename, rename map to the in-code column names v6 expects)
TABLES = {
    "bom": ("bom.csv", {
        "Super_parent":   "Super parent",
        "Parent_qty":     "Parent qty",
        "Parent_unit":    "Parent unit",
        "child_quantity": "child quantity",
        "child_Unit":     "child Unit",
    }),
    "routing":              ("routing.csv", {}),
    "plan":                 ("plan.csv", {}),
    "itemtype_master":      ("itemtype_master.csv", {}),
    "aging_master":         ("aging_master.csv", {}),
    "mpq":                  ("mpq.csv", {}),
    "buffer_master":        ("buffer_master.csv", {}),
    "mg_preference":        ("mg_preference.csv", {}),
    "belt_wire_mapping":    ("belt_wire_mapping.csv", {}),
    "building_cycle_times": ("building_cycle_times.csv", {}),
    "planning_max_aging":   ("planning_max_aging.csv", {}),
    "inventory":            ("inventory.csv", {}),
}


# ── DB MODE (restored 2026-07-23) ────────────────────────────────────────────────
# CTP now HAS a database: jkplanning_CTP (MySQL 9.3, 303 tables). config.yaml decides:
#     data_source: csv   -> read inputs/*.csv          (unchanged, still the default)
#     data_source: db    -> read jkplanning_CTP LIVE   (via db_pipeline/)
# The DB path returns the SAME schema the CSV path returns: db_pipeline.adapters maps the
# two tables whose columns differ (mpq, plan) and everything else passes through, so no
# phase can tell which source it got. Credentials live ONLY in db_pipeline/.env.
_DBP = ROOT / "db_pipeline"


def _db_bits():
    """Import the db_pipeline helpers lazily, so csv mode never needs sqlalchemy."""
    import sys
    if str(_DBP) not in sys.path:
        sys.path.insert(0, str(_DBP))
    import db_connect, adapters, table_map          # noqa: E401
    return db_connect, adapters, table_map


def _resolve_source(cfg=None):
    """'db' or 'csv', from config.yaml. Anything unrecognised falls back to csv."""
    src = str((cfg or {}).get("data_source", "csv")).strip().lower()
    return "db" if src in ("db", "mysql", "database") else "csv"


def get_engine(cfg=None):
    """Real SQLAlchemy engine against jkplanning_CTP (credentials from db_pipeline/.env)."""
    db_connect, _, _ = _db_bits()
    return db_connect.get_engine()


def _read_csv(path: pathlib.Path) -> pd.DataFrame:
    path = pathlib.Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path)
    if not path.exists():
        alt = path.with_suffix(".xlsx")
        if alt.exists():
            return pd.read_excel(alt)
    for enc in ("utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, engine="python",
                               on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError:
            continue
    raise IOError(path)


def load_input(config_key, cfg=None, csv_path_override=None):
    """Load one input table from `inputs/`. Returns a DataFrame. Never touches a DB."""
    if config_key not in TABLES:
        raise KeyError(f"Unknown input config_key '{config_key}'. Known: {list(TABLES)}")
    default_csv, rename_map = TABLES[config_key]

    # ── DB PATH ──────────────────────────────────────────────────────────────────
    # Only when data_source: db AND no explicit file override was passed. Falls back to
    # the CSV path (loudly) rather than dying, so a network blip cannot silently halt a run.
    # OPT-IN KEYS: `plan` is NOT pulled from the DB just because data_source is db.
    # THE BUG THIS FIXES: adapt_inputs honoured `db_plan: false` and wrote the JUNE shift
    # sheet to inputs/plan.csv -- but the phases call _db_or_csv("plan") and read
    # CuringSchedule_pcr (APRIL) straight from the DB, silently overriding it. The schedule
    # then started 2026-04-01 and the makespan read 95.33 d instead of 34.78 d. Both layers
    # must agree on which keys are DB-sourced.
    _OPT_IN = {"plan": "db_plan"}
    _gate = _OPT_IN.get(config_key)
    _allowed = (_gate is None) or bool((cfg or {}).get(_gate, False))

    # ══ THE PHASES DO **NOT** READ THE DATABASE DIRECTLY. ═══════════════════════════
    # `data_source: db` tells adapt_inputs WHERE TO SOURCE THE MASTERS. adapt_inputs then
    # writes inputs/*.csv and the phases read those, exactly as in file mode.
    #
    # WHY (measured 2026-07-23, this cost a 3x wrong schedule):
    #   adapt_inputs.routing() applies PLANT-CONFIRMED OVERRIDES. For CAP PLY SLITTER the
    #   raw table says proc_time 60 M/MIN @ eff 0.96; the override makes it 396 @ 0.78.
    #   Reading the raw table here skipped that override -> effective 57.6 M/min instead of
    #   309 -> the slitter needed 4,773 h instead of 890 h on 2 machines -> MAKESPAN 106.00 d
    #   instead of 34.78 d. Same failure as the itemtype gap-fill (raw table has ZERO
    #   'Green Tyres'; adapt_inputs types 221 of them from the routing).
    #   adapt_inputs is NOT a column renamer. Bypassing it silently produces a wrong plan.
    #
    # Set `phases_read_db: true` ONLY for a raw-table read that needs no adaptation.
    if not bool((cfg or {}).get("phases_read_db", False)):
        _allowed = False

    if csv_path_override is None and _allowed and _resolve_source(cfg) == "db":
        try:
            db_connect, adapters, table_map = _db_bits()
            _prod = __import__("os").environ.get("CTP_PRODUCT", "pcr").strip().lower()
            table = table_map.table_for(config_key, _prod)
            raw = db_connect.read_table(table)
            kw = {}
            if config_key == "plan":
                try:
                    kw["cycle_df"] = db_connect.read_table(
                        "Master_Curing_Design_CycleTime_tbr" if _prod == "tbr"
                        else "Master_Curing_Design_CycleTime_pcr")
                except Exception:
                    pass
            df = adapters.adapt(config_key, raw, **kw)
            if rename_map:
                df = df.rename(columns=rename_map)
            return df
        except Exception as e:
            print(f"  [db_loader] DB read failed for {config_key!r} ({type(e).__name__}: "
                  f"{str(e)[:120]}) — FALLING BACK to inputs/. The run continues on FILE "
                  f"data; do not report it as a database run.")

    if csv_path_override is not None:
        path = pathlib.Path(csv_path_override)
    else:
        fname = default_csv
        if cfg and "files" in cfg and config_key in cfg["files"]:
            fname = cfg["files"][config_key]
        path = INPUTS / fname

    df = _read_csv(path)
    if rename_map:
        df = df.rename(columns=rename_map)
    if config_key == "building_cycle_times" and "Machine" in df.columns:
        df["Machine"] = df["Machine"].astype(str).str.strip()
    return df


def load_bom(cfg=None):                  return load_input("bom", cfg)
def load_routing(cfg=None):              return load_input("routing", cfg)
def load_plan(cfg=None):                 return load_input("plan", cfg)
def load_itemtype(cfg=None):             return load_input("itemtype_master", cfg)
def load_aging(cfg=None):                return load_input("aging_master", cfg)
def load_mpq(cfg=None):                  return load_input("mpq", cfg)
def load_buffer(cfg=None):               return load_input("buffer_master", cfg)
def load_mg_preference(cfg=None):        return load_input("mg_preference", cfg)
def load_belt_wire(cfg=None):            return load_input("belt_wire_mapping", cfg)
def load_building_cycle_times(cfg=None): return load_input("building_cycle_times", cfg)
def load_planning_max_aging(cfg=None):   return load_input("planning_max_aging", cfg)
def load_inventory(cfg=None):            return load_input("inventory", cfg)


# ═══════════════════════════════════════════════════════════════════════════════════════
#  THE ONE THAT MATTERS — see CHANGE 2 in the module docstring.
# ═══════════════════════════════════════════════════════════════════════════════════════
_DEMAND_LENGTH_UNIT = "M"                                   # CTP is denominated in METRES
_LENGTH_UNITS = {"M", "MTR", "METER", "METRE", "MM", "CM"}
# Converts TO metres (the demand's unit). Never away from it.
_TO_METRES = {"M": 1.0, "MTR": 1.0, "METER": 1.0, "METRE": 1.0, "MM": 0.001, "CM": 0.01}


def load_wip_by_item(cfg=None, verbose=True, alive_at=None, max_age_h=None):
    """itemcode -> total available WIP, IN THE SAME UNIT THE DEMAND USES.

    *** NO 1000x CONVERSION. *** CTP works in metres end to end. A length row already in
    metres is passed straight through, unscaled.

    The only scaling applied is a genuine mismatch WITHIN the length family — a row stated
    in MM or CM when the demand is in M. That is converted TO METRES and REPORTED, so it can
    never happen silently. KG and NOS rows are passed through untouched.
    """
    df = load_inventory(cfg)
    if df is None or len(df) == 0:
        return {}
    df = df.copy()

    item_col = next((c for c in ("itemcode", "ItemCode", "item_code", "Item_Code")
                     if c in df.columns), None)
    qty_col = next((c for c in ("inventory", "availableinventory", "available_inventory",
                                "Inventory", "qty", "Qty", "quantity")
                    if c in df.columns), None)
    if item_col is None or qty_col is None:
        return {}

    df[item_col] = df[item_col].astype(str).str.strip()
    df[qty_col] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0)

    # EXPIRED-WIP FIX (2026-09-26, opt-in: phase1c passes alive_at + max_age_h when
    # config `phase1c_exclude_expired_wip` is on). A row already past its shelf life at plan
    # start is SCRAP: phase5 (F3) and phase6 (F5) refuse to use it, so phase1c must not net
    # demand against it either -- otherwise building is told to make less than it can use.
    if alive_at is not None and max_age_h and "produced_time" in df.columns:
        _pt = pd.to_datetime(df["produced_time"], errors="coerce", format="mixed")
        _age = (pd.Timestamp(alive_at) - _pt).dt.total_seconds() / 3600.0
        _lim = df[item_col].map(max_age_h)
        _dead = _pt.notna() & _lim.notna() & (_age > _lim)
        if verbose:
            print(f"  [WIP] expired at plan start (NOT netted): {int(_dead.sum()):,} rows / "
                  f"{df.loc[_dead, item_col].nunique():,} items")
        df = df[~_dead].copy()

    unit_col = next((c for c in ("unit", "uom", "UOM", "Unit") if c in df.columns), None)
    if unit_col is None:
        return df.groupby(item_col)[qty_col].sum().to_dict()

    u = df[unit_col].astype(str).str.upper().str.strip()

    wrong = u.isin(_LENGTH_UNITS) & (u != _DEMAND_LENGTH_UNIT)
    n_wrong = int(wrong.sum())
    factor = pd.Series(1.0, index=df.index)
    if n_wrong:
        factor.loc[wrong] = u[wrong].map(_TO_METRES).fillna(1.0)
        if verbose:
            print(f"  [WIP] {n_wrong:,} length rows are not in metres "
                  f"({dict(u[wrong].value_counts())}) -> converted TO METRES to match the "
                  f"demand.")

    df["_q"] = df[qty_col] * factor
    wip = df.groupby(item_col)["_q"].sum().to_dict()

    if verbose:
        n_len = int(u.isin(_LENGTH_UNITS).sum())
        print(f"  [WIP] {len(wip):,} items from inputs/{TABLES['inventory'][0]}; "
              f"{n_len:,} length rows kept IN METRES — no x1000 conversion "
              f"(v6 applied BTP's MM convention here and made 1,621 m look like 1,621 km).")
    return wip


def load_plan_horizon(cfg=None):
    plan = load_plan(cfg)
    if plan is None or len(plan) == 0:
        return (None, None)
    st = pd.to_datetime(plan.get("startTime"), errors="coerce")
    en = pd.to_datetime(plan.get("endTime"), errors="coerce")
    return (st.min() if st is not None else None,
            en.max() if en is not None else None)


# ── DB write path: REMOVED. These exist only so the phases' optional imports resolve. ──
OUTPUT_TABLES: dict[str, str] = {}


def write_to_db(df, output_key=None, table_name=None, cfg=None,
                truncate=True, chunksize=5000):
    """NO-OP. ctp_v6 has no database. Every output is written to `outputs/` and `outputs2/`
    as CSV/XLSX by the phases themselves."""
    return 0
