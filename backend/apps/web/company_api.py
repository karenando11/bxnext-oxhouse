"""JSON API for the company page (Step 18): ``GET /api/company/<ticker>``.

Requires sign-in. The page is one read of the ``company_page`` read model (the latest completed
score run) plus the read-time trial derivations from ``ct_study_version``. It touches no other
table. The response carries the blocks (header, tiles, scores, runway, catalyst, trials, short
selling, insider activity, footer), the stale banners, and ``fields``: all 55 page fields, each
with a value or a reason for its absence.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.trials.display import latest_studies
from apps.web.auth_views import api_login_required, error
from apps.web.company_page import build_page
from apps.web.models import CompanyPage
from apps.web.screener_api import market_today


@require_GET
@api_login_required
@never_cache
def company_page(request: HttpRequest, ticker: str) -> JsonResponse:
    row = CompanyPage.objects.filter(ticker__iexact=ticker).first()
    if row is None:
        return error(404, "unknown_ticker", f"No company page for {ticker.upper()}.")
    studies = latest_studies(row.company_id, row.as_of_date)
    return JsonResponse(build_page(row, studies, market_today()))
