"""T2.6 and T2.7 (unit): the golden universe reproduces every stored Cash Health value.

Needs the private golden export (see ``conftest.py``); skipped when it is absent.
"""

from __future__ import annotations

from datetime import date

import pytest

from scoring_engine import (
    CashHealthInput,
    cash_health,
    display_score,
    parse_clinical_date,
    parse_completion_date,
)

pytestmark = pytest.mark.golden

GOLDEN_DAY = date(2026, 10, 12)
Row = dict[str, str]


def _number(raw: str) -> float | None:
    return float(raw) if raw else None


def cash_input(row: Row) -> CashHealthInput:
    active = (_number(row["ct_active_phase_2"]) or 0) + (_number(row["ct_active_phase_3"]) or 0)
    return CashHealthInput(
        quick_ratio=_number(row["quick_ratio"]),
        cash_per_share=_number(row["cash_sh"]),
        price=_number(row["price"]),
        debt_to_equity=_number(row["td_eq"]),
        price_to_cash=_number(row["pcash"]),
        runway_months=_number(row["runway_months"]),
        clinical_date=parse_clinical_date(row["catalyst_clinical_date"]),
        fallback_completion_date=parse_completion_date(
            row["ct_next_trial_completion"] or row["ct_latest_trial_completion"]
        ),
        has_active_late_stage_trial=active > 0,
    )


def test_every_row_matches(golden_rows: list[Row]) -> None:
    wrong = []
    for row in golden_rows:
        expected = _number(row["cash_health"])
        if expected is None:
            continue
        shown = display_score("cash_health", cash_health(cash_input(row), GOLDEN_DAY).score)
        if shown != expected:
            wrong.append((row["ticker"], shown, expected))
    assert not wrong, f"{len(wrong)} of {len(golden_rows)} differ: {wrong[:10]}"


def test_every_company_has_a_stored_value(golden_rows: list[Row]) -> None:
    assert all(row["cash_health"] for row in golden_rows)


@pytest.mark.parametrize(
    ("ticker", "score"),
    [("KTTA", 10.0), ("SMMT", 4.9), ("APLM", 1.3), ("YDES", 2.5), ("PACS", 0.8)],
)
def test_fixture_tickers(golden_rows: list[Row], ticker: str, score: float) -> None:
    row = next(r for r in golden_rows if r["ticker"] == ticker)
    result = cash_health(cash_input(row), GOLDEN_DAY)
    assert display_score("cash_health", result.score) == score


def test_ktta_uses_the_fallback_date(golden_rows: list[Row]) -> None:
    row = next(r for r in golden_rows if r["ticker"] == "KTTA")
    result = cash_health(cash_input(row), GOLDEN_DAY)
    assert result.dampener_reason == "fallback_date"
    assert result.dampener_factor == 1.0


def test_aplm_runway_cap_does_not_bind(golden_rows: list[Row]) -> None:
    row = next(r for r in golden_rows if r["ticker"] == "APLM")
    result = cash_health(cash_input(row), GOLDEN_DAY)
    assert result.runway_cap == 3.0
    assert result.capped_score == result.base_score
    assert result.dampener_factor == 0.5
