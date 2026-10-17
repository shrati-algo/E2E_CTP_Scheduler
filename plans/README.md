# Plan folders

Put each plan in its own folder here (or anywhere), with exactly one each of the following:

    ctp_curing_schedule_*_pcr*.xlsx     ctp_curing_schedule_*_tbr*.xlsx
    ctp_building_schedule_*_pcr*.xlsx   ctp_building_schedule_*_tbr*.xlsx

Then run `python run_schedule.py --plans plans/<folder>`.

Plant plans are **not** kept in this public repository. The reference run used `plan-12oct` (PCR 13 / TBR 14) and the second example was `plan-130ct` (PCR 14 / TBR 15). The file format is in [../docs/INPUTS.md](../docs/INPUTS.md).
