"""Operational endpoints."""

from __future__ import annotations

import logging

from django.db import Error as DatabaseError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from apps.platform.golden import latest_run_is_fixture

logger = logging.getLogger(__name__)


@csrf_exempt  # stateless and read-only: non-GET requests get 405, not a CSRF 403
@require_GET
@never_cache
def healthz(request: HttpRequest) -> JsonResponse:
    """Report whether the application can reach its database.

    Returns 200 with the latest applied migration as the schema version, or 503
    when the database is unreachable. No connection details are exposed.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        recorder = MigrationRecorder(connection)
        latest = recorder.migration_qs.order_by("-applied", "-id").first()
        applied = recorder.migration_qs.count()
        fixture = latest_run_is_fixture()
    except DatabaseError:
        logger.exception("Health check could not reach the database")
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)

    schema_version = f"{latest.app}.{latest.name}" if latest else None
    return JsonResponse(
        {
            "status": "ok",
            "database": "ok",
            "schema_version": schema_version,
            "migrations_applied": applied,
            "data_source": "none" if fixture is None else "fixture" if fixture else "live",
        }
    )
