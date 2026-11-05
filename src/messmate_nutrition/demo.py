"""Render nutrition estimates for a Phase 1 JSON snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .calculator import NutritionUnavailableError, estimate_serving
from .catalog import lookup_food


def render_menu(snapshot_path: Path) -> str:
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    lines = [f"Nutrition estimate for {data['requested_date']} (source: {data['source_url']})"]
    unresolved: list[str] = []
    for meal, section in data["meals"].items():
        lines.extend(("", f"{meal}:"))
        for name in section["items"]:
            food = lookup_food(name)
            if food is None:
                unresolved.append(name)
                lines.append(f"  - {name}: UNMATCHED — no nutrition assigned; manual review required")
                continue
            weight = f"{food.serving.grams} g" if food.serving else "no serving estimate"
            if food.nutrition_per_100g is None:
                lines.append(f"  - {name}: UNAVAILABLE — {weight}; confidence {food.confidence}; {food.notes}")
                continue
            try:
                estimate = estimate_serving(food)
            except NutritionUnavailableError as exc:
                lines.append(f"  - {name}: UNAVAILABLE — {exc}")
                continue
            m = estimate.macros
            basis = f" Assumption: {food.recipe.description}" if food.recipe else ""
            lines.append(
                f"  - {name}: {weight}; {m.calories_kcal} kcal, P {m.protein_g} g, "
                f"C {m.carbohydrates_g} g, F {m.fat_g} g; confidence {food.confidence}.{basis}"
            )
            if food.source:
                lines.append(f"      Source basis: {food.source.name}. {food.source.note}")
            if food.serving:
                lines.append(f"      Serving range {food.serving.minimum_grams}–{food.serving.maximum_grams} g ({food.serving.basis}).")
    if unresolved:
        lines.extend(("", "Unmatched portal labels: " + ", ".join(unresolved)))
    lines.extend(("", "Totals: not summed; records include unresolved items and uncertain recipe/portion assumptions."))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(render_menu(args.snapshot))


if __name__ == "__main__":
    main()
