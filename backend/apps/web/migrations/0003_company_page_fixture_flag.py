"""Mark fixture-backed pages and give fields a fixture reason when their source is absent.

A score run whose pipeline run has a ``fixture`` source call is a fixture run. For such runs the
short-volume, insider and trial-list groups report ``fixture_not_available`` instead of a generic
reason, and ``is_fixture`` is true so the UI can show its banner.
"""

import importlib

from django.db import migrations, models

NEW_VIEW = """
CREATE VIEW company_page AS
WITH latest AS (
    SELECT id, as_of_date, finviz_as_of_date
    FROM scoring_scorerun
    WHERE status = 'complete'
    ORDER BY as_of_date DESC, id DESC
    LIMIT 1
),
today AS (SELECT (now() AT TIME ZONE 'America/New_York')::date AS d),
fx AS (
    SELECT EXISTS (
        SELECT 1
        FROM scoring_scorerun s
        JOIN platform_sourcecall x ON x.run_id = s.pipeline_run_id AND x.feed = 'fixture'
        WHERE s.id = (SELECT id FROM latest)
    ) AS is_fixture
)
SELECT
    c.id AS company_id,
    th.ticker,
    c.name AS company_name,
    c.industry,
    l.id AS score_run_id,
    l.as_of_date,
    sr.composite,
    sr.divergence,
    sr.cash_health,
    sr.milestone,
    sr.outlier,
    sr.recency,
    sr.mcap_bucket,
    sr.peer_group_key,
    sr.peer_n,
    fs.price,
    fs.mcap_musd,
    fs.w52h,
    fs.sma200,
    fs.rsi,
    fs.avg_vol_k,
    fs.rel_vol,
    fs.perf_w,
    fs.perf_m,
    fs.perf_q,
    fs.perf_ytd,
    fs.cash_sh,
    fs.quick_ratio,
    fs.td_eq,
    fs.inst_trans,
    fs.short_float,
    fs.insider_own,
    fs.gross_margin,
    fs.op_margin,
    fs.earnings_date_text,
    rc.runway_months,
    rc.runway_capped,
    cs.total_studies,
    cs.active_phase_3,
    cs.active_phase_2,
    cs.latest_trial_nct,
    cs.latest_trial_status,
    sv.latest_ratio AS short_volume_latest_ratio,
    sv.trend AS short_volume_trend,
    sv.delta_pts AS short_volume_delta_pts,
    im.filings_count AS insider_filings_count,
    im.open_market_buys_count AS insider_buys_count,
    im.open_market_sales_count AS insider_sales_count,
    im.officers_directors_buying_count AS insider_officers_directors_buying,
    im.officers_directors_selling_count AS insider_officers_directors_selling,
    im.pct_10b5_1_dollars AS insider_pct_10b5_1_dollars,
    sr.carried_inputs,
    sr.missing_inputs,
    jsonb_build_object(
        'market', jsonb_build_object(
            'as_of', fs.as_of_date,
            'age_days', (t.d - fs.as_of_date),
            'source_call_id', fs.source_call_id,
            'reasons', CASE
                WHEN fs.id IS NULL AND fx.is_fixture THEN to_jsonb(ARRAY['fixture_not_available'])
                ELSE '[]'::jsonb
            END
        ),
        'scores', jsonb_build_object('as_of', l.as_of_date, 'age_days', (t.d - l.as_of_date)),
        'runway', jsonb_build_object(
            'calc_date', rc.calc_date,
            'balance_sheet_date', rc.cash_end_date,
            'carried_forward', rc.carried_forward,
            'reasons', to_jsonb(coalesce(rc.reason_codes, ARRAY[]::text[]))
        ),
        'catalyst', jsonb_build_object(
            'as_of', cs.as_of_date,
            'carried_forward', cs.carried_forward,
            'reasons', to_jsonb(coalesce(cs.reason_codes, ARRAY[]::text[]))
        ),
        'short_volume', jsonb_build_object(
            'latest_trade_date', sv.latest_trade_date,
            'age_days', sv.age_days,
            'is_stale', coalesce(sv.is_stale, true),
            'reasons', CASE
                WHEN sv.company_id IS NULL AND fx.is_fixture
                    THEN to_jsonb(ARRAY['fixture_not_available'])
                ELSE to_jsonb(coalesce(sv.reason_codes, ARRAY['not_in_finra_file']))
            END
        ),
        'insider', jsonb_build_object(
            'age_days', im.age_days,
            'is_stale', coalesce(im.is_stale, true),
            'reasons', CASE
                WHEN im.status = 'unavailable' AND fx.is_fixture
                    THEN to_jsonb(ARRAY['fixture_not_available'])
                ELSE to_jsonb(coalesce(im.reason_codes, ARRAY[]::text[]))
            END
        ),
        'trial_lists', jsonb_build_object(
            'reasons', CASE
                WHEN EXISTS (SELECT 1 FROM trials_ctstudyversion z WHERE z.company_id = c.id)
                    THEN '[]'::jsonb
                WHEN fx.is_fixture THEN to_jsonb(ARRAY['fixture_not_available'])
                ELSE to_jsonb(ARRAY['no_trials'])
            END
        )
    ) AS field_meta,
    fx.is_fixture
FROM latest l
CROSS JOIN today t
CROSS JOIN fx
JOIN scoring_scoreresult sr ON sr.score_run_id = l.id
JOIN companies_company c ON c.id = sr.company_id
LEFT JOIN companies_tickerhistory th ON th.company_id = c.id AND th.valid_to IS NULL
LEFT JOIN marketdata_finvizsnapshot fs
       ON fs.company_id = c.id AND fs.as_of_date = l.finviz_as_of_date
LEFT JOIN sec_runwaycalc rc ON rc.id = sr.runway_calc_id
LEFT JOIN trials_catalystsignal cs ON cs.id = sr.catalyst_signal_id
LEFT JOIN short_volume_metric sv ON sv.company_id = c.id
LEFT JOIN insider_metric_daily im ON im.company_id = c.id;
"""


def _old_view():
    module = importlib.import_module("apps.web.migrations.0002_company_page_view")
    return module.VIEW


class Migration(migrations.Migration):
    dependencies = [
        ("web", "0002_company_page_view"),
        ("platform", "0001_initial"),
        ("scoring", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="companypage",
            name="is_fixture",
            field=models.BooleanField(
                default=False, help_text="True when the score run came from a fixture load."
            ),
            preserve_default=False,
        ),
        migrations.RunSQL(
            "CREATE OR REPLACE VIEW company_page AS" + NEW_VIEW.split("CREATE VIEW company_page AS", 1)[1],
            reverse_sql="DROP VIEW company_page; " + _old_view(),
        ),
    ]
