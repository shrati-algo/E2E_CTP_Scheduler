import pandas as pd, numpy as np, sys
v=sys.argv[1]; R=f"latest_schedule/{v}/run_output"; P=R+"/PHASEWISE"
S=pd.read_csv(R+"/phase5_schedule_updated.csv",low_memory=False).drop_duplicates("lot_id")
S["machine"]=S.machine.astype(str).str.strip('"'); S["s"]=pd.to_datetime(S.scheduled_start); S["f"]=pd.to_datetime(S.scheduled_finish)
S["h"]=(S.f-S.s).dt.total_seconds()/3600
S=S[~S.department.astype(str).str.upper().isin(["BUILDING","CURING"])&~S.lot_id.astype(str).str.startswith("BLD")]
P4=pd.read_csv(P+"/PHASE4_CPM/phase4_lot_times_updated.csv",low_memory=False).set_index("lot_id")
print("LFT sample",P4.LFT.head(3).tolist(), "min/max", P4.LFT.min(), P4.LFT.max())
S["due"]=S.lot_id.map(P4.LFT)
t0=pd.Timestamp("2026-10-12 07:00")
# LFT in hours from t0? detect
if np.issubdtype(type(S.due.dropna().iloc[0]),np.number) or str(S.due.dropna().iloc[0]).replace('.','',1).lstrip('-').isdigit():
    S["due_h"]=pd.to_numeric(S.due,errors="coerce")
else:
    S["due_h"]=(pd.to_datetime(S.due)-t0).dt.total_seconds()/3600
S["start_h"]=(S.s-t0).dt.total_seconds()/3600
out=[]
for m,d in S.groupby("machine"):
    d=d.dropna(subset=["due_h"]).sort_values("due_h")
    if d.h.sum()<20: continue
    t=0.0; late_lots=0; late_h=0.0; maxlate=0.0
    for r in d.itertuples():          # EDD from t0, no input wait, no changeover: optimistic
        t+=r.h
        L=t-r.due_h
        if L>0.5: late_lots+=1; late_h+=r.h; maxlate=max(maxlate,L)
    act_late=(d.start_h+d.h>d.due_h+0.5).sum()
    out.append(dict(machine=m,section=d.department.iloc[0],need_h=round(d.h.sum(),1),load_pct=round(d.h.sum()/240*100),
        lots=len(d),best_case_late_lots=late_lots,best_case_max_late_h=round(maxlate,1),v_late_lots=int(act_late)))
o=pd.DataFrame(out).sort_values(["best_case_late_lots","load_pct"],ascending=False)
pd.set_option("display.width",250)
print(o.head(30).to_string(index=False))
