"""T5.7 (integration): the metric views and the company_page read model on synthetic rows."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import connection

from apps.companies.models import Company
from apps.web.models import CompanyPage
from tests import factories as f

DAY = f.DAY  # a Monday


def _rows(sql: str, params: list[Any]) -> dict[int, dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        names = [col.name for col in cursor.description or []]
        return {row[0]: dict(zip(names, row, strict=True)) for row in cursor.fetchall()}


def short_volume(as_of: date) -> dict[int, dict[str, Any]]:
    return _rows("SELECT * FROM short_volume_metric_at(%s)", [as_of])


def insider(as_of: date) -> dict[int, dict[str, Any]]:
    return _rows("SELECT * FROM insider_metric_at(%s)", [as_of])


def trading_days(count: int, end: date = DAY) -> list[date]:
    """The ``count`` weekdays ending on ``end``, oldest first."""
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


def load_series(company: Company, ratios: list[float], end: date = DAY) -> None:
    for day, ratio in zip(trading_days(len(ratios), end), ratios, strict=True):
        f.short_volume(company, day, round(ratio * 1000), 1000)


# ------------------------------------------------------------------------- short volume


@pytest.mark.django_db
def test_short_volume_matches_a_hand_calculation() -> None:
    co = f.company()
    load_series(co, [0.5] * 25 + [0.6] * 5)
    row = short_volume(DAY)[co.pk]
    assert row["latest_trade_date"] == DAY
    assert row["latest_ratio"] == Decimal("0.6000")
    assert row["avg_ratio_5d"] == pytest.approx(0.6)
    assert row["avg_ratio_30d"] == pytest.approx((25 * 0.5 + 5 * 0.6) / 30)  # includes the last 5
    assert row["delta_pts"] == pytest.approx((0.6 - (25 * 0.5 + 5 * 0.6) / 30) * 100)
    assert row["trend"] == "rising"
    assert (row["days_in_5d"], row["days_in_30d"]) == (5, 30)
    assert row["status"] == "ok"
    assert row["reason_codes"] == []
    assert (row["age_days"], row["is_stale"]) == (0, False)


@pytest.mark.django_db
def test_short_volume_trend_falling_and_flat() -> None:
    falling, flat = f.company(), f.company()
    load_series(falling, [0.6] * 25 + [0.4] * 5)
    load_series(flat, [0.5] * 30)
    rows = short_volume(DAY)
    assert rows[falling.pk]["trend"] == "falling"
    assert rows[falling.pk]["delta_pts"] < 0
    assert rows[flat.pk]["trend"] == "flat"
    assert rows[flat.pk]["delta_pts"] == pytest.approx(0.0)


@pytest.mark.django_db
def test_short_volume_uses_only_the_latest_30_days() -> None:
    co = f.company()
    load_series(co, [0.9] * 10 + [0.5] * 30)  # the 10 oldest days fall outside the window
    row = short_volume(DAY)[co.pk]
    assert row["days_in_30d"] == 30
    assert row["avg_ratio_30d"] == pytest.approx(0.5)


@pytest.mark.django_db
def test_short_volume_with_fewer_than_five_days_is_insufficient() -> None:
    co = f.company()
    load_series(co, [0.5] * 4)
    row = short_volume(DAY)[co.pk]
    assert row["status"] == "insufficient_history"
    assert row["reason_codes"] == ["insufficient_history"]
    assert row["latest_ratio"] is None
    assert row["delta_pts"] is None
    assert row["trend"] is None
    assert row["days_in_5d"] == 4


@pytest.mark.django_db
def test_short_volume_sums_markets_and_skips_zero_volume() -> None:
    co = f.company()
    for day in trading_days(5):
        f.short_volume(co, day, 300, 1000, market="CNMS")
        f.short_volume(co, day, 100, 1000, market="OTHER")  # combined ratio 0.2
    f.short_volume(co, DAY - timedelta(days=30), 0, 0, market="ZERO", ticker_raw="Z")
    row = short_volume(DAY)[co.pk]
    assert row["latest_ratio"] == Decimal("0.2000")
    assert row["days_in_30d"] == 5


@pytest.mark.django_db
def test_short_volume_ignores_unmatched_tickers_and_future_dates() -> None:
    co = f.company()
    load_series(co, [0.5] * 5)
    f.short_volume(None, DAY, 900, 1000, ticker_raw="NOMATCH")
    f.short_volume(co, DAY + timedelta(days=1), 900, 1000, market="FUTURE")
    rows = short_volume(DAY)
    assert set(rows) == {co.pk}
    assert rows[co.pk]["latest_trade_date"] == DAY


@pytest.mark.django_db
@pytest.mark.parametrize(("extra_days", "stale"), [(0, False), (7, False), (8, True), (10, True)])
def test_short_volume_goes_stale_after_five_weekdays(extra_days: int, stale: bool) -> None:
    co = f.company()
    load_series(co, [0.5] * 5)
    row = short_volume(DAY + timedelta(days=extra_days))[co.pk]
    assert row["age_days"] == extra_days
    assert row["is_stale"] is stale
    assert row["latest_trade_date"] == DAY  # a missed file carries the last data forward


@pytest.mark.django_db
def test_short_volume_as_of_a_past_date_ignores_later_data() -> None:
    co = f.company()
    load_series(co, [0.5] * 10 + [0.9] * 5)
    past = trading_days(15)[9]
    row = short_volume(past)[co.pk]
    assert row["latest_trade_date"] == past
    assert row["avg_ratio_5d"] == pytest.approx(0.5)


@pytest.mark.django_db
def test_the_short_volume_view_reads_today() -> None:
    co = f.company()
    with connection.cursor() as cursor:
        cursor.execute("SELECT (now() AT TIME ZONE 'America/New_York')::date")
        today = cursor.fetchone()[0]
    load_series(co, [0.5] * 5, end=today)
    with connection.cursor() as cursor:
        cursor.execute("SELECT latest_trade_date, age_days FROM short_volume_metric")
        assert cursor.fetchall() == [(trading_days(5, today)[-1], 0)]


# --------------------------------------------------------------------------- insider


def fetched(co: Company, when: date = DAY) -> None:
    """Record a successful submissions fetch for the company."""
    f.source_call(
        feed="sec_submissions",
        scope_key=co.cik,
        completed_at=f.NOW - timedelta(days=(DAY - when).days),
    )


def trade(
    co: Company, code: str, shares: int, price: int, *, owner: str, role: str,
    planned: bool = False, filed: date | None = None, accession: str | None = None,
) -> Any:  # fmt: skip
    filing = f.form4_filing(
        co,
        filed_at=filed or DAY - timedelta(days=10),
        is_10b5_1=planned,
        **({"accession": accession} if accession else {}),
    )
    return f.form4_transaction(
        filing,
        transaction_code=code,
        shares=Decimal(shares),
        price_per_share=Decimal(price),
        reporting_owner_cik=owner,
        is_officer=role == "officer",
        is_director=role == "director",
    )


def worked_example(co: Company) -> None:
    trade(co, "P", 1000, 5, owner="0000000001", role="officer")  # CEO buys
    trade(co, "S", 2000, 6, owner="0000000002", role="officer", planned=True)  # CFO, 10b5-1
    trade(co, "S", 1000, 6, owner="0000000003", role="director")  # Dir1
    trade(co, "S", 500, 7, owner="0000000003", role="director")  # Dir1 again


@pytest.mark.django_db
def test_insider_worked_example() -> None:
    co = f.company()
    fetched(co)
    worked_example(co)
    row = insider(DAY)[co.pk]
    assert row["filings_count"] == 4
    assert row["open_market_buys_count"] == 1
    assert row["open_market_sales_count"] == 3
    assert row["officers_directors_buying_count"] == 1
    assert row["officers_directors_selling_count"] == 2
    assert row["pct_10b5_1_dollars"] == pytest.approx(100 * 12000 / 21500)  # 55.81
    assert row["window_days"] == 90
    assert (row["status"], row["age_days"], row["is_stale"]) == ("ok", 0, False)


@pytest.mark.django_db
def test_insider_window_includes_day_90_and_excludes_day_91() -> None:
    co = f.company()
    fetched(co)
    trade(co, "P", 10, 1, owner="0000000001", role="officer", filed=DAY - timedelta(days=90))
    trade(co, "P", 10, 1, owner="0000000002", role="officer", filed=DAY - timedelta(days=91))
    assert insider(DAY)[co.pk]["filings_count"] == 1


@pytest.mark.django_db
def test_insider_other_codes_are_stored_but_not_counted_as_buys_or_sales() -> None:
    co = f.company()
    fetched(co)
    trade(co, "G", 100, 0, owner="0000000001", role="officer")
    row = insider(DAY)[co.pk]
    assert (
        row["filings_count"],
        row["open_market_buys_count"],
        row["open_market_sales_count"],
    ) == (1, 0, 0)


@pytest.mark.django_db
def test_insider_no_filings_is_zero_counts_and_null_percentage() -> None:
    co = f.company()
    fetched(co)
    row = insider(DAY)[co.pk]
    assert [
        row[k] for k in ("filings_count", "open_market_buys_count", "open_market_sales_count")
    ] == [0, 0, 0]
    assert row["officers_directors_buying_count"] == 0
    assert row["pct_10b5_1_dollars"] is None
    assert row["status"] == "ok"


@pytest.mark.django_db
def test_insider_only_purchases_gives_a_null_percentage() -> None:
    co = f.company()
    fetched(co)
    trade(co, "P", 100, 5, owner="0000000001", role="officer", planned=True)
    assert insider(DAY)[co.pk]["pct_10b5_1_dollars"] is None


@pytest.mark.django_db
def test_insider_never_fetched_is_unavailable_not_zero() -> None:
    co = f.company()
    row = insider(DAY)[co.pk]
    assert row["status"] == "unavailable"
    assert row["reason_codes"] == ["fetch_error"]
    assert row["filings_count"] is None
    assert row["is_stale"] is True


@pytest.mark.django_db
def test_insider_failed_fetches_do_not_count_as_fetched() -> None:
    co = f.company()
    f.source_call(feed="sec_submissions", scope_key=co.cik, status="failed", completed_at=f.NOW)
    assert insider(DAY)[co.pk]["status"] == "unavailable"


@pytest.mark.django_db
@pytest.mark.parametrize(("days_old", "stale"), [(0, False), (7, False), (8, True)])
def test_insider_goes_stale_after_seven_days(days_old: int, stale: bool) -> None:
    co = f.company()
    fetched(co, DAY - timedelta(days=days_old))
    row = insider(DAY)[co.pk]
    assert row["age_days"] == days_old
    assert row["is_stale"] is stale


@pytest.mark.django_db
def test_insider_parse_failures_are_excluded() -> None:
    co = f.company()
    fetched(co)
    f.form4_filing(
        co, filed_at=DAY - timedelta(days=5), parse_status="failed", parse_reason="bad xml"
    )
    trade(co, "P", 100, 5, owner="0000000001", role="officer")
    assert insider(DAY)[co.pk]["filings_count"] == 1


@pytest.mark.django_db
def test_insider_amendment_replaces_the_original() -> None:
    co = f.company()
    fetched(co)
    original = trade(
        co, "S", 1000, 10, owner="0000000001", role="officer", accession="0000000001-26-000100"
    )
    amendment = f.form4_filing(
        co, form="4/A", filed_at=DAY - timedelta(days=8), amends_accession="0000000001-26-000100"
    )
    f.form4_transaction(
        amendment, transaction_code="S", shares=Decimal(400), price_per_share=Decimal(10),
        reporting_owner_cik="0000000001", is_officer=True,
    )  # fmt: skip
    row = insider(DAY)[co.pk]
    assert original.pk
    assert row["filings_count"] == 1
    assert row["open_market_sales_count"] == 1


@pytest.mark.django_db
def test_insider_failed_amendment_does_not_hide_the_original() -> None:
    co = f.company()
    fetched(co)
    trade(co, "S", 1000, 10, owner="0000000001", role="officer", accession="0000000001-26-000200")
    f.form4_filing(
        co, form="4/A", filed_at=DAY - timedelta(days=8), parse_status="failed",
        amends_accession="0000000001-26-000200",
    )  # fmt: skip
    assert insider(DAY)[co.pk]["filings_count"] == 1


@pytest.mark.django_db
def test_insider_ignores_delisted_companies() -> None:
    gone = f.company(status="delisted")
    assert gone.pk not in insider(DAY)


# ------------------------------------------------------------------------ company_page


def build_page_inputs() -> tuple[Company, Any]:
    co = f.company(name="Example Bio")
    f.ticker(co, ticker="EXBI")
    snap = f.snapshot(
        co, price=5.39, mcap_musd=307.94, short_float=29.92, earnings_date_text="8/13/2026"
    )
    rc = f.runway_calc(co, runway_months=20.0, runway_capped=False)
    cs = f.catalyst_signal(
        co, total_studies=7, active_phase_3=1, active_phase_2=2, latest_trial_nct="NCT00000042"
    )
    run = f.score_run(finviz_as_of_date=DAY)
    f.score_result(run, co, composite=6.4, cash_health=7.0, runway_calc=rc, catalyst_signal=cs,
                   carried_inputs=["runway"], missing_inputs=["eps_this"])  # fmt: skip
    assert snap.pk
    return co, run


@pytest.mark.django_db
def test_company_page_is_empty_without_a_completed_run() -> None:
    f.score_run(status="running")
    assert CompanyPage.objects.count() == 0


@pytest.mark.django_db
def test_company_page_joins_the_exact_rows_the_score_used() -> None:
    co, run = build_page_inputs()
    page = CompanyPage.objects.get(pk=co.pk)
    assert (page.ticker, page.company_name, page.industry) == (
        "EXBI",
        "Example Bio",
        "Biotechnology",
    )
    assert page.score_run_id == run.pk
    assert (page.composite, page.cash_health) == (6.4, 7.0)
    assert (page.price, page.mcap_musd, page.short_float) == (5.39, 307.94, 29.92)
    assert page.runway_months == 20.0
    assert (page.total_studies, page.active_phase_3, page.active_phase_2) == (7, 1, 2)
    assert page.latest_trial_nct == "NCT00000042"
    assert page.carried_inputs == ["runway"]
    assert page.missing_inputs == ["eps_this"]
    assert set(page.field_meta) == {
        "market",
        "scores",
        "runway",
        "catalyst",
        "short_volume",
        "insider",
        "trial_lists",
    }
    assert page.field_meta["market"]["as_of"] == DAY.isoformat()
    assert page.field_meta["runway"]["carried_forward"] is False


@pytest.mark.django_db
def test_company_page_uses_only_the_latest_completed_run() -> None:
    co, _ = build_page_inputs()
    newer = f.score_run(
        as_of_date=DAY + timedelta(days=1), finviz_as_of_date=DAY + timedelta(days=1)
    )
    f.score_result(newer, co, composite=9.0)
    f.snapshot(co, as_of_date=DAY + timedelta(days=1), price=7.0)
    f.score_run(as_of_date=DAY + timedelta(days=2), status="failed")
    page = CompanyPage.objects.get(pk=co.pk)
    assert page.composite == 9.0
    assert page.price == 7.0
    assert page.as_of_date == DAY + timedelta(days=1)


@pytest.mark.django_db
def test_company_page_marks_missing_source_data_with_a_reason() -> None:
    co = f.company()
    f.score_result(f.score_run(), co)
    page = CompanyPage.objects.get(pk=co.pk)
    assert page.price is None
    assert page.field_meta["market"]["reasons"] == []
    assert page.field_meta["short_volume"]["reasons"] == ["not_in_finra_file"]
    assert page.field_meta["short_volume"]["is_stale"] is True


@pytest.mark.django_db
def test_company_page_has_one_row_per_scored_company() -> None:
    run = f.score_run()
    for _ in range(3):
        f.score_result(run, f.company())
    assert CompanyPage.objects.count() == 3
