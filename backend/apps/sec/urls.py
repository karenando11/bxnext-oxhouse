"""SEC endpoints. No Django imports, so scripts can use the same URLs as the collectors."""

from __future__ import annotations

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FORM4_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"


def form4_document_url(cik: str, accession: str, primary_document: str) -> str:
    """The raw Form 4 XML. The submissions feed names the styled view (``xslF345X05/name.xml``);
    the XML is the same file name one level up, under the issuer's CIK."""
    return FORM4_URL.format(
        cik=int(cik),
        accession=accession.replace("-", ""),
        document=primary_document.rsplit("/", 1)[-1],
    )
