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
class MealRule:
    minimum_distinct_items: int
    maximum_distinct_items: int
    maximum_servings_per_item: int = 2
    maximum_total_servings: int = 4
    minimum_calories_kcal: Decimal = Decimal("0")
    maximum_calories_kcal: Decimal = Decimal("2500")

    def __post_init__(self) -> None:
        if self.minimum_distinct_items < 0 or self.maximum_distinct_items < self.minimum_distinct_items:
            raise ValueError("invalid meal distinct-item bounds")
        if self.maximum_servings_per_item < 1:
            raise ValueError("maximum servings per item must be positive")
        if self.maximum_total_servings < self.minimum_distinct_items:
            raise ValueError("maximum total servings cannot be below minimum distinct items")
        if self.minimum_calories_kcal < 0 or self.maximum_calories_kcal < self.minimum_calories_kcal:
            raise ValueError("invalid meal calorie bounds")


@dataclass(frozen=True)
class FoodPortionLimit:
    """Maximum typical portions for one food over a day and within one meal."""

    maximum_servings_per_day: int
    maximum_servings_per_meal: int
    exclusive_group: str | None = None
    exclusive_group_maximum_servings_per_day: int | None = None

    def __post_init__(self) -> None:
        if self.maximum_servings_per_day < 1 or self.maximum_servings_per_meal < 1:
            raise ValueError("food serving limits must be positive")
        if self.exclusive_group is None and self.exclusive_group_maximum_servings_per_day is not None:
            raise ValueError("group limit requires an exclusive group")
        if self.exclusive_group_maximum_servings_per_day is not None and self.exclusive_group_maximum_servings_per_day < 1:
            raise ValueError("exclusive group maximum must be positive")


DEFAULT_MEAL_RULES: dict[str, MealRule] = {
    "Breakfast": MealRule(1, 4, 2, 3, Decimal("350"), Decimal("650")),
    "Lunch": MealRule(1, 5, 2, 4, Decimal("400"), Decimal("700")),
    # Optional: every snack item on the actual snapshot falls below the safety
    # threshold or has unknown macros, so the default policy allows no snack.
    "Snacks": MealRule(0, 3, 2, 2, Decimal("0"), Decimal("300")),
    "Dinner": MealRule(1, 5, 2, 4, Decimal("400"), Decimal("700")),
}


# These are optimizer safety caps, independent of the nutrition catalog. Each
# unit is the catalog's typical serving weight; for example, 1 rice serving is
# 180 g cooked and 1 milk serving is 150 g. The milk group treats hot/cold as
# alternatives and limits the day to one of them.
DEFAULT_FOOD_PORTION_LIMITS: dict[str, FoodPortionLimit] = {
    "banana": FoodPortionLimit(1, 1),
    "masala_oats": FoodPortionLimit(1, 1),
    "muesli": FoodPortionLimit(1, 1),
    "sada_paratha": FoodPortionLimit(2, 2),
    "chana_palak": FoodPortionLimit(1, 1),
    "hot_milk": FoodPortionLimit(1, 1, "milk", 1),
    "cold_milk": FoodPortionLimit(1, 1, "milk", 1),
    "tea_d": FoodPortionLimit(1, 1),
    "coffee_powder": FoodPortionLimit(1, 1),
    "bread_butter_jam": FoodPortionLimit(1, 1),
    "arhar_dal": FoodPortionLimit(1, 1),
    "paneer_do_pyaza": FoodPortionLimit(1, 1),
    "ullipai_pacchadi": FoodPortionLimit(1, 1),
    "curd": FoodPortionLimit(1, 1),
    "green_salad": FoodPortionLimit(1, 1),
    "jeera_rice": FoodPortionLimit(2, 1),
    "chapati": FoodPortionLimit(4, 2),
    "veg_puff": FoodPortionLimit(1, 1),
    "hot_coffee": FoodPortionLimit(1, 1),
    "ketchup": FoodPortionLimit(1, 1),
    "chole_masala": FoodPortionLimit(1, 1),
    "jeera_aloo": FoodPortionLimit(1, 1),
    "rasam": FoodPortionLimit(1, 1),
}


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
    objective_values: tuple[tuple[str, Decimal], ...] = ()

    @property
    def is_valid_target_plan(self) -> bool:
        return self.status is PlanStatus.FEASIBLE


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
    product-label profile and is minimized before all other soft objectives.
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
        meal_rules: Mapping[str, MealRule] | None = None,
        food_portion_limits: Mapping[str, FoodPortionLimit] | None = None,
        plant_protein_meal: str = "Breakfast",
        soya_meal: str | None = None,
        max_time_seconds: float = 10.0,
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
        self.meal_rules = dict(meal_rules or DEFAULT_MEAL_RULES)
        self.food_portion_limits = {**DEFAULT_FOOD_PORTION_LIMITS, **(food_portion_limits or {})}
        if plant_protein_meal not in self.meal_rules:
            raise ValueError("plant protein meal must exist in meal rules")
        if soya_meal is None:
            soya_meal = next((meal for meal in ("Snacks", "Lunch", plant_protein_meal) if meal in self.meal_rules), plant_protein_meal)
        if self.soya is not None and soya_meal not in self.meal_rules:
            raise ValueError("soya meal must exist in meal rules")
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
        eligible: list[MenuItem] = []
        excluded: list[tuple[str, str, str]] = []
        missing: list[str] = []
        for item in menu:
            food = item.food
            if food.nutrition_per_100g is None:
                reason = food.notes or "Nutrition unavailable; cannot optimize this item."
                excluded.append((item.meal, food.display_name, reason))
                missing.append(f"{item.meal}: {food.display_name}")
            elif food.confidence < self.minimum_confidence:
                excluded.append((item.meal, food.display_name,
                                 f"confidence {food.confidence} is below safety threshold {self.minimum_confidence}"))
            elif food.serving is None:
                excluded.append((item.meal, food.display_name, "No serving weight is defined."))
            else:
                eligible.append(item)

        hard = self._run(eligible, require_protein=True, enforce_minimum_calories=True)
        if hard is not None:
            portions, soya_g, totals, objective_values, records, meal_calories = hard
            self._validate(portions, totals, soya_g, records=records, meal_calories=meal_calories,
                           require_protein=True, enforce_minimum_calories=True)
            return OptimizationResult(
                PlanStatus.FEASIBLE, portions, totals, 1, soya_g, Decimal("0"),
                tuple(excluded), tuple(sorted(set(missing))),
                self._complete_message(totals, soya_g), meal_calories, objective_values,
            )

        # Diagnostic fallback preserves the hard calorie ceiling and maximizes
        # achievable protein; it never labels a sub-target result as a valid plan.
        fallback = self._run(eligible, require_protein=False, enforce_minimum_calories=True)
        if fallback is None:
            fallback = self._run(eligible, require_protein=False, enforce_minimum_calories=False)
        if fallback is None:
            return OptimizationResult(
                PlanStatus.INFEASIBLE, (), MacroTotals(self.plant_protein.calories_kcal, self.plant_protein.protein_g,
                    self.plant_protein.carbohydrates_g, self.plant_protein.fat_g),
                1, Decimal("0"), self.profile.targets.minimum_protein_g,
                tuple(excluded), tuple(sorted(set(missing))),
                "No menu combination satisfies the calorie ceiling and meal rules, even before requiring the protein minimum.",
            )
        portions, soya_g, totals, objective_values, records, meal_calories = fallback
        shortfall = max(Decimal("0"), self.profile.targets.minimum_protein_g - totals.protein_g)
        self._validate(portions, totals, soya_g, records=records, meal_calories=meal_calories, require_protein=False,
                       enforce_minimum_calories=totals.calories_kcal >= self.profile.targets.minimum_calories_kcal)
        if totals.calories_kcal < self.profile.targets.minimum_calories_kcal:
            fallback_status = PlanStatus.INFEASIBLE
            fallback_message = (f"No plan met the {self.profile.targets.minimum_calories_kcal} kcal minimum. "
                                f"Best diagnostic plan provides {totals.calories_kcal} kcal and {totals.protein_g} g protein.")
        elif shortfall > 0:
            fallback_status = PlanStatus.PROTEIN_SHORTFALL
            fallback_message = (f"No plan met {self.profile.targets.minimum_protein_g} g protein within the calorie limits. "
                                f"Best diagnostic plan provides {totals.protein_g} g protein "
                                f"({shortfall} g shortfall) at {totals.calories_kcal} kcal.")
        else:
            fallback_status = PlanStatus.FEASIBLE
            fallback_message = self._complete_message(totals, soya_g)
        return OptimizationResult(
            fallback_status, portions, totals, 1, soya_g, shortfall,
            tuple(excluded), tuple(sorted(set(missing))),
            fallback_message, meal_calories, objective_values,
        )

    def _run(self, eligible: Sequence[MenuItem], *, require_protein: bool,
             enforce_minimum_calories: bool) -> tuple[tuple[SelectedPortion, ...], Decimal, MacroTotals, tuple[tuple[str, Decimal], ...], Mapping[str, FoodRecord], tuple[tuple[str, Decimal], ...]] | None:
        targets = self.profile.targets
        model = cp_model.CpModel()
        serving_vars: dict[str, cp_model.IntVar] = {}
        selected_vars: dict[str, cp_model.IntVar] = {}
        item_map: dict[str, MenuItem] = {}
        meal_vars: dict[str, list[cp_model.IntVar]] = {meal: [] for meal in self.meal_rules}
        meal_portions: dict[str, list[cp_model.IntVar]] = {meal: [] for meal in self.meal_rules}
        daily_food_portions: dict[str, list[cp_model.IntVar]] = {}
        meal_food_portions: dict[tuple[str, str], list[cp_model.IntVar]] = {}
        group_portions: dict[str, list[cp_model.IntVar]] = {}
        group_caps: dict[str, int] = {}
        meal_food_calories: dict[str, list[object]] = {meal: [] for meal in self.meal_rules}
        for index, item in enumerate(eligible):
            if item.meal not in self.meal_rules:
                continue
            key = f"{item.key}:{index}"
            rule = self.meal_rules[item.meal]
            limit = self.food_portion_limits.get(item.food.key, FoodPortionLimit(2, 1))
            item_max = min(rule.maximum_servings_per_item, limit.maximum_servings_per_meal,
                           limit.maximum_servings_per_day)
            portion = model.new_int_var(0, item_max, f"servings_{index}")
            active = model.new_bool_var(f"selected_{index}")
            model.add(portion >= active)
            model.add(portion <= item_max * active)
            serving_vars[key], selected_vars[key], item_map[key] = portion, active, item
            meal_vars[item.meal].append(active)
            meal_portions[item.meal].append(portion)
            daily_food_portions.setdefault(item.food.key, []).append(portion)
            meal_food_portions.setdefault((item.food.key, item.meal), []).append(portion)
            if limit.exclusive_group is not None:
                group_portions.setdefault(limit.exclusive_group, []).append(portion)
                cap = limit.exclusive_group_maximum_servings_per_day
                if cap is not None:
                    existing = group_caps.setdefault(limit.exclusive_group, cap)
                    if existing != cap:
                        raise ValueError(f"inconsistent maximum for food group {limit.exclusive_group!r}")
            assert item.food.nutrition_per_100g is not None and item.food.serving is not None
            kcal_per_serving = item.food.nutrition_per_100g.calories_kcal * item.food.serving.grams / Decimal("100")
            meal_food_calories[item.meal].append(self._units(kcal_per_serving) * portion)

        for food_key, portions_for_food in daily_food_portions.items():
            limit = self.food_portion_limits.get(food_key, FoodPortionLimit(2, 1))
            model.add(sum(portions_for_food) <= limit.maximum_servings_per_day)
        for (food_key, _meal), portions_for_food_meal in meal_food_portions.items():
            limit = self.food_portion_limits.get(food_key, FoodPortionLimit(2, 1))
            model.add(sum(portions_for_food_meal) <= limit.maximum_servings_per_meal)
        for group, portions_in_group in group_portions.items():
            if group in group_caps:
                model.add(sum(portions_in_group) <= group_caps[group])

        soya_units: cp_model.IntVar | None = None
        soya_cap = self.maximum_soya_grams // self.soya_step_grams
        if self.soya is not None and soya_cap > 0:
            soya_units = model.new_int_var(0, soya_cap, "soya_dry_portion_steps")

        meal_kcal_expressions: dict[str, object] = {}
        for meal in self.meal_rules:
            supplement_calories = 0
            if meal == self.plant_protein_meal:
                supplement_calories += self._units(self.plant_protein.calories_kcal)
            if meal == self.soya_meal and soya_units is not None and self.soya is not None:
                per_step = self.soya.calories_kcal * Decimal(self.soya_step_grams) / Decimal("100")
                meal_food_calories[meal].append(self._units(per_step) * soya_units)
            expression = sum(meal_food_calories[meal]) + supplement_calories
            meal_kcal_expressions[meal] = expression

        for meal, rule in self.meal_rules.items():
            vars_for_meal = meal_vars[meal]
            model.add(sum(vars_for_meal) >= rule.minimum_distinct_items)
            model.add(sum(vars_for_meal) <= rule.maximum_distinct_items)
            model.add(sum(meal_portions[meal]) <= rule.maximum_total_servings)
            model.add(meal_kcal_expressions[meal] >= self._units(rule.minimum_calories_kcal))
            model.add(meal_kcal_expressions[meal] <= self._units(rule.maximum_calories_kcal))

        def macro_expr(field_name: str):
            terms = []
            for key, var in serving_vars.items():
                food = item_map[key].food
                assert food.nutrition_per_100g is not None and food.serving is not None
                per_portion = getattr(food.nutrition_per_100g, field_name) * food.serving.grams / Decimal("100")
                terms.append(self._units(per_portion) * var)
            plant_value = getattr(self.plant_protein, field_name)
            fixed = self._units(plant_value) if plant_value is not None else 0
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
        model.add(kcal <= self._units(targets.maximum_calories_kcal))
        if enforce_minimum_calories:
            model.add(kcal >= self._units(targets.minimum_calories_kcal))
        if require_protein:
            model.add(protein >= self._units(targets.minimum_protein_g))

        stages: list[tuple[str, object, str]] = []
        if require_protein and soya_units is not None:
            stages.append(("soya_minimization", soya_units, "min"))
        if require_protein:
            kcal_deviation = model.new_int_var(0, self._units(targets.maximum_calories_kcal), "preferred_calorie_deviation")
            model.add_abs_equality(kcal_deviation, kcal - self._units(targets.preferred_calories_kcal))
            stages.append(("calorie_target_deviation", kcal_deviation, "min"))
            if fat is not None:
                fat_deviation = model.new_int_var(0, self._units(targets.maximum_calories_kcal), "preferred_fat_deviation")
                model.add_abs_equality(fat_deviation, fat - self._units(targets.preferred_fat_g))
                stages.append(("fat_target_deviation", fat_deviation, "min"))
            stretch = model.new_int_var(0, self._units(targets.stretch_protein_g), "protein_up_to_stretch")
            model.add_min_equality(stretch, [protein, self._units(targets.stretch_protein_g)])
            stages.append(("optional_protein_stretch", stretch, "max"))
            # After calorie/fat/protein priorities, carbohydrates fill the
            # remaining budget where complete supplement labels permit it.
            if carbs is not None:
                stages.append(("remaining_carbohydrates", carbs, "max"))
        else:
            stages.append(("achievable_protein", protein, "max"))
            if soya_units is not None:
                stages.append(("minimum_soya_at_best_protein", soya_units, "min"))
            kcal_deviation = model.new_int_var(0, self._units(targets.maximum_calories_kcal), "preferred_calorie_deviation")
            model.add_abs_equality(kcal_deviation, kcal - self._units(targets.preferred_calories_kcal))
            stages.append(("calorie_target_deviation", kcal_deviation, "min"))
            if fat is not None:
                fat_deviation = model.new_int_var(0, self._units(targets.maximum_calories_kcal), "preferred_fat_deviation")
                model.add_abs_equality(fat_deviation, fat - self._units(targets.preferred_fat_g))
                stages.append(("fat_target_deviation", fat_deviation, "min"))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_time_seconds
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        stage_values: list[tuple[str, Decimal]] = []
        for name, expression, direction in stages:
            if direction == "min":
                model.minimize(expression)
            else:
                model.maximize(expression)
            status = solver.solve(model)
            if status != cp_model.OPTIMAL:
                return None
            value = solver.value(expression)
            stage_values.append((name, self._decimal(value)))
            if direction == "min":
                model.add(expression == value)
            else:
                model.add(expression == value)

        # Solve one final time with all lexicographic objective optima fixed.
        status = solver.solve(model)
        if status != cp_model.OPTIMAL:
            return None
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
        return tuple(portions), soya_grams, totals, tuple(stage_values), records, meal_calories

    def _recalculate_meal_calories(self, portions: Sequence[SelectedPortion], soya_grams: Decimal,
                                   records: Mapping[str, FoodRecord]) -> tuple[tuple[str, Decimal], ...]:
        totals = {meal: Decimal("0") for meal in self.meal_rules}
        totals[self.plant_protein_meal] += self.plant_protein.calories_kcal
        for portion in portions:
            record = records[portion.food_key]
            assert record.nutrition_per_100g is not None
            totals[portion.meal] += record.nutrition_per_100g.calories_kcal * portion.total_grams / Decimal("100")
        if soya_grams:
            assert self.soya is not None
            totals[self.soya_meal] += self.soya.calories_kcal * soya_grams / Decimal("100")
        return tuple((meal, totals[meal]) for meal in self.meal_rules)

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
                  *, records: Mapping[str, FoodRecord], meal_calories: Sequence[tuple[str, Decimal]],
                  require_protein: bool, enforce_minimum_calories: bool) -> None:
        # Independent recomputation uses catalog source data, not CP-SAT values.
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
        if self.soya is None and soya_grams != 0:
            raise OptimizationValidationError("soya was used without a product label profile")
        recalculated_meals = self._recalculate_meal_calories(portions, soya_grams, records)
        if tuple(meal_calories) != recalculated_meals:
            raise OptimizationValidationError("independent meal calorie recalculation mismatch")
        for meal, kcal in recalculated_meals:
            rule = self.meal_rules[meal]
            if not rule.minimum_calories_kcal <= kcal <= rule.maximum_calories_kcal:
                raise OptimizationValidationError(f"meal {meal} is outside its calorie bounds")
        daily_counts: dict[str, int] = {}
        meal_counts: dict[tuple[str, str], int] = {}
        group_counts: dict[str, int] = {}
        for portion in portions:
            daily_counts[portion.food_key] = daily_counts.get(portion.food_key, 0) + portion.servings
            meal_counts[(portion.food_key, portion.meal)] = meal_counts.get((portion.food_key, portion.meal), 0) + portion.servings
            limit = self.food_portion_limits.get(portion.food_key, FoodPortionLimit(2, 1))
            if limit.exclusive_group is not None:
                group_counts[limit.exclusive_group] = group_counts.get(limit.exclusive_group, 0) + portion.servings
        for food_key, count in daily_counts.items():
            limit = self.food_portion_limits.get(food_key, FoodPortionLimit(2, 1))
            if count > limit.maximum_servings_per_day:
                raise OptimizationValidationError(f"{food_key} exceeds its daily portion limit")
        for (food_key, _meal), count in meal_counts.items():
            limit = self.food_portion_limits.get(food_key, FoodPortionLimit(2, 1))
            if count > limit.maximum_servings_per_meal:
                raise OptimizationValidationError(f"{food_key} exceeds its per-meal portion limit")
        for portion in portions:
            limit = self.food_portion_limits.get(portion.food_key, FoodPortionLimit(2, 1))
            if limit.exclusive_group and limit.exclusive_group_maximum_servings_per_day is not None:
                if group_counts.get(limit.exclusive_group, 0) > limit.exclusive_group_maximum_servings_per_day:
                    raise OptimizationValidationError(f"food group {limit.exclusive_group} exceeds its daily limit")

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
