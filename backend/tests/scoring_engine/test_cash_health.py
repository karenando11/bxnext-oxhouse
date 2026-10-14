"""T2.1 to T2.5 and T2.8 (unit): Cash Health against synthetic inputs."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from scoring_engine import (
    CashHealthConfig,
    CashHealthInput,
    base_score,
    cash_health,
    catalyst_dampener,
    dampener_factor_for_months,
    display_score,
    parse_clinical_date,
    parse_completion_date,
    runway_cap,
)

NOW = date(2026, 10, 12)


def months_out(months: float) -> date:
    return NOW + timedelta(days=round(months * 30))


# ----------------------------------------------------------------------------- T2.1 buckets


@pytest.mark.parametrize(
    ("quick_ratio", "score"),
    [(5.0, 10), (4.99, 8), (3.0, 8), (2.99, 6), (1.5, 6), (1.49, 4), (1.0, 4), (0.99, 2),
     (0.5, 2), (0.49, 0), (0.0, 0)],
)  # fmt: skip
def test_quick_ratio_buckets(quick_ratio: float, score: float) -> None:
    result, terms, _ = base_score(CashHealthInput(quick_ratio=quick_ratio, debt_to_equity=0.0))
    assert terms[0].name == "quick_ratio"
    assert terms[0].score == score
    assert result > 0 or score == 0


@pytest.mark.parametrize(
    ("ratio", "score"),
    [(0.8, 10), (0.79, 8), (0.5, 8), (0.49, 6), (0.3, 6), (0.29, 4), (0.1, 4), (0.09, 2), (0.0, 2)],
)
def test_cash_to_price_buckets(ratio: float, score: float) -> None:
    _, terms, _ = base_score(CashHealthInput(cash_per_share=ratio * 4, price=4.0))
    assert [t.name for t in terms] == ["cash_to_price"]
    assert terms[0].score == score


@pytest.mark.parametrize(
    ("debt", "score"),
    [(-3.0, 10), (0.1, 10), (0.11, 8), (0.3, 8), (0.31, 6), (0.5, 6), (0.51, 4), (1.0, 4),
     (1.01, 2), (2.0, 2), (2.01, 0)],
)  # fmt: skip
def test_debt_to_equity_buckets(debt: float, score: float) -> None:
    _, terms, _ = base_score(CashHealthInput(debt_to_equity=debt))
    assert terms[0].name == "debt_to_equity"
    assert terms[0].score == score


def test_terms_are_a_weighted_average() -> None:
    # quick 10 (w 1.5), cash/price 8 (w 1.0), debt 0 (w 0.8)
    data = CashHealthInput(quick_ratio=6.0, cash_per_share=2.0, price=4.0, debt_to_equity=5.0)
    result, terms, fallback = base_score(data)
    assert [t.weight for t in terms] == [1.5, 1.0, 0.8]
    assert result == pytest.approx((10 * 1.5 + 8 * 1.0 + 0 * 0.8) / 3.3)
    assert fallback is False


def test_missing_terms_are_skipped_not_zero() -> None:
    only_quick, _, _ = base_score(CashHealthInput(quick_ratio=6.0))
    assert only_quick == 10.0  # not diluted by absent terms


def test_missing_zero_or_negative_price_skips_the_cash_term() -> None:
    for price in (None, 0.0, -1.0, math.nan):
        _, terms, _ = base_score(CashHealthInput(cash_per_share=2.0, price=price, quick_ratio=2.0))
        assert [t.name for t in terms] == ["quick_ratio"]


def test_negative_cash_per_share_buckets_by_value() -> None:
    _, terms, _ = base_score(CashHealthInput(cash_per_share=-1.0, price=2.0))
    assert terms[0].score == 2.0


def test_non_finite_values_are_missing() -> None:
    _, terms, _ = base_score(
        CashHealthInput(quick_ratio=math.nan, debt_to_equity=math.inf, price_to_cash=math.nan)
    )
    assert terms == ()


# ------------------------------------------------------------------------ T2.2 P/Cash fallback


def test_pcash_is_added_when_weight_is_below_threshold() -> None:
    # only debt (0.8) is available, below 1.5
    result, terms, fallback = base_score(CashHealthInput(debt_to_equity=0.0, price_to_cash=1.0))
    assert fallback is True
    assert [t.name for t in terms] == ["debt_to_equity", "price_to_cash"]
    assert result == pytest.approx((10 * 0.8 + 9 * 1.0) / 1.8)


def test_pcash_is_not_added_at_exactly_the_threshold() -> None:
    _, terms, fallback = base_score(CashHealthInput(quick_ratio=2.0, price_to_cash=1.0))
    assert fallback is False  # weight 1.5 is not below 1.5
    assert [t.name for t in terms] == ["quick_ratio"]


def test_pcash_with_no_other_term() -> None:
    result, _, fallback = base_score(CashHealthInput(price_to_cash=15.0))
    assert fallback is True
    assert result == 3.0


@pytest.mark.parametrize(
    ("pcash", "score"),
    [(2.0, 9), (2.01, 7), (5.0, 7), (5.01, 5), (10.0, 5), (10.01, 3), (20.0, 3), (20.01, 1)],
)
def test_pcash_buckets(pcash: float, score: float) -> None:
    assert base_score(CashHealthInput(price_to_cash=pcash))[0] == score


def test_no_usable_inputs_gives_the_neutral_base() -> None:
    result, terms, fallback = base_score(CashHealthInput())
    assert (result, terms, fallback) == (5.0, (), False)


# ---------------------------------------------------------------------- T2.3 runway ceiling


@pytest.mark.parametrize(
    ("runway", "cap"),
    [(3.06, 3.0), (5.99, 3.0), (6.0, 5.0), (10.9, 5.0), (12.0, 7.0), (15.0, 7.0), (18.0, None),
     (20.0, None), (None, None), (math.nan, None)],
)  # fmt: skip
def test_runway_ceiling(runway: float | None, cap: float | None) -> None:
    assert runway_cap(runway) == cap


def test_ceiling_only_lowers_the_score() -> None:
    strong = CashHealthInput(quick_ratio=6.0, runway_months=3.0, clinical_date=NOW)
    weak = CashHealthInput(debt_to_equity=5.0, runway_months=3.0, clinical_date=NOW)
    assert cash_health(strong, NOW).score == 3.0
    assert cash_health(weak, NOW).score == 0.0


# --------------------------------------------------------------------------- T2.4 dampener


@pytest.mark.parametrize(
    ("months", "factor"),
    [(0, 1.0), (12, 1.0), (12.1, 0.8), (18, 0.8), (18.1, 0.65), (24, 0.65), (24.1, 0.5)],
)
def test_dampener_bands(months: float, factor: float) -> None:
    assert dampener_factor_for_months(months) == factor


def test_clinical_date_band_boundaries_in_days() -> None:
    def factor(days: int) -> float:
        data = CashHealthInput(clinical_date=NOW + timedelta(days=days))
        return catalyst_dampener(data, NOW)[0]

    assert factor(360) == 1.0  # exactly 12 thirty-day months
    assert factor(361) == 0.8
    assert factor(540) == 0.8
    assert factor(541) == 0.65
    assert factor(720) == 0.65
    assert factor(721) == 0.5


def test_overdue_clinical_date_counts_as_zero_months() -> None:
    factor, reason, _, months = catalyst_dampener(
        CashHealthInput(clinical_date=NOW - timedelta(days=400)), NOW
    )
    assert (factor, reason, months) == (1.0, "clinical_date", 0.0)


def test_clinical_date_wins_over_the_fallback_date() -> None:
    data = CashHealthInput(clinical_date=months_out(30), fallback_completion_date=months_out(1))
    assert catalyst_dampener(data, NOW)[:2] == (0.5, "clinical_date")


# ------------------------------------------------------- T2.5 fallback date and active trials


def test_fallback_date_uses_the_same_bands() -> None:
    data = CashHealthInput(fallback_completion_date=months_out(20))
    factor, reason, used, _ = catalyst_dampener(data, NOW)
    assert (factor, reason, used) == (0.65, "fallback_date", months_out(20))


def test_fallback_date_lapsed_exactly_90_days_still_counts() -> None:
    data = CashHealthInput(fallback_completion_date=NOW - timedelta(days=90))
    assert catalyst_dampener(data, NOW)[:2] == (1.0, "fallback_date")


def test_lapsed_fallback_date_with_active_late_stage_trial_is_neutral() -> None:
    data = CashHealthInput(
        fallback_completion_date=NOW - timedelta(days=91), has_active_late_stage_trial=True
    )
    assert catalyst_dampener(data, NOW)[:2] == (1.0, "active_late_stage")


def test_lapsed_fallback_date_without_active_trial_is_penalised() -> None:
    data = CashHealthInput(fallback_completion_date=NOW - timedelta(days=91))
    assert catalyst_dampener(data, NOW)[:2] == (0.5, "no_catalyst")


def test_no_date_with_active_late_stage_trial_is_neutral() -> None:
    assert catalyst_dampener(CashHealthInput(has_active_late_stage_trial=True), NOW)[0] == 1.0


def test_no_catalyst_of_any_kind() -> None:
    assert catalyst_dampener(CashHealthInput(), NOW) == (0.5, "no_catalyst", None, None)


def test_a_valid_fallback_date_beats_an_active_trial() -> None:
    data = CashHealthInput(
        fallback_completion_date=months_out(30), has_active_late_stage_trial=True
    )
    assert catalyst_dampener(data, NOW)[:2] == (0.5, "fallback_date")


# --------------------------------------------------------------------- date parsing and config


def test_month_only_dates_differ_by_field() -> None:
    assert parse_clinical_date("2028-04") == date(2028, 4, 15)
    assert parse_completion_date("2028-04") == date(2028, 4, 1)
    assert parse_clinical_date("2028-04-30") == date(2028, 4, 30)


@pytest.mark.parametrize("text", [None, "", "  ", "2028", "2028-13", "soon", "2028-02-31"])
def test_unusable_dates_are_none(text: str | None) -> None:
    assert parse_clinical_date(text) is None
    assert parse_completion_date(text) is None


def test_config_changes_the_result() -> None:
    config = CashHealthConfig(factor_no_catalyst=0.9)
    assert cash_health(CashHealthInput(quick_ratio=6.0), NOW, config).score == 9.0


# ------------------------------------------------------------- T2.8 robustness, components


def test_components_explain_the_score() -> None:
    data = CashHealthInput(
        quick_ratio=2.0,
        debt_to_equity=0.4,
        runway_months=10.9,
        fallback_completion_date=months_out(14),
    )
    result = cash_health(data, NOW)
    assert [(t.name, t.score, t.weight) for t in result.terms] == [
        ("quick_ratio", 6.0, 1.5),
        ("debt_to_equity", 6.0, 0.8),
    ]
    assert result.base_score == pytest.approx(6.0)
    assert result.runway_cap == 5.0
    assert result.capped_score == 5.0
    assert (result.dampener_factor, result.dampener_reason) == (0.8, "fallback_date")
    assert result.months_to_date == pytest.approx(14.0, abs=0.04)
    assert result.score == pytest.approx(4.0)


def test_empty_input_does_not_raise() -> None:
    result = cash_health(CashHealthInput(), NOW)
    assert result.score == 2.5  # neutral 5 x no-catalyst 0.5


def test_depends_on_now_only_through_the_argument() -> None:
    data = CashHealthInput(quick_ratio=6.0, clinical_date=date(2027, 10, 7))
    assert cash_health(data, NOW).score == 10.0
    assert cash_health(data, date(2026, 1, 1)).score == 6.5  # about 21 months out
    assert cash_health(data, date(2025, 1, 1)).score == 5.0  # about 34 months out


def test_display_rounds_half_up() -> None:
    assert display_score("cash_health", 1.25) == 1.3
    assert display_score("cash_health", 0.75) == 0.8
