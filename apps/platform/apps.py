from __future__ import annotations

from django.apps import AppConfig


class PlatformConfig(AppConfig):
    name = "apps.platform"
    verbose_name = "Platform"
    default_auto_field = "django.db.models.BigAutoField"
