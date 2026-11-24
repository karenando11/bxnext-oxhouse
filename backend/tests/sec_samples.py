"""Synthetic SEC responses for tests. They are invented, never recorded SEC data.

They follow the shape of the real documents (F7 is still to be recorded from the production host).
The six sample cases: KTTA (US filer), PACS (US filer with a 10-K/A), BNTX (IFRS filer), a
non-USD filer, a ticker with no CIK, and a filer with an amendment.
"""

from __future__ import annotations

import json
from typing import Any

TICKERS = {
    "KTTA": 1000001,
    "PACS": 1000002,
    "BNTX": 1000003,
    "NUSD": 1000004,
    "AMND": 1000005,
}


def tickers_json(extra: dict[str, int] | None = None) -> bytes:
    mapping = {**TICKERS, **(extra or {})}
    return json.dumps(
        {
            str(i): {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Inc"}
            for i, (ticker, cik) in enumerate(mapping.items())
        }
    ).encode()


def submissions_json(filings: list[tuple[str, str, str, str]]) -> bytes:
    """filings: (accession, form, filing date, report date)."""
    return json.dumps(
        {
            "filings": {
                "recent": {
                    "accessionNumber": [f[0] for f in filings],
                    "form": [f[1] for f in filings],
                    "filingDate": [f[2] for f in filings],
                    "reportDate": [f[3] for f in filings],
                    "primaryDocument": [f"doc-{f[0]}.htm" for f in filings],
                }
            }
        }
    ).encode()


def fact(
    val: int | float,
    end: str,
    accn: str,
    start: str | None = None,
    form: str = "10-K",
    filed: str = "2026-03-01",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "end": end,
        "val": val,
        "accn": accn,
        "fy": int(end[:4]),
        "fp": "FY",
        "form": form,
        "filed": filed,
    }
    if start:
        item["start"] = start
    return item


def facts_json(taxonomy: str, tags: dict[str, dict[str, list[dict[str, Any]]]]) -> bytes:
    """tags: tag name to {unit: [facts]}."""
    return json.dumps(
        {"facts": {taxonomy: {tag: {"units": units} for tag, units in tags.items()}}}
    ).encode()


US_FACTS = facts_json(
    "us-gaap",
    {
        "CashAndCashEquivalentsAtCarryingValue": {
            "USD": [fact(1_500_000, "2025-12-31", "0001000001-26-000001")]
        },
        "NetCashProvidedByUsedInOperatingActivities": {
            "USD": [
                fact(
                    -400_000,
                    "2025-12-31",
                    "0001000001-26-000001",
                    start="2025-01-01",
                )
            ]
        },
        "EntityCommonStockSharesOutstanding": {  # not on the allow-list
            "shares": [fact(1000, "2025-12-31", "0001000001-26-000001")]
        },
    },
)

IFRS_FACTS = facts_json(
    "ifrs-full",
    {"CashAndCashEquivalents": {"USD": [fact(9_000_000, "2025-12-31", "0001000003-26-000001")]}},
)

NON_USD_FACTS = facts_json(
    "ifrs-full",
    {"CashAndCashEquivalents": {"EUR": [fact(5_000_000, "2025-12-31", "0001000004-26-000001")]}},
)

AMEND_FACTS = facts_json(
    "us-gaap",
    {
        "CashAndCashEquivalentsAtCarryingValue": {
            "USD": [
                fact(700_000, "2025-12-31", "0001000005-26-000001"),
                fact(
                    650_000,
                    "2025-12-31",
                    "0001000005-26-000009",
                    form="10-K/A",
                    filed="2026-04-01",
                ),
            ]
        }
    },
)


class FakeSec:
    """A transport that plays back documents by URL and records every request."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = responses or {}
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.requests.append((url, headers))
        outcome = self.responses.get(url, (404, b""))
        if isinstance(outcome, list):  # a script of outcomes, used one per request
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, bytes):
            return 200, outcome
        return outcome

    def count(self, fragment: str) -> int:
        return sum(1 for url, _ in self.requests if fragment in url)
