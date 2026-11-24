"""T20.1 to T20.4: the daily diff against the current system and the pass condition."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from apps.platform import parallel
from apps.platform.models import ParallelDiff
from apps.platform.parallel import (
    CARRIED,
    QUIRK,
    TIMING,
    UNEXPLAINED,
    checklist,
    compare_day,
    evaluate_pass,
    parse_cell,
    read_export,
)
from apps.web.models import CompanyPage

pytestmark = pytest.mark.django_db

DAY = date(2026, 10, 12)  # a Monday


def page(ticker: str = "AAA", **kw: Any) -> CompanyPage:
    """An unsaved read-model row that matches ``old()`` unless a value is overridden."""
    values: dict[str, Any] = {
        "ticker": ticker,
        "company_name": "A Co",
        "industry": "Biotechnology",
        "as_of_date": DAY,
        "composite": 7.4162,
        "cash_health": 9.1,
        "milestone": 7.1,
        "outlier": 10.0,
        "recency": 0.5,
        "peer_group_key": "Biotechnology|Micro",
        "peer_n": 94,
        "price": 5.39,
        "mcap_musd": 307.94,
        "runway_months": 46.68,
        "runway_calc_date": date(2026, 10, 10),
        "total_studies": 1,
        "active_phase_2": 0,
        "active_phase_3": 1,
        "latest_trial_nct": "NCT00000001",
        "latest_trial_status": "RECRUITING",
        "clinical_date": date(2027, 6, 30),
        "clinical_overdue": False,
        "earnings_date_text": "8/12/2026 8:30:00 AM",
        "score_input_resolution": {
            "runway": {"state": "fresh"},
            "catalyst": {"state": "fresh"},
        },
    }
    values.update(kw)
    return CompanyPage(**values)


def old(ticker: str = "AAA", **kw: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "ticker": ticker,
        "indication": "Biotechnology",
        "composite_score": 7.4162,
        "cash_health": 9.1,
        "milestone_proximity": 7.1,
        "outlier_score": 10.0,
        "divergence_recency": 0.5,
        "peer_group": "Biotechnology|Micro",
        "peer_n": 94.0,
        "price": 5.39,
        "mcap": 307.94,
        "runway_months": 46.68,
        "runway_quarters": 15.0,  # the old system floors months / 3
        "runway_rating": 5.0,
        "runway_as_of": "2026-10-10",
        "ct_total_studies": 1.0,
        "ct_active_phase_2": 0.0,
        "ct_active_phase_3": 1.0,
        "ct_latest_trial_nct": "NCT00000001",
        "ct_latest_trial_status": "RECRUITING",
        "catalyst_clinical_date": "2027-06-30",
        "catalyst_clinical_overdue": False,
        "earnings_date": "8/12/2026 8:30:00 AM",
    }
    values.update(kw)
    return values


def diffs(report: dict[str, Any]) -> dict[str, str]:
    return {d["field"]: d["class"] for d in (*report["score_diffs"], *report["field_diffs"])}


# --- reading ---------------------------------------------------------------------------------


def test_cells_parse_blank_numbers_and_booleans() -> None:
    assert [parse_cell(x) for x in ("", "-", " 5.5 ", "True", "abc", "nan", None)] == [
        None,
        None,
        5.5,
        True,
        "abc",
        None,
        None,
    ]


def test_an_export_is_read_by_ticker(tmp_path: Path) -> None:
    path = tmp_path / "e.csv"
    path.write_text("ticker,price,peer_group\nabc,5.5,x\nABC,9,y\nDEF,,z\n")
    rows = read_export(path)
    assert rows["ABC"]["price"] == 5.5  # the first row wins
    assert rows["DEF"]["price"] is None


# --- T20.4 fields: counts exact, ratios within display rounding -------------------------------


def test_a_matching_day_has_no_differences() -> None:
    report = compare_day(DAY, {"AAA": old()}, [page()])
    assert report["score_diffs"] == [] and report["field_diffs"] == []
    assert report["share_within_tolerance"] == 1.0


def test_ratios_match_within_display_rounding_and_counts_exactly() -> None:
    inside = compare_day(DAY, {"AAA": old(price=5.394, mcap=308.3)}, [page()])
    assert diffs(inside) == {}
    outside = compare_day(DAY, {"AAA": old(price=5.40, ct_total_studies=2.0)}, [page()])
    assert set(diffs(outside)) == {"price", "ct_total_studies"}


def test_a_blank_on_one_side_is_a_difference() -> None:
    report = compare_day(DAY, {"AAA": old(price=None)}, [page()])
    assert diffs(report) == {"price": UNEXPLAINED}
    assert compare_day(DAY, {"AAA": old(price=None)}, [page(price=None)])["field_diffs"] == []


def test_runway_quarters_and_rating_compare_in_the_old_systems_terms() -> None:
    # 46.68 months: the old system shows 15 quarters (floor) and rating 5; we show Long
    assert diffs(compare_day(DAY, {"AAA": old()}, [page()])) == {}
    wrong = compare_day(DAY, {"AAA": old(runway_rating=2.0)}, [page()])
    assert "runway_rating" in diffs(wrong)
    low = compare_day(
        DAY,
        {"AAA": old(runway_months=10.0, runway_quarters=3.0, runway_rating=2.0)},
        [page(runway_months=10.0)],
    )
    assert diffs(low) == {}


# --- T20.1 scores and classification ----------------------------------------------------------


def test_a_score_inside_the_tolerance_is_within_and_outside_is_not() -> None:
    near = compare_day(DAY, {"AAA": old(composite_score=7.45)}, [page()])
    assert near["within_tolerance"] == 1
    far = compare_day(DAY, {"AAA": old(composite_score=7.6)}, [page()])
    assert far["within_tolerance"] == 0
    assert far["score_diffs"][0]["delta"] == pytest.approx(-0.1838)


def test_a_carried_runway_explains_the_runway_and_the_scores_built_on_it() -> None:
    row = page(
        runway_months=30.0,
        cash_health=8.0,
        composite=6.9,
        score_input_resolution={"runway": {"state": "carried"}, "catalyst": {"state": "fresh"}},
    )
    report = compare_day(
        DAY,
        {"AAA": old(runway_months=40.0, runway_quarters=13.0, cash_health=9.1)},
        [row],
    )
    kinds = diffs(report)
    assert kinds["runway_months"] == CARRIED
    assert kinds["cash_health"] == CARRIED
    assert kinds["composite_score"] == CARRIED


def test_a_carried_catalyst_explains_the_milestone() -> None:
    row = page(
        milestone=5.0,
        score_input_resolution={"runway": {"state": "fresh"}, "catalyst": {"state": "carried"}},
    )
    assert diffs(compare_day(DAY, {"AAA": old()}, [row]))["milestone_proximity"] == CARRIED


def test_a_large_milestone_gap_is_not_a_clock_effect() -> None:
    kinds = diffs(compare_day(DAY, {"AAA": old()}, [page(milestone=5.0, composite=6.0)]))
    assert kinds["milestone_proximity"] == UNEXPLAINED
    assert kinds["composite_score"] == UNEXPLAINED


def test_milestone_and_catalyst_dates_are_timing_differences() -> None:
    row = page(milestone=6.8, composite=7.2, clinical_date=date(2027, 7, 15))
    kinds = diffs(compare_day(DAY, {"AAA": old()}, [row]))
    assert kinds["milestone_proximity"] == TIMING
    assert kinds["composite_score"] == TIMING  # only milestone moved it
    assert kinds["catalyst_clinical_date"] == TIMING


def test_a_composite_difference_with_no_component_difference_is_unexplained() -> None:
    assert diffs(compare_day(DAY, {"AAA": old()}, [page(composite=6.0)])) == {
        "composite_score": UNEXPLAINED
    }


def test_a_runway_row_of_another_date_is_timing_and_the_same_date_is_not() -> None:
    other = compare_day(DAY, {"AAA": old(runway_as_of="2026-10-09")}, [page(runway_months=40.0)])
    assert diffs(other)["runway_months"] == TIMING
    same = compare_day(DAY, {"AAA": old()}, [page(runway_months=40.0)])
    assert diffs(same)["runway_months"] == UNEXPLAINED


def test_a_price_difference_is_never_explained_by_timing() -> None:
    assert diffs(compare_day(DAY, {"AAA": old()}, [page(price=6.0)])) == {"price": UNEXPLAINED}


def test_a_known_quirk_fix_is_classified_by_setting() -> None:
    from tests import factories as f

    f.setting(key="parallel.known_quirk_fixes", value=["AAA:price", "*:peer_n"])
    report = compare_day(
        DAY, {"AAA": old(), "BBB": old("BBB")}, [page(price=6.0, peer_n=10), page("BBB", peer_n=9)]
    )
    got = {(d["ticker"], d["field"]): d["class"] for d in report["field_diffs"]}
    assert got[("AAA", "price")] == QUIRK
    assert got[("AAA", "peer_n")] == QUIRK
    assert got[("BBB", "peer_n")] == QUIRK


def test_companies_on_one_side_only_are_listed() -> None:
    report = compare_day(DAY, {"AAA": old(), "OLD": old("OLD")}, [page(), page("NEW")])
    assert report["only_in_export"] == ["OLD"] and report["only_here"] == ["NEW"]
    assert report["compared"] == 1
    assert report["share_within_tolerance"] == 0.5  # out of the export's two companies


# --- the pass condition -----------------------------------------------------------------------


def good_report(
    day: date, share: float = 1.0, unexplained: Sequence[tuple[str, str]] = ()
) -> dict[str, Any]:
    return {
        "share_within_tolerance": share,
        "score_diffs": [],
        "field_diffs": [{"ticker": t, "field": f, "class": UNEXPLAINED} for t, f in unexplained],
    }


def window(end: date = date(2026, 10, 30)) -> list[date]:
    return parallel.trading_window(end)


def test_the_window_is_fourteen_trading_days() -> None:
    days = window()
    assert len(days) == 14 and days[-1] == date(2026, 10, 30)
    assert all(d.weekday() < 5 for d in days)


def test_fourteen_good_days_pass() -> None:
    result = evaluate_pass({d: good_report(d) for d in window()})
    assert result["passed"] is True


def test_two_bad_days_pass_and_three_fail() -> None:
    days = window()
    two = {d: good_report(d, 0.5 if i < 2 else 1.0) for i, d in enumerate(days)}
    three = {d: good_report(d, 0.5 if i < 3 else 1.0) for i, d in enumerate(days)}
    assert evaluate_pass(two)["passed"] is True
    result = evaluate_pass(three)
    assert result["passed"] is False and result["good_days"] == 11


def test_a_day_at_exactly_99_percent_counts_as_good() -> None:
    assert evaluate_pass({d: good_report(d, 0.99) for d in window()})["passed"] is True
    assert evaluate_pass({d: good_report(d, 0.989) for d in window()})["passed"] is False


def test_a_missing_day_fails_the_window() -> None:
    days = window()
    reports = {d: good_report(d) for d in days[1:]}
    result = evaluate_pass(reports)
    assert result["passed"] is False
    assert result["missing_days"] == [days[0].isoformat()]


def test_a_difference_unexplained_for_more_than_two_days_fails() -> None:
    days = window()
    key = [("AAA", "price")]
    twice = {d: good_report(d, 1.0, key if i < 2 else []) for i, d in enumerate(days)}
    thrice = {d: good_report(d, 1.0, key if i < 3 else []) for i, d in enumerate(days)}
    assert evaluate_pass(twice)["passed"] is True
    result = evaluate_pass(thrice)
    assert result["passed"] is False
    assert result["unexplained_more_than_2_days"] == ["AAA:price"]


def test_nothing_reported_is_not_a_pass() -> None:
    assert evaluate_pass({})["passed"] is False


def test_the_checklist_shows_each_check_and_the_signature_lines() -> None:
    text = checklist(evaluate_pass({d: good_report(d) for d in window()}))
    assert text.count("[x]") == 4 and "Result: PASS" in text
    assert "Product owner:" in text and "Engineer:" in text
    partial = checklist(evaluate_pass({window()[-1]: good_report(window()[-1])}))
    assert "[ ] The diff report ran on all 14 trading days (missing:" in partial


# --- the commands -----------------------------------------------------------------------------


def test_the_commands_store_a_report_and_evaluate_it(tmp_path: Path, capsys: Any) -> None:
    from tests import factories as f

    run = f.score_run(as_of_date=DAY)
    company = f.company()
    f.ticker(company, ticker="AAA")
    f.score_result(run, company)
    path = tmp_path / "current.csv"
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ticker", "composite_score"])
        writer.writerow(["AAA", "9.9"])
    call_command("compare_day", export=str(path), date=DAY.isoformat())
    out = capsys.readouterr().out
    assert "1 companies compared" in out and "composite_score" in out
    stored = ParallelDiff.objects.get(day=DAY).report
    assert stored["by_class"][UNEXPLAINED] >= 1
    call_command("parallel_status")
    assert "passed: False" in capsys.readouterr().out
    call_command("parallel_status", checklist=True)
    assert "Result: NOT YET" in capsys.readouterr().out
    # a day the read model does not serve is an error, not an empty report
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="no rows"):
        call_command("compare_day", export=str(path), date=(DAY - timedelta(days=3)).isoformat())
