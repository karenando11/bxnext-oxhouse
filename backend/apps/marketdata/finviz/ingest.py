"""The Stage A market step: download the Finviz export, keep the raw file, load the snapshot.

Order of work: download (three attempts), store the file unchanged under its hash, parse by
position, then in one transaction upsert companies and tickers and write one snapshot per company
for the day, then data-quality checks. A download or format failure stops the stage and is
recorded; a failed data-quality check is recorded and the load stands (Step 16 decides what
the rest of the pipeline does with it, Step 17 turns failures into alerts).

Loading the same file for the same day again changes nothing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from django.db import transaction

from apps.companies.models import Company, TickerHistory
from apps.marketdata.finviz.client import (
    DEFAULT_ATTEMPTS,
    DEFAULT_BASE_URL,
    DEFAULT_FILTER,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_VIEW,
    Download,
    FinvizDownloadError,
    FinvizRequest,
    FinvizTokenMissing,
    Transport,
    download,
    urllib_transport,
)
from apps.marketdata.finviz.columns import FIELDS, NUMERIC_FIELDS
from apps.marketdata.finviz.parser import ExportFormatError, ParsedExport, parse_export
from apps.marketdata.models import FinvizSnapshot
from apps.marketdata.storage import RawStorage, get_raw_storage, sha256_hex
from apps.platform.bulk import BATCH_SIZE, upsert
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from apps.platform.settings_store import get_setting

logger = logging.getLogger(__name__)

DEFAULT_ROW_COUNT_ALERT_PCT = 20.0
TEXT_FIELDS = ("company_name_raw", "sector_raw", "industry_raw", "earnings_date_text")
SNAPSHOT_FIELDS = (*NUMERIC_FIELDS, *TEXT_FIELDS)
QUALITY_FIELDS = tuple(f for f in FIELDS if f not in {"ticker", "sector_raw"})


class FinvizIngestFailed(RuntimeError):
    """The stage stopped. ``run`` and ``call`` are the stored records of what happened."""

    def __init__(self, run: PipelineRun, call: SourceCall, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.run, self.call, self.reason_code = run, call, reason_code


@dataclass
class IngestResult:
    run: PipelineRun
    call: SourceCall
    unchanged: bool = False
    rows: int = 0
    new_companies: int = 0
    industry_changes: list[dict[str, str]] = field(default_factory=list)
    checks: list[DataQualityCheck] = field(default_factory=list)

    @property
    def failed_checks(self) -> list[DataQualityCheck]:
        return [c for c in self.checks if c.status == DataQualityCheck.Status.FAIL]


def build_request() -> FinvizRequest:
    """The export request from the stored settings (view, sector filter, columns, attempts)."""
    return FinvizRequest(
        base_url=str(get_setting("finviz.base_url", DEFAULT_BASE_URL)),
        view=get_setting("finviz.view", DEFAULT_VIEW),
        sector_filter=str(get_setting("finviz.sector_filter", DEFAULT_FILTER)),
        columns=str(get_setting("finviz.columns", "")),
        attempts=int(get_setting("finviz.attempts", DEFAULT_ATTEMPTS)),
        timeout=float(get_setting("finviz.timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
    )


def scope_key(day: date, request: FinvizRequest, source: str = "") -> str:
    """What a source call covered: the day and the universe definition (never the token)."""
    columns = sha256_hex(request.columns.encode())[:8] if request.columns else "default"
    text = f"{day}|v={request.view}|f={request.sector_filter}|c={columns}"
    return f"{text}|{source}" if source else text


def _universe(key: str) -> str:
    """The view and filter part of a scope key."""
    return "|".join(part for part in key.split("|")[1:3])


class _Steps:
    """Collects the per-step records stored on ``PipelineRun.steps``."""

    def __init__(self, run: PipelineRun) -> None:
        self.run = run
        self.items: list[dict[str, Any]] = []

    def record(
        self,
        name: str,
        started: float,
        status: str = "ok",
        rows_in: int | None = None,
        rows_out: int | None = None,
        error: str = "",
    ) -> None:
        now = datetime.now(UTC)
        self.items.append(
            {
                "step_name": name,
                "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
                "finished_at": now.isoformat(),
                "status": status,
                "rows_in": rows_in,
                "rows_out": rows_out,
                "error_text": error,
            }
        )
        self.run.steps = self.items
        self.run.save(update_fields=["steps"])


def _fail(
    run: PipelineRun,
    steps: _Steps,
    call: SourceCall,
    check_name: str,
    reason: str,
    message: str,
    attempts: int,
    http_status: int | None = None,
) -> FinvizIngestFailed:
    """Record a stopped stage: the call, a failed check (the alert) and the failed run."""
    call.status, call.reason_code, call.attempts = SourceCall.Status.FAILED, reason, attempts
    call.http_status, call.error_text = http_status, message
    call.completed_at = datetime.now(UTC)
    call.save()
    DataQualityCheck.objects.create(
        run=run,
        check_name=check_name,
        scope="finviz",
        expected="a usable export",
        observed=reason,
        status=DataQualityCheck.Status.FAIL,
        detail={"message": message, "attempts": attempts},
    )
    run.status, run.finished_at = PipelineRun.Status.FAILED, datetime.now(UTC)
    run.save(update_fields=["status", "finished_at"])
    logger.error("Finviz stage stopped (%s): %s", reason, message)
    return FinvizIngestFailed(run, call, reason, message)


def ingest_finviz(
    as_of_date: date,
    *,
    trigger: str = PipelineRun.Trigger.MANUAL,
    content: bytes | None = None,
    source_name: str = "",
    token: str | None = None,
    transport: Transport = urllib_transport,
    sleep: Callable[[float], None] = time.sleep,
    storage: RawStorage | None = None,
) -> IngestResult:
    """Run the Finviz step for ``as_of_date``.

    With ``content`` (a saved raw file) nothing is downloaded: used for dry runs, reloads and
    the contract sample. Raises ``FinvizIngestFailed`` when the stage has to stop.
    """
    request = build_request()
    storage = storage or get_raw_storage()
    key = scope_key(as_of_date, request, source_name)
    started_at = datetime.now(UTC)
    run = PipelineRun.objects.create(
        stage=PipelineRun.Stage.MARKET,
        as_of_date=as_of_date,
        trigger=trigger,
        started_at=started_at,
    )
    steps = _Steps(run)
    call = SourceCall.objects.create(
        run=run,
        feed=SourceCall.Feed.FINVIZ,
        scope_key=key,
        requested_at=started_at,
        status=SourceCall.Status.PARTIAL,
        storage_uri="",
    )

    # 1. Download
    tick = time.time()
    if content is None:
        try:
            got: Download = download(request, token=token, transport=transport, sleep=sleep)
        except FinvizTokenMissing as exc:
            steps.record("download", tick, "failed", error=str(exc))
            raise _fail(run, steps, call, "finviz_download", "fetch_error", str(exc), 0) from exc
        except FinvizDownloadError as exc:
            steps.record("download", tick, "failed", error=str(exc))
            raise _fail(
                run,
                steps,
                call,
                "finviz_download",
                exc.reason_code,
                str(exc),
                exc.attempts,
                exc.http_status,
            ) from exc
        content, attempts, http_status = got.content, got.attempts, got.http_status
    else:
        attempts, http_status = 1, None
    steps.record("download", tick, rows_out=len(content))

    # 2. Keep the file unchanged, named by its hash
    tick = time.time()
    digest = sha256_hex(content)
    uri = storage.put(f"finviz/{as_of_date}/{digest}.csv", content)
    call.content_hash, call.storage_uri, call.bytes = digest, uri, len(content)
    call.attempts, call.http_status = attempts, http_status
    steps.record("store_raw", tick, rows_in=len(content))

    # 3. The same file for the same day was loaded before: nothing to do
    earlier = SourceCall.objects.filter(
        feed=SourceCall.Feed.FINVIZ,
        status=SourceCall.Status.OK,
        content_hash=digest,
        scope_key__startswith=f"{as_of_date}|",
    ).exclude(pk=call.pk)
    if earlier.exists() and FinvizSnapshot.objects.filter(as_of_date=as_of_date).exists():
        call.status, call.completed_at = SourceCall.Status.SKIPPED, datetime.now(UTC)
        call.save()
        steps.record("load", time.time(), "skipped")
        run.status, run.finished_at = PipelineRun.Status.OK, datetime.now(UTC)
        run.save(update_fields=["status", "finished_at"])
        return IngestResult(run, call, unchanged=True)

    # 4. Parse by position
    tick = time.time()
    try:
        parsed = parse_export(content)
        if not parsed.rows:
            raise ExportFormatError("The export has a header but no companies.")
    except ExportFormatError as exc:
        steps.record("parse", tick, "failed", error=str(exc))
        raise _fail(run, steps, call, "finviz_format", "parse_error", str(exc), attempts) from exc
    steps.record("parse", tick, rows_in=len(content), rows_out=parsed.row_count)

    # 5. Load and check, in one transaction
    tick = time.time()
    with transaction.atomic():
        result = IngestResult(run, call, rows=parsed.row_count)
        _load(parsed, as_of_date, call, result)
        steps.record("load", tick, rows_in=parsed.row_count, rows_out=parsed.row_count)
        tick = time.time()
        _run_checks(parsed, as_of_date, request, call, result)
        steps.record("quality_checks", tick, rows_out=len(result.checks))
        call.status, call.completed_at = SourceCall.Status.OK, datetime.now(UTC)
        call.save()
        failed = bool(result.failed_checks)
        run.status = PipelineRun.Status.PARTIAL if failed else PipelineRun.Status.OK
        run.finished_at = datetime.now(UTC)
        run.save(update_fields=["status", "finished_at"])
    for check in result.failed_checks:
        logger.error("Finviz data-quality check failed: %s %s", check.check_name, check.scope)
    return result


# --------------------------------------------------------------------------------------- load


def _load(parsed: ParsedExport, day: date, call: SourceCall, result: IngestResult) -> None:
    tickers = [row.ticker for row in parsed.rows]
    open_rows = TickerHistory.objects.select_related("company").filter(
        ticker__in=tickers, valid_to__isnull=True
    )
    companies: dict[str, Company] = {row.ticker: row.company for row in open_rows}

    new_rows = [row for row in parsed.rows if row.ticker not in companies]
    created = Company.objects.bulk_create(
        [
            Company(
                name=row.values.get("company_name_raw") or row.ticker,
                sector=row.values.get("sector_raw") or "",
                industry=row.values.get("industry_raw") or "",
                first_seen_date=day,
                last_seen_date=day,
            )
            for row in new_rows
        ],
        batch_size=BATCH_SIZE,
    )
    TickerHistory.objects.bulk_create(
        [
            TickerHistory(company=company, ticker=row.ticker, valid_from=day)
            for company, row in zip(created, new_rows, strict=True)
        ],
        batch_size=BATCH_SIZE,
    )
    for company, row in zip(created, new_rows, strict=True):
        companies[row.ticker] = company
    result.new_companies = len(created)

    # Existing companies: refresh the descriptive fields and mark them seen today. A company that
    # is absent from today's file is not touched, so its last_seen_date does not advance.
    changed: list[Company] = []
    new_ids = {c.pk for c in created}
    for row in parsed.rows:
        company = companies[row.ticker]
        if company.pk in new_ids:
            continue
        industry = row.values.get("industry_raw") or ""
        if company.industry and industry and company.industry != industry:
            result.industry_changes.append(
                {"ticker": row.ticker, "from": company.industry, "to": industry}
            )
        company.name = row.values.get("company_name_raw") or company.name
        company.sector = row.values.get("sector_raw") or company.sector
        company.industry = industry or company.industry
        company.last_seen_date = max(company.last_seen_date, day)
        changed.append(company)
    if changed:
        Company.objects.bulk_update(
            changed, ["name", "sector", "industry", "last_seen_date"], batch_size=BATCH_SIZE
        )

    # The saved Finviz view can carry 100 or more columns we do not use; keeping them is opt-in.
    store_extras = bool(get_setting("finviz.store_extras", False))
    existing = {
        s.company_id: s
        for s in FinvizSnapshot.objects.filter(as_of_date=day, company__in=companies.values())
    }
    wanted: dict[int, dict[str, Any]] = {}
    create_with: dict[int, dict[str, Any]] = {}
    for row in parsed.rows:
        company = companies[row.ticker]
        values = {name: row.values.get(name) for name in NUMERIC_FIELDS}
        values.update({name: row.values.get(name) or "" for name in TEXT_FIELDS})
        values["earnings_date"] = row.values.get("earnings_date")
        values["extras"] = row.extras if store_extras else {}
        values["source_call"] = call
        wanted[company.pk] = values
        create_with[company.pk] = {"company": company, "as_of_date": day}
    upsert(FinvizSnapshot, existing, wanted, create_with)


# ------------------------------------------------------------------------------------- checks


def _check(
    result: IngestResult,
    name: str,
    scope: str,
    status: str,
    *,
    expected: str = "",
    observed: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    result.checks.append(
        DataQualityCheck.objects.create(
            run=result.run,
            check_name=name,
            scope=scope,
            expected=expected,
            observed=observed,
            status=status,
            detail=detail or {},
        )
    )


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def _run_checks(
    parsed: ParsedExport, day: date, request: FinvizRequest, call: SourceCall, result: IngestResult
) -> None:
    ok, warn, fail = (
        DataQualityCheck.Status.PASS,
        DataQualityCheck.Status.WARN,
        DataQualityCheck.Status.FAIL,
    )

    # A column that is blank for every company (the eps_this case) or missing from the file
    blank = []
    for name in QUALITY_FIELDS:
        absent = name in parsed.missing_fields
        if absent or all(_is_blank(row.values.get(name)) for row in parsed.rows):
            blank.append(name)
            _check(
                result,
                "blank_column",
                f"finviz.{name}",
                fail,
                expected="some values",
                observed="column missing from the export" if absent else "blank for every company",
            )
    if not blank:
        _check(result, "blank_column", "finviz.*", ok, observed="no blank columns")

    # Row count against the previous day
    previous_day = (
        FinvizSnapshot.objects.filter(as_of_date__lt=day)
        .order_by("-as_of_date")
        .values_list("as_of_date", flat=True)
        .first()
    )
    threshold = float(get_setting("finviz.row_count_change_alert_pct", DEFAULT_ROW_COUNT_ALERT_PCT))
    if previous_day is None:
        _check(
            result,
            "row_count",
            "finviz",
            ok,
            observed=str(parsed.row_count),
            detail={"first_run": True},
        )
    else:
        before = FinvizSnapshot.objects.filter(as_of_date=previous_day).count()
        change = 100.0 * (parsed.row_count - before) / before if before else 0.0
        _check(
            result,
            "row_count",
            "finviz",
            fail if abs(change) > threshold else ok,
            expected=f"within {threshold:g}% of {before}",
            observed=str(parsed.row_count),
            detail={
                "previous_day": previous_day.isoformat(),
                "previous": before,
                "change_pct": round(change, 2),
                "threshold_pct": threshold,
            },
        )

    # A changed universe definition (view or sector filter) is visible, not silent
    last = (
        SourceCall.objects.filter(feed=SourceCall.Feed.FINVIZ, status=SourceCall.Status.OK)
        .exclude(pk=call.pk)
        .order_by("-requested_at")
        .first()
    )
    now_universe = _universe(call.scope_key)
    if last is not None and _universe(last.scope_key) != now_universe:
        _check(
            result,
            "universe_definition",
            "finviz",
            warn,
            expected=_universe(last.scope_key),
            observed=now_universe,
            detail={"previous_scope": last.scope_key, "scope": call.scope_key},
        )
    else:
        _check(result, "universe_definition", "finviz", ok, observed=now_universe)

    if parsed.duplicate_tickers:
        _check(
            result,
            "duplicate_tickers",
            "finviz",
            warn,
            observed=str(len(parsed.duplicate_tickers)),
            detail={"tickers": sorted(set(parsed.duplicate_tickers)), "kept": "first row"},
        )
    if parsed.unmapped_headers:
        _check(
            result,
            "unmapped_columns",
            "finviz",
            ok,
            observed=str(len(parsed.unmapped_headers)),
            detail={
                "headers": parsed.unmapped_headers,
                "stored": bool(get_setting("finviz.store_extras", False)),
            },
        )
    if parsed.duplicate_headers:
        headers = parsed.headers
        _check(
            result,
            "duplicate_headers",
            "finviz",
            ok,
            observed=", ".join(sorted(parsed.duplicate_headers)),
            detail={
                "positions": parsed.duplicate_headers,
                "chosen": {f: [p, headers[p]] for f, p in parsed.duplicate_choice.items()},
            },
        )
    if parsed.bad_cells:
        _check(
            result,
            "unparseable_cells",
            "finviz",
            warn,
            observed=str(sum(parsed.bad_cells.values())),
            detail={"by_field": dict(parsed.bad_cells)},
        )
    if result.industry_changes:
        _check(
            result,
            "industry_changes",
            "finviz",
            warn,
            observed=str(len(result.industry_changes)),
            detail={"changes": result.industry_changes},
        )
