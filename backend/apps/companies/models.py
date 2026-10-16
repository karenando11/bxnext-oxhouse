"""Company identity. Tickers are not keys: a company keeps one id across ticker changes."""

from __future__ import annotations

from django.db import models


class Company(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DELISTED = "delisted", "Delisted"
        UNMAPPED = "unmapped", "Unmapped"

    name = models.CharField(max_length=255)
    sector = models.CharField(max_length=100, blank=True)
    industry = models.CharField(max_length=100, blank=True)
    cik = models.CharField(max_length=10, null=True, blank=True)  # noqa: DJ001
    country = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    is_adr = models.BooleanField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    first_seen_date = models.DateField()
    last_seen_date = models.DateField()

    class Meta:
        verbose_name_plural = "companies"
        constraints = [
            models.UniqueConstraint(
                fields=["cik"], condition=models.Q(cik__isnull=False), name="company_cik_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(cik__isnull=True) | models.Q(cik__regex=r"^[0-9]{10}$"),
                name="company_cik_ten_digits",
            ),
        ]


class TickerHistory(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="tickers")
    ticker = models.CharField(max_length=16)
    exchange = models.CharField(max_length=16, blank=True)
    valid_from = models.DateField()
    valid_to = models.DateField(
        null=True, blank=True, help_text="Empty while the ticker is current."
    )

    class Meta:
        verbose_name_plural = "ticker history"
        constraints = [
            models.UniqueConstraint(
                fields=["ticker"],
                condition=models.Q(valid_to__isnull=True),
                name="tickerhistory_one_open_per_ticker",
            ),
        ]
        indexes = [models.Index(fields=["ticker"], name="tickerhistory_ticker")]


class CompanySponsorAlias(models.Model):
    """Names a company is known by in ClinicalTrials.gov. Manual overrides win."""

    class Source(models.TextChoices):
        DERIVED = "derived", "Derived from the company name"
        SEC_FORMER_NAME = "sec_former_name", "SEC former name"
        MANUAL = "manual", "Manual"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="sponsor_aliases")
    sponsor_name = models.CharField(max_length=255)
    source = models.CharField(max_length=24, choices=Source.choices)
    match_score = models.FloatField(null=True, blank=True, help_text="0 to 1 similarity.")
    is_override = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "sponsor_name", "source"], name="sponsoralias_unique"
            )
        ]
