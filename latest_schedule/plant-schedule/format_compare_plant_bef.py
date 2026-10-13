"""Belt edge filler 1802 (BEF001 from B1144): format the plant schedule (beltedgefiller_schedule.xlsx) in the layout
of the other plant schedules and compare it with the scheduler (v74, v72) on the common plant days.
Data is only grouped, never changed. Run from the repository root."""
import re

import numpy as np
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

P = 'latest_schedule/plant-schedule/'
SRC = P + 'beltedgefiller_schedule.xlsx'
I = 'latest_schedule/v74/run_output/PHASEWISE/00_INPUTS/'
OUT_F = P + 'BELT_EDGE_FILLER_SCHEDULE_PLANT_formatted.xlsx'
OUT_C = P + 'PLANT_vs_v74_BELT_EDGE_FILLER_COMPARISON.xlsx'
VERS = ['v74', 'v72']
LONG = 25                                  # a booking longer than this (normal ~15 min) holds a stop
H7 = pd.Timedelta(hours=7)

# ---------------- masters
rt = pd.read_csv(I + 'routing.csv', dtype=str, low_memory=False).apply(lambda c: c.str.strip())
SPEED = float(rt.loc[rt['routed_product'] == 'BEF001', 'proc_time'].iloc[0])           # m/min, efficiency 1
bom = pd.read_csv(I + 'bom.csv', dtype=str, low_memory=False).apply(lambda c: c.str.strip())
r = bom[(bom['Parent'] == 'BEF001') & (bom['child'] == 'B1144')].iloc[0]
KGPM = float(r['child_quantity']) / float(r['Parent_qty'])                              # kg B1144 per m
MPT = bom[bom['child'] == 'BEF001'].assign(q=lambda x: x['child_quantity'].astype(float) / x['Parent_qty'].astype(float))['q'].median()
ag = pd.read_csv(I + 'aging_master.csv', dtype=str)
ag = ag[ag['ItemCode'].str.strip() == 'B1144'].iloc[0]
U = {'Days': 24, 'Hours': 1}
MIN_AGE, MAX_AGE = float(ag['MinAging']) * U[ag['MinAgingUnit']], float(ag['MaxAging']) * U[ag['MaxAgingUnit']]

# ---------------- plant
raw = pd.read_excel(SRC)
d = raw.copy()
d['pday'] = (d['start_time'] - H7).dt.date
bk = d.groupby('prod_id').agg(shift=('shift', 'first'), date=('date', 'first'), s=('start_time', 'min'), e=('end_time', 'max'),
                              kg=('qty', 'sum'), batches=('consumption_id', lambda x: ';'.join(dict.fromkeys(x.astype(str)))),
                              rows=('qty', 'size')).sort_values('s')
bk['min'] = (bk['e'] - bk['s']).dt.total_seconds() / 60
bk['metres_est'] = bk['kg'] / KGPM
bk['pday'] = (bk['s'] - H7).dt.date
normal = bk[(bk['min'] <= LONG) & (bk['kg'] > 0)]
KG_PER_MIN = normal['kg'].sum() / normal['min'].sum()                                     # plant running rate
bk['run_min_est'] = bk['kg'] / KG_PER_MIN
bk['stop_min_est'] = (bk['min'] - bk['run_min_est']).clip(lower=0)
bk['long'] = bk['min'] > LONG

# formatted schedule: per plant day x shift (one item, one machine -> rows = shifts)
rows = []
for (pdy, sh), g in bk.groupby(['pday', 'shift'], sort=False):
    rows.append({'line': 'TBR', 'plant_day': pdy, 'date': pd.to_datetime(g['date'].iloc[0]).date(), 'shift': sh,
                 'process': 'Belt edge filler (steel belt edge strip)', 'machine': 'TBR Belt EdgeFiller', 'machine_code': '1802',
                 'item': 'BEF001', 'item_type': 'Steel Belt Edge Strip', 'start_time': g['s'].min(), 'end_time': g['e'].max(),
                 'booked_min': round(g['min'].sum(), 1), 'bookings (prod_id)': len(g), 'long_bookings (> %d min)' % LONG: int(g['long'].sum()),
                 'stop_min_est (inside long bookings)': round(g['stop_min_est'].sum(), 1),
                 'B1144_kg': round(g['kg'].sum(), 2), 'metres_est (BOM)': round(g['metres_est'].sum()),
                 'TBR_tyres_equiv (BOM m per tyre)': round(g['metres_est'].sum() / MPT),
                 'B1144 batches (consumption_id)': ';'.join(dict.fromkeys(';'.join(g['batches']).split(';'))),
                 'prod_ids': ';'.join(g.index.astype(str)), 'source_rows': int(g['rows'].sum())})
M = pd.DataFrame(rows).sort_values('start_time').reset_index(drop=True)


def day_plant(g):
    return {'bookings': len(g), 'B1144 kg': round(g['kg'].sum()), 'metres (est.)': round(g['metres_est'].sum()),
            'TBR tyres equiv.': round(g['metres_est'].sum() / MPT), 'running h (est.)': round(g['run_min_est'].sum() / 60, 1),
            'stopped h (est.)': round(g['stop_min_est'].sum() / 60, 1), 'long bookings': int(g['long'].sum())}


DAYP = pd.DataFrame([{'plant_day': k, **day_plant(g)} for k, g in bk.groupby('pday')])

# B1144 batch age at use (consumption id F0xYYYYMMDDHHMMSS_n = mixing batch time)
ages = []
for x in d.itertuples():
    mt = re.match(r'^(F\d{2})(\d{14})_', str(x.consumption_id))
    if mt and x.qty > 0:
        t = pd.to_datetime(mt.group(2), format='%Y%m%d%H%M%S', errors='coerce')
        ages.append({'prod_id': x.prod_id, 'used_at': x.start_time, 'batch': x.consumption_id, 'batch_prefix': mt.group(1),
                     'batch_time': t, 'age_h': round((x.start_time - t).total_seconds() / 3600, 1), 'kg': x.qty})
AG = pd.DataFrame(ages)
AG['status'] = np.where(AG['age_h'] > MAX_AGE, 'OVER max aging', np.where(AG['age_h'] < MIN_AGE, 'UNDER min aging', 'OK'))
# B1144 mixing pattern seen by the plant: distinct batches by batch time -> runs (gap > 2 h = new run)
bt = AG.drop_duplicates('batch').sort_values('batch_time')
bt['run'] = (bt['batch_time'].diff().dt.total_seconds() / 3600 > 2).cumsum()
RUNS = bt.groupby('run').agg(prefix=('batch_prefix', lambda s: '/'.join(sorted(set(s)))), first_batch=('batch_time', 'min'),
                             last_batch=('batch_time', 'max'), batches_used=('batch', 'size')).reset_index(drop=True)
RUNS['hours since previous run'] = RUNS['first_batch'].diff().dt.total_seconds().div(3600).round(1)

# ---------------- scheduler versions
VD, VL = {}, {}
for v in VERS:
    up = pd.read_excel(f'latest_schedule/{v}/CTP_06OCT-16OCT_SCHEDULE_{v}.xlsx', 'Upstream schedule')
    up['machine'] = up['machine'].astype(str)
    e = up[up['machine'] == '1802'].copy()
    e['s'], e['e'] = pd.to_datetime(e['start_time']), pd.to_datetime(e['end_time'])
    e['h'] = (e['e'] - e['s']).dt.total_seconds() / 3600
    e['pday'] = (e['s'] - H7).dt.date
    VL[v] = e
    b = up[up['item'].astype(str).str.strip() == 'B1144']
    b = b.assign(pday=(pd.to_datetime(b['start_time']) - H7).dt.date)
    rows_ = []
    for k, g in e.groupby('pday'):
        rows_.append({'plant_day': k, 'metres': round(g['produce_qty'].sum()), 'TBR tyres equiv.': round(g['produce_qty'].sum() / MPT),
                      'B1144 kg': round(g['produce_qty'].sum() * KGPM), 'running h': round(g['h'].sum(), 1),
                      'idle h': round(24 - g['h'].sum(), 1), 'B1144 runs that day': int(b[b['pday'] == k]['lot_id'].nunique())})
    VD[v] = pd.DataFrame(rows_)

common = sorted(set(DAYP['plant_day']) & set(VD['v74']['plant_day']))
common = [c for c in common if c <= pd.Timestamp('2026-10-10').date()]       # full plant days in both
CMP = []
for c in common:
    p = DAYP[DAYP['plant_day'] == c].iloc[0]
    row = {'plant_day': c, 'PLANT metres (est.)': p['metres (est.)'], 'PLANT tyres equiv.': p['TBR tyres equiv.'],
           'PLANT running h (est.)': p['running h (est.)'], 'PLANT stopped h (est.)': p['stopped h (est.)']}
    for v in VERS:
        q = VD[v][VD[v]['plant_day'] == c]
        q = q.iloc[0] if len(q) else None
        row[f'{v} metres'] = q['metres'] if q is not None else 0
        row[f'{v} tyres equiv.'] = q['TBR tyres equiv.'] if q is not None else 0
        row[f'{v} running h'] = q['running h'] if q is not None else 0
    CMP.append(row)
CMP = pd.DataFrame(CMP)
tot = CMP.drop(columns='plant_day').sum(numeric_only=True)
CMP = pd.concat([CMP, pd.DataFrame([{'plant_day': 'TOTAL 06-10 Oct', **tot.round(1).to_dict()}])], ignore_index=True)

# v74 idle gaps (waiting for B1144)
gaps = []
for v in VERS:
    e = VL[v].sort_values('s').reset_index(drop=True)
    for i in range(1, len(e)):
        gh = (e.loc[i, 's'] - e.loc[i - 1, 'e']).total_seconds() / 3600
        if gh > 1 and e.loc[i, 's'] < pd.Timestamp('2026-10-16 07:00'):
            gaps.append({'version': v, 'idle from': e.loc[i - 1, 'e'], 'idle to': e.loc[i, 's'], 'idle h': round(gh, 1),
                         'next lot': e.loc[i, 'lot_id'], 'next lot inputs': e.loc[i, 'consumption_id']})
GAPS = pd.DataFrame(gaps)

# plant stops by shift and hour of day
st = bk[bk['long']].copy()
st['hour'] = st['s'].dt.hour
STOPS = st.groupby('shift').agg(stops=('min', 'size'), stop_h=('stop_min_est', lambda s: round(s.sum() / 60, 1)),
                                median_stop_min=('stop_min_est', 'median'),
                                typical_start_hours=('hour', lambda s: ', '.join(f'{h:02d}h' for h in s.value_counts().index[:3]))).reset_index()

plant_speed = round(KG_PER_MIN / KGPM, 2)
SUM = pd.DataFrame([
    {'measure': 'data window', 'PLANT': f"{bk['s'].min():%d-%b %H:%M} -> {bk['e'].max():%d-%b %H:%M} ({len(bk)} bookings)",
     'v74': '06-Oct 07:00 -> 16-Oct 07:00', 'v72': '06-Oct 07:00 -> 16-Oct 07:00'},
    {'measure': 'running speed (m/min)', 'PLANT': f'{plant_speed} (B1144 kg / BOM kg per m, normal bookings)', 'v74': SPEED, 'v72': SPEED},
    {'measure': 'metres per day, 06-10 Oct (avg)', 'PLANT': round(CMP.iloc[:-1]['PLANT metres (est.)'].mean()),
     'v74': round(CMP.iloc[:-1]['v74 metres'].mean()), 'v72': round(CMP.iloc[:-1]['v72 metres'].mean())},
    {'measure': 'TBR tyres equiv. per day, 06-10 Oct (avg)', 'PLANT': round(CMP.iloc[:-1]['PLANT tyres equiv.'].mean()),
     'v74': round(CMP.iloc[:-1]['v74 tyres equiv.'].mean()), 'v72': round(CMP.iloc[:-1]['v72 tyres equiv.'].mean())},
    {'measure': 'running h per day, 06-10 Oct (avg)', 'PLANT': round(CMP.iloc[:-1]['PLANT running h (est.)'].mean(), 1),
     'v74': round(CMP.iloc[:-1]['v74 running h'].mean(), 1), 'v72': round(CMP.iloc[:-1]['v72 running h'].mean(), 1)},
    {'measure': 'how it stops', 'PLANT': f"{int(bk['long'].sum())} stops of ~2-2.5 h inside bookings (about one per shift), machine never idle > 0 min between bookings",
     'v74': f"{int((GAPS['version'] == 'v74').sum())} idle gaps of 8-18 h waiting for B1144", 'v72': f"{int((GAPS['version'] == 'v72').sum())} idle gaps"},
    {'measure': 'B1144 age at use (h)', 'PLANT': f"{AG['age_h'].min():.0f}-{AG['age_h'].max():.0f} (median {AG['age_h'].median():.0f}); "
                                                f"{int((AG['status'] == 'OVER max aging').sum())} uses over {MAX_AGE:.0f} h, {int((AG['status'] == 'UNDER min aging').sum())} under {MIN_AGE:.0f} h",
     'v74': f'within {MIN_AGE:.0f}-{MAX_AGE:.0f} h (enforced)', 'v72': f'within {MIN_AGE:.0f}-{MAX_AGE:.0f} h (enforced)'},
    {'measure': 'B1144 mixing runs seen (batch times, gap > 2 h = new run)', 'PLANT': f"{len(RUNS)} runs in {(bt['batch_time'].max() - bt['batch_time'].min()).days} days, median {RUNS['hours since previous run'].median():.0f} h apart",
     'v74': f"{VL['v74'].pipe(lambda e: 0)}" if False else '9 runs 06-16 Oct (one mixer / day, >= 15 h apart)', 'v72': ''},
])

note = pd.DataFrame({'note': [
    f'Source: {SRC} ({len(raw):,} rows, {raw["prod_id"].nunique()} bookings) -- data NOT changed; original rows in "Source data (as received)".',
    'One machine (TBR Belt EdgeFiller = 1802), one item (BEF001), one input (B1144). Bookings run back to back (no gaps), '
    f'so stops sit inside bookings longer than {LONG} min (normal booking ~15 min, ~37.5 kg B1144).',
    f'metres_est = B1144 kg / BOM {KGPM:.4f} kg per m (1.530354 kg per 6.146 m); TBR tyres equiv. = metres / {MPT:.3f} m per tyre (BOM median).',
    f'running time = kg / the plant running rate {KG_PER_MIN:.2f} kg/min (normal bookings) = {plant_speed} m/min; stop = booking - running time.',
    f'B1144 age = use time - batch time from the consumption id (F0x + yyyymmddhhmmss); aging master B1144 {MIN_AGE:g}-{MAX_AGE:g} h.',
    'Plant day = 07:00 -> 07:00; 02-Oct (holiday) has no bookings except one long one; 30-Sep and 11-Oct are part days.']})


def write(path, sheets):
    hdr, bold = PatternFill('solid', fgColor='FF1F3864'), Font(name='Arial', bold=True, color='FFFFFFFF')
    with pd.ExcelWriter(path, engine='openpyxl') as xw:
        for name, df in sheets:
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            for c in ws[1]:
                c.fill, c.font, c.alignment = hdr, bold, Alignment(wrap_text=True, vertical='center')
            for j, c in enumerate(df.columns, 1):
                ws.column_dimensions[get_column_letter(j)].width = min(max(10, int(df[c].astype(str).str.len().head(300).max() if len(df) else 10) + 2, len(str(c)) + 2), 60)
                if str(c) in ('start_time', 'end_time', 's', 'e', 'used_at', 'batch_time', 'first_batch', 'last_batch', 'idle from', 'idle to'):
                    for rr in range(2, len(df) + 2):
                        ws.cell(row=rr, column=j).number_format = 'dd-mmm-yy hh:mm'
            ws.auto_filter.ref = ws.dimensions
            ws.freeze_panes = 'A2'


write(OUT_F, [('Summary by day', DAYP), ('1802 schedule', M), ('Stops by shift', STOPS), ('Bookings (prod_id)', bk.reset_index()),
              ('B1144 age at use', AG), ('B1144 runs seen', RUNS), ('Source data (as received)', raw), ('Notes', note)])
write(OUT_C, [('Summary', SUM), ('By day 06-10 Oct', CMP), ('Plant by day', DAYP)] + [(f'{v} by day', VD[v]) for v in VERS]
      + [('Scheduler idle gaps', GAPS), ('Plant stops by shift', STOPS), ('Plant B1144 age', AG), ('Plant B1144 runs', RUNS), ('Notes', note)])

pd.set_option('display.width', 250)
pd.set_option('display.max_colwidth', 130)
print('rows', len(raw), 'bookings', len(bk), '| kg', round(raw['qty'].sum(), 1), '=', round(M['B1144_kg'].sum(), 1))
print(SUM.to_string(index=False))
print(CMP.to_string(index=False))
print(DAYP.to_string(index=False))
print(STOPS.to_string(index=False))
print(AG['status'].value_counts().to_dict(), AG.groupby('status')['age_h'].agg(['min', 'max']).to_string())
print(RUNS.to_string(index=False))
print(GAPS.to_string(index=False))
print('written', OUT_F, OUT_C)
