# -*- coding: utf-8 -*-
"""
table_map.py — which jkplanning_CTP table feeds which scheduler input.

Verified against the LIVE database 2026-07-23 (MySQL 9.3.0, 303 tables) and against
`jkplanning_CTP_Data_Dictionary 1 1.xlsx`. Row counts below are the live counts.

THE POINT OF THIS FILE: the scheduler must never guess a table name. Every input the
pipeline needs is declared here once, with the exact DB table and what it replaces.
"""

# ── INPUTS THE SCHEDULER ALREADY USES ────────────────────────────────────────────
# key = the scheduler's config `files:` key   ->   the DB table that supplies it.
# `excel` = the ctp_inputfiles workbook this REPLACES (for provenance / diffing).
CORE = {
    "bom": {
        "table": "jkt_bom_pcr", "rows": 42580,
        "excel": "jkt_bom_pcr 15.xlsx",
        "note": "Columns are IDENTICAL to the workbook (Super_parent..child_description).",
    },
    "routing": {
        "table": "jkt_routing_pcr", "rows": 14752,
        "excel": "jkt_routing_pcr 20.xlsx",
        "note": "21 cols incl. proc_time/proc_time_UOM/batch_size/transfer_time_min/efficiency "
                "-- the columns CTP deviations #1/#2/#6b depend on.",
    },
    "itemtype_master": {
        "table": "jkt_itemType_master_pcr", "rows": 2862,
        "excel": "jkt_itemType_master_pcr.csv",
        "note": "ItemCode, ItemType. build_mpq_map keys on ItemType -- exact strings matter.",
    },
    "aging_master": {
        "table": "jkt_aging_master_pcr", "rows": 2656,
        "excel": "jkt_aging_master_pcr 1.xlsx",
        "note": "ItemCode, MaxAging, MinAging, MaxAgingUnit, MinAgingUnit, Equipment.",
    },
    "mpq": {
        "table": "jkt_mpq_pcr", "rows": 22,
        "excel": "jkt_mpq.xlsx (sheet 'mpq')",
        "note": "Item_Type, Parameters, Minimum_Run_Qty, Maximum_Run_Qty, UOM -- EXACTLY the "
                "'mpq' sheet. NOTE: the DB has NO 'mixing mpq' equivalent (per-mixer sheet is "
                "workbook-only), and Maximum_Run_Qty is blank -> MHE stays inert.",
    },
    "buffer_master": {
        "table": "jkt_buffer_master_pcr", "rows": 18,
        "excel": "jkt_buffer_master_pcr.csv",
        "note": "'Item type', 'Buffer Level (Hrs)'.",
    },
    "inventory": {
        "table": "opening_wip_pcr", "rows": 39854,
        "excel": "opening_wip.csv",
        "note": "production_id, produced_time, itemcode, item_type, unit, inventory (+aging). "
                "This is the opening-WIP snapshot phase1c nets against.",
    },
    "changeover": {
        "table": "jkt_changeover_matrix_combined", "rows": 210984,
        "excel": "jkt_changeover_matrix_combined (1).xlsx",
        "note": "machine, from/to_MaterialCode_O, changeover_letter, changeover_time_min, type.",
    },
    "plan": {
        "table": "CuringSchedule_pcr", "rows": 6732,
        "excel": "ctppcr_plan_shiftsheet.xlsx",
        "note": "THE DEMAND DRUM. date, shift, machineName, sizeCode, startTime, endTime, "
                "scheduleQuantity, remarks -- the shift-level curing schedule. NOT `Plan_pcr` "
                "(266 rows), which is a size-level volume plan, a different thing.",
    },
}

# ── AVAILABLE IN THE DB BUT NOT WIRED IN TODAY ───────────────────────────────────
# config.yaml currently declares several of these "NOT PROVIDED by CTP". They EXIST.
EXTRA = {
    "building_cycle_times": {
        "table": "jkt_building_cycle_times_pcr", "rows": 245,
        "note": "SKU code, Stage, MG, cycle_sec. config.yaml says 'NOT PROVIDED -- no per-machine "
                "cycle times; routing duration is used'. IT EXISTS. Wiring it would give building "
                "real per-SKU cycle times.",
    },
    "building_changeover": {
        "table": "Master_Building_ChangeoverTime_pcr", "rows": 11,
        "note": "Same/Different-size changeover minutes per building machine. config.yaml says "
                "'NOT PROVIDED -- no per-machine building setup table'. IT EXISTS.",
    },
    "building_allowable_machines": {
        "table": "Master_Building_Allowable_Machines_pcr", "rows": 223,
        "note": "SKUCode -> list of building machines that CAN build it. Directly relevant to the "
                "single-machine-pinning makespan problem: the routing pins some green tyres to 1 "
                "TBM while this table may allow several.",
    },
    "curing_allowable_machines": {
        "table": "Master_Curing_Allowable_Machines_pcr", "rows": 300,
        "note": "SKUCode -> list of curing presses that can run it.",
    },
    "curing_design_cycletime": {
        "table": "Master_Curing_Design_CycleTime_pcr", "rows": 248,
        "note": "Designed cure time per SKU (description, size, Cure Time, Sapcode).",
    },
    "wc_master": {
        "table": "Master_WC_Master", "rows": 290,
        "note": "wcID -> WCNAME/Description. Machine-name resolution (e.g. the 270F/F270 question).",
    },
    "running_moulds": {
        "table": "current_running_moulds_pcr", "rows": 88,
        "note": "WCNAME, Current MouldNo, SKUCode, Mould life -- warm-start state for curing.",
    },
    "gt_inventory": {
        "table": "gt_inventory_manual_pcr", "rows": 28,
        "note": "Opening GREEN TYRE inventory -- relevant to the cold-start/warm-start gap.",
    },
    "mould_sku_map": {
        "table": "Master_Mapping_Mould_SKU", "rows": 2208,
        "note": "Mould -> SKU mapping (capacity bound work).",
    },
}

ALL = {**CORE, **EXTRA}

# adapt_inputs uses config's `ctp_files` keys, which are shorter than the phases' `files`
# keys. Alias them so BOTH layers resolve to the same table instead of silently falling
# back to the workbook (that fallback is exactly how the itemtype gap-fill was nearly lost).
ALIASES = {
    "itemtype": "itemtype_master",
    "aging": "aging_master",
    "buffer": "buffer_master",
    "changeover_matrix": "changeover",
}
for _a, _t in ALIASES.items():
    ALL.setdefault(_a, ALL[_t])


def for_product(table: str, product: str = "pcr") -> str:
    """Swap a `_pcr` table to `_<product>` (tbr). Shared tables (combined / WC / Mould) keep as-is."""
    if product and product != "pcr" and str(table).endswith("_pcr"):
        return table[:-4] + "_" + product
    return table


def table_for(key: str, product: str = "pcr") -> str:
    """Scheduler input key -> DB table name (product-aware). Raises loudly on an unknown key."""
    if key not in ALL:
        raise KeyError(f"No DB table mapped for input {key!r}. "
                       f"Known: {sorted(ALL)}")
    return for_product(ALL[key]["table"], product)


def core_keys():
    return list(CORE)


if __name__ == "__main__":
    print(f"{'input key':<30}{'DB table':<46}{'rows':>10}")
    print("-" * 88)
    for k, v in CORE.items():
        print(f"{k:<30}{v['table']:<46}{v['rows']:>10,}")
    print("\n-- available but NOT wired in --")
    for k, v in EXTRA.items():
        print(f"{k:<30}{v['table']:<46}{v['rows']:>10,}")
