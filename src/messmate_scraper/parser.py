"""Conservative parsing of the portal's visible accessibility text."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from .models import MEAL_NAMES, MealName, MealSection, MenuNotPublishedError, PortalStructureError

_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_WEEKDAY_ALIASES = {
    "MON": "MON", "MONDAY": "MON",
    "TUE": "TUE", "TUESDAY": "TUE",
    "WED": "WED", "WEDNESDAY": "WED",
    "THU": "THU", "THURSDAY": "THU",
    "FRI": "FRI", "FRIDAY": "FRI",
    "SAT": "SAT", "SATURDAY": "SAT",
    "SUN": "SUN", "SUNDAY": "SUN",
}
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_TODAY_RE = re.compile(
    r"\bTODAY\s+(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|"
    r"MON|TUE|WED|THU|FRI|SAT|SUN)\s*,?\s*(\d{1,2})\s+"
    r"([A-Z]{3,9})\b",
    re.IGNORECASE,
)
_SELECTED_DAY_RE = re.compile(
    r"\b(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\s+MESS\s+MENU\b",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}", re.IGNORECASE)
_FEEDBACK_RE = re.compile(r"^give feedback$", re.IGNORECASE)
_CAMPUS_NOTICE_RE = re.compile(
    r"\s+As the strength of the learners on the campus is significantly reduced\b.*$",
    re.IGNORECASE,
)


def _normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return value.replace("\u00a0", " ").replace("\r", "\n")


def parse_portal_today(body_text: str, reference_date: date) -> date:
    """Parse the portal's ``TODAY THU, 05 NOV`` label and infer its year."""

    match = _TODAY_RE.search(_normalise_text(body_text))
    if not match:
        raise PortalStructureError("Could not find the portal's TODAY weekday/date label")

    day = int(match.group(2))
    month_text = match.group(3).upper()
    month = next(
        (index + 1 for index, abbreviation in enumerate(_MONTHS) if month_text.startswith(abbreviation)),
        None,
    )
    if month is None:
        raise PortalStructureError(f"Unrecognized portal month: {month_text}")
    candidates: list[date] = []
    for year in (reference_date.year - 1, reference_date.year, reference_date.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise PortalStructureError(f"Invalid portal date: {day} {month_text}")

    parsed = min(candidates, key=lambda candidate: abs((candidate - reference_date).days))
    expected_weekday = _WEEKDAYS[parsed.weekday()]
    if _WEEKDAY_ALIASES[match.group(1).upper()] != expected_weekday:
        raise PortalStructureError(
            f"Portal weekday {match.group(1)} does not match displayed date {parsed.isoformat()}"
        )
    return parsed


def selected_weekday(body_text: str) -> str | None:
    """Return the selected weekday heading, if the portal shows one."""

    match = _SELECTED_DAY_RE.search(_normalise_text(body_text))
    return match.group(1).upper() if match else None


def extract_meal_items(body_text: str, meal: MealName) -> MealSection:
    """Extract one expanded section without guessing unmarked separators."""

    lines = [line.strip() for line in _normalise_text(body_text).splitlines()]
    start_index = next(
        (index for index, line in enumerate(lines) if line.casefold() == meal.casefold()),
        None,
    )
    if start_index is None:
        raise PortalStructureError(f"Meal heading not found: {meal}")

    end_index = len(lines)
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        if any(line.casefold() == name.casefold() for name in MEAL_NAMES if name != meal):
            end_index = index
            break
        if line.casefold().startswith("made with"):
            end_index = index
            break

    content_lines: list[str] = []
    for line in lines[start_index + 1 : end_index]:
        if not line or _TIME_RE.match(line) or _FEEDBACK_RE.match(line):
            continue
        if line.casefold() in {"important message", "go to today"}:
            continue
        if line.startswith("TODAY "):
            continue
        if _SELECTED_DAY_RE.search(line):
            continue
        content_lines.append(line)

    raw_text = " ".join(content_lines).strip()
    if not raw_text:
        raise MenuNotPublishedError(f"No visible items found for {meal}")

    # These are the separators observed in the live portal (featured-item star
    # and middle-dot separators). Do not split on commas, slashes, or ampersands.
    if any(separator in raw_text for separator in ("★", "·", "•")):
        candidates = re.split(r"\s*[★·•]\s*", raw_text)
    else:
        # When markup renders each entry on a separate line without a marker,
        # preserve those lines as entries. A lone line remains one exact item.
        candidates = [line.strip() for line in content_lines]

    # The portal may append its temporary campus notice directly to the last
    # menu item in accessible text. Remove only this known notice prefix so
    # that an otherwise exact catalog item (for example, Chapati) survives.
    items = tuple(
        cleaned.strip(" \t\n-•·★")
        for item in candidates
        if (cleaned := _CAMPUS_NOTICE_RE.split(item, maxsplit=1)[0].strip(" \t\n-•·★"))
    )
    if not items:
        raise MenuNotPublishedError(f"No parseable menu items found for {meal}")
    return MealSection(name=meal, items=items, raw_text=raw_text)


def validate_target_date(target_date: date, portal_today: date, max_days_ahead: int = 6) -> None:
    """Allow only the portal's current menu day or a weekday in the next 6 days."""

    offset = (target_date - portal_today).days
    if offset < 0:
        raise PortalStructureError(
            f"Requested menu date {target_date} is before portal date {portal_today}"
        )
    if offset > max_days_ahead:
        raise PortalStructureError(
            f"Requested menu date {target_date} is {offset} days ahead; portal exposes weekday tabs only"
        )


def weekday_button_label(value: date) -> str:
    return ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[value.weekday()]
