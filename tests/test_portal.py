import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from messmate_scraper.models import PortalStructureError
from messmate_scraper.portal import PortalScraper


TODAY_TEXT = """\
TODAY THU, 05 NOV
Mon
Tue
Wed
Thu
Fri
Sat
Sun
Breakfast
07:30 - 09:30 AM
★ Banana ★ Masala Oats ★ Muesli ★ Sada Paratha ★ Chana Palak Ki Sabzi · Hot Milk (D) · Cold Milk (D) · Tea (D) · Coffee Powder · Bread/Butter/Jam
Give Feedback
Lunch
12:00 - 02:30 PM
★ Arhar Dal Home Style ★ Paneer Do-Pyaza ★ Ullipai Pacchadi · Curd · Green Salad · Jeera Rice · Chapati
Give Feedback
Snacks
04:30 - 06:30 PM
★ Veg Puff ★ Hot Coffee · Ketchup · Tea (D) · Coffee Powder
Give Feedback
Dinner
07:30 - 09:30 PM
★ Chole Masala ★ Jeera Aloo · Green Salad · Rasam · Jeera Rice · Chapati
Give Feedback
Made with love
"""

FRIDAY_TEXT = TODAY_TEXT.replace("TODAY THU, 05 NOV", "FRIDAY Mess menu")


class FakeLocator:
    def __init__(self, page, role, name):
        self.page = page
        self.role = role
        self.name = name

    async def wait_for(self, state="visible", timeout=None):
        if self.role == "heading" and self.name not in {"Breakfast", "Lunch", "Snacks", "Dinner"}:
            raise AssertionError("unexpected heading wait")

    async def count(self):
        if self.role == "button" and self.name == "OK":
            return 1 if self.page.overlay else 0
        if self.role == "button" and self.name in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}:
            return 1
        if self.role == "heading" and self.name in {"Breakfast", "Lunch", "Snacks", "Dinner"}:
            return 1
        return 0

    async def click(self):
        if self.role == "button" and self.name == "OK":
            self.page.overlay = False
            self.page.text = self.page.text.replace(self.page.overlay_text, "", 1)
            return
        if self.role == "button":
            if self.page.overlay:
                raise AssertionError("overlay must be dismissed before clicking a weekday")
            self.page.text = FRIDAY_TEXT.replace("FRIDAY Mess menu", self.page.day_heading)


class FakeBodyLocator:
    def __init__(self, page):
        self.page = page

    async def inner_text(self):
        return self.page.text


class FakePage:
    def __init__(self):
        self.overlay_text = (
            "We Listen to You 👂\nGive your feedback\n"
            "(Feedback opens during meal timings)\nOK\n"
        )
        self.text = self.overlay_text + TODAY_TEXT
        self.day_heading = "FRIDAY Mess menu"
        self.overlay = True

    def get_by_role(self, role, name, exact=True):
        return FakeLocator(self, role, name)

    def locator(self, selector):
        if selector != "body":
            raise AssertionError(f"unexpected selector: {selector}")
        return FakeBodyLocator(self)

    async def content(self):
        return f"<html><body>{self.text}</body></html>"


class PortalScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_weekday_heading_extracts_all_meals_and_writes_raw_snapshot(self):
        fixed_time = datetime(2026, 11, 5, 0, 7, tzinfo=ZoneInfo("Asia/Kolkata"))
        with tempfile.TemporaryDirectory() as temporary_dir:
            scraper = PortalScraper(
                snapshot_dir=Path(temporary_dir),
                clock=lambda: fixed_time,
            )
            menu = await scraper._scrape_loaded_page(FakePage(), date(2026, 11, 6), attempt=1)

            self.assertEqual(menu.menu_date, date(2026, 11, 6))
            self.assertEqual(menu.portal_today, date(2026, 11, 5))
            self.assertEqual([section.name for section in menu.meals], ["Breakfast", "Lunch", "Snacks", "Dinner"])
            self.assertEqual(menu.meals[0].items[0], "Banana")
            self.assertEqual(menu.meals[1].items[1], "Paneer Do-Pyaza")
            self.assertEqual(menu.meals[2].items[0], "Veg Puff")
            self.assertEqual(menu.meals[3].items[0], "Chole Masala")

            snapshot = json.loads(Path(menu.snapshot_path).read_text(encoding="utf-8"))
            self.assertEqual(snapshot["status"], "complete")
            self.assertIn("We Listen to You", snapshot["initial_page"]["visible_text"])
            self.assertEqual(set(snapshot["meals"]), {"Breakfast", "Lunch", "Snacks", "Dinner"})
            self.assertTrue(all(value["html"].startswith("<html>") for value in snapshot["meals"].values()))
            self.assertTrue(Path(menu.snapshot_path).with_name("attempt-01-breakfast.html").exists())
            self.assertTrue(Path(menu.snapshot_path).with_name("attempt-01-breakfast.txt").exists())

    async def test_rejects_a_selected_weekday_mismatch(self):
        fixed_time = datetime(2026, 11, 5, 0, 7, tzinfo=ZoneInfo("Asia/Kolkata"))
        wrong_day_page = FakePage()
        wrong_day_page.day_heading = "THURSDAY Mess menu"
        with tempfile.TemporaryDirectory() as temporary_dir:
            scraper = PortalScraper(snapshot_dir=Path(temporary_dir), clock=lambda: fixed_time)
            with self.assertRaises(PortalStructureError):
                await scraper._scrape_loaded_page(wrong_day_page, date(2026, 11, 6), attempt=1)


if __name__ == "__main__":
    unittest.main()
