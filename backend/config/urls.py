"""Root URL configuration."""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

from apps.platform.views import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("api/", include("apps.web.urls")),
    path("admin/", admin.site.urls),
]
