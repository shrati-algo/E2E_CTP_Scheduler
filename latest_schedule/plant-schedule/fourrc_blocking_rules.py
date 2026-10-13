#!/usr/bin/env python3
"""fourrc_blocking_rules.py vNN -- which 901 rule kept each LATE 901 lot from running earlier.
A lot is late when finish + min aging > its first consumer's start and it started > 2 h after its inputs were ready.
For the first idle gap on 901 (between inputs ready and its start) long enough for the lot, the blocking rule is
taken in this order: max aging at a later consumer, one go a day, creel 240 min, dip / wire load held for another
lot (4RC-10 hold / wire campaign), else the fixed day order. No idle gap long enough -> 901 busy with other lots."""
import sys, pandas as pd
v = sys.argv[1]; R = f"latest_schedule/{v}/run_output"; P = R + "/PHASEWISE"
S = pd.read_csv(R + "/phase5_schedule_updated.csv", low_memory=False).drop_duplicates("lot_id")
S["machine"] = S.machine.astype(str).str.strip('"'); S["s"] = pd.to_datetime(S.scheduled_start); S["f"] = pd.to_datetime(S.scheduled_finish)
SI = S.set_index("lot_id")
L = pd.read_csv(P + "/PHASE2_LOTS/phase2_lots_updated.csv", low_memory=False).set_index("lot_id")
mn = (L.min_aging_h.fillna(0) + L.transfer_time_h.fillna(0)); mx = L.max_aging_h
E = pd.read_csv(P + "/PHASE3_DAG/phase3_dag_edges_updated.csv", low_memory=False)
E = E[(E.consume_qty > 0) & E.producer_lot.isin(SI.index) & E.consumer_lot.isin(SI.index)]
fl = pd.read_csv(P + "/00_INPUTS/frc_lengths.csv")
cls = dict(zip(fl["item"].str.strip(), fl["class"].str.lower())); wm = dict(zip(fl["item"].str.strip(), fl["wire"].astype(str)))
f = S[S.machine == "901"].sort_values("s").reset_index(drop=True)
f["it"] = f.item.astype(str).str.strip(); f["w"] = f.it.map(lambda i: wm.get(i, i)); f["cl"] = f.it.map(lambda i: cls.get(i, "fabric"))
ein = E[E.consumer_lot.isin(f.lot_id)].copy()
ein["rdy"] = ein.producer_lot.map(SI.f) + pd.to_timedelta(ein.producer_lot.map(mn).fillna(0), unit="h")
f["rdy"] = f.lot_id.map(ein.groupby("consumer_lot").rdy.max()).fillna(pd.Timestamp("2026-10-12 07:00"))
eo = E[E.producer_lot.isin(f.lot_id)].copy(); eo["cs"] = eo.consumer_lot.map(SI.s)
f["need"] = f.lot_id.map(eo.groupby("producer_lot").cs.min()); f["lastc"] = f.lot_id.map(eo.groupby("producer_lot").cs.max())
f["dur"] = f.f - f.s
f["avail"] = f.f + pd.to_timedelta(f.lot_id.map(mn).fillna(0), unit="h")
f["late_h"] = (f.avail - f.need).dt.total_seconds() / 3600
f["load"] = (f.w != f.w.shift()).cumsum()
H7 = pd.Timedelta(hours=7); day = lambda t: (t - H7).normalize()
gaps = [(f.f.iloc[i], f.s.iloc[i + 1], i) for i in range(len(f) - 1) if f.s.iloc[i + 1] - f.f.iloc[i] > pd.Timedelta(minutes=20)]
rows = []
for i, r in f.iterrows():
    if pd.isna(r.need) or r.late_h <= 0.5 or (r.s - r.rdy).total_seconds() / 3600 <= 2:
        continue
    g = next(((a, b, k) for a, b, k in gaps if b > r.rdy and a < r.s and (b - max(a, r.rdy)) >= r.dur), None)
    if g is None:
        why = "901 busy with other lots the whole time (lot in front / day order)"
    else:
        t = max(g[0], r.rdy)
        mxh = mx.get(r.lot_id)
        prev_st = f[(f.f <= t) & (f.cl == "steel")].tail(1)
        if pd.notna(mxh) and pd.notna(r.lastc) and r.lastc - pd.Timedelta(hours=float(mxh) - 4) > t + r.dur:
            why = "max aging at a LATER consumer of the same lot (shelf life)"
        elif ((f.it == r.it) & (f.lot_id != r.lot_id) & (f.s.map(day) == day(t)) & (f.load != r.load)).any():
            why = "one go a day (item already has a block that plant day)"
        elif r.cl == "steel" and len(prev_st) and prev_st.w.iloc[0] != r.w and t < prev_st.f.iloc[0] + pd.Timedelta(minutes=240):
            why = "creel reload 240 min (steel cord change)"
        elif (f[(f.load == r.load) & (f.lot_id != r.lot_id)].rdy > t).any():
            why = "dip / wire load held for another lot (4RC-10 hold / wire campaign)"
        else:
            why = "fixed day-plan order (no rule needed it; 901 idle)"
    rows.append(dict(lot=r.lot_id, item=r.it, cls=r.cl, late_h=round(r.late_h, 1), why=why))
o = pd.DataFrame(rows)
print(f"{v}: {len(o)} late 901 lots ({o.late_h.sum():,.0f} lot-hours late)")
print(o.groupby("why").agg(lots=("lot", "size"), late_h=("late_h", "sum"), items=("item", lambda x: ", ".join(sorted(set(x))[:6])))
      .sort_values("late_h", ascending=False).round(0).to_string())
