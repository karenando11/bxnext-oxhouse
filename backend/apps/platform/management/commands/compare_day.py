"""The daily diff against the current system (Step 20).

    python manage.py compare_day --export current_2026-11-24.csv --date 2026-11-24

``--date`` is the market date both systems describe (default: the date of the latest score run).
The report is stored (one row per day) and summarised. Run it every day of the parallel run, then
``python manage.py parallel_status`` for the pass condition and the sign-off checklist.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.platform.parallel import CLASSES, compare_day, read_export, save_report
from apps.scoring.models import ScoreRun


class Command(BaseCommand):
    help = "Compare one day's export of the current system with this system's scores and fields."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--export", required=True, help="The current system's daily CSV.")
        parser.add_argument("--date", help="Market date, YYYY-MM-DD (default: latest score run).")
        parser.add_argument("--top", type=int, default=15, help="Differences to list.")

    def handle(self, *args: Any, **options: Any) -> None:
        path = Path(options["export"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")
        if options["date"]:
            try:
                day = date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError(f"Not a date: {options['date']}") from exc
        else:
            run = (
                ScoreRun.objects.filter(status=ScoreRun.Status.COMPLETE)
                .order_by("-as_of_date", "-id")
                .first()
            )
            if run is None:
                raise CommandError("There is no completed score run.")
            day = run.as_of_date
        report = compare_day(day, read_export(path))
        if report["our_companies"] == 0:
            raise CommandError(
                f"The read model has no rows for {day}. It serves the latest score run only; "
                "compare on the day it was scored."
            )
        save_report(day, report)
        out = self.stdout.write
        out(
            f"{day}: {report['compared']} companies compared "
            f"({report['only_in_export_n']} only in the export, {report['only_here_n']} only here)."
        )
        out(
            f"Scores within 0.05: {report['within_tolerance']} "
            f"({report['share_within_tolerance']:.1%})."
        )
        out("Differences by class: " + ", ".join(f"{c} {report['by_class'][c]}" for c in CLASSES))
        shown = [
            d
            for d in (*report["score_diffs"], *report["field_diffs"])
            if d["class"] == "unexplained"
        ]
        for entry in shown[: options["top"]]:
            out(f"  {entry['ticker']:<7} {entry['field']:<24} {entry['old']!r} -> {entry['new']!r}")
        out(report["not_compared"])
