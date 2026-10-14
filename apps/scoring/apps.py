from __future__ import annotations

from django.apps import AppConfig


class ScoringConfig(AppConfig):
    name = "apps.scoring"
    verbose_name = "Scoring"
    default_auto_field = "django.db.models.BigAutoField"
