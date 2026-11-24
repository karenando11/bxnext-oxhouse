"""T16.1 to T16.11: the daily batch, end to end, with every source replaced by a fake."""

from __future__ import annotations

import json
import time
from datetime import UTC, date
from typing import Any

import psycopg
import pytest
from django.conf import settings
from django.db import connection

from apps.companies.models import TickerHistory
from apps.marketdata.models import FinvizSnapshot
from apps.platform import batch as batch_module
from apps.platform.alerts import RecordingAlerts
from apps.platform.batch import LOCK_KEY, Sources, run_batch, scheduled_start
from apps.platform.models import PipelineRun
from apps.scoring.models import ScoreResult, ScoreRun
from apps.screener.models import ScreenRun
from apps.sec.client import SecClient, SecLimiter
from apps.sec.urls import TICKERS_URL
from apps.web.models import CompanyPage
from tests import factories as f
from tests.finra_samples import FakeFinra
from tests.finviz_samples import export_csv, universe
from tests.sec_samples import submissions_json

RUN = date(2026, 10, 13)  # a Tuesday: it describes Monday 2026-10-12
MARKET = date(2026, 10, 12)
COUNT = 12


def tickers_json(count: int = COUNT) -> bytes:
    mapping = {
        str(i): {"cik_str": 3_000_000 + i, "ticker": f"T{i:03d}", "title": f"T{i:03d} Inc"}
        for i in range(count)
    }
    return json.dumps(mapping).encode()


class World:
    """Fake Finviz, SEC, FINRA and ClinicalTrials.gov. Each can be made to fail."""

    def __init__(self, count: int = COUNT) -> None:
        self.finviz = export_csv(universe(count))
        self.finviz_status = 200
        self.finviz_calls = 0
        self.sec_down = False
        self.sec_calls = 0
        self.sec_delay = 0.0
        self.ct_calls = 0
        self.finra = FakeFinra()
        self.finra_down = False

    def finviz_transport(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.finviz_calls += 1
        return (self.finviz_status, self.finviz if self.finviz_status == 200 else b"")

    def sec_transport(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.sec_calls += 1
        if self.sec_delay:
            time.sleep(self.sec_delay)
            self.sec_delay = 0.0
        if self.sec_down:
            return 503, b""
        if url == TICKERS_URL:
            return 200, tickers_json(COUNT)
        if "submissions" in url:
            return 200, submissions_json([])
        return 404, b""

    def ct_transport(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.ct_calls += 1
        return 200, b'{"studies": []}'

    def finra_transport(self, url: str, timeout: float) -> tuple[int, bytes]:
        if self.finra_down:
            return 500, b""
        return self.finra(url, timeout)

    def sources(self) -> Sources:
        def client(transport: Any) -> SecClient:
            return SecClient(
                user_agent="test",
                limiter=SecLimiter(0),
                transport=transport,
                sleep=lambda _: None,
                attempts=1,
            )

        return Sources(
            finviz_transport=self.finviz_transport,
            finra_transport=self.finra_transport,
            sec_client=client(self.sec_transport),
            ctgov_client=client(self.ct_transport),
            sleep=lambda _: None,
        )


@pytest.fixture(autouse=True)
def _finviz_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINVIZ_AUTH_TOKEN", "test-token")


def names(run: PipelineRun | None) -> list[str]:
    assert run is not None
    return [s["step_name"] for s in run.steps]


def status_of(run: PipelineRun | None) -> dict[str, str]:
    assert run is not None
    return {s["step_name"]: s["status"] for s in run.steps}


# --- T16.1 dry run of the whole batch --------------------------------------------------------


@pytest.mark.django_db
def test_the_whole_batch_runs_in_order_and_publishes() -> None:
    world, alerts = World(), RecordingAlerts()
    result = run_batch(RUN, sources=world.sources(), alerts=alerts)
    assert result.status == "ok" and result.scored and result.new_market_day
    assert result.market_date == MARKET
    assert names(result.run) == [
        "finviz", "sec", "finra", "trials", "runway", "catalyst", "metrics", "resolve_inputs",
        "score", "screen", "read_model",
    ]  # fmt: skip
    assert set(status_of(result.run).values()) == {"ok"}
    assert alerts.sent == []
    # Everything is stored under the market date.
    assert FinvizSnapshot.objects.filter(as_of_date=MARKET).count() == COUNT
    score = ScoreRun.objects.get()
    assert score.as_of_date == MARKET and score.status == "complete" and score.scored_n == COUNT
    assert ScreenRun.objects.get().score_run == score
    assert CompanyPage.objects.count() == COUNT
    assert world.finviz_calls == 1 and world.ct_calls > 0 and world.sec_calls > 0
    assert result.run is not None
    assert result.run.run_date == RUN and result.run.stage == "batch"


@pytest.mark.django_db
def test_every_unit_keeps_its_own_run_and_the_batch_links_them() -> None:
    result = run_batch(RUN, sources=World().sources(), alerts=RecordingAlerts())
    assert result.run is not None
    linked = {s["step_name"]: s.get("detail", {}) for s in result.run.steps}
    for name in ("finviz", "sec", "finra", "trials", "runway", "catalyst"):
        assert PipelineRun.objects.filter(pk=linked[name]["run_id"]).exists(), name
    assert linked["score"]["score_run_id"] == ScoreRun.objects.get().pk


@pytest.mark.django_db
def test_the_dry_run_replays_recorded_files(tmp_path: Any) -> None:
    (tmp_path / "finviz.csv").write_bytes(export_csv(universe(COUNT)))
    (tmp_path / "tickers.json").write_bytes(tickers_json())
    (tmp_path / "manifest.json").write_text(
        json.dumps([{"url": TICKERS_URL, "status": 200, "file": "tickers.json"}])
    )
    result = run_batch(RUN, sources=batch_module.replay_sources(tmp_path), alerts=RecordingAlerts())
    assert result.scored and result.run is not None
    assert status_of(result.run)["finviz"] == "ok"
    assert ScoreRun.objects.count() == 1


# --- T16.9 the scorer's clock ----------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("run_date", "utc_hour"),
    [
        (date(2026, 3, 6), 11),
        (date(2026, 3, 9), 10),
        (date(2026, 11, 2), 11),
        (date(2026, 10, 13), 10),
    ],
)
def test_the_scheduled_start_is_six_eastern_across_daylight_saving(
    run_date: date, utc_hour: int
) -> None:
    start = scheduled_start(run_date).astimezone(UTC)
    assert (start.hour, start.minute, start.date()) == (utc_hour, 0, run_date)


@pytest.mark.django_db
def test_scores_use_the_scheduled_time_and_a_rerun_gives_identical_scores() -> None:
    world = World()
    first = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    second = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert first.run is not None and second.run is not None and first.run.pk != second.run.pk
    runs = list(ScoreRun.objects.order_by("id"))
    assert len(runs) == 2 and runs[0].scored_at == runs[1].scored_at == scheduled_start(RUN)

    def composites(run: ScoreRun) -> dict[int, float]:
        return dict(
            ScoreResult.objects.filter(score_run=run).values_list("company_id", "composite")
        )

    assert composites(runs[0]) == composites(runs[1])
    assert runs[0].status == "superseded" and runs[1].status == "complete"
    assert FinvizSnapshot.objects.filter(as_of_date=MARKET).count() == COUNT  # no duplicates


# --- T16.6 weekends and holidays -------------------------------------------------------------


@pytest.mark.django_db
def test_a_day_with_no_new_market_date_collects_but_does_not_score_or_alert() -> None:
    world, alerts = World(), RecordingAlerts()
    monday = date(2027, 1, 18)  # Martin Luther King Jr. Day: the timer still runs
    tuesday = date(2027, 1, 19)  # the market date is still Friday 15 January
    first = run_batch(monday, sources=world.sources(), alerts=alerts)
    assert first.scored and first.market_date == date(2027, 1, 15)
    sec_before, finra_before = world.sec_calls, len(world.finra.urls)
    second = run_batch(tuesday, sources=world.sources(), alerts=alerts)
    assert second.market_date == date(2027, 1, 15) and second.new_market_day is False
    assert second.status == "ok" and not second.scored
    assert ScoreRun.objects.count() == 1 and ScreenRun.objects.count() == 1
    assert world.finviz_calls == 1  # Finviz was not asked again
    assert world.sec_calls > sec_before and len(world.finra.urls) >= finra_before
    steps = status_of(second.run)
    assert steps["market"] == "skipped" and "score" not in steps and "finviz" not in steps
    assert steps["sec"] == steps["trials"] == steps["runway"] == "ok"
    assert alerts.sent == []
    assert CompanyPage.objects.count() == COUNT  # the previous page stays


# --- T16.2 fault injection -------------------------------------------------------------------


@pytest.mark.django_db
def test_finviz_failure_skips_scoring_alerts_and_keeps_the_previous_page() -> None:
    good = World()
    run_batch(date(2026, 10, 12), sources=good.sources(), alerts=RecordingAlerts())  # Friday 9 Oct
    page_before = list(CompanyPage.objects.values_list("score_run_id", "composite"))
    bad, alerts = World(), RecordingAlerts()
    bad.finviz_status = 503
    result = run_batch(RUN, sources=bad.sources(), alerts=alerts)
    assert result.status == "failed" and not result.scored
    steps = status_of(result.run)
    assert steps["finviz"] == "failed" and steps["score"] == "skipped"
    assert all(steps[n] == "ok" for n in ("sec", "finra", "trials", "runway", "catalyst"))
    assert alerts.kinds() == ["finviz_failed"]
    assert ScoreRun.objects.count() == 1 and ScreenRun.objects.count() == 1
    assert list(CompanyPage.objects.values_list("score_run_id", "composite")) == page_before


@pytest.mark.django_db
@pytest.mark.parametrize("down", ["sec", "finra"])
def test_insider_short_volume_and_sec_failures_never_block_scoring(down: str) -> None:
    world, alerts = World(), RecordingAlerts()
    setattr(world, f"{down}_down", True)
    result = run_batch(RUN, sources=world.sources(), alerts=alerts)
    assert result.scored and result.status == "partial"
    assert status_of(result.run)[down] == "failed"
    assert status_of(result.run)["score"] == "ok"
    assert ScoreRun.objects.get().status == "complete"
    assert alerts.sent == []


@pytest.mark.django_db
def test_a_down_sec_feed_leaves_scoring_to_carried_inputs() -> None:
    world = World()
    run_batch(date(2026, 10, 12), sources=world.sources(), alerts=RecordingAlerts())  # day one
    company = TickerHistory.objects.get(ticker="T000", valid_to__isnull=True).company
    f.runway_calc(company, calc_date=date(2026, 10, 7), runway_months=14.0)  # a good value
    world.sec_down = True
    result = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert result.scored
    # Today's runway has no value; the production formula carries the last good one forward.
    row = ScoreResult.objects.get(score_run__as_of_date=MARKET, company=company)
    assert row.carried_inputs == ["runway"]
    assert row.inputs["runway_months"] == 14.0
    assert row.input_resolution["runway"]["age_days"] == 5


@pytest.mark.django_db
def test_a_failed_row_count_check_stops_scoring() -> None:
    first = World(COUNT)
    run_batch(date(2026, 10, 12), sources=first.sources(), alerts=RecordingAlerts())
    shrunk, alerts = World(COUNT - 6), RecordingAlerts()  # half the universe vanished
    result = run_batch(RUN, sources=shrunk.sources(), alerts=alerts)
    steps = status_of(result.run)
    assert result.status == "failed" and steps["finviz"] == "failed" and steps["score"] == "skipped"
    assert result.run is not None
    assert (
        "row_count" in next(s for s in result.run.steps if s["step_name"] == "score")["error_text"]
    )
    assert ScoreRun.objects.count() == 1


@pytest.mark.django_db
def test_a_blank_scoring_column_stops_scoring() -> None:
    world = World()
    world.finviz = export_csv(universe(COUNT, quick_ratio=""))
    result = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert result.status == "failed" and not result.scored
    assert ScoreRun.objects.count() == 0
    assert result.run is not None
    score = next(s for s in result.run.steps if s["step_name"] == "score")
    assert score["status"] == "skipped" and "quick_ratio" in score["error_text"]


@pytest.mark.django_db
def test_another_failed_check_lets_scoring_run_and_marks_the_run_partial() -> None:
    world = World()
    world.finviz = export_csv(universe(COUNT, beta=""))  # a blank column that is not a score input
    result = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert result.scored and result.status == "partial"
    assert status_of(result.run)["finviz"] == "ok"
    assert ScoreRun.objects.get().status == "complete"


# --- T16.8 the read model is published last, all or nothing ----------------------------------


@pytest.mark.django_db
def test_a_publish_failure_rolls_back_and_the_rerun_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    run_batch(date(2026, 10, 12), sources=world.sources(), alerts=RecordingAlerts())
    page_before = list(CompanyPage.objects.values_list("score_run_id", flat=True).distinct())

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("screen exploded")

    alerts = RecordingAlerts()
    with monkeypatch.context() as patched:
        patched.setattr(batch_module, "materialise_default_screen", boom)
        failed = run_batch(RUN, sources=world.sources(), alerts=alerts)
    assert failed.status == "failed" and not failed.scored
    steps = status_of(failed.run)
    assert steps["score"] == "failed" and steps["screen"] == "failed"
    assert "read_model" not in steps
    assert alerts.kinds() == ["publish_failed"]
    assert ScoreRun.objects.filter(as_of_date=MARKET).count() == 0  # rolled back
    assert (
        list(CompanyPage.objects.values_list("score_run_id", flat=True).distinct()) == page_before
    )

    finviz_calls, sec_calls = world.finviz_calls, world.sec_calls
    resumed = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert failed.run is not None
    assert resumed.resumed and resumed.run is not None and resumed.run.pk == failed.run.pk
    assert resumed.status == "ok" and resumed.scored
    assert world.finviz_calls == finviz_calls and world.sec_calls == sec_calls  # not repeated
    final = status_of(resumed.run)
    assert final["score"] == final["screen"] == final["read_model"] == "ok"
    assert names(resumed.run).count("finviz") == 1  # recorded once, never duplicated
    assert ScoreRun.objects.filter(as_of_date=MARKET, status="complete").count() == 1


@pytest.mark.django_db
def test_a_failed_source_is_retried_on_resume_and_the_rest_is_not_repeated() -> None:
    world = World()
    world.sec_down = True
    first = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert first.status == "partial" and status_of(first.run)["sec"] == "failed"
    trials_calls, finviz_calls = world.ct_calls, world.finviz_calls
    world.sec_down = False
    second = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert first.run is not None
    assert second.resumed and second.run is not None and second.run.pk == first.run.pk
    assert second.status == "ok"
    sec_entries = [s["status"] for s in second.run.steps if s["step_name"] == "sec"]
    assert sec_entries == ["failed", "ok"]
    assert world.ct_calls == trials_calls and world.finviz_calls == finviz_calls
    assert ScoreRun.objects.count() == 1


# --- T16.4 no overlap ------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_second_start_exits_without_work() -> None:
    db = settings.DATABASES["default"]
    name = connection.settings_dict["NAME"]
    other = psycopg.connect(
        host=db["HOST"], port=db["PORT"] or 5432, user=db["USER"], password=db["PASSWORD"],
        dbname=name, autocommit=True,
    )  # fmt: skip
    try:
        other.execute("SELECT pg_advisory_lock(%s)", [LOCK_KEY])
        world = World()
        result = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
        assert result.status == "locked" and result.run is None
        assert PipelineRun.objects.count() == 0 and world.finviz_calls == 0
        other.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
        assert run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts()).status == "ok"
    finally:
        other.close()


# --- T16.10 the 07:00 alert ------------------------------------------------------------------


@pytest.mark.django_db
def test_a_batch_still_running_at_the_deadline_alerts_once_naming_the_step() -> None:
    world, alerts = World(), RecordingAlerts()
    world.sec_delay = 0.6  # the SEC step is slow
    result = run_batch(RUN, sources=world.sources(), alerts=alerts, seconds_to_deadline=0.2)
    assert result.status == "ok"
    assert alerts.kinds() == ["batch_overrun"]
    subject, body = alerts.sent[0][1], alerts.sent[0][2]
    assert "still running" in subject and "'sec'" in body


@pytest.mark.django_db
def test_a_batch_that_finishes_in_time_does_not_alert() -> None:
    alerts = RecordingAlerts()
    run_batch(RUN, sources=World().sources(), alerts=alerts, seconds_to_deadline=60)
    assert alerts.sent == []


@pytest.mark.django_db
def test_a_late_manual_run_does_not_alert_for_a_deadline_already_gone() -> None:
    alerts = RecordingAlerts()
    run_batch(RUN, sources=World().sources(), alerts=alerts, seconds_to_deadline=-5)
    assert alerts.sent == []


# --- Idempotence -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_company_rows_are_not_duplicated_by_repeated_runs() -> None:
    world = World()
    for _ in range(3):
        run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())
    assert FinvizSnapshot.objects.filter(as_of_date=MARKET).count() == COUNT
    assert ScoreRun.objects.filter(status="complete").count() == 1


@pytest.mark.django_db
def test_backfilling_an_earlier_day_leaves_the_newer_page_up() -> None:
    world = World()
    newer = run_batch(RUN, sources=world.sources(), alerts=RecordingAlerts())  # market 12 Oct
    assert newer.scored
    served = list(CompanyPage.objects.values_list("score_run_id", flat=True).distinct())
    earlier = run_batch(date(2026, 10, 12), sources=world.sources(), alerts=RecordingAlerts())
    assert earlier.status == "ok" and earlier.scored  # market date 9 October
    step = next(s for s in earlier.run.steps if s["step_name"] == "read_model")  # type: ignore[union-attr]
    assert step["status"] == "ok" and step["detail"]["serves_new_run"] is False
    assert list(CompanyPage.objects.values_list("score_run_id", flat=True).distinct()) == served
