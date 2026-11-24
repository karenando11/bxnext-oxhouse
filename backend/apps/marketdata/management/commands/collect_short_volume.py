"""Run the FINRA short-volume step: download missing daily files and load the universe's rows.

    python manage.py collect_short_volume [--date 2026-10-12] [--day 2026-10-09 --day 2026-10-08]

``--date`` is the market date of the run (default: today, US Eastern); the files fetched are the
ones before it. ``--day`` fetches exactly that trading date again (a reload or a repair).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.marketdata.finra.ingest import collect_short_volume


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise CommandError(f"Not a date: {text}") from exc


class Command(BaseCommand):
    help = "Download FINRA Reg SHO short-volume files and load rows for the company universe."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--date", help="Market date, YYYY-MM-DD (default: today, US Eastern).")
        parser.add_argument("--day", action="append", help="Fetch exactly this date (repeatable).")

    def handle(self, *args: Any, **options: Any) -> None:
        as_of = (
            _day(options["date"])
            if options["date"]
            else datetime.now(ZoneInfo("America/New_York")).date()
        )
        days = [_day(d) for d in options["day"]] if options["day"] else None
        result = collect_short_volume(as_of, dates=days)
        self.stdout.write(
            f"Run for {as_of}: {len(result.of('loaded'))} files loaded "
            f"({result.rows_loaded} rows), {len(result.of('unchanged'))} unchanged, "
            f"{len(result.of('no_file_for_date'))} with no file, {len(result.failed)} failed."
        )
        for day, reason in sorted(result.failed.items()):
            self.stderr.write(f"FAILED: {day}: {reason}")
