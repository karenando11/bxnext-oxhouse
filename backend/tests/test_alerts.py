"""T17.1 to T17.6: the pipeline health API and the alert conditions (one email each)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

from apps.platform.alert_rules import evaluate_alerts
from apps.platform.alerts import AlertSink, DedupAlerts, EmailAlerts, RecordingAlerts
from apps.platform.models import AlertState, DataQualityCheck, PipelineRun, SourceCall
from tests import factories as f

DAY = date(2026, 10, 14)  # a Wednesday
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def batch_run(
    run_date: date = DAY, status: str = "ok", steps: list[dict[str, Any]] | None = None
) -> PipelineRun:
    started = datetime(run_date.year, run_date.month, run_date.day, 10, 0, tzinfo=UTC)
    return PipelineRun.objects.create(
        stage=PipelineRun.Stage.BATCH,
        as_of_date=run_date - timedelta(days=1),
        run_date=run_date,
        trigger="schedule",
        started_at=started,
        finished_at=started + timedelta(minutes=20),
        status=status,
        steps=steps or [],
    )


def collector_run(batch: PipelineRun) -> PipelineRun:
    return PipelineRun.objects.create(
        stage=PipelineRun.Stage.FILINGS,
        as_of_date=batch.as_of_date,
        trigger="schedule",
        started_at=batch.started_at + timedelta(minutes=1),
        status="ok",
    )


def check(run: PipelineRun, name: str, scope: str, status: str, **kw: Any) -> None:
    DataQualityCheck.objects.create(run=run, check_name=name, scope=scope, status=status, **kw)


def call(
    feed: str, status: str, when: datetime, scope: str = "", reason: str | None = None
) -> None:
    SourceCall.objects.create(
        feed=feed, scope_key=scope, requested_at=when, status=status, reason_code=reason
    )


def evaluate(run: PipelineRun, sink: AlertSink | None = None) -> RecordingAlerts:
    recorder = RecordingAlerts()
    dedup = DedupAlerts(sink or recorder)
    evaluate_alerts(run, run.as_of_date, dedup, [])
    return recorder


# --- de-duplication (T17.3, T17.4) -------------------------------------------------------------


@pytest.mark.django_db
def test_a_persisting_condition_sends_once_and_a_recurrence_sends_again() -> None:
    sink = RecordingAlerts()
    first = DedupAlerts(sink)
    assert first.raise_condition("k", "kind", "subject", "body") is True
    assert first.raise_condition("k", "kind", "subject", "body") is False
    second = DedupAlerts(sink)  # the next batch
    assert second.raise_condition("k", "kind", "subject", "body") is False
    assert sink.kinds() == ["kind"]
    second.clear("k")
    assert AlertState.objects.get(key="k").active is False
    assert second.raise_condition("k", "kind", "subject", "body") is True
    assert sink.kinds() == ["kind", "kind"]
    assert AlertState.objects.get(key="k").send_count == 2


class Broken:
    def send(self, kind: str, subject: str, body: str) -> None:
        raise OSError("mail server down")


@pytest.mark.django_db
def test_an_undeliverable_alert_is_tried_again_next_time() -> None:
    assert DedupAlerts(Broken()).raise_condition("k", "kind", "s", "b") is False
    assert not AlertState.objects.filter(key="k").exists()
    sink = RecordingAlerts()
    assert DedupAlerts(sink).raise_condition("k", "kind", "s", "b") is True


@pytest.mark.django_db
def test_the_email_sink_sends_one_message_to_the_team(settings: Any) -> None:
    sink = EmailAlerts(["a@example.com", "b@example.com"], "alerts@example.com")
    DedupAlerts(sink).raise_condition("k", "kind", "Batch failed", "details")
    DedupAlerts(sink).raise_condition("k", "kind", "Batch failed", "details")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].subject == "[OxHouse] Batch failed"
    assert mail.outbox[0].to == ["a@example.com", "b@example.com"]


# --- T17.2 each condition ---------------------------------------------------------------------


@pytest.mark.django_db
def test_a_failed_batch_with_no_step_alert_is_reported_once() -> None:
    run = batch_run(status="failed", steps=[{"step_name": "sec", "status": "failed"}])
    assert evaluate(run).kinds() == ["batch_failed"]
    assert evaluate(run).kinds() == []  # the same failure on the next evaluation


@pytest.mark.django_db
def test_a_failed_batch_whose_step_already_alerted_adds_nothing() -> None:
    run = batch_run(status="failed")
    dedup = DedupAlerts(sink := RecordingAlerts())
    dedup.send("finviz_failed", "Finviz down", "x")
    evaluate_alerts(run, run.as_of_date, dedup, [])
    assert sink.kinds() == ["finviz_failed"]


@pytest.mark.django_db
def test_step_alerts_clear_when_the_step_later_succeeds() -> None:
    DedupAlerts(RecordingAlerts()).send("finviz_failed", "Finviz down", "x")
    run = batch_run(steps=[{"step_name": "finviz", "status": "ok"}])
    evaluate(run)
    assert AlertState.objects.get(key="finviz_failed").active is False


@pytest.mark.django_db
def test_two_consecutive_days_of_a_failing_source_alert_once() -> None:
    yesterday, today = batch_run(DAY - timedelta(days=1)), batch_run(DAY)
    for run in (yesterday, today):
        call("ctgov_studies", "failed", run.started_at + timedelta(minutes=2), reason="fetch_error")
    call("sec_submissions", "failed", today.started_at + timedelta(minutes=2), reason="fetch_error")
    call("sec_submissions", "ok", today.started_at + timedelta(minutes=3))
    assert evaluate(today).kinds() == ["source_failing"]
    assert evaluate(today).kinds() == []
    assert AlertState.objects.get(key="source_failing:ctgov_studies").active


@pytest.mark.django_db
def test_one_failed_day_does_not_alert_and_recovery_clears() -> None:
    yesterday, today = batch_run(DAY - timedelta(days=1)), batch_run(DAY)
    call("ctgov_studies", "failed", today.started_at + timedelta(minutes=2), reason="fetch_error")
    assert evaluate(today).kinds() == []
    call(
        "ctgov_studies", "failed", yesterday.started_at + timedelta(minutes=2), reason="fetch_error"
    )
    assert evaluate(today).kinds() == ["source_failing"]
    tomorrow = batch_run(DAY + timedelta(days=1))
    call("ctgov_studies", "ok", tomorrow.started_at + timedelta(minutes=2))
    evaluate(tomorrow)
    assert AlertState.objects.get(key="source_failing:ctgov_studies").active is False


@pytest.mark.django_db
def test_a_column_blank_across_the_universe_alerts_once_per_column() -> None:
    run = batch_run()
    finviz = collector_run(run)
    check(finviz, "blank_column", "finviz.eps_this", "fail", observed="blank for every company")
    check(finviz, "blank_column", "finviz.rsi", "fail", observed="blank for every company")
    assert evaluate(run).kinds() == ["blank_column", "blank_column"]
    assert evaluate(run).kinds() == []


@pytest.mark.django_db
def test_a_blank_column_clears_when_the_column_returns() -> None:
    run = batch_run()
    check(collector_run(run), "blank_column", "finviz.eps_this", "fail")
    evaluate(run)
    later = batch_run(DAY + timedelta(days=1))
    check(collector_run(later), "blank_column", "finviz.*", "pass")
    evaluate(later)
    assert AlertState.objects.get(key="blank_column:finviz.eps_this").active is False


@pytest.mark.django_db
def test_a_large_change_in_the_finviz_row_count_alerts() -> None:
    run = batch_run()
    detail = {
        "previous": 1054,
        "previous_day": "2026-10-13",
        "change_pct": -40.0,
        "threshold_pct": 20,
    }
    check(collector_run(run), "row_count", "finviz", "fail", observed="632", detail=detail)
    sink = evaluate(run)
    assert sink.kinds() == ["finviz_row_count"]
    assert "632 rows against 1054" in sink.sent[0][2]
    assert evaluate(run).kinds() == []


@pytest.mark.django_db
def test_form4_parse_failures_alert_only_above_the_threshold() -> None:
    run = batch_run()
    form4 = collector_run(run)
    check(form4, "form4_parse_failures", "sec", "warn", observed="5", detail={"failures": {}})
    assert evaluate(run).kinds() == []  # exactly the threshold
    later = batch_run(DAY + timedelta(days=1))
    check(
        collector_run(later),
        "form4_parse_failures",
        "sec",
        "warn",
        observed="6",
        detail={"failures": {"0001-26-000001": "parse_error"}},
    )
    assert evaluate(later).kinds() == ["form4_parse_failures"]
    assert evaluate(later).kinds() == []


@pytest.mark.django_db
def test_a_missing_finra_file_for_a_trading_day_alerts_once() -> None:
    run = batch_run()  # market date Tuesday 13 October
    call("finra_regsho", "ok", NOW, scope="2026-10-12")
    call("finra_regsho", "skipped", NOW, scope="2026-10-13", reason="no_file_for_date")
    call("finra_regsho", "skipped", NOW, scope="2026-10-10", reason="no_file_for_date")  # Saturday
    sink = evaluate(run)
    assert sink.kinds() == ["finra_missing"]
    assert "2026-10-13" in sink.sent[0][1]
    assert evaluate(run).kinds() == []


@pytest.mark.django_db
def test_a_market_holiday_is_not_a_missing_finra_file() -> None:
    run = batch_run(date(2026, 10, 14))
    recorder = RecordingAlerts()
    holidays = [date(2026, 10, 13)]
    call("finra_regsho", "skipped", NOW, scope="2026-10-13", reason="no_file_for_date")
    evaluate_alerts(run, run.as_of_date, DedupAlerts(recorder), holidays)
    assert recorder.kinds() == []


@pytest.mark.django_db
def test_a_file_that_arrives_later_clears_the_alert() -> None:
    run = batch_run()
    call("finra_regsho", "skipped", NOW, scope="2026-10-13", reason="no_file_for_date")
    evaluate(run)
    call("finra_regsho", "ok", NOW + timedelta(hours=3), scope="2026-10-13")
    evaluate(run)
    assert AlertState.objects.get(key="finra_missing:2026-10-13").active is False


# --- T17.1 and T17.6 the health endpoint -------------------------------------------------------


def signed_in() -> Client:
    client = Client()
    user = get_user_model().objects.create_user("analyst", password="pw-for-tests-123")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_the_health_endpoint_requires_sign_in() -> None:
    response = Client().get("/api/health")
    assert response.status_code == 401
    assert response.json()["login_url"] == "/api/auth/login"


@pytest.mark.django_db
def test_seeded_failures_appear_in_the_health_response() -> None:
    run = batch_run(
        status="partial",
        steps=[
            {"step_name": "finviz", "status": "ok"},
            {"step_name": "trials", "status": "failed", "error_text": "fetch_error"},
        ],
    )
    batch_run(DAY - timedelta(days=1))
    check(collector_run(run), "blank_column", "finviz.eps_this", "fail", observed="blank")
    check(collector_run(run), "row_count", "finviz", "pass", observed="1054")
    call("ctgov_studies", "ok", NOW - timedelta(days=3))
    call("ctgov_studies", "failed", NOW, reason="fetch_error")
    DedupAlerts(RecordingAlerts()).send("finviz_failed", "Finviz down", "x")
    data = signed_in().get("/api/health").json()
    assert data["last_batch"]["status"] == "partial"
    assert data["last_batch"]["failed_steps"] == ["trials"]
    assert [s["step_name"] for s in data["last_batch"]["steps"]] == ["finviz", "trials"]
    ctgov = next(s for s in data["sources"] if s["feed"] == "ctgov_studies")
    assert ctgov["last_failure_reason"] == "fetch_error"
    assert ctgov["last_success"].startswith("2026-10-11")
    assert data["quality_checks"][0]["status"] == "fail"
    assert data["quality_checks"][0]["scope"] == "finviz.eps_this"
    assert [a["key"] for a in data["active_alerts"]] == ["finviz_failed"]
    assert [h["run_date"] for h in data["history"]] == ["2026-10-14", "2026-10-13"]
    assert data["inputs"] is None  # no score run yet


@pytest.mark.django_db
def test_the_health_response_counts_carried_and_null_inputs() -> None:
    run = f.score_run(as_of_date=DAY)
    resolution = [
        {
            "runway": {"state": "fresh", "age_days": 0},
            "catalyst": {"state": "fresh", "age_days": 0},
        },
        {
            "runway": {"state": "carried", "age_days": 90},
            "catalyst": {"state": "carried", "age_days": 2},
        },
        {
            "runway": {"state": "carried", "age_days": 10},
            "catalyst": {"state": "expired", "age_days": 20},
        },
        {"runway": {"state": "missing", "age_days": None}, "catalyst": {"state": "missing"}},
    ]
    for item in resolution:
        f.score_result(run, input_resolution=item, missing_inputs=[])
    f.score_result(run, input_resolution=resolution[3], missing_inputs=["runway_months"])
    inputs = signed_in().get("/api/health").json()["inputs"]
    assert inputs["resolution"]["runway"] == {"fresh": 1, "carried": 2, "missing": 2}
    assert inputs["resolution"]["catalyst"] == {
        "fresh": 1,
        "carried": 1,
        "expired": 1,
        "missing": 2,
    }
    # a carried runway older than half its 120-day limit (90 days) and a carried catalyst
    # older than half of 7 days (none: 2 days is under 3.5)
    assert inputs["carried_past_half_max_age"] == {"runway": 1}
    assert inputs["null_inputs"] == {"runway_months": 1}


# --- through the real batch (T17.2 to T17.4) ---------------------------------------------------


@pytest.mark.django_db
def test_a_finviz_outage_emails_once_then_again_after_recovery(monkeypatch: Any) -> None:
    from apps.platform.batch import run_batch
    from tests.test_batch import World

    monkeypatch.setenv("FINVIZ_AUTH_TOKEN", "test-token")
    sink = RecordingAlerts()
    bad, good = World(), World()
    bad.finviz_status = 500
    run_batch(date(2026, 10, 13), sources=bad.sources(), alerts=sink)
    run_batch(date(2026, 10, 14), sources=bad.sources(), alerts=sink)  # still down
    # the step alert, then the two-days-in-a-row alert; the third check of the outage adds nothing
    assert sink.kinds() == ["finviz_failed", "source_failing"]
    run_batch(date(2026, 10, 15), sources=good.sources(), alerts=sink)  # recovers
    assert AlertState.objects.get(key="finviz_failed").active is False
    run_batch(date(2026, 10, 16), sources=bad.sources(), alerts=sink)  # down again
    assert sink.kinds() == ["finviz_failed", "source_failing", "finviz_failed"]


@pytest.mark.django_db
def test_a_step_that_failed_and_was_resumed_is_not_listed_as_failed() -> None:
    batch_run(
        steps=[
            {"step_name": "sec", "status": "failed"},
            {"step_name": "trials", "status": "failed"},
            {"step_name": "sec", "status": "ok"},
        ]
    )
    assert signed_in().get("/api/health").json()["last_batch"]["failed_steps"] == ["trials"]
