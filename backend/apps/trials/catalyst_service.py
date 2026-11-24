"""Derive and store the catalyst signal for the whole universe on a day (Step 14).

Reads each company's latest stored study versions on or before the day, applies rule v0 and
writes one ``CatalystSignal`` per company and day (replaced when run again). The runway
financing date, when there is one, makes ``financing`` the next event for companies with no
clinical date.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime

from apps.companies.models import Company, TickerHistory
from apps.platform.models import PipelineRun
from apps.platform.steps import StepRecorder
from apps.sec.models import RunwayCalc
from apps.trials.catalyst import CatalystResult, derive_catalyst
from apps.trials.display import Study
from apps.trials.models import CatalystSignal, CtStudyVersion

_FIELDS = (
    "rule_version",
    "clinical_date",
    "clinical_phase",
    "clinical_overdue",
    "clinical_source_nct",
    "fallback_end_raw",
    "fallback_end_nct",
    "active_phase_2",
    "active_phase_3",
    "total_studies",
    "latest_trial_nct",
    "latest_trial_status",
    "financing_date",
    "next_event_source",
    "candidates",
    "status",
    "reason_codes",
)


@dataclass
class CatalystRunResult:
    run: PipelineRun
    companies: int
    by_source: dict[str, int]


def _studies_by_company(company_ids: list[int], day: date) -> dict[int, list[Study]]:
    latest: dict[tuple[int, str], CtStudyVersion] = {}
    versions = CtStudyVersion.objects.filter(
        company_id__in=company_ids, observed_on__lte=day
    ).order_by("observed_on", "id")
    for version in versions:
        latest[(version.company_id, version.nct_id)] = version
    grouped: dict[int, list[Study]] = {}
    for (company_id, _), version in sorted(latest.items()):
        grouped.setdefault(company_id, []).append(Study.from_version(version))
    return grouped


def _financing_dates(company_ids: list[int], day: date) -> dict[int, date]:
    """The financing date of each company's newest runway calculation on or before the day."""
    found: dict[int, date] = {}
    rows = (
        RunwayCalc.objects.filter(
            company_id__in=company_ids, calc_date__lte=day, financing_date__isnull=False
        )
        .order_by("calc_date")
        .values_list("company_id", "financing_date")
    )
    for company_id, financing in rows:
        if financing is not None:
            found[company_id] = financing
    return found


def derive_universe(
    day: date, *, trigger: str = PipelineRun.Trigger.MANUAL, tickers: list[str] | None = None
) -> CatalystRunResult:
    run = PipelineRun.objects.create(
        stage=PipelineRun.Stage.FILINGS,
        as_of_date=day,
        trigger=trigger,
        started_at=datetime.now(UTC),
    )
    steps = StepRecorder(run)
    started = time.time()
    open_tickers = TickerHistory.objects.select_related("company").filter(
        valid_to__isnull=True, company__status=Company.Status.ACTIVE
    )
    if tickers:
        open_tickers = open_tickers.filter(ticker__in=[t.upper() for t in tickers])
    companies = [t.company for t in open_tickers.order_by("company_id")]
    ids = [c.pk for c in companies]
    studies = _studies_by_company(ids, day)
    financing = _financing_dates(ids, day)

    by_source: dict[str, int] = {}
    rows: list[CatalystSignal] = []
    for company in companies:
        result: CatalystResult = derive_catalyst(
            studies.get(company.pk, []), day, runway_financing_date=financing.get(company.pk)
        )
        by_source[result.next_event_source] = by_source.get(result.next_event_source, 0) + 1
        rows.append(
            CatalystSignal(
                company=company,
                as_of_date=day,
                **{name: getattr(result, name) for name in _FIELDS},
            )
        )
    CatalystSignal.objects.bulk_create(
        rows,
        batch_size=500,
        update_conflicts=True,
        unique_fields=["company", "as_of_date"],
        update_fields=[*_FIELDS, "carried_forward", "carried_from_date"],
    )
    steps.record("catalyst", started, rows_in=len(rows), rows_out=len(rows))
    run.status = PipelineRun.Status.OK
    run.finished_at = datetime.now(UTC)
    run.save(update_fields=["status", "finished_at"])
    return CatalystRunResult(run, len(rows), by_source)
