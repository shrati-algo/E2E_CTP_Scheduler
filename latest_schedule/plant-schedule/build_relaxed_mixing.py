"""Relaxed mixing data for the v75_relaxed EVALUATION run (user 2026-10-11): per compound, the more favourable of
routing and the plant's 08-Oct measurement (PLANT_vs_SCHEDULER_MIXING_CYCLE_TIMES.xlsx).

- measured on an allowed mixer (>= 5 plant batches): plant cycle + plant batch weight when the plant's kg per second
  is higher than routing's, else routing (cycle and weight are taken as a pair -- the plant runs heavier batches longer);
- not measured: routing cycle x the plant / routing ratio of its mixers (plant faster only; ratio <= 1);
- batch weight is never lowered. Writes relaxed_mixing_overrides.csv. Run from the repository root."""
import pandas as pd

P = 'latest_schedule/plant-schedule/'
I = 'latest_schedule/v75/run_output/PHASEWISE/00_INPUTS/'
MIN_BATCHES = 5
RATIO = {'F270': 0.91, 'K310': 0.91, 'F440': 0.91, 'K270M': 0.90, 'K430-1': 0.93, 'K430-2': 1.00}   # plant / ours, by mixer

rt = pd.read_csv(I + 'routing.csv', dtype=str, low_memory=False)
rt = rt[rt['operation_name'].astype(str).str.upper().str.contains('MIXING')]
rt = rt.drop_duplicates('routed_product')
pc = pd.read_excel(P + 'PLANT_vs_SCHEDULER_MIXING_CYCLE_TIMES.xlsx')
pc = pc[pc['plant_batches'] >= MIN_BATCHES]

rows = []
for r in rt.itertuples():
    item = str(r.routed_product).strip()
    mach = [m.strip().strip('"').replace('310', 'K310') if m.strip().strip('"') == '310' else m.strip().strip('"')
            for m in str(r.machines).split(',') if m.strip()]
    unit = str(r.proc_time_UOM).upper()
    cyc = float(r.proc_time) * (60 if unit.startswith('MIN') else 1)
    kg = float(r.batch_size)
    if not (cyc > 0 and kg > 0):                     # no usable routing values -> left untouched
        continue
    m_ =pc[(pc['item'] == item) & pc['mixer'].isin(mach)]
    basis = 'routing'
    new_cyc, new_kg = cyc, kg
    if len(m_):
        w = m_['plant_batches']
        p_cyc = (m_['plant_cycle_s'] * w).sum() / w.sum()
        p_kg = (m_['plant_batch_kg'] * w).sum() / w.sum()
        if p_kg / p_cyc > kg / cyc * 1.0001:
            new_cyc, new_kg = p_cyc, max(p_kg, kg) if p_kg >= kg else kg
            if p_kg < kg:                      # never lower the weight: keep routing kg at the plant's kg/s
                new_cyc = kg / (p_kg / p_cyc)
            basis = f'plant measured ({int(w.sum())} batches on {",".join(m_["mixer"])})'
    else:
        rs = [RATIO[m] for m in mach if m in RATIO]
        if rs and min(1.0, sum(rs) / len(rs)) < 1.0:
            new_cyc = cyc * min(1.0, sum(rs) / len(rs))
            basis = f'mixer ratio {sum(rs) / len(rs):.2f} ({",".join(mach)})'
    rows.append({'item': item, 'machines': ','.join(mach), 'routing_cycle_s': round(cyc, 2), 'routing_batch_kg': kg,
                 'relaxed_cycle_s': round(new_cyc, 2), 'relaxed_batch_kg': round(new_kg, 1),
                 'routing_kg_per_h': round(kg / cyc * 3600), 'relaxed_kg_per_h': round(new_kg / new_cyc * 3600),
                 'gain_%': round((new_kg / new_cyc) / (kg / cyc) * 100 - 100, 1), 'basis': basis})
O = pd.DataFrame(rows)
O.to_csv(P + 'relaxed_mixing_overrides.csv', index=False)
print(O['basis'].str.split(' ').str[0].value_counts().to_dict())
print('median gain %', O['gain_%'].median(), '| measured gains', O[O['basis'].str.startswith('plant')]['gain_%'].describe().round(1).to_dict())
print(O.sort_values('gain_%', ascending=False).head(12).to_string(index=False))
