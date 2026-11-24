"""Record real SEC and FINRA responses as test fixtures (F7, F11, F12). No database is touched.

    export SEC_USER_AGENT="Your Team you@example.com"
    uv run --project backend python scripts/record_fixtures.py sec  KTTA PACS BNTX --out DIR
    uv run --project backend python scripts/record_fixtures.py form4 KTTA PACS --out DIR
    uv run --project backend python scripts/record_fixtures.py finra --days 3 --out DIR

Every response is saved exactly as received, with a manifest.json beside the files (URL, status,
size, SHA-256, time). SEC requests go through the application's own client, so they carry the
declared User-Agent, keep 110 ms apart and retry politely. Nothing is retried or hammered if the
SEC refuses: a 403 or 429 is reported and the command stops.

Run it from the machine that will run production, because the SEC blocked a shared analyst IP.
The files are public data, but they are not committed by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from apps.marketdata.finra.client import file_url
from apps.sec.client import SecClient, SecFetchError, SecUserAgentMissing
from apps.sec.form4 import Form4ParseError, parse_form4
from apps.sec.parsing import is_form4, parse_company_tickers, parse_submissions
from apps.sec.urls import FACTS_URL, SUBMISSIONS_URL, TICKERS_URL, form4_document_url


class Recorder:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.manifest_path = out / "manifest.json"
        self.items: list[dict[str, Any]] = (
            json.loads(self.manifest_path.read_text())
            if self.manifest_path.exists()
            else []
        )

    def save(
        self, name: str, url: str, status: int, content: bytes, note: str = ""
    ) -> None:
        if content:
            (self.out / name).write_bytes(content)
        self.items = [i for i in self.items if i["file"] != name]
        self.items.append(
            {
                "file": name if content else None,
                "url": url,
                "status": status,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest() if content else None,
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "note": note,
            }
        )
        self.manifest_path.write_text(json.dumps(self.items, indent=2) + "\n")
        print(
            f"  {status} {name if content else '(no file)'} {len(content):>10,} bytes  {note}"
        )


def sec_client() -> SecClient:
    try:
        return SecClient()
    except SecUserAgentMissing as exc:
        sys.exit(f"{exc} Set it to a name and a contact address, then run again.")


def sec_get(client: SecClient, url: str) -> tuple[int, bytes]:
    """The body, or (404, b"") for a document the SEC does not have. Stops on a refusal."""
    try:
        got = client.get(url)
    except SecFetchError as exc:
        if exc.http_status == 404:
            return 404, b""
        sys.exit(
            f"The SEC refused or failed ({exc.reason_code}, HTTP {exc.http_status}): {url}"
        )
    return got.http_status, got.content


def cmd_sec(args: argparse.Namespace) -> None:
    """F7: company_tickers.json, and submissions and companyfacts for each ticker."""
    rec, client = Recorder(args.out), sec_client()
    print("company_tickers.json")
    status, tickers_raw = sec_get(client, TICKERS_URL)
    rec.save("company_tickers.json", TICKERS_URL, status, tickers_raw)
    mapping = parse_company_tickers(tickers_raw)
    for ticker in [t.upper() for t in args.tickers]:
        cik = mapping.get(ticker)
        if cik is None:
            print(f"{ticker}: no CIK in company_tickers.json (a good 'no CIK' case)")
            continue
        print(f"{ticker} (CIK {cik})")
        for kind, template in (
            ("submissions", SUBMISSIONS_URL),
            ("companyfacts", FACTS_URL),
        ):
            url = template.format(cik=cik)
            status, body = sec_get(client, url)
            note = "" if body else "the SEC has no document"
            rec.save(f"{kind}_{ticker}_CIK{cik}.json", url, status, body, note)
    print(
        "\nNote which ticker covers each case (IFRS filer, non-USD filer, 10-K/A filer):"
    )
    print(
        "  the manifest lists every file; open companyfacts to see us-gaap / ifrs-full / units."
    )


def cmd_form4(args: argparse.Namespace) -> None:
    """F12: recent Form 4 documents of the given tickers, with a summary of the cases covered."""
    rec, client = Recorder(args.out), sec_client()
    status, tickers_raw = sec_get(client, TICKERS_URL)
    mapping = parse_company_tickers(tickers_raw)
    seen: dict[str, list[str]] = {}
    for ticker in [t.upper() for t in args.tickers]:
        cik = mapping.get(ticker)
        if cik is None:
            print(f"{ticker}: no CIK, skipped")
            continue
        print(f"{ticker} (CIK {cik})")
        status, raw = sec_get(client, SUBMISSIONS_URL.format(cik=cik))
        entries = [e for e in parse_submissions(raw) if is_form4(e.form)]
        entries.sort(key=lambda e: e.filed_at, reverse=True)
        for e in entries[: args.per_ticker]:
            url = form4_document_url(cik, e.accession, e.primary_document)
            status, body = sec_get(client, url)
            note = ""
            if body:
                try:
                    p = parse_form4(body)
                    codes = sorted({t.code for t in p.transactions}) or [
                        "no non-derivative lines"
                    ]
                    note = (
                        f"{p.document_type}; owners={len(p.owners)}; codes={','.join(codes)}; "
                        f"10b5-1={p.is_10b5_1}; issuer={'this company' if p.issuer_cik == cik else p.issuer_cik}"
                    )
                    for label in _cases(p, cik):
                        seen.setdefault(label, []).append(e.accession)
                except Form4ParseError as exc:
                    note = f"does not parse: {exc}"
                    seen.setdefault("unparseable", []).append(e.accession)
            rec.save(f"form4_{ticker}_{e.accession}.xml", url, status, body, note)
    print("\nCases found (F12 wants all of these):")
    wanted = [
        "purchase", "sale under a 10b5-1 plan", "sale not under a plan",
        "multi-owner", "gift (G)", "amendment (4/A)", "unparseable",
    ]  # fmt: skip
    for label in wanted:
        print(
            f"  {'yes' if label in seen else 'NOT YET'}  {label}  {seen.get(label, [''])[0]}"
        )
    print("Add more tickers, or raise --per-ticker, until every case has a file.")


def _cases(p: Any, cik: str) -> list[str]:
    labels = []
    codes = {t.code for t in p.transactions}
    if "P" in codes:
        labels.append("purchase")
    if "S" in codes:
        labels.append(
            "sale under a 10b5-1 plan" if p.is_10b5_1 else "sale not under a plan"
        )
    if len(p.owners) > 1:
        labels.append("multi-owner")
    if "G" in codes:
        labels.append("gift (G)")
    if p.document_type == "4/A":
        labels.append("amendment (4/A)")
    return labels


def cmd_finra(args: argparse.Namespace) -> None:
    """F11: the latest consecutive daily files, and a weekend date (which has no file)."""
    rec = Recorder(args.out)
    found: list[date] = []
    day = date.today() - timedelta(days=1)
    weekend: date | None = None
    for _ in range(args.lookback):
        if len(found) >= args.days and weekend is not None:
            break
        url = file_url(day)
        if day.weekday() >= 5:
            if weekend is None:
                status, body = _finra_get(url)
                rec.save(
                    f"CNMSshvol{day:%Y%m%d}.txt",
                    url,
                    status,
                    body,
                    "weekend: expect no file",
                )
                weekend = day
        elif len(found) < args.days:
            status, body = _finra_get(url)
            if status == 200:
                rec.save(f"CNMSshvol{day:%Y%m%d}.txt", url, status, body)
                found.append(day)
            else:
                rec.save(
                    f"CNMSshvol{day:%Y%m%d}.txt",
                    url,
                    status,
                    body,
                    "no file (holiday?)",
                )
        day -= timedelta(days=1)
        time.sleep(1.0)
    print(f"\nFiles for {len(found)} trading days; weekend date: {weekend}.")
    print(
        "The manifest records the status FINRA gave for dates with no file (403 or 404)."
    )
    print(
        "For the 'universe ticker missing from a file' case, no recording is needed: pick a"
    )
    print("ticker that is in the universe but absent from one of these files.")


def _finra_get(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "oxhouse-research/0.1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, helptext in (
        ("sec", cmd_sec, "F7: tickers file, submissions and companyfacts"),
        ("form4", cmd_form4, "F12: recent Form 4 documents"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("tickers", nargs="+")
        p.add_argument("--out", type=Path, required=True)
        if name == "form4":
            p.add_argument("--per-ticker", type=int, default=15)
        p.set_defaults(fn=fn)
    p = sub.add_parser("finra", help="F11: daily short-volume files")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--lookback", type=int, default=14)
    p.set_defaults(fn=cmd_finra)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
