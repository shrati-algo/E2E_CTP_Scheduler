# Step 7: checks on the schedule

| Script | Writes | Checks |
|---|---|---|
| `engine/validate_floor_schedule.py` | `VALIDATION.csv`, `VALIDATION_FAILURES*.csv` | 7 tests per line: (1) machine allowed by the routing, (2) department, (3) routing sequence + min ageing, (4) full kit before building, (5) machine coverage, (6) even curing quantities, (7) shelf life |
| `engine/make_machine_coverage.py` | `MACHINE_COVERAGE.csv` | Which register machines carry work |
| `engine/br_compliance_v15.py` | `BUSINESS_RULES_COMPLIANCE_v15.csv`, `BUSINESS_RULES_MISSED_v15.csv` | The plant's 76 business rules: applied / partial / missed |
| `engine/mixing_compliance.py` | `MIXING_RULE_COMPLIANCE.csv` | Mixing rules (batches, repeat gap, clean-outs, butyl, …) |

**Known result.** Test 5 "machine coverage" fails on both lines, as in v46k:
- the cutters and slitters are bypassed by design (cut stock is converted);
- TBM 3804 has only data-blocked SKUs.

That is why the exit code is 1 on this data.

These steps never stop the run: a failure is reported and the run continues.
