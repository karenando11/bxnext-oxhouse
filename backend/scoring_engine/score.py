"""Score a whole universe from raw inputs under a formula version.

This is the pure-Python core of the score run: Outlier, Divergence and Recency over the whole
universe, then Cash Health, Milestone and Composite per company, all driven by one
``FormulaVersion``. Step 7 wraps it with the database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from scoring_engine.cash_health import (
    CashHealthInput,
    CashHealthResult,
    cash_health,
    parse_clinical_date,
    parse_completion_date,
)
from scoring_engine.composite import CompositeResult, composite
from scoring_engine.effective_runway import effective_runway_months
from scoring_engine.formula import FormulaVersion
from scoring_engine.milestone import (
    MilestoneInput,
    MilestoneResult,
    milestone,
    parse_earnings_date,
)
from scoring_engine.topology import (
    CompanyPeerInput,
    PeerGroupStat,
    TopologyResult,
    compute_topology,
)

MARKET_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CompanyInputs:
    """Everything the scores need for one company. ``None`` and empty text mean missing.

    Dates arrive as the text the sources provide; month-only values are interpreted by each
    score as the existing system does.
    """

    company_id: str
    industry: str | None
    mcap: float | None  # USD millions
    peer_metrics: Mapping[str, float | None] = field(default_factory=dict)

    quick_ratio: float | None = None
    cash_per_share: float | None = None
    price: float | None = None
    debt_to_equity: float | None = None
    price_to_cash: float | None = None

    runway_months: float | None = None
    runway_balance_sheet_date: str | None = None  # ISO date

    clinical_date_text: str | None = None
    clinical_overdue: bool = False
    clinical_phase: str | None = None
    active_phase_3: float | None = None
    active_phase_2: float | None = None
    next_completion_text: str | None = None
    latest_completion_text: str | None = None
    latest_trial_status: str | None = None

    earnings_date_text: str | None = None
    eps_next: float | None = None
    rel_vol: float | None = None
    eps_this: float | None = None


@dataclass(frozen=True)
class CompanyScores:
    company_id: str
    topology: TopologyResult
    cash_health: CashHealthResult
    milestone: MilestoneResult
    composite: CompositeResult
    runway_used: float | None  # the runway the ceiling and boost saw


@dataclass(frozen=True)
class UniverseScores:
    scores: dict[str, CompanyScores]
    group_stats: tuple[PeerGroupStat, ...]  # median, MAD and count per peer group and dimension


def score_universe(
    companies: Sequence[CompanyInputs], now: datetime, formula: FormulaVersion
) -> dict[str, CompanyScores]:
    """Score every company. Pass the whole universe; peer statistics depend on every member."""
    return score_universe_with_stats(companies, now, formula).scores


def score_universe_with_stats(
    companies: Sequence[CompanyInputs], now: datetime, formula: FormulaVersion
) -> UniverseScores:
    """Like ``score_universe``, and also returns the peer group statistics used."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(MARKET_TIMEZONE).date()
    config, quirks = formula.config, formula.quirks

    topology_output = compute_topology(
        [CompanyPeerInput(c.company_id, c.industry, c.mcap, c.peer_metrics) for c in companies],
        config.topology,
    )
    topology = topology_output.results

    scores: dict[str, CompanyScores] = {}
    for c in companies:
        balance_sheet_date = parse_completion_date(c.runway_balance_sheet_date)
        runway = effective_runway_months(
            c.runway_months,
            balance_sheet_date,
            today,
            runway_as_filed=quirks.runway_as_filed,
            config=config.runway,
        )
        active = (c.active_phase_3 or 0) + (c.active_phase_2 or 0)
        cash = cash_health(
            CashHealthInput(
                quick_ratio=c.quick_ratio,
                cash_per_share=c.cash_per_share,
                price=c.price,
                debt_to_equity=c.debt_to_equity,
                price_to_cash=c.price_to_cash,
                runway_months=runway,
                clinical_date=parse_clinical_date(c.clinical_date_text),
                fallback_completion_date=parse_completion_date(
                    c.next_completion_text or c.latest_completion_text
                ),
                has_active_late_stage_trial=active > 0,
            ),
            today,
            config.cash_health,
        )
        stage = milestone(
            MilestoneInput(
                clinical_date=parse_completion_date(c.clinical_date_text),
                clinical_overdue=c.clinical_overdue,
                clinical_phase=c.clinical_phase,
                active_phase_3=c.active_phase_3,
                active_phase_2=c.active_phase_2,
                next_completion_date=parse_completion_date(c.next_completion_text),
                latest_completion_date=parse_completion_date(c.latest_completion_text),
                latest_trial_status=c.latest_trial_status,
                earnings_date=parse_earnings_date(c.earnings_date_text),
                eps_next=c.eps_next,
                rel_vol=c.rel_vol,
                eps_this=c.eps_this,
                runway_months=runway,
            ),
            now,
            config.milestone,
            earnings_past_is_imminent=quirks.earnings_past_is_imminent,
        )
        peers = topology[c.company_id]
        blend = composite(
            cash.score,
            stage.score,
            peers.outlier,
            peers.recency,
            peers.divergence,
            config.composite,
        )
        scores[c.company_id] = CompanyScores(c.company_id, peers, cash, stage, blend, runway)
    return UniverseScores(scores, topology_output.group_stats)
