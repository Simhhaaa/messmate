"""Typed data models and errors for the portal scraper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

MealName = Literal["Breakfast", "Lunch", "Snacks", "Dinner"]
MEAL_NAMES: tuple[MealName, ...] = ("Breakfast", "Lunch", "Snacks", "Dinner")


class ScrapeError(RuntimeError):
    """Base class for expected scraper failures."""


class PortalStructureError(ScrapeError):
    """The portal no longer exposes an expected control or label."""


class MenuNotPublishedError(ScrapeError):
    """A requested meal section exists but has no extractable menu items yet."""


class PortalTemporarilyUnavailableError(ScrapeError):
    """A transient navigation or page-load failure occurred."""


class ScrapeRetryExhausted(ScrapeError):
    """All configured attempts failed; ``last_error`` explains the cause."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Scrape failed after {attempts} attempts: {last_error}")


@dataclass(frozen=True)
class MealSection:
    name: MealName
    items: tuple[str, ...]
    raw_text: str


@dataclass(frozen=True)
class DailyMenu:
    menu_date: date
    portal_today: date
    fetched_at: datetime
    source_url: str
    meals: tuple[MealSection, ...]
    snapshot_path: str


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry policy for a menu that is not ready or a transient failure."""

    attempts: int = 3
    initial_delay_seconds: float = 30.0
    multiplier: float = 2.0
    max_delay_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")

    def delay_after(self, failed_attempt: int) -> float:
        if failed_attempt < 1:
            raise ValueError("failed_attempt must be at least 1")
        delay = self.initial_delay_seconds * self.multiplier ** (failed_attempt - 1)
        return min(delay, self.max_delay_seconds)
