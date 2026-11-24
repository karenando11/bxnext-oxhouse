"""T13.1 to T13.6: ClinicalTrials.gov parsing, matching, filters, versions and failures."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from apps.companies.models import CompanySponsorAlias
from apps.platform.models import PipelineRun, SourceCall
from apps.sec.client import SecClient, SecLimiter
from apps.trials.collector import collect_trials, default_client, ensure_aliases
from apps.trials.ctgov import (
    derive_alias,
    highest_phase,
    keep_study,
    match_sponsor,
    parse_ct_date,
    parse_studies,
    similarity,
)
from apps.trials.models import CtStudyVersion
from tests import factories as f

DAY = date(2026, 10, 12)


def study_json(
    nct: str = "NCT00000001",
    sponsor: str = "Apellis Pharmaceuticals, Inc.",
    status: str = "RECRUITING",
    phases: tuple[str, ...] = ("PHASE3",),
    study_type: str = "INTERVENTIONAL",
    primary: str | None = "2027-06-25",
    completion: str | None = "2027-11",
    posted: str = "2026-06-24",
    conditions: tuple[str, ...] = ("Geographic Atrophy",),
) -> dict[str, Any]:
    status_module: dict[str, Any] = {
        "overallStatus": status,
        "lastUpdatePostDateStruct": {"date": posted},
    }
    if primary:
        status_module["primaryCompletionDateStruct"] = {"date": primary}
    if completion:
        status_module["completionDateStruct"] = {"date": completion}
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct, "briefTitle": f"Study {nct}"},
            "statusModule": status_module,
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": sponsor}},
            "conditionsModule": {"conditions": list(conditions)},
            "designModule": {"studyType": study_type, "phases": list(phases)},
            "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "Zimura"}]},
        }
    }


def page(studies: list[dict[str, Any]], token: str | None = None) -> bytes:
    body: dict[str, Any] = {"studies": studies}
    if token:
        body["nextPageToken"] = token
    return json.dumps(body).encode()


class Fake:
    """A transport that answers by sponsor query (and page token) and records every URL."""

    def __init__(self, answers: dict[str, list[bytes] | int]) -> None:
        self.answers = answers
        self.urls: list[str] = []
        self.calls: dict[str, int] = {}

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.urls.append(url)
        q = parse_qs(urlparse(url).query)
        sponsor = q["query.spons"][0]
        self.calls[sponsor] = self.calls.get(sponsor, 0) + 1
        answer = self.answers[sponsor]
        if isinstance(answer, int):
            return answer, b""
        index = 0 if "pageToken" not in q else int(q["pageToken"][0])
        return 200, answer[index]


def client(transport: Fake, sleeps: list[float] | None = None) -> SecClient:
    return SecClient(
        user_agent="test",
        limiter=SecLimiter(0, sleep=lambda s: None),
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def make_company(name: str = "Apellis Pharmaceuticals, Inc.", **kw: Any) -> Any:
    company = f.company(name=name)
    f.ticker(company, **kw)
    return company


# --- T13.3 dates -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "value", "precision"),
    [("2027-06-25", date(2027, 6, 25), "day"), ("2027-11", date(2027, 11, 1), "month"),
     ("", None, ""), (None, None, ""), ("2027", None, "")],
)  # fmt: skip
def test_date_precision(raw: str | None, value: date | None, precision: str) -> None:
    parsed = parse_ct_date(raw)
    assert (parsed.value, parsed.precision) == (value, precision)
    assert parsed.raw == (raw or "")


def test_highest_phase() -> None:
    assert highest_phase(["PHASE2", "PHASE3"]) == "PHASE3"
    assert highest_phase(["EARLY_PHASE1"]) == "EARLY_PHASE1"
    assert highest_phase(["NA"]) == "NA"
    assert highest_phase([]) == ""


# --- T13.2 matching --------------------------------------------------------------------------


def test_similarity_ignores_suffixes_and_order() -> None:
    assert similarity("Apellis Pharmaceuticals, Inc.", "Apellis Pharmaceuticals") == 1.0
    assert similarity("Pharmaceuticals Apellis", "apellis pharmaceuticals inc") == 1.0
    assert similarity("Apellis Pharmaceuticals", "Pfizer") < 0.3


def test_threshold_decides_the_match() -> None:
    aliases = [("Kalvista Pharmaceuticals", False)]
    assert match_sponsor("KalVista Pharmaceuticals, Inc.", aliases) == 1.0
    assert match_sponsor("Kalvista Therapeutics", aliases, threshold=0.6) is not None
    assert match_sponsor("Kalvista Therapeutics", aliases, threshold=0.99) is None
    assert match_sponsor("Novartis", aliases) is None


def test_manual_override_always_matches() -> None:
    assert match_sponsor("Odd Legal Name LLC", [("odd legal name llc", True)]) == 1.0
    assert match_sponsor("Odd Legal Name LLC", [("Something Else", True)], threshold=0.6) is None


def test_derive_alias_drops_the_company_suffix() -> None:
    assert derive_alias("Apellis Pharmaceuticals, Inc.") == "Apellis Pharmaceuticals"
    assert derive_alias("Inc") == "Inc"


# --- parsing and filters ---------------------------------------------------------------------


def test_parse_page_and_token() -> None:
    studies, token = parse_studies(page([study_json(phases=("PHASE2", "PHASE3"))], "abc"))
    assert token == "abc"
    study = studies[0]
    assert study.highest_phase == "PHASE3" and study.phases == ["PHASE2", "PHASE3"]
    assert study.completion.precision == "month" and study.completion.value == date(2027, 11, 1)
    assert study.interventions == [{"type": "DRUG", "name": "Zimura"}]
    assert study.conditions == ["Geographic Atrophy"]


def test_missing_optional_modules_parse() -> None:
    bare = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT1"},
            "statusModule": {"overallStatus": "WITHDRAWN"},
        }
    }
    studies, token = parse_studies(json.dumps({"studies": [bare]}).encode())
    assert token is None and studies[0].sponsor_name == "" and studies[0].highest_phase == ""


def test_filters() -> None:
    old = parse_studies(
        page([study_json(status="COMPLETED", primary="2020-01-01", completion="2020-02-01")])
    )[0][0]
    recent = parse_studies(page([study_json(status="COMPLETED", completion="2026-06-01")]))[0][0]
    obs = parse_studies(page([study_json(study_type="OBSERVATIONAL")]))[0][0]
    ended = [
        parse_studies(page([study_json(status=s)]))[0][0]
        for s in ("TERMINATED", "WITHDRAWN", "SUSPENDED")
    ]
    assert keep_study(old, DAY) == (False, "completed_over_730_days_ago")
    assert keep_study(recent, DAY)[0]
    assert keep_study(obs, DAY) == (False, "observational")
    assert all(keep_study(s, DAY)[0] for s in ended)


# --- collection ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_queries_each_alias_reads_all_pages_and_stores_the_match() -> None:
    company = make_company()
    CompanySponsorAlias.objects.create(
        company=company, sponsor_name="Apellis Pharma", source="manual", is_override=False
    )
    transport = Fake(
        {
            "Apellis Pharmaceuticals": [
                page([study_json("NCT00000001")], "1"),
                page([study_json("NCT00000002")]),
            ],
            "Apellis Pharma": [
                page([study_json("NCT00000001"), study_json("NCT00000003", sponsor="Pfizer")])
            ],
        }
    )
    assert ensure_aliases(company)  # both exist; nothing derived
    result = collect_trials(DAY, client=client(transport))
    assert sorted(transport.calls) == ["Apellis Pharma"]  # only the stored active alias is queried
    assert CompanySponsorAlias.objects.filter(company=company).count() == 1
    stored = CtStudyVersion.objects.order_by("nct_id")
    assert [s.nct_id for s in stored] == ["NCT00000001"]  # Pfizer's study is not attached
    assert (
        result.versions_added == 1
        and stored[0].match_score is not None
        and 0.6 <= stored[0].match_score < 1.0
    )
    assert stored[0].completion_precision == "month"
    assert stored[0].completion_raw == "2027-11" and stored[0].completion_date == date(2027, 11, 1)


@pytest.mark.django_db
def test_pagination_and_derived_alias() -> None:
    company = make_company()
    transport = Fake(
        {
            "Apellis Pharmaceuticals": [
                page([study_json("NCT00000001")], "1"),
                page([study_json("NCT00000002")]),
            ]
        }
    )
    result = collect_trials(DAY, client=client(transport))
    assert transport.calls["Apellis Pharmaceuticals"] == 2
    assert CtStudyVersion.objects.filter(company=company).count() == 2
    assert company.sponsor_aliases.get().source == "derived"
    assert result.queries == 1


@pytest.mark.django_db
def test_unchanged_content_writes_nothing_and_a_change_writes_one_row() -> None:
    make_company()
    first = Fake({"Apellis Pharmaceuticals": [page([study_json()])]})
    assert collect_trials(DAY, client=client(first)).versions_added == 1
    again = collect_trials(date(2026, 10, 13), client=client(first))
    assert (again.versions_added, again.unchanged) == (0, 1)
    assert CtStudyVersion.objects.count() == 1
    changed = Fake(
        {"Apellis Pharmaceuticals": [page([study_json(status="ACTIVE_NOT_RECRUITING")])]}
    )
    third = collect_trials(date(2026, 10, 14), client=client(changed))
    assert third.versions_added == 1
    assert list(CtStudyVersion.objects.order_by("id").values_list("status", flat=True)) == [
        "RECRUITING",
        "ACTIVE_NOT_RECRUITING",
    ]


@pytest.mark.django_db
def test_content_that_reverts_reuses_the_old_row() -> None:
    make_company()
    a = Fake({"Apellis Pharmaceuticals": [page([study_json()])]})
    b = Fake({"Apellis Pharmaceuticals": [page([study_json(status="TERMINATED")])]})
    collect_trials(DAY, client=client(a))
    collect_trials(date(2026, 10, 13), client=client(b))
    collect_trials(date(2026, 10, 14), client=client(a))
    assert CtStudyVersion.objects.count() == 2
    latest = CtStudyVersion.objects.order_by("-observed_on").first()
    assert latest is not None and latest.status == "RECRUITING"


@pytest.mark.django_db
def test_dropped_studies_are_counted_and_not_stored() -> None:
    make_company()
    transport = Fake(
        {
            "Apellis Pharmaceuticals": [
                page(
                    [
                        study_json("NCT00000001", study_type="OBSERVATIONAL"),
                        study_json(
                            "NCT00000002",
                            status="COMPLETED",
                            primary="2020-01-01",
                            completion="2020-02-01",
                        ),
                        study_json("NCT00000003", status="TERMINATED"),
                    ]
                )
            ]
        }
    )
    result = collect_trials(DAY, client=client(transport))
    assert dict(result.dropped) == {"observational": 1, "completed_over_730_days_ago": 1}
    assert list(CtStudyVersion.objects.values_list("nct_id", flat=True)) == ["NCT00000003"]


@pytest.mark.django_db
def test_one_failing_sponsor_does_not_stop_the_others() -> None:
    make_company("Alpha Bio, Inc.", ticker="ALPH")
    make_company("Beta Therapeutics, Inc.", ticker="BETA")
    transport = Fake(
        {
            "Alpha Bio": 500,
            "Beta Therapeutics": [
                page([study_json("NCT00000009", sponsor="Beta Therapeutics Inc")])
            ],
        }
    )
    result = collect_trials(DAY, client=client(transport))
    assert result.failures == {"Alpha Bio": "http_5xx"}
    assert transport.calls["Alpha Bio"] == 3  # three attempts
    assert list(CtStudyVersion.objects.values_list("nct_id", flat=True)) == ["NCT00000009"]
    assert result.run.status == PipelineRun.Status.PARTIAL
    failed = SourceCall.objects.get(feed="ctgov_studies", status="failed")
    assert failed.reason_code == "http_5xx" and failed.scope_key == "Alpha Bio"
    assert SourceCall.objects.filter(feed="ctgov_studies", status="ok").count() == 1


@pytest.mark.django_db
def test_a_bad_payload_is_a_parse_error_for_that_sponsor_only() -> None:
    make_company()
    transport = Fake({"Apellis Pharmaceuticals": [b"not json"]})
    result = collect_trials(DAY, client=client(transport))
    assert result.failures == {"Apellis Pharmaceuticals": "parse_error"}
    assert CtStudyVersion.objects.count() == 0


@pytest.mark.django_db
def test_override_alias_matches_a_differently_named_sponsor() -> None:
    company = make_company("Short Co")
    CompanySponsorAlias.objects.create(
        company=company,
        sponsor_name="Totally Different Legal Entity LLC",
        source="manual",
        is_override=True,
    )
    transport = Fake(
        {
            "Totally Different Legal Entity LLC": [
                page([study_json(sponsor="Totally Different Legal Entity LLC")])
            ]
        }
    )
    assert collect_trials(DAY, client=client(transport)).versions_added == 1


@pytest.mark.django_db
def test_inactive_alias_is_not_queried() -> None:
    company = make_company()
    CompanySponsorAlias.objects.create(
        company=company, sponsor_name="Old Name", source="manual", active=False
    )
    CompanySponsorAlias.objects.create(company=company, sponsor_name="New Name", source="manual")
    transport = Fake({"New Name": [page([])]})
    collect_trials(DAY, client=client(transport))
    assert list(transport.calls) == ["New Name"]


# --- T13.5 politeness ------------------------------------------------------------------------


def test_requests_are_spaced_by_the_configured_delay() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = SecLimiter(0.5, clock=lambda: now[0], sleep=sleep)
    transport = Fake({"A": [page([])], "B": [page([])]})
    c = SecClient(user_agent="t", limiter=limiter, transport=transport, sleep=lambda s: None)
    c.get("https://x/?query.spons=A")
    c.get("https://x/?query.spons=B")
    assert sleeps == [0.5]


@pytest.mark.django_db
def test_default_client_reads_the_delay_from_settings() -> None:
    f.setting(key="ctgov_delay_seconds", value=1.5)
    assert default_client().limiter.min_interval == 1.5
    assert default_client(0.25).limiter.min_interval == 0.25


@pytest.mark.django_db
def test_retries_back_off_then_succeed() -> None:
    make_company()
    attempts = {"n": 0}

    def flaky(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        attempts["n"] += 1
        return (503, b"") if attempts["n"] < 3 else (200, page([study_json()]))

    sleeps: list[float] = []
    c = SecClient(
        user_agent="t",
        limiter=SecLimiter(0, sleep=lambda s: None),
        transport=flaky,
        sleep=sleeps.append,
    )
    result = collect_trials(DAY, client=c)
    assert attempts["n"] == 3 and len(sleeps) == 2 and result.versions_added == 1
