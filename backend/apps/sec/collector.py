"""The SEC step of the filings stage: ticker to CIK, filing discovery and the facts refresh.

For each company in the universe: resolve the CIK from company_tickers.json (fetched once a day),
read the submissions feed and store new filings, then fetch companyfacts when there is a new
filing, no usable stored facts, or the last attempt failed. Only allow-listed USD facts are
stored. A payload whose hash equals the stored hash is not reloaded.

One company's failure is stored on its own source call and never stops the others. Every request
goes through ``SecClient`` and so through the one shared limiter. New Form 4 entries are handed
to the Form 4 collector (Step 11B) through ``form4_handler`` and returned on the result.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from django.db import transaction

from apps.companies.models import Company, TickerHistory
from apps.marketdata.storage import RawStorage, get_raw_storage, sha256_hex
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from apps.platform.steps import StepRecorder
from apps.sec.client import SecClient, SecFetchError, SecResponse, SecUserAgentMissing
from apps.sec.models import SecFact, SecFiling, SecForm4Filing
from apps.sec.parsing import (
    FORM4_WINDOW_DAYS,
    FactRow,
    SubmissionEntry,
    is_form4,
    is_stored_filing_form,
    parse_company_tickers,
    parse_companyfacts,
    parse_submissions,
)
from apps.sec.urls import FACTS_URL, SUBMISSIONS_URL, TICKERS_URL

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
# A company whose last facts call ended like this is not asked again until it files something.
TERMINAL_FACT_REASONS = ("no_companyfacts", "non_usd_only")
MAX_FAILURES_IN_CHECK = 50


@dataclass(frozen=True)
class Form4Entry:
    """A Form 4 or 4/A seen in the submissions feed and not yet stored by Step 11B."""

    company_id: int
    cik: str
    accession: str
    form: str
    filed_at: date
    period_of_report: date | None
    primary_document: str


Form4Handler = Callable[[list[Form4Entry]], None]


class SecCollectionFailed(RuntimeError):
    """The stage could not start (no User-Agent, or the ticker file is unavailable)."""

    def __init__(self, run: PipelineRun, call: SourceCall, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.run, self.call, self.reason_code = run, call, reason_code


@dataclass
class CollectionResult:
    run: PipelineRun
    companies: int = 0
    no_cik: list[str] = field(default_factory=list)
    filings_added: int = 0
    facts_refreshed: int = 0
    facts_added: int = 0
    unchanged: int = 0
    skipped_facts: dict[str, str] = field(default_factory=dict)  # ticker -> reason code
    failures: dict[str, str] = field(default_factory=dict)  # ticker -> reason code
    form4_entries: list[Form4Entry] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def _open_call(run: PipelineRun, feed: str, scope: str) -> SourceCall:
    return SourceCall.objects.create(
        run=run,
        feed=feed,
        scope_key=scope,
        requested_at=_now(),
        status=SourceCall.Status.PARTIAL,
    )


def _close_call(
    call: SourceCall,
    status: str,
    *,
    reason: str | None = None,
    response: SecResponse | None = None,
    error: str = "",
    attempts: int | None = None,
    http_status: int | None = None,
) -> None:
    call.status, call.reason_code, call.error_text = status, reason, error
    if response is not None:
        call.attempts, call.http_status = response.attempts, response.http_status
    if attempts is not None:
        call.attempts = attempts
    if http_status is not None:
        call.http_status = http_status
    call.completed_at = _now()
    call.save()


def _keep(storage: RawStorage, call: SourceCall, kind: str, scope: str, content: bytes) -> str:
    """Store the payload unchanged, named by its hash, and note it on the call."""
    digest = sha256_hex(content)
    call.content_hash, call.bytes = digest, len(content)
    call.storage_uri = storage.put(f"sec/{kind}/{scope}/{digest}.json", content)
    return digest


# ---------------------------------------------------------------------------- ticker to CIK


def _load_ticker_map(
    run: PipelineRun, day: date, client: SecClient, storage: RawStorage
) -> dict[str, str]:
    """company_tickers.json, fetched once for the day and then read from the stored copy."""
    cached = (
        SourceCall.objects.filter(
            feed=SourceCall.Feed.SEC_TICKERS,
            status=SourceCall.Status.OK,
            scope_key=str(day),
        )
        .exclude(storage_uri="")
        .order_by("-requested_at")
        .first()
    )
    if cached is not None:
        try:
            return parse_company_tickers(storage.get(cached.storage_uri))
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("The cached ticker file for %s is unusable; fetching again.", day)
    call = _open_call(run, SourceCall.Feed.SEC_TICKERS, str(day))
    try:
        response = client.get(TICKERS_URL)
        mapping = parse_company_tickers(response.content)
    except SecFetchError as exc:
        _close_call(
            call,
            SourceCall.Status.FAILED,
            reason=exc.reason_code,
            error=str(exc),
            attempts=exc.attempts,
            http_status=exc.http_status,
        )
        raise SecCollectionFailed(run, call, exc.reason_code, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        _close_call(call, SourceCall.Status.FAILED, reason="parse_error", error=str(exc))
        raise SecCollectionFailed(run, call, "parse_error", str(exc)) from exc
    _keep(storage, call, "company_tickers", str(day), response.content)
    _close_call(call, SourceCall.Status.OK, response=response)
    return mapping


# ------------------------------------------------------------------------- per-company work


def _store_filings(
    company: Company, entries: list[SubmissionEntry], call: SourceCall
) -> list[SubmissionEntry]:
    """Store the new 10-K, 10-Q, 20-F and 6-K filings (and amendments). Returns the new ones."""
    wanted = [e for e in entries if is_stored_filing_form(e.form)]
    known = set(
        SecFiling.objects.filter(accession__in=[e.accession for e in wanted]).values_list(
            "accession", flat=True
        )
    )
    fresh = [e for e in wanted if e.accession not in known]
    SecFiling.objects.bulk_create(
        [
            SecFiling(
                company=company,
                accession=e.accession,
                form=e.form,
                filed_at=e.filed_at,
                period_end=e.period_end,
                is_amendment=e.is_amendment,
                source_call=call,
            )
            for e in fresh
        ],
        batch_size=BATCH_SIZE,
        ignore_conflicts=True,
    )
    return fresh


def _new_form4(
    company: Company, cik: str, entries: list[SubmissionEntry], since: date
) -> list[Form4Entry]:
    """Form 4 and 4/A entries filed since ``since`` that the Form 4 collector has not stored."""
    wanted = [e for e in entries if is_form4(e.form) and e.filed_at >= since]
    known = set(
        SecForm4Filing.objects.filter(accession__in=[e.accession for e in wanted]).values_list(
            "accession", flat=True
        )
    )
    return [
        Form4Entry(
            company.pk,
            cik,
            e.accession,
            e.form,
            e.filed_at,
            e.period_end,
            e.primary_document,
        )
        for e in wanted
        if e.accession not in known
    ]


def _last_facts_call(cik: str) -> SourceCall | None:
    return (
        SourceCall.objects.filter(feed=SourceCall.Feed.SEC_COMPANYFACTS, scope_key=cik)
        .order_by("-requested_at", "-pk")
        .first()
    )


def _needs_facts(company: Company, cik: str, new_filings: bool) -> bool:
    if new_filings:
        return True
    last = _last_facts_call(cik)
    if last is None or last.status == SourceCall.Status.FAILED:
        return True
    if last.reason_code in TERMINAL_FACT_REASONS:
        return False
    return not SecFact.objects.filter(company=company).exists()


def _insert_facts(company: Company, rows: list[FactRow], call: SourceCall) -> int:
    before = SecFact.objects.filter(company=company).count()
    SecFact.objects.bulk_create(
        [
            SecFact(
                company=company,
                taxonomy=r.taxonomy,
                tag=r.tag,
                unit=r.unit,
                period_start=r.period_start,
                period_end=r.period_end,
                value=r.value,
                accession=r.accession,
                form=r.form,
                fy=r.fy,
                fp=r.fp,
                filed_at=r.filed_at,
                source_call=call,
            )
            for r in rows
        ],
        batch_size=BATCH_SIZE,
        ignore_conflicts=True,
    )
    return SecFact.objects.filter(company=company).count() - before


def _refresh_facts(
    run: PipelineRun,
    company: Company,
    ticker: str,
    cik: str,
    client: SecClient,
    storage: RawStorage,
    result: CollectionResult,
) -> None:
    call = _open_call(run, SourceCall.Feed.SEC_COMPANYFACTS, cik)
    result.facts_refreshed += 1
    try:
        response = client.get(FACTS_URL.format(cik=cik))
    except SecFetchError as exc:
        if exc.http_status == 404:  # the SEC has no facts for this filer
            _close_call(
                call,
                SourceCall.Status.SKIPPED,
                reason="no_companyfacts",
                attempts=exc.attempts,
                http_status=404,
            )
            result.skipped_facts[ticker] = "no_companyfacts"
            return
        _close_call(
            call,
            SourceCall.Status.FAILED,
            reason=exc.reason_code,
            error=str(exc),
            attempts=exc.attempts,
            http_status=exc.http_status,
        )
        result.failures[ticker] = exc.reason_code
        return
    previous = (
        SourceCall.objects.filter(
            feed=SourceCall.Feed.SEC_COMPANYFACTS,
            scope_key=cik,
            status__in=[SourceCall.Status.OK, SourceCall.Status.SKIPPED],
        )
        .exclude(content_hash="")
        .exclude(pk=call.pk)
        .order_by("-requested_at", "-pk")
        .first()
    )
    digest = _keep(storage, call, "companyfacts", cik, response.content)
    if previous is not None and previous.content_hash == digest:
        _close_call(call, SourceCall.Status.SKIPPED, response=response)
        result.unchanged += 1
        return
    parsed = parse_companyfacts(response.content)
    if parsed.reason_code is not None:
        _close_call(call, SourceCall.Status.SKIPPED, reason=parsed.reason_code, response=response)
        result.skipped_facts[ticker] = parsed.reason_code
        return
    with transaction.atomic():
        result.facts_added += _insert_facts(company, parsed.rows, call)
        _close_call(call, SourceCall.Status.OK, response=response)


def _process_company(
    run: PipelineRun,
    form4_since: date,
    company: Company,
    ticker: str,
    cik: str,
    client: SecClient,
    storage: RawStorage,
    result: CollectionResult,
) -> None:
    call = _open_call(run, SourceCall.Feed.SEC_SUBMISSIONS, cik)
    try:
        response = client.get(SUBMISSIONS_URL.format(cik=cik))
    except SecFetchError as exc:
        _close_call(
            call,
            SourceCall.Status.FAILED,
            reason=exc.reason_code,
            error=str(exc),
            attempts=exc.attempts,
            http_status=exc.http_status,
        )
        result.failures[ticker] = exc.reason_code
        return
    _keep(storage, call, "submissions", cik, response.content)
    entries = parse_submissions(response.content)
    with transaction.atomic():
        fresh = _store_filings(company, entries, call)
        _close_call(call, SourceCall.Status.OK, response=response)
    result.filings_added += len(fresh)
    result.form4_entries.extend(_new_form4(company, cik, entries, form4_since))
    if _needs_facts(company, cik, bool(fresh)):
        _refresh_facts(run, company, ticker, cik, client, storage, result)


def _fail_stage(
    run: PipelineRun, call: SourceCall, steps: StepRecorder, reason: str, message: str
) -> SecCollectionFailed:
    DataQualityCheck.objects.create(
        run=run,
        check_name="sec_stage",
        scope="sec",
        expected="a usable SEC connection",
        observed=reason,
        status=DataQualityCheck.Status.FAIL,
        detail={"message": message},
    )
    run.status, run.finished_at = PipelineRun.Status.FAILED, _now()
    run.save(update_fields=["status", "finished_at"])
    logger.error("SEC stage stopped (%s): %s", reason, message)
    return SecCollectionFailed(run, call, reason, message)


# --------------------------------------------------------------------------------- the step


def collect_sec(
    as_of_date: date,
    *,
    trigger: str = PipelineRun.Trigger.MANUAL,
    tickers: list[str] | None = None,
    client: SecClient | None = None,
    storage: RawStorage | None = None,
    form4_handler: Form4Handler | None = None,
) -> CollectionResult:
    """Run the SEC step for ``as_of_date`` over the companies that have a current ticker.

    ``tickers`` limits the run to those tickers. Raises ``SecCollectionFailed`` only when the
    stage cannot start; a company that fails is recorded and skipped.
    """
    storage = storage or get_raw_storage()
    run = PipelineRun.objects.create(
        stage=PipelineRun.Stage.FILINGS,
        as_of_date=as_of_date,
        trigger=trigger,
        started_at=_now(),
    )
    result = CollectionResult(run)
    steps = StepRecorder(run)

    tick = time.time()
    try:
        client = client or SecClient()
    except SecUserAgentMissing as exc:
        call = _open_call(run, SourceCall.Feed.SEC_TICKERS, str(as_of_date))
        _close_call(call, SourceCall.Status.FAILED, reason="fetch_error", error=str(exc))
        steps.record("ticker_map", tick, "failed", error=str(exc))
        raise _fail_stage(run, call, steps, "fetch_error", str(exc)) from exc
    try:
        ticker_map = _load_ticker_map(run, as_of_date, client, storage)
    except SecCollectionFailed as exc:
        steps.record("ticker_map", tick, "failed", error=str(exc))
        raise _fail_stage(run, exc.call, steps, exc.reason_code, str(exc)) from exc
    steps.record("ticker_map", tick, rows_out=len(ticker_map))

    open_tickers = TickerHistory.objects.select_related("company").filter(
        valid_to__isnull=True, company__status=Company.Status.ACTIVE
    )
    if tickers:
        open_tickers = open_tickers.filter(ticker__in=[t.upper() for t in tickers])
    universe = list(open_tickers.order_by("company_id"))
    result.companies = len(universe)
    form4_since = as_of_date - timedelta(days=FORM4_WINDOW_DAYS)

    tick = time.time()
    for row in universe:
        company, ticker = row.company, row.ticker
        cik = ticker_map.get(ticker.upper())
        if cik is None:
            skipped = _open_call(run, SourceCall.Feed.SEC_TICKERS, ticker)
            _close_call(skipped, SourceCall.Status.SKIPPED, reason="no_cik")
            result.no_cik.append(ticker)
            continue
        try:
            if company.cik != cik:
                company.cik = cik
                company.save(update_fields=["cik"])
            _process_company(run, form4_since, company, ticker, cik, client, storage, result)
        except Exception as exc:  # one company never stops the run
            logger.exception("SEC collection failed for %s", ticker)
            SourceCall.objects.filter(run=run, status=SourceCall.Status.PARTIAL).update(
                status=SourceCall.Status.FAILED,
                reason_code="parse_error",
                error_text=f"{type(exc).__name__}: {exc}"[:1000],
                completed_at=_now(),
            )
            result.failures[ticker] = "parse_error"
    steps.record(
        "companies",
        tick,
        "failed" if result.failures else "ok",
        rows_in=len(universe),
        rows_out=len(universe) - len(result.failures) - len(result.no_cik),
    )

    DataQualityCheck.objects.create(
        run=run,
        check_name="sec_company_failures",
        scope="sec",
        expected="0",
        observed=str(len(result.failures)),
        status=DataQualityCheck.Status.WARN if result.failures else DataQualityCheck.Status.PASS,
        detail={"failures": dict(list(result.failures.items())[:MAX_FAILURES_IN_CHECK])},
    )
    run.status = PipelineRun.Status.PARTIAL if result.failures else PipelineRun.Status.OK
    run.finished_at = _now()
    run.save(update_fields=["status", "finished_at"])

    if result.form4_entries and form4_handler is not None:
        try:
            form4_handler(result.form4_entries)
        except Exception:  # the hand-off must not undo a finished SEC step
            logger.exception("The Form 4 hand-off failed")
    for ticker, reason in result.failures.items():
        logger.error("SEC collection failed for %s: %s", ticker, reason)
    return result
