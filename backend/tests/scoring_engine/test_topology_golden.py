"""T1.6 (unit): the golden universe of 1,053 companies reproduces every stored score.

Needs the private golden export (see ``conftest.py``); skipped when it is absent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from scoring_engine import (
    DIMENSIONS,
    CompanyPeerInput,
    TopologyOutput,
    compute_topology,
    round_half_up,
)

pytestmark = pytest.mark.golden

Row = dict[str, str]


@dataclass(frozen=True)
class Scored:
    output: TopologyOutput
    seconds: float


def _number(raw: str) -> float | None:
    return float(raw) if raw else None


def _companies(rows: list[Row]) -> list[CompanyPeerInput]:
    return [
        CompanyPeerInput(
            company_id=row["ticker"],
            industry=row["indication"] or None,  # the Finviz industry
            mcap=_number(row["mcap"]),
            metrics={dimension: _number(row[dimension]) for dimension in DIMENSIONS},
        )
        for row in rows
    ]


@pytest.fixture(scope="module")
def scored(golden_rows: list[Row]) -> Scored:
    started = time.perf_counter()
    output = compute_topology(_companies(golden_rows))
    return Scored(output, time.perf_counter() - started)


def test_universe_has_the_expected_size(golden_rows: list[Row]) -> None:
    assert len(golden_rows) == 1053


def test_peer_groups_match(golden_rows: list[Row], scored: Scored) -> None:
    wrong = [
        row["ticker"]
        for row in golden_rows
        if (result := scored.output.results[row["ticker"]]).peer_group_key != row["peer_group"]
        or result.peer_n != int(row["peer_n"])
        or result.is_sparse != (row["sparse_cluster"] == "True")
    ]
    assert wrong == []


def test_outlier_matches_all_rows(golden_rows: list[Row], scored: Scored) -> None:
    wrong = [
        row["ticker"]
        for row in golden_rows
        if round_half_up(scored.output.results[row["ticker"]].outlier, 1)
        != float(row["outlier_score"])
    ]
    assert wrong == []


def test_divergence_matches_all_rows(golden_rows: list[Row], scored: Scored) -> None:
    wrong = [
        row["ticker"]
        for row in golden_rows
        if round_half_up(scored.output.results[row["ticker"]].divergence, 1)
        != float(row["divergence_direction"])
    ]
    assert wrong == []


def test_recency_matches_all_rows_exactly(golden_rows: list[Row], scored: Scored) -> None:
    wrong = [
        row["ticker"]
        for row in golden_rows
        if scored.output.results[row["ticker"]].recency != float(row["divergence_recency"])
    ]
    assert wrong == []


def test_full_universe_scores_in_under_two_seconds(scored: Scored) -> None:
    assert scored.seconds < 2.0
