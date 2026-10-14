"""T3.1, T3.6 to T3.10 (unit): the golden universe reproduces the stored Milestone and Composite.

Needs the private golden export (see ``conftest.py``); skipped when it is absent.

``eps_this`` is blank in the golden export. Rows whose Milestone does not depend on it must
match with it absent; every other row must be explained by some ``eps_this`` bucket.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from scoring_engine import (
    DIMENSIONS,
    CompanyPeerInput,
    MilestoneInput,
    cash_health,
    composite,
    compute_topology,
    display_score,
    milestone,
    parse_completion_date,
    parse_earnings_date,
)
from tests.scoring_engine.test_cash_health_golden import cash_input

pytestmark = pytest.mark.golden

# Stage A runs at 16:30 US Eastern, which is 20:30 UTC on the golden day.
NOW = datetime(2026, 10, 12, 20, 30, tzinfo=UTC)
GOLDEN_DAY = date(2026, 10, 12)
# One value per eps_this bucket: 0 or below, 0 to 20, 20 to 50, above 50.
EPS_THIS_SAMPLES = (-5.0, 10.0, 30.0, 80.0)
COMPOSITE_TOLERANCE = 0.045

Row = dict[str, str]


def _number(raw: str) -> float | None:
    return float(raw) if raw else None


def milestone_input(row: Row, eps_this: float | None = None) -> MilestoneInput:
    return MilestoneInput(
        clinical_date=parse_completion_date(row["catalyst_clinical_date"]),
        clinical_overdue=row["catalyst_clinical_overdue"] == "True",
        clinical_phase=row["catalyst_phase"] or None,
        active_phase_3=_number(row["ct_active_phase_3"]),
        active_phase_2=_number(row["ct_active_phase_2"]),
        next_completion_date=parse_completion_date(row["ct_next_trial_completion"]),
        latest_completion_date=parse_completion_date(row["ct_latest_trial_completion"]),
        latest_trial_status=row["ct_latest_trial_status"] or None,
        earnings_date=parse_earnings_date(row["earnings_date"]),
        eps_next=_number(row["eps_next"]),
        rel_vol=_number(row["rel_vol"]),
        eps_this=eps_this,
        runway_months=_number(row["runway_months"]),
    )


def _shown(row: Row, eps_this: float | None) -> float:
    return display_score("milestone", milestone(milestone_input(row, eps_this), NOW).score)


def _matches_without_eps(row: Row) -> bool:
    return _shown(row, None) == float(row["milestone_proximity"])


def test_floor_rows_match(golden_rows: list[Row]) -> None:
    floor = [r for r in golden_rows if float(r["milestone_proximity"]) == 3.0]
    wrong = [r["ticker"] for r in floor if not _matches_without_eps(r)]
    assert len(floor) > 400
    assert not wrong, wrong[:10]


def test_rows_without_an_earnings_date_match(golden_rows: list[Row]) -> None:
    rows = [r for r in golden_rows if not r["earnings_date"]]
    wrong = [r["ticker"] for r in rows if not _matches_without_eps(r)]
    assert rows
    assert not wrong, wrong[:10]


def test_rows_that_do_not_need_eps_this_match(golden_rows: list[Row]) -> None:
    matched = [r for r in golden_rows if _matches_without_eps(r)]
    assert len(matched) >= 621


def test_every_other_row_is_explained_by_an_eps_this_bucket(golden_rows: list[Row]) -> None:
    unexplained = [
        r["ticker"]
        for r in golden_rows
        if not _matches_without_eps(r)
        and not any(
            _shown(r, sample) == float(r["milestone_proximity"]) for sample in EPS_THIS_SAMPLES
        )
    ]
    assert not unexplained, unexplained[:10]


def test_aplm_stress_case(golden_rows: list[Row]) -> None:
    row = next(r for r in golden_rows if r["ticker"] == "APLM")
    result = milestone(milestone_input(row), NOW)
    earnings = next(t for t in result.terms if t.name == "earnings")
    assert (earnings.score, earnings.weight) == (10.0, 2.0)
    assert result.boost == 3.0
    assert display_score("milestone", result.score) == 8.6


def test_composite_matches_within_rounding(golden_rows: list[Row]) -> None:
    output = compute_topology(
        [
            CompanyPeerInput(
                company_id=row["ticker"],
                industry=row["indication"] or None,
                mcap=_number(row["mcap"]),
                metrics={d: _number(row[d]) for d in DIMENSIONS},
            )
            for row in golden_rows
        ]
    ).results

    def best_milestone(row: Row) -> float:
        expected = float(row["milestone_proximity"])
        for sample in (None, *EPS_THIS_SAMPLES):
            value = milestone(milestone_input(row, sample), NOW).score
            if display_score("milestone", value) == expected:
                return value
        raise AssertionError(row["ticker"])

    wrong = []
    for row in golden_rows:
        peers = output[row["ticker"]]
        result = composite(
            cash_health(cash_input(row), GOLDEN_DAY).score,
            best_milestone(row),
            peers.outlier,
            peers.recency,
            peers.divergence,
        )
        stored = float(row["composite_score"])
        if abs(result.score - stored) > COMPOSITE_TOLERANCE:
            wrong.append((row["ticker"], round(result.score, 4), stored))
    assert not wrong, f"{len(wrong)} outside tolerance: {wrong[:10]}"
