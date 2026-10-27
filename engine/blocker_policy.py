"""blocker_policy.py — v39 DATA-BLOCKER POLICY: one catalogue, used everywhere.

Every SKU (and every cure lot) is in exactly one state:

  CLEAN         every master record the schedule needs exists
  ASSUMED       a record is missing and a STATED assumption (A-code) fills it;
                counted in the final figures, flagged, share shown separately
  DATA_BLOCKED  a record is missing that cannot be assumed (B-code); the demand
                is listed in VALIDATION_REPORT and taken OUT of the final
                figures - out of the plan AND out of the cured count

The state is decided in phase 0 (phase0_sku_status.csv), carried onto every
cure lot in phase 7, and summarised by make_validation_report.py.

Rule: an assumption is allowed only when the stand-in value comes from the
plant's own data and the error it can cause is timing, not "can this tyre be
made at all". Anything with no such stand-in is a blocker. A run never invents
a value with no source.
"""
from __future__ import annotations

STATUS_FILE = "phase0_sku_status.csv"
CLEAN, ASSUMED, BLOCKED = "CLEAN", "ASSUMED", "DATA_BLOCKED"

# ---- blockers: missing data with no safe stand-in ----------------------------
BLOCKERS = {
    "B01": {"what": "no BOM for the SKU",
            "table": "JK_CTP_PPDS.CTP_Master_BOM",
            "why": "the components and their quantities are unknown",
            "owner": "master data (BOM)"},
    "B02": {"what": "no green-tyre (GT BUILDING) routing / GT code for the SKU",
            "table": "JK_CTP_PPDS.CTP_Master_Routing",
            "why": "the item to build is unknown",
            "owner": "master data (routing)"},
    "B03": {"what": "no allowable press / mould for the SKU",
            "table": "JK_CTP_PPDS.CTP_Master_CuringAllowable",
            "why": "the tyre cannot be cured",
            "owner": "curing / mould planning"},
    "B04": {"what": "no curing cycle time and no plan time",
            "table": "JK_CTP_PPDS.CTP_Master_CuringCycleTime",
            "why": "the cure duration is unknown",
            "owner": "curing master"},
    "B05": {"what": "a BOM component has no routing and is not stock or purchased",
            "table": "JK_CTP_PPDS.CTP_Master_Routing",
            "why": "the component can be neither made nor bought",
            "owner": "master data (routing)"},
    "B06": {"what": "a component the SKU needs has no machine-wise speed in all_machine_spec",
            "table": "JK_CTP_PPDS.CTP_Master_Routing_v2 (all_machine_spec)",
            "why": "the run time cannot be computed; no department median is used (v46j)",
            "owner": "master data (routing v2)"},
}

# ---- assumptions: missing data with a stated stand-in -----------------------
# status: "active" = applied by the engine today; "planned" = registered, applied
# in the step of IMPLEMENTATION_PLAN.md named in `step`.
ASSUMPTIONS = {
    "A01": {"gap": "machine list for an item", "rule": "department machine pool",
            "source": "routing, same department", "confidence": "medium", "status": "active"},
    "A02": {"gap": "process time (and batch size, only where none is stated)",
            "rule": "median of rated items in the same department",
            "source": "routing, same department", "confidence": "low", "status": "active"},
    "A03": {"gap": "maximum run quantity (MPQ max)", "rule": "lot capped at 8 h of work",
            "source": "run-length stand-in (phase 3)", "confidence": "medium", "status": "active"},
    "A04": {"gap": "FRC / calender line speed",
            "rule": "slowest master speed of the same item type on the same machine",
            "source": "FRC master, same machine", "confidence": "medium",
            "status": "planned", "step": "7"},
    "A05": {"gap": "mould change time on a press", "rule": "120 min PCR / 180 min TBR",
            "source": "rules file (until the plant confirms)", "confidence": "low",
            "status": "planned", "step": "3"},
    "A06": {"gap": "ageing spec (min/max)", "rule": "department default ageing",
            "source": "apply_aging_defaults", "confidence": "medium", "status": "active"},
    "A07": {"gap": "changeover pair missing in the matrix", "rule": "machine default changeover",
            "source": "CHANGEOVER_UNPRICED.csv", "confidence": "medium", "status": "active"},
    "A08": {"gap": "routing for a component the BOM uses",
            "rule": "component available from stock / purchase; walk stops at it",
            "source": "BOM item type", "confidence": "low", "status": "planned", "step": "6b"},
    "A09": {"gap": "buffer name does not match", "rule": "token match on the name",
            "source": "CTP_Master_Buffer", "confidence": "high", "status": "active"},
    "A10": {"gap": "holiday / downtime record", "rule": "machine available 24x7",
            "source": "-", "confidence": "medium", "status": "active"},
    "A11": {"gap": "GT code differs between building plan and curing plan for one SKU",
            "rule": "keep the building lot's GT (the tyre physically built); curing GT_Code "
                    "is used only where the SKU has no building lot",
            "source": "building plan / curing plan GT_Code", "confidence": "medium",
            "status": "active"},
    "A12": {"gap": "rate given for one line only", "rule": "same value for the other line",
            "source": "routing", "confidence": "medium", "status": "planned", "step": "1+"},
    "A13": {"gap": "builder setup for a product change the plan did not place",
            "rule": "plan's median CO_Mins for that SKU, else the line's median CO_Mins",
            "source": "building plan CO_Mins", "confidence": "medium", "status": "active"},
}

# phase-0 finding -> how the policy treats it
ISSUE_TO_ASSUMPTION = {"no_machines": "A01", "no_proc_time": "A02"}
ISSUE_TO_BLOCKER = {"no_bom": "B01", "no_building_routing": "B02",
                    "no_routing": "B05", "no_machine_speed": "B06"}


def in_kpi(state: str) -> bool:
    """Does this demand count in the final figures?"""
    return str(state) != BLOCKED
