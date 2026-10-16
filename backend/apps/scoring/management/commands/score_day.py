"""Score a stored day.

python manage.py score_day 2026-10-12 [--formula parity] [--formula-version 1]
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.scoring.service import ScoreRunFailed, run_scoring


class Command(BaseCommand):
    help = "Score the stored inputs of one day under a formula version."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("day", help="Market date, YYYY-MM-DD.")
        parser.add_argument("--formula", default="parity", help="Formula version name.")
        parser.add_argument(
            "--formula-version", type=int, help="Formula version number (default latest)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            day = date.fromisoformat(options["day"])
        except ValueError as exc:
            raise CommandError(f"Not a date: {options['day']}") from exc
        try:
            run = run_scoring(
                day, formula_name=options["formula"], formula_version=options["formula_version"]
            )
        except ScoreRunFailed as exc:
            raise CommandError(f"{exc} (run {exc.run.pk})") from exc
        self.stdout.write(
            f"Score run {run.pk} complete for {run.as_of_date}: {run.scored_n} of "
            f"{run.universe_n} companies scored under {run.formula_version.name} "
            f"v{run.formula_version.version}."
        )
