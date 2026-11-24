"""The parallel-run pass condition and the sign-off checklist (Step 20).

python manage.py parallel_status                 # the 14 trading days ending at the latest report
python manage.py parallel_status --end 2026-12-11 --checklist
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.platform.models import ParallelDiff
from apps.platform.parallel import checklist, evaluate_pass


class Command(BaseCommand):
    help = "Evaluate the parallel-run acceptance rule over the stored daily reports."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--end", help="Last day of the window (default: latest report).")
        parser.add_argument(
            "--checklist", action="store_true", help="Print the sign-off checklist."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        reports = {r.day: r.report for r in ParallelDiff.objects.all()}
        end = date.fromisoformat(options["end"]) if options["end"] else None
        result = evaluate_pass(reports, end)
        if options["checklist"]:
            self.stdout.write(checklist(result))
            return
        for key, value in result.items():
            self.stdout.write(f"{key}: {value}")
