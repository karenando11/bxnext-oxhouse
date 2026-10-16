"""Score a stored day.

``run_scoring`` reads one day's stored inputs, scores the whole universe under a formula version
with ``scoring_engine``, and stores the scores together with the exact inputs and every component.

* Atomic: all results are written in one transaction. A failure leaves no partial rows and is
  recorded as a failed run with its error.
* Deterministic: the same stored inputs and formula version give identical results.
* History is kept: a later completed run for the same day supersedes the earlier one, which stays.
  Readers use the latest completed run.

Runway and catalyst inputs are the latest rows at or before the day. Choosing between fresh,
carried-forward and expired values is the job of the effective-input resolver (Step 15).
"""

from __future__ import annotations

import dataclasses
import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction

from apps.marketdata.models import FinvizSnapshot
from apps.platform.models import PipelineRun
from apps.scoring.models import FormulaVersion, ScoreResult, ScoreRun
from apps.sec.models import RunwayCalc
from apps.trials.models import CatalystSignal
from scoring_engine import DIMENSIONS, CompanyInputs, CompanyScores, score_universe_with_stats
from scoring_engine import FormulaVersion as EngineFormula

MARKET_TIMEZONE = ZoneInfo("America/New_York")
STAGE_A_TIME = time(16, 30)  # the market snapshot is scored at 16:30 US Eastern
BATCH_SIZE = 500

# Inputs reported as missing when they are absent for a company.
TRACKED_INPUTS = (
    "quick_ratio",
    "cash_per_share",
    "price",
    "debt_to_equity",
    "price_to_cash",
    "runway_months",
    "eps_next",
    "rel_vol",
    "eps_this",
    "earnings_date_text",
)


class ScoreRunFailed(RuntimeError):
    """A score run failed; ``run`` is the stored failed run."""

    def __init__(self, run: ScoreRun, cause: BaseException) -> None:
        super().__init__(f"Score run for {run.as_of_date} failed: {cause}")
        self.run = run


def stage_a_time(day: date) -> datetime:
    """The scorer's 'now' for a day: 16:30 US Eastern, as an aware UTC timestamp."""
    return datetime.combine(day, STAGE_A_TIME, tzinfo=MARKET_TIMEZONE).astimezone(UTC)


def load_formula(
    name: str = "parity", version: int | None = None
) -> tuple[FormulaVersion, EngineFormula]:
    """The stored formula version (latest of ``name`` unless pinned) and its engine form."""
    versions = FormulaVersion.objects.filter(name=name)
    stored = (
        versions.get(version=version)
        if version is not None
        else versions.order_by("-version").first()
    )
    if stored is None:
        raise FormulaVersion.DoesNotExist(f"No formula version named {name!r}")
    engine = EngineFormula.from_plain(
        {
            "name": stored.name,
            "version": stored.version,
            "config": stored.config,
            "quirks": stored.quirks,
            "notes": stored.notes,
        }
    )
    return stored, engine


def latest_complete_run(as_of_date: date | None = None) -> ScoreRun | None:
    """The run readers should use: the latest completed one, optionally for a given day."""
    runs = ScoreRun.objects.filter(status=ScoreRun.Status.COMPLETE)
    if as_of_date is not None:
        runs = runs.filter(as_of_date=as_of_date)
    return runs.order_by("-as_of_date", "-id").first()


def run_scoring(
    as_of_date: date,
    *,
    formula_name: str = "parity",
    formula_version: int | None = None,
    finviz_as_of_date: date | None = None,
    now: datetime | None = None,
    pipeline_run: PipelineRun | None = None,
) -> ScoreRun:
    """Score ``as_of_date`` and return the completed run. Raises ``ScoreRunFailed`` on error."""
    stored, engine = load_formula(formula_name, formula_version)
    finviz_day = finviz_as_of_date or as_of_date
    scored_at = now or stage_a_time(as_of_date)
    if pipeline_run is None:
        pipeline_run = (
            PipelineRun.objects.filter(stage=PipelineRun.Stage.MARKET, as_of_date=as_of_date)
            .order_by("-id")
            .first()
        )
    try:
        with transaction.atomic():
            return _score(as_of_date, finviz_day, scored_at, stored, engine, pipeline_run)
    except Exception as exc:
        failed = ScoreRun.objects.create(
            pipeline_run=pipeline_run,
            as_of_date=as_of_date,
            scored_at=scored_at,
            formula_version=stored,
            finviz_as_of_date=finviz_day,
            status=ScoreRun.Status.FAILED,
            error_text=f"{type(exc).__name__}: {exc}",
        )
        raise ScoreRunFailed(failed, exc) from exc


def _score(
    as_of_date: date,
    finviz_day: date,
    scored_at: datetime,
    stored: FormulaVersion,
    engine: EngineFormula,
    pipeline_run: PipelineRun | None,
) -> ScoreRun:
    inputs, snapshots = _load_inputs(as_of_date, finviz_day)
    if not inputs:
        raise ValueError(f"No Finviz snapshots for {finviz_day}")
    run = ScoreRun.objects.create(
        pipeline_run=pipeline_run,
        as_of_date=as_of_date,
        scored_at=scored_at,
        formula_version=stored,
        finviz_as_of_date=finviz_day,
        universe_n=len(inputs),
        status=ScoreRun.Status.RUNNING,
    )
    universe = score_universe_with_stats(list(inputs.values()), scored_at, engine)
    rows = [
        _result_row(run, company_id, inputs[company_id], universe.scores[company_id], links)
        for company_id, links in snapshots.items()
    ]
    for start in range(0, len(rows), BATCH_SIZE):
        ScoreResult.objects.bulk_create(rows[start : start + BATCH_SIZE])
    run.scored_n = len(rows)
    run.peer_group_stats = [_plain(stat) for stat in universe.group_stats]
    run.status = ScoreRun.Status.COMPLETE
    run.save(update_fields=["scored_n", "peer_group_stats", "status"])
    ScoreRun.objects.filter(as_of_date=as_of_date, status=ScoreRun.Status.COMPLETE).exclude(
        pk=run.pk
    ).update(status=ScoreRun.Status.SUPERSEDED)
    return run


@dataclasses.dataclass(frozen=True)
class _Links:
    company_pk: int
    runway: RunwayCalc | None
    catalyst: CatalystSignal | None


def _load_inputs(
    as_of_date: date, finviz_day: date
) -> tuple[dict[str, CompanyInputs], dict[str, _Links]]:
    snapshots = list(FinvizSnapshot.objects.filter(as_of_date=finviz_day).select_related("company"))
    company_ids = [s.company_id for s in snapshots]
    runways: dict[int, RunwayCalc] = {}
    for runway in RunwayCalc.objects.filter(
        company_id__in=company_ids, calc_date__lte=as_of_date
    ).order_by("calc_date", "id"):
        runways[runway.company_id] = runway
    catalysts: dict[int, CatalystSignal] = {}
    for signal in CatalystSignal.objects.filter(
        company_id__in=company_ids, as_of_date__lte=as_of_date
    ).order_by("as_of_date", "id"):
        catalysts[signal.company_id] = signal

    inputs: dict[str, CompanyInputs] = {}
    links: dict[str, _Links] = {}
    for snap in sorted(snapshots, key=lambda s: s.company_id):
        key = str(snap.company_id)
        latest_runway = runways.get(snap.company_id)
        latest_catalyst = catalysts.get(snap.company_id)
        inputs[key] = _company_inputs(key, snap, latest_runway, latest_catalyst)
        links[key] = _Links(snap.company_id, latest_runway, latest_catalyst)
    return inputs, links


def _company_inputs(
    key: str,
    snap: FinvizSnapshot,
    runway: RunwayCalc | None,
    catalyst: CatalystSignal | None,
) -> CompanyInputs:
    return CompanyInputs(
        company_id=key,
        industry=snap.industry_raw or snap.company.industry or None,
        mcap=snap.mcap_musd,
        peer_metrics={name: getattr(snap, name) for name in DIMENSIONS},
        quick_ratio=snap.quick_ratio,
        cash_per_share=snap.cash_sh,
        price=snap.price,
        debt_to_equity=snap.td_eq,
        price_to_cash=snap.pcash,
        runway_months=runway.runway_months if runway else None,
        runway_balance_sheet_date=(
            runway.cash_end_date.isoformat() if runway and runway.cash_end_date else None
        ),
        clinical_date_text=(
            catalyst.clinical_date.isoformat() if catalyst and catalyst.clinical_date else None
        ),
        clinical_overdue=bool(catalyst and catalyst.clinical_overdue),
        clinical_phase=(catalyst.clinical_phase or None) if catalyst else None,
        active_phase_3=catalyst.active_phase_3 if catalyst else None,
        active_phase_2=catalyst.active_phase_2 if catalyst else None,
        next_completion_text=(catalyst.fallback_end_raw or None) if catalyst else None,
        latest_trial_status=(catalyst.latest_trial_status or None) if catalyst else None,
        earnings_date_text=snap.earnings_date_text or None,
        eps_next=snap.eps_next,
        rel_vol=snap.rel_vol,
        eps_this=snap.eps_this,
    )


def _result_row(
    run: ScoreRun, key: str, inputs: CompanyInputs, scores: CompanyScores, links: _Links
) -> ScoreResult:
    topology = scores.topology
    return ScoreResult(
        score_run=run,
        company_id=links.company_pk,
        mcap_bucket=topology.mcap_bucket,
        peer_group_key=topology.peer_group_key,
        peer_n=topology.peer_n,
        is_sparse=topology.is_sparse,
        cash_health=scores.cash_health.score,
        milestone=scores.milestone.score,
        outlier=topology.outlier,
        divergence=topology.divergence,
        recency=topology.recency,
        composite=scores.composite.score,
        runway_calc=links.runway,
        catalyst_signal=links.catalyst,
        carried_inputs=[],
        missing_inputs=_missing(inputs),
        inputs=_plain(inputs),
        components={
            "topology": _plain(topology),
            "cash_health": _plain(scores.cash_health),
            "milestone": _plain(scores.milestone),
            "composite": _plain(scores.composite),
            "runway_used": scores.runway_used,
        },
    )


def _missing(inputs: CompanyInputs) -> list[str]:
    names = [name for name in TRACKED_INPUTS if getattr(inputs, name) is None]
    names += [f"peer.{name}" for name in DIMENSIONS if inputs.peer_metrics.get(name) is None]
    return names


def _plain(value: Any) -> Any:
    """JSON-safe form of scores and inputs: dataclasses to dicts, dates to ISO text."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
