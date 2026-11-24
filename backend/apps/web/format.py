"""Display strings for the company page. Pure functions; ``None`` in, ``None`` out.

Formats follow the report layout: ``$5.39``, ``$308M`` or ``$1.2B``, signed percentages with two
decimals (``-29.61%``), plain percentages with two decimals, volume in thousands with a ``K``
(no decimals), RSI with one decimal and relative volume with three.
Rounding is half up, never banker's.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def _round(value: float, places: int) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def usd(value: float | None) -> str | None:
    return None if value is None else f"${_round(value, 2):,}"


def market_cap(musd: float | None) -> str | None:
    """Market cap in USD millions: ``$308M`` below a billion, ``$1.2B`` from a billion."""
    if musd is None:
        return None
    if abs(musd) >= 1000:
        return f"${_round(musd / 1000, 1):,}B"
    return f"${_round(musd, 0):,}M"


def percent(value: float | None) -> str | None:
    return None if value is None else f"{_round(value, 2)}%"


def signed_percent(value: float | None) -> str | None:
    if value is None:
        return None
    rounded = _round(value, 2)
    return f"+{rounded}%" if rounded > 0 else f"{rounded}%"


def volume_k(value: float | None) -> str | None:
    """Average volume is stored in thousands of shares: ``842.3`` is shown as ``842K``."""
    return None if value is None else f"{_round(value, 0):,}K"


def number(value: float | None, places: int = 2) -> str | None:
    return None if value is None else f"{_round(value, places)}"
