"""Playwright adapter for RU Print Portal."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .models import (
    DailyMenu,
    MEAL_NAMES,
    MealSection,
    MenuNotPublishedError,
    PortalStructureError,
    PortalTemporarilyUnavailableError,
    RetryPolicy,
    ScrapeRetryExhausted,
)
from .parser import (
    extract_meal_items,
    parse_portal_today,
    selected_weekday,
    validate_target_date,
    weekday_button_label,
)
from .snapshots import write_snapshot

PORTAL_URL = "https://ru-print.vercel.app/"
IST = ZoneInfo("Asia/Kolkata")


async def retry_async(
    operation: Callable[[int], Awaitable[DailyMenu]],
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DailyMenu:
    """Retry only expected not-ready/transient portal failures."""

    retryable = (MenuNotPublishedError, PortalTemporarilyUnavailableError)
    last_error: Exception | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation(attempt)
        except retryable as exc:
            last_error = exc
            if attempt == policy.attempts:
                break
            await sleep(policy.delay_after(attempt))
    assert last_error is not None
    raise ScrapeRetryExhausted(policy.attempts, last_error) from last_error


class PortalScraper:
    """Fetch and parse one date from the portal's visible menu UI."""

    def __init__(
        self,
        snapshot_dir: Path = Path("data/raw_snapshots"),
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        url: str = PORTAL_URL,
    ) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.retry_policy = retry_policy or RetryPolicy()
        self.clock = clock or (lambda: datetime.now(IST))
        self.url = url

    async def scrape(self, target_date: date | None = None) -> DailyMenu:
        """Launch Chromium and fetch all four sections for ``target_date``."""

        if target_date is None:
            target_date = self.clock().astimezone(IST).date()

        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required. Install the project and run `playwright install chromium`."
            ) from exc

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
            page = await context.new_page()
            page.set_default_timeout(10_000)

            async def operation(attempt: int) -> DailyMenu:
                try:
                    await page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightTimeoutError as exc:
                    await self._save_failure_snapshot(
                        page, target_date, attempt, "navigation_timeout", str(exc), []
                    )
                    raise PortalTemporarilyUnavailableError("Portal navigation timed out") from exc
                try:
                    return await self._scrape_loaded_page(page, target_date, attempt)
                except PlaywrightTimeoutError as exc:
                    raise PortalTemporarilyUnavailableError(
                        "Portal controls or menu content did not become ready before timeout"
                    ) from exc

            try:
                return await retry_async(operation, self.retry_policy)
            finally:
                await context.close()
                await browser.close()

    async def _scrape_loaded_page(self, page: Any, target_date: date, attempt: int) -> DailyMenu:
        fetched_at = self.clock().astimezone(IST)
        snapshot: dict[str, Any] = {
            "source_url": self.url,
            "requested_date": target_date.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "attempt": attempt,
            "status": "in_progress",
            "portal_today": None,
            "selected_weekday": None,
            "meals": {},
        }

        try:
            await page.get_by_role("heading", name="Breakfast", exact=True).wait_for(
                state="visible", timeout=15_000
            )
            body_text = await page.locator("body").inner_text()
            snapshot["initial_page"] = {
                "visible_text": body_text,
                "html": await page.content(),
            }
            await self._dismiss_initial_overlay(page)
            body_text = await page.locator("body").inner_text()
            portal_today = parse_portal_today(body_text, fetched_at.date())
            snapshot["portal_today"] = portal_today.isoformat()
            validate_target_date(target_date, portal_today)

            if target_date == portal_today:
                snapshot["selected_weekday"] = portal_today.strftime("%A").upper()
            else:
                button_name = weekday_button_label(target_date)
                button = page.get_by_role("button", name=button_name, exact=True)
                if await button.count() != 1:
                    raise PortalStructureError(f"Expected one weekday button named {button_name}")
                await button.click()
                body_text = await page.locator("body").inner_text()
                actual_weekday = selected_weekday(body_text)
                expected_weekday = target_date.strftime("%A").upper()
                if actual_weekday != expected_weekday:
                    raise PortalStructureError(
                        f"Selected menu heading was {actual_weekday!r}; expected {expected_weekday}"
                    )
                snapshot["selected_weekday"] = actual_weekday

            meal_sections: list[MealSection] = []
            for meal_name in MEAL_NAMES:
                heading = page.get_by_role("heading", name=meal_name, exact=True)
                if await heading.count() != 1:
                    raise PortalStructureError(f"Expected one meal heading named {meal_name}")
                await heading.click()
                body_text = await page.locator("body").inner_text()
                section = extract_meal_items(body_text, meal_name)
                meal_sections.append(section)
                snapshot["meals"][meal_name] = {
                    "visible_text": body_text,
                    "items": list(section.items),
                    "raw_section_text": section.raw_text,
                    "html": await page.content(),
                }

            snapshot["status"] = "complete"
            snapshot_path = write_snapshot(
                self.snapshot_dir, target_date, attempt, snapshot
            )
            return DailyMenu(
                menu_date=target_date,
                portal_today=portal_today,
                fetched_at=fetched_at,
                source_url=self.url,
                meals=tuple(meal_sections),
                snapshot_path=str(snapshot_path),
            )
        except (MenuNotPublishedError, PortalStructureError):
            snapshot["status"] = "incomplete"
            snapshot["error"] = "A meal was empty or the page structure/date was invalid"
            snapshot["last_visible_text"] = await _safe_body_text(page)
            snapshot["html"] = await _safe_page_content(page)
            write_snapshot(self.snapshot_dir, target_date, attempt, snapshot)
            raise
        except Exception as exc:
            snapshot["status"] = "error"
            snapshot["error"] = f"{type(exc).__name__}: {exc}"
            snapshot["last_visible_text"] = await _safe_body_text(page)
            snapshot["html"] = await _safe_page_content(page)
            write_snapshot(self.snapshot_dir, target_date, attempt, snapshot)
            raise

    async def _dismiss_initial_overlay(self, page: Any) -> None:
        """Close the portal's informational feedback overlay when it is present."""

        ok_button = page.get_by_role("button", name="OK", exact=True)
        count = await ok_button.count()
        if count > 1:
            raise PortalStructureError("Found multiple OK buttons; cannot safely dismiss the overlay")
        if count == 1:
            await ok_button.click()

    async def _save_failure_snapshot(
        self,
        page: Any,
        target_date: date,
        attempt: int,
        error_type: str,
        error: str,
        meals: list[dict[str, Any]],
    ) -> None:
        write_snapshot(
            self.snapshot_dir,
            target_date,
            attempt,
            {
                "source_url": self.url,
                "requested_date": target_date.isoformat(),
                "fetched_at": self.clock().astimezone(IST).isoformat(),
                "attempt": attempt,
                "status": "error",
                "error_type": error_type,
                "error": error,
                "meals": meals,
                "last_visible_text": await _safe_body_text(page),
                "html": await _safe_page_content(page),
            },
        )


async def _safe_body_text(page: Any) -> str | None:
    try:
        return await page.locator("body").inner_text()
    except Exception:
        return None


async def _safe_page_content(page: Any) -> str | None:
    try:
        return await page.content()
    except Exception:
        return None
