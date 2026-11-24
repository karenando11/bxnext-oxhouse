"""F7, F11 and F12: the SEC and FINRA collectors replayed on real recorded responses.

The files in ``tests/fixtures/recorded`` were saved unchanged by ``scripts/record_fixtures.py``
(see its manifest.json for URLs, times and hashes). Nothing here touches the network. The tests
skip when the folder is absent.

What the real files showed that the synthetic ones did not: FINRA volumes carry fractions
(``637633.605702``) and symbols are case-sensitive (``BCpC`` is not ``BCPC``); FINRA answers 403
for a date with no file.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.companies.models import Company
from apps.marketdata.finra.ingest import collect_short_volume
from apps.marketdata.finra.parser import parse_short_volume
from apps.marketdata.models import ShortVolumeDaily
from apps.marketdata.storage import LocalRawStorage
from apps.platform.models import DataQualityCheck
from apps.sec.client import SecClient, SecLimiter
from apps.sec.collector import Form4Entry, collect_sec
from apps.sec.form4 import parse_form4
from apps.sec.form4_collector import collect_form4
from apps.sec.models import SecFact, SecFiling, SecForm4Filing, SecForm4Transaction
from apps.sec.parsing import ALLOWED_TAGS, is_form4, parse_submissions
from apps.sec.urls import FACTS_URL, SUBMISSIONS_URL, TICKERS_URL, form4_document_url
from tests import factories
from tests.finra_samples import FakeFinra
from tests.sec_samples import FakeSec
from tests.test_sec_client import UA, Clock

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded"
pytestmark = pytest.mark.skipif(
    not RECORDED.exists(), reason="Recorded fixtures not found. Run scripts/record_fixtures.py."
)

SEC_TICKERS = ["KTTA", "RZLT", "XBIT", "YDES", "APLM", "SMMT", "PACS", "INDV", "ALPS", "BNTX"]
FINRA_DAYS = [date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19)]
FINRA_WEEKEND = date(2026, 11, 15)


def sec_files() -> dict[str, Path]:
    return {p.name: p for p in (RECORDED / "sec").glob("*.json")}


def per_ticker(kind: str) -> dict[str, tuple[str, bytes]]:
    """ticker -> (CIK, content) for the recorded submissions or companyfacts files."""
    found = {}
    for path in (RECORDED / "sec").glob(f"{kind}_*.json"):
        _, ticker, cik = path.stem.split("_")
        found[ticker] = (cik.removeprefix("CIK"), path.read_bytes())
    return found


@pytest.fixture
def storage(tmp_path: Path) -> LocalRawStorage:
    return LocalRawStorage(tmp_path / "raw")


def client_for(transport: Any, clock: Clock) -> SecClient:
    return SecClient(
        user_agent=UA,
        limiter=SecLimiter(clock=clock.now, sleep=clock.sleep),
        transport=transport,
        sleep=clock.sleep,
    )


# ------------------------------------------------------------------------ F7: SEC feeds


@pytest.fixture
def sec_universe() -> dict[str, Company]:
    companies = {}
    for ticker in SEC_TICKERS:
        companies[ticker] = factories.company(name=ticker, cik=None)
        factories.ticker(companies[ticker], ticker=ticker, valid_from=date(2026, 11, 1))
    return companies


@pytest.fixture
def sec_routes() -> dict[str, Any]:
    routes: dict[str, Any] = {TICKERS_URL: (RECORDED / "sec" / "company_tickers.json").read_bytes()}
    for cik, content in per_ticker("submissions").values():
        routes[SUBMISSIONS_URL.format(cik=cik)] = content
    for cik, content in per_ticker("companyfacts").values():
        routes[FACTS_URL.format(cik=cik)] = content
    return routes


def run_sec(routes: dict[str, Any], storage: Any, day: date = date(2026, 11, 20)) -> Any:
    clock = Clock()
    client = client_for(FakeSec(routes), clock)
    return collect_sec(day, client=client, storage=storage)


@pytest.mark.django_db
def test_the_real_sec_feeds_load_and_each_case_comes_out_as_specified(
    sec_universe: dict[str, Company], sec_routes: dict[str, Any], storage: LocalRawStorage
) -> None:
    result = run_sec(sec_routes, storage)
    assert result.failures == {}
    assert result.run.status == "ok"

    # A ticker with no CIK; every other ticker resolved to ten digits.
    assert result.no_cik == ["INDV"]
    for ticker, company in sec_universe.items():
        company.refresh_from_db()
        assert (company.cik is None) == (ticker == "INDV")
        if company.cik:
            assert len(company.cik) == 10 and company.cik.isdigit()

    # US filers read us-gaap; an IFRS filer reads ifrs-full with USD values.
    taxonomy = {
        t: set(SecFact.objects.filter(company=sec_universe[t]).values_list("taxonomy", flat=True))
        for t in SEC_TICKERS
    }
    for ticker in ("KTTA", "RZLT", "XBIT", "PACS", "SMMT", "YDES"):
        assert taxonomy[ticker] == {"us-gaap"}, ticker
    assert taxonomy["APLM"] == {"ifrs-full"}

    # A filer reporting in euros has no stored facts, and says why; so does one with no XBRL.
    assert taxonomy["BNTX"] == set() and result.skipped_facts["BNTX"] == "non_usd_only"
    assert taxonomy["ALPS"] == set() and result.skipped_facts["ALPS"] == "no_companyfacts"

    # Only allow-listed USD facts are stored, each with its accession, filed date and period.
    facts = SecFact.objects.all()
    assert facts.count() > 1000
    assert set(facts.values_list("unit", flat=True)) == {"USD"}
    for taxonomy_name, tags in ALLOWED_TAGS.items():
        used = set(facts.filter(taxonomy=taxonomy_name).values_list("tag", flat=True))
        assert used <= tags
    assert not facts.filter(accession="").exists()

    # Amended filings are recognised: 10-K/A, 10-Q/A, 20-F/A and 6-K/A all occur in these feeds.
    amended = SecFiling.objects.filter(is_amendment=True)
    forms = set(amended.values_list("form", flat=True))
    assert {"10-K/A", "10-Q/A", "20-F/A", "6-K/A"} <= forms
    assert all(f.form.endswith("/A") for f in amended)
    assert not SecFiling.objects.filter(is_amendment=False, form__endswith="/A").exists()
    assert set(SecFiling.objects.values_list("form", flat=True)) <= {
        f"{base}{suffix}" for base in ("10-K", "10-Q", "20-F", "6-K") for suffix in ("", "/A")
    }


@pytest.mark.django_db
def test_the_real_sec_feeds_reload_without_new_rows_and_hand_over_form4_entries(
    sec_universe: dict[str, Company], sec_routes: dict[str, Any], storage: LocalRawStorage
) -> None:
    first = run_sec(sec_routes, storage)
    filings, facts = SecFiling.objects.count(), SecFact.objects.count()
    second = run_sec(sec_routes, storage, day=date(2026, 11, 21))
    assert (SecFiling.objects.count(), SecFact.objects.count()) == (filings, facts)
    assert (second.filings_added, second.facts_added) == (0, 0)
    # Every Form 4 and 4/A filed in the 90 days before the run is offered for fetching.
    since = date(2026, 11, 20) - timedelta(days=90)
    expected = sum(
        1
        for _, content in per_ticker("submissions").values()
        for e in parse_submissions(content)
        if is_form4(e.form) and e.filed_at >= since
    )
    assert len(first.form4_entries) == expected > 0
    assert {e.form for e in first.form4_entries} == {"4", "4/A"}
    assert all(e.primary_document for e in first.form4_entries)


# -------------------------------------------------------------------- F12: Form 4 files


def form4_docs() -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (RECORDED / "form4").glob("form4_*.xml")}


def test_every_recorded_form4_document_parses_and_the_cases_are_all_there() -> None:
    docs = form4_docs()
    assert len(docs) > 200
    parsed = {name: parse_form4(content) for name, content in docs.items()}
    codes = {t.code for p in parsed.values() for t in p.transactions}
    assert {"P", "S", "G", "A", "F", "M"} <= codes
    assert any(p.is_10b5_1 and any(t.code == "S" for t in p.transactions) for p in parsed.values())
    assert any(
        not p.is_10b5_1 and any(t.code == "S" for t in p.transactions) for p in parsed.values()
    )
    assert any(p.document_type == "4/A" and p.original_submission_date for p in parsed.values())
    # Derivative-only filings (option grants) are common and have no non-derivative lines.
    assert any(not p.transactions for p in parsed.values())
    for p in parsed.values():
        assert p.owners and p.issuer_cik.isdigit() and len(p.issuer_cik) == 10
        for t in p.transactions:
            if t.shares is None or t.price_per_share is None:
                assert t.amount_usd is None
            else:
                assert t.amount_usd == (t.shares * t.price_per_share).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )


@pytest.fixture
def form4_setup() -> tuple[list[Form4Entry], dict[str, Any], date]:
    """Entries for every recorded document, built from the recorded submissions feeds."""
    docs = form4_docs()
    entries: list[Form4Entry] = []
    responses: dict[str, Any] = {}
    for ticker, (cik, content) in per_ticker("submissions").items():
        company = factories.company(name=ticker, cik=cik)
        for e in parse_submissions(content):
            name = f"form4_{ticker}_{e.accession}.xml"
            if is_form4(e.form) and name in docs:
                entries.append(
                    Form4Entry(
                        company.pk,
                        cik,
                        e.accession,
                        e.form,
                        e.filed_at,
                        e.period_end,
                        e.primary_document,
                    )  # fmt: skip
                )
                responses[form4_document_url(cik, e.accession, e.primary_document)] = docs[name]
    return entries, responses, max(e.filed_at for e in entries)


@pytest.mark.django_db
def test_the_form4_collector_on_real_documents(
    form4_setup: tuple[list[Form4Entry], dict[str, Any], date], storage: LocalRawStorage
) -> None:
    entries, responses, as_of = form4_setup
    client = client_for(FakeSec(responses), Clock())
    result = collect_form4(entries, as_of, client=client, storage=storage)

    assert result.parse_failures == {} and result.fetch_failures == {}
    assert result.not_issuer == []
    assert result.stored > 20 and result.stored + result.too_old == len(entries)
    assert SecForm4Filing.objects.filter(parse_status="failed").count() == 0

    # Every stored line equals what the parser reads from the XML, and amount is shares x price.
    docs = form4_docs()
    for filing in SecForm4Filing.objects.select_related("company"):
        parsed = parse_form4(docs[f"form4_{filing.company.name}_{filing.accession}.xml"])
        assert filing.is_10b5_1 is parsed.is_10b5_1
        assert filing.period_of_report == parsed.period_of_report
        stored = list(filing.transactions.order_by("line_no"))
        assert [(t.transaction_code, t.shares) for t in stored] == [
            (t.code, t.shares) for t in parsed.transactions
        ]
        for line in stored:
            if line.shares is not None and line.price_per_share is not None:
                assert line.amount_usd == (line.shares * line.price_per_share).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            assert line.reporting_owner_cik == parsed.owners[0].cik

    # Real amendments are linked to the original filed by the same owner for the same period.
    amendments = SecForm4Filing.objects.filter(form="4/A").exclude(amends_accession="")
    assert amendments.count() >= 3 and result.amendments_linked == amendments.count()
    for amendment in amendments:
        original = SecForm4Filing.objects.get(accession=amendment.amends_accession)
        assert original.form in {"4", "4/A"} and original.company_id == amendment.company_id
        assert original.period_of_report == amendment.period_of_report
        assert original.filed_at <= amendment.filed_at
        owner = amendment.transactions.first()
        assert owner is not None
        assert original.transactions.filter(reporting_owner_cik=owner.reporting_owner_cik).exists()


@pytest.mark.django_db
def test_real_form4_documents_load_once(
    form4_setup: tuple[list[Form4Entry], dict[str, Any], date], storage: LocalRawStorage
) -> None:
    entries, responses, as_of = form4_setup
    sec = FakeSec(responses)
    client = client_for(sec, Clock())
    collect_form4(entries, as_of, client=client, storage=storage)
    stored, lines, asked = (
        SecForm4Filing.objects.count(),
        SecForm4Transaction.objects.count(),
        len(sec.requests),
    )
    again = collect_form4(entries, as_of, client=client, storage=storage)
    assert (again.stored, len(sec.requests)) == (0, asked)
    assert (SecForm4Filing.objects.count(), SecForm4Transaction.objects.count()) == (stored, lines)


# --------------------------------------------------------------------- F11: FINRA files


def finra_file(day: date) -> bytes:
    return (RECORDED / "finra" / f"CNMSshvol{day:%Y%m%d}.txt").read_bytes()


@pytest.mark.parametrize("day", FINRA_DAYS)
def test_a_real_finra_file_parses_completely(day: date) -> None:
    raw = finra_file(day)
    parsed = parse_short_volume(raw, day)
    footer = int(raw.decode().splitlines()[-1])  # the record count that ends the file
    assert parsed.malformed == 0 and len(parsed.rows) == footer > 10000
    assert {r.market for r in parsed.rows} >= {"B,Q,N", "Q", "N"}
    symbols = [r.symbol for r in parsed.rows]
    assert len(set(symbols)) == len(symbols)
    # Symbols are case-sensitive: these two share their letters and are different securities.
    assert {"BCPC", "BCpC"} <= set(symbols)
    # Share counts carry fractions and are stored as whole shares, rounded half up.
    raw_rows = {
        parts[1]: parts
        for parts in (line.split("|") for line in raw.decode().splitlines()[1:])
        if len(parts) == 6
    }
    sample = next(r for r in parsed.rows if r.symbol == "AAA")
    assert sample.short_volume == int(
        Decimal(raw_rows["AAA"][2]).to_integral_value(rounding=ROUND_HALF_UP)
    )
    assert any("." in parts[2] for parts in raw_rows.values())


@pytest.mark.django_db
def test_the_finra_step_on_real_files(storage: LocalRawStorage) -> None:
    tickers = ["KTTA", "RZLT", "SMMT", "PACS", "APLM", "XBIT", "BF-B", "ZZZNOPE"]
    companies = {}
    for ticker in tickers:
        companies[ticker] = factories.company(name=ticker)
        factories.ticker(companies[ticker], ticker=ticker)
    files: dict[date, Any] = {d: finra_file(d) for d in FINRA_DAYS}
    files[FINRA_WEEKEND] = (403, b"")  # what FINRA answered for a date with no file
    fake = FakeFinra(files)
    result = collect_short_volume(
        date(2026, 11, 20),
        dates=[FINRA_WEEKEND, *FINRA_DAYS],
        transport=fake,
        sleep=lambda s: None,
        storage=storage,
    )
    assert result.of("loaded") == FINRA_DAYS
    assert result.of("no_file_for_date") == [FINRA_WEEKEND]  # Sunday: no request is made
    assert result.failed == {} and result.run.status == "ok"
    assert fake.days_asked() == FINRA_DAYS

    for day in FINRA_DAYS:
        rows = ShortVolumeDaily.objects.filter(trade_date=day)
        names = set(rows.values_list("company__name", flat=True))
        assert names == set(tickers) - {"ZZZNOPE"}
        # Finviz writes BF-B where FINRA writes BF/B.
        assert rows.get(company=companies["BF-B"]).ticker_raw == "BF/B"
        check = DataQualityCheck.objects.get(
            run=result.run, check_name="finra_universe_coverage", scope=f"finra.{day}"
        )
        assert (check.status, check.detail["missing"]) == ("warn", ["ZZZNOPE"])
        assert check.detail["reason_code"] == "not_in_finra_file"
        assert check.detail["file_rows"] > 10000

    # The same files again change nothing.
    again = collect_short_volume(
        date(2026, 11, 20), dates=FINRA_DAYS, transport=fake, sleep=lambda s: None, storage=storage
    )
    assert again.of("unchanged") == FINRA_DAYS
    assert ShortVolumeDaily.objects.count() == 3 * 7


def test_the_manifest_records_what_was_fetched() -> None:
    for folder in ("sec", "form4", "finra"):
        manifest = json.loads((RECORDED / folder / "manifest.json").read_text())
        assert manifest and all(item["url"].startswith("https://") for item in manifest)
        assert all(len(item["sha256"] or "00") in (2, 64) for item in manifest)
    weekend = [i for i in json.loads((RECORDED / "finra" / "manifest.json").read_text())]
    assert any(i["status"] == 403 and i["file"] is None for i in weekend)
