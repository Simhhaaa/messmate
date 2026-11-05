"""Pure deterministic scaling functions."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import FoodRecord, MacroNutrients, NutritionEstimate


class NutritionUnavailableError(ValueError):
    """Raised when an item has no reviewed nutrition record."""


_PRECISION = Decimal("0.01")


def scale_per_100g(values: MacroNutrients, grams: Decimal | int | str) -> MacroNutrients:
    weight = Decimal(str(grams))
    if weight <= 0:
        raise ValueError("serving weight must be positive")
    ratio = weight / Decimal("100")
    return MacroNutrients(**{
        name: (value * ratio).quantize(_PRECISION, rounding=ROUND_HALF_UP)
        for name, value in values.as_dict().items()
    })


def estimate_serving(food: FoodRecord, grams: Decimal | int | str | None = None) -> NutritionEstimate:
    if food.nutrition_per_100g is None:
        raise NutritionUnavailableError(
            f"Nutrition unavailable for {food.display_name!r}; review the food identity/recipe first."
        )
    if grams is None:
        if food.serving is None:
            raise NutritionUnavailableError(f"No serving estimate is defined for {food.display_name!r}.")
        weight = food.serving.grams
    else:
        weight = Decimal(str(grams))
    return NutritionEstimate(
        food_key=food.key,
        food_name=food.display_name,
        serving_grams=weight,
        macros=scale_per_100g(food.nutrition_per_100g, weight),
        confidence=food.confidence,
        assumptions=food.recipe.description if food.recipe else "Per-100 g reference profile scaled by edible serving weight.",
    )


def estimate_range(food: FoodRecord) -> tuple[NutritionEstimate, NutritionEstimate, NutritionEstimate]:
    if food.serving is None:
        raise NutritionUnavailableError(f"No serving estimate is defined for {food.display_name!r}.")
    return tuple(estimate_serving(food, grams) for grams in (
        food.serving.minimum_grams, food.serving.grams, food.serving.maximum_grams
    ))  # type: ignore[return-value]
