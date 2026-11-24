"""Give the read model the rest of the company page: institutional ownership, runway detail,
the catalyst candidates and the score components, so the company-page API reads one view.

The view gains columns at the end, so existing readers are unaffected."""

import importlib

from django.db import migrations, models

OLD = importlib.import_module("apps.web.migrations.0004_company_page_screener_fields").NEW_VIEW

NEW_VIEW = OLD.replace(
    "SELECT id, as_of_date, finviz_as_of_date\n    FROM scoring_scorerun",
    "SELECT id, as_of_date, finviz_as_of_date, scored_at\n    FROM scoring_scorerun",
).replace(
    "    cs.financing_date\nFROM latest l",
    """    cs.financing_date,
    fs.inst_own,
    l.scored_at,
    sr.components AS score_components,
    sr.input_resolution AS score_input_resolution,
    rc.status AS runway_status,
    rc.calc_date AS runway_calc_date,
    rc.cash_usd AS runway_cash_usd,
    rc.cash_tag AS runway_cash_tag,
    rc.cash_end_date AS runway_balance_sheet_date,
    rc.operating_burn_monthly_usd AS runway_operating_burn_usd,
    rc.trial_burn_monthly_usd AS runway_trial_burn_usd,
    rc.burn_total_monthly_usd AS runway_burn_total_usd,
    rc.burn_source AS runway_burn_source,
    rc.cap_reason AS runway_cap_reason,
    rc.trial_burn_lines AS runway_trial_burn_lines,
    cs.status AS catalyst_status,
    cs.as_of_date AS catalyst_as_of,
    cs.candidates AS catalyst_candidates,
    cs.clinical_source_nct AS catalyst_source_nct,
    cs.fallback_end_raw AS catalyst_fallback_end_raw
FROM latest l""",
)
assert NEW_VIEW != OLD


def _add(name, field):
    return migrations.AddField(
        model_name="companypage", name=name, field=field, preserve_default=False
    )


def _money(name):
    return _add(name, models.DecimalField(max_digits=22, decimal_places=2, null=True, default=None))


class Migration(migrations.Migration):
    dependencies = [("web", "0004_company_page_screener_fields")]

    operations = [
        _add("inst_own", models.FloatField(null=True, default=None)),
        _add("scored_at", models.DateTimeField(null=True, default=None)),
        _add("score_components", models.JSONField(default=dict)),
        _add("score_input_resolution", models.JSONField(default=dict)),
        _add("runway_status", models.CharField(max_length=16, null=True, default=None)),
        _add("runway_calc_date", models.DateField(null=True, default=None)),
        _money("runway_cash_usd"),
        _add("runway_cash_tag", models.CharField(max_length=128, null=True, default=None)),
        _add("runway_balance_sheet_date", models.DateField(null=True, default=None)),
        _money("runway_operating_burn_usd"),
        _money("runway_trial_burn_usd"),
        _money("runway_burn_total_usd"),
        _add("runway_burn_source", models.CharField(max_length=24, null=True, default=None)),
        _add("runway_cap_reason", models.CharField(max_length=16, null=True, default=None)),
        _add("runway_trial_burn_lines", models.JSONField(null=True, default=None)),
        _add("catalyst_status", models.CharField(max_length=16, null=True, default=None)),
        _add("catalyst_as_of", models.DateField(null=True, default=None)),
        _add("catalyst_candidates", models.JSONField(null=True, default=None)),
        _add("catalyst_source_nct", models.CharField(max_length=16, null=True, default=None)),
        _add("catalyst_fallback_end_raw", models.CharField(max_length=16, null=True, default=None)),
        migrations.RunSQL(
            "CREATE OR REPLACE VIEW company_page AS"
            + NEW_VIEW.split("CREATE VIEW company_page AS", 1)[1],
            reverse_sql="DROP VIEW company_page; CREATE VIEW company_page AS"
            + OLD.split("CREATE VIEW company_page AS", 1)[1],
        ),
    ]
