"""Deterministic CP-SAT optimization for daily mess-food portions.

All CP-SAT coefficients use integer hundredths. The selected plan is then
recalculated independently with Decimal arithmetic before it is returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Mapping, Sequence

from ortools.sat.python import cp_model

from .catalog import lookup_food
from .models import FoodCategory, FoodRecord, MacroNutrients


class OptimizationError(RuntimeError):
    """Base class for optimizer errors."""


class OptimizationValidationError(OptimizationError):
    """The independent Decimal validation rejected a solver result."""


class PlanStatus(StrEnum):
    FEASIBLE = "feasible"
    PROTEIN_SHORTFALL = "protein_shortfall"
    INFEASIBLE = "infeasible"


@dataclass(frozen=True)
class NutritionTargets:
    preferred_calories_kcal: Decimal = Decimal("1900")
    minimum_calories_kcal: Decimal = Decimal("1800")
    maximum_calories_kcal: Decimal = Decimal("2000")
    minimum_protein_g: Decimal = Decimal("120")
    stretch_protein_g: Decimal = Decimal("155")
    preferred_fat_g: Decimal = Decimal("55")

    def __post_init__(self) -> None:
        if not Decimal("0") < self.minimum_calories_kcal <= self.preferred_calories_kcal <= self.maximum_calories_kcal:
            raise ValueError("calorie targets must satisfy 0 < minimum <= preferred <= maximum")
        if not Decimal("0") < self.minimum_protein_g <= self.stretch_protein_g:
            raise ValueError("protein targets must satisfy 0 < minimum <= stretch")
        if self.preferred_fat_g < 0:
            raise ValueError("preferred fat cannot be negative")


@dataclass(frozen=True)
class UserNutritionProfile:
    height_cm: Decimal
    weight_kg: Decimal
    goal: str
    targets: NutritionTargets = field(default_factory=NutritionTargets)

    def __post_init__(self) -> None:
        if not self.height_cm.is_finite() or not Decimal("100") <= self.height_cm <= Decimal("250"):
            raise ValueError("height_cm must be a finite value from 100 to 250 cm")
        if not self.weight_kg.is_finite() or not Decimal("30") <= self.weight_kg <= Decimal("400"):
            raise ValueError("weight_kg must be a finite value from 30 to 400 kg")
        if not self.goal.strip() or len(self.goal.strip()) > 200:
            raise ValueError("goal must contain 1 to 200 non-whitespace characters")


@dataclass(frozen=True)
class SupplementProfile:
    """Per-serving fixed plant isolate facts or per-100-g dry soya label.

    ``carbohydrates_g`` and ``fat_g`` may be omitted only when those label
    fields are unavailable. Missing data stays unknown.
    """

    name: str
    calories_kcal: Decimal
    protein_g: Decimal
    carbohydrates_g: Decimal | None
    fat_g: Decimal | None
    basis: str

    def __post_init__(self) -> None:
        for field_name in ("calories_kcal", "protein_g", "carbohydrates_g", "fat_g"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")


FIXED_PLANT_PROTEIN = SupplementProfile(
    name="Plant protein isolate",
    calories_kcal=Decimal("132"),
    protein_g=Decimal("29"),
    carbohydrates_g=Decimal("2"),
    fat_g=Decimal("0.48"),
    basis="Exactly one 35 g scoop; user-provided product-label values.",
)

SOYA_CHUNKS_LABEL = SupplementProfile(
    name="Soya chunks",
    calories_kcal=Decimal("316"),
    protein_g=Decimal("32.9"),
    carbohydrates_g=Decimal("31.7"),
    fat_g=Decimal("0.9"),
    basis="User-provided product-label values per 100 g dry weight.",
)


@dataclass(frozen=True)
class MealGuidance:
    """Soft preferred calorie window for a meal; never a hard meal limit."""

    minimum_calories_kcal: Decimal
    maximum_calories_kcal: Decimal

    def __post_init__(self) -> None:
        if self.minimum_calories_kcal < 0 or self.maximum_calories_kcal < self.minimum_calories_kcal:
            raise ValueError("invalid preferred meal calorie window")


@dataclass(frozen=True)
class PortionGuidance:
    """Advisory household-serving thresholds used for warnings and soft penalties."""

    servings_per_meal: int = 2
    servings_per_day: int = 4

    def __post_init__(self) -> None:
        if self.servings_per_meal < 1 or self.servings_per_day < 1:
            raise ValueError("portion guidance thresholds must be positive")


@dataclass(frozen=True)
class PortionLimits:
    """Hard maximum standard servings for a food at one meal and per day."""

    servings_per_meal: int
    servings_per_day: int

    def __post_init__(self) -> None:
        if self.servings_per_meal < 1 or self.servings_per_day < 1:
            raise ValueError("hard portion limits must be positive")


@dataclass(frozen=True)
class MealProteinGuidance:
    """Soft protein range for a meal; values include supplements assigned there."""

    minimum_protein_g: Decimal
    maximum_protein_g: Decimal

    def __post_init__(self) -> None:
        if self.minimum_protein_g < 0 or self.maximum_protein_g < self.minimum_protein_g:
            raise ValueError("invalid preferred meal protein range")


DEFAULT_MEAL_GUIDANCE: dict[str, MealGuidance] = {
    "Breakfast": MealGuidance(Decimal("350"), Decimal("650")),
    "Lunch": MealGuidance(Decimal("400"), Decimal("700")),
    "Snacks": MealGuidance(Decimal("0"), Decimal("300")),
    "Dinner": MealGuidance(Decimal("400"), Decimal("700")),
}

_DEFAULT_PROTEIN_SHARES: dict[str, tuple[Decimal, Decimal]] = {
    "Breakfast": (Decimal("0.25"), Decimal("0.35")),
    "Lunch": (Decimal("0.30"), Decimal("0.40")),
    "Dinner": (Decimal("0.30"), Decimal("0.40")),
    "Snacks": (Decimal("0"), Decimal("0.15")),
}

# These are explicit food-specific household-serving limits. They are
# configurable through DietOptimizer and do not alter catalog nutrition.
DEFAULT_PORTION_LIMITS: dict[str, PortionLimits] = {
    "banana": PortionLimits(1, 2),
    "masala_oats": PortionLimits(1, 1),
    "muesli": PortionLimits(1, 1),
    "sada_paratha": PortionLimits(1, 2),
    "chana_palak": PortionLimits(1, 2),
    "hot_milk": PortionLimits(1, 2),
    "cold_milk": PortionLimits(1, 2),
    "tea_d": PortionLimits(1, 2),
    "coffee_powder": PortionLimits(1, 1),
    "bread_butter_jam": PortionLimits(1, 1),
    "arhar_dal": PortionLimits(2, 3),
    "paneer_do_pyaza": PortionLimits(1, 2),
    "ullipai_pacchadi": PortionLimits(1, 2),
    "curd": PortionLimits(1, 2),
    "green_salad": PortionLimits(2, 3),
    "jeera_rice": PortionLimits(2, 3),
    "chapati": PortionLimits(3, 6),
    "veg_puff": PortionLimits(1, 2),
    "hot_coffee": PortionLimits(1, 2),
    "ketchup": PortionLimits(1, 2),
    "chole_masala": PortionLimits(1, 2),
    "jeera_aloo": PortionLimits(1, 2),
    "rasam": PortionLimits(2, 3),
    "steamed_rice": PortionLimits(2, 3),
    "french_fries": PortionLimits(1, 1),
}

REQUIRED_MEALS = ("Breakfast", "Lunch", "Dinner")
MILK_PORTION_LIMITS = PortionLimits(servings_per_meal=1, servings_per_day=2)

# These are advisory thresholds, not mess rules or solver bounds. They are
# configurable per food and only affect soft objectives and warning output.
DEFAULT_PORTION_GUIDANCE: dict[str, PortionGuidance] = {
    "default": PortionGuidance(2, 4),
    "chapati": PortionGuidance(3, 6),
    "jeera_rice": PortionGuidance(2, 4),
    "masala_oats": PortionGuidance(2, 3),
    "muesli": PortionGuidance(2, 3),
    "sada_paratha": PortionGuidance(2, 4),
    "banana": PortionGuidance(2, 3),
    "hot_milk": PortionGuidance(2, 4),
    "cold_milk": PortionGuidance(2, 4),
    "arhar_dal": PortionGuidance(2, 4),
    "chana_palak": PortionGuidance(2, 3),
    "chole_masala": PortionGuidance(2, 3),
    "curd": PortionGuidance(2, 4),
}

MILK_FOOD_KEYS = frozenset({"hot_milk", "cold_milk"})
MILK_PORTION_GUIDANCE = PortionGuidance(servings_per_meal=1, servings_per_day=2)


@dataclass(frozen=True)
class MenuItem:
    meal: str
    food: FoodRecord

    @property
    def key(self) -> str:
        return f"{self.meal}:{self.food.key}"


@dataclass(frozen=True)
class SelectedPortion:
    meal: str
    food_key: str
    food_name: str
    servings: int
    serving_grams: Decimal
    total_grams: Decimal
    confidence: Decimal


@dataclass(frozen=True)
class MealCoverage:
    meal: str
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class PlanValidationContext:
    targets: NutritionTargets
    required_meals: tuple[str, ...]
    meal_names: tuple[str, ...]
    menu_inventory: tuple[MenuItem, ...]
    meal_coverage: tuple[MealCoverage, ...]
    portion_limits: tuple[tuple[str, PortionLimits], ...]
    minimum_confidence: Decimal
    plant_protein: SupplementProfile
    soya: SupplementProfile | None
    soya_step_grams: int
    maximum_soya_grams: int
    plant_protein_meal: str
    soya_meal: str


@dataclass(frozen=True)
class MacroTotals:
    calories_kcal: Decimal
    protein_g: Decimal
    carbohydrates_g: Decimal | None
    fat_g: Decimal | None


@dataclass(frozen=True)
class OptimizationResult:
    status: PlanStatus
    portions: tuple[SelectedPortion, ...]
    totals: MacroTotals
    fixed_plant_protein_scoops: int
    soya_dry_grams: Decimal
    protein_shortfall_g: Decimal
    excluded_items: tuple[tuple[str, str, str], ...]
    missing_nutrition: tuple[str, ...]
    message: str
    meal_calories_kcal: tuple[tuple[str, Decimal], ...] = ()
    meal_protein_g: tuple[tuple[str, Decimal], ...] = ()
    objective_values: tuple[tuple[str, Decimal], ...] = ()
    soft_preference_deviations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    meal_coverage: tuple[MealCoverage, ...] = ()
    validation_context: PlanValidationContext | None = field(default=None, repr=False, compare=False)

    @property
    def is_valid_target_plan(self) -> bool:
        return self.status is PlanStatus.FEASIBLE and not validate_optimization_result(self)


def menu_items_from_snapshot(snapshot: Mapping[str, object]) -> tuple[MenuItem, ...]:
    meals = snapshot.get("meals")
    if not isinstance(meals, Mapping):
        raise ValueError("snapshot must contain meal mapping")
    result: list[MenuItem] = []
    for meal, section in meals.items():
        if not isinstance(section, Mapping) or not isinstance(section.get("items"), list):
            raise ValueError(f"malformed menu section {meal!r}")
        for label in section["items"]:
            if not isinstance(label, str):
                raise ValueError(f"menu item label in {meal!r} must be a string")
            food = lookup_food(label)
            # Keep unknowns visible to solve() for the exclusion report.
            if food is None:
                result.append(MenuItem(str(meal), _unknown_record(label)))
            else:
                result.append(MenuItem(str(meal), food))
    return tuple(result)


def _unknown_record(label: str) -> FoodRecord:
    # A local sentinel with no nutrient profile; it can only be excluded.
    from .models import FoodRecord
    return FoodRecord(
        key=f"unknown:{label}", display_name=label, category=FoodCategory.INGREDIENT,
        aliases=(label,), nutrition_per_100g=None, serving=None,
        confidence=Decimal("0"), source=None, review_required=True,
        notes="No exact curated catalog match.",
    )


class DietOptimizer:
    """CP-SAT optimizer for menu foods plus one fixed plant-isolate serving.

    Confidence policy: only known records at/above ``minimum_confidence`` are
    eligible. Foods are selected in typical-serving increments. Soya is never
    part of the menu catalog; it is available solely through its explicit
    product-label profile and is minimized before other soft objectives.
    Household-serving counts and meal calorie windows are guidance only.
    """

    SCALE = 100  # Decimal hundredths -> CP-SAT integers.

    def __init__(
        self,
        profile: UserNutritionProfile | None = None,
        *,
        plant_protein: SupplementProfile = FIXED_PLANT_PROTEIN,
        soya: SupplementProfile | None = SOYA_CHUNKS_LABEL,
        minimum_confidence: Decimal = Decimal("0.50"),
        soya_step_grams: int = 5,
        maximum_soya_grams: int = 70,
        meal_guidance: Mapping[str, MealGuidance] | None = None,
        meal_protein_guidance: Mapping[str, MealProteinGuidance] | None = None,
        portion_guidance: Mapping[str, PortionGuidance] | None = None,
        portion_limits: Mapping[str, PortionLimits] | None = None,
        milk_guidance: PortionGuidance = MILK_PORTION_GUIDANCE,
        plant_protein_meal: str = "Breakfast",
        soya_meal: str | None = None,
        max_time_seconds: float = 60.0,
    ) -> None:
        if not Decimal("0") <= minimum_confidence <= Decimal("1"):
            raise ValueError("minimum_confidence must be in [0, 1]")
        if soya_step_grams <= 0 or maximum_soya_grams < 0:
            raise ValueError("invalid soya portion limits")
        if maximum_soya_grams > 70:
            raise ValueError("daily dry soya quantity cannot exceed 70 g")
        if max_time_seconds <= 0:
            raise ValueError("max_time_seconds must be positive")
        if soya is not None and soya.name.casefold() != "soya chunks":
            raise ValueError("the only permitted supplemental protein is soya chunks")
        if soya is not None and (soya.carbohydrates_g is None or soya.fat_g is None):
            raise ValueError("soya label must include calories, protein, carbohydrates, and fat per 100 g dry")
        if soya is not None and "dry" not in soya.basis.casefold():
            raise ValueError("soya profile basis must confirm dry-weight label values")
        if soya is not None and soya.protein_g + soya.carbohydrates_g + soya.fat_g > Decimal("100.5"):
            raise ValueError("soya label macros per 100 g cannot exceed 100 g")
        if soya is not None and soya.protein_g <= 0:
            raise ValueError("soya label must provide positive protein per 100 g dry")
        if plant_protein.name.casefold() != "plant protein isolate" or (
            plant_protein.calories_kcal, plant_protein.protein_g,
            plant_protein.carbohydrates_g, plant_protein.fat_g,
        ) != (Decimal("132"), Decimal("29"), Decimal("2"), Decimal("0.48")):
            raise ValueError("the fixed plant isolate must remain exactly one 35 g scoop: 132 kcal, P29, C2, F0.48")
        if profile is None:
            raise ValueError("UserNutritionProfile is required; load it from private runtime configuration")
        self.profile = profile
        self.plant_protein = plant_protein
        self.soya = soya
        self.minimum_confidence = minimum_confidence
        self.soya_step_grams = soya_step_grams
        self.maximum_soya_grams = maximum_soya_grams
        self.meal_guidance = {**DEFAULT_MEAL_GUIDANCE, **(meal_guidance or {})}
        target_protein = self.profile.targets.minimum_protein_g
        self.meal_protein_guidance = {
            meal: MealProteinGuidance(target_protein * low_share, target_protein * high_share)
            for meal, (low_share, high_share) in _DEFAULT_PROTEIN_SHARES.items()
            if meal in self.meal_guidance
        }
        self.meal_protein_guidance.update(meal_protein_guidance or {})
        self.portion_guidance = {**DEFAULT_PORTION_GUIDANCE, **(portion_guidance or {})}
        self.portion_limits = {**DEFAULT_PORTION_LIMITS, **(portion_limits or {})}
        self.milk_guidance = milk_guidance
        if plant_protein_meal not in self.meal_guidance:
            raise ValueError("plant protein meal must exist in meal guidance")
        if soya_meal is None:
            soya_meal = next((meal for meal in ("Snacks", "Lunch", plant_protein_meal) if meal in self.meal_guidance), plant_protein_meal)
        if self.soya is not None and soya_meal not in self.meal_guidance:
            raise ValueError("soya meal must exist in meal guidance")
        self.plant_protein_meal = plant_protein_meal
        self.soya_meal = soya_meal
        self.max_time_seconds = max_time_seconds

    @classmethod
    def _units(cls, value: Decimal | int | str) -> int:
        return int((Decimal(str(value)) * cls.SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @classmethod
    def _decimal(cls, units: int) -> Decimal:
        return Decimal(units) / cls.SCALE

    def optimize(self, menu: Sequence[MenuItem]) -> OptimizationResult:
        menu = tuple(menu)
        eligible: list[MenuItem] = []
        excluded: list[tuple[str, str, str]] = []
        missing: list[str] = []
        for item in menu:
            food = item.food
            if item.meal not in self.meal_guidance:
                excluded.append((item.meal, food.display_name, "Meal section has no configured guidance."))
            elif food.nutrition_per_100g is None:
                reason = food.notes or "Nutrition unavailable; cannot optimize this item."
                excluded.append((item.meal, food.display_name, reason))
                missing.append(f"{item.meal}: {food.display_name}")
            elif food.confidence < self.minimum_confidence:
                excluded.append((item.meal, food.display_name,
                                 f"confidence {food.confidence} is below safety threshold {self.minimum_confidence}"))
            elif food.serving is None:
                excluded.append((item.meal, food.display_name, "No serving weight is defined."))
            elif food.key not in self.portion_limits:
                excluded.append((item.meal, food.display_name,
                                 "No food-specific safe serving limit is configured."))
            else:
                eligible.append(item)

        eligible_required_meals = tuple(
            meal for meal in REQUIRED_MEALS if any(item.meal == meal for item in eligible)
        )
        hard = self._run(eligible, require_protein=True, enforce_minimum_calories=True,
                         required_meals=eligible_required_meals)
        if hard is not None:
            portions, soya_g, totals, objective_values, records, meal_calories, optimal = hard
            self._validate(portions, totals, soya_g, records=records,
                           require_protein=True, enforce_minimum_calories=True)
            coverage = self._meal_coverage(menu, eligible, portions, excluded)
            meal_protein = self._recalculate_meal_protein(portions, soya_g, records)
            coverage_complete = all(item.available for item in coverage)
            status = PlanStatus.FEASIBLE if coverage_complete else PlanStatus.INFEASIBLE
            message = self._complete_message(totals, soya_g) if coverage_complete else self._coverage_message(coverage, totals)
            deviations, warnings = self._result_notes(
                portions, totals, meal_calories, meal_protein, excluded, missing,
                status=status, optimal=optimal, meal_coverage=coverage,
            )
            result = OptimizationResult(
                status=status, portions=portions, totals=totals,
                fixed_plant_protein_scoops=1, soya_dry_grams=soya_g,
                protein_shortfall_g=Decimal("0"), excluded_items=tuple(excluded),
                missing_nutrition=tuple(sorted(set(missing))),
                message=message,
                meal_calories_kcal=meal_calories, meal_protein_g=meal_protein,
                objective_values=objective_values,
                soft_preference_deviations=deviations, warnings=warnings,
                meal_coverage=coverage,
                validation_context=self._validation_context(menu, coverage),
            )
            if status is PlanStatus.FEASIBLE:
                errors = validate_optimization_result(result)
                if errors:
                    raise OptimizationValidationError("final plan validation failed: " + "; ".join(errors))
            return result

        # The hard model is infeasible. Produce a clearly labeled diagnostic
        # that maximizes achievable protein while preserving the calorie ceiling.
        fallback = self._run(eligible, require_protein=False, enforce_minimum_calories=True,
                             required_meals=eligible_required_meals)
        if fallback is None:
            fallback = self._run(eligible, require_protein=False, enforce_minimum_calories=False,
                                 required_meals=eligible_required_meals)
        if fallback is None:
            # If complete meal coverage itself conflicts with the calorie
            # ceiling, keep a clearly diagnostic partial solution if possible.
            fallback = self._run(eligible, require_protein=False, enforce_minimum_calories=False)
        if fallback is None:
            totals = MacroTotals(
                self.plant_protein.calories_kcal, self.plant_protein.protein_g,
                self.plant_protein.carbohydrates_g, self.plant_protein.fat_g,
            )
            coverage = self._meal_coverage(menu, eligible, (), excluded)
            meal_protein = self._recalculate_meal_protein((), Decimal("0"), {})
            message = "No menu combination fits the hard calorie ceiling, even before requiring the protein minimum."
            if any(not entry.available for entry in coverage):
                message += " " + self._coverage_message(coverage, totals)
            deviations, warnings = self._result_notes(
                (), totals, self._recalculate_meal_calories((), Decimal("0"), {}), meal_protein,
                excluded, missing, status=PlanStatus.INFEASIBLE, optimal=True,
                meal_coverage=coverage,
            )
            return OptimizationResult(
                status=PlanStatus.INFEASIBLE, portions=(), totals=totals,
                fixed_plant_protein_scoops=1, soya_dry_grams=Decimal("0"),
                protein_shortfall_g=self.profile.targets.minimum_protein_g,
                excluded_items=tuple(excluded), missing_nutrition=tuple(sorted(set(missing))),
                message=message, meal_calories_kcal=tuple(),
                soft_preference_deviations=deviations, warnings=warnings,
                meal_protein_g=meal_protein,
                meal_coverage=coverage,
                validation_context=self._validation_context(menu, coverage),
            )

        portions, soya_g, totals, objective_values, records, meal_calories, optimal = fallback
        coverage = self._meal_coverage(menu, eligible, portions, excluded)
        meal_protein = self._recalculate_meal_protein(portions, soya_g, records)
        shortfall = max(Decimal("0"), self.profile.targets.minimum_protein_g - totals.protein_g)
        calories_valid = totals.calories_kcal >= self.profile.targets.minimum_calories_kcal
        self._validate(portions, totals, soya_g, records=records, require_protein=False,
                       enforce_minimum_calories=calories_valid)
        coverage_complete = all(item.available for item in coverage)
        if not coverage_complete:
            status = PlanStatus.INFEASIBLE
            detail = self._coverage_message(coverage, totals)
            if shortfall:
                detail += f" Protein is also {shortfall} g below the required minimum."
            message = detail
        elif not calories_valid:
            status = PlanStatus.INFEASIBLE
            message = (f"No plan met the {self.profile.targets.minimum_calories_kcal} kcal minimum. "
                       f"Best diagnostic plan provides {totals.calories_kcal} kcal and {totals.protein_g} g protein.")
        elif shortfall > 0:
            status = PlanStatus.PROTEIN_SHORTFALL
            message = (f"No plan met {self.profile.targets.minimum_protein_g} g protein within the calorie limits. "
                       f"Best diagnostic plan provides {totals.protein_g} g protein "
                       f"({shortfall} g shortfall) at {totals.calories_kcal} kcal.")
        else:
            status = PlanStatus.FEASIBLE
            message = self._complete_message(totals, soya_g)
        deviations, warnings = self._result_notes(
            portions, totals, meal_calories, meal_protein, excluded, missing,
            status=status, optimal=optimal, meal_coverage=coverage,
        )
        return OptimizationResult(
            status=status, portions=portions, totals=totals,
            fixed_plant_protein_scoops=1, soya_dry_grams=soya_g,
            protein_shortfall_g=shortfall, excluded_items=tuple(excluded),
            missing_nutrition=tuple(sorted(set(missing))), message=message,
            meal_calories_kcal=meal_calories, meal_protein_g=meal_protein,
            objective_values=objective_values,
            soft_preference_deviations=deviations, warnings=warnings,
            meal_coverage=coverage,
            validation_context=self._validation_context(menu, coverage),
        )

    def _run(self, eligible: Sequence[MenuItem], *, require_protein: bool,
             enforce_minimum_calories: bool,
             required_meals: Sequence[str] = ()) -> tuple[tuple[SelectedPortion, ...], Decimal, MacroTotals,
             tuple[tuple[str, Decimal], ...], Mapping[str, FoodRecord],
             tuple[tuple[str, Decimal], ...], bool] | None:
        targets = self.profile.targets
        model = cp_model.CpModel()
        serving_vars: dict[str, cp_model.IntVar] = {}
        selected_vars: dict[str, cp_model.IntVar] = {}
        item_map: dict[str, MenuItem] = {}
        daily_food_portions: dict[str, list[cp_model.IntVar]] = {}
        meal_food_portions: dict[tuple[str, str], list[cp_model.IntVar]] = {}
        meal_food_calories: dict[str, list[object]] = {meal: [] for meal in self.meal_guidance}
        meal_kcal_domain_upper: dict[str, int] = {meal: 0 for meal in self.meal_guidance}
        daily_food_selected: dict[str, list[cp_model.IntVar]] = {}
        food_serving_domain: dict[str, int] = {}

        max_kcal_units = self._units(targets.maximum_calories_kcal)
        for index, item in enumerate(eligible):
            key = f"{item.key}:{index}"
            food = item.food
            assert food.nutrition_per_100g is not None and food.serving is not None
            kcal_per_serving_units = self._units(
                food.nutrition_per_100g.calories_kcal * food.serving.grams / Decimal("100")
            )
            hard_limit = self._portion_limit(food.key)
            item_max = min(self._safe_serving_domain(food), hard_limit.servings_per_meal)
            food_serving_domain[food.key] = food_serving_domain.get(food.key, 0) + item_max
            portion = model.new_int_var(0, item_max, f"servings_{index}")
            active = model.new_bool_var(f"selected_{index}")
            model.add(portion >= active)
            model.add(portion <= item_max * active)
            serving_vars[key], selected_vars[key], item_map[key] = portion, active, item
            daily_food_portions.setdefault(food.key, []).append(portion)
            meal_food_portions.setdefault((food.key, item.meal), []).append(portion)
            daily_food_selected.setdefault(food.key, []).append(active)
            meal_food_calories[item.meal].append(kcal_per_serving_units * portion)
            meal_kcal_domain_upper[item.meal] += kcal_per_serving_units * item_max

        # A meal with eligible menu food must contain at least one mess-food
        # serving. Missing/unsafe meals are reported separately as unavailable.
        for meal in required_meals:
            active_meal_items = [
                active for key, active in selected_vars.items() if item_map[key].meal == meal
            ]
            if active_meal_items:
                model.add(sum(active_meal_items) >= 1)

        # Enforce configured food-specific daily and per-meal maximum servings.
        for food_key, vars_for_food in daily_food_portions.items():
            model.add(sum(vars_for_food) <= self._portion_limit(food_key).servings_per_day)
        for (food_key, meal), vars_for_meal in meal_food_portions.items():
            model.add(sum(vars_for_meal) <= self._portion_limit(food_key).servings_per_meal)

        soya_units: cp_model.IntVar | None = None
        soya_cap = self.maximum_soya_grams // self.soya_step_grams
        if self.soya is not None and soya_cap > 0:
            soya_units = model.new_int_var(0, soya_cap, "soya_dry_portion_steps")

        meal_kcal_expressions: dict[str, object] = {}
        for meal in self.meal_guidance:
            supplements = 0
            if meal == self.plant_protein_meal:
                supplements += self._units(self.plant_protein.calories_kcal)
                meal_kcal_domain_upper[meal] += self._units(self.plant_protein.calories_kcal)
            if meal == self.soya_meal and soya_units is not None and self.soya is not None:
                per_step = self.soya.calories_kcal * Decimal(self.soya_step_grams) / Decimal("100")
                meal_food_calories[meal].append(self._units(per_step) * soya_units)
                meal_kcal_domain_upper[meal] += self._units(per_step) * soya_cap
            meal_kcal_expressions[meal] = sum(meal_food_calories[meal]) + supplements

        meal_deviations: list[cp_model.IntVar] = []
        for meal, guidance in self.meal_guidance.items():
            expression = meal_kcal_expressions[meal]
            minimum_units = self._units(guidance.minimum_calories_kcal)
            maximum_units = self._units(guidance.maximum_calories_kcal)
            minimum_possible = (
                self._units(self.plant_protein.calories_kcal)
                if meal == self.plant_protein_meal else 0
            )
            below_bound = max(0, minimum_units - minimum_possible)
            above_bound = max(0, meal_kcal_domain_upper[meal] - maximum_units)
            below = model.new_int_var(0, below_bound, f"{meal.lower()}_below_window")
            above = model.new_int_var(0, above_bound, f"{meal.lower()}_above_window")
            model.add(below >= minimum_units - expression)
            model.add(above >= expression - maximum_units)
            meal_deviations.extend((below, above))

        def macro_expr(field_name: str):
            terms = []
            for key, var in serving_vars.items():
                food = item_map[key].food
                assert food.nutrition_per_100g is not None and food.serving is not None
                per_portion = getattr(food.nutrition_per_100g, field_name) * food.serving.grams / Decimal("100")
                terms.append(self._units(per_portion) * var)
            fixed_value = getattr(self.plant_protein, field_name)
            fixed = self._units(fixed_value) if fixed_value is not None else 0
            if soya_units is not None and self.soya is not None:
                soya_value = getattr(self.soya, field_name)
                per_step = soya_value * Decimal(self.soya_step_grams) / Decimal("100") if soya_value is not None else Decimal("0")
                terms.append(self._units(per_step) * soya_units)
            return sum(terms) + fixed

        kcal = macro_expr("calories_kcal")
        protein = macro_expr("protein_g")
        fat_known = self.plant_protein.fat_g is not None and (self.soya is None or self.soya.fat_g is not None)
        carbs_known = self.plant_protein.carbohydrates_g is not None and (self.soya is None or self.soya.carbohydrates_g is not None)
        fat = macro_expr("fat_g") if fat_known else None
        carbs = macro_expr("carbohydrates_g") if carbs_known else None
        model.add(kcal <= max_kcal_units)
        if enforce_minimum_calories:
            model.add(kcal >= self._units(targets.minimum_calories_kcal))
        if require_protein:
            model.add(protein >= self._units(targets.minimum_protein_g))

        meal_protein_terms: dict[str, list[object]] = {meal: [] for meal in self.meal_guidance}
        meal_protein_upper: dict[str, int] = {meal: 0 for meal in self.meal_guidance}
        for key, portion in serving_vars.items():
            item = item_map[key]
            assert item.food.nutrition_per_100g is not None and item.food.serving is not None
            protein_per_serving = self._units(
                item.food.nutrition_per_100g.protein_g * item.food.serving.grams / Decimal("100")
            )
            meal_protein_terms[item.meal].append(protein_per_serving * portion)
            meal_protein_upper[item.meal] += protein_per_serving * self._domain_upper(portion)
        if self.plant_protein_meal in meal_protein_terms:
            isolate_units = self._units(self.plant_protein.protein_g)
            meal_protein_terms[self.plant_protein_meal].append(isolate_units)
            meal_protein_upper[self.plant_protein_meal] += isolate_units
        if soya_units is not None and self.soya is not None and self.soya_meal in meal_protein_terms:
            soya_protein_per_step = self._units(
                self.soya.protein_g * Decimal(self.soya_step_grams) / Decimal("100")
            )
            meal_protein_terms[self.soya_meal].append(soya_protein_per_step * soya_units)
            meal_protein_upper[self.soya_meal] += soya_protein_per_step * soya_cap

        meal_protein_expressions: dict[str, object] = {
            meal: sum(terms) for meal, terms in meal_protein_terms.items()
        }
        protein_distribution_deviations: list[cp_model.IntVar] = []
        for meal, guidance in self.meal_protein_guidance.items():
            if meal not in meal_protein_expressions:
                continue
            minimum_units = self._units(guidance.minimum_protein_g)
            maximum_units = self._units(guidance.maximum_protein_g)
            below = model.new_int_var(
                0, minimum_units, f"{meal.lower()}_protein_below_guidance"
            )
            above = model.new_int_var(
                # Keep a conservative bound based on the full possible meal
                # protein expression. Subtracting the preferred maximum here
                # proved brittle when the expression includes fixed isolate
                # protein; guidance must remain soft and never constrain a
                # nutritionally feasible plan by an undersized deviation var.
                0, meal_protein_upper[meal], f"{meal.lower()}_protein_above_guidance"
            )
            expression = meal_protein_expressions[meal]
            model.add(below >= minimum_units - expression)
            model.add(above >= expression - maximum_units)
            protein_distribution_deviations.extend((below, above))

        # Soft portion guidance discourages high reliance on one dish, but never
        # makes a serving quantity unavailable to the solver.
        portion_excess: list[cp_model.IntVar] = []
        for food_key, vars_for_food in daily_food_portions.items():
            guidance = self._portion_guidance(food_key)
            # Keep advisory excess variables genuinely soft. Derive their finite
            # upper bounds from the already calorie-derived serving domains.
            max_possible = food_serving_domain[food_key]
            excess = model.new_int_var(0, max(0, max_possible), f"{food_key}_daily_guidance_excess")
            model.add(excess >= sum(vars_for_food) - guidance.servings_per_day)
            portion_excess.append(excess)
        for (food_key, meal), vars_for_meal in meal_food_portions.items():
            guidance = self._portion_guidance(food_key)
            max_possible = sum(
                self._safe_serving_domain(item_map[key].food)
                for key, var in serving_vars.items()
                if item_map[key].food.key == food_key and item_map[key].meal == meal
            )
            excess = model.new_int_var(0, max(0, max_possible), f"{meal.lower()}_{food_key}_guidance_excess")
            model.add(excess >= sum(vars_for_meal) - guidance.servings_per_meal)
            portion_excess.append(excess)

        # Hot and cold milk are two menu names for the same beverage. Count
        # their portions together for advice so changing the label does not
        # evade the practical serving guidance.
        for meal in self.meal_guidance:
            milk_vars = [
                var for (food_key, item_meal), vars_for_meal in meal_food_portions.items()
                if food_key in MILK_FOOD_KEYS and item_meal == meal
                for var in vars_for_meal
            ]
            if milk_vars:
                model.add(sum(milk_vars) <= MILK_PORTION_LIMITS.servings_per_meal)
                max_possible = sum(
                    self._safe_serving_domain(item_map[key].food)
                    for key in serving_vars
                    if item_map[key].food.key in MILK_FOOD_KEYS and item_map[key].meal == meal
                )
                excess = model.new_int_var(0, max_possible, f"{meal.lower()}_milk_group_guidance_excess")
                model.add(excess >= sum(milk_vars) - self.milk_guidance.servings_per_meal)
                portion_excess.extend((excess, excess, excess))
        milk_vars_daily = [
            var for (food_key, _), vars_for_meal in meal_food_portions.items()
            if food_key in MILK_FOOD_KEYS
            for var in vars_for_meal
        ]
        if milk_vars_daily:
            model.add(sum(milk_vars_daily) <= MILK_PORTION_LIMITS.servings_per_day)
            max_possible = sum(
                self._safe_serving_domain(item_map[key].food)
                for key in serving_vars if item_map[key].food.key in MILK_FOOD_KEYS
            )
            excess = model.new_int_var(0, max_possible, "daily_milk_group_guidance_excess")
            model.add(excess >= sum(milk_vars_daily) - self.milk_guidance.servings_per_day)
            portion_excess.extend((excess, excess, excess))

        distinct_food_vars: list[cp_model.IntVar] = []
        for food_key, flags in daily_food_selected.items():
            is_used = model.new_bool_var(f"daily_food_selected_{food_key}")
            model.add(sum(flags) >= is_used)
            model.add(sum(flags) <= len(flags) * is_used)
            distinct_food_vars.append(is_used)

        stages: list[tuple[str, object, str]] = []
        if require_protein and soya_units is not None:
            soya_stage = ("soya_minimization", soya_units, "min")
        else:
            soya_stage = None
        # Practical dish limits are hard constraints. Soft portion preference
        # and soya minimization then keep valid solutions from clustering.
        stages.append(("portion_guidance", sum(portion_excess), "min"))
        if soya_stage is not None:
            stages.append(soya_stage)
        stages.extend((
            ("meal_distribution", sum(meal_deviations), "min"),
            ("protein_distribution", sum(protein_distribution_deviations), "min"),
        ))
        if require_protein:
            # Portion and meal distribution preference may take priority over
            # the soft 1900 kcal / 55 g fat aims.
            kcal_deviation = model.new_int_var(0, max_kcal_units, "preferred_calorie_deviation")
            model.add_abs_equality(kcal_deviation, kcal - self._units(targets.preferred_calories_kcal))
            stages.append(("calorie_target_deviation", kcal_deviation, "min"))
            stages.extend((
                ("food_variety", sum(distinct_food_vars), "max"),
            ))
            if fat is not None:
                fat_deviation = model.new_int_var(0, max_kcal_units, "preferred_fat_deviation")
                model.add_abs_equality(fat_deviation, fat - self._units(targets.preferred_fat_g))
                stages.append(("fat_target_deviation", fat_deviation, "min"))
            stretch = model.new_int_var(0, self._units(targets.stretch_protein_g), "optional_protein_stretch")
            model.add_min_equality(stretch, [protein, self._units(targets.stretch_protein_g)])
            stages.append(("optional_protein_stretch", stretch, "max"))
            if carbs is not None:
                stages.append(("remaining_carbohydrates", carbs, "max"))
        else:
            stages.append(("achievable_protein", protein, "max"))
            stages.append(("portion_guidance", sum(portion_excess), "min"))
            if soya_units is not None:
                stages.append(("minimum_soya_at_best_protein", soya_units, "min"))
            stages.extend((
                ("meal_distribution", sum(meal_deviations), "min"),
                ("protein_distribution", sum(protein_distribution_deviations), "min"),
            ))
            kcal_deviation = model.new_int_var(0, max_kcal_units, "preferred_calorie_deviation")
            model.add_abs_equality(kcal_deviation, kcal - self._units(targets.preferred_calories_kcal))
            stages.append(("calorie_target_deviation", kcal_deviation, "min"))
            if fat is not None:
                fat_deviation = model.new_int_var(0, max_kcal_units, "preferred_fat_deviation")
                model.add_abs_equality(fat_deviation, fat - self._units(targets.preferred_fat_g))
                stages.append(("fat_target_deviation", fat_deviation, "min"))
            stages.append(("food_variety", sum(distinct_food_vars), "max"))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_time_seconds
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        stage_values: list[tuple[str, Decimal]] = []
        all_optimal = True
        # Find a hard-valid starting point before optimizing preferences. With
        # wide calorie-derived domains this avoids mistaking a time-limited
        # preference search for nutritional infeasibility.
        status = solver.solve(model)
        if status == cp_model.INFEASIBLE:
            return None
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise OptimizationError(
                f"CP-SAT could not find a hard-constraint-feasible starting point (status={solver.status_name(status)})."
            )
        decision_vars = list(serving_vars.values()) + list(selected_vars.values())
        if soya_units is not None:
            decision_vars.append(soya_units)
        for variable in decision_vars:
            model.add_hint(variable, solver.value(variable))
        for name, expression, direction in stages:
            model.minimize(expression) if direction == "min" else model.maximize(expression)
            status = solver.solve(model)
            if status == cp_model.INFEASIBLE:
                return None
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                # A prior hint/optimum supplies a feasible plan. Stop improving
                # soft objectives and retain a freshly validated incumbent.
                model.clear_objective()
                status = solver.solve(model)
                if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                    raise OptimizationError(
                        f"CP-SAT could not restore a hard-valid incumbent after preference search (status={solver.status_name(status)})."
                    )
                all_optimal = False
                break
            if status != cp_model.OPTIMAL:
                all_optimal = False
            value = solver.value(expression)
            if name == "soya_minimization":
                display_value = Decimal(value * self.soya_step_grams)
            elif name in {"portion_guidance", "food_variety"}:
                display_value = Decimal(value)
            else:
                display_value = self._decimal(value)
            stage_values.append((name, display_value))
            model.add(expression == value)

        status = solver.solve(model)
        if status == cp_model.INFEASIBLE:
            return None
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            model.clear_objective()
            status = solver.solve(model)
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                raise OptimizationError(
                    f"CP-SAT could not finalize a hard-valid result before the search limit (status={solver.status_name(status)})."
                )
            all_optimal = False
        if status != cp_model.OPTIMAL:
            all_optimal = False
        portions: list[SelectedPortion] = []
        for key, var in serving_vars.items():
            count = solver.value(var)
            if not count:
                continue
            item = item_map[key]
            assert item.food.serving is not None
            total_grams = item.food.serving.grams * count
            portions.append(SelectedPortion(item.meal, item.food.key, item.food.display_name,
                count, item.food.serving.grams, total_grams, item.food.confidence))
        soya_grams = Decimal(solver.value(soya_units) * self.soya_step_grams) if soya_units is not None else Decimal("0")
        records = {item.food.key: item.food for item in item_map.values()}
        totals = self._recalculate(portions, soya_grams, records)
        meal_calories = self._recalculate_meal_calories(portions, soya_grams, records)
        return tuple(portions), soya_grams, totals, tuple(stage_values), records, meal_calories, all_optimal

    def _safe_serving_domain(self, food: FoodRecord) -> int:
        """Return a CP-SAT domain bound implied only by the daily kcal ceiling."""
        assert food.nutrition_per_100g is not None and food.serving is not None
        per_serving = food.nutrition_per_100g.calories_kcal * food.serving.grams / Decimal("100")
        per_serving_units = self._units(per_serving)
        if per_serving <= 0 or per_serving_units <= 0:
            raise OptimizationError(
                f"Cannot derive a safe calorie-based solver domain for {food.display_name}: "
                "per-serving calories must be positive at solver precision."
            )
        # This is not a mess portion cap: any plan above the bound would exceed
        # the hard daily calorie ceiling even if this were the only selected food.
        return self._units(self.profile.targets.maximum_calories_kcal) // per_serving_units

    def _domain_upper(self, variable: cp_model.IntVar) -> int:
        return variable.proto.domain[-1]

    def _portion_guidance(self, food_key: str) -> PortionGuidance:
        return self.portion_guidance.get(food_key, self.portion_guidance["default"])

    def _portion_limit(self, food_key: str) -> PortionLimits:
        return self.portion_limits[food_key]

    def _meal_coverage(self, menu, eligible, portions, excluded) -> tuple[MealCoverage, ...]:
        menu_meals = {meal: [item for item in menu if item.meal == meal] for meal in REQUIRED_MEALS}
        eligible_meals = {meal: [item for item in eligible if item.meal == meal] for meal in REQUIRED_MEALS}
        selected_meals = {portion.meal for portion in portions}
        coverage: list[MealCoverage] = []
        for meal in REQUIRED_MEALS:
            if not menu_meals[meal]:
                coverage.append(MealCoverage(meal, False, "No menu items were published for this meal."))
            elif meal in selected_meals:
                coverage.append(MealCoverage(meal, True))
            elif not eligible_meals[meal]:
                excluded_for_meal = [
                    f"{name}: {reason}" for excluded_meal, name, reason in excluded if excluded_meal == meal
                ]
                detail = " " + "; ".join(excluded_for_meal[:2]) if excluded_for_meal else ""
                coverage.append(MealCoverage(
                    meal, False,
                    f"No menu food passed nutrition, confidence ≥ {self.minimum_confidence}, serving-weight, and portion-limit checks.{detail}",
                ))
            else:
                coverage.append(MealCoverage(
                    meal, False,
                    "Available foods could not be selected while satisfying the hard calorie, protein, and portion constraints.",
                ))
        return tuple(coverage)

    def _coverage_message(self, coverage: Sequence[MealCoverage], totals: MacroTotals) -> str:
        unavailable = [item for item in coverage if not item.available]
        if not unavailable:
            return ""
        detail = "; ".join(f"{item.meal}: {item.reason}" for item in unavailable)
        return (
            f"No complete Breakfast/Lunch/Dinner plan is available ({detail}). "
            f"Diagnostic partial totals are {totals.calories_kcal} kcal and {totals.protein_g} g protein; not deliverable."
        )

    def _validation_context(self, menu, coverage) -> PlanValidationContext:
        return PlanValidationContext(
            targets=self.profile.targets,
            required_meals=REQUIRED_MEALS,
            meal_names=tuple(self.meal_guidance),
            menu_inventory=tuple(menu),
            meal_coverage=tuple(coverage),
            portion_limits=tuple(sorted(self.portion_limits.items())),
            minimum_confidence=self.minimum_confidence,
            plant_protein=self.plant_protein,
            soya=self.soya,
            soya_step_grams=self.soya_step_grams,
            maximum_soya_grams=self.maximum_soya_grams,
            plant_protein_meal=self.plant_protein_meal,
            soya_meal=self.soya_meal,
        )

    def _result_notes(self, portions, totals, meal_calories, meal_protein, excluded, missing,
                      *, status, optimal, meal_coverage=()):
        targets = self.profile.targets
        deviations: list[str] = []
        warnings: list[str] = []
        calorie_delta = totals.calories_kcal - targets.preferred_calories_kcal
        if calorie_delta:
            direction = "above" if calorie_delta > 0 else "below"
            deviations.append(f"Daily calories are {abs(calorie_delta)} kcal {direction} the {targets.preferred_calories_kcal} kcal preference.")
        if totals.fat_g is not None:
            fat_delta = totals.fat_g - targets.preferred_fat_g
            if fat_delta:
                direction = "above" if fat_delta > 0 else "below"
                deviations.append(f"Fat is {abs(fat_delta)} g {direction} the {targets.preferred_fat_g} g preference.")
        if totals.protein_g < targets.stretch_protein_g:
            deviations.append(f"Optional protein stretch is {targets.stretch_protein_g - totals.protein_g} g short; this is not a hard requirement.")
        meal_values = dict(meal_calories)
        for meal, guidance in self.meal_guidance.items():
            calories = meal_values.get(meal, Decimal("0"))
            if calories < guidance.minimum_calories_kcal:
                deviations.append(f"{meal} is {guidance.minimum_calories_kcal - calories} kcal below its soft meal window.")
                warnings.append(f"{meal} is below its soft preferred calorie window ({guidance.minimum_calories_kcal}–{guidance.maximum_calories_kcal} kcal).")
            elif calories > guidance.maximum_calories_kcal:
                deviations.append(f"{meal} is {calories - guidance.maximum_calories_kcal} kcal above its soft meal window.")
                warnings.append(f"{meal} is above its soft preferred calorie window ({guidance.minimum_calories_kcal}–{guidance.maximum_calories_kcal} kcal).")

        protein_values = dict(meal_protein)
        for meal, guidance in self.meal_protein_guidance.items():
            protein = protein_values.get(meal, Decimal("0"))
            if protein < guidance.minimum_protein_g:
                deviations.append(
                    f"{meal} protein is {guidance.minimum_protein_g - protein} g below its soft distribution range."
                )
                warnings.append(
                    f"{meal} protein is below the preferred {guidance.minimum_protein_g}–{guidance.maximum_protein_g} g range."
                )
            elif protein > guidance.maximum_protein_g:
                deviations.append(
                    f"{meal} protein is {protein - guidance.maximum_protein_g} g above its soft distribution range."
                )
                warnings.append(
                    f"{meal} protein is above the preferred {guidance.minimum_protein_g}–{guidance.maximum_protein_g} g range."
                )

        for coverage in meal_coverage:
            if not coverage.available:
                warnings.append(f"{coverage.meal} unavailable: {coverage.reason}")

        daily_counts: dict[str, int] = {}
        meal_counts: dict[tuple[str, str], int] = {}
        names: dict[str, str] = {}
        for portion in portions:
            daily_counts[portion.food_key] = daily_counts.get(portion.food_key, 0) + portion.servings
            meal_counts[(portion.food_key, portion.meal)] = meal_counts.get((portion.food_key, portion.meal), 0) + portion.servings
            names[portion.food_key] = portion.food_name
        for food_key, count in sorted(daily_counts.items()):
            guidance = self._portion_guidance(food_key)
            if count > guidance.servings_per_day:
                warnings.append(
                    f"Large-portion guidance: {names[food_key]} is {count} standard servings/day "
                    f"(soft guide {guidance.servings_per_day}; hard limit {self._portion_limit(food_key).servings_per_day})."
                )
        for (food_key, meal), count in sorted(meal_counts.items()):
            guidance = self._portion_guidance(food_key)
            if count > guidance.servings_per_meal:
                warnings.append(
                    f"Large-portion guidance: {names[food_key]} is {count} standard servings in {meal} "
                    f"(soft guide {guidance.servings_per_meal}; hard limit {self._portion_limit(food_key).servings_per_meal})."
                )
        milk_daily = sum(count for food_key, count in daily_counts.items() if food_key in MILK_FOOD_KEYS)
        if milk_daily > self.milk_guidance.servings_per_day:
            warnings.append(
                f"Large-portion guidance: milk drinks total {milk_daily} cups/day "
                f"(soft guide {self.milk_guidance.servings_per_day}; hard limit {MILK_PORTION_LIMITS.servings_per_day})."
            )
        for meal in self.meal_guidance:
            milk_count = sum(count for (food_key, m), count in meal_counts.items()
                             if food_key in MILK_FOOD_KEYS and m == meal)
            if milk_count > self.milk_guidance.servings_per_meal:
                warnings.append(
                    f"Large-portion guidance: milk drinks total {milk_count} cups in {meal} "
                    f"(soft guide {self.milk_guidance.servings_per_meal}; hard limit {MILK_PORTION_LIMITS.servings_per_meal})."
                )
        if len(daily_counts) < 2 and portions:
            warnings.append("Food-variety guidance: fewer than two distinct mess foods were selected.")
        if missing:
            warnings.append(f"{len(set(missing))} menu item(s) have unavailable nutrition and were excluded.")
        low_confidence = [x for x in excluded if "below safety threshold" in x[2]]
        if low_confidence:
            warnings.append(f"{len(low_confidence)} menu item(s) were excluded below the {self.minimum_confidence} confidence threshold.")
        if status is PlanStatus.PROTEIN_SHORTFALL:
            warnings.append(f"Protein minimum missed by {self.profile.targets.minimum_protein_g - totals.protein_g} g; this result is diagnostic only.")
        elif status is PlanStatus.INFEASIBLE:
            warnings.append("No valid plan satisfies all hard daily calorie and protein constraints.")
        if not optimal:
            warnings.append("CP-SAT returned a hard-constraint-valid incumbent before proving all soft objectives optimal.")
        return tuple(deviations), tuple(warnings)

    def _recalculate_meal_calories(self, portions: Sequence[SelectedPortion], soya_grams: Decimal,
                                   records: Mapping[str, FoodRecord]) -> tuple[tuple[str, Decimal], ...]:
        totals = {meal: Decimal("0") for meal in self.meal_guidance}
        totals[self.plant_protein_meal] += self.plant_protein.calories_kcal
        for portion in portions:
            record = records[portion.food_key]
            assert record.nutrition_per_100g is not None
            totals[portion.meal] += record.nutrition_per_100g.calories_kcal * portion.total_grams / Decimal("100")
        if soya_grams:
            assert self.soya is not None
            totals[self.soya_meal] += self.soya.calories_kcal * soya_grams / Decimal("100")
        return tuple((meal, totals[meal]) for meal in self.meal_guidance)

    def _recalculate_meal_protein(self, portions: Sequence[SelectedPortion], soya_grams: Decimal,
                                  records: Mapping[str, FoodRecord]) -> tuple[tuple[str, Decimal], ...]:
        totals = {meal: Decimal("0") for meal in self.meal_guidance}
        totals[self.plant_protein_meal] += self.plant_protein.protein_g
        for portion in portions:
            record = records[portion.food_key]
            assert record.nutrition_per_100g is not None
            totals[portion.meal] += record.nutrition_per_100g.protein_g * portion.total_grams / Decimal("100")
        if soya_grams:
            assert self.soya is not None
            totals[self.soya_meal] += self.soya.protein_g * soya_grams / Decimal("100")
        return tuple((meal, totals[meal]) for meal in self.meal_guidance)

    def _recalculate(self, portions: Sequence[SelectedPortion], soya_grams: Decimal,
                     records: Mapping[str, FoodRecord] | None = None) -> MacroTotals:
        calories = self.plant_protein.calories_kcal
        protein = self.plant_protein.protein_g
        carbs: Decimal | None = self.plant_protein.carbohydrates_g
        fat: Decimal | None = self.plant_protein.fat_g
        for portion in portions:
            record = records.get(portion.food_key) if records is not None else None
            if record is None:
                record = _catalog_record_for_key(portion.food_key)
            assert record is not None and record.nutrition_per_100g is not None
            ratio = portion.total_grams / Decimal("100")
            macros = record.nutrition_per_100g
            calories += macros.calories_kcal * ratio
            protein += macros.protein_g * ratio
            if carbs is not None:
                carbs += macros.carbohydrates_g * ratio
            if fat is not None:
                fat += macros.fat_g * ratio
        if soya_grams:
            assert self.soya is not None
            ratio = soya_grams / Decimal("100")
            calories += self.soya.calories_kcal * ratio
            protein += self.soya.protein_g * ratio
            carbs = carbs + self.soya.carbohydrates_g * ratio if carbs is not None and self.soya.carbohydrates_g is not None else None
            fat = fat + self.soya.fat_g * ratio if fat is not None and self.soya.fat_g is not None else None
        return MacroTotals(calories, protein, carbs, fat)

    def _validate(self, portions: Sequence[SelectedPortion], totals: MacroTotals, soya_grams: Decimal,
                  *, records: Mapping[str, FoodRecord], require_protein: bool,
                  enforce_minimum_calories: bool) -> None:
        # Independent recomputation checks the result without any serving caps.
        checked = self._recalculate(portions, soya_grams, records)
        if checked != totals:
            raise OptimizationValidationError(f"macro recalculation mismatch: {checked!r} != {totals!r}")
        targets = self.profile.targets
        if totals.calories_kcal > targets.maximum_calories_kcal:
            raise OptimizationValidationError("independent calories exceed hard ceiling")
        if enforce_minimum_calories and totals.calories_kcal < targets.minimum_calories_kcal:
            raise OptimizationValidationError("independent calories fall below required minimum")
        if require_protein and totals.protein_g < targets.minimum_protein_g:
            raise OptimizationValidationError("independent protein is below required minimum")
        if soya_grams < 0 or soya_grams > Decimal(self.maximum_soya_grams):
            raise OptimizationValidationError("soya exceeds the configured dry-weight maximum")
        if self.soya is None and soya_grams != 0:
            raise OptimizationValidationError("soya was used without a product label profile")
        recalculated_meals = self._recalculate_meal_calories(portions, soya_grams, records)
        if sum((value for _, value in recalculated_meals), Decimal("0")) != totals.calories_kcal:
            raise OptimizationValidationError("independent meal calorie totals do not sum to daily calories")
        if any(p.servings <= 0 or p.total_grams != p.serving_grams * p.servings for p in portions):
            raise OptimizationValidationError("selected serving weight does not match its integer serving count")

    def _complete_message(self, totals: MacroTotals, soya_grams: Decimal) -> str:
        gaps = []
        if totals.fat_g is None:
            gaps.append("fat (plant isolate label fat missing)")
        if totals.carbohydrates_g is None:
            gaps.append("carbohydrates (plant isolate label carbs missing)")
        text = f"Target constraints met at {totals.calories_kcal} kcal and {totals.protein_g} g protein."
        if soya_grams:
            text += f" Added {soya_grams} g dry soya chunks as the minimum feasible supplement quantity."
        else:
            text += " No soya chunks needed."
        if gaps:
            text += " Full macro total incomplete: " + " and ".join(gaps) + " are unknown."
        return text


def _catalog_record_for_key(key: str) -> FoodRecord | None:
    # Lazy import avoids catalog/optimizer module cycles in standalone use.
    from .catalog import CATALOG
    return CATALOG.get(key)


def validate_optimization_result(result: OptimizationResult) -> tuple[str, ...]:
    """Independently verify a complete, target-compliant plan for delivery.

    The menu inventory and limit snapshot are retained in the result so the
    delivery boundary can recheck quantities and macros without trusting the
    solver's reported totals or inventing catalog items.
    """
    errors: list[str] = []
    context = result.validation_context
    if context is None:
        return ("Optimization result has no validation context.",)
    if result.status is not PlanStatus.FEASIBLE:
        errors.append(f"Optimization status is {result.status.value}, not feasible.")
    coverage_by_meal = {coverage.meal: coverage for coverage in result.meal_coverage}
    selected_meals = {portion.meal for portion in result.portions}
    for meal in context.required_meals:
        coverage = coverage_by_meal.get(meal)
        if coverage is None or not coverage.available:
            reason = coverage.reason if coverage is not None else "coverage result missing"
            errors.append(f"{meal} is unavailable: {reason}")
        if meal not in selected_meals:
            errors.append(f"{meal} has no selected mess food.")

    inventory: dict[tuple[str, str], list[FoodRecord]] = {}
    for item in context.menu_inventory:
        inventory.setdefault((item.meal, item.food.key), []).append(item.food)
    limits = dict(context.portion_limits)
    daily_counts: dict[str, int] = {}
    meal_counts: dict[tuple[str, str], int] = {}
    selected_records: dict[str, FoodRecord] = {}

    for portion in result.portions:
        records = inventory.get((portion.meal, portion.food_key), [])
        record = next((candidate for candidate in records
                       if candidate.nutrition_per_100g is not None
                       and candidate.serving is not None
                       and candidate.confidence >= context.minimum_confidence), None)
        if record is None:
            errors.append(f"{portion.meal}/{portion.food_key} is not an eligible item in the supplied menu.")
            continue
        if (portion.food_name != record.display_name or portion.confidence != record.confidence
                or portion.serving_grams != record.serving.grams
                or portion.servings <= 0
                or portion.total_grams != portion.serving_grams * portion.servings):
            errors.append(f"{portion.meal}/{portion.food_key} has inconsistent serving identity or weight.")
        daily_counts[portion.food_key] = daily_counts.get(portion.food_key, 0) + portion.servings
        meal_key = (portion.meal, portion.food_key)
        meal_counts[meal_key] = meal_counts.get(meal_key, 0) + portion.servings
        selected_records[portion.food_key] = record

    for food_key, count in daily_counts.items():
        limit = limits.get(food_key)
        if limit is None:
            errors.append(f"{food_key} has no configured hard portion limit.")
        elif count > limit.servings_per_day:
            errors.append(f"{food_key} exceeds its hard daily portion limit.")
    for (meal, food_key), count in meal_counts.items():
        limit = limits.get(food_key)
        if limit is not None and count > limit.servings_per_meal:
            errors.append(f"{meal}/{food_key} exceeds its hard per-meal portion limit.")

    if result.fixed_plant_protein_scoops != 1 or context.plant_protein != FIXED_PLANT_PROTEIN:
        errors.append("Fixed plant protein isolate must be exactly one configured 35 g scoop.")
    if result.soya_dry_grams < 0 or result.soya_dry_grams > Decimal(context.maximum_soya_grams):
        errors.append("Soya dry quantity exceeds its configured maximum.")
    if result.soya_dry_grams % Decimal(context.soya_step_grams) != 0:
        errors.append("Soya dry quantity does not match the configured portion step.")
    if result.soya_dry_grams and context.soya is None:
        errors.append("Soya was selected without a configured label profile.")

    calories = context.plant_protein.calories_kcal
    protein = context.plant_protein.protein_g
    carbohydrates = context.plant_protein.carbohydrates_g
    fat = context.plant_protein.fat_g
    meal_calories = {meal: Decimal("0") for meal in context.meal_names}
    meal_protein = {meal: Decimal("0") for meal in context.meal_names}
    meal_carbohydrates = {meal: Decimal("0") for meal in context.meal_names}
    meal_fat = {meal: Decimal("0") for meal in context.meal_names}
    meal_calories[context.plant_protein_meal] += context.plant_protein.calories_kcal
    meal_protein[context.plant_protein_meal] += context.plant_protein.protein_g
    if context.plant_protein.carbohydrates_g is not None:
        meal_carbohydrates[context.plant_protein_meal] += context.plant_protein.carbohydrates_g
    if context.plant_protein.fat_g is not None:
        meal_fat[context.plant_protein_meal] += context.plant_protein.fat_g

    for portion in result.portions:
        record = selected_records.get(portion.food_key)
        if record is None or record.nutrition_per_100g is None:
            continue
        macros = record.nutrition_per_100g
        ratio = portion.total_grams / Decimal("100")
        calories += macros.calories_kcal * ratio
        protein += macros.protein_g * ratio
        carbohydrates += macros.carbohydrates_g * ratio
        fat += macros.fat_g * ratio
        meal_calories[portion.meal] += macros.calories_kcal * ratio
        meal_protein[portion.meal] += macros.protein_g * ratio
        meal_carbohydrates[portion.meal] += macros.carbohydrates_g * ratio
        meal_fat[portion.meal] += macros.fat_g * ratio

    if result.soya_dry_grams and context.soya is not None:
        ratio = result.soya_dry_grams / Decimal("100")
        calories += context.soya.calories_kcal * ratio
        protein += context.soya.protein_g * ratio
        if context.soya.carbohydrates_g is not None:
            carbohydrates += context.soya.carbohydrates_g * ratio
        else:
            carbohydrates = None
        if context.soya.fat_g is not None:
            fat += context.soya.fat_g * ratio
        else:
            fat = None
        meal_calories[context.soya_meal] += context.soya.calories_kcal * ratio
        meal_protein[context.soya_meal] += context.soya.protein_g * ratio
        if context.soya.carbohydrates_g is not None:
            meal_carbohydrates[context.soya_meal] += context.soya.carbohydrates_g * ratio
        if context.soya.fat_g is not None:
            meal_fat[context.soya_meal] += context.soya.fat_g * ratio

    recomputed = MacroTotals(calories, protein, carbohydrates, fat)
    if recomputed != result.totals:
        errors.append("Reported daily macros do not match independent nutrition recalculation.")
    if calories < context.targets.minimum_calories_kcal or calories > context.targets.maximum_calories_kcal:
        errors.append("Independent calories are outside the hard daily range.")
    if protein < context.targets.minimum_protein_g:
        errors.append("Independent protein is below the hard daily minimum.")
    if dict(result.meal_calories_kcal) != meal_calories:
        errors.append("Reported meal calories do not match independent recalculation.")
    if dict(result.meal_protein_g) != meal_protein:
        errors.append("Reported meal protein does not match independent recalculation.")
    return tuple(dict.fromkeys(errors))


def summarize_validation_failure(
    result: OptimizationResult, errors: Sequence[str] | None = None,
) -> str:
    """Return a compact failure summary without menu text or profile data.

    Validation messages can contain scraped food names. Reports therefore use
    stable constraint labels and aggregates only.
    """
    errors = tuple(errors if errors is not None else validate_optimization_result(result))
    labels: set[str] = set()
    for error in errors:
        lowered = error.casefold()
        if "status is" in lowered:
            labels.add("plan_status")
        if "unavailable:" in lowered or "has no selected mess food" in lowered:
            labels.add("required_meal_coverage")
        if "eligible item in the supplied menu" in lowered:
            labels.add("menu_inventory")
        if "hard daily portion limit" in lowered:
            labels.add("daily_portion_limit")
        if "hard per-meal portion limit" in lowered:
            labels.add("meal_portion_limit")
        if "hard portion limit" in lowered:
            labels.add("portion_limit_configuration")
        if "serving identity or weight" in lowered:
            labels.add("serving_identity")
        if "plant protein isolate" in lowered:
            labels.add("fixed_supplement")
        if "soya" in lowered:
            labels.add("soya_limit")
        if "macros do not match" in lowered or "meal calories do not match" in lowered or "meal protein does not match" in lowered:
            labels.add("macro_consistency")
        if "calories are outside" in lowered:
            labels.add("calorie_range")
        if "protein is below" in lowered:
            labels.add("protein_minimum")
        if not labels:
            labels.add("validation_invariant")

    context = result.validation_context
    parts = ["failed_constraints=" + ",".join(sorted(labels))]
    if context is None:
        parts.append("validation_context=missing")
        return "; ".join(parts)

    coverage = {entry.meal: entry.available for entry in result.meal_coverage}
    parts.append("meal_coverage=" + ",".join(
        f"{meal}:{'available' if coverage.get(meal, False) else 'unavailable'}"
        for meal in context.required_meals
    ))
    limits = dict(context.portion_limits)
    eligible_counts = {meal: 0 for meal in context.required_meals}
    for item in context.menu_inventory:
        food = item.food
        if (item.meal in eligible_counts and food.nutrition_per_100g is not None
                and food.confidence >= context.minimum_confidence
                and food.serving is not None and food.key in limits):
            eligible_counts[item.meal] += 1
    parts.append("eligible_menu_items=" + ",".join(
        f"{meal}:{eligible_counts[meal]}" for meal in context.required_meals
    ))
    targets = context.targets
    parts.append(
        f"calories_kcal={result.totals.calories_kcal} "
        f"(allowed {targets.minimum_calories_kcal}-{targets.maximum_calories_kcal}); "
        f"protein_g={result.totals.protein_g} (minimum {targets.minimum_protein_g})"
    )

    exclusion_counts: dict[str, int] = {}
    for _, _, reason in result.excluded_items:
        lowered = reason.casefold()
        if "no exact curated catalog match" in lowered:
            key = "unmapped_catalog"
        elif "below safety threshold" in lowered:
            key = "below_confidence"
        elif "no serving weight" in lowered:
            key = "missing_serving_weight"
        elif "no food-specific safe serving limit" in lowered:
            key = "missing_portion_limit"
        elif "nutrition unavailable" in lowered or "no nutrition assigned" in lowered or "no macro value" in lowered:
            key = "nutrition_unavailable"
        else:
            key = "other_exclusion"
        exclusion_counts[key] = exclusion_counts.get(key, 0) + 1
    if exclusion_counts:
        parts.append("excluded_items=" + ",".join(
            f"{key}:{exclusion_counts[key]}" for key in sorted(exclusion_counts)
        ))
    return "; ".join(parts)
