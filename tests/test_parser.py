from datetime import date
import unittest

from messmate_scraper.models import MenuNotPublishedError, PortalStructureError
from messmate_scraper.parser import (
    extract_meal_items,
    parse_portal_today,
    selected_weekday,
    validate_target_date,
    weekday_button_label,
)


FRIDAY_BODY = """\
FRIDAY Mess menu
Go to Today
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


class ParserTests(unittest.TestCase):
    def test_extracts_all_four_meals_and_preserves_item_text(self) -> None:
        expected = {
            "Breakfast": (
                "Banana", "Masala Oats", "Muesli", "Sada Paratha", "Chana Palak Ki Sabzi",
                "Hot Milk (D)", "Cold Milk (D)", "Tea (D)", "Coffee Powder", "Bread/Butter/Jam",
            ),
            "Lunch": (
                "Arhar Dal Home Style", "Paneer Do-Pyaza", "Ullipai Pacchadi", "Curd",
                "Green Salad", "Jeera Rice", "Chapati",
            ),
            "Snacks": ("Veg Puff", "Hot Coffee", "Ketchup", "Tea (D)", "Coffee Powder"),
            "Dinner": ("Chole Masala", "Jeera Aloo", "Green Salad", "Rasam", "Jeera Rice", "Chapati"),
        }
        for meal, items in expected.items():
            with self.subTest(meal=meal):
                self.assertEqual(extract_meal_items(FRIDAY_BODY, meal).items, items)

    def test_parses_today_date_and_validates_weekday(self) -> None:
        self.assertEqual(parse_portal_today("TODAY THU, 05 NOV", date(2026, 11, 5)), date(2026, 11, 5))
        self.assertEqual(parse_portal_today("TODAY THURSDAY, 05 NOVEMBER", date(2026, 11, 5)), date(2026, 11, 5))
        with self.assertRaises(PortalStructureError):
            parse_portal_today("TODAY FRI, 05 NOV", date(2026, 11, 5))

    def test_infers_year_across_new_year(self) -> None:
        self.assertEqual(parse_portal_today("TODAY FRI, 01 JAN", date(2027, 1, 1)), date(2027, 1, 1))

    def test_selected_weekday_is_read_from_portal_heading(self) -> None:
        self.assertEqual(selected_weekday(FRIDAY_BODY), "FRIDAY")
        self.assertEqual(selected_weekday("FRIDAY\nMess menu"), "FRIDAY")
        self.assertIsNone(selected_weekday("TODAY THU, 05 NOV"))

    def test_rejects_dates_outside_portal_week(self) -> None:
        validate_target_date(date(2026, 11, 6), date(2026, 11, 5))
        with self.assertRaises(PortalStructureError):
            validate_target_date(date(2026, 11, 4), date(2026, 11, 5))
        with self.assertRaises(PortalStructureError):
            validate_target_date(date(2026, 11, 12), date(2026, 11, 5))

    def test_weekday_button_mapping_covers_all_seven_labels(self) -> None:
        monday = date(2026, 11, 2)
        self.assertEqual(
            [weekday_button_label(monday.fromordinal(monday.toordinal() + offset)) for offset in range(7)],
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        )

    def test_trailing_campus_notice_does_not_corrupt_last_menu_item(self) -> None:
        body = """Dinner
07:30 - 09:30 PM
★ Chole Masala ★ Chapati As the strength of the learners on the campus is significantly reduced during the Diwali break, meals will be served in the A Block Dining Hall only from tomorrow till 13th November 2026.
Give Feedback
Made with love"""
        section = extract_meal_items(body, "Dinner")
        self.assertEqual(section.items, ("Chole Masala", "Chapati"))

    def test_missing_or_empty_section_fails_closed(self) -> None:
        with self.assertRaises(PortalStructureError):
            extract_meal_items("TODAY THU, 05 NOV\nLunch\nitems", "Breakfast")
        with self.assertRaises(MenuNotPublishedError):
            extract_meal_items("TODAY THU, 05 NOV\nBreakfast\n07:30 - 09:30 AM\n", "Breakfast")

    def test_unmarked_lines_are_preserved_without_guessing_delimiters(self) -> None:
        section = extract_meal_items("Breakfast\n08:00 - 09:00 AM\nPlain Oats\nBanana", "Breakfast")
        self.assertEqual(section.items, ("Plain Oats", "Banana"))


if __name__ == "__main__":
    unittest.main()
