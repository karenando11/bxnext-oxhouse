"""Sign-in for the API: session cookie plus CSRF protection.

A single-page frontend calls ``GET /api/auth/csrf`` once (it sets the CSRF cookie), then posts
credentials to ``/api/auth/login`` and sends the ``X-CSRFToken`` header on every unsafe request.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

LOGIN_URL = "/api/auth/login"


def error(status: int, code: str, message: str, **extra: Any) -> JsonResponse:
    return JsonResponse({"error": code, "message": message, **extra}, status=status)


def api_login_required(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Anonymous requests get 401 with the sign-in URL (an API has nothing to redirect to)."""

    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return error(
                401, "authentication_required", "Sign in to continue.", login_url=LOGIN_URL
            )
        return view(request, *args, **kwargs)

    return wrapped


def read_json(request: HttpRequest) -> dict[str, Any] | None:
    """The JSON object in the request body, or None when it is missing or malformed."""
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@require_GET
@ensure_csrf_cookie
@never_cache
def csrf(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"csrf": "cookie set"})


@require_GET
@never_cache
def me(request: HttpRequest) -> JsonResponse:
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False})
    return JsonResponse({"authenticated": True, "username": request.user.get_username()})


@require_POST
@never_cache
def login_view(request: HttpRequest) -> JsonResponse:
    body = read_json(request)
    if body is None:
        return error(400, "bad_request", "The body must be a JSON object.")
    username, password = body.get("username"), body.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return error(400, "bad_request", "username and password are required.")
    user = authenticate(request, username=username, password=password)
    if user is None:
        return error(401, "invalid_credentials", "Wrong username or password.")
    login(request, user)
    return JsonResponse({"authenticated": True, "username": user.get_username()})


@require_http_methods(["POST"])
@never_cache
def logout_view(request: HttpRequest) -> JsonResponse:
    logout(request)
    return JsonResponse({"authenticated": False})
