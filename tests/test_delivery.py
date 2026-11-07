from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import os
import unittest
import uuid
from io import BytesIO
from functools import lru_cache
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError

from messmate_delivery.adapters import MockWhatsAppAdapter, WhatsAppCloudAPIAdapter
from messmate_delivery.errors import (
    LiveSendingDisabled, PermanentChannelRejection, PlanNotDeliverable, RetryableChannelRejection,
    RetryableWhatsAppRejection, UncertainChannelSend, UncertainWhatsAppSend,
)
from messmate_delivery.formatter import render_plan_message
from messmate_delivery.models import DeliveryIdentity, DeliveryStatus
from messmate_delivery.service import DietPlanDeliveryService
from messmate_delivery.store import SQLiteDeliveryStore
from messmate_delivery.telegram_adapter import TelegramBotAPIAdapter
from messmate_nutrition.optimizer import DietOptimizer, PlanStatus, UserNutritionProfile, menu_items_from_snapshot


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests/fixtures/nov6_menu_fixture.json"


@lru_cache(maxsize=1)
def actual_nov6_result():
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return DietOptimizer(profile=UserNutritionProfile(Decimal("170"), Decimal("65"), "Synthetic unit-test profile")).optimize(menu_items_from_snapshot(snapshot))


class DeliveryTests(unittest.TestCase):
    def test_actual_nov6_dry_run_is_default_and_has_complete_concise_message(self):
        store = SQLiteDeliveryStore()
        adapter = MockWhatsAppAdapter()
        # Omitting dry_run explicitly exercises the safe default.
        outcome = DietPlanDeliveryService(store, adapter).deliver("+15551234567", date(2026, 11, 6), actual_nov6_result())
        self.assertTrue(outcome.dry_run)
        self.assertFalse(outcome.sent)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(store.get(outcome.identity).status, DeliveryStatus.PREPARED)
        self.assertIn("06 Nov 2026", outcome.message)
        self.assertIn("Plant protein isolate: 1 scoop (35 g; label: 132 kcal, P 29 g, C 2 g, F 0.48 g)", outcome.message)
        self.assertIn("Nutrition values and household serving weights are estimates.", outcome.message)
        result = actual_nov6_result()
        self.assertIn(f"{result.totals.calories_kcal} kcal", outcome.message)
        if result.totals.protein_g >= Decimal("120"):
            self.assertIn("Protein target met", outcome.message)
        else:
            self.assertIn("Best effort only", outcome.message)
        if result.soya_dry_grams:
            self.assertIn(f"Soya chunks: {result.soya_dry_grams} g dry weight", outcome.message)
        self.assertEqual(outcome.message, render_plan_message(date(2026, 11, 6), result))

    def test_live_requires_explicit_enable_and_mock_acceptance_is_not_delivery(self):
        result = actual_nov6_result()
        store = SQLiteDeliveryStore()
        identity = DeliveryIdentity.for_recipient("+15551234567", date(2026, 11, 6))
        service = DietPlanDeliveryService(store, MockWhatsAppAdapter(), dry_run=False, live_send_enabled=False)
        with self.assertRaises(LiveSendingDisabled):
            service.deliver("+15551234567", date(2026, 11, 6), result)
        store2 = SQLiteDeliveryStore()
        mock = MockWhatsAppAdapter()
        live_controlled = DietPlanDeliveryService(store2, mock, dry_run=False, live_send_enabled=True)
        sent = live_controlled.deliver("+15551234567", date(2026, 11, 6), result)
        self.assertEqual(sent.status, DeliveryStatus.ACCEPTED)
        self.assertNotEqual(sent.status, DeliveryStatus.DELIVERED)
        self.assertEqual(len(mock.calls), 1)
        repeated = live_controlled.deliver("+15551234567", date(2026, 11, 6), result)
        self.assertFalse(repeated.sent)
        self.assertEqual(len(mock.calls), 1)

    def test_invalid_optimization_result_never_reaches_delivery_adapter(self):
        result = replace(actual_nov6_result(), status=PlanStatus.INFEASIBLE)
        store = SQLiteDeliveryStore()
        adapter = MockWhatsAppAdapter()
        service = DietPlanDeliveryService(
            store, adapter, dry_run=False, live_send_enabled=True, channel="whatsapp"
        )
        recipient = "+15551234567"
        plan_date = date(2026, 11, 6)
        with self.assertRaises(PlanNotDeliverable):
            service.deliver(recipient, plan_date, result)
        self.assertEqual(adapter.calls, [])
        identity = DeliveryIdentity.for_recipient(recipient, plan_date, channel="whatsapp")
        self.assertIsNone(store.get(identity))

    def test_atomic_claim_allows_exactly_one_concurrent_sender(self):
        store = SQLiteDeliveryStore()
        identity = DeliveryIdentity.for_recipient("+15551234567", date(2026, 11, 6))
        store.prepare(identity, "payload", "template")
        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(lambda _: store.claim(identity), range(32)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_interrupted_sending_becomes_uncertain_and_is_never_resent(self):
        store = SQLiteDeliveryStore()
        identity = DeliveryIdentity.for_recipient("+15551234567", date(2026, 11, 6))
        store.prepare(identity, "payload", "template")
        lease = store.claim(identity, lease_seconds=1)
        expired = datetime.now(timezone.utc) + timedelta(seconds=5)
        self.assertEqual(store.recover_interrupted(expired), 1)
        self.assertEqual(store.get(identity).status, DeliveryStatus.UNCERTAIN)
        self.assertIsNone(store.claim(identity))

    def test_failed_payload_conflict_does_not_overwrite_prepared_delivery(self):
        store = SQLiteDeliveryStore()
        identity = DeliveryIdentity.for_recipient("+15551234567", date(2026, 11, 6))
        store.prepare(identity, "payload-a", "template")
        with self.assertRaises(ValueError):
            store.prepare(identity, "payload-b", "template")
        self.assertEqual(store.get(identity).status, DeliveryStatus.PREPARED)

    def test_only_explicit_definite_429_rejections_are_retried(self):
        result = actual_nov6_result()

        class Once429:
            def __init__(self): self.calls = 0
            def send(self, *_):
                self.calls += 1
                if self.calls == 1:
                    raise RetryableWhatsAppRejection("http_429", 1)
                return type("Accepted", (), {"provider_message_id": "wamid.ok"})()

        adapter = Once429()
        sleeps = []
        outcome = DietPlanDeliveryService(SQLiteDeliveryStore(), adapter, dry_run=False,
                    live_send_enabled=True, sleep_fn=sleeps.append).deliver(
                        "+15551234567", date(2026, 11, 6), result)
        self.assertEqual(outcome.status, DeliveryStatus.ACCEPTED)
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(sleeps, [1])

    def test_cloud_adapter_marks_ambiguous_http_failure_uncertain_without_leaking_secret(self):
        adapter = WhatsAppCloudAPIAdapter(access_token="top-secret-token", phone_number_id="123",
                                         template_name="messmate", graph_version="v23.0")
        failure = HTTPError("https://graph.facebook.com", 503, "unavailable", {}, None)
        with patch("messmate_delivery.adapters.urlopen", side_effect=failure):
            with self.assertRaises(UncertainWhatsAppSend) as caught:
                adapter.send("+15551234567", "test content", "messmate")
        self.assertNotIn("top-secret-token", str(caught.exception))

    def test_telegram_adapter_sends_plain_text_and_returns_provider_id(self):
        adapter = TelegramBotAPIAdapter(bot_token="12345:synthetic-test-token")

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"ok":true,"result":{"message_id":321}}'

        with patch("messmate_delivery.telegram_adapter.urlopen", return_value=Response()) as request:
            accepted = adapter.send("-1001234567890", "hello Telegram", "ignored-template")
        self.assertEqual(accepted.provider_message_id, "321")
        req = request.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(payload, {"chat_id": "-1001234567890", "text": "hello Telegram"})
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(req.method, "POST")

    def test_telegram_429_is_definite_and_exposes_only_retry_delay(self):
        adapter = TelegramBotAPIAdapter(bot_token="token-for-test")
        error = HTTPError("https://api.telegram.org/bottoken-for-test/sendMessage", 429,
                          "private response", {}, BytesIO(
                              b'{"ok":false,"error_code":429,"parameters":{"retry_after":17}}'))
        with patch("messmate_delivery.telegram_adapter.urlopen", side_effect=error):
            with self.assertRaises(RetryableChannelRejection) as caught:
                adapter.send("123456", "private message", "unused")
        self.assertEqual(caught.exception.retry_after_seconds, 17)
        self.assertNotIn("token-for-test", str(caught.exception))
        self.assertNotIn("123456", str(caught.exception))
        self.assertNotIn("private message", str(caught.exception))
        self.assertNotIn("api.telegram.org", str(caught.exception))
        self.assertNotIn("private response", str(caught.exception))

    def test_telegram_permanent_rejection_is_sanitized(self):
        adapter = TelegramBotAPIAdapter(bot_token="token-for-test")
        error = HTTPError("secret-request-url", 400, "private provider body", {}, BytesIO(
            b'{"ok":false,"error_code":400,"description":"private provider body"}'))
        with patch("messmate_delivery.telegram_adapter.urlopen", side_effect=error):
            with self.assertRaises(PermanentChannelRejection) as caught:
                adapter.send("123456", "private message", "unused")
        for secret in ("secret-request-url", "private provider body", "private message"):
            self.assertNotIn(secret, str(caught.exception))

    def test_telegram_transport_and_server_failures_are_uncertain_and_redacted(self):
        adapter = TelegramBotAPIAdapter(bot_token="token-for-test")
        error = HTTPError("secret-request-url", 503, "private provider body", {}, BytesIO(b"{}"))
        with patch("messmate_delivery.telegram_adapter.urlopen", side_effect=error):
            with self.assertRaises(UncertainChannelSend) as caught:
                adapter.send("123456", "private message", "unused")
        self.assertTrue(caught.exception.uncertain)
        for secret in ("secret-request-url", "private provider body", "private message", "123456"):
            self.assertNotIn(secret, str(caught.exception))

        with patch("messmate_delivery.telegram_adapter.urlopen", side_effect=OSError("raw token-for-test")):
            with self.assertRaises(UncertainChannelSend) as transport:
                adapter.send("123456", "private message", "unused")
        self.assertNotIn("token-for-test", str(transport.exception))

    def test_telegram_unexpected_transport_error_is_uncertain_and_fully_sanitized(self):
        token = "synthetic-token-never-log"
        chat_id = "987654321"
        body = "private diet message"
        request_url = f"https://api.telegram.org/bot{token}/sendMessage"
        adapter = TelegramBotAPIAdapter(bot_token=token)
        # IncompleteRead is an http.client exception, not an OSError, and its
        # text includes response bytes. It must not escape the adapter.
        transport_error = IncompleteRead(f"response includes {token}".encode())
        with self.assertNoLogs("messmate_delivery.telegram_adapter", level="DEBUG"):
            with patch("messmate_delivery.telegram_adapter.urlopen", side_effect=transport_error):
                with self.assertRaises(UncertainChannelSend) as caught:
                    adapter.send(chat_id, body, "unused")
        self.assertTrue(caught.exception.uncertain)
        for sensitive in (token, chat_id, body, request_url, "response includes"):
            self.assertNotIn(sensitive, str(caught.exception))

    def test_telegram_message_length_and_chat_id_validation(self):
        adapter = TelegramBotAPIAdapter(bot_token="synthetic")
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"ok":true,"result":{"message_id":1}}'

        with patch("messmate_delivery.telegram_adapter.urlopen", return_value=Response()) as request:
            adapter.send("123456", "x" * 4096, "unused")
            with self.assertRaises(ValueError):
                adapter.send("123456", "x" * 4097, "unused")
            with self.assertRaises(ValueError):
                adapter.send("bad chat id", "hello", "unused")
        self.assertEqual(request.call_count, 1)

    def test_missing_telegram_token_fails_before_any_request(self):
        with patch("messmate_delivery.telegram_adapter.os.environ.get", return_value=""):
            with patch("messmate_delivery.telegram_adapter.urlopen") as request:
                with self.assertRaisesRegex(ValueError, "TELEGRAM_BOT_TOKEN is required"):
                    TelegramBotAPIAdapter.from_env()
        request.assert_not_called()

    def test_delivery_identity_is_channel_isolated_and_whatsapp_legacy_key_is_preserved(self):
        plan_date = date(2026, 11, 6)
        legacy = DeliveryIdentity.for_recipient("+1 (555) 123-4567", plan_date)
        explicit_whatsapp = DeliveryIdentity.for_recipient("+1 (555) 123-4567", plan_date, channel="whatsapp")
        telegram = DeliveryIdentity.for_recipient("-1001234567890", plan_date, channel="telegram")
        self.assertEqual(legacy, explicit_whatsapp)
        self.assertNotEqual(legacy.delivery_key, telegram.delivery_key)
        self.assertEqual(telegram.message_kind, "telegram:diet_plan")

    def test_telegram_duplicate_protection_and_channel_isolation(self):
        result = actual_nov6_result()

        class CountingAdapter:
            def __init__(self): self.calls = 0
            def send(self, *_):
                self.calls += 1
                return type("Accepted", (), {"provider_message_id": "321"})()

        store = SQLiteDeliveryStore()
        telegram_adapter = CountingAdapter()
        telegram = DietPlanDeliveryService(store, telegram_adapter, dry_run=False,
                                           live_send_enabled=True, channel="telegram")
        first = telegram.deliver("-1001234567890", date(2026, 11, 6), result)
        second = telegram.deliver("-1001234567890", date(2026, 11, 6), result)
        self.assertEqual(first.status, DeliveryStatus.ACCEPTED)
        self.assertFalse(second.sent)
        self.assertEqual(telegram_adapter.calls, 1)

        whatsapp_adapter = MockWhatsAppAdapter()
        whatsapp = DietPlanDeliveryService(store, whatsapp_adapter, dry_run=False,
                                           live_send_enabled=True, channel="whatsapp")
        other = whatsapp.deliver("-1001234567890", date(2026, 11, 6), result)
        self.assertEqual(other.status, DeliveryStatus.ACCEPTED)
        self.assertEqual(len(whatsapp_adapter.calls), 1)

    def test_telegram_retry_after_is_honored_and_uncertain_failure_is_never_resent(self):
        result = actual_nov6_result()

        class OnceLimited:
            def __init__(self): self.calls = 0
            def send(self, *_):
                self.calls += 1
                if self.calls == 1:
                    raise RetryableChannelRejection("telegram_http_429", 47)
                return type("Accepted", (), {"provider_message_id": "654"})()

        rate_limited = OnceLimited()
        sleeps = []
        outcome = DietPlanDeliveryService(SQLiteDeliveryStore(), rate_limited, dry_run=False,
                    live_send_enabled=True, channel="telegram", sleep_fn=sleeps.append).deliver(
                        "123456", date(2026, 11, 6), result)
        self.assertEqual(outcome.status, DeliveryStatus.ACCEPTED)
        self.assertEqual(rate_limited.calls, 2)
        self.assertEqual(sleeps, [47])

        class Ambiguous:
            def __init__(self): self.calls = 0
            def send(self, *_):
                self.calls += 1
                raise UncertainChannelSend("telegram_transport_ambiguous")

        uncertain = Ambiguous()
        store = SQLiteDeliveryStore()
        service = DietPlanDeliveryService(store, uncertain, dry_run=False,
                                          live_send_enabled=True, channel="telegram")
        first = service.deliver("123456", date(2026, 11, 6), result)
        second = service.deliver("123456", date(2026, 11, 6), result)
        self.assertEqual(first.status, DeliveryStatus.UNCERTAIN)
        self.assertFalse(second.sent)
        self.assertEqual(uncertain.calls, 1)


@unittest.skipUnless(os.getenv("MESSMATE_TEST_NEON_DATABASE_URL"), "Neon integration URL not configured")
class NeonConcurrencyIntegrationTests(unittest.TestCase):
    def test_neon_migration_unique_delivery_and_atomic_claim(self):
        from messmate_delivery.neon_store import NeonDeliveryStore
        dsn = os.environ["MESSMATE_TEST_NEON_DATABASE_URL"]
        store_a, store_b = NeonDeliveryStore(dsn), NeonDeliveryStore(dsn)
        store_a.migrate()
        with store_a._psycopg.connect(dsn) as conn:
            constraints = conn.execute("""SELECT contype, pg_get_constraintdef(oid)
                FROM pg_constraint WHERE conrelid='whatsapp_deliveries'::regclass""").fetchall()
            indexes = conn.execute("""SELECT indexdef FROM pg_indexes
                WHERE tablename='whatsapp_deliveries'""").fetchall()
        self.assertTrue(any(kind == "u" and all(key in definition for key in
                            ("recipient_key", "plan_date", "message_kind"))
                            for kind, definition in constraints), "Neon recipient/date/message unique constraint missing")
        allowed_states = ("prepared", "sending", "accepted", "delivered", "failed", "uncertain")
        self.assertTrue(any(kind == "c" and all(state in definition for state in allowed_states)
                            for kind, definition in constraints), "Neon status constraint missing or incomplete")
        self.assertTrue(any("(status, lease_expires_at)" in index for (index,) in indexes),
                        "Neon lease-recovery index missing")
        identity = DeliveryIdentity.for_recipient("+15550001111", date(2026, 11, 6), f"test_{uuid.uuid4().hex}")
        store_a.prepare(identity, "integration-test-payload", "test_template")
        store_b.prepare(identity, "integration-test-payload", "test_template")
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda store: store.claim(identity), (store_a, store_b)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
        self.assertEqual(store_a.recover_interrupted(datetime.now(timezone.utc) + timedelta(minutes=10),
                                                     delivery_key=identity.delivery_key), 1)
        self.assertEqual(store_b.get(identity).status, DeliveryStatus.UNCERTAIN)
        self.assertIsNone(store_b.claim(identity))


if __name__ == "__main__":
    unittest.main()
