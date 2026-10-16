"""Materialise the default screen for the latest completed score run of a day.

python manage.py screen_day 2026-10-12
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.scoring.service import latest_complete_run
from apps.screener.service import materialise_default_screen


class Command(BaseCommand):
    help = "Run and store the default screen for a scored day, and write its funnel events."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("day", help="Market date, YYYY-MM-DD.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            day = date.fromisoformat(options["day"])
        except ValueError as exc:
            raise CommandError(f"Not a date: {options['day']}") from exc
        score_run = latest_complete_run(day)
        if score_run is None:
            raise CommandError(f"No completed score run for {day}")
        run = materialise_default_screen(score_run)
        self.stdout.write(
            f"Default screen for {day}: {run.passed_n} of {run.universe_n} companies passed."
        )
