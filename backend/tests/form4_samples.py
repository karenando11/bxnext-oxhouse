"""Synthetic Form 4 documents for tests. They are invented, never recorded SEC data.

They follow the ``ownershipDocument`` format (F12 is still to be recorded). The seven cases:
an officer purchase, a sale under a 10b5-1 plan, a sale not under a plan, a multi-owner filing,
a gift (code G), an amendment (4/A) and a document that fails to parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from apps.sec.collector import Form4Entry


@dataclass(frozen=True)
class Owner:
    cik: str = "0000000001"
    name: str = "Pat Example"
    officer: bool = False
    director: bool = False
    ten_percent: bool = False
    title: str = ""


CEO = Owner("0000000001", "Chief Exec", officer=True, title="Chief Executive Officer")
CFO = Owner("0000000002", "Chief Fin", officer=True, title="Chief Financial Officer")
DIR1 = Owner("0000000003", "Dana Director", director=True)


@dataclass(frozen=True)
class Line:
    code: str
    shares: str | None
    price: str | None
    day: str = "2026-09-30"
    side: str = "D"


def _flag(value: bool) -> str:
    return "1" if value else "0"


def _line(line: Line) -> str:
    shares = (
        f"<transactionShares><value>{line.shares}</value></transactionShares>"
        if line.shares
        else ""
    )
    price = (
        f"<transactionPricePerShare><value>{line.price}</value></transactionPricePerShare>"
        if line.price
        else '<transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>'
    )
    return f"""
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{line.day}</value></transactionDate>
      <transactionCoding>
        <transactionFormTypeCode>4</transactionFormTypeCode>
        <transactionCode>{line.code}</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        {shares}
        {price}
        <transactionAcquiredDisposedCode><value>{line.side}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>"""


def form4_xml(
    owners: list[Owner],
    lines: list[Line],
    *,
    issuer_cik: str = "0001000001",
    document_type: str = "4",
    period: str = "2026-09-30",
    plan: bool | None = False,
    original_date: str | None = None,
    derivative_only: bool = False,
) -> bytes:
    owner_xml = "".join(
        f"""
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>{o.cik}</rptOwnerCik><rptOwnerName>{o.name}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{_flag(o.director)}</isDirector><isOfficer>{_flag(o.officer)}</isOfficer>
      <isTenPercentOwner>{_flag(o.ten_percent)}</isTenPercentOwner>
      <officerTitle>{o.title}</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>"""
        for o in owners
    )
    table = (
        ""
        if derivative_only
        else f"<nonDerivativeTable>{''.join(_line(x) for x in lines)}</nonDerivativeTable>"
    )
    flag = "" if plan is None else f"<aff10b5One>{_flag(plan)}</aff10b5One>"
    original = (
        f"<dateOfOriginalSubmission>{original_date}</dateOfOriginalSubmission>"
        if original_date
        else ""
    )
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>{document_type}</documentType>
  <periodOfReport>{period}</periodOfReport>
  {original}
  {flag}
  <issuer>
    <issuerCik>{issuer_cik}</issuerCik><issuerName>Example Inc</issuerName>
    <issuerTradingSymbol>KTTA</issuerTradingSymbol>
  </issuer>{owner_xml}
  {table}
  <derivativeTable></derivativeTable>
  <ownerSignature><signatureName>x</signatureName><signatureDate>2026-10-01</signatureDate></ownerSignature>
</ownershipDocument>""".encode()


def entry(
    company_id: int,
    accession: str,
    *,
    cik: str = "0001000001",
    form: str = "4",
    filed: date = date(2026, 10, 1),
    period: date | None = date(2026, 9, 30),
) -> Form4Entry:
    return Form4Entry(
        company_id=company_id,
        cik=cik,
        accession=accession,
        form=form,
        filed_at=filed,
        period_of_report=period,
        primary_document=f"xslF345X05/{accession}.xml",
    )


def url_for(e: Form4Entry) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(e.cik)}/"
        f"{e.accession.replace('-', '')}/{e.accession}.xml"
    )
