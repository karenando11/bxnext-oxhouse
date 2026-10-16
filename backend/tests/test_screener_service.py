"""T8.1 to T8.7 (integration): screens over stored scores, the default screen and the funnel."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.companies.models import Company
from apps.platform.golden import load_golden
from apps.scoring.models import ScoreResult, ScoreRun
from apps.scoring.service import latest_complete_run, run_scoring
from apps.screener.filters import ScreenConfig, near_the_line
from apps.screener.models import FunnelEvent, ScreenMember, ScreenRun
from apps.screener.service import (
    adhoc_screen,
    load_rows,
    materialise_default_screen,
    previous_screen_run,
)
from tests import factories as f

DAY1 = date(2026, 10, 12)
DAY2 = DAY1 + timedelta(days=1)


def scored_company(run: ScoreRun, ticker: str, composite: float, **snapshot: float) -> Company:
    company = f.company(name=f"{ticker} Inc")
    f.ticker(company, ticker=ticker)
    values = {"mcap_musd": 300.0, "price": 5.0, "avg_vol_k": 200.0, "shr_out_m": 100.0, **snapshot}
    f.snapshot(company, as_of_date=run.finviz_as_of_date, **values)
    f.score_result(run, company, composite=composite)
    return company


def add_to_run(run: ScoreRun, company: Company, composite: float, **snapshot: float) -> None:
    values = {"mcap_musd": 300.0, "price": 5.0, "avg_vol_k": 200.0, "shr_out_m": 100.0, **snapshot}
    f.snapshot(company, as_of_date=run.finviz_as_of_date, **values)
    f.score_result(run, company, composite=composite)


def new_run(day: date) -> ScoreRun:
    return f.score_run(as_of_date=day, finviz_as_of_date=day)


# ---------------------------------------------------------------------- ad-hoc screens


@pytest.mark.django_db
def test_an_adhoc_screen_reads_stored_scores_and_writes_nothing() -> None:
    run = new_run(DAY1)
    scored_company(run, "AAA", 7.0)
    scored_company(run, "BBB", 4.0)
    scored_company(run, "CCC", 6.0, price=0.5)
    before = (ScoreResult.objects.count(), ScreenRun.objects.count(), ScreenMember.objects.count())
    outcomes = adhoc_screen(run, ScreenConfig())
    assert [(o.row.ticker, o.passed) for o in outcomes] == [
        ("AAA", True),
        ("BBB", False),
        ("CCC", False),
    ]
    assert outcomes[2].reasons == ("price_min",)
    assert (
        ScoreResult.objects.count(),
        ScreenRun.objects.count(),
        ScreenMember.objects.count(),
    ) == before


@pytest.mark.django_db
def test_many_filter_sets_reuse_one_load_without_further_queries() -> None:
    run = new_run(DAY1)
    for i in range(5):
        scored_company(run, f"T{i}", 5.0 + i / 2)
    rows = load_rows(run)
    with CaptureQueriesContext(connection) as queries:
        for floor in (5.0, 5.5, 6.0, 6.5):
            adhoc_screen(run, ScreenConfig(composite_floor=floor), rows)
    assert len(queries) == 0


@pytest.mark.django_db
def test_rows_use_the_snapshot_the_run_scored() -> None:
    run = new_run(DAY1)
    company = scored_company(run, "AAA", 6.0, price=5.0)
    f.snapshot(company, as_of_date=DAY2, price=0.5)  # a later snapshot must not leak in
    assert load_rows(run)[0].price == 5.0


@pytest.mark.django_db
def test_a_company_without_a_snapshot_is_excluded_for_missing_fields() -> None:
    run = new_run(DAY1)
    company = f.company()
    f.ticker(company, ticker="NOSNAP")
    f.score_result(run, company, composite=8.0)
    outcome = adhoc_screen(run, ScreenConfig())[0]
    assert outcome.reasons == (
        "mcap_missing",
        "price_missing",
        "avg_vol_missing",
        "shares_outstanding_missing",
    )


# ----------------------------------------------------------- the stored default screen


@pytest.mark.django_db
def test_the_default_screen_is_stored_with_every_member() -> None:
    run = new_run(DAY1)
    scored_company(run, "AAA", 7.0)
    scored_company(run, "BBB", 6.0)
    scored_company(run, "CCC", 3.0, price=0.5)
    stored = materialise_default_screen(run)
    assert (stored.universe_n, stored.passed_n) == (3, 2)
    assert stored.definition.is_default is True
    assert stored.filter_config["composite_floor"] == 5.0
    members = {
        m.company.tickers.get().ticker: m for m in ScreenMember.objects.filter(screen_run=stored)
    }
    assert (members["AAA"].passed, members["AAA"].rank) == (True, 1)
    assert (members["BBB"].passed, members["BBB"].rank) == (True, 2)
    assert (members["CCC"].passed, members["CCC"].rank) == (False, None)
    assert members["CCC"].excluded_reasons == ["price_min", "composite_floor"]


@pytest.mark.django_db
def test_materialising_again_replaces_the_screen_of_that_run() -> None:
    run = new_run(DAY1)
    scored_company(run, "AAA", 7.0)
    first = materialise_default_screen(run)
    second = materialise_default_screen(run)
    assert first.pk != second.pk
    assert ScreenRun.objects.filter(score_run=run).count() == 1
    assert ScreenMember.objects.count() == 1


# ----------------------------------------------------------------------- funnel events


@pytest.mark.django_db
def test_funnel_records_who_entered_and_left_between_two_days() -> None:
    day1 = new_run(DAY1)
    stays = scored_company(day1, "STAY", 7.0)
    leaves = scored_company(day1, "LEAVE", 6.0)
    enters = scored_company(day1, "ENTER", 3.0)  # excluded on the composite floor
    gone = scored_company(day1, "GONE", 6.0)  # drops out of the universe
    scored_company(day1, "NEVER", 2.0)
    assert materialise_default_screen(day1)  # the first day has nothing to compare with
    assert FunnelEvent.objects.count() == 0

    day2 = new_run(DAY2)
    add_to_run(day2, stays, 7.5)
    add_to_run(day2, leaves, 6.0, price=0.5, avg_vol_k=10.0)  # now fails two filters
    add_to_run(day2, enters, 5.5)
    newcomer = f.company()
    f.ticker(newcomer, ticker="NEW")
    add_to_run(day2, newcomer, 6.5)
    never = Company.objects.get(tickers__ticker="NEVER")
    add_to_run(day2, never, 2.0)
    materialise_default_screen(day2)

    events = {
        e.company.tickers.get().ticker: (e.from_state, e.to_state, e.reason)
        for e in FunnelEvent.objects.filter(as_of_date=DAY2)
    }
    assert events == {
        "LEAVE": ("passed", "excluded", "price_min,avg_vol_min"),
        "ENTER": ("excluded", "passed", "passed_all_filters"),
        "NEW": ("absent", "passed", "new_to_universe"),
        "GONE": ("passed", "absent", "left_universe"),
    }
    assert "STAY" not in events
    assert "NEVER" not in events
    assert gone.pk


@pytest.mark.django_db
def test_funnel_events_are_not_duplicated_when_a_day_is_screened_again() -> None:
    day1 = new_run(DAY1)
    leaves = scored_company(day1, "LEAVE", 6.0)
    materialise_default_screen(day1)
    day2 = new_run(DAY2)
    add_to_run(day2, leaves, 2.0)
    materialise_default_screen(day2)
    materialise_default_screen(day2)
    assert FunnelEvent.objects.count() == 1


@pytest.mark.django_db
def test_the_previous_screen_is_the_latest_earlier_day() -> None:
    run1, run2 = new_run(DAY1), new_run(DAY1 + timedelta(days=3))
    scored_company(run1, "A", 6.0)
    scored_company(run2, "A2", 6.0)
    first = materialise_default_screen(run1)
    second = materialise_default_screen(run2)
    assert previous_screen_run(first) is None
    assert previous_screen_run(second) == first


# --------------------------------------------------------------------------- near the line


@pytest.mark.django_db
def test_near_the_line_over_stored_scores() -> None:
    run = new_run(DAY1)
    scored_company(run, "PASS", 7.0)
    scored_company(run, "CLOSE", 4.7)
    scored_company(run, "FAR", 3.0)
    scored_company(run, "PRICE", 6.0, price=0.5)
    config = ScreenConfig()
    near = near_the_line(adhoc_screen(run, config), config)
    assert [o.row.ticker for o in near] == ["PRICE", "CLOSE"]


# ----------------------------------------------------------------------------- command


@pytest.mark.django_db
def test_the_screen_day_command(capsys: pytest.CaptureFixture[str]) -> None:
    run = new_run(DAY1)
    scored_company(run, "AAA", 7.0)
    call_command("screen_day", "2026-10-12")
    assert "1 of 1 companies passed" in capsys.readouterr().out
    with pytest.raises(CommandError, match="Not a date"):
        call_command("screen_day", "soon")
    with pytest.raises(CommandError, match="No completed score run"):
        call_command("screen_day", "2020-01-01")


# ----------------------------------------------------------------------------- golden


def _golden(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def golden_day(golden_path: Path, runway_path: Path) -> tuple[ScoreRun, list[dict[str, str]]]:
    """The golden day scored, with the stored composite put back into each result.

    The export has ``eps_this`` blank, so our own composite differs slightly from the stored one
    for some rows. These tests check the filter logic against the counts the stored composite gives.
    """
    load_golden(golden_path, runway_path)
    run = run_scoring(DAY1)
    rows = _golden(golden_path)
    stored = {r["ticker"]: float(r["composite_score"]) for r in rows}
    results = list(ScoreResult.objects.filter(score_run=run).prefetch_related("company__tickers"))
    for result in results:
        result.composite = stored[result.company.tickers.get().ticker]
    ScoreResult.objects.bulk_update(results, ["composite"])
    return run, rows


@pytest.mark.golden
@pytest.mark.django_db
def test_default_screen_counts_on_the_golden_day(
    golden_day: tuple[ScoreRun, list[dict[str, str]]],
) -> None:
    run, _ = golden_day
    rows = load_rows(run)
    composite_only = ScreenConfig(
        mcap_min_musd=None, mcap_max_musd=None, price_min=None, avg_vol_min_k=None,
        shares_outstanding_max_m=None, outlier_floor=None,
    )  # fmt: skip
    assert sum(o.passed for o in adhoc_screen(run, composite_only, rows)) == 339
    assert sum(o.passed for o in adhoc_screen(run, ScreenConfig(), rows)) == 139


@pytest.mark.golden
@pytest.mark.django_db
@pytest.mark.parametrize(("floor", "count"), [(5.0, 139), (5.5, 88), (5.75, 62), (6.0, 46)])
def test_composite_floor_sensitivity_on_the_golden_day(
    golden_day: tuple[ScoreRun, list[dict[str, str]]], floor: float, count: int
) -> None:
    run, _ = golden_day
    outcomes = adhoc_screen(run, ScreenConfig(composite_floor=floor))
    assert sum(o.passed for o in outcomes) == count


@pytest.mark.golden
@pytest.mark.django_db
def test_every_excluded_golden_company_lists_its_failed_filters(
    golden_day: tuple[ScoreRun, list[dict[str, str]]],
) -> None:
    run, _ = golden_day
    outcomes = adhoc_screen(run, ScreenConfig())
    excluded = [o for o in outcomes if not o.passed]
    assert len(excluded) == 1053 - 139
    assert all(o.reasons for o in excluded)
    assert any(len(o.reasons) > 1 for o in excluded)


@pytest.mark.golden
@pytest.mark.django_db
def test_golden_ordering_is_stable_and_deterministic(
    golden_day: tuple[ScoreRun, list[dict[str, str]]],
) -> None:
    run, _ = golden_day
    first = [o.row.ticker for o in adhoc_screen(run, ScreenConfig()) if o.passed]
    second = [o.row.ticker for o in adhoc_screen(run, ScreenConfig()) if o.passed]
    assert first == second
    composites = {o.row.ticker: o.row.composite for o in adhoc_screen(run, ScreenConfig())}
    keys = [(-composites[t], t) for t in first]
    assert keys == sorted(keys)


@pytest.mark.golden
@pytest.mark.django_db
def test_near_the_line_on_the_golden_day_matches_a_hand_computed_sample(
    golden_day: tuple[ScoreRun, list[dict[str, str]]],
) -> None:
    run, rows = golden_day
    config = ScreenConfig()
    near = {o.row.ticker for o in near_the_line(adhoc_screen(run, config), config)}

    def reasons(r: dict[str, str]) -> list[str]:
        found = []
        mcap, price = (
            float(r["mcap"]) if r["mcap"] else None,
            float(r["price"]) if r["price"] else None,
        )
        vol, shares = (
            float(r["avg_vol"]) if r["avg_vol"] else None,
            float(r["shr_out"]) if r["shr_out"] else None,
        )
        if mcap is None or not 75 <= mcap <= 1000:
            found.append("mcap")
        if price is None or price < 1:
            found.append("price")
        if vol is None or vol < 50:
            found.append("volume")
        if shares is None or shares > 500:
            found.append("shares")
        if float(r["composite_score"]) < 5:
            found.append("composite")
        return found

    expected = set()
    for r in rows:
        failed = reasons(r)
        if len(failed) == 1 and (
            failed[0] != "composite" or 5 - float(r["composite_score"]) <= 0.5
        ):
            expected.add(r["ticker"])
    assert near == expected
    assert len(near) > 10


@pytest.mark.golden
@pytest.mark.django_db
def test_the_default_screen_is_stored_for_the_golden_day(
    golden_day: tuple[ScoreRun, list[dict[str, str]]],
) -> None:
    run, _ = golden_day
    stored = materialise_default_screen(run)
    assert (stored.universe_n, stored.passed_n) == (1053, 139)
    assert ScreenMember.objects.filter(screen_run=stored, passed=True).count() == 139
    assert latest_complete_run(DAY1) == run
