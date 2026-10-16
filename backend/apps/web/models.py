"""The read model: one row per company for the latest completed score run.

The API reads only this, never the base tables. The view is defined in the migration. Each of the
page's 55 fields is a typed column here or is derived from one at read time (runway in quarters,
runway rating, and the trial lists derived from ``CtStudyVersion``).
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.companies.models import Company


class CompanyPage(models.Model):
    company = models.OneToOneField(
        Company, primary_key=True, on_delete=models.DO_NOTHING, related_name="page"
    )
    ticker = models.CharField(max_length=16, null=True)  # noqa: DJ001
    company_name = models.CharField(max_length=255)
    industry = models.CharField(max_length=100)
    score_run_id = models.BigIntegerField()
    as_of_date = models.DateField(help_text="Date of the score run.")

    # Scores
    composite = models.FloatField()
    divergence = models.FloatField()
    cash_health = models.FloatField()
    milestone = models.FloatField()
    outlier = models.FloatField()
    recency = models.FloatField()
    mcap_bucket = models.CharField(max_length=8)
    peer_group_key = models.CharField(max_length=128)
    peer_n = models.IntegerField()

    # Finviz
    price = models.FloatField(null=True)
    mcap_musd = models.FloatField(null=True)
    w52h = models.FloatField(null=True)
    sma200 = models.FloatField(null=True)
    rsi = models.FloatField(null=True)
    avg_vol_k = models.FloatField(null=True)
    rel_vol = models.FloatField(null=True)
    perf_w = models.FloatField(null=True)
    perf_m = models.FloatField(null=True)
    perf_q = models.FloatField(null=True)
    perf_ytd = models.FloatField(null=True)
    cash_sh = models.FloatField(null=True)
    quick_ratio = models.FloatField(null=True)
    td_eq = models.FloatField(null=True)
    inst_trans = models.FloatField(null=True)
    short_float = models.FloatField(null=True)
    insider_own = models.FloatField(null=True)
    gross_margin = models.FloatField(null=True)
    op_margin = models.FloatField(null=True)
    earnings_date_text = models.CharField(max_length=64, null=True)  # noqa: DJ001

    # Runway
    runway_months = models.FloatField(null=True)
    runway_capped = models.BooleanField(null=True)

    # Trials
    total_studies = models.IntegerField(null=True)
    active_phase_3 = models.IntegerField(null=True)
    active_phase_2 = models.IntegerField(null=True)
    latest_trial_nct = models.CharField(max_length=16, null=True)  # noqa: DJ001
    latest_trial_status = models.CharField(max_length=32, null=True)  # noqa: DJ001

    # Short volume (FINRA)
    short_volume_latest_ratio = models.DecimalField(max_digits=8, decimal_places=4, null=True)
    short_volume_trend = models.CharField(max_length=8, null=True)  # noqa: DJ001
    short_volume_delta_pts = models.FloatField(null=True)

    # Insider activity (Form 4)
    insider_filings_count = models.IntegerField(null=True)
    insider_buys_count = models.IntegerField(null=True)
    insider_sales_count = models.IntegerField(null=True)
    insider_officers_directors_buying = models.IntegerField(null=True)
    insider_officers_directors_selling = models.IntegerField(null=True)
    insider_pct_10b5_1_dollars = models.FloatField(null=True)

    carried_inputs = ArrayField(models.CharField(max_length=64))
    missing_inputs = ArrayField(models.CharField(max_length=64))
    field_meta = models.JSONField(
        help_text="Per source group: its as-of date, age in days, staleness and reason codes."
    )
    is_fixture = models.BooleanField(help_text="True when the score run came from a fixture load.")

    class Meta:
        managed = False
        db_table = "company_page"
