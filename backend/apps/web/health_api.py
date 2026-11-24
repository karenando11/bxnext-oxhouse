"""``GET /api/health``: the pipeline health report (Step 17). Requires sign-in."""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.platform.health import build_health
from apps.web.auth_views import api_login_required


@require_GET
@api_login_required
@never_cache
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse(build_health())
