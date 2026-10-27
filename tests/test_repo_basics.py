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
        # v46f-k
        self.assertIn("CAP 66", R.rule("linespeed.single_strand"))
        self.assertTrue(R.rule("campaign.urgency_by_trolley_need"))
        self.assertAlmostEqual(R.rule("wip.mhe_conversion")["CAP 66"]["STL"]["factor"], 0.1)
        self.assertTrue(R.rule("wip.cut_width_from_bom"))
        self.assertEqual(R.rule("wip.alternative_children"), ["SNCT001", "SNCT003"])
        self.assertEqual(R.rule("wip.extra_cut_codes")["tbr"]["BCFT002"], "bias")
        self.assertIsNone(R.rule("routing_speed.source"))      # v46j speeds NOT used

    def test_plan_day_and_new_plan_folder(self):
        import run_pipeline as rp
        f = rp.find_plan_files(ROOT / "plans" / "2026-10-27")
        start, day = rp.plan_day(f)
        self.assertEqual(str(day), "2026-10-27")
        self.assertEqual(start.hour, 7)

    def test_db_env_file_is_ignored_and_example_has_no_values(self):
        import subprocess
        r = subprocess.run(["git", "check-ignore", "db.env"], cwd=ROOT, capture_output=True)
        self.assertEqual(r.returncode, 0, "db.env must be git-ignored")
        for ln in (ROOT / "db.env.example").read_text(encoding="utf-8").splitlines():
            if ln.startswith(("CTP_DB_HOST", "CTP_DB_USER", "CTP_DB_PASSWORD")):
                self.assertTrue(ln.rstrip().endswith("="), ln)

    def test_engine_entry_points_exist(self):
        for f in ("run_all.py", "phase5_placement.py", "make_validation_report.py",
                  "ctp_campaign.py", "ctp_aligned_needby.py", "make_bypass_report.py",
                  "stage_wip_v6.py", "make_wip_presence_report.py",
                  "make_day1_wip_building_report.py", "refresh_ctp_sql.py"):
            self.assertTrue((ROOT / "engine" / f).exists(), f)

    def test_no_credential_defaults(self):
        import re
        s = (ROOT / "engine" / "ctpsql.py").read_text(encoding="utf-8")
        for key in ("CTP_DB_HOST", "CTP_DB_USER", "CTP_DB_PASSWORD"):
            m = re.search(rf'os\.environ\.get\("{key}",\s*"([^"]*)"\)', s)
            self.assertTrue(m is None or m.group(1) == "", key)


if __name__ == "__main__":
    unittest.main()
