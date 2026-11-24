"""T12.1 to T12.9: the runway calculator, its storage, and the arithmetic on the runway export."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.companies.models import Company
from apps.platform.models import PipelineRun
from apps.sec.models import RunwayCalc, SecFact
from apps.sec.runway import (
    RUNWAY_CAP_MONTHS,
    Fact,
    calculate_runway,
    runway_display,
    runway_for,
    runway_rating,
    trial_burn,
)
from apps.sec.runway_service import calculate_universe, phase_key
from tests import factories as f

TODAY = date(2026, 10, 12)
BS = date(2026, 6, 30)
M = 1_000_000
EXPORT = Path(__file__).parent / "fixtures/private/edgar_runway_export_2026-10-14.csv"

_id = iter(range(1, 10_000))


def fact(tag: str, value: float, end: date = BS, start: date | None = None, form: str = "10-K",
         taxonomy: str = "us-gaap", filed: date = date(2026, 8, 1)) -> Fact:  # fmt: skip
    return Fact(next(_id), taxonomy, tag, start, end, Decimal(str(value)), form, filed)


CASH = "CashAndCashEquivalentsAtCarryingValue"
STI = "ShortTermInvestments"
OCF = "NetCashProvidedByUsedInOperatingActivities"
RD = "ResearchAndDevelopmentExpense"


def annual_ocf(value: float, end: date = BS) -> Fact:
    return fact(OCF, value, end=end, start=end - timedelta(days=364))


def quarter(value: float, start: date, end: date) -> Fact:
    return fact(OCF, value, end=end, start=start, form="10-Q")


# --- T12.1 basic runway and caps ------------------------------------------------------------


@pytest.mark.parametrize(
    ("burn", "runway", "capped", "reason"),
    [(0, 360, True, "zero_burn"), (0.2, 360, True, "tiny_burn"), (5, 20, False, "")],
)
def test_cap_at_360_months(burn: float, runway: float, capped: bool, reason: str) -> None:
    result = calculate_runway([fact(CASH, 100 * M), annual_ocf(-burn * 12 * M)])
    assert result.runway_months == runway
    assert result.runway_capped is capped
    assert result.cap_reason == reason


def test_operating_burn_from_the_annual_figure() -> None:
    result = calculate_runway([fact(CASH, 100 * M), annual_ocf(-60 * M)])
    assert result.operating_burn_monthly_usd == 5 * M
    assert result.burn_source == "operating_cash_flow"
    assert result.runway_months == 20


def test_positive_operating_cash_flow() -> None:
    result = calculate_runway([fact(CASH, 100 * M), annual_ocf(30 * M)])
    assert result.operating_burn_monthly_usd == 0
    assert "company_cash_positive" in result.reason_codes
    assert "company_cash_positive" in result.notes
    assert result.runway_months == RUNWAY_CAP_MONTHS
    assert result.cap_reason == "zero_burn"


# --- T12.2 and T12.3: trial burn -------------------------------------------------------------


def test_trial_burn_lines() -> None:
    total, lines = trial_burn({"phase1": 2, "phase2": 3, "phase3": 1})
    assert total == 2 * 150_000 + 3 * 800_000 + 2_500_000
    assert [(line.phase, line.active_trial_count, line.amount_usd) for line in lines] == [
        ("phase1", 2, Decimal(300_000)),
        ("phase2", 3, Decimal(2_400_000)),
        ("phase3", 1, Decimal(2_500_000)),
    ]


def test_total_burn_is_operating_plus_trial() -> None:
    result = calculate_runway(
        [fact(CASH, 100 * M), annual_ocf(-60 * M)], {"phase2": 1}, rates={"phase2": 1_000_000}
    )
    assert result.burn_total_monthly_usd == 6 * M
    assert result.trial_burn_monthly_usd == 1 * M
    assert result.trial_burn_lines[0]["amount_usd"] == "1000000"


def test_rates_come_from_the_caller() -> None:
    _, lines = trial_burn({"other": 2}, {"other": 5})
    assert lines[0].amount_usd == 10


def test_krys_example() -> None:
    # 11 trials worth 6.45 million a month, zero operating burn, 845.6 million cash.
    result = calculate_runway(
        [fact(CASH, 845.6 * M), annual_ocf(10 * M)],
        {"phase3": 2, "phase2": 1, "phase1": 8},
        rates={"phase3": 2_000_000, "phase2": 600_000, "phase1": 231_250},
    )
    assert result.burn_total_monthly_usd == Decimal(6_450_000)
    assert result.runway_months == pytest.approx(131.1, abs=0.05)
    assert not result.runway_capped


# --- T12.4: fallbacks and reason codes -------------------------------------------------------


def test_rd_fallback() -> None:
    result = calculate_runway(
        [fact(CASH, 100 * M), fact(RD, 24 * M, start=BS - timedelta(days=364))]
    )
    assert result.burn_source == "rd_expense"
    assert result.operating_burn_monthly_usd == 2 * M
    assert {"fallback_rd_expense", "operating_cash_flow_missing"} <= set(result.reason_codes)
    assert result.runway_months == 50


def test_foreign_filer_without_usable_data() -> None:
    result = calculate_runway([], {"phase2": 2}, reason_without_facts="non_usd_only")
    assert result.cash_usd is None
    assert result.runway_months is None
    assert result.trial_burn_monthly_usd == 1_600_000
    assert "phase_burn_only_no_xbrl" in result.reason_codes
    assert "non_usd_only" in result.reason_codes
    assert result.status == "partial"


def test_no_facts_and_no_trials_is_unavailable_with_reasons() -> None:
    result = calculate_runway([], reason_without_facts="no_cik")
    assert result.status == "unavailable"
    assert result.runway_months is None
    assert {"cash_missing", "operating_cash_flow_missing", "no_cik"} <= set(result.reason_codes)


def test_cash_without_any_burn_has_no_runway() -> None:
    result = calculate_runway([fact(CASH, 100 * M)])
    assert result.runway_months is None
    assert "operating_cash_flow_missing" in result.reason_codes
    assert result.status == "partial"


def test_missing_cash_has_no_runway() -> None:
    result = calculate_runway([annual_ocf(-60 * M)])
    assert result.runway_months is None
    assert "cash_missing" in result.reason_codes
    assert result.operating_burn_monthly_usd == 5 * M


def test_ifrs_used_only_when_us_gaap_has_nothing() -> None:
    result = calculate_runway(
        [
            fact("CashAndCashEquivalents", 50 * M, taxonomy="ifrs-full"),
            fact(
                "CashFlowsFromUsedInOperatingActivities",
                -12 * M,
                start=BS - timedelta(days=364),
                taxonomy="ifrs-full",
                form="20-F",
            ),
        ]
    )
    assert result.runway_months == 50


def test_every_null_has_a_reason() -> None:
    for facts in ([], [fact(CASH, 1 * M)], [annual_ocf(-1 * M)]):
        result = calculate_runway(facts)
        assert result.runway_months is None
        assert result.reason_codes


def test_trial_burn_on_profitable_switch() -> None:
    facts = [fact(CASH, 100 * M), annual_ocf(30 * M)]
    on = calculate_runway(facts, {"phase3": 1}, trial_burn_on_profitable=True)
    off = calculate_runway(facts, {"phase3": 1}, trial_burn_on_profitable=False)
    assert on.burn_total_monthly_usd == 2_500_000
    assert off.burn_total_monthly_usd == 0
    assert off.runway_months == RUNWAY_CAP_MONTHS


# --- Cash and short-term investments ---------------------------------------------------------


def test_liquidity_adds_investments_with_the_same_date() -> None:
    result = calculate_runway([fact(CASH, 60 * M), fact(STI, 40 * M), annual_ocf(-60 * M)])
    assert result.cash_usd == 100 * M
    assert result.cash_tag == f"{CASH}+{STI}"
    assert result.runway_months == 20


def test_liquidity_is_cash_only_when_dates_differ() -> None:
    older = date(2026, 3, 31)
    result = calculate_runway(
        [fact(CASH, 60 * M), fact(STI, 40 * M, end=older), annual_ocf(-60 * M)]
    )
    assert result.cash_usd == 60 * M
    assert result.cash_tag == CASH


def test_newest_cash_date_wins() -> None:
    result = calculate_runway(
        [fact(CASH, 10 * M, end=date(2026, 3, 31)), fact(CASH, 60 * M), annual_ocf(-60 * M)]
    )
    assert result.cash_usd == 60 * M
    assert result.cash_end_date == BS


# --- T12.5: year-to-date cash flow -----------------------------------------------------------


def cumulative() -> list[Fact]:
    # Year-to-date outflows of 10, 25 and 45 million over 3, 6 and 9 months (no annual filing).
    start = date(2026, 1, 1)
    return [
        fact(CASH, 100 * M),
        quarter(-10 * M, start, date(2026, 3, 31)),
        quarter(-25 * M, start, date(2026, 6, 30)),
        quarter(-45 * M, start, date(2026, 9, 30)),
    ]


def test_ytd_switch_on_uses_the_old_naive_sum() -> None:
    result = calculate_runway(cumulative(), ocf_ytd_naive_sum=True)
    assert result.ocf_usd == -80 * M  # 10 + 25 + 45, the cumulative figures counted twice
    assert result.operating_burn_monthly_usd == (Decimal(80 * M) / 9).quantize(Decimal("0.01"))


def test_ytd_switch_off_decumulates() -> None:
    result = calculate_runway(cumulative(), ocf_ytd_naive_sum=False)
    assert result.ocf_usd == -45 * M
    assert result.operating_burn_monthly_usd == 5 * M  # 45 million over 9 months
    assert result.ocf_period_start == date(2026, 1, 1)
    assert result.ocf_period_end == date(2026, 9, 30)


def test_annual_figure_beats_quarterly() -> None:
    facts = [*cumulative(), annual_ocf(-60 * M, end=date(2025, 12, 31))]
    result = calculate_runway(facts, ocf_ytd_naive_sum=True)
    assert result.operating_burn_monthly_usd == 5 * M
    assert result.ocf_period_end == date(2025, 12, 31)


# --- Financing date --------------------------------------------------------------------------


def test_financing_date() -> None:
    result = calculate_runway([fact(CASH, 100 * M), annual_ocf(-60 * M)])
    assert result.financing_date == BS + timedelta(days=round(20 * 30.44))


# --- T12.8 and T12.9: display ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("months", "quarters", "rating"),
    [(5.0, 1.7, "Critical"), (11.9, 4.0, "Short"), (20.0, 6.7, "Moderate"), (131.1, 43.7, "Long")],
)
def test_derived_display_values(months: float, quarters: float, rating: str) -> None:
    shown = runway_display(months)
    assert (shown.quarters, shown.rating) == (quarters, rating)


@pytest.mark.parametrize(
    ("months", "rating"),
    [(5.99, "Critical"), (6, "Short"), (11.99, "Short"), (12, "Moderate"),
     (23.99, "Moderate"), (24, "Long")],
)  # fmt: skip
def test_rating_boundaries(months: float, rating: str) -> None:
    assert runway_rating(months) == rating


def test_cash_flow_positive_displays_blank_with_label() -> None:
    shown = runway_display(360, capped=True, cash_flow_positive=True)
    assert shown.months is None and shown.quarters is None
    assert shown.label == "cash-flow positive"


def test_capped_runway_label() -> None:
    assert runway_display(360, capped=True).months_label == "360+"


# --- Storage ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("highest", "key"),
    [("PHASE1", "phase1"), ("PHASE2", "phase2"), ("PHASE3", "phase3"), ("PHASE1/PHASE2", "phase2"),
     ("EARLY_PHASE1", "phase1"), ("PHASE4", "other"), ("", "other"), ("NA", "other")],
)  # fmt: skip
def test_phase_key(highest: str, key: str) -> None:
    assert phase_key(highest) == key


@pytest.mark.django_db
def test_universe_stores_every_input_and_is_repeatable() -> None:
    company = f.company()
    f.ticker(company, ticker="KTTA")
    cash = f.sec_fact(company, value=Decimal(100 * M), period_end=BS)
    ocf = f.sec_fact(
        company,
        tag=OCF,
        value=Decimal(-60 * M),
        period_start=BS - timedelta(days=364),
        period_end=BS,
        form="10-K",
    )
    f.ct_study(company, status="RECRUITING", highest_phase="PHASE3")
    f.ct_study(company, status="COMPLETED", highest_phase="PHASE3")  # not active
    f.ct_study(company, status="RECRUITING", highest_phase="PHASE1/PHASE2")

    first = calculate_universe(TODAY)
    again = calculate_universe(TODAY)

    assert first.companies == 1 and RunwayCalc.objects.count() == 1
    assert again.run.pk != first.run.pk
    row = RunwayCalc.objects.get()
    assert row.cash_usd == 100 * M and row.cash_tag == CASH and row.cash_end_date == BS
    assert row.ocf_usd == -60 * M and row.ocf_tag == OCF and row.ocf_period_end == BS
    assert row.operating_burn_monthly_usd == 5 * M
    assert row.trial_burn_monthly_usd == 2_500_000 + 800_000
    assert row.burn_total_monthly_usd == 5 * M + 3_300_000
    assert {line["phase"] for line in row.trial_burn_lines} == {"phase2", "phase3"}
    assert row.source_fact_ids == sorted([cash.pk, ocf.pk])
    assert row.method_version == "runway-v1"
    assert row.runway_months == pytest.approx(100 / 8.3)
    assert first.run.stage == PipelineRun.Stage.FILINGS


@pytest.mark.django_db
def test_facts_filed_after_the_day_are_not_used() -> None:
    company = f.company()
    f.ticker(company)
    f.sec_fact(company, value=Decimal(100 * M), filed_at=TODAY + timedelta(days=1))
    calculate_universe(TODAY)
    assert RunwayCalc.objects.get().cash_usd is None


@pytest.mark.django_db
def test_company_without_cik_gets_no_cik_reason() -> None:
    company = f.company(cik=None)
    f.ticker(company)
    calculate_universe(TODAY)
    assert "no_cik" in RunwayCalc.objects.get().reason_codes


@pytest.mark.django_db
def test_ticker_filter_and_inactive_companies() -> None:
    kept, other = f.company(), f.company()
    f.ticker(kept, ticker="AAA")
    f.ticker(other, ticker="BBB")
    gone = f.company(status="delisted")
    f.ticker(gone, ticker="CCC")
    assert calculate_universe(TODAY, tickers=["aaa"]).companies == 1
    assert calculate_universe(TODAY).companies == 2


@pytest.mark.django_db
def test_switches_are_read_from_the_stored_formula() -> None:
    company = f.company()
    f.ticker(company)
    cumulative_db(company)
    f.formula_version(name="parity", version=2, quirks={"ocf_ytd_naive_sum": False})
    calculate_universe(TODAY)
    assert RunwayCalc.objects.get().ocf_usd == -45 * M


def cumulative_db(company: Company) -> list[SecFact]:
    start = date(2026, 1, 1)
    out: list[SecFact] = [f.sec_fact(company, value=Decimal(100 * M), period_end=date(2026, 9, 30))]
    for value, end in (
        (-10, date(2026, 3, 31)),
        (-25, date(2026, 6, 30)),
        (-45, date(2026, 9, 30)),
    ):
        out.append(
            f.sec_fact(
                company,
                tag=OCF,
                value=Decimal(value * M),
                period_start=start,
                period_end=end,
                form="10-Q",
                accession=f"0000000000-26-0001{-value}",
            )  # fmt: skip
        )
    return out


# --- T12.6: arithmetic on the export ---------------------------------------------------------


@pytest.mark.skipif(not EXPORT.exists(), reason="the runway export is not in the private folder")
def test_arithmetic_on_the_runway_export() -> None:
    checked = 0
    for row in csv.DictReader(EXPORT.open()):
        if not row["cash_on_hand_usd"] or not row["monthly_burn_usd"]:
            continue
        cash, burn = Decimal(row["cash_on_hand_usd"]), Decimal(row["monthly_burn_usd"])
        months, _, _ = runway_for(cash, burn)
        assert months == pytest.approx(float(row["runway_months_reported"]), rel=1e-6), row[
            "ticker"
        ]
        checked += 1
    assert checked == 1040
