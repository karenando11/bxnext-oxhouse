"""The daily batch (Step 16): every source, then scoring, the screen and the read model, in order.

``run_batch(run_date)`` is started by a timer at 06:00 US Eastern, Monday to Friday. It describes
the last completed trading day before the run date (the *market date*): the Finviz snapshot, the
score run and the screen are stored under it.

Each unit of work keeps its own run and source-call records (they already did before this step);
the batch run (stage ``batch``) records one step per unit, in order, with the ids of the runs it
started. Rules:

* **No overlap.** A PostgreSQL advisory lock is held for the whole batch; a second start exits
  without work. The lock goes with the process, so a crash never leaves it stuck.
* **Weekends and holidays.** A run whose market date an earlier run already described collects
  filings, trials, insider and short-volume data, records "no new market day", and skips the
  market steps and sends no alert. The next trading day's batch catches up.
* **Failure isolation.** A failing source is recorded and the batch goes on. Finviz failing, a
  failed row-count check or a blank scoring column stops scoring; the run is then marked failed
  and an alert is sent. Insider (Form 4) and short-volume failures never block scoring, and a
  down SEC feed leaves scoring to carried-forward inputs.
* **Publish last, all at once.** Scoring, the screen and the read-model check run in one database
  transaction, so readers see the previous day's complete page or the new one, never a mix. If
  any of them fails the transaction is rolled back and the previous page stays.
* **Deterministic.** The scorer's "now" is the scheduled start (06:00 US Eastern on the run
  date), not the clock, so a re-run the same day gives identical scores.
* **Resumable.** A batch for a run date that did not finish cleanly is resumed: steps already
  recorded as ok are not repeated.
* **Overrun alert.** A batch still running at 07:00 sends one alert naming its current step.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from django.db import connection, transaction

from apps.marketdata.finra.client import Transport as FinraTransport
from apps.marketdata.finra.ingest import collect_short_volume
from apps.marketdata.finviz.client import Transport as FinvizTransport
from apps.marketdata.finviz.ingest import FinvizIngestFailed, IngestResult, ingest_finviz
from apps.marketdata.models import FinvizSnapshot, ShortVolumeDaily
from apps.marketdata.storage import RawStorage
from apps.platform.alerts import AlertSink, LogAlerts
from apps.platform.calendar import market_date_for
from apps.platform.models import PipelineRun
from apps.platform.settings_store import get_setting
from apps.platform.steps import StepRecorder
from apps.scoring.models import ScoreRun
from apps.scoring.resolver import max_age, resolve_catalysts, resolve_runways
from apps.scoring.service import ScoreRunFailed, load_formula, run_scoring
from apps.screener.service import materialise_default_screen
from apps.sec.client import SecClient
from apps.sec.collector import SecCollectionFailed, collect_sec
from apps.sec.form4_collector import Form4Collector
from apps.sec.models import SecForm4Filing
from apps.sec.runway_service import calculate_universe
from apps.trials.catalyst_service import derive_universe
from apps.trials.collector import collect_trials
from apps.web.models import CompanyPage

logger = logging.getLogger(__name__)

MARKET_TZ = ZoneInfo("America/New_York")
LOCK_KEY = 7_160_001  # the advisory lock of the daily batch
DEFAULT_START_ET = "06:00"
DEFAULT_DEADLINE_ET = "07:00"
DEFAULT_FORMULA = "production"

# Finviz columns that feed the scores. One blank for every company stops scoring.
# ``eps_this`` joins the list once the export carries real values for it.
SCORING_INPUT_FIELDS = frozenset(
    {
        "price",
        "mcap_musd",
        "quick_ratio",
        "cash_sh",
        "td_eq",
        "pcash",
        "eps_next",
        "rel_vol",
        "earnings_date_text",
        "perf_ytd",
        "perf_m",
        "perf_w",
        "rsi",
        "inst_own",
        "short_float",
        "vol_m",
    }
)

OK, FAILED, SKIPPED = "ok", "failed", "skipped"


@dataclass
class Sources:
    """The external connections. Left empty, every source is reached the normal way."""

    finviz_content: bytes | None = None  # a saved export: nothing is downloaded
    finviz_transport: FinvizTransport | None = None
    finra_transport: FinraTransport | None = None
    sec_client: SecClient | None = None
    ctgov_client: SecClient | None = None
    storage: RawStorage | None = None
    sleep: Callable[[float], None] | None = None  # replaces waiting between retries


@dataclass
class Outcome:
    status: str = OK
    rows_in: int | None = None
    rows_out: int | None = None
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchResult:
    status: str  # ok, partial, failed or locked
    run: PipelineRun | None = None
    market_date: date | None = None
    new_market_day: bool = True
    scored: bool = False
    resumed: bool = False


# ------------------------------------------------------------------------------------- the lock


@contextmanager
def batch_lock() -> Iterator[bool]:
    """Hold the batch's advisory lock. Yields False, without waiting, when it is already held."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        row = cursor.fetchone()
        got = bool(row and row[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])


# ------------------------------------------------------------------------------ the time budget


def _at(day: date, text: str) -> datetime:
    hour, minute = (int(part) for part in text.split(":"))
    return datetime.combine(day, clock_time(hour, minute), tzinfo=MARKET_TZ)


def scheduled_start(run_date: date) -> datetime:
    """06:00 US Eastern on the run date (setting ``schedule.batch_start_et``), as an aware time."""
    return _at(run_date, str(get_setting("schedule.batch_start_et", DEFAULT_START_ET)))


def deadline_for(run_date: date) -> datetime:
    return _at(run_date, str(get_setting("schedule.batch_deadline_et", DEFAULT_DEADLINE_ET)))


class OverrunWatch:
    """Sends one alert, naming the step in progress, if the batch runs past its deadline."""

    def __init__(
        self,
        alerts: AlertSink,
        seconds_left: float,
        current_step: Callable[[], str],
        run: PipelineRun,
    ) -> None:
        self._done = threading.Event()
        self._alerts, self._current, self._run = alerts, current_step, run
        self.sent = False
        self._thread = threading.Thread(target=self._wait, args=(seconds_left,), daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        self._thread.join(timeout=2)

    def _wait(self, seconds: float) -> None:
        if self._done.wait(timeout=max(seconds, 0)):
            return
        self.sent = True
        step = self._current() or "starting"
        try:
            self._alerts.send(
                "batch_overrun",
                f"Daily batch still running (run {self._run.pk})",
                f"The batch for {self._run.run_date} is still on step '{step}' at its deadline.",
            )
        except Exception:  # an alert that cannot be sent must not stop the batch
            logger.exception("Could not send the overrun alert")


# --------------------------------------------------------------------------- the batch itself


def _settings_holidays() -> list[date]:
    raw = get_setting("market_holidays", []) or []
    return [date.fromisoformat(str(item)) for item in raw]


def _already_described(market_day: date, run_date: date) -> bool:
    """An earlier run date's batch already scored this market date."""
    for run in PipelineRun.objects.filter(
        stage=PipelineRun.Stage.BATCH, as_of_date=market_day, run_date__lt=run_date
    ):
        if any(s["step_name"] == "score" and s["status"] == OK for s in run.steps):
            return True
    return False


def _open_run(run_date: date, market_day: date, trigger: str) -> tuple[PipelineRun, bool]:
    """The batch run for ``run_date``: the unfinished one is resumed, otherwise a new one."""
    prior = (
        PipelineRun.objects.filter(stage=PipelineRun.Stage.BATCH, run_date=run_date)
        .order_by("-id")
        .first()
    )
    unfinished = (PipelineRun.Status.FAILED, PipelineRun.Status.PARTIAL, PipelineRun.Status.RUNNING)
    if prior is not None and prior.status in unfinished:
        prior.status, prior.finished_at = PipelineRun.Status.RUNNING, None
        prior.as_of_date = market_day
        prior.save(update_fields=["status", "finished_at", "as_of_date"])
        return prior, True
    run = PipelineRun.objects.create(
        stage=PipelineRun.Stage.BATCH,
        as_of_date=market_day,
        run_date=run_date,
        trigger=trigger,
        started_at=datetime.now(UTC),
    )
    return run, False


class _Batch:
    def __init__(
        self,
        run: PipelineRun,
        run_date: date,
        market_day: date,
        sources: Sources,
        alerts: AlertSink,
        trigger: str,
    ) -> None:
        self.run, self.run_date, self.market_day = run, run_date, market_day
        self.sources, self.alerts, self.trigger = sources, alerts, trigger
        self.steps = StepRecorder(run)
        self.done = {s["step_name"] for s in run.steps if s["status"] == OK}
        self.current = ""
        self.failed_steps: list[str] = []
        self.finviz: IngestResult | None = None
        self.finviz_failed = False
        self.scoring_blocked = ""
        self.partial = False
        self.scored = False

    # -- running one step
    def step(self, name: str, work: Callable[[], Outcome]) -> Outcome | None:
        if name in self.done:
            return None
        self.current = name
        started = time.time()
        try:
            outcome = work()
        except Exception as exc:
            logger.exception("Batch step %s failed", name)
            outcome = Outcome(FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
        self.steps.record(
            name,
            started,
            outcome.status,
            outcome.rows_in,
            outcome.rows_out,
            outcome.error,
            outcome.detail or None,
        )
        if outcome.status == FAILED:
            self.failed_steps.append(name)
        self.current = ""
        return outcome

    def skip(self, name: str, why: str) -> None:
        if name not in self.done:
            self.steps.record(name, time.time(), SKIPPED, error=why)

    # -- the sources
    def finviz_step(self) -> Outcome:
        try:
            result = ingest_finviz(
                self.market_day,
                trigger=self.trigger,
                content=self.sources.finviz_content,
                transport=self.sources.finviz_transport or _finviz_default(),
                sleep=self.sources.sleep or time.sleep,
                storage=self.sources.storage,
            )
        except FinvizIngestFailed as exc:
            self.finviz_failed = True
            self.alerts.send(
                "finviz_failed",
                f"Finviz download failed for {self.market_day}",
                f"{exc.reason_code}: {exc}",
            )
            return Outcome(
                FAILED, error=f"{exc.reason_code}: {exc}"[:500], detail={"run_id": exc.run.pk}
            )
        self.finviz = result
        blocking = [
            f"{c.check_name} {c.scope}".strip()
            for c in result.failed_checks
            if c.check_name == "row_count"
            or (
                c.check_name == "blank_column"
                and c.scope.removeprefix("finviz.") in SCORING_INPUT_FIELDS
            )
        ]
        if blocking:
            self.scoring_blocked = "; ".join(blocking)
        failed_checks = [f"{c.check_name} {c.scope}" for c in result.failed_checks]
        if failed_checks and not blocking:
            self.partial = True  # other failed checks let scoring run, the run is partial
        status = FAILED if blocking else OK
        return Outcome(
            status,
            rows_out=result.rows,
            error=f"failed checks: {', '.join(failed_checks)}" if failed_checks else "",
            detail={
                "run_id": result.run.pk,
                "unchanged": result.unchanged,
                "failed_checks": failed_checks,
            },
        )

    def sec_step(self) -> Outcome:
        form4 = Form4Collector(
            self.market_day,
            client=self.sources.sec_client,
            storage=self.sources.storage,
            trigger=self.trigger,
        )
        try:
            result = collect_sec(
                self.market_day,
                trigger=self.trigger,
                client=self.sources.sec_client,
                storage=self.sources.storage,
                form4_handler=form4,
            )
        except SecCollectionFailed as exc:
            return Outcome(
                FAILED, error=f"{exc.reason_code}: {exc}"[:500], detail={"run_id": exc.run.pk}
            )
        detail: dict[str, Any] = {
            "run_id": result.run.pk,
            "steps": [s["step_name"] for s in result.run.steps],
        }
        errors = []
        if result.failures:
            errors.append(f"{len(result.failures)} companies failed")
        if form4.result is not None:
            detail["form4_run_id"] = form4.result.run.pk
            detail["form4_stored"] = form4.result.stored
            if form4.result.fetch_failures:
                errors.append(f"{len(form4.result.fetch_failures)} Form 4 documents not fetched")
        status = FAILED if errors else OK
        return Outcome(
            status,
            rows_in=result.companies,
            rows_out=result.filings_added,
            error="; ".join(errors),
            detail=detail,
        )

    def finra_step(self) -> Outcome:
        result = collect_short_volume(
            self.market_day,
            trigger=self.trigger,
            transport=self.sources.finra_transport or _finra_default(),
            sleep=self.sources.sleep or time.sleep,
            storage=self.sources.storage,
        )
        failed = result.failed
        return Outcome(
            FAILED if failed else OK,
            rows_out=result.rows_loaded,
            error=f"{len(failed)} dates failed" if failed else "",
            detail={"run_id": result.run.pk},
        )

    def trials_step(self) -> Outcome:
        result = collect_trials(
            self.market_day,
            trigger=self.trigger,
            client=self.sources.ctgov_client,
            storage=self.sources.storage,
        )
        failures = len(result.failures)
        return Outcome(
            FAILED if failures else OK,
            rows_in=result.queries,
            rows_out=result.versions_added,
            error=f"{failures} sponsor queries failed" if failures else "",
            detail={"run_id": result.run.pk},
        )

    def runway_step(self) -> Outcome:
        result = calculate_universe(self.market_day, trigger=self.trigger)
        return Outcome(
            rows_in=result.companies,
            rows_out=result.runway_months_present,
            detail={"run_id": result.run.pk},
        )

    def catalyst_step(self) -> Outcome:
        result = derive_universe(self.market_day, trigger=self.trigger)
        return Outcome(
            rows_in=result.companies,
            rows_out=result.companies,
            detail={"run_id": result.run.pk, "next_event": result.by_source},
        )

    def metrics_step(self) -> Outcome:
        """Insider and short-volume metrics are database views, read as of the market date."""
        short = (
            ShortVolumeDaily.objects.filter(trade_date__lte=self.market_day, company__isnull=False)
            .values("company_id")
            .distinct()
            .count()
        )
        insider = (
            SecForm4Filing.objects.filter(filed_at__lte=self.market_day)
            .values("company_id")
            .distinct()
            .count()
        )
        return Outcome(
            rows_out=short + insider,
            detail={"short_volume_companies": short, "insider_companies": insider},
        )

    # -- the market steps
    def resolve_step(self, carry_forward: bool) -> Outcome:
        ids = list(
            FinvizSnapshot.objects.filter(as_of_date=self.market_day).values_list(
                "company_id", flat=True
            )
        )
        runways = resolve_runways(
            ids, self.market_day, carry_forward=carry_forward, max_age_days=max_age("runway")
        )
        catalysts = resolve_catalysts(
            ids, self.market_day, carry_forward=carry_forward, max_age_days=max_age("catalyst")
        )
        counts: dict[str, dict[str, int]] = {"runway": {}, "catalyst": {}}
        for name, resolved in (("runway", runways), ("catalyst", catalysts)):
            for r in resolved.values():
                counts[name][r.state] = counts[name].get(r.state, 0) + 1
        return Outcome(rows_in=len(ids), rows_out=len(ids), detail={"states": counts})

    def publish(self, formula: str) -> None:
        """Score, run the default screen and check the read model: all or nothing."""
        scored_at = scheduled_start(self.run_date)
        timings: dict[str, tuple[float, Outcome]] = {}
        score_error: ScoreRunFailed | None = None
        publish_error: tuple[str, Exception] | None = None
        try:
            with transaction.atomic():
                tick = time.time()
                self.current = "score"
                try:
                    score_run = run_scoring(
                        self.market_day,
                        formula_name=formula,
                        now=scored_at,
                        pipeline_run=self.run,
                    )
                except ScoreRunFailed as exc:  # keep the failed run's record, publish nothing
                    score_error = exc
                else:
                    timings["score"] = (
                        tick,
                        Outcome(
                            rows_in=score_run.universe_n,
                            rows_out=score_run.scored_n,
                            detail={
                                "score_run_id": score_run.pk,
                                "formula": formula,
                                "scored_at": scored_at.isoformat(),
                            },
                        ),
                    )
                    tick = time.time()
                    self.current = "screen"
                    try:
                        screen = materialise_default_screen(score_run)
                        timings["screen"] = (
                            tick,
                            Outcome(
                                rows_in=screen.universe_n,
                                rows_out=screen.passed_n,
                                detail={"screen_run_id": screen.pk},
                            ),
                        )
                        tick = time.time()
                        self.current = "read_model"
                        served = (
                            ScoreRun.objects.filter(status=ScoreRun.Status.COMPLETE)
                            .order_by("-as_of_date", "-id")
                            .first()
                        )
                        assert served is not None  # the run just scored is complete
                        pages = CompanyPage.objects.filter(score_run_id=served.pk).count()
                        if pages != served.scored_n:
                            raise RuntimeError(
                                f"The read model shows {pages} of {served.scored_n} companies"
                            )
                        timings["read_model"] = (
                            tick,
                            Outcome(
                                rows_in=served.scored_n,
                                rows_out=pages,
                                detail={
                                    "score_run_id": served.pk,
                                    # A later market date is already served (a re-run or backfill
                                    # of an earlier day): its page stays.
                                    "serves_new_run": served.pk == score_run.pk,
                                },
                            ),
                        )
                    except Exception as exc:
                        publish_error = (self.current, exc)
                        raise
        except Exception:
            if publish_error is None:
                raise
            name, error = publish_error
            logger.exception("Publishing failed at %s; rolled back", name)
            self.scored = False
            message = f"{type(error).__name__}: {error}"[:400]
            self.steps.record("score", time.time(), FAILED, error=f"rolled back: {name} failed")
            self.failed_steps.append("score")
            self.steps.record(name, time.time(), FAILED, error=message)
            self.failed_steps.append(name)
            self.alerts.send(
                "publish_failed", f"Batch could not publish {self.market_day}", f"{name}: {message}"
            )
            self.current = ""
            return
        self.current = ""
        if score_error is not None:
            self.steps.record(
                "score",
                time.time(),
                FAILED,
                error=str(score_error)[:400],
                detail={"score_run_id": score_error.run.pk},
            )
            self.failed_steps.append("score")
            self.alerts.send(
                "score_failed", f"Scoring failed for {self.market_day}", str(score_error)
            )
            return
        for name in ("score", "screen", "read_model"):
            tick, outcome = timings[name]
            self.steps.record(
                name,
                tick,
                outcome.status,
                outcome.rows_in,
                outcome.rows_out,
                outcome.error,
                outcome.detail or None,
            )
        self.scored = True


def _finviz_default() -> FinvizTransport:
    from apps.marketdata.finviz.client import urllib_transport

    return urllib_transport


def _finra_default() -> FinraTransport:
    from apps.marketdata.finra.client import urllib_transport

    return urllib_transport


def run_batch(
    run_date: date,
    *,
    trigger: str = PipelineRun.Trigger.MANUAL,
    sources: Sources | None = None,
    alerts: AlertSink | None = None,
    seconds_to_deadline: float | None = None,
) -> BatchResult:
    """Run the daily batch for ``run_date``. Returns ``locked`` without work if one is running."""
    sources = sources or Sources()
    alerts = alerts or LogAlerts()
    with batch_lock() as got:
        if not got:
            logger.warning("A batch is already running; this start does nothing.")
            return BatchResult("locked")
        return _run_locked(run_date, trigger, sources, alerts, seconds_to_deadline)


def _run_locked(
    run_date: date,
    trigger: str,
    sources: Sources,
    alerts: AlertSink,
    seconds_to_deadline: float | None,
) -> BatchResult:
    market_day = market_date_for(run_date, _settings_holidays())
    new_day = not _already_described(market_day, run_date)
    run, resumed = _open_run(run_date, market_day, trigger)
    batch = _Batch(run, run_date, market_day, sources, alerts, trigger)

    if seconds_to_deadline is None:
        seconds_to_deadline = (deadline_for(run_date) - datetime.now(MARKET_TZ)).total_seconds()
    watch: OverrunWatch | None = None
    if seconds_to_deadline > 0:  # a late manual run does not alert for a deadline already gone
        watch = OverrunWatch(alerts, seconds_to_deadline, lambda: batch.current, run)
        watch.start()
    try:
        _run_steps(batch, new_day)
    finally:
        if watch is not None:
            watch.stop()

    failed_hard = (
        batch.finviz_failed
        or bool(batch.scoring_blocked)
        or any(name in batch.failed_steps for name in ("score", "screen", "read_model"))
    )
    if failed_hard:
        status = PipelineRun.Status.FAILED
    elif batch.failed_steps or batch.partial:
        status = PipelineRun.Status.PARTIAL
    else:
        status = PipelineRun.Status.OK
    run.status, run.finished_at = status, datetime.now(UTC)
    run.save(update_fields=["status", "finished_at"])
    return BatchResult(status, run, market_day, new_day, batch.scored, resumed)


def _run_steps(batch: _Batch, new_day: bool) -> None:
    formula = str(get_setting("batch.formula", DEFAULT_FORMULA))
    if new_day:
        batch.step("finviz", batch.finviz_step)
    else:
        batch.skip("market", "no new market day")

    batch.step("sec", batch.sec_step)
    batch.step("finra", batch.finra_step)
    batch.step("trials", batch.trials_step)
    batch.step("runway", batch.runway_step)
    batch.step("catalyst", batch.catalyst_step)
    batch.step("metrics", batch.metrics_step)

    if not new_day:
        return
    if batch.finviz_failed:
        batch.skip("score", "Finviz failed; the previous day's page stays")
        return
    if batch.scoring_blocked:
        batch.skip("score", f"failed quality check: {batch.scoring_blocked}")
        return
    if "finviz" not in batch.done and batch.finviz is None:
        return
    carry = bool(load_formula(formula)[1].quirks.carry_forward)
    batch.step("resolve_inputs", lambda: batch.resolve_step(carry))
    if "score" in batch.done and "read_model" in batch.done:
        batch.scored = True
        return
    batch.publish(formula)


# ---------------------------------------------------------------------------------- dry runs


def replay_sources(directory: Path) -> Sources:
    """Sources that answer from recorded files: no network is touched.

    ``directory`` holds ``finviz.csv`` (the saved export) and ``manifest.json`` (the format of
    ``scripts/record_fixtures.py``: a list of ``{url, status, file}``). A URL that is not in the
    manifest answers 404, as a missing document would.
    """
    import json

    from apps.sec.client import SecLimiter

    answers: dict[str, tuple[int, bytes]] = {}
    manifest = directory / "manifest.json"
    if manifest.exists():
        for item in json.loads(manifest.read_text()):
            body = (directory / item["file"]).read_bytes() if item.get("file") else b""
            answers[item["url"]] = (int(item["status"]), body)

    def answer(url: str) -> tuple[int, bytes]:
        return answers.get(url, (404, b""))

    def sec_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        return answer(url)

    def plain_transport(url: str, timeout: float) -> tuple[int, bytes]:
        return answer(url)

    def client() -> SecClient:
        return SecClient(
            user_agent="dry-run",
            limiter=SecLimiter(0),
            transport=sec_transport,
            sleep=lambda _: None,
            attempts=1,
        )

    finviz = directory / "finviz.csv"
    return Sources(
        finviz_content=finviz.read_bytes() if finviz.exists() else None,
        finviz_transport=plain_transport,
        finra_transport=plain_transport,
        sec_client=client(),
        ctgov_client=client(),
        sleep=lambda _: None,
    )
