"""Seed the default settings. Re-running never duplicates or overwrites edited values."""

from django.db import migrations

DEFAULT_SETTINGS = {
    # Finviz universe
    "finviz.view": 151,
    "finviz.sector_filter": "sec_healthcare",
    "finviz.row_count_change_alert_pct": 20,
    # Carry-forward maximum ages
    "max_age.sec_runway_days": 120,
    "max_age.ctgov_days": 7,
    "max_age.short_volume_trading_days": 5,
    "max_age.insider_days": 7,
    "max_age.finviz_days": 0,  # Finviz fields are never carried forward
    # Trial burn placeholder rates, USD per active recruiting trial per month
    "runway.trial_burn_rates_usd": {"phase_1": 150000, "phase_2": 800000, "phase_3": 2500000, "other": 100000},
    "runway.cap_months": 360,
    # ClinicalTrials.gov sponsor matching
    "ctgov.sponsor_match_threshold": 0.6,
    # Schedule, US Eastern
    "schedule.market_stage_et": "16:30",
    "schedule.filings_stage_et": "23:30",
    # Stale-data banners
    "stale.snapshot_days": 3,
    "stale.balance_sheet_days": 120,
}


def seed(apps, schema_editor):
    Setting = apps.get_model("platform", "Setting")
    for key, value in DEFAULT_SETTINGS.items():
        Setting.objects.get_or_create(key=key, defaults={"value": value, "updated_by": "seed"})


def unseed(apps, schema_editor):
    apps.get_model("platform", "Setting").objects.filter(
        key__in=list(DEFAULT_SETTINGS), updated_by="seed"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("platform", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
