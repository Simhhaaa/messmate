"""Create a local dry-run message from a saved menu snapshot."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from messmate_nutrition.optimizer import DietOptimizer, menu_items_from_snapshot
from messmate_nutrition.profile_config import load_user_profile
from .service import DietPlanDeliveryService
from .store import SQLiteDeliveryStore


def build_dry_run(snapshot_path: Path) -> str:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    plan_date = date.fromisoformat(str(snapshot["requested_date"]))
    result = DietOptimizer(profile=load_user_profile()).optimize(menu_items_from_snapshot(snapshot))
    # A local SQLite store persists only a prepared marker for this demonstration.
    outcome = DietPlanDeliveryService(SQLiteDeliveryStore(), dry_run=True).deliver(
        "+10000000000", plan_date, result)
    return outcome.message


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(build_dry_run(args.snapshot))


if __name__ == "__main__":
    main()
