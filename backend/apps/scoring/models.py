"""Formula versions, score runs and per-company score results."""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.companies.models import Company
from apps.platform.models import PipelineRun
from apps.sec.models import RunwayCalc
from apps.trials.models import CatalystSignal


class FormulaVersion(models.Model):
    """A versioned scoring configuration. Once a score run uses it, it cannot be changed.

    The database enforces this with a trigger (see the migration). ``config`` and ``quirks``
    are the plain data of ``scoring_engine.FormulaVersion``.
    """

    name = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    effective_from = models.DateTimeField()
    config = models.JSONField()
    quirks = models.JSONField()
    notes = models.TextField(blank=True)
    code_ref = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["name", "version"], name="formulaversion_name_version")
        ]


class ScoreRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        SUPERSEDED = "superseded", "Superseded by a later run for the same day"
        FAILED = "failed", "Failed"

    pipeline_run = models.ForeignKey(PipelineRun, null=True, blank=True, on_delete=models.PROTECT)
    as_of_date = models.DateField()
    scored_at = models.DateTimeField(help_text="The 'now' the scorer used.")
    formula_version = models.ForeignKey(FormulaVersion, on_delete=models.PROTECT)
    finviz_as_of_date = models.DateField(help_text="Date of the Finviz snapshot that was scored.")
    universe_n = models.PositiveIntegerField(default=0)
    scored_n = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    error_text = models.TextField(blank=True, help_text="Why the run failed.")
    peer_group_stats = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "For every group and dimension: peer_group_key, dimension, n_values, median, mad."
        ),
    )

    class Meta:
        indexes = [models.Index(fields=["status", "-as_of_date"], name="scorerun_status_date")]


class ScoreResult(models.Model):
    """Scores for one company in one run. All scores are stored unrounded."""

    score_run = models.ForeignKey(ScoreRun, on_delete=models.CASCADE, related_name="results")
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    mcap_bucket = models.CharField(max_length=8)
    peer_group_key = models.CharField(max_length=128)
    peer_n = models.PositiveIntegerField()
    is_sparse = models.BooleanField()
    cash_health = models.FloatField(help_text="0 to 10.")
    milestone = models.FloatField(help_text="0 to 10.")
    outlier = models.FloatField(help_text="0 to 10.")
    divergence = models.FloatField(help_text="Signed, unbounded.")
    recency = models.FloatField(help_text="1.0, 0.75, 0.5 or 0.3.")
    composite = models.FloatField(help_text="0 to 10.")
    runway_calc = models.ForeignKey(RunwayCalc, null=True, blank=True, on_delete=models.PROTECT)
    catalyst_signal = models.ForeignKey(
        CatalystSignal, null=True, blank=True, on_delete=models.PROTECT
    )
    carried_inputs = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    missing_inputs = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    inputs = models.JSONField(help_text="The exact dictionary the scorer received.")
    components = models.JSONField(help_text="Every intermediate term of every score.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["score_run", "company"], name="scoreresult_run_company")
        ]
        indexes = [
            models.Index(fields=["score_run", "-composite"], name="scoreresult_run_composite"),
            models.Index(fields=["company", "score_run"], name="scoreresult_company_run"),
        ]
