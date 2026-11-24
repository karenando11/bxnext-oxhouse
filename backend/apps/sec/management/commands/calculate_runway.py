"""Calculate cash runway for the universe from the stored SEC facts (Step 12).

    python manage.py calculate_runway [--date 2026-10-12] [--ticker KTTA --ticker PACS]

Run it after ``collect_sec``. A repeat run for the same day replaces that day's rows.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.sec.runway_service import calculate_universe


class Command(BaseCommand):
    help = "Calculate runway (cash, burn, trial burn) for the company universe."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--date", help="Market date, YYYY-MM-DD (default: today, US Eastern).")
        parser.add_argument("--ticker", action="append", help="Only this ticker (repeatable).")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            day = (
                date.fromisoformat(options["date"])
                if options["date"]
                else datetime.now(ZoneInfo("America/New_York")).date()
            )
        except ValueError as exc:
            raise CommandError(f"Not a date: {options['date']}") from exc
        result = calculate_universe(day, tickers=options["ticker"])
        counts = ", ".join(f"{n} {s}" for s, n in sorted(result.by_status.items()))
        self.stdout.write(
            f"{result.companies} companies for {day}: {counts}; "
            f"{result.runway_months_present} with a runway."
        )
