"""Run screens over a stored score run and keep the funnel log.

* Ad-hoc screens (any filters) are computed live from the stored scores and the Finviz snapshot
  the run scored. Nothing is recomputed and nothing is written.
* The default screen is materialised once per score run: a ``ScreenRun`` with one
  ``ScreenMember`` per company (passed or excluded, with every failed filter).
* ``FunnelEvent`` rows record who entered or left the default list since the previous day.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.companies.models import TickerHistory
from apps.marketdata.models import FinvizSnapshot
from apps.scoring.models import ScoreResult, ScoreRun
from apps.screener.filters import ScreenConfig, ScreenOutcome, ScreenRow, screen
from apps.screener.models import FunnelEvent, ScreenDefinition, ScreenMember, ScreenRun

PASSED = "passed"
EXCLUDED = "excluded"
ABSENT = "absent"  # not in that day's universe
NEW_TO_UNIVERSE = "new_to_universe"
LEFT_UNIVERSE = "left_universe"
PASSED_ALL = "passed_all_filters"


def load_rows(score_run: ScoreRun) -> list[ScreenRow]:
    """The scored universe of a run, joined to the Finviz fields the filters use."""
    snapshots: dict[int, dict[str, Any]] = {
        s["company_id"]: dict(s)
        for s in FinvizSnapshot.objects.filter(as_of_date=score_run.finviz_as_of_date).values(
            "company_id", "mcap_musd", "price", "avg_vol_k", "shr_out_m"
        )
    }
    tickers = dict(
        TickerHistory.objects.filter(valid_to__isnull=True).values_list("company_id", "ticker")
    )
    rows = []
    for result in ScoreResult.objects.filter(score_run=score_run).values(
        "company_id", "composite", "outlier", "is_sparse"
    ):
        snap = snapshots.get(result["company_id"], {})
        rows.append(
            ScreenRow(
                company_id=result["company_id"],
                ticker=tickers.get(result["company_id"], str(result["company_id"])),
                composite=result["composite"],
                outlier=result["outlier"],
                is_sparse=result["is_sparse"],
                mcap_musd=snap.get("mcap_musd"),
                price=snap.get("price"),
                avg_vol_k=snap.get("avg_vol_k"),
                shr_out_m=snap.get("shr_out_m"),
            )
        )
    return rows


def adhoc_screen(
    score_run: ScoreRun,
    config: ScreenConfig,
    rows: list[ScreenRow] | None = None,
) -> list[ScreenOutcome]:
    """Screen a run with any filters. Pass ``rows`` to reuse one load across many filter sets."""
    return screen(rows if rows is not None else load_rows(score_run), config)


def default_definition() -> ScreenDefinition:
    return ScreenDefinition.objects.get(is_default=True)


@transaction.atomic
def materialise_default_screen(score_run: ScoreRun) -> ScreenRun:
    """Store the default screen for a run and write the day's funnel events.

    Running it again for the same score run replaces that run's screen and its funnel events.
    """
    definition = default_definition()
    config = ScreenConfig.from_plain(definition.config)
    outcomes = screen(load_rows(score_run), config)

    ScreenRun.objects.filter(score_run=score_run, definition=definition).delete()
    run = ScreenRun.objects.create(
        score_run=score_run,
        definition=definition,
        filter_config=config.to_plain(),
        universe_n=len(outcomes),
        passed_n=sum(1 for o in outcomes if o.passed),
        executed_at=timezone.now(),
    )
    ScreenMember.objects.bulk_create(
        [
            ScreenMember(
                screen_run=run,
                company_id=o.row.company_id,
                passed=o.passed,
                excluded_reasons=list(o.reasons),
                rank=o.rank,
            )
            for o in outcomes
        ],
        batch_size=500,
    )
    _write_funnel_events(run, outcomes)
    return run


def previous_screen_run(run: ScreenRun) -> ScreenRun | None:
    """The default screen of the latest earlier day."""
    return (
        ScreenRun.objects.filter(
            definition=run.definition, score_run__as_of_date__lt=run.score_run.as_of_date
        )
        .order_by("-score_run__as_of_date", "-id")
        .first()
    )


def _write_funnel_events(run: ScreenRun, outcomes: Iterable[ScreenOutcome]) -> None:
    day = run.score_run.as_of_date
    FunnelEvent.objects.filter(as_of_date=day).delete()
    previous = previous_screen_run(run)
    if previous is None:
        return  # the first day has nothing to compare with
    before = {m.company_id: m for m in ScreenMember.objects.filter(screen_run=previous)}
    today = {o.row.company_id: o for o in outcomes}
    events: list[FunnelEvent] = []

    for company_id, outcome in today.items():
        was = before.get(company_id)
        was_state = ABSENT if was is None else PASSED if was.passed else EXCLUDED
        now_state = PASSED if outcome.passed else EXCLUDED
        if was_state == now_state or PASSED not in (was_state, now_state):
            continue
        if now_state == PASSED:
            reason = NEW_TO_UNIVERSE if was is None else PASSED_ALL
        else:
            reason = ",".join(outcome.reasons)
        events.append(
            FunnelEvent(
                company_id=company_id,
                as_of_date=day,
                from_state=was_state,
                to_state=now_state,
                reason=reason,
            )
        )
    for company_id, was in before.items():
        if company_id not in today and was.passed:
            events.append(
                FunnelEvent(
                    company_id=company_id,
                    as_of_date=day,
                    from_state=PASSED,
                    to_state=ABSENT,
                    reason=LEFT_UNIVERSE,
                )
            )
    FunnelEvent.objects.bulk_create(events, batch_size=500)
