"""API routes, mounted under ``/api/``."""

from __future__ import annotations

from django.urls import path

from apps.web import auth_views, screener_api

urlpatterns = [
    path("auth/csrf", auth_views.csrf, name="api-csrf"),
    path("auth/me", auth_views.me, name="api-me"),
    path("auth/login", auth_views.login_view, name="api-login"),
    path("auth/logout", auth_views.logout_view, name="api-logout"),
    path("screener/", screener_api.screener, name="api-screener"),
    path("screener/export.csv", screener_api.export_csv, name="api-screener-export"),
    path("screener/near-the-line", screener_api.near_line, name="api-near-the-line"),
    path("screener/funnel", screener_api.funnel, name="api-funnel"),
    path("screener/why/<str:ticker>", screener_api.why_excluded, name="api-why-excluded"),
    path("screens/", screener_api.screens, name="api-screens"),
    path("screens/<int:screen_id>", screener_api.screen_detail, name="api-screen"),
]
