"""T9.1 to T9.5 (integration): the screener JSON API.

The browser page itself (T9.6, T9.7) belongs to the React frontend; these tests cover everything
the page needs from the backend: sign-in, the table, live filters, why-excluded, saved screens,
the funnel, the near-the-line list, the CSV export and the freshness banner.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.screener.service import materialise_default_screen
from tests import factories as f

DAY = date(2026, 10, 12)
PASSWORD = "correct horse battery staple"


@pytest.fixture
def user(db: None) -> Any:
    return get_user_model().objects.create_user("analyst", password=PASSWORD)


@pytest.fixture
def client(user: Any) -> Client:
    c = Client()
    c.force_login(user)
    return c


def make_company(run: Any, ticker: str, composite: float, **fields: Any) -> Any:
    snapshot = {"mcap_musd": 300.0, "price": 5.0, "avg_vol_k": 200.0, "shr_out_m": 100.0}
    snapshot.update({k: v for k, v in fields.items() if k in snapshot})
    result = {k: v for k, v in fields.items() if k not in snapshot}
    company = f.company(name=f"{ticker} Inc", industry=result.pop("industry", "Biotechnology"))
    f.ticker(company, ticker=ticker)
    f.snapshot(company, as_of_date=run.finviz_as_of_date, **snapshot)
    f.score_result(run, company, composite=composite, **result)
    return company


@pytest.fixture
def universe(db: None) -> Any:
    """Six companies in a completed run: three pass the default screen."""
    run = f.score_run(as_of_date=DAY, finviz_as_of_date=DAY)
    make_company(run, "TOP", 8.0, cash_health=9.0, milestone=7.0, outlier=6.0)
    make_company(run, "MID", 6.0)
    make_company(run, "LOW", 5.0)
    make_company(run, "WEAK", 3.0)  # composite floor
    make_company(run, "PENNY", 7.0, price=0.5, avg_vol_k=10.0)  # price and volume
    make_company(run, "SPARSE", 6.5, is_sparse=True)
    return run


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apps.web.screener_api.market_today", lambda: DAY + timedelta(days=1))


def get_json(client: Client, name: str, *args: Any, **params: Any) -> Any:
    response = client.get(reverse(name, args=args), params)
    return response, response.json() if response["Content-Type"].startswith(
        "application/json"
    ) else None


def tickers(payload: dict[str, Any]) -> list[str]:
    return [r["ticker"] for r in payload["rows"]]


# --------------------------------------------------------------------- T9.5 authentication


PROTECTED = [
    ("api-screener", ()),
    ("api-screener-export", ()),
    ("api-near-the-line", ()),
    ("api-funnel", ()),
    ("api-why-excluded", ("AAA",)),
    ("api-screens", ()),
    ("api-screen", (1,)),
]


@pytest.mark.django_db
@pytest.mark.parametrize(("name", "args"), PROTECTED)
def test_anonymous_requests_are_refused_with_the_sign_in_url(
    name: str, args: tuple[Any, ...]
) -> None:
    response = Client().get(reverse(name, args=args))
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "authentication_required"
    assert body["login_url"] == "/api/auth/login"


@pytest.mark.django_db
def test_anonymous_cannot_save_or_delete_screens() -> None:
    c = Client()
    assert (
        c.post(reverse("api-screens"), {"name": "x"}, content_type="application/json").status_code
        == 401
    )
    assert c.delete(reverse("api-screen", args=(1,))).status_code == 401


@pytest.mark.django_db
def test_me_reports_anonymous_without_failing() -> None:
    assert Client().get(reverse("api-me")).json() == {"authenticated": False}


@pytest.mark.django_db
def test_sign_in_with_csrf_protection_and_sign_out(user: Any) -> None:
    c = Client(enforce_csrf_checks=True)
    body = {"username": "analyst", "password": PASSWORD}
    assert c.post(reverse("api-login"), body, content_type="application/json").status_code == 403
    c.get(reverse("api-csrf"))
    token = c.cookies["csrftoken"].value
    bad = c.post(
        reverse("api-login"),
        {**body, "password": "wrong"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert bad.status_code == 401
    assert bad.json()["error"] == "invalid_credentials"
    ok = c.post(reverse("api-login"), body, content_type="application/json", HTTP_X_CSRFTOKEN=token)
    assert ok.status_code == 200
    assert ok.json() == {"authenticated": True, "username": "analyst"}
    assert c.get(reverse("api-me")).json() == {"authenticated": True, "username": "analyst"}
    assert c.get(reverse("api-screens")).status_code == 200
    token = c.cookies["csrftoken"].value
    c.post(reverse("api-logout"), content_type="application/json", HTTP_X_CSRFTOKEN=token)
    assert c.get(reverse("api-screens")).status_code == 401


@pytest.mark.django_db
def test_login_rejects_malformed_bodies(user: Any) -> None:
    c = Client()
    url = reverse("api-login")
    assert c.post(url, "not json", content_type="application/json").status_code == 400
    assert c.post(url, [1, 2], content_type="application/json").status_code == 400
    assert c.post(url, {"username": "analyst"}, content_type="application/json").status_code == 400
    assert (
        c.post(url, {"username": 1, "password": 2}, content_type="application/json").status_code
        == 400
    )


@pytest.mark.django_db
def test_unsafe_requests_need_the_csrf_token(user: Any, universe: Any) -> None:
    c = Client(enforce_csrf_checks=True)
    c.force_login(user)
    response = c.post(reverse("api-screens"), {"name": "x"}, content_type="application/json")
    assert response.status_code == 403


# ------------------------------------------------------------------- T9.1 the default view


@pytest.mark.django_db
def test_default_view_ranks_the_passing_companies(client: Client, universe: Any) -> None:
    response, body = get_json(client, "api-screener")
    assert response.status_code == 200
    assert tickers(body) == ["TOP", "SPARSE", "MID", "LOW"]
    assert [r["rank"] for r in body["rows"]] == [1, 2, 3, 4]
    assert (body["universe_n"], body["count"]) == (6, 4)
    assert body["filters"]["composite_floor"] == 5.0
    assert body["as_of_date"] == "2026-10-12"


@pytest.mark.django_db
def test_rows_carry_every_table_column(client: Client, universe: Any) -> None:
    _, body = get_json(client, "api-screener")
    top = body["rows"][0]
    assert top["ticker"] == "TOP"
    assert top["company"] == "TOP Inc"
    assert top["industry"] == "Biotechnology"
    assert (top["mcap_musd"], top["price"], top["composite"]) == (300.0, 5.0, 8.0)
    assert (top["cash_health"], top["milestone"], top["outlier"]) == (9.0, 7.0, 6.0)
    assert top["peer_group"] == "Biotechnology|Micro"
    assert set(top["next_catalyst"]) == {"date", "phase", "overdue", "source", "financing_date"}
    assert "runway_months" in top
    assert top["display"] == {
        "composite": 8.0,
        "cash_health": 9.0,
        "milestone": 7.0,
        "outlier": 6.0,
    }


@pytest.mark.django_db
def test_display_values_round_half_up(client: Client, universe: Any) -> None:
    run = f.score_run(as_of_date=DAY + timedelta(days=1), finviz_as_of_date=DAY + timedelta(days=1))
    make_company(run, "HALF", 6.25, cash_health=1.25)
    _, body = get_json(client, "api-screener")
    row = body["rows"][0]
    assert row["display"]["composite"] == 6.3
    assert row["display"]["cash_health"] == 1.3


@pytest.mark.django_db
def test_an_empty_database_gives_an_empty_response(client: Client) -> None:
    response, body = get_json(client, "api-screener")
    assert response.status_code == 200
    assert body["empty"] is True
    assert body["rows"] == []


# -------------------------------------------------------------------------- T9.2 live filters


@pytest.mark.django_db
def test_changing_a_filter_changes_the_rows(client: Client, universe: Any) -> None:
    _, body = get_json(client, "api-screener", composite_floor=6.0)
    assert tickers(body) == ["TOP", "SPARSE", "MID"]
    _, body = get_json(client, "api-screener", composite_floor="off")
    assert tickers(body) == ["TOP", "SPARSE", "MID", "LOW", "WEAK"]
    assert body["filters"]["composite_floor"] is None


@pytest.mark.django_db
def test_every_filter_can_be_moved_or_switched_off(client: Client, universe: Any) -> None:
    _, body = get_json(client, "api-screener", price_min="off", avg_vol_min_k="")
    assert "PENNY" in tickers(body)
    _, body = get_json(client, "api-screener", include_sparse="false")
    assert "SPARSE" not in tickers(body)
    _, body = get_json(client, "api-screener", mcap_max_musd=250)
    assert tickers(body) == []
    _, body = get_json(client, "api-screener", mcap_min_musd=400, mcap_max_musd="null")
    assert tickers(body) == []
    _, body = get_json(client, "api-screener", shares_outstanding_max_m=50)
    assert tickers(body) == []
    _, body = get_json(client, "api-screener", outlier_floor=5.5)
    assert tickers(body) == ["TOP"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "key"),
    [
        ({"composite_floor": "high"}, "composite_floor"),
        ({"price_min": "1,5"}, "price_min"),
        ({"include_sparse": "maybe"}, "include_sparse"),
        ({"screen": "999999"}, "screen"),
        ({"screen": "abc"}, "screen"),
    ],
)
def test_bad_filters_are_rejected_naming_the_key(
    client: Client, universe: Any, params: dict[str, str], key: str
) -> None:
    response, body = get_json(client, "api-screener", **params)
    assert response.status_code == 400
    assert body["error"] == "invalid_filters"
    assert key in body["message"]


@pytest.mark.django_db
def test_filter_changes_are_computed_from_stored_scores(client: Client, universe: Any) -> None:
    with CaptureQueriesContext(connection) as queries:
        get_json(client, "api-screener", composite_floor=6.5)
    written = [
        q["sql"]
        for q in queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert not written


# ------------------------------------------------------- A9.7: the read model only


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("api-screener", ()),
        ("api-screener-export", ()),
        ("api-near-the-line", ()),
        ("api-why-excluded", ("WEAK",)),
    ],
)
def test_the_table_export_and_explanations_read_only_the_read_model(
    client: Client, universe: Any, name: str, args: tuple[str, ...]
) -> None:
    client.get(reverse(name, args=args))  # warm up sessions and settings
    with CaptureQueriesContext(connection) as queries:
        assert client.get(reverse(name, args=args)).status_code == 200
    forbidden = (
        "scoring_scoreresult",
        "marketdata_finvizsnapshot",
        "sec_runwaycalc",
        "trials_catalystsignal",
        "companies_company",
        "scoring_scorerun",
    )
    touched = [q["sql"] for q in queries if any(table in q["sql"] for table in forbidden)]
    assert not touched
    assert any("company_page" in q["sql"] for q in queries)


# -------------------------------------------------------------------- freshness banner


@pytest.mark.django_db
@pytest.mark.parametrize(("days_after", "stale"), [(1, False), (3, False), (4, True), (10, True)])
def test_stale_banner_appears_after_three_days(
    client: Client, universe: Any, monkeypatch: pytest.MonkeyPatch, days_after: int, stale: bool
) -> None:
    monkeypatch.setattr(
        "apps.web.screener_api.market_today", lambda: DAY + timedelta(days=days_after)
    )
    _, body = get_json(client, "api-screener")
    assert body["age_days"] == days_after
    assert body["stale"] is stale
    assert (body["stale_banner"] is not None) is stale
    if stale:
        assert f"{days_after} days old" in body["stale_banner"]


@pytest.mark.django_db
def test_the_stale_limit_is_a_setting(
    client: Client, universe: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.platform.models import Setting

    Setting.objects.filter(key="stale.snapshot_days").update(value=1)
    monkeypatch.setattr("apps.web.screener_api.market_today", lambda: DAY + timedelta(days=2))
    assert get_json(client, "api-screener")[1]["stale"] is True


@pytest.mark.django_db
def test_fixture_data_is_flagged(client: Client, universe: Any) -> None:
    assert get_json(client, "api-screener")[1]["is_fixture"] is False


# ------------------------------------------------------------------------------- CSV export


@pytest.mark.django_db
def test_csv_export_matches_the_table(client: Client, universe: Any) -> None:
    response = client.get(reverse("api-screener-export"), {"composite_floor": "6.0"})
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert response["Content-Disposition"] == 'attachment; filename="screener_2026-10-12.csv"'
    rows = list(csv.DictReader(io.StringIO(response.content.decode())))
    _, body = get_json(client, "api-screener", composite_floor=6.0)
    assert [r["ticker"] for r in rows] == tickers(body)
    assert [r["rank"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["composite"] == "8.0"
    assert list(rows[0]) == [
        "rank", "ticker", "company", "industry", "mcap_musd", "price", "composite", "cash_health",
        "milestone", "outlier", "peer_group", "next_catalyst_date", "next_catalyst_source",
        "runway_months",
    ]  # fmt: skip


@pytest.mark.django_db
def test_csv_export_blocks_spreadsheet_formula_injection(client: Client, universe: Any) -> None:
    run = f.score_run(as_of_date=DAY + timedelta(days=1), finviz_as_of_date=DAY + timedelta(days=1))
    company = make_company(run, "EVIL", 9.0)
    company.name = '=HYPERLINK("http://example.com")'
    company.save()
    text = client.get(reverse("api-screener-export")).content.decode()
    assert "\"'=HYPERLINK" in text or "'=HYPERLINK" in text
    assert ",=HYPERLINK" not in text


@pytest.mark.django_db
def test_csv_export_rejects_bad_filters(client: Client, universe: Any) -> None:
    assert client.get(reverse("api-screener-export"), {"price_min": "x"}).status_code == 400


# ------------------------------------------------------------------------- why excluded


@pytest.mark.django_db
def test_why_excluded_lists_every_failed_filter_with_values(client: Client, universe: Any) -> None:
    response, body = get_json(client, "api-why-excluded", "PENNY")
    assert response.status_code == 200
    assert body["passed"] is False
    assert body["rank"] is None
    by_filter = {x["filter"]: x for x in body["failures"]}
    assert set(by_filter) == {"price", "avg_volume"}
    assert by_filter["price"] == {
        "reason": "price_min",
        "filter": "price",
        "actual": 0.5,
        "threshold": 1.0,
        "comparison": "at_least",
    }
    assert by_filter["avg_volume"]["actual"] == 10.0
    assert by_filter["avg_volume"]["threshold"] == 50.0


@pytest.mark.django_db
def test_why_excluded_accepts_lower_case_and_reports_passers(client: Client, universe: Any) -> None:
    _, body = get_json(client, "api-why-excluded", "mid")
    assert body["passed"] is True
    assert body["rank"] == 3
    assert body["failures"] == []


@pytest.mark.django_db
def test_why_excluded_uses_the_filters_in_the_query(client: Client, universe: Any) -> None:
    _, body = get_json(client, "api-why-excluded", "WEAK", composite_floor=2.0)
    assert body["passed"] is True
    _, body = get_json(client, "api-why-excluded", "WEAK")
    assert [x["reason"] for x in body["failures"]] == ["composite_floor"]
    assert body["failures"][0]["actual"] == 3.0


@pytest.mark.django_db
def test_why_excluded_for_an_unknown_ticker(client: Client, universe: Any) -> None:
    response, body = get_json(client, "api-why-excluded", "NOPE")
    assert response.status_code == 404
    assert body["error"] == "unknown_ticker"


@pytest.mark.django_db
def test_why_excluded_reports_missing_values_without_numbers(client: Client, universe: Any) -> None:
    run = f.score_run(as_of_date=DAY + timedelta(days=1), finviz_as_of_date=DAY + timedelta(days=1))
    company = f.company()
    f.ticker(company, ticker="BLANK")
    f.score_result(run, company, composite=8.0)  # no snapshot at all
    _, body = get_json(client, "api-why-excluded", "BLANK")
    reasons = [x["reason"] for x in body["failures"]]
    assert reasons == [
        "mcap_missing",
        "price_missing",
        "avg_vol_missing",
        "shares_outstanding_missing",
    ]
    assert all(x["actual"] is None and x["comparison"] == "present" for x in body["failures"])


# ------------------------------------------------------------------------- near the line


@pytest.mark.django_db
def test_near_the_line_endpoint(client: Client, universe: Any) -> None:
    run = f.score_run(as_of_date=DAY + timedelta(days=1), finviz_as_of_date=DAY + timedelta(days=1))
    make_company(run, "CLOSE", 4.8)
    make_company(run, "ONEFAIL", 7.0, price=0.5)
    response, body = get_json(client, "api-near-the-line")
    assert response.status_code == 200
    assert tickers(body) == ["ONEFAIL", "CLOSE"]
    assert body["margin"] == 0.5
    assert body["rows"][0]["failures"][0]["filter"] == "price"
    _, narrow = get_json(client, "api-near-the-line", margin=0.1)
    assert tickers(narrow) == ["ONEFAIL"]
    assert get_json(client, "api-near-the-line", margin="wide")[0].status_code == 400


# ------------------------------------------------------------------------------- funnel


@pytest.mark.django_db
def test_funnel_log_groups_events_by_day(client: Client) -> None:
    day1 = f.score_run(as_of_date=DAY, finviz_as_of_date=DAY)
    stays = make_company(day1, "STAY", 7.0)
    leaves = make_company(day1, "LEAVE", 6.0)
    enters = make_company(day1, "ENTER", 3.0)
    materialise_default_screen(day1)
    day2 = f.score_run(
        as_of_date=DAY + timedelta(days=1), finviz_as_of_date=DAY + timedelta(days=1)
    )
    for company, composite in ((stays, 7.0), (leaves, 2.0), (enters, 6.0)):
        f.snapshot(
            company,
            as_of_date=day2.finviz_as_of_date,
            mcap_musd=300.0,
            price=5.0,
            avg_vol_k=200.0,
            shr_out_m=100.0,
        )
        f.score_result(day2, company, composite=composite)
    materialise_default_screen(day2)
    response, body = get_json(client, "api-funnel")
    assert response.status_code == 200
    assert [d["date"] for d in body["days"]] == ["2026-10-13"]
    day = body["days"][0]
    assert [e["ticker"] for e in day["entered"]] == ["ENTER"]
    assert day["entered"][0]["reason"] == "passed_all_filters"
    assert [e["ticker"] for e in day["left"]] == ["LEAVE"]
    assert day["left"][0]["reason"] == "composite_floor"
    assert (day["left"][0]["from"], day["left"][0]["to"]) == ("passed", "excluded")


@pytest.mark.django_db
def test_funnel_log_validation_and_empty_state(client: Client) -> None:
    assert get_json(client, "api-funnel")[1] == {"days": []}
    for bad in ("0", "366", "x", "-1", "1.5"):
        assert get_json(client, "api-funnel", days=bad)[0].status_code == 400


# ---------------------------------------------------------------------------- saved screens


def post_json(client: Client, name: str, body: Any, *args: Any) -> Any:
    return client.post(reverse(name, args=args), body, content_type="application/json")


@pytest.mark.django_db
def test_a_saved_screen_reloads_with_equal_filters(client: Client, universe: Any) -> None:
    filters = {"composite_floor": 6.0, "price_min": None, "include_sparse": False}
    created = post_json(client, "api-screens", {"name": "My screen", "filters": filters})
    assert created.status_code == 201
    saved = created.json()
    assert saved["owner"] == "analyst"
    assert saved["is_default"] is False
    reloaded = client.get(reverse("api-screen", args=(saved["id"],))).json()
    assert reloaded["filters"] == saved["filters"]
    assert reloaded["filters"]["composite_floor"] == 6.0
    assert reloaded["filters"]["price_min"] is None
    assert reloaded["filters"]["mcap_min_musd"] == 75.0  # unspecified filters keep their defaults
    _, direct = get_json(
        client,
        "api-screener",
        **{"composite_floor": 6.0, "price_min": "off", "include_sparse": "false"},
    )
    _, via_saved = get_json(client, "api-screener", screen=saved["id"])
    assert tickers(via_saved) == tickers(direct)


@pytest.mark.django_db
def test_query_filters_override_a_saved_screen(client: Client, universe: Any) -> None:
    saved = post_json(
        client, "api-screens", {"name": "Strict", "filters": {"composite_floor": 7.0}}
    ).json()
    _, body = get_json(client, "api-screener", screen=saved["id"])
    assert tickers(body) == ["TOP"]
    _, body = get_json(client, "api-screener", screen=saved["id"], composite_floor=5.0)
    assert len(tickers(body)) == 4


@pytest.mark.django_db
def test_saved_screens_persist_across_sessions_and_are_shared(user: Any, universe: Any) -> None:
    first = Client()
    first.force_login(user)
    post_json(first, "api-screens", {"name": "Shared", "filters": {"composite_floor": 6.5}})
    colleague = get_user_model().objects.create_user("colleague", password=PASSWORD)
    second = Client()
    second.force_login(colleague)
    listed = second.get(reverse("api-screens")).json()["screens"]
    names = [s["name"] for s in listed]
    assert names[0] == "Default screen"
    assert "Shared" in names
    assert {s["owner"] for s in listed if s["name"] == "Shared"} == {"analyst"}


@pytest.mark.django_db
def test_saving_validates_name_and_filters(client: Client) -> None:
    url = "api-screens"
    assert post_json(client, url, {"name": "", "filters": {}}).status_code == 400
    assert post_json(client, url, {"name": "x" * 101}).status_code == 400
    assert post_json(client, url, {"filters": {}}).status_code == 400
    assert post_json(client, url, {"name": 5}).status_code == 400
    bad = post_json(client, url, {"name": "Bad", "filters": {"composite_flor": 5}})
    assert bad.status_code == 400
    assert "composite_flor" in bad.json()["message"]
    assert post_json(client, url, [1]).status_code == 400
    assert client.post(reverse(url), "garbage", content_type="application/json").status_code == 400


@pytest.mark.django_db
def test_a_saved_screen_name_is_unique_per_owner(client: Client, user: Any) -> None:
    assert post_json(client, "api-screens", {"name": "Dupe"}).status_code == 201
    second = post_json(client, "api-screens", {"name": "Dupe"})
    assert second.status_code == 409
    assert second.json()["error"] == "name_taken"
    other = Client()
    other.force_login(get_user_model().objects.create_user("other", password=PASSWORD))
    assert post_json(other, "api-screens", {"name": "Dupe"}).status_code == 201


@pytest.mark.django_db
def test_screens_can_be_deleted_only_by_their_owner(client: Client, user: Any) -> None:
    saved = post_json(client, "api-screens", {"name": "Mine"}).json()
    other = Client()
    other.force_login(get_user_model().objects.create_user("other", password=PASSWORD))
    assert other.delete(reverse("api-screen", args=(saved["id"],))).status_code == 403
    assert client.delete(reverse("api-screen", args=(saved["id"],))).status_code == 200
    assert client.get(reverse("api-screen", args=(saved["id"],))).status_code == 404
    assert client.delete(reverse("api-screen", args=(saved["id"],))).status_code == 404


@pytest.mark.django_db
def test_the_default_screen_cannot_be_deleted(client: Client) -> None:
    listed = client.get(reverse("api-screens")).json()["screens"]
    default = next(s for s in listed if s["is_default"])
    assert client.delete(reverse("api-screen", args=(default["id"],))).status_code == 403


@pytest.mark.django_db
def test_the_default_screen_cannot_be_created_through_the_api(client: Client) -> None:
    created = post_json(client, "api-screens", {"name": "Fake default", "is_default": True}).json()
    assert created["is_default"] is False


# ------------------------------------------------------------------------------ golden day


@pytest.mark.golden
@pytest.mark.django_db
def test_the_default_view_shows_139_companies_on_the_golden_day(user: Any, golden_day: Any) -> None:
    c = Client()
    c.force_login(user)
    response = c.get(reverse("api-screener"))
    body = response.json()
    assert (body["universe_n"], body["count"]) == (1053, 139)
    assert body["is_fixture"] is True
    assert len(body["rows"]) == 139
    composites = [r["composite"] for r in body["rows"]]
    assert composites == sorted(composites, reverse=True)


@pytest.mark.golden
@pytest.mark.django_db
@pytest.mark.parametrize(("floor", "count"), [(5.5, 88), (5.75, 62), (6.0, 46)])
def test_filter_changes_are_fast_and_correct_on_the_golden_day(
    user: Any, golden_day: Any, floor: float, count: int
) -> None:
    c = Client()
    c.force_login(user)
    c.get(reverse("api-screener"))  # warm up
    timings = []
    for _ in range(3):  # the best of three, so a busy machine does not fail the test
        started = time.perf_counter()
        body = c.get(reverse("api-screener"), {"composite_floor": str(floor)}).json()
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.5
    assert body["count"] == count


@pytest.mark.golden
@pytest.mark.django_db
def test_why_excluded_matches_the_export_for_five_sample_tickers(
    user: Any, golden_day: Any
) -> None:
    _, rows = golden_day
    c = Client()
    c.force_login(user)

    def failed(r: dict[str, str]) -> dict[str, float | None]:
        values = {
            "price": (float(r["price"]) if r["price"] else None, 1.0, "price_min"),
            "avg_volume": (float(r["avg_vol"]) if r["avg_vol"] else None, 50.0, "avg_vol_min"),
            "composite": (float(r["composite_score"]), 5.0, "composite_floor"),
        }
        return {k: v[0] for k, v in values.items() if v[0] is not None and v[0] < v[1]}

    samples = [r for r in rows if len(failed(r)) >= 2][:3] + [
        r for r in rows if r["price"] and set(failed(r)) == {"price"}
    ][:2]
    assert len(samples) == 5
    for r in samples:
        body = c.get(reverse("api-why-excluded", args=(r["ticker"],))).json()
        reported = {
            x["filter"]: x["actual"]
            for x in body["failures"]
            if x["filter"] in {"price", "avg_volume", "composite"}
        }
        assert reported == failed(r), r["ticker"]
        assert body["passed"] is False
