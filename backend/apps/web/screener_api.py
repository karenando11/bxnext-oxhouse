"""JSON API for the screener page.

Every endpoint requires sign-in. The table, the CSV export, why-excluded and near-the-line read
only the ``company_page`` read model (the latest completed score run); filters run live in memory
over its ~1,000 rows, so nothing is recomputed. Saved screens and the funnel log have their own
stores.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from apps.companies.models import TickerHistory
from apps.platform.models import Setting
from apps.screener.filters import (
    DEFAULT_NEAR_LINE_MARGIN,
    ScreenConfig,
    ScreenConfigError,
    ScreenOutcome,
    ScreenRow,
    explain_failures,
    near_the_line,
    screen,
)
from apps.screener.models import FunnelEvent, ScreenDefinition
from apps.web.auth_views import api_login_required, error, read_json
from apps.web.models import CompanyPage
from scoring_engine import display_score

MARKET_TIMEZONE = ZoneInfo("America/New_York")
DEFAULT_STALE_DAYS = 3
MAX_FUNNEL_DAYS = 365
NUMERIC_FILTERS = (
    "mcap_min_musd",
    "mcap_max_musd",
    "price_min",
    "avg_vol_min_k",
    "shares_outstanding_max_m",
    "composite_floor",
    "outlier_floor",
)
OFF_WORDS = {"", "null", "none", "off"}
CSV_COLUMNS = (
    "rank",
    "ticker",
    "company",
    "industry",
    "mcap_musd",
    "price",
    "composite",
    "cash_health",
    "milestone",
    "outlier",
    "peer_group",
    "next_catalyst_date",
    "next_catalyst_source",
    "runway_months",
)
PAGE_FIELDS = (
    "company_id", "ticker", "company_name", "industry", "mcap_musd", "price", "avg_vol_k",
    "shr_out_m", "composite", "cash_health", "milestone", "outlier", "recency",
    "peer_group_key", "is_sparse", "runway_months", "clinical_date", "clinical_phase",
    "clinical_overdue", "next_event_source", "financing_date", "as_of_date", "is_fixture",
    "score_run_id",
)  # fmt: skip


def market_today() -> date:
    return datetime.now(MARKET_TIMEZONE).date()


@dataclass(frozen=True)
class Page:
    """The read model, loaded once per request."""

    rows: list[ScreenRow]
    details: dict[int, dict[str, Any]]
    as_of_date: date
    is_fixture: bool
    score_run_id: int


def load_page() -> Page | None:
    records = list(CompanyPage.objects.values(*PAGE_FIELDS))
    if not records:
        return None
    rows = [
        ScreenRow(
            company_id=r["company_id"],
            ticker=r["ticker"] or str(r["company_id"]),
            composite=r["composite"],
            outlier=r["outlier"],
            is_sparse=r["is_sparse"],
            mcap_musd=r["mcap_musd"],
            price=r["price"],
            avg_vol_k=r["avg_vol_k"],
            shr_out_m=r["shr_out_m"],
        )
        for r in records
    ]
    first = records[0]
    return Page(
        rows=rows,
        details={r["company_id"]: r for r in records},
        as_of_date=first["as_of_date"],
        is_fixture=first["is_fixture"],
        score_run_id=first["score_run_id"],
    )


# ------------------------------------------------------------------------------- filters


def default_definition() -> ScreenDefinition | None:
    return ScreenDefinition.objects.filter(is_default=True).first()


def parse_config(request: HttpRequest) -> ScreenConfig:
    """Filters from the query string over a base: a saved screen (``screen=<id>``) or the default.

    A filter given as empty, ``null``, ``none`` or ``off`` is switched off. Raises
    ``ScreenConfigError`` naming the bad key.
    """
    base_plain: dict[str, Any] = {}
    screen_id = request.GET.get("screen")
    if screen_id:
        saved = ScreenDefinition.objects.filter(pk=screen_id if screen_id.isdigit() else -1).first()
        if saved is None:
            raise ScreenConfigError("screen: no such saved screen")
        base_plain = dict(saved.config)
    else:
        default = default_definition()
        base_plain = dict(default.config) if default else {}
    overrides: dict[str, Any] = {}
    for name in NUMERIC_FILTERS:
        if name in request.GET:
            raw = request.GET[name].strip()
            if raw.lower() in OFF_WORDS:
                overrides[name] = None
            else:
                try:
                    overrides[name] = float(raw)
                except ValueError as exc:
                    raise ScreenConfigError(f"{name}: expected a number") from exc
    if "include_sparse" in request.GET:
        raw = request.GET["include_sparse"].strip().lower()
        if raw not in {"true", "false"}:
            raise ScreenConfigError("include_sparse: expected true or false")
        overrides["include_sparse"] = raw == "true"
    return ScreenConfig.from_plain({**base_plain, **overrides})


def bad_filters(exc: ScreenConfigError) -> JsonResponse:
    return error(400, "invalid_filters", str(exc))


# --------------------------------------------------------------------------- payloads


def freshness(page: Page) -> dict[str, Any]:
    stale_days = _stale_days()
    age = (market_today() - page.as_of_date).days
    stale = age > stale_days
    return {
        "as_of_date": page.as_of_date.isoformat(),
        "age_days": age,
        "stale": stale,
        "stale_banner": (
            f"Market data is stale: the latest score run is {age} days old." if stale else None
        ),
        "is_fixture": page.is_fixture,
    }


def _stale_days() -> int:
    setting = (
        Setting.objects.filter(key="stale.snapshot_days").values_list("value", flat=True).first()
    )
    return (
        setting
        if isinstance(setting, int) and not isinstance(setting, bool)
        else DEFAULT_STALE_DAYS
    )


def _display(value: float, name: str) -> float:
    return display_score(name, value)


def row_payload(outcome: ScreenOutcome, page: Page) -> dict[str, Any]:
    row, d = outcome.row, page.details[outcome.row.company_id]
    return {
        "rank": outcome.rank,
        "company_id": row.company_id,
        "ticker": row.ticker,
        "company": d["company_name"],
        "industry": d["industry"],
        "mcap_musd": row.mcap_musd,
        "price": row.price,
        "composite": row.composite,
        "cash_health": d["cash_health"],
        "milestone": d["milestone"],
        "outlier": row.outlier,
        "display": {
            "composite": _display(row.composite, "composite"),
            "cash_health": _display(d["cash_health"], "cash_health"),
            "milestone": _display(d["milestone"], "milestone"),
            "outlier": _display(row.outlier, "outlier"),
        },
        "peer_group": d["peer_group_key"],
        "next_catalyst": {
            "date": d["clinical_date"].isoformat() if d["clinical_date"] else None,
            "phase": d["clinical_phase"] or None,
            "overdue": d["clinical_overdue"],
            "source": d["next_event_source"],
            "financing_date": d["financing_date"].isoformat() if d["financing_date"] else None,
        },
        "runway_months": d["runway_months"],
    }


def failure_payload(outcome: ScreenOutcome, config: ScreenConfig) -> list[dict[str, Any]]:
    return [
        {
            "reason": f.reason,
            "filter": f.filter,
            "actual": f.actual,
            "threshold": f.threshold,
            "comparison": f.comparison,
        }
        for f in explain_failures(outcome.row, config)
    ]


def empty_response() -> JsonResponse:
    return JsonResponse(
        {"empty": True, "count": 0, "rows": [], "message": "No completed score run."}
    )


# --------------------------------------------------------------------------------- views


@require_GET
@api_login_required
@never_cache
def screener(request: HttpRequest) -> JsonResponse:
    try:
        config = parse_config(request)
    except ScreenConfigError as exc:
        return bad_filters(exc)
    page = load_page()
    if page is None:
        return empty_response()
    passers = [o for o in screen(page.rows, config) if o.passed]
    return JsonResponse(
        {
            **freshness(page),
            "filters": config.to_plain(),
            "universe_n": len(page.rows),
            "count": len(passers),
            "rows": [row_payload(o, page) for o in passers],
        }
    )


def _safe_cell(value: Any) -> Any:
    """Stop spreadsheet formula injection: text starting with = + - @ is prefixed with a quote."""
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


@require_GET
@api_login_required
@never_cache
def export_csv(request: HttpRequest) -> HttpResponse:
    try:
        config = parse_config(request)
    except ScreenConfigError as exc:
        return bad_filters(exc)
    page = load_page()
    if page is None:
        return empty_response()
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="screener_{page.as_of_date}.csv"'
    writer = csv.writer(response)
    writer.writerow(CSV_COLUMNS)
    for outcome in screen(page.rows, config):
        if not outcome.passed:
            continue
        r = row_payload(outcome, page)
        writer.writerow(
            [
                _safe_cell(value)
                for value in (
                    r["rank"],
                    r["ticker"],
                    r["company"],
                    r["industry"],
                    r["mcap_musd"],
                    r["price"],
                    r["composite"],
                    r["cash_health"],
                    r["milestone"],
                    r["outlier"],
                    r["peer_group"],
                    r["next_catalyst"]["date"],
                    r["next_catalyst"]["source"],
                    r["runway_months"],
                )
            ]  # fmt: skip
        )
    return response


@require_GET
@api_login_required
@never_cache
def why_excluded(request: HttpRequest, ticker: str) -> JsonResponse:
    try:
        config = parse_config(request)
    except ScreenConfigError as exc:
        return bad_filters(exc)
    page = load_page()
    if page is None:
        return empty_response()
    wanted = ticker.strip().upper()
    outcomes = screen(page.rows, config)
    outcome = next((o for o in outcomes if o.row.ticker.upper() == wanted), None)
    if outcome is None:
        return error(404, "unknown_ticker", f"{ticker} is not in the universe.")
    return JsonResponse(
        {
            **freshness(page),
            "ticker": outcome.row.ticker,
            "company": page.details[outcome.row.company_id]["company_name"],
            "passed": outcome.passed,
            "rank": outcome.rank,
            "filters": config.to_plain(),
            "failures": failure_payload(outcome, config),
        }
    )


@require_GET
@api_login_required
@never_cache
def near_line(request: HttpRequest) -> JsonResponse:
    try:
        config = parse_config(request)
        margin = float(request.GET.get("margin", DEFAULT_NEAR_LINE_MARGIN))
    except ScreenConfigError as exc:
        return bad_filters(exc)
    except ValueError:
        return error(400, "invalid_filters", "margin: expected a number")
    page = load_page()
    if page is None:
        return empty_response()
    near = near_the_line(screen(page.rows, config), config, margin)
    return JsonResponse(
        {
            **freshness(page),
            "filters": config.to_plain(),
            "margin": margin,
            "count": len(near),
            "rows": [
                {**row_payload(o, page), "failures": failure_payload(o, config)} for o in near
            ],
        }
    )


@require_GET
@api_login_required
@never_cache
def funnel(request: HttpRequest) -> JsonResponse:
    raw = request.GET.get("days", "30")
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_FUNNEL_DAYS:
        return error(
            400, "invalid_filters", f"days: expected a whole number from 1 to {MAX_FUNNEL_DAYS}"
        )
    latest = (
        FunnelEvent.objects.order_by("-as_of_date").values_list("as_of_date", flat=True).first()
    )
    if latest is None:
        return JsonResponse({"days": []})
    since = date.fromordinal(latest.toordinal() - int(raw) + 1)
    events = list(
        FunnelEvent.objects.filter(as_of_date__gte=since)
        .select_related("company")
        .order_by("-as_of_date", "company_id")
    )
    tickers = dict(
        TickerHistory.objects.filter(
            company_id__in={e.company_id for e in events}, valid_to__isnull=True
        ).values_list("company_id", "ticker")
    )
    days: dict[date, dict[str, Any]] = {}
    for event in events:
        entry = days.setdefault(
            event.as_of_date, {"date": event.as_of_date.isoformat(), "entered": [], "left": []}
        )
        item = {
            "ticker": tickers.get(event.company_id, str(event.company_id)),
            "company": event.company.name,
            "from": event.from_state,
            "to": event.to_state,
            "reason": event.reason,
        }
        entry["entered" if event.to_state == "passed" else "left"].append(item)
    for entry in days.values():
        entry["entered"].sort(key=lambda i: i["ticker"])
        entry["left"].sort(key=lambda i: i["ticker"])
    return JsonResponse({"days": list(days.values())})


# ------------------------------------------------------------------------ saved screens


def screen_payload(definition: ScreenDefinition) -> dict[str, Any]:
    return {
        "id": definition.pk,
        "name": definition.name,
        "owner": definition.created_by,
        "is_default": definition.is_default,
        "filters": definition.config,
        "updated_at": definition.updated_at.isoformat(),
    }


@require_http_methods(["GET", "POST"])
@api_login_required
@never_cache
def screens(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        listed = ScreenDefinition.objects.order_by("-is_default", "name", "id")
        return JsonResponse({"screens": [screen_payload(s) for s in listed]})
    body = read_json(request)
    if body is None:
        return error(400, "bad_request", "The body must be a JSON object.")
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        return error(400, "invalid_name", "name: expected 1 to 100 characters.")
    try:
        config = ScreenConfig.from_plain(body.get("filters") or {})
    except ScreenConfigError as exc:
        return bad_filters(exc)
    try:
        with transaction.atomic():
            definition = ScreenDefinition.objects.create(
                name=name.strip(),
                config=config.to_plain(),
                created_by=request.user.get_username(),
            )
    except IntegrityError:
        return error(409, "name_taken", "You already have a saved screen with that name.")
    return JsonResponse(screen_payload(definition), status=201)


@require_http_methods(["GET", "DELETE"])
@api_login_required
@never_cache
def screen_detail(request: HttpRequest, screen_id: int) -> JsonResponse:
    definition = ScreenDefinition.objects.filter(pk=screen_id).first()
    if definition is None:
        return error(404, "not_found", "No such saved screen.")
    if request.method == "GET":
        return JsonResponse(screen_payload(definition))
    if definition.is_default:
        return error(403, "forbidden", "The default screen cannot be deleted.")
    if definition.created_by != request.user.get_username():
        return error(403, "forbidden", "Only the owner can delete a saved screen.")
    definition.delete()
    return JsonResponse({"deleted": screen_id})
