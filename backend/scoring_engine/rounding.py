"""Display rounding.

The existing system rounds with JavaScript ``toFixed`` semantics: round half up applied to
the *exact binary value* of the number. That is why 1.25 (exactly representable) displays as
1.3 while 4.55 (stored as 4.5499999999999998) displays as 4.5. Stored values are never
rounded; rounding happens only for display and for comparisons against stored display values.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

# Decimals shown for each score. ``None`` means the value is displayed unrounded
# (Recency is one of 1.0, 0.75, 0.5 or 0.3 and must never be rounded to one decimal).
DISPLAY_DECIMALS: dict[str, int | None] = {
    "outlier": 1,
    "divergence": 1,
    "recency": None,
}


def round_half_up(value: float, ndigits: int = 1) -> float:
    """Round ``value`` half away from zero, using its exact binary expansion."""
    if not math.isfinite(value):
        raise ValueError(f"Cannot round a non-finite value: {value!r}")
    quantum = Decimal(1).scaleb(-ndigits)
    rounded = float(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))
    return 0.0 if rounded == 0 else rounded  # never display "-0.0"


def display_score(name: str, value: float) -> float:
    """Return ``value`` as it is displayed for the score called ``name``."""
    decimals = DISPLAY_DECIMALS[name]
    return value if decimals is None else round_half_up(value, decimals)
