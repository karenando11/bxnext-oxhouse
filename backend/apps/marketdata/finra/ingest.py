"""The FINRA short-volume step of the filings stage.

For each trading date still missing (the previous 45 calendar days on the first run, the last
week afterwards) the consolidated file is downloaded once, stored unchanged under its hash, and
the rows for universe tickers are loaded into ``short_volume_daily``. A date with no file (a
weekend or a market holiday, or a file not published yet) is recorded as ``no_file_for_date`` and
is not a failure; it is tried again on later runs while it is inside the retry window. A universe
ticker missing from a day's file is listed in that day's coverage check with reason
``not_in_finra_file``.

A failure here never raises and never blocks scoring: it is stored, and the
``short_volume_metric`` view keeps using the latest stored trading days (``age_days`` and
``is_stale`` say how old they are). Loading a date again changes nothing.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from django.db import transaction

from apps.companies.models import Company, TickerHistory
from apps.marketdata.finra.client import (
    FinraDownload,
    FinraDownloadError,
    FinraNoFile,
    Transport,
    download,
    file_url,
    urllib_transport,
)
from apps.marketdata.finra.parser import (
    ParsedShortVolume,
    ShortVolumeFormatError,
    parse_short_volume,
)
from apps.marketdata.models import ShortVolumeDaily
from apps.marketdata.storage import RawStorage, get_raw_storage, sha256_hex
from apps.platform.bulk import BATCH_SIZE
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from apps.platform.steps import StepRecorder

logger = logging.getLogger(__name__)

BACKFILL_DAYS = 45
RETRY_DAYS = 7
MAX_TICKERS_IN_CHECK = 200


@dataclass
class DateOutcome:
    day: date
    status: str  # loaded, unchanged, no_file_for_date, failed
    rows: int = 0
    matched: int = 0
    reason_code: str | None = None


@dataclass
class ShortVolumeResult:
    run: PipelineRun
    outcomes: list[DateOutcome] = field(default_factory=list)

    def of(self, status: str) -> list[date]:
        return [o.day for o in self.outcomes if o.status == status]

    @property
    def rows_loaded(self) -> int:
        return sum(o.rows for o in self.outcomes if o.status == "loaded")

    @property
    def failed(self) -> dict[date, str]:
        return {
            o.day: o.reason_code or "fetch_error" for o in self.outcomes if o.status == "failed"
        }


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_symbol(symbol: str) -> str:
    """Compare tickers without punctuation: FINRA writes ``BRK.B`` where Finviz writes ``BRK-B``."""
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


class _Universe:
    """The companies with a current ticker, found by exact symbol first and then normalised."""

    def __init__(self) -> None:
        rows = TickerHistory.objects.filter(
            valid_to__isnull=True, company__status=Company.Status.ACTIVE
        ).values_list("ticker", "company_id")
        self.exact: dict[str, int] = {}
        self.loose: dict[str, int | None] = {}
        for ticker, company_id in rows:
            self.exact[ticker] = company_id
            key = normalize_symbol(ticker)
            # Two tickers that normalise alike are ambiguous: only an exact match counts.
            self.loose[key] = None if self.loose.get(key, company_id) != company_id else company_id
        self.names = {company_id: ticker for ticker, company_id in rows}

    def find(self, symbol: str) -> int | None:
        found = self.exact.get(symbol)
        if found is not None:
            return found
        if symbol != symbol.upper():
            return None  # a lowercase letter marks a different security (BCpC is not BCPC)
        return self.loose.get(normalize_symbol(symbol))

    def __len__(self) -> int:
        return len(self.names)


def dates_to_fetch(as_of_date: date) -> list[date]:
    """Calendar dates before ``as_of_date`` that still need a file, oldest first."""
    loaded = SourceCall.objects.filter(feed=SourceCall.Feed.FINRA, status=SourceCall.Status.OK)
    first_run = not ShortVolumeDaily.objects.exists() and not loaded.exists()
    lookback = BACKFILL_DAYS if first_run else RETRY_DAYS
    done = set(loaded.values_list("scope_key", flat=True))
    days = (as_of_date - timedelta(days=n) for n in range(lookback, 0, -1))
    return [d for d in days if d.isoformat() not in done]


def _open_call(run: PipelineRun, day: date) -> SourceCall:
    return SourceCall.objects.create(
        run=run,
        feed=SourceCall.Feed.FINRA,
        scope_key=day.isoformat(),
        requested_at=_now(),
        status=SourceCall.Status.PARTIAL,
    )


def _close(
    call: SourceCall,
    status: str,
    reason: str | None = None,
    error: str = "",
    http_status: int | None = None,
    attempts: int | None = None,
) -> None:
    call.status, call.reason_code, call.error_text = status, reason, error
    if http_status is not None:
        call.http_status = http_status
    if attempts is not None:
        call.attempts = attempts
    call.completed_at = _now()
    call.save()


def _check(
    run: PipelineRun, name: str, day: date, status: str, expected: str, observed: str, detail: dict
) -> DataQualityCheck:
    return DataQualityCheck.objects.create(
        run=run,
        check_name=name,
        scope=f"finra.{day.isoformat()}",
        expected=expected,
        observed=observed,
        status=status,
        detail=detail,
    )


def _load(
    parsed: ParsedShortVolume, day: date, universe: _Universe, call: SourceCall
) -> tuple[int, set[int]]:
    """Upsert the rows for universe tickers. Returns (rows stored, company ids seen)."""
    wanted = []
    seen: set[int] = set()
    for row in parsed.rows:
        company_id = universe.find(row.symbol)
        if company_id is None:
            continue
        seen.add(company_id)
        wanted.append(
            ShortVolumeDaily(
                ticker_raw=row.symbol,
                company_id=company_id,
                trade_date=day,
                short_volume=row.short_volume,
                short_exempt_volume=row.short_exempt_volume,
                total_volume=row.total_volume,
                market=row.market,
                source_call=call,
            )
        )
    ShortVolumeDaily.objects.bulk_create(
        wanted,
        batch_size=BATCH_SIZE,
        update_conflicts=True,
        unique_fields=["ticker_raw", "trade_date", "market"],
        update_fields=[
            "company",
            "short_volume",
            "short_exempt_volume",
            "total_volume",
            "source_call",
        ],
    )
    return len(wanted), seen


def _process_date(
    run: PipelineRun,
    day: date,
    universe: _Universe,
    storage: RawStorage,
    transport: Transport,
    sleep: Callable[[float], None],
) -> DateOutcome:
    call = _open_call(run, day)
    if day.weekday() >= 5:  # no file is published for a weekend, so none is asked for
        _close(call, SourceCall.Status.SKIPPED, "no_file_for_date")
        return DateOutcome(day, "no_file_for_date", reason_code="no_file_for_date")
    try:
        got: FinraDownload = download(day, transport=transport, sleep=sleep)
    except FinraNoFile as exc:
        _close(call, SourceCall.Status.SKIPPED, "no_file_for_date", http_status=exc.http_status)
        return DateOutcome(day, "no_file_for_date", reason_code="no_file_for_date")
    except FinraDownloadError as exc:
        _close(
            call,
            SourceCall.Status.FAILED,
            exc.reason_code,
            str(exc),
            exc.http_status,
            exc.attempts,
        )
        _check(
            run,
            "finra_download",
            day,
            DataQualityCheck.Status.WARN,
            "a file or a clean no-file answer",
            exc.reason_code,
            {"message": str(exc), "attempts": exc.attempts, "url": file_url(day)},
        )
        logger.error("FINRA download failed for %s (%s): %s", day, exc.reason_code, exc)
        return DateOutcome(day, "failed", reason_code=exc.reason_code)

    digest = sha256_hex(got.content)
    call.content_hash, call.bytes = digest, len(got.content)
    call.http_status, call.attempts = got.http_status, got.attempts
    call.storage_uri = storage.put(f"finra/{day.isoformat()}/{digest}.txt", got.content)
    previous = (
        SourceCall.objects.filter(
            feed=SourceCall.Feed.FINRA,
            status=SourceCall.Status.OK,
            scope_key=day.isoformat(),
            content_hash=digest,
        )
        .exclude(pk=call.pk)
        .exists()
    )
    if previous and ShortVolumeDaily.objects.filter(trade_date=day).exists():
        _close(call, SourceCall.Status.SKIPPED)
        return DateOutcome(day, "unchanged")
    try:
        parsed = parse_short_volume(got.content, day)
    except ShortVolumeFormatError as exc:
        _close(call, SourceCall.Status.FAILED, "parse_error", str(exc), attempts=got.attempts)
        _check(
            run,
            "finra_format",
            day,
            DataQualityCheck.Status.FAIL,
            "a readable file",
            "parse_error",
            {"message": str(exc)},
        )
        logger.error("FINRA file for %s is unreadable: %s", day, exc)
        return DateOutcome(day, "failed", reason_code="parse_error")

    with transaction.atomic():
        stored, seen = _load(parsed, day, universe, call)
        missing = sorted(name for cid, name in universe.names.items() if cid not in seen)
        status = DataQualityCheck.Status.PASS
        if not seen:
            status = DataQualityCheck.Status.FAIL
        elif missing:
            status = DataQualityCheck.Status.WARN
        _check(
            run,
            "finra_universe_coverage",
            day,
            status,
            f"{len(universe)} universe tickers",
            f"{len(seen)} found",
            {
                "reason_code": "not_in_finra_file",
                "missing_count": len(missing),
                "missing": missing[:MAX_TICKERS_IN_CHECK],
                "file_rows": len(parsed.rows),
            },
        )
        if parsed.malformed:
            _check(
                run,
                "finra_malformed_rows",
                day,
                DataQualityCheck.Status.WARN,
                "0",
                str(parsed.malformed),
                {"malformed": parsed.malformed},
            )
        _close(call, SourceCall.Status.OK)
    return DateOutcome(day, "loaded", rows=stored, matched=len(seen))


def collect_short_volume(
    as_of_date: date,
    *,
    trigger: str = PipelineRun.Trigger.MANUAL,
    dates: list[date] | None = None,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
    storage: RawStorage | None = None,
) -> ShortVolumeResult:
    """Run the short-volume step for ``as_of_date``.

    ``dates`` fetches exactly those trading dates (a reload or a repair) instead of the missing
    ones. Never raises for a download or format problem: they are stored on the run.
    """
    storage = storage or get_raw_storage()
    run = PipelineRun.objects.create(
        stage=PipelineRun.Stage.FILINGS,
        as_of_date=as_of_date,
        trigger=trigger,
        started_at=_now(),
    )
    steps = StepRecorder(run)
    result = ShortVolumeResult(run)
    universe = _Universe()
    wanted = sorted(set(dates)) if dates is not None else dates_to_fetch(as_of_date)
    seen_weekend = set(
        SourceCall.objects.filter(
            feed=SourceCall.Feed.FINRA,
            reason_code="no_file_for_date",
            scope_key__in=[d.isoformat() for d in wanted if d.weekday() >= 5],
        ).values_list("scope_key", flat=True)
    )
    for day in wanted:
        if day.weekday() >= 5 and day.isoformat() in seen_weekend:
            continue  # recorded on an earlier run; nothing to ask or to say again
        tick = time.time()
        try:
            outcome = _process_date(run, day, universe, storage, transport, sleep)
        except Exception as exc:  # one date never stops the others
            logger.exception("FINRA short volume failed for %s", day)
            SourceCall.objects.filter(run=run, scope_key=day.isoformat()).filter(
                status=SourceCall.Status.PARTIAL
            ).update(
                status=SourceCall.Status.FAILED,
                reason_code="parse_error",
                error_text=f"{type(exc).__name__}: {exc}"[:1000],
                completed_at=_now(),
            )
            outcome = DateOutcome(day, "failed", reason_code="parse_error")
        result.outcomes.append(outcome)
        steps.record(
            day.isoformat(),
            tick,
            "failed" if outcome.status == "failed" else "ok",
            rows_out=outcome.rows,
            error=outcome.reason_code or "" if outcome.status == "failed" else "",
        )
    run.status = PipelineRun.Status.PARTIAL if result.failed else PipelineRun.Status.OK
    run.finished_at = _now()
    run.save(update_fields=["status", "finished_at"])
    return result
