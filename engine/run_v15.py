#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_v15.py -- end-to-end CTP schedule with every v14 rule, from a chosen opening-WIP date/time.

v15 (2026-10-04) = v14 rules (config.yaml unchanged: whole mixing batches, 60 s changeover, 120 s
final-mixer butyl clean-out / none on master mixers, IL3/IL5 daily campaign, +5 buffer batches,
aging from aging_comb.csv only) + opening WIP read LIVE from the DB for --wip-date, plan cut to
--plan-start .. --plan-end. See HOW_TO_RUN_V15.md.

    python run_v15.py                                   # v15 defaults: WIP 03-Oct-2026 07:00
    python run_v15.py --wip-date 2026-10-05 --wip-time 07:00 --plan-end "2026-10-15 07:00" --name v16
"""
import argparse, os, shutil, subprocess, sys
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser(description="CTP end-to-end schedule from a chosen opening-WIP date/time")
ap.add_argument("--wip-date", default="2026-10-03", help="opening-WIP day, YYYY-MM-DD (default 2026-10-03)")
ap.add_argument("--wip-time", default="07:00", help="opening-WIP time = plan start, HH:MM (default 07:00)")
ap.add_argument("--plan-end", default=None, help='plan end "YYYY-MM-DD HH:MM" (default: plan start + --days)')
ap.add_argument("--days", type=float, default=10, help="plan length in days when --plan-end is not given (default 10)")
ap.add_argument("--plan-dir", default=os.path.join(ROOT, "plan-3ct"),
                help="folder with the 4 plan files ctp_{curing,building}_schedule_*_{pcr,tbr}*.xlsx")
ap.add_argument("--name", default="v15", help="output folder name under latest_schedule/ (default v15)")
ap.add_argument("--offline", action="store_true",
                help="do NOT contact the DB: use the last downloaded tables in db_pipeline/raw")
ap.add_argument("--wip-source", choices=["old", "mix"], default="old",
                help="old (default since v16): ALL stock from jkplanning_CTP.opening_wip of --wip-date; "
                     "mix (v15): components from JK_CTP_PPDS.CTP_Opening_WIP, compounds + green tyres from the old feed")
ap.add_argument("--no-wip-mix", action="store_true",
                help="if CTP_Opening_WIP has no snapshot on the WIP day, use the old feed only "
                     "(no components) instead of components from the latest earlier snapshot")
ap.add_argument("--compare", nargs="*", default=[],
                help="version(s) to compare the daily numbers with at the end, e.g. --compare v82")
a = ap.parse_args()

start = pd.Timestamp(f"{a.wip_date} {a.wip_time}")
end = pd.Timestamp(a.plan_end) if a.plan_end else start + pd.Timedelta(days=a.days)
plan_dir = os.path.abspath(a.plan_dir)
vdir = os.path.join(ROOT, "latest_schedule", a.name)
OUT = os.path.join("latest_schedule", a.name, "run_output")
xlsx = os.path.join(vdir, f"CTP_{start:%d%b}-{end:%d%b}".upper() + f"_SCHEDULE_{a.name}.xlsx")
os.makedirs(vdir, exist_ok=True)

ENV = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
           CTP_BOM_GATE="warn", DB_OFFLINE="1",
           CTP_AGING_SOURCE="db",                                     # v34 BR-12: JK_CTP_PPDS.CTP_Master_Aging (was aging_comb.csv, v14)
           CTP_PLAN_DIR=plan_dir,
           CTP_PLAN_START=f"{start:%Y-%m-%d %H:%M}", CTP_PLAN_END=f"{end:%Y-%m-%d %H:%M}",
           CTP_WIP_DATE=a.wip_date, CTP_WIP_CUTOFF=f"{start:%Y-%m-%d %H:%M}",
           CTP_WIP_MIX="0" if a.no_wip_mix else "1",
           CTP_WIP_SOURCE="old" if a.wip_source == "old" else "ppds",
           CTP_EXPECTED_PLAN_MONTH=f"{start:%Y-%m}")

# every line of the run also goes to latest_schedule/<name>/run_log.txt (the rule reports read it);
# a line containing "password" is never written to the file
LOG = open(os.path.join(vdir, "run_log.txt"), "w", encoding="utf-8")
try:
    sys.stdout.reconfigure(errors="replace")      # a console without UTF-8 shows '?' instead of failing
except Exception:
    pass


def say(t):
    print(t, flush=True)
    if "password" not in t.lower():
        LOG.write(t + "\n")
        LOG.flush()


say(f"  {a.name}: opening WIP {a.wip_date} {a.wip_time} | plan {start} .. {end} | plans {plan_dir}\n"
    f"  DB: {'OFFLINE (db_pipeline/raw)' if a.offline else 'LIVE (db_pipeline/.env)'} | output {vdir}")

# build_sep_inputs reads the DB itself (DB_OFFLINE does not apply to it); --offline blocks the socket
BUILD = """
import sys, time, runpy, pymysql
def _no_db(*a, **k): raise ConnectionError('offline run: using db_pipeline/raw snapshot')
pymysql.connect = _no_db
time.sleep = lambda s: None
sys.argv = ['build_sep_inputs.py']
runpy.run_path('build_sep_inputs.py', run_name='__main__')
"""


def sh(*args):
    say("\n" + "#" * 74 + "\n#  " + " ".join(x if len(x) < 80 else x[:60] + "..." for x in args)
        + "\n" + "#" * 74)
    p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=ENV, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        say(line.rstrip("\n"))
    if p.wait() != 0:
        LOG.close()
        raise SystemExit(f"{args[0]} failed (exit {p.returncode})")


if a.offline:
    sh("-c", BUILD)
else:
    sh("build_sep_inputs.py")
sh("adapt_inputs.py")
sh("build_mpq_items.py")                      # v26: item-level MPQ from CTP_Master_MPQ
bak = os.path.join(ROOT, "inputs", "routing.csv.bak_biascal")
if os.path.exists(bak):
    os.remove(bak)
sh("apply_biascutter_calibration.py")
sh("apply_routing_gapfill.py")
sh("apply_ctb_approximations.py")
sh("run_scheduler.py", "--offline", "--out", OUT)
gates = os.path.join(ROOT, "outputs2", "phase5_building_echo_gates.csv")
if os.path.exists(gates):
    shutil.copy(gates, os.path.join(ROOT, OUT, "phase5_building_echo_gates.csv"))
sh("capacity_wall.py", f"{(end - start).total_seconds() / 86400:g}")
sh("audit_business_rules.py", OUT)
sh("make_schedule_v18.py", OUT, plan_dir, os.path.join("output-format", "CTP_e2e_v18_SCHEDULE.xlsx"), xlsx)
sh("mixing_compliance.py", OUT, "--xlsx", os.path.join(vdir, f"MIXING_RULE_COMPLIANCE_{a.name}.xlsx"))
sh("fourrc_checks.py", OUT, "--xlsx", os.path.join(vdir, f"FOURRC_RULES_{a.name}.xlsx"))
sh("fourrc_sequence_report.py", a.name)            # v48: 901 utilisation, idle causes, same-wire sequencing breaks
sh("quintoplex_checks.py", OUT, "--xlsx", os.path.join(vdir, f"QUINTOPLEX_RULES_{a.name}.xlsx"))
sh("quintoplex_checks.py", OUT, "--rules", "duplex_rules", "--xlsx", os.path.join(vdir, f"DUPLEX_RULES_{a.name}.xlsx"))
sh("quintoplex_checks.py", OUT, "--rules", "triplex_rules", "--xlsx", os.path.join(vdir, f"TRIPLEX_RULES_{a.name}.xlsx"))
sh("quintoplex_checks.py", OUT, "--rules", "quadraplex_rules", "--xlsx", os.path.join(vdir, f"QUADRAPLEX_RULES_{a.name}.xlsx"))
sh("validate_build_cure.py", xlsx, "--xlsx", os.path.join(vdir, f"BUILD_CURE_VALIDATION_{a.name}.xlsx"))
sh("mpq_checks.py", OUT, "--xlsx", os.path.join(vdir, f"MPQ_COMPLIANCE_{a.name}.xlsx"))
sh("general_rules_checks.py", OUT, "--xlsx", os.path.join(vdir, f"GENERAL_RULES_{a.name}.xlsx"))
sh("mixing_rule_audit.py", a.name)                     # MIXING_RULE_AUDIT_<name>.xlsx: 01-Oct mixing rules, row by row
sh("rule_adherence_report.py", a.name)                 # RULE_ADHERENCE_REPORT_<name>.xlsx: adherence % per rule / section
sh("make_version_readme.py", a.name)                   # README_<name>.md: rules in force, by section
sh("daily_numbers.py", a.name, *a.compare)             # DAILY_NUMBERS_<name>.md / .xlsx: plan vs built / cured per plant day
sh(os.path.join("backcalc", "back_calculation.py"), a.name)   # no-cutter-slitter: BACK_CALCULATION_<name>.xlsx (DB vs SMART)
say(f"\nALL STEPS DONE -> {xlsx}\n  daily numbers: {os.path.join(vdir, f'DAILY_NUMBERS_{a.name}.md')}")
LOG.close()
