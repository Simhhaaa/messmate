"""Fail-closed runtime configuration for scheduled daily runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os


class RunConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class RunMode:
    dry_run: bool
    live_send: bool


def resolve_channel(environ: dict[str, str] | None = None) -> str:
    """Select a delivery channel; Telegram is the new default."""
    env = os.environ if environ is None else environ
    channel = env.get("MESSMATE_CHANNEL", "telegram").strip().casefold()
    if channel not in {"telegram", "whatsapp"}:
        raise RunConfigurationError("MESSMATE_CHANNEL must be telegram or whatsapp")
    return channel


def _boolean(name: str, raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    normalized = raw.strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise RunConfigurationError(f"{name} must be a boolean value")


def resolve_run_mode(environ: dict[str, str] | None = None) -> RunMode:
    """Default to dry-run; live sending needs two explicit gates."""
    env = os.environ if environ is None else environ
    dry_run = _boolean("MESSMATE_DRY_RUN", env.get("MESSMATE_DRY_RUN"), default=True)
    live_requested = env.get("MESSMATE_LIVE_SEND", "false").strip().casefold() == "true"
    if not dry_run and not live_requested:
        raise RunConfigurationError("dry-run can be disabled only when MESSMATE_LIVE_SEND=true")
    # A configured live secret alone never overrides a dry-run choice.
    return RunMode(dry_run=dry_run, live_send=(live_requested and not dry_run))


def resolve_target_date(raw: str | None, *, today: date) -> date:
    if raw is None or not raw.strip():
        return today
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise RunConfigurationError("MESSMATE_TARGET_DATE must use YYYY-MM-DD") from exc
