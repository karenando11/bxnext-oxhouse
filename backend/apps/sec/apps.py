from __future__ import annotations

from django.apps import AppConfig


class SecConfig(AppConfig):
    name = "apps.sec"
    verbose_name = "SEC"
    default_auto_field = "django.db.models.BigAutoField"
