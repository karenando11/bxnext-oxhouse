from __future__ import annotations

from django.apps import AppConfig


class CompaniesConfig(AppConfig):
    name = "apps.companies"
    verbose_name = "Companies"
    default_auto_field = "django.db.models.BigAutoField"
