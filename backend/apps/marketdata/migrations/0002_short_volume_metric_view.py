"""Short-volume metrics as a function of the market date, plus a view for today.

Per company and trading day the volumes of all reporting markets are summed first. The latest
ratio is short volume over total volume (0 to 1, four decimals). The 5-day and 30-day averages
are means of daily ratios over the company's last 5 and 30 days with data, so the 30-day window
includes the latest 5 days (a parity quirk). Fewer than 5 days of data give NULL metrics with
reason ``insufficient_history``. ``age_days`` is the calendar distance from the market date to the
latest trading day with data; the data is stale after more than 5 weekdays (market holidays are
not modelled).
"""

from django.db import migrations

FUNCTION = """
CREATE FUNCTION short_volume_metric_at(as_of date)
RETURNS TABLE (
    company_id bigint,
    as_of_date date,
    latest_trade_date date,
    latest_ratio numeric,
    avg_ratio_5d double precision,
    avg_ratio_30d double precision,
    delta_pts double precision,
    trend text,
    days_in_5d integer,
    days_in_30d integer,
    status text,
    reason_codes text[],
    age_days integer,
    is_stale boolean
)
LANGUAGE sql STABLE AS $$
WITH daily AS (
    SELECT s.company_id,
           s.trade_date,
           sum(s.short_volume)::numeric / sum(s.total_volume) AS ratio
    FROM marketdata_shortvolumedaily s
    WHERE s.company_id IS NOT NULL AND s.trade_date <= as_of
    GROUP BY s.company_id, s.trade_date
    HAVING sum(s.total_volume) > 0
),
ranked AS (
    SELECT d.*, row_number() OVER (PARTITION BY d.company_id ORDER BY d.trade_date DESC) AS rn
    FROM daily d
),
agg AS (
    SELECT r.company_id,
           max(r.trade_date) FILTER (WHERE r.rn = 1) AS latest_trade_date,
           max(r.ratio) FILTER (WHERE r.rn = 1) AS latest_ratio,
           avg(r.ratio) FILTER (WHERE r.rn <= 5) AS avg5,
           avg(r.ratio) AS avg30,
           count(*) FILTER (WHERE r.rn <= 5)::integer AS n5,
           count(*)::integer AS n30
    FROM ranked r
    WHERE r.rn <= 30
    GROUP BY r.company_id
)
SELECT a.company_id,
       as_of,
       a.latest_trade_date,
       CASE WHEN a.n5 >= 5 THEN round(a.latest_ratio, 4) END,
       CASE WHEN a.n5 >= 5 THEN a.avg5::double precision END,
       CASE WHEN a.n5 >= 5 THEN a.avg30::double precision END,
       CASE WHEN a.n5 >= 5 THEN ((a.avg5 - a.avg30) * 100)::double precision END,
       CASE WHEN a.n5 < 5 THEN NULL
            WHEN a.avg5 > a.avg30 THEN 'rising'
            WHEN a.avg5 < a.avg30 THEN 'falling'
            ELSE 'flat' END,
       a.n5,
       a.n30,
       CASE WHEN a.n5 >= 5 THEN 'ok' ELSE 'insufficient_history' END,
       CASE WHEN a.n5 >= 5 THEN ARRAY[]::text[] ELSE ARRAY['insufficient_history']::text[] END,
       (as_of - a.latest_trade_date)::integer,
       (SELECT count(*) FROM generate_series(
            a.latest_trade_date + 1, as_of, interval '1 day') AS g(d)
         WHERE extract(isodow FROM g.d) < 6) > 5
FROM agg a
$$;
"""

VIEW = """
CREATE VIEW short_volume_metric AS
SELECT * FROM short_volume_metric_at((now() AT TIME ZONE 'America/New_York')::date);
"""


class Migration(migrations.Migration):
    dependencies = [("marketdata", "0001_initial")]

    operations = [
        migrations.RunSQL(FUNCTION, "DROP FUNCTION IF EXISTS short_volume_metric_at(date)"),
        migrations.RunSQL(VIEW, "DROP VIEW IF EXISTS short_volume_metric"),
    ]
