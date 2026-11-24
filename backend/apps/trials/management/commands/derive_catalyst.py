"""Derive the clinical catalyst signal for the universe from the stored studies (Step 14).

    python manage.py derive_catalyst [--date 2026-10-12] [--ticker FDMT --ticker KTTA]

Run it after ``collect_trials`` and ``calculate_runway``. A repeat run for the same day replaces
that day's rows.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.trials.catalyst_service import derive_universe


class Command(BaseCommand):
    help = "Derive the clinical catalyst date, phase and trial counts for each company."

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
        result = derive_universe(day, tickers=options["ticker"])
        sources = ", ".join(f"{n} {s}" for s, n in sorted(result.by_source.items()))
        self.stdout.write(f"{result.companies} companies for {day}: next event {sources}.")
