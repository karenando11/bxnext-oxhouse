"""T14.1 to T14.5: the clinical catalyst rule (v0), its stored signal and the scorer link."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from apps.trials.catalyst import RULE_VERSION, consider, derive_catalyst, milestone
from apps.trials.catalyst_service import derive_universe
from apps.trials.display import Study
from apps.trials.models import CatalystSignal
from tests import factories as f

TODAY = date(2026, 10, 12)


def s(
    nct: str = "NCT00000001",
    phase: str = "PHASE3",
    status: str = "RECRUITING",
    primary: date | None = None,
    primary_precision: str = "day",
    completion: date | None = None,
    completion_precision: str = "day",
    study_type: str = "INTERVENTIONAL",
    **kw: Any,
) -> Study:
    return Study(
        nct,
        highest_phase=phase,
        status=status,
        primary_completion=primary,
        primary_precision=primary_precision if primary else "",
        completion=completion,
        completion_precision=completion_precision if completion else "",
        study_type=study_type,
        **kw,
    )


def later(days: int) -> date:
    return TODAY + timedelta(days=days)


# --- T14.1 eligibility -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "eligible", "reason"),
    [
        ({"phase": "PHASE2"}, True, ""),
        ({"phase": "PHASE3"}, True, ""),
        ({"phase": "PHASE1"}, False, "phase_not_2_or_3"),
        ({"phase": "EARLY_PHASE1"}, False, "phase_not_2_or_3"),
        ({"phase": "PHASE4"}, False, "phase_not_2_or_3"),
        ({"phase": ""}, False, "phase_not_2_or_3"),
        ({"study_type": "OBSERVATIONAL"}, False, "not_interventional"),
        ({"status": "RECRUITING"}, True, ""),
        ({"status": "NOT_YET_RECRUITING"}, True, ""),
        ({"status": "ENROLLING_BY_INVITATION"}, True, ""),
        ({"status": "ACTIVE_NOT_RECRUITING"}, True, ""),
        ({"status": "COMPLETED"}, False, "status_not_active"),
        ({"status": "TERMINATED"}, False, "status_not_active"),
        ({"status": "WITHDRAWN"}, False, "status_not_active"),
        ({"status": "SUSPENDED"}, False, "status_not_active"),
        ({"status": "UNKNOWN"}, False, "status_not_active"),
    ],
)
def test_eligibility(kwargs: dict[str, Any], eligible: bool, reason: str) -> None:
    study = s(primary=later(100), **kwargs)
    candidate = consider(study, TODAY)
    assert candidate.eligible is eligible
    assert candidate.reject_reason == reason


def test_a_study_without_a_date_is_rejected() -> None:
    assert consider(s(), TODAY).reject_reason == "no_date"


def test_phase_1_2_counts_as_phase_2_and_phase_2_3_as_phase_3() -> None:
    # The collector stores the highest phase of a multi-phase study.
    result = derive_catalyst([s("A", "PHASE2", primary=later(50))], TODAY)
    assert result.clinical_phase == "phase_2"
    result = derive_catalyst([s("B", "PHASE3", primary=later(50))], TODAY)
    assert result.clinical_phase == "phase_3"


# --- Date selection --------------------------------------------------------------------------


def test_primary_date_beats_the_study_date() -> None:
    study = s(primary=later(30), completion=later(300))
    assert milestone(study) == (later(30), "day")
    only_end = s(completion=later(300))
    assert milestone(only_end) == (later(300), "day")


def test_month_only_dates_become_the_fifteenth() -> None:
    study = s(primary=date(2027, 6, 1), primary_precision="month")
    assert milestone(study) == (date(2027, 6, 15), "month")
    assert derive_catalyst([study], TODAY).clinical_date == date(2027, 6, 15)


@pytest.mark.parametrize(
    ("days_ago", "kept"), [(179, True), (180, True), (181, False), (0, True), (-5, True)]
)
def test_the_180_day_window(days_ago: int, kept: bool) -> None:
    study = s(primary=TODAY - timedelta(days=days_ago))
    candidate = consider(study, TODAY)
    assert candidate.eligible is kept
    if not kept:
        assert candidate.reject_reason == "date_over_180_days_ago"


def test_the_earliest_date_wins() -> None:
    result = derive_catalyst(
        [
            s("A", primary=later(300)),
            s("B", "PHASE2", primary=later(40)),
            s("C", primary=later(90)),
        ],
        TODAY,
    )
    assert result.clinical_date == later(40)
    assert result.clinical_phase == "phase_2"
    assert result.clinical_source_nct == "B"
    assert result.next_event_source == "clinical"


def test_a_tie_goes_to_phase_3_then_the_lower_nct() -> None:
    both = [s("A", "PHASE2", primary=later(40)), s("B", "PHASE3", primary=later(40))]
    assert derive_catalyst(both, TODAY).clinical_source_nct == "B"
    two_p3 = [s("N2", primary=later(40)), s("N1", primary=later(40))]
    assert derive_catalyst(two_p3, TODAY).clinical_source_nct == "N1"


def test_overdue_flag() -> None:
    past = derive_catalyst([s(primary=TODAY - timedelta(days=30))], TODAY)
    assert past.clinical_overdue is True
    today = derive_catalyst([s(primary=TODAY)], TODAY)
    assert today.clinical_overdue is False
    assert derive_catalyst([s(primary=later(1))], TODAY).clinical_overdue is False


# --- T14.3 no candidate ----------------------------------------------------------------------


def test_no_candidate_with_a_financing_date() -> None:
    result = derive_catalyst(
        [s(phase="PHASE1", primary=later(10))], TODAY, runway_financing_date=later(400)
    )
    assert result.clinical_date is None and result.clinical_phase == ""
    assert result.next_event_source == "financing"
    assert result.financing_date == later(400)
    assert result.reason_codes == ["no_catalyst"]


def test_no_candidate_and_no_financing_date() -> None:
    result = derive_catalyst([s(phase="PHASE1", primary=later(10))], TODAY)
    assert result.next_event_source == "none"


def test_no_studies_at_all() -> None:
    result = derive_catalyst([], TODAY)
    assert result.total_studies is None and result.active_phase_2 is None
    assert set(result.reason_codes) == {"no_trials", "no_catalyst"}
    assert result.next_event_source == "none"
    assert result.candidates == []


def test_a_clinical_date_wins_over_financing() -> None:
    result = derive_catalyst([s(primary=later(60))], TODAY, runway_financing_date=later(10))
    assert result.next_event_source == "clinical"
    assert result.financing_date == later(10)


# --- Candidates and counts -------------------------------------------------------------------


def test_every_study_is_explained() -> None:
    studies = [
        s("A", primary=later(60)),
        s("B", phase="PHASE1", primary=later(10)),
        s("C", status="TERMINATED", primary=later(20)),
        s("D", primary=later(90)),
    ]
    rows = {c["nct_id"]: c for c in derive_catalyst(studies, TODAY).candidates}
    assert set(rows) == {"A", "B", "C", "D"}
    assert rows["A"]["chosen"] is True and rows["A"]["eligible"] is True
    assert rows["D"]["eligible"] is True and rows["D"]["chosen"] is False
    assert rows["B"]["reject_reason"] == "phase_not_2_or_3"
    assert rows["C"]["reject_reason"] == "status_not_active"
    assert sum(1 for c in rows.values() if c["chosen"]) == 1
    assert rows["A"]["date_value"] == later(60).isoformat() and rows["A"]["date_precision"] == "day"


def test_active_counts_use_the_active_status_set() -> None:
    studies = [
        s("A", "PHASE3", "RECRUITING"),
        s("B", "PHASE3", "ACTIVE_NOT_RECRUITING"),
        s("C", "PHASE3", "ENROLLING_BY_INVITATION"),
        s("D", "PHASE3", "NOT_YET_RECRUITING"),  # not counted as active
        s("E", "PHASE3", "COMPLETED"),
        s("F", "PHASE2", "RECRUITING"),
        s("G", "PHASE1", "RECRUITING"),
    ]
    result = derive_catalyst(studies, TODAY)
    assert (result.active_phase_3, result.active_phase_2, result.total_studies) == (3, 1, 7)


def test_latest_trial_is_the_highest_nct_number() -> None:
    studies = [s("NCT00000009", status="SUSPENDED"), s("NCT00000100", status="RECRUITING")]
    result = derive_catalyst(studies, TODAY)
    assert (result.latest_trial_nct, result.latest_trial_status) == ("NCT00000100", "RECRUITING")


def test_fallback_completion_is_the_earliest_active_future_completion() -> None:
    studies = [
        s("A", completion=later(200), completion_precision="day", primary=later(100)),
        s("B", completion=later(50)),
        s("C", status="COMPLETED", completion=later(10)),
        s("D", completion=TODAY - timedelta(days=10)),
    ]
    studies[0] = Study(**{**studies[0].__dict__, "completion_raw": later(200).isoformat()})
    studies[1] = Study(**{**studies[1].__dict__, "completion_raw": "2026-12"})
    result = derive_catalyst(studies, TODAY)
    assert (result.fallback_end_raw, result.fallback_end_nct) == ("2026-12", "B")


def test_rule_version_is_reported() -> None:
    assert derive_catalyst([], TODAY).rule_version == RULE_VERSION == "v0"


# --- T14.5 storage ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_signals_are_stored_per_company_with_every_candidate() -> None:
    company = f.company()
    f.ticker(company, ticker="AAA")
    f.ct_study(company, nct_id="NCT00000001", highest_phase="PHASE3", status="RECRUITING",
               study_type="INTERVENTIONAL", primary_completion_date=later(60),
               primary_completion_precision="day", content_hash="a")  # fmt: skip
    f.ct_study(company, nct_id="NCT00000002", highest_phase="PHASE1", status="RECRUITING",
               study_type="INTERVENTIONAL", primary_completion_date=later(10),
               content_hash="b")  # fmt: skip
    quiet = f.company()
    f.ticker(quiet, ticker="BBB")
    f.runway_calc(quiet, calc_date=TODAY, financing_date=later(500))

    first = derive_universe(TODAY)
    again = derive_universe(TODAY)
    assert first.companies == 2 and CatalystSignal.objects.count() == 2
    assert again.run.pk != first.run.pk
    assert first.by_source == {"clinical": 1, "financing": 1}

    row = CatalystSignal.objects.get(company=company)
    assert row.rule_version == "v0" and row.clinical_date == later(60)
    assert row.clinical_phase == "phase_3" and row.clinical_source_nct == "NCT00000001"
    assert row.next_event_source == "clinical" and row.total_studies == 2
    assert len(row.candidates) == 2 and sum(c["chosen"] for c in row.candidates) == 1
    other = CatalystSignal.objects.get(company=quiet)
    assert other.next_event_source == "financing" and other.financing_date == later(500)
    assert set(other.reason_codes) == {"no_trials", "no_catalyst"}


@pytest.mark.django_db
def test_only_versions_seen_by_the_day_are_used() -> None:
    company = f.company()
    f.ticker(company)
    f.ct_study(company, nct_id="NCT00000001", highest_phase="PHASE3", status="RECRUITING",
               study_type="INTERVENTIONAL", primary_completion_date=later(60),
               observed_on=later(1), content_hash="a")  # fmt: skip
    derive_universe(TODAY)
    assert CatalystSignal.objects.get().total_studies is None


@pytest.mark.django_db
def test_the_scorer_reads_the_stored_signal() -> None:
    """The stored fields are the ones the scoring service turns into milestone inputs."""
    from apps.scoring.service import _company_inputs

    company = f.company()
    f.ticker(company)
    f.ct_study(company, nct_id="NCT00000001", highest_phase="PHASE3", status="RECRUITING",
               study_type="INTERVENTIONAL", primary_completion_date=later(60),
               primary_completion_precision="day", content_hash="a")  # fmt: skip
    derive_universe(TODAY)
    signal = CatalystSignal.objects.get()
    inputs = _company_inputs("k", f.snapshot(company), None, signal)
    assert inputs.clinical_date_text == later(60).isoformat()
    assert inputs.clinical_phase == "phase_3"
    assert inputs.active_phase_3 == 1 and inputs.latest_trial_status == "RECRUITING"
