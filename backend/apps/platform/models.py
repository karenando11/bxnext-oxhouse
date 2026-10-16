"""Pipeline runs, provenance of every external call, data-quality checks and settings."""

from __future__ import annotations

from django.db import models

from apps.platform.reasons import reason_code_check


class PipelineRun(models.Model):
    """One execution of a pipeline stage."""

    class Stage(models.TextChoices):
        MARKET = "market", "Market snapshot and scoring"
        FILINGS = "filings", "Filings, trials, insider and short volume"

    class Trigger(models.TextChoices):
        SCHEDULE = "schedule", "Schedule"
        MANUAL = "manual", "Manual"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        OK = "ok", "Completed"
        PARTIAL = "partial", "Completed with failures"
        FAILED = "failed", "Failed"

    stage = models.CharField(max_length=16, choices=Stage.choices)
    as_of_date = models.DateField(help_text="Market date (US Eastern) the run describes.")
    trigger = models.CharField(max_length=16, choices=Trigger.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    code_ref = models.CharField(max_length=64, blank=True, help_text="Git commit of the code.")
    steps = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "One entry per step: step_name, started_at, finished_at, status, rows_in, rows_out, "
            "error_text."
        ),
    )

    class Meta:
        indexes = [models.Index(fields=["stage", "-as_of_date"], name="pipelinerun_stage_date")]


class SourceCall(models.Model):
    """Provenance of every external request. Everything downstream links here."""

    class Feed(models.TextChoices):
        FINVIZ = "finviz_elite", "Finviz Elite export"
        SEC_TICKERS = "sec_tickers", "SEC company tickers"
        SEC_COMPANYFACTS = "sec_companyfacts", "SEC company facts"
        SEC_SUBMISSIONS = "sec_submissions", "SEC submissions"
        CTGOV = "ctgov_studies", "ClinicalTrials.gov studies"
        FINRA = "finra_regsho", "FINRA Reg SHO short volume"
        SEC_FORM4 = "sec_form4", "SEC Form 4 documents"
        FIXTURE = "fixture", "Loaded fixture"

    class Status(models.TextChoices):
        OK = "ok", "OK"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    run = models.ForeignKey(PipelineRun, null=True, blank=True, on_delete=models.PROTECT)
    feed = models.CharField(max_length=32, choices=Feed.choices)
    scope_key = models.CharField(max_length=128, blank=True, help_text="CIK, sponsor or date.")
    requested_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    reason_code = models.CharField(max_length=40, null=True, blank=True)  # noqa: DJ001
    attempts = models.PositiveSmallIntegerField(default=1)
    bytes = models.BigIntegerField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    storage_uri = models.CharField(max_length=512, blank=True)
    error_text = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["feed", "scope_key", "-requested_at"], name="sourcecall_feed_scope"
            ),
            models.Index(fields=["run"], name="sourcecall_run"),
        ]
        constraints = [reason_code_check("reason_code", "sourcecall_reason_known")]


class DataQualityCheck(models.Model):
    """A check on a run's output, such as a column that is blank across the whole universe."""

    class Status(models.TextChoices):
        PASS = "pass", "Pass"
        WARN = "warn", "Warning"
        FAIL = "fail", "Fail"

    run = models.ForeignKey(PipelineRun, on_delete=models.PROTECT)
    check_name = models.CharField(max_length=64)
    scope = models.CharField(max_length=128, blank=True, help_text="For example finviz.eps_this.")
    expected = models.CharField(max_length=128, blank=True)
    observed = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["run", "status"], name="dqcheck_run_status")]


class Setting(models.Model):
    """Editable settings: default filters, maximum ages, schedule offsets, rates."""

    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField()
    updated_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
