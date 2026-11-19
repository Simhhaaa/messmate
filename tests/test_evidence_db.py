from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from messmate_nutrition.evidence_db import (
    FIXTURE_DIR,
    MenuFixture,
    PRIORITY_DISHES,
    build_database,
)


class EvidenceDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls._tmp.name) / "evidence.sqlite"
        fixtures = [
            MenuFixture(date(2026, 11, 6), json.loads((FIXTURE_DIR / "nov6_menu_fixture.json").read_text(encoding="utf-8"))),
            MenuFixture(date(2026, 11, 7), json.loads((FIXTURE_DIR / "nov7_menu_fixture.json").read_text(encoding="utf-8"))),
            MenuFixture(date(2026, 11, 18), json.loads((FIXTURE_DIR / "nov18_menu_fixture.json").read_text(encoding="utf-8"))),
            MenuFixture(date(2026, 11, 19), json.loads((FIXTURE_DIR / "nov19_menu_fixture.json").read_text(encoding="utf-8"))),
        ]
        build_database(cls.path, fixtures)
        cls.connection = sqlite3.connect(cls.path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()
        cls._tmp.cleanup()

    def test_ifct_reference_rows_are_ingredient_only_and_complete(self) -> None:
        rows = self.connection.execute("SELECT ifct_code, basis FROM ifct_ingredient_reference").fetchall()
        self.assertEqual(len(rows), 12)
        self.assertTrue(all("not a dish" in basis for _, basis in rows))

    def test_dish_table_stores_no_nutrition_values(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(dish_evidence)")}
        for forbidden in ("calories_kcal", "protein_g", "carbohydrates_g", "fat_g"):
            self.assertNotIn(forbidden, columns)
        statuses = dict(self.connection.execute("SELECT dish_name, nutrition_status FROM dish_evidence"))
        for dish in PRIORITY_DISHES:
            self.assertTrue(statuses[dish["dish_name"]].startswith("estimated"), dish["dish_name"])

    def test_priority_dishes_are_recorded_with_missing_kitchen_fields(self) -> None:
        for name in ("Mix Yellow Dal", "Menthi Dal Tadka", "Palakura Pappu"):
            missing = self.connection.execute(
                "SELECT missing_kitchen_fields FROM dish_evidence WHERE dish_name = ?", (name,)
            ).fetchone()[0]
            self.assertIn("batch yield", missing)

    def test_menu_run_diagnostics_match_known_outcomes(self) -> None:
        runs = {
            row[0]: row
            for row in self.connection.execute(
                "SELECT menu_date, optimizer_status, calories_kcal, protein_g, protein_shortfall_g FROM menu_run_diagnostic"
            )
        }
        feasible = runs["2026-11-06"]
        self.assertEqual(feasible[1], "feasible")
        self.assertAlmostEqual(feasible[2], 1996.6, places=2)
        self.assertAlmostEqual(feasible[3], 120.04, places=2)

        # Robust planning: estimated dals must meet the minimums at their -25% case.
        # No such plan exists for Nov 7 (LP bound 116.9 g) or Nov 18 (LP bound 111.8 g).
        nov7 = runs["2026-11-07"]
        self.assertEqual(nov7[1], "protein_shortfall")
        self.assertAlmostEqual(nov7[2], 1807.20, places=2)
        self.assertAlmostEqual(nov7[3], 93.2595, places=4)
        self.assertAlmostEqual(nov7[4], 26.7405, places=4)

        nov18 = runs["2026-11-18"]
        self.assertEqual(nov18[1], "infeasible")
        self.assertLess(nov18[3], 120)

    def test_nov19_menu_is_recorded_with_gap_dishes(self) -> None:
        status, shortfall = self.connection.execute(
            "SELECT optimizer_status, protein_shortfall_g FROM menu_run_diagnostic WHERE menu_date = '2026-11-19'"
        ).fetchone()
        self.assertEqual(status, "protein_shortfall")
        self.assertGreater(shortfall, 0)
        gaps = self.connection.execute(
            "SELECT COUNT(*) FROM dish_evidence WHERE priority = 'gap (19 Nov menu)' AND nutrition_status = 'unverified'"
        ).fetchone()[0]
        self.assertEqual(gaps, 12)

    def test_ineligible_occurrences_always_carry_a_reason(self) -> None:
        missing_reason = self.connection.execute(
            "SELECT COUNT(*) FROM menu_occurrence WHERE eligible = 0 AND exclusion_reason = ''"
        ).fetchone()[0]
        self.assertEqual(missing_reason, 0)

    def test_nov18_estimated_dinner_dal_is_eligible_but_plan_stays_blocked(self) -> None:
        dal = self.connection.execute(
            "SELECT eligible FROM menu_occurrence WHERE menu_date = '2026-11-18' AND meal = 'Dinner' AND item_name = 'Panchratna Dal'"
        ).fetchone()[0]
        self.assertEqual(dal, 1)
        status = self.connection.execute(
            "SELECT optimizer_status FROM menu_run_diagnostic WHERE menu_date = '2026-11-18'"
        ).fetchone()[0]
        self.assertEqual(status, "infeasible")


if __name__ == "__main__":
    unittest.main()
