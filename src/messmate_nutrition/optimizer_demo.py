"""Run the Phase 3 optimizer against a saved Phase 1 menu snapshot."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path

from .optimizer import DietOptimizer, UserNutritionProfile, menu_items_from_snapshot
from .profile_config import load_user_profile
from .serving_display import format_fixed_supplement, format_selected_portion


def render_optimization(snapshot_path: Path, *, profile: UserNutritionProfile | None = None) -> str:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    optimizer = DietOptimizer(profile=profile or load_user_profile())  # soya is added only if needed
    result = optimizer.optimize(menu_items_from_snapshot(snapshot))
    lines = [
        f"MESSMATE optimization for {snapshot.get('requested_date')}",
        "Targets: preferred 1900 kcal; 1800–2000 kcal hard range; protein ≥120 g; 155 g optional; fat target 55 g.",
        f"Status: {result.status.value}",
        format_fixed_supplement("Fixed plant protein isolate", "exactly 1 scoop", Decimal("35"), "product-label serving weight") + "; 132 kcal, P 29 g, C 2 g, F 0.48 g",
        f"Soya chunks: {result.soya_dry_grams} g dry (label per 100 g: 316 kcal, P 32.9 g, C 31.7 g, F 0.9 g; max 70 g/day)",
        "",
        "Meal plan:",
    ]
    if result.portions:
        for meal in ("Breakfast", "Lunch", "Snacks", "Dinner"):
            meal_items = [part for part in result.portions if part.meal == meal]
            if meal_items:
                lines.append(f"  {meal}:")
                for part in meal_items:
                    lines.append(f"    {part.food_name}: {format_selected_portion(part)}; confidence {part.confidence}")
            if result.soya_dry_grams and meal == optimizer.soya_meal:
                if not meal_items:
                    lines.append(f"  {meal}:")
                lines.append(f"    Soya chunks supplement: {result.soya_dry_grams} g dry weight")
            elif meal == "Snacks" and not meal_items:
                lines.append("  Snacks: no safe, scorable menu item; meal is skipped under the default rule.")
    else:
        lines.append("  No valid menu portions.")
    lines.extend((
        "",
        f"Meal calories (including isolate in {optimizer.plant_protein_meal} and soya in {optimizer.soya_meal}):",
    ))
    for meal, calories in result.meal_calories_kcal:
        rule = optimizer.meal_rules[meal]
        lines.append(f"  {meal}: {calories} kcal (allowed {rule.minimum_calories_kcal}–{rule.maximum_calories_kcal} kcal)")
    lines.extend((
        "",
        f"Recalculated totals: {result.totals.calories_kcal} kcal; protein {result.totals.protein_g} g; "
        f"carbohydrates {result.totals.carbohydrates_g if result.totals.carbohydrates_g is not None else 'UNKNOWN'} g; "
        f"fat {result.totals.fat_g if result.totals.fat_g is not None else 'UNKNOWN'} g.",
        f"Protein shortfall vs 120 g: {result.protein_shortfall_g} g.",
        result.message,
        "",
        f"Safety threshold: nutrition known and confidence ≥ {optimizer.minimum_confidence}.",
        "Excluded menu items:",
    ))
    if result.excluded_items:
        for meal, name, reason in result.excluded_items:
            lines.append(f"  - {meal}: {name} — {reason}")
    else:
        lines.append("  (none)")
    if result.missing_nutrition:
        lines.extend(("", "Missing nutrition requiring manual review:"))
        lines.extend(f"  - {name}" for name in result.missing_nutrition)
    if not result.is_valid_target_plan:
        lines.extend(("", "This is a diagnostic best-effort result, not a target-compliant diet plan."))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(render_optimization(args.snapshot))


if __name__ == "__main__":
    main()
