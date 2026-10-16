"""SEC filings, XBRL facts, runway and Form 4 insider activity."""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.companies.models import Company
from apps.platform.models import SourceCall
from apps.platform.reasons import reason_codes_check


class SecFiling(models.Model):
    """A filing from the submissions feed. Amendments are recorded so restatements show."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    accession = models.CharField(max_length=20, unique=True)
    form = models.CharField(max_length=16)
    filed_at = models.DateField()
    period_end = models.DateField(null=True, blank=True)
    is_amendment = models.BooleanField(default=False)
    source_call = models.ForeignKey(SourceCall, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        indexes = [models.Index(fields=["company", "-filed_at"], name="secfiling_company_filed")]


class SecFact(models.Model):
    """An allow-listed XBRL fact. ``filed_at`` gives true point-in-time reads."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    taxonomy = models.CharField(max_length=16)
    tag = models.CharField(max_length=128)
    unit = models.CharField(max_length=16)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField()
    value = models.DecimalField(max_digits=24, decimal_places=4, help_text="In the unit, USD.")
    accession = models.CharField(max_length=20)
    form = models.CharField(max_length=16, blank=True)
    fy = models.PositiveSmallIntegerField(null=True, blank=True)
    fp = models.CharField(max_length=4, blank=True)
    filed_at = models.DateField()
    source_call = models.ForeignKey(SourceCall, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "company",
                    "taxonomy",
                    "tag",
                    "unit",
                    "period_start",
                    "period_end",
                    "accession",
                ],
                name="secfact_natural_key",
                nulls_distinct=False,
            )
        ]
        indexes = [
            models.Index(fields=["company", "tag", "-period_end"], name="secfact_company_tag")
        ]


class RunwayCalc(models.Model):
    """Cash runway for one company and day, with every input stored so any number is traceable."""

    class Status(models.TextChoices):
        OK = "ok", "OK"
        PARTIAL = "partial", "Partial"
        UNAVAILABLE = "unavailable", "Unavailable"

    class BurnSource(models.TextChoices):
        OPERATING_CASH_FLOW = "operating_cash_flow", "Operating cash flow"
        RD_EXPENSE = "rd_expense", "R&D expense"
        PHASE_BURN_ONLY = "phase_burn_only", "Trial burn only"

    class CapReason(models.TextChoices):
        TINY_BURN = "tiny_burn", "Tiny burn"
        ZERO_BURN = "zero_burn", "Zero burn"

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    calc_date = models.DateField()
    method_version = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices)
    reason_codes = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    cash_usd = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    cash_tag = models.CharField(max_length=128, blank=True)
    cash_end_date = models.DateField(null=True, blank=True, help_text="Balance-sheet date.")
    ocf_usd = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)
    ocf_tag = models.CharField(max_length=128, blank=True)
    ocf_period_start = models.DateField(null=True, blank=True)
    ocf_period_end = models.DateField(null=True, blank=True)
    operating_burn_monthly_usd = models.DecimalField(
        max_digits=22, decimal_places=2, null=True, blank=True
    )
    trial_burn_monthly_usd = models.DecimalField(
        max_digits=22, decimal_places=2, null=True, blank=True
    )
    burn_total_monthly_usd = models.DecimalField(
        max_digits=22, decimal_places=2, null=True, blank=True
    )
    burn_source = models.CharField(max_length=24, choices=BurnSource.choices, blank=True)
    runway_months = models.FloatField(null=True, blank=True, help_text="Months, capped at 360.")
    runway_capped = models.BooleanField(default=False)
    cap_reason = models.CharField(max_length=16, choices=CapReason.choices, blank=True)
    financing_date = models.DateField(null=True, blank=True)
    source_fact_ids = models.JSONField(default=list, blank=True)
    trial_burn_lines = models.JSONField(
        default=list,
        blank=True,
        help_text="One entry per phase: phase, active_trial_count, monthly_rate_usd, amount_usd.",
    )
    notes = models.TextField(
        blank=True, help_text="Source notes exactly as delivered (semicolon separated)."
    )
    carried_forward = models.BooleanField(default=False)
    carried_from_date = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "calc_date"], name="runwaycalc_company_date"
            ),
            reason_codes_check("reason_codes", "runwaycalc_reasons_known"),
        ]


class SecForm4Filing(models.Model):
    """A Form 4 (or 4/A) discovered from the submissions feed."""

    class ParseStatus(models.TextChoices):
        OK = "ok", "Parsed"
        FAILED = "failed", "Failed"

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    accession = models.CharField(max_length=20, unique=True)
    form = models.CharField(max_length=8, help_text="4 or 4/A.")
    filed_at = models.DateField()
    period_of_report = models.DateField(null=True, blank=True)
    primary_document = models.CharField(max_length=255, blank=True)
    is_10b5_1 = models.BooleanField(default=False, help_text="Filing-level checkbox.")
    amends_accession = models.CharField(
        max_length=20, blank=True, help_text="For a 4/A, the filing it replaces."
    )
    parse_status = models.CharField(max_length=8, choices=ParseStatus.choices)
    parse_reason = models.CharField(max_length=255, blank=True)
    source_call = models.ForeignKey(SourceCall, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        indexes = [models.Index(fields=["company", "-filed_at"], name="form4filing_company_filed")]


class SecForm4Transaction(models.Model):
    """A non-derivative transaction line. ``amount_usd`` is shares times price, else NULL."""

    filing = models.ForeignKey(
        SecForm4Filing, on_delete=models.CASCADE, related_name="transactions"
    )
    line_no = models.PositiveSmallIntegerField()
    reporting_owner_cik = models.CharField(max_length=10)
    reporting_owner_name = models.CharField(max_length=255, blank=True)
    is_officer = models.BooleanField(default=False)
    is_director = models.BooleanField(default=False)
    is_ten_percent_owner = models.BooleanField(default=False)
    officer_title = models.CharField(max_length=255, blank=True)
    transaction_date = models.DateField()
    transaction_code = models.CharField(max_length=2)
    shares = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    price_per_share = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True, help_text="USD."
    )
    acquired_or_disposed = models.CharField(max_length=1, blank=True, help_text="A or D.")
    amount_usd = models.DecimalField(max_digits=22, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["filing", "line_no"], name="form4tx_filing_line")
        ]


class InsiderMetricDaily(models.Model):
    """Read-only view over the Form 4 tables for today's market date (see the migration)."""

    company = models.OneToOneField(Company, primary_key=True, on_delete=models.DO_NOTHING)
    as_of_date = models.DateField()
    window_days = models.IntegerField()
    filings_count = models.IntegerField(null=True)
    open_market_buys_count = models.IntegerField(null=True)
    open_market_sales_count = models.IntegerField(null=True)
    officers_directors_buying_count = models.IntegerField(null=True)
    officers_directors_selling_count = models.IntegerField(null=True)
    pct_10b5_1_dollars = models.FloatField(null=True, help_text="Percent of sale dollars.")
    status = models.CharField(max_length=24)
    reason_codes = ArrayField(models.CharField(max_length=40), default=list)
    age_days = models.IntegerField(null=True)
    is_stale = models.BooleanField()

    class Meta:
        managed = False
        db_table = "insider_metric_daily"
