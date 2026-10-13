"""Compare the plant's TDA day (tda_schedule.csv, 08-Oct; TDA1/2/3 = 2403/2404/2405) with v72 on the same machines:
rule fulfilment (routing machine, routing rate, matrix changeover, BR-63/64/65 inch rules, one go per day, aging) and
cycle times. Run from the repository root after format_plant_tda.py."""
import numpy as np
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

P = 'latest_schedule/plant-schedule/'
V = 'v72'
I = f'latest_schedule/{V}/run_output/PHASEWISE/00_INPUTS/'
OUT = P + f'PLANT_vs_{V}_TDA_COMPARISON.xlsx'
MAP = {'TDA1': '2403', 'TDA2': '2404', 'TDA3': '2405'}
MXL = {'2403': 'TBR Bead Apexing TDA 1', '2404': 'TBR Bead Apexing TDA 2', '2405': 'TBR Bead Apexing TDA 3'}
DAY0 = pd.Timestamp('2026-10-08 07:00')
LONG = 45

# ---------------- masters
rt = pd.read_csv(I + 'routing.csv', dtype=str, low_memory=False).apply(lambda c: c.str.strip()).drop_duplicates('routed_product').set_index('routed_product')
allowed = lambda k: rt.loc[k, 'machines'].replace('"', '').split(',') if k in rt.index else []
rate = lambda k: float(rt.loc[k, 'proc_time']) if k in rt.index else np.nan   # efficiency 1 (config efficiency_override), as the scheduler
inch = pd.read_csv(P + 'tbr_bead_inch.csv', dtype=str).set_index('item')['inch'].to_dict()
mx = pd.read_csv(I + 'changeover_matrix.csv', low_memory=False)
for c in ('machine', 'from_MaterialCode_O', 'to_MaterialCode_O'):
    mx[c] = mx[c].astype(str).str.strip()
co = {(m, a, b): (t, ty) for m, a, b, t, ty in zip(mx['machine'], mx['from_MaterialCode_O'], mx['to_MaterialCode_O'],
                                                   pd.to_numeric(mx['changeover_time_min'], errors='coerce'), mx['changeover_type'])}


def co_time(mc, a, b):
    t = co.get((MXL[mc], a, b)) or co.get(('TBR Bead Winding', a, b))
    return (t[0], t[1]) if t and t[0] == t[0] else (np.nan, 'no matrix value')


# ---------------- plant bookings (one booking = winding + apexing of ~33 beads)
fx = pd.read_excel(P + 'TDA_SCHEDULE_PLANT_formatted_2403-2405.xlsx', 'Bookings (prod_id)')
fx['machine'] = fx['machine'].map(MAP)
fx = fx.rename(columns={'s': 'start', 'e': 'end', 'beads_est': 'qty'}).sort_values(['machine', 'start'])
fx['kind'] = 'apex (incl. winding)'
PL = fx[['machine', 'item', 'kind', 'start', 'end', 'min', 'qty', 'prod_id']].copy()

# ---------------- v72 lots on 2403-2405 (rows split by shift -> merged per lot)
up = pd.read_excel(f'latest_schedule/{V}/CTP_06OCT-16OCT_SCHEDULE_{V}.xlsx', 'Upstream schedule')
up['machine'] = up['machine'].astype(str)
u = up[up['machine'].isin(MAP.values())].copy()
u['item'] = u['item'].astype(str).str.strip()
VL = u.groupby(['machine', 'lot_id', 'item', 'process']).agg(start=('start_time', 'min'), end=('end_time', 'max'),
                                                            qty=('produce_qty', 'sum')).reset_index()
VL['kind'] = np.where(VL['item'].str.startswith('TBWB'), 'winding (separate)', 'apex')
VL['min'] = (VL['end'] - VL['start']).dt.total_seconds() / 60
VL = VL.sort_values(['machine', 'start']).reset_index(drop=True)


def pday(t):
    return (t - pd.Timedelta(hours=7)).dt.date


PL['pday'], VL['pday'] = pday(PL['start']), pday(VL['start'])
PLd = PL[PL['end'] > DAY0].copy()               # plant day 08-Oct; 2405's first booking starts 22:47 the evening before
PLd['start'] = PLd['start'].clip(lower=DAY0 - pd.Timedelta(minutes=10))
PLd['min'] = (PLd['end'] - PLd['start']).dt.total_seconds() / 60


def runs(df):
    df = df.sort_values('start').copy()
    df['run'] = (df['item'] != df['item'].shift()).cumsum()
    return df.groupby('run').agg(item=('item', 'first'), kind=('kind', 'first'), start=('start', 'min'), end=('end', 'max'),
                                 qty=('qty', 'sum'), lots=('item', 'size'), busy_min=('min', 'sum')).reset_index(drop=True)


# ---------------- per machine-day measures + rule checks
def day_stats(df, mc, src, day):
    g = df[df['machine'] == mc]
    if not len(g):
        return None, []
    R = runs(g)
    ap = R[R['kind'] != 'winding (separate)']
    wd = R[R['kind'] == 'winding (separate)']
    out = []

    def add(rule, what, occ, ok, note=''):
        out.append({'source': src, 'day': day, 'machine': mc, 'rule': rule, 'check': what, 'occurrences': int(occ),
                    'followed': int(ok), 'compliance %': round(100 * ok / occ, 1) if occ else None, 'note': note})
    # routing machine
    bad = [i for i in R['item'].unique() if mc not in allowed(i)]
    add('BR-58/59 approved machine', 'item made on a machine its routing allows', R['item'].nunique(), R['item'].nunique() - len(bad), ', '.join(bad))
    # BR-63 inch dedication: one inch per machine per day
    ins = sorted(set(inch.get(i, '?') for i in ap['item']))
    add('BR-63 inch per machine', 'machine runs ONE bead inch in the day (no 20 / 22.5 mixing)', 1, int(len(ins) <= 1), '/'.join(ins))
    # BR-65 group: one run per item per day (apex)
    rc = ap['item'].value_counts()
    add('BR-65 grouping / one go', 'apexed bead item made in ONE run in the day', len(rc), int((rc <= 1).sum()),
        ', '.join(f'{k} x{v}' for k, v in rc[rc > 1].items()))
    # matrix changeover between consecutive runs (apex->apex, winding->winding by matrix; mixed kinds need setup too)
    n = ok = 0
    notes = []
    for i in range(1, len(R)):
        a, b = R.loc[i - 1], R.loc[i]
        t, ty = co_time(mc, a['item'], b['item'])
        gap = (b['start'] - a['end']).total_seconds() / 60
        if t == t and t > 0:
            n += 1
            ok += gap >= t - 0.5
            if gap < t - 0.5:
                notes.append(f"{a['item']}->{b['item']} gap {gap:.0f} < {t:.0f} min")
        elif t != t and a['item'] != b['item']:
            notes.append(f"{a['item']}->{b['item']}: no matrix value")
    add('BR-60/64 matrix changeover', 'gap between item switches >= matrix time (20 min die / preformer or inch)', n, ok, '; '.join(notes[:6]))
    busy = R['busy_min'].sum()
    st = {'source': src, 'day': day, 'machine': mc, 'apex items': ap['item'].nunique(), 'apex runs': len(ap),
          'apexed beads': int(ap['qty'].sum()), 'apex h': round(ap['busy_min'].sum() / 60, 1),
          'winding lots (separate)': int(wd['lots'].sum()), 'winding h (separate)': round(wd['busy_min'].sum() / 60, 1),
          'item switches': len(R) - 1, 'inch': '/'.join(ins), 'busy h': round(busy / 60, 1),
          'beads per busy h': round(ap['qty'].sum() / (busy / 60)) if busy else None}
    return st, out


ST, CK = [], []
for mc in MAP.values():
    s, c = day_stats(PLd, mc, 'PLANT', '08-Oct')
    ST.append(s)
    CK += c
for dd, g in VL.groupby('pday'):
    for mc in MAP.values():
        s, c = day_stats(g, mc, V, pd.Timestamp(dd).strftime('%d-%b'))
        if s:
            ST.append(s)
            CK += c
ST, CK = pd.DataFrame(ST), pd.DataFrame(CK)

# rule summary: plant 08-Oct vs v72 08-Oct vs v72 all days
RS = []
for rule in CK['rule'].unique():
    row = {'rule': rule, 'check': CK.loc[CK['rule'] == rule, 'check'].iloc[0]}
    for lab, sel in [('PLANT 08-Oct', (CK['source'] == 'PLANT')), (f'{V} 08-Oct', (CK['source'] == V) & (CK['day'] == '08-Oct')),
                     (f'{V} 06-19 Oct', CK['source'] == V)]:
        g = CK[sel & (CK['rule'] == rule)]
        o, f = g['occurrences'].sum(), g['followed'].sum()
        row[lab] = f'{f}/{o} ({100 * f / o:.0f} %)' if o else 'n/a'
    RS.append(row)
RS = pd.DataFrame(RS)
# plant books one roll after another with no gap, so the matrix gap cannot be seen; report the extra time in the first booking
RS['PLANT 08-Oct'] = RS['PLANT 08-Oct'].astype(object)
RS.loc[RS['rule'].str.startswith('BR-60'), 'PLANT 08-Oct'] = 'not visible (no gaps between bookings; see sheet Plant changeovers)'

# ---------------- cycle time per item (beads / min)
CT = []
pn = PL[PL['min'] <= LONG]
for (mc, it), g in pn.groupby(['machine', 'item']):
    CT.append({'machine': mc, 'item': it, 'inch': inch.get(it), 'plant bookings (normal)': len(g),
               'plant beads / min (median booking)': round((g['qty'] / g['min']).median(), 2),
               'plant beads / min (total)': round(g['qty'].sum() / g['min'].sum(), 2)})
CT = pd.DataFrame(CT)
va = VL[VL['kind'] == 'apex']
vr = (va.groupby(['machine', 'item'])['qty'].sum() / va.groupby(['machine', 'item'])['min'].sum()).round(2).rename(f'{V} apex beads / min')
CT = CT.merge(vr.reset_index(), on=['machine', 'item'], how='outer')
CT['routing apex NOS/min (eff 1)'] = CT['item'].map(lambda k: round(rate(k), 2))
wb = {i: [c for c in rt.index if False] for i in CT['item']}
bom = pd.read_csv(I + 'bom.csv', dtype=str, low_memory=False).apply(lambda c: c.str.strip())
bun = bom[bom['child'].str.startswith('TBWB', na=False)].drop_duplicates('Parent').set_index('Parent')['child'].to_dict()
CT['bead bundle'] = CT['item'].map(bun)
CT['routing winding NOS/min (eff 1)'] = CT['bead bundle'].map(lambda k: round(rate(k), 2) if isinstance(k, str) else None)
CT['v72 min per bead (winding + apex)'] = (1 / CT['routing winding NOS/min (eff 1)'] + 1 / CT['routing apex NOS/min (eff 1)']).round(2)
CT['plant min per bead (one booking)'] = (1 / CT['plant beads / min (total)']).round(2)
CT['plant as % of routing apex time'] = (CT['routing apex NOS/min (eff 1)'] / CT['plant beads / min (total)'] * 100).round(0)
CT['plant as % of v72 winding + apex time'] = (CT['plant min per bead (one booking)'] / CT['v72 min per bead (winding + apex)'] * 100).round(0)

# changeover seen inside the plant bookings: first booking of a new item vs the normal booking time
CO = []
for mc, g in PL.sort_values('start').groupby('machine'):
    g = g.reset_index(drop=True)
    med = (g['min'] / g['qty']).median()
    for i in range(1, len(g)):
        if g.loc[i, 'item'] != g.loc[i - 1, 'item'] and g.loc[i, 'start'] >= DAY0 - pd.Timedelta(minutes=10):
            extra = g.loc[i, 'min'] - g.loc[i, 'qty'] * med
            t, ty = co_time(mc, g.loc[i - 1, 'item'], g.loc[i, 'item'])
            CO.append({'machine': mc, 'from': g.loc[i - 1, 'item'], 'to': g.loc[i, 'item'], 'at': g.loc[i, 'start'],
                       'first booking min': round(g.loc[i, 'min'], 1), 'normal min for its beads': round(g.loc[i, 'qty'] * med, 1),
                       'extra min (changeover / stop)': round(extra, 1), 'matrix min': t, 'matrix type': ty,
                       'inch change': inch.get(g.loc[i - 1, 'item']) != inch.get(g.loc[i, 'item'])})
CO = pd.DataFrame(CO)

# ---------------- summary
def tot(df):
    return {'apexed beads': int(df['apexed beads'].sum()), 'apex h': round(df['apex h'].sum(), 1),
            'winding h (separate)': round(df['winding h (separate)'].sum(), 1), 'busy h (3 machines, 72 h)': round(df['busy h'].sum(), 1),
            'apex runs': int(df['apex runs'].sum()), 'item switches': int(df['item switches'].sum()),
            'beads per busy h': round(df['apexed beads'].sum() / df['busy h'].sum()) if df['busy h'].sum() else None}


vd = ST[ST['source'] == V]
steady = vd[vd['day'].isin(['09-Oct', '10-Oct', '11-Oct', '12-Oct', '13-Oct', '14-Oct', '15-Oct'])]
sd = [tot(g) for _, g in steady.groupby('day')]
SUM = pd.DataFrame([{'measure': k, 'PLANT 08-Oct': v, f'{V} 08-Oct': tot(vd[vd['day'] == '08-Oct'])[k],
                     f'{V} avg 09-15 Oct': round(np.mean([x[k] for x in sd]), 1)} for k, v in tot(ST[ST['source'] == 'PLANT']).items()])

hdr, bold = PatternFill('solid', fgColor='FF1F3864'), Font(name='Arial', bold=True, color='FFFFFFFF')
with pd.ExcelWriter(OUT, engine='openpyxl') as xw:
    for name, df in [('Summary', SUM), ('Rule summary', RS), ('Rule checks by machine-day', CK), ('Machine-day stats', ST),
                     ('Cycle time by item', CT), ('Plant changeovers', CO), ('Plant bookings', PL), (f'{V} lots 2403-2405', VL)]:
        df.to_excel(xw, sheet_name=name[:31], index=False)
        ws = xw.sheets[name[:31]]
        for c in ws[1]:
            c.fill, c.font, c.alignment = hdr, bold, Alignment(wrap_text=True, vertical='center')
        for j, c in enumerate(df.columns, 1):
            ws.column_dimensions[get_column_letter(j)].width = min(max(10, int(df[c].astype(str).str.len().head(300).max() if len(df) else 10) + 2, len(str(c)) + 2), 60)
        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = 'A2'
pd.set_option('display.width', 260)
pd.set_option('display.max_colwidth', 120)
print(SUM.to_string(index=False))
print(RS.to_string(index=False))
print(ST[(ST['source'] == 'PLANT') | (ST['day'].isin(['08-Oct', '10-Oct', '13-Oct']))].to_string(index=False))
print(CT.to_string(index=False))
print(CO.to_string(index=False))
print(CK[(CK['followed'] < CK['occurrences'])].groupby(['source', 'rule']).size())
print(CK[(CK['source'] == 'PLANT')][['machine', 'rule', 'occurrences', 'followed', 'note']].to_string(index=False))
print('written', OUT)
