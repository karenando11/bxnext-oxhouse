"""Seed the production formula version: the parity configuration with carry-forward on.

Step 4 keeps carry-forward off for parity and on in production. The parity version is frozen, so
production is a version of its own, copied from parity as it stands when this runs. The scorer
reads the switch from whichever formula it is run with (``score_day --formula production``).
"""

from django.db import migrations
from django.utils import timezone


def seed(apps, schema_editor):
    FormulaVersion = apps.get_model("scoring", "FormulaVersion")
    parity = FormulaVersion.objects.filter(name="parity").order_by("version").first()
    if parity is None:
        return
    FormulaVersion.objects.get_or_create(
        name="production",
        version=1,
        defaults={
            "effective_from": timezone.now(),
            "config": parity.config,
            "quirks": {**parity.quirks, "carry_forward": True},
            "notes": "Parity configuration and quirks, with carry-forward on (Step 15).",
        },
    )


def unseed(apps, schema_editor):
    apps.get_model("scoring", "FormulaVersion").objects.filter(
        name="production", version=1
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("scoring", "0005_scoreresult_input_resolution")]

    operations = [migrations.RunPython(seed, unseed)]
