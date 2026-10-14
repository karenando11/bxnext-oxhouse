from __future__ import annotations

from django.apps import AppConfig


class WebConfig(AppConfig):
    name = "apps.web"
    verbose_name = "Web"
    default_auto_field = "django.db.models.BigAutoField"
