"""Fast checks that need no master data:  python -m unittest discover tests"""
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))


class RepoBasics(unittest.TestCase):
    def test_walk_counts_each_recipe_once(self):
        """v46d: a roll reached through two parents is entered once."""
        os.environ["CTP_RULES"] = str(ROOT / "config" / "rules.yaml")
        import rules as R
        R._CFG = None
        R.CFG_PATH = ROOT / "config" / "rules.yaml"
        import ctpio as io
        idx = {("S", "E"): {
            "GT": [("B1", 1.0, "M", "belt"), ("B2", 1.0, "M", "belt")],
            "B1": [("ROLL", 0.3, "M", "roll")], "B2": [("ROLL", 0.2, "M", "roll")],
            "ROLL": [("CPD", 0.5, "KG", "compound"), ("CPD", 0.4, "KG", "compound")]}}
        wc = io.WalkCache(idx, {"GT", "ROLL", "CPD"})
        wc.once_made, wc.once_contracted = True, True
        nodes, _, _ = wc.get("S", "E", "GT", frozenset())
        self.assertAlmostEqual(nodes["ROLL"]["per_tyre"], 0.5)
        self.assertAlmostEqual(nodes["CPD"]["per_tyre"], 0.9)


    def test_plan_files_found_by_name(self):
        import run_pipeline as rp
        f = rp.find_plan_files(ROOT / "plans" / "october_2026")
        for k in [("building", "pcr"), ("building", "tbr"), ("curing", "pcr"), ("curing", "tbr")]:
            self.assertIsNotNone(f[k], k)

    def test_resolved_rules_load(self):
        os.environ["CTP_RULES"] = str(ROOT / "config" / "rules.yaml")
        import rules as R
        R._CFG = None
        R.CFG_PATH = ROOT / "config" / "rules.yaml"
        self.assertEqual(R.rule("building.sequence_by"), "plan_order")
        self.assertEqual(R.rule("dispatch.choose_when_free"), "all")
        self.assertFalse(R.rule("bead_gate.enabled", True))
        self.assertIn("901", R.rule("dispatch.trolley_need_first_machines"))
        # v46a-e
        self.assertTrue(R.rule("wip.bypass_stock_to_children"))
        self.assertEqual(R.rule("wip.bypass_conversion_bom"), "reference_bom")
        self.assertIn("BEAD WINDING", R.rule("bypass_departments"))
        self.assertTrue(R.rule("tda_inline.enabled"))
        self.assertTrue(R.rule("bypass_transfer_parents_from_routing"))
        self.assertEqual(R.rule("bom.walk_each_item_once"), "all")
        self.assertTrue(R.rule("fourrc_dip.enabled"))

    def test_engine_entry_points_exist(self):
        for f in ("run_all.py", "phase5_placement.py", "make_validation_report.py",
                  "ctp_campaign.py", "ctp_aligned_needby.py", "make_bypass_report.py"):
            self.assertTrue((ROOT / "engine" / f).exists(), f)

    def test_no_credential_defaults(self):
        import re
        s = (ROOT / "engine" / "ctpsql.py").read_text(encoding="utf-8")
        for key in ("CTP_DB_HOST", "CTP_DB_USER", "CTP_DB_PASSWORD"):
            m = re.search(rf'os\.environ\.get\("{key}",\s*"([^"]*)"\)', s)
            self.assertTrue(m is None or m.group(1) == "", key)


if __name__ == "__main__":
    unittest.main()
