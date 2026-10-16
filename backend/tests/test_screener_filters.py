"""T8.3 to T8.5 (unit): screen filters, reasons, ranking and the near-the-line list."""

from __future__ import annotations

import math

import pytest

from apps.screener.filters import (
    AVG_VOL_MIN,
    AVG_VOL_MISSING,
    COMPOSITE_FLOOR,
    MCAP_MAX,
    MCAP_MIN,
    MCAP_MISSING,
    OUTLIER_FLOOR,
    PRICE_MIN,
    PRICE_MISSING,
    SHARES_MAX,
    SHARES_MISSING,
    SPARSE_GROUP,
    ScreenConfig,
    ScreenConfigError,
    ScreenRow,
    failed_filters,
    near_the_line,
    screen,
)

CONFIG = ScreenConfig()


def row(ticker: str = "AAA", /, **kw: float | bool | None) -> ScreenRow:
    values: dict[str, float | bool | None] = {
        "composite": 6.0, "outlier": 5.0, "is_sparse": False, "mcap_musd": 300.0,
        "price": 5.0, "avg_vol_k": 200.0, "shr_out_m": 100.0,
    }  # fmt: skip
    values.update(kw)
    return ScreenRow(company_id=hash(ticker) % 10_000, ticker=ticker, **values)  # type: ignore[arg-type]


def test_a_company_inside_every_filter_passes() -> None:
    assert failed_filters(row(), CONFIG) == []


def test_the_default_filters() -> None:
    assert CONFIG.to_plain() == {
        "mcap_min_musd": 75.0, "mcap_max_musd": 1000.0, "price_min": 1.0, "avg_vol_min_k": 50.0,
        "shares_outstanding_max_m": 500.0, "composite_floor": 5.0, "outlier_floor": 0.0,
        "include_sparse": True,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("mcap_musd", 74.99, MCAP_MIN),
        ("mcap_musd", 1000.01, MCAP_MAX),
        ("price", 0.99, PRICE_MIN),
        ("avg_vol_k", 49.9, AVG_VOL_MIN),
        ("shr_out_m", 500.1, SHARES_MAX),
        ("composite", 4.99, COMPOSITE_FLOOR),
    ],
)
def test_each_filter_fails_just_past_its_boundary(field: str, value: float, reason: str) -> None:
    assert failed_filters(row(**{field: value}), CONFIG) == [reason]


@pytest.mark.parametrize(
    ("field", "value"),
    [("mcap_musd", 75.0), ("mcap_musd", 1000.0), ("price", 1.0), ("avg_vol_k", 50.0),
     ("shr_out_m", 500.0), ("composite", 5.0), ("outlier", 0.0)],
)  # fmt: skip
def test_each_boundary_value_itself_passes(field: str, value: float) -> None:
    assert failed_filters(row(**{field: value}), CONFIG) == []


def test_every_failing_filter_is_reported_not_only_the_first() -> None:
    reasons = failed_filters(row(price=0.5, avg_vol_k=10.0, composite=2.0, mcap_musd=20.0), CONFIG)
    assert reasons == [MCAP_MIN, PRICE_MIN, AVG_VOL_MIN, COMPOSITE_FLOOR]


@pytest.mark.parametrize(
    ("field", "reason"),
    [("mcap_musd", MCAP_MISSING), ("price", PRICE_MISSING), ("avg_vol_k", AVG_VOL_MISSING),
     ("shr_out_m", SHARES_MISSING)],
)  # fmt: skip
@pytest.mark.parametrize("missing", [None, math.nan, math.inf])
def test_missing_values_exclude_with_a_missing_reason(
    field: str, reason: str, missing: float | None
) -> None:
    assert failed_filters(row(**{field: missing}), CONFIG) == [reason]


def test_a_missing_value_is_never_treated_as_zero() -> None:
    config = ScreenConfig(price_min=None, mcap_min_musd=None, mcap_max_musd=None)
    assert failed_filters(row(price=None, mcap_musd=None), config) == []  # filter off, so no reason


def test_a_company_with_no_market_cap_fails_both_bounds_with_one_reason() -> None:
    assert failed_filters(row(mcap_musd=None), CONFIG) == [MCAP_MISSING]


def test_filters_set_to_none_are_switched_off() -> None:
    config = ScreenConfig(
        mcap_min_musd=None, mcap_max_musd=None, price_min=None, avg_vol_min_k=None,
        shares_outstanding_max_m=None, composite_floor=None, outlier_floor=None,
    )  # fmt: skip
    worst = row(
        composite=0.0, outlier=0.0, mcap_musd=None, price=None, avg_vol_k=None, shr_out_m=None
    )
    assert failed_filters(worst, config) == []


def test_sparse_groups_are_excluded_only_when_asked() -> None:
    sparse = row(is_sparse=True)
    assert failed_filters(sparse, CONFIG) == []
    assert failed_filters(sparse, ScreenConfig(include_sparse=False)) == [SPARSE_GROUP]


def test_the_outlier_floor_applies() -> None:
    config = ScreenConfig(outlier_floor=4.0)
    assert failed_filters(row(outlier=3.9), config) == [OUTLIER_FLOOR]
    assert failed_filters(row(outlier=4.0), config) == []


# ------------------------------------------------------------------------ ranking


def test_passers_are_ranked_by_composite_then_ticker_and_come_first() -> None:
    rows = [
        row("CCC", composite=6.0),
        row("BBB", composite=7.0),
        row("AAA", composite=6.0),
        row("ZZZ", composite=2.0),
        row("DDD", composite=9.0, price=0.1),
    ]
    outcomes = screen(rows, CONFIG)
    got = [(o.row.ticker, o.passed, o.rank) for o in outcomes]
    assert got == [
        ("BBB", True, 1),
        ("AAA", True, 2),
        ("CCC", True, 3),
        ("DDD", False, None),
        ("ZZZ", False, None),
    ]


def test_screening_is_deterministic_and_independent_of_input_order() -> None:
    rows = [row(f"T{i:02d}", composite=5 + (i % 4)) for i in range(20)]
    assert screen(rows, CONFIG) == screen(rows[::-1], CONFIG)


def test_excluded_outcomes_carry_every_reason() -> None:
    outcomes = screen([row(price=0.5, avg_vol_k=1.0)], CONFIG)
    assert outcomes[0].reasons == (PRICE_MIN, AVG_VOL_MIN)
    assert outcomes[0].passed is False


@pytest.mark.parametrize(("floor", "count"), [(5.0, 6), (5.5, 4), (6.0, 2), (8.0, 0)])
def test_the_composite_floor_changes_the_count(floor: float, count: int) -> None:
    rows = [row(f"T{i}", composite=c) for i, c in enumerate([5.0, 5.4, 5.5, 5.9, 6.0, 7.0, 4.0])]
    passed = [o for o in screen(rows, ScreenConfig(composite_floor=floor)) if o.passed]
    assert len(passed) == count


# ------------------------------------------------------------------- near the line


def test_near_the_line_lists_single_failures_and_close_composites() -> None:
    rows = [
        row("PASS", composite=7.0),
        row("PRICE", price=0.5),  # one failure: always near the line
        row("CLOSE", composite=4.6),  # composite only, within 0.5 below the floor
        row("FAR", composite=3.0),  # composite only, too far below
        row("EDGE", composite=4.5),  # exactly 0.5 below
        row("TWO", price=0.5, avg_vol_k=1.0),  # two failures
    ]
    near = near_the_line(screen(rows, CONFIG), CONFIG)
    assert [o.row.ticker for o in near] == ["PRICE", "CLOSE", "EDGE"]  # composite descending
    assert {o.row.ticker for o in near} == {"PRICE", "CLOSE", "EDGE"}


def test_near_the_line_margin_is_configurable_and_ordered() -> None:
    rows = [row("A", composite=4.0), row("B", composite=4.4), row("C", price=0.1, composite=6.0)]
    outcomes = screen(rows, CONFIG)
    assert [o.row.ticker for o in near_the_line(outcomes, CONFIG, margin=1.0)] == ["C", "B", "A"]
    assert [o.row.ticker for o in near_the_line(outcomes, CONFIG, margin=0.1)] == ["C"]


def test_a_non_composite_failure_is_near_the_line_whatever_the_composite() -> None:
    outcomes = screen([row("X", price=0.5, composite=9.9)], CONFIG)
    assert [o.row.ticker for o in near_the_line(outcomes, CONFIG)] == ["X"]


# -------------------------------------------------------------------- configuration


def test_config_round_trips_and_keeps_defaults_for_missing_keys() -> None:
    config = ScreenConfig.from_plain({"composite_floor": 5.5, "price_min": None})
    assert config.composite_floor == 5.5
    assert config.price_min is None
    assert config.mcap_min_musd == 75.0
    assert ScreenConfig.from_plain(config.to_plain()) == config


def test_integers_are_accepted_as_numbers() -> None:
    assert ScreenConfig.from_plain({"mcap_min_musd": 100}).mcap_min_musd == 100.0


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"mcap_min": 1}, "mcap_min"),
        ({"price_min": "1"}, "price_min"),
        ({"price_min": True}, "price_min"),
        ({"composite_floor": math.nan}, "composite_floor"),
        ({"include_sparse": "yes"}, "include_sparse"),
        ({"include_sparse": None}, "include_sparse"),
    ],
)
def test_invalid_config_names_the_key(data: dict[str, object], key: str) -> None:
    with pytest.raises(ScreenConfigError, match=key):
        ScreenConfig.from_plain(data)
