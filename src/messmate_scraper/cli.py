"""Command-line entry point for a one-shot menu scrape."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import RetryPolicy
from .portal import IST, PortalScraper


def _int_env(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float_env(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape a date from RU Print Portal")
    parser.add_argument("--target-date", help="Menu date in YYYY-MM-DD (default: today in Asia/Kolkata)")
    parser.add_argument("--snapshot-dir", type=Path, default=Path("data/raw_snapshots"))
    parser.add_argument("--attempts", type=int, default=_int_env("MESSMATE_MENU_RETRY_ATTEMPTS", 3))
    parser.add_argument(
        "--retry-initial-seconds",
        type=float,
        default=_float_env("MESSMATE_MENU_RETRY_INITIAL_SECONDS", 30.0),
    )
    parser.add_argument(
        "--retry-multiplier",
        type=float,
        default=_float_env("MESSMATE_MENU_RETRY_MULTIPLIER", 2.0),
    )
    parser.add_argument(
        "--retry-max-seconds",
        type=float,
        default=_float_env("MESSMATE_MENU_RETRY_MAX_SECONDS", 300.0),
    )
    return parser


def _serialise_menu(menu: object) -> dict[str, object]:
    result = asdict(menu)
    result["menu_date"] = menu.menu_date.isoformat()
    result["portal_today"] = menu.portal_today.isoformat()
    result["fetched_at"] = menu.fetched_at.isoformat()
    return result


async def _run(args: argparse.Namespace) -> int:
    target_date = date.fromisoformat(args.target_date) if args.target_date else datetime.now(IST).date()
    policy = RetryPolicy(
        attempts=args.attempts,
        initial_delay_seconds=args.retry_initial_seconds,
        multiplier=args.retry_multiplier,
        max_delay_seconds=args.retry_max_seconds,
    )
    menu = await PortalScraper(args.snapshot_dir, policy).scrape(target_date)
    print(json.dumps(_serialise_menu(menu), ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    args = build_parser().parse_args()
    try:
        exit_code = asyncio.run(_run(args))
    except Exception as exc:
        raise SystemExit(f"menu scrape failed: {exc}") from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
