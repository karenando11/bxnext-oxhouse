"""The effective-input resolver (Step 15): which stored runway and catalyst the scorer sees.

For each company and each of the two inputs, the stored rows on or before the day are looked at:

With ``carry_forward`` on (production):

* a good row dated the day itself is **fresh** (age 0);
* otherwise the newest good row not older than the maximum age is **carried** (age in days,
  reason ``stale_carried``);
* a newest good row that is too old gives NULL with ``stale_expired`` (**expired**);
* no good row at all gives NULL with the reason the latest row stored (**missing**).

With ``carry_forward`` off (the original system, kept for parity) the latest row is used as it was
delivered when it is good, whatever its date (the golden export's runway rows are a few days
old), and a bad latest row gives NULL with its own reason.

Finviz fields are never carried forward: this module does not touch them. The pure part has no
database; ``resolve_runways`` and ``resolve_catalysts`` read the rows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from apps.platform.settings_store import get_setting
from apps.sec.models import RunwayCalc
from apps.trials.models import CatalystSignal

FRESH, CARRIED, EXPIRED, MISSING = "fresh", "carried", "expired", "missing"

DEFAULT_MAX_AGES = {
    "runway": 120,  # days, SEC-derived
    "catalyst": 7,  # days, ClinicalTrials.gov-derived
    "short_volume": 5,  # trading days (applied by the database view)
    "insider": 7,  # days (applied by the database view)
}
SETTING_KEYS = {
    "runway": "max_age_runway_days",
    "catalyst": "max_age_catalyst_days",
    "short_volume": "max_age_short_volume_trading_days",
    "insider": "max_age_insider_days",
}


@dataclass(frozen=True)
class Candidate[R]:
    """One stored row as the resolver sees it."""

    day: date
    good: bool
    reason: str | None
    row: R


@dataclass(frozen=True)
class Resolution[R]:
    state: str
    row: R | None  # the row the scorer uses; None when the input is NULL
    age_days: int | None
    reason: str | None
    source_date: date | None

    @property
    def carried(self) -> bool:
        return self.state == CARRIED

    def as_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "age_days": self.age_days,
            "reason": self.reason,
            "source_date": self.source_date.isoformat() if self.source_date else None,
        }


def max_age(name: str) -> int:
    """The maximum age in days for ``runway``, ``catalyst``, ``short_volume`` or ``insider``."""
    return int(get_setting(SETTING_KEYS[name], DEFAULT_MAX_AGES[name]))


def resolve[R](
    candidates: Sequence[Candidate[R]], day: date, *, max_age_days: int, carry_forward: bool
) -> Resolution[R]:
    """Choose the effective row. ``candidates`` are the rows on or before ``day``, any order."""
    ordered = sorted((c for c in candidates if c.day <= day), key=lambda c: c.day)
    latest = ordered[-1] if ordered else None
    original = latest.reason if latest is not None else None
    if not carry_forward:
        if latest is not None and latest.good:
            return Resolution(FRESH, latest.row, (day - latest.day).days, None, latest.day)
        return Resolution(MISSING, None, None, original, None)
    if latest is not None and latest.day == day and latest.good:
        return Resolution(FRESH, latest.row, 0, None, latest.day)
    good = [c for c in ordered if c.good]
    if not good:
        return Resolution(MISSING, None, None, original, None)
    newest = good[-1]
    age = (day - newest.day).days
    if age > max_age_days:
        return Resolution(EXPIRED, None, age, "stale_expired", newest.day)
    return Resolution(CARRIED, newest.row, age, "stale_carried", newest.day)


def _first_reason(codes: Sequence[str]) -> str | None:
    return codes[0] if codes else None


def resolve_runways(
    company_ids: Sequence[int], day: date, *, carry_forward: bool, max_age_days: int
) -> dict[int, Resolution[RunwayCalc]]:
    grouped: dict[int, list[Candidate[RunwayCalc]]] = defaultdict(list)
    rows = RunwayCalc.objects.filter(company_id__in=list(company_ids), calc_date__lte=day)
    for row in rows.order_by("calc_date", "id"):
        good = row.runway_months is not None
        reason = _first_reason(row.reason_codes)
        grouped[row.company_id].append(Candidate(row.calc_date, good, reason, row))
    return {
        company_id: resolve(
            grouped.get(company_id, []), day, max_age_days=max_age_days, carry_forward=carry_forward
        )
        for company_id in company_ids
    }


def resolve_catalysts(
    company_ids: Sequence[int], day: date, *, carry_forward: bool, max_age_days: int
) -> dict[int, Resolution[CatalystSignal]]:
    grouped: dict[int, list[Candidate[CatalystSignal]]] = defaultdict(list)
    rows = CatalystSignal.objects.filter(company_id__in=list(company_ids), as_of_date__lte=day)
    for row in rows.order_by("as_of_date", "id"):
        good = row.status == "ok"
        reason = _first_reason(row.reason_codes)
        grouped[row.company_id].append(Candidate(row.as_of_date, good, reason, row))
    return {
        company_id: resolve(
            grouped.get(company_id, []), day, max_age_days=max_age_days, carry_forward=carry_forward
        )
        for company_id in company_ids
    }
