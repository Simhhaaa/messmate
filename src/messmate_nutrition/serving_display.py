"""Household-unit labels for presenting selected weights without changing math."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping

from .optimizer import SelectedPortion


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class HouseholdServing:
    """Display unit and its configurable estimated weight for one food."""

    singular: str
    plural: str
    grams_per_unit: Decimal
    weight_basis: str

    def __post_init__(self) -> None:
        if self.grams_per_unit <= 0:
            raise ValueError("grams_per_unit must be positive")
        if not self.singular.strip() or not self.plural.strip() or not self.weight_basis.strip():
            raise ValueError("serving labels and weight basis cannot be empty")


# These weights are display defaults and kept separate from nutrition inputs.
# A user can override each food's unit weight without changing macro math.
DEFAULT_HOUSEHOLD_SERVINGS: dict[str, HouseholdServing] = {
    "banana": HouseholdServing("medium banana", "medium bananas", Decimal("118"), "edible weight"),
    "masala_oats": HouseholdServing("medium bowl", "medium bowls", Decimal("200"), "prepared weight"),
    "muesli": HouseholdServing("dry cereal serving", "dry cereal servings", Decimal("45"), "dry weight"),
    "sada_paratha": HouseholdServing("medium paratha", "medium parathas", Decimal("70"), "cooked weight"),
    "chana_palak": HouseholdServing("medium katori", "medium katoris", Decimal("150"), "cooked weight"),
    "hot_milk": HouseholdServing("cup", "cups", Decimal("150"), "served weight estimate"),
    "cold_milk": HouseholdServing("cup", "cups", Decimal("150"), "served weight estimate"),
    "tea_d": HouseholdServing("cup", "cups", Decimal("150"), "served weight estimate"),
    "coffee_powder": HouseholdServing("measured portion", "measured portions", Decimal("5"), "dry weight estimate"),
    "bread_butter_jam": HouseholdServing("bread-and-spread serving", "bread-and-spread servings", Decimal("100"), "served weight estimate"),
    "arhar_dal": HouseholdServing("medium bowl", "medium bowls", Decimal("180"), "cooked weight"),
    "paneer_do_pyaza": HouseholdServing("medium bowl", "medium bowls", Decimal("180"), "cooked weight"),
    "ullipai_pacchadi": HouseholdServing("small bowl", "small bowls", Decimal("25"), "served weight estimate"),
    "curd": HouseholdServing("small bowl", "small bowls", Decimal("100"), "served weight estimate"),
    "green_salad": HouseholdServing("side portion", "side portions", Decimal("100"), "raw weight estimate"),
    "jeera_rice": HouseholdServing("medium bowl", "medium bowls", Decimal("180"), "cooked weight"),
    "chapati": HouseholdServing("medium chapati", "medium chapatis", Decimal("40"), "cooked weight"),
    "veg_puff": HouseholdServing("piece", "pieces", Decimal("80"), "baked weight estimate"),
    "hot_coffee": HouseholdServing("cup", "cups", Decimal("150"), "served weight estimate"),
    "ketchup": HouseholdServing("small sachet", "small sachets", Decimal("15"), "served weight estimate"),
    "chole_masala": HouseholdServing("medium bowl", "medium bowls", Decimal("180"), "cooked weight"),
    "jeera_aloo": HouseholdServing("small bowl", "small bowls", Decimal("150"), "cooked weight"),
    "rasam": HouseholdServing("medium bowl", "medium bowls", Decimal("180"), "prepared weight"),
    "steamed_rice": HouseholdServing("cooked cup", "cooked cups", Decimal("185"), "cooked weight; USDA SR28 measure"),
    "french_fries": HouseholdServing("medium serving", "medium servings", Decimal("117"), "prepared weight; USDA SR28 measure"),
    "mix_yellow_dal": HouseholdServing("medium katori", "medium katoris", Decimal("180"), "cooked weight; estimated average"),
    "menthi_dal_tadka": HouseholdServing("medium katori", "medium katoris", Decimal("180"), "cooked weight; estimated average"),
    "palakura_pappu": HouseholdServing("medium katori", "medium katoris", Decimal("180"), "cooked weight; estimated average"),
    "panchratna_dal": HouseholdServing("medium katori", "medium katoris", Decimal("180"), "cooked weight; estimated average"),
    "pappu_charu": HouseholdServing("medium bowl", "medium bowls", Decimal("180"), "prepared weight; estimated average"),
    "dhaba_dal": HouseholdServing("medium katori", "medium katoris", Decimal("180"), "cooked weight; estimated average"),
}


def format_selected_portion(
    portion: SelectedPortion,
    *,
    serving_config: Mapping[str, HouseholdServing] | None = None,
    compact: bool = False,
) -> str:
    """Render a selected portion as a household count plus estimated grams.

    This function only formats ``portion.total_grams``; it never feeds display
    weights back into the optimizer or nutrition calculations.
    """
    configs = DEFAULT_HOUSEHOLD_SERVINGS if serving_config is None else {**DEFAULT_HOUSEHOLD_SERVINGS, **serving_config}
    config = configs.get(portion.food_key)
    if config is None:
        config = HouseholdServing("serving", "servings", portion.serving_grams, "estimated weight")
    units = (portion.total_grams / config.grams_per_unit).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if units == units.to_integral_value():
        unit_count = str(int(units))
    else:
        unit_count = f"about {units.normalize()}"
    label = config.singular if units == Decimal("1") else config.plural
    if compact:
        return f"{unit_count} {label} (~{_number(portion.total_grams)} g; {config.weight_basis})"
    return (f"{unit_count} {label} (estimated {_number(portion.total_grams)} g total; "
            f"{config.weight_basis}; about {_number(config.grams_per_unit)} g per configured unit)")


def format_fixed_supplement(label: str, quantity: str, grams: Decimal, weight_basis: str) -> str:
    """Format a non-optimized fixed supplement serving."""
    return f"{label}: {quantity} (estimated {_number(grams)} g; {weight_basis})"
