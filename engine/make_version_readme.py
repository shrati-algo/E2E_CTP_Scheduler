#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_version_readme.py -- README for one schedule version: the business rules in force, by section.

    python make_version_readme.py v30                -> latest_schedule/v30/README_v30.md
    python make_version_readme.py v22 v23 ... v30    -> one README per version

RULES below is the register of every rule added since v15: section, id, what it does, the version it
came in, and the config key that switches it. A version's README lists the rules with since <= that
version ("in force"), marks the ones new in that version, and adds the run facts it can find in the
version folder (plan window, WIP, check workbooks). Add a row here for every new rule.
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

SECTIONS = ["General rules (BR-1..BR-17)", "Inputs and plan", "Mixing", "4-roll calender (901)", "Extrusion - Quintoplex PCR tread (1002)",
            "Extrusion - Duplex TBR tread (1801)", "Extrusion - Triplex PCR sidewall (1001)",
            "Extrusion - Quadraplex TBR sidewall / shoulder pad (1803)", "IL / calendering", "Upstream (all machines)",
            "MPQ (minimum production quantity)", "Building (TBM)", "Curing (presses)", "Green tyres and aging"]

# (section, id, rule, since, config key)
RULES = [
    ("General rules (BR-1..BR-17)", "BR-1", "Building plan followed shift-wise: same TBM and qty, start in the plan shift (early at most one shift); starvation-risk SKUs (feed-hours <= 8 h) built first", 32, "building_plan_feasible.pull_forward_h, building_priority"),
    ("General rules (BR-1..BR-17)", "BR-2", "Shift-start opening stock (DB snapshot at plan start); lots sized on net demand (+MPQ / batch / buffer only)", 32, "run_v15.py --wip-date / mpq_pooling"),
    ("General rules (BR-1..BR-17)", "BR-3", "Lowest building feed-hours first: time until the presses run out of the GT after GT stock decides the building order", 32, "building_priority"),
    ("General rules (BR-1..BR-17)", "BR-4", "Only approved (routing) machine / item combinations; hard block", 32, "routing (check)"),
    ("General rules (BR-1..BR-17)", "BR-5", "Item-specific line speed from routing (mixing: routing min/batch, no calibration / cap)", 32, "mixer_capacity_calibration.enabled: false"),
    ("General rules (BR-1..BR-17)", "BR-7", "Previous -> next item matrix changeover on every matrix-mapped machine; machines without a matrix line reported", 32, "machine_to_changeover_line"),
    ("General rules (BR-1..BR-17)", "BR-8", "Family batching (same item, then BOM input family, then least changeover) per plant day on all matrix-mapped upstream machines, within coverage", 32, "family_batching"),
    ("General rules (BR-1..BR-17)", "BR-9", "Same item not split into several blocks in one shift on any upstream machine (except 4RC), unless a consumer would starve", 32, "business_rules.same_item_shift_merge.machines: [ALL]"),
    ("General rules (BR-1..BR-17)", "BR-10", "No production inside PM / MTC windows; available minutes exclude them (cutter shear change: no data)", 32, "downtime_reason"),
    ("General rules (BR-1..BR-17)", "BR-12", "Aging hard block: upstream lots consume only input alive at their start (min..max aging, FEFO), else wait for fresh supply / flagged; building waits for an aging-valid kit; green tyres > 72 h scrapped", 33, "aging_hard_block"),
    ("General rules (BR-1..BR-17)", "BR-14", "No item change in the first / last 30 min of a shift (07:00 / 15:00 / 23:00) on upstream machines", 33, "shift_handover"),
    ("General rules (BR-1..BR-17)", "BR-15", "Replan each shift: run_shift_refresh.py with the shift-start WIP snapshot, live masters / downtime / latest plans", 33, "run_shift_refresh.py"),
    ("General rules (BR-1..BR-17)", "BR-16", "Similar urgency -> fewer changeovers, longer runs: family passes (no lot made late first), MPQ pooling, 4RC spool runs; reported", 33, "family_batching / *_rules"),
    ("General rules (BR-1..BR-17)", "BR-17", "Latest plant-approved masters: live JK_CTP_PPDS each run, current rows only, data stamp + overrides in the README", 33, "build_sep_inputs.py (live)"),
    ("General rules (BR-1..BR-17)", "BR-12 strict", "Aging limits from JK_CTP_PPDS.CTP_Master_Aging (Hours/Days/Month=30 d/Min), green tyres 72 h (user); hard block on every consumption, no remake", 34, "run_v15.py CTP_AGING_SOURCE=db, aging_hard_block"),
    ("IL / calendering", "BR-37", "IL made to net building need after opening IL stock; one lot covers <= 24 h of building need (no excess / early IL)", 34, "mpq_pooling.max_span_h_itypes"),
    ("IL / calendering", "BR-38", "IL codes only on their routed machine (1101 / 1901 / 2201)", 34, "routing (check)"),
    ("IL / calendering", "BR-39", "Code-specific IL line speed from routing; Expansion Work Out conversion: no data (BOM used)", 34, "routing (check)"),
    ("IL / calendering", "BR-41", "Per plant day: TBR RHC type blocks (TIL / SQ / EG) then compound groups; PCR RHC and 2201 compound groups together", 34, "family_batching 1901 / 1101 / 2201"),
    ("4-roll calender (901)", "4RC-7", "Each item made in ONE GO per plant day (07:00 -> 07:00)", 34, "fourrc_rules.day_plan"),
    ("4-roll calender (901)", "4RC-8", "Spool / dip roll (CTP_Master_FRC, per wire code) ENTIRELY consumed before the cord / dip change; last lot lengthened, extra to stock", 34, "fourrc_rules.day_plan"),
    ("4-roll calender (901)", "4RC-9", "Least changeover: same wire together, steel <-> fabric only between wire blocks, fabric fills the creel reload", 34, "fourrc_rules.day_plan"),
    ("Inputs and plan", "GEN-02", "Opening WIP from the live DB snapshot of the plan start day (07:00); plant day 07:00 -> 07:00", 15, "run_v15.py --wip-date / --wip-time"),
    ("Inputs and plan", "GEN-01", "Plan window cut from the plant plan files (building + curing, PCR + TBR) to --plan-end", 15, "run_v15.py --plan-end"),
    ("Inputs and plan", "V22", "Plan files from latest_plan_file; all JK_CTP_PPDS masters read LIVE each run (BOM, routing, changeover matrix, MPQ, FRC, WIP)", 22, "--plan-dir latest_plan_file (no --offline)"),
    ("Mixing", "BR-V1", "Mixing planned in whole batches; batch size = routing batch_size (kg)", 15, "business_rules.mixing_output_uom"),
    ("Mixing", "BRM-01", "Mixing lot >= 3 batches (MPQ) x the compound's routing batch weight", 15, "mixing_mpq_min_batches"),
    ("Mixing", "BRM-13", "+5 buffer batches on each compound's last lot (not special / sticky compounds)", 15, "business_rules.buffer_batches"),
    ("Mixing", "BRM-10 / BR-74", "Compound changeover on a mixer = 60 s", 15, "mixer_changeover_min"),
    ("Mixing", "BRM-06", "Master <-> final switch on one mixer: TR100 clean-out 5 min", 15, "business_rules.master_final_cleanout_min"),
    ("Mixing", "BRM-02", "Sticky compounds (JT444, JT466) in stacks of 3 batches", 15, "business_rules.sticky_compounds"),
    ("Mixing", "BR-V2 / BR-V13", "Butyl sequence: final mixer B1/B2/B99 before 30G/IL3/IL5 (else clean-out); master: no clean-out, 60 s changeover only", 15, "business_rules.butyl_sequence_rule"),
    ("Mixing", "BRM-09", "Same compound once per (mixer, shift): split runs joined", 18, "business_rules.same_item_shift_merge"),
    ("Mixing", "3.1.3 / BRM-07", "IL3/IL5 in ONE campaign per plant day (07:00 -> 07:00) on F270, B99 directly in front", 20, "il_daily_campaign.once_per_plant_day"),
    ("Mixing", "BUTYL-120", "Butyl clean-out fixed 120 s for all runs", 28, "business_rules.butyl_sequence_rule.cleanout_sec"),
    ("Mixing", "MIX-CAP", "NO mixer batch cap (user, from v32 and every run after): no 230/145 calibration, no 220 master / 160 final limit; mixer time = routing min/batch as-is", 28, "mixer_capacity_calibration.enabled: false"),
    ("Mixing", "BR-V2 30G", "30G run placed with a real B99 lot (or a B99 prep lot) directly in front, like IL3/IL5", 29, "butyl_prep_campaign"),
    ("4-roll calender (901)", "4RC-2", "CAP 66 slitted material bypassed: mother roll -> B616M + fabric, no slitted lots", 21, "fourrc_rules.bypass_cap66_slitted"),
    ("4-roll calender (901)", "4RC-3", "Capacity: steel and fabric minutes per plant day vs available, separately and combined (report)", 21, "fourrc_rules.capacity_check"),
    ("4-roll calender (901)", "4RC-4", "Run time = metres / (routing proc_time x efficiency) for every 901 lot", 21, "fourrc_rules.routing_speed_only"),
    ("4-roll calender (901)", "4RC-5", "One creel: new steel cord >= 240 min after the old cord, reload in parallel (fabric fills the window), not stacked on the changeover", 21, "fourrc_rules.creel"),
    ("4-roll calender (901)", "4RC-6", "Roll lengths (CTP_Master_FRC): run <= one spool / dip length, made at once; min roll 300 m; no residual < 300 m; fewest runs", 27, "fourrc_rules.roll_lengths"),
    ("Extrusion - Quintoplex PCR tread (1002)", "BR-18", "Quintoplex only for routing-approved tread / sidewall codes", 24, "routing (check)"),
    ("Extrusion - Quintoplex PCR tread (1002)", "BR-19", "Tread-code-specific line speed (routing M/MIN x efficiency)", 24, "routing (check)"),
    ("Extrusion - Quintoplex PCR tread (1002)", "BR-20", "Changeover = matrix class A-H time (4.54 - 60 min) from item -> to item", 24, "changeover matrix 'Quintoplex PCR Tread'"),
    ("Extrusion - Quintoplex PCR tread (1002)", "BR-21", "Per plant day: same die and compatible compound family together, least changeover, within coverage (24 h window from v25)", 24, "quintoplex_rules"),
    ("Extrusion - Duplex TBR tread (1801)", "BR-22", "Duplex only for routing-approved TBR treads", 25, "routing (check)"),
    ("Extrusion - Duplex TBR tread (1801)", "BR-23", "Tread-code-specific line speed (routing M/MIN x efficiency)", 25, "routing (check)"),
    ("Extrusion - Duplex TBR tread (1801)", "BR-24", "Matrix: die 4.76, die+preformer 4.92, compound 12.5 / 14 min; pair missing in the matrix = 14 min", 25, "changeover_missing_pair_min"),
    ("Extrusion - Duplex TBR tread (1801)", "BR-25", "Per plant day: one BOM compound family (T636 / T639) at a stretch, then least changeover", 25, "duplex_rules"),
    ("Extrusion - Triplex PCR sidewall (1001)", "BR-26", "Triplex only for routing-approved sidewall codes", 30, "routing (check)"),
    ("Extrusion - Triplex PCR sidewall (1001)", "BR-27", "Sidewall-code-specific line speed (routing M/MIN x efficiency)", 30, "routing (check)"),
    ("Extrusion - Triplex PCR sidewall (1001)", "BR-28", "Matrix: die+preformer 3 min, compound/barrel/flow channel 28 min (missing pair = 28); per plant day each compound family (full BOM compound set) at a stretch -> fewest compound changes", 30, "triplex_rules"),
    ("Extrusion - Quadraplex TBR sidewall / shoulder pad (1803)", "BR-29", "Quadraplex only for routing-approved TBR sidewall / shoulder-pad items", 31, "routing (check)"),
    ("Extrusion - Quadraplex TBR sidewall / shoulder pad (1803)", "BR-30", "Item-specific Quadraplex line speed (routing M/MIN x efficiency)", 31, "routing (check)"),
    ("Extrusion - Quadraplex TBR sidewall / shoulder pad (1803)", "BR-31", "Matrix: die 2, die+preformer 3, sidewall->shoulder pad 36, shoulder pad->sidewall 34, shoulder-pad compound 15 min; missing pair 36 (SW<->SP) / 15", 31, "changeover_missing_pair_by_type"),
    ("Extrusion - Quadraplex TBR sidewall / shoulder pad (1803)", "BR-32", "Per plant day all sidewalls in one block and all shoulder pads in one block: the 34-36 min SW<->SP changeover at most once a day; day starts with the group already running", 31, "quadraplex_rules"),
    ("Dispatch (all sections)", "CD-1", "Cover first: cover_h = (stock + aged WIP) / consumption rate (moulds x cure rate per mould; upstream: the consumers' rate). A SKU / item under its threshold is urgent and runs next, lowest cover first. Threshold: building 8 h (one shift; 90th-pct restock 5.6 h PCR / 4.6 h TBR + margin); upstream item = matrix changeover + lot run time (routing) + min aging (CTP_Master_Aging) + transfer + 2 h", 35, "cover_dispatch"),
    ("Dispatch (all sections)", "CD-2", "Equal cover: the SKU / item feeding FEWER moulds goes first (user, 2026-10-06)", 35, "cover_dispatch"),
    ("Dispatch (all sections)", "CD-3", "No urgent item: same item continues, then the same compound family, then the cheapest changeover in CTP_Master_ChangeoverMatrix (least minutes), then the next cheapest; a run breaks only when another item becomes urgent, at a lot / MPQ / spool end; grouping may cross plant days within shelf life", 35, "cover_dispatch"),
    ("Upstream (all machines)", "BR-7/10", "Final repair: every upstream lot after its inputs + min aging + transfer time, changeovers kept, PM/MTC windows blocked", 15, "final_repair_pass"),
    ("Upstream (all machines)", "BOTTLENECK", "Saturated machines (mixers, 1802, bead apex, 1803) start when inputs are ready, limited only by shelf life", 25, "bottleneck_release"),
    ("MPQ (minimum production quantity)", "MPQ-DB", "Every CTP_Master_MPQ row (PCR + TBR) mapped to its items: tread by rim size, calendar roll spool / dipped cord, chaffer, STC, BRTTL, white SW, edge strip / gum (gauge not in DB -> smallest)", 26, "mpq_item_level"),
    ("MPQ (minimum production quantity)", "MPQ-POOL", "Small same-item demand pooled up to the MPQ within shelf life before the floor (no per-lot over-production)", 26, "mpq_pooling"),
    ("Building (TBM)", "BLD-ECHO", "Building follows the plant building plan: same TBM, same quantity, later only for kit readiness", 15, "building_plan_echo"),
    ("Building (TBM)", "B1", "A SKU builds only on TBMs its GT is routed to", 23, "routing (check)"),
    ("Building (TBM)", "B2", "Build time = qty x routing cycle / efficiency; every GT change charged the matrix different-size time (TBR 24, PCR 60)", 23, "capacity_rules_v23.building"),
    ("Building (TBM)", "BR-V7", "Idle TBM may start its own ready plan row up to 24 h early", 25, "building_plan_feasible"),
    ("Building (TBM)", "C4", "No build block starts before its material physically exists (material ledger)", 15, "echo_kit_ledger"),
    ("Curing (presses)", "CUR-ECHO", "Curing follows the plant curing plan (pinned press echo)", 15, "phase6"),
    ("Curing (presses)", "C1", "A SKU cures only on its routing presses", 23, "routing (check)"),
    ("Curing (presses)", "C2", "q_shift = tyres a press cures in a shift = floor(2 x 480 / (routing cure + allowance)); block <= q_shift, press time = qty x 480 / q_shift", 23, "capacity_rules_v23.curing"),
    ("Green tyres and aging", "AGE", "Component aging: min / max aging from the aging master, transfer time from routing added to min aging", 15, "aging master"),
    ("Green tyres and aging", "AGE-DB", "Aging min / max from JK_CTP_PPDS.CTP_Master_Aging (Hours / Days / Month = 720 h / Min), green tyres 72 h (user); applied as in v26: inputs + min aging + transfer before use, max aging on build kits, over-age upstream use reported (no BR-12 hard block)", 36, "run_v15.py CTP_AGING_SOURCE=db"),
    ("4-roll calender (901)", "4RC-1..3 v38", "Applied completely: every plant day in one-go / wire-block / least-changeover order (fabric between steel cords); every run of a wire / dip code made in WHOLE spools of its Spool/Dip Length (CTP_Master_FRC by item_code), top-up spread over the run's lots, each <= one spool (4RC-6 kept), extra to stock; 1 m tolerance", 38, "fourrc_rules.day_plan.mandatory"),
    ("Mixing", "BRM-09 v38", "Same compound once per (mixer, shift) applied completely: after the strict rounds the remaining splits are joined with a consumer delay <= 8 h and the repair re-times the later lots", 38, "same_item_shift_merge.post_repair_relaxed_h"),
    ("Mixing", "BUTYL-DAY v39", "ONE butyl block per plant day (07:00 -> 07:00) per group, butyl + butyl-friendly together: final (F270) B99 -> IL3 -> 120 s -> B99 -> IL5 -> 120 s -> B99 -> 30G -> 120 s; master (K430-2) friendly -> 29MG -> 120 s -> friendly -> ML405 -> 120 s -> friendly -> ML605 -> 120 s -> friendly; real friendly lots first, else 5-batch prep lots; IL3 / IL5 each in one go", 39, "butyl_day_block"),
    ("4-roll calender (901)", "4RC rule 3 v40", "Least EFFECTIVE changeover: steel cord change = matrix minutes + idle 240 min creel reload, so steel -> fabric -> steel is preferred; after the last repair every steel cord change without fabric between gets that plant day's fabric run moved into the reload window (whole wire run, inputs ready, one run per item per day kept)", 40, "fourrc_rules.fabric_bridge"),
    ("Mixing", "IL-CAMPAIGN v41", "IL3 and IL5 made together in the final mixer in ONE go once a plant day: B99 (butyl-friendly) -> IL3 -> IL5 back to back (plain changeover, no clean-out between), 120 s clean-out after the campaign; 30G keeps its own B99 in front", 41, "butyl_day_block.groups.final.campaigns"),
    ("4-roll calender (901)", "4RC wire campaign v42", "CTP_Master_FRC wire_type = one creel load / dip roll: items sharing a wire run together in whole Spool/Dip Length, cheapest CTP_Master_ChangeoverMatrix order inside, a load may take the next 3 days' lots of its wire within max aging; creel reload (240 min) only when steel items change wire_type; changeover type / minutes from the matrix; reload windows filled with fabric (next day's dip within shelf life)", 42, "fourrc_rules.creel_source / wire_campaign"),
    ("4-roll calender (901)", "4RC level loading v43", "901 output 30-35 km per plant day: a light day pulls the next 3 days' lots forward (inputs ready, within max aging - 4 h), never above 35 km, work only moves earlier; mixers feed 901 first (skim compounds + masters of every 901 lot due 24 h earlier)", 43, "fourrc_rules.level_loading, protect_bottleneck_feed.machines"),
    ("4-roll calender (901)", "4RC deep roll v44", "Dipped-fabric items in FULL dips (CTP_Master_FRC Spool/Dip Length, 2,000 m for most): 48 h need >= one dip -> floor(need / dip) dips (4,400 m -> 2 dips); 48 h need < one dip -> extended to 72 h and decided again; never a partial dip, dip stock covers the next demand first", 44, "fourrc_rules.deep_roll"),
    ("4-roll calender (901)", "4RC level plan v44", "Option C: band = average daily 901 need +- 5 km (made metres, whole spools / dips); 901 planned FIRST (made metres = whole Spool/Dip Length per FRC wire): peaks moved to earlier days, light days pull the next 3 days' lots, always within max aging - 4 h; each lot gets a target start and its feeding compounds (skim final, master, re-mill) are back-scheduled from it (start - min aging - transfer) so the mixers make them in time", 44, "fourrc_rules.level_plan"),
    ("Mixing", "MIX-1MIXER v47", "One compound is planned on only ONE mixer per plant day, never split across mixers: each compound gets one routing-eligible mixer (butyl / butyl-friendly on their block mixer, the rest balanced by run time)", 47, "mixing_v47.one_mixer_per_day"),
    ("Mixing", "MIX-GAP v47", "One run makes at once at least the next 15 h of demand (24 h for IL3 / IL5 and the 4RC compounds) and the same compound is not started again within 15 h / 24 h of its previous run's start; butyl-friendly B99 / B1 / B2 / MB100 / MB200 / XMB98 exempt", 47, "mixing_v47.repeat_gap"),
    ("Mixing", "MIX-CATCHUP v48", "A late compound window that is already due joins the current run of its compound on the same mixer (one bigger run) instead of waiting the 15 h / 24 h gap -- inputs ready and within shelf life at its last use", 48, "mixing_v47.catch_up_merge"),
    ("4-roll calender (901)", "4RC ready-first v48", "901 re-planned after the repair WITHOUT the level band: whenever it is free it runs a READY lot (same item, same CTP_Master_FRC wire, fabric during a creel reload, earliest need); a lot whose compound is missing is skipped, not waited for; FOURRC_SEQUENCE_REPORT lists idle time and same-wire sequencing breaks with reasons", 48, "fourrc_rules.replan_after_repair"),
    ("General rules", "QUEUE-UNBLOCK v49", "No lot waits behind a lot whose input is missing: on 901 and the belt cutters (802, 801) lots keep their planned order, but when the next lot's input is not ready within 30 min the machine runs the next READY lot (same item first; 901 creel 240 min kept and filled with fabric / same wire)", 49, "queue_unblock"),
    ("Mixing", "MIX-1MIXER-DAY v49", "Rule A as stated: one compound on ONE mixer per PLANT DAY (07:00-07:00), chosen at placement by load -- the mixer may differ between days (v47 / v48 fixed one mixer for the whole plan); butyl-group compounds stay on their block mixer", 49, "mixing_v47.one_mixer_mode"),
    ("Mixing", "MIX-PLACE v53", "08-Oct mixing rules applied AT PLACEMENT: a compound lot joins its compound's run (directly behind it, same mixer) or starts >= 15 h / 24 h after any run of it on any mixer; one mixer per compound per plant day chosen at placement", 53, "mixing_v47.placement_aware"),
    ("General rules", "REPAIR-COMPACT v53", "The final repair may move a lot back towards its placement start (not only later): machine order, changeovers, inputs, PM, creel and shelf life at the latest consumer kept; build rows likewise -- removes the idle holes the later-only repair left on the mixers", 53, "repair_compaction"),
    ("Mixing", "MIX-PLANNER v56", "Stock-coverage mixing planner: all compound lots planned first on the mixers (forward, needed-soonest first, masters ahead of finals); runs cover 15 h / 24 h (IL / 4RC), separate lots back to back, no changeover, no repeat within that time, one mixer per plant day, daily butyl / IL blocks with friendly + clean-outs; the repair never moves mixing lots", 56, "mix_planner"),
    ("Mixing", "MIX-FEED v47", "Final mixers target ~230 batches per shift (report, no cap): master compounds made early enough that every final lot can start from its own shelf-life floor (last use - (max aging - 2 h))", 47, "mixing_v47.final_feed"),
    ("4-roll calender (901)", "4RC re-plan v45", "901 re-planned after the final repair on the real input-ready times: whenever the calender is free it runs the best READY lot (same item, same FRC wire, fabric during a creel reload, earliest need) up to the level band per day -- no lot waits behind another lot's compound; deep-roll decisions made as one lot per dip (4RC-6)", 45, "fourrc_rules.replan_after_repair"),
    ("4-roll calender (901)", "4RC deep roll v46", "Deep Roll quantity = the HIGHER of the 48 h requirement and the requirement inside the item's max-aging window (CTP_Master_Aging), in FULL dips only (floor, at least 1): 1.4 vs 2.1 -> 2 dips, 1.4 vs 1.8 -> 1 dip", 46, "fourrc_rules.deep_roll.mode"),
    ("Inputs and plan", "EFF-1", "Efficiency taken as 1.0 for every routing row (user): upstream / mixing run time = routing time, build time = routing cycle; was routing 0.96 (cap ply slitter 0.78); curing unchanged", 37, "efficiency_override: 1.0"),
    ("Green tyres and aging", "GT-72", "Green tyre shelf life 72 h; tyres past it are scrapped, never cured over age", 19, "green_tyre_scrap_expired"),
]

CHECKS = [("BUILD_CURE_VALIDATION", "Building / curing eligibility and capacity, TBM use by day"),
          ("MIXING_RULE_COMPLIANCE", "Mixing rules (incl. BR-V2 prep in front)"),
          ("MPQ_COMPLIANCE", "Lots vs CTP_Master_MPQ, over-production"),
          ("FOURRC_RULES", "4-roll calender rules 2-6"),
          ("QUINTOPLEX_RULES", "BR-18..21"), ("DUPLEX_RULES", "BR-22..25"), ("TRIPLEX_RULES", "BR-26..28"), ("QUADRAPLEX_RULES", "BR-29..32"), ("GENERAL_RULES", "BR-1..BR-10")]


def _data_stamp(n):
    """BR-17: the master data this run read (db_pipeline/raw = the run's live download) + overrides + data gaps."""
    if n < 33:
        return []
    import pandas as pd
    import yaml
    out = ["## Master data used (BR-17)", "", "| Table | Rows | Latest change in the table |", "|---|--:|---|"]
    for f in sorted(glob.glob(os.path.join(HERE, "db_pipeline", "raw", "JK_CTP_PPDS__*.csv"))):
        try:
            d = pd.read_csv(f, low_memory=False)
            if "is_current" in d.columns:
                d = d[pd.to_numeric(d["is_current"], errors="coerce").fillna(1) == 1]
            tc = [c for c in d.columns if c.lower() in ("updatedat", "updated_at", "valid_from", "inventory_date")]
            last = max((str(pd.to_datetime(d[c], errors="coerce").max()) for c in tc), default="-")
            out.append(f"| {os.path.basename(f)[13:-4]} | {len(d):,} | {last} |")
        except Exception:
            continue
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    ov = [("Green tyre shelf life", f"{cfg.get('green_tyre_max_age_h', 72)} h", "user"),
          ("Mixer capacity calibration", "on" if (cfg.get("mixer_capacity_calibration") or {}).get("enabled") else "off (routing)", "user"),
          ("Butyl clean-out", f"{((cfg.get('business_rules') or {}).get('butyl_sequence_rule') or {}).get('cleanout_sec')} s", "user"),
          ("Missing matrix pairs", str(cfg.get("changeover_missing_pair_min")) + " / " + str(cfg.get("changeover_missing_pair_by_type")), "user"),
          ("Edge strip / gum gauge", "smallest DB value (gauge not in DB)", "pending plant"),
          ("Building changeover", "different-size time on every GT change (v23)", "user; BJ/CONTI pending")]
    out += ["", "**Values not taken from a plant master (overrides):**", "", "| Setting | Value | Approved by |", "|---|---|---|"]
    out += [f"| {a} | {b} | {c} |" for a, b, c in ov]
    out += [""]
    return out


def _off_rules():
    """v36+: rules whose switch is OFF in config.yaml are not 'in force' (user: v36 = v26 rules + v27-v31)."""
    import yaml
    c = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
    on = lambda k: bool((c.get(k) or {}).get("enabled", False))
    sw = {"BR-1": on("building_priority"), "BR-3": on("building_priority"), "BR-8": on("family_batching"),
          "BR-16": on("family_batching"), "BR-41": on("family_batching"),
          "BR-9": bool(((c.get("business_rules") or {}).get("same_item_shift_merge") or {}).get("machines")),
          "BR-12": on("aging_hard_block"), "BR-12 strict": on("aging_hard_block"), "BR-14": on("shift_handover"),
          "BR-37": bool((c.get("mpq_pooling") or {}).get("max_span_h_itypes")),
          "4RC-7": bool(((c.get("fourrc_rules") or {}).get("day_plan") or {}).get("enabled")),
          "CD-1": on("cover_dispatch"), "CD-2": on("cover_dispatch"), "CD-3": on("cover_dispatch")}
    sw["4RC-8"] = sw["4RC-9"] = sw["4RC-7"]
    if not ((c.get("fourrc_rules") or {}).get("day_plan") or {}).get("whole_spool_topup", True):
        sw["4RC-8"] = False                           # v76 (user): 4RC makes demand metres only -> whole spools not enforced
    _f = c.get("fourrc_rules") or {}                  # v47: v43-v46 4RC rules switched off (back to v42)
    sw["4RC level loading v43"] = bool((_f.get("level_loading") or {}).get("enabled"))
    sw["4RC level plan v44"] = bool((_f.get("level_plan") or {}).get("enabled"))
    sw["4RC deep roll v44"] = sw["4RC deep roll v46"] = bool((_f.get("deep_roll") or {}).get("enabled"))
    sw["4RC re-plan v45"] = bool(_f.get("replan_after_repair"))
    _m = c.get("mixing_v47") or {}
    sw["MIX-1MIXER v47"] = bool(_m.get("enabled") and _m.get("one_mixer_per_day", True))
    sw["MIX-GAP v47"] = bool(_m.get("enabled") and _m.get("repeat_gap", True))
    sw["MIX-CATCHUP v48"] = bool(_m.get("enabled") and _m.get("catch_up_merge"))
    sw["4RC ready-first v48"] = sw["4RC re-plan v45"]
    sw["QUEUE-UNBLOCK v49"] = bool((c.get("queue_unblock") or {}).get("enabled"))
    sw["MIX-1MIXER-DAY v49"] = bool(_m.get("enabled") and str(_m.get("one_mixer_mode", "plan")).lower() == "day")
    sw["MIX-PLACE v53"] = bool(_m.get("enabled") and _m.get("placement_aware"))
    sw["REPAIR-COMPACT v53"] = bool((c.get("repair_compaction") or {}).get("enabled"))
    sw["MIX-PLANNER v56"] = bool((c.get("mix_planner") or {}).get("enabled"))
    sw["MIX-FEED v47"] = bool(_m.get("enabled") and (_m.get("final_feed") or {}).get("enabled"))
    return {k for k, v in sw.items() if not v}


def readme(v):
    _dg = re.fullmatch(r"v(\d+)", v)
    n = int(_dg.group(1)) if _dg else max(r[3] for r in RULES)   # experiment / shift run: all rules in force
    vdir = os.path.join(HERE, "latest_schedule", v)
    if not os.path.isdir(vdir):
        print(f"  skip {v}: no folder")
        return
    head = ""
    lp = os.path.join(vdir, "run_log.txt")
    if os.path.exists(lp):
        with open(lp, encoding="utf-8", errors="replace") as fh:
            head = fh.readline().strip()
    off = _off_rules() if n >= 36 else set()
    rules_all = RULES
    RULES_ = [r for r in rules_all if r[1] not in off]
    new = [r for r in RULES_ if r[3] == n]
    L = [f"# Schedule {v} -- business rules in force", ""]
    if head:
        L += [f"**Run:** `{head}`", ""]
    L += ["## New in this version", ""]
    L += ([f"- **{r[0]} -- {r[1]}:** {r[2]} (`{r[4]}`)" for r in new] or ["- No new rule (same rules, new data or settings)."])
    if off:
        L += ["", "## Switched off in this version (config)", ""]
        L += [f"- **{r[1]}** ({r[0]}): {r[2]} (`{r[4]}`)" for r in rules_all if r[1] in off and r[3] <= n]
    L += ["", "## All rules in force, by section", ""]
    for sec in SECTIONS:
        rs = [r for r in RULES_ if r[0] == sec and r[3] <= n]
        if not rs:
            continue
        L += [f"### {sec}", "", "| Rule | What it does | Since | Config |", "|---|---|---|---|"]
        L += [f"| {r[1]}{' **(new)**' if r[3] == n else ''} | {r[2]} | v{r[3]} | `{r[4]}` |" for r in rs]
        L += [""]
    L += _data_stamp(n)
    files = [os.path.basename(f) for f in sorted(glob.glob(os.path.join(vdir, "*.xlsx")))]
    L += ["## Files in this folder", ""]
    for f in files:
        desc = next((d for k, d in CHECKS if f.startswith(k)), "schedule" if "SCHEDULE" in f else "")
        L += [f"- `{f}`" + (f" -- {desc}" if desc else "")]
    L += ["", "Full history and results: `latest_schedule/BUSINESS_RULES_LOG.md`.", ""]
    out = os.path.join(vdir, f"README_{v}.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"  wrote {out} ({len(new)} new rules, {sum(1 for r in RULES_ if r[3] <= n)} in force, {len(off)} switched off)")


if __name__ == "__main__":
    for v in sys.argv[1:]:
        readme(v)
