"""Collect ClinicalTrials.gov studies for the company universe (Step 13).

Each active sponsor alias is queried against the v2 API (all pages, all statuses). A returned study
belongs to a company when its lead sponsor matches one of that company's aliases at or above the
threshold; a manual override with the same name always matches. The study is stored as a new
``CtStudyVersion`` only when its content differs from the company's last stored version.

The politeness delay and the three attempts come from ``SecClient`` (it only needs a URL and a
User-Agent), with a limiter of its own so ClinicalTrials.gov requests are not slowed by, or
counted with, the SEC's.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from apps.companies.models import Company, CompanySponsorAlias, TickerHistory
from apps.marketdata.storage import RawStorage, get_raw_storage, sha256_hex
from apps.platform.models import PipelineRun, SourceCall
from apps.platform.settings_store import get_setting
from apps.platform.steps import StepRecorder
from apps.sec.client import SecClient, SecFetchError, SecLimiter
from apps.trials.ctgov import (
    DEFAULT_THRESHOLD,
    ParsedStudy,
    derive_alias,
    keep_study,
    match_sponsor,
    parse_studies,
    query_url,
)
from apps.trials.models import CtStudyVersion

DEFAULT_DELAY_SECONDS = 0.5
USER_AGENT = "oxhouse-research/0.1"


@dataclass
class TrialsResult:
    run: PipelineRun
    companies: int = 0
    queries: int = 0
    studies_seen: int = 0
    versions_added: int = 0
    unchanged: int = 0
    dropped: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    failures: dict[str, str] = field(default_factory=dict)  # alias -> reason code
    shared_skipped: int = 0


def default_client(delay: float | None = None) -> SecClient:
    seconds = get_setting("ctgov_delay_seconds", DEFAULT_DELAY_SECONDS) if delay is None else delay
    return SecClient(user_agent=USER_AGENT, limiter=SecLimiter(min_interval=float(seconds)))


def ensure_aliases(company: Company) -> list[CompanySponsorAlias]:
    """The active aliases of the company, deriving one from its name when it has none.

    A derived alias that no longer equals what the company name gives (the rule changed, or the
    name did) is switched off and replaced. Manual and SEC aliases are never touched.
    """
    wanted = derive_alias(company.name)
    derived = list(company.sponsor_aliases.filter(source=CompanySponsorAlias.Source.DERIVED))
    for alias in derived:
        if alias.active and alias.sponsor_name != wanted:
            alias.active = False
            alias.save(update_fields=["active"])
    aliases = list(company.sponsor_aliases.filter(active=True))
    if not aliases:
        alias, _ = CompanySponsorAlias.objects.get_or_create(
            company=company, sponsor_name=wanted, source=CompanySponsorAlias.Source.DERIVED
        )
        if not alias.active:
            alias.active = True
            alias.save(update_fields=["active"])
        aliases = [alias]
    return aliases


def _content_hash(study: ParsedStudy) -> str:
    text = json.dumps(study.content(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _fetch_alias(
    run: PipelineRun, alias: str, client: SecClient, storage: RawStorage
) -> list[ParsedStudy]:
    """Every page for one alias. Records one source call; raises SecFetchError on failure."""
    call = SourceCall.objects.create(
        run=run,
        feed=SourceCall.Feed.CTGOV,
        scope_key=alias[:128],
        requested_at=datetime.now(UTC),
        status=SourceCall.Status.PARTIAL,
    )
    studies: list[ParsedStudy] = []
    token: str | None = None
    pages, size, attempts = 0, 0, 0
    digest = hashlib.sha256()
    try:
        while True:
            got = client.get(query_url(alias, token))
            attempts = max(attempts, got.attempts)
            batch, token = parse_studies(got.content)
            studies += batch
            pages += 1
            size += len(got.content)
            digest.update(got.content)
            if pages == 1 and token is None:
                call.storage_uri = storage.put(
                    f"ctgov/{sha256_hex(alias.encode())[:16]}/{sha256_hex(got.content)}.json",
                    got.content,
                )
            if token is None:
                break
    except SecFetchError as exc:
        call.status, call.reason_code = SourceCall.Status.FAILED, exc.reason_code
        call.http_status, call.attempts, call.error_text = exc.http_status, exc.attempts, str(exc)
        call.completed_at = datetime.now(UTC)
        call.save()
        raise
    except (ValueError, KeyError, TypeError) as exc:
        call.status, call.reason_code = SourceCall.Status.FAILED, "parse_error"
        call.error_text = f"{type(exc).__name__}: {exc}"[:500]
        call.completed_at = datetime.now(UTC)
        call.save()
        raise SecFetchError("parse_error", attempts or 1, str(exc)) from exc
    call.status, call.http_status, call.attempts = SourceCall.Status.OK, 200, attempts
    call.bytes, call.content_hash = size, digest.hexdigest()
    call.completed_at = datetime.now(UTC)
    call.save()
    return studies


def collect_trials(
    as_of_date: date,
    *,
    trigger: str = PipelineRun.Trigger.MANUAL,
    tickers: list[str] | None = None,
    client: SecClient | None = None,
    storage: RawStorage | None = None,
) -> TrialsResult:
    run = PipelineRun.objects.create(
        stage=PipelineRun.Stage.FILINGS,
        as_of_date=as_of_date,
        trigger=trigger,
        started_at=datetime.now(UTC),
    )
    result = TrialsResult(run)
    steps = StepRecorder(run)
    client = client or default_client()
    storage = storage or get_raw_storage()
    threshold = float(get_setting("ctgov_match_threshold", DEFAULT_THRESHOLD))

    open_tickers = TickerHistory.objects.select_related("company").filter(
        valid_to__isnull=True, company__status=Company.Status.ACTIVE
    )
    if tickers:
        open_tickers = open_tickers.filter(ticker__in=[t.upper() for t in tickers])
    companies = [t.company for t in open_tickers.order_by("company_id")]
    result.companies = len(companies)

    started = time.time()
    aliases: dict[int, list[CompanySponsorAlias]] = {c.pk: ensure_aliases(c) for c in companies}
    names = sorted({a.sponsor_name for rows in aliases.values() for a in rows})
    fetched: dict[str, list[ParsedStudy]] = {}
    for name in names:
        result.queries += 1
        try:
            fetched[name] = _fetch_alias(run, name, client, storage)
        except SecFetchError as exc:
            result.failures[name] = exc.reason_code
    steps.record(
        "fetch",
        started,
        "ok" if not result.failures else "partial",
        rows_in=len(names),
        rows_out=len(fetched),
    )

    started = time.time()
    for company in companies:
        pairs = [(a.sponsor_name, a.is_override) for a in aliases[company.pk]]
        seen: dict[str, tuple[ParsedStudy, float]] = {}
        for alias in aliases[company.pk]:
            for study in fetched.get(alias.sponsor_name, []):
                score = match_sponsor(study.sponsor_name, pairs, threshold)
                if score is not None and study.nct_id not in seen:
                    seen[study.nct_id] = (study, score)
        result.studies_seen += len(seen)
        _store(company, seen, as_of_date, result)
    steps.record("store", started, rows_in=result.studies_seen, rows_out=result.versions_added)

    run.status = PipelineRun.Status.PARTIAL if result.failures else PipelineRun.Status.OK
    run.finished_at = datetime.now(UTC)
    run.save(update_fields=["status", "finished_at"])
    return result


def _store(
    company: Company,
    matched: dict[str, tuple[ParsedStudy, float]],
    day: date,
    result: TrialsResult,
) -> None:
    if not matched:
        return
    latest: dict[str, CtStudyVersion] = {}
    for version in CtStudyVersion.objects.filter(
        company=company, nct_id__in=list(matched)
    ).order_by("observed_on", "id"):
        latest[version.nct_id] = version
    new: list[CtStudyVersion] = []
    for nct_id, (study, score) in matched.items():
        keep, reason = keep_study(study, day)
        if not keep:
            result.dropped[reason] += 1
            continue
        digest = _content_hash(study)
        previous = latest.get(nct_id)
        if previous is not None and previous.content_hash == digest:
            result.unchanged += 1
            continue
        existing = CtStudyVersion.objects.filter(nct_id=nct_id, content_hash=digest).first()
        if existing is not None:
            if existing.company_id == company.pk:  # content reverted to an earlier version
                existing.observed_on = day
                existing.save(update_fields=["observed_on"])
                result.versions_added += 1
            else:  # the same study already stored for another company (one row per content)
                result.shared_skipped += 1
            continue
        new.append(_version(company, study, score, day, digest))
    CtStudyVersion.objects.bulk_create(new)
    result.versions_added += len(new)


def _version(
    company: Company, study: ParsedStudy, score: float, day: date, digest: str
) -> CtStudyVersion:
    return CtStudyVersion(
        nct_id=study.nct_id,
        company=company,
        sponsor_name=study.sponsor_name[:255],
        match_score=score,
        observed_on=day,
        content_hash=digest,
        phases=study.phases,
        highest_phase=study.highest_phase,
        status=study.status,
        study_type=study.study_type,
        primary_completion_raw=study.primary_completion.raw,
        primary_completion_date=study.primary_completion.value,
        primary_completion_precision=study.primary_completion.precision,
        completion_raw=study.completion.raw,
        completion_date=study.completion.value,
        completion_precision=study.completion.precision,
        last_update_posted=study.last_update_posted,
        title=study.title,
        conditions=study.conditions,
        interventions=study.interventions,
    )
