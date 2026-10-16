"""Screen definitions, daily screen runs and the funnel log."""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.companies.models import Company
from apps.scoring.models import ScoreRun


class ScreenDefinition(models.Model):
    """A saved set of filters. ``config`` holds market cap min and max (USD millions), price min,
    average volume min (thousands of shares), shares outstanding max (millions), composite floor,
    outlier floor and whether sparse peer groups are included."""

    name = models.CharField(max_length=100)
    is_default = models.BooleanField(default=False)
    config = models.JSONField()
    created_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["is_default"],
                condition=models.Q(is_default=True),
                name="screendefinition_one_default",
            ),
            models.UniqueConstraint(
                fields=["name", "created_by"], name="screendefinition_name_owner"
            ),
        ]


class ScreenRun(models.Model):
    score_run = models.ForeignKey(ScoreRun, on_delete=models.PROTECT)
    definition = models.ForeignKey(ScreenDefinition, on_delete=models.PROTECT)
    filter_config = models.JSONField(help_text="Snapshot of the filters used.")
    universe_n = models.PositiveIntegerField()
    passed_n = models.PositiveIntegerField()
    executed_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["score_run", "definition"], name="screenrun_run_definition")
        ]


class ScreenMember(models.Model):
    """One company in a screen run, passed or excluded, with every failed filter."""

    screen_run = models.ForeignKey(ScreenRun, on_delete=models.CASCADE, related_name="members")
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    passed = models.BooleanField()
    excluded_reasons = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    rank = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["screen_run", "company"], name="screenmember_run_company"
            ),
            models.CheckConstraint(
                condition=models.Q(passed=True, excluded_reasons=[]) | models.Q(passed=False),
                name="screenmember_passed_has_no_reasons",
            ),
        ]
        indexes = [
            models.Index(fields=["screen_run", "passed", "rank"], name="screenmember_run_rank")
        ]


class FunnelEvent(models.Model):
    """Who entered or left the list, and why."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    as_of_date = models.DateField()
    from_state = models.CharField(max_length=24)
    to_state = models.CharField(max_length=24)
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["company", "-as_of_date"], name="funnelevent_company_date"),
            models.Index(fields=["as_of_date"], name="funnelevent_date"),
        ]
