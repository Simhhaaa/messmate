"""Load the private user profile from runtime environment configuration."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import os
from pathlib import Path

from .optimizer import UserNutritionProfile


class ProfileConfigurationError(ValueError):
    """Raised when required private profile values are absent or invalid."""


_REQUIRED = (
    "MESSMATE_USER_HEIGHT_CM",
    "MESSMATE_USER_WEIGHT_KG",
    "MESSMATE_USER_GOAL",
)


def _load_private_file(path: Path) -> dict[str, str]:
    """Read only the three profile keys; never execute a shell or log values."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                raise ProfileConfigurationError(f"invalid private profile configuration at line {line_number}")
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in _REQUIRED:
                value = value.strip().strip("\"'")
                if not value:
                    raise ProfileConfigurationError(f"{key} must not be empty")
                values[key] = value
    except OSError as exc:
        raise ProfileConfigurationError("could not read private profile configuration") from exc
    return values


def load_user_profile(
    *, environ: dict[str, str] | None = None, private_file: Path = Path(".env.local")
) -> UserNutritionProfile:
    """Load profile values, preferring the process environment over .env.local.

    The file is a local-development convenience. CI should provide the same
    names through protected environment variables / GitHub Actions Secrets.
    """
    env = os.environ if environ is None else environ
    file_values = _load_private_file(private_file)
    values = {key: env.get(key) or file_values.get(key) for key in _REQUIRED}
    missing = [key for key, value in values.items() if value is None or not value.strip()]
    if missing:
        raise ProfileConfigurationError(
            "missing required private nutrition profile setting(s): " + ", ".join(missing)
        )
    try:
        height = Decimal(values["MESSMATE_USER_HEIGHT_CM"].strip())
        weight = Decimal(values["MESSMATE_USER_WEIGHT_KG"].strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ProfileConfigurationError("height and weight profile settings must be decimal numbers") from exc
    try:
        return UserNutritionProfile(
            height_cm=height,
            weight_kg=weight,
            goal=values["MESSMATE_USER_GOAL"].strip(),
        )
    except (ValueError, AttributeError) as exc:
        raise ProfileConfigurationError(f"invalid private nutrition profile: {exc}") from exc
