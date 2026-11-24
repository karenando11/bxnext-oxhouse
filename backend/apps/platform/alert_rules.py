"""The alert conditions of Step 17, evaluated once at the end of every batch.

Each condition either holds (it is raised: one email, then nothing while it persists) or does not
(it is cleared, so a recurrence sends again). The batch's own step alerts (Finviz failed, scoring
failed, publish rolled back, still running at the deadline) are raised where they happen; this
module clears them when the step later succeeds and adds the conditions that need a look across
runs:

* the daily batch failed (when no step alert already said why);
* a source failed on two consecutive batch days;
* a Finviz column is blank across the whole universe;
* the Finviz row count changed by more than the configured percentage;
* Form 4 parse failures exceeded the threshold;
* FINRA has no file for a trading day.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from django.db.models import QuerySet

from apps.platform.alerts import DedupAlerts
from apps.platform.calendar import is_trading_day
from apps.platform.models import DataQualityCheck, PipelineRun, SourceCall
from apps.platform.settings_store import get_setting

DEFAULT_FORM4_PARSE_FAILURES_MAX = 5
FINRA_LOOKBACK_DAYS = 10
STEP_ALERTS = (
    ("finviz_failed", "finviz"),
    ("score_failed", "score"),
    ("publish_failed", "score"),
)


def evaluate_alerts(
    run: PipelineRun, market_day: date, alerts: DedupAlerts, holidays: list[date]
) -> None:
    """Raise or clear every condition after the batch ``run`` has finished."""
    _batch_conditions(run, alerts)
    window = DataQualityCheck.objects.filter(run__started_at__gte=run.started_at)
    _finviz_conditions(window, alerts)
    _form4_condition(window, alerts)
    _source_failures(alerts)
    _finra_missing_files(market_day, alerts, holidays)


def _batch_conditions(run: PipelineRun, alerts: DedupAlerts) -> None:
    ok_steps = {s["step_name"] for s in run.steps or [] if s.get("status") == "ok"}
    for kind, step in STEP_ALERTS:
        if step in ok_steps and kind not in alerts.raised_this_run:
            alerts.clear(kind)
    if run.status == PipelineRun.Status.FAILED:
        if not alerts.raised_this_run:  # a step alert already told the team why
            failed = [s["step_name"] for s in run.steps or [] if s.get("status") == "failed"]
            alerts.raise_condition(
                "batch_failed",
                "batch_failed",
                f"Daily batch failed (run {run.pk})",
                f"The batch for {run.run_date} failed. "
                f"Failed steps: {', '.join(failed) or 'none'}.",
            )
    else:
        alerts.clear("batch_failed")
    if "batch_overrun" not in alerts.raised_this_run:
        alerts.clear("batch_overrun")


def _finviz_conditions(window: QuerySet[DataQualityCheck], alerts: DedupAlerts) -> None:
    checks = window.filter(scope__startswith="finviz")
    blank = checks.filter(check_name="blank_column")
    if blank.exists():
        failing = [c.scope for c in blank if c.status == DataQualityCheck.Status.FAIL]
        keep = set()
        for scope in failing:
            key = f"blank_column:{scope}"
            keep.add(key)
            alerts.raise_condition(
                key,
                "blank_column",
                f"Finviz column {scope.removeprefix('finviz.')} is blank for every company",
                f"{scope}: the export has no values in this column for any company.",
            )
        alerts.clear_except("blank_column:", keep)
    count = checks.filter(check_name="row_count").order_by("-id").first()
    if count is not None:
        if count.status == DataQualityCheck.Status.FAIL:
            change = count.detail.get("change_pct")
            alerts.raise_condition(
                "finviz_row_count",
                "finviz_row_count",
                "Finviz row count changed sharply",
                f"{count.observed} rows against {count.detail.get('previous')} on "
                f"{count.detail.get('previous_day')} ({change}%; limit "
                f"{count.detail.get('threshold_pct')}%).",
            )
        else:
            alerts.clear("finviz_row_count")


def _form4_condition(window: QuerySet[DataQualityCheck], alerts: DedupAlerts) -> None:
    check = window.filter(check_name="form4_parse_failures").order_by("-id").first()
    if check is None:
        return
    limit = int(get_setting("alerts.form4_parse_failures_max", DEFAULT_FORM4_PARSE_FAILURES_MAX))
    failures = int(check.observed or 0)
    if failures > limit:
        alerts.raise_condition(
            "form4_parse_failures",
            "form4_parse_failures",
            f"{failures} Form 4 documents could not be parsed",
            f"More than the limit of {limit}. Accessions: "
            f"{', '.join(sorted(check.detail.get('failures', {})))[:600]}",
        )
    else:
        alerts.clear("form4_parse_failures")


def _source_failures(alerts: DedupAlerts) -> None:
    """A feed with failed calls and no good call on each of the last two batch days."""
    by_day: dict[date, list[PipelineRun]] = defaultdict(list)
    batches = PipelineRun.objects.filter(stage=PipelineRun.Stage.BATCH).exclude(
        status=PipelineRun.Status.RUNNING
    )
    for batch in batches.order_by("-run_date", "-id")[:12]:
        if batch.run_date is not None:
            by_day[batch.run_date].append(batch)
    days = sorted(by_day, reverse=True)[:2]
    if len(days) < 2:
        alerts.clear_except("source_failing:", set())
        return
    failing: list[set[str]] = []
    for day in days:
        starts = min(b.started_at for b in by_day[day])
        ends = max(b.finished_at or datetime.now(UTC) for b in by_day[day])
        calls = SourceCall.objects.filter(requested_at__gte=starts, requested_at__lte=ends)
        bad = set(calls.filter(status=SourceCall.Status.FAILED).values_list("feed", flat=True))
        good = set(calls.filter(status=SourceCall.Status.OK).values_list("feed", flat=True))
        failing.append(bad - good)
    both = failing[0] & failing[1]
    keep = set()
    for feed in sorted(both):
        key = f"source_failing:{feed}"
        keep.add(key)
        alerts.raise_condition(
            key,
            "source_failing",
            f"{feed} has failed on two consecutive batch days",
            f"Every {feed} call failed or none succeeded on {days[1]} and on {days[0]}.",
        )
    alerts.clear_except("source_failing:", keep)


def _finra_missing_files(market_day: date, alerts: DedupAlerts, holidays: list[date]) -> None:
    """Trading days up to the market date with no loaded FINRA file, in the last ten days."""
    start = market_day - timedelta(days=FINRA_LOOKBACK_DAYS)
    calls = SourceCall.objects.filter(
        feed=SourceCall.Feed.FINRA,
        scope_key__gte=start.isoformat(),
        scope_key__lte=market_day.isoformat(),
    )
    loaded = set(calls.filter(status=SourceCall.Status.OK).values_list("scope_key", flat=True))
    absent = set(calls.filter(reason_code="no_file_for_date").values_list("scope_key", flat=True))
    keep = set()
    for text in sorted(absent - loaded):
        day = date.fromisoformat(text)
        if not is_trading_day(day, holidays):
            continue
        key = f"finra_missing:{text}"
        keep.add(key)
        alerts.raise_condition(
            key,
            "finra_missing",
            f"FINRA short-volume file missing for {text}",
            f"No file has been published for the trading day {text}; each batch asks again.",
        )
    alerts.clear_except("finra_missing:", keep)
