"""Send one controlled Telegram delivery test; never runs on a schedule.

The test message is built from the saved 6 Nov 2026 menu fixture, the one menu
known to produce a validated plan, and is labelled so it cannot be mistaken for
today's plan. It verifies the real bot token, chat ID, message formatting and
Telegram's length limit. It writes nothing to the delivery database.

Sending requires ``MESSMATE_TELEGRAM_TEST=confirm``; the workflow sets that only
for a manual dispatch with ``telegram_test`` selected.
"""

from __future__ import annotations

from datetime import date
import json
import logging
import os
from pathlib import Path
from typing import Any

from messmate_delivery.formatter import render_plan_message
from messmate_delivery.telegram_adapter import TelegramBotAPIAdapter
from messmate_nutrition.optimizer import (
    DietOptimizer,
    UserNutritionProfile,
    menu_items_from_snapshot,
    validate_optimization_result,
)
from messmate_nutrition.profile_config import load_user_profile

logger = logging.getLogger("messmate.telegram_test")

SAMPLE_DATE = date(2026, 11, 6)
SAMPLE_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "nov6_menu_fixture.json"
CONFIRM_VALUE = "confirm"
REPORT_PATH = Path("reports") / "messmate-telegram-test.txt"


class TelegramTestNotConfirmed(RuntimeError):
    pass


def build_test_message(profile: UserNutritionProfile) -> str:
    """Render the labelled sample plan; refuse if it does not validate."""

    snapshot = json.loads(SAMPLE_FIXTURE.read_text(encoding="utf-8"))
    result = DietOptimizer(profile=profile).optimize(menu_items_from_snapshot(snapshot))
    errors = validate_optimization_result(result)
    if errors:
        raise ValueError("sample plan failed validation; not sending")
    header = (
        "MESSMATE controlled Telegram test\n"
        "This is a one-off delivery test, not today's plan.\n"
        f"Sample plan below uses the saved {SAMPLE_DATE.strftime('%d %b %Y')} menu.\n"
    )
    return header + "\n" + render_plan_message(SAMPLE_DATE, result)


def send_test(*, adapter: Any, recipient: str, profile: UserNutritionProfile,
              environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    if env.get("MESSMATE_TELEGRAM_TEST", "") != CONFIRM_VALUE:
        raise TelegramTestNotConfirmed("MESSMATE_TELEGRAM_TEST must equal 'confirm'")
    if not recipient.strip():
        raise ValueError("TELEGRAM_CHAT_ID is required")
    message = build_test_message(profile)
    acceptance = adapter.send(recipient, message, "telegram.sendMessage")
    return acceptance.provider_message_id


def _write_report(text: str) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(text.rstrip() + "\n", encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        send_test(
            adapter=TelegramBotAPIAdapter.from_env(),
            recipient=os.environ.get("TELEGRAM_CHAT_ID", ""),
            profile=load_user_profile(),
        )
    except Exception as exc:
        # Exception text may contain provider or configuration details; log the type only.
        logger.error("Telegram test failed error_type=%s", type(exc).__name__)
        _write_report(f"MESSMATE Telegram test failed\nError type: {type(exc).__name__}")
        raise SystemExit(1) from None
    logger.info("Telegram test accepted by provider")
    _write_report("MESSMATE Telegram test accepted by Telegram\nMessage content is omitted for privacy.")
    print("MESSMATE Telegram test accepted")


if __name__ == "__main__":
    main()
