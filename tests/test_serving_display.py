from decimal import Decimal
import unittest

from messmate_nutrition.catalog import CATALOG
from messmate_nutrition.optimizer import SelectedPortion
from messmate_nutrition.serving_display import (
    DEFAULT_HOUSEHOLD_SERVINGS,
    HouseholdServing,
    format_fixed_supplement,
    format_selected_portion,
)


def portion(key: str, name: str, count: int, unit_grams: str) -> SelectedPortion:
    grams = Decimal(unit_grams)
    return SelectedPortion("Lunch", key, name, count, grams, grams * count, Decimal("0.8"))


class HouseholdServingDisplayTests(unittest.TestCase):
    def test_every_catalog_food_has_a_configurable_display_unit(self):
        self.assertEqual(set(DEFAULT_HOUSEHOLD_SERVINGS), set(CATALOG))

    def test_chapati_shows_number_and_total_estimated_grams(self):
        self.assertEqual(
            format_selected_portion(portion("chapati", "Chapati", 2, "40")),
            "2 medium chapatis (estimated 80 g total; cooked weight; about 40 g per configured unit)",
        )

    def test_rice_dal_curd_and_chole_use_distinct_bowl_weights(self):
        self.assertEqual(DEFAULT_HOUSEHOLD_SERVINGS["jeera_rice"].grams_per_unit, Decimal("180"))
        self.assertEqual(DEFAULT_HOUSEHOLD_SERVINGS["arhar_dal"].grams_per_unit, Decimal("180"))
        self.assertEqual(DEFAULT_HOUSEHOLD_SERVINGS["chole_masala"].grams_per_unit, Decimal("180"))
        self.assertEqual(DEFAULT_HOUSEHOLD_SERVINGS["curd"].grams_per_unit, Decimal("100"))
        rendered = format_selected_portion(portion("jeera_rice", "Jeera Rice", 1, "180"))
        self.assertIn("1 medium bowl", rendered)
        self.assertIn("estimated 180 g total", rendered)

    def test_usda_cooked_rice_and_fries_use_documented_serving_weights(self):
        self.assertEqual(DEFAULT_HOUSEHOLD_SERVINGS["steamed_rice"].grams_per_unit, Decimal("185"))
        self.assertEqual(DEFAULT_HOUSEHOLD_SERVINGS["french_fries"].grams_per_unit, Decimal("117"))
        self.assertEqual(
            format_selected_portion(portion("steamed_rice", "Steamed Rice", 1, "185")),
            "1 cooked cup (estimated 185 g total; cooked weight; USDA SR28 measure; about 185 g per configured unit)",
        )

    def test_configurable_household_weight_changes_display_only(self):
        item = portion("chapati", "Chapati", 2, "40")
        custom = {"chapati": HouseholdServing("small chapati", "small chapatis", Decimal("35"), "cooked weight")}
        rendered = format_selected_portion(item, serving_config=custom)
        self.assertIn("about 2.3 small chapatis", rendered)
        self.assertIn("estimated 80 g total", rendered)
        self.assertEqual(item.total_grams, Decimal("80"))

    def test_dry_and_cooked_weights_are_identified(self):
        self.assertIn("dry weight", DEFAULT_HOUSEHOLD_SERVINGS["muesli"].weight_basis)
        self.assertIn("cooked weight", DEFAULT_HOUSEHOLD_SERVINGS["jeera_rice"].weight_basis)
        self.assertIn("cooked weight", DEFAULT_HOUSEHOLD_SERVINGS["chapati"].weight_basis)

    def test_fixed_isolate_uses_scoop_count_and_label_weight(self):
        rendered = format_fixed_supplement("Plant protein isolate", "exactly 1 scoop", Decimal("35"), "product-label serving weight")
        self.assertEqual(rendered, "Plant protein isolate: exactly 1 scoop (estimated 35 g; product-label serving weight)")


if __name__ == "__main__":
    unittest.main()
