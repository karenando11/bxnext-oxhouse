"""Parse a Finviz Elite CSV export.

Pure Python: no database, no network. Columns are read **by position**, never by header text
alone, because the export repeats some headers (the 52-week pairs); a dictionary keyed by header
would silently keep only one of each pair.

Rules:

* ``-``, ``N/A`` and blank cells are NULL, never zero.
* ``%`` is stripped (12.4% is 12.4). ``$`` and thousands separators are stripped. A K, M, B or T
  suffix is scaled into the stored unit (``$1,234.5M`` is 1234.5 in millions).
* When a mapped header occurs more than once, the occurrence that holds percent values is used for
  percent columns, else the first. Every other occurrence and every unmapped column is kept under
  ``extras`` as ``"<header>#<position>"`` so nothing is lost.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from apps.marketdata.finviz.columns import (
    COLUMNS,
    MILLIONS,
    NUMBER,
    PERCENT,
    THOUSANDS,
    Column,
    header_lookup,
    normalise_header,
)

NULL_TOKENS = frozenset({"", "-", "--", "n/a", "na", "nan", "none", "null"})
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_BASE = {MILLIONS: 1e6, THOUSANDS: 1e3}
_EARNINGS = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


class ExportFormatError(ValueError):
    """The file is not a usable Finviz export (empty, not CSV, or required headers missing)."""

    def __init__(self, message: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing = missing


def parse_number(raw: str | None, unit: str = "plain") -> float | None:
    """A numeric cell, or None for a blank, ``-`` or anything that is not a finite number."""
    text = (raw or "").strip()
    if text.casefold() in NULL_TOKENS:
        return None
    text = text.replace(",", "").replace("$", "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    scale = 1.0
    if text and text[-1].upper() in _SUFFIX and not text[-1].isdigit():
        scale = _SUFFIX[text[-1].upper()] / _BASE.get(unit, 1.0)
        text = text[:-1].strip()
    try:
        value = float(text) * scale
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_earnings_date(text: str | None) -> date | None:
    """The date in an earnings text when it contains M/D/YYYY, else None."""
    match = _EARNINGS.search(text or "")
    if match is None:
        return None
    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


@dataclass(frozen=True)
class ParsedRow:
    ticker: str
    values: dict[str, Any]  # snapshot field -> parsed value (None when blank)
    extras: dict[str, str]  # unmapped and duplicate columns, as delivered


@dataclass
class ParsedExport:
    rows: list[ParsedRow]
    headers: list[str]  # as delivered, in order
    mapped: dict[str, int]  # snapshot field -> the position it was read from
    missing_fields: list[str]  # mapped fields whose header is absent from the file
    unmapped_headers: list[str]
    duplicate_headers: dict[str, list[int]]  # header text -> positions
    duplicate_choice: dict[str, int] = field(default_factory=dict)  # field -> position chosen
    bad_cells: Counter[str] = field(default_factory=Counter)  # field -> unparseable cell count
    duplicate_tickers: list[str] = field(default_factory=list)
    skipped_rows: int = 0  # rows with no ticker

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _percent_share(rows: list[list[str]], position: int) -> float:
    cells = [r[position].strip() for r in rows if position < len(r)]
    cells = [c for c in cells if c.casefold() not in NULL_TOKENS]
    return 0.0 if not cells else sum(c.endswith("%") for c in cells) / len(cells)


def parse_export(content: bytes | str) -> ParsedExport:
    """Parse a whole export. Raises ``ExportFormatError`` for an unusable file."""
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("﻿")
    if not text.strip():
        raise ExportFormatError("The export is empty.")
    if text.lstrip().startswith("<"):
        raise ExportFormatError("The response is a web page, not a CSV (check the token).")
    table = list(csv.reader(io.StringIO(text)))
    headers, body = table[0], [r for r in table[1:] if any(cell.strip() for cell in r)]

    lookup = header_lookup()
    by_header: dict[str, list[int]] = {}
    for position, header in enumerate(headers):
        by_header.setdefault(normalise_header(header), []).append(position)
    duplicates = {
        headers[positions[0]].strip(): positions
        for positions in by_header.values()
        if len(positions) > 1
    }

    mapped: dict[str, int] = {}
    choice: dict[str, int] = {}
    columns: dict[str, Column] = {}
    for key, positions in by_header.items():
        column = lookup.get(key)
        if column is None or column.field in mapped:
            continue
        position = positions[0]
        if len(positions) > 1 and column.unit == PERCENT:
            position = max(positions, key=lambda p: (_percent_share(body, p), -p))
            choice[column.field] = position
        elif len(positions) > 1:
            choice[column.field] = position
        mapped[column.field], columns[column.field] = position, column
    taken = set(mapped.values())
    unmapped = [
        headers[p].strip()
        for p in range(len(headers))
        if normalise_header(headers[p]) not in lookup
    ]

    missing_required = tuple(c.field for c in COLUMNS if c.required and c.field not in mapped)
    if missing_required:
        raise ExportFormatError(
            "Required column(s) missing from the export: " + ", ".join(missing_required),
            missing_required,
        )

    result = ParsedExport(
        rows=[],
        headers=[h.strip() for h in headers],
        mapped=mapped,
        missing_fields=[c.field for c in COLUMNS if c.field not in mapped],
        unmapped_headers=unmapped,
        duplicate_headers=duplicates,
        duplicate_choice=choice,
    )
    seen: set[str] = set()
    for raw in body:
        cells = [raw[p].strip() if p < len(raw) else "" for p in range(len(headers))]
        ticker = cells[mapped["ticker"]].upper()
        if not ticker:
            result.skipped_rows += 1
            continue
        if ticker in seen:
            result.duplicate_tickers.append(ticker)
            continue
        seen.add(ticker)
        values: dict[str, Any] = {}
        for column in COLUMNS:
            if column.field == "ticker" or column.field not in mapped:
                continue
            cell = cells[mapped[column.field]]
            if column.kind == NUMBER:
                number = parse_number(cell, column.unit)
                if number is None and cell.casefold() not in NULL_TOKENS:
                    result.bad_cells[column.field] += 1
                values[column.field] = number
            else:
                values[column.field] = cell
        if "earnings_date_text" in mapped:
            values["earnings_date"] = parse_earnings_date(values.get("earnings_date_text"))
        extras = {
            f"{headers[p].strip()}#{p}": cells[p]
            for p in range(len(headers))
            if p not in taken and cells[p].strip()
        }
        result.rows.append(ParsedRow(ticker, values, extras))
    return result
