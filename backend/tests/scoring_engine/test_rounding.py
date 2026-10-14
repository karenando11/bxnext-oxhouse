"""T1.7 (unit): display rounding follows the existing system's behaviour."""

from __future__ import annotations

import math

import pytest

from scoring_engine import display_score, round_half_up


@pytest.mark.parametrize(
    ("value", "ndigits", "expected"),
    [
        (1.25, 1, 1.3),  # exactly representable tie: rounds up
        (4.55, 1, 4.5),  # stored as 4.5499999999999998: rounds down
        (0.75, 1, 0.8),
        (-0.05, 1, -0.1),  # ties go away from zero
        (2.5, 0, 3.0),
        (-2.5, 0, -3.0),
        (1.005, 2, 1.0),  # stored just below the tie
        (22.4178, 1, 22.4),
        (10.0, 1, 10.0),
    ],
)
def test_round_half_up(value: float, ndigits: int, expected: float) -> None:
    assert round_half_up(value, ndigits) == expected


@pytest.mark.parametrize("value", [-0.04, -0.0, 0.04])
def test_values_that_round_to_zero_never_display_as_negative_zero(value: float) -> None:
    result = round_half_up(value, 1)
    assert result == 0.0
    assert math.copysign(1.0, result) == 1.0


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_values_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        round_half_up(value)


def test_recency_is_displayed_unrounded() -> None:
    assert display_score("recency", 0.75) == 0.75


def test_outlier_and_divergence_are_displayed_to_one_decimal() -> None:
    assert display_score("outlier", 1.25) == 1.3
    assert display_score("divergence", 22.4178) == 22.4


def test_unknown_score_names_are_rejected() -> None:
    with pytest.raises(KeyError):
        display_score("not_a_score", 1.0)
