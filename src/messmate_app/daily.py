"""Run one end-to-end scrape, optimize, and delivery-preparation cycle."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import logging
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from messmate_delivery.adapters import WhatsAppCloudAPIAdapter
from messmate_delivery.models import DeliveryOutcome
from messmate_delivery.neon_store import NeonDeliveryStore
from messmate_delivery.service import DietPlanDeliveryService
from messmate_nutrition.optimizer import DietOptimizer, UserNutritionProfile, menu_items_from_snapshot
from messmate_nutrition.profile_config import load_user_profile
from messmate_scraper.models import DailyMenu, RetryPolicy
from messmate_scraper.portal import PortalScraper

from .config import RunConfigurationError, RunMode, resolve_run_mode, resolve_target_date

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("messmate.daily")


@dataclass(frozen=True)
class DailyRunResult:
    plan_date: date
    outcome: DeliveryOutcome
    report_path: Path


def _snapshot_for_menu(menu: DailyMenu) -> dict[str, Any]:
    return {
        "requested_date": menu.menu_date.isoformat(),
        "meals": {
            section.name.value if hasattr(section.name, "value") else str(section.name): {
                "items": list(section.items),
            }
            for section in menu.meals
        },
    }


def _write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


async def run_daily_pipeline(
    *,
    target_date: date,
    mode: RunMode,
    recipient: str,
    report_dir: Path = Path("reports"),
    snapshot_dir: Path = Path("data/raw_snapshots"),
    scraper: Any | None = None,
    store: Any | None = None,
    adapter: Any | None = None,
    profile: UserNutritionProfile | None = None,
) -> DailyRunResult:
    """Execute the production pipeline; dependencies are injectable for tests."""
    report_path = report_dir / f"messmate-diet-{target_date.isoformat()}.txt"
    stage = "configuration"
    try:
        if not mode.dry_run and not mode.live_send:
            raise RunConfigurationError("live mode requires MESSMATE_LIVE_SEND=true")
        stage = "profile configuration"
        profile = profile or load_user_profile()
        stage = "scrape"
        scraper = scraper or PortalScraper(
            snapshot_dir=snapshot_dir,
            retry_policy=RetryPolicy(
                attempts=int(os.getenv("MESSMATE_MENU_RETRY_ATTEMPTS", "5")),
                initial_delay_seconds=float(os.getenv("MESSMATE_MENU_RETRY_INITIAL_SECONDS", "120")),
                multiplier=float(os.getenv("MESSMATE_MENU_RETRY_MULTIPLIER", "2")),
                max_delay_seconds=float(os.getenv("MESSMATE_MENU_RETRY_MAX_SECONDS", "900")),
            ),
        )
        menu = await scraper.scrape(target_date)
        if menu.menu_date != target_date:
            raise ValueError("scraper returned a menu for a different date")

        stage = "optimize"
        snapshot = _snapshot_for_menu(menu)
        result = DietOptimizer(profile=profile).optimize(menu_items_from_snapshot(snapshot))

        stage = "database"
        store = store or NeonDeliveryStore()
        migrate = getattr(store, "migrate", None)
        if callable(migrate):
            migrate()

        stage = "delivery"
        if not mode.dry_run and adapter is None:
            adapter = WhatsAppCloudAPIAdapter.from_env()
        service = DietPlanDeliveryService(
            store,
            adapter,
            dry_run=mode.dry_run,
            live_send_enabled=mode.live_send,
            template_name=os.getenv("WHATSAPP_TEMPLATE_NAME", "messmate_diet_plan"),
        )
        outcome = service.deliver(recipient, target_date, result)
        # Artifacts are visible to repository users; keep them operational-only.
        # The private meal plan remains in memory for delivery and is never written.
        report = (
            f"MESSMATE run completed\nPlan date: {target_date.isoformat()}\n"
            f"Run mode: {'dry-run' if outcome.dry_run else 'live'}\n"
            f"Delivery status: {outcome.status.value}\n"
            "Meal plan and nutrition details are omitted for privacy.\n"
        )
        _write_report(report_path, report)
        logger.info("Daily run completed date=%s mode=%s status=%s", target_date, "dry-run" if outcome.dry_run else "live", outcome.status.value)
        return DailyRunResult(target_date, outcome, report_path)
    except Exception as exc:
        # Do not include exception messages: drivers/clients may embed connection
        # information or request data in those strings.
        logger.error("Daily run failed stage=%s error_type=%s", stage, type(exc).__name__)
        try:
            _write_report(
                report_path,
                f"MESSMATE run failed\nPlan date: {target_date.isoformat()}\nStage: {stage}\nError type: {type(exc).__name__}",
            )
        except OSError:
            logger.error("Could not write failure report")
        raise


async def _run_from_environment() -> DailyRunResult:
    today = datetime.now(IST).date()
    try:
        mode = resolve_run_mode()
        target_date = resolve_target_date(os.getenv("MESSMATE_TARGET_DATE"), today=today)
        recipient = os.environ.get("WHATSAPP_RECIPIENT", "")
        if not recipient:
            raise ValueError("WHATSAPP_RECIPIENT is required")
        profile = load_user_profile()
    except Exception as exc:
        logger.error("Daily run failed stage=configuration error_type=%s", type(exc).__name__)
        try:
            _write_report(
                Path("reports") / f"messmate-diet-{today.isoformat()}.txt",
                f"MESSMATE run failed\nPlan date: {today.isoformat()}\nStage: configuration\nError type: {type(exc).__name__}",
            )
        except OSError:
            logger.error("Could not write configuration failure report")
        raise
    return await run_daily_pipeline(target_date=target_date, mode=mode, recipient=recipient, profile=profile)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-date", help="Override plan date (YYYY-MM-DD); defaults to today in Asia/Kolkata")
    args = parser.parse_args()
    if args.target_date:
        os.environ["MESSMATE_TARGET_DATE"] = args.target_date
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        result = asyncio.run(_run_from_environment())
    except Exception:
        raise SystemExit(1) from None
    print(f"MESSMATE completed: {result.plan_date.isoformat()} status={result.outcome.status.value} report={result.report_path}")


if __name__ == "__main__":
    main()
