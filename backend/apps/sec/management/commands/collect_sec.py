"""Run the SEC step for a day: tickers to CIKs, new filings, and the facts refresh.

    python manage.py collect_sec [--date 2026-10-12] [--ticker KTTA --ticker PACS] [--skip-form4]

New Form 4 entries found in the submissions feeds are fetched and stored straight away (Step 11B)
unless --skip-form4 is given.

The User-Agent comes from the SEC_USER_AGENT environment variable (a name and a contact address).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.sec.collector import SecCollectionFailed, collect_sec
from apps.sec.form4_collector import Form4Collector


class Command(BaseCommand):
    help = "Collect SEC filings and allow-listed XBRL facts for the company universe."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--date", help="Market date, YYYY-MM-DD (default: today, US Eastern).")
        parser.add_argument(
            "--ticker", action="append", help="Only this ticker (repeat for several)."
        )
        parser.add_argument(
            "--skip-form4", action="store_true", help="Do not fetch the new Form 4 documents."
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
        form4 = None if options["skip_form4"] else Form4Collector(day)
        try:
            result = collect_sec(day, tickers=options["ticker"], form4_handler=form4)
        except SecCollectionFailed as exc:
            raise CommandError(f"Stage stopped ({exc.reason_code}): {exc}") from exc
        self.stdout.write(
            f"{result.companies} companies for {day}: {result.filings_added} new filings, "
            f"{result.facts_added} new facts from {result.facts_refreshed} refreshes, "
            f"{result.unchanged} unchanged, {len(result.no_cik)} without a CIK, "
            f"{len(result.form4_entries)} new Form 4 entries."
        )
        for ticker, reason in result.failures.items():
            self.stderr.write(f"FAILED: {ticker}: {reason}")
        if form4 is not None and form4.result is not None:
            r = form4.result
            self.stdout.write(
                f"Form 4: {r.stored} stored ({r.transactions} transactions), "
                f"{len(r.parse_failures)} unparseable, {len(r.fetch_failures)} not fetched, "
                f"{r.too_old} older than 90 days."
            )
            for accession, reason in r.parse_failures.items():
                self.stderr.write(f"UNPARSEABLE: {accession}: {reason}")
            for accession, reason in r.fetch_failures.items():
                self.stderr.write(f"FAILED: {accession}: {reason}")
