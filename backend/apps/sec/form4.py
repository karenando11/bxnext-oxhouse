"""Parse a Form 4 or 4/A XML document (the ``ownershipDocument`` format).

Only the non-derivative table is read. Every transaction line is attributed to the first
reporting owner listed; the 10b5-1 flag is the filing-level checkbox (``aff10b5One``), absent on
older filings and then taken as not 10b5-1. ``amount_usd`` is shares times price, or ``None`` when
either is missing.

A document that cannot be read raises ``Form4ParseError`` with a short reason. Documents that
declare a DOCTYPE or an ENTITY are refused (real Form 4 files have neither), which closes the
entity-expansion attacks of the standard XML parser.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TRUE_VALUES = {"1", "true"}
CENTS = Decimal("0.01")


class Form4ParseError(ValueError):
    """The document cannot be turned into a filing. The message is stored as the reason."""


@dataclass(frozen=True)
class ReportingOwner:
    cik: str
    name: str
    is_officer: bool
    is_director: bool
    is_ten_percent_owner: bool
    officer_title: str


@dataclass(frozen=True)
class Form4Transaction:
    line_no: int
    transaction_date: date
    code: str
    shares: Decimal | None
    price_per_share: Decimal | None
    acquired_or_disposed: str

    @property
    def amount_usd(self) -> Decimal | None:
        if self.shares is None or self.price_per_share is None:
            return None
        return (self.shares * self.price_per_share).quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass
class ParsedForm4:
    document_type: str
    period_of_report: date | None
    is_10b5_1: bool
    issuer_cik: str
    original_submission_date: date | None
    owners: list[ReportingOwner] = field(default_factory=list)
    transactions: list[Form4Transaction] = field(default_factory=list)


def _clean(text: str | None) -> str:
    return (text or "").strip()


def _date(text: str | None) -> date | None:
    value = _clean(text)
    return date.fromisoformat(value[:10]) if value else None


def _flag(text: str | None) -> bool:
    return _clean(text).lower() in TRUE_VALUES


def _decimal(text: str | None) -> Decimal | None:
    value = _clean(text)
    return Decimal(value) if value else None


def _cik(text: str | None) -> str:
    value = _clean(text)
    if not value.isdigit() or len(value) > 10:
        raise Form4ParseError(f"not a CIK: {value[:20]!r}")
    return value.zfill(10)


def _value(parent: ET.Element, path: str) -> str | None:
    node = parent.find(f"{path}/value")
    return node.text if node is not None else None


def parse_form4(content: bytes) -> ParsedForm4:
    head = content[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in content.lower():
        raise Form4ParseError("the document declares a DOCTYPE or an ENTITY")
    try:
        root = ET.fromstring(content)  # noqa: S314 (DOCTYPE and ENTITY refused above)
    except ET.ParseError as exc:
        raise Form4ParseError(f"not well-formed XML: {exc}") from exc
    if root.tag != "ownershipDocument":
        raise Form4ParseError(f"not an ownership document: <{root.tag}>")
    try:
        return _parse(root)
    except Form4ParseError:
        raise
    except (ValueError, InvalidOperation, ArithmeticError) as exc:
        raise Form4ParseError(f"unreadable value: {exc}") from exc


def _parse(root: ET.Element) -> ParsedForm4:
    issuer = root.find("issuer")
    if issuer is None:
        raise Form4ParseError("no issuer")
    parsed = ParsedForm4(
        document_type=_clean(root.findtext("documentType")),
        period_of_report=_date(root.findtext("periodOfReport")),
        is_10b5_1=_flag(root.findtext("aff10b5One")),
        issuer_cik=_cik(issuer.findtext("issuerCik")),
        original_submission_date=_date(root.findtext("dateOfOriginalSubmission")),
    )
    for node in root.findall("reportingOwner"):
        relationship = node.find("reportingOwnerRelationship")
        flags = relationship if relationship is not None else ET.Element("none")
        parsed.owners.append(
            ReportingOwner(
                cik=_cik(node.findtext("reportingOwnerId/rptOwnerCik")),
                name=_clean(node.findtext("reportingOwnerId/rptOwnerName")),
                is_officer=_flag(flags.findtext("isOfficer")),
                is_director=_flag(flags.findtext("isDirector")),
                is_ten_percent_owner=_flag(flags.findtext("isTenPercentOwner")),
                officer_title=_clean(flags.findtext("officerTitle")),
            )
        )
    if not parsed.owners:
        raise Form4ParseError("no reporting owner")
    for line_no, node in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction"), 1):
        when = _date(_value(node, "transactionDate"))
        code = _clean(node.findtext("transactionCoding/transactionCode"))
        if when is None or not code:
            raise Form4ParseError(f"transaction {line_no} has no date or code")
        amounts = node.find("transactionAmounts")
        if amounts is None:
            raise Form4ParseError(f"transaction {line_no} has no amounts")
        parsed.transactions.append(
            Form4Transaction(
                line_no=line_no,
                transaction_date=when,
                code=code,
                shares=_decimal(_value(amounts, "transactionShares")),
                price_per_share=_decimal(_value(amounts, "transactionPricePerShare")),
                acquired_or_disposed=_clean(_value(amounts, "transactionAcquiredDisposedCode")),
            )
        )
    return parsed
