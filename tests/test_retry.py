from datetime import date, datetime, timezone
import unittest

from messmate_scraper.models import DailyMenu, MealSection, MenuNotPublishedError, RetryPolicy, ScrapeRetryExhausted
from messmate_scraper.portal import retry_async


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_waits_with_configured_backoff_then_returns(self) -> None:
        calls = []
        delays = []
        expected_menu = DailyMenu(
            menu_date=date(2026, 11, 6),
            portal_today=date(2026, 11, 5),
            fetched_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
            source_url="https://ru-print.vercel.app/",
            meals=(MealSection("Breakfast", ("Banana",), "★ Banana"),),
            snapshot_path="snapshot.json",
        )

        async def operation(attempt: int) -> DailyMenu:
            calls.append(attempt)
            if attempt < 3:
                raise MenuNotPublishedError("not published yet")
            return expected_menu

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        result = await retry_async(
            operation,
            RetryPolicy(attempts=3, initial_delay_seconds=2, multiplier=3, max_delay_seconds=5),
            sleep=fake_sleep,
        )
        self.assertEqual(result, expected_menu)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(delays, [2, 5])

    async def test_exhaustion_reports_last_error(self) -> None:
        async def operation(attempt: int) -> DailyMenu:
            raise MenuNotPublishedError(f"attempt {attempt}")

        async def no_wait(_: float) -> None:
            return None

        with self.assertRaises(ScrapeRetryExhausted) as raised:
            await retry_async(operation, RetryPolicy(attempts=2), sleep=no_wait)
        self.assertEqual(raised.exception.attempts, 2)
        self.assertIn("attempt 2", str(raised.exception.last_error))

    def test_retry_policy_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            RetryPolicy(attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(multiplier=0.5)


if __name__ == "__main__":
    unittest.main()
