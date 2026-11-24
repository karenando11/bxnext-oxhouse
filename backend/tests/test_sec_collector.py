"""T11.3 to T11.8 (contract and integration): the SEC step against the database.

The responses are synthetic stand-ins for the six F7 sample cases (see ``tests/sec_samples``).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.companies.models import Company, TickerHistory
from apps.marketdata.storage import LocalRawStorage
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from apps.sec.client import MIN_INTERVAL_SECONDS, USER_AGENT_ENV_VAR, SecClient, SecLimiter
from apps.sec.collector import (
    FACTS_URL,
    SUBMISSIONS_URL,
    TICKERS_URL,
    SecCollectionFailed,
    collect_sec,
)
from apps.sec.models import SecFact, SecFiling
from apps.sec.parsing import parse_companyfacts
from tests import factories
from tests.sec_samples import (
    AMEND_FACTS,
    IFRS_FACTS,
    NON_USD_FACTS,
    US_FACTS,
    FakeSec,
    fact,
    facts_json,
    submissions_json,
    tickers_json,
)
from tests.test_sec_client import UA, Clock

DAY = date(2026, 10, 12)
CIK = {"KTTA": "0001000001", "PACS": "0001000002", "BNTX": "0001000003", "NUSD": "0001000004"}
CIK["AMND"] = "0001000005"


def sub_url(ticker: str) -> str:
    return SUBMISSIONS_URL.format(cik=CIK[ticker])


def facts_url(ticker: str) -> str:
    return FACTS_URL.format(cik=CIK[ticker])


def routes() -> dict[str, Any]:
    """Six cases: US filer, US filer without facts, IFRS filer, non-USD filer, amendment, no CIK."""
    return {
        TICKERS_URL: tickers_json(),
        sub_url("KTTA"): submissions_json(
            [
                ("0001000001-26-000001", "10-K", "2026-03-01", "2025-12-31"),
                ("0001000001-26-000002", "8-K", "2026-03-05", ""),
                ("0001000001-26-000003", "4", "2026-10-06", ""),
                ("0001000001-26-000004", "4", "2026-03-06", ""),  # older than 90 days
            ]
        ),
        facts_url("KTTA"): US_FACTS,
        sub_url("PACS"): submissions_json(
            [("0001000002-26-000001", "10-Q", "2026-05-01", "2026-03-31")]
        ),
        sub_url("BNTX"): submissions_json(
            [("0001000003-26-000001", "20-F", "2026-03-20", "2025-12-31")]
        ),
        facts_url("BNTX"): IFRS_FACTS,
        sub_url("NUSD"): submissions_json(
            [("0001000004-26-000001", "6-K", "2026-04-02", "2026-03-31")]
        ),
        facts_url("NUSD"): NON_USD_FACTS,
        sub_url("AMND"): submissions_json(
            [
                ("0001000005-26-000001", "10-K", "2026-03-01", "2025-12-31"),
                ("0001000005-26-000009", "10-K/A", "2026-04-01", "2025-12-31"),
            ]
        ),
        facts_url("AMND"): AMEND_FACTS,
    }


@pytest.fixture
def universe() -> dict[str, Company]:
    """The five companies with a CIK in the feed, plus one that has none."""
    companies = {}
    for name in [*CIK, "NOCK"]:
        companies[name] = factories.company(name=name, cik=None)
        factories.ticker(companies[name], ticker=name, valid_from=DAY)
    return companies


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


def collect(sec: FakeSec, clock: Clock, storage: Any, day: date = DAY, **kw: Any) -> Any:
    return collect_sec(day, client=client_for(sec, clock), storage=storage, **kw)


# ------------------------------------------------------------- T11.3 the six sample cases


@pytest.mark.django_db
def test_the_six_sample_cases(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    sec = FakeSec(routes())
    result = collect(sec, clock, storage)

    assert result.companies == 6
    assert result.run.status == PipelineRun.Status.OK
    assert result.failures == {}

    # Ticker to CIK, ten digits, and a ticker with no CIK is skipped with a reason.
    for name, cik in CIK.items():
        universe[name].refresh_from_db()
        assert universe[name].cik == cik
    assert result.no_cik == ["NOCK"]
    skipped = SourceCall.objects.get(run=result.run, scope_key="NOCK")
    assert (skipped.status, skipped.reason_code) == ("skipped", "no_cik")
    assert sec.count("NOCK") == 0

    # Filings: the four forms are stored, an 8-K and a Form 4 are not.
    stored = {f.accession: f for f in SecFiling.objects.all()}
    assert "0001000001-26-000002" not in stored
    assert "0001000001-26-000003" not in stored
    assert stored["0001000001-26-000001"].form == "10-K"
    assert stored["0001000001-26-000001"].period_end == date(2025, 12, 31)
    assert stored["0001000002-26-000001"].form == "10-Q"
    assert stored["0001000003-26-000001"].form == "20-F"
    assert stored["0001000004-26-000001"].form == "6-K"
    assert len(stored) == 6

    # Facts: only allow-listed tags, with accession, filed date and period.
    ktta = SecFact.objects.filter(company=universe["KTTA"])
    assert sorted(ktta.values_list("tag", flat=True)) == [
        "CashAndCashEquivalentsAtCarryingValue",
        "NetCashProvidedByUsedInOperatingActivities",
    ]
    cash = ktta.get(tag="CashAndCashEquivalentsAtCarryingValue")
    assert (cash.taxonomy, cash.unit, cash.value) == ("us-gaap", "USD", 1_500_000)
    assert (cash.accession, cash.filed_at, cash.period_end) == (
        "0001000001-26-000001",
        date(2026, 3, 1),
        date(2025, 12, 31),
    )
    ocf = ktta.get(tag="NetCashProvidedByUsedInOperatingActivities")
    assert (ocf.period_start, ocf.value) == (date(2025, 1, 1), -400_000)
    assert cash.source_call is not None and cash.source_call.feed == "sec_companyfacts"

    # A company the SEC has no facts for (404) is skipped, not failed.
    assert not SecFact.objects.filter(company=universe["PACS"]).exists()
    assert result.skipped_facts["PACS"] == "no_companyfacts"


@pytest.mark.django_db
def test_every_request_carries_the_declared_user_agent(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    sec = FakeSec(routes())
    collect(sec, clock, storage)
    assert sec.requests
    assert {headers["User-Agent"] for _, headers in sec.requests} == {UA}


@pytest.mark.django_db
def test_requests_stay_110_ms_apart_across_feeds(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    times: list[float] = []
    sec = FakeSec(routes())

    def timed(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        times.append(clock.now())
        return sec(url, headers, timeout)

    client = SecClient(
        user_agent=UA,
        limiter=SecLimiter(clock=clock.now, sleep=clock.sleep),
        transport=timed,
        sleep=clock.sleep,
    )
    collect_sec(DAY, client=client, storage=storage)
    assert len(times) > 10
    assert all(b - a >= MIN_INTERVAL_SECONDS for a, b in zip(times, times[1:], strict=False))


# ----------------------------------------------------------- T11.4 and T11.5 taxonomies


@pytest.mark.django_db
def test_an_ifrs_filer_uses_ifrs_full_tags(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    collect(FakeSec(routes()), clock, storage)
    facts = SecFact.objects.filter(company=universe["BNTX"])
    assert [(f.taxonomy, f.tag, f.unit) for f in facts] == [
        ("ifrs-full", "CashAndCashEquivalents", "USD")
    ]


def test_ifrs_is_read_only_when_us_gaap_has_nothing() -> None:
    payload = json.loads(
        facts_json("us-gaap", {"Revenues": {"USD": [fact(10, "2025-12-31", "a-1")]}})
    )
    payload["facts"]["ifrs-full"] = {"Revenue": {"units": {"USD": [fact(99, "2025-12-31", "a-2")]}}}
    parsed = parse_companyfacts(json.dumps(payload).encode())
    assert (parsed.taxonomy, {r.tag for r in parsed.rows}) == ("us-gaap", {"Revenues"})
    del payload["facts"]["us-gaap"]
    parsed = parse_companyfacts(json.dumps(payload).encode())
    assert (parsed.taxonomy, {r.tag for r in parsed.rows}) == ("ifrs-full", {"Revenue"})


@pytest.mark.django_db
def test_a_non_usd_filer_stores_no_facts_and_says_why(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    result = collect(FakeSec(routes()), clock, storage)
    assert not SecFact.objects.filter(company=universe["NUSD"]).exists()
    assert result.skipped_facts["NUSD"] == "non_usd_only"
    call = SourceCall.objects.get(feed="sec_companyfacts", scope_key=CIK["NUSD"], run=result.run)
    assert (call.status, call.reason_code) == ("skipped", "non_usd_only")


# ------------------------------------------------------------------ T11.6 amendments


@pytest.mark.django_db
def test_a_10k_amendment_is_detected(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    collect(FakeSec(routes()), clock, storage)
    original = SecFiling.objects.get(accession="0001000005-26-000001")
    amended = SecFiling.objects.get(accession="0001000005-26-000009")
    assert (original.is_amendment, amended.is_amendment) == (False, True)
    assert amended.form == "10-K/A"
    # Both versions of the fact are kept, each with its own accession and filed date.
    values = SecFact.objects.filter(company=universe["AMND"]).order_by("filed_at")
    assert [(f.accession[-2:], f.value) for f in values] == [("01", 700_000), ("09", 650_000)]


# ------------------------------------------------------------------ T11.7 reloads


@pytest.mark.django_db
def test_reloading_the_same_payload_creates_no_new_rows(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    collect(FakeSec(routes()), clock, storage)
    filings, facts = SecFiling.objects.count(), SecFact.objects.count()
    second = FakeSec(routes())
    result = collect(second, clock, storage)
    assert (SecFiling.objects.count(), SecFact.objects.count()) == (filings, facts)
    assert (result.filings_added, result.facts_added) == (0, 0)
    # Nothing is new, so companyfacts is not even asked for again.
    assert second.count("companyfacts/CIK0001000001") == 0


@pytest.mark.django_db
def test_an_unchanged_facts_payload_is_not_reloaded(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    collect(FakeSec(routes()), clock, storage)
    before = SecFact.objects.count()
    # A new filing sends the company back to companyfacts, whose payload has not changed.
    changed = routes()
    changed[sub_url("KTTA")] = submissions_json(
        [
            ("0001000001-26-000001", "10-K", "2026-03-01", "2025-12-31"),
            ("0001000001-26-000010", "10-Q", "2026-05-10", "2026-03-31"),
        ]
    )
    result = collect(FakeSec(changed), clock, storage, day=DAY + timedelta(days=1))
    assert result.filings_added == 1
    assert result.unchanged == 1
    assert SecFact.objects.count() == before
    call = SourceCall.objects.filter(feed="sec_companyfacts", scope_key=CIK["KTTA"]).latest(
        "requested_at", "pk"
    )
    assert (call.status, call.reason_code) == ("skipped", None)


@pytest.mark.django_db
def test_a_changed_facts_payload_adds_only_the_new_facts(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    collect(FakeSec(routes()), clock, storage)
    changed = routes()
    changed[sub_url("KTTA")] = submissions_json(
        [("0001000001-26-000010", "10-Q", "2026-05-10", "2026-03-31")]
    )
    changed[facts_url("KTTA")] = facts_json(
        "us-gaap",
        {
            "CashAndCashEquivalentsAtCarryingValue": {
                "USD": [
                    fact(1_500_000, "2025-12-31", "0001000001-26-000001"),
                    fact(1_100_000, "2026-03-31", "0001000001-26-000010", form="10-Q"),
                ]
            }
        },
    )
    result = collect(FakeSec(changed), clock, storage, day=DAY + timedelta(days=1))
    assert result.facts_added == 1
    assert SecFact.objects.filter(company__name="KTTA").count() == 3


# ------------------------------------------------------------------ T11.8 isolation


@pytest.mark.django_db
def test_one_failing_company_does_not_stop_the_others(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    broken = routes()
    broken[sub_url("KTTA")] = [(503, b"")]
    result = collect(FakeSec(broken), clock, storage)

    assert result.failures == {"KTTA": "http_5xx"}
    assert result.run.status == PipelineRun.Status.PARTIAL
    call = SourceCall.objects.get(run=result.run, feed="sec_submissions", scope_key=CIK["KTTA"])
    assert (call.status, call.reason_code, call.attempts, call.http_status) == (
        "failed",
        "http_5xx",
        3,
        503,
    )
    # The others completed.
    assert SecFact.objects.filter(company=universe["BNTX"]).exists()
    assert SecFact.objects.filter(company=universe["AMND"]).exists()
    assert SecFiling.objects.filter(company=universe["PACS"]).exists()
    check = DataQualityCheck.objects.get(run=result.run, check_name="sec_company_failures")
    assert (check.status, check.observed) == ("warn", "1")
    assert check.detail["failures"] == {"KTTA": "http_5xx"}


@pytest.mark.django_db
def test_a_malformed_payload_is_a_parse_error_for_that_company_only(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    broken = routes()
    broken[facts_url("BNTX")] = b"<html>not json</html>"
    result = collect(FakeSec(broken), clock, storage)
    assert result.failures == {"BNTX": "parse_error"}
    failed = SourceCall.objects.get(run=result.run, scope_key=CIK["BNTX"], status="failed")
    assert failed.reason_code == "parse_error"
    assert SecFact.objects.filter(company=universe["KTTA"]).exists()
    # The next run tries BNTX's facts again and succeeds.
    result = collect(FakeSec(routes()), clock, storage, day=DAY + timedelta(days=1))
    assert result.failures == {}
    assert SecFact.objects.filter(company=universe["BNTX"]).exists()


@pytest.mark.django_db
def test_a_failed_facts_fetch_is_retried_on_the_next_run(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    broken = routes()
    broken[facts_url("KTTA")] = [(500, b"")]
    first = collect(FakeSec(broken), clock, storage)
    assert first.failures == {"KTTA": "http_5xx"}
    assert not SecFact.objects.filter(company=universe["KTTA"]).exists()
    # No new filing, but the failed call sends it back to companyfacts.
    second = collect(FakeSec(routes()), clock, storage, day=DAY + timedelta(days=1))
    assert second.failures == {}
    assert SecFact.objects.filter(company=universe["KTTA"]).count() == 2


# ------------------------------------------------------------------ the rest of the step


@pytest.mark.django_db
def test_the_ticker_file_is_fetched_once_a_day(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    sec = FakeSec(routes())
    collect(sec, clock, storage)
    collect(sec, clock, storage)
    assert sec.count("company_tickers") == 1
    collect(sec, clock, storage, day=DAY + timedelta(days=1))
    assert sec.count("company_tickers") == 2


@pytest.mark.django_db
def test_new_form4_entries_are_handed_over_once(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    handed: list[Any] = []
    result = collect(FakeSec(routes()), clock, storage, form4_handler=handed.append)
    # The Form 4 filed in March is outside the 90-day window and is never queued.
    assert [e.accession for e in result.form4_entries] == ["0001000001-26-000003"]
    assert handed == [result.form4_entries]
    entry = result.form4_entries[0]
    assert (entry.form, entry.cik, entry.filed_at) == ("4", CIK["KTTA"], date(2026, 10, 6))
    # Once the Form 4 collector has stored it, it is not offered again.
    factories.form4_filing(universe["KTTA"], accession="0001000001-26-000003")
    again = collect(FakeSec(routes()), clock, storage)
    assert again.form4_entries == []


@pytest.mark.django_db
def test_a_failing_form4_handler_does_not_undo_the_step(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    def boom(entries: list[Any]) -> None:
        raise RuntimeError("collector down")

    result = collect(FakeSec(routes()), clock, storage, form4_handler=boom)
    assert result.run.status == PipelineRun.Status.OK
    assert SecFiling.objects.exists()


@pytest.mark.django_db
def test_raw_payloads_are_kept_and_recorded_on_the_call(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    result = collect(FakeSec(routes()), clock, storage)
    call = SourceCall.objects.get(run=result.run, feed="sec_companyfacts", scope_key=CIK["KTTA"])
    assert call.status == "ok" and call.content_hash and call.bytes == len(US_FACTS)
    assert storage.get(call.storage_uri) == US_FACTS


@pytest.mark.django_db
def test_the_run_can_be_limited_to_some_tickers(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    sec = FakeSec(routes())
    result = collect(sec, clock, storage, tickers=["ktta"])
    assert result.companies == 1
    assert sec.count("CIK0001000002") == 0


@pytest.mark.django_db
def test_a_company_without_a_current_ticker_is_not_collected(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    TickerHistory.objects.filter(ticker="PACS").update(valid_to=DAY)
    sec = FakeSec(routes())
    result = collect(sec, clock, storage)
    assert result.companies == 5
    assert sec.count("CIK0001000002") == 0


@pytest.mark.django_db
def test_stage_records_its_steps(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    result = collect(FakeSec(routes()), clock, storage)
    run = PipelineRun.objects.get(pk=result.run.pk)
    assert run.stage == "filings"
    assert [s["step_name"] for s in run.steps] == ["ticker_map", "companies"]
    assert run.finished_at is not None


# ------------------------------------------------------------- the stage cannot start


@pytest.mark.django_db
def test_an_unavailable_ticker_file_stops_the_stage(
    universe: dict[str, Company], clock: Clock, storage: LocalRawStorage
) -> None:
    sec = FakeSec({TICKERS_URL: [(500, b"")]})
    with pytest.raises(SecCollectionFailed) as caught:
        collect(sec, clock, storage)
    assert caught.value.reason_code == "http_5xx"
    run = PipelineRun.objects.get()
    assert run.status == "failed"
    assert DataQualityCheck.objects.get(run=run).status == "fail"
    assert SourceCall.objects.get(run=run).status == "failed"
    assert sec.count("submissions") == 0


@pytest.mark.django_db
def test_a_missing_user_agent_stops_the_stage(
    monkeypatch: pytest.MonkeyPatch, storage: LocalRawStorage
) -> None:
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    with pytest.raises(SecCollectionFailed):
        collect_sec(DAY, storage=storage)
    assert PipelineRun.objects.get().status == "failed"


@pytest.mark.django_db
def test_the_command_reports_a_stopped_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(USER_AGENT_ENV_VAR, raising=False)
    with pytest.raises(CommandError, match="Stage stopped"):
        call_command("collect_sec", "--date", "2026-10-12")
    with pytest.raises(CommandError, match="Not a date"):
        call_command("collect_sec", "--date", "nope")
