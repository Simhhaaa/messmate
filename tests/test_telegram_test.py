from decimal import Decimal
import unittest

from messmate_app.telegram_test import (
    TelegramTestNotConfirmed,
    build_test_message,
    send_test,
)
from messmate_delivery.adapters import AdapterAcceptance
from messmate_nutrition.optimizer import UserNutritionProfile

PROFILE = UserNutritionProfile(Decimal("170"), Decimal("70"), "Synthetic Telegram-test profile")


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, recipient: str, message: str, template_name: str) -> AdapterAcceptance:
        self.calls.append((recipient, message, template_name))
        return AdapterAcceptance("101")


class ControlledTelegramTestTests(unittest.TestCase):
    def test_message_is_labelled_validated_and_within_telegram_limit(self) -> None:
        message = build_test_message(PROFILE)
        self.assertTrue(message.startswith("MESSMATE controlled Telegram test"))
        self.assertIn("not today's plan", message)
        self.assertIn("06 Nov 2026", message)
        self.assertIn("Protein target met", message)
        self.assertLessEqual(len(message), 4096)

    def test_refuses_to_send_without_explicit_confirmation(self) -> None:
        adapter = FakeAdapter()
        for env in ({}, {"MESSMATE_TELEGRAM_TEST": "true"}, {"MESSMATE_TELEGRAM_TEST": "CONFIRM"}):
            with self.assertRaises(TelegramTestNotConfirmed):
                send_test(adapter=adapter, recipient="12345", profile=PROFILE, environ=env)
        self.assertEqual(adapter.calls, [])

    def test_requires_a_recipient(self) -> None:
        adapter = FakeAdapter()
        with self.assertRaises(ValueError):
            send_test(adapter=adapter, recipient="  ", profile=PROFILE,
                      environ={"MESSMATE_TELEGRAM_TEST": "confirm"})
        self.assertEqual(adapter.calls, [])

    def test_confirmed_test_sends_exactly_one_message(self) -> None:
        adapter = FakeAdapter()
        message_id = send_test(adapter=adapter, recipient="12345", profile=PROFILE,
                               environ={"MESSMATE_TELEGRAM_TEST": "confirm"})
        self.assertEqual(message_id, "101")
        self.assertEqual(len(adapter.calls), 1)
        recipient, message, template = adapter.calls[0]
        self.assertEqual(recipient, "12345")
        self.assertEqual(template, "telegram.sendMessage")
        self.assertTrue(message.startswith("MESSMATE controlled Telegram test"))


if __name__ == "__main__":
    unittest.main()
