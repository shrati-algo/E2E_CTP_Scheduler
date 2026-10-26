# plans/ - input plan folders

Each sub-folder is one planning input for `run_pipeline.py --plan-folder`. Files are
found by name (case-insensitive):

| file name contains | meaning | required |
|---|---|---|
| `building` and `pcr` | PCR building plan ("Shift Schedule" sheet) | yes |
| `building` and `tbr` | TBR building plan | yes |
| `curing` and `pcr` | PCR curing plan | no (derived if absent) |
| `curing` and `tbr` | TBR curing plan | no (derived if absent) |

`october_2026/` is the plan the v45b reference run used (`plan-latest` in the
original project).
