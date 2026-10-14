"""T4.1 (unit): the whole golden suite passes through ``score_universe`` under parity.

Needs the private golden export (see ``conftest.py``); skipped when it is absent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scoring_engine import (
    DIMENSIONS,
    PARITY,
    CompanyInputs,
    CompanyScores,
    display_score,
    round_half_up,
    score_universe,
)

pytestmark = pytest.mark.golden

NOW = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)  # Stage A: 16:30 US Eastern
COMPOSITE_TOLERANCE = 0.045

Row = dict[str, str]


def _number(raw: str) -> float | None:
    return float(raw) if raw else None


def inputs(row: Row) -> CompanyInputs:
    return CompanyInputs(
        company_id=row["ticker"],
        industry=row["indication"] or None,
        mcap=_number(row["mcap"]),
        peer_metrics={d: _number(row[d]) for d in DIMENSIONS},
        quick_ratio=_number(row["quick_ratio"]),
        cash_per_share=_number(row["cash_sh"]),
        price=_number(row["price"]),
        debt_to_equity=_number(row["td_eq"]),
        price_to_cash=_number(row["pcash"]),
        runway_months=_number(row["runway_months"]),
        runway_balance_sheet_date=row["runway_as_of"] or None,
        clinical_date_text=row["catalyst_clinical_date"] or None,
        clinical_overdue=row["catalyst_clinical_overdue"] == "True",
        clinical_phase=row["catalyst_phase"] or None,
        active_phase_3=_number(row["ct_active_phase_3"]),
        active_phase_2=_number(row["ct_active_phase_2"]),
        next_completion_text=row["ct_next_trial_completion"] or None,
        latest_completion_text=row["ct_latest_trial_completion"] or None,
        latest_trial_status=row["ct_latest_trial_status"] or None,
        earnings_date_text=row["earnings_date"] or None,
        eps_next=_number(row["eps_next"]),
        rel_vol=_number(row["rel_vol"]),
        eps_this=None,  # blank in the golden export
    )


@pytest.fixture(scope="module")
def scored(golden_rows: list[Row]) -> dict[str, CompanyScores]:
    return score_universe([inputs(row) for row in golden_rows], NOW, PARITY)


def test_peer_scores_match(golden_rows: list[Row], scored: dict[str, CompanyScores]) -> None:
    wrong = []
    for row in golden_rows:
        topology = scored[row["ticker"]].topology
        if (
            round_half_up(topology.outlier) != float(row["outlier_score"])
            or round_half_up(topology.divergence) != float(row["divergence_direction"])
            or topology.recency != float(row["divergence_recency"])
        ):
            wrong.append(row["ticker"])
    assert len(golden_rows) == 1053
    assert not wrong, wrong[:10]


def test_cash_health_matches(golden_rows: list[Row], scored: dict[str, CompanyScores]) -> None:
    wrong = [
        row["ticker"]
        for row in golden_rows
        if display_score("cash_health", scored[row["ticker"]].cash_health.score)
        != float(row["cash_health"])
    ]
    assert not wrong, wrong[:10]


def test_milestone_matches_where_it_needs_no_eps_this(
    golden_rows: list[Row], scored: dict[str, CompanyScores]
) -> None:
    matched = [
        row
        for row in golden_rows
        if display_score("milestone", scored[row["ticker"]].milestone.score)
        == float(row["milestone_proximity"])
    ]
    assert len(matched) >= 621


def test_composite_matches_where_milestone_matches(
    golden_rows: list[Row], scored: dict[str, CompanyScores]
) -> None:
    checked = 0
    wrong = []
    for row in golden_rows:
        result = scored[row["ticker"]]
        if display_score("milestone", result.milestone.score) != float(row["milestone_proximity"]):
            continue  # depends on eps_this, which the export lacks
        checked += 1
        if abs(result.composite.score - float(row["composite_score"])) > COMPOSITE_TOLERANCE:
            wrong.append(row["ticker"])
    assert checked >= 621
    assert not wrong, wrong[:10]
