"""The Form 4 step of the filings stage: fetch, parse and store insider filings.

The SEC collector (Step 11) finds new Form 4 and 4/A entries in each company's submissions feed
and hands them over. Entries older than 90 days are ignored; the rest are fetched one document at
a time through ``SecClient``, so they share the one limiter with every other SEC request, and are
stored with their non-derivative transactions.

* A document that cannot be parsed is stored with ``parse_status`` ``failed`` and its reason, so
  it is not fetched again, never counts in a metric, and is reported by a data-quality check.
* A document that cannot be fetched stores nothing and is offered again by the next SEC run.
* A 4/A is linked to the filing it replaces (same issuer, same period of report, a shared
  reporting owner, filed on or before the amendment), so the metric view counts only the
  amendment. The XML does not name the original, so this is matched, not read.
* A document whose issuer is a different company (the company appeared as a reporting owner) is
  not an insider filing for this company. Nothing is stored for it: the filing's real issuer lists
  the same accession and must be able to store it. It is recorded as a skipped call and fetched
  again on later runs until it is older than 90 days.

One failing document never stops the others and nothing here blocks scoring. The metrics are the
``insider_metric_daily`` view and ``insider_metric_at`` function from Step 5.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from django.db import transaction

from apps.marketdata.storage import RawStorage, get_raw_storage, sha256_hex
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from apps.platform.steps import StepRecorder
from apps.sec.client import SecClient, SecFetchError, SecUserAgentMissing
from apps.sec.collector import Form4Entry
from apps.sec.form4 import Form4ParseError, ParsedForm4, parse_form4
from apps.sec.models import SecForm4Filing, SecForm4Transaction
from apps.sec.parsing import FORM4_WINDOW_DAYS
from apps.sec.urls import form4_document_url

logger = logging.getLogger(__name__)

WINDOW_DAYS = FORM4_WINDOW_DAYS
NOT_THE_ISSUER = "not_the_issuer"
MAX_ACCESSIONS_IN_CHECK = 50


@dataclass
class Form4Result:
    run: PipelineRun
    offered: int = 0
    too_old: int = 0
    already_stored: int = 0
    stored: int = 0
    transactions: int = 0
    parse_failures: dict[str, str] = field(default_factory=dict)  # accession -> reason
    not_issuer: list[str] = field(default_factory=list)
    fetch_failures: dict[str, str] = field(default_factory=dict)  # accession -> reason code
    amendments_linked: int = 0


def document_url(entry: Form4Entry) -> str:
    return form4_document_url(entry.cik, entry.accession, entry.primary_document)


def _now() -> datetime:
    return datetime.now(UTC)


def _find_original(entry: Form4Entry, parsed: ParsedForm4) -> str:
    """The accession a 4/A replaces, or an empty string when it cannot be found."""
    owners = [o.cik for o in parsed.owners]
    candidates = SecForm4Filing.objects.filter(
        company_id=entry.company_id,
        parse_status=SecForm4Filing.ParseStatus.OK,
        period_of_report=parsed.period_of_report,
        filed_at__lte=entry.filed_at,
        transactions__reporting_owner_cik__in=owners,
    ).exclude(accession=entry.accession)
    if parsed.original_submission_date is not None:
        same_day = candidates.filter(filed_at=parsed.original_submission_date)
        if same_day.exists():
            candidates = same_day
    found = candidates.order_by("-filed_at", "-pk").first()
    return found.accession if found else ""


def _store(
    entry: Form4Entry, parsed: ParsedForm4, call: SourceCall, result: Form4Result
) -> SecForm4Filing:
    amends = _find_original(entry, parsed) if entry.form == "4/A" else ""
    owner = parsed.owners[0]
    with transaction.atomic():
        filing = SecForm4Filing.objects.create(
            company_id=entry.company_id,
            accession=entry.accession,
            form=entry.form,
            filed_at=entry.filed_at,
            period_of_report=parsed.period_of_report,
            primary_document=entry.primary_document[:255],
            is_10b5_1=parsed.is_10b5_1,
            amends_accession=amends,
            parse_status=SecForm4Filing.ParseStatus.OK,
            source_call=call,
        )
        SecForm4Transaction.objects.bulk_create(
            [
                SecForm4Transaction(
                    filing=filing,
                    line_no=t.line_no,
                    reporting_owner_cik=owner.cik,
                    reporting_owner_name=owner.name[:255],
                    is_officer=owner.is_officer,
                    is_director=owner.is_director,
                    is_ten_percent_owner=owner.is_ten_percent_owner,
                    officer_title=owner.officer_title[:255],
                    transaction_date=t.transaction_date,
                    transaction_code=t.code[:2],
                    shares=t.shares,
                    price_per_share=t.price_per_share,
                    acquired_or_disposed=t.acquired_or_disposed[:1],
                    amount_usd=t.amount_usd,
                )
                for t in parsed.transactions
            ]
        )
    result.transactions += len(parsed.transactions)
    result.amendments_linked += 1 if amends else 0
    return filing


def _store_failed(entry: Form4Entry, reason: str, call: SourceCall) -> None:
    SecForm4Filing.objects.create(
        company_id=entry.company_id,
        accession=entry.accession,
        form=entry.form,
        filed_at=entry.filed_at,
        period_of_report=entry.period_of_report,
        primary_document=entry.primary_document[:255],
        parse_status=SecForm4Filing.ParseStatus.FAILED,
        parse_reason=reason[:255],
        source_call=call,
    )


def _close(
    call: SourceCall, status: str, reason: str | None = None, error: str = "", attempts: int = 1
) -> None:
    call.status, call.reason_code, call.error_text = status, reason, error
    call.attempts = attempts
    call.completed_at = _now()
    call.save()


def _process(
    run: PipelineRun,
    entry: Form4Entry,
    client: SecClient,
    storage: RawStorage,
    result: Form4Result,
) -> None:
    call = SourceCall.objects.create(
        run=run,
        feed=SourceCall.Feed.SEC_FORM4,
        scope_key=entry.accession,
        requested_at=_now(),
        status=SourceCall.Status.PARTIAL,
    )
    try:
        response = client.get(document_url(entry))
    except SecFetchError as exc:
        call.http_status = exc.http_status
        _close(call, SourceCall.Status.FAILED, exc.reason_code, str(exc), exc.attempts)
        result.fetch_failures[entry.accession] = exc.reason_code
        return
    digest = sha256_hex(response.content)
    call.content_hash, call.bytes, call.http_status = digest, len(response.content), 200
    call.storage_uri = storage.put(f"sec/form4/{entry.accession}/{digest}.xml", response.content)
    try:
        parsed = parse_form4(response.content)
        if parsed.issuer_cik != entry.cik:
            raise Form4ParseError(
                f"{NOT_THE_ISSUER}: the issuer is {parsed.issuer_cik}, not {entry.cik}"
            )
    except Form4ParseError as exc:
        reason = str(exc)
        if reason.startswith(NOT_THE_ISSUER):
            # Nothing is stored: the accession belongs to the filing's real issuer, whose own
            # feed lists it too, and a placeholder here would block that company's row.
            _close(call, SourceCall.Status.SKIPPED, error=reason, attempts=response.attempts)
            result.not_issuer.append(entry.accession)
            return
        _store_failed(entry, reason, call)
        _close(call, SourceCall.Status.FAILED, "parse_error", reason, response.attempts)
        result.parse_failures[entry.accession] = reason
        return
    _store(entry, parsed, call, result)
    _close(call, SourceCall.Status.OK, attempts=response.attempts)
    result.stored += 1


def collect_form4(
    entries: list[Form4Entry],
    as_of_date: date,
    *,
    trigger: str = PipelineRun.Trigger.MANUAL,
    client: SecClient | None = None,
    storage: RawStorage | None = None,
) -> Form4Result:
    """Fetch and store the Form 4 documents behind ``entries``. Never raises for a bad document."""
    storage = storage or get_raw_storage()
    run = PipelineRun.objects.create(
        stage=PipelineRun.Stage.FILINGS,
        as_of_date=as_of_date,
        trigger=trigger,
        started_at=_now(),
    )
    result = Form4Result(run, offered=len(entries))
    steps = StepRecorder(run)
    tick = time.time()
    try:
        client = client or SecClient()
    except SecUserAgentMissing as exc:
        steps.record("fetch", tick, "failed", error=str(exc))
        _finish(run, result, failed=True, message=str(exc))
        return result

    cutoff = as_of_date - timedelta(days=WINDOW_DAYS)
    in_window = [e for e in entries if e.filed_at >= cutoff]
    result.too_old = len(entries) - len(in_window)
    stored = set(
        SecForm4Filing.objects.filter(accession__in=[e.accession for e in in_window]).values_list(
            "accession", flat=True
        )
    )
    todo = [e for e in in_window if e.accession not in stored]
    result.already_stored = len(in_window) - len(todo)
    # Originals before their amendments, so an amendment can find what it replaces.
    todo.sort(key=lambda e: (e.filed_at, e.form == "4/A", e.accession))

    for entry in todo:
        # The same filing is listed by every company it names. Once one row holds the
        # accession, the others have nothing to add.
        if SecForm4Filing.objects.filter(accession=entry.accession).exists():
            result.already_stored += 1
            continue
        try:
            _process(run, entry, client, storage, result)
        except Exception as exc:  # one document never stops the others
            logger.exception("Form 4 collection failed for %s", entry.accession)
            SourceCall.objects.filter(
                run=run, scope_key=entry.accession, status=SourceCall.Status.PARTIAL
            ).update(
                status=SourceCall.Status.FAILED,
                reason_code="parse_error",
                error_text=f"{type(exc).__name__}: {exc}"[:1000],
                completed_at=_now(),
            )
            result.fetch_failures[entry.accession] = "parse_error"
    failed = bool(result.fetch_failures)
    steps.record(
        "fetch_and_store",
        tick,
        "failed" if failed else "ok",
        rows_in=len(todo),
        rows_out=result.stored,
    )
    _finish(run, result, failed=failed)
    return result


def _finish(run: PipelineRun, result: Form4Result, *, failed: bool, message: str = "") -> None:
    if message:  # the stage could not start
        DataQualityCheck.objects.create(
            run=run,
            check_name="form4_stage",
            scope="sec",
            expected="a usable SEC connection",
            observed="fetch_error",
            status=DataQualityCheck.Status.FAIL,
            detail={"message": message},
        )
    else:
        DataQualityCheck.objects.create(
            run=run,
            check_name="form4_parse_failures",
            scope="sec",
            expected="0",
            observed=str(len(result.parse_failures)),
            status=DataQualityCheck.Status.WARN
            if result.parse_failures
            else DataQualityCheck.Status.PASS,
            detail={
                "failures": dict(list(result.parse_failures.items())[:MAX_ACCESSIONS_IN_CHECK]),
                "not_the_issuer": len(result.not_issuer),
            },
        )
    if message:
        run.status = PipelineRun.Status.FAILED
    elif failed or result.parse_failures:
        run.status = PipelineRun.Status.PARTIAL
    else:
        run.status = PipelineRun.Status.OK
    run.finished_at = _now()
    run.save(update_fields=["status", "finished_at"])
    for accession, reason in result.parse_failures.items():
        logger.error("Form 4 %s could not be parsed: %s", accession, reason)


class Form4Collector:
    """A ``form4_handler`` for ``collect_sec``: runs the Form 4 step on the entries handed over."""

    def __init__(
        self,
        as_of_date: date,
        *,
        client: SecClient | None = None,
        storage: RawStorage | None = None,
        trigger: str = PipelineRun.Trigger.MANUAL,
    ) -> None:
        self.as_of_date = as_of_date
        self.client = client
        self.storage = storage
        self.trigger = trigger
        self.result: Form4Result | None = None

    def __call__(self, entries: list[Form4Entry]) -> None:
        self.result = collect_form4(
            entries,
            self.as_of_date,
            trigger=self.trigger,
            client=self.client,
            storage=self.storage,
        )
