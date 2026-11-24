"""T15.1 to T15.5: choosing the runway and catalyst the scorer sees (carry-forward)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from apps.companies.models import CompanySponsorAlias
from apps.platform.models import PipelineRun
from apps.scoring.models import FormulaVersion, ScoreResult
from apps.scoring.resolver import (
    CARRIED,
    DEFAULT_MAX_AGES,
    EXPIRED,
    FRESH,
    MISSING,
    SETTING_KEYS,
    Candidate,
    max_age,
    resolve,
    resolve_catalysts,
    resolve_runways,
)
from apps.scoring.service import run_scoring
from apps.trials.catalyst_service import derive_universe, fetch_failures
from tests import factories as f

DAY = date(2026, 10, 12)


def ago(days: int) -> date:
    return DAY - timedelta(days=days)


def good(days_ago: int, row: str = "g") -> Candidate[str]:
    return Candidate(ago(days_ago), True, None, row)


def bad(days_ago: int, reason: str = "cash_missing", row: str = "b") -> Candidate[str]:
    return Candidate(ago(days_ago), False, reason, row)


# --- T15.1 resolution table ------------------------------------------------------------------


def test_fetched_this_cycle_is_fresh_with_age_zero() -> None:
    r = resolve([good(0, "today"), good(3)], DAY, max_age_days=120, carry_forward=True)
    assert (r.state, r.row, r.age_days, r.reason, r.source_date) == (FRESH, "today", 0, None, DAY)


def test_failed_today_with_a_recent_good_value_is_carried() -> None:
    r = resolve([good(10, "old"), bad(0)], DAY, max_age_days=120, carry_forward=True)
    assert (r.state, r.row, r.age_days, r.reason) == (CARRIED, "old", 10, "stale_carried")
    assert r.carried and r.source_date == ago(10)


def test_no_row_today_with_a_recent_good_value_is_carried() -> None:
    r = resolve([good(4, "old")], DAY, max_age_days=120, carry_forward=True)
    assert (r.state, r.age_days) == (CARRIED, 4)


def test_the_newest_good_value_is_the_one_carried() -> None:
    r = resolve(
        [good(30, "older"), good(5, "newer"), bad(0)], DAY, max_age_days=120, carry_forward=True
    )
    assert r.row == "newer" and r.age_days == 5


def test_failed_today_with_an_old_good_value_expires() -> None:
    r = resolve([good(200), bad(0)], DAY, max_age_days=120, carry_forward=True)
    assert (r.state, r.row, r.age_days, r.reason) == (EXPIRED, None, 200, "stale_expired")


def test_never_obtained_keeps_its_original_reason() -> None:
    r = resolve(
        [bad(0, "non_usd_only"), bad(5, "cash_missing")], DAY, max_age_days=120, carry_forward=True
    )
    assert (r.state, r.row, r.age_days, r.reason) == (MISSING, None, None, "non_usd_only")


def test_no_rows_at_all_is_missing_without_a_reason() -> None:
    none: list[Candidate[str]] = []
    r = resolve(none, DAY, max_age_days=120, carry_forward=True)
    assert (r.state, r.reason) == (MISSING, None)


def test_rows_after_the_day_are_ignored() -> None:
    r = resolve([good(-3, "future"), good(2, "past")], DAY, max_age_days=120, carry_forward=True)
    assert r.row == "past"


# --- T15.2 boundary at the maximum age -------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "state"), [(119, CARRIED), (120, CARRIED), (121, EXPIRED), (7, CARRIED), (8, CARRIED)]
)
def test_runway_age_boundary(age: int, state: str) -> None:
    r = resolve([good(age), bad(0)], DAY, max_age_days=120, carry_forward=True)
    assert r.state == state
    assert r.age_days == age


@pytest.mark.parametrize(("age", "state"), [(6, CARRIED), (7, CARRIED), (8, EXPIRED)])
def test_catalyst_age_boundary(age: int, state: str) -> None:
    assert resolve([good(age), bad(0)], DAY, max_age_days=7, carry_forward=True).state == state


# --- T15.4 the parity switch -----------------------------------------------------------------


def test_with_carry_forward_off_a_failed_day_is_null() -> None:
    r = resolve([good(2), bad(0, "cash_missing")], DAY, max_age_days=120, carry_forward=False)
    assert (r.state, r.row, r.reason) == (MISSING, None, "cash_missing")


def test_with_carry_forward_off_the_latest_good_row_is_used_as_delivered() -> None:
    # The golden export's runway rows are a few days old and must still be used.
    r = resolve([good(3, "delivered")], DAY, max_age_days=120, carry_forward=False)
    assert (r.state, r.row, r.age_days) == (FRESH, "delivered", 3)
    assert resolve([good(300, "x")], DAY, max_age_days=120, carry_forward=False).row == "x"


# --- Maximum ages are settings ---------------------------------------------------------------


@pytest.mark.django_db
def test_default_maximum_ages_and_their_settings() -> None:
    assert DEFAULT_MAX_AGES == {"runway": 120, "catalyst": 7, "short_volume": 5, "insider": 7}
    assert {name: max_age(name) for name in SETTING_KEYS} == DEFAULT_MAX_AGES
    f.setting(key=SETTING_KEYS["runway"], value=60)
    assert max_age("runway") == 60


# --- Reading the stored rows -----------------------------------------------------------------


@pytest.mark.django_db
def test_runways_are_resolved_from_the_stored_rows() -> None:
    fresh, carried, expired, never = (f.company() for _ in range(4))
    f.runway_calc(fresh, calc_date=DAY, runway_months=20.0)
    f.runway_calc(carried, calc_date=ago(30), runway_months=18.0)
    f.runway_calc(carried, calc_date=DAY, status="unavailable", runway_months=None,
                  reason_codes=["cash_missing"])  # fmt: skip
    f.runway_calc(expired, calc_date=ago(200), runway_months=9.0)
    f.runway_calc(never, calc_date=DAY, status="unavailable", runway_months=None,
                  reason_codes=["non_usd_only", "cash_missing"])  # fmt: skip
    ids = [fresh.pk, carried.pk, expired.pk, never.pk]
    got = resolve_runways(ids, DAY, carry_forward=True, max_age_days=120)
    assert got[fresh.pk].state == FRESH
    assert got[carried.pk].state == CARRIED and got[carried.pk].age_days == 30
    carried_row = got[carried.pk].row
    assert carried_row is not None and carried_row.runway_months == 18.0
    assert got[expired.pk].state == EXPIRED
    assert got[never.pk].state == MISSING and got[never.pk].reason == "non_usd_only"
    off = resolve_runways(ids, DAY, carry_forward=False, max_age_days=120)
    assert off[carried.pk].state == MISSING and off[carried.pk].reason == "cash_missing"


@pytest.mark.django_db
def test_a_failed_catalyst_is_not_good_but_an_empty_one_is() -> None:
    empty, failed = f.company(), f.company()
    f.catalyst_signal(empty, as_of_date=DAY, reason_codes=["no_trials", "no_catalyst"])
    f.catalyst_signal(failed, as_of_date=ago(3), clinical_phase="phase_3")
    f.catalyst_signal(failed, as_of_date=DAY, status="failed", reason_codes=["http_5xx"])
    got = resolve_catalysts([empty.pk, failed.pk], DAY, carry_forward=True, max_age_days=7)
    assert got[empty.pk].state == FRESH  # no trials is a legitimate answer
    assert got[failed.pk].state == CARRIED and got[failed.pk].age_days == 3


# --- Catalyst fetch failures -----------------------------------------------------------------


def _call(alias: str, day: date, status: str, reason: str | None = None) -> None:
    run = f.pipeline_run(as_of_date=day, stage="filings")
    f.source_call(run=run, feed="ctgov_studies", scope_key=alias, status=status, reason_code=reason)


@pytest.mark.django_db
def test_fetch_failures_follow_the_sponsor_calls_of_the_day() -> None:
    ok, failed, silent, unjudged = (f.company() for _ in range(4))
    for company, alias in ((ok, "Ok Co"), (failed, "Failed Co"), (silent, "Silent Co")):
        CompanySponsorAlias.objects.create(company=company, sponsor_name=alias, source="manual")
    CompanySponsorAlias.objects.create(company=unjudged, sponsor_name="Never Co", source="manual")
    _call("Ok Co", DAY, "ok")
    _call("Failed Co", DAY, "failed", "http_5xx")
    _call("Silent Co", ago(1), "ok")  # fetched yesterday, not today
    ids = [ok.pk, failed.pk, silent.pk, unjudged.pk]
    assert fetch_failures(ids, DAY) == {failed.pk: "http_5xx", silent.pk: "fetch_error"}


@pytest.mark.django_db
def test_a_company_with_one_good_alias_is_not_failed() -> None:
    company = f.company()
    for alias in ("One", "Two"):
        CompanySponsorAlias.objects.create(company=company, sponsor_name=alias, source="manual")
    _call("One", DAY, "failed", "timeout")
    _call("Two", DAY, "ok")
    assert fetch_failures([company.pk], DAY) == {}


@pytest.mark.django_db
def test_a_failed_fetch_makes_the_stored_signal_failed() -> None:
    company = f.company()
    f.ticker(company, ticker="AAA")
    CompanySponsorAlias.objects.create(company=company, sponsor_name="Aaa", source="manual")
    _call("Aaa", DAY, "failed", "http_5xx")
    derive_universe(DAY)
    from apps.trials.models import CatalystSignal

    row = CatalystSignal.objects.get(company=company)
    assert row.status == "failed" and row.reason_codes[0] == "http_5xx"


# --- T15.3 the score result lists what was carried -------------------------------------------


def _score(formula: str, carry_catalyst_day: int = 3) -> tuple[ScoreResult, ScoreResult]:
    """Two companies scored on DAY: one fresh, one whose runway and catalyst must be carried."""
    fresh, stale = f.company(), f.company()
    for company in (fresh, stale):
        f.ticker(company)
        f.snapshot(company, mcap_musd=60, eps_this=None)
    f.runway_calc(fresh, calc_date=DAY, runway_months=20.0, cash_end_date=ago(30))
    f.catalyst_signal(
        fresh, as_of_date=DAY, clinical_phase="phase_3", clinical_date=DAY + timedelta(days=60)
    )
    f.runway_calc(stale, calc_date=ago(40), runway_months=15.0, cash_end_date=ago(70))
    f.runway_calc(stale, calc_date=DAY, status="unavailable", runway_months=None,
                  reason_codes=["cash_missing"])  # fmt: skip
    f.catalyst_signal(stale, as_of_date=ago(carry_catalyst_day), clinical_phase="phase_3",
                      clinical_date=DAY + timedelta(days=90))  # fmt: skip
    f.catalyst_signal(stale, as_of_date=DAY, status="failed", reason_codes=["timeout"])
    run = run_scoring(DAY, formula_name=formula, pipeline_run=PipelineRun.objects.create(
        stage="market", as_of_date=DAY, trigger="manual", started_at=f.NOW))  # fmt: skip
    results = {r.company_id: r for r in ScoreResult.objects.filter(score_run=run)}
    return results[fresh.pk], results[stale.pk]


@pytest.mark.django_db
def test_production_formula_carries_and_lists_every_carried_input() -> None:
    fresh, stale = _score("production")
    assert fresh.carried_inputs == []
    assert fresh.input_resolution["runway"]["state"] == FRESH
    assert stale.carried_inputs == ["runway", "catalyst"]
    runway = stale.input_resolution["runway"]
    assert (runway["state"], runway["age_days"], runway["reason"]) == (CARRIED, 40, "stale_carried")
    assert runway["source_date"] == ago(40).isoformat()
    assert stale.input_resolution["catalyst"]["age_days"] == 3
    assert stale.inputs["runway_months"] == 15.0
    assert stale.inputs["clinical_phase"] == "phase_3"
    assert stale.runway_calc is not None and stale.runway_calc.calc_date == ago(40)
    assert "runway_months" not in stale.missing_inputs


@pytest.mark.django_db
def test_an_expired_catalyst_is_null_and_explained() -> None:
    _, stale = _score("production", carry_catalyst_day=9)
    assert stale.input_resolution["catalyst"]["state"] == EXPIRED
    assert stale.input_resolution["catalyst"]["reason"] == "stale_expired"
    assert stale.inputs["clinical_date_text"] is None
    assert stale.catalyst_signal is None
    assert stale.carried_inputs == ["runway"]


@pytest.mark.django_db
def test_the_parity_formula_nulls_a_failed_input() -> None:
    _, stale = _score("parity")
    assert stale.carried_inputs == []
    assert stale.inputs["runway_months"] is None and "runway_months" in stale.missing_inputs
    assert stale.input_resolution["runway"]["state"] == MISSING
    assert stale.input_resolution["runway"]["reason"] == "cash_missing"
    assert stale.input_resolution["catalyst"]["reason"] == "timeout"


@pytest.mark.django_db
def test_finviz_fields_are_never_carried_forward() -> None:
    company = f.company()
    f.ticker(company)
    f.snapshot(company, as_of_date=ago(1), eps_this=40.0, mcap_musd=60)
    f.snapshot(company, as_of_date=DAY, eps_this=None, mcap_musd=60)
    f.runway_calc(company, calc_date=DAY)
    run = run_scoring(DAY, formula_name="production")
    row = ScoreResult.objects.get(score_run=run, company=company)
    assert row.inputs["eps_this"] is None and "eps_this" in row.missing_inputs
    assert "eps_this" not in row.carried_inputs


# --- The production formula ------------------------------------------------------------------


@pytest.mark.django_db
def test_production_formula_is_parity_with_carry_forward_on() -> None:
    parity = FormulaVersion.objects.get(name="parity", version=1)
    production = FormulaVersion.objects.get(name="production", version=1)
    assert production.config == parity.config
    assert production.quirks == {**parity.quirks, "carry_forward": True}
    assert parity.quirks["carry_forward"] is False
