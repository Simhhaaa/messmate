from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import inspect
import json
from pathlib import Path
import unittest

from messmate_nutrition.models import FoodCategory, FoodRecord, MacroNutrients, ServingEstimate, SourceReference
from messmate_nutrition.optimizer import (
    FIXED_PLANT_PROTEIN,
    SOYA_CHUNKS_LABEL,
    DEFAULT_MEAL_GUIDANCE,
    DietOptimizer,
    MealProteinGuidance,
    MealGuidance,
    MenuItem,
    NutritionTargets,
    PlanStatus,
    PortionGuidance,
    PortionLimits,
    SelectedPortion,
    SupplementProfile,
    UserNutritionProfile,
    menu_items_from_snapshot,
    summarize_validation_failure,
    validate_optimization_result,
)
from messmate_nutrition.optimizer_demo import render_optimization


def food(key: str, name: str, protein: str, *, calories: str = "300", confidence: str = "0.9") -> FoodRecord:
    return FoodRecord(
        key=key, display_name=name, category=FoodCategory.COMPLETE_DISH, aliases=(name,),
        nutrition_per_100g=MacroNutrients(Decimal(calories), Decimal(protein), Decimal("40"), Decimal("8")),
        serving=ServingEstimate("test portion", Decimal("100"), Decimal("100"), Decimal("100"), "test fixture"),
        confidence=Decimal(confidence), source=SourceReference("test fixture", "https://example.invalid"),
    )


SYNTHETIC_PROFILE = UserNutritionProfile(
    height_cm=Decimal("170"), weight_kg=Decimal("65"), goal="Synthetic unit-test profile"
)


def test_menu(protein: str = "20", calories: str = "300") -> tuple:
    return (
        MenuItem("Breakfast", food("a", "Test oats", protein, calories=calories)),
        MenuItem("Lunch", food("b", "Test dal", protein, calories=calories)),
        MenuItem("Dinner", food("c", "Test curry", protein, calories=calories)),
    )


def test_optimizer(**kwargs) -> DietOptimizer:
    kwargs.setdefault("profile", SYNTHETIC_PROFILE)
    test_limits = {key: PortionLimits(2, 4) for key in
                   ("a", "b", "c", "domain", "zero", "only", "low", "weak")}
    test_limits["repeat_dish"] = PortionLimits(2, 6)
    test_limits.update(kwargs.pop("portion_limits", {}))
    kwargs.setdefault("portion_limits", test_limits)
    return DietOptimizer(**kwargs)


def soya_profile() -> SupplementProfile:
    return SupplementProfile("Soya chunks", Decimal("316"), Decimal("32.9"), Decimal("31.7"), Decimal("0.9"), "synthetic test label per 100 g dry")


def actual_menu() -> tuple:
    snapshot = json.loads(Path("tests/fixtures/nov6_menu_fixture.json").read_text(encoding="utf-8"))
    return menu_items_from_snapshot(snapshot)


class OptimizerProfileTests(unittest.TestCase):
    def test_profile_targets_and_fixed_supplement_values_remain_unchanged(self):
        profile = SYNTHETIC_PROFILE
        self.assertEqual(profile.targets.preferred_calories_kcal, Decimal("1900"))
        self.assertEqual(profile.targets.minimum_calories_kcal, Decimal("1800"))
        self.assertEqual(profile.targets.maximum_calories_kcal, Decimal("2000"))
        self.assertEqual(profile.targets.minimum_protein_g, Decimal("120"))
        self.assertEqual(profile.targets.stretch_protein_g, Decimal("155"))
        self.assertEqual(profile.targets.preferred_fat_g, Decimal("55"))
        self.assertEqual((FIXED_PLANT_PROTEIN.calories_kcal, FIXED_PLANT_PROTEIN.protein_g), (Decimal("132"), Decimal("29")))
        self.assertEqual((FIXED_PLANT_PROTEIN.carbohydrates_g, FIXED_PLANT_PROTEIN.fat_g), (Decimal("2"), Decimal("0.48")))
        self.assertEqual(SOYA_CHUNKS_LABEL.calories_kcal, Decimal("316"))
        self.assertEqual(SOYA_CHUNKS_LABEL.protein_g, Decimal("32.9"))
        self.assertEqual(SOYA_CHUNKS_LABEL.carbohydrates_g, Decimal("31.7"))
        self.assertEqual(SOYA_CHUNKS_LABEL.fat_g, Decimal("0.9"))

    def test_private_profile_is_required_and_validated(self):
        with self.assertRaisesRegex(TypeError, "height_cm"):
            UserNutritionProfile()
        with self.assertRaisesRegex(ValueError, "UserNutritionProfile is required"):
            DietOptimizer()
        with self.assertRaisesRegex(ValueError, "height_cm"):
            UserNutritionProfile(Decimal("999"), Decimal("65"), "synthetic")
        with self.assertRaisesRegex(ValueError, "goal"):
            UserNutritionProfile(Decimal("170"), Decimal("65"), "   ")

    def test_invalid_targets_and_supplements_are_rejected(self):
        with self.assertRaises(ValueError):
            NutritionTargets(minimum_calories_kcal=Decimal("2001"))
        with self.assertRaisesRegex(ValueError, "only permitted"):
            DietOptimizer(profile=SYNTHETIC_PROFILE, soya=SupplementProfile("Paneer", Decimal("300"), Decimal("20"), Decimal("2"), Decimal("20"), "bad"))
        with self.assertRaisesRegex(ValueError, "exactly one 35 g scoop"):
            DietOptimizer(profile=SYNTHETIC_PROFILE, plant_protein=SupplementProfile("Plant protein isolate", Decimal("133"), Decimal("29"), Decimal("2"), Decimal("0.48"), "altered"))
        with self.assertRaisesRegex(ValueError, "per 100 g dry"):
            DietOptimizer(profile=SYNTHETIC_PROFILE, soya=SupplementProfile("Soya chunks", Decimal("345"), Decimal("52"), None, Decimal("1"), "incomplete"))
        with self.assertRaisesRegex(ValueError, "cannot exceed 70 g"):
            DietOptimizer(profile=SYNTHETIC_PROFILE, maximum_soya_grams=71)

    def test_meal_and_portion_guidance_are_soft_configurable_values(self):
        self.assertEqual(DEFAULT_MEAL_GUIDANCE["Breakfast"], MealGuidance(Decimal("350"), Decimal("650")))
        with self.assertRaises(ValueError):
            MealGuidance(Decimal("700"), Decimal("400"))
        with self.assertRaises(ValueError):
            PortionGuidance(0, 2)
        with self.assertRaises(ValueError):
            PortionLimits(0, 2)
        self.assertEqual(MealProteinGuidance(Decimal("20"), Decimal("40")).maximum_protein_g, Decimal("40"))
        params = inspect.signature(DietOptimizer).parameters
        self.assertIn("portion_limits", params)
        self.assertIn("meal_protein_guidance", params)


class OptimizerBehaviorTests(unittest.TestCase):
    def test_fixed_plant_isolate_is_included_exactly_once(self):
        result = test_optimizer().optimize(test_menu())
        mess_kcal = sum((Decimal("300") * p.servings for p in result.portions), Decimal("0"))
        self.assertEqual(result.fixed_plant_protein_scoops, 1)
        self.assertEqual(result.totals.calories_kcal - mess_kcal - result.soya_dry_grams * Decimal("3.16"), Decimal("132"))

    def test_hard_calorie_and_protein_constraints_are_met_when_feasible(self):
        result = test_optimizer().optimize(test_menu())
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertGreaterEqual(result.totals.calories_kcal, Decimal("1800"))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))
        self.assertGreaterEqual(result.totals.protein_g, Decimal("120"))
        self.assertLess(result.totals.protein_g, Decimal("155"))  # stretch is not mandatory

    def test_food_specific_hard_daily_and_per_meal_limits_are_enforced(self):
        same_dish = food("repeat_dish", "Available curry", "20")
        menu = tuple(MenuItem(meal, same_dish) for meal in ("Breakfast", "Lunch", "Dinner"))
        result = test_optimizer().optimize(menu)
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        total_servings = sum(p.servings for p in result.portions)
        self.assertLessEqual(total_servings, 6)
        self.assertTrue(all(p.servings <= 2 for p in result.portions))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))
        self.assertTrue(result.is_valid_target_plan)

    def test_solver_domain_is_derived_from_calorie_ceiling(self):
        optimizer = test_optimizer()
        candidate = food("domain", "Domain test food", "10", calories="100")
        self.assertEqual(optimizer._safe_serving_domain(candidate), 20)
        zero_kcal = food("zero", "Zero energy item", "10", calories="0")
        with self.assertRaisesRegex(Exception, "per-serving calories must be positive"):
            optimizer._safe_serving_domain(zero_kcal)

    def test_meal_calorie_windows_are_soft_and_deviations_are_reported(self):
        strict_advice = {
            "Breakfast": MealGuidance(Decimal("0"), Decimal("100")),
            "Lunch": MealGuidance(Decimal("0"), Decimal("2000")),
            "Snacks": MealGuidance(Decimal("0"), Decimal("2000")),
            "Dinner": MealGuidance(Decimal("0"), Decimal("2000")),
        }
        result = test_optimizer(meal_guidance=strict_advice).optimize(
            (MenuItem("Breakfast", food("only", "Only available food", "20")),)
        )
        self.assertEqual(result.status, PlanStatus.INFEASIBLE)
        self.assertTrue(any(item.meal == "Lunch" and not item.available for item in result.meal_coverage))
        self.assertGreater(dict(result.meal_calories_kcal)["Breakfast"], Decimal("100"))
        self.assertTrue(any("Breakfast" in deviation and "soft meal window" in deviation for deviation in result.soft_preference_deviations))
        self.assertTrue(any("Breakfast" in warning and "soft preferred calorie window" in warning for warning in result.warnings))

    def test_meal_window_minimum_above_daily_ceiling_remains_soft(self):
        guidance = dict(DEFAULT_MEAL_GUIDANCE)
        guidance["Breakfast"] = MealGuidance(Decimal("3000"), Decimal("3200"))
        result = test_optimizer(meal_guidance=guidance).optimize(test_menu())
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertGreaterEqual(result.totals.calories_kcal, Decimal("1800"))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))
        self.assertGreaterEqual(result.totals.protein_g, Decimal("120"))
        self.assertLess(dict(result.meal_calories_kcal)["Breakfast"], Decimal("2000"))
        self.assertTrue(any("Breakfast" in warning and "soft preferred calorie window" in warning
                            for warning in result.warnings))

    def test_configured_hard_portion_limit_and_soft_warning_are_both_reported(self):
        dish = food("only", "Only available food", "15", calories="250")
        menu = tuple(MenuItem(meal, dish) for meal in ("Breakfast", "Lunch", "Dinner"))
        result = test_optimizer(
            portion_guidance={"only": PortionGuidance(1, 2)},
            portion_limits={"only": PortionLimits(2, 6)},
        ).optimize(menu)
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertLessEqual(sum(p.servings for p in result.portions), 6)
        self.assertLessEqual(max(p.servings for p in result.portions), 2)
        self.assertTrue(any("Only available food is" in w and "soft guide 2; hard limit 6" in w for w in result.warnings))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))

    def test_milk_aliases_obey_combined_hard_daily_and_meal_limits(self):
        menu = test_menu()
        hot = food("hot_milk", "Hot milk", "20")
        cold = food("cold_milk", "Cold milk", "20")
        menu = menu + (MenuItem("Breakfast", hot), MenuItem("Lunch", cold))
        result = test_optimizer().optimize(menu)
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        milk_portions = [p for p in result.portions if p.food_key in {"hot_milk", "cold_milk"}]
        self.assertLessEqual(sum(p.servings for p in milk_portions), 2)
        self.assertTrue(all(p.servings <= 1 for p in milk_portions))

    def test_variety_and_meal_balance_are_soft_objectives(self):
        result = test_optimizer().optimize(test_menu())
        objectives = dict(result.objective_values)
        selected_foods = {p.food_key for p in result.portions}
        self.assertGreaterEqual(len(selected_foods), 2)
        self.assertIn("meal_distribution", objectives)
        self.assertIn("protein_distribution", objectives)
        self.assertIn("portion_guidance", objectives)
        self.assertIn("food_variety", objectives)
        self.assertIn("optional_protein_stretch", objectives)
        self.assertEqual(sum(value for _, value in result.meal_calories_kcal), result.totals.calories_kcal)
        self.assertEqual(sum(value for _, value in result.meal_protein_g), result.totals.protein_g)
        self.assertEqual(validate_optimization_result(result), ())

    def test_soya_is_minimized_and_added_only_if_needed(self):
        sufficient = test_optimizer().optimize(test_menu("20"))
        self.assertEqual(sufficient.soya_dry_grams, Decimal("0"))
        needs_small_amount = test_optimizer().optimize(test_menu("15"))
        self.assertEqual(needs_small_amount.status, PlanStatus.FEASIBLE)
        self.assertEqual(needs_small_amount.soya_dry_grams, Decimal("5"))
        self.assertGreaterEqual(needs_small_amount.totals.protein_g, Decimal("120"))
        self.assertLessEqual(needs_small_amount.totals.calories_kcal, Decimal("2000"))

    def test_soya_stays_within_70_g_dry_and_reported_in_grams(self):
        low_protein = (
            __import__("messmate_nutrition.optimizer", fromlist=["MenuItem"]).MenuItem("Breakfast", food("a", "Low protein breakfast", "1", calories="250")),
            __import__("messmate_nutrition.optimizer", fromlist=["MenuItem"]).MenuItem("Lunch", food("b", "Low protein lunch", "1", calories="250")),
            __import__("messmate_nutrition.optimizer", fromlist=["MenuItem"]).MenuItem("Dinner", food("c", "Low protein dinner", "1", calories="250")),
        )
        result = test_optimizer().optimize(low_protein)
        self.assertLessEqual(result.soya_dry_grams, Decimal("70"))
        self.assertGreaterEqual(result.totals.calories_kcal, Decimal("1800"))
        self.assertEqual(result.status, PlanStatus.PROTEIN_SHORTFALL)
        self.assertGreater(result.protein_shortfall_g, Decimal("0"))
        self.assertGreaterEqual(result.totals.calories_kcal, Decimal("1800"))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))

    def test_independent_macro_recalculation_and_soft_preferences_are_exposed(self):
        result = test_optimizer().optimize(test_menu())
        known_food_protein = sum((Decimal("20") * p.servings for p in result.portions), Decimal("0"))
        self.assertEqual(result.totals.protein_g, known_food_protein + Decimal("29"))
        self.assertIsNotNone(result.totals.fat_g)
        self.assertIsNotNone(result.totals.carbohydrates_g)
        self.assertTrue(any("Fat is" in d for d in result.soft_preference_deviations))

    def test_unknown_and_low_confidence_items_are_never_zero_filled(self):
        unknown_snapshot = {"meals": {"Breakfast": {"items": ["Mystery curry"]}, "Lunch": {"items": []}, "Snacks": {"items": []}, "Dinner": {"items": []}}}
        items = menu_items_from_snapshot(unknown_snapshot)
        self.assertIsNone(items[0].food.nutrition_per_100g)
        unknown_result = test_optimizer().optimize(items)
        self.assertIn("Breakfast: Mystery curry", unknown_result.missing_nutrition)
        low = food("low", "Low confidence food", "40", confidence="0.49")
        low_result = test_optimizer(soya=None).optimize((MenuItem("Breakfast", low),))
        self.assertFalse(low_result.portions)
        self.assertIn("below safety threshold", low_result.excluded_items[0][2])

    def test_missing_dinner_is_explicit_and_plan_is_not_valid(self):
        result = test_optimizer().optimize((MenuItem("Breakfast", food("only", "Known breakfast", "20")),))
        dinner = next(item for item in result.meal_coverage if item.meal == "Dinner")
        self.assertFalse(dinner.available)
        self.assertIn("No menu items were published", dinner.reason)
        self.assertEqual(result.status, PlanStatus.INFEASIBLE)
        self.assertFalse(result.is_valid_target_plan)

    def test_curds_hard_food_limits_reject_twenty_one_bowls(self):
        result = DietOptimizer(profile=SYNTHETIC_PROFILE).optimize(actual_menu())
        self.assertLessEqual(sum(p.servings for p in result.portions if p.food_key == "curd"), 2)
        oversized = SelectedPortion(
            "Lunch", "curd", "Curd", 21, Decimal("100"), Decimal("2100"), Decimal("0.62")
        )
        forged = replace(
            result,
            portions=tuple(p for p in result.portions if p.food_key != "curd") + (oversized,),
        )
        errors = validate_optimization_result(forged)
        self.assertTrue(any("curd exceeds its hard daily portion limit" in error for error in errors))
        self.assertTrue(any("Lunch/curd exceeds its hard per-meal portion limit" in error for error in errors))

    def test_nov7_live_menu_failure_summary_is_actionable_and_sanitized(self):
        source = json.loads(Path("tests/fixtures/nov7_menu_fixture.json").read_text(encoding="utf-8"))
        result = DietOptimizer(profile=SYNTHETIC_PROFILE).optimize(menu_items_from_snapshot(source))
        errors = validate_optimization_result(result)
        summary = summarize_validation_failure(result, errors)

        self.assertEqual(result.status, PlanStatus.PROTEIN_SHORTFALL)
        self.assertIn("plan_status", summary)
        self.assertIn("protein_minimum", summary)
        self.assertIn("meal_coverage=Breakfast:available,Lunch:available,Dinner:available", summary)
        self.assertIn("eligible_menu_items=Breakfast:2,Lunch:3,Dinner:2", summary)
        self.assertIn("calories_kcal=1807.20", summary)
        self.assertIn("protein_g=93.2595", summary)
        self.assertIn("unmapped_catalog:12", summary)
        self.assertEqual(sum(p.servings for p in result.portions if p.food_key == "steamed_rice"), 3)
        self.assertEqual(result.soya_dry_grams, Decimal("70"))
        self.assertLess(result.totals.protein_g, Decimal("120"))
        self.assertNotIn("Menthi Dal Tadka", summary)
        self.assertNotIn("As the strength of the learners", summary)

    def test_validator_rejects_food_missing_from_menu_inventory(self):
        result = DietOptimizer(profile=SYNTHETIC_PROFILE).optimize(actual_menu())
        fabricated = SelectedPortion(
            "Dinner", "invented_food", "Invented Food", 1, Decimal("100"), Decimal("100"), Decimal("0.9")
        )
        errors = validate_optimization_result(replace(result, portions=result.portions + (fabricated,)))
        self.assertTrue(any("not an eligible item in the supplied menu" in error for error in errors))

    def test_unfeasible_inventory_and_protein_shortfall_are_reported_honestly(self):
        empty = test_optimizer().optimize(())
        self.assertEqual(empty.status, PlanStatus.INFEASIBLE)
        self.assertFalse(empty.is_valid_target_plan)
        self.assertIn("No complete Breakfast/Lunch/Dinner plan", empty.message)
        low_food = food("weak", "Low-protein dish", "1", calories="600")
        low_protein = tuple(MenuItem(meal, low_food) for meal in ("Breakfast", "Lunch", "Dinner"))
        shortfall = test_optimizer().optimize(low_protein)
        self.assertEqual(shortfall.status, PlanStatus.PROTEIN_SHORTFALL)
        self.assertLess(shortfall.totals.protein_g, Decimal("120"))
        self.assertTrue(any("Protein minimum missed" in w for w in shortfall.warnings))


class ActualMenuOptimizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot_path = Path("tests/fixtures/nov6_menu_fixture.json")
        cls.snapshot = json.loads(cls.snapshot_path.read_text(encoding="utf-8"))
        cls.menu = menu_items_from_snapshot(cls.snapshot)

    def test_november_6_menu_has_four_sections_and_can_meet_protein_with_configured_soya_cap(self):
        self.assertEqual(set(self.snapshot["meals"]), {"Breakfast", "Lunch", "Snacks", "Dinner"})
        result = DietOptimizer(profile=SYNTHETIC_PROFILE).optimize(self.menu)
        self.assertEqual(result.status, PlanStatus.FEASIBLE)
        self.assertEqual(result.fixed_plant_protein_scoops, 1)
        self.assertLessEqual(result.soya_dry_grams, Decimal("70"))
        self.assertGreaterEqual(result.totals.protein_g, Decimal("120"))
        self.assertGreaterEqual(result.totals.calories_kcal, Decimal("1800"))
        self.assertLessEqual(result.totals.calories_kcal, Decimal("2000"))
        self.assertTrue(result.warnings)  # unavailable/low-confidence items and/or portion advice are explicit
        self.assertEqual(validate_optimization_result(result), ())

    def test_actual_menu_report_discloses_sections_macros_preferences_and_exclusions(self):
        output = render_optimization(self.snapshot_path, profile=SYNTHETIC_PROFILE)
        self.assertIn("Actual menu:", output)
        self.assertIn("Breakfast:", output)
        self.assertIn("Lunch:", output)
        self.assertIn("Snacks:", output)
        self.assertIn("Dinner:", output)
        self.assertIn("Meal nutrition (including supplements)", output)
        self.assertIn("Soft-preference deviations:", output)
        self.assertIn("Warnings and serving guidance", output)
        self.assertIn("Excluded menu items:", output)
        self.assertIn("Plant protein isolate: exactly 1 scoop", output)


if __name__ == "__main__":
    unittest.main()
