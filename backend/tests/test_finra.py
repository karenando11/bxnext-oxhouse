"""T11A.1 to T11A.8: the FINRA short-volume step (parser, client and database load).

The files are synthetic stand-ins for F11 (see ``tests/finra_samples``). The metric formulas
themselves are tested in ``test_read_views``; here they are checked on data the collector loaded.
"""

from __future__ import annotations

import socket
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.companies.models import Company
from apps.marketdata.finra.client import FinraDownloadError, FinraNoFile, download, file_url
from apps.marketdata.finra.ingest import (
    BACKFILL_DAYS,
    RETRY_DAYS,
    collect_short_volume,
    dates_to_fetch,
    normalize_symbol,
)
from apps.marketdata.finra.parser import ShortVolumeFormatError, parse_short_volume
from apps.marketdata.models import ShortVolumeDaily
from apps.marketdata.storage import LocalRawStorage, sha256_hex
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from tests import factories
from tests.finra_samples import HEADER, FakeFinra, finra_file
from tests.test_read_views import short_volume as metric_at

AS_OF = date(2026, 10, 12)  # a Monday
WED, THU, FRI = date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)
SAT, SUN = date(2026, 10, 10), date(2026, 10, 11)


def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def storage(tmp_path: Path) -> LocalRawStorage:
    return LocalRawStorage(tmp_path / "raw")


@pytest.fixture
def universe() -> dict[str, Company]:
    companies = {}
    for name in ["AAA", "BBB", "CCC", "BRK-B"]:
        companies[name] = factories.company(name=name)
        factories.ticker(companies[name], ticker=name)
    return companies


def day_file(day: date, **kw: Any) -> bytes:
    rows = [
        ("AAA", 600, 1000, "B,Q,N"),
        ("BBB", 300, 1000, "B,Q,N"),
        ("BRK.B", 100, 400, "B,Q,N"),
        ("ZZZZ", 50, 100, "B,Q,N"),  # not in the universe
    ]
    return finra_file(day, rows, **kw)


def collect(fake: FakeFinra, storage: Any, as_of: date = AS_OF, **kw: Any) -> Any:
    return collect_short_volume(as_of, transport=fake, sleep=no_sleep, storage=storage, **kw)


# ----------------------------------------------------------------------------- parser


def test_parser_reads_rows_and_ignores_the_record_count() -> None:
    parsed = parse_short_volume(day_file(FRI), FRI)
    assert [(r.symbol, r.short_volume, r.total_volume, r.market) for r in parsed.rows][:2] == [
        ("AAA", 600, 1000, "B,Q,N"),
        ("BBB", 300, 1000, "B,Q,N"),
    ]
    assert (len(parsed.rows), parsed.malformed) == (4, 0)
    assert parsed.rows[0].short_exempt_volume == 0


def test_parser_skips_and_counts_bad_rows() -> None:
    text = "\n".join(
        [
            HEADER,
            f"{FRI:%Y%m%d}|AAA|600|0|1000|B",
            f"{FRI:%Y%m%d}|BAD|x|0|1000|B",
            f"{FRI:%Y%m%d}|NEG|-5|0|1000|B",
            f"{THU:%Y%m%d}|OLD|5|0|10|B",
            f"{FRI:%Y%m%d}|AAA|1|0|2|B",  # repeats a key
            f"{FRI:%Y%m%d}||1|0|2|B",
            "12",
        ]
    )
    parsed = parse_short_volume(text.encode(), FRI)
    assert [r.symbol for r in parsed.rows] == ["AAA"]
    assert parsed.malformed == 5


def test_parser_rounds_fractional_share_counts_half_up() -> None:
    text = "\n".join(
        [
            HEADER,
            f"{FRI:%Y%m%d}|AAA|637633.605702|305.5|813100.060152|B,Q,N",
            f"{FRI:%Y%m%d}|BBB|10.5|0.4999|20.4999|B",
            f"{FRI:%Y%m%d}|NAN|NaN|0|10|B",
            f"{FRI:%Y%m%d}|INF|Infinity|0|10|B",
            "3",
        ]
    )
    parsed = parse_short_volume(text.encode(), FRI)
    got = {r.symbol: (r.short_volume, r.short_exempt_volume, r.total_volume) for r in parsed.rows}
    assert got == {"AAA": (637634, 306, 813100), "BBB": (11, 0, 20)}
    assert parsed.malformed == 2


@pytest.mark.parametrize(
    "content",
    [b"", b"Symbol|Volume\nAAA|1\n", f"{HEADER}\n12\n".encode(), b"\xff\xfe\x00bad"],
)
def test_parser_rejects_an_unusable_file(content: bytes) -> None:
    with pytest.raises(ShortVolumeFormatError):
        parse_short_volume(content, FRI)


def test_symbols_are_case_sensitive_in_the_file() -> None:
    text = f"{HEADER}\n{FRI:%Y%m%d}|BCPC|1|0|2|B\n{FRI:%Y%m%d}|BCpC|3|0|4|Q\n2\n"
    parsed = parse_short_volume(text.encode(), FRI)
    assert [(r.symbol, r.short_volume) for r in parsed.rows] == [("BCPC", 1), ("BCpC", 3)]
    assert parsed.malformed == 0


def test_symbols_compare_without_punctuation() -> None:
    assert normalize_symbol("brk.b") == normalize_symbol("BRK-B") == "BRKB"


# ----------------------------------------------------------------------------- client


def test_the_file_url_names_the_consolidated_file() -> None:
    assert file_url(FRI).endswith("/CNMSshvol20261009.txt")


@pytest.mark.parametrize("status", [404, 403])
def test_a_missing_file_is_not_an_error(status: int) -> None:
    with pytest.raises(FinraNoFile):
        download(FRI, transport=lambda u, t: (status, b""), sleep=no_sleep)


def test_transient_failures_are_retried_then_reported() -> None:
    fake = FakeFinra({FRI: [(503, b""), (429, b""), (200, b"ok")]})
    got = download(FRI, transport=fake, sleep=no_sleep)
    assert (got.attempts, got.content) == (3, b"ok")
    fake = FakeFinra({FRI: [(500, b"")]})
    with pytest.raises(FinraDownloadError) as caught:
        download(FRI, transport=fake, sleep=no_sleep)
    assert (caught.value.reason_code, caught.value.attempts) == ("http_5xx", 3)
    assert len(fake.urls) == 3
    fake = FakeFinra({FRI: [TimeoutError(), socket.gaierror("dns"), TimeoutError()]})
    with pytest.raises(FinraDownloadError) as caught:
        download(FRI, transport=fake, sleep=no_sleep)
    assert caught.value.reason_code == "timeout"
    with pytest.raises(FinraDownloadError) as caught:
        download(FRI, transport=FakeFinra({FRI: (400, b"")}), sleep=no_sleep)
    assert (caught.value.reason_code, caught.value.attempts) == ("http_4xx", 1)


# ------------------------------------------------------------- T11A.1, T11A.2 three days


@pytest.mark.django_db
def test_three_trading_days_and_a_weekend(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    files = {d: day_file(d) for d in (WED, THU, FRI)}
    fake = FakeFinra(files)
    result = collect(fake, storage, dates=[WED, THU, FRI, SAT, SUN])

    assert result.of("loaded") == [WED, THU, FRI]
    assert result.of("no_file_for_date") == [SAT, SUN]
    assert result.failed == {}
    assert result.run.status == PipelineRun.Status.OK
    assert fake.days_asked() == [WED, THU, FRI]  # no request for a weekend

    # Only universe tickers are stored: AAA, BBB and BRK.B (matched to BRK-B) on each day.
    assert ShortVolumeDaily.objects.count() == 9
    assert not ShortVolumeDaily.objects.filter(ticker_raw="ZZZZ").exists()
    row = ShortVolumeDaily.objects.get(trade_date=FRI, ticker_raw="AAA")
    assert (row.short_volume, row.total_volume, row.short_exempt_volume) == (600, 1000, 0)
    assert (row.market, row.company_id) == ("B,Q,N", universe["AAA"].pk)
    brk = ShortVolumeDaily.objects.get(trade_date=FRI, ticker_raw="BRK.B")
    assert brk.company_id == universe["BRK-B"].pk

    # The raw file is kept unchanged under its hash, and the call records it.
    call = SourceCall.objects.get(feed="finra_regsho", scope_key=str(FRI), status="ok")
    assert call.content_hash == sha256_hex(files[FRI])
    assert storage.get(call.storage_uri) == files[FRI]
    assert (call.http_status, call.attempts, call.bytes) == (200, 1, len(files[FRI]))
    assert row.source_call_id == call.pk

    # A weekend is recorded as having no file, and is not a failure.
    weekend = SourceCall.objects.get(feed="finra_regsho", scope_key=str(SAT))
    assert (weekend.status, weekend.reason_code) == ("skipped", "no_file_for_date")


@pytest.mark.django_db
def test_a_holiday_is_recorded_and_tried_again_when_the_file_appears(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    fake = FakeFinra({FRI: day_file(FRI)})  # Thursday has no file yet
    first = collect(fake, storage, dates=[THU, FRI])
    assert first.of("no_file_for_date") == [THU]
    assert first.run.status == PipelineRun.Status.OK
    assert first.failed == {}
    call = SourceCall.objects.get(scope_key=str(THU))
    assert (call.status, call.reason_code, call.http_status) == ("skipped", "no_file_for_date", 404)

    fake.files[THU] = day_file(THU)
    assert THU in dates_to_fetch(AS_OF)  # inside the retry window, not loaded
    second = collect(fake, storage)
    assert second.of("loaded") == [THU]
    assert FRI not in second.of("loaded")
    assert ShortVolumeDaily.objects.filter(trade_date=THU).count() == 3


# ----------------------------------------------------------------------------- backfill


@pytest.mark.django_db
def test_the_first_run_fetches_45_calendar_days(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    expected = [AS_OF - timedelta(days=n) for n in range(BACKFILL_DAYS, 0, -1)]
    assert dates_to_fetch(AS_OF) == expected
    fake = FakeFinra()
    result = collect(fake, storage)
    assert [o.day for o in result.outcomes] == expected
    weekdays = [d for d in expected if d.weekday() < 5]
    assert fake.days_asked() == weekdays
    assert result.failed == {}


@pytest.mark.django_db
def test_later_runs_look_back_one_week_and_skip_loaded_dates(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    collect(FakeFinra({FRI: day_file(FRI)}), storage, dates=[FRI])
    days = dates_to_fetch(AS_OF)
    assert len(days) == RETRY_DAYS - 1  # the week before, minus Friday which is loaded
    assert FRI not in days
    assert AS_OF - timedelta(days=RETRY_DAYS) in days


# ----------------------------------------------------------------------------- reloads


@pytest.mark.django_db
def test_loading_a_date_twice_creates_no_duplicates(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    fake = FakeFinra({FRI: day_file(FRI)})
    collect(fake, storage, dates=[FRI])
    again = collect(fake, storage, dates=[FRI])
    assert again.of("unchanged") == [FRI]
    assert ShortVolumeDaily.objects.count() == 3
    assert SourceCall.objects.filter(scope_key=str(FRI), status="skipped").count() == 1


@pytest.mark.django_db
def test_a_corrected_file_updates_rows_in_place(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    collect(FakeFinra({FRI: day_file(FRI)}), storage, dates=[FRI])
    corrected = finra_file(FRI, [("AAA", 700, 1000, "B,Q,N")])
    result = collect(FakeFinra({FRI: corrected}), storage, dates=[FRI])
    assert result.of("loaded") == [FRI]
    assert ShortVolumeDaily.objects.filter(ticker_raw="AAA").count() == 1
    assert ShortVolumeDaily.objects.get(ticker_raw="AAA").short_volume == 700


# ------------------------------------------------------------------------- coverage


@pytest.mark.django_db
def test_a_universe_ticker_missing_from_a_file_is_reported(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    result = collect(FakeFinra({FRI: day_file(FRI)}), storage, dates=[FRI])
    check = DataQualityCheck.objects.get(run=result.run, check_name="finra_universe_coverage")
    assert (check.status, check.scope, check.observed) == ("warn", f"finra.{FRI}", "3 found")
    assert check.detail["reason_code"] == "not_in_finra_file"
    assert check.detail["missing"] == ["CCC"]
    assert check.detail["missing_count"] == 1


@pytest.mark.django_db
def test_a_file_with_none_of_the_universe_fails_the_coverage_check(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    other = finra_file(FRI, [("ZZZZ", 1, 2, "B")])
    result = collect(FakeFinra({FRI: other}), storage, dates=[FRI])
    check = DataQualityCheck.objects.get(run=result.run, check_name="finra_universe_coverage")
    assert check.status == "fail"
    assert result.of("loaded") == [FRI]  # the load stands; Step 16 decides what it means


@pytest.mark.django_db
def test_malformed_rows_are_reported_but_the_day_loads(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    text = f"{HEADER}\n{FRI:%Y%m%d}|AAA|600|0|1000|B\n{FRI:%Y%m%d}|BBB|oops|0|1000|B\n2\n"
    result = collect(FakeFinra({FRI: text.encode()}), storage, dates=[FRI])
    assert ShortVolumeDaily.objects.count() == 1
    check = DataQualityCheck.objects.get(run=result.run, check_name="finra_malformed_rows")
    assert (check.status, check.observed) == ("warn", "1")


# ------------------------------------------------------------ metrics from loaded data


def load_days(storage: Any, days: list[date], ratio_by_day: dict[date, int] | None = None) -> None:
    ratios = ratio_by_day or {}
    files = {d: finra_file(d, [("AAA", ratios.get(d, 500), 1000, "B,Q,N")]) for d in days}
    collect(FakeFinra(files), storage, dates=days)


@pytest.mark.django_db
def test_four_days_of_data_give_null_metrics_with_a_reason(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    load_days(storage, [date(2026, 10, 6), WED, THU, FRI])  # four trading days
    row = metric_at(AS_OF)[universe["AAA"].pk]
    assert row["status"] == "insufficient_history"
    assert row["reason_codes"] == ["insufficient_history"]
    assert row["latest_ratio"] is None and row["delta_pts"] is None


@pytest.mark.django_db
def test_the_view_matches_the_formulas_on_loaded_data(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    days = sorted(
        (d for d in (AS_OF - timedelta(days=n) for n in range(1, 15)) if d.weekday() < 5),
        reverse=True,
    )  # ten trading days, latest first
    # The latest five days are higher (0.60 down to 0.52) than the five before (0.40).
    ratios = dict(zip(days, [600, 580, 560, 540, 520, 400, 400, 400, 400, 400], strict=True))
    load_days(storage, days, ratios)
    row = metric_at(AS_OF)[universe["AAA"].pk]
    daily = [ratios[d] / 1000 for d in days]
    avg5, avg30 = sum(daily[:5]) / 5, sum(daily) / len(daily)
    assert row["status"] == "ok"
    assert float(row["latest_ratio"]) == pytest.approx(daily[0])
    assert row["avg_ratio_5d"] == pytest.approx(avg5, abs=1e-9)
    assert row["avg_ratio_30d"] == pytest.approx(avg30, abs=1e-9)
    assert row["delta_pts"] == pytest.approx((avg5 - avg30) * 100, abs=1e-9)
    assert row["trend"] == "rising"


# ------------------------------------------------------- failure and carry-forward


@pytest.mark.django_db
def test_a_failed_download_is_stored_and_the_latest_days_are_carried_then_recover(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    week = [d for d in (AS_OF - timedelta(days=n) for n in range(2, 10)) if d.weekday() < 5]
    load_days(storage, week)
    latest = max(week)

    failing = AS_OF  # the next trading day's file fails after three attempts
    result = collect(FakeFinra({failing: [(503, b"")]}), storage, dates=[failing])
    assert result.failed == {failing: "http_5xx"}
    assert result.run.status == PipelineRun.Status.PARTIAL
    call = SourceCall.objects.get(run=result.run, scope_key=str(failing))
    assert (call.status, call.reason_code, call.attempts, call.http_status) == (
        "failed",
        "http_5xx",
        3,
        503,
    )
    assert (
        DataQualityCheck.objects.get(run=result.run, check_name="finra_download").status == "warn"
    )
    assert not ShortVolumeDaily.objects.filter(trade_date=failing).exists()

    # The view still reports the stored days, with their age.
    row = metric_at(AS_OF)[universe["AAA"].pk]
    assert row["status"] == "ok"
    assert row["latest_trade_date"] == latest
    assert row["age_days"] == (AS_OF - latest).days and row["is_stale"] is False
    # A week later with nothing new it is stale; the next run retries the failed date.
    assert metric_at(AS_OF + timedelta(days=10))[universe["AAA"].pk]["is_stale"] is True
    assert failing in dates_to_fetch(AS_OF + timedelta(days=1))

    recovered = collect(FakeFinra({failing: day_file(failing)}), storage, dates=[failing])
    assert recovered.of("loaded") == [failing]
    row = metric_at(AS_OF)[universe["AAA"].pk]
    assert (row["latest_trade_date"], row["age_days"]) == (failing, 0)


@pytest.mark.django_db
def test_an_unreadable_file_fails_that_date_only(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    fake = FakeFinra({THU: b"<html>maintenance</html>", FRI: day_file(FRI)})
    result = collect(fake, storage, dates=[THU, FRI])
    assert result.failed == {THU: "parse_error"}
    assert result.of("loaded") == [FRI]
    assert DataQualityCheck.objects.get(run=result.run, check_name="finra_format").status == "fail"
    assert result.run.status == PipelineRun.Status.PARTIAL


@pytest.mark.django_db
def test_a_failure_never_raises(universe: dict[str, Company], storage: LocalRawStorage) -> None:
    result = collect(FakeFinra({FRI: [socket.gaierror("down")]}), storage, dates=[FRI])
    assert result.failed == {FRI: "fetch_error"}


@pytest.mark.django_db
def test_the_run_records_one_step_per_date(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    result = collect(FakeFinra({FRI: day_file(FRI)}), storage, dates=[FRI, SAT])
    run = PipelineRun.objects.get(pk=result.run.pk)
    assert run.stage == "filings"
    assert [s["step_name"] for s in run.steps] == [str(FRI), str(SAT)]
    assert run.finished_at is not None


# ----------------------------------------------------------------------------- command


@pytest.mark.django_db
def test_the_command_rejects_a_bad_date() -> None:
    with pytest.raises(CommandError, match="Not a date"):
        call_command("collect_short_volume", "--date", "nope")
    with pytest.raises(CommandError, match="Not a date"):
        call_command("collect_short_volume", "--day", "2026-13-40")


@pytest.mark.django_db
def test_a_lowercase_symbol_is_a_different_security_and_is_not_matched(
    universe: dict[str, Company], storage: LocalRawStorage
) -> None:
    rows = [("AAA", 600, 1000, "B,Q,N"), ("AAa", 90, 100, "Q"), ("BRK/B", 100, 400, "B,Q,N")]
    result = collect(FakeFinra({FRI: finra_file(FRI, rows)}), storage, dates=[FRI])
    stored = ShortVolumeDaily.objects.filter(trade_date=FRI)
    assert sorted(stored.values_list("ticker_raw", flat=True)) == ["AAA", "BRK/B"]
    assert result.of("loaded") == [FRI]
