"""Load the golden day (and optionally the runway export) as fixture data.

python manage.py load_golden path/to/golden_export.csv [--runway path/to/runway_export.csv]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.platform.golden import load_golden


class Command(BaseCommand):
    help = "Load a golden export as fixture data. Safe to run again."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("export", type=Path, help="The golden export CSV.")
        parser.add_argument("--runway", type=Path, help="The runway export CSV to overlay.")

    def handle(self, *args: Any, **options: Any) -> None:
        export: Path = options["export"]
        runway: Path | None = options["runway"]
        for path in (export, runway):
            if path is not None and not path.is_file():
                raise CommandError(f"File not found: {path}")
        result = load_golden(export, runway)
        self.stdout.write(
            f"Loaded {result.companies} companies, {result.snapshots} snapshots, "
            f"{result.runway_calcs} runway rows and {result.catalyst_signals} catalyst signals."
        )
        if runway is not None:
            self.stdout.write(
                f"Runway overlay matched {result.overlay_matched} companies "
                f"({result.overlay_with_cash_and_burn} with cash and burn); "
                f"{len(result.overlay_unmatched_tickers)} export tickers are not in the universe."
            )
