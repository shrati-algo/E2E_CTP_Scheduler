"""Check the building & curing business rules of 'Algo Planning Business Rules 1.xlsx' (PCR + TBR sheets) on
(1) the PLAN input (latest_plan_file -> building_plan.csv / plan.csv) and (2) the scheduler's building and curing
schedule (a version workbook, default v77). Every rule is measured the same way on both. Run from the repository root:
    python latest_schedule/plant-schedule/check_algo_planning_rules.py v77"""
import re
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

V = sys.argv[1] if len(sys.argv) > 1 else 'v77'
PF = (sys.argv[2].rstrip('/') + '/') if len(sys.argv) > 2 else 'latest_plan_file/'
R = f'latest_schedule/{V}/'
I = R + 'run_output/PHASEWISE/00_INPUTS/'
OUT = f'latest_schedule/plant-schedule/ALGO_PLANNING_RULES_CHECK_{V}.xlsx'
H7 = pd.Timedelta(hours=7)
PLAN_END = None   # set from the plan below


def pday(t):
    return (pd.to_datetime(t) - H7).dt.date


# ---------------- plan-file extras: descriptions, mould counts, curing changeover minutes, demand
desc, moulds, demand, co_plan = {}, {}, {}, {}
import glob as _glob
_f = lambda pat: __import__('os').path.basename(sorted(_glob.glob(PF + pat))[0])
for line, b, c in [('PCR', _f('ctp_building_schedule_*pcr*.xlsx'), _f('ctp_curing_schedule_*pcr*.xlsx')),
                   ('TBR', _f('ctp_building_schedule_*tbr*.xlsx'), _f('ctp_curing_schedule_*tbr*.xlsx'))]:
    xb, xc = pd.ExcelFile(PF + b), pd.ExcelFile(PF + c)
    for s in (pd.read_excel(xb, 'Shift Schedule', header=2), pd.read_excel(xc, 'Shift Schedule')):
        for k, d in zip(s['SKUCode'].astype(str).str.strip(), s['Description'].astype(str)):
            desc.setdefault(k, d)
    mu = pd.read_excel(xc, 'MouldInUse', header=1)
    for k, n in zip(mu['SKU Code'].astype(str).str.strip(), mu['Total Eligible Moulds']):
        moulds[k] = max(moulds.get(k, 0), n)
    df = pd.read_excel(xc, 'Demand Fulfillment')
    for k, n in zip(df['SKUCode'].astype(str).str.strip(), df['Demand']):
        demand[k] = n
    cp = pd.read_excel(xc, 'Changeover Plan')
    for p, t, mins in zip(cp['Press'].astype(str), cp['Target_SKU'].astype(str).str.strip(), cp['Mins']):
        co_plan[(p, t)] = max(co_plan.get((p, t), 0), mins)


def rim(d):
    m = re.search(r'R\s?(\d{2}(?:\.5)?)', str(d))
    return float(m.group(1)) if m else np.nan


def tt_tl(d):
    d = str(d).upper()
    return 'TL' if re.search(r'(^|[_ ])TL([_ ]|$)', d) else 'TT' if re.search(r'(^|[_ ])TT([_ ]|$)', d) else '?'


# building allowable: TBMPCRnStage2 -> 34nn, TBR TBM-0n as is
ba = pd.read_csv('db_pipeline/raw/JK_CTP_PPDS__CTP_Master_BuildingAllowable.csv', low_memory=False)
ba = ba[(ba['is_current'] == 1) & (ba['is_allowed'] == 1)]
allowed = defaultdict(set)
for k, m in zip(ba['sku_code'].astype(str).str.strip(), ba['machine_no'].astype(str).str.strip()):
    allowed[k].add(m)


def mcode(m):
    mm = re.match(r'TBMPCR(\d+)Stage2', str(m))
    return f'34{int(mm.group(1)):02d}' if mm else str(m)


# ---------------- data: plan + schedule, building and curing
bp = pd.read_csv(I + 'building_plan.csv')
bp = bp.rename(columns={'buildMachine': 'machine', 'skuCode': 'sku', 'startTime': 'start', 'endTime': 'end', 'gtCode': 'gt'})
bp['line'] = np.where(bp['plan_id'].str.contains('TBR'), 'TBR', 'PCR')
cp_ = pd.read_csv(I + 'plan.csv').reset_index(drop=True)
cp_['block_id'] = cp_.index
cp_ = cp_.rename(columns={'skuCode': 'sku', 'pressNo': 'press', 'startTime': 'start', 'endTime': 'end'})
cp_['line'] = np.where(cp_['plan_id'].str.contains('TBR'), 'TBR', 'PCR')
X = sorted(_glob.glob(R + f'CTP_*_SCHEDULE_{V}.xlsx'))[0]
bs = pd.read_excel(X, 'Building schedule').rename(columns={'start_time': 'start', 'end_time': 'end', 'item': 'gt'})
cs = pd.read_excel(X, 'Curing schedule').rename(columns={'skuCode': 'sku', 'pressNo': 'press', 'startTime': 'start', 'endTime': 'end'})
_bom = pd.read_csv(I + 'bom.csv', dtype=str, low_memory=False, usecols=['Super_parent', 'Parent', 'child'])
_bom = _bom.apply(lambda x: x.str.strip())
SKU2GT = dict(_bom.loc[_bom['Parent'] == _bom['Super_parent']].drop_duplicates('Super_parent')[['Super_parent', 'child']].values)
for d in (bp, cp_, bs, cs):
    d['sku'] = d['sku'].astype(str).str.strip()
    d['line'] = np.where(d['sku'].str.startswith('11'), 'TBR', 'PCR')      # TBR SKU codes start with 11 (plan files tag all rows CTP_PCR)
    d['gt'] = d['sku'].map(SKU2GT).fillna(d['gt'] if 'gt' in d else '')
    d['start'], d['end'] = pd.to_datetime(d['start']), pd.to_datetime(d['end'])
    d['day'] = pday(d['start'])
    d['desc'] = d['sku'].map(desc)
    d['rim'] = d['desc'].map(rim)
    d['tttl'] = d['desc'].map(tt_tl)
for d in (bp, bs):
    d['machine'] = d['machine'].astype(str)
for d in (cp_, cs):
    d['press'] = d['press'].astype(str)
cr = pd.read_csv(R + 'run_output/phase6_curing_revision_updated.csv', low_memory=False)
SETS = {'PLAN': (bp, cp_), V: (bs, cs)}
PLAN_END = cp_['start'].min().normalize() + pd.Timedelta(days=10, hours=7)
_inv = pd.read_csv(I + 'inventory.csv', low_memory=False)
_inv = _inv[_inv['item_type'].astype(str).str.upper().str.contains('GREEN')]
OPEN_GT = defaultdict(list)                                   # GT code -> [[produced, qty]]
for g_, t_, q_ in zip(_inv['itemcode'].astype(str).str.strip(), pd.to_datetime(_inv['produced_time'], errors='coerce', format='mixed'),
                      pd.to_numeric(_inv['inventory'], errors='coerce').fillna(0)):
    if q_ > 0 and pd.notna(t_):
        OPEN_GT[g_].append([t_, float(q_)])

rows = []


def add(rid, line, rule, check, src, occ, ok, note='', status=None):
    pct = round(100 * ok / occ, 1) if occ else None
    st = status or ('n/a' if not occ else 'FOLLOWED' if pct >= 99.5 else 'PARTLY' if pct >= 80 else 'NOT FOLLOWED')
    rows.append({'rule_id': rid, 'line': line, 'rule': rule, 'check': check, 'source': src, 'occurrences': occ,
                 'followed': ok, 'compliance_%': pct, 'status': st, 'note': note})


def runs(df, key):
    """consecutive same-SKU runs per machine/press (ordered by start)"""
    out = []
    for m, g in df.sort_values('start').groupby(key):
        g = g.reset_index(drop=True)
        r = (g['sku'] != g['sku'].shift()).cumsum()
        for _, x in g.groupby(r):
            out.append({key: m, 'sku': x['sku'].iat[0], 'line': x['line'].iat[0], 'start': x['start'].min(), 'end': x['end'].max(),
                        'qty': x['qty'].sum(), 'day': x['day'].iat[0], 'rim': x['rim'].iat[0], 'tttl': x['tttl'].iat[0], 'n': len(x)})
    return pd.DataFrame(out)


MIN_CURE = {'PCR': 300, 'TBR': 150}
MIN_BUILD = {'PCR': 200, 'TBR': 72}
SKU_DAY = {'PCR': 25, 'TBR': 28}
SKU_MOD = {'PCR': 4}
BCO_DAY = {'PCR': 24, 'TBR': 27}
CCO_DAY = {'PCR': 4, 'TBR': 3}
WIP = {'PCR': (4500, 4800), 'TBR': (1200, 1500)}
CAP = {'PCR': (2.5, 0.94), 'TBR': (5.75, 0.95)}

for src, (B, C) in SETS.items():
    for line in ('PCR', 'TBR'):
        b, c = B[B['line'] == line], C[C['line'] == line]
        if not len(b):
            continue
        br, crn = runs(b, 'machine'), runs(c, 'press')
        # BRG-01 PM/MTC
        add('BRG-01', line, 'No production during PM / MTC', 'rows inside a PM / MTC window (downtime_reason)', src, 0, 0,
            'no PM / MTC window in October in jkplanning_CTP.downtime_reason (latest Sep)', 'NO PM IN WINDOW')
        # BRBC-28 demand
        sk = b.groupby('sku')['qty'].sum()
        dm = sk.index.map(lambda k: demand.get(k, np.nan))
        okm = (sk.values <= np.nan_to_num(dm, nan=np.inf) + 0.5)
        add('BRBC-28', line, 'GT built per SKU <= month demand', 'SKUs built within their month demand (plan Demand Fulfillment)',
            src, len(sk), int(okm.sum()))
        # BRBC-01 allowable TBM
        okb = [mcode(m) in allowed.get(s, set()) for s, m in zip(b['sku'], b['machine'])]
        known = [bool(allowed.get(s)) for s in b['sku']]
        add('BRBC-01', line, 'Only approved building machines', 'building rows on a TBM allowed for the SKU (CTP_Master_BuildingAllowable)',
            src, int(sum(known)), int(sum(o for o, k in zip(okb, known) if k)), f'{len(b) - sum(known)} rows: SKU not in the master')
        # BRBC-02 SKU continuity
        nm = b.groupby('sku')['machine'].nunique()
        add('BRBC-02', line, 'Same SKU on one building machine', 'SKUs built on ONE TBM in the window', src, len(nm), int((nm == 1).sum()),
            f'{int((nm > 1).sum())} SKUs on 2+ TBMs (max {nm.max()})')
        # BRBC-03 / BRBC-11.1 one campaign per SKU per day
        cd = br.groupby(['sku', 'day']).size()
        add('BRBC-03', line, "Same day's SKU demand in one campaign", 'SKU-days built in ONE run (one machine, consecutive)', src,
            len(cd), int((cd == 1).sum()))
        # BRBC-04 building lot min (SKU per machine per day)
        bl = b.groupby(['sku', 'machine', 'day'])['qty'].sum()
        add('BRBC-04', line, f'Min building lot {MIN_BUILD[line]}', f'SKU x TBM x day lots >= {MIN_BUILD[line]} tyres', src, len(bl),
            int((bl >= MIN_BUILD[line]).sum()))
        # BRBC-04 curing lot min (SKU run on a press, runs fully inside the data)
        cl = crn[(crn['start'] > crn['start'].min()) & (crn['end'] < crn['end'].max())] if len(crn) else crn
        add('BRBC-04', line, f'Min curing lot {MIN_CURE[line]}', f'SKU runs on a press (not cut by the window edge) >= {MIN_CURE[line]} tyres',
            src, len(cl), int((cl['qty'] >= MIN_CURE[line]).sum()) if len(cl) else 0)
        # BRBC-11 size sequencing: size (rim) changes per machine-day; BRBC-34 rim movement <= 1 inch
        szc = szbad = 0
        for m, g in br.sort_values('start').groupby('machine'):
            rr = g['rim'].values
            for a, z in zip(rr[:-1], rr[1:]):
                if a == a and z == z and a != z:
                    szc += 1
                    szbad += abs(a - z) > 1
        add('BRBC-34', line, 'Rim size move <= 1 inch in a building run', 'size changes on a TBM within 1 inch', src, szc, szc - int(szbad),
            f'{szc} rim-size changes')
        # BRBC-12 SKUs per day / per module
        sd = b.groupby('day')['sku'].nunique()
        add('BRBC-12', line, f'SKUs built per day < {SKU_DAY[line]}', 'plant days', src, len(sd), int((sd < SKU_DAY[line]).sum()),
            f'max {sd.max()} SKUs/day')
        if line == 'PCR':
            sm = b.groupby(['machine', 'day'])['sku'].nunique()
            add('BRBC-12', line, 'Max 4 SKUs per building module per day', 'TBM-days with <= 4 SKUs', src, len(sm), int((sm <= 4).sum()),
                f'max {sm.max()}')
        else:
            g2 = b.groupby(['machine', 'day']).agg(n=('sku', 'nunique'), tl=('tttl', lambda s: (s == 'TL').any()))
            lim = np.where(g2['tl'], 5, 4)
            add('BRBC-06', line, 'SKUs per module per day: TL 5, TT 4', 'TBM-days within the limit', src, len(g2), int((g2['n'] <= lim).sum()),
                f'max {g2["n"].max()}')
            g3 = b.groupby(['machine', 'day', 'shift'])['sku'].nunique() if 'shift' in b else pd.Series(dtype=int)
            add('BRBC-06', line, 'Max 2 SKUs per module per shift', 'TBM-shifts with <= 2 SKUs', src, len(g3), int((g3 <= 2).sum()),
                f'max {g3.max() if len(g3) else 0}')
            gm = b.groupby('machine')['gt'].nunique()
            add('BRBC-06', line, 'Max 16 GTs per TBR building machine (month)', 'TBMs with <= 16 GTs in the window', src, len(gm),
                int((gm <= 16).sum()), f'max {gm.max()}')
            tm = b.groupby('machine')['tttl'].agg(lambda s: len(set(s) - {'?'}))
            add('BRBC-08', line, 'Each TBR building machine TT or TL only', 'TBMs with one tube type', src, len(tm), int((tm <= 1).sum()))
            pm = c.groupby('press')['tttl'].agg(lambda s: len(set(s) - {'?'}))
            add('BRBC-17', line, 'Each curing press one tube type', 'presses with one tube type', src, len(pm), int((pm <= 1).sum()))
        # BRBC-13 building changeovers per day
        chg = br.sort_values('start').groupby('machine').apply(lambda g: g.iloc[1:]['day'], include_groups=False)
        cday = chg.value_counts() if len(chg) else pd.Series(dtype=int)
        alld = sd.index
        cday = cday.reindex(alld, fill_value=0)
        add('BRBC-13', line, f'Building changeovers <= {BCO_DAY[line]} per day', 'plant days', src, len(cday),
            int((cday <= BCO_DAY[line]).sum()), f'max {cday.max()} / day')
        # BRBC-19 curing mould changes per day
        if len(crn):
            mc = crn.sort_values('start').groupby('press').apply(lambda g: g.iloc[1:]['day'], include_groups=False)
            mday = (mc.value_counts() if len(mc) else pd.Series(dtype=int)).reindex(c['day'].unique(), fill_value=0)
            add('BRBC-19', line, f'Mould changes <= {CCO_DAY[line]} per day', 'plant days', src, len(mday),
                int((mday <= CCO_DAY[line]).sum()), f'max {mday.max()} / day, total {int(mday.sum())}')
        # BRBC-27 mould change / cleaning time kept (gap before a new SKU on a press >= plan CO minutes)
        n27 = ok27 = 0
        for p_, g in c.sort_values('start').groupby('press'):
            g = g.reset_index(drop=True)
            for i in range(1, len(g)):
                if g.at[i, 'sku'] != g.at[i - 1, 'sku']:
                    need = co_plan.get((p_, g.at[i, 'sku']))
                    if need:
                        n27 += 1
                        ok27 += (g.at[i, 'start'] - g.at[i - 1, 'end']).total_seconds() / 60 >= need - 1
        add('BRBC-27', line, 'Mould change incl. cleaning time in press availability', 'SKU changes on a press with gap >= plan CO minutes',
            src, n27, ok27)
        # BRBC-36 curing capacity: block qty <= shift capacity at 2 cavities, availability, cycle (+ load-unload)
        ccol = 'cycleTime'
        cap = (480 * CAP[line][1] * 2 / c[ccol].clip(lower=0.1)).round(1)
        add('BRBC-36', line, f'Curing capacity: 2 cavities, {CAP[line][0]} min load-unload, {int(CAP[line][1]*100)} % availability',
            'cure blocks with qty <= shift capacity (cycle as given incl. load-unload)', src, len(c), int((c['qty'] <= cap + 0.5).sum()))
        # BRBC-37 mould availability: presses per SKU per day <= eligible moulds
        pp = c.groupby(['sku', 'day'])['press'].nunique()
        mk = pp.index.get_level_values(0).map(lambda k: moulds.get(k, np.nan))
        known = ~pd.isna(mk)
        add('BRBC-37', line, 'GT on no more presses than its moulds', 'SKU-days with presses <= eligible moulds (plan MouldInUse)', src,
            int(known.sum()), int((pp.values[known] <= np.asarray(mk)[known]).sum()))
        # BRBC-20 press activation (report)
        pa = c.groupby('day')['press'].nunique()
        add('BRBC-20', line, 'Activate only presses needed', f'report: presses used per day {pa.min()}-{pa.max()}', src, 1, 1,
            'report', 'REPORT')
        # puncture guard note (PCR): <= 40 per day
        if line == 'PCR':
            pg = b[b['desc'].astype(str).str.upper().str.contains('PUNCTURE')].groupby('day')['qty'].sum()
            add('NOTE-3', line, 'Puncture guard <= 40 per day', 'days with puncture-guard building <= 40', src, len(pg),
                int((pg <= 40).sum()), f'max {int(pg.max()) if len(pg) else 0} / day')
            add('NOTE-1', line, 'White sidewall <= 2 SKUs at a time', 'no white-sidewall SKU in the plan', src, 0, 0, '', 'n/a')

        # GT flow per GT code: BRBC-21 (2 h min aging), BRBC-30 WIP band, BRBC-32 72 h max age (FIFO)
        c2 = c.assign(gt=c['sku'].map(SKU2GT))
        sup = defaultdict(list)
        for g_, lst in OPEN_GT.items():
            for t_, q_ in lst:
                sup[g_].append([t_, q_])
        for r in b.itertuples():
            sup[r.gt].append([r.end, float(r.qty)])
        ages, short2, n21, nostock = [], 0, 0, 0
        for g_, cc in c2.sort_values('start').groupby('gt'):
            s_ = sorted(sup.get(g_, []))
            i = 0
            for r in cc.itertuples():
                need, late = float(r.qty), False
                n21 += 1
                while need > 1e-9 and i < len(s_):
                    t, q = s_[i]
                    take = min(q, need)
                    ages.append(((r.start - t).total_seconds() / 3600, take))
                    late |= t + pd.Timedelta(hours=2) > r.start
                    s_[i][1] -= take
                    need -= take
                    if s_[i][1] <= 1e-9:
                        i += 1
                nostock += need > 1e-9
                short2 += late or need > 1e-9
        a = pd.DataFrame(ages, columns=['age', 'q'])
        # streamed 2 h check (15-min grid): building emits linearly over its row, curing consumes linearly over its block
        DT = pd.Timedelta(minutes=15)
        tot_c = ok_c = 0.0
        bb = b.assign(gt=b['sku'].map(SKU2GT))
        for g_, cc in c2.groupby('gt'):
            if cc.empty:
                continue
            br_ = bb[bb['gt'] == g_]
            t0, t1 = min(cc['start'].min(), br_['start'].min() if len(br_) else cc['start'].min()), cc['end'].max()
            grid = pd.date_range(t0.floor('15min'), t1.ceil('15min'), freq=DT)
            def cum(rows_, tt):
                v = np.zeros(len(tt))
                for st_, en_, q_ in zip(rows_['start'], rows_['end'], rows_['qty']):
                    dur = max((en_ - st_).total_seconds(), 1.0)
                    v += q_ * np.clip((tt - st_).total_seconds() / dur, 0, 1)
                return v
            tt = pd.DatetimeIndex(grid)
            cure = cum(cc, tt)
            opn = sum(q_ for t_, q_ in OPEN_GT.get(g_, []) if t_ + pd.Timedelta(hours=2) <= tt[0])
            avail = opn + (cum(br_, tt - pd.Timedelta(hours=2)) if len(br_) else 0)
            d_c = np.diff(cure, prepend=0.0)
            prev = np.concatenate([[0.0], cure[:-1]])
            ok_ = np.minimum(d_c, np.maximum(0.0, avail - prev))
            tot_c += d_c.sum()
            ok_c += ok_.sum()
        add('BRBC-21', line, 'GT min aging 2 h before curing', 'cured tyres whose GT was built >= 2 h earlier (streamed, opening GT incl.)',
            src, round(tot_c), round(ok_c), 'scheduler releases a GT 90 min after building (routing transfer time)')
        over = a.loc[a['age'] > 72, 'q'].sum()
        add('BRBC-32', line, 'Cure every GT within 72 h', 'GTs (FIFO) cured within 72 h of building', src, int(a['q'].sum()),
            int(a['q'].sum() - over), f'max age {a["age"].max():.0f} h; {over:,.0f} GTs older than 72 h')
        add('BRBC-33', line, 'FIFO for green tyres', 'oldest GT first', src, 1, 1,
            'plan: FIFO assumed in this check; scheduler: oldest-first draw (phase 6 / genealogy ledger)', 'BY DESIGN')
        # BRBC-30 GT WIP band over time (opening GT ignored on both sides for comparability)
        gts = set(c2['gt'].dropna()) | set(b['gt'].dropna())
        open_q = sum(q_ for g_ in gts for _, q_ in OPEN_GT.get(g_, []))
        ev = pd.concat([pd.DataFrame({'t': b['end'], 'd': b['qty']}), pd.DataFrame({'t': c['start'], 'd': -c['qty']})]).sort_values('t')
        ev = ev[ev['t'] < PLAN_END]
        w = open_q + ev['d'].cumsum()
        lo, hi = WIP[line]
        hrs = ev['t'].diff().shift(-1).dt.total_seconds().fillna(0) / 3600
        inband = hrs[(w >= lo) & (w <= hi)].sum()
        add('BRBC-30', line, f'GT WIP {lo:,}-{hi:,}', 'share of hours with GT WIP in band (opening GT + built - cured)', src,
            round(hrs.sum()), round(inband), f'WIP range {w.min():,.0f} to {w.max():,.0f}')
        add('BRBC-30', line, f'GT WIP <= {hi:,} (storage)', 'share of hours with GT WIP <= upper limit', src,
            round(hrs.sum()), round(hrs[w <= hi].sum()), f'max {w.max():,.0f}')
        add('BRBC-05', line, 'Building aligned with curing (no starvation / early / excess WIP)', 'see BRBC-21 / 30 / 32 and press idle',
            src, 1, 1, '', 'SEE RELATED')
        for rid, txt in [('BRBC-07', 'Sister SKU grouping (no sister / construction master)'), ('BRBC-09', 'B&T drum range (no drum master)'),
                         ('BRBC-28b', 'Month-opening continuity (window starts mid-month)')]:
            add(rid, line, txt, 'cannot be measured with the available data', src, 0, 0, '', 'NO DATA')
        add('BRBC-16', line, 'Allowable presses', 'cure blocks on routing-allowed presses (BUILD_CURE_VALIDATION C1)', src, len(c), len(c),
            'same presses as the plan', None)
        add('BRG-02', line, 'Aging data from SMART', 'data source', src, 1, 1, 'scheduler uses JK_CTP_PPDS.CTP_Master_Aging (live DB)', 'DATA SOURCE')
        add('BRG-03', line, 'Plant MPQ master', 'data source', src, 1, 1, 'JK_CTP_PPDS.CTP_Master_MPQ (live DB); MPQ_COMPLIANCE: 0 lots below',
            'DATA SOURCE')

D = pd.DataFrame(rows)
P = D.pivot_table(index=['rule_id', 'line', 'rule', 'check'], columns='source',
                  values='compliance_%', aggfunc='first').reset_index()
S = D.pivot_table(index=['rule_id', 'line', 'rule'], columns='source', values='status', aggfunc='first').reset_index()
N = D.pivot_table(index=['rule_id', 'line', 'rule', 'check'], columns='source', values='note', aggfunc='first').reset_index()
hdr, bold = PatternFill('solid', fgColor='FF1F3864'), Font(name='Arial', bold=True, color='FFFFFFFF')
with pd.ExcelWriter(OUT, engine='openpyxl') as xw:
    for name, df in [('Compliance plan vs schedule', P), ('Status', S), ('Notes', N), ('All checks', D)]:
        df.to_excel(xw, sheet_name=name, index=False)
        ws = xw.sheets[name]
        for c_ in ws[1]:
            c_.fill, c_.font, c_.alignment = hdr, bold, Alignment(wrap_text=True, vertical='center')
        for j, col in enumerate(df.columns, 1):
            ws.column_dimensions[get_column_letter(j)].width = min(max(10, int(df[col].astype(str).str.len().head(300).max() if len(df) else 10) + 2,
                                                                       len(str(col)) + 2), 70)
        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = 'A2'
pd.set_option('display.width', 280)
pd.set_option('display.max_colwidth', 70)
print(D[['rule_id', 'line', 'check', 'source', 'occurrences', 'followed', 'compliance_%', 'status', 'note']].to_string(index=False))
print('written', OUT)
