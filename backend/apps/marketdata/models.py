"""Daily Finviz snapshots and FINRA short volume.

Units used throughout: market cap in USD millions, shares in millions, average volume in
thousands of shares, percentages as plain numbers (12.4 means 12.4%), money in USD.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.companies.models import Company
from apps.platform.models import SourceCall


def _pct(label: str) -> models.FloatField:
    return models.FloatField(null=True, blank=True, help_text=f"{label}, percent (12.4 = 12.4%).")


def _num(label: str, unit: str = "") -> models.FloatField:
    suffix = f", {unit}" if unit else ""
    return models.FloatField(null=True, blank=True, help_text=f"{label}{suffix}.")


class FinvizSnapshot(models.Model):
    """One row per company per day, from the Finviz Elite export.

    Blank cells and ``-`` are stored as NULL, never zero. The two 52-week column pairs share
    header text, so the parser reads by position.
    """

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="snapshots")
    as_of_date = models.DateField()
    source_call = models.ForeignKey(SourceCall, null=True, blank=True, on_delete=models.PROTECT)

    # Identity, as delivered
    company_name_raw = models.CharField(max_length=255, blank=True)
    sector_raw = models.CharField(max_length=100, blank=True)
    industry_raw = models.CharField(max_length=100, blank=True)

    # Price and size
    price = _num("Last price", "USD")
    mcap_musd = _num("Market cap", "USD millions")
    shr_out_m = _num("Shares outstanding", "millions")
    shr_float_m = _num("Shares in the public float", "millions")
    avg_vol_k = _num("Average daily volume", "thousands of shares")
    rel_vol = _num("Relative volume, ratio to the average")

    # Performance
    perf_w = _pct("Performance, week")
    perf_m = _pct("Performance, month")
    perf_q = _pct("Performance, quarter")
    perf_h = _pct("Performance, half year")
    perf_ytd = _pct("Performance, year to date")
    perf_y = _pct("Performance, year")

    # Volatility and technicals
    vol_w = _pct("Volatility, week")
    vol_m = _pct("Volatility, month")
    beta = _num("Beta")
    atr = _num("Average true range", "USD")
    sma20 = _pct("Distance from the 20-day moving average")
    sma50 = _pct("Distance from the 50-day moving average")
    sma200 = _pct("Distance from the 200-day moving average")
    w52h = _pct("Distance from the 52-week high (a percent distance, not a price)")
    w52l = _pct("Distance from the 52-week low (a percent distance, not a price)")
    rsi = _num("Relative strength index (14), 0 to 100")

    # Valuation
    pe = _num("Price to earnings, ratio")
    fwd_pe = _num("Forward price to earnings, ratio")
    ps = _num("Price to sales, ratio")
    pb = _num("Price to book, ratio")
    pcash = _num("Price to cash per share, ratio")

    # Balance sheet and margins
    quick_ratio = _num("Quick ratio")
    cur_ratio = _num("Current ratio")
    ltd_eq = _num("Long-term debt to equity, ratio")
    td_eq = _num("Total debt to equity, ratio")
    gross_margin = _pct("Gross margin")
    op_margin = _pct("Operating margin")
    profit_margin = _pct("Profit margin")
    roa = _pct("Return on assets")
    roe = _pct("Return on equity")
    cash_sh = _num("Cash per share", "USD")

    # Ownership and trading
    insider_own = _pct("Insider ownership")
    inst_own = _pct("Institutional ownership")
    inst_trans = _pct("Institutional transactions")
    insider_trans = _pct("Insider transactions")
    short_float = _pct("Short interest as a share of the float")
    short_ratio = _num("Short ratio, days to cover")

    # Earnings
    eps_this = _pct("Estimated EPS growth this year")
    eps_next = _pct("Estimated EPS growth next year")
    earnings_date_text = models.CharField(
        max_length=64, blank=True, help_text="Earnings date exactly as delivered."
    )
    earnings_date = models.DateField(
        null=True, blank=True, help_text="Parsed from the text when it matches M/D/YYYY."
    )

    extras = models.JSONField(default=dict, blank=True, help_text="Columns added later.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "as_of_date"], name="finvizsnapshot_company_date_unique"
            )
        ]
        indexes = [models.Index(fields=["as_of_date"], name="finvizsnapshot_date")]


class ShortVolumeDaily(models.Model):
    """One FINRA Reg SHO row per ticker, trading day and reporting market.

    Only rows for universe tickers are stored; the raw file stays in object storage.
    """

    ticker_raw = models.CharField(max_length=16)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.PROTECT)
    trade_date = models.DateField()
    short_volume = models.BigIntegerField(help_text="Shares sold short that day.")
    short_exempt_volume = models.BigIntegerField(null=True, blank=True, help_text="Shares.")
    total_volume = models.BigIntegerField(help_text="Shares traded that day.")
    market = models.CharField(max_length=32, help_text="Reporting market code.")
    source_call = models.ForeignKey(SourceCall, null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticker_raw", "trade_date", "market"], name="shortvolume_ticker_date_market"
            )
        ]
        indexes = [models.Index(fields=["company", "-trade_date"], name="shortvolume_company_date")]


class ShortVolumeMetric(models.Model):
    """Read-only view over ``ShortVolumeDaily`` for today's market date (see the migration).

    ``latest_ratio`` is short volume divided by total volume, 0 to 1 at four decimals. The 30-day
    average includes the latest 5 days (a parity quirk of the current system).
    """

    company = models.OneToOneField(Company, primary_key=True, on_delete=models.DO_NOTHING)
    as_of_date = models.DateField()
    latest_trade_date = models.DateField()
    latest_ratio = models.DecimalField(max_digits=8, decimal_places=4, null=True)
    avg_ratio_5d = models.FloatField(null=True)
    avg_ratio_30d = models.FloatField(null=True)
    delta_pts = models.FloatField(null=True, help_text="Percentage points, 5-day minus 30-day.")
    trend = models.CharField(max_length=8, null=True)  # noqa: DJ001
    days_in_5d = models.IntegerField()
    days_in_30d = models.IntegerField()
    status = models.CharField(max_length=24)
    reason_codes = ArrayField(models.CharField(max_length=40), default=list)
    age_days = models.IntegerField()
    is_stale = models.BooleanField()

    class Meta:
        managed = False
        db_table = "short_volume_metric"
