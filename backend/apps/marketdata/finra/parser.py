"""Parse a FINRA consolidated short-volume file.

Format: pipe-delimited with the header ``Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|
Market`` and dates as YYYYMMDD. A file may end with a line holding only a record count; lines
that are not six columns wide are ignored. Volumes may have decimals and are rounded to whole
shares. A data row that cannot be read (bad number, negative or non-finite volume, a date
other than the requested one, a blank symbol) is skipped and counted, so one bad row does not
lose the day. Symbols are kept exactly as written: they are case-sensitive (``BCpC`` and
``BCPC`` are different securities). A file with a wrong header or with no readable row raises
``ShortVolumeFormatError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

HEADER = ("date", "symbol", "shortvolume", "shortexemptvolume", "totalvolume", "market")


class ShortVolumeFormatError(ValueError):
    pass


@dataclass(frozen=True)
class ShortVolumeRow:
    symbol: str
    short_volume: int
    short_exempt_volume: int | None
    total_volume: int
    market: str


@dataclass
class ParsedShortVolume:
    rows: list[ShortVolumeRow] = field(default_factory=list)
    malformed: int = 0


def _volume(text: str) -> int:
    """A share count. The real files carry fractions (``637633.605702``), so a value is rounded to
    the nearest whole share; the effect on a ratio is far below the four decimals that are kept."""
    value = Decimal(text.strip())
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a share count: {text}")
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def parse_short_volume(content: bytes, expected: date) -> ParsedShortVolume:
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ShortVolumeFormatError(f"The file is not UTF-8 text: {exc}") from exc
    lines = [line for line in lines if line.strip()]
    if not lines:
        raise ShortVolumeFormatError("The file is empty.")
    header = tuple(part.strip().lower() for part in lines[0].split("|"))
    if header != HEADER:
        raise ShortVolumeFormatError(f"Unexpected header: {lines[0][:120]!r}")
    wanted = f"{expected:%Y%m%d}"
    parsed = ParsedShortVolume()
    seen: set[tuple[str, str]] = set()
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) != len(HEADER):
            continue  # the trailing record count
        day, symbol, short, exempt, total, market = (p.strip() for p in parts)
        try:
            if day != wanted or not symbol or not market:
                raise ValueError("wrong date, symbol or market")
            row = ShortVolumeRow(
                symbol=symbol,
                short_volume=_volume(short),
                short_exempt_volume=_volume(exempt) if exempt else None,
                total_volume=_volume(total),
                market=market,
            )
        except (ValueError, InvalidOperation):
            parsed.malformed += 1
            continue
        key = (row.symbol, row.market)
        if key in seen:  # a repeated line would break the unique key
            parsed.malformed += 1
            continue
        seen.add(key)
        parsed.rows.append(row)
    if not parsed.rows:
        raise ShortVolumeFormatError("The file has no readable rows.")
    return parsed
