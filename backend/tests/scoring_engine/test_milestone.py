"""T3.1 to T3.5 (unit): Milestone against synthetic inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from scoring_engine import (
    MilestoneConfig,
    MilestoneInput,
    MilestoneTerm,
    display_score,
    milestone,
    parse_earnings_date,
    time_decay,
)

# Midnight UTC, so a date N days ahead is exactly N days out.
NOW = datetime(2026, 10, 12, tzinfo=UTC)
TODAY = NOW.date()


def ahead(days: int) -> date:
    return TODAY + timedelta(days=days)


def term(result_terms: tuple[MilestoneTerm, ...], name: str) -> MilestoneTerm:
    return next(t for t in result_terms if t.name == name)


# ------------------------------------------------------------------------- T3.1 the floor


def test_nothing_qualifies_gives_the_floor() -> None:
    result = milestone(MilestoneInput(), NOW)
    assert result.score == 3.0
    assert result.floor_applied is True
    assert result.has_identified_catalyst is False


def test_floor_ignores_every_other_term() -> None:
    data = MilestoneInput(
        earnings_date=ahead(5), eps_next=80, rel_vol=9, eps_this=90, runway_months=1.0
    )
    assert milestone(data, NOW).score == 3.0  # no catalyst, so the other terms do not count


def test_lapsed_readout_without_an_active_trial_is_not_a_catalyst() -> None:
    data = MilestoneInput(clinical_date=ahead(-120), earnings_date=ahead(5))
    result = milestone(data, NOW)
    assert result.has_identified_catalyst is False
    assert result.score == 3.0


def test_active_late_stage_trial_is_enough() -> None:
    result = milestone(MilestoneInput(active_phase_2=1), NOW)
    assert result.has_identified_catalyst is True
    assert result.score == 3.0  # 3 x 1 at weight 1.0 averages to 3, with no boost


# ------------------------------------------------------------------------ T3.2 time decay


@pytest.mark.parametrize(
    ("days", "peak", "score"),
    [
        (-5, 10, 5), (0, 10, 5), (0, 9, 4), (0, 6, 2),
        (0.5, 10, 8), (30, 10, 8), (30, 9, 7), (30, 4, 3),
        (31, 10, 9), (90, 10, 9), (90, 9, 8), (90, 5, 5),
        (91, 10, 9.5), (180, 10, 9.5), (180, 9, 8.5), (180, 7, 7),
        (181, 10, 10), (365, 10, 10), (365, 9, 9),
        (366, 10, 5), (540, 10, 5), (540, 9, 4),
        (541, 10, 2), (730, 10, 2),
        (731, 10, 1), (2000, 9, 1),
    ],
)  # fmt: skip
def test_time_decay_boundaries(days: float, peak: float, score: float) -> None:
    assert time_decay(days, peak) == score


# ------------------------------------------------------------------- clinical date scoring


def test_phase_3_clinical_date_scores_at_weight_three() -> None:
    data = MilestoneInput(clinical_date=ahead(200), clinical_phase="phase_3")
    clinical = term(milestone(data, NOW).terms, "clinical_date")
    assert (clinical.score, clinical.weight) == (10.0, 3.0)


def test_other_phases_score_lower_at_weight_two() -> None:
    data = MilestoneInput(clinical_date=ahead(200), clinical_phase="phase_2")
    clinical = term(milestone(data, NOW).terms, "clinical_date")
    assert (clinical.score, clinical.weight) == (9.0, 2.0)


def test_missing_phase_scores_as_other() -> None:
    clinical = term(milestone(MilestoneInput(clinical_date=ahead(10)), NOW).terms, "clinical_date")
    assert (clinical.score, clinical.weight) == (7.0, 2.0)


# ---------------------------------------------------------------- T3.3 overdue and lapsed


def test_overdue_flag_counts_as_one_day_out_whatever_the_age() -> None:
    data = MilestoneInput(
        clinical_date=ahead(-900), clinical_overdue=True, clinical_phase="phase_3"
    )
    result = milestone(data, NOW)
    assert term(result.terms, "clinical_date").score == 8.0  # decay(1 day, peak 10)
    assert result.has_identified_catalyst is True


def test_recently_lapsed_readout_counts_as_one_day_out() -> None:
    data = MilestoneInput(clinical_date=ahead(-60), clinical_phase="phase_3")
    result = milestone(data, NOW)
    assert term(result.terms, "clinical_date").score == 8.0
    assert result.has_identified_catalyst is True


def test_long_lapsed_readout_scores_lowest_and_is_not_a_catalyst() -> None:
    data = MilestoneInput(clinical_date=ahead(-120), clinical_phase="phase_3", active_phase_3=0)
    result = milestone(data, NOW)
    assert term(result.terms, "clinical_date").score == 5.0  # decay(<= 0, peak 10)
    assert result.has_identified_catalyst is False


def test_readout_exactly_at_the_lapse_limit_is_long_lapsed() -> None:
    data = MilestoneInput(clinical_date=ahead(-90))
    assert term(milestone(data, NOW).terms, "clinical_date").score == 4.0  # decay(<= 0, peak 9)


# ------------------------------------------------------------------ active trials, recruiting


@pytest.mark.parametrize(("count", "score"), [(1, 4.0), (2, 8.0), (3, 10.0), (9, 10.0)])
def test_active_phase_3_counts(count: float, score: float) -> None:
    result = milestone(MilestoneInput(active_phase_3=count), NOW)
    assert (
        term(result.terms, "active_phase_3").score,
        term(result.terms, "active_phase_3").weight,
    ) == (score, 1.5)


@pytest.mark.parametrize(("count", "score"), [(1, 3.0), (2, 6.0), (3, 9.0), (8, 9.0)])
def test_active_phase_2_counts(count: float, score: float) -> None:
    result = milestone(MilestoneInput(active_phase_2=count), NOW)
    assert term(result.terms, "active_phase_2").score == score
    assert term(result.terms, "active_phase_2").weight == 1.0


def test_zero_or_missing_counts_add_no_term() -> None:
    result = milestone(MilestoneInput(clinical_date=ahead(10), active_phase_3=0), NOW)
    assert {t.name for t in result.terms} == {"clinical_date"}


def test_recruiting_bonus_needs_an_active_phase_3_trial() -> None:
    with_p3 = milestone(MilestoneInput(active_phase_3=1, latest_trial_status="RECRUITING"), NOW)
    assert (
        term(with_p3.terms, "recruiting_bonus").score,
        term(with_p3.terms, "recruiting_bonus").weight,
    ) == (7.0, 0.5)
    without = milestone(MilestoneInput(active_phase_2=1, latest_trial_status="RECRUITING"), NOW)
    assert "recruiting_bonus" not in {t.name for t in without.terms}
    other = milestone(MilestoneInput(active_phase_3=1, latest_trial_status="COMPLETED"), NOW)
    assert "recruiting_bonus" not in {t.name for t in other.terms}


# ------------------------------------------------------------------------ trial completion


def test_completion_fallback_applies_without_a_clinical_date() -> None:
    result = milestone(MilestoneInput(next_completion_date=ahead(200)), NOW)
    completion = term(result.terms, "trial_completion")
    assert (completion.score, completion.weight) == (9.0, 1.0)
    assert result.has_identified_catalyst is True


def test_next_completion_is_preferred_over_latest() -> None:
    data = MilestoneInput(next_completion_date=ahead(20), latest_completion_date=ahead(400))
    assert term(milestone(data, NOW).terms, "trial_completion").score == 7.0


def test_latest_completion_is_used_when_there_is_no_next() -> None:
    data = MilestoneInput(latest_completion_date=ahead(400))
    assert term(milestone(data, NOW).terms, "trial_completion").score == 4.0


def test_completion_is_ignored_when_a_clinical_date_was_scored() -> None:
    data = MilestoneInput(clinical_date=ahead(100), next_completion_date=ahead(50))
    assert "trial_completion" not in {t.name for t in milestone(data, NOW).terms}


def test_recently_lapsed_completion_counts_but_long_lapsed_does_not() -> None:
    recent = milestone(MilestoneInput(next_completion_date=ahead(-60)), NOW)
    assert term(recent.terms, "trial_completion").score == 7.0  # decay(1 day, peak 9)
    old = milestone(MilestoneInput(next_completion_date=ahead(-120), active_phase_2=1), NOW)
    assert "trial_completion" not in {t.name for t in old.terms}


# ------------------------------------------------------------------------ T3.4 earnings


@pytest.mark.parametrize(
    ("days", "clinical", "score", "weight"),
    [
        (0, True, 10, 1.0),
        (-75, True, 10, 1.0),
        (14, True, 10, 1.0),
        (15, True, 8, 1.0),
        (20, False, 8, 2.0),
        (30, False, 8, 2.0),
        (31, False, 6, 2.0),
        (60, False, 6, 2.0),
        (61, False, 4, 2.0),
        (90, False, 4, 2.0),
        (100, False, 2, 2.0),
    ],
)
def test_earnings_proximity(days: int, clinical: bool, score: float, weight: float) -> None:
    data = MilestoneInput(
        clinical_date=ahead(300) if clinical else None,
        active_phase_2=1,
        earnings_date=ahead(days),
    )
    earnings = term(milestone(data, NOW).terms, "earnings")
    assert (earnings.score, earnings.weight) == (score, weight)


def test_earnings_date_text_parsing() -> None:
    assert parse_earnings_date("8/13/2026 8:30:00 AM") == date(2026, 8, 13)
    assert parse_earnings_date("10/5/2026") == date(2026, 10, 5)
    for text in (None, "", "TBD", "13/45/2026"):
        assert parse_earnings_date(text) is None


# ------------------------------------------------------------------ growth and volume terms


@pytest.mark.parametrize(
    ("value", "score"),
    [(80, 8), (50.01, 8), (50, 6), (20.01, 6), (20, 4), (0.01, 4), (0, 2), (-9, 2)],
)
def test_eps_next_buckets(value: float, score: float) -> None:
    eps = term(milestone(MilestoneInput(active_phase_2=1, eps_next=value), NOW).terms, "eps_next")
    assert (eps.score, eps.weight) == (score, 0.8)


@pytest.mark.parametrize(
    ("value", "score"),
    [
        (9, 10),
        (5.01, 10),
        (5, 8),
        (3.01, 8),
        (3, 6),
        (2.01, 6),
        (2, 4),
        (1.51, 4),
        (1.5, 2),
        (0.08, 2),
    ],
)
def test_rel_vol_buckets(value: float, score: float) -> None:
    rel = term(milestone(MilestoneInput(active_phase_2=1, rel_vol=value), NOW).terms, "rel_vol")
    assert (rel.score, rel.weight) == (score, 0.5)


@pytest.mark.parametrize(
    ("value", "score"),
    [(535.0, 7), (50.01, 7), (50, 5), (20.01, 5), (20, 3), (0.01, 3), (0, 1), (-195.79, 1)],
)
def test_eps_this_buckets(value: float, score: float) -> None:
    eps = term(milestone(MilestoneInput(active_phase_2=1, eps_this=value), NOW).terms, "eps_this")
    assert (eps.score, eps.weight) == (score, 0.5)


def test_null_growth_and_volume_inputs_are_skipped_not_zero() -> None:
    result = milestone(MilestoneInput(active_phase_2=1), NOW)
    assert {t.name for t in result.terms} == {"active_phase_2"}
    assert result.score == 3.0  # not diluted by absent terms


# --------------------------------------------------------------------- T3.5 financing boost


@pytest.mark.parametrize(
    ("runway", "boost"),
    [(None, 0), (30.0, 0), (6.0, 0), (5.0, 1.0), (3.0, 1.5), (0.5, 1.5), (0.0, 2.0), (-6.0, 3.0)],
)
def test_boost_for_a_milestone_six_months_out(runway: float | None, boost: float) -> None:
    # 180 days out is exactly 6.0 months, so the gap is 6 - runway
    data = MilestoneInput(clinical_date=ahead(180), runway_months=runway)
    result = milestone(data, NOW)
    assert result.boost == boost


def test_boost_gap_boundaries() -> None:
    # milestone 360 days out = 12.0 months
    def boost(runway: float) -> float:
        return milestone(MilestoneInput(clinical_date=ahead(360), runway_months=runway), NOW).boost

    assert boost(0.0) == 3.0  # gap 12
    assert boost(0.1) == 2.0  # gap 11.9
    assert boost(6.0) == 2.0  # gap 6
    assert boost(6.1) == 1.5  # gap 5.9
    assert boost(9.0) == 1.5  # gap 3
    assert boost(9.1) == 1.0  # gap 2.9
    assert boost(12.0) == 0.0  # runway equal to the milestone: not shorter


def test_boost_uses_the_completion_date_when_there_is_no_clinical_date() -> None:
    data = MilestoneInput(next_completion_date=ahead(360), runway_months=1.0)
    assert milestone(data, NOW).boost == 2.0


def test_no_boost_without_a_future_milestone_date() -> None:
    data = MilestoneInput(active_phase_2=1, runway_months=1.0, earnings_date=ahead(5))
    assert milestone(data, NOW).boost == 0.0


def test_boost_is_added_after_the_average_and_the_total_is_clamped() -> None:
    data = MilestoneInput(
        clinical_date=ahead(300), clinical_phase="phase_3", active_phase_3=3, runway_months=1.0
    )
    result = milestone(data, NOW)
    assert result.boost == 2.0
    assert result.score == 10.0  # the average is 10 and the boost of 2 would take it past 10


# ------------------------------------------------------------------ blend, time, components


def test_blend_is_the_weighted_average() -> None:
    data = MilestoneInput(clinical_date=ahead(200), clinical_phase="phase_3", eps_next=10)
    # clinical 10 at 3.0, eps_next 4 at 0.8
    assert milestone(data, NOW).score == pytest.approx((10 * 3.0 + 4 * 0.8) / 3.8)


def test_aplm_style_stress_case() -> None:
    # no clinical date, earnings in the past, relative volume 0.08, runway 3.06 months
    data = MilestoneInput(
        active_phase_2=1,
        earnings_date=ahead(-30),
        rel_vol=0.08,
        runway_months=3.06,
        next_completion_date=ahead(600),
    )
    result = milestone(data, NOW)
    assert term(result.terms, "earnings").score == 10.0
    assert term(result.terms, "earnings").weight == 2.0
    assert result.boost == 3.0
    assert result.months_to_milestone == pytest.approx(20.0)


def test_fractional_days_follow_now() -> None:
    evening = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)
    # 30 days to midnight is 29.15 days from 20:30, which falls in the 30-day band
    data = MilestoneInput(clinical_date=ahead(30), clinical_phase="phase_3")
    assert term(milestone(data, evening).terms, "clinical_date").score == 8.0
    assert term(milestone(data, NOW).terms, "clinical_date").score == 8.0
    assert (
        term(milestone(MilestoneInput(clinical_date=ahead(31)), NOW).terms, "clinical_date").score
        == 8.0
    )


def test_now_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        milestone(MilestoneInput(), datetime(2026, 10, 12))  # noqa: DTZ001


def test_config_changes_the_floor() -> None:
    assert milestone(MilestoneInput(), NOW, MilestoneConfig(floor_score=4.0)).score == 4.0


def test_non_finite_inputs_are_missing() -> None:
    result = milestone(
        MilestoneInput(active_phase_2=1, eps_next=float("nan"), rel_vol=float("inf")), NOW
    )
    assert {t.name for t in result.terms} == {"active_phase_2"}


def test_display_rounds_half_up() -> None:
    assert display_score("milestone", 8.25) == 8.3
