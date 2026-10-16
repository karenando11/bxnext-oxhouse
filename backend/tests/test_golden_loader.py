"""T6.1 to T6.6 (integration): loading a golden day as fixture data.

The synthetic tests use small files written on the fly. The ``golden`` tests use the private
exports and are skipped when those are absent.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
from django.test import Client

from apps.companies.models import Company, TickerHistory
from apps.marketdata.models import FinvizSnapshot
from apps.platform.golden import (
    NUMERIC_COLUMNS,
    earnings_date,
    integer,
    latest_run_is_fixture,
    load_golden,
    number,
)
from apps.platform.models import PipelineRun, SourceCall
from apps.sec.models import RunwayCalc
from apps.trials.models import CatalystSignal
from apps.web.models import CompanyPage
from tests import factories as f

DAY = date(2026, 10, 12)
GOLDEN_COLUMNS = [
    "ticker", "name", "indication", "price", "mcap", "shr_out", "shr_float", "cash_sh", "pcash",
    "quick_ratio", "cur_ratio", "td_eq", "eps_next", "eps_this", "rel_vol", "rsi", "beta", "w52h",
    "w52l", "sma200", "vol_m", "perf_w", "perf_m", "perf_q", "perf_ytd", "avg_vol", "inst_own",
    "short_float", "earnings_date", "runway_months", "runway_as_of", "runway_capped_derived",
    "ct_total_studies", "ct_active_phase_2", "ct_active_phase_3", "ct_latest_trial_nct",
    "ct_latest_trial_status", "ct_latest_trial_completion", "ct_next_trial_completion",
    "catalyst_phase", "catalyst_source", "catalyst_clinical_date", "catalyst_clinical_overdue",
    "snapshot_at",
]  # fmt: skip
RUNWAY_COLUMNS = ["ticker", "cik", "cash_on_hand_usd", "monthly_burn_usd", "burn_source", "notes"]


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def golden_row(ticker: str, **overrides: str) -> dict[str, str]:
    row = dict.fromkeys(GOLDEN_COLUMNS, "")
    row.update(
        ticker=ticker,
        name=f"{ticker} Therapeutics",
        indication="Biotechnology",
        price="5.39",
        mcap="307.94",
        avg_vol="842.3",
        perf_ytd="-12.5",
        earnings_date="8/13/2026 8:30:00 AM",
        runway_months="20.5",
        runway_as_of="2026-10-08",
        runway_capped_derived="False",
        ct_total_studies="7",
        ct_active_phase_2="2",
        ct_active_phase_3="1",
        ct_latest_trial_nct="NCT00000042",
        ct_latest_trial_status="RECRUITING",
        ct_next_trial_completion="2027-09",
        catalyst_phase="phase_3",
        catalyst_source="clinical",
        catalyst_clinical_date="2027-04-15",
        catalyst_clinical_overdue="False",
        snapshot_at="2026-10-12 16:30:00.031-04",
    )
    row.update(overrides)
    return row


def blank_row(ticker: str) -> dict[str, str]:
    """A company with a dash for the price and nothing else, as Finviz delivers a thin listing."""
    row = dict.fromkeys(GOLDEN_COLUMNS, "")
    row.update(
        ticker=ticker,
        name=f"{ticker} Therapeutics",
        indication="Biotechnology",
        price="-",
        snapshot_at="2026-10-12 16:30:00.031-04",
    )
    return row


@pytest.fixture
def golden_file(tmp_path: Path) -> Path:
    rows = [
        golden_row("AAA"),
        blank_row("BBB"),
        golden_row("CCC", eps_this="12.5%", cur_ratio="0", runway_months="3.06"),
    ]  # fmt: skip
    return write_csv(tmp_path / "golden.csv", GOLDEN_COLUMNS, rows)


@pytest.fixture
def runway_file(tmp_path: Path) -> Path:
    rows = [
        {"ticker": "AAA", "cik": "0000001234", "cash_on_hand_usd": "100000000",
         "monthly_burn_usd": "5000000.126", "burn_source": "operating_cash_flow",
         "notes": "company_cash_positive;no_active_trials;fallback_to_rd_expense"},
        {"ticker": "CCC", "cik": "0000005678", "cash_on_hand_usd": "",
         "monthly_burn_usd": "", "burn_source": "something_new", "notes": "cash_on_hand_missing"},
        {"ticker": "ZZZ", "cik": "0000009999", "cash_on_hand_usd": "1",
         "monthly_burn_usd": "1", "burn_source": "", "notes": ""},
    ]  # fmt: skip
    return write_csv(tmp_path / "runway.csv", RUNWAY_COLUMNS, rows)


# -------------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("raw", "value"),
    [("12.4", 12.4), ("12.4%", 12.4), ("-", None), ("", None), ("  ", None), (None, None),
     ("1,234.5", 1234.5), ("nan", None), ("inf", None), ("abc", None), ("0", 0.0), ("-0.5", -0.5)],
)  # fmt: skip
def test_number_parsing_never_turns_a_blank_into_zero(raw: str | None, value: float | None) -> None:
    assert number(raw) == value


def test_integer_parsing() -> None:
    assert (integer("7"), integer("7.0"), integer(""), integer("-3")) == (7, 7, None, None)


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [("8/13/2026 8:30:00 AM", date(2026, 8, 13)), ("09/02/2026", date(2026, 9, 2)), ("", None),
     ("TBD", None), ("13/45/2026", None)],
)  # fmt: skip
def test_earnings_date_parsing(raw: str, parsed: date | None) -> None:
    assert earnings_date(raw) == parsed


# ------------------------------------------------------------------------- T6.1 and T6.2


@pytest.mark.django_db
def test_load_creates_companies_tickers_snapshots_runway_and_catalysts(golden_file: Path) -> None:
    result = load_golden(golden_file)
    assert (result.companies, result.snapshots) == (3, 3)
    assert (result.runway_calcs, result.catalyst_signals) == (2, 2)  # BBB has no values
    assert Company.objects.count() == 3
    open_tickers = TickerHistory.objects.filter(valid_to__isnull=True)
    assert set(open_tickers.values_list("ticker", flat=True)) == {"AAA", "BBB", "CCC"}
    assert FinvizSnapshot.objects.filter(as_of_date=DAY).count() == 3
    assert RunwayCalc.objects.count() == 2
    assert CatalystSignal.objects.count() == 2


@pytest.mark.django_db
def test_loaded_values_equal_the_file_with_units_applied(golden_file: Path) -> None:
    load_golden(golden_file)
    company = Company.objects.get(tickers__ticker="AAA")
    snap = FinvizSnapshot.objects.get(company=company)
    assert (company.name, company.industry, company.sector) == (
        "AAA Therapeutics",
        "Biotechnology",
        "Healthcare",
    )
    assert (snap.price, snap.mcap_musd, snap.avg_vol_k, snap.perf_ytd) == (
        5.39,
        307.94,
        842.3,
        -12.5,
    )
    assert snap.earnings_date_text == "8/13/2026 8:30:00 AM"
    assert snap.earnings_date == date(2026, 8, 13)
    assert (snap.company_name_raw, snap.industry_raw) == ("AAA Therapeutics", "Biotechnology")
    runway = RunwayCalc.objects.get(company=company)
    assert (runway.calc_date, runway.runway_months, runway.runway_capped) == (
        date(2026, 10, 8),
        20.5,
        False,
    )
    signal = CatalystSignal.objects.get(company=company)
    assert signal.clinical_date == date(2027, 4, 15)
    assert (signal.clinical_phase, signal.next_event_source) == ("phase_3", "clinical")
    assert (signal.active_phase_2, signal.active_phase_3, signal.total_studies) == (2, 1, 7)
    assert signal.fallback_end_raw == "2027-09"
    assert signal.latest_trial_status == "RECRUITING"


@pytest.mark.django_db
def test_percent_suffix_is_stripped_and_zero_is_kept(golden_file: Path) -> None:
    load_golden(golden_file)
    snap = FinvizSnapshot.objects.get(company__tickers__ticker="CCC")
    assert snap.eps_this == 12.5
    assert snap.cur_ratio == 0.0  # a real zero stays zero


# ------------------------------------------------------------------------------ T6.4 blanks


@pytest.mark.django_db
def test_blank_and_dash_cells_are_null_never_zero(golden_file: Path) -> None:
    load_golden(golden_file)
    snap = FinvizSnapshot.objects.get(company__tickers__ticker="BBB")
    assert snap.price is None  # "-"
    assert snap.mcap_musd is None  # blank
    assert snap.earnings_date_text == ""
    assert snap.earnings_date is None
    assert not RunwayCalc.objects.filter(company__tickers__ticker="BBB").exists()
    assert not CatalystSignal.objects.filter(company__tickers__ticker="BBB").exists()
    nulls = [name for name in NUMERIC_COLUMNS.values() if getattr(snap, name) is None]
    assert len(nulls) == len(NUMERIC_COLUMNS)  # nothing was turned into zero


# --------------------------------------------------------------------------- T6.3 idempotent


MODELS = (
    Company,
    TickerHistory,
    FinvizSnapshot,
    RunwayCalc,
    CatalystSignal,
    SourceCall,
    PipelineRun,
)


def _counts() -> dict[str, int]:
    return {
        model.__name__: model.objects.count()
        for model in MODELS
    }  # fmt: skip


@pytest.mark.django_db
def test_loading_twice_changes_nothing(golden_file: Path, runway_file: Path) -> None:
    load_golden(golden_file, runway_file)
    before = _counts()
    values = list(FinvizSnapshot.objects.order_by("pk").values())
    load_golden(golden_file, runway_file)
    assert _counts() == before
    assert list(FinvizSnapshot.objects.order_by("pk").values()) == values


@pytest.mark.django_db
def test_a_changed_file_updates_in_place(golden_file: Path, tmp_path: Path) -> None:
    load_golden(golden_file)
    rows = list(csv.DictReader(golden_file.open()))
    rows[0]["price"] = "6.00"
    changed = write_csv(tmp_path / "changed.csv", GOLDEN_COLUMNS, rows)
    load_golden(changed)
    assert FinvizSnapshot.objects.count() == 3
    assert FinvizSnapshot.objects.get(company__tickers__ticker="AAA").price == 6.0


# ---------------------------------------------------------------- T6.5 runway export overlay


@pytest.mark.django_db
def test_runway_overlay_adds_cash_burn_source_and_notes(
    golden_file: Path, runway_file: Path
) -> None:
    result = load_golden(golden_file, runway_file)
    assert (result.overlay_matched, result.overlay_with_cash_and_burn) == (2, 1)
    assert result.overlay_unmatched_tickers == ["ZZZ"]
    aaa = RunwayCalc.objects.get(company__tickers__ticker="AAA")
    assert float(aaa.cash_usd or 0) == 100000000.0
    assert float(aaa.burn_total_monthly_usd or 0) == 5000000.13
    assert aaa.burn_source == "operating_cash_flow"
    assert aaa.notes == "company_cash_positive;no_active_trials;fallback_to_rd_expense"
    assert aaa.reason_codes == ["company_cash_positive", "fallback_rd_expense"]
    assert aaa.company.cik == "0000001234"


@pytest.mark.django_db
def test_overlay_with_missing_cash_stores_null_and_an_unknown_burn_source_as_blank(
    golden_file: Path, runway_file: Path
) -> None:
    load_golden(golden_file, runway_file)
    ccc = RunwayCalc.objects.get(company__tickers__ticker="CCC")
    assert ccc.cash_usd is None
    assert ccc.burn_total_monthly_usd is None
    assert ccc.burn_source == ""
    assert ccc.reason_codes == ["cash_missing"]


@pytest.mark.django_db
def test_overlay_does_not_invent_runway_rows(golden_file: Path, runway_file: Path) -> None:
    load_golden(golden_file, runway_file)
    assert RunwayCalc.objects.count() == 2


# ------------------------------------------------------ fixture marking and the banner


@pytest.mark.django_db
def test_every_loaded_row_traces_to_a_fixture_source_call(
    golden_file: Path, runway_file: Path
) -> None:
    result = load_golden(golden_file, runway_file)
    calls = SourceCall.objects.filter(feed="fixture")
    assert {c.scope_key for c in calls} == {"golden.csv", "runway.csv"}
    assert all(c.run_id == result.run_id and c.content_hash and c.status == "ok" for c in calls)
    assert result.run_id is not None
    assert PipelineRun.objects.get(pk=result.run_id).code_ref == "fixture"
    golden_call = calls.get(scope_key="golden.csv")
    assert FinvizSnapshot.objects.exclude(source_call=golden_call).count() == 0


@pytest.mark.django_db
def test_the_latest_run_is_flagged_as_fixture(golden_file: Path) -> None:
    assert latest_run_is_fixture() is None
    load_golden(golden_file)
    assert latest_run_is_fixture() is True
    f.pipeline_run(as_of_date=date(2026, 10, 13))  # a later, real run
    assert latest_run_is_fixture() is False


@pytest.mark.django_db
def test_healthz_reports_the_data_source(golden_file: Path) -> None:
    client = Client()
    assert client.get("/healthz").json()["data_source"] == "none"
    load_golden(golden_file)
    assert client.get("/healthz").json()["data_source"] == "fixture"
    f.pipeline_run(as_of_date=date(2026, 10, 13))
    assert client.get("/healthz").json()["data_source"] == "live"


@pytest.mark.django_db
def test_company_page_marks_fixture_data_and_explains_absent_fields(golden_file: Path) -> None:
    result = load_golden(golden_file)
    assert result.run_id is not None
    run = f.score_run(pipeline_run=PipelineRun.objects.get(pk=result.run_id))
    company = Company.objects.get(tickers__ticker="AAA")
    f.score_result(run, company)
    page = CompanyPage.objects.get(pk=company.pk)
    assert page.is_fixture is True
    assert page.field_meta["short_volume"]["reasons"] == ["fixture_not_available"]
    assert page.field_meta["insider"]["reasons"] == ["fixture_not_available"]
    assert page.field_meta["trial_lists"]["reasons"] == ["fixture_not_available"]
    assert page.short_volume_latest_ratio is None
    assert page.insider_filings_count is None


@pytest.mark.django_db
def test_company_page_for_a_live_run_is_not_marked_fixture() -> None:
    company = f.company()
    f.score_result(f.score_run(), company)
    page = CompanyPage.objects.get(pk=company.pk)
    assert page.is_fixture is False
    assert page.field_meta["trial_lists"]["reasons"] == ["no_trials"]
    assert page.field_meta["short_volume"]["reasons"] == ["not_in_finra_file"]


# ---------------------------------------------------------------------------- golden data


def _golden(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.golden
@pytest.mark.django_db
def test_the_golden_day_loads_1053_companies_and_snapshots(golden_path: Path) -> None:
    result = load_golden(golden_path)
    assert (result.companies, result.snapshots) == (1053, 1053)
    assert Company.objects.count() == 1053
    assert FinvizSnapshot.objects.filter(as_of_date=DAY).count() == 1053
    assert TickerHistory.objects.filter(valid_to__isnull=True).count() == 1053


@pytest.mark.golden
@pytest.mark.django_db
def test_every_mapped_column_reads_back_equal_to_the_file(golden_path: Path) -> None:
    load_golden(golden_path)
    snapshots = {
        s.company.tickers.get().ticker: s
        for s in FinvizSnapshot.objects.select_related("company").prefetch_related(
            "company__tickers"
        )
    }
    mismatches: list[tuple[str, str]] = []
    for row in _golden(golden_path):
        snap = snapshots[row["ticker"]]
        for column, name in NUMERIC_COLUMNS.items():
            if getattr(snap, name) != number(row[column]):
                mismatches.append((row["ticker"], column))
        if snap.earnings_date_text != row["earnings_date"].strip():
            mismatches.append((row["ticker"], "earnings_date"))
        if (
            snap.company_name_raw != row["name"].strip()
            or snap.industry_raw != row["indication"].strip()
        ):
            mismatches.append((row["ticker"], "name or industry"))
    assert not mismatches, mismatches[:10]


@pytest.mark.golden
@pytest.mark.django_db
def test_blank_golden_columns_stay_null(golden_path: Path) -> None:
    load_golden(golden_path)
    for name in ("eps_this", "cur_ratio", "w52l"):
        assert FinvizSnapshot.objects.filter(**{f"{name}__isnull": False}).count() == 0


@pytest.mark.golden
@pytest.mark.django_db
def test_the_golden_runway_and_catalyst_counts(golden_path: Path) -> None:
    result = load_golden(golden_path)
    rows = _golden(golden_path)
    assert result.runway_calcs == sum(1 for r in rows if r["runway_months"])
    assert RunwayCalc.objects.count() == result.runway_calcs
    assert CatalystSignal.objects.count() == result.catalyst_signals
    assert result.catalyst_signals >= 1050


@pytest.mark.golden
@pytest.mark.django_db
def test_reloading_the_golden_day_changes_nothing(golden_path: Path, runway_path: Path) -> None:
    load_golden(golden_path, runway_path)
    before = _counts()
    load_golden(golden_path, runway_path)
    assert _counts() == before


@pytest.mark.golden
@pytest.mark.django_db
def test_the_runway_overlay_on_the_golden_day(golden_path: Path, runway_path: Path) -> None:
    result = load_golden(golden_path, runway_path)
    with runway_path.open(newline="", encoding="utf-8") as handle:
        export = list(csv.DictReader(handle))
    universe = {r["ticker"] for r in _golden(golden_path)}
    expected = sum(
        1
        for r in export
        if r["ticker"] in universe and r["cash_on_hand_usd"] and r["monthly_burn_usd"]
    )
    assert result.overlay_with_cash_and_burn == expected
    assert (
        RunwayCalc.objects.filter(
            cash_usd__isnull=False, burn_total_monthly_usd__isnull=False
        ).count()
        == expected
    )
    assert expected >= 990
    assert len(result.overlay_unmatched_tickers) == sum(
        1 for r in export if r["ticker"] not in universe
    )
