#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_sep_inputs.py — build the SEPTEMBER 2026 combined PCR+TBR inputs (2026-09-26).

  PLANS  : discard/ctp_{curing,building}_schedule_September_2026_{pcr 5,tbr 4}.xlsx
           -> ctp_inputfiles/curing_sep_comb.xlsx, building_sep_comb.xlsx
  LIVE DB: jkplanning_CTP.opening_wip           -> ctp_inputfiles/opening_wip_live.csv
           JK_CTP_PPDS.CTP_Master_Aging          -> ctp_inputfiles/aging_sep_live.csv
                                                    (aging_comb.csv only for items it lacks)
           gt_inventory_manual / current_running_moulds are cross-checks only (report).
"""
import os, re, sys
import pandas as pd
import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
SRC, OUT = os.path.join(HERE, "discard"), os.path.join(HERE, "ctp_inputfiles")
sys.path.insert(0, os.path.join(HERE, "db_pipeline"))
from db_connect import load_env   # noqa: E402

# Plan folder: argv[1] or env CTP_PLAN_DIR (default discard/). The 4 files are auto-detected by
# name: ctp_{curing,building}_schedule_*_{pcr,tbr}*.xlsx (exactly one of each required).
import glob as _glob
SRC = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CTP_PLAN_DIR", SRC))


def _one(kind, prod):
    hits = sorted(_glob.glob(os.path.join(SRC, f"ctp_{kind}_schedule_*_{prod}*.xlsx")))
    if len(hits) != 1:
        raise SystemExit(f"need exactly one {kind}/{prod} plan in {SRC}, found {hits}")
    return os.path.basename(hits[0])


CUR = [_one("curing", "pcr"), _one("curing", "tbr")]
BLD = [_one("building", "pcr"), _one("building", "tbr")]
print(f"  plan folder: {SRC} | curing: {CUR} | building: {BLD}")


def _sheet(path):
    probe = pd.read_excel(path, sheet_name="Shift Schedule", header=None, nrows=10)
    hdr = next(i for i in range(len(probe))
               if {"Machine", "SKUCode", "StartTime"} <= {str(v).strip() for v in probe.iloc[i]})
    d = pd.read_excel(path, sheet_name="Shift Schedule", header=hdr)
    d.columns = [str(c).strip() for c in d.columns]
    return d


def plans():
    cur = pd.concat([_sheet(os.path.join(SRC, f)) for f in CUR], ignore_index=True)
    b = pd.concat([_sheet(os.path.join(SRC, f)) for f in BLD], ignore_index=True)
    # PLAN WINDOW (v15, 2026-10-04): env CTP_PLAN_START / CTP_PLAN_END ("YYYY-MM-DD HH:MM") keep only
    # plan rows with start_time in [start, end). Unset = whole plan (v14 behaviour). The plan start is
    # also the opening-WIP time: live() picks the WIP snapshot of that day / at or before that time.
    _ps, _pe = os.environ.get("CTP_PLAN_START"), os.environ.get("CTP_PLAN_END")
    if _ps or _pe:
        _lo = pd.Timestamp(_ps) if _ps else pd.Timestamp.min
        _hi = pd.Timestamp(_pe) if _pe else pd.Timestamp.max
        for _n, _d in (("curing", cur), ("building", b)):
            _s = pd.to_datetime(_d["StartTime"], errors="coerce")
            _k = (_s >= _lo) & (_s < _hi)
            print(f"  PLAN WINDOW {_ps or '-'} .. {_pe or '-'}: {_n} {int(_k.sum()):,} of {len(_d):,} rows kept "
                  f"(qty {pd.to_numeric(_d.loc[_k, 'Qty'], errors='coerce').sum():,.0f})")
        cur = cur[(pd.to_datetime(cur["StartTime"]) >= _lo) & (pd.to_datetime(cur["StartTime"]) < _hi)]
        b = b[(pd.to_datetime(b["StartTime"]) >= _lo) & (pd.to_datetime(b["StartTime"]) < _hi)]
        cur, b = cur.reset_index(drop=True), b.reset_index(drop=True)
    cur.to_excel(os.path.join(OUT, "curing_sep_comb.xlsx"), sheet_name="Shift Schedule", index=False)
    st, en = pd.to_datetime(b["StartTime"]), pd.to_datetime(b["EndTime"])
    co = pd.to_numeric(b["CO_Mins"], errors="coerce").fillna(0)
    qty = pd.to_numeric(b["Qty"], errors="coerce").fillna(0)
    win = ((en - st).dt.total_seconds() / 60 - co).clip(lower=1)
    # CYCLE-TIME FIX (2026-09-26): the plant slices a DAY plan into shifts pro-rata but keeps the
    # FULL shift window on every slice, so a 1-tyre tail slice read as 480 min/tyre (42 rows,
    # +214.6 phantom TBM hours). The true per-tyre rate of a SKU on a machine is the one its
    # FULL slices show; apply that rate to the short slices. (Plant data only -- nothing invented.)
    rate = win / qty.where(qty > 0)
    full = qty >= 0.8 * qty.groupby([b["Machine"], b["SKUCode"]]).transform("max")
    base = rate.where(full).groupby([b["Machine"], b["SKUCode"]]).transform("median")
    base = base.fillna(rate.groupby(b["SKUCode"]).transform("min"))
    fix = rate > 1.5 * base
    mins = win.where(~fix, qty * base)
    print(f"  building cycle-time fix: {int(fix.sum())} short slices re-rated "
          f"({float((win - mins).sum() / 60):,.1f} phantom TBM hours removed)")
    # LIVE DB CYCLE TIME (2026-09-29): the Shift Schedule is a DAY plan sliced pro-rata (plant Notes
    # sheet), so window/qty is NOT a cycle time -- 3-4 SKUs share one 8 h shift window. Use the
    # plant's building cycle master: JK_CTP_PPDS.CTP_Master_BuildingAllowable (current version, per
    # SKU; median over allowed machines) -> jkplanning_CTP.jkt_building_cycle_times_pcr/tbr ->
    # JK_CTP_PPDS.CTP_Master_Routing GT BUILDING proc_time. Only if none has the SKU is the old
    # window-derived rate kept.
    sku = b["SKUCode"].astype(str).str.strip()
    src = pd.Series("window", index=b.index)
    sec = pd.Series(float("nan"), index=b.index)
    try:
        ba = _table("JK_CTP_PPDS", "CTP_Master_BuildingAllowable")
        ba = ba[(pd.to_numeric(ba["is_current"], errors="coerce") == 1)
                & (pd.to_numeric(ba["is_allowed"], errors="coerce") == 1)]
        m1 = pd.to_numeric(ba["cycle_time_sec"], errors="coerce").groupby(ba["sku_code"].astype(str).str.strip()).median()
        cp = _table("jkplanning_CTP", "jkt_building_cycle_times_pcr")
        ctt = _table("jkplanning_CTP", "jkt_building_cycle_times_tbr")
        m2 = pd.concat([pd.Series(pd.to_numeric(cp["conti_sec"], errors="coerce").values,
                                  index=cp["SKU code"].astype(str).str.strip()),
                        pd.Series(pd.to_numeric(ctt["cycle_sec"], errors="coerce").values,
                                  index=ctt["SKU code"].astype(str).str.strip())])
        m2 = m2[~m2.index.duplicated()]
        rt = _table("JK_CTP_PPDS", "CTP_Master_Routing")
        rt = rt[rt["operation_name"].astype(str).str.upper().str.strip() == "GT BUILDING"]
        m3 = pd.to_numeric(rt["proc_time"], errors="coerce").groupby(rt["finished_product"].astype(str).str.strip()).median()
        for lab, m in (("routing", m3), ("jkt_building_cycle_times", m2), ("CTP_Master_BuildingAllowable", m1)):
            v = sku.map(m)
            ok = v.notna() & (v > 0)
            sec = sec.where(~ok, v); src = src.where(~ok, lab)
        have = sec.notna()
        # + the plant's own planned changeover minutes of that row (CO_Mins, 68 h in the 29-09 plan):
        # the DB cycle is pure build time, so without this building would be ~3% optimistic
        mins = mins.where(~have, qty * sec / 60.0 + co)
        print(f"  building cycle time from LIVE DB: {src.value_counts().to_dict()} | TBM hours "
              f"{float(win.sum() / 60):,.0f} (window) -> {float(mins.sum() / 60):,.0f} (DB cycle)")
    except Exception as e:
        print(f"  !! building cycle time: DB masters unavailable ({type(e).__name__}) -- window rate kept")
    bo = pd.DataFrame({
        "Machine": b["Machine"], "Day": st.dt.day, "Shift": b["Shift"], "SKU": b["SKUCode"],
        "Qty": b["Qty"], "Mins": mins.round(2),
        "Phase": b.get("CO_Type", ""), "StartTime": st.dt.strftime("%Y-%m-%d %H:%M"),
        "EndTime": en.dt.strftime("%Y-%m-%d %H:%M"), "GT_Code": b["GT_Code"],
        "SKU_Description": b["Description"]})
    bo.to_excel(os.path.join(OUT, "building_sep_comb.xlsx"), sheet_name="Shift Schedule (per-shift)",
                index=False)
    print(f"  curing   {len(cur):,} rows  qty {pd.to_numeric(cur.Qty).sum():,.0f}  "
          f"{cur.StartTime.min()} .. {cur.EndTime.max()}")
    print(f"  building {len(bo):,} rows  qty {pd.to_numeric(bo.Qty).sum():,.0f}  "
          f"{bo.StartTime.min()} .. {bo.EndTime.max()}")


RAW = os.path.join(HERE, "db_pipeline", "raw")


def _table(db, table, tries=4):
    """Read one live table (retry -- the 35.208.174.2 link is intermittent). Every successful
    read refreshes db_pipeline/raw/<db>__<table>.csv; if the server stays unreachable the
    last snapshot is used and SAID so (same convention as db_connect.read_table)."""
    import time
    # v56 (user 2026-10-09): a master may be read from a FILE instead of the DB (config master_files: {table: path})
    try:
        import yaml
        _mf = (yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {}).get("master_files") or {}
    except Exception:
        _mf = {}
    if table in _mf and _mf[table]:
        _p = _mf[table] if os.path.isabs(_mf[table]) else os.path.join(HERE, _mf[table])
        df = pd.read_excel(_p, sheet_name=table) if _p.lower().endswith(".xlsx") else pd.read_csv(_p, low_memory=False)
        print(f"  [file] {db}.{table}: {len(df):,} rows FROM FILE {os.path.relpath(_p, HERE)} (config master_files)")
        return df
    c = load_env()
    snap = os.path.join(RAW, f"{db}__{table}.csv")
    for n in range(1, tries + 1):
        try:
            con = pymysql.connect(host=c["DB_HOST"], port=int(c["DB_PORT"]), user=c["DB_USER"],
                                  password=c["DB_PASSWORD"], database=db, connect_timeout=30,
                                  read_timeout=300)
            try:
                df = pd.read_sql(f"SELECT * FROM `{table}`", con)
            finally:
                con.close()
            os.makedirs(RAW, exist_ok=True)
            df.to_csv(snap, index=False)
            print(f"  [db] {db}.{table}: {len(df):,} rows LIVE")
            return df
        except Exception as e:
            print(f"  [db] {db}.{table}: attempt {n}/{tries} failed ({type(e).__name__})")
            time.sleep(5 * n)
    if os.path.exists(snap):
        df = pd.read_csv(snap, low_memory=False)
        print(f"  [db] !! {db}.{table}: server unreachable -> using SNAPSHOT {os.path.basename(snap)} "
              f"({len(df):,} rows, saved {time.ctime(os.path.getmtime(snap))})")
        return df
    raise SystemExit(f"{db}.{table}: unreachable and no snapshot in {RAW}")


def live():
    wip = _table("jkplanning_CTP", "opening_wip")
    # ONE SNAPSHOT ONLY (2026-09-27): opening_wip can hold several inventory_date snapshots at
    # once (26-Sep AND 27-Sep were both present) -> loading all of them counts the stock twice.
    # Keep the snapshot for the PLAN-START date (or env CTP_WIP_DATE); never a later one.
    _cur = pd.read_excel(os.path.join(OUT, "curing_sep_comb.xlsx"))
    _t0 = pd.to_datetime(_cur["StartTime"], errors="coerce").min()
    _want = os.environ.get("CTP_WIP_DATE") or (_t0 - pd.Timedelta(hours=7)).strftime("%Y-%m-%d")
    _dates = sorted(wip["inventory_date"].astype(str).str[:10].unique())
    _ok = [d for d in _dates if d <= _want]
    _use = _ok[-1] if _ok else _dates[0]
    wip = wip[wip["inventory_date"].astype(str).str[:10] == _use].copy()
    print(f"  opening_wip snapshots in DB: {_dates} -> using {_use} (plan start {_t0}) : {len(wip):,} rows")
    # 2026-09-29 (plant engine's table list): stock = JK_CTP_PPDS.CTP_Opening_WIP, the 06:30 snapshot
    # of the plan-start day (jkplanning_CTP.opening_wip is the OLD feed; it misses ~500 recent lots,
    # e.g. 93 inner-liner lots). The snapshot carries NO compound rows, so compound stock alone is
    # still taken from the old feed. CTP_WIP_SOURCE=old restores the old feed.
    if os.environ.get("CTP_WIP_SOURCE", "ppds").lower() != "old":
        pp_all = _table("JK_CTP_PPDS", "CTP_Opening_WIP")
        pp = pp_all[pp_all["inventory_date"].astype(str).str[:10] == _use]
        # env CTP_WIP_CUTOFF ("YYYY-MM-DD HH:MM") = latest snapshot time allowed (default: plan start)
        cut = pd.Timestamp(os.environ.get("CTP_WIP_CUTOFF") or _t0).strftime("%Y-%m-%d %H:%M:%S")
        snaps = sorted(pp["updated_at"].astype(str).unique())
        ok = [s for s in snaps if s <= cut]
        # WIP MIX (v15, 2026-10-04): CTP_WIP_MIX=1 and no CTP_Opening_WIP snapshot on the WIP day ->
        # components from the LATEST full snapshot <= cut (an earlier day), while compounds and green
        # tyres -- the item types the old feed carries for the WIP day -- come from the old feed.
        mixed = False
        if not ok and os.environ.get("CTP_WIP_MIX", "").strip().lower() in ("1", "true", "yes"):
            older = sorted(s for s in pp_all["updated_at"].astype(str).unique() if s <= cut)
            if older:
                ok, pp, mixed = [older[-1]], pp_all, True
        if ok:
            snap = ok[-1]
            pp = pp[pp["updated_at"].astype(str) == snap]
            # v87 (user 2026-10-13): env CTP_WIP_CORRECTION ("YYYY-MM-DD HH:MM") = a later correction snapshot of the
            # same day (12-Oct 17:26). Its lots produced at or before plan start that the main snapshot lacks are
            # ADDED (111 final-compound lots / 414 batches on 12-Oct). Lots in both keep the main snapshot's qty
            # (the later qty is what was left after the day's consumption) unless CTP_WIP_CORRECTION_MODE=replace.
            _corr = os.environ.get("CTP_WIP_CORRECTION", "").strip()
            if _corr:
                cr = pp_all[pp_all["updated_at"].astype(str).str.startswith(_corr)]
                cr = cr[pd.to_datetime(cr["produced_time"], errors="coerce", format="mixed") <= pd.Timestamp(cut)]
                _rep = os.environ.get("CTP_WIP_CORRECTION_MODE", "add").lower() == "replace"
                _both = pp["production_id"].isin(set(cr["production_id"]))
                _add = cr if _rep else cr[~cr["production_id"].isin(set(pp["production_id"]))]
                pp = pd.concat([pp[~_both] if _rep else pp, _add], ignore_index=True)
                print(f"  WIP CORRECTION {_corr}: {len(cr):,} rows produced <= {cut}; "
                      f"{'replaced ' + str(int(_both.sum())) + ' lots, ' if _rep else ''}added {len(_add):,} rows "
                      f"{_add.groupby('item_type')['inventory'].apply(lambda s: round(float(pd.to_numeric(s, errors='coerce').sum()), 1)).to_dict()}")
            if mixed:
                # Only compounds + green tyres are taken from the old feed: its component rows have a
                # blank unit, other UOMs (tread not in NOS), no TBR belt/nylon/chipper types and ~21k
                # rows with no item_type -- v14 never used them either.
                _ot = wip["item_type"].astype(str).str.upper()
                wip = wip[_ot.str.contains("COMPOUND") | (_ot.str.replace(r"[\s_]+", "", regex=True) == "GREENTYRE")]
                own = set(wip["item_type"].astype(str).str.upper())
                _drop = pp["item_type"].astype(str).str.upper().isin(own)
                print(f"  WIP MIX: components from CTP_Opening_WIP {snap} ({int((~_drop).sum()):,} rows); "
                      f"{sorted(own)} from old feed {_use} ({len(wip):,} rows) -- "
                      f"{int(_drop.sum()):,} snapshot rows of those types dropped")
                pp = pp[~_drop]
            new = pd.DataFrame({
                "production_id": pp["production_id"], "produced_time": pp["produced_time"],
                "itemcode": pp["item_code"], "item_type": pp["item_type"], "unit": pp["unit"],
                "inventory": pd.to_numeric(pp["inventory"], errors="coerce"),
                "MaxAging": pp["max_aging"], "MaxAgingUnit": pp["max_aging_unit"],
                "MinAging": pp["min_aging"], "MinAgingUnit": pp["min_aging_unit"],
                "inventory_date": pp["inventory_date"], "updatedAt": pp["updated_at"]})
            covered = set(new["item_type"].astype(str).str.upper())
            comp = wip[wip["item_type"].astype(str).str.upper().str.contains("COMPOUND")
                       & ~wip["item_type"].astype(str).str.upper().isin(covered)].copy()
            # COMPOUND KG FIX (2026-09-30): a compound stock row is ONE BATCH -- its quantity field holds
            # the batch serial (ProductionID ..._41 -> 41), not kg. Stock = 1 batch x the compound's
            # routing batch weight (KG). Measured: 4,699 batches = ~1,017 t, not the 74.7 "kg" read before.
            try:
                _rt = pd.read_csv(os.path.join(OUT, "routing_live.csv"), dtype=str)
                _mx = _rt[_rt["operation_name"].astype(str).str.upper().str.contains("MIXING")]
                _bw = pd.to_numeric(_mx["batch_size"], errors="coerce").groupby(
                    _mx["routed_product"].astype(str).str.strip()).median()
                _k = comp["itemcode"].astype(str).str.strip()
                _w = _k.map(_bw)
                _old = float(pd.to_numeric(comp["inventory"], errors="coerce").sum())
                comp["inventory"] = _w.where(_w.notna(), pd.to_numeric(comp["inventory"], errors="coerce"))
                comp["unit"] = "KG"
                print(f"  COMPOUND STOCK: {len(comp):,} batch rows -> {float(comp['inventory'].sum()):,.0f} kg "
                      f"(batches x routing batch weight; was {_old:,.0f} read from the batch-serial field); "
                      f"{int(_w.isna().sum())} rows had no batch weight and kept their value")
            except Exception as _e:
                print(f"  !! COMPOUND STOCK kg conversion failed ({_e}) -- raw values kept")
            # WIP MIX: the old feed's other WIP-day rows (green tyres) join as they are (1 tyre per row)
            rest = (wip[~wip["item_type"].astype(str).str.upper().str.contains("COMPOUND")
                        & ~wip["item_type"].astype(str).str.upper().isin(covered)]
                    if mixed else wip.iloc[0:0])
            wip = pd.concat([new, comp.reindex(columns=new.columns), rest.reindex(columns=new.columns)],
                            ignore_index=True)
            if mixed:
                print(f"  WIP MIX: + {len(rest):,} {sorted(set(rest['item_type'].astype(str)))} rows "
                      f"({float(pd.to_numeric(rest['inventory'], errors='coerce').sum()):,.0f}) from old feed {_use}")
            # BATCHES -> KG (2026-09-30): the 06:30 snapshot now carries compounds itself, with
            # unit "Batches" and inventory = 1 per batch. The scheduler works in KG for compounds, so
            # convert every "Batches" row: batches x the compound's routing batch weight.
            try:
                _rt2 = pd.read_csv(os.path.join(OUT, "routing_live.csv"), dtype=str)
                _mx2 = _rt2[_rt2["operation_name"].astype(str).str.upper().str.contains("MIXING")]
                _bw2 = pd.to_numeric(_mx2["batch_size"], errors="coerce").groupby(
                    _mx2["routed_product"].astype(str).str.strip()).median()
                _isb = wip["unit"].astype(str).str.strip().str.upper().isin(["BATCHES", "BATCH"])
                if _isb.any():
                    _w2 = wip.loc[_isb, "itemcode"].astype(str).str.strip().map(_bw2)
                    _q2 = pd.to_numeric(wip.loc[_isb, "inventory"], errors="coerce").fillna(0)
                    wip.loc[_isb, "inventory"] = (_q2 * _w2).where(_w2.notna(), _q2)
                    wip.loc[_isb & wip["itemcode"].astype(str).str.strip().isin(_bw2.index), "unit"] = "KG"
                    print(f"  BATCHES -> KG: {int(_isb.sum()):,} compound rows ({float(_q2.sum()):,.0f} batches) "
                          f"-> {float(wip.loc[_isb, 'inventory'].sum()):,.0f} kg; {int(_w2.isna().sum())} without a batch weight")
            except Exception as _e:
                print(f"  !! Batches -> KG conversion failed ({_e})")
            print(f"  WIP SOURCE: JK_CTP_PPDS.CTP_Opening_WIP snapshot {snap} -> {len(new):,} rows "
                  f"+ {len(comp):,} compound rows from the old feed (not in the snapshot)")
        else:
            print(f"  !! CTP_Opening_WIP has no snapshot <= {cut} for {_use} -- old feed kept")
    else:
        # OLD FEED ONLY (v16 update, user 2026-10-04): every item from jkplanning_CTP.opening_wip of the
        # WIP day, cleaned so the scheduler can use it:
        #  - rows with no item_type are dropped (their item codes are corrupted, e.g. '\x14\x14720');
        #  - compound rows are ONE batch each -> 1 x the compound's routing batch weight, in KG;
        #  - blank component units are filled with the item's BOM unit (what demand is in; tread = M).
        _t = wip["item_type"].astype(str).str.strip()
        _bad = wip["item_type"].isna() | _t.eq("") | _t.str.upper().isin(["NAN", "NONE"])
        print(f"  WIP OLD FEED {_use}: {len(wip):,} rows; {int(_bad.sum()):,} with no item_type DROPPED "
              f"(corrupted item codes)")
        wip = wip[~_bad].copy()
        # v17 (2026-10-05): the old feed holds the 07:00 stock. A WIP time before 07:00 (CTP_WIP_CUTOFF,
        # e.g. 06:30) drops the lots produced after it, i.e. the stock as it stood at that time.
        if os.environ.get("CTP_WIP_CUTOFF"):
            _co = pd.Timestamp(os.environ["CTP_WIP_CUTOFF"])
            _late = pd.to_datetime(wip["produced_time"], errors="coerce", format="mixed") > _co
            print(f"  WIP OLD FEED: {int(_late.sum()):,} rows produced after the WIP time {_co} DROPPED "
                  f"({wip.loc[_late, 'item_type'].astype(str).value_counts().to_dict()})")
            wip = wip[~_late].copy()
        _ic = wip["itemcode"].astype(str).str.strip()
        _isc = wip["item_type"].astype(str).str.upper().str.contains("COMPOUND")
        _rt = pd.read_csv(os.path.join(OUT, "routing_live.csv"), dtype=str)
        _mx = _rt[_rt["operation_name"].astype(str).str.upper().str.contains("MIXING")]
        _bw = pd.to_numeric(_mx["batch_size"], errors="coerce").groupby(
            _mx["routed_product"].astype(str).str.strip()).median()
        _w = _ic[_isc].map(_bw)
        wip.loc[_isc, "inventory"] = _w.where(_w.notna(), 1.0)
        wip.loc[_isc, "unit"] = "KG"
        print(f"  WIP OLD FEED: {int(_isc.sum()):,} compound batch rows -> {float(wip.loc[_isc, 'inventory'].sum()):,.0f} kg "
              f"(1 batch x routing batch weight); {int(_w.isna().sum())} without a batch weight kept as 1")
        _bm = pd.read_csv(os.path.join(OUT, "bom_live.csv"), dtype=str, usecols=["Parent", "Parent_unit"])
        _bu = _bm.dropna().assign(Parent=lambda d: d["Parent"].str.strip()).groupby("Parent")["Parent_unit"].agg(
            lambda s: s.str.strip().str.upper().mode().iat[0])
        _blank = ~_isc & (wip["unit"].isna() | wip["unit"].astype(str).str.strip().isin(["", "nan", "None"]))
        _fill = _ic[_blank].map(_bu)
        wip.loc[_blank, "unit"] = _fill
        print(f"  WIP OLD FEED: {int(_blank.sum()):,} component rows had no unit -> BOM unit "
              f"{_fill.value_counts().to_dict()}; {int(_fill.isna().sum())} items not in the BOM left blank")
    gtm = pd.concat([_table("jkplanning_CTP", "gt_inventory_manual_pcr")[["ItemCode", "SKUCode"]],
                     _table("jkplanning_CTP", "gt_inventory_manual_tbr")[["ItemCode", "SKUCode"]]])
    # GT-CODE FIX (2026-09-26): TBR green tyres sit in opening_wip under their DESCRIPTION
    # ("11.00R20JDE++", "GT 5075 - 295/90R20 JUH XF"), which matches no BOM code, so 1,108
    # tyres of real stock were invisible. The plant's own gt_inventory_manual table maps
    # ItemCode -> SKUCode (= the BOM GT code). Use it ONLY where it gives exactly one code
    # that IS a BOM green tyre; keep the original in itemcode_raw.
    norm = lambda x: re.sub(r"\s+", " ", str(x)).strip()
    bom = pd.read_csv(os.path.join(OUT, "bom_live.csv"), dtype=str,
                      usecols=["child", "child_description"])
    bom_gt = {norm(c) for c in bom.loc[bom["child_description"].isin(["Green Tyres", "GREEN_TYRE"]),
                                       "child"]}
    gtm = gtm.dropna()
    cand = gtm.assign(k=gtm["ItemCode"].map(norm), v=gtm["SKUCode"].map(norm))
    cand = cand[cand["v"].isin(bom_gt)].groupby("k")["v"].agg(lambda x: set(x))
    one = {k: next(iter(v)) for k, v in cand.items() if len(v) == 1}
    is_gt = (wip["item_type"].astype(str).str.upper().str.replace(r"[\s_]+", "", regex=True)
             .isin(["GREENTYRE", "GREENTYRES"]))
    code_n = wip["itemcode"].map(norm)
    remap = is_gt & ~code_n.isin(bom_gt) & code_n.isin(one.keys())
    wip["itemcode_raw"] = wip["itemcode"]
    wip.loc[remap, "itemcode"] = code_n[remap].map(one)
    left = is_gt & ~wip["itemcode"].map(norm).isin(bom_gt)
    print(f"  GT-code fix: {int(remap.sum()):,} green-tyre rows "
          f"({float(wip.loc[remap, 'inventory'].sum()):,.0f} tyres) re-keyed to their BOM GT code "
          f"via gt_inventory_manual; still unmatched: {float(wip.loc[left, 'inventory'].sum()):,.0f} tyres")
    wip.to_csv(os.path.join(OUT, "opening_wip_live.csv"), index=False)
    print(f"  opening_wip {len(wip):,} rows, inventory_date {sorted(wip.inventory_date.astype(str).unique())}")
    old = pd.read_csv(os.path.join(OUT, "aging_comb.csv"), dtype=str)
    if os.environ.get("CTP_AGING_SOURCE", "db").lower() == "comb":
        # v14 (user 2026-10-01): aging from the local aging_comb.csv ONLY (no CTP_Master_Aging).
        # aging_comb has several rows per item (one per section); conflicting values are resolved
        # to the STRICTEST: shortest shelf life (min MaxAging), longest wait (max MinAging).
        old["ItemCode"] = old["ItemCode"].astype(str).str.strip()
        for c in ("MaxAging", "MinAging"):
            old[c] = pd.to_numeric(old[c], errors="coerce")
        out = old.groupby("ItemCode", as_index=False, sort=False).agg(
            MaxAging=("MaxAging", "min"), MinAging=("MinAging", "max"),
            MaxAgingUnit=("MaxAgingUnit", "first"), MinAgingUnit=("MinAgingUnit", "first"),
            Equipment=("Equipment", lambda s: "/".join(dict.fromkeys(s.dropna().astype(str)))))
        # a row with a shelf life but no min aging -> no minimum wait (0 h), as the plant table reads
        out.loc[out.MaxAging.notna() & out.MinAging.isna(), "MinAging"] = 0.0
        # min aging but no shelf life -> treated as no row (adapt_inputs' item-type fallback fills it)
        out.loc[out.MinAging.notna() & out.MaxAging.isna(), "MinAging"] = float("nan")
        out.loc[out.MaxAging.notna(), ["MaxAgingUnit", "MinAgingUnit"]] = "Hours"
        ag, miss = out, old.iloc[0:0]
        print(f"  aging  SOURCE = aging_comb.csv ONLY: {len(old):,} rows -> {len(out):,} items "
              f"({int(out.MaxAging.isna().sum())} with no values)")
    else:
        ag = _table("JK_CTP_PPDS", "CTP_Master_Aging")
        ag["ItemCode"] = ag["ItemCode"].astype(str).str.strip()
        eq = old.drop_duplicates("ItemCode").set_index("ItemCode")["Equipment"]
        ag["Equipment"] = ag["ItemCode"].map(eq).fillna("")
        miss = old[~old["ItemCode"].isin(ag["ItemCode"])]
        out = pd.concat([ag, miss], ignore_index=True)
    # GT SHELF LIFE (2026-09-27, user): 72 h for EVERY green tyre (DB had 0 h on 26 GT codes).
    gt_h = 72.0
    bgt = pd.read_csv(os.path.join(OUT, "bom_live.csv"), dtype=str, usecols=["child", "child_description"])
    gts = set(bgt.loc[bgt["child_description"].isin(["Green Tyres", "GREEN_TYRE"]), "child"].str.strip())
    out["ItemCode"] = out["ItemCode"].astype(str).str.strip()
    hit = out["ItemCode"].isin(gts)
    out.loc[hit, ["MaxAging", "MaxAgingUnit"]] = [gt_h, "Hours"]
    add = sorted(gts - set(out["ItemCode"]))
    if add:
        out = pd.concat([out, pd.DataFrame({"ItemCode": add, "MaxAging": gt_h, "MinAging": 0,
                                            "MaxAgingUnit": "Hours", "MinAgingUnit": "Hours",
                                            "Equipment": ""})], ignore_index=True)
    print(f"  GT shelf life: {int(hit.sum())} green-tyre rows set to {gt_h:.0f} h, {len(add)} added")
    # v50 EXPERIMENT (user 2026-10-09, config aging_relax): max aging (shelf life) x max_factor for every item except
    # green tyres (72 h kept); min aging unchanged. OFF = plant values.
    import yaml
    _ar = (yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {}).get("aging_relax") or {}
    if _ar.get("enabled", False):
        _f = float(_ar.get("max_factor", 1.0))
        _sel = ~out["ItemCode"].isin(gts) & pd.to_numeric(out["MaxAging"], errors="coerce").notna()
        out.loc[_sel, "MaxAging"] = pd.to_numeric(out.loc[_sel, "MaxAging"], errors="coerce") * _f
        print(f"  AGING RELAX (experiment): max aging x {_f:g} on {int(_sel.sum()):,} items (green tyres kept at {gt_h:.0f} h, "
              f"min aging unchanged)")
    out.to_csv(os.path.join(OUT, "aging_sep_live.csv"), index=False)
    print(f"  aging  CTP_Master_Aging {len(ag):,} + {miss.ItemCode.nunique()} items kept from aging_comb")


_SLIT, _MROLL = "CAP 66- slitted material", "CAP 66-MOTHERROLL"


def _cap66_bypass(d, tab):
    """4RC rule 2 (2026-10-06, config fourrc_rules.bypass_cap66_slitted): CAP 66-MOTHERROLL is made on
    the 4RC directly from the slitted material's own inputs (nylon fabric + B616M). BOM: each row
    MOTHERROLL -> slitted (qty q per MOTHERROLL Parent_qty) becomes one row per slitted child c with
    qty q x (c per 1 slitted); rows whose Parent is the slitted material are dropped. Routing: the
    slitted material's operation is dropped and the mother roll's inputs are the slitted inputs."""
    import yaml
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    if not ((cfg.get("fourrc_rules") or {}).get("bypass_cap66_slitted", False)):
        return d
    if tab == "CTP_Master_BOM":
        P, C = d["Parent"].astype(str).str.strip(), d["child"].astype(str).str.strip()
        sl = d[P == _SLIT].copy()
        sl["_r"] = pd.to_numeric(sl["child_quantity"], errors="coerce") / pd.to_numeric(sl["Parent_qty"], errors="coerce")
        kids = (sl.drop_duplicates(["child"]).set_index(sl.drop_duplicates(["child"])["child"].astype(str).str.strip()))
        link = d[(P == _MROLL) & (C == _SLIT)]
        new = []
        for _, r in link.iterrows():
            q = float(pd.to_numeric(r["child_quantity"], errors="coerce"))
            for c, kr in kids.iterrows():
                x = r.copy()
                x["child"], x["child_quantity"] = kr["child"], q * float(kr["_r"])
                x["child_Unit"], x["child_description"] = kr["child_Unit"], kr["child_description"]
                new.append(x)
        keep = ~((P == _MROLL) & (C == _SLIT)) & (P != _SLIT)
        out = pd.concat([d[keep], pd.DataFrame(new)], ignore_index=True)
        print(f"  [4RC rule 2] BOM: {len(link)} '{_MROLL} -> {_SLIT}' rows replaced by {len(new)} rows "
              f"(mother roll -> {', '.join(kids.index)}: {', '.join(f'{v:.4f}' for v in kids['_r'])} per m); "
              f"{int((P == _SLIT).sum())} slitted-material rows dropped")
        return out
    if tab == "CTP_Master_Routing":
        rp = d["routed_product"].astype(str).str.strip()
        sl = d[rp == _SLIT]
        inp = ",".join(sorted({x.strip() for v in sl["input_components_from_bom"].dropna().astype(str)
                               for x in v.split(",") if x.strip()}))
        d = d[rp != _SLIT].copy()
        m = d["routed_product"].astype(str).str.strip() == _MROLL
        d.loc[m, "input_components_from_bom"] = inp
        print(f"  [4RC rule 2] routing: {len(sl)} '{_SLIT}' operation rows dropped (2401/2402); "
              f"'{_MROLL}' inputs -> {inp}")
        return d
    return d


def _capstrip_direct(d, tab, key="capstrip_direct", tag="CAPSTRIP DIRECT"):
    """v120 (user 2026-10-14): GT > CAP 66 - CAPSTRIP > from CAP 66-MOTHERROLL ONLY, no slitter step. The cap strip
    demand goes straight to the mother roll (901); the cap ply slitter (701 / 702) is not scheduled (2401 / 2402 are
    already bypassed by 4RC rule 2). BOM: GT -> strip (m / tyre) becomes GT -> mother roll (strip m x mother-roll m per
    strip m of the master's strip -> mother roll row); strip -> mother roll rows dropped. Routing: the strip's slitter
    rows dropped. Ratios written to capstrip_direct.csv (apply_ctb_approximations converts strip stock with them).
    Config capstrip_direct: {enabled, items: {strip: mother roll}}; off = v117.
    v121 (user 2026-10-14): the same skip for edge gum (config edgegum_direct): EG01 > B1-PBLT > GT, the cap ply
    cutter (601) slit forms EGSL01 / 02 / 03 and BPGSL01 not scheduled -- belts / plies take EG0x / BPG01 directly."""
    try:
        import yaml
        cd = (yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {}).get(key) or {}
    except Exception:
        cd = {}
    if not cd.get("enabled", False):
        return d
    items = {str(k).strip(): str(v).strip() for k, v in (cd.get("items") or {}).items()}
    d = d.copy()
    if tab == "CTP_Master_BOM":
        P, C = d["Parent"].astype(str).str.strip(), d["child"].astype(str).str.strip()
        drop = pd.Series(False, index=d.index)
        ratios = []
        for strip, mroll in items.items():
            link = d[(P == strip) & (C == mroll)]
            r = (pd.to_numeric(link["child_quantity"], errors="coerce") /
                 pd.to_numeric(link["Parent_qty"], errors="coerce")).dropna()
            if r.empty:
                print(f"  [{tag}] !! no '{strip} -> {mroll}' row -- {strip} left as is")
                continue
            rat = float(r.median())
            use = (C == strip) & (P != strip)
            # ratio per tyre + consumer: the strip -> source row of the same Super_parent whose grand_parent is the
            # consumer (EGSL01 -> EG01 is 1/7 for most belts but 1/21..1/10.5 for some; BPGSL01 -> BPG01 differs
            # per ply); the median only where no such row exists
            rmap = dict(zip(zip(link["Super_parent"].astype(str).str.strip(), link["grand_parent"].astype(str).str.strip()),
                            r.reindex(link.index)))
            rr = pd.Series([rmap.get((s_, p_)) for s_, p_ in zip(d.loc[use, "Super_parent"].astype(str).str.strip(),
                                                                 P[use])], index=d.index[use], dtype=float)
            n_fb = int(rr.isna().sum())
            rr = rr.fillna(rat)
            d.loc[use, "child_quantity"] = pd.to_numeric(d.loc[use, "child_quantity"], errors="coerce") * rr
            d.loc[use, "child"] = mroll
            _desc = d.loc[C == mroll, "child_description"].dropna()
            _desc = str(_desc.iloc[0]) if len(_desc) else ""
            d.loc[use, "child_description"] = _desc
            drop |= (P == strip)
            ratios.append({"strip": strip, "mother_roll": mroll, "m_per_m": rat, "item_type": _desc})
            print(f"  [{tag}] BOM: {int(use.sum())} GT -> '{strip}' rows now take '{mroll}' directly "
                  f"(m per m of strip from the same tyre's row, {n_fb} by median {rat:.6f}; spread {r.min():.6f}.."
                  f"{r.max():.6f}); {int((P == strip).sum())} "
                  f"'{strip}' rows dropped -- no slitter step")
        pd.DataFrame(ratios).to_csv(os.path.join(OUT, f"{key}.csv"), index=False)
        return d[~drop]
    if tab == "CTP_Master_Routing":
        rp = d["routed_product"].astype(str).str.strip()
        for strip, mroll in items.items():
            m = rp == strip
            print(f"  [{tag}] routing: {int(m.sum())} '{strip}' rows dropped "
                  f"({sorted(set(d.loc[m, 'machines'].astype(str)))}) -- only '{mroll}' is scheduled")
            d = d[~m]
            rp = rp[~m]
        return d
    return d


def _linespeed_file(d, tab):
    """v129 / v130 TEST (user 2026-10-15): routing speeds from the line-speed workbook (one sheet per section; col D =
    measured production line speed [median], col E = changeover-master speed). Config linespeed_file: {enabled, path,
    mode, routing_sheets}. mode "max_DE" (v129): proc_time = max(D, E) for every item in the workbook. mode
    "faster_of_routing" (v130): proc_time = the FASTER of the DB routing value and max(D, E) (rate UOMs: the higher;
    MIN/BATCH, SEC/BATCH, MIN, SEC: the lower time); items of the sheets in routing_sheets keep the DB routing value.
    The routing row's UOM is kept. Every item's choice is written to ctp_inputfiles/linespeed_used.xlsx."""
    if tab != "CTP_Master_Routing":
        return d
    try:
        import yaml
        lf = (yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {}).get("linespeed_file") or {}
    except Exception:
        lf = {}
    if not lf.get("enabled", False):
        return d
    p = lf.get("path")
    p = p if os.path.isabs(p) else os.path.join(HERE, p)
    mode = str(lf.get("mode", "max_DE"))
    keep_sh = {str(s).strip() for s in (lf.get("routing_sheets") or [])}
    info = {}                                     # item -> dict(sheets, op, D, E, file)
    for sh, x in pd.read_excel(p, sheet_name=None).items():
        c = list(x.columns)
        x = x[x[c[1]].notna()]
        for _, r in x.iterrows():
            it = str(r[c[1]]).strip()
            Dv, Ev = pd.to_numeric(r[c[3]], errors="coerce"), pd.to_numeric(r[c[4]], errors="coerce")
            fv = pd.Series([Dv, Ev]).max()
            k = info.setdefault(it, {"sheets": [], "op": str(r[c[0]]), "D": None, "E": None, "file": None})
            k["sheets"].append(sh)
            if fv == fv and fv > 0 and (k["file"] is None or fv > k["file"]):
                k.update(D=Dv, E=Ev, file=float(fv), src_col=("D" if (Dv == Dv and Dv >= (Ev if Ev == Ev else -1)) else "E"))
    d = d.copy()
    rp = d["routed_product"].astype(str).str.strip()
    uom = d["proc_time_UOM"].astype(str).str.strip().str.upper()
    old = pd.to_numeric(d["proc_time"], errors="coerce")
    TIME_UOM = {"MIN/BATCH", "SEC/BATCH", "MIN", "SEC"}
    newv, src = old.copy(), pd.Series("routing (DB)", index=d.index)
    for i in d.index[rp.isin(info.keys())]:
        k = info[rp[i]]
        fv, ov = k["file"], old[i]
        if fv is None:
            continue
        if mode == "max_DE":
            use_file = True
        elif keep_sh & set(k["sheets"]):
            use_file = False
        elif ov != ov:
            use_file = True
        elif uom[i] in TIME_UOM:
            use_file = fv < ov
        else:
            use_file = fv > ov
        if use_file:
            newv[i] = fv
            src[i] = "production speed file, col " + k.get("src_col", "D") + (" (max D,E)" if mode == "max_DE" else " (faster)")
        elif keep_sh & set(k["sheets"]):
            src[i] = "routing (DB) -- section kept on routing"
    d["proc_time"] = d["proc_time"].astype(object)
    d["proc_time"] = newv.where(newv.notna(), d["proc_time"]).values
    rep = []
    import warnings
    warnings.filterwarnings("ignore", message="Mean of empty slice")
    for it, k in info.items():
        m = rp == it
        if not m.any():
            rep.append({"section (sheet)": ", ".join(k["sheets"]), "operation": k["op"], "item": it, "UOM": "",
                        "routing speed (DB)": None, "file col D (production median)": k["D"], "file col E (changeover master)": k["E"],
                        "file max(D,E)": k["file"], "speed used": None, "source": "item not in routing"})
            continue
        j = d.index[m][0]
        rep.append({"section (sheet)": ", ".join(k["sheets"]), "operation": str(d.at[j, "operation_name"]), "item": it,
                    "UOM": uom[j], "routing speed (DB)": old[m].median(), "file col D (production median)": k["D"],
                    "file col E (changeover master)": k["E"], "file max(D,E)": k["file"],
                    "speed used": pd.to_numeric(pd.Series(newv[m]), errors="coerce").median(), "source": src[j]})
    rdf = pd.DataFrame(rep).sort_values(["section (sheet)", "item"])
    try:
        rdf.to_excel(os.path.join(OUT, "linespeed_used.xlsx"), index=False)
    except Exception:
        rdf.to_csv(os.path.join(OUT, "linespeed_used.csv"), index=False)
    print(f"  [LINESPEED FILE] mode {mode}: {len(info)} items; source of the speed used -> "
          f"{rdf['source'].value_counts().to_dict()}; routing kept for sheets {sorted(keep_sh)}")
    return d


def _master_fixes(d, tab):
    """v116 (user 2026-10-14): logical fixes on the 14-Oct masters (config master_fixes; off = v98).
    bom_kg_per_tyre_from_old: {parent: {child: {factor, above}}} -- child_quantity x factor where the
      kg per parent unit is above `above` (SP003: metres halved but kg doubled -> x0.5 keeps the old
      kg per tyre). Applied only while the master still carries the inflated value.
    routing_keep: [{operation, uom_from, uom_to, proc_time}] -- e.g. BELT CUTTER M/MIN on NOS lots
      -> keep the old 15 CUTS/MIN (no metres per piece to convert with)."""
    try:
        import yaml
        mf = (yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8")) or {}).get("master_fixes") or {}
    except Exception:
        mf = {}
    if not mf.get("enabled", False):
        return d
    d = d.copy()
    if tab == "CTP_Master_BOM":
        for par, kids in (mf.get("bom_kg_per_tyre_from_old") or {}).items():
            # kids: {child: factor}; the factor is applied only when kg per parent unit is above `above`
            for kid, spec in kids.items():
                m = (d["Parent"].astype(str).str.strip() == par) & (d["child"].astype(str).str.strip() == kid)
                pq = pd.to_numeric(d.loc[m, "Parent_qty"], errors="coerce")
                cq = pd.to_numeric(d.loc[m, "child_quantity"], errors="coerce")
                hit = (cq / pq) > float(spec["above"])
                idx = hit[hit].index
                d.loc[idx, "child_quantity"] = cq[idx] * float(spec["factor"])
                print(f"  [master_fixes] BOM {par} -> {kid}: child_quantity x{spec['factor']} on {len(idx)} rows "
                      f"(kg per m {sorted({round(float(x), 3) for x in (cq / pq)[idx]})} -> "
                      f"{sorted({round(float(x) * float(spec['factor']), 3) for x in (cq / pq)[idx]})})")
    if tab == "CTP_Master_Routing":
        for r in (mf.get("routing_keep") or []):
            m = ((d["operation_name"].astype(str).str.strip() == r["operation"]) &
                 (d["proc_time_UOM"].astype(str).str.strip().str.upper() == r["uom_from"]))
            d.loc[m, "proc_time"] = r["proc_time"]
            d.loc[m, "proc_time_UOM"] = r["uom_to"]
            print(f"  [master_fixes] routing {r['operation']}: {r['uom_from']} -> {r['proc_time']} {r['uom_to']} "
                  f"on {int(m.sum())} rows ({d.loc[m, 'routed_product'].nunique()} items)")
    return d


def live_masters():
    """2026-09-29: BOM + routing from the LIVE plant masters (JK_CTP_PPDS.CTP_Master_BOM /
    CTP_Master_Routing, same columns as the old combined files + tyre_type). The 2-Sep local
    copies missed plant corrections (TBR belt wrap halved, chafer SNCT003 +55%, B333 chain +34%)."""
    for tab, out in (("CTP_Master_BOM", "bom_live.csv"), ("CTP_Master_Routing", "routing_live.csv")):
        d = _table("JK_CTP_PPDS", tab)
        if tab == "CTP_Master_BOM" and "tyre_type" in d.columns:
            # item -> PCR / TBR / BOTH (which line's MPQ applies to it)
            tt = d.assign(k=d["child"].astype(str).str.strip(),
                          t=d["tyre_type"].astype(str).str.strip().str.upper())
            tt = tt.groupby("k")["t"].agg(lambda s: "BOTH" if s.nunique() > 1 else s.iloc[0])
            tt.rename_axis("item").rename("tyre_type").reset_index().to_csv(
                os.path.join(OUT, "item_tyre_type_live.csv"), index=False)
            print(f"  item_tyre_type_live.csv {len(tt):,} items: {tt.value_counts().to_dict()}")
        d = d.drop(columns=["tyre_type"], errors="ignore")
        d = _cap66_bypass(d, tab)
        d = _master_fixes(d, tab)
        d = _linespeed_file(d, tab)
        d = _capstrip_direct(d, tab)
        d = _capstrip_direct(d, tab, key="edgegum_direct", tag="EDGE GUM DIRECT")
        d.to_csv(os.path.join(OUT, out), index=False)
        print(f"  {out:18s} {len(d):,} rows  <- JK_CTP_PPDS.{tab} (LIVE)")
    # CHANGEOVER MATRIX (2026-09-30): JK_CTP_PPDS.CTP_Master_ChangeoverMatrix (updated 26-Sep, 293k
    # pairs) replaces the 3-Jul local xlsx (211k pairs; 84k pairs had different minutes).
    cm = _table("JK_CTP_PPDS", "CTP_Master_ChangeoverMatrix")
    cm.to_csv(os.path.join(OUT, "changeover_live.csv"), index=False)
    print(f"  changeover_live.csv {len(cm):,} pairs <- JK_CTP_PPDS.CTP_Master_ChangeoverMatrix (LIVE)")
    # TBR MPQ (2026-09-29): the corrected MPQ file is the PCR sheet only, so TBR items took PCR floors
    # (TBR bead apex 400 vs plant 33, ply 219 vs 130). JK_CTP_PPDS.CTP_Master_MPQ carries the TBR rows.
    m = _table("JK_CTP_PPDS", "CTP_Master_MPQ")
    m = m[m["tyre_type"].astype(str).str.upper().str.strip() == "TBR"]
    m[["Item_Type", "Parameters", "Minimum_Run_Qty", "Maximum_Run_Qty", "UOM"]].to_csv(
        os.path.join(OUT, "mpq_tbr_live.csv"), index=False)
    print(f"  mpq_tbr_live.csv   {len(m)} TBR rows <- JK_CTP_PPDS.CTP_Master_MPQ (LIVE)")
    # 4RC roll lengths (2026-10-06): spool / dip length per calender material -> db_pipeline/raw (build_mpq_items.py)
    fr = _table("JK_CTP_PPDS", "CTP_Master_FRC")
    print(f"  CTP_Master_FRC     {len(fr)} calender materials (spool / dip length) (LIVE)")
    # v85 (user 2026-10-13): mixing batch weight per (compound, mixer) from JK_CTP_PPDS.CTP_Master_mixing (current,
    # allowed rows), written in the routing layout mixing_batches.BatchMap reads (config business_rules.mixing_batch_source
    # -> ctp_inputfiles/mixing_master_live.csv). Rows without a batch weight are skipped (BatchMap then falls back to the
    # DB routing's batch_size for that compound). Mixer codes are canonicalised by BatchMap (310 = K310, 270M = K270M ...).
    try:
        mm = _table("JK_CTP_PPDS", "CTP_Master_mixing")
        mm = mm[(pd.to_numeric(mm["is_current"], errors="coerce") == 1) & (pd.to_numeric(mm["is_allowed"], errors="coerce") == 1)]
        w = pd.to_numeric(mm["batch_weight_kg"], errors="coerce")
        # v87 (user 2026-10-13): a pair with NO batch weight in CTP_Master_mixing takes the compound's batch_size from
        # JK_CTP_PPDS.CTP_Master_Routing (routing_live.csv, written just above; one size per compound, not per mixer)
        _rl = pd.read_csv(os.path.join(OUT, "routing_live.csv"), dtype=str, low_memory=False)
        _rl = _rl[_rl["department"].astype(str).str.upper().str.contains("MIXING")]
        _rbs = pd.to_numeric(_rl["batch_size"], errors="coerce").groupby(_rl["routed_product"].astype(str).str.strip()).median()
        _nul = w.isna()
        w = w.where(~_nul, mm["item_code"].astype(str).str.strip().map(_rbs))
        print(f"  CTP_Master_mixing: {int(_nul.sum())} rows without batch weight -> routing batch_size "
              f"({int((_nul & w.notna()).sum())} filled, {int(w.isna().sum())} still empty)")
        out = pd.DataFrame({"routed_product": mm["item_code"].astype(str).str.strip(), "operation_name": mm["item_type"],
                            "machines": '"' + mm["machine_no"].astype(str).str.strip() + '"', "batch_size": w,
                            "batch_UNIT": "KG", "proc_time": pd.to_numeric(mm["cycle_time_sec"], errors="coerce") / 60.0,
                            "proc_time_UOM": "MIN/BATCH", "efficiency": 1.0, "machine_pref": mm["machine_pref"]})
        out.to_csv(os.path.join(OUT, "mixing_master_live.csv"), index=False)
        print(f"  mixing_master_live.csv {len(out)} rows / {out['routed_product'].nunique()} compounds <- "
              f"JK_CTP_PPDS.CTP_Master_mixing (LIVE); {int(w.isna().sum())} rows without a batch weight; "
              f"machine_pref set on {int((pd.to_numeric(mm['machine_pref'], errors='coerce').fillna(0) != 0).sum())} rows")
    except Exception as e:
        print(f"  !! CTP_Master_mixing unavailable ({type(e).__name__}: {e})")


if __name__ == "__main__":
    plans()
    live_masters()                   # BOM/routing first: live() reads the BOM
    live()
    # v15 (2026-10-04): refresh the PM / holiday windows the phases read from db_pipeline/raw
    # (an offline run falls back to the existing snapshot, unchanged)
    _table("jkplanning_CTP", "downtime_reason")
