"""Atomic JSON snapshots of each expanded portal state."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any


def write_snapshot(snapshot_dir: Path, menu_date: date, attempt: int, payload: dict[str, Any]) -> Path:
    """Write a raw snapshot atomically so interrupted runs leave no partial file."""

    target_dir = snapshot_dir / menu_date.isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"attempt-{attempt:02d}.json"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write(target_path, encoded + "\n")

    initial_page = payload.get("initial_page")
    if isinstance(initial_page, dict):
        _write_page_sidecars(target_dir, attempt, "initial", initial_page)

    meals = payload.get("meals")
    if isinstance(meals, dict):
        for meal_name, state in meals.items():
            if isinstance(state, dict):
                slug = str(meal_name).lower().replace(" ", "-")
                _write_page_sidecars(target_dir, attempt, slug, state)

    return target_path


def _write_page_sidecars(
    target_dir: Path, attempt: int, name: str, page_state: dict[str, Any]
) -> None:
    for field, suffix in (("html", "html"), ("visible_text", "txt")):
        value = page_state.get(field)
        if isinstance(value, str):
            _atomic_write(target_dir / f"attempt-{attempt:02d}-{name}.{suffix}", value + "\n")


def _atomic_write(target_path: Path, content: str) -> None:
    """Write a text artifact atomically in the target directory."""

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, target_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
