"""Typed records used by the independent nutrition engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class FoodCategory(StrEnum):
    WHOLE_FOOD = "whole_food"
    COMPLETE_DISH = "complete_dish"
    INGREDIENT = "ingredient"
    BEVERAGE = "beverage"
    CONDIMENT = "condiment"
    FOOD_COMBINATION = "food_combination"


@dataclass(frozen=True)
class MacroNutrients:
    """Macros per 100 g edible portion. ``None`` is unknown, never zero-filled."""

    calories_kcal: Decimal
    protein_g: Decimal
    carbohydrates_g: Decimal
    fat_g: Decimal

    def __post_init__(self) -> None:
        for name, value in self.as_dict().items():
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.protein_g + self.carbohydrates_g + self.fat_g > Decimal("100.5"):
            raise ValueError("macronutrient mass per 100 g is implausibly above 100 g")

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "calories_kcal": self.calories_kcal,
            "protein_g": self.protein_g,
            "carbohydrates_g": self.carbohydrates_g,
            "fat_g": self.fat_g,
        }


@dataclass(frozen=True)
class ServingEstimate:
    label: str
    grams: Decimal
    minimum_grams: Decimal
    maximum_grams: Decimal
    basis: str

    def __post_init__(self) -> None:
        if self.minimum_grams <= 0 or self.grams <= 0 or self.maximum_grams <= 0:
            raise ValueError("serving weights must be positive")
        if not self.minimum_grams <= self.grams <= self.maximum_grams:
            raise ValueError("typical serving must be within serving range")


@dataclass(frozen=True)
class SourceReference:
    name: str
    url: str
    reference_id: str | None = None
    note: str = ""


@dataclass(frozen=True)
class RecipeAssumption:
    description: str
    components: tuple[str, ...] = ()
    yield_basis: str = "Finished-dish weight; water/oil retention is approximated."


@dataclass(frozen=True)
class FoodRecord:
    """Catalog entry. Confidence is a heuristic score, not a probability.

    Approximate interpretation: >=0.80 strong generic identity/reference match,
    0.50–0.79 usable but recipe/portion-dependent, and <0.50 weak or especially
    assumption-sensitive. Records with missing nutrition always require review.
    """

    key: str
    display_name: str
    category: FoodCategory
    aliases: tuple[str, ...]
    nutrition_per_100g: MacroNutrients | None
    serving: ServingEstimate | None
    confidence: Decimal
    source: SourceReference | None
    recipe: RecipeAssumption | None = None
    review_required: bool = False
    notes: str = ""
    # Estimated tier: fractional uncertainty (0.25 = ±25%) applied to every macro.
    # Plans with estimated items must also pass low/high range checks.
    estimate_range: Decimal | None = None

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        if self.estimate_range is not None:
            if self.nutrition_per_100g is None:
                raise ValueError("estimated records must carry nutrition values")
            if not Decimal("0") < self.estimate_range <= Decimal("0.5"):
                raise ValueError("estimate_range must be greater than 0 and at most 0.5")
        if self.nutrition_per_100g is None and not self.review_required:
            raise ValueError("unknown nutrition must be marked for review")
        if self.nutrition_per_100g is not None and self.source is None:
            raise ValueError("nutrition estimates require a source reference")


@dataclass(frozen=True)
class NutritionEstimate:
    food_key: str
    food_name: str
    serving_grams: Decimal
    macros: MacroNutrients
    confidence: Decimal
    assumptions: str
