"""Run the Finviz step for a day: download (or read a saved file), store, load and check.

    python manage.py ingest_finviz [--date 2026-10-12] [--file raw_export.csv]

The token comes from the FINVIZ_AUTH_TOKEN environment variable and is never printed.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.marketdata.finviz.ingest import FinvizIngestFailed, ingest_finviz


class Command(BaseCommand):
    help = "Download the Finviz export (or load a saved file) and load the daily snapshot."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--date", help="Market date, YYYY-MM-DD (default: today, US Eastern).")
        parser.add_argument("--file", type=Path, help="Load this saved raw export instead.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            day = (
                date.fromisoformat(options["date"])
                if options["date"]
                else datetime.now(ZoneInfo("America/New_York")).date()
            )
        except ValueError as exc:
            raise CommandError(f"Not a date: {options['date']}") from exc
        path: Path | None = options["file"]
        content = None
        if path is not None:
            if not path.is_file():
                raise CommandError(f"File not found: {path}")
            content = path.read_bytes()
        try:
            result = ingest_finviz(day, content=content, source_name=path.name if path else "")
        except FinvizIngestFailed as exc:
            raise CommandError(f"Stage stopped ({exc.reason_code}): {exc}") from exc
        if result.unchanged:
            self.stdout.write(f"The same file was already loaded for {day}; nothing changed.")
            return
        self.stdout.write(f"Loaded {result.rows} companies for {day} ({result.new_companies} new).")
        for check in result.failed_checks:
            self.stderr.write(f"CHECK FAILED: {check.check_name} {check.scope}: {check.observed}")
