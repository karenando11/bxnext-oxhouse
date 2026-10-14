"""Runway as the scorer sees it, controlled by the ``runway_as_filed`` quirk.

The existing system uses runway as computed at the balance-sheet date, even when that date is
months old. That is the parity behaviour (``runway_as_filed`` on). With the quirk off the
runway is reduced by the time that has passed since the balance-sheet date, floored at 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RunwayAdjustConfig:
    days_per_month: float = 30.44  # the same month length the financing date uses


def effective_runway_months(
    runway_months: float | None,
    balance_sheet_date: date | None,
    today: date,
    *,
    runway_as_filed: bool = True,
    config: RunwayAdjustConfig = RunwayAdjustConfig(),  # noqa: B008 - frozen, safe as a default
) -> float | None:
    """Runway used by the Cash Health ceiling and the financing boost.

    Returns the runway as filed when the quirk is on, or when it cannot be adjusted because the
    balance-sheet date is unknown. Otherwise subtracts the months elapsed since that date.
    """
    if runway_months is None or not math.isfinite(runway_months):
        return None
    if runway_as_filed or balance_sheet_date is None:
        return runway_months
    elapsed = max((today - balance_sheet_date).days, 0) / config.days_per_month
    return max(runway_months - elapsed, 0.0)
