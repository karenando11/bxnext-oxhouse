"""T18.1 to T18.7: the company-page API (blocks, formats, banners, the 55 fields, one read)."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.platform.calendar import trading_days_between
from apps.scoring.service import run_scoring
from apps.trials.display import Study
from apps.web import format as fmt
from apps.web.company_page import FIELDS, build_page
from apps.web.models import CompanyPage
from tests import factories as f

DAY = date(2026, 10, 12)  # a Monday: the score run's date and "today" unless a test moves it


# --- T18.2 formats ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("func", "value", "shown"),
    [
        (fmt.usd, 5.39, "$5.39"),
        (fmt.usd, 0.485, "$0.49"),
        (fmt.usd, 1234.5, "$1,234.50"),
        (fmt.market_cap, 307.94, "$308M"),
        (fmt.market_cap, 17.65, "$18M"),
        (fmt.market_cap, 1234.0, "$1.2B"),
        (fmt.market_cap, 999.5, "$1,000M"),
        (fmt.signed_percent, -29.61, "-29.61%"),
        (fmt.signed_percent, 5.0, "+5.00%"),
        (fmt.signed_percent, 0.0, "0.00%"),
        (fmt.percent, 29.92, "29.92%"),
        (fmt.percent, 69.11, "69.11%"),
        (fmt.volume_k, 842.3, "842K"),
        (fmt.volume_k, 206.5, "207K"),
        (fmt.usd, None, None),
        (fmt.market_cap, None, None),
        (fmt.signed_percent, None, None),
    ],
)
def test_display_formats(func: Any, value: float | None, shown: str | None) -> None:
    assert func(value) == shown


# --- the page, built from one read-model row -------------------------------------------------


def make_page(**overrides: Any) -> CompanyPage:
    """An unsaved read-model row for the ABEO-style fixture."""
    values: dict[str, Any] = dict(
        company_id=1,
        ticker="ABEO",
        company_name="Abeona Therapeutics",
        industry="Biotechnology",
        score_run_id=1,
        as_of_date=DAY,
        composite=7.835,
        divergence=22.42,
        cash_health=6.0,
        milestone=8.5,
        outlier=10.0,
        recency=1.0,
        mcap_bucket="Micro",
        peer_group_key="Biotechnology|Micro",
        peer_n=40,
        price=5.39,
        mcap_musd=307.94,
        w52h=-29.61,
        sma200=-8.3,
        rsi=48.3,
        avg_vol_k=206.5,
        rel_vol=1.25,
        perf_w=1.1,
        perf_m=-3.4,
        perf_q=5.0,
        perf_ytd=-12.5,
        cash_sh=2.57,
        quick_ratio=2.1,
        td_eq=0.1,
        inst_trans=1.2,
        short_float=29.92,
        insider_own=4.1,
        gross_margin=64.0,
        op_margin=12.5,
        earnings_date_text="11/13/2026 8:30:00 AM",
        runway_months=20.0,
        runway_capped=False,
        total_studies=5,
        active_phase_3=2,
        active_phase_2=1,
        latest_trial_nct="NCT01",
        latest_trial_status="RECRUITING",
        short_volume_latest_ratio=Decimal("0.4100"),
        short_volume_trend="up",
        short_volume_delta_pts=3.5,
        insider_filings_count=4,
        insider_buys_count=1,
        insider_sales_count=2,
        insider_officers_directors_buying=1,
        insider_officers_directors_selling=1,
        insider_pct_10b5_1_dollars=50.0,
        carried_inputs=[],
        missing_inputs=[],
        is_fixture=False,
        shr_out_m=30.0,
        is_sparse=False,
        clinical_date=DAY + timedelta(days=60),
        clinical_phase="phase_3",
        clinical_overdue=False,
        next_event_source="clinical",
        financing_date=DAY + timedelta(days=600),
        inst_own=69.11,
        scored_at=None,
        score_components={"cash_health": {"score": 6.0}},
        score_input_resolution={
            "runway": {"state": "fresh", "age_days": 0, "reason": None},
            "catalyst": {"state": "fresh", "age_days": 0, "reason": None},
        },
        runway_status="ok",
        runway_calc_date=DAY,
        runway_cash_usd=Decimal("100000000"),
        runway_cash_tag="CashAndCashEquivalentsAtCarryingValue",
        runway_balance_sheet_date=DAY - timedelta(days=30),
        runway_operating_burn_usd=Decimal("5000000"),
        runway_trial_burn_usd=Decimal("0"),
        runway_burn_total_usd=Decimal("5000000"),
        runway_burn_source="operating_cash_flow",
        runway_cap_reason="",
        runway_trial_burn_lines=[],
        catalyst_status="ok",
        catalyst_as_of=DAY,
        catalyst_candidates=[{"nct_id": "NCT01", "chosen": True}],
        catalyst_source_nct="NCT01",
        catalyst_fallback_end_raw="2027-03",
        field_meta={
            "market": {"as_of": DAY.isoformat(), "age_days": 0, "reasons": []},
            "scores": {"as_of": DAY.isoformat(), "age_days": 0},
            "runway": {"calc_date": DAY.isoformat(), "reasons": []},
            "catalyst": {"as_of": DAY.isoformat(), "reasons": []},
            "short_volume": {
                "latest_trade_date": DAY.isoformat(),
                "age_days": 0,
                "is_stale": False,
                "reasons": [],
            },
            "insider": {"age_days": 1, "is_stale": False, "reasons": []},
            "trial_lists": {"reasons": []},
        },
    )
    values.update(overrides)
    return CompanyPage(**values)


STUDIES = [
    Study(
        "NCT01",
        "PHASE3",
        "RECRUITING",
        primary_completion=DAY + timedelta(days=182),
        conditions=("Geographic Atrophy",),
        interventions=({"type": "DRUG", "name": "DRUG: Zimura"},),
        title="A Phase 3 study",
    ),
    Study(
        "NCT02",
        "PHASE2",
        "RECRUITING",
        primary_completion=DAY + timedelta(days=20),
        conditions=("Geographic Atrophy", "AMD"),
        title="A Phase 2 study",
    ),
]


def page(today: date = DAY, studies: list[Study] | None = None, **overrides: Any) -> dict[str, Any]:
    return build_page(make_page(**overrides), STUDIES if studies is None else studies, today)


def test_the_abeo_tiles_have_the_specified_formats() -> None:
    tiles = page()["tiles"]
    assert tiles["price"]["display"] == "$5.39"
    assert tiles["market_cap"]["display"] == "$308M"
    assert tiles["vs_52w_high"]["display"] == "-29.61%"
    assert tiles["short_float"]["display"] == "29.92%"
    assert tiles["inst_ownership"]["display"] == "69.11%"
    assert tiles["cash_per_share"]["display"] == "$2.57"
    assert tiles["earnings_date"]["display"] == "11/13/2026 8:30:00 AM"
    assert tiles["fair_value"]["status"] == "not_yet_available" and tiles["upside"]["value"] is None


def test_momentum_display_formats_follow_the_requirements() -> None:
    momentum = page(rsi=61.04, rel_vol=1.2345, avg_vol_k=842.3, perf_w=-29.612)["momentum"]
    assert momentum["rsi_14"]["display"] == "61.0"
    assert momentum["rel_volume"]["display"] == "1.235"
    assert momentum["avg_volume_k"]["display"] == "842K"
    assert momentum["perf_week"]["display"] == "-29.61%"
    assert fmt.market_cap(1250) == "$1.3B" and fmt.usd(5.386) == "$5.39"


def test_header_chart_and_peer_group() -> None:
    p = page()
    assert p["header"]["industry"] == "Biotechnology" and p["header"]["as_of_date"] == "2026-10-12"
    assert p["header"]["peer_group"] == {
        "mcap_bucket": "Micro",
        "key": "Biotechnology|Micro",
        "peer_count": 40,
        "is_sparse": False,
    }
    assert p["chart"] == {"provider": "tradingview", "symbol": "ABEO"}


def test_scores_carry_display_values_and_components() -> None:
    scores = page()["scores"]
    assert scores["composite"]["value"] == 7.835 and scores["composite"]["display"] == 7.8
    assert scores["spring"]["display"] == 22.4 and scores["recency"]["shown"] is False
    assert scores["components"] == {"cash_health": {"score": 6.0}}


def test_runway_block_derives_quarters_rating_and_the_burn_breakdown() -> None:
    runway = page()["runway"]
    assert (runway["months"], runway["quarters"], runway["rating"]) == (20.0, 6.7, "Moderate")
    assert runway["cash_usd"] == 100000000.0 and runway["burn_total_monthly_usd"] == 5000000.0
    assert runway["burn_source"] == "operating_cash_flow" and runway["capped"] is False
    assert runway["balance_sheet_date"] == "2026-09-12" and runway["financing_date"] == "2028-06-03"


def test_a_capped_and_a_cash_flow_positive_runway() -> None:
    capped = page(runway_months=360.0, runway_capped=True, runway_cap_reason="tiny_burn")["runway"]
    assert capped["months_label"] == "360+" and capped["cap_reason"] == "tiny_burn"
    meta = make_page().field_meta
    meta["runway"]["reasons"] = ["company_cash_positive"]
    positive = page(runway_months=360.0, runway_capped=True, field_meta=meta)["runway"]
    assert positive["quarters"] is None and positive["rating"] is None
    assert positive["label"] == "cash-flow positive"


def test_trial_block_uses_the_stored_studies() -> None:
    trials = page()["trials"]
    assert trials["next_milestone"]["nct_id"] == "NCT01"  # the Phase 3 study leads
    assert trials["next_milestone"]["quarters_out"] == "2 quarters out"
    assert trials["next_milestone"]["drug"] == "Zimura"
    assert trials["pipeline"]["total"] == 2 and trials["pipeline"]["phase_chips"] == [
        "PHASE2",
        "PHASE3",
    ]
    assert trials["indications"] == ["Geographic Atrophy", "Geographic Atrophy", "AMD"]
    assert [u["nct_id"] for u in trials["upcoming_completions"]] == ["NCT02", "NCT01"]


def test_catalyst_block_lists_every_candidate() -> None:
    catalyst = page()["catalyst"]
    assert catalyst["clinical_date"] == "2026-12-11" and catalyst["phase"] == "phase_3"
    assert catalyst["candidates"] == [{"nct_id": "NCT01", "chosen": True}]
    assert (
        catalyst["next_event_source"] == "clinical" and catalyst["fallback_completion"] == "2027-03"
    )


def test_short_selling_and_insider_blocks() -> None:
    p = page()
    assert p["short_selling"]["label"] == "short volume, not short interest"
    assert p["short_selling"]["latest_ratio"] == 0.41 and p["short_selling"]["trend"] == "up"
    assert (
        p["insider"]["filings_90d"] == 4 and p["insider"]["pct_sale_dollars_under_10b5_1"] == 50.0
    )


# --- T18.1 stale banners ---------------------------------------------------------------------


def codes(p: dict[str, Any]) -> list[str]:
    return [b["code"] for b in p["banners"]]


def test_a_fresh_page_has_no_banner() -> None:
    assert page()["banners"] == []


def test_earnings_date_passed() -> None:
    p = page(today=date(2026, 8, 14), earnings_date_text="8/13/2026 8:30:00 AM")
    assert "earnings_passed" in codes(p)
    messages = {b["code"]: b["message"] for b in p["banners"]}
    assert messages["earnings_passed"] == "Earnings date has passed"
    assert "earnings_passed" not in codes(
        page(
            today=date(2026, 8, 13),
            as_of_date=date(2026, 8, 13),
            earnings_date_text="8/13/2026 8:30:00 AM",
        )
    )


def test_catalyst_date_passed() -> None:
    p = page(clinical_date=DAY - timedelta(days=1), clinical_overdue=True)
    assert [b["message"] for b in p["banners"] if b["code"] == "catalyst_passed"] == [
        "Catalyst date has passed"
    ]
    assert "catalyst_passed" not in codes(page(clinical_date=DAY))


@pytest.mark.parametrize(("age", "stale"), [(120, False), (121, True)])
def test_cash_data_older_than_120_days(age: int, stale: bool) -> None:
    p = page(runway_balance_sheet_date=DAY - timedelta(days=age))
    assert ("cash_stale" in codes(p)) is stale


def test_a_carried_input_shows_its_age() -> None:
    resolution = {
        "runway": {"state": "carried", "age_days": 12, "reason": "stale_carried"},
        "catalyst": {"state": "carried", "age_days": 3, "reason": "stale_carried"},
    }
    p = page(score_input_resolution=resolution, carried_inputs=["runway", "catalyst"])
    assert [b["message"] for b in p["banners"] if b["code"] == "carried_input"] == [
        "Using data from 12 days ago"
    ]


@pytest.mark.parametrize(
    ("today", "stale"),
    [
        (date(2026, 10, 15), False),  # Thursday: 3 trading days after Monday
        (date(2026, 10, 16), True),  # Friday: 4
        (date(2026, 10, 19), True),
    ],
)
def test_market_data_older_than_three_trading_days(today: date, stale: bool) -> None:
    assert ("market_stale" in codes(page(today=today))) is stale


def test_a_holiday_weekend_does_not_make_market_data_look_stale() -> None:
    friday, tuesday = date(2027, 1, 15), date(2027, 1, 19)  # Monday 18 January is a holiday
    meta = make_page().field_meta
    meta["market"]["as_of"] = friday.isoformat()
    p = page(today=tuesday, as_of_date=friday, field_meta=meta)
    assert trading_days_between(friday, tuesday) == 1
    assert "market_stale" not in codes(p)


def test_short_volume_and_insider_staleness_come_from_the_views() -> None:
    meta = make_page().field_meta
    meta["short_volume"]["is_stale"] = True
    meta["insider"]["is_stale"] = True
    p = page(field_meta=meta)
    messages = {b["code"]: b["message"] for b in p["banners"]}
    assert messages["short_volume_stale"] == "Short-volume data is stale"
    assert messages["insider_stale"] == "Insider data is stale"


def test_a_fixture_page_says_so() -> None:
    assert "fixture" in codes(page(is_fixture=True))


# --- T18.3 vintage ---------------------------------------------------------------------------


def test_every_block_lists_its_vintage_and_source() -> None:
    footer = {v["block"]: v for v in page()["vintage"]}
    assert set(footer) == {
        "header",
        "tiles",
        "scores",
        "runway",
        "catalyst",
        "trials",
        "short_selling",
        "insider",
    }
    assert all(v["source"] for v in footer.values())
    assert footer["runway"]["balance_sheet_date"] == "2026-09-12"
    assert footer["short_selling"]["as_of"] == "2026-10-12"
    assert footer["insider"]["as_of"] == "2026-10-11"  # one day old


# --- T18.6 the 55 fields ---------------------------------------------------------------------


def test_the_field_list_is_complete_and_numbered_one_to_fifty_five() -> None:
    assert [field.n for field in FIELDS] == list(range(1, 56))
    assert len({field.key for field in FIELDS}) == 55
    assert set(page()["fields"]) == {field.key for field in FIELDS}


def test_a_full_page_has_a_value_for_every_field() -> None:
    fields = page()["fields"]
    missing = [k for k, v in fields.items() if v["value"] is None]
    assert missing == [] and all(v["reason"] is None for v in fields.values())
    assert (
        fields["runway_quarters"]["value"] == 6.7 and fields["runway_rating"]["value"] == "Moderate"
    )
    assert fields["spring"]["value"] == 22.42 and fields["cash_cat_score"]["value"] == 7.835


def test_an_empty_page_explains_every_missing_field() -> None:
    nulls: dict[str, Any] = dict.fromkeys(
        [
            "price",
            "mcap_musd",
            "w52h",
            "sma200",
            "rsi",
            "avg_vol_k",
            "rel_vol",
            "perf_w",
            "perf_m",
            "perf_q",
            "perf_ytd",
            "cash_sh",
            "quick_ratio",
            "td_eq",
            "inst_trans",
            "short_float",
            "insider_own",
            "gross_margin",
            "op_margin",
            "earnings_date_text",
            "runway_months",
            "runway_capped",
            "total_studies",
            "active_phase_3",
            "active_phase_2",
            "latest_trial_nct",
            "latest_trial_status",
            "short_volume_latest_ratio",
            "short_volume_trend",
            "short_volume_delta_pts",
            "insider_filings_count",
            "insider_buys_count",
            "insider_sales_count",
            "insider_officers_directors_buying",
            "insider_officers_directors_selling",
            "insider_pct_10b5_1_dollars",
        ],
        None,
    )
    meta = make_page().field_meta
    meta["runway"]["reasons"] = ["non_usd_only"]
    meta["short_volume"]["reasons"] = ["not_in_finra_file"]
    meta["insider"]["reasons"] = ["fetch_error"]
    p = page(studies=[], field_meta=meta, **nulls)
    fields = p["fields"]
    assert len(fields) == 55
    for key, item in fields.items():
        assert item["value"] is not None or item["reason"], key
    assert fields["runway_months"]["reason"] == "non_usd_only"
    assert fields["runway_rating"]["reason"] == "non_usd_only"
    assert fields["short_ratio_latest"]["reason"] == "not_in_finra_file"
    assert fields["insider_filings_90d"]["reason"] == "fetch_error"
    assert fields["next_milestone"]["reason"] == "no_trials"
    assert fields["indications"]["reason"] == "no_trials"
    assert fields["price"]["reason"] == "not_reported"
    assert p["trials"]["reasons"] == ["no_trials"]


def test_no_open_market_sales_explains_the_missing_ten_b_five_one_share() -> None:
    p = page(insider_sales_count=0, insider_pct_10b5_1_dollars=None)
    assert p["fields"]["insider_pct_10b5_1"]["reason"] == "no_open_market_sales"


# --- the endpoint ----------------------------------------------------------------------------


@pytest.fixture
def signed_in() -> Client:
    user = get_user_model().objects.create_user("tester", "t@example.com", "pw")
    client = Client()
    client.force_login(user)
    return client


def seed_scored_company(ticker: str = "ABEO") -> Any:
    """A company scored through the real scorer, so the read model is real."""
    companies = [f.company() for _ in range(12)]
    for i, company in enumerate(companies):
        f.ticker(company, ticker=ticker if i == 0 else f"T{i:03d}")
        f.snapshot(
            company,
            mcap_musd=60,
            price=5 + i,
            quick_ratio=1 + i / 5,
            cash_sh=1.5,
            td_eq=0.2,
            rel_vol=1.0,
            eps_next=25,
            inst_own=69.11 if i == 0 else 10,
            earnings_date_text="8/13/2030 8:30:00 AM",
        )
        f.runway_calc(
            company,
            calc_date=DAY,
            runway_months=20.0,
            cash_usd=100_000_000,
            cash_end_date=DAY - timedelta(days=30),
            financing_date=DAY + timedelta(days=500),
        )
    f.ct_study(
        companies[0],
        nct_id="NCT00000001",
        highest_phase="PHASE3",
        status="RECRUITING",
        conditions=["Geographic Atrophy"],
        observed_on=DAY,
        content_hash="a",
        primary_completion_date=DAY + timedelta(days=182),
        study_type="INTERVENTIONAL",
        interventions=[{"type": "DRUG", "name": "Zimura"}],
        title="Study",
    )
    run_scoring(DAY, formula_name="production")
    return companies[0]


@pytest.mark.django_db
def test_the_endpoint_returns_the_page(signed_in: Client) -> None:
    seed_scored_company()
    response = signed_in.get("/api/company/abeo")  # the ticker is case-insensitive
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "ABEO" and body["header"]["industry"] == "Biotechnology"
    assert body["tiles"]["inst_ownership"]["display"] == "69.11%"
    assert body["trials"]["next_milestone"]["nct_id"] == "NCT00000001"
    assert body["trials"]["next_milestone"]["drug"] == "Zimura"
    assert body["runway"]["months"] == 20.0 and body["runway"]["rating"] == "Moderate"
    assert len(body["fields"]) == 55
    assert body["scores"]["input_resolution"]["runway"]["state"] == "fresh"
    assert "no-store" in response["Cache-Control"] or "no-cache" in response["Cache-Control"]


@pytest.mark.django_db
def test_an_unknown_ticker_is_404_and_anonymous_is_401(signed_in: Client) -> None:
    seed_scored_company()
    missing = signed_in.get("/api/company/NOPE")
    assert missing.status_code == 404 and missing.json()["error"] == "unknown_ticker"
    anonymous = Client().get("/api/company/ABEO")
    assert anonymous.status_code == 401 and anonymous.json()["error"] == "authentication_required"


@pytest.mark.django_db
def test_every_scored_company_has_all_55_fields_valued_or_explained(signed_in: Client) -> None:
    seed_scored_company()
    for row in CompanyPage.objects.all():
        body = signed_in.get(f"/api/company/{row.ticker}").json()
        assert len(body["fields"]) == 55
        assert all(v["value"] is not None or v["reason"] for v in body["fields"].values())


@pytest.mark.django_db
def test_the_page_reads_only_the_read_model_and_the_study_versions(signed_in: Client) -> None:
    seed_scored_company()
    signed_in.get("/api/company/ABEO")  # warm the session and content-type caches
    with CaptureQueriesContext(connection) as queries:
        assert signed_in.get("/api/company/ABEO").status_code == 200
    allowed = {"company_page", "trials_ctstudyversion", "django_session", "auth_user"}
    touched: set[str] = set()
    for query in queries.captured_queries:
        touched |= set(re.findall(r'(?:from|join)\s+"?(\w+)"?', query["sql"], flags=re.IGNORECASE))
    assert touched <= allowed, touched - allowed
    assert {"company_page", "trials_ctstudyversion"} <= touched
    assert len(queries) <= 6
