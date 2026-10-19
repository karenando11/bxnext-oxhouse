"""Screen filters: pure Python with no database access.

A screen narrows the scored universe with adjustable filters. Every company gets an outcome:
passed, or excluded with the reason of *every* failed filter (not only the first). A missing
value in a filtered field excludes the company with the reason ``<field>_missing``; a missing
value is never treated as zero.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

# Reason codes: one per way a filter can fail.
MCAP_MISSING = "mcap_missing"
MCAP_MIN = "mcap_min"
MCAP_MAX = "mcap_max"
PRICE_MISSING = "price_missing"
PRICE_MIN = "price_min"
AVG_VOL_MISSING = "avg_vol_missing"
AVG_VOL_MIN = "avg_vol_min"
SHARES_MISSING = "shares_outstanding_missing"
SHARES_MAX = "shares_outstanding_max"
COMPOSITE_FLOOR = "composite_floor"
OUTLIER_FLOOR = "outlier_floor"
SPARSE_GROUP = "sparse_group"

DEFAULT_NEAR_LINE_MARGIN = 0.5


class ScreenConfigError(ValueError):
    """A screen configuration is invalid; the message names the key."""


@dataclass(frozen=True)
class ScreenConfig:
    """Filters. ``None`` switches a filter off. Units follow the data model: market cap in USD
    millions, average volume in thousands of shares, shares outstanding in millions."""

    mcap_min_musd: float | None = 75.0
    mcap_max_musd: float | None = 1000.0
    price_min: float | None = 1.0
    avg_vol_min_k: float | None = 50.0
    shares_outstanding_max_m: float | None = 500.0
    composite_floor: float | None = 5.0
    outlier_floor: float | None = 0.0
    include_sparse: bool = True

    @classmethod
    def from_plain(cls, data: Mapping[str, Any]) -> ScreenConfig:
        """Build from stored JSON. Missing keys keep their defaults; unknown keys fail."""
        allowed = {f.name for f in fields(cls)}
        for key in data:
            if key not in allowed:
                raise ScreenConfigError(f"{key}: unknown filter")
        values: dict[str, Any] = {}
        for name, value in data.items():
            if name == "include_sparse":
                if not isinstance(value, bool):
                    raise ScreenConfigError(f"{name}: expected true or false")
            elif value is not None:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise ScreenConfigError(f"{name}: expected a number or null")
                if not math.isfinite(value):
                    raise ScreenConfigError(f"{name}: expected a finite number")
                value = float(value)
            values[name] = value
        return cls(**values)

    def to_plain(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class ScreenRow:
    """One scored company with the fields the filters look at."""

    company_id: int
    ticker: str
    composite: float
    outlier: float
    is_sparse: bool
    mcap_musd: float | None = None
    price: float | None = None
    avg_vol_k: float | None = None
    shr_out_m: float | None = None


@dataclass(frozen=True)
class ScreenOutcome:
    row: ScreenRow
    passed: bool
    reasons: tuple[str, ...]  # every failed filter, in a fixed order
    rank: int | None  # 1 for the highest composite among passers; None when excluded


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def failed_filters(row: ScreenRow, config: ScreenConfig) -> list[str]:
    """Reason codes of every filter the company fails (empty when it passes)."""
    reasons: list[str] = []

    mcap = _finite(row.mcap_musd)
    if config.mcap_min_musd is not None or config.mcap_max_musd is not None:
        if mcap is None:
            reasons.append(MCAP_MISSING)
        else:
            if config.mcap_min_musd is not None and mcap < config.mcap_min_musd:
                reasons.append(MCAP_MIN)
            if config.mcap_max_musd is not None and mcap > config.mcap_max_musd:
                reasons.append(MCAP_MAX)

    if config.price_min is not None:
        price = _finite(row.price)
        if price is None:
            reasons.append(PRICE_MISSING)
        elif price < config.price_min:
            reasons.append(PRICE_MIN)

    if config.avg_vol_min_k is not None:
        volume = _finite(row.avg_vol_k)
        if volume is None:
            reasons.append(AVG_VOL_MISSING)
        elif volume < config.avg_vol_min_k:
            reasons.append(AVG_VOL_MIN)

    if config.shares_outstanding_max_m is not None:
        shares = _finite(row.shr_out_m)
        if shares is None:
            reasons.append(SHARES_MISSING)
        elif shares > config.shares_outstanding_max_m:
            reasons.append(SHARES_MAX)

    if config.composite_floor is not None and row.composite < config.composite_floor:
        reasons.append(COMPOSITE_FLOOR)
    if config.outlier_floor is not None and row.outlier < config.outlier_floor:
        reasons.append(OUTLIER_FLOOR)
    if not config.include_sparse and row.is_sparse:
        reasons.append(SPARSE_GROUP)
    return reasons


def screen(rows: Iterable[ScreenRow], config: ScreenConfig) -> list[ScreenOutcome]:
    """Evaluate every row. Passers come first, by composite descending then ticker, with ranks."""
    evaluated = [(row, failed_filters(row, config)) for row in rows]
    passers = sorted(
        (item for item in evaluated if not item[1]),
        key=lambda item: (-item[0].composite, item[0].ticker),
    )
    excluded = sorted((item for item in evaluated if item[1]), key=lambda item: item[0].ticker)
    outcomes = [
        ScreenOutcome(row, True, (), rank) for rank, (row, _) in enumerate(passers, start=1)
    ]
    outcomes += [ScreenOutcome(row, False, tuple(reasons), None) for row, reasons in excluded]
    return outcomes


def near_the_line(
    outcomes: Iterable[ScreenOutcome],
    config: ScreenConfig,
    margin: float = DEFAULT_NEAR_LINE_MARGIN,
) -> list[ScreenOutcome]:
    """Companies that fail exactly one filter.

    A company whose only failure is the composite floor counts only when it sits within
    ``margin`` below the floor; any other single failure always counts. Ordered by composite
    descending, then ticker.
    """
    near = []
    for outcome in outcomes:
        if len(outcome.reasons) != 1:
            continue
        if outcome.reasons[0] == COMPOSITE_FLOOR and config.composite_floor is not None:
            if config.composite_floor - outcome.row.composite > margin:
                continue
        near.append(outcome)
    return sorted(near, key=lambda o: (-o.row.composite, o.row.ticker))


@dataclass(frozen=True)
class FilterFailure:
    """One failed filter with the company's actual value and the threshold it missed."""

    reason: str
    filter: str  # market_cap, price, avg_volume, shares_outstanding, composite, outlier, peer_group
    actual: float | bool | None
    threshold: float | bool | None
    comparison: str  # "at_least", "at_most", "present" or "not_sparse"


def explain_failures(row: ScreenRow, config: ScreenConfig) -> list[FilterFailure]:
    """Every failed filter with its actual value and threshold, in the same order as the reasons."""
    details: dict[str, FilterFailure] = {
        MCAP_MISSING: FilterFailure(MCAP_MISSING, "market_cap", None, None, "present"),
        MCAP_MIN: FilterFailure(
            MCAP_MIN, "market_cap", row.mcap_musd, config.mcap_min_musd, "at_least"
        ),
        MCAP_MAX: FilterFailure(
            MCAP_MAX, "market_cap", row.mcap_musd, config.mcap_max_musd, "at_most"
        ),
        PRICE_MISSING: FilterFailure(PRICE_MISSING, "price", None, None, "present"),
        PRICE_MIN: FilterFailure(PRICE_MIN, "price", row.price, config.price_min, "at_least"),
        AVG_VOL_MISSING: FilterFailure(AVG_VOL_MISSING, "avg_volume", None, None, "present"),
        AVG_VOL_MIN: FilterFailure(
            AVG_VOL_MIN, "avg_volume", row.avg_vol_k, config.avg_vol_min_k, "at_least"
        ),
        SHARES_MISSING: FilterFailure(SHARES_MISSING, "shares_outstanding", None, None, "present"),
        SHARES_MAX: FilterFailure(
            SHARES_MAX,
            "shares_outstanding",
            row.shr_out_m,
            config.shares_outstanding_max_m,
            "at_most",
        ),
        COMPOSITE_FLOOR: FilterFailure(
            COMPOSITE_FLOOR, "composite", row.composite, config.composite_floor, "at_least"
        ),
        OUTLIER_FLOOR: FilterFailure(
            OUTLIER_FLOOR, "outlier", row.outlier, config.outlier_floor, "at_least"
        ),
        SPARSE_GROUP: FilterFailure(SPARSE_GROUP, "peer_group", True, False, "not_sparse"),
    }
    return [details[reason] for reason in failed_filters(row, config)]
