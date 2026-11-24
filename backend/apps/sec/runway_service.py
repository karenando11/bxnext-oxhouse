"""Calculate and store runway for the whole universe on a day (Step 12).

Facts are read as they stood on the day (``filed_at`` on or before it). Trial burn counts the
latest observed version of each study; until ClinicalTrials.gov is collected (Step 13) there are
no studies and trial burn is empty.

The calculation is repeatable: one row per company and day, replaced when run again.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

from apps.companies.models import Company, TickerHistory
from apps.platform.models import PipelineRun, SourceCall
from apps.platform.settings_store import get_setting
from apps.platform.steps import StepRecorder
from apps.scoring.models import FormulaVersion
from apps.sec.models import RunwayCalc, SecFact
from apps.sec.runway import (
    DEFAULT_TRIAL_RATES,
    METHOD_VERSION,
    Fact,
    RunwayResult,
    calculate_runway,
)
from apps.trials.models import CtStudyVersion

DEFAULT_ACTIVE_STATUSES = ["RECRUITING"]
_DIGITS = re.compile(r"[1-4]")

_RESULT_FIELDS = (
    "status",
    "reason_codes",
    "cash_usd",
    "cash_tag",
    "cash_end_date",
    "ocf_usd",
    "ocf_tag",
    "ocf_period_start",
    "ocf_period_end",
    "operating_burn_monthly_usd",
    "trial_burn_monthly_usd",
    "burn_total_monthly_usd",
    "burn_source",
    "runway_months",
    "runway_capped",
    "cap_reason",
    "financing_date",
    "source_fact_ids",
    "trial_burn_lines",
    "notes",
)


@dataclass
class RunwayRunResult:
    run: PipelineRun
    companies: int
    by_status: dict[str, int]
    runway_months_present: int


def phase_key(highest_phase: str) -> str:
    """phase1, phase2, phase3 or other. A multi-phase study is rated at its highest phase."""
    digits = [int(d) for d in _DIGITS.findall(highest_phase or "")]
    top = max(digits) if digits else 0
    return f"phase{top}" if 1 <= top <= 3 else "other"


def active_trial_counts(
    company_ids: list[int], day: date, statuses: list[str]
) -> dict[int, dict[str, int]]:
    """Active studies per company and phase, from each study's latest version on the day."""
    latest: dict[tuple[int, str], CtStudyVersion] = {}
    versions = CtStudyVersion.objects.filter(
        company_id__in=company_ids, observed_on__lte=day
    ).order_by("observed_on", "id")
    for version in versions:
        latest[(version.company_id, version.nct_id)] = version
    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (company_id, _), version in latest.items():
        if version.status in statuses:
            counts[company_id][phase_key(version.highest_phase)] += 1
    return {k: dict(v) for k, v in counts.items()}


def _facts_by_company(company_ids: list[int], day: date) -> dict[int, list[Fact]]:
    rows = SecFact.objects.filter(
        company_id__in=company_ids, unit="USD", filed_at__lte=day
    ).values_list(
        "id",
        "company_id",
        "taxonomy",
        "tag",
        "period_start",
        "period_end",
        "value",
        "form",
        "filed_at",
        "accession",
    )
    facts: dict[int, list[Fact]] = defaultdict(list)
    for id_, company_id, taxonomy, tag, start, end, value, form, filed, accession in rows:
        facts[company_id].append(
            Fact(id_, taxonomy, tag, start, end, value, form, filed, accession)
        )
    return facts


def _reason_without_facts(company: Company) -> str | None:
    if not company.cik:
        return "no_cik"
    call = (
        SourceCall.objects.filter(
            feed=SourceCall.Feed.SEC_COMPANYFACTS, scope_key=company.cik, reason_code__isnull=False
        )
        .order_by("-requested_at")
        .first()
    )
    if call is not None and call.reason_code in ("no_companyfacts", "non_usd_only"):
        return call.reason_code
    return None


def _quirk(name: str, default: bool) -> bool:
    quirks = (
        FormulaVersion.objects.filter(name="parity")
        .order_by("-version")
        .values_list("quirks", flat=True)
        .first()
    )
    return bool((quirks or {}).get(name, default))


def calculate_universe(
    day: date, *, trigger: str = PipelineRun.Trigger.MANUAL, tickers: list[str] | None = None
) -> RunwayRunResult:
    """Calculate runway for each active company and store one ``RunwayCalc`` per company."""
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
    company_list = [t.company for t in open_tickers.order_by("company_id")]
    ids = [c.pk for c in company_list]

    rates = {**DEFAULT_TRIAL_RATES, **(get_setting("runway_trial_rates_usd", {}) or {})}
    statuses = get_setting("runway_active_trial_statuses", DEFAULT_ACTIVE_STATUSES)
    naive = _quirk("ocf_ytd_naive_sum", True)
    on_profitable = _quirk("trial_burn_on_profitable", True)
    facts = _facts_by_company(ids, day)
    trials = active_trial_counts(ids, day, statuses)

    by_status: dict[str, int] = defaultdict(int)
    rows: list[RunwayCalc] = []
    for company in company_list:
        company_facts = facts.get(company.pk, [])
        result: RunwayResult = calculate_runway(
            company_facts,
            trials.get(company.pk),
            rates=rates,
            ocf_ytd_naive_sum=naive,
            trial_burn_on_profitable=on_profitable,
            reason_without_facts=None if company_facts else _reason_without_facts(company),
        )
        by_status[result.status] += 1
        rows.append(
            RunwayCalc(
                company=company,
                calc_date=day,
                method_version=METHOD_VERSION,
                **{name: getattr(result, name) for name in _RESULT_FIELDS},
            )
        )
    RunwayCalc.objects.bulk_create(
        rows,
        batch_size=500,
        update_conflicts=True,
        unique_fields=["company", "calc_date"],
        update_fields=["method_version", *_RESULT_FIELDS, "carried_forward", "carried_from_date"],
    )
    present = sum(1 for r in rows if r.runway_months is not None)
    steps.record("runway", started, rows_in=len(rows), rows_out=present)
    run.status = PipelineRun.Status.OK
    run.finished_at = datetime.now(UTC)
    run.save(update_fields=["status", "finished_at"])
    return RunwayRunResult(run, len(rows), dict(by_status), present)
