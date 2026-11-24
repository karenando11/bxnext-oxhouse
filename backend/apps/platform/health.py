"""The pipeline health report (Step 17): what the last batch did and what needs attention."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from apps.platform.models import AlertState, DataQualityCheck, PipelineRun, SourceCall
from apps.scoring.models import ScoreResult, ScoreRun
from apps.scoring.resolver import DEFAULT_MAX_AGES, max_age

HISTORY_DAYS = 30
CHECK_LIMIT = 300


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _failed_steps(steps: list[dict[str, Any]]) -> list[str]:
    """Steps whose latest entry failed (a resumed batch keeps the failed try too)."""
    latest = {s["step_name"]: s.get("status") for s in steps}
    return [name for name, status in latest.items() if status == "failed"]


def _batch_json(run: PipelineRun, with_steps: bool) -> dict[str, Any]:
    steps = run.steps or []
    out: dict[str, Any] = {
        "run_id": run.pk,
        "run_date": run.run_date.isoformat() if run.run_date else None,
        "market_date": run.as_of_date.isoformat(),
        "status": run.status,
        "trigger": run.trigger,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_seconds": (
            round((run.finished_at - run.started_at).total_seconds()) if run.finished_at else None
        ),
        "failed_steps": _failed_steps(steps),
    }
    if with_steps:
        out["steps"] = steps
    return out


def _sources() -> list[dict[str, Any]]:
    out = []
    for feed, label in SourceCall.Feed.choices:
        if feed == SourceCall.Feed.FIXTURE:
            continue
        calls = SourceCall.objects.filter(feed=feed)
        ok = calls.filter(status=SourceCall.Status.OK).order_by("-requested_at").first()
        bad = calls.filter(status=SourceCall.Status.FAILED).order_by("-requested_at").first()
        out.append(
            {
                "feed": feed,
                "label": label,
                "last_success": _iso(ok.requested_at) if ok else None,
                "last_failure": _iso(bad.requested_at) if bad else None,
                "last_failure_reason": bad.reason_code if bad else None,
            }
        )
    return out


def _checks(batch: PipelineRun | None) -> list[dict[str, Any]]:
    if batch is None:
        return []
    rows = DataQualityCheck.objects.filter(run__started_at__gte=batch.started_at).select_related(
        "run"
    )
    order = {"fail": 0, "warn": 1, "pass": 2}
    items = sorted(rows, key=lambda c: (order.get(c.status, 3), c.check_name, c.scope))
    return [
        {
            "check": c.check_name,
            "scope": c.scope,
            "status": c.status,
            "expected": c.expected,
            "observed": c.observed,
            "detail": c.detail,
        }
        for c in items[:CHECK_LIMIT]
    ]


def _inputs() -> dict[str, Any] | None:
    """Counts of fresh, carried, expired and missing inputs in the latest complete score run."""
    run = (
        ScoreRun.objects.filter(status=ScoreRun.Status.COMPLETE)
        .order_by("-as_of_date", "-id")
        .first()
    )
    if run is None:
        return None
    states: dict[str, Counter[str]] = {"runway": Counter(), "catalyst": Counter()}
    half_age: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    rows = ScoreResult.objects.filter(score_run=run).values_list(
        "input_resolution", "missing_inputs"
    )
    for resolution, missing_inputs in rows:
        for name in missing_inputs:
            missing[name] += 1
        for name in states:
            item = (resolution or {}).get(name)
            if not item:
                continue
            states[name][item["state"]] += 1
            limit = max_age(name) if name in DEFAULT_MAX_AGES else 0
            if item["state"] == "carried" and (item.get("age_days") or 0) * 2 > limit:
                half_age[name] += 1
    return {
        "score_run_id": run.pk,
        "as_of_date": run.as_of_date.isoformat(),
        "companies": run.scored_n,
        "resolution": {name: dict(counter) for name, counter in states.items()},
        "carried_past_half_max_age": dict(half_age),
        "null_inputs": dict(missing),
    }


def build_health() -> dict[str, Any]:
    batches = list(
        PipelineRun.objects.filter(stage=PipelineRun.Stage.BATCH).order_by("-run_date", "-id")[
            :HISTORY_DAYS
        ]
    )
    last = batches[0] if batches else None
    active = AlertState.objects.filter(active=True).order_by("-raised_at")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "last_batch": _batch_json(last, True) if last else None,
        "sources": _sources(),
        "quality_checks": _checks(last),
        "inputs": _inputs(),
        "active_alerts": [
            {
                "key": a.key,
                "kind": a.kind,
                "subject": a.subject,
                "raised_at": _iso(a.raised_at),
                "emails_sent": a.send_count,
            }
            for a in active
        ],
        "history": [_batch_json(b, False) for b in batches],
    }
