"""Milestone: how close the next identified catalyst is, blended with earnings and momentum.

Pure Python. No Django, database, network or clock: the caller passes ``now`` explicitly.

How it works
------------
Milestone is a weighted average of the terms that apply to a company, plus a financing boost:

* the clinical catalyst date, scored by how far away it is (Phase 3 peaks higher);
* active Phase 3 and Phase 2 trial counts;
* the trial completion date, used only when no clinical date was scored;
* a recruiting bonus for a recruiting latest trial when a Phase 3 trial is active;
* earnings proximity;
* analyst growth estimates and relative volume.

A company with no identified catalyst (no usable date and no active Phase 2 or Phase 3 trial)
gets the floor score. A company whose runway ends before its next milestone gets a boost that
grows with the gap. The result is clamped to 0..10.

Time arithmetic follows the existing system: dates are midnight UTC, ``now`` is a timezone-aware
timestamp, and differences are fractional days.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

# (days up to, peak offset, minimum) rows of the time decay; the last two are fixed values.
_DECAY_ROWS = ((30.0, 2.0, 3.0), (90.0, 1.0, 5.0), (180.0, 0.5, 7.0))


@dataclass(frozen=True)
class MilestoneConfig:
    """Every constant used by Milestone. Step 4 loads these from the formula version.

    Validated against the 2026-10-12 golden export. Not pinned by it: the weight and bands of
    ``eps_this``, because the export has that column blank (the rule comes from the existing
    system's source and was spot-checked against real Finviz values).
    """

    floor_score: float = 3.0
    days_per_month: float = 30.0
    lapse_days: float = 90.0  # a readout this recently in the past still counts as 1 day out

    # Time decay
    decay_overdue: tuple[float, float] = (5.0, 2.0)  # (peak offset, minimum) when days <= 0
    decay_rows: tuple[tuple[float, float, float], ...] = _DECAY_ROWS
    decay_year_days: float = 365.0
    decay_year_after: tuple[float, float] = (5.0, 4.0)  # up to 540 days: max(4, peak - 5)
    decay_after_days: float = 540.0
    decay_two_year_days: float = 730.0
    decay_two_year_score: float = 2.0
    decay_far_score: float = 1.0

    clinical_phase_3_peak: float = 10.0
    clinical_phase_3_weight: float = 3.0
    clinical_other_peak: float = 9.0
    clinical_other_weight: float = 2.0
    clinical_phase_3_label: str = "phase_3"

    active_phase_3_per_trial: float = 4.0
    active_phase_3_cap: float = 10.0
    active_phase_3_weight: float = 1.5
    active_phase_2_per_trial: float = 3.0
    active_phase_2_cap: float = 9.0
    active_phase_2_weight: float = 1.0

    completion_peak: float = 9.0
    completion_weight: float = 1.0

    recruiting_status: str = "RECRUITING"
    recruiting_score: float = 7.0
    recruiting_weight: float = 0.5

    # Earnings proximity: (days up to, score); beyond the last row the fallback score applies.
    earnings_bands: tuple[tuple[float, float], ...] = (
        (14.0, 10.0),
        (30.0, 8.0),
        (60.0, 6.0),
        (90.0, 4.0),
    )
    earnings_far_score: float = 2.0
    earnings_weight_with_clinical: float = 1.0
    earnings_weight_without_clinical: float = 2.0

    # Growth and volume buckets: (value above, score), then the fallback score.
    eps_next_buckets: tuple[tuple[float, float], ...] = ((50.0, 8.0), (20.0, 6.0), (0.0, 4.0))
    eps_next_fallback: float = 2.0
    eps_next_weight: float = 0.8
    rel_vol_buckets: tuple[tuple[float, float], ...] = (
        (5.0, 10.0),
        (3.0, 8.0),
        (2.0, 6.0),
        (1.5, 4.0),
    )
    rel_vol_fallback: float = 2.0
    rel_vol_weight: float = 0.5
    eps_this_buckets: tuple[tuple[float, float], ...] = ((50.0, 7.0), (20.0, 5.0), (0.0, 3.0))
    eps_this_fallback: float = 1.0
    eps_this_weight: float = 0.5

    # Financing boost: (gap in months at least, boost), then the fallback boost.
    boost_bands: tuple[tuple[float, float], ...] = ((12.0, 3.0), (6.0, 2.0), (3.0, 1.5))
    boost_fallback: float = 1.0

    minimum: float = 0.0
    maximum: float = 10.0


DEFAULT_CONFIG = MilestoneConfig()


@dataclass(frozen=True)
class MilestoneInput:
    """Plain inputs for one company. ``None`` means missing."""

    clinical_date: date | None = None
    clinical_overdue: bool = False
    clinical_phase: str | None = None
    active_phase_3: float | None = None
    active_phase_2: float | None = None
    next_completion_date: date | None = None
    latest_completion_date: date | None = None
    latest_trial_status: str | None = None
    earnings_date: date | None = None
    eps_next: float | None = None
    rel_vol: float | None = None
    eps_this: float | None = None
    runway_months: float | None = None  # as filed (see the runway quirk in Step 4)


@dataclass(frozen=True)
class MilestoneTerm:
    name: str
    score: float
    weight: float


@dataclass(frozen=True)
class MilestoneResult:
    score: float  # unrounded
    terms: tuple[MilestoneTerm, ...]
    boost: float
    boost_gap_months: float | None  # runway shortfall against the next milestone, when boosted
    months_to_milestone: float | None  # next milestone date used for the boost
    clinical_scored: bool
    has_identified_catalyst: bool
    floor_applied: bool  # True when the floor score replaced the blend


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _days_until(target: date, now: datetime) -> float:
    return (datetime.combine(target, time(), tzinfo=UTC) - now).total_seconds() / 86400


def _readout_days_out(days: float, config: MilestoneConfig) -> float:
    """A readout up to ``lapse_days`` in the past is treated as 1 day out."""
    return 1.0 if -config.lapse_days < days <= 0 else days


def time_decay(days_out: float, peak: float, config: MilestoneConfig = DEFAULT_CONFIG) -> float:
    """Score for a milestone ``days_out`` days away; closer is higher, far or overdue is low."""
    if days_out <= 0:
        offset, minimum = config.decay_overdue
        return max(minimum, peak - offset)
    for limit, offset, minimum in config.decay_rows:
        if days_out <= limit:
            return max(minimum, peak - offset)
    if days_out <= config.decay_year_days:
        return peak
    if days_out <= config.decay_after_days:
        offset, minimum = config.decay_year_after
        return max(minimum, peak - offset)
    if days_out <= config.decay_two_year_days:
        return config.decay_two_year_score
    return config.decay_far_score


def _bucket_above(value: float, buckets: tuple[tuple[float, float], ...], fallback: float) -> float:
    for above, score in buckets:
        if value > above:
            return score
    return fallback


def _earnings_score(days: float, config: MilestoneConfig) -> float:
    for limit, score in config.earnings_bands:
        if days <= limit:
            return score
    return config.earnings_far_score


def _boost_for_gap(gap: float, config: MilestoneConfig) -> float:
    for minimum, boost in config.boost_bands:
        if gap >= minimum:
            return boost
    return config.boost_fallback


def milestone(
    data: MilestoneInput, now: datetime, config: MilestoneConfig = DEFAULT_CONFIG
) -> MilestoneResult:
    """Milestone for one company at ``now`` (timezone-aware), unrounded, with every component."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    clinical_days: float | None = None
    if data.clinical_date is not None:
        clinical_days = (
            1.0
            if data.clinical_overdue
            else _readout_days_out(_days_until(data.clinical_date, now), config)
        )
    clinical_scored = clinical_days is not None

    completion = data.next_completion_date or data.latest_completion_date
    completion_days: float | None = None
    if completion is not None and not clinical_scored:
        completion_days = _readout_days_out(_days_until(completion, now), config)

    active_3, active_2 = _finite(data.active_phase_3), _finite(data.active_phase_2)
    has_active_3 = active_3 is not None and active_3 > 0
    has_active_2 = active_2 is not None and active_2 > 0

    # An identified catalyst is an upcoming date or an active late-stage trial.
    if clinical_scored:
        upcoming_date = clinical_days is not None and clinical_days > 0
    elif completion is not None:
        upcoming_date = _readout_days_out(_days_until(completion, now), config) > 0
    else:
        upcoming_date = False
    identified = upcoming_date or has_active_3 or has_active_2

    terms: list[MilestoneTerm] = []
    if clinical_days is not None:
        if data.clinical_phase == config.clinical_phase_3_label:
            peak, weight = config.clinical_phase_3_peak, config.clinical_phase_3_weight
        else:
            peak, weight = config.clinical_other_peak, config.clinical_other_weight
        terms.append(
            MilestoneTerm("clinical_date", time_decay(clinical_days, peak, config), weight)
        )
    if has_active_3 and active_3 is not None:
        score = min(active_3 * config.active_phase_3_per_trial, config.active_phase_3_cap)
        terms.append(MilestoneTerm("active_phase_3", score, config.active_phase_3_weight))
    if has_active_2 and active_2 is not None:
        score = min(active_2 * config.active_phase_2_per_trial, config.active_phase_2_cap)
        terms.append(MilestoneTerm("active_phase_2", score, config.active_phase_2_weight))
    if completion_days is not None and completion_days > 0:
        terms.append(
            MilestoneTerm(
                "trial_completion",
                time_decay(completion_days, config.completion_peak, config),
                config.completion_weight,
            )
        )
    if data.latest_trial_status == config.recruiting_status and has_active_3:
        terms.append(
            MilestoneTerm("recruiting_bonus", config.recruiting_score, config.recruiting_weight)
        )
    if data.earnings_date is not None:
        days = max(0.0, _days_until(data.earnings_date, now))
        weight = (
            config.earnings_weight_with_clinical
            if clinical_scored
            else config.earnings_weight_without_clinical
        )
        terms.append(MilestoneTerm("earnings", _earnings_score(days, config), weight))
    eps_next = _finite(data.eps_next)
    if eps_next is not None:
        terms.append(
            MilestoneTerm(
                "eps_next",
                _bucket_above(eps_next, config.eps_next_buckets, config.eps_next_fallback),
                config.eps_next_weight,
            )
        )
    rel_vol = _finite(data.rel_vol)
    if rel_vol is not None:
        terms.append(
            MilestoneTerm(
                "rel_vol",
                _bucket_above(rel_vol, config.rel_vol_buckets, config.rel_vol_fallback),
                config.rel_vol_weight,
            )
        )
    eps_this = _finite(data.eps_this)
    if eps_this is not None:
        terms.append(
            MilestoneTerm(
                "eps_this",
                _bucket_above(eps_this, config.eps_this_buckets, config.eps_this_fallback),
                config.eps_this_weight,
            )
        )

    # Financing boost: runway ends before the next milestone date.
    months_to_milestone: float | None = None
    if clinical_days is not None and clinical_days > 0:
        months_to_milestone = clinical_days / config.days_per_month
    elif not clinical_scored and completion is not None:
        raw_days = _days_until(completion, now)
        if raw_days > 0:
            months_to_milestone = raw_days / config.days_per_month
    boost = 0.0
    gap: float | None = None
    runway = _finite(data.runway_months)
    if runway is not None and months_to_milestone is not None and runway < months_to_milestone:
        gap = months_to_milestone - runway
        boost = _boost_for_gap(gap, config)

    total_weight = sum(t.weight for t in terms)
    if not identified or total_weight == 0:
        return MilestoneResult(
            score=config.floor_score,
            terms=tuple(terms),
            boost=boost,
            boost_gap_months=gap,
            months_to_milestone=months_to_milestone,
            clinical_scored=clinical_scored,
            has_identified_catalyst=identified,
            floor_applied=True,
        )
    blend = sum(t.score * t.weight for t in terms) / total_weight + boost
    return MilestoneResult(
        score=min(max(blend, config.minimum), config.maximum),
        terms=tuple(terms),
        boost=boost,
        boost_gap_months=gap,
        months_to_milestone=months_to_milestone,
        clinical_scored=clinical_scored,
        has_identified_catalyst=identified,
        floor_applied=False,
    )


_EARNINGS_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_earnings_date(text: str | None) -> date | None:
    """The date in a Finviz earnings text such as ``8/13/2026 8:30:00 AM``; None if absent."""
    match = _EARNINGS_DATE.search(text or "")
    if match is None:
        return None
    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None
