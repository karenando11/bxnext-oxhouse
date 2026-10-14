from __future__ import annotations

from django.apps import AppConfig


class MarketdataConfig(AppConfig):
    name = "apps.marketdata"
    verbose_name = "Market data"
    default_auto_field = "django.db.models.BigAutoField"
