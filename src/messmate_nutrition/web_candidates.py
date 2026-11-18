"""Parse web recipe nutrition into candidate values; no network access here.

A source is usable only when its nutrition block states a gram serving size
(for example "1 small cup (katori) (101 g)") and numeric calories and protein.
Sources without a gram basis, or with batch-level values, are recorded as
unusable with a reason and never enter an average.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import re
from statistics import median
from typing import Any, Iterable, Sequence

MIN_SOURCES_FOR_AVERAGE = 3
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_GRAMS = re.compile(r"(\d+(?:\.\d+)?)\s*g\b", re.IGNORECASE)
_BATCH_WORDS = re.compile(r"\b(batch|whole|recipe|pot|total)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CandidateValue:
    source_url: str
    serving_text: str
    serving_grams: Decimal | None
    calories_kcal_per_100g: Decimal | None
    protein_g_per_100g: Decimal | None
    usable: bool
    reason: str


def _first_number(value: Any) -> Decimal | None:
    if value is None:
        return None
    match = _NUMBER.search(str(value))
    return Decimal(match.group(0)) if match else None


def _recipe_nodes(document: Any) -> Iterable[dict[str, Any]]:
    if isinstance(document, list):
        for item in document:
            yield from _recipe_nodes(item)
    elif isinstance(document, dict):
        if "@graph" in document:
            yield from _recipe_nodes(document["@graph"])
        kind = document.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "Recipe" in kinds:
            yield document


def parse_json_ld_recipes(html: str) -> list[dict[str, Any]]:
    """Return every schema.org Recipe object found in JSON-LD script blocks."""

    blocks = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I)
    recipes: list[dict[str, Any]] = []
    for block in blocks:
        try:
            document = json.loads(block)
        except json.JSONDecodeError:
            continue
        recipes.extend(_recipe_nodes(document))
    return recipes


def evaluate_recipe_nutrition(source_url: str, recipe: dict[str, Any]) -> CandidateValue:
    """Turn one Recipe's nutrition block into a usable or rejected candidate."""

    nutrition = recipe.get("nutrition")
    if not isinstance(nutrition, dict):
        return CandidateValue(source_url, "", None, None, None, False, "no nutrition block")
    serving_text = str(nutrition.get("servingSize", "")).strip()
    grams_match = _GRAMS.search(serving_text)
    if grams_match is None:
        return CandidateValue(source_url, serving_text, None, None, None, False, "no gram serving basis")
    grams = Decimal(grams_match.group(1))
    if grams <= 0:
        return CandidateValue(source_url, serving_text, grams, None, None, False, "non-positive serving weight")
    if _BATCH_WORDS.search(serving_text):
        return CandidateValue(source_url, serving_text, grams, None, None, False, "batch-level serving text")
    calories = _first_number(nutrition.get("calories"))
    protein = _first_number(nutrition.get("proteinContent"))
    if calories is None or protein is None:
        return CandidateValue(source_url, serving_text, grams, None, None, False, "calories or protein missing")
    per_100g_factor = Decimal("100") / grams
    return CandidateValue(
        source_url, serving_text, grams,
        (calories * per_100g_factor).quantize(Decimal("0.01")),
        (protein * per_100g_factor).quantize(Decimal("0.01")),
        True, "usable: gram serving basis stated",
    )


def evaluate_page(source_url: str, html: str) -> list[CandidateValue]:
    recipes = parse_json_ld_recipes(html)
    if not recipes:
        return [CandidateValue(source_url, "", None, None, None, False, "no schema.org Recipe data")]
    return [evaluate_recipe_nutrition(source_url, recipe) for recipe in recipes]


@dataclass(frozen=True)
class DishAverage:
    dish: str
    usable_sources: int
    calories_kcal_per_100g_median: Decimal | None
    protein_g_per_100g_median: Decimal | None
    protein_g_per_100g_min: Decimal | None
    protein_g_per_100g_max: Decimal | None
    note: str


def summarize_dish(dish: str, candidates: Sequence[CandidateValue]) -> DishAverage:
    """Median and range over usable candidates; refuses to average too few sources."""

    usable = [c for c in candidates if c.usable and c.calories_kcal_per_100g is not None and c.protein_g_per_100g is not None]
    if len(usable) < MIN_SOURCES_FOR_AVERAGE:
        protein = [c.protein_g_per_100g for c in usable if c.protein_g_per_100g is not None]
        return DishAverage(
            dish, len(usable), None, None,
            min(protein) if protein else None, max(protein) if protein else None,
            f"not averaged: {len(usable)} usable source(s), need {MIN_SOURCES_FOR_AVERAGE}",
        )
    kcal = [c.calories_kcal_per_100g for c in usable]
    protein = [c.protein_g_per_100g for c in usable]
    return DishAverage(
        dish, len(usable), Decimal(str(median(kcal))), Decimal(str(median(protein))),
        min(protein), max(protein), f"median of {len(usable)} usable sources",
    )
