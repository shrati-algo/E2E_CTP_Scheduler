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

    def test_db_plan_gives_the_engine_the_same_plan_as_excel(self):
        """The 27 Oct Excel plans, reduced to the DB table's columns and written by
        plan_from_db, load into the engine exactly as the Excel files did."""
        import shutil, tempfile
        import pandas as pd
        from pipeline import plan_from_db as P
        src = ROOT / "plans" / "2026-10-27"
        if not src.exists():
            self.skipTest("reference plan folder not present")
        import ctpio as io
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            for ln in ("pcr", "tbr"):
                xb = next(src.glob(f"*building*{ln}*.xlsx"))
                xc = next(src.glob(f"*curing*{ln}*.xlsx"))
                need = ["Machine", "SKUCode", "Qty", "StartTime"]
                b = io._pe_sheet(xb, need)
                c = io._pe_sheet(xc, need).rename(
                    columns={"Description": "skuDescription"})
                B, C = P.to_engine_format(b, c)
                self.assertEqual(list(B.columns), P.BUILD_COLS)
                self.assertEqual(list(C.columns), P.CURE_COLS)
                for kind, X, ref in (("building", B, xb), ("curing", C, xc)):
                    d = tmp / "db" / ln / "pe_format"; d.mkdir(parents=True, exist_ok=True)
                    with pd.ExcelWriter(d / f"ctp_{kind}_schedule_plan_{ln}.xlsx") as w:
                        X.to_excel(w, sheet_name="Shift Schedule", index=False)
                    e = tmp / "xl" / ln / "pe_format"; e.mkdir(parents=True, exist_ok=True)
                    shutil.copy(ref, e / f"ctp_{kind}_schedule_plan_{ln}.xlsx")
                for which in ("db", "xl"):
                    io.PE_DIR = tmp / which
                    got = (io.load_building_plan(ln, {})[0], io.load_curing_plan(ln, {})[0])
                    if which == "db":
                        first = got
                cols_b = ["machine_raw", "sku", "qty", "build_start", "build_end", "shift",
                          "setup_min"]
                cols_c = ["press_raw", "sku", "qty", "start", "end", "shift", "setup_min",
                          "cycle_min"]
                pd.testing.assert_frame_equal(first[0][cols_b], got[0][cols_b], check_dtype=False)
                pd.testing.assert_frame_equal(first[1][cols_c], got[1][cols_c], check_dtype=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_snapshot_gate_refuses_an_incomplete_sync(self):
        import pandas as pd
        from pipeline import stock_snapshot as S
        s = pd.DataFrame({"inventory_date": ["2026-10-27"] * 3 + ["2026-10-28"],
                          "shift": ["A", "B", "C", "A"],
                          "updated_at": pd.to_datetime(["2026-10-27 06:31", "2026-10-27 14:31",
                                                        "2026-10-27 23:26", "2026-10-28 09:46"]),
                          "n": [13333, 13424, 13662, 1644]})
        with self.assertRaises(SystemExit):
            S._judge(s, "2026-10-28", "wip", 0.5, None)          # 1,644 of ~13,400
        ok = S._judge(s, "2026-10-27", "wip", 0.5, None)
        self.assertEqual(ok["sync"], "2026-10-27 06:31:00.000")
        pick = S._judge(s, "2026-10-28", "wip", 0.5, "2026-10-27 23:26")
        self.assertEqual(pick["chosen_by"], "explicit (command line)")

    def test_normal_runs_read_the_live_plan_tables(self):
        import importlib
        from pipeline import plan_from_db as P
        P = importlib.reload(P)
        self.assertEqual(P.BUILD_T, "CTP_Schedule_BuildingPlan")
        self.assertEqual(P.CURE_T, "CTP_Schedule_CuringPlan")
        src = (ROOT / "run_pipeline.py").read_text(encoding="utf-8")
        # the history tables are reachable ONLY behind the explicit test flag
        self.assertEqual(src.count("use_history_tables_for_test()"), 1)
        self.assertIn("if a.test_history_plan:", src)
        importlib.reload(P)

    def test_schedule_id_format(self):
        import datetime as dt, re
        from pipeline import persist_floor as F
        s = F.make_schedule_id(dt.datetime(2026, 10, 28, 16, 23, 33, 512000), "v46k")
        self.assertEqual(s["schedule_id"], "20261028_162333_v46k")
        self.assertEqual(s["db_schedule_id"], "2026-10-28 16:23:33")
        self.assertEqual(F.rules_version(ROOT / "config" / "rules.yaml"), "v46k")

    def test_plant_day_runs_0630_to_0629(self):
        import datetime as dt
        import run_pipeline as rp
        D = dt.datetime
        self.assertEqual(str(rp.plant_day(D(2026, 10, 28, 6, 30))), "2026-10-28")
        self.assertEqual(str(rp.plant_day(D(2026, 10, 28, 23, 59))), "2026-10-28")
        self.assertEqual(str(rp.plant_day(D(2026, 10, 29, 6, 29))), "2026-10-28")
        self.assertEqual(str(rp.plant_day(D(2026, 10, 29, 6, 30))), "2026-10-29")
        self.assertEqual(str(rp.plant_day(D(2026, 10, 29, 0, 15))), "2026-10-28")

    def test_preflight_report_and_verdict(self):
        from pipeline import preflight as PF
        rows = [{"id": "BP-PCR", "check": "plan", "status": "PASS", "detail": "ok"},
                {"id": "WIP-2", "check": "wip complete", "status": "FAIL", "detail": "1,644 rows"},
                {"id": "WIP-3", "check": "wip time", "status": "WARN", "detail": "09:46"}]
        txt = PF.show(rows, "2026-10-28")
        self.assertIn("[FAIL] WIP-2", txt)
        self.assertIn("NOT RUN", txt)
        self.assertTrue(PF.failed(rows))
        self.assertFalse(PF.failed(rows[:1] + rows[2:]))

    def test_db_env_file_is_ignored_and_example_has_no_values(self):
        import subprocess
        r = subprocess.run(["git", "check-ignore", "db.env"], cwd=ROOT, capture_output=True)
        if r.returncode == 128:             # not a git checkout (e.g. a ZIP download)
            ign = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
            self.assertTrue("*.env" in ign or "db.env" in ign, "db.env must be git-ignored")
        else:
            self.assertEqual(r.returncode, 0, "db.env must be git-ignored")
        for ln in (ROOT / "db.env.example").read_text(encoding="utf-8").splitlines():
            if ln.startswith(("CTP_DB_HOST", "CTP_DB_USER", "CTP_DB_PASSWORD")):
                self.assertTrue(ln.rstrip().endswith("="), ln)

    def test_engine_entry_points_exist(self):
        for f in ("run_all.py", "phase5_placement.py", "make_validation_report.py",
                  "ctp_campaign.py", "ctp_aligned_needby.py", "make_bypass_report.py",
                  "stage_wip_v6.py", "make_wip_presence_report.py",
                  "make_day1_wip_building_report.py", "refresh_ctp_sql.py",
                  "make_machine_idle_report.py"):
            self.assertTrue((ROOT / "engine" / f).exists(), f)
        for f in ("all_machines_day1_start.py", "shift_load_balance.py",
                  "shift_balance_from_floor.py", "built_not_cured.py", "_common.py"):
            self.assertTrue((ROOT / "analysis" / f).exists(), f)

    def test_no_credential_defaults(self):
        import re
        s = (ROOT / "engine" / "ctpsql.py").read_text(encoding="utf-8")
        for key in ("CTP_DB_HOST", "CTP_DB_USER", "CTP_DB_PASSWORD"):
            m = re.search(rf'os\.environ\.get\("{key}",\s*"([^"]*)"\)', s)
            self.assertTrue(m is None or m.group(1) == "", key)


if __name__ == "__main__":
    unittest.main()
