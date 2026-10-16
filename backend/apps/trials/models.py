"""ClinicalTrials.gov studies and the catalyst signal derived from them."""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.companies.models import Company
from apps.platform.models import SourceCall
from apps.platform.reasons import reason_codes_check


class CtStudyVersion(models.Model):
    """A study as observed on a day. A new row is written only when the content changes."""

    class Precision(models.TextChoices):
        DAY = "day", "Day"
        MONTH = "month", "Month"

    nct_id = models.CharField(max_length=16)
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    sponsor_name = models.CharField(max_length=255, blank=True)
    match_score = models.FloatField(null=True, blank=True, help_text="0 to 1 sponsor similarity.")
    observed_on = models.DateField()
    content_hash = models.CharField(max_length=64)
    phases = models.JSONField(default=list, blank=True)
    highest_phase = models.CharField(max_length=16, blank=True)
    status = models.CharField(max_length=32, blank=True)
    study_type = models.CharField(max_length=32, blank=True)
    primary_completion_raw = models.CharField(max_length=16, blank=True)
    primary_completion_date = models.DateField(null=True, blank=True)
    primary_completion_precision = models.CharField(
        max_length=8, choices=Precision.choices, blank=True
    )
    completion_raw = models.CharField(max_length=16, blank=True)
    completion_date = models.DateField(null=True, blank=True)
    last_update_posted = models.DateField(null=True, blank=True)
    title = models.TextField(blank=True)
    conditions = models.JSONField(default=list, blank=True)
    interventions = models.JSONField(default=list, blank=True)
    source_call = models.ForeignKey(SourceCall, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["nct_id", "content_hash"], name="ctstudy_nct_hash")
        ]
        indexes = [
            models.Index(fields=["company", "nct_id", "-observed_on"], name="ctstudy_company_nct")
        ]


class CatalystSignal(models.Model):
    """What the scorer consumes: the chosen catalyst date and trial counts for one day."""

    class NextEventSource(models.TextChoices):
        CLINICAL = "clinical", "Clinical"
        FINANCING = "financing", "Financing"
        NONE = "none", "None"

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    as_of_date = models.DateField()
    rule_version = models.CharField(max_length=32)
    clinical_date = models.DateField(null=True, blank=True)
    clinical_phase = models.CharField(max_length=16, blank=True)
    clinical_overdue = models.BooleanField(default=False)
    clinical_source_nct = models.CharField(max_length=16, blank=True)
    fallback_end_raw = models.CharField(max_length=16, blank=True)
    fallback_end_nct = models.CharField(max_length=16, blank=True)
    active_phase_2 = models.PositiveIntegerField(null=True, blank=True)
    active_phase_3 = models.PositiveIntegerField(null=True, blank=True)
    total_studies = models.PositiveIntegerField(null=True, blank=True)
    latest_trial_nct = models.CharField(max_length=16, blank=True)
    latest_trial_status = models.CharField(max_length=32, blank=True)
    financing_date = models.DateField(null=True, blank=True)
    next_event_source = models.CharField(
        max_length=16, choices=NextEventSource.choices, default=NextEventSource.NONE
    )
    candidates = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Every considered study: nct_id, highest_phase, status, date_value, date_precision, "
            "eligible, reject_reason, chosen."
        ),
    )
    status = models.CharField(max_length=16, default="ok")
    reason_codes = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    carried_forward = models.BooleanField(default=False)
    carried_from_date = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "as_of_date"], name="catalystsignal_company_date"
            ),
            reason_codes_check("reason_codes", "catalystsignal_reasons_known"),
        ]
