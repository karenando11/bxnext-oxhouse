"""Collect ClinicalTrials.gov studies for the company universe (Step 13).

    python manage.py collect_trials [--date 2026-10-12] [--ticker KTTA --ticker APLM]

Each company is searched under its sponsor aliases (derived from its name when it has none). A
study is stored again only when its content changed since the last run.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.trials.collector import collect_trials


class Command(BaseCommand):
    help = "Collect ClinicalTrials.gov studies by sponsor alias."

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
        r = collect_trials(day, tickers=options["ticker"])
        self.stdout.write(
            f"{r.companies} companies, {r.queries} sponsor queries for {day}: "
            f"{r.studies_seen} matched studies, {r.versions_added} new versions, "
            f"{r.unchanged} unchanged, dropped {dict(r.dropped)}, "
            f"{r.shared_skipped} shared with another company."
        )
        for alias, reason in r.failures.items():
            self.stderr.write(f"FAILED: {alias}: {reason}")
