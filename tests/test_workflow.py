from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo
import json
import re
import unittest

from messmate_app.config import RunConfigurationError, resolve_run_mode, resolve_target_date
from messmate_app.daily import run_daily_pipeline
from messmate_delivery.adapters import MockWhatsAppAdapter
from messmate_delivery.models import DeliveryStatus
from messmate_delivery.store import SQLiteDeliveryStore
from messmate_nutrition.optimizer import UserNutritionProfile
from decimal import Decimal
from messmate_scraper.models import DailyMenu, MealSection


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/daily-messmate.yml"
SNAPSHOT = ROOT / "tests/fixtures/nov6_menu_fixture.json"


class RunModeTests(unittest.TestCase):
    def test_defaults_to_dry_run(self):
        self.assertEqual(resolve_run_mode({}), type(resolve_run_mode({}))(True, False))

    def test_live_secret_does_not_override_manual_dry_run(self):
        mode = resolve_run_mode({"MESSMATE_DRY_RUN": "true", "MESSMATE_LIVE_SEND": "true"})
        self.assertTrue(mode.dry_run)
        self.assertFalse(mode.live_send)

    def test_live_requires_both_explicit_gates(self):
        with self.assertRaises(RunConfigurationError):
            resolve_run_mode({"MESSMATE_DRY_RUN": "false", "MESSMATE_LIVE_SEND": "false"})
        mode = resolve_run_mode({"MESSMATE_DRY_RUN": "false", "MESSMATE_LIVE_SEND": "true"})
        self.assertFalse(mode.dry_run)
        self.assertTrue(mode.live_send)

    def test_invalid_bool_and_date_values_fail_closed(self):
        with self.assertRaises(RunConfigurationError):
            resolve_run_mode({"MESSMATE_DRY_RUN": "sometimes"})
        with self.assertRaises(RunConfigurationError):
            resolve_target_date("11/06/2026", today=date(2026, 11, 6))


class WorkflowConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_ist_schedule_and_manual_dispatch_are_configured(self):
        self.assertRegex(self.text, r'cron:\s*["\']30 18 \* \* \*["\']')
        self.assertRegex(self.text, r"(?m)^  workflow_dispatch:")
        self.assertRegex(self.text, r"(?m)^        default: true$")
        self.assertIn("inputs:", self.text)

    def test_secrets_are_referenced_but_no_values_are_committed(self):
        for secret in ("NEON_DATABASE_URL", "WHATSAPP_RECIPIENT", "WHATSAPP_ACCESS_TOKEN",
                       "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TEMPLATE_NAME"):
            self.assertIn(f"secrets.{secret}", self.text)
        self.assertIn("secrets.MESSMATE_LIVE_SEND", self.text)
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read$")

    def test_duplicate_execution_and_artifact_scope_are_safe(self):
        self.assertIn("concurrency:", self.text)
        self.assertIn("cancel-in-progress: false", self.text)
        self.assertIn("path: reports/*.txt", self.text)
        self.assertNotIn("path: data/raw_snapshots", self.text)
        self.assertIn("retention-days: 1", self.text)
        for name in ("MESSMATE_USER_HEIGHT_CM", "MESSMATE_USER_WEIGHT_KG", "MESSMATE_USER_GOAL"):
            self.assertIn(f"secrets.{name}", self.text)

    def test_actions_are_full_sha_pinned(self):
        uses = re.findall(r"(?m)^\s+uses: ([^\s]+)", self.text)
        self.assertEqual(len(uses), 3)
        for action in uses:
            self.assertRegex(action, r"@[0-9a-f]{40}$")


class DailyPipelineSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_menu_runs_complete_dry_pipeline_without_adapter_send(self):
        source = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        plan_date = date.fromisoformat(source["requested_date"])
        meals = tuple(MealSection(name=name, items=tuple(section["items"]), raw_text="")
                      for name, section in source["meals"].items())
        menu = DailyMenu(plan_date, date.fromisoformat(source["portal_today"]),
                         datetime.fromisoformat(source["fetched_at"]), source["source_url"], meals,
                         str(SNAPSHOT))

        class FakeScraper:
            async def scrape(self, target):
                self.target = target
                return menu

        store = SQLiteDeliveryStore()
        adapter = MockWhatsAppAdapter()
        with TemporaryDirectory() as temporary:
            run = await run_daily_pipeline(
                target_date=plan_date,
                mode=resolve_run_mode({}),
                recipient="+15551234567",
                report_dir=Path(temporary) / "reports",
                scraper=FakeScraper(),
                store=store,
                adapter=adapter,
                profile=UserNutritionProfile(Decimal("170"), Decimal("65"), "Synthetic unit-test profile"),
            )
            self.assertTrue(run.outcome.dry_run)
            self.assertEqual(run.outcome.status, DeliveryStatus.PREPARED)
            self.assertEqual(adapter.calls, [])
            report = run.report_path.read_text(encoding="utf-8")
            self.assertIn("Delivery status: prepared", report)
            self.assertIn("omitted for privacy", report)
            self.assertNotIn("Best effort only", report)
            self.assertNotIn("+15551234567", report)
            self.assertNotIn("Protein:", report)
            self.assertNotIn("Chole", report)
            self.assertNotIn("kcal", report)

    async def test_invalid_live_mode_stops_before_scraping_or_sending(self):
        class NeverScrape:
            async def scrape(self, _target):
                raise AssertionError("scraper should not be called")

        with TemporaryDirectory() as temporary:
            with self.assertRaises(RunConfigurationError):
                await run_daily_pipeline(
                    target_date=date(2026, 11, 6),
                    mode=type(resolve_run_mode({}))(False, False),
                    recipient="+15551234567",
                    report_dir=Path(temporary),
                    scraper=NeverScrape(),
                    store=SQLiteDeliveryStore(),
                )

    async def test_failure_report_omits_exception_message(self):
        class BrokenScraper:
            async def scrape(self, _target):
                raise RuntimeError("private diagnostic marker")

        with TemporaryDirectory() as temporary:
            report = Path(temporary) / "messmate-diet-2026-11-06.txt"
            with self.assertRaises(RuntimeError):
                await run_daily_pipeline(
                    target_date=date(2026, 11, 6),
                    mode=resolve_run_mode({}),
                    recipient="+15551234567",
                    report_dir=Path(temporary),
                    scraper=BrokenScraper(),
                    store=SQLiteDeliveryStore(),
                )
            contents = report.read_text(encoding="utf-8")
            self.assertIn("Error type: RuntimeError", contents)
            self.assertNotIn("private diagnostic marker", contents)


if __name__ == "__main__":
    unittest.main()
