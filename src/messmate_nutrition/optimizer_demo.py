"""Render an optimization report from a saved menu snapshot."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from .optimizer import (
    FIXED_PLANT_PROTEIN, SOYA_CHUNKS_LABEL, DietOptimizer,
    UserNutritionProfile, menu_items_from_snapshot,
)
from .profile_config import load_user_profile
from .serving_display import format_selected_portion


def render_optimization(snapshot_path: Path, *, profile: UserNutritionProfile | None = None) -> str:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    profile = profile or load_user_profile()
    optimizer = DietOptimizer(profile=profile)
    menu = menu_items_from_snapshot(snapshot)
    records = {item.food.key: item.food for item in menu}
    result = optimizer.optimize(menu)
    meal_names = tuple(optimizer.meal_guidance)
    fields = ("calories_kcal", "protein_g", "carbohydrates_g", "fat_g")
    by_meal = {meal: {field: Decimal("0") for field in fields} for meal in meal_names}

    for portion in result.portions:
        nutrients = records[portion.food_key].nutrition_per_100g
        if nutrients is None:
            continue
        factor = portion.total_grams / Decimal("100")
        for field in fields:
            by_meal[portion.meal][field] += getattr(nutrients, field) * factor
    for field in fields:
        value = getattr(FIXED_PLANT_PROTEIN, field)
        if value is None:
            for meal in meal_names:
                by_meal[meal][field] = None
        elif by_meal[optimizer.plant_protein_meal][field] is not None:
            by_meal[optimizer.plant_protein_meal][field] += value
    if result.soya_dry_grams:
        factor = result.soya_dry_grams / Decimal("100")
        for field in fields:
            value = getattr(SOYA_CHUNKS_LABEL, field)
            if value is None:
                by_meal[optimizer.soya_meal][field] = None
            elif by_meal[optimizer.soya_meal][field] is not None:
                by_meal[optimizer.soya_meal][field] += value * factor

    def fmt(value: Decimal | None) -> str:
        return "UNKNOWN" if value is None else f"{value:.2f}"

    lines = [
        f"MESSMATE optimization for {snapshot.get('requested_date')}",
        "Targets: 1800–2000 kcal hard range; protein ≥120 g hard minimum; 1900 kcal and 55 g fat preferred; 155 g protein optional.",
        f"Status: {result.status.value}",
        f"Target-compliant: {'yes' if result.is_valid_target_plan else 'no'}",
        "", "Actual menu:",
    ]
    meals = snapshot.get("meals", {})
    for meal in meal_names:
        section = meals.get(meal, {}) if isinstance(meals, dict) else {}
        lines.append(f"  {meal}:")
        items = section.get("items", []) if isinstance(section, dict) else []
        lines.extend(f"    - {item}" for item in items)
        if not items:
            lines.append("    - No menu items extracted")
    lines.extend(("", "Selected foods by meal:"))
    coverage = {entry.meal: entry for entry in result.meal_coverage}
    for meal in meal_names:
        lines.append(f"  {meal}:")
        selected = [part for part in result.portions if part.meal == meal]
        lines.extend(f"    - {part.food_name}: {format_selected_portion(part)}; confidence {part.confidence}" for part in selected)
        if not selected:
            meal_coverage = coverage.get(meal)
            if meal in {"Breakfast", "Lunch", "Dinner"} and meal_coverage is not None:
                lines.append(f"    - Unavailable: {meal_coverage.reason}")
            else:
                lines.append("    - No eligible mess food selected")
        if meal == optimizer.plant_protein_meal:
            lines.append("    - Plant protein isolate: exactly 1 scoop (35 g; label P 29 g, C 2 g, F 0.48 g, 132 kcal)")
        if meal == optimizer.soya_meal and result.soya_dry_grams:
            lines.append(f"    - Soya chunks: {result.soya_dry_grams} g dry weight")
    lines.extend((
        "", "Meal nutrition (including supplements):",
        "  Meal | kcal | Protein g | Carbohydrates g | Fat g",
    ))
    meal_calories = dict(result.meal_calories_kcal)
    for meal in meal_names:
        m = by_meal[meal]
        guide = optimizer.meal_guidance[meal]
        lines.append(
            f"  {meal}: {fmt(m['calories_kcal'])} kcal, P {fmt(m['protein_g'])} g, "
            f"C {fmt(m['carbohydrates_g'])} g, F {fmt(m['fat_g'])} g; "
            f"soft meal window {guide.minimum_calories_kcal}–{guide.maximum_calories_kcal} kcal; "
            f"meal calories {meal_calories.get(meal, Decimal('0'))} kcal"
        )
    totals = result.totals
    lines.extend((
        "",
        f"Daily totals: {fmt(totals.calories_kcal)} kcal; protein {fmt(totals.protein_g)} g; "
        f"carbohydrates {fmt(totals.carbohydrates_g)} g; fat {fmt(totals.fat_g)} g.",
        f"Daily comparison: calories preferred 1900 (hard range 1800–2000); "
        f"protein minimum 120; optional stretch 155; fat preferred 55 g.",
        f"Protein shortfall: {result.protein_shortfall_g} g.",
        f"Fixed plant isolate: {result.fixed_plant_protein_scoops} scoop, 35 g.",
        f"Soya: {result.soya_dry_grams} g dry; maximum 70 g.",
        result.message,
        "", "Soft-preference deviations:",
    ))
    lines.extend(f"  - {item}" for item in result.soft_preference_deviations)
    if not result.soft_preference_deviations:
        lines.append("  - None")
    lines.extend(("", "Warnings and serving guidance (food-specific hard limits plus soft preferences):"))
    lines.extend(f"  - {item}" for item in result.warnings)
    if not result.warnings:
        lines.append("  - None")
    lines.extend(("", f"Nutrition eligibility threshold: confidence ≥ {optimizer.minimum_confidence}.", "Excluded menu items:"))
    if result.excluded_items:
        lines.extend(f"  - {meal}: {name} — {reason}" for meal, name, reason in result.excluded_items)
    else:
        lines.append("  - None")
    if result.missing_nutrition:
        lines.append("Foods with missing nutrition:")
        lines.extend(f"  - {name}" for name in result.missing_nutrition)
    lines.extend(("", "Soft objectives:", f"  {dict(result.objective_values)}"))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(render_optimization(args.snapshot))


if __name__ == "__main__":
    main()
