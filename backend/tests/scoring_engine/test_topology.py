"""T1.1 to T1.5 (unit): peer-relative scores against synthetic and reference data."""

from __future__ import annotations

import math
import random
import time

import pytest

from scoring_engine import (
    DIMENSIONS,
    CompanyPeerInput,
    TopologyConfig,
    compute_topology,
    group_statistics,
    mcap_bucket,
    median_and_mad,
    peer_group_key,
    recency_from_z,
    scores_from_z,
    topology_scores,
)

# Reference calculator example (OxHouse Screen Snapshot Reference workbook): eight synthetic
# peers and one example ticker, in DIMENSIONS order.
CALCULATOR_PEERS = [
    (12.4, 3.1, -1.2, 48.2, 22.5, 8.1, 9.4),
    (-8.2, -2.0, 0.5, 61.0, 34.0, 11.2, 7.8),
    (31.0, 9.4, 4.2, 55.3, 18.9, 15.6, 12.1),
    (5.1, 1.0, -0.3, 44.0, 41.2, 6.4, 6.9),
    (-14.5, -5.6, -2.1, 38.7, 29.3, 19.8, 10.2),
    (18.9, 4.3, 1.8, 59.1, 25.0, 9.9, 8.6),
    (2.3, 0.4, -0.9, 51.6, 33.8, 12.4, 7.1),
    (-3.7, -1.1, 0.2, 47.9, 27.6, 10.1, 8.9),
]
CALCULATOR_EXAMPLE = (65.2, 14.8, 7.9, 71.4, 12.1, 4.2, 13.8)


def metrics(values: tuple[float, ...]) -> dict[str, float | None]:
    return dict(zip(DIMENSIONS, values, strict=True))


def company(
    company_id: str,
    values: tuple[float | None, ...] | None = None,
    industry: str | None = "Biotechnology",
    mcap: float | None = 50.0,
) -> CompanyPeerInput:
    data: dict[str, float | None] = (
        dict.fromkeys(DIMENSIONS) if values is None else dict(zip(DIMENSIONS, values, strict=True))
    )
    return CompanyPeerInput(company_id, industry, mcap, data)


def varied_group(
    size: int, industry: str = "Biotechnology", mcap: float = 50.0
) -> list[CompanyPeerInput]:
    """A group whose members all have distinct values, so statistics are well defined."""
    return [
        company(
            f"{industry[:3]}{mcap}-{i}",
            tuple(float(i + offset) for offset in range(len(DIMENSIONS))),
            industry,
            mcap,
        )
        for i in range(size)
    ]


# ----------------------------------------------------------------------------- T1.1 buckets


@pytest.mark.parametrize(
    ("mcap", "bucket"),
    [
        (14.95, "Nano"),
        (24.99, "Nano"),
        (25.0, "Micro"),
        (99.9, "Micro"),
        (100.0, "Small"),
        (307.94, "Small"),
        (499.99, "Small"),
        (500.0, "Mid"),
        (1500.0, "Mid"),
        (None, "?"),
        (math.nan, "?"),
    ],
)
def test_market_cap_bucket(mcap: float | None, bucket: str) -> None:
    assert mcap_bucket(mcap) == bucket


@pytest.mark.parametrize(
    ("industry", "mcap", "key"),
    [
        ("Biotechnology", 14.95, "Biotechnology|Nano"),
        ("  Medical Devices  ", 300.0, "Medical Devices|Small"),
        (None, 50.0, "Unknown|Micro"),
        ("   ", None, "Unknown|?"),
    ],
)
def test_peer_group_key(industry: str | None, mcap: float | None, key: str) -> None:
    assert peer_group_key(industry, mcap) == key


# ------------------------------------------------------------------- T1.2 statistics and z-score


def test_median_and_mad_are_unscaled() -> None:
    # deviations from the median 3 are [2, 1, 0, 1, 97]; their median is 1, with no 1.4826 factor
    assert median_and_mad([1.0, 2.0, 3.0, 4.0, 100.0]) == (3.0, 1.0)


def test_dimension_needs_at_least_five_values() -> None:
    four = [metrics((1.0, 1.0, 1.0, 1.0, 1.0, 1.0, float(i))) for i in range(4)]
    five = [metrics((1.0, 1.0, 1.0, 1.0, 1.0, 1.0, float(i))) for i in range(5)]
    assert "vol_m" not in group_statistics(four)
    assert group_statistics(five)["vol_m"].n_values == 5


def test_missing_and_non_finite_values_are_not_counted() -> None:
    group = [{"rsi": v} for v in (1.0, 2.0, 3.0, 4.0, 5.0, None, math.nan, math.inf)]
    assert group_statistics(group)["rsi"].n_values == 5


def test_zero_mad_gives_zero_z() -> None:
    peers = [company(f"P{i}", (5.0,) * 7) for i in range(10)]
    outlier = company("OUT", (99.0,) * 7)
    result = compute_topology([*peers, outlier]).results["OUT"]
    assert all(z == 0.0 for z in result.z_scores.values())
    assert result.outlier == 0.0


def test_calculator_example_z_scores() -> None:
    stats = group_statistics([metrics(v) for v in CALCULATOR_PEERS])
    from scoring_engine import z_scores

    z = z_scores(metrics(CALCULATOR_EXAMPLE), stats)
    expected = [5.971, 5.529, 7.95, 3.805, -3.0, -3.0, 4.208]
    for dimension, value in zip(DIMENSIONS, expected, strict=True):
        assert z[dimension] == pytest.approx(value, abs=1e-3)


# ---------------------------------------------------------------- T1.3 sparse groups and no data


def test_sparse_group_receives_fixed_defaults() -> None:
    result = compute_topology(varied_group(9))
    assert len(result.results) == 9
    for scored in result.results.values():
        assert scored.is_sparse is True
        assert scored.peer_n == 9
        assert (scored.outlier, scored.divergence, scored.recency) == (7.0, 0.0, 1.0)
        assert scored.z_scores == {}
    assert result.group_stats == ()


def test_group_of_exactly_ten_is_not_sparse() -> None:
    result = compute_topology(varied_group(10))
    assert all(not scored.is_sparse for scored in result.results.values())
    assert result.group_stats  # statistics were produced


def test_company_with_no_usable_dimension_gets_the_no_data_outlier() -> None:
    group = [*varied_group(10), company("EMPTY", None)]
    scored = compute_topology(group).results["EMPTY"]
    assert scored.is_sparse is False
    assert (scored.outlier, scored.divergence, scored.recency) == (5.0, 0.0, 0.3)


def test_config_changes_the_sparse_threshold() -> None:
    config = TopologyConfig(min_peers=3)
    assert all(not r.is_sparse for r in compute_topology(varied_group(3), config).results.values())


def test_group_statistics_rows_are_reported_for_non_sparse_groups() -> None:
    output = compute_topology(varied_group(10))
    assert {stat.dimension for stat in output.group_stats} == set(DIMENSIONS)
    assert all(stat.n_values == 10 for stat in output.group_stats)


# ------------------------------------------------------------ T1.3 outlier and divergence formulae


def test_outlier_is_the_scaled_root_mean_square_of_z() -> None:
    z = {"perf_ytd": 1.0, "perf_m": -1.0}
    assert scores_from_z(z).outlier == pytest.approx(2.5)  # sqrt(mean(1, 1)) * 2.5


def test_outlier_is_clamped_to_ten() -> None:
    assert scores_from_z({"rsi": 50.0}).outlier == 10.0


def test_divergence_signed_dimensions_contribute_full_z() -> None:
    assert scores_from_z({"perf_ytd": -2.0, "rsi": 0.5}).divergence == pytest.approx(-1.5)


@pytest.mark.parametrize(
    ("z", "divergence"),
    [
        ({"short_float": 1.0}, 0.0),  # must exceed 1, not equal it
        ({"short_float": 1.5}, 0.45),
        ({"vol_m": -2.0}, 0.6),  # direction is ignored for these dimensions
    ],
)
def test_divergence_other_dimensions_need_abs_z_above_one(
    z: dict[str, float], divergence: float
) -> None:
    assert scores_from_z(z).divergence == pytest.approx(divergence)


# --------------------------------------------------------------------------- T1.4 recency table


@pytest.mark.parametrize(
    ("week", "month", "ytd", "recency"),
    [
        (2.0, 1.0, 0.5, 1.0),
        (0.5, 2.0, 1.0, 0.75),
        (0.5, 0.4, 1.5, 0.5),
        (0.5, 0.4, 0.3, 0.3),
        (1.0, 0.5, 0.2, 0.3),  # week dominates but does not exceed the threshold of 1
        (-2.0, 1.0, 0.5, 1.0),  # absolute values are compared
    ],
)
def test_recency_decision_table(week: float, month: float, ytd: float, recency: float) -> None:
    z = {"perf_w": week, "perf_m": month, "perf_ytd": ytd}
    assert recency_from_z(z) == recency


# ---------------------------------------------------------------------- T1.5 reference example


def test_reference_calculator_example() -> None:
    stats = group_statistics([metrics(v) for v in CALCULATOR_PEERS])
    scores = topology_scores(metrics(CALCULATOR_EXAMPLE), stats)

    assert scores.outlier == 10.0  # raw value is above 10 and is clamped
    assert scores.divergence == pytest.approx(22.418, abs=1e-3)
    assert scores.recency == 1.0


# ----------------------------------------------------------------- determinism and input checks


def test_duplicate_company_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="DUP"):
        compute_topology([company("DUP"), company("DUP")])


def test_results_do_not_depend_on_input_order() -> None:
    universe = varied_group(12) + varied_group(15, "Medical Devices", 400.0)
    shuffled = universe[:]
    random.Random(1).shuffle(shuffled)

    assert compute_topology(universe) == compute_topology(shuffled)


def test_running_twice_gives_identical_results() -> None:
    universe = varied_group(12)
    assert compute_topology(universe) == compute_topology(universe)


def test_full_universe_runs_quickly() -> None:
    rng = random.Random(7)
    industries = [f"Industry {i}" for i in range(11)]
    universe = [
        company(
            f"T{i}",
            tuple(rng.uniform(-50, 100) for _ in DIMENSIONS),
            rng.choice(industries),
            rng.choice([10.0, 60.0, 300.0, 900.0]),
        )
        for i in range(1053)
    ]
    started = time.perf_counter()
    output = compute_topology(universe)
    elapsed = time.perf_counter() - started

    assert len(output.results) == 1053
    assert elapsed < 2.0
