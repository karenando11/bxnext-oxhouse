"""Load a golden day (a stored export of the existing system) into the database.

The golden export holds one day of Finviz inputs, runway and trial figures for the whole
universe. Loading it gives every later step real-shaped data before the live feeds exist.
Everything loaded is traceable to a ``fixture`` source call, and the run is marked so the
application can warn that it shows fixture data.

The loader is idempotent: loading the same files again changes nothing. Blank cells and ``-``
are stored as NULL, never zero. The golden export carries no scores to store; Step 7 recomputes
them and compares with the file.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.companies.models import Company, TickerHistory
from apps.marketdata.models import FinvizSnapshot
from apps.platform.models import PipelineRun, SourceCall
from apps.platform.reasons import REASON_CODES
from apps.sec.models import RunwayCalc
from apps.trials.models import CatalystSignal

FIXTURE_FEED = SourceCall.Feed.FIXTURE
FIXTURE_CODE_REF = "fixture"
SECTOR = "Healthcare"  # the export is the sec_healthcare universe

# Golden column -> FinvizSnapshot field, for numeric columns that carry over unchanged.
NUMERIC_COLUMNS: dict[str, str] = {
    "price": "price",
    "mcap": "mcap_musd",
    "shr_out": "shr_out_m",
    "shr_float": "shr_float_m",
    "avg_vol": "avg_vol_k",
    "rel_vol": "rel_vol",
    "perf_w": "perf_w",
    "perf_m": "perf_m",
    "perf_q": "perf_q",
    "perf_ytd": "perf_ytd",
    "vol_m": "vol_m",
    "beta": "beta",
    "sma200": "sma200",
    "w52h": "w52h",
    "w52l": "w52l",
    "rsi": "rsi",
    "pcash": "pcash",
    "quick_ratio": "quick_ratio",
    "cur_ratio": "cur_ratio",
    "td_eq": "td_eq",
    "cash_sh": "cash_sh",
    "inst_own": "inst_own",
    "short_float": "short_float",
    "eps_this": "eps_this",
    "eps_next": "eps_next",
}

# Notes in the runway export that are reason codes, mapped to ours.
NOTE_TO_REASON = {
    "company_cash_positive": "company_cash_positive",
    "operating_cash_flow_missing": "operating_cash_flow_missing",
    "phase_burn_only_no_xbrl": "phase_burn_only_no_xbrl",
    "fallback_to_rd_expense": "fallback_rd_expense",
    "cash_on_hand_missing": "cash_missing",
}

_EARNINGS = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


@dataclass
class LoadResult:
    companies: int = 0
    snapshots: int = 0
    runway_calcs: int = 0
    catalyst_signals: int = 0
    overlay_matched: int = 0
    overlay_with_cash_and_burn: int = 0
    overlay_unmatched_tickers: list[str] = field(default_factory=list)
    run_id: int | None = None


def number(raw: str | None) -> float | None:
    """A numeric cell, or None for blank, ``-`` and anything that is not a finite number."""
    text = (raw or "").strip().rstrip("%").replace(",", "")
    if not text or text == "-":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def integer(raw: str | None) -> int | None:
    value = number(raw)
    return None if value is None or value < 0 else int(value)


def iso_date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip()[:10])
    except ValueError:
        return None


def earnings_date(raw: str | None) -> date | None:
    """The date in an earnings text such as ``8/13/2026 8:30:00 AM``."""
    match = _EARNINGS.search(raw or "")
    if match is None:
        return None
    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_time(rows: list[dict[str, str]]) -> datetime:
    for row in rows:
        text = (row.get("snapshot_at") or "").strip()
        if text:
            try:
                return datetime.fromisoformat(text.replace(" ", "T", 1)).astimezone(UTC)
            except ValueError:
                continue
    return timezone.now()


def _fixture_call(path: Path, run: PipelineRun, requested_at: datetime) -> SourceCall:
    digest = _sha256(path)
    call, _ = SourceCall.objects.get_or_create(
        feed=FIXTURE_FEED,
        scope_key=path.name,
        content_hash=digest,
        defaults={
            "run": run,
            "requested_at": requested_at,
            "completed_at": timezone.now(),
            "status": SourceCall.Status.OK,
            "storage_uri": f"fixture://{path.name}",
            "bytes": path.stat().st_size,
        },
    )
    return call


def load_golden(export: Path, runway_export: Path | None = None) -> LoadResult:
    """Load the golden export and, when given, overlay the runway export. Safe to repeat."""
    rows = _read(export)
    result = LoadResult()
    snapshot_at = _snapshot_time(rows)
    day = snapshot_at.astimezone(_eastern()).date()
    with transaction.atomic():
        run, _ = PipelineRun.objects.get_or_create(
            stage=PipelineRun.Stage.MARKET,
            as_of_date=day,
            code_ref=FIXTURE_CODE_REF,
            defaults={
                "trigger": PipelineRun.Trigger.MANUAL,
                "started_at": snapshot_at,
                "finished_at": timezone.now(),
                "status": PipelineRun.Status.OK,
            },
        )
        result.run_id = run.pk
        call = _fixture_call(export, run, snapshot_at)
        companies = _load_companies(rows, day)
        result.companies = len(companies)
        result.snapshots = _load_snapshots(rows, companies, day, call)
        result.runway_calcs = _load_runways(rows, companies, day)
        result.catalyst_signals = _load_catalysts(rows, companies, day)
        if runway_export is not None:
            _fixture_call(runway_export, run, snapshot_at)  # provenance of the overlay file
            _overlay_runway(runway_export, companies, result)
    return result


def _eastern() -> Any:
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def _text(row: dict[str, str], column: str) -> str:
    return (row.get(column) or "").strip()


def _upsert(
    model: type[Any],
    existing: dict[Any, Any],
    wanted: dict[Any, dict[str, Any]],
    create_with: dict[Any, dict[str, Any]],
) -> None:
    """Create the missing objects and update the existing ones, in bulk."""
    to_create, to_update = [], []
    update_fields: set[str] = set()
    for key, values in wanted.items():
        obj = existing.get(key)
        if obj is None:
            to_create.append(model(**create_with[key], **values))
            continue
        for name, value in values.items():
            setattr(obj, name, value)
        update_fields.update(values)
        to_update.append(obj)
    if to_create:
        model.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        model.objects.bulk_update(to_update, sorted(update_fields), batch_size=500)


def _load_companies(rows: list[dict[str, str]], day: date) -> dict[str, Company]:
    tickers = [_text(row, "ticker") for row in rows]
    open_rows = TickerHistory.objects.select_related("company").filter(
        ticker__in=tickers, valid_to__isnull=True
    )
    companies = {row.ticker: row.company for row in open_rows}
    new_rows = [row for row in rows if _text(row, "ticker") not in companies]
    created = Company.objects.bulk_create(
        [
            Company(
                name=_text(row, "name") or _text(row, "ticker"),
                sector=SECTOR,
                industry=_text(row, "indication"),
                first_seen_date=day,
                last_seen_date=day,
            )
            for row in new_rows
        ],
        batch_size=500,
    )
    TickerHistory.objects.bulk_create(
        [
            TickerHistory(company=company, ticker=_text(row, "ticker"), valid_from=day)
            for company, row in zip(created, new_rows, strict=True)
        ],
        batch_size=500,
    )
    for company, row in zip(created, new_rows, strict=True):
        companies[_text(row, "ticker")] = company
    known = [company for row in rows if (company := companies[_text(row, "ticker")]) not in created]
    by_ticker = {_text(row, "ticker"): row for row in rows}
    for ticker, company in companies.items():
        if company in known:
            row = by_ticker[ticker]
            company.name = _text(row, "name") or company.name
            company.industry = _text(row, "indication")
            company.sector = SECTOR
            company.last_seen_date = max(company.last_seen_date, day)
    if known:
        Company.objects.bulk_update(
            known, ["name", "industry", "sector", "last_seen_date"], batch_size=500
        )
    return companies


def _load_snapshots(
    rows: list[dict[str, str]], companies: dict[str, Company], day: date, call: SourceCall
) -> int:
    existing = {
        s.company_id: s
        for s in FinvizSnapshot.objects.filter(as_of_date=day, company__in=companies.values())
    }
    wanted: dict[int, dict[str, Any]] = {}
    create_with: dict[int, dict[str, Any]] = {}
    for row in rows:
        company = companies[_text(row, "ticker")]
        text = _text(row, "earnings_date")
        wanted[company.pk] = {
            **{name: number(row.get(col)) for col, name in NUMERIC_COLUMNS.items()},
            "source_call": call,
            "company_name_raw": _text(row, "name"),
            "sector_raw": SECTOR,
            "industry_raw": _text(row, "indication"),
            "earnings_date_text": text,
            "earnings_date": earnings_date(text),
        }
        create_with[company.pk] = {"company": company, "as_of_date": day}
    _upsert(FinvizSnapshot, existing, wanted, create_with)
    return len(wanted)


def _load_runways(rows: list[dict[str, str]], companies: dict[str, Company], day: date) -> int:
    existing = {
        (r.company_id, r.calc_date): r
        for r in RunwayCalc.objects.filter(company__in=companies.values())
    }
    wanted: dict[tuple[int, date], dict[str, Any]] = {}
    create_with: dict[tuple[int, date], dict[str, Any]] = {}
    for row in rows:
        months = number(row.get("runway_months"))
        if months is None:
            continue
        company = companies[_text(row, "ticker")]
        key = (company.pk, iso_date(row.get("runway_as_of")) or day)
        wanted[key] = {
            "method_version": "fixture",
            "status": RunwayCalc.Status.OK,
            "runway_months": months,
            "runway_capped": _text(row, "runway_capped_derived") == "True",
        }
        create_with[key] = {"company": company, "calc_date": key[1]}
    _upsert(RunwayCalc, existing, wanted, create_with)
    return len(wanted)


_CATALYST_COLUMNS = (
    "catalyst_source",
    "catalyst_clinical_date",
    "ct_total_studies",
    "ct_latest_trial_nct",
    "ct_active_phase_2",
    "ct_active_phase_3",
)


def _load_catalysts(rows: list[dict[str, str]], companies: dict[str, Company], day: date) -> int:
    existing = {
        c.company_id: c
        for c in CatalystSignal.objects.filter(as_of_date=day, company__in=companies.values())
    }
    wanted: dict[int, dict[str, Any]] = {}
    create_with: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not any(_text(row, column) for column in _CATALYST_COLUMNS):
            continue
        company = companies[_text(row, "ticker")]
        source = _text(row, "catalyst_source")
        if source not in CatalystSignal.NextEventSource.values:
            source = CatalystSignal.NextEventSource.NONE
        wanted[company.pk] = {
            "rule_version": "fixture",
            "clinical_date": iso_date(row.get("catalyst_clinical_date")),
            "clinical_phase": _text(row, "catalyst_phase"),
            "clinical_overdue": _text(row, "catalyst_clinical_overdue") == "True",
            "fallback_end_raw": _text(row, "ct_next_trial_completion")
            or _text(row, "ct_latest_trial_completion"),
            "active_phase_2": integer(row.get("ct_active_phase_2")),
            "active_phase_3": integer(row.get("ct_active_phase_3")),
            "total_studies": integer(row.get("ct_total_studies")),
            "latest_trial_nct": _text(row, "ct_latest_trial_nct"),
            "latest_trial_status": _text(row, "ct_latest_trial_status"),
            "next_event_source": source,
        }
        create_with[company.pk] = {"company": company, "as_of_date": day}
    _upsert(CatalystSignal, existing, wanted, create_with)
    return len(wanted)


def _overlay_runway(path: Path, companies: dict[str, Company], result: LoadResult) -> None:
    """Add cash, monthly burn, burn source and notes to the runway rows already loaded."""
    latest: dict[int, RunwayCalc] = {}
    for stored in RunwayCalc.objects.filter(company__in=companies.values()).order_by("calc_date"):
        latest[stored.company_id] = stored  # the most recent calculation per company
    changed_companies: list[Company] = []
    changed_calcs: list[RunwayCalc] = []
    for row in _read(path):
        ticker = _text(row, "ticker")
        company = companies.get(ticker)
        if company is None:
            result.overlay_unmatched_tickers.append(ticker)
            continue
        cik = _text(row, "cik")
        if re.fullmatch(r"[0-9]{10}", cik) and company.cik != cik:
            company.cik = cik
            changed_companies.append(company)
        calc = latest.get(company.pk)
        if calc is None:
            continue
        cash, burn = number(row.get("cash_on_hand_usd")), number(row.get("monthly_burn_usd"))
        source, notes = _text(row, "burn_source"), _text(row, "notes")
        calc.cash_usd = None if cash is None else Decimal(str(round(cash, 2)))
        calc.burn_total_monthly_usd = None if burn is None else Decimal(str(round(burn, 2)))
        calc.burn_source = source if source in RunwayCalc.BurnSource.values else ""
        calc.notes = notes
        calc.reason_codes = sorted(
            {
                NOTE_TO_REASON[note]
                for note in notes.split(";")
                if NOTE_TO_REASON.get(note) in REASON_CODES
            }
        )
        changed_calcs.append(calc)
        result.overlay_matched += 1
        if cash is not None and burn is not None:
            result.overlay_with_cash_and_burn += 1
    if changed_companies:
        Company.objects.bulk_update(changed_companies, ["cik"], batch_size=500)
    if changed_calcs:
        RunwayCalc.objects.bulk_update(
            changed_calcs,
            ["cash_usd", "burn_total_monthly_usd", "burn_source", "notes", "reason_codes"],
            batch_size=500,
        )


def latest_run_is_fixture() -> bool | None:
    """True when the latest market run is a fixture run, False when live, None with no runs."""
    latest = (
        PipelineRun.objects.filter(stage=PipelineRun.Stage.MARKET)
        .order_by("-as_of_date", "-id")
        .first()
    )
    if latest is None:
        return None
    return SourceCall.objects.filter(run=latest, feed=FIXTURE_FEED).exists()
