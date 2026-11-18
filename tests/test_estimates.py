from dataclasses import replace
from decimal import Decimal
import unittest

from messmate_nutrition.catalog import _ESTIMATE, lookup_food
from messmate_nutrition.models import FoodCategory, FoodRecord, MacroNutrients
from messmate_nutrition.optimizer import DEFAULT_PORTION_LIMITS, DietOptimizer, SelectedPortion, UserNutritionProfile

PROFILE = UserNutritionProfile(Decimal("170"), Decimal("70"), "Synthetic estimate-test profile")
ESTIMATED_DALS = ("mix_yellow_dal", "menthi_dal_tadka", "palakura_pappu", "panchratna_dal", "pappu_charu", "dhaba_dal")


def _macros(protein_per_100g: str) -> MacroNutrients:
    """Energy is derived from macros (4/4/9 kcal per gram) so the fixture is consistent."""
    protein, carbs, fat = Decimal(protein_per_100g), Decimal("10"), Decimal("2")
    return MacroNutrients(4 * protein + 4 * carbs + 9 * fat, protein, carbs, fat)


def _estimated_record(protein_per_100g: str) -> FoodRecord:
    return FoodRecord(
        key="synthetic_estimate", display_name="Synthetic Estimate", category=FoodCategory.COMPLETE_DISH,
        aliases=("synthetic estimate",),
        nutrition_per_100g=_macros(protein_per_100g),
        serving=None, confidence=Decimal("0.50"), source=_ESTIMATE, estimate_range=Decimal("0.25"),
    )


def _portion(food: FoodRecord, grams: str) -> SelectedPortion:
    return SelectedPortion(
        meal="Lunch", food_key=food.key, food_name=food.display_name, servings=1,
        serving_grams=Decimal(grams), total_grams=Decimal(grams), confidence=food.confidence,
    )


class EstimatedTierTests(unittest.TestCase):
    def test_estimate_range_must_be_positive_and_at_most_half(self) -> None:
        base = _estimated_record("40")
        for bad in (Decimal("0"), Decimal("0.6")):
            with self.assertRaises(ValueError):
                replace(base, estimate_range=bad)
        with self.assertRaises(ValueError):
            replace(base, nutrition_per_100g=None, review_required=True)

    def test_low_case_protein_shortfall_is_reported(self) -> None:
        food = _estimated_record("40")  # 300 g -> 120 g nominal; 90 g at -25%
        optimizer = DietOptimizer(profile=PROFILE)
        portions = (_portion(food, "300"),)
        records = {food.key: food}
        failures = optimizer._estimate_stress_failures(portions, Decimal("0"), records)
        self.assertTrue(any(f.startswith("low-estimate protein") for f in failures), failures)

    def test_non_estimated_plans_are_not_stress_tested(self) -> None:
        food = replace(_estimated_record("40"), estimate_range=None)
        optimizer = DietOptimizer(profile=PROFILE)
        self.assertEqual(optimizer._estimate_stress_failures((_portion(food, "300"),), Decimal("0"), {food.key: food}), ())

    def test_comfortable_margin_passes_protein_low_case(self) -> None:
        food = _estimated_record("60")  # 300 g -> 180 g nominal; 135 g at -25%
        optimizer = DietOptimizer(profile=PROFILE)
        failures = optimizer._estimate_stress_failures((_portion(food, "300"),), Decimal("0"), {food.key: food})
        self.assertFalse(any(f.startswith("low-estimate protein") for f in failures), failures)

    def test_robust_planner_never_returns_a_plan_failing_the_range_check(self) -> None:
        import json
        from messmate_nutrition.optimizer import PlanStatus, menu_items_from_snapshot
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
        for name in ("nov6", "nov7", "nov18"):
            snapshot = json.loads((root / f"{name}_menu_fixture.json").read_text(encoding="utf-8"))
            items = menu_items_from_snapshot(snapshot)
            optimizer = DietOptimizer(profile=PROFILE)
            result = optimizer.optimize(items)
            records = {item.food.key: item.food for item in items}
            if result.status is PlanStatus.FEASIBLE:
                self.assertEqual(
                    optimizer._estimate_stress_failures(result.portions, result.soya_dry_grams, records), (), name
                )

    def test_estimated_dal_records_are_flagged_and_limited(self) -> None:
        for key in ESTIMATED_DALS:
            record = lookup_food(key.replace("_", " "))
            self.assertIsNotNone(record, key)
            self.assertEqual(record.estimate_range, Decimal("0.25"), key)
            self.assertEqual(record.confidence, Decimal("0.50"), key)
            self.assertIn(record.key, DEFAULT_PORTION_LIMITS, key)


if __name__ == "__main__":
    unittest.main()
