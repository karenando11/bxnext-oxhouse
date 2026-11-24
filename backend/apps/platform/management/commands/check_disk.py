"""Alert when disk usage is above 80 percent (Step 19). Run it from a timer, for example hourly.

    python manage.py check_disk                    # the database volume and the backup folder
    python manage.py check_disk --path / --limit 85

Inside the application container ``/`` is the container's disk; the host timer passes the
host's data directories instead (see deploy/README.md).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from apps.platform.disk import DEFAULT_LIMIT_PERCENT, check_disk


class Command(BaseCommand):
    help = "Raise one alert when a disk is more than 80 percent full."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--path", action="append", help="A path on the disk (repeatable).")
        parser.add_argument("--limit", type=float, default=DEFAULT_LIMIT_PERCENT)

    def handle(self, *args: Any, **options: Any) -> None:
        for reading in check_disk(options["path"] or ["/"], limit=options["limit"]):
            self.stdout.write(f"{reading.path}: {reading.percent_used:g}% used")
