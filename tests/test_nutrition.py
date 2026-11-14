from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import unittest

from messmate_nutrition.calculator import NutritionUnavailableError, estimate_range, estimate_serving, scale_per_100g
from messmate_nutrition.catalog import CATALOG, lookup_food, normalize_name
from messmate_nutrition.demo import render_menu
from messmate_nutrition.models import FoodCategory, FoodRecord, MacroNutrients, ServingEstimate


class NutritionModelTests(unittest.TestCase):
    def test_macro_values_are_nonnegative(self):
        with self.assertRaises(ValueError):
            MacroNutrients(Decimal("1"), Decimal("-1"), Decimal("1"), Decimal("0"))

    def test_macro_values_reject_impossible_mass(self):
        with self.assertRaises(ValueError):
            MacroNutrients(Decimal("500"), Decimal("60"), Decimal("50"), Decimal("1"))

    def test_serving_range_requires_typical_inside_bounds(self):
        with self.assertRaises(ValueError):
            ServingEstimate("piece", Decimal("90"), Decimal("40"), Decimal("80"), "estimated")

    def test_food_confidence_is_bounded(self):
        with self.assertRaises(ValueError):
            FoodRecord("x", "X", FoodCategory.INGREDIENT, (), None, None, Decimal("1.1"), None, review_required=True)

    def test_unknown_nutrition_must_be_reviewable(self):
        with self.assertRaises(ValueError):
            FoodRecord("x", "X", FoodCategory.INGREDIENT, (), None, None, Decimal("0.5"), None)


class CatalogTests(unittest.TestCase):
    def test_catalog_has_all_live_menu_names(self):
        snapshot = json.loads(Path("tests/fixtures/nov6_menu_fixture.json").read_text())
        self.assertEqual(len(snapshot["meals"]), 4)
        missing = [name for section in snapshot["meals"].values() for name in section["items"] if lookup_food(name) is None]
        self.assertEqual(missing, [])

    def test_nov7_plain_rice_and_french_fries_use_documented_usda_records(self):
        rice = lookup_food("Steamed Rice")
        fries = lookup_food("French Fries")
        self.assertIsNotNone(rice)
        self.assertIsNotNone(fries)
        assert rice is not None and fries is not None
        self.assertEqual(rice.confidence, Decimal("0.52"))
        self.assertEqual(rice.source.reference_id, "NDB 20445")
        self.assertEqual(rice.serving.grams, Decimal("185"))
        self.assertEqual(rice.nutrition_per_100g.calories_kcal, Decimal("130"))
        self.assertEqual(rice.nutrition_per_100g.protein_g, Decimal("2.69"))
        self.assertEqual(fries.confidence, Decimal("0.52"))
        self.assertEqual(fries.source.reference_id, "NDB 21138")
        self.assertEqual(fries.serving.grams, Decimal("117"))
        self.assertEqual(fries.nutrition_per_100g.calories_kcal, Decimal("312"))
        self.assertEqual(fries.nutrition_per_100g.protein_g, Decimal("3.43"))

    def test_normalization_is_case_and_punctuation_insensitive(self):
        self.assertEqual(normalize_name("  Paneer Do-Pyaza! "), "paneer do pyaza")
        self.assertIs(lookup_food("PANEER DO PYAZA"), CATALOG["paneer_do_pyaza"])

    def test_fuzzy_names_do_not_receive_nutrition(self):
        self.assertIsNone(lookup_food("Paneer Do Pyazaa"))

    def test_uncertain_menu_items_have_no_nutrients(self):
        for key in ("tea_d", "hot_coffee", "coffee_powder", "ullipai_pacchadi"):
            food = CATALOG[key]
            self.assertIsNone(food.nutrition_per_100g)
            self.assertTrue(food.review_required)

    def test_categories_distinguish_menu_food_types(self):
        self.assertEqual(CATALOG["banana"].category, FoodCategory.WHOLE_FOOD)
        self.assertEqual(CATALOG["coffee_powder"].category, FoodCategory.INGREDIENT)
        self.assertEqual(CATALOG["hot_milk"].category, FoodCategory.BEVERAGE)
        self.assertEqual(CATALOG["ketchup"].category, FoodCategory.CONDIMENT)
        self.assertEqual(CATALOG["bread_butter_jam"].category, FoodCategory.FOOD_COMBINATION)


class CalculatorTests(unittest.TestCase):
    def test_scaling_is_deterministic_and_per_100g(self):
        banana = CATALOG["banana"].nutrition_per_100g
        assert banana is not None
        macros = scale_per_100g(banana, 118)
        self.assertEqual(macros.calories_kcal, Decimal("105.02"))
        self.assertEqual(macros.protein_g, Decimal("1.29"))

    def test_nonpositive_weight_is_rejected(self):
        banana = CATALOG["banana"].nutrition_per_100g
        assert banana is not None
        with self.assertRaises(ValueError):
            scale_per_100g(banana, 0)

    def test_unavailable_food_raises_instead_of_returning_zero(self):
        with self.assertRaisesRegex(NutritionUnavailableError, "Nutrition unavailable"):
            estimate_serving(CATALOG["ullipai_pacchadi"])

    def test_serving_estimate_scales_known_item(self):
        estimate = estimate_serving(CATALOG["chapati"])
        self.assertEqual(estimate.serving_grams, Decimal("40"))
        self.assertEqual(estimate.macros.calories_kcal, Decimal("116.00"))

    def test_explicit_custom_serving_weight(self):
        self.assertEqual(estimate_serving(CATALOG["chapati"], 60).macros.calories_kcal, Decimal("174.00"))

    def test_range_returns_low_typical_high_estimates(self):
        low, usual, high = estimate_range(CATALOG["jeera_rice"])
        self.assertEqual((low.serving_grams, usual.serving_grams, high.serving_grams), (Decimal("120"), Decimal("180"), Decimal("250")))
        self.assertLess(low.macros.calories_kcal, usual.macros.calories_kcal)
        self.assertLess(usual.macros.calories_kcal, high.macros.calories_kcal)

    def test_no_default_serving_fails_closed(self):
        with self.assertRaises(NutritionUnavailableError):
            estimate_serving(CATALOG["coffee_powder"])


class ActualMenuDemonstrationTests(unittest.TestCase):
    def test_demo_includes_four_meals_and_unavailable_labels(self):
        output = render_menu(Path("tests/fixtures/nov6_menu_fixture.json"))
        for meal in ("Breakfast:", "Lunch:", "Snacks:", "Dinner:"):
            self.assertIn(meal, output)
        self.assertIn("Ullipai Pacchadi: UNAVAILABLE", output)
        self.assertIn("Tea (D): UNAVAILABLE", output)
        self.assertIn("no nutrition assigned", output)
        self.assertIn("Totals: not summed", output)


if __name__ == "__main__":
    unittest.main()
