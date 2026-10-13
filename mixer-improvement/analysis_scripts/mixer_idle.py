import sys, pandas as pd, numpy as np
pd.set_option('display.width', 260); pd.set_option('display.max_colwidth', 80)
ROOT = r'C:\Users\shradha\Downloads\JKT-CTP-SCHEDULER-main\JKT-CTP-SCHEDULER-main'
ST, END = pd.Timestamp('2026-10-06 07:00'), pd.Timestamp('2026-10-16 07:00')
MIX = ['F270', 'K310', 'F440', 'K270M', 'K430-1', 'K430-2']
TOL = pd.Timedelta(minutes=10)
H = pd.Timedelta(hours=1)
GAPW = {}


def run(v):
    R = f'{ROOT}/latest_schedule/{v}/run_output/'
    s = pd.read_csv(R + 'phase5_schedule_updated.csv', parse_dates=['scheduled_start', 'scheduled_finish'])
    L = pd.read_csv(R + 'phase2_lots_updated.csv', low_memory=False, parse_dates=['first_need', 'last_need'])
    T = pd.read_csv(R + 'PHASEWISE/PHASE4_CPM/phase4_lot_times_updated.csv'); T['LFT'] = pd.to_datetime(T['LFT'], errors='coerce', format='mixed')
    E = pd.read_csv(R + 'PHASEWISE/PHASE3_DAG/phase3_dag_edges_updated.csv', usecols=['producer_lot', 'consumer_lot', 'consume_qty'])
    E = E[pd.to_numeric(E.consume_qty, errors='coerce').fillna(0) > 0]
    pred = E.groupby('consumer_lot').producer_lot.apply(list).to_dict()
    fin = dict(zip(s.lot_id, s.scheduled_finish)); sta = dict(zip(s.lot_id, s.scheduled_start))
    Li = L.set_index('lot_id')
    mn = (Li.min_aging_h.fillna(0) + Li.transfer_time_h.fillna(0)).to_dict()
    mx = Li.max_aging_h.to_dict(); it = Li.item_code.astype(str).str.strip().to_dict()
    ity = Li.item_type.to_dict(); bat = Li.batches.to_dict(); mach = Li.machines.astype(str).to_dict()
    lft = dict(zip(T.lot_id, T.LFT)); dur = (s.set_index('lot_id').scheduled_finish - s.set_index('lot_id').scheduled_start).to_dict()

    def ready(l):
        r = ST
        for p in pred.get(l, ()):
            if p in fin:
                r = max(r, fin[p] + pd.Timedelta(hours=float(mn.get(p, 0) or 0)))
        return r

    def shelf(l):
        lf, m_ = lft.get(l), mx.get(l)
        if lf is None or pd.isna(lf) or not m_ or m_ != m_:
            return ST
        return max(ST, lf - dur.get(l, pd.Timedelta(0)) - pd.Timedelta(hours=float(m_) - 2))

    rows, other = [], []
    mxl = s[s.machine.isin(MIX) & ~s.lot_id.str.startswith('BLD')].copy()
    mxl['rdy'] = mxl.lot_id.map(ready); mxl['shf'] = mxl.lot_id.map(shelf)
    for m, g in mxl.groupby('machine'):
        g = g.sort_values('scheduled_start')
        prev_f, prev_it = ST, None
        lastrun = {}
        for _, r in g.iterrows():
            if r.scheduled_start >= END:
                break
            t0 = max(prev_f, ST)
            gap = (r.scheduled_start - t0)
            if gap > pd.Timedelta(minutes=5):
                rd, sf = r.rdy, r.shf
                if abs(r.scheduled_start - rd) <= TOL and rd > t0:
                    c = 'A waiting for input (master / compound not ready)'
                elif abs(r.scheduled_start - sf) <= TOL and sf > t0:
                    c = 'B shelf-life floor (too early to make)'
                else:
                    c = 'C held by sequencing / repair order (inputs ready, allowed)'
                # was other work available for this mixer at t0?
                cand = mxl[(mxl.scheduled_start > r.scheduled_start) & (mxl.rdy <= t0) & (mxl.shf <= t0)
                           & mxl.lot_id.map(lambda x: m in mach.get(x, ''))]
                rows.append(dict(machine=m, gap_h=gap / H, cause=c, next=r.lot_id, other_ready=len(cand)))
            prev_f = max(prev_f, r.scheduled_finish)
    G = pd.DataFrame(rows)
    w = mxl[mxl.scheduled_start < END]
    busy = ((w.scheduled_finish.clip(upper=END) - w.scheduled_start) / H).groupby(w.machine).sum()
    out = pd.DataFrame({'busy_h': busy.round(0)})
    out['idle_h'] = (240 - out.busy_h).round(0)
    piv = G.pivot_table(index='machine', columns='cause', values='gap_h', aggfunc='sum', fill_value=0).round(0)
    out = out.join(piv)
    out['idle_with_other_ready_work_h'] = G[G.other_ready > 0].groupby('machine').gap_h.sum().round(0)
    print(f'\n===== {v} =====')
    print(out.fillna(0).to_string())
    tot = G.groupby('cause').gap_h.sum().round(0)
    print('TOTAL idle by cause (h):', tot.to_dict(), '| idle with other ready work (h):', round(G[G.other_ready > 0].gap_h.sum()))
    # demand vs capacity
    cl = L[L.item_type.isin(['MASTER COMPOUND', 'FINAL COMPOUND'])]
    need = cl[cl.first_need < END].groupby('item_type').batches.sum()
    made = w.merge(L[['lot_id', 'item_type', 'batches']], on='lot_id').groupby('item_type').batches.sum()
    late = s.merge(L[['lot_id', 'item_type', 'first_need']], on='lot_id')
    late = late[late.item_type.isin(['MASTER COMPOUND', 'FINAL COMPOUND'])]
    print('batches needed before plan end:', need.round(0).to_dict(), '| made in window:', made.round(0).to_dict())
    print('compound lots finishing after first need: %d of %d' % ((late.scheduled_finish > late.first_need).sum(), len(late)))
    return G


for v in sys.argv[1:]:
    run(v)
