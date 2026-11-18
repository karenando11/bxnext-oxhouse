"""One-off contract check against the real Finviz API (writes nothing to the database).

    python manage.py finviz_contract_check [--save path.csv]

Downloads one export with the token from FINVIZ_AUTH_TOKEN, then reports how it lines up with the
column map: which headers matched, which did not, which repeat, blanks per column, and a few
sample cells with their parsed values. Use ``--save`` to keep the raw file (it is licensed data:
save it under backend/tests/fixtures/private/, which is git-ignored).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.marketdata.finviz.client import (
    FinvizDownloadError,
    FinvizTokenMissing,
    download,
)
from apps.marketdata.finviz.columns import COLUMNS, EXPECTED_COLUMN_COUNT
from apps.marketdata.finviz.ingest import build_request
from apps.marketdata.finviz.parser import ExportFormatError, parse_export

SAMPLE_ROWS = 3


class Command(BaseCommand):
    help = "Download one real export and report how it matches the column map."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--save", type=Path, help="Write the raw export to this path.")

    def handle(self, *args: Any, **options: Any) -> None:
        request = build_request()
        self.stdout.write(f"Requesting {request.public_url()}")
        try:
            got = download(request)
        except FinvizTokenMissing as exc:
            raise CommandError(str(exc)) from exc
        except FinvizDownloadError as exc:
            raise CommandError(f"Download failed ({exc.reason_code}): {exc}") from exc
        if options["save"]:
            options["save"].parent.mkdir(parents=True, exist_ok=True)
            options["save"].write_bytes(got.content)
            self.stdout.write(f"Saved the raw export to {options['save']}")
        try:
            parsed = parse_export(got.content)
        except ExportFormatError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"{parsed.row_count} companies, {len(parsed.headers)} columns "
            f"(the request is for {EXPECTED_COLUMN_COUNT})."
        )
        self.stdout.write(f"Matched {len(parsed.mapped)} of {len(COLUMNS)} mapped fields.")
        if parsed.missing_fields:
            self.stdout.write(f"NOT FOUND in the export: {', '.join(parsed.missing_fields)}")
        if parsed.unmapped_headers:
            self.stdout.write(f"Not in the column map: {', '.join(parsed.unmapped_headers)}")
        for header, positions in parsed.duplicate_headers.items():
            self.stdout.write(f"Repeated header {header!r} at positions {positions}")
        for name, position in parsed.duplicate_choice.items():
            self.stdout.write(f"  {name} reads position {position} ({parsed.headers[position]!r})")

        self.stdout.write("\nField, position, blanks, sample (raw -> parsed):")
        for column in COLUMNS:
            if column.field not in parsed.mapped:
                continue
            blanks = sum(1 for r in parsed.rows if r.values.get(column.field) in (None, ""))
            samples = [r.values.get(column.field) for r in parsed.rows[:SAMPLE_ROWS]]
            self.stdout.write(
                f"  {column.field:<20} #{parsed.mapped[column.field]:<3} "
                f"blank {blanks:>5}/{parsed.row_count}  {samples}"
            )
        if parsed.bad_cells:
            self.stdout.write(f"\nUnparseable cells: {dict(parsed.bad_cells)}")
