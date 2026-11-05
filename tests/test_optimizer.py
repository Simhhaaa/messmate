from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import unittest

from messmate_nutrition.models import FoodCategory, FoodRecord, MacroNutrients, ServingEstimate, SourceReference
from messmate_nutrition.optimizer import (
    FIXED_PLANT_PROTEIN,
    SOYA_CHUNKS_LABEL,
    DietOptimizer,
    FoodPortionLimit,
    MealRule,
    MenuItem,
    NutritionTargets,
    PlanStatus,
    SupplementProfile,
    UserNutritionProfile,
    menu_items_from_snapshot,
)
from messmate_nutrition.optimizer_demo import render_optimization


def food(key: str, name: str, protein: str, *, calories: str = "300", confidence: str = "0.9") -> FoodRecord:
    return FoodRecord(
        key=key, display_name=name, category=FoodCategory.COMPLETE_DISH, aliases=(name,),
        nutrition_per_100g=MacroNutrients(Decimal(calories), Decimal(protein), Decimal("40"), Decimal("8")),
        serving=ServingEstimate("test portion", Decimal("100"), Decimal("100"), Decimal("100"), "test fixture"),
        confidence=Decimal(confidence),
        source=SourceReference("test fixture", "https://example.invalid"),
    )


def test_menu(protein: str = "20") -> tuple[MenuItem, ...]:
    return (
        MenuItem("Breakfast", food("a", "Test oats", protein)),
        MenuItem("Lunch", food("b", "Test dal", protein)),
        MenuItem("Dinner", food("c", "Test curry", protein)),
    )


RULES = {"Breakfast": MealRule(1, 1, 2), "Lunch": MealRule(1, 1, 2), "Dinner": MealRule(1, 1, 2)}
TEST_LIMITS = {key: FoodPortionLimit(2, 2) for key in ("a", "b", "c")}


SYNTHETIC_PROFILE = UserNutritionProfile(
    height_cm=Decimal("170"), weight_kg=Decimal("65"), goal="Synthetic unit-test profile"
)


def test_optimizer(**kwargs) -> DietOptimizer:
    kwargs.setdefault("profile", SYNTHETIC_PROFILE)
    kwargs.setdefault("meal_rules", RULES)
    kwargs.setdefault("food_portion_limits", TEST_LIMITS)
    return DietOptimizer(**kwargs)


def soya_profile() -> SupplementProfile:
    # Synthetic label fixture for behavior tests only; not a claimed product label.
    return SupplementProfile("Soya chunks", Decimal("345"), Decimal("52"), Decimal("33"), Decimal("0.5"), "synthetic unit-test label per 100 g dry")


def actual_menu() -> tuple[MenuItem, ...]:
    snapshot = json.loads(Path("tests/fixtures/nov6_menu_fixture.json").read_text(encoding="utf-8"))
    return menu_items_from_snapshot(snapshot)


class OptimizerProfileTests(unittest.TestCase):
    def test_exact_user_targets_and_fixed_plant_isolate_profile(self):
        profile = SYNTHETIC_PROFILE
        self.assertEqual((profile.height_cm, profile.weight_kg), (Decimal("170"), Decimal("65")))
        self.assertEqual(profile.goal, "Synthetic unit-test profile")
        self.assertEqual(profile.targets.preferred_calories_kcal, Decimal("1900"))
        self.assertEqual(profile.targets.minimum_calories_kcal, Decimal("1800"))
        self.assertEqual(profile.targets.maximum_calories_kcal, Decimal("2000"))
        self.assertEqual(profile.targets.minimum_protein_g, Decimal("120"))
        self.assertEqual(profile.targets.stretch_protein_g, Decimal("155"))
        self.assertEqual(profile.targets.preferred_fat_g, Decimal("55"))
        self.assertEqual((FIXED_PLANT_PROTEIN.calories_kcal, FIXED_PLANT_PROTEIN.protein_g), (Decimal("132"), Decimal("29")))
        self.assertEqual((FIXED_PLANT_PROTEIN.carbohydrates_g, FIXED_PLANT_PROTEIN.fat_g), (Decimal("2"), Decimal("0.48")))
        self.assertEqual(FIXED_PLANT_PROTEIN.basis, "Exactly one 35 g scoop; user-provided product-label values.")
        self.assertEqual(SOYA_CHUNKS_LABEL.calories_kcal, Decimal("316"))
        self.assertEqual(SOYA_CHUNKS_LABEL.protein_g, Decimal("32.9"))
        self.assertEqual(SOYA_CHUNKS_LABEL.carbohydrates_g, Decimal("31.7"))
        self.assertEqual(SOYA_CHUNKS_LABEL.fat_g, Decimal("0.9"))

    def test_profile_values_are_required_explicitly(self):
        with self.assertRaisesRegex(TypeError, "height_cm"):
            UserNutritionProfile()
        with self.assertRaisesRegex(ValueError, "UserNutritionProfile is required"):
            DietOptimizer()

    def test_profile_values_are_validated(self):
        with self.assertRaisesRegex(ValueError, "height_cm"):
            UserNutritionProfile(Decimal("999"), Decimal("65"), "synthetic")
        with self.assertRaisesRegex(ValueError, "goal"):
            UserNutritionProfile(Decimal("170"), Decimal("65"), "   ")

    def test_invalid_targets_are_rejected(self):
        with self.assertRaises(ValueError):
            NutritionTargets(minimum_calories_kcal=Decimal("2001"))

    def test_only_soya_chunks_can_be_configured_as_extra_supplement(self):
        with self.assertRaisesRegex(ValueError, "only permitted"):
            DietOptimizer(soya=SupplementProfile("Paneer", Decimal("300"), Decimal("20"), Decimal("2"), Decimal("20"), "bad"))

    def test_fixed_plant_isolate_label_cannot_be_overridden(self):
        with self.assertRaisesRegex(ValueError, "exactly one 35 g scoop"):
            DietOptimizer(plant_protein=SupplementProfile("Plant protein isolate", Decimal("133"), Decimal("29"), Decimal("2"), Decimal("0.48"), "altered label"))

    def test_soya_label_requires_all_macro_fields_and_dry_weight_basis(self):
        with self.assertRaisesRegex(ValueError, "per 100 g dry"):
            DietOptimizer(soya=SupplementProfile("Soya chunks", Decimal("345"), Decimal("52"), None, Decimal("1"), "incomplete"))
        self.assertIn("dry", soya_profile().basis)

    def test_soya_daily_cap_cannot_exceed_70_g_dry(self):
        with self.assertRaisesRegex(ValueError, "cannot exceed 70 g"):
            DietOptimizer(maximum_soya_grams=71)

    def test_user_soya_label_scales_to_confirmed_70_g_values(self):
        profile = SOYA_CHUNKS_LABEL
        actual = MacroNutrients(
            profile.calories_kcal * Decimal("0.7"),
            profile.protein_g * Decimal("0.7"),
            profile.carbohydrates_g * Decimal("0.7"),
            profile.fat_g * Decimal("0.7"),
        )
        self.assertEqual(actual, MacroNutrients(Decimal("221.2"), Decimal("23.03"), Decimal("22.19"), Decimal("0.63")))


class OptimizerBehaviorTests(unittest.TestCase):
    def test_fixed_plant_isolate_is_included_exactly_once_in_every_result(self):
        result = test_optimizer().optimize(test_menu())
        self.assertEqual(result.fixed_plant_protein_scoops, 1)
        self.assertEqual(result.totals.calories_kcal - sum((Decimal("300") * p.servings for p in result.portions), Decimal("0")), Decimal("132"))
        self.assertEqual(result.totals.protein_g - sum((Decimal("20") * p.servings for p in result.portions), Decimal("0")), Decimal("29"))

    def test_meets_120_g_protein_but_155_is_not_mandatory(self):
        result = test_optimizer().optimize(test_menu("20"))
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertGreaterEqual(result.totals.protein_g, Decimal("120"))
        self.assertLess(result.totals.protein_g, Decimal("155"))

    def test_calories_stay_inside_1800_to_2000(self):
        result = test_optimizer().optimize(test_menu("20"))
        self.assertGreaterEqual(result.totals.calories_kcal, Decimal("1800"))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))

    def test_exact_1800_kcal_lower_boundary_is_allowed(self):
        lower_menu = (
            MenuItem("Breakfast", food("a", "Breakfast item", "20", calories="275")),
            MenuItem("Lunch", food("b", "Lunch item", "20", calories="276")),
            MenuItem("Dinner", food("c", "Dinner item", "20", calories="275")),
        )
        lower_menu = tuple(MenuItem(item.meal, food(item.food.key, item.food.display_name, "20", calories="278")) for item in lower_menu)
        targets = NutritionTargets(Decimal("1800"), Decimal("1800"), Decimal("1800"), Decimal("120"), Decimal("155"), Decimal("55"))
        result = test_optimizer(profile=UserNutritionProfile(height_cm=Decimal("170"), weight_kg=Decimal("65"), goal="Synthetic unit-test profile", targets=targets)).optimize(lower_menu)
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertEqual(result.totals.calories_kcal, Decimal("1800"))

    def test_exact_2000_kcal_upper_boundary_is_allowed(self):
        upper_menu = (
            MenuItem("Breakfast", food("a", "Breakfast item", "20", calories="311")),
            MenuItem("Lunch", food("b", "Lunch item", "20", calories="311")),
            MenuItem("Dinner", food("c", "Dinner item", "20", calories="312")),
        )
        targets = NutritionTargets(Decimal("2000"), Decimal("2000"), Decimal("2000"), Decimal("120"), Decimal("155"), Decimal("55"))
        result = test_optimizer(profile=UserNutritionProfile(height_cm=Decimal("170"), weight_kg=Decimal("65"), goal="Synthetic unit-test profile", targets=targets)).optimize(upper_menu)
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertEqual(result.totals.calories_kcal, Decimal("2000"))

    def test_soya_is_zero_when_mess_food_plus_isolate_meets_protein(self):
        result = test_optimizer(soya=soya_profile()).optimize(test_menu("20"))
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertEqual(result.soya_dry_grams, Decimal("0"))

    def test_minimum_soya_added_only_when_needed(self):
        result = test_optimizer(soya=soya_profile()).optimize(test_menu("15"))
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertGreater(result.soya_dry_grams, Decimal("0"))
        self.assertEqual(result.soya_dry_grams % Decimal("5"), Decimal("0"))
        self.assertGreaterEqual(result.totals.protein_g, Decimal("120"))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))

    def test_soya_never_exceeds_70_g_dry_weight(self):
        low_protein_menu = (
            MenuItem("Breakfast", food("a", "Low protein breakfast", "1", calories="250")),
            MenuItem("Lunch", food("b", "Low protein lunch", "1", calories="250")),
            MenuItem("Dinner", food("c", "Low protein dinner", "1", calories="250")),
        )
        result = test_optimizer().optimize(low_protein_menu)
        self.assertEqual(result.soya_dry_grams, Decimal("70"))
        self.assertLessEqual(result.soya_dry_grams, Decimal("70"))
        self.assertEqual(result.totals.calories_kcal, Decimal("1853.2"))

    def test_configurable_food_portion_limits_are_applied(self):
        limits = {"chapati": FoodPortionLimit(maximum_servings_per_day=1, maximum_servings_per_meal=1)}
        result = test_optimizer(food_portion_limits=limits).optimize(actual_menu())
        chapatis = [portion for portion in result.portions if portion.food_key == "chapati"]
        self.assertLessEqual(sum(portion.servings for portion in chapatis), 1)

    def test_per_day_limits_prevent_two_milk_variants_and_excess_dal_or_chole(self):
        result = test_optimizer().optimize(actual_menu())
        selected = {portion.food_key: portion.servings for portion in result.portions}
        self.assertLessEqual(selected.get("hot_milk", 0) + selected.get("cold_milk", 0), 1)
        self.assertLessEqual(selected.get("arhar_dal", 0), 1)
        self.assertLessEqual(selected.get("chole_masala", 0), 1)
        self.assertLessEqual(selected.get("jeera_rice", 0), 2)

    def test_meal_calorie_totals_obey_configured_distribution(self):
        result = test_optimizer().optimize(actual_menu())
        actual_by_meal = dict(result.meal_calories_kcal)
        optimizer = test_optimizer()
        self.assertEqual(set(actual_by_meal), set(optimizer.meal_rules))
        for meal, calories in actual_by_meal.items():
            rule = optimizer.meal_rules[meal]
            self.assertGreaterEqual(calories, rule.minimum_calories_kcal)
            self.assertLessEqual(calories, rule.maximum_calories_kcal)
        self.assertEqual(sum(actual_by_meal.values()), result.totals.calories_kcal)

    def test_no_supplement_paneer_is_injected(self):
        result = test_optimizer().optimize(test_menu("20"))
        self.assertFalse(any("paneer" in item.food_name.casefold() for item in result.portions))
        self.assertEqual(result.soya_dry_grams, 0)

    def test_no_feasible_meal_inventory_is_reported(self):
        result = test_optimizer().optimize(())
        self.assertEqual(result.status, PlanStatus.INFEASIBLE)
        self.assertFalse(result.is_valid_target_plan)
        self.assertIn("No menu combination", result.message)

    def test_unknown_food_is_excluded_and_never_zero_filled(self):
        menu = ({"meals": {"Breakfast": {"items": ["Mystery curry"]}, "Lunch": {"items": []}, "Dinner": {"items": []}}})
        items = menu_items_from_snapshot(menu)
        self.assertIsNone(items[0].food.nutrition_per_100g)
        result = test_optimizer(meal_rules={"Breakfast": MealRule(0, 1), "Lunch": MealRule(0, 1), "Dinner": MealRule(0, 1)}).optimize(items)
        self.assertIn("Breakfast: Mystery curry", result.missing_nutrition)
        self.assertTrue(any(row[1] == "Mystery curry" for row in result.excluded_items))

    def test_low_confidence_menu_food_is_excluded_by_default(self):
        low = food("low", "Low confidence food", "40", confidence="0.49")
        result = test_optimizer(meal_rules={"Breakfast": MealRule(0, 1)}, soya=None).optimize((MenuItem("Breakfast", low),))
        self.assertFalse(result.portions)
        self.assertIn("below safety threshold", result.excluded_items[0][2])

    def test_deterministic_across_repeated_runs(self):
        first = test_optimizer().optimize(test_menu("20"))
        second = test_optimizer().optimize(test_menu("20"))
        self.assertEqual(first, second)

    def test_independent_macro_recalculation_is_exposed_in_totals(self):
        result = test_optimizer().optimize(test_menu("20"))
        food_protein = sum((Decimal("20") * portion.servings for portion in result.portions), Decimal("0"))
        self.assertEqual(result.totals.protein_g, food_protein + Decimal("29"))
        self.assertIsNotNone(result.totals.fat_g)
        self.assertIsNotNone(result.totals.carbohydrates_g)

    def test_fixed_plant_label_enables_fat_and_carbohydrate_objectives(self):
        result = test_optimizer(plant_protein=FIXED_PLANT_PROTEIN).optimize(test_menu("20"))
        self.assertIsNotNone(result.totals.fat_g)
        self.assertIsNotNone(result.totals.carbohydrates_g)
        self.assertIn("fat_target_deviation", dict(result.objective_values))
        self.assertIn("remaining_carbohydrates", dict(result.objective_values))


class ActualMenuOptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        snapshot = json.loads(Path("tests/fixtures/nov6_menu_fixture.json").read_text(encoding="utf-8"))
        cls.snapshot = snapshot
        cls.menu = menu_items_from_snapshot(snapshot)

    def test_november_6_menu_has_all_four_sections(self):
        self.assertEqual(set(self.snapshot["meals"]), {"Breakfast", "Lunch", "Snacks", "Dinner"})
        self.assertTrue(self.menu)

    def test_actual_menu_respects_realistic_limits_and_reports_shortfall(self):
        result = DietOptimizer(profile=SYNTHETIC_PROFILE).optimize(self.menu)
        self.assertEqual(result.fixed_plant_protein_scoops, 1)
        self.assertEqual(result.soya_dry_grams, Decimal("70"))
        self.assertLessEqual(result.soya_dry_grams, Decimal("70"))
        self.assertEqual(result.status, PlanStatus.PROTEIN_SHORTFALL)
        self.assertEqual(result.totals.calories_kcal, Decimal("1971.2"))
        self.assertEqual(result.totals.protein_g, Decimal("115.03"))
        self.assertEqual(result.totals.carbohydrates_g, Decimal("273.09"))
        self.assertEqual(result.totals.fat_g, Decimal("42.16"))
        self.assertEqual(result.protein_shortfall_g, Decimal("4.97"))

    def test_actual_menu_report_discloses_unknown_macros_and_exclusions(self):
        output = render_optimization(Path("tests/fixtures/nov6_menu_fixture.json"), profile=SYNTHETIC_PROFILE)
        self.assertIn("Fixed plant protein isolate: exactly 1", output)
        self.assertIn("carbohydrates 273.09 g", output)
        self.assertIn("fat 42.16 g", output)
        self.assertIn("Soya chunks: 70 g dry", output)
        self.assertIn("allowed 350–650 kcal", output)
        self.assertIn("2 medium chapatis (estimated 80 g total", output)
        self.assertIn("1 medium bowl (estimated 180 g total; cooked weight", output)


if __name__ == "__main__":
    unittest.main()
