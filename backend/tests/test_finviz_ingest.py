"""T10.2 to T10.7 (integration): the Finviz step against the database, with a fake download."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.companies.models import Company, TickerHistory
from apps.marketdata.finviz.columns import FIELDS, NUMERIC_FIELDS
from apps.marketdata.finviz.ingest import (
    FinvizIngestFailed,
    build_request,
    ingest_finviz,
    scope_key,
)
from apps.marketdata.models import FinvizSnapshot
from apps.marketdata.storage import LocalRawStorage, sha256_hex
from apps.platform.models import DataQualityCheck, PipelineRun, Setting, SourceCall
from tests.finviz_samples import company_row, export_csv, universe
from tests.test_finviz_client import TOKEN, FakeTransport

DAY = date(2026, 10, 12)


@pytest.fixture
def storage(tmp_path: Path) -> LocalRawStorage:
    return LocalRawStorage(tmp_path / "raw")


def ingest(day: date = DAY, content: bytes | None = None, storage: Any = None, **kw: Any) -> Any:
    return ingest_finviz(day, content=content, storage=storage, **kw)


def checks(result: Any, name: str) -> list[DataQualityCheck]:
    return [c for c in result.checks if c.check_name == name]


# ---------------------------------------------------------------- download and store


@pytest.mark.django_db
def test_download_store_and_load(storage: LocalRawStorage) -> None:
    raw = export_csv(universe(10))
    transport = FakeTransport((200, raw))
    result = ingest_finviz(DAY, token=TOKEN, transport=transport, storage=storage)
    assert (result.rows, result.new_companies, result.unchanged) == (10, 10, False)
    call = result.call
    assert (call.feed, call.status, call.http_status, call.attempts) == (
        "finviz_elite",
        "ok",
        200,
        1,
    )
    assert call.content_hash == sha256_hex(raw)
    assert call.bytes == len(raw)
    assert storage.get(call.storage_uri) == raw  # stored unchanged
    assert call.scope_key == "2026-10-12|v=151|f=sec_healthcare|c=default"
    assert TOKEN not in call.scope_key + call.storage_uri + call.error_text
    assert result.run.status == "ok"
    assert result.run.stage == "market"
    assert [s["step_name"] for s in result.run.steps] == [
        "download", "store_raw", "parse", "load", "quality_checks",
    ]  # fmt: skip
    assert FinvizSnapshot.objects.filter(as_of_date=DAY).count() == 10


@pytest.mark.django_db
def test_the_source_call_records_the_filter_used(storage: LocalRawStorage) -> None:
    Setting.objects.filter(key="finviz.sector_filter").update(value="sec_technology")
    result = ingest(content=export_csv(universe(3)), storage=storage)
    assert "f=sec_technology" in result.call.scope_key


@pytest.mark.django_db
def test_build_request_reads_the_settings() -> None:
    Setting.objects.filter(key="finviz.view").update(value=111)
    Setting.objects.create(key="finviz.columns", value="1,2,3")
    Setting.objects.create(key="finviz.attempts", value=2)
    request = build_request()
    assert (request.view, request.sector_filter, request.columns, request.attempts) == (
        111,
        "sec_healthcare",
        "1,2,3",
        2,
    )
    assert "c=" in scope_key(DAY, request) and "c=default" not in scope_key(DAY, request)
    assert len(scope_key(DAY, request)) < 128


@pytest.mark.django_db
def test_every_mapped_column_is_stored_for_every_company(storage: LocalRawStorage) -> None:
    ingest(content=export_csv(universe(12)), storage=storage)
    assert len(FIELDS) == 50
    for snap in FinvizSnapshot.objects.all():
        blanks = [f for f in NUMERIC_FIELDS if getattr(snap, f) is None]
        # only the columns the sample deliberately leaves as "-" for loss-making companies
        assert set(blanks) <= {"pe", "fwd_pe", "gross_margin", "op_margin", "profit_margin"}
        assert snap.company_name_raw.endswith("Therapeutics")
        assert (snap.sector_raw, snap.industry_raw) == ("Healthcare", "Biotechnology")
        assert snap.earnings_date_text == "8/13/2026 8:30:00 AM"
        assert snap.earnings_date == date(2026, 8, 13)


@pytest.mark.django_db
def test_values_are_stored_in_the_documented_units(storage: LocalRawStorage) -> None:
    ingest(content=export_csv([company_row("ABC", 3)]), storage=storage)
    snap = FinvizSnapshot.objects.get(company__tickers__ticker="ABC")
    assert (snap.price, snap.mcap_musd, snap.shr_out_m, snap.avg_vol_k) == (5.3, 103.5, 33.0, 203.5)
    assert (snap.perf_ytd, snap.w52h, snap.eps_this, snap.cash_sh) == (-12.5, -29.61, 13.95, 2.57)


@pytest.mark.django_db
def test_unmapped_columns_are_kept_in_extras(storage: LocalRawStorage) -> None:
    result = ingest(
        content=export_csv([company_row("ABC")], extra={"Analyst Recom.": "1.8"}), storage=storage
    )
    assert FinvizSnapshot.objects.get().extras == {"Analyst Recom.#50": "1.8"}
    assert checks(result, "unmapped_columns")[0].status == "warn"


# ------------------------------------------------------------ companies and tickers


@pytest.mark.django_db
def test_a_new_ticker_creates_a_company_and_an_open_ticker(storage: LocalRawStorage) -> None:
    ingest(content=export_csv(universe(2)), storage=storage)
    company = Company.objects.get(tickers__ticker="T001")
    assert (company.name, company.sector, company.industry) == (
        "T001 Therapeutics",
        "Healthcare",
        "Biotechnology",
    )
    assert (company.first_seen_date, company.last_seen_date) == (DAY, DAY)
    ticker = TickerHistory.objects.get(ticker="T001")
    assert (ticker.valid_from, ticker.valid_to) == (DAY, None)


@pytest.mark.django_db
def test_a_known_company_is_matched_by_ticker_and_not_duplicated(storage: LocalRawStorage) -> None:
    ingest(content=export_csv(universe(3)), storage=storage)
    next_day = DAY + timedelta(days=1)
    result = ingest(next_day, export_csv(universe(3, price="6.00")), storage)
    assert result.new_companies == 0
    assert Company.objects.count() == 3
    assert FinvizSnapshot.objects.count() == 6
    assert Company.objects.get(tickers__ticker="T000").last_seen_date == next_day


@pytest.mark.django_db
def test_a_changed_industry_is_recorded(storage: LocalRawStorage) -> None:
    ingest(content=export_csv(universe(3)), storage=storage)
    rows = universe(3)
    rows[1]["industry_raw"] = "Medical Devices"
    result = ingest(DAY + timedelta(days=1), export_csv(rows), storage)
    assert result.industry_changes == [
        {"ticker": "T001", "from": "Biotechnology", "to": "Medical Devices"}
    ]
    check = checks(result, "industry_changes")[0]
    assert check.status == "warn"
    assert check.detail["changes"] == result.industry_changes
    assert Company.objects.get(tickers__ticker="T001").industry == "Medical Devices"
    assert Company.objects.get(tickers__ticker="T000").industry == "Biotechnology"


@pytest.mark.django_db
def test_a_company_absent_today_keeps_its_last_seen_date(storage: LocalRawStorage) -> None:
    ingest(content=export_csv(universe(3)), storage=storage)
    next_day = DAY + timedelta(days=1)
    ingest(next_day, export_csv(universe(2)), storage)
    assert Company.objects.get(tickers__ticker="T002").last_seen_date == DAY
    assert Company.objects.get(tickers__ticker="T001").last_seen_date == next_day
    assert not FinvizSnapshot.objects.filter(
        company__tickers__ticker="T002", as_of_date=next_day
    ).exists()


@pytest.mark.django_db
def test_duplicate_tickers_in_the_file_are_loaded_once_and_reported(
    storage: LocalRawStorage,
) -> None:
    rows = [company_row("AAA", 1), company_row("AAA", 2), company_row("BBB")]
    result = ingest(content=export_csv(rows), storage=storage)
    assert result.rows == 2
    assert Company.objects.count() == 2
    check = checks(result, "duplicate_tickers")[0]
    assert (check.status, check.detail["tickers"]) == ("warn", ["AAA"])


# -------------------------------------------------------------- T10.5 idempotent


@pytest.mark.django_db
def test_the_same_file_twice_creates_no_duplicates(storage: LocalRawStorage) -> None:
    raw = export_csv(universe(8))
    first = ingest(content=raw, storage=storage)
    counts = (
        Company.objects.count(),
        FinvizSnapshot.objects.count(),
        TickerHistory.objects.count(),
    )
    values = list(FinvizSnapshot.objects.order_by("pk").values())
    second = ingest(content=raw, storage=storage)
    assert second.unchanged is True
    assert second.call.status == "skipped"
    assert (
        Company.objects.count(),
        FinvizSnapshot.objects.count(),
        TickerHistory.objects.count(),
    ) == counts
    assert list(FinvizSnapshot.objects.order_by("pk").values()) == values
    assert second.run.status == "ok"
    assert first.call.pk != second.call.pk


@pytest.mark.django_db
def test_a_corrected_file_for_the_same_day_updates_in_place(storage: LocalRawStorage) -> None:
    ingest(content=export_csv(universe(3)), storage=storage)
    result = ingest(content=export_csv(universe(3, price="9.99")), storage=storage)
    assert result.unchanged is False
    assert FinvizSnapshot.objects.count() == 3
    assert set(FinvizSnapshot.objects.values_list("price", flat=True)) == {9.99}
    assert SourceCall.objects.filter(feed="finviz_elite", status="ok").count() == 2


@pytest.mark.django_db
def test_the_same_file_on_another_day_is_loaded_again(storage: LocalRawStorage) -> None:
    raw = export_csv(universe(3))
    ingest(content=raw, storage=storage)
    result = ingest(DAY + timedelta(days=1), raw, storage)
    assert result.unchanged is False
    assert FinvizSnapshot.objects.count() == 6


# ------------------------------------------------------------- blank column checks


@pytest.mark.django_db
def test_a_column_blank_for_every_company_fails_a_check(storage: LocalRawStorage) -> None:
    result = ingest(content=export_csv(universe(10, eps_this="")), storage=storage)
    failed = result.failed_checks
    assert [(c.check_name, c.scope) for c in failed] == [("blank_column", "finviz.eps_this")]
    assert failed[0].observed == "blank for every company"
    assert result.run.status == "partial"
    assert FinvizSnapshot.objects.count() == 10  # the load stands
    assert checks(result, "blank_column")[0].status == "fail"


@pytest.mark.django_db
def test_a_dash_counts_as_blank_and_one_value_is_enough(storage: LocalRawStorage) -> None:
    rows = universe(10, eps_this="-")
    assert (
        ingest(content=export_csv(rows), storage=storage).failed_checks[0].scope
        == "finviz.eps_this"
    )
    rows[4]["eps_this"] = "12.5%"
    result = ingest(DAY + timedelta(days=1), export_csv(rows), storage)
    assert not [c for c in result.failed_checks if c.scope == "finviz.eps_this"]


@pytest.mark.django_db
def test_a_column_missing_from_the_file_also_fails_the_check(storage: LocalRawStorage) -> None:
    result = ingest(content=export_csv(universe(5), drop=("eps_this",)), storage=storage)
    failed = result.failed_checks
    assert [c.scope for c in failed] == ["finviz.eps_this"]
    assert failed[0].observed == "column missing from the export"
    assert FinvizSnapshot.objects.get(company__tickers__ticker="T000").eps_this is None


@pytest.mark.django_db
def test_a_clean_file_passes_with_one_summary_check(storage: LocalRawStorage) -> None:
    result = ingest(content=export_csv(universe(10)), storage=storage)
    blank = checks(result, "blank_column")
    assert [(c.scope, c.status) for c in blank] == [("finviz.*", "pass")]
    assert result.failed_checks == []


# ------------------------------------------------------------ T10.4 row count anomaly


def load_previous_day(count: int, storage: LocalRawStorage) -> None:
    ingest(DAY - timedelta(days=1), export_csv(universe(count)), storage)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("today", "fails"),
    [(100, False), (120, False), (80, False), (121, True), (79, True), (150, True), (30, True)],
)
def test_row_count_change_over_20_percent_fails(
    today: int, fails: bool, storage: LocalRawStorage
) -> None:
    load_previous_day(100, storage)
    result = ingest(content=export_csv(universe(today)), storage=storage)
    check = checks(result, "row_count")[0]
    assert (check.status == "fail") is fails
    assert check.detail["previous"] == 100
    assert check.detail["change_pct"] == pytest.approx(today - 100)


@pytest.mark.django_db
def test_the_first_load_has_nothing_to_compare_with(storage: LocalRawStorage) -> None:
    check = checks(ingest(content=export_csv(universe(5)), storage=storage), "row_count")[0]
    assert (check.status, check.detail) == ("pass", {"first_run": True})


@pytest.mark.django_db
def test_the_threshold_is_a_setting(storage: LocalRawStorage) -> None:
    load_previous_day(100, storage)
    Setting.objects.filter(key="finviz.row_count_change_alert_pct").update(value=5)
    check = checks(ingest(content=export_csv(universe(110)), storage=storage), "row_count")[0]
    assert check.status == "fail"


@pytest.mark.django_db
def test_the_comparison_uses_the_latest_earlier_day(storage: LocalRawStorage) -> None:
    ingest(DAY - timedelta(days=5), export_csv(universe(10)), storage)
    ingest(DAY - timedelta(days=2), export_csv(universe(100)), storage)
    result = ingest(content=export_csv(universe(105)), storage=storage)
    assert checks(result, "row_count")[0].detail["previous"] == 100


# ----------------------------------------------------- universe definition changes


@pytest.mark.django_db
def test_a_changed_view_or_filter_is_recorded(storage: LocalRawStorage) -> None:
    ingest(content=export_csv(universe(5)), storage=storage)
    Setting.objects.filter(key="finviz.view").update(value=111)
    result = ingest(DAY + timedelta(days=1), export_csv(universe(5)), storage)
    check = checks(result, "universe_definition")[0]
    assert check.status == "warn"
    assert check.expected == "v=151|f=sec_healthcare"
    assert check.observed == "v=111|f=sec_healthcare"
    # and the next day, with the new definition unchanged, is quiet again
    again = ingest(DAY + timedelta(days=2), export_csv(universe(5)), storage)
    assert checks(again, "universe_definition")[0].status == "pass"


# --------------------------------------------------------- T10.6 download failure


@pytest.mark.django_db
def test_a_failed_download_stops_the_stage_and_is_recorded(
    storage: LocalRawStorage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeTransport((503, b""), (503, b""), (503, b""))
    with pytest.raises(FinvizIngestFailed) as raised:
        ingest_finviz(DAY, token=TOKEN, transport=transport, sleep=lambda _: None, storage=storage)
    assert len(transport.urls) == 3
    call, run = raised.value.call, raised.value.run
    call.refresh_from_db()
    assert (call.status, call.reason_code, call.attempts, call.http_status) == (
        "failed",
        "http_5xx",
        3,
        503,
    )
    assert run.status == "failed"
    assert FinvizSnapshot.objects.count() == 0
    alert = DataQualityCheck.objects.get(run=run)
    assert (alert.check_name, alert.status, alert.observed) == (
        "finviz_download",
        "fail",
        "http_5xx",
    )
    assert TOKEN not in caplog.text + call.error_text + str(alert.detail) + call.scope_key


@pytest.mark.django_db
def test_a_missing_token_stops_the_stage_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, storage: LocalRawStorage
) -> None:
    monkeypatch.delenv("FINVIZ_AUTH_TOKEN", raising=False)
    with pytest.raises(FinvizIngestFailed, match="FINVIZ_AUTH_TOKEN") as raised:
        ingest_finviz(DAY, transport=FakeTransport(), storage=storage)
    assert raised.value.call.status == "failed"
    assert raised.value.reason_code == "fetch_error"


@pytest.mark.django_db
def test_a_failure_leaves_the_previous_day_untouched(storage: LocalRawStorage) -> None:
    ingest(DAY - timedelta(days=1), export_csv(universe(5)), storage)
    with pytest.raises(FinvizIngestFailed):
        ingest_finviz(DAY, token=TOKEN, transport=FakeTransport((404, b"")), storage=storage)
    assert FinvizSnapshot.objects.count() == 5
    assert SourceCall.objects.filter(status="failed").count() == 1


# -------------------------------------------------------------------- bad files


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"<html>log in</html>", "web page"),
        (b"", "empty"),
        (export_csv([company_row("AAA")], drop=("price",)), "price"),
        (export_csv([]), "no companies"),
    ],
)
def test_an_unusable_file_stops_the_stage_with_a_parse_error(
    content: bytes, message: str, storage: LocalRawStorage
) -> None:
    with pytest.raises(FinvizIngestFailed, match=message) as raised:
        ingest(content=content, storage=storage)
    assert raised.value.reason_code == "parse_error"
    assert raised.value.run.status == "failed"
    assert FinvizSnapshot.objects.count() == 0
    assert DataQualityCheck.objects.get(run=raised.value.run).check_name == "finviz_format"


@pytest.mark.django_db
def test_the_raw_file_is_kept_even_when_it_cannot_be_parsed(storage: LocalRawStorage) -> None:
    junk = b"<html>nope</html>"
    with pytest.raises(FinvizIngestFailed) as raised:
        ingest(content=junk, storage=storage)
    assert storage.get(raised.value.call.storage_uri) == junk


@pytest.mark.django_db
def test_a_load_error_rolls_everything_back(
    monkeypatch: pytest.MonkeyPatch, storage: LocalRawStorage
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database trouble")

    monkeypatch.setattr("apps.marketdata.finviz.ingest.upsert", boom)
    with pytest.raises(RuntimeError, match="database trouble"):
        ingest(content=export_csv(universe(5)), storage=storage)
    assert Company.objects.count() == 0
    assert FinvizSnapshot.objects.count() == 0


# --------------------------------------------------------- the percent duplicate headers


@pytest.mark.django_db
def test_repeated_52_week_headers_are_stored_by_position(storage: LocalRawStorage) -> None:
    from tests.test_finviz_parser import two_52_week_pairs

    result = ingest(content=two_52_week_pairs(percent_first=False), storage=storage)
    snap = FinvizSnapshot.objects.get(company__tickers__ticker="T000")
    assert (snap.w52h, snap.w52l) == (-29.61, 41.2)
    assert snap.extras == {"52-Week High#50": "7.65", "52-Week Low#51": "3.10"} or set(
        snap.extras.values()
    ) == {"7.65", "3.10"}
    check = checks(result, "duplicate_headers")[0]
    assert check.status == "pass"
    assert set(check.detail["chosen"]) == {"w52h", "w52l"}


# --------------------------------------------------------------------- the commands


@pytest.mark.django_db
def test_the_ingest_command_loads_a_saved_file(
    tmp_path: Path, settings: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.RAW_STORAGE_DIR = tmp_path / "raw"
    path = tmp_path / "export.csv"
    path.write_bytes(export_csv(universe(4, eps_this="")))
    call_command("ingest_finviz", "--date", "2026-10-12", "--file", str(path))
    captured = capsys.readouterr()
    assert "Loaded 4 companies for 2026-10-12 (4 new)" in captured.out
    assert "CHECK FAILED: blank_column finviz.eps_this" in captured.err
    call_command("ingest_finviz", "--date", "2026-10-12", "--file", str(path))
    assert "nothing changed" in capsys.readouterr().out
    assert SourceCall.objects.filter(scope_key__endswith="|export.csv").count() == 2


@pytest.mark.django_db
def test_the_ingest_command_reports_problems(tmp_path: Path, settings: Any) -> None:
    settings.RAW_STORAGE_DIR = tmp_path / "raw"
    with pytest.raises(CommandError, match="Not a date"):
        call_command("ingest_finviz", "--date", "soon")
    with pytest.raises(CommandError, match="File not found"):
        call_command("ingest_finviz", "--file", str(tmp_path / "nope.csv"))
    bad = tmp_path / "bad.csv"
    bad.write_bytes(b"<html></html>")
    with pytest.raises(CommandError, match="parse_error"):
        call_command("ingest_finviz", "--date", "2026-10-12", "--file", str(bad))
    with pytest.raises(CommandError, match="FINVIZ_AUTH_TOKEN"):
        call_command("ingest_finviz", "--date", "2026-10-12")


@pytest.mark.django_db
def test_the_contract_check_needs_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINVIZ_AUTH_TOKEN", raising=False)
    with pytest.raises(CommandError, match="FINVIZ_AUTH_TOKEN"):
        call_command("finviz_contract_check")


@pytest.mark.django_db
def test_the_contract_check_reports_the_match_and_saves_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = export_csv(universe(5, eps_this=""), drop=("cash_sh",), extra={"Mystery": "1"})
    monkeypatch.setenv("FINVIZ_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(
        "apps.marketdata.management.commands.finviz_contract_check.download",
        lambda request: __import__("apps.marketdata.finviz.client", fromlist=["x"]).Download(
            raw, 200, 1, request.public_url()
        ),
    )
    target = tmp_path / "private" / "raw.csv"
    call_command("finviz_contract_check", "--save", str(target))
    out = capsys.readouterr().out
    assert target.read_bytes() == raw
    assert "5 companies, 50 columns" in out
    assert "NOT FOUND in the export: cash_sh" in out
    assert "Not in the column map: Mystery" in out
    assert TOKEN not in out
    assert FinvizSnapshot.objects.count() == 0  # the check writes nothing
    assert PipelineRun.objects.count() == 0
