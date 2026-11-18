from decimal import Decimal
import unittest

from messmate_nutrition.web_candidates import (
    CandidateValue,
    evaluate_page,
    summarize_dish,
)


def _page(nutrition: str) -> str:
    return (
        '<html><script type="application/ld+json">'
        '{"@context": "https://schema.org", "@type": "Recipe", "name": "Test dal",'
        f' "nutrition": {nutrition}}}'
        "</script></html>"
    )


class WebCandidateParsingTests(unittest.TestCase):
    def test_gram_serving_with_numeric_values_is_usable_and_normalised_per_100g(self) -> None:
        html = _page('{"@type": "NutritionInformation", "servingSize": "1 small cup (katori) (101 g)",'
                     ' "calories": "87 calories", "proteinContent": "3.9 g"}')
        (value,) = evaluate_page("https://example.test/a", html)
        self.assertTrue(value.usable)
        self.assertEqual(value.serving_grams, Decimal("101"))
        self.assertEqual(value.calories_kcal_per_100g, Decimal("86.14"))
        self.assertEqual(value.protein_g_per_100g, Decimal("3.86"))

    def test_serving_without_grams_is_rejected(self) -> None:
        html = _page('{"@type": "NutritionInformation", "servingSize": "2 serving",'
                     ' "calories": "651.71", "proteinContent": "g"}')
        (value,) = evaluate_page("https://example.test/b", html)
        self.assertFalse(value.usable)
        self.assertEqual(value.reason, "no gram serving basis")

    def test_batch_level_wording_is_rejected_even_with_grams(self) -> None:
        html = _page('{"@type": "NutritionInformation", "servingSize": "whole batch (1200 g)",'
                     ' "calories": "1500", "proteinContent": "60 g"}')
        (value,) = evaluate_page("https://example.test/c", html)
        self.assertFalse(value.usable)
        self.assertEqual(value.reason, "batch-level serving text")

    def test_missing_protein_is_rejected(self) -> None:
        html = _page('{"@type": "NutritionInformation", "servingSize": "100 g", "calories": "90"}')
        (value,) = evaluate_page("https://example.test/d", html)
        self.assertFalse(value.usable)
        self.assertEqual(value.reason, "calories or protein missing")

    def test_page_without_recipe_data_is_recorded_as_unusable(self) -> None:
        (value,) = evaluate_page("https://example.test/e", "<html><body>no json</body></html>")
        self.assertFalse(value.usable)
        self.assertEqual(value.reason, "no schema.org Recipe data")


class WebCandidateAverageTests(unittest.TestCase):
    @staticmethod
    def _usable(protein: str, kcal: str) -> CandidateValue:
        return CandidateValue("u", "100 g", Decimal("100"), Decimal(kcal), Decimal(protein), True, "usable")

    def test_refuses_to_average_fewer_than_three_usable_sources(self) -> None:
        average = summarize_dish("Dal", [self._usable("5", "100"), self._usable("6", "110")])
        self.assertIsNone(average.calories_kcal_per_100g_median)
        self.assertIn("not averaged", average.note)

    def test_median_and_range_over_three_usable_sources(self) -> None:
        average = summarize_dish("Dal", [
            self._usable("4", "90"), self._usable("6", "110"), self._usable("10", "130"),
        ])
        self.assertEqual(average.calories_kcal_per_100g_median, Decimal("110"))
        self.assertEqual(average.protein_g_per_100g_median, Decimal("6"))
        self.assertEqual((average.protein_g_per_100g_min, average.protein_g_per_100g_max), (Decimal("4"), Decimal("10")))

    def test_unusable_candidates_never_enter_the_average(self) -> None:
        unusable = CandidateValue("u", "4 serving", None, None, None, False, "no gram serving basis")
        average = summarize_dish("Dal", [self._usable("5", "100"), unusable, unusable, unusable])
        self.assertEqual(average.usable_sources, 1)
        self.assertIsNone(average.protein_g_per_100g_median)


if __name__ == "__main__":
    unittest.main()
