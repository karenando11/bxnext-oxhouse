"""T3.9 (unit): the Composite formula on synthetic components."""

from __future__ import annotations

import pytest

from scoring_engine import CompositeConfig, composite, display_score


def test_composite_is_the_weighted_sum() -> None:
    result = composite(cash_health=8.0, milestone=6.0, outlier=4.0, recency=0.75, divergence=2.0)
    expected = 0.30 * 8 + 0.35 * 6 + 0.15 * 4 + 0.10 * 7.5 + 0.10 * 1.0
    assert result.score == pytest.approx(expected)
    assert result.raw == pytest.approx(expected)


def test_recency_is_scaled_to_ten_before_weighting() -> None:
    assert composite(0, 0, 0, 1.0, 0).score == pytest.approx(1.0)
    assert composite(0, 0, 0, 0.3, 0).score == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("divergence", "term"),
    [(0.0, 0.0), (2.0, 1.0), (4.0, 2.0), (50.0, 2.0), (-4.0, -2.0), (-50.0, -2.0), (-1.0, -0.5)],
)
def test_divergence_term_is_halved_and_clamped(divergence: float, term: float) -> None:
    result = composite(5, 5, 5, 0.5, divergence)
    assert result.divergence_term == term
    assert result.divergence_part == pytest.approx(0.10 * term)


def test_result_is_clamped_to_zero_and_ten() -> None:
    heavy = CompositeConfig(cash_weight=0.60)
    assert composite(10, 10, 10, 1.0, 50.0, heavy).score == 10.0
    assert composite(10, 10, 10, 1.0, 50.0, heavy).raw > 10.0
    assert composite(0, 0, 0, 0.0, -50.0).score == 0.0
    assert composite(0, 0, 0, 0.0, -50.0).raw == pytest.approx(-0.2)


def test_it_uses_unrounded_components() -> None:
    result = composite(7.04, 5.04, 3.04, 0.5, 0.0)
    rounded = composite(7.0, 5.0, 3.0, 0.5, 0.0)
    assert result.score != rounded.score


def test_parts_add_up_to_the_raw_score() -> None:
    result = composite(6.3, 4.4, 8.1, 0.75, -1.2)
    parts = (
        result.cash_part
        + result.milestone_part
        + result.outlier_part
        + result.recency_part
        + result.divergence_part
    )
    assert parts == pytest.approx(result.raw)


def test_config_changes_the_weights() -> None:
    config = CompositeConfig(cash_weight=1.0, milestone_weight=0.0, outlier_weight=0.0,
                             recency_weight=0.0, divergence_weight=0.0)  # fmt: skip
    assert composite(6.0, 9.0, 9.0, 1.0, 9.0, config).score == 6.0


def test_display_rounds_half_up() -> None:
    assert display_score("composite", 5.25) == 5.3
