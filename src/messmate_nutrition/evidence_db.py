"""Local SQLite evidence database for MESSMATE nutrition research.

The database records what is known and what is still missing. Dish rows keep
the evidence status, the kitchen fields still needed, and the sources checked.
Macro values for estimated dishes live only in the catalog (flagged as the
estimated tier); this database holds no macro columns for dishes. Ingredient
rows are IFCT 2017 reference values, which are not dish values.

The file is generated into ``data/research/`` (git-ignored) and can be rebuilt
from committed inputs at any time.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping

from .catalog import lookup_food
from .optimizer import DietOptimizer, UserNutritionProfile, menu_items_from_snapshot

REPO_ROOT = Path(__file__).resolve().parents[2]
IFCT_CSV = REPO_ROOT / "docs" / "ifct_2017_ingredient_references.csv"
DEFAULT_DB = REPO_ROOT / "data" / "research" / "messmate_evidence.sqlite"

# Synthetic profile used only for diagnostics; the production profile is private.
DIAGNOSTIC_PROFILE = UserNutritionProfile(
    height_cm=Decimal("170"), weight_kg=Decimal("70"), goal="Synthetic diagnostic profile"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ifct_ingredient_reference (
    ifct_code TEXT PRIMARY KEY,
    ingredient_name TEXT NOT NULL,
    regions INTEGER,
    protein_g_per_100g REAL,
    fat_g_per_100g REAL,
    available_carbohydrate_g_per_100g REAL,
    energy_kcal_per_100g REAL,
    basis TEXT NOT NULL DEFAULT 'IFCT 2017 Table 1 sampled mean; ingredient only, not a dish'
);
CREATE TABLE IF NOT EXISTS dish_evidence (
    dish_name TEXT PRIMARY KEY,
    menu_dates TEXT NOT NULL,
    priority TEXT NOT NULL,
    catalog_key TEXT,
    nutrition_status TEXT NOT NULL,
    decision TEXT NOT NULL,
    missing_kitchen_fields TEXT NOT NULL,
    sources_checked TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_check (
    query TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    what_it_gave TEXT NOT NULL,
    usable INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (query, source_url)
);
CREATE TABLE IF NOT EXISTS menu_occurrence (
    menu_date TEXT NOT NULL,
    meal TEXT NOT NULL,
    item_name TEXT NOT NULL,
    catalog_key TEXT,
    eligible INTEGER NOT NULL,
    exclusion_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (menu_date, meal, item_name)
);
CREATE TABLE IF NOT EXISTS menu_run_diagnostic (
    menu_date TEXT PRIMARY KEY,
    optimizer_status TEXT NOT NULL,
    calories_kcal REAL NOT NULL,
    protein_g REAL NOT NULL,
    protein_shortfall_g REAL NOT NULL,
    eligible_count INTEGER NOT NULL,
    excluded_count INTEGER NOT NULL,
    profile TEXT NOT NULL
);
"""


# Evidence status recorded from the worksheet review and the web source checks
# run on 18 Nov 2026. Nutrition is deliberately absent for every row below.
PRIORITY_DISHES: tuple[dict[str, str], ...] = (
    {
        "dish_name": "Mix Yellow Dal",
        "menu_dates": "2026-11-07",
        "priority": "1 (highest)",
        "decision": "Estimated tier: IFCT-based hostel average (assumed blend and ratios). Weighed kitchen data still needed.",
        "missing_kitchen_fields": "dal species and blend %, raw dry weights, oil/fat grams, water, batch yield, served weight, recipe variation",
        "sources_checked": "WORKSHEET; web: Tarla Dalal spicy mixed dal (adds masoor/urad, not the mess recipe)",
    },
    {
        "dish_name": "Menthi Dal Tadka",
        "menu_dates": "2026-11-07",
        "priority": "1 (highest)",
        "decision": "Estimated tier: IFCT-based hostel average (assumed dal and fenugreek ratios). Weighed kitchen data still needed.",
        "missing_kitchen_fields": "dal species/weights, fenugreek form and raw weight, aromatics, tadka oil, batch yield, served weight",
        "sources_checked": "WORKSHEET; web: Tarla Dalal, clearcals, Slurrp methi dal (serving sizes inconsistent: 87–287 kcal)",
    },
    {
        "dish_name": "Palakura Pappu",
        "menu_dates": "2026-11-07",
        "priority": "1 (highest)",
        "decision": "Estimated tier: IFCT-based hostel average (assumed dal and spinach ratio). Weighed kitchen data still needed.",
        "missing_kitchen_fields": "dal species/weights, raw spinach weight, aromatics, tadka oil, batch yield, served weight",
        "sources_checked": "WORKSHEET; web: Slurrp, FatSecret, SparkPeople (62–836 kcal per serving: conflicting)",
    },
    {
        "dish_name": "Panchratna Dal",
        "menu_dates": "2026-11-18",
        "priority": "new dal (18 Nov dinner)",
        "decision": "Estimated tier: equal-split five-dal average (toor not in references). Weighed kitchen data still needed.",
        "missing_kitchen_fields": "each pulse and weight, aromatics, oil, batch yield, served weight",
        "sources_checked": "MENU ONLY (not yet researched)",
    },
    {
        "dish_name": "Dhaba Dal",
        "menu_dates": "2026-11-18",
        "priority": "new dal (18 Nov lunch)",
        "decision": "Estimated tier: IFCT-based hostel average (assumed lentil/black gram split). Weighed kitchen data still needed.",
        "missing_kitchen_fields": "pulse species/blend, oil, batch yield, served weight",
        "sources_checked": "MENU ONLY (not yet researched)",
    },
    {
        "dish_name": "Pappu Charu",
        "menu_dates": "2026-11-18",
        "priority": "new dal (18 Nov dinner)",
        "decision": "Estimated tier: thin dal-broth average (tamarind/tomato not counted). Weighed kitchen data still needed.",
        "missing_kitchen_fields": "pulse species/weights, tamarind/tomato/spice quantities, oil, batch yield, served weight",
        "sources_checked": "MENU ONLY (not yet researched)",
    },
)

SOURCE_CHECKS: tuple[dict[str, object], ...] = (
    {
        "query": "Menthi dal tadka fenugreek nutrition",
        "source_title": "Tarla Dalal: Methi Toovar Dal (healthy)",
        "source_url": "https://www.tarladalal.com/calories-for-Healthy-Methi-Dal-Methi-Dal-Dry-42405",
        "what_it_gave": "Per-serving energy 175 kcal, protein 9.8 g; serving weight not stated in the search snippet",
        "usable": 0,
        "reason": "Serving weight and batch yield missing; recipe differs from mess preparation",
    },
    {
        "query": "Menthi dal tadka fenugreek nutrition",
        "source_title": "clearcals: Methi dal",
        "source_url": "https://clearcals.com/recipes/methi-dal",
        "what_it_gave": "86.6 kcal and 3.9 g protein per 100 g",
        "usable": 0,
        "reason": "Aggregator entry without recipe weights; cannot be traced to ingredients",
    },
    {
        "query": "Menthi dal tadka fenugreek nutrition",
        "source_title": "Slurrp: Methi dal",
        "source_url": "https://www.slurrp.com/amp/recipes/methi-dal-1698671358926",
        "what_it_gave": "287 kcal and 17 g protein per serving (serving size not stated)",
        "usable": 0,
        "reason": "Serving size unknown; conflicts with other sources by about 3x",
    },
    {
        "query": "Palakura pappu spinach dal nutrition",
        "source_title": "Slurrp: Spinach dal palakura pappu with methi leaves",
        "source_url": "https://www.slurrp.com/amp/recipes/spinach-dal-palakura-pappu-methi-leaves-1617968318",
        "what_it_gave": "250 kcal and 16 g protein per serving",
        "usable": 0,
        "reason": "Recipe includes methi leaves and toor dal only; batch yield and servings not verified",
    },
    {
        "query": "Palakura pappu spinach dal nutrition",
        "source_title": "Slurrp: Andhra palakura pappu",
        "source_url": "https://slurrp.com/recipes/andhra-palakura-pappu-1612539290",
        "what_it_gave": "836 kcal and 53 g protein per serving (likely full batch)",
        "usable": 0,
        "reason": "Per-serving basis appears to be the whole batch; implausible per serving",
    },
    {
        "query": "Palakura pappu spinach dal nutrition",
        "source_title": "FatSecret: Palakura pappu",
        "source_url": "https://www.fatsecret.co.in/calories-nutrition/generic/palakura-pappu",
        "what_it_gave": "110 kcal per 100 g; about 14.9 g protein per 100 g basis",
        "usable": 0,
        "reason": "Generic entry; the protein and kcal bases disagree with each other; no recipe",
    },
    {
        "query": "Mix yellow dal toor moong chana nutrition",
        "source_title": "Tarla Dalal: Spicy mixed dal",
        "source_url": "https://www.tarladalal.com/calories-for-spicy-mixed-dal-60",
        "what_it_gave": "160 kcal, 5.5 g protein per serving; serving weight not stated",
        "usable": 0,
        "reason": "Recipe adds masoor and urad dal; serving weight unknown",
    },
    {
        "query": "Mix yellow dal toor moong chana nutrition",
        "source_title": "Hassan Chef: Moong dal nutrition",
        "source_url": "https://www.hassanchef.com/2026/04/moong-dal-nutrition-protein-benefits.html",
        "what_it_gave": "Raw moong dal protein about 23.9 g per 100 g; cooked about 7 g per 100 g",
        "usable": 0,
        "reason": "Single-pulse values only; blend and cooking water ratio unknown",
    },
)


def _dish_row(dish: Mapping[str, str]) -> dict[str, object]:
    """Describe a dish's evidence status using the catalog as the single source."""

    record = lookup_food(dish["dish_name"])
    if record is not None and record.estimate_range is not None:
        status = f"estimated (hostel-mess average, ±{int(record.estimate_range * 100)}% range check)"
        catalog_key = record.key
    else:
        status = "unverified"
        catalog_key = None
    return {**dish, "catalog_key": catalog_key, "nutrition_status": status}


@dataclass(frozen=True)
class MenuFixture:
    menu_date: date
    snapshot: Mapping[str, object]


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


def _load_ifct_rows(csv_path: Path = IFCT_CSV) -> list[tuple[object, ...]]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append((
                row["IFCT code"],
                row["Ingredient as named in IFCT"],
                int(row["Regions"]),
                float(row["Protein mean g/100g"]),
                float(row["Fat mean g/100g"]),
                float(row["Available carbohydrate mean g/100g"]),
                round(float(row["Energy mean kcal/100g"]), 2),
            ))
        return rows


def _menu_rows(fixture: MenuFixture) -> tuple[list[tuple[object, ...]], dict[str, object]]:
    items = menu_items_from_snapshot(dict(fixture.snapshot))
    result = DietOptimizer(profile=DIAGNOSTIC_PROFILE).optimize(items)
    excluded = {(meal, name): reason for meal, name, reason in result.excluded_items}
    rows = []
    for item in items:
        raw_name = item.food.display_name
        matched = lookup_food(raw_name)
        reason = excluded.get((item.meal, raw_name), "")
        rows.append((
            fixture.menu_date.isoformat(),
            item.meal,
            raw_name,
            matched.key if matched else None,
            0 if reason else 1,
            reason,
        ))
    diagnostic = {
        "menu_date": fixture.menu_date.isoformat(),
        "optimizer_status": result.status.value,
        "calories_kcal": float(result.totals.calories_kcal),
        "protein_g": float(result.totals.protein_g),
        "protein_shortfall_g": float(result.protein_shortfall_g),
        "eligible_count": sum(1 for row in rows if row[4]),
        "excluded_count": sum(1 for row in rows if not row[4]),
        "profile": "synthetic diagnostic (170 cm, 70 kg)",
    }
    return rows, diagnostic


def build_database(
    path: Path,
    fixtures: Iterable[MenuFixture],
    ifct_csv: Path = IFCT_CSV,
) -> Path:
    """Rebuild the evidence database from committed inputs and menu fixtures."""

    if path.exists():
        path.unlink()
    connection = _connect(path)
    try:
        with connection:
            connection.executemany(
                "INSERT INTO ifct_ingredient_reference (ifct_code, ingredient_name, regions, protein_g_per_100g,"
                " fat_g_per_100g, available_carbohydrate_g_per_100g, energy_kcal_per_100g) VALUES (?,?,?,?,?,?,?)",
                _load_ifct_rows(ifct_csv),
            )
            connection.executemany(
                "INSERT INTO dish_evidence VALUES (:dish_name, :menu_dates, :priority, :catalog_key, :nutrition_status,"
                " :decision, :missing_kitchen_fields, :sources_checked)",
                [_dish_row(dish) for dish in PRIORITY_DISHES],
            )
            connection.executemany(
                "INSERT INTO source_check (query, source_title, source_url, what_it_gave, usable, reason)"
                " VALUES (:query, :source_title, :source_url, :what_it_gave, :usable, :reason)",
                SOURCE_CHECKS,
            )
            for fixture in fixtures:
                rows, diagnostic = _menu_rows(fixture)
                connection.executemany(
                    "INSERT INTO menu_occurrence (menu_date, meal, item_name, catalog_key, eligible, exclusion_reason)"
                    " VALUES (?,?,?,?,?,?)",
                    rows,
                )
                connection.execute(
                    "INSERT INTO menu_run_diagnostic VALUES (:menu_date, :optimizer_status, :calories_kcal,"
                    " :protein_g, :protein_shortfall_g, :eligible_count, :excluded_count, :profile)",
                    diagnostic,
                )
    finally:
        connection.close()
    return path


FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
MENU_FIXTURES = (
    MenuFixture(date(2026, 11, 6), "nov6_menu_fixture.json"),
    MenuFixture(date(2026, 11, 7), "nov7_menu_fixture.json"),
    MenuFixture(date(2026, 11, 18), "nov18_menu_fixture.json"),
)


def main() -> None:
    import json

    loaded = [
        MenuFixture(fixture.menu_date, json.loads((FIXTURE_DIR / fixture.snapshot).read_text(encoding="utf-8")))
        for fixture in MENU_FIXTURES
    ]
    path = build_database(DEFAULT_DB, loaded)
    print(f"Evidence database written: {path}")


if __name__ == "__main__":
    main()
