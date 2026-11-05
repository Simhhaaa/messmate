from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import os
import unittest
import uuid
from unittest.mock import patch
from urllib.error import HTTPError

from messmate_delivery.adapters import MockWhatsAppAdapter, WhatsAppCloudAPIAdapter
from messmate_delivery.errors import LiveSendingDisabled, RetryableWhatsAppRejection, UncertainWhatsAppSend
from messmate_delivery.formatter import render_plan_message
from messmate_delivery.models import DeliveryIdentity, DeliveryStatus
from messmate_delivery.service import DietPlanDeliveryService
from messmate_delivery.store import SQLiteDeliveryStore
from messmate_nutrition.optimizer import DietOptimizer, UserNutritionProfile, menu_items_from_snapshot


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests/fixtures/nov6_menu_fixture.json"


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
        self.assertIn("Best effort only — protein shortfall: 4.97 g (115.03/120 g target)", outcome.message)
        self.assertIn("2 medium chapatis (~80 g; cooked weight)", outcome.message)
        self.assertIn("1 medium bowl (~180 g; cooked weight)", outcome.message)
        self.assertIn("Soya chunks: 70 g dry weight", outcome.message)
        self.assertIn("Plant protein isolate: 1 scoop (35 g; label: 132 kcal, P 29 g, C 2 g, F 0.48 g)", outcome.message)
        self.assertIn("Nutrition values and household serving weights are estimates.", outcome.message)
        self.assertIn("1971.2 kcal", outcome.message)
        self.assertEqual(outcome.message, render_plan_message(date(2026, 11, 6), actual_nov6_result()))
        self.assertEqual(actual_nov6_result().totals.calories_kcal, Decimal("1971.2"))
        self.assertEqual(actual_nov6_result().totals.protein_g, Decimal("115.03"))

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
