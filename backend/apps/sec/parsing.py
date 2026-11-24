"""Pure parsing of the three SEC documents: company_tickers, submissions and companyfacts.

Nothing here touches the database or the network. Malformed input raises ``ValueError``,
``KeyError`` or ``TypeError``; the collector records that as ``parse_error`` for the one company.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

# Filings stored in sec_filing. Amendments carry a "/A" suffix.
FILING_FORMS = frozenset({"10-K", "10-Q", "20-F", "6-K"})
FORM4_FORMS = frozenset({"4", "4/A"})
# Form 4 filings older than this many days are neither queued nor fetched.
FORM4_WINDOW_DAYS = 90

# Taxonomies in the order they are read: ifrs-full only if us-gaap gives nothing.
TAXONOMIES = ("us-gaap", "ifrs-full")

# The allow-list: cash, short-term investments, operating cash flow, R&D and revenue. The final
# list is an open item (Step 11); this is the other system's documented set.
ALLOWED_TAGS: dict[str, frozenset[str]] = {
    "us-gaap": frozenset(
        {
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "Cash",
            "ShortTermInvestments",
            "MarketableSecuritiesCurrent",
            "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
            "NetCashProvidedByUsedInOperatingActivities",
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
        }
    ),
    "ifrs-full": frozenset(
        {
            "CashAndCashEquivalents",
            "OtherCurrentFinancialAssets",
            "CashFlowsFromUsedInOperatingActivities",
            "ResearchAndDevelopmentExpense",
            "Revenue",
        }
    ),
}


def pad_cik(value: Any) -> str:
    """A CIK as the ten-digit string the database stores."""
    digits = str(int(value))
    if len(digits) > 10:
        raise ValueError(f"CIK has more than ten digits: {value}")
    return digits.zfill(10)


def parse_company_tickers(content: bytes) -> dict[str, str]:
    """company_tickers.json: upper-case ticker to ten-digit CIK."""
    data = json.loads(content)
    mapping: dict[str, str] = {}
    for entry in data.values():
        ticker = str(entry["ticker"]).strip().upper()
        if ticker and ticker not in mapping:
            mapping[ticker] = pad_cik(entry["cik_str"])
    return mapping


@dataclass(frozen=True)
class SubmissionEntry:
    accession: str
    form: str
    filed_at: date
    period_end: date | None
    primary_document: str

    @property
    def is_amendment(self) -> bool:
        return self.form.endswith("/A")


def parse_submissions(content: bytes) -> list[SubmissionEntry]:
    """The recent filings of a submissions feed (the older ``files`` pages are not read)."""
    recent = json.loads(content)["filings"]["recent"]
    entries = []
    for i, accession in enumerate(recent["accessionNumber"]):
        report = recent.get("reportDate", [""] * (i + 1))[i]
        documents = recent.get("primaryDocument", [""] * (i + 1))
        entries.append(
            SubmissionEntry(
                accession=accession,
                form=recent["form"][i],
                filed_at=date.fromisoformat(recent["filingDate"][i]),
                period_end=date.fromisoformat(report) if report else None,
                primary_document=documents[i] if i < len(documents) else "",
            )
        )
    return entries


def is_stored_filing_form(form: str) -> bool:
    return form.removesuffix("/A") in FILING_FORMS


def is_form4(form: str) -> bool:
    return form in FORM4_FORMS


@dataclass(frozen=True)
class FactRow:
    taxonomy: str
    tag: str
    unit: str
    period_start: date | None
    period_end: date
    value: Decimal
    accession: str
    form: str
    fy: int | None
    fp: str
    filed_at: date

    @property
    def key(self) -> tuple[Any, ...]:
        return (
            self.taxonomy,
            self.tag,
            self.unit,
            self.period_start,
            self.period_end,
            self.accession,
        )


@dataclass(frozen=True)
class FactsParse:
    rows: list[FactRow]
    taxonomy: str = ""
    reason_code: str | None = None  # why there are no rows: no_companyfacts or non_usd_only


# A value that would not fit the column (24 digits, 4 decimals) is treated as bad data.
_MAX_ABS_VALUE = Decimal(10) ** 20


def parse_companyfacts(content: bytes) -> FactsParse:
    """Allow-listed USD facts. us-gaap is read first, ifrs-full only if us-gaap has nothing."""
    facts = json.loads(content, parse_float=Decimal).get("facts", {})
    saw_other_currency = False
    for taxonomy in TAXONOMIES:
        rows: dict[tuple[Any, ...], FactRow] = {}
        tags = facts.get(taxonomy, {})
        for tag in ALLOWED_TAGS[taxonomy]:
            units = tags.get(tag, {}).get("units", {})
            if units and "USD" not in units:
                saw_other_currency = True
            for item in units.get("USD", []):
                value = Decimal(str(item["val"]))
                if abs(value) >= _MAX_ABS_VALUE:
                    continue
                row = FactRow(
                    taxonomy=taxonomy,
                    tag=tag,
                    unit="USD",
                    period_start=date.fromisoformat(item["start"]) if item.get("start") else None,
                    period_end=date.fromisoformat(item["end"]),
                    value=value,
                    accession=item["accn"],
                    form=item.get("form", ""),
                    fy=int(item["fy"]) if item.get("fy") is not None else None,
                    fp=item.get("fp") or "",
                    filed_at=date.fromisoformat(item["filed"]),
                )
                rows.setdefault(row.key, row)
        if rows:
            return FactsParse(list(rows.values()), taxonomy)
    return FactsParse([], reason_code="non_usd_only" if saw_other_currency else "no_companyfacts")
