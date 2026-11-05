"""Compact WhatsApp representation of an optimization result."""

from __future__ import annotations

from datetime import date

from messmate_nutrition.optimizer import OptimizationResult, PlanStatus
from messmate_nutrition.serving_display import format_selected_portion


def _fmt(value) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_plan_message(plan_date: date, result: OptimizationResult) -> str:
    """Render a concise, estimate-labelled plan using household units."""
    lines = [f"MESSMATE | {plan_date.strftime('%d %b %Y')}"]
    if result.status is PlanStatus.PROTEIN_SHORTFALL:
        lines.append(f"Best effort only — protein shortfall: {_fmt(result.protein_shortfall_g)} g ({_fmt(result.totals.protein_g)}/120 g target)")
    elif result.status is PlanStatus.INFEASIBLE:
        lines.append(f"No target-compliant plan: {result.message}")
    else:
        lines.append("Protein target met (≥120 g).")

    for meal in ("Breakfast", "Lunch", "Snacks", "Dinner"):
        portions = [p for p in result.portions if p.meal == meal]
        if not portions and not (meal == "Snacks" and result.soya_dry_grams):
            continue
        lines.append(f"\n{meal}")
        lines.extend(f"• {p.food_name}: {format_selected_portion(p, compact=True)}" for p in portions)
        if meal == "Snacks" and result.soya_dry_grams:
            lines.append(f"• Soya chunks: {_fmt(result.soya_dry_grams)} g dry weight")
        if meal == "Breakfast" and result.fixed_plant_protein_scoops:
            lines.append("• Plant protein isolate: 1 scoop (35 g; label: 132 kcal, P 29 g, C 2 g, F 0.48 g)")

    carbs = "unknown" if result.totals.carbohydrates_g is None else f"{_fmt(result.totals.carbohydrates_g)} g"
    fat = "unknown" if result.totals.fat_g is None else f"{_fmt(result.totals.fat_g)} g"
    lines.extend((
        f"\nDaily estimate: {_fmt(result.totals.calories_kcal)} kcal | "
        f"Protein {_fmt(result.totals.protein_g)} g | Carbs {carbs} | Fat {fat}",
        "Nutrition values and household serving weights are estimates.",
    ))
    return "\n".join(lines)
