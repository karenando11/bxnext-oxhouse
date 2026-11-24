"""The company page as data (Step 18): blocks, the 55 fields, banners and vintages.

``build_page`` takes one ``company_page`` row, the company's stored studies and a ``today``
value. It reads nothing else and has no hidden clock, so every banner is testable.

The page has nine blocks (header, metric tiles, scores, runway, catalyst, trials, short selling,
insider activity, footer) plus ``fields``: all 55 fields of the field list, each with a value or a
reason code for its absence, so a consumer can render any field without knowing which block holds
it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from apps.platform.calendar import trading_days_between
from apps.platform.golden import earnings_date
from apps.sec.runway import runway_display
from apps.trials.display import Study, TrialPanel, trial_panel
from apps.web import format as fmt
from apps.web.models import CompanyPage
from scoring_engine import display_score

MARKET_STALE_TRADING_DAYS = 3
CASH_STALE_DAYS = 120
SHORT_VOLUME_LABEL = "short volume, not short interest"
NOT_YET_AVAILABLE = {"status": "not_yet_available", "value": None}

# Reasons for a missing value that no collector stores (the source simply left it blank).
NOT_REPORTED = "not_reported"
NOT_AVAILABLE = "not_available"
NO_SALES = "no_open_market_sales"


def plain(value: Any) -> Any:
    """JSON-safe: Decimal to float, date to ISO text."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


# ------------------------------------------------------------------------------ the 55 fields


@dataclass(frozen=True)
class PageField:
    n: int
    key: str
    label: str
    group: str  # where a reason comes from when the value is missing
    value: Callable[[CompanyPage, Derived], Any]


@dataclass(frozen=True)
class Derived:
    trials: TrialPanel
    runway_quarters: float | None
    runway_rating: str
    studies: Sequence[Study]


def _attr(name: str) -> Callable[[CompanyPage, Derived], Any]:
    return lambda row, _d: plain(getattr(row, name))


FIELDS: tuple[PageField, ...] = (
    PageField(1, "ticker", "Ticker", "identity", _attr("ticker")),
    PageField(2, "company", "Company", "identity", _attr("company_name")),
    PageField(3, "market_cap", "Market cap", "market", _attr("mcap_musd")),
    PageField(
        4,
        "price_chart",
        "Price chart",
        "chart",
        lambda r, _d: {"provider": "tradingview", "symbol": r.ticker},
    ),  # fmt: skip
    PageField(5, "cash_cat_score", "Cash + Cat. Score", "scores", _attr("composite")),
    PageField(6, "spring", "Spring", "scores", _attr("divergence")),
    PageField(7, "setup", "Setup", "scores", _attr("cash_health")),
    PageField(8, "timing", "Timing", "scores", _attr("milestone")),
    PageField(9, "outlier", "Outlier", "scores", _attr("outlier")),
    PageField(10, "recency", "Recency (feeds composite, not shown)", "scores", _attr("recency")),
    PageField(11, "price", "Price", "market", _attr("price")),
    PageField(12, "market_cap_tile", "Market cap", "market", _attr("mcap_musd")),
    PageField(13, "vs_52w_high", "vs 52W high", "market", _attr("w52h")),
    PageField(14, "vs_200d_ma", "vs 200D MA", "market", _attr("sma200")),
    PageField(
        15,
        "short_ratio_latest",
        "Latest short ratio",
        "short_volume",
        _attr("short_volume_latest_ratio"),
    ),  # fmt: skip
    PageField(16, "short_ratio_trend", "Trend", "short_volume", _attr("short_volume_trend")),
    PageField(
        17,
        "short_ratio_delta_vs_30d",
        "Δ vs 30d baseline",
        "short_volume",
        _attr("short_volume_delta_pts"),
    ),  # fmt: skip
    PageField(18, "rsi_14", "RSI (14)", "market", _attr("rsi")),
    PageField(19, "avg_volume", "Avg vol", "market", _attr("avg_vol_k")),
    PageField(20, "rel_volume", "Rel vol", "market", _attr("rel_vol")),
    PageField(21, "perf_week", "Week", "market", _attr("perf_w")),
    PageField(22, "perf_month", "Month", "market", _attr("perf_m")),
    PageField(23, "perf_quarter", "Quarter", "market", _attr("perf_q")),
    PageField(24, "perf_ytd", "YTD", "market", _attr("perf_ytd")),
    PageField(
        25, "runway_quarters", "Runway (quarters)", "runway", lambda _r, d: d.runway_quarters
    ),
    PageField(26, "runway_months", "Runway months", "runway", _attr("runway_months")),
    PageField(
        27, "runway_rating", "Runway rating", "runway", lambda _r, d: d.runway_rating or None
    ),
    PageField(28, "cash_per_share", "Cash/share", "market", _attr("cash_sh")),
    PageField(29, "quick_ratio", "Quick ratio", "market", _attr("quick_ratio")),
    PageField(30, "debt_to_equity", "Debt/equity", "market", _attr("td_eq")),
    PageField(31, "inst_transactions", "Inst. transactions", "market", _attr("inst_trans")),
    PageField(32, "short_float", "Short float", "market", _attr("short_float")),
    PageField(33, "insider_ownership", "Insider own", "market", _attr("insider_own")),
    PageField(
        34, "insider_filings_90d", "Filings (90d)", "insider", _attr("insider_filings_count")
    ),
    PageField(
        35, "insider_open_market_buys", "Open-market BUYS", "insider", _attr("insider_buys_count")
    ),
    PageField(
        36,
        "insider_open_market_sales",
        "Open-market SALES",
        "insider",
        _attr("insider_sales_count"),
    ),
    PageField(
        37,
        "insider_officers_directors_buying",
        "Officers/dirs BUYING",
        "insider",
        _attr("insider_officers_directors_buying"),
    ),  # fmt: skip
    PageField(
        38,
        "insider_officers_directors_selling",
        "Officers/dirs SELLING",
        "insider",
        _attr("insider_officers_directors_selling"),
    ),  # fmt: skip
    PageField(
        39, "insider_pct_10b5_1", "% under 10b5-1", "insider", _attr("insider_pct_10b5_1_dollars")
    ),
    PageField(40, "gross_margin", "Gross margin", "market", _attr("gross_margin")),
    PageField(41, "op_margin", "Op margin", "market", _attr("op_margin")),
    PageField(42, "industry", "Industry", "identity", _attr("industry")),
    PageField(43, "mcap_bucket", "MCap bucket", "scores", _attr("mcap_bucket")),
    PageField(44, "peer_group", "Peer group", "scores", _attr("peer_group_key")),
    PageField(45, "peer_count", "Peer count", "scores", _attr("peer_n")),
    PageField(46, "earnings_date", "Earnings date", "market", _attr("earnings_date_text")),
    PageField(47, "total_studies", "Total studies", "catalyst", _attr("total_studies")),
    PageField(48, "active_phase_3", "Active Phase 3", "catalyst", _attr("active_phase_3")),
    PageField(49, "active_phase_2", "Active Phase 2", "catalyst", _attr("active_phase_2")),
    PageField(50, "latest_trial", "Latest trial", "catalyst", _attr("latest_trial_nct")),
    PageField(51, "latest_status", "Latest status", "catalyst", _attr("latest_trial_status")),
    PageField(
        52,
        "next_milestone",
        "Next Milestone card",
        "trial_lists",
        lambda _r, d: _milestone_card(d.trials),
    ),  # fmt: skip
    PageField(
        53,
        "trial_pipeline",
        "Trial Pipeline total / phase chips",
        "trial_lists",
        lambda _r, d: _pipeline(d),
    ),  # fmt: skip
    PageField(
        54, "indications", "Indications", "trial_lists", lambda _r, d: d.trials.indications or None
    ),  # fmt: skip
    PageField(
        55,
        "upcoming_completions",
        "Upcoming completions",
        "trial_lists",
        lambda _r, d: [_upcoming(u) for u in d.trials.upcoming] or None,
    ),  # fmt: skip
)
assert [f.n for f in FIELDS] == list(range(1, 56))


def _group_reasons(row: CompanyPage, group: str, derived: Derived) -> list[str]:
    meta: dict[str, Any] = row.field_meta or {}
    resolution: dict[str, Any] = row.score_input_resolution or {}
    if group == "market":
        return list((meta.get("market") or {}).get("reasons") or []) or [NOT_REPORTED]
    if group == "runway":
        reasons = list((meta.get("runway") or {}).get("reasons") or [])
        fallback = (resolution.get("runway") or {}).get("reason")
        return reasons or ([fallback] if fallback else []) or ["cash_missing"]
    if group == "catalyst":
        reasons = list((meta.get("catalyst") or {}).get("reasons") or [])
        return reasons or ([] if row.total_studies else ["no_trials"]) or [NOT_AVAILABLE]
    if group == "trial_lists":
        return (
            derived.trials.reason_codes
            or list((meta.get("trial_lists") or {}).get("reasons") or [])
            or [NOT_AVAILABLE]
        )
    if group in ("short_volume", "insider"):
        reasons = list((meta.get(group) or {}).get("reasons") or [])
        if not reasons and group == "insider" and row.insider_sales_count == 0:
            return [NO_SALES]  # no open-market sales in the window: no share to report
        return reasons or [NOT_AVAILABLE]
    return [NOT_AVAILABLE]


def build_fields(row: CompanyPage, derived: Derived) -> dict[str, dict[str, Any]]:
    """All 55 fields: each has a ``value`` or a ``reason`` (never neither)."""
    out: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        value = field.value(row, derived)
        out[field.key] = {
            "n": field.n,
            "label": field.label,
            "value": value,
            "reason": None if value is not None else _group_reasons(row, field.group, derived)[0],
        }
    return out


# ------------------------------------------------------------------------------------ blocks


def _milestone_card(panel: TrialPanel) -> dict[str, Any] | None:
    study = panel.next_milestone
    if study is None:
        return None
    return {
        "nct_id": study.nct_id,
        "title": study.title,
        "phase": study.highest_phase,
        "status": study.status,
        "date": plain(study.milestone_date),
        "quarters_out": panel.quarters_out,
        "drug": panel.drug,
    }


def _pipeline(derived: Derived) -> dict[str, Any] | None:
    panel = derived.trials
    if panel.pipeline_total == 0:
        return None
    return {
        "total": panel.pipeline_total,
        "phase_chips": panel.phase_chips,
        "studies": [
            {
                "nct_id": s.nct_id,
                "title": s.title,
                "phase": s.highest_phase,
                "status": s.status,
                "date": plain(s.milestone_date),
                "conditions": list(s.conditions),
            }
            for s in derived.studies
        ],
    }


def _upcoming(item: Any) -> dict[str, Any]:
    return {"nct_id": item.nct_id, "date": plain(item.completion), "phase": item.highest_phase}


def _tile(value: float | None, display: str | None) -> dict[str, Any]:
    return {"value": value, "display": display}


def _scores(row: CompanyPage) -> dict[str, Any]:
    def score(name: str, label: str, value: float) -> dict[str, Any]:
        return {"label": label, "value": value, "display": display_score(name, value)}

    return {
        "composite": score("composite", "Cash + Cat. Score", row.composite),
        "spring": score("divergence", "Spring", row.divergence),
        "setup": score("cash_health", "Setup", row.cash_health),
        "timing": score("milestone", "Timing", row.milestone),
        "outlier": score("outlier", "Outlier", row.outlier),
        "recency": {"label": "Recency", "value": row.recency, "shown": False},
        "components": row.score_components,
        "carried_inputs": list(row.carried_inputs),
        "missing_inputs": list(row.missing_inputs),
        "input_resolution": row.score_input_resolution,
        "scored_at": row.scored_at.isoformat() if row.scored_at else None,
    }


def _runway(row: CompanyPage, reasons: list[str]) -> dict[str, Any]:
    positive = "company_cash_positive" in reasons
    shown = runway_display(
        row.runway_months, capped=bool(row.runway_capped), cash_flow_positive=positive
    )
    return {
        "months": row.runway_months,
        "months_label": shown.months_label or None,
        "quarters": shown.quarters,
        "rating": shown.rating or None,
        "label": shown.label or None,
        "capped": row.runway_capped,
        "cap_reason": row.runway_cap_reason or None,
        "cash_usd": plain(row.runway_cash_usd),
        "cash_source_tags": row.runway_cash_tag or None,
        "operating_burn_monthly_usd": plain(row.runway_operating_burn_usd),
        "trial_burn_monthly_usd": plain(row.runway_trial_burn_usd),
        "burn_total_monthly_usd": plain(row.runway_burn_total_usd),
        "trial_burn_lines": row.runway_trial_burn_lines or [],
        "burn_source": row.runway_burn_source or None,
        "balance_sheet_date": plain(row.runway_balance_sheet_date),
        "calc_date": plain(row.runway_calc_date),
        "financing_date": plain(row.financing_date),
        "status": row.runway_status,
        "reasons": reasons,
        "resolution": (row.score_input_resolution or {}).get("runway"),
    }


def _catalyst(row: CompanyPage, reasons: list[str]) -> dict[str, Any]:
    return {
        "clinical_date": plain(row.clinical_date),
        "phase": row.clinical_phase or None,
        "overdue": row.clinical_overdue,
        "source_nct": row.catalyst_source_nct or None,
        "next_event_source": row.next_event_source,
        "financing_date": plain(row.financing_date),
        "fallback_completion": row.catalyst_fallback_end_raw or None,
        "candidates": row.catalyst_candidates or [],
        "status": row.catalyst_status,
        "as_of_date": plain(row.catalyst_as_of),
        "reasons": reasons,
        "resolution": (row.score_input_resolution or {}).get("catalyst"),
    }


def _trials(row: CompanyPage, derived: Derived) -> dict[str, Any]:
    return {
        "total_studies": row.total_studies,
        "active_phase_3": row.active_phase_3,
        "active_phase_2": row.active_phase_2,
        "latest_trial": row.latest_trial_nct or None,
        "latest_status": row.latest_trial_status or None,
        "next_milestone": _milestone_card(derived.trials),
        "pipeline": _pipeline(derived),
        "indications": derived.trials.indications,
        "upcoming_completions": [_upcoming(u) for u in derived.trials.upcoming],
        "reasons": derived.trials.reason_codes,
    }


# ----------------------------------------------------------------------------- banners, vintage


def banners(row: CompanyPage, today: date) -> list[dict[str, str]]:
    """The stale-data banners that apply. Ages count trading days where the rules say so."""
    meta: dict[str, Any] = row.field_meta or {}
    found: list[dict[str, str]] = []

    def add(code: str, message: str, block: str) -> None:
        found.append({"code": code, "message": message, "block": block})

    earnings = earnings_date(row.earnings_date_text)
    if earnings is not None and earnings < today:
        add("earnings_passed", "Earnings date has passed", "tiles")
    if row.clinical_date is not None and row.clinical_date < today:
        add("catalyst_passed", "Catalyst date has passed", "catalyst")
    sheet = row.runway_balance_sheet_date
    if sheet is not None and (today - sheet).days > CASH_STALE_DAYS:
        add("cash_stale", "Cash data is stale", "runway")
    ages = [
        int(item["age_days"])
        for item in (row.score_input_resolution or {}).values()
        if item.get("state") == "carried" and item.get("age_days") is not None
    ]
    if ages:
        add("carried_input", f"Using data from {max(ages)} days ago", "scores")
    market = (meta.get("market") or {}).get("as_of")
    market_day = date.fromisoformat(market) if market else row.as_of_date
    if trading_days_between(market_day, today) > MARKET_STALE_TRADING_DAYS:
        add("market_stale", "Market data is stale", "header")
    if (meta.get("short_volume") or {}).get(
        "is_stale"
    ) and row.short_volume_latest_ratio is not None:
        add("short_volume_stale", "Short-volume data is stale", "short_selling")
    if (meta.get("insider") or {}).get("is_stale") and row.insider_filings_count is not None:
        add("insider_stale", "Insider data is stale", "insider")
    if row.is_fixture:
        add("fixture", "This page shows fixture data, not a live collection", "header")
    return found


def vintage(row: CompanyPage, today: date) -> list[dict[str, Any]]:
    """Each block's as-of date and source: the footer."""
    meta: dict[str, Any] = row.field_meta or {}
    market = (meta.get("market") or {}).get("as_of")
    short = meta.get("short_volume") or {}
    insider = meta.get("insider") or {}
    return [
        {
            "block": "header",
            "as_of": market or plain(row.as_of_date),
            "source": "Finviz Elite export",
        },
        {"block": "tiles", "as_of": market, "source": "Finviz Elite export"},
        {
            "block": "scores",
            "as_of": plain(row.as_of_date),
            "source": "Computed in-house",
            "produced_at": row.scored_at.isoformat() if row.scored_at else None,
        },  # fmt: skip
        {
            "block": "runway",
            "as_of": plain(row.runway_calc_date),
            "balance_sheet_date": plain(row.runway_balance_sheet_date),
            "source": "SEC EDGAR XBRL company facts",
        },  # fmt: skip
        {
            "block": "catalyst",
            "as_of": plain(row.catalyst_as_of),
            "source": "ClinicalTrials.gov API v2",
        },
        {
            "block": "trials",
            "as_of": plain(row.catalyst_as_of),
            "source": "ClinicalTrials.gov API v2",
        },
        {
            "block": "short_selling",
            "as_of": short.get("latest_trade_date"),
            "age_days": short.get("age_days"),
            "source": "FINRA Reg SHO daily short-volume files",
        },  # fmt: skip
        {
            "block": "insider",
            "as_of": _insider_as_of(insider.get("age_days"), today),
            "age_days": insider.get("age_days"),
            "source": "SEC EDGAR Form 4",
        },  # fmt: skip
    ]


def _insider_as_of(age_days: int | None, today: date) -> str | None:
    """The date of the last successful Form 4 check, from its age."""
    return None if age_days is None else (today - timedelta(days=int(age_days))).isoformat()


# ------------------------------------------------------------------------------------ the page


def build_page(row: CompanyPage, studies: Sequence[Study], today: date) -> dict[str, Any]:
    """The whole company page for one ``company_page`` row."""
    meta: dict[str, Any] = row.field_meta or {}
    panel = trial_panel(studies, row.as_of_date)
    shown = runway_display(
        row.runway_months,
        capped=bool(row.runway_capped),
        cash_flow_positive="company_cash_positive"
        in ((meta.get("runway") or {}).get("reasons") or []),
    )
    derived = Derived(panel, shown.quarters, shown.rating, studies)
    short = meta.get("short_volume") or {}
    insider = meta.get("insider") or {}
    return {
        "ticker": row.ticker,
        "company": row.company_name,
        "as_of_date": plain(row.as_of_date),
        "today": plain(today),
        "is_fixture": row.is_fixture,
        "header": {
            "ticker": row.ticker,
            "company": row.company_name,
            "industry": row.industry,
            "peer_group": {
                "mcap_bucket": row.mcap_bucket,
                "key": row.peer_group_key,
                "peer_count": row.peer_n,
                "is_sparse": row.is_sparse,
            },
            "as_of_date": plain(row.as_of_date),
            "market_data_as_of": (meta.get("market") or {}).get("as_of"),
            "is_fixture": row.is_fixture,
        },
        "chart": {"provider": "tradingview", "symbol": row.ticker},
        "banners": banners(row, today),
        "tiles": {
            "price": _tile(row.price, fmt.usd(row.price)),
            "market_cap": _tile(row.mcap_musd, fmt.market_cap(row.mcap_musd)),
            "vs_52w_high": _tile(row.w52h, fmt.signed_percent(row.w52h)),
            "vs_200d_ma": _tile(row.sma200, fmt.signed_percent(row.sma200)),
            "short_float": _tile(row.short_float, fmt.percent(row.short_float)),
            "inst_ownership": _tile(row.inst_own, fmt.percent(row.inst_own)),
            "cash_per_share": _tile(row.cash_sh, fmt.usd(row.cash_sh)),
            "earnings_date": {"value": row.earnings_date_text, "display": row.earnings_date_text},
            "fair_value": NOT_YET_AVAILABLE,
            "bear_case": NOT_YET_AVAILABLE,
            "bull_case": NOT_YET_AVAILABLE,
            "upside": NOT_YET_AVAILABLE,
        },
        "momentum": {
            "rsi_14": _tile(row.rsi, fmt.number(row.rsi, 1)),
            "avg_volume_k": _tile(row.avg_vol_k, fmt.volume_k(row.avg_vol_k)),
            "rel_volume": _tile(row.rel_vol, fmt.number(row.rel_vol, 3)),
            "perf_week": _tile(row.perf_w, fmt.signed_percent(row.perf_w)),
            "perf_month": _tile(row.perf_m, fmt.signed_percent(row.perf_m)),
            "perf_quarter": _tile(row.perf_q, fmt.signed_percent(row.perf_q)),
            "perf_ytd": _tile(row.perf_ytd, fmt.signed_percent(row.perf_ytd)),
        },
        "balance_sheet": {
            "cash_per_share": _tile(row.cash_sh, fmt.usd(row.cash_sh)),
            "quick_ratio": _tile(row.quick_ratio, fmt.number(row.quick_ratio)),
            "debt_to_equity": _tile(row.td_eq, fmt.number(row.td_eq)),
            "gross_margin": _tile(row.gross_margin, fmt.percent(row.gross_margin)),
            "op_margin": _tile(row.op_margin, fmt.percent(row.op_margin)),
        },
        "ownership": {
            "short_float": _tile(row.short_float, fmt.percent(row.short_float)),
            "inst_ownership": _tile(row.inst_own, fmt.percent(row.inst_own)),
            "inst_transactions": _tile(row.inst_trans, fmt.signed_percent(row.inst_trans)),
            "insider_ownership": _tile(row.insider_own, fmt.percent(row.insider_own)),
        },
        "scores": _scores(row),
        "runway": _runway(row, list((meta.get("runway") or {}).get("reasons") or [])),
        "catalyst": _catalyst(row, list((meta.get("catalyst") or {}).get("reasons") or [])),
        "trials": _trials(row, derived),
        "short_selling": {
            "label": SHORT_VOLUME_LABEL,
            "latest_ratio": plain(row.short_volume_latest_ratio),
            "trend": row.short_volume_trend,
            "delta_vs_30d_pts": row.short_volume_delta_pts,
            "latest_trade_date": short.get("latest_trade_date"),
            "age_days": short.get("age_days"),
            "is_stale": short.get("is_stale"),
            "reasons": list(short.get("reasons") or []),
        },
        "insider": {
            "filings_90d": row.insider_filings_count,
            "open_market_buys": row.insider_buys_count,
            "open_market_sales": row.insider_sales_count,
            "officers_directors_buying": row.insider_officers_directors_buying,
            "officers_directors_selling": row.insider_officers_directors_selling,
            "pct_sale_dollars_under_10b5_1": row.insider_pct_10b5_1_dollars,
            "age_days": insider.get("age_days"),
            "is_stale": insider.get("is_stale"),
            "reasons": list(insider.get("reasons") or []),
        },
        "vintage": vintage(row, today),
        "fields": build_fields(row, derived),
    }
