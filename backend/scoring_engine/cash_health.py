"""Cash Health: how well funded a company is, adjusted for how soon a catalyst is due.

Pure Python. No Django, database, network or clock: the caller passes ``now`` explicitly.

How it works
------------
1. Each available balance-sheet term (quick ratio, cash per share over price, total debt to
   equity) is bucketed to a score from 0 to 10 and the terms are averaged with fixed weights.
   Missing terms are skipped, never treated as zero. When the available weight is below
   ``pcash_fallback_below``, a price-to-cash term is added if it is present. With no usable
   term the base score is ``no_data_score``.
2. A runway ceiling caps the base score for companies that are close to running out of cash.
3. The capped score is multiplied by a catalyst dampener: a distant catalyst means the
   market has little reason to look at the company soon, so the score is reduced. The date
   comes from the clinical catalyst, else the trial completion fallback date.

Stored values are never rounded; use ``display_score("cash_health", ...)`` for display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

# (minimum value, score); first row whose minimum is met wins. Used for "higher is better".
_QUICK_RATIO = ((5.0, 10.0), (3.0, 8.0), (1.5, 6.0), (1.0, 4.0), (0.5, 2.0))
_CASH_TO_PRICE = ((0.8, 10.0), (0.5, 8.0), (0.3, 6.0), (0.1, 4.0))
# (maximum value, score); first row whose maximum is met wins. Used for "lower is better".
_DEBT_TO_EQUITY = ((0.1, 10.0), (0.3, 8.0), (0.5, 6.0), (1.0, 4.0), (2.0, 2.0))
_PRICE_TO_CASH = ((2.0, 9.0), (5.0, 7.0), (10.0, 5.0), (20.0, 3.0))


@dataclass(frozen=True)
class CashHealthConfig:
    """Every constant used by Cash Health. Step 4 loads these from the formula version.

    Validated against the 2026-10-12 golden export. Not pinned by it: the 90-day lapse limit
    is pinned only between 60 and 120 days of tested alternatives, and negative inputs do not
    occur in the data that decides a bucket.
    """

    quick_ratio_weight: float = 1.5
    cash_to_price_weight: float = 1.0
    debt_to_equity_weight: float = 0.8
    pcash_weight: float = 1.0
    pcash_fallback_below: float = 1.5
    no_data_score: float = 5.0

    quick_ratio_buckets: tuple[tuple[float, float], ...] = _QUICK_RATIO
    quick_ratio_floor_score: float = 0.0
    cash_to_price_buckets: tuple[tuple[float, float], ...] = _CASH_TO_PRICE
    cash_to_price_floor_score: float = 2.0
    debt_to_equity_buckets: tuple[tuple[float, float], ...] = _DEBT_TO_EQUITY
    debt_to_equity_ceiling_score: float = 0.0
    pcash_buckets: tuple[tuple[float, float], ...] = _PRICE_TO_CASH
    pcash_ceiling_score: float = 1.0

    # Runway ceiling: (runway months below, cap). Longer runways are not capped.
    runway_caps: tuple[tuple[float, float], ...] = ((6.0, 3.0), (12.0, 5.0), (18.0, 7.0))

    days_per_month: float = 30.0
    # Dampener bands: (months above, factor), checked in order; otherwise ``factor_near``.
    dampener_bands: tuple[tuple[float, float], ...] = ((24.0, 0.5), (18.0, 0.65), (12.0, 0.8))
    factor_near: float = 1.0
    fallback_lapse_days: int = 90
    factor_active_late_stage: float = 1.0
    factor_no_catalyst: float = 0.5


DEFAULT_CONFIG = CashHealthConfig()


@dataclass(frozen=True)
class CashHealthInput:
    """Plain inputs for one company. ``None`` means missing."""

    quick_ratio: float | None = None
    cash_per_share: float | None = None
    price: float | None = None
    debt_to_equity: float | None = None
    price_to_cash: float | None = None
    runway_months: float | None = None  # as filed (see the runway quirk in Step 4)
    clinical_date: date | None = None
    fallback_completion_date: date | None = None  # next trial completion, else latest
    has_active_late_stage_trial: bool = False  # an active Phase 2 or Phase 3 trial


@dataclass(frozen=True)
class CashHealthTerm:
    name: str
    value: float
    score: float
    weight: float


@dataclass(frozen=True)
class CashHealthResult:
    score: float  # unrounded
    base_score: float  # before the runway cap and the dampener
    terms: tuple[CashHealthTerm, ...]
    used_pcash_fallback: bool
    runway_cap: float | None  # the cap that applied to the runway, None when uncapped
    capped_score: float  # base score after the runway ceiling
    dampener_factor: float
    dampener_reason: str  # clinical_date, fallback_date, active_late_stage, no_catalyst
    dampener_date: date | None
    months_to_date: float | None  # 30-day months, overdue counts as 0


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _higher_is_better(
    value: float, buckets: tuple[tuple[float, float], ...], floor_score: float
) -> float:
    for minimum, score in buckets:
        if value >= minimum:
            return score
    return floor_score


def _lower_is_better(
    value: float, buckets: tuple[tuple[float, float], ...], ceiling_score: float
) -> float:
    for maximum, score in buckets:
        if value <= maximum:
            return score
    return ceiling_score


def _terms(data: CashHealthInput, config: CashHealthConfig) -> list[CashHealthTerm]:
    terms: list[CashHealthTerm] = []
    quick = _finite(data.quick_ratio)
    if quick is not None:
        terms.append(
            CashHealthTerm(
                "quick_ratio",
                quick,
                _higher_is_better(
                    quick, config.quick_ratio_buckets, config.quick_ratio_floor_score
                ),
                config.quick_ratio_weight,
            )
        )
    cash, price = _finite(data.cash_per_share), _finite(data.price)
    if cash is not None and price is not None and price > 0:
        ratio = cash / price
        terms.append(
            CashHealthTerm(
                "cash_to_price",
                ratio,
                _higher_is_better(
                    ratio, config.cash_to_price_buckets, config.cash_to_price_floor_score
                ),
                config.cash_to_price_weight,
            )
        )
    debt = _finite(data.debt_to_equity)
    if debt is not None:
        terms.append(
            CashHealthTerm(
                "debt_to_equity",
                debt,
                _lower_is_better(
                    debt, config.debt_to_equity_buckets, config.debt_to_equity_ceiling_score
                ),
                config.debt_to_equity_weight,
            )
        )
    return terms


def base_score(
    data: CashHealthInput, config: CashHealthConfig = DEFAULT_CONFIG
) -> tuple[float, tuple[CashHealthTerm, ...], bool]:
    """Weighted blend of the balance-sheet terms: (score, terms used, P/Cash fallback used)."""
    terms = _terms(data, config)
    used_fallback = False
    if sum(t.weight for t in terms) < config.pcash_fallback_below:
        pcash = _finite(data.price_to_cash)
        if pcash is not None:
            terms.append(
                CashHealthTerm(
                    "price_to_cash",
                    pcash,
                    _lower_is_better(pcash, config.pcash_buckets, config.pcash_ceiling_score),
                    config.pcash_weight,
                )
            )
            used_fallback = True
    if not terms:
        return config.no_data_score, (), False
    total_weight = sum(t.weight for t in terms)
    return sum(t.score * t.weight for t in terms) / total_weight, tuple(terms), used_fallback


def runway_cap(
    runway_months: float | None, config: CashHealthConfig = DEFAULT_CONFIG
) -> float | None:
    """The ceiling the runway puts on the base score, or None when it does not cap."""
    months = _finite(runway_months)
    if months is None:
        return None
    for below, cap in config.runway_caps:
        if months < below:
            return cap
    return None


def dampener_factor_for_months(months: float, config: CashHealthConfig = DEFAULT_CONFIG) -> float:
    for above, factor in config.dampener_bands:
        if months > above:
            return factor
    return config.factor_near


def _months_until(target: date, now: date, config: CashHealthConfig) -> float:
    return max((target - now).days / config.days_per_month, 0.0)


def catalyst_dampener(
    data: CashHealthInput, now: date, config: CashHealthConfig = DEFAULT_CONFIG
) -> tuple[float, str, date | None, float | None]:
    """(factor, reason, date used, months to that date)."""
    if data.clinical_date is not None:
        months = _months_until(data.clinical_date, now, config)
        return (
            dampener_factor_for_months(months, config),
            "clinical_date",
            data.clinical_date,
            months,
        )
    fallback = data.fallback_completion_date
    if fallback is not None and (now - fallback).days <= config.fallback_lapse_days:
        months = _months_until(fallback, now, config)
        return dampener_factor_for_months(months, config), "fallback_date", fallback, months
    if data.has_active_late_stage_trial:
        return config.factor_active_late_stage, "active_late_stage", None, None
    return config.factor_no_catalyst, "no_catalyst", None, None


def cash_health(
    data: CashHealthInput, now: date, config: CashHealthConfig = DEFAULT_CONFIG
) -> CashHealthResult:
    """Cash Health for one company at ``now`` (unrounded, with every component)."""
    base, terms, used_fallback = base_score(data, config)
    cap = runway_cap(data.runway_months, config)
    capped = base if cap is None else min(base, cap)
    factor, reason, used_date, months = catalyst_dampener(data, now, config)
    return CashHealthResult(
        score=capped * factor,
        base_score=base,
        terms=terms,
        used_pcash_fallback=used_fallback,
        runway_cap=cap,
        capped_score=capped,
        dampener_factor=factor,
        dampener_reason=reason,
        dampener_date=used_date,
        months_to_date=months,
    )


def parse_clinical_date(text: str | None) -> date | None:
    """A clinical catalyst date: ``YYYY-MM-DD``, or ``YYYY-MM`` meaning the 15th."""
    return _parse_partial_date(text, month_only_day=15)


def parse_completion_date(text: str | None) -> date | None:
    """A trial completion date: ``YYYY-MM-DD``, or ``YYYY-MM`` meaning the 1st."""
    return _parse_partial_date(text, month_only_day=1)


def _parse_partial_date(text: str | None, month_only_day: int) -> date | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    parts = cleaned.split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), month_only_day)
    except ValueError:
        return None
    return None
