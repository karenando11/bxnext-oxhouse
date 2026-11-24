"""Trial panel values derived from stored studies (Step 13A). Read-time only; nothing is stored.

Every function takes the studies and a ``today`` value: there is no hidden clock.

Defaults where the requirements leave a choice (see the Step 13A open items):
* A combined Phase 2/3 study counts as Phase 3 (its stored ``highest_phase``).
* "Quarters out" is the days left divided by 91.31 (a quarter of a year), rounded up, so any
  future date is at least one quarter out. Today and the past are "Past due".
* A study's milestone date is its primary completion date, else its study completion date.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

DAYS_PER_QUARTER = 91.31
PHASE_ORDER = ("EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4")
_TYPE_PREFIX = re.compile(r"^[A-Z][A-Z_ ]*:\s*")


@dataclass(frozen=True)
class Study:
    nct_id: str
    highest_phase: str = ""
    status: str = ""
    primary_completion: date | None = None
    completion: date | None = None
    conditions: Sequence[str] = field(default_factory=tuple)
    interventions: Sequence[dict[str, str]] = field(default_factory=tuple)
    title: str = ""
    study_type: str = ""
    primary_precision: str = ""  # "day", "month" or ""
    completion_precision: str = ""
    primary_raw: str = ""
    completion_raw: str = ""

    @property
    def milestone_date(self) -> date | None:
        return self.primary_completion or self.completion

    @classmethod
    def from_version(cls, version: Any) -> Study:
        """A ``CtStudyVersion`` (or anything with the same fields)."""
        return cls(
            nct_id=version.nct_id,
            highest_phase=version.highest_phase,
            status=version.status,
            primary_completion=version.primary_completion_date,
            completion=version.completion_date,
            conditions=tuple(version.conditions or ()),
            interventions=tuple(version.interventions or ()),
            title=version.title,
            study_type=version.study_type,
            primary_precision=version.primary_completion_precision,
            completion_precision=version.completion_precision,
            primary_raw=version.primary_completion_raw,
            completion_raw=version.completion_raw,
        )


def quarters_out(completion: date | None, today: date) -> str:
    """ "2 quarters out", "1 quarter out" or "Past due" (also for today); blank without a date."""
    if completion is None:
        return ""
    days = (completion - today).days
    if days <= 0:
        return "Past due"
    quarters = math.ceil(days / DAYS_PER_QUARTER)
    return f"{quarters} quarter{'s' if quarters != 1 else ''} out"


def drug_name(study: Study) -> str:
    """The first intervention's name with any "DRUG: " style type prefix removed."""
    for item in study.interventions:
        name = _TYPE_PREFIX.sub("", (item.get("name") or "").strip())
        if name:
            return name
    return ""


def _is_phase3(study: Study) -> bool:
    return study.highest_phase == "PHASE3"


def next_milestone(studies: Sequence[Study], today: date) -> Study | None:
    """The Phase 3 study with the soonest date; otherwise the study with the soonest date.

    Past dates are not filtered out, so an overdue Phase 3 study still leads. Studies with no
    date are never chosen.
    """
    dated = [s for s in studies if s.milestone_date is not None]
    if not dated:
        return None
    phase3 = [s for s in dated if _is_phase3(s)]
    pool = phase3 or dated
    return min(pool, key=lambda s: (s.milestone_date or date.max, s.nct_id))


def phase_chips(studies: Sequence[Study]) -> list[str]:
    """One chip per distinct phase, lowest phase first. Studies with no phase add none."""
    present = {s.highest_phase for s in studies if s.highest_phase}
    known = [p for p in PHASE_ORDER if p in present]
    return known + sorted(present - set(PHASE_ORDER))


def indications(studies: Sequence[Study]) -> list[str]:
    """Every condition of every study, in study order, not de-duplicated (as the page does)."""
    return [c for s in studies for c in s.conditions]


@dataclass(frozen=True)
class Upcoming:
    nct_id: str
    completion: date
    highest_phase: str


def upcoming_completions(studies: Sequence[Study], today: date) -> list[Upcoming]:
    """Studies completing on or after today, soonest first, by primary date else study date."""
    rows = [
        Upcoming(s.nct_id, s.milestone_date, s.highest_phase)
        for s in studies
        if s.milestone_date is not None and s.milestone_date >= today
    ]
    return sorted(rows, key=lambda u: (u.completion, u.nct_id))


@dataclass(frozen=True)
class TrialPanel:
    next_milestone: Study | None
    quarters_out: str
    drug: str
    pipeline_total: int
    phase_chips: list[str]
    indications: list[str]
    upcoming: list[Upcoming]
    reason_codes: list[str]


def trial_panel(studies: Sequence[Study], today: date) -> TrialPanel:
    """All the panel values. A company with no studies yields empty values and ``no_trials``."""
    if not studies:
        return TrialPanel(None, "", "", 0, [], [], [], ["no_trials"])
    milestone = next_milestone(studies, today)
    return TrialPanel(
        next_milestone=milestone,
        quarters_out=quarters_out(milestone.milestone_date, today) if milestone else "",
        drug=drug_name(milestone) if milestone else "",
        pipeline_total=len(studies),
        phase_chips=phase_chips(studies),
        indications=indications(studies),
        upcoming=upcoming_completions(studies, today),
        reason_codes=[],
    )


def latest_studies(company_id: int, today: date) -> list[Study]:
    """Each study's newest stored version on or before ``today``, ordered by NCT id."""
    from apps.trials.models import CtStudyVersion

    latest: dict[str, CtStudyVersion] = {}
    for version in CtStudyVersion.objects.filter(
        company_id=company_id, observed_on__lte=today
    ).order_by("observed_on", "id"):
        latest[version.nct_id] = version
    return [Study.from_version(latest[k]) for k in sorted(latest)]
