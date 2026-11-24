"""Run the daily batch (Step 16). A timer starts it at 06:00 US Eastern, Monday to Friday.

    python manage.py run_batch                          # for today (US Eastern)
    python manage.py run_batch --date 2026-10-13        # for a given run date (a re-run resumes)
    python manage.py run_batch --dry-run --fixtures DIR # no network: answers come from DIR

Exits with an error when the batch failed (Finviz down, scoring blocked or failed, publish
rolled back) so the timer sees it. A partial batch (a source failed, scoring ran) exits normally.
A second start while one is running does nothing.

Example crontab (the host's clock is set to US Eastern, or use CRON_TZ):

    CRON_TZ=America/New_York
    0 6 * * 1-5  cd /app && python manage.py run_batch
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.platform.batch import replay_sources, run_batch
from apps.platform.models import PipelineRun


class Command(BaseCommand):
    help = "Run the daily batch: all sources, scoring, the default screen and the read model."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--date", help="Run date, YYYY-MM-DD (default: today, US Eastern).")
        parser.add_argument(
            "--dry-run", action="store_true", help="Answer every request from files."
        )
        parser.add_argument("--fixtures", help="Directory with finviz.csv and manifest.json.")
        parser.add_argument(
            "--schedule", action="store_true", help="Record the run as started by the timer."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            day = (
                date.fromisoformat(options["date"])
                if options["date"]
                else datetime.now(ZoneInfo("America/New_York")).date()
            )
        except ValueError as exc:
            raise CommandError(f"Not a date: {options['date']}") from exc
        sources = None
        if options["dry_run"]:
            if not options["fixtures"]:
                raise CommandError("--dry-run needs --fixtures DIR.")
            sources = replay_sources(Path(options["fixtures"]))
        trigger = (
            PipelineRun.Trigger.SCHEDULE if options["schedule"] else PipelineRun.Trigger.MANUAL
        )
        result = run_batch(day, trigger=trigger, sources=sources)
        if result.status == "locked":
            self.stdout.write("A batch is already running. Nothing was started.")
            return
        assert result.run is not None
        self.stdout.write(
            f"Batch {result.run.pk} for run date {day}, market date {result.market_date}: "
            f"{result.status}"
            + ("" if result.new_market_day else " (no new market day)")
            + (", resumed" if result.resumed else "")
            + (", scored and published" if result.scored else "")
            + "."
        )
        for step in result.run.steps:
            line = f"  {step['step_name']:<14} {step['status']:<8}"
            if step["error_text"]:
                line += f" {step['error_text']}"
            self.stdout.write(line)
        if result.status == PipelineRun.Status.FAILED:
            raise CommandError("The batch failed; see the steps above.")
