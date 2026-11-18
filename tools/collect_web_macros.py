"""Collect recipe-page nutrition candidates for dishes and store them locally.

Usage:
    python tools/collect_web_macros.py sources.json

``sources.json`` maps each dish name to a list of recipe-page URLs:
    {"Methi Dal": ["https://example.com/recipe", ...]}

Rules enforced here:
  * robots.txt is read for each host and disallowed URLs are skipped, not fetched.
  * Requests are spaced at least 2 seconds apart and identify the client generically.
  * Only sources with a gram serving basis enter averages (see web_candidates).
  * Results go to the git-ignored evidence database and to a JSON file under
    data/research/. Nothing here changes the catalog or the optimizer.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib import error, request, robotparser
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from messmate_nutrition.evidence_db import DEFAULT_DB  # noqa: E402
from messmate_nutrition.web_candidates import evaluate_page, summarize_dish  # noqa: E402

USER_AGENT = "MESSMATE-research/1.0 (recipe nutrition research; no personal data)"
REQUEST_DELAY_SECONDS = 2.0
TIMEOUT_SECONDS = 20

CANDIDATE_TABLE = """
CREATE TABLE IF NOT EXISTS web_candidate (
    dish_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    serving_text TEXT NOT NULL,
    serving_grams REAL,
    calories_kcal_per_100g REAL,
    protein_g_per_100g REAL,
    usable INTEGER NOT NULL,
    reason TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (dish_name, source_url, serving_text)
);
"""


class RobotsCache:
    def __init__(self) -> None:
        self._parsers: dict[str, robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            parser = robotparser.RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                parser.read()
                self._parsers[origin] = parser
            except (error.URLError, OSError):
                self._parsers[origin] = None
        parser = self._parsers[origin]
        return parser is not None and parser.can_fetch(USER_AGENT, url)


def fetch(url: str) -> str:
    req = request.Request(url, headers={"User-Agent": USER_AGENT})
    with request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - http(s) recipe pages only
        return response.read().decode("utf-8", errors="replace")


def collect(sources: dict[str, list[str]]) -> dict[str, object]:
    robots = RobotsCache()
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[tuple] = []
    report: dict[str, object] = {"collected_at": collected_at, "dishes": {}}
    last_request = 0.0
    for dish, urls in sources.items():
        candidates = []
        for url in urls:
            if urlsplit(url).scheme not in {"http", "https"}:
                candidates.append(_record_failure(url, "unsupported URL scheme"))
                continue
            if not robots.allowed(url):
                candidates.append(_record_failure(url, "disallowed by robots.txt; not fetched"))
                continue
            wait = REQUEST_DELAY_SECONDS - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                html = fetch(url)
                values = evaluate_page(url, html)
            except (error.URLError, OSError, ValueError) as exc:
                values = [_record_failure(url, f"fetch failed: {type(exc).__name__}")]
            last_request = time.monotonic()
            candidates.extend(values)
        average = summarize_dish(dish, candidates)
        report["dishes"][dish] = {
            "usable_sources": average.usable_sources,
            "calories_kcal_per_100g_median": str(average.calories_kcal_per_100g_median) if average.calories_kcal_per_100g_median else None,
            "protein_g_per_100g_median": str(average.protein_g_per_100g_median) if average.protein_g_per_100g_median else None,
            "protein_g_per_100g_range": [
                str(average.protein_g_per_100g_min) if average.protein_g_per_100g_min is not None else None,
                str(average.protein_g_per_100g_max) if average.protein_g_per_100g_max is not None else None,
            ],
            "note": average.note,
        }
        for candidate in candidates:
            rows.append((
                dish, candidate.source_url, candidate.serving_text,
                float(candidate.serving_grams) if candidate.serving_grams is not None else None,
                float(candidate.calories_kcal_per_100g) if candidate.calories_kcal_per_100g is not None else None,
                float(candidate.protein_g_per_100g) if candidate.protein_g_per_100g is not None else None,
                1 if candidate.usable else 0, candidate.reason, collected_at,
            ))
    _store(rows)
    return report


def _record_failure(url: str, reason: str):
    from messmate_nutrition.web_candidates import CandidateValue

    return CandidateValue(url, "", None, None, None, False, reason)


def _store(rows: list[tuple]) -> None:
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DEFAULT_DB)
    try:
        with connection:
            connection.execute(CANDIDATE_TABLE)
            connection.executemany(
                "INSERT OR REPLACE INTO web_candidate VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
    finally:
        connection.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    sources = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    report = collect(sources)
    out = DEFAULT_DB.parent / "web_candidates_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nWrote {out} and table web_candidate in {DEFAULT_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
