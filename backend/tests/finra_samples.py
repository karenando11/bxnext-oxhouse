"""Synthetic FINRA Reg SHO files for tests. They are invented, never real FINRA data."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

HEADER = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"


def finra_file(day: date, rows: list[tuple[str, int, int, str]], footer: bool = True) -> bytes:
    """rows: (symbol, short volume, total volume, market). A record-count line ends the file."""
    lines = [HEADER]
    lines += [
        f"{day:%Y%m%d}|{sym}|{short}|0|{total}|{market}" for sym, short, total, market in rows
    ]
    if footer:
        lines.append(str(len(rows)))
    return ("\r\n".join(lines) + "\r\n").encode()


class FakeFinra:
    """A transport that plays back files by date and records every request."""

    def __init__(self, files: Mapping[date, object] | None = None, weekdays: bool = False) -> None:
        self.files: dict[date, object] = dict(files or {})
        self.weekdays = weekdays  # publish a small file for every weekday not listed in ``files``
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, bytes]:
        self.urls.append(url)
        day = date(int(url[-12:-8]), int(url[-8:-6]), int(url[-6:-4]))
        outcome = self.files.get(day, (404, b""))
        if self.weekdays and day not in self.files and day.weekday() < 5:
            outcome = finra_file(day, [("ZZZZ", 100, 200, "Q")])
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, bytes):
            return 200, outcome
        assert isinstance(outcome, tuple)
        return outcome

    def days_asked(self) -> list[date]:
        return [date(int(u[-12:-8]), int(u[-8:-6]), int(u[-6:-4])) for u in self.urls]
