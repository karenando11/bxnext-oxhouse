"""Clinical catalyst derivation (Step 14), rule version v0. Pure: studies and a day in.

The catalyst is the earliest completion date among the company's eligible studies, with that
study's highest phase. Every considered study is reported with its decision (A14.2).

Rule v0, from the requirements:
* Eligible: interventional, Phase 2 or Phase 3 (Phase 1/2 counts as Phase 2, Phase 2/3 as
  Phase 3), status recruiting, not yet recruiting, enrolling by invitation or active not
  recruiting.
* Date: primary completion, else study completion. A month-only date becomes the 15th. Dates more
  than 180 days in the past are ignored. The earliest remaining date wins; a tie goes to Phase 3.
* Overdue when the chosen date is before the day.

Choices made where the requirements leave room (Step 14 open items): active counts use the same
statuses except NOT_YET_RECRUITING, which the data excludes; ``total_studies`` is every stored
study; the latest trial is the one with the highest NCT number (the most recently registered);
the fallback completion is the earliest completion of an active study that is not in the past,
else the latest completion of the latest trial.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from apps.trials.display import Study

RULE_VERSION = "v0"
WINDOW_DAYS = 180
ELIGIBLE_STATUSES = frozenset(
    {"RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING"}
)
ACTIVE_STATUSES = ELIGIBLE_STATUSES - {"NOT_YET_RECRUITING"}
PHASE_LABEL = {"PHASE2": "phase_2", "PHASE3": "phase_3"}


@dataclass
class Candidate:
    nct_id: str
    highest_phase: str
    status: str
    date_value: date | None
    date_precision: str
    eligible: bool
    reject_reason: str = ""
    chosen: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "nct_id": self.nct_id,
            "highest_phase": self.highest_phase,
            "status": self.status,
            "date_value": self.date_value.isoformat() if self.date_value else None,
            "date_precision": self.date_precision,
            "eligible": self.eligible,
            "reject_reason": self.reject_reason,
            "chosen": self.chosen,
        }


@dataclass
class CatalystResult:
    rule_version: str = RULE_VERSION
    clinical_date: date | None = None
    clinical_phase: str = ""
    clinical_overdue: bool = False
    clinical_source_nct: str = ""
    fallback_end_raw: str = ""
    fallback_end_nct: str = ""
    active_phase_2: int | None = None
    active_phase_3: int | None = None
    total_studies: int | None = None
    latest_trial_nct: str = ""
    latest_trial_status: str = ""
    financing_date: date | None = None
    next_event_source: str = "none"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    reason_codes: list[str] = field(default_factory=list)


def milestone(study: Study) -> tuple[date | None, str]:
    """The study's date and its precision: primary completion, else study completion.

    A month-only date becomes the 15th (the stored value is the 1st).
    """
    if study.primary_completion is not None:
        value, precision = study.primary_completion, study.primary_precision or "day"
    elif study.completion is not None:
        value, precision = study.completion, study.completion_precision or "day"
    else:
        return None, ""
    if precision == "month":
        value = value.replace(day=15)
    return value, precision


def consider(study: Study, today: date) -> Candidate:
    value, precision = milestone(study)
    candidate = Candidate(study.nct_id, study.highest_phase, study.status, value, precision, False)
    if study.study_type and study.study_type != "INTERVENTIONAL":
        candidate.reject_reason = "not_interventional"
    elif study.highest_phase not in PHASE_LABEL:
        candidate.reject_reason = "phase_not_2_or_3"
    elif study.status not in ELIGIBLE_STATUSES:
        candidate.reject_reason = "status_not_active"
    elif value is None:
        candidate.reject_reason = "no_date"
    elif value < today - timedelta(days=WINDOW_DAYS):
        candidate.reject_reason = "date_over_180_days_ago"
    else:
        candidate.eligible = True
    return candidate


def _fallback(studies: Sequence[Study], latest: Study | None, today: date) -> tuple[str, str]:
    upcoming = []
    for study in studies:
        if study.status not in ACTIVE_STATUSES:
            continue
        for value, raw in (
            (study.completion, study.completion_raw),
            (study.primary_completion, study.primary_raw),
        ):
            if value is not None and value >= today:
                upcoming.append((value, raw or value.isoformat(), study.nct_id))
    if upcoming:
        _, raw, nct = min(upcoming)
        return raw, nct
    if latest is not None and (latest.completion or latest.primary_completion):
        raw = latest.completion_raw if latest.completion else latest.primary_raw
        return raw, latest.nct_id
    return "", ""


def derive_catalyst(
    studies: Sequence[Study], today: date, *, runway_financing_date: date | None = None
) -> CatalystResult:
    result = CatalystResult(financing_date=runway_financing_date)
    reasons: list[str] = []
    considered = [consider(s, today) for s in studies]
    eligible = [c for c in considered if c.eligible and c.date_value is not None]
    if eligible:
        best = min(
            eligible,
            key=lambda c: (c.date_value or date.max, c.highest_phase != "PHASE3", c.nct_id),
        )
        best.chosen = True
        assert best.date_value is not None
        result.clinical_date = best.date_value
        result.clinical_phase = PHASE_LABEL[best.highest_phase]
        result.clinical_overdue = best.date_value < today
        result.clinical_source_nct = best.nct_id
    result.candidates = [c.as_json() for c in considered]

    if studies:
        result.total_studies = len(studies)
        result.active_phase_2 = sum(
            1 for s in studies if s.highest_phase == "PHASE2" and s.status in ACTIVE_STATUSES
        )
        result.active_phase_3 = sum(
            1 for s in studies if s.highest_phase == "PHASE3" and s.status in ACTIVE_STATUSES
        )
        latest = max(studies, key=lambda s: s.nct_id)
        result.latest_trial_nct, result.latest_trial_status = latest.nct_id, latest.status
        result.fallback_end_raw, result.fallback_end_nct = _fallback(studies, latest, today)
    else:
        reasons.append("no_trials")

    if result.clinical_date is not None:
        result.next_event_source = "clinical"
    else:
        reasons.append("no_catalyst")
        result.next_event_source = "financing" if runway_financing_date is not None else "none"
    result.reason_codes = reasons
    return result
