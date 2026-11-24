"""T11B.1 to T11B.11: the Form 4 parser and collector, and the metrics on what they stored.

The documents are synthetic stand-ins for the seven F12 cases (see ``tests/form4_samples``).
The metric SQL itself is tested in ``test_read_views``; here it runs on collected data.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.companies.models import Company
from apps.marketdata.storage import LocalRawStorage
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from apps.sec.client import MIN_INTERVAL_SECONDS, SecClient, SecLimiter
from apps.sec.collector import (
    FACTS_URL,
    SUBMISSIONS_URL,
    TICKERS_URL,
    collect_sec,
)
from apps.sec.form4 import Form4ParseError, parse_form4
from apps.sec.form4_collector import Form4Collector, collect_form4, document_url
from apps.sec.models import SecForm4Filing, SecForm4Transaction
from tests import factories
from tests.form4_samples import CEO, CFO, DIR1, Line, entry, form4_xml, url_for
from tests.sec_samples import FakeSec, submissions_json, tickers_json
from tests.test_read_views import fetched, insider
from tests.test_sec_client import UA, Clock

AS_OF = date(2026, 10, 12)
CIK = "0001000001"


@pytest.fixture
def company() -> Company:
    co = factories.company(name="KTTA", cik=CIK)
    factories.ticker(co, ticker="KTTA")
    fetched(co)  # a successful submissions fetch, so counts are real zeros rather than unknown
    return co


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def storage(tmp_path: Path) -> LocalRawStorage:
    return LocalRawStorage(tmp_path / "raw")


def client_for(sec: FakeSec, clock: Clock) -> SecClient:
    return SecClient(
        user_agent=UA,
        limiter=SecLimiter(clock=clock.now, sleep=clock.sleep),
        transport=sec,
        sleep=clock.sleep,
    )


def run_collect(sec: FakeSec, clock: Clock, storage: Any, entries: list[Any], **kw: Any) -> Any:
    return collect_form4(entries, AS_OF, client=client_for(sec, clock), storage=storage, **kw)


BUY = Line("P", "1000", "5.00")
SELL = Line("S", "2000", "6.00")


# ----------------------------------------------------------------------- T11B.1 parser


def test_an_officer_purchase() -> None:
    parsed = parse_form4(form4_xml([CEO], [BUY]))
    assert (parsed.document_type, parsed.period_of_report) == ("4", date(2026, 9, 30))
    assert parsed.is_10b5_1 is False
    owner = parsed.owners[0]
    assert (owner.cik, owner.is_officer, owner.is_director, owner.officer_title) == (
        "0000000001",
        True,
        False,
        "Chief Executive Officer",
    )
    (tx,) = parsed.transactions
    assert (tx.line_no, tx.code, tx.shares, tx.price_per_share) == (
        1,
        "P",
        Decimal("1000"),
        Decimal("5.00"),
    )
    assert (tx.amount_usd, tx.acquired_or_disposed, tx.transaction_date) == (
        Decimal("5000.00"),
        "D",
        date(2026, 9, 30),
    )


def test_the_planned_sale_flag_is_the_filing_level_checkbox() -> None:
    assert parse_form4(form4_xml([CFO], [SELL], plan=True)).is_10b5_1 is True
    assert parse_form4(form4_xml([CFO], [SELL], plan=False)).is_10b5_1 is False
    # Filings made before the checkbox existed carry no element and count as not planned.
    assert parse_form4(form4_xml([CFO], [SELL], plan=None)).is_10b5_1 is False


def test_a_multi_owner_filing_keeps_every_owner_and_every_line() -> None:
    parsed = parse_form4(form4_xml([DIR1, CEO], [SELL, Line("S", "10", "7.5")]))
    assert [o.cik for o in parsed.owners] == ["0000000003", "0000000001"]
    assert [t.line_no for t in parsed.transactions] == [1, 2]


def test_a_gift_and_missing_numbers_have_no_amount() -> None:
    gift = parse_form4(form4_xml([CEO], [Line("G", "500", None, side="D")]))
    assert gift.transactions[0].code == "G" and gift.transactions[0].amount_usd is None
    no_shares = parse_form4(form4_xml([CEO], [Line("S", None, "6.00")]))
    assert no_shares.transactions[0].shares is None
    assert no_shares.transactions[0].amount_usd is None


def test_an_amendment_names_its_original_submission_date() -> None:
    parsed = parse_form4(form4_xml([CEO], [BUY], document_type="4/A", original_date="2026-10-01"))
    assert (parsed.document_type, parsed.original_submission_date) == ("4/A", date(2026, 10, 1))


def test_the_amount_is_rounded_to_cents() -> None:
    parsed = parse_form4(form4_xml([CEO], [Line("S", "3", "0.3333")]))
    assert parsed.transactions[0].amount_usd == Decimal("1.00")


def test_a_filing_with_only_derivative_lines_has_no_transactions() -> None:
    assert parse_form4(form4_xml([CEO], [], derivative_only=True)).transactions == []


@pytest.mark.parametrize(
    "content",
    [
        b"<ownershipDocument><issuer>",
        b"",
        b"<html><body>Service unavailable</body></html>",
        b'<!DOCTYPE x [<!ENTITY a "aaaa">]><ownershipDocument/>',
        form4_xml([CEO], [BUY]).replace(b"<issuerCik>0001000001</issuerCik>", b""),
        form4_xml([CEO], [BUY]).replace(b"2026-09-30</value>", b"not a date</value>"),
        form4_xml([CEO], [BUY]).replace(b"<rptOwnerCik>0000000001", b"<rptOwnerCik>abc"),
        form4_xml([CEO], [BUY]).replace(b"<value>1000</value>", b"<value>lots</value>"),
        form4_xml([], [BUY]),
    ],
)
def test_an_unusable_document_raises_a_parse_error(content: bytes) -> None:
    with pytest.raises(Form4ParseError):
        parse_form4(content)


# ------------------------------------------------------- T11B.2, T11B.11 worked example


def worked_example_docs(company: Company) -> tuple[list[Any], FakeSec]:
    docs = [
        ("0001000001-26-000101", form4_xml([CEO], [Line("P", "1000", "5.00")])),
        ("0001000001-26-000102", form4_xml([CFO], [Line("S", "2000", "6.00")], plan=True)),
        ("0001000001-26-000103", form4_xml([DIR1], [Line("S", "1000", "6.00")])),
        ("0001000001-26-000104", form4_xml([DIR1], [Line("S", "500", "7.00")])),
    ]
    entries = [entry(company.pk, accession) for accession, _ in docs]
    return entries, FakeSec({url_for(e): body for e, (_, body) in zip(entries, docs, strict=True)})


@pytest.mark.django_db
def test_the_worked_example_through_the_collector_and_the_view(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    entries, sec = worked_example_docs(company)
    result = run_collect(sec, clock, storage, entries)
    assert (result.stored, result.transactions, result.parse_failures) == (4, 4, {})
    assert result.run.status == PipelineRun.Status.OK

    row = insider(AS_OF)[company.pk]
    assert row["filings_count"] == 4
    assert (row["open_market_buys_count"], row["open_market_sales_count"]) == (1, 3)
    assert row["officers_directors_buying_count"] == 1
    assert row["officers_directors_selling_count"] == 2
    assert row["pct_10b5_1_dollars"] == pytest.approx(100 * 12000 / 21500, abs=1e-2)
    assert round(row["pct_10b5_1_dollars"], 2) == 55.81

    # The same stored data read as of a later date, still inside the window.
    later = insider(AS_OF + timedelta(days=20))[company.pk]
    assert later["filings_count"] == 4
    assert round(later["pct_10b5_1_dollars"], 2) == 55.81


@pytest.mark.django_db
def test_stored_fields_of_a_filing_and_its_transactions(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    e = entry(company.pk, "0001000001-26-000102")
    sec = FakeSec({url_for(e): form4_xml([CFO], [SELL, Line("S", "10", "7.5")], plan=True)})
    result = run_collect(sec, clock, storage, [e])
    filing = SecForm4Filing.objects.get()
    assert (filing.form, filing.filed_at, filing.period_of_report) == (
        "4",
        date(2026, 10, 1),
        date(2026, 9, 30),
    )
    assert (filing.is_10b5_1, filing.parse_status, filing.amends_accession) == (True, "ok", "")
    assert filing.primary_document == "xslF345X05/0001000001-26-000102.xml"
    first, second = filing.transactions.order_by("line_no")
    assert (first.reporting_owner_cik, first.is_officer, first.is_director) == (
        "0000000002",
        True,
        False,
    )
    assert (first.officer_title, first.transaction_code, first.acquired_or_disposed) == (
        "Chief Financial Officer",
        "S",
        "D",
    )
    assert (first.shares, first.price_per_share, first.amount_usd) == (
        Decimal("2000"),
        Decimal("6.00"),
        Decimal("12000.00"),
    )
    assert second.amount_usd == Decimal("75.00")
    # The raw XML is kept and the call records where.
    call = SourceCall.objects.get(feed="sec_form4", scope_key=e.accession)
    assert (call.status, call.http_status) == ("ok", 200)
    assert storage.get(call.storage_uri) == form4_xml(
        [CFO], [SELL, Line("S", "10", "7.5")], plan=True
    )
    assert filing.source_call_id == call.pk and result.stored == 1


def test_the_document_url_uses_the_raw_xml_not_the_styled_view() -> None:
    e = entry(1, "0001000001-26-000102")
    assert document_url(e) == (
        "https://www.sec.gov/Archives/edgar/data/1000001/000100000126000102/"
        "0001000001-26-000102.xml"
    )


# ---------------------------------------------------------------- T11B.3 window, T11B.4


@pytest.mark.django_db
def test_the_window_includes_day_90_and_excludes_day_91(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    day90, day91 = AS_OF - timedelta(days=90), AS_OF - timedelta(days=91)
    in_window = entry(company.pk, "0001000001-26-000201", filed=day90)
    too_old = entry(company.pk, "0001000001-26-000202", filed=day91)
    body = form4_xml([CEO], [BUY])
    sec = FakeSec({url_for(in_window): body, url_for(too_old): body})
    result = run_collect(sec, clock, storage, [in_window, too_old])
    assert (result.stored, result.too_old) == (1, 1)
    assert [url for url, _ in sec.requests] == [url_for(in_window)]
    assert insider(AS_OF)[company.pk]["filings_count"] == 1


@pytest.mark.django_db
def test_other_codes_are_stored_but_counted_as_neither_buy_nor_sale(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    e = entry(company.pk, "0001000001-26-000301")
    sec = FakeSec({url_for(e): form4_xml([CEO], [Line("G", "500", None), Line("M", "10", "1")])})
    run_collect(sec, clock, storage, [e])
    assert SecForm4Transaction.objects.count() == 2
    row = insider(AS_OF)[company.pk]
    assert (row["filings_count"], row["open_market_buys_count"]) == (1, 0)
    assert row["open_market_sales_count"] == 0 and row["pct_10b5_1_dollars"] is None


@pytest.mark.django_db
def test_no_filings_and_no_sales_give_zeros_and_a_null_percentage(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    row = insider(AS_OF)[company.pk]
    assert (row["filings_count"], row["open_market_buys_count"]) == (0, 0)
    assert row["pct_10b5_1_dollars"] is None
    e = entry(company.pk, "0001000001-26-000401")
    run_collect(FakeSec({url_for(e): form4_xml([CEO], [BUY])}), clock, storage, [e])
    assert insider(AS_OF)[company.pk]["pct_10b5_1_dollars"] is None


# ------------------------------------------------------------------- T11B.6 re-runs


@pytest.mark.django_db
def test_a_second_run_fetches_nothing_and_creates_no_duplicates(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    entries, sec = worked_example_docs(company)
    run_collect(sec, clock, storage, entries)
    asked = len(sec.requests)
    again = run_collect(sec, clock, storage, entries)
    assert len(sec.requests) == asked
    assert (again.stored, again.already_stored) == (0, 4)
    assert SecForm4Filing.objects.count() == 4
    assert SecForm4Transaction.objects.count() == 4


# --------------------------------------------------------------- T11B.8 unparseable


@pytest.mark.django_db
def test_an_unparseable_document_is_stored_as_failed_excluded_and_reported(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    good = entry(company.pk, "0001000001-26-000501")
    bad = entry(company.pk, "0001000001-26-000502")
    sec = FakeSec(
        {url_for(good): form4_xml([CEO], [BUY]), url_for(bad): b"<ownershipDocument><issuer>"}
    )
    result = run_collect(sec, clock, storage, [good, bad])

    failed = SecForm4Filing.objects.get(accession=bad.accession)
    assert failed.parse_status == "failed"
    assert failed.parse_reason.startswith("not well-formed XML")
    assert failed.transactions.count() == 0
    assert list(result.parse_failures) == [bad.accession]
    assert result.run.status == PipelineRun.Status.PARTIAL
    call = SourceCall.objects.get(scope_key=bad.accession)
    assert (call.status, call.reason_code) == ("failed", "parse_error")
    check = DataQualityCheck.objects.get(run=result.run, check_name="form4_parse_failures")
    assert (check.status, check.observed) == ("warn", "1")
    assert list(check.detail["failures"]) == [bad.accession]
    # Excluded from every count, and not fetched again.
    assert insider(AS_OF)[company.pk]["filings_count"] == 1
    rerun = run_collect(sec, clock, storage, [good, bad])
    assert rerun.already_stored == 2 and len(sec.requests) == 2


@pytest.mark.django_db
def test_a_clean_run_has_a_passing_parse_check(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    entries, sec = worked_example_docs(company)
    result = run_collect(sec, clock, storage, entries)
    check = DataQualityCheck.objects.get(run=result.run, check_name="form4_parse_failures")
    assert (check.status, check.observed) == ("pass", "0")


@pytest.mark.django_db
def test_a_filing_where_the_company_is_only_a_reporting_owner_is_skipped_and_stores_nothing(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    e = entry(company.pk, "0001000001-26-000601")
    other_issuer = form4_xml([CEO], [BUY], issuer_cik="0009999999")
    sec = FakeSec({url_for(e): other_issuer})
    result = run_collect(sec, clock, storage, [e])
    assert not SecForm4Filing.objects.exists()  # no placeholder that could block the issuer
    assert result.not_issuer == [e.accession] and result.parse_failures == {}
    assert result.run.status == PipelineRun.Status.OK  # not a problem
    call = SourceCall.objects.get(scope_key=e.accession)
    assert call.status == "skipped" and call.error_text.startswith("not_the_issuer")
    assert insider(AS_OF)[company.pk]["filings_count"] == 0
    # It is looked at again on the next run, which costs one request and changes nothing.
    again = run_collect(sec, clock, storage, [e])
    assert again.not_issuer == [e.accession] and len(sec.requests) == 2


@pytest.mark.django_db
@pytest.mark.parametrize("issuer_first", [True, False])
def test_a_filing_listed_by_two_companies_is_stored_once_for_its_issuer(
    clock: Clock, storage: LocalRawStorage, issuer_first: bool
) -> None:
    """A holds the stock; B (a fund company) reported it. Both feeds list the accession."""
    issuer = factories.company(name="ISSUER", cik="0001000001")
    holder = factories.company(name="HOLDER", cik="0001000002")
    accession = "0001000002-26-001501"
    as_issuer = entry(issuer.pk, accession, cik="0001000001")
    as_holder = entry(holder.pk, accession, cik="0001000002")
    body = form4_xml([CEO], [BUY], issuer_cik="0001000001")
    sec = FakeSec({url_for(as_issuer): body, url_for(as_holder): body})
    entries = [as_issuer, as_holder] if issuer_first else [as_holder, as_issuer]
    result = run_collect(sec, clock, storage, entries)
    assert result.run.status == PipelineRun.Status.OK
    assert result.fetch_failures == {} and result.parse_failures == {}
    assert result.stored == 1
    if issuer_first:  # the holder's entry finds the accession already stored
        assert (result.already_stored, result.not_issuer) == (1, [])
    else:  # the holder is looked at first, found not to be the issuer, and leaves no row
        assert (result.already_stored, result.not_issuer) == (0, [accession])
    filing = SecForm4Filing.objects.get()
    assert (filing.company_id, filing.parse_status) == (issuer.pk, "ok")


@pytest.mark.django_db
def test_an_accession_already_stored_by_another_company_is_not_stored_again(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    other = factories.company(name="OTHER", cik="0001000009")
    e = entry(company.pk, "0001000001-26-001601")
    stored = factories.form4_filing(other, accession=e.accession)
    result = run_collect(FakeSec({url_for(e): form4_xml([CEO], [BUY])}), clock, storage, [e])
    assert (result.stored, result.already_stored) == (0, 1)
    assert SecForm4Filing.objects.get().pk == stored.pk
    assert result.run.status == PipelineRun.Status.OK


# --------------------------------------------------------------------- failures


@pytest.mark.django_db
def test_a_failed_fetch_stores_nothing_and_is_tried_again_later(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    e = entry(company.pk, "0001000001-26-000701")
    broken = FakeSec({url_for(e): [(503, b"")]})
    result = run_collect(broken, clock, storage, [e])
    assert result.fetch_failures == {e.accession: "http_5xx"}
    assert result.run.status == PipelineRun.Status.PARTIAL
    assert not SecForm4Filing.objects.exists()
    call = SourceCall.objects.get(scope_key=e.accession)
    assert (call.status, call.reason_code, call.attempts, call.http_status) == (
        "failed",
        "http_5xx",
        3,
        503,
    )
    # Not stored, so the next SEC run offers it again, and now it works.
    ok = FakeSec({url_for(e): form4_xml([CEO], [BUY])})
    assert run_collect(ok, clock, storage, [e]).stored == 1


@pytest.mark.django_db
def test_one_failing_document_does_not_stop_the_others(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    entries, sec = worked_example_docs(company)
    sec.responses[url_for(entries[0])] = [(500, b"")]
    result = run_collect(sec, clock, storage, entries)
    assert result.stored == 3
    assert result.fetch_failures == {entries[0].accession: "http_5xx"}


@pytest.mark.django_db
def test_other_companies_continue_when_one_company_has_no_entries(
    clock: Clock, storage: LocalRawStorage
) -> None:
    a = factories.company(name="AAA", cik="0001000001")
    b = factories.company(name="BBB", cik="0001000002")
    ea = entry(a.pk, "0001000001-26-000801", cik="0001000001")
    eb = entry(b.pk, "0001000002-26-000801", cik="0001000002")
    sec = FakeSec(
        {
            url_for(ea): [(404, b"")],
            url_for(eb): form4_xml([CEO], [BUY], issuer_cik="0001000002"),
        }
    )
    result = run_collect(sec, clock, storage, [ea, eb])
    assert result.stored == 1 and list(result.fetch_failures) == [ea.accession]
    assert SecForm4Filing.objects.get().company_id == b.pk


@pytest.mark.django_db
def test_a_missing_user_agent_fails_the_step_without_raising(
    company: Company, storage: LocalRawStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    result = collect_form4([entry(company.pk, "0001000001-26-000901")], AS_OF, storage=storage)
    assert result.stored == 0
    assert result.run.status == PipelineRun.Status.FAILED
    assert DataQualityCheck.objects.get(run=result.run).status == "fail"


# ---------------------------------------------------------------- T11B.10 amendments


@pytest.mark.django_db
def test_an_amendment_replaces_the_original(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    original = entry(company.pk, "0001000001-26-001001", filed=date(2026, 10, 1))
    amendment = entry(company.pk, "0001000001-26-001002", form="4/A", filed=date(2026, 10, 5))
    sec = FakeSec(
        {
            url_for(original): form4_xml([CEO], [Line("P", "1000", "5.00")]),
            url_for(amendment): form4_xml(
                [CEO],
                [Line("P", "1200", "5.00")],
                document_type="4/A",
                original_date="2026-10-01",
            ),
        }
    )
    # Handed over in the wrong order on purpose: the original is still stored first.
    result = run_collect(sec, clock, storage, [amendment, original])
    assert result.amendments_linked == 1
    stored = SecForm4Filing.objects.get(accession=amendment.accession)
    assert (stored.form, stored.amends_accession) == ("4/A", original.accession)
    row = insider(AS_OF)[company.pk]
    assert (row["filings_count"], row["open_market_buys_count"]) == (1, 1)
    effective = SecForm4Transaction.objects.filter(filing__accession=amendment.accession).get()
    assert effective.shares == Decimal("1200")


@pytest.mark.django_db
def test_an_amendment_of_a_different_owner_or_period_does_not_replace_the_original(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    original = entry(company.pk, "0001000001-26-001101", filed=date(2026, 10, 1))
    other_owner = entry(company.pk, "0001000001-26-001102", form="4/A", filed=date(2026, 10, 5))
    other_period = entry(company.pk, "0001000001-26-001103", form="4/A", filed=date(2026, 10, 6))
    sec = FakeSec(
        {
            url_for(original): form4_xml([CEO], [BUY]),
            url_for(other_owner): form4_xml([CFO], [BUY], document_type="4/A"),
            url_for(other_period): form4_xml(
                [CEO], [BUY], document_type="4/A", period="2026-08-15"
            ),
        }
    )
    result = run_collect(sec, clock, storage, [original, other_owner, other_period])
    assert result.amendments_linked == 0
    assert set(SecForm4Filing.objects.values_list("amends_accession", flat=True)) == {""}
    assert insider(AS_OF)[company.pk]["filings_count"] == 3


@pytest.mark.django_db
def test_an_amendment_with_no_stored_original_counts_on_its_own(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    amendment = entry(company.pk, "0001000001-26-001201", form="4/A")
    sec = FakeSec({url_for(amendment): form4_xml([CEO], [BUY], document_type="4/A")})
    run_collect(sec, clock, storage, [amendment])
    assert SecForm4Filing.objects.get().amends_accession == ""
    assert insider(AS_OF)[company.pk]["filings_count"] == 1


# -------------------------------------------------- multi-owner and the SEC hand-off


@pytest.mark.django_db
def test_a_multi_owner_filing_is_one_filing_with_each_line_stored_once(
    company: Company, clock: Clock, storage: LocalRawStorage
) -> None:
    e = entry(company.pk, "0001000001-26-001301")
    sec = FakeSec({url_for(e): form4_xml([DIR1, CEO], [SELL, Line("S", "10", "7.5")])})
    run_collect(sec, clock, storage, [e])
    assert SecForm4Filing.objects.count() == 1
    rows = SecForm4Transaction.objects.order_by("line_no")
    assert [r.reporting_owner_cik for r in rows] == ["0000000003", "0000000003"]
    assert [r.is_director for r in rows] == [True, True]
    row = insider(AS_OF)[company.pk]
    assert (row["filings_count"], row["open_market_sales_count"]) == (1, 2)


def sec_routes(form4_body: bytes) -> dict[str, Any]:
    accession = "0001000001-26-001401"
    return {
        TICKERS_URL: tickers_json({"KTTA": 1000001}),
        SUBMISSIONS_URL.format(cik=CIK): submissions_json(
            [
                ("0001000001-26-000001", "10-K", "2026-03-01", "2025-12-31"),
                (accession, "4", "2026-10-01", "2026-09-30"),
            ]
        ),
        FACTS_URL.format(cik=CIK): (404, b""),
        f"https://www.sec.gov/Archives/edgar/data/1000001/{accession.replace('-', '')}/"
        f"doc-{accession}.htm": form4_body,
    }


@pytest.mark.django_db
def test_the_collector_is_the_form4_handler_of_the_sec_step_and_shares_its_limiter(
    clock: Clock, storage: LocalRawStorage
) -> None:
    times: list[float] = []
    sec = FakeSec(sec_routes(form4_xml([CEO], [BUY])))

    def timed(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        times.append(clock.now())
        return sec(url, headers, timeout)

    # One limiter, as in production: the SEC step and the Form 4 step use the same one.
    client = SecClient(
        user_agent=UA,
        limiter=SecLimiter(clock=clock.now, sleep=clock.sleep),
        transport=timed,
        sleep=clock.sleep,
    )
    co = factories.company(name="KTTA", cik=None)
    factories.ticker(co, ticker="KTTA", valid_from=AS_OF)
    handler = Form4Collector(AS_OF, client=client, storage=storage)
    result = collect_sec(AS_OF, client=client, storage=storage, form4_handler=handler)

    assert [e.accession for e in result.form4_entries] == ["0001000001-26-001401"]
    assert handler.result is not None and handler.result.stored == 1
    assert SecForm4Filing.objects.get().company_id == co.pk
    # Tickers, submissions, facts and the Form 4 document: every gap is at least 110 ms.
    assert len(times) == 4
    assert all(b - a >= MIN_INTERVAL_SECONDS for a, b in zip(times, times[1:], strict=False))
    # The SEC step's own run is untouched by anything the Form 4 step does.
    assert PipelineRun.objects.filter(stage="filings").count() == 2
    assert result.run.status == PipelineRun.Status.OK


@pytest.mark.django_db
def test_a_failing_form4_step_never_undoes_the_sec_step(
    clock: Clock, storage: LocalRawStorage
) -> None:
    sec = FakeSec(sec_routes(b"<html>nope</html>"))
    client = client_for(sec, clock)
    co = factories.company(name="KTTA", cik=None)
    factories.ticker(co, ticker="KTTA", valid_from=AS_OF)
    handler = Form4Collector(AS_OF, client=client, storage=storage)
    result = collect_sec(AS_OF, client=client, storage=storage, form4_handler=handler)
    assert result.run.status == PipelineRun.Status.OK
    assert handler.result is not None and len(handler.result.parse_failures) == 1
