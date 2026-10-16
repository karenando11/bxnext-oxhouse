"""Seed the default screen. Re-running never duplicates or overwrites an edited screen."""

from django.db import migrations

DEFAULT_SCREEN = {
    "mcap_min_musd": 75,
    "mcap_max_musd": 1000,
    "price_min": 1,
    "avg_vol_min_k": 50,
    "shares_outstanding_max_m": 500,
    "composite_floor": 5.0,
    "outlier_floor": None,
    "include_sparse": True,
}


def seed(apps, schema_editor):
    ScreenDefinition = apps.get_model("screener", "ScreenDefinition")
    if not ScreenDefinition.objects.filter(is_default=True).exists():
        ScreenDefinition.objects.create(
            name="Default screen", is_default=True, config=DEFAULT_SCREEN, created_by="seed"
        )


def unseed(apps, schema_editor):
    apps.get_model("screener", "ScreenDefinition").objects.filter(
        is_default=True, created_by="seed"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("screener", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
