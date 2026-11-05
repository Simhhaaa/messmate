"""Deterministic nutrition estimates for MESSMATE menu items."""

from .calculator import NutritionUnavailableError, estimate_serving, estimate_range
from .catalog import CATALOG, lookup_food
from .models import FoodRecord, FoodCategory, MacroNutrients, ServingEstimate
from .optimizer import DietOptimizer, MealGuidance, NutritionTargets, PortionGuidance, SupplementProfile, UserNutritionProfile, menu_items_from_snapshot

__all__ = [
    "CATALOG", "DietOptimizer", "FoodCategory", "FoodRecord", "MacroNutrients",
    "MealGuidance", "NutritionTargets", "PortionGuidance", "NutritionUnavailableError", "ServingEstimate", "SupplementProfile",
    "UserNutritionProfile", "estimate_range", "estimate_serving", "lookup_food",
    "menu_items_from_snapshot",
]
